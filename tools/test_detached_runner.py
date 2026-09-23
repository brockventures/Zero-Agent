#!/usr/bin/env python3
"""
test_detached_runner.py - Unit tests for detached_runner failure-only notifications and task lifecycle.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tools.detached_runner import (
    start_task,
    get_task_status,
    cancel_task,
    _run_worker,
    _write_meta,
    _read_meta,
    _format_duration,
)


class TestDetachedRunner(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.orig_tasks_dir = patch("tools.detached_runner.TASKS_DIR", Path(self.test_dir.name))
        self.orig_tasks_dir.start()

    def tearDown(self):
        self.orig_tasks_dir.stop()
        self.test_dir.cleanup()

    @patch("tools.outbox.queue_outbox_message")
    def test_success_suppresses_notification_by_default(self, mock_queue):
        task_id = "dt-test-success-silent"
        t_dir = Path(self.test_dir.name) / task_id
        t_dir.mkdir(parents=True)
        log_file = t_dir / "run.log"
        log_file.write_text("All tests passed cleanly.\n")

        meta = {
            "task_id": task_id,
            "name": "Unit Test Suite",
            "command": "pytest",
            "channel": "zero-chat",
            "timeout": 300,
            "cwd": "/workspace",
            "status": "starting",
            "notify_on_success": False,
            "log_file": str(log_file),
            "start_time": 1000.0,
        }
        _write_meta(task_id, meta)

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.pid = 99999

        with patch("subprocess.Popen", return_value=mock_proc):
            _run_worker(task_id)

        # Verification: Outbox notification must NOT be queued on success
        mock_queue.assert_not_called()

        # Meta must still reflect completed state
        updated = _read_meta(task_id)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["exit_code"], 0)

        # Log must record suppression
        log_content = log_file.read_text()
        self.assertIn("Suppressing success notification", log_content)

    @patch("tools.outbox.queue_outbox_message")
    def test_success_sends_notification_when_explicitly_requested(self, mock_queue):
        task_id = "dt-test-success-notify"
        t_dir = Path(self.test_dir.name) / task_id
        t_dir.mkdir(parents=True)
        log_file = t_dir / "run.log"
        log_file.write_text("Build succeeded.\nArtifact generated.\n")

        meta = {
            "task_id": task_id,
            "name": "Docker Image Build",
            "command": "docker build",
            "channel": "zero-chat",
            "timeout": 300,
            "cwd": "/workspace",
            "status": "starting",
            "notify_on_success": True,
            "log_file": str(log_file),
            "start_time": 1000.0,
        }
        _write_meta(task_id, meta)

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.pid = 99999

        with patch("subprocess.Popen", return_value=mock_proc):
            _run_worker(task_id)

        # Verification: Outbox notification SHOULD be sent
        mock_queue.assert_called_once()
        args, kwargs = mock_queue.call_args
        content = kwargs.get("content") or args[1]
        self.assertIn("✅ **Background Task Complete: Docker Image Build**", content)
        self.assertIn("• *Inspect full logs:* `python3 /workspace/tools/detached_runner.py logs dt-test-success-notify`", content)
        # Verify internal paths and task ID lines were stripped
        self.assertNotIn("• **Task ID:**", content)
        self.assertNotIn("• **Log File:**", content)

    @patch("tools.outbox.queue_outbox_message")
    def test_failure_always_sends_alert(self, mock_queue):
        task_id = "dt-test-failure-alert"
        t_dir = Path(self.test_dir.name) / task_id
        t_dir.mkdir(parents=True)
        log_file = t_dir / "run.log"
        log_file.write_text("--- [Detached Runner] Started at ... ---\nError: Connection to database failed.\nStack trace line 42\n")

        meta = {
            "task_id": task_id,
            "name": "DB Migration",
            "command": "python migrate.py",
            "channel": "zero-chat",
            "timeout": 300,
            "cwd": "/workspace",
            "status": "starting",
            "notify_on_success": False,
            "log_file": str(log_file),
            "start_time": 1000.0,
        }
        _write_meta(task_id, meta)

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 1
        mock_proc.pid = 99999

        with patch("subprocess.Popen", return_value=mock_proc):
            _run_worker(task_id)

        # Verification: Outbox notification MUST be sent on failure
        mock_queue.assert_called_once()
        args, kwargs = mock_queue.call_args
        content = kwargs.get("content") or args[1]
        self.assertIn("❌ **Background Task Failed: DB Migration**", content)
        self.assertIn("Exit Code: 1", content)
        self.assertIn("Connection to database failed", content)
        # Runner banner line must be filtered out
        self.assertNotIn("--- [Detached Runner] Started at", content)
        self.assertIn("• *Inspect full logs:* `python3 /workspace/tools/detached_runner.py logs dt-test-failure-alert`", content)

    @patch("tools.outbox.queue_outbox_message")
    def test_timeout_always_sends_alert(self, mock_queue):
        import subprocess as sp
        task_id = "dt-test-timeout-alert"
        t_dir = Path(self.test_dir.name) / task_id
        t_dir.mkdir(parents=True)
        log_file = t_dir / "run.log"
        log_file.write_text("Starting infinite loop...\n")

        meta = {
            "task_id": task_id,
            "name": "Infinite Test",
            "command": "sleep 9999",
            "channel": "zero-chat",
            "timeout": 5,
            "cwd": "/workspace",
            "status": "starting",
            "notify_on_success": False,
            "log_file": str(log_file),
            "start_time": 1000.0,
        }
        _write_meta(task_id, meta)

        mock_proc = MagicMock()
        mock_proc.wait.side_effect = sp.TimeoutExpired(cmd="sleep 9999", timeout=5)
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc), patch("os.killpg"):
            _run_worker(task_id)

        # Verification: Timeout notification MUST be sent
        mock_queue.assert_called_once()
        args, kwargs = mock_queue.call_args
        content = kwargs.get("content") or args[1]
        self.assertIn("⏱️ **Background Task Timed Out: Infinite Test** (Limit: 5s)", content)


if __name__ == "__main__":
    unittest.main()
