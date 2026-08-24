"""Performance collection and signup attribution.

v1 is manual-pull: metric rows arrive from a file (platform exports, or
numbers the owner reads off the app), keyed by post id. After every batch the
collector recomputes `performance_norm` — each post's headline metric against
the brand median, scaled so median = 0.5 and 2x median = 1.0 — which is what
the scorer's `history` term reads back. That closes the feedback loop with no
API dependency.

Signup attribution is presence-switched by the brand's `funnel.products`
config (like the funnel module — no brand-name conditionals): a signup row
carries the code the person used; it attributes to the most recent published
draft that mentions the code within the attribution window."""
from __future__ import annotations

import statistics

from ..core.brand import BrandContext
from ..core.store import Store

HEADLINE_DEFAULT = "views"
ATTRIBUTION_WINDOW_DAYS = 7


def headline_metric(ctx: BrandContext) -> str:
    return (ctx.config.get("listening") or {}).get("headline_metric", HEADLINE_DEFAULT)


def record_performance(store: Store, ctx: BrandContext, rows: list[dict]) -> dict:
    """Ingest metric rows [{post_id, <metric>: value, ...}] and recompute
    performance_norm across every post that has the headline metric."""
    recorded = 0
    for row in rows:
        post_id = row.get("post_id")
        if not post_id:
            continue
        for key, value in row.items():
            if key == "post_id" or not isinstance(value, (int, float)):
                continue
            store.record_metric(post_id, key, float(value))
            recorded += 1

    metric = headline_metric(ctx)
    values = store.latest_metrics_by_post(metric)
    normalized = 0
    if values:
        median = statistics.median(values.values())
        for post_id, value in values.items():
            norm = 0.5 if median <= 0 else min(1.0, value / (2 * median))
            store.set_metric(post_id, "performance_norm", norm)
            normalized += 1
    return {"recorded": recorded, "normalized": normalized, "headline_metric": metric}


def attribute_signups(store: Store, ctx: BrandContext, signups: list[dict],
                      window_days: int = ATTRIBUTION_WINDOW_DAYS) -> list[dict]:
    """Attribute signup rows [{code, ts, ...}] to published drafts.

    A signup matches the most recently published draft whose text mentions the
    code, published inside the window before the signup. Unmatched signups are
    returned with post=None so the owner sees them rather than losing them."""
    if not (ctx.config.get("funnel") or {}).get("products"):
        raise ValueError("signup attribution requires funnel.products in the brand config")
    results = []
    for s in signups:
        code = (s.get("code") or "").strip().lower()
        ts = int(s.get("ts") or 0)
        match = None
        if code and ts:
            for row in store.published_since(ts - window_days * 86400):
                if row["published_at"] <= ts and code in row["text"].lower():
                    match = row
                    break  # rows are newest-first: first hit is most recent
        if match:
            store.record_metric(match["draft_id"], "signup", 1.0)
        results.append({
            "code": code, "ts": ts,
            "draft_id": match["draft_id"] if match else None,
            "platform": match["platform"] if match else None,
        })
    return results
