"""SEARCH_BACKEND=vector（app/search_vector.py と、tools.py の非同期の検索）のテスト。実 API・実 DB は使わない。

埋め込みの API と DB は、偽物（`embed_query` の差し替えと、偽の接続プール）に置き換える。グラフが使う `.ainvoke` の側を通す
（同期の `.invoke` は常に BM25）。既定の bm25 のときの動きが変わらないことも固定する。
"""

import asyncio
import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.utils.function_calling import convert_to_openai_tool

from app import search_vector
from app.agents import tools
from app.agents.tools import TOP_K, search_documents
from app.ingestion import embedding
from app.main import app

PDF_FILES = ["aoba_aw820_manual.pdf", "cafe_store_guide.pdf", "tsubasa_dorm_rules.pdf"]
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
    """psycopg_pool.AsyncConnectionPool の connection() / execute() / fetchall() だけの偽物。呼び出し（SQL, 引数）を記録する。"""

    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any]) -> FakeCursor:
                pool.calls.append((sql, params))
                if pool.error is not None:
                    raise pool.error
                return FakeCursor(pool.rows)

        yield Conn()


class Embedder:
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
    """SEARCH_BACKEND=vector にして、埋め込みと DB を偽物に差し替える。(pool, embedder) を返す。"""
    monkeypatch.setenv("SEARCH_BACKEND", "vector")
    monkeypatch.delenv("SEARCH_SCOPE", raising=False)
    pool = FakePool([aoba_row(0.81)])
    embedder = Embedder()
    monkeypatch.setattr(search_vector, "get_pool", lambda: pool)
    monkeypatch.setattr(search_vector, "embed_query", embedder)
    return pool, embedder


def ainvoke(query: str) -> str:
    return asyncio.run(search_documents.ainvoke({"query": query}))


# ---- 既定（bm25）は変わらない ----


def test_default_backend_ainvoke_is_the_same_as_invoke(monkeypatch):
    async def must_not_run(query: str):
        raise AssertionError("bm25 のときは埋め込みを呼ばない")

    monkeypatch.setattr(search_vector, "embed_query", must_not_run)
    for query in ("みなとコーヒーの定休日", "大気", "xyzqwerty"):
        assert ainvoke(query) == search_documents.invoke({"query": query})


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


@pytest.mark.parametrize("value, expected", [(None, "bm25"), ("", "bm25"), (" Vector ", "vector"), ("bm25", "bm25")])
def test_search_backend_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("SEARCH_BACKEND", raising=False)
    else:
        monkeypatch.setenv("SEARCH_BACKEND", value)
    assert search_vector.search_backend() == expected


def test_unknown_backend_is_an_error(monkeypatch):
    monkeypatch.setenv("SEARCH_BACKEND", "hybrid")
    with pytest.raises(ValueError, match="SEARCH_BACKEND"):
        search_vector.search_backend()


# ---- vector: 非同期の検索（.ainvoke） ----


def test_ainvoke_returns_the_chunk_in_the_same_format_and_queries_the_scoped_sources(vector_env):
    pool, embedder = vector_env
    assert ainvoke("糸くずフィルターの掃除") == "[aoba_aw820_manual.pdf]\nお手入れ\n糸くずフィルターは、洗濯のたびに掃除する。"
    assert embedder.queries == ["糸くずフィルターの掃除"]
    (sql, params), = pool.calls
    assert "OPERATOR(extensions.<=>)" in sql and "d.status = 'ready'" in sql
    assert sorted(params["sources"]) == PDF_FILES  # 検索する資料群の PDF 全部。.txt は含まない
    assert params["k"] == TOP_K and params["model"] == embedding.MODEL and params["dims"] == embedding.DIMENSIONS
    assert params["q"] == embedding.vector_literal(VECTOR) and params["q"].count(",") == embedding.DIMENSIONS - 1


def test_chunk_without_heading_is_returned_as_its_content(vector_env):
    pool, _ = vector_env
    pool.rows = [{"source": "cafe_store_guide.pdf", "heading": None, "content": "毎週水曜日は定休日です。", "sim": 0.9}]
    assert ainvoke("定休日") == "[cafe_store_guide.pdf]\n毎週水曜日は定休日です。"


def test_results_below_the_threshold_are_not_returned(vector_env):
    pool, _ = vector_env
    below = search_vector.MIN_SIMILARITY - 1e-6
    pool.rows = [aoba_row(below)]
    assert ainvoke("糸くずフィルターの掃除") == tools._NOT_FOUND_TEXT  # 弱い一致も返さない（既知の制約: 答えのある質問を断ることがある）
    pool.rows = [aoba_row(search_vector.MIN_SIMILARITY)]
    assert "[aoba_aw820_manual.pdf]" in ainvoke("糸くずフィルターの掃除")  # しきい値ちょうどは返す


def test_txt_collection_stays_on_bm25_and_is_interleaved_first(vector_env):
    hits = asyncio.run(tools.search_chunks_async("大気"))
    assert [name for name, _ in hits] == ["ザーハード1.txt", "aoba_aw820_manual.pdf"]
    assert hits[0] == tools.search_chunks("大気", corpus=tools.load_chunks())[0]  # .txt 側は従来の BM25 と同じ文


def test_scope_without_pdf_collections_never_touches_the_vector_path(vector_env, monkeypatch):
    pool, embedder = vector_env
    monkeypatch.setenv("SEARCH_SCOPE", "zahard")
    assert ainvoke("大気") == search_documents.invoke({"query": "大気"})
    assert embedder.queries == [] and pool.calls == []


def test_scope_with_only_one_pdf_collection_searches_only_its_files(vector_env, monkeypatch):
    pool, _ = vector_env
    monkeypatch.setenv("SEARCH_SCOPE", "cafe")
    pool.rows = []
    assert ainvoke("大気") == tools._NOT_FOUND_TEXT  # zahard は範囲外。.txt の文は返らない
    assert pool.calls[0][1]["sources"] == ["cafe_store_guide.pdf"]


def test_sync_invoke_is_always_bm25(vector_env):
    pool, embedder = vector_env
    assert "[aoba_aw820_manual.pdf]" in search_documents.invoke({"query": "洗濯槽のお手入れ"})
    assert embedder.queries == [] and pool.calls == []


# ---- 失敗したら、その呼び出しだけ BM25 ----


@pytest.mark.parametrize("failure", ["embedding", "db", "no_pool"])
def test_failure_falls_back_to_bm25_for_that_call_and_logs_only_the_type(vector_env, monkeypatch, caplog, failure):
    pool, embedder = vector_env
    if failure == "embedding":
        embedder.error = RuntimeError(SECRET)
    elif failure == "db":
        pool.error = ConnectionError(SECRET)
    else:
        monkeypatch.setattr(search_vector, "get_pool", lambda: None)
    query = "糸くずフィルターの掃除"
    with caplog.at_level(logging.WARNING):
        output = ainvoke(query)
    assert output == search_documents.invoke({"query": query})  # BM25 の結果（全資料群）と同じ
    assert "[aoba_aw820_manual.pdf]" in output
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "ベクトル検索に失敗したため" in warnings[0].getMessage()
    logged = warnings[0].getMessage() + str(warnings[0].args)
    for leaked in ("AIzaSECRETKEY", "postgresql://", "example.com", query):
        assert leaked not in logged


def test_cancellation_is_not_swallowed_by_the_fallback(vector_env):
    _, embedder = vector_env
    embedder.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        ainvoke("糸くずフィルターの掃除")  # クライアントの切断で取り消されたら、BM25 に戻さずそのまま止まる


# ---- 起動の検査（verify / lifespan） ----


def doc_rows(tmp_path: Path, **override: Any) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF fake")
    row = {
        "source": "a.pdf", "status": "ready", "document_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        "heading": "見出し", "content": "本文", "has_vector": True, "model": embedding.MODEL,
        "dims": embedding.DIMENSIONS, "input_hash": embedding.input_hash("見出し", "本文"),
    }
    row.update(override)
    return {"a.pdf": path}, [row]


def run_verify(files, rows, monkeypatch, *, pool_open: bool = True, mock: bool = False, vertex: bool = False):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(search_vector, "get_pool", lambda: FakePool(rows) if pool_open else None)
    asyncio.run(search_vector.verify(files, mock=mock, vertex=vertex))


def test_verify_accepts_a_matching_database(tmp_path, monkeypatch):
    files, rows = doc_rows(tmp_path)
    run_verify(files, rows, monkeypatch)


@pytest.mark.parametrize(
    "override, message",
    [
        ({"document_hash": "0" * 64}, "取り込み後にファイルが変わっています"),
        ({"status": "failed"}, "取り込みに失敗した状態です"),
        ({"has_vector": False}, "埋め込みが無い、または古い"),
        ({"model": "other-model"}, "埋め込みが無い、または古い"),
        ({"dims": 3072}, "埋め込みが無い、または古い"),
        ({"input_hash": "stale"}, "埋め込みが無い、または古い"),
        ({"content": None}, "チャンクがありません"),
    ],
)
def test_verify_rejects_a_database_that_does_not_match(tmp_path, monkeypatch, override, message):
    files, rows = doc_rows(tmp_path, **override)
    with pytest.raises(search_vector.VectorSearchError, match=message) as info:
        run_verify(files, rows, monkeypatch)
    assert "a.pdf" in str(info.value)


def test_verify_rejects_a_missing_document(tmp_path, monkeypatch):
    files, _ = doc_rows(tmp_path)
    with pytest.raises(search_vector.VectorSearchError, match="DB に取り込まれていません"):
        run_verify(files, [], monkeypatch)


@pytest.mark.parametrize("kwargs, message", [
    ({"mock": True}, "モックモード"),
    ({"vertex": True}, "Vertex AI"),
    ({"pool_open": False}, "DATABASE_URL"),
])
def test_verify_rejects_unsupported_modes(tmp_path, monkeypatch, kwargs, message):
    files, rows = doc_rows(tmp_path)
    with pytest.raises(search_vector.VectorSearchError, match=message):
        run_verify(files, rows, monkeypatch, **kwargs)


def test_verify_requires_an_api_key(tmp_path, monkeypatch):
    files, rows = doc_rows(tmp_path)
    monkeypatch.setattr(search_vector, "get_pool", lambda: FakePool(rows))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(search_vector.VectorSearchError, match="GEMINI_API_KEY"):
        asyncio.run(search_vector.verify(files, mock=False, vertex=False))


def test_startup_fails_instead_of_silently_using_bm25(monkeypatch):
    monkeypatch.setenv("SEARCH_BACKEND", "vector")  # テストはモックモードなので、使えない状態
    with pytest.raises(search_vector.VectorSearchError, match="モックモード"):
        with TestClient(app):
            pass


def test_startup_with_the_default_backend_is_unchanged():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


# ---- グラフ（ToolNode の非同期実行）まで通す ----


def test_chat_graph_uses_the_vector_results_as_context(vector_env, client):
    pool, embedder = vector_env
    pool.rows = [aoba_row(0.86, "糸くずフィルターは、毎回の洗濯のあとで掃除します。")]
    body = client.post("/chat", json={"message": "糸くずフィルターの掃除", "thread_id": str(uuid.uuid4())}).json()
    assert embedder.queries == ["糸くずフィルターの掃除"]  # モックの LLM が、発言をそのままクエリにして検索した
    assert "[aoba_aw820_manual.pdf]\nお手入れ\n糸くずフィルターは、毎回の洗濯のあとで掃除します。" in body["context"]
    assert "aoba_aw820_manual.pdf" in body["reply"]  # 出典
