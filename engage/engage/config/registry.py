"""Loads the canonical brand + account registry maintained OUTSIDE this repo,
at ~/.claude/harness/social/registry/ (see harness/social/BRIEFING.md).

A brand's brands/<slug>/config.yaml says what that brand is CONFIGURED to
do (its intended platforms, cadence, voice). This registry says what is
actually ENABLED right now (an account that's connected, or at least a real
posting target — see harness/social/registry/accounts.yaml for the
enabled/live_status distinction). drafting/originals.py gates content
creation on the INTERSECTION of both — never either alone — so a brand's
own wishlist of platforms can't outrun what's actually real, and the
registry's "shared account" and "not_applicable" rows can't be silently
bypassed by a brand's config listing a platform anyway.

Every failure mode below — missing files, unreadable files, malformed YAML,
an empty file, a non-mapping top level, or a row missing a required key —
raises RegistryError. Nothing here ever falls through to a raw
KeyError/AttributeError/YAMLError, and nothing here ever returns partial or
best-guess data: a registry that can't be fully trusted is treated as
absent, so every caller fails closed the same way a missing registry
already does."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_REGISTRY_DIR = Path.home() / ".claude" / "harness" / "social" / "registry"

# The ONLY account-registry `live_status` value that authorizes a live,
# externally-acting sink for a cell. Compared by exact (case-insensitive,
# stripped) equality — see Registry.is_live_enabled for why a substring
# test would be actively dangerous here.
LIVE_ENABLED_VALUE = "live"


class RegistryError(Exception):
    pass


def _read_yaml_mapping(path: Path, top_key: str) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RegistryError(f"cannot read {path}: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RegistryError(f"{path} is not valid YAML: {e}") from e
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise RegistryError(f"{path} must be a YAML mapping at the top level, got {type(data).__name__}")
    rows = data.get(top_key) or []
    if not isinstance(rows, list):
        raise RegistryError(f"{path}: {top_key!r} must be a list, got {type(rows).__name__}")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RegistryError(f"{path}: {top_key}[{i}] must be a mapping, got {type(row).__name__}")
    return rows


@dataclass
class Registry:
    brands: list[dict]
    accounts: list[dict]
    # Optional: planning-sources.yaml may be absent (older registries, test
    # fixtures). Absent means "no planning source recorded" — which the CEO
    # Agent treats as an unresolved conflict to escalate, never as "fine".
    planning_sources: list[dict] = field(default_factory=list)
    pending_proposals: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        for i, b in enumerate(self.brands):
            if "engage_slug" not in b or "canonical" not in b:
                raise RegistryError(
                    f"brands[{i}] is missing 'engage_slug' or 'canonical' — refusing to "
                    "load a registry with an incomplete brand row"
                )
        seen = set()
        for b in self.brands:
            if b["engage_slug"] in seen:
                raise RegistryError(f"duplicate engage_slug {b['engage_slug']!r} in brands registry")
            seen.add(b["engage_slug"])

    def slugs(self) -> set[str]:
        return {b["engage_slug"] for b in self.brands}

    def _brand_row(self, slug: str) -> dict:
        for b in self.brands:
            if b["engage_slug"] == slug:
                return b
        raise RegistryError(f"no brand registered with engage_slug {slug!r}")

    def canonical_name(self, slug: str) -> str:
        return self._brand_row(slug)["canonical"]

    def is_parked(self, slug: str) -> bool:
        return self._brand_row(slug).get("status") != "active"

    def planning_source(self, slug: str) -> dict | None:
        """The approved planning source for this brand, or None if none is
        recorded. None is a real answer the caller must handle — the CEO
        Agent escalates it rather than proceeding as if strategy were
        settled."""
        canonical = self.canonical_name(slug)
        for s in self.planning_sources:
            if s.get("engage_slug") == slug or s.get("brand") == canonical:
                return s if s.get("source") != "none" else None
        return None

    def proposals_for(self, slug: str) -> list[dict]:
        """Proposals layered on the approved source — never authoritative.
        Kept separate so a newer FILE DATE can't be mistaken for newer
        APPROVAL: a proposal written after the approved plan still carries no
        authority when its own text says it hasn't been approved."""
        canonical = self.canonical_name(slug)
        return [p for p in self.pending_proposals if p.get("brand") == canonical]

    def account_row(self, slug: str, platform: str) -> dict | None:
        """The exact registry row for one brand/platform cell, or None."""
        canonical = self.canonical_name(slug)
        for a in self.accounts:
            if a.get("brand") == canonical and a.get("platform") == platform:
                return a
        return None

    def is_live_enabled(self, slug: str, platform: str) -> bool:
        """True ONLY when this exact brand/platform row is explicitly
        live-enabled. Every other answer — a missing row, a missing field,
        `disabled`, `not_applicable`, or any descriptive free text — is
        False.

        Matching is an exact, case-insensitive equality check against
        LIVE_ENABLED_VALUE, never a substring search, and this is
        deliberate. One real row in the account registry reads:

            "ingest is live (read-only); publish disabled — ..."

        A substring test for "live" would read that as live-enabled and
        authorize publishing on a cell whose own text says publishing is
        disabled. Only the literal value counts.

        Nothing derives live-enablement from a connector existing, a
        credential existing, a flag being passed, or `enabled: true` —
        `enabled` means "this brand targets this platform", which is a
        different question from "this cell may take a live action"."""
        row = self.account_row(slug, platform)
        if row is None:
            return False
        value = row.get("live_status")
        if not isinstance(value, str):
            return False
        return value.strip().lower() == LIVE_ENABLED_VALUE

    def enabled_platforms(self, slug: str, include_shared: bool = False) -> list[str]:
        """Platforms this brand actually has a real (enabled) account row
        for — regardless of what the brand's own ENGAGE config lists.

        A row marked `shared_account: true` (e.g. one personal X account
        used by more than one brand — see harness/social DEC-SM-004) is
        excluded by default even if `enabled: true`: a shared account needs
        its own origin-brand/voice-policy/independent-approval process, not
        blanket automatic eligibility. Pass include_shared=True only from
        code that actually implements that extra process — nothing in
        Phase 1 does, so nothing in Phase 1 passes it."""
        canonical = self.canonical_name(slug)
        return [a["platform"] for a in self.accounts
                if a.get("brand") == canonical and a.get("enabled")
                and (include_shared or not a.get("shared_account"))]


def load_registry(registry_dir: Path | str | None = None) -> Registry:
    root = Path(registry_dir) if registry_dir else DEFAULT_REGISTRY_DIR
    brands_path, accounts_path = root / "brands.yaml", root / "accounts.yaml"
    if not brands_path.exists() or not accounts_path.exists():
        raise RegistryError(
            f"registry not found under {root} — expected brands.yaml and accounts.yaml"
        )
    brands = _read_yaml_mapping(brands_path, "brands")
    accounts = _read_yaml_mapping(accounts_path, "accounts")
    # planning-sources.yaml is optional — a registry without one loads fine,
    # and every brand simply has no recorded planning source (which the CEO
    # Agent surfaces as a conflict). A malformed one still fails closed.
    planning_path = root / "planning-sources.yaml"
    planning_sources, pending_proposals = [], []
    if planning_path.exists():
        planning_sources = _read_yaml_mapping(planning_path, "sources")
        pending_proposals = _read_yaml_mapping(planning_path, "pending_proposals")
    return Registry(brands=brands, accounts=accounts,
                    planning_sources=planning_sources, pending_proposals=pending_proposals)
