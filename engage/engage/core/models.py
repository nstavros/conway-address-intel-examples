"""Data models shared across pipeline stages."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Post:
    """A post seen by the listening layer, or one of our own published posts."""

    id: str
    brand: str
    platform: str
    author: str
    text: str
    url: str = ""
    created_at: int = 0  # unix seconds
    # engagement snapshots: (unix_ts, total_engagement) — two or more enable momentum
    snapshots: list[tuple[int, int]] = field(default_factory=list)
    own: bool = False  # True when the post is ours (triage/measure, never an opportunity)


@dataclass
class Opportunity:
    post_id: str
    brand: str
    score: float
    components: dict[str, float]
    excluded: bool = False
    exclusion_reason: str = ""


# Draft lifecycle:
#   DRAFT -> (gates) -> PENDING | BLOCKED
#   PENDING -> (human) -> APPROVED -> (publisher) -> PUBLISHED
#   any edit resets to DRAFT and invalidates approval.
DRAFT_STATUSES = ("DRAFT", "PENDING", "BLOCKED", "APPROVED", "PUBLISHED")


@dataclass
class Draft:
    id: str
    brand: str
    kind: str  # reply | original | repurpose
    platform: str
    text: str
    post_id: str = ""  # source post for replies
    material_id: str = ""  # source material for originals/repurposes
    funnel_class: str = ""  # TOP | MIDDLE | BOTTOM ("" when brand has no funnel)
    status: str = "DRAFT"
    gate_reasons: list[str] = field(default_factory=list)
    angle: str = ""

    @property
    def hash(self) -> str:
        return content_hash(self.text)


@dataclass
class SourceMaterial:
    id: str
    brand: str
    title: str
    text: str
    kind: str = "note"  # note | script | deal_update | photo_caption | article
