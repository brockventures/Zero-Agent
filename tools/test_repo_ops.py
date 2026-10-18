#!/usr/bin/env python3
"""
Unit tests for tools/repo_ops.py.
"""

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from tools.repo_ops import prune_merged_branches, cleanup_worktrees, clean_repo


class TestRepoOps(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = TemporaryDirectory()
        self.repo_dir = Path(self.tmp_dir.name)
        # Initialize a git repo with a commit
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo_dir, capture_output=True, check=True)
        
        test_file = self.repo_dir / "README.md"
        test_file.write_text("initial")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.repo_dir, capture_output=True, check=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_prune_merged_branches(self):
        # Create a branch and merge it
        subprocess.run(["git", "checkout", "-b", "feature-test"], cwd=self.repo_dir, capture_output=True, check=True)
        feat_file = self.repo_dir / "feature.txt"
        feat_file.write_text("feature content")
        subprocess.run(["git", "add", "."], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "feature commit"], cwd=self.repo_dir, capture_output=True, check=True)

        subprocess.run(["git", "checkout", "main"], cwd=self.repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "merge", "feature-test"], cwd=self.repo_dir, capture_output=True, check=True)

        # Run prune
        deleted = prune_merged_branches(self.repo_dir, target_base="main")
        self.assertIn("feature-test", deleted)

        # Verify branch is gone
        branches = subprocess.run(["git", "branch"], cwd=self.repo_dir, capture_output=True, text=True).stdout
        self.assertNotIn("feature-test", branches)

    def test_cleanup_worktrees(self):
        wt_dir = self.repo_dir.parent / "wt-test"
        subprocess.run(["git", "worktree", "add", "-b", "wt-branch", str(wt_dir)], cwd=self.repo_dir, capture_output=True, check=True)

        # Verify worktree exists
        wts_before = subprocess.run(["git", "worktree", "list"], cwd=self.repo_dir, capture_output=True, text=True).stdout
        self.assertIn(str(wt_dir), wts_before)

        # Run cleanup
        cleaned = cleanup_worktrees(self.repo_dir)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(Path(cleaned[0]), wt_dir.resolve())

        # Verify worktree is gone
        wts_after = subprocess.run(["git", "worktree", "list"], cwd=self.repo_dir, capture_output=True, text=True).stdout
        self.assertNotIn(str(wt_dir), wts_after)


if __name__ == "__main__":
    unittest.main()
