"""LangSmith Studio（`langgraph dev`）用の入口。ローカル開発だけで使い、本番（Cloud Run）は import しない。

`langgraph.json` の `graphs.docunavi` が、この `graph` を指す。Studio のサーバーが履歴の保存先（checkpointer）を
差し込むので、ここでは渡さない。LLM は `build_graph()` の既定どおり: `GEMINI_API_KEY` があれば本物の Gemini、
無ければモック（資料を検索しない固定の返答）。本物で検索まで動かすには、ローカルの Postgres（`docker-compose.pg.yml`）の
`DATABASE_URL` と `DB_BACKEND=pg` が要る。

検索の範囲（`ACCESS_SCOPE_KEY`）は、本番では API が利用者ごとに config で渡す。Studio は渡さないので、そのままだと
検索が「何も見えない範囲」になり、いつも空になる（`app/agents/tools.py`）。ここで `Scope.ALL` を既定の config に足す。
ローカルの使い捨て DB を、開発者本人が見るためだけの設定で、本番の経路（`app/api/routes.py`）には入らない。
"""

from typing import Any

from dotenv import load_dotenv

load_dotenv()  # `.env` の LANGSMITH_* と GEMINI_API_KEY を、グラフを組み立てる前に読む

from app.agents.graph import build_graph  # noqa: E402
from app.scope import ACCESS_SCOPE_KEY, Scope  # noqa: E402


def prepare_for_studio(compiled: Any) -> Any:
    """Studio 用に、組み立て済みのグラフを整える（同じ物を返す）。"""
    # build_graph は checkpointer が無いと MemorySaver を付ける。LangGraph API は自前で履歴を持ち、独自の checkpointer を拒否するので外す
    compiled.checkpointer = None
    # 実行ごとの config に足される既定値。Studio の config に ACCESS_SCOPE_KEY が無くても、検索を全グループにする
    compiled.config = {**(compiled.config or {}), "configurable": {ACCESS_SCOPE_KEY: Scope.ALL}}
    return compiled


graph = prepare_for_studio(build_graph())
