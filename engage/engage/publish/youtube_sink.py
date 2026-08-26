"""YouTubeUploadSink — one verified YouTube cell only. DISABLED BY DEFAULT.

Which cell is decided by DATA, not by this file: the sink refuses any
brand/platform whose account-registry row carries no verified `channel_id`.
Exactly one row has one today, so exactly one cell is reachable, and
widening that is a reviewed registry decision rather than an engine edit.
This is also what keeps the engine brand-agnostic (DESIGN.md, enforced by
tests/test_isolation.py) — no brand name appears anywhere in this module.


This sink cannot upload anything in this build. It makes no network call,
has no browser fallback, performs no OAuth, and handles no token material.
Its transport is injectable and the only implementation that exists is a
test-only mock. What it DOES is enumerate — precisely, and in one place —
every condition that must hold before a first live upload could ever be
considered, and refuse loudly while any of them does not.

Eight independent refusal conditions, all checked, all reported together
rather than one-at-a-time (so the operator sees the whole gap, not the
first item of a queue):

  1. sink-specific enable flag absent
  2. the exact registry row is not live-enabled
  3. target channel id != the registry's verified channel id
  4. content record not approved, or its draft hash != the approval hash
  5. final asset / title / description / explicit visibility / explicit
     made-for-kids declaration missing
  6. platform constraints unknown or unvalidated at execution time
  7. duplicate asset or content hash already uploaded
  8. no credential, or no transport

Condition 6 is satisfied only by a ConstraintSource carrying its own
provenance (source URLs + verification date). As of 2026-08-21 one exists,
sourced from Google's own documentation — see
harness/social/registry/youtube-constraints.yaml and DEC-SM-020. The sink
still will not accept a remembered or guessed limit: a limit absent from
the source file is reported unverifiable, and an UNMEASURED asset fails
closed rather than being assumed compliant.

Deliberately NOT enforced: Shorts aspect ratio. It is not officially
documented anywhere, so checking it would launder an assumption into the
system as though it were a platform rule.

This sink is NOT in operations.NON_ACTING_SINKS, so it is additionally
gated by require_live_enabled() before it is ever reached. The live-status
check here is deliberate defence in depth, not redundancy: a caller that
constructs this sink directly, bypassing operations.py, still cannot act."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from ..config.registry import Registry
from ..core.brand import BrandContext
from ..core.store import Store
from ..drafting.legacy_intake import LEGACY_INTAKE_KIND, hash_asset
from .credentials import CredentialError, load_youtube_credentials
from .reconcile import already_live
from .sinks import PublishSink, SinkResult, SinkUnavailableError
from .youtube_transport import UploadAuthorization, build_video_resource

BOUND_PLATFORM = "youtube"

REQUIRED_METADATA_FIELDS = ("final_title", "final_description", "visibility", "made_for_kids")


class UploadRefused(SinkUnavailableError):
    """Every reason the upload cannot proceed, together."""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        joined = "\n  - ".join(self.reasons)
        super().__init__(
            f"YouTubeUploadSink refused ({len(self.reasons)} unmet condition(s)):\n  - {joined}"
        )


DEFAULT_CONSTRAINTS_PATH = (
    Path.home() / ".claude" / "harness" / "social" / "registry" / "youtube-constraints.yaml"
)


@dataclass
class ConstraintSource:
    """Platform limits that carry their own provenance.

    Deliberately has no defaults and no built-in values: `verified_at` and
    `sources` must be supplied, so a limit cannot enter this system without
    a record of where it came from and when. `from_file()` is the only
    practical way to build one, and it refuses a file missing either.

    Everything here was read from Google's own documentation. Anything the
    official docs do not conclusively establish stays in `unknown` and is
    never enforced — notably Shorts aspect ratio, which is NOT officially
    documented and must not be checked."""

    verified_at: str
    sources: dict
    limits: dict
    required_by_api: list
    required_by_internal_policy: list
    unknown: list

    @classmethod
    def from_file(cls, path: Path | str | None = None) -> "ConstraintSource":
        p = Path(path) if path else DEFAULT_CONSTRAINTS_PATH
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except OSError as e:
            raise ConstraintSourceError(f"cannot read constraint source {p}: {e.strerror}") from None
        except yaml.YAMLError as e:
            raise ConstraintSourceError(f"{p} is not valid YAML: {e}") from None
        if not isinstance(doc, dict):
            raise ConstraintSourceError(f"{p} must be a YAML mapping")
        for key in ("verified_at", "sources", "limits"):
            if not doc.get(key):
                raise ConstraintSourceError(
                    f"{p} is missing {key!r} — a constraint source without provenance is "
                    "refused; limits must carry where they came from and when"
                )
        return cls(
            verified_at=str(doc["verified_at"]),
            sources=doc["sources"],
            limits=doc["limits"],
            required_by_api=doc.get("required_by_api") or [],
            required_by_internal_policy=doc.get("required_by_internal_policy") or [],
            unknown=doc.get("unknown") or [],
        )

    def _limit(self, key):
        entry = self.limits.get(key)
        return entry.get("value") if isinstance(entry, dict) else None

    def violations(self, brief: dict, asset: dict) -> list[str]:
        """Check a record against the DOCUMENTED limits only. A limit that is
        absent from the source file is not checked and is reported as
        unverifiable rather than silently passed."""
        out: list[str] = []

        title = brief.get("final_title") or ""
        max_title = self._limit("title_max_characters")
        if max_title is None:
            out.append("title_max_characters unknown in the constraint source")
        elif not title.strip():
            out.append("snippet.title is empty — documented error invalidTitle")
        elif len(title) > max_title:
            out.append(f"title is {len(title)} characters, documented limit {max_title}")

        # BYTES, not characters — the source file says so explicitly.
        description = brief.get("final_description") or ""
        max_desc = self._limit("description_max_bytes")
        if max_desc is None:
            out.append("description_max_bytes unknown in the constraint source")
        else:
            nbytes = len(description.encode("utf-8"))
            if nbytes > max_desc:
                out.append(f"description is {nbytes} bytes, documented limit {max_desc}")

        allowed = self._limit("privacy_status_values") or []
        if brief.get("visibility") not in allowed:
            out.append(
                f"visibility {brief.get('visibility')!r} is not one of the documented "
                f"privacyStatus values {allowed}"
            )

        max_bytes = self._limit("max_file_size_bytes")
        size = asset.get("size_bytes")
        if max_bytes is not None and size is not None and size > max_bytes:
            out.append(f"asset is {size} bytes, documented maximum {max_bytes}")

        # Shorts duration/resolution. An UNMEASURED asset fails closed: not
        # knowing a value and the value being fine are different facts, and
        # this module never resolves that difference in favour of uploading.
        # Measurement is done by the caller (see cli / probe_media) so the
        # engine keeps no shell-out or media-decoding capability of its own.
        max_secs = self._limit("shorts_max_duration_seconds")
        duration = asset.get("duration_seconds")
        if duration is None:
            out.append(
                "asset duration has not been measured — cannot verify against the documented "
                "Shorts maximum; supply measured media facts to the preflight"
            )
        elif max_secs is not None and duration > max_secs:
            out.append(f"duration {duration}s exceeds documented Shorts maximum {max_secs}s")

        max_h = self._limit("shorts_max_resolution_height")
        height, width = asset.get("height"), asset.get("width")
        if height is None or width is None:
            out.append(
                "asset dimensions have not been measured — cannot verify against the "
                "documented Shorts maximum resolution"
            )
        elif max_h is not None and min(height, width) > max_h:
            out.append(
                f"resolution {width}x{height} exceeds the documented Shorts maximum "
                f"of {max_h}p on its shorter side"
            )
        # NOTE: aspect ratio is deliberately NOT checked. It is not
        # officially documented (see `unknown` in the source file).
        return out


class ConstraintSourceError(Exception):
    """The constraint source is absent, malformed, or lacks provenance."""


class MockTransport:
    """TEST-ONLY. Records what WOULD have been sent and returns a canned
    response. Never performs I/O of any kind. Its presence in a test proves
    the surrounding refusal logic ran; it can never stand in for a real
    upload because no real transport exists to swap it for."""

    def __init__(self, response: dict | None = None):
        self.calls: list[dict] = []
        self.response = response or {"video_id": "MOCK_VIDEO_ID", "mock": True}

    def upload(self, payload: dict) -> dict:
        self.calls.append(payload)
        return dict(self.response)


class YouTubeUploadSink(PublishSink):
    name = "youtube_upload"

    def __init__(self, registry: Registry, ctx: BrandContext, *,
                 enabled: bool = False,
                 transport: MockTransport | None = None,
                 credential_path: Path | str | None = None,
                 constraint_source: ConstraintSource | None = None,
                 store: Store | None = None,
                 verify_reader: Callable[[], list] | None = None):
        self.registry = registry
        self.ctx = ctx
        self.enabled = bool(enabled)
        self.transport = transport
        self.credential_path = credential_path
        self.constraint_source = constraint_source
        self.store = store
        # Future read-back verification reuses the EXISTING public RSS path
        # (ingest/youtube_rss.py) rather than requesting a broader scope.
        # Not called in this phase.
        self.verify_reader = verify_reader

    # -- refusal analysis ---------------------------------------------------
    def refusal_reasons(self, record=None, media: dict | None = None) -> list[str]:
        """Every unmet condition. A pure check — never acts, never mutates,
        and safe to call for reporting.

        `media` carries measured facts about the asset (duration_seconds,
        width, height) produced OUTSIDE this module, so the engine holds no
        media-decoding or shell-out capability. Omitting it does not pass
        those checks — it fails them as unverified."""
        reasons: list[str] = []

        if record is not None and record.platform != BOUND_PLATFORM:
            reasons.append(
                f"this sink handles {BOUND_PLATFORM} only "
                f"(got {getattr(record, 'platform', '?')})"
            )

        # Cell restriction lives in DATA, not in this module: the sink
        # refuses any cell whose registry row carries no verified
        # channel_id. Exactly one row has one, so exactly one cell is
        # reachable — and widening that is a registry decision, reviewed as
        # such, rather than an edit to engine code. This also keeps the
        # engine brand-agnostic per DESIGN.md (tests/test_isolation.py).
        if not (self.registry.account_row(self.ctx.name, BOUND_PLATFORM) or {}).get("channel_id"):
            reasons.append(
                f"no verified channel_id in the account registry for "
                f"{self.ctx.name}/{BOUND_PLATFORM} — this sink only operates on a cell whose "
                "channel identity has been verified and recorded"
            )

        if not self.enabled:
            reasons.append(
                "sink enable flag is absent — YouTubeUploadSink is disabled by default and "
                "must be explicitly enabled by whoever authorizes a first upload"
            )

        if not self.registry.is_live_enabled(self.ctx.name, BOUND_PLATFORM):
            row = self.registry.account_row(self.ctx.name, BOUND_PLATFORM) or {}
            reasons.append(
                f"registry row {self.ctx.name}/{BOUND_PLATFORM} is not live-enabled "
                f"(live_status={row.get('live_status')!r}) — a connector, credential, or "
                "flag never authorizes a live action; only the registry row does"
            )

        expected_channel = (self.registry.account_row(self.ctx.name, BOUND_PLATFORM) or {}).get("channel_id", "")
        if record is not None:
            brief = record.brief or {}
            target_channel = (brief.get("channel_id") or "").strip()
            if not expected_channel:
                reasons.append("no channel_id in the account registry — nothing to bind to")
            elif target_channel != expected_channel:
                reasons.append(
                    f"target channel {target_channel!r} != registry channel "
                    f"{expected_channel!r} — refusing to upload to an unverified channel"
                )

            if record.lifecycle_status != "approved":
                reasons.append(
                    f"content record is {record.lifecycle_status!r}, not 'approved'"
                )
            if self.store is not None and record.draft_id:
                draft = self.store.get_draft(record.draft_id, record.brand)
                approved_hash = self.store.get_approval_hash(record.draft_id)
                if draft is None or approved_hash is None:
                    reasons.append("no approval hash bound to this record's draft")
                elif approved_hash != draft.hash:
                    reasons.append(
                        "draft hash != approval hash — content changed since approval"
                    )
            elif self.store is not None:
                reasons.append("record has no draft, so nothing is hash-bound")

            for field in REQUIRED_METADATA_FIELDS:
                if brief.get(field) in (None, ""):
                    reasons.append(f"missing required final metadata: {field}")
            if not isinstance(brief.get("made_for_kids"), bool):
                reasons.append(
                    "made_for_kids must be an explicit boolean declaration — 'unset' is a "
                    "hard blocker and is never inherited from a channel default"
                )
            if brief.get("visibility") not in ("public", "unlisted", "private"):
                reasons.append("visibility must be explicitly set — never defaulted")

            asset = brief.get("legacy_asset") or {}
            asset_path = asset.get("path", "")
            if record.kind == LEGACY_INTAKE_KIND:
                if not asset_path:
                    reasons.append("no asset path on record")
                elif not Path(asset_path).exists():
                    reasons.append(f"asset file not present at {asset_path}")
                else:
                    try:
                        current = hash_asset(asset_path)
                        if current.sha256 != asset.get("sha256"):
                            reasons.append(
                                "asset file bytes changed since approval — refusing to upload "
                                "content that was never reviewed"
                            )
                    except Exception as e:  # noqa: BLE001 — any read problem is a refusal
                        reasons.append(f"cannot re-hash asset: {e}")

            # Constraints, checked at EXECUTION time against a source that
            # carries its own provenance. Without one, refuse — limits must
            # never come from memory or a cached guess.
            if self.constraint_source is None:
                reasons.append(
                    "no ConstraintSource supplied — limits must be validated against a current "
                    "official source at execution time, never from memory or a cached guess"
                )
            else:
                measured = {**asset, **(media or {})}
                reasons.extend(self.constraint_source.violations(brief, measured))

            if self.store is not None:
                dup = self._duplicate(record)
                if dup is not None:
                    reasons.append(
                        f"duplicate: content record {dup} already holds this asset/content hash"
                    )

                # The check above scans OUR OWN records only, so it answers
                # "did we publish this", not "is this already live". Anything
                # posted by hand or by another tool is invisible to it — which
                # is how an upload of an already-public video came within one
                # approval of happening. Consult observed platform state too.
                external = already_live(
                    self.store, record.brand, record.platform,
                    (brief.get("final_title") or ""))
                if external is not None and external.get("source") == "external_post":
                    how = external.get("matched_on")
                    match_desc = ("the same title" if how == "exact_title"
                                  else "the same title apart from its hashtags")
                    reasons.append(
                        f"duplicate: a post with {match_desc} is already live on the platform "
                        f"({external.get('platform_post_id')}: {external.get('title')!r}) but "
                        "was not published through this system — refusing a second publish. "
                        "Reconcile first (publish/reconcile.py) if this is believed stale."
                    )

        try:
            load_youtube_credentials(self.credential_path, brand=self.ctx.name)
        except CredentialError as e:
            reasons.append(f"credential unavailable: {e}")

        if self.transport is None:
            reasons.append(
                "no transport — this build ships no real transport and makes no network calls"
            )

        return reasons

    def _duplicate(self, record) -> str | None:
        brief = record.brief or {}
        sha = (brief.get("legacy_asset") or {}).get("sha256")
        for other in self.store.list_content_records(record.brand):
            if other.id == record.id:
                continue
            if other.lifecycle_status not in ("publishing", "published", "verified"):
                continue
            other_sha = ((other.brief or {}).get("legacy_asset") or {}).get("sha256")
            if sha and other_sha == sha:
                return other.id
        return None

    def _refuse(self, record=None):
        raise UploadRefused(self.refusal_reasons(record))

    def mint_authorization(self, record, media: dict | None = None) -> UploadAuthorization:
        """Re-check EVERY gate and, only if all pass, mint the capability
        token the transport demands. This is the sole place an
        UploadAuthorization can come from, which is what makes the transport
        undrivable directly.

        Gates are re-run here rather than trusted from an earlier call: this
        is the moment immediately before the network, and a cell can be
        switched off, a file swapped, or an approval revoked in between."""
        reasons = self.refusal_reasons(record, media=media)
        if reasons:
            raise UploadRefused(reasons)
        brief = record.brief or {}
        asset = brief.get("legacy_asset") or {}
        draft = self.store.get_draft(record.draft_id, record.brand)
        return UploadAuthorization(
            content_record_id=record.id,
            channel_id=brief.get("channel_id", ""),
            asset_path=asset.get("path", ""),
            asset_sha256=asset.get("sha256", ""),
            metadata_hash=draft.hash,
            minted_at=int(time.time()),
            gate_report=("all_gates_passed",),
        )

    def build_request_body(self, record) -> dict:
        """The exact videos.insert body this record would send. Pure — safe
        to call for review without any authorization."""
        brief = record.brief or {}
        return build_video_resource(
            title=brief.get("final_title", ""),
            description=brief.get("final_description", ""),
            privacy_status=brief.get("visibility", ""),
            self_declared_made_for_kids=brief.get("made_for_kids"),
        )

    # -- PublishSink interface ---------------------------------------------
    # Every method refuses. None is reachable past _refuse() in this build,
    # because condition 6 (no ConstraintSource) and condition 8 (no
    # credential, no transport) cannot currently be satisfied.
    def schedule(self, record, scheduled_at: int, timezone: str) -> SinkResult:
        self._refuse(record)

    def publish(self, record) -> SinkResult:
        self._refuse(record)

    def verify(self, record) -> SinkResult:
        """Future read-back verification reuses the existing public RSS
        feed — videoId present, title matching, URL path /shorts/ — which
        needs no additional scope beyond upload. Not performed in this
        phase."""
        self._refuse(record)
