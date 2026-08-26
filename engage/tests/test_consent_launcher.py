"""Tests for the one-time consent launcher at ~/engage-consent/.

The launcher lives OUTSIDE this repository on purpose; it is loaded here by
path so its behaviour is covered without it becoming project source.

Fully mocked: no browser opens, no loopback socket binds during a flow, no
real credential is written, no consent runs, and no YouTube API is
contacted. Several tests assert exactly that."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

LAUNCHER = Path.home() / "engage-consent" / "youtube_consent.py"
CHANNEL = "UC9772FnuAXMVabS0gtr6cew"


def load_launcher():
    spec = importlib.util.spec_from_file_location("youtube_consent_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(LAUNCHER.exists(), "launcher not present")
class LauncherBase(unittest.TestCase):
    def setUp(self):
        self.mod = load_launcher()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.client = self.root / "client.json"
        self.client.write_text(json.dumps({"installed": {
            "client_id": "cid.apps.googleusercontent.com",
            "client_secret": "GOCSPX-LAUNCHER-SECRET"}}))
        os.chmod(self.client, 0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv, consent_result=None, consent_side_effect=None, answer=None):
        buf = io.StringIO()
        with mock.patch.object(self.mod, "run_consent_hook", create=True):
            pass
        oauth = self.mod._load_helper()
        with mock.patch.object(oauth, "run_consent") as rc, \
             mock.patch("builtins.input", return_value=answer if answer is not None else "yes"), \
             mock.patch("webbrowser.open") as browser:
            if consent_side_effect:
                rc.side_effect = consent_side_effect
            else:
                rc.return_value = consent_result
            with redirect_stdout(buf):
                code = self.mod.main(argv)
        return code, buf.getvalue(), rc, browser


def outcome(mod, *, stored, verification, detail="", path=""):
    oauth = mod._load_helper()
    return oauth.ConsentOutcome(stored=stored, credential_path=path,
                                verification=verification, detail=detail)


# ---------------------------------------------------------------------------
# Location — the launcher must not live anywhere tracked or agent-readable
# ---------------------------------------------------------------------------
class TestLauncherLocation(unittest.TestCase):
    @unittest.skipUnless(LAUNCHER.exists(), "launcher not present")
    def test_is_outside_the_repository(self):
        repo = Path(__file__).resolve().parents[1]
        self.assertNotIn(repo, LAUNCHER.resolve().parents)

    @unittest.skipUnless(LAUNCHER.exists(), "launcher not present")
    def test_is_outside_dot_claude(self):
        self.assertNotIn(Path.home() / ".claude", LAUNCHER.resolve().parents)

    @unittest.skipUnless(LAUNCHER.exists(), "launcher not present")
    def test_is_not_inside_any_git_worktree(self):
        for parent in [LAUNCHER.resolve(), *LAUNCHER.resolve().parents]:
            with self.subTest(parent=str(parent)):
                self.assertFalse((parent / ".git").exists())

    def test_no_production_code_references_the_launcher(self):
        """It must not be reachable from cron, hooks, tasks, or agent paths.

        Scoped to the `engage/` package — production code. Tests may load
        the launcher to verify it (tests/test_consent_post_redirect.py
        exercises its real callback parser), which is the opposite of
        wiring it into an automated path."""
        pkg = Path(__file__).resolve().parents[1] / "engage"
        hits = [str(p) for p in pkg.rglob("*.py")
                if "youtube_consent" in p.read_text(encoding="utf-8", errors="ignore")]
        self.assertEqual(hits, [], "no production module may reference the launcher")

    def test_no_scheduled_task_or_hook_invokes_the_launcher(self):
        """The other half: nothing automated may INVOKE it.

        Scheduled tasks are defined by any file under scheduled-tasks/
        (including SKILL.md), so that tree is scanned wholesale. The harness
        is scanned for executables only — its decision log legitimately
        names the launcher's path in prose, and documenting a path is the
        opposite of wiring it into an automated path."""
        targets = []
        tasks = Path.home() / ".claude" / "scheduled-tasks"
        if tasks.exists():
            targets += [p for p in tasks.rglob("*") if p.is_file()]
        harness = Path.home() / ".claude" / "harness"
        if harness.exists():
            targets += [p for p in harness.rglob("*")
                        if p.is_file() and p.suffix in (".sh", ".py", ".json", ".yaml", ".yml")]
        hits = []
        for p in targets:
            try:
                if "youtube_consent.py" in p.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(p))
            except OSError:
                pass
        self.assertEqual(hits, [], "no scheduled task or hook may invoke the launcher")


# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------
class TestConfirmation(LauncherBase):
    OK_V = {"status": "verified", "expected_channel_id": CHANNEL,
            "returned_channel_ids": [CHANNEL], "returned_channel_count": 1,
            "target_channel_membership_verified": True}

    def test_declining_cancels_without_opening_a_browser(self):
        code, out, rc, browser = self.run_main(
            ["--client-json", str(self.client)], answer="n")
        self.assertEqual(code, 1)
        self.assertIn("Cancelled", out)
        browser.assert_not_called()
        rc.assert_not_called()

    def test_explicit_yes_proceeds(self):
        code, out, rc, _ = self.run_main(
            ["--client-json", str(self.client)], answer="yes",
            consent_result=outcome(self.mod, stored=True, verification=self.OK_V,
                                   path="/tmp/x.json"))
        self.assertEqual(code, 0)
        rc.assert_called_once()

    def test_bare_enter_proceeds(self):
        code, _, rc, _ = self.run_main(
            ["--client-json", str(self.client)], answer="",
            consent_result=outcome(self.mod, stored=True, verification=self.OK_V,
                                   path="/tmp/x.json"))
        self.assertEqual(code, 0)
        rc.assert_called_once()

    def test_notice_shows_both_scopes_and_the_channel_before_confirming(self):
        _, out, _, _ = self.run_main(["--client-json", str(self.client)], answer="n")
        self.assertIn("youtube.upload", out)
        self.assertIn("youtube.readonly", out)
        self.assertIn(CHANNEL, out)

    def test_notice_states_nothing_is_uploaded(self):
        _, out, _, _ = self.run_main(["--client-json", str(self.client)], answer="n")
        self.assertIn("will not upload", out)


# ---------------------------------------------------------------------------
# Client-file validation happens before anything is offered
# ---------------------------------------------------------------------------
class TestClientFileGate(LauncherBase):
    def test_missing_client_file_refused_before_any_prompt(self):
        code, out, rc, browser = self.run_main(
            ["--client-json", str(self.root / "nope.json")])
        self.assertEqual(code, 2)
        self.assertIn("Refused", out)
        browser.assert_not_called()
        rc.assert_not_called()

    def test_world_readable_client_refused(self):
        os.chmod(self.client, 0o644)
        code, out, _, browser = self.run_main(["--client-json", str(self.client)])
        self.assertEqual(code, 2)
        self.assertIn("chmod 600", out)
        browser.assert_not_called()

    def test_client_inside_the_repo_refused(self):
        repo = Path(__file__).resolve().parents[1]
        inside = repo / "_tmp_launcher_client.json"
        inside.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}))
        os.chmod(inside, 0o600)
        try:
            code, out, _, browser = self.run_main(["--client-json", str(inside)])
            self.assertEqual(code, 2)
            browser.assert_not_called()
        finally:
            inside.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
class TestOutcomes(LauncherBase):
    def test_success_reports_path_and_permissions_language(self):
        v = {"status": "verified", "expected_channel_id": CHANNEL,
             "returned_channel_ids": [CHANNEL], "returned_channel_count": 1,
             "target_channel_membership_verified": True}
        code, out, _, _ = self.run_main(
            ["--client-json", str(self.client)],
            consent_result=outcome(self.mod, stored=True, verification=v,
                                   path=str(Path.home() / ".config/engage/youtube/empires.json")))
        self.assertEqual(code, 0)
        self.assertIn("CONSENT SUCCEEDED", out)
        self.assertIn("0600", out)
        self.assertIn("empires.json", out)

    def test_success_states_sink_and_live_status_unchanged(self):
        v = {"status": "verified", "expected_channel_id": CHANNEL,
             "returned_channel_ids": [CHANNEL], "returned_channel_count": 1,
             "target_channel_membership_verified": True}
        _, out, _, _ = self.run_main(["--client-json", str(self.client)],
                                     consent_result=outcome(self.mod, stored=True,
                                                            verification=v, path="/tmp/x"))
        self.assertIn("live_status are", out)
        self.assertIn("Nothing has been uploaded", out)

    def test_multi_channel_success_shows_the_full_list(self):
        v = {"status": "verified", "expected_channel_id": CHANNEL,
             "returned_channel_ids": ["UCother", CHANNEL], "returned_channel_count": 2,
             "target_channel_membership_verified": True}
        _, out, _, _ = self.run_main(["--client-json", str(self.client)],
                                     consent_result=outcome(self.mod, stored=True,
                                                            verification=v, path="/tmp/x"))
        self.assertIn("UCother", out)
        self.assertIn("2", out)

    def test_success_message_distinguishes_control_from_upload_target(self):
        v = {"status": "verified", "expected_channel_id": CHANNEL,
             "returned_channel_ids": [CHANNEL], "returned_channel_count": 1,
             "target_channel_membership_verified": True}
        _, out, _, _ = self.run_main(["--client-json", str(self.client)],
                                     consent_result=outcome(self.mod, stored=True,
                                                            verification=v, path="/tmp/x"))
        self.assertIn("does not prove which", out)

    def test_channel_mismatch_reports_failure_and_no_credential(self):
        v = {"status": "mismatch", "expected_channel_id": CHANNEL,
             "actual_channel_id": "UCwrong", "returned_channel_count": 1,
             "target_channel_membership_verified": False}
        code, out, _, _ = self.run_main(
            ["--client-json", str(self.client)],
            consent_result=outcome(self.mod, stored=False, verification=v,
                                   detail="no credential was retained"))
        self.assertEqual(code, 4)
        self.assertIn("DID NOT PRODUCE A CREDENTIAL", out)
        self.assertIn("UCwrong", out)
        self.assertIn("No credential was retained", out)

    def test_state_mismatch_surfaces_as_a_failure(self):
        oauth = self.mod._load_helper()
        code, out, _, _ = self.run_main(
            ["--client-json", str(self.client)],
            consent_side_effect=oauth.OAuthHelperError("OAuth state mismatch"))
        self.assertEqual(code, 3)
        self.assertIn("state mismatch", out)

    def test_no_secret_appears_in_any_output(self):
        v = {"status": "verified", "expected_channel_id": CHANNEL,
             "returned_channel_ids": [CHANNEL], "returned_channel_count": 1,
             "target_channel_membership_verified": True}
        for result in (outcome(self.mod, stored=True, verification=v, path="/tmp/x"),
                       outcome(self.mod, stored=False, verification=v, detail="failed")):
            _, out, _, _ = self.run_main(["--client-json", str(self.client)],
                                         consent_result=result)
            self.assertNotIn("GOCSPX-LAUNCHER-SECRET", out)
            self.assertNotIn("cid.apps.googleusercontent.com", out)


class TestLauncherTakesNoWriteAction(LauncherBase):
    def test_launcher_never_imports_the_upload_sink(self):
        src = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("YouTubeUploadSink", src)
        self.assertNotIn("mint_authorization", src)

    def test_launcher_contains_no_upload_or_live_status_mutation(self):
        """Checks for MUTATION-shaped code, not the bare words. The launcher
        legitimately names live_status and uploading in its own 'what this
        will never do' prose, which is worth keeping — so the test targets
        assignment and call constructs instead."""
        src = LAUNCHER.read_text(encoding="utf-8")
        for construct in ('live_status =', 'live_status=', '"live_status":',
                          "videos.insert", "enabled=True", "enabled = True",
                          ".upload(", "mint_authorization("):
            with self.subTest(construct=construct):
                self.assertNotIn(construct, src)

    def test_launcher_prose_does_state_what_it_will_never_do(self):
        src = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("live_status", src)  # named in the safety prose, deliberately

    def test_scopes_come_from_the_audited_helper_not_the_launcher(self):
        src = LAUNCHER.read_text(encoding="utf-8")
        self.assertNotIn("auth/youtube.upload", src)
        self.assertNotIn("auth/youtube.readonly", src)

    def test_loopback_binds_only_localhost(self):
        src = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1"', src)
        self.assertNotIn('"0.0.0.0"', src)

    def test_callback_handler_silences_access_logs(self):
        """The redirect URL query string carries the authorization code."""
        self.assertIn("def log_message", LAUNCHER.read_text(encoding="utf-8"))

    def test_launcher_tests_never_write_the_real_credential(self):
        """A real credential now exists (owner consent, 2026-08-22). These
        tests must leave it exactly as found."""
        p = Path.home() / ".config" / "engage" / "youtube" / "empires.json"
        before = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
        load_launcher()
        after = (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
