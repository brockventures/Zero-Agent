#!/usr/bin/env python3
"""Unit tests for tools/mealie_image_backfill.py"""

import json
import sys
import unittest
from unittest.mock import patch, MagicMock

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import tools.mealie_image_backfill as mib


class TestMealieImageBackfill(unittest.TestCase):
    def test_extract_hero_from_pdf_without_fitz(self):
        with patch.object(mib, "fitz", None):
            hero, ext = mib.extract_hero_from_pdf("https://example.com/card.pdf")
            self.assertIsNone(hero)
            self.assertIsNone(ext)

    @patch("tools.mealie_image_backfill.requests.put")
    def test_upload_image_to_mealie_success(self, mock_put):
        mock_put.return_value.status_code = 200
        ok = mib.upload_image_to_mealie("pasta-carbonara", b"fakeimg", "jpg", "http://mealie:9090", "token123")
        self.assertTrue(ok)
        mock_put.assert_called_once()
        args, kwargs = mock_put.call_args
        self.assertIn("pasta-carbonara", args[0])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token123")

    @patch("tools.mealie_image_backfill.requests.put")
    def test_upload_image_to_mealie_failure(self, mock_put):
        mock_put.return_value.status_code = 500
        ok = mib.upload_image_to_mealie("pasta-carbonara", b"fakeimg", "jpg", "http://mealie:9090", "token123")
        self.assertFalse(ok)

    @patch("tools.mealie_image_backfill.subprocess.run")
    def test_get_missing_image_recipes_empty(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "[]"
        mock_run.return_value = mock_res
        missing = mib.get_missing_image_recipes()
        self.assertEqual(missing, [])

    @patch("tools.mealie_image_backfill.subprocess.run")
    def test_get_missing_image_recipes_populated(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = json.dumps([{"id": 1, "slug": "tacos", "name": "Fish Tacos", "org_url": ""}])
        mock_run.return_value = mock_res
        missing = mib.get_missing_image_recipes()
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["slug"], "tacos")


if __name__ == "__main__":
    unittest.main()
