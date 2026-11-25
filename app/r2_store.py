"""PDF の置き場（Cloudflare R2。S3 互換）の読み書き。

置き場の形は `storage/pdf/` と同じ。オブジェクトのキーは `<資料群>/<ファイル名>`（例: `cafe/menu.pdf`）。
バケットは private のまま使う（公開 URL・r2.dev は使わない）。手順と設計は docs/r2-key-issuer.md。

設定は環境変数で渡す（値をコード・ログ・応答に出さない）:

- `R2_ACCOUNT_ID`: Cloudflare のアカウント ID（エンドポイント `https://<ID>.r2.cloudflarestorage.com` に使う）
- `R2_BUCKET`: バケット名（`docunavi-documents`）
- `R2_CREDENTIALS_JSON`: `{"access_key_id": …, "secret_access_key": …}`。Secret Manager の `r2-app-read`（読み取り）または
  `r2-ingest-rw`（読み書き）の値を、Cloud Run の `--set-secrets` でそのまま渡す
- `R2_CREDENTIALS_FILE`: 同じ JSON が入ったファイルのパス（Cloud Run が Secret をボリュームとして渡すとき。例
  `--set-secrets=/secrets/r2/credentials.json=r2-app-read:latest`）。`R2_CREDENTIALS_JSON` が空のときだけ使う

書き込み（`python -m app.r2_store upload`）は、読み書きのキーが要る。アプリ本体（`app/main.py`）は読み取りしかしない。

鍵の入れ替え（docs/r2-key-issuer.md）: R2 が認証エラー（401/403）を返したら、`R2_CREDENTIALS_FILE` を読み直し、**鍵の ID が変わっていれば**
新しい鍵で 1 回だけやり直す。ID が同じなら（本当に権限が無い）やり直さずに元のエラーを返す。再配備は要らない。
`R2_CREDENTIALS_JSON`（環境変数）は起動時の値のまま変わらないので、読み直しても同じ ID になり、やり直しは起きない。
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}
_AUTH_CODES = {
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "AccessDenied",
    "InvalidToken",
    "ExpiredToken",
    "401",
    "403",
}
_CRED_KEYS = ("access_key_id", "secret_access_key")
# 資料群ではないプレフィックス。ヒーロー画像（DB の document_images.r2_key）が同じバケットの hero/ に入る
NON_COLLECTION_PREFIXES = frozenset({"hero"})


class R2ConfigError(Exception):
    """設定の不足・形の誤り。メッセージには変数の名前だけを入れる（値は入れない）。"""


@dataclass(frozen=True)
class R2Config:
    account_id: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def config_from_env(env: Mapping[str, str] | None = None) -> R2Config:
    env = os.environ if env is None else env
    values = {name: env.get(name, "").strip() for name in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_CREDENTIALS_JSON")}
    path = env.get("R2_CREDENTIALS_FILE", "").strip()
    if not values["R2_CREDENTIALS_JSON"] and path:  # ボリュームで渡された Secret。呼ぶたびに読む（入れ替えに追従する）
        try:
            values["R2_CREDENTIALS_JSON"] = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            raise R2ConfigError("R2_CREDENTIALS_FILE cannot be read") from None
    key_id, secret = env.get("R2_ACCESS_KEY_ID", "").strip(), env.get("R2_SECRET_ACCESS_KEY", "").strip()
    # Infisical など、鍵を 2 つの変数で渡す経路（JSON・ファイルが先）
    if not values["R2_CREDENTIALS_JSON"] and key_id and secret:
        values["R2_CREDENTIALS_JSON"] = json.dumps({"access_key_id": key_id, "secret_access_key": secret})
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise R2ConfigError(f"missing: {', '.join(missing)}")
    try:
        creds = json.loads(values["R2_CREDENTIALS_JSON"])
    except ValueError:
        raise R2ConfigError("R2_CREDENTIALS_JSON is not valid JSON") from None
    if not isinstance(creds, dict) or not all(isinstance(creds.get(k), str) and creds[k] for k in _CRED_KEYS):
        raise R2ConfigError("R2_CREDENTIALS_JSON needs access_key_id and secret_access_key")
    return R2Config(values["R2_ACCOUNT_ID"], values["R2_BUCKET"], creds["access_key_id"], creds["secret_access_key"])


def make_client(config: R2Config) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        region_name="auto",  # R2 では使わないが、SDK が要る
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=15,
        ),
    )


def _is_auth_error(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error.get("Code") in _AUTH_CODES or status in (401, 403)


class R2Store:
    """バケット 1 つへの操作。`client` は boto3 の S3 クライアント（テストでは偽物を渡す）。

    `reload` は、鍵が入れ替わったかもしれないときに呼ぶ（新しいクライアントと鍵の ID を返す）。認証エラーのとき 1 回だけ呼び、
    鍵の ID が変わっていれば、新しいクライアントで操作をやり直す。
    """

    def __init__(
        self,
        client: Any,
        bucket: str,
        reload: Callable[[], tuple[Any, str]] | None = None,
        key_id: str | None = None,
    ) -> None:
        self._state = (client, key_id)  # クライアントと鍵の ID は、まとめて入れ替える（スレッドから同時に呼ばれる）
        self._bucket = bucket
        self._reload = reload

    def _call(self, operation: str, **params: Any) -> Any:
        client, key_id = self._state
        try:
            return getattr(client, operation)(Bucket=self._bucket, **params)
        except ClientError as exc:
            if not _is_auth_error(exc) or self._reload is None:
                raise
            try:
                fresh_client, fresh_id = self._reload()
            except Exception:  # noqa: BLE001 - 読み直せないときは、元のエラーを返す（読み直しの失敗の中身は出さない）
                raise exc from None
            if fresh_id == key_id:  # 鍵は同じ。権限が無いだけなので、やり直さない
                raise
            self._state = (fresh_client, fresh_id)
            logger.info("R2 の鍵が入れ替わっていたので、読み直しました")
            return getattr(fresh_client, operation)(Bucket=self._bucket, **params)

    def _pages(self, **params: Any) -> Any:
        token = None
        while True:
            kwargs = dict(params)
            if token:
                kwargs["ContinuationToken"] = token
            page = self._call("list_objects_v2", **kwargs)
            yield page
            if not page.get("IsTruncated"):
                return
            token = page["NextContinuationToken"]

    def collections(self) -> list[str]:
        """バケット直下の「資料群」（`cafe/` のような最上位のプレフィックス）を名前順に。画像の置き場（`hero/`）は含めない。"""
        names = [p["Prefix"].rstrip("/") for page in self._pages(Delimiter="/") for p in page.get("CommonPrefixes", [])]
        return sorted(n for n in names if n and n not in NON_COLLECTION_PREFIXES)

    def pdf_keys(self, prefix: str) -> list[str]:
        """プレフィックス配下の PDF のキー（拡張子の大小は問わない）を名前順に。"""
        keys = [o["Key"] for page in self._pages(Prefix=prefix) for o in page.get("Contents", [])]
        return sorted(k for k in keys if k.lower().endswith(".pdf"))

    def get(self, key: str, max_bytes: int) -> bytes | None:
        """オブジェクトの中身。無い・大きすぎるなら None。それ以外の失敗（権限・通信）は例外のまま。"""
        try:
            response = self._call("get_object", Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                return None
            raise
        length = response.get("ContentLength")
        if isinstance(length, int) and length > max_bytes:
            return None
        data = response["Body"].read(max_bytes + 1)
        return data if len(data) <= max_bytes else None

    def etag(self, key: str) -> str | None:
        """オブジェクトの ETag（単一パートの PUT なら MD5 の 16 進）。無ければ None。"""
        try:
            response = self._call("head_object", Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                return None
            raise
        return str(response.get("ETag", "")).strip('"') or None

    def put(self, key: str, data: bytes, content_type: str = "application/pdf") -> None:
        self._call("put_object", Key=key, Body=data, ContentType=content_type)


def _fresh_client() -> tuple[Any, str]:
    """環境（`R2_CREDENTIALS_FILE` なら、いまのファイルの中身）から、新しいクライアントと鍵の ID を作る。"""
    config = config_from_env()
    return make_client(config), config.access_key_id


@lru_cache(maxsize=1)
def default_store() -> R2Store:
    """環境変数の設定で作る、プロセスで 1 つの置き場。設定が足りなければ R2ConfigError。"""
    config = config_from_env()
    return R2Store(make_client(config), config.bucket, reload=_fresh_client, key_id=config.access_key_id)


# --- 取り込み・アップロード用 ---------------------------------------------------------------------------------


_REVISIONS = "revisions.csv"  # app/ingestion/revisions.py の REVISIONS_FILE と同じ（取り込みの層を import しない）
_CSV_TYPE = "text/csv; charset=utf-8"


def download_collection(
    store: R2Store, collection: str, target: Path, max_bytes: int, only: str | None = None
) -> list[Path]:
    """`<collection>/` 配下の PDF を `target/<collection>/…` に保存して、そのパスを返す（取り込みに渡すため）。"""
    out: list[Path] = []
    for key in store.pdf_keys(f"{collection}/"):
        relative = key[len(collection) + 1 :]
        if only is not None and relative != only:
            continue
        parts = Path(relative).parts
        if not parts or ".." in parts or Path(relative).is_absolute():  # キーは置き場の外へ出さない
            logger.warning("安全でないキーを飛ばしました")
            continue
        data = store.get(key, max_bytes)
        if data is None:
            continue
        path = target / collection / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        out.append(path)
    return out


def upload_plan(store: R2Store, root: Path) -> list[tuple[str, Path, str]]:
    """`root/<資料群>/<ファイル>.pdf`（と、資料群の直下の版の一覧 `revisions.csv`）を、R2 の同じキーに置く計画。(キー, パス, 状態) の一覧。

    状態は new（無い）・changed（中身が違う）・same（同じ。上書きしない）。比較は MD5（R2 の ETag）。
    """
    plan: list[tuple[str, Path, str]] = []
    # 版の一覧は、資料群ごとに PDF より先に置く（新しい版の PDF が、古い版のラベルのまま取り込まれる時間を作らない）
    candidates = [p for p in root.rglob("*") if p.is_file() and (p.suffix.lower() == ".pdf" or p.name == _REVISIONS)]
    for path in sorted(candidates, key=lambda p: (p.parent.as_posix(), p.name != _REVISIONS, p.name)):
        relative = path.relative_to(root).as_posix()
        if "/" not in relative:  # 資料群のディレクトリの直下に置く決まり
            continue
        if path.name == _REVISIONS and relative.count("/") != 1:  # 版の一覧は、資料群の直下の 1 つだけ
            continue
        remote = store.etag(relative)
        state = (
            "new" if remote is None else "same" if remote == hashlib.md5(path.read_bytes()).hexdigest() else "changed"
        )
        plan.append((relative, path, state))
    return plan


def check_originals(documents: Sequence[Mapping[str, Any]], find: Any) -> list[tuple[Mapping[str, Any], Any]]:
    """DB の資料ごとに、R2 に PDF が（名前とハッシュで）見つかるかを確かめる。(資料, 結果) の並び。R2 への問い合わせは、資料 1 件につきここだけ。"""
    return [(doc, find(doc["source"], doc["content_hash"])) for doc in documents]


def summarize_originals(checked: Sequence[tuple[Mapping[str, Any], Any]]) -> tuple[list[str], int]:
    """`check_originals` の結果を、(表示する行, 問題の件数) にする。"""
    lines: list[str] = []
    problems = 0
    for doc, found in checked:
        state = "ok" if found.ok else str(found.reason)
        problems += 0 if found.ok else 1
        lines.append(f"{state:12} id={doc['id']} {doc['source']}")
    return lines, problems


def verify_originals(documents: Sequence[Mapping[str, Any]], find: Any) -> tuple[list[str], int]:
    """DB の資料ごとに、R2 に PDF が（名前とハッシュで）見つかるかを確かめる。(表示する行, 問題の件数) を返す。

    `find` は `pdf_originals.find_original_in_r2` と同じ形（source, content_hash → 結果）。本番の `/documents/{id}/file` と同じ規則。
    """
    return summarize_originals(check_originals(documents, find))


def record_originals(db: Any, checked: Sequence[tuple[Mapping[str, Any], Any]]) -> tuple[int, int]:
    """確かめた結果を、DB の `documents.original_verified_at` に書く。(記録した件数, 消した件数) を返す。

    一致した資料は、いまの時刻を入れる（`content_hash` が、確かめたときと同じ行だけ。確かめている間に取り込みで変わった行は触らない）。
    一致しなかった資料（見つからない・食い違い・R2 を読めない）は、NULL に戻す（`/chunks` は、NULL ならその場で確かめる）。
    `/chunks` は、記録があれば R2 に聞かずに `original_pdf: true` を返す（読む側の確認を、書く側へ移す。ADR 0017）。
    """
    recorded = cleared = 0
    for doc, found in checked:
        if found.ok:
            rows = db.execute(
                "UPDATE documents SET original_verified_at = unixepoch() * 1000 WHERE id = ? AND content_hash = ? RETURNING id",
                (doc["id"], doc["content_hash"]),
            )
            recorded += len(rows)
        else:
            rows = db.execute(
                "UPDATE documents SET original_verified_at = NULL WHERE id = ? AND original_verified_at IS NOT NULL RETURNING id",
                (doc["id"],),
            )
            cleared += len(rows)
    return recorded, cleared


def _ready_documents() -> list[dict[str, Any]]:
    """DB の `status = 'ready'` の資料（id・source・content_hash）。接続は `DB_BACKEND` に従う（アプリと同じ。本番は pg の `DATABASE_URL`。d1 は退役済みで、テストの偽 DB 用）。"""
    from app.d1 import SyncD1
    from app.pg import sync_client

    with sync_client(SyncD1) as db:
        return db.execute("SELECT id, source, content_hash FROM documents WHERE status = 'ready' ORDER BY id")


def run_verify(record: bool = False) -> int:
    """終了コード: 0 すべて一致 / 1 一致しないものがある・資料が 0 件・DB か R2 を読めない / 2 設定の不足。

    `record=True`（`--record`）のときは、結果を DB の `documents.original_verified_at` にも書く（DB への書き込み。既定は書かない）。
    """
    from app import pdf_originals
    from app.history import database_url

    if not database_url():
        print("D1_API_URL（DB_BACKEND=pg のときは DATABASE_URL）が設定されていません。", file=sys.stderr)
        return 2
    try:
        default_store()
    except R2ConfigError as exc:
        print(f"R2 の設定が足りません（{exc}）。", file=sys.stderr)
        return 2
    try:
        documents = _ready_documents()
        checked = check_originals(documents, pdf_originals.find_original_in_r2)
        lines, problems = summarize_originals(checked)
    except Exception as exc:  # noqa: BLE001 - 例外の文字列に接続先・SQL が入りうる。クラス名だけを出す
        print(f"確認に失敗しました（{type(exc).__name__}）。", file=sys.stderr)
        return 1
    print("\n".join(lines))
    if not documents:
        print("確認する資料がありません（status = ready が 0 件）。一致したとは言えません。", file=sys.stderr)
        return 1
    print(f"資料 {len(documents)} 件、一致しない {problems} 件")
    if record:
        from app.d1 import SyncD1
        from app.pg import sync_client

        try:
            with sync_client(SyncD1) as db:
                recorded, cleared = record_originals(db, checked)
        except Exception as exc:  # noqa: BLE001 - 上と同じ
            print(f"DB への記録に失敗しました（{type(exc).__name__}）。", file=sys.stderr)
            return 1
        print(f"original_verified_at を記録 {recorded} 件、消去 {cleared} 件")
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.r2_store",
        description=(
            "upload: storage/pdf/ の PDF を R2 に置く（既定は計画の表示だけ。--apply で置く。読み書きのキーが要る）。"
            "verify: DB の資料ごとに、R2 に PDF が一致して見つかるかを確かめる（読み取りのキーと DB の接続先が要る。--record で結果を DB にも記録する）"
        ),
    )
    parser.add_argument("action", choices=["upload", "verify"])
    parser.add_argument(
        "--input", type=Path, default=Path("storage/pdf"), help="資料群のディレクトリを含む置き場（既定 storage/pdf）"
    )
    parser.add_argument("--apply", action="store_true", help="実際に置く（既定は計画の表示だけ）")
    parser.add_argument(
        "--record",
        action="store_true",
        help="verify の結果を、DB の documents.original_verified_at にも書く（DB への書き込み。既定は書かない）",
    )
    args = parser.parse_args(argv)
    if args.record and args.action != "verify":
        parser.error("--record は verify のときだけ使えます")
    if args.action == "verify":
        return run_verify(record=args.record)
    try:
        store = default_store()
    except R2ConfigError as exc:
        print(f"R2 の設定が足りません（{exc}）。", file=sys.stderr)
        return 2
    try:
        plan = upload_plan(store, args.input)
        for key, path, state in plan:
            print(f"{state:8} {key}")
            if args.apply and state != "same":
                store.put(key, path.read_bytes(), _CSV_TYPE if path.name == _REVISIONS else "application/pdf")
    except Exception as exc:  # noqa: BLE001 - 例外の文字列に接続先が入りうる。クラス名だけを出す
        print(f"R2 の操作に失敗しました（{type(exc).__name__}）。", file=sys.stderr)
        return 1
    changes = sum(1 for _, _, state in plan if state != "same")
    suffix = "" if args.apply else "（確認だけ。何も置いていません。--apply で置きます）"
    print(f"ファイル {len(plan)} 件（PDF と版の一覧）、置く対象 {changes} 件{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
