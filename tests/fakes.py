"""テスト用の偽物。"""

import re
import uuid
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.graph import MockChatModel
from app.scope import Scope
from app.search_vector import Hit

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

    # (キーワード, ファイル名, 本文, ページ)。上から順に調べ、キーワードが質問に含まれるものを、top_k 件まで返す。
    # ページは、出典のページが画面まで正しく届くことを確かめるための、ファイルごとに違う値
    CHUNKS: tuple[tuple[tuple[str, ...], str, str, int], ...] = (
        (("大気",), "ザーハード1.pdf", "ザーハードの大気は薄く、二酸化炭素が主成分である。", 1),
        (("ザーハリアン", "文明"), "ザーハード2.pdf", "ザーハリアン文明は、遺跡だけを残して姿を消した。", 2),
        (("エネルギー",), "ザーハード3.pdf", "岩山の内部には、未知のエネルギー源が眠っている可能性がある。", 3),
    )
    FILE_NAMES = frozenset(name for _, name, _, _ in CHUNKS)

    async def __call__(
        self, query: str, top_k: int = 2, collection: str | None = None, *, scope: Scope | None = None
    ) -> list[Hit]:
        # scope は受け取るだけで見ない（範囲を見る偽物は GroupedFakeSearch）。collection も従来どおり見ない
        hits = [
            Hit(name, text, page) for keywords, name, text, page in self.CHUNKS if any(k in query for k in keywords)
        ]
        return hits[:top_k]


class ScopedFakeSearch(FakeSearch):
    """検索範囲（資料のグループ）を見る偽の検索。E2E の偽サーバー（tests/e2e_server.py）だけが使う。

    FakeSearch の資料に、グループ（zahard）を付ける。`collection` が渡されたら、そのグループの資料だけを返す
    （グループが違えば 0 件になるので、画面の「該当する資料がありません」の案内を確かめられる）。
    FakeSearch そのものは変えない（ほかのテストは、出典の collection が None であることを前提にしている）。
    """

    COLLECTION = "zahard"

    async def __call__(
        self, query: str, top_k: int = 2, collection: str | None = None, *, scope: Scope | None = None
    ) -> list[Hit]:
        hits = await super().__call__(query, top_k)
        if collection is not None and collection != self.COLLECTION:
            return []
        return [hit._replace(collection=self.COLLECTION) for hit in hits]


class GroupedFakeSearch:
    """資料のグループと、検索できる範囲（`scope`）の両方を見る偽の検索。アクセス制御のテスト（tests/test_access*.py）が使う。

    本物の `search_vector.search` と同じ規則: scope は必須、範囲の外のグループは返さない、グループの無い資料は全グループのときだけ。
    呼ばれた範囲は `calls` に残す（渡し忘れがないことを確かめる）。
    """

    # (キーワード, ファイル名, 本文, グループ)
    CHUNKS: tuple[tuple[str, str, str, str | None], ...] = (
        ("大気", "zahard_atmosphere.pdf", "ザーハードの大気は薄く、二酸化炭素が主成分である。", "zahard"),
        ("営業", "cafe_hours.pdf", "みなとコーヒーの営業時間は朝8時から夜8時までです。", "cafe"),
        ("申し込み", "dorm_apply.pdf", "入寮の申し込み期限は毎年3月末日です。", "guides"),
        ("未分類", "orphan.pdf", "どのグループにも属さない資料の本文です。", None),
    )
    SECRET = "ザーハードの大気は薄く"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, Scope]] = []

    async def __call__(
        self, query: str, top_k: int = 2, collection: str | None = None, *, scope: Scope | None = None
    ) -> list[Hit]:
        assert isinstance(scope, Scope), "scope が渡されていない"
        self.calls.append((query, collection, scope))
        hits = [
            Hit(name, text, 1, None, 0.9, group)
            for keyword, name, text, group in self.CHUNKS
            if keyword in query and scope.allows(group) and (collection is None or group == collection)
        ]
        return hits[:top_k]
