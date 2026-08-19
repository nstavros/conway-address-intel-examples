"""Publisher. Refuses anything not APPROVED, verifies the approval hash
against the current text (an edit after approval invalidates it), and enforces
per-platform rate limits from config.

v1 ships DryRunSink only: no live write path exists in the codebase until a
platform sink is explicitly added and wired. There are deliberately no DM,
follow, or unfollow capabilities anywhere.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..core.brand import BrandContext
from ..core.models import Draft
from ..core.store import Store


class PublishError(Exception):
    pass


@dataclass
class PlannedPost:
    draft: Draft
    note: str = ""


class DryRunSink:
    """Prints exactly what would post, where. Never writes anywhere."""

    def post(self, draft: Draft) -> str:
        return ""  # no external URL — nothing was published


def publish(store: Store, ctx: BrandContext, dry_run: bool = True,
            sink=None) -> list[PlannedPost]:
    approved = store.list_drafts(ctx.name, status="APPROVED")
    plan: list[PlannedPost] = []
    day_ago = int(time.time()) - 86400
    limits = ctx.config.get("rate_limits", {})

    for draft in approved:
        stored_hash = store.get_approval_hash(draft.id)
        if stored_hash != draft.hash:
            store.set_draft_status(draft.id, "DRAFT")
            store.revoke_approval(draft.id)
            plan.append(PlannedPost(draft, "SKIPPED: text changed after approval — "
                                           "approval revoked, draft must re-run gates"))
            continue
        cap = (limits.get(draft.platform) or {}).get("posts_per_day")
        if cap is not None:
            used = store.published_count_since(draft.platform, day_ago)
            if used >= cap:
                plan.append(PlannedPost(draft, f"HELD: {draft.platform} daily "
                                               f"rate limit reached ({cap})"))
                continue
        if dry_run:
            plan.append(PlannedPost(draft, "DRY RUN: would post"))
            continue
        if sink is None:
            raise PublishError(
                "no live sink configured — v1 is dry-run only; add a platform "
                "sink explicitly to go live"
            )
        url = sink.post(draft)
        store.record_published(draft.id, draft.platform, url)
        store.set_draft_status(draft.id, "PUBLISHED")
        plan.append(PlannedPost(draft, f"POSTED: {url or '(no url returned)'}"))
    return plan
