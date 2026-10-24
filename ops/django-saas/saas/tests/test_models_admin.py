import uuid

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from saas.models import Membership, Organization, Plan, Role


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


class AdminTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("operator", password="dev-only-pass")
        self.client.force_login(user)
        plan = Plan.objects.create(code="free", name="無料")
        self.org = Organization.objects.create(name="A社", slug="a", plan=plan)

    def test_changelists_and_the_org_form_with_the_inline_load(self):
        for name in ("plan_changelist", "organization_changelist", "membership_changelist"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"admin:saas_{name}")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("admin:saas_organization_change", args=[self.org.pk])).status_code, 200
        )

    def test_anonymous_is_redirected_to_the_login(self):
        self.client.logout()
        response = self.client.get(reverse("admin:saas_organization_changelist"))
        self.assertEqual(response.status_code, 302)
