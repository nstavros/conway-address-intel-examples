"""Comment triage: HUMAN / DRAFTABLE / SKIP classification and theme mining,
against the shipped brand configs."""
import unittest

from engage.triage.comments import classify_comment, mine_themes, triage_comments

from .helpers import ctx_for


class TestClassification(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")

    def cls(self, text):
        return classify_comment(text, self.ctx)[0]

    def test_complaints_and_trust_issues_go_human(self):
        self.assertEqual(self.cls("This is wrong, the numbers are misleading"), "HUMAN")
        self.assertEqual(self.cls("Is this a scam?"), "HUMAN")

    def test_contact_and_business_requests_go_human(self):
        self.assertEqual(self.cls("Can you DM me the details?"), "HUMAN")
        self.assertEqual(self.cls("Would love to partner with you on a deal"), "HUMAN")

    def test_personal_money_questions_go_human(self):
        self.assertEqual(self.cls("Should I invest my 401k in this?"), "HUMAN")
        self.assertEqual(self.cls("What should I do with my savings?"), "HUMAN")

    def test_on_topic_question_is_draftable(self):
        self.assertEqual(self.cls("How does the promote work on a first deal?"), "DRAFTABLE")
        self.assertEqual(self.cls("What goes into a capital stack?"), "DRAFTABLE")

    def test_off_topic_question_goes_human(self):
        self.assertEqual(self.cls("What's your favorite restaurant in Miami?"), "HUMAN")

    def test_praise_and_noise_skipped(self):
        self.assertEqual(self.cls("Fire!"), "SKIP")
        self.assertEqual(self.cls("🔥🔥🔥"), "SKIP")
        self.assertEqual(self.cls("great"), "SKIP")
        self.assertEqual(self.cls("Been following since day one."), "SKIP")


class TestBatchAndThemes(unittest.TestCase):
    def setUp(self):
        self.ctx = ctx_for("capstack")
        self.comments = [
            {"id": "1", "author": "a", "text": "How do I structure a co-gp promote?"},
            {"id": "2", "author": "b", "text": "What does a construction loan lender want?"},
            {"id": "3", "author": "c", "text": "co-gp questions again: who gets the promote?"},
            {"id": "4", "author": "d", "text": "🔥"},
        ]

    def test_batch_preserves_ids(self):
        results = triage_comments(self.comments, self.ctx)
        self.assertEqual([r.comment_id for r in results], ["1", "2", "3", "4"])

    def test_theme_mining_counts_topics(self):
        themes = dict(mine_themes(self.comments, self.ctx))
        self.assertEqual(themes.get("co-gp"), 2)
        self.assertEqual(themes.get("promote"), 2)
        self.assertEqual(themes.get("construction loan"), 1)


if __name__ == "__main__":
    unittest.main()
