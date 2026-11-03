#!/usr/bin/env python3
"""
test_detached_runner.py - Unit tests for detached_runner failure-only notifications and task lifecycle.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
elif sys.path[0] != _REPO_ROOT:
    sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)

from tools.detached_runner import (
    _run_worker,
    _write_meta,
    _read_meta,
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

        # Verification: Outbox notification SHOULD be sent as agent synthesis turn
        mock_queue.assert_called_once()
        args, kwargs = mock_queue.call_args
        content = kwargs.get("content") or args[1]
        msg_type = kwargs.get("msg_type") or "text"
        self.assertIn("[SYSTEM NOTIFICATION: DETACHED TASK COMPLETE]", content)
        self.assertIn("Task Name: Docker Image Build", content)
        self.assertEqual(msg_type, "agent_turn")

    @patch("tools.outbox.queue_outbox_message")
    def test_success_raw_notification_when_no_synthesize(self, mock_queue):
        task_id = "dt-test-success-raw"
        t_dir = Path(self.test_dir.name) / task_id
        t_dir.mkdir(parents=True)
        log_file = t_dir / "run.log"
        log_file.write_text("--- [Detached Runner] Started at ... ---\nBuild finished with status 0\n")

        meta = {
            "task_id": task_id,
            "name": "Docker Image Build",
            "command": "docker build -t test .",
            "channel": "zero-chat",
            "timeout": 300,
            "cwd": "/workspace",
            "status": "starting",
            "notify_on_success": True,
            "synthesize": False,
            "log_file": str(log_file),
            "start_time": 1000.0,
        }
        _write_meta(task_id, meta)

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.pid = 99998

        with patch("subprocess.Popen", return_value=mock_proc):
            _run_worker(task_id)

        # Verification: Raw outbox notification SHOULD be sent
        mock_queue.assert_called_once()
        args, kwargs = mock_queue.call_args
        content = kwargs.get("content") or args[1]
        self.assertIn("✅ **Background Task Complete: Docker Image Build**", content)
        self.assertIn("• *Inspect full logs:* `python3 /workspace/tools/detached_runner.py logs dt-test-success-raw`", content)

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

        # Verification: Outbox notification MUST be sent on failure (as agent synthesis turn by default)
        mock_queue.assert_called_once()
        args, kwargs = mock_queue.call_args
        content = kwargs.get("content") or args[1]
        msg_type = kwargs.get("msg_type") or "text"
        self.assertIn("[SYSTEM NOTIFICATION: DETACHED TASK FAILED]", content)
        self.assertIn("Task Name: DB Migration", content)
        self.assertIn("Exit Code: 1", content)
        self.assertEqual(msg_type, "agent_turn")

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

        # Verification: Timeout notification MUST be sent (as agent synthesis turn by default)
        mock_queue.assert_called_once()
        args, kwargs = mock_queue.call_args
        content = kwargs.get("content") or args[1]
        msg_type = kwargs.get("msg_type") or "text"
        self.assertIn("[SYSTEM NOTIFICATION: DETACHED TASK TIMED OUT]", content)
        self.assertIn("Task Name: Infinite Test", content)
        self.assertIn("Exit Code: -99", content)
        self.assertEqual(msg_type, "agent_turn")


if __name__ == "__main__":
    unittest.main()
