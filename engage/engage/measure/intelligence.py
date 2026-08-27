"""Performance Intelligence Agent (Phase 3). Report-only / read-only: this
module and everything it imports never touches approval/queue.py's write
path, publish/operations.py, publish/sinks.py, or any comment/DM/account
mechanism — there is no import of any of them anywhere in measure/. That
absence is load-bearing, not incidental (see
tests/test_performance_intelligence.py's structural import check).

Ingests performance data (manual import today; internal_event and future
read-only api/mcp sources share the same PerformanceRecord shape) keyed to
the exact ContentRecord and its approved-content hash — never a bare
post_id, and never blended across brands or unrecognized accounts.

Nothing here writes to Claude Mem directly — that's a different system,
written by the agent operating this code, not by this code itself.
learning_candidate() produces a properly-labeled, threshold-gated payload
matching harness/social/LEARNING-POLICY.md's frontmatter shape for a human
(or the calling agent) to review before any durable memory write."""
from __future__ import annotations

import time

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.models import (
    ATTRIBUTION_STATUS_VALUES,
    DATA_COVERAGE_VALUES,
    FINDING_LABELS,
    PERFORMANCE_METRIC_FIELDS,
    PERFORMANCE_SOURCES,
    RECOMMENDATION_TARGETS,
    PerformanceRecord,
    new_id,
)
from ..core.store import Store
from ..drafting.originals import eligible_platforms
from . import kpi
from .diagnosis import Finding


class IngestRejected(Exception):
    """One row was refused — the caller decides whether to continue with
    the rest of the batch or stop."""


def _account_handle(registry: Registry, ctx: BrandContext, platform: str) -> str:
    canonical = registry.canonical_name(ctx.name)
    for a in registry.accounts:
        if a.get("brand") == canonical and a.get("platform") == platform:
            return a.get("handle") or ""
    return ""


def _coverage(key_metrics: list[str], metrics: dict) -> str:
    if not metrics:
        return "none"
    if key_metrics and all(k in metrics for k in key_metrics):
        return "complete"
    return "partial"


def ingest_performance_row(store: Store, ctx: BrandContext, registry: Registry, row: dict) -> PerformanceRecord:
    """One row: {content_record_id, metrics: {...}, source?, attribution_status?,
    campaign?, creative_asset_id?, data_collected_at?}. Everything else
    (brand, platform, account, pillar, format, hook_type, cta, content_hash,
    publish_time) is DERIVED from the live ContentRecord + registry — never
    re-entered, so it can never silently drift from the record it measures.

    Rejects, rather than blends in: a malformed row, a record from a
    different brand, or a platform not currently eligible (disabled,
    unconnected, shared-account, or non-applicable) in the registry."""
    content_record_id = row.get("content_record_id")
    if not content_record_id:
        raise IngestRejected("row has no content_record_id")
    record = store.get_content_record(content_record_id)
    if record is None:
        raise IngestRejected(f"no content record {content_record_id!r}")
    if record.brand != ctx.name:
        raise IngestRejected(
            f"content record {content_record_id} belongs to brand {record.brand!r}, not {ctx.name!r}"
        )
    if registry.is_parked(ctx.name):
        raise IngestRejected(f"'{ctx.name}' is parked in the brand registry — no performance ingest permitted")
    if record.platform not in eligible_platforms(registry, ctx):
        raise IngestRejected(
            f"platform {record.platform!r} is not currently eligible for {ctx.name!r} "
            "(disabled, unconnected, shared-account, or non-applicable) — refusing to blend in this data"
        )

    metrics_in = row.get("metrics") or {}
    unknown_fields = set(metrics_in) - set(PERFORMANCE_METRIC_FIELDS)
    if unknown_fields:
        raise IngestRejected(f"unknown metric field(s): {sorted(unknown_fields)}")
    metrics = {k: float(v) for k, v in metrics_in.items() if v is not None}

    source = row.get("source", "manual_import")
    if source not in PERFORMANCE_SOURCES:
        raise IngestRejected(f"unknown source {source!r} (expected one of {PERFORMANCE_SOURCES})")
    attribution_status = row.get("attribution_status", "unattributed")
    if attribution_status not in ATTRIBUTION_STATUS_VALUES:
        raise IngestRejected(f"unknown attribution_status {attribution_status!r}")

    brief = record.brief or {}
    kpi_conf = kpi.kpi_config(ctx)
    perf = PerformanceRecord(
        id=new_id(), brand=ctx.name, content_record_id=record.id,
        content_hash=(store.get_approval_hash(record.draft_id) or "") if record.draft_id else "",
        account=_account_handle(registry, ctx, record.platform), platform=record.platform,
        campaign=row.get("campaign"), pillar=record.pillar, format=brief.get("format", ""),
        hook_type=brief.get("hook", "")[:40], cta=brief.get("cta", ""),
        creative_asset_id=row.get("creative_asset_id"), publish_time=record.published_at,
        metrics=metrics, data_coverage=_coverage(kpi_conf.get("key_metrics", []), metrics),
        attribution_status=attribution_status, source=source,
        data_collected_at=row.get("data_collected_at", int(time.time())),
    )
    store.save_performance_record(perf)
    return perf


def ingest_performance_batch(store: Store, ctx: BrandContext, registry: Registry,
                             rows: list[dict]) -> tuple[list[PerformanceRecord], list[dict]]:
    """Never lets one bad row poison the batch: accepted rows are ingested,
    rejected rows are reported with why — never silently dropped, never
    silently blended in."""
    accepted, rejected = [], []
    for row in rows:
        try:
            accepted.append(ingest_performance_row(store, ctx, registry, row))
        except IngestRejected as e:
            rejected.append({"row": row, "reason": str(e)})
    return accepted, rejected


MIN_CONFIDENCE_FOR_LEARNING = ("medium", "high")


def is_learning_write_eligible(finding: Finding) -> bool:
    """The ONLY gate for whether a finding may become a durable-memory
    candidate: validated_learning label AND at least medium confidence.
    Nothing at 'observation'/'hypothesis'/'early_signal' ever qualifies,
    regardless of how compelling it looks."""
    return finding.label == "validated_learning" and finding.confidence in MIN_CONFIDENCE_FOR_LEARNING


def learning_candidate(finding: Finding, registry: Registry, ctx: BrandContext,
                       cross_brand: bool = False) -> dict | None:
    """A ready-to-review payload matching harness/social/LEARNING-POLICY.md's
    frontmatter shape — NOT a memory write. This module never writes to
    ~/.claude/projects/*/memory/ or any claude-mem interface; that happens
    (if at all) as a deliberate act by whoever is operating this code,
    after reviewing exactly this payload. Returns None when the finding
    doesn't meet the threshold — never a "weak" candidate."""
    if not is_learning_write_eligible(finding):
        return None
    return {
        "brand": "cross_brand" if cross_brand else registry.canonical_name(ctx.name),
        "cross_brand": cross_brand,
        "summary": finding.summary,
        "evidence": finding.evidence,
        "confidence": finding.confidence,
        "kind": finding.kind,
        "dimension": finding.dimension,
        "key": finding.key,
    }


def cross_brand_learning_candidate(findings_by_brand: dict[str, Finding], registry: Registry) -> dict | None:
    """Cross-brand learning requires evidence from MORE THAN ONE brand
    showing the same kind+dimension+key pattern, each already meeting the
    single-brand threshold — never asserted from one brand's data with a
    cross_brand flag slapped on."""
    qualifying = {b: f for b, f in findings_by_brand.items() if f and is_learning_write_eligible(f)}
    if len(qualifying) < 2:
        return None
    kinds = {f.kind for f in qualifying.values()}
    dims = {f.dimension for f in qualifying.values()}
    keys = {f.key for f in qualifying.values()}
    if len(kinds) != 1 or len(dims) != 1 or len(keys) != 1:
        return None  # different patterns per brand — not one cross-brand learning
    any_finding = next(iter(qualifying.values()))
    return {
        "brand": "cross_brand", "cross_brand": True,
        "summary": f"Consistent across {sorted(qualifying)}: {any_finding.summary}",
        "evidence": {b: f.evidence for b, f in qualifying.items()},
        "confidence": "high" if all(f.confidence == "high" for f in qualifying.values()) else "medium",
        "kind": any_finding.kind, "dimension": any_finding.dimension, "key": any_finding.key,
    }


def write_recommendation(store: Store, brand: str, target: str, label: str, summary: str,
                         evidence: dict, confidence: str, cross_brand: bool = False) -> dict:
    """Pattern-level recommendation (not tied to one ContentRecord) — a
    sibling to drafting.originals.write_handoff(), not a duplicate: that
    function requires a specific record and this module's findings usually
    span several. Same non-binding discipline: explicit authorization text,
    an append-only record, never an instruction to act."""
    if target not in RECOMMENDATION_TARGETS:
        raise ValueError(f"unknown recommendation target {target!r} (expected one of {RECOMMENDATION_TARGETS})")
    if label not in FINDING_LABELS:
        raise ValueError(f"unknown label {label!r}")
    rec = {
        "id": new_id(), "brand": brand, "target": target, "label": label, "summary": summary,
        "evidence": evidence, "confidence": confidence, "cross_brand": cross_brand,
        "authorization": (
            "NONE — this is a non-binding recommendation for a human or another agent to "
            "evaluate. It does not authorize publishing, scheduling, commenting, messaging, "
            "spending money, or changing any account setting."
        ),
        "created_at": int(time.time()),
    }
    store.save_recommendation(rec)
    return rec
