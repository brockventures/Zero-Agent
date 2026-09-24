#!/usr/bin/env python3
"""
test_task_settle.py — Unit Tests for TaskSettle Supervisor Protocol.
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tools.task_settle import (
    evaluate_and_settle_turn,
    extract_turn_lines,
    get_turn_pending_tasks,
    is_task_completed,
    wait_for_tasks_to_settle,
)


class TestTaskSettle(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.conv_id = "test-conv-1234"
        self.conv_path = Path(self.test_dir) / self.conv_id
        self.logs_dir = self.conv_path / ".system_generated" / "logs"
        self.msgs_dir = self.conv_path / ".system_generated" / "messages"
        self.tasks_dir = self.conv_path / ".system_generated" / "tasks"

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.msgs_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_extract_turn_lines(self):
        lines = [
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "first prompt"}),
            json.dumps({"type": "PLANNER_RESPONSE", "content": "first response"}),
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "second prompt"}),
            json.dumps({"type": "PLANNER_RESPONSE", "content": "second response"}),
        ]
        extracted = extract_turn_lines(lines)
        self.assertEqual(len(extracted), 2)
        d = json.loads(extracted[0])
        self.assertEqual(d.get("content"), "second prompt")

    def test_get_turn_pending_tasks_empty_when_no_tasks(self):
        tpath = self.logs_dir / "transcript.jsonl"
        lines = [
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "run check"}),
            json.dumps({"type": "PLANNER_RESPONSE", "content": "everything nominal"}),
        ]
        tpath.write_text("\n".join(lines), encoding="utf-8")

        pending = get_turn_pending_tasks(self.conv_id, brain_dir=self.test_dir)
        self.assertEqual(pending, [])

    def test_get_turn_pending_tasks_finds_uncompleted_task(self):
        tpath = self.logs_dir / "transcript.jsonl"
        lines = [
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "start build"}),
            json.dumps({
                "type": "GENERIC",
                "source": "MODEL",
                "content": "Tool is running as a background task with task id: test-conv-1234/task-999\nTask Description: sleep 10",
            }),
            json.dumps({"type": "PLANNER_RESPONSE", "content": "Wait for background task to complete..."}),
        ]
        tpath.write_text("\n".join(lines), encoding="utf-8")

        pending = get_turn_pending_tasks(self.conv_id, brain_dir=self.test_dir)
        self.assertEqual(pending, ["task-999"])

    def test_get_turn_pending_tasks_cleared_by_disk_message(self):
        tpath = self.logs_dir / "transcript.jsonl"
        lines = [
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "start build"}),
            json.dumps({
                "type": "GENERIC",
                "source": "MODEL",
                "content": "Tool is running as a background task with task id: test-conv-1234/task-999",
            }),
        ]
        tpath.write_text("\n".join(lines), encoding="utf-8")

        # Before message creation: pending
        self.assertEqual(get_turn_pending_tasks(self.conv_id, brain_dir=self.test_dir), ["task-999"])

        # Write completion message
        msg_file = self.msgs_dir / "msg-1.json"
        msg_payload = {
            "sender": "test-conv-1234/task-999",
            "content": 'Task id "test-conv-1234/task-999" finished with result: OK',
        }
        msg_file.write_text(json.dumps(msg_payload), encoding="utf-8")

        # After message creation: not pending
        self.assertEqual(get_turn_pending_tasks(self.conv_id, brain_dir=self.test_dir), [])

    def test_is_task_completed(self):
        self.assertFalse(is_task_completed(self.conv_id, "task-555", brain_dir=self.test_dir))

        msg_file = self.msgs_dir / "msg-555.json"
        msg_payload = {
            "sender": "test-conv-1234/task-555",
            "content": 'Task id "task-555" finished with exit code 0',
        }
        msg_file.write_text(json.dumps(msg_payload), encoding="utf-8")

        self.assertTrue(is_task_completed(self.conv_id, "task-555", brain_dir=self.test_dir))

    def test_cancellation_message_does_not_clear_pending_task(self):
        tpath = self.logs_dir / "transcript.jsonl"
        lines = [
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "run long test"}),
            json.dumps({
                "type": "GENERIC",
                "source": "MODEL",
                "content": "Tool is running as a background task with task id: test-conv-1234/task-246",
            }),
        ]
        tpath.write_text("\n".join(lines), encoding="utf-8")

        # Cancellation message written to disk
        msg_file = self.msgs_dir / "msg-cancel.json"
        msg_payload = {
            "sender": "test-conv-1234/task-246",
            "content": 'Task id "test-conv-1234/task-246" was canceled with result:\nTool execution was canceled',
        }
        msg_file.write_text(json.dumps(msg_payload), encoding="utf-8")

        # Must NOT be considered finished or completed
        self.assertFalse(is_task_completed(self.conv_id, "task-246", brain_dir=self.test_dir))
        self.assertEqual(get_turn_pending_tasks(self.conv_id, brain_dir=self.test_dir), ["task-246"])

    async def test_wait_for_tasks_to_settle_success(self):
        # Simulate background task completing 0.2s later
        async def delayed_complete():
            await asyncio.sleep(0.15)
            msg_file = self.msgs_dir / "msg-async.json"
            msg_file.write_text(json.dumps({
                "sender": "test-conv-1234/task-async",
                "content": 'Task id "task-async" finished',
            }), encoding="utf-8")

        asyncio.create_task(delayed_complete())

        settled, elapsed, still_pending = await wait_for_tasks_to_settle(
            conv_id=self.conv_id,
            pending_tasks=["task-async"],
            brain_dir=self.test_dir,
            timeout_seconds=2.0,
            poll_interval=0.05,
        )
        self.assertTrue(settled)
        self.assertEqual(still_pending, [])
        self.assertLess(elapsed, 1.5)

    async def test_wait_for_tasks_to_settle_timeout(self):
        # Never completes
        settled, elapsed, still_pending = await wait_for_tasks_to_settle(
            conv_id=self.conv_id,
            pending_tasks=["task-neverending"],
            brain_dir=self.test_dir,
            timeout_seconds=0.2,
            poll_interval=0.05,
        )
        self.assertFalse(settled)
        self.assertEqual(still_pending, ["task-neverending"])
        self.assertGreaterEqual(elapsed, 0.2)

    async def test_evaluate_and_settle_turn_success(self):
        tpath = self.logs_dir / "transcript.jsonl"
        lines = [
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "fetch map"}),
            json.dumps({
                "type": "GENERIC",
                "source": "MODEL",
                "content": "Tool is running as a background task with task id: test-conv-1234/task-777",
            }),
            json.dumps({"type": "PLANNER_RESPONSE", "content": "Wait for background task to complete..."}),
        ]
        tpath.write_text("\n".join(lines), encoding="utf-8")

        # Delayed completion
        async def delayed_complete():
            await asyncio.sleep(0.1)
            msg_file = self.msgs_dir / "msg-777.json"
            msg_file.write_text(json.dumps({
                "sender": "test-conv-1234/task-777",
                "content": 'Task id "task-777" finished with output: tile.png',
            }), encoding="utf-8")

        asyncio.create_task(delayed_complete())

        mock_reinvoke = AsyncMock(return_value="Here is the final map review with high resolution tiles.")

        was_settled, result = await evaluate_and_settle_turn(
            conv_id=self.conv_id,
            channel_id=123456,
            mode="external",
            status_msg=None,
            reply_target=None,
            reinvoke_coro_fn=mock_reinvoke,
            timeout_seconds=2.0,
            brain_dir=self.test_dir,
        )

        self.assertTrue(was_settled)
        self.assertEqual(result, "Here is the final map review with high resolution tiles.")
        mock_reinvoke.assert_called_once()
        self.assertIn("Background task(s) task-777 completed", mock_reinvoke.call_args.kwargs["prompt"])
        self.assertTrue(mock_reinvoke.call_args.kwargs["is_settle_reinvocation"])

    async def test_evaluate_and_settle_turn_timeout_returns_clean_notice_when_leak(self):
        tpath = self.logs_dir / "transcript.jsonl"
        lines = [
            json.dumps({"type": "USER_INPUT", "source": "USER", "content": "long task"}),
            json.dumps({
                "type": "GENERIC",
                "source": "MODEL",
                "content": "Tool is running as a background task with task id: test-conv-1234/task-999",
            }),
            json.dumps({"type": "PLANNER_RESPONSE", "content": "Wait for background task to complete..."}),
        ]
        tpath.write_text("\n".join(lines), encoding="utf-8")

        mock_reinvoke = AsyncMock()

        # Case 1: External mode with leak text suppresses placeholder notices (silent background execution)
        was_settled, result = await evaluate_and_settle_turn(
            conv_id=self.conv_id,
            channel_id=123456,
            mode="external",
            status_msg=None,
            reply_target=None,
            reinvoke_coro_fn=mock_reinvoke,
            timeout_seconds=0.1,
            brain_dir=self.test_dir,
            current_text="An async command is running. Task log: /tmp/log\nMatch: b'foo'",
        )

        self.assertFalse(was_settled)
        self.assertIsNone(result)

        # Case 2: Home mode with empty text
        was_settled, result_home = await evaluate_and_settle_turn(
            conv_id=self.conv_id,
            channel_id=123456,
            mode="home",
            status_msg=None,
            reply_target=None,
            reinvoke_coro_fn=mock_reinvoke,
            timeout_seconds=0.1,
            brain_dir=self.test_dir,
            current_text="",
        )

        self.assertFalse(was_settled)
        self.assertIn("Background task in progress", result_home)

    def test_register_pending_tasks_and_dispatch_completed(self):
        from tools.task_settle import (
            register_pending_tasks,
            check_and_dispatch_completed_tasks,
            get_task_completion_details,
        )

        data_dir = Path(self.test_dir) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # 1. Register pending task
        register_pending_tasks(
            conv_id=self.conv_id,
            channel_id=123456,
            mode="external",
            task_ids=["task-auto-1"],
            data_dir=data_dir,
        )

        pending_file = data_dir / "pending_sdk_tasks.json"
        self.assertTrue(pending_file.exists())
        pdata = json.loads(pending_file.read_text())
        self.assertEqual(pdata[self.conv_id]["task_ids"], ["task-auto-1"])

        # 2. Before completion: dispatch does nothing
        with patch("tools.outbox.queue_outbox_message") as mock_outbox:
            dispatched = check_and_dispatch_completed_tasks(brain_dir=self.test_dir, data_dir=data_dir)
            self.assertEqual(dispatched, [])
            mock_outbox.assert_not_called()

        # 3. Simulate task completing with log and message
        log_file = self.tasks_dir / "task-auto-1.log"
        log_file.write_text("Starting step 1\nStep 1 OK\nBuild complete successfully\n")

        msg_file = self.msgs_dir / "msg-auto-1.json"
        msg_file.write_text(json.dumps({
            "sender": f"{self.conv_id}/task-auto-1",
            "content": f'Task id "{self.conv_id}/task-auto-1" finished with exit code 0',
        }))

        # 4. Dispatch should find it, queue outbox, and remove from pending
        with patch("tools.outbox.queue_outbox_message") as mock_outbox:
            dispatched = check_and_dispatch_completed_tasks(brain_dir=self.test_dir, data_dir=data_dir)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0]["task_id"], "task-auto-1")
            mock_outbox.assert_called_once()
            call_kwargs = mock_outbox.call_args.kwargs
            self.assertEqual(call_kwargs["channel"], 123456)
            self.assertIn("Background Task Complete", call_kwargs["content"])
            self.assertIn("Build complete successfully", call_kwargs["content"])

        # 5. Pending file should now be cleared
        pdata_after = json.loads(pending_file.read_text())
        self.assertNotIn(self.conv_id, pdata_after)


if __name__ == "__main__":
    unittest.main()
