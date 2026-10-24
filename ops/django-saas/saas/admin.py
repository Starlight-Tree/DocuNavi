from django.contrib import admin

from .models import Membership, Organization, Plan


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "monthly_price_yen", "max_members")
    search_fields = ("code", "name")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "plan", "created_at")
    list_filter = ("plan",)
    search_fields = ("name", "slug")
    inlines = [MembershipInline]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("external_user_id", "organization", "role", "created_at")
    list_filter = ("role", "organization")
    search_fields = ("external_user_id",)
