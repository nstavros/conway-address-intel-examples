"""Phase 6 — visibility/governance layer around the REAL, externally-run
ChatPlace comment-triggered-DM automation. Nothing here mocks away the real
account-binding / identity-verification logic: every test exercises
verify_binding() against the actual fixture registry, not a stub."""
from __future__ import annotations

import unittest

from engage.engagement import live_capability
from engage.engagement.live_capability import (
    ACTION_TYPE_COMMENT_TRIGGERED_DM,
    SnapshotError,
    ceo_visibility_summary,
    load_and_verify,
    load_live_automations,
    performance_signal,
    record_snapshot_audit,
)

from .helpers import FIXTURES, mem_store
from .test_originals import load_test_registry

LIVE_FIXTURES = FIXTURES / "live_capability"
VALID_SNAPSHOT = LIVE_FIXTURES / "valid_snapshot.yaml"
BAD_ACTION_TYPE_SNAPSHOT = LIVE_FIXTURES / "bad_action_type.yaml"


class TestSnapshotLoading(unittest.TestCase):
    def test_loads_valid_snapshot(self):
        verified_at, bindings = load_live_automations(VALID_SNAPSHOT)
        self.assertEqual(verified_at, "2026-08-21")
        self.assertEqual(len(bindings), 3)
        for b in bindings:
            self.assertEqual(b.action_type, ACTION_TYPE_COMMENT_TRIGGERED_DM)

    def test_rejects_non_dm_action_type(self):
        with self.assertRaises(SnapshotError):
            load_live_automations(BAD_ACTION_TYPE_SNAPSHOT)

    def test_missing_file_fails_closed(self):
        with self.assertRaises(SnapshotError):
            load_live_automations(LIVE_FIXTURES / "does_not_exist.yaml")

    def test_never_reports_comment_reply_capability(self):
        _, bindings = load_live_automations(VALID_SNAPSHOT)
        for b in bindings:
            self.assertNotIn("comment_reply", b.action_type)
            self.assertNotIn("auto_comment", b.action_type)


class TestIdentityVerification(unittest.TestCase):
    def setUp(self):
        self.registry = load_test_registry()

    def test_matching_handle_verifies(self):
        _, bindings = load_and_verify(self.registry, VALID_SNAPSHOT)
        capstack = next(b for b in bindings if b.bot_username == "@capstacknick")
        self.assertTrue(capstack.identity_verified)

    def test_empires_paused_binding_verifies_independently_of_status(self):
        _, bindings = load_and_verify(self.registry, VALID_SNAPSHOT)
        empires = next(b for b in bindings if b.bot_username == "@empires.and.egos")
        self.assertTrue(empires.identity_verified)
        self.assertEqual(empires.status, "Paused")

    def test_mismatched_handle_flagged_not_silently_trusted(self):
        _, bindings = load_and_verify(self.registry, VALID_SNAPSHOT)
        imposter = next(b for b in bindings if b.bot_username == "@not_capstacknick_at_all")
        self.assertFalse(imposter.identity_verified)
        self.assertIn("MISMATCH", imposter.verification_reason)

    def test_status_never_conflated_active_vs_paused(self):
        _, bindings = load_and_verify(self.registry, VALID_SNAPSHOT)
        statuses = {b.bot_username: b.status for b in bindings if b.identity_verified}
        self.assertEqual(statuses["@capstacknick"], "Active")
        self.assertEqual(statuses["@empires.and.egos"], "Paused")


class TestAuditTrail(unittest.TestCase):
    def test_append_only_grows_by_one_per_call(self):
        store = mem_store()
        registry = load_test_registry()
        _, bindings = load_and_verify(registry, VALID_SNAPSHOT)
        b = next(x for x in bindings if x.bot_username == "@capstacknick")
        synthetic_id = f"external:{b.automation_id}"
        record_snapshot_audit(store, b, "2026-08-21")
        self.assertEqual(len(store.list_content_events(synthetic_id)), 1)
        record_snapshot_audit(store, b, "2026-08-22")
        events = store.list_content_events(synthetic_id)
        self.assertEqual(len(events), 2)
        # prior event is untouched, not overwritten
        self.assertEqual(events[0]["detail"]["verified_at"], "2026-08-21")
        self.assertEqual(events[1]["detail"]["verified_at"], "2026-08-22")

    def test_audit_event_never_labels_capability_as_comment_reply(self):
        store = mem_store()
        registry = load_test_registry()
        _, bindings = load_and_verify(registry, VALID_SNAPSHOT)
        b = next(x for x in bindings if x.bot_username == "@capstacknick")
        record_snapshot_audit(store, b, "2026-08-21")
        events = store.list_content_events(f"external:{b.automation_id}")
        self.assertEqual(events[0]["detail"]["action_type"], ACTION_TYPE_COMMENT_TRIGGERED_DM)


class TestCeoVisibility(unittest.TestCase):
    def test_summary_scoped_to_one_brand(self):
        registry = load_test_registry()
        summary = ceo_visibility_summary(registry, "capstack", VALID_SNAPSHOT)
        usernames = {row["bot_username"] for row in summary["bindings"]}
        self.assertEqual(usernames, {"@capstacknick", "@not_capstacknick_at_all"})

    def test_summary_surfaces_unverified_flag(self):
        registry = load_test_registry()
        summary = ceo_visibility_summary(registry, "capstack", VALID_SNAPSHOT)
        flags = {row["bot_username"]: row["identity_verified"] for row in summary["bindings"]}
        self.assertTrue(flags["@capstacknick"])
        self.assertFalse(flags["@not_capstacknick_at_all"])

    def test_brand_with_no_binding_returns_empty_list(self):
        registry = load_test_registry()
        summary = ceo_visibility_summary(registry, "stowecap", VALID_SNAPSHOT)
        self.assertEqual(summary["bindings"], [])


class TestPerformanceSignal(unittest.TestCase):
    def test_zero_execution_history_labels_insufficient_data_not_a_result(self):
        store = mem_store()
        registry = load_test_registry()
        rec = performance_signal(store, registry, "capstack", VALID_SNAPSHOT)
        self.assertEqual(rec["label"], "insufficient_data")
        self.assertEqual(rec["target"], "performance")

    def test_never_fabricates_nonzero_counts(self):
        store = mem_store()
        registry = load_test_registry()
        rec = performance_signal(store, registry, "capstack", VALID_SNAPSHOT)
        self.assertEqual(rec["evidence"]["total_conversions"], 0)

    def test_brand_with_no_binding_returns_none(self):
        store = mem_store()
        registry = load_test_registry()
        rec = performance_signal(store, registry, "stowecap", VALID_SNAPSHOT)
        self.assertIsNone(rec)


if __name__ == "__main__":
    unittest.main()
