"""サーバーの停止（SIGTERM）の合図で、進行中の /chat と /chat/stream が LLM 呼び出しを取り消して終わること（ロードマップ ⑥）。

合図の本体は app/drain.py。置き場は `app.state.drain` で、`app/serve.py` が SIGTERM の少し後に立てる。
手書きの ASGI ハーネス（tests/test_chat_disconnect.py と同じ）で、サーバーは起動しない。
"""

import asyncio
import time

import pytest
from test_chat_disconnect import FAST, HANG, Connection, SlowLLM, leftover_tasks, until
from test_chat_stream import LONG, StreamLLM
from test_chat_stream_disconnect import StreamConnection, start

from app.agents.graph import build_graph
from app.api import routes
from app.drain import Drain
from app.main import app


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)

    def _serve(llm: StreamLLM, hedge=FAST):
        graph = build_graph(llm, hedge=hedge)
        monkeypatch.setattr(routes, "get_graph", lambda mock: graph)

    return _serve


@pytest.fixture(autouse=False)
def drain_slot():
    """app.state.drain を、テストの間だけ置く（置いたものは必ず外す）。"""
    yield
    app.state.drain = None


def test_drain_ends_a_running_stream_with_a_fixed_message_and_cancels_the_llm(serve, drain_slot):
    llm = StreamLLM(plans=[[("sleep", HANG), ("text", LONG)]])
    serve(llm)

    async def scenario() -> StreamConnection:
        drain = Drain()
        app.state.drain = drain
        conn = StreamConnection({"message": "こんにちは", "thread_id": "drain-1"})
        task = start(conn)
        await until(lambda: llm.started == 3)
        started = time.monotonic()
        drain.set()
        await asyncio.wait_for(task, 5)
        assert time.monotonic() - started < 1.0  # 30 秒待たない
        assert leftover_tasks() == []
        return conn

    conn = asyncio.run(scenario())
    assert (llm.started, llm.cancelled) == (3, 3)
    body = conn.body()
    assert conn.status == 200 and "[DONE]" in body
    assert routes.DRAIN_MESSAGE in body and '"status":503' in body.replace(" ", "")
    assert "retryAfterSeconds" in body


def test_drain_ends_a_running_chat_with_503_and_retry_after(monkeypatch, drain_slot):
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    llm = SlowLLM(delays=[HANG])
    graph = build_graph(llm, hedge=FAST)
    monkeypatch.setattr(routes, "get_graph", lambda mock: graph)

    async def scenario() -> Connection:
        drain = Drain()
        app.state.drain = drain
        conn = Connection({"message": "こんにちは", "thread_id": "drain-2"})
        task = start(conn)
        await until(lambda: llm.calls >= 1)
        started = time.monotonic()
        drain.set()
        await asyncio.wait_for(task, 5)
        assert time.monotonic() - started < 1.0
        assert leftover_tasks() == []
        return conn

    conn = asyncio.run(scenario())
    assert llm.cancelled == llm.calls >= 1
    assert conn.status == 503
    assert conn.json() == {"detail": routes.DRAIN_MESSAGE}
    headers = dict(next(m for m in conn.sent if m["type"] == "http.response.start")["headers"])
    assert headers[b"retry-after"] == str(routes.DRAIN_RETRY_AFTER_SECONDS).encode()


def test_without_a_drain_signal_nothing_changes(serve):
    llm = StreamLLM(plans=[[("text", LONG[:30]), ("text", LONG[30:])]])
    serve(llm, hedge=None)

    async def scenario() -> StreamConnection:
        assert getattr(app.state, "drain", None) is None
        conn = StreamConnection({"message": "こんにちは", "thread_id": "drain-3"})
        await asyncio.wait_for(start(conn), 5)
        assert leftover_tasks() == []
        return conn

    conn = asyncio.run(scenario())
    assert "[DONE]" in conn.body() and routes.DRAIN_MESSAGE not in conn.body()
