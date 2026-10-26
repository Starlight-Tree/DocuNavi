from django.test import SimpleTestCase

from accessadmin import services

MATRIX = {
    "default": {"zahard", "cafe", "guides"},
    "cafeonly": {"cafe"},
    "guidesonly": {"guides"},
    "empty": set(),
}


class VisibleGroupsTests(SimpleTestCase):
    def test_no_assignment_means_the_default_role(self):
        self.assertEqual(services.visible_groups([], MATRIX), {"zahard", "cafe", "guides"})

    def test_roles_are_united_and_default_is_not_added_implicitly(self):
        self.assertEqual(services.visible_groups(["cafeonly"], MATRIX), {"cafe"})
        self.assertEqual(services.visible_groups(["cafeonly", "guidesonly"], MATRIX), {"cafe", "guides"})
        self.assertEqual(services.visible_groups(["cafeonly", "default"], MATRIX), {"zahard", "cafe", "guides"})

    def test_a_role_without_groups_or_not_in_the_matrix_adds_nothing(self):
        self.assertEqual(services.visible_groups(["empty"], MATRIX), frozenset())
        self.assertEqual(services.visible_groups(["ghost"], MATRIX), frozenset())

    def test_a_missing_default_role_leaves_nothing_visible(self):
        self.assertEqual(services.visible_groups([], {"cafeonly": {"cafe"}}), frozenset())


class PlanChangeTests(SimpleTestCase):
    def test_diff_and_before_after(self):
        change = services.plan_change(["cafeonly"], ["guidesonly"], MATRIX)
        self.assertEqual(change.add, {"guidesonly"})
        self.assertEqual(change.remove, {"cafeonly"})
        self.assertEqual((change.lost, change.gained), ({"cafe"}, {"guides"}))
        self.assertTrue(change.changed)
        self.assertTrue(change.shrinks)

    def test_giving_a_role_to_a_user_without_assignments_drops_the_default_fallback(self):
        change = services.plan_change([], ["cafeonly"], MATRIX)
        self.assertTrue(change.leaves_default_fallback)
        self.assertEqual(change.lost, {"zahard", "guides"})
        self.assertTrue(change.shrinks)

    def test_adding_default_together_keeps_what_the_user_sees(self):
        change = services.plan_change([], ["cafeonly", "default"], MATRIX)
        self.assertFalse(change.leaves_default_fallback)
        self.assertFalse(change.shrinks)

    def test_removing_every_assignment_returns_to_default_and_can_widen(self):
        change = services.plan_change(["cafeonly"], [], MATRIX)
        self.assertTrue(change.returns_to_default_fallback)
        self.assertEqual(change.gained, {"zahard", "guides"})
        self.assertFalse(change.shrinks)

    def test_losing_everything_is_flagged(self):
        change = services.plan_change([], ["empty"], MATRIX)
        self.assertTrue(change.loses_everything)
        self.assertTrue(change.shrinks)

    def test_no_change(self):
        change = services.plan_change(["cafeonly"], ["cafeonly"], MATRIX)
        self.assertFalse(change.changed)
        self.assertFalse(change.shrinks)
        self.assertEqual((change.add, change.remove), (frozenset(), frozenset()))


class HelperTests(SimpleTestCase):
    def test_unknown_roles_are_listed_sorted(self):
        self.assertEqual(services.unknown_roles(["b", "default", "a"], ["default"]), ["a", "b"])

    def test_unassigned_groups_are_registered_groups_in_no_role(self):
        self.assertEqual(services.unassigned_groups(["zahard", "cafe", "newgroup", "a"], MATRIX), ["a", "newgroup"])
        self.assertEqual(services.unassigned_groups([], MATRIX), [])
