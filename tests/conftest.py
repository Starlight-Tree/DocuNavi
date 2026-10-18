import os

# app.main は import 時に load_dotenv() で .env を読む。実キーが入っていても
# テストが本物の Gemini を叩かないよう、空文字を先に設定してモックモードに固定する
# （load_dotenv は既存の環境変数を上書きしない）。
os.environ["GEMINI_API_KEY"] = ""
# Vertex AI が有効だとキー無しでも本物の LLM になるため、これも明示的に無効化する
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
# .env に DATABASE_URL があっても、テストが本物の DB（会話履歴の保存先）へ書かないよう、空にしてメモリ履歴に固定する
os.environ["DATABASE_URL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agents import graph as graph_module  # noqa: E402
from app.agents import tools  # noqa: E402
from app.api import routes  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakes import FakeSearch, SearchingMockLLM  # noqa: E402


@pytest.fixture(autouse=True)
def fake_search(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """アプリの検索（DB のベクトル検索）を、テスト用の偽物に差し替える。DB も埋め込みの API も使わない。

    偽物は tests/fakes.py の FakeSearch（テスト内の小さな資料をキーワードで引く。PDF も BM25 も読まない）。
    本物の検索（app/search_vector.py）を確かめるテストは、`@pytest.mark.real_search` で差し替えを外し、DB と埋め込みを自分で偽物にする。
    """
    if request.node.get_closest_marker("real_search"):
        return

    monkeypatch.setattr(tools, "search_chunks_async", FakeSearch())


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
