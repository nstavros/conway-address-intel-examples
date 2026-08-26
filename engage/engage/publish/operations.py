"""Publishing Operations Agent (Phase 2). Its only input is a structured
handoff produced by drafting/originals.py's write_handoff(target="publishing").
It never talks to a platform, browser, or credential store — Phase 2 ships
with exactly two working sinks (DryRunSink, ManualReviewSink; see
publish/sinks.py) and four sink STUBS that fail closed until a real,
authorized integration replaces them.

Nothing here duplicates existing machinery: registry checks reuse
config/registry.py and drafting/originals.eligible_platforms(); approval
and hash-binding reuse approval/queue.py unchanged; lifecycle transitions
reuse drafting/originals.transition() (same LIFECYCLE_TRANSITIONS table
Phase 1 built); the event log and handoff format reuse
Store.record_content_event() and drafting/originals.write_handoff().

Copy is never rewritten. The exact caption text approved in Phase 1 is what
gets scheduled, "published" (simulated), and handed to the checklist — no
function in this module edits record.brief or the underlying Draft.text."""
from __future__ import annotations

import time

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.models import ContentRecord, LifecycleError
from ..core.store import (
    AMBIGUOUS_EVENT,
    RECONCILIATION_INCONSISTENT,
    Store,
)
from ..drafting.originals import eligible_platforms, transition, write_handoff
from ..measure.collector import ATTRIBUTION_WINDOW_DAYS
from .constraints import check_constraints
from .sinks import PublishSink
from .youtube_transport import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_CONFIRMED,
    VERIFICATION_CONFIRMED,
    VERIFICATION_MISMATCH,
    VERIFICATION_PENDING,
)

RETRYABLE_STATUS = "transient_failure"

# Sinks that provably take NO external action: DryRunSink simulates
# everything in-process, ManualReviewSink emits a checklist for a human.
# Neither can reach a network, a browser, or a credential store.
#
# This is an ALLOWLIST, not a blocklist, and that is the whole point: any
# sink not named here — including every sink that does not exist yet — is
# treated as live-capable and refused unless its exact brand/account/platform
# row is explicitly live-enabled in the registry. A future upload sink is
# therefore gated the moment it is written, without anyone having to remember
# to add it to a list of dangerous things.
NON_ACTING_SINKS = frozenset({"dry_run", "manual_review"})


class PublishingRejected(Exception):
    """A handoff or publish attempt was refused before any lifecycle
    transition happened — the record is left exactly as it was."""



# An ambiguous upload may surface a remote id, but it is NOT a confirmed
# platform post id: nothing proved the object exists, or that it landed on
# the authorized channel. Recorded under a deliberately different key so no
# reader — duplicate detection, URL building, verification, reporting — can
# mistake it for a binding. Never written to the ContentRecord, and never
# called a "video id" in any API, report, or user-facing output.
AMBIGUOUS_ID_KEY = "candidate_remote_id"

TERMINAL_VERIFICATION_STATUSES = (VERIFICATION_MISMATCH,)

RECONCILIATION_MIN_EVIDENCE = 8

# Allowlisted attestations. Free-text evidence alone is unauditable. Each
# constant is valid for exactly ONE function.
RECONCILIATION_ATTEST_STUDIO_CONFIRMED = "youtube_studio_confirmed"
RECONCILIATION_ATTEST_STUDIO_NO_MATCH = "youtube_studio_checked_no_matching_post"
RECONCILIATION_ATTESTATIONS = (
    RECONCILIATION_ATTEST_STUDIO_CONFIRMED,
    RECONCILIATION_ATTEST_STUDIO_NO_MATCH,
)

# Allowlisted BASIS values: how the operator established the finding.
#
# This is what excludes absence-of-signal reasoning. Public-feed absence, a
# timeout, an expired window, and an assumption are excluded because none is
# an accepted basis value — NOT because operator prose is searched. Evidence
# text is never inspected; it is recorded verbatim (truncated) for audit.
# Prose filtering would be leaky and could be satisfied by rewording rather
# than by actually checking.
RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK = "youtube_studio_direct_check"
RECONCILIATION_BASES = (RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK,)


def assert_no_open_reconciliation(store: Store, record: ContentRecord, action: str) -> None:
    """Block EVERY forward action while a remote-upload outcome is unresolved.

    Deliberately independent of lifecycle state: the predicate does not filter
    on 'publishing', so a record carrying an unresolved outcome is blocked
    wherever it sits — including one wrongly returned to 'approved'.

    An unresolved outcome means a remote object may already exist. Any forward
    action risks duplicating it. This is NOT ordinary in-flight work briefly
    delayed."""
    state = store.record_requires_reconciliation(record.id)
    if not state.required:
        return
    urgent = (" URGENT — unresolved outcome outside the parking state."
              if state.is_urgent else "")
    raise PublishingRejected(
        f"{action} blocked for content record {record.id}: unresolved remote-upload "
        f"outcome, manual reconciliation required (ambiguous event id "
        f"{state.ambiguous_event_id}, category {state.category}, lifecycle "
        f"{state.lifecycle_status!r}).{urgent} A remote object may already exist. "
        "Only an explicit reconciliation event can clear this."
    )


def require_live_enabled(registry: Registry, ctx: BrandContext, platform: str,
                         sink: PublishSink) -> None:
    """Refuse any externally-acting sink unless THIS exact brand/platform
    cell is explicitly live-enabled in the account registry.

    Before this existed, `live_status` was documentation only: the account
    registry described it as the control for live publishing and DEC-SM-005
    treated it as the per-cell gate, but no code read it anywhere. That was
    harmless only for as long as every live-capable sink was a stub that
    raised. This makes the documented control real, so enablement is a
    deliberate registry edit rather than a side effect of a sink existing.

    Explicitly NOT sufficient to pass this gate: a connector being
    connected, a credential or token existing, a CLI flag being passed, the
    platform being `enabled: true`, or the sink having been implemented.
    Only the registry row's own `live_status` counts."""
    if sink.name in NON_ACTING_SINKS:
        return
    if not registry.is_live_enabled(ctx.name, platform):
        row = registry.account_row(ctx.name, platform)
        current = (row or {}).get("live_status", "<no row in registry>")
        raise PublishingRejected(
            f"sink {sink.name!r} takes live external action, but {ctx.name}/{platform} "
            f"is not live-enabled (live_status={current!r}). Enabling requires an "
            f"explicit, per-account registry change — a connector, credential, or "
            f"flag alone never authorizes a live action."
        )


def _account_handle(registry: Registry, ctx: BrandContext, platform: str) -> str:
    canonical = registry.canonical_name(ctx.name)
    for a in registry.accounts:
        if a.get("brand") == canonical and a.get("platform") == platform:
            return a.get("handle") or ""
    return ""


def _account_timezone(registry: Registry, ctx: BrandContext, platform: str) -> str:
    canonical = registry.canonical_name(ctx.name)
    for a in registry.accounts:
        if a.get("brand") == canonical and a.get("platform") == platform and a.get("timezone"):
            return a["timezone"]
    return "America/New_York"


def _duplicate_in_flight(store: Store, ctx: BrandContext, record: ContentRecord, draft) -> ContentRecord | None:
    """Same brand, platform, and approved-content hash already scheduled or
    further along — the deterministic duplicate check from §5."""
    for other in store.list_content_records(ctx.name):
        if other.id == record.id or other.platform != record.platform:
            continue
        if other.lifecycle_status not in ("scheduled", "publishing", "published", "verified"):
            continue
        other_draft = store.get_draft(other.draft_id, ctx.name) if other.draft_id else None
        if other_draft and draft and other_draft.hash == draft.hash:
            return other
    return None


def _frequency_exceeded(store: Store, ctx: BrandContext, platform: str, now: int) -> bool:
    """Reuses the brand's own rate_limits.<platform>.posts_per_day —
    counted against SIMULATED activity only (content_records with a
    publish_method set), never the pre-existing `published` table, which
    tracks real publishes and must not be polluted by dry-run/manual-review
    activity."""
    cap = (ctx.config.get("rate_limits", {}).get(platform) or {}).get("posts_per_day")
    if cap is None:
        return False
    day_ago = now - 86400
    count = sum(
        1 for r in store.list_content_records(ctx.name)
        if r.platform == platform and r.publish_method and r.scheduled_at and r.scheduled_at >= day_ago
    )
    return count >= cap


def receive_handoff(store: Store, ctx: BrandContext, registry: Registry, handoff: dict, *,
                    sink: PublishSink, scheduled_at: int, timezone: str | None = None,
                    constraints_override_reason: str = "",
                    actor: str = "publishing_operations_agent") -> ContentRecord:
    """Validate a publishing handoff end to end and, if everything checks
    out, schedule it (dry-run only — see the sink). Every rejection reason
    is checked explicitly; none are optional or bypassable from this
    function's public signature. Re-derives every fact from the LIVE store,
    never trusting the handoff payload's own snapshot of brand/platform/
    lifecycle_status — time may have passed since it was written."""
    if handoff.get("target") != "publishing":
        raise PublishingRejected(f"handoff target is {handoff.get('target')!r}, not 'publishing'")
    record_id = handoff.get("content_record_id")
    record = store.get_content_record(record_id) if record_id else None
    if record is None:
        raise PublishingRejected(f"no content record {record_id!r} — malformed or unknown handoff")
    if record.brand != ctx.name:
        raise PublishingRejected(
            f"handoff record {record.id} belongs to brand {record.brand!r}, not {ctx.name!r} — refusing"
        )
    assert_no_open_reconciliation(store, record, "scheduling")

    if registry.is_parked(ctx.name):
        raise PublishingRejected(f"'{ctx.name}' is parked in the brand registry — never eligible to publish")

    eligible = eligible_platforms(registry, ctx)
    if record.platform not in eligible:
        raise PublishingRejected(
            f"platform {record.platform!r} is not eligible for {ctx.name!r} — disabled, "
            f"unconnected, shared-account, or non-applicable (eligible: {eligible})"
        )

    if record.lifecycle_status == "paused":
        # The only legal way back into scheduling from 'paused' is when
        # Publishing Operations itself paused THIS record for unverifiable
        # constraints (checked via its own event history, not trusted from
        # the caller) and the caller now supplies an explicit override — a
        # record paused for any other reason (e.g. a human's own judgment
        # call) is never silently resumed here.
        events = store.list_content_events(record.id)
        if not events or events[-1]["event_type"] != "constraints_unverifiable":
            raise PublishingRejected(
                f"content record {record.id} is paused for a reason other than "
                "unverifiable platform constraints — requires human review, not a reschedule"
            )
        if not constraints_override_reason:
            raise PublishingRejected(
                f"content record {record.id} is paused pending platform-constraint "
                "verification — resuming requires an explicit constraints_override_reason"
            )
    elif record.lifecycle_status != "approved":
        raise PublishingRejected(
            f"content record {record.id} is {record.lifecycle_status!r}, not 'approved' — "
            "already scheduled, not yet approved, or terminal"
        )
    draft = store.get_draft(record.draft_id, ctx.name) if record.draft_id else None
    approved_hash = store.get_approval_hash(record.draft_id) if record.draft_id else None
    if draft is None or approved_hash is None or approved_hash != draft.hash:
        raise PublishingRejected(
            f"content record {record.id}: no matching approval hash for its draft — "
            "unapproved, edited since approval, or the draft is missing"
        )

    caption = (record.brief or {}).get("caption", "")
    if not caption.strip():
        raise PublishingRejected(f"content record {record.id} has no caption/content to publish")

    dup = _duplicate_in_flight(store, ctx, record, draft)
    if dup is not None:
        raise PublishingRejected(
            f"content record {record.id} duplicates {dup.id} — same brand, platform, and "
            f"approved-content hash already {dup.lifecycle_status}"
        )

    if _frequency_exceeded(store, ctx, record.platform, scheduled_at):
        raise PublishingRejected(f"{ctx.name}/{record.platform}: daily posting frequency limit reached")

    account = _account_handle(registry, ctx, record.platform)
    cr = check_constraints(record.platform, record.brief or {})
    if not cr.ok and not constraints_override_reason:
        # The only structurally legal fail-closed target from "approved" is
        # "paused" (LIFECYCLE_TRANSITIONS["approved"] has no
        # "review_required" — that state is only reachable earlier in the
        # pipeline). Never proceeds to schedule with an unknown/violated
        # constraint; never truncates or reformats the caption to "fix" it.
        return transition(store, record, "paused", "constraints_unverifiable",
                          {"account": account, "unknown": cr.unknown, "violations": cr.violations},
                          actor=actor)

    # Last gate before the sink is touched at all: a live-capable sink may
    # not even schedule against a cell that is not explicitly live-enabled.
    require_live_enabled(registry, ctx, record.platform, sink)

    tz = timezone or _account_timezone(registry, ctx, record.platform)
    result = sink.schedule(record, scheduled_at, tz)
    if result.status != "ok":
        raise PublishingRejected(f"sink {sink.name} refused to schedule: {result.error}")

    record.scheduled_at = scheduled_at
    record.timezone = tz
    record.publish_method = sink.name
    record.sink_job_id = result.sink_job_id
    return transition(store, record, "scheduled", "scheduled_dry_run",
                      {"account": account, "sink": sink.name, "scheduled_at": scheduled_at,
                       "timezone": tz, "constraints_override_reason": constraints_override_reason or None},
                      actor=actor)


def attempt_publish(store: Store, ctx: BrandContext, registry: Registry, record: ContentRecord,
                    sink: PublishSink, *, actor: str = "publishing_operations_agent") -> ContentRecord:
    """One publish attempt. manual_review's 'publish' step produces a
    checklist and deliberately does NOT advance past 'scheduled' — nothing
    automated posted anything, so nothing here may claim 'published'. Every
    other sink drives scheduled -> publishing -> published (or -> failed,
    classified, never silently)."""
    if record.lifecycle_status != "scheduled":
        raise PublishingRejected(
            f"content record {record.id} is {record.lifecycle_status!r}, not 'scheduled'"
        )
    assert_no_open_reconciliation(store, record, "publishing")
    # Re-checked here, not just at schedule time: a cell can be turned off
    # between scheduling and publishing, and this is the call that would
    # actually act. Never trust the earlier check.
    require_live_enabled(registry, ctx, record.platform, sink)
    account = _account_handle(registry, ctx, record.platform)

    if sink.name == "manual_review":
        result = sink.publish(record)
        record.verification_status = result.verification_status
        record.verification_evidence = result.verification_evidence
        store.save_content_record(record)
        store.record_content_event(
            record.id, "manual_checklist_ready",
            {"brand": record.brand, "platform": record.platform, "account": account,
             "checklist": result.verification_evidence.get("checklist", {}), "simulated": True},
            actor=actor,
        )
        return record

    record = transition(store, record, "publishing", "publish_attempt_started",
                        {"account": account, "sink": sink.name}, actor=actor)
    result = sink.publish(record)
    if result.status == "ok":
        record.published_at = record.scheduled_at  # simulated — the sink never contacts a clock of its own
        record.sink_job_id = result.sink_job_id or record.sink_job_id
        record.published_url = result.published_url    # always "" for every sink in Phase 2
        record.platform_post_id = result.platform_post_id  # always "" for every sink in Phase 2
        return transition(store, record, "published", "publish_simulated_ok",
                          {"account": account, "sink": sink.name, "simulated": True},
                          actor=actor)

    record.retry_count += 1
    record.last_error = result.error
    record.verification_evidence = {**record.verification_evidence, "last_failure_classification": result.status}
    return transition(store, record, "failed", "publish_failed",
                      {"account": account, "sink": sink.name, "classification": result.status,
                       "error": result.error, "retry_count": record.retry_count},
                      actor=actor)


def verify_publication(store: Store, ctx: BrandContext, registry: Registry, record: ContentRecord,
                       sink: PublishSink, *, actor: str = "publishing_operations_agent") -> ContentRecord:
    """published -> verified (simulated). On success, automatically emits
    the downstream handoffs to Engagement and Performance Intelligence —
    both explicitly labeled simulated, neither an instruction to act."""
    if record.lifecycle_status != "published":
        raise PublishingRejected(
            f"content record {record.id} is {record.lifecycle_status!r}, not 'published'"
        )
    assert_no_open_reconciliation(store, record, "verification")
    require_live_enabled(registry, ctx, record.platform, sink)
    account = _account_handle(registry, ctx, record.platform)
    result = sink.verify(record)
    if result.status == "ok":
        record.verification_status = result.verification_status
        record.verification_evidence = result.verification_evidence
        record = transition(store, record, "verified", "verification_simulated_ok",
                            {"account": account, "sink": sink.name, "simulated": True}, actor=actor)
        emit_downstream_handoffs(store, ctx, registry, record)
        return record

    record.retry_count += 1
    record.last_error = result.error
    record.verification_evidence = {**record.verification_evidence, "last_failure_classification": result.status}
    return transition(store, record, "failed", "verification_failed",
                      {"account": account, "sink": sink.name, "classification": result.status,
                       "error": result.error, "retry_count": record.retry_count},
                      actor=actor)


def retry_publish(store: Store, ctx: BrandContext, record: ContentRecord, *,
                  retry_limit: int = 3, actor: str = "publishing_operations_agent") -> ContentRecord:
    """Requeues a failed record for another attempt — ONLY when the last
    failure was classified transient_failure and the retry limit hasn't
    been reached. An ambiguous or permanent failure, or one that's already
    exhausted its retries, is never auto-retried; this function is the only
    door back to 'scheduled' from 'failed', and it enforces both checks
    every time it's called, not just once at record creation."""
    if record.lifecycle_status != "failed":
        raise PublishingRejected(f"content record {record.id} is {record.lifecycle_status!r}, not 'failed'")
    classification = (record.verification_evidence or {}).get("last_failure_classification")
    if classification != RETRYABLE_STATUS:
        raise PublishingRejected(
            f"content record {record.id}'s last failure was classified {classification!r}, "
            f"not {RETRYABLE_STATUS!r} — never auto-retried"
        )
    if record.retry_count >= retry_limit:
        raise PublishingRejected(
            f"content record {record.id} has used {record.retry_count}/{retry_limit} retries — "
            "requires manual requeue (review_required), not another automatic retry"
        )
    return transition(store, record, "scheduled", "retry_requeued",
                      {"retry_count": record.retry_count, "retry_limit": retry_limit}, actor=actor)


def emit_downstream_handoffs(store: Store, ctx: BrandContext, registry: Registry,
                             record: ContentRecord) -> dict:
    """Structured, explicitly-simulated handoffs for the future Engagement
    and Performance Intelligence agents. Reuses write_handoff() — the same
    non-duplication, non-instruction, hash-bound guarantees Phase 1 already
    built apply here unchanged. No comment, DM, or post is ever sent by
    this function or anything it calls."""
    account = _account_handle(registry, ctx, record.platform)
    approved_hash = store.get_approval_hash(record.draft_id) if record.draft_id else None
    brief = record.brief or {}
    extra = {
        "account": account,
        "platform_post_id": record.platform_post_id or None,  # never fabricated
        "published_url": record.published_url or None,        # never fabricated
        "approved_content_hash": approved_hash,
        "campaign": None,  # no campaign concept exists anywhere in ENGAGE yet — not invented here
        "cta": brief.get("cta", ""),
        "simulated_publish_time": record.published_at,
        "monitoring_window_days": ATTRIBUTION_WINDOW_DAYS,  # reused from measure/collector.py, not invented
        "simulated": True,
    }
    return {
        "engagement": write_handoff(store, record, "engagement", **extra),
        "performance": write_handoff(store, record, "performance", **extra),
    }


def execute_youtube_upload(store: Store, ctx: BrandContext, registry: Registry,
                           record: ContentRecord, sink, *, media: dict,
                           actor: str = "youtube_upload_sink"):
    """The ONLY sanctioned direct-upload orchestration path.

    It exists because none did. Before this, performing an upload meant
    improvising the lifecycle sequence at the call site — which is how a record
    ended up at 'approved' while holding a live published_url. Ordering mirrors
    attempt_publish(): every state is ENTERED before the fields that state
    implies are persisted, and transition() sets lifecycle_status before
    calling save_content_record(), so the store never sees an identifier at a
    pre-'published' state.

    Returns (record, result). Nothing is ever retried here.
    """
    if record.lifecycle_status != "approved":
        raise PublishingRejected(
            f"content record {record.id} is {record.lifecycle_status!r}, not 'approved'")
    assert_no_open_reconciliation(store, record, "upload")

    brief = record.brief or {}
    asset = brief.get("legacy_asset") or {}

    auth = sink.mint_authorization(record, media=media)   # re-runs every gate
    d = {"channel_id": brief.get("channel_id", ""), "sink": sink.name}

    record = transition(store, record, "scheduled", "upload_authorized", d, actor=actor)
    record = transition(store, record, "publishing", "upload_started", d, actor=actor)

    result = sink.transport.upload(
        authorization=auth,
        asset_path=asset.get("path", ""),
        asset_sha256=asset.get("sha256", ""),
        metadata_hash=store.get_draft(record.draft_id, record.brand).hash,
        video_resource=sink.build_request_body(record))

    # ---- UNRESOLVED OUTCOME ---------------------------------------------
    # The record stays at 'publishing' — the existing technical parking state,
    # retained for compatibility and duplicate protection. This is NOT
    # ordinary in-flight work: the outcome of the remote upload is unresolved
    # and a human must reconcile it. No identifier is bound. Never retried.
    # This event is what makes the record discoverable by the predicate, and
    # the ONLY thing a resolution event can undo.
    if result.outcome == OUTCOME_AMBIGUOUS:
        detail = {**d,
                  "outcome": result.outcome,
                  "http_status": result.http_status,
                  "diagnostic_category": "post_transmission_unresolved_outcome",
                  "status_note": ("unresolved remote-upload outcome; "
                                  "manual reconciliation required"),
                  "detail": (result.error_detail or "")[:500],
                  "requires_manual_reconciliation": True,
                  "is_ordinary_in_flight": False}
        if result.video_id:
            detail[AMBIGUOUS_ID_KEY] = result.video_id   # deliberately NOT "video_id"
        store.record_content_event(record.id, AMBIGUOUS_EVENT, detail, actor=actor)
        return record, result

    # ---- DEFINITE FAILURE ------------------------------------------------
    if result.outcome != OUTCOME_CONFIRMED:
        record = transition(
            store, record, "failed", "upload_failed",
            {**d, "outcome": result.outcome, "error_reason": result.error_reason,
             "http_status": result.http_status,
             "detail": (result.error_detail or "")[:500]}, actor=actor)
        return record, result

    # ---- CONFIRMED -------------------------------------------------------
    # CONFIRMED implies the transport already matched the returned channelId
    # against the capability token.
    record.published_url = f"https://www.youtube.com/shorts/{result.video_id}"
    record.platform_post_id = result.video_id
    record.published_at = result.uploaded_at
    record.publish_method = sink.name
    record = transition(
        store, record, "published", "upload_confirmed",
        {**d, "video_id": result.video_id,
         "returned_channel_id": result.returned_channel_id,
         "privacy": result.returned_privacy_status,
         "title": result.returned_title, "simulated": False}, actor=actor)
    return record, result


def record_rss_verification(store: Store, record: ContentRecord, verification: dict,
                            *, window_expired: bool = False,
                            actor: str = "youtube_upload_sink") -> ContentRecord:
    """Read-back outcome, in three categories.

    A. CONFIRMED  published -> verified, identifier retained.
    B. PENDING    stays 'published'. Never re-uploads, never clears the
                  identifier. The feed lags, so absence inside the window is
                  an unknown, not a failure.
    C. TERMINAL   published -> failed, retaining platform_post_id,
                  published_url, published_at and publish_method for manual
                  recovery. 'failed' is in PUBLISHED_ID_STATES precisely so
                  this route is persistable.

    `window_expired` is an explicit caller decision. verify_via_rss() has no
    clock, so a timeout is never inferred here.

    Performs no upload, mints no authorization, refreshes no token, reads no
    credential, makes no network call.
    """
    # A public-feed read-back can neither resolve nor clear an unresolved
    # outcome: absence from a lagging feed is not evidence, and a present
    # entry does not prove WHICH attempt created it.
    assert_no_open_reconciliation(store, record, "public-feed read-back")

    status = verification.get("status")
    vid = verification.get("video_id", "")
    terminal = (status in TERMINAL_VERIFICATION_STATUSES) or (
        status == VERIFICATION_PENDING and window_expired)

    if (status == VERIFICATION_CONFIRMED or terminal) and record.lifecycle_status != "published":
        raise PublishingRejected(
            f"content record {record.id} is {record.lifecycle_status!r}, not 'published' — "
            "read-back may only confirm or terminally fail a record that reached "
            "'published'.")

    if status == VERIFICATION_CONFIRMED:
        return transition(
            store, record, "verified", "rss_readback_verified",
            {"video_id": vid, "url": verification.get("url", ""),
             "is_short": verification.get("is_short")}, actor=actor)

    if terminal:
        reason = ("title_mismatch" if status == VERIFICATION_MISMATCH
                  else "verification_window_expired")
        # Identifier deliberately RETAINED — clearing it would destroy the
        # only pointer to a real public object.
        return transition(
            store, record, "failed", "rss_readback_failed_terminal",
            {"video_id": vid, "failure_category": reason,
             "actual_title": str(verification.get("actual_title", ""))[:200],
             "detail": str(verification.get("detail", ""))[:300],
             "identifier_retained": True, "requires_manual_recovery": True},
            actor=actor)

    store.record_content_event(
        record.id, "rss_readback_pending",
        {"video_id": vid, "detail": str(verification.get("detail", ""))[:300],
         "identifier_retained": True, "no_reupload": True}, actor=actor)
    return record


def _assert_resolvable(store: Store, record: ContentRecord, operation: str):
    """Raises before any mutation. Resolution requires an unresolved outcome
    AND lifecycle exactly 'publishing'. An unresolved outcome outside the
    parking state is deliberately NOT resolvable: forcing a transition out of
    a state that should be impossible would destroy the evidence of how it
    arose. It needs a separate, explicitly authorized forensic repair that
    does not exist yet."""
    state = store.record_requires_reconciliation(record.id)
    if not state.required:
        raise PublishingRejected(
            f"{operation}: content record {record.id} has no unresolved remote-upload "
            "outcome to reconcile")
    if state.category == RECONCILIATION_INCONSISTENT:
        raise PublishingRejected(
            f"{operation}: content record {record.id} has an unresolved remote-upload "
            f"outcome while lifecycle is {state.lifecycle_status!r}, outside the "
            "'publishing' parking state. This should be impossible and is NOT resolvable "
            "by the normal operations. No lifecycle transition will be forced. A separate, "
            "explicitly authorized forensic repair is required. Nothing was changed.")
    return state


def _assert_attestation(attestation: str, expected: str, operation: str) -> None:
    """Allowlist check. Runs BEFORE any mutation or event write."""
    if attestation is None or not str(attestation).strip():
        raise PublishingRejected(
            f"{operation} requires an explicit attestation. Free-text evidence alone is "
            f"not sufficient. Expected {expected!r}.")
    if attestation not in RECONCILIATION_ATTESTATIONS:
        raise PublishingRejected(
            f"{operation}: unsupported attestation {attestation!r}. "
            f"Allowed: {list(RECONCILIATION_ATTESTATIONS)}")
    if attestation != expected:
        raise PublishingRejected(
            f"{operation}: attestation {attestation!r} is not valid for this operation. "
            f"Expected {expected!r}. Nothing was changed.")


def _assert_basis(basis: str, expected: str, operation: str) -> None:
    """Allowlist check on HOW the finding was established. Exact match only —
    no substring, no normalisation, no case folding. Evidence prose is never
    inspected."""
    if basis is None or not str(basis).strip():
        raise PublishingRejected(
            f"{operation} requires an explicit basis describing how the finding was "
            f"established. Expected {expected!r}. Nothing was changed.")
    if basis not in RECONCILIATION_BASES:
        raise PublishingRejected(
            f"{operation}: unsupported basis {basis!r}. Allowed: "
            f"{list(RECONCILIATION_BASES)}. Absence from the public feed, a timeout, an "
            "expired window, or an assumption are not accepted bases — a direct check of "
            "the channel's own management surface is required. Nothing was changed.")
    if basis != expected:
        raise PublishingRejected(
            f"{operation}: basis {basis!r} is not valid for this operation. "
            f"Expected {expected!r}. Nothing was changed.")


def _assert_operator(actor: str, evidence: str, operation: str) -> None:
    if not actor or not actor.strip():
        raise PublishingRejected(f"{operation} requires a named operator identity")
    if not evidence or len(evidence.strip()) < RECONCILIATION_MIN_EVIDENCE:
        raise PublishingRejected(
            f"{operation} requires an evidence reference of at least "
            f"{RECONCILIATION_MIN_EVIDENCE} characters describing what was checked")


def reconcile_confirmed_remote_post(store: Store, record: ContentRecord, *,
                                    actor: str, evidence: str, attestation: str,
                                    confirmed_video_id: str,
                                    expected_channel_id: str) -> ContentRecord:
    """An operator looked at YouTube Studio and the object EXISTS on the
    expected channel.

    No basis parameter: a positive sighting IS the direct check, and the
    attestation already names Studio.

    `confirmed_video_id` is supplied explicitly and never defaulted from the
    event's candidate_remote_id — a human retyping what they actually saw is
    the entire evidentiary value.

    Every precondition is validated BEFORE any mutation, so a refusal leaves
    the record byte-identical and writes no event. No upload, no sink, no
    token refresh, no credential read, no network call.
    """
    op = "reconcile_confirmed_remote_post"
    _assert_resolvable(store, record, op)
    _assert_attestation(attestation, RECONCILIATION_ATTEST_STUDIO_CONFIRMED, op)
    _assert_operator(actor, evidence, op)

    vid = (confirmed_video_id or "").strip()
    if not vid:
        raise PublishingRejected(f"{op} requires a non-empty confirmed video id")
    bound = (record.brief or {}).get("channel_id", "")
    if not expected_channel_id or expected_channel_id != bound:
        raise PublishingRejected(
            f"{op}: channel {expected_channel_id!r} does not match the record's bound "
            f"channel {bound!r} — refusing to bind an identifier to an unverified "
            "channel. Nothing was changed.")

    record.published_url = f"https://www.youtube.com/shorts/{vid}"
    record.platform_post_id = vid
    record.published_at = record.published_at or int(time.time())
    record.publish_method = record.publish_method or "youtube_upload"
    return transition(
        store, record, "published", "reconciliation_confirmed_remote_post",
        {"confirmed_video_id": vid, "channel_id": expected_channel_id,
         "attestation": attestation, "reconciled_by": actor,
         "evidence_reference": evidence.strip()[:300],
         "resolution": ("operator directly observed the object on the expected channel "
                        "in the channel's management surface"),
         "inferred_from_feed_presence": False}, actor=actor)


def reconcile_confirmed_no_remote_post(store: Store, record: ContentRecord, *,
                                       actor: str, evidence: str,
                                       attestation: str, basis: str) -> ContentRecord:
    """An operator looked at YouTube Studio and NO matching object exists.

    ABSENCE OF SIGNAL IS NOT EVIDENCE, guaranteed STRUCTURALLY: the only
    accepted basis is a direct check of the channel's own management surface.
    Public-feed absence, timeout, expiry, and assumption cannot be supplied as
    a basis at all.

    The guarantee does NOT come from inspecting operator prose. Evidence text
    is never searched and may freely mention the feed, a timeout, or anything
    else — it is recorded verbatim (truncated) for the audit trail.

    A wrong answer here permits a fresh upload and can create a duplicate
    public object, which is why the bar is a structured value.
    """
    op = "reconcile_confirmed_no_remote_post"
    _assert_resolvable(store, record, op)
    _assert_attestation(attestation, RECONCILIATION_ATTEST_STUDIO_NO_MATCH, op)
    _assert_basis(basis, RECONCILIATION_BASIS_YOUTUBE_STUDIO_DIRECT_CHECK, op)
    _assert_operator(actor, evidence, op)

    record.published_url = ""
    record.platform_post_id = ""
    return transition(
        store, record, "failed", "reconciliation_confirmed_no_remote_post",
        {"attestation": attestation, "basis": basis, "reconciled_by": actor,
         "evidence_reference": evidence.strip()[:300],
         "resolution": ("operator directly checked the channel's management surface and "
                        "confirmed no matching remote object exists"),
         "inferred_from_feed_absence": False}, actor=actor)
