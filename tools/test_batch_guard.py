#!/usr/bin/env python3
"""
Unit test suite for enhanced tool batching guard hook.
Verifies enforcement across shell inspections, repetitive view_file slices,
serial file view chains, and repetitive replace_file_content calls.
"""
import sys
import os
import unittest
from pathlib import Path

WORKSPACE = Path("/workspace")
sys.path.insert(0, str(WORKSPACE / ".agents" / "hooks"))

import batch_guard
from batch_guard import (
    check_batching,
    check_tool_use,
    STATE_FILE,
    is_single_inspection,
    is_batched_or_mutating,
    load_state,
)


class TestBatchGuard(unittest.TestCase):

    def setUp(self):
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    def tearDown(self):
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    def test_catches_serial_shell_inspections(self):
        for i in range(3):
            allowed, reason = check_batching("docker ps")
            self.assertTrue(allowed, f"Command {i + 1} should be allowed")

        allowed, reason = check_batching("docker inspect foo")
        self.assertFalse(allowed, "Expected 4th inspection to be blocked")
        self.assertIn("consecutive single-line inspection commands", reason)

    def test_scratch_script_resets_shell_count(self):
        for i in range(3):
            check_batching("docker ps")

        allowed, reason = check_batching("python3 /workspace/scratch/check.py")
        self.assertTrue(allowed)

        allowed, reason = check_batching("docker ps")
        self.assertTrue(allowed)

    def test_compound_command_allowed(self):
        allowed, reason = check_batching("ssh testuser@remote-host.local 'docker ps; crontab -l' ")
        self.assertTrue(allowed)

    def test_ssh_inspection_counting(self):
        for i in range(3):
            allowed, reason = check_batching('ssh testuser@remote-host.local "docker ps"')
            self.assertTrue(allowed)

        allowed, reason = check_batching('ssh testuser@remote-host.local "crontab -l"')
        self.assertFalse(allowed)

    def test_view_file_same_file_blocks_on_third_call(self):
        allowed, _ = check_tool_use("view_file", {"AbsolutePath": "/workspace/tools/foo.py", "StartLine": 1, "EndLine": 50})
        self.assertTrue(allowed)
        allowed, _ = check_tool_use("view_file", {"AbsolutePath": "/workspace/tools/foo.py", "StartLine": 51, "EndLine": 100})
        self.assertTrue(allowed)
        allowed, reason = check_tool_use("view_file", {"AbsolutePath": "/workspace/tools/foo.py", "StartLine": 101, "EndLine": 150})
        self.assertFalse(allowed)
        self.assertIn("3 consecutive view_file calls on '/workspace/tools/foo.py'", reason)

    def test_view_file_across_different_files_blocks_on_fifth(self):
        files = ["/a.py", "/b.py", "/c.py", "/d.py"]
        for f in files:
            allowed, _ = check_tool_use("view_file", {"AbsolutePath": f})
            self.assertTrue(allowed)
        allowed, reason = check_tool_use("view_file", {"AbsolutePath": "/e.py"})
        self.assertFalse(allowed)
        self.assertIn("5 consecutive serial view_file calls", reason)

    def test_replace_file_content_same_file_blocks_on_fourth(self):
        for _ in range(3):
            allowed, _ = check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
            self.assertTrue(allowed)
        allowed, reason = check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
        self.assertFalse(allowed)
        self.assertIn("4 consecutive replace_file_content calls on '/workspace/tools/bar.py'", reason)

    def test_test_command_resets_file_edits(self):
        for _ in range(3):
            check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
        allowed, _ = check_tool_use("run_command", {"CommandLine": "python3 -m unittest test_bar.py"})
        self.assertTrue(allowed)
        allowed, _ = check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
        self.assertTrue(allowed)

    def test_write_to_file_resets_all_counters(self):
        for _ in range(3):
            check_tool_use("view_file", {"AbsolutePath": f"/{_}.py"})
        allowed, _ = check_tool_use("write_to_file", {"TargetFile": "/workspace/foo.py"})
        self.assertTrue(allowed)
        state = load_state()
        self.assertEqual(state["consecutive_file_views"], 0)
        self.assertEqual(state["cmd_inspections"], 0)
        self.assertEqual(state.get("consecutive_git_cmds", 0), 0)

    def test_unscoped_pytest_blocks(self):
        for cmd in ["pytest", "pytest -v", "pytest tests", "pytest tests/", "pytest ."]:
            allowed, reason = check_tool_use("run_command", {"CommandLine": cmd})
            self.assertFalse(allowed, f"Expected {cmd} to be blocked")
            self.assertIn("Unscoped full test suite execution", reason)

    def test_scoped_pytest_allows(self):
        for cmd in ["pytest tests/test_foo.py", "pytest -k test_bar", "pytest tests/test_bar.py::test_func"]:
            allowed, _ = check_tool_use("run_command", {"CommandLine": cmd})
            self.assertTrue(allowed, f"Expected {cmd} to be allowed")

    def test_serial_git_lifecycle_blocks_on_third(self):
        allowed, _ = check_tool_use("run_command", {"CommandLine": "git add file.py"})
        self.assertTrue(allowed)
        allowed, _ = check_tool_use("run_command", {"CommandLine": "git commit -m 'feat: update'"})
        self.assertTrue(allowed)
        allowed, reason = check_tool_use("run_command", {"CommandLine": "git push origin branch"})
        self.assertFalse(allowed)
        self.assertIn("3 consecutive unbatched git lifecycle commands in serial", reason)

    def test_compound_git_pipeline_allows(self):
        allowed, _ = check_tool_use("run_command", {"CommandLine": "git add file.py && git commit -m 'feat: update' && git push origin branch"})
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
