"""Engagement Agent (Phase 5). Review-only: turns imported comments into
triage results, suggested reply drafts, lead/risk escalations, and
aggregate insight — never an external action. No function in this module
or anything it imports posts, edits, deletes, likes, follows, DMs, or
otherwise touches a live platform; that absence is structural (no import of
publish/sinks.py, no browser, no requests/urllib) and test-enforced.

Reuses rather than duplicates: triage/comments.py's classification rules
(via engagement/triage_ext.py, which wraps it), drafting/replies.py's reply
generation (same LLM chokepoint, same canary protection, same two-angle
"adds something" heuristic), safety/gates.py's fail-closed gate pipeline,
config/registry.py's eligibility checks, ceo/agent.py's escalation
mechanism, and measure/intelligence.py's recommendation/threshold
machinery. DESIGN.md's exclusion of DM endpoints is unchanged — nothing
here reads, writes, or references a DM."""
from __future__ import annotations

import time
from collections import Counter

from ..ceo.agent import create_escalation
from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.models import (
    REPLY_REVIEW_STATUSES,
    REPLY_REVIEW_TRANSITIONS,
    Comment,
    Post,
    ReplyDraft,
    content_hash,
    new_id,
)
from ..core.store import Store
from ..drafting.originals import eligible_platforms
from ..drafting.replies import draft_replies
from ..measure.diagnosis import label_finding
from ..measure.intelligence import write_recommendation
from ..measure.kpi import MIN_EVIDENCE_THRESHOLD
from ..safety.gates import run_gates
from .triage_ext import PARTNERSHIP_MARKERS, classify_extended


class EngagementRejected(Exception):
    """A comment import, reply generation, or transition was refused — the
    caller's prior state is left exactly as it was."""


def _account_handle(registry: Registry, ctx: BrandContext, platform: str) -> str:
    canonical = registry.canonical_name(ctx.name)
    for a in registry.accounts:
        if a.get("brand") == canonical and a.get("platform") == platform:
            return a.get("handle") or ""
    return ""


def _planning_source_label(registry: Registry, ctx: BrandContext) -> str:
    source = registry.planning_source(ctx.name)
    return f"{source['title']} ({source['updated']})" if source else "NONE RECORDED"


def _expire_stale_replies(store: Store, ctx: BrandContext, comment: Comment, *, actor: str) -> None:
    for d in store.list_reply_drafts(ctx.name, comment_id=comment.id):
        if d.review_status not in ("expired", "rejected"):
            transition_reply(store, d, "expired", actor=actor,
                             reason="source comment text changed since this reply was drafted")


def import_comment(store: Store, ctx: BrandContext, registry: Registry, row: dict, *,
                   actor: str = "engagement_agent") -> Comment:
    """Import ONE comment from an internal/manual fixture — never a live
    fetch. Rejects malformed rows, cross-brand data, and any account row
    that isn't currently eligible (disabled, unconnected, shared, parked,
    non-applicable) rather than blending it in. Idempotent on
    (brand, platform, source_id, external_id): a true re-import (identical
    text) returns the existing row unchanged; a text CHANGE expires any
    reply drafts bound to the old version and requires fresh review."""
    text, author = row.get("text"), row.get("author")
    platform, source_type, source_id = row.get("platform"), row.get("source_type"), row.get("source_id")
    if not text or not author or not platform or not source_type or not source_id:
        raise EngagementRejected(
            "comment row missing required field(s): text/author/platform/source_type/source_id"
        )
    if source_type not in ("content_record", "post"):
        raise EngagementRejected(f"unknown source_type {source_type!r} (expected content_record|post)")

    if registry.is_parked(ctx.name):
        raise EngagementRejected(f"'{ctx.name}' is parked — no comment import permitted")
    eligible = eligible_platforms(registry, ctx)
    if platform not in eligible:
        raise EngagementRejected(
            f"platform {platform!r} is not eligible for {ctx.name!r} — disabled, unconnected, "
            f"shared-account, or non-applicable (eligible: {eligible})"
        )

    source_content_hash = ""
    if source_type == "content_record":
        record = store.get_content_record(source_id)
        if record is None:
            raise EngagementRejected(f"no content record {source_id!r} — comment has no valid source context")
        if record.brand != ctx.name:
            raise EngagementRejected(
                f"content record {source_id} belongs to brand {record.brand!r}, not {ctx.name!r}"
            )
        if record.draft_id:
            source_content_hash = store.get_approval_hash(record.draft_id) or ""
    else:  # "post"
        post = store.get_post(source_id, ctx.name)
        if post is None:
            raise EngagementRejected(f"no post {source_id!r} — comment has no valid source context")

    account = _account_handle(registry, ctx, platform)
    text_hash = content_hash(text)
    external_id = row.get("external_id", "")
    existing = store.get_comment_by_external_id(ctx.name, platform, source_id, external_id)

    if existing is not None and existing.text_hash == text_hash:
        return existing  # true duplicate — idempotent no-op

    cls, conf, reason = classify_extended(text, author, ctx)
    comment_id = existing.id if existing is not None else new_id()
    if existing is not None:
        _expire_stale_replies(store, ctx, existing, actor=actor)

    comment = Comment(
        id=comment_id, brand=ctx.name, platform=platform, account=account, author=author, text=text,
        source_type=source_type, source_id=source_id, external_id=external_id,
        content_hash=source_content_hash, text_hash=text_hash,
        imported_at=row.get("imported_at", int(time.time())),
        triage_class=cls, triage_confidence=conf, triage_reason=reason,
    )
    store.save_comment(comment)
    store.record_content_event(comment.id, "comment_imported",
                               {"brand": ctx.name, "platform": platform, "account": account,
                                "triage_class": cls, "confidence": conf, "reason": reason,
                                "reimport": existing is not None},
                               actor=actor)
    return comment


def import_comments_batch(store: Store, ctx: BrandContext, registry: Registry,
                          rows: list[dict]) -> tuple[list[Comment], list[dict]]:
    """One bad row never poisons the batch — mirrors
    measure.intelligence.ingest_performance_batch's contract exactly."""
    accepted, rejected = [], []
    for row in rows:
        try:
            accepted.append(import_comment(store, ctx, registry, row))
        except EngagementRejected as e:
            rejected.append({"row": row, "reason": str(e)})
    return accepted, rejected


def generate_reply_review(store: Store, ctx: BrandContext, registry: Registry, comment: Comment,
                          backend, *, gate_backend=None, actor: str = "engagement_agent") -> list[ReplyDraft]:
    """Wraps drafting.replies.draft_replies() unchanged — same LLM
    chokepoint, same canary protection, same up-to-two-angle behavior, same
    'adds something concrete' heuristic. Only replies for comments already
    classified 'draftable' are generated. Every draft is gate-checked
    (safety/gates.py, unchanged) before it can reach review_required; a
    gate failure lands it at 'rejected' — it never reaches a human as if it
    had passed."""
    if comment.triage_class != "draftable":
        raise EngagementRejected(
            f"comment {comment.id} is classified {comment.triage_class!r}, not 'draftable' — "
            "replies are only generated for draftable comments"
        )
    gate_backend = gate_backend or backend
    post = Post(id=comment.id, brand=ctx.name, platform=comment.platform, author=comment.author,
               text=comment.text, own=False)
    generated = draft_replies(post, ctx, backend)
    source_label = _planning_source_label(registry, ctx)

    results: list[ReplyDraft] = []
    for d in generated:
        existing = store.get_reply_draft_by_dedup_key(comment.id, comment.text_hash, d.angle)
        if existing is not None:
            results.append(existing)  # idempotent: same comment version + angle already drafted
            continue
        gate_result = run_gates(d.text, ctx, source_texts=(comment.text,), llm=gate_backend)
        reply = ReplyDraft(
            id=new_id(), brand=ctx.name, platform=comment.platform, account=comment.account,
            comment_id=comment.id, comment_text_hash=comment.text_hash, draft_text=d.text,
            draft_text_hash=content_hash(d.text), angle=d.angle, brand_strategy_source=source_label,
            review_status="review_required" if gate_result.passed else "rejected",
            gate_reasons=gate_result.reasons, generated_at=int(time.time()),
        )
        store.save_reply_draft(reply)
        store.record_content_event(comment.id, "reply_draft_generated",
                                   {"reply_draft_id": reply.id, "angle": d.angle,
                                    "review_status": reply.review_status, "gate_reasons": gate_result.reasons},
                                   actor=actor)
        results.append(reply)
    return results


def transition_reply(store: Store, reply: ReplyDraft, new_status: str, *,
                     actor: str = "engagement_agent", reason: str = "") -> ReplyDraft:
    """'approved_for_manual_posting' is refused here on purpose — reach it
    only through approve_for_manual_posting(), which requires a human
    identity. No transition here, or anywhere in this module, creates any
    posting capability; every reply stays exactly what it started as: text
    for a human to copy and post themselves."""
    if new_status == "approved_for_manual_posting":
        raise EngagementRejected(
            "cannot reach 'approved_for_manual_posting' via transition_reply() — use "
            "approve_for_manual_posting(), which requires a human approver identity"
        )
    if new_status not in REPLY_REVIEW_STATUSES:
        raise EngagementRejected(f"unknown review_status {new_status!r}")
    prior = reply.review_status
    allowed = REPLY_REVIEW_TRANSITIONS.get(prior, frozenset())
    if new_status not in allowed:
        raise EngagementRejected(
            f"illegal reply-review transition for {reply.id}: {prior!r} -> {new_status!r} "
            f"(allowed: {sorted(allowed) or 'none — terminal'})"
        )
    reply.review_status = new_status
    store.save_reply_draft(reply)
    store.record_content_event(reply.comment_id, f"reply_transition:{new_status}",
                               {"reply_draft_id": reply.id, "from": prior, "to": new_status, "reason": reason},
                               actor=actor)
    return reply


def approve_for_manual_posting(store: Store, reply: ReplyDraft, approver: str) -> ReplyDraft:
    """The ONLY path to 'approved_for_manual_posting'. This records that a
    human reviewed and approved the TEXT — it never creates, enables, or
    triggers any automated posting. Posting remains a manual act outside
    this system, done by a person, on their own account, at their own
    initiative."""
    if not (approver or "").strip():
        raise EngagementRejected("approve_for_manual_posting requires a non-empty approver identity")
    if reply.review_status != "review_required":
        raise EngagementRejected(
            f"reply draft {reply.id} is {reply.review_status!r}, not 'review_required' — cannot approve"
        )
    reply.review_status = "approved_for_manual_posting"
    store.save_reply_draft(reply)
    store.record_content_event(reply.comment_id, "reply_approved_for_manual_posting",
                               {"reply_draft_id": reply.id, "approver": approver}, actor=approver)
    return reply


def route_risk_signal(store: Store, ctx: BrandContext, registry: Registry, comment: Comment, *,
                      actor: str = "engagement_agent") -> dict:
    """Every reputation/safety-risk comment escalates — a single risk
    signal is inherently escalation-worthy, unlike a single lead."""
    if comment.triage_class != "reputation_or_safety_risk":
        raise EngagementRejected(f"comment {comment.id} is not classified reputation_or_safety_risk")
    return create_escalation(
        store, ctx, registry, trigger="brand_or_reputation_risk",
        issue=f"Comment on {comment.platform} (account {comment.account}) flagged as a "
              f"reputation/safety risk: {comment.triage_reason}",
        why_it_matters="An unaddressed risk-flagged comment (complaint, legal language, or a "
                      "trap/impersonation account) stays visible on the post and can affect trust.",
        recommended_action="Review the comment and its source post directly before any reply is "
                           "considered; do not post anything until reviewed.",
        alternatives=["Escalate further if the pattern indicates a real legal/support issue",
                     "Mark as a false positive and note it if the triage pattern is too broad"],
        deadline="within 24h", consequence_of_no_decision="The flagged comment remains unaddressed "
                                                          "and visible on the live post.",
        traces_to=[comment.id], actor=actor,
    )


def route_lead_signal(store: Store, ctx: BrandContext, registry: Registry, comment: Comment, *,
                      actor: str = "engagement_agent") -> dict | None:
    """Only partnership-flavored leads escalate individually — matches
    ceo.agent's existing 'partnership' trigger exactly. A general
    commercial-interest comment ('price?') is NOT escalated per-comment;
    it feeds the aggregate engagement summary instead (see
    emit_engagement_insights), so routine interest doesn't spam the CEO's
    escalation queue — the same principle Phase 4 built escalation policy
    around."""
    if comment.triage_class != "possible_lead":
        raise EngagementRejected(f"comment {comment.id} is not classified possible_lead")
    if not PARTNERSHIP_MARKERS.search(comment.text):
        return None
    return create_escalation(
        store, ctx, registry, trigger="partnership",
        issue=f"Comment on {comment.platform} (account {comment.account}) reads as partnership/"
              f"collaboration interest: {comment.triage_reason}",
        why_it_matters="Partnership inquiries are time-sensitive and can't be handled by a "
                      "reply draft alone — they need a direct human response.",
        recommended_action="Review the comment and respond personally (outside this system); "
                           "this system drafts no reply for partnership inquiries.",
        alternatives=["Decline via a brief personal reply", "Request more detail before deciding"],
        deadline="within 48h", consequence_of_no_decision="The inquiry goes unanswered and the "
                                                          "opportunity may lapse.",
        traces_to=[comment.id], actor=actor,
    )


def emit_engagement_insights(store: Store, ctx: BrandContext, registry: Registry, *,
                             actor: str = "engagement_agent") -> dict:
    """Aggregate, privacy-safe counts only — no raw comment text, no author
    handles — sent to Performance Intelligence via the SAME
    write_recommendation() Phase 3 already built.

    The label is deliberately NOT computed from the bare comment count — a
    volume number is activity, not a claim about anything, and labeling
    'we triaged N comments' as validated_learning would overclaim exactly
    the way this whole system exists to prevent. Instead, label_finding()
    is applied to the DOMINANT triage category's occurrence count, and only
    when that category is an actual majority (not a coincidence of small
    numbers) is `consistent` ever True. A single comment, or a category
    with no real majority, can never reach validated_learning."""
    comments = store.list_comments(ctx.name)
    by_class = Counter(c.triage_class for c in comments)
    by_platform = Counter(c.platform for c in comments)
    evidence = {"total_comments": len(comments), "by_triage_class": dict(by_class),
               "by_platform": dict(by_platform)}

    if by_class:
        dominant_class, dominant_n = by_class.most_common(1)[0]
        is_majority = dominant_n / len(comments) >= 0.5
        label = label_finding(dominant_n, consistent=is_majority)
        evidence["dominant_pattern"] = {"class": dominant_class, "count": dominant_n,
                                        "share": round(dominant_n / len(comments), 2)}
    else:
        label = "insufficient_data"

    confidence = "high" if len(comments) >= MIN_EVIDENCE_THRESHOLD * 2 else (
        "medium" if len(comments) >= MIN_EVIDENCE_THRESHOLD else "low")
    return write_recommendation(
        store, ctx.name, "performance", label,
        f"Engagement summary: {len(comments)} comment(s) triaged for {ctx.name}",
        evidence, confidence, cross_brand=False,
    )


def render_review_queue_item(store: Store, ctx: BrandContext, registry: Registry,
                             comment: Comment) -> dict:
    """The human-facing review queue item. No part of this — or anything
    that renders it — can take an external action; that's why the
    instruction and checklist below are explicit text, not a button."""
    replies = store.list_reply_drafts(ctx.name, comment_id=comment.id)
    return {
        "comment_id": comment.id,
        "brand": ctx.name,
        "brand_canonical": registry.canonical_name(ctx.name),
        "platform": comment.platform,
        "account": comment.account or "(handle not recorded in registry)",
        "source": {"type": comment.source_type, "id": comment.source_id,
                   "content_hash": comment.content_hash or None},
        "author": comment.author,
        "comment_text": comment.text,
        "triage": {"class": comment.triage_class, "confidence": comment.triage_confidence,
                  "reason": comment.triage_reason},
        "suggested_replies": [
            {"id": r.id, "angle": r.angle, "text": r.draft_text, "review_status": r.review_status,
             "gate_reasons": r.gate_reasons} for r in replies
        ],
        "instruction": (
            "These are DRAFTS FOR HUMAN REVIEW ONLY. Nothing here has been posted, and nothing in "
            "this system is capable of posting it. If you approve a reply, YOU must post it "
            "yourself, manually, on the correct account, exactly as written."
        ),
        "manual_posting_checklist": [
            f"Confirm you are logged into the correct account ({comment.account or comment.platform}) "
            f"for {registry.canonical_name(ctx.name)} — not a different brand's account.",
            "Open the exact source post this comment is on — do not guess which post.",
            "Copy the approved reply text exactly as shown. Do not edit it after approval — an "
            "edit invalidates the approval and needs a fresh review.",
            "Post the reply yourself. No part of this system will do it for you.",
            "If the comment or your account state has changed since this was generated, stop — "
            "re-import the comment and re-review instead of posting a stale reply.",
        ],
    }
