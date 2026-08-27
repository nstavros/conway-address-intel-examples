"""Weekly and daily-exceptions reports for Performance Intelligence.
Multi-brand sections rendered separately, never blended — the same
pattern report/digest.py already established for the daily engagement
digest (`## {ctx.name.upper()}` per brand, one report call spans brands,
each brand's data stays in its own section)."""
from __future__ import annotations

import time

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.store import Store
from . import diagnosis, kpi

WEEK = 7 * 86400
DAY = 86400


def _pct(n: int, total: int) -> str:
    return f"{(100 * n / total):.0f}%" if total else "n/a"


def _brand_weekly_section(ctx: BrandContext, store: Store, registry: Registry, now: int) -> list[str]:
    lines = [f"\n## {ctx.name.upper()} ({registry.canonical_name(ctx.name)})"]
    if registry.is_parked(ctx.name):
        lines.append("PARKED — excluded from analysis by design (see harness/social/registry).")
        return lines

    kpi_conf = kpi.kpi_config(ctx)
    lines.append(f"Business objective: {kpi_conf.get('business_objective') or '(not configured — see brand config kpi: block)'}")
    lines.append(f"Key metrics: {', '.join(kpi_conf.get('key_metrics', [])) or '(none configured)'}")

    week_ago = now - WEEK
    perf = [r for r in store.list_performance_records(ctx.name)
           if r.data_collected_at and r.data_collected_at >= week_ago]
    content = store.list_content_records(ctx.name)

    n_complete = sum(1 for r in perf if r.data_coverage == "complete")
    n_partial = sum(1 for r in perf if r.data_coverage == "partial")
    n_none = sum(1 for r in perf if r.data_coverage == "none")
    lines.append(f"Data coverage this week: {len(perf)} performance row(s) — "
                f"{n_complete} complete, {n_partial} partial, {n_none} none.")
    if not perf:
        lines.append("INSUFFICIENT DATA this week — no performance rows imported yet.")

    published = [c for c in content if c.lifecycle_status in ("published", "verified")]
    failed = [c for c in content if c.lifecycle_status == "failed"]
    paused = [c for c in content if c.lifecycle_status == "paused"]
    total_attempts = len(published) + len(failed)
    lines.append(f"Publishing reliability: {len(published)} published/verified, {len(failed)} failed "
                f"({_pct(len(published), total_attempts)} success), {len(paused)} paused pending review.")

    if perf:
        winners = diagnosis.repeated_winners(perf, "pillar")
        losers = diagnosis.repeated_underperformers(perf, "pillar")
        lines.append("Top content patterns:" if winners else "Top content patterns: none yet (insufficient data)")
        for f in sorted(winners, key=lambda f: -f.evidence.get("n", 0))[:3]:
            lines.append(f"  + [{f.label}, {f.confidence}] {f.summary}")
        lines.append("Bottom content patterns:" if losers else "Bottom content patterns: none yet (insufficient data)")
        for f in sorted(losers, key=lambda f: -f.evidence.get("n", 0))[:3]:
            lines.append(f"  - [{f.label}, {f.confidence}] {f.summary}")

        leads = [r for r in perf if r.metrics.get("qualified_leads") is not None]
        if leads:
            total_leads = sum(r.metrics.get("qualified_leads", 0) for r in leads)
            lines.append(f"Qualified intent/leads: {total_leads:.0f} across {len(leads)} measured post(s).")
        else:
            lines.append("Qualified intent/leads: no lead data available this week.")
    else:
        lines.append("Top/bottom content patterns: insufficient data.")
        lines.append("Qualified intent/leads: insufficient data.")

    experiments = store.list_experiments(ctx.name)
    evaluated = [e for e in experiments if e.status == "evaluated"]
    proposed = [e for e in experiments if e.status == "proposed"]
    lines.append(f"Experiments evaluated this period: {len(evaluated)}.")
    for e in evaluated[:3]:
        lines.append(f"  - {e.hypothesis[:100]} -> {e.outcome.get('conclusion', '(no conclusion recorded)')}")
    if proposed:
        lines.append(f"Recommended next tests ({len(proposed)} proposed, awaiting approval):")
        for e in proposed[:3]:
            lines.append(f"  - {e.hypothesis[:100]}")

    decisions = []
    if paused:
        decisions.append(f"{len(paused)} content record(s) paused pending review")
    if failed:
        decisions.append(f"{len(failed)} publish attempt(s) failed")
    if proposed:
        decisions.append(f"{len(proposed)} experiment(s) awaiting approval")
    lines.append("Owner decisions required: " + ("; ".join(decisions) if decisions else "none"))
    return lines


def build_weekly_report(sections: list[tuple[BrandContext, Store, Registry]], now: int | None = None) -> str:
    now = now if now is not None else int(time.time())
    lines = ["ENGAGE WEEKLY INTELLIGENCE REPORT", "=" * 40]
    for ctx, store, registry in sections:
        lines.extend(_brand_weekly_section(ctx, store, registry, now))
    return "\n".join(lines)


def _brand_exceptions_section(ctx: BrandContext, store: Store, registry: Registry, now: int) -> list[str]:
    if registry.is_parked(ctx.name):
        return []
    day_ago = now - DAY
    perf_today = [r for r in store.list_performance_records(ctx.name)
                 if r.data_collected_at and r.data_collected_at >= day_ago]
    content = store.list_content_records(ctx.name)

    items = []
    missing = [r for r in perf_today if r.data_coverage == "none"]
    if missing:
        items.append(f"MISSING DATA: {len(missing)} item(s) published with no metrics collected")

    # ContentRecord doesn't expose an updated_at field on the dataclass, so
    # this checks current failed state rather than "failed in the last
    # 24h" specifically — a coarser but honest signal, not a fabricated one.
    failed = [c for c in content if c.lifecycle_status == "failed"]
    repeated_failures = [c for c in failed if c.retry_count >= 2]
    if repeated_failures:
        items.append(f"REPEATED OPERATIONAL FAILURES: {len(repeated_failures)} record(s) failed "
                     f"{max(c.retry_count for c in repeated_failures)}+ times")

    all_perf = store.list_performance_records(ctx.name)
    underperf = diagnosis.repeated_underperformers(all_perf, "platform") if all_perf else []
    if underperf:
        items.append(f"UNUSUAL UNDERPERFORMANCE: {underperf[0].summary}")

    high_leads = [r for r in perf_today if (r.metrics.get("qualified_leads") or 0) > 0]
    if high_leads:
        total = sum(r.metrics.get("qualified_leads", 0) for r in high_leads)
        items.append(f"HIGH-VALUE LEAD SIGNAL: {total:.0f} qualified lead(s) in the last 24h")

    if not items:
        return []
    return [f"\n## {ctx.name.upper()}"] + [f"  - {i}" for i in items]


def build_daily_exceptions(sections: list[tuple[BrandContext, Store, Registry]], now: int | None = None) -> str:
    """Surfaces ONLY action-worthy items — a brand with nothing wrong
    produces no section at all, not an empty reassurance."""
    now = now if now is not None else int(time.time())
    body = []
    for ctx, store, registry in sections:
        body.extend(_brand_exceptions_section(ctx, store, registry, now))
    if not body:
        return "ENGAGE DAILY EXCEPTIONS\n" + "=" * 40 + "\n(nothing action-worthy today)"
    return "ENGAGE DAILY EXCEPTIONS\n" + "=" * 40 + "\n" + "\n".join(body)
