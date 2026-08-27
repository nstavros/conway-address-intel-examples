"""Reply drafting: two variants per opportunity, each from a distinct angle
defined in the brand config (voice.reply_angles). The prompt contract requires
each reply to add one concrete thing the original didn't say — enforced again
by a cheap heuristic here (a reply that only rephrases post vocabulary is
rejected) and by the LLM gate downstream."""
from __future__ import annotations

import re
from typing import Callable

from ..core.brand import BrandContext
from ..core.models import Draft, Post, new_id
from .llm import complete

WORD_RE = re.compile(r"[a-z0-9'-]+")


def _adds_something(reply: str, post_text: str) -> bool:
    post_words = set(WORD_RE.findall(post_text.lower()))
    reply_words = [w for w in WORD_RE.findall(reply.lower()) if len(w) > 3]
    if not reply_words:
        return False
    novel = [w for w in reply_words if w not in post_words]
    return len(novel) / len(reply_words) >= 0.25


def draft_replies(post: Post, ctx: BrandContext, backend: Callable[[str], str]) -> list[Draft]:
    angles = ctx.config["voice"].get("reply_angles", ["most useful concrete addition",
                                                      "respectful counterpoint with evidence"])[:2]
    drafts = []
    for angle in angles:
        text = complete(ctx, "reply", {
            "angle": angle,
            "author": post.author,
            "platform": post.platform,
            "post_text": post.text,
            "voice": ctx.prompts.get("voice", ""),
        }, backend)
        if not _adds_something(text, post.text):
            continue  # generic agreement / rephrase — not worth a human's review time
        drafts.append(Draft(
            id=new_id(), brand=ctx.name, kind="reply", platform=post.platform,
            text=text, post_id=post.id, angle=angle,
        ))
    return drafts
