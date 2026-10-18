import os

# app.main は import 時に load_dotenv() で .env を読む。実キーが入っていても
# テストが本物の Gemini を叩かないよう、空文字を先に設定してモックモードに固定する
# （load_dotenv は既存の環境変数を上書きしない）。
os.environ["GEMINI_API_KEY"] = ""
# Vertex AI が有効だとキー無しでも本物の LLM になるため、これも明示的に無効化する
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
# .env に DATABASE_URL があっても、テストが本物の DB（会話履歴の保存先）へ書かないよう、空にしてメモリ履歴に固定する
os.environ["DATABASE_URL"] = ""

import asyncio  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agents import graph as graph_module  # noqa: E402
from app.agents import tools  # noqa: E402
from app.api import routes  # noqa: E402
from app.main import app  # noqa: E402
from scripts import bm25_baseline  # noqa: E402
from tests.fakes import SearchingMockLLM  # noqa: E402
from tests.zahard_corpus import ZAHARD_TXT_DIR  # noqa: E402


@pytest.fixture(autouse=True)
def fake_search(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """アプリの検索（DB のベクトル検索）を、テスト用の偽物に差し替える。DB も埋め込みの API も使わない。

    偽物は、storage/pdf/ の PDF を読む BM25 の基準線（scripts/bm25_baseline.py）。決まった入力に決まった出力を返す。
    本物の検索（app/search_vector.py）を確かめるテストは、`@pytest.mark.real_search` で差し替えを外し、DB と埋め込みを自分で偽物にする。
    """
    if request.node.get_closest_marker("real_search"):
        return

    async def fake_search_chunks(query: str, top_k: int = tools.TOP_K) -> list[tuple[str, str]]:
        return await asyncio.to_thread(bm25_baseline.search_chunks, query, top_k)

    monkeypatch.setattr(tools, "search_chunks_async", fake_search_chunks)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def searching_llm(monkeypatch: pytest.MonkeyPatch):
    """アプリのグラフの LLM を、ツール（偽の検索）を呼ぶ偽物（SearchingMockLLM）にする。アプリのモックは固定の返答で、検索しない。
    グラフはキャッシュされるので、前後で捨てる。"""
    monkeypatch.setattr(graph_module, "create_llm", lambda: SearchingMockLLM())
    routes._graph_for.cache_clear()
    yield
    routes._graph_for.cache_clear()


@pytest.fixture
def no_pdfs(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """BM25 の基準線の storage/pdf/ を、存在しないディレクトリに差し替える（PDF の資料群を外す。読み込みのキャッシュも、前後で捨てる）。"""
    monkeypatch.setattr(bm25_baseline, "PDF_DIR", tmp_path / "pdf")
    bm25_baseline._load_pdf_collections.cache_clear()
    yield
    bm25_baseline._load_pdf_collections.cache_clear()


@pytest.fixture
def txt_dir(monkeypatch: pytest.MonkeyPatch):
    """storage/chunk/ を、元の .txt（evals/fixtures/zahard_txt/）に差し替える。ザーハードは PDF に移ったので、
    「.txt の資料群」の経路（まだコードに残っている）を確かめるテストだけが使う。読み込みのキャッシュも、前後で捨てる。"""
    monkeypatch.setattr(bm25_baseline, "CHUNK_DIR", ZAHARD_TXT_DIR)
    bm25_baseline.load_chunks.cache_clear()
    yield
    bm25_baseline.load_chunks.cache_clear()


@pytest.fixture
def txt_collection_only(txt_dir, no_pdfs):
    """元の .txt の資料群 zahard だけを検索する状態（PDF の資料群は無い）。"""
