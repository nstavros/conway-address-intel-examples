"""Regression tests for the two safety defects found in the Phase 7A
readiness assessment (DEC-SM-016) and fixed in Phase 7A-fix.

DEFECT 1 — publish/constraints.py failed OPEN for any format not listed in
REQUIRED_KEYS_BY_FORMAT: the lookup resolved an unknown format to an empty
required-key tuple, the check loop never ran, and the function returned
ok=True. A brief with an unrecognized format — or no format at all — thus
bypassed every constraint check and went straight to scheduling.

DEFECT 2 — registry `live_status` was documentation only. The account
registry describes it as the control for live publishing and DEC-SM-005
treats it as the per-cell gate, but no code read it anywhere; it appeared
in exactly one docstring. Safe only while every live-capable sink was a
stub that raised.

Everything here runs fully offline: fixture registry, in-memory stores, no
network, no browser, no credentials, no real platform contact anywhere.
Every account in every registry used here is live-DISABLED except the one
purpose-built in-memory fixture that exists solely to prove the positive
case is reachable at all."""
import unittest

from engage.config.registry import LIVE_ENABLED_VALUE, Registry, load_registry
from engage.drafting.originals import FORMATS, write_handoff
from engage.publish.constraints import (
    PLATFORM_CONSTRAINTS,
    REQUIRED_KEYS_BY_FORMAT,
    check_constraints,
)
from engage.publish.operations import (
    NON_ACTING_SINKS,
    PublishingRejected,
    attempt_publish,
    receive_handoff,
    require_live_enabled,
    verify_publication,
)
from engage.publish.sinks import (
    ApiSink,
    BrowserSink,
    DryRunSink,
    ManualReviewSink,
    MCPConnectorSink,
    PublishSink,
    SchedulerSink,
)

from .helpers import ctx_for, mem_store
from .test_originals import load_test_registry
from .test_publish_operations import NOW, approved_record


# ---------------------------------------------------------------------------
# DEFECT 1 — unknown/unsupported/missing format must fail closed
# ---------------------------------------------------------------------------
class TestUnknownFormatFailsClosed(unittest.TestCase):
    def test_unrecognized_format_is_not_ok(self):
        r = check_constraints("youtube", {"format": "bogus-format", "caption": "hi"})
        self.assertFalse(r.ok)
        self.assertTrue(r.unknown)

    def test_missing_format_is_not_ok(self):
        self.assertFalse(check_constraints("youtube", {"caption": "hi"}).ok)

    def test_empty_format_is_not_ok(self):
        self.assertFalse(check_constraints("youtube", {"format": "", "caption": "hi"}).ok)

    def test_the_original_defect_case_a_huge_caption_no_longer_passes(self):
        """The exact reproduction from DEC-SM-016: format='short' with a
        100,000-character caption previously returned ok=True."""
        r = check_constraints("youtube", {"format": "short", "caption": "x" * 100_000})
        self.assertFalse(r.ok)

    def test_unknown_format_rejected_on_every_platform_not_just_youtube(self):
        for platform in PLATFORM_CONSTRAINTS:
            with self.subTest(platform=platform):
                self.assertFalse(check_constraints(platform, {"format": "nope"}).ok)

    def test_unknown_format_reason_names_the_format_and_the_known_set(self):
        r = check_constraints("youtube", {"format": "wat"})
        joined = " ".join(r.unknown)
        self.assertIn("wat", joined)
        self.assertIn("short", joined)

    def test_known_formats_still_behave_as_before(self):
        """The fix must not turn a recognized format into a hard failure for
        the wrong reason — 'reel' still fails closed on an UNKNOWN LIMIT,
        which is a different finding from an unrecognized format."""
        r = check_constraints("youtube", {"format": "reel", "caption": "hi"})
        self.assertFalse(r.ok)
        self.assertTrue(any("caption_character_limit" in u for u in r.unknown))
        self.assertFalse(any("not a recognized format" in u for u in r.unknown))


class TestYouTubeShortFormat(unittest.TestCase):
    def test_short_is_a_recognized_format(self):
        self.assertIn("short", REQUIRED_KEYS_BY_FORMAT)

    def test_short_is_accepted_by_the_drafting_vocabulary(self):
        self.assertIn("short", FORMATS)

    def test_reel_remains_valid_and_unchanged(self):
        """'reel' is the existing catch-all for short vertical video. Adding
        'short' must not retire it or invalidate existing briefs."""
        self.assertIn("reel", FORMATS)
        self.assertEqual(REQUIRED_KEYS_BY_FORMAT["reel"], ("caption_character_limit",))

    def test_short_requires_title_description_and_media(self):
        self.assertEqual(
            REQUIRED_KEYS_BY_FORMAT["short"],
            ("title_character_limit", "description_character_limit", "media_required"),
        )

    def test_short_platform_limits_remain_unknown_not_invented(self):
        """Naming the format declares WHICH limits must be known, never what
        they are. Every YouTube value must still be None."""
        for key, value in PLATFORM_CONSTRAINTS["youtube"].items():
            with self.subTest(key=key):
                self.assertIsNone(value)

    def test_short_reports_every_unknown_limit_rather_than_the_first(self):
        r = check_constraints("youtube", {"format": "short", "title": "t", "description": "d"})
        self.assertFalse(r.ok)
        self.assertEqual(len(r.unknown), 3)


class TestUnknownFormatPausesRatherThanSchedules(unittest.TestCase):
    """End-to-end: an unverifiable constraint must land the record in
    'paused', never 'scheduled' and never 'approved'."""

    def setUp(self):
        self.store, self.ctx = mem_store(), ctx_for("empires")
        self.registry = load_test_registry()

    def _record_with_format(self, fmt):
        record = approved_record(self.store, self.ctx, self.registry, "m1", "Body text.", "instagram")
        record.brief = {**(record.brief or {}), "format": fmt}
        self.store.save_content_record(record)
        return record

    def test_unrecognized_format_pauses_and_never_schedules(self):
        record = self._record_with_format("bogus-format")
        handoff = write_handoff(self.store, record, "publishing")
        out = receive_handoff(self.store, self.ctx, self.registry, handoff,
                              sink=DryRunSink(), scheduled_at=NOW)
        self.assertEqual(out.lifecycle_status, "paused")
        self.assertEqual(out.publish_method, "")

    def test_pause_event_records_the_unknown_reason(self):
        record = self._record_with_format("bogus-format")
        handoff = write_handoff(self.store, record, "publishing")
        receive_handoff(self.store, self.ctx, self.registry, handoff,
                        sink=DryRunSink(), scheduled_at=NOW)
        events = self.store.list_content_events(record.id)
        self.assertEqual(events[-1]["event_type"], "constraints_unverifiable")
        self.assertTrue(events[-1]["detail"]["unknown"])

    def test_caption_is_never_altered_to_make_it_pass(self):
        record = self._record_with_format("bogus-format")
        original = dict(record.brief)
        handoff = write_handoff(self.store, record, "publishing")
        out = receive_handoff(self.store, self.ctx, self.registry, handoff,
                              sink=DryRunSink(), scheduled_at=NOW)
        self.assertEqual(out.brief.get("caption"), original.get("caption"))
        self.assertEqual(out.brief.get("format"), "bogus-format")


# ---------------------------------------------------------------------------
# DEFECT 2 — live_status must be enforced, not merely documented
# ---------------------------------------------------------------------------
def _registry_with(live_status, platform="instagram", brand="Empires and Egos"):
    """An in-memory registry for the positive/negative live cases. Built here
    rather than as a file fixture so no registry on disk — real or fixture —
    ever carries a live-enabled row."""
    row = {"brand": brand, "platform": platform, "enabled": True}
    if live_status is not _MISSING:
        row["live_status"] = live_status
    return Registry(
        brands=[{"canonical": brand, "engage_slug": "empires", "status": "active"}],
        accounts=[row],
    )


_MISSING = object()


class TestLiveStatusReading(unittest.TestCase):
    def test_exact_live_value_enables(self):
        self.assertTrue(_registry_with("live").is_live_enabled("empires", "instagram"))

    def test_matching_is_case_insensitive_and_whitespace_tolerant(self):
        for value in ("LIVE", "  live  ", "Live"):
            with self.subTest(value=value):
                self.assertTrue(_registry_with(value).is_live_enabled("empires", "instagram"))

    def test_disabled_is_not_live(self):
        self.assertFalse(_registry_with("disabled").is_live_enabled("empires", "instagram"))

    def test_not_applicable_is_not_live(self):
        self.assertFalse(_registry_with("not_applicable").is_live_enabled("empires", "instagram"))

    def test_missing_live_status_field_is_not_live(self):
        self.assertFalse(_registry_with(_MISSING).is_live_enabled("empires", "instagram"))

    def test_non_string_live_status_is_not_live(self):
        for value in (True, 1, [], {}, None):
            with self.subTest(value=value):
                self.assertFalse(_registry_with(value).is_live_enabled("empires", "instagram"))

    def test_missing_account_row_is_not_live(self):
        self.assertFalse(_registry_with("live").is_live_enabled("empires", "youtube"))

    def test_substring_trap_descriptive_text_containing_live_is_not_live(self):
        """The real registry's YouTube row reads 'ingest is live
        (read-only); publish disabled — ...'. A substring test for 'live'
        would authorize publishing on a cell whose own text says publishing
        is disabled. Only the literal value counts."""
        sneaky = "ingest is live (read-only); publish disabled — first live-PUBLISHING candidate"
        self.assertIn("live", sneaky)
        self.assertFalse(_registry_with(sneaky).is_live_enabled("empires", "instagram"))

    def test_enabled_true_alone_never_implies_live(self):
        r = _registry_with("disabled")
        self.assertTrue(r.account_row("empires", "instagram")["enabled"])
        self.assertFalse(r.is_live_enabled("empires", "instagram"))


class TestEveryRealAccountIsLiveDisabled(unittest.TestCase):
    """Standing assertion: nothing anywhere is live-enabled right now."""

    def test_no_row_in_the_fixture_registry_is_live_enabled(self):
        registry = load_test_registry()
        for row in registry.accounts:
            with self.subTest(brand=row.get("brand"), platform=row.get("platform")):
                slug = next(b["engage_slug"] for b in registry.brands
                            if b["canonical"] == row["brand"])
                self.assertFalse(registry.is_live_enabled(slug, row["platform"]))

    def test_exactly_one_real_harness_cell_is_live_enabled(self):
        """Live-enablement is intentional and singular as of 2026-08-22:
        Empires and Egos -> YouTube, authorized by the owner for one pending
        upload. The invariant is no longer "nothing is live" but "exactly
        this one cell is, and nothing else." A second live row, or a
        different brand/platform/channel, fails this test."""
        try:
            registry = load_registry()
        except Exception:  # noqa: BLE001 — harness registry absent in CI is fine
            self.skipTest("real harness registry not present")
        live = []
        for row in registry.accounts:
            slug = next((b["engage_slug"] for b in registry.brands
                         if b["canonical"] == row["brand"]), None)
            if slug is None:
                continue
            if registry.is_live_enabled(slug, row["platform"]):
                live.append((slug, row["platform"], row.get("channel_id")))
        self.assertEqual(
            live, [("empires", "youtube", "UC9772FnuAXMVabS0gtr6cew")],
            "exactly one cell may be live-enabled, and only the authorized one")

    def test_every_other_real_row_remains_not_live_enabled(self):
        try:
            registry = load_registry()
        except Exception:  # noqa: BLE001
            self.skipTest("real harness registry not present")
        others = 0
        for row in registry.accounts:
            slug = next((b["engage_slug"] for b in registry.brands
                         if b["canonical"] == row["brand"]), None)
            if slug is None or (slug, row["platform"]) == ("empires", "youtube"):
                continue
            with self.subTest(brand=slug, platform=row["platform"]):
                self.assertFalse(registry.is_live_enabled(slug, row["platform"]))
            others += 1
        self.assertEqual(others, 14, "expected 14 non-authorized rows")


class TestSinkGate(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("empires")

    def test_non_acting_sinks_are_allowed_without_live_enablement(self):
        registry = _registry_with("disabled")
        for sink in (DryRunSink(), ManualReviewSink()):
            with self.subTest(sink=sink.name):
                require_live_enabled(registry, self.ctx, "instagram", sink)  # must not raise

    def test_every_live_capable_sink_is_refused_when_disabled(self):
        registry = _registry_with("disabled")
        for cls in (ApiSink, MCPConnectorSink, SchedulerSink, BrowserSink):
            with self.subTest(sink=cls.name):
                with self.assertRaises(PublishingRejected):
                    require_live_enabled(registry, self.ctx, "instagram", cls())

    def test_a_brand_new_unknown_sink_is_refused_by_default(self):
        """The allowlist is the point: a sink written tomorrow is gated
        without anyone remembering to add it to a list of dangerous things."""
        class FutureYouTubeUploadSink(PublishSink):
            name = "youtube_upload"

        registry = _registry_with("disabled")
        with self.assertRaises(PublishingRejected):
            require_live_enabled(registry, self.ctx, "instagram", FutureYouTubeUploadSink())

    def test_allowlist_contains_only_the_two_provably_non_acting_sinks(self):
        self.assertEqual(NON_ACTING_SINKS, frozenset({"dry_run", "manual_review"}))

    def test_live_enabled_row_permits_a_live_capable_sink(self):
        """Proves the gate is a real gate and not an unconditional refusal."""
        require_live_enabled(_registry_with("live"), self.ctx, "instagram", ApiSink())

    def test_refusal_names_the_cell_and_the_current_status(self):
        registry = _registry_with("disabled")
        with self.assertRaises(PublishingRejected) as cm:
            require_live_enabled(registry, self.ctx, "instagram", ApiSink())
        self.assertIn("instagram", str(cm.exception))
        self.assertIn("disabled", str(cm.exception))


class TestCredentialOrConnectorNeverImpliesAuthorization(unittest.TestCase):
    """The core claim of the fix: a sink cannot operate merely because a
    connector, flag, or credential exists."""

    def setUp(self):
        self.ctx = ctx_for("empires")

    def test_a_sink_holding_a_credential_is_still_refused(self):
        class SinkWithCredential(PublishSink):
            name = "api_with_token"

            def __init__(self):
                self.oauth_token = "pretend-token"  # noqa: S105 — inert test double
                self.connector_id = "pretend-connector"

        with self.assertRaises(PublishingRejected):
            require_live_enabled(_registry_with("disabled"), self.ctx, "instagram",
                                 SinkWithCredential())

    def test_a_registry_row_with_a_connector_id_is_still_not_live(self):
        registry = Registry(
            brands=[{"canonical": "Empires and Egos", "engage_slug": "empires", "status": "active"}],
            accounts=[{"brand": "Empires and Egos", "platform": "tiktok", "enabled": True,
                       "live_status": "disabled", "connector_id": "a-real-connected-connector",
                       "connected_at": "2026-08-19T21:46:41Z"}],
        )
        self.assertFalse(registry.is_live_enabled("empires", "tiktok"))
        with self.assertRaises(PublishingRejected):
            require_live_enabled(registry, self.ctx, "tiktok", ApiSink())

    def test_publishing_method_text_claiming_connected_is_not_authorization(self):
        registry = Registry(
            brands=[{"canonical": "Empires and Egos", "engage_slug": "empires", "status": "active"}],
            accounts=[{"brand": "Empires and Egos", "platform": "tiktok", "enabled": True,
                       "live_status": "disabled",
                       "publishing_method": "connected (Higgsfield TikTok connector)"}],
        )
        self.assertFalse(registry.is_live_enabled("empires", "tiktok"))


class TestGateIsEnforcedAtEveryActionPath(unittest.TestCase):
    """schedule / publish / verify each re-check independently — a cell can
    be turned off between steps, and each call is the one that would act."""

    def setUp(self):
        self.store, self.ctx = mem_store(), ctx_for("empires")
        self.registry = load_test_registry()

    def test_receive_handoff_refuses_a_live_capable_sink(self):
        record = approved_record(self.store, self.ctx, self.registry, "m1", "Body text.", "instagram")
        handoff = write_handoff(self.store, record, "publishing")
        with self.assertRaises(PublishingRejected):
            receive_handoff(self.store, self.ctx, self.registry, handoff, sink=ApiSink(),
                            scheduled_at=NOW, constraints_override_reason="test")

    def test_refusal_leaves_the_record_untouched(self):
        record = approved_record(self.store, self.ctx, self.registry, "m1", "Body text.", "instagram")
        handoff = write_handoff(self.store, record, "publishing")
        with self.assertRaises(PublishingRejected):
            receive_handoff(self.store, self.ctx, self.registry, handoff, sink=ApiSink(),
                            scheduled_at=NOW, constraints_override_reason="test")
        after = self.store.get_content_record(record.id)
        self.assertEqual(after.lifecycle_status, "approved")
        self.assertEqual(after.publish_method, "")

    def test_attempt_publish_refuses_a_live_capable_sink(self):
        record = approved_record(self.store, self.ctx, self.registry, "m1", "Body text.", "instagram")
        handoff = write_handoff(self.store, record, "publishing")
        scheduled = receive_handoff(self.store, self.ctx, self.registry, handoff,
                                    sink=DryRunSink(), scheduled_at=NOW,
                                    constraints_override_reason="test")
        self.assertEqual(scheduled.lifecycle_status, "scheduled")
        with self.assertRaises(PublishingRejected):
            attempt_publish(self.store, self.ctx, self.registry, scheduled, ApiSink())

    def test_a_cell_switched_off_after_scheduling_blocks_the_publish_step(self):
        """Scheduled while live-enabled, then the row is turned off — the
        publish call must refuse rather than trust the earlier check."""
        live = _registry_with("live")
        record = approved_record(self.store, self.ctx, live, "m1", "Body text.", "instagram")
        handoff = write_handoff(self.store, record, "publishing")
        scheduled = receive_handoff(self.store, self.ctx, live, handoff, sink=DryRunSink(),
                                    scheduled_at=NOW, constraints_override_reason="test")
        turned_off = _registry_with("disabled")
        with self.assertRaises(PublishingRejected):
            attempt_publish(self.store, self.ctx, turned_off, scheduled, ApiSink())

    def test_verify_publication_refuses_a_live_capable_sink(self):
        record = approved_record(self.store, self.ctx, self.registry, "m1", "Body text.", "instagram")
        handoff = write_handoff(self.store, record, "publishing")
        scheduled = receive_handoff(self.store, self.ctx, self.registry, handoff,
                                    sink=DryRunSink(), scheduled_at=NOW,
                                    constraints_override_reason="test")
        published = attempt_publish(self.store, self.ctx, self.registry, scheduled, DryRunSink())
        self.assertEqual(published.lifecycle_status, "published")
        with self.assertRaises(PublishingRejected):
            verify_publication(self.store, self.ctx, self.registry, published, ApiSink())

    def test_dry_run_still_completes_the_whole_path_unchanged(self):
        """The fix must not break the existing dry-run pipeline."""
        record = approved_record(self.store, self.ctx, self.registry, "m1", "Body text.", "instagram")
        handoff = write_handoff(self.store, record, "publishing")
        rec = receive_handoff(self.store, self.ctx, self.registry, handoff, sink=DryRunSink(),
                              scheduled_at=NOW, constraints_override_reason="test")
        rec = attempt_publish(self.store, self.ctx, self.registry, rec, DryRunSink())
        rec = verify_publication(self.store, self.ctx, self.registry, rec, DryRunSink())
        self.assertEqual(rec.lifecycle_status, "verified")


if __name__ == "__main__":
    unittest.main()
