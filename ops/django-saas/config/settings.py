"""学習用の設定。開発コンテナの一時 Postgres（`saas` スキーマ）と疑似 JWT だけを前提にする。

秘密情報は持たない。SECRET_KEY は開発用の固定値で、公開環境では使えない。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-a-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",  # 管理画面のログイン（運用者）のためだけに要る。利用者の ID は Membership の外部 ID
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",  # 開発サーバーが管理画面の CSS・JS を配るため（試作は本番に出さない）
    "saas",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Django は `saas` スキーマだけを使う（search_path の先頭。django_migrations もここに作られる）。
# 既定のユーザーは、`saas` だけに権限を持つ saas_app。テストは DB の作成が要るので、SAAS_DB_USER=postgres で動かす。
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("SAAS_DB_NAME", "saas_dev"),
        "USER": os.environ.get("SAAS_DB_USER", "saas_app"),
        "HOST": os.environ.get("SAAS_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("SAAS_DB_PORT", "55432"),
        "OPTIONS": {"options": "-c search_path=saas"},
        "CONN_MAX_AGE": int(os.environ.get("SAAS_DB_CONN_MAX_AGE", "0")),
        # テスト用 DB は、`saas` スキーマだけが入ったテンプレートから作る（bench/bootstrap_db.py が用意する）
        "TEST": {"TEMPLATE": "saas_template"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "ja"
TIME_ZONE = "Asia/Tokyo"
USE_TZ = True
STATIC_URL = "static/"

# 利用者向け画面の試作ログイン（`sub` を選ぶか入力するだけ。パスワードも検証もない）。ローカルの確認専用で、既定は DEBUG と同じ。
# 公開する段階では、JWT の `sub` に置き換える（saas/session_auth.py の current_sub だけを差し替える）。
SAAS_DEMO_LOGIN = os.environ.get("SAAS_DEMO_LOGIN", "1" if DEBUG else "0") == "1"

# 疑似 JWT の検証（実際の Supabase には接続しない。公開鍵は JWKS のファイルから読む）
SAAS_JWT_ISSUER = os.environ.get("SAAS_JWT_ISSUER", "http://127.0.0.1:54321/auth/v1")
SAAS_JWT_AUDIENCE = "authenticated"
SAAS_JWKS_FILE = os.environ.get("SAAS_JWKS_FILE", "")
