"""起動時の DB の検査を、1 回の問い合わせにまとめる。

本番は `ACCESS_MODE=enforce` と本物の LLM なので、起動のたびに「ロール表の確認」（`app/access.py`）と「資料と埋め込みの確認」
（`app/search/vector.py`）の 2 回、DB に問い合わせていた。Prisma Postgres の Free は 1 SQL = 1 operation で数えるので、起動（コールドスタート）
ごとに 1 回ぶん減らす。検査の中身と判定は、それぞれのモジュールが持つ。ここでは SQL をつなぐだけで、判定を複製しない。

- 両方が有効で、接続プールが開いているときだけ、まとめる。それ以外（ACCESS_MODE=off、モックモード、接続なし）は、従来どおり単独で確かめる。
- まとめた問い合わせが失敗したら、単独の検査をやり直す（原因に合った固定のメッセージで、起動を失敗させるため）。失敗する起動だけが SQL を余分に使う。
- 検査の SQL を増やさない。足すなら、`PROBE_COLUMNS_SQL`（access）か `VERIFY_SQL`（vector）の中に混ぜる（起動のたびの operations が増える）。
"""

import logging
from typing import Any

from app import access
from app.agents.graph import is_mock_mode
from app.history import get_pool
from app.search import vector as search_vector

logger = logging.getLogger(__name__)

# アクセス制御の検査（1 行）と、資料の検査（資料・チャンクごとの行）を 1 本に。資料が無いときは、資料の列が NULL の 1 行になる。
COMBINED_SQL = (
    f"SELECT p.*, v.* FROM (SELECT {access.PROBE_COLUMNS_SQL}) p LEFT JOIN ({search_vector.VERIFY_SQL}) v ON TRUE"
)


async def _verify_separately(*, search: bool) -> None:
    await access.verify_access_config()
    if search:
        await search_vector.verify()


async def run_startup_checks() -> None:
    """起動時の DB の検査（利用資格・アクセス制御・資料の検索）。使えない状態なら、AccessConfigError / VectorSearchError で起動を失敗させる。

    利用資格（`MEMBERSHIP_MODE`）はアクセス制御（`ACCESS_MODE`）と独立で、有効なときだけ `app_members` を 1 回読む（off は何も読まない）。
    """
    membership = access.check_membership_config()  # 知らない MEMBERSHIP_MODE・匿名との組み合わせは、ここで失敗する
    await _run_data_checks()
    await access.verify_membership_config(membership)


async def _run_data_checks() -> None:
    mode = access.check_config()  # 知らない ACCESS_MODE・不正な TTL は、ここで失敗する
    search = not is_mock_mode()
    pool = get_pool()
    if mode == "off" or not search or pool is None:
        await _verify_separately(search=search)
        return
    search_vector.check_config()
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(COMBINED_SQL, {"default": access.DEFAULT_ROLE})
            rows: list[dict[str, Any]] = list(await cursor.fetchall())
    except Exception as exc:  # noqa: BLE001 - 型の名前だけを残す
        logger.warning("起動時の検査をまとめて実行できませんでした (%s)。個別に確かめ直します", type(exc).__name__)
        await _verify_separately(search=search)
        return
    if not rows:
        await _verify_separately(search=search)
        return
    access.check_probe_row(mode, rows[0])
    search_vector.check_rows(rows)
