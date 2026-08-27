"""Cadence target parsing — posts per WEEK.

Regression suite for a live defect found 2026-08-24: _cadence_target read the
first integer and assumed per-week, so "1 reel/day" scored 1 instead of 7.
Empires' total target reported as 4/week against a real ~16, and the weekly
report's "publish more" rule (fires below 0.5 * target) effectively never
triggered — it needed fewer than 2 posts in a week.
"""
from __future__ import annotations

import unittest

from engage.report.weekly import _cadence_target

from .helpers import ctx_for


class TestLegacyStringForm(unittest.TestCase):
    def test_per_day_converts_to_per_week(self):
        # The exact bug: this returned 1 before.
        self.assertEqual(_cadence_target("1 reel/day"), 7)
        self.assertEqual(_cadence_target("1/day cross-post"), 7)

    def test_per_week_is_unchanged(self):
        self.assertEqual(_cadence_target("5-7/week"), 5)
        self.assertEqual(_cadence_target("3-4/week"), 3)
        self.assertEqual(_cadence_target("2-3/week repurposed"), 2)

    def test_range_floor_is_the_commitment(self):
        self.assertEqual(_cadence_target("2-3/week"), 2)

    def test_no_number_is_zero(self):
        self.assertEqual(_cadence_target("opportunistic"), 0)

    def test_number_without_a_period_fails_closed(self):
        # Guessing a unit is how the original bug happened. A missing target
        # is visible in the report; a wrong one is not.
        self.assertEqual(_cadence_target("3"), 0)
        self.assertEqual(_cadence_target("about 4 posts"), 0)

    def test_monthly_floors_to_zero(self):
        # ~1/month is not a weekly commitment and must not inflate the gap.
        self.assertEqual(_cadence_target("1/month"), 0)


class TestStructuredForm(unittest.TestCase):
    def test_count_and_period(self):
        self.assertEqual(_cadence_target({"count": 1, "period": "day"}), 7)
        self.assertEqual(_cadence_target({"count": 2, "period": "week"}), 2)

    def test_growth_mode_uses_the_top_level_rate(self):
        block = {"mode": "launch_growth", "count": 1, "period": "day",
                 "steady_state": {"count": 2, "period": "week"}}
        self.assertEqual(_cadence_target(block), 7)

    def test_steady_mode_uses_steady_state(self):
        block = {"mode": "steady", "count": 1, "period": "day",
                 "steady_state": {"count": 2, "period": "week"}}
        self.assertEqual(_cadence_target(block), 2)

    def test_missing_period_fails_closed(self):
        self.assertEqual(_cadence_target({"count": 3}), 0)

    def test_unknown_period_fails_closed(self):
        self.assertEqual(_cadence_target({"count": 3, "period": "fortnight"}), 0)

    def test_long_form_block_is_not_counted(self):
        # long_form lives inside the youtube block and must never be summed
        # into the weekly target.
        block = {"mode": "launch_growth", "count": 1, "period": "day",
                 "long_form": {"count": 1, "period": "month"}}
        self.assertEqual(_cadence_target(block), 7)


class TestShippedBrandConfigs(unittest.TestCase):
    """Validates the SHIPPED configs, not synthetic ones."""

    def _targets(self, slug):
        cadence = ctx_for(slug).config.get("cadence") or {}
        return {p: _cadence_target(v) for p, v in cadence.items()}

    def test_empires_total_is_twentyone_per_week(self):
        t = self._targets("empires")
        self.assertEqual(t, {"instagram": 7, "tiktok": 7, "youtube": 7})
        self.assertEqual(sum(t.values()), 21,
                         "Empires' weekly target regressed — it read 4/week "
                         "before the units fix; a silent drop back means the "
                         "cadence gap rule stopped firing.")

    def test_empires_youtube_is_in_launch_growth_mode(self):
        yt = ctx_for("empires").config["cadence"]["youtube"]
        self.assertEqual(yt["mode"], "launch_growth")
        self.assertIn("500", yt["until"])
        self.assertEqual(_cadence_target(yt), 7, "daily Shorts while growing")

    def test_capstack_unchanged_by_the_units_fix(self):
        self.assertEqual(self._targets("capstack"),
                         {"x": 5, "instagram": 3, "tiktok": 2})

    def test_stowecap_opportunistic_stays_zero(self):
        self.assertEqual(self._targets("stowecap")["x"], 0)


if __name__ == "__main__":
    unittest.main()
