"""E2E（frontend-next/e2e、Playwright）用の API サーバー起動。偽の LLM と偽の検索で本物のアプリを立てる。

    python tests/e2e_server.py          # 127.0.0.1:8100（E2E_PORT で変える）

実 API（Gemini・埋め込み・DB・Supabase）は使わない。アプリ本体（app/）は変えず、tests/conftest.py と同じく
環境変数と差し替えだけで固定する: モックモード・メモリ履歴・認証なし、検索は FakeSearch、グラフの LLM は SearchingMockLLM。
画面は配信しない。画面は別のポートの Next.js（frontend-next を E2E 用にビルドしたもの）で、本番（Vercel → Cloud Run）と同じ別オリジン。
CORS は E2E_WEB_ORIGINS（カンマ区切りのオリジン）だけに許す。画面側のオリジンが違えば、ブラウザーが API を呼べず、テストが失敗する。
"""

import os
import sys
from pathlib import Path

# app.main は import 時に load_dotenv() で .env を読むが、既存の環境変数は上書きしない。実キーが入っていても使わないよう、先に固定する
os.environ["GEMINI_API_KEY"] = ""
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
os.environ["DATABASE_URL"] = ""
os.environ["AUTH_MODE"] = "off"
# app.main が import 時に読む。.env の値や既定の "*" ではなく、画面のオリジンだけに絞る
os.environ["CORS_ORIGINS"] = os.environ.get("E2E_WEB_ORIGINS", "")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.agents import graph as graph_module  # noqa: E402
from app.agents import tools  # noqa: E402
from app.api import routes  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakes import FakeSearch, SearchingMockLLM  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8100


def main() -> None:
    tools.search_chunks_async = FakeSearch()
    graph_module.create_llm = lambda: SearchingMockLLM()
    routes._graph_for.cache_clear()
    uvicorn.run(app, host=HOST, port=int(os.environ.get("E2E_PORT", DEFAULT_PORT)), log_level="warning")


if __name__ == "__main__":
    main()
