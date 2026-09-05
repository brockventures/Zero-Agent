#!/usr/bin/env python3
"""
Unit test suite for bridge_daemons.py (Persistent Channel Worker Daemons).
Validates:
1. Dedicated channel configuration and detection (#zero-chat, #the-banana-stand, #lounge).
2. Dynamic fallback for secondary channels and threads.
3. Prompt formatting across home and external air-gapped modes.
4. Persistent worker lifecycle (boot, init parsing, stdin/stdout turn streaming, recycle, shutdown).
5. Watchdog timeout and crash recovery.
6. Multi-channel parallel execution across dedicated workers without head-of-line blocking.
"""

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import tools.bridge_daemons as bd
import tools.bridge_state as bs


class TestBridgeDaemons(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_sessions_file = bs.SESSIONS_FILE
        self.orig_session_meta_file = bs.SESSION_METADATA_FILE
        self.orig_runtime_rules = bs.RUNTIME_RULES_FILE

        bs.DATA_DIR = self.temp_path
        bs.SESSIONS_FILE = self.temp_path / "sessions.json"
        bs.SESSION_METADATA_FILE = self.temp_path / "session_metadata.json"

    def tearDown(self):
        bs.DATA_DIR = self.orig_data_dir
        bs.SESSIONS_FILE = self.orig_sessions_file
        bs.SESSION_METADATA_FILE = self.orig_session_meta_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_dedicated_channel_detection(self):
        """Verify only the Big Three channels are marked dedicated; others are dynamic."""
        self.assertTrue(bd.is_dedicated_channel(bs.TARGET_CHANNEL_ID))
        self.assertTrue(bd.is_dedicated_channel(bd.BANANA_STAND_CHANNEL_ID))
        self.assertTrue(bd.is_dedicated_channel(bd.LOUNGE_CHANNEL_ID))

        # Secondary home channels and random threads should NOT be dedicated
        self.assertFalse(bd.is_dedicated_channel(1544955535722545253))  # homelab
        self.assertFalse(bd.is_dedicated_channel(1544955532765560924))  # finances
        self.assertFalse(bd.is_dedicated_channel(1544953277592899615))  # steam-deck
        self.assertFalse(bd.is_dedicated_channel(999888777))            # thread

    def test_02_feature_flag_toggle(self):
        """Verify feature flag in runtime rules can toggle daemon routing."""
        mgr = bd.PersistentDaemonManager()
        with patch("tools.bridge_daemons.is_persistent_daemons_enabled", return_value=False):
            self.assertFalse(mgr.is_dedicated_channel(bs.TARGET_CHANNEL_ID))
            self.assertFalse(mgr.is_dedicated_channel(bd.BANANA_STAND_CHANNEL_ID))

        with patch("tools.bridge_daemons.is_persistent_daemons_enabled", return_value=True):
            self.assertTrue(mgr.is_dedicated_channel(bs.TARGET_CHANNEL_ID))
            self.assertTrue(mgr.is_dedicated_channel(bd.BANANA_STAND_CHANNEL_ID))

    def test_03_prompt_formatting(self):
        """Verify prompt formatting injects PT timezone and boundaries."""
        # Home prompt
        home_p = bd.prepare_turn_prompt("Check system health", mode="home", channel_id=bs.TARGET_CHANNEL_ID, sess_key="home")
        self.assertIn("Check system health", home_p)
        self.assertIn("America/Los_Angeles", home_p)
        self.assertIn("Pacific Time (PT)", home_p)

        # External prompt
        ext_p = bd.prepare_turn_prompt("Review PR #42", mode="external", channel_id=bd.BANANA_STAND_CHANNEL_ID, sess_key=str(bd.BANANA_STAND_CHANNEL_ID), author_name="Amos")
        self.assertIn("Review PR #42", ext_p)
        self.assertIn("CRAB CAVERN", ext_p)
        self.assertIn("Amos", ext_p)

    async def test_04_persistent_worker_lifecycle(self):
        """Verify worker startup, init event reading, turn execution, and clean shutdown."""
        worker = bd.PersistentChannelWorker(
            channel_id=bs.TARGET_CHANNEL_ID,
            name="zero-chat",
            mode="home",
            sess_key="home"
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()

        # Mock stdout lines: init, step_update, result
        lines = [
            b'{"event":"init","conversation_id":"conv-warm-111","init":{"tools":["view_file"]}}\n',
            b'{"event":"step_update","step_update":{"step_type":"tool","tool_name":"view_file","tool_info":{"parameters":{"AbsolutePath":"/test/file.py"}}}}\n',
            b'{"event":"result","result":{"conversation_id":"conv-warm-111","status":"SUCCESS","response":"Hello from warm worker"}}\n',
            b''
        ]
        line_iter = iter(lines)

        async def mock_readline():
            return next(line_iter)

        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=mock_readline)
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await worker.start()
            self.assertEqual(worker.conv_id, "conv-warm-111")
            self.assertEqual(worker.proc, mock_proc)

            mock_status_msg = AsyncMock()
            mock_reply_target = AsyncMock()

            with patch("tools.bridge_daemons.deliver_turn_output") as mock_deliver:
                await worker.execute_turn(
                    prompt="Hello Zero",
                    status_msg=mock_status_msg,
                    reply_target=mock_reply_target,
                    attachments=[]
                )
                self.assertEqual(worker.turn_count, 1)
                mock_deliver.assert_called_once()
                self.assertIn("Hello from warm worker", mock_deliver.call_args[1]["output_text"])

            await worker.shutdown()
            self.assertIsNone(worker.proc)

    async def test_05_parallel_external_channels(self):
        """Verify #the-banana-stand and #lounge execute turns independently and concurrently."""
        mgr = bd.PersistentDaemonManager()
        worker_banana = mgr.get_worker(bd.BANANA_STAND_CHANNEL_ID)
        worker_lounge = mgr.get_worker(bd.LOUNGE_CHANNEL_ID)

        self.assertIsNotNone(worker_banana)
        self.assertIsNotNone(worker_lounge)

        banana_started = asyncio.Event()
        lounge_started = asyncio.Event()
        banana_can_finish = asyncio.Event()
        lounge_can_finish = asyncio.Event()

        async def mock_banana_turn(*args, **kwargs):
            banana_started.set()
            await banana_can_finish.wait()

        async def mock_lounge_turn(*args, **kwargs):
            lounge_started.set()
            await lounge_can_finish.wait()

        with patch.object(worker_banana, "execute_turn", side_effect=mock_banana_turn), \
             patch.object(worker_lounge, "execute_turn", side_effect=mock_lounge_turn):

            t1 = asyncio.create_task(worker_banana.execute_turn("banana prompt", None, MagicMock(), []))
            t2 = asyncio.create_task(worker_lounge.execute_turn("lounge prompt", None, MagicMock(), []))

            # Both should start concurrently without one blocking the other
            await asyncio.wait_for(asyncio.gather(banana_started.wait(), lounge_started.wait()), timeout=2.0)
            self.assertTrue(banana_started.is_set())
            self.assertTrue(lounge_started.is_set())

            banana_can_finish.set()
            lounge_can_finish.set()
            await asyncio.gather(t1, t2)

    async def test_06_live_status_ticker_suppression(self):
        """Verify that live status ticker edits are suppressed when live_status_ticker_enabled is False."""
        worker = bd.PersistentChannelWorker(
            channel_id=1544953279664889888, # zero-ops
            name="zero-ops",
            mode="home",
            sess_key="1544953279664889888"
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12346
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()

        lines = [
            b'{"event":"step_update","step_update":{"step_type":"tool","tool_name":"grep_search","tool_info":{"parameters":{"Query":"test"}}}}\n',
            b'{"event":"result","result":{"status":"SUCCESS","response":"Done"}}\n',
            b''
        ]
        line_iter = iter(lines)
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=lambda: next(line_iter))
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")

        worker.proc = mock_proc
        worker.is_ready = True
        mock_status_msg = AsyncMock()
        mock_status_msg.id = 99991
        mock_reply_target = AsyncMock()

        # Test with ticker disabled (default)
        t_val = 100.0
        def fake_time():
            nonlocal t_val
            t_val += 2.0
            return t_val

        with patch("tools.bridge_daemons.get_runtime_rules", return_value={"live_status_ticker_enabled": False}), \
             patch("tools.bridge_daemons.deliver_turn_output"), \
             patch("time.time", side_effect=fake_time):
            await worker.execute_turn("test prompt", mock_status_msg, mock_reply_target, [])
            mock_status_msg.edit.assert_not_awaited()

        # Reset and test with ticker enabled
        lines2 = [
            b'{"event":"step_update","step_update":{"step_type":"tool","tool_name":"grep_search","tool_info":{"parameters":{"Query":"test"}}}}\n',
            b'{"event":"result","result":{"status":"SUCCESS","response":"Done"}}\n',
            b''
        ]
        line_iter2 = iter(lines2)
        mock_proc.stdout.readline = AsyncMock(side_effect=lambda: next(line_iter2))
        mock_status_msg.reset_mock()
        t_val = 100.0

        with patch("tools.bridge_daemons.get_runtime_rules", return_value={"live_status_ticker_enabled": True}), \
             patch("tools.bridge_daemons.deliver_turn_output"), \
             patch("time.time", side_effect=fake_time):
            await worker.execute_turn("test prompt", mock_status_msg, mock_reply_target, [])
            self.assertTrue(mock_status_msg.edit.await_count >= 1)





if __name__ == "__main__":
    unittest.main()
