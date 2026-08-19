"""Auditable opportunity scoring. Pure function over (Post, BrandContext, Store).

score = w_rel*relevance + w_auth*author_value + w_rec*recency
      + w_mom*momentum + w_hist*history
      [* question_multiplier when the brand hunts answerable questions]
Hard exclusions zero the score outright and record why.
Every component is stored so "why did this rank #1" is always answerable.
"""
from __future__ import annotations

import math
import re
import time

from ..core.brand import BrandContext
from ..core.models import Opportunity, Post
from ..core.store import Store

WORD_RE = re.compile(r"[#\w'-]+")


def _terms(ctx: BrandContext) -> list[str]:
    w = ctx.config.get("watch", {}) or {}
    return [t.lower() for t in (w.get("keywords", []) + w.get("hashtags", []))]


def _relevance(post: Post, ctx: BrandContext) -> float:
    text = post.text.lower()
    hits = sum(1 for t in _terms(ctx) if t in text)
    return min(1.0, hits / 3.0)


def _author_value(post: Post, ctx: BrandContext) -> float:
    tiers = {"A": 1.0, "B": 0.6, "C": 0.3}
    for acct in (ctx.config.get("watch", {}) or {}).get("accounts", []):
        if acct.get("handle", "").lower() == post.author.lower():
            return tiers.get(acct.get("tier", "C"), 0.3)
    return 0.3


def _recency(post: Post, ctx: BrandContext, now: int) -> float:
    half_life = ctx.config["scoring"]["half_life_hours"].get(post.platform, 24)
    age_h = max(0.0, (now - post.created_at) / 3600.0)
    return math.exp(-age_h * math.log(2) / max(half_life, 0.1))


def _momentum(post: Post, ctx: BrandContext) -> float:
    if len(post.snapshots) < 2:
        return 0.5  # neutral: manual ingest shouldn't be penalized
    (t0, e0), (t1, e1) = post.snapshots[-2], post.snapshots[-1]
    hours = max((t1 - t0) / 3600.0, 0.05)
    velocity = max(0.0, (e1 - e0) / hours)
    baseline = ctx.config["scoring"]["momentum_baseline_per_hour"]
    return min(1.0, velocity / max(baseline, 1))


def _history(post: Post, store: Store) -> float:
    h = store.author_history_score(post.author)
    return 0.5 if h is None else h


def _is_answerable_question(post: Post, ctx: BrandContext) -> bool:
    if "?" not in post.text:
        return False
    topics = [t.lower() for t in (ctx.config.get("watch", {}) or {}).get("teachable_topics", [])]
    text = post.text.lower()
    return any(t in text for t in topics)


def score_post(post: Post, ctx: BrandContext, store: Store, now: int | None = None) -> Opportunity:
    now = now or int(time.time())

    if post.own:
        return Opportunity(post.id, ctx.name, 0.0, {}, excluded=True,
                           exclusion_reason="own post")
    if store.has_engaged(post.id):
        return Opportunity(post.id, ctx.name, 0.0, {}, excluded=True,
                           exclusion_reason="already engaged with this post")
    blocklist = [a.lower() for a in (ctx.config.get("watch", {}) or {}).get("blocked_accounts", [])]
    if post.author.lower() in blocklist:
        return Opportunity(post.id, ctx.name, 0.0, {}, excluded=True,
                           exclusion_reason="author on blocklist")
    last = store.last_touch(post.author)
    cooldown = ctx.config["scoring"]["cooldown_days"] * 86400
    if last is not None and (now - last) < cooldown:
        return Opportunity(post.id, ctx.name, 0.0, {}, excluded=True,
                           exclusion_reason=f"author touched within cooldown "
                                            f"({ctx.config['scoring']['cooldown_days']}d)")

    components = {
        "relevance": _relevance(post, ctx),
        "author_value": _author_value(post, ctx),
        "recency": _recency(post, ctx, now),
        "momentum": _momentum(post, ctx),
        "history": _history(post, store),
    }
    weights = ctx.config["scoring"]["weights"]
    score = sum(weights[k] * v for k, v in components.items())

    mult = ctx.config["scoring"]["question_multiplier"]
    if mult != 1.0 and _is_answerable_question(post, ctx):
        components["question_multiplier"] = mult
        score *= mult

    return Opportunity(post.id, ctx.name, round(score, 4), components)
