#!/usr/bin/env python3
"""
Unit test suite for tool batching guard hook.
"""
import sys
import os
import unittest
from pathlib import Path

WORKSPACE = Path("/workspace")
sys.path.insert(0, str(WORKSPACE / ".agents" / "hooks"))

import batch_guard
from batch_guard import check_batching, STATE_FILE, is_single_inspection, is_batched_or_mutating


class TestBatchGuard(unittest.TestCase):

    def setUp(self):
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    def tearDown(self):
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    def test_catches_serial_inspections(self):
        # Commands 1, 2, 3 ought to pass
        for i in range(3):
            allowed, reason = check_batching("docker ps")
            self.assertTrue(allowed, f"Command {i+ 1} should be allowed")

        # 4th consecutive inspection should be denied
        allowed, reason = check_batching("docker inspect foo")
        self.assertFalse(allowed, "Expected 4th inspection to be blocked")
        self.assertIn("Invariant Triggered", reason)

    def test_scpatch_script_resets(self):
        # 3 inspections
        for i in range(3):
            check_batching("docker ps")

        # Scratch script execution
        allowed, reason = check_batching("python3 /workspace/scratch/check.py")
        self.assertTrue(allowed)

        # New inspection after reset should succeed (count reset to 1)
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

if __name__ == "__main__":
    unittest.main()
