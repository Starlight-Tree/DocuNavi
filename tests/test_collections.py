"""資料グループの一覧（app/api/collections.py）と、マイグレーション 0006 の中身のテスト。実 DB は使わない（接続プールは偽物）。

確かめるのは、DB 未設定・0 件・表が無い・取得失敗の区別、未登録の名前の返し方、例外の文字列を返さないこと、AUTH_MODE=required での認証。
SQL が本物の Postgres で期待どおりの行を返すかは、ここでは確かめない（実 DB が要る）。
"""

import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app import auth
from app.api import collections
from app.d1 import D1Error

SECRET = "postgresql://user:pw@db.example.com/x AIzaSECRETKEY"
MIGRATION = (
    Path(__file__).resolve().parent.parent / "supabase" / "migrations" / "20261025205258_document_collections.sql"
)


def row(
    name: str,
    label: str | None = None,
    description: str | None = "",
    sort_order: int | None = 10,
    registered: bool = True,
    document_count: int = 1,
    ready_count: int | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "description": description,
        "sort_order": sort_order,
        "registered": registered,
        "document_count": document_count,
        "ready_count": document_count if ready_count is None else ready_count,
    }


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class FakePool:
    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any]) -> FakeCursor:
                pool.calls.append((sql, params))
                if pool.error is not None:
                    raise pool.error
                assert sql == collections._LIST_SQL
                return FakeCursor(pool.rows)

        yield Conn()


@pytest.fixture
def use_pool(monkeypatch):
    def install(pool: FakePool | None) -> FakePool | None:
        monkeypatch.setattr(collections, "get_pool", lambda: pool)
        return pool

    return install


# --- 一覧 -------------------------------------------------------------------------------------------------


def test_db_not_configured_is_reported_apart_from_an_empty_list(client, use_pool):
    use_pool(None)
    res = client.get("/collections")
    assert res.status_code == 200
    assert res.json() == {"configured": False, "collections": []}


def test_an_empty_result_is_configured_and_empty(client, use_pool):
    use_pool(FakePool(rows=[]))
    assert client.get("/collections").json() == {"configured": True, "collections": []}


def test_lists_registered_groups_with_label_description_and_counts(client, use_pool):
    pool = use_pool(
        FakePool(
            rows=[
                row("zahard", "設定資料", "ザーハードに関する設定資料です。", 10, document_count=3),
                row("memo", "メモ", "", 40, document_count=0),
            ]
        )
    )
    body = client.get("/collections").json()
    assert body["configured"] is True
    assert body["collections"][0] == {
        "name": "zahard",
        "label": "設定資料",
        "description": "ザーハードに関する設定資料です。",
        "sort_order": 10,
        "registered": True,
        "document_count": 3,
        "ready_count": 3,
    }
    assert body["collections"][1]["document_count"] == 0 and body["collections"][1]["ready_count"] == 0
    assert pool.calls == [(collections._LIST_SQL, {"limit": collections._MAX_COLLECTIONS, "allowed": None})]


def test_an_unregistered_name_is_returned_with_the_name_as_its_label(client, use_pool):
    use_pool(FakePool(rows=[row("newfolder", None, None, None, registered=False, document_count=2, ready_count=1)]))
    (item,) = client.get("/collections").json()["collections"]
    assert item == {
        "name": "newfolder",
        "label": "newfolder",
        "description": "",
        "sort_order": None,
        "registered": False,
        "document_count": 2,
        "ready_count": 1,
    }


def test_a_blank_label_falls_back_to_the_name(client, use_pool):
    use_pool(FakePool(rows=[row("cafe", "   ")]))
    assert client.get("/collections").json()["collections"][0]["label"] == "cafe"


def test_the_response_is_not_cached(client, use_pool):
    use_pool(FakePool(rows=[row("cafe", "店舗案内")]))
    assert client.get("/collections").headers["cache-control"] == "no-store"


def test_a_missing_table_is_a_distinct_fixed_503(client, use_pool, caplog):
    use_pool(FakePool(error=D1Error("no such table: document_collections" + SECRET)))
    with caplog.at_level(logging.WARNING):
        res = client.get("/collections")
    assert res.status_code == 503
    assert res.json() == {"detail": collections._DETAIL_NOT_READY}
    assert SECRET not in res.text and SECRET not in caplog.text


def test_the_503_responses_are_not_cached_either(client, use_pool):
    for error in (D1Error("no such table: x"), RuntimeError("x")):
        use_pool(FakePool(error=error))
        res = client.get("/collections")
        assert res.status_code == 503 and res.headers["cache-control"] == "no-store"


def test_any_other_failure_is_a_fixed_503_without_the_exception_text(client, use_pool, caplog):
    use_pool(FakePool(error=RuntimeError(SECRET)))
    with caplog.at_level(logging.WARNING):
        res = client.get("/collections")
    assert res.status_code == 503
    assert res.json() == {"detail": collections._DETAIL_UNAVAILABLE}
    assert SECRET not in res.text and "AIza" not in res.text
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text


def test_the_list_is_read_only(client, use_pool):
    use_pool(FakePool(rows=[]))
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/collections").status_code == 405


def test_the_query_reads_no_embeddings_and_never_writes():
    sql = collections._LIST_SQL.upper()
    assert "EMBEDDING" not in sql and "CONTENT" not in sql
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER)\b", sql)


# --- 認証 -------------------------------------------------------------------------------------------------


@pytest.fixture
def required(monkeypatch):
    """AUTH_MODE=required。署名の検証そのものは tests/test_auth.py が確かめるので、ここでは差し替える。"""
    user = str(uuid.uuid4())
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("AUTH_BASE_URL", "https://auth.docunavi.example")

    async def verify(token: str) -> str:
        if token != "good-token":
            raise auth.InvalidToken("signature")
        return user

    monkeypatch.setattr(auth, "verify_token", verify)


def test_required_rejects_requests_without_a_valid_token(client, use_pool, required):
    pool = use_pool(FakePool(rows=[row("cafe", "店舗案内")]))
    for headers in ({}, {"Authorization": "Bearer bad-token"}, {"Authorization": "Basic good-token"}):
        res = client.get("/collections", headers=headers)
        assert res.status_code == 401
        assert res.headers["www-authenticate"] == "Bearer"
    assert pool.calls == []  # 認証の前に DB を読まない


def test_required_accepts_a_valid_token(client, use_pool, required):
    use_pool(FakePool(rows=[row("cafe", "店舗案内")]))
    assert client.get("/collections", headers={"Authorization": "Bearer good-token"}).status_code == 200


def test_off_needs_no_token(client, use_pool, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "off")
    use_pool(FakePool(rows=[]))
    assert client.get("/collections").status_code == 200


# --- マイグレーション 0006 の中身（文字列の検査。実際の適用は DB が要る） --------------------------------------


def test_the_migration_name_pattern_matches_the_api_and_ingestion_pattern():
    from app.api import documents
    from app.ingestion import service

    sql = MIGRATION.read_text(encoding="utf-8")
    pattern = "^[A-Za-z0-9_-]{1,40}$"
    assert pattern in sql
    assert documents._COLLECTION_PATTERN.pattern == "[A-Za-z0-9_-]{1,40}"
    assert service.COLLECTION_PATTERN.pattern == "[A-Za-z0-9_-]{1,40}"


def test_the_migration_is_rerunnable_and_has_no_foreign_key():
    sql = MIGRATION.read_text(encoding="utf-8")
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--")).lower()
    assert "create table if not exists chatbot.document_collections" in code
    assert "on conflict (name) do nothing" in code
    assert "drop trigger if exists" in code and "create or replace function" in code
    assert "references" not in code and "foreign key" not in code
    assert "enable row level security" not in code
    assert "grant select on chatbot.document_collections to chatbot_app" in code
    # documents / chunks には触れない
    assert "alter table chatbot.documents" not in code and "chatbot.chunks" not in code


def test_the_seeded_groups_are_the_three_existing_ones_with_their_labels():
    sql = MIGRATION.read_text(encoding="utf-8")
    seeded = re.findall(r"\('([A-Za-z0-9_-]+)', '([^']+)', '([^']*)', (\d+)\)", sql)
    assert [(n, label) for n, label, _, _ in seeded] == [
        ("zahard", "設定資料"),
        ("cafe", "店舗案内"),
        ("guides", "規程・取扱説明書"),
    ]
