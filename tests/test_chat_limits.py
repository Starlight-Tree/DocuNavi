"""待ち時間・再検索回数の上限と、切断時の停止（direct の検索先行の形）。実 API・DB は使わない。

- 1 回の質問で検索できる回数に上限がある（モデルがツールを呼び続けても、2 回で打ち切る。LangGraph の再帰の上限は 10007）
- 最初のチャンクまでの総時間に上限がある（主モデルが黙り続けても、上限で試行を全て取り消して error にする）
- 主モデルの 503 は、主モデルを投げ直さず、すぐフォールバックを投げる（HTTP 経由）
- 切断したら、検索も LLM の試行（ヘッジを含む）も取り消し、タスクを取り残さない
"""

import asyncio
import time
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from test_chat_disconnect import HANG, leftover_tasks, until
from test_chat_stream import LONG, StreamLLM, parse_sse
from test_chat_stream_disconnect import StreamConnection, start
from test_llm_direct_retrieval import RecordingLLM, ask

from app.agents import tools
from app.agents.graph import SEARCH_LIMIT_REPLY, build_graph, max_searches_per_turn
from app.agents.resilience import HedgePolicy
from app.api import routes
from tests.fakes import FakeSearch


class Unavailable(Exception):
    code = 503


class AlwaysSearch(RecordingLLM):
    """どの回も、検索を求める（text があれば本文も付ける）。"""

    text: str = ""

    async def _agenerate(self, messages: list[Any], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        call = {"name": "search_documents", "args": {"query": "別の語"}, "id": f"c{len(self.calls)}"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.text, tool_calls=[call]))])


@pytest.fixture
def searches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def spy(query: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(query)
        return await FakeSearch()(query, *args, **kwargs)

    monkeypatch.setattr(tools, "search_chunks_async", spy)
    return seen


@pytest.mark.parametrize("retrieval", ["direct", "agent"])
def test_a_model_that_keeps_searching_is_cut_at_the_search_limit(searches: list[str], retrieval: str) -> None:
    llm = AlwaysSearch(calls=[])

    final, _ = ask(build_graph(llm, retrieval=retrieval), "ザーハードの大気について")

    assert len(searches) == max_searches_per_turn() == 2
    assert len(llm.calls) == (
        2 if retrieval == "direct" else 3
    )  # direct は最初の検索の分だけ、LLM の呼び出しが 1 回少ない
    last = final["messages"][-1]
    assert isinstance(last, AIMessage) and not last.tool_calls and last.content == SEARCH_LIMIT_REPLY
    # 結果の無い検索の呼び出しを、履歴に残さない（検索の結果は呼び出しの数だけある）
    calls = sum(len(m.tool_calls) for m in final["messages"] if isinstance(m, AIMessage))
    assert calls == sum(isinstance(m, ToolMessage) for m in final["messages"]) == 2


def test_the_text_the_model_attached_is_kept_when_the_search_is_cut(searches: list[str]) -> None:
    llm = AlwaysSearch(calls=[], text="資料には記載がありません。")

    final, _ = ask(build_graph(llm, retrieval="direct"), "ザーハードの大気について")

    assert final["messages"][-1].content == "資料には記載がありません。"


@pytest.mark.parametrize(("value", "expected"), [(None, 2), ("", 2), ("1", 1), ("3", 3), ("0", 2), ("-1", 2), ("x", 2)])
def test_search_limit_from_the_environment(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int) -> None:
    if value is None:
        monkeypatch.delenv("LLM_MAX_SEARCHES", raising=False)
    else:
        monkeypatch.setenv("LLM_MAX_SEARCHES", value)
    assert max_searches_per_turn() == expected


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)

    def _serve(llm: Any, hedge: HedgePolicy, **kwargs: Any) -> None:
        graph = build_graph(llm, hedge=hedge, retrieval="direct", **kwargs)
        monkeypatch.setattr(routes, "get_graph", lambda mock: graph)

    return _serve


def test_a_silent_primary_is_cut_at_the_first_chunk_deadline(serve: Any) -> None:
    llm = StreamLLM(plans=[[("sleep", HANG), ("text", LONG)]])
    serve(llm, HedgePolicy(hedge_delay=0.05, max_attempts=3, retry_delay=0.01, deadline=0.4))

    async def scenario() -> tuple[StreamConnection, float]:
        conn = StreamConnection({"message": "ザーハードの大気について", "thread_id": "limit-1"})
        t = time.monotonic()
        await asyncio.wait_for(start(conn), 5)
        elapsed = time.monotonic() - t
        assert leftover_tasks() == []
        return conn, elapsed

    conn, elapsed = asyncio.run(scenario())

    assert 0.35 <= elapsed < 1.5  # 30 秒（HANG）も、60 秒（LLM_TIMEOUT_SECONDS）も待たない
    assert (llm.started, llm.cancelled) == (3, 3)  # 投げた試行は全て取り消す
    chunks, _, _ = parse_sse(conn.body())
    kinds = [c.get("type") for c in chunks]
    assert "error" in kinds and "finish" not in kinds and "text-delta" not in kinds


def test_a_503_goes_to_the_fallback_without_retrying_the_primary(serve: Any) -> None:
    primary = StreamLLM(plans=[[("raise", Unavailable())]])
    fallback = StreamLLM(plans=[[("text", "フォールバックの回答です。")]])
    serve(
        primary,
        HedgePolicy(hedge_delay=5.0, max_attempts=3, retry_delay=0.5, early_fallback=True, deadline=20.0),
        fallback_llm=fallback,
    )

    async def scenario() -> tuple[StreamConnection, float]:
        conn = StreamConnection({"message": "ザーハードの大気について", "thread_id": "limit-2"})
        t = time.monotonic()
        await asyncio.wait_for(start(conn), 5)
        return conn, time.monotonic() - t

    conn, elapsed = asyncio.run(scenario())

    assert (primary.started, fallback.started) == (1, 1)  # 主モデルは投げ直さない
    assert elapsed < 0.4  # retry_delay（0.5 秒）も hedge_delay（5 秒）も待たない
    chunks, done, _ = parse_sse(conn.body())
    assert (
        done
        and "".join(c.get("delta", "") for c in chunks if c.get("type") == "text-delta") == "フォールバックの回答です。"
    )


def test_disconnect_during_the_search_cancels_it_and_never_calls_the_llm(
    serve: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = StreamLLM(plans=[[("text", "届かない")]])
    serve(llm, HedgePolicy(hedge_delay=0.02, max_attempts=3, retry_delay=0.01))
    state = {"started": 0, "cancelled": 0}

    async def slow_search(*args: Any, **kwargs: Any) -> list:
        state["started"] += 1
        try:
            await asyncio.sleep(HANG)
        except asyncio.CancelledError:
            state["cancelled"] += 1
            raise
        return []

    monkeypatch.setattr(tools, "search_chunks_async", slow_search)

    async def scenario() -> None:
        conn = StreamConnection({"message": "ザーハードの大気について", "thread_id": "limit-3"})
        task = start(conn)
        await until(lambda: state["started"] == 1)
        t = time.monotonic()
        conn.disconnect()
        await asyncio.wait_for(task, 5)
        assert time.monotonic() - t < 1.0
        assert leftover_tasks() == []

    asyncio.run(scenario())

    assert state == {"started": 1, "cancelled": 1}
    assert llm.started == 0  # 検索の途中で切れたので、LLM は呼ばれない


def test_disconnect_while_waiting_for_the_llm_cancels_every_attempt_after_one_search(
    serve: Any, searches: list[str]
) -> None:
    llm = StreamLLM(plans=[[("sleep", HANG), ("text", LONG)]])
    serve(llm, HedgePolicy(hedge_delay=0.02, max_attempts=3, retry_delay=0.01))

    async def scenario() -> None:
        conn = StreamConnection({"message": "ザーハードの大気について", "thread_id": "limit-4"})
        task = start(conn)
        await until(lambda: llm.started == 3)
        conn.disconnect()
        await asyncio.wait_for(task, 5)
        assert leftover_tasks() == []

    asyncio.run(scenario())

    assert (llm.started, llm.cancelled) == (3, 3)
    assert len(searches) == 1  # 検索は 1 回だけ。切断のあとに追加の検索・LLM 呼び出しは起きない
