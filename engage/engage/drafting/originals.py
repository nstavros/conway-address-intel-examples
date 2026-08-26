"""Original-content briefs and platform-native drafting — the module named
but never built in DESIGN.md §1 ("weekly queue from source material").

Two things happen here, kept deliberately distinct:

1. A BRIEF (the planning artifact — audience, objective, hook, cta, sources,
   claims) is assembled from a brand's own SourceMaterial and validated by
   `validate_brief()`, adapted from content-os's validator (retired
   2026-08-21; see ~/.claude/harness/social/DECISIONS.md DEC-SM-002). Exit
   codes are preserved: 0 ok, 1 incomplete/invalid, 2 not-approved,
   3 mismatch/unusable.
2. The DRAFT TEXT generated from that brief still goes through the existing,
   unchanged approval/queue.py + safety/gates.py pipeline — same fail-closed
   gates, same single write path, same SHA-256 approval binding. This module
   adds no second way to reach PENDING/APPROVED for a Draft.

Every step — brief creation, brief validation, gate results, approval,
handoffs — is appended to Store.content_events for a full audit trail
(core/models.py ContentRecord + LIFECYCLE_STATES).

Platform eligibility is gated on the INTERSECTION of the brand's own
platforms (brands/<slug>/config.yaml) and the harness account registry's
enabled rows (config/registry.py) — a platform in one but not the other is
never drafted for. A brand marked deprioritized in the registry is refused
outright unless the caller explicitly opts in (allow_parked=True)."""
from __future__ import annotations

from typing import Callable

from ..approval.queue import approve as approve_draft
from ..approval.queue import submit
from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.models import (
    LIFECYCLE_STATES,
    LIFECYCLE_TRANSITIONS,
    ContentRecord,
    Draft,
    LifecycleError,
    new_id,
)
from ..core.store import Store
from .llm import complete
from .repurpose import repurpose

REQUIRED_BRIEF_FIELDS = (
    "brand", "audience", "objective", "core_insight", "platform",
    "format", "hook", "cta", "brand_voice", "lifecycle_status",
)
# "reel" remains the catch-all for short vertical video (IG Reel / TikTok /
# YouTube Short) — unchanged, so every existing brief stays valid. "short"
# is additive: it declares a title+description+media piece specifically,
# which publish/constraints.py checks against a different key set than
# reel's single caption. Keep both; neither replaces the other.
FORMATS = {"reel", "carousel", "story-sequence", "lead-magnet", "short"}
PLACEHOLDERS = {"tbd", "todo", "n/a", "na", "xxx", "...", "placeholder", "fixme", "?"}
HANDOFF_TARGETS = ("publishing", "engagement", "performance", "drafting", "ceo")
APPROVED_LIKE = {"approved", "scheduled", "publishing", "published", "verified"}


class BriefValidation:
    """Mirrors the harvested validator's exit-code contract:
    0 ok -> production may proceed, 1 incomplete/invalid, 2 not approved,
    3 mismatch/unusable. `passed` is a convenience for `code == 0`."""

    def __init__(self, code: int, status: str, problems: list[str]):
        self.code = code
        self.status = status
        self.problems = problems

    @property
    def passed(self) -> bool:
        return self.code == 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"BriefValidation(code={self.code}, status={self.status!r}, problems={self.problems!r})"


def validate_brief(brief: dict, registry: Registry, *,
                    require_brand: str | None = None,
                    require_platform: str | None = None) -> BriefValidation:
    missing, empty, placeholder = [], [], []
    for field_name in REQUIRED_BRIEF_FIELDS:
        if field_name not in brief:
            missing.append(field_name)
            continue
        value = brief[field_name]
        text = " ".join(map(str, value)) if isinstance(value, list) else str(value)
        stripped = text.strip()
        if not stripped:
            empty.append(field_name)
        elif stripped.lower() in PLACEHOLDERS:
            placeholder.append(f"{field_name} (= {stripped!r})")

    problems = []
    if missing:
        problems.append("missing: " + ", ".join(missing))
    if empty:
        problems.append("empty: " + ", ".join(empty))
    if placeholder:
        problems.append("still a placeholder: " + ", ".join(placeholder))
    if problems:
        return BriefValidation(1, "incomplete", problems)

    bad = []
    if brief["brand"] not in registry.slugs():
        bad.append(f"brand must be one of {sorted(registry.slugs())}, got {brief['brand']!r}")
    if brief["format"] not in FORMATS:
        bad.append(f"format must be one of {sorted(FORMATS)}, got {brief['format']!r}")
    if brief["lifecycle_status"] not in LIFECYCLE_STATES:
        bad.append(f"lifecycle_status must be one of {sorted(LIFECYCLE_STATES)}, "
                   f"got {brief['lifecycle_status']!r}")
    if bad:
        return BriefValidation(1, "incomplete", bad)

    # caller-fit checks run BEFORE the approval check, same ordering as the
    # harvested validator — a mismatched caller gets a useful error rather
    # than a misleading "not approved"
    if require_brand and brief["brand"] != require_brand:
        return BriefValidation(3, "mismatch",
                               [f"wrong brand: brief is {brief['brand']!r}, "
                                f"caller expected {require_brand!r}"])
    if require_platform and brief["platform"] != require_platform:
        return BriefValidation(3, "mismatch",
                               [f"wrong platform: brief is {brief['platform']!r}, "
                                f"caller expected {require_platform!r}"])

    if brief["lifecycle_status"] not in APPROVED_LIKE:
        return BriefValidation(2, "not_approved",
                               [f"brief is not approved (lifecycle_status: "
                                f"{brief['lifecycle_status']}). A human sets this "
                                "via approve_content_record() — no command may."])
    return BriefValidation(0, "ok", [])


def _assert_active(registry: Registry, ctx: BrandContext, allow_parked: bool) -> None:
    if not allow_parked and registry.is_parked(ctx.name):
        raise ValueError(
            f"'{ctx.name}' is marked deprioritized/parked in the brand registry — "
            "pass allow_parked=True if this is intentional"
        )


def eligible_platforms(registry: Registry, ctx: BrandContext) -> list[str]:
    """Platforms this brand may draft for: configured in ENGAGE AND enabled
    in the harness account registry. Neither list alone is sufficient — see
    module docstring."""
    return [p for p in ctx.config["platforms"] if p in registry.enabled_platforms(ctx.name)]


def next_pillar(store: Store, ctx: BrandContext) -> str:
    """Least-used configured pillar, mirroring the harvested content
    calendar's own rule: 'keep roughly even over time.' Returns '' when the
    brand has no content_pillars configured."""
    pillars = ctx.config.get("content_pillars") or []
    if not pillars:
        return ""
    names = [p["name"] for p in pillars]
    counts = {n: 0 for n in names}
    for r in store.list_content_records(ctx.name):
        if r.pillar in counts:
            counts[r.pillar] += 1
    return min(counts, key=counts.get)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip(" -*#\t")
        if stripped:
            return stripped[:200]
    return ""


def transition(store: Store, record: ContentRecord, new_status: str,
               event_type: str = "", detail: dict | None = None,
               actor: str = "system") -> ContentRecord:
    """Generic lifecycle transition. 'approved' is refused here on
    purpose — reach it only through approve_content_record(), which
    requires a human approval record.

    Every transition's event carries brand/platform/from/to alongside
    whatever caller-specific detail is passed, so the audit trail never
    depends on a downstream join to answer "what changed, for what brand
    and platform" — used by both Phase 1 (drafting) and Phase 2
    (publish/operations.py), which reuses this function rather than
    building a second transition path."""
    if new_status == "approved":
        raise LifecycleError(
            "cannot reach 'approved' via transition() — use approve_content_record(), "
            "which requires a human approval record"
        )
    if new_status not in LIFECYCLE_STATES:
        raise LifecycleError(f"unknown lifecycle status {new_status!r}")
    prior_status = record.lifecycle_status
    allowed = LIFECYCLE_TRANSITIONS.get(prior_status, frozenset())
    if new_status not in allowed:
        raise LifecycleError(
            f"illegal transition for content record {record.id}: "
            f"{prior_status!r} -> {new_status!r} "
            f"(allowed: {sorted(allowed) or 'none — terminal state'})"
        )
    record.lifecycle_status = new_status
    if record.brief:
        record.brief["lifecycle_status"] = new_status
    store.save_content_record(record)
    full_detail = {"brand": record.brand, "platform": record.platform,
                   "from": prior_status, "to": new_status, **(detail or {})}
    store.record_content_event(record.id, event_type or f"transition:{new_status}",
                               full_detail, actor=actor)
    return record


def approve_content_record(store: Store, ctx: BrandContext, record: ContentRecord,
                           approver: str, note: str = "") -> ContentRecord:
    """The ONLY path to lifecycle_status == 'approved'. Requires a non-empty
    human approver identity, delegates the underlying Draft's approval to
    the existing single write path (approval/queue.py approve(), which
    hash-binds the exact text and refuses anything not PENDING)."""
    if not approver or not approver.strip():
        raise LifecycleError("approve_content_record requires a non-empty approver identity")
    if record.lifecycle_status != "review_required":
        raise LifecycleError(
            f"content record {record.id} is {record.lifecycle_status!r}, not "
            "'review_required' — cannot approve"
        )
    if not record.draft_id:
        raise LifecycleError(f"content record {record.id} has no underlying draft to approve")
    approve_draft(store, ctx, record.draft_id)  # existing hash-binding write path
    record.lifecycle_status = "approved"
    if record.brief:
        record.brief["lifecycle_status"] = "approved"
        record.brief["approved_by"] = approver
    store.save_content_record(record)
    store.record_content_event(record.id, "approval_recorded",
                               {"approver": approver, "note": note, "draft_id": record.draft_id},
                               actor=approver)
    return record


def create_brief(ctx: BrandContext, registry: Registry, material, platform: str,
                 backend: Callable[[str], str], *, audience: str, objective: str, cta: str,
                 pillar: str | None = None, format: str = "reel",
                 lead_magnet: str = "none", success_metric: str = "views",
                 allow_parked: bool = False) -> dict:
    """Assembles a schema-shaped brief from existing SourceMaterial. Calls
    the brand's own 'original' prompt once (the existing LLM chokepoint,
    canary-checked) — the same generated text becomes both the brief's
    caption/hook and, unmodified, the Draft.text that later goes through
    safety/gates.py. No brand-specific defaults live in this function; the
    engine stays brand-agnostic (DESIGN.md) — audience/objective/cta are
    caller-supplied, sourced from the brand's own material/config, never
    hardcoded here.

    Enforces the same parked-brand guard as create_original_content() even
    though this function is also callable on its own — a caller reaching
    for the lower-level building block must not lose the guard."""
    _assert_active(registry, ctx, allow_parked)
    eligible = eligible_platforms(registry, ctx)
    if platform not in eligible:
        raise ValueError(
            f"'{ctx.name}': platform {platform!r} is not both configured AND "
            f"enabled in the account registry (eligible: {eligible})"
        )
    generated = complete(ctx, "original", {"platform": platform, "material": material["text"]}, backend)
    hook = _first_line(generated) or _first_line(material["text"])
    return {
        "id": new_id(),
        "brand": ctx.name,
        "pillar": pillar or "",
        "audience": audience,
        "objective": objective,
        "core_insight": _first_line(material["text"]),
        "platform": platform,
        "format": format,
        "hook": hook,
        "hooks_considered": [],
        "cta": cta,
        "lead_magnet": lead_magnet,
        "brand_voice": f"brands/{ctx.name}/prompts/voice.md",
        "success_metric": success_metric,
        "outline": [],
        "caption": generated,
        "engagement_question": "",
        "sources": [material["id"]],
        "claims": [],
        "lifecycle_status": "draft",
        "approved_by": "",
        "approved_at": "",
    }


def _existing_open_record(store: Store, ctx: BrandContext, material_id: str, platform: str) -> ContentRecord | None:
    """An identical request — same material, same platform, not already
    rejected/cancelled — that's still open or already moving forward.
    Duplicate-guard for create_original_content(); the repurpose path
    already gets this for free from the existing reuse-ledger."""
    for r in store.list_content_records(ctx.name):
        if (r.kind == "original" and r.platform == platform
                and r.lifecycle_status not in ("rejected", "cancelled")
                and r.brief.get("sources") == [material_id]):
            return r
    return None


def create_original_content(store: Store, ctx: BrandContext, registry: Registry, material,
                            platform: str, backend: Callable[[str], str], *,
                            audience: str, objective: str, cta: str,
                            pillar: str | None = None, format: str = "reel",
                            lead_magnet: str = "none", success_metric: str = "views",
                            allow_parked: bool = False, force: bool = False) -> ContentRecord:
    """The main entry point: brief -> validate -> draft -> gates -> record.
    Creates a draft only — no scheduling, publishing, browser automation,
    account connection, credentials, comments, or DMs happen here or
    anywhere in this module.

    Refuses to create a second original content record for the same
    (material, platform) pair while an earlier one is still open — an
    identical request should not silently pile up duplicates. Pass
    force=True for a deliberate second pass at the same source (e.g. a
    reworked angle)."""
    _assert_active(registry, ctx, allow_parked)
    if not force:
        dup = _existing_open_record(store, ctx, material["id"], platform)
        if dup is not None:
            raise ValueError(
                f"content record {dup.id} already exists for material "
                f"{material['id']!r} on {platform!r} (status: {dup.lifecycle_status}) — "
                "pass force=True to create another anyway"
            )
    pillar = pillar if pillar is not None else next_pillar(store, ctx)
    brief = create_brief(ctx, registry, material, platform, backend,
                         audience=audience, objective=objective, cta=cta,
                         pillar=pillar, format=format, lead_magnet=lead_magnet,
                         success_metric=success_metric, allow_parked=allow_parked)

    record = ContentRecord(id=new_id(), brand=ctx.name, platform=platform, kind="original",
                           brief=brief, pillar=brief["pillar"], lifecycle_status="draft")
    store.save_content_record(record)
    store.record_content_event(record.id, "brief_created",
                               {"brief_id": brief["id"], "pillar": brief["pillar"],
                                "material_id": material["id"]})

    bv = validate_brief(brief, registry)
    store.record_content_event(record.id, "brief_validated",
                               {"code": bv.code, "status": bv.status, "problems": bv.problems})
    if bv.code == 1:
        return transition(store, record, "rejected", "brief_incomplete", {"problems": bv.problems})

    draft = Draft(id=new_id(), brand=ctx.name, kind="original", platform=platform,
                  text=brief["caption"], material_id=material["id"])
    draft = submit(store, ctx, draft, (material["text"],), backend)
    record.draft_id = draft.id
    store.save_content_record(record)
    store.record_content_event(record.id, "gates_run",
                               {"draft_id": draft.id, "draft_status": draft.status,
                                "gate_reasons": draft.gate_reasons})

    if draft.status == "BLOCKED":
        return transition(store, record, "rejected", "gates_blocked",
                          {"gate_reasons": draft.gate_reasons})
    return transition(store, record, "review_required", "awaiting_approval",
                      {"draft_id": draft.id})


def repurpose_for_registry(store: Store, ctx: BrandContext, registry: Registry, material,
                           backend: Callable[[str], str], primary_platform: str,
                           allow_parked: bool = False) -> list[ContentRecord]:
    """Platform-native fan-out, draft creation only. Reuses the existing,
    tested repurpose() machinery (per-platform LLM call through
    prompts/repurpose.md — never a copy-paste of the primary draft's text).
    Restricted to platforms that are BOTH configured for this brand AND
    enabled in the account registry, excluding whichever platform was
    already drafted natively. A disabled, unconnected, or non-applicable
    account row is never included."""
    _assert_active(registry, ctx, allow_parked)
    eligible = [p for p in eligible_platforms(registry, ctx) if p != primary_platform]
    records: list[ContentRecord] = []
    if not eligible:
        return records
    drafts, _skipped = repurpose(store, ctx, material, backend, eligible)
    for d in drafts:
        d = submit(store, ctx, d, (material["text"],), backend)
        record = ContentRecord(id=new_id(), brand=ctx.name, platform=d.platform, kind="repurpose",
                               draft_id=d.id, lifecycle_status="draft")
        store.save_content_record(record)
        store.record_content_event(record.id, "repurpose_drafted",
                                   {"draft_id": d.id, "material_id": material["id"],
                                    "draft_status": d.status})
        if d.status == "BLOCKED":
            transition(store, record, "rejected", "gates_blocked", {"gate_reasons": d.gate_reasons})
        else:
            transition(store, record, "review_required", "awaiting_approval", {"draft_id": d.id})
        records.append(record)
    return records


def write_handoff(store: Store, record: ContentRecord, target: str, **extra) -> dict:
    """A structured REQUEST record for a future consumer (Publishing
    Operations, Engagement, Performance Intelligence) — persisted to the
    content_events audit trail, never sent anywhere. No external action of
    any kind, and no authority to publish: the payload says so explicitly,
    since a future agent reading only the payload (not this docstring)
    must not be able to mistake it for an instruction.

    A 'publishing' handoff additionally requires the record to be
    approved-or-later AND the underlying draft's current text to still
    match the SHA-256 bound at approval time — a rejected/paused/failed/
    cancelled record, or one edited after approval, can never be handed off
    as publishable. The handoff is bound to that same hash, so a later edit
    invalidates a handoff already written exactly the way it already
    invalidates the approval itself.

    Idempotent: an identical (record, target, content_hash) handoff that
    was already written is not duplicated — the existing payload is
    returned instead of appending a second event."""
    if target not in HANDOFF_TARGETS:
        raise ValueError(f"unknown handoff target {target!r} (expected one of {HANDOFF_TARGETS})")

    draft = store.get_draft(record.draft_id, record.brand) if record.draft_id else None
    content_hash = draft.hash if draft else None

    if target == "publishing":
        if record.lifecycle_status not in APPROVED_LIKE:
            raise LifecycleError(
                f"content record {record.id} is {record.lifecycle_status!r} — only "
                f"{sorted(APPROVED_LIKE)} may be handed off to publishing"
            )
        approved_hash = store.get_approval_hash(record.draft_id)
        if draft is None or approved_hash is None or approved_hash != draft.hash:
            raise LifecycleError(
                f"content record {record.id}'s draft has no matching approval "
                "hash — an edit after approval must be re-approved before handoff"
            )

    existing = [e for e in store.list_content_events(record.id)
               if e["event_type"] == f"handoff:{target}"
               and e["detail"].get("content_hash") == content_hash]
    if existing:
        return existing[-1]["detail"]

    payload = {
        "content_record_id": record.id,
        "brand": record.brand,
        "platform": record.platform,
        "kind": record.kind,
        "draft_id": record.draft_id,
        "content_hash": content_hash,
        "lifecycle_status": record.lifecycle_status,
        "pillar": record.pillar,
        "target": target,
        "authorization": (
            "NONE — this is a request record for a future agent to evaluate, "
            "not an instruction to publish, schedule, or take any external "
            "action automatically."
        ),
        **extra,
    }
    store.record_content_event(record.id, f"handoff:{target}", payload)
    return payload
