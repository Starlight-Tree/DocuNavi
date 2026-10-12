import os
import re
import uuid
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agents.state import AgentState
from app.agents.tools import TOOLS, bigrams

SYSTEM_PROMPT = (
    "あなたは架空の惑星「ザーハード」の設定に詳しいアシスタントです。"
    "ザーハードに関する質問には、必ず search_zahard_lore ツールで設定資料を検索し、"
    "その内容だけを根拠に日本語で回答してください。"
    "資料に書かれていないことは推測せず、「資料には記載がありません」と答えてください。"
)

# .env.example のままコピーされた場合もキー未設定として扱う
_PLACEHOLDER_KEYS = {"", "your_api_key_here"}
_NOT_FOUND_MARK = "該当する設定資料は見つかりませんでした"
_MOCK_TOOL_NAME = "search_zahard_lore"


def is_mock_mode() -> bool:
    """GEMINI_API_KEY が未設定（またはテンプレートのまま）ならモックモード。"""
    return os.environ.get("GEMINI_API_KEY", "").strip() in _PLACEHOLDER_KEYS


class MockChatModel(BaseChatModel):
    """API キーなしで動かすための擬似 LLM。

    本物の LLM と同じ「ツール呼び出し → 回答」の流れを再現する。
      1. ユーザー発言が最後なら、発言をクエリに search_zahard_lore を呼ぶ
      2. ツール結果が最後なら、結果から質問に近い文を抜粋して返答する
    """

    @property
    def _llm_type(self) -> str:
        return "mock-zahard"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "MockChatModel":
        # ツールは常に search_zahard_lore を固定で呼ぶため、バインドは不要
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            question = next(
                (str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage)), ""
            )
            message = AIMessage(content=_compose_mock_reply(question, str(last.content)))
        else:
            query = str(last.content)
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": _MOCK_TOOL_NAME,
                        "args": {"query": query},
                        "id": f"mock-{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _compose_mock_reply(question: str, tool_output: str) -> str:
    header = "【モックモード】GEMINI_API_KEY が未設定のため、設定資料から該当箇所を抜粋して返答しています。"
    if _NOT_FOUND_MARK in tool_output:
        return f"{header}\n\n質問に該当する設定資料は見つかりませんでした。"

    # ツール出力は "[ファイル名.txt]\n本文" の繰り返し
    parts = re.split(r"(?m)^\[([^\]\n]+\.txt)\]\n", tool_output)
    docs = list(zip(parts[1::2], parts[2::2]))
    if not docs:
        return f"{header}\n\n{tool_output.strip()}"

    q_grams = bigrams(question)
    scored: list[tuple[int, int, str]] = []  # (score, 出現順, 文)
    for _, text in docs:
        for sentence in re.split(r"[。\\\n]+", text):
            sentence = sentence.strip().lstrip("・").strip()
            if sentence:
                scored.append((len(q_grams & bigrams(sentence)), len(scored), sentence))

    # 最高スコアの 6 割以上の文だけを、高スコア順に最大 3 文採用する
    top_score = max((s[0] for s in scored), default=0)
    threshold = max(1, 0.6 * top_score)
    best = sorted((s for s in scored if s[0] >= threshold), key=lambda s: (-s[0], s[1]))[:3]
    if best:
        picked = [s[2] for s in sorted(best, key=lambda s: s[1])]
    else:
        picked = [scored[0][2]] if scored else []

    bullets = "\n".join(f"・{s}。" for s in picked)
    sources = "、".join(name for name, _ in docs)
    return f"{header}\n\n{bullets}\n\n（出典: {sources}）"


def create_llm() -> BaseChatModel:
    """API キーがあれば Gemini、なければモックを返す。"""
    if is_mock_mode():
        return MockChatModel()
    return ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite",
        google_api_key=os.environ["GEMINI_API_KEY"],
        temperature=0,
    )


def build_graph(llm: BaseChatModel | None = None):
    """START -> agent -> (tools -> agent)* -> END のエージェントグラフを組み立てる。"""
    llm_with_tools = (llm or create_llm()).bind_tools(TOOLS)
    tool_node = ToolNode(TOOLS)

    async def agent(state: AgentState) -> dict:
        messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [await llm_with_tools.ainvoke(messages)]}

    async def tools(state: AgentState) -> dict:
        result = await tool_node.ainvoke(state)
        retrieved = "\n\n".join(str(m.content) for m in result["messages"])
        return {**result, "context": retrieved}

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_edge(START, "agent")
    # ツール呼び出しがあれば tools へ、なければ終了
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    # thread_id ごとに会話履歴を保持する（プロセス内メモリ）
    return builder.compile(checkpointer=MemorySaver())
