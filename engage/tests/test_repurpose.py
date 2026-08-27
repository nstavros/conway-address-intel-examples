"""Repurposing fan-out: platform-native variants, reuse ledger enforcement
("one shot, one use"), and fail-closed behavior for brands without a
repurpose prompt."""
import unittest

from engage.approval.queue import submit
from engage.drafting.llm import PromptAssemblyError
from engage.drafting.repurpose import eligible_platforms, repurpose

from .helpers import ctx_for, mem_store, pass_llm

MATERIAL = {
    "id": "cove1",
    "title": "Cove deal update",
    "text": "$74M construction loan closed. 376 mixed-income units. "
            "The lender is a life insurer, not a bank.",
    "kind": "deal_update",
}


def stub_backend(prompt: str) -> str:
    # extract the platform line so each variant is distinguishable
    platform = next((ln.split(":", 1)[1].strip() for ln in prompt.splitlines()
                     if ln.startswith("Target platform:")), "?")
    return f"The lender is a life insurer, not a bank. Native cut for {platform}."


class TestFanOut(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")
        self.store = mem_store()
        self.store.add_material(MATERIAL["id"], MATERIAL["title"],
                                MATERIAL["text"], MATERIAL["kind"])
        self.material = self.store.get_material(MATERIAL["id"])

    def test_one_draft_per_platform(self):
        drafts, skipped = repurpose(self.store, self.ctx, self.material, stub_backend)
        self.assertEqual(skipped, [])
        self.assertEqual({d.platform for d in drafts}, set(self.ctx.config["platforms"]))
        for d in drafts:
            self.assertEqual(d.kind, "repurpose")
            self.assertEqual(d.material_id, MATERIAL["id"])
            self.assertIn(d.platform, d.text)

    def test_platform_subset(self):
        drafts, _ = repurpose(self.store, self.ctx, self.material, stub_backend, ["x"])
        self.assertEqual([d.platform for d in drafts], ["x"])

    def test_unknown_platform_rejected(self):
        with self.assertRaises(ValueError):
            repurpose(self.store, self.ctx, self.material, stub_backend, ["myspace"])

    def test_reuse_ledger_blocks_recycling(self):
        drafts, _ = repurpose(self.store, self.ctx, self.material, stub_backend, ["x"])
        submit(self.store, self.ctx, drafts[0], (MATERIAL["text"],), pass_llm)
        # same material, same platform, inside the window -> skipped
        eligible, skipped = eligible_platforms(self.store, self.ctx, MATERIAL["id"], ["x"])
        self.assertEqual(eligible, [])
        self.assertEqual(skipped, ["x"])
        drafts2, skipped2 = repurpose(self.store, self.ctx, self.material, stub_backend, ["x"])
        self.assertEqual(drafts2, [])
        self.assertEqual(skipped2, ["x"])
        # other platforms remain eligible (capstack has no linkedin target — see DEC-SM-003)
        eligible, _ = eligible_platforms(self.store, self.ctx, MATERIAL["id"], ["instagram"])
        self.assertEqual(eligible, ["instagram"])

    def test_blocked_draft_does_not_burn_the_reuse_slot(self):
        from .helpers import block_llm
        drafts, _ = repurpose(self.store, self.ctx, self.material, stub_backend, ["x"])
        submit(self.store, self.ctx, drafts[0], (MATERIAL["text"],), block_llm)
        self.assertEqual(drafts[0].status, "BLOCKED")
        eligible, skipped = eligible_platforms(self.store, self.ctx, MATERIAL["id"], ["x"])
        self.assertEqual(eligible, ["x"], "a blocked draft must not consume the material")

    def test_brand_without_repurpose_prompt_fails_closed(self):
        ctx = ctx_for("stowecap")
        store = mem_store()
        store.add_material("m1", "t", "text", "note")
        with self.assertRaises(PromptAssemblyError):
            repurpose(store, ctx, store.get_material("m1"), stub_backend,
                      [ctx.config["platforms"][0]])


if __name__ == "__main__":
    unittest.main()
