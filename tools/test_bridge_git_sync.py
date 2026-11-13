#!/usr/bin/env python3
"""
test_bridge_git_sync.py — Unit Tests for Automated Pre-Reload Git Synchronization.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
if _REPO_ROOT in sys.path:
    sys.path.remove(_REPO_ROOT)
sys.path.insert(0, _REPO_ROOT)
from tools.bridge_git_sync import get_current_git_sha, sync_git_on_reload


class TestBridgeGitSync(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        (self.temp_path / ".git").mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_current_git_sha(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abcdef1\n")
            sha = get_current_git_sha(self.temp_path)
            self.assertEqual(sha, "abcdef1")

    def test_sync_git_on_reload_clean_working_tree(self):
        with patch("subprocess.run") as mock_run, \
             patch("tools.bridge_git_sync.get_current_git_sha", return_value="1234567"):
            # git status --porcelain returns empty
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            res = sync_git_on_reload(repo_dir=self.temp_path)
            self.assertTrue(res["clean"])
            self.assertFalse(res["synced"])
            self.assertEqual(res["sha"], "1234567")
            self.assertIn("Working tree clean", res["message"])

    def test_sync_git_on_reload_with_changes_commits_and_pushes(self):
        fake_sync_file = self.temp_path / "last_sync.json"
        with patch("subprocess.run") as mock_run, \
             patch("tools.bridge_git_sync.LAST_SYNC_FILE", fake_sync_file), \
             patch("tools.bridge_git_sync.get_current_git_sha", side_effect=["old_sha", "new_sha"]):

            def side_effect(cmd, *args, **kwargs):
                if "status" in cmd:
                    return MagicMock(returncode=0, stdout=" M tools/bridge.py\n?? tools/new_tool.py\n?? scratch/junk.tmp\n")
                elif "diff" in cmd:
                    return MagicMock(returncode=0, stdout="tools/bridge.py\ntools/new_tool.py\n")
                elif "commit" in cmd or "push" in cmd or "add" in cmd or "pull" in cmd:
                    return MagicMock(returncode=0, stdout="")
                return MagicMock(returncode=0, stdout="")

            mock_run.side_effect = side_effect

            res = sync_git_on_reload(
                repo_dir=self.temp_path,
                reason="Deploying new feature",
                initiator="Ryan",
            )

            self.assertTrue(res["synced"])
            self.assertFalse(res["clean"])
            self.assertEqual(res["sha"], "new_sha")
            self.assertEqual(len(res["files"]), 2)
            self.assertIn("tools/bridge.py", res["files"])
            self.assertIn("tools/new_tool.py", res["files"])

            # Verify sync JSON was written
            self.assertTrue(fake_sync_file.exists())
            data = json.loads(fake_sync_file.read_text())
            self.assertEqual(data["sha"], "new_sha")
            self.assertEqual(data["reason"], "Deploying new feature")
            self.assertEqual(data["initiator"], "Ryan")

    def test_sync_git_on_reload_handles_error_gracefully(self):
        with patch("subprocess.run", side_effect=RuntimeError("Network timeout")):
            res = sync_git_on_reload(repo_dir=self.temp_path)
            self.assertFalse(res["synced"])
            self.assertIsNotNone(res["error"])
            self.assertIn("Network timeout", res["message"])

    def test_sync_git_on_reload_batches_untracked_and_enforces_timeouts(self):
        import subprocess

        recorded_calls = []

        def fake_run(cmd, *args, **kwargs):
            recorded_calls.append((cmd, kwargs.get("timeout"), kwargs.get("env", {})))
            if "status" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="?? tools/a.py\n?? tools/b.py\n?? scratch/ignore.tmp\n",
                )
            if "diff" in cmd:
                return MagicMock(returncode=0, stdout="tools/a.py\ntools/b.py\n")
            return MagicMock(returncode=0, stdout="abc9999\n")

        fake_sync_file = self.temp_path / "last_sync.json"
        with patch("subprocess.run", side_effect=fake_run), \
             patch("tools.bridge_git_sync.LAST_SYNC_FILE", fake_sync_file):
            res = sync_git_on_reload(repo_dir=self.temp_path, timeout=None)

        self.assertTrue(res["synced"])
        self.assertTrue(all(t is not None and t > 0 for _, t, _ in recorded_calls))
        self.assertTrue(all(env.get("GIT_TERMINAL_PROMPT") == "0" for _, _, env in recorded_calls))
        untracked_adds = [cmd for cmd, _, _ in recorded_calls if cmd[:3] == ["git", "add", "--"]]
        self.assertEqual(untracked_adds, [["git", "add", "--", "tools/a.py", "tools/b.py"]])


if __name__ == "__main__":
    unittest.main()
