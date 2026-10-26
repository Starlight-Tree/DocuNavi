"""管理画面。標準の Django Admin の枠で、利用者・ロール・変更の記録を一覧する（4a の範囲では、書き込みは利用者ごとの割り当てだけ）。

どのモデルも追加・変更・削除はできない（user_roles への書き込みは、専用の画面。DB ロールにもそれ以外の権限が無い）。
"""

from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry
from django.contrib.admin.views.main import ChangeList
from django.contrib.auth.models import Group
from django.db import models
from django.db.models.expressions import RawSQL
from django.http import HttpRequest

from . import db, services
from .models import AccessAudit, AdminUser, Role

# このサービスでは使わない標準の画面。入れておくと操作の対象が増える
admin.site.unregister(Group)


class ReadOnlyAdmin(admin.ModelAdmin):
    """閲覧だけ。追加・変更・削除の権限を持たせない（URL を直接開いても 403）。"""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


class AssignmentFilter(admin.SimpleListFilter):
    title = "ロールの割り当て"
    parameter_name = "assigned"

    def lookups(self, request, model_admin):
        return [("yes", "あり"), ("no", "なし（default として扱う）")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(role_names__isnull=False)
        if self.value() == "no":
            return queryset.filter(role_names__isnull=True)
        return queryset


class AdminUserChangeList(ChangeList):
    """1 ページぶんの利用者に、割り当てと見えるグループを付ける（ロールの表は 1 回だけ読む）。"""

    def get_results(self, request):
        super().get_results(request)
        users = list(self.result_list)
        matrix = db.access_matrix()
        for user in users:
            user.assigned = frozenset(filter(None, (user.role_names or "").split(",")))
            user.visible = services.visible_groups(user.assigned, matrix)
        self.result_list = users


@admin.register(AdminUser)
class AdminUserAdmin(ReadOnlyAdmin):
    list_display = ("email", "last_sign_in_at", "current_roles", "visible_groups")
    search_fields = ("email",)
    list_filter = (AssignmentFilter,)
    list_per_page = 50
    ordering = ("email",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                role_names=RawSQL(
                    "(SELECT string_agg(ur.role, ',' ORDER BY ur.role) FROM chatbot.user_roles ur "
                    'WHERE ur.user_id = "chatbot"."admin_users"."id")',
                    [],
                    output_field=models.TextField(),
                )
            )
        )

    def get_changelist(self, request, **kwargs):
        return AdminUserChangeList

    @admin.display(description="現在のロール")
    def current_roles(self, obj: AdminUser) -> str:
        return ", ".join(sorted(obj.assigned)) or f"なし（{services.DEFAULT_ROLE} として扱う）"

    @admin.display(description="見えるグループ")
    def visible_groups(self, obj: AdminUser) -> str:
        return ", ".join(sorted(obj.visible)) or "なし（利用できる資料がありません）"


@admin.register(Role)
class RoleAdmin(ReadOnlyAdmin):
    list_display = ("name", "label", "groups", "user_count")

    def changelist_view(self, request, extra_context=None):
        # 一覧の描画で使う値を、1 回ずつ読む。リクエストをまたいで持たない（スレッドをまたぐ状態を作らない）
        matrix = db.access_matrix()
        request.access_matrix = matrix
        request.role_user_counts = db.role_user_counts()
        stray = services.unassigned_groups(db.registered_collections(), matrix)
        if stray:
            self.message_user(
                request,
                "どのロールにも割り当てられていない資料グループがあります（default を含め、誰にも見えません）: "
                + ", ".join(stray),
                level=messages.WARNING,
            )
        return super().changelist_view(request, extra_context)

    def get_changelist(self, request, **kwargs):
        return RoleChangeList

    @admin.display(description="見られるグループ")
    def groups(self, obj: Role) -> str:
        return ", ".join(sorted(obj.group_names)) or "なし"

    @admin.display(description="割り当て済みの利用者")
    def user_count(self, obj: Role) -> int:
        return obj.user_total


class RoleChangeList(ChangeList):
    def get_results(self, request):
        super().get_results(request)
        roles = list(self.result_list)
        matrix = getattr(request, "access_matrix", None) or db.access_matrix()
        counts = getattr(request, "role_user_counts", None) or db.role_user_counts()
        for role in roles:
            role.group_names = matrix.get(role.name, set())
            role.user_total = counts.get(role.name, 0)
        self.result_list = roles


@admin.register(AccessAudit)
class AccessAuditAdmin(ReadOnlyAdmin):
    list_display = ("id", "at", "actor", "table_name", "op", "summary")
    list_filter = ("table_name", "op")
    list_per_page = 50
    ordering = ("-id",)

    @admin.display(description="内容")
    def summary(self, obj: AccessAudit) -> str:
        text = str(obj.row_data) if obj.row_data is not None else "（表全体の削除）"
        return text if len(text) <= 160 else text[:157] + "…"


@admin.register(LogEntry)
class LogEntryAdmin(ReadOnlyAdmin):
    """Django の操作履歴（誰が管理画面で何をしたか）。"""

    list_display = ("action_time", "user", "content_type", "object_id", "action_flag", "change_message")
    list_filter = ("action_flag",)
    list_per_page = 50
    ordering = ("-action_time",)
