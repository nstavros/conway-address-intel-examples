"""Social Media CEO Agent (Phase 4) — planning / read-only.
Synthetic fixtures only. No network, browser, credential, or external
action anywhere in this suite or the code it exercises."""
import tempfile
import unittest
from pathlib import Path

from engage.ceo import learning_queue
from engage.ceo.agent import (
    ESCALATION_TRIGGERS,
    INTERNAL_DECISIONS,
    DirectiveRejected,
    EscalationRejected,
    business_objective,
    create_directive,
    create_escalation,
    detect_conflicts,
)
from engage.ceo.learning_queue import LearningQueueRejected, list_queue, queue_candidate, review_candidate
from engage.ceo.reports import build_ceo_daily_exceptions, build_ceo_weekly
from engage.config.registry import load_registry
from engage.drafting.originals import approve_content_record, create_original_content
from engage.measure.diagnosis import Finding

from .helpers import PKG_DIR, ctx_for, mem_store
from .test_originals import FIXTURES, load_test_registry

NOW = 1_700_000_000
REAL_REGISTRY = Path.home() / ".claude" / "harness" / "social" / "registry"


def stub(prompt: str) -> str:
    return "PASS" if "'PASS' or 'BLOCK" in prompt else "A hook.\nCaption body."


def approved_record(store, ctx, registry, mid, text, platform="instagram"):
    store.add_material(mid, mid, text, "note")
    rec = create_original_content(store, ctx, registry, store.get_material(mid), platform, stub,
                                  audience="a", objective="o", cta="c")
    return approve_content_record(store, ctx, rec, approver="nick")


def directive_kwargs(**over):
    base = dict(business_objective="raw_growth", audience="history viewers",
                platform_scope=["instagram"], campaign="q3-format-confirm",
                owner_agent="planning_drafting", deliverable="Draft 2 posts",
                deadline="2026-08-28", success_metric="views",
                approval_requirement="human approval required before publishing",
                exclusions=["no paid promotion"])
    base.update(over)
    return base


class TestPermissionBoundaries(unittest.TestCase):
    """§1 — no publish/schedule/comment/DM/browser/credential/account paths."""

    def test_ceo_package_imports_nothing_write_capable(self):
        forbidden = ("from ..publish", "from ..approval.queue import approve",
                     "from ..approval.queue import submit", "webbrowser", "requests.",
                     "urllib.request", "selenium", "playwright", "SINKS", "DryRunSink")
        for f in ("agent.py", "reports.py", "learning_queue.py"):
            src = (PKG_DIR / "ceo" / f).read_text(encoding="utf-8")
            for term in forbidden:
                self.assertNotIn(term, src, f"ceo/{f} references {term!r}")

    def test_ceo_package_never_calls_state_write_paths(self):
        forbidden = ("save_content_record", "save_draft", "record_approval",
                     "set_draft_status", "record_published", "save_experiment")
        for f in ("agent.py", "reports.py", "learning_queue.py"):
            src = (PKG_DIR / "ceo" / f).read_text(encoding="utf-8")
            for term in forbidden:
                self.assertNotIn(term, src, f"ceo/{f} calls {term!r}")

    def test_no_credential_handling(self):
        for f in ("agent.py", "reports.py", "learning_queue.py"):
            src = (PKG_DIR / "ceo" / f).read_text(encoding="utf-8").lower()
            for term in ("password", "oauth_token", "api_key", "cookie"):
                # learning_queue names these only to BLOCK them
                if f == "learning_queue.py":
                    continue
                self.assertNotIn(term, src, f"ceo/{f} references {term!r}")

    def test_directive_carries_explicit_no_authorization(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        d = create_directive(store, ctx, registry, **directive_kwargs(business_objective="leads"))
        self.assertIn("NONE", d["authorization"])
        self.assertIn("does not authorize", d["authorization"])


class TestBrandAndAccountIsolation(unittest.TestCase):
    """§3/§4 — brands stay separate; ineligible targets refused."""

    def test_directive_refuses_ineligible_platform(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        with self.assertRaises(DirectiveRejected) as cm:
            create_directive(store, ctx, registry, **directive_kwargs(platform_scope=["tiktok"]))
        self.assertIn("not eligible", str(cm.exception))

    def test_directive_refuses_shared_account_platform(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        with self.assertRaises(DirectiveRejected):
            create_directive(store, ctx, registry, **directive_kwargs(platform_scope=["x"]))

    def test_directive_refuses_parked_brand(self):
        store, ctx, registry = mem_store(), ctx_for("stowecap"), load_test_registry()
        with self.assertRaises(DirectiveRejected) as cm:
            create_directive(store, ctx, registry, **directive_kwargs(platform_scope=["linkedin"]))
        self.assertIn("parked", str(cm.exception))

    def test_conflict_scan_flags_cross_brand_record(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        rec.brand = "empires"
        store.save_content_record(rec)
        kinds = {c["kind"] for c in detect_conflicts(store, ctx, registry)}
        self.assertIn("cross_brand_contamination", kinds)


class TestDirectiveValidation(unittest.TestCase):
    """§3 — every required field present, else refused."""

    def test_all_required_fields_enforced(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        for field in ("business_objective", "audience", "campaign", "deliverable",
                      "deadline", "success_metric", "approval_requirement"):
            with self.assertRaises(DirectiveRejected, msg=f"{field} not enforced"):
                create_directive(store, ctx, registry, **directive_kwargs(**{field: ""}))

    def test_unknown_owner_agent_refused(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        with self.assertRaises(DirectiveRejected):
            create_directive(store, ctx, registry, **directive_kwargs(owner_agent="marketing_intern"))

    def test_directive_is_traceable(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        d = create_directive(store, ctx, registry, **directive_kwargs(traces_to=[rec.id]))
        self.assertEqual(d["traces_to"], [rec.id])
        self.assertTrue(any(r["id"] == d["id"] for r in store.list_recommendations(ctx.name)))


class TestEscalationPolicy(unittest.TestCase):
    """§5 — internal decisions stay internal; escalations carry all 6 fields."""

    def _kwargs(self, **over):
        base = dict(trigger="live_publishing", issue="Live publishing requested",
                    why_it_matters="No live sink exists and no connector is authorized",
                    recommended_action="Keep dry-run until a sink is built and reviewed",
                    alternatives=["manual_review checklist"], deadline="2026-08-28",
                    consequence_of_no_decision="Publishing stays dry-run; nothing goes out")
        base.update(over)
        return base

    def test_internal_decision_cannot_be_escalated(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        for trigger in INTERNAL_DECISIONS:
            with self.assertRaises(EscalationRejected, msg=trigger):
                create_escalation(store, ctx, registry, **self._kwargs(trigger=trigger))

    def test_unknown_trigger_refused(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        with self.assertRaises(EscalationRejected):
            create_escalation(store, ctx, registry, **self._kwargs(trigger="vibes"))

    def test_all_six_required_fields_enforced(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        for field in ("issue", "why_it_matters", "recommended_action", "deadline",
                      "consequence_of_no_decision"):
            with self.assertRaises(EscalationRejected, msg=field):
                create_escalation(store, ctx, registry, **self._kwargs(**{field: ""}))

    def test_valid_escalation_records_all_fields(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        e = create_escalation(store, ctx, registry, **self._kwargs())
        for key in ("issue", "why_it_matters", "recommended_action", "alternatives",
                    "deadline", "consequence_of_no_decision"):
            self.assertTrue(e[key], key)

    def test_every_escalation_trigger_is_accepted(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        for trigger in ESCALATION_TRIGGERS:
            e = create_escalation(store, ctx, registry, **self._kwargs(trigger=trigger))
            self.assertEqual(e["trigger"], trigger)


class TestApprovalAndGateNonBypass(unittest.TestCase):
    """§4 — the CEO cannot move content toward publishing."""

    def test_directive_does_not_change_any_content_record_state(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        before = store.get_content_record(rec.id).lifecycle_status
        create_directive(store, ctx, registry, **directive_kwargs(traces_to=[rec.id]))
        self.assertEqual(store.get_content_record(rec.id).lifecycle_status, before)

    def test_awaiting_approval_is_surfaced_not_resolved(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        store.add_material("m1", "m1", "text", "note")
        create_original_content(store, ctx, registry, store.get_material("m1"), "instagram",
                                stub, audience="a", objective="o", cta="c")
        conflicts = detect_conflicts(store, ctx, registry)
        missing = [c for c in conflicts if c["kind"] == "missing_approval"]
        self.assertTrue(missing)
        self.assertTrue(missing[0]["requires_escalation"])


class TestPlanningSourceSelection(unittest.TestCase):
    """§3 — objectives come from the approved planning source; a missing
    source is a conflict, never a silent fallback."""

    def test_missing_planning_source_is_a_conflict(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        # the fixture registry has no planning-sources.yaml
        obj = business_objective(ctx, registry)
        self.assertFalse(obj["planning_source_resolved"])
        kinds = {c["kind"] for c in detect_conflicts(store, ctx, registry)}
        self.assertIn("strategy_plan_mismatch", kinds)

    def test_missing_source_conflict_requires_escalation(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        mismatch = [c for c in detect_conflicts(store, ctx, registry)
                    if c["kind"] == "strategy_plan_mismatch"]
        self.assertTrue(all(c["requires_escalation"] for c in mismatch))

    def test_real_registry_resolves_both_active_brands(self):
        if not (REAL_REGISTRY / "planning-sources.yaml").exists():
            self.skipTest("real harness registry not present in this environment")
        registry = load_registry(REAL_REGISTRY)
        for slug in ("empires", "capstack"):
            obj = business_objective(ctx_for(slug), registry)
            self.assertTrue(obj["planning_source_resolved"], slug)
            self.assertTrue(obj["planning_source"]["updated"], slug)

    def test_pending_proposal_is_not_treated_as_approved(self):
        if not (REAL_REGISTRY / "planning-sources.yaml").exists():
            self.skipTest("real harness registry not present in this environment")
        registry = load_registry(REAL_REGISTRY)
        store, ctx = mem_store(), ctx_for("empires")
        obj = business_objective(ctx, registry)
        self.assertTrue(obj["pending_proposals"])
        self.assertTrue(all(p["status"] == "awaiting_owner_go" for p in obj["pending_proposals"]))
        details = [c["detail"] for c in detect_conflicts(store, ctx, registry)]
        self.assertTrue(any("awaiting your go" in d for d in details))


class TestLearningQueueControls(unittest.TestCase):
    """§7 — review queue only; never a durable memory write."""

    def _finding(self, label="validated_learning", confidence="high"):
        return Finding(label=label, kind="repeated_winner", dimension="pillar", key="X",
                       summary="pillar X wins consistently", confidence=confidence)

    def test_below_threshold_refused(self):
        store = mem_store()
        with self.assertRaises(LearningQueueRejected):
            queue_candidate(store, "capstack", self._finding(label="early_signal", confidence="low"))

    def test_eligible_candidate_queues(self):
        store = mem_store()
        row = queue_candidate(store, "capstack", self._finding())
        self.assertEqual(row["evidence"]["queue_status"], "queued")
        self.assertIn("does NOT write", row["authorization"])

    def test_cross_brand_requires_two_brands(self):
        store = mem_store()
        with self.assertRaises(LearningQueueRejected) as cm:
            queue_candidate(store, "capstack", self._finding(), cross_brand=True,
                            supporting_brands=["capstack"])
        self.assertIn("more than one brand", str(cm.exception))

    def test_cross_brand_accepted_with_two_brands(self):
        store = mem_store()
        row = queue_candidate(store, "capstack", self._finding(), cross_brand=True,
                              supporting_brands=["capstack", "empires"])
        self.assertTrue(row["cross_brand"])
        self.assertEqual(row["brand"], "cross_brand")

    def test_secrets_never_queued(self):
        store = mem_store()
        f = Finding(label="validated_learning", kind="repeated_winner", dimension="pillar",
                    key="X", summary="the api_key rotation improved reach", confidence="high")
        with self.assertRaises(LearningQueueRejected):
            queue_candidate(store, "capstack", f)

    def test_review_requires_reviewer_identity(self):
        store = mem_store()
        row = queue_candidate(store, "capstack", self._finding())
        with self.assertRaises(LearningQueueRejected):
            review_candidate(store, row, decision="approved_for_write", reviewer="")

    def test_approved_for_write_still_writes_nothing_durable(self):
        store = mem_store()
        row = queue_candidate(store, "capstack", self._finding())
        updated = review_candidate(store, row, decision="approved_for_write", reviewer="nick")
        self.assertEqual(updated["evidence"]["queue_status"], "approved_for_write")
        self.assertIn("does NOT write", updated["authorization"])
        # Structural: no filesystem-write machinery of any kind. Checks for
        # actual write CALLS, not for the words "memory/"/"claude-mem" — the
        # module's own docstring names those precisely to say it never
        # touches them, so a naive string check would fail on the disclaimer.
        src = (PKG_DIR / "ceo" / "learning_queue.py").read_text(encoding="utf-8")
        for write_call in ("open(", "write_text", "Path(", "os.", "shutil", "mkdir"):
            self.assertNotIn(write_call, src, f"learning_queue.py uses {write_call!r}")

    def test_queue_listing_is_brand_scoped(self):
        store = mem_store()
        queue_candidate(store, "capstack", self._finding())
        self.assertEqual(len(list_queue(store, "capstack")), 1)
        self.assertEqual(len(list_queue(store, "empires")), 0)


class TestReportSeparation(unittest.TestCase):
    """§6 — brands separate, parked shown as parked, limitations stated."""

    def _sections(self):
        registry = load_test_registry()
        out = []
        for slug in ("empires", "capstack", "stowecap"):
            out.append((ctx_for(slug), mem_store(), registry))
        return out

    def test_weekly_shows_each_brand_separately(self):
        report = build_ceo_weekly(self._sections(), now=NOW)
        self.assertIn("## EMPIRES", report)
        self.assertIn("## CAPSTACK", report)

    def test_weekly_shows_parked_brand_as_parked(self):
        report = build_ceo_weekly(self._sections(), now=NOW)
        self.assertIn("PARKED", report)

    def test_weekly_states_data_limitation(self):
        report = build_ceo_weekly(self._sections(), now=NOW)
        self.assertIn("DATA LIMITATION", report)

    def test_weekly_keeps_manual_import_status_visible(self):
        report = build_ceo_weekly(self._sections(), now=NOW)
        self.assertIn("manual import", report)
        self.assertIn("no analytics connector", report)

    def test_weekly_has_an_owner_decisions_section_per_brand(self):
        report = build_ceo_weekly(self._sections(), now=NOW)
        self.assertGreaterEqual(report.count("OWNER DECISIONS REQUIRED"), 2)

    def test_daily_exceptions_silent_when_nothing_wrong(self):
        registry = load_test_registry()
        # a brand whose only conflict source is the missing planning file
        # still reports it — so use the real registry when available
        if (REAL_REGISTRY / "planning-sources.yaml").exists():
            registry = load_registry(REAL_REGISTRY)
        sections = [(ctx_for("capstack"), mem_store(), registry)]
        report = build_ceo_daily_exceptions(sections, now=NOW)
        self.assertIn("nothing needs you today", report)

    def test_daily_exceptions_excludes_parked_brand(self):
        registry = load_test_registry()
        report = build_ceo_daily_exceptions([(ctx_for("stowecap"), mem_store(), registry)], now=NOW)
        self.assertNotIn("STOWECAP", report)

    def test_reports_never_blend_brands(self):
        report = build_ceo_weekly(self._sections(), now=NOW)
        empires_block = report.split("## EMPIRES")[1].split("##")[0]
        self.assertNotIn("CAPSTACK", empires_block)


class TestNoExternalActionPaths(unittest.TestCase):
    def test_scheduled_task_untouched(self):
        task = Path.home() / ".claude" / "scheduled-tasks" / "empires-egos-daily-review" / "SKILL.md"
        if not task.exists():
            self.skipTest("scheduled task not present")
        self.assertIn("Do NOT post anything", task.read_text(encoding="utf-8"))

    def test_conflict_scan_is_read_only(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        before = [(r.id, r.lifecycle_status) for r in store.list_content_records(ctx.name)]
        detect_conflicts(store, ctx, registry)
        after = [(r.id, r.lifecycle_status) for r in store.list_content_records(ctx.name)]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
