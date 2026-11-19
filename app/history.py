"""会話履歴（LangGraph の checkpointer）の保存先。

本番は `DB_BACKEND=pg`（Postgres + pgvector。Prisma Postgres）。起動時に `DATABASE_URL` へ接続して `PgSaver` の checkpointer を作り、終了時に閉じる
（`app/main.py` の lifespan）。本番の Cloud Run の環境変数が `DB_BACKEND` を明示しているかは、リポジトリからは未確認。
`d1`（`DB_BACKEND` のコード上の既定）は退役済みで、テストの偽 DB（`tests/d1_fakes.py` の偽の D1 Worker）のために残る。`D1_API_URL` があれば、
HTTP 経由でデータ用 Worker（撤去済み。テストでは偽物）に接続して `D1Saver` の checkpointer を作る。
DB の接続先（pg は `DATABASE_URL`、d1 は `D1_API_URL`）が空なら何もせず、従来どおりプロセス内メモリ（MemorySaver）で動く。
`DB_BACKEND` を指定しない場合も d1 になるので、`D1_API_URL` が空ならメモリ。
接続できないまま起動してメモリに切り替わると、履歴が黙って失われるので、開けなければ起動を失敗させる。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app import d1, pg

logger = logging.getLogger(__name__)

# lifespan の間だけ入る。get_checkpointer() の呼び出し側（routes.get_graph）が読む
_checkpointer: BaseCheckpointSaver | None = None
# 同じ DB への接続（pg は `app.pg.PgPool`、d1 は `app.d1.D1Pool`）。履歴のほかに、検索・資料の一覧・アクセス制御も借りる
_pool: Any | None = None


def database_url() -> str:
    """DB の場所。`pg`（本番）は DATABASE_URL（Postgres）、`DB_BACKEND=d1`（コード上の既定。退役済みで、テストの偽 DB 用）は D1_API_URL（データ用 Worker の URL）。空なら DB を使わず、履歴はメモリ。"""
    return pg.database_url() if pg.backend() == "pg" else d1.api_url()


def new_pool(url: str) -> Any:
    """`DB_BACKEND` に合わせた接続（`D1Pool` か `PgPool`）。open / close は呼び出し側。"""
    return pg.new_pool(url) if pg.backend() == "pg" else d1.D1Pool(url)


def get_checkpointer() -> BaseCheckpointSaver | None:
    """開いている DB の checkpointer（pg は `PgSaver`、d1 は `D1Saver`）。無ければ None（グラフは MemorySaver を使う）。"""
    return _checkpointer


def get_pool() -> Any | None:
    """開いている DB への接続（pg は `PgPool`、d1 は `D1Pool`）。lifespan の外、または DB の接続先（`database_url()`）が空なら None。"""
    return _pool


def history_backend() -> str:
    """いま履歴をどこに置いているか。memory = プロセス内 / pg = Postgres / d1 = データ用 Worker 経由の D1（退役済み。テストの偽 Worker 用）。"""
    return "memory" if _checkpointer is None else pg.backend()


@asynccontextmanager
async def history_store() -> AsyncIterator[BaseCheckpointSaver | None]:
    """DB の接続先（`database_url()`）があれば、接続を開いて checkpointer（pg は `PgSaver`、d1 は `D1Saver`）を渡し、抜けるときに閉じる。無ければ None。"""
    global _checkpointer, _pool

    url = database_url()
    if not url:
        logger.info("会話履歴の保存先: プロセス内メモリ（D1_API_URL 未設定。再起動で消える）")
        yield None
        return

    pool = new_pool(url)
    try:
        await pool.open()
    except BaseException:
        await pool.close()
        raise
    if pg.backend() == "pg":
        from app.pg_saver import PgSaver

        _checkpointer = PgSaver(pool)
    else:
        from app.d1_saver import D1Saver

        _checkpointer = D1Saver(pool)
    _pool = pool
    logger.info("会話履歴の保存先: %s", "Postgres" if pg.backend() == "pg" else "D1（データ用 Worker 経由）")
    try:
        yield _checkpointer
    finally:
        _checkpointer = None
        _pool = None
        await pool.close()
