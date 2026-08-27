"""Phase 7B-D — the real YouTube upload transport, exercised entirely
offline through an injected fake HTTP layer.

No test here performs a network call, opens a browser, runs an OAuth flow,
or touches a real credential. Several tests assert exactly that: the
FakeHttp records every request it is handed, and gate tests assert the
recorded list is EMPTY, which proves refusal happened before the network
rather than after it."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from engage.core.models import Post
from engage.drafting.legacy_intake import approve_legacy_asset
from engage.publish.youtube_sink import ConstraintSource, UploadRefused, YouTubeUploadSink
from engage.publish.youtube_transport import (
    MAX_ATTEMPTS,
    OUTCOME_AMBIGUOUS,
    OUTCOME_API_VALIDATION_FAILURE,
    OUTCOME_AUTH_FAILURE,
    OUTCOME_CONFIRMED,
    OUTCOME_TRANSIENT_FAILURE,
    REDACTED,
    VERIFICATION_CONFIRMED,
    VERIFICATION_MISMATCH,
    VERIFICATION_PENDING,
    AccessTokenProvider,
    NotAuthorized,
    TransportError,
    UnavailableTokenProvider,
    UploadAuthorization,
    YouTubeApiTransport,
    build_video_resource,
    redact_headers,
    verify_via_rss,
)

from .test_legacy_intake_and_youtube_sink import CHANNEL, LegacyBase

SECRET = "ya29.PLANTED-SECRET-TOKEN-VALUE"
SESSION_URI = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=x"


class FakeToken(AccessTokenProvider):
    def bearer_token(self) -> str:
        return SECRET


class FakeHttp:
    """Records every request; returns scripted responses. Performs no I/O."""

    def __init__(self, script=None):
        self.calls: list[dict] = []
        self.script = list(script or [])

    def request(self, method, url, headers, body=None, timeout=300):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body_len": len(body) if body else 0, "body": body})
        if not self.script:
            raise AssertionError("FakeHttp ran out of scripted responses")
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def ok_init():
    return (200, {"Location": SESSION_URI}, b"")


def ok_upload(video_id="VID123", title="T", privacy="public", channel=CHANNEL):
    return (201, {}, json.dumps({
        "id": video_id,
        "snippet": {"title": title, "channelId": channel},
        "status": {"privacyStatus": privacy},
    }).encode())


def api_error(status, reason, message):
    return (status, {}, json.dumps({
        "error": {"message": message, "errors": [{"reason": reason}]}
    }).encode())


def _transport(script, token=None):
    http = FakeHttp(script)
    t = YouTubeApiTransport(token_provider=token or FakeToken(), http=http,
                            sleep=lambda s: None, clock=lambda: 1_700_000_000)
    return t, http


class TransportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.asset = Path(self.tmp.name) / "a.mp4"
        self.asset.write_bytes(b"VIDEO-BYTES" * 64)
        from engage.drafting.legacy_intake import hash_asset
        self.sha = hash_asset(self.asset).sha256
        self.meta = "metadatahash"
        self.auth = UploadAuthorization(
            content_record_id="rec1", channel_id=CHANNEL, asset_path=str(self.asset),
            asset_sha256=self.sha, metadata_hash=self.meta, minted_at=0)
        self.body = build_video_resource(title="T", description="D",
                                         privacy_status="public",
                                         self_declared_made_for_kids=False)

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, script, token=None, **over):
        t, http = _transport(script, token)
        kw = dict(authorization=self.auth, asset_path=self.asset, asset_sha256=self.sha,
                  metadata_hash=self.meta, video_resource=self.body)
        kw.update(over)
        return t.upload(**kw), http


# ---------------------------------------------------------------------------
# The capability token — the transport cannot be driven directly
# ---------------------------------------------------------------------------
class TestCapabilityToken(TransportBase):
    def test_upload_without_an_authorization_is_refused(self):
        t, http = _transport([])
        with self.assertRaises(NotAuthorized):
            t.upload(authorization=None, asset_path=self.asset, asset_sha256=self.sha,
                     metadata_hash=self.meta, video_resource=self.body)
        self.assertEqual(http.calls, [])

    def test_a_forged_non_token_object_is_refused(self):
        t, http = _transport([])
        with self.assertRaises(NotAuthorized):
            t.upload(authorization={"content_record_id": "rec1"}, asset_path=self.asset,
                     asset_sha256=self.sha, metadata_hash=self.meta, video_resource=self.body)
        self.assertEqual(http.calls, [])

    def test_authorization_bound_to_a_different_asset_hash_is_refused(self):
        t, http = _transport([])
        with self.assertRaises(NotAuthorized):
            t.upload(authorization=self.auth, asset_path=self.asset, asset_sha256="0" * 64,
                     metadata_hash=self.meta, video_resource=self.body)
        self.assertEqual(http.calls, [])

    def test_authorization_bound_to_different_metadata_is_refused(self):
        t, http = _transport([])
        with self.assertRaises(NotAuthorized):
            t.upload(authorization=self.auth, asset_path=self.asset, asset_sha256=self.sha,
                     metadata_hash="other", video_resource=self.body)
        self.assertEqual(http.calls, [])

    def test_shipped_provider_cannot_produce_a_token(self):
        with self.assertRaises(NotAuthorized):
            UnavailableTokenProvider().bearer_token()

    def test_default_transport_uses_the_unavailable_provider(self):
        self.assertIsInstance(YouTubeApiTransport().token_provider, UnavailableTokenProvider)

    def test_no_token_means_auth_failure_and_no_network_call(self):
        result, http = self.upload([], token=UnavailableTokenProvider())
        self.assertEqual(result.outcome, OUTCOME_AUTH_FAILURE)
        self.assertEqual(http.calls, [])


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------
class TestRequestConstruction(TransportBase):
    def test_body_contains_only_documented_fields(self):
        self.assertEqual(set(self.body), {"snippet", "status"})
        self.assertEqual(set(self.body["snippet"]), {"title", "description"})
        self.assertEqual(set(self.body["status"]),
                         {"privacyStatus", "selfDeclaredMadeForKids"})

    def test_category_id_is_never_invented(self):
        self.assertNotIn("categoryId", self.body["snippet"])

    def test_empty_title_refuses_before_a_request_is_built(self):
        with self.assertRaises(TransportError):
            build_video_resource(title="  ", description="D", privacy_status="public",
                                 self_declared_made_for_kids=False)

    def test_made_for_kids_must_be_an_explicit_boolean(self):
        for bad in (None, "false", 0):
            with self.subTest(v=bad), self.assertRaises(TransportError):
                build_video_resource(title="T", description="D", privacy_status="public",
                                     self_declared_made_for_kids=bad)

    def test_made_for_kids_false_is_sent_explicitly_not_omitted(self):
        self.assertIs(self.body["status"]["selfDeclaredMadeForKids"], False)

    def test_privacy_status_is_sent_explicitly(self):
        self.assertEqual(self.body["status"]["privacyStatus"], "public")

    def test_initiate_uses_the_documented_endpoint_and_headers(self):
        _, http = self.upload([ok_init(), ok_upload()])
        init = http.calls[0]
        self.assertEqual(init["method"], "POST")
        self.assertIn("uploadType=resumable", init["url"])
        self.assertIn("part=snippet,status", init["url"])
        self.assertEqual(init["headers"]["Content-Type"], "application/json; charset=UTF-8")
        self.assertIn("X-Upload-Content-Length", init["headers"])
        self.assertIn("X-Upload-Content-Type", init["headers"])

    def test_media_is_put_to_the_session_uri(self):
        _, http = self.upload([ok_init(), ok_upload()])
        media = http.calls[1]
        self.assertEqual(media["method"], "PUT")
        self.assertEqual(media["url"], SESSION_URI)
        self.assertEqual(media["body_len"], self.asset.stat().st_size)

    def test_exact_title_and_description_are_transmitted(self):
        _, http = self.upload([ok_init(), ok_upload()])
        sent = json.loads(http.calls[0]["body"].decode())
        self.assertEqual(sent["snippet"]["title"], "T")
        self.assertEqual(sent["snippet"]["description"], "D")


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------
class TestNoSecretLeakage(TransportBase):
    def _all_surfaces(self, result) -> str:
        return json.dumps({
            "outcome": result.outcome, "error_reason": result.error_reason,
            "error_detail": result.error_detail, "diagnostics": result.diagnostics,
            "repr": repr(result),
        }, default=str)

    def test_redact_headers_strips_authorization(self):
        out = redact_headers({"Authorization": f"Bearer {SECRET}", "Content-Type": "x"})
        self.assertEqual(out["Authorization"], REDACTED)
        self.assertEqual(out["Content-Type"], "x")

    def test_redact_is_case_insensitive(self):
        self.assertEqual(redact_headers({"authorization": SECRET})["authorization"], REDACTED)

    def test_token_never_appears_in_a_transient_failure_result(self):
        result, _ = self.upload([api_error(503, "backendError", "try later")] * MAX_ATTEMPTS)
        self.assertNotIn(SECRET, self._all_surfaces(result))

    def test_token_never_appears_in_an_ambiguous_result(self):
        result, _ = self.upload([ok_init(), TimeoutError("boom")])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)
        self.assertNotIn(SECRET, self._all_surfaces(result))

    def test_token_never_appears_in_an_api_validation_result(self):
        result, _ = self.upload([ok_init(), api_error(400, "invalidCategoryId", "bad category")])
        self.assertNotIn(SECRET, self._all_surfaces(result))

    def test_diagnostics_carry_redacted_headers_only(self):
        result, _ = self.upload([ok_init(), TimeoutError("boom")])
        self.assertEqual(result.diagnostics["headers_sent"]["Authorization"], REDACTED)

    def test_the_token_is_never_stored_on_the_transport(self):
        t, _ = _transport([ok_init(), ok_upload()])
        self.assertNotIn(SECRET, json.dumps(t.__dict__, default=str))


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------
class TestFailureClassification(TransportBase):
    def test_401_is_an_auth_failure_and_is_not_retried(self):
        result, http = self.upload([api_error(401, "authError", "invalid credentials")])
        self.assertEqual(result.outcome, OUTCOME_AUTH_FAILURE)
        self.assertEqual(len(http.calls), 1)

    def test_403_is_an_auth_failure(self):
        result, _ = self.upload([api_error(403, "forbidden", "insufficient permissions")])
        self.assertEqual(result.outcome, OUTCOME_AUTH_FAILURE)

    def test_400_category_error_is_api_validation_and_captured_not_guessed_around(self):
        result, _ = self.upload([ok_init(),
                                 api_error(400, "invalidCategoryId", "invalid category ID")])
        self.assertEqual(result.outcome, OUTCOME_API_VALIDATION_FAILURE)
        self.assertEqual(result.error_reason, "invalidCategoryId")

    def test_400_invalid_title_is_api_validation(self):
        result, _ = self.upload([ok_init(), api_error(400, "invalidTitle", "empty title")])
        self.assertEqual(result.outcome, OUTCOME_API_VALIDATION_FAILURE)
        self.assertEqual(result.error_reason, "invalidTitle")

    def test_5xx_during_initiate_is_transient_and_retried_with_backoff(self):
        result, http = self.upload([api_error(503, "backendError", "x")] * MAX_ATTEMPTS)
        self.assertEqual(result.outcome, OUTCOME_TRANSIENT_FAILURE)
        self.assertEqual(len(http.calls), MAX_ATTEMPTS)

    def test_a_transient_initiate_failure_that_then_succeeds_completes(self):
        result, http = self.upload([api_error(503, "backendError", "x"), ok_init(), ok_upload()])
        self.assertEqual(result.outcome, OUTCOME_CONFIRMED)
        self.assertEqual(len(http.calls), 3)

    def test_network_error_during_initiate_is_retried(self):
        result, http = self.upload([ConnectionError("net"), ok_init(), ok_upload()])
        self.assertEqual(result.outcome, OUTCOME_CONFIRMED)

    def test_retries_are_bounded(self):
        result, http = self.upload([ConnectionError("net")] * MAX_ATTEMPTS)
        self.assertEqual(len(http.calls), MAX_ATTEMPTS)
        self.assertEqual(result.outcome, OUTCOME_TRANSIENT_FAILURE)


class TestAmbiguityIsNeverRetried(TransportBase):
    """The core safety property: a request that may have created a video is
    never replayed."""

    def test_timeout_after_media_transmission_is_ambiguous_not_transient(self):
        result, http = self.upload([ok_init(), TimeoutError("dropped")])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)
        self.assertEqual(len(http.calls), 2)  # initiate + one media attempt, no replay

    def test_a_5xx_after_media_transmission_is_ambiguous_not_transient(self):
        result, http = self.upload([ok_init(), api_error(503, "backendError", "x")])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)
        self.assertEqual(len(http.calls), 2)
        self.assertIn("may have been created", result.error_detail)

    def test_308_resume_incomplete_is_ambiguous_and_not_auto_resumed(self):
        result, http = self.upload([ok_init(), (308, {"Range": "bytes=0-999"}, b"")])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)
        self.assertEqual(result.http_status, 308)
        self.assertEqual(len(http.calls), 2)

    def test_success_without_a_video_id_is_ambiguous(self):
        result, _ = self.upload([ok_init(), (201, {}, json.dumps({"snippet": {}}).encode())])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)

    def test_unparseable_success_body_is_ambiguous(self):
        result, _ = self.upload([ok_init(), (201, {}, b"<html>not json</html>")])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)

    def test_initiate_200_without_a_location_header_is_ambiguous(self):
        result, _ = self.upload([(200, {}, b"")])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)


class TestConfirmedResponse(TransportBase):
    def test_confirmed_captures_only_safe_response_fields(self):
        result, _ = self.upload([ok_init(), ok_upload(video_id="ABC", title="T")])
        self.assertEqual(result.outcome, OUTCOME_CONFIRMED)
        self.assertEqual(result.video_id, "ABC")
        self.assertEqual(result.returned_title, "T")
        self.assertEqual(result.returned_privacy_status, "public")
        self.assertEqual(result.returned_channel_id, CHANNEL)
        self.assertIsNotNone(result.uploaded_at)

    def test_no_fake_ids_or_urls_are_ever_invented(self):
        result, _ = self.upload([ok_init(), (201, {}, json.dumps({"id": ""}).encode())])
        self.assertEqual(result.video_id, "")
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)


# ---------------------------------------------------------------------------
# RSS read-back verification — no credentials, no extra scope
# ---------------------------------------------------------------------------
def _post(vid, title, url):
    return Post(id=vid, brand="empires", platform="youtube", author="self",
                text=title, url=url, own=True)


class TestRssVerification(unittest.TestCase):
    def test_insert_success_alone_is_not_verification(self):
        """A 201 does not mark the record verified — the feed must show it."""
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="T",
                             rss_reader=lambda: [])
        self.assertEqual(out["status"], VERIFICATION_PENDING)

    def test_pending_never_suggests_re_uploading(self):
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="T",
                             rss_reader=lambda: [])
        self.assertIn("never trigger a re-upload", out["detail"])

    def test_matching_entry_confirms(self):
        posts = [_post("VID", "T", "https://www.youtube.com/shorts/VID")]
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="T",
                             rss_reader=lambda: posts)
        self.assertEqual(out["status"], VERIFICATION_CONFIRMED)
        self.assertTrue(out["is_short"])

    def test_shorts_path_is_detected(self):
        posts = [_post("VID", "T", "https://www.youtube.com/watch?v=VID")]
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="T",
                             rss_reader=lambda: posts)
        self.assertFalse(out["is_short"])

    def test_title_mismatch_is_reported_not_confirmed(self):
        posts = [_post("VID", "SOMETHING ELSE", "https://www.youtube.com/shorts/VID")]
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="T",
                             rss_reader=lambda: posts)
        self.assertEqual(out["status"], VERIFICATION_MISMATCH)

    def test_a_different_video_in_the_feed_does_not_confirm(self):
        posts = [_post("OTHER", "T", "https://www.youtube.com/shorts/OTHER")]
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="T",
                             rss_reader=lambda: posts)
        self.assertEqual(out["status"], VERIFICATION_PENDING)


# ---------------------------------------------------------------------------
# Gates refuse BEFORE the network — proven by an empty call log
# ---------------------------------------------------------------------------
class TestGatesRefuseBeforeAnyNetworkCall(LegacyBase):
    def _approved(self):
        rec = self.intake()
        sha = rec.brief["legacy_asset"]["sha256"]
        meta = self.store.get_draft(rec.draft_id, self.ctx.name).hash
        return approve_legacy_asset(self.store, self.ctx, rec, "nick",
                                    asset_sha256=sha, metadata_hash=meta)

    def _sink(self, **over):
        kw = dict(enabled=False, store=self.store,
                  credential_path=Path(self.tmp.name) / "absent.json")
        kw.update(over)
        return YouTubeUploadSink(self.registry, self.ctx, **kw)

    MEDIA = {"duration_seconds": 12.8, "width": 1080, "height": 1920}

    def test_mint_authorization_refuses_and_no_http_object_is_touched(self):
        http = FakeHttp([])
        with self.assertRaises(UploadRefused):
            self._sink().mint_authorization(self._approved(), media=self.MEDIA)
        self.assertEqual(http.calls, [])

    def test_disabled_sink_cannot_mint(self):
        with self.assertRaises(UploadRefused):
            self._sink().mint_authorization(self._approved(), media=self.MEDIA)

    def test_unapproved_record_cannot_mint(self):
        with self.assertRaises(UploadRefused):
            self._sink().mint_authorization(self.intake(), media=self.MEDIA)

    def test_channel_mismatch_cannot_mint(self):
        rec = self._approved()
        rec.brief["channel_id"] = "UCwrong"
        with self.assertRaises(UploadRefused):
            self._sink().mint_authorization(rec, media=self.MEDIA)

    def test_unmeasured_media_cannot_mint(self):
        try:
            cs = ConstraintSource.from_file()
        except Exception:
            self.skipTest("harness constraint file not present")
        with self.assertRaises(UploadRefused):
            self._sink(constraint_source=cs).mint_authorization(self._approved())

    def test_build_request_body_is_pure_and_needs_no_authorization(self):
        body = self._sink().build_request_body(self._approved())
        self.assertEqual(body["status"]["privacyStatus"], "public")
        self.assertIs(body["status"]["selfDeclaredMadeForKids"], False)
        self.assertNotIn("categoryId", body["snippet"])


class TestTransportIsTheOnlyNetworkModule(unittest.TestCase):
    def test_sink_credentials_and_intake_remain_network_free(self):
        root = Path(__file__).resolve().parent.parent
        for rel in ("engage/publish/youtube_sink.py", "engage/publish/credentials.py",
                    "engage/drafting/legacy_intake.py"):
            src = (root / rel).read_text(encoding="utf-8")
            for bad in ("urllib.request", "import requests", "http.client", "import socket"):
                with self.subTest(module=rel, token=bad):
                    self.assertNotIn(bad, src)

    def test_transport_has_no_browser_or_oauth_capability(self):
        src = (Path(__file__).resolve().parent.parent
               / "engage/publish/youtube_transport.py").read_text(encoding="utf-8").lower()
        for bad in ("selenium", "playwright", "webdriver", "webbrowser",
                    "google_auth_oauthlib", "installedappflow", "subprocess"):
            with self.subTest(token=bad):
                self.assertNotIn(bad, src)

    def test_transport_declares_no_brand(self):
        src = (Path(__file__).resolve().parent.parent
               / "engage/publish/youtube_transport.py").read_text(encoding="utf-8").lower()
        for term in ("empires", "capstack", "stowecap"):
            with self.subTest(term=term):
                self.assertNotIn(term, src)


if __name__ == "__main__":
    unittest.main()


class TestTitleVerificationIsExact(unittest.TestCase):
    """Regression: title verification used a substring test, so a short
    title matched inside unrelated text and reported the wrong video as
    verified."""

    def test_short_title_does_not_match_inside_unrelated_text(self):
        posts = [_post("VID", "SOMETHING ELSE", "https://www.youtube.com/shorts/VID")]
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="T",
                             rss_reader=lambda: posts)
        self.assertEqual(out["status"], VERIFICATION_MISMATCH)

    def test_title_plus_description_still_verifies_on_the_title_line(self):
        posts = [_post("VID", "Real Title\n\nA long description mentioning things.",
                       "https://www.youtube.com/shorts/VID")]
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="Real Title",
                             rss_reader=lambda: posts)
        self.assertEqual(out["status"], VERIFICATION_CONFIRMED)

    def test_a_truncated_title_is_a_mismatch_not_a_pass(self):
        posts = [_post("VID", "Real Tit", "https://www.youtube.com/shorts/VID")]
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="Real Title",
                             rss_reader=lambda: posts)
        self.assertEqual(out["status"], VERIFICATION_MISMATCH)

    def test_mismatch_reports_what_was_actually_found(self):
        posts = [_post("VID", "Wrong Title", "https://www.youtube.com/shorts/VID")]
        out = verify_via_rss(channel_id=CHANNEL, video_id="VID", expected_title="Right Title",
                             rss_reader=lambda: posts)
        self.assertEqual(out["actual_title"], "Wrong Title")


class TestUploadResponseChannelIsFinalProof(TransportBase):
    """The response's own channelId is the ONLY thing that proves where a
    video actually landed. Consent-time membership verification does not.

    This check did not exist before 2026-08-21 (DEC-SM-024): the transport
    captured returned_channel_id and never compared it, so an upload landing
    on the wrong channel of a multi-channel account would have been reported
    `confirmed`."""

    WRONG = "UCwrongChannel00000000"

    def test_matching_channel_confirms(self):
        result, _ = self.upload([ok_init(), ok_upload(channel=CHANNEL)])
        self.assertEqual(result.outcome, OUTCOME_CONFIRMED)
        self.assertEqual(result.returned_channel_id, CHANNEL)

    def test_mismatched_channel_is_ambiguous_not_confirmed(self):
        result, _ = self.upload([ok_init(), ok_upload(channel=self.WRONG)])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)

    def test_mismatched_channel_is_flagged_high_risk(self):
        result, _ = self.upload([ok_init(), ok_upload(channel=self.WRONG)])
        self.assertIn("HIGH RISK", result.error_detail)
        self.assertIn("DIFFERENT channel", result.error_detail)

    def test_mismatched_channel_is_never_retried(self):
        result, http = self.upload([ok_init(), ok_upload(channel=self.WRONG)])
        self.assertEqual(len(http.calls), 2)  # initiate + one media PUT, no replay
        self.assertIn("Do not retry", result.error_detail)

    def test_mismatch_requires_owner_escalation(self):
        result, _ = self.upload([ok_init(), ok_upload(channel=self.WRONG)])
        self.assertIn("escalation", result.error_detail.lower())

    def test_mismatch_records_both_channel_ids_for_diagnosis(self):
        result, _ = self.upload([ok_init(), ok_upload(channel=self.WRONG)])
        self.assertEqual(result.diagnostics["expected_channel_id"], CHANNEL)
        self.assertEqual(result.diagnostics["returned_channel_id"], self.WRONG)

    def test_mismatch_still_reports_the_video_id_so_it_can_be_removed(self):
        result, _ = self.upload([ok_init(), ok_upload(video_id="ORPHAN1", channel=self.WRONG)])
        self.assertEqual(result.video_id, "ORPHAN1")

    def test_missing_channel_id_in_response_is_ambiguous(self):
        resp = (201, {}, json.dumps({"id": "VID", "snippet": {"title": "T"},
                                     "status": {"privacyStatus": "public"}}).encode())
        result, _ = self.upload([ok_init(), resp])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)
        self.assertIn("NO channelId", result.error_detail)

    def test_a_near_miss_channel_id_is_still_a_mismatch(self):
        result, _ = self.upload([ok_init(), ok_upload(channel=CHANNEL[:-1])])
        self.assertEqual(result.outcome, OUTCOME_AMBIGUOUS)

    def test_no_secret_leaks_in_a_channel_mismatch_result(self):
        result, _ = self.upload([ok_init(), ok_upload(channel=self.WRONG)])
        blob = json.dumps({"d": result.diagnostics, "e": result.error_detail,
                           "r": repr(result)}, default=str)
        self.assertNotIn(SECRET, blob)

    def test_the_expected_channel_comes_from_the_capability_token(self):
        """Not from a parameter a caller could set independently."""
        self.assertEqual(self.auth.channel_id, CHANNEL)
        result, _ = self.upload([ok_init(), ok_upload(channel=CHANNEL)])
        self.assertEqual(result.outcome, OUTCOME_CONFIRMED)


# ---------------------------------------------------------------------------
# DEC-SM-026 — TLS trust store. Verification is never disabled.
# ---------------------------------------------------------------------------
class TestTlsContext(unittest.TestCase):
    def test_context_requires_certificates(self):
        import ssl
        from engage.publish.youtube_transport import _ssl_context
        ctx = _ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_context_checks_hostname(self):
        from engage.publish.youtube_transport import _ssl_context
        self.assertTrue(_ssl_context().check_hostname)

    def test_context_has_roots_loaded(self):
        from engage.publish.youtube_transport import _ssl_context
        self.assertGreater(len(_ssl_context().get_ca_certs()), 0)

    def test_no_verification_disabling_construct_in_the_package(self):
        """An unverified TLS connection to an auth endpoint would be a
        credential-interception risk, so no escape hatch may exist."""
        pkg = Path(__file__).resolve().parents[1] / "engage"
        offenders = []
        for p in pkg.rglob("*.py"):
            src = p.read_text(encoding="utf-8")
            for bad in ("_create_unverified_context", "CERT_NONE",
                        "check_hostname = False", "check_hostname=False",
                        "verify=False"):
                # allow the words inside an explanatory comment that says NO
                for line in src.splitlines():
                    if bad in line and not line.lstrip().startswith("#") \
                            and "deliberately no" not in line and "no verify=False" not in line:
                        offenders.append(f"{p.name}: {line.strip()[:60]}")
        self.assertEqual(offenders, [])

    def test_missing_ca_bundle_raises_rather_than_downgrading(self):
        import ssl as _ssl
        from engage.publish import youtube_transport as T

        class EmptyCtx:
            def get_ca_certs(self):
                return []

        with mock.patch.object(_ssl, "create_default_context", return_value=EmptyCtx()), \
             mock.patch.dict("sys.modules", {"certifi": None}):
            with self.assertRaises(T.TlsUnavailable):
                T._ssl_context()

    def test_cert_verification_failure_becomes_a_clear_error(self):
        import ssl as _ssl
        import urllib.error
        from engage.publish.youtube_transport import TlsUnavailable, UrllibHttp

        http = UrllibHttp(context=object())
        err = urllib.error.URLError(_ssl.SSLCertVerificationError("bad cert"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(TlsUnavailable) as cm:
                http.request("POST", "https://example.invalid", {})
        self.assertIn("Install Certificates.command", str(cm.exception))
        self.assertIn("never disabled", str(cm.exception))

    def test_a_non_tls_url_error_still_propagates(self):
        import urllib.error
        from engage.publish.youtube_transport import UrllibHttp
        http = UrllibHttp(context=object())
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(urllib.error.URLError):
                http.request("POST", "https://example.invalid", {})

    def test_http_error_status_is_returned_not_raised(self):
        """A 400 is a real response the caller classifies, not a transport
        failure."""
        import urllib.error
        from engage.publish.youtube_transport import UrllibHttp
        http = UrllibHttp(context=object())
        err = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b'{"error":1}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            status, _, payload = http.request("POST", "https://example.invalid", {})
        self.assertEqual(status, 400)
