import datetime
import queue
import tempfile
import unittest
from pathlib import Path

from animew.checker import CHECK_INTERVAL, NewContentChecker
from animew.store import Store


class FakeMAL:
    def __init__(self, details_map):
        self.details_map = details_map

    def anime(self, mal_id, fields=""):
        return self.details_map[mal_id]


class CheckerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.out = queue.Queue()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def watch(self, mal_id, ep=3):
        self.store.upsert_anime(mal_id, f"Show {mal_id}", "tv")
        self.store.mark_watched(mal_id, ep)

    def test_detects_new_sequel(self):
        self.watch(1)
        mal = FakeMAL({
            1: {"id": 1, "related_anime": [{"node": {"id": 2}, "relation_type": "sequel"}]},
            2: {"id": 2, "title": "Show 1 2nd Season", "media_type": "tv"},
        })
        checker = NewContentChecker(self.store, mal, self.out)
        detected = checker.run_check()
        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0]["target_mal_id"], 2)
        self.assertEqual(detected[0]["target_title"], "Show 1 2nd Season")
        self.assertEqual(self.store.active_new_targets(1), [2])
        self.assertEqual(self.store.badge_map().get(1), [2])

    def test_badge_on_all_franchise_cards(self):
        # Youjo Senki case: S1 (32615) and S2 (49233) watched, Movie (37055)
        # untracked. S1's related links the movie (sequel); the movie links
        # S2 (sequel). The badge must appear on BOTH seasons, not on the
        # parody (34742).
        for mid in (32615, 49233):
            self.watch(mid)
        mal = FakeMAL({
            32615: {"id": 32615, "related_anime": [
                {"node": {"id": 37055}, "relation_type": "sequel"},
                {"node": {"id": 34742}, "relation_type": "other"},
            ]},
            37055: {"id": 37055, "title": "Youjo Senki Movie", "media_type": "movie",
                    "related_anime": [
                        {"node": {"id": 32615}, "relation_type": "prequel"},
                        {"node": {"id": 49233}, "relation_type": "sequel"},
                    ]},
            49233: {"id": 49233, "related_anime": [
                {"node": {"id": 37055}, "relation_type": "prequel"}]},
            34742: {"id": 34742, "title": "Youjo Shenki", "media_type": "tv"},
        })
        checker = NewContentChecker(self.store, mal, self.out)
        detected = checker.run_check()
        self.assertEqual([d["target_mal_id"] for d in detected], [37055])
        bm = self.store.badge_map()
        self.assertEqual(bm.get(32615), [37055])
        self.assertEqual(bm.get(49233), [37055])
        self.assertNotIn(34742, bm)

    def test_badge_clears_when_target_watched(self):
        self.watch(1)
        mal = FakeMAL({
            1: {"id": 1, "related_anime": [{"node": {"id": 2}, "relation_type": "sequel"}]},
            2: {"id": 2, "title": "S2", "media_type": "tv"},
        })
        NewContentChecker(self.store, mal, self.out).run_check()
        self.assertEqual(self.store.badge_map().get(1), [2])
        # user starts watching the detected sequel
        self.store.upsert_anime(2, "S2", "tv")
        self.store.mark_watched(2, 1)
        self.assertEqual(self.store.badge_map(), {})

    def test_skips_already_tracked(self):
        self.watch(1)
        self.store.upsert_anime(2, "Already Known", "tv")  # in anime table
        mal = FakeMAL({
            1: {"id": 1, "related_anime": [{"node": {"id": 2}, "relation_type": "sequel"}]},
        })
        detected = NewContentChecker(self.store, mal, self.out).run_check()
        self.assertEqual(detected, [])

    def test_skips_junk_media(self):
        self.watch(1)
        mal = FakeMAL({
            1: {"id": 1, "related_anime": [{"node": {"id": 2}, "relation_type": "sequel"}]},
            2: {"id": 2, "title": "OP Theme", "media_type": "music"},
        })
        detected = NewContentChecker(self.store, mal, self.out).run_check()
        self.assertEqual(detected, [])

    def test_other_relation_movies_only(self):
        self.watch(1)
        mal = FakeMAL({
            1: {"id": 1, "related_anime": [
                {"node": {"id": 2}, "relation_type": "other"},   # parody tv -> skip
                {"node": {"id": 3}, "relation_type": "other"},   # movie -> detect
            ]},
            2: {"id": 2, "title": "Parody Show", "media_type": "tv"},
            3: {"id": 3, "title": "The Movie", "media_type": "movie"},
        })
        detected = NewContentChecker(self.store, mal, self.out).run_check()
        self.assertEqual([d["target_mal_id"] for d in detected], [3])

    def test_dedupe_across_runs(self):
        self.watch(1)
        mal = FakeMAL({
            1: {"id": 1, "related_anime": [{"node": {"id": 2}, "relation_type": "sequel"}]},
            2: {"id": 2, "title": "S2", "media_type": "tv"},
        })
        checker = NewContentChecker(self.store, mal, self.out)
        self.assertEqual(len(checker.run_check()), 1)
        self.assertEqual(checker.run_check(), [])  # already recorded

    def test_no_time_window_old_sequel_detected(self):
        # Q7: no time filter — an old untracked sequel still qualifies.
        self.watch(1)
        mal = FakeMAL({
            1: {"id": 1, "related_anime": [{"node": {"id": 2}, "relation_type": "sequel"}]},
            2: {"id": 2, "title": "Old Sequel", "media_type": "tv"},
        })
        detected = NewContentChecker(self.store, mal, self.out).run_check()
        self.assertEqual(len(detected), 1)

    def test_dismiss_clears_active(self):
        self.watch(1)
        mal = FakeMAL({
            1: {"id": 1, "related_anime": [{"node": {"id": 2}, "relation_type": "sequel"}]},
            2: {"id": 2, "title": "S2", "media_type": "tv"},
        })
        NewContentChecker(self.store, mal, self.out).run_check()
        self.store.dismiss_new_content(1)
        self.assertEqual(self.store.active_new_targets(1), [])
        self.assertEqual(self.store.badge_map(), {})

    def test_due_scheduling(self):
        checker = NewContentChecker(self.store, FakeMAL({}), self.out)
        self.assertTrue(checker.due())  # never checked
        checker.run_check()
        self.assertFalse(checker.due())
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(seconds=CHECK_INTERVAL + 10)).isoformat()
        self.store.set_setting("new_content_last_check", old)
        self.assertTrue(checker.due())


if __name__ == "__main__":
    unittest.main()
