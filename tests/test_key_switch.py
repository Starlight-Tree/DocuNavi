"""無料キーが 429 で尽きたときだけ、有料キー（GEMINI_API_KEY_PAID）で 1 回試す。

実際の API は呼ばない。判定（どの失敗で切り替えるか）・保持・ストリーミングでの切り替え位置・グラフと埋め込みへの
組み込み・環境変数からの組み立てを、偽の LLM とストリームで確かめる。キーの値はログに出さない。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app import search_vector
from app.agents import graph as graph_module
from app.agents.graph import build_graph, create_paid_llm
from app.agents.resilience import (
    KeySwitch,
    call_with_key_switch,
    key_switch_hold_seconds,
    paid_api_key,
    stream_with_key_switch,
)
from tests.test_llm_fallback import FAST, PlanLLM, RateLimited, Rejected, Unavailable, ask

ANSWER_FREE = [("text", "無料キーの回答です。")]
ANSWER_PAID = [("text", "有料キーの回答です。")]
LIMITED = [("raise", RateLimited("429 RESOURCE_EXHAUSTED"))]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Calls:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.count = 0

    async def __call__(self) -> Any:
        self.count += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def switch_call(free: Calls, paid: Calls, switch: KeySwitch | None = None) -> Any:
    return asyncio.run(call_with_key_switch(free, paid, switch=switch or KeySwitch(60), name="Gemini"))


# --- call_with_key_switch ------------------------------------------------------


def test_free_success_never_touches_the_paid_key():
    free, paid = Calls("free"), Calls("paid")
    switch = KeySwitch(60)
    assert switch_call(free, paid, switch) == "free"
    assert (free.count, paid.count, switch.free_used, switch.paid_used) == (1, 0, 1, 0)


def test_a_429_switches_to_the_paid_key_exactly_once(caplog):
    free, paid = Calls(RateLimited("429")), Calls("paid")
    switch = KeySwitch(60)
    with caplog.at_level(logging.WARNING):
        assert switch_call(free, paid, switch) == "paid"
    assert (free.count, paid.count, switch.paid_used) == (1, 1, 1)
    assert "有料キーで応答しました" in caplog.text


@pytest.mark.parametrize("failure", [Rejected("400"), Unavailable("503"), TimeoutError()])
def test_other_failures_do_not_switch(failure):
    free, paid = Calls(failure), Calls("paid")
    with pytest.raises(type(failure)):
        switch_call(free, paid)
    assert paid.count == 0


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_client_errors_do_not_switch(code):
    class Denied(Exception):
        pass

    denied = Denied("denied")
    denied.code = code  # type: ignore[attr-defined]
    free, paid = Calls(denied), Calls("paid")
    with pytest.raises(Denied):
        switch_call(free, paid)
    assert paid.count == 0


def test_when_the_paid_key_also_fails_the_original_429_is_raised(caplog):
    free, paid = Calls(RateLimited("free 429")), Calls(Unavailable("paid 503"))
    with caplog.at_level(logging.WARNING), pytest.raises(RateLimited):
        switch_call(free, paid)
    assert "有料キーも失敗しました（Unavailable）" in caplog.text


def test_after_a_switch_the_paid_key_goes_first_until_the_hold_ends():
    clock = Clock()
    switch = KeySwitch(60, clock=clock)
    assert switch_call(Calls(RateLimited("429")), Calls("paid"), switch) == "paid"
    free, paid = Calls("free"), Calls("paid again")
    clock.now += 30
    assert switch_call(free, paid, switch) == "paid again"
    assert free.count == 0
    clock.now += 61  # 最後に有料キーが成功してから 61 秒
    assert switch_call(free, paid, switch) == "free"


def test_a_failing_paid_key_during_the_hold_ends_the_hold_and_tries_the_free_key():
    clock = Clock()
    switch = KeySwitch(60, clock=clock)
    switch_call(Calls(RateLimited("429")), Calls("paid"), switch)
    free, paid = Calls("free"), Calls(Unavailable("503"))
    assert switch_call(free, paid, switch) == "free"
    assert (free.count, paid.count) == (1, 1)
    assert not switch.use_paid()


def test_a_zero_hold_keeps_no_state():
    switch = KeySwitch(0)
    assert switch_call(Calls(RateLimited("429")), Calls("paid"), switch) == "paid"
    assert not switch.use_paid()
    free = Calls("free")
    assert switch_call(free, Calls("paid"), switch) == "free"


def test_cancellation_is_not_swallowed():
    async def scenario() -> None:
        started = asyncio.Event()

        async def slow() -> str:
            started.set()
            await asyncio.sleep(10)
            return "never"

        task = asyncio.ensure_future(call_with_key_switch(slow, Calls("paid"), switch=KeySwitch(60)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


# --- stream_with_key_switch ----------------------------------------------------


def stream_of(*items: Any):
    """items を順に流す。例外のインスタンスは、その位置で送出する。"""
    state = {"count": 0}

    async def factory() -> AsyncIterator[Any]:
        state["count"] += 1
        for item in items:
            if isinstance(item, BaseException):
                raise item
            yield item

    factory.state = state  # type: ignore[attr-defined]
    return factory


def collect(free: Any, paid: Any, switch: KeySwitch | None = None) -> list[Any]:
    async def run() -> list[Any]:
        return [x async for x in stream_with_key_switch(free, paid, switch=switch or KeySwitch(60), name="Gemini")]

    return asyncio.run(run())


def test_the_stream_switches_when_the_free_key_is_limited_before_the_first_chunk():
    free, paid = stream_of(RateLimited("429")), stream_of("p1", "p2")
    switch = KeySwitch(60)
    assert collect(free, paid, switch) == ["p1", "p2"]
    assert (free.state["count"], paid.state["count"], switch.paid_used) == (1, 1, 1)


def test_the_stream_never_switches_after_the_first_chunk():
    free, paid = stream_of("f1", RateLimited("429")), stream_of("p1")
    with pytest.raises(RateLimited):
        collect(free, paid)
    assert paid.state["count"] == 0


def test_the_stream_does_not_switch_on_other_failures_or_an_empty_stream():
    for free in (stream_of(Rejected("400")), stream_of(Unavailable("503")), stream_of()):
        paid = stream_of("p1")
        with pytest.raises(Exception):  # noqa: B017 - 失敗の型は 3 通り（EmptyStream を含む）
            collect(free, paid)
        assert paid.state["count"] == 0


def test_the_stream_raises_the_original_429_when_the_paid_key_fails_too():
    free, paid = stream_of(RateLimited("free")), stream_of(Unavailable("paid"))
    with pytest.raises(RateLimited):
        collect(free, paid)


def test_the_stream_starts_with_the_paid_key_during_the_hold():
    clock = Clock()
    switch = KeySwitch(60, clock=clock)
    collect(stream_of(RateLimited("429")), stream_of("p"), switch)
    free, paid = stream_of("f"), stream_of("p2")
    assert collect(free, paid, switch) == ["p2"]
    assert free.state["count"] == 0


# --- グラフへの組み込み --------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_graph_without_a_paid_llm_keeps_failing_like_before(stream):
    free = PlanLLM(plans=[LIMITED])
    with pytest.raises(RateLimited):
        ask(build_graph(free, hedge=FAST), stream=stream)
    assert free.calls == FAST.max_attempts


@pytest.mark.parametrize("stream", [False, True])
def test_graph_uses_the_paid_llm_after_the_free_attempts_are_limited(stream):
    free, paid = PlanLLM(plans=[LIMITED]), PlanLLM(plans=[ANSWER_PAID])
    result = ask(build_graph(free, hedge=FAST, paid_llm=paid), stream=stream)
    assert result["messages"][-1].content == "有料キーの回答です。"
    assert free.calls == FAST.max_attempts  # 先に、無料キーの間隔を空けた再試行を使い切る
    assert paid.calls == 1


@pytest.mark.parametrize("stream", [False, True])
def test_graph_does_not_use_the_paid_llm_when_the_free_one_answers(stream):
    free, paid = PlanLLM(plans=[ANSWER_FREE]), PlanLLM(plans=[ANSWER_PAID])
    result = ask(build_graph(free, hedge=FAST, paid_llm=paid), stream=stream)
    assert result["messages"][-1].content == "無料キーの回答です。"
    assert paid.calls == 0


@pytest.mark.parametrize("stream", [False, True])
def test_graph_does_not_use_the_paid_llm_for_a_server_failure(stream):
    free, paid = PlanLLM(plans=[[("raise", Unavailable("503"))]]), PlanLLM(plans=[ANSWER_PAID])
    with pytest.raises(Unavailable):
        ask(build_graph(free, hedge=FAST, paid_llm=paid), stream=stream)
    assert paid.calls == 0


def test_graph_skips_the_free_key_during_the_hold():
    free, paid = PlanLLM(plans=[LIMITED]), PlanLLM(plans=[ANSWER_PAID])
    graph = build_graph(free, hedge=FAST, paid_llm=paid, key_switch=KeySwitch(60))
    ask(graph)
    calls_after_first = free.calls
    ask(graph)
    assert free.calls == calls_after_first  # 2 回目は、無料キーを呼ばずに有料キーから始めた
    assert paid.calls == 2


# --- 環境変数からの組み立て ----------------------------------------------------


def test_the_paid_key_is_off_by_default(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY_PAID", raising=False)
    assert paid_api_key() == ""
    assert create_paid_llm() is None


def test_the_paid_llm_uses_the_paid_key_and_the_same_model(monkeypatch):
    created: list[tuple[str, str | None]] = []
    monkeypatch.setenv("GEMINI_API_KEY", "free-key")
    monkeypatch.setenv("GEMINI_API_KEY_PAID", "paid-key")
    monkeypatch.setenv("GEMINI_MODEL", "some-model")
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setattr(
        graph_module, "_create_gemini", lambda model, api_key=None: created.append((model, api_key)) or object()
    )
    assert create_paid_llm() is not None
    assert created == [("some-model", "paid-key")]


def test_the_paid_llm_is_not_made_in_mock_mode_or_with_vertex_ai(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_PAID", "paid-key")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert create_paid_llm() is None
    monkeypatch.setenv("GEMINI_API_KEY", "free-key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    assert create_paid_llm() is None


@pytest.mark.parametrize(
    ("raw", "expected"), [("", 60.0), ("30", 30.0), ("0", 0.0), ("-1", 60.0), ("x", 60.0), ("nan", 60.0)]
)
def test_hold_seconds_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("GEMINI_KEY_SWITCH_HOLD_SECONDS", raw)
    assert key_switch_hold_seconds() == expected


def test_the_key_value_never_appears_in_the_logs(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_API_KEY_PAID", "secret-paid-value")
    switch = KeySwitch(60)
    with caplog.at_level(logging.DEBUG):
        switch_call(Calls(RateLimited("429")), Calls("paid"), switch)
    assert "secret-paid-value" not in caplog.text


# --- 質問の埋め込み ------------------------------------------------------------


class FakeEmbedClient:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.count = 0
        self.aio = self
        self.models = self

    async def embed_content(self, **kwargs: Any) -> Any:
        self.count += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _Embedding:
    def __init__(self, values: list[float]) -> None:
        self.values = values


class _Result:
    embeddings = [_Embedding([0.1] * 768)]


def _embed_env(monkeypatch, free: FakeEmbedClient, paid: FakeEmbedClient | None, *, with_paid_key: bool = True) -> None:
    monkeypatch.setattr(search_vector, "_get_client", lambda: free)
    monkeypatch.setattr(search_vector, "_get_paid_client", lambda: paid)
    monkeypatch.setattr(search_vector, "_key_switch", None)
    if with_paid_key:
        monkeypatch.setenv("GEMINI_API_KEY_PAID", "paid-key")
    else:
        monkeypatch.delenv("GEMINI_API_KEY_PAID", raising=False)


def test_the_embedding_switches_to_the_paid_key_on_a_429(monkeypatch):
    free, paid = FakeEmbedClient(RateLimited("429")), FakeEmbedClient(_Result())
    _embed_env(monkeypatch, free, paid)
    assert len(asyncio.run(search_vector.embed_query("質問"))) == 768
    assert (free.count, paid.count) == (1, 1)
    asyncio.run(search_vector.embed_query("もう一度"))
    assert (free.count, paid.count) == (1, 2)  # 保持の間は、最初から有料キー


def test_the_embedding_does_not_switch_on_other_failures(monkeypatch):
    free, paid = FakeEmbedClient(Rejected("400")), FakeEmbedClient(_Result())
    _embed_env(monkeypatch, free, paid)
    with pytest.raises(Rejected):
        asyncio.run(search_vector.embed_query("質問"))
    assert paid.count == 0


def test_the_embedding_without_a_paid_key_never_builds_the_paid_client(monkeypatch):
    free = FakeEmbedClient(RateLimited("429"))
    _embed_env(monkeypatch, free, None, with_paid_key=False)
    with pytest.raises(RateLimited):
        asyncio.run(search_vector.embed_query("質問"))
    assert free.count == 1
