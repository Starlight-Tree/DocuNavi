import uuid

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client

from .base import CAFE_STAFF, STUDENT_A, STUDENT_B, AccessDbTestCase

DEFAULT_GROUPS = "cafe, guides, zahard"


def url(user_id: str) -> str:
    return f"/admin/accessadmin/adminuser/{user_id}/roles/"


def roles_of(user_id: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT role FROM chatbot.user_roles WHERE user_id = %s", (user_id,))
        return {row[0] for row in cursor.fetchall()}


def audit_ops(user_id: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT op FROM chatbot.access_audit WHERE table_name = 'user_roles' AND row_data->>'user_id' = %s ORDER BY id",
            (user_id,),
        )
        return [row[0] for row in cursor.fetchall()]


class AssignRolesTestCase(AccessDbTestCase):
    def setUp(self):
        super().setUp()
        self.seed_access_data()
        self.client = self.staff_client()
        self.audit_before = {u: len(audit_ops(u)) for u in (STUDENT_A, STUDENT_B, CAFE_STAFF)}

    def preview(self, user_id, roles, base=(), **extra):
        return self.client.post(url(user_id), {"action": "preview", "roles": list(roles), "base": list(base), **extra})

    def apply(self, user_id, roles, base=(), **extra):
        return self.client.post(url(user_id), {"action": "apply", "roles": list(roles), "base": list(base), **extra})


class SelectScreenTests(AssignRolesTestCase):
    def test_shows_the_user_the_roles_and_what_each_role_grants(self):
        html = self.client.get(url(CAFE_STAFF)).content.decode()
        self.assertIn("cafe-staff@example.com", html)
        self.assertIn("cafeonly", html)
        self.assertIn("店舗だけ", html)
        self.assertIn('value="cafeonly" id="role-cafeonly" checked', html)  # いまの割り当て
        self.assertNotIn('id="role-guidesonly" checked', html)
        self.assertIn('name="with_default"', html)

    def test_a_user_without_assignments_is_shown_with_the_default_groups(self):
        html = self.client.get(url(STUDENT_A)).content.decode()
        self.assertIn(DEFAULT_GROUPS, html)

    def test_the_list_links_to_the_screen(self):
        html = self.client.get("/admin/accessadmin/adminuser/").content.decode()
        self.assertIn(url(STUDENT_A), html)

    def test_unknown_user_is_404(self):
        self.assertEqual(self.client.get(url(str(uuid.uuid4()))).status_code, 404)

    def test_only_a_superuser_may_use_it(self):
        staff = get_user_model().objects.create_user("staff", "s@example.com", "pw", is_staff=True)
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.get(url(STUDENT_A)).status_code, 403)
        self.assertEqual(client.post(url(STUDENT_A), {"action": "apply", "roles": ["cafeonly"]}).status_code, 403)
        self.assertEqual(roles_of(STUDENT_A), set())

    def test_anonymous_visitors_are_sent_to_the_login(self):
        response = Client().get(url(STUDENT_A))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_other_methods_are_not_allowed(self):
        self.assertEqual(self.client.put(url(STUDENT_A)).status_code, 405)
        self.assertEqual(self.client.delete(url(STUDENT_A)).status_code, 405)


class PreviewTests(AssignRolesTestCase):
    def test_preview_shows_before_and_after_and_writes_nothing(self):
        response = self.preview(STUDENT_A, ["cafeonly"])
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(DEFAULT_GROUPS, html)  # 変更前
        self.assertIn("<strong>cafe</strong>", html)  # 変更後
        self.assertIn("guides, zahard", html)  # 見えなくなる
        self.assertIn('name="confirm_shrink"', html)  # 減るので確認が要る
        self.assertIn("default</code> のグループは見えなくなります", html)
        self.assertEqual(roles_of(STUDENT_A), set())
        self.assertEqual(len(audit_ops(STUDENT_A)), self.audit_before[STUDENT_A])

    def test_preview_of_a_change_that_does_not_shrink_asks_for_no_confirmation(self):
        html = self.preview(STUDENT_A, ["cafeonly", "default"], with_default="1").content.decode()
        self.assertNotIn('name="confirm_shrink"', html)

    def test_preview_flags_a_user_who_would_see_nothing(self):
        self.as_superuser("INSERT INTO chatbot.roles (name, label) VALUES ('nothing', '無し')")
        html = self.preview(STUDENT_A, ["nothing"]).content.decode()
        self.assertIn("利用できる資料がありません", html)

    def test_no_change_redirects_back(self):
        response = self.preview(CAFE_STAFF, ["cafeonly"], base=["cafeonly"])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(roles_of(CAFE_STAFF), {"cafeonly"})

    def test_unknown_role_is_rejected_without_echoing_it(self):
        response = self.preview(STUDENT_A, ["ghost-role"])
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("ghost-role", response.content.decode())
        self.assertEqual(roles_of(STUDENT_A), set())


class ApplyTests(AssignRolesTestCase):
    def test_a_shrinking_change_without_the_confirmation_is_refused(self):
        response = self.apply(STUDENT_A, ["cafeonly"], base=[])
        self.assertEqual(response.status_code, 400)
        self.assertIn("確認のチェックが必要です", response.content.decode())
        self.assertEqual(roles_of(STUDENT_A), set())  # 何も書かない
        self.assertEqual(len(LogEntry.objects.all()), 0)

    def test_a_confirmed_change_is_saved_audited_and_logged(self):
        response = self.apply(STUDENT_A, ["cafeonly"], base=[], confirm_shrink="1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(roles_of(STUDENT_A), {"cafeonly"})
        self.assertEqual(audit_ops(STUDENT_A)[self.audit_before[STUDENT_A] :], ["INSERT"])  # DB のトリガーの記録
        entry = LogEntry.objects.get()
        self.assertEqual((entry.action_flag, entry.object_id, entry.object_repr), (CHANGE, STUDENT_A, STUDENT_A))
        self.assertIn("cafeonly", entry.change_message)
        self.assertNotIn("student-a@example.com", entry.change_message + entry.object_repr)  # メールは載せない
        self.assertEqual(entry.user.username, "operator")

    def test_default_together_keeps_what_the_user_sees_and_needs_no_confirmation(self):
        self.apply(STUDENT_A, ["cafeonly"], base=[], with_default="1")
        self.assertEqual(roles_of(STUDENT_A), {"cafeonly", "default"})

    def test_removing_every_role_returns_to_default_and_is_audited(self):
        response = self.apply(CAFE_STAFF, [], base=["cafeonly"])
        self.assertEqual(response.status_code, 302)  # 広がる変更なので確認は要らない
        self.assertEqual(roles_of(CAFE_STAFF), set())
        self.assertEqual(audit_ops(CAFE_STAFF)[self.audit_before[CAFE_STAFF] :], ["DELETE"])

    def test_swapping_a_role_deletes_and_inserts(self):
        self.apply(CAFE_STAFF, ["guidesonly"], base=["cafeonly"], confirm_shrink="1")
        self.assertEqual(roles_of(CAFE_STAFF), {"guidesonly"})
        self.assertEqual(sorted(audit_ops(CAFE_STAFF)[self.audit_before[CAFE_STAFF] :]), ["DELETE", "INSERT"])

    def test_a_stale_screen_is_refused(self):
        """画面を開いたあとに別の変更が入っていたら、保存しない（送られた元の割り当てと、いまの DB が違う）。"""
        self.as_superuser("INSERT INTO chatbot.user_roles (user_id, role) VALUES (%s, 'guidesonly')", (STUDENT_A,))
        response = self.apply(STUDENT_A, ["cafeonly"], base=[], confirm_shrink="1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(roles_of(STUDENT_A), {"guidesonly"})  # 変わらない
        self.assertEqual(len(LogEntry.objects.all()), 0)

    def test_the_server_recomputes_instead_of_trusting_the_posted_values(self):
        """送られた値のうち使うのは、ロール名・元の割り当て・確認のチェックだけ。見えるグループなどを送っても無視する。"""
        response = self.apply(
            STUDENT_A, ["cafeonly"], base=[], lost="なし", after="cafe, guides, zahard", needs_confirm="", shrinks="0"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(roles_of(STUDENT_A), set())

    def test_unknown_role_is_rejected(self):
        self.assertEqual(self.apply(STUDENT_A, ["cafeonly", "ghost"], base=[], confirm_shrink="1").status_code, 404)
        self.assertEqual(roles_of(STUDENT_A), set())  # 一部だけ書かれることもない

    def test_applying_the_same_change_twice_does_not_duplicate(self):
        self.apply(STUDENT_B, ["cafeonly"], base=[], confirm_shrink="1")
        again = self.apply(STUDENT_B, ["cafeonly"], base=[], confirm_shrink="1")
        self.assertEqual(again.status_code, 302)  # 2 回目は、元の割り当てが違うので保存しない
        self.assertEqual(roles_of(STUDENT_B), {"cafeonly"})
        self.assertEqual(len(LogEntry.objects.all()), 1)

    def test_get_never_writes(self):
        self.client.get(url(STUDENT_A) + "?action=apply&roles=cafeonly&confirm_shrink=1")
        self.assertEqual(roles_of(STUDENT_A), set())

    def test_csrf_is_enforced(self):
        user = get_user_model().objects.get(username="operator")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        response = client.post(url(STUDENT_A), {"action": "apply", "roles": ["cafeonly"], "confirm_shrink": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(roles_of(STUDENT_A), set())

    def test_the_new_assignment_is_what_the_user_sees_in_the_list(self):
        self.apply(STUDENT_A, ["guidesonly"], base=[], confirm_shrink="1")
        html = self.client.get("/admin/accessadmin/adminuser/?q=student-a").content.decode()
        self.assertIn('class="field-current_roles">guidesonly<', html)
        self.assertIn('class="field-visible_groups">guides<', html)
