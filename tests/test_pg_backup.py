"""本番 Postgres の日次バックアップ Job（ops/pg_backup/main.py）。pg_dump・R2 には接続しない（すべて模擬）。"""

import json
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.pg_backup import main as backup

PASSWORD = "PASSWORD-DO-NOT-LOG"
URL = f"postgres://dbuser:{PASSWORD}@db.example.test:5432/postgres?sslmode=require"
SECRET_KEY = "R2-SECRET-DO-NOT-LOG"
CREDS = json.dumps({"access_key_id": "kid", "secret_access_key": SECRET_KEY, "issued_at": "x"})
TOC = "\n".join(
    f"{i}; 0 {i} TABLE DATA public {t} -"
    for i, t in enumerate(("documents", "chunks", '"user"', "account", "roles"), 1)
)


def test_libpq_env_keeps_the_password_out_of_the_command_line() -> None:
    env = backup.libpq_env(URL)
    assert env == {
        "PGHOST": "db.example.test",
        "PGPORT": "5432",
        "PGDATABASE": "postgres",
        "PGUSER": "dbuser",
        "PGPASSWORD": PASSWORD,
        "PGSSLMODE": "require",
    }


def test_libpq_env_decodes_percent_escapes_and_defaults_the_port() -> None:
    env = backup.libpq_env("postgresql://u%40x:p%2Fw@h/db")
    assert (env["PGUSER"], env["PGPASSWORD"], env["PGPORT"]) == ("u@x", "p/w", "5432")


@pytest.mark.parametrize("bad", ["", "mysql://u:p@h/db", "postgres://u:p@/db", "postgres://u:p@h/", "not a url"])
def test_libpq_env_rejects_other_urls(bad: str) -> None:
    with pytest.raises(backup.BackupError) as e:
        backup.libpq_env(bad)
    assert e.value.code == backup.EXIT_CONFIG


def test_backup_key_is_utc_and_under_the_prefix() -> None:
    now = datetime(2026, 11, 9, 3, 30, 5, tzinfo=UTC)
    assert backup.backup_key(now) == "pg/docunavi-20261109-033005.dump"


def test_run_dump_passes_the_password_by_environment_only(tmp_path: Path) -> None:
    seen: dict = {}

    def runner(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return SimpleNamespace(returncode=0, stderr="")

    backup.run_dump(tmp_path / "x.dump", backup.libpq_env(URL), runner)
    assert PASSWORD not in " ".join(seen["cmd"]) and "dbuser" not in " ".join(seen["cmd"])
    assert seen["env"]["PGPASSWORD"] == PASSWORD
    assert "--format=custom" in seen["cmd"] and "--no-owner" in seen["cmd"]


def test_run_dump_failure_redacts_the_password_in_the_log(tmp_path: Path, caplog) -> None:
    def runner(cmd, **kw):
        return SimpleNamespace(returncode=1, stderr=f"pg_dump: error: connection failed: {PASSWORD} for dbuser\n")

    with caplog.at_level(logging.INFO), pytest.raises(backup.BackupError) as e:
        backup.run_dump(tmp_path / "x.dump", backup.libpq_env(URL), runner)
    assert e.value.code == backup.EXIT_DUMP
    assert PASSWORD not in caplog.text and "dbuser" not in caplog.text and "<redacted>" in caplog.text


def test_run_dump_timeout_is_a_dump_failure(tmp_path: Path) -> None:
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with pytest.raises(backup.BackupError) as e:
        backup.run_dump(tmp_path / "x.dump", {}, runner)
    assert e.value.code == backup.EXIT_DUMP


def test_verify_dump_counts_tables_and_accepts_a_quoted_user_table(tmp_path: Path) -> None:
    f = tmp_path / "x.dump"
    f.write_bytes(b"data")
    assert backup.verify_dump(f, lambda *a, **k: SimpleNamespace(returncode=0, stdout=TOC)) == 5


def test_verify_dump_rejects_empty_unreadable_and_incomplete_dumps(tmp_path: Path) -> None:
    f = tmp_path / "x.dump"
    with pytest.raises(backup.BackupError):  # 無い
        backup.verify_dump(f, lambda *a, **k: SimpleNamespace(returncode=0, stdout=TOC))
    f.write_bytes(b"")
    with pytest.raises(backup.BackupError):  # 空
        backup.verify_dump(f, lambda *a, **k: SimpleNamespace(returncode=0, stdout=TOC))
    f.write_bytes(b"data")
    with pytest.raises(backup.BackupError):  # 読めない
        backup.verify_dump(f, lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(backup.BackupError) as e:  # 必要な表が無い
        backup.verify_dump(f, lambda *a, **k: SimpleNamespace(returncode=0, stdout="1; 0 1 TABLE DATA public roles -"))
    assert e.value.code == backup.EXIT_VERIFY and "chunks" in str(e.value)


def test_load_credentials_needs_both_fields() -> None:
    assert backup.load_credentials(CREDS) == {"access_key_id": "kid", "secret_access_key": SECRET_KEY}
    for bad in ("not json", "[]", json.dumps({"access_key_id": "k"})):
        with pytest.raises(backup.BackupError) as e:
            backup.load_credentials(bad)
        assert e.value.code == backup.EXIT_CONFIG


class FakeS3:
    def __init__(self, length: int | None = None, fail: bool = False) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.length, self.fail = length, fail

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.uploaded.append((bucket, key))
        self._size = Path(filename).stat().st_size

    def head_object(self, Bucket: str, Key: str) -> dict:
        return {"ContentLength": self._size if self.length is None else self.length}


def test_upload_checks_the_stored_size(tmp_path: Path) -> None:
    f = tmp_path / "x.dump"
    f.write_bytes(b"12345")
    assert backup.upload(FakeS3(), "b", "pg/k.dump", f) == 5
    with pytest.raises(backup.BackupError) as e:
        backup.upload(FakeS3(length=3), "b", "pg/k.dump", f)
    assert e.value.code == backup.EXIT_UPLOAD
    with pytest.raises(backup.BackupError) as e:
        backup.upload(FakeS3(fail=True), "b", "pg/k.dump", f)
    assert e.value.code == backup.EXIT_UPLOAD


ENV = {
    "DATABASE_URL": URL,
    "CLOUDFLARE_ACCOUNT_ID": "acct",
    "R2_BUCKET": "docunavi-backups",
    "R2_CREDENTIALS_JSON": CREDS,
}


def _wire(monkeypatch: pytest.MonkeyPatch, s3: FakeS3) -> None:
    def fake_dump(path: Path, env: dict, runner=None) -> None:
        path.write_bytes(b"PGDMP-data")

    monkeypatch.setattr(backup, "run_dump", fake_dump)
    monkeypatch.setattr(backup, "verify_dump", lambda path, runner=None: 5)
    monkeypatch.setattr(backup, "s3_client", lambda account, creds: s3)


def test_main_dumps_verifies_uploads_and_logs_no_secrets(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    s3 = FakeS3()
    _wire(monkeypatch, s3)
    with caplog.at_level(logging.INFO):
        assert backup.main(ENV) == 0
    [(bucket, key)] = s3.uploaded
    assert bucket == "docunavi-backups" and key.startswith("pg/docunavi-") and key.endswith(".dump")
    assert "backup ok" in caplog.text and "bytes=10" in caplog.text
    for secret in (PASSWORD, SECRET_KEY, "dbuser", "db.example.test"):
        assert secret not in caplog.text


@pytest.mark.parametrize("missing", list(ENV))
def test_main_requires_every_setting(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, FakeS3())
    assert backup.main({k: v for k, v in ENV.items() if k != missing}) == backup.EXIT_CONFIG


def test_main_does_not_upload_when_the_dump_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = FakeS3()
    _wire(monkeypatch, s3)

    def failing(path, env, runner=None):
        raise backup.BackupError(backup.EXIT_DUMP, "pg_dump failed (exit 1)")

    monkeypatch.setattr(backup, "run_dump", failing)
    assert backup.main(ENV) == backup.EXIT_DUMP and s3.uploaded == []


def test_main_does_not_upload_when_verification_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = FakeS3()
    _wire(monkeypatch, s3)

    def failing(path, runner=None):
        raise backup.BackupError(backup.EXIT_VERIFY, "required tables missing")

    monkeypatch.setattr(backup, "verify_dump", failing)
    assert backup.main(ENV) == backup.EXIT_VERIFY and s3.uploaded == []


def test_main_unexpected_error_logs_only_the_type(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    _wire(monkeypatch, FakeS3())

    def boom(account, creds):
        raise RuntimeError(f"leak {PASSWORD}")

    monkeypatch.setattr(backup, "s3_client", boom)
    with caplog.at_level(logging.INFO):
        assert backup.main(ENV) == 1
    assert PASSWORD not in caplog.text and "RuntimeError" in caplog.text
