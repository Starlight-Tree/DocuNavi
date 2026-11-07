"""実 API 検証用のサーバー起動（frontend/e2e/real-api、`npm run test:e2e:real-api`）。**本物の Gemini を呼ぶ（課金）。**

    python tests/real_api_server.py     # 127.0.0.1:8102（E2E_REAL_API_PORT で変える）

確かめたいこと: 主モデルで始めた会話を、フォールバックのモデルで続けられるか（逆方向も）。本物の 503 は狙って起こせないので、
**失敗だけを偽物にする**: 主モデルへの要求を 503 に差し替え（tests/real_api_faults.py。本物と同じ経路を通る）、
フォールバック先は本物の Gemini を呼ぶ。切り替えの操作と、Gemini への要求の記録は `/__real_api/*`（このサーバーだけにある）。

触れないもの: DB（DATABASE_URL を空にして、履歴はメモリ）、Supabase（AUTH_MODE=off）、資料の検索（偽の検索。埋め込みの API も呼ばない）。
使うのは Gemini の生成だけ。API キーは `.env` の GEMINI_API_KEY（app.main が読む。値は表示も記録もしない）。
`app/` は変えない。.env の GEMINI_MODEL・LLM_*・GEMINI_FALLBACK_* は、結果が .env に左右されないよう打ち消す。
キーが無い（モックモード）ときは、そのことを /health の mode で示して起動する（テストは理由を示してスキップする）。
"""

import os
import sys
from pathlib import Path

# app.main は import 時に load_dotenv() で .env を読むが、既存の環境変数は上書きしない。先に固定する（空は「既定」を意味する）
os.environ["DATABASE_URL"] = ""
os.environ["AUTH_MODE"] = "off"
os.environ["AUTH_BASE_URL"] = ""
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
# トレースを外部（LangSmith）へ送らない
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
for _name in (
    "GEMINI_MODEL",
    "GEMINI_FALLBACK_MODEL",
    "GEMINI_FALLBACK_HOLD_SECONDS",
    "LLM_HEDGE_DELAY_SECONDS",
    "LLM_ATTEMPT_TIMEOUT_SECONDS",
    "LLM_MAX_ATTEMPTS",
    "LLM_TIMEOUT_SECONDS",
):
    os.environ[_name] = ""

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import APIRouter  # noqa: E402

from app.agents import graph as graph_module  # noqa: E402
from app.agents import tools  # noqa: E402
from app.api import routes  # noqa: E402
from app.main import app  # noqa: E402
from app.search import vector as search_vector  # noqa: E402
from tests.fakes import FakeSearch  # noqa: E402
from tests.real_api_faults import FaultInjector, RecordingSearch  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8102
FALLBACK_DEFAULT = "gemini-3.5-flash-lite"

PRIMARY = os.environ.get("E2E_REAL_PRIMARY_MODEL", "").strip() or graph_module.DEFAULT_MODEL
FALLBACK = os.environ.get("E2E_REAL_FALLBACK_MODEL", "").strip() or FALLBACK_DEFAULT

faults = FaultInjector(PRIMARY)
search = RecordingSearch(FakeSearch())
control = APIRouter(prefix="/__real_api")


@control.get("/state")
async def state() -> dict[str, object]:
    return {
        "primary": PRIMARY,
        "fallback": FALLBACK,
        "primary_fail": faults.primary_fail,
        "mock_mode": graph_module.is_mock_mode(),
        "calls": faults.calls,
        "searches": search.searches,
    }


@control.post("/primary-fail")
async def set_primary_fail(body: dict[str, bool]) -> dict[str, bool]:
    """主モデルへの要求を 503 にする（on=true）/ 本物に戻す（on=false）。"""
    faults.primary_fail = bool(body.get("on"))
    return {"primary_fail": faults.primary_fail}


@control.post("/reset")
async def reset() -> dict[str, bool]:
    faults.reset()
    search.reset()
    return {"ok": True}


async def _skip_db_verify() -> None:
    """資料の検索を偽物にしているので、DB の資料と埋め込みの検査（起動時）は行わない。"""


def main() -> None:
    # 主モデルと、フォールバックの設定を明示する。グラフを作るときに、環境変数から読まれる
    os.environ["GEMINI_MODEL"] = PRIMARY
    os.environ["GEMINI_FALLBACK_MODEL"] = FALLBACK
    os.environ["GEMINI_FALLBACK_HOLD_SECONDS"] = "0"  # 質問ごとに主モデルから試す（前の質問の切り替えを持ち越さない）
    tools.search_chunks_async = search
    search_vector.verify = _skip_db_verify  # type: ignore[assignment]
    routes._graph_for.cache_clear()
    faults.install()
    app.include_router(control)
    uvicorn.run(app, host=HOST, port=int(os.environ.get("E2E_REAL_API_PORT", DEFAULT_PORT)), log_level="warning")


if __name__ == "__main__":
    main()
