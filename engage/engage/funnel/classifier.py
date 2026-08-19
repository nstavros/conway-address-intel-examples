"""Funnel logic, active only for brands whose config declares a `funnel:`
section (presence-switched — no brand-name conditionals in the engine).

Every draft is classified TOP (reach/hook), MIDDLE (teach/trust, including
lead-magnet capture), or BOTTOM (paid offer). check_week enforces the
configured ratio and refuses outright a week that is all bottom-funnel."""
from __future__ import annotations

import re

from ..core.brand import BrandContext
from ..core.models import Draft


class FunnelViolation(Exception):
    pass


DEFAULT_BOTTOM = [r"\bbuy\b", r"\benroll\b", r"\bprice\b", r"\bdiscount\b",
                  r"\bcourse\b", r"\bcheckout\b", r"link in bio to (buy|purchase|enroll)"]
DEFAULT_MIDDLE = [r"\bhow to\b", r"\bsave this\b", r"\bswipe\b", r"\bthe mechanism\b",
                  r"\bstep\b", r"\blesson\b", r"\bcomment \w+ and\b", r"\bhere'?s the\b"]


def _patterns(ctx: BrandContext, cls: str, defaults: list[str]) -> list[re.Pattern]:
    funnel = ctx.config.get("funnel") or {}
    raw = (funnel.get("patterns") or {}).get(cls, defaults)
    return [re.compile(p, re.IGNORECASE) for p in raw]


def classify(text: str, ctx: BrandContext) -> str:
    if not ctx.config.get("funnel"):
        return ""
    if any(rx.search(text) for rx in _patterns(ctx, "bottom", DEFAULT_BOTTOM)):
        return "BOTTOM"
    if any(rx.search(text) for rx in _patterns(ctx, "middle", DEFAULT_MIDDLE)):
        return "MIDDLE"
    return "TOP"


def check_week(drafts: list[Draft], ctx: BrandContext, tolerance: float = 0.10) -> dict:
    funnel = ctx.config.get("funnel")
    if not funnel:
        return {"enforced": False}
    if not drafts:
        raise FunnelViolation("empty week — nothing to queue")
    counts = {"TOP": 0, "MIDDLE": 0, "BOTTOM": 0}
    for d in drafts:
        cls = d.funnel_class or classify(d.text, ctx)
        counts[cls or "TOP"] += 1
    total = len(drafts)
    bottom_frac = counts["BOTTOM"] / total
    ratio = funnel["ratio"]
    if counts["BOTTOM"] == total:
        raise FunnelViolation(
            "refusing to queue a week that is all bottom-funnel — "
            "add reach and teaching content first"
        )
    if bottom_frac > ratio["bottom"] + tolerance:
        raise FunnelViolation(
            f"bottom-funnel share {bottom_frac:.0%} exceeds configured "
            f"{ratio['bottom']:.0%} (+{tolerance:.0%} tolerance)"
        )
    return {"enforced": True, "counts": counts,
            "shares": {k: round(v / total, 2) for k, v in counts.items()},
            "target": ratio}
