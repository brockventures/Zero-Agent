#!/usr/bin/env python3
"""
test_gemini_image.py - Unit tests for Antigravity subscription-first image generation and paid-API guard
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile
import base64

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.gemini_image import (
    get_gemini_api_key,
    generate_image_api,
    generate_image_subscription,
    check_and_record_paid_budget,
)


class TestGeminiImage(unittest.TestCase):

    @patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyTestEnvKey12345"})
    def test_get_gemini_api_key_from_env(self):
        key = get_gemini_api_key()
        self.assertEqual(key, "AIzaSyTestEnvKey12345")

    @patch("requests.post")
    @patch("tools.gemini_image.generate_image_subscription")
    def test_generate_image_api_defaults_to_subscription_and_blocks_paid_rest(self, mock_sub, mock_post):
        mock_sub.return_value = {
            "success": True,
            "path": "/tmp/sub_render.jpg",
            "filename": "sub_render.jpg",
            "model": "subscription:gemini-4-argon-low",
            "billing_mode": "subscription",
            "prompt": "Test prompt, in square 1:1 format",
            "width": 1024,
            "height": 1024,
            "size_bytes": 100000,
            "size_kb": 97.6,
        }
        with patch.dict("os.environ", {"GEMINI_IMAGE_ALLOW_PAID": ""}, clear=False):
            res = generate_image_api(
                prompt="Test prompt",
                aspect_ratio="1:1",
                image_name="sub_render",
                output_dir="/tmp",
                timeout=15,
            )

        self.assertTrue(res["success"])
        self.assertEqual(res["billing_mode"], "subscription")
        mock_sub.assert_called_once()
        mock_post.assert_not_called()

    @patch("subprocess.run")
    @patch("tools.gemini_image.check_image")
    def test_generate_image_subscription_invokes_agy_and_copies_artifact(self, mock_check, mock_run):
        mock_check.return_value = {
            "valid": True,
            "width": 1024,
            "height": 1024,
            "size_bytes": 54321,
            "size_kb": 53.05,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            src_img = Path(tmpdir) / "brain_generated_999.jpg"
            src_img.write_bytes(b"fake_subscription_jpeg_bytes")

            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = f"Saved generated image to {src_img}\n"
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            out_dir = Path(tmpdir) / "out"
            res = generate_image_subscription(
                prompt="A futuristic server rack",
                aspect_ratio="16:9",
                image_name="server_rack",
                output_dir=str(out_dir),
                timeout=30,
            )

            self.assertTrue(res["success"])
            self.assertEqual(res["billing_mode"], "subscription")
            self.assertEqual(res["model"], "subscription:gemini-4-argon-low")
            self.assertIn("16:9", res["prompt"])
            self.assertTrue(Path(res["path"]).exists())
            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            self.assertEqual(called_cmd[0], "agy")
            self.assertIn("--model=gemini-4-argon-low", called_cmd)
            self.assertNotIn("--agent=image-generator", called_cmd)
            self.assertTrue(any("image-generator" in arg for arg in called_cmd))

    @patch("requests.post")
    @patch("tools.gemini_image.check_and_record_paid_budget", return_value={"allowed": True, "used_24h": 1, "daily_limit": 5})
    @patch("tools.gemini_image.check_image")
    @patch("tools.gemini_image.get_gemini_api_key", return_value="AIzaSyMockKey")
    def test_generate_image_api_paid_success_when_explicitly_allowed(self, mock_key, mock_check, mock_budget, mock_post):
        mock_check.return_value = {
            "valid": True,
            "width": 1024,
            "height": 1024,
            "size_bytes": 123456,
            "size_kb": 120.5,
        }

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
            allow_paid_api=True,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["model"], "gemini-3.1-flash-image")
        self.assertEqual(res["billing_mode"], "paid_api")
        self.assertIn("16:9", res["prompt"])
        self.assertEqual(res["width"], 1024)
        self.assertEqual(res["height"], 1024)
        mock_post.assert_called_once()

    @patch("requests.post")
    @patch("tools.gemini_image.check_and_record_paid_budget", return_value={"allowed": True, "used_24h": 1, "daily_limit": 5})
    @patch("tools.gemini_image.check_image")
    @patch("tools.gemini_image.get_gemini_api_key", return_value="AIzaSyMockKey")
    def test_generate_image_model_cascade_fallback(self, mock_key, mock_check, mock_budget, mock_post):
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
            allow_paid_api=True,
        )

        self.assertTrue(res["success"])
        self.assertEqual(res["model"], "gemini-3.1-flash-image-preview")
        self.assertEqual(mock_post.call_count, 2)

    @patch("requests.post")
    def test_paid_api_daily_budget_guard_blocks_excess_calls(self, mock_post):
        with tempfile.TemporaryDirectory() as tmpdir:
            budget_file = Path(tmpdir) / "budget.json"
            c1 = check_and_record_paid_budget(budget_file=budget_file, daily_limit=2)
            c2 = check_and_record_paid_budget(budget_file=budget_file, daily_limit=2)
            c3 = check_and_record_paid_budget(budget_file=budget_file, daily_limit=2)

            self.assertTrue(c1["allowed"])
            self.assertTrue(c2["allowed"])
            self.assertFalse(c3["allowed"])
            self.assertIn("daily budget exceeded", c3["error"])

        with patch("tools.gemini_image.check_and_record_paid_budget", return_value=c3):
            res = generate_image_api(
                prompt="Blocked paid call",
                allow_paid_api=True,
            )
            self.assertFalse(res["success"])
            self.assertEqual(res["billing_mode"], "paid_api_blocked")
            mock_post.assert_not_called()

    @patch("tools.gemini_image.deliver_image")
    @patch("tools.gemini_image.generate_image_api")
    def test_main_no_default_prompt_caption(self, mock_gen, mock_deliver):
        mock_gen.return_value = {"success": True, "path": "/tmp/test.jpg"}
        mock_deliver.return_value = {"success": True}
        with patch("sys.argv", ["gemini_image.py", "--prompt", "secret internal prompt", "--channel", "lounge"]):
            from tools.gemini_image import main
            main()
        mock_gen.assert_called_once()
        self.assertFalse(mock_gen.call_args.kwargs.get("allow_paid_api"))
        mock_deliver.assert_called_once_with("/tmp/test.jpg", channel_input="lounge", caption=None)


if __name__ == "__main__":
    unittest.main()
