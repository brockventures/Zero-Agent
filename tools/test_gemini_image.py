#!/usr/bin/env python3
"""
test_gemini_image.py - Unit tests for Antigravity subscription-first image generation,
paid-API guard, and Studio Ghibli Hearth holiday & birthday pack generation.
"""

import base64
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.gemini_image import (
    HEARTH_HOLIDAY_LEGACY_STATES,
    HEARTH_HOLIDAY_SCENES,
    build_hearth_birthday_prompt,
    build_hearth_holiday_prompt,
    check_and_record_paid_budget,
    generate_hearth_holiday_pack,
    generate_image_api,
    generate_image_subscription,
    get_gemini_api_key,
    sync_legacy_holiday_states,
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
            "width": 1280,
            "height": 720,
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

    @patch("subprocess.run")
    @patch("tools.gemini_image.check_image")
    def test_generate_image_subscription_exact_filename_atomic_replace(self, mock_check, mock_run):
        mock_check.return_value = {
            "valid": True,
            "width": 1280,
            "height": 720,
            "size_bytes": 98765,
            "size_kb": 96.45,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "pack"
            out_dir.mkdir(parents=True, exist_ok=True)
            existing = out_dir / "holiday_christmas_v1.jpg"
            existing.write_bytes(b"stale_old_bytes")
            old_ts = time.time() - 3600
            os.utime(existing, (old_ts, old_ts))

            src_img = Path(tmpdir) / "fresh_christmas.jpg"
            src_img.write_bytes(b"fresh_new_ghibli_bytes")

            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = f"{src_img}\n"
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            res = generate_image_subscription(
                prompt="Ghibli winter scene",
                aspect_ratio="16:9",
                image_name="holiday_christmas_v1.jpg",
                output_dir=str(out_dir),
                timeout=30,
                exact_filename=True,
            )

            self.assertTrue(res["success"])
            self.assertEqual(res["filename"], "holiday_christmas_v1.jpg")
            self.assertEqual(Path(res["path"]), existing)
            self.assertEqual(existing.read_bytes(), b"fresh_new_ghibli_bytes")
            # Ensure no leftover staging files remain
            self.assertEqual(list(out_dir.glob("staging_*")), [])

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

    def test_build_hearth_holiday_and_birthday_prompts(self):
        cast = "older sister (age 7), younger brother (age 4), and infant baby sister held in arms"
        for key in ("thanksgiving", "christmas", "winter_solstice", "new_years"):
            self.assertIn(key, HEARTH_HOLIDAY_SCENES)
            prompts = [build_hearth_holiday_prompt(key, v, cast) for v in (1, 2, 3)]
            self.assertEqual(len(set(prompts)), 3)
            for p in prompts:
                self.assertIn("Studio Ghibli watercolor", p)
                self.assertIn("right 42%", p)
                self.assertIn(cast, p)

        with self.assertRaises(ValueError):
            build_hearth_holiday_prompt("unknown_holiday", 1, cast)

        b_morning = build_hearth_birthday_prompt(
            slot="morning",
            celebrant_description="4-year-old boy",
            cast_description=cast,
            age=4,
            season_hint="late-December winter",
        )
        self.assertIn("number '4'", b_morning)
        self.assertIn("late-December winter", b_morning)
        self.assertIn("right 42%", b_morning)

        with self.assertRaises(ValueError):
            build_hearth_birthday_prompt("midnight", "child", cast)

    def test_generate_hearth_holiday_pack_and_legacy_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_dir = Path(tmpdir) / "ghibli_test_pack"

            def fake_generate(prompt, aspect_ratio, image_name, output_dir, timeout, reference_image, allow_paid_api, exact_filename):
                self.assertEqual(aspect_ratio, "16:9")
                self.assertTrue(exact_filename)
                self.assertFalse(allow_paid_api)
                out_path = Path(output_dir) / f"{image_name}.jpg"
                out_path.write_bytes(f"jpeg_bytes_for_{image_name}".encode("utf-8"))
                return {
                    "success": True,
                    "path": str(out_path),
                    "filename": out_path.name,
                    "model": "subscription:gemini-4-argon-low",
                    "billing_mode": "subscription",
                    "width": 1280,
                    "height": 720,
                    "size_bytes": out_path.stat().st_size,
                    "size_kb": 0.1,
                }

            with patch("tools.gemini_image.generate_image_api", side_effect=fake_generate):
                res = generate_hearth_holiday_pack(
                    pack_dir=pack_dir,
                    cast_description="three siblings in Ghibli watercolor style",
                    holidays=("thanksgiving", "christmas", "new_years"),
                    variants_per_holiday=3,
                    birthday_specs=[
                        {
                            "key": "child_two",
                            "celebrant_description": "younger brother turning 4",
                            "age": 4,
                            "season_hint": "late-December winter",
                        }
                    ],
                    sync_legacy_states=True,
                    max_workers=2,
                )

            self.assertTrue(res["success"])
            # 3 holidays * 3 variants + 3 birthday slots = 12 generated files
            self.assertEqual(res["generated_count"], 12)

            for h in ("thanksgiving", "christmas", "new_years"):
                for v in (1, 2, 3):
                    self.assertTrue((pack_dir / f"holiday_{h}_v{v}.jpg").exists())
                for st in HEARTH_HOLIDAY_LEGACY_STATES[h]:
                    self.assertTrue((pack_dir / f"holiday_{h}_{st}.jpg").exists())

            for slot in ("morning", "afternoon", "evening"):
                self.assertTrue((pack_dir / f"birthday_child_two_{slot}.jpg").exists())

            # Verify skip_existing skips valid existing assets
            with patch("tools.gemini_image.check_image", return_value={"valid": True}), patch(
                "tools.gemini_image.generate_image_api", side_effect=fake_generate
            ) as mock_regen:
                res2 = generate_hearth_holiday_pack(
                    pack_dir=pack_dir,
                    cast_description="three siblings in Ghibli watercolor style",
                    holidays=("christmas",),
                    variants_per_holiday=3,
                    skip_existing=True,
                    sync_legacy_states=False,
                )
                self.assertTrue(res2["success"])
                self.assertEqual(res2["generated_count"], 0)
                self.assertEqual(res2["skipped_count"], 3)
                mock_regen.assert_not_called()

    def test_sync_legacy_holiday_states_handles_empty_or_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            self.assertEqual(sync_legacy_holiday_states(p, "unknown_key"), [])
            self.assertEqual(sync_legacy_holiday_states(p, "christmas"), [])


if __name__ == "__main__":
    unittest.main()
