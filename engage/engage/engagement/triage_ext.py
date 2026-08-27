"""Extends triage/comments.py's HUMAN/DRAFTABLE/SKIP into the 7-way
vocabulary Phase 5 needs, WITHOUT modifying that module or its rules —
classify_extended() calls classify_comment() first and only refines its
result. The existing HUMAN/SKIP regex lists, brand YAML extension points
(listening.triage.{human,skip}), and topic-mining logic are all reused
exactly as they are.

Confidence values are a fixed, documented, deterministic table — never a
learned or fabricated certainty score, consistent with this being a pure
regex classifier throughout."""
from __future__ import annotations

import re

from ..core.brand import BrandContext
from ..triage.comments import classify_comment

RISK_MARKERS = re.compile(
    r"\b(scam|refund|lawyer|legal|sue|lawsuit|wrong|incorrect|false|misleading|lying|stolen|report(ed|ing)?)\b",
    re.IGNORECASE,
)
# Subset of LEAD_MARKERS used to route escalations specifically — a
# partnership-flavored lead is CEO-escalation-worthy on its own (matches
# ceo.agent's "partnership" trigger exactly); a general commercial-interest
# lead ("price?", "how do I get started?") is not escalated individually —
# it feeds the aggregate engagement summary instead, so every "how much"
# comment doesn't spam the owner's escalation queue.
PARTNERSHIP_MARKERS = re.compile(
    r"\b(partner(ship)?|sponsor(ship)?|collab(oration)?|hire|work with you)\b", re.IGNORECASE,
)
LEAD_MARKERS = re.compile(
    r"\b(partner(ship)?|sponsor(ship)?|collab(oration)?|hire|work with you|"
    r"how (can|do) i (get started|join|work with)|price|pricing|cost|interested in)\b",
    re.IGNORECASE,
)
SPAM_MARKERS = re.compile(
    r"(https?://|\bdm me\b.{0,20}\bfree\b|\bcheck (my|out) (profile|bio|page)\b|\bfollow (back|for follow)\b)",
    re.IGNORECASE,
)
MIN_CONFIDENT_LENGTH = 12

# Confidence table — deterministic, not learned. Blocklist hits are the most
# certain signal available (a brand-configured fact, not an inference);
# short/ambiguous text is the least.
CONFIDENCE = {
    "blocklisted_author": 0.9,
    "risk_pattern": 0.75,
    "lead_pattern": 0.70,
    "spam_pattern": 0.80,
    "draftable": 0.75,
    "skip": 0.70,
    "human_generic": 0.60,
    "insufficient_context": 0.40,
}


def _blocklist(ctx: BrandContext) -> set[str]:
    return {a.lower() for a in (ctx.config.get("watch") or {}).get("blocked_accounts", [])}


def classify_extended(text: str, author: str, ctx: BrandContext) -> tuple[str, float, str]:
    """Returns (class, confidence, reason). `class` is one of
    core.models.ENGAGEMENT_TRIAGE_CLASSES."""
    if author.lower() in _blocklist(ctx):
        return ("reputation_or_safety_risk", CONFIDENCE["blocklisted_author"],
                "author is on this brand's configured blocklist (trap/impersonation account)")

    if SPAM_MARKERS.search(text):
        return "spam_or_low_value", CONFIDENCE["spam_pattern"], "matched a spam/promotional pattern"

    base_cls, base_reason = classify_comment(text, ctx)

    if base_cls == "SKIP":
        return "skip", CONFIDENCE["skip"], base_reason
    if base_cls == "DRAFTABLE":
        return "draftable", CONFIDENCE["draftable"], base_reason

    # base_cls == "HUMAN" — sub-classify why, using the SAME text the base
    # classifier already matched against, not a second independent read.
    if RISK_MARKERS.search(text):
        return ("reputation_or_safety_risk", CONFIDENCE["risk_pattern"],
                f"risk language matched (base triage: {base_reason})")
    if LEAD_MARKERS.search(text):
        return ("possible_lead", CONFIDENCE["lead_pattern"],
                f"lead/commercial-interest language matched (base triage: {base_reason})")
    if len(text.strip()) < MIN_CONFIDENT_LENGTH:
        return ("insufficient_context", CONFIDENCE["insufficient_context"],
                "too short/ambiguous to classify confidently — needs human read")
    return "human_needed", CONFIDENCE["human_generic"], base_reason


def triage_comment_texts(comments: list[dict], ctx: BrandContext) -> list[dict]:
    """Batch convenience: [{id, author, text}] -> same rows with
    class/confidence/reason attached. Does not touch the store — pure
    function, offline-testable, matching the rest of this codebase."""
    out = []
    for c in comments:
        cls, conf, reason = classify_extended(c.get("text", ""), c.get("author", ""), ctx)
        out.append({**c, "triage_class": cls, "triage_confidence": conf, "triage_reason": reason})
    return out
