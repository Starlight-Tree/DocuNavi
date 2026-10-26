import uuid
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from saas.models import Membership, Organization, Plan, Role, Subscription, Usage
from saas.permissions import allows


class ModelTests(TestCase):
    def setUp(self):
        self.plan = Plan.objects.create(code="free", name="無料", max_members=2)
        self.org = Organization.objects.create(name="A社", slug="a", plan=self.plan)

    def test_a_sub_joins_an_org_once(self):
        sub = uuid.uuid4()
        Membership.objects.create(organization=self.org, external_user_id=sub, role=Role.OWNER)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(organization=self.org, external_user_id=sub)

    def test_a_plan_in_use_cannot_be_deleted(self):
        with self.assertRaises(ProtectedError):
            self.plan.delete()

    def test_deleting_an_org_deletes_its_memberships(self):
        Membership.objects.create(organization=self.org, external_user_id=uuid.uuid4())
        self.org.delete()
        self.assertEqual(Membership.objects.count(), 0)

    def test_an_org_has_one_subscription(self):
        Subscription.objects.create(organization=self.org)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Subscription.objects.create(organization=self.org)

    def test_a_new_subscription_starts_as_trialing(self):
        self.assertEqual(Subscription.objects.create(organization=self.org).status, Subscription.Status.TRIALING)

    def test_deleting_an_org_deletes_its_subscription(self):
        Subscription.objects.create(organization=self.org)
        self.org.delete()
        self.assertEqual(Subscription.objects.count(), 0)

    def test_the_plan_limits_have_defaults(self):
        self.assertEqual((self.plan.max_pdfs, self.plan.max_searches_per_month), (10, 100))


class RoleOrderTests(TestCase):
    def test_roles_are_ordered_viewer_member_admin_owner(self):
        order = [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]
        for i, role in enumerate(order):
            for j, minimum in enumerate(order):
                with self.subTest(role=role, minimum=minimum):
                    self.assertEqual(allows(role, minimum), i >= j)

    def test_an_unknown_role_allows_nothing(self):
        self.assertFalse(allows("nobody", Role.VIEWER))


class SeedDemoTests(TestCase):
    def test_seed_demo_is_repeatable(self):
        for _ in range(2):
            call_command("seed_demo", stdout=StringIO())
        self.assertEqual((Plan.objects.count(), Organization.objects.count(), Subscription.objects.count()), (2, 3, 3))
        self.assertEqual(Membership.objects.count(), 8)
        self.assertEqual(Membership.objects.filter(role=Role.ADMIN).count(), 1)
        self.assertEqual(Usage.objects.count(), 3)
        # 複数組織に入っている利用者が 1 人いる
        self.assertEqual(
            Membership.objects.filter(external_user_id=uuid.UUID("00000000-0000-4000-8000-000000000002")).count(), 2
        )

    def test_seed_demo_covers_the_three_usage_states(self):
        from saas.presenters import meters_for

        call_command("seed_demo", stdout=StringIO())
        states = set()
        for org in Organization.objects.select_related("plan", "usage"):
            states.update(m.state for m in meters_for(org, org.memberships.count()))
        self.assertEqual(states, {"ok", "near", "reached"})


class AdminTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("operator", password="dev-only-pass")
        self.client.force_login(user)
        plan = Plan.objects.create(code="free", name="無料")
        self.org = Organization.objects.create(name="A社", slug="a", plan=plan)

    def test_changelists_and_the_org_form_with_the_inline_load(self):
        Usage.objects.create(organization=self.org, pdf_count=1)
        for name in ("plan_changelist", "organization_changelist", "membership_changelist", "subscription_changelist"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"admin:saas_{name}")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("admin:saas_organization_change", args=[self.org.pk])).status_code, 200
        )

    def test_the_subscription_pages_load(self):
        sub = Subscription.objects.create(organization=self.org)
        self.assertEqual(self.client.get(reverse("admin:saas_subscription_add")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:saas_subscription_change", args=[sub.pk])).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("admin:saas_organization_change", args=[self.org.pk])).status_code, 200
        )

    def test_anonymous_is_redirected_to_the_login(self):
        self.client.logout()
        response = self.client.get(reverse("admin:saas_organization_changelist"))
        self.assertEqual(response.status_code, 302)
