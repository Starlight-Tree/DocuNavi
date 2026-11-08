"""LangSmith Studio（`langgraph dev`）用の入口。ローカル開発だけで使い、本番（Cloud Run）は import しない。

`langgraph.json` の `graphs.docunavi` が、この `graph` を指す。Studio のサーバーが履歴の保存先（checkpointer）を
差し込むので、ここでは渡さない。LLM は `build_graph()` の既定どおり: `GEMINI_API_KEY` があれば本物の Gemini、
無ければモック（資料を検索しない固定の返答）。本物で検索まで動かすには、ローカルの Postgres（`docker-compose.pg.yml`）の
`DATABASE_URL` と `DB_BACKEND=pg` が要る。
"""

from dotenv import load_dotenv

load_dotenv()  # `.env` の LANGSMITH_* と GEMINI_API_KEY を、グラフを組み立てる前に読む

from app.agents.graph import build_graph  # noqa: E402

graph = build_graph()
# build_graph は checkpointer が無いと MemorySaver を付ける。LangGraph API は自前で履歴を持ち、独自の checkpointer を拒否するので外す
graph.checkpointer = None
