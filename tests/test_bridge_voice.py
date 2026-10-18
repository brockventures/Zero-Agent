#!/usr/bin/env python3
"""Unit tests for tools/bridge_voice.py (Discord Voice + Gemini Live API)."""

import asyncio
import sys
import unittest
from unittest.mock import MagicMock

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

from tools.bridge_voice import (
    DEFAULT_LIVE_MODEL,
    ZERO_VOICE_CHANNEL_ID,
    StreamingPCMAudioSource,
    ZeroVoiceSessionManager,
    _build_live_tools,
    _build_zero_live_system_prompt,
    _ensure_opus_loaded,
)


class TestBridgeVoice(unittest.TestCase):
    def test_opus_loaded(self):
        self.assertTrue(_ensure_opus_loaded())

    def test_streaming_pcm_audio_source(self):
        src = StreamingPCMAudioSource()
        # Empty buffer should return 3840 bytes of silence (never b'')
        silence = src.read()
        self.assertEqual(len(silence), 3840)
        self.assertEqual(silence, b"\x00" * 3840)
        self.assertFalse(src.is_speaking_out)

        # Feed 20ms of 24kHz mono PCM (480 samples * 2 bytes = 960 bytes)
        pcm_24k = b"\x10\x00" * 480
        src.feed_gemini_pcm_24k_mono(pcm_24k)
        self.assertAlmostEqual(src.output_audio_seconds, 0.02, places=3)

        frame = src.read()
        self.assertEqual(len(frame), 3840)
        self.assertTrue(src.is_speaking_out)

        src.clear_buffer()
        self.assertFalse(src.is_speaking_out)
        src.cleanup()
        self.assertEqual(src.read(), b"")

    def test_prompt_and_tools(self):
        prompt = _build_zero_live_system_prompt()
        self.assertIn("Zero", prompt)
        self.assertIn("PT", prompt)
        tools = _build_live_tools()
        self.assertEqual(len(tools), 1)
        fn_names = [f.name for f in tools[0].function_declarations]
        self.assertIn("search_zero_memory", fn_names)
        self.assertIn("post_note_to_zero_chat", fn_names)

    def test_session_manager_idle_status(self):
        bot = MagicMock()
        mgr = ZeroVoiceSessionManager(bot)
        st = mgr.get_status_dict()
        self.assertFalse(st["active"])
        self.assertEqual(st["model"], DEFAULT_LIVE_MODEL)
        self.assertEqual(ZERO_VOICE_CHANNEL_ID, 1560924105279275042)


if __name__ == "__main__":
    unittest.main()
