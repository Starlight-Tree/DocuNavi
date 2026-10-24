import os
import uuid

from django.test import TestCase

from saas.models import Membership, Organization, Plan, Role

from .helpers import Signer, install_keys


class ApiTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.signer = Signer()
        cls.keyfile = install_keys(cls.signer)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.keyfile)
        super().tearDownClass()

    def setUp(self):
        self.plan = Plan.objects.create(code="free", name="無料", max_members=3)
        self.org = Organization.objects.create(name="A社", slug="a", plan=self.plan)
        self.other = Organization.objects.create(name="B社", slug="b", plan=self.plan)
        self.owner, self.member, self.viewer, self.stranger = (str(uuid.uuid4()) for _ in range(4))
        Membership.objects.create(organization=self.org, external_user_id=self.owner, role=Role.OWNER)
        Membership.objects.create(organization=self.org, external_user_id=self.member, role=Role.MEMBER)
        Membership.objects.create(organization=self.org, external_user_id=self.viewer, role=Role.VIEWER)
        Membership.objects.create(organization=self.other, external_user_id=self.stranger, role=Role.OWNER)

    def get(self, path, sub=None, token=None):
        headers = {}
        if sub or token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token or self.signer.token(sub=sub)}"
        return self.client.get(path, **headers)

    def post(self, path, body, sub):
        return self.client.post(
            path, data=body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.signer.token(sub=sub)}"
        )

    def test_no_token_or_a_bad_token_is_401_with_a_fixed_message(self):
        for response in (self.get("/api/me/orgs/"), self.get("/api/me/orgs/", token="x.y.z")):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"detail": "ログインが必要です。ログインし直してください。"})

    def test_me_orgs_lists_only_my_orgs(self):
        body = self.get("/api/me/orgs/", sub=self.owner).json()
        self.assertEqual([o["id"] for o in body["orgs"]], [self.org.id])
        self.assertEqual(body["orgs"][0]["role"], "owner")

    def test_every_role_can_read_the_org_and_members(self):
        for sub in (self.owner, self.member, self.viewer):
            self.assertEqual(self.get(f"/api/orgs/{self.org.id}/", sub=sub).status_code, 200)
            self.assertEqual(self.get(f"/api/orgs/{self.org.id}/members/", sub=sub).status_code, 200)

    def test_a_non_member_gets_404_not_403(self):
        self.assertEqual(self.get(f"/api/orgs/{self.org.id}/", sub=self.stranger).status_code, 404)
        self.assertEqual(self.get(f"/api/orgs/{self.org.id}/members/", sub=self.stranger).status_code, 404)

    def test_only_the_owner_can_add_members(self):
        new = str(uuid.uuid4())
        self.plan.max_members = 10  # 上限（3 人）に当たらないようにする
        self.plan.save()
        for sub in (self.member, self.viewer):
            self.assertEqual(self.post(f"/api/orgs/{self.org.id}/members/", {"sub": new}, sub).status_code, 403)
        self.assertEqual(
            self.post(f"/api/orgs/{self.org.id}/members/", {"sub": new, "role": "member"}, self.owner).status_code, 201
        )
        self.assertTrue(Membership.objects.filter(organization=self.org, external_user_id=new, role="member").exists())

    def test_the_plan_limit_and_duplicates_are_409(self):
        path = f"/api/orgs/{self.org.id}/members/"
        self.assertEqual(
            self.post(path, {"sub": str(uuid.uuid4())}, self.owner).status_code, 409
        )  # すでに 3 人（上限 3）
        self.plan.max_members = 10
        self.plan.save()
        self.assertEqual(self.post(path, {"sub": self.member}, self.owner).status_code, 409)  # 重複

    def test_bad_bodies_are_400(self):
        path = f"/api/orgs/{self.org.id}/members/"
        self.assertEqual(self.post(path, {"sub": "nope"}, self.owner).status_code, 400)
        self.assertEqual(self.post(path, {"sub": str(uuid.uuid4()), "role": "root"}, self.owner).status_code, 400)

    def test_the_owner_of_another_org_cannot_add_here(self):
        self.assertEqual(
            self.post(f"/api/orgs/{self.org.id}/members/", {"sub": str(uuid.uuid4())}, self.stranger).status_code, 404
        )

    def test_other_methods_are_405(self):
        self.assertEqual(
            self.client.delete(f"/api/orgs/{self.org.id}/", HTTP_AUTHORIZATION="Bearer x").status_code, 405
        )
