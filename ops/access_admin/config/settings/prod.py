"""本番（非公開の Cloud Run `docunavi-admin`）の設定。値はすべて環境変数から読む。足りなければ起動を失敗させる。

必須:
  ADMIN_SECRET_KEY    Django の SECRET_KEY（Secret Manager。32 文字以上）
  ADMIN_DATABASE_URL  access_admin ロールの接続 URL（Secret Manager。postgresql://ユーザー:パスワード@ホスト:ポート/DB?sslmode=require）
  ADMIN_ALLOWED_HOSTS   カンマ区切りのホスト名（Cloud Run の URL のホスト。`gcloud run services proxy` なら localhost）
任意:
  ADMIN_CSRF_TRUSTED_ORIGINS  カンマ区切りのオリジン（https://… または、プロキシ経由なら http://localhost:8080）
  ADMIN_SECURE_COOKIES        既定 1。0 にするのはプロキシ経由の http://localhost だけ（Cookie の Secure を外す）
  ADMIN_TRUST_PROXY_SSL       既定 1。X-Forwarded-Proto: https を信じる（Cloud Run が付ける）
"""

import os
from urllib.parse import parse_qs, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"環境変数 {name} が設定されていません。")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in ("0", "1"):
        raise ImproperlyConfigured(f"環境変数 {name} は 0 か 1 にしてください。")
    return raw == "1"


def _list(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def database_from_url(url: str) -> dict:
    """接続 URL を Django の DATABASES の 1 件にする。エラーの文に URL（パスワード）を含めない。"""
    try:
        parts = urlsplit(url)
        port = parts.port or 5432
    except ValueError:
        raise ImproperlyConfigured("ADMIN_DATABASE_URL の形式が正しくありません。") from None
    if parts.scheme not in ("postgresql", "postgres") or not parts.hostname or not parts.username:
        raise ImproperlyConfigured("ADMIN_DATABASE_URL の形式が正しくありません。")
    options: dict = {
        # 接続プーラー（トランザクションモード）でも壊れないよう、プリペアドステートメントを使わない（app/history.py と同じ）
        "prepare_threshold": None,
    }
    sslmode = parse_qs(parts.query).get("sslmode", [""])[0]
    if sslmode:
        options["sslmode"] = sslmode
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")) or "postgres",
        "USER": unquote(parts.username),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname,
        "PORT": port,
        "CONN_MAX_AGE": 0,
        "DISABLE_SERVER_SIDE_CURSORS": True,
        # search_path（saas, chatbot）は DB ロールの設定で決まる。起動時のオプションは、プーラーが無視することがあるので渡さない
        "OPTIONS": options,
    }


SECRET_KEY = _required("ADMIN_SECRET_KEY")
if len(SECRET_KEY) < 32:
    raise ImproperlyConfigured("ADMIN_SECRET_KEY は 32 文字以上にしてください。")

ALLOWED_HOSTS = _list("ADMIN_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("環境変数 ADMIN_ALLOWED_HOSTS が設定されていません。")
CSRF_TRUSTED_ORIGINS = _list("ADMIN_CSRF_TRUSTED_ORIGINS")

DATABASES = {"default": database_from_url(_required("ADMIN_DATABASE_URL"))}

_secure = _flag("ADMIN_SECURE_COOKIES", True)
SESSION_COOKIE_SECURE = _secure
CSRF_COOKIE_SECURE = _secure
if _flag("ADMIN_TRUST_PROXY_SSL", True):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
