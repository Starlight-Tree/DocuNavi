"""組織の API（学習用の最小）。認証は Bearer の JWT だけ。固定メッセージで返し、例外の文字列は出さない。

- GET  /api/me/orgs/                    自分が入っている組織の一覧
- GET  /api/orgs/<id>/                  組織の概要（閲覧者以上）
- GET  /api/orgs/<id>/members/          メンバー一覧（閲覧者以上）
- POST /api/orgs/<id>/members/          メンバーの追加（オーナーだけ。プランの人数の上限あり）
入っていない組織は、存在を知らせないために 404 にする。
"""

import functools
import json
import uuid

from django.db import IntegrityError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .jwt_auth import InvalidToken, verify_token
from .models import Membership, Role
from .permissions import allows

_UNAUTHORIZED = "ログインが必要です。ログインし直してください。"
_FORBIDDEN = "この操作をする権限がありません。"
_NOT_FOUND = "見つかりません。"
_BAD_REQUEST = "リクエストの形が正しくありません。"


def _error(status: int, message: str) -> JsonResponse:
    return JsonResponse({"detail": message}, status=status)


def authenticated(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return _error(401, _UNAUTHORIZED)
        try:
            request.sub = verify_token(token.strip())
        except InvalidToken:
            return _error(401, _UNAUTHORIZED)
        return view(request, *args, **kwargs)

    return wrapper


def org_role(minimum: str):
    """`sub` がその組織に入っていて、役割が minimum 以上であることを求める。request.membership に入れる。"""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, org_id, *args, **kwargs):
            membership = (
                Membership.objects.select_related("organization__plan")
                .filter(organization_id=org_id, external_user_id=request.sub)
                .first()
            )
            if membership is None:
                return _error(404, _NOT_FOUND)
            if not allows(membership.role, minimum):
                return _error(403, _FORBIDDEN)
            request.membership = membership
            return view(request, org_id, *args, **kwargs)

        return wrapper

    return decorator


@csrf_exempt
@require_http_methods(["GET"])
@authenticated
def my_orgs(request):
    rows = (
        Membership.objects.filter(external_user_id=request.sub)
        .select_related("organization")
        .order_by("organization_id")
    )
    return JsonResponse(
        {"orgs": [{"id": m.organization_id, "name": m.organization.name, "role": m.role} for m in rows]}
    )


@csrf_exempt
@require_http_methods(["GET"])
@authenticated
@org_role(Role.VIEWER)
def org_detail(request, org_id):
    org = request.membership.organization
    return JsonResponse(
        {
            "id": org.id,
            "name": org.name,
            "plan": {"code": org.plan.code, "max_members": org.plan.max_members},
            "member_count": org.memberships.count(),
            "your_role": request.membership.role,
        }
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
@authenticated
def members(request, org_id):
    if request.method == "GET":
        return _list_members(request, org_id)
    return _add_member(request, org_id)


@org_role(Role.VIEWER)
def _list_members(request, org_id):
    rows = Membership.objects.filter(organization_id=org_id).order_by("id")
    return JsonResponse({"members": [{"sub": str(m.external_user_id), "role": m.role} for m in rows]})


@org_role(Role.OWNER)
def _add_member(request, org_id):
    try:
        body = json.loads(request.body or b"{}")
        sub = str(uuid.UUID(body["sub"]))
        role = body.get("role", Role.VIEWER)
        if role not in Role.values:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return _error(400, _BAD_REQUEST)
    org = request.membership.organization
    if org.memberships.count() >= org.plan.max_members:
        return _error(409, "プランの人数の上限に達しています。")
    try:
        Membership.objects.create(organization=org, external_user_id=sub, role=role)
    except IntegrityError:
        return _error(409, "すでにメンバーです。")
    return JsonResponse({"sub": sub, "role": role}, status=201)
