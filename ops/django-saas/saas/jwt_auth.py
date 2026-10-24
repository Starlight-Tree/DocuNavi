"""JWT の検証。`app/auth.py` と同じ規則（ES256 / RS256 だけ、`exp` `iss` `aud` `sub` 必須、匿名は拒否）。

違い: 公開鍵は JWKS のファイル（SAAS_JWKS_FILE）から一度だけ読む。実際の Supabase の JWKS の取得・キャッシュ・取り直しは持たない
（学習用の測定のため。実装するなら app/auth.py の JwksCache と同じ考え方になる）。
"""

import json
import threading
import uuid

import jwt
from django.conf import settings

ALLOWED_ALGORITHMS = ("ES256", "RS256")
_REQUIRED_CLAIMS = ["exp", "iss", "aud", "sub"]
_LEEWAY_SECONDS = 10


class InvalidToken(Exception):
    """受け付けないトークン。理由の種類（reason）だけを持つ。トークンや例外の文字列は持たない。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_keys: dict[str, jwt.PyJWK] | None = None
_lock = threading.Lock()


def reset_keys() -> None:
    global _keys
    _keys = None


def _load_keys() -> dict[str, jwt.PyJWK]:
    global _keys
    if _keys is not None:
        return _keys
    with _lock:
        if _keys is None:
            path = settings.SAAS_JWKS_FILE
            if not path:
                raise InvalidToken("no_keys")
            with open(path, encoding="utf-8") as f:
                document = json.load(f)
            loaded: dict[str, jwt.PyJWK] = {}
            for entry in document.get("keys", []):
                kid = entry.get("kid")
                if isinstance(kid, str) and kid and entry.get("kty") in ("EC", "RSA"):
                    loaded[kid] = jwt.PyJWK(entry)
            _keys = loaded
    return _keys


def verify_token(token: str) -> str:
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

    key = _load_keys().get(kid)
    if key is None or key.algorithm_name != alg:
        raise InvalidToken("unknown_key")
    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=[alg],
            audience=settings.SAAS_JWT_AUDIENCE,
            issuer=settings.SAAS_JWT_ISSUER,
            leeway=_LEEWAY_SECONDS,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(type(exc).__name__) from exc

    sub = claims.get("sub")
    try:
        user_id = str(uuid.UUID(sub)) if isinstance(sub, str) else None
    except ValueError:
        user_id = None
    if user_id is None:
        raise InvalidToken("sub")
    if claims.get("role") != settings.SAAS_JWT_AUDIENCE or claims.get("is_anonymous") is True:
        raise InvalidToken("role")
    return user_id
