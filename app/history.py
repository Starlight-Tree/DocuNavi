"""会話履歴（LangGraph の checkpointer）の保存先。

`D1_API_URL` が空なら何もせず、従来どおりプロセス内メモリ（MemorySaver）で動く。設定されていれば、
起動時にデータ用 Worker（`workers/api/`）へ接続して D1 の checkpointer を作り、終了時に閉じる（`app/main.py` の lifespan）。
接続できないまま起動してメモリに切り替わると、履歴が黙って失われるので、開けなければ起動を失敗させる。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app import d1

logger = logging.getLogger(__name__)

# lifespan の間だけ入る。get_checkpointer() の呼び出し側（routes.get_graph）が読む
_checkpointer: BaseCheckpointSaver | None = None
# 同じ D1 への接続（`app.d1.D1Pool`）。履歴のほかに、検索・資料の一覧・アクセス制御も借りる
_pool: Any | None = None


def database_url() -> str:
    """D1_API_URL（データ用 Worker の URL。空なら DB を使わず、履歴はメモリ）。"""
    return d1.api_url()


def get_checkpointer() -> BaseCheckpointSaver | None:
    """開いている D1 の checkpointer。無ければ None（グラフは MemorySaver を使う）。"""
    return _checkpointer


def get_pool() -> Any | None:
    """開いている D1 への接続（`app.d1.D1Pool`）。lifespan の外、または D1_API_URL が空なら None。"""
    return _pool


def history_backend() -> str:
    """いま履歴をどこに置いているか。memory = プロセス内 / d1 = データ用 Worker 経由の D1。"""
    return "memory" if _checkpointer is None else "d1"


@asynccontextmanager
async def history_store() -> AsyncIterator[BaseCheckpointSaver | None]:
    """`D1_API_URL` があれば D1 の checkpointer を開いて渡し、抜けるときに閉じる。無ければ None。"""
    global _checkpointer, _pool

    url = database_url()
    if not url:
        logger.info("会話履歴の保存先: プロセス内メモリ（D1_API_URL 未設定。再起動で消える）")
        yield None
        return

    from app.d1_saver import D1Saver

    pool = d1.D1Pool(url)
    try:
        await pool.open()
    except BaseException:
        await pool.close()
        raise
    _checkpointer = D1Saver(pool)
    _pool = pool
    logger.info("会話履歴の保存先: D1（データ用 Worker 経由）")
    try:
        yield _checkpointer
    finally:
        _checkpointer = None
        _pool = None
        await pool.close()
