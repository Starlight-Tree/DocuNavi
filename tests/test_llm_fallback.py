"""主モデルの一時的な失敗が続いたときだけ、別のモデルで 1 回試す（GEMINI_FALLBACK_MODEL）。

実際の API は呼ばない。判定（どの失敗で切り替えるか）・ストリーミングでの切り替え位置・グラフへの組み込み・
環境変数からの組み立てを、偽の LLM とストリームで確かめる。
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agents import graph as graph_module
from app.agents.graph import (
    STREAM_TOKENS_KEY,
    MockChatModel,
    build_graph,
    create_fallback_llm,
    llm_fallback_hold_seconds,
    llm_fallback_model,
)
from app.agents.resilience import (
    FallbackTrace,
    HedgePolicy,
    PrimaryBreaker,
    call_with_fallback,
    hedged_call,
    hedged_stream,
    stream_with_fallback,
)

FAST = HedgePolicy(hedge_delay=0.05, max_attempts=3, retry_delay=0.01, rate_limit_delay=0.02)


class Unavailable(Exception):
    """503 のように、投げ直せば通りうる失敗。"""

    code = 503


class RateLimited(Exception):
    code = 429


class NotFound(Exception):
    """モデルが使えない・設定が悪い失敗（404）。投げ直しても、別モデルでも解決しない。"""

    code = 404


class Rejected(Exception):
    """リクエストの内容が悪い失敗（コードなし。投げ直しても同じ）。"""


# --- call_with_fallback --------------------------------------------------------


class Calls:
    """呼ばれた回数を数え、決められた結果（値または例外）を返す偽の呼び出し。"""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.count = 0

    async def __call__(self) -> Any:
        self.count += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def call(primary: Calls, fallback: Calls) -> Any:
    return asyncio.run(call_with_fallback(primary, fallback, name="Gemini", fallback_name="別モデル"))


def test_primary_success_never_touches_the_fallback():
    primary, fallback = Calls("primary"), Calls("fallback")
    assert call(primary, fallback) == "primary"
    assert (primary.count, fallback.count) == (1, 0)


def test_transient_failure_falls_back_exactly_once(caplog):
    primary, fallback = Calls(Unavailable("503")), Calls("fallback")
    with caplog.at_level(logging.INFO, logger="app.agents.resilience"):
        assert call(primary, fallback) == "fallback"
    assert (primary.count, fallback.count) == (1, 1)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "別モデル" in logged and "Unavailable" in logged


@pytest.mark.parametrize("exc", [NotFound("404"), Rejected("400"), PermissionError("403")])
def test_non_transient_failures_do_not_fall_back(exc):
    primary, fallback = Calls(exc), Calls("fallback")
    with pytest.raises(type(exc)):
        call(primary, fallback)
    assert fallback.count == 0


def test_rate_limit_does_not_fall_back():
    primary, fallback = Calls(RateLimited("429")), Calls("fallback")
    with pytest.raises(RateLimited):
        call(primary, fallback)
    assert fallback.count == 0


def test_when_the_fallback_fails_too_the_original_failure_is_raised():
    original = Unavailable("primary down")
    primary, fallback = Calls(original), Calls(NotFound("no longer available to new users"))

    with pytest.raises(Unavailable) as info:
        call(primary, fallback)

    assert info.value is original  # 呼び出し側（API のエラー変換）には、これまでと同じ失敗が見える
    assert fallback.count == 1


def test_cancellation_while_on_the_fallback_is_not_swallowed():
    async def scenario() -> bool:
        entered = asyncio.Event()
        cancelled = False

        async def primary() -> Any:
            raise Unavailable("503")

        async def fallback() -> Any:
            nonlocal cancelled
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled = True
                raise

        task = asyncio.ensure_future(call_with_fallback(primary, fallback, name="Gemini"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return cancelled

    assert asyncio.run(scenario()) is True


# --- stream_with_fallback -------------------------------------------------------


class Streams:
    """呼ばれるたびに 1 本のストリームを返す偽の生成関数。steps は ("yield", 値) / ("raise", 例外)。"""

    def __init__(self, *steps: tuple[str, Any]) -> None:
        self.steps = steps
        self.started = 0
        self.closed = 0

    def __call__(self) -> AsyncIterator[Any]:
        self.started += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        try:
            for kind, value in self.steps:
                if kind == "yield":
                    yield value
                else:
                    raise value
        finally:
            self.closed += 1


async def _collect(primary: Streams, fallback: Streams) -> list[Any]:
    return [item async for item in stream_with_fallback(primary, fallback, name="Gemini", fallback_name="別モデル")]


def collect(primary: Streams, fallback: Streams) -> list[Any]:
    return asyncio.run(_collect(primary, fallback))


def test_stream_primary_success_never_touches_the_fallback():
    primary, fallback = Streams(("yield", "a"), ("yield", "b")), Streams(("yield", "x"))
    assert collect(primary, fallback) == ["a", "b"]
    assert (primary.started, fallback.started) == (1, 0)


def test_stream_failure_before_the_first_chunk_falls_back():
    primary = Streams(("raise", Unavailable("503")))
    fallback = Streams(("yield", "x"), ("yield", "y"))
    assert collect(primary, fallback) == ["x", "y"]
    assert (primary.started, fallback.started) == (1, 1)
    assert primary.closed == 1 and fallback.closed == 1


def test_stream_that_ends_without_a_chunk_falls_back():
    primary, fallback = Streams(), Streams(("yield", "x"))
    assert collect(primary, fallback) == ["x"]


def test_stream_failure_after_the_first_chunk_is_not_switched():
    # 送った本文と食い違うので、途中の失敗は別モデルに切り替えず、そのまま伝える
    primary = Streams(("yield", "a"), ("raise", Unavailable("503")))
    fallback = Streams(("yield", "x"))
    seen: list[Any] = []

    async def scenario() -> None:
        async for item in stream_with_fallback(primary, fallback, name="Gemini"):
            seen.append(item)

    with pytest.raises(Unavailable):
        asyncio.run(scenario())
    assert seen == ["a"]
    assert fallback.started == 0


@pytest.mark.parametrize("exc", [NotFound("404"), Rejected("400"), RateLimited("429")])
def test_stream_non_transient_failures_do_not_fall_back(exc):
    primary, fallback = Streams(("raise", exc)), Streams(("yield", "x"))
    with pytest.raises(type(exc)):
        collect(primary, fallback)
    assert fallback.started == 0


def test_stream_when_the_fallback_fails_before_a_chunk_the_original_failure_is_raised():
    original = Unavailable("primary down")
    primary, fallback = Streams(("raise", original)), Streams(("raise", NotFound("404")))

    with pytest.raises(Unavailable) as info:
        collect(primary, fallback)

    assert info.value is original


def test_stream_closes_the_underlying_stream_when_the_consumer_stops():
    primary, fallback = Streams(("yield", "a"), ("yield", "b"), ("yield", "c")), Streams(("yield", "x"))

    async def scenario() -> list[Any]:
        out: list[Any] = []
        gen = stream_with_fallback(primary, fallback, name="Gemini")
        async for item in gen:
            out.append(item)
            break
        await gen.aclose()
        return out

    assert asyncio.run(scenario()) == ["a"]
    assert primary.closed == 1


# --- グラフへの組み込み ----------------------------------------------------------


class PlanLLM(BaseChatModel):
    """n 回目の呼び出しの振る舞いを plans[n] で決める偽の LLM。

    手順は ("text", 断片) / ("tool", ツール名, 引数) / ("raise", 例外) / ("sleep", 秒)。
    """

    plans: list[list[tuple]]
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "plan-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "PlanLLM":
        return self

    def _next_plan(self) -> list[tuple]:
        index = self.calls
        self.calls += 1
        return self.plans[min(index, len(self.plans) - 1)]

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        text = ""
        tool_calls: list[dict[str, Any]] = []
        for step in self._next_plan():
            if step[0] == "sleep":
                await asyncio.sleep(step[1])
                continue
            if step[0] == "raise":
                raise step[1]
            if step[0] == "tool":
                tool_calls.append({"name": step[1], "args": step[2], "id": f"call-{self.calls}-{len(tool_calls)}"})
            else:
                text += step[1]
        message = AIMessage(content=text, tool_calls=tool_calls)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        for step in self._next_plan():
            if step[0] == "sleep":
                await asyncio.sleep(step[1])
                continue
            if step[0] == "raise":
                raise step[1]
            if step[0] == "tool":
                call = {"name": step[1], "args": json.dumps(step[2]), "id": f"call-{self.calls}", "index": 0}
                yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[call]))
            else:
                yield ChatGenerationChunk(message=AIMessageChunk(content=step[1]))


DOWN = [("raise", Unavailable("503 UNAVAILABLE"))]
ANSWER = [("text", "フォールバックのモデルからの回答です。")]
PRIMARY_ANSWER = [("text", "主モデルからの回答です。")]


def ask(graph: Any, *, stream: bool = False) -> dict:
    configurable: dict[str, Any] = {"thread_id": "fallback-test"}
    if stream:
        configurable[STREAM_TOKENS_KEY] = True
    state = {"messages": [HumanMessage(content="こんにちは")], "context": ""}

    async def run() -> dict:
        if not stream:
            return await graph.ainvoke(state, {"configurable": configurable})
        final: dict = {}
        async for mode, payload in graph.astream(
            state, {"configurable": configurable}, stream_mode=["custom", "values"]
        ):
            if mode == "values":
                final = payload
        return final

    return asyncio.run(run())


def test_graph_without_a_fallback_keeps_failing_like_before():
    primary = PlanLLM(plans=[DOWN])
    with pytest.raises(Unavailable):
        ask(build_graph(primary, hedge=FAST))
    assert primary.calls == FAST.max_attempts


def test_graph_uses_the_fallback_after_all_primary_attempts_fail():
    primary, fallback = PlanLLM(plans=[DOWN]), PlanLLM(plans=[ANSWER])

    result = ask(build_graph(primary, hedge=FAST, fallback_llm=fallback))

    assert result["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert primary.calls == FAST.max_attempts  # 主モデルの試行は使い切ってから
    assert fallback.calls == 1  # フォールバックは 1 回だけ


def test_graph_does_not_call_the_fallback_when_the_primary_recovers():
    primary = PlanLLM(plans=[DOWN, PRIMARY_ANSWER])
    fallback = PlanLLM(plans=[ANSWER])

    result = ask(build_graph(primary, hedge=FAST, fallback_llm=fallback))

    assert result["messages"][-1].content == "主モデルからの回答です。"
    assert fallback.calls == 0


def test_graph_falls_back_without_a_hedge_policy_too():
    primary, fallback = PlanLLM(plans=[DOWN]), PlanLLM(plans=[ANSWER])

    result = ask(build_graph(primary, fallback_llm=fallback))

    assert result["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert (primary.calls, fallback.calls) == (1, 1)


def test_graph_does_not_fall_back_on_a_configuration_failure():
    primary, fallback = PlanLLM(plans=[[("raise", NotFound("404"))]]), PlanLLM(plans=[ANSWER])

    with pytest.raises(NotFound):
        ask(build_graph(primary, hedge=FAST, fallback_llm=fallback))
    assert (primary.calls, fallback.calls) == (1, 0)


def test_streaming_graph_falls_back_before_the_first_chunk():
    primary, fallback = PlanLLM(plans=[DOWN]), PlanLLM(plans=[ANSWER])

    result = ask(build_graph(primary, hedge=FAST, fallback_llm=fallback), stream=True)

    assert result["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert primary.calls == FAST.max_attempts
    assert fallback.calls == 1


def test_streaming_graph_without_a_hedge_policy_falls_back_too():
    primary, fallback = PlanLLM(plans=[DOWN]), PlanLLM(plans=[ANSWER])

    result = ask(build_graph(primary, fallback_llm=fallback), stream=True)

    assert result["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert (primary.calls, fallback.calls) == (1, 1)


def test_streaming_graph_does_not_switch_after_the_first_chunk():
    # 先頭の断片を送ったあとの失敗は、別モデルで書き直さない（送った本文と食い違うため）
    primary = PlanLLM(plans=[[("text", "主モデルの書き出し。"), ("raise", Unavailable("503"))]])
    fallback = PlanLLM(plans=[ANSWER])

    with pytest.raises(Unavailable):
        ask(build_graph(primary, fallback_llm=fallback), stream=True)
    assert fallback.calls == 0


# --- 主モデルを飛ばすブレーカー（PrimaryBreaker）--------------------------------------


class Clock:
    """進めた分だけ進む時計（実際には待たない）。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Sequence:
    """n 回目の呼び出しが返す結果（値または例外）を順に決める。最後の結果は繰り返す。"""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = outcomes
        self.count = 0

    async def __call__(self) -> Any:
        outcome = self.outcomes[min(self.count, len(self.outcomes) - 1)]
        self.count += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_breaker_is_closed_until_opened_and_reopens_after_the_hold_time():
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    assert breaker.route() == "primary"

    breaker.opened()
    assert breaker.route() == "fallback"
    clock.now += 59.9
    assert breaker.route() == "fallback"
    clock.now += 0.2
    assert breaker.route() == "probe"  # 満了後の最初の 1 件だけが主モデルを試す
    assert breaker.route() == "fallback"  # 試している間は、ほかを飛ばしたまま
    breaker.primary_ok()
    assert breaker.route() == "primary"


def test_breaker_opens_again_when_the_probe_fails_and_closes_when_the_fallback_fails():
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    breaker.opened()
    clock.now += 61
    assert breaker.route() == "probe"
    breaker.opened()  # まだ失敗 → 満了を今から数え直す
    assert breaker.route() == "fallback"
    clock.now += 59
    assert breaker.route() == "fallback"

    clock.now += 2
    assert breaker.route() == "probe"
    breaker.fallback_failed()  # フォールバックも失敗 → 飛ばし続けない
    assert breaker.route() == "primary"


def test_breaker_lets_the_next_call_probe_when_a_probe_ends_without_a_result():
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    breaker.opened()
    clock.now += 61
    assert breaker.route() == "probe"
    breaker.end_probe()  # 取り消し・設定の失敗など
    assert breaker.route() == "probe"


@pytest.mark.parametrize("hold", [0, -5])
def test_breaker_with_no_hold_time_never_opens(hold):
    breaker = PrimaryBreaker(hold, clock=Clock())
    breaker.opened()
    assert breaker.route() == "primary"


def test_open_breaker_skips_the_primary_on_the_next_call():
    breaker = PrimaryBreaker(60, clock=Clock())
    primary, fallback = Sequence(Unavailable("503")), Sequence("F")

    async def go() -> list[Any]:
        return [await call_with_fallback(primary, fallback, breaker=breaker) for _ in range(3)]

    assert asyncio.run(go()) == ["F", "F", "F"]
    assert primary.count == 1  # 最初の 1 回だけ。後の 2 回は主モデルを待たない
    assert fallback.count == 3


def test_probe_that_succeeds_closes_the_breaker():
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    primary, fallback = Sequence(Unavailable("503"), "P"), Sequence("F")

    async def go() -> list[Any]:
        results = [await call_with_fallback(primary, fallback, breaker=breaker)]
        clock.now += 61
        results.append(await call_with_fallback(primary, fallback, breaker=breaker))  # 試す 1 件
        results.append(await call_with_fallback(primary, fallback, breaker=breaker))  # 通常に戻る
        return results

    assert asyncio.run(go()) == ["F", "P", "P"]
    assert (primary.count, fallback.count) == (3, 1)


def test_probe_that_fails_opens_the_breaker_again():
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    primary, fallback = Sequence(Unavailable("503")), Sequence("F")

    async def go() -> None:
        await call_with_fallback(primary, fallback, breaker=breaker)
        clock.now += 61
        await call_with_fallback(primary, fallback, breaker=breaker)  # 試す 1 件: まだ落ちている
        await call_with_fallback(primary, fallback, breaker=breaker)  # また飛ばす

    asyncio.run(go())
    assert primary.count == 2
    assert fallback.count == 3


def test_calls_during_a_probe_go_straight_to_the_fallback():
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    breaker.opened()
    clock.now += 61

    async def go() -> tuple[Any, Any]:
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_primary() -> str:
            started.set()
            await release.wait()
            return "P"

        probe = asyncio.ensure_future(call_with_fallback(slow_primary, Sequence("F"), breaker=breaker))
        await started.wait()
        other = await call_with_fallback(Sequence("P2"), Sequence("F"), breaker=breaker)
        release.set()
        return await probe, other

    assert asyncio.run(go()) == ("P", "F")


def test_cancelled_probe_does_not_leave_the_breaker_stuck():
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    breaker.opened()
    clock.now += 61

    async def go() -> None:
        started = asyncio.Event()

        async def hang() -> str:
            started.set()
            await asyncio.Event().wait()
            return "never"

        task = asyncio.ensure_future(call_with_fallback(hang, Sequence("F"), breaker=breaker))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert breaker.route() == "probe"  # 次の呼び出しが、また試せる


def test_failure_that_is_not_transient_does_not_open_the_breaker():
    breaker = PrimaryBreaker(60, clock=Clock())
    for failure in (RateLimited("429"), NotFound("404"), Rejected("400")):
        with pytest.raises(type(failure)):
            asyncio.run(call_with_fallback(Sequence(failure), Sequence("F"), breaker=breaker))
        assert breaker.route() == "primary"


def test_fallback_that_fails_while_the_breaker_is_open_returns_to_the_primary():
    breaker = PrimaryBreaker(60, clock=Clock())
    breaker.opened()
    primary, fallback = Sequence("P"), Sequence(Unavailable("503 fallback"))

    result = asyncio.run(call_with_fallback(primary, fallback, breaker=breaker))

    assert result == "P"
    assert (primary.count, fallback.count) == (1, 1)
    assert breaker.route() == "primary"  # 落ちたフォールバックを頼り続けない


def test_both_failing_while_open_raises_the_primary_failure():
    breaker = PrimaryBreaker(60, clock=Clock())
    breaker.opened()
    primary_failure = Unavailable("503 primary")
    primary, fallback = Sequence(primary_failure), Sequence(Unavailable("503 fallback"))

    with pytest.raises(Unavailable) as raised:
        asyncio.run(call_with_fallback(primary, fallback, breaker=breaker))

    assert raised.value is primary_failure
    assert (primary.count, fallback.count) == (1, 1)  # フォールバックを 2 回試さない


def test_prefer_fallback_starts_with_the_fallback_without_a_breaker():
    primary, fallback = Sequence("P"), Sequence("F")
    assert asyncio.run(call_with_fallback(primary, fallback, prefer_fallback=True)) == "F"
    assert (primary.count, fallback.count) == (0, 1)


def test_on_fallback_is_called_only_when_the_fallback_answered():
    served: list[str] = []

    asyncio.run(call_with_fallback(Sequence("P"), Sequence("F"), on_fallback=lambda: served.append("a")))
    assert served == []

    asyncio.run(call_with_fallback(Sequence(Unavailable("503")), Sequence("F"), on_fallback=lambda: served.append("b")))
    asyncio.run(call_with_fallback(Sequence("P"), Sequence("F"), prefer_fallback=True, on_fallback=lambda: served.append("c")))
    assert served == ["b", "c"]


async def _collect_with(primary: Streams, fallback: Streams, **kwargs: Any) -> list[Any]:
    return [
        item
        async for item in stream_with_fallback(primary, fallback, name="Gemini", fallback_name="別モデル", **kwargs)
    ]


def test_stream_open_breaker_skips_the_primary_stream():
    breaker = PrimaryBreaker(60, clock=Clock())
    primary = Streams(("raise", Unavailable("503")))
    fallback = Streams(("yield", "F1"), ("yield", "F2"))

    async def go() -> list[Any]:
        first = await _collect_with(primary, fallback, breaker=breaker)
        second = await _collect_with(primary, fallback, breaker=breaker)
        return first + second

    assert asyncio.run(go()) == ["F1", "F2", "F1", "F2"]
    assert primary.started == 1  # 2 回目の質問は、主モデルを待たない


def test_stream_first_primary_chunk_closes_the_breaker_even_if_the_consumer_stops():
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    breaker.opened()
    clock.now += 61

    async def go() -> None:
        stream = stream_with_fallback(Streams(("yield", "P1"), ("yield", "P2")), Streams(("yield", "F")), breaker=breaker)
        async with aclosing(stream) as items:
            async for _ in items:
                break  # 最初のチャンクだけ受けて、やめる

    asyncio.run(go())
    assert breaker.route() == "primary"


def test_stream_fallback_that_fails_before_a_chunk_returns_to_the_primary():
    breaker = PrimaryBreaker(60, clock=Clock())
    breaker.opened()
    primary = Streams(("yield", "P"))
    fallback = Streams(("raise", Unavailable("503 fallback")))

    assert asyncio.run(_collect_with(primary, fallback, breaker=breaker)) == ["P"]
    assert breaker.route() == "primary"


def test_stream_fallback_that_fails_after_a_chunk_is_not_replaced_by_the_primary():
    breaker = PrimaryBreaker(60, clock=Clock())
    breaker.opened()
    primary = Streams(("yield", "P"))
    fallback = Streams(("yield", "F1"), ("raise", Unavailable("503 mid-stream")))

    with pytest.raises(Unavailable):
        asyncio.run(_collect_with(primary, fallback, breaker=breaker))
    assert primary.started == 0


def test_stream_prefer_fallback_and_on_fallback():
    served: list[str] = []
    primary, fallback = Streams(("yield", "P")), Streams(("yield", "F"))

    items = asyncio.run(_collect_with(primary, fallback, prefer_fallback=True, on_fallback=lambda: served.append("x")))

    assert items == ["F"]
    assert primary.started == 0
    assert served == ["x"]


# --- 同じ質問の中で、フォールバックを覚える（グラフ）---------------------------------

SEARCH = ("tool", "search_documents", {"query": "大気"})


@pytest.mark.parametrize("stream", [False, True], ids=["invoke", "stream"])
def test_later_calls_in_the_same_turn_do_not_wait_for_the_primary_again(stream):
    # 1 回目（検索の判断）でフォールバックに切り替わったら、2 回目（回答）は主モデルを試さない
    primary = PlanLLM(plans=[DOWN])
    fallback = PlanLLM(plans=[[SEARCH], ANSWER])

    result = ask(build_graph(primary, hedge=FAST, fallback_llm=fallback), stream=stream)

    assert result["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert primary.calls == FAST.max_attempts  # 1 回目の分だけ（2 回目は 0 回）
    assert fallback.calls == 2


@pytest.mark.parametrize("stream", [False, True], ids=["invoke", "stream"])
def test_the_next_turn_tries_the_primary_again_without_a_breaker(stream):
    # 覚えるのは同じ質問の中だけ。次の質問は、主モデルから試す（復旧したら主モデルに戻る）
    primary = PlanLLM(plans=[DOWN, DOWN, DOWN, PRIMARY_ANSWER])
    fallback = PlanLLM(plans=[ANSWER])
    graph = build_graph(primary, hedge=FAST, fallback_llm=fallback)

    first = ask(graph, stream=stream)
    second = ask(graph, stream=stream)

    assert first["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert second["messages"][-1].content == "主モデルからの回答です。"
    assert primary.calls == FAST.max_attempts + 1
    assert fallback.calls == 1


@pytest.mark.parametrize("stream", [False, True], ids=["invoke", "stream"])
def test_breaker_skips_the_primary_for_the_next_turn_and_then_probes_it(stream):
    clock = Clock()
    breaker = PrimaryBreaker(60, clock=clock)
    primary = PlanLLM(plans=[DOWN, DOWN, DOWN, PRIMARY_ANSWER])
    fallback = PlanLLM(plans=[ANSWER])
    graph = build_graph(primary, hedge=FAST, fallback_llm=fallback, fallback_breaker=breaker)

    first = ask(graph, stream=stream)
    second = ask(graph, stream=stream)  # 60 秒以内: 主モデルを待たずに、フォールバックから
    assert second["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert primary.calls == FAST.max_attempts
    assert fallback.calls == 2

    clock.now += 61
    third = ask(graph, stream=stream)  # 満了後: 主モデルを 1 件だけ試す（戻っている）
    fourth = ask(graph, stream=stream)

    assert first["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert third["messages"][-1].content == "主モデルからの回答です。"
    assert fourth["messages"][-1].content == "主モデルからの回答です。"
    assert fallback.calls == 2


def test_graph_without_a_fallback_has_no_fallback_flag():
    result = ask(build_graph(PlanLLM(plans=[PRIMARY_ANSWER]), hedge=FAST))
    assert "fallback_used" not in result or result["fallback_used"] is False


def test_flag_marks_a_turn_that_the_fallback_answered():
    primary, fallback = PlanLLM(plans=[DOWN]), PlanLLM(plans=[ANSWER])
    result = ask(build_graph(primary, hedge=FAST, fallback_llm=fallback))
    assert result["fallback_used"] is True


# --- 最初のヘッジで、フォールバックを並行して投げる（クロスモデルヘッジ）--------------------

RACE = HedgePolicy(hedge_delay=0.05, max_attempts=3, retry_delay=0.01, rate_limit_delay=0.02)


class Hangs:
    """seconds 秒たってから value を返す（取り消されたら cancelled を数える）偽の試行。"""

    def __init__(self, seconds: float, value: Any = "P") -> None:
        self.seconds = seconds
        self.value = value
        self.count = 0
        self.cancelled = 0

    async def __call__(self) -> Any:
        self.count += 1
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


def delayed_sequence(steps: list[tuple[float, Any]]):
    """n 回目の呼び出しが (待ち秒, 結果) のとおりに動く偽の試行。"""
    state = {"n": 0}

    async def call() -> Any:
        delay, outcome = steps[min(state["n"], len(steps) - 1)]
        state["n"] += 1
        await asyncio.sleep(delay)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return call


def test_race_the_fallback_answers_when_the_primary_hangs():
    primary, fallback = Hangs(5.0), Sequence("F")
    trace = FallbackTrace()

    async def go() -> tuple[Any, float]:
        t = asyncio.get_running_loop().time()
        result = await hedged_call(primary, RACE, name="Gemini", fallback=fallback, trace=trace)
        return result, asyncio.get_running_loop().time() - t

    result, elapsed = asyncio.run(go())

    assert result == "F"
    assert 0.04 <= elapsed < 0.5  # ヘッジの遅延（0.05 秒）で投げた分だけ。主モデルの 5 秒は待たない
    assert (primary.count, fallback.count) == (1, 1)
    assert primary.cancelled == 1  # 負けた主モデルの試行は取り消す
    assert trace.tried and trace.won
    assert not trace.primary_failed  # エラーは出ていない（遅いだけ）


def test_race_is_not_started_when_the_primary_answers_in_time():
    primary, fallback = Hangs(0.0), Sequence("F")
    trace = FallbackTrace()

    assert asyncio.run(hedged_call(primary, RACE, name="Gemini", fallback=fallback, trace=trace)) == "P"
    assert fallback.count == 0
    assert not trace.tried and not trace.won


def test_race_primary_that_answers_before_the_fallback_wins():
    primary, fallback = Hangs(0.1, "P"), Hangs(5.0, "F")
    trace = FallbackTrace()

    result = asyncio.run(hedged_call(primary, RACE, name="Gemini", fallback=fallback, trace=trace))

    assert result == "P"
    assert fallback.cancelled == 1  # 負けたフォールバックの試行は取り消す
    assert trace.tried and not trace.won


def test_race_failure_of_the_fallback_is_ignored_whatever_its_kind():
    # 設定の失敗（404）・429 でも、主モデルの待ちを止めない。送出するのは主モデルの結果
    for failure in (NotFound("404"), RateLimited("429"), Unavailable("503")):
        primary, fallback = Hangs(0.15, "P"), Sequence(failure)
        trace = FallbackTrace()

        result = asyncio.run(hedged_call(primary, RACE, name="Gemini", fallback=fallback, trace=trace))

        assert result == "P"
        assert fallback.count == 1  # 1 回だけ
        assert trace.tried and not trace.won


def test_race_when_everything_fails_raises_the_primary_failure_and_tries_the_fallback_once():
    primary_failure = Unavailable("503 primary")
    primary, fallback = Hangs(0.2, primary_failure), Sequence(Unavailable("503 fallback"))
    trace = FallbackTrace()

    with pytest.raises(Unavailable) as raised:
        asyncio.run(hedged_call(primary, RACE, name="Gemini", fallback=fallback, trace=trace))

    assert raised.value is primary_failure
    assert fallback.count == 1
    assert primary.count == 2  # フォールバックが max_attempts の 1 本分を使う（3 本 = 主 2 + フォールバック 1）
    assert trace.primary_failed


def test_race_records_a_primary_failure_even_when_the_fallback_then_wins():
    # 1 本目は即座に 503、2 本目は無応答 → フォールバックが先に応答。主モデルは「遅い」だけでなく失敗もしている
    primary = delayed_sequence([(0.0, Unavailable("503")), (5.0, "P")])
    trace = FallbackTrace()

    result = asyncio.run(hedged_call(primary, RACE, name="Gemini", fallback=Sequence("F"), trace=trace))

    assert result == "F"
    assert trace.won and trace.primary_failed


def test_race_does_not_start_after_a_rate_limit_or_with_a_single_attempt():
    one = HedgePolicy(hedge_delay=0.05, max_attempts=1, retry_delay=0.01)
    primary, fallback = Hangs(0.15, "P"), Sequence("F")
    assert asyncio.run(hedged_call(primary, one, name="Gemini", fallback=fallback)) == "P"
    assert fallback.count == 0

    primary = delayed_sequence([(0.0, RateLimited("429")), (0.3, "P")])
    fallback = Sequence("F")
    assert asyncio.run(hedged_call(primary, RACE, name="Gemini", fallback=fallback)) == "P"
    assert fallback.count == 0  # 429 のあとは、時間経過による追加（フォールバックを含む）を止める


def test_race_is_not_started_again_when_the_fallback_was_already_tried():
    primary, fallback = Hangs(0.15, "P"), Sequence("F")
    trace = FallbackTrace(tried=True)

    assert asyncio.run(hedged_call(primary, RACE, name="Gemini", fallback=fallback, trace=trace)) == "P"
    assert fallback.count == 0


def test_race_stream_takes_the_fallback_stream_when_the_primary_has_no_first_chunk():
    async def slow_primary():
        await asyncio.sleep(5)
        yield "P"

    fallback = Streams(("yield", "F1"), ("yield", "F2"))
    trace = FallbackTrace()

    async def go() -> list[Any]:
        stream = hedged_stream(slow_primary, RACE, name="Gemini", fallback=fallback, trace=trace)
        async with aclosing(stream) as items:
            return [item async for item in items]

    assert asyncio.run(go()) == ["F1", "F2"]
    assert trace.won and not trace.primary_failed
    assert fallback.closed == 1


# --- 並行して投げた結果の扱い（ブレーカー・フォールバックの記録）----------------------------


def _hedged_primary(primary: Any, fallback: Any, trace: FallbackTrace):
    async def call() -> Any:
        return await hedged_call(primary, RACE, name="Gemini", fallback=fallback, trace=trace)

    return call


def test_fallback_that_won_only_by_speed_does_not_open_the_breaker_but_is_remembered():
    breaker = PrimaryBreaker(60, clock=Clock())
    served: list[str] = []
    trace = FallbackTrace()
    primary, fallback = Hangs(5.0), Sequence("F")

    result = asyncio.run(
        call_with_fallback(
            _hedged_primary(primary, fallback, trace),
            fallback,
            breaker=breaker,
            on_fallback=lambda: served.append("x"),
            trace=trace,
        )
    )

    assert result == "F"
    assert fallback.count == 1  # 並行して投げた 1 回だけ（逐次の fallback() は呼ばない）
    assert breaker.route() == "primary"  # 遅いだけでは、主モデルを飛ばさない
    assert served == ["x"]  # 同じ質問の中では覚える


def test_fallback_that_won_after_a_primary_error_opens_the_breaker():
    breaker = PrimaryBreaker(60, clock=Clock())
    trace = FallbackTrace()
    primary = delayed_sequence([(0.0, Unavailable("503")), (5.0, "P")])
    fallback = Sequence("F")

    result = asyncio.run(
        call_with_fallback(_hedged_primary(primary, fallback, trace), fallback, breaker=breaker, trace=trace)
    )

    assert result == "F"
    assert breaker.route() == "fallback"


def test_fallback_already_tried_in_the_race_is_not_called_again_after_the_primary_fails():
    trace = FallbackTrace()
    primary_failure = Unavailable("503 primary")
    primary, fallback = Hangs(0.2, primary_failure), Sequence(Unavailable("503 fallback"))

    with pytest.raises(Unavailable) as raised:
        asyncio.run(
            call_with_fallback(_hedged_primary(primary, fallback, trace), fallback, trace=trace, name="Gemini")
        )

    assert raised.value is primary_failure
    assert fallback.count == 1  # 並行して投げた 1 回で終わり。逐次でもう一度試さない


def test_stream_fallback_that_won_only_by_speed_does_not_open_the_breaker():
    breaker = PrimaryBreaker(60, clock=Clock())
    served: list[str] = []
    trace = FallbackTrace()

    async def slow_primary():
        await asyncio.sleep(5)
        yield "P"

    fallback = Streams(("yield", "F"))

    def hedged():
        return hedged_stream(slow_primary, RACE, name="Gemini", fallback=fallback, trace=trace)

    items = asyncio.run(
        _collect_with(hedged, fallback, breaker=breaker, on_fallback=lambda: served.append("x"), trace=trace)
    )

    assert items == ["F"]
    assert fallback.started == 1
    assert breaker.route() == "primary"
    assert served == ["x"]


# --- グラフ（画面の経路 /chat/stream だけが並行して投げる）----------------------------------

SLOW = [("sleep", 0.6), ("text", "遅い主モデルの回答です。")]


def test_streaming_graph_races_the_fallback_when_the_primary_hangs():
    primary, fallback = PlanLLM(plans=[SLOW]), PlanLLM(plans=[ANSWER])
    started = time.monotonic()

    result = ask(build_graph(primary, hedge=FAST, fallback_llm=fallback), stream=True)

    assert result["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert time.monotonic() - started < 0.4  # 主モデルの遅い回答（0.6 秒）は待たない
    assert (primary.calls, fallback.calls) == (1, 1)  # 主モデルの 1 本が止まっている間に、フォールバック 1 本


def test_non_streaming_graph_does_not_race_it_waits_for_the_primary_attempts():
    # /chat は並行して投げない。主モデルの遅い試行が先に返れば、その結果を採る（フォールバックは呼ばない）
    primary, fallback = PlanLLM(plans=[SLOW]), PlanLLM(plans=[ANSWER])

    result = ask(build_graph(primary, hedge=FAST, fallback_llm=fallback))

    assert result["messages"][-1].content == "遅い主モデルの回答です。"
    assert fallback.calls == 0


def test_streaming_graph_does_not_open_the_breaker_when_the_primary_was_only_slow():
    breaker = PrimaryBreaker(60, clock=Clock())
    primary = PlanLLM(plans=[SLOW, SLOW])
    fallback = PlanLLM(plans=[ANSWER])
    graph = build_graph(primary, hedge=FAST, fallback_llm=fallback, fallback_breaker=breaker)

    ask(graph, stream=True)
    ask(graph, stream=True)

    assert primary.calls == 2  # 2 問目も主モデルから試す（飛ばさない）
    assert fallback.calls == 2


def test_streaming_graph_opens_the_breaker_when_the_primary_failed_before_the_fallback_won():
    breaker = PrimaryBreaker(60, clock=Clock())
    primary = PlanLLM(plans=[[("sleep", 0.01), ("raise", Unavailable("503"))], SLOW])
    fallback = PlanLLM(plans=[ANSWER])
    graph = build_graph(primary, hedge=FAST, fallback_llm=fallback, fallback_breaker=breaker)

    first = ask(graph, stream=True)
    second = ask(graph, stream=True)

    assert first["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert second["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert primary.calls == 2  # 1 問目の分だけ。2 問目は主モデルを飛ばす
    assert fallback.calls == 2


def test_streaming_graph_remembers_the_fallback_for_the_rest_of_the_turn_after_a_race():
    primary = PlanLLM(plans=[SLOW])
    fallback = PlanLLM(plans=[[SEARCH], ANSWER])

    result = ask(build_graph(primary, hedge=FAST, fallback_llm=fallback), stream=True)

    assert result["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert primary.calls == 1  # 検索の判断の 1 本だけ。回答（2 回目）は主モデルを試さない
    assert fallback.calls == 2


# --- 環境変数からの組み立て --------------------------------------------------------


class RecordingLLM:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def gemini_env(monkeypatch):
    for name in ("GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "real-looking-key")
    monkeypatch.setattr(graph_module, "ChatGoogleGenerativeAI", RecordingLLM)
    return monkeypatch


def test_fallback_is_off_by_default(gemini_env):
    assert llm_fallback_model() == ""
    assert create_fallback_llm() is None


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_fallback_model_is_off(gemini_env, value):
    gemini_env.setenv("GEMINI_FALLBACK_MODEL", value)
    assert create_fallback_llm() is None


def test_fallback_model_builds_a_second_client_with_the_same_settings(gemini_env):
    gemini_env.setenv("GEMINI_FALLBACK_MODEL", " gemini-3.5-flash-lite ")

    llm = create_fallback_llm()

    assert isinstance(llm, RecordingLLM)
    assert llm.kwargs == {
        "model": "gemini-3.5-flash-lite",
        "google_api_key": "real-looking-key",
    }


def test_fallback_equal_to_the_primary_model_is_off(gemini_env):
    gemini_env.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    gemini_env.setenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    assert create_fallback_llm() is None


def test_fallback_equal_to_the_default_model_is_off(gemini_env):
    gemini_env.setenv("GEMINI_FALLBACK_MODEL", graph_module.DEFAULT_MODEL)
    assert create_fallback_llm() is None


def test_fallback_is_off_in_mock_mode(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    assert create_fallback_llm() is None


def test_build_graph_without_an_llm_picks_up_the_env_fallback(gemini_env):
    primary, fallback = PlanLLM(plans=[DOWN]), PlanLLM(plans=[ANSWER])
    gemini_env.setenv("LLM_HEDGE_DELAY_SECONDS", "0")
    gemini_env.setattr(graph_module, "create_llm", lambda: primary)
    gemini_env.setattr(graph_module, "create_fallback_llm", lambda: fallback)

    result = ask(build_graph())

    assert result["messages"][-1].content == "フォールバックのモデルからの回答です。"
    assert (primary.calls, fallback.calls) == (1, 1)


def test_build_graph_never_adds_a_fallback_to_the_mock_model(gemini_env):
    built: list[str] = []
    gemini_env.setattr(graph_module, "create_llm", lambda: MockChatModel())
    gemini_env.setattr(graph_module, "create_fallback_llm", lambda: built.append("called") or PlanLLM(plans=[ANSWER]))

    result = ask(build_graph())

    assert result["messages"][-1].content  # モックが回答を返す
    assert built == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 60.0), ("", 60.0), ("30", 30.0), ("0", 0.0), ("-1", 60.0), ("abc", 60.0), ("nan", 60.0), ("inf", 60.0)],
)
def test_fallback_hold_seconds_from_the_environment(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("GEMINI_FALLBACK_HOLD_SECONDS", raising=False)
    else:
        monkeypatch.setenv("GEMINI_FALLBACK_HOLD_SECONDS", value)
    assert llm_fallback_hold_seconds() == expected


def _env_graph(gemini_env, primary: PlanLLM, fallback: PlanLLM):
    gemini_env.setenv("LLM_HEDGE_DELAY_SECONDS", "0")
    gemini_env.setattr(graph_module, "create_llm", lambda: primary)
    gemini_env.setattr(graph_module, "create_fallback_llm", lambda: fallback)
    return build_graph()


def test_build_graph_from_the_environment_skips_the_primary_on_the_next_turn(gemini_env):
    primary, fallback = PlanLLM(plans=[DOWN]), PlanLLM(plans=[ANSWER])
    graph = _env_graph(gemini_env, primary, fallback)

    ask(graph)
    ask(graph)

    assert (primary.calls, fallback.calls) == (1, 2)


def test_build_graph_from_the_environment_tries_the_primary_every_turn_when_hold_is_zero(gemini_env):
    gemini_env.setenv("GEMINI_FALLBACK_HOLD_SECONDS", "0")
    primary, fallback = PlanLLM(plans=[DOWN]), PlanLLM(plans=[ANSWER])
    graph = _env_graph(gemini_env, primary, fallback)

    ask(graph)
    ask(graph)

    assert (primary.calls, fallback.calls) == (2, 2)
