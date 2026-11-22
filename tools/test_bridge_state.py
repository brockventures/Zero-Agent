#!/usr/bin/env python3
# ruff: noqa
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

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
elif sys.path[0] != str(REPO_ROOT):
    sys.path.remove(str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT))
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

import tools.bridge_state as bs


class TestBridgeState(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.orig_agent_role = os.environ.get("AGENT_ROLE")
        self.orig_discord_ch_id = os.environ.get("DISCORD_CHANNEL_ID")
        self.orig_vulcan_delegated = os.environ.get("VULCAN_DELEGATED")
        os.environ["AGENT_ROLE"] = ""
        os.environ.pop("DISCORD_CHANNEL_ID", None)
        os.environ.pop("VULCAN_DELEGATED", None)
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_sessions_file = bs.SESSIONS_FILE
        self.orig_session_metadata_file = bs.SESSION_METADATA_FILE
        self.orig_config_file = bs.CONFIG_FILE
        self.orig_beacon_file = bs.BEACON_FILE
        self.orig_in_flight = bs.IN_FLIGHT_FILE

        bs.DATA_DIR = self.temp_path
        bs.SESSIONS_FILE = self.temp_path / "sessions.json"
        bs.SESSION_METADATA_FILE = self.temp_path / "session_metadata.json"
        bs.CONFIG_FILE = self.temp_path / "runtime_config.json"
        bs.BEACON_FILE = self.temp_path / "liveness_beacon.json"
        bs.IN_FLIGHT_FILE = self.temp_path / "in_flight_turn.json"

    def tearDown(self):
        if self.orig_agent_role is not None:
            os.environ["AGENT_ROLE"] = self.orig_agent_role
        else:
            os.environ.pop("AGENT_ROLE", None)
        if self.orig_discord_ch_id is not None:
            os.environ["DISCORD_CHANNEL_ID"] = self.orig_discord_ch_id
        else:
            os.environ.pop("DISCORD_CHANNEL_ID", None)
        if self.orig_vulcan_delegated is not None:
            os.environ["VULCAN_DELEGATED"] = self.orig_vulcan_delegated
        else:
            os.environ.pop("VULCAN_DELEGATED", None)
        bs.DATA_DIR = self.orig_data_dir
        bs.SESSIONS_FILE = self.orig_sessions_file
        bs.SESSION_METADATA_FILE = self.orig_session_metadata_file
        bs.CONFIG_FILE = self.orig_config_file
        bs.BEACON_FILE = self.orig_beacon_file
        bs.IN_FLIGHT_FILE = self.orig_in_flight
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

    def test_session_rotation_parent_tracking(self):
        ch = 123456789
        bs.set_channel_session_id(ch, "external", "conv-v1")
        self.assertEqual(bs.get_channel_session_id(ch, "external"), "conv-v1")
        self.assertIsNone(bs.get_session_parent_id(str(ch)))

        # Rotate directly via set_channel_session_id
        bs.set_channel_session_id(ch, "external", "conv-v2")
        self.assertEqual(bs.get_channel_session_id(ch, "external"), "conv-v2")
        self.assertEqual(bs.get_session_parent_id(str(ch)), "conv-v1")
        meta = bs.get_session_metadata(str(ch))
        self.assertEqual(meta.get("conv_history"), ["conv-v1"])

        # Rotate via clear_channel_session_id then set
        bs.clear_channel_session_id(ch, "external")
        self.assertIsNone(bs.get_channel_session_id(ch, "external"))
        self.assertEqual(bs.get_session_parent_id(str(ch)), "conv-v2")
        meta = bs.get_session_metadata(str(ch))
        self.assertEqual(meta.get("conv_history"), ["conv-v1", "conv-v2"])

        bs.set_channel_session_id(ch, "external", "conv-v3")
        self.assertEqual(bs.get_channel_session_id(ch, "external"), "conv-v3")
        self.assertEqual(bs.get_session_parent_id(str(ch)), "conv-v2")

    def test_compaction_thresholds(self):
        # 1. Turns threshold >= 15
        needed, reason = bs.check_compaction_needed("conv-1", 15)
        self.assertTrue(needed)
        self.assertIn("turn count", reason)

        # 2. No conv_id and low turns
        needed, _ = bs.check_compaction_needed(None, 5)
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

        # 4. Transcript step count ceiling (1500 steps) triggers even on small files
        transcript_steps = conv_dir / "transcript_steps" / ".system_generated" / "logs"
        transcript_steps.mkdir(parents=True, exist_ok=True)
        t_file = transcript_steps / "transcript.jsonl"
        with open(t_file, "w") as f:
            for i in range(1505):
                f.write(f'{{"step": {i}}}\n')
        needed, reason = bs.check_compaction_needed("transcript_steps", 2, brain_root=conv_dir)
        self.assertTrue(needed)
        self.assertIn("transcript steps (1505) exceeds", reason)

        # 5. Transcript step index from tail seek triggers compaction without full-file line scan
        transcript_tail = conv_dir / "transcript_tail" / ".system_generated" / "logs"
        transcript_tail.mkdir(parents=True, exist_ok=True)
        t_tail_file = transcript_tail / "transcript.jsonl"
        with open(t_tail_file, "w") as f:
            for i in range(250):
                f.write(f'{{"step_index": {i}, "type": "USER_INPUT"}}\n')
        needed, reason = bs.check_compaction_needed("transcript_tail", 2, brain_root=conv_dir)
        self.assertTrue(needed)
        self.assertIn("transcript steps (250) exceeds", reason)

        # 6. Dead thread retitle helpers removed
        self.assertFalse(hasattr(bs, "is_thread_retitled"))
        self.assertFalse(hasattr(bs, "mark_thread_retitled"))

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

    async def test_persistent_turn_queue_epoch_and_metadata_roundtrip(self):
        """Verify PersistentTurnQueue persists Unix epoch queued_at_epoch and routing metadata without mixing perf_counter."""
        q_file = self.temp_path / "epoch_queue.json"
        turn_q = bs.PersistentTurnQueue(q_file)

        before_epoch = time.time()
        item = {
            "prompt": "Resume thread work",
            "attachments": ["/tmp/example.png"],
            "channel_id": 9988776655,
            "mode": "home",
            "author_name": "Ryan",
            "is_thread_task": True,
        }
        await turn_q.put(item)
        after_epoch = time.time()

        # In-memory item maintains both monotonic queued_at and wall-clock queued_at_epoch
        self.assertIn("queued_at", item)
        self.assertIn("queued_at_epoch", item)
        self.assertGreaterEqual(item["queued_at_epoch"], before_epoch - 1.0)
        self.assertLessEqual(item["queued_at_epoch"], after_epoch + 1.0)

        # Disk payload serializes Unix epoch and full routing metadata
        with open(q_file) as f:
            disk_items = json.load(f)
        self.assertEqual(len(disk_items), 1)
        saved = disk_items[0]
        self.assertEqual(saved["prompt"], "Resume thread work")
        self.assertEqual(saved["attachments"], ["/tmp/example.png"])
        self.assertEqual(saved["channel_id"], 9988776655)
        self.assertEqual(saved["mode"], "home")
        self.assertEqual(saved["author_name"], "Ryan")
        self.assertTrue(saved["is_thread_task"])
        self.assertGreaterEqual(saved["queued_at_epoch"], before_epoch - 1.0)
        self.assertLessEqual(saved["queued_at_epoch"], after_epoch + 1.0)

        # Fresh queue instance simulates restart recovery via load_persisted()
        reloaded_q = bs.PersistentTurnQueue(q_file)
        loaded = reloaded_q.load_persisted()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["channel_id"], 9988776655)
        self.assertEqual(loaded[0]["mode"], "home")
        self.assertEqual(loaded[0]["author_name"], "Ryan")
        self.assertTrue(loaded[0]["is_thread_task"])
        self.assertLess(time.time() - loaded[0]["queued_at_epoch"], 900)

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
        self.assertIn("Cooldown (0/4 min turns", guidance_low)
        self.assertNotIn("⚠️ DUE", guidance_low)

        for _ in range(5):
            bs.increment_gif_turn("chan_a")
        guidance_due = bs.get_gif_prompt_guidance("chan_a")
        self.assertIn("Eligible (5 turns since last GIF", guidance_due)
        self.assertNotIn("⚠️ DUE", guidance_due)

        # 7. Disabled channel GIF policy guidance
        self.assertTrue(bs.is_gif_disabled_for_channel("1534436119888793750"))
        self.assertTrue(bs.is_gif_disabled_for_channel("the-banana-stand"))
        self.assertTrue(bs.is_gif_disabled_for_channel("random", channel_id=1534436119888793750))
        self.assertFalse(bs.is_gif_disabled_for_channel("home", channel_id=1542081375287640084))

        banana_guidance = bs.get_gif_prompt_guidance("1534436119888793750")
        self.assertIn("Reaction GIFs are STRICTLY DISABLED", banana_guidance)
        self.assertNotIn("⚠️ DUE", banana_guidance)

    test_gif_cadence_tracking = test_gif_turn_tracking_and_guidance

    def test_in_flight_turn_lifecycle_and_attempts(self):
        # 1. First recording has attempts = 1
        bs.record_in_flight(12345, "First prompt test", conv_id="conv-1")
        self.assertTrue(bs.IN_FLIGHT_FILE.exists())
        with open(bs.IN_FLIGHT_FILE) as f:
            d = json.load(f)
        self.assertEqual(d["12345"]["attempts"], 1)
        self.assertEqual(d["attempts"], 1)

        # 2. Re-recording the exact same prompt increments attempts
        bs.record_in_flight(12345, "First prompt test", conv_id="conv-1")
        with open(bs.IN_FLIGHT_FILE) as f:
            d2 = json.load(f)
        self.assertEqual(d2["12345"]["attempts"], 2)
        self.assertEqual(d2["attempts"], 2)

        # 3. Recording a different prompt resets attempts to 1
        bs.record_in_flight(12345, "Different prompt", conv_id="conv-2")
        with open(bs.IN_FLIGHT_FILE) as f:
            d3 = json.load(f)
        self.assertEqual(d3["12345"]["attempts"], 1)

        # 4. Clear in-flight
        bs.clear_in_flight(12345)
        self.assertFalse(bs.IN_FLIGHT_FILE.exists())

    def test_in_flight_turn_seq_monotonic_and_clear_fencing(self):
        # Monotonic turn_seq persists across clear_in_flight unlinks
        seq1 = bs.record_in_flight(12345, "Turn 1", conv_id="conv-1")
        seq2 = bs.record_in_flight(12345, "Turn 1", conv_id="conv-1", pid=os.getpid())
        self.assertIsInstance(seq1, int)
        self.assertGreater(seq2, seq1)

        with open(bs.IN_FLIGHT_FILE) as f:
            data = json.load(f)
        self.assertEqual(data["12345"]["turn_seq"], seq2)
        self.assertEqual(data["turn_seq"], seq2)

        # Stale clear_in_flight with old expected_turn_seq must NOT evict newer turn_seq
        self.assertFalse(bs.clear_in_flight(12345, expected_turn_seq=seq1))
        self.assertTrue(bs.IN_FLIGHT_FILE.exists())
        self.assertEqual(bs.get_in_flight()["12345"]["turn_seq"], seq2)

        # Matching expected_turn_seq clears cleanly
        self.assertTrue(bs.clear_in_flight(12345, expected_turn_seq=seq2))
        self.assertFalse(bs.IN_FLIGHT_FILE.exists())

        # New turn after unlink still gets strictly greater turn_seq
        seq3 = bs.record_in_flight(12345, "Turn 2", conv_id="conv-2")
        self.assertGreater(seq3, seq2)

    def test_reap_stale_in_flight_entries_fences_out_concurrent_new_turn(self):
        # Create a conv transcript that looks completed (PLANNER_RESPONSE DONE) and entry older than 30s
        brain_root = Path("/root/.gemini/antigravity-cli/brain")
        conv_id = f"test-reap-cas-{os.getpid()}-{int(time.time() * 1000)}"
        tdir = brain_root / conv_id / ".system_generated" / "logs"
        tdir.mkdir(parents=True, exist_ok=True)
        tpath = tdir / "transcript.jsonl"
        try:
            tpath.write_text(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE"}) + "\n", encoding="utf-8")
            old_mtime = time.time() - 60.0
            os.utime(tpath, (old_mtime, old_mtime))

            seq1 = bs.record_in_flight(777888, "Old stale prompt", conv_id=conv_id, pid=os.getpid())
            # Backdate entry ts so age > 30s triggers transcript inspection
            with open(bs.IN_FLIGHT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["777888"]["ts"] = time.time() - 120.0
            data["ts"] = data["777888"]["ts"]
            bs.IN_FLIGHT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

            # Inject concurrent record_in_flight while reap inspects transcript
            orig_read_text = Path.read_text
            injected = []

            def _racy_read_text(path_obj, *args, **kwargs):
                res = orig_read_text(path_obj, *args, **kwargs)
                if path_obj == tpath and not injected:
                    new_seq = bs.record_in_flight(777888, "Fresh concurrent prompt", conv_id="conv-fresh", pid=os.getpid())
                    injected.append(new_seq)
                return res

            from unittest.mock import patch
            with patch.object(Path, "read_text", _racy_read_text):
                reaped = bs.reap_stale_in_flight_entries(max_age_seconds=1800.0)

            self.assertEqual(len(injected), 1)
            self.assertEqual(reaped, [])
            cur = bs.get_in_flight()
            self.assertIn("777888", cur)
            self.assertEqual(cur["777888"]["turn_seq"], injected[0])
            self.assertGreater(cur["777888"]["turn_seq"], seq1)
            self.assertEqual(cur["777888"]["prompt"], "Fresh concurrent prompt")
        finally:
            shutil.rmtree(brain_root / conv_id, ignore_errors=True)

    def test_in_flight_pid_unique_tmp_and_concurrent_flock_safety(self):
        import concurrent.futures
        from unittest.mock import patch

        replaced_srcs = []
        orig_replace = os.replace

        def _spy_replace(src, dst):
            replaced_srcs.append(str(src))
            return orig_replace(src, dst)

        with patch("tools.bridge_state.os.replace", side_effect=_spy_replace):
            seq = bs.record_in_flight(999111, "PID temp test", conv_id="conv-pid")
            self.assertIsNotNone(seq)

        self.assertTrue(any(s.endswith(f".tmp.{os.getpid()}") for s in replaced_srcs), replaced_srcs)

        # Concurrent multi-thread writes across channels must never lose entries or corrupt JSON
        channels = [10000 + i for i in range(12)]

        def _worker(ch):
            return bs.record_in_flight(ch, f"Prompt for {ch}", conv_id=f"conv-{ch}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            seqs = list(pool.map(_worker, channels))

        self.assertEqual(len(set(seqs)), len(channels))
        final = bs.get_in_flight()
        for ch in channels:
            self.assertIn(str(ch), final)
            self.assertEqual(final[str(ch)]["prompt"], f"Prompt for {ch}")

    def test_is_home_channel_excludes_baseball(self):
        class DummyChannel:
            def __init__(self, ch_id, cat_id=None, parent_id=None):
                self.id = ch_id
                self.category_id = cat_id
                self.parent_id = parent_id
                self.parent = None

        # #zero-chat is home
        zero_chat = DummyChannel(1542081375287640084, cat_id=1544953274363412533)
        self.assertTrue(bs.is_home_channel(zero_chat))

        # #baseball is excluded even though category is operations
        baseball_ch = DummyChannel(1548196929308065893, cat_id=1544953274363412533)
        self.assertFalse(bs.is_home_channel(baseball_ch))

        # Thread in #baseball is also excluded
        baseball_thread = DummyChannel(999999999, parent_id=1548196929308065893)
        self.assertFalse(bs.is_home_channel(baseball_thread))


if __name__ == "__main__":
    unittest.main()

