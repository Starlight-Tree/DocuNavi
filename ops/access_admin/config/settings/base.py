"""設定の共通部分。秘密情報は持たない（SECRET_KEY・DB の接続先は prod.py が環境変数から読む）。

方針（docs/access-admin.md）:
- Django が変えるのは `saas` スキーマだけ。`chatbot` の表は supabase/migrations/ が唯一の変更経路で、このサービスの DB ロール
  （access_admin）には chatbot への CREATE を付けていない。chatbot の表を写すモデルは managed = False にする。
- 認証は、非公開の Cloud Run の IAM → Django のパスワード → TOTP の 3 段。
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

INSTALLED_APPS = [
    "config.apps.AccessAdminConfig",  # django.contrib.admin の代わり（default_site を差し替える）
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "accessadmin",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "ja"
TIME_ZONE = "Asia/Tokyo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ログイン後 1 時間で切れる（ブラウザーを閉じても切れる）。JavaScript から読めず、別サイトからの遷移では送られない
SESSION_COOKIE_AGE = 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

# TOTP の発行元の名前（認証アプリに出る）。コードの桁数・間隔・許容のずれは django-otp の既定（6 桁・30 秒・前後 1 ステップ）
OTP_TOTP_ISSUER = "ドキュナビ 管理"

LOGIN_URL = "admin:login"
LOGIN_REDIRECT_URL = "admin:index"

# 通常のログ（コンソール）。例外の本文・SQL・接続先をユーザーに返さない（DEBUG は常に False）
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
