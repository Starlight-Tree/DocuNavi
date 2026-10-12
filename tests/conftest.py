import os

# app.main は import 時に load_dotenv() で .env を読む。実キーが入っていても
# テストが本物の Gemini を叩かないよう、空文字を先に設定してモックモードに固定する
# （load_dotenv は既存の環境変数を上書きしない）。
os.environ["GEMINI_API_KEY"] = ""
# Vertex AI が有効だとキー無しでも本物の LLM になるため、これも明示的に無効化する
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
