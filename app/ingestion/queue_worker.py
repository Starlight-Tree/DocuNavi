"""R2 のイベント通知（Cloudflare Queue）を pull して取り込むコマンド（docs/r2-ingest-design.md の第 2 版）。

通知は、遅れる（実測で最大約 6 分）、順序が前後する、重複しうる。だから通知の中身は「どの PDF を見に行くか」の合図としてだけ使い、
新旧は R2 の現物と DB の内容のハッシュで決まる（取り込みが冪等）。状態の持ち主は DB の `documents` で、Queue ではない。
取りこぼし（無料プランの保持は 24 時間）は、毎時の `python -m app.ingestion.sync` が拾う。このコマンドは R2 を読むだけで書かない
（通知の元になる書き込みを自分で起こさないので、ループしない）。

    python -m app.ingestion.queue_worker            # 設定の確認だけ（Queue には触れない）
    python -m app.ingestion.queue_worker --apply    # Queue から pull し、取り込み・埋め込みをして ack する（埋め込みは課金される）

`R2_ACCOUNT_ID` `R2_BUCKET` `R2_CREDENTIALS_JSON`（または `R2_CREDENTIALS_FILE`）、`DATABASE_URL`、`GEMINI_API_KEY`、
`INGEST_QUEUE_ID`、`INGEST_QUEUE_TOKEN`（Queues の read + write の API トークン）が要る。値は表示しない。

メッセージの扱い（入力は信用しない）: bucket が違う・対象外の action・キーの形が `<資料群>/<ファイル名>.pdf` でない・大きすぎる、は
取り込まずに ack して捨てる（再試行しても直らない）。ファイルが R2 に無いときも ack する（毎時の突き合わせが拾う）。
取り込みの終了コードで ack と retry を決める: 0・3 は ack（3 の失敗は documents.failure_reason に残る）、1 は遅延付きの retry、
2（設定の誤り）は ack も retry もしない（`visibility_timeout` の後に再配信される。ログで知らせる）。
埋め込みは、取り込み後に 1 回だけ呼ぶ。失敗したら、その回の取り込みの分は retry にする。
続けて質問の生成（`python -m app.ingestion.suggest --if-enabled`。`SUGGEST_ENABLED=true` のときだけ動く）を呼ぶ。その成否は ack・retry に影響させない。

終了コード: 0 = 完了（Queue が空になった、または 1 回の起動の上限に達した）、1 = 一時的な失敗（Queue・R2・DB・埋め込み）、
2 = 設定の誤り・使い方の誤り。
"""

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.embedding import DEFAULT_MAX_CALLS, DEFAULT_MAX_CHUNKS
from app.ingestion.service import COLLECTION_PATTERN
from app.ingestion.suggest import after_ingest
from app.pdf_originals import MAX_ORIGINAL_BYTES

logger = logging.getLogger(__name__)

Command = Callable[[Sequence[str]], int]

# 取り込みの対象にする通知の action。DeleteObject は扱わない（R2 から消しても DB の文書は残す。設計の「失敗の扱い」）
HANDLED_ACTIONS = frozenset({"PutObject", "CopyObject", "CompleteMultipartUpload"})
# 取り込みの終了コード（app/ingestion/__main__.py）。0 = 完了、3 = 一部の PDF が取り込めなかった（失敗は DB に残る）
_INGEST_DONE = {0, 3}

# 1 回の pull の件数。小さめにして、1 件の失敗で多くの通知を再配信させない
DEFAULT_BATCH_SIZE = 5
# pull のくり返しの上限（Job の実行時間を区切る）。残りは次の起動か、毎時の突き合わせが拾う
DEFAULT_MAX_BATCHES = 10
# 取り込み + 埋め込みの 1 回分より長く取る。短いと処理中に再配信され、同じ PDF を二重に処理する（冪等なので壊れはしない）
DEFAULT_VISIBILITY_TIMEOUT_SECONDS = 15 * 60
RETRY_DELAY_SECONDS = 60

_API = "https://api.cloudflare.com/client/v4"


class QueueError(Exception):
    """Queue の応答が成功を示さなかった。中身は出さない。"""


@dataclass(frozen=True)
class QueueMessage:
    id: str
    lease_id: str
    body: Any
    attempts: int = 1


@dataclass(frozen=True)
class Target:
    """取り込む PDF。`key` は R2 のキー（`<資料群>/<ファイル名>`）。"""

    collection: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.collection}/{self.name}"


@dataclass(frozen=True)
class Drop:
    """取り込まずに ack して捨てる通知。`reason` は固定の短い語（ログ用。通知の中身は入れない）。"""

    reason: str


def _parse_body(body: Any) -> Mapping[str, Any] | None:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return None
    return body if isinstance(body, Mapping) else None


def classify(body: Any, bucket: str) -> Target | Drop:
    """通知 1 件を検査する。キーの文字列は、パスやシェルに渡す前に形で絞る。"""
    message = _parse_body(body)
    if message is None:
        return Drop("body")
    if message.get("bucket") != bucket:
        return Drop("bucket")
    if message.get("action") not in HANDLED_ACTIONS:
        return Drop("action")
    obj = message.get("object")
    key = obj.get("key") if isinstance(obj, Mapping) else None
    if not isinstance(key, str):
        return Drop("key")
    collection, slash, name = key.partition("/")
    if (
        not slash
        or not COLLECTION_PATTERN.fullmatch(collection)
        or not name
        or "/" in name
        or name in (".", "..")
        or "\x00" in name
        or not name.lower().endswith(".pdf")
    ):
        return Drop("key")
    size = obj.get("size")
    if isinstance(size, int) and not isinstance(size, bool) and size > MAX_ORIGINAL_BYTES:
        return Drop("size")
    return Target(collection, name)


class QueueClient:
    """Cloudflare Queues の HTTP pull と ack（`urlopen` は、テストで偽物に差し替える）。"""

    def __init__(
        self,
        account_id: str,
        queue_id: str,
        token: str,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = 30.0,
    ) -> None:
        account = urllib.parse.quote(account_id, safe="")
        queue = urllib.parse.quote(queue_id, safe="")
        self._base = f"{_API}/accounts/{account}/queues/{queue}/messages"
        self._token = token
        self._urlopen = urlopen
        self._timeout = timeout

    def _post(self, action: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - 固定の https の URL
            f"{self._base}/{action}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        with self._urlopen(request, timeout=self._timeout) as response:
            data = json.loads(response.read())
        if not isinstance(data, Mapping) or data.get("success") is not True:
            raise QueueError("unsuccessful")
        return data

    def pull(self, batch_size: int, visibility_timeout_seconds: int) -> list[QueueMessage]:
        data = self._post(
            "pull", {"batch_size": batch_size, "visibility_timeout_ms": visibility_timeout_seconds * 1000}
        )
        result = data.get("result")
        raw = result.get("messages") if isinstance(result, Mapping) else None
        messages: list[QueueMessage] = []
        for item in raw or []:
            if isinstance(item, Mapping) and isinstance(item.get("lease_id"), str):
                attempts = item.get("attempts")
                messages.append(
                    QueueMessage(
                        id=str(item.get("id", "")),
                        lease_id=item["lease_id"],
                        body=item.get("body"),
                        attempts=attempts if isinstance(attempts, int) else 1,
                    )
                )
        return messages

    def ack(self, acks: Sequence[str], retries: Sequence[str], retry_delay_seconds: int = RETRY_DELAY_SECONDS) -> None:
        if not acks and not retries:
            return
        self._post(
            "ack",
            {
                "acks": [{"lease_id": lease} for lease in acks],
                "retries": [{"lease_id": lease, "delay_seconds": retry_delay_seconds} for lease in retries],
            },
        )


@dataclass
class Outcome:
    acked: int = 0
    dropped: int = 0
    retried: int = 0
    left: int = 0  # ack も retry もしなかった分（設定の誤り。visibility_timeout の後に再配信される）
    ingested: int = 0
    transient: bool = False
    config_error: bool = False
    drops: dict[str, int] = field(default_factory=dict)

    def exit_code(self) -> int:
        if self.config_error:
            return 2
        return 1 if self.transient else 0

    def add(self, other: "Outcome") -> None:
        self.acked += other.acked
        self.dropped += other.dropped
        self.retried += other.retried
        self.left += other.left
        self.ingested += other.ingested
        self.transient |= other.transient
        self.config_error |= other.config_error


def process_batch(
    messages: Sequence[QueueMessage],
    *,
    bucket: str,
    exists: Callable[[str], bool],
    ingest_main: Command,
    embed_main: Command,
    max_chunks: int,
    max_calls: int,
    suggest_main: Command | None = None,
) -> tuple[list[str], list[str], Outcome]:
    """1 回分の通知を処理し、(ack する lease, retry する lease, 結果) を返す。同じ PDF の通知は 1 回だけ取り込む。"""
    outcome = Outcome()
    acks: list[str] = []
    retries: list[str] = []
    waiting: dict[Target, list[str]] = {}  # 取り込む PDF → その通知の lease

    for message in messages:
        verdict = classify(message.body, bucket)
        if isinstance(verdict, Drop):
            logger.warning("通知を取り込まずに捨てました (reason=%s, message_id=%s)", verdict.reason, message.id)
            outcome.dropped += 1
            outcome.drops[verdict.reason] = outcome.drops.get(verdict.reason, 0) + 1
            acks.append(message.lease_id)
        else:
            waiting.setdefault(verdict, []).append(message.lease_id)

    done: dict[Target, list[str]] = {}  # 取り込めた PDF → lease（埋め込みの結果を待って ack する）
    for target, leases in waiting.items():
        try:
            if not exists(target.key):
                logger.warning("R2 に無い PDF の通知を捨てました (collection=%s)", target.collection)
                outcome.dropped += len(leases)
                outcome.drops["missing"] = outcome.drops.get("missing", 0) + len(leases)
                acks += leases
                continue
            code = ingest_main(["--input", f"r2://{target.key}", "--apply"])
        except Exception as exc:  # noqa: BLE001 - 例外の文字列に接続先・鍵の ID が入りうる。クラス名だけを出す
            logger.error("取り込みの前後で失敗しました (%s)", type(exc).__name__)
            code = 1
        if code in _INGEST_DONE:
            done[target] = leases
        elif code == 2:
            logger.warning("取り込みが設定の誤りで終わりました。再配信を待ちます (collection=%s)", target.collection)
            outcome.config_error = True
            outcome.left += len(leases)
        else:
            outcome.transient = True
            outcome.retried += len(leases)
            retries += leases

    if done:
        try:
            embed_code = embed_main(["--apply", "--max-chunks", str(max_chunks), "--max-calls", str(max_calls)])
        except Exception as exc:  # noqa: BLE001
            logger.error("埋め込みで失敗しました (%s)", type(exc).__name__)
            embed_code = 1
        if suggest_main is not None:
            after_ingest(suggest_main, apply=True)  # 失敗しても ack・retry には影響させない（取り込みは済んでいる）
        leases = [lease for group in done.values() for lease in group]
        if embed_code == 0:
            outcome.ingested += len(done)
            outcome.acked += len(leases)
            acks += leases
        elif embed_code == 2:
            outcome.config_error = True
            outcome.left += len(leases)
        else:
            outcome.transient = True
            outcome.retried += len(leases)
            retries += leases
    outcome.acked += outcome.dropped  # 捨てた通知も ack する
    return acks, retries, outcome


def run_worker(
    client: Any,
    *,
    bucket: str,
    exists: Callable[[str], bool],
    ingest_main: Command,
    embed_main: Command,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    visibility_timeout_seconds: int = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    max_calls: int = DEFAULT_MAX_CALLS,
    suggest_main: Command | None = None,
) -> int:
    """Queue が空になるか、`max_batches` 回に達するまで、pull → 処理 → ack をくり返す。"""
    total = Outcome()
    for _ in range(max_batches):
        try:
            messages = client.pull(batch_size, visibility_timeout_seconds)
        except urllib.error.HTTPError as exc:
            # 401・403 は、トークンの誤り・権限不足（待っても直らない）。応答の本文は出さない
            print(f"Queue から pull できませんでした（HTTP {exc.code}）。", file=sys.stderr)
            return 2 if exc.code in (401, 403) else 1
        except Exception as exc:  # noqa: BLE001
            print(f"Queue から pull できませんでした（{type(exc).__name__}）。", file=sys.stderr)
            return 1
        if not messages:
            break
        acks, retries, outcome = process_batch(
            messages,
            bucket=bucket,
            exists=exists,
            ingest_main=ingest_main,
            embed_main=embed_main,
            max_chunks=max_chunks,
            max_calls=max_calls,
            suggest_main=suggest_main,
        )
        try:
            client.ack(acks, retries)
        except Exception as exc:  # noqa: BLE001
            # ack できなかった通知は、visibility_timeout の後に再配信される。取り込みは冪等なので、二重に走っても結果は同じ
            print(f"Queue へ ack できませんでした（{type(exc).__name__}）。", file=sys.stderr)
            return 1
        total.add(outcome)
        if outcome.config_error:
            break  # 設定の誤りは、次の回も同じ。続けても再配信が増えるだけ

    print(
        f"取り込み {total.ingested} 件、ack {total.acked} 件（うち捨てた通知 {total.dropped} 件）、"
        f"retry {total.retried} 件、見送り {total.left} 件。"
    )
    return total.exit_code()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingestion.queue_worker",
        description="R2 の通知を Queue から pull して取り込む（既定は設定の確認だけ。--apply で pull し、保存し、埋め込む）",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Queue から pull して取り込み、ack する（埋め込みは課金される）"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="1 回の pull の件数")
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES, help="pull のくり返しの上限")
    parser.add_argument(
        "--visibility-timeout",
        type=int,
        default=DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
        help="pull した通知を他へ見せない秒数（処理時間より長く）",
    )
    parser.add_argument("--max-chunks", type=int, default=DEFAULT_MAX_CHUNKS, help="埋め込みの対象の上限")
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, help="埋め込みの API への送信回数の上限")
    return parser


def _call_ingest(argv: Sequence[str]) -> int:
    from app.ingestion.__main__ import main

    return main(argv)


def _call_embed(argv: Sequence[str]) -> int:
    from app.ingestion.embed import main

    return main(argv)


def _call_suggest(argv: Sequence[str]) -> int:
    from app.ingestion.suggest import main

    return main(argv)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = os.environ if env is None else env
    limits = (args.batch_size, args.max_batches, args.visibility_timeout, args.max_chunks, args.max_calls)
    if any(value < 1 for value in limits):
        print("数を指定するオプションは、すべて 1 以上にしてください。", file=sys.stderr)
        return 2

    from app import r2_store

    try:
        config = r2_store.config_from_env(env)
    except r2_store.R2ConfigError as exc:
        print(f"R2 の設定が足りません（{exc}）。", file=sys.stderr)
        return 2
    queue_id = env.get("INGEST_QUEUE_ID", "").strip()
    token = env.get("INGEST_QUEUE_TOKEN", "").strip()
    if not queue_id or not token:
        print("INGEST_QUEUE_ID と INGEST_QUEUE_TOKEN が要ります（未設定です）。", file=sys.stderr)
        return 2
    if not args.apply:
        print("設定は揃っています。何も pull していません（--apply で pull し、取り込みます）。")
        return 0

    client = QueueClient(config.account_id, queue_id, token)
    store = r2_store.default_store()
    return run_worker(
        client,
        bucket=config.bucket,
        exists=lambda key: store.etag(key) is not None,
        ingest_main=_call_ingest,
        embed_main=_call_embed,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        visibility_timeout_seconds=args.visibility_timeout,
        max_chunks=args.max_chunks,
        max_calls=args.max_calls,
        suggest_main=_call_suggest,
    )


if __name__ == "__main__":
    raise SystemExit(main())
