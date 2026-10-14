"""会話履歴の保存先を、本物の Postgres で検証する（TEST_DATABASE_URL があるときだけ動く）。

TEST_DATABASE_URL は、書き込んでよい Postgres（開発用の DB でよい）への接続 URL。テストごとに専用のスキーマを
作り（search_path をそこへ向ける）、終わったら DROP SCHEMA ... CASCADE で消す。既存のテーブルには触れない。
実行例: TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres pytest tests/test_history_postgres.py
"""

import asyncio
import os
import random
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from psycopg.conninfo import make_conninfo

from app import history
from app.agents.graph import MockChatModel, build_graph
from app.main import app

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL が未設定")

QUESTION = "ザーハードの大気について教えて"


@pytest.fixture
def db(monkeypatch):
    """専用スキーマを作り、DATABASE_URL をそこへ向ける。(接続情報, スキーマ名) を返す。"""
    schema = f"zhist_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    # application_name は、テストが DB 側からこの接続だけを見分けて切るために付ける
    conninfo = make_conninfo(TEST_DATABASE_URL, options=f"-c search_path={schema}", application_name=schema)
    monkeypatch.setenv("DATABASE_URL", conninfo)
    monkeypatch.delenv("DATABASE_POOL_MAX_SIZE", raising=False)
    try:
        yield conninfo, schema
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def _turn(saver, message: str, thread_id: str) -> dict:
    graph = build_graph(MockChatModel(), checkpointer=saver)
    return await graph.ainvoke({"messages": [HumanMessage(content=message)], "context": ""}, _config(thread_id))


async def _messages(saver, thread_id: str) -> list:
    graph = build_graph(MockChatModel(), checkpointer=saver)
    return list((await graph.aget_state(_config(thread_id))).values.get("messages", []))


def test_tables_are_created_in_the_target_schema_only(db):
    _, schema = db

    async def scenario():
        async with history.history_store():
            pass

    asyncio.run(scenario())

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,)
        ).fetchall()
    assert {r[0] for r in rows} == {
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    }


def test_history_survives_a_restart(db):
    thread_id = str(uuid.uuid4())

    async def scenario():
        async with history.history_store() as saver:
            await _turn(saver, QUESTION, thread_id)
        # 再起動に相当: 接続プールも保存先もグラフも作り直す。setup() は適用済みを確かめるだけ
        async with history.history_store() as saver:
            before = await _messages(saver, thread_id)
            await _turn(saver, "もう少し詳しく", thread_id)
            after = await _messages(saver, thread_id)
        return before, after

    before, after = asyncio.run(scenario())

    assert len(before) >= 2
    assert len(after) >= len(before) + 2  # 続きの発言が、前のターンの後ろに足される
    assert before[0].content == QUESTION


def test_threads_are_kept_apart(db):
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    async def scenario():
        async with history.history_store() as saver:
            await _turn(saver, QUESTION, first)
            return await _messages(saver, second)

    assert asyncio.run(scenario()) == []


def test_concurrent_first_startup_succeeds(db):
    """初回に何台かが同時に起動しても、全部が起動できる（競合のエラーを再試行で吸収する）。"""

    async def open_and_close():
        async with history.history_store():
            await asyncio.sleep(0.05)

    async def scenario():
        await asyncio.gather(*(open_and_close() for _ in range(8)))

    asyncio.run(scenario())


def test_dropped_connections_are_replaced(db):
    """DB 側から接続が切られた（再起動・プーラーの整理）あとも、次のリクエストは成功する。"""
    _, schema = db
    thread_id = str(uuid.uuid4())

    def terminate_pool_backends() -> int:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
            rows = admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND application_name = %s",
                (schema,),
            ).fetchall()
        return len(rows)

    async def scenario():
        async with history.history_store() as saver:
            await _turn(saver, QUESTION, thread_id)
            killed = await asyncio.to_thread(terminate_pool_backends)
            await _turn(saver, "続きを教えて", thread_id)
            return killed, await _messages(saver, thread_id)

    killed, messages = asyncio.run(scenario())

    assert killed >= 1
    assert len(messages) >= 4


def test_cancelled_turns_do_not_leak_connections(db):
    """クライアントの切断（タスクの取り消し）が書き込みの途中に当たっても、接続を取り残さない。"""

    async def scenario():
        async with history.history_store() as saver:
            rng = random.Random(0)
            for i in range(25):
                task = asyncio.ensure_future(_turn(saver, QUESTION, str(uuid.uuid4())))
                await asyncio.sleep(rng.uniform(0.0, 0.03))
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await asyncio.sleep(0.3)  # 取り消し後の後始末が済むのを待つ
            stats = saver.conn.get_stats()
            await _turn(saver, QUESTION, str(uuid.uuid4()))  # 取り消しの後も普通に使える
            return stats

    stats = asyncio.run(scenario())

    assert stats["pool_available"] == stats["pool_size"]  # 借りっぱなしの接続が無い


def test_app_serves_chat_and_stores_the_thread(db):
    _, schema = db
    thread_id = str(uuid.uuid4())

    with TestClient(app) as client:
        assert client.get("/health").json()["history"] == "postgres"
        res = client.post("/chat", json={"message": QUESTION, "thread_id": thread_id})
        assert res.status_code == 200
        again = client.post("/chat", json={"message": "もう少し詳しく", "thread_id": thread_id})
        assert again.status_code == 200 and again.json()["thread_id"] == thread_id

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        count = conn.execute(
            f'SELECT count(*) FROM "{schema}".checkpoints WHERE thread_id = %s', (thread_id,)
        ).fetchone()[0]
    assert count >= 2  # ターンごと・ステップごとにチェックポイントが残る
