"""Original-content briefs, drafting, and lifecycle — Phase 1
(drafting/originals.py, the module named but never built in DESIGN.md §1).
Registry fixtures live in fixtures/registry/ so this suite is fully offline
and independent of the real ~/.claude/harness/social/registry/ path."""
import unittest

from engage.config.registry import RegistryError, load_registry
from engage.core.models import LIFECYCLE_STATES, LifecycleError
from engage.drafting.llm import PromptAssemblyError
from engage.drafting.originals import (
    approve_content_record,
    create_brief,
    create_original_content,
    next_pillar,
    repurpose_for_registry,
    transition,
    validate_brief,
    write_handoff,
)

from .helpers import FIXTURES, ctx_for, mem_store

REGISTRY_DIR = FIXTURES / "registry"

MATERIAL = {
    "id": "src1",
    "title": "Draft source",
    "text": "The lender required a completion guarantee before closing.",
    "kind": "note",
}


def load_test_registry():
    return load_registry(REGISTRY_DIR)


def _is_gate_judge_prompt(prompt: str) -> bool:
    # safety/gates.py's own judge-prompt template asks explicitly for this
    # exact phrasing — a real LLM backend answers it contextually; these
    # stubs must too, since submit() reuses the same callable for both
    # generation and gate-judging (matching cli.py's own convention).
    return "'PASS' or 'BLOCK" in prompt


def original_stub(prompt: str) -> str:
    if _is_gate_judge_prompt(prompt):
        return "PASS"
    return ("A cold-open paradox hook that earns the scroll.\n"
            "Full caption body follows in brand voice.")


def repurpose_stub(prompt: str) -> str:
    if _is_gate_judge_prompt(prompt):
        return "PASS"
    platform = next((ln.split(":", 1)[1].strip() for ln in prompt.splitlines()
                     if ln.startswith("Target platform:")), "?")
    return f"Native cut for {platform}, no figures invented here."


def blocked_stub_for(ctx):
    """Text guaranteed to trip that brand's own regex gate, deterministically
    — the regex layer runs before the LLM judge, so the judge branch here is
    unreachable in practice but kept for a realistic dual-purpose callable."""
    text = {
        "capstack": "We offer a guaranteed return on every deal.",
        "empires": "Trump would have loved this empire's rise.",
    }[ctx.name]

    def stub(prompt: str) -> str:
        return "PASS" if _is_gate_judge_prompt(prompt) else text
    return stub


def seed_material(store):
    store.add_material(MATERIAL["id"], MATERIAL["title"], MATERIAL["text"], MATERIAL["kind"])
    return store.get_material(MATERIAL["id"])


class TestRegistry(unittest.TestCase):
    def test_loads_fixture(self):
        reg = load_test_registry()
        self.assertEqual(reg.slugs(), {"empires", "capstack", "stowecap"})

    def test_missing_registry_dir_raises(self):
        with self.assertRaises(RegistryError):
            load_registry(FIXTURES / "no-such-registry-dir")

    def test_canonical_name_and_parked_status(self):
        reg = load_test_registry()
        self.assertEqual(reg.canonical_name("capstack"), "Capstacknick")
        self.assertFalse(reg.is_parked("capstack"))
        self.assertTrue(reg.is_parked("stowecap"))

    def test_enabled_platforms_does_not_leak_across_brands(self):
        reg = load_test_registry()
        # Stowecap owns linkedin in the fixture; capstack has no linkedin row
        # at all. A canonical-name mismatch in the lookup would leak it in.
        self.assertNotIn("linkedin", reg.enabled_platforms("capstack"))
        self.assertIn("linkedin", reg.enabled_platforms("stowecap"))

    def test_enabled_platforms_excludes_disabled_rows(self):
        reg = load_test_registry()
        # capstack's own ENGAGE config lists tiktok and x as platforms, but
        # the registry fixture marks both enabled: false.
        self.assertNotIn("tiktok", reg.enabled_platforms("capstack"))
        self.assertNotIn("x", reg.enabled_platforms("capstack"))
        self.assertIn("instagram", reg.enabled_platforms("capstack"))


class TestBriefValidation(unittest.TestCase):
    def setUp(self):
        self.registry = load_test_registry()

    def _brief(self, **overrides):
        brief = {
            "brand": "capstack", "audience": "first-time GPs", "objective": "teach the mechanism",
            "core_insight": "the completion guarantee is the real risk transfer",
            "platform": "instagram", "format": "reel", "hook": "A real hook line.",
            "cta": "Comment GAP", "brand_voice": "brands/capstack/prompts/voice.md",
            "lifecycle_status": "approved",
        }
        brief.update(overrides)
        return brief

    def test_complete_and_approved_passes(self):
        v = validate_brief(self._brief(), self.registry)
        self.assertEqual(v.code, 0)
        self.assertTrue(v.passed)

    def test_missing_field_is_incomplete(self):
        brief = self._brief()
        del brief["hook"]
        v = validate_brief(brief, self.registry)
        self.assertEqual(v.code, 1)
        self.assertIn("hook", v.problems[0])

    def test_placeholder_value_is_incomplete(self):
        v = validate_brief(self._brief(cta="TBD"), self.registry)
        self.assertEqual(v.code, 1)
        self.assertIn("placeholder", v.problems[0])

    def test_unknown_brand_is_incomplete(self):
        v = validate_brief(self._brief(brand="not-a-brand"), self.registry)
        self.assertEqual(v.code, 1)

    def test_not_yet_approved_is_code_2(self):
        v = validate_brief(self._brief(lifecycle_status="review_required"), self.registry)
        self.assertEqual(v.code, 2)
        self.assertEqual(v.status, "not_approved")

    def test_require_brand_mismatch_is_code_3(self):
        v = validate_brief(self._brief(), self.registry, require_brand="empires")
        self.assertEqual(v.code, 3)
        self.assertEqual(v.status, "mismatch")

    def test_require_platform_mismatch_is_code_3(self):
        v = validate_brief(self._brief(), self.registry, require_platform="tiktok")
        self.assertEqual(v.code, 3)
        self.assertEqual(v.status, "mismatch")


class TestLifecycleTransitions(unittest.TestCase):
    def setUp(self):
        self.store = mem_store()
        self.ctx = ctx_for("capstack")
        self.material = seed_material(self.store)
        self.registry = load_test_registry()

    def _record(self, status="draft"):
        record = create_original_content(
            self.store, self.ctx, self.registry, self.material, "instagram", original_stub,
            audience="a", objective="o", cta="c",
        )
        record.lifecycle_status = status
        self.store.save_content_record(record)
        return record

    def test_legal_transition(self):
        record = self._record("draft")
        transition(self.store, record, "cancelled")
        self.assertEqual(record.lifecycle_status, "cancelled")

    def test_illegal_transition_raises(self):
        record = self._record("draft")
        with self.assertRaises(LifecycleError):
            transition(self.store, record, "verified")

    def test_approved_is_refused_via_generic_transition(self):
        record = self._record("review_required")
        with self.assertRaises(LifecycleError) as cm:
            transition(self.store, record, "approved")
        self.assertIn("approve_content_record", str(cm.exception))

    def test_terminal_state_has_no_outbound_transitions(self):
        record = self._record("rejected")
        with self.assertRaises(LifecycleError):
            transition(self.store, record, "review_required")

    def test_all_states_are_declared(self):
        for state in ("draft", "review_required", "approved", "scheduled", "publishing",
                     "published", "verified", "paused", "failed", "rejected", "cancelled"):
            self.assertIn(state, LIFECYCLE_STATES)


class TestApprovalRequirement(unittest.TestCase):
    def setUp(self):
        self.store = mem_store()
        self.ctx = ctx_for("capstack")
        self.material = seed_material(self.store)
        self.registry = load_test_registry()

    def test_approval_requires_nonempty_approver(self):
        record = create_original_content(
            self.store, self.ctx, self.registry, self.material, "instagram", original_stub,
            audience="a", objective="o", cta="c",
        )
        self.assertEqual(record.lifecycle_status, "review_required")
        with self.assertRaises(LifecycleError):
            approve_content_record(self.store, self.ctx, record, approver="")

    def test_approval_requires_review_required_status(self):
        record = create_original_content(
            self.store, self.ctx, self.registry, self.material, "instagram", original_stub,
            audience="a", objective="o", cta="c",
        )
        record.lifecycle_status = "draft"
        self.store.save_content_record(record)
        with self.assertRaises(LifecycleError):
            approve_content_record(self.store, self.ctx, record, approver="nick")

    def test_approval_succeeds_and_binds_the_underlying_draft(self):
        record = create_original_content(
            self.store, self.ctx, self.registry, self.material, "instagram", original_stub,
            audience="a", objective="o", cta="c",
        )
        approved = approve_content_record(self.store, self.ctx, record, approver="nick",
                                          note="looks right")
        self.assertEqual(approved.lifecycle_status, "approved")
        draft = self.store.get_draft(approved.draft_id, self.ctx.name)
        self.assertEqual(draft.status, "APPROVED")
        events = [e["event_type"] for e in self.store.list_content_events(record.id)]
        self.assertIn("approval_recorded", events)

    def test_gates_blocked_content_is_rejected_and_never_approvable(self):
        record = create_original_content(
            self.store, self.ctx, self.registry, self.material, "instagram",
            blocked_stub_for(self.ctx), audience="a", objective="o", cta="c",
        )
        self.assertEqual(record.lifecycle_status, "rejected")
        with self.assertRaises(LifecycleError):
            approve_content_record(self.store, self.ctx, record, approver="nick")


class TestBrandIsolationInOriginals(unittest.TestCase):
    def setUp(self):
        self.registry = load_test_registry()

    def test_refuses_platform_not_eligible_for_brand(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        material = seed_material(store)
        # tiktok is in capstack's own platforms list but disabled in the registry
        with self.assertRaises(ValueError):
            create_original_content(store, ctx, self.registry, material, "tiktok",
                                    original_stub, audience="a", objective="o", cta="c")

    def test_parked_brand_refused_without_opt_in(self):
        store = mem_store()
        ctx = ctx_for("stowecap")
        material = seed_material(store)
        with self.assertRaises(ValueError):
            create_original_content(store, ctx, self.registry, material, "linkedin",
                                    original_stub, audience="a", objective="o", cta="c")

    def test_parked_brand_with_opt_in_still_fails_closed_on_missing_prompt(self):
        # stowecap has no prompts/original.md (deliberate stub, see
        # harness/social/BRIEFING.md) — even with allow_parked=True this
        # must fail closed, mirroring test_repurpose's
        # test_brand_without_repurpose_prompt_fails_closed.
        store = mem_store()
        ctx = ctx_for("stowecap")
        material = seed_material(store)
        with self.assertRaises(PromptAssemblyError):
            create_original_content(store, ctx, self.registry, material, "linkedin",
                                    original_stub, audience="a", objective="o", cta="c",
                                    allow_parked=True)


class TestPlatformNativeRepurposing(unittest.TestCase):
    def test_fans_out_only_to_eligible_platforms(self):
        store = mem_store()
        ctx = ctx_for("empires")
        registry = load_test_registry()
        material = seed_material(store)
        fanned = repurpose_for_registry(store, ctx, registry, material, repurpose_stub, "instagram")
        self.assertEqual({r.platform for r in fanned}, {"tiktok", "youtube"})
        for r in fanned:
            self.assertEqual(r.kind, "repurpose")
            self.assertEqual(r.lifecycle_status, "review_required")

    def test_no_fan_out_when_no_other_platform_is_enabled(self):
        # capstack's only registry-enabled AND brand-configured platform is
        # instagram — the primary — so nothing else should be drafted.
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        fanned = repurpose_for_registry(store, ctx, registry, material, repurpose_stub, "instagram")
        self.assertEqual(fanned, [])

    def test_does_not_naively_copy_text_between_platforms(self):
        store = mem_store()
        ctx = ctx_for("empires")
        registry = load_test_registry()
        material = seed_material(store)
        fanned = repurpose_for_registry(store, ctx, registry, material, repurpose_stub, "instagram")
        texts = {r.platform: store.get_draft(r.draft_id, ctx.name).text for r in fanned}
        self.assertIn("tiktok", texts["tiktok"])
        self.assertIn("youtube", texts["youtube"])
        self.assertNotEqual(texts["tiktok"], texts["youtube"])


class TestPillarBalance(unittest.TestCase):
    def test_next_pillar_prefers_least_used(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        first_pillar = next_pillar(store, ctx)
        self.assertTrue(first_pillar)  # capstack has content_pillars configured
        create_original_content(store, ctx, registry, material, "instagram", original_stub,
                                audience="a", objective="o", cta="c", pillar=first_pillar)
        self.assertNotEqual(next_pillar(store, ctx), first_pillar)


class TestHandoffs(unittest.TestCase):
    def test_handoff_is_recorded_not_sent(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        record = approve_content_record(store, ctx, record, approver="nick")
        payload = write_handoff(store, record, "publishing", note="ready for scheduling")
        self.assertEqual(payload["target"], "publishing")
        self.assertEqual(payload["content_record_id"], record.id)
        events = store.list_content_events(record.id)
        self.assertTrue(any(e["event_type"] == "handoff:publishing" for e in events))

    def test_unknown_target_rejected(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        with self.assertRaises(ValueError):
            write_handoff(store, record, "not-a-real-target")


if __name__ == "__main__":
    unittest.main()
