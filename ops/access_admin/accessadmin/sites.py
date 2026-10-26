from django.contrib.admin import AdminSite
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django_otp import devices_for_user


class AccessAdminSite(AdminSite):
    """管理サイト。見た目は Django 標準の枠のまま（独自のデザインは作らない。承認済み）。

    入るには、パスワードのあとに TOTP の確認が済んでいること（`is_verified()`）が必須。確認が済んでいない運用者は、
    管理画面のどの URL を開いても、コードの登録（初回）か入力（2 回目以降）へ送られる。
    """

    site_header = "ドキュナビ 資料グループの管理"
    site_title = "ドキュナビ 管理"
    index_title = "管理"

    def has_permission(self, request: HttpRequest) -> bool:
        return super().has_permission(request) and request.user.is_verified()

    def login(self, request: HttpRequest, extra_context=None) -> HttpResponse:
        user = request.user
        if (
            request.method == "GET"
            and user.is_authenticated
            and user.is_active
            and user.is_staff
            and not user.is_verified()
        ):
            registered = any(devices_for_user(user, confirmed=True))
            return redirect(reverse("otp:verify" if registered else "otp:setup"))
        return super().login(request, extra_context)
