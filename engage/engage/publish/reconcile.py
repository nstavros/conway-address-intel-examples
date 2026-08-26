"""Reconciliation: teach the store about posts it did not make.

WHY THIS EXISTS. The content_records table answers "what did this system
do". For most of this system's life that was treated as an answer to "what
is live on the platform", and those are different questions. An operator
posting by hand, another tool, or a scheduled job elsewhere all produce
posts that content_records cannot see. On 2026-08-26 the store knew about
one published video while four were live, and the duplicate guard — which
reads that same table — would have cleared a second upload of a video
already public.

So: read the platform, and record anything the store cannot account for as
an `external_post`. That row is NOT a content record. It carries no
lifecycle, no approval and no gate history, because it genuinely has none.

READ-ONLY AGAINST THE PLATFORM. Nothing here uploads, edits, deletes,
authorizes, or mints a token. The only writes are to the local store. The
YouTube path uses the public feed — no credential and no OAuth scope.

TRUST DIRECTION. The platform is authoritative for what is live; the store
is authoritative for what this system did. Where they disagree, this module
never edits a content record to match the feed — it records the
disagreement. Rewriting local history to match an external observation is
how an audit trail stops being one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.store import Store

# Lifecycle states at which a content record legitimately holds a platform
# identifier. Mirrors the store's own published-identifier invariant: a
# record outside these states cannot be the origin of a live post.
ATTRIBUTABLE_STATES = ("published", "verified", "failed")

RECONCILE_EVENT = "external_post_observed"


class ReconciliationError(Exception):
    pass


@dataclass
class ReconciliationReport:
    """What one reconciliation pass found. Purely descriptive — every
    action it implies is left to the caller."""

    brand: str
    platform: str
    channel_id: str = ""
    observed: int = 0
    attributed: list[dict] = field(default_factory=list)
    unattributed_new: list[dict] = field(default_factory=list)
    unattributed_known: list[dict] = field(default_factory=list)
    checked_at: int = 0

    @property
    def drift(self) -> int:
        """Live posts this system cannot account for."""
        return len(self.unattributed_new) + len(self.unattributed_known)

    def summary(self) -> dict:
        return {
            "brand": self.brand,
            "platform": self.platform,
            "channel_id": self.channel_id,
            "observed": self.observed,
            "attributed": len(self.attributed),
            "drift": self.drift,
            "newly_recorded": len(self.unattributed_new),
            "checked_at": self.checked_at,
        }


def _title_of(post) -> str:
    """ingest/youtube_rss.py builds Post.text as '<title>\\n\\n<description>'.
    The title is the first line. Split, never substring-match — the same
    exact-match rule the upload verifier uses for the same reason."""
    return (getattr(post, "text", "") or "").split("\n", 1)[0].strip()


def base_title(title: str) -> str:
    """The title with its trailing hashtag run removed.

    WHY THIS IS NOT SUBSTRING MATCHING. Exact full-title comparison missed a
    real duplicate: the same video was staged as
    '... Greek Helmet #ancientgreece #hoplite' and published as
    '... Greek Helmet #ancientgreece #trojanwar #theodyssey'. Hashtags on a
    title are swappable metadata; the prose before them is the identity.

    Only a TRAILING run of hashtag tokens is stripped, and what remains is
    still compared exactly. A short title therefore still cannot match
    inside a longer unrelated one — the bug that made exact matching the
    rule here in the first place.
    """
    words = (title or "").strip().split()
    while words and words[-1].startswith("#"):
        words.pop()
    return " ".join(words).strip()


def _registry_channel_id(registry: Registry, ctx: BrandContext, platform: str) -> str:
    return ((registry.account_row(ctx.name, platform) or {}).get("channel_id") or "").strip()


def _indexed_records(store: Store, brand: str, platform: str) -> dict[str, object]:
    """Content records that hold a platform identifier, keyed by it."""
    out: dict[str, object] = {}
    for rec in store.list_content_records(brand):
        if rec.platform != platform:
            continue
        if rec.lifecycle_status not in ATTRIBUTABLE_STATES:
            continue
        pid = (rec.platform_post_id or "").strip()
        if pid:
            out[pid] = rec
    return out


def reconcile_youtube(store: Store, ctx: BrandContext, registry: Registry, *,
                      rss_reader, actor: str = "reconciliation",
                      clock=time.time) -> ReconciliationReport:
    """Compare the live public feed against this store and record the gap.

    `rss_reader` is a zero-argument callable returning Post objects — the
    same shape ingest/youtube_rss.py produces and publish/operations.py's
    read-back verification already consumes. Injected rather than
    constructed here so this module holds no network capability of its own
    and stays testable without a feed.
    """
    platform = "youtube"
    channel_id = _registry_channel_id(registry, ctx, platform)
    if not channel_id:
        raise ReconciliationError(
            f"no verified channel_id in the account registry for {ctx.name}/{platform} — "
            "refusing to attribute observed posts to an unverified channel"
        )

    posts = list(rss_reader() or [])
    known = _indexed_records(store, ctx.name, platform)
    report = ReconciliationReport(brand=ctx.name, platform=platform,
                                  channel_id=channel_id, observed=len(posts),
                                  checked_at=int(clock()))

    for post in posts:
        pid = (getattr(post, "id", "") or "").strip()
        if not pid:
            continue
        title = _title_of(post)
        url = getattr(post, "url", "") or ""
        published_at = getattr(post, "created_at", None)

        rec = known.get(pid)
        if rec is not None:
            report.attributed.append(
                {"platform_post_id": pid, "title": title,
                 "content_record_id": rec.id, "lifecycle_status": rec.lifecycle_status})
            continue

        seen_before = store.get_external_post(ctx.name, platform, pid) is not None
        store.record_external_post(
            brand=ctx.name, platform=platform, platform_post_id=pid,
            channel_id=channel_id, title=title, url=url,
            published_at=published_at, source="youtube_public_feed",
        )
        entry = {"platform_post_id": pid, "title": title, "url": url,
                 "published_at": published_at}
        if seen_before:
            report.unattributed_known.append(entry)
        else:
            report.unattributed_new.append(entry)

    return report


def live_titles(store: Store, brand: str, platform: str) -> set[str]:
    """Every title known to be live for this cell, from BOTH sources.

    This is the set a pre-publish duplicate check must consult. Consulting
    content_records alone is what made a duplicate upload possible.
    """
    titles: set[str] = set()
    for rec in store.list_content_records(brand):
        if rec.platform == platform and rec.lifecycle_status in ATTRIBUTABLE_STATES:
            t = ((rec.brief or {}).get("final_title") or "").strip()
            if t:
                titles.add(t)
    for row in store.list_external_posts(brand, platform):
        t = (row.get("title") or "").strip()
        if t:
            titles.add(t)
    return titles


def already_live(store: Store, brand: str, platform: str, title: str) -> dict | None:
    """Is a post with EXACTLY this title already live on this cell?

    Exact match, never substring — a short title is a substring of many
    unrelated ones, and the same mistake was already made and fixed in the
    upload verifier. Returns the matching evidence, or None.

    A None result means "no evidence of a duplicate", NOT "definitely not
    published": the feed carries only recent uploads, and a platform with no
    public feed contributes nothing here. Callers must treat this as one
    signal, not a guarantee.
    """
    want = (title or "").strip()
    if not want:
        return None
    want_base = base_title(want)

    def _match(other: str) -> str | None:
        other = (other or "").strip()
        if not other:
            return None
        if other == want:
            return "exact_title"
        if want_base and base_title(other) == want_base:
            return "base_title"
        return None

    for rec in store.list_content_records(brand):
        if rec.platform != platform or rec.lifecycle_status not in ATTRIBUTABLE_STATES:
            continue
        how = _match((rec.brief or {}).get("final_title") or "")
        if how:
            return {"source": "content_record", "matched_on": how,
                    "content_record_id": rec.id,
                    "lifecycle_status": rec.lifecycle_status,
                    "platform_post_id": rec.platform_post_id}
    for row in store.list_external_posts(brand, platform):
        how = _match(row.get("title") or "")
        if how:
            return {"source": "external_post", "matched_on": how,
                    "title": row.get("title"),
                    "platform_post_id": row.get("platform_post_id"),
                    "url": row.get("url"), "published_at": row.get("published_at")}
    return None
