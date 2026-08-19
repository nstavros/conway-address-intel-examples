"""YouTube RSS ingest adapter — offline against a fixture feed."""
import unittest

from engage.ingest.youtube_rss import YouTubeRSSSource

from .helpers import FIXTURES, mem_store


class TestYouTubeRSS(unittest.TestCase):
    def setUp(self):
        self.src = YouTubeRSSSource("somebrand", path=FIXTURES / "youtube_feed_sample.xml")

    def test_parses_entries_as_own_posts(self):
        posts = self.src.poll()
        self.assertEqual(len(posts), 2)
        p = posts[0]
        self.assertEqual(p.id, "abc123def45")
        self.assertEqual(p.platform, "youtube")
        self.assertTrue(p.own)
        self.assertIn("First sample video", p.text)
        self.assertIn("short description", p.text)
        self.assertEqual(p.url, "https://www.youtube.com/shorts/abc123def45")
        self.assertGreater(p.created_at, 0)

    def test_view_counts_become_snapshots(self):
        posts = self.src.poll()
        self.assertEqual(posts[0].snapshots[0][1], 717)
        self.assertEqual(posts[1].snapshots[0][1], 1030)

    def test_repeat_ingest_appends_snapshots_not_duplicates(self):
        store = mem_store()
        for p in self.src.poll():
            store.upsert_post(p)
        for p in self.src.poll():  # second pull, later timestamp or same
            store.upsert_post(p)
        stored = store.get_post("abc123def45", "somebrand")
        self.assertIsNotNone(stored)
        # identical (ts, views) pairs are deduped; count stays sane
        self.assertLessEqual(len(stored.snapshots), 2)
        self.assertEqual(stored.snapshots[0][1], 717)

    def test_requires_channel_or_file(self):
        with self.assertRaises(ValueError):
            YouTubeRSSSource("somebrand")


if __name__ == "__main__":
    unittest.main()
