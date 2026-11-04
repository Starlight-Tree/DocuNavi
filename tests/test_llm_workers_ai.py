"""CHAT_ANSWER_BACKEND=workers_ai: 回答の生成だけを Cloudflare Workers AI（Qwen3）に切り替える試験用の経路。

実際の API は使わない（通信は httpx.MockTransport の偽物）。確かめるのは次の点。
- 送る内容（宛先・認証・モデル名・ツール・履歴の形）と、応答（通常・ストリーミング・ツール呼び出し・推論の除去・usage）
- 無料枠: 自分で決めた予算に達したら送らない、Cloudflare の枠切れ（3036）で止まる、UTC の日が替わったら戻る、予算は 10,000 以下
- 失敗は固定メッセージの 429/500/504/502 になり、トークンや応答の本文は含まない。Gemini へは自動で切り替えない
- build_graph: 既定は Gemini のまま。workers_ai のときは、ヘッジ・有料キー・フォールバックを使わず、追質問の補完だけ Gemini
"""

import asyncio
import json
from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import SecretStr

from app.agents import followup, tools
from app.agents import graph as graph_module
from app.agents.graph import ANSWER_WORKERS_AI, MockChatModel, answer_backend, build_graph
from app.agents.workers_ai import (
    FREE_DAILY_NEURONS,
    MODEL,
    ChatWorkersAI,
    NeuronBudget,
    ThinkStripper,
    WorkersAIAuthError,
    WorkersAIError,
    WorkersAIQuotaExceeded,
    WorkersAIRateLimited,
    WorkersAITimeout,
    create_workers_ai_llm,
    daily_budget,
    neurons_for,
    strip_think,
    to_openai_messages,
)
from app.api.routes import classify_llm_error
from app.scope import ACCESS_SCOPE_KEY, Scope
from tests.fakes import FakeSearch

TOKEN = "cf-test-token-0123456789"
ACCOUNT = "0123456789abcdef0123456789abcdef"
SEARCH_TOOL = {"name": "search_documents", "description": "資料を検索する", "parameters": {"type": "object"}}


class Upstream:
    """偽の Workers AI。届いた要求を記録し、用意した応答を返す。"""

    def __init__(self, *responses: httpx.Response | Exception):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def completion(text: str, *, prompt: int = 1000, output: int = 200, tool_calls: list | None = None) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output},
        },
    )


def sse(*events: dict | str) -> httpx.Response:
    lines = [f"data: {e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)}\n\n" for e in events]
    return httpx.Response(200, content="".join(lines).encode(), headers={"content-type": "text/event-stream"})


def delta(**fields: Any) -> dict:
    return {"choices": [{"index": 0, "delta": fields}]}


def model(upstream: Upstream, **kwargs: Any) -> ChatWorkersAI:
    return ChatWorkersAI(account_id=ACCOUNT, api_token=SecretStr(TOKEN), transport=upstream.transport, **kwargs)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def collect(llm: Any, messages: list[BaseMessage]) -> AIMessage:
    full = None
    async for chunk in llm.astream(messages):
        full = chunk if full is None else full + chunk
    assert full is not None
    return full


HELLO = [SystemMessage(content="指示"), HumanMessage(content="こんにちは")]


# --- 推論（<think>）の除去 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<think>考え中</think>\n\n答えです", "答えです"),
        ("答えです", "答えです"),
        ("前置き<think>考え</think>後ろ", "前置き後ろ"),
        ("考え中</think>\n答えです", "答えです"),  # 開きの目印がテンプレート側にある形
        ("<think>閉じない考え", ""),
    ],
)
def test_strip_think(text: str, expected: str) -> None:
    assert strip_think(text) == expected


def test_think_markers_split_across_chunks_are_removed() -> None:
    stripper = ThinkStripper()
    out = [stripper.feed(p) for p in ["<th", "ink>考え", "中</thi", "nk>\n\n答え", "です<", "b>"]]
    assert "".join(out) + stripper.flush() == "答えです<b>"


def test_a_lone_angle_bracket_at_the_end_is_not_lost() -> None:
    stripper = ThinkStripper()
    assert stripper.feed("a < b <") + stripper.feed("c") + stripper.flush() == "a < b <c"


# --- 要求の組み立て ---


def test_history_with_the_search_call_and_result_is_sent_in_openai_form() -> None:
    call = AIMessage(content="", tool_calls=[{"name": "search_documents", "args": {"query": "大気"}, "id": "c1"}])
    converted = to_openai_messages(
        [
            SystemMessage(content="指示"),
            HumanMessage(content="質問"),
            call,
            ToolMessage(content="本文", tool_call_id="c1"),
        ]
    )

    assert converted[:2] == [{"role": "system", "content": "指示"}, {"role": "user", "content": "質問"}]
    assert converted[2]["tool_calls"] == [
        {"id": "c1", "type": "function", "function": {"name": "search_documents", "arguments": '{"query": "大気"}'}}
    ]
    assert converted[3] == {"role": "tool", "tool_call_id": "c1", "content": "本文"}


def test_request_goes_to_the_account_endpoint_with_the_fixed_model_and_token() -> None:
    upstream = Upstream(completion("答えです"))
    reply = run(model(upstream).ainvoke(HELLO))

    assert reply.content == "答えです"
    request = upstream.requests[0]
    assert str(request.url) == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    body = upstream.body()
    assert body["model"] == MODEL == "@cf/qwen/qwen3-30b-a3b-fp8"
    assert body["stream"] is False and "temperature" not in body and "chat_template_kwargs" not in body
    assert body["max_tokens"] == 1500


def test_thinking_off_sends_enable_thinking_false() -> None:
    upstream = Upstream(completion("答え"))
    run(model(upstream, thinking="off").ainvoke(HELLO))
    assert upstream.body()["chat_template_kwargs"] == {"enable_thinking": False}


def test_bound_tools_are_sent_and_tool_calls_come_back() -> None:
    upstream = Upstream(
        completion(
            "",
            tool_calls=[
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "search_documents", "arguments": '{"query": "別の語"}'},
                }
            ],
        )
    )
    reply = run(model(upstream).bind_tools([SEARCH_TOOL]).ainvoke(HELLO))

    assert upstream.body()["tools"][0]["function"]["name"] == "search_documents"
    assert reply.tool_calls == [
        {"name": "search_documents", "args": {"query": "別の語"}, "id": "t1", "type": "tool_call"}
    ]


def test_reasoning_in_the_answer_is_not_returned() -> None:
    reply = run(model(Upstream(completion("<think>こう考える</think>\n\n答えです"))).ainvoke(HELLO))
    assert reply.content == "答えです"


# --- ストリーミング ---


def test_streaming_joins_the_text_drops_reasoning_and_counts_usage() -> None:
    llm = model(
        Upstream(
            sse(
                delta(role="assistant", content=""),
                delta(content="<think>考え"),
                delta(content="</think>\n\n資料に"),
                delta(content="よると、"),
                delta(reasoning_content="無視される"),
                delta(content="大気は窒素です。"),
                {"choices": [], "usage": {"prompt_tokens": 2000, "completion_tokens": 100}},
                "[DONE]",
            )
        )
    )
    full = run(collect(llm, HELLO))

    assert full.content == "資料によると、大気は窒素です。"
    assert llm.budget.used == pytest.approx(neurons_for(2000, 100))


def test_streaming_assembles_a_tool_call_from_fragments() -> None:
    fragments = [
        delta(tool_calls=[{"index": 0, "id": "t1", "function": {"name": "search_documents", "arguments": '{"que'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'ry": "大気"}'}}]),
        "[DONE]",
    ]
    full = run(collect(model(Upstream(sse(*fragments))).bind_tools([SEARCH_TOOL]), HELLO))

    assert full.tool_calls == [{"name": "search_documents", "args": {"query": "大気"}, "id": "t1", "type": "tool_call"}]


def test_streaming_without_usage_estimates_neurons_on_the_high_side() -> None:
    llm = model(Upstream(sse(delta(content="答え"), "[DONE]")))
    run(collect(llm, HELLO))
    assert llm.budget.used > 0


# --- 無料枠 ---


def test_neurons_follow_the_published_rates() -> None:
    assert neurons_for(1_000_000, 0) == 4625 and neurons_for(0, 1_000_000) == 30475


def test_a_typical_answer_uses_a_few_dozen_neurons() -> None:
    assert 20 < neurons_for(4000, 600) < 60


def test_budget_never_exceeds_the_free_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert NeuronBudget(50_000).limit == FREE_DAILY_NEURONS
    monkeypatch.setenv("WORKERS_AI_DAILY_NEURON_BUDGET", "99999")
    assert daily_budget() == FREE_DAILY_NEURONS
    for bad in ("", "abc", "0", "-5", "nan"):
        monkeypatch.setenv("WORKERS_AI_DAILY_NEURON_BUDGET", bad)
        assert daily_budget() == 8000


def test_calls_stop_before_sending_once_the_budget_is_used() -> None:
    upstream = Upstream(completion("答え", prompt=100_000, output=0))  # 約 463 Neurons
    llm = model(upstream, budget=NeuronBudget(400))

    assert run(llm.ainvoke(HELLO)).content == "答え"
    with pytest.raises(WorkersAIQuotaExceeded):
        run(llm.ainvoke(HELLO))
    assert len(upstream.requests) == 1  # 2 回目は Cloudflare へ送っていない


def test_streaming_also_stops_once_the_budget_is_used() -> None:
    upstream = Upstream(sse(delta(content="x"), "[DONE]"))
    llm = model(upstream, budget=NeuronBudget(0.0001))
    run(collect(llm, HELLO))
    with pytest.raises(WorkersAIQuotaExceeded):
        run(collect(llm, HELLO))
    assert len(upstream.requests) == 1


def test_cloudflare_allocation_error_stops_the_day_and_nothing_more_is_sent() -> None:
    upstream = Upstream(httpx.Response(429, json={"errors": [{"code": 3036, "message": "daily free allocation"}]}))
    llm = model(upstream)

    with pytest.raises(WorkersAIQuotaExceeded):
        run(llm.ainvoke(HELLO))
    with pytest.raises(WorkersAIQuotaExceeded):
        run(llm.ainvoke(HELLO))
    assert len(upstream.requests) == 1


def test_temporary_capacity_error_does_not_stop_the_day() -> None:
    upstream = Upstream(
        httpx.Response(429, json={"errors": [{"code": 3040, "message": "capacity"}]}), completion("答え")
    )
    llm = model(upstream)

    with pytest.raises(WorkersAIRateLimited):
        run(llm.ainvoke(HELLO))
    assert run(llm.ainvoke(HELLO)).content == "答え"


def test_the_budget_starts_over_on_the_next_utc_day() -> None:
    now = [86400 * 100 + 10.0]
    budget = NeuronBudget(100, clock=lambda: now[0])
    budget.add(150)
    budget.mark_exhausted()
    with pytest.raises(WorkersAIQuotaExceeded):
        budget.check()

    now[0] = 86400 * 101 + 1.0
    budget.check()
    assert budget.used == 0


# --- 失敗の扱い ---


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(429, json={"errors": [{"code": 3036}]}), 429),
        (httpx.Response(429, json={"errors": [{"code": 3040}]}), 429),
        (httpx.Response(401, json={"errors": [{"code": 10000}]}), 500),
        (httpx.Response(403, json={"errors": [{"code": 5035}]}), 500),
        (httpx.Response(404, json={}), 500),
        (httpx.Response(504, text="gateway"), 504),
        (httpx.Response(500, text="boom"), 502),
        (httpx.Response(200, text="not json"), 502),
        (httpx.Response(200, json={"choices": []}), 502),
    ],
)
def test_failures_become_fixed_api_statuses_without_leaking_anything(response: httpx.Response, status: int) -> None:
    with pytest.raises(Exception) as caught:  # noqa: PT011 - 例外の種類は、API の変換結果で確かめる
        run(model(Upstream(response)).ainvoke(HELLO))

    failure = classify_llm_error(caught.value)
    assert failure.status_code == status
    for text in (str(caught.value), str(failure.detail)):
        assert TOKEN not in text and ACCOUNT not in text and "boom" not in text and "gateway" not in text


def test_timeouts_are_504_and_no_other_backend_is_tried() -> None:
    upstream = Upstream(httpx.ReadTimeout("slow"))
    with pytest.raises(WorkersAITimeout) as caught:
        run(model(upstream).ainvoke(HELLO))

    assert classify_llm_error(caught.value).status_code == 504
    assert len(upstream.requests) == 1  # 再試行しない


def test_a_streaming_failure_before_any_text_raises_the_same_errors() -> None:
    with pytest.raises(WorkersAIAuthError):
        run(collect(model(Upstream(httpx.Response(401, json={"errors": [{"code": 10000}]}))), HELLO))
    with pytest.raises(WorkersAIError):
        run(collect(model(Upstream(sse("{broken"))), HELLO))


def test_the_token_is_not_in_the_repr_or_the_dump() -> None:
    llm = model(Upstream(completion("x")))
    assert TOKEN not in repr(llm) and TOKEN not in str(llm.model_dump())


def test_missing_credentials_fail_with_a_fixed_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)
    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID") as caught:
        create_workers_ai_llm()
    assert TOKEN not in str(caught.value)


# --- グラフへの組み込み ---


class FakeLLM(BaseChatModel):
    """回答（answer）と検索語の補完（rewrite）を返す偽の LLM。呼び出しを記録する。"""

    name: str
    answer: str
    calls: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeLLM":
        return self

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        is_rewrite = messages[0].content == followup.REWRITE_INSTRUCTION
        text = "ザーハードの大気の成分" if is_rewrite else self.answer
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def ask(graph: Any, text: str, thread: str) -> dict:
    state = {"messages": [HumanMessage(content=text)], "context": ""}
    return run(graph.ainvoke(state, {"configurable": {"thread_id": thread, ACCESS_SCOPE_KEY: Scope.ALL}}))


@pytest.fixture
def backends(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeLLM, FakeLLM]:
    """Gemini の代わり（create_llm）と Workers AI の代わり（create_workers_ai_llm）。有料キー・フォールバックは、作られたら失敗にする。"""
    gemini = FakeLLM(name="gemini", answer="Gemini の回答", calls=[])
    workers = FakeLLM(name="workers", answer="Workers AI の回答", calls=[])
    monkeypatch.setattr(graph_module, "create_llm", lambda: gemini)
    monkeypatch.setattr(graph_module, "create_workers_ai_llm", lambda **_: workers)

    def forbidden() -> Any:
        raise AssertionError("Workers AI のときに、有料キー・フォールバックを作ってはいけない")

    monkeypatch.setattr(graph_module, "create_paid_llm", forbidden)
    monkeypatch.setattr(graph_module, "create_fallback_llm", forbidden)
    monkeypatch.setattr(tools, "search_chunks_async", FakeSearch())
    return gemini, workers


def test_default_backend_is_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHAT_ANSWER_BACKEND", raising=False)
    assert answer_backend() == "gemini"
    monkeypatch.setenv("CHAT_ANSWER_BACKEND", " Gemini ")
    assert answer_backend() == "gemini"


def test_an_invalid_backend_value_fails_instead_of_falling_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_ANSWER_BACKEND", "workers")
    with pytest.raises(ValueError, match="CHAT_ANSWER_BACKEND"):
        answer_backend()


def test_gemini_still_answers_by_default(monkeypatch: pytest.MonkeyPatch, backends: tuple[FakeLLM, FakeLLM]) -> None:
    monkeypatch.delenv("CHAT_ANSWER_BACKEND", raising=False)
    monkeypatch.setattr(graph_module, "create_paid_llm", lambda: None)
    monkeypatch.setattr(graph_module, "create_fallback_llm", lambda: None)
    gemini, workers = backends

    final = ask(build_graph(), "ザーハードの大気について教えてください", "default")

    assert final["messages"][-1].content == "Gemini の回答" and workers.calls == []


def test_workers_ai_answers_only_and_search_stays(
    monkeypatch: pytest.MonkeyPatch, backends: tuple[FakeLLM, FakeLLM]
) -> None:
    monkeypatch.setenv("CHAT_ANSWER_BACKEND", ANSWER_WORKERS_AI)
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_API_KEY_PAID", "paid-key-placeholder")
    gemini, workers = backends

    final = ask(build_graph(), "ザーハードの大気について教えてください", "workers")

    assert final["messages"][-1].content == "Workers AI の回答"
    assert len(workers.calls) == 1 and gemini.calls == []  # 検索（偽）は従来どおり先に済み、Gemini は回答に使われない
    assert final["hit_count"] == 1 and final["sources"][0]["name"] == "ザーハード1.pdf"


def test_only_the_follow_up_query_completion_stays_on_gemini(
    monkeypatch: pytest.MonkeyPatch, backends: tuple[FakeLLM, FakeLLM]
) -> None:
    monkeypatch.setenv("CHAT_ANSWER_BACKEND", ANSWER_WORKERS_AI)
    gemini, workers = backends
    graph = build_graph()

    ask(graph, "ザーハードの大気について教えてください", "follow")
    ask(graph, "それはなぜですか", "follow")

    assert len(gemini.calls) == 1 and gemini.calls[0][0].content == followup.REWRITE_INSTRUCTION
    assert len(workers.calls) == 2


def test_quota_exhaustion_stops_the_answer_and_is_not_sent_to_gemini(
    monkeypatch: pytest.MonkeyPatch, backends: tuple[FakeLLM, FakeLLM]
) -> None:
    monkeypatch.setenv("CHAT_ANSWER_BACKEND", ANSWER_WORKERS_AI)
    gemini, _ = backends

    class Exhausted(FakeLLM):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> ChatResult:
            raise WorkersAIQuotaExceeded()

    monkeypatch.setattr(graph_module, "create_workers_ai_llm", lambda **_: Exhausted(name="w", answer="", calls=[]))
    with pytest.raises(WorkersAIQuotaExceeded) as caught:
        ask(build_graph(), "ザーハードの大気について教えてください", "quota")

    assert classify_llm_error(caught.value).status_code == 429
    assert gemini.calls == []


def test_mock_mode_ignores_the_backend_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_ANSWER_BACKEND", ANSWER_WORKERS_AI)
    monkeypatch.setattr(graph_module, "create_llm", lambda: MockChatModel())
    monkeypatch.setattr(
        graph_module, "create_workers_ai_llm", lambda **_: pytest.fail("モックモードで Workers AI を作ってはいけない")
    )
    build_graph()
