"""利用者向けの画面（学習用の最小）。HTML を返す。認証は session_auth.current_sub だけに依存する。

- /login/ /logout/                          試作ログイン（SAAS_DEMO_LOGIN が有効なときだけ）
- /dashboard/                               所属組織・役割・契約・上限と使用量・最近の会話
- /orgs/ /orgs/<id>/                        所属組織の一覧と詳細
- /orgs/<id>/members/manage/                メンバー管理の入口（owner / admin だけ。中身は準備中）
入っていない組織は、存在を知らせないために 404（API と同じ）。役割が足りないときは 403。
"""

import functools
import uuid

from django.db.models import Count
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from .models import Membership, Role
from .permissions import allows, rank
from .presenters import meters_for, subscription_summary
from .session_auth import current_sub, demo_login_enabled, end_session, start_session

_BAD_SUB = "識別子（UUID）の形が正しくありません。"


def sub_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        sub = current_sub(request)
        if sub is None:
            return redirect("login")
        request.sub = sub
        return view(request, *args, **kwargs)

    return wrapper


def _short(sub: uuid.UUID) -> str:
    return f"…{str(sub)[-8:]}"


def _memberships(sub: uuid.UUID) -> list[Membership]:
    return list(
        Membership.objects.filter(external_user_id=sub)
        .select_related("organization__plan", "organization__subscription", "organization__usage")
        .order_by("organization_id")
    )


def _member_counts(org_ids: list[int]) -> dict[int, int]:
    rows = Membership.objects.filter(organization_id__in=org_ids).values("organization_id").annotate(n=Count("id"))
    return {row["organization_id"]: row["n"] for row in rows}


def _card(membership: Membership, member_count: int) -> dict:
    org = membership.organization
    return {
        "org": org,
        "role": membership.role,
        "role_label": Role(membership.role).label,
        "plan": org.plan,
        "subscription": subscription_summary(getattr(org, "subscription", None)),
        "meters": meters_for(org, member_count),
        "can_manage": allows(membership.role, Role.ADMIN),
    }


def _cards(sub: uuid.UUID) -> list[dict]:
    memberships = _memberships(sub)
    counts = _member_counts([m.organization_id for m in memberships])
    return [_card(m, counts.get(m.organization_id, 0)) for m in memberships]


def _context(request, **extra) -> dict:
    return {"sub": request.sub, "sub_short": _short(request.sub), **extra}


def index(request):
    return redirect("dashboard")


@require_http_methods(["GET", "POST"])
def login_view(request):
    if not demo_login_enabled():
        raise Http404
    error = ""
    if request.method == "POST":
        raw = (request.POST.get("sub_typed") or request.POST.get("sub") or "").strip()
        try:
            sub = uuid.UUID(raw)
        except ValueError:
            error = _BAD_SUB
        else:
            start_session(request, sub)
            return redirect("dashboard")
    rows = Membership.objects.select_related("organization").order_by("external_user_id", "organization_id")
    people: dict[uuid.UUID, list[str]] = {}
    for m in rows:
        people.setdefault(m.external_user_id, []).append(f"{Role(m.role).label} @ {m.organization.name}")
    choices = [{"sub": sub, "short": _short(sub), "summary": " / ".join(parts)} for sub, parts in people.items()]
    return render(request, "saas/login.html", {"choices": choices, "error": error}, status=400 if error else 200)


@require_POST
def logout_view(request):
    end_session(request)
    return redirect("login")


@sub_required
def dashboard(request):
    return render(request, "saas/dashboard.html", _context(request, cards=_cards(request.sub), nav="dashboard"))


@sub_required
def org_list(request):
    return render(request, "saas/org_list.html", _context(request, cards=_cards(request.sub), nav="orgs"))


def _own_membership(request, org_id: int) -> Membership:
    membership = (
        Membership.objects.select_related("organization__plan", "organization__subscription", "organization__usage")
        .filter(organization_id=org_id, external_user_id=request.sub)
        .first()
    )
    if membership is None:
        raise Http404
    return membership


@sub_required
def org_detail(request, org_id):
    membership = _own_membership(request, org_id)
    members = list(Membership.objects.filter(organization_id=org_id).order_by("id"))
    members.sort(key=lambda m: -rank(m.role))  # 強い役割から（同じ役割は登録順）
    card = _card(membership, len(members))
    rows = [
        {
            "sub_short": _short(m.external_user_id),
            "role": m.role,
            "role_label": Role(m.role).label,
            "is_me": m.external_user_id == request.sub,
        }
        for m in members
    ]
    return render(request, "saas/org_detail.html", _context(request, card=card, members=rows, nav="orgs"))


@sub_required
def members_manage(request, org_id):
    membership = _own_membership(request, org_id)
    if not allows(membership.role, Role.ADMIN):
        return render(
            request, "saas/forbidden.html", _context(request, org=membership.organization, nav="orgs"), status=403
        )
    return render(
        request,
        "saas/members_manage.html",
        _context(request, org=membership.organization, role_label=Role(membership.role).label, nav="orgs"),
    )
