import unittest
from lyrics_utils import *


class TestNames(unittest.TestCase):
    def test_clean_title(self):
        self.assertEqual(clean_title("Song (feat. X)"), "Song")
        self.assertEqual(clean_title("Song - Remastered 2011"), "Song")
        self.assertEqual(clean_title("Song - 2011 Remaster"), "Song")
        self.assertEqual(clean_title("Песня - Ремастер 2015"), "Песня")
        self.assertEqual(clean_title("Spider-Man"), "Spider-Man")
        self.assertEqual(clean_title("(Intro)"), "(Intro)")

    def test_first_artist(self):
        self.assertEqual(first_artist("A, B"), "A")
        self.assertEqual(first_artist("A & B"), "A")
        self.assertEqual(first_artist("A feat. B"), "A")
        self.assertEqual(first_artist("AC/DC"), "AC/DC")
        self.assertEqual(first_artist("Скриптонит"), "Скриптонит")

    def test_artist_match(self):
        self.assertTrue(artist_match("Монеточка", "монеточка"))
        self.assertFalse(artist_match("Монеточка", "Другой"))
        self.assertFalse(artist_match("", "x"))


class TestLrc(unittest.TestCase):
    def test_variants(self):
        r = parse_lrc("[ar:Artist]\n[00:12.34]hello\n[00:15]no decimals\n[00:20.00]\n[01:02.5][02:03.50]double\n[00:30:25]colon")
        self.assertEqual(r[0], (12.34, "hello"))
        self.assertIn((15.0, "no decimals"), r)
        self.assertIn((20.0, ""), r)
        self.assertIn((62.5, "double"), r)
        self.assertIn((123.5, "double"), r)
        self.assertIn((30.25, "colon"), r)
        self.assertEqual([t for t, _ in r], sorted(t for t, _ in r))

    def test_enhanced_word_stamps(self):
        r = parse_lrc("[00:10.00]<00:10.00>Hel<00:10.50>lo")
        self.assertEqual(r, [(10.0, "Hello")])

    def test_empty(self):
        self.assertEqual(parse_lrc(""), [])
        self.assertEqual(parse_lrc("[00:01.00]\n[00:02.00]"), [])
        self.assertEqual(parse_lrc("no stamps here"), [])


class TestPick(unittest.TestCase):
    items = [
        {"artistName": "Other", "duration": 200, "syncedLyrics": "[00:01.00]wrong artist"},
        {"artistName": "Band", "duration": 260, "syncedLyrics": "[00:01.00]wrong version"},
        {"artistName": "Band", "duration": 201.5, "syncedLyrics": "[00:01.00]ok", "plainLyrics": "ok plain"},
        {"artistName": "Band", "duration": 199, "syncedLyrics": None, "plainLyrics": "plain only"},
    ]

    def test_duration_filter(self):
        s, p = pick_lrclib(self.items, "Band", 200, strict_artist=True)
        self.assertEqual(s, "[00:01.00]ok")
        self.assertEqual(p, "plain only")

    def test_no_duration_known(self):
        s, _ = pick_lrclib(self.items, "Band", None, strict_artist=True)
        self.assertEqual(s, "[00:01.00]wrong version")

    def test_nothing_within_tolerance(self):
        self.assertEqual(pick_lrclib(self.items, "Band", 100), (None, None))

    def test_garbage(self):
        self.assertEqual(pick_lrclib(None, "x", 1), (None, None))
        self.assertEqual(pick_lrclib([1, "a", None], "x", 1), (None, None))


class TestUnsynced(unittest.TestCase):
    def test_spread(self):
        r = spread_lines(["a", "", "b", "c"], 200)
        self.assertEqual([x for _, x in r], ["a", "b", "c"])
        times = [t for t, _ in r]
        self.assertEqual(times, sorted(times))
        self.assertLess(times[-1], 200)
        self.assertGreater(times[0], 0)

    def test_spread_unknown_duration(self):
        self.assertEqual(spread_lines(["a", "b"], 0), [(0.0, "a"), (4.0, "b")])

    def test_genius_clean(self):
        raw = "Song Lyrics\n[Verse 1]\nline one\n\nline two\nYou might also like\nlast line42Embed"
        self.assertEqual(clean_genius_lines(raw), ["line one", "line two", "last line"])


class TestEase(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(ease_out_cubic(0), 0.0)
        self.assertEqual(ease_out_cubic(1), 1.0)
        self.assertEqual(ease_out_cubic(5), 1.0)
        self.assertGreater(ease_out_cubic(0.5), 0.5)


if __name__ == "__main__":
    unittest.main()
