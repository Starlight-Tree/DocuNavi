"""会話履歴（Postgres。`DB_BACKEND=d1` なら D1）の保守コマンド: 保存期間を過ぎた会話の削除と、1 件の会話の削除。

アプリ本体（`app/main.py`）からは使わない。運用者が、アプリと同じ接続設定（`DB_BACKEND=pg` は `DATABASE_URL`、`d1` は `D1_API_URL` / `D1_API_TOKEN`。`.env` も読む）で手元・ジョブから実行する。
本番は Postgres で、Cloud Run の Job `docunavi-history-cleanup` が `--older-than-days 7`（`--apply` なし）で動かしている。
既定は確認だけで、何も消さない。`--apply` を付けたときだけ削除する。

    python -m app.history_admin                                  # 7 日（既定）以上動きのない会話の数を表示する
    python -m app.history_admin --older-than-days 30             # 30 日以上動きのない会話の数を表示する
    python -m app.history_admin --older-than-days 30 --apply     # それらを削除する
    python -m app.history_admin --thread-id <thread_id> --apply  # 1 件の会話を削除する
    python -m app.history_admin --backfill-titles                # 題名が未保存の会話の数を表示する（何も書かない）
    python -m app.history_admin --backfill-titles --apply        # それらの題名を checkpoints.metadata の title へ書く

`--backfill-titles` は、題名の補完（削除ではない）。会話の一覧の題名は、保存時に `checkpoints.metadata` の `title` へ入れた値（`app/thread_title.py`）で、
Go の読み取り API（ops/read-api）はチェックポイント本体を復号せず、この値だけを読む。この機能より前に保存した会話には `title` が無いので、
画面の向き先を Go へ切り替える前に、1 回流す。各会話の最新のチェックポイントだけを見て、本文のあるユーザー発言があれば、その先頭を題名にする
（無い会話は、いまも「（題名なし）」と表示される。補完の対象外で、何度流しても同じ）。書くのは `metadata` の `title` だけ。流し直しても安全（題名のある行は触らない）。

日数を省くと 7 日（`DEFAULT_OLDER_THAN_DAYS`）。`--thread-id` と `--older-than-days` は同時に指定できない。
「動きのない」は、会話の最新のチェックポイントの書き込み時刻（`checkpoints.created_at`）で決める。
表示するのは件数だけで、`thread_id` は出さない（知っていれば会話を続けられる値のため）。
一覧を取ってから消すまでの間に新しい発言が入った会話も消えうる。保存期間を日単位で決める用途なので許容している。

終了コード: 0 = 完了、1 = DB に接続できない・テーブルが無いなどの失敗、2 = 使い方の誤り・接続先（`DATABASE_URL` か `D1_API_URL`）の未設定。
"""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from dotenv import load_dotenv

from app.d1 import D1Error, SyncD1
from app.d1_saver import D1Saver
from app.history import database_url
from app.pg import backend, sync_client
from app.pg_saver import PgSaver
from app.thread_title import stored_title

DEFAULT_OLDER_THAN_DAYS = 7

_STALE_THREADS_SQL = """
SELECT thread_id
FROM checkpoints
GROUP BY thread_id
HAVING max(created_at) < (unixepoch() - ? * 86400) * 1000
ORDER BY thread_id
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.history_admin",
        description="会話履歴の削除（既定は確認だけ。--apply で削除する）",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--older-than-days",
        type=int,
        metavar="N",
        help=f"N 日以上動きのない会話を対象にする（N は 1 以上。既定 {DEFAULT_OLDER_THAN_DAYS}）",
    )
    target.add_argument("--thread-id", metavar="ID", help="この 1 件の会話を対象にする")
    target.add_argument(
        "--backfill-titles",
        action="store_true",
        help="題名が未保存の会話に、題名を書き込む（削除ではない。無ければ件数を表示するだけ。--apply で書く）",
    )
    parser.add_argument("--apply", action="store_true", help="実際に削除する（無ければ件数を表示するだけ）")
    return parser


# 各会話（checkpoint_ns = ''）の最新のチェックポイントのうち、題名（metadata の title）が無い行。最新 = checkpoint_id の最大（app/api/threads.py と同じ）
_UNTITLED_LATEST_SQL = """
SELECT thread_id, checkpoint_id, type, checkpoint, metadata
FROM checkpoints
WHERE checkpoint_ns = ''
  AND json_extract(metadata, '$.title') IS ?
  AND checkpoint_id = (SELECT max(x.checkpoint_id) FROM checkpoints x WHERE x.thread_id = checkpoints.thread_id AND x.checkpoint_ns = '')
ORDER BY thread_id
"""
_SET_TITLE_SQL = "UPDATE checkpoints SET metadata = ? WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id = ?"
_BACKFILL_BATCH = 50


def _backfill_titles(db, apply: bool) -> int:
    """題名の無い会話の題名を、最新のチェックポイントから作って書く（`apply` でなければ数えるだけ）。件数だけを表示する（会話 ID・題名は出さない）。"""
    saver = (PgSaver if backend() == "pg" else D1Saver)(None)  # 復号だけに使う（接続は要らない）
    pending: list[tuple[str, tuple[Any, ...]]] = []
    untitled = writable = skipped = broken = 0
    written = 0
    for row in db.execute(_UNTITLED_LATEST_SQL, (None,)):  # title が NULL（IS ? に None）
        untitled += 1
        try:
            messages = saver.load_checkpoint(row).get("channel_values", {}).get("messages", [])
        except Exception:  # noqa: BLE001 - 壊れた行は飛ばす（中身も例外の文字列も出さない）
            broken += 1
            continue
        title = stored_title(messages)
        if title is None:
            skipped += 1
            continue
        writable += 1
        raw = row["metadata"]
        meta = dict(raw) if isinstance(raw, dict) else json.loads(raw or "{}")
        meta["title"] = title
        pending.append((_SET_TITLE_SQL, (json.dumps(meta, ensure_ascii=False), row["thread_id"], row["checkpoint_id"])))
        if apply and len(pending) >= _BACKFILL_BATCH:
            db.batch(pending)
            written += len(pending)
            pending = []
    if apply and pending:
        db.batch(pending)
        written += len(pending)
    print(
        f"題名が未保存の会話: {untitled} 件（題名を作れる: {writable} 件、本文のあるユーザー発言が無い: {skipped} 件、復号できない: {broken} 件）"
    )
    print(f"書き込んだ題名: {written} 件" if apply else "書き込みはしていません。--apply で題名を書きます")
    return 0


def _find_threads(db, args: argparse.Namespace) -> list[str]:
    if args.thread_id is not None:
        rows = db.execute("SELECT 1 AS ok FROM checkpoints WHERE thread_id = ? LIMIT 1", (args.thread_id,))
        return [args.thread_id] if rows else []
    return [row["thread_id"] for row in db.execute(_STALE_THREADS_SQL, (args.older_than_days,))]


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.thread_id is None and args.older_than_days is None and not args.backfill_titles:
        args.older_than_days = DEFAULT_OLDER_THAN_DAYS
    if args.older_than_days is not None and args.older_than_days < 1:
        print("--older-than-days は 1 以上にしてください。", file=sys.stderr)
        return 2
    if args.thread_id is not None and not args.thread_id.strip():
        print("--thread-id が空です。", file=sys.stderr)
        return 2

    load_dotenv()
    url = database_url()
    if not url:
        print(
            "DB の接続先（pg は DATABASE_URL、d1 は D1_API_URL）が未設定です（履歴がメモリのときは、消す対象がありません）。",
            file=sys.stderr,
        )
        return 2

    try:
        with sync_client(SyncD1, url) as db:
            if args.backfill_titles:
                return _backfill_titles(db, args.apply)
            threads = _find_threads(db, args)
            if not args.apply:
                print(f"対象の会話: {len(threads)} 件（削除はしていません。--apply で削除します）")
                return 0
            for thread_id in threads:
                db.batch(
                    [
                        ("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)),
                        ("DELETE FROM checkpoint_writes WHERE thread_id = ?", (thread_id,)),
                    ]
                )
            print(f"削除した会話: {len(threads)} 件")
            return 0
    except D1Error as exc:
        print(f"DB の操作に失敗しました（{exc}）。", file=sys.stderr)  # URL・トークンは含まない
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
