"""Brand isolation / cross-contamination suite. These tests validate the
SHIPPED brand configs, not synthetic ones — if a config regresses, CI fails."""
import unittest

from engage.core.brand import IsolationError
from engage.drafting.llm import assemble
from engage.safety.contamination import engine_purity_violations, foreign_signatures

from .helpers import BRANDS_DIR, PKG_DIR, all_contexts, ctx_for


class TestEnginePurity(unittest.TestCase):
    def test_engine_contains_no_brand_terms(self):
        contexts = all_contexts()
        terms = [c.name for c in contexts]
        for c in contexts:
            terms += c.config["voice"].get("signature_phrases", [])
        violations = engine_purity_violations(PKG_DIR, terms)
        self.assertEqual(violations, [], "brand language leaked into the engine package")


class TestCanaries(unittest.TestCase):
    def test_every_prompt_carries_own_canary(self):
        for ctx in all_contexts():
            for name, text in ctx.prompts.items():
                self.assertIn(ctx.canary, text, f"{ctx.name}/{name} missing canary")

    def test_foreign_canary_in_variables_raises(self):
        empires = ctx_for("empires")
        capstack = ctx_for("capstack")
        with self.assertRaises(IsolationError):
            assemble(empires, "reply", {
                "angle": "x", "author": "a", "platform": "instagram",
                "post_text": f"stray {capstack.canary} token",
            })

    def test_own_canary_assembles_cleanly(self):
        empires = ctx_for("empires")
        out = assemble(empires, "reply", {
            "angle": "detail", "author": "a", "platform": "instagram",
            "post_text": "Rome fell in 476.",
        })
        self.assertIn("Rome fell in 476.", out)


class TestSignatureSeparation(unittest.TestCase):
    def test_brand_signatures_do_not_cross(self):
        contexts = all_contexts()
        empires = next(c for c in contexts if c.name == "empires")
        capstack = next(c for c in contexts if c.name == "capstack")

        empires_text = "Every empire dies wearing someone else's clothes."
        hits = foreign_signatures(empires_text, capstack, contexts)
        self.assertTrue(hits, "empires tagline should be flagged in a capstack draft")

        capstack_text = "Match yourself to the money. Comment GAP for the map."
        hits = foreign_signatures(capstack_text, empires, contexts)
        self.assertTrue(hits, "capstack signature should be flagged in an empires draft")

        # a brand's own signature is never a violation for itself
        self.assertEqual(foreign_signatures(empires_text, empires, contexts), [])

    def test_calibration_fixtures_are_clean_in_their_own_brand(self):
        # the real pulled posts must not trip the OTHER brand's ownership
        contexts = all_contexts()
        empires = next(c for c in contexts if c.name == "empires")
        import json

        from .helpers import FIXTURES
        rows = json.loads((FIXTURES / "empires_instagram_media.json").read_text())
        for r in rows:
            self.assertEqual(
                foreign_signatures(r["caption"], empires, contexts), [],
                f"empires post {r['mediaId']} contains another brand's signature",
            )


class TestLoaderPathSafety(unittest.TestCase):
    def test_prompt_dir_outside_brand_refused(self):
        import shutil
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            brand_dir = Path(td) / "brands" / "testbrand"
            shutil.copytree(BRANDS_DIR / "empires", brand_dir)
            cfg_path = brand_dir / "config.yaml"
            cfg = cfg_path.read_text().replace(
                "brand: empires", "brand: testbrand"
            ).replace("prompt_dir: prompts", "prompt_dir: ../../")
            cfg_path.write_text(cfg)
            from engage.config import load_brand
            with self.assertRaises(IsolationError):
                load_brand(f"{td}/brands", "testbrand")


if __name__ == "__main__":
    unittest.main()
