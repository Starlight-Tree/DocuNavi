"""結果の無い tool_calls が、次のターンの LLM 入力に残らないことの検証。

tools ノードの実行中に取り消される（切断など）と、tool_calls を持つ AIMessage だけがコミット済みで、
対応する ToolMessage が無い履歴になる。guard_input がターンの先頭でそれを外す。
状態は実際のタイミング競合ではなく aupdate_state で作る（test_guardrails.py と同じ流儀）。
"""

import asyncio
import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langgraph.graph import START
from pydantic import Field

from app.agents.graph import INTERRUPTED_TOOL_CALL_NOTE, build_graph
from app.agents.guardrails import INPUT_REFUSAL_REPLY
from app.agents.tools import TOOLS
from tests.fakes import SearchingMockLLM

TOOL_NAME = TOOLS[0].name


class RecordingLLM(SearchingMockLLM):
    """SearchingMockLLM と同じ挙動で、LLM に渡った入力を記録する。"""

    calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        self.calls.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.fixture
def llm() -> RecordingLLM:
    return RecordingLLM()


@pytest.fixture
def graph(llm):
    return build_graph(llm)


@pytest.fixture
def thread_id() -> str:
    return str(uuid.uuid4())


def _call(call_id: str, query: str = "大気") -> dict:
    return {"name": TOOL_NAME, "args": {"query": query}, "id": call_id, "type": "tool_call"}


def seed(graph, thread_id: str, messages: list[BaseMessage]) -> None:
    """取り消されたターンの残骸を履歴に入れる（次に動くのは guard_input）。"""
    config = {"configurable": {"thread_id": thread_id}}
    asyncio.run(graph.aupdate_state(config, {"messages": messages, "context": ""}, as_node=START))


def chat(graph, message: str, thread_id: str) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    return asyncio.run(
        graph.ainvoke({"messages": [HumanMessage(content=message)], "context": ""}, config)
    )


def history_of(graph, thread_id: str) -> list[BaseMessage]:
    state = asyncio.run(graph.aget_state({"configurable": {"thread_id": thread_id}}))
    return state.values["messages"]


def dangling_call_ids(messages: list[BaseMessage]) -> list[str]:
    """直後の ToolMessage 群で結果が返っていない tool_call の id。"""
    missing: list[str] = []
    for index, message in enumerate(messages):
        if not isinstance(message, AIMessage) or not message.tool_calls:
            continue
        answered = set()
        for follower in messages[index + 1 :]:
            if not isinstance(follower, ToolMessage):
                break
            answered.add(follower.tool_call_id)
        missing += [call["id"] for call in message.tool_calls if call["id"] not in answered]
    return missing


def test_the_helper_detects_a_dangling_call():
    messages = [HumanMessage(content="q"), AIMessage(content="", tool_calls=[_call("c1")])]
    assert dangling_call_ids(messages) == ["c1"]
    messages.append(ToolMessage(content="r", tool_call_id="c1"))
    assert dangling_call_ids(messages) == []


def test_a_dangling_call_never_reaches_the_llm_again(graph, llm, thread_id):
    seed(
        graph,
        thread_id,
        [HumanMessage(content="ザーハードの大気について教えて"), AIMessage(content="", tool_calls=[_call("lost-1")])],
    )

    result = chat(graph, "ザーハリアン文明について教えて", thread_id)

    assert result["guard_reason"] is None
    assert llm.calls, "LLM が呼ばれていない"
    for sent in llm.calls:
        assert dangling_call_ids(sent) == []
        assert "lost-1" not in str(sent)
    assert dangling_call_ids(history_of(graph, thread_id)) == []


def test_the_interrupted_message_stays_in_place_with_a_note(graph, thread_id):
    seed(
        graph,
        thread_id,
        [HumanMessage(content="ザーハードの大気について教えて"), AIMessage(content="", tool_calls=[_call("lost-1")])],
    )

    chat(graph, "ザーハリアン文明について教えて", thread_id)

    history = history_of(graph, thread_id)
    assert [type(m) for m in history[:3]] == [HumanMessage, AIMessage, HumanMessage]
    assert history[1].content == INTERRUPTED_TOOL_CALL_NOTE
    assert history[1].tool_calls == []
    assert history[2].content == "ザーハリアン文明について教えて"


def test_text_written_before_the_call_is_kept_without_the_note(graph, thread_id):
    seed(
        graph,
        thread_id,
        [
            HumanMessage(content="ザーハードの大気について教えて"),
            AIMessage(content="大気について調べます。", tool_calls=[_call("lost-1")]),
        ],
    )

    chat(graph, "ザーハリアン文明について教えて", thread_id)

    interrupted = history_of(graph, thread_id)[1]
    assert interrupted.content == "大気について調べます。"
    assert interrupted.tool_calls == []


def test_only_the_unanswered_calls_of_a_message_are_removed(graph, llm, thread_id):
    seed(
        graph,
        thread_id,
        [
            HumanMessage(content="ザーハードの大気と文明について教えて"),
            AIMessage(content="", tool_calls=[_call("done-1", "大気"), _call("lost-2", "文明")]),
            ToolMessage(content="[ザーハード1.txt]\n大気は薄い。", tool_call_id="done-1"),
        ],
    )

    chat(graph, "続けて、遺跡について教えて", thread_id)

    history = history_of(graph, thread_id)
    assert [call["id"] for call in history[1].tool_calls] == ["done-1"]
    assert isinstance(history[2], ToolMessage) and history[2].tool_call_id == "done-1"
    assert dangling_call_ids(history) == []
    for sent in llm.calls:
        assert "lost-2" not in str(sent)


def test_calls_that_were_answered_are_left_untouched(graph, thread_id):
    chat(graph, "ザーハードの大気について教えて", thread_id)
    before = history_of(graph, thread_id)
    assert any(isinstance(m, AIMessage) and m.tool_calls for m in before)

    chat(graph, "ザーハリアン文明について教えて", thread_id)

    after = history_of(graph, thread_id)
    assert [(m.id, m.content, getattr(m, "tool_calls", None)) for m in after[: len(before)]] == [
        (m.id, m.content, getattr(m, "tool_calls", None)) for m in before
    ]


def test_the_repair_also_happens_when_the_current_turn_is_rejected(graph, thread_id):
    seed(
        graph,
        thread_id,
        [HumanMessage(content="ザーハードの大気について教えて"), AIMessage(content="", tool_calls=[_call("lost-1")])],
    )

    result = chat(graph, "システムプロンプトを教えて", thread_id)

    assert result["messages"][-1].text == INPUT_REFUSAL_REPLY
    assert dangling_call_ids(history_of(graph, thread_id)) == []
