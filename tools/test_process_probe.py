#!/usr/bin/env python3
"""Unit tests for tools.process_probe."""

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.process_probe import (
    get_process_children,
    inspect_single_process,
    scan_tail_for_prompts,
    diagnose_process_tree,
    INTERACTIVE_PROMPT_PATTERNS,
)


class TestProcessProbe(unittest.TestCase):

    def test_scan_tail_for_prompts(self):
        # Prompt detections
        self.assertIsNotNone(scan_tail_for_prompts("Need to install packages\nOk to proceed? (y)\n"))
        self.assertIsNotNone(scan_tail_for_prompts("2. On the linking page copy link.\nPaste the link: "))
        self.assertIsNotNone(scan_tail_for_prompts("Enter passphrase for key: "))
        self.assertIsNotNone(scan_tail_for_prompts("Do you want to continue? [Y/n] "))
        
        # Non-prompt outputs
        self.assertIsNone(scan_tail_for_prompts("Compilation finished successfully.\nExited 0."))
        self.assertIsNone(scan_tail_for_prompts("Running tests in parallel...\nAll 5 passed."))
        self.assertIsNone(scan_tail_for_prompts(""))

    def test_inspect_current_process(self):
        my_pid = os.getpid()
        info = inspect_single_process(my_pid)
        self.assertTrue(info["exists"])
        self.assertEqual(info["pid"], my_pid)
        self.assertTrue(len(info["name"]) > 0)

    def test_inspect_nonexistent_process(self):
        info = inspect_single_process(9999999)
        self.assertFalse(info["exists"])
        self.assertEqual(info["pid"], 9999999)

    def test_detect_stdin_wedged_subprocess(self):
        # Spawn a python child that blocks on stdin
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('READY', flush=True); sys.stdin.readline()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait for child to reach readline
            line = proc.stdout.readline()
            self.assertIn("READY", line)
            time.sleep(0.2)

            diag = diagnose_process_tree(proc.pid, output_buffer="Paste the link: ")
            self.assertTrue(diag["exists"])
            self.assertTrue(diag["is_wedged"])
            self.assertTrue(diag["is_interactive_stdin"])
            self.assertIn("wedged", diag["summary"].lower())
        finally:
            if proc.stdin: proc.stdin.close()
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
            proc.kill()
            proc.wait()

    def test_detect_normal_running_process(self):
        # Spawn a process sleeping
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            diag = diagnose_process_tree(proc.pid)
            self.assertTrue(diag["exists"])
            # While sleeping in time.sleep, it's not waiting on STDIN
            self.assertFalse(diag["is_interactive_stdin"])
        finally:
            if proc.stdin: proc.stdin.close()
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
            proc.kill()
            proc.wait()

    def test_cpu_ticks_and_progress_detection(self):
        from tools.process_probe import get_process_cpu_ticks, is_process_making_progress, reap_stale_agy_processes
        my_pid = os.getpid()
        ticks = get_process_cpu_ticks(my_pid)
        self.assertIsNotNone(ticks)
        self.assertGreaterEqual(ticks, 0)

        # Process executing CPU work should be detected as making progress
        progress = is_process_making_progress(my_pid, sample_window=0.01)
        # Should return a boolean without error
        self.assertIsInstance(progress, bool)

        # Dry-run reap with allowed pid should not reap current or active processes
        reaped = reap_stale_agy_processes(allowed_active_pids={my_pid}, dry_run=True)
        self.assertEqual(len(reaped), 0)

    def test_pipe_read_child_with_tty_stdin_not_flagged_as_stdin_wedged(self):
        """Verify that child processes reading internal IPC pipes under a PTY are NOT flagged as stdin wedged."""
        import pty
        master, slave = pty.openpty()
        try:
            parent = subprocess.Popen(
                [sys.executable, "-c", """
import subprocess, time
p = subprocess.Popen(['python3', '-c', 'import os; r, w = os.pipe(); os.read(r, 100)'])
time.sleep(5)
"""],
                stdin=slave,
                stdout=slave,
                stderr=slave
            )
            time.sleep(0.3)
            diag = diagnose_process_tree(parent.pid)
            self.assertTrue(diag["exists"])
            # Child is reading an internal pipe without interactive prompts; must NOT be flagged as interactive STDIN!
            self.assertFalse(diag["is_interactive_stdin"], f"False positive interactive stdin on internal pipe: {diag}")
        finally:
            parent.kill()
            parent.wait()
            os.close(slave)
            os.close(master)

    def test_reaper_does_not_reap_young_child_on_old_turn(self):
        """Verify that reap_stale_agy_processes does not reap a turn when the culprit child is younger than 60s."""
        from unittest.mock import patch
        from tools.process_probe import reap_stale_agy_processes

        fake_diag = {
            "root_pid": 12345,
            "exists": True,
            "is_wedged": True,
            "is_interactive_stdin": True,
            "summary": "Subprocess 'git' (PID 12346) is wedged",
            "prompt": "Password: ",
            "culprit": {"pid": 12346, "name": "git", "is_waiting_stdin": True},
            "tree": []
        }

        # Root agy is 75s old, but child git is only 5s old!
        def fake_age(pid):
            if pid == 12345:
                return 75.0
            if pid == 12346:
                return 5.0
            return 10.0

        with patch("tools.process_probe.Path.iterdir") as mock_iterdir, \
             patch("tools.process_probe.get_process_age", side_effect=fake_age), \
             patch("tools.process_probe.diagnose_process_tree", return_value=fake_diag), \
             patch("tools.process_probe.is_process_making_progress", return_value=False):

            class FakeProcPath:
                def __init__(self, pid_str):
                    self.name = pid_str
                def is_dir(self): return True
                def __truediv__(self, sub):
                    class FakeFile:
                        def exists(self): return True
                        def read_bytes(self): return b"agy --conversation=123"
                    return FakeFile()

            mock_iterdir.return_value = [FakeProcPath("12345")]
            reaped = reap_stale_agy_processes(allowed_active_pids={12345}, dry_run=True)
            # The child process was only 5s old, so it must NOT be reaped!
            self.assertEqual(len(reaped), 0, f"Young child should not cause parent reap: {reaped}")


if __name__ == "__main__":
    unittest.main()
