"""Visibility layer for the REAL, externally-configured live engagement
automation (ChatPlace) — investigated read-only per Nick's Phase 6
instruction on 2026-08-21 (see harness/social/DECISIONS.md DEC-SM-015 and
harness/social/registry/live-engagement-automations.yaml, the verified
snapshot this module loads).

Architectural boundary this module respects: ENGAGE's own Python code has
no mechanism to call ChatPlace's MCP tools live — MCP access exists only
for the Claude agent driving an interactive session, never for code running
here. So this module cannot poll ChatPlace itself; it reads the
periodically-refreshed YAML snapshot instead, the same pattern this system
already uses for performance data and comments (manual/periodic import,
never a live credentialed connector — no new credential storage is added
anywhere by this module).

Naming discipline enforced throughout: the verified capability is a
COMMENT-TRIGGERED DM (a comment containing a keyword fires a DM sequence to
the commenter), never a public comment reply. ChatPlace's own action
vocabulary has no action that posts a public comment reply anywhere
(confirmed exhaustively against automations_reference_actions() — see the
snapshot file's header). Nothing in this module may ever label that
capability "comment_reply", "auto_reply", or "commenting" — doing so would
misrepresent a DM capability as a public-posting capability to the CEO
Agent, Performance Intelligence, and any human reading these reports.

This module does NOT gate, disable, replace, downgrade, or touch the real
automation's configuration in any way — it has no write path to ChatPlace
at all. It also does not replace engagement/agent.py's Phase 5 review-only
reply-draft workflow, which remains the intelligence/triage/governance
layer for comments this automation doesn't touch (every platform other
than the two bound Instagram accounts, and every comment that doesn't
contain a configured trigger keyword)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config.registry import Registry, RegistryError
from ..core.store import Store
from ..measure.intelligence import write_recommendation

DEFAULT_SNAPSHOT_PATH = (
    Path.home() / ".claude" / "harness" / "social" / "registry" / "live-engagement-automations.yaml"
)

# The only action type this module will ever record for a ChatPlace binding.
# Deliberately singular and explicit so a future edit can't casually widen it
# to include a "comment_reply"-shaped value without touching this constant.
ACTION_TYPE_COMMENT_TRIGGERED_DM = "comment_triggered_dm"

EXTERNAL_ACTION_OUTCOMES = (
    "auto_dm_sent", "drafted_only", "manually_posted", "skipped", "escalated", "unknown",
)


class SnapshotError(Exception):
    pass


@dataclass
class LiveAutomationBinding:
    engage_slug: str
    canonical_brand: str
    platform: str
    bot_id: str
    bot_username: str
    registry_handle: str
    automation_id: str
    automation_name: str
    status: str
    action_type: str
    trigger_keyword: str
    action_summary: str
    total_clients: int
    total_conversions: int
    last_run_at: str | None
    execution_note: str
    identity_verified: bool = False
    verification_reason: str = ""


def load_live_automations(path: Path | str | None = None) -> tuple[str, list[LiveAutomationBinding]]:
    """Reads the verified ChatPlace snapshot. Returns (verified_at, bindings).
    Fails closed on anything malformed — same discipline as config/registry.py:
    a snapshot that can't be fully trusted is treated as absent, never as
    partial/best-guess data."""
    p = Path(path) if path else DEFAULT_SNAPSHOT_PATH
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise SnapshotError(f"cannot read {p}: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SnapshotError(f"{p} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise SnapshotError(f"{p} must be a YAML mapping at the top level")
    verified_at = data.get("verified_at")
    if not verified_at:
        raise SnapshotError(f"{p} is missing verified_at")
    rows = data.get("bindings") or []
    if not isinstance(rows, list):
        raise SnapshotError(f"{p}: 'bindings' must be a list")
    bindings = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SnapshotError(f"{p}: bindings[{i}] must be a mapping")
        required = ("engage_slug", "canonical_brand", "platform", "bot_id", "bot_username",
                    "automation_id", "status", "action_type")
        missing = [k for k in required if k not in row]
        if missing:
            raise SnapshotError(f"{p}: bindings[{i}] missing required field(s) {missing}")
        if row["action_type"] != ACTION_TYPE_COMMENT_TRIGGERED_DM:
            raise SnapshotError(
                f"{p}: bindings[{i}] has action_type={row['action_type']!r} — this module only "
                f"ever accepts {ACTION_TYPE_COMMENT_TRIGGERED_DM!r}. A different value likely means "
                "the snapshot was hand-edited without re-verifying the underlying automation; "
                "refusing to load rather than silently trusting an unverified capability claim."
            )
        bindings.append(LiveAutomationBinding(
            engage_slug=row["engage_slug"],
            canonical_brand=row["canonical_brand"],
            platform=row["platform"],
            bot_id=row["bot_id"],
            bot_username=row["bot_username"],
            registry_handle=row.get("registry_account_handle", ""),
            automation_id=row["automation_id"],
            automation_name=row.get("automation_name", ""),
            status=row["status"],
            action_type=row["action_type"],
            trigger_keyword=row.get("trigger_keyword", ""),
            action_summary=row.get("action_summary", ""),
            total_clients=int(row.get("total_clients") or 0),
            total_conversions=int(row.get("total_conversions") or 0),
            last_run_at=row.get("last_run_at"),
            execution_note=row.get("execution_note", ""),
        ))
    return verified_at, bindings


def verify_binding(binding: LiveAutomationBinding, registry: Registry) -> LiveAutomationBinding:
    """Maps a binding to the canonical registry and confirms identity by
    EXACT handle match — never guessed, never fuzzy-matched. An account
    whose identity can't be confirmed is flagged, not silently trusted,
    per Nick's explicit instruction: 'Reject or flag any existing
    connection whose handle/account identity cannot be verified.'"""
    try:
        canonical = registry.canonical_name(binding.engage_slug)
    except RegistryError as e:
        binding.identity_verified = False
        binding.verification_reason = f"unknown engage_slug in canonical registry: {e}"
        return binding
    if canonical != binding.canonical_brand:
        binding.identity_verified = False
        binding.verification_reason = (
            f"brand name mismatch: snapshot says {binding.canonical_brand!r}, "
            f"registry says {canonical!r} for slug {binding.engage_slug!r}"
        )
        return binding

    account_row = next(
        (a for a in registry.accounts
         if a.get("brand") == canonical and a.get("platform") == binding.platform),
        None,
    )
    if account_row is None:
        binding.identity_verified = False
        binding.verification_reason = f"no account row in registry for {canonical}/{binding.platform}"
        return binding

    registry_handle = account_row.get("handle")
    bare_bot_username = binding.bot_username.lstrip("@")
    if not registry_handle or not isinstance(registry_handle, str) or "PLACEHOLDER" in registry_handle:
        binding.identity_verified = False
        binding.verification_reason = (
            f"registry has no confirmed handle for {canonical}/{binding.platform} — "
            "cannot verify against an unconfirmed placeholder"
        )
        return binding

    if registry_handle.lstrip("@") != bare_bot_username:
        binding.identity_verified = False
        binding.verification_reason = (
            f"IDENTITY MISMATCH — bot username {binding.bot_username!r} does not match "
            f"registry handle {registry_handle!r} for {canonical}/{binding.platform}"
        )
        return binding

    binding.identity_verified = True
    binding.verification_reason = "exact handle match against registry/accounts.yaml"
    return binding


def load_and_verify(registry: Registry, path: Path | str | None = None) -> tuple[str, list[LiveAutomationBinding]]:
    verified_at, bindings = load_live_automations(path)
    return verified_at, [verify_binding(b, registry) for b in bindings]


def record_snapshot_audit(store: Store, binding: LiveAutomationBinding, verified_at: str,
                          *, actor: str = "live_capability_sync") -> None:
    """Append-only audit event for this binding. Reuses content_events
    (Phase 1) with a synthetic namespaced id, the same precedent
    engagement/agent.py already established for comment-scoped events not
    tied to a real ContentRecord. Never mutates a prior row — every call
    adds a new event."""
    synthetic_id = f"external:{binding.automation_id}"
    store.record_content_event(
        synthetic_id, "external_automation_snapshot_recorded",
        detail={
            "source": "chatplace_snapshot",
            "verified_at": verified_at,
            "brand": binding.canonical_brand,
            "platform": binding.platform,
            "bot_username": binding.bot_username,
            "automation_name": binding.automation_name,
            "status": binding.status,
            "action_type": binding.action_type,
            "identity_verified": binding.identity_verified,
            "verification_reason": binding.verification_reason,
            "total_clients": binding.total_clients,
            "total_conversions": binding.total_conversions,
            "last_run_at": binding.last_run_at,
        },
        actor=actor,
    )


def ceo_visibility_summary(registry: Registry, engage_slug: str,
                           path: Path | str | None = None) -> dict:
    """What the CEO Agent sees for one brand: every verified-or-flagged
    ChatPlace binding, queue status, and execution counts — never a live
    poll, always the last-synced snapshot, with its own age visible so
    staleness is legible rather than silently assumed current."""
    verified_at, bindings = load_and_verify(registry, path)
    mine = [b for b in bindings if b.engage_slug == engage_slug]
    return {
        "snapshot_verified_at": verified_at,
        "bindings": [
            {
                "platform": b.platform,
                "bot_username": b.bot_username,
                "automation_name": b.automation_name,
                "status": b.status,
                "action_type": b.action_type,  # always comment_triggered_dm — never comment_reply
                "identity_verified": b.identity_verified,
                "verification_reason": b.verification_reason,
                "total_clients": b.total_clients,
                "total_conversions": b.total_conversions,
                "last_run_at": b.last_run_at,
                "execution_note": b.execution_note,
            }
            for b in mine
        ],
    }


def performance_signal(store: Store, registry: Registry, engage_slug: str,
                       path: Path | str | None = None, *, actor: str = "live_capability_sync") -> dict | None:
    """Privacy-minimized, aggregate-only signal handed to Performance
    Intelligence — counts only, no comment text, no author identity, no
    message content. Every binding's real counts are currently zero, so the
    label is always 'insufficient_data': this NEVER invents a performance
    number or declares a result from a system that has not actually run,
    matching measure/kpi.py's evidence-threshold discipline."""
    verified_at, bindings = load_and_verify(registry, path)
    mine = [b for b in bindings if b.engage_slug == engage_slug]
    if not mine:
        return None
    total_conversions = sum(b.total_conversions for b in mine)
    total_clients = sum(b.total_clients for b in mine)
    canonical = registry.canonical_name(engage_slug)
    rec = write_recommendation(
        store, canonical, target="performance", label="insufficient_data",
        summary=(
            f"External automation coverage for {canonical}: {len(mine)} ChatPlace comment-triggered-DM "
            f"binding(s), {total_clients} total client(s), {total_conversions} total conversion(s) ever. "
            "No execution history exists yet to support any performance claim."
        ),
        evidence={"bindings": len(mine), "total_clients": total_clients,
                  "total_conversions": total_conversions, "snapshot_verified_at": verified_at},
        confidence="low",
    )
    return rec
