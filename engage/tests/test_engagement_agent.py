"""Engagement Agent (Phase 5) — review-only. Synthetic/manual fixtures
only; no live comment fetch anywhere in this suite or the code it
exercises. No comment/DM/like/follow/moderation action exists anywhere in
engagement/ — verified structurally, not just asserted."""
import tempfile
import unittest
from pathlib import Path

from engage.config.registry import load_registry
from engage.core.models import REPLY_REVIEW_TRANSITIONS
from engage.drafting.originals import approve_content_record, create_original_content
from engage.engagement.agent import (
    EngagementRejected,
    approve_for_manual_posting,
    emit_engagement_insights,
    generate_reply_review,
    import_comment,
    import_comments_batch,
    render_review_queue_item,
    route_lead_signal,
    route_risk_signal,
    transition_reply,
)
from engage.engagement.reports import build_engagement_daily_exceptions, build_engagement_weekly_summary
from engage.engagement.triage_ext import classify_extended

from .helpers import PKG_DIR, ctx_for, mem_store
from .test_originals import FIXTURES, load_test_registry

NOW = 1_700_000_000


def stub(prompt: str) -> str:
    return "PASS" if "'PASS' or 'BLOCK" in prompt else "A hook.\nCaption body about capital stack."


def approved_record(store, ctx, registry, mid, text, platform="instagram"):
    store.add_material(mid, mid, text, "note")
    rec = create_original_content(store, ctx, registry, store.get_material(mid), platform, stub,
                                  audience="a", objective="o", cta="c")
    return approve_content_record(store, ctx, rec, approver="nick")


def comment_row(source_id, text="What does the capital stack actually look like?",
                author="curious_gp", platform="instagram", external_id="ext-1", **over):
    row = {"text": text, "author": author, "platform": platform, "source_type": "content_record",
          "source_id": source_id, "external_id": external_id}
    row.update(over)
    return row


class TestBrandAndAccountIsolation(unittest.TestCase):
    """§2 — malformed, cross-brand, disabled/unconnected/shared/parked/
    non-applicable rows all refused, never blended in."""

    def test_rejects_missing_required_fields(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        with self.assertRaises(EngagementRejected):
            import_comment(store, ctx, registry, {"text": "hi"})

    def test_rejects_unknown_source_type(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        with self.assertRaises(EngagementRejected):
            import_comment(store, ctx, registry, comment_row("x") | {"source_type": "tweet"})

    def test_rejects_nonexistent_source(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        with self.assertRaises(EngagementRejected) as cm:
            import_comment(store, ctx, registry, comment_row("no-such-record"))
        self.assertIn("no valid source context", str(cm.exception))

    def test_rejects_cross_brand_source(self):
        store = mem_store()
        empires_ctx, capstack_ctx = ctx_for("empires"), ctx_for("capstack")
        registry = load_test_registry()
        rec = approved_record(store, empires_ctx, registry, "m1", "text")
        with self.assertRaises(EngagementRejected) as cm:
            import_comment(store, capstack_ctx, registry, comment_row(rec.id))
        self.assertIn("belongs to brand", str(cm.exception))

    def test_rejects_disabled_platform(self):
        store, ctx = mem_store(), ctx_for("capstack")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "brands.yaml").write_text(
                'brands:\n  - canonical: "Capstacknick"\n    engage_slug: capstack\n    status: active\n',
                encoding="utf-8")
            (tmp / "accounts.yaml").write_text(
                'accounts:\n  - brand: "Capstacknick"\n    platform: tiktok\n    enabled: true\n',
                encoding="utf-8")
            permissive = load_registry(tmp)
            rec = approved_record(store, ctx, permissive, "m1", "text", platform="tiktok")
        strict = load_test_registry()
        with self.assertRaises(EngagementRejected) as cm:
            import_comment(store, ctx, strict, comment_row(rec.id, platform="tiktok"))
        self.assertIn("not eligible", str(cm.exception))

    def test_rejects_shared_account_platform(self):
        store, ctx = mem_store(), ctx_for("capstack")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "brands.yaml").write_text(
                'brands:\n  - canonical: "Capstacknick"\n    engage_slug: capstack\n    status: active\n',
                encoding="utf-8")
            (tmp / "accounts.yaml").write_text(
                'accounts:\n  - brand: "Capstacknick"\n    platform: x\n    enabled: true\n',
                encoding="utf-8")
            permissive = load_registry(tmp)
            rec = approved_record(store, ctx, permissive, "m1", "text", platform="x")
        with tempfile.TemporaryDirectory() as d2:
            tmp2 = Path(d2)
            (tmp2 / "brands.yaml").write_text(
                'brands:\n  - canonical: "Capstacknick"\n    engage_slug: capstack\n    status: active\n',
                encoding="utf-8")
            (tmp2 / "accounts.yaml").write_text(
                'accounts:\n  - brand: "Capstacknick"\n    platform: x\n    enabled: true\n'
                '    shared_account: true\n', encoding="utf-8")
            now_shared = load_registry(tmp2)
            with self.assertRaises(EngagementRejected):
                import_comment(store, ctx, now_shared, comment_row(rec.id, platform="x"))

    def test_rejects_parked_brand(self):
        from engage.approval.queue import approve as approve_draft
        from engage.approval.queue import submit
        from engage.core.models import ContentRecord, Draft, new_id
        store, ctx, registry = mem_store(), ctx_for("stowecap"), load_test_registry()
        draft = Draft(id=new_id(), brand=ctx.name, kind="original", platform="linkedin",
                      text="a clean sentence")
        draft = submit(store, ctx, draft, (), lambda p: "PASS")
        approve_draft(store, ctx, draft.id)
        rec = ContentRecord(id=new_id(), brand=ctx.name, platform="linkedin", kind="original",
                            draft_id=draft.id, lifecycle_status="approved")
        store.save_content_record(rec)
        with self.assertRaises(EngagementRejected) as cm:
            import_comment(store, ctx, registry, comment_row(rec.id, platform="linkedin"))
        self.assertIn("parked", str(cm.exception))


class TestSourceBinding(unittest.TestCase):
    """§2 — every comment ties to an exact source and, where available, a
    publication/version hash."""

    def test_comment_binds_to_content_record_and_hash(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        c = import_comment(store, ctx, registry, comment_row(rec.id))
        self.assertEqual(c.source_type, "content_record")
        self.assertEqual(c.source_id, rec.id)
        self.assertTrue(c.content_hash)
        draft_hash = store.get_draft(rec.draft_id, ctx.name).hash
        self.assertEqual(c.content_hash, draft_hash)

    def test_text_hash_set_and_deterministic(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        c = import_comment(store, ctx, registry, comment_row(rec.id))
        from engage.core.models import content_hash
        self.assertEqual(c.text_hash, content_hash(c.text))


class TestBlockedTrapAccounts(unittest.TestCase):
    """§3 — reuses the SAME blocked_accounts list scoring.py already
    enforces, not a second blocklist concept."""

    def test_blocked_author_flagged_as_risk(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        blocked_author = ctx.config["watch"]["blocked_accounts"][0]
        c = import_comment(store, ctx, registry, comment_row(rec.id, text="totally normal comment",
                                                              author=blocked_author))
        self.assertEqual(c.triage_class, "reputation_or_safety_risk")
        self.assertIn("blocklist", c.triage_reason)
        self.assertGreaterEqual(c.triage_confidence, 0.9)


class TestTriageClassificationAndConfidence(unittest.TestCase):
    """§3 — 7-way classification, every result carries confidence + reason."""

    def test_all_results_carry_confidence_and_reason(self):
        ctx = ctx_for("capstack")
        for text, author in [("nice post!", "a"), ("What does the capital stack look like?", "b"),
                             ("I'll sue you for this scam", "c"), ("interested in a partnership", "d"),
                             ("check my profile for free stuff http://x.co", "e"), ("ok", "f")]:
            cls, conf, reason = classify_extended(text, author, ctx)
            self.assertGreater(conf, 0.0)
            self.assertTrue(reason)

    def test_draftable_on_topic_question(self):
        ctx = ctx_for("capstack")
        cls, _, _ = classify_extended("What does the capital stack actually look like?", "a", ctx)
        self.assertEqual(cls, "draftable")

    def test_reputation_risk_language(self):
        ctx = ctx_for("capstack")
        cls, _, _ = classify_extended("This is a scam, I want a refund", "a", ctx)
        self.assertEqual(cls, "reputation_or_safety_risk")

    def test_possible_lead_language(self):
        ctx = ctx_for("capstack")
        cls, _, _ = classify_extended("Interested in a partnership with you", "a", ctx)
        self.assertEqual(cls, "possible_lead")

    def test_spam_language(self):
        ctx = ctx_for("capstack")
        cls, _, _ = classify_extended("check my profile for a free gift http://spam.co", "a", ctx)
        self.assertEqual(cls, "spam_or_low_value")

    def test_insufficient_context_short_ambiguous(self):
        ctx = ctx_for("capstack")
        cls, _, _ = classify_extended("what about that?", "a", ctx)
        self.assertIn(cls, ("insufficient_context", "human_needed"))  # short + question, borderline is fine either way

    def test_skip_praise(self):
        ctx = ctx_for("capstack")
        cls, _, _ = classify_extended("love this!", "a", ctx)
        self.assertEqual(cls, "skip")

    def test_no_inference_beyond_text_and_rules(self):
        # a comment mentioning money without a configured risk/lead marker
        # must not be classified as a lead or risk from "inference" alone
        ctx = ctx_for("capstack")
        cls, _, _ = classify_extended("my uncle works in finance too", "a", ctx)
        self.assertNotIn(cls, ("possible_lead", "reputation_or_safety_risk"))


class TestReplyDraftHashBindingAndTransitions(unittest.TestCase):
    """§4 — hash-bound to comment + draft text; strict review-status transitions."""

    def _draftable_comment(self, store, ctx, registry):
        rec = approved_record(store, ctx, registry, "m1", "text")
        return import_comment(store, ctx, registry, comment_row(rec.id))

    def test_reply_has_at_least_two_angles(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        comment = self._draftable_comment(store, ctx, registry)
        replies = generate_reply_review(store, ctx, registry, comment, stub)
        self.assertGreaterEqual(len(replies), 2)
        self.assertEqual(len({r.angle for r in replies}), len(replies))  # distinct angles

    def test_reply_bound_to_comment_and_draft_hashes(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        comment = self._draftable_comment(store, ctx, registry)
        replies = generate_reply_review(store, ctx, registry, comment, stub)
        from engage.core.models import content_hash
        for r in replies:
            self.assertEqual(r.comment_text_hash, comment.text_hash)
            self.assertEqual(r.draft_text_hash, content_hash(r.draft_text))
            self.assertTrue(r.brand_strategy_source)
            self.assertTrue(r.generated_at)

    def test_only_draftable_comments_get_replies(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        comment = import_comment(store, ctx, registry, comment_row(rec.id, text="love this!!"))
        self.assertEqual(comment.triage_class, "skip")
        with self.assertRaises(EngagementRejected):
            generate_reply_review(store, ctx, registry, comment, stub)

    def test_illegal_transition_raises(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        comment = self._draftable_comment(store, ctx, registry)
        reply = generate_reply_review(store, ctx, registry, comment, stub)[0]
        # review_required's legal targets are approved_for_manual_posting (via
        # the dedicated function only), rejected, paused, expired — NOT "draft"
        with self.assertRaises(EngagementRejected):
            transition_reply(store, reply, "draft")

    def test_approved_status_only_reachable_via_dedicated_function(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        comment = self._draftable_comment(store, ctx, registry)
        reply = generate_reply_review(store, ctx, registry, comment, stub)[0]
        with self.assertRaises(EngagementRejected) as cm:
            transition_reply(store, reply, "approved_for_manual_posting")
        self.assertIn("approve_for_manual_posting", str(cm.exception))

    def test_approve_requires_nonempty_approver(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        comment = self._draftable_comment(store, ctx, registry)
        reply = generate_reply_review(store, ctx, registry, comment, stub)[0]
        with self.assertRaises(EngagementRejected):
            approve_for_manual_posting(store, reply, "")

    def test_approve_succeeds_with_approver(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        comment = self._draftable_comment(store, ctx, registry)
        reply = generate_reply_review(store, ctx, registry, comment, stub)[0]
        approved = approve_for_manual_posting(store, reply, "nick")
        self.assertEqual(approved.review_status, "approved_for_manual_posting")

    def test_all_declared_states_are_real(self):
        for state in ("draft", "review_required", "approved_for_manual_posting",
                     "rejected", "expired", "paused"):
            self.assertIn(state, REPLY_REVIEW_TRANSITIONS)

    def test_source_comment_change_expires_stale_replies(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        c1 = import_comment(store, ctx, registry, comment_row(rec.id, external_id="ext-1"))
        reply = generate_reply_review(store, ctx, registry, c1, stub)[0]
        self.assertEqual(reply.review_status, "review_required")
        # re-import same external id, DIFFERENT text -> must expire the old reply
        import_comment(store, ctx, registry, comment_row(
            rec.id, text="What does the capital stack look like on a bigger deal?", external_id="ext-1"))
        refreshed = store.get_reply_draft(reply.id)
        self.assertEqual(refreshed.review_status, "expired")

    def test_gate_failure_lands_reply_as_rejected(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        comment = import_comment(store, ctx, registry, comment_row(rec.id))

        def bad_backend(prompt):
            if "'PASS' or 'BLOCK" in prompt:
                return "PASS"
            return "We offer a guaranteed return on every deal."  # trips capstack's regex gate

        replies = generate_reply_review(store, ctx, registry, comment, bad_backend)
        self.assertTrue(replies)
        self.assertTrue(all(r.review_status == "rejected" for r in replies))
        self.assertTrue(all(r.gate_reasons for r in replies))


class TestDuplicateIdempotency(unittest.TestCase):
    """§8 — duplicate comment and duplicate reply-draft idempotency."""

    def test_reimporting_identical_comment_is_a_noop(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        c1 = import_comment(store, ctx, registry, comment_row(rec.id, external_id="ext-1"))
        c2 = import_comment(store, ctx, registry, comment_row(rec.id, external_id="ext-1"))
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(len(store.list_comments(ctx.name)), 1)

    def test_regenerating_reply_for_same_comment_version_is_idempotent(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        comment = import_comment(store, ctx, registry, comment_row(rec.id))
        first = generate_reply_review(store, ctx, registry, comment, stub)
        second = generate_reply_review(store, ctx, registry, comment, stub)
        self.assertEqual({r.id for r in first}, {r.id for r in second})
        self.assertEqual(len(store.list_reply_drafts(ctx.name, comment_id=comment.id)), len(first))

    def test_batch_import_continues_after_one_bad_row(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        rows = [comment_row("no-such-record", external_id="bad"),
               comment_row(rec.id, external_id="good")]
        accepted, rejected = import_comments_batch(store, ctx, registry, rows)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)


class TestLeadAndRiskRouting(unittest.TestCase):
    """§5 — non-binding CEO escalations; partnership leads escalate,
    general commercial-interest leads do not (aggregate only)."""

    def test_risk_comment_always_escalates(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        c = import_comment(store, ctx, registry, comment_row(rec.id, text="this is a scam, refund me"))
        self.assertEqual(c.triage_class, "reputation_or_safety_risk")
        esc = route_risk_signal(store, ctx, registry, c)
        self.assertEqual(esc["trigger"], "brand_or_reputation_risk")
        self.assertIn("NONE", esc["authorization"])
        self.assertIn(c.id, esc["traces_to"])

    def test_partnership_lead_escalates(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        c = import_comment(store, ctx, registry, comment_row(rec.id, text="Would love to explore a partnership"))
        self.assertEqual(c.triage_class, "possible_lead")
        esc = route_lead_signal(store, ctx, registry, c)
        self.assertIsNotNone(esc)
        self.assertEqual(esc["trigger"], "partnership")

    def test_general_commercial_lead_does_not_escalate_individually(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        c = import_comment(store, ctx, registry, comment_row(rec.id, text="what's the price on this?"))
        self.assertEqual(c.triage_class, "possible_lead")
        esc = route_lead_signal(store, ctx, registry, c)
        self.assertIsNone(esc)

    def test_routing_wrong_class_raises(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        c = import_comment(store, ctx, registry, comment_row(rec.id, text="love this!!"))
        with self.assertRaises(EngagementRejected):
            route_risk_signal(store, ctx, registry, c)


class TestPrivacyMinimizedPerformanceHandoff(unittest.TestCase):
    """§6 — aggregate only; never raw text/author; single comment never
    becomes validated learning."""

    def test_insights_never_include_raw_text_or_author(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        import_comment(store, ctx, registry, comment_row(
            rec.id, text="my secret account handle is @realnick123", author="jane_the_lead"))
        rec_dict = emit_engagement_insights(store, ctx, registry)
        payload_str = str(rec_dict["evidence"])
        self.assertNotIn("realnick123", payload_str)
        self.assertNotIn("jane_the_lead", payload_str)

    def test_single_comment_is_not_validated_learning(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        import_comment(store, ctx, registry, comment_row(rec.id))
        result = emit_engagement_insights(store, ctx, registry)
        self.assertNotEqual(result["label"], "validated_learning")

    def test_mixed_categories_never_become_validated_learning_regardless_of_volume(self):
        # A real bug caught here: labeling the BARE comment count (not a
        # recurring pattern) validated_learning once count>=threshold, even
        # with no majority category — "we triaged N comments" is activity,
        # not a claim about anything. Three distinct categories, no
        # majority, must never be called validated_learning no matter how
        # many total comments there are.
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        texts_authors = [("love this!!", "a"), ("This is a scam", "b"),
                         ("Interested in a partnership", "c")]
        for i, (text, author) in enumerate(texts_authors):
            import_comment(store, ctx, registry, comment_row(
                rec.id, text=text, author=author, external_id=f"ext-{i}"))
        result = emit_engagement_insights(store, ctx, registry)
        self.assertNotEqual(result["label"], "validated_learning")

    def test_dominant_majority_category_can_reach_validated_learning(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        for i in range(4):
            import_comment(store, ctx, registry, comment_row(
                rec.id, text="love this so much!!", author=f"fan{i}", external_id=f"ext-{i}"))
        result = emit_engagement_insights(store, ctx, registry)
        self.assertEqual(result["evidence"]["dominant_pattern"]["class"], "skip")
        self.assertEqual(result["label"], "validated_learning")

    def test_insights_reuses_performance_recommendation_channel(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        import_comment(store, ctx, registry, comment_row(rec.id))
        result = emit_engagement_insights(store, ctx, registry)
        self.assertEqual(result["target"], "performance")
        recs = store.list_recommendations(ctx.name, target="performance")
        self.assertTrue(any(r["id"] == result["id"] for r in recs))


class TestNoDMEndpointsNoExternalAction(unittest.TestCase):
    """§1 — DESIGN.md's DM exclusion unchanged; no browser/network/posting
    capability anywhere in engagement/."""

    def test_no_dm_endpoint_field_or_function_exists(self):
        # The docstrings legitimately SAY "no DMs" in prose (same as Phase 4's
        # "does NOT write to memory/" disclaimer) — that's not a violation.
        # What must never appear is an actual DM-shaped endpoint: a function
        # named for sending one, or a data field carrying a DM id/thread.
        forbidden_code_shapes = ("def send_dm", "def dm_", "dm_endpoint", "dm_id",
                                 "dm_thread", "\"dm\":", "'dm':", "direct_message_id")
        for f in ("agent.py", "reports.py", "triage_ext.py"):
            src = (PKG_DIR / "engagement" / f).read_text(encoding="utf-8").lower()
            for shape in forbidden_code_shapes:
                self.assertNotIn(shape, src, f"{f} contains a DM-shaped construct: {shape!r}")

    def test_no_write_capable_or_network_imports(self):
        forbidden = ("from ..publish", "webbrowser", "requests.", "urllib.request",
                     "selenium", "playwright", "post_comment", "send_comment", "send_dm",
                     "like_post", "follow_account", "unfollow_account")
        for f in ("agent.py", "reports.py", "triage_ext.py"):
            src = (PKG_DIR / "engagement" / f).read_text(encoding="utf-8")
            for term in forbidden:
                self.assertNotIn(term, src, f"{f} references {term!r}")

    def test_no_credential_handling(self):
        for f in ("agent.py", "reports.py", "triage_ext.py"):
            src = (PKG_DIR / "engagement" / f).read_text(encoding="utf-8").lower()
            for term in ("password", "oauth_token", "api_key", "cookie", "mfa_code"):
                self.assertNotIn(term, src, f"{f} references {term!r}")

    def test_queue_item_states_manual_posting_explicitly(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        comment = import_comment(store, ctx, registry, comment_row(rec.id))
        item = render_review_queue_item(store, ctx, registry, comment)
        self.assertIn("DRAFTS FOR HUMAN REVIEW ONLY", item["instruction"])
        self.assertIn("YOU must post it", item["instruction"])
        self.assertGreaterEqual(len(item["manual_posting_checklist"]), 3)


class TestReportSeparation(unittest.TestCase):
    """§6/§7 — brands separate, parked shown as parked."""

    def _sections(self):
        registry = load_test_registry()
        return [(ctx_for(slug), mem_store(), registry) for slug in ("empires", "capstack", "stowecap")]

    def test_weekly_shows_brands_separately(self):
        report = build_engagement_weekly_summary(self._sections(), now=NOW)
        self.assertIn("## EMPIRES", report)
        self.assertIn("## CAPSTACK", report)

    def test_weekly_shows_parked_brand_as_parked(self):
        report = build_engagement_weekly_summary(self._sections(), now=NOW)
        self.assertIn("PARKED", report)

    def test_weekly_states_review_only_mode(self):
        report = build_engagement_weekly_summary(self._sections(), now=NOW)
        self.assertIn("review-only", report)
        self.assertIn("draft awaiting your manual posting", report)

    def test_daily_exceptions_silent_when_nothing_wrong(self):
        report = build_engagement_daily_exceptions(
            [(ctx_for("capstack"), mem_store(), load_test_registry())], now=NOW)
        self.assertIn("nothing action-worthy", report)

    def test_daily_exceptions_excludes_parked_brand(self):
        report = build_engagement_daily_exceptions(
            [(ctx_for("stowecap"), mem_store(), load_test_registry())], now=NOW)
        self.assertNotIn("STOWECAP", report)

    def test_daily_exceptions_surfaces_risk(self):
        store, ctx, registry = mem_store(), ctx_for("capstack"), load_test_registry()
        rec = approved_record(store, ctx, registry, "m1", "text")
        import_comment(store, ctx, registry, comment_row(rec.id, text="this is a scam, refund me"))
        report = build_engagement_daily_exceptions([(ctx, store, registry)], now=NOW)
        self.assertIn("REPUTATION/SAFETY RISK", report)


class TestScheduledTaskUntouched(unittest.TestCase):
    def test_scheduled_task_still_forbids_posting(self):
        task = Path.home() / ".claude" / "scheduled-tasks" / "empires-egos-daily-review" / "SKILL.md"
        if not task.exists():
            self.skipTest("scheduled task not present in this environment")
        self.assertIn("Do NOT post anything", task.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
