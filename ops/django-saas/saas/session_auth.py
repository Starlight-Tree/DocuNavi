"""利用者向け画面の「いま誰か」。画面側は current_sub だけに依存する。

試作では、セッションに入れた `sub`（UUID）を返す。実際の認証に置き換えるときは、current_sub の中身を JWT の検証
（saas/jwt_auth.py の verify_token）に差し替えればよい。`sub` 以外から利用者 ID を取らない点は API（views.py）と同じ。
"""

import uuid

from django.conf import settings

_SESSION_KEY = "saas_sub"


def demo_login_enabled() -> bool:
    return bool(settings.SAAS_DEMO_LOGIN)


def current_sub(request) -> uuid.UUID | None:
    value = request.session.get(_SESSION_KEY)
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def start_session(request, sub: uuid.UUID) -> None:
    request.session.cycle_key()  # ログイン前のセッション ID を引き継がない
    request.session[_SESSION_KEY] = str(sub)


def end_session(request) -> None:
    request.session.flush()
