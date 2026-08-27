"""Phase 7B-F — the refresh-token AccessTokenProvider.

NO TEST HERE READS THE REAL CREDENTIAL. Every test builds a throwaway
credential in a temp directory. A guard asserts the real credential at
~/.config/engage/youtube/empires.json is byte-for-byte unchanged by the
whole module.

No browser, no network, no consent, no upload endpoint. The HTTP layer is a
recorder that performs no I/O and asserts which endpoints were contacted."""
from __future__ import annotations

import json
import os
import unittest
import tempfile
from pathlib import Path

from engage.publish.credentials import REQUIRED_SCOPES, YOUTUBE_UPLOAD_SCOPE
from engage.publish.youtube_token import (
    AUTH_INVALID,
    AUTH_REFRESH_REJECTED,
    AUTH_SCOPE_MISMATCH,
    AUTH_TRANSPORT_FAILURE,
    AUTH_UNAVAILABLE,
    MAX_REFRESH_ATTEMPTS,
    REDACTED,
    AuthorizationHeader,
    RefreshTokenProvider,
    TokenUnavailable,
)
from engage.publish.youtube_transport import NotAuthorized, TlsUnavailable

CHANNEL = "UC9772FnuAXMVabS0gtr6cew"
REAL_CREDENTIAL = Path.home() / ".config" / "engage" / "youtube" / "empires.json"

# Planted secrets — asserted absent from every surface.
REFRESH = "1//0ePLANTED_REFRESH_TOKEN"
CLIENT_SECRET = "GOCSPX-PLANTED_CLIENT_SECRET"
CLIENT_ID = "1083-planted.apps.googleusercontent.com"
ACCESS = "ya29.PLANTED_ACCESS_TOKEN"
SECRETS = (REFRESH, CLIENT_SECRET, CLIENT_ID, ACCESS)


def refresh_ok(token=ACCESS, expires_in=3599, scope=None):
    doc = {"access_token": token, "expires_in": expires_in, "token_type": "Bearer"}
    if scope is not None:
        doc["scope"] = scope
    return (200, {}, json.dumps(doc).encode())


def oauth_error(status, err, desc="Token has been expired or revoked."):
    return (status, {}, json.dumps({"error": err, "error_description": desc}).encode())


class RecordingHttp:
    def __init__(self, script):
        self.calls, self.script = [], list(script)

    def request(self, method, url, headers, body=None, timeout=300):
        self.calls.append({"method": method, "url": url,
                           "body": body.decode() if isinstance(body, bytes) else body})
        if not self.script:
            raise AssertionError(f"unscripted request: {method} {url}")
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class ProviderBase(unittest.TestCase):
    @staticmethod
    def _real_fingerprint():
        if not REAL_CREDENTIAL.exists():
            return None
        st = REAL_CREDENTIAL.stat()
        return (st.st_mtime_ns, st.st_size, st.st_mode)

    def setUp(self):
        self._real_before = self._real_fingerprint()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "empires.json"
        self.write_credential()
        self.now = 1_000_000

    def tearDown(self):
        self.assertEqual(self._real_fingerprint(), self._real_before,
                         "the REAL credential must be untouched by these tests")
        self.tmp.cleanup()

    def write_credential(self, *, scopes=None, channel=CHANNEL, mode=0o600,
                         omit=(), raw=None):
        if raw is not None:
            self.path.write_text(raw)
        else:
            doc = {"brand": "empires", "platform": "youtube",
                   "scopes": list(scopes if scopes is not None else REQUIRED_SCOPES),
                   "channel_id": channel, "client_id": CLIENT_ID,
                   "client_secret": CLIENT_SECRET, "refresh_token": REFRESH}
            for k in omit:
                doc.pop(k, None)
            self.path.write_text(json.dumps(doc))
        os.chmod(self.path, mode)

    def provider(self, script, **over):
        http = RecordingHttp(script)
        kw = dict(brand="empires", expected_channel_id=CHANNEL,
                  credential_path=self.path, http=http,
                  clock=lambda: self.now, sleep=lambda s: None)
        kw.update(over)
        return RefreshTokenProvider(**kw), http

    def assert_no_secret(self, text):
        for s in SECRETS:
            self.assertNotIn(s, str(text))


# ---------------------------------------------------------------------------
# Happy path + opaque handoff
# ---------------------------------------------------------------------------
class TestRefreshSuccess(ProviderBase):
    def test_valid_refresh_yields_an_authorization_header(self):
        p, http = self.provider([refresh_ok()])
        headers = p.authorization_header()
        self.assertEqual(headers, {"Authorization": f"Bearer {ACCESS}"})
        self.assertEqual(len(http.calls), 1)

    def test_refresh_uses_the_refresh_token_grant_only(self):
        p, http = self.provider([refresh_ok()])
        p.authorization_header()
        import urllib.parse
        body = urllib.parse.parse_qs(http.calls[0]["body"])
        self.assertEqual(body["grant_type"], ["refresh_token"])
        self.assertNotIn("code", body)
        self.assertNotIn("code_verifier", body)

    def test_refresh_never_requests_a_wider_scope(self):
        p, http = self.provider([refresh_ok()])
        p.authorization_header()
        self.assertNotIn("scope", http.calls[0]["body"])

    def test_only_the_token_endpoint_is_contacted(self):
        p, http = self.provider([refresh_ok()])
        p.authorization_header()
        self.assertEqual([c["url"] for c in http.calls],
                         ["https://oauth2.googleapis.com/token"])

    def test_authorization_header_object_is_opaque(self):
        h = AuthorizationHeader("SECRETVALUE", 123)
        self.assertNotIn("SECRETVALUE", repr(h))
        self.assertNotIn("SECRETVALUE", str(h))
        self.assertIn(REDACTED, repr(h))
        self.assertEqual(h.as_headers()["Authorization"], "Bearer SECRETVALUE")

    def test_transport_prefers_the_opaque_header_mechanism(self):
        from engage.publish.youtube_transport import YouTubeApiTransport
        p, _ = self.provider([refresh_ok()])
        t = YouTubeApiTransport(token_provider=p, http=RecordingHttp([]))
        self.assertEqual(t._auth_header(), {"Authorization": f"Bearer {ACCESS}"})


class TestCaching(ProviderBase):
    def test_second_call_reuses_the_cached_token(self):
        p, http = self.provider([refresh_ok()])
        p.authorization_header()
        p.authorization_header()
        self.assertEqual(len(http.calls), 1)

    def test_refresh_happens_once_expiry_is_within_the_margin(self):
        p, http = self.provider([refresh_ok(expires_in=3599),
                                 refresh_ok(token="ya29.SECOND")])
        p.authorization_header()
        self.now += 3599 - 299          # inside the 300s margin
        headers = p.authorization_header()
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(headers["Authorization"], "Bearer ya29.SECOND")

    def test_no_refresh_while_comfortably_valid(self):
        p, http = self.provider([refresh_ok(expires_in=3599)])
        p.authorization_header()
        self.now += 100
        p.authorization_header()
        self.assertEqual(len(http.calls), 1)

    def test_invalidate_forces_a_refresh(self):
        p, http = self.provider([refresh_ok(), refresh_ok(token="ya29.AGAIN")])
        p.authorization_header()
        p.invalidate()
        p.authorization_header()
        self.assertEqual(len(http.calls), 2)

    def test_token_is_never_persisted_to_disk(self):
        p, _ = self.provider([refresh_ok()])
        p.authorization_header()
        for f in Path(self.tmp.name).rglob("*"):
            if f.is_file():
                self.assertNotIn(ACCESS, f.read_text(errors="ignore"))

    def test_zero_expiry_forces_refresh_every_time(self):
        p, http = self.provider([refresh_ok(expires_in=0), refresh_ok(expires_in=0)])
        p.authorization_header()
        p.authorization_header()
        self.assertEqual(len(http.calls), 2)

    def test_constructing_a_provider_performs_no_io(self):
        http = RecordingHttp([])
        RefreshTokenProvider(brand="empires", expected_channel_id=CHANNEL,
                             credential_path=self.path, http=http)
        self.assertEqual(http.calls, [])


# ---------------------------------------------------------------------------
# Credential-state failures
# ---------------------------------------------------------------------------
class TestCredentialFailures(ProviderBase):
    def _expect(self, category, **write):
        if write:
            self.write_credential(**write)
        p, http = self.provider([])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, category)
        self.assertEqual(http.calls, [], "no request may be made on a bad credential")
        self.assert_no_secret(cm.exception)
        return cm.exception

    def test_missing_credential(self):
        self.path.unlink()
        self._expect(AUTH_UNAVAILABLE)

    def test_insecure_permissions_group_readable(self):
        self._expect(AUTH_INVALID, mode=0o640)

    def test_insecure_permissions_world_readable(self):
        self._expect(AUTH_INVALID, mode=0o644)

    def test_malformed_json(self):
        self._expect(AUTH_INVALID, raw="{not json")

    def test_missing_refresh_token(self):
        self._expect(AUTH_INVALID, omit=("refresh_token",))

    def test_missing_client_secret(self):
        self._expect(AUTH_INVALID, omit=("client_secret",))

    def test_missing_scope_rejected(self):
        self._expect(AUTH_SCOPE_MISMATCH, scopes=[YOUTUBE_UPLOAD_SCOPE])

    def test_broader_scope_rejected(self):
        self._expect(AUTH_SCOPE_MISMATCH,
                     scopes=[*REQUIRED_SCOPES, "https://www.googleapis.com/auth/youtube"])

    def test_channel_binding_mismatch(self):
        e = self._expect(AUTH_INVALID, channel="UCsomeoneElse")
        self.assertIn("UCsomeoneElse", str(e))

    def test_missing_channel_binding(self):
        self._expect(AUTH_INVALID, channel="")

    def test_wrong_brand_is_refused(self):
        p, http = self.provider([], brand="capstack")
        with self.assertRaises(TokenUnavailable):
            p.authorization_header()
        self.assertEqual(http.calls, [])


# ---------------------------------------------------------------------------
# Token-endpoint failures
# ---------------------------------------------------------------------------
class TestTokenEndpointFailures(ProviderBase):
    def test_invalid_grant_is_rejected_and_not_retried(self):
        p, http = self.provider([oauth_error(400, "invalid_grant")])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, AUTH_REFRESH_REJECTED)
        self.assertEqual(len(http.calls), 1, "a permanent OAuth error must not be retried")
        self.assertIn("Re-run consent", str(cm.exception))

    def test_every_permanent_oauth_error_is_not_retried(self):
        for err in ("invalid_client", "unauthorized_client", "invalid_scope",
                    "invalid_request", "unsupported_grant_type"):
            with self.subTest(error=err):
                p, http = self.provider([oauth_error(400, err)])
                with self.assertRaises(TokenUnavailable):
                    p.authorization_header()
                self.assertEqual(len(http.calls), 1)

    def test_401_is_rejected_not_retried(self):
        p, http = self.provider([oauth_error(401, "unauthorized")])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, AUTH_REFRESH_REJECTED)
        self.assertEqual(len(http.calls), 1)

    def test_403_is_rejected_not_retried(self):
        p, http = self.provider([oauth_error(403, "forbidden")])
        with self.assertRaises(TokenUnavailable):
            p.authorization_header()
        self.assertEqual(len(http.calls), 1)

    def test_error_description_is_not_echoed_back(self):
        """error_description can reflect request content; only the code is used."""
        p, _ = self.provider([oauth_error(400, "invalid_grant",
                                          desc=f"bad token {REFRESH}")])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assert_no_secret(cm.exception)

    def test_unparseable_body_is_rejected(self):
        p, _ = self.provider([(200, {}, b"<html>not json</html>")])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, AUTH_REFRESH_REJECTED)

    def test_success_without_an_access_token_is_rejected(self):
        p, _ = self.provider([(200, {}, json.dumps({"expires_in": 3599}).encode())])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, AUTH_REFRESH_REJECTED)

    def test_narrowed_granted_scope_is_refused(self):
        p, _ = self.provider([refresh_ok(scope=YOUTUBE_UPLOAD_SCOPE)])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, AUTH_SCOPE_MISMATCH)

    def test_widened_granted_scope_is_refused(self):
        p, _ = self.provider([refresh_ok(
            scope=" ".join([*REQUIRED_SCOPES, "https://www.googleapis.com/auth/youtube"]))])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, AUTH_SCOPE_MISMATCH)

    def test_matching_granted_scope_is_accepted(self):
        p, _ = self.provider([refresh_ok(scope=" ".join(REQUIRED_SCOPES))])
        self.assertIn("Authorization", p.authorization_header())


class TestTransportFailures(ProviderBase):
    def test_network_failure_is_retried_up_to_the_bound(self):
        p, http = self.provider([ConnectionError("net")] * MAX_REFRESH_ATTEMPTS)
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, AUTH_TRANSPORT_FAILURE)
        self.assertEqual(len(http.calls), MAX_REFRESH_ATTEMPTS)

    def test_a_transient_failure_that_then_succeeds_completes(self):
        p, http = self.provider([ConnectionError("net"), refresh_ok()])
        self.assertIn("Authorization", p.authorization_header())
        self.assertEqual(len(http.calls), 2)

    def test_tls_failure_is_not_retried(self):
        p, http = self.provider([TlsUnavailable("no CA bundle")])
        with self.assertRaises(TokenUnavailable) as cm:
            p.authorization_header()
        self.assertEqual(cm.exception.category, AUTH_TRANSPORT_FAILURE)
        self.assertEqual(len(http.calls), 1, "a trust-store problem cannot fix itself")


# ---------------------------------------------------------------------------
# Leakage, contract, and no-action guarantees
# ---------------------------------------------------------------------------
class TestNoLeakage(ProviderBase):
    def test_provider_repr_and_summary_are_redacted(self):
        p, _ = self.provider([refresh_ok()])
        p.authorization_header()
        for probe in (repr(p), str(p), json.dumps(p.summary())):
            self.assert_no_secret(probe)
        self.assertEqual(p.summary()["token_material"], REDACTED)

    def test_summary_reports_cache_state_without_the_value(self):
        p, _ = self.provider([refresh_ok()])
        self.assertFalse(p.summary()["has_cached_token"])
        p.authorization_header()
        self.assertTrue(p.summary()["has_cached_token"])

    def test_no_secret_in_any_failure_category(self):
        cases = [
            ([oauth_error(400, "invalid_grant")], {}),
            ([ConnectionError("x")] * MAX_REFRESH_ATTEMPTS, {}),
            ([refresh_ok(scope=YOUTUBE_UPLOAD_SCOPE)], {}),
        ]
        for script, kw in cases:
            with self.subTest(script=str(script)[:40]):
                p, _ = self.provider(script, **kw)
                with self.assertRaises(TokenUnavailable) as cm:
                    p.authorization_header()
                self.assert_no_secret(cm.exception)
                self.assert_no_secret(repr(cm.exception))

    def test_token_unavailable_subclasses_not_authorized(self):
        """So the transport classifies it as auth_failure with no new path."""
        self.assertTrue(issubclass(TokenUnavailable, NotAuthorized))

    def test_only_the_token_provider_calls_secret_material(self):
        pkg = Path(__file__).resolve().parents[1] / "engage"
        callers = [p.name for p in pkg.rglob("*.py")
                   if ".secret_material()" in p.read_text(encoding="utf-8")]
        self.assertEqual(sorted(callers), ["youtube_token.py"])

    def test_module_has_no_browser_or_upload_capability(self):
        """Checks CODE constructs, not prose. The module legitimately names
        videos.insert and authorization codes in its own 'what it will never
        do' section, which is worth keeping — so this targets imports, URLs,
        and calls instead of bare words."""
        src = (Path(__file__).resolve().parents[1]
               / "engage/publish/youtube_token.py").read_text(encoding="utf-8")
        code_lines = "\n".join(
            line for line in src.splitlines()
            if not line.lstrip().startswith("#")
        ).lower()
        # strip the module docstring before scanning
        if code_lines.count('"""') >= 2:
            first = code_lines.index('"""')
            second = code_lines.index('"""', first + 3)
            code_lines = code_lines[:first] + code_lines[second + 3:]
        for bad in ("import webbrowser", "webbrowser.open", "selenium", "playwright",
                    "upload/youtube/v3", "\"authorization_code\"", "'authorization_code'",
                    "import subprocess", "os.system"):
            with self.subTest(construct=bad):
                self.assertNotIn(bad, code_lines)

    def test_grant_type_sent_is_refresh_token_only(self):
        """The positive counterpart: the only grant this module can perform."""
        src = (Path(__file__).resolve().parents[1]
               / "engage/publish/youtube_token.py").read_text(encoding="utf-8")
        self.assertIn('"grant_type": "refresh_token"', src)

    def test_module_declares_no_brand(self):
        src = (Path(__file__).resolve().parents[1]
               / "engage/publish/youtube_token.py").read_text(encoding="utf-8").lower()
        for term in ("empires", "capstack", "stowecap"):
            with self.subTest(term=term):
                self.assertNotIn(term, src)

    def test_nothing_in_the_package_constructs_this_provider(self):
        """It exists but is wired to nothing — building it enabled nothing."""
        pkg = Path(__file__).resolve().parents[1] / "engage"
        users = [p.name for p in pkg.rglob("*.py")
                 if "RefreshTokenProvider(" in p.read_text(encoding="utf-8")
                 and p.name != "youtube_token.py"]
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
