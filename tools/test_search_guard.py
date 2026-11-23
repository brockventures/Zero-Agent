#!/usr/bin/env python3
"""
Unit test suite for search_guard hook (PreToolUse search & command safety gate).
"""

import posixpath
import sys
import unittest
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE / ".agents" / "hooks"))

from search_guard import check_command_line, check_search_path

VOL1_DOCKER = "/vol" + "ume1/" + "docker"
VOL2_DOCKER = "/vol" + "ume2/" + "docker"


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


    def test_blocks_recursive_grep_in_blocked_prefixes(self):
        """Verify recursive grep targeting /root, /docker, /workspace/data is blocked."""
        blocked = [
            'grep -ri "customization.*budget" /root/.gemini/antigravity-cli/',
            'grep -rn "secret" /docker/some_dir',
            'rg "pattern" /workspace/data/',
        ]
        for cmd in blocked:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertFalse(allowed, f"Expected command to be blocked: {cmd}")
            self.assertIn("Command Blocked", reason)

    def test_blocks_binary_inspection(self):
        """Verify strings, disassembly, or grep on /usr/local/bin or /usr/bin is blocked."""
        blocked = [
            'strings /usr/local/bin/agy | grep -i "budget"',
            'grep -a -o -E "[a-zA-Z0-9_]{0,30}budget" /usr/local/bin/agy',
            'objdump -d /usr/bin/python3',
            'hexdump -C /usr/local/bin/agy',
        ]
        for cmd in blocked:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertFalse(allowed, f"Expected binary command to be blocked: {cmd}")
            self.assertIn("Command Blocked", reason)

    def test_allows_targeted_recursive_grep(self):
        """Verify targeted recursive grep on specific folders is allowed."""
        allowed_cmds = [
            'grep -rn "pattern" /volume1/baseball/',
            'grep -rn "pattern" /workspace/tools/',
            'rg "pattern" /workspace/tools',
        ]
        for cmd in allowed_cmds:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertTrue(allowed, f"Expected command to be allowed: {cmd} (got {reason})")

    def test_blocks_du_on_nas_docker_and_agent_paths(self):
        """Verify du (including du -sh, du -s, du --max-depth=0) on NAS docker/agent paths is blocked."""
        blocked_cmds = [
            f"du -sh {VOL1_DOCKER}",
            f"du -s {VOL2_DOCKER}",
            f"du --max-depth=0 {VOL1_DOCKER}",
            f"du -sh {posixpath.join(VOL1_DOCKER, 'discord-agy-agent')}",
            f"du -s {posixpath.join(VOL1_DOCKER, 'discord-vulcan-agent')}",
            f"du --max-depth=0 {posixpath.join(VOL1_DOCKER, 'discord-ivy-agent')}",
            "du -sh /opt/agents/discord-agy-agent",
            "du -sh discord-vulcan-agent",
            "du -sh $NAS_DOCKER_ROOT",
            'du -sh "${NAS_DOCKER_ROOT}/discord-ivy-agent"',
        ]
        for cmd in blocked_cmds:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertFalse(allowed, f"Expected du command to be blocked: {cmd}")
            self.assertIn("Command Blocked", reason)
            self.assertIn("df -h", reason)
            self.assertIn("inode", reason.lower())

    def test_blocks_du_on_blocked_roots_and_prefixes(self):
        """Verify du on blocked roots and bulk/system tree prefixes is blocked."""
        blocked_cmds = [
            "du -sh /",
            "du -sh /workspace",
            "du -sh /workspace/data",
            "du -sh /workspace/data/takeout",
            "du -sh /root",
            "du -sh /root/.gemini",
            "du -sh /docker",
            "du -sh /archives",
            "du -sh /volume1",
            "du -sh /volume2",
            "du -sh /volumeUSB1",
            "du -sh",
            "du -sh .",
            "du -sh *",
            "sudo du -sh /root/.gemini",
            "timeout 30 du -sh /workspace/data",
        ]
        for cmd in blocked_cmds:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertFalse(allowed, f"Expected du command to be blocked: {cmd}")
            self.assertIn("Command Blocked", reason)
            self.assertIn("df -h", reason)

    def test_blocks_du_via_cd_subshell_and_ssh(self):
        """Verify du wrapped in cd && du, $(du ...), backticks, and ssh is blocked."""
        blocked_cmds = [
            f"cd {VOL1_DOCKER} && du -sh *",
            f"cd {VOL1_DOCKER}; du -s .",
            "cd /workspace/data && du -sh *",
            f"echo $(du -sh {VOL1_DOCKER})",
            f"SIZE=$(du -sh {posixpath.join(VOL1_DOCKER, 'discord-agy-agent')})",
            f"echo `du -sh {VOL1_DOCKER}`",
            f'ssh testuser@198.51.100.10 "du -sh {VOL1_DOCKER}"',
            f"ssh -p 2222 testuser@198.51.100.10 'cd {VOL1_DOCKER} && du -sh *'",
            f"ssh -i /secrets/key testuser@198.51.100.10 du -sh {posixpath.join(VOL1_DOCKER, 'discord-ivy-agent')}",
            'ssh testuser@198.51.100.10 "du -sh /volume1"',
        ]
        for cmd in blocked_cmds:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertFalse(allowed, f"Expected wrapped du command to be blocked: {cmd}")
            self.assertIn("Command Blocked", reason)
            self.assertIn("df -h", reason)

    def test_allows_df_and_scoped_workspace_du(self):
        """Verify df -h and du on small explicitly scoped workspace subdirectories are allowed."""
        allowed_cmds = [
            "df -h",
            'ssh testuser@198.51.100.10 "df -h"',
            "du -sh /workspace/tools",
            "du -h --max-depth=1 /workspace/tools",
            "du -d 1 /workspace/config",
            "du -sh /workspace/tools/nas_config.py",
            "cd /workspace/tools && du -sh .",
            "pytest tools/test_search_guard.py -k du",
        ]
        for cmd in allowed_cmds:
            allowed, reason = check_command_line(cmd, "/workspace")
            self.assertTrue(allowed, f"Expected command to be allowed: {cmd} (got {reason})")


if __name__ == "__main__":
    unittest.main()
