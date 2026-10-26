"""ローカルと CI のテスト用の設定。秘密情報は持たない（固定の SECRET_KEY は、この設定でしか使えない）。

DB は、一時 Postgres（scripts/test-postgres-up.sh。ポート 55432、trust 認証）。CI は環境変数で上書きする。
テスト用の DB は、`chatbot` / `auth` の身代わりと、supabase/migrations の SQL を入れたテンプレートから作る
（config/test_runner.py）。接続のユーザーは postgres で、テストの中で `SET LOCAL ROLE access_admin` に切り替えて権限を確かめる。
"""

import os

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-only-not-a-secret-0123456789abcdef"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "access_admin_dev",
        "USER": os.environ.get("ADMIN_TEST_DB_USER", "postgres"),
        "PASSWORD": os.environ.get("ADMIN_TEST_DB_PASSWORD", ""),
        "HOST": os.environ.get("ADMIN_TEST_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("ADMIN_TEST_DB_PORT", "55432"),
        "OPTIONS": {"options": "-c search_path=saas,chatbot"},
        "TEST": {"NAME": "test_access_admin", "TEMPLATE": "access_admin_template"},
    }
}

TEST_RUNNER = "config.test_runner.TemplateDatabaseRunner"

# テストを速くする（本番は PBKDF2 のまま）
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# 静的ファイルのディレクトリ（collectstatic の出力）は、テストでは作らない。WhiteNoise の警告を避ける
MIDDLEWARE = [m for m in MIDDLEWARE if not m.startswith("whitenoise")]  # noqa: F405
