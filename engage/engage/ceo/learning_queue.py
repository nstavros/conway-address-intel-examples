"""Durable-learning review queue. The CEO may REVIEW validated-learning
candidates from Performance Intelligence; it cannot write them into durable
Claude Mem. Nothing in this module writes to ~/.claude/projects/*/memory/ or
any claude-mem interface — queueing a candidate and approving one both only
record an internal row. The actual memory write stays a deliberate human act,
per harness/social/LEARNING-POLICY.md.

Brand-scoped by default. cross_brand requires the flag AND evidence from more
than one brand — the same two-brand rule measure/intelligence.py already
enforces, reused here rather than re-implemented."""
from __future__ import annotations

import time

from ..core.models import new_id
from ..core.store import Store
from ..measure.intelligence import is_learning_write_eligible

QUEUE_STATUSES = ("queued", "approved_for_write", "rejected")

FORBIDDEN_CONTENT_MARKERS = ("password", "oauth_token", "api_key", "mfa", "private_dm", "cookie")


class LearningQueueRejected(Exception):
    pass


def queue_candidate(store: Store, brand: str, finding, *, cross_brand: bool = False,
                    supporting_brands: list[str] | None = None, actor: str = "ceo_agent") -> dict:
    """Queue a validated-learning candidate for human review. Refuses
    anything below the Performance Intelligence write-eligibility threshold,
    and refuses a cross_brand claim backed by fewer than two brands."""
    if not is_learning_write_eligible(finding):
        raise LearningQueueRejected(
            f"finding is {finding.label!r}/{finding.confidence!r} — only "
            "validated_learning at medium+ confidence may be queued for durable learning"
        )
    supporting = supporting_brands or []
    if cross_brand and len(set(supporting)) < 2:
        raise LearningQueueRejected(
            f"cross_brand: true requires evidence from more than one brand — got {supporting}"
        )
    lowered = f"{finding.summary} {finding.evidence}".lower()
    for marker in FORBIDDEN_CONTENT_MARKERS:
        if marker in lowered:
            raise LearningQueueRejected(
                f"candidate references {marker!r} — private DMs, credentials, and secrets "
                "are never stored in durable learning"
            )

    row = {
        "id": new_id(), "brand": "cross_brand" if cross_brand else brand,
        "target": "ceo", "label": finding.label,
        "summary": f"[LEARNING CANDIDATE] {finding.summary}",
        "evidence": {"finding": {"kind": finding.kind, "dimension": finding.dimension,
                                 "key": finding.key, "evidence": finding.evidence},
                     "supporting_brands": sorted(set(supporting)) if cross_brand else [brand],
                     "queue_status": "queued", "reviewed_by": "", "actor": actor},
        "confidence": finding.confidence, "cross_brand": cross_brand,
        "authorization": (
            "NONE — queued for human review only. This does NOT write to Claude Mem "
            "or any durable memory file; a person makes that call after reading this."
        ),
        "created_at": int(time.time()),
    }
    store.save_recommendation(row)
    return row


def list_queue(store: Store, brand: str, status: str = "queued") -> list[dict]:
    return [r for r in store.list_recommendations(brand, target="ceo")
            if r["summary"].startswith("[LEARNING CANDIDATE]")
            and r["evidence"].get("queue_status") == status]


def review_candidate(store: Store, candidate: dict, *, decision: str, reviewer: str) -> dict:
    """Record a human's review decision. Even 'approved_for_write' does NOT
    write anything durable — it marks the candidate as cleared for a person
    to write, which remains a separate manual act."""
    if decision not in ("approved_for_write", "rejected"):
        raise LearningQueueRejected(f"decision must be approved_for_write or rejected, got {decision!r}")
    if not (reviewer or "").strip():
        raise LearningQueueRejected("review_candidate requires a non-empty reviewer identity")
    updated = dict(candidate)
    updated["evidence"] = {**candidate["evidence"], "queue_status": decision, "reviewed_by": reviewer}
    store.save_recommendation(updated)
    return updated
