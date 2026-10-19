"""検索が空のとき、LLM がすぐに断らず、言い換えで検索し直すための文面と、その流れがグラフで通ること。

実際の LLM がこの文面で再検索するかは、ここでは確かめない（実 API が要る。測定は docs/search-design.md）。
ここで確かめるのは、(1) 空の返答とシステムプロンプトに、再検索の指示と回数の上限があること、
(2) LLM が言い換えで 2 回目を呼べば、2 回目の結果が回答の根拠・出典になること。
"""

import asyncio
import uuid
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents import tools
from app.agents.graph import SYSTEM_PROMPT, build_graph
from tests.fakes import NOT_FOUND_MARK, TOOL_NAME, SearchingMockLLM


def test_the_empty_reply_keeps_its_mark_and_tells_the_llm_to_search_again_with_a_limit():
    text = tools._NOT_FOUND_TEXT
    assert text.startswith(NOT_FOUND_MARK)  # 偽の LLM と評価の目印は、この先頭の一文
    assert TOOL_NAME in text and "2 回まで" in text
    assert "言い換え" in text and "資料には記載がありません" in text


def test_the_system_prompt_asks_for_a_search_again_before_refusing_and_keeps_the_refusal_rule():
    assert "最大 2 回まで検索し直して" in SYSTEM_PROMPT
    assert "検索し直しても見つからないときに限り" in SYSTEM_PROMPT
    assert "推測せず" in SYSTEM_PROMPT and "資料には記載がありません" in SYSTEM_PROMPT


class RephrasingLLM(SearchingMockLLM):
    """最初は質問そのままで検索し、空が返ったら、キーワードを変えて 1 回だけ検索し直す LLM。"""

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        last = messages[-1]
        rounds = sum(isinstance(m, ToolMessage) for m in messages)
        if isinstance(last, ToolMessage) and NOT_FOUND_MARK in str(last.content) and rounds == 1:
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": TOOL_NAME, "args": {"query": "大気"}, "id": "retry-1", "type": "tool_call"},
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])
        return super()._generate(messages, *args, **kwargs)


def test_a_second_search_after_an_empty_one_supplies_the_answer_and_the_sources():
    graph = build_graph(RephrasingLLM())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    result = asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="空気について")], "context": ""}, config))

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 2
    assert str(tool_messages[0].content) == tools._NOT_FOUND_TEXT  # 1 回目は空（質問に FakeSearch のキーワードが無い）
    assert "[ザーハード1.pdf]" in str(tool_messages[1].content)  # 2 回目は言い換えで当たる
    assert [s["name"] for s in result["sources"]] == ["ザーハード1.pdf"]  # 画面の出典は 2 回目のもの
    assert "ザーハード1.pdf" in result["messages"][-1].text
