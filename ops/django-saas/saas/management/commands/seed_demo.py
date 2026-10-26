"""架空の組織・メンバー・プラン・契約を入れる（学習用）。実データは使わない。何度実行しても同じ結果になる。

ローカルの試作 DB にだけ使う。本番の DB URL を向けない（この試作は本番に接続しない）。
"""

import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from saas.models import Membership, Organization, Plan, Role, Subscription

_PLANS = [
    {
        "code": "free",
        "name": "フリー",
        "monthly_price_yen": 0,
        "max_members": 3,
        "max_pdfs": 5,
        "max_searches_per_month": 100,
    },
    {
        "code": "pro",
        "name": "プロ",
        "monthly_price_yen": 4980,
        "max_members": 20,
        "max_pdfs": 100,
        "max_searches_per_month": 5000,
    },
]

# (slug, 名前, プラン, 契約の状態, [(固定の UUID の末尾, 役割), ...])
_ORGS = [
    (
        "demo-cafe",
        "デモ カフェ",
        "free",
        Subscription.Status.TRIALING,
        [(1, Role.OWNER), (2, Role.MEMBER), (3, Role.VIEWER)],
    ),
    (
        "demo-dorm",
        "デモ 寮運営",
        "pro",
        Subscription.Status.ACTIVE,
        [(4, Role.OWNER), (5, Role.ADMIN), (6, Role.MEMBER)],
    ),
    ("demo-old", "デモ 旧組織", "pro", Subscription.Status.CANCELED, [(7, Role.OWNER)]),
]


def _sub(n: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-4000-8000-{n:012d}")


class Command(BaseCommand):
    help = "架空の組織・メンバー・プラン・契約を入れる（ローカルの試作 DB 専用）"

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()
        plans = {}
        for row in _PLANS:
            plans[row["code"]], _ = Plan.objects.update_or_create(code=row["code"], defaults=row)
        for slug, name, plan_code, status, members in _ORGS:
            org, _ = Organization.objects.update_or_create(slug=slug, defaults={"name": name, "plan": plans[plan_code]})
            Subscription.objects.update_or_create(
                organization=org,
                defaults={
                    "status": status,
                    "trial_ends_at": now + timedelta(days=14) if status == Subscription.Status.TRIALING else None,
                    "current_period_end": now + timedelta(days=30) if status == Subscription.Status.ACTIVE else None,
                    "canceled_at": now - timedelta(days=3) if status == Subscription.Status.CANCELED else None,
                },
            )
            for n, role in members:
                Membership.objects.update_or_create(organization=org, external_user_id=_sub(n), defaults={"role": role})
        self.stdout.write(
            f"プラン {Plan.objects.count()} 件、組織 {Organization.objects.count()} 件、契約 {Subscription.objects.count()} 件"
        )
