#!/usr/bin/env python3
"""
Unit tests for gif_tool.py
Tests multi-tier dynamic search: Tenor -> Giphy -> Graceful Skip.
"""
import unittest
from unittest.mock import patch
from tools.gif_tool import (
    clean_slug_title,
    is_ocr_safe,
    extract_gif_ocr,
    get_contextual_gif,
    is_valid_gif_url,
    detect_franchise,
    check_cooldown,
    get_cooldown_summary,
    get_history_urls,
    load_history
)

class TestGifTool(unittest.TestCase):

    def test_clean_slug_title(self):
        self.assertEqual(
            clean_slug_title("arrested-development-lucille-bluth-lock-the-door-gif-26514757"),
            "Arrested Development Lucille Bluth Lock The"
        )
        self.assertEqual(
            clean_slug_title("ive-made-a-huge-mistake-gif-12345"),
            "I've Made A Huge Mistake"
        )
        self.assertEqual(
            clean_slug_title("dont-look-back-in-anger-999"),
            "Don't Look Back In Anger"
        )
        self.assertEqual(
            clean_slug_title("intergalactic-quality-gif-hd-trending-dance-moves"),
            "Dance Moves"
        )
        self.assertEqual(clean_slug_title(""), "Reaction GIF")
        self.assertEqual(clean_slug_title(None), "Reaction GIF")

    def test_is_ocr_safe(self):
        self.assertTrue(is_ocr_safe("I've made a huge mistake"))
        self.assertTrue(is_ocr_safe(""))
        self.assertTrue(is_ocr_safe("This is fine."))
        # Word boundary protects benign substrings
        self.assertTrue(is_ocr_safe("we met at the cocktail lounge"))
        # Blocked terms rejected
        self.assertFalse(is_ocr_safe("get out you faggot"))
        self.assertFalse(is_ocr_safe("go kill yourself right now"))
        self.assertFalse(is_ocr_safe("white nazi propaganda"))

    def test_extract_gif_ocr_empty(self):
        self.assertEqual(extract_gif_ocr(""), "")
        self.assertEqual(extract_gif_ocr("http://invalid.local/fake.gif"), "")

    def test_detect_franchise(self):
        self.assertEqual(detect_franchise("arrested development lucille bluth"), "arrested_development")
        self.assertEqual(detect_franchise("good-for-her-lucille-bluth-gif"), "arrested_development")
        self.assertEqual(detect_franchise("dead dove do not eat"), "arrested_development")
        self.assertEqual(detect_franchise("gilfoyle server fire"), "silicon_valley")
        self.assertEqual(detect_franchise("richard-hendricks-pied-piper"), "silicon_valley")
        self.assertEqual(detect_franchise("larry david pretty good"), "curb_your_enthusiasm")
        self.assertEqual(detect_franchise("tim robinson hot dog suit"), "i_think_you_should_leave")
        self.assertEqual(detect_franchise("jeff winger community wow"), "community")
        self.assertEqual(detect_franchise("random cat typing"), None)
        self.assertEqual(detect_franchise(""), None)
        self.assertEqual(detect_franchise(None), None)

    def test_cooldown_calculation(self):
        mock_history = [
            {"url": "u1", "franchise": "curb_your_enthusiasm"},
            {"url": "u2", "franchise": "silicon_valley"},
            {"url": "u3", "franchise": "community"},
            {"url": "u4", "franchise": "arrested_development"},
        ]
        # arrested_development is distance 1, default threshold 8 -> True
        is_cd, dist, thresh = check_cooldown("arrested_development", history=mock_history)
        self.assertTrue(is_cd)
        self.assertEqual(dist, 1)
        self.assertEqual(thresh, 8)

        # community is distance 2, default threshold 5 -> True
        is_cd, dist, thresh = check_cooldown("community", history=mock_history)
        self.assertTrue(is_cd)
        self.assertEqual(dist, 2)

        # Unused franchise has distance 999 -> False
        is_cd, dist, thresh = check_cooldown("30_rock", history=mock_history)
        self.assertFalse(is_cd)

    def test_history_legacy_strings_support(self):
        legacy = [
            "https://tenor.com/view/already-used-1",
            "https://tenor.com/view/good-for-her-arrested-development-lucille-bluth-gif-11778278665179077372"
        ]
        urls = get_history_urls(legacy)
        self.assertIn("https://tenor.com/view/already-used-1", urls)
        self.assertIn("https://tenor.com/view/good-for-her-arrested-development-lucille-bluth-gif-11778278665179077372", urls)

        # check cooldown works on legacy list
        is_cd, dist, thresh = check_cooldown("arrested_development", history=legacy)
        self.assertTrue(is_cd)
        self.assertEqual(dist, 1)

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_tenor_primary_success(self, mock_rec, mock_hist, mock_tenor, mock_valid):
        mock_tenor.return_value = [
            {"title": "Test Tenor", "url": "https://tenor.com/view/test-123", "media_url": None}
        ]
        res = get_contextual_gif("test query", run_ocr=False)
        self.assertEqual(res["source"], "dynamic_tenor")
        self.assertEqual(res["url"], "https://tenor.com/view/test-123")
        self.assertEqual(res["markdown"], "[Test Tenor](https://tenor.com/view/test-123)")
        mock_rec.assert_called_once_with(
            "https://tenor.com/view/test-123",
            query="test query",
            title="Test Tenor",
            franchise=None
        )

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.extract_gif_ocr")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_tenor_ocr_safety_rejection(self, mock_rec, mock_hist, mock_ocr, mock_tenor, mock_valid):
        # First candidate contains toxic text; second candidate is clean
        mock_tenor.return_value = [
            {"title": "Toxic Candidate", "url": "https://tenor.com/view/toxic-1", "media_url": "https://media.tenor.com/toxic.gif"},
            {"title": "Clean Candidate", "url": "https://tenor.com/view/clean-2", "media_url": "https://media.tenor.com/clean.gif"}
        ]
        mock_ocr.side_effect = ["kill yourself", "I've made a huge mistake"]
        res = get_contextual_gif("test query", run_ocr=True)
        self.assertEqual(res["source"], "dynamic_tenor")
        self.assertEqual(res["url"], "https://tenor.com/view/clean-2")
        self.assertEqual(res["title"], "Clean Candidate")
        self.assertEqual(res["markdown"], "[Clean Candidate](https://tenor.com/view/clean-2)")
        mock_rec.assert_called_once_with(
            "https://tenor.com/view/clean-2",
            query="test query",
            title="Clean Candidate",
            franchise="arrested_development"
        )

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor", return_value=[])
    @patch("tools.gif_tool.search_giphy")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_giphy_fallback_success(self, mock_rec, mock_hist, mock_giphy, mock_tenor, mock_valid):
        mock_giphy.return_value = [
            {"title": "Test Giphy", "url": "https://giphy.com/gifs/test-giphy-abc456", "media_url": None}
        ]
        res = get_contextual_gif("test query", run_ocr=False)
        self.assertEqual(res["source"], "dynamic_giphy")
        self.assertEqual(res["url"], "https://giphy.com/gifs/test-giphy-abc456")
        self.assertEqual(res["markdown"], "[Test Giphy](https://giphy.com/gifs/test-giphy-abc456)")
        mock_rec.assert_called_once_with(
            "https://giphy.com/gifs/test-giphy-abc456",
            query="test query",
            title="Test Giphy",
            franchise=None
        )

    @patch("tools.gif_tool.search_tenor", return_value=[])
    @patch("tools.gif_tool.search_giphy", return_value=[])
    @patch("tools.gif_tool.load_history", return_value=[])
    def test_graceful_skip_when_both_fail(self, mock_hist, mock_giphy, mock_tenor):
        res = get_contextual_gif("unfindable query")
        self.assertEqual(res["source"], "skip")
        self.assertIsNone(res["url"])
        self.assertIsNone(res["title"])
        self.assertIsNone(res["markdown"])

    @patch("tools.gif_tool.record_history")
    @patch("tools.gif_tool.is_valid_gif_url")
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history")
    def test_history_filtering(self, mock_hist, mock_tenor, mock_valid, mock_rec):
        used_url = "https://tenor.com/view/already-used-1"
        fresh_url = "https://tenor.com/view/fresh-2"
        mock_hist.return_value = [{"url": used_url, "franchise": None}]
        mock_tenor.return_value = [
            {"title": "used", "url": used_url, "media_url": None},
            {"title": "fresh", "url": fresh_url, "media_url": None}
        ]
        mock_valid.return_value = True
        res = get_contextual_gif("query", run_ocr=False)
        self.assertEqual(res["url"], fresh_url)

    @patch("tools.gif_tool.check_cooldown", return_value=(True, 1, 8))
    def test_explicit_query_cooldown_blocked(self, mock_cd):
        res = get_contextual_gif("arrested development lucille wink")
        self.assertEqual(res["source"], "cooldown_blocked")
        self.assertIn("cooldown", res["error"].lower())
        self.assertEqual(res["franchise"], "arrested_development")

    @patch("tools.gif_tool.record_history")
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history")
    def test_candidate_filtering_skips_cooled_down_franchise(self, mock_hist, mock_tenor, mock_valid, mock_rec):
        mock_hist.return_value = [
            {"url": "https://tenor.com/view/ad-1", "franchise": "arrested_development", "query": "", "title": ""}
        ]
        mock_tenor.return_value = [
            {"title": "Lucille Bluth Wink", "url": "https://tenor.com/view/lucille-bluth-wink-1", "media_url": None},
            {"title": "Gilfoyle Smug", "url": "https://tenor.com/view/gilfoyle-smug-2", "media_url": None}
        ]
        res = get_contextual_gif("smug stare", run_ocr=False)
        self.assertEqual(res["url"], "https://tenor.com/view/gilfoyle-smug-2")
        self.assertEqual(res["franchise"], "silicon_valley")

    @patch("tools.gif_tool.record_history")
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history")
    def test_force_bypasses_cooldown(self, mock_hist, mock_tenor, mock_valid, mock_rec):
        mock_hist.return_value = [
            {"url": "https://tenor.com/view/ad-1", "franchise": "arrested_development", "query": "", "title": ""}
        ]
        mock_tenor.return_value = [
            {"title": "Lucille Bluth Wink", "url": "https://tenor.com/view/lucille-bluth-wink-1", "media_url": None}
        ]
        res = get_contextual_gif("lucille bluth wink", run_ocr=False, force=True)
        self.assertEqual(res["url"], "https://tenor.com/view/lucille-bluth-wink-1")
        self.assertEqual(res["source"], "dynamic_tenor")

if __name__ == "__main__":
    unittest.main()
