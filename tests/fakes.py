"""テスト用の偽物。"""

import re
import uuid
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.graph import MockChatModel

# search_documents の出力 "[ファイル名.pdf]\n本文" の、ファイル名の行
SOURCE_HEADER = re.compile(r"(?m)^\[([^\]\n]+\.(?:txt|pdf))\]\n")
TOOL_NAME = "search_documents"
NOT_FOUND_MARK = "該当する設定資料は見つかりませんでした"


class SearchingMockLLM(MockChatModel):
    """グラフの「ツール呼び出し → 回答」の流れを通すための偽の LLM（アプリのモックは、ツールを呼ばない固定の返答）。

    ユーザー発言が最後なら、発言をクエリに search_documents を呼ぶ。ツール結果が最後なら、見つかった資料名を出典として並べる。
    検索の実体は、conftest の偽の検索（`FakeSearch`）。
    """

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            names = SOURCE_HEADER.findall(str(last.content))
            if names:
                reply = "資料から該当箇所が見つかりました。\n\n（出典: " + "、".join(names) + "）"
            else:
                reply = "質問に該当する設定資料は見つかりませんでした。"
            message = AIMessage(content=reply)
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": TOOL_NAME,
                        "args": {"query": str(last.content)},
                        "id": f"mock-{uuid.uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


class FakeSearch:
    """アプリの検索（DB のベクトル検索）の偽物。テスト内の小さな資料を、キーワードが質問に含まれるときだけ返す。

    PDF も DB も BM25 も読まない。資料はこのクラスの中にあり、実際の資料とは関係がない（実際の PDF は tests/test_zahard_pdf.py が見る）。
    """

    # (キーワード, ファイル名, 本文)。上から順に調べ、キーワードが質問に含まれるものを、top_k 件まで返す
    CHUNKS: tuple[tuple[tuple[str, ...], str, str], ...] = (
        (("大気",), "ザーハード1.pdf", "ザーハードの大気は薄く、二酸化炭素が主成分である。"),
        (("ザーハリアン", "文明"), "ザーハード2.pdf", "ザーハリアン文明は、遺跡だけを残して姿を消した。"),
        (("エネルギー",), "ザーハード3.pdf", "岩山の内部には、未知のエネルギー源が眠っている可能性がある。"),
    )
    FILE_NAMES = frozenset(name for _, name, _ in CHUNKS)

    async def __call__(self, query: str, top_k: int = 2) -> list[tuple[str, str]]:
        hits = [(name, text) for keywords, name, text in self.CHUNKS if any(k in query for k in keywords)]
        return hits[:top_k]
