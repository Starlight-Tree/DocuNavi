"""元 PDF の置き場（Cloudflare R2。S3 互換）の読み書き。

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
        """バケット直下の「資料群」（`cafe/` のような最上位のプレフィックス）を名前順に。"""
        names = [p["Prefix"].rstrip("/") for page in self._pages(Delimiter="/") for p in page.get("CommonPrefixes", [])]
        return sorted(n for n in names if n)

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

    def put(self, key: str, data: bytes) -> None:
        self._call("put_object", Key=key, Body=data, ContentType="application/pdf")


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
    """`root/<資料群>/<ファイル>.pdf` を、R2 の `<資料群>/<ファイル>.pdf` に置く計画。(キー, パス, 状態) の一覧。

    状態は new（無い）・changed（中身が違う）・same（同じ。上書きしない）。比較は MD5（R2 の ETag）。
    """
    plan: list[tuple[str, Path, str]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"):
        relative = path.relative_to(root).as_posix()
        if "/" not in relative:  # 資料群のディレクトリの直下に置く決まり
            continue
        remote = store.etag(relative)
        state = (
            "new" if remote is None else "same" if remote == hashlib.md5(path.read_bytes()).hexdigest() else "changed"
        )
        plan.append((relative, path, state))
    return plan


def verify_originals(documents: Sequence[Mapping[str, Any]], find: Any) -> tuple[list[str], int]:
    """DB の資料ごとに、R2 に元の PDF が（名前とハッシュで）見つかるかを確かめる。(表示する行, 問題の件数) を返す。

    `find` は `pdf_originals.find_original_in_r2` と同じ形（source, content_hash → 結果）。本番の `/documents/{id}/file` と同じ規則。
    """
    lines: list[str] = []
    problems = 0
    for doc in documents:
        found = find(doc["source"], doc["content_hash"])
        state = "ok" if found.ok else str(found.reason)
        problems += 0 if found.ok else 1
        lines.append(f"{state:12} id={doc['id']} {doc['source']}")
    return lines, problems


def _ready_documents() -> list[dict[str, Any]]:
    """DB の `status = 'ready'` の資料（id・source・content_hash）。接続は DATABASE_URL（アプリと同じ）。"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=10) as conn:
        rows = conn.execute(
            "SELECT id, source, content_hash FROM documents WHERE status = 'ready' ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def run_verify() -> int:
    """終了コード: 0 すべて一致 / 1 一致しないものがある・資料が 0 件・DB か R2 を読めない / 2 設定の不足。"""
    from app import pdf_originals

    if not os.environ.get("DATABASE_URL", "").strip():
        print("DATABASE_URL が設定されていません。", file=sys.stderr)
        return 2
    try:
        default_store()
    except R2ConfigError as exc:
        print(f"R2 の設定が足りません（{exc}）。", file=sys.stderr)
        return 2
    try:
        documents = _ready_documents()
        lines, problems = verify_originals(documents, pdf_originals.find_original_in_r2)
    except Exception as exc:  # noqa: BLE001 - 例外の文字列に接続先・SQL が入りうる。クラス名だけを出す
        print(f"確認に失敗しました（{type(exc).__name__}）。", file=sys.stderr)
        return 1
    print("\n".join(lines))
    if not documents:
        print("確認する資料がありません（status = ready が 0 件）。一致したとは言えません。", file=sys.stderr)
        return 1
    print(f"資料 {len(documents)} 件、一致しない {problems} 件")
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.r2_store",
        description=(
            "upload: storage/pdf/ の PDF を R2 に置く（既定は計画の表示だけ。--apply で置く。読み書きのキーが要る）。"
            "verify: DB の資料ごとに、R2 に元の PDF が一致して見つかるかを確かめる（読み取りのキーと DATABASE_URL が要る）"
        ),
    )
    parser.add_argument("action", choices=["upload", "verify"])
    parser.add_argument(
        "--input", type=Path, default=Path("storage/pdf"), help="資料群のディレクトリを含む置き場（既定 storage/pdf）"
    )
    parser.add_argument("--apply", action="store_true", help="実際に置く（既定は計画の表示だけ）")
    args = parser.parse_args(argv)
    if args.action == "verify":
        return run_verify()
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
                store.put(key, path.read_bytes())
    except Exception as exc:  # noqa: BLE001 - 例外の文字列に接続先が入りうる。クラス名だけを出す
        print(f"R2 の操作に失敗しました（{type(exc).__name__}）。", file=sys.stderr)
        return 1
    changes = sum(1 for _, _, state in plan if state != "same")
    suffix = "" if args.apply else "（確認だけ。何も置いていません。--apply で置きます）"
    print(f"PDF {len(plan)} 件、置く対象 {changes} 件{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
