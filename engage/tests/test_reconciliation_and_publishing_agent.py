"""Reconciliation, capability preflight, and the Publishing Operations Agent.

Every fixture here is synthetic. Nothing in this suite or the code it
exercises opens a socket, reads a credential, mints a token, or uploads.
"""
import unittest
from pathlib import Path

from engage.core.models import ContentRecord, Draft, Post
from engage.publish import agent as pub_agent
from engage.publish.capability import (
    SATISFIED,
    UNKNOWN,
    UNSATISFIED,
    youtube_capability,
)
from engage.publish.reconcile import (
    ReconciliationError,
    already_live,
    base_title,
    live_titles,
    reconcile_youtube,
)

from .helpers import FIXTURES, PKG_DIR, ctx_for, mem_store
from .test_originals import load_test_registry

CHANNEL = "UC9772FnuAXMVabS0gtr6cew"
NOW = 1_700_000_000


def post(pid, title, url="", created=NOW):
    """Matches ingest/youtube_rss.py's shape: text is '<title>\\n\\n<body>'."""
    return Post(id=pid, brand="empires", platform="youtube", author="chan",
                text=f"{title}\n\nbody text", url=url or f"https://youtu.be/{pid}",
                created_at=created, snapshots=[], own=True)


class LiveRegistry:
    """Minimal registry stand-in for the live-enabled path. The fixture
    registry deliberately has no live cell, so a live path needs one."""

    def __init__(self, live=True, channel_id=CHANNEL):
        self._live, self._channel = live, channel_id

    def account_row(self, brand, platform):
        return {"channel_id": self._channel, "live_status": "live" if self._live else "disabled"}

    def is_live_enabled(self, brand, platform):
        return self._live


class FakeSink:
    def __init__(self, reasons=()):
        self._reasons = list(reasons)

    def refusal_reasons(self, record=None, media=None):
        return list(self._reasons)


def saved_record(store, rid, *, title, status="verified", pid="", platform="youtube"):
    """Build a record at a given lifecycle state.

    'approved' cannot be faked — save_content_record() refuses to persist it
    without a real approvals row for the underlying draft, whatever set the
    field. So an approved fixture builds the draft and the approval too,
    rather than the test reaching around the invariant it depends on.
    """
    draft_id = ""
    if status == "approved":
        draft = Draft(id=f"dr-{rid}", brand="empires", kind="original",
                      platform=platform, text=title, status="APPROVED")
        store.save_draft(draft)
        store.record_approval(draft.id, draft.hash)
        draft_id = draft.id
    rec = ContentRecord(id=rid, brand="empires", platform=platform,
                        kind="legacy_asset_intake", lifecycle_status=status,
                        draft_id=draft_id,
                        brief={"final_title": title, "channel_id": CHANNEL})
    if pid:
        rec.platform_post_id = pid
        rec.published_url = f"https://www.youtube.com/shorts/{pid}"
    store.save_content_record(rec)
    return rec


# ---------------------------------------------------------------------------
# external_posts storage
# ---------------------------------------------------------------------------
class TestExternalPostStorage(unittest.TestCase):
    def setUp(self):
        self.store = mem_store()

    def test_requires_a_platform_id(self):
        with self.assertRaises(ValueError):
            self.store.record_external_post(brand="empires", platform="youtube",
                                            platform_post_id="")

    def test_reobserving_is_idempotent(self):
        for _ in range(3):
            self.store.record_external_post(brand="empires", platform="youtube",
                                            platform_post_id="abc", title="T")
        self.assertEqual(len(self.store.list_external_posts("empires", "youtube")), 1)

    def test_conflict_updates_rather_than_replacing(self):
        """The write path must NOT be INSERT OR REPLACE.

        Asserting the specific survival, not merely 'one row remains': a
        REPLACE also leaves one row, having deleted the prior one and lost
        the attribution. That is the failure this test exists to catch.
        """
        self.store.record_external_post(brand="empires", platform="youtube",
                                        platform_post_id="abc", title="Old",
                                        content_record_id="rec-1")
        self.store.record_external_post(brand="empires", platform="youtube",
                                        platform_post_id="abc", title="New",
                                        content_record_id="")
        row = self.store.get_external_post("empires", "youtube", "abc")
        self.assertEqual(row["title"], "New")           # mutable field refreshed
        self.assertEqual(row["content_record_id"], "rec-1")  # attribution SURVIVED

    def test_observed_at_records_first_sighting(self):
        self.store.record_external_post(brand="empires", platform="youtube",
                                        platform_post_id="abc", title="T")
        first = self.store.get_external_post("empires", "youtube", "abc")["observed_at"]
        self.store.record_external_post(brand="empires", platform="youtube",
                                        platform_post_id="abc", title="T2")
        again = self.store.get_external_post("empires", "youtube", "abc")["observed_at"]
        self.assertEqual(first, again)

    def test_brands_do_not_collide(self):
        self.store.record_external_post(brand="empires", platform="youtube",
                                        platform_post_id="abc", title="A")
        self.store.record_external_post(brand="capstack", platform="youtube",
                                        platform_post_id="abc", title="B")
        self.assertEqual(len(self.store.list_external_posts("empires", "youtube")), 1)
        self.assertEqual(len(self.store.list_external_posts("capstack", "youtube")), 1)


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------
class TestReconciliation(unittest.TestCase):
    def setUp(self):
        self.store = mem_store()
        self.ctx = ctx_for("empires")
        self.registry = load_test_registry()

    def test_refuses_without_a_verified_channel(self):
        class NoChannel:
            def account_row(self, b, p):
                return {}

            def is_live_enabled(self, b, p):
                return False
        with self.assertRaises(ReconciliationError):
            reconcile_youtube(self.store, self.ctx, NoChannel(),
                              rss_reader=lambda: [])

    def test_unknown_posts_become_drift(self):
        rep = reconcile_youtube(
            self.store, self.ctx, self.registry,
            rss_reader=lambda: [post("v1", "One"), post("v2", "Two")])
        self.assertEqual(rep.observed, 2)
        self.assertEqual(rep.drift, 2)
        self.assertEqual(len(rep.unattributed_new), 2)
        self.assertEqual(len(rep.attributed), 0)

    def test_second_pass_reports_known_not_new(self):
        reader = lambda: [post("v1", "One")]  # noqa: E731
        reconcile_youtube(self.store, self.ctx, self.registry, rss_reader=reader)
        rep = reconcile_youtube(self.store, self.ctx, self.registry, rss_reader=reader)
        self.assertEqual(len(rep.unattributed_new), 0)
        self.assertEqual(len(rep.unattributed_known), 1)
        self.assertEqual(rep.drift, 1)

    def test_records_we_published_are_attributed(self):
        saved_record(self.store, "rec-1", title="One", status="verified", pid="v1")
        rep = reconcile_youtube(self.store, self.ctx, self.registry,
                                rss_reader=lambda: [post("v1", "One")])
        self.assertEqual(len(rep.attributed), 1)
        self.assertEqual(rep.drift, 0)
        self.assertIsNone(self.store.get_external_post("empires", "youtube", "v1"))

    def test_unpublished_record_does_not_attribute(self):
        """A record at review_required cannot be the origin of a live post."""
        saved_record(self.store, "rec-1", title="One", status="review_required")
        rep = reconcile_youtube(self.store, self.ctx, self.registry,
                                rss_reader=lambda: [post("v1", "One")])
        self.assertEqual(rep.drift, 1)


# ---------------------------------------------------------------------------
# the duplicate hole this all exists to close
# ---------------------------------------------------------------------------
class TestAlreadyLive(unittest.TestCase):
    def setUp(self):
        self.store = mem_store()
        self.ctx = ctx_for("empires")
        self.registry = load_test_registry()

    def test_externally_posted_title_is_detected(self):
        """The regression: a post made outside this system was invisible, so
        a duplicate upload of it would have been cleared."""
        reconcile_youtube(self.store, self.ctx, self.registry,
                          rss_reader=lambda: [post("v1", "The Evolution of the Roman Shield")])
        hit = already_live(self.store, "empires", "youtube",
                           "The Evolution of the Roman Shield")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "external_post")
        self.assertEqual(hit["platform_post_id"], "v1")

    def test_our_own_published_title_is_detected(self):
        saved_record(self.store, "rec-1", title="Ours", status="published", pid="v9")
        hit = already_live(self.store, "empires", "youtube", "Ours")
        self.assertEqual(hit["source"], "content_record")

    def test_match_is_exact_not_substring(self):
        reconcile_youtube(self.store, self.ctx, self.registry,
                          rss_reader=lambda: [post("v1", "SOMETHING ELSE ENTIRELY")])
        self.assertIsNone(already_live(self.store, "empires", "youtube", "T"))

    def test_differing_hashtags_still_match(self):
        """The real 2026-08-26 case: staged with one tag set, published with
        another. Exact full-title comparison missed it."""
        reconcile_youtube(
            self.store, self.ctx, self.registry,
            rss_reader=lambda: [post(
                "v1", "The Evolution of the Greek Helmet #ancientgreece #trojanwar #theodyssey")])
        hit = already_live(self.store, "empires", "youtube",
                           "The Evolution of the Greek Helmet #ancientgreece #hoplite")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["matched_on"], "base_title")

    def test_base_title_does_not_collapse_distinct_subjects(self):
        reconcile_youtube(
            self.store, self.ctx, self.registry,
            rss_reader=lambda: [post("v1", "The Evolution of the Roman Shield #a #b")])
        self.assertIsNone(already_live(self.store, "empires", "youtube",
                                       "The Evolution of the Greek Helmet #a #b"))

    def test_base_title_strips_only_trailing_tags(self):
        self.assertEqual(base_title("A #b c #d #e"), "A #b c")
        self.assertEqual(base_title("Plain title"), "Plain title")
        self.assertEqual(base_title("#only #tags"), "")

    def test_all_hashtag_title_does_not_match_everything(self):
        """base_title('') must never become a wildcard."""
        reconcile_youtube(self.store, self.ctx, self.registry,
                          rss_reader=lambda: [post("v1", "Real Title #x")])
        self.assertIsNone(already_live(self.store, "empires", "youtube", "#x #y"))

    def test_unknown_title_returns_none(self):
        self.assertIsNone(already_live(self.store, "empires", "youtube", "Never posted"))

    def test_live_titles_unions_both_sources(self):
        saved_record(self.store, "rec-1", title="Ours", status="verified", pid="v9")
        reconcile_youtube(self.store, self.ctx, self.registry,
                          rss_reader=lambda: [post("v1", "Theirs")])
        self.assertEqual(live_titles(self.store, "empires", "youtube"), {"Ours", "Theirs"})


# ---------------------------------------------------------------------------
# capability preflight
# ---------------------------------------------------------------------------
class TestCapability(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("empires")

    def test_unprobed_permission_is_unknown_and_blocks(self):
        rep = youtube_capability(self.ctx, LiveRegistry(),
                                 sink_factory=lambda: FakeSink())
        self.assertEqual(rep.preconditions["permission"]["status"], UNKNOWN)
        self.assertFalse(rep.publishable)

    def test_refused_permission_is_unsatisfied(self):
        rep = youtube_capability(self.ctx, LiveRegistry(),
                                 sink_factory=lambda: FakeSink(),
                                 permission_probe=lambda: False)
        self.assertEqual(rep.preconditions["permission"]["status"], UNSATISFIED)
        self.assertFalse(rep.publishable)

    def test_a_probe_that_raises_counts_as_denial(self):
        def boom():
            raise RuntimeError("blocked by classifier")
        rep = youtube_capability(self.ctx, LiveRegistry(),
                                 sink_factory=lambda: FakeSink(), permission_probe=boom)
        self.assertEqual(rep.preconditions["permission"]["status"], UNSATISFIED)

    def test_all_satisfied_is_publishable(self):
        rep = youtube_capability(self.ctx, LiveRegistry(),
                                 sink_factory=lambda: FakeSink(),
                                 permission_probe=lambda: True)
        self.assertTrue(rep.publishable, rep.blockers)

    def test_absent_sink_factory_is_unknown_not_inferred(self):
        """The directive bug: capability asserted from prose. Without a sink
        to interrogate, the answer is UNKNOWN — never a guess either way."""
        rep = youtube_capability(self.ctx, LiveRegistry(), permission_probe=lambda: True)
        for key in ("sink", "credential", "transport"):
            self.assertEqual(rep.preconditions[key]["status"], UNKNOWN)
        self.assertFalse(rep.publishable)

    def test_disabled_registry_row_blocks(self):
        rep = youtube_capability(self.ctx, LiveRegistry(live=False),
                                 sink_factory=lambda: FakeSink(),
                                 permission_probe=lambda: True)
        self.assertEqual(rep.preconditions["registry"]["status"], UNSATISFIED)
        self.assertFalse(rep.publishable)

    def test_missing_credential_is_surfaced(self):
        rep = youtube_capability(
            self.ctx, LiveRegistry(),
            sink_factory=lambda: FakeSink(["credential unavailable: no such file"]),
            permission_probe=lambda: True)
        self.assertEqual(rep.preconditions["credential"]["status"], UNSATISFIED)

    def test_sink_construction_failure_does_not_claim_downstream_answers(self):
        def boom():
            raise RuntimeError("nope")
        rep = youtube_capability(self.ctx, LiveRegistry(), sink_factory=boom)
        self.assertEqual(rep.preconditions["sink"]["status"], UNSATISFIED)
        self.assertEqual(rep.preconditions["credential"]["status"], UNKNOWN)


# ---------------------------------------------------------------------------
# the Publishing Operations Agent
# ---------------------------------------------------------------------------
def directive_row(rid="d1", *, traces=(), scope=("youtube",), deliverable="Post it"):
    return {"id": rid, "brand": "empires", "target": "publishing",
            "label": "observation", "summary": f"[CEO DIRECTIVE] {deliverable}",
            "evidence": {"kind": "ceo_directive", "deliverable": deliverable,
                         "deadline": "09:30 EDT", "platform_scope": list(scope),
                         "traces_to": list(traces)},
            "confidence": "n/a", "cross_brand": False,
            "authorization": "NONE", "created_at": NOW}


class TestPublishingAgent(unittest.TestCase):
    def setUp(self):
        self.store = mem_store()
        self.ctx = ctx_for("empires")
        self.registry = LiveRegistry()

    def assess(self, row, **kw):
        kw.setdefault("sink_factory", lambda: FakeSink())
        kw.setdefault("permission_probe", lambda: True)
        return pub_agent.assess(self.store, self.ctx, self.registry, row, **kw)

    def test_cannot_approve_structurally(self):
        """The one boundary whose erosion makes every downstream gate
        decorative. Checked against source, not behaviour."""
        src = (PKG_DIR / "publish" / "agent.py").read_text(encoding="utf-8")
        self.assertNotIn("approve_content_record", src)
        self.assertNotIn("approve_legacy_asset", src)
        self.assertNotIn("from ..approval", src)

    def test_unapproved_record_escalates_rather_than_self_approving(self):
        saved_record(self.store, "rec-1", title="T", status="review_required")
        disp = self.assess(directive_row(traces=["rec-1"]))
        self.assertEqual(disp.status, pub_agent.NEEDS_OWNER_APPROVAL)
        self.assertFalse(disp.executable)

    def test_duplicate_blocks_before_approval_is_considered(self):
        saved_record(self.store, "rec-1", title="Dup", status="approved")
        self.store.record_external_post(brand="empires", platform="youtube",
                                        platform_post_id="v1", title="Dup")
        disp = self.assess(directive_row(traces=["rec-1"]))
        self.assertEqual(disp.status, pub_agent.BLOCKED_DUPLICATE)
        self.assertEqual(disp.duplicate_evidence["platform_post_id"], "v1")

    def test_unprobed_permission_blocks_execution(self):
        saved_record(self.store, "rec-1", title="T", status="approved")
        disp = self.assess(directive_row(traces=["rec-1"]), permission_probe=None)
        self.assertEqual(disp.status, pub_agent.BLOCKED_CAPABILITY)

    def test_directive_naming_no_record_is_not_executable(self):
        disp = self.assess(directive_row(traces=[]))
        self.assertEqual(disp.status, pub_agent.NO_RECORD)

    def test_out_of_scope_platform_refused(self):
        disp = self.assess(directive_row(traces=[], scope=("tiktok",)))
        self.assertEqual(disp.status, pub_agent.BLOCKED_CAPABILITY)

    def test_ready_when_everything_clears(self):
        saved_record(self.store, "rec-1", title="Fresh", status="approved")
        disp = self.assess(directive_row(traces=["rec-1"]))
        self.assertEqual(disp.status, pub_agent.READY, disp.reasons)
        self.assertTrue(disp.executable)

    def test_open_directives_excludes_executed(self):
        self.store.save_recommendation(directive_row("d1"))
        self.assertEqual(len(pub_agent.open_directives(self.store, self.ctx)), 1)
        self.store.record_content_event("d1", pub_agent.EXECUTED_EVENT, {})
        self.assertEqual(len(pub_agent.open_directives(self.store, self.ctx)), 0)

    def test_non_directive_recommendations_are_ignored(self):
        self.store.save_recommendation({
            "id": "r9", "brand": "empires", "target": "publishing", "label": "observation",
            "summary": "not a directive", "evidence": {"kind": "something_else"},
            "confidence": "low", "cross_brand": False, "authorization": "NONE",
            "created_at": NOW})
        self.assertEqual(pub_agent.open_directives(self.store, self.ctx), [])

    def test_work_queue_assesses_every_open_directive(self):
        saved_record(self.store, "rec-1", title="T", status="review_required")
        self.store.save_recommendation(directive_row("d1", traces=["rec-1"]))
        self.store.save_recommendation(directive_row("d2", traces=["rec-1"]))
        q = pub_agent.work_queue(self.store, self.ctx, self.registry,
                                 sink_factory=lambda: FakeSink(),
                                 permission_probe=lambda: True)
        self.assertEqual(len(q), 2)
        self.assertTrue(all(d.status == pub_agent.NEEDS_OWNER_APPROVAL for d in q))


class TestAgentPurity(unittest.TestCase):
    def test_no_network_or_credential_capability_in_reconcile(self):
        src = (PKG_DIR / "publish" / "reconcile.py").read_text(encoding="utf-8")
        for banned in ("urllib", "requests", "socket", "credential", "token"):
            self.assertNotIn(f"import {banned}", src)

    def test_capability_module_makes_no_network_call(self):
        src = (PKG_DIR / "publish" / "capability.py").read_text(encoding="utf-8")
        for banned in ("urllib", "requests", "socket"):
            self.assertNotIn(f"import {banned}", src)


if __name__ == "__main__":
    unittest.main()


class ExternalPostTimestamps(unittest.TestCase):
    """A recorded post must never be silently unsortable.

    Found in production on 2026-08-26 by a peer session, not by this suite:
    the manual record path supplies no published_at, so the TikTok and
    Instagram rows went in as NULL. The rows existed and `SELECT COUNT(*)`
    saw them, but every query that ORDERed or filtered on published_at
    dropped them without error — so a reader concluded those platforms had
    never been recorded at all. Same failure this table exists to prevent,
    one layer down: present in the store, absent from the answer.
    """

    def test_missing_published_at_falls_back_to_observed_at(self):
        store = mem_store()
        store.record_external_post(brand="empires", platform="tiktok",
                                   platform_post_id="tt1", title="a tiktok post")
        row = store.get_external_post("empires", "tiktok", "tt1")
        self.assertIsNotNone(row["published_at"],
                             "a recorded post with no timestamp is invisible to "
                             "every ordered query — it must never be NULL")
        self.assertEqual(row["published_at"], row["observed_at"])

    def test_recorded_post_survives_an_ordered_query(self):
        """The actual symptom, asserted directly rather than via the column."""
        store = mem_store()
        store.record_external_post(brand="empires", platform="instagram",
                                   platform_post_id="ig1", title="an ig post")
        rows = store.db.execute(
            "SELECT platform_post_id FROM external_posts "
            "WHERE brand='empires' AND published_at IS NOT NULL "
            "ORDER BY published_at DESC").fetchall()
        self.assertEqual([r[0] for r in rows], ["ig1"])

    def test_fallback_never_overwrites_a_real_platform_timestamp(self):
        """RSS supplies the true publish time. A later manual re-record that
        omits it must not downgrade that to 'whenever we happened to look'."""
        store = mem_store()
        store.record_external_post(brand="empires", platform="youtube",
                                   platform_post_id="yt1", title="from rss",
                                   published_at=NOW)
        store.record_external_post(brand="empires", platform="youtube",
                                   platform_post_id="yt1", title="re-recorded by hand")
        row = store.get_external_post("empires", "youtube", "yt1")
        self.assertEqual(row["published_at"], NOW)
        self.assertEqual(row["title"], "re-recorded by hand")

    def test_a_supplied_timestamp_still_wins_on_conflict(self):
        store = mem_store()
        store.record_external_post(brand="empires", platform="youtube",
                                   platform_post_id="yt2", title="first")
        store.record_external_post(brand="empires", platform="youtube",
                                   platform_post_id="yt2", title="corrected",
                                   published_at=NOW)
        row = store.get_external_post("empires", "youtube", "yt2")
        self.assertEqual(row["published_at"], NOW)
