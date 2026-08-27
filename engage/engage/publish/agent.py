"""Publishing Operations Agent — the consumer for directives addressed here.

WHY THIS EXISTS. Every other division in this system is an agent with a
decision boundary. Publishing Operations was a set of functions, so a
directive with `owner_agent="publishing_operations"` had no reader: it sat
in the recommendations table until a human happened to open it. On
2026-08-26 one was filed at 08:52 with a 09:30 deadline and was still
unexecuted when the deadline passed. The plan was fine. Nobody was in the
room.

WHAT IT DOES. Reads directives targeted at this division, and for each one
produces a DISPOSITION: what stands between the directive and a live post,
measured rather than assumed. Where nothing stands in the way and the work
is already owner-approved, it can execute — through
operations.execute_youtube_upload(), the single sanctioned path, never by
improvising a lifecycle at the call site.

WHAT IT CANNOT DO, STRUCTURALLY. It cannot approve. `approve` appears
nowhere in this module and the approval write path is not imported: a
record that is not already at 'approved' produces an escalation to the
owner, never a self-approval. That is the one boundary whose erosion would
make every gate downstream decorative, and it is enforced by test.

It also cannot decide it is allowed to act. Runtime permission is measured
by capability.py from a real attempt, and an unprobed permission is UNKNOWN,
which is never treated as satisfied.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.store import Store
from .capability import CapabilityReport, youtube_capability
from .operations import execute_youtube_upload
from .reconcile import already_live, reconcile_youtube

OWNED_TARGET = "publishing"
DIRECTIVE_KIND = "ceo_directive"

# Dispositions, ordered from "cannot proceed" to "ready".
BLOCKED_CAPABILITY = "blocked_capability"
BLOCKED_DUPLICATE = "blocked_duplicate"
NEEDS_OWNER_APPROVAL = "needs_owner_approval"
NO_RECORD = "no_content_record"
READY = "ready"

EXECUTED_EVENT = "directive_executed"
ESCALATED_EVENT = "directive_escalated"


class PublishingAgentError(Exception):
    pass


@dataclass
class Disposition:
    """What actually stands between one directive and a live post."""

    directive_id: str
    brand: str
    deliverable: str = ""
    deadline: str = ""
    platform_scope: list = field(default_factory=list)
    status: str = NO_RECORD
    reasons: list = field(default_factory=list)
    content_record_id: str = ""
    capability: dict = field(default_factory=dict)
    duplicate_evidence: dict = field(default_factory=dict)
    assessed_at: int = 0

    @property
    def executable(self) -> bool:
        return self.status == READY

    def summary(self) -> dict:
        return {"directive_id": self.directive_id, "status": self.status,
                "deliverable": self.deliverable, "deadline": self.deadline,
                "content_record_id": self.content_record_id,
                "reasons": self.reasons, "assessed_at": self.assessed_at}


def open_directives(store: Store, ctx: BrandContext) -> list[dict]:
    """Directives addressed to this division, newest first.

    A directive is 'open' when no execution event has been recorded against
    its id. Completion is derived from events rather than a status column,
    for the same reason reconciliation is: this codebase has no migration
    mechanism, so a new column on an existing table would never appear on an
    existing database.
    """
    out = []
    for rec in store.list_recommendations(ctx.name, target=OWNED_TARGET):
        ev = rec.get("evidence") or {}
        if ev.get("kind") != DIRECTIVE_KIND:
            continue
        events = store.list_content_events(rec["id"])
        if any(e["event_type"] == EXECUTED_EVENT for e in events):
            continue
        out.append(rec)
    return sorted(out, key=lambda r: r.get("created_at") or 0, reverse=True)


def _record_for(store: Store, ctx: BrandContext, directive: dict):
    """The content record a directive points at, via traces_to.

    A directive that names no record is not executable — this agent never
    goes looking for 'something that looks close enough' to publish.
    """
    for rid in (directive.get("traces_to") or []):
        rec = store.get_content_record(rid)
        if rec is not None and rec.brand == ctx.name:
            return rec
    return None


def assess(store: Store, ctx: BrandContext, registry: Registry, directive_row: dict, *,
           sink_factory=None, permission_probe=None,
           clock=time.time) -> Disposition:
    """Measure one directive against reality. Pure read — changes nothing.

    Order matters: capability first (cheapest, and a hard stop), then
    duplicate (protects against the irreversible mistake), then approval
    (the human gate). Reporting the approval gap first would invite someone
    to clear it before discovering the work could not run anyway.
    """
    d = directive_row.get("evidence") or {}
    disp = Disposition(
        directive_id=directive_row["id"], brand=ctx.name,
        deliverable=d.get("deliverable", ""), deadline=d.get("deadline", ""),
        platform_scope=list(d.get("platform_scope") or []),
        assessed_at=int(clock()),
    )

    platforms = disp.platform_scope or []
    if "youtube" not in platforms:
        disp.status = BLOCKED_CAPABILITY
        disp.reasons.append(
            f"this agent can execute youtube only; directive scope is {platforms}")
        return disp

    cap: CapabilityReport = youtube_capability(
        ctx, registry, sink_factory=sink_factory,
        permission_probe=permission_probe, clock=clock)
    disp.capability = cap.summary()
    if not cap.publishable:
        disp.status = BLOCKED_CAPABILITY
        disp.reasons.extend(cap.blockers)
        return disp

    rec = _record_for(store, ctx, d)
    if rec is None:
        disp.status = NO_RECORD
        disp.reasons.append(
            "directive traces to no content record in this brand — nothing to publish. "
            "A directive names its work; this agent never selects a substitute.")
        return disp
    disp.content_record_id = rec.id

    title = ((rec.brief or {}).get("final_title") or "").strip()
    dup = already_live(store, ctx.name, rec.platform, title)
    if dup is not None:
        disp.status = BLOCKED_DUPLICATE
        disp.duplicate_evidence = dup
        disp.reasons.append(
            f"a post with exactly this title is already live ({dup.get('source')}: "
            f"{dup.get('platform_post_id')}) — refusing a second publish")
        return disp

    if rec.lifecycle_status != "approved":
        disp.status = NEEDS_OWNER_APPROVAL
        disp.reasons.append(
            f"content record {rec.id} is {rec.lifecycle_status!r}, not 'approved'. "
            "This agent cannot approve; owner approval is required.")
        return disp

    disp.status = READY
    return disp


def work_queue(store: Store, ctx: BrandContext, registry: Registry, *,
               sink_factory=None, permission_probe=None) -> list[Disposition]:
    """Every open directive, assessed. This is the thing that was missing:
    something that reads the room."""
    return [assess(store, ctx, registry, row, sink_factory=sink_factory,
                   permission_probe=permission_probe)
            for row in open_directives(store, ctx)]


def refresh_reality(store: Store, ctx: BrandContext, registry: Registry, *,
                    rss_reader) -> dict:
    """Reconcile the store against the platform BEFORE assessing anything.

    Called first by execute_directive(): a duplicate check against a store
    that has not seen the last four posts is the check that failed.
    """
    return reconcile_youtube(store, ctx, registry, rss_reader=rss_reader).summary()


def execute_directive(store: Store, ctx: BrandContext, registry: Registry,
                      directive_row: dict, *, sink, media: dict,
                      rss_reader=None, permission_probe=None,
                      actor: str = "publishing_operations_agent") -> tuple[Disposition, object]:
    """Execute one directive, or refuse with a recorded reason.

    Reconciles first when a reader is supplied, re-assesses, and only then
    calls the single sanctioned upload path. A non-READY disposition is
    recorded as an escalation and returned — never coerced into an attempt.
    """
    if rss_reader is not None:
        refresh_reality(store, ctx, registry, rss_reader=rss_reader)

    disp = assess(store, ctx, registry, directive_row,
                  sink_factory=lambda: sink, permission_probe=permission_probe)

    if not disp.executable:
        store.record_content_event(
            directive_row["id"], ESCALATED_EVENT,
            {"status": disp.status, "reasons": disp.reasons,
             "content_record_id": disp.content_record_id,
             "authorization": "NONE — this agent cannot clear any of these itself"},
            actor=actor)
        return disp, None

    rec = store.get_content_record(disp.content_record_id)
    rec, result = execute_youtube_upload(store, ctx, registry, rec, sink,
                                         media=media, actor=actor)
    store.record_content_event(
        directive_row["id"], EXECUTED_EVENT,
        {"content_record_id": rec.id, "outcome": getattr(result, "outcome", ""),
         "lifecycle_status": rec.lifecycle_status,
         "platform_post_id": rec.platform_post_id},
        actor=actor)
    return disp, result
