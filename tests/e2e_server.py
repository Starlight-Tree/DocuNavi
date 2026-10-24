"""E2E（frontend/e2e、Playwright）用のサーバー起動。偽の LLM と偽の検索で本物のアプリを立てる。

    python tests/e2e_server.py          # 127.0.0.1:8100（E2E_PORT で変える）

実 API（Gemini・埋め込み・DB・Supabase）は使わない。アプリ本体（app/）は変えず、tests/conftest.py と同じく
環境変数と差し替えだけで固定する: モックモード・メモリ履歴・認証なし、検索は FakeSearch、グラフの LLM は SearchingMockLLM。
画面は frontend/dist-e2e（`npm run build:e2e`。VITE_SUPABASE_* を空にしたビルド）を、本番と同じ app/frontend.py の配信で返す。
E2E_DIST で、frontend/ の下の別のビルドを選べる（`dist-e2e-auth` = 偽の Supabase の URL を入れたビルド）。
選んだビルドが無ければ、frontend/dist に切り替えず失敗する（別のビルドを見てしまわないため）。

E2E_API_ONLY=1 のときは、画面を配信せず API だけを立てる（frontend-next/ の E2E 用。画面は別のポートの Next.js で、本番と同じ別オリジン）。
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
API_ONLY = os.environ.get("E2E_API_ONLY") == "1"
if API_ONLY:
    # app.main が import 時に読む。.env の値や既定の "*" ではなく、画面のオリジンだけに絞る
    os.environ["CORS_ORIGINS"] = os.environ.get("E2E_WEB_ORIGINS", "")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.agents import graph as graph_module  # noqa: E402
from app.agents import tools  # noqa: E402
from app.api import routes  # noqa: E402
from app.frontend import mount_frontend  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakes import FakeSearch, SearchingMockLLM  # noqa: E402

DEFAULT_DIST = "dist-e2e"
HOST = "127.0.0.1"
DEFAULT_PORT = 8100


def main() -> None:
    tools.search_chunks_async = FakeSearch()
    graph_module.create_llm = lambda: SearchingMockLLM()
    routes._graph_for.cache_clear()
    if not API_ONLY:
        _mount_built_frontend()
    uvicorn.run(app, host=HOST, port=int(os.environ.get("E2E_PORT", DEFAULT_PORT)), log_level="warning")


def _mount_built_frontend() -> None:
    dist_name = os.environ.get("E2E_DIST", DEFAULT_DIST)
    if not dist_name.startswith("dist-e2e") or "/" in dist_name or "\\" in dist_name or ".." in dist_name:
        sys.exit("E2E_DIST は dist-e2e で始まる、frontend/ 直下のフォルダー名にしてください。")
    dist_dir = ROOT / "frontend" / dist_name
    if not (dist_dir / "index.html").is_file():
        sys.exit(
            f"{dist_dir} がありません。先に `cd frontend && npm run build:e2e`（または build:e2e:auth）を実行してください。"
        )
    mount_frontend(app, dist_dir)  # app.main が既定の frontend/dist を差し込んだものを、E2E 用のビルドに置き換える


if __name__ == "__main__":
    main()
