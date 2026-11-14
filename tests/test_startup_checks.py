"""起動時の DB の検査を 1 回の問い合わせにまとめる（app/startup_checks.py）。偽の接続プールで、SQL の回数と判定を確かめる。"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app import access, startup_checks
from app.ingestion import embedding
from app.search import vector as search_vector
from app.search.types import VectorSearchError

PROBE_OK = {"default_roles": 1, "default_collections": 3, "assignments": 2}
DOC = {
    "id": 1,
    "source": "a.pdf",
    "status": "ready",
    "heading": "見出し",
    "content": "本文",
    "has_vector": True,
    "model": embedding.MODEL,
    "dims": embedding.DIMENSIONS,
    "input_hash": embedding.input_hash("見出し", "本文"),
}
NO_DOC = {k: None for k in DOC}  # 資料が 1 件も無いとき、LEFT JOIN が返す 1 行


class Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class Pool:
    """SQL の本文で、まとめた問い合わせ・アクセス制御だけ・資料だけを見分けて、行を返す偽の接続プール。"""

    def __init__(self, probe: dict[str, Any], docs: list[dict[str, Any]], fail_combined: bool = False) -> None:
        self.probe, self.docs, self.fail_combined = probe, docs, fail_combined
        self.sql: list[str] = []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql: str, params: Any = None) -> Cursor:
                pool.sql.append(sql)
                if sql == startup_checks.COMBINED_SQL:
                    if pool.fail_combined:
                        raise RuntimeError("postgresql://u:pw@h/x")
                    return Cursor([{**pool.probe, **(d if d else NO_DOC)} for d in (pool.docs or [None])])
                if sql == access._PROBE_SQL:
                    return Cursor([pool.probe])
                if sql == search_vector._VERIFY_SQL:
                    return Cursor(pool.docs)
                raise AssertionError(sql)

        yield Conn()


@pytest.fixture
def real_llm(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(startup_checks, "is_mock_mode", lambda: False)


def use(monkeypatch, pool: Pool | None, mode: str = "enforce") -> None:
    monkeypatch.setenv("ACCESS_MODE", mode)
    monkeypatch.setattr(startup_checks, "get_pool", lambda: pool)
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    monkeypatch.setattr(search_vector, "get_pool", lambda: pool)


def test_the_two_checks_run_as_one_query_when_both_are_enabled(monkeypatch, real_llm):
    pool = Pool(PROBE_OK, [DOC])
    use(monkeypatch, pool)
    asyncio.run(startup_checks.run_startup_checks())
    assert pool.sql == [startup_checks.COMBINED_SQL]


def test_the_combined_sql_is_built_from_the_two_modules_sql():
    assert access.PROBE_COLUMNS_SQL in startup_checks.COMBINED_SQL
    assert search_vector.VERIFY_SQL in startup_checks.COMBINED_SQL


def test_a_database_without_documents_fails_even_though_the_join_returns_one_row(monkeypatch, real_llm):
    use(monkeypatch, Pool(PROBE_OK, []))
    with pytest.raises(VectorSearchError, match="検索できる資料が DB にありません"):
        asyncio.run(startup_checks.run_startup_checks())


def test_a_missing_default_role_fails_before_the_documents_are_judged(monkeypatch, real_llm):
    use(monkeypatch, Pool({"default_roles": 0, "default_collections": 0, "assignments": 0}, []))
    with pytest.raises(access.AccessConfigError, match="default"):
        asyncio.run(startup_checks.run_startup_checks())


def test_stale_embeddings_fail(monkeypatch, real_llm):
    use(monkeypatch, Pool(PROBE_OK, [{**DOC, "has_vector": False}]))
    with pytest.raises(VectorSearchError, match="埋め込み"):
        asyncio.run(startup_checks.run_startup_checks())


def test_a_failed_combined_query_falls_back_to_the_separate_checks(monkeypatch, real_llm):
    pool = Pool(PROBE_OK, [DOC], fail_combined=True)
    use(monkeypatch, pool)
    asyncio.run(startup_checks.run_startup_checks())  # 単独の検査が通れば、起動できる
    assert pool.sql == [startup_checks.COMBINED_SQL, access._PROBE_SQL, search_vector._VERIFY_SQL]


def test_a_failed_combined_query_reports_the_cause_with_the_fixed_message(monkeypatch, real_llm):
    class Broken(Pool):
        @asynccontextmanager
        async def connection(self):
            raise RuntimeError("postgresql://u:pw@h/x")
            yield

    use(monkeypatch, Broken(PROBE_OK, [DOC]))
    with pytest.raises(access.AccessConfigError, match="20261026045921") as failure:
        asyncio.run(startup_checks.run_startup_checks())
    assert "postgresql://" not in str(failure.value)


def test_access_off_checks_only_the_documents(monkeypatch, real_llm):
    pool = Pool(PROBE_OK, [DOC])
    use(monkeypatch, pool, mode="off")
    asyncio.run(startup_checks.run_startup_checks())
    assert pool.sql == [search_vector._VERIFY_SQL]


def test_mock_mode_checks_only_the_access_tables(monkeypatch):
    monkeypatch.setattr(startup_checks, "is_mock_mode", lambda: True)
    pool = Pool(PROBE_OK, [DOC])
    use(monkeypatch, pool)
    asyncio.run(startup_checks.run_startup_checks())
    assert pool.sql == [access._PROBE_SQL]


def test_an_unknown_access_mode_fails_before_any_query(monkeypatch, real_llm):
    pool = Pool(PROBE_OK, [DOC])
    use(monkeypatch, pool, mode="strict")
    with pytest.raises(access.AccessConfigError):
        asyncio.run(startup_checks.run_startup_checks())
    assert pool.sql == []


def test_a_missing_api_key_fails_before_any_query(monkeypatch, real_llm):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.delenv("EMBEDDING_USE_VERTEXAI", raising=False)
    pool = Pool(PROBE_OK, [DOC])
    use(monkeypatch, pool)
    with pytest.raises(VectorSearchError, match="GEMINI_API_KEY"):
        asyncio.run(startup_checks.run_startup_checks())
    assert pool.sql == []
