"""Credential-loading INTERFACE for a future YouTube upload path.

This module creates nothing. It does not write a credential file, set an
environment variable, create a Google Cloud project or OAuth client, or
perform an OAuth flow. As of 2026-08-21 the file it looks for does not
exist, so every call fails closed — which is the intended state and what
the tests assert.

Convention approved in principle 2026-08-21 (DEC-SM-018):
  path   ~/.config/engage/youtube/<brand-slug>.json  (OUTSIDE the repository)
  mode   0600, owner-only
  scope  https://www.googleapis.com/auth/youtube.upload — and nothing else
  bound  one credential per brand cell; never shared between brands

The brand slug is always supplied by the caller (ultimately from the
account registry). This module names no brand of its own — DESIGN.md's
engine-purity contract, enforced by tests/test_isolation.py.

SECRET DISCIPLINE — the load-bearing rule of this module:
this loader NEVER returns, logs, formats, stores, or echoes token material.
It answers exactly one question — "is there a valid, correctly-scoped,
correctly-permissioned credential for this cell?" — and returns a handle
carrying only non-secret facts (path, scopes, brand, platform). Actual
token material has no authorized consumer in this phase, so requesting it
raises. Every error message is built from the path and the structural
problem, never from a file value, so a malformed secret cannot leak
through an exception string into an event, log, report, or handoff."""
from __future__ import annotations

import json
import stat
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CREDENTIAL_DIR = Path.home() / ".config" / "engage" / "youtube"

# =========================================================================
# CANONICAL SCOPE DEFINITION — the single source of truth.
# =========================================================================
# These literals appear exactly ONCE in the codebase. engage/publish/
# youtube_oauth.py imports them rather than restating them, so the scopes a
# consent flow REQUESTS and the scopes a credential loader ACCEPTS cannot
# drift apart.
#
# They did drift, and it mattered: when consent moved to two scopes
# (DEC-SM-023) this loader still demanded upload-only, so a credential
# produced by a fully successful consent would have been rejected as
# wrongly-scoped — surfacing as a baffling "credential unavailable" at
# exactly the moment everything was supposed to work. Found during the
# pre-upload readiness check, fixed here (DEC-SM-025).
#
# Least privilege for the job, and no more:
#   upload    — upload the one approved video. Cannot read, edit, or delete.
#   readonly  — the channels.list identity check ONLY. Cannot write anything.
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

REQUIRED_SCOPES: tuple[str, ...] = (YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE)
ALLOWED_SCOPES = frozenset(REQUIRED_SCOPES)

# Explicitly refused. Named so a future edit reaching for one fails loudly
# instead of silently escalating what a token can do. Note the allowlist —
# not this list — is what governs: a scope absent from BOTH still fails.
FORBIDDEN_YOUTUBE_SCOPES = frozenset({
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtubepartner",
    "https://www.googleapis.com/auth/youtubepartner-channel-audit",
    "https://www.googleapis.com/auth/youtube.channel-memberships.creator",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
})

BOUND_PLATFORM = "youtube"


def credential_path_for(brand: str) -> Path:
    """One credential file per brand, never a shared one. The brand slug is
    supplied by the caller (ultimately from the registry) — this module
    holds no brand identity of its own, per DESIGN.md's engine-purity
    contract, which the isolation test enforces."""
    if not brand or "/" in brand or "\\" in brand or brand.startswith("."):
        raise CredentialError(f"invalid brand slug for a credential path: {brand!r}")
    return DEFAULT_CREDENTIAL_DIR / f"{brand}.json"

# Owner read/write only. Any group or other permission bit is a refusal.
FORBIDDEN_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO

# Non-secret keys that may appear in a redacted summary. Everything else in
# a credential document is treated as secret by default — an allowlist, so
# a key added by a future Google format is redacted rather than exposed.
NON_SECRET_KEYS = frozenset({"scopes", "brand", "platform", "client_id_present", "type"})

REDACTED = "[REDACTED]"


class CredentialError(Exception):
    """Raised for every failure mode: absent, unreadable, malformed,
    insecurely permissioned, wrongly scoped, or wrongly bound. Never
    carries a value read out of the credential document."""


@dataclass(frozen=True)
class CredentialHandle:
    """Proof that a valid credential exists for one cell — and nothing more.

    Deliberately carries no token material. `secret_material()` exists so
    the absence of an authorized consumer is explicit and testable rather
    than implied by omission."""

    path: Path
    scopes: tuple[str, ...]
    brand: str
    platform: str
    _redacted_keys: tuple[str, ...] = field(default=())

    def __repr__(self) -> str:  # never interpolates file contents
        return (f"CredentialHandle(path={self.path.name!r}, brand={self.brand!r}, "
                f"platform={self.platform!r}, scopes={list(self.scopes)!r}, "
                f"secrets={REDACTED})")

    __str__ = __repr__

    def summary(self) -> dict:
        """Safe to put in an event, report, or handoff."""
        return {"credential_path": str(self.path), "brand": self.brand,
                "platform": self.platform, "scopes": list(self.scopes),
                "secret_material": REDACTED}

    def secret_material(self) -> dict:
        """The ONE sanctioned accessor for token material.

        Until 2026-08-22 this raised unconditionally, because no authorized
        consumer existed. One now does: publish/youtube_token.py's
        RefreshTokenProvider, which exchanges the refresh token for a
        short-lived access token and confines it to the transport boundary.
        The guarantee was changed deliberately and visibly rather than
        worked around, so this docstring stays true.

        Three properties are load-bearing and tested:
          * values are re-read from disk on each call and NEVER cached on
            this object, so a handle sitting in memory holds no secret;
          * nothing here reaches `repr`, `summary()`, an event, a report, a
            handoff, or an exception message;
          * a test asserts youtube_token.py is the only module that calls
            this — any new caller is a deliberate, reviewable change."""
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # Structure only — never the offending content.
            raise CredentialError(
                f"cannot read credential material from {self.path}: {type(e).__name__}"
            ) from None
        missing = [k for k in ("client_id", "client_secret", "refresh_token")
                   if not doc.get(k)]
        if missing:
            raise CredentialError(
                f"{self.path} is missing {missing} — a refresh-token grant is impossible. "
                "Re-run consent to obtain a credential with a refresh token."
            )
        return {"client_id": doc["client_id"], "client_secret": doc["client_secret"],
                "refresh_token": doc["refresh_token"],
                "channel_id": doc.get("channel_id", "")}


def redact(obj):
    """Recursively replace every value that is not on the non-secret
    allowlist. Used on anything derived from a credential document before
    it can reach an event, log, report, or handoff."""
    if isinstance(obj, dict):
        return {k: (redact(v) if k in NON_SECRET_KEYS else REDACTED) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def load_youtube_credentials(path: Path | str | None = None, *,
                             brand: str,
                             platform: str = BOUND_PLATFORM) -> CredentialHandle:
    """Validate the credential for ONE cell, or raise. Fails closed on every
    abnormality; there is no partial-success path and no default that
    substitutes for a missing or malformed file.

    `brand` is required and has no default: there is no such thing as "the"
    credential here, only a credential for a named cell."""
    if platform != BOUND_PLATFORM:
        raise CredentialError(
            f"this loader handles {BOUND_PLATFORM} credentials only, not {platform!r} — "
            "another platform would need its own separately-authorized convention, "
            "and none is defined"
        )

    p = Path(path) if path else credential_path_for(brand)

    if not p.exists():
        raise CredentialError(
            f"no credential at {p} — none has been created, and this build never creates one. "
            "Uploading remains impossible until an authorized credential exists."
        )
    if not p.is_file():
        raise CredentialError(f"{p} is not a regular file")

    mode = p.stat().st_mode
    if mode & FORBIDDEN_MODE_BITS:
        raise CredentialError(
            f"{p} has permissions {oct(stat.S_IMODE(mode))} — group or world access is present. "
            "Required: 0600 (owner only). Refusing to read an insecurely permissioned credential."
        )

    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise CredentialError(f"cannot read {p}: {e.strerror}") from None

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        # e.msg is a parser message ("Expecting value"), never file content;
        # position only. Deliberately does not include the offending text.
        raise CredentialError(f"{p} is not valid JSON: {e.msg} at line {e.lineno}") from None

    if not isinstance(doc, dict):
        raise CredentialError(f"{p} must contain a JSON object at the top level")

    scopes = doc.get("scopes")
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise CredentialError(f"{p} is missing a 'scopes' list")

    # Scope strings are not secret — they are the thing being checked, and
    # naming the mismatch is the whole point of these errors.
    got_set = set(scopes)
    missing = sorted(ALLOWED_SCOPES - got_set)
    extra = sorted(got_set - ALLOWED_SCOPES)

    if missing:
        raise CredentialError(
            f"{p} is missing required scope(s) {missing}. Both {sorted(ALLOWED_SCOPES)} are "
            "needed: upload to publish the approved video, readonly to verify the channel "
            "before doing so. Re-run consent and approve both."
        )
    if extra:
        forbidden = [s for s in extra if s in FORBIDDEN_YOUTUBE_SCOPES]
        raise CredentialError(
            f"{p} carries scope(s) outside the allowlist: {extra}"
            + (f" (explicitly refused: {forbidden})" if forbidden else "")
            + f". This build authorizes exactly {sorted(ALLOWED_SCOPES)}. A broader scope is "
              "refused, not accepted as a superset — widening it requires an explicit, "
              "approved configuration change."
        )
    # Report in CANONICAL order, not alphabetical: the handle's scope tuple
    # should compare equal to REQUIRED_SCOPES itself, so callers can check
    # identity of meaning rather than accidentally depending on sort order.
    got = tuple(s for s in REQUIRED_SCOPES if s in got_set)

    doc_brand, doc_platform = doc.get("brand"), doc.get("platform")
    if doc_brand != brand or doc_platform != platform:
        raise CredentialError(
            f"{p} is bound to {doc_brand!r}/{doc_platform!r}, not {brand!r}/{platform!r} — "
            "a credential may only be used for the cell it was issued for"
        )

    return CredentialHandle(
        path=p, scopes=tuple(got), brand=brand, platform=platform,
        _redacted_keys=tuple(sorted(k for k in doc if k not in NON_SECRET_KEYS)),
    )
