import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from animelist import config
from animelist.auth import TokenStore
from animelist.store import Store
from animelist.sync import MALSync


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, handler=None):
        self.handler = handler
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, params=None, data=None):
        self.calls.append((method, url, dict(headers or {}), params, data))
        if self.handler:
            return self.handler(method, url, params, data)
        return FakeResp({})


def make_sync(tmp, handler=None):
    store = Store(Path(tmp) / "sync.db")
    tokens = TokenStore(Path(tmp) / "token.json")
    tokens.save({"access_token": "tok", "refresh_token": "rt", "expires_at": time.time() + 3600})
    session = FakeSession(handler)
    sync = MALSync("cid", "sec", tokens, store, session)
    return sync, store, session


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sync, self.store, self.session = make_sync(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def patches(self):
        return [c for c in self.session.calls if c[0] == "PATCH"]

    def test_push_creates_watching_entry(self):
        self.sync.push_episode(62322, 5, 12)
        calls = self.patches()
        self.assertEqual(len(calls), 1)
        data = calls[0][4]
        self.assertEqual(data["num_watched_episodes"], 5)
        self.assertEqual(data["status"], "watching")
        self.assertNotIn("score", data)
        row = self.store.get_mal_status(62322)
        self.assertEqual(row["num_watched_episodes"], 5)
        self.assertEqual(row["status"], "watching")

    def test_push_raises_progress_without_status_change(self):
        self.store.set_mal_status(62322, "watching", 3, 7, None)
        self.sync.push_episode(62322, 5, 12)
        data = self.patches()[0][4]
        self.assertEqual(data["num_watched_episodes"], 5)
        self.assertNotIn("status", data)  # already watching
        self.assertNotIn("score", data)

    def test_push_never_lowers_progress(self):
        self.store.set_mal_status(62322, "watching", 10, None, None)
        self.sync.push_episode(62322, 3, 12)
        self.assertEqual(self.patches(), [])  # no request at all

    def test_push_completion_flip(self):
        self.store.set_mal_status(62322, "watching", 11, None, None)
        self.sync.push_episode(62322, 12, 12)
        data = self.patches()[0][4]
        self.assertEqual(data["num_watched_episodes"], 12)
        self.assertEqual(data["status"], "completed")
        self.assertEqual(self.store.get_mal_status(62322)["status"], "completed")

    def test_push_plan_to_watch_becomes_watching(self):
        self.store.set_mal_status(62322, "plan_to_watch", 0, None, None)
        self.sync.push_episode(62322, 1, 12)
        data = self.patches()[0][4]
        self.assertEqual(data["status"], "watching")

    def test_push_movie_completes(self):
        self.sync.push_episode(62322, None, 1)  # movie: episode None
        data = self.patches()[0][4]
        self.assertEqual(data["num_watched_episodes"], 1)
        self.assertEqual(data["status"], "completed")

    def test_failure_queues_pending(self):
        self.session.handler = lambda m, u, p, d: FakeResp({}, status=500)
        self.sync.push_episode(62322, 5, 12)
        pending = self.store.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["mal_id"], 62322)

    def test_import_marks_watched_locally(self):
        def handler(method, url, params, data):
            return FakeResp({
                "data": [{
                    "node": {"id": 1, "title": "A", "media_type": "tv"},
                    "list_status": {"status": "watching", "num_episodes_watched": 3,
                                    "updated_at": "2025-01-01T00:00:00+00:00"},
                }],
                "paging": {},
            })

        self.session.handler = handler
        self.sync.import_list()
        self.assertTrue(self.store.is_watched(1, 3))
        self.assertFalse(self.store.is_watched(1, 4))

    def test_new_entry_completes_prequels(self):
        def handler(method, url, params, data):
            if method == "GET" and "/anime/101" in url:
                return FakeResp({
                    "id": 101, "title": "Show 2nd Season", "media_type": "tv",
                    "num_episodes": 12,
                    "related_anime": [
                        {"node": {"id": 100, "title": "Show"},
                         "relation_type": "prequel"},
                    ],
                })
            if method == "GET" and "/anime/100" in url:
                return FakeResp({
                    "id": 100, "title": "Show", "media_type": "tv",
                    "num_episodes": 12,
                    "synopsis": "The first season's real synopsis. " * 10,
                    "main_picture": {},
                    "related_anime": [],
                })
            return FakeResp({})

        self.session.handler = handler
        self.sync.push_episode(101, 5, 12)
        patches = self.patches()
        self.assertEqual(len(patches), 2)
        m101 = [c for c in patches if "/anime/101/" in c[1]][0][4]
        m100 = [c for c in patches if "/anime/100/" in c[1]][0][4]
        self.assertEqual(m101["status"], "watching")
        self.assertEqual(m100, {"status": "completed", "num_watched_episodes": 12})
        self.assertEqual(self.store.get_mal_status(100)["status"], "completed")
        self.assertTrue(self.store.is_watched(100, 12))
        # the completed prequel must get a local card row too, otherwise it
        # never shows in the widget grid until the next MAL list import
        row = self.store.get_anime(100)
        self.assertIsNotNone(row)
        self.assertEqual(row["title"], "Show")
        self.assertEqual(row["num_episodes"], 12)
        self.assertTrue(row["synopsis"].startswith("The first season's real synopsis"))
        self.assertEqual(row["mal_url"], "https://myanimelist.net/anime/100")

    def test_prequel_completion_backfills_stub_synopsis(self):
        # Completing S2 creates S1's local card; when S1's own synopsis is a
        # MAL stub, walk one more prequel to the first season's real text.
        def handler(method, url, params, data):
            if method == "GET" and "/anime/101" in url:
                return FakeResp({
                    "id": 101, "media_type": "tv", "num_episodes": 12,
                    "related_anime": [{"node": {"id": 100}, "relation_type": "prequel"}],
                })
            if method == "GET" and "/anime/100" in url:
                return FakeResp({
                    "id": 100, "title": "Show S1", "media_type": "tv",
                    "num_episodes": 12, "synopsis": "Second season of Show.",
                    "related_anime": [{"node": {"id": 99}, "relation_type": "prequel"}],
                })
            if method == "GET" and "/anime/99" in url:
                return FakeResp({
                    "id": 99, "synopsis": "The real first season synopsis. " * 10,
                    "related_anime": [],
                })
            return FakeResp({})

        self.session.handler = handler
        self.sync.push_episode(101, 5, 12)
        row = self.store.get_anime(100)
        self.assertIsNotNone(row)
        self.assertTrue(row["synopsis"].startswith("The real first season synopsis"))

    def test_prequel_chain_through_movie(self):
        # MAL often routes S2 -> prequel Movie -> prequel S1 (e.g. Youjo Senki).
        # The movie must not be marked watched, but the chain must continue
        # to S1 and complete it with its real episode count.
        def handler(method, url, params, data):
            if method == "GET" and "/anime/101" in url:
                return FakeResp({
                    "id": 101, "media_type": "tv", "num_episodes": 12,
                    "related_anime": [{"node": {"id": 55, "title": "The Movie"},
                                        "relation_type": "prequel"}],
                })
            if method == "GET" and "/anime/55" in url:
                return FakeResp({
                    "id": 55, "media_type": "movie", "num_episodes": 1,
                    "related_anime": [{"node": {"id": 100, "title": "Show"},
                                        "relation_type": "prequel"}],
                })
            if method == "GET" and "/anime/100" in url:
                return FakeResp({"id": 100, "media_type": "tv", "num_episodes": 12,
                                 "related_anime": []})
            return FakeResp({})

        self.session.handler = handler
        self.sync.push_episode(101, 5, 12)
        patches = self.patches()
        m100 = [c for c in patches if "/anime/100/" in c[1]]
        self.assertEqual(len(m100), 1)
        self.assertEqual(m100[0][4], {"status": "completed", "num_watched_episodes": 12})
        # the movie must NOT be completed
        self.assertEqual([c for c in patches if "/anime/55/" in c[1]], [])
        self.assertTrue(self.store.is_watched(100, 12))

    def test_prequel_completion_respects_dropped(self):
        self.store.set_mal_status(100, "dropped", 3, None, None)

        def handler(method, url, params, data):
            if method == "GET" and "/anime/101" in url:
                return FakeResp({
                    "id": 101, "title": "Show 2", "media_type": "tv", "num_episodes": 12,
                    "related_anime": [{"node": {"id": 100, "title": "Show",
                                                   "media_type": "tv", "num_episodes": 12},
                                        "relation_type": "prequel"}],
                })
            return FakeResp({})

        self.session.handler = handler
        self.sync.push_episode(101, 5, 12)
        patches = self.patches()
        self.assertEqual(len(patches), 1)  # only the new entry; prequel was dropped
        self.assertEqual(patches[0][1], config.MAL_API_BASE + "/anime/101/my_list_status")

    def test_reconcile_pushes_backlog_and_skips_satisfied(self):
        # 49233: locally watched, not on MAL -> push
        self.store.upsert_anime(49233, "Youjo Senki II", "tv", 12)
        self.store.mark_watched(49233, 4)
        # 32615: locally watched but already completed on MAL -> skip
        self.store.upsert_anime(32615, "Youjo Senki", "tv", 12)
        self.store.mark_watched(32615, 4)
        self.store.set_mal_status(32615, "completed", 12, None, None)
        n = self.sync.reconcile()
        self.assertEqual(n, 1)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertIn("/anime/49233/", patches[0][1])
        self.assertEqual(patches[0][4]["num_watched_episodes"], 4)

    def test_reconcile_skips_matching_progress(self):
        self.store.upsert_anime(1, "A", "tv", 12)
        self.store.mark_watched(1, 5)
        self.store.set_mal_status(1, "watching", 5, None, None)
        n = self.sync.reconcile()
        self.assertEqual(n, 0)
        self.assertEqual(self.patches(), [])

    def test_reconcile_movie(self):
        self.store.upsert_anime(7, "Movie", "movie", 1)
        self.store.mark_watched(7)  # episode None
        n = self.sync.reconcile()
        self.assertEqual(n, 1)
        data = self.patches()[0][4]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["num_watched_episodes"], 1)

    def test_retry_pending_clears_on_success(self):
        self.session.handler = lambda m, u, p, d: FakeResp({}, status=500)
        self.sync.push_episode(62322, 5, 12)
        self.assertEqual(len(self.store.list_pending()), 1)
        self.session.handler = lambda m, u, p, d: FakeResp({})
        left = self.sync.retry_pending()
        self.assertEqual(left, 0)
        self.assertEqual(len(self.store.list_pending()), 0)
        self.assertEqual(self.store.get_mal_status(62322)["num_watched_episodes"], 5)

    def test_import_list_two_pages(self):
        calls = []

        def handler(method, url, params, data):
            offset = (params or {}).get("offset", 0)
            calls.append(offset)
            if offset == 0:
                return FakeResp({
                    "data": [{
                        "node": {"id": 1, "title": "A", "media_type": "tv",
                                 "main_picture": {"large": "http://img/1.jpg"}},
                        "list_status": {"status": "watching", "num_episodes_watched": 5, "score": 8,
                                        "updated_at": "2025-01-01T00:00:00+00:00"},
                    }],
                    "paging": {"next": "page2"},
                })
            return FakeResp({
                "data": [{
                    "node": {"id": 2, "title": "B", "media_type": "movie"},
                    "list_status": {"status": "completed", "num_episodes_watched": 1, "score": 9,
                                    "updated_at": "2025-01-02T00:00:00+00:00"},
                }],
                "paging": {},
            })

        self.session.handler = handler
        with mock.patch("animelist.sync.ensure_image", return_value=None):
            n = self.sync.import_list()
        self.assertEqual(n, 2)
        self.assertEqual(calls, [0, 100])
        self.assertEqual(self.store.get_mal_status(1)["num_watched_episodes"], 5)
        self.assertEqual(self.store.get_mal_status(2)["status"], "completed")
        self.assertEqual(self.store.get_anime(1)["title"], "A")


if __name__ == "__main__":
    unittest.main()
