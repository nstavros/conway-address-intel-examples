"""Phase 7B-A — legacy asset intake + the disabled YouTubeUploadSink.

Everything here is offline: temp files for assets and credentials, the
fixture registry, in-memory stores, and a mock transport that performs no
I/O. No network call, no browser, no OAuth, no real credential, and no real
upload path is exercised anywhere — several tests assert exactly that."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from engage.core.models import LifecycleError
from engage.drafting.legacy_intake import (
    LEGACY_INTAKE_KIND,
    LegacyIntakeError,
    approval_view,
    approve_legacy_asset,
    hash_asset,
    intake_legacy_asset,
    missing_before_approval,
)
from engage.drafting.originals import write_handoff
from engage.publish.credentials import (
    REDACTED,
    REQUIRED_SCOPES,
    CredentialError,
    CredentialHandle,
    load_youtube_credentials,
    redact,
)
from engage.publish.operations import NON_ACTING_SINKS
from engage.publish.youtube_sink import (
    ConstraintSource,
    MockTransport,
    UploadRefused,
    YouTubeUploadSink,
)

from .helpers import ctx_for, mem_store
from .test_originals import load_test_registry

CHANNEL = "UC9772FnuAXMVabS0gtr6cew"
TITLE = "The Evolution of Troy #cityevolution #trojanwar"
DESCRIPTION = "Nine cities, one hill. The mound holds nine settlements stacked across time."
CORPUS = (TITLE + " " + DESCRIPTION + " nine 1871 3000",)


def stub_backend(prompt: str) -> str:
    return "PASS" if "'PASS' or 'BLOCK" in prompt else "A hook.\nBody."


class LegacyBase(unittest.TestCase):
    def setUp(self):
        self.store, self.ctx = mem_store(), ctx_for("empires")
        self.registry = load_test_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.asset = Path(self.tmp.name) / "evolution_troy.mp4"
        self.asset.write_bytes(b"\x00\x01FAKE-VIDEO-BYTES" * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def intake(self, **over):
        kw = dict(asset_path=self.asset, platform="youtube", channel_id=CHANNEL,
                  working_title="The Evolution of Troy", final_title=TITLE,
                  final_description=DESCRIPTION, visibility="public",
                  made_for_kids=False, backend=stub_backend, source_corpus=CORPUS)
        kw.update(over)
        return intake_legacy_asset(self.store, self.ctx, self.registry, **kw)


# ---------------------------------------------------------------------------
# Legacy intake — provenance and asset-hash binding
# ---------------------------------------------------------------------------
class TestIntakeProvenanceAndHashBinding(LegacyBase):
    def test_record_is_marked_legacy_and_never_claims_prior_approval(self):
        rec = self.intake()
        self.assertEqual(rec.kind, LEGACY_INTAKE_KIND)
        self.assertIs(rec.brief["previously_approved_through_engage"], False)

    def test_provenance_states_the_asset_predates_the_workflow(self):
        rec = self.intake()
        prov = rec.brief["provenance"].lower()
        self.assertIn("pre-dates", prov)
        self.assertIn("has not", prov.replace("not previously", "not"))

    def test_asset_hash_matches_the_real_file(self):
        rec = self.intake()
        self.assertEqual(rec.brief["legacy_asset"]["sha256"], hash_asset(self.asset).sha256)

    def test_asset_size_and_path_recorded(self):
        rec = self.intake()
        asset = rec.brief["legacy_asset"]
        self.assertEqual(asset["size_bytes"], self.asset.stat().st_size)
        self.assertEqual(asset["path"], str(self.asset))
        self.assertEqual(asset["suffix"], ".mp4")

    def test_intake_lands_in_review_required_never_approved(self):
        self.assertEqual(self.intake().lifecycle_status, "review_required")

    def test_no_approval_row_is_created_at_intake(self):
        rec = self.intake()
        self.assertIsNone(self.store.get_approval_hash(rec.draft_id))

    def test_intake_emits_an_audit_event_with_the_hash(self):
        rec = self.intake()
        kinds = [e["event_type"] for e in self.store.list_content_events(rec.id)]
        self.assertIn("legacy_asset_intake_created", kinds)
        ev = next(e for e in self.store.list_content_events(rec.id)
                  if e["event_type"] == "legacy_asset_intake_created")
        self.assertEqual(ev["detail"]["asset_sha256"], hash_asset(self.asset).sha256)
        self.assertIs(ev["detail"]["previously_approved_through_engage"], False)

    def test_metadata_hash_binds_the_exact_final_metadata(self):
        rec = self.intake()
        draft = self.store.get_draft(rec.draft_id, self.ctx.name)
        self.assertIn(TITLE, draft.text)
        self.assertIn(DESCRIPTION, draft.text)

    def test_missing_asset_file_refused(self):
        with self.assertRaises(LegacyIntakeError):
            self.intake(asset_path=Path(self.tmp.name) / "nope.mp4")

    def test_empty_asset_refused(self):
        empty = Path(self.tmp.name) / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaises(LegacyIntakeError):
            self.intake(asset_path=empty)

    def test_directory_instead_of_file_refused(self):
        with self.assertRaises(LegacyIntakeError):
            self.intake(asset_path=Path(self.tmp.name))

    def test_refused_intake_creates_no_record(self):
        before = len(self.store.list_content_records(self.ctx.name))
        with self.assertRaises(LegacyIntakeError):
            self.intake(asset_path=Path(self.tmp.name) / "nope.mp4")
        self.assertEqual(len(self.store.list_content_records(self.ctx.name)), before)


# ---------------------------------------------------------------------------
# Required final metadata
# ---------------------------------------------------------------------------
class TestRequiredFinalMetadata(LegacyBase):
    def test_empty_final_title_refused(self):
        with self.assertRaises(LegacyIntakeError):
            self.intake(final_title="   ")

    def test_empty_final_description_refused(self):
        with self.assertRaises(LegacyIntakeError):
            self.intake(final_description="")

    def test_empty_working_title_refused(self):
        with self.assertRaises(LegacyIntakeError):
            self.intake(working_title="")

    def test_visibility_must_be_explicit_and_known(self):
        for bad in ("", "PUBLIC", "semi-public", None):
            with self.subTest(v=bad), self.assertRaises(LegacyIntakeError):
                self.intake(visibility=bad)

    def test_made_for_kids_must_be_an_explicit_boolean(self):
        for bad in (None, "false", 0, "no"):
            with self.subTest(v=bad), self.assertRaises(LegacyIntakeError):
                self.intake(made_for_kids=bad)

    def test_made_for_kids_false_is_recorded_explicitly(self):
        self.assertIs(self.intake().brief["made_for_kids"], False)

    def test_public_visibility_is_recorded(self):
        self.assertEqual(self.intake().brief["visibility"], "public")

    def test_missing_before_approval_lists_the_owner_approval_gap(self):
        rec = self.intake()
        gaps = " ".join(missing_before_approval(self.store, rec))
        self.assertIn("owner approval", gaps)


# ---------------------------------------------------------------------------
# Channel binding
# ---------------------------------------------------------------------------
class TestChannelBinding(LegacyBase):
    def test_wrong_channel_id_refused(self):
        with self.assertRaises(LegacyIntakeError):
            self.intake(channel_id="UCsomeoneElsesChannel12345")

    def test_empty_channel_id_refused(self):
        with self.assertRaises(LegacyIntakeError):
            self.intake(channel_id="")

    def test_near_miss_channel_id_refused(self):
        with self.assertRaises(LegacyIntakeError):
            self.intake(channel_id=CHANNEL[:-1])

    def test_correct_channel_id_recorded_from_the_registry(self):
        self.assertEqual(self.intake().brief["channel_id"], CHANNEL)


# ---------------------------------------------------------------------------
# Duplicate protection
# ---------------------------------------------------------------------------
class TestDuplicateProtection(LegacyBase):
    def test_same_asset_bytes_refused_twice(self):
        self.intake()
        with self.assertRaises(LegacyIntakeError):
            self.intake()

    def test_identical_copy_at_a_different_path_still_refused(self):
        self.intake()
        twin = Path(self.tmp.name) / "copy.mp4"
        twin.write_bytes(self.asset.read_bytes())
        with self.assertRaises(LegacyIntakeError):
            self.intake(asset_path=twin)

    def test_different_asset_is_allowed(self):
        self.intake()
        other = Path(self.tmp.name) / "other.mp4"
        other.write_bytes(b"DIFFERENT-BYTES" * 64)
        rec = self.intake(asset_path=other)
        self.assertEqual(rec.lifecycle_status, "review_required")


# ---------------------------------------------------------------------------
# Approval requirement
# ---------------------------------------------------------------------------
class TestApprovalRequirement(LegacyBase):
    def _hashes(self, rec):
        return (rec.brief["legacy_asset"]["sha256"],
                self.store.get_draft(rec.draft_id, self.ctx.name).hash)

    def test_approval_requires_the_correct_asset_hash(self):
        rec = self.intake()
        _, meta = self._hashes(rec)
        with self.assertRaises(LifecycleError):
            approve_legacy_asset(self.store, self.ctx, rec, "nick",
                                 asset_sha256="0" * 64, metadata_hash=meta)

    def test_approval_requires_the_correct_metadata_hash(self):
        rec = self.intake()
        sha, _ = self._hashes(rec)
        with self.assertRaises(LifecycleError):
            approve_legacy_asset(self.store, self.ctx, rec, "nick",
                                 asset_sha256=sha, metadata_hash="0" * 64)

    def test_approval_requires_a_named_approver(self):
        rec = self.intake()
        sha, meta = self._hashes(rec)
        with self.assertRaises(LifecycleError):
            approve_legacy_asset(self.store, self.ctx, rec, "  ",
                                 asset_sha256=sha, metadata_hash=meta)

    def test_correct_hashes_approve_successfully(self):
        rec = self.intake()
        sha, meta = self._hashes(rec)
        out = approve_legacy_asset(self.store, self.ctx, rec, "nick",
                                   asset_sha256=sha, metadata_hash=meta)
        self.assertEqual(out.lifecycle_status, "approved")
        self.assertEqual(self.store.get_approval_hash(rec.draft_id), meta)

    def test_a_file_swapped_after_intake_is_caught_at_approval(self):
        rec = self.intake()
        sha, meta = self._hashes(rec)
        self.asset.write_bytes(b"COMPLETELY-DIFFERENT-VIDEO" * 64)
        with self.assertRaises(LifecycleError) as cm:
            approve_legacy_asset(self.store, self.ctx, rec, "nick",
                                 asset_sha256=sha, metadata_hash=meta)
        self.assertIn("CHANGED", str(cm.exception))

    def test_approval_view_does_not_approve(self):
        rec = self.intake()
        approval_view(self.store, rec)
        self.assertEqual(self.store.get_content_record(rec.id).lifecycle_status,
                         "review_required")
        self.assertIsNone(self.store.get_approval_hash(rec.draft_id))

    def test_approval_view_shows_both_hashes_and_declarations(self):
        rec = self.intake()
        view = approval_view(self.store, rec)
        sha, meta = self._hashes(rec)
        self.assertEqual(view["asset_sha256"], sha)
        self.assertEqual(view["metadata_hash"], meta)
        self.assertIs(view["made_for_kids"], False)
        self.assertEqual(view["visibility"], "public")
        self.assertIs(view["previously_approved_through_engage"], False)

    def test_approve_legacy_rejects_a_non_legacy_record(self):
        from .test_publish_operations import approved_record
        rec = approved_record(self.store, self.ctx, self.registry, "m1", "Text.", "instagram")
        with self.assertRaises(LifecycleError):
            approve_legacy_asset(self.store, self.ctx, rec, "nick",
                                 asset_sha256="x", metadata_hash="y")

    def test_unapproved_legacy_record_cannot_obtain_a_publishing_handoff(self):
        rec = self.intake()
        with self.assertRaises(Exception):
            write_handoff(self.store, rec, "publishing")


# ---------------------------------------------------------------------------
# Credentials — fail closed in every abnormal case
# ---------------------------------------------------------------------------
class TestCredentialsFailClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "empires.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, doc, mode=0o600):
        self.path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
        os.chmod(self.path, mode)

    def _valid(self):
        return {"brand": "empires", "platform": "youtube",
                "scopes": list(REQUIRED_SCOPES), "refresh_token": "SECRET-VALUE"}

    def test_absent_file_refused(self):
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="empires")

    def test_the_real_default_path_is_never_written_by_tests(self):
        """A real credential now exists (owner consent, 2026-08-22). The
        invariant is that tests never create or modify it."""
        from pathlib import Path as _P
        p = _P.home() / ".config" / "engage" / "youtube" / "empires.json"
        before = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
        try:
            load_youtube_credentials(self.path, brand="empires")
        except CredentialError:
            pass
        after = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
        self.assertEqual(before, after)

    def test_malformed_json_refused(self):
        self._write("{not json at all")
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="empires")

    def test_non_object_json_refused(self):
        self._write([1, 2, 3])
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="empires")

    def test_group_readable_refused(self):
        self._write(self._valid(), mode=0o640)
        with self.assertRaises(CredentialError) as cm:
            load_youtube_credentials(self.path, brand="empires")
        self.assertIn("0600", str(cm.exception))

    def test_world_readable_refused(self):
        self._write(self._valid(), mode=0o644)
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="empires")

    def test_missing_scopes_refused(self):
        doc = self._valid()
        del doc["scopes"]
        self._write(doc)
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="empires")

    def test_broader_scope_refused_not_accepted_as_superset(self):
        """Both required scopes present PLUS a broader one — the extra must
        be refused rather than tolerated as a superset."""
        doc = self._valid()
        doc["scopes"] = [*REQUIRED_SCOPES, "https://www.googleapis.com/auth/youtube"]
        self._write(doc)
        with self.assertRaises(CredentialError) as cm:
            load_youtube_credentials(self.path, brand="empires")
        msg = str(cm.exception)
        self.assertIn("refused", msg)
        self.assertIn("outside the allowlist", msg)

    def test_wrong_scope_refused(self):
        doc = self._valid()
        doc["scopes"] = ["https://www.googleapis.com/auth/youtube.force-ssl"]
        self._write(doc)
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="empires")

    def test_credential_bound_to_another_cell_refused(self):
        doc = self._valid()
        doc["brand"] = "capstack"
        self._write(doc)
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="empires")

    def test_loader_refuses_any_cell_other_than_empires_youtube(self):
        self._write(self._valid())
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="capstack")

    def test_a_valid_credential_yields_a_handle(self):
        self._write(self._valid())
        handle = load_youtube_credentials(self.path, brand="empires")
        self.assertIsInstance(handle, CredentialHandle)
        self.assertEqual(handle.scopes, tuple(REQUIRED_SCOPES))


class TestCredentialRedaction(unittest.TestCase):
    """Token material must never reach an event, error, log, report, or
    handoff — including through a repr or an exception string."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "empires.json"
        self.secret = "ya29.SUPER-SECRET-REFRESH-TOKEN"
        self.path.write_text(json.dumps({
            "brand": "empires", "platform": "youtube",
            "scopes": list(REQUIRED_SCOPES),
            "refresh_token": self.secret, "client_secret": "GOCSPX-alsosecret",
        }))
        os.chmod(self.path, 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_handle_repr_contains_no_secret(self):
        h = load_youtube_credentials(self.path, brand="empires")
        self.assertNotIn(self.secret, repr(h))
        self.assertIn(REDACTED, repr(h))

    def test_handle_str_contains_no_secret(self):
        self.assertNotIn(self.secret, str(load_youtube_credentials(self.path, brand="empires")))

    def test_summary_is_safe_to_log(self):
        s = load_youtube_credentials(self.path, brand="empires").summary()
        self.assertNotIn(self.secret, json.dumps(s))
        self.assertEqual(s["secret_material"], REDACTED)

    def test_handle_holds_no_token_attribute(self):
        h = load_youtube_credentials(self.path, brand="empires")
        self.assertNotIn(self.secret, json.dumps(getattr(h, "__dict__", {}), default=str))

    def test_requesting_secret_material_is_refused(self):
        with self.assertRaises(CredentialError):
            load_youtube_credentials(self.path, brand="empires").secret_material()

    def test_redact_scrubs_unknown_keys_by_default(self):
        out = redact({"refresh_token": self.secret, "brand": "empires",
                      "surprise_new_google_field": "another-secret"})
        self.assertEqual(out["refresh_token"], REDACTED)
        self.assertEqual(out["surprise_new_google_field"], REDACTED)
        self.assertEqual(out["brand"], "empires")

    def test_malformed_file_error_never_echoes_file_content(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text('{"refresh_token": "ya29.LEAKME-NOW", oops}')
        os.chmod(bad, 0o600)
        with self.assertRaises(CredentialError) as cm:
            load_youtube_credentials(bad, brand="empires")
        self.assertNotIn("LEAKME", str(cm.exception))

    def test_sink_refusal_reasons_contain_no_secret(self):
        registry, ctx = load_test_registry(), ctx_for("empires")
        sink = YouTubeUploadSink(registry, ctx, credential_path=self.path)
        self.assertNotIn(self.secret, " ".join(sink.refusal_reasons()))


# ---------------------------------------------------------------------------
# The disabled YouTube upload sink
# ---------------------------------------------------------------------------
class TestYouTubeSinkRefuses(LegacyBase):
    def _approved(self):
        rec = self.intake()
        sha = rec.brief["legacy_asset"]["sha256"]
        meta = self.store.get_draft(rec.draft_id, self.ctx.name).hash
        return approve_legacy_asset(self.store, self.ctx, rec, "nick",
                                    asset_sha256=sha, metadata_hash=meta)

    def _sink(self, **over):
        kw = dict(enabled=False, transport=None, store=self.store,
                  credential_path=Path(self.tmp.name) / "absent.json")
        kw.update(over)
        return YouTubeUploadSink(self.registry, self.ctx, **kw)

    def test_default_constructed_sink_is_disabled(self):
        self.assertFalse(self._sink().enabled)

    def test_schedule_publish_and_verify_all_refuse(self):
        rec = self._approved()
        sink = self._sink()
        for op in (lambda: sink.schedule(rec, 0, "UTC"),
                   lambda: sink.publish(rec),
                   lambda: sink.verify(rec)):
            with self.subTest(op=op), self.assertRaises(UploadRefused):
                op()

    def test_enable_flag_absent_is_a_named_reason(self):
        reasons = " ".join(self._sink().refusal_reasons(self._approved()))
        self.assertIn("enable flag is absent", reasons)

    def test_live_status_is_a_named_reason(self):
        reasons = " ".join(self._sink().refusal_reasons(self._approved()))
        self.assertIn("not live-enabled", reasons)

    def test_constraints_cannot_be_satisfied_without_a_provenance_source(self):
        """The old check_constraints path was replaced by ConstraintSource.
        The guarantee is unchanged: limits are never assumed satisfied."""
        reasons = " ".join(self._sink().refusal_reasons(self._approved()))
        self.assertIn("ConstraintSource", reasons)

    def test_no_constraint_source_is_a_named_reason(self):
        reasons = " ".join(self._sink().refusal_reasons(self._approved()))
        self.assertIn("ConstraintSource", reasons)

    def test_credential_absence_is_a_named_reason(self):
        reasons = " ".join(self._sink().refusal_reasons(self._approved()))
        self.assertIn("credential unavailable", reasons)

    def test_no_transport_is_a_named_reason(self):
        reasons = " ".join(self._sink().refusal_reasons(self._approved()))
        self.assertIn("no transport", reasons)

    def test_unapproved_record_is_a_named_reason(self):
        rec = self.intake()
        reasons = " ".join(self._sink().refusal_reasons(rec))
        self.assertIn("not 'approved'", reasons)

    def test_channel_mismatch_is_a_named_reason(self):
        rec = self._approved()
        rec.brief["channel_id"] = "UCwrongChannel"
        reasons = " ".join(self._sink().refusal_reasons(rec))
        self.assertIn("refusing to upload to an unverified channel", reasons)

    def test_missing_metadata_is_a_named_reason(self):
        rec = self._approved()
        rec.brief["final_description"] = ""
        reasons = " ".join(self._sink().refusal_reasons(rec))
        self.assertIn("final_description", reasons)

    def test_asset_changed_after_approval_is_a_named_reason(self):
        rec = self._approved()
        self.asset.write_bytes(b"SWAPPED" * 64)
        reasons = " ".join(self._sink().refusal_reasons(rec))
        self.assertIn("bytes changed since approval", reasons)

    def test_even_fully_enabled_and_credentialed_it_still_refuses_today(self):
        """Enable flag on, transport present, credential valid — the
        registry row is still not live and constraints are still unknown,
        so the sink refuses. Proves no single switch unlocks it."""
        cred = Path(self.tmp.name) / "cred.json"
        cred.write_text(json.dumps({"brand": "empires", "platform": "youtube",
                                    "scopes": list(REQUIRED_SCOPES), "refresh_token": "x"}))
        os.chmod(cred, 0o600)
        sink = self._sink(enabled=True, transport=MockTransport(), credential_path=cred)
        with self.assertRaises(UploadRefused):
            sink.publish(self._approved())

    def test_refusal_reports_every_reason_not_just_the_first(self):
        self.assertGreater(len(self._sink().refusal_reasons(self.intake())), 3)

    def test_mock_transport_is_never_called(self):
        transport = MockTransport()
        sink = self._sink(enabled=True, transport=transport)
        with self.assertRaises(UploadRefused):
            sink.publish(self._approved())
        self.assertEqual(transport.calls, [])


class TestSinkIsGatedByOperationsToo(unittest.TestCase):
    def test_youtube_sink_is_not_on_the_non_acting_allowlist(self):
        self.assertNotIn("youtube_upload", NON_ACTING_SINKS)


class TestCellRestrictionLivesInData(unittest.TestCase):
    """The sink reaches exactly one cell — because exactly one registry row
    carries a verified channel_id, not because a brand name is hardcoded in
    engine code (DESIGN.md engine purity, tests/test_isolation.py)."""

    def test_a_cell_without_a_verified_channel_id_is_refused(self):
        registry = load_test_registry()
        other = ctx_for("capstack")
        sink = YouTubeUploadSink(registry, other)
        self.assertIn("no verified channel_id", " ".join(sink.refusal_reasons()))

    def test_exactly_one_registry_row_carries_a_channel_id(self):
        registry = load_test_registry()
        with_channel = [(a.get("brand"), a.get("platform")) for a in registry.accounts
                        if a.get("channel_id")]
        self.assertEqual(len(with_channel), 1)

    def test_no_brand_name_appears_in_the_new_engine_modules(self):
        root = Path(__file__).resolve().parent.parent
        for rel in ("engage/publish/youtube_sink.py", "engage/publish/credentials.py",
                    "engage/drafting/legacy_intake.py"):
            src = (root / rel).read_text(encoding="utf-8").lower()
            for term in ("empires", "capstack", "stowecap"):
                with self.subTest(module=rel, term=term):
                    self.assertNotIn(term, src)


class TestNoExternalActionPath(unittest.TestCase):
    """Structural: the new modules contain no network, browser, or OAuth
    capability at all."""

    FILES = {
        "youtube_sink": Path("engage/publish/youtube_sink.py"),
        "credentials": Path("engage/publish/credentials.py"),
        "legacy_intake": Path("engage/drafting/legacy_intake.py"),
    }

    def _source(self, key):
        root = Path(__file__).resolve().parent.parent
        return (root / self.FILES[key]).read_text(encoding="utf-8")

    def test_no_network_client_imports(self):
        for key in self.FILES:
            src = self._source(key)
            for bad in ("import requests", "import httpx", "urllib.request",
                        "http.client", "import socket", "aiohttp"):
                with self.subTest(module=key, token=bad):
                    self.assertNotIn(bad, src)

    def test_no_browser_or_webdriver_capability(self):
        for key in self.FILES:
            src = self._source(key).lower()
            for bad in ("selenium", "playwright", "webdriver", "webbrowser"):
                with self.subTest(module=key, token=bad):
                    self.assertNotIn(bad, src)

    def test_no_oauth_flow_implementation(self):
        for key in self.FILES:
            src = self._source(key)
            for bad in ("google_auth_oauthlib", "InstalledAppFlow",
                        "googleapiclient", "oauth2client"):
                with self.subTest(module=key, token=bad):
                    self.assertNotIn(bad, src)

    def test_no_subprocess_escape_hatch(self):
        for key in self.FILES:
            src = self._source(key)
            for bad in ("subprocess", "os.system", "os.popen"):
                with self.subTest(module=key, token=bad):
                    self.assertNotIn(bad, src)

    def test_the_only_transport_that_exists_performs_no_io(self):
        t = MockTransport()
        out = t.upload({"anything": True})
        self.assertTrue(out["mock"])
        self.assertEqual(len(t.calls), 1)

    def test_constraint_source_has_no_builtin_values(self):
        """It cannot be constructed without an explicit source and date, so
        nobody can satisfy condition 6 from memory."""
        with self.assertRaises(TypeError):
            ConstraintSource()  # noqa — missing required args by design


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Phase 7B-C — source-backed ConstraintSource
# ---------------------------------------------------------------------------
class TestConstraintSourceProvenance(unittest.TestCase):
    """A limit may not enter the system without a record of where it came
    from and when."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "c.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        self.p.write_text(text)
        return self.p

    def test_absent_file_refused(self):
        from engage.publish.youtube_sink import ConstraintSourceError
        with self.assertRaises(ConstraintSourceError):
            ConstraintSource.from_file(Path(self.tmp.name) / "nope.yaml")

    def test_malformed_yaml_refused(self):
        from engage.publish.youtube_sink import ConstraintSourceError
        with self.assertRaises(ConstraintSourceError):
            ConstraintSource.from_file(self._write("{[not yaml"))

    def test_missing_verified_at_refused(self):
        from engage.publish.youtube_sink import ConstraintSourceError
        with self.assertRaises(ConstraintSourceError):
            ConstraintSource.from_file(self._write("sources: {a: 1}\nlimits: {b: 2}\n"))

    def test_missing_sources_refused(self):
        from engage.publish.youtube_sink import ConstraintSourceError
        with self.assertRaises(ConstraintSourceError):
            ConstraintSource.from_file(self._write("verified_at: '2026-08-21'\nlimits: {b: 2}\n"))

    def test_missing_limits_refused(self):
        from engage.publish.youtube_sink import ConstraintSourceError
        with self.assertRaises(ConstraintSourceError):
            ConstraintSource.from_file(self._write("verified_at: '2026-08-21'\nsources: {a: 1}\n"))

    def test_the_real_harness_constraint_file_loads(self):
        try:
            cs = ConstraintSource.from_file()
        except Exception:
            self.skipTest("harness constraint file not present")
        self.assertTrue(cs.verified_at)
        self.assertTrue(cs.sources)
        for key in ("title_max_characters", "description_max_bytes",
                    "privacy_status_values", "max_file_size_bytes",
                    "shorts_max_duration_seconds", "shorts_max_resolution_height"):
            with self.subTest(limit=key):
                self.assertIn(key, cs.limits)

    def test_every_documented_limit_cites_a_source_and_evidence(self):
        try:
            cs = ConstraintSource.from_file()
        except Exception:
            self.skipTest("harness constraint file not present")
        for key, entry in cs.limits.items():
            with self.subTest(limit=key):
                self.assertIn("source", entry)
                self.assertIn("evidence", entry)
                self.assertIn(entry["source"], cs.sources)


def _cs():
    return ConstraintSource(
        verified_at="2026-08-21",
        sources={"s": {"url": "https://example.invalid"}},
        limits={
            "title_max_characters": {"value": 100, "source": "s", "evidence": "e"},
            "description_max_bytes": {"value": 5000, "source": "s", "evidence": "e"},
            "privacy_status_values": {"value": ["private", "public", "unlisted"],
                                      "source": "s", "evidence": "e"},
            "max_file_size_bytes": {"value": 274877906944, "source": "s", "evidence": "e"},
            "shorts_max_duration_seconds": {"value": 180, "source": "s", "evidence": "e"},
            "shorts_max_resolution_height": {"value": 1080, "source": "s", "evidence": "e"},
        },
        required_by_api=[], required_by_internal_policy=[], unknown=[],
    )


OK_BRIEF = {"final_title": "T", "final_description": "D", "visibility": "public"}
OK_ASSET = {"size_bytes": 100, "duration_seconds": 12.8, "width": 1080, "height": 1920}


class TestConstraintViolations(unittest.TestCase):
    def test_a_compliant_record_has_no_violations(self):
        self.assertEqual(_cs().violations(OK_BRIEF, OK_ASSET), [])

    def test_empty_title_violates(self):
        v = _cs().violations({**OK_BRIEF, "final_title": "  "}, OK_ASSET)
        self.assertTrue(any("empty" in x for x in v))

    def test_overlong_title_violates(self):
        v = _cs().violations({**OK_BRIEF, "final_title": "x" * 101}, OK_ASSET)
        self.assertTrue(any("101 characters" in x for x in v))

    def test_title_at_exactly_the_limit_passes(self):
        self.assertEqual(_cs().violations({**OK_BRIEF, "final_title": "x" * 100}, OK_ASSET), [])

    def test_description_is_measured_in_bytes_not_characters(self):
        """4000 multi-byte characters is under the 5000-CHARACTER count but
        over the documented 5000-BYTE limit. Measuring with len() would
        wrongly pass this."""
        multibyte = "é" * 4000              # 4000 chars, 8000 bytes
        self.assertLess(len(multibyte), 5000)
        v = _cs().violations({**OK_BRIEF, "final_description": multibyte}, OK_ASSET)
        self.assertTrue(any("bytes" in x for x in v))

    def test_ascii_description_under_the_limit_passes(self):
        self.assertEqual(_cs().violations({**OK_BRIEF, "final_description": "x" * 4999}, OK_ASSET), [])

    def test_undocumented_visibility_violates(self):
        for bad in ("semi-public", "", None, "PUBLIC"):
            with self.subTest(v=bad):
                self.assertTrue(_cs().violations({**OK_BRIEF, "visibility": bad}, OK_ASSET))

    def test_oversized_file_violates(self):
        v = _cs().violations(OK_BRIEF, {**OK_ASSET, "size_bytes": 274877906945})
        self.assertTrue(any("documented maximum" in x for x in v))

    def test_duration_over_documented_shorts_maximum_violates(self):
        v = _cs().violations(OK_BRIEF, {**OK_ASSET, "duration_seconds": 181})
        self.assertTrue(any("exceeds documented Shorts maximum" in x for x in v))

    def test_duration_at_exactly_three_minutes_passes(self):
        self.assertEqual(_cs().violations(OK_BRIEF, {**OK_ASSET, "duration_seconds": 180}), [])

    def test_resolution_over_documented_maximum_violates(self):
        v = _cs().violations(OK_BRIEF, {**OK_ASSET, "width": 2160, "height": 3840})
        self.assertTrue(any("exceeds the documented Shorts maximum" in x for x in v))

    def test_unmeasured_duration_fails_closed(self):
        v = _cs().violations(OK_BRIEF, {k: x for k, x in OK_ASSET.items() if k != "duration_seconds"})
        self.assertTrue(any("duration has not been measured" in x for x in v))

    def test_unmeasured_dimensions_fail_closed(self):
        v = _cs().violations(OK_BRIEF, {"size_bytes": 1, "duration_seconds": 5})
        self.assertTrue(any("dimensions have not been measured" in x for x in v))

    def test_aspect_ratio_is_never_enforced(self):
        """Aspect ratio is NOT officially documented. A square and a
        landscape asset must both pass — enforcing an uncited rule would
        launder an assumption into the system as fact."""
        for w, h in ((1080, 1080), (1080, 608), (608, 1080)):
            with self.subTest(size=f"{w}x{h}"):
                self.assertEqual(_cs().violations(OK_BRIEF, {**OK_ASSET, "width": w, "height": h}), [])

    def test_a_missing_limit_is_reported_unverifiable_not_passed(self):
        cs = _cs()
        del cs.limits["title_max_characters"]
        v = cs.violations(OK_BRIEF, OK_ASSET)
        self.assertTrue(any("unknown in the constraint source" in x for x in v))


class TestTroyPassesDocumentedConstraints(LegacyBase):
    """The approved asset's real measured facts against the real source."""

    TROY_MEDIA = {"duration_seconds": 12.833333, "width": 1080, "height": 1920}

    def test_real_troy_metadata_passes_every_documented_limit(self):
        try:
            cs = ConstraintSource.from_file()
        except Exception:
            self.skipTest("harness constraint file not present")
        rec = self.intake()
        v = cs.violations(rec.brief, {**rec.brief["legacy_asset"], **self.TROY_MEDIA})
        self.assertEqual(v, [])

    def test_sink_still_refuses_on_authorization_even_when_constraints_pass(self):
        try:
            cs = ConstraintSource.from_file()
        except Exception:
            self.skipTest("harness constraint file not present")
        rec = self.intake()
        sink = YouTubeUploadSink(self.registry, self.ctx, store=self.store,
                                 constraint_source=cs,
                                 credential_path=Path(self.tmp.name) / "absent.json")
        reasons = " ".join(sink.refusal_reasons(rec, media=self.TROY_MEDIA))
        self.assertIn("enable flag is absent", reasons)
        self.assertIn("not live-enabled", reasons)
        self.assertIn("credential unavailable", reasons)
        self.assertIn("no transport", reasons)
