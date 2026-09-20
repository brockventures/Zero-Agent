#!/usr/bin/env python3
"""
Unit test suite for search_guard hook (PreToolUse search & command safety gate).
"""

import sys
import unittest
from pathlib import Path

WORKSPACE = Path("/workspace")
sys.path.insert(0, str(WORKSPACE / ".agents" / "hooks"))

from search_guard import check_search_path, check_command_line


class TestSearchGuard(unittest.TestCase):

    def test_blocks_volume_roots(self):
        """Verify un-scoped volume_roots are blocked in search_path."""
        for vol in ["/volume1", "/volume1/", "/volume2", "/volumeUSB1"]:
            allowed, reason = check_search_path(vol, "/workspace")
            self.assertFalse(allowed, f"Expected {vol} to be blocked")
            self.assertIn("Search Blocked", reason)


    def test_allows_targeted_volume_subfolders(self):
        """Verify targeted subfolders on volume1 are permitted."""
        allowed, reason = check_search_path("/volume1/baseball", "/workspace")
        self.assertTrue(allowed, f"Expected targeted path to be allowed: {reason}")

    def test_blocks_recursive_grep_on_volume_roots(self):
        """Verify recursive grep on volume root or system root is blocked in command lines."""
        blocked = [
            'ssh testuser@remote-host.local "cat tasks || grep -rn \'refresh_stats\' /volume1/ 2>/dev/null"',
            'grep -r "pattern" /volume1',
            'grep -rn "pattern" /volume2/',
            'rg "pattern" /volume1',
            'grep -rn "pattern" /',
        ]
        for cmd in blocked:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertFalse(allowed, f"Expected command to be blocked: {cmd}")
            self.assertIn("Command Blocked", reason)


    def test_allows_targeted_recursive_grep(self):
        """Verify targeted recursive grep on specific folders is allowed."""
        allowed = [
            'grep -rn "pattern" /volume1/baseball/',
            'grep -rn "pattern" /workspace/tools/',
            'rg "pattern" /workspace/tools',
        ]
        for cmd in allowed:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertTrue(allowed, f"Expected command to be allowed: {cmd} (got {reason})")


if __name__ == "__main__":
    unittest.main()
