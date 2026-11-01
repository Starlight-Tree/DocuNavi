"""メンバー管理の規則（画面の追加・役割の変更・外す）。HTTP には依存しない。失敗は `MemberError`（ステータスと固定の文）で返す。

誰が何をできるか（サーバー側で毎回確かめる。画面でボタンを隠すだけにしない）:

| 操作する人 | 追加・付与できる役割 | 役割の変更・外す対象 |
|---|---|---|
| owner | owner / admin / member / viewer | 全員（自分を含む）。ただし組織の owner が 0 人になる操作は不可 |
| admin | member / viewer | member / viewer だけ（admin・owner には触れない。自分にも触れない） |
| member / viewer | なし | なし |

- 追加はプランの人数の上限（`Plan.max_members`）を超えられない（API の追加と同じ。409）。
- 同じ組織に同じ `sub`（JWT の識別子）は 2 回入らない（409）。
- 別の組織のメンバーの ID を指定しても 404（存在を知らせない）。
- 組織の全メンバーの行を `select_for_update` で押さえてから数える（同時の操作で owner が 0 人になるのを防ぐ）。
API の追加（`POST /api/orgs/<id>/members`）は owner だけのまま。画面は admin にも member / viewer の追加を許す（試作の決定）。
"""

import uuid
from dataclasses import dataclass

from django.db import IntegrityError, transaction

from .models import Membership, Organization, Role
from .permissions import rank

_ADMIN_GRANTS = (Role.MEMBER, Role.VIEWER)


@dataclass
class MemberError(Exception):
    status: int
    message: str

    def __str__(self) -> str:
        return self.message


def grantable_roles(actor_role: str) -> list[Role]:
    """この役割の人が、追加・変更で付けられる役割（強い順）。"""
    if actor_role == Role.OWNER:
        return [Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER]
    if actor_role == Role.ADMIN:
        return list(_ADMIN_GRANTS)
    return []


def can_modify(actor: Membership, target: Membership) -> bool:
    """役割の変更・外すことができるか。owner は全員、admin は自分より弱い役割だけ。"""
    if actor.role == Role.OWNER:
        return True
    return actor.role == Role.ADMIN and rank(target.role) < rank(Role.ADMIN)


def _lock(org: Organization) -> list[Membership]:
    return list(Membership.objects.select_for_update().filter(organization=org).order_by("id"))


def _actor(members: list[Membership], actor: Membership) -> Membership:
    # 操作の直前に、いまの役割を確かめ直す（画面を開いた後に降格された人を通さない）
    current = next((m for m in members if m.pk == actor.pk), None)
    if current is None or not grantable_roles(current.role):
        raise MemberError(403, "この操作をする権限がありません。")
    return current


def _target(members: list[Membership], membership_id: int) -> Membership:
    target = next((m for m in members if m.pk == membership_id), None)
    if target is None:
        raise MemberError(404, "メンバーが見つかりません。")
    return target


def _role(raw: str, actor: Membership) -> Role:
    if raw not in Role.values:
        raise MemberError(400, "役割の指定が正しくありません。")
    role = Role(raw)
    if role not in grantable_roles(actor.role):
        raise MemberError(403, "その役割は付与できません。")
    return role


def _would_leave_no_owner(members: list[Membership], target: Membership, new_role: str | None) -> bool:
    if target.role != Role.OWNER or new_role == Role.OWNER:
        return False
    return sum(1 for m in members if m.role == Role.OWNER) <= 1


_LAST_OWNER = "オーナーが 1 人もいなくなるため、できません。先に別のオーナーを決めてください。"


@transaction.atomic
def add_member(org: Organization, actor: Membership, raw_sub: str, raw_role: str) -> Membership:
    members = _lock(org)
    actor = _actor(members, actor)
    try:
        sub = uuid.UUID(raw_sub.strip())
    except ValueError:
        raise MemberError(400, "識別子（UUID）の形が正しくありません。") from None
    role = _role(raw_role, actor)
    if len(members) >= org.plan.max_members:
        raise MemberError(409, "プランの人数の上限に達しています。")
    if any(m.external_user_id == sub for m in members):
        raise MemberError(409, "すでにメンバーです。")
    try:
        with transaction.atomic():
            return Membership.objects.create(organization=org, external_user_id=sub, role=role)
    except IntegrityError:
        raise MemberError(409, "すでにメンバーです。") from None


@transaction.atomic
def change_role(org: Organization, actor: Membership, membership_id: int, raw_role: str) -> Membership:
    members = _lock(org)
    actor = _actor(members, actor)
    target = _target(members, membership_id)
    if not can_modify(actor, target):
        raise MemberError(403, "この操作をする権限がありません。")
    role = _role(raw_role, actor)
    if _would_leave_no_owner(members, target, role):
        raise MemberError(409, _LAST_OWNER)
    target.role = role
    target.save(update_fields=["role"])
    return target


@transaction.atomic
def remove_member(org: Organization, actor: Membership, membership_id: int) -> None:
    members = _lock(org)
    actor = _actor(members, actor)
    target = _target(members, membership_id)
    if not can_modify(actor, target):
        raise MemberError(403, "この操作をする権限がありません。")
    if _would_leave_no_owner(members, target, None):
        raise MemberError(409, _LAST_OWNER)
    target.delete()
