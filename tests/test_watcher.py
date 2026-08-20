import queue
import tempfile
import unittest
from pathlib import Path

from animew.mal import franchise_synopsis, is_real_synopsis
from animew.store import Store
from animew.watcher import WatchWorker


class FakeMAL:
    def __init__(self, results=None, details=None, fail_search=False, anime_map=None):
        self.results = results or [
            {"id": 62322, "title": "Lv999 no Murabito", "image_url": None, "media_type": "tv"}
        ]
        self.details = details or {
            "id": 62322, "title": "Lv999 no Murabito", "main_picture": {"large": ""},
            "synopsis": "A villager levels up.", "media_type": "tv", "num_episodes": 12,
        }
        self.fail_search = fail_search
        self.anime_map = anime_map or {}
        self.searches = []

    def search(self, query, limit=5):
        self.searches.append(query)
        if self.fail_search:
            raise RuntimeError("network down")
        return self.results

    def anime(self, mal_id, fields=""):
        if mal_id in self.anime_map:
            return self.anime_map[mal_id]
        d = dict(self.details)
        d["id"] = mal_id
        return d

    def details_with_synopsis(self, mal_id, fields=""):
        """Mirror of MALClient.details_with_synopsis: replace a stub synopsis
        with the first real one found along the prequel chain."""
        d = self.anime(mal_id, fields=fields)
        if not is_real_synopsis(d.get("synopsis")):
            real = franchise_synopsis(
                lambda mid: self.anime(mid, fields="id,synopsis,related_anime"),
                mal_id,
            )
            if real:
                d = dict(d)
                d["synopsis"] = real
        return d


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, d):
        self.t += d


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.out = queue.Queue()
        self.cmd = queue.Queue()
        self.clock = FakeClock()
        self.worker = WatchWorker(
            [], self.out, self.cmd,
            store=self.store, mal=FakeMAL(), threshold=5.0, clock=self.clock,
            tags=["Demo"],
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def play_episode(self, filename, seconds):
        self.worker.handle_event({"type": "file", "filename": filename})
        self.worker.handle_event({"type": "property", "name": "pause", "data": False})
        for i in range(int(seconds)):
            self.clock.advance(1)
            self.worker.handle_event({"type": "property", "name": "time-pos", "data": float(i)})

    def drain(self):
        msgs = []
        while True:
            try:
                msgs.append(self.out.get_nowait())
            except queue.Empty:
                return msgs

    def test_confirm_after_threshold(self):
        self.play_episode("[Demo] Lv999 no Murabito - 05 (1080p) [ABC].mkv", 6)
        msgs = self.drain()
        self.assertEqual(msgs[0]["type"], "resolved")
        self.assertEqual(msgs[0]["mal_id"], 62322)
        self.assertEqual(msgs[0]["episode"], 5)
        self.assertEqual(msgs[1]["type"], "confirmed")
        self.assertTrue(msgs[1]["first"])
        self.assertTrue(self.store.is_watched(62322, 5))

    def test_not_confirmed_below_threshold(self):
        self.play_episode("[Demo] Lv999 no Murabito - 05 (1080p) [ABC].mkv", 4)
        msgs = self.drain()
        self.assertEqual([m["type"] for m in msgs], ["resolved"])
        self.assertFalse(self.store.is_watched(62322, 5))

    def test_pause_does_not_accrue(self):
        self.worker.handle_event({"type": "file", "filename": "[Demo] X - 01 (1080p).mkv"})
        self.worker.handle_event({"type": "property", "name": "pause", "data": False})
        self.clock.advance(2)
        self.worker.handle_event({"type": "property", "name": "time-pos", "data": 2.0})
        # pause for a long time — no events, then resume
        self.worker.handle_event({"type": "property", "name": "pause", "data": True})
        self.clock.advance(60)
        self.worker.handle_event({"type": "property", "name": "pause", "data": False})
        self.clock.advance(2)
        self.worker.handle_event({"type": "property", "name": "time-pos", "data": 4.0})
        # accrued = 2 + 2 = 4 < threshold 5 (the 60s pause accrued nothing)
        msgs = self.drain()
        self.assertEqual([m["type"] for m in msgs], ["resolved"])
        self.assertFalse(self.store.is_watched(1, 1))

    def test_rewatch_notifies_second_time(self):
        self.play_episode("[Demo] X - 01 (1080p).mkv", 6)
        self.drain()
        # replay the same episode
        self.worker.handle_event({"type": "idle"})
        self.play_episode("[Demo] X - 01 (1080p).mkv", 6)
        msgs = self.drain()
        confirmed = [m for m in msgs if m["type"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertFalse(confirmed[0]["first"])

    def test_movie_tracked_without_episode(self):
        self.play_episode("[Demo] Suzume (2022) (1080p) [ABC].mkv", 6)
        msgs = self.drain()
        confirmed = [m for m in msgs if m["type"] == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertIsNone(confirmed[0]["episode"])
        self.assertTrue(self.store.is_watched(62322))  # episode=None

    def test_duplicate_file_events_ignored(self):
        self.worker.handle_event({"type": "file", "filename": "[Demo] X - 01 (1080p).mkv"})
        self.worker.handle_event({"type": "file", "filename": "[Demo] X - 01 (1080p).mkv"})
        msgs = self.drain()
        self.assertEqual([m["type"] for m in msgs], ["resolved"])

    def test_ignored_non_tracked(self):
        self.worker.handle_event({"type": "file", "filename": "/tmp/vacation.mp4"})
        msgs = self.drain()
        self.assertEqual(msgs[0]["type"], "ignored")
        self.assertIsNone(self.worker.current)

    def test_empty_tags_track_nothing(self):
        self.worker.tags = []
        self.worker.handle_event({"type": "file", "filename": "[Demo] X - 01 (1080p).mkv"})
        msgs = self.drain()
        self.assertEqual(msgs[0]["type"], "ignored")
        self.assertIsNone(self.worker.current)

    def test_set_tags_command_updates_parser(self):
        self.cmd.put({"cmd": "set_tags", "tags": ["Second"]})
        self.worker._drain_commands()
        self.assertEqual(self.worker.tags, ["Second"])
        self.worker.handle_event({"type": "file", "filename": "[Demo] X - 01 (1080p).mkv"})
        self.assertEqual(self.drain()[0]["type"], "ignored")
        self.worker.handle_event({"type": "file", "filename": "[Second] X - 01 (1080p).mkv"})
        self.assertEqual(self.drain()[0]["type"], "resolved")

    def test_resolve_failed(self):
        self.worker.mal = FakeMAL(fail_search=True)
        self.worker.handle_event({"type": "file", "filename": "[Demo] X - 01 (1080p).mkv"})
        msgs = self.drain()
        self.assertEqual(msgs[0]["type"], "resolve_failed")
        self.assertIsNone(self.worker.current)

    def test_season_marker_resolves_to_sequel(self):
        # search for "Show S2" returns S1 first; S1's related_anime has the sequel
        s1 = {"id": 100, "title": "Show", "image_url": None, "media_type": "tv"}
        s1_details = {
            "id": 100, "title": "Show", "main_picture": {}, "synopsis": "s",
            "media_type": "tv", "num_episodes": 12,
            "related_anime": [
                {"node": {"id": 101, "title": "Show 2nd Season", "media_type": "tv",
                           "main_picture": {}},
                 "relation_type": "sequel"},
            ],
        }
        self.worker.mal = FakeMAL(results=[s1], anime_map={100: s1_details})
        self.worker.handle_event({"type": "file", "filename": "[Demo] Show S2 - 05 (1080p).mkv"})
        msgs = self.drain()
        self.assertEqual(msgs[0]["type"], "resolved")
        self.assertEqual(msgs[0]["mal_id"], 101)

    def test_season_marker_prefers_marked_candidate(self):
        s1 = {"id": 100, "title": "Show", "image_url": None, "media_type": "tv"}
        s2 = {"id": 101, "title": "Show 2nd Season", "image_url": None, "media_type": "tv"}
        self.worker.mal = FakeMAL(results=[s1, s2])
        self.worker.handle_event({"type": "file", "filename": "[Demo] Show S2 - 05 (1080p).mkv"})
        msgs = self.drain()
        self.assertEqual(msgs[0]["mal_id"], 101)

    def test_no_marker_uses_top_result(self):
        top = {"id": 55, "title": "Plain Show", "image_url": None, "media_type": "tv"}
        self.worker.mal = FakeMAL(results=[top])
        self.worker.handle_event({"type": "file", "filename": "[Demo] Plain Show - 02 (1080p).mkv"})
        msgs = self.drain()
        self.assertEqual(msgs[0]["mal_id"], 55)

    def test_stub_synopsis_falls_back_to_first_season(self):
        s2 = {"id": 101, "title": "Show 2nd Season", "image_url": None, "media_type": "tv"}
        s2_details = {
            "id": 101, "title": "Show 2nd Season", "main_picture": {},
            "synopsis": "Second season of Show.", "media_type": "tv",
            "num_episodes": 12,
            "related_anime": [{"node": {"id": 100, "title": "Show"},
                                "relation_type": "prequel"}],
        }
        s1_details = {
            "id": 100, "title": "Show", "main_picture": {},
            "synopsis": "The first season's real synopsis, long enough to pass. " * 10,
            "media_type": "tv", "num_episodes": 12, "related_anime": [],
        }
        self.worker.mal = FakeMAL(
            results=[s2], anime_map={101: dict(s2_details), 100: dict(s1_details)})
        self.worker.handle_event(
            {"type": "file", "filename": "[Demo] Show S2 - 01 (1080p).mkv"})
        row = self.store.get_anime(101)
        self.assertIsNotNone(row)
        self.assertEqual(row["synopsis"], s1_details["synopsis"].strip())
        self.assertEqual(row["num_episodes"], 12)

    def test_repick_pushes_confirmed_episode_to_sync(self):
        class FakeSync:
            def __init__(self):
                self.pushed = []

            def push_episode(self, mal_id, episode, num_episodes):
                self.pushed.append((mal_id, episode, num_episodes))

        bad = {"id": 999, "title": "Wrong", "image_url": None, "media_type": "tv"}
        good = {"id": 62322, "title": "Lv999 no Murabito", "main_picture": {}, "synopsis": "s",
                "media_type": "tv", "num_episodes": 12, "related_anime": []}
        self.worker.mal = FakeMAL(results=[bad], anime_map={62322: good})
        self.worker.sync = FakeSync()
        self.play_episode("[Demo] Show - 05 (1080p).mkv", 6)  # confirmed under 999
        self.drain()
        self.assertEqual(self.worker.sync.pushed, [(999, 5, 12)])
        self.cmd.put({"cmd": "repick", "old_mal_id": 999, "new_mal_id": 62322, "title": "Lv999 no Murabito"})
        self.worker._drain_commands()
        # the corrected entry gets pushed to MAL
        self.assertIn((62322, 5, 12), self.worker.sync.pushed)
        msgs = self.drain()
        self.assertEqual(msgs[0]["type"], "repicked")
        self.assertEqual(msgs[0]["mal_id"], 62322)
        self.assertTrue(msgs[0]["confirmed"])

    def test_repick_switches_resolution(self):
        # resolve to the wrong id, then re-pick to the right one
        bad = {"id": 999, "title": "Wrong Show", "image_url": None, "media_type": "tv"}
        self.worker.mal = FakeMAL(results=[bad], details={
            "id": 62322, "title": "Lv999 no Murabito", "main_picture": {"large": ""},
            "synopsis": "A villager levels up.", "media_type": "tv", "num_episodes": 12,
        })
        self.play_episode("[Demo] Lv999 no Murabito - 05 (1080p) [ABC].mkv", 6)
        self.drain()
        self.assertTrue(self.store.is_watched(999, 5))
        self.worker.handle_event({"type": "idle"})
        self.worker._drain_commands()  # no-op
        self.cmd.put({"cmd": "repick", "old_mal_id": 999, "new_mal_id": 62322, "title": "Lv999 no Murabito"})
        self.worker._drain_commands()
        self.assertIsNone(self.store.get_anime(999))
        self.assertFalse(self.store.is_watched(999, 5))
        self.assertIsNotNone(self.store.get_anime(62322))
        msgs = self.drain()
        self.assertEqual(msgs[0]["type"], "repicked")


if __name__ == "__main__":
    unittest.main()
