"""会話履歴（LangGraph の checkpointer）の保存先。

`DATABASE_URL` が空なら何もせず、従来どおりプロセス内メモリ（MemorySaver）で動く。設定されていれば、
起動時に接続プールを開いて Postgres の checkpointer を作り、終了時に閉じる（`app/main.py` の lifespan）。
接続できないまま起動してメモリに切り替わると、履歴が黙って失われるので、開けなければ起動を失敗させる。

Postgres 用のライブラリは、`DATABASE_URL` を設定したときだけ import する（空なら入っていなくても動く）。
"""

import asyncio
import logging
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)

DEFAULT_POOL_MAX_SIZE = 5
# 起動時に最初の接続を待つ上限。届かなければ起動を失敗させる
_OPEN_TIMEOUT_SECONDS = 10.0
# 1 回のリクエストが接続プールから接続を借りるのを待つ上限（LLM_TIMEOUT_SECONDS より十分短くする）
_CHECKOUT_TIMEOUT_SECONDS = 10.0
# 初回の setup() は、インスタンスが同時に起動すると、他の台が同じ物を作っている競合のエラーになる
# （実測: 8 並列で 7 件前後。種類は UniqueViolation が大半で、DuplicateObject と DuplicateTable も出る）。
# 先に走った 1 件が終われば後続は適用済みを確認するだけなので、短い間隔で数回やり直せば収まる
_SETUP_ATTEMPTS = 3
_SETUP_RETRY_SECONDS = 0.5

# lifespan の間だけ入る。get_checkpointer() の呼び出し側（routes.get_graph）が読む
_checkpointer: BaseCheckpointSaver | None = None


def database_url() -> str:
    """DATABASE_URL（空なら履歴はメモリ）。値は秘密情報（パスワードを含む）なのでログに出さない。"""
    return os.environ.get("DATABASE_URL", "").strip()


def pool_max_size() -> int:
    """DATABASE_POOL_MAX_SIZE（既定 5）。不正値・1 未満は既定値に戻る。

    インスタンスあたりの接続数の上限。`--max-instances` × この値が、DB の接続数の上限に収まるようにする。
    """
    try:
        value = int(os.environ.get("DATABASE_POOL_MAX_SIZE", ""))
    except ValueError:
        return DEFAULT_POOL_MAX_SIZE
    return value if value >= 1 else DEFAULT_POOL_MAX_SIZE


def get_checkpointer() -> BaseCheckpointSaver | None:
    """開いている Postgres の checkpointer。無ければ None（グラフは MemorySaver を使う）。"""
    return _checkpointer


def history_backend() -> str:
    """いま履歴をどこに置いているか。memory = プロセス内 / postgres = DATABASE_URL の DB。"""
    return "memory" if _checkpointer is None else "postgres"


async def _setup_with_retry(saver) -> None:
    # 他の台が同じ物を作っている最中に CREATE ... IF NOT EXISTS が重なると、この 3 つのどれかになる（実測）
    from psycopg.errors import DuplicateObject, DuplicateTable, UniqueViolation

    for attempt in range(1, _SETUP_ATTEMPTS + 1):
        try:
            await saver.setup()
            return
        except (UniqueViolation, DuplicateTable, DuplicateObject):
            if attempt == _SETUP_ATTEMPTS:
                raise
            logger.info("履歴テーブルの作成が他のインスタンスと重なったため、やり直します (attempt=%d)", attempt)
            # 全台が同じ間隔で待つと、また同時にぶつかる。待ち時間にばらつきを入れる（暗号用の乱数は要らない）
            await asyncio.sleep(_SETUP_RETRY_SECONDS * attempt * random.uniform(0.5, 1.5))


@asynccontextmanager
async def history_store() -> AsyncIterator[BaseCheckpointSaver | None]:
    """`DATABASE_URL` があれば Postgres の checkpointer を開いて渡し、抜けるときに閉じる。無ければ None。"""
    global _checkpointer

    url = database_url()
    if not url:
        logger.info("会話履歴の保存先: プロセス内メモリ（DATABASE_URL 未設定。再起動で消える）")
        yield None
        return

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    max_size = pool_max_size()
    pool = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        timeout=_CHECKOUT_TIMEOUT_SECONDS,
        open=False,
        # 取り出すときに生きているか確かめる（接続を落とす DB・プーラーの後でも、1 回目で失敗しない）
        check=AsyncConnectionPool.check_connection,
        kwargs={
            # checkpointer が要求する設定（autocommit と dict 形式の行）
            "autocommit": True,
            "row_factory": dict_row,
            # prepared statement を使わない。transaction mode のプーラー（Supabase の 6543 番など）でも動かす
            "prepare_threshold": None,
            "connect_timeout": int(_OPEN_TIMEOUT_SECONDS),
        },
    )
    try:
        await pool.open(wait=True, timeout=_OPEN_TIMEOUT_SECONDS)
        saver = AsyncPostgresSaver(pool)
        await _setup_with_retry(saver)
    except BaseException:
        await pool.close()
        raise

    _checkpointer = saver
    logger.info("会話履歴の保存先: Postgres（接続プールの上限 %d）", max_size)
    try:
        yield saver
    finally:
        _checkpointer = None
        await pool.close()
