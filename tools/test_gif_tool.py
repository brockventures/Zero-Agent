#!/usr/bin/env python3
"""
Unit tests for gif_tool.py
Tests multi-tier dynamic search: Tenor -> Giphy -> Graceful Skip.
"""
import unittest
from unittest.mock import patch
from tools.gif_tool import (
    get_contextual_gif,
    is_valid_gif_url
)

class TestGifTool(unittest.TestCase):

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_tenor_primary_success(self, mock_rec, mock_hist, mock_tenor, mock_valid):
        mock_tenor.return_value = [
            {"title": "test tenor", "url": "https://tenor.com/view/test-123"}
        ]
        res = get_contextual_gif("test query")
        self.assertEqual(res["source"], "dynamic_tenor")
        self.assertEqual(res["url"], "https://tenor.com/view/test-123")
        mock_rec.assert_called_once_with("https://tenor.com/view/test-123")

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor", return_value=[])
    @patch("tools.gif_tool.search_giphy")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_giphy_fallback_success(self, mock_rec, mock_hist, mock_giphy, mock_tenor, mock_valid):
        mock_giphy.return_value = [
            {"title": "test giphy", "url": "https://giphy.com/gifs/test-giphy-abc456"}
        ]
        res = get_contextual_gif("test query")
        self.assertEqual(res["source"], "dynamic_giphy")
        self.assertEqual(res["url"], "https://giphy.com/gifs/test-giphy-abc456")
        mock_rec.assert_called_once_with("https://giphy.com/gifs/test-giphy-abc456")

    @patch("tools.gif_tool.search_tenor", return_value=[])
    @patch("tools.gif_tool.search_giphy", return_value=[])
    @patch("tools.gif_tool.load_history", return_value=[])
    def test_graceful_skip_when_both_fail(self, mock_hist, mock_giphy, mock_tenor):
        res = get_contextual_gif("unfindable query")
        self.assertEqual(res["source"], "skip")
        self.assertIsNone(res["url"])
        self.assertIsNone(res["title"])

    @patch("tools.gif_tool.is_valid_gif_url")
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history")
    def test_history_filtering(self, mock_hist, mock_tenor, mock_valid):
        used_url = "https://tenor.com/view/already-used-1"
        fresh_url = "https://tenor.com/view/fresh-2"
        mock_hist.return_value = [used_url]
        mock_tenor.return_value = [
            {"title": "used", "url": used_url},
            {"title": "fresh", "url": fresh_url}
        ]
        mock_valid.return_value = True
        res = get_contextual_gif("query")
        self.assertEqual(res["url"], fresh_url)

if __name__ == "__main__":
    unittest.main()
