from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

OPERATOR = "11111111-1111-4111-8111-111111111111"
STUDENT_A = "22222222-2222-4222-8222-222222222222"
STUDENT_B = "33333333-3333-4333-8333-333333333333"
CAFE_STAFF = "44444444-4444-4444-8444-444444444444"


class AccessDbTestCase(TestCase):
    """本番と同じ権限（DB ロール access_admin）で動かす基底クラス。

    接続はスーパーユーザーだが、各テストの中で `SET LOCAL ROLE access_admin` に切り替える（トランザクションの巻き戻しで元に戻る）。
    権限が足りない操作は、テストが InsufficientPrivilege で落ちる。
    """

    def setUp(self) -> None:
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE access_admin")

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

    def seed_access_data(self) -> None:
        """利用者 4 人と、ロール 2 つ（cafeonly / guidesonly）。CAFE_STAFF だけ cafeonly を割り当て済み。default は初期データのまま。"""
        with self.superuser(), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO auth.users (id, email, encrypted_password, last_sign_in_at) VALUES "
                "(%s, 'operator@example.com', 'HASH', now()), (%s, 'student-a@example.com', 'HASH', now()), "
                "(%s, 'student-b@example.com', 'HASH', NULL), (%s, 'cafe-staff@example.com', 'HASH', now())",
                (OPERATOR, STUDENT_A, STUDENT_B, CAFE_STAFF),
            )
            cursor.execute(
                "INSERT INTO chatbot.roles (name, label) VALUES ('cafeonly', '店舗だけ'), ('guidesonly', '規程だけ')"
            )
            cursor.execute(
                "INSERT INTO chatbot.role_collection_access (role, collection) VALUES ('cafeonly', 'cafe'), ('guidesonly', 'guides')"
            )
            cursor.execute("INSERT INTO chatbot.user_roles (user_id, role) VALUES (%s, 'cafeonly')", (CAFE_STAFF,))

    def make_operator(self, username: str = "operator", *, superuser: bool = True):
        user_model = get_user_model()
        existing = user_model.objects.filter(username=username).first()
        if existing is not None:
            return existing
        if superuser:
            return user_model.objects.create_superuser(username, f"{username}@example.com", "pw-for-tests-only")
        return user_model.objects.create_user(username, f"{username}@example.com", "pw-for-tests-only", is_staff=True)

    def password_only_client(self, user=None) -> Client:
        """パスワードでログインしただけ（TOTP は未確認）のクライアント。"""
        client = Client()
        client.force_login(user or self.make_operator())
        return client

    def staff_client(self) -> Client:
        """パスワードに加えて TOTP も確認済みの運用者（Django のスーパーユーザー）のクライアント。"""
        user = self.make_operator()
        client = self.password_only_client(user)
        device = TOTPDevice.objects.create(user=user, name="test", confirmed=True)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
        return client


def current_token(device: TOTPDevice) -> str:
    """その機器の、いま有効なコード（認証アプリが出すものと同じ）。"""
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return str(totp.token()).zfill(device.digits)
