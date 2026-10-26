from django.apps import apps
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection

from accessadmin.models import AccessAudit, AdminUser, Role

from .base import CAFE_STAFF, STUDENT_A, AccessDbTestCase

USERS = "/admin/accessadmin/adminuser/"


class ModelBoundaryTests(AccessDbTestCase):
    def test_every_model_of_the_app_is_unmanaged_so_migrate_never_touches_chatbot(self):
        models = list(apps.get_app_config("accessadmin").get_models())
        self.assertEqual({m.__name__ for m in models}, {"AdminUser", "Role", "AccessAudit"})
        self.assertTrue(all(m._meta.managed is False for m in models))

    def test_the_initial_migration_emits_no_ddl(self):
        from io import StringIO

        out = StringIO()
        call_command("sqlmigrate", "accessadmin", "0001", stdout=out)
        sql = out.getvalue().upper()
        self.assertNotIn("CREATE TABLE", sql)
        self.assertNotIn("CREATE VIEW", sql)
        self.assertNotIn("ALTER TABLE", sql)

    def test_standard_screens_that_are_not_needed_are_not_registered(self):
        self.assertFalse(admin.site.is_registered(Group))
        for model in (AdminUser, Role, AccessAudit, LogEntry):
            self.assertTrue(admin.site.is_registered(model), model)


class UserListTests(AccessDbTestCase):
    def setUp(self):
        super().setUp()
        self.seed_access_data()
        self.client = self.staff_client()

    def test_anonymous_visitors_are_sent_to_the_login(self):
        from django.test import Client

        response = Client().get(USERS)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_lists_users_with_current_roles_and_visible_groups(self):
        html = self.client.get(USERS).content.decode()
        for email in (
            "operator@example.com",
            "student-a@example.com",
            "student-b@example.com",
            "cafe-staff@example.com",
        ):
            self.assertIn(email, html)
        self.assertIn("cafeonly", html)  # CAFE_STAFF の割り当て
        self.assertIn("なし（default として扱う）", html)  # 割り当てが無い利用者
        self.assertIn("cafe, guides, zahard", html)  # default が見えるグループ

    def test_a_user_whose_roles_have_no_groups_sees_the_no_materials_text(self):
        self.as_superuser("INSERT INTO chatbot.roles (name, label) VALUES ('nothing', '無し')")
        self.as_superuser("INSERT INTO chatbot.user_roles (user_id, role) VALUES (%s, 'nothing')", (STUDENT_A,))
        html = self.client.get(USERS + "?q=student-a").content.decode()
        self.assertIn("なし（利用できる資料がありません）", html)

    def test_search_by_email(self):
        html = self.client.get(USERS + "?q=student-a").content.decode()
        self.assertIn("student-a@example.com", html)
        self.assertNotIn("student-b@example.com", html)
        self.assertNotIn("cafe-staff@example.com", html)

    def test_filter_by_assignment(self):
        yes = self.client.get(USERS + "?assigned=yes").content.decode()
        self.assertIn("cafe-staff@example.com", yes)
        self.assertNotIn("student-a@example.com", yes)
        no = self.client.get(USERS + "?assigned=no").content.decode()
        self.assertNotIn("cafe-staff@example.com", no)
        for email in ("operator@example.com", "student-a@example.com", "student-b@example.com"):
            self.assertIn(email, no)

    def test_the_list_runs_a_fixed_number_of_queries(self):
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as small:
            self.client.get(USERS)
        for i in range(30):
            self.as_superuser(
                "INSERT INTO auth.users (id, email) VALUES (gen_random_uuid(), %s)", (f"bulk-{i}@example.com",)
            )
        with CaptureQueriesContext(connection) as large:
            self.client.get(USERS)
        self.assertEqual(len(small), len(large))  # 利用者が増えても、問い合わせは増えない

    def test_nobody_can_add_change_or_delete_a_user(self):
        self.assertEqual(self.client.get(USERS + "add/").status_code, 403)
        self.assertEqual(self.client.post(USERS + "add/", {"email": "x@example.com"}).status_code, 403)
        self.assertEqual(self.client.post(f"{USERS}{STUDENT_A}/delete/", {"post": "yes"}).status_code, 403)
        self.assertEqual(self.client.post(f"{USERS}{STUDENT_A}/change/", {"email": "x@example.com"}).status_code, 403)
        self.assertEqual(AdminUser.objects.count(), 4)


class RoleListTests(AccessDbTestCase):
    def setUp(self):
        super().setUp()
        self.seed_access_data()
        self.client = self.staff_client()

    def test_lists_roles_with_groups_and_user_counts(self):
        html = self.client.get("/admin/accessadmin/role/").content.decode()
        self.assertIn("cafeonly", html)
        self.assertIn("店舗だけ", html)
        self.assertIn("cafe, guides, zahard", html)  # default

    def test_warns_about_a_registered_group_that_no_role_has(self):
        self.as_superuser(
            "INSERT INTO chatbot.document_collections (name, label) VALUES ('newgroup', '新しいグループ')"
        )
        html = self.client.get("/admin/accessadmin/role/").content.decode()
        self.assertIn("どのロールにも割り当てられていない資料グループ", html)
        self.assertIn("newgroup", html)

    def test_no_warning_when_every_registered_group_is_in_some_role(self):
        html = self.client.get("/admin/accessadmin/role/").content.decode()
        self.assertNotIn("どのロールにも割り当てられていない", html)

    def test_roles_are_read_only(self):
        self.assertEqual(self.client.get("/admin/accessadmin/role/add/").status_code, 403)
        self.assertEqual(self.client.post("/admin/accessadmin/role/cafeonly/delete/", {"post": "yes"}).status_code, 403)
        self.assertEqual(Role.objects.count(), 3)


class AuditListTests(AccessDbTestCase):
    def setUp(self):
        super().setUp()
        self.seed_access_data()
        self.client = self.staff_client()

    def test_shows_the_changes_recorded_by_the_triggers(self):
        html = self.client.get("/admin/accessadmin/accessaudit/").content.decode()
        self.assertIn("user_roles", html)
        self.assertIn("INSERT", html)
        self.assertIn(CAFE_STAFF, html)

    def test_filter_by_table(self):
        html = self.client.get("/admin/accessadmin/accessaudit/?table_name=roles").content.decode()
        self.assertNotIn(CAFE_STAFF, html)

    def test_the_record_is_read_only(self):
        self.assertEqual(self.client.get("/admin/accessadmin/accessaudit/add/").status_code, 403)
        before = AccessAudit.objects.count()
        first = AccessAudit.objects.order_by("id").first()
        self.assertEqual(
            self.client.post(f"/admin/accessadmin/accessaudit/{first.id}/delete/", {"post": "yes"}).status_code, 403
        )
        self.assertEqual(AccessAudit.objects.count(), before)


class OperationLogTests(AccessDbTestCase):
    def test_the_django_operation_log_is_viewable_but_not_editable(self):
        client = self.staff_client()
        self.assertEqual(client.get("/admin/admin/logentry/").status_code, 200)
        self.assertEqual(client.get("/admin/admin/logentry/add/").status_code, 403)
