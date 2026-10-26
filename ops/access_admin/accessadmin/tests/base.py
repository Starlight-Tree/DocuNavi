from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase

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

    def staff_client(self) -> Client:
        """ログイン済みの運用者（Django のスーパーユーザー）のクライアント。"""
        user = get_user_model().objects.create_superuser("operator", "operator@example.com", "pw-for-tests-only")
        client = Client()
        client.force_login(user)
        return client
