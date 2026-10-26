"""TOTP（認証アプリのワンタイムコード）の登録と確認。パスワードのあとの 2 つ目の要素で、管理画面に入るのに必須。

- 登録（setup）: 確認済みの機器が 1 つも無いログイン済みの運用者だけ。QR コード（サーバー側で SVG にする。外部のサービスにシークレットを
  送らない）を見せ、認証アプリが出すコードで確認できたら有効にする。シークレットは文字では出さない・ログに書かない・操作履歴に載せない
  （標準の TOTP 機器の管理画面は登録しない）。
- 確認（verify）: 確認済みの機器があるときの、ログインのたびのコード入力。誤ったコード・使用済みのコードは拒否する（django-otp が、
  失敗の連続への待ち時間と、同じコードの再利用の拒否を持つ）。
- 機器の初期化は管理コマンド `totp_reset`（運用者が実行する）。画面からは、すでに登録がある利用者の機器を作り直せない。
"""

import segno
from django.contrib import admin, messages
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods, require_POST
from django_otp import devices_for_user
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice

SETUP_TEMPLATE = "accessadmin/otp_setup.html"
VERIFY_TEMPLATE = "accessadmin/otp_verify.html"
DEVICE_NAME = "default"

ERROR_BAD_CODE = "コードが正しくありません。認証アプリに表示されている最新のコードを入力してください。"


def _allowed(user) -> bool:
    return user.is_authenticated and user.is_active and user.is_staff


def _next_url(request: HttpRequest) -> str:
    """ログイン後に戻る先。同じサイトの URL だけ（外部のサイトへは飛ばさない）。"""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return reverse("admin:index")


def _to_login(request: HttpRequest) -> HttpResponse:
    return redirect(f"{reverse('admin:login')}?next={reverse('admin:index')}")


def _context(request: HttpRequest, title: str, **extra) -> dict:
    """管理サイトの枠（サイト名など）と、ページ固有の値。"""
    return {
        **admin.site.each_context(request),
        "title": title,
        "next": _next_url(request),
        "username": request.user.get_username(),
        **extra,
    }


def _finish(request: HttpRequest, device: TOTPDevice) -> HttpResponse:
    otp_login(request, device)
    request.session.cycle_key()  # パスワードだけの段階の ID を、そのまま持ち越さない
    return redirect(_next_url(request))


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def setup(request: HttpRequest) -> HttpResponse:
    user = request.user
    if not _allowed(user):
        return _to_login(request)
    if user.is_verified():
        return redirect(reverse("admin:index"))
    if any(devices_for_user(user, confirmed=True)):
        return redirect(f"{reverse('otp:verify')}?next={_next_url(request)}")  # 登録済み。確認のほうへ

    device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(user=user, name=DEVICE_NAME, confirmed=False)

    if request.method == "POST":
        token = request.POST.get("token", "").strip()
        if token.isdigit() and device.verify_token(token):
            device.confirmed = True
            device.save(update_fields=["confirmed"])
            messages.success(request, "認証アプリを登録しました。")
            return _finish(request, device)
        messages.error(request, ERROR_BAD_CODE)

    svg = segno.make(device.config_url, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
    response = render(request, SETUP_TEMPLATE, _context(request, "認証アプリの登録", qr=mark_safe(svg)))  # noqa: S308 - segno が生成した SVG
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@csrf_protect
@require_http_methods(["GET", "POST"])
def verify(request: HttpRequest) -> HttpResponse:
    user = request.user
    if not _allowed(user):
        return _to_login(request)
    if user.is_verified():
        return redirect(reverse("admin:index"))
    devices = list(devices_for_user(user, confirmed=True))
    if not devices:
        return redirect(f"{reverse('otp:setup')}?next={_next_url(request)}")

    if request.method == "POST":
        token = request.POST.get("token", "").strip()
        if token.isdigit():
            for device in devices:
                if device.verify_token(token):
                    return _finish(request, device)
        messages.error(request, ERROR_BAD_CODE)
    return render(request, VERIFY_TEMPLATE, _context(request, "ワンタイムコードの入力"))


@csrf_protect
@require_POST
def sign_out(request: HttpRequest) -> HttpResponse:
    """コード入力の途中でやめる（管理画面のログアウトは、確認前は使えないので、ここに置く）。"""
    logout(request)
    return redirect(reverse("admin:login"))
