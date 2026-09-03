#!/usr/bin/env python3
"""
Unit test suite for bridge_state.py (State, Session, and Queue Persistence).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import tools.bridge_state as bs


class TestBridgeState(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_sessions_file = bs.SESSIONS_FILE
        self.orig_session_metadata_file = bs.SESSION_METADATA_FILE
        self.orig_config_file = bs.CONFIG_FILE
        self.orig_beacon_file = bs.BEACON_FILE

        bs.DATA_DIR = self.temp_path
        bs.SESSIONS_FILE = self.temp_path / "sessions.json"
        bs.SESSION_METADATA_FILE = self.temp_path / "session_metadata.json"
        bs.CONFIG_FILE = self.temp_path / "runtime_config.json"
        bs.BEACON_FILE = self.temp_path / "liveness_beacon.json"

    def tearDown(self):
        bs.DATA_DIR = self.orig_data_dir
        bs.SESSIONS_FILE = self.orig_sessions_file
        bs.SESSION_METADATA_FILE = self.orig_session_metadata_file
        bs.CONFIG_FILE = self.orig_config_file
        bs.BEACON_FILE = self.orig_beacon_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_session_metadata_lifecycle(self):
        meta = bs.get_session_metadata("test_sess")
        self.assertEqual(meta, {})

        bs.set_session_metadata("test_sess", {"turns": 5, "last_active": 12345})
        meta = bs.get_session_metadata("test_sess")
        self.assertEqual(meta.get("turns"), 5)

        turn = bs.increment_session_turn("test_sess")
        self.assertEqual(turn, 6)

        bs.reset_session_meta("test_sess")
        meta = bs.get_session_metadata("test_sess")
        self.assertEqual(meta.get("turns"), 0)

    def test_channel_session_mapping(self):
        target_ch = bs.TARGET_CHANNEL_ID
        bs.set_channel_session_id(target_ch, "home", "conv-home-123")
        cid = bs.get_channel_session_id(target_ch, "home")
        self.assertEqual(cid, "conv-home-123")

        ext_ch = 987654321
        bs.set_channel_session_id(ext_ch, "external", "conv-ext-456")
        ext_cid = bs.get_channel_session_id(ext_ch, "external")
        self.assertEqual(ext_cid, "conv-ext-456")

        bs.clear_channel_session_id(ext_ch, "external")
        self.assertIsNone(bs.get_channel_session_id(ext_ch, "external"))

    def test_compaction_thresholds(self):
        # 1. Turns threshold >= 25
        needed, reason = bs.check_compaction_needed("conv-1", 25)
        self.assertTrue(needed)
        self.assertIn("turn count", reason)

        # 2. No conv_id and low turns
        needed, _ = bs.check_compaction_needed(None, 10)
        self.assertFalse(needed)

        # 3. Brain directory size check
        brain_temp = self.temp_path / "brain"
        conv_dir = brain_temp / "conv-mock" / ".system_generated" / "logs"
        conv_dir.mkdir(parents=True, exist_ok=True)
        transcript = conv_dir / "transcript.jsonl"

        # Create mock 2.1 MB transcript
        with open(transcript, "wb") as f:
            f.write(b"x" * int(2.1 * 1024 * 1024))

        needed, reason = bs.check_compaction_needed("conv-mock", 5, brain_root=brain_temp)
        self.assertTrue(needed)
        self.assertIn("transcript size", reason)

    async def test_persistent_turn_queue(self):
        q_file = self.temp_path / "test_queue.json"
        turn_q = bs.PersistentTurnQueue(q_file)

        self.assertTrue(turn_q.empty())

        item1 = {"prompt": "Task 1", "attachments": []}
        item2 = {"prompt": "Task 2", "attachments": ["/path/test"]}
        await turn_q.put(item1)
        await turn_q.put(item2)

        self.assertFalse(turn_q.empty())
        self.assertTrue(q_file.exists())

        # Verify disk persistence format
        with open(q_file) as f:
            disk_items = json.load(f)
        self.assertEqual(len(disk_items), 2)
        self.assertEqual(disk_items[0]["prompt"], "Task 1")

        # Get items and complete tasks
        got1 = await turn_q.get()
        self.assertEqual(got1["prompt"], "Task 1")
        turn_q.task_done(got1)

        with open(q_file) as f:
            disk_items = json.load(f)
        self.assertEqual(len(disk_items), 1)
        self.assertEqual(disk_items[0]["prompt"], "Task 2")

        got2 = await turn_q.get()
        self.assertEqual(got2["prompt"], "Task 2")
        turn_q.task_done(got2)

        self.assertTrue(turn_q.empty())

    def test_active_model_and_runtime_config(self):
        bs.set_active_model("claude-sonnet-4-6")
        self.assertEqual(bs.get_active_model(), "claude-sonnet-4-6")

        with open(bs.CONFIG_FILE) as f:
            data = json.load(f)
        self.assertEqual(data.get("model"), "claude-sonnet-4-6")

    def test_beacon_update(self):
        bs.update_beacon("PROCESSING", "Running heavy task")
        self.assertTrue(bs.BEACON_FILE.exists())
        with open(bs.BEACON_FILE) as f:
            b_data = json.load(f)
        self.assertEqual(b_data["state"], "PROCESSING")
        self.assertIn("Running heavy task", b_data["prompt"])

    def test_gif_turn_tracking_and_guidance(self):
        # 1. Defaults to 0
        self.assertEqual(bs.get_gif_turn_count("chan_a"), 0)

        # 2. Increment per-channel
        c1 = bs.increment_gif_turn("chan_a")
        self.assertEqual(c1, 1)
        c2 = bs.increment_gif_turn("chan_a")
        self.assertEqual(c2, 2)

        # 3. Channel isolation
        self.assertEqual(bs.get_gif_turn_count("chan_b"), 0)

        # 4. Reset counter
        bs.reset_gif_turn("chan_a")
        self.assertEqual(bs.get_gif_turn_count("chan_a"), 0)

        # 5. has_reaction_gif regex detection
        self.assertTrue(bs.has_reaction_gif("Check this out\nhttps://tenor.com/view/youre-busted-man-gif-4979634115261473598"))
        self.assertTrue(bs.has_reaction_gif("https://giphy.com/gifs/funny-cat-123"))
        self.assertTrue(bs.has_reaction_gif("https://cdn.example.com/reactions/laugh.gif"))
        self.assertFalse(bs.has_reaction_gif("Here is the technical diagnosis without visual media."))
        self.assertFalse(bs.has_reaction_gif(""))

        # 6. Prompt guidance formatting & overrides
        guidance_low = bs.get_gif_prompt_guidance("chan_a")
        self.assertIn("Nominal (0/5-7 turns)", guidance_low)
        self.assertIn("Serious / Critical Override", guidance_low)
        self.assertIn("Social / Banter Override", guidance_low)

        for _ in range(5):
            bs.increment_gif_turn("chan_a")
        guidance_due = bs.get_gif_prompt_guidance("chan_a")
        self.assertIn("⚠️ DUE (>=5 turns without GIF)", guidance_due)


if __name__ == "__main__":
    unittest.main()
