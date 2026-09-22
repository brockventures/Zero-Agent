#!/usr/bin/env python3
"""Unit tests for Web QA & Browser Evaluation Tool."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

if "playwright" not in sys.modules:
    try:
        import playwright
    except ImportError:
        sys.modules["playwright"] = MagicMock()
        sys.modules["playwright.sync_api"] = MagicMock()

from tools.web_qa import run_web_qa


class TestWebQA(unittest.TestCase):

    @patch("playwright.sync_api.sync_playwright")
    def test_run_web_qa_mock_success(self, mock_playwright):
        # Setup mock playwright hierarchy
        mock_p = MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_p

        mock_browser = MagicMock()
        mock_p.chromium.launch.return_value = mock_browser

        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_page.goto.return_value = mock_resp
        mock_page.title.return_value = "Test App"
        mock_page.url = "http://localhost:3000"

        # Mock elements
        h1_mock = MagicMock()
        h1_mock.inner_text.return_value = "Dashboard"
        mock_page.locator.return_value.all.return_value = [h1_mock]
        mock_page.locator.return_value.count.return_value = 1
        mock_page.get_by_text.return_value.count.return_value = 1

        result = run_web_qa(
            url="http://localhost:3000",
            screenshot=False,
            expect_selectors=["h1"],
            expect_texts=["Dashboard"],
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["title"], "Test App")
        self.assertEqual(len(result["assertions"]), 2)
        self.assertTrue(all(a["passed"] for a in result["assertions"]))

    @patch("playwright.sync_api.sync_playwright")
    def test_run_web_qa_assertion_failure(self, mock_playwright):
        mock_p = MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_p
        mock_browser = MagicMock()
        mock_p.chromium.launch.return_value = mock_browser
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_page.goto.return_value = mock_resp
        mock_page.title.return_value = "Broken App"
        mock_page.url = "http://localhost:3000"

        # Simulate missing element
        mock_page.locator.return_value.all.return_value = []
        mock_page.locator.return_value.count.return_value = 0

        result = run_web_qa(
            url="http://localhost:3000",
            screenshot=False,
            expect_selectors=[".missing-btn"],
        )

        self.assertFalse(result["success"])
        self.assertEqual(len(result["assertions"]), 1)
        self.assertFalse(result["assertions"][0]["passed"])


if __name__ == "__main__":
    unittest.main()
