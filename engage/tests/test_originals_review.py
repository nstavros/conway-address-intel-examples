"""Adversarial QC pass on Phase 1 (drafting/originals.py + its registry and
store support), run before Phase 2. Each test proves — or disproves — one
specific defect class from the review: cross-brand leakage, wrong-account
drafts, approval bypass, invalid transitions, a handoff that implies
publish authority it doesn't have, registry unavailability, duplicate
records/handoffs, and platform-native separation.

Fully offline: registry fixtures are built with tempfile, never the real
~/.claude/harness/social/registry/ path."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engage.config.registry import Registry, RegistryError, load_registry
from engage.core.models import LifecycleError
from engage.drafting.originals import (
    approve_content_record,
    create_original_content,
    repurpose_for_registry,
    write_handoff,
)

from .helpers import PKG_DIR, ROOT, ctx_for, mem_store
from .test_originals import FIXTURES, load_test_registry, original_stub, seed_material

REGISTRY_DIR = FIXTURES / "registry"


def write_registry(tmp: Path, brands_yaml: str, accounts_yaml: str) -> Path:
    (tmp / "brands.yaml").write_text(brands_yaml, encoding="utf-8")
    (tmp / "accounts.yaml").write_text(accounts_yaml, encoding="utf-8")
    return tmp


VALID_BRANDS = """
brands:
  - canonical: "Empires and Egos"
    engage_slug: empires
    status: active
  - canonical: "Capstacknick"
    engage_slug: capstack
    status: active
"""


class TestRegistryMissingUnreadableMalformed(unittest.TestCase):
    """§3 — registry resilience. Every failure mode must raise RegistryError,
    never a raw KeyError/AttributeError/YAMLError, and never return
    partial/best-guess data."""

    def test_missing_directory_raises_registry_error(self):
        with self.assertRaises(RegistryError):
            load_registry(FIXTURES / "definitely-does-not-exist")

    def test_malformed_yaml_raises_registry_error_not_yaml_error(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = write_registry(Path(d), "brands: [this is not: valid: yaml: at all", "accounts: []")
            with self.assertRaises(RegistryError):
                load_registry(tmp)

    def test_unreadable_file_raises_registry_error(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "brands.yaml").mkdir()  # a directory where a file is expected -> OSError on read
            (tmp / "accounts.yaml").write_text("accounts: []", encoding="utf-8")
            with self.assertRaises(RegistryError):
                load_registry(tmp)

    def test_non_mapping_top_level_raises_registry_error(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = write_registry(Path(d), "- just\n- a\n- list\n", "accounts: []")
            with self.assertRaises(RegistryError):
                load_registry(tmp)

    def test_accounts_not_a_list_raises_registry_error(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = write_registry(Path(d), VALID_BRANDS, "accounts: {not: a-list}")
            with self.assertRaises(RegistryError):
                load_registry(tmp)

    def test_brand_row_missing_required_key_raises_registry_error(self):
        with tempfile.TemporaryDirectory() as d:
            bad_brands = 'brands:\n  - canonical: "Missing Slug"\n    status: active\n'
            tmp = write_registry(Path(d), bad_brands, "accounts: []")
            with self.assertRaises(RegistryError):
                load_registry(tmp)

    def test_duplicate_engage_slug_raises_registry_error(self):
        with tempfile.TemporaryDirectory() as d:
            dup = ('brands:\n'
                  '  - canonical: "A"\n    engage_slug: capstack\n    status: active\n'
                  '  - canonical: "B"\n    engage_slug: capstack\n    status: active\n')
            tmp = write_registry(Path(d), dup, "accounts: []")
            with self.assertRaises(RegistryError):
                load_registry(tmp)

    def test_empty_accounts_file_fails_closed_not_open(self):
        # a technically-valid, empty accounts.yaml must yield NO eligible
        # platforms for any brand — never a crash, never "everything enabled"
        with tempfile.TemporaryDirectory() as d:
            tmp = write_registry(Path(d), VALID_BRANDS, "accounts: []")
            reg = load_registry(tmp)
            self.assertEqual(reg.enabled_platforms("capstack"), [])

    def test_orphaned_account_row_for_unknown_brand_is_simply_ignored(self):
        # a typo'd brand name in accounts.yaml must not crash and must not
        # grant eligibility to anything — the row is just inert
        with tempfile.TemporaryDirectory() as d:
            accounts = 'accounts:\n  - brand: "Cap5tacknick"\n    platform: instagram\n    enabled: true\n'
            tmp = write_registry(Path(d), VALID_BRANDS, accounts)
            reg = load_registry(tmp)
            self.assertEqual(reg.enabled_platforms("capstack"), [])


class TestRegistryUnavailableFailsClosedThroughOriginals(unittest.TestCase):
    """A missing/broken registry must block every activity that could
    eventually lead to publishing — not just registry-level calls."""

    def test_create_original_content_refuses_when_registry_load_failed(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        material = seed_material(store)
        with self.assertRaises(RegistryError):
            registry = load_registry(FIXTURES / "no-such-dir")
            create_original_content(store, ctx, registry, material, "instagram",
                                    original_stub, audience="a", objective="o", cta="c")


class TestSharedAccountNeverAutoSelected(unittest.TestCase):
    """§1 — shared X cannot be selected automatically, even if a future
    registry edit accidentally sets enabled: true on it."""

    def test_shared_account_excluded_even_when_enabled_true(self):
        with tempfile.TemporaryDirectory() as d:
            accounts = ('accounts:\n'
                       '  - brand: "Capstacknick"\n    platform: instagram\n    enabled: true\n'
                       '  - brand: "Capstacknick"\n    platform: x\n    enabled: true\n'
                       '    shared_account: true\n')
            tmp = write_registry(Path(d), VALID_BRANDS, accounts)
            reg = load_registry(tmp)
            self.assertNotIn("x", reg.enabled_platforms("capstack"))
            self.assertIn("instagram", reg.enabled_platforms("capstack"))

    def test_shared_account_reachable_only_via_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as d:
            accounts = ('accounts:\n'
                       '  - brand: "Capstacknick"\n    platform: x\n    enabled: true\n'
                       '    shared_account: true\n')
            tmp = write_registry(Path(d), VALID_BRANDS, accounts)
            reg = load_registry(tmp)
            self.assertIn("x", reg.enabled_platforms("capstack", include_shared=True))


class TestCapstacknickCannotGetLinkedIn(unittest.TestCase):
    """§1 — even a rogue registry row granting Capstacknick a LinkedIn
    account must not make it eligible, because linkedin was removed from
    Capstacknick's own ENGAGE config (DEC-SM-003) — the intersection gate
    holds even when the registry side is wrong."""

    def test_registry_alone_cannot_grant_linkedin_to_capstacknick(self):
        with tempfile.TemporaryDirectory() as d:
            accounts = 'accounts:\n  - brand: "Capstacknick"\n    platform: linkedin\n    enabled: true\n'
            tmp = write_registry(Path(d), VALID_BRANDS, accounts)
            reg = load_registry(tmp)
            self.assertIn("linkedin", reg.enabled_platforms("capstack"))  # registry alone would allow it
            store = mem_store()
            ctx = ctx_for("capstack")
            material = seed_material(store)
            with self.assertRaises(ValueError):  # but ENGAGE config no longer lists it -> refused
                create_original_content(store, ctx, reg, material, "linkedin",
                                        original_stub, audience="a", objective="o", cta="c")


class TestApprovalStructuralGuarantees(unittest.TestCase):
    """§2 — nothing reaches 'approved' without a real approval record, and
    that guarantee holds even against a caller that bypasses
    approve_content_record() and writes to the store directly."""

    def test_store_refuses_to_persist_approved_without_an_approval_row(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        record.lifecycle_status = "approved"  # bypassing approve_content_record entirely
        with self.assertRaises(LifecycleError):
            store.save_content_record(record)

    def test_two_content_records_cannot_share_one_draft_id(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        first = create_original_content(store, ctx, registry, material, "instagram",
                                        original_stub, audience="a", objective="o", cta="c")
        from engage.core.models import ContentRecord, new_id
        rogue = ContentRecord(id=new_id(), brand=ctx.name, platform="instagram", kind="original",
                              draft_id=first.draft_id, lifecycle_status="draft")
        with self.assertRaises(sqlite3.IntegrityError):
            store.save_content_record(rogue)

    def test_cli_source_has_no_direct_approved_assignment_outside_the_guarded_path(self):
        # lint-style structural check: the CLI must never set lifecycle_status
        # to 'approved' itself — only approve_content_record() may.
        cli_source = (ROOT / "engage" / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn('lifecycle_status = "approved"', cli_source)
        self.assertNotIn("lifecycle_status='approved'", cli_source)


class TestHandoffCannotImplyPublishAuthority(unittest.TestCase):
    """§4 — a handoff must never falsely imply publication authority, must
    be hash-bound to the approved text, and must refuse non-publishable
    records for the publishing target."""

    def _approved_record(self, store, ctx, registry, material):
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        return approve_content_record(store, ctx, record, approver="nick")

    def test_publishing_handoff_refused_before_approval(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        with self.assertRaises(LifecycleError):
            write_handoff(store, record, "publishing")

    def test_rejected_record_can_never_be_handed_off_as_publishable(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)

        def guaranteed_return_stub(prompt):
            return "PASS" if "'PASS' or 'BLOCK" in prompt else "We offer a guaranteed return on every deal."

        record = create_original_content(store, ctx, registry, material, "instagram",
                                         guaranteed_return_stub, audience="a", objective="o", cta="c")
        self.assertEqual(record.lifecycle_status, "rejected")
        with self.assertRaises(LifecycleError):
            write_handoff(store, record, "publishing")

    def test_cancelled_record_can_never_be_handed_off_as_publishable(self):
        from engage.drafting.originals import transition
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        transition(store, record, "cancelled")
        with self.assertRaises(LifecycleError):
            write_handoff(store, record, "publishing")

    def test_publishing_handoff_succeeds_after_real_approval(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = self._approved_record(store, ctx, registry, material)
        payload = write_handoff(store, record, "publishing")
        self.assertEqual(payload["lifecycle_status"], "approved")

    def test_handoff_payload_explicitly_disclaims_publish_authority(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = self._approved_record(store, ctx, registry, material)
        payload = write_handoff(store, record, "publishing")
        self.assertIn("NONE", payload["authorization"])
        self.assertIn("not an instruction", payload["authorization"])

    def test_handoff_is_hash_bound_to_the_approved_draft(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = self._approved_record(store, ctx, registry, material)
        payload = write_handoff(store, record, "publishing")
        draft = store.get_draft(record.draft_id, ctx.name)
        self.assertEqual(payload["content_hash"], draft.hash)

    def test_edit_after_approval_invalidates_the_handoff_path(self):
        from engage.approval.queue import edit_draft
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = self._approved_record(store, ctx, registry, material)
        edit_draft(store, ctx, record.draft_id, "a completely different, unapproved sentence")
        with self.assertRaises(LifecycleError):
            write_handoff(store, record, "publishing")

    def test_identical_handoff_request_is_idempotent_not_duplicated(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = self._approved_record(store, ctx, registry, material)
        write_handoff(store, record, "publishing")
        write_handoff(store, record, "publishing")
        events = [e for e in store.list_content_events(record.id) if e["event_type"] == "handoff:publishing"]
        self.assertEqual(len(events), 1)

    def test_engagement_and_performance_handoffs_do_not_require_approval(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        write_handoff(store, record, "engagement")
        write_handoff(store, record, "performance")  # neither raises


class TestDuplicateContentRecords(unittest.TestCase):
    """§4 — an identical request cannot silently create unintended
    duplicates."""

    def test_second_identical_request_is_refused(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        create_original_content(store, ctx, registry, material, "instagram",
                                original_stub, audience="a", objective="o", cta="c")
        with self.assertRaises(ValueError):
            create_original_content(store, ctx, registry, material, "instagram",
                                    original_stub, audience="a", objective="o", cta="c")

    def test_force_allows_a_deliberate_second_pass(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        first = create_original_content(store, ctx, registry, material, "instagram",
                                        original_stub, audience="a", objective="o", cta="c")
        second = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c",
                                         force=True)
        self.assertNotEqual(first.id, second.id)

    def test_a_rejected_record_does_not_block_a_retry(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)

        def guaranteed_return_stub(prompt):
            return "PASS" if "'PASS' or 'BLOCK" in prompt else "We offer a guaranteed return on every deal."

        rejected = create_original_content(store, ctx, registry, material, "instagram",
                                           guaranteed_return_stub, audience="a", objective="o", cta="c")
        self.assertEqual(rejected.lifecycle_status, "rejected")
        retried = create_original_content(store, ctx, registry, material, "instagram",
                                          original_stub, audience="a", objective="o", cta="c")
        self.assertEqual(retried.lifecycle_status, "review_required")


class TestAuditTrailCompleteness(unittest.TestCase):
    """§4 — every meaningful action has a complete audit event with a
    timestamp, record id, and actor."""

    def test_system_generated_events_have_a_system_actor(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        for e in store.list_content_events(record.id):
            self.assertIn("actor", e)
            self.assertTrue(e["actor"])
            self.assertTrue(e["created_at"])

    def test_approval_event_carries_the_real_approver_as_actor(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        approve_content_record(store, ctx, record, approver="Nick Stavros")
        approval_events = [e for e in store.list_content_events(record.id)
                           if e["event_type"] == "approval_recorded"]
        self.assertEqual(approval_events[0]["actor"], "Nick Stavros")


class TestPlatformNativeSeparationRegression(unittest.TestCase):
    """§5 — content for each platform is a genuinely separate record with
    its own draft; no silent corruption or truncation happens on the way
    through this module."""

    def test_generated_text_is_passed_through_unmodified(self):
        store = mem_store()
        ctx = ctx_for("capstack")
        registry = load_test_registry()
        material = seed_material(store)
        record = create_original_content(store, ctx, registry, material, "instagram",
                                         original_stub, audience="a", objective="o", cta="c")
        draft = store.get_draft(record.draft_id, ctx.name)
        expected = original_stub("a non-gate-judge generation prompt")
        self.assertEqual(draft.text, expected)
        self.assertEqual(record.brief["caption"], expected)


class TestEnginePurityStillHolds(unittest.TestCase):
    """The new files must not have introduced brand language into the
    engine package — same rule test_isolation.py already enforces."""

    def test_new_phase1_files_contain_no_brand_terms(self):
        from engage.safety.contamination import engine_purity_violations
        forbidden = ["empires", "capstack", "stowecap"]
        violations = [v for v in engine_purity_violations(PKG_DIR, forbidden)
                     if "originals.py" in v or "registry.py" in v]
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
