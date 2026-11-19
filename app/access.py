"""ロール別の資料グループ制御（ロードマップ ⑧）。利用者ごとに、検索・回答・出典・本文・PDF・関係図・会話の表示が届くグループを決める。

- 見えるグループ = 利用者のロール（`user_roles`）が持つグループ（`role_collection_access`）の和集合。ロールが 1 つも無い利用者
  （`AUTH_MODE=off` の匿名を含む）は `default` ロール。プロセス内に短い TTL（既定 60 秒。`ACCESS_CACHE_TTL_SECONDS`、0 で無効）の
  キャッシュを持つ（DB の operations を減らすため）。ロールの付け替え・外しが効くまでに、最大でこの秒数かかる（2026-11-11 に利用者が許容）。
  引けなかった結果と、「全グループを引けなかった」結果は、キャッシュしない。
- 権限の判断はサーバーだけで行う（LLM には任せない）。検索は **検索の前に** SQL で絞る（結果を取ってから消さない）。
- `ACCESS_MODE`: `off`（既定。制限なし。DB も読まない）/ `shadow`（制限せず、enforce なら何件が絞り込まれるかをログに出す）/
  `enforce`（制限する）。知らない値は起動を失敗させる。enforce で権限を引けなければ 503（制限なしには倒れない）、shadow では制限なしで続ける。
- 見えないものは「無い」のと同じに見せる: 資料は 404、グループの指定は 403、見えるグループが 0 件なら 403。文はすべて固定文で、
  見えないグループの名前を載せない。過去の会話は表示するが、見えないグループの検索結果に基づく回答は伏せる（`mask_hidden_turns`）。
- `docunavi-mcp`（`ops/mcp_search`）は運用者専用で、`Scope.ALL` を明示して全グループを検索する。このモジュールは通らない。
- 利用者へのロールの割り当て（`user_roles`）の変更は、`python -m app.access_admin` だけで行う（SQL で直接 `DELETE` しない。理由は `access_admin.py` の docstring）。
  ロールの作成とグループの編集は SQL（`docs/configuration.md`）。管理画面（フェーズ 4）は未実装。

利用資格（`MEMBERSHIP_MODE`。資料の権限とは別の設定）:
- 「この利用者はアプリを使ってよいか」は `app_members`（利用者 ID の一覧。マイグレーション 0003）で決める。資料のロール・グループとは独立で、
  `default` に資料を足しても、ロールを付け替えても、資格は変わらない。逆に、資格があっても、見える資料が 0 件なら `require_any` が 403 にする。
- `MEMBERSHIP_MODE`: `off`（既定。確認しない）/ `shadow`（拒否せず、enforce なら拒否される要求をログに出す）/ `enforce`（資格のない利用者は 403）。
  `ACCESS_MODE` とは独立（片方が off でも、もう片方は効く）。enforce で資格を引けなければ 503（許可には倒れない）、shadow では拒否せず続ける。
- 確認は `request_access`（資料を扱う全経路が通る）と `current_member`（会話の一覧・削除）に 1 か所ずつ。経路ごとに書き足さない。
  確認するのはリクエストの開始時だけで、始まったストリームは最後まで流れる。結果は、資料の権限と同じ TTL でプロセス内に控える
  （許可・不許可のどちらも。取り消し・付与は、新しいリクエストに、通常はその TTL 以内に効く。再起動すると控えは空になる）。
- 資格のない利用者のユーザー行（Better Auth）は消さない。ログインはできても、料金のかかる操作と資料には届かない。
"""

import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.auth import auth_mode, current_user
from app.history import get_pool
from app.scope import ALLOWED_FILTER_SQL, Scope

logger = logging.getLogger(__name__)

ACCESS_MODES = ("off", "shadow", "enforce")
MEMBERSHIP_MODES = ACCESS_MODES
DEFAULT_ROLE = "default"

# 利用資格がない 403 を、資料の権限の 403 と機械的に区別するための識別コード（応答の本文の `code`。画面が見る）
MEMBERSHIP_REQUIRED_CODE = "APP_MEMBERSHIP_REQUIRED"
DETAIL_MEMBERSHIP_REQUIRED = "このアカウントには利用許可がありません。"
DETAIL_MEMBERSHIP_UNAVAILABLE = "利用資格を確認できませんでした。しばらく待ってから再試行してください。"

# クライアントへ返す固定メッセージ。見えないグループの名前・存在を載せない
DETAIL_FORBIDDEN_COLLECTION = "指定された資料グループは利用できません。"
DETAIL_NO_COLLECTIONS = "利用できる資料がありません。"
# 対象（`documents.target`）の指定が使えないとき。見えない対象・資料のない対象・存在しない対象を、同じ文にする（名前は載せない）
DETAIL_FORBIDDEN_TARGET = "指定された対象は利用できません。"
DETAIL_UNAVAILABLE = "資料の利用権限を確認できませんでした。しばらく待ってから再試行してください。"
# 過去の会話で、いま見えないグループの検索結果に基づく部分の代わりに出す文
MASKED_TOOL_TEXT = "（この検索結果は、現在利用できない資料に基づくため表示できません）"
MASKED_ANSWER_TEXT = "この回答は、現在利用できない資料に基づいているため表示できません。"

# 利用者が見えるグループ。user_roles が 1 件も無い利用者（user_id が NULL の匿名も）は default ロール
_VISIBLE_SQL = (
    "SELECT DISTINCT a.collection AS collection FROM role_collection_access a "
    "WHERE a.role IN (SELECT r.role FROM user_roles r WHERE r.user_id = %(uid)s) "
    "OR (a.role = %(default)s AND NOT EXISTS (SELECT 1 FROM user_roles r WHERE r.user_id = %(uid)s))"
)
# いま存在する全グループ。登録済み（document_collections）と、資料にだけあるもの（documents.collection）の両方
_KNOWN_SQL = (
    "SELECT name AS collection FROM document_collections "
    "UNION SELECT collection FROM documents WHERE collection IS NOT NULL"
)
# 起動時の検査の列（app/startup_checks.py が、資料の検査と 1 本の SQL にまとめるときにも使う）
PROBE_COLUMNS_SQL = (
    "(SELECT count(*) FROM roles WHERE name = %(default)s) AS default_roles, "
    "(SELECT count(*) FROM role_collection_access WHERE role = %(default)s) AS default_collections, "
    "(SELECT count(*) FROM user_roles) AS assignments"
)
_PROBE_SQL = f"SELECT {PROBE_COLUMNS_SQL}"


# この範囲で見える資料が 1 件でもある対象か（状態は問わない。取り込み中の資料だけの対象は、「準備中」として選べる）
_TARGET_VISIBLE_SQL = (
    "SELECT 1 AS ok FROM documents d WHERE d.target = %(target)s AND " + ALLOWED_FILTER_SQL + " LIMIT 1"
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


class MembershipRequired(HTTPException):
    """利用資格がない（403）。応答の本文は `{"detail": <固定の日本語>, "code": "APP_MEMBERSHIP_REQUIRED"}`（`app/main.py` の handler）。"""

    def __init__(self) -> None:
        super().__init__(status_code=403, detail=DETAIL_MEMBERSHIP_REQUIRED)
        self.code = MEMBERSHIP_REQUIRED_CODE


def membership_mode() -> str:
    """MEMBERSHIP_MODE（off / shadow / enforce。未設定・空は off）。知らない値は、黙って off にせず例外にする。ACCESS_MODE とは独立。"""
    value = os.environ.get("MEMBERSHIP_MODE", "").strip().lower() or "off"
    if value not in MEMBERSHIP_MODES:
        raise AccessConfigError(f"MEMBERSHIP_MODE は {' / '.join(MEMBERSHIP_MODES)} のどれかにしてください。")
    return value


DEFAULT_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ENTRIES = 1024
# 利用者 ID（匿名は None）→ (期限の monotonic 秒, 見えるグループ, 全グループを含むか, 引いた接続プール)。プロセス内だけ。Cloud Run のインスタンスごとに別。
# 接続プールが入れ替わったら（テストの偽の DB を含む）、控えは使わない
_cache: dict[str | None, tuple[float, frozenset[str], bool, Any]] = {}
# 利用者 ID → (期限の monotonic 秒, 資格があるか, 引いた接続プール)。許可も不許可も控える。プロセス内だけ（インスタンスごとに別）
_member_cache: dict[str, tuple[float, bool, Any]] = {}


def cache_ttl_seconds() -> float:
    """`ACCESS_CACHE_TTL_SECONDS`（秒。0 で無効。既定 60）。負・数でない値は、黙って既定にせず例外にする。"""
    raw = os.environ.get("ACCESS_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CACHE_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise AccessConfigError("ACCESS_CACHE_TTL_SECONDS は 0 以上の数（秒）にしてください。") from None
    if value < 0 or value != value or value == float("inf"):
        raise AccessConfigError("ACCESS_CACHE_TTL_SECONDS は 0 以上の数（秒）にしてください。")
    return value


def clear_cache() -> None:
    """キャッシュ（見えるグループと利用資格）を空にする（テスト・運用の即時反映用）。"""
    _cache.clear()
    _member_cache.clear()


def _make_room(cache: dict[Any, tuple[Any, ...]], now: float) -> None:
    """上限に達していたら、期限切れを捨て、それでも満杯なら最も古く入れたものを 1 つ捨てる。"""
    if len(cache) < _CACHE_MAX_ENTRIES:
        return
    for key in [k for k, v in cache.items() if v[0] <= now]:
        del cache[key]
    if len(cache) >= _CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)))


def _cache_get(user_id: str | None, pool: Any) -> tuple[frozenset[str], bool] | None:
    hit = _cache.get(user_id)
    if hit is None or hit[3] is not pool:
        return None
    if hit[0] <= time.monotonic():
        _cache.pop(user_id, None)
        return None
    return hit[1], hit[2]


def _cache_put(user_id: str | None, visible: frozenset[str], complete: bool, pool: Any) -> None:
    ttl = cache_ttl_seconds()
    if ttl <= 0:
        return
    now = time.monotonic()
    _make_room(_cache, now)
    _cache[user_id] = (now + ttl, visible, complete, pool)


async def visible_collections(user_id: str | None) -> frozenset[str]:
    """利用者が見えるグループ。ロールが 1 つも無い利用者（None を含む）は default ロール。TTL 内は、プロセス内の控えを返す。"""
    pool = get_pool()
    if pool is None:
        raise AccessUnavailable("DB の接続が開いていません")
    if (hit := _cache_get(user_id, pool)) is not None:
        return hit[0]
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
        raise AccessUnavailable("DB の接続が開いていません")
    if (hit := _cache_get(user_id, pool)) is not None:
        return hit
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
            return visible, False  # 引けなかった結果は、控えない
    complete = bool(visible) and known <= visible
    _cache_put(user_id, visible, complete, pool)
    return visible, complete


async def target_is_visible(scope: Scope, target: str) -> bool:
    """この範囲で、対象に見える資料が 1 件でもあるか。DB が無い（モックモード）ときは True（検索もしないので、何も漏れない）。

    存在しない対象・資料のない対象・資料が見えない対象は、どれも False（見えないものと区別させない）。
    引けなければ `AccessUnavailable`（呼び出し側が 503 の固定文にする）。
    """
    if scope.is_empty:
        return False
    pool = get_pool()
    if pool is None:
        return True
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_TARGET_VISIBLE_SQL, {"target": target, "allowed": scope.sql_names()})
            return bool(await cursor.fetchall())
    except Exception as exc:  # noqa: BLE001 - 型の名前だけを残す
        logger.warning("対象の有無を引けませんでした (%s)", type(exc).__name__)
        raise AccessUnavailable("対象を確認できません") from None


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


_MEMBER_SQL = "SELECT 1 AS ok FROM app_members WHERE user_id = %(uid)s"


async def is_member(user_id: str | None) -> bool:
    """利用資格があるか（`app_members` に行があるか）。匿名（None）は資格なし。TTL 内は、プロセス内の控えを返す。

    引けなければ `AccessUnavailable`（呼び出し側が、拒否ではなく 503 にする。引けなかった結果は控えない）。
    """
    if user_id is None:
        return False
    pool = get_pool()
    if pool is None:
        raise AccessUnavailable("DB の接続が開いていません")
    hit = _member_cache.get(user_id)
    if hit is not None and hit[2] is pool and hit[0] > time.monotonic():
        return hit[1]
    async with pool.connection() as conn:
        cursor = await conn.execute(_MEMBER_SQL, {"uid": user_id})
        member = bool(await cursor.fetchall())
    ttl = cache_ttl_seconds()
    if ttl > 0:
        now = time.monotonic()
        _make_room(_member_cache, now)
        _member_cache[user_id] = (now + ttl, member, pool)
    return member


async def enforce_membership(user_id: str | None, path: str) -> None:
    """利用資格を確かめる。資格がなければ `MembershipRequired`（403）。ACCESS_MODE とは独立に、MEMBERSHIP_MODE だけで決まる。

    off は何も読まない。shadow は拒否せず、enforce なら拒否される要求を（経路だけ）ログに出す。引けなかったときは、
    enforce なら 503（許可には倒れない）、shadow なら続ける。DB 障害と「資格なし」は区別する（503 と 403）。
    """
    mode = membership_mode()
    if mode == "off":
        return
    try:
        member = await is_member(user_id)
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
        logger.warning("利用資格を引けませんでした (mode=%s, %s)", mode, type(exc).__name__)
        if mode == "shadow":
            return
        raise HTTPException(status_code=503, detail=DETAIL_MEMBERSHIP_UNAVAILABLE) from None
    if member:
        return
    if mode == "shadow":
        logger.info("利用資格(shadow): %s は enforce では拒否される（資格のない利用者）", path)
        return
    raise MembershipRequired()


async def current_member(request: Request, user_id: Annotated[str | None, Depends(current_user)]) -> str | None:
    """FastAPI の依存。認証（`current_user`）のあとで利用資格を確かめ、利用者 ID を返す。資料の権限は見ない経路（会話の一覧・削除）が使う。"""
    await enforce_membership(user_id, request.url.path)
    return user_id


async def request_access(request: Request, user_id: Annotated[str | None, Depends(current_user)]) -> Access:
    """FastAPI の依存。認証（`current_user`）のあとで、利用資格（MEMBERSHIP_MODE）と、利用者のアクセス権（ACCESS_MODE）を決める。

    資料を扱う経路は、すべてここを通る。資格は資料の権限より先に確かめる（どちらが off でも、もう一方は効く）。off では DB を読まない。
    """
    await enforce_membership(user_id, request.url.path)
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


def check_config() -> str:
    """起動時の検査のうち、DB を使わない部分。ACCESS_MODE を返す。知らない値・不正な TTL は、起動を失敗させる。"""
    mode = access_mode()
    if mode != "off":
        cache_ttl_seconds()
    return mode


def check_probe_row(mode: str, row: dict[str, Any]) -> None:
    """起動時の検査の結果（`PROBE_COLUMNS_SQL` の 1 行）を判定する。default ロールが無ければ AccessConfigError。"""
    if not row["default_roles"]:
        raise AccessConfigError(
            "roles に default ロールがありません。マイグレーション 20261026045921 の初期データを確認してください。"
        )
    if not row["default_collections"]:
        logger.warning("default ロールに資料グループが割り当てられていません。ロールの無い利用者は、何も見えません")
    logger.info("アクセス制御: %s（user_roles の割り当て %d 件）", mode, row["assignments"])


async def verify_access_config() -> None:
    """起動時の検査。ACCESS_MODE が shadow / enforce で使えない状態なら、AccessConfigError で起動を失敗させる。

    off は何も読まない。それ以外は DB が要り、マイグレーション（20261026045921）の表と default ロールが無ければ失敗する。
    本番の起動は `app/startup_checks.py` が、資料の検査とまとめて 1 回の問い合わせで行う。これは、まとめられないときの単独の経路。
    """
    mode = check_config()
    if mode == "off":
        logger.info("アクセス制御: 無効（ACCESS_MODE=off。利用者ごとの資料の制限はしない）")
        return
    pool = get_pool()
    if pool is None:
        raise AccessConfigError(
            f"ACCESS_MODE={mode} には DB の接続（pg は DATABASE_URL、d1 は D1_API_URL）が必要です。"
        )
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_PROBE_SQL, {"default": DEFAULT_ROLE})
            row: dict[str, Any] = (await cursor.fetchall())[0]
    except Exception as exc:  # noqa: BLE001 - 型の名前だけを残す
        raise AccessConfigError(
            f"ロールの表を読めませんでした ({type(exc).__name__})。マイグレーション 20261026045921 が適用済みか確認してください。"
        ) from exc
    check_probe_row(mode, row)


def check_membership_config() -> str:
    """起動時の検査のうち、DB を使わない部分。MEMBERSHIP_MODE を返す。知らない値・不正な TTL・匿名との組み合わせは、起動を失敗させる。

    本番（AUTH_MODE=required）で MEMBERSHIP_MODE が未設定なら、「off 扱い（ログインした全員が使える）」と分かるよう警告を残す
    （設定し忘れに気づけるように。値を明示すれば、この警告は出ない）。
    """
    mode = membership_mode()
    if mode != "off":
        cache_ttl_seconds()
        if auth_mode() == "off":
            raise AccessConfigError(
                f"MEMBERSHIP_MODE={mode} には AUTH_MODE=required が必要です（匿名では利用者を区別できません）。"
            )
    elif auth_mode() == "required":
        if os.environ.get("MEMBERSHIP_MODE", "").strip():
            logger.info("利用資格: 無効（MEMBERSHIP_MODE=off。ログインした全員が使える）")
        else:
            logger.warning(
                "利用資格: MEMBERSHIP_MODE が未設定です（off 扱い。ログインした全員が使えます）。意図した値を明示してください"
            )
    return mode


async def verify_membership_config(mode: str) -> None:
    """起動時の検査。MEMBERSHIP_MODE が shadow / enforce で使えない状態なら、AccessConfigError で起動を失敗させる（off は何も読まない）。

    `app_members`（マイグレーション 0003）を読めなければ失敗する。enforce で行が 0 件なら、全員が拒否されるので警告する。
    ACCESS_MODE の検査とは独立（片方が off でも行う）。
    """
    if mode == "off":
        return
    pool = get_pool()
    if pool is None:
        raise AccessConfigError(f"MEMBERSHIP_MODE={mode} には DB（DATABASE_URL）が必要です。")
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT count(*) AS members FROM app_members")
            members = int((await cursor.fetchall())[0]["members"])
    except Exception as exc:  # noqa: BLE001 - 型の名前だけを残す
        raise AccessConfigError(
            f"app_members を読めませんでした ({type(exc).__name__})。マイグレーション 0003 が適用済みか確認してください。"
        ) from exc
    logger.info("利用資格: %s（app_members %d 件）", mode, members)
    if mode == "enforce" and members == 0:
        logger.warning("利用資格: app_members が 0 件です。enforce では、全員が拒否されます")


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
