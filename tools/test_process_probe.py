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


if __name__ == "__main__":
    unittest.main()
