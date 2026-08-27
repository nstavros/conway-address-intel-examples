"""Event-derived reconciliation — Option D v3.

Offline only: fake transport, in-memory stores, no network, no socket, no
browser, no credential read, no token refresh, no YouTube API. The real Troy
record is read only, never written.

ORDERING NOTE. Every ordering assertion here forces one identical created_at
across all events, so correctness can only come from content_events.id.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engage.core.models import LifecycleError
from engage.core.store import (
    AMBIGUOUS_EVENT,
    RECONCILIATION_INCONSISTENT,
    RECONCILIATION_OPEN,
    RECONCILIATION_RESOLVED_EVENTS,
    Store,
)
from engage.drafting.legacy_intake import approve_legacy_asset
from engage.drafting.originals import transition
from engage.publish import operations as ops
from engage.publish.operations import (
    AMBIGUOUS_ID_KEY,
    RECONCILIATION_ATTEST_STUDIO_CONFIRMED,
    RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
    RECONCILIATION_ATTESTATIONS,
    RECONCILIATION_BASES,
    RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK,
    PublishingRejected,
    execute_youtube_upload,
    reconcile_confirmed_no_remote_post,
    reconcile_confirmed_remote_post,
    record_rss_verification,
)
from engage.publish.youtube_transport import VERIFICATION_CONFIRMED, VERIFICATION_PENDING

from .test_legacy_intake_and_youtube_sink import CHANNEL, LegacyBase
from .test_youtube_upload_lifecycle import (
    MEDIA,
    FakeTransport,
    UploadBase,
    ambiguous,
    confirmed,
)

REAL_DB = Path("brands/empires/data/engage.db")
TROY = "aaafb1495c86"
GOOD_EVIDENCE = "opened Studio and checked the uploads list"


# ---------------------------------------------------------------------------
# Predicate — driven directly against a synthetic store
# ---------------------------------------------------------------------------
class PredicateBase(unittest.TestCase):
    T = 1787408989          # ONE timestamp for every event written here

    def setUp(self):
        self.store = Store(":memory:")

    def mk(self, rid, lifecycle="publishing"):
        self.store.db.execute(
            "INSERT INTO content_records(id,brand,platform,kind,lifecycle_status,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (rid, "empires", "youtube", "legacy_asset_intake", lifecycle, 0, 0))
        self.store.db.commit()

    def ev(self, rid, event_type):
        self.store.db.execute(
            "INSERT INTO content_events(content_record_id,event_type,detail,actor,created_at) "
            "VALUES(?,?,'{}','test',?)", (rid, event_type, self.T))
        self.store.db.commit()

    def life(self, rid, lifecycle):
        self.store.db.execute("UPDATE content_records SET lifecycle_status=? WHERE id=?",
                              (lifecycle, rid))
        self.store.db.commit()

    def distinct_timestamps(self, rid):
        return self.store.db.execute(
            "SELECT COUNT(DISTINCT created_at) FROM content_events WHERE content_record_id=?",
            (rid,)).fetchone()[0]


class TestPredicate(PredicateBase):
    def test_ambiguous_only_is_required(self):
        self.mk("a"); self.ev("a", AMBIGUOUS_EVENT)
        s = self.store.record_requires_reconciliation("a")
        self.assertTrue(s.required)
        self.assertEqual(s.category, RECONCILIATION_OPEN)

    def test_same_second_ambiguous_then_resolution_is_resolved(self):
        self.mk("b"); self.ev("b", AMBIGUOUS_EVENT); self.ev("b", RECONCILIATION_RESOLVED_EVENTS[0])
        s = self.store.record_requires_reconciliation("b")
        self.assertFalse(s.required)
        self.assertEqual(self.distinct_timestamps("b"), 1)

    def test_same_second_resolution_then_ambiguous_is_required(self):
        self.mk("c"); self.ev("c", RECONCILIATION_RESOLVED_EVENTS[0]); self.ev("c", AMBIGUOUS_EVENT)
        s = self.store.record_requires_reconciliation("c")
        self.assertTrue(s.required)
        self.assertGreater(s.ambiguous_event_id, s.resolution_event_id)
        self.assertEqual(self.distinct_timestamps("c"), 1)

    def test_ordering_uses_event_id_not_timestamp(self):
        self.mk("d"); self.ev("d", RECONCILIATION_RESOLVED_EVENTS[1]); self.ev("d", AMBIGUOUS_EVENT)
        s = self.store.record_requires_reconciliation("d")
        self.assertEqual(self.distinct_timestamps("d"), 1)
        self.assertTrue(s.required)

    def test_multiple_ambiguous_one_later_resolution_resolved(self):
        self.mk("e")
        self.ev("e", AMBIGUOUS_EVENT); self.ev("e", AMBIGUOUS_EVENT)
        self.ev("e", RECONCILIATION_RESOLVED_EVENTS[0])
        self.assertFalse(self.store.record_requires_reconciliation("e").required)

    def test_multiple_resolutions_latest_wins(self):
        self.mk("f")
        self.ev("f", AMBIGUOUS_EVENT)
        self.ev("f", RECONCILIATION_RESOLVED_EVENTS[0])
        self.ev("f", RECONCILIATION_RESOLVED_EVENTS[1])
        self.assertFalse(self.store.record_requires_reconciliation("f").required)

    def test_strict_greater_than(self):
        self.mk("g"); self.ev("g", RECONCILIATION_RESOLVED_EVENTS[0]); self.ev("g", AMBIGUOUS_EVENT)
        s = self.store.record_requires_reconciliation("g")
        self.assertEqual(s.ambiguous_event_id, s.resolution_event_id + 1)
        self.assertTrue(s.required)

    def test_no_ambiguous_event_is_not_required(self):
        self.mk("h"); self.ev("h", "upload_confirmed")
        self.assertFalse(self.store.record_requires_reconciliation("h").required)

    def test_unknown_record_is_not_required(self):
        s = self.store.record_requires_reconciliation("nope")
        self.assertFalse(s.required)
        self.assertEqual(s.lifecycle_status, "")

    def test_non_publishing_unresolved_is_lifecycle_inconsistency(self):
        for lifecycle in ("approved", "scheduled", "published", "verified",
                          "failed", "paused", "cancelled"):
            with self.subTest(lifecycle=lifecycle):
                rid = f"i_{lifecycle}"
                self.mk(rid, lifecycle); self.ev(rid, AMBIGUOUS_EVENT)
                s = self.store.record_requires_reconciliation(rid)
                self.assertTrue(s.required)
                self.assertEqual(s.category, RECONCILIATION_INCONSISTENT)
                self.assertTrue(s.is_urgent)
                self.assertFalse(s.is_resolvable)

    def test_lifecycle_transition_does_not_clear_the_flag(self):
        self.mk("j"); self.ev("j", AMBIGUOUS_EVENT)
        for lifecycle in ("published", "verified", "failed", "cancelled"):
            self.life("j", lifecycle)
            self.assertTrue(self.store.record_requires_reconciliation("j").required)

    def test_rss_events_do_not_clear_the_flag(self):
        self.mk("k"); self.ev("k", AMBIGUOUS_EVENT)
        for t in ("rss_verification_pending", "rss_readback_verified",
                  "rss_readback_failed_terminal", "transition:published"):
            self.ev("k", t)
        self.assertTrue(self.store.record_requires_reconciliation("k").required)

    def test_only_a_resolution_event_clears_it(self):
        self.mk("l"); self.ev("l", AMBIGUOUS_EVENT)
        self.assertTrue(self.store.record_requires_reconciliation("l").required)
        self.ev("l", RECONCILIATION_RESOLVED_EVENTS[0])
        self.assertFalse(self.store.record_requires_reconciliation("l").required)

    def test_list_returns_both_categories_ordered(self):
        self.mk("m1", "publishing"); self.ev("m1", AMBIGUOUS_EVENT)
        self.mk("m2", "verified"); self.ev("m2", AMBIGUOUS_EVENT)
        out = self.store.list_records_requiring_reconciliation("empires")
        self.assertEqual([x.record_id for x in out], ["m1", "m2"])
        self.assertEqual(out[0].category, RECONCILIATION_OPEN)
        self.assertEqual(out[1].category, RECONCILIATION_INCONSISTENT)

    def test_list_excludes_resolved(self):
        self.mk("n"); self.ev("n", AMBIGUOUS_EVENT); self.ev("n", RECONCILIATION_RESOLVED_EVENTS[1])
        self.assertEqual(self.store.list_records_requiring_reconciliation("empires"), [])


# ---------------------------------------------------------------------------
# Blocking across all five forward paths
# ---------------------------------------------------------------------------
class TestForwardPathsBlocked(PredicateBase):
    def test_guard_blocks_at_every_lifecycle(self):
        from engage.core.models import ContentRecord
        for lifecycle in ("approved", "scheduled", "publishing", "published",
                          "verified", "failed", "paused", "cancelled"):
            with self.subTest(lifecycle=lifecycle):
                rid = f"blk_{lifecycle}"
                self.mk(rid, lifecycle); self.ev(rid, AMBIGUOUS_EVENT)
                rec = ContentRecord(id=rid, brand="empires", platform="youtube",
                                    kind="legacy_asset_intake", lifecycle_status=lifecycle)
                for action in ("scheduling", "publishing", "verification",
                               "upload", "public-feed read-back"):
                    with self.assertRaises(PublishingRejected):
                        ops.assert_no_open_reconciliation(self.store, rec, action)

    def test_guard_passes_when_not_required(self):
        from engage.core.models import ContentRecord
        self.mk("clean")
        rec = ContentRecord(id="clean", brand="empires", platform="youtube",
                            kind="legacy_asset_intake", lifecycle_status="publishing")
        ops.assert_no_open_reconciliation(self.store, rec, "upload")   # must not raise


# ---------------------------------------------------------------------------
# Ambiguous handling through the real orchestrator
# ---------------------------------------------------------------------------
class TestAmbiguousHandling(UploadBase):
    def _run(self, video_id="ORPHAN7"):
        rec = self.approved_record()
        t = FakeTransport(ambiguous(video_id=video_id))
        rec, result = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                             self.sink_with(t), media=MEDIA)
        return rec, result, t

    def test_identifier_fields_stay_empty(self):
        rec, _, _ = self._run()
        self.assertEqual(rec.platform_post_id, "")
        self.assertEqual(rec.published_url, "")
        reloaded = self.store.get_content_record(rec.id)
        self.assertEqual(reloaded.platform_post_id, "")
        self.assertEqual(reloaded.published_url, "")

    def test_candidate_id_only_in_the_ambiguous_event(self):
        rec, _, _ = self._run("ORPHAN7")
        events = self.store.list_content_events(rec.id)
        amb = [e for e in events if e["event_type"] == AMBIGUOUS_EVENT]
        self.assertEqual(len(amb), 1)
        self.assertEqual(amb[0]["detail"][AMBIGUOUS_ID_KEY], "ORPHAN7")
        for e in events:
            if e["event_type"] == AMBIGUOUS_EVENT:
                continue
            self.assertNotIn("ORPHAN7", json.dumps(e, default=str))

    def test_event_never_uses_the_confirmed_key_name(self):
        rec, _, _ = self._run()
        amb = next(e for e in self.store.list_content_events(rec.id)
                   if e["event_type"] == AMBIGUOUS_EVENT)
        self.assertNotIn("video_id", amb["detail"])

    def test_no_url_built_from_candidate(self):
        rec, _, _ = self._run("ORPHAN7")
        blob = json.dumps(self.store.list_content_events(rec.id), default=str)
        self.assertNotIn("youtube.com/shorts/ORPHAN7", blob)

    def test_operational_wording_present(self):
        rec, _, _ = self._run()
        amb = next(e for e in self.store.list_content_events(rec.id)
                   if e["event_type"] == AMBIGUOUS_EVENT)
        self.assertEqual(amb["detail"]["status_note"],
                         "unresolved remote-upload outcome; manual reconciliation required")
        self.assertIs(amb["detail"]["is_ordinary_in_flight"], False)
        self.assertIs(amb["detail"]["requires_manual_reconciliation"], True)

    def test_no_remote_id_omits_the_key(self):
        rec, _, _ = self._run("")
        amb = next(e for e in self.store.list_content_events(rec.id)
                   if e["event_type"] == AMBIGUOUS_EVENT)
        self.assertNotIn(AMBIGUOUS_ID_KEY, amb["detail"])

    def test_predicate_flags_it(self):
        rec, _, _ = self._run()
        self.assertTrue(self.store.record_requires_reconciliation(rec.id).required)

    def test_further_upload_is_blocked(self):
        rec, _, t = self._run()
        with self.assertRaises(PublishingRejected):
            execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                   self.sink_with(t), media=MEDIA)
        self.assertEqual(len(t.calls), 1)

    def test_rss_read_back_is_blocked(self):
        rec, _, _ = self._run()
        with self.assertRaises(PublishingRejected):
            record_rss_verification(self.store, rec, {
                "status": VERIFICATION_CONFIRMED, "video_id": "ORPHAN7"})

    def test_duplicate_intake_still_refused(self):
        from engage.drafting.legacy_intake import LegacyIntakeError
        self._run()
        with self.assertRaises(LegacyIntakeError):
            self.intake()

    def test_confirmed_upload_is_not_flagged(self):
        rec = self.approved_record()
        rec, _ = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                        self.sink_with(FakeTransport(confirmed())),
                                        media=MEDIA)
        self.assertFalse(self.store.record_requires_reconciliation(rec.id).required)


# ---------------------------------------------------------------------------
# Resolution: attestations and basis
# ---------------------------------------------------------------------------
class TestResolution(UploadBase):
    def _open(self):
        rec = self.approved_record()
        rec, _ = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                        self.sink_with(FakeTransport(ambiguous("ORPHAN7"))),
                                        media=MEDIA)
        return rec

    def _snapshot(self, rid):
        r = self.store.get_content_record(rid)
        return (r.lifecycle_status, r.published_url, r.platform_post_id,
                len(self.store.list_content_events(rid)))

    def test_allowlists_are_exact(self):
        self.assertEqual(RECONCILIATION_BASES,
                         (RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK,))
        self.assertEqual(set(RECONCILIATION_ATTESTATIONS),
                         {RECONCILIATION_ATTEST_STUDIO_CONFIRMED,
                          RECONCILIATION_ATTEST_STUDIO_NO_MATCH})

    def test_no_prose_filtering_exists(self):
        self.assertFalse(hasattr(ops, "UNACCEPTABLE_NO_MATCH_BASES"))
        src = (Path(__file__).resolve().parents[1]
               / "engage/publish/operations.py").read_text(encoding="utf-8")
        self.assertNotIn("evidence.lower()", src)

    def test_confirmed_post_rejects_bad_attestations_inertly(self):
        for att in (None, "", "  ", "i_looked", RECONCILIATION_ATTEST_STUDIO_NO_MATCH):
            with self.subTest(att=att):
                rec = self._open(); before = self._snapshot(rec.id)
                with self.assertRaises(PublishingRejected):
                    reconcile_confirmed_remote_post(
                        self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
                        attestation=att, confirmed_video_id="ABC",
                        expected_channel_id=CHANNEL)
                self.assertEqual(self._snapshot(rec.id), before)
                self.tearDown(); self.setUp()

    def test_no_match_rejects_bad_attestations_inertly(self):
        for att in (None, "", "x", RECONCILIATION_ATTEST_STUDIO_CONFIRMED):
            with self.subTest(att=att):
                rec = self._open(); before = self._snapshot(rec.id)
                with self.assertRaises(PublishingRejected):
                    reconcile_confirmed_no_remote_post(
                        self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
                        attestation=att,
                        basis=RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)
                self.assertEqual(self._snapshot(rec.id), before)
                self.tearDown(); self.setUp()

    def test_no_match_rejects_bad_basis_inertly(self):
        for basis in (None, "", "   ", "rss_absence", "assumed", "YOUTUBE_STUDIO_DIRECT_CHECK"):
            with self.subTest(basis=basis):
                rec = self._open(); before = self._snapshot(rec.id)
                with self.assertRaises(PublishingRejected):
                    reconcile_confirmed_no_remote_post(
                        self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
                        attestation=RECONCILIATION_ATTEST_STUDIO_NO_MATCH, basis=basis)
                self.assertEqual(self._snapshot(rec.id), before)
                self.tearDown(); self.setUp()

    def test_actor_and_evidence_required(self):
        rec = self._open(); before = self._snapshot(rec.id)
        with self.assertRaises(PublishingRejected):
            reconcile_confirmed_no_remote_post(
                self.store, rec, actor="  ", evidence=GOOD_EVIDENCE,
                attestation=RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
                basis=RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)
        with self.assertRaises(PublishingRejected):
            reconcile_confirmed_no_remote_post(
                self.store, rec, actor="nick", evidence="short",
                attestation=RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
                basis=RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)
        self.assertEqual(self._snapshot(rec.id), before)

    def test_confirmed_post_requires_id_and_exact_channel(self):
        rec = self._open(); before = self._snapshot(rec.id)
        for vid, chan in (("", CHANNEL), ("ABC", "UCwrong"), ("ABC", "")):
            with self.subTest(vid=vid, chan=chan), self.assertRaises(PublishingRejected):
                reconcile_confirmed_remote_post(
                    self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
                    attestation=RECONCILIATION_ATTEST_STUDIO_CONFIRMED,
                    confirmed_video_id=vid, expected_channel_id=chan)
        self.assertEqual(self._snapshot(rec.id), before)

    def test_confirmed_post_success(self):
        rec = self._open()
        rec = reconcile_confirmed_remote_post(
            self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
            attestation=RECONCILIATION_ATTEST_STUDIO_CONFIRMED,
            confirmed_video_id="ABC", expected_channel_id=CHANNEL)
        self.assertEqual(rec.lifecycle_status, "published")
        self.assertEqual(rec.platform_post_id, "ABC")
        self.assertFalse(self.store.record_requires_reconciliation(rec.id).required)
        ev = next(e for e in self.store.list_content_events(rec.id)
                  if e["event_type"] == "reconciliation_confirmed_remote_post")
        self.assertEqual(ev["detail"]["attestation"], RECONCILIATION_ATTEST_STUDIO_CONFIRMED)
        self.assertEqual(ev["detail"]["reconciled_by"], "nick")

    def test_no_match_success(self):
        rec = self._open()
        rec = reconcile_confirmed_no_remote_post(
            self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
            attestation=RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
            basis=RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)
        self.assertEqual(rec.lifecycle_status, "failed")
        self.assertEqual(rec.platform_post_id, "")
        self.assertEqual(rec.published_url, "")
        self.assertFalse(self.store.record_requires_reconciliation(rec.id).required)
        ev = next(e for e in self.store.list_content_events(rec.id)
                  if e["event_type"] == "reconciliation_confirmed_no_remote_post")
        self.assertEqual(ev["detail"]["basis"],
                         RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)
        self.assertIs(ev["detail"]["inferred_from_feed_absence"], False)

    def test_evidence_may_mention_rss_and_feed_freely(self):
        """Structured basis is authoritative — prose is never inspected."""
        rec = self._open()
        prose = ("checked Studio directly; the RSS feed had not updated and I did not "
                 "rely on it, nor on any timeout")
        rec = reconcile_confirmed_no_remote_post(
            self.store, rec, actor="nick", evidence=prose,
            attestation=RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
            basis=RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)
        self.assertEqual(rec.lifecycle_status, "failed")

    def test_evidence_reference_is_truncated(self):
        rec = self._open()
        rec = reconcile_confirmed_no_remote_post(
            self.store, rec, actor="nick", evidence="x" * 500,
            attestation=RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
            basis=RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)
        ev = next(e for e in self.store.list_content_events(rec.id)
                  if e["event_type"] == "reconciliation_confirmed_no_remote_post")
        self.assertEqual(len(ev["detail"]["evidence_reference"]), 300)

    def test_refuses_when_nothing_open(self):
        rec = self.approved_record()
        with self.assertRaises(PublishingRejected):
            reconcile_confirmed_no_remote_post(
                self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
                attestation=RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
                basis=RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)


# ---------------------------------------------------------------------------
# Sealed lifecycle inconsistency
# ---------------------------------------------------------------------------
class TestSealedInconsistency(PredicateBase):
    def _rec(self, rid, lifecycle):
        from engage.core.models import ContentRecord
        self.mk(rid, lifecycle); self.ev(rid, AMBIGUOUS_EVENT)
        return ContentRecord(id=rid, brand="empires", platform="youtube",
                             kind="legacy_asset_intake", lifecycle_status=lifecycle)

    def test_both_functions_refuse_outside_publishing(self):
        for lifecycle in ("approved", "scheduled", "published", "verified",
                          "failed", "cancelled", "paused"):
            with self.subTest(lifecycle=lifecycle):
                rec = self._rec(f"s_{lifecycle}", lifecycle)
                before = len(self.store.list_content_events(rec.id))
                with self.assertRaises(PublishingRejected) as cm:
                    reconcile_confirmed_remote_post(
                        self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
                        attestation=RECONCILIATION_ATTEST_STUDIO_CONFIRMED,
                        confirmed_video_id="ABC", expected_channel_id=CHANNEL)
                msg = str(cm.exception)
                self.assertIn("outside the 'publishing' parking state", msg)
                self.assertIn("forensic repair", msg)
                self.assertIn("Nothing was changed", msg)
                with self.assertRaises(PublishingRejected):
                    reconcile_confirmed_no_remote_post(
                        self.store, rec, actor="nick", evidence=GOOD_EVIDENCE,
                        attestation=RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
                        basis=RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK)
                self.assertEqual(len(self.store.list_content_events(rec.id)), before)
                self.assertTrue(self.store.record_requires_reconciliation(rec.id).required)


# ---------------------------------------------------------------------------
# Troy record — read only
# ---------------------------------------------------------------------------
class TestTroyUnaffected(unittest.TestCase):
    @unittest.skipUnless(REAL_DB.exists(), "live db not present")
    def test_troy_not_flagged_because_it_has_no_ambiguous_event(self):
        db = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            amb = db.execute(
                "SELECT COUNT(*) FROM content_events WHERE content_record_id=? AND event_type=?",
                (TROY, AMBIGUOUS_EVENT)).fetchone()[0]
            life = db.execute("SELECT lifecycle_status FROM content_records WHERE id=?",
                              (TROY,)).fetchone()["lifecycle_status"]
            events = db.execute(
                "SELECT COUNT(*) FROM content_events WHERE content_record_id=?",
                (TROY,)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(amb, 0, "excluded by clause (b), not by lifecycle")
        self.assertEqual(life, "verified")
        self.assertEqual(events, 11)


if __name__ == "__main__":
    unittest.main()
