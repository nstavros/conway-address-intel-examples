"""Measure (performance normalization + signup attribution) and the weekly
report's single-recommendation rule chain."""
import time
import unittest

from engage.core.models import Draft, new_id
from engage.measure.collector import attribute_signups, record_performance
from engage.report.weekly import build_weekly

from .helpers import ctx_for, mem_store

NOW = int(time.time())


def _publish(store, platform="x", text="Comment MAP for the breakdown.", when=NOW):
    d = Draft(id=new_id(), brand="capstack", kind="original", platform=platform,
              text=text, status="PUBLISHED")
    store.save_draft(d)
    store.db.execute(
        "INSERT INTO published(draft_id,platform,published_at) VALUES(?,?,?)",
        (d.id, platform, when))
    store.db.commit()
    return d


class TestPerformanceNorm(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")
        self.store = mem_store()

    def test_median_normalization(self):
        report = record_performance(self.store, self.ctx, [
            {"post_id": "p1", "views": 100},
            {"post_id": "p2", "views": 200},
            {"post_id": "p3", "views": 400},
        ])
        self.assertEqual(report["recorded"], 3)
        self.assertEqual(self.store.latest_metric("p2", "performance_norm"), 0.5)
        self.assertEqual(self.store.latest_metric("p1", "performance_norm"), 0.25)
        self.assertEqual(self.store.latest_metric("p3", "performance_norm"), 1.0)

    def test_recompute_replaces_not_appends(self):
        record_performance(self.store, self.ctx, [{"post_id": "p1", "views": 100}])
        record_performance(self.store, self.ctx, [{"post_id": "p1", "views": 100}])
        rows = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM metrics WHERE post_id='p1' "
            "AND metric='performance_norm'").fetchone()
        self.assertEqual(rows["c"], 1)

    def test_non_numeric_fields_ignored(self):
        report = record_performance(self.store, self.ctx,
                                    [{"post_id": "p1", "views": 10, "note": "manual pull"}])
        self.assertEqual(report["recorded"], 1)


class TestSignupAttribution(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")
        self.store = mem_store()

    def test_signup_attributes_to_most_recent_code_mention(self):
        old = _publish(self.store, text="Comment MAP for the breakdown.", when=NOW - 86400 * 3)
        new = _publish(self.store, text="Comment MAP and I'll send it.", when=NOW - 3600)
        results = attribute_signups(self.store, self.ctx,
                                    [{"code": "MAP", "ts": NOW}])
        self.assertEqual(results[0]["draft_id"], new.id)
        self.assertEqual(self.store.latest_metric(new.id, "signup"), 1.0)
        self.assertIsNone(self.store.latest_metric(old.id, "signup"))

    def test_unmatched_signup_is_surfaced_not_lost(self):
        _publish(self.store, text="No code in this one.")
        results = attribute_signups(self.store, self.ctx,
                                    [{"code": "NOPE", "ts": NOW}])
        self.assertIsNone(results[0]["draft_id"])

    def test_outside_window_does_not_match(self):
        d = _publish(self.store, text="Comment MAP for it.", when=NOW - 86400 * 30)
        results = attribute_signups(self.store, self.ctx,
                                    [{"code": "MAP", "ts": NOW}])
        self.assertIsNone(results[0]["draft_id"])
        self.assertIsNone(self.store.latest_metric(d.id, "signup"))

    def test_brand_without_products_refused(self):
        ctx = ctx_for("empires")
        with self.assertRaises(ValueError):
            attribute_signups(mem_store(), ctx, [{"code": "X", "ts": NOW}])


class TestWeeklyRecommendation(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")
        self.store = mem_store()

    def test_exactly_one_recommendation(self):
        report = build_weekly(self.ctx, self.store)
        self.assertEqual(report.count("DO THIS:"), 1)

    def test_blocked_queue_wins_over_everything(self):
        d = Draft(id=new_id(), brand="capstack", kind="original", platform="x",
                  text="blocked text", status="BLOCKED",
                  gate_reasons=["llm gate blocked: BLOCK: invented figure"])
        self.store.save_draft(d)
        report = build_weekly(self.ctx, self.store)
        self.assertIn("blocked", report.split("DO THIS:")[1].lower())

    def test_cadence_gap_recommended_when_underpublished(self):
        _publish(self.store)  # 1 post against a ~13/week target
        report = build_weekly(self.ctx, self.store)
        self.assertIn("cadence", report.split("DO THIS:")[1])

    def test_missing_metrics_recommended_when_published_but_unmeasured(self):
        for platform, n in (("x", 5), ("linkedin", 3), ("instagram", 3), ("tiktok", 2)):
            for _ in range(n):
                _publish(self.store, platform=platform)
        report = build_weekly(self.ctx, self.store)
        self.assertIn("engage measure", report.split("DO THIS:")[1])


if __name__ == "__main__":
    unittest.main()
