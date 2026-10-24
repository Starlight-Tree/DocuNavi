"""組織・メンバー・プラン。利用者の ID は Supabase の JWT の `sub`（外部 ID）で、Django のユーザー表は使わない。"""

from django.db import models


class Plan(models.Model):
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    monthly_price_yen = models.PositiveIntegerField(default=0)
    max_members = models.PositiveIntegerField(default=3)

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
