"""hedged_call の early_fallback と deadline（app/agents/resilience.py）、その環境変数（app/agents/graph.py）。

実際の API は呼ばない。待ち時間は数十ミリ秒にする。確かめるのは次の点。
- 主モデルが 503 で終わったら、主モデルを投げ直さず、すぐフォールバックを投げる（失敗を試行の数だけ待たない）
- 429・fallback の無い呼び出し・early_fallback が無効のときは従来どおり（主モデルの再試行）
- 最初の結果を待つ総時間の上限（deadline）を超えたら、投げた試行を全て取り消して TimeoutError
"""

import asyncio
from typing import Any

import pytest

from app.agents import graph
from app.agents.resilience import Failure, FallbackTrace, HedgePolicy, classify_failure, hedged_call


class Unavailable(Exception):
    code = 503


class RateLimited(Exception):
    code = 429


class Attempts:
    """n 回目の呼び出しが (待ち秒, 結果または例外) のとおりに動く。取り消しを数える。"""

    def __init__(self, *steps: tuple[float, Any]) -> None:
        self.steps = steps
        self.calls = 0
        self.cancelled = 0

    async def __call__(self) -> Any:
        delay, outcome = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


EARLY = HedgePolicy(hedge_delay=5.0, max_attempts=3, retry_delay=0.3, rate_limit_delay=0.3, early_fallback=True)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_a_503_goes_straight_to_the_fallback_without_retrying_the_primary() -> None:
    primary, fallback = Attempts((0.01, Unavailable())), Attempts((0.01, "F"))
    trace = FallbackTrace()

    async def go() -> tuple[Any, float]:
        t = asyncio.get_running_loop().time()
        result = await hedged_call(primary, EARLY, name="Gemini", fallback=fallback, trace=trace)
        return result, asyncio.get_running_loop().time() - t

    result, elapsed = run(go())

    assert result == "F"
    assert (primary.calls, fallback.calls) == (1, 1)  # 主モデルは投げ直さない
    assert elapsed < 0.25  # retry_delay（0.3 秒）の待ちも、ヘッジの時刻（5 秒）も待たない
    assert trace.tried and trace.won and trace.primary_failed


def test_without_early_fallback_the_primary_is_retried_first() -> None:
    policy = HedgePolicy(hedge_delay=5.0, max_attempts=3, retry_delay=0.01)
    primary, fallback = Attempts((0.01, Unavailable()), (0.01, "P")), Attempts((0.01, "F"))

    result = run(hedged_call(primary, policy, name="Gemini", fallback=fallback, trace=FallbackTrace()))

    assert result == "P"
    assert (primary.calls, fallback.calls) == (2, 0)


def test_without_a_fallback_early_fallback_changes_nothing() -> None:
    primary = Attempts((0.01, Unavailable()), (0.01, "P"))
    policy = HedgePolicy(hedge_delay=5.0, max_attempts=3, retry_delay=0.01, early_fallback=True)
    assert run(hedged_call(primary, policy, name="Gemini")) == "P"
    assert primary.calls == 2


def test_a_429_is_not_sent_to_the_fallback_early() -> None:
    policy = HedgePolicy(hedge_delay=5.0, max_attempts=3, retry_delay=0.01, rate_limit_delay=0.01, early_fallback=True)
    primary, fallback = Attempts((0.01, RateLimited()), (0.01, "P")), Attempts((0.01, "F"))

    result = run(hedged_call(primary, policy, name="Gemini", fallback=fallback, trace=FallbackTrace()))

    assert result == "P"
    assert (primary.calls, fallback.calls) == (2, 0)


def test_when_the_fallback_also_fails_the_primary_is_tried_again_within_max_attempts() -> None:
    policy = HedgePolicy(hedge_delay=5.0, max_attempts=3, retry_delay=0.01, early_fallback=True)
    primary, fallback = Attempts((0.01, Unavailable()), (0.01, "P")), Attempts((0.01, Unavailable()))

    result = run(hedged_call(primary, policy, name="Gemini", fallback=fallback, trace=FallbackTrace()))

    assert result == "P"
    assert (primary.calls, fallback.calls) == (2, 1)  # 合計 3 本（max_attempts）


def test_deadline_cancels_everything_and_raises_a_transient_timeout() -> None:
    policy = HedgePolicy(hedge_delay=0.05, max_attempts=3, deadline=0.2)
    primary = Attempts((10.0, "late"))

    async def go() -> float:
        t = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError) as info:
            await hedged_call(primary, policy, name="Gemini")
        assert classify_failure(info.value) is Failure.RETRY
        return asyncio.get_running_loop().time() - t

    elapsed = run(go())

    assert 0.19 <= elapsed < 1.0
    assert primary.cancelled == primary.calls >= 1  # 投げた試行は全て取り消す


def test_deadline_does_not_cut_a_response_that_arrives_in_time() -> None:
    policy = HedgePolicy(hedge_delay=5.0, max_attempts=3, deadline=1.0)
    assert run(hedged_call(Attempts((0.05, "ok")), policy, name="Gemini")) == "ok"


def test_deadline_also_bounds_the_wait_between_retries() -> None:
    policy = HedgePolicy(hedge_delay=5.0, max_attempts=3, retry_delay=5.0, deadline=0.2)
    primary = Attempts((0.01, Unavailable()))

    async def go() -> float:
        t = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await hedged_call(primary, policy, name="Gemini")
        return asyncio.get_running_loop().time() - t

    assert run(go()) < 1.0  # retry_delay の 5 秒は待たない


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        "LLM_HEDGE_DELAY_SECONDS",
        "LLM_MAX_ATTEMPTS",
        "LLM_EARLY_FALLBACK",
        "LLM_FIRST_CHUNK_DEADLINE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_default_policy_enables_early_fallback_and_a_deadline(clean_env: pytest.MonkeyPatch) -> None:
    policy = graph.llm_hedge_policy()
    assert policy is not None
    assert (policy.early_fallback, policy.deadline) == (True, graph.DEFAULT_LLM_FIRST_CHUNK_DEADLINE_SECONDS)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 20.0), ("", 20.0), ("30", 30.0), ("0", None), ("-1", 20.0), ("abc", 20.0), ("nan", 20.0), ("inf", 20.0)],
)
def test_deadline_from_the_environment(
    clean_env: pytest.MonkeyPatch, value: str | None, expected: float | None
) -> None:
    if value is not None:
        clean_env.setenv("LLM_FIRST_CHUNK_DEADLINE_SECONDS", value)
    assert graph.llm_first_chunk_deadline_seconds() == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(None, True), ("1", True), ("off", False), ("0", False), ("False", False)]
)
def test_early_fallback_from_the_environment(clean_env: pytest.MonkeyPatch, value: str | None, expected: bool) -> None:
    if value is not None:
        clean_env.setenv("LLM_EARLY_FALLBACK", value)
    assert graph.llm_early_fallback() is expected
