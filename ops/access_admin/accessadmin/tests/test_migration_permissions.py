"""supabase/migrations/20261026070500_access_admin.sql が作る権限を、実際の Postgres で確かめる。

grant の一覧を引くだけでなく、access_admin に切り替えて、許す操作が通り、許さない操作が InsufficientPrivilege になることを確かめる。
（本番の Supabase 上で、この SQL を postgres ロールが実行できることは、ここでは確かめられない。R3 の適用のときに確かめる。）
"""

import uuid
from contextlib import contextmanager

from django.db import ProgrammingError, connection, transaction

from .base import AccessDbTestCase

USER_ID = str(uuid.uuid4())


def _rows(sql: str, params: tuple = ()) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


class AccessAdminRoleTests(AccessDbTestCase):
    @contextmanager
    def superuser(self):
        """この間だけ、テストの接続のスーパーユーザーに戻る（準備の書き込みと、pg_authid の読み取り用）。"""
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE access_admin")

    def as_superuser(self, sql: str, params: tuple = ()) -> None:
        with self.superuser(), connection.cursor() as cursor:
            cursor.execute(sql, params)

    def assertDenied(self, sql: str, params: tuple = ()) -> None:
        with self.assertRaisesMessage(ProgrammingError, "permission denied"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, params)

    def test_role_is_a_plain_login_role_with_the_saas_first_search_path(self):
        with self.superuser():
            row = _rows(
                "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolinherit, rolreplication, "
                "rolpassword IS NULL FROM pg_authid WHERE rolname = 'access_admin'"  # パスワードはこの SQL では決めない
            )[0]
        self.assertEqual(row, (True, False, False, False, False, False, False, True))
        config = _rows("SELECT rolconfig FROM pg_roles WHERE rolname = 'access_admin'")[0][0]
        self.assertEqual(config, ["search_path=saas, chatbot"])

    def test_table_privileges_are_exactly_the_approved_set(self):
        rows = _rows(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'access_admin' AND table_schema = 'chatbot'"
        )
        granted: dict[str, set[str]] = {}
        for table, privilege in rows:
            granted.setdefault(table, set()).add(privilege)
        self.assertEqual(
            granted,
            {
                "user_roles": {"SELECT", "INSERT", "DELETE"},
                "roles": {"SELECT"},
                "role_collection_access": {"SELECT"},
                "access_audit": {"SELECT"},
                "document_collections": {"SELECT"},
                "admin_users": {"SELECT"},
            },
        )

    def test_no_privilege_on_the_search_data_or_on_creating_in_chatbot(self):
        for table in ("documents", "chunks"):
            self.assertFalse(
                _rows("SELECT has_table_privilege('access_admin', %s, 'SELECT')", (f"chatbot.{table}",))[0][0]
            )
        self.assertFalse(_rows("SELECT has_schema_privilege('access_admin', 'chatbot', 'CREATE')")[0][0])
        self.assertTrue(_rows("SELECT has_schema_privilege('access_admin', 'chatbot', 'USAGE')")[0][0])
        self.assertTrue(_rows("SELECT has_schema_privilege('access_admin', 'saas', 'CREATE')")[0][0])
        self.assertEqual(
            _rows("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'saas'")[0][0], "postgres"
        )

    def test_assigning_and_removing_a_role_work_and_are_audited(self):
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO chatbot.user_roles (user_id, role) VALUES (%s, 'default')", (USER_ID,))
            cursor.execute("DELETE FROM chatbot.user_roles WHERE user_id = %s", (USER_ID,))
        ops = _rows(
            "SELECT op FROM chatbot.access_audit WHERE table_name = 'user_roles' AND row_data->>'user_id' = %s ORDER BY id",
            (USER_ID,),
        )
        self.assertEqual(ops, [("INSERT",), ("DELETE",)])

    def test_everything_else_is_denied(self):
        self.as_superuser("INSERT INTO chatbot.user_roles (user_id, role) VALUES (%s, 'default')", (USER_ID,))
        self.assertDenied("UPDATE chatbot.user_roles SET role = 'default' WHERE user_id = %s", (USER_ID,))
        self.assertDenied("TRUNCATE chatbot.user_roles")
        self.assertDenied("INSERT INTO chatbot.roles (name, label) VALUES ('x', 'x')")
        self.assertDenied("DELETE FROM chatbot.roles WHERE name = 'default'")
        self.assertDenied("INSERT INTO chatbot.role_collection_access (role, collection) VALUES ('default', 'x')")
        self.assertDenied("DELETE FROM chatbot.role_collection_access")
        self.assertDenied("INSERT INTO chatbot.document_collections (name, label) VALUES ('x', 'x')")
        self.assertDenied("DELETE FROM chatbot.access_audit")
        self.assertDenied("SELECT * FROM chatbot.documents")
        self.assertDenied("SELECT * FROM chatbot.chunks")
        self.assertDenied("CREATE TABLE chatbot.should_not_exist (id int)")
        self.assertDenied("SELECT * FROM auth.users")  # auth スキーマには届かない。届くのはビューだけ

    def test_admin_users_exposes_only_id_email_and_last_sign_in(self):
        self.as_superuser(
            "INSERT INTO auth.users (id, email, encrypted_password, raw_user_meta_data) "
            "VALUES (%s, 'someone@example.com', 'HASH-MUST-NOT-LEAK', '{\"a\": 1}')",
            (USER_ID,),
        )
        columns = [
            r[0]
            for r in _rows(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'chatbot' AND table_name = 'admin_users' "
                "ORDER BY ordinal_position"
            )
        ]
        self.assertEqual(columns, ["id", "email", "last_sign_in_at"])
        self.assertEqual(
            _rows("SELECT id::text, email FROM chatbot.admin_users WHERE id = %s", (USER_ID,)),
            [(USER_ID, "someone@example.com")],
        )

    def test_admin_users_is_closed_to_the_other_roles(self):
        for role in ("anon", "authenticated", "chatbot_app"):
            self.assertFalse(
                _rows("SELECT has_table_privilege(%s, 'chatbot.admin_users', 'SELECT')", (role,))[0][0], role
            )
        self.assertFalse(_rows("SELECT has_table_privilege('public', 'chatbot.admin_users', 'SELECT')")[0][0])

    def test_the_app_role_keeps_its_read_only_access(self):
        for table in ("roles", "user_roles", "role_collection_access", "access_audit", "document_collections"):
            self.assertTrue(
                _rows("SELECT has_table_privilege('chatbot_app', %s, 'SELECT')", (f"chatbot.{table}",))[0][0], table
            )
            self.assertFalse(
                _rows("SELECT has_table_privilege('chatbot_app', %s, 'INSERT')", (f"chatbot.{table}",))[0][0], table
            )

    def test_the_migration_can_be_applied_twice(self):
        from config.test_runner import MIGRATIONS_DIR

        sql = (MIGRATIONS_DIR / "20261026070500_access_admin.sql").read_text(encoding="utf-8")
        self.as_superuser(sql)
        self.as_superuser(sql)
        self.assertTrue(_rows("SELECT has_table_privilege('access_admin', 'chatbot.user_roles', 'INSERT')")[0][0])
