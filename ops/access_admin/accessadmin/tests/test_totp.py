from io import StringIO

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client
from django_otp.plugins.otp_totp.models import TOTPDevice

from .base import STUDENT_A, AccessDbTestCase, current_token

PASSWORD = "pw-for-tests-only"
USERS = "/admin/accessadmin/adminuser/"


def location(response) -> str:
    return response["Location"]


class TotpTestCase(AccessDbTestCase):
    def setUp(self):
        super().setUp()
        self.seed_access_data()
        self.user = self.make_operator()

    def password_login(self, client: Client, **kwargs):
        return client.post("/admin/login/", {"username": "operator", "password": PASSWORD, "next": "/admin/"}, **kwargs)

    def confirmed_device(self) -> TOTPDevice:
        return TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)


class GateTests(TotpTestCase):
    def test_a_password_only_session_cannot_open_any_admin_page(self):
        client = self.password_only_client(self.user)
        for path in (
            "/admin/",
            USERS,
            "/admin/accessadmin/role/",
            "/admin/accessadmin/accessaudit/",
            "/admin/auth/user/",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/admin/login/", location(response), path)

    def test_a_password_only_session_cannot_write(self):
        client = self.password_only_client(self.user)
        response = client.post(
            f"{USERS}{STUDENT_A}/roles/", {"action": "apply", "roles": ["cafeonly"], "base": [], "confirm_shrink": "1"}
        )
        self.assertEqual(response.status_code, 302)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM chatbot.user_roles WHERE user_id = %s", (STUDENT_A,))
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_an_unregistered_superuser_is_sent_to_the_registration(self):
        client = Client()
        response = self.password_login(client)
        self.assertEqual(response.status_code, 302)
        final = client.get("/admin/", follow=True)
        self.assertEqual(final.redirect_chain[-1][0], "/otp/setup/")
        self.assertContains(final, "認証アプリの登録")

    def test_a_registered_user_is_sent_to_the_code_entry(self):
        self.confirmed_device()
        client = Client()
        self.password_login(client)
        final = client.get("/admin/", follow=True)
        self.assertEqual(final.redirect_chain[-1][0], "/otp/verify/")
        self.assertContains(final, "ワンタイムコードの入力")

    def test_the_stock_totp_device_screen_is_not_available(self):
        client = self.staff_client()
        self.assertEqual(client.get("/admin/otp_totp/totpdevice/").status_code, 404)

    def test_health_checks_need_no_totp(self):
        self.assertEqual(Client().get("/healthz").status_code, 200)

    def test_session_cookie_flags_after_the_password_login(self):
        client = Client()
        response = self.password_login(client)
        cookie = response.cookies["sessionid"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(
            int(cookie["max-age"] or 0), 0
        )  # ブラウザーを閉じると切れる（SESSION_EXPIRE_AT_BROWSER_CLOSE）
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_AGE, 3600)


class RegistrationTests(TotpTestCase):
    def open_setup(self):
        client = self.password_only_client(self.user)
        return client, client.get("/otp/setup/")

    def test_the_page_shows_a_qr_but_never_the_secret_as_text(self):
        client, response = self.open_setup()
        self.assertContains(response, "<svg")
        device = TOTPDevice.objects.get(user=self.user)
        html = response.content.decode()
        self.assertNotIn("otpauth://", html)
        self.assertNotIn("secret=", html)
        self.assertNotIn(device.key, html)
        from base64 import b32encode

        self.assertNotIn(b32encode(device.bin_key).decode().rstrip("="), html)
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_pages_use_the_site_name_and_a_page_title(self):
        _, response = self.open_setup()
        self.assertContains(response, "ドキュナビ 資料グループの管理")
        self.assertContains(response, "<title>認証アプリの登録 | ドキュナビ 管理</title>", html=True)

    def test_the_same_pending_device_is_shown_until_it_is_confirmed(self):
        client, _ = self.open_setup()
        client.get("/otp/setup/")
        self.assertEqual(TOTPDevice.objects.filter(user=self.user).count(), 1)

    def test_a_wrong_code_does_not_register_or_open_the_admin(self):
        client, _ = self.open_setup()
        response = client.post("/otp/setup/", {"token": "000000"})
        self.assertContains(response, "コードが正しくありません")
        self.assertFalse(TOTPDevice.objects.get(user=self.user).confirmed)
        self.assertEqual(client.get("/admin/").status_code, 302)

    def test_a_non_numeric_code_is_rejected(self):
        client, _ = self.open_setup()
        self.assertContains(client.post("/otp/setup/", {"token": "abcdef"}), "コードが正しくありません")
        self.assertFalse(TOTPDevice.objects.get(user=self.user).confirmed)

    def test_the_right_code_registers_the_device_and_opens_the_admin(self):
        client, _ = self.open_setup()
        device = TOTPDevice.objects.get(user=self.user)
        response = client.post("/otp/setup/", {"token": current_token(device), "next": "/admin/"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TOTPDevice.objects.get(user=self.user).confirmed)
        self.assertEqual(client.get("/admin/").status_code, 200)
        self.assertEqual(client.get(USERS).status_code, 200)

    def test_nothing_about_the_device_reaches_the_operation_log_or_other_pages(self):
        client, _ = self.open_setup()
        device = TOTPDevice.objects.get(user=self.user)
        client.post("/otp/setup/", {"token": current_token(device)})
        self.assertEqual(LogEntry.objects.count(), 0)
        for path in ("/admin/", USERS, "/admin/admin/logentry/"):
            self.assertNotIn(device.key, client.get(path).content.decode())

    def test_registration_is_closed_once_a_confirmed_device_exists(self):
        self.confirmed_device()
        client = self.password_only_client(self.user)
        response = client.get("/otp/setup/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/otp/verify/", location(response))
        self.assertEqual(TOTPDevice.objects.filter(user=self.user).count(), 1)  # 新しい機器は作らない

    def test_anonymous_visitors_cannot_register(self):
        response = Client().get("/otp/setup/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", location(response))


class VerificationTests(TotpTestCase):
    def setUp(self):
        super().setUp()
        self.device = self.confirmed_device()
        self.client = self.password_only_client(self.user)

    def test_the_right_code_opens_the_admin(self):
        response = self.client.post("/otp/verify/", {"token": current_token(self.device), "next": USERS})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(location(response), USERS)
        self.assertEqual(self.client.get(USERS).status_code, 200)

    def test_a_wrong_code_is_refused(self):
        response = self.client.post("/otp/verify/", {"token": "123456"})
        self.assertContains(response, "コードが正しくありません")
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_an_empty_code_is_refused(self):
        self.assertContains(self.client.post("/otp/verify/", {"token": ""}), "コードが正しくありません")
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_a_used_code_cannot_be_used_again(self):
        token = current_token(self.device)
        self.assertEqual(self.client.post("/otp/verify/", {"token": token}).status_code, 302)
        second = self.password_only_client(self.user)
        self.assertContains(second.post("/otp/verify/", {"token": token}), "コードが正しくありません")
        self.assertEqual(second.get("/admin/").status_code, 302)

    def test_repeated_failures_are_throttled_even_for_the_right_code(self):
        for _ in range(3):
            self.client.post("/otp/verify/", {"token": "000000"})
        self.assertContains(
            self.client.post("/otp/verify/", {"token": current_token(self.device)}), "コードが正しくありません"
        )
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_an_external_next_url_is_ignored(self):
        response = self.client.post(
            "/otp/verify/", {"token": current_token(self.device), "next": "https://evil.example/"}
        )
        self.assertEqual(location(response), "/admin/")

    def test_another_users_device_cannot_verify_this_session(self):
        other = get_user_model().objects.create_superuser("other", "o@example.com", PASSWORD)
        other_device = TOTPDevice.objects.create(user=other, name="default", confirmed=True)
        self.assertContains(
            self.client.post("/otp/verify/", {"token": current_token(other_device)}), "コードが正しくありません"
        )

    def test_the_session_id_changes_after_the_code(self):
        before = self.client.session.session_key
        self.client.post("/otp/verify/", {"token": current_token(self.device)})
        self.assertNotEqual(self.client.session.session_key, before)

    def test_signing_out_from_the_code_page_ends_the_session(self):
        self.client.post("/otp/sign-out/")
        self.assertEqual(self.client.get("/otp/verify/").status_code, 302)
        self.assertIn("/admin/login/", location(self.client.get("/otp/verify/")))

    def test_logout_of_the_admin_ends_the_verification(self):
        self.client.post("/otp/verify/", {"token": current_token(self.device)})
        self.client.post("/admin/logout/")
        self.assertEqual(self.client.get("/admin/").status_code, 302)


class ResetCommandTests(TotpTestCase):
    def test_reset_deletes_the_devices_and_prints_no_secret(self):
        device = self.confirmed_device()
        out = StringIO()
        call_command("totp_reset", "operator", stdout=out)
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertIn("1 台", out.getvalue())
        self.assertNotIn(device.key, out.getvalue())

    def test_after_a_reset_the_next_login_registers_again(self):
        self.confirmed_device()
        call_command("totp_reset", "operator", stdout=StringIO())
        client = self.password_only_client(self.user)
        self.assertIn("/otp/setup/", location(client.get("/admin/login/")))

    def test_unknown_user(self):
        with self.assertRaises(CommandError):
            call_command("totp_reset", "nobody", stdout=StringIO())

    def test_other_users_keep_their_devices(self):
        other = get_user_model().objects.create_superuser("other", "o@example.com", PASSWORD)
        TOTPDevice.objects.create(user=other, name="default", confirmed=True)
        self.confirmed_device()
        call_command("totp_reset", "operator", stdout=StringIO())
        self.assertTrue(TOTPDevice.objects.filter(user=other).exists())
