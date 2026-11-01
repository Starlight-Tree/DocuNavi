"""メンバー管理（画面の追加・役割の変更・外す）のテスト。規則は saas/member_admin.py。"""

import uuid

from django.test import Client, TestCase
from django.urls import reverse

from saas.member_admin import MemberError, add_member, change_role, grantable_roles, remove_member
from saas.models import Membership, Organization, Plan, Role


def sub(n: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-4000-8000-{900 + n:012d}")


class MembersTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        plan = Plan.objects.create(code="t5", name="テスト 6 人", max_members=6)
        cls.org = Organization.objects.create(name="テスト組織", slug="members-test", plan=plan)
        cls.other = Organization.objects.create(name="別の組織", slug="members-other", plan=plan)
        cls.owner = Membership.objects.create(organization=cls.org, external_user_id=sub(1), role=Role.OWNER)
        cls.admin = Membership.objects.create(organization=cls.org, external_user_id=sub(2), role=Role.ADMIN)
        cls.member = Membership.objects.create(organization=cls.org, external_user_id=sub(3), role=Role.MEMBER)
        cls.viewer = Membership.objects.create(organization=cls.org, external_user_id=sub(4), role=Role.VIEWER)
        cls.foreign = Membership.objects.create(organization=cls.other, external_user_id=sub(5), role=Role.MEMBER)

    def login(self, who: Membership, client: Client | None = None) -> Client:
        client = client or self.client
        session = client.session
        session["saas_sub"] = str(who.external_user_id)
        session.save()
        return client

    def url(self) -> str:
        return reverse("members_manage", args=[self.org.pk])

    def post(self, **data):
        return self.client.post(self.url(), data)

    def roles(self) -> dict[uuid.UUID, str]:
        return {m.external_user_id: m.role for m in Membership.objects.filter(organization=self.org)}


class RuleTests(MembersTestCase):
    def test_who_can_grant_what(self):
        self.assertEqual(grantable_roles(Role.OWNER), [Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER])
        self.assertEqual(grantable_roles(Role.ADMIN), [Role.MEMBER, Role.VIEWER])
        self.assertEqual(grantable_roles(Role.MEMBER), [])
        self.assertEqual(grantable_roles(Role.VIEWER), [])

    def test_an_owner_adds_any_role_and_an_admin_only_member_or_viewer(self):
        added = add_member(self.org, self.owner, str(sub(10)), "admin")
        self.assertEqual((added.role, added.external_user_id), (Role.ADMIN, sub(10)))
        added = add_member(self.org, self.admin, str(sub(11)), "member")
        self.assertEqual(added.role, Role.MEMBER)
        with self.assertRaises(MemberError) as err:
            add_member(self.org, self.admin, str(sub(12)), "admin")
        self.assertEqual(err.exception.status, 403)

    def test_member_and_viewer_cannot_do_anything(self):
        for actor in (self.member, self.viewer):
            with self.subTest(role=actor.role):
                for call in (
                    lambda a=actor: add_member(self.org, a, str(sub(20)), "viewer"),
                    lambda a=actor: change_role(self.org, a, self.viewer.pk, "member"),
                    lambda a=actor: remove_member(self.org, a, self.viewer.pk),
                ):
                    with self.assertRaises(MemberError) as err:
                        call()
                    self.assertEqual(err.exception.status, 403)

    def test_a_bad_uuid_or_role_is_400_and_a_duplicate_is_409(self):
        for raw, role, status in (
            ("not-a-uuid", "viewer", 400),
            (str(sub(30)), "boss", 400),
            (str(sub(3)), "viewer", 409),
        ):
            with self.subTest(raw=raw, role=role), self.assertRaises(MemberError) as err:
                add_member(self.org, self.owner, raw, role)
            self.assertEqual(err.exception.status, status)

    def test_the_plan_member_limit_is_enforced(self):
        add_member(self.org, self.owner, str(sub(40)), "viewer")  # 5 人目
        add_member(self.org, self.owner, str(sub(42)), "viewer")  # 6 人目（上限）
        with self.assertRaises(MemberError) as err:
            add_member(self.org, self.owner, str(sub(41)), "viewer")
        self.assertEqual((err.exception.status, err.exception.message), (409, "プランの人数の上限に達しています。"))

    def test_an_admin_cannot_touch_admin_owner_or_self(self):
        for target in (self.owner, self.admin):
            with self.subTest(target=target.role):
                for call in (
                    lambda t=target: change_role(self.org, self.admin, t.pk, "viewer"),
                    lambda t=target: remove_member(self.org, self.admin, t.pk),
                ):
                    with self.assertRaises(MemberError) as err:
                        call()
                    self.assertEqual(err.exception.status, 403)

    def test_an_admin_changes_a_member_but_cannot_promote_to_admin(self):
        change_role(self.org, self.admin, self.member.pk, "viewer")
        self.assertEqual(self.roles()[sub(3)], Role.VIEWER)
        with self.assertRaises(MemberError) as err:
            change_role(self.org, self.admin, self.member.pk, "admin")
        self.assertEqual(err.exception.status, 403)

    def test_the_last_owner_can_be_neither_demoted_nor_removed(self):
        for call in (
            lambda: change_role(self.org, self.owner, self.owner.pk, "admin"),
            lambda: remove_member(self.org, self.owner, self.owner.pk),
        ):
            with self.assertRaises(MemberError) as err:
                call()
            self.assertEqual(err.exception.status, 409)
        self.assertEqual(self.roles()[sub(1)], Role.OWNER)

    def test_with_a_second_owner_the_first_can_step_down(self):
        change_role(self.org, self.owner, self.admin.pk, "owner")
        change_role(self.org, self.owner, self.owner.pk, "admin")
        self.assertEqual(self.roles()[sub(1)], Role.ADMIN)

    def test_a_member_of_another_organization_is_404_and_untouched(self):
        for call in (
            lambda: change_role(self.org, self.owner, self.foreign.pk, "viewer"),
            lambda: remove_member(self.org, self.owner, self.foreign.pk),
        ):
            with self.assertRaises(MemberError) as err:
                call()
            self.assertEqual(err.exception.status, 404)
        self.assertTrue(Membership.objects.filter(pk=self.foreign.pk, role=Role.MEMBER).exists())

    def test_the_actor_role_is_checked_again_at_the_time_of_the_operation(self):
        stale = Membership.objects.get(pk=self.admin.pk)
        Membership.objects.filter(pk=self.admin.pk).update(role=Role.VIEWER)  # 画面を開いた後に降格された
        with self.assertRaises(MemberError) as err:
            add_member(self.org, stale, str(sub(50)), "viewer")
        self.assertEqual(err.exception.status, 403)


class PageTests(MembersTestCase):
    def test_add_through_the_page_redirects_and_shows_the_notice(self):
        self.login(self.owner)
        response = self.post(action="add", sub=str(sub(60)), role="member")
        self.assertRedirects(response, self.url() + "?done=added")
        self.assertEqual(self.roles()[sub(60)], Role.MEMBER)
        self.assertContains(self.client.get(self.url() + "?done=added"), "メンバーを追加しました。")

    def test_change_and_remove_through_the_page(self):
        self.login(self.owner)
        self.assertRedirects(
            self.post(action="set_role", membership=self.member.pk, role="viewer"), self.url() + "?done=role"
        )
        self.assertEqual(self.roles()[sub(3)], Role.VIEWER)
        self.assertRedirects(self.post(action="remove", membership=self.viewer.pk), self.url() + "?done=removed")
        self.assertNotIn(sub(4), self.roles())

    def test_errors_keep_the_status_and_a_fixed_message_and_change_nothing(self):
        self.login(self.admin)
        before = self.roles()
        cases = (
            (dict(action="add", sub="x", role="viewer"), 400, "識別子（UUID）の形が正しくありません。"),
            (dict(action="add", sub=str(sub(70)), role="owner"), 403, "その役割は付与できません。"),
            (dict(action="remove", membership=self.owner.pk), 403, "この操作をする権限がありません。"),
            (dict(action="remove", membership="abc"), 400, "対象のメンバーの指定が正しくありません。"),
            (dict(action="remove", membership=self.foreign.pk), 404, "メンバーが見つかりません。"),
            (dict(action="nope"), 400, "操作の指定が正しくありません。"),
        )
        for data, status, message in cases:
            with self.subTest(data=data):
                response = self.post(**data)
                self.assertContains(response, message, status_code=status)
        self.assertEqual(self.roles(), before)

    def test_member_and_viewer_get_403_on_post_too_and_change_nothing(self):
        before = self.roles()
        for who in (self.member, self.viewer):
            with self.subTest(role=who.role):
                client = self.login(who, Client())
                response = client.post(self.url(), {"action": "remove", "membership": self.viewer.pk})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.roles(), before)

    def test_the_page_shows_controls_only_for_rows_the_actor_may_change(self):
        self.login(self.admin)
        html = self.client.get(self.url()).content.decode()
        self.assertEqual(html.count('value="remove"'), 2)  # member と viewer だけ
        self.assertNotIn('value="owner"', html)  # admin は owner を付与できない
        self.login(self.owner)
        html = self.client.get(self.url()).content.decode()
        self.assertEqual(html.count('value="remove"'), 4)
        self.assertIn('value="owner"', html)

    def test_get_is_read_only(self):
        self.login(self.owner)
        before = self.roles()
        self.client.get(self.url() + "?action=remove&membership=%d" % self.member.pk)
        self.assertEqual(self.roles(), before)

    def test_a_post_without_a_csrf_token_is_rejected(self):
        client = self.login(self.owner, Client(enforce_csrf_checks=True))
        response = client.post(self.url(), {"action": "remove", "membership": self.viewer.pk})
        self.assertEqual(response.status_code, 403)
        self.assertIn(sub(4), self.roles())

    def test_the_page_of_an_organization_i_am_not_in_is_404_on_post(self):
        self.login(self.foreign)
        response = self.client.post(self.url(), {"action": "add", "sub": str(sub(80)), "role": "viewer"})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(sub(80), self.roles())
