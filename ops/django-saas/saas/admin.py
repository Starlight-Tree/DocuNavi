from django.contrib import admin

from .models import Membership, Organization, Plan, Subscription, Usage


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


class SubscriptionInline(admin.StackedInline):
    model = Subscription
    extra = 0


class UsageInline(admin.StackedInline):
    model = Usage
    extra = 0


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "monthly_price_yen", "max_members", "max_pdfs", "max_searches_per_month")
    search_fields = ("code", "name")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "plan", "created_at")
    list_filter = ("plan",)
    search_fields = ("name", "slug")
    inlines = [SubscriptionInline, UsageInline, MembershipInline]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("external_user_id", "organization", "role", "created_at")
    list_filter = ("role", "organization")
    search_fields = ("external_user_id",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("organization", "status", "trial_ends_at", "current_period_end", "canceled_at")
    list_filter = ("status",)
    search_fields = ("organization__name", "organization__slug")
