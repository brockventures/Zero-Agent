#!/usr/bin/env python3
"""
test_gemini_image.py - Unit tests for direct Gemini API image generation
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import json
import base64

import sys
sys.path.insert(0, "/workspace")

from tools.gemini_image import (
    get_gemini_api_key,
    generate_image_api,
    ASPECT_RATIO_PROMPTS,
)


class TestGeminiImage(unittest.TestCase):

    @patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyTestEnvKey12345"})
    def test_get_gemini_api_key_from_env(self):
        key = get_gemini_api_key()
        self.assertEqual(key, "AIzaSyTestEnvKey12345")

    @patch("requests.post")
    @patch("tools.gemini_image.check_image")
    @patch("tools.gemini_image.get_gemini_api_key", return_value="AIzaSyMockKey")
    def test_generate_image_api_success(self, mock_key, mock_check, mock_post):
        mock_check.return_value = {
            "valid": True,
            "width": 1024,
            "height": 1024,
            "size_bytes": 123456,
            "size_kb": 120.5,
        }

        # Mock successful Gemini API response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        fake_b64 = base64.b64encode(b"fake_jpeg_binary_data").decode("utf-8")
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/jpeg",
                                    "data": fake_b64,
                                }
                            }
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        res = generate_image_api(
            prompt="Test prompt",
            aspect_ratio="16:9",
            image_name="test_render",
            output_dir="/tmp",
            timeout=15,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["model"], "gemini-3.1-flash-image")
        self.assertIn("16:9", res["prompt"])
        self.assertEqual(res["width"], 1024)
        self.assertEqual(res["height"], 1024)
        mock_post.assert_called_once()

    @patch("requests.post")
    @patch("tools.gemini_image.check_image")
    @patch("tools.gemini_image.get_gemini_api_key", return_value="AIzaSyMockKey")
    def test_generate_image_model_cascade_fallback(self, mock_key, mock_check, mock_post):
        mock_check.return_value = {
            "valid": True,
            "width": 1024,
            "height": 1024,
            "size_bytes": 123456,
            "size_kb": 120.5,
        }

        # First model fails with 503, second model succeeds
        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = "Capacity exhausted"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        fake_b64 = base64.b64encode(b"fake_jpeg_binary_data").decode("utf-8")
        resp_200.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/jpeg",
                                    "data": fake_b64,
                                }
                            }
                        ]
                    }
                }
            ]
        }

        mock_post.side_effect = [resp_503, resp_200]

        res = generate_image_api(
            prompt="Test prompt",
            aspect_ratio="1:1",
            image_name="fallback_render",
            output_dir="/tmp",
            timeout=15,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["model"], "gemini-3.1-flash-image-preview")
        self.assertEqual(mock_post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
