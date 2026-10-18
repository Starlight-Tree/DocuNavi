import os

# app.main は import 時に load_dotenv() で .env を読む。実キーが入っていても
# テストが本物の Gemini を叩かないよう、空文字を先に設定してモックモードに固定する
# （load_dotenv は既存の環境変数を上書きしない）。
os.environ["GEMINI_API_KEY"] = ""
# Vertex AI が有効だとキー無しでも本物の LLM になるため、これも明示的に無効化する
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
# .env に DATABASE_URL があっても、テストが本物の DB（会話履歴の保存先）へ書かないよう、空にしてメモリ履歴に固定する
os.environ["DATABASE_URL"] = ""
# .env に SEARCH_BACKEND=vector があっても、テストが DB と埋め込みの API を使わないよう、BM25 に固定する
os.environ["SEARCH_BACKEND"] = "bm25"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agents import tools  # noqa: E402
from app.main import app  # noqa: E402
from tests.zahard_corpus import ZAHARD_TXT_DIR  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def no_pdfs(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """storage/pdf/ を、存在しないディレクトリに差し替える（PDF の資料群を外す。読み込みのキャッシュも、前後で捨てる）。"""
    monkeypatch.setattr(tools, "PDF_DIR", tmp_path / "pdf")
    tools._load_pdf_collections.cache_clear()
    yield
    tools._load_pdf_collections.cache_clear()


@pytest.fixture
def txt_dir(monkeypatch: pytest.MonkeyPatch):
    """storage/chunk/ を、元の .txt（evals/fixtures/zahard_txt/）に差し替える。ザーハードは PDF に移ったので、
    「.txt の資料群」の経路（まだコードに残っている）を確かめるテストだけが使う。読み込みのキャッシュも、前後で捨てる。"""
    monkeypatch.setattr(tools, "CHUNK_DIR", ZAHARD_TXT_DIR)
    tools.load_chunks.cache_clear()
    yield
    tools.load_chunks.cache_clear()


@pytest.fixture
def txt_collection_only(txt_dir, no_pdfs):
    """元の .txt の資料群 zahard だけを検索する状態（PDF の資料群は無い）。"""
