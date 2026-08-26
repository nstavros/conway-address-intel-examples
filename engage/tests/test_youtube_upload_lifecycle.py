"""Lifecycle base — store backstop and execute_youtube_upload ordering.

Regression coverage for the defect where a real upload produced a record
carrying a live published_url while its lifecycle still read 'approved',
because no orchestration function existed and the sequence was improvised.

Offline only: fake transport, in-memory store, no network, no browser, no
credential, no YouTube API. The real Troy record is never loaded or written.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from engage.core.models import ContentRecord, LifecycleError, LIFECYCLE_STATES, LIFECYCLE_TRANSITIONS
from engage.core.store import PUBLISHED_ID_STATES
from engage.drafting.legacy_intake import approve_legacy_asset
from engage.drafting.originals import transition
from engage.publish.operations import (
    PublishingRejected,
    execute_youtube_upload,
    record_rss_verification,
)
from engage.publish.youtube_sink import ConstraintSource, UploadRefused, YouTubeUploadSink
from engage.publish.youtube_transport import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_API_VALIDATION_FAILURE,
    OUTCOME_CONFIRMED,
    UploadResult,
    VERIFICATION_CONFIRMED,
    VERIFICATION_MISMATCH,
    VERIFICATION_PENDING,
)

from .test_legacy_intake_and_youtube_sink import CHANNEL, LegacyBase

REAL_CREDENTIAL = Path.home() / ".config" / "engage" / "youtube" / "empires.json"
MEDIA = {"duration_seconds": 12.8, "width": 1080, "height": 1920}


class FakeTransport:
    """Records calls, returns a scripted UploadResult. No I/O of any kind."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def upload(self, **kw):
        self.calls.append(kw)
        if self._raises:
            raise self._raises
        return self._result


def confirmed(video_id="VID123", channel=CHANNEL):
    return UploadResult(outcome=OUTCOME_CONFIRMED, http_status=201, video_id=video_id,
                        returned_title="T", returned_channel_id=channel,
                        returned_privacy_status="public", uploaded_at=1_700_000_000)


def ambiguous(video_id=""):
    return UploadResult(outcome=OUTCOME_AMBIGUOUS, video_id=video_id,
                        error_detail="HIGH RISK: may or may not have been created")


def validation_failure():
    return UploadResult(outcome=OUTCOME_API_VALIDATION_FAILURE, http_status=400,
                        error_reason="invalidCategoryId", error_detail="bad category")


class UploadBase(LegacyBase):
    @staticmethod
    def _cred_fingerprint():
        if not REAL_CREDENTIAL.exists():
            return None
        st = REAL_CREDENTIAL.stat()
        return (st.st_mtime_ns, st.st_size, st.st_mode)

    def setUp(self):
        super().setUp()
        self._cred_before = self._cred_fingerprint()

    def tearDown(self):
        self.assertEqual(self._cred_fingerprint(), self._cred_before,
                         "the real credential must be untouched")
        super().tearDown()

    def approved_record(self):
        rec = self.intake()
        sha = rec.brief["legacy_asset"]["sha256"]
        meta = self.store.get_draft(rec.draft_id, self.ctx.name).hash
        return approve_legacy_asset(self.store, self.ctx, rec, "nick",
                                    asset_sha256=sha, metadata_hash=meta)

    def sink_with(self, transport, *, enabled=True, live=True):
        if live:
            for row in self.registry.accounts:
                if row.get("brand") == "Empires and Egos" and row.get("platform") == "youtube":
                    row["live_status"] = "live"
        cred = Path(self.tmp.name) / "cred.json"
        cred.write_text(json.dumps({
            "brand": "empires", "platform": "youtube", "channel_id": CHANNEL,
            "scopes": ["https://www.googleapis.com/auth/youtube.upload",
                       "https://www.googleapis.com/auth/youtube.readonly"],
            "client_id": "x", "client_secret": "y", "refresh_token": "z"}))
        os.chmod(cred, 0o600)
        sink = YouTubeUploadSink(self.registry, self.ctx, store=self.store, enabled=enabled,
                                 constraint_source=ConstraintSource.from_file(),
                                 credential_path=cred)
        sink.transport = transport
        return sink

    def states_seen(self, rid):
        return [e["detail"].get("to") for e in self.store.list_content_events(rid)
                if e["detail"].get("to")]


# ---------------------------------------------------------------------------
# Store backstop
# ---------------------------------------------------------------------------
class TestStoreBackstop(UploadBase):
    FORBIDDEN = ("draft", "review_required", "approved", "scheduled",
                 "publishing", "paused", "rejected", "cancelled")

    def _rec(self, status, url="https://www.youtube.com/shorts/X", pid="X"):
        return ContentRecord(id="probe", brand="empires", platform="youtube",
                             kind="legacy_asset_intake", lifecycle_status=status,
                             published_url=url, platform_post_id=pid)

    def test_permitted_states_are_exactly_three(self):
        self.assertEqual(PUBLISHED_ID_STATES, ("published", "verified", "failed"))

    def test_identifier_rejected_at_every_forbidden_state(self):
        for status in self.FORBIDDEN:
            with self.subTest(status=status), self.assertRaises(LifecycleError):
                self.store.save_content_record(self._rec(status))

    def test_url_alone_is_rejected(self):
        with self.assertRaises(LifecycleError):
            self.store.save_content_record(self._rec("approved", pid=""))

    def test_post_id_alone_is_rejected(self):
        with self.assertRaises(LifecycleError):
            self.store.save_content_record(self._rec("approved", url=""))

    def test_the_exact_observed_defect_is_rejected(self):
        """approved + a live published_url — what actually happened. It is
        rejected; the pre-existing approval guard happens to fire first, which
        is defence in depth rather than a gap."""
        with self.assertRaises(LifecycleError):
            self.store.save_content_record(self._rec(
                "approved", url="https://www.youtube.com/shorts/2e_7QbSufSA",
                pid="2e_7QbSufSA"))

    def test_backstop_message_names_the_identifier_problem(self):
        """A state the approval guard does not cover, so the new backstop is
        demonstrably the thing that refuses."""
        with self.assertRaises(LifecycleError) as cm:
            self.store.save_content_record(self._rec("scheduled"))
        self.assertIn("published URL/post id", str(cm.exception))

    def test_identifier_permitted_at_published_verified_failed(self):
        for status in PUBLISHED_ID_STATES:
            with self.subTest(status=status):
                rec = self._rec(status)
                rec.id = f"ok-{status}"
                self.store.save_content_record(rec)   # must not raise

    def test_reachability_only_published_can_yield_url_bearing_failed(self):
        into_failed = {s for s, allowed in LIFECYCLE_TRANSITIONS.items() if "failed" in allowed}
        self.assertEqual(into_failed, {"published", "publishing"})
        self.assertNotIn("publishing", PUBLISHED_ID_STATES)
        for s in ("approved", "scheduled"):
            self.assertNotIn("failed", LIFECYCLE_TRANSITIONS[s])


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
class TestUploadOrdering(UploadBase):
    def test_full_legal_progression(self):
        rec = self.approved_record()
        rec, result = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                             self.sink_with(FakeTransport(confirmed())),
                                             media=MEDIA)
        self.assertEqual(result.outcome, OUTCOME_CONFIRMED)
        self.assertEqual(rec.lifecycle_status, "published")
        self.assertEqual(self.states_seen(rec.id)[-3:],
                         ["scheduled", "publishing", "published"])

    def test_no_identifier_persisted_before_published(self):
        rec = self.approved_record()
        sink = self.sink_with(FakeTransport(confirmed()))
        seen = []
        orig = self.store.save_content_record

        def spy(r):
            seen.append((r.lifecycle_status, bool(r.published_url or r.platform_post_id)))
            return orig(r)

        self.store.save_content_record = spy
        execute_youtube_upload(self.store, self.ctx, self.registry, rec, sink, media=MEDIA)
        self.store.save_content_record = orig
        for status, has_id in seen:
            with self.subTest(status=status):
                if status not in PUBLISHED_ID_STATES:
                    self.assertFalse(has_id, f"identifier present at {status}")

    def test_identifier_bound_only_after_confirmation(self):
        rec = self.approved_record()
        rec, _ = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                        self.sink_with(FakeTransport(confirmed("ABC"))),
                                        media=MEDIA)
        self.assertEqual(rec.platform_post_id, "ABC")
        self.assertEqual(rec.published_url, "https://www.youtube.com/shorts/ABC")

    def test_non_approved_refused_before_transport(self):
        rec = self.intake()
        t = FakeTransport(confirmed())
        with self.assertRaises(PublishingRejected):
            execute_youtube_upload(self.store, self.ctx, self.registry,
                                   self.store.get_content_record(rec.id),
                                   self.sink_with(t), media=MEDIA)
        self.assertEqual(t.calls, [])

    def test_disabled_sink_refused_before_transport(self):
        rec = self.approved_record()
        t = FakeTransport(confirmed())
        with self.assertRaises(UploadRefused):
            execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                   self.sink_with(t, enabled=False), media=MEDIA)
        self.assertEqual(t.calls, [])
        self.assertEqual(self.store.get_content_record(rec.id).lifecycle_status, "approved")


# ---------------------------------------------------------------------------
# Non-confirmed outcomes never bind
# ---------------------------------------------------------------------------
class TestNonConfirmedOutcomes(UploadBase):
    def test_api_validation_failure_binds_nothing(self):
        rec = self.approved_record()
        rec, result = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                             self.sink_with(FakeTransport(validation_failure())),
                                             media=MEDIA)
        self.assertEqual(result.outcome, OUTCOME_API_VALIDATION_FAILURE)
        self.assertEqual(rec.lifecycle_status, "failed")
        self.assertEqual(rec.published_url, "")
        self.assertEqual(rec.platform_post_id, "")

    def test_ambiguous_binds_nothing_and_stays_publishing(self):
        rec = self.approved_record()
        rec, result = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                             self.sink_with(FakeTransport(ambiguous("ORPHAN"))),
                                             media=MEDIA)
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)
        self.assertEqual(rec.lifecycle_status, "publishing")
        self.assertEqual(rec.published_url, "")
        self.assertEqual(rec.platform_post_id, "")

    def test_channel_mismatch_binds_nothing(self):
        mismatch = UploadResult(outcome=OUTCOME_AMBIGUOUS, http_status=201,
                                video_id="WRONG", returned_channel_id="UCwrong",
                                error_detail="HIGH RISK: DIFFERENT channel")
        rec = self.approved_record()
        rec, _ = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                        self.sink_with(FakeTransport(mismatch)), media=MEDIA)
        self.assertNotEqual(rec.lifecycle_status, "published")
        self.assertEqual(rec.published_url, "")
        self.assertEqual(rec.platform_post_id, "")

    def test_ambiguous_is_not_retried(self):
        rec = self.approved_record()
        t = FakeTransport(ambiguous())
        execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                               self.sink_with(t), media=MEDIA)
        self.assertEqual(len(t.calls), 1)

    def test_transport_exception_leaves_publishing_without_identifier(self):
        rec = self.approved_record()
        with self.assertRaises(RuntimeError):
            execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                   self.sink_with(FakeTransport(raises=RuntimeError("crash"))),
                                   media=MEDIA)
        reloaded = self.store.get_content_record(rec.id)
        self.assertEqual(reloaded.lifecycle_status, "publishing")
        self.assertEqual(reloaded.published_url, "")


# ---------------------------------------------------------------------------
# Read-back: confirmed / pending / terminal
# ---------------------------------------------------------------------------
class TestReadBack(UploadBase):
    def _published(self, vid="VID123"):
        rec = self.approved_record()
        rec, _ = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                        self.sink_with(FakeTransport(confirmed(vid))),
                                        media=MEDIA)
        return rec

    def test_confirmed_verifies_and_retains_identifier(self):
        rec = record_rss_verification(self.store, self._published(), {
            "status": VERIFICATION_CONFIRMED, "video_id": "VID123",
            "url": "https://www.youtube.com/shorts/VID123", "is_short": True})
        self.assertEqual(rec.lifecycle_status, "verified")
        self.assertEqual(rec.platform_post_id, "VID123")

    def test_pending_stays_published_and_keeps_identifier(self):
        rec = record_rss_verification(self.store, self._published(), {
            "status": VERIFICATION_PENDING, "video_id": "VID123", "detail": "lag"})
        self.assertEqual(rec.lifecycle_status, "published")
        self.assertEqual(rec.platform_post_id, "VID123")

    def test_pending_never_re_uploads(self):
        rec = self.approved_record()
        t = FakeTransport(confirmed())
        rec, _ = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                        self.sink_with(t), media=MEDIA)
        record_rss_verification(self.store, rec, {
            "status": VERIFICATION_PENDING, "video_id": "VID123", "detail": "lag"})
        self.assertEqual(len(t.calls), 1)

    def test_terminal_mismatch_fails_but_retains_identifier(self):
        rec = record_rss_verification(self.store, self._published(), {
            "status": VERIFICATION_MISMATCH, "video_id": "VID123",
            "actual_title": "Something Else", "detail": "mismatch"})
        self.assertEqual(rec.lifecycle_status, "failed")
        self.assertEqual(rec.platform_post_id, "VID123")
        self.assertTrue(rec.published_url)
        reloaded = self.store.get_content_record(rec.id)
        self.assertEqual(reloaded.platform_post_id, "VID123")

    def test_terminal_retains_published_at_and_method(self):
        rec = self._published()
        at, method = rec.published_at, rec.publish_method
        rec = record_rss_verification(self.store, rec, {
            "status": VERIFICATION_MISMATCH, "video_id": "VID123", "detail": "x"})
        self.assertEqual(rec.published_at, at)
        self.assertEqual(rec.publish_method, method)

    def test_expired_window_terminal_only_when_declared(self):
        rec = self._published()
        rec = record_rss_verification(self.store, rec, {
            "status": VERIFICATION_PENDING, "video_id": "VID123", "detail": "lag"})
        self.assertEqual(rec.lifecycle_status, "published")
        rec = record_rss_verification(self.store, rec, {
            "status": VERIFICATION_PENDING, "video_id": "VID123", "detail": "lag"},
            window_expired=True)
        self.assertEqual(rec.lifecycle_status, "failed")


# ---------------------------------------------------------------------------
# Duplicate protection and isolation
# ---------------------------------------------------------------------------
class TestDuplicateAndIsolation(UploadBase):
    def test_same_asset_refused_after_verified_upload(self):
        from engage.drafting.legacy_intake import LegacyIntakeError
        rec = self.approved_record()
        rec, _ = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                        self.sink_with(FakeTransport(confirmed())),
                                        media=MEDIA)
        record_rss_verification(self.store, rec, {
            "status": VERIFICATION_CONFIRMED, "video_id": "VID123",
            "url": "https://www.youtube.com/shorts/VID123", "is_short": True})
        with self.assertRaises(LegacyIntakeError):
            self.intake()

    def test_second_upload_of_same_record_refused(self):
        rec = self.approved_record()
        sink = self.sink_with(FakeTransport(confirmed()))
        rec, _ = execute_youtube_upload(self.store, self.ctx, self.registry, rec,
                                        sink, media=MEDIA)
        with self.assertRaises(PublishingRejected):
            execute_youtube_upload(self.store, self.ctx, self.registry, rec, sink, media=MEDIA)

    def test_operations_module_has_no_network_import(self):
        src = (Path(__file__).resolve().parents[1]
               / "engage/publish/operations.py").read_text(encoding="utf-8")
        for bad in ("urllib.request", "import requests", "http.client",
                    "import socket", "webbrowser"):
            with self.subTest(token=bad):
                self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
