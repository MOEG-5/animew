import unittest

from animelist.widget import wrap_tooltip


class TooltipTest(unittest.TestCase):
    def test_short_text_passthrough(self):
        self.assertEqual(wrap_tooltip("Hello world"), "Hello world")

    def test_wraps_to_column_width(self):
        text = "word " * 100
        out = wrap_tooltip(text, width=40)
        for line in out.split("\n"):
            self.assertLessEqual(len(line), 40)
        # no words lost or gained
        self.assertEqual(" ".join(out.split()), " ".join(text.split()))

    def test_normalizes_whitespace_and_newlines(self):
        out = wrap_tooltip("line one\n\n   line two")
        self.assertNotIn("\n\n", out)
        self.assertNotIn("   ", out)
        self.assertEqual(out, "line one line two")

    def test_caps_length_with_ellipsis(self):
        out = wrap_tooltip("a" * 600)
        self.assertLess(len(out.replace("\n", "")), 600)
        self.assertTrue(out.endswith("…"))

    def test_empty(self):
        self.assertEqual(wrap_tooltip(""), "")
        self.assertEqual(wrap_tooltip("   "), "")


if __name__ == "__main__":
    unittest.main()
