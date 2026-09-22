#!/usr/bin/env python3
"""Unit tests for capture_screenshot tool."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

if "playwright" not in sys.modules:
    try:
        import playwright
    except ImportError:
        sys.modules["playwright"] = MagicMock()
        sys.modules["playwright.sync_api"] = MagicMock()

from tools.capture_screenshot import (
    VIEWPORT_PRESETS,
    capture_and_deliver,
    capture_screenshot,
    get_active_brain_dir,
    parse_viewport,
)


class TestCaptureScreenshot(unittest.TestCase):

    def test_parse_viewport_presets(self):
        self.assertEqual(parse_viewport("desktop"), (1280, 800))
        self.assertEqual(parse_viewport("mobile"), (390, 844))
        self.assertEqual(parse_viewport("tablet"), (820, 1180))
        self.assertEqual(parse_viewport("desktop-hd"), (1920, 1080))

    def test_parse_viewport_custom(self):
        self.assertEqual(parse_viewport("1920x1080"), (1920, 1080))
        self.assertEqual(parse_viewport("800x600"), (800, 600))
        self.assertEqual(parse_viewport("invalid"), (1280, 800))

    @patch("tools.capture_screenshot.BRAIN_ROOT")
    def test_get_active_brain_dir(self, mock_brain_root):
        mock_dir1 = MagicMock(spec=Path)
        mock_dir1.is_dir.return_value = True
        mock_dir1.name = "conv-1"
        mock_dir1.stat.return_value.st_mtime = 100

        mock_dir2 = MagicMock(spec=Path)
        mock_dir2.is_dir.return_value = True
        mock_dir2.name = "conv-2"
        mock_dir2.stat.return_value.st_mtime = 200

        mock_brain_root.exists.return_value = True
        mock_brain_root.iterdir.return_value = [mock_dir1, mock_dir2]

        active = get_active_brain_dir()
        self.assertEqual(active, mock_dir2)

    @patch("tools.capture_screenshot.get_active_brain_dir")
    @patch("playwright.sync_api.sync_playwright")
    @patch("PIL.Image.open")
    @patch("pathlib.Path.exists")
    def test_capture_screenshot_viewport_success(
        self, mock_exists, mock_img_open, mock_playwright, mock_brain_dir
    ):
        mock_exists.return_value = True
        mock_brain_dir.return_value = None

        mock_img = MagicMock()
        mock_img.size = (1280, 800)
        mock_img.format = "PNG"
        mock_img_open.return_value.__enter__.return_value = mock_img

        mock_p = MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_p
        mock_browser = MagicMock()
        mock_p.chromium.launch.return_value = mock_browser
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_page.title.return_value = "Test Title"

        with patch("pathlib.Path.stat") as mock_stat:
            mock_stat_obj = MagicMock()
            mock_stat_obj.st_size = 50000
            mock_stat.return_value = mock_stat_obj

            result = capture_screenshot(
                url="http://localhost:3000",
                output_name="test_out",
                viewport="desktop",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["title"], "Test Title")
        self.assertEqual(result["width"], 1280)
        self.assertEqual(result["height"], 800)
        mock_page.screenshot.assert_called_once()

    @patch("tools.capture_screenshot.get_active_brain_dir")
    @patch("playwright.sync_api.sync_playwright")
    @patch("PIL.Image.open")
    @patch("pathlib.Path.exists")
    def test_capture_screenshot_selector_multi(
        self, mock_exists, mock_img_open, mock_playwright, mock_brain_dir
    ):
        mock_exists.return_value = True
        mock_brain_dir.return_value = None

        mock_img = MagicMock()
        mock_img.size = (800, 200)
        mock_img.format = "PNG"
        mock_img_open.return_value.__enter__.return_value = mock_img

        mock_p = MagicMock()
        mock_playwright.return_value.__enter__.return_value = mock_p
        mock_browser = MagicMock()
        mock_p.chromium.launch.return_value = mock_browser
        mock_context = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_page = MagicMock()
        mock_context.new_page.return_value = mock_page

        loc1 = MagicMock()
        loc1.bounding_box.return_value = {"x": 10, "y": 20, "width": 100, "height": 50}
        loc2 = MagicMock()
        loc2.bounding_box.return_value = {"x": 120, "y": 20, "width": 100, "height": 50}
        mock_page.locator.return_value.all.return_value = [loc1, loc2]

        with patch("pathlib.Path.stat") as mock_stat:
            mock_stat_obj = MagicMock()
            mock_stat_obj.st_size = 30000
            mock_stat.return_value = mock_stat_obj

            result = capture_screenshot(
                url="http://localhost:3000",
                selector=".card",
                output_name="test_cards",
            )

        self.assertTrue(result["success"])
        mock_page.screenshot.assert_called_once()
        call_kwargs = mock_page.screenshot.call_args[1]
        self.assertIn("clip", call_kwargs)
        clip = call_kwargs["clip"]
        self.assertLessEqual(clip["x"], 10)
        self.assertGreaterEqual(clip["width"], 210)

    @patch("tools.capture_screenshot.capture_screenshot")
    @patch("tools.deliver_image.deliver_image")
    def test_capture_and_deliver_success(self, mock_deliver, mock_capture):
        mock_capture.return_value = {
            "success": True,
            "file_path": "/workspace/data/qa_screenshots/test.png",
            "url": "http://localhost:3000",
        }
        mock_deliver.return_value = {
            "success": True,
            "channel": "zero-chat",
            "message_id": "12345",
        }

        res = capture_and_deliver(
            url="http://localhost:3000",
            channel="zero-chat",
            caption="Preview",
        )

        self.assertTrue(res["success"])
        self.assertTrue(res["delivered"])
        self.assertEqual(res["delivery_info"]["message_id"], "12345")
        mock_deliver.assert_called_once_with(
            image_path="/workspace/data/qa_screenshots/test.png",
            channel_input="zero-chat",
            caption="Preview",
        )


if __name__ == "__main__":
    unittest.main()
