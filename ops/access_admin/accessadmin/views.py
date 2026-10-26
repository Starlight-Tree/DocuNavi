"""利用者ごとのロールの割り当て・解除の画面（4a で唯一の書き込み）。

流れ: 選ぶ → 確認（変更前後の見えるグループを見せる）→ 保存。
- 見えるグループの前後は、保存の直前に **サーバー側で DB から計算し直す**（画面から送られた値は、選ばれたロールの名前と
  確認のチェックだけを使う）。
- 見えるグループが減る変更は、確認のチェックボックスが無ければ保存しない。
- 確認の画面を開いてから保存するまでに、その利用者の割り当てが変わっていたら、保存せずにやり直させる（送られた「元の割り当て」と比べる）。
- 書き込みは Django のスーパーユーザーだけ。DB への書き込みは user_roles の INSERT / DELETE のみ（DB ロールの権限もそこまで）。
- 操作は Django の操作履歴（LogEntry。利用者は ID だけで、メールは載せない）と、DB のトリガー（access_audit）に残る。
"""

from uuid import UUID

from django.contrib import messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from . import db, services
from .models import AdminUser, Role

TEMPLATE = "accessadmin/assign_roles.html"


def _fmt(groups) -> str:
    return ", ".join(sorted(groups)) or "なし"


def _context(request: HttpRequest, model_admin, user: AdminUser, **extra) -> dict:
    return {
        **model_admin.admin_site.each_context(request),
        "opts": AdminUser._meta,
        "title": "ロールの割り当て",
        "target": user,
        "list_url": reverse("admin:accessadmin_adminuser_changelist"),
        **extra,
    }


def _role_rows(current: frozenset[str], checked: frozenset[str], matrix) -> list[dict]:
    return [
        {
            "name": name,
            "label": label,
            "groups": _fmt(matrix.get(name, ())),
            "checked": name in checked,
            "current": name in current,
        }
        for name, label in Role.objects.order_by("name").values_list("name", "label")
    ]


def _parse(request: HttpRequest) -> tuple[frozenset[str], frozenset[str]]:
    """送られた (選ばれたロール, 画面を開いた時点の元の割り当て)。default を同時に付ける選択は、選ばれたロールに足す。"""
    desired = {r for r in request.POST.getlist("roles") if r}
    if request.POST.get("with_default"):
        desired.add(services.DEFAULT_ROLE)
    base = {r for r in request.POST.getlist("base") if r}
    return frozenset(desired), frozenset(base)


def _log(request: HttpRequest, user: AdminUser, change: services.RoleChange) -> None:
    message = (
        f"ロールを変更: 追加 [{', '.join(sorted(change.add))}] / 解除 [{', '.join(sorted(change.remove))}]。"
        f"見えるグループ: [{', '.join(sorted(change.before))}] → [{', '.join(sorted(change.after))}]"
    )
    LogEntry.objects.create(
        user_id=request.user.pk,
        content_type=ContentType.objects.get_for_model(AdminUser),
        object_id=str(user.pk),
        object_repr=str(user.pk),  # メールは操作履歴に残さない
        action_flag=CHANGE,
        change_message=message,
    )


@never_cache
@require_http_methods(["GET", "POST"])
def assign_roles(request: HttpRequest, model_admin, user_id: UUID) -> HttpResponse:
    if not request.user.is_superuser:
        raise PermissionDenied
    try:
        user = AdminUser.objects.get(pk=user_id)
    except AdminUser.DoesNotExist:
        raise Http404("利用者が見つかりません") from None
    uid = str(user.pk)
    matrix = db.access_matrix()

    if request.method == "GET":
        current = db.assigned_roles(uid)
        return render(
            request,
            TEMPLATE,
            _context(
                request,
                model_admin,
                user,
                step="select",
                roles=_role_rows(current, current, matrix),
                current=current,
                current_groups=_fmt(services.visible_groups(current, matrix)),
                base=sorted(current),
            ),
        )

    desired, base = _parse(request)
    known = db.role_names()
    if services.unknown_roles(desired, known):
        raise Http404("ロールが見つかりません")  # 画面に無いロール名。何も書かない（名前は返さない）

    action = request.POST.get("action")
    if action == "apply":
        with transaction.atomic():
            db.lock_user(uid)
            current = db.assigned_roles(uid)
            if current != base:
                messages.error(
                    request,
                    "確認の画面を開いたあとに、この利用者の割り当てが変わりました。もう一度、内容を確かめてください。",
                )
                return redirect(request.path)
            change = services.plan_change(current, desired, matrix)
            if not change.changed:
                messages.info(request, "変更はありません。")
                return redirect(request.path)
            if change.shrinks and not request.POST.get("confirm_shrink"):
                return _render_preview(request, model_admin, user, change, matrix, error=True)
            db.apply_role_change(uid, change.add, change.remove)
            _log(request, user, change)
        messages.success(
            request,
            f"ロールを変更しました。見えるグループ: {_fmt(change.before)} → {_fmt(change.after)}",
        )
        return redirect(reverse("admin:accessadmin_adminuser_changelist"))

    # 確認（preview）。DB は変えない
    current = db.assigned_roles(uid)
    change = services.plan_change(current, desired, matrix)
    if not change.changed:
        messages.info(request, "変更はありません。")
        return redirect(request.path)
    return _render_preview(request, model_admin, user, change, matrix, error=False)


def _render_preview(request, model_admin, user, change: services.RoleChange, matrix, *, error: bool) -> HttpResponse:
    return render(
        request,
        TEMPLATE,
        _context(
            request,
            model_admin,
            user,
            step="confirm",
            change=change,
            before=_fmt(change.before),
            after=_fmt(change.after),
            lost=_fmt(change.lost),
            gained=_fmt(change.gained),
            add=sorted(change.add),
            remove=sorted(change.remove),
            desired=sorted(change.desired),
            base=sorted(change.current),
            needs_confirm=change.shrinks,
            error=error,
        ),
        status=400 if error else 200,
    )
