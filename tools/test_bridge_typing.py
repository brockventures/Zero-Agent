"""Unit tests for bridge queued typing indicator, slot-wait coalescing,
interactive channel fast-lane routing, and worker pool concurrency.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.bridge_handlers as bh
import tools.bridge_state as bs
from tools.bridge_handlers import (
    FAST_LANE_CHANNEL_IDS,
    channel_queues,
    channel_waiting_items,
    get_concurrency_semaphore,
    is_fast_lane_channel,
    resilient_typing,
    run_channel_turn_worker,
    run_thread_turn_worker,
    try_coalesce_pending_turn,
)


class TestBridgeTypingAndConcurrency(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bh.channel_concurrency_semaphore = None
        channel_waiting_items.clear()
        channel_queues.clear()
        bh.channel_active_tasks.clear()
        bh.channel_active_prompts.clear()
        bh.channel_active_status_msgs.clear()

    def tearDown(self):
        bh.channel_concurrency_semaphore = None
        channel_waiting_items.clear()
        channel_queues.clear()
        bh.channel_active_tasks.clear()
        bh.channel_active_prompts.clear()
        bh.channel_active_status_msgs.clear()

    def test_runtime_rules_max_parallel_workers_is_4(self):
        """Verify config/runtime_rules.json configures 4 parallel workers."""
        rules_path = Path("config/runtime_rules.json")
        self.assertTrue(rules_path.exists())
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("max_parallel_workers"), 4)

    def test_get_concurrency_semaphore_default_workers(self):
        """Verify get_concurrency_semaphore defaults to 4 max parallel workers."""
        bh.channel_concurrency_semaphore = None
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={}):
            sem = get_concurrency_semaphore()
            self.assertIsInstance(sem, asyncio.Semaphore)
            self.assertEqual(sem._value, 4)

    def test_get_concurrency_semaphore_from_rules(self):
        """Verify get_concurrency_semaphore honors max_parallel_workers from runtime rules."""
        bh.channel_concurrency_semaphore = None
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"max_parallel_workers": 4}):
            sem = get_concurrency_semaphore()
            self.assertEqual(sem._value, 4)

    def test_fast_lane_channel_ids_membership(self):
        """Verify FAST_LANE_CHANNEL_IDS contains interactive gotchi and zero-ops channels."""
        expected_channels = {1561509863018856468, 1562246590943461429, 1544953279664889888}
        self.assertEqual(FAST_LANE_CHANNEL_IDS, expected_channels)

    def test_is_fast_lane_channel(self):
        """Verify is_fast_lane_channel correctly identifies root, interactive, and dedicated channels."""
        # Root home channel
        self.assertTrue(is_fast_lane_channel(bs.TARGET_CHANNEL_ID))
        self.assertTrue(is_fast_lane_channel(str(bs.TARGET_CHANNEL_ID)))

        # Interactive channels in FAST_LANE_CHANNEL_IDS
        for ch_id in FAST_LANE_CHANNEL_IDS:
            self.assertTrue(is_fast_lane_channel(ch_id))
            self.assertTrue(is_fast_lane_channel(str(ch_id)))

        # Dedicated 24/7 channels
        self.assertTrue(is_fast_lane_channel(1534436119888793750))  # banana-stand
        self.assertTrue(is_fast_lane_channel(1534452820995080192))  # lounge

        # Secondary background channels are NOT fast lane
        self.assertFalse(is_fast_lane_channel(123456789012345678))
        self.assertFalse(is_fast_lane_channel("123456789012345678"))

        # Invalid or non-numeric channel IDs
        self.assertFalse(is_fast_lane_channel("invalid_channel_id"))
        self.assertFalse(is_fast_lane_channel(None))

    async def test_resilient_typing_dynamic_callable_target_switching(self):
        """Verify resilient_typing continuously polls dynamic callable and follows target changes."""
        target_a = MagicMock()
        target_a.channel = MagicMock()
        target_a.channel.typing = AsyncMock()

        target_b = MagicMock()
        target_b.channel = MagicMock()
        target_b.channel.typing = AsyncMock()

        current_target = target_a

        async with resilient_typing(lambda: current_target, interval=0.03):
            await asyncio.sleep(0.05)
            # Switch target to target_b mid-run
            current_target = target_b
            await asyncio.sleep(0.08)

        self.assertGreaterEqual(target_a.channel.typing.call_count, 1)
        self.assertGreaterEqual(target_b.channel.typing.call_count, 1)

    def test_try_coalesce_pending_turn_in_channel_waiting_items(self):
        """Verify try_coalesce_pending_turn merges follow-up message into slot-waiting item."""
        cid = 777123
        aid = 888456
        author_name = "Ryan"

        waiting_item = {
            "prompt": "Inspect the container logs",
            "raw_prompt": "Inspect the container logs",
            "channel_id": cid,
            "author_id": aid,
            "author_name": author_name,
            "is_steer": False,
            "attachments": ["/tmp/trace.log"],
            "reply_target": MagicMock(),
            "coalesced_count": 1,
        }
        channel_waiting_items[cid] = waiting_item

        new_msg = MagicMock()
        new_msg.id = 999001

        result = try_coalesce_pending_turn(
            channel_id=cid,
            author_id=aid,
            author_name=author_name,
            new_text="Also check memory consumption",
            new_attachments=["/tmp/meminfo.txt"],
            new_msg=new_msg,
            is_group=False,
        )

        self.assertTrue(result)
        self.assertEqual(
            waiting_item["prompt"],
            "Inspect the container logs\n\nAlso check memory consumption",
        )
        self.assertEqual(
            waiting_item["raw_prompt"],
            "Inspect the container logs\n\nAlso check memory consumption",
        )
        self.assertEqual(
            waiting_item["attachments"],
            ["/tmp/trace.log", "/tmp/meminfo.txt"],
        )
        self.assertIs(waiting_item["reply_target"], new_msg)
        self.assertEqual(waiting_item["coalesced_count"], 2)

    def test_try_coalesce_pending_turn_waiting_item_different_author_rejected(self):
        """Verify messages from a different author do not coalesce into slot-waiting item."""
        cid = 777124
        channel_waiting_items[cid] = {
            "prompt": "Work on task A",
            "channel_id": cid,
            "author_id": 111,
            "author_name": "Alice",
            "attachments": [],
        }

        result = try_coalesce_pending_turn(
            channel_id=cid,
            author_id=222,
            author_name="Bob",
            new_text="Work on task B",
        )

        self.assertFalse(result)
        self.assertEqual(channel_waiting_items[cid]["prompt"], "Work on task A")

    def test_try_coalesce_pending_turn_waiting_item_steering(self):
        """Verify steering items in channel_waiting_items are formatted properly on coalesce."""
        cid = 777125
        aid = 333
        channel_waiting_items[cid] = {
            "prompt": "[MID-TURN GROUP CONVERSATION UPDATE]\nPart 1",
            "raw_prompt": "Part 1",
            "original_task": "Build feature X",
            "channel_id": cid,
            "author_id": aid,
            "author_name": "Charlie",
            "is_steer": True,
            "attachments": [],
        }

        result = try_coalesce_pending_turn(
            channel_id=cid,
            author_id=aid,
            author_name="Charlie",
            new_text="Part 2",
            is_group=True,
        )

        self.assertTrue(result)
        self.assertEqual(channel_waiting_items[cid]["raw_prompt"], "Part 1\n\nPart 2")
        self.assertIn("Part 1\n\nPart 2", channel_waiting_items[cid]["prompt"])
        self.assertIn("Build feature X", channel_waiting_items[cid]["prompt"])

    async def test_run_channel_turn_worker_fast_lane_bypasses_semaphore(self):
        """Verify fast-lane channels do not acquire concurrency semaphore."""
        fast_cid = 1561509863018856468  # Gotchi channel
        q = asyncio.Queue()
        mock_reply = MagicMock()
        mock_reply.typing = AsyncMock()

        item = {
            "prompt": "Fast-lane test task",
            "reply_target": mock_reply,
            "channel_id": fast_cid,
        }
        await q.put(item)

        mock_sem = MagicMock()
        mock_sem.acquire = AsyncMock()
        mock_sem.release = MagicMock()

        executed = False

        async def fake_execute(*args, **kwargs):
            nonlocal executed
            executed = True

        with patch("tools.bridge_handlers.get_concurrency_semaphore", return_value=mock_sem), \
             patch("tools.bridge_handlers.execute_agy_turn", side_effect=fake_execute):
            await run_channel_turn_worker(fast_cid, q, MagicMock())

        self.assertTrue(executed)
        mock_sem.acquire.assert_not_called()
        self.assertNotIn(fast_cid, channel_waiting_items)

    async def test_run_channel_turn_worker_secondary_channel_waits_and_coalesces(self):
        """Verify secondary channels acquire semaphore, track channel_waiting_items,
        and coalesce follow-ups arriving during semaphore wait.
        """
        sec_cid = 999888777
        q = asyncio.Queue()
        target_1 = MagicMock()
        target_1.typing = AsyncMock()

        item = {
            "prompt": "Initial chunk",
            "raw_prompt": "Initial chunk",
            "reply_target": target_1,
            "channel_id": sec_cid,
            "author_id": 444,
            "author_name": "Dave",
            "attachments": ["/tmp/file1.txt"],
        }
        await q.put(item)

        saw_waiting_item_during_gate = False
        coalesced_during_wait = False

        original_sem = asyncio.Semaphore(1)
        await original_sem.acquire()  # Exhaust the semaphore

        target_2 = MagicMock()
        target_2.typing = AsyncMock()

        async def worker_runner():
            with patch("tools.bridge_handlers.get_concurrency_semaphore", return_value=original_sem), \
                 patch("tools.bridge_handlers.execute_agy_turn", new_callable=AsyncMock) as mock_exec:
                # Launch worker in task
                worker_task = asyncio.create_task(
                    run_channel_turn_worker(sec_cid, q, MagicMock())
                )

                # Wait until item is in channel_waiting_items
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if sec_cid in channel_waiting_items:
                        break

                nonlocal saw_waiting_item_during_gate, coalesced_during_wait
                saw_waiting_item_during_gate = (sec_cid in channel_waiting_items)

                # Send follow-up message while worker is blocked on semaphore
                coalesced_during_wait = try_coalesce_pending_turn(
                    channel_id=sec_cid,
                    author_id=444,
                    author_name="Dave",
                    new_text="Follow-up chunk",
                    new_attachments=["/tmp/file2.txt"],
                    new_msg=target_2,
                )

                # Release the semaphore to let worker proceed
                original_sem.release()

                await worker_task

                # Verify execute_agy_turn received coalesced parameters
                mock_exec.assert_called_once()
                call_args = mock_exec.call_args[0]
                self.assertEqual(call_args[0], "Initial chunk\n\nFollow-up chunk")
                self.assertIs(call_args[2], target_2)
                self.assertEqual(call_args[3], ["/tmp/file1.txt", "/tmp/file2.txt"])

        await worker_runner()
        self.assertTrue(saw_waiting_item_during_gate)
        self.assertTrue(coalesced_during_wait)
        self.assertNotIn(sec_cid, channel_waiting_items)

    async def test_run_channel_turn_worker_cleans_waiting_item_on_cancel(self):
        """Verify channel_waiting_items is cleaned up if worker task is cancelled during semaphore wait."""
        sec_cid = 888777666
        q = asyncio.Queue()
        target = MagicMock()
        target.typing = AsyncMock()

        item = {
            "prompt": "Task that gets cancelled",
            "reply_target": target,
            "channel_id": sec_cid,
            "author_id": 555,
            "author_name": "Eve",
        }
        await q.put(item)

        sem = asyncio.Semaphore(0)  # Always blocks
        with patch("tools.bridge_handlers.get_concurrency_semaphore", return_value=sem):
            worker_task = asyncio.create_task(
                run_channel_turn_worker(sec_cid, q, MagicMock())
            )
            for _ in range(50):
                await asyncio.sleep(0.01)
                if sec_cid in channel_waiting_items:
                    break

            self.assertIn(sec_cid, channel_waiting_items)
            worker_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker_task

            self.assertNotIn(sec_cid, channel_waiting_items)

    async def test_run_thread_turn_worker_typing_active_before_semaphore(self):
        """Verify run_thread_turn_worker activates typing indicator before acquiring semaphore."""
        mock_reply = MagicMock()
        mock_reply.channel = MagicMock()
        mock_reply.channel.typing = AsyncMock()

        item = {
            "prompt": "Thread task",
            "status_msg": None,
            "reply_target": mock_reply,
            "attachments": [],
            "channel_id": 12345,
        }

        typing_entered_before_acquire = False
        sem = asyncio.Semaphore(0)

        async def delayed_release():
            await asyncio.sleep(0.02)
            nonlocal typing_entered_before_acquire
            if mock_reply.channel.typing.call_count >= 1:
                typing_entered_before_acquire = True
            sem.release()

        asyncio.create_task(delayed_release())

        with patch("tools.bridge_handlers.get_concurrency_semaphore", return_value=sem), \
             patch("tools.bridge_handlers.execute_agy_turn", new_callable=AsyncMock) as mock_exec:
            await run_thread_turn_worker(item, MagicMock())
            mock_exec.assert_called_once()

        self.assertTrue(typing_entered_before_acquire)


if __name__ == "__main__":
    unittest.main()
