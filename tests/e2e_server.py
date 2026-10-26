"""E2E（frontend/e2e、Playwright）用の API サーバー起動。偽の LLM と偽の検索で本物のアプリを立てる。

    python tests/e2e_server.py          # 127.0.0.1:8100（E2E_PORT で変える）

実 API（Gemini・埋め込み・DB・Supabase）は使わない。アプリ本体（app/）は変えず、tests/conftest.py と同じく
環境変数と差し替えだけで固定する: モックモード・メモリ履歴・認証なし、検索は ScopedFakeSearch（FakeSearch に資料のグループを付けたもの）、
グラフの LLM は SearchingMockLLM。GET /documents と GET /collections だけ、偽の DB（このファイルの _FakePool）から答える。
画面は配信しない。画面は別のポートの Next.js（frontend を E2E 用にビルドしたもの）で、本番（Vercel → Cloud Run）と同じ別オリジン。
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
from app.api import collections as collections_api  # noqa: E402
from app.api import documents as documents_api  # noqa: E402
from app.api import routes  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakes import ScopedFakeSearch, SearchingMockLLM  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8100

# 資料グループの画面（グループのチップ・カード）を確かめるための、偽の資料（DB は使わない）。
# (id, source, title, collection, status, pages, chunk_count)
_DOCUMENTS = (
    (1, "ザーハード1.pdf", "ザーハード 1", "zahard", "ready", 4, 12),
    (2, "ザーハード2.pdf", "ザーハード 2", "zahard", "ready", 3, 9),
    (3, "ザーハード3.pdf", "ザーハード 3", "zahard", "ready", 5, 14),
    (4, "cafe_store_guide.pdf", "店舗案内", "cafe", "ready", 2, 30),
    (5, "tsubasa_dorm_rules.pdf", "入寮規程", "guides", "ready", 6, 20),
    (6, "washer_manual.pdf", "洗濯乾燥機の取扱説明書", "guides", "ready", 8, 18),
    (7, "field_note.pdf", "フィールドノート", "memo", "ready", 1, 4),
)
# (name, label, description, sort_order)。memo は登録しない（フォルダを置いて取り込んだだけのグループ）。draft は資料が無い
_REGISTERED = (
    ("zahard", "設定資料", "ザーハードに関する設定資料です。", 10),
    ("cafe", "店舗案内", "店舗の案内や利用方法に関する資料です。", 20),
    ("guides", "規程・取扱説明書", "規程や機器の取扱説明書です。", 30),
    ("draft", "準備中の資料", "", 40),
)


def _document_rows() -> list[dict]:
    return [
        {
            "id": id_,
            "source": source,
            "title": title,
            "collection": collection,
            "status": status,
            "failure_reason": None,
            "pages": pages,
            "updated_at": None,
            "chunk_count": chunks,
            "embedded_count": chunks,
        }
        for id_, source, title, collection, status, pages, chunks in _DOCUMENTS
    ]


def _collection_rows() -> list[dict]:
    rows = []
    for name, label, description, order in _REGISTERED:
        mine = [d for d in _DOCUMENTS if d[3] == name]
        rows.append(
            {
                "name": name,
                "label": label,
                "description": description,
                "sort_order": order,
                "registered": True,
                "document_count": len(mine),
                "ready_count": sum(1 for d in mine if d[4] == "ready"),
            }
        )
    registered = {name for name, *_ in _REGISTERED}
    for name in sorted({d[3] for d in _DOCUMENTS} - registered):
        mine = [d for d in _DOCUMENTS if d[3] == name]
        rows.append(
            {
                "name": name,
                "label": None,
                "description": None,
                "sort_order": None,
                "registered": False,
                "document_count": len(mine),
                "ready_count": sum(1 for d in mine if d[4] == "ready"),
            }
        )
    return rows


class _FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict]:
        return self._rows


class _FakeConnection:
    async def execute(self, sql: str, params: dict | None = None) -> _FakeCursor:
        if "FROM document_collections" in sql:
            return _FakeCursor(_collection_rows())
        if "FROM documents d LEFT JOIN chunks" in sql:
            return _FakeCursor(_document_rows())
        raise AssertionError("E2E の偽の DB は、資料の一覧とグループの一覧だけに答える")


class _FakeConnectionContext:
    async def __aenter__(self) -> _FakeConnection:
        return _FakeConnection()

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakePool:
    """GET /documents と GET /collections の DB の偽物。履歴（チャットの会話）には使わない。"""

    def connection(self) -> _FakeConnectionContext:
        return _FakeConnectionContext()


def main() -> None:
    tools.search_chunks_async = ScopedFakeSearch()
    # 会話の履歴はメモリのまま。資料の一覧とグループの一覧だけ、偽の DB から返す
    collections_api.get_pool = lambda: _FakePool()
    documents_api.get_pool = lambda: _FakePool()
    graph_module.create_llm = lambda: SearchingMockLLM()
    routes._graph_for.cache_clear()
    uvicorn.run(app, host=HOST, port=int(os.environ.get("E2E_PORT", DEFAULT_PORT)), log_level="warning")


if __name__ == "__main__":
    main()
