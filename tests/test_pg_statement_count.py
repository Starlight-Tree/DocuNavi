"""SQL の本数の計測（`app/pg.py` の `StatementCountMiddleware`）と、写しの取得が Postgres で 1 回で済むことのテスト。Postgres は要らない。"""

import asyncio
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import pg
from app.search import pipeline


class _FakePool(pg.PgPool):
    """接続せずに、batch / vectors_query が本数を数えるところまでを通す。"""

    def __init__(self) -> None:  # 親の __init__（接続プールの作成）は呼ばない
        pass

    async def run(self, n: int) -> None:
        pg._count(n)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(pg.StatementCountMiddleware)
    pool = _FakePool()

    @app.get("/threads/{thread_id}")
    async def threads(thread_id: str) -> dict[str, str]:
        await pool.run(3)
        await asyncio.create_task(pool.run(2))  # 子タスクの分も同じリクエストに数える
        return {"ok": "1"}

    @app.get("/quiet")
    async def quiet() -> dict[str, str]:
        return {"ok": "1"}

    return app


def test_logs_statements_per_request_with_route_template(caplog):
    with caplog.at_level(logging.INFO, logger="app.pg"):
        TestClient(_app()).get("/threads/user-secret:abc")
    lines = [r.getMessage() for r in caplog.records if "[計測] DB" in r.getMessage()]
    assert lines == ["[計測] DB GET /threads/{thread_id}: SQL 5 本"]
    assert "user-secret" not in caplog.text


def test_silent_when_no_sql(caplog):
    with caplog.at_level(logging.INFO, logger="app.pg"):
        TestClient(_app()).get("/quiet")
    assert "[計測] DB" not in caplog.text


def test_counts_do_not_leak_between_requests(caplog):
    client = TestClient(_app())
    with caplog.at_level(logging.INFO, logger="app.pg"):
        client.get("/threads/a")
        client.get("/threads/b")
    assert [r.getMessage() for r in caplog.records].count("[計測] DB GET /threads/{thread_id}: SQL 5 本") == 2


def test_corpus_page_is_one_shot_on_postgres(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "pg")
    assert pipeline._corpus_page() >= 2313  # 本番の資料（2,313 チャンク）が 1 回の SQL に収まる
    monkeypatch.setenv("DB_BACKEND", "d1")
    assert pipeline._corpus_page() == pipeline._CORPUS_PAGE
