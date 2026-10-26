from django.db import connection
from django.test import TestCase


class AccessDbTestCase(TestCase):
    """本番と同じ権限（DB ロール access_admin）で動かす基底クラス。

    接続はスーパーユーザーだが、各テストの中で `SET LOCAL ROLE access_admin` に切り替える（トランザクションの巻き戻しで元に戻る）。
    権限が足りない操作は、テストが InsufficientPrivilege で落ちる。
    """

    def setUp(self) -> None:
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE access_admin")
