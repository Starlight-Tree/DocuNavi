"""組織・メンバー・プラン。利用者の ID は Supabase の JWT の `sub`（外部 ID）で、Django のユーザー表は使わない。"""

from django.db import models


class Plan(models.Model):
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    monthly_price_yen = models.PositiveIntegerField(default=0)
    max_members = models.PositiveIntegerField(default=3)
    # 架空の値。表示するだけで、検索や取り込みの側では強制しない（試作）。
    max_pdfs = models.PositiveIntegerField(default=10)
    max_searches_per_month = models.PositiveIntegerField(default=100)

    def __str__(self) -> str:
        return self.name


class Organization(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="organizations")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


class Role(models.TextChoices):
    OWNER = "owner", "オーナー"
    ADMIN = "admin", "管理者"
    MEMBER = "member", "メンバー"
    VIEWER = "viewer", "閲覧者"


class Membership(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    external_user_id = models.UUIDField(help_text="Supabase の JWT の sub")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.VIEWER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "external_user_id"], name="uniq_member_per_org"),
        ]
        indexes = [models.Index(fields=["external_user_id"], name="membership_sub_idx")]

    def __str__(self) -> str:
        return f"{self.external_user_id} @ {self.organization_id} ({self.role})"


class Subscription(models.Model):
    """組織ごとの契約の状態（架空）。プランは Organization.plan が持つので、ここには置かない。支払いの情報は持たない。"""

    class Status(models.TextChoices):
        TRIALING = "trialing", "お試し中"
        ACTIVE = "active", "契約中"
        CANCELED = "canceled", "解約済み"

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="subscription")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.TRIALING)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.organization} ({self.status})"


class Usage(models.Model):
    """組織ごとの使用量（架空の値）。ダッシュボードに表示するだけで、検索や取り込みの側からは更新しない（試作）。"""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="usage")
    pdf_count = models.PositiveIntegerField(default=0)
    searches_this_month = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.organization} (PDF {self.pdf_count}, 検索 {self.searches_this_month})"
