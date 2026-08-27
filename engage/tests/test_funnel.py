import unittest

from engage.core.models import Draft, new_id
from engage.funnel.classifier import FunnelViolation, check_week, classify

from .helpers import ctx_for


def d(text, brand="capstack"):
    return Draft(id=new_id(), brand=brand, kind="original", platform="x", text=text)


TOP = "Every real estate deal has two owners. Everybody wants to be the first one."
MIDDLE = "Comment GAP and I'll DM you the map of who's who in a deal."
MIDDLE2 = "Save this before your next underwrite."
BOTTOM = "The course is open — enroll before Friday."


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")

    def test_classes(self):
        self.assertEqual(classify(BOTTOM, self.ctx), "BOTTOM")
        self.assertEqual(classify(MIDDLE, self.ctx), "MIDDLE")
        self.assertEqual(classify(MIDDLE2, self.ctx), "MIDDLE")
        self.assertEqual(classify(TOP, self.ctx), "TOP")

    def test_no_funnel_brand_returns_empty(self):
        empires = ctx_for("empires")
        self.assertEqual(classify(BOTTOM, empires), "")


class TestWeekEnforcement(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")

    def test_all_bottom_refused(self):
        with self.assertRaises(FunnelViolation):
            check_week([d(BOTTOM), d(BOTTOM), d(BOTTOM)], self.ctx)

    def test_bottom_heavy_refused(self):
        week = [d(BOTTOM), d(BOTTOM), d(TOP), d(MIDDLE)]  # 50% bottom vs 10% target
        with self.assertRaises(FunnelViolation):
            check_week(week, self.ctx)

    def test_healthy_week_passes(self):
        week = [d(TOP), d(TOP), d(TOP), d(TOP), d(TOP), d(TOP),
                d(MIDDLE), d(MIDDLE), d(MIDDLE), d(BOTTOM)]
        report = check_week(week, self.ctx)
        self.assertTrue(report["enforced"])
        self.assertEqual(report["counts"]["BOTTOM"], 1)

    def test_brand_without_funnel_not_enforced(self):
        empires = ctx_for("empires")
        report = check_week([d(BOTTOM, brand="empires")], empires)
        self.assertFalse(report["enforced"])


class TestApprovalHashBinding(unittest.TestCase):
    def test_edit_after_approval_invalidates(self):
        from engage.approval.queue import approve, edit_draft, submit
        from engage.publish.publisher import publish

        from .helpers import mem_store, pass_llm
        ctx = ctx_for("capstack")
        store = mem_store()
        draft = d(TOP)
        submit(store, ctx, draft, llm=pass_llm)
        approve(store, ctx, draft.id)

        # tamper directly with the stored text after approval
        stored = store.get_draft(draft.id, ctx.name)
        stored.text = TOP + " Also, buy my course."
        store.save_draft(stored)

        plan = publish(store, ctx, dry_run=True)
        self.assertEqual(len(plan), 1)
        self.assertIn("SKIPPED", plan[0].note)
        self.assertEqual(store.get_draft(draft.id, ctx.name).status, "DRAFT")

    def test_clean_approval_publishes_dry(self):
        from engage.approval.queue import approve, submit
        from engage.publish.publisher import publish

        from .helpers import mem_store, pass_llm
        ctx = ctx_for("capstack")
        store = mem_store()
        draft = d(TOP)
        submit(store, ctx, draft, llm=pass_llm)
        approve(store, ctx, draft.id)
        plan = publish(store, ctx, dry_run=True)
        self.assertIn("DRY RUN", plan[0].note)
        # dry run must not mark anything published
        self.assertEqual(store.list_drafts(ctx.name, status="PUBLISHED"), [])


if __name__ == "__main__":
    unittest.main()
