"""db/migrate.py の接続先・TLS・権限の検査を確かめる。DB には接続しない（接続後の検査は偽の接続で）。

実際の Cloud SQL・Prisma への接続、`CREATE ROLE` で作ったユーザーが本当に cloudsqlsuperuser でないことは、未確認（本番の作成の後に確かめる）。
"""

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("db_migrate", ROOT / "db" / "migrate.py")
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)

HOST, DB, USER = "10.30.0.3", "docunavi", "docunavi_migrator"
PASSWORD = "s3cr3t-PASSWORD-xyz"


@pytest.fixture
def ca(tmp_path: Path) -> Path:
    p = tmp_path / "server-ca.pem"
    p.write_text("dummy", encoding="utf-8")
    return p


@pytest.fixture
def env() -> dict[str, str]:
    return {"MIGRATE_ALLOWED_HOST": HOST, "MIGRATE_ALLOWED_DB": DB, "MIGRATE_ALLOWED_USER": USER}


def url(ca: Path, *, host=HOST, port=5432, db=DB, user=USER, query: str | None = None) -> str:
    q = query if query is not None else f"sslmode=verify-ca&sslrootcert={ca}"
    return f"postgresql://{user}:{PASSWORD}@{host}:{port}/{db}?{q}"


def rejected(capsys, u: str, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as e:
        migrate.validate_cloudsql_url(u, env)
    message = str(e.value)
    assert PASSWORD not in message
    return message


def test_accepts_the_allowlisted_url(ca, env) -> None:
    migrate.validate_cloudsql_url(url(ca), env)
    migrate.validate_cloudsql_url(url(ca, query=f"sslmode=verify-full&sslrootcert={ca}"), env)


@pytest.mark.parametrize(
    ("kwargs", "word"),
    [
        ({"host": "10.30.0.4"}, "host"),
        ({"host": "db.prisma.io"}, "prisma.io"),
        ({"host": f"{HOST},10.30.0.4"}, "host"),
        ({"port": 6543}, "port"),
        ({"db": "postgres"}, "database"),
        ({"user": "postgres"}, "user"),
    ],
)
def test_rejects_other_targets(ca, env, capsys, kwargs, word) -> None:
    assert word in rejected(capsys, url(ca, **kwargs), env)


@pytest.mark.parametrize(
    "query",
    [
        "sslmode=require&sslrootcert={ca}",
        "sslmode=prefer&sslrootcert={ca}",
        "sslmode=disable",
        "sslrootcert={ca}",
        "sslmode=verify-ca",
        "sslmode=verify-ca&sslrootcert=/nonexistent/ca.pem",
        "sslmode=verify-ca&sslrootcert={ca}&hostaddr=1.2.3.4",
        "sslmode=verify-ca&sslrootcert={ca}&host=evil.example",
        "sslmode=verify-ca&sslrootcert={ca}&options=-csearch_path%3Devil",
    ],
)
def test_rejects_weak_tls_and_libpq_overrides(ca, env, capsys, query) -> None:
    rejected(capsys, url(ca, query=query.format(ca=ca)), env)


@pytest.mark.parametrize("missing", ["MIGRATE_ALLOWED_HOST", "MIGRATE_ALLOWED_DB", "MIGRATE_ALLOWED_USER"])
def test_requires_every_allowlist_value(ca, env, capsys, missing) -> None:
    env.pop(missing)
    assert missing in rejected(capsys, url(ca), env)


def test_rejects_pg_environment_overrides(ca, env, capsys) -> None:
    assert "PGHOSTADDR" in rejected(capsys, url(ca), {**env, "PGHOSTADDR": "1.2.3.4"})
    assert "PGSSLMODE" in rejected(capsys, url(ca), {**env, "PGSSLMODE": "disable"})


def test_requires_a_password_in_the_url(ca, env, capsys) -> None:
    rejected(capsys, f"postgresql://{USER}@{HOST}:5432/{DB}?sslmode=verify-ca&sslrootcert={ca}", env)


def test_rejects_a_prisma_allowlist(ca, env, capsys) -> None:
    rejected(capsys, url(ca, host="db.prisma.io"), {**env, "MIGRATE_ALLOWED_HOST": "db.prisma.io"})


# ---- 接続後の検査（偽の接続） ----


class FakeConn:
    def __init__(self, *, host=HOST, port=5432, dbname=DB, user=USER, ssl_in_use=True, row=None) -> None:
        self.info = SimpleNamespace(host=host, port=port, dbname=dbname, user=user, ssl_in_use=ssl_in_use)
        self.row = row if row is not None else (DB, USER, USER, True, False, False, True)
        self.queries: list[str] = []

    def execute(self, query: str, params=None):
        self.queries.append(query)
        return SimpleNamespace(fetchone=lambda: self.row)


def session_rejected(conn: FakeConn, env: dict[str, str]) -> str:
    with pytest.raises(SystemExit) as e:
        migrate.check_cloudsql_session(conn, env)
    return str(e.value)


def test_session_checks_pass_for_a_plain_migration_user(env) -> None:
    conn = FakeConn()
    migrate.check_cloudsql_session(conn, env)
    assert len(conn.queries) == 1  # 1 回の読み取りだけ


@pytest.mark.parametrize(
    ("conn", "word"),
    [
        (FakeConn(host="10.30.0.9"), "differ"),
        (FakeConn(dbname="postgres"), "differ"),
        (FakeConn(user="postgres"), "differ"),
        (FakeConn(ssl_in_use=False), "TLS"),
        (FakeConn(row=("postgres", USER, USER, True, False, False, True)), "current_database"),
        (FakeConn(row=(DB, "postgres", USER, True, False, False, True)), "current_database"),
        (FakeConn(row=(DB, USER, "postgres", True, False, False, True)), "current_database"),
        (FakeConn(row=(DB, USER, USER, False, False, False, True)), "not encrypted"),
        (FakeConn(row=(DB, USER, USER, True, True, False, True)), "superuser"),
        (FakeConn(row=(DB, USER, USER, True, False, True, True)), "cloudsqlsuperuser"),
        (FakeConn(row=(DB, USER, USER, True, False, False, False)), "vector"),
    ],
)
def test_session_checks_reject(env, conn, word) -> None:
    assert word in session_rejected(conn, env)


# ---- connect() の分岐 ----


def test_prisma_target_keeps_the_old_guard(monkeypatch) -> None:
    monkeypatch.delenv("MIGRATE_TARGET", raising=False)
    monkeypatch.setenv("DIRECT_URL", "postgresql://u:p@10.0.0.1:5432/db")  # Prisma 以外の接続先は、接続前に止まる
    with pytest.raises(SystemExit, match="db.prisma.io"):
        migrate.connect(read_only=True)


def test_prisma_target_does_not_run_the_cloudsql_checks(monkeypatch) -> None:
    # Prisma Postgres は 1 クエリが 1 操作（上限あり）。検査用のクエリを足さない
    calls: list[str] = []
    fake = FakeConn()
    fake.execute = lambda q, p=None: calls.append(q)  # type: ignore[method-assign]
    monkeypatch.delenv("MIGRATE_TARGET", raising=False)
    monkeypatch.setenv("DIRECT_URL", "postgresql://u:p@db.prisma.io:5432/db")
    monkeypatch.setattr(migrate.psycopg, "connect", lambda *a, **k: fake)
    monkeypatch.setattr(migrate, "check_cloudsql_session", lambda *a, **k: pytest.fail("cloudsql check on prisma"))
    migrate.connect(read_only=False)
    assert calls == []  # apply では、接続しただけで 1 クエリも流さない
    migrate.connect(read_only=True)
    assert calls == ["set default_transaction_read_only = on"]  # 従来どおり


def test_cloudsql_target_ignores_direct_url(monkeypatch) -> None:
    monkeypatch.setenv("MIGRATE_TARGET", "cloudsql")
    monkeypatch.setenv("DIRECT_URL", "postgresql://u:p@db.prisma.io:5432/db")
    monkeypatch.delenv("MIGRATE_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="MIGRATE_DATABASE_URL"):
        migrate.connect(read_only=True)


def test_unknown_target_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MIGRATE_TARGET", "other")
    with pytest.raises(SystemExit, match="MIGRATE_TARGET"):
        migrate.connect(read_only=True)


def test_connection_failure_prints_no_secret(monkeypatch, ca, env) -> None:
    def boom(*a, **k):
        raise migrate.psycopg.OperationalError(f"connection to {HOST} failed for {USER} password {PASSWORD}")

    monkeypatch.setenv("MIGRATE_TARGET", "cloudsql")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MIGRATE_DATABASE_URL", url(ca))
    monkeypatch.setattr(migrate.psycopg, "connect", boom)
    with pytest.raises(SystemExit) as e:
        migrate.connect(read_only=True)
    message = str(e.value)
    assert "OperationalError" in message
    assert PASSWORD not in message
    assert HOST not in message


def test_cloudsql_connect_passes_the_url_untouched_and_checks_the_session(monkeypatch, ca, env) -> None:
    seen: dict = {}
    fake = FakeConn()
    fake.execute = lambda q, p=None: SimpleNamespace(fetchone=lambda: fake.row)  # type: ignore[method-assign]

    def fake_connect(u, **kw):
        seen["url"] = u
        return fake

    monkeypatch.setenv("MIGRATE_TARGET", "cloudsql")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MIGRATE_DATABASE_URL", url(ca))
    monkeypatch.setattr(migrate.psycopg, "connect", fake_connect)
    assert migrate.connect(read_only=True) is fake
    assert seen["url"] == url(ca)


# ---- 権限の付与・資材 ----


def test_grant_app_role_uses_a_quoted_identifier_and_no_ownership_change() -> None:
    class Conn:
        def __init__(self) -> None:
            self.statements: list[object] = []

        def execute(self, q, params=None):
            self.statements.append(q)

    conn = Conn()
    migrate.grant_app_role(conn, 'app"; DROP TABLE x; --')
    text = " ".join(s.as_string(None) if hasattr(s, "as_string") else str(s) for s in conn.statements)
    assert '"app""; DROP TABLE x; --"' in text  # 識別子として引用される
    assert not re.search(r"\b(OWNER TO|SUPERUSER|CREATEROLE|ALL PRIVILEGES ON DATABASE)\b", text)
    assert "REVOKE ALL ON TABLE schema_migrations" in text


def test_dockerfile_copies_only_the_migration_materials() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chown=app:app db/migrate.py ./db/migrate.py" in dockerfile
    assert "COPY --chown=app:app db/migrations ./db/migrations" in dockerfile
    assert not re.search(r"^COPY\b.*\bdb(/seeds|/local-init)?\s+\./db\b", dockerfile, flags=re.MULTILINE)
    assert "seeds" not in dockerfile.split("USER app")[0].split("COPY --chown=app:app db/")[1]
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert not re.search(r"^/?db/?$|^db/migrat", ignore, flags=re.MULTILINE)
