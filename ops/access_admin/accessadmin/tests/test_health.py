import json
from unittest import mock

from django.db import connection
from django.test import Client

from .base import AccessDbTestCase


class HealthTests(AccessDbTestCase):
    def test_healthz_answers_without_the_database(self):
        with mock.patch.object(connection, "cursor", side_effect=AssertionError("healthz は DB を見ない")):
            response = Client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_readyz_is_ok_when_search_path_view_and_migrations_are_in_place(self):
        response = Client().get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {"status": "ok"})

    def test_readyz_fails_when_saas_is_not_first_in_the_search_path(self):
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL search_path = chatbot")
        response = Client().get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content), {"status": "unavailable", "check": "search_path"})

    def test_readyz_fails_when_the_admin_users_view_cannot_be_read(self):
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("REVOKE SELECT ON chatbot.admin_users FROM access_admin")
            cursor.execute("SET LOCAL ROLE access_admin")
        response = Client().get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["check"], "admin_users")

    def test_readyz_never_returns_the_exception_text(self):
        secret = "postgresql://user:SECRET-PASSWORD@host/db"
        with mock.patch("accessadmin.health.CHECKS", (("search_path", mock.Mock(side_effect=RuntimeError(secret))),)):
            with self.assertLogs("accessadmin.health", level="WARNING") as logs:
                response = Client().get("/readyz")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.content.decode())
        self.assertNotIn("SECRET", "\n".join(logs.output))  # ログにも型の名前だけ

    def test_health_endpoints_do_not_accept_writes(self):
        self.assertEqual(Client().post("/healthz").status_code, 405)
        self.assertEqual(Client().post("/readyz").status_code, 405)
