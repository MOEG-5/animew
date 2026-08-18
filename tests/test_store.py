import tempfile
import unittest
from pathlib import Path

from animelist.store import Store


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_mark_watched_idempotent(self):
        self.store.upsert_anime(1, "Show", "tv", 12, mal_url="https://myanimelist.net/anime/1")
        self.assertTrue(self.store.mark_watched(1, 5))
        self.assertFalse(self.store.mark_watched(1, 5))  # already marked
        self.assertTrue(self.store.is_watched(1, 5))
        self.assertFalse(self.store.is_watched(1, 6))

    def test_movie_watched(self):
        self.store.upsert_anime(2, "Movie", "movie")
        self.assertTrue(self.store.mark_watched(2))  # episode=None
        self.assertFalse(self.store.mark_watched(2))
        self.assertTrue(self.store.is_watched(2))
        self.assertFalse(self.store.is_watched(2, 1))  # distinct from episode 1

    def test_latest_watched_order(self):
        self.store.upsert_anime(1, "A")
        self.store.upsert_anime(2, "B")
        self.store.mark_watched(1, 3)
        self.store.mark_watched(2, 1)
        rows = self.store.latest_watched()
        self.assertEqual([r["mal_id"] for r in rows], [2, 1])

    def test_latest_watched_groups_by_anime(self):
        self.store.upsert_anime(1, "A")
        self.store.mark_watched(1, 3)
        self.store.mark_watched(1, 5)
        rows = self.store.latest_watched()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["episode"], 5)  # latest episode of the show

    def test_mark_episodes_watched(self):
        self.store.upsert_anime(1, "A")
        self.store.mark_episodes_watched(1, 5)
        self.assertTrue(self.store.is_watched(1, 1))
        self.assertTrue(self.store.is_watched(1, 5))
        self.assertFalse(self.store.is_watched(1, 6))
        self.store.mark_episodes_watched(1, 7)  # extends, keeps existing
        self.assertTrue(self.store.is_watched(1, 7))

    def test_collection_unifies_local_and_mal(self):        # 1: locally watched only; 2: on MAL list with progress (not started);
        # 3: on MAL list plan_to_watch (excluded); 4: local + MAL progress
        self.store.upsert_anime(1, "Local only", "tv")
        self.store.mark_watched(1, 5)
        self.store.upsert_anime(2, "Mal not started", "tv")
        self.store.set_mal_status(2, "watching", 0, None, "2025-01-01T00:00:00+00:00")
        self.store.upsert_anime(3, "Plan to watch", "tv")
        self.store.set_mal_status(3, "plan_to_watch", 0, None, None)
        self.store.upsert_anime(4, "Both", "tv")
        self.store.mark_watched(4, 3)
        self.store.set_mal_status(4, "watching", 12, None, "2025-01-01T00:00:00+00:00")
        rows = {r["mal_id"]: r for r in self.store.collection()}
        self.assertEqual(set(rows), {1, 2, 4})
        self.assertEqual(rows[1]["episode"], 5)
        self.assertEqual(rows[2]["episode"], 0)  # not started
        self.assertEqual(rows[2]["mal_status"], "watching")
        self.assertEqual(rows[4]["episode"], 12)  # max(local 3, mal 12)

    def test_upsert_updates_and_preserves(self):
        self.store.upsert_anime(1, "Old Title", "tv", 12)
        self.store.upsert_anime(1, "New Title", "tv", num_episodes=24)
        row = self.store.get_anime(1)
        self.assertEqual(row["title"], "New Title")
        self.assertEqual(row["num_episodes"], 24)
        self.assertEqual(row["media_type"], "tv")

    def test_remove_anime(self):
        self.store.upsert_anime(1, "Show", "tv")
        self.store.mark_watched(1, 5)
        self.store.remove_anime(1)
        self.assertIsNone(self.store.get_anime(1))
        self.assertFalse(self.store.is_watched(1, 5))

    def test_store_cross_thread(self):
        # The widget shares one Store between the UI thread and the watcher
        # thread; a connection created in one thread must work from another.
        errors = []

        def worker():
            try:
                self.store.upsert_anime(7, "T")
                self.store.mark_watched(7, 1)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        import threading

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        self.assertEqual(errors, [])
        self.assertTrue(self.store.is_watched(7, 1))

    def test_mal_list_and_pending(self):
        self.assertIsNone(self.store.get_mal_status(1))
        self.store.set_mal_status(1, "watching", 5, 8, None)
        row = self.store.get_mal_status(1)
        self.assertEqual(row["num_watched_episodes"], 5)
        self.assertEqual(row["score"], 8)
        self.store.add_pending(1, 6, 12)
        self.store.add_pending(1, 6, 12)  # dedupe
        self.assertEqual(len(self.store.list_pending()), 1)
        self.store.remove_pending(self.store.list_pending()[0]["id"])
        self.assertEqual(self.store.list_pending(), [])

    def test_badge_map(self):
        self.store.upsert_anime(2, "Old watch", "tv")
        self.store.mark_watched(2, 1)
        self.store.add_new_content(2, 99, "sequel", "Sequel", badge_ids=[2])
        bm = self.store.badge_map()
        self.assertEqual(bm.get(2), [99])
        # legacy rows (no badge_ids) badge the source only
        self.store.add_new_content(3, 88, "sequel", "S2")
        bm = self.store.badge_map()
        self.assertEqual(bm.get(3), [88])
        # badge clears once the target is watched
        self.store.upsert_anime(99, "Sequel", "tv")
        self.store.mark_watched(99, 1)
        bm = self.store.badge_map()
        self.assertNotIn(2, bm)
        self.assertEqual(bm.get(3), [88])

    def test_new_content_dedupe_and_notified(self):
        self.assertTrue(self.store.add_new_content(1, 2, "sequel", "S2"))
        self.assertFalse(self.store.add_new_content(1, 2, "sequel", "S2"))
        self.assertEqual(self.store.active_new_targets(1), [2])
        self.assertEqual(len(self.store.unnotified_new_content()), 1)
        self.store.set_new_content_notified(1, 2)
        self.assertEqual(self.store.unnotified_new_content(), [])

    def test_migration_adds_target_title_column(self):
        import sqlite3

        db = Path(self.tmp.name) / "mig.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE new_content (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "source_mal_id INTEGER NOT NULL, target_mal_id INTEGER NOT NULL, "
            "relation TEXT, detected_at TEXT NOT NULL, "
            "notified INTEGER NOT NULL DEFAULT 0, dismissed INTEGER NOT NULL DEFAULT 0, "
            "UNIQUE(source_mal_id, target_mal_id))"
        )
        conn.commit()
        conn.close()
        s = Store(db)
        cols = {r[1] for r in s._db.execute("PRAGMA table_info(new_content)")}
        self.assertIn("target_title", cols)
        self.assertIn("badge_ids", cols)
        s.close()

    def test_settings_helpers(self):
        from animelist.store import (
            COLUMNS_KEY,
            IMAGE_WIDTH_KEY,
            ROWS_KEY,
            TAGS_KEY,
            THRESHOLD_KEY,
            get_card_image_width,
            get_grid_columns,
            get_grid_rows,
            get_release_tags,
            get_watch_threshold,
            set_release_tags,
        )

        self.assertEqual(get_release_tags(self.store), [])
        set_release_tags(self.store, ["Demo", "Second", " "])
        self.assertEqual(get_release_tags(self.store), ["Demo", "Second"])
        self.assertAlmostEqual(get_watch_threshold(self.store), 600.0)
        self.store.set_setting(THRESHOLD_KEY, "15")
        self.assertAlmostEqual(get_watch_threshold(self.store), 900.0)
        self.assertEqual(get_grid_columns(self.store), 2)
        self.assertEqual(get_grid_rows(self.store), 3)
        self.assertEqual(get_card_image_width(self.store), 140)
        self.store.set_setting(COLUMNS_KEY, "4")
        self.store.set_setting(ROWS_KEY, "9")  # clamps to 8
        self.store.set_setting(IMAGE_WIDTH_KEY, "999")  # clamps to 450
        self.assertEqual(get_grid_columns(self.store), 4)
        self.assertEqual(get_grid_rows(self.store), 8)
        self.assertEqual(get_card_image_width(self.store), 450)
        self.assertFalse(self.store.has_data())
        self.store.upsert_anime(1, "A")
        self.assertTrue(self.store.has_data())

    def test_rows_needing_details(self):
        self.store.upsert_anime(1, "A", "tv", 12, None)                       # no synopsis
        self.store.upsert_anime(2, "B", "tv", 12, "Second season of B.")      # stub
        self.store.upsert_anime(3, "C", "tv", None, "A real synopsis " * 30)  # num missing
        self.store.upsert_anime(4, "D", "tv", 12, "A real synopsis " * 30)    # complete
        ids = {r["mal_id"] for r in self.store.rows_needing_details()}
        self.assertEqual(ids, {1, 2, 3})

    def test_rows_needing_details_includes_orphans(self):
        # Orphans: watched/mal_list rows with no anime row (e.g. prequels
        # completed by franchise sync before they got a local card).
        self.store.upsert_anime(1, "A", "tv", 12, "A real synopsis " * 30)
        self.store.mark_watched(5)                                    # orphan
        self.store.set_mal_status(6, "watching", 1, None, None)       # orphan
        ids = {r["mal_id"] for r in self.store.rows_needing_details()}
        self.assertIn(5, ids)
        self.assertIn(6, ids)
        self.assertNotIn(1, ids)

    def test_settings(self):
        self.assertIsNone(self.store.get_setting("nope"))
        self.store.set_setting("last_check", "2025-08-17T12:00:00")
        self.assertEqual(self.store.get_setting("last_check"), "2025-08-17T12:00:00")
        self.assertEqual(self.store.get_setting("last_check", "fallback"), "2025-08-17T12:00:00")


if __name__ == "__main__":
    unittest.main()
