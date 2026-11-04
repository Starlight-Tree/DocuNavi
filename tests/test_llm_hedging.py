"""遅い・止まる LLM 呼び出しへの対処（app/agents/resilience.py のヘッジ付き再試行）。

実際の API は呼ばない。待ち時間は数十ミリ秒にして、試行の起動順・取り消し・失敗の扱いを確かめる。
"""

import asyncio
import time
from typing import Any

import httpx
import pytest
from langchain_core.exceptions import ModelConnectionError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents import graph as graph_module
from app.agents import resilience
from app.agents.graph import MockChatModel, build_graph
from app.agents.resilience import AttemptCancelled, Failure, HedgePolicy, classify_failure, hedged_call

# 試行を 50ms 待っても返らなければ追加する。失敗後の再試行・429 後の待ちも短くしておく
FAST = HedgePolicy(hedge_delay=0.05, max_attempts=3, retry_delay=0.01, rate_limit_delay=0.02)


class Api5xx(Exception):
    """google-genai の ServerError のように、HTTP ステータスを code に持つ例外。"""

    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


class Script:
    """n 本目の試行の振る舞い（待ち秒数と、返す値または送出する例外）を順番に決める偽の LLM 呼び出し。"""

    def __init__(self, *steps: tuple[float, Any]) -> None:
        self.steps = steps
        self.started = 0
        self.cancelled: list[int] = []
        self.started_at: list[float] = []
        self._t0 = time.monotonic()

    async def __call__(self) -> Any:
        index = self.started
        self.started += 1
        self.started_at.append(time.monotonic() - self._t0)
        delay, outcome = self.steps[min(index, len(self.steps) - 1)]
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled.append(index)
            raise
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run(script: Any, policy: HedgePolicy = FAST) -> Any:
    return asyncio.run(hedged_call(script, policy))


async def until(condition: Any, timeout: float = 5.0) -> None:
    """condition() が真になるまで待つ（固定秒数の sleep はマシンの遅延で外れるため使わない）。"""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.005)


# --- 失敗の分類 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        ConnectionResetError(),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
        httpx.RemoteProtocolError("Server disconnected"),
        ModelConnectionError("refused"),
        AttemptCancelled("cancelled by the SDK"),
        Api5xx(408),
        Api5xx(500),
        Api5xx(502),
        Api5xx(503),
        Api5xx(504),
    ],
)
def test_transient_failures_are_retried(exc):
    assert classify_failure(exc) is Failure.RETRY


def test_rate_limit_is_its_own_kind():
    assert classify_failure(Api5xx(429)) is Failure.RATE_LIMITED


@pytest.mark.parametrize("exc", [ValueError("bad"), Api5xx(400), Api5xx(401), Api5xx(403), Api5xx(404), KeyError("x")])
def test_other_failures_are_fatal(exc):
    assert classify_failure(exc) is Failure.FATAL


def test_the_real_cause_behind_a_wrapper_is_used():
    try:
        try:
            raise Api5xx(503)
        except Api5xx as cause:
            raise RuntimeError("Error calling model") from cause
    except RuntimeError as wrapped:
        assert classify_failure(wrapped) is Failure.RETRY


def test_a_boolean_code_is_not_mistaken_for_an_http_status():
    exc = Exception()
    exc.code = True  # type: ignore[attr-defined]
    assert classify_failure(exc) is Failure.FATAL


# --- スケジューラ -------------------------------------------------------------


def test_a_fast_first_attempt_is_not_hedged():
    script = Script((0.0, "ok"))

    assert run(script) == "ok"
    assert script.started == 1
    assert script.cancelled == []


def test_a_slow_attempt_is_hedged_and_the_loser_is_cancelled():
    script = Script((1.0, "slow"), (0.0, "fast"))
    t = time.monotonic()

    assert run(script) == "fast"

    assert time.monotonic() - t < 0.5  # 1 本目（1 秒）を待たない
    assert script.started == 2
    assert script.cancelled == [0]
    assert script.started_at[1] >= 0.05 - 0.01  # hedge_delay より前には追加しない（遅れる分には上限を置かない）


def test_the_first_attempt_wins_when_it_finishes_before_the_hedge_does():
    script = Script((0.2, "first"), (0.5, "second"))

    assert run(script, HedgePolicy(hedge_delay=0.05, max_attempts=2)) == "first"
    assert script.started == 2
    assert script.cancelled == [1]


def test_a_failed_attempt_is_retried_right_away_not_after_the_hedge_delay():
    policy = HedgePolicy(hedge_delay=5.0, max_attempts=3, retry_delay=0.01)
    script = Script((0.0, httpx.ReadTimeout("slow")), (0.0, "ok"))
    t = time.monotonic()

    assert run(script, policy) == "ok"

    assert time.monotonic() - t < 1.0  # hedge_delay（5 秒）を待たない
    assert script.started == 2


def test_a_fatal_failure_is_raised_at_once_without_retrying():
    script = Script((0.0, Api5xx(401)))

    with pytest.raises(Api5xx) as info:
        run(script)

    assert info.value.code == 401
    assert script.started == 1


def test_a_fatal_failure_cancels_the_other_attempts():
    script = Script((1.0, "never"), (0.0, Api5xx(400)))

    with pytest.raises(Api5xx):
        run(script)

    assert script.cancelled == [0]


def test_when_every_attempt_fails_the_last_failure_is_raised():
    script = Script((0.0, httpx.ReadTimeout("a")), (0.0, Api5xx(503)), (0.0, Api5xx(504)))

    with pytest.raises(Api5xx) as info:
        run(script)

    assert info.value.code == 504
    assert script.started == 3  # max_attempts を超えて投げない


def test_attempts_never_exceed_max_attempts_even_if_all_are_slow():
    script = Script((0.4, "late"))

    assert run(script, HedgePolicy(hedge_delay=0.02, max_attempts=3)) == "late"
    assert script.started == 3


def test_one_attempt_means_no_hedge_and_no_retry():
    script = Script((0.2, "slow"), (0.0, "fast"))

    assert run(script, HedgePolicy(hedge_delay=0.02, max_attempts=1)) == "slow"
    assert script.started == 1


def test_rate_limit_stops_hedging_and_retries_after_a_longer_wait():
    policy = HedgePolicy(hedge_delay=0.05, max_attempts=3, retry_delay=0.0, rate_limit_delay=0.15)
    script = Script((0.0, Api5xx(429)), (0.0, "ok"))

    assert run(script, policy) == "ok"

    assert script.started == 2
    assert script.started_at[1] >= 0.15  # rate_limit_delay 分だけ空ける


def test_after_a_rate_limit_no_more_hedges_are_added_while_others_are_pending():
    # 1 本目は遅い。追加した 2 本目が 429 を返したら、1 本目が返るまで 3 本目は投げない
    policy = HedgePolicy(hedge_delay=0.05, max_attempts=3, retry_delay=0.01, rate_limit_delay=0.02)
    script = Script((0.4, "slow"), (0.01, Api5xx(429)), (0.0, "third"))

    assert run(script, policy) == "slow"
    assert script.started == 2


def test_cancelling_the_caller_cancels_every_attempt_and_leaves_no_tasks():
    script = Script((5.0, "never"))

    async def scenario() -> None:
        task = asyncio.create_task(hedged_call(script, HedgePolicy(hedge_delay=0.02, max_attempts=3)))
        await until(lambda: script.started == 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert leftovers == []

    asyncio.run(scenario())
    assert sorted(script.cancelled) == [0, 1, 2]


def test_a_timeout_around_the_whole_call_cancels_every_attempt():
    script = Script((5.0, "never"))

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            # 3 本とも投げ終えてから（0.1 秒後）切れるよう、外側の上限には余裕を持たせる
            await asyncio.wait_for(hedged_call(script, HedgePolicy(hedge_delay=0.05, max_attempts=3)), 0.6)

    asyncio.run(scenario())
    assert sorted(script.cancelled) == [0, 1, 2]


def test_an_attempt_that_cancels_itself_is_retried_instead_of_crashing():
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError()  # SDK の内部のキャンセル等。呼び出し元の取り消しではない
        return "ok"

    assert run(attempt) == "ok"
    assert calls == 2


def test_when_every_attempt_cancels_itself_it_is_a_transient_failure_after_max_attempts():
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    with pytest.raises(AttemptCancelled):
        run(attempt)

    assert calls == 3


def test_a_synchronous_error_from_the_factory_is_a_failed_attempt_not_a_crash():
    calls = 0

    async def slow() -> str:
        await asyncio.sleep(5)
        return "never"

    async def fast() -> str:
        return "ok"

    def make() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return slow()
        if calls == 2:
            raise TimeoutError("raised before an awaitable was built")
        return fast()

    # 2 本目が同期的に失敗しても 1 本目は生きており、次のヘッジで 3 本目が投げられて成功する
    assert run(make) == "ok"
    assert calls == 3


def test_a_fatal_synchronous_error_from_the_factory_is_raised_and_cancels_the_others():
    script = Script((5.0, "never"))
    calls = 0

    def make() -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("bad request")
        return script()

    with pytest.raises(ValueError, match="bad request"):
        run(make)

    assert script.cancelled == [0]


def test_failures_in_the_same_round_are_handled_in_launch_order():
    """同じ回に複数の試行が失敗しても、どの失敗が返るかは投げた順で決まる（集合の順序に依らない）。"""
    seen = set()
    for _ in range(12):

        async def scenario() -> str:
            gate = asyncio.Event()
            calls = 0

            async def attempt() -> str:
                nonlocal calls
                calls += 1
                me = calls
                await gate.wait()
                raise ValueError(f"fatal-{me}")

            task = asyncio.ensure_future(hedged_call(attempt, HedgePolicy(hedge_delay=0.02, max_attempts=2)))
            await until(lambda: calls == 2)
            gate.set()  # 2 本が同じ回に失敗する
            try:
                await task
            except ValueError as exc:
                return str(exc)
            raise AssertionError("expected a failure")

        seen.add(asyncio.run(scenario()))

    assert seen == {"fatal-1"}


def test_a_rate_limit_is_reported_even_when_the_last_failure_was_something_else():
    script = Script((0.0, Api5xx(429)), (0.0, Api5xx(503)), (0.0, Api5xx(503)))

    with pytest.raises(Api5xx) as info:
        run(script)

    assert info.value.code == 429  # 呼び出し側が待ってから再送できるよう、429 を返す
    assert script.started == 3


def test_a_second_cancellation_during_cleanup_still_waits_for_the_attempts_to_finish():
    finished: list[int] = []
    started: list[int] = []

    async def attempt() -> str:
        started.append(1)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # 後始末に時間がかかる
            finished.append(1)
            raise
        return "never"

    async def scenario() -> None:
        task = asyncio.ensure_future(hedged_call(attempt, HedgePolicy(hedge_delay=5.0, max_attempts=1)))
        await until(lambda: started)
        task.cancel()
        await asyncio.sleep(0.01)  # 試行は後始末の最中
        task.cancel()  # 2 回目（外側の wait_for の期限切れ等を想定）
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [1]  # 後始末が終わってから戻る
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

    asyncio.run(scenario())


def test_a_loser_that_ignores_cancellation_does_not_hold_up_the_winner(monkeypatch):
    monkeypatch.setattr(resilience, "_CANCEL_GRACE_SECONDS", 0.1)

    async def scenario() -> tuple[str, float]:
        release = asyncio.Event()
        asyncio.get_running_loop().call_later(5.0, release.set)  # 退行したときにテストが固まらないための保険
        calls = 0

        async def attempt() -> str:
            nonlocal calls
            calls += 1
            if calls > 1:
                return "ok"
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass  # 取り消しを無視する
            return "stubborn"

        t = time.monotonic()
        result = await hedged_call(attempt, FAST)
        elapsed = time.monotonic() - t
        release.set()
        await asyncio.sleep(0.05)  # 居座っていた試行を終わらせる
        return result, elapsed

    result, elapsed = asyncio.run(scenario())

    assert result == "ok"
    assert elapsed < 2.0  # 取り消しの待ちは上限（0.1 秒）で切り上げる


# --- グラフへの組み込み -------------------------------------------------------


class FlakyLLM(BaseChatModel):
    """呼ばれた順に待ち時間が決まる LLM（ツールは呼ばず、固定の回答を返す）。"""

    delays: list[float]
    calls: int = 0
    cancelled: int = 0

    @property
    def _llm_type(self) -> str:
        return "flaky"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FlakyLLM":
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        index = self.calls
        self.calls += 1
        try:
            await asyncio.sleep(self.delays[min(index, len(self.delays) - 1)])
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="こんにちは。"))])


def ask(graph: Any) -> dict:
    config = {"configurable": {"thread_id": "hedge-test"}}
    state = {"messages": [HumanMessage(content="こんにちは")], "context": ""}
    return asyncio.run(graph.ainvoke(state, config))


def test_graph_hedges_a_slow_llm_call_when_a_policy_is_given():
    llm = FlakyLLM(delays=[2.0, 0.0])
    graph = build_graph(llm, hedge=FAST)
    t = time.monotonic()

    result = ask(graph)

    assert result["messages"][-1].content == "こんにちは。"
    assert time.monotonic() - t < 1.0
    assert (llm.calls, llm.cancelled) == (2, 1)


def test_graph_makes_a_single_call_without_a_policy():
    llm = FlakyLLM(delays=[0.2, 0.0])
    graph = build_graph(llm)  # llm を渡したときは、hedge を明示しない限りヘッジしない

    result = ask(graph)

    assert result["messages"][-1].content == "こんにちは。"
    assert (llm.calls, llm.cancelled) == (1, 0)


# --- 本番の経路（llm を渡さない build_graph）-----------------------------------


def _use_model(monkeypatch: pytest.MonkeyPatch, llm: BaseChatModel) -> list[HedgePolicy | None]:
    """create_llm() の戻りを差し替え、hedged_call に渡された方針を記録する。"""
    policies: list[HedgePolicy | None] = []
    real = graph_module.hedged_call

    async def spy(make_attempt: Any, policy: HedgePolicy, *, name: str = "LLM") -> Any:
        policies.append(policy)
        return await real(make_attempt, policy, name=name)

    monkeypatch.setattr(graph_module, "create_llm", lambda: llm)
    monkeypatch.setattr(graph_module, "hedged_call", spy)
    return policies


def test_build_graph_without_an_llm_hedges_a_real_model_with_the_env_policy(monkeypatch):
    monkeypatch.setenv("CHAT_RETRIEVAL_MODE", "agent")  # 1 回目の呼び出し（検索の決定）で試行を数える
    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", "0.05")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "2")
    llm = FlakyLLM(delays=[2.0, 0.0])
    policies = _use_model(monkeypatch, llm)
    t = time.monotonic()

    result = ask(build_graph())

    assert result["messages"][-1].content == "こんにちは。"
    assert time.monotonic() - t < 1.0
    assert (llm.calls, llm.cancelled) == (2, 1)
    assert policies == [HedgePolicy(hedge_delay=0.05, max_attempts=2, early_fallback=True, deadline=20.0)]


def test_build_graph_without_an_llm_does_not_hedge_when_disabled(monkeypatch):
    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", "0")
    llm = FlakyLLM(delays=[0.2, 0.0])
    policies = _use_model(monkeypatch, llm)

    ask(build_graph())

    assert (llm.calls, llm.cancelled) == (1, 0)
    assert policies == []


def test_build_graph_never_hedges_the_mock_model(monkeypatch):
    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", "0.01")
    policies = _use_model(monkeypatch, MockChatModel())

    result = ask(build_graph())

    assert result["messages"][-1].content  # モックが回答を返す
    assert policies == []
