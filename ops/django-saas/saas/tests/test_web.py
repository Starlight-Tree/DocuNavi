import uuid
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from saas import session_auth
from saas.models import Membership, Organization, Plan, Role, Subscription, Usage
from saas.presenters import Meter, subscription_summary


def sub(n: int) -> uuid.UUID:
    """seed_demo と同じ固定の UUID。1: カフェの owner、2: カフェの member + 寮の viewer、3: カフェの viewer、
    4: 寮の owner、5: 寮の admin、6: 寮の member、7: 旧組織の owner。"""
    return uuid.UUID(f"00000000-0000-4000-8000-{n:012d}")


class WebTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", stdout=StringIO())
        cls.cafe = Organization.objects.get(slug="demo-cafe")
        cls.dorm = Organization.objects.get(slug="demo-dorm")
        cls.old = Organization.objects.get(slug="demo-old")

    def login(self, who: uuid.UUID) -> None:
        session = self.client.session
        session["saas_sub"] = str(who)
        session.save()


class MeterTests(SimpleTestCase):
    def test_three_states(self):
        self.assertEqual(Meter("x", "件", 10, 100).state, "ok")
        self.assertEqual(Meter("x", "件", 79, 100).state, "ok")
        self.assertEqual(Meter("x", "件", 80, 100).state, "near")
        self.assertEqual(Meter("x", "件", 99, 100).state, "near")
        self.assertEqual(Meter("x", "件", 100, 100).state, "reached")
        self.assertEqual(Meter("x", "件", 120, 100).state, "reached")

    def test_remaining_and_percent_never_go_out_of_range(self):
        over = Meter("x", "件", 120, 100)
        self.assertEqual((over.remaining, over.percent), (0, 100))
        self.assertEqual(Meter("x", "件", 25, 100).percent, 25)
        self.assertEqual((Meter("x", "件", 0, 0).percent, Meter("x", "件", 0, 0).state), (100, "reached"))

    def test_text_mentions_the_remaining_count_and_the_state(self):
        self.assertIn("残り 90件", Meter("x", "件", 10, 100).text)
        self.assertIn("上限に近い", Meter("x", "件", 80, 100).text)
        self.assertIn("この試作では制限しません", Meter("x", "件", 100, 100).text)
        self.assertNotIn("この試作では制限しません", Meter("x", "人", 3, 3, enforced=True).text)


class SubscriptionSummaryTests(SimpleTestCase):
    def test_none(self):
        self.assertEqual(subscription_summary(None)["label"], "契約情報なし")

    def test_the_three_statuses(self):
        now = timezone.now()
        trial = Subscription(status="trialing", trial_ends_at=now + timedelta(days=10))
        active = Subscription(status="active", current_period_end=now + timedelta(days=30))
        canceled = Subscription(status="canceled", canceled_at=now - timedelta(days=3))
        self.assertEqual(subscription_summary(trial, now)["label"], "お試し中")
        self.assertIn("あと 11 日", subscription_summary(trial, now)["detail"])
        self.assertEqual(subscription_summary(active, now)["label"], "契約中")
        self.assertEqual(subscription_summary(canceled, now)["label"], "解約済み")
        self.assertIn("再開の手順は、この試作にはありません", subscription_summary(canceled, now)["detail"])

    def test_an_ended_trial_says_so(self):
        now = timezone.now()
        ended = Subscription(status="trialing", trial_ends_at=now - timedelta(days=1))
        self.assertIn("終わりました", subscription_summary(ended, now)["detail"])


class SessionAuthTests(WebTestCase):
    def test_a_broken_session_value_is_anonymous(self):
        session = self.client.session
        session["saas_sub"] = "not-a-uuid"
        session.save()
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_anonymous_pages_redirect_to_the_login(self):
        for name, args in (
            ("dashboard", []),
            ("org_list", []),
            ("org_detail", [self.cafe.pk]),
            ("members_manage", [self.cafe.pk]),
        ):
            with self.subTest(page=name):
                response = self.client.get(reverse(name, args=args))
                self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_the_root_goes_to_the_dashboard(self):
        self.assertRedirects(self.client.get("/"), reverse("dashboard"), fetch_redirect_response=False)


class LoginTests(WebTestCase):
    def test_login_page_lists_the_seeded_people(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, "オーナー @ デモ カフェ")
        self.assertContains(response, "メンバー @ デモ カフェ / 閲覧者 @ デモ 寮運営")

    def test_picking_a_sub_logs_in(self):
        response = self.client.post(reverse("login"), {"sub": str(sub(1))})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=True)
        self.assertEqual(self.client.session["saas_sub"], str(sub(1)))

    def test_a_typed_uuid_wins_over_the_pick(self):
        self.client.post(reverse("login"), {"sub": str(sub(1)), "sub_typed": f" {sub(99)} "})
        self.assertEqual(self.client.session["saas_sub"], str(sub(99)))

    def test_a_bad_value_is_rejected_with_a_fixed_message(self):
        response = self.client.post(reverse("login"), {"sub_typed": "<script>x</script>"})
        self.assertContains(response, "識別子（UUID）の形が正しくありません。", status_code=400)
        self.assertNotContains(response, "<script>x</script>", status_code=400)
        self.assertNotIn("saas_sub", self.client.session)

    def test_login_changes_the_session_key(self):
        self.client.get(reverse("login"))
        self.client.post(reverse("login"), {"sub": str(sub(1))})
        after = self.client.session.session_key
        self.assertTrue(after)

    @override_settings(SAAS_DEMO_LOGIN=False)
    def test_login_is_off_when_the_demo_login_is_disabled(self):
        self.assertEqual(self.client.get(reverse("login")).status_code, 404)
        self.assertEqual(self.client.post(reverse("login"), {"sub": str(sub(1))}).status_code, 404)
        self.assertFalse(session_auth.demo_login_enabled())

    def test_logout_needs_post_and_clears_the_session(self):
        self.login(sub(1))
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("login"), fetch_redirect_response=False)


class DashboardTests(WebTestCase):
    def test_owner_sees_the_org_role_status_plan_limits_and_usage(self):
        self.login(sub(1))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "デモ カフェ")
        self.assertContains(response, "あなたの役割: <strong>オーナー</strong>")
        self.assertContains(response, "契約: <strong>お試し中</strong>")
        self.assertContains(response, "プラン: <strong>フリー</strong>")
        self.assertContains(response, "4 / 5件")  # PDF 数
        self.assertContains(response, "100 / 100回")  # 今月の検索回数
        self.assertContains(response, "3 / 3人")  # メンバー数（実数）
        self.assertContains(response, "…00000001")
        self.assertContains(response, "最近の会話")
        self.assertContains(response, "まだ会話はありません。")

    def test_the_visible_groups_are_not_shown(self):
        self.login(sub(1))
        self.assertNotContains(self.client.get(reverse("dashboard")), "見えるグループ")

    def test_the_three_usage_states_are_shown_in_words(self):
        self.login(sub(1))
        self.assertContains(self.client.get(reverse("dashboard")), "上限に近い")
        self.assertContains(self.client.get(reverse("dashboard")), "上限に達した")
        self.login(sub(4))
        self.assertContains(self.client.get(reverse("dashboard")), "余裕あり")

    def test_a_user_in_two_orgs_sees_both_with_each_role(self):
        self.login(sub(2))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "デモ カフェ")
        self.assertContains(response, "デモ 寮運営")
        self.assertContains(response, "あなたの役割: <strong>メンバー</strong>")
        self.assertContains(response, "あなたの役割: <strong>閲覧者</strong>")
        self.assertNotContains(response, "デモ 旧組織")

    def test_a_user_in_no_org_sees_both_empty_states_with_reason_and_next_step(self):
        self.login(sub(99))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "まだどの組織にも入っていません")
        self.assertContains(response, "理由:")
        self.assertContains(response, "次にやること:")
        self.assertContains(response, str(sub(99)))  # 招待を頼むための識別子
        self.assertContains(response, "まだ会話はありません。")
        self.assertNotContains(response, "デモ カフェ")

    def test_a_canceled_org_is_shown_as_canceled(self):
        self.login(sub(7))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "契約: <strong>解約済み</strong>")
        self.assertContains(response, "再開の手順は、この試作にはありません")

    def test_an_org_without_usage_or_subscription_still_renders(self):
        plan = Plan.objects.get(code="free")
        org = Organization.objects.create(name="空の組織", slug="blank", plan=plan)
        Membership.objects.create(organization=org, external_user_id=sub(50), role=Role.OWNER)
        self.login(sub(50))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "契約情報なし")
        self.assertContains(response, "0 / 5件")
        self.assertFalse(Usage.objects.filter(organization=org).exists())

    def test_names_are_escaped(self):
        self.cafe.name = "<script>alert(1)</script>"
        self.cafe.save()
        self.login(sub(1))
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_navigation_has_only_the_three_entries(self):
        self.login(sub(1))
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "ダッシュボード</a>")
        self.assertContains(response, "所属組織</a>")
        self.assertContains(response, "ログアウト</button>")
        self.assertContains(response, '<a href="/dashboard/" aria-current="page">')
        self.assertNotContains(response, '<a href="/orgs/" aria-current="page">')


class OrgPagesTests(WebTestCase):
    def test_org_list_shows_only_my_orgs(self):
        self.login(sub(2))
        response = self.client.get(reverse("org_list"))
        self.assertContains(response, "デモ カフェ")
        self.assertContains(response, "デモ 寮運営")
        self.assertNotContains(response, "デモ 旧組織")

    def test_org_list_empty_state(self):
        self.login(sub(99))
        self.assertContains(self.client.get(reverse("org_list")), "次にやること:")

    def test_detail_shows_role_status_plan_limits_and_members_strongest_first(self):
        self.login(sub(6))
        response = self.client.get(reverse("org_detail", args=[self.dorm.pk]))
        self.assertContains(response, "デモ 寮運営")
        self.assertContains(response, "あなたの役割: <strong>メンバー</strong>")
        self.assertContains(response, "契約: <strong>契約中</strong>")
        self.assertContains(response, "30 / 100件")
        self.assertContains(response, "4200 / 5000回")
        self.assertContains(response, "メンバー（4 人）")
        body = response.content.decode().split("メンバー（4 人）")[1]  # 一覧の部分だけ
        positions = [
            body.index(label)
            for label in ("オーナー</strong>", "管理者</strong>", "メンバー</strong>", "閲覧者</strong>")
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertContains(response, '<span class="tag">あなた</span>', count=1)  # 自分の行の印

    def test_detail_of_someone_elses_org_is_404(self):
        self.login(sub(1))
        self.assertEqual(self.client.get(reverse("org_detail", args=[self.dorm.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("org_detail", args=[99999])).status_code, 404)

    def test_the_manage_entry_is_shown_only_to_owner_and_admin(self):
        entry = {"owner": (1, self.cafe), "admin": (5, self.dorm), "member": (2, self.cafe), "viewer": (3, self.cafe)}
        for role, (n, org) in entry.items():
            with self.subTest(role=role):
                self.login(sub(n))
                response = self.client.get(reverse("org_detail", args=[org.pk]))
                url = reverse("members_manage", args=[org.pk])
                if role in ("owner", "admin"):
                    self.assertContains(response, url)
                else:
                    self.assertNotContains(response, url)
                    self.assertContains(response, "管理者またはオーナーが行います")

    def test_the_manage_page_is_rejected_on_the_server_for_member_and_viewer(self):
        for n, org in ((2, self.cafe), (3, self.cafe), (6, self.dorm)):
            with self.subTest(sub=n):
                self.login(sub(n))
                response = self.client.get(reverse("members_manage", args=[org.pk]))
                self.assertContains(response, "この操作をする権限がありません", status_code=403)

    def test_the_manage_page_is_open_to_owner_and_admin_and_shows_the_forms(self):
        for n, org in ((1, self.cafe), (5, self.dorm)):
            with self.subTest(sub=n):
                self.login(sub(n))
                response = self.client.get(reverse("members_manage", args=[org.pk]))
                self.assertContains(response, "メンバーを追加する")
                self.assertNotContains(response, "準備中")

    def test_the_manage_page_of_an_org_i_am_not_in_is_404(self):
        self.login(sub(1))
        self.assertEqual(self.client.get(reverse("members_manage", args=[self.dorm.pk])).status_code, 404)
