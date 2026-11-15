"""資料の検索（app/search/vector.py と、tools.py の search_documents）のテスト。実 API・実 DB は使わない。

埋め込みの API と DB は、偽物（`embed_query` の差し替えと、偽の接続プール）に置き換える。conftest の偽の検索への差し替えは、
このファイルでは外す（`real_search`）。アプリの検索は DB のベクトル検索だけで、別の経路に戻らない。
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.agents import tools
from app.agents.tools import TOP_K, renumber_content, search_documents
from app.api import routes
from app.ingestion import embedding
from app.main import app
from app.scope import ACCESS_SCOPE_KEY, Scope
from app.search import vector as search_vector

pytestmark = pytest.mark.real_search

# 検索ツールは、検索できる範囲を config で受け取る（渡されなければ何も検索しない）。このファイルは全グループで呼ぶ
ALL_CONFIG = {"configurable": {ACCESS_SCOPE_KEY: Scope.ALL}}
VECTOR = [0.001 * i for i in range(1, embedding.DIMENSIONS + 1)]
SECRET = "AIzaSECRETKEY postgresql://user:pw@db.example.com/x https://example.com/leak"


def aoba_row(sim: float, content: str = "糸くずフィルターは、洗濯のたびに掃除する。") -> dict[str, Any]:
    return {"source": "aoba_aw820_manual.pdf", "heading": "お手入れ", "content": content, "sim": sim}


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class FakePool:
    """D1Pool の connection() / execute() / fetchall() / vectors_query() だけの偽物。呼び出し（SQL, 引数）を記録する。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.vector_calls: list[tuple[int, dict[str, Any] | None]] = []

    async def vectors_query(self, vector, top_k, filter=None):
        """Vectorize の偽物。行の `sim` を点として返す（チャンク ID は行に無ければ連番）。"""
        self.vector_calls.append((top_k, filter))
        if self.error is not None:
            raise self.error
        matches = []
        for i, row in enumerate(self.rows):
            row.setdefault("chunk_id", i + 1)
            row.setdefault("chunk_index", i)
            if "sim" in row:
                matches.append((str(row["chunk_id"]), float(row["sim"])))
        return matches

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any] | None = None) -> FakeCursor:
                pool.calls.append((sql, params))
                if pool.error is not None:
                    raise pool.error
                return FakeCursor(pool.rows)

        yield Conn()


class Embedder:
    """検索クエリの埋め込みの偽物（固定のベクトルを返す。API は呼ばない）。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.queries: list[str] = []

    async def __call__(self, query: str) -> list[float]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return VECTOR


@pytest.fixture
def vector_env(monkeypatch: pytest.MonkeyPatch):
    """埋め込みと DB を偽物に差し替える。(pool, embedder) を返す。"""
    pool = FakePool([aoba_row(0.81)])
    embedder = Embedder()
    monkeypatch.setattr(search_vector, "get_pool", lambda: pool)
    monkeypatch.setattr(search_vector, "embed_query", embedder)
    return pool, embedder


def ainvoke(query: str) -> str:
    return asyncio.run(search_documents.ainvoke({"query": query}, ALL_CONFIG))


# ---- ツールの定義 ----


def test_the_tool_definition_the_model_sees_is_unchanged():
    assert convert_to_openai_tool(search_documents) == {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": "登録された資料（設定資料・店舗案内など）を検索する。\n\n"
            "質問に答える前に必ず使う。query には質問の要点となるキーワードや短い文を渡す。",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    }


def test_the_tool_has_no_synchronous_path():
    # 同期で呼ぶと DB とネットワークを待ってしまう。グラフは .ainvoke で呼ぶ
    with pytest.raises(NotImplementedError):
        search_documents.invoke({"query": "大気"})


# ---- 出典のページ（LLM には見せず、画面にだけ渡す） ----


def call_tool(query: str):
    """ToolCall の形で呼ぶ（グラフの ToolNode と同じ）。ToolMessage の content が LLM に見える文面、artifact が画面に渡す出典。"""
    call = {"name": "search_documents", "args": {"query": query}, "id": "call-1", "type": "tool_call"}
    message = asyncio.run(search_documents.ainvoke(call, ALL_CONFIG))
    # `_text`（グラフが番号を数え直すための本文）は、画面に渡す出典ではない。`test_numbering_*` は取り除く前を確かめる
    if isinstance(message.artifact, list):
        message.artifact = [{k: v for k, v in item.items() if k != "_text"} for item in message.artifact]
    return message


def test_the_page_goes_to_the_artifact_and_never_into_the_text_the_model_sees(vector_env):
    pool, _ = vector_env
    pool.rows = [{**aoba_row(0.81), "page": 3, "page_end": 4}]

    message = call_tool("糸くずフィルターの掃除")

    assert message.content == "[1] aoba_aw820_manual.pdf\nお手入れ\n糸くずフィルターは、洗濯のたびに掃除する。"
    assert message.artifact == [
        {
            "name": "aoba_aw820_manual.pdf",
            "page": 3,
            "page_end": 4,
            "weak": False,
            "collection": None,
            "target": None,
            "document_id": None,
            "title": None,
        }
    ]
    # ページの値は、どの形でも文面に混ざらない
    assert "3" not in message.content and "p." not in message.content


def test_the_document_title_goes_to_the_artifact_and_never_into_the_text_the_model_sees(vector_env):
    pool, _ = vector_env
    pool.rows = [{**aoba_row(0.81), "title": "  青葉 AW-820 取扱説明書 "}]

    message = call_tool("糸くずフィルターの掃除")

    assert message.artifact[0]["title"] == "青葉 AW-820 取扱説明書"
    assert message.artifact[0]["name"] == "aoba_aw820_manual.pdf"  # キーはファイル名のまま
    assert "青葉" not in message.content
    ((sql, _),) = pool.calls
    assert "d.title AS title" in sql


@pytest.mark.parametrize("title", [None, "", "   ", 3])
def test_a_missing_or_blank_title_becomes_none_so_the_screen_falls_back_to_the_file_name(vector_env, title):
    pool, _ = vector_env
    pool.rows = [{**aoba_row(0.81), "title": title}]
    assert call_tool("糸くずフィルターの掃除").artifact[0]["title"] is None


def test_the_search_asks_the_database_for_the_page_columns(vector_env):
    pool, _ = vector_env
    ainvoke("糸くずフィルターの掃除")
    ((sql, _),) = pool.calls
    assert "c.page AS page" in sql and "c.page_end AS page_end" in sql


@pytest.mark.parametrize(
    ("page", "page_end", "expected"),
    [
        (2, None, (2, None)),
        (2, 3, (2, 3)),
        (2, 2, (2, None)),  # またがっていない
        (2, 1, (2, None)),  # 逆転した値は捨てる
        (0, 3, (None, None)),  # 1 未満のページは出さない（page_end も意味を失う）
        (None, 3, (None, None)),
        ("2", None, (None, None)),  # 整数でない値は出さない
    ],
)
def test_invalid_pages_become_none_so_the_screen_shows_nothing(vector_env, page, page_end, expected):
    pool, _ = vector_env
    pool.rows = [{**aoba_row(0.81), "page": page, "page_end": page_end}]
    assert call_tool("糸くずフィルターの掃除").artifact == [
        {
            "name": "aoba_aw820_manual.pdf",
            "page": expected[0],
            "page_end": expected[1],
            "weak": False,
            "collection": None,
            "target": None,
            "document_id": None,
            "title": None,
        }
    ]


def test_a_second_hit_far_below_the_first_is_marked_weak_but_still_goes_to_the_model(vector_env):
    pool, _ = vector_env
    pool.rows = [
        aoba_row(0.80),
        {**aoba_row(0.69), "content": "別の箇所"},
        {**aoba_row(0.75), "content": "近い箇所"},
    ]  # Vectorize の点で並べ直される

    message = call_tool("糸くずフィルターの掃除")

    # 1 位 0.80 との差が 0.06（> WEAK_GAP）の 2 件目は弱く、差が 0.05 ちょうどの 3 件目は弱くない。LLM に渡す文面は 3 件とも変わらない
    # 点の高い順に top_k（2）件。0.80 と 0.75（差 0.05 ちょうど）はどちらも弱くない
    assert [s["weak"] for s in message.artifact] == [False, False]
    assert message.content.count("] aoba_aw820_manual.pdf\n") == 2


def test_the_gap_is_measured_from_the_first_hit_and_a_close_second_is_not_weak():
    from app.agents.tools import WEAK_GAP, _sources
    from app.search.types import Hit

    assert WEAK_GAP == 0.05
    close = _sources([Hit("a.pdf", "A", similarity=0.80), Hit("b.pdf", "B", similarity=0.78)])
    assert [s["weak"] for s in close] == [False, False]
    far = _sources([Hit("a.pdf", "A", similarity=0.80), Hit("b.pdf", "B", similarity=0.70)])
    assert [s["weak"] for s in far] == [False, True]
    # 類似度が無い（偽の検索など）ときは、絞らない。1 件目は、いつも弱くない
    assert [s["weak"] for s in _sources([Hit("a.pdf", "A"), Hit("b.pdf", "B")])] == [False, False]
    assert _sources([]) == []


def test_a_tool_message_with_an_artifact_survives_the_checkpoint_serializer():
    # Postgres の履歴（AsyncPostgresSaver）が使う既定のシリアライザで、artifact つきの ToolMessage と sources が往復できる
    from langchain_core.messages import ToolMessage
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    sources = [
        {"name": "a.pdf", "page": 2, "page_end": 3, "weak": False},
        {"name": "b.pdf", "page": None, "page_end": None, "weak": True},
    ]
    state = {
        "messages": [ToolMessage(content="[a.pdf]\n本文", tool_call_id="c1", id="t1", artifact=sources)],
        "sources": sources,
    }
    serde = JsonPlusSerializer()

    restored = serde.loads_typed(serde.dumps_typed(state))

    assert restored["messages"][0].artifact == sources
    assert restored["messages"][0].content == "[a.pdf]\n本文"
    assert restored["sources"] == sources


def test_no_hit_gives_an_empty_artifact(vector_env):
    pool, _ = vector_env
    pool.rows = []
    message = call_tool("関係のない質問")
    assert message.content == tools._NOT_FOUND_TEXT
    assert message.artifact == []


# ---- 検索 ----


def test_ainvoke_returns_the_chunk_in_the_tool_format_and_reads_only_ready_documents(vector_env):
    pool, embedder = vector_env
    assert (
        ainvoke("糸くずフィルターの掃除")
        == "[1] aoba_aw820_manual.pdf\nお手入れ\n糸くずフィルターは、洗濯のたびに掃除する。"
    )
    assert embedder.queries == ["糸くずフィルターの掃除"]
    ((sql, params),) = pool.calls
    assert "json_each(%(ids)s)" in sql and "d.status = 'ready'" in sql
    assert "sources" not in params  # ローカルの PDF の一覧では絞らない。DB の検索できる資料の全部が対象
    assert params["model"] == embedding.MODEL and params["dims"] == embedding.DIMENSIONS and params["ids"] == [1]
    # ベクトルの検索は Vectorize（メタデータの絞り込みは空 = 全資料）。top_k より多めに取り、D1 の条件で絞り直す
    ((top, flt),) = pool.vector_calls
    assert top >= TOP_K and flt == {}


def test_chunk_without_heading_is_returned_as_its_content(vector_env):
    pool, _ = vector_env
    pool.rows = [{"source": "cafe_store_guide.pdf", "heading": None, "content": "毎週水曜日は定休日です。", "sim": 0.9}]
    assert ainvoke("定休日") == "[1] cafe_store_guide.pdf\n毎週水曜日は定休日です。"


def test_the_similarity_floor_is_a_coarse_one_and_ties_are_kept():
    # 0.65: 資料にない話題の質問は、しきい値では見分けられないので LLM に断らせる（評価は evals/README.md）。変えるときは評価をやり直す
    assert search_vector.MIN_SIMILARITY == 0.65


def test_results_below_the_floor_are_not_returned(vector_env):
    pool, _ = vector_env
    pool.rows = [aoba_row(search_vector.MIN_SIMILARITY - 1e-6)]
    assert ainvoke("糸くずフィルターの掃除") == tools._NOT_FOUND_TEXT
    pool.rows = [aoba_row(search_vector.MIN_SIMILARITY)]
    assert "[1] aoba_aw820_manual.pdf" in ainvoke("糸くずフィルターの掃除")  # 下限ちょうどは返す


def test_a_blank_query_or_a_zero_top_k_does_not_call_the_embedding_or_the_database(vector_env):
    pool, embedder = vector_env
    assert asyncio.run(search_vector.search("   ", TOP_K, scope=Scope.ALL)) == []
    assert asyncio.run(search_vector.search("大気", 0, scope=Scope.ALL)) == []
    assert embedder.queries == [] and pool.calls == []


# ---- 失敗しても別の経路には戻らない ----


@pytest.mark.parametrize("failure", ["embedding", "db", "no_pool"])
def test_failure_raises_a_fixed_error_without_falling_back(vector_env, monkeypatch, caplog, failure):
    pool, embedder = vector_env
    if failure == "embedding":
        embedder.error = RuntimeError(SECRET)
    elif failure == "db":
        pool.error = ConnectionError(SECRET)
    else:
        monkeypatch.setattr(search_vector, "get_pool", lambda: None)
    query = "糸くずフィルターの掃除"
    with caplog.at_level("WARNING"):
        with pytest.raises(search_vector.VectorSearchError) as info:
            ainvoke(query)
    for leaked in ("AIzaSECRETKEY", "postgresql://", "example.com", query):
        assert leaked not in str(info.value)
        assert leaked not in caplog.text


def test_cancellation_is_not_swallowed(vector_env):
    _, embedder = vector_env
    embedder.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        ainvoke("糸くずフィルターの掃除")  # クライアントの切断で取り消されたら、そのまま止まる


def test_a_search_failure_is_a_503_with_a_fixed_message(vector_env, searching_llm, client):
    _, embedder = vector_env
    embedder.error = RuntimeError(SECRET)
    res = client.post("/chat", json={"message": "糸くずフィルターの掃除", "thread_id": str(uuid.uuid4())})
    assert res.status_code == 503
    assert res.json() == {"detail": routes._DETAIL_SEARCH}
    for leaked in ("AIzaSECRETKEY", "postgresql://", "example.com"):
        assert leaked not in res.text


def test_a_search_failure_on_the_stream_is_a_503_error_chunk_with_a_fixed_message(vector_env, searching_llm, client):
    # /chat/stream は検索の前にレスポンスを始めているので、HTTP ではなく error チャンクで伝える
    _, embedder = vector_env
    embedder.error = RuntimeError(SECRET)
    res = client.post("/chat/stream", json={"message": "糸くずフィルターの掃除", "thread_id": str(uuid.uuid4())})
    assert res.status_code == 200
    events = [json.loads(line[len("data: ") :]) for line in res.text.split("\n\n") if line.startswith("data: {")]
    assert events[-2] == {"type": "data-failure", "data": {"status": 503}, "transient": True}
    assert events[-1] == {"type": "error", "errorText": routes._DETAIL_SEARCH}
    assert "searching" in [e["data"]["phase"] for e in events if e["type"] == "data-status"]
    for leaked in ("AIzaSECRETKEY", "postgresql://", "example.com"):
        assert leaked not in res.text


def test_chat_graph_uses_the_search_results_as_context(vector_env, searching_llm, client):
    pool, embedder = vector_env
    pool.rows = [aoba_row(0.86, "糸くずフィルターは、毎回の洗濯のあとで掃除します。")]
    body = client.post("/chat", json={"message": "糸くずフィルターの掃除", "thread_id": str(uuid.uuid4())}).json()
    assert embedder.queries == ["糸くずフィルターの掃除"]  # 偽の LLM が、発言をそのままクエリにして検索した
    assert "[1] aoba_aw820_manual.pdf\nお手入れ\n糸くずフィルターは、毎回の洗濯のあとで掃除します。" in body["context"]
    assert "aoba_aw820_manual.pdf" in body["reply"]  # 出典


# ---- 質問の埋め込みのクライアント ----


def test_the_embedding_client_is_always_the_api_key_one_even_when_the_llm_uses_vertex_ai(monkeypatch):
    # GOOGLE_GENAI_USE_VERTEXAI が真でも、埋め込みは API キーで呼ぶ（SDK は環境変数だけで Vertex を選ぶので、明示的に切る）
    from google import genai

    created: list[dict[str, Any]] = []

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setattr(genai, "Client", RecordingClient)
    monkeypatch.setattr(search_vector, "_client", None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    try:
        search_vector._get_client()
    finally:
        search_vector._client = None
    ((kwargs,),) = [(c,) for c in created]
    assert kwargs["vertexai"] is False and kwargs["api_key"] == "test-key"


def test_the_query_embedding_can_use_vertex_ai_without_any_key(monkeypatch):
    from google import genai

    created: list[dict[str, Any]] = []

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

    monkeypatch.setattr(genai, "Client", RecordingClient)
    monkeypatch.setattr(search_vector, "_client", None)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", "true")
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    try:
        search_vector._get_client()
    finally:
        search_vector._client = None
    ((kwargs,),) = [(c,) for c in created]
    assert kwargs["vertexai"] is True and "api_key" not in kwargs and kwargs["location"] == "global"


def test_vertex_ai_never_switches_to_the_paid_key(monkeypatch):
    # Vertex AI は ADC 認証で、無料キーの 429 という概念が無い。有料キーがあっても、429 で別経路に回らない
    class RateLimited(Exception):
        code = 429

    calls: list[str] = []

    async def fake_embed(client: Any, query: str) -> list[float]:
        calls.append(client)
        raise RateLimited()

    monkeypatch.setattr(search_vector, "_embed", fake_embed)
    monkeypatch.setattr(search_vector, "_get_client", lambda: "vertex")
    monkeypatch.setattr(search_vector, "_get_paid_client", lambda: "paid")
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", "true")
    monkeypatch.setenv("GEMINI_API_KEY_PAID", "paid-key")
    with pytest.raises(RateLimited):
        asyncio.run(search_vector.embed_query("質問"))
    assert calls == ["vertex"]


def test_the_embedding_needs_an_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(search_vector.VectorSearchError, match="GEMINI_API_KEY"):
        search_vector._api_key()


# ---- 起動の検査（verify / lifespan） ----


def doc_rows(**override: Any) -> list[dict[str, Any]]:
    row = {
        "id": 1,
        "source": "a.pdf",
        "status": "ready",
        "heading": "見出し",
        "content": "本文",
        "has_vector": True,
        "model": embedding.MODEL,
        "dims": embedding.DIMENSIONS,
        "input_hash": embedding.input_hash("見出し", "本文"),
    }
    row.update(override)
    return [row]


def run_verify(rows, monkeypatch, *, pool_open: bool = True) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(search_vector, "get_pool", lambda: FakePool(rows) if pool_open else None)
    asyncio.run(search_vector.verify())


def test_verify_accepts_a_database_whose_ready_documents_are_all_embedded(monkeypatch):
    run_verify(doc_rows(), monkeypatch)


def test_verify_does_not_read_local_files_and_ignores_documents_that_are_not_ready(monkeypatch):
    # 検索の対象は DB の ready の文書だけ。取り込みに失敗した文書は、埋め込みが無くても検査の対象外
    rows = doc_rows() + doc_rows(id=2, source="broken.pdf", status="failed", has_vector=False, content=None)
    run_verify(rows, monkeypatch)


@pytest.mark.parametrize(
    "override, message",
    [
        ({"has_vector": False}, "埋め込みが無い、または古い"),
        ({"model": "other-model"}, "埋め込みが無い、または古い"),
        ({"dims": 3072}, "埋め込みが無い、または古い"),
        ({"input_hash": "stale"}, "埋め込みが無い、または古い"),
        ({"content": None}, "チャンクがありません"),
    ],
)
def test_verify_rejects_a_ready_document_without_current_embeddings(monkeypatch, override, message):
    with pytest.raises(search_vector.VectorSearchError, match=message) as info:
        run_verify(doc_rows(**override), monkeypatch)
    assert "a.pdf" in str(info.value)


def test_verify_rejects_a_database_without_searchable_documents(monkeypatch):
    with pytest.raises(search_vector.VectorSearchError, match="検索できる資料が DB にありません"):
        run_verify([], monkeypatch)
    with pytest.raises(search_vector.VectorSearchError, match="検索できる資料が DB にありません"):
        run_verify(doc_rows(status="failed"), monkeypatch)


def test_verify_requires_the_database(monkeypatch):
    with pytest.raises(search_vector.VectorSearchError, match="D1_API_URL"):
        run_verify(doc_rows(), monkeypatch, pool_open=False)


def test_verify_requires_an_api_key(monkeypatch):
    monkeypatch.setattr(search_vector, "get_pool", lambda: FakePool(doc_rows()))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(search_vector.VectorSearchError, match="GEMINI_API_KEY"):
        asyncio.run(search_vector.verify())


def test_startup_in_mock_mode_needs_neither_the_database_nor_the_embedding(monkeypatch):
    async def must_not_run() -> None:
        raise AssertionError("モックモードでは、検索の検査をしない")

    monkeypatch.setattr(search_vector, "verify", must_not_run)
    with TestClient(app) as client:  # テストはモックモード（conftest）
        assert client.get("/health").status_code == 200


def test_startup_with_a_real_llm_fails_without_the_database(monkeypatch):
    # 本物の LLM なのに D1_API_URL が無い（検索できない）まま動き出さない
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(search_vector.VectorSearchError, match="D1_API_URL"):
        with TestClient(app):
            pass


# ---- 引用の番号と資料の ID ----


def test_the_document_id_goes_to_the_artifact_only(vector_env):
    pool, _ = vector_env
    pool.rows = [{**aoba_row(0.81), "document_id": 42}]
    message = call_tool("糸くずフィルターの掃除")
    assert message.artifact[0]["document_id"] == 42
    assert "42" not in message.content  # LLM には見せない


def test_the_tool_text_numbers_hits_from_one_and_the_artifact_keeps_the_text_for_renumbering(vector_env):
    pool, _ = vector_env
    pool.rows = [aoba_row(0.80), {**aoba_row(0.79), "content": "別の箇所"}]
    call = {
        "name": "search_documents",
        "args": {"query": "糸くずフィルターの掃除"},
        "id": "call-1",
        "type": "tool_call",
    }
    message = asyncio.run(search_documents.ainvoke(call, ALL_CONFIG))
    assert (
        message.content.startswith("[1] aoba_aw820_manual.pdf\n")
        and "\n\n[2] aoba_aw820_manual.pdf\n" in message.content
    )
    assert [item["_text"] for item in message.artifact] == [
        "お手入れ\n糸くずフィルターは、洗濯のたびに掃除する。",
        "お手入れ\n別の箇所",
    ]


def test_renumber_content_continues_the_numbers_and_refuses_when_the_text_is_missing():
    artifact = [{"name": "a.pdf", "_text": "本文A"}, {"name": "b.pdf", "_text": "本文B"}]
    assert renumber_content(artifact, 3) == "[3] a.pdf\n本文A\n\n[4] b.pdf\n本文B"
    assert renumber_content([], 1) is None  # 見つからなかったときの定型文は、そのまま残す
    assert renumber_content([{"name": "a.pdf"}], 1) is None
