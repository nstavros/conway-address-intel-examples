"""Engagement reports — daily exceptions and weekly CEO summary. Brands
rendered separately, never blended — the same convention every report in
this codebase (digest, measure, ceo) already uses."""
from __future__ import annotations

import time
from collections import Counter

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.store import Store

DAY = 86400
WEEK = 7 * DAY


def _brand_engagement_summary(ctx: BrandContext, store: Store, registry: Registry) -> list[str]:
    canonical = registry.canonical_name(ctx.name)
    lines = [f"\n## {ctx.name.upper()} ({canonical})"]
    if registry.is_parked(ctx.name):
        lines.append("PARKED — excluded from engagement review by design.")
        return lines

    comments = store.list_comments(ctx.name)
    by_class = Counter(c.triage_class for c in comments)
    by_platform = Counter(c.platform for c in comments)
    lines.append(f"Volume: {len(comments)} comment(s) triaged.")
    if by_platform:
        lines.append("  by platform: " + ", ".join(f"{p}={n}" for p, n in by_platform.items()))
    if by_class:
        lines.append("  by triage category: " + ", ".join(f"{c}={n}" for c, n in by_class.items()))
    else:
        lines.append("  no comments imported yet.")

    drafts = store.list_reply_drafts(ctx.name)
    awaiting = [d for d in drafts if d.review_status == "review_required"]
    approved = [d for d in drafts if d.review_status == "approved_for_manual_posting"]
    lines.append(f"Response-draft queue: {len(awaiting)} awaiting your review, "
                 f"{len(approved)} approved for manual posting (you still post them yourself).")

    leads = [c for c in comments if c.triage_class == "possible_lead"]
    risks = [c for c in comments if c.triage_class == "reputation_or_safety_risk"]
    lines.append(f"Possible leads: {len(leads)}.  Reputation/safety risks: {len(risks)}.")

    insufficient = [c for c in comments if c.triage_class == "insufficient_context"]
    if insufficient:
        lines.append(f"Unresolved (insufficient context): {len(insufficient)} — needs a human read.")

    return lines


def build_engagement_weekly_summary(sections: list[tuple[BrandContext, Store, Registry]],
                                    now: int | None = None) -> str:
    """The weekly summary sent to the CEO Agent's reporting — same
    brand-separated shape the CEO's own weekly report uses."""
    now = now if now is not None else int(time.time())
    lines = ["ENGAGEMENT — WEEKLY SUMMARY (for CEO Agent)", "=" * 46,
             "Mode: review-only. No comment, DM, like, follow, or moderation action exists anywhere "
             "in this system — every reply below is a draft awaiting your manual posting."]
    for ctx, store, registry in sections:
        lines.extend(_brand_engagement_summary(ctx, store, registry))
    return "\n".join(lines)


def _brand_exceptions(ctx: BrandContext, store: Store, registry: Registry) -> list[str]:
    if registry.is_parked(ctx.name):
        return []
    comments = store.list_comments(ctx.name)
    items = []
    risks = [c for c in comments if c.triage_class == "reputation_or_safety_risk"]
    if risks:
        items.append(f"REPUTATION/SAFETY RISK: {len(risks)} comment(s) flagged, unresolved")
    leads = [c for c in comments if c.triage_class == "possible_lead"]
    if leads:
        items.append(f"POSSIBLE LEAD SIGNAL: {len(leads)} comment(s)")
    drafts = store.list_reply_drafts(ctx.name)
    stuck = [d for d in drafts if d.review_status == "review_required"]
    if len(stuck) >= 3:
        items.append(f"RESPONSE QUEUE BACKLOG: {len(stuck)} reply draft(s) awaiting review")
    insufficient = [c for c in comments if c.triage_class == "insufficient_context"]
    if insufficient:
        items.append(f"NEEDS HUMAN READ: {len(insufficient)} comment(s) with insufficient context "
                     "to classify confidently")
    if not items:
        return []
    return [f"\n## {ctx.name.upper()} ({registry.canonical_name(ctx.name)})"] + [f"  - {i}" for i in items]


def build_engagement_daily_exceptions(sections: list[tuple[BrandContext, Store, Registry]],
                                      now: int | None = None) -> str:
    """Only action-worthy items — silence means nothing needs you."""
    now = now if now is not None else int(time.time())
    body: list[str] = []
    for ctx, store, registry in sections:
        body.extend(_brand_exceptions(ctx, store, registry))
    header = "ENGAGEMENT — DAILY EXCEPTIONS\n" + "=" * 46
    if not body:
        return header + "\n(nothing action-worthy today)"
    return header + "\n" + "\n".join(body)
