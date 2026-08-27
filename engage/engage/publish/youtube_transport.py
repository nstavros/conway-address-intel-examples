"""YouTube Data API v3 upload transport — resumable protocol, DISABLED.

This is the ONLY module in the codebase with network capability for
writing. Every other publish module is structurally network-free and tested
to stay that way, so this file is the single place a reviewer must audit to
know what can reach the internet.

It cannot upload anything today. Two independent reasons:
  * `upload()` requires an `UploadAuthorization` capability token, and the
    only function that can mint one lives in the sink, downstream of every
    gate. The transport cannot be driven directly.
  * Obtaining a bearer token requires an `AccessTokenProvider`. The only
    provider that ships is `UnavailableTokenProvider`, which raises. No
    OAuth flow exists anywhere in this codebase.

PROTOCOL — verified 2026-08-21 against
https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
  initiate  POST https://www.googleapis.com/upload/youtube/v3/videos
              ?uploadType=resumable&part=<parts>
            headers: Authorization, Content-Type: application/json;
              charset=UTF-8, Content-Length, X-Upload-Content-Length,
              X-Upload-Content-Type
            body: the JSON video resource
            -> 200 OK, resumable session URI in the `Location` header
  bytes     PUT <session URI>
            headers: Authorization, Content-Length, Content-Type
            -> 201 Created on completion
            -> 308 Resume Incomplete, with `Range: bytes=0-N` when partial
  status    PUT <session URI> with `Content-Range: bytes */<total>` and
            Content-Length: 0 — a documented, side-effect-free query
  retry     500/502/503/504 with exponential backoff. Other 4xx/5xx are
            permanent. 404 means the session expired.

SECRETS: the bearer token is fetched at the moment of use, passed straight
into a request, and never stored on `self`, never returned, never placed in
an exception, an audit event, a diagnostic dict, or a log line. Every
outbound header set is passed through `redact_headers()` before it can
reach any of those. There is a test asserting a planted token never appears
in any diagnostic surface."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# Outcome vocabulary. These are deliberately distinct: collapsing
# "ambiguous" into "failure" is how a system ends up double-uploading.
OUTCOME_PREFLIGHT_REFUSAL = "preflight_refusal"
OUTCOME_AUTH_FAILURE = "auth_failure"
OUTCOME_API_VALIDATION_FAILURE = "api_validation_failure"
OUTCOME_TRANSIENT_FAILURE = "transient_failure"
OUTCOME_AMBIGUOUS = "ambiguous"
OUTCOME_CONFIRMED = "confirmed"

OUTCOMES = (
    OUTCOME_PREFLIGHT_REFUSAL, OUTCOME_AUTH_FAILURE, OUTCOME_API_VALIDATION_FAILURE,
    OUTCOME_TRANSIENT_FAILURE, OUTCOME_AMBIGUOUS, OUTCOME_CONFIRMED,
)

INITIATE_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
UPLOAD_PARTS = ("snippet", "status")

RETRYABLE_STATUS = (500, 502, 503, 504)
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2

SECRET_HEADERS = frozenset({"authorization", "x-goog-api-key", "cookie", "set-cookie"})
REDACTED = "[REDACTED]"


class TransportError(Exception):
    pass


class NotAuthorized(TransportError):
    """No valid capability token — the transport was driven directly."""


def redact_headers(headers: dict) -> dict:
    """Strip every credential-bearing header. Applied to anything that could
    reach an exception, event, report, or log."""
    return {k: (REDACTED if k.lower() in SECRET_HEADERS else v) for k, v in (headers or {}).items()}


@dataclass(frozen=True)
class UploadAuthorization:
    """Capability token proving every gate passed, minted immediately before
    the call by the sink — never constructible usefully by a caller reaching
    for the transport directly, because the transport verifies its contents
    against the asset and metadata it was handed.

    Carries no secret. It is an authorization to act, not a credential."""

    content_record_id: str
    channel_id: str
    asset_path: str
    asset_sha256: str
    metadata_hash: str
    minted_at: int
    gate_report: tuple = field(default=())


class AccessTokenProvider:
    """Supplies a bearer token at the moment of use. Never asked to store
    one, never asked to return one to any caller but the transport."""

    def bearer_token(self) -> str:
        raise NotImplementedError


class UnavailableTokenProvider(AccessTokenProvider):
    """The only provider that ships. No OAuth flow exists in this codebase,
    so there is nothing that could return a real token."""

    def bearer_token(self) -> str:
        raise NotAuthorized(
            "no access-token provider is configured — this build performs no OAuth flow "
            "and holds no token material. A future authorized build must supply one."
        )


class TlsUnavailable(TransportError):
    """No usable CA bundle. Verification is never disabled to work around
    this — an unverified TLS connection to an auth endpoint is worse than
    no connection."""


def _ssl_context():
    """A verifying SSL context, falling back to certifi's CA bundle when the
    interpreter has no system CA linkage.

    python.org builds on macOS routinely ship without it, which surfaces as
    `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` on
    the very first HTTPS call. ingest/youtube_rss.py has handled this since
    Phase 0; this module did not, and the gap only appeared when a real
    consent run hit the token endpoint (DEC-SM-026).

    Verification is ALWAYS on. There is deliberately no ssl._create_unverified_context
    path here, and no verify=False escape hatch: this layer carries bearer
    tokens to Google's auth endpoints, so a downgraded connection would be a
    credential-interception risk, not a convenience."""
    import ssl

    context = ssl.create_default_context()
    # A context with no loaded roots cannot verify anything; certifi supplies them.
    if not context.get_ca_certs():
        try:
            import certifi
        except ImportError as e:
            raise TlsUnavailable(
                "no CA bundle available to verify TLS. Run the macOS "
                "'Install Certificates.command' for your Python, or 'pip install certifi'. "
                "Certificate verification is never disabled to work around this."
            ) from e
        context = ssl.create_default_context(cafile=certifi.where())
    return context


class UrllibHttp:
    """Minimal stdlib HTTP layer, injectable so every protocol, retry, and
    classification path is testable with no network. Matches the approach
    ingest/youtube_rss.py already uses for reads."""

    def __init__(self, context=None):
        self._context = context

    def request(self, method: str, url: str, headers: dict, body=None, timeout: int = 300):
        import ssl
        import urllib.error
        import urllib.request

        context = self._context or _ssl_context()
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            # A real HTTP response carrying an error status — the caller
            # classifies it. Not a transport failure.
            return e.code, dict(e.headers or {}), e.read()
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise TlsUnavailable(
                    "TLS certificate verification failed. Run the macOS "
                    "'Install Certificates.command' for your Python, or 'pip install certifi'. "
                    "Verification is never disabled to work around this."
                ) from e
            raise


@dataclass
class UploadResult:
    outcome: str
    video_id: str = ""
    returned_title: str = ""
    returned_privacy_status: str = ""
    returned_channel_id: str = ""
    uploaded_at: int | None = None
    http_status: int | None = None
    error_reason: str = ""
    error_detail: str = ""
    diagnostics: dict = field(default_factory=dict)
    attempts: int = 0

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}")


def build_video_resource(*, title: str, description: str, privacy_status: str,
                         self_declared_made_for_kids: bool,
                         tags: list | None = None) -> dict:
    """The exact videos.insert request body.

    Contains ONLY fields whose requiredness or validity is officially
    documented (see harness/social/registry/youtube-constraints.yaml,
    DEC-SM-020). Notably absent: `categoryId`. Its requiredness at insert is
    NOT officially documented, and inventing a category id would publish
    under a fabricated classification. If the API turns out to require it,
    the documented 400 is captured and the record pauses — a safe failure."""
    if not title or not title.strip():
        raise TransportError(
            "snippet.title is empty — documented error invalidTitle; refusing to construct "
            "a request that is known to be invalid"
        )
    if not isinstance(self_declared_made_for_kids, bool):
        raise TransportError("selfDeclaredMadeForKids must be an explicit boolean")
    body = {
        "snippet": {"title": title, "description": description},
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": self_declared_made_for_kids,
        },
    }
    if tags:
        body["snippet"]["tags"] = list(tags)
    return body


def _classify_http(status: int) -> str:
    if status in (401,):
        return OUTCOME_AUTH_FAILURE
    if status in (403,):
        return OUTCOME_AUTH_FAILURE
    if status in RETRYABLE_STATUS:
        return OUTCOME_TRANSIENT_FAILURE
    if 400 <= status < 500:
        return OUTCOME_API_VALIDATION_FAILURE
    if status >= 500:
        return OUTCOME_API_VALIDATION_FAILURE
    return OUTCOME_CONFIRMED


def _parse_error(payload: bytes) -> tuple[str, str]:
    """Extract Google's documented error reason/message. Never returns
    request content — only the server's own error text."""
    try:
        doc = json.loads(payload.decode("utf-8", "replace"))
        err = doc.get("error", {})
        errors = err.get("errors") or [{}]
        return str(errors[0].get("reason", "")), str(err.get("message", ""))[:300]
    except Exception:  # noqa: BLE001
        return "", ""


class YouTubeApiTransport:
    """Resumable videos.insert. Refuses without a capability token."""

    def __init__(self, token_provider: AccessTokenProvider | None = None,
                 http=None, *, sleep=time.sleep, clock=time.time):
        self.token_provider = token_provider or UnavailableTokenProvider()
        self.http = http or UrllibHttp()
        self._sleep = sleep
        self._clock = clock

    # -- helpers -----------------------------------------------------------
    def _auth_header(self) -> dict:
        # Fetched at the moment of use; never stored on self.
        #
        # Prefer authorization_header(): a provider that implements it never
        # hands a bare token string to anything, so the value cannot be
        # accidentally interpolated into a log or an f-string. bearer_token()
        # remains the fallback for the base contract.
        provider = self.token_provider
        header_fn = getattr(provider, "authorization_header", None)
        if callable(header_fn):
            return header_fn()
        return {"Authorization": f"Bearer {provider.bearer_token()}"}

    def _verify_authorization(self, auth: UploadAuthorization, asset_path: Path,
                              asset_sha256: str, metadata_hash: str) -> None:
        if not isinstance(auth, UploadAuthorization):
            raise NotAuthorized(
                "upload() requires an UploadAuthorization minted by Publishing Operations "
                "after all gates pass — the transport cannot be driven directly"
            )
        if auth.asset_sha256 != asset_sha256:
            raise NotAuthorized("authorization asset hash does not match the asset being sent")
        if auth.metadata_hash != metadata_hash:
            raise NotAuthorized("authorization metadata hash does not match the metadata being sent")
        if str(asset_path) != auth.asset_path:
            raise NotAuthorized("authorization asset path does not match the file being sent")

    # -- protocol ----------------------------------------------------------
    def upload(self, *, authorization: UploadAuthorization, asset_path: Path | str,
               asset_sha256: str, metadata_hash: str, video_resource: dict,
               mime_type: str = "video/*") -> UploadResult:
        p = Path(asset_path)
        self._verify_authorization(authorization, p, asset_sha256, metadata_hash)

        if not p.is_file():
            return UploadResult(outcome=OUTCOME_PREFLIGHT_REFUSAL,
                                error_detail=f"asset not present at {p}")
        size = p.stat().st_size

        # ---- phase 1: initiate. No video exists yet, so a transient
        # failure here is safe to retry.
        session_uri, init = self._initiate(video_resource, size, mime_type)
        if init is not None:
            return init

        # ---- phase 2: send bytes. Retry policy tightens sharply here.
        # The expected channel travels from the capability token, so the
        # response check is bound to what Publishing Operations authorized.
        return self._send_bytes(session_uri, p, size, mime_type,
                                expected_channel_id=authorization.channel_id)

    def _initiate(self, video_resource: dict, size: int, mime_type: str):
        body = json.dumps(video_resource).encode("utf-8")
        url = f"{INITIATE_URL}?uploadType=resumable&part={','.join(UPLOAD_PARTS)}"
        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {
                "Content-Type": "application/json; charset=UTF-8",
                "Content-Length": str(len(body)),
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": mime_type,
            }
            try:
                headers.update(self._auth_header())
            except NotAuthorized as e:
                return None, UploadResult(outcome=OUTCOME_AUTH_FAILURE,
                                          error_detail=str(e), attempts=attempt)
            try:
                status, resp_headers, payload = self.http.request("POST", url, headers, body)
            except Exception as e:  # noqa: BLE001 — network layer failure
                last = UploadResult(outcome=OUTCOME_TRANSIENT_FAILURE,
                                    error_detail=f"{type(e).__name__} during initiate",
                                    attempts=attempt,
                                    diagnostics={"phase": "initiate",
                                                 "headers_sent": redact_headers(headers)})
                self._backoff(attempt)
                continue

            if status == 200:
                location = resp_headers.get("Location") or resp_headers.get("location")
                if not location:
                    return None, UploadResult(
                        outcome=OUTCOME_AMBIGUOUS, http_status=status,
                        error_detail="initiate returned 200 without a Location session URI",
                        diagnostics={"phase": "initiate",
                                     "headers_sent": redact_headers(headers)})
                return location, None

            reason, message = _parse_error(payload)
            outcome = _classify_http(status)
            result = UploadResult(outcome=outcome, http_status=status, error_reason=reason,
                                  error_detail=message, attempts=attempt,
                                  diagnostics={"phase": "initiate",
                                               "headers_sent": redact_headers(headers)})
            if outcome == OUTCOME_TRANSIENT_FAILURE and attempt < MAX_ATTEMPTS:
                last = result
                self._backoff(attempt)
                continue
            return None, result
        return None, last

    def _send_bytes(self, session_uri: str, p: Path, size: int, mime_type: str,
                    expected_channel_id: str = "") -> UploadResult:
        """Sends the media. A 308 is the documented resume signal and is safe
        to continue from. Anything ELSE that is not a clean 2xx after bytes
        have been transmitted is treated as AMBIGUOUS, never retried: a
        request that may have created a video must never be replayed."""
        data = p.read_bytes()
        headers = {"Content-Length": str(size), "Content-Type": mime_type}
        try:
            headers.update(self._auth_header())
        except NotAuthorized as e:
            return UploadResult(outcome=OUTCOME_AUTH_FAILURE, error_detail=str(e))

        try:
            status, resp_headers, payload = self.http.request("PUT", session_uri, headers, data)
        except Exception as e:  # noqa: BLE001
            # Bytes may or may not have landed. Do NOT retry the write.
            return UploadResult(
                outcome=OUTCOME_AMBIGUOUS,
                error_detail=(f"{type(e).__name__} after media transmission began — the upload "
                              "may or may not have been created. Refusing to retry; verify "
                              "before any further action."),
                diagnostics={"phase": "media", "session_uri_present": True,
                             "headers_sent": redact_headers(headers)},
            )

        if status in (200, 201):
            return self._parse_success(status, payload, headers,
                                       expected_channel_id=expected_channel_id)

        if status == 308:
            # Documented partial-upload signal. A resume is protocol-defined
            # and safe, but this build stops here and reports rather than
            # looping: partial-upload resumption belongs to the authorized
            # build, with a verified session, not to a disabled transport.
            return UploadResult(
                outcome=OUTCOME_AMBIGUOUS, http_status=308,
                error_detail=("308 Resume Incomplete — upload partially transmitted. Resume is "
                              "protocol-defined but is not performed automatically here."),
                diagnostics={"phase": "media",
                             "range": resp_headers.get("Range") or resp_headers.get("range", ""),
                             "headers_sent": redact_headers(headers)},
            )

        reason, message = _parse_error(payload)
        outcome = _classify_http(status)
        # After bytes were sent, even a "transient" status is ambiguous —
        # the server may have accepted the media before failing.
        if outcome == OUTCOME_TRANSIENT_FAILURE:
            outcome = OUTCOME_AMBIGUOUS
            message = (f"{message} (received after media transmission; not retried because the "
                       "upload may have been created)")
        return UploadResult(outcome=outcome, http_status=status, error_reason=reason,
                            error_detail=message,
                            diagnostics={"phase": "media",
                                         "headers_sent": redact_headers(headers)})

    def _parse_success(self, status: int, payload: bytes, headers: dict,
                       expected_channel_id: str = "") -> UploadResult:
        try:
            doc = json.loads(payload.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return UploadResult(
                outcome=OUTCOME_AMBIGUOUS, http_status=status,
                error_detail="upload returned success but the response body was unparseable",
                diagnostics={"phase": "media", "headers_sent": redact_headers(headers)})
        video_id = str(doc.get("id", ""))
        if not video_id:
            return UploadResult(
                outcome=OUTCOME_AMBIGUOUS, http_status=status,
                error_detail="upload returned success without a video id",
                diagnostics={"phase": "media", "headers_sent": redact_headers(headers)})
        snippet, st = doc.get("snippet") or {}, doc.get("status") or {}
        returned_channel = str(snippet.get("channelId", ""))

        # FINAL HARD PROOF OF THE UPLOAD TARGET.
        # Consent-time verification establishes only that the authenticated
        # account CONTROLS the expected channel; an account controlling
        # several channels could still have landed this video on the wrong
        # one. The response's own channelId is the only thing that answers
        # "where did this actually go", so a missing or mismatched value is
        # high-risk, never confirmed — and never retried, because the video
        # demonstrably exists somewhere.
        if expected_channel_id:
            if not returned_channel:
                return UploadResult(
                    outcome=OUTCOME_AMBIGUOUS, http_status=status, video_id=video_id,
                    returned_title=str(snippet.get("title", "")),
                    returned_privacy_status=str(st.get("privacyStatus", "")),
                    uploaded_at=int(self._clock()),
                    error_detail=("HIGH RISK: upload succeeded but the response carried NO "
                                  "channelId, so the actual upload target is unproven. The "
                                  "video id is NOT bound to the record. Do not retry — the "
                                  "video exists. Owner escalation required."),
                    diagnostics={"phase": "response_verification",
                                 "expected_channel_id": expected_channel_id,
                                 "returned_channel_id": "",
                                 "headers_sent": redact_headers(headers)})
            if returned_channel != expected_channel_id:
                return UploadResult(
                    outcome=OUTCOME_AMBIGUOUS, http_status=status, video_id=video_id,
                    returned_title=str(snippet.get("title", "")),
                    returned_channel_id=returned_channel,
                    returned_privacy_status=str(st.get("privacyStatus", "")),
                    uploaded_at=int(self._clock()),
                    error_detail=("HIGH RISK: the video was created on a DIFFERENT channel "
                                  f"({returned_channel}) than the approved target "
                                  f"({expected_channel_id}). The video id is NOT bound to the "
                                  "record and the record is NOT marked published or verified. "
                                  "Do not retry — that would create a second video. Owner "
                                  "escalation required to review and remove the wrong upload."),
                    diagnostics={"phase": "response_verification",
                                 "expected_channel_id": expected_channel_id,
                                 "returned_channel_id": returned_channel,
                                 "headers_sent": redact_headers(headers)})

        return UploadResult(
            outcome=OUTCOME_CONFIRMED, http_status=status, video_id=video_id,
            returned_title=str(snippet.get("title", "")),
            returned_channel_id=returned_channel,
            returned_privacy_status=str(st.get("privacyStatus", "")),
            uploaded_at=int(self._clock()),
        )

    def _backoff(self, attempt: int) -> None:
        self._sleep(BACKOFF_BASE_SECONDS ** attempt)


# ---------------------------------------------------------------------------
# Read-back verification — public RSS, no credentials, no extra scope.
# ---------------------------------------------------------------------------
VERIFICATION_CONFIRMED = "verified"
VERIFICATION_PENDING = "pending"
VERIFICATION_MISMATCH = "mismatch"


def verify_via_rss(*, channel_id: str, video_id: str, expected_title: str,
                   rss_reader, window_seconds: int = 900, clock=time.time) -> dict:
    """Confirm the upload actually exists on the CORRECT channel by reading
    the public feed — no credentials, no additional OAuth scope.

    A videos.insert 201 is NOT treated as verification. Absence from the
    feed inside the window is `pending`, never a reason to re-upload: RSS is
    known to lag, and a missing entry is an unknown, not a failure."""
    posts = rss_reader()
    for post in posts:
        if post.id != video_id:
            continue
        # EXACT comparison against the title line, not a substring test.
        # ingest/youtube_rss.py builds Post.text as "<title>\n\n<description>",
        # so the first line is the title. A substring test here was a real
        # bug: a short title like "T" matches inside an unrelated
        # "SOMETHING ELSE" and would report a wrong video as verified.
        actual_title = (post.text or "").split("\n", 1)[0].strip()
        if expected_title and actual_title != expected_title.strip():
            return {"status": VERIFICATION_MISMATCH, "video_id": video_id,
                    "actual_title": actual_title,
                    "detail": ("video id found but the title does not exactly match the "
                               "approved metadata")}
        return {"status": VERIFICATION_CONFIRMED, "video_id": video_id,
                "url": post.url,
                "is_short": "/shorts/" in (post.url or ""),
                "verified_at": int(clock())}
    return {"status": VERIFICATION_PENDING, "video_id": video_id,
            "detail": (f"not present in the public feed yet; within the {window_seconds}s "
                       "verification window. This is not a failure and must never trigger "
                       "a re-upload.")}
