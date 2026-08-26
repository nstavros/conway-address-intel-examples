"""CEO executive reports. Brands rendered in separate sections, never
blended — the same convention report/digest.py and measure/reports.py already
use. A brand marked parked in the registry always renders as parked.

The weekly report's last section is deliberately the ONLY place owner
decisions appear: everything above it is context the CEO already handled."""
from __future__ import annotations

import time

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.store import (
    RECONCILIATION_INCONSISTENT,
    RECONCILIATION_OPEN,
    Store,
)
from ..engagement import live_capability
from ..measure import kpi
from . import agent as ceo_agent

WEEK = 7 * 86400


def _planning_line(ctx: BrandContext, registry: Registry) -> list[str]:
    obj = ceo_agent.business_objective(ctx, registry)
    lines = []
    if obj["planning_source_resolved"]:
        ps = obj["planning_source"]
        lines.append(f"Planning source: {ps['title']} (updated {ps['updated']})")
        lines.append(f"  location: {ps['location']}")
    else:
        lines.append("Planning source: **NONE RECORDED** — strategy cannot be verified. Escalated.")
    lines.append(f"Business objective: {obj['business_objective'] or '(not configured)'}")
    if obj["targets"]:
        lines.append(f"Targets: {obj['targets']}")
    for gate in obj["decision_gates"]:
        lines.append(f"  gate: {gate}")
    for p in obj["pending_proposals"]:
        lines.append(f"  PENDING PROPOSAL (not authoritative): {p['title']} [{p['status']}]")
    return lines


def _external_automation_lines(ctx: BrandContext, registry: Registry) -> list[str]:
    """External automation coverage (ChatPlace) — a real, externally-run
    system this repo has no write path to, surfaced here for visibility
    only. Never conflate with public comment-posting: every binding's
    action_type is comment_triggered_dm, enforced at snapshot-load time in
    engagement/live_capability.py. A missing/unreadable snapshot degrades to
    'not recorded', never a crash — this section is visibility, not a gate."""
    try:
        verified_at, bindings = live_capability.load_and_verify(registry)
    except live_capability.SnapshotError:
        return ["External automation coverage (ChatPlace): not recorded (no snapshot found)."]
    mine = [b for b in bindings if b.engage_slug == ctx.name]
    if not mine:
        return [f"External automation coverage (ChatPlace): none configured (snapshot as of {verified_at})."]
    lines = [f"External automation coverage (ChatPlace, snapshot verified {verified_at}):"]
    for b in mine:
        id_flag = "verified" if b.identity_verified else f"UNVERIFIED — {b.verification_reason}"
        lines.append(
            f"  - {b.platform}/{b.bot_username}: \"{b.automation_name}\" [{b.status}] "
            f"— {b.action_type} (identity: {id_flag}); "
            f"{b.total_clients} clients / {b.total_conversions} conversions / "
            f"last run {b.last_run_at or 'never'}"
        )
    return lines


def _brand_section(ctx: BrandContext, store: Store, registry: Registry, now: int) -> list[str]:
    canonical = registry.canonical_name(ctx.name)
    lines = [f"\n## {ctx.name.upper()} ({canonical})"]
    if registry.is_parked(ctx.name):
        lines.append("PARKED — excluded from planning, reporting, and delegation by design.")
        return lines

    lines.extend(_planning_line(ctx, registry))

    records = store.list_content_records(ctx.name)
    approved = [r for r in records if r.lifecycle_status == "approved"]
    awaiting = [r for r in records if r.lifecycle_status == "review_required"]
    scheduled = [r for r in records if r.lifecycle_status == "scheduled"]
    published = [r for r in records if r.lifecycle_status in ("published", "verified")]
    failed = [r for r in records if r.lifecycle_status == "failed"]
    paused = [r for r in records if r.lifecycle_status == "paused"]
    # Records with an unresolved remote-upload outcome are NOT healthy
    # in-flight work and are excluded from that count.
    recon = store.list_records_requiring_reconciliation(ctx.name)
    recon_ids = {s.record_id for s in recon}
    in_flight = [r for r in records
                 if r.lifecycle_status == "publishing" and r.id not in recon_ids]

    lines.append(f"Approved-content inventory: {len(approved)} approved and ready, "
                 f"{len(scheduled)} scheduled (dry-run).")
    lines.append(f"Draft/approval queue: {len(awaiting)} awaiting your approval.")
    total_attempts = len(published) + len(failed)
    rate = f"{100 * len(published) / total_attempts:.0f}%" if total_attempts else "n/a"
    lines.append(f"Dry-run publishing readiness: {len(published)} published/verified, "
                 f"{len(failed)} failed ({rate} success), {len(paused)} paused. "
                 "All publishing is dry-run; no live sink exists.")

    perf = store.list_performance_records(ctx.name)
    measured = {p.content_record_id for p in perf}
    unmeasured = [r for r in published if r.id not in measured]
    lines.append(f"Performance/data coverage: {len(perf)} row(s) on record; "
                 f"{len(unmeasured)} published item(s) unmeasured. "
                 "Source: manual import + internal events only (no analytics connector).")
    if not perf:
        lines.append("  DATA LIMITATION: no performance data yet — nothing below is evidence-backed.")

    recs = [r for r in store.list_recommendations(ctx.name)
            if not r["summary"].startswith(("[CEO DIRECTIVE]", "[OWNER ESCALATION]", "[LEARNING CANDIDATE]"))]
    if recs:
        lines.append("Top improvement recommendations:")
        for r in recs[:3]:
            lines.append(f"  - [{r['label']}, {r['confidence']}] {r['summary'][:110]}")
    else:
        lines.append("Top improvement recommendations: none yet (insufficient data).")

    proposed = store.list_experiments(ctx.name, status="proposed")
    if proposed:
        lines.append(f"Proposed experiments ({len(proposed)}, awaiting approval):")
        for e in proposed[:3]:
            lines.append(f"  - {e.hypothesis[:100]}")
    else:
        lines.append("Proposed experiments: none.")

    conflicts = ceo_agent.detect_conflicts(store, ctx, registry)
    risks = [c for c in conflicts if not c["requires_escalation"]]
    if risks:
        lines.append("Risks:")
        for c in risks[:4]:
            lines.append(f"  - [{c['kind']}] {c['detail']}")
    else:
        lines.append("Risks: none detected.")

    open_recon = [x for x in recon if x.category == RECONCILIATION_OPEN]
    inconsistent = [x for x in recon if x.category == RECONCILIATION_INCONSISTENT]
    if open_recon:
        lines.append(f"UNRESOLVED REMOTE-UPLOAD OUTCOMES ({len(open_recon)}) — "
                     "manual reconciliation required, NOT in-flight work:")
        for x in open_recon:
            lines.append(f"  ! {x.record_id}: outcome unresolved; a remote object may "
                         "exist. All further actions blocked until reconciled.")
    if inconsistent:
        lines.append(f"URGENT — UNRESOLVED OUTCOME OUTSIDE PARKING STATE ({len(inconsistent)}):")
        for x in inconsistent:
            lines.append(f"  !! {x.record_id}: unresolved remote-upload outcome while "
                         f"lifecycle is {x.lifecycle_status!r}. A remote object may exist "
                         "unaccounted for. Not resolvable by normal operations — forensic "
                         "repair only.")

    lines.extend(_external_automation_lines(ctx, registry))

    owner_items = [c for c in conflicts if c["requires_escalation"]]
    lines.append("")
    if owner_items:
        lines.append(f"OWNER DECISIONS REQUIRED ({len(owner_items)}):")
        for c in owner_items:
            lines.append(f"  ! [{c['kind']}] {c['detail']}")
    else:
        lines.append("OWNER DECISIONS REQUIRED: none.")
    return lines


def build_ceo_weekly(sections: list[tuple[BrandContext, Store, Registry]], now: int | None = None) -> str:
    now = now if now is not None else int(time.time())
    lines = ["SOCIAL MEDIA CEO — WEEKLY REPORT", "=" * 44,
             "Mode: planning / read-only. No live publishing, commenting, DMs, or "
             "account connections exist anywhere in this system."]
    for ctx, store, registry in sections:
        lines.extend(_brand_section(ctx, store, registry, now))
    return "\n".join(lines)


def build_ceo_daily_exceptions(sections: list[tuple[BrandContext, Store, Registry]],
                              now: int | None = None) -> str:
    """Only action-worthy items. A brand with nothing wrong produces no
    section — silence is the signal that nothing needs you."""
    now = now if now is not None else int(time.time())
    body: list[str] = []
    for ctx, store, registry in sections:
        if registry.is_parked(ctx.name):
            continue
        conflicts = [c for c in ceo_agent.detect_conflicts(store, ctx, registry)
                     if c["requires_escalation"]]
        recon = store.list_records_requiring_reconciliation(ctx.name)
        if not conflicts and not recon:
            continue
        body.append(f"\n## {ctx.name.upper()} ({registry.canonical_name(ctx.name)})")
        # No age threshold — an unresolved outcome may correspond to a live
        # public object from the moment it is recorded.
        for x in recon:
            if x.category == RECONCILIATION_INCONSISTENT:
                body.append(f"  !! [unresolved_outcome_outside_parking_state] {x.record_id}: "
                            f"lifecycle {x.lifecycle_status!r}. URGENT — forensic repair "
                            "required; all actions blocked.")
            else:
                body.append(f"  ! [unresolved_remote_upload_outcome] {x.record_id}: manual "
                            "reconciliation required; all actions blocked.")
        for c in conflicts:
            body.append(f"  ! [{c['kind']}] {c['detail']}")
    header = "SOCIAL MEDIA CEO — DAILY EXCEPTIONS\n" + "=" * 44
    if not body:
        return header + "\n(nothing needs you today)"
    return header + "\n" + "\n".join(body)
