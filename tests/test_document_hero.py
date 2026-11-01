"""ヒーロー画像の配信（GET /documents/{id}/hero）と、一覧の `hero_image` のテスト。実 DB・実 API は使わない。

確かめるのは、承認済みの画像だけを返すこと（SQL が approved に絞ること）、無い・見えないグループは同じ 404 であること、
認証、DB の失敗が固定メッセージであること、一覧に画像のバイト列を入れず `hero_image` の真偽だけを入れること。
"""

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.api import documents
from tests.test_access import CAFE_ONLY, set_access  # noqa: F401
from tests.test_documents import SECRET, FakePool, required, summary_row, use_pool  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class HeroPool:
    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any]) -> Cursor:
                pool.calls.append((sql, params))
                if pool.error is not None:
                    raise pool.error
                assert sql == documents._HERO_SQL, f"想定外の SQL: {sql}"
                return Cursor(pool.rows)

        yield Conn()


def hero_row(collection: str | None = "guides", mime: str = "image/png", data: bytes = PNG) -> dict[str, Any]:
    return {"collection": collection, "mime_type": mime, "data": data}


@pytest.fixture
def hero_pool(monkeypatch):
    def install(pool: HeroPool | None) -> HeroPool | None:
        monkeypatch.setattr(documents, "get_pool", lambda: pool)
        return pool

    return install


def test_serves_the_approved_image_bytes_unchanged(client, hero_pool):
    pool = hero_pool(HeroPool([hero_row()]))
    res = client.get("/documents/7/hero")
    assert res.status_code == 200
    assert res.content == PNG
    assert res.headers["content-type"] == "image/png"
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert pool.calls == [(documents._HERO_SQL, {"id": 7})]


def test_the_query_only_reads_approved_hero_images():
    sql = documents._HERO_SQL
    assert "i.status = 'approved'" in sql and "i.kind = 'hero'" in sql and "i.data" in sql


def test_a_document_without_an_approved_image_is_404(client, hero_pool):
    hero_pool(HeroPool([]))
    res = client.get("/documents/7/hero")
    assert res.status_code == 404
    assert res.json() == {"detail": documents._DETAIL_NO_HERO}


def test_an_unexpected_mime_type_is_404_not_served(client, hero_pool):
    hero_pool(HeroPool([hero_row(mime="text/html", data=b"<script>alert(1)</script>")]))
    assert client.get("/documents/7/hero").status_code == 404


def test_without_a_database_the_route_is_503(client, hero_pool):
    hero_pool(None)
    assert client.get("/documents/7/hero").status_code == 503


def test_a_database_failure_is_a_fixed_message(client, hero_pool):
    hero_pool(HeroPool(error=RuntimeError(SECRET)))
    res = client.get("/documents/7/hero")
    assert res.status_code == 503
    assert SECRET not in res.text and res.json() == {"detail": documents._DETAIL_UNAVAILABLE}


def test_an_out_of_range_id_is_422_before_the_database(client, hero_pool):
    pool = hero_pool(HeroPool([hero_row()]))
    assert client.get("/documents/0/hero").status_code == 422
    assert client.get(f"/documents/{documents._MAX_ID + 1}/hero").status_code == 422
    assert pool.calls == []


def test_a_group_outside_the_scope_looks_the_same_as_no_image(client, hero_pool, set_access):
    set_access("enforce", CAFE_ONLY)
    hero_pool(HeroPool([hero_row(collection="zahard")]))
    hidden = client.get("/documents/7/hero")
    missing_pool = HeroPool([])
    hero_pool(missing_pool)
    missing = client.get("/documents/7/hero")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.content == missing.content


def test_a_group_inside_the_scope_is_served(client, hero_pool, set_access):
    set_access("enforce", CAFE_ONLY)
    hero_pool(HeroPool([hero_row(collection="cafe")]))
    assert client.get("/documents/7/hero").status_code == 200


def test_required_rejects_a_request_without_a_valid_token(client, hero_pool, required):
    pool = hero_pool(HeroPool([hero_row()]))
    for headers in ({}, {"Authorization": "Bearer bad-token"}):
        assert client.get("/documents/7/hero", headers=headers).status_code == 401
    assert pool.calls == []
    assert client.get("/documents/7/hero", headers={"Authorization": "Bearer good-token"}).status_code == 200


def test_the_list_carries_a_flag_and_never_the_image_bytes(client, use_pool):
    use_pool(FakePool(summaries=[{**summary_row(1), "hero_image": True}, summary_row(2)]))
    body = client.get("/documents").json()["documents"]
    assert [d["hero_image"] for d in body] == [True, False]
    assert "data" not in body[0] and "hero_image" in documents._LIST_SQL.replace("AS hero_image", "hero_image")
    assert "i.data" not in documents._LIST_SQL
