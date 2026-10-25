"""クライアントの切断で /chat/stream の LLM 呼び出し（ヘッジの追加試行を含む）が取り消されること。

tests/test_chat_disconnect.py と同じ手書きの ASGI ハーネス（uvicorn と同じ約束で、ASGI spec 2.3。
Starlette の StreamingResponse は、切断を見つけると本体の生成を取り消す）で、サーバーは起動しない。
"""

import asyncio
import logging
import time

import pytest
from test_chat_disconnect import FAST, HANG, Connection, leftover_tasks, until
from test_chat_stream import LONG, StreamLLM, parse_sse

from app.agents.graph import build_graph
from app.api import routes
from app.main import app


class StreamConnection(Connection):
    @property
    def scope(self) -> dict:
        scope = super().scope
        scope["path"] = "/chat/stream"
        scope["raw_path"] = b"/chat/stream"
        return scope

    def body(self) -> str:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body").decode()


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)

    def _serve(llm: StreamLLM, hedge=FAST):
        graph = build_graph(llm, hedge=hedge)
        monkeypatch.setattr(routes, "get_graph", lambda mock: graph)
        return graph

    return _serve


def start(conn: Connection) -> "asyncio.Task[None]":
    return asyncio.ensure_future(app(conn.scope, conn.receive, conn.send))


def streamed_text(conn: StreamConnection) -> bool:
    return any(m["type"] == "http.response.body" and b"text-delta" in m.get("body", b"") for m in conn.sent)


def test_disconnect_before_the_first_text_cancels_every_hedged_attempt(serve, caplog):
    llm = StreamLLM(plans=[[("sleep", HANG), ("text", LONG)]])
    serve(llm)
    secret = "SECRET-MESSAGE-xyz"

    async def scenario() -> StreamConnection:
        conn = StreamConnection({"message": secret, "thread_id": "gone-1"})
        task = start(conn)
        await until(lambda: llm.started == 3)  # 3 本とも飛んでいる
        started = time.monotonic()
        conn.disconnect()
        await asyncio.wait_for(task, 5)  # 30 秒待たずに、例外も出さずに戻る
        assert time.monotonic() - started < 1.0
        assert leftover_tasks() == []  # 試行も、グラフの実行も、切断の監視も取り残さない
        return conn

    with caplog.at_level(logging.INFO, logger="app.api.routes"):
        conn = asyncio.run(scenario())

    assert (llm.started, llm.cancelled) == (3, 3)
    # レスポンスは最初の本文を待たずに始まる（進み具合を見せるため）ので、start と status までは届いている。本文は届いていない
    assert not streamed_text(conn)
    assert not any(b"finish" in m.get("body", b"") for m in conn.sent if m["type"] == "http.response.body")
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert all(r.exc_info is None for r in caplog.records)
    lines = [r for r in caplog.records if r.name == "app.api.routes"]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    # レスポンスを始めた後なので、本体の取り消し（ストリームを中断し…）か、切断の検知（クライアントが切断したため…）のどちらか 1 行
    message = lines[0].getMessage()
    assert "gone-1" in message and ("切断" in message or "中断" in message)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in logged and "127.0.0.1" not in logged


def test_disconnect_while_streaming_cancels_the_llm_call(serve, caplog):
    llm = StreamLLM(plans=[[("text", LONG), ("sleep", HANG), ("text", "届かない")]])
    serve(llm)

    async def scenario() -> StreamConnection:
        conn = StreamConnection({"message": "こんにちは", "thread_id": "gone-2"})
        task = start(conn)
        await until(lambda: streamed_text(conn))  # 本文が届き始めた
        conn.disconnect()
        await asyncio.wait_for(task, 5)
        assert leftover_tasks() == []
        return conn

    with caplog.at_level(logging.INFO, logger="app.api.routes"):
        conn = asyncio.run(scenario())

    assert (llm.started, llm.cancelled, llm.closed) == (1, 1, 1)
    assert conn.status == 200
    assert "[DONE]" not in conn.body()  # 最後まで届いていない
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert all(r.exc_info is None for r in caplog.records)
    lines = [r for r in caplog.records if r.name == "app.api.routes"]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert "gone-2" in lines[0].getMessage()


def test_disconnect_before_the_llm_starts_leaves_nothing_behind(serve):
    llm = StreamLLM(plans=[[("sleep", HANG), ("text", LONG)]])
    serve(llm)

    async def scenario() -> None:
        conn = StreamConnection()
        conn.disconnect()  # 本文を読んだ直後に、すでに切れている
        await asyncio.wait_for(start(conn), 5)
        assert leftover_tasks() == []

    asyncio.run(scenario())
    assert llm.cancelled == llm.started


def test_a_normal_stream_completes_and_leaves_no_tasks(serve, caplog):
    llm = StreamLLM(plans=[[("text", LONG[:30]), ("text", LONG[30:])]])
    serve(llm, hedge=None)

    async def scenario() -> StreamConnection:
        conn = StreamConnection({"message": "こんにちは", "thread_id": "ok-1"})
        await asyncio.wait_for(start(conn), 5)
        assert leftover_tasks() == []  # 切断の監視を取り残さない
        return conn

    with caplog.at_level(logging.INFO, logger="app.api.routes"):
        conn = asyncio.run(scenario())

    chunks, done, _ = parse_sse(conn.body())
    assert conn.status == 200 and done
    assert (llm.started, llm.cancelled, llm.closed) == (1, 0, 1)
    assert "".join(c["delta"] for c in chunks if c["type"] == "text-delta") == LONG
    assert not any("切断" in r.getMessage() for r in caplog.records)
