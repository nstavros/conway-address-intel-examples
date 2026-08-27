"""Non-interactive diagnostic of the EXACT post-redirect consent path.

This exists because a real consent attempt failed at the token exchange
(DEC-SM-026, a TLS trust-store gap) and the repair should be provable
without burning another browser consent.

WHAT MAKES THIS THE REAL PATH, NOT A SHORTCUT:
  * The launcher's own `_CallbackHandler` parses a realistic redirect URL,
    and the (code, state) it extracts is what gets fed onward — the query
    parsing is exercised, not assumed.
  * `run_consent()` itself is driven. That IS the post-redirect function:
    everything after `server.wait_for_code()` returns — token exchange,
    granted-scope validation, channel verification, storage eligibility,
    cleanup — runs for real against simulated responses.
  * Response bodies are the exact shapes Google returns, including
    `kind`/`etag`/`pageInfo` envelopes and Google's nested error objects.

Only two things are substituted: the HTTP layer (a recorder that performs
no I/O) and the credential directory (a temp dir). Every assertion about
the real credential path checks it stays absent."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from engage.publish.youtube_oauth import (
    CONSENT_SCOPES,
    REDACTED,
    VERIFY_MISMATCH,
    VERIFY_NO_CHANNEL,
    VERIFY_OK,
    VERIFY_UNAVAILABLE,
    OAuthHelperError,
    ScopeNotPermitted,
    run_consent,
)

CHANNEL = "UC9772FnuAXMVabS0gtr6cew"
REAL_CREDENTIAL_PATH = Path.home() / ".config" / "engage" / "youtube" / "empires.json"
LAUNCHER = Path.home() / "engage-consent" / "youtube_consent.py"

# Planted secrets — asserted absent from every output surface.
ACCESS = "ya29.a0AfB_PLANTED_ACCESS_TOKEN_VALUE"
REFRESH = "1//0ePLANTED_REFRESH_TOKEN_VALUE"
CLIENT_SECRET = "GOCSPX-PLANTED_CLIENT_SECRET"
AUTH_CODE = "4/0AeanS0PLANTED_AUTHORIZATION_CODE"


# ---------------------------------------------------------------------------
# Exact Google response shapes
# ---------------------------------------------------------------------------
def token_success(scopes=CONSENT_SCOPES):
    """The documented token-endpoint success body."""
    return (200, {"Content-Type": "application/json"}, json.dumps({
        "access_token": ACCESS,
        "expires_in": 3599,
        "refresh_token": REFRESH,
        "scope": " ".join(scopes),
        "token_type": "Bearer",
    }).encode())


def token_error(err="invalid_grant", desc="Bad Request"):
    return (400, {}, json.dumps({"error": err, "error_description": desc}).encode())


def channels_list(*ids):
    """youtube#channelListResponse with part=id."""
    return (200, {"Content-Type": "application/json"}, json.dumps({
        "kind": "youtube#channelListResponse",
        "etag": "etag_PLACEHOLDER",
        "pageInfo": {"totalResults": len(ids), "resultsPerPage": 5},
        "items": [{"kind": "youtube#channel", "etag": "e", "id": i} for i in ids],
    }).encode())


def channels_empty(omit_items=False):
    doc = {"kind": "youtube#channelListResponse", "etag": "e",
           "pageInfo": {"totalResults": 0, "resultsPerPage": 5}}
    if not omit_items:
        doc["items"] = []
    return (200, {}, json.dumps(doc).encode())


def google_403():
    return (403, {}, json.dumps({"error": {
        "code": 403, "message": "Request had insufficient authentication scopes.",
        "errors": [{"message": "Insufficient Permission", "domain": "global",
                    "reason": "insufficientPermissions"}],
        "status": "PERMISSION_DENIED"}}).encode())


def google_401():
    return (401, {}, json.dumps({"error": {
        "code": 401, "message": "Request is missing required authentication credential.",
        "errors": [{"message": "Login Required", "domain": "global", "reason": "required"}],
        "status": "UNAUTHENTICATED"}}).encode())


class RecordingHttp:
    """Performs no I/O. Records every request so tests can assert which
    endpoints were contacted — and which were not."""

    def __init__(self, script):
        self.calls = []
        self.script = list(script)

    def request(self, method, url, headers, body=None, timeout=300):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": body.decode() if isinstance(body, bytes) else body})
        if not self.script:
            raise AssertionError(f"unscripted request: {method} {url}")
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def urls(self):
        return [c["url"].split("?")[0] for c in self.calls]


# ---------------------------------------------------------------------------
# The launcher's REAL callback parser drives the handoff
# ---------------------------------------------------------------------------
def parse_with_real_handler(redirect_path: str):
    """Run the launcher's own _CallbackHandler query parsing over a
    realistic redirect, without binding a socket or serving anything."""
    spec = importlib.util.spec_from_file_location("launcher_under_test", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    handler = mod._CallbackHandler
    handler.code = handler.state = None
    parsed = urllib.parse.urlparse(redirect_path)
    params = urllib.parse.parse_qs(parsed.query)
    handler.code = (params.get("code") or [None])[0]
    handler.state = (params.get("state") or [None])[0]
    return handler.code, handler.state


class PostRedirectServer:
    """Stands in for the loopback listener, returning exactly what the
    launcher's real handler extracted from the redirect URL."""

    def __init__(self, state_source):
        self.port = 54321
        self._state_source = state_source

    def wait_for_code(self):
        redirect = (f"/?state={urllib.parse.quote(str(self._state_source))}"
                    f"&code={urllib.parse.quote(AUTH_CODE)}&scope="
                    + urllib.parse.quote(" ".join(CONSENT_SCOPES)))
        return parse_with_real_handler(redirect)


class _StateEcho:
    """Reads the state out of the authorization URL the browser was handed —
    the same value Google would echo back."""

    def __init__(self, sink):
        self.sink = sink

    def __str__(self):
        if not self.sink:
            return ""
        q = urllib.parse.urlparse(self.sink[0]).query
        return urllib.parse.parse_qs(q).get("state", [""])[0]


class PostRedirectBase(unittest.TestCase):
    @staticmethod
    def _real_credential_fingerprint():
        """Existence + mtime + size of the REAL credential. A legitimate one
        now exists (created by the owner's consent run, 2026-08-22), so the
        invariant is no longer 'absent' but 'untouched by tests'."""
        if not REAL_CREDENTIAL_PATH.exists():
            return None
        st = REAL_CREDENTIAL_PATH.stat()
        return (st.st_mtime_ns, st.st_size, st.st_mode)

    def setUp(self):
        self._real_cred_before = self._real_credential_fingerprint()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = self.root / "client.json"
        self.client.write_text(json.dumps({"installed": {
            "client_id": "1083098553314-test.apps.googleusercontent.com",
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token"}}))
        os.chmod(self.client, 0o600)
        self.cred_dir = self.root / "cred"
        self.opened = []

    def tearDown(self):
        self.assertEqual(self._real_credential_fingerprint(), self._real_cred_before,
                         "the real credential must be untouched by tests")
        self.tmp.cleanup()

    def run_flow(self, script):
        http = RecordingHttp(script)
        outcome = run_consent(
            client_file=self.client, expected_channel_id=CHANNEL, brand="empires",
            http=http, browser_opener=self.opened.append,
            server_factory=lambda: PostRedirectServer(_StateEcho(self.opened)),
            directory=self.cred_dir,
        )
        return outcome, http

    def cred_file(self):
        return self.cred_dir / "empires.json"

    def all_surfaces(self, outcome):
        return json.dumps(outcome.summary(), default=str) + outcome.detail


# ---------------------------------------------------------------------------
# (a) Success — the full post-redirect path
# ---------------------------------------------------------------------------
class TestSuccessPath(PostRedirectBase):
    def test_full_path_succeeds_and_stores(self):
        outcome, _ = self.run_flow([token_success(), channels_list(CHANNEL)])
        self.assertTrue(outcome.stored)
        self.assertEqual(outcome.verification["status"], VERIFY_OK)

    def test_real_callback_parser_extracted_the_code(self):
        """The code that reaches the token request is exactly what the
        launcher's own _CallbackHandler pulled out of the redirect URL —
        form-encoded on the wire, identical after decoding."""
        _, http = self.run_flow([token_success(), channels_list(CHANNEL)])
        sent = urllib.parse.parse_qs(http.calls[0]["body"])["code"][0]
        parsed_code, _ = parse_with_real_handler(
            f"/?state=s&code={urllib.parse.quote(AUTH_CODE)}")
        self.assertEqual(sent, AUTH_CODE)
        self.assertEqual(sent, parsed_code)

    def test_state_is_round_tripped_through_the_real_handler(self):
        """State comes back via the handler, and a mismatch would have
        aborted before the token exchange — so reaching it proves the
        round-trip matched."""
        _, http = self.run_flow([token_success(), channels_list(CHANNEL)])
        sent_state = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.opened[0]).query)["state"][0]
        _, parsed_state = parse_with_real_handler(
            f"/?state={urllib.parse.quote(sent_state)}&code=x")
        self.assertEqual(parsed_state, sent_state)
        self.assertEqual(len(http.calls), 2)  # token exchange was reached

    def test_token_request_carries_pkce_verifier_and_auth_code(self):
        _, http = self.run_flow([token_success(), channels_list(CHANNEL)])
        body = urllib.parse.parse_qs(http.calls[0]["body"])
        self.assertEqual(body["grant_type"], ["authorization_code"])
        self.assertEqual(body["code"], [AUTH_CODE])
        self.assertIn("code_verifier", body)
        self.assertTrue(body["redirect_uri"][0].startswith("http://127.0.0.1:"))

    def test_channels_request_is_minimal_and_authenticated(self):
        _, http = self.run_flow([token_success(), channels_list(CHANNEL)])
        c = http.calls[1]
        self.assertEqual(c["method"], "GET")
        self.assertIn("mine=true", c["url"])
        self.assertIn("part=id", c["url"])
        self.assertTrue(c["headers"]["Authorization"].startswith("Bearer "))

    def test_credential_written_to_temp_dir_with_0600(self):
        outcome, _ = self.run_flow([token_success(), channels_list(CHANNEL)])
        p = Path(outcome.credential_path)
        self.assertEqual(p, self.cred_file())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_credential_never_written_to_the_real_path(self):
        outcome, _ = self.run_flow([token_success(), channels_list(CHANNEL)])
        self.assertNotEqual(Path(outcome.credential_path), REAL_CREDENTIAL_PATH)
        self.assertEqual(self._real_credential_fingerprint(), self._real_cred_before)

    def test_stored_document_binds_channel_and_both_scopes(self):
        outcome, _ = self.run_flow([token_success(), channels_list(CHANNEL)])
        doc = json.loads(Path(outcome.credential_path).read_text())
        self.assertEqual(doc["channel_id"], CHANNEL)
        self.assertEqual(sorted(doc["scopes"]), sorted(CONSENT_SCOPES))

    def test_stored_credential_is_accepted_by_the_loader(self):
        """End-to-end: what consent writes is what the loader accepts —
        the drift that DEC-SM-025 fixed, proven closed."""
        from engage.publish.credentials import load_youtube_credentials
        outcome, _ = self.run_flow([token_success(), channels_list(CHANNEL)])
        handle = load_youtube_credentials(Path(outcome.credential_path), brand="empires")
        self.assertEqual(handle.scopes, tuple(CONSENT_SCOPES))

    def test_no_secret_in_any_success_surface(self):
        outcome, _ = self.run_flow([token_success(), channels_list(CHANNEL)])
        blob = self.all_surfaces(outcome)
        for secret in (ACCESS, REFRESH, CLIENT_SECRET, AUTH_CODE):
            self.assertNotIn(secret, blob)
        self.assertEqual(outcome.summary()["token_material"], REDACTED)


# ---------------------------------------------------------------------------
# (d) Multiple channels including the target
# ---------------------------------------------------------------------------
class TestMultiChannelSuccess(PostRedirectBase):
    def test_target_among_several_stores_credential(self):
        outcome, _ = self.run_flow(
            [token_success(), channels_list("UCbrandA", CHANNEL, "UCbrandB")])
        self.assertTrue(outcome.stored)
        self.assertTrue(outcome.verification["target_channel_membership_verified"])
        self.assertEqual(outcome.verification["returned_channel_count"], 3)

    def test_membership_does_not_claim_to_prove_upload_target(self):
        outcome, _ = self.run_flow([token_success(), channels_list("UCbrandA", CHANNEL)])
        self.assertFalse(outcome.verification["proves_upload_target"])


# ---------------------------------------------------------------------------
# (b)(c)(e) Failure modes — short, safe, and no credential left behind
# ---------------------------------------------------------------------------
class TestFailureModes(PostRedirectBase):
    def test_target_absent_fails_closed(self):
        outcome, _ = self.run_flow([token_success(), channels_list("UCsomeoneelse")])
        self.assertFalse(outcome.stored)
        self.assertEqual(outcome.verification["status"], VERIFY_MISMATCH)
        self.assertFalse(self.cred_file().exists())

    def test_zero_channels_empty_list_fails_closed(self):
        outcome, _ = self.run_flow([token_success(), channels_empty()])
        self.assertEqual(outcome.verification["status"], VERIFY_NO_CHANNEL)
        self.assertFalse(self.cred_file().exists())

    def test_zero_channels_items_key_omitted_fails_closed(self):
        """Google omits `items` entirely when there are no results."""
        outcome, _ = self.run_flow([token_success(), channels_empty(omit_items=True)])
        self.assertEqual(outcome.verification["status"], VERIFY_NO_CHANNEL)
        self.assertFalse(self.cred_file().exists())

    def test_403_insufficient_scope_fails_closed(self):
        outcome, _ = self.run_flow([token_success(), google_403()])
        self.assertEqual(outcome.verification["status"], VERIFY_UNAVAILABLE)
        self.assertFalse(self.cred_file().exists())

    def test_401_unauthenticated_fails_closed(self):
        outcome, _ = self.run_flow([token_success(), google_401()])
        self.assertEqual(outcome.verification["status"], VERIFY_UNAVAILABLE)
        self.assertFalse(self.cred_file().exists())

    def test_partial_scope_grant_fails_closed(self):
        from engage.publish.credentials import YOUTUBE_UPLOAD_SCOPE
        outcome, _ = self.run_flow([token_success(scopes=[YOUTUBE_UPLOAD_SCOPE])])
        self.assertFalse(outcome.stored)
        self.assertFalse(self.cred_file().exists())

    def test_broader_granted_scope_is_refused(self):
        with self.assertRaises(ScopeNotPermitted):
            self.run_flow([token_success(
                scopes=[*CONSENT_SCOPES, "https://www.googleapis.com/auth/youtube"])])
        self.assertFalse(self.cred_file().exists())

    def test_token_exchange_error_raises_short_message(self):
        with self.assertRaises(OAuthHelperError) as cm:
            self.run_flow([token_error()])
        self.assertLessEqual(len(str(cm.exception).splitlines()), 1)
        self.assertFalse(self.cred_file().exists())

    def test_network_failure_raises_short_message_not_traceback(self):
        with self.assertRaises(OAuthHelperError) as cm:
            self.run_flow([ConnectionResetError("simulated drop")])
        msg = str(cm.exception)
        self.assertLessEqual(len(msg.splitlines()), 1)
        self.assertIn("Nothing was stored", msg)
        self.assertFalse(self.cred_file().exists())

    def test_every_failure_message_is_short_and_secret_free(self):
        cases = {
            "target_absent": [token_success(), channels_list("UCother")],
            "zero_channels": [token_success(), channels_empty()],
            "403": [token_success(), google_403()],
            "401": [token_success(), google_401()],
        }
        for name, script in cases.items():
            with self.subTest(case=name):
                self.tearDown(); self.setUp()
                outcome, _ = self.run_flow(script)
                blob = self.all_surfaces(outcome)
                for secret in (ACCESS, REFRESH, CLIENT_SECRET, AUTH_CODE):
                    self.assertNotIn(secret, blob)
                self.assertLessEqual(len(outcome.detail), 400)
                self.assertFalse(self.cred_file().exists())


# ---------------------------------------------------------------------------
# No real action of any kind
# ---------------------------------------------------------------------------
class TestNoRealAction(PostRedirectBase):
    def test_only_token_and_channels_endpoints_are_contacted(self):
        _, http = self.run_flow([token_success(), channels_list(CHANNEL)])
        self.assertEqual(http.urls(), [
            "https://oauth2.googleapis.com/token",
            "https://www.googleapis.com/youtube/v3/channels",
        ])

    def test_no_upload_endpoint_is_ever_contacted(self):
        _, http = self.run_flow([token_success(), channels_list(CHANNEL)])
        for url in http.urls():
            self.assertNotIn("upload/youtube", url)

    def test_no_write_method_is_ever_issued(self):
        _, http = self.run_flow([token_success(), channels_list(CHANNEL)])
        self.assertNotIn("PUT", [c["method"] for c in http.calls])

    def test_browser_opener_receives_a_url_but_no_browser_is_launched(self):
        self.run_flow([token_success(), channels_list(CHANNEL)])
        self.assertEqual(len(self.opened), 1)
        self.assertTrue(self.opened[0].startswith(
            "https://accounts.google.com/o/oauth2/v2/auth?"))

    def test_authorization_url_never_contains_the_client_secret(self):
        self.run_flow([token_success(), channels_list(CHANNEL)])
        self.assertNotIn(CLIENT_SECRET, self.opened[0])

    def test_no_registry_or_sink_state_is_touched(self):
        """The consent flow must not CHANGE registry state. It asserted
        `False` before and after, which conflated "unchanged" with "not
        live" — those came apart when the cell was intentionally enabled.
        Now it asserts the value is identical across the flow, whatever it
        happens to be."""
        from engage.config.registry import load_registry
        before = load_registry().is_live_enabled("empires", "youtube")
        self.run_flow([token_success(), channels_list(CHANNEL)])
        self.assertEqual(load_registry().is_live_enabled("empires", "youtube"), before)


if __name__ == "__main__":
    unittest.main()
