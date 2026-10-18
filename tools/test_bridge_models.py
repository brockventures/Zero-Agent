#!/usr/bin/env python3
"""Tests for tools/bridge_models.py (live model registry, alias resolution, fallback)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/workspace")
import tools.bridge_models as bm

LIVE = {
    "gemini-4-argon-high": "Gemini 4 Argon (High)",
    "gemini-4-argon-medium": "Gemini 4 Argon (Medium)",
    "gemini-4-argon-low": "Gemini 4 Argon (Low)",
    "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
    "claude-opus-5-5-low": "Claude Opus 5.5 (Low)",
    "claude-opus-5-5-medium": "Claude Opus 5.5 (Medium)",
    "claude-opus-5-5-high": "Claude Opus 5.5 (High)",
    "claude-sonnet-5-5-low": "Claude Sonnet 5.5 (Low)",
    "claude-sonnet-5-5-medium": "Claude Sonnet 5.5 (Medium)",
    "claude-sonnet-5-5-high": "Claude Sonnet 5.5 (High)",
    "gpt-oss-120b-medium": "GPT-OSS 120B (Medium)",
}


class TestParseAndErrors(unittest.TestCase):
    def test_parse_models_output(self):
        out = "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)\n\njunk line with spaces\n"
        parsed = bm.parse_models_output(out)
        self.assertEqual(parsed["gemini-3.8-flash-high"], "Gemini 3.8 Flash (High)")
        self.assertEqual(len(parsed), 2)

    def test_is_model_error(self):
        self.assertTrue(bm.is_model_error('Error: invalid model selection (--model "x"): model x is not recognized as a known model'))
        self.assertTrue(bm.is_model_error("model foo is not recognized as a known model or custom model in settings"))
        self.assertFalse(bm.is_model_error("Rate limit exceeded"))
        self.assertFalse(bm.is_model_error(None))


class TestResolve(unittest.TestCase):
    def test_exact_id(self):
        self.assertEqual(bm.resolve_model("gemini-3.8-flash-low", LIVE), "gemini-3.8-flash-low")

    def test_family_shortcuts_default_to_highest_effort(self):
        self.assertEqual(bm.resolve_model("sonnet", LIVE), "claude-sonnet-5-5-high")
        self.assertEqual(bm.resolve_model("claude", LIVE), "claude-sonnet-5-5-high")
        self.assertEqual(bm.resolve_model("opus", LIVE), "claude-opus-5-5-high")
        self.assertEqual(bm.resolve_model("flash", LIVE), "gemini-3.8-flash-high")
        self.assertEqual(bm.resolve_model("3.8", LIVE), "gemini-3.8-flash-high")
        self.assertEqual(bm.resolve_model("gpt", LIVE), "gpt-oss-120b-medium")

    def test_effort_suffix(self):
        self.assertEqual(bm.resolve_model("sonnet-medium", LIVE), "claude-sonnet-5-5-medium")
        self.assertEqual(bm.resolve_model("3.8-lite", LIVE), "gemini-3.8-flash-low")
        self.assertEqual(bm.resolve_model("flash-med", LIVE), "gemini-3.8-flash-medium")

    def test_argon_shortcuts(self):
        self.assertEqual(bm.resolve_model("argon", LIVE), "gemini-4-argon-high")
        self.assertEqual(bm.resolve_model("argon-med", LIVE), "gemini-4-argon-medium")
        self.assertEqual(bm.resolve_model("argon-low", LIVE), "gemini-4-argon-low")
        self.assertEqual(bm.resolve_model("4", LIVE), "gemini-4-argon-high")
        self.assertEqual(bm.resolve_model("gemini-4", LIVE), "gemini-4-argon-high")
        self.assertEqual(bm.resolve_model("gemini-4-argon-high", LIVE), "gemini-4-argon-high")

    def test_retired_or_unknown_models_do_not_resolve(self):
        self.assertIsNone(bm.resolve_model("3.7", LIVE))
        self.assertIsNone(bm.resolve_model("3.1-pro", LIVE))
        self.assertIsNone(bm.resolve_model("pro", LIVE))
        self.assertIsNone(bm.resolve_model("gpt-oss-120b-high", LIVE))
        self.assertIsNone(bm.resolve_model("nonsense", LIVE))

    def test_newest_version_wins(self):
        live = dict(LIVE)
        live["claude-sonnet-6-0-high"] = "Claude Sonnet 6.0 (High)"
        self.assertEqual(bm.resolve_model("sonnet", live), "claude-sonnet-6-0-high")


class TestFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(bm, "_data_dir", return_value=Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_default_fallback_is_sonnet_medium(self):
        self.assertEqual(bm.get_fallback_model(LIVE), "claude-sonnet-5-5-medium")

    def test_configured_fallback_honored_when_real(self):
        (Path(self.tmp.name) / "runtime_config.json").write_text(json.dumps({"model": "x", "fallback_model": "gemini-3.8-flash-medium"}))
        self.assertEqual(bm.get_fallback_model(LIVE), "gemini-3.8-flash-medium")

    def test_configured_fallback_ignored_when_not_real(self):
        (Path(self.tmp.name) / "runtime_config.json").write_text(json.dumps({"fallback_model": "claude-sonnet-4-6"}))
        self.assertEqual(bm.get_fallback_model(LIVE), "claude-sonnet-5-5-medium")

    def test_fallback_never_equals_excluded(self):
        self.assertNotEqual(bm.get_fallback_model(LIVE, exclude="claude-sonnet-5-5-medium"), "claude-sonnet-5-5-medium")

    def test_preflight_valid_model_no_notice(self):
        with patch.object(bm, "list_available_models", return_value=LIVE):
            self.assertEqual(bm.preflight_model("gemini-3.8-flash-high"), ("gemini-3.8-flash-high", None))

    def test_preflight_invalid_model_falls_back_with_notice(self):
        with patch.object(bm, "list_available_models", return_value=LIVE), \
             patch.object(bm, "_fetch_models", return_value=LIVE), patch.object(bm, "_write_cache"):
            model, notice = bm.preflight_model("gemini-3.7-flash-high")
        self.assertEqual(model, "claude-sonnet-5-5-medium")
        self.assertIn("gemini-3.7-flash-high", notice)
        self.assertIn("claude-sonnet-5-5-medium", notice)

    def test_preflight_refetches_before_falling_back(self):
        subset = {k: v for k, v in LIVE.items() if "gemini" in k}
        full = dict(LIVE)
        full["claude-sonnet-5-5-high"] = "x"
        with patch.object(bm, "list_available_models", return_value=subset), \
             patch.object(bm, "_fetch_models", return_value=full), patch.object(bm, "_write_cache"):
            self.assertEqual(bm.preflight_model("claude-opus-5-5-high"), ("claude-opus-5-5-high", None))

    def test_preflight_fails_open_when_list_unavailable(self):
        with patch.object(bm, "list_available_models", return_value={}):
            self.assertEqual(bm.preflight_model("gemini-3.8-flash-high"), ("gemini-3.8-flash-high", None))

    def test_list_uses_stale_cache_when_cli_fails(self):
        (Path(self.tmp.name) / "available_models.json").write_text(json.dumps({"fetched_at": 0, "models": LIVE}))
        with patch.object(bm.subprocess, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(bm.list_available_models(), LIVE)


    def test_channel_model_overrides(self):
        # Tier 1: Gemini 4 Argon (High)
        self.assertEqual(bm.get_channel_model_override(1555973316752449576, "3d-print"), "gemini-4-argon-high")
        self.assertEqual(bm.get_channel_model_override(None, "3d-print"), "gemini-4-argon-high")
        # Tier 2: Gemini 4 Argon (Medium)
        self.assertEqual(bm.get_channel_model_override(1548196929308065893, "baseball"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(1544955532765560924, "finances"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(1550577910757458015, "vault"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(1544953279664889888, "zero-ops"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(None, "harness-management"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(1544953275877556334, "home-assistant"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(1544955535722545253, "homelab"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(1548196930788524094, "projects"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(1558202642663211169, "agora"), "gemini-4-argon-medium")
        self.assertEqual(bm.get_channel_model_override(1551465050072416286, "side-project"), "gemini-4-argon-medium")
        # Tier 3: Gemini 4 Argon (Low)
        self.assertEqual(bm.get_channel_model_override(1542081375287640084, "zero-chat"), "gemini-4-argon-low")
        self.assertEqual(bm.get_channel_model_override(1550577908811178095, "brock-house"), "gemini-4-argon-low")
        self.assertEqual(bm.get_channel_model_override(1544955538033348618, "shopping"), "gemini-4-argon-low")
        self.assertEqual(bm.get_channel_model_override(1544953277592899615, "steam-deck"), "gemini-4-argon-low")
        self.assertEqual(bm.get_channel_model_override(1534452820995080192), "gemini-4-argon-low")
        self.assertEqual(bm.get_channel_model_override(None, "lounge"), "gemini-4-argon-low")
        self.assertEqual(bm.get_channel_model_override(1534436119888793750), "gemini-4-argon-low")
        self.assertEqual(bm.get_channel_model_override(None, "the-banana-stand"), "gemini-4-argon-low")
        # Tier 4: Gemini 3.8 Flash (Low)
        self.assertEqual(bm.get_channel_model_override(1330447543477338202), "gemini-3.8-flash-low")
        self.assertEqual(bm.get_channel_model_override(1210466877835313155), "gemini-3.8-flash-low")
        self.assertEqual(bm.get_channel_model_override(1453427860793463000), "gemini-3.8-flash-low")
        self.assertEqual(bm.get_channel_model_override(None, "seerr-requests-and-chat"), "gemini-3.8-flash-low")
        self.assertEqual(bm.get_channel_model_override(None, "seer-alerts"), "gemini-3.8-flash-low")
        # Unpinned channel falls back to global default (None override)
        self.assertIsNone(bm.get_channel_model_override(1549555629222404096, "debug-huddle"))


if __name__ == "__main__":
    unittest.main()

