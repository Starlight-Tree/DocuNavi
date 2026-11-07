"""ログイン（Better Auth（データ用 Worker）が発行した JWT の検証）。

`AUTH_MODE=off`（既定）なら何も確かめず、従来どおり匿名で動く。`AUTH_MODE=required` のときは、`/chat` と
`/chat/stream` が `Authorization: Bearer <access_token>` を要求し、署名・有効期限・発行元・対象・`sub` を確かめる。

- 署名の検証は、`{AUTH_BASE_URL}/api/auth/jwks`（`AUTH_JWKS_URL` で上書きできる）の公開鍵（ES256 / RS256）だけで行う。
  発行元（`iss`）は `AUTH_BASE_URL`、対象（`aud`）は `AUTH_AUDIENCE`（既定 docunavi）。
  公開鍵が 1 つも無いときは、共有秘密の検証へ黙って切り替えず、起動を失敗させる
- 利用者 ID は、検証に通ったトークンの `sub` だけから取る。クライアントが送った値は使わない
- トークンの中身・Authorization ヘッダはログに出さない。ログに残すのは、失敗の種類（例外のクラス名）だけ
"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import jwt
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

AUTH_MODES = ("off", "required")
# 署名に使ってよいアルゴリズム。alg=none や HS256（公開鍵を共有秘密として使う攻撃）を受け付けない
ALLOWED_ALGORITHMS = ("ES256", "RS256")
_ALLOWED_KEY_TYPES = ("EC", "RSA")
DEFAULT_AUDIENCE = "docunavi"
# Better Auth の利用者 ID（英数字と _ -）。履歴のキーの接頭辞に使うので、区切り文字（:）や空白を含む値は受け付けない
_USER_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_REQUIRED_CLAIMS = ["exp", "iss", "aud", "sub"]
# 端末の時計のずれを許す秒数
_LEEWAY_SECONDS = 10
# JWKS の取得: 1 回の待ち時間の上限と、読む本文の上限
_FETCH_TIMEOUT_SECONDS = 5.0
_MAX_JWKS_BYTES = 1_000_000
# 取得した鍵を使い続ける時間。過ぎたら取り直す（取り直しに失敗したときは、前の鍵を使い続ける）
_KEYS_TTL_SECONDS = 600.0
# 未知の kid を理由にした取り直しの最小間隔。でたらめな kid のトークンで、JWKS への問い合わせを増幅させないため
_MIN_REFRESH_INTERVAL_SECONDS = 60.0

# クライアントへ返す固定メッセージ。例外の文字列・トークンは含めない
_DETAIL_UNAUTHORIZED = "ログインが必要です。ログインし直してください。"
_DETAIL_UNAVAILABLE = "認証サービスに接続できませんでした。しばらく待ってから再試行してください。"

# テストが差し替える（時計と、JWKS の取得）
_now: Callable[[], float] = time.monotonic


class AuthConfigError(RuntimeError):
    """認証の設定が正しくない。起動を失敗させる（メッセージに秘密は含めない）。"""


class InvalidToken(Exception):
    """トークンを受け付けない。理由はログ用で、クライアントへは返さない。"""


class AuthUnavailable(Exception):
    """鍵を取得できず、検証できない。"""


def auth_mode() -> str:
    """AUTH_MODE（off / required。未設定・空は off）。知らない値は、黙って off にせず例外にする。"""
    value = os.environ.get("AUTH_MODE", "").strip().lower() or "off"
    if value not in AUTH_MODES:
        raise AuthConfigError(f"AUTH_MODE は {' / '.join(AUTH_MODES)} のどれかにしてください。")
    return value


def auth_base_url() -> str:
    """AUTH_BASE_URL（Better Auth の公開オリジン。末尾のスラッシュなし）。未設定・不正な形なら AuthConfigError。"""
    raw = os.environ.get("AUTH_BASE_URL", "").strip().rstrip("/")
    if not raw:
        raise AuthConfigError("AUTH_MODE=required には AUTH_BASE_URL が必要です。")
    parts = urlsplit(raw)
    local = _is_local_host(parts.hostname)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.query or parts.fragment or parts.path:
        raise AuthConfigError("AUTH_BASE_URL は https://<ホスト> の形にしてください（パスや問い合わせは付けない）。")
    if parts.scheme == "http" and not local:
        raise AuthConfigError("AUTH_BASE_URL は https にしてください（http はローカルだけ）。")
    return raw


def audience() -> str:
    return os.environ.get("AUTH_AUDIENCE", "").strip() or DEFAULT_AUDIENCE


def _is_local_host(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


_JWKS_USER_AGENT = "docunavi-api/1.0"


def issuer() -> str:
    return auth_base_url()


def jwks_url() -> str:
    override = os.environ.get("AUTH_JWKS_URL", "").strip()
    return override or f"{auth_base_url()}/api/auth/jwks"


def fetch_jwks_document(url: str) -> dict[str, Any]:
    """JWKS を取得して JSON として返す（同期。呼び出し側がスレッドで実行する）。"""
    # 既定の UA（Python-urllib）は Cloudflare に 403（error 1010）で弾かれ、起動時の検査が落ちる。
    request = urllib.request.Request(  # noqa: S310 (http/https は検証済み)
        url, headers={"Accept": "application/json", "User-Agent": _JWKS_USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        body = response.read(_MAX_JWKS_BYTES + 1)
    if len(body) > _MAX_JWKS_BYTES:
        raise ValueError("JWKS が大きすぎます")
    document = json.loads(body)
    if not isinstance(document, dict):
        raise ValueError("JWKS が JSON のオブジェクトではありません")
    return document


def _parse_keys(document: dict[str, Any]) -> dict[str, jwt.PyJWK]:
    """JWKS から、使ってよい鍵（EC / RSA で kid があるもの）だけを kid で引ける形にする。"""
    keys: dict[str, jwt.PyJWK] = {}
    raw_keys = document.get("keys")
    for item in raw_keys if isinstance(raw_keys, list) else []:
        if not isinstance(item, dict) or item.get("kty") not in _ALLOWED_KEY_TYPES:
            continue
        kid = item.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        try:
            key = jwt.PyJWK(item)
        except jwt.PyJWTError:
            continue
        if key.algorithm_name in ALLOWED_ALGORITHMS:
            keys[kid] = key
    return keys


class JwksCache:
    """JWKS の公開鍵の置き場。期限が来たら取り直し、未知の kid は間隔を空けて取り直す。"""

    def __init__(self, url: str) -> None:
        self._url = url
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None  # 最後に取得に成功した時刻
        self._attempted_at: float | None = None  # 最後に取得を試みた時刻
        self._lock = asyncio.Lock()

    @property
    def url(self) -> str:
        return self._url

    async def refresh(self) -> int:
        """鍵を取り直して、使える鍵の数を返す。取得できなければ例外（前の鍵はそのまま残す）。"""
        async with self._lock:
            return await self._refresh_locked()

    async def _refresh_locked(self) -> int:
        self._attempted_at = _now()
        document = await asyncio.to_thread(fetch_jwks_document, self._url)
        self._keys = _parse_keys(document)
        self._fetched_at = _now()
        return len(self._keys)

    async def get_key(self, kid: str) -> jwt.PyJWK | None:
        now = _now()
        expired = self._fetched_at is None or now - self._fetched_at >= _KEYS_TTL_SECONDS
        if not expired and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = _now()
            expired = self._fetched_at is None or now - self._fetched_at >= _KEYS_TTL_SECONDS
            if not expired and kid in self._keys:  # 待っている間に、ほかのリクエストが取り直した
                return self._keys[kid]
            recently = self._attempted_at is not None and now - self._attempted_at < _MIN_REFRESH_INTERVAL_SECONDS
            if recently and self._keys:
                return self._keys.get(kid)
            try:
                await self._refresh_locked()
            except Exception as exc:
                # 失敗の種類だけを残す（URL・本文は出さない）。前の鍵があれば、それで検証を続ける
                logger.warning("JWKS を取得できませんでした (%s)", type(exc).__name__)
                if not self._keys:
                    raise AuthUnavailable from exc
            return self._keys.get(kid)


_cache: JwksCache | None = None


def get_cache() -> JwksCache:
    global _cache
    url = jwks_url()
    if _cache is None or _cache.url != url:
        _cache = JwksCache(url)
    return _cache


async def verify_auth_config() -> None:
    """起動時の検査。AUTH_MODE=required で使えない状態なら、AuthConfigError で起動を失敗させる。"""
    if auth_mode() == "off":
        logger.info("認証: 無効（AUTH_MODE=off。だれでも会話できる。公開するときは required にする）")
        return
    cache = get_cache()
    try:
        count = await cache.refresh()
    except Exception as exc:
        raise AuthConfigError(
            f"JWKS を取得できませんでした ({type(exc).__name__})。AUTH_BASE_URL を確認してください。"
        ) from exc
    if count == 0:
        # 共有秘密（HS256）の旧方式のプロジェクトは、ここが空になる。黙って別の検証方式へ切り替えない
        raise AuthConfigError(
            "JWKS に公開鍵（ES256 / RS256）がありません。Better Auth の jwt プラグイン（ES256）の鍵を確認してください"
            "（共有秘密による検証には切り替えません）。"
        )
    logger.info("認証: 必須（AUTH_MODE=required。JWKS の公開鍵 %d 個）", count)


async def verify_token(token: str) -> str:
    """トークンを検証して、利用者 ID（`sub` の UUID）を返す。受け付けないときは InvalidToken。"""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidToken("malformed") from exc
    alg, kid = header.get("alg"), header.get("kid")
    if alg not in ALLOWED_ALGORITHMS:
        raise InvalidToken("algorithm")
    if not isinstance(kid, str) or not kid:
        raise InvalidToken("kid")

    key = await get_cache().get_key(kid)
    if key is None or key.algorithm_name != alg:
        raise InvalidToken("unknown_key")
    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=[alg],
            audience=audience(),
            issuer=issuer(),
            leeway=_LEEWAY_SECONDS,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(type(exc).__name__) from exc

    sub = claims.get("sub")
    try:
        user_id = sub if isinstance(sub, str) and _USER_ID.fullmatch(sub) else None
    except ValueError:
        user_id = None
    if user_id is None:
        raise InvalidToken("sub")
    return user_id


def _bearer_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    token = value.strip()
    return token if scheme.lower() == "bearer" and token else None


async def current_user(request: Request) -> str | None:
    """FastAPI の依存。AUTH_MODE=off なら None（匿名）、required なら検証済みの利用者 ID。"""
    if auth_mode() == "off":
        return None
    unauthorized = HTTPException(status_code=401, detail=_DETAIL_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    token = _bearer_token(request)
    if token is None:
        raise unauthorized
    try:
        return await verify_token(token)
    except InvalidToken as exc:
        logger.info("認証に失敗しました (reason=%s)", exc)
        raise unauthorized from None
    except AuthUnavailable:
        raise HTTPException(status_code=503, detail=_DETAIL_UNAVAILABLE) from None


def conversation_key(user_id: str | None, thread_id: str) -> str:
    """履歴の保存先で使う会話のキー。匿名はそのまま、ログイン中は `<利用者 ID>:<thread_id>`。

    thread_id は `[A-Za-z0-9_-]` だけ（リクエストの検証で保証）で `:` を含まないので、他の利用者の接頭辞を作って
    その会話に届くことはない。クライアントに見せる thread_id は接頭辞なしのまま。
    """
    return thread_id if user_id is None else f"{user_id}:{thread_id}"
