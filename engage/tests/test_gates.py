"""Safety gate tests against the SHIPPED brand configs. Every hard block from
the spec has a test, including fail-closed behavior."""
import unittest

from engage.safety.gates import run_gates

from .helpers import block_llm, broken_llm, ctx_for, pass_llm


class TestStowecapBlocks(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("stowecap")

    def assert_blocked(self, text):
        r = run_gates(text, self.ctx, source_texts=(text,), llm=pass_llm)
        self.assertFalse(r.passed, f"should have blocked: {text!r} — {r.reasons}")

    def test_returns_figures_blocked(self):
        self.assert_blocked("We're targeting an 18% IRR on the next one.")
        self.assert_blocked("Delivered a 2.1x multiple to our partners.")
        self.assert_blocked("MOIC came in strong this quarter.")

    def test_projections_blocked(self):
        self.assert_blocked("We're projecting RevPAR growth of 6% next year.")
        self.assert_blocked("Our pro forma assumes stabilization in 24 months.")

    def test_fundraising_language_blocked(self):
        self.assert_blocked("We're raising $20M for our next hotel.")
        self.assert_blocked("This offering is 506(b) — accredited only.")
        self.assert_blocked("Allocation remaining for Q4 — reach out.")
        self.assert_blocked("Invest with us.")

    def test_clean_operational_post_passes(self):
        text = "Topped out on 47th Street this week. 212 keys, steel up in 14 months."
        r = run_gates(text, self.ctx, source_texts=(text,), llm=pass_llm)
        self.assertTrue(r.passed, r.reasons)


class TestFailClosed(unittest.TestCase):
    def test_no_llm_blocks(self):
        ctx = ctx_for("stowecap")
        r = run_gates("Great weather at the site today.", ctx, llm=None)
        self.assertFalse(r.passed)
        self.assertIn("fail closed", " ".join(r.reasons))

    def test_llm_error_blocks(self):
        ctx = ctx_for("capstack")
        r = run_gates("Deals get done by people who show up.", ctx, llm=broken_llm)
        self.assertFalse(r.passed)

    def test_llm_block_verdict_blocks(self):
        ctx = ctx_for("capstack")
        r = run_gates("Deals get done by people who show up.", ctx, llm=block_llm)
        self.assertFalse(r.passed)

    def test_blocked_draft_cannot_be_approved(self):
        from engage.approval.queue import ApprovalError, approve, submit
        from engage.core.models import Draft, new_id

        from .helpers import mem_store
        ctx = ctx_for("stowecap")
        store = mem_store()
        d = Draft(id=new_id(), brand=ctx.name, kind="original", platform="linkedin",
                  text="We're raising $20M.")
        submit(store, ctx, d, llm=pass_llm)
        self.assertEqual(d.status, "BLOCKED")
        with self.assertRaises(ApprovalError):
            approve(store, ctx, d.id)


class TestCapstackNumericProvenance(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")
        self.source = ("$74M construction loan closed. The Cove, Fort Lauderdale — "
                       "376 mixed-income units. Lender: Pacific Life.",)

    def test_sourced_number_passes(self):
        text = "A $74M construction loan just closed — and the lender is a life insurer, not a bank."
        r = run_gates(text, self.ctx, source_texts=self.source, llm=pass_llm)
        self.assertTrue(r.passed, r.reasons)

    def test_invented_number_blocked(self):
        text = "A $95M construction loan just closed in Fort Lauderdale."
        r = run_gates(text, self.ctx, source_texts=self.source, llm=pass_llm)
        self.assertFalse(r.passed)
        self.assertTrue(any("invented" in s or "not found" in s for s in r.reasons))

    def test_numbers_with_no_source_blocked(self):
        text = "My first co-GP slice came on a $73M deal."
        r = run_gates(text, self.ctx, source_texts=(), llm=pass_llm)
        self.assertFalse(r.passed)

    def test_guarantee_language_blocked(self):
        text = "Follow this structure for a guaranteed return."
        r = run_gates(text, self.ctx, source_texts=(), llm=pass_llm)
        self.assertFalse(r.passed)

    def test_advice_language_blocked(self):
        text = "You should invest in multifamily right now."
        r = run_gates(text, self.ctx, source_texts=(), llm=pass_llm)
        self.assertFalse(r.passed)


class TestMetaTextLayer(unittest.TestCase):
    """A backend refusal is well-formed text with no figures and no blocked
    terms; without this layer it lands PENDING (observed live 2026-08-19)."""

    def setUp(self):
        self.ctx = ctx_for("capstack")

    def assert_blocked(self, text):
        r = run_gates(text, self.ctx, source_texts=(text,), llm=pass_llm)
        self.assertFalse(r.passed, f"should have blocked: {text!r}")
        self.assertTrue(any("meta-text" in s for s in r.reasons), r.reasons)

    def test_refusal_blocked(self):
        self.assert_blocked("I can't write this reply. It asks me to speak in "
                            "first person as a specific real person.")
        self.assert_blocked("I'm sorry, but I am unable to help with that request.")
        self.assert_blocked("I cannot generate content impersonating a real individual.")

    def test_prompt_machinery_commentary_blocked(self):
        self.assert_blocked("I don't have any source material provided in this "
                            "conversation (no voice guide, no real deal numbers).")
        self.assert_blocked("Since no voice guide was supplied, here is a generic take.")

    def test_ai_meta_text_blocked(self):
        self.assert_blocked("As an AI, I don't have personal deal experience.")
        self.assert_blocked("I'm a language model and can't verify these figures.")

    def test_first_person_emphasis_passes(self):
        text = ("I can't stress this enough: the promote is negotiated before "
                "the money shows up, not after.")
        r = run_gates(text, self.ctx, source_texts=(text,), llm=pass_llm)
        self.assertTrue(r.passed, r.reasons)


class TestEmpiresSourceClaims(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("empires")
        self.source = ("In 216 BC at Cannae, Hannibal destroyed roughly 80,000 men "
                       "in a single afternoon. Rome lost around eighty senators.",)

    def test_sourced_claim_passes(self):
        text = "216 BC. Cannae. 80,000 men gone in an afternoon — and Rome refused to talk peace."
        r = run_gates(text, self.ctx, source_texts=self.source, llm=pass_llm)
        self.assertTrue(r.passed, r.reasons)

    def test_unsourced_year_blocked(self):
        text = "In 415 BC Athens sailed for Sicily and never recovered."
        r = run_gates(text, self.ctx, source_texts=self.source, llm=pass_llm)
        self.assertFalse(r.passed)

    def test_present_day_politics_blocked(self):
        text = "Rome fell to inflation, and America is next."
        r = run_gates(text, self.ctx, source_texts=(text,), llm=pass_llm)
        self.assertFalse(r.passed)


if __name__ == "__main__":
    unittest.main()
