#!/usr/bin/env python3
"""
Test Suite for Parallel Channel Worker Architecture.
Validates:
1. Inter-channel concurrency: Different channels run in parallel.
2. Intra-channel serialization: Turns in the same channel run strictly sequentially (FIFO).
3. Priority fast-lane: #zero-chat runs immediately without background semaphore delays.
4. Multi-channel in-flight persistence and cleanup.
5. Channel-isolated mid-turn steering.
"""
import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import tools.bridge_handlers as bh
import tools.bridge_runner as br
import tools.bridge_state as bs


class TestParallelChannels(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_in_flight = bs.IN_FLIGHT_FILE
        self.orig_queue_file = bs.QUEUE_FILE

        bs.DATA_DIR = self.temp_path
        bs.IN_FLIGHT_FILE = self.temp_path / "in_flight_turn.json"
        bs.QUEUE_FILE = self.temp_path / "turn_queue.json"

        # Clear active tasks and queues
        bh.channel_queues.clear()
        bh.channel_active_tasks.clear()
        bh.channel_active_status_msgs.clear()
        bh.channel_concurrency_semaphore = None
        br.channel_active_procs.clear()
        br.active_proc = None
        br.ext_active_proc = None

    def tearDown(self):
        for t in list(bh.channel_active_tasks.values()):
            if t and not t.done():
                t.cancel()
        bh.channel_queues.clear()
        bh.channel_active_tasks.clear()
        bh.channel_active_status_msgs.clear()
        bh.channel_concurrency_semaphore = None

        bs.DATA_DIR = self.orig_data_dir
        bs.IN_FLIGHT_FILE = self.orig_in_flight
        bs.QUEUE_FILE = self.orig_queue_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_01_inter_channel_concurrency(self):
        """Verify that turns in different channels (#zero-chat and #zero-ops) run in parallel."""
        home_queue = bs.PersistentTurnQueue(bs.QUEUE_FILE)
        ch_zero_chat = bs.TARGET_CHANNEL_ID
        ch_zero_ops = 1544953279664889888

        turn1_started = asyncio.Event()
        turn2_started = asyncio.Event()
        turn1_can_finish = asyncio.Event()
        turn2_can_finish = asyncio.Event()

        active_concurrent = []
        max_concurrent_seen = 0

        async def mock_execute_turn(prompt, status_msg, reply_target, attachments, mode, channel_id, **kwargs):
            nonlocal max_concurrent_seen
            active_concurrent.append(channel_id)
            max_concurrent_seen = max(max_concurrent_seen, len(active_concurrent))

            if channel_id == ch_zero_ops:
                turn1_started.set()
                await turn1_can_finish.wait()
            elif channel_id == ch_zero_chat:
                turn2_started.set()
                await turn2_can_finish.wait()

            active_concurrent.remove(channel_id)

        mock_bot = MagicMock()
        worker_task = asyncio.create_task(bh.queue_worker(home_queue, mock_bot))

        try:
            with patch("tools.bridge_handlers.execute_agy_turn", side_effect=mock_execute_turn):
                # Enqueue turn in #zero-ops first
                await home_queue.put({
                    "prompt": "ops_long_running_task",
                    "status_msg": None,
                    "reply_target": MagicMock(),
                    "attachments": [],
                    "channel_id": ch_zero_ops
                })

                # Wait for ops turn to start running
                await asyncio.wait_for(turn1_started.wait(), timeout=1.0)
                self.assertIn(ch_zero_ops, active_concurrent)

                # While ops turn is actively in-flight, enqueue a turn in #zero-chat
                await home_queue.put({
                    "prompt": "chat_user_message",
                    "status_msg": None,
                    "reply_target": MagicMock(),
                    "attachments": [],
                    "channel_id": ch_zero_chat
                })

                # Verify #zero-chat starts running CONCURRENTLY without waiting for ops
                await asyncio.wait_for(turn2_started.wait(), timeout=1.0)
                self.assertIn(ch_zero_chat, active_concurrent)
                self.assertEqual(max_concurrent_seen, 2)

                # Release both turns
                turn1_can_finish.set()
                turn2_can_finish.set()
                await asyncio.sleep(0.05)

                self.assertEqual(len(active_concurrent), 0)
        finally:
            worker_task.cancel()

    async def test_02_intra_channel_serialization(self):
        """Verify that multiple turns in the SAME channel run strictly sequentially (FIFO)."""
        home_queue = bs.PersistentTurnQueue(bs.QUEUE_FILE)
        ch_id = bs.TARGET_CHANNEL_ID

        execution_log = []
        turn1_can_finish = asyncio.Event()

        async def mock_execute_turn(prompt, status_msg, reply_target, attachments, mode, channel_id, **kwargs):
            execution_log.append(f"start:{prompt}")
            if prompt == "turn_1":
                await turn1_can_finish.wait()
            execution_log.append(f"end:{prompt}")

        mock_bot = MagicMock()
        worker_task = asyncio.create_task(bh.queue_worker(home_queue, mock_bot))

        try:
            with patch("tools.bridge_handlers.execute_agy_turn", side_effect=mock_execute_turn):
                await home_queue.put({
                    "prompt": "turn_1",
                    "status_msg": None,
                    "reply_target": MagicMock(),
                    "attachments": [],
                    "channel_id": ch_id
                })
                await home_queue.put({
                    "prompt": "turn_2",
                    "status_msg": None,
                    "reply_target": MagicMock(),
                    "attachments": [],
                    "channel_id": ch_id
                })

                await asyncio.sleep(0.05)
                # turn_1 should have started, but turn_2 should NOT have started yet
                self.assertIn("start:turn_1", execution_log)
                self.assertNotIn("start:turn_2", execution_log)

                # Allow turn 1 to complete
                turn1_can_finish.set()
                await asyncio.sleep(0.05)

                # Now turn_2 must run after turn_1
                self.assertEqual(execution_log, [
                    "start:turn_1",
                    "end:turn_1",
                    "start:turn_2",
                    "end:turn_2"
                ])
        finally:
            worker_task.cancel()

    async def test_03_zero_chat_priority_fast_lane(self):
        """Verify that #zero-chat bypasses secondary channel concurrency semaphore."""
        home_queue = bs.PersistentTurnQueue(bs.QUEUE_FILE)
        ch_zero_chat = bs.TARGET_CHANNEL_ID

        # Artificially set semaphore to 1 so only 1 secondary channel can run at once
        bh.channel_concurrency_semaphore = asyncio.Semaphore(1)

        sec_ch1 = 1544953279664889888
        sec_ch2 = 1544955532765560924

        sec1_started = asyncio.Event()
        chat_started = asyncio.Event()
        sec1_can_finish = asyncio.Event()
        chat_can_finish = asyncio.Event()

        active = []

        async def mock_execute_turn(prompt, status_msg, reply_target, attachments, mode, channel_id, **kwargs):
            active.append(channel_id)
            if channel_id == sec_ch1:
                sec1_started.set()
                await sec1_can_finish.wait()
            elif channel_id == ch_zero_chat:
                chat_started.set()
                await chat_can_finish.wait()
            active.remove(channel_id)

        mock_bot = MagicMock()
        worker_task = asyncio.create_task(bh.queue_worker(home_queue, mock_bot))

        try:
            with patch("tools.bridge_handlers.execute_agy_turn", side_effect=mock_execute_turn):
                # Enqueue secondary channel 1 (exhausts the 1 semaphore slot)
                await home_queue.put({
                    "prompt": "sec1",
                    "status_msg": None,
                    "reply_target": MagicMock(),
                    "attachments": [],
                    "channel_id": sec_ch1
                })
                await asyncio.wait_for(sec1_started.wait(), timeout=1.0)
                self.assertEqual(bh.channel_concurrency_semaphore._value, 0)

                # Enqueue #zero-chat prompt: should run immediately despite semaphore == 0
                await home_queue.put({
                    "prompt": "ryan_chat_priority",
                    "status_msg": None,
                    "reply_target": MagicMock(),
                    "attachments": [],
                    "channel_id": ch_zero_chat
                })

                await asyncio.wait_for(chat_started.wait(), timeout=1.0)
                self.assertIn(ch_zero_chat, active)
                self.assertIn(sec_ch1, active)

                sec1_can_finish.set()
                chat_can_finish.set()
                await asyncio.sleep(0.05)
        finally:
            worker_task.cancel()

    def test_04_multi_channel_in_flight_persistence(self):
        """Verify in_flight_turn.json properly manages concurrent channels without clobbering."""
        ch1 = bs.TARGET_CHANNEL_ID
        ch2 = 1544953279664889888

        # Record ch1
        bs.record_in_flight(ch1, "prompt_1", conv_id="conv-1")
        self.assertTrue(bs.IN_FLIGHT_FILE.exists())

        with open(bs.IN_FLIGHT_FILE) as f:
            data = json.load(f)
        self.assertIn(str(ch1), data)
        self.assertEqual(data["prompt"], "prompt_1")

        # Record ch2 while ch1 is still running
        bs.record_in_flight(ch2, "prompt_2", conv_id="conv-2")
        with open(bs.IN_FLIGHT_FILE) as f:
            data = json.load(f)
        self.assertIn(str(ch1), data)
        self.assertIn(str(ch2), data)

        # Clear ch1: ch2 should remain
        bs.clear_in_flight(ch1)
        self.assertTrue(bs.IN_FLIGHT_FILE.exists())
        with open(bs.IN_FLIGHT_FILE) as f:
            data = json.load(f)
        self.assertNotIn(str(ch1), data)
        self.assertIn(str(ch2), data)
        self.assertEqual(data["prompt"], "prompt_2")

        # Clear ch2: file should now be cleanly unlinked
        bs.clear_in_flight(ch2)
        self.assertFalse(bs.IN_FLIGHT_FILE.exists())

    async def test_05_steering_isolation_per_channel(self):
        """Verify mid-turn steering sends SIGINT and edits status only for the targeted channel."""
        ch1 = bs.TARGET_CHANNEL_ID
        ch2 = 1544953279664889888

        proc1 = MagicMock()
        proc1.returncode = None
        proc1.send_signal = MagicMock()

        proc2 = MagicMock()
        proc2.returncode = None
        proc2.send_signal = MagicMock()

        br.channel_active_procs[ch1] = proc1
        br.channel_active_procs[ch2] = proc2

        mock_status1 = AsyncMock()
        mock_status2 = AsyncMock()
        bh.channel_active_status_msgs[ch1] = mock_status1
        bh.channel_active_status_msgs[ch2] = mock_status2

        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        # Message arrives in ch1
        msg1 = MagicMock()
        msg1.channel.id = ch1
        msg1.author.id = bs.OWNER_USER_ID
        msg1.author.bot = False
        msg1.content = "New steer directive for chat"
        msg1.attachments = []
        msg1.created_at.timestamp.return_value = time.time()

        home_queue = bs.PersistentTurnQueue(bs.QUEUE_FILE)
        await bh.handle_message(msg1, mock_bot, home_queue, AsyncMock())

        # Verify ONLY proc1 received SIGINT and ONLY mock_status1 was edited
        proc1.send_signal.assert_called_once()
        proc2.send_signal.assert_not_called()
        mock_status1.edit.assert_awaited_once()
        mock_status2.edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
