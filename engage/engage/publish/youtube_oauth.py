"""Local-only OAuth consent helper for the disabled YouTube upload path.

NOTHING IN THIS MODULE RUNS BY ITSELF. `run_consent()` is the only function
that opens a browser or contacts Google, it must be called explicitly by the
account owner, and it is never invoked from anywhere else in this codebase —
no scheduler, no agent, no sink, no CLI default path calls it.

WHAT IT DOES NOT DO: upload, schedule, publish, edit, or delete anything;
enable a sink; change a registry live_status; copy the client JSON anywhere;
or retain a credential that fails channel verification.

SECRET DISCIPLINE. The client JSON's contents are read into locals for the
single token exchange and are never stored on an object, returned to a
caller, written to a log, placed in an exception message, or included in any
event, report, or handoff. Every diagnostic this module produces is built
from paths, scopes, booleans, and channel ids — never from a file value.

=============================================================================
SCOPES — exactly two, allowlisted (owner decision 2026-08-21, DEC-SM-023)
=============================================================================
  youtube.upload    upload the one approved video, and nothing else. Cannot
                    read, edit, or delete existing videos.
  youtube.readonly  ONLY to confirm, BEFORE any upload, that the
                    authenticated account owns the registry-bound channel.
                    Cannot write anything.

Why readonly is necessary: verified 2026-08-21 against
https://developers.google.com/youtube/v3/docs/channels/list — `youtube.upload`
is not among the scopes accepted for `channels.list`, so an upload-only token
cannot perform the `mine=true` identity read. Without that read, the "never
upload to the wrong channel" guarantee could only be checked AFTER an upload,
which is too late.

Anything broader is refused by `assert_scopes_allowed()` — enforced at URL
construction and again against what Google actually GRANTS, since a consent
screen lets a user untick a scope. Explicitly refused: `youtube`,
`youtube.force-ssl`, `youtubepartner`, `youtubepartner-channel-audit`, and
any scope not on the two-item allowlist (so an unknown future scope fails
closed rather than slipping through).

Verification is still a hard gate: mismatch, zero channels, ambiguity
(more than one channel returned), or an unauthorized read all mean the
credential is NOT stored, anything this run wrote is purged, and nothing is
enabled. Consent completing is not sufficient; a verified channel binding is.
=============================================================================
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels?part=id&mine=true"

# Scopes are NOT defined here. They are imported from the canonical source
# in credentials.py so the set a consent flow REQUESTS and the set a
# credential loader ACCEPTS are the same objects and cannot drift — they
# did drift once (DEC-SM-025) and it would have blocked a fully successful
# consent. Re-exported under these names for readability at the call sites.
from .credentials import (  # noqa: E402 — canonical scope source
    ALLOWED_SCOPES,
    FORBIDDEN_YOUTUBE_SCOPES as FORBIDDEN_SCOPES,
    YOUTUBE_READONLY_SCOPE as CHANNEL_READ_SCOPE,
    YOUTUBE_UPLOAD_SCOPE as UPLOAD_SCOPE,
)
from .credentials import REQUIRED_SCOPES as CONSENT_SCOPES  # noqa: E402


REDIRECT_HOST = "127.0.0.1"
CREDENTIAL_DIR = Path.home() / ".config" / "engage" / "youtube"

VERIFY_OK = "verified"
VERIFY_MISMATCH = "mismatch"
VERIFY_UNAVAILABLE = "unavailable"
VERIFY_AMBIGUOUS = "ambiguous"
VERIFY_NO_CHANNEL = "no_channel"

REDACTED = "[REDACTED]"


class OAuthHelperError(Exception):
    pass


class ClientFileRejected(OAuthHelperError):
    """The client JSON is somewhere it must never live, or is readable by
    more than its owner."""


class ChannelVerificationFailed(OAuthHelperError):
    """The authorized channel is not the expected one, or could not be
    confirmed. Either way nothing is stored and nothing is enabled."""


class ScopeNotPermitted(OAuthHelperError):
    """A scope outside the approved two-scope allowlist was requested or
    granted."""


def assert_scopes_allowed(scopes) -> tuple[str, ...]:
    """Allowlist check. Anything not in ALLOWED_SCOPES is refused — including
    scopes not on the FORBIDDEN list, so an unknown future scope fails closed
    rather than slipping through."""
    requested = tuple(scopes)
    for s in requested:
        if s in FORBIDDEN_SCOPES:
            raise ScopeNotPermitted(
                f"scope {s} is explicitly refused — it grants more than uploading one "
                f"approved video and verifying channel identity"
            )
        if s not in ALLOWED_SCOPES:
            raise ScopeNotPermitted(
                f"scope {s} is not on the approved allowlist {sorted(ALLOWED_SCOPES)}"
            )
    return requested


# ---------------------------------------------------------------------------
# 1. Local client-file handling
# ---------------------------------------------------------------------------
def _forbidden_roots() -> list[Path]:
    """Places a credential must never live. The repository itself, the
    harness (which is read by report code and copied into handoffs), and
    the Claude project directory (which is summarized into memory)."""
    repo_root = Path(__file__).resolve().parents[2]
    return [
        repo_root,
        Path.home() / ".claude" / "harness",
        Path.home() / ".claude" / "projects",
        Path.home() / ".claude" / "skills",
    ]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _in_a_git_worktree(path: Path) -> Path | None:
    """Any ancestor containing .git — a tracked project directory, where a
    credential is one `git add -A` away from being committed."""
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


@dataclass(frozen=True)
class ClientFileRef:
    """A validated REFERENCE to the client JSON. Holds the path and nothing
    read out of the file — no client id, no client secret, ever."""

    path: Path
    mode: int
    checked_at: int

    def __repr__(self) -> str:
        return f"ClientFileRef(path={self.path.name!r}, contents={REDACTED})"

    __str__ = __repr__

    def summary(self) -> dict:
        return {"client_file": str(self.path), "mode": oct(stat.S_IMODE(self.mode)),
                "contents": REDACTED}


def validate_client_file(path: Path | str) -> ClientFileRef:
    """Check WHERE the client JSON lives and WHO can read it. Never opens it.

    Location is checked before permissions on purpose: a file in the repo is
    wrong even if its mode is perfect, and saying so is more useful than
    complaining about bits."""
    p = Path(path).expanduser()
    try:
        p = p.resolve(strict=True)
    except (OSError, FileNotFoundError):
        raise ClientFileRejected(f"no client JSON at {path}") from None
    if not p.is_file():
        raise ClientFileRejected(f"{p} is not a regular file")

    for root in _forbidden_roots():
        if _is_within(p, root):
            raise ClientFileRejected(
                f"client JSON is inside {root} — credentials must never live in the "
                "repository, the harness, or a Claude project directory, all of which are "
                "read, copied, summarized, or committed by other parts of this system. "
                "Move it somewhere private (for example ~/.config/engage/youtube/) and "
                "pass that path instead."
            )
    tracked = _in_a_git_worktree(p)
    if tracked is not None:
        raise ClientFileRejected(
            f"client JSON is inside the git worktree at {tracked} — one `git add -A` from "
            "being committed. Move it outside any tracked directory."
        )

    mode = p.stat().st_mode
    if os.name == "posix" and (mode & (stat.S_IRWXG | stat.S_IRWXO)):
        raise ClientFileRejected(
            f"{p} has permissions {oct(stat.S_IMODE(mode))} — group or world access is "
            "present. Run: chmod 600 <path>"
        )
    return ClientFileRef(path=p, mode=mode, checked_at=int(time.time()))


def _read_client_config(ref: ClientFileRef) -> tuple[str, str]:
    """Read client id/secret for ONE token exchange. Returned as locals to a
    single caller and never retained. Errors quote structure, never values."""
    try:
        doc = json.loads(ref.path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ClientFileRejected(
            f"client JSON is not valid JSON: {e.msg} at line {e.lineno}") from None
    except OSError as e:
        raise ClientFileRejected(f"cannot read client JSON: {e.strerror}") from None
    block = doc.get("installed") or doc.get("web")
    if not isinstance(block, dict):
        raise ClientFileRejected(
            "client JSON has no 'installed' section — this must be a DESKTOP OAuth client")
    if doc.get("web"):
        raise ClientFileRejected(
            "this is a WEB OAuth client; the local loopback flow requires a DESKTOP client")
    cid, csecret = block.get("client_id"), block.get("client_secret")
    if not cid or not csecret:
        raise ClientFileRejected("client JSON is missing client_id or client_secret")
    return cid, csecret


# ---------------------------------------------------------------------------
# 2. PKCE + authorization URL
# ---------------------------------------------------------------------------
def generate_pkce() -> tuple[str, str]:
    """RFC 7636 S256. Verifier is 43-128 chars of unreserved characters."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def build_authorization_url(client_id: str, redirect_uri: str, code_challenge: str,
                            state: str, scopes: tuple[str, ...] = CONSENT_SCOPES) -> str:
    from urllib.parse import urlencode

    # Enforced here, not just at the constant, so a caller cannot smuggle a
    # broader scope into the consent screen.
    assert_scopes_allowed(scopes)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",     # needed for a refresh token
        "prompt": "consent",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# 4. Channel verification — the gate that decides whether anything is stored
# ---------------------------------------------------------------------------
def verify_channel(access_token: str, expected_channel_id: str, http) -> dict:
    """Minimum authorized read to confirm WHICH channel was authorized.

    Returns safe, non-secret metadata only. A 401/403 means the granted
    scope does not authorize this read (see the module docstring's scope
    conflict) — reported as `unavailable`, never as success."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        status, _, payload = http.request("GET", CHANNELS_ENDPOINT, headers)
    except Exception as e:  # noqa: BLE001
        return {"status": VERIFY_UNAVAILABLE, "expected_channel_id": expected_channel_id,
                "actual_channel_id": "", "checked_at": int(time.time()),
                "detail": f"{type(e).__name__} during channel verification"}

    if status in (401, 403):
        return {"status": VERIFY_UNAVAILABLE, "expected_channel_id": expected_channel_id,
                "actual_channel_id": "", "checked_at": int(time.time()), "http_status": status,
                "detail": ("the granted scope does not authorize an identity read. "
                           f"channels.list does not accept {UPLOAD_SCOPE}. Channel binding "
                           "cannot be confirmed, so the credential will NOT be stored.")}
    if status != 200:
        return {"status": VERIFY_UNAVAILABLE, "expected_channel_id": expected_channel_id,
                "actual_channel_id": "", "checked_at": int(time.time()), "http_status": status,
                "detail": "channel verification did not return a usable response"}
    try:
        items = json.loads(payload.decode("utf-8", "replace")).get("items") or []
    except Exception:  # noqa: BLE001
        return {"status": VERIFY_UNAVAILABLE, "expected_channel_id": expected_channel_id,
                "actual_channel_id": "", "checked_at": int(time.time()),
                "detail": "channel verification response was unparseable"}

    actual_ids = [str(i.get("id", "")) for i in items if i.get("id")]
    base = {"expected_channel_id": expected_channel_id, "checked_at": int(time.time()),
            "returned_channel_ids": sorted(actual_ids),
            "returned_channel_count": len(actual_ids)}

    if not actual_ids:
        return {**base, "status": VERIFY_NO_CHANNEL, "actual_channel_id": "",
                "target_channel_membership_verified": False,
                "detail": ("the authenticated account returned NO channel. Nothing is stored "
                           "and nothing is enabled.")}

    if expected_channel_id not in actual_ids:
        return {**base, "status": VERIFY_MISMATCH,
                "actual_channel_id": ",".join(sorted(actual_ids)),
                "target_channel_membership_verified": False,
                "detail": ("the expected channel is NOT among the channels this account "
                           "controls. The credential is discarded and nothing is enabled. "
                           "Re-run consent with the account that owns the target channel.")}

    # MEMBERSHIP is what consent-time verification establishes: this Google
    # identity controls the target channel. That is a real, useful guarantee
    # and it is sufficient to store a credential.
    #
    # It is NOT proof of which channel a future videos.insert will land on —
    # an account controlling several channels could still upload to the
    # wrong one. That question is answered only by the upload response's own
    # channelId, checked in youtube_transport._parse_success(). The flag
    # below exists so no downstream reader can mistake one for the other.
    return {**base, "status": VERIFY_OK,
            "actual_channel_id": expected_channel_id,
            "target_channel_membership_verified": True,
            "proves_upload_target": False,
            "detail": ("the authenticated account controls the expected channel"
                       + (f" (one of {len(actual_ids)} channels it controls)"
                          if len(actual_ids) > 1 else "")
                       + ". This verifies control, NOT which channel a future upload will "
                         "land on — the upload response's own channelId is the final proof.")}


# ---------------------------------------------------------------------------
# 3. Credential storage — only after verification succeeds
# ---------------------------------------------------------------------------
def _purge_new_credential_material(path: Path, pre_existing: bool) -> bool:
    """Remove credential material written by THIS run. Never touches a
    credential that already existed and was not created here — a failed
    re-consent must not destroy a previously valid, verified credential."""
    if pre_existing:
        return False
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def store_credential(token_response: dict, *, brand: str, channel_id: str,
                     directory: Path | None = None) -> Path:
    """Write the token at the approved path with 0600. Parent directories are
    created HERE and only here, so nothing exists until consent actually
    runs. Callers must not reach this without a `verified` channel result."""
    d = Path(directory) if directory else CREDENTIAL_DIR
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    path = d / f"{brand}.json"
    doc = {
        "brand": brand,
        "platform": "youtube",
        "scopes": list(CONSENT_SCOPES),
        "channel_id": channel_id,
        "refresh_token": token_response.get("refresh_token"),
        "client_id": token_response.get("client_id"),
        "client_secret": token_response.get("client_secret"),
        "token_uri": TOKEN_ENDPOINT,
        "created_at": int(time.time()),
    }
    # Create with owner-only permissions from the start — never briefly
    # world-readable between write and chmod.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
    return path


# ---------------------------------------------------------------------------
# The explicit consent entry point
# ---------------------------------------------------------------------------
CONSENT_NOTICE = """\
This will open a browser window on THIS machine for Google's consent screen.

  scope requested : {scopes}
  channel expected: {channel}
  token will be   : written to {path} with permissions 0600, and ONLY if the
                    authorized channel matches the expected channel above

It will not upload, schedule, publish, edit, or delete anything, and it does
not enable the upload sink or change any account's live status. Those remain
separate, deliberate actions.

Nothing happens until you run this explicitly.
"""


def consent_notice(expected_channel_id: str, brand: str) -> str:
    return CONSENT_NOTICE.format(
        scopes=" ".join(CONSENT_SCOPES),
        channel=expected_channel_id,
        path=CREDENTIAL_DIR / f"{brand}.json",
    )


@dataclass
class ConsentOutcome:
    stored: bool
    credential_path: str = ""
    verification: dict = field(default_factory=dict)
    detail: str = ""

    def summary(self) -> dict:
        """Safe for events, reports, and handoffs — no secret can be here."""
        return {"stored": self.stored, "credential_path": self.credential_path,
                "verification": {k: v for k, v in self.verification.items()},
                "detail": self.detail, "token_material": REDACTED}


def run_consent(*, client_file: Path | str, expected_channel_id: str, brand: str,
                http, browser_opener=None, server_factory=None,
                directory: Path | None = None) -> ConsentOutcome:
    """Run the installed-app flow. EXPLICIT INVOCATION ONLY.

    `http`, `browser_opener`, and `server_factory` are injected so every path
    is testable with no browser and no network. Nothing in this codebase
    calls this function; a human runs it.
    """
    ref = validate_client_file(client_file)
    client_id, client_secret = _read_client_config(ref)
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(32)

    if server_factory is None:  # pragma: no cover - real flow, never in tests
        raise OAuthHelperError(
            "no loopback server factory supplied — this build does not start a listener "
            "on its own. An authorized run must provide one explicitly."
        )
    server = server_factory()
    redirect_uri = f"http://{REDIRECT_HOST}:{server.port}"
    auth_url = build_authorization_url(client_id, redirect_uri, challenge, state)

    if browser_opener is not None:
        browser_opener(auth_url)

    code, returned_state = server.wait_for_code()
    if not secrets.compare_digest(str(returned_state), state):
        raise OAuthHelperError(
            "OAuth state mismatch — the redirect did not originate from this request. "
            "Nothing was stored."
        )

    from urllib.parse import urlencode
    body = urlencode({
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    try:
        status, _, payload = http.request(
            "POST", TOKEN_ENDPOINT,
            {"Content-Type": "application/x-www-form-urlencoded"}, body)
    except Exception as e:  # noqa: BLE001 — transport-layer failure
        # Surface the cause without a traceback and WITHOUT the request body,
        # which contains the authorization code and client secret.
        raise OAuthHelperError(
            f"could not reach the token endpoint ({type(e).__name__}: {e}). "
            "Nothing was stored."
        ) from None
    if status != 200:
        raise OAuthHelperError(
            f"token exchange failed with HTTP {status}. Nothing was stored."
        )
    try:
        tokens = json.loads(payload.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        raise OAuthHelperError("token exchange returned an unparseable response") from None

    access_token = tokens.get("access_token")
    if not access_token:
        raise OAuthHelperError("token exchange returned no access token. Nothing was stored.")

    # What was actually GRANTED may be narrower than what was requested — the
    # consent screen lets a user untick a scope. Verify the grant against the
    # same allowlist, so a partial grant fails closed instead of producing a
    # credential that cannot do the job.
    granted_raw = tokens.get("scope") or ""
    granted = tuple(s for s in str(granted_raw).split() if s)
    if granted:
        assert_scopes_allowed(granted)
        missing = sorted(set(CONSENT_SCOPES) - set(granted))
        if missing:
            return ConsentOutcome(
                stored=False,
                verification={"status": VERIFY_UNAVAILABLE,
                              "expected_channel_id": expected_channel_id,
                              "actual_channel_id": "", "checked_at": int(time.time()),
                              "granted_scopes": list(granted), "missing_scopes": missing},
                detail=(f"consent granted only {list(granted)}; missing {missing}. Nothing "
                        "was stored. Re-run consent and approve both requested permissions."),
            )

    target = (Path(directory) if directory else CREDENTIAL_DIR) / f"{brand}.json"
    pre_existing = target.exists()

    verification = verify_channel(access_token, expected_channel_id, http)
    if verification["status"] != VERIFY_OK:
        # Fail closed. Remove anything THIS run may have written — never a
        # credential that already existed and was not created here.
        purged = _purge_new_credential_material(target, pre_existing)
        return ConsentOutcome(
            stored=False,
            verification={**verification, "temporary_material_purged": purged,
                          "pre_existing_credential_left_untouched": pre_existing},
            detail=("channel verification did not succeed; no credential was retained and "
                    "nothing was enabled. " + verification.get("detail", "")),
        )

    path = store_credential({**tokens, "client_id": client_id, "client_secret": client_secret},
                            brand=brand, channel_id=expected_channel_id, directory=directory)
    return ConsentOutcome(stored=True, credential_path=str(path), verification=verification,
                          detail="credential stored with permissions 0600")
