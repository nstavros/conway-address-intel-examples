"""Per-brand weekly report + exactly ONE specific recommendation.

The recommendation is a deterministic first-match rule chain over the store,
ordered by leverage: unblock safety failures before publishing more, publish
to cadence before optimizing, close the metrics loop before trusting history,
rebalance the funnel before selling harder, then double down on what worked.
One recommendation, not a list — a list is a way to do nothing."""
from __future__ import annotations

import re
import time

from ..core.brand import BrandContext
from ..core.store import Store
from ..funnel.classifier import classify

WEEK = 7 * 86400


def _cadence_target(value) -> int:
    """'5-7/week' -> 5, '3/week' -> 3 (the floor is the commitment)."""
    m = re.search(r"\d+", str(value))
    return int(m.group(0)) if m else 0


def _recommend(ctx: BrandContext, store: Store, published, pending, blocked,
               perf_by_post: dict[str, float]) -> str:
    cadence = ctx.config.get("cadence") or {}
    targets = {p: _cadence_target(v) for p, v in cadence.items()}
    total_target = sum(targets.values())

    if blocked and len(blocked) >= max(1, len(pending)):
        d = blocked[0]
        return (f"Clear the blocked queue first: {len(blocked)} draft(s) failed gates "
                f"(e.g. {d.id}: {'; '.join(d.gate_reasons)[:120]}). Fix the source "
                f"material or regenerate before drafting anything new.")
    if total_target and len(published) < 0.5 * total_target:
        gap = total_target - len(published)
        note = (f"{len(pending)} pending draft(s) are already waiting for approval"
                if pending else "the queue is empty — draft first")
        return (f"Published {len(published)}/{total_target} of the weekly cadence "
                f"({gap} short); {note}.")
    if published and not perf_by_post:
        return ("No performance pulls recorded this week — run `engage measure` "
                "with this week's numbers or the scorer's history term stays blind.")
    if ctx.config.get("funnel") and published:
        shares = {"TOP": 0, "MIDDLE": 0, "BOTTOM": 0}
        for row in published:
            shares[row["funnel_class"] or classify(row["text"], ctx) or "TOP"] += 1
        bottom = shares["BOTTOM"] / len(published)
        limit = ctx.config["funnel"]["ratio"]["bottom"]
        if bottom > limit + 0.10:
            return (f"Bottom-funnel share of published posts is {bottom:.0%} against a "
                    f"{limit:.0%} target — lead next week with reach and teaching content.")
    if perf_by_post:
        best = max(perf_by_post, key=perf_by_post.get)
        if perf_by_post[best] >= 0.75:
            return (f"Post {best} performed at {perf_by_post[best]:.2f} (2x-median = 1.0) — "
                    f"repurpose its material to the platforms it hasn't hit yet "
                    f"(`engage repurpose`).")
    return ("Steady week. Ingest fresh listening data and score it — the pipeline "
            "is only as good as this week's opportunities.")


def build_weekly(ctx: BrandContext, store: Store, now: int | None = None) -> str:
    now = now or int(time.time())
    published = store.published_since(now - WEEK)
    pending = store.list_drafts(ctx.name, status="PENDING")
    blocked = store.list_drafts(ctx.name, status="BLOCKED")
    perf_by_post = {row["draft_id"]: v for row in published
                    if (v := store.latest_metric(row["draft_id"], "performance_norm")) is not None}

    lines = [f"ENGAGE WEEKLY — {ctx.name.upper()}", "=" * 40]

    by_platform: dict[str, int] = {}
    for row in published:
        by_platform[row["platform"]] = by_platform.get(row["platform"], 0) + 1
    cadence = ctx.config.get("cadence") or {}
    lines.append(f"Published this week: {len(published)}")
    for platform in sorted(set(by_platform) | set(cadence)):
        target = _cadence_target(cadence.get(platform, ""))
        lines.append(f"  {platform}: {by_platform.get(platform, 0)}"
                     + (f" (target {target}/week)" if target else ""))

    lines.append(f"Queue: {len(pending)} pending, {len(blocked)} blocked")

    if perf_by_post:
        ranked = sorted(perf_by_post.items(), key=lambda kv: kv[1], reverse=True)
        best_id, best_v = ranked[0]
        lines.append(f"Best performer: {best_id} (performance_norm {best_v:.2f})")
        if len(ranked) > 1:
            worst_id, worst_v = ranked[-1]
            lines.append(f"Weakest: {worst_id} (performance_norm {worst_v:.2f})")
    elif published:
        lines.append("No metrics recorded for this week's posts.")

    signups = [row for row in published if store.latest_metric(row["draft_id"], "signup")]
    if (ctx.config.get("funnel") or {}).get("products"):
        lines.append(f"Signups attributed to this week's posts: {len(signups)}")

    lines.append("")
    lines.append("DO THIS: " + _recommend(ctx, store, published, pending, blocked, perf_by_post))
    return "\n".join(lines)
