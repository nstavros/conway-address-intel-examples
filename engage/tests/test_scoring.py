import time
import unittest

from engage.core.models import Post
from engage.scoring.scorer import score_post

from .helpers import ctx_for, mem_store

NOW = int(time.time())


def post(**kw):
    base = dict(id="p1", brand="capstack", platform="x", author="someone",
                text="How do you structure a co-GP promote on a first deal?",
                created_at=NOW - 3600)
    base.update(kw)
    return Post(**base)


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")
        self.store = mem_store()

    def test_components_recorded(self):
        o = score_post(post(), self.ctx, self.store, NOW)
        for k in ("relevance", "author_value", "recency", "momentum", "history"):
            self.assertIn(k, o.components)
        self.assertGreater(o.score, 0)

    def test_question_multiplier_applies(self):
        q = score_post(post(), self.ctx, self.store, NOW)
        self.assertIn("question_multiplier", q.components)
        s = score_post(post(id="p2", text="Structured a co-GP promote on my first deal."),
                       self.ctx, self.store, NOW)
        self.assertNotIn("question_multiplier", s.components)
        self.assertGreater(q.score, s.score)

    def test_recency_decays(self):
        fresh = score_post(post(id="f", created_at=NOW - 1800), self.ctx, self.store, NOW)
        stale = score_post(post(id="s", created_at=NOW - 72 * 3600), self.ctx, self.store, NOW)
        self.assertGreater(fresh.components["recency"], stale.components["recency"])

    def test_momentum_from_snapshots(self):
        climbing = post(id="c", snapshots=[(NOW - 3600, 10), (NOW, 100)])
        flat = post(id="fl", snapshots=[(NOW - 3600, 10), (NOW, 11)])
        oc = score_post(climbing, self.ctx, self.store, NOW)
        of = score_post(flat, self.ctx, self.store, NOW)
        self.assertGreater(oc.components["momentum"], of.components["momentum"])
        single = score_post(post(id="one"), self.ctx, self.store, NOW)
        self.assertEqual(single.components["momentum"], 0.5)

    def test_exclusions(self):
        self.store.record_touch("someone", "x", "p1", NOW - 100)
        o = score_post(post(), self.ctx, self.store, NOW)
        self.assertTrue(o.excluded)
        self.assertEqual(o.score, 0.0)

        o2 = score_post(post(id="p9", author="other"), self.ctx, self.store, NOW)
        self.assertFalse(o2.excluded)

        own = score_post(post(id="mine", author="me", own=True), self.ctx, self.store, NOW)
        self.assertTrue(own.excluded)

    def test_cooldown_expires(self):
        old = NOW - (self.ctx.config["scoring"]["cooldown_days"] + 1) * 86400
        self.store.record_touch("someone", "x", "old-post", old)
        o = score_post(post(id="p10"), self.ctx, self.store, NOW)
        self.assertFalse(o.excluded)


if __name__ == "__main__":
    unittest.main()
