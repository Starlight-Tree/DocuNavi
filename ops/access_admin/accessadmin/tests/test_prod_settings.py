"""config/settings/prod.py の検査。環境変数を差し替えて、設定モジュールを読み直す（本番の値は使わない）。"""

import importlib
import os
import sys
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

VALID = {
    "ADMIN_SECRET_KEY": "x" * 40,
    "ADMIN_ALLOWED_HOSTS": "admin.example.run.app, localhost",
    "ADMIN_DATABASE_URL": "postgresql://access_admin.proj:p%40ss@pooler.example.com:5432/postgres?sslmode=require",
}


def load_prod(**env: str):
    """prod 設定を、与えた環境変数だけで読み込む（足りなければ ImproperlyConfigured）。"""
    sys.modules.pop("config.settings.prod", None)
    with mock.patch.dict(os.environ, env, clear=True):
        return importlib.import_module("config.settings.prod")


class ProdSettingsTests(SimpleTestCase):
    def tearDown(self) -> None:
        sys.modules.pop("config.settings.prod", None)

    def test_valid_environment_gives_hardened_defaults(self):
        prod = load_prod(**VALID, ADMIN_CSRF_TRUSTED_ORIGINS="https://admin.example.run.app")
        self.assertFalse(prod.DEBUG)
        self.assertEqual(prod.ALLOWED_HOSTS, ["admin.example.run.app", "localhost"])
        self.assertEqual(prod.CSRF_TRUSTED_ORIGINS, ["https://admin.example.run.app"])
        self.assertEqual(prod.SESSION_COOKIE_AGE, 3600)
        self.assertTrue(prod.SESSION_COOKIE_HTTPONLY)
        self.assertEqual(prod.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertTrue(prod.SESSION_COOKIE_SECURE)
        self.assertTrue(prod.CSRF_COOKIE_SECURE)
        self.assertEqual(prod.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertEqual(prod.X_FRAME_OPTIONS, "DENY")

    def test_the_cookie_secure_flag_and_proxy_trust_can_be_switched_by_environment(self):
        prod = load_prod(**VALID, ADMIN_SECURE_COOKIES="0", ADMIN_TRUST_PROXY_SSL="0")
        self.assertFalse(prod.SESSION_COOKIE_SECURE)
        self.assertFalse(prod.CSRF_COOKIE_SECURE)
        self.assertFalse(hasattr(prod, "SECURE_PROXY_SSL_HEADER"))
        with self.assertRaises(ImproperlyConfigured):
            load_prod(**VALID, ADMIN_SECURE_COOKIES="maybe")

    def test_database_url_is_parsed_without_server_side_cursors_or_prepared_statements(self):
        db = load_prod(**VALID).DATABASES["default"]
        self.assertEqual(db["USER"], "access_admin.proj")
        self.assertEqual(db["PASSWORD"], "p@ss")  # URL エンコードを戻す
        self.assertEqual((db["HOST"], db["PORT"], db["NAME"]), ("pooler.example.com", 5432, "postgres"))
        self.assertEqual(db["OPTIONS"], {"prepare_threshold": None, "sslmode": "require"})
        self.assertTrue(db["DISABLE_SERVER_SIDE_CURSORS"])
        self.assertEqual(db["CONN_MAX_AGE"], 0)
        self.assertNotIn("search_path", str(db["OPTIONS"]))  # search_path は DB ロールの設定に任せる

    def test_missing_or_weak_settings_fail_the_start(self):
        for name in VALID:
            env = {k: v for k, v in VALID.items() if k != name}
            with self.subTest(missing=name), self.assertRaises(ImproperlyConfigured):
                load_prod(**env)
        with self.assertRaises(ImproperlyConfigured):
            load_prod(**{**VALID, "ADMIN_SECRET_KEY": "short"})

    def test_a_bad_database_url_fails_without_echoing_the_password(self):
        for url in (
            "mysql://u:TOPSECRET@h/db",
            "postgresql://:TOPSECRET@h/db",
            "postgresql://u:TOPSECRET@h:notaport/db",
            "TOPSECRET",
        ):
            with self.subTest(url=url), self.assertRaises(ImproperlyConfigured) as ctx:
                load_prod(**{**VALID, "ADMIN_DATABASE_URL": url})
            self.assertNotIn("TOPSECRET", str(ctx.exception))
