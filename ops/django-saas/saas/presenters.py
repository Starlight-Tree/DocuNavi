"""画面に出す文言と数値を作る（DB には触れない）。使用量の状態は、数字と文言で示し、色だけに頼らない。"""

from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from .models import Organization, Subscription

NEAR_PERCENT = 80  # 上限のこの割合（%）以上を「上限に近い」とする

STATE_LABELS = {"ok": "余裕あり", "near": "上限に近い", "reached": "上限に達した"}


@dataclass(frozen=True)
class Meter:
    label: str
    unit: str
    used: int
    limit: int
    enforced: bool = False  # 上限を実際に強制しているか（メンバー数は API が拒否する。PDF 数・検索回数は強制しない）

    @property
    def state(self) -> str:
        if self.used >= self.limit:
            return "reached"
        if self.used * 100 >= self.limit * NEAR_PERCENT:
            return "near"
        return "ok"

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def percent(self) -> int:
        if self.limit == 0:
            return 100
        return min(self.used * 100 // self.limit, 100)

    @property
    def text(self) -> str:
        if self.state == "ok":
            return f"残り {self.remaining}{self.unit}（余裕あり）"
        if self.state == "near":
            return f"残り {self.remaining}{self.unit}。上限に近いです。"
        if self.enforced:
            return "上限に達しています。新しいメンバーは API から追加できません。"
        return "上限に達しています（この試作では制限しません）。"


def meters_for(org: Organization, member_count: int) -> list[Meter]:
    """人数は実数、PDF 数と今月の検索回数は Usage の架空の値（Usage が無ければ 0）。"""
    usage = getattr(org, "usage", None)
    plan = org.plan
    return [
        Meter("メンバー数", "人", member_count, plan.max_members, enforced=True),
        Meter("PDF 数", "件", usage.pdf_count if usage else 0, plan.max_pdfs),
        Meter("今月の検索回数", "回", usage.searches_this_month if usage else 0, plan.max_searches_per_month),
    ]


def subscription_summary(subscription: Subscription | None, now: datetime | None = None) -> dict:
    """契約状態のラベルと補足。支払いの情報は持たない（架空）。"""
    if subscription is None:
        return {"status": "none", "label": "契約情報なし", "detail": "この組織には契約の情報が登録されていません。"}
    now = now or timezone.now()
    status = subscription.status
    label = Subscription.Status(status).label
    if status == Subscription.Status.TRIALING:
        end = subscription.trial_ends_at
        if end is None:
            detail = "お試し期間の終了日は未設定です。"
        elif end <= now:
            detail = f"お試し期間は {timezone.localtime(end):%Y-%m-%d} に終わりました。"
        else:
            detail = f"あと {(end - now).days + 1} 日（{timezone.localtime(end):%Y-%m-%d} まで）"
    elif status == Subscription.Status.ACTIVE:
        end = subscription.current_period_end
        detail = f"現在の期間は {timezone.localtime(end):%Y-%m-%d} まで" if end else "期間の終了日は未設定です。"
    else:
        canceled = subscription.canceled_at
        when = f"{timezone.localtime(canceled):%Y-%m-%d} に解約されました。" if canceled else "解約されています。"
        detail = f"{when}再開の手順は、この試作にはありません。"
    return {"status": status, "label": label, "detail": detail}
