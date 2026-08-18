import unittest

from animelist.mal import (
    MIN_REAL_SYNOPSIS,
    MALClient,
    franchise_synopsis,
    is_real_synopsis,
)


class SynopsisHeuristicsTest(unittest.TestCase):
    def test_stub_is_not_real(self):
        # MAL's "Second season of X." stubs are ~30-70 chars.
        self.assertFalse(is_real_synopsis("Second season of Show."))
        self.assertFalse(is_real_synopsis(""))
        self.assertFalse(is_real_synopsis("   "))
        self.assertFalse(is_real_synopsis(None))

    def test_long_is_real(self):
        self.assertTrue(is_real_synopsis("x" * MIN_REAL_SYNOPSIS))
        self.assertTrue(is_real_synopsis("x" * 500))

    def test_franchise_synopsis_walks_prequels(self):
        catalog = {
            101: {"synopsis": "Second season of Show.",
                  "related_anime": [{"node": {"id": 100}, "relation_type": "prequel"}]},
            100: {"synopsis": "A real first-season synopsis. " * 8, "related_anime": []},
        }

        def fetch(mid):
            return catalog[mid]

        self.assertEqual(franchise_synopsis(fetch, 101), catalog[100]["synopsis"].strip())

    def test_franchise_synopsis_returns_own_when_real(self):
        catalog = {5: {"synopsis": "A genuinely long synopsis. " * 20, "related_anime": []}}
        self.assertEqual(franchise_synopsis(lambda m: catalog[m], 5), catalog[5]["synopsis"].strip())

    def test_franchise_synopsis_walks_through_movies(self):
        # MAL often routes S2 -> prequel Movie -> prequel S1.
        catalog = {
            101: {"synopsis": "stub", "related_anime": [{"node": {"id": 55}, "relation_type": "prequel"}]},
            55: {"synopsis": "Movie synopsis stub", "related_anime": [{"node": {"id": 100}, "relation_type": "prequel"}]},
            100: {"synopsis": "The real first season synopsis. " * 10, "related_anime": []},
        }
        self.assertEqual(franchise_synopsis(lambda m: catalog[m], 101), catalog[100]["synopsis"].strip())

    def test_franchise_synopsis_handles_cycles_and_missing(self):
        catalog = {
            1: {"synopsis": "stub", "related_anime": [{"node": {"id": 2}, "relation_type": "prequel"}]},
            2: {"synopsis": "stub", "related_anime": [{"node": {"id": 1}, "relation_type": "prequel"}]},
        }
        self.assertEqual(franchise_synopsis(lambda m: catalog[m], 1), "")

    def test_franchise_synopsis_skips_broken_fetches(self):
        def fetch(mid):
            if mid == 1:
                raise RuntimeError("network down")
            return {"synopsis": "real synopsis " * 20, "related_anime": []}

        self.assertEqual(franchise_synopsis(fetch, 1), "")


# Reuse the real client methods against a stub that only implements ``anime``.
class _DummyClient:
    def __init__(self, catalog):
        self.catalog = catalog

    def anime(self, mal_id, fields=""):
        return dict(self.catalog[mal_id])


_DummyClient.find_franchise_synopsis = MALClient.find_franchise_synopsis
_DummyClient.details_with_synopsis = MALClient.details_with_synopsis


class DetailsWithSynopsisTest(unittest.TestCase):
    def test_replaces_stub_with_first_season_synopsis(self):
        catalog = {
            101: {"id": 101, "title": "Show 2nd Season", "synopsis": "Second season of Show.",
                  "related_anime": [{"node": {"id": 100}, "relation_type": "prequel"}]},
            100: {"id": 100, "title": "Show", "synopsis": "The real first season synopsis. " * 10,
                  "related_anime": []},
        }
        d = _DummyClient(catalog).details_with_synopsis(101)
        self.assertEqual(d["synopsis"], catalog[100]["synopsis"].strip())
        self.assertEqual(d["title"], "Show 2nd Season")

    def test_keeps_real_synopsis(self):
        catalog = {5: {"id": 5, "synopsis": "A proper synopsis. " * 30, "related_anime": []}}
        d = _DummyClient(catalog).details_with_synopsis(5)
        self.assertEqual(d["synopsis"], catalog[5]["synopsis"])

    def test_keeps_stub_when_no_real_found(self):
        catalog = {7: {"id": 7, "synopsis": "Second season of Show.", "related_anime": []}}
        d = _DummyClient(catalog).details_with_synopsis(7)
        self.assertEqual(d["synopsis"], "Second season of Show.")


if __name__ == "__main__":
    unittest.main()
