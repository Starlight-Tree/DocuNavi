"""本番 Postgres の定期バックアップ（週 1 回）（Cloud Run Job）。pg_dump -Fc → pg_restore --list で中身を検査 → R2 に保存する。

- 本番 DB は読むだけ（pg_dump は読み取り専用のトランザクション）。書き込みはしない。
- 接続文字列は環境変数 DATABASE_URL（Secret Manager の database-url:1）。引数には載せず、PG* 環境変数で pg_dump に渡す。
- R2 のキーは環境変数 R2_CREDENTIALS_JSON（Secret Manager の r2-backup-rw。access_key_id と secret_access_key の JSON）。
- ログに出すのは、バイト数・表の数・オブジェクトのキー・固定の文言だけ。接続文字列・パスワード・キーは出さない。
- 保持期間は、バケットのライフサイクル規則が持つ。この Job は消さない。通常のダンプ（pg/docunavi-*）は 14 日、
  月次の写し（pg/monthly/）は 400 日（規則の作り方は docs/pg-backup.md）。
- その月の最初のバックアップは、R2 の中で pg/monthly/docunavi-YYYYMM.dump に写す（サーバー側のコピー。DB には触れない）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger("pg-backup")

KEY_PREFIX = "pg/"
DUMP_TIMEOUT_SECONDS = 480
# 復元に要る表が、ダンプに入っているかの確認（無ければ、空のダンプを保存してしまった可能性）
REQUIRED_TABLES = ("documents", "chunks", "user", "account")
EXIT_CONFIG, EXIT_DUMP, EXIT_VERIFY, EXIT_UPLOAD, EXIT_MONTHLY = 2, 3, 4, 5, 6
# 月次の長期保持の置き場。通常のダンプのキー（pg/docunavi-…）と重ならないので、ライフサイクル規則を別にできる
MONTHLY_PREFIX = "pg/monthly/"


class BackupError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def libpq_env(database_url: str) -> dict[str, str]:
    """接続文字列を PG* 環境変数にする（pg_dump の引数に秘密を載せないため）。"""
    u = urlsplit(database_url)
    if u.scheme not in ("postgres", "postgresql") or not u.hostname or not u.path.strip("/"):
        raise BackupError(EXIT_CONFIG, "DATABASE_URL is not a postgres URL")
    env = {
        "PGHOST": u.hostname,
        "PGPORT": str(u.port or 5432),
        "PGDATABASE": unquote(u.path.lstrip("/")),
    }
    if u.username:
        env["PGUSER"] = unquote(u.username)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    sslmode = parse_qs(u.query).get("sslmode", ["require"])[0]
    env["PGSSLMODE"] = sslmode
    return env


def backup_key(now: datetime) -> str:
    return f"{KEY_PREFIX}docunavi-{now.astimezone(UTC):%Y%m%d-%H%M%S}.dump"


def monthly_key(now: datetime) -> str:
    return f"{MONTHLY_PREFIX}docunavi-{now.astimezone(UTC):%Y%m}.dump"


def _is_missing(exc: Exception) -> bool:
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
    return str(code) in ("404", "NoSuchKey", "NotFound")


def copy_monthly(s3: Any, bucket: str, source_key: str, now: datetime, size: int) -> str | None:
    """その月（UTC）の月次の写しが無ければ、保存したダンプを pg/monthly/ に写す。写したキーを返す。もうあれば何もせず None。
    再実行（リトライ）しても同じ月に 2 つは作らない。写したあと、サイズを確かめる。"""
    key = monthly_key(now)
    try:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return None
        except Exception as e:  # noqa: BLE001 - 無いときだけ先へ進む
            if not _is_missing(e):
                raise
        s3.copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": source_key})
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001
        raise BackupError(EXIT_MONTHLY, f"monthly copy failed ({type(e).__name__})") from e
    if int(head.get("ContentLength", -1)) != size:
        raise BackupError(EXIT_MONTHLY, "monthly copy size does not match the stored dump")
    return key


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "<redacted>")
    return text


def run_dump(path: Path, env: dict[str, str], runner: Callable[..., Any] = subprocess.run) -> None:
    secrets = [env.get("PGPASSWORD", ""), env.get("PGUSER", "")]
    try:
        result = runner(
            ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(path)],
            env={"PATH": os.environ.get("PATH", ""), **env},
            capture_output=True,
            text=True,
            timeout=DUMP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise BackupError(EXIT_DUMP, "pg_dump timed out") from e
    if result.returncode != 0:
        # 失敗の理由が分からないと直せないので、先頭の数行だけ、秘密を伏せて出す
        for line in redact(result.stderr or "", secrets).splitlines()[:5]:
            log.error("pg_dump: %s", line[:200])
        raise BackupError(EXIT_DUMP, f"pg_dump failed (exit {result.returncode})")


def verify_dump(path: Path, runner: Callable[..., Any] = subprocess.run) -> int:
    """pg_restore --list で目次を読み、必要な表のデータが入っていることを確かめる。表のデータ数を返す。"""
    if not path.exists() or path.stat().st_size == 0:
        raise BackupError(EXIT_VERIFY, "dump file is empty")
    result = runner(["pg_restore", "--list", str(path)], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise BackupError(EXIT_VERIFY, f"pg_restore --list failed (exit {result.returncode})")
    toc = result.stdout or ""
    data = [name.strip('"') for name in re.findall(r"TABLE DATA public (\S+) ", toc)]
    missing = [t for t in REQUIRED_TABLES if t not in data]
    if missing:
        raise BackupError(EXIT_VERIFY, f"required tables missing from the dump: {', '.join(missing)}")
    return len(data)


def load_credentials(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise BackupError(EXIT_CONFIG, "R2_CREDENTIALS_JSON is not JSON") from e
    if not isinstance(data, dict) or not {"access_key_id", "secret_access_key"} <= data.keys():
        raise BackupError(EXIT_CONFIG, "R2_CREDENTIALS_JSON has an unexpected shape")
    return {k: str(data[k]) for k in ("access_key_id", "secret_access_key")}


def s3_client(account_id: str, creds: dict[str, str]) -> Any:
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        config=BotoConfig(retries={"max_attempts": 3}, connect_timeout=10, read_timeout=60),
    )


def upload(s3: Any, bucket: str, key: str, path: Path) -> int:
    size = path.stat().st_size
    try:
        s3.upload_file(str(path), bucket, key)
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as e:
        raise BackupError(EXIT_UPLOAD, f"upload failed ({type(e).__name__})") from e
    if int(head.get("ContentLength", -1)) != size:
        raise BackupError(EXIT_UPLOAD, "uploaded size does not match the local file")
    return size


def main(environ: dict[str, str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    env = dict(os.environ if environ is None else environ)
    try:
        database_url = env.get("DATABASE_URL", "")
        account, bucket = env.get("CLOUDFLARE_ACCOUNT_ID", ""), env.get("R2_BUCKET", "")
        if not (database_url and account and bucket and env.get("R2_CREDENTIALS_JSON")):
            raise BackupError(
                EXIT_CONFIG, "DATABASE_URL, CLOUDFLARE_ACCOUNT_ID, R2_BUCKET and R2_CREDENTIALS_JSON are required"
            )
        pg_env = libpq_env(database_url)
        creds = load_credentials(env["R2_CREDENTIALS_JSON"])
        now = datetime.now(UTC)
        key = backup_key(now)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "backup.dump"
            run_dump(path, pg_env)
            tables = verify_dump(path)
            s3 = s3_client(account, creds)
            size = upload(s3, bucket, key, path)
        log.info("backup ok: bucket=%s key=%s bytes=%d tables_with_data=%d", bucket, key, size, tables)
        copied = copy_monthly(s3, bucket, key, now, size)
        log.info("monthly copy: %s", f"key={copied}" if copied else "already exists this month")
        return 0
    except BackupError as e:
        log.error("backup failed: %s", e)
        return e.code
    except Exception as e:
        log.error("backup failed (%s)", type(e).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
