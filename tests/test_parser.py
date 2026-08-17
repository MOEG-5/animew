import unittest

from animelist.parser import parse_title

TAGS = ["Demo"]


class TestParser(unittest.TestCase):
    def test_episode(self):
        p = parse_title("[Demo] Lv999 no Murabito - 05 (1080p) [ABC123].mkv", TAGS)
        self.assertEqual(p.kind, "episode")
        self.assertEqual(p.anime_name, "Lv999 no Murabito")
        self.assertEqual(p.episode, 5)

    def test_name_with_dash(self):
        p = parse_title("[Demo] 86 - Eighty Six - 05 (1080p) [ABC].mkv", TAGS)
        self.assertEqual(p.kind, "episode")
        self.assertEqual(p.anime_name, "86 - Eighty Six")
        self.assertEqual(p.episode, 5)

    def test_name_ending_in_digits(self):
        p = parse_title("[Demo] 86 - 05 (1080p) [ABC].mkv", TAGS)
        self.assertEqual(p.anime_name, "86")
        self.assertEqual(p.episode, 5)

    def test_versioned_episode(self):
        p = parse_title("[Demo] Some Show - 05v2 (1080p) [ABC].mkv", TAGS)
        self.assertEqual(p.kind, "episode")
        self.assertEqual(p.episode, 5)

    def test_movie(self):
        p = parse_title("[Demo] Suzume (2022) (1080p) [ABC].mkv", TAGS)
        self.assertEqual(p.kind, "movie")
        self.assertEqual(p.anime_name, "Suzume")
        self.assertIsNone(p.episode)

    def test_batch_defensive(self):
        p = parse_title("[Demo] Old Show - 01-12 (1080p) [ABC].mkv", TAGS)
        self.assertEqual(p.kind, "batch")
        self.assertEqual(p.anime_name, "Old Show")
        self.assertEqual(p.episode, 1)
        self.assertEqual(p.episode_end, 12)

    def test_wrong_tag_ignored(self):
        self.assertIsNone(parse_title("[Second] Some Show - 05 (1080p).mkv", TAGS))
        self.assertIsNone(parse_title("/home/user/Videos/vacation.mp4", TAGS))
        self.assertIsNone(parse_title("", TAGS))

    def test_full_path_handling(self):
        p = parse_title("/media/Anime/[Demo] Test Show - 03 (1080p).mkv", TAGS)
        self.assertEqual(p.anime_name, "Test Show")
        self.assertEqual(p.episode, 3)

    def test_file_url_prefix(self):
        p = parse_title("file:///media/Anime/[Demo] Test - 07 (720p).mkv", TAGS)
        self.assertEqual(p.anime_name, "Test")
        self.assertEqual(p.episode, 7)

    def test_lowercase_tag(self):
        p = parse_title("[demo] Lowercase - 02 (1080p).mkv", TAGS)
        self.assertEqual(p.kind, "episode")
        self.assertEqual(p.episode, 2)

    # -- F14: configurable release tags -----------------------------------------

    def test_empty_tags_match_nothing(self):
        self.assertIsNone(parse_title("[Demo] Show - 01 (1080p).mkv", []))
        self.assertIsNone(parse_title("[Demo] Show - 01 (1080p).mkv"))  # default None

    def test_multiple_tags_any_matches(self):
        tags = ["Second", "Demo"]
        p = parse_title("[Demo] Show - 01 (1080p).mkv", tags)
        self.assertEqual(p.kind, "episode")
        p2 = parse_title("[Second] Show - 01 (1080p).mkv", tags)
        self.assertEqual(p2.kind, "episode")

    def test_tag_case_insensitive(self):
        p = parse_title("[DEMO] Show - 01 (1080p).mkv", ["demo"])
        self.assertEqual(p.kind, "episode")


if __name__ == "__main__":
    unittest.main()
