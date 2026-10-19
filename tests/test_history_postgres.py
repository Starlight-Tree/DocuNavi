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
from app.agents.graph import build_graph
from app.main import app
from tests.fakes import SearchingMockLLM

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
    graph = build_graph(SearchingMockLLM(), checkpointer=saver)
    return await graph.ainvoke({"messages": [HumanMessage(content=message)], "context": ""}, _config(thread_id))


async def _messages(saver, thread_id: str) -> list:
    graph = build_graph(SearchingMockLLM(), checkpointer=saver)
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


def _age_thread(schema: str, thread_id: str, days: int) -> None:
    """そのスレッドのチェックポイントの時刻を、days 日前に書き換える（保存期間のテスト用）。"""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(
            f'UPDATE "{schema}".checkpoints '
            "SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb((now() - make_interval(days => %s))::text)) "
            "WHERE thread_id = %s",
            (days, thread_id),
        )


def _row_counts(schema: str, thread_id: str) -> dict[str, int]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        return {
            table: conn.execute(
                f'SELECT count(*) FROM "{schema}".{table} WHERE thread_id = %s', (thread_id,)
            ).fetchone()[0]
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
        }


def _seed_threads(schema: str) -> tuple[str, str]:
    """古い会話と新しい会話を 1 件ずつ作る。(古い, 新しい) の thread_id を返す。"""
    old, fresh = str(uuid.uuid4()), str(uuid.uuid4())

    async def scenario():
        async with history.history_store() as saver:
            await _turn(saver, QUESTION, old)
            await _turn(saver, QUESTION, fresh)

    asyncio.run(scenario())
    _age_thread(schema, old, days=40)
    return old, fresh


def test_purge_dry_run_deletes_nothing(db, capsys):
    from app import history_admin

    _, schema = db
    old, fresh = _seed_threads(schema)

    assert history_admin.main(["--older-than-days", "30"]) == 0

    assert "1 件" in capsys.readouterr().out
    assert _row_counts(schema, old)["checkpoints"] > 0
    assert _row_counts(schema, fresh)["checkpoints"] > 0


def test_purge_deletes_only_stale_threads(db, capsys):
    from app import history_admin

    _, schema = db
    old, fresh = _seed_threads(schema)
    before = _row_counts(schema, fresh)
    assert all(n > 0 for n in _row_counts(schema, old).values())

    assert history_admin.main(["--older-than-days", "30", "--apply"]) == 0

    assert "削除した会話: 1 件" in capsys.readouterr().out
    assert _row_counts(schema, old) == {"checkpoints": 0, "checkpoint_blobs": 0, "checkpoint_writes": 0}
    assert _row_counts(schema, fresh) == before  # 新しい会話は、行数が変わらない

    async def scenario():
        async with history.history_store() as saver:
            return await _messages(saver, fresh), await _messages(saver, old)

    kept, gone = asyncio.run(scenario())
    assert len(kept) >= 2 and gone == []  # 残した会話は読め、消した会話は空になる


def test_purge_by_thread_id(db, capsys):
    from app import history_admin

    _, schema = db
    old, fresh = _seed_threads(schema)

    assert history_admin.main(["--thread-id", fresh, "--apply"]) == 0

    assert _row_counts(schema, fresh)["checkpoints"] == 0
    assert _row_counts(schema, old)["checkpoints"] > 0  # 指定していない会話（古くても）は残る
    assert "1 件" in capsys.readouterr().out


def test_purge_unknown_thread_id_is_zero(db, capsys):
    from app import history_admin

    _seed_threads(db[1])

    assert history_admin.main(["--thread-id", "no-such-thread", "--apply"]) == 0
    assert "0 件" in capsys.readouterr().out


def test_purge_without_tables_fails_cleanly(db, capsys):
    from app import history_admin

    # テーブルを作る前（アプリを一度も起動していない DB）
    assert history_admin.main(["--older-than-days", "30"]) == 1
    assert "テーブル" in capsys.readouterr().err


def test_purge_keeps_a_thread_with_any_recent_checkpoint(db, capsys):
    """古いチェックポイントが残っていても、最新が新しい会話は消さない（会話の古さは最新で決める）。"""
    from app import history_admin

    _, schema = db
    _, fresh = _seed_threads(schema)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(
            f'UPDATE "{schema}".checkpoints '
            "SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb((now() - interval '90 days')::text)) "
            f'WHERE thread_id = %s AND checkpoint_id = (SELECT min(checkpoint_id) FROM "{schema}".checkpoints WHERE thread_id = %s)',
            (fresh, fresh),
        )
        total = admin.execute(f'SELECT count(*) FROM "{schema}".checkpoints WHERE thread_id = %s', (fresh,)).fetchone()[
            0
        ]
    assert total >= 2  # 古い 1 行と、新しい行がある

    assert history_admin.main(["--older-than-days", "30", "--apply"]) == 0

    assert _row_counts(schema, fresh)["checkpoints"] == total


def test_purge_at_seven_days_follows_the_clock(db, capsys):
    """運用の最初の設定（7 日）を、日がたつ様子で確かめる: 6 日目は残り、8 日目に消え、続きを話した会話は残る。"""
    from app import history_admin

    _, schema = db
    idle, active = str(uuid.uuid4()), str(uuid.uuid4())

    async def talk(*thread_ids):
        async with history.history_store() as saver:
            for thread_id in thread_ids:
                await _turn(saver, QUESTION, thread_id)

    asyncio.run(talk(idle, active))
    purge = ["--older-than-days", "7", "--apply"]

    # 1 日目: 何も消えない
    assert history_admin.main(purge) == 0
    assert "削除した会話: 0 件" in capsys.readouterr().out

    # 6 日たつ: まだ消えない
    _age_thread(schema, idle, days=6)
    _age_thread(schema, active, days=6)
    assert history_admin.main(purge) == 0
    assert "削除した会話: 0 件" in capsys.readouterr().out
    assert _row_counts(schema, idle)["checkpoints"] > 0

    # 6 日目に片方だけ続きを話す（最新のチェックポイントが新しくなる）
    asyncio.run(talk(active))

    # 8 日たつ: 話さなかった会話だけ消える。続きを話した会話は、その時点から数え直される
    _age_thread(schema, idle, days=8)
    assert history_admin.main(purge) == 0
    assert "削除した会話: 1 件" in capsys.readouterr().out
    assert _row_counts(schema, idle) == {"checkpoints": 0, "checkpoint_blobs": 0, "checkpoint_writes": 0}
    assert _row_counts(schema, active)["checkpoints"] > 0

    # 同じ条件で何度実行しても、結果は変わらない
    assert history_admin.main(purge) == 0
    assert "削除した会話: 0 件" in capsys.readouterr().out
    assert _row_counts(schema, active)["checkpoints"] > 0
