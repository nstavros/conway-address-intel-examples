"""Performance Intelligence Agent (Phase 3) — report-only/read-only.
Synthetic fixtures only; no live social data. Every test runs offline
against the fixture registry, in-memory stores, and hand-built performance
rows — no network, no browser, no credentials, no write to any
publishing/comment/DM/account mechanism anywhere."""
import tempfile
import unittest
from pathlib import Path

from engage.config.registry import load_registry
from engage.core.models import EXPERIMENT_STATUSES, Experiment, PerformanceRecord, new_id
from engage.drafting.originals import approve_content_record, create_original_content
from engage.measure import diagnosis, kpi, reports
from engage.measure.diagnosis import Finding, label_finding
from engage.measure.experiments import (
    ExperimentValidationError,
    approve_experiment,
    create_experiment,
    evaluate_experiment,
    validate_experiment,
)
from engage.measure.intelligence import (
    IngestRejected,
    cross_brand_learning_candidate,
    ingest_performance_batch,
    ingest_performance_row,
    is_learning_write_eligible,
    learning_candidate,
    write_recommendation,
)

from .helpers import PKG_DIR, ROOT, ctx_for, mem_store
from .test_originals import FIXTURES, load_test_registry

REGISTRY_DIR = FIXTURES / "registry"
NOW = 1_700_000_000


def stub(prompt: str) -> str:
    return "PASS" if "'PASS' or 'BLOCK" in prompt else "A hook.\nCaption body."


def approved_record(store, ctx, registry, material_id, text, platform="instagram"):
    store.add_material(material_id, material_id, text, "note")
    material = store.get_material(material_id)
    record = create_original_content(store, ctx, registry, material, platform, stub,
                                     audience="a", objective="o", cta="c")
    return approve_content_record(store, ctx, record, approver="nick")


def perf_row(content_record_id, **metrics):
    return {"content_record_id": content_record_id, "metrics": metrics}


def make_perf(brand="capstack", platform="instagram", pillar="The Way In", **metrics):
    """A standalone PerformanceRecord for pure diagnosis-function tests
    that don't need a real ContentRecord behind them."""
    return PerformanceRecord(id=new_id(), brand=brand, content_record_id=new_id(),
                             content_hash="h", platform=platform, pillar=pillar,
                             metrics=metrics, data_coverage="complete" if metrics else "none")


class TestIngestIsolation(unittest.TestCase):
    """§1 — reject malformed, cross-brand, disabled, parked, shared, and
    unconnected data rather than blending it into reports."""

    def test_rejects_missing_content_record_id(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        with self.assertRaises(IngestRejected):
            ingest_performance_row(store, ctx, registry, {"metrics": {"views": 100}})

    def test_rejects_unknown_content_record(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        with self.assertRaises(IngestRejected):
            ingest_performance_row(store, ctx, registry, perf_row("no-such-id", views=100))

    def test_rejects_cross_brand_content_record(self):
        store = mem_store()
        empires_ctx = ctx_for("empires")
        capstack_ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, empires_ctx, registry, "m1", "text", platform="instagram")
        with self.assertRaises(IngestRejected) as cm:
            ingest_performance_row(store, capstack_ctx, registry, perf_row(record.id, views=100))
        self.assertIn("belongs to brand", str(cm.exception))

    def test_rejects_disabled_platform(self):
        # tiktok drafted under a permissive registry, ingest attempted
        # under the real (strict) one — proves independent re-validation.
        store = mem_store()
        ctx = ctx_for("capstack")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "brands.yaml").write_text(
                'brands:\n  - canonical: "Capstacknick"\n    engage_slug: capstack\n    status: active\n',
                encoding="utf-8")
            (tmp / "accounts.yaml").write_text(
                'accounts:\n  - brand: "Capstacknick"\n    platform: tiktok\n    enabled: true\n',
                encoding="utf-8")
            permissive = load_registry(tmp)
            record = approved_record(store, ctx, permissive, "m1", "text", platform="tiktok")
        strict = load_test_registry()
        with self.assertRaises(IngestRejected) as cm:
            ingest_performance_row(store, ctx, strict, perf_row(record.id, views=100))
        self.assertIn("not currently eligible", str(cm.exception))

    def test_rejects_parked_brand(self):
        from engage.approval.queue import approve as approve_draft
        from engage.approval.queue import submit
        from engage.core.models import ContentRecord, Draft
        store = mem_store()
        ctx = ctx_for("stowecap")
        registry = load_test_registry()
        draft = Draft(id=new_id(), brand=ctx.name, kind="original", platform="linkedin",
                      text="a clean sentence")
        draft = submit(store, ctx, draft, (), lambda p: "PASS")
        approve_draft(store, ctx, draft.id)
        record = ContentRecord(id=new_id(), brand=ctx.name, platform="linkedin", kind="original",
                               draft_id=draft.id, lifecycle_status="approved")
        store.save_content_record(record)
        with self.assertRaises(IngestRejected) as cm:
            ingest_performance_row(store, ctx, registry, perf_row(record.id, views=100))
        self.assertIn("parked", str(cm.exception))

    def test_rejects_shared_account(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "brands.yaml").write_text(
                'brands:\n  - canonical: "Capstacknick"\n    engage_slug: capstack\n    status: active\n',
                encoding="utf-8")
            (tmp / "accounts.yaml").write_text(
                'accounts:\n  - brand: "Capstacknick"\n    platform: x\n    enabled: true\n',
                encoding="utf-8")
            permissive = load_registry(tmp)
            record = approved_record(store, ctx, permissive, "m1", "text", platform="x")
        with tempfile.TemporaryDirectory() as d2:
            tmp2 = Path(d2)
            (tmp2 / "brands.yaml").write_text(
                'brands:\n  - canonical: "Capstacknick"\n    engage_slug: capstack\n    status: active\n',
                encoding="utf-8")
            (tmp2 / "accounts.yaml").write_text(
                'accounts:\n  - brand: "Capstacknick"\n    platform: x\n    enabled: true\n'
                '    shared_account: true\n', encoding="utf-8")
            now_shared = load_registry(tmp2)
            with self.assertRaises(IngestRejected):
                ingest_performance_row(store, ctx, now_shared, perf_row(record.id, views=100))

    def test_rejects_unknown_metric_field(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text")
        with self.assertRaises(IngestRejected):
            ingest_performance_row(store, ctx, registry, perf_row(record.id, made_up_metric=1))

    def test_batch_continues_after_one_bad_row(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text")
        rows = [perf_row("no-such-id", views=1), perf_row(record.id, views=100)]
        accepted, rejected = ingest_performance_batch(store, ctx, registry, rows)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)


class TestMissingDataHandling(unittest.TestCase):
    """§2 — missing data is never interpreted as poor performance."""

    def test_no_metrics_is_coverage_none(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text")
        perf = ingest_performance_row(store, ctx, registry, {"content_record_id": record.id, "metrics": {}})
        self.assertEqual(perf.data_coverage, "none")

    def test_partial_metrics_is_partial_coverage(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text")
        perf = ingest_performance_row(store, ctx, registry, perf_row(record.id, views=100))
        self.assertEqual(perf.data_coverage, "partial")  # capstack's key_metrics need more than views

    def test_complete_when_all_key_metrics_present(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text")
        key_metrics = kpi.kpi_config(ctx)["key_metrics"]
        row = perf_row(record.id, **{m: 10 for m in key_metrics})
        perf = ingest_performance_row(store, ctx, registry, row)
        self.assertEqual(perf.data_coverage, "complete")

    def test_missing_baseline_never_produces_a_finding(self):
        # a single performance record can never establish a baseline —
        # no "underperformer" finding should ever be fabricated from it
        records = [make_perf(views=1, reach=1)]
        self.assertEqual(diagnosis.repeated_underperformers(records, "pillar"), [])
        self.assertEqual(diagnosis.repeated_winners(records, "pillar"), [])


class TestNormalization(unittest.TestCase):
    """§3 — normalized metrics, never fabricated from a missing/zero denominator."""

    def test_per_1000_none_when_denominator_missing(self):
        self.assertIsNone(kpi.per_1000(10, None))

    def test_per_1000_none_when_denominator_zero(self):
        self.assertIsNone(kpi.per_1000(10, 0))

    def test_engagement_per_1000_reach_computes(self):
        m = {"likes": 20, "comments": 5, "shares": 5, "saves": 10, "reach": 1000}
        self.assertEqual(kpi.engagement_per_1000_reach(m), 40.0)

    def test_conversion_none_when_numerator_missing(self):
        self.assertIsNone(kpi.profile_visit_to_lead_conversion({"profile_visits": 100}))

    def test_conversion_none_when_denominator_zero(self):
        self.assertIsNone(kpi.click_to_qualified_lead_conversion({"link_clicks": 0, "qualified_leads": 3}))

    def test_normalize_returns_all_seven_keys(self):
        result = kpi.normalize({"reach": 1000, "likes": 10})
        self.assertEqual(set(result), set(kpi.NORMALIZED_METRICS))


class TestKPIConfig(unittest.TestCase):
    """§3 — brand-specific KPI configuration; business objectives required."""

    def test_empires_objective_matches_its_stated_goal(self):
        ctx = ctx_for("empires")
        conf = kpi.kpi_config(ctx)
        self.assertEqual(conf["business_objective"], "raw_growth")

    def test_capstack_objective_matches_its_stated_goal(self):
        ctx = ctx_for("capstack")
        conf = kpi.kpi_config(ctx)
        self.assertEqual(conf["business_objective"], "lead_generation_and_sales")

    def test_missing_kpi_config_is_honestly_empty_not_fabricated(self):
        ctx = ctx_for("stowecap")
        conf = kpi.kpi_config(ctx)
        self.assertEqual(conf["business_objective"], "")


class TestFindingLabeling(unittest.TestCase):
    """§4 — observation/hypothesis/early_signal/validated_learning/insufficient_data."""

    def test_zero_is_insufficient_data(self):
        self.assertEqual(label_finding(0, consistent=True), "insufficient_data")

    def test_one_is_observation(self):
        self.assertEqual(label_finding(1, consistent=True), "observation")

    def test_below_threshold_mixed_is_hypothesis(self):
        self.assertEqual(label_finding(2, consistent=False, threshold=3), "hypothesis")

    def test_below_threshold_consistent_is_early_signal(self):
        self.assertEqual(label_finding(2, consistent=True, threshold=3), "early_signal")

    def test_at_threshold_consistent_is_validated_learning(self):
        self.assertEqual(label_finding(3, consistent=True, threshold=3), "validated_learning")

    def test_at_threshold_mixed_stays_hypothesis(self):
        self.assertEqual(label_finding(3, consistent=False, threshold=3), "hypothesis")

    def test_never_declares_winner_on_views_alone(self):
        # high views, but qualified_leads stays at/below baseline — must
        # NOT be flagged a repeated winner despite the awareness spike
        records = [
            make_perf(pillar="The Way In", views=100, reach=1000, qualified_leads=1),
            make_perf(pillar="The Way In", views=100, reach=1000, qualified_leads=1),
            make_perf(pillar="The Reality", views=9000, reach=1000, qualified_leads=0),
            make_perf(pillar="The Reality", views=9000, reach=1000, qualified_leads=0),
        ]
        winners = diagnosis.repeated_winners(records, "pillar", awareness_metric="views",
                                             intent_metric="qualified_leads")
        winning_keys = {f.key for f in winners}
        self.assertNotIn("The Reality", winning_keys)


class TestExperimentValidation(unittest.TestCase):
    """§5 — required fields, single-brand targeting, ineligible-account rejection."""

    def test_valid_experiment_creates(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        exp = create_experiment(
            store, ctx, registry, business_objective="lead_generation_and_sales",
            hypothesis="A concrete-case-open hook increases qualified leads",
            independent_variable="hook style", control="abstract opening",
            treatment="concrete case opening", target_platform="instagram",
            success_metric="qualified_leads_per_1000_reach",
            decision_rule="treatment must exceed control's median by >=20% across >=3 posts",
        )
        self.assertEqual(exp.status, "proposed")

    def test_rejects_missing_business_objective(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        with self.assertRaises(ExperimentValidationError):
            create_experiment(
                store, ctx, registry, business_objective="", hypothesis="h",
                independent_variable="iv", control="c", treatment="t",
                target_platform="instagram", success_metric="m", decision_rule="d",
            )

    def test_rejects_missing_decision_rule(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        with self.assertRaises(ExperimentValidationError):
            create_experiment(
                store, ctx, registry, business_objective="o", hypothesis="h",
                independent_variable="iv", control="c", treatment="t",
                target_platform="instagram", success_metric="m", decision_rule="",
            )

    def test_rejects_brand_mismatch(self):
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        exp = Experiment(id=new_id(), brand="empires", business_objective="o", hypothesis="h",
                         independent_variable="iv", control="c", treatment="t",
                         target_platform="instagram", success_metric="m", decision_rule="d")
        problems = validate_experiment(exp, registry, ctx)
        self.assertTrue(any("does not match ctx" in p for p in problems))

    def test_rejects_parked_target(self):
        ctx = ctx_for("stowecap")
        registry = load_test_registry()
        exp = Experiment(id=new_id(), brand="stowecap", business_objective="o", hypothesis="h",
                         independent_variable="iv", control="c", treatment="t",
                         target_platform="linkedin", success_metric="m", decision_rule="d")
        problems = validate_experiment(exp, registry, ctx)
        self.assertTrue(any("parked" in p for p in problems))

    def test_rejects_disabled_platform_target(self):
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        exp = Experiment(id=new_id(), brand="capstack", business_objective="o", hypothesis="h",
                         independent_variable="iv", control="c", treatment="t",
                         target_platform="tiktok", success_metric="m", decision_rule="d")
        problems = validate_experiment(exp, registry, ctx)
        self.assertTrue(any("not eligible" in p for p in problems))

    def test_experiment_lifecycle_approve_and_evaluate(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        exp = create_experiment(
            store, ctx, registry, business_objective="o", hypothesis="h", independent_variable="iv",
            control="c", treatment="t", target_platform="instagram", success_metric="m",
            decision_rule="d",
        )
        exp = approve_experiment(store, exp, approver="nick")
        self.assertEqual(exp.status, "approved")
        exp = evaluate_experiment(store, exp, {"conclusion": "no significant difference"})
        self.assertEqual(exp.status, "evaluated")
        self.assertIn("decided_at", exp.outcome)

    def test_experiment_never_touches_content_records(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        before = len(store.list_content_records(ctx.name))
        create_experiment(
            store, ctx, registry, business_objective="o", hypothesis="h", independent_variable="iv",
            control="c", treatment="t", target_platform="instagram", success_metric="m",
            decision_rule="d",
        )
        self.assertEqual(len(store.list_content_records(ctx.name)), before)


class TestCrossBrandLearningControls(unittest.TestCase):
    """§6 — brand-scoped by default; cross_brand only with evidence from
    more than one brand and only above the write-eligibility threshold."""

    def test_below_threshold_never_eligible(self):
        f = Finding(label="hypothesis", kind="repeated_winner", dimension="pillar", key="X",
                   summary="s", confidence="low")
        self.assertFalse(is_learning_write_eligible(f))

    def test_validated_learning_with_confidence_is_eligible(self):
        f = Finding(label="validated_learning", kind="repeated_winner", dimension="pillar", key="X",
                   summary="s", confidence="high")
        self.assertTrue(is_learning_write_eligible(f))

    def test_learning_candidate_none_below_threshold(self):
        registry = load_test_registry()
        ctx = ctx_for("capstack")
        f = Finding(label="early_signal", kind="repeated_winner", dimension="pillar", key="X",
                   summary="s", confidence="medium")
        self.assertIsNone(learning_candidate(f, registry, ctx))

    def test_learning_candidate_present_when_eligible(self):
        registry = load_test_registry()
        ctx = ctx_for("capstack")
        f = Finding(label="validated_learning", kind="repeated_winner", dimension="pillar", key="X",
                   summary="s", confidence="high")
        candidate = learning_candidate(f, registry, ctx)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["brand"], "Capstacknick")
        self.assertFalse(candidate["cross_brand"])

    def test_single_brand_finding_not_cross_brand_eligible(self):
        registry = load_test_registry()
        f = Finding(label="validated_learning", kind="repeated_winner", dimension="pillar", key="X",
                   summary="s", confidence="high")
        result = cross_brand_learning_candidate({"empires": f, "capstack": None}, registry)
        self.assertIsNone(result)

    def test_two_brands_same_pattern_is_cross_brand_eligible(self):
        registry = load_test_registry()
        f1 = Finding(label="validated_learning", kind="repeated_winner", dimension="pillar", key="X",
                    summary="empires says X wins", confidence="high")
        f2 = Finding(label="validated_learning", kind="repeated_winner", dimension="pillar", key="X",
                    summary="capstack says X wins", confidence="medium")
        result = cross_brand_learning_candidate({"empires": f1, "capstack": f2}, registry)
        self.assertIsNotNone(result)
        self.assertTrue(result["cross_brand"])
        self.assertEqual(result["brand"], "cross_brand")

    def test_two_brands_different_pattern_not_cross_brand_eligible(self):
        registry = load_test_registry()
        f1 = Finding(label="validated_learning", kind="repeated_winner", dimension="pillar", key="X",
                    summary="s1", confidence="high")
        f2 = Finding(label="validated_learning", kind="repeated_winner", dimension="format", key="Y",
                    summary="s2", confidence="high")
        result = cross_brand_learning_candidate({"empires": f1, "capstack": f2}, registry)
        self.assertIsNone(result)

    def test_write_recommendation_default_is_brand_scoped(self):
        store = mem_store()
        rec = write_recommendation(store, "capstack", "drafting", "hypothesis", "summary", {}, "low")
        self.assertFalse(rec["cross_brand"])
        self.assertIn("NONE", rec["authorization"])
        self.assertIn("does not authorize", rec["authorization"])


class TestReadOnlyNoExternalAction(unittest.TestCase):
    """§1/§8 — the agent has no write-capable publishing, commenting,
    messaging, browser, credential, or account-management access."""

    def test_measure_package_imports_no_write_capable_module(self):
        forbidden_imports = ("from ..publish", "from ..approval.queue import approve",
                             "from ..approval.queue import submit", "webbrowser", "requests.",
                             "urllib.request", "selenium", "playwright")
        for f in ("kpi.py", "diagnosis.py", "experiments.py", "intelligence.py", "reports.py"):
            src = (PKG_DIR / "measure" / f).read_text(encoding="utf-8")
            for forbidden in forbidden_imports:
                self.assertNotIn(forbidden, src, f"{f} contains forbidden reference {forbidden!r}")

    def test_measure_package_never_calls_content_record_write_paths(self):
        forbidden_calls = ("save_content_record", "save_draft", "record_approval", "set_draft_status")
        for f in ("kpi.py", "diagnosis.py", "experiments.py", "intelligence.py", "reports.py"):
            src = (PKG_DIR / "measure" / f).read_text(encoding="utf-8")
            for forbidden in forbidden_calls:
                self.assertNotIn(forbidden, src, f"{f} calls forbidden write path {forbidden!r}")

    def test_no_credential_or_secret_handling_anywhere(self):
        forbidden = ("password", "credential", "oauth_token", "api_key", "cookie", "mfa_code")
        for f in ("kpi.py", "diagnosis.py", "experiments.py", "intelligence.py", "reports.py"):
            src = (PKG_DIR / "measure" / f).read_text(encoding="utf-8").lower()
            for term in forbidden:
                self.assertNotIn(term, src, f"{f} references {term!r}")


class TestReportRendering(unittest.TestCase):
    """§7 — weekly and daily-exceptions reports, brands shown separately."""

    def test_weekly_report_shows_brands_separately(self):
        store_e = mem_store()
        ctx_e = ctx_for("empires")
        store_c = mem_store()
        ctx_c = ctx_for("capstack")
        registry = load_test_registry()
        report = reports.build_weekly_report([(ctx_e, store_e, registry), (ctx_c, store_c, registry)], now=NOW)
        self.assertIn("## EMPIRES", report)
        self.assertIn("## CAPSTACK", report)
        self.assertIn("raw_growth", report)
        self.assertIn("lead_generation_and_sales", report)

    def test_weekly_report_flags_parked_brand(self):
        store = mem_store()
        ctx = ctx_for("stowecap")
        registry = load_test_registry()
        report = reports.build_weekly_report([(ctx, store, registry)], now=NOW)
        self.assertIn("PARKED", report)

    def test_weekly_report_states_insufficient_data_explicitly(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        report = reports.build_weekly_report([(ctx, store, registry)], now=NOW)
        self.assertIn("INSUFFICIENT DATA", report)

    def test_daily_exceptions_empty_when_nothing_wrong(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        report = reports.build_daily_exceptions([(ctx, store, registry)], now=NOW)
        self.assertIn("nothing action-worthy", report)

    def test_daily_exceptions_surfaces_missing_data(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        record = approved_record(store, ctx, registry, "m1", "text")
        ingest_performance_row(store, ctx, registry, {"content_record_id": record.id, "metrics": {},
                                                       "data_collected_at": NOW})
        report = reports.build_daily_exceptions([(ctx, store, registry)], now=NOW)
        self.assertIn("MISSING DATA", report)

    def test_daily_exceptions_excludes_parked_brand(self):
        store = mem_store()
        ctx = ctx_for("stowecap")
        registry = load_test_registry()
        report = reports.build_daily_exceptions([(ctx, store, registry)], now=NOW)
        self.assertNotIn("STOWECAP", report)


if __name__ == "__main__":
    unittest.main()
