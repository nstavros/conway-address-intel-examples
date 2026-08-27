"""Legacy asset intake — a dedicated, auditable path for content that was
created BEFORE the ENGAGE content-record and approval pipeline existed.

The honesty requirement is the point of this module. These assets did not
pass through drafting, gates, or approval, and this code never pretends
otherwise: intake produces a record explicitly marked
`kind="legacy_asset_intake"`, carrying written provenance that says the
asset pre-dates the workflow and is being retrofitted for controlled future
publishing. No historic approval is ever fabricated, no approval row is
back-dated, and nothing here implies the asset was previously approved.

What intake DOES reuse, unchanged, is every downstream guarantee: the same
lifecycle states and transitions, the same safety gates, the same
single-write-path approval with SHA-256 hash-binding, the same duplicate
protection, the same append-only event log, and the same handoff
restrictions. A legacy record is therefore *harder* to publish than new
content, never easier — it carries an extra binding (the asset file's own
hash) that new text-only content does not have.

TWO HASHES, both binding:
  * `asset_sha256`  — the exact bytes of the video file
  * `draft.hash`    — the exact final metadata (title + description)
Approval requires the owner to echo BOTH, and re-hashes the file from disk
at approval time, so a file swapped between intake and approval is caught
rather than silently inherited.

Nothing in this module uploads, schedules, publishes, opens a browser, or
makes a network call."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..approval.queue import submit
from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.models import ContentRecord, Draft, LifecycleError, new_id
from ..core.store import Store
from .originals import (
    _assert_active,
    approve_content_record,
    eligible_platforms,
    transition,
)

LEGACY_INTAKE_KIND = "legacy_asset_intake"

# Read in chunks so a large video is hashed without loading it into memory.
_HASH_CHUNK = 1024 * 1024

VISIBILITY_VALUES = ("public", "unlisted", "private")

# Metadata that must be present and non-empty before a legacy record may
# even reach review. Absence is a refusal at intake, not a warning deferred
# to publish time.
REQUIRED_FINAL_METADATA = ("final_title", "final_description", "visibility", "made_for_kids")


class LegacyIntakeError(Exception):
    """Intake refused. No record is created and nothing is persisted."""


@dataclass(frozen=True)
class LegacyAsset:
    path: Path
    sha256: str
    size_bytes: int
    suffix: str


def hash_asset(asset_path: Path | str) -> LegacyAsset:
    """Hash the exact file. Refuses anything that cannot be found, read, or
    that is empty — an unreadable or zero-byte asset is never given a
    record."""
    p = Path(asset_path).expanduser()
    if not p.exists():
        raise LegacyIntakeError(f"asset not found: {p}")
    if not p.is_file():
        raise LegacyIntakeError(f"asset is not a regular file: {p}")
    h = hashlib.sha256()
    total = 0
    try:
        with p.open("rb") as fh:
            while chunk := fh.read(_HASH_CHUNK):
                h.update(chunk)
                total += len(chunk)
    except OSError as e:
        raise LegacyIntakeError(f"cannot read asset {p}: {e.strerror}") from None
    if total == 0:
        raise LegacyIntakeError(f"asset {p} is empty (0 bytes) — refusing intake")
    return LegacyAsset(path=p, sha256=h.hexdigest(), size_bytes=total, suffix=p.suffix.lower())


def _registry_channel_id(registry: Registry, ctx: BrandContext, platform: str) -> str:
    row = registry.account_row(ctx.name, platform) or {}
    return (row.get("channel_id") or "").strip()


def _duplicate_asset(store: Store, ctx: BrandContext, sha256: str) -> ContentRecord | None:
    """Same brand, same asset bytes, already on record in any non-terminal
    state — the asset-level counterpart of the existing content-hash
    duplicate check."""
    for other in store.list_content_records(ctx.name):
        if other.lifecycle_status in ("rejected", "cancelled"):
            continue
        existing = ((other.brief or {}).get("legacy_asset") or {}).get("sha256")
        if existing and existing == sha256:
            return other
    return None


def intake_legacy_asset(store: Store, ctx: BrandContext, registry: Registry, *,
                        asset_path: Path | str,
                        platform: str,
                        channel_id: str,
                        working_title: str,
                        final_title: str,
                        final_description: str,
                        visibility: str,
                        made_for_kids: bool,
                        backend: Callable[[str], str],
                        cta: str = "",
                        provenance: str = "",
                        source_corpus: tuple[str, ...] = (),
                        actor: str = "legacy_asset_intake") -> ContentRecord:
    """Retrofit one pre-existing asset into a reviewable content record.

    Produces `review_required` at best — never `approved`. Every refusal
    happens before anything is written, so a rejected intake leaves no
    partial record behind."""
    _assert_active(registry, ctx, False)  # parked brands never take intake

    eligible = eligible_platforms(registry, ctx)
    if platform not in eligible:
        raise LegacyIntakeError(
            f"platform {platform!r} is not eligible for {ctx.name!r} (eligible: {eligible})"
        )

    # Channel binding, checked against the canonical registry — never the
    # handle, never the caller's word alone.
    expected_channel = _registry_channel_id(registry, ctx, platform)
    if not expected_channel:
        raise LegacyIntakeError(
            f"no channel_id recorded in the account registry for {ctx.name}/{platform} — "
            "cannot bind an asset to an unverified channel"
        )
    if channel_id.strip() != expected_channel:
        raise LegacyIntakeError(
            f"channel id {channel_id!r} does not match the registry's verified channel "
            f"{expected_channel!r} for {ctx.name}/{platform} — refusing intake"
        )

    if visibility not in VISIBILITY_VALUES:
        raise LegacyIntakeError(
            f"visibility must be one of {VISIBILITY_VALUES}, got {visibility!r} — "
            "visibility is always explicit and never defaulted"
        )
    if not isinstance(made_for_kids, bool):
        raise LegacyIntakeError(
            "made_for_kids must be an explicit boolean — 'unset' is a hard blocker, "
            "never an inherited channel default"
        )
    for field_name, value in (("working_title", working_title), ("final_title", final_title),
                              ("final_description", final_description)):
        if not str(value or "").strip():
            raise LegacyIntakeError(f"{field_name} is required and must be non-empty")

    asset = hash_asset(asset_path)

    dup = _duplicate_asset(store, ctx, asset.sha256)
    if dup is not None:
        raise LegacyIntakeError(
            f"asset already on record as content record {dup.id} "
            f"({dup.lifecycle_status}) — same exact bytes, refusing a second intake"
        )

    if not provenance.strip():
        provenance = (
            "Asset pre-dates the ENGAGE content-record and approval pipeline. It was "
            "produced and (where applicable) published through the earlier manual "
            "workflow, and is being retrofitted here so it can be brought under "
            "controlled approval and, in future, controlled publishing. It has NOT "
            "previously passed through ENGAGE drafting, gates, or approval."
        )

    brief = {
        "id": new_id(),
        "brand": ctx.name,
        "platform": platform,
        "kind": LEGACY_INTAKE_KIND,
        "format": "short",
        "working_title": working_title.strip(),
        "final_title": final_title.strip(),
        "final_description": final_description.strip(),
        "cta": cta.strip(),
        "caption": final_title.strip(),  # what the gates and constraint check read
        "visibility": visibility,
        "made_for_kids": made_for_kids,
        "channel_id": expected_channel,
        "legacy_asset": {
            "path": str(asset.path),
            "sha256": asset.sha256,
            "size_bytes": asset.size_bytes,
            "suffix": asset.suffix,
        },
        "provenance": provenance.strip(),
        "previously_approved_through_engage": False,
        "lifecycle_status": "draft",
    }

    record = ContentRecord(id=new_id(), brand=ctx.name, platform=platform,
                           kind=LEGACY_INTAKE_KIND, brief=brief, pillar="",
                           lifecycle_status="draft")
    store.save_content_record(record)
    store.record_content_event(
        record.id, "legacy_asset_intake_created",
        {"asset_path": str(asset.path), "asset_sha256": asset.sha256,
         "size_bytes": asset.size_bytes, "suffix": asset.suffix,
         "channel_id": expected_channel, "working_title": brief["working_title"],
         "visibility": visibility, "made_for_kids": made_for_kids,
         "previously_approved_through_engage": False,
         "provenance": brief["provenance"]},
        actor=actor,
    )

    # The draft's text IS the exact final metadata, so the existing approval
    # hash-binding binds the metadata a human actually signs off on.
    metadata_text = f"{brief['final_title']}\n\n{brief['final_description']}".strip()
    draft = Draft(id=new_id(), brand=ctx.name, kind="original", platform=platform,
                  text=metadata_text)
    draft = submit(store, ctx, draft, source_corpus, backend)
    record.draft_id = draft.id
    store.save_content_record(record)
    store.record_content_event(record.id, "gates_run",
                               {"draft_id": draft.id, "draft_status": draft.status,
                                "gate_reasons": draft.gate_reasons}, actor=actor)

    if draft.status == "BLOCKED":
        return transition(store, record, "rejected", "gates_blocked",
                          {"gate_reasons": draft.gate_reasons}, actor=actor)

    return transition(store, record, "review_required", "awaiting_owner_approval",
                      {"draft_id": draft.id, "asset_sha256": asset.sha256,
                       "metadata_hash": draft.hash}, actor=actor)


def missing_before_approval(store: Store, record: ContentRecord) -> list[str]:
    """Everything still standing between this record and approvability.
    Pure read — changes nothing."""
    brief = record.brief or {}
    gaps: list[str] = []
    if record.kind != LEGACY_INTAKE_KIND:
        gaps.append(f"record kind is {record.kind!r}, not {LEGACY_INTAKE_KIND!r}")
    for key in REQUIRED_FINAL_METADATA:
        if brief.get(key) in (None, ""):
            gaps.append(f"missing final metadata: {key}")
    asset = brief.get("legacy_asset") or {}
    if not asset.get("sha256"):
        gaps.append("missing asset sha256 binding")
    else:
        p = Path(asset.get("path", ""))
        if not p.exists():
            gaps.append(f"asset file no longer present at {p}")
    if not record.draft_id:
        gaps.append("no underlying draft to approve")
    elif store.get_approval_hash(record.draft_id) is None:
        gaps.append("owner approval of the exact asset hash and final metadata (not yet given)")
    if record.lifecycle_status != "review_required":
        gaps.append(f"lifecycle is {record.lifecycle_status!r}, not 'review_required'")
    return gaps


def approval_view(store: Store, record: ContentRecord) -> dict:
    """Exactly what the owner is asked to approve — the two hashes they must
    confirm, the final metadata verbatim, and the declarations. Read-only:
    building this view never approves anything."""
    brief = record.brief or {}
    asset = brief.get("legacy_asset") or {}
    draft = store.get_draft(record.draft_id, record.brand) if record.draft_id else None
    return {
        "content_record_id": record.id,
        "kind": record.kind,
        "brand": record.brand,
        "platform": record.platform,
        "channel_id": brief.get("channel_id", ""),
        "lifecycle_status": record.lifecycle_status,
        "asset_path": asset.get("path", ""),
        "asset_sha256": asset.get("sha256", ""),
        "asset_size_bytes": asset.get("size_bytes"),
        "final_title": brief.get("final_title", ""),
        "final_description": brief.get("final_description", ""),
        "cta": brief.get("cta", ""),
        "visibility": brief.get("visibility", ""),
        "made_for_kids": brief.get("made_for_kids"),
        "metadata_hash": draft.hash if draft else "",
        "previously_approved_through_engage": False,
        "provenance": brief.get("provenance", ""),
        "missing_before_approval": missing_before_approval(store, record),
        "what_approval_authorizes": (
            "Approval binds this exact asset hash and this exact metadata, and moves the "
            "record to 'approved'. It does NOT publish, schedule, or upload anything: no "
            "authorized upload sink exists, the account is live-disabled, and platform "
            "constraints are still unknown."
        ),
    }


def approve_legacy_asset(store: Store, ctx: BrandContext, record: ContentRecord,
                         approver: str, *, asset_sha256: str, metadata_hash: str,
                         note: str = "") -> ContentRecord:
    """Owner approval of a legacy record. Stricter than
    approve_content_record(): the approver must echo BOTH hashes, and the
    asset is re-hashed from disk so a file swapped since intake is caught
    here rather than inherited silently."""
    if record.kind != LEGACY_INTAKE_KIND:
        raise LifecycleError(
            f"content record {record.id} is kind {record.kind!r} — approve_legacy_asset is "
            f"only for {LEGACY_INTAKE_KIND!r} records"
        )
    brief = record.brief or {}
    asset = brief.get("legacy_asset") or {}
    recorded_sha = asset.get("sha256", "")

    if not asset_sha256 or asset_sha256.strip() != recorded_sha:
        raise LifecycleError(
            f"asset hash confirmation does not match the record: expected {recorded_sha!r}. "
            "Approval requires confirming the exact asset hash."
        )

    draft = store.get_draft(record.draft_id, ctx.name) if record.draft_id else None
    if draft is None:
        raise LifecycleError(f"content record {record.id} has no underlying draft to approve")
    if not metadata_hash or metadata_hash.strip() != draft.hash:
        raise LifecycleError(
            f"metadata hash confirmation does not match the current draft: expected "
            f"{draft.hash!r}. The metadata may have changed since it was shown for approval."
        )

    # Re-hash from disk — the file may have been replaced since intake.
    current = hash_asset(asset.get("path", ""))
    if current.sha256 != recorded_sha:
        raise LifecycleError(
            f"the asset file at {asset.get('path')} has CHANGED since intake "
            f"(recorded {recorded_sha[:16]}…, now {current.sha256[:16]}…) — refusing to "
            "approve bytes that were never reviewed. Re-run intake for the new file."
        )

    store.record_content_event(
        record.id, "legacy_asset_reverified_at_approval",
        {"asset_sha256": current.sha256, "size_bytes": current.size_bytes,
         "metadata_hash": draft.hash}, actor=approver,
    )
    # Delegates to the single, unchanged approval write path — same
    # hash-binding, same three-layer guard, no shortcut.
    return approve_content_record(store, ctx, record, approver=approver, note=note)
