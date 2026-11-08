"""app/access_admin.py（ロールの割り当てコマンド）。

- 引数の検査と、DATABASE_URL・DB_BACKEND の扱いは、DB なしで動く。
- 割り当て・解除の本体は、`PG_TEST_URL`（pgvector 入りの Postgres）があるときだけ、使い捨ての DB で本物の SQL を実行して確かめる。
"""

import json

import pytest

from app import access_admin
from app.pg import SyncPg
from tests.test_history_pg_backend import needs_pg, pg_url  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    monkeypatch.setattr(access_admin, "load_dotenv", lambda *a, **k: False)


def test_a_command_is_required():
    with pytest.raises(SystemExit) as exc:
        access_admin.main([])
    assert exc.value.code == 2


def test_email_and_user_id_are_exclusive():
    with pytest.raises(SystemExit) as exc:
        access_admin.main(["show", "--email", "a@example.com", "--user-id", "u1"])
    assert exc.value.code == 2


def test_role_is_required_for_assign():
    with pytest.raises(SystemExit) as exc:
        access_admin.main(["assign", "--email", "a@example.com"])
    assert exc.value.code == 2


def test_requires_the_pg_backend(monkeypatch, capsys):
    monkeypatch.setenv("DB_BACKEND", "d1")
    assert access_admin.main(["show", "--email", "a@example.com"]) == 2
    assert "DB_BACKEND=pg" in capsys.readouterr().err


def test_without_database_url_nothing_is_done(monkeypatch, capsys):
    monkeypatch.setenv("DB_BACKEND", "pg")
    monkeypatch.setenv("DATABASE_URL", "")
    assert access_admin.main(["assign", "--email", "a@example.com", "--role", "r", "--apply"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def _seed(url: str) -> None:
    with SyncPg(url) as db:
        db.batch(
            [
                ("INSERT INTO document_collections (name, label) VALUES ('a', 'A'), ('b', 'B'), ('c', 'C')", None),
                ("INSERT INTO roles (name, label) VALUES ('default', 'D'), ('staff', 'S'), ('wide', 'W')", None),
                (
                    "INSERT INTO role_collection_access (role, collection) VALUES "
                    "('default', 'a'), ('default', 'b'), ('staff', 'b'), ('wide', 'a'), ('wide', 'b'), ('wide', 'c')",
                    None,
                ),
                ("INSERT INTO \"user\" (id, name, email) VALUES ('u1', 'U', 'User@Example.com')", None),
            ]
        )


def _run(monkeypatch, url: str, *argv: str) -> int:
    monkeypatch.setenv("DB_BACKEND", "pg")
    monkeypatch.setenv("DATABASE_URL", url)
    return access_admin.main(list(argv))


def _roles(url: str) -> list[str]:
    with SyncPg(url) as db:
        return [r["role"] for r in db.execute("SELECT role FROM user_roles ORDER BY role")]


def _audit(url: str) -> list[dict]:
    with SyncPg(url) as db:
        return db.execute("SELECT actor, table_name, op, row_data FROM access_audit ORDER BY id")


@needs_pg
def test_show_uses_the_default_role_and_does_not_print_identifiers(pg_url, monkeypatch, capsys):  # noqa: F811
    _seed(pg_url)
    assert _run(monkeypatch, pg_url, "show", "--email", "user@example.com") == 0
    out = capsys.readouterr().out
    assert "割り当てなし = default" in out and "a、b" in out
    assert "user@example.com" not in out.lower() and "u1" not in out


@needs_pg
def test_assign_is_a_dry_run_unless_apply(pg_url, monkeypatch, capsys):  # noqa: F811
    _seed(pg_url)
    assert _run(monkeypatch, pg_url, "assign", "--email", "user@example.com", "--role", "staff") == 0
    out = capsys.readouterr().out
    assert "a、b → b" in out and "default は使われなくなります" in out and "書き込みはしていません" in out
    assert _roles(pg_url) == [] and _audit(pg_url) == []


@needs_pg
def test_assign_with_apply_writes_the_role_and_one_audit_row(pg_url, monkeypatch):  # noqa: F811
    _seed(pg_url)
    assert _run(monkeypatch, pg_url, "assign", "--user-id", "u1", "--role", "staff", "--apply") == 0
    assert _roles(pg_url) == ["staff"]
    rows = _audit(pg_url)
    assert len(rows) == 1 and rows[0]["actor"] == "access_admin_cli" and rows[0]["op"] == "INSERT"
    data = rows[0]["row_data"]
    data = json.loads(data) if isinstance(data, str) else data
    assert data == {"user_id": "u1", "role": "staff"}
    # もう一度は、変更なし（監査も増えない）
    assert _run(monkeypatch, pg_url, "assign", "--user-id", "u1", "--role", "staff", "--apply") == 0
    assert len(_audit(pg_url)) == 1


@needs_pg
def test_unknown_user_or_role_is_refused_without_writing(pg_url, monkeypatch, capsys):  # noqa: F811
    _seed(pg_url)
    assert _run(monkeypatch, pg_url, "assign", "--email", "nobody@example.com", "--role", "staff", "--apply") == 1
    assert _run(monkeypatch, pg_url, "assign", "--user-id", "u1", "--role", "nope", "--apply") == 1
    assert _roles(pg_url) == [] and _audit(pg_url) == []
    assert "見つかりません" in capsys.readouterr().err


@needs_pg
def test_revoking_the_last_role_that_widens_access_needs_allow_widen(pg_url, monkeypatch, capsys):  # noqa: F811
    _seed(pg_url)
    assert _run(monkeypatch, pg_url, "assign", "--user-id", "u1", "--role", "staff", "--apply") == 0
    # staff（b だけ）を外すと default（a、b）に戻る = a が増える
    assert _run(monkeypatch, pg_url, "revoke", "--user-id", "u1", "--role", "staff", "--apply") == 1
    assert _roles(pg_url) == ["staff"]
    assert "--allow-widen" in capsys.readouterr().err
    assert _run(monkeypatch, pg_url, "revoke", "--user-id", "u1", "--role", "staff", "--apply", "--allow-widen") == 0
    assert _roles(pg_url) == []
    assert [r["op"] for r in _audit(pg_url)] == ["INSERT", "DELETE"]


@needs_pg
def test_revoking_one_of_two_roles_that_narrows_access_is_allowed(pg_url, monkeypatch):  # noqa: F811
    _seed(pg_url)
    for role in ("wide", "staff"):
        assert _run(monkeypatch, pg_url, "assign", "--user-id", "u1", "--role", role, "--apply") == 0
    assert _run(monkeypatch, pg_url, "revoke", "--user-id", "u1", "--role", "wide", "--apply") == 0
    assert _roles(pg_url) == ["staff"]
