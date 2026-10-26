"""ロール別の資料グループ制御（ロードマップ ⑧）。利用者ごとに、検索・回答・出典・本文・PDF・関係図・会話の表示が届くグループを決める。

- 見えるグループ = 利用者のロール（`user_roles`）が持つグループ（`role_collection_access`）の和集合。ロールが 1 つも無い利用者
  （`AUTH_MODE=off` の匿名を含む）は `default` ロール。キャッシュは持たず、リクエストごとに引く（権限の変更が次のリクエストから効く）。
- 権限の判断はサーバーだけで行う（LLM には任せない）。検索は **検索の前に** SQL で絞る（結果を取ってから消さない）。
- `ACCESS_MODE`: `off`（既定。制限なし。DB も読まない）/ `shadow`（制限せず、enforce なら何件が絞り込まれるかをログに出す）/
  `enforce`（制限する）。知らない値は起動を失敗させる。enforce で権限を引けなければ 503（制限なしには倒れない）、shadow では制限なしで続ける。
- 見えないものは「無い」のと同じに見せる: 資料は 404、グループの指定は 403、見えるグループが 0 件なら 403。文はすべて固定文で、
  見えないグループの名前を載せない。過去の会話は表示するが、見えないグループの検索結果に基づく回答は伏せる（`mask_hidden_turns`）。
- `docunavi-mcp`（`ops/mcp_search`）は運用者専用で、`Scope.ALL` を明示して全グループを検索する。このモジュールは通らない。
- 利用者へのロールの割り当て（`user_roles`）は、管理画面ができるまで（フェーズ 4）、運用者が SQL で行う。
"""

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.auth import current_user
from app.history import get_pool
from app.scope import Scope

logger = logging.getLogger(__name__)

ACCESS_MODES = ("off", "shadow", "enforce")
DEFAULT_ROLE = "default"

# クライアントへ返す固定メッセージ。見えないグループの名前・存在を載せない
DETAIL_FORBIDDEN_COLLECTION = "指定された資料グループは利用できません。"
DETAIL_NO_COLLECTIONS = "利用できる資料がありません。"
DETAIL_UNAVAILABLE = "資料の利用権限を確認できませんでした。しばらく待ってから再試行してください。"
# 過去の会話で、いま見えないグループの検索結果に基づく部分の代わりに出す文
MASKED_TOOL_TEXT = "（この検索結果は、現在利用できない資料に基づくため表示できません）"
MASKED_ANSWER_TEXT = "この回答は、現在利用できない資料に基づいているため表示できません。"

# 利用者が見えるグループ。user_roles が 1 件も無い利用者（user_id が NULL の匿名も）は default ロール
_VISIBLE_SQL = (
    "SELECT DISTINCT a.collection AS collection FROM role_collection_access a "
    "WHERE a.role IN (SELECT r.role FROM user_roles r WHERE r.user_id = %(uid)s::uuid) "
    "OR (a.role = %(default)s AND NOT EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = %(uid)s::uuid))"
)
# いま存在する全グループ。登録済み（document_collections）と、資料にだけあるもの（documents.collection）の両方
_KNOWN_SQL = (
    "SELECT name AS collection FROM document_collections "
    "UNION SELECT collection FROM documents WHERE collection IS NOT NULL"
)
_PROBE_SQL = (
    "SELECT (SELECT count(*) FROM roles WHERE name = %(default)s) AS default_roles, "
    "(SELECT count(*) FROM role_collection_access WHERE role = %(default)s) AS default_collections, "
    "(SELECT count(*) FROM user_roles) AS assignments"
)


class AccessConfigError(RuntimeError):
    """アクセス制御の設定が正しくない。起動を失敗させる（メッセージに秘密は含めない）。"""


class AccessUnavailable(Exception):
    """利用者の見えるグループを引けない（DB が無い・表が無い・接続の失敗）。"""


def access_mode() -> str:
    """ACCESS_MODE（off / shadow / enforce。未設定・空は off）。知らない値は、黙って off にせず例外にする。"""
    value = os.environ.get("ACCESS_MODE", "").strip().lower() or "off"
    if value not in ACCESS_MODES:
        raise AccessConfigError(f"ACCESS_MODE は {' / '.join(ACCESS_MODES)} のどれかにしてください。")
    return value


async def visible_collections(user_id: str | None) -> frozenset[str]:
    """利用者が見えるグループ。毎回 DB から引く（キャッシュなし）。ロールが 1 つも無い利用者（None を含む）は default ロール。"""
    pool = get_pool()
    if pool is None:
        raise AccessUnavailable("DB の接続プールが開いていません")
    async with pool.connection() as conn:
        cursor = await conn.execute(_VISIBLE_SQL, {"uid": user_id, "default": DEFAULT_ROLE})
        rows = await cursor.fetchall()
    return frozenset(row["collection"] for row in rows)


async def visible_with_completeness(user_id: str | None) -> tuple[frozenset[str], bool]:
    """見えるグループと、それが「いま存在する全グループ」を含むか。同じ接続で引く。

    全グループを引けなかったときは、含まない（False）として返す。会話を伏せる側に倒れるだけで、検索の絞り込みは変わらない。
    見えるグループの取得に失敗したときは `visible_collections` と同じ例外を送る。
    """
    pool = get_pool()
    if pool is None:
        raise AccessUnavailable("DB の接続プールが開いていません")
    async with pool.connection() as conn:
        cursor = await conn.execute(_VISIBLE_SQL, {"uid": user_id, "default": DEFAULT_ROLE})
        visible = frozenset(row["collection"] for row in await cursor.fetchall())
        try:
            cursor = await conn.execute(_KNOWN_SQL)
            known = frozenset(row["collection"] for row in await cursor.fetchall())
        except Exception as exc:  # noqa: BLE001 - 型の名前だけを残す。伏せる側に倒す
            logger.warning(
                "存在する資料グループを引けませんでした (%s)。古い会話は伏せたままにします", type(exc).__name__
            )
            return visible, False
    return visible, bool(visible) and known <= visible


@dataclass(frozen=True)
class Access:
    """1 リクエストぶんのアクセス権。`scope` が実際に効く範囲（off と shadow は `Scope.ALL`）。"""

    mode: str
    scope: Scope

    @property
    def restricts(self) -> bool:
        return not self.scope.is_all

    def allows(self, collection: str | None) -> bool:
        return self.scope.allows(collection)

    def require_any(self) -> None:
        """見えるグループが 0 件なら 403（「資料なし」の空の結果にはしない）。"""
        if self.scope.is_empty:
            raise HTTPException(status_code=403, detail=DETAIL_NO_COLLECTIONS)

    def check_collection(self, collection: str | None, path: str) -> None:
        """利用者が指定したグループが見えなければ 403。存在しないグループも同じ（見えないものと区別させない）。"""
        if collection is None:
            return
        self.scope.log_shadow(path, [collection])
        if not self.scope.allows(collection):
            raise HTTPException(status_code=403, detail=DETAIL_FORBIDDEN_COLLECTION)

    def require_visible(self, collection: str | None, path: str, detail: str) -> None:
        """資料 1 件のグループが見えなければ 404（存在しないのと同じ文）。"""
        self.scope.log_shadow(path, [collection])
        if not self.scope.allows(collection):
            raise HTTPException(status_code=404, detail=detail)

    def mask(self, messages: Sequence[BaseMessage], path: str) -> list[BaseMessage]:
        """見えないグループの検索結果に基づく部分を伏せた会話。shadow では伏せずに、伏せる件数をログに出す。"""
        if self.scope.shadow is not None:
            _, count = mask_hidden_turns(messages, Scope(self.scope.shadow, complete=self.scope.complete))
            logger.info("アクセス制御(shadow): %s 会話の %d 件が enforce では伏せられる", path, count)
        masked, _ = mask_hidden_turns(messages, self.scope)
        return masked


async def request_access(user_id: Annotated[str | None, Depends(current_user)]) -> Access:
    """FastAPI の依存。認証（`current_user`）のあとで、利用者のアクセス権を決める。off では DB を読まない。"""
    mode = access_mode()
    if mode == "off":
        return Access(mode, Scope.ALL)
    try:
        visible, complete = await visible_with_completeness(user_id)
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
        logger.warning("利用できる資料グループを引けませんでした (mode=%s, %s)", mode, type(exc).__name__)
        if mode == "shadow":
            return Access(mode, Scope.ALL)  # shadow は観測だけ。引けなくても、いまの挙動のまま続ける
        raise HTTPException(status_code=503, detail=DETAIL_UNAVAILABLE) from None
    if mode == "shadow":
        return Access(mode, Scope(None, shadow=visible, complete=complete))
    return Access(mode, Scope(visible, complete=complete))


async def verify_access_config() -> None:
    """起動時の検査。ACCESS_MODE が shadow / enforce で使えない状態なら、AccessConfigError で起動を失敗させる。

    off は何も読まない。それ以外は DB が要り、マイグレーション（20261026045921）の表と default ロールが無ければ失敗する。
    """
    mode = access_mode()
    if mode == "off":
        logger.info("アクセス制御: 無効（ACCESS_MODE=off。利用者ごとの資料の制限はしない）")
        return
    pool = get_pool()
    if pool is None:
        raise AccessConfigError(f"ACCESS_MODE={mode} には DATABASE_URL が必要です。")
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_PROBE_SQL, {"default": DEFAULT_ROLE})
            row: dict[str, Any] = (await cursor.fetchall())[0]
    except Exception as exc:  # noqa: BLE001 - 型の名前だけを残す
        raise AccessConfigError(
            f"ロールの表を読めませんでした ({type(exc).__name__})。マイグレーション 20261026045921 が適用済みか確認してください。"
        ) from exc
    if not row["default_roles"]:
        raise AccessConfigError(
            "roles に default ロールがありません。マイグレーション 20261026045921 の初期データを確認してください。"
        )
    if not row["default_collections"]:
        logger.warning("default ロールに資料グループが割り当てられていません。ロールの無い利用者は、何も見えません")
    logger.info("アクセス制御: %s（user_roles の割り当て %d 件）", mode, row["assignments"])


def _source_allowed(source: dict[str, Any], scope: Scope) -> bool:
    """出典を見せてよいか。`collection` の項目そのものが無い古い出典は、全グループが見える範囲（`complete`）のときだけ見せる。

    項目はあるが値が null の出典（グループの無い資料）は、全グループが見える範囲でも見せない（`allows` のとおり）。
    """
    if scope.complete and "collection" not in source:
        return True
    return scope.allows(source.get("collection"))


def _hidden_sources(message: BaseMessage, scope: Scope) -> bool:
    """ツールの結果（ToolMessage）の出典に、範囲の外のグループ（グループ不明を含む）があるか。"""
    artifact = getattr(message, "artifact", None)
    if not isinstance(artifact, list):
        return False
    return any(isinstance(source, dict) and not _source_allowed(source, scope) for source in artifact)


def mask_hidden_turns(messages: Sequence[BaseMessage], scope: Scope) -> tuple[list[BaseMessage], int]:
    """範囲の外のグループの検索結果に基づく部分を、固定文に置き換えた会話と、置き換えた件数。

    1 回の質問（ユーザーの発言から次のユーザーの発言まで）の中で、範囲の外のグループが出典に入った検索結果（ToolMessage）が
    あれば、その結果と、その後の最終回答（ツール呼び出しの無い AI の発言）を伏せる。ユーザーの発言は残す。
    出典のグループが分からない検索結果は、範囲を絞っているときは伏せる。ただし、`collection` の項目そのものが無い古い会話
    （2026-10-26 より前）は、範囲がいま存在する全グループを含む（`scope.complete`）なら伏せない。値が null の出典は伏せる。
    LLM に渡す履歴と、会話の表示の両方に使う（回答の本文には見えないグループの内容が入りうるため、LLM にも見せない）。
    元のメッセージは変えない。
    """
    if scope.is_all:
        return list(messages), 0
    masked = 0
    turn_hidden = False
    result: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            turn_hidden = False
            result.append(message)
        elif isinstance(message, ToolMessage):
            turn_hidden = turn_hidden or _hidden_sources(message, scope)
            if turn_hidden:
                result.append(message.model_copy(update={"content": MASKED_TOOL_TEXT, "artifact": []}))
                masked += 1
            else:
                result.append(message)
        elif turn_hidden and isinstance(message, AIMessage) and not message.tool_calls and message.text.strip():
            result.append(AIMessage(content=MASKED_ANSWER_TEXT, id=message.id))
            masked += 1
        else:
            result.append(message)
    return result, masked
