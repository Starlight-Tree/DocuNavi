"""資料の一覧（app/api/documents.py）のテスト。実 DB・実 API は使わない（接続プールは偽物）。

確かめるのは、DB 未設定・文書 0 件・取得失敗の区別、埋め込みのベクトルを返さないこと、例外の文字列を返さないこと、
AUTH_MODE=required での認証、id の検証。
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from app import auth
from app.api import documents

SECRET = "postgresql://user:pw@db.example.com/x AIzaSECRETKEY"
UPDATED = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)


def summary_row(doc_id: int = 1, source: str = "tsubasa_dorm_rules.pdf", chunks: int = 3, embedded: int = 3) -> dict:
    return {
        "id": doc_id,
        "source": source,
        "status": "ready",
        "failure_reason": None,
        "pages": 3,
        "updated_at": UPDATED,
        "chunk_count": chunks,
        "embedded_count": embedded,
    }


def chunk_row(index: int, content: str = "第1条 この規程は…", embedded: bool = True) -> dict:
    return {
        "chunk_index": index,
        "page": index + 1,
        "page_end": None,
        "kind": "text",
        "heading": "第1章 総則",
        "content": content,
        "embedded": embedded,
        "embedding_model": "gemini-embedding-2" if embedded else None,
    }


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class FakePool:
    """connection() / execute() / fetchall() だけの偽物。SQL の種類で返す行を変え、呼び出しを記録する。"""

    def __init__(
        self,
        summaries: list[dict] | None = None,
        document: list[dict] | None = None,
        chunks: list[dict] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.summaries = summaries or []
        self.document = document or []
        self.chunks = chunks or []
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
                if sql == documents._LIST_SQL:
                    return FakeCursor(pool.summaries)
                if sql == documents._DOCUMENT_SQL:
                    return FakeCursor(pool.document)
                if sql == documents._CHUNKS_SQL:
                    return FakeCursor(pool.chunks)
                raise AssertionError(f"想定外の SQL: {sql}")

        yield Conn()


@pytest.fixture
def use_pool(monkeypatch):
    def install(pool: FakePool | None) -> FakePool | None:
        monkeypatch.setattr(documents, "get_pool", lambda: pool)
        return pool

    return install


# --- 一覧 -------------------------------------------------------------------------------------------------


def test_db_not_configured_is_reported_apart_from_an_empty_list(client, use_pool):
    use_pool(None)
    res = client.get("/documents")
    assert res.status_code == 200
    assert res.json() == {"configured": False, "documents": []}


def test_zero_documents_is_configured_and_empty(client, use_pool):
    use_pool(FakePool(summaries=[]))
    res = client.get("/documents")
    assert res.status_code == 200
    assert res.json() == {"configured": True, "documents": []}


def test_lists_documents_with_counts(client, use_pool):
    pool = use_pool(FakePool(summaries=[summary_row(1), summary_row(2, "cafe_store_guide.pdf", 30, 28)]))
    body = client.get("/documents").json()
    assert body["configured"] is True
    assert [(d["id"], d["source"], d["chunk_count"], d["embedded_count"]) for d in body["documents"]] == [
        (1, "tsubasa_dorm_rules.pdf", 3, 3),
        (2, "cafe_store_guide.pdf", 30, 28),
    ]
    assert body["documents"][0]["pages"] == 3
    assert body["documents"][0]["status"] == "ready"
    assert len(pool.calls) == 1
    assert pool.calls[0][1] == {"limit": documents._MAX_DOCUMENTS, "allowed": None}


def test_titles_are_returned_and_a_missing_or_blank_title_is_null(client, use_pool):
    rows = [
        {**summary_row(1), "title": "つばさ大学学生寮 入寮規程"},
        {**summary_row(2, "cafe_store_guide.pdf"), "title": None},
        {**summary_row(3, "blank.pdf"), "title": "   "},
        summary_row(4, "no_column.pdf"),  # 行に title が無い
    ]
    use_pool(FakePool(summaries=rows))
    titles = [d["title"] for d in client.get("/documents").json()["documents"]]
    assert titles == ["つばさ大学学生寮 入寮規程", None, None, None]


def test_the_detail_returns_the_title_and_both_queries_select_it(client, use_pool):
    use_pool(FakePool(document=[{**summary_row(1), "title": "入寮規程"}], chunks=[chunk_row(0)]))
    assert client.get("/documents/1/chunks").json()["document"]["title"] == "入寮規程"
    assert "d.title AS title" in documents._LIST_SQL
    assert "d.title AS title" in documents._DOCUMENT_SQL


def test_responses_are_not_cached(client, use_pool):
    use_pool(FakePool(summaries=[summary_row()], document=[summary_row()], chunks=[chunk_row(0)]))
    assert client.get("/documents").headers["cache-control"] == "no-store"
    assert client.get("/documents/1/chunks").headers["cache-control"] == "no-store"


def test_list_failure_is_a_fixed_503_without_the_exception_text(client, use_pool, caplog):
    use_pool(FakePool(error=RuntimeError(SECRET)))
    with caplog.at_level(logging.WARNING):
        res = client.get("/documents")
    assert res.status_code == 503
    assert res.json() == {"detail": documents._DETAIL_UNAVAILABLE}
    assert SECRET not in res.text and "AIza" not in res.text
    assert SECRET not in caplog.text and "RuntimeError" in caplog.text


# --- 詳細 -------------------------------------------------------------------------------------------------


def test_chunks_are_returned_in_order_with_full_text_and_counts(client, use_pool):
    long_text = "条文。" * 300
    pool = use_pool(
        FakePool(
            document=[summary_row(7)],
            chunks=[chunk_row(0, long_text), chunk_row(1, embedded=False)],
        )
    )
    res = client.get("/documents/7/chunks")
    assert res.status_code == 200
    body = res.json()
    assert body["document"]["id"] == 7
    assert body["document"]["chunk_count"] == 2
    assert body["document"]["embedded_count"] == 1
    assert [c["chunk_index"] for c in body["chunks"]] == [0, 1]
    assert body["chunks"][0]["content"] == long_text  # 全文（途中で切らない）
    assert body["chunks"][0]["heading"] == "第1章 総則"
    assert body["chunks"][0]["embedding_model"] == "gemini-embedding-2"
    assert body["chunks"][1]["embedded"] is False
    assert [params["id"] for _, params in pool.calls] == [7, 7]


def test_chunks_carry_the_page_labels_and_old_rows_without_them_give_null(client, use_pool):
    labelled = {**chunk_row(0), "page": 2, "page_label": "i", "page_end_label": None}
    use_pool(FakePool(document=[summary_row()], chunks=[labelled, chunk_row(1)]))
    chunks = client.get("/documents/1/chunks").json()["chunks"]
    assert [(c["page"], c["page_label"], c["page_end_label"]) for c in chunks] == [(2, "i", None), (2, None, None)]


def test_embedding_vectors_are_never_selected_or_returned(client, use_pool):
    # 埋め込みの列は「あるか」だけを SELECT し、値そのものは読まない
    for sql in (documents._LIST_SQL, documents._DOCUMENT_SQL, documents._CHUNKS_SQL):
        rest = (
            sql.replace("c.embedding IS NOT NULL", "")
            .replace("count(c.embedding)", "")
            .replace("c.embedding_model", "")
        )
        assert "embedding" not in rest.replace("embedded", "").replace("embedding_model", "")
    use_pool(FakePool(document=[summary_row()], chunks=[chunk_row(0)]))
    body = client.get("/documents/1/chunks").json()
    assert set(body["chunks"][0]) == {
        "chunk_index",
        "page",
        "page_end",
        "page_label",
        "page_end_label",
        "kind",
        "heading",
        "content",
        "embedded",
        "embedding_model",
    }


def test_unknown_document_is_404_and_chunks_are_not_queried(client, use_pool):
    pool = use_pool(FakePool(document=[]))
    res = client.get("/documents/99/chunks")
    assert res.status_code == 404
    assert res.json() == {"detail": documents._DETAIL_NOT_FOUND}
    assert len(pool.calls) == 1


def test_chunks_without_a_database_is_a_fixed_503(client, use_pool):
    use_pool(None)
    res = client.get("/documents/1/chunks")
    assert res.status_code == 503
    assert res.json() == {"detail": documents._DETAIL_NOT_CONFIGURED}


def test_chunks_failure_is_a_fixed_503_without_the_exception_text(client, use_pool):
    use_pool(FakePool(error=ConnectionError(SECRET)))
    res = client.get("/documents/1/chunks")
    assert res.status_code == 503
    assert res.json() == {"detail": documents._DETAIL_UNAVAILABLE}
    assert SECRET not in res.text


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "9223372036854775808", "1.5"])
def test_document_id_is_validated_before_the_database(client, use_pool, bad):
    pool = use_pool(FakePool(document=[summary_row()]))
    res = client.get(f"/documents/{bad}/chunks")
    assert res.status_code == 422
    assert pool.calls == []


def test_the_api_is_read_only(client):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/documents").status_code == 405
        assert getattr(client, method)("/documents/1/chunks").status_code == 405


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


@pytest.mark.parametrize("path", ["/documents", "/documents/1/chunks"])
def test_required_rejects_requests_without_a_valid_token(client, use_pool, required, path):
    pool = use_pool(FakePool(summaries=[summary_row()], document=[summary_row()], chunks=[chunk_row(0)]))
    for headers in ({}, {"Authorization": "Bearer bad-token"}, {"Authorization": "Basic good-token"}):
        res = client.get(path, headers=headers)
        assert res.status_code == 401
        assert res.headers["www-authenticate"] == "Bearer"
        assert "good-token" not in res.text
    assert pool.calls == []  # 認証の前に DB を読まない


@pytest.mark.parametrize("path", ["/documents", "/documents/1/chunks"])
def test_required_accepts_a_valid_token(client, use_pool, required, path):
    use_pool(FakePool(summaries=[summary_row()], document=[summary_row()], chunks=[chunk_row(0)]))
    assert client.get(path, headers={"Authorization": "Bearer good-token"}).status_code == 200


def test_off_needs_no_token(client, use_pool, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "off")
    use_pool(FakePool(summaries=[summary_row()]))
    assert client.get("/documents").status_code == 200


# --- 版（documents.revision）--------------------------------------------------------------------------------


def test_the_api_returns_the_revision_and_treats_blank_or_missing_as_null(client, use_pool):
    use_pool(
        FakePool(
            summaries=[
                {**summary_row(1), "revision": "改訂 2"},
                {**summary_row(2, "b.pdf"), "revision": "  初版 "},
                {**summary_row(3, "c.pdf"), "revision": "   "},
                {**summary_row(4, "d.pdf"), "revision": "版" * (40 + 1)},
                summary_row(5, "e.pdf"),  # 行に revision が無い
            ]
        )
    )
    revisions = [d["revision"] for d in client.get("/documents").json()["documents"]]
    assert revisions == ["改訂 2", "初版", None, None, None]


def test_both_queries_select_the_revision():
    assert "d.revision AS revision" in documents._LIST_SQL
    assert "d.revision AS revision" in documents._DOCUMENT_SQL
