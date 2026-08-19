"""Comment triage on our own posts: split incoming comments into HUMAN
(needs the owner personally — complaints, personal-money questions, business
inquiries, anything legal), DRAFTABLE (an on-topic question the drafting
pipeline can answer through the normal gates), and SKIP (praise, emoji,
nothing to act on). Plus theme mining: which configured topics the audience
keeps raising, so comment demand feeds the content queue.

Engine defaults are brand-neutral; a brand can extend them under
listening.triage.{human,skip} in its YAML."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ..core.brand import BrandContext

# Anything where a wrong or automated answer costs trust or money: the owner
# answers these personally. Fail toward HUMAN — over-triage is cheap,
# under-triage is not.
HUMAN_DEFAULT = [
    r"\b(refund|scam|report(ed|ing)?|lawyer|legal|sue|lawsuit)\b",
    r"\b(dm|email|call|contact) (me|you)\b",
    r"\b(my|our) (money|savings|401k|retirement|situation|portfolio)\b",
    r"\bshould i (buy|sell|invest|put)\b",
    r"\b(partner(ship)?|sponsor(ship)?|collab(oration)?|hire|work with you)\b",
    r"\b(wrong|incorrect|false|misleading|lying|stolen)\b",
    r"\bunsubscribe|stop (posting|messaging)\b",
]

SKIP_DEFAULT = [
    r"^\s*[\W\d\s]*$",                       # emoji / punctuation / numbers only
    r"^\s*(nice|great|cool|awesome|love (it|this)|fire|amazing|w)\s*[!.❤️]*\s*$",
    r"^\s*(first|second)\s*[!.]*\s*$",
]

QUESTION_RE = re.compile(
    r"\?|^\s*(how|what|why|when|where|who|which|can|could|do(es)?|is|are|should|would)\b",
    re.IGNORECASE,
)

WORD_RE = re.compile(r"[a-z0-9'-]+")


@dataclass
class TriageResult:
    comment_id: str
    author: str
    text: str
    cls: str  # HUMAN | DRAFTABLE | SKIP
    reason: str


def _patterns(ctx: BrandContext, key: str, defaults: list[str]) -> list[re.Pattern]:
    triage_cfg = (ctx.config.get("listening") or {}).get("triage") or {}
    extra = triage_cfg.get(key) or []
    return [re.compile(p, re.IGNORECASE) for p in list(defaults) + list(extra)]


def _topics(ctx: BrandContext) -> list[str]:
    watch = ctx.config.get("watch") or {}
    merged = [t.lower() for t in
              list(watch.get("keywords") or []) + list(watch.get("teachable_topics") or [])]
    return list(dict.fromkeys(merged))  # dedupe, keep order


def classify_comment(text: str, ctx: BrandContext) -> tuple[str, str]:
    for rx in _patterns(ctx, "human", HUMAN_DEFAULT):
        m = rx.search(text)
        if m:
            return "HUMAN", f"matched {m.group(0)!r}"
    for rx in _patterns(ctx, "skip", SKIP_DEFAULT):
        if rx.search(text):
            return "SKIP", "no substance to answer"
    if QUESTION_RE.search(text):
        lower = text.lower()
        hits = [t for t in _topics(ctx) if t in lower]
        if hits:
            return "DRAFTABLE", f"on-topic question ({hits[0]})"
        return "HUMAN", "question outside configured topics — owner judges"
    return "SKIP", "statement, nothing to act on"


def triage_comments(comments: list[dict], ctx: BrandContext) -> list[TriageResult]:
    out = []
    for c in comments:
        cls, reason = classify_comment(c.get("text", ""), ctx)
        out.append(TriageResult(
            comment_id=str(c.get("id", "")), author=c.get("author", ""),
            text=c.get("text", ""), cls=cls, reason=reason,
        ))
    return out


def mine_themes(comments: list[dict], ctx: BrandContext, top_n: int = 8) -> list[tuple[str, int]]:
    """Count configured-topic mentions across a comment batch. What the
    audience keeps asking about is next week's content queue."""
    counts: Counter = Counter()
    topics = _topics(ctx)
    for c in comments:
        lower = (c.get("text") or "").lower()
        for t in topics:
            if t in lower:
                counts[t] += 1
    return counts.most_common(top_n)
