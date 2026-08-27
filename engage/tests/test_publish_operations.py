"""Publishing Operations Agent (Phase 2) — dry-run only. Every test here
runs fully offline against the fixture registry (fixtures/registry/),
in-memory stores, and the DryRunSink/ManualReviewSink — no network,
no browser, no credentials, no real platform contact anywhere."""
import unittest
from pathlib import Path

from engage.config.registry import load_registry
from engage.core.models import LifecycleError
from engage.drafting.originals import (
    approve_content_record,
    create_original_content,
    transition,
    write_handoff,
)
from engage.publish.constraints import check_constraints
from engage.publish.operations import (
    PublishingRejected,
    attempt_publish,
    emit_downstream_handoffs,
    receive_handoff,
    retry_publish,
    verify_publication,
)
from engage.publish.sinks import (
    ApiSink,
    BrowserSink,
    DryRunSink,
    ManualReviewSink,
    MCPConnectorSink,
    SchedulerSink,
    SinkUnavailableError,
)

from .helpers import ROOT, ctx_for, mem_store
from .test_originals import FIXTURES, load_test_registry

REGISTRY_DIR = FIXTURES / "registry"
NOW = 1_700_000_000  # fixed, deterministic — never time.time()


def stub_for(text: str):
    def backend(prompt: str) -> str:
        return "PASS" if "'PASS' or 'BLOCK" in prompt else f"A hook.\n{text}"
    return backend


def approved_record(store, ctx, registry, material_id, text, platform="instagram", force=False):
    store.add_material(material_id, material_id, text, "note")
    material = store.get_material(material_id)
    record = create_original_content(store, ctx, registry, material, platform, stub_for(text),
                                     audience="a", objective="o", cta="c", force=force)
    return approve_content_record(store, ctx, record, approver="nick")


def override_schedule(store, ctx, registry, record, sink=None, scheduled_at=NOW):
    sink = sink or DryRunSink()
    handoff = write_handoff(store, record, "publishing")
    return receive_handoff(store, ctx, registry, handoff, sink=sink, scheduled_at=scheduled_at,
                           constraints_override_reason="test: manually verified")


def temp_registry(tmp_path, accounts_yaml, brand="capstack", canonical="Capstacknick", status="active"):
    (tmp_path / "brands.yaml").write_text(
        f'brands:\n  - canonical: "{canonical}"\n    engage_slug: {brand}\n    status: {status}\n',
        encoding="utf-8")
    (tmp_path / "accounts.yaml").write_text(accounts_yaml, encoding="utf-8")
    return load_registry(tmp_path)


class TestAccountAndRegistryRejections(unittest.TestCase):
    """§1 — malformed, disabled, unconnected, inactive, parked,
    shared-account, and non-applicable rows are all refused.

    Several of these use a PERMISSIVE registry to draft+approve (proving
    Publishing Operations doesn't just inherit whatever check happened at
    draft time) and then a STRICTER registry to publish — the realistic
    scenario where an account is disconnected, disabled, or reclassified
    shared between drafting and publishing."""

    def test_disabled_unconnected_platform_rejected(self):
        import tempfile
        from pathlib import Path
        store = mem_store()
        ctx = ctx_for("capstack")
        with tempfile.TemporaryDirectory() as d:
            permissive = temp_registry(Path(d), 'accounts:\n  - brand: "Capstacknick"\n'
                                       '    platform: tiktok\n    enabled: true\n')
            record = approved_record(store, ctx, permissive, "m1", "tiktok text", platform="tiktok")
        strict = load_test_registry()  # tiktok disabled here
        with self.assertRaises(PublishingRejected) as cm:
            override_schedule(store, ctx, strict, record)
        self.assertIn("not eligible", str(cm.exception))

    def test_parked_brand_never_eligible_even_if_drafted_with_override(self):
        # Stowecap has no prompts/original.md (deliberate Phase-1 stub, see
        # harness/social/BRIEFING.md), so it can never reach this point via
        # create_original_content — build the approved record from the
        # lower-level primitives directly to isolate what's actually under
        # test: Publishing Operations' OWN parked-brand check, independent
        # of drafting's.
        from engage.approval.queue import approve as approve_draft
        from engage.approval.queue import submit
        from engage.core.models import ContentRecord, Draft, new_id
        store = mem_store()
        ctx = ctx_for("stowecap")
        registry = load_test_registry()
        draft = Draft(id=new_id(), brand=ctx.name, kind="original", platform="linkedin",
                      text="a clean, gate-passing sentence with no blocked terms")
        draft = submit(store, ctx, draft, (), lambda p: "PASS")
        self.assertEqual(draft.status, "PENDING")
        approve_draft(store, ctx, draft.id)
        record = ContentRecord(id=new_id(), brand=ctx.name, platform="linkedin", kind="original",
                               draft_id=draft.id, brief={}, lifecycle_status="approved")
        store.save_content_record(record)
        with self.assertRaises(PublishingRejected) as cm:
            receive_handoff(store, ctx, registry,
                            {"target": "publishing", "content_record_id": record.id},
                            sink=DryRunSink(), scheduled_at=NOW, constraints_override_reason="test")
        self.assertIn("parked", str(cm.exception))

    def test_shared_account_rejected_even_if_registry_changes_between_draft_and_publish(self):
        import tempfile
        from pathlib import Path
        store = mem_store()
        ctx = ctx_for("capstack")
        with tempfile.TemporaryDirectory() as d:
            permissive = temp_registry(Path(d), 'accounts:\n  - brand: "Capstacknick"\n'
                                       '    platform: x\n    enabled: true\n')
            record = approved_record(store, ctx, permissive, "m1", "shared x text", platform="x")
        with tempfile.TemporaryDirectory() as d2:
            now_shared = temp_registry(Path(d2), 'accounts:\n  - brand: "Capstacknick"\n'
                                       '    platform: x\n    enabled: true\n    shared_account: true\n')
            with self.assertRaises(PublishingRejected) as cm:
                override_schedule(store, ctx, now_shared, record)
            self.assertIn("not eligible", str(cm.exception))

    def test_malformed_registry_never_reaches_receive_handoff(self):
        from engage.config.registry import RegistryError
        with self.assertRaises(RegistryError):
            load_registry(FIXTURES / "no-such-registry-dir")

    def test_wrong_brand_content_record_rejected(self):
        store = mem_store()
        empires_ctx = ctx_for("empires")
        capstack_ctx = ctx_for("capstack")
        registry = load_test_registry()
        # empires' own record, processed with capstack's ctx — the store is
        # physically single-brand so this can only happen via a mismatched
        # caller, but the check must hold regardless of how it happened.
        record = approved_record(store, empires_ctx, registry, "m1", "empires text", platform="instagram")
        handoff = write_handoff(store, record, "publishing")
        with self.assertRaises(PublishingRejected) as cm:
            receive_handoff(store, capstack_ctx, registry, handoff, sink=DryRunSink(),
                            scheduled_at=NOW, constraints_override_reason="test")
        self.assertIn("belongs to brand", str(cm.exception))

    def test_non_publishing_handoff_target_rejected(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        eng_handoff = write_handoff(store, record, "engagement")
        with self.assertRaises(PublishingRejected):
            receive_handoff(store, ctx, registry, eng_handoff, sink=DryRunSink(), scheduled_at=NOW)


class TestApprovalAndHashChecks(unittest.TestCase):
    """§1/§2 — not approved, hash mismatch, or changed since approval are
    all refused before anything is scheduled."""

    def test_unapproved_record_rejected(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        store.add_material("m1", "m1", "text", "note")
        material = store.get_material("m1")
        record = create_original_content(store, ctx, registry, material, "instagram", stub_for("text"),
                                         audience="a", objective="o", cta="c")
        handoff_stub = {"target": "publishing", "content_record_id": record.id}
        with self.assertRaises(PublishingRejected) as cm:
            receive_handoff(store, ctx, registry, handoff_stub, sink=DryRunSink(), scheduled_at=NOW)
        self.assertIn("not 'approved'", str(cm.exception))

    def test_edited_after_approval_rejected(self):
        # A handoff issued while approved, THEN the draft is edited — the
        # stale handoff must still be caught at processing time, not just
        # at the moment write_handoff() was originally called.
        from engage.approval.queue import edit_draft
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "original text", platform="instagram")
        handoff = write_handoff(store, record, "publishing")  # issued while still valid
        edit_draft(store, ctx, record.draft_id, "a different, unapproved sentence")
        with self.assertRaises(PublishingRejected) as cm:
            receive_handoff(store, ctx, registry, handoff, sink=DryRunSink(), scheduled_at=NOW,
                            constraints_override_reason="test")
        self.assertIn("approval hash", str(cm.exception))

    def test_already_scheduled_record_cannot_be_rescheduled(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        self.assertEqual(record.lifecycle_status, "scheduled")
        with self.assertRaises(PublishingRejected):
            override_schedule(store, ctx, registry, record)


class TestPlatformConstraintsUnknown(unittest.TestCase):
    """§4 — no invented numbers; unknown constraints fail closed."""

    def test_every_constraint_value_is_explicitly_unknown(self):
        from engage.publish.constraints import PLATFORM_CONSTRAINTS
        for platform, table in PLATFORM_CONSTRAINTS.items():
            for key, value in table.items():
                self.assertIsNone(value, f"{platform}.{key} should be an explicit unknown placeholder")

    def test_check_constraints_reports_unknown_not_ok(self):
        result = check_constraints("instagram", {"format": "reel", "caption": "hello"})
        self.assertFalse(result.ok)
        self.assertTrue(result.unknown)
        self.assertEqual(result.violations, [])

    def test_unknown_constraints_pause_the_record_without_override(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        handoff = write_handoff(store, record, "publishing")
        record = receive_handoff(store, ctx, registry, handoff, sink=DryRunSink(), scheduled_at=NOW)
        self.assertEqual(record.lifecycle_status, "paused")
        events = store.list_content_events(record.id)
        self.assertTrue(any(e["event_type"] == "constraints_unverifiable" for e in events))

    def test_caption_is_never_truncated_or_reformatted(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        original_text = "text"
        record = approved_record(store, ctx, registry, "m1", original_text, platform="instagram")
        caption_before = record.brief["caption"]
        handoff = write_handoff(store, record, "publishing")
        record = receive_handoff(store, ctx, registry, handoff, sink=DryRunSink(), scheduled_at=NOW)
        self.assertEqual(record.brief["caption"], caption_before)

    def test_override_reason_allows_proceeding_to_scheduled(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        self.assertEqual(record.lifecycle_status, "scheduled")

    def test_a_record_paused_for_constraints_can_be_resumed_with_a_later_override(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        handoff = write_handoff(store, record, "publishing")
        record = receive_handoff(store, ctx, registry, handoff, sink=DryRunSink(), scheduled_at=NOW)
        self.assertEqual(record.lifecycle_status, "paused")
        # write_handoff() would refuse a paused record — the resume path
        # must not depend on it; construct the minimal handoff shape directly,
        # exactly as the CLI does for this specific case.
        resume_handoff = {"target": "publishing", "content_record_id": record.id}
        record = receive_handoff(store, ctx, registry, resume_handoff, sink=DryRunSink(),
                                 scheduled_at=NOW, constraints_override_reason="operator confirmed later")
        self.assertEqual(record.lifecycle_status, "scheduled")

    def test_a_record_paused_for_a_different_reason_is_not_silently_resumable(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = transition(store, record, "paused", "operator_hold", {"reason": "waiting on legal review"})
        resume_handoff = {"target": "publishing", "content_record_id": record.id}
        with self.assertRaises(PublishingRejected) as cm:
            receive_handoff(store, ctx, registry, resume_handoff, sink=DryRunSink(),
                            scheduled_at=NOW, constraints_override_reason="trying to sneak past the hold")
        self.assertIn("reason other than", str(cm.exception))


class TestDuplicateScheduling(unittest.TestCase):
    """§5 — deterministic duplicate check on brand+platform+content hash."""

    def test_duplicate_content_hash_same_platform_rejected(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        r1 = approved_record(store, ctx, registry, "m1", "identical text", platform="instagram")
        r2 = approved_record(store, ctx, registry, "m2", "identical text", platform="instagram", force=True)
        override_schedule(store, ctx, registry, r1)
        with self.assertRaises(PublishingRejected) as cm:
            override_schedule(store, ctx, registry, r2)
        self.assertIn("duplicates", str(cm.exception))

    def test_different_content_same_platform_not_treated_as_duplicate(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        r1 = approved_record(store, ctx, registry, "m1", "first text", platform="instagram")
        r2 = approved_record(store, ctx, registry, "m2", "second, different text", platform="instagram")
        override_schedule(store, ctx, registry, r1)
        record = override_schedule(store, ctx, registry, r2)
        self.assertEqual(record.lifecycle_status, "scheduled")


class TestFrequencyLimit(unittest.TestCase):
    """§1 — configured posting windows/frequency rules are enforced,
    counted against SIMULATED activity only."""

    def test_daily_cap_enforced(self):
        # brands/capstack/config.yaml: rate_limits.instagram.posts_per_day == 2
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        r1 = approved_record(store, ctx, registry, "m1", "post one", platform="instagram")
        r2 = approved_record(store, ctx, registry, "m2", "post two", platform="instagram")
        r3 = approved_record(store, ctx, registry, "m3", "post three", platform="instagram")
        override_schedule(store, ctx, registry, r1)
        override_schedule(store, ctx, registry, r2)
        with self.assertRaises(PublishingRejected) as cm:
            override_schedule(store, ctx, registry, r3)
        self.assertIn("frequency", str(cm.exception))

    def test_frequency_check_does_not_touch_the_real_published_table(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        override_schedule(store, ctx, registry, record)
        day_ago = NOW - 86400
        self.assertEqual(store.published_count_since("instagram", day_ago), 0)


class TestLifecycleTransitionsPhase2(unittest.TestCase):
    """§3 — strict transitions; forbidden jumps raise."""

    def test_full_happy_path(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        self.assertEqual(record.lifecycle_status, "scheduled")
        record = attempt_publish(store, ctx, registry, record, DryRunSink())
        self.assertEqual(record.lifecycle_status, "published")
        record = verify_publication(store, ctx, registry, record, DryRunSink())
        self.assertEqual(record.lifecycle_status, "verified")

    def test_cannot_skip_from_scheduled_directly_to_verified(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        with self.assertRaises(LifecycleError):
            transition(store, record, "verified")

    def test_cannot_attempt_publish_before_scheduled(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        with self.assertRaises(PublishingRejected):
            attempt_publish(store, ctx, registry, record, DryRunSink())

    def test_cannot_verify_before_published(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        with self.assertRaises(PublishingRejected):
            verify_publication(store, ctx, registry, record, DryRunSink())


class TestManualReviewSinkBehavior(unittest.TestCase):
    """§2 — checklist only, no browser, no post, never claims 'published'."""

    def test_manual_review_produces_checklist_and_stays_scheduled(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "exact approved text", platform="instagram")
        record = override_schedule(store, ctx, registry, record, sink=ManualReviewSink())
        record = attempt_publish(store, ctx, registry, record, ManualReviewSink())
        self.assertEqual(record.lifecycle_status, "scheduled")
        self.assertEqual(record.verification_status, "ready_for_manual_posting")
        checklist = record.verification_evidence["checklist"]
        self.assertIn("exact approved text", checklist["caption"])
        self.assertIn("informational only", checklist["instructions"])

    def test_manual_review_has_no_verify_step(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record, sink=ManualReviewSink())
        record = attempt_publish(store, ctx, registry, record, ManualReviewSink())
        # still 'scheduled', not 'published' — verify_publication's own
        # precondition check refuses before ever reaching the sink
        with self.assertRaises(PublishingRejected):
            verify_publication(store, ctx, registry, record, ManualReviewSink())

    def test_manual_review_sink_class_itself_refuses_verify(self):
        with self.assertRaises(SinkUnavailableError):
            ManualReviewSink().verify(object())


class TestFailedAndAmbiguousBehavior(unittest.TestCase):
    """§5 — transient/permanent/ambiguous are classified distinctly; only
    transient is ever retryable, and only within the configured limit."""

    def _scheduled_record(self, fault_status):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        sink = DryRunSink(fault_injector=lambda: fault_status)
        record = override_schedule(store, ctx, registry, record, sink=DryRunSink())  # schedule cleanly
        return store, ctx, registry, record, sink

    def test_transient_failure_is_classified_and_retryable(self):
        store, ctx, registry, record, sink = self._scheduled_record("transient_failure")
        record = attempt_publish(store, ctx, registry, record, sink)
        self.assertEqual(record.lifecycle_status, "failed")
        self.assertEqual(record.verification_evidence["last_failure_classification"], "transient_failure")
        record = retry_publish(store, ctx, record)
        self.assertEqual(record.lifecycle_status, "scheduled")

    def test_permanent_failure_is_never_retryable(self):
        store, ctx, registry, record, sink = self._scheduled_record("permanent_failure")
        record = attempt_publish(store, ctx, registry, record, sink)
        self.assertEqual(record.lifecycle_status, "failed")
        with self.assertRaises(PublishingRejected):
            retry_publish(store, ctx, record)

    def test_ambiguous_failure_is_never_retryable(self):
        store, ctx, registry, record, sink = self._scheduled_record("ambiguous")
        record = attempt_publish(store, ctx, registry, record, sink)
        self.assertEqual(record.lifecycle_status, "failed")
        self.assertEqual(record.verification_evidence["last_failure_classification"], "ambiguous")
        with self.assertRaises(PublishingRejected) as cm:
            retry_publish(store, ctx, record)
        self.assertIn("never auto-retried", str(cm.exception))

    def test_retry_limit_enforced(self):
        store, ctx, registry, record, sink = self._scheduled_record("transient_failure")
        record = attempt_publish(store, ctx, registry, record, sink)  # retry_count -> 1
        record = retry_publish(store, ctx, record, retry_limit=2)     # 1 < 2 -> scheduled again
        record = attempt_publish(store, ctx, registry, record, sink)  # retry_count -> 2, still failed
        with self.assertRaises(PublishingRejected) as cm:
            retry_publish(store, ctx, record, retry_limit=2)          # 2 >= 2 -> refused
        self.assertIn("retry", str(cm.exception).lower())

    def test_verify_failure_also_classified(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record, sink=DryRunSink())
        record = attempt_publish(store, ctx, registry, record, DryRunSink())
        self.assertEqual(record.lifecycle_status, "published")
        failing_sink = DryRunSink(fault_injector=lambda: "transient_failure")
        record = verify_publication(store, ctx, registry, record, failing_sink)
        self.assertEqual(record.lifecycle_status, "failed")
        self.assertEqual(record.verification_evidence["last_failure_classification"], "transient_failure")


class TestNeverFakesIdentifiers(unittest.TestCase):
    """§5/§6 — a simulated publish never invents a public URL or post ID."""

    def test_dry_run_never_sets_published_url_or_platform_post_id(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        record = attempt_publish(store, ctx, registry, record, DryRunSink())
        record = verify_publication(store, ctx, registry, record, DryRunSink())
        self.assertEqual(record.published_url, "")
        self.assertEqual(record.platform_post_id, "")

    def test_sink_result_rejects_a_fake_url_on_a_simulated_result(self):
        from engage.publish.sinks import SinkResult
        with self.assertRaises(ValueError):
            SinkResult(status="ok", simulated=False, published_url="https://example.com/fake")


class TestDownstreamHandoffs(unittest.TestCase):
    """§6 — labeled simulated, no external send capability anywhere."""

    def test_verified_publication_emits_engagement_and_performance_handoffs(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        record = attempt_publish(store, ctx, registry, record, DryRunSink())
        record = verify_publication(store, ctx, registry, record, DryRunSink())
        events = [e["event_type"] for e in store.list_content_events(record.id)]
        self.assertIn("handoff:engagement", events)
        self.assertIn("handoff:performance", events)

    def test_handoff_payload_is_explicitly_labeled_simulated(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        record = attempt_publish(store, ctx, registry, record, DryRunSink())
        record = verify_publication(store, ctx, registry, record, DryRunSink())
        handoffs = emit_downstream_handoffs(store, ctx, registry, record)
        for payload in handoffs.values():
            self.assertTrue(payload["simulated"])
            self.assertIsNone(payload["platform_post_id"])
            self.assertIsNone(payload["published_url"])
            self.assertIn("NONE", payload["authorization"])

    def test_no_send_capability_exists_anywhere_in_the_publish_package(self):
        src = "\n".join((ROOT / "engage" / "publish" / f).read_text(encoding="utf-8")
                        for f in ("operations.py", "sinks.py", "constraints.py"))
        for forbidden in ("send_comment", "send_dm", "post_comment", "webbrowser.open",
                          "requests.", "urllib.request", "selenium", "playwright"):
            self.assertNotIn(forbidden, src, f"found {forbidden!r} in the publish package")


class TestIdempotencyAndEventCompleteness(unittest.TestCase):
    """§4/§7 — every transition's event has actor/brand/platform/from/to;
    nothing duplicates on repeat calls."""

    def test_full_trace_events_are_complete(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        record = override_schedule(store, ctx, registry, record)
        record = attempt_publish(store, ctx, registry, record, DryRunSink())
        record = verify_publication(store, ctx, registry, record, DryRunSink())
        transition_events = [e for e in store.list_content_events(record.id)
                             if e["event_type"] in
                             ("scheduled_dry_run", "publish_attempt_started",
                              "publish_simulated_ok", "verification_simulated_ok")]
        self.assertEqual(len(transition_events), 4)
        for e in transition_events:
            self.assertTrue(e["actor"])
            self.assertTrue(e["created_at"])
            for key in ("brand", "platform", "from", "to"):
                self.assertIn(key, e["detail"])

    def test_receive_handoff_is_not_reentrant_for_the_same_record(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text", platform="instagram")
        override_schedule(store, ctx, registry, record)
        events_after_first = len(store.list_content_events(record.id))
        with self.assertRaises(PublishingRejected):
            override_schedule(store, ctx, registry, record)
        # the rejected attempt must not have appended a phantom transition
        events_after_second_attempt = len(store.list_content_events(record.id))
        self.assertEqual(events_after_first, events_after_second_attempt)


class TestStubSinksFailClosed(unittest.TestCase):
    """§2 — every unimplemented sink refuses every call, clearly."""

    def test_all_stub_sinks_refuse_every_method(self):
        for cls in (ApiSink, MCPConnectorSink, SchedulerSink, BrowserSink):
            sink = cls()
            for method, args in (("schedule", (None, NOW, "America/New_York")),
                                 ("publish", (None,)), ("verify", (None,))):
                with self.assertRaises(SinkUnavailableError, msg=f"{cls.__name__}.{method}"):
                    getattr(sink, method)(*args)

    def test_stub_sink_error_message_states_disabled(self):
        with self.assertRaises(SinkUnavailableError) as cm:
            BrowserSink().schedule(None, NOW, "America/New_York")
        self.assertIn("disabled", str(cm.exception).lower())


class TestScheduledTaskUntouched(unittest.TestCase):
    """User directive — do not change the existing scheduled task."""

    def test_daily_review_task_still_forbids_posting(self):
        task = (Path.home() / ".claude" / "scheduled-tasks" /
               "empires-egos-daily-review" / "SKILL.md")
        if not task.exists():
            self.skipTest("scheduled task file not present in this environment")
        text = task.read_text(encoding="utf-8")
        self.assertIn("Do NOT post anything", text)


if __name__ == "__main__":
    unittest.main()
