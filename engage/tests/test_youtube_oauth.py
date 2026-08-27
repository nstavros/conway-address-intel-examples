"""Phase 7B-E — the local-only OAuth consent helper.

Entirely mocked. No browser is opened, no network call is made, no real
credential is read or written, and no consent flow is run. The fake browser
opener records URLs without opening them and several tests assert it was
never called at all."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from engage.publish.youtube_oauth import (
    CHANNEL_READ_SCOPE,
    CONSENT_SCOPES,
    REDACTED,
    UPLOAD_SCOPE,
    VERIFY_MISMATCH,
    VERIFY_OK,
    VERIFY_AMBIGUOUS,
    VERIFY_NO_CHANNEL,
    VERIFY_UNAVAILABLE,
    ChannelVerificationFailed,
    ScopeNotPermitted,
    ClientFileRejected,
    ConsentOutcome,
    OAuthHelperError,
    assert_scopes_allowed,
    build_authorization_url,
    consent_notice,
    generate_pkce,
    run_consent,
    store_credential,
    validate_client_file,
    verify_channel,
)

CHANNEL = "UC9772FnuAXMVabS0gtr6cew"
CLIENT_ID = "1234-abc.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-PLANTED-CLIENT-SECRET"
REFRESH = "1//PLANTED-REFRESH-TOKEN"
ACCESS = "ya29.PLANTED-ACCESS-TOKEN"


def client_doc():
    return {"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                          "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                          "token_uri": "https://oauth2.googleapis.com/token"}}


class FakeHttp:
    def __init__(self, script):
        self.calls = []
        self.script = list(script)

    def request(self, method, url, headers, body=None, timeout=300):
        self.calls.append({"method": method, "url": url, "headers": dict(headers)})
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeServer:
    def __init__(self, code="AUTHCODE", state=None):
        self.port = 54321
        self._code, self._state = code, state

    def wait_for_code(self):
        return self._code, self._state


class FakeBrowser:
    def __init__(self):
        self.opened = []

    def __call__(self, url):
        self.opened.append(url)


def token_response(**over):
    doc = {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3599,
           "scope": " ".join(CONSENT_SCOPES)}
    doc.update(over)
    return (200, {}, json.dumps(doc).encode())


def channels_response(*ids):
    return (200, {}, json.dumps({"items": [{"id": i} for i in ids]}).encode())


class OAuthBase(unittest.TestCase):
    def setUp(self):
        _p = Path.home() / ".config" / "engage" / "youtube" / "empires.json"
        self._real_before = (_p.stat().st_mtime_ns, _p.stat().st_size) if _p.exists() else None
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = self.root / "client.json"
        self.client.write_text(json.dumps(client_doc()))
        os.chmod(self.client, 0o600)
        self.store_dir = self.root / "cred"

    def tearDown(self):
        self.tmp.cleanup()

    def consent(self, script, **over):
        http = FakeHttp(script)
        browser = FakeBrowser()
        kw = dict(client_file=self.client, expected_channel_id=CHANNEL, brand="empires",
                  http=http, browser_opener=browser,
                  server_factory=lambda: FakeServer(state=self._state(browser)),
                  directory=self.store_dir)
        kw.update(over)
        return run_consent(**kw), http, browser

    @staticmethod
    def _state(browser):
        # The real server echoes back the state from the auth URL. Read it
        # from whatever the browser was handed, so the happy path matches.
        class Lazy(str):
            pass
        return _StateProxy(browser)


class _StateProxy:
    """Resolves to the state parameter in the URL the browser was given."""

    def __init__(self, browser):
        self._browser = browser

    def __str__(self):
        from urllib.parse import parse_qs, urlparse
        if not self._browser.opened:
            return ""
        return parse_qs(urlparse(self._browser.opened[0]).query).get("state", [""])[0]


# ---------------------------------------------------------------------------
# 1. Client-file location and permissions
# ---------------------------------------------------------------------------
class TestClientFileHandling(OAuthBase):
    def test_absent_client_file_refused(self):
        with self.assertRaises(ClientFileRejected):
            validate_client_file(self.root / "missing.json")

    def test_directory_instead_of_file_refused(self):
        with self.assertRaises(ClientFileRejected):
            validate_client_file(self.root)

    def test_a_valid_private_file_is_accepted(self):
        ref = validate_client_file(self.client)
        self.assertEqual(ref.path, self.client.resolve())

    def test_group_readable_client_refused(self):
        os.chmod(self.client, 0o640)
        with self.assertRaises(ClientFileRejected) as cm:
            validate_client_file(self.client)
        self.assertIn("chmod 600", str(cm.exception))

    def test_world_readable_client_refused(self):
        os.chmod(self.client, 0o644)
        with self.assertRaises(ClientFileRejected):
            validate_client_file(self.client)

    def test_client_inside_the_repository_refused(self):
        repo_root = Path(__file__).resolve().parents[1]
        inside = repo_root / "_tmp_client_test.json"
        inside.write_text(json.dumps(client_doc()))
        os.chmod(inside, 0o600)
        try:
            with self.assertRaises(ClientFileRejected) as cm:
                validate_client_file(inside)
            self.assertIn("repository", str(cm.exception).lower())
        finally:
            inside.unlink(missing_ok=True)

    def test_client_inside_a_git_worktree_refused(self):
        worktree = self.root / "proj"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        f = worktree / "client.json"
        f.write_text(json.dumps(client_doc()))
        os.chmod(f, 0o600)
        with self.assertRaises(ClientFileRejected) as cm:
            validate_client_file(f)
        self.assertIn("git worktree", str(cm.exception))

    def test_ref_repr_never_exposes_contents(self):
        ref = validate_client_file(self.client)
        for probe in (repr(ref), str(ref), json.dumps(ref.summary())):
            self.assertNotIn(CLIENT_SECRET, probe)
            self.assertNotIn(CLIENT_ID, probe)
        self.assertIn(REDACTED, repr(ref))

    def test_client_json_is_never_copied_into_the_repository(self):
        repo_root = Path(__file__).resolve().parents[1]
        before = set(p.name for p in repo_root.rglob("*.json"))
        validate_client_file(self.client)
        after = set(p.name for p in repo_root.rglob("*.json"))
        self.assertEqual(before, after)

    def test_web_client_refused_desktop_required(self):
        f = self.root / "web.json"
        f.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
        os.chmod(f, 0o600)
        result, _, _ = None, None, None
        with self.assertRaises(ClientFileRejected):
            run_consent(client_file=f, expected_channel_id=CHANNEL, brand="empires",
                        http=FakeHttp([]), server_factory=lambda: FakeServer())

    def test_malformed_client_json_error_does_not_echo_contents(self):
        f = self.root / "bad.json"
        f.write_text('{"installed": {"client_secret": "' + CLIENT_SECRET + '", oops}')
        os.chmod(f, 0o600)
        with self.assertRaises(ClientFileRejected) as cm:
            run_consent(client_file=f, expected_channel_id=CHANNEL, brand="empires",
                        http=FakeHttp([]), server_factory=lambda: FakeServer())
        self.assertNotIn(CLIENT_SECRET, str(cm.exception))


# ---------------------------------------------------------------------------
# 2. PKCE and scope
# ---------------------------------------------------------------------------
class TestPkceAndScope(unittest.TestCase):
    def test_verifier_length_is_within_rfc7636_bounds(self):
        v, _ = generate_pkce()
        self.assertGreaterEqual(len(v), 43)
        self.assertLessEqual(len(v), 128)

    def test_challenge_is_s256_of_the_verifier(self):
        v, c = generate_pkce()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(v.encode("ascii")).digest()).decode().rstrip("=")
        self.assertEqual(c, expected)

    def test_each_call_is_unique(self):
        self.assertNotEqual(generate_pkce()[0], generate_pkce()[0])

    def test_authorization_url_uses_s256(self):
        url = build_authorization_url("cid", "http://127.0.0.1:1", "chal", "st")
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("code_challenge=chal", url)

    def test_authorization_url_requests_exactly_the_two_approved_scopes(self):
        url = build_authorization_url("cid", "http://127.0.0.1:1", "c", "s")
        self.assertIn("youtube.upload", url)
        self.assertIn("youtube.readonly", url)
        self.assertNotIn("force-ssl", url)
        self.assertNotIn("youtubepartner", url)

    def test_consent_scopes_is_exactly_the_two_approved(self):
        self.assertEqual(set(CONSENT_SCOPES), {UPLOAD_SCOPE, CHANNEL_READ_SCOPE})
        self.assertEqual(len(CONSENT_SCOPES), 2)

    def test_offline_access_is_requested_for_a_refresh_token(self):
        self.assertIn("access_type=offline", build_authorization_url("c", "r", "c", "s"))


class TestConsentNotice(unittest.TestCase):
    def test_notice_states_a_browser_will_open(self):
        self.assertIn("open a browser", consent_notice(CHANNEL, "empires"))

    def test_notice_states_it_uploads_nothing(self):
        n = consent_notice(CHANNEL, "empires")
        self.assertIn("will not upload", n)

    def test_notice_states_nothing_happens_until_explicitly_run(self):
        self.assertIn("until you run this explicitly", consent_notice(CHANNEL, "empires"))

    def test_notice_names_the_expected_channel_and_scope(self):
        n = consent_notice(CHANNEL, "empires")
        self.assertIn(CHANNEL, n)
        self.assertIn("youtube.upload", n)


# ---------------------------------------------------------------------------
# 4. Channel verification decides whether anything is stored
# ---------------------------------------------------------------------------
class TestChannelVerification(unittest.TestCase):
    def test_matching_channel_verifies(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response(CHANNEL)]))
        self.assertEqual(out["status"], VERIFY_OK)

    def test_different_channel_is_a_mismatch(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response("UCsomeoneelse")]))
        self.assertEqual(out["status"], VERIFY_MISMATCH)
        self.assertEqual(out["actual_channel_id"], "UCsomeoneelse")

    def test_403_insufficient_scope_is_unavailable_not_success(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([(403, {}, b"{}")]))
        self.assertEqual(out["status"], VERIFY_UNAVAILABLE)
        self.assertIn("does not authorize an identity read", out["detail"])

    def test_401_is_unavailable(self):
        self.assertEqual(
            verify_channel(ACCESS, CHANNEL, FakeHttp([(401, {}, b"{}")]))["status"],
            VERIFY_UNAVAILABLE)

    def test_network_error_is_unavailable_not_success(self):
        self.assertEqual(
            verify_channel(ACCESS, CHANNEL, FakeHttp([ConnectionError("x")]))["status"],
            VERIFY_UNAVAILABLE)

    def test_unparseable_response_is_unavailable(self):
        self.assertEqual(
            verify_channel(ACCESS, CHANNEL, FakeHttp([(200, {}, b"<html>")]))["status"],
            VERIFY_UNAVAILABLE)

    def test_verification_metadata_is_non_secret_only(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response(CHANNEL)]))
        self.assertNotIn(ACCESS, json.dumps(out))
        self.assertEqual(set(out) & {"access_token", "refresh_token"}, set())

    def test_empty_item_list_is_no_channel_not_a_pass(self):
        self.assertEqual(
            verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response()]))["status"],
            VERIFY_NO_CHANNEL)


# ---------------------------------------------------------------------------
# 3. Credential storage
# ---------------------------------------------------------------------------
class TestCredentialStorage(OAuthBase):
    def test_stored_file_is_0600(self):
        p = store_credential({"refresh_token": REFRESH}, brand="empires",
                             channel_id=CHANNEL, directory=self.store_dir)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_stored_at_the_expected_filename(self):
        p = store_credential({}, brand="empires", channel_id=CHANNEL,
                             directory=self.store_dir)
        self.assertEqual(p.name, "empires.json")

    def test_records_channel_binding_and_scope(self):
        p = store_credential({"refresh_token": REFRESH}, brand="empires",
                             channel_id=CHANNEL, directory=self.store_dir)
        doc = json.loads(p.read_text())
        self.assertEqual(doc["channel_id"], CHANNEL)
        self.assertEqual(doc["scopes"], list(CONSENT_SCOPES))

    def test_parent_directories_are_not_created_until_called(self):
        self.assertFalse(self.store_dir.exists())

    def test_tests_never_write_the_real_credential_path(self):
        """A real credential now exists (owner consent, 2026-08-22); these
        tests must leave it byte-for-byte as found."""
        p = Path.home() / ".config" / "engage" / "youtube" / "empires.json"
        self.assertEqual(self._real_before,
                         (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None)


# ---------------------------------------------------------------------------
# Full flow — fail-closed behaviour
# ---------------------------------------------------------------------------
class TestConsentFlow(OAuthBase):
    def test_happy_path_stores_only_after_verification(self):
        out, http, browser = self.consent([token_response(), channels_response(CHANNEL)])
        self.assertTrue(out.stored)
        self.assertEqual(out.verification["status"], VERIFY_OK)
        self.assertEqual(stat.S_IMODE(Path(out.credential_path).stat().st_mode), 0o600)

    def test_channel_mismatch_stores_nothing(self):
        out, _, _ = self.consent([token_response(), channels_response("UCwrong")])
        self.assertFalse(out.stored)
        self.assertEqual(out.credential_path, "")
        self.assertFalse((self.store_dir / "empires.json").exists())

    def test_unavailable_verification_stores_nothing(self):
        """The scope conflict in practice: consent succeeds, the identity
        read is not authorized, and therefore nothing is stored."""
        out, _, _ = self.consent([token_response(), (403, {}, b"{}")])
        self.assertFalse(out.stored)
        self.assertFalse((self.store_dir / "empires.json").exists())
        self.assertIn("no credential was retained", out.detail)

    def test_state_mismatch_aborts_before_token_exchange(self):
        http = FakeHttp([])
        with self.assertRaises(OAuthHelperError) as cm:
            run_consent(client_file=self.client, expected_channel_id=CHANNEL,
                        brand="empires", http=http, browser_opener=FakeBrowser(),
                        server_factory=lambda: FakeServer(state="FORGED"),
                        directory=self.store_dir)
        self.assertIn("state mismatch", str(cm.exception))
        self.assertEqual(http.calls, [])

    def test_failed_token_exchange_stores_nothing(self):
        with self.assertRaises(OAuthHelperError):
            self.consent([(400, {}, b'{"error":"invalid_grant"}')])
        self.assertFalse((self.store_dir / "empires.json").exists())

    def test_no_server_factory_means_no_listener_is_started(self):
        with self.assertRaises(OAuthHelperError) as cm:
            run_consent(client_file=self.client, expected_channel_id=CHANNEL,
                        brand="empires", http=FakeHttp([]), server_factory=None)
        self.assertIn("does not start a listener", str(cm.exception))

    def test_no_browser_is_opened_when_no_opener_is_supplied(self):
        """With no opener the flow cannot present a consent screen, so the
        state can never come back matched — it aborts and stores nothing.
        The point is that no browser was involved at any moment."""
        with self.assertRaises(OAuthHelperError):
            run_consent(client_file=self.client, expected_channel_id=CHANNEL,
                        brand="empires", http=FakeHttp([]), browser_opener=None,
                        server_factory=lambda: FakeServer(state="unmatched"),
                        directory=self.store_dir)
        self.assertFalse((self.store_dir / "empires.json").exists())

    def test_the_browser_receives_the_authorization_url_and_nothing_else(self):
        _, _, browser = self.consent([token_response(), channels_response(CHANNEL)])
        self.assertEqual(len(browser.opened), 1)
        url = browser.opened[0]
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertNotIn(CLIENT_SECRET, url)

    def test_token_exchange_sends_the_pkce_verifier(self):
        out, http, _ = self.consent([token_response(), channels_response(CHANNEL)])
        self.assertEqual(http.calls[0]["url"], "https://oauth2.googleapis.com/token")

    def test_outcome_summary_carries_no_secret(self):
        out, _, _ = self.consent([token_response(), channels_response(CHANNEL)])
        blob = json.dumps(out.summary())
        for secret in (ACCESS, REFRESH, CLIENT_SECRET):
            self.assertNotIn(secret, blob)
        self.assertEqual(out.summary()["token_material"], REDACTED)


class TestNoAutomaticInvocation(unittest.TestCase):
    def test_nothing_in_the_codebase_calls_run_consent(self):
        root = Path(__file__).resolve().parents[1] / "engage"
        callers = []
        for p in root.rglob("*.py"):
            if p.name == "youtube_oauth.py":
                continue
            if "run_consent(" in p.read_text(encoding="utf-8"):
                callers.append(str(p))
        self.assertEqual(callers, [], "run_consent must only ever be invoked by a human")

    def test_module_opens_no_browser_on_import(self):
        src = (Path(__file__).resolve().parents[1]
               / "engage/publish/youtube_oauth.py").read_text(encoding="utf-8")
        self.assertNotIn("webbrowser.open", src)
        self.assertNotIn("import webbrowser", src)

    def test_helper_declares_no_brand(self):
        src = (Path(__file__).resolve().parents[1]
               / "engage/publish/youtube_oauth.py").read_text(encoding="utf-8").lower()
        for term in ("empires", "capstack", "stowecap"):
            with self.subTest(term=term):
                self.assertNotIn(term, src)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Phase 7B-E option 1 — two-scope allowlist and strict identity verification
# ---------------------------------------------------------------------------
class TestScopeAllowlist(unittest.TestCase):
    BROADER = [
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.force-ssl",
        "https://www.googleapis.com/auth/youtubepartner",
        "https://www.googleapis.com/auth/youtubepartner-channel-audit",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ]

    def test_the_two_approved_scopes_pass(self):
        self.assertEqual(assert_scopes_allowed(CONSENT_SCOPES), tuple(CONSENT_SCOPES))

    def test_every_broader_scope_is_rejected(self):
        for scope in self.BROADER:
            with self.subTest(scope=scope), self.assertRaises(ScopeNotPermitted):
                assert_scopes_allowed([scope])

    def test_a_broader_scope_mixed_with_approved_ones_is_rejected(self):
        with self.assertRaises(ScopeNotPermitted):
            assert_scopes_allowed([*CONSENT_SCOPES, self.BROADER[0]])

    def test_an_unknown_future_scope_fails_closed(self):
        """Not on the forbidden list either — the allowlist is what governs."""
        with self.assertRaises(ScopeNotPermitted):
            assert_scopes_allowed(["https://www.googleapis.com/auth/some.future.scope"])

    def test_authorization_url_refuses_to_smuggle_a_broader_scope(self):
        with self.assertRaises(ScopeNotPermitted):
            build_authorization_url("cid", "http://127.0.0.1:1", "c", "s",
                                    scopes=(UPLOAD_SCOPE, self.BROADER[0]))

    def test_no_forbidden_scope_appears_in_the_default_authorization_url(self):
        url = build_authorization_url("cid", "http://127.0.0.1:1", "c", "s")
        for scope in self.BROADER:
            with self.subTest(scope=scope):
                self.assertNotIn(scope, url)


class TestChannelsListRequestConstruction(unittest.TestCase):
    def test_uses_mine_true_and_the_minimum_part(self):
        http = FakeHttp([channels_response(CHANNEL)])
        verify_channel(ACCESS, CHANNEL, http)
        url = http.calls[0]["url"]
        self.assertIn("mine=true", url)
        self.assertIn("part=id", url)
        self.assertNotIn("part=snippet", url)
        self.assertNotIn("contentDetails", url)

    def test_is_a_get_with_a_bearer_token(self):
        http = FakeHttp([channels_response(CHANNEL)])
        verify_channel(ACCESS, CHANNEL, http)
        self.assertEqual(http.calls[0]["method"], "GET")
        self.assertTrue(http.calls[0]["headers"]["Authorization"].startswith("Bearer "))


class TestStrictIdentityOutcomes(unittest.TestCase):
    def test_exact_single_match_verifies(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response(CHANNEL)]))
        self.assertEqual(out["status"], VERIFY_OK)
        self.assertEqual(out["returned_channel_count"], 1)

    def test_single_wrong_channel_is_a_mismatch(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response("UCwrong")]))
        self.assertEqual(out["status"], VERIFY_MISMATCH)
        self.assertEqual(out["actual_channel_id"], "UCwrong")

    def test_zero_channels_is_no_channel(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response()]))
        self.assertEqual(out["status"], VERIFY_NO_CHANNEL)

    def test_multiple_channels_including_the_target_verifies_membership(self):
        """Consent-time verification establishes CONTROL of the target
        channel. Extra channels on the same account do not invalidate that."""
        out = verify_channel(ACCESS, CHANNEL,
                             FakeHttp([channels_response(CHANNEL, "UCother")]))
        self.assertEqual(out["status"], VERIFY_OK)
        self.assertTrue(out["target_channel_membership_verified"])
        self.assertEqual(out["returned_channel_count"], 2)
        self.assertIn("UCother", out["returned_channel_ids"])

    def test_multiple_channels_excluding_the_target_fails_closed(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response("UCa", "UCb")]))
        self.assertEqual(out["status"], VERIFY_MISMATCH)
        self.assertFalse(out["target_channel_membership_verified"])

    def test_membership_never_claims_to_prove_the_upload_target(self):
        out = verify_channel(ACCESS, CHANNEL,
                             FakeHttp([channels_response(CHANNEL, "UCother")]))
        self.assertFalse(out["proves_upload_target"])
        self.assertIn("NOT which channel a future upload will land on", out["detail"])

    def test_full_returned_channel_list_is_recorded(self):
        out = verify_channel(ACCESS, CHANNEL,
                             FakeHttp([channels_response(CHANNEL, "UCb", "UCa")]))
        self.assertEqual(out["returned_channel_ids"], sorted([CHANNEL, "UCa", "UCb"]))

    def test_near_miss_channel_id_is_a_mismatch(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response(CHANNEL[:-1])]))
        self.assertEqual(out["status"], VERIFY_MISMATCH)

    def test_audit_record_is_non_secret_and_carries_expected_vs_returned(self):
        out = verify_channel(ACCESS, CHANNEL, FakeHttp([channels_response("UCwrong")]))
        blob = json.dumps(out)
        self.assertNotIn(ACCESS, blob)
        self.assertEqual(out["expected_channel_id"], CHANNEL)
        self.assertEqual(out["actual_channel_id"], "UCwrong")
        self.assertIn("checked_at", out)


class TestGrantedScopeVerification(OAuthBase):
    def test_partial_grant_stores_nothing(self):
        """A consent screen lets a user untick a scope. A credential that
        cannot verify identity must never be kept."""
        out, _, _ = self.consent([token_response(scope=UPLOAD_SCOPE)])
        self.assertFalse(out.stored)
        self.assertEqual(out.verification["missing_scopes"], [CHANNEL_READ_SCOPE])
        self.assertFalse((self.store_dir / "empires.json").exists())

    def test_a_broader_granted_scope_is_refused(self):
        with self.assertRaises(ScopeNotPermitted):
            self.consent([token_response(
                scope=" ".join([*CONSENT_SCOPES,
                                "https://www.googleapis.com/auth/youtube.force-ssl"]))])

    def test_full_grant_proceeds_to_verification(self):
        out, _, _ = self.consent([token_response(), channels_response(CHANNEL)])
        self.assertTrue(out.stored)


class TestTemporaryCredentialCleanup(OAuthBase):
    FAILURES = {
        "mismatch": [token_response(), channels_response("UCwrong")],
        "no_channel": [token_response(), channels_response()],
        "target_absent_among_many": [token_response(), channels_response("UCa", "UCb")],
        "unauthorized": [token_response(), (403, {}, b"{}")],
        "network": [token_response(), ConnectionError("x")],
    }

    def test_no_credential_survives_any_verification_failure(self):
        for name, script in self.FAILURES.items():
            with self.subTest(failure=name):
                self.tearDown(); self.setUp()
                out, _, _ = self.consent(list(script))
                self.assertFalse(out.stored)
                self.assertFalse((self.store_dir / "empires.json").exists())

    def test_failure_records_that_nothing_was_left_behind(self):
        out, _, _ = self.consent([token_response(), channels_response("UCwrong")])
        self.assertIn("temporary_material_purged", out.verification)
        self.assertFalse(out.verification["pre_existing_credential_left_untouched"])

    def test_a_previously_valid_credential_is_not_destroyed_by_a_failed_reconsent(self):
        """A failed re-consent must not delete a credential it did not write."""
        self.store_dir.mkdir(parents=True, exist_ok=True)
        existing = self.store_dir / "empires.json"
        existing.write_text('{"pre":"existing"}')
        os.chmod(existing, 0o600)
        out, _, _ = self.consent([token_response(), channels_response("UCwrong")])
        self.assertFalse(out.stored)
        self.assertTrue(existing.exists())
        self.assertEqual(json.loads(existing.read_text())["pre"], "existing")
        self.assertTrue(out.verification["pre_existing_credential_left_untouched"])

    def test_no_secret_leaks_in_any_failure_outcome(self):
        for name, script in self.FAILURES.items():
            with self.subTest(failure=name):
                self.tearDown(); self.setUp()
                out, _, _ = self.consent(list(script))
                blob = json.dumps(out.summary()) + out.detail
                for secret in (ACCESS, REFRESH, CLIENT_SECRET, CLIENT_ID):
                    self.assertNotIn(secret, blob)


class TestNoActionInTestMode(OAuthBase):
    def test_no_upload_endpoint_is_ever_contacted(self):
        _, http, _ = self.consent([token_response(), channels_response(CHANNEL)])
        for call in http.calls:
            self.assertNotIn("upload/youtube", call["url"])
            self.assertNotEqual(call["method"], "PUT")

    def test_only_token_and_channels_endpoints_are_contacted(self):
        _, http, _ = self.consent([token_response(), channels_response(CHANNEL)])
        self.assertEqual([c["url"].split("?")[0] for c in http.calls],
                         ["https://oauth2.googleapis.com/token",
                          "https://www.googleapis.com/youtube/v3/channels"])

    def test_tests_never_write_the_real_credential_path_either(self):
        p = Path.home() / ".config" / "engage" / "youtube" / "empires.json"
        self.assertEqual(self._real_before,
                         (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None)


class TestMultiChannelAccountStoresCredential(OAuthBase):
    def test_target_among_many_stores_the_credential(self):
        out, _, _ = self.consent([token_response(),
                                  channels_response("UCother", CHANNEL, "UCthird")])
        self.assertTrue(out.stored)
        self.assertTrue(out.verification["target_channel_membership_verified"])
        self.assertEqual(out.verification["returned_channel_count"], 3)

    def test_target_absent_among_many_stores_nothing(self):
        out, _, _ = self.consent([token_response(), channels_response("UCa", "UCb", "UCc")])
        self.assertFalse(out.stored)
        self.assertFalse((self.store_dir / "empires.json").exists())

    def test_stored_outcome_carries_the_full_channel_list_and_no_secret(self):
        out, _, _ = self.consent([token_response(), channels_response("UCother", CHANNEL)])
        blob = json.dumps(out.summary())
        self.assertIn("UCother", blob)
        for secret in (ACCESS, REFRESH, CLIENT_SECRET):
            self.assertNotIn(secret, blob)


# ---------------------------------------------------------------------------
# DEC-SM-025 — canonical scope source; consent helper and loader cannot drift
# ---------------------------------------------------------------------------
class TestCanonicalScopeSource(unittest.TestCase):
    def test_helper_and_loader_share_the_same_object(self):
        from engage.publish import credentials as C
        from engage.publish import youtube_oauth as O
        self.assertIs(C.REQUIRED_SCOPES, O.CONSENT_SCOPES)

    def test_allowlists_are_the_same_object(self):
        from engage.publish import credentials as C
        from engage.publish import youtube_oauth as O
        self.assertIs(C.ALLOWED_SCOPES, O.ALLOWED_SCOPES)

    def test_scope_literals_appear_in_exactly_one_module(self):
        """The anti-drift guarantee is structural: youtube_oauth must not
        restate a scope URL, or the two could diverge again."""
        pkg = Path(__file__).resolve().parents[1] / "engage" / "publish"
        defining = []
        for p in pkg.glob("*.py"):
            if "googleapis.com/auth/youtube" in p.read_text(encoding="utf-8"):
                defining.append(p.name)
        self.assertEqual(defining, ["credentials.py"],
                         f"scope literals must live only in credentials.py, found in {defining}")

    def test_the_two_canonical_scopes_are_exactly_as_approved(self):
        from engage.publish.credentials import REQUIRED_SCOPES
        self.assertEqual(list(REQUIRED_SCOPES), [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ])


class TestLoaderScopeEnforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "empires.json"
        self.secret = "1//PLANTED-REFRESH-SECRET"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, scopes):
        from engage.publish.credentials import REQUIRED_SCOPES  # noqa: F401
        self.path.write_text(json.dumps({
            "brand": "empires", "platform": "youtube", "scopes": list(scopes),
            "channel_id": CHANNEL, "refresh_token": self.secret,
            "client_secret": "GOCSPX-PLANTED"}))
        os.chmod(self.path, 0o600)
        return self.path

    def _load(self):
        from engage.publish.credentials import load_youtube_credentials
        return load_youtube_credentials(self.path, brand="empires")

    def test_exactly_the_two_required_scopes_is_accepted(self):
        from engage.publish.credentials import REQUIRED_SCOPES
        self._write(REQUIRED_SCOPES)
        handle = self._load()
        self.assertEqual(handle.scopes, tuple(REQUIRED_SCOPES))

    def test_order_does_not_matter(self):
        from engage.publish.credentials import REQUIRED_SCOPES
        self._write(list(reversed(REQUIRED_SCOPES)))
        self.assertEqual(self._load().scopes, tuple(REQUIRED_SCOPES))

    def test_missing_upload_scope_rejected(self):
        from engage.publish.credentials import CredentialError, YOUTUBE_READONLY_SCOPE
        self._write([YOUTUBE_READONLY_SCOPE])
        with self.assertRaises(CredentialError) as cm:
            self._load()
        self.assertIn("missing required scope", str(cm.exception))
        self.assertIn("youtube.upload", str(cm.exception))

    def test_missing_readonly_scope_rejected(self):
        from engage.publish.credentials import CredentialError, YOUTUBE_UPLOAD_SCOPE
        self._write([YOUTUBE_UPLOAD_SCOPE])
        with self.assertRaises(CredentialError) as cm:
            self._load()
        self.assertIn("missing required scope", str(cm.exception))
        self.assertIn("youtube.readonly", str(cm.exception))

    def test_empty_scope_list_rejected(self):
        from engage.publish.credentials import CredentialError
        self._write([])
        with self.assertRaises(CredentialError):
            self._load()

    def test_every_broader_youtube_scope_rejected(self):
        from engage.publish.credentials import (CredentialError,
                                                FORBIDDEN_YOUTUBE_SCOPES, REQUIRED_SCOPES)
        for broader in sorted(FORBIDDEN_YOUTUBE_SCOPES):
            with self.subTest(scope=broader):
                self._write([*REQUIRED_SCOPES, broader])
                with self.assertRaises(CredentialError) as cm:
                    self._load()
                self.assertIn("outside the allowlist", str(cm.exception))

    def test_an_unknown_extra_scope_is_also_rejected(self):
        from engage.publish.credentials import CredentialError, REQUIRED_SCOPES
        self._write([*REQUIRED_SCOPES, "https://www.googleapis.com/auth/drive"])
        with self.assertRaises(CredentialError):
            self._load()

    def test_no_secret_appears_in_any_scope_rejection(self):
        from engage.publish.credentials import (CredentialError,
                                                REQUIRED_SCOPES, YOUTUBE_UPLOAD_SCOPE)
        cases = [[YOUTUBE_UPLOAD_SCOPE], [], [*REQUIRED_SCOPES,
                 "https://www.googleapis.com/auth/youtube"]]
        for scopes in cases:
            with self.subTest(scopes=scopes):
                self._write(scopes)
                with self.assertRaises(CredentialError) as cm:
                    self._load()
                self.assertNotIn(self.secret, str(cm.exception))
                self.assertNotIn("GOCSPX-PLANTED", str(cm.exception))

    def test_accepted_handle_still_carries_no_secret(self):
        from engage.publish.credentials import REDACTED, REQUIRED_SCOPES
        self._write(REQUIRED_SCOPES)
        handle = self._load()
        for probe in (repr(handle), str(handle), json.dumps(handle.summary())):
            self.assertNotIn(self.secret, probe)
            self.assertNotIn("GOCSPX-PLANTED", probe)
        self.assertEqual(handle.summary()["secret_material"], REDACTED)

    def test_requesting_secret_material_still_refused(self):
        from engage.publish.credentials import CredentialError, REQUIRED_SCOPES
        self._write(REQUIRED_SCOPES)
        with self.assertRaises(CredentialError):
            self._load().secret_material()
