"""`chatbot` スキーマの表を写すモデル。すべて managed = False（Django は表を作らず、migrate で触らない）。

`chatbot` の表の変更は supabase/migrations/ だけが行う。表名は、search_path に頼らないよう `chatbot` を付けて書く
（`db_table` の引用符の書き方は、Django の PostgreSQL で schema を指定する慣用の形）。
書き込み（user_roles の割り当てと解除）は、監査を明示するため、モデルではなく db.py の SQL で行う。
"""

from django.db import models


class AdminUser(models.Model):
    """`chatbot.admin_users`（auth.users のうち id / email / last_sign_in_at だけを見せる読み取り専用のビュー）。"""

    id = models.UUIDField(primary_key=True)
    email = models.TextField(null=True)
    last_sign_in_at = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = 'chatbot"."admin_users'
        ordering = ["email"]
        verbose_name = "利用者"
        verbose_name_plural = "利用者"

    def __str__(self) -> str:
        return self.email or str(self.id)


class Role(models.Model):
    """`chatbot.roles`。読み取りだけ（ロールの作成・変更は 4a の対象外。SQL で行う）。"""

    name = models.TextField(primary_key=True)
    label = models.TextField()

    class Meta:
        managed = False
        db_table = 'chatbot"."roles'
        ordering = ["name"]
        verbose_name = "ロール"
        verbose_name_plural = "ロール"

    def __str__(self) -> str:
        return self.name


class AccessAudit(models.Model):
    """`chatbot.access_audit`。roles / user_roles / role_collection_access の変更の記録（追記だけ。トリガーが書く）。"""

    id = models.BigIntegerField(primary_key=True)
    at = models.DateTimeField()
    actor = models.TextField()
    table_name = models.TextField()
    op = models.TextField()
    row_data = models.JSONField(null=True)

    class Meta:
        managed = False
        db_table = 'chatbot"."access_audit'
        ordering = ["-id"]
        verbose_name = "権限の変更の記録"
        verbose_name_plural = "権限の変更の記録"

    def __str__(self) -> str:
        return f"{self.table_name} {self.op} #{self.id}"
