"""Refresh-token AccessTokenProvider for the YouTube upload transport.

Turns the stored, channel-bound credential into a short-lived access token.
It is NOT invoked anywhere in this build: the upload sink is disabled, the
registry cell is not live, and nothing constructs this provider outside
tests. Building it does not enable anything.

WHAT IT WILL NEVER DO
  Open a browser. Run an interactive consent flow. Use an authorization
  code. Widen scope. Persist an access token. Call videos.insert or any
  upload endpoint. Return token material to a report, event, log, handoff,
  CLI surface, or exception.

SECRET BOUNDARY
  Refresh material is fetched from the audited loader's `secret_material()`
  at the moment of use, held in locals for one HTTP request, and dropped.
  The access token lives only in `self._token`, in process memory, and is
  reachable only through `authorization_header()` — which the transport
  drops straight into a request. `__repr__` is redacted, and there is no
  accessor that returns the raw token to a caller.

FAILURE CATEGORIES (short, safe, never carrying a value)
  auth_unavailable      no credential, or none usable
  auth_invalid          malformed, insecure, or wrong-channel credential
  auth_scope_mismatch   stored or granted scope is not the approved pair
  auth_transport_failure  network/TLS problem reaching the token endpoint
  auth_refresh_rejected Google refused the refresh (e.g. invalid_grant)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from .credentials import (
    ALLOWED_SCOPES,
    REQUIRED_SCOPES,
    CredentialError,
    load_youtube_credentials,
)
# TOKEN_ENDPOINT is defined once, in youtube_oauth — imported rather than
# restated, the same single-source discipline the scopes follow. No cycle:
# youtube_oauth imports only credentials.
from .youtube_oauth import TOKEN_ENDPOINT
from .youtube_transport import (
    AccessTokenProvider,
    NotAuthorized,
    TlsUnavailable,
    UrllibHttp,
)

AUTH_UNAVAILABLE = "auth_unavailable"
AUTH_INVALID = "auth_invalid"
AUTH_SCOPE_MISMATCH = "auth_scope_mismatch"
AUTH_TRANSPORT_FAILURE = "auth_transport_failure"
AUTH_REFRESH_REJECTED = "auth_refresh_rejected"

AUTH_CATEGORIES = (AUTH_UNAVAILABLE, AUTH_INVALID, AUTH_SCOPE_MISMATCH,
                   AUTH_TRANSPORT_FAILURE, AUTH_REFRESH_REJECTED)

# Refresh this long before actual expiry, so a token cannot go stale
# mid-upload. A resumable upload can run for minutes.
DEFAULT_REFRESH_MARGIN_SECONDS = 300

# Bounded retry, and ONLY for transport-level failures that happened before
# Google could act on the request. An OAuth error response is never retried.
MAX_REFRESH_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2

# OAuth error codes that are permanent. Retrying these cannot help and
# repeated attempts against an auth endpoint look like credential probing.
PERMANENT_OAUTH_ERRORS = frozenset({
    "invalid_grant", "invalid_client", "unauthorized_client",
    "invalid_scope", "invalid_request", "unsupported_grant_type",
})

REDACTED = "[REDACTED]"


class TokenUnavailable(NotAuthorized):
    """Carries a category and a safe message — never a value.

    Subclasses NotAuthorized so the transport's existing handling classifies
    it as OUTCOME_AUTH_FAILURE without a new code path."""

    def __init__(self, category: str, detail: str):
        if category not in AUTH_CATEGORIES:
            raise ValueError(f"unknown auth category {category!r}")
        self.category = category
        self.detail = detail
        super().__init__(f"{category}: {detail}")


@dataclass(frozen=True)
class AuthorizationHeader:
    """Opaque carrier. Renders redacted; the value is reachable only through
    `as_headers()`, which the transport passes straight into a request."""

    _value: str
    expires_at: int

    def __repr__(self) -> str:
        return f"AuthorizationHeader(token={REDACTED}, expires_at={self.expires_at})"

    __str__ = __repr__

    def as_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._value}"}


class RefreshTokenProvider(AccessTokenProvider):
    """Exchanges a stored refresh token for a short-lived access token.

    Constructing one performs no I/O and reads no credential. Nothing
    happens until `authorization_header()` is called, which nothing in this
    build does."""

    def __init__(self, *, brand: str, expected_channel_id: str,
                 credential_path: Path | str | None = None,
                 http=None,
                 clock=time.time,
                 sleep=time.sleep,
                 refresh_margin_seconds: int = DEFAULT_REFRESH_MARGIN_SECONDS,
                 max_attempts: int = MAX_REFRESH_ATTEMPTS):
        self.brand = brand
        self.expected_channel_id = expected_channel_id
        self.credential_path = credential_path
        self._http = http
        self._clock = clock
        self._sleep = sleep
        self._margin = refresh_margin_seconds
        self._max_attempts = max_attempts
        # In-process only. Never written to disk, never serialized.
        self._token: str | None = None
        self._expires_at: int = 0

    def __repr__(self) -> str:
        state = "cached" if self._token else "empty"
        return (f"RefreshTokenProvider(brand={self.brand!r}, "
                f"channel={self.expected_channel_id!r}, token={REDACTED} ({state}), "
                f"expires_at={self._expires_at})")

    __str__ = __repr__

    def summary(self) -> dict:
        """Safe for events, reports, and handoffs."""
        return {"brand": self.brand, "expected_channel_id": self.expected_channel_id,
                "has_cached_token": bool(self._token),
                "expires_at": self._expires_at, "token_material": REDACTED}

    # -- validation --------------------------------------------------------
    def _validated_handle(self):
        """Delegates every location, permission, scope, and binding check to
        the audited loader. No shortcut path exists."""
        try:
            handle = load_youtube_credentials(self.credential_path, brand=self.brand)
        except CredentialError as e:
            msg = str(e)
            if "scoped" in msg or "scope" in msg:
                raise TokenUnavailable(AUTH_SCOPE_MISMATCH, msg) from None
            if "no credential at" in msg:
                raise TokenUnavailable(AUTH_UNAVAILABLE, msg) from None
            raise TokenUnavailable(AUTH_INVALID, msg) from None

        # Defence in depth: the loader already enforces this, but a scope set
        # that is not exactly the approved pair must never reach a refresh.
        if set(handle.scopes) != set(ALLOWED_SCOPES):
            raise TokenUnavailable(
                AUTH_SCOPE_MISMATCH,
                f"credential scopes {sorted(handle.scopes)} are not the approved "
                f"{sorted(ALLOWED_SCOPES)}")
        return handle

    def _refresh_material(self, handle) -> dict:
        try:
            material = handle.secret_material()
        except CredentialError as e:
            raise TokenUnavailable(AUTH_INVALID, str(e)) from None

        bound = (material.get("channel_id") or "").strip()
        if not bound:
            raise TokenUnavailable(
                AUTH_INVALID,
                "credential records no channel binding — refusing to refresh a token that "
                "cannot be tied to the approved channel")
        if bound != self.expected_channel_id:
            raise TokenUnavailable(
                AUTH_INVALID,
                f"credential is bound to channel {bound!r}, not the expected "
                f"{self.expected_channel_id!r}")
        return material

    # -- refresh -----------------------------------------------------------
    def _post_refresh(self, material: dict):
        """One refresh-token grant. Retries ONLY transport failures, which
        occur before Google can act on the request."""
        http = self._http or UrllibHttp()
        body = urlencode({
            "client_id": material["client_id"],
            "client_secret": material["client_secret"],
            "refresh_token": material["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        last = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return http.request("POST", TOKEN_ENDPOINT, headers, body)
            except TlsUnavailable as e:
                # A trust-store problem will not fix itself on retry.
                raise TokenUnavailable(AUTH_TRANSPORT_FAILURE, str(e)) from None
            except Exception as e:  # noqa: BLE001 — pre-request transport failure
                last = f"{type(e).__name__} reaching the token endpoint"
                if attempt < self._max_attempts:
                    self._sleep(BACKOFF_BASE_SECONDS ** attempt)
                    continue
        raise TokenUnavailable(
            AUTH_TRANSPORT_FAILURE,
            f"{last} after {self._max_attempts} attempts. Nothing was changed.")

    def _refresh(self) -> None:
        handle = self._validated_handle()
        material = self._refresh_material(handle)
        status, _, payload = self._post_refresh(material)

        try:
            doc = json.loads(payload.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            raise TokenUnavailable(
                AUTH_REFRESH_REJECTED,
                f"token endpoint returned HTTP {status} with an unparseable body") from None

        if status != 200:
            err = str(doc.get("error", "")) or "unknown_error"
            # Deliberately does NOT include error_description, which can echo
            # request content back.
            if err in PERMANENT_OAUTH_ERRORS:
                raise TokenUnavailable(
                    AUTH_REFRESH_REJECTED,
                    f"the refresh token was rejected ({err}, HTTP {status}). It may have been "
                    "revoked or expired. Re-run consent. Not retried — this is permanent.")
            if status in (401, 403):
                raise TokenUnavailable(
                    AUTH_REFRESH_REJECTED,
                    f"authorization refused at the token endpoint (HTTP {status}, {err}).")
            raise TokenUnavailable(
                AUTH_REFRESH_REJECTED,
                f"token refresh failed (HTTP {status}, {err}).")

        # A refresh response may echo the granted scope. If it does, it must
        # still be the approved pair — a silently widened or narrowed grant
        # is refused rather than used.
        granted = str(doc.get("scope") or "").split()
        if granted and set(granted) != set(REQUIRED_SCOPES):
            raise TokenUnavailable(
                AUTH_SCOPE_MISMATCH,
                f"refresh returned scopes {sorted(granted)}, not the approved "
                f"{sorted(REQUIRED_SCOPES)}")

        token = doc.get("access_token")
        if not token:
            raise TokenUnavailable(
                AUTH_REFRESH_REJECTED, "token endpoint returned no access token")

        expires_in = doc.get("expires_in")
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError):
            expires_in = 0
        self._token = token
        self._expires_at = int(self._clock()) + max(expires_in, 0)

    # -- public surface ----------------------------------------------------
    def _needs_refresh(self) -> bool:
        if not self._token:
            return True
        return int(self._clock()) + self._margin >= self._expires_at

    def authorization_header(self) -> dict:
        """The safe mechanism the transport uses. Refreshes if needed."""
        if self._needs_refresh():
            self._refresh()
        return AuthorizationHeader(self._token, self._expires_at).as_headers()

    def bearer_token(self) -> str:
        """Kept for the AccessTokenProvider contract. The transport prefers
        `authorization_header()`; this exists so the interface stays
        satisfiable and is never the path a report can reach."""
        if self._needs_refresh():
            self._refresh()
        return self._token

    def invalidate(self) -> None:
        """Drop the cached token — e.g. after a 401 mid-operation."""
        self._token = None
        self._expires_at = 0
