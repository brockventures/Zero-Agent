#!/usr/bin/env python3
"""test_gh_pr_watcher.py - Comprehensive Unit Tests for GitHub PR Watcher Sidecar."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, call, patch
from typing import Any

# Ensure local repo path takes precedence over global /workspace
_local_repo = str(Path(__file__).resolve().parent.parent)
if _local_repo in sys.path:
    sys.path.remove(_local_repo)
sys.path.insert(0, _local_repo)
for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.append(_p)

from tools.gh_pr_watcher import (
    FORGE_CHANNEL_ID,
    WHITELISTED_AUTHORS,
    ZERO_OPS_CHANNEL_ID,
    check_pr,
    discover_open_prs,
    dispatch_notification,
    ensure_schedule_registered,
    format_comment_message,
    format_review_message,
    format_review_request_message,
    get_authenticated_user,
    get_pr_details,
    is_ci_passing_or_empty,
    load_state,
    poll_and_notify,
    save_state,
    truncate_body,
    try_auto_merge_pr,
)


class TestGhPrWatcherState(unittest.TestCase):
    """Test state persistence and schema initialization."""

    def test_load_state_default_when_missing(self) -> None:
        missing_path = Path("/tmp/nonexistent_gh_watcher_state.json")
        if missing_path.exists():
            missing_path.unlink()
        state = load_state(missing_path)
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["seen_review_ids"], [])
        self.assertEqual(state["seen_comment_ids"], [])
        self.assertEqual(state["seen_review_requests"], [])
        self.assertEqual(state["review_decisions"], {})

    def test_load_state_corrupt_json(self) -> None:
        corrupt_path = Path("/tmp/corrupt_gh_watcher_state.json")
        corrupt_path.write_text("{broken json", encoding="utf-8")
        try:
            state = load_state(corrupt_path)
            self.assertEqual(state["version"], 1)
            self.assertEqual(state["seen_review_ids"], [])
        finally:
            corrupt_path.unlink(missing_ok=True)

    def test_save_state_creates_directories_and_writes_atomically(self) -> None:
        target_path = Path("/tmp/test_dir_state/nested/state.json")
        try:
            sample_state = {
                "version": 1,
                "seen_review_ids": ["rev-2", "rev-1", "rev-2"],  # duplicate check
                "seen_comment_ids": ["c1"],
                "seen_review_requests": ["req1"],
                "review_decisions": {"repo#1": "APPROVED"},
            }
            save_state(sample_state, target_path)
            self.assertTrue(target_path.exists())

            loaded = load_state(target_path)
            self.assertEqual(loaded["seen_review_ids"], ["rev-1", "rev-2"])
            self.assertEqual(loaded["review_decisions"]["repo#1"], "APPROVED")
            self.assertIsNotNone(loaded["last_run"])
        finally:
            if target_path.exists():
                target_path.unlink()
            if target_path.parent.exists():
                target_path.parent.rmdir()
            if target_path.parent.parent.exists():
                target_path.parent.parent.rmdir()


class TestGhPrWatcherDiscovery(unittest.TestCase):
    """Test discovery of open PRs across author, review-requested, and repos."""

    @patch("subprocess.run")
    def test_get_authenticated_user_cli(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh", "api", "/user"],
            returncode=0,
            stdout="custom_user\n",
            stderr="",
        )
        user = get_authenticated_user()
        self.assertEqual(user, "custom_user")

    @patch("subprocess.run")
    def test_get_authenticated_user_fallback(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = RuntimeError("gh not available")
        with patch.dict("os.environ", {"GH_USER": "env_user"}):
            user = get_authenticated_user()
            self.assertEqual(user, "env_user")

    @patch("tools.gh_pr_watcher.run_gh")
    def test_discover_open_prs(self, mock_run_gh: MagicMock) -> None:
        # Define mock responses based on CLI arguments
        def side_effect(args: list[str], timeout: int = 30):
            if "search" in args and "--author=@me" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=json.dumps([
                        {
                            "repository": {"nameWithOwner": "mcarmody/lomin"},
                            "number": 5,
                            "title": "PR 5",
                            "url": "https://github.com/mcarmody/lomin/pull/5",
                            "author": {"login": "brockventures"},
                        }
                    ]),
                    stderr="",
                )
            if "search" in args and "--review-requested=@me" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=json.dumps([
                        {
                            "repository": {"nameWithOwner": "brockventures/gotchi"},
                            "number": 109,
                            "title": "PR 109",
                            "url": "https://github.com/brockventures/gotchi/pull/109",
                            "author": {"login": "alice"},
                        }
                    ]),
                    stderr="",
                )
            if "pr" in args and "list" in args and "--repo" in args:
                # Target repo mcarmody/lomin (duplicate PR 5 should be deduped)
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=json.dumps([
                        {
                            "number": 5,
                            "title": "PR 5",
                            "url": "https://github.com/mcarmody/lomin/pull/5",
                            "author": {"login": "brockventures"},
                            "reviewDecision": "CHANGES_REQUESTED",
                        },
                        {
                            "number": 6,
                            "title": "PR 6",
                            "url": "https://github.com/mcarmody/lomin/pull/6",
                            "author": {"login": "brockventures"},
                            "reviewDecision": "CHANGES_REQUESTED",
                        },
                    ]),
                    stderr="",
                )
            if "search" in args and "--owner=arcane103" in args:
                # Non-existent or empty repo search
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr="Invalid search query",
                )
            if "api" in args and "/notifications" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout=json.dumps([
                        {
                            "subject": {
                                "type": "PullRequest",
                                "title": "PR 7",
                                "url": "https://api.github.com/repos/mcarmody/lomin/pulls/7",
                            }
                        }
                    ]),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="[]", stderr=""
            )

        mock_run_gh.side_effect = side_effect

        prs = discover_open_prs(
            target_patterns=["mcarmody/lomin", "arcane103/*"],
            current_user="brockventures",
        )
        pr_numbers = {p["number"] for p in prs}
        self.assertIn(5, pr_numbers)
        self.assertIn(6, pr_numbers)
        self.assertIn(7, pr_numbers)
        self.assertIn(109, pr_numbers)
        # Check deduplication on PR 5
        pr5_list = [p for p in prs if p["number"] == 5 and p["repo"] == "mcarmody/lomin"]
        self.assertEqual(len(pr5_list), 1)

    @patch("tools.gh_pr_watcher.run_gh")
    def test_get_pr_details(self, mock_run_gh: MagicMock) -> None:
        mock_run_gh.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps({
                "number": 5,
                "title": "feat(ingest): camera frames",
                "url": "https://github.com/mcarmody/lomin/pull/5",
                "author": {"login": "brockventures"},
                "reviewDecision": "CHANGES_REQUESTED",
                "reviews": [],
                "latestReviews": [],
                "reviewRequests": [],
                "comments": [],
            }),
            stderr="",
        )
        details = get_pr_details("mcarmody/lomin", 5)
        self.assertIsNotNone(details)
        assert details is not None
        self.assertEqual(details["repo"], "mcarmody/lomin")
        self.assertEqual(details["number"], 5)
        self.assertEqual(details["reviewDecision"], "CHANGES_REQUESTED")


class TestGhPrWatcherReviewsAndOutbox(unittest.TestCase):
    """Test review evaluation, deduplication, and outbox dispatch."""

    def test_truncate_body(self) -> None:
        self.assertEqual(truncate_body(""), "*(No review body provided)*")
        self.assertEqual(truncate_body("Short summary"), "Short summary")
        long_text = "A" * 1500
        truncated = truncate_body(long_text, max_length=100)
        self.assertTrue(truncated.endswith("... *(truncated)*"))
        self.assertLessEqual(len(truncated), 125)

    def test_format_review_message(self) -> None:
        review = {
            "state": "CHANGES_REQUESTED",
            "author": {"login": "mcarmody"},
            "submittedAt": "2026-11-02T04:10:04Z",
            "body": "**Round 3: changes needed.**\n- raw image_0..4 not magic-checked",
        }
        msg = format_review_message(
            repo="mcarmody/lomin",
            pr_number=5,
            title="feat: vision adpcm",
            url="https://github.com/mcarmody/lomin/pull/5",
            review=review,
            current_decision="CHANGES_REQUESTED",
        )
        self.assertIn("⚠️ **GitHub PR Review: CHANGES_REQUESTED**", msg)
        self.assertIn("`mcarmody/lomin`", msg)
        self.assertIn("@mcarmody", msg)
        self.assertIn("raw image_0..4 not magic-checked", msg)

    def test_format_review_request_message(self) -> None:
        msg = format_review_request_message(
            repo="mcarmody/lomin",
            pr_number=5,
            title="feat: vision adpcm",
            url="https://github.com/mcarmody/lomin/pull/5",
            reviewer="alice",
            author="brockventures",
        )
        self.assertIn("👀 **GitHub Review Requested**", msg)
        self.assertIn("`mcarmody/lomin`", msg)
        self.assertIn("@alice", msg)
        self.assertIn("@brockventures", msg)

    def test_format_comment_message(self) -> None:
        comment = {
            "author": {"login": "mcarmody"},
            "body": "Please rebase onto main.",
        }
        msg = format_comment_message(
            repo="mcarmody/lomin",
            pr_number=5,
            title="feat: vision adpcm",
            url="https://github.com/mcarmody/lomin/pull/5",
            comment=comment,
        )
        self.assertIn("💬 **GitHub PR Comment**", msg)
        self.assertIn("`mcarmody/lomin`", msg)
        self.assertIn("@mcarmody", msg)
        self.assertIn("Please rebase onto main.", msg)

    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_dispatch_notification(self, mock_queue: MagicMock) -> None:
        mock_queue.return_value = {"status": "queued"}
        results = dispatch_notification(
            "Test notification", channels=[FORGE_CHANNEL_ID, ZERO_OPS_CHANNEL_ID]
        )
        self.assertEqual(len(results), 2)
        mock_queue.assert_has_calls([
            call(
                channel=FORGE_CHANNEL_ID,
                content="Test notification",
                source_turn="gh_pr_watcher",
            ),
            call(
                channel=ZERO_OPS_CHANNEL_ID,
                content="Test notification",
                source_turn="gh_pr_watcher",
            ),
        ])

    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_check_pr_new_review_dispatches_outbox_and_dedupes(
        self, mock_queue: MagicMock
    ) -> None:
        mock_queue.return_value = {"status": "queued"}
        state: dict[str, Any] = {
            "version": 1,
            "seen_review_ids": [],
            "seen_comment_ids": [],
            "seen_review_requests": [],
            "review_decisions": {},
        }

        pr_details = {
            "repo": "mcarmody/lomin",
            "number": 5,
            "title": "feat(ingest): vision",
            "url": "https://github.com/mcarmody/lomin/pull/5",
            "author": {"login": "brockventures"},
            "reviewDecision": "CHANGES_REQUESTED",
            "reviews": [
                {
                    "id": "PRR_12345",
                    "author": {"login": "mcarmody"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-11-02T04:10:04Z",
                    "body": "Needs magic-checking on image_0..4",
                }
            ],
            "reviewRequests": [],
            "comments": [],
        }

        # First run: should notify both channels
        stats1 = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
            channels=[FORGE_CHANNEL_ID, ZERO_OPS_CHANNEL_ID],
        )
        self.assertEqual(stats1["reviews_notified"], 1)
        self.assertEqual(mock_queue.call_count, 2)
        self.assertIn("PRR_12345", state["seen_review_ids"])
        self.assertEqual(state["review_decisions"]["mcarmody/lomin#5"], "CHANGES_REQUESTED")

        # Second run: should be deduped
        mock_queue.reset_mock()
        stats2 = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
            channels=[FORGE_CHANNEL_ID, ZERO_OPS_CHANNEL_ID],
        )
        self.assertEqual(stats2["reviews_notified"], 0)
        mock_queue.assert_not_called()

    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_check_pr_ignores_self_authored_reviews(
        self, mock_queue: MagicMock
    ) -> None:
        state: dict[str, Any] = {
            "version": 1,
            "seen_review_ids": [],
            "seen_comment_ids": [],
            "seen_review_requests": [],
            "review_decisions": {},
        }
        pr_details = {
            "repo": "brockventures/gotchi",
            "number": 1,
            "title": "Test PR",
            "url": "https://github.com/brockventures/gotchi/pull/1",
            "author": {"login": "brockventures"},
            "reviewDecision": "COMMENTED",
            "reviews": [
                {
                    "id": "PRR_SELF_1",
                    "author": {"login": "brockventures"},  # Self-review
                    "state": "COMMENTED",
                    "submittedAt": "2026-11-02T00:00:00Z",
                    "body": "Self comment",
                }
            ],
            "reviewRequests": [],
            "comments": [],
        }

        stats = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
        )
        self.assertEqual(stats["reviews_notified"], 0)
        mock_queue.assert_not_called()
        self.assertIn("PRR_SELF_1", state["seen_review_ids"])

    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_check_pr_review_request(self, mock_queue: MagicMock) -> None:
        mock_queue.return_value = {"status": "queued"}
        state: dict[str, Any] = {
            "version": 1,
            "seen_review_ids": [],
            "seen_comment_ids": [],
            "seen_review_requests": [],
            "review_decisions": {},
        }
        pr_details = {
            "repo": "mcarmody/lomin",
            "number": 12,
            "title": "feat: new feature",
            "url": "https://github.com/mcarmody/lomin/pull/12",
            "author": {"login": "mcarmody"},
            "reviewDecision": "REVIEW_REQUIRED",
            "reviews": [],
            "reviewRequests": [{"login": "brockventures"}],
            "comments": [],
        }

        stats = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
        )
        self.assertEqual(stats["review_requests_notified"], 1)
        self.assertEqual(mock_queue.call_count, 2)
        self.assertIn(
            "mcarmody/lomin#12:req:brockventures",
            state["seen_review_requests"],
        )

        # Second run dedupes
        mock_queue.reset_mock()
        stats_dup = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
        )
        self.assertEqual(stats_dup["review_requests_notified"], 0)
        mock_queue.assert_not_called()

    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_check_pr_comments(self, mock_queue: MagicMock) -> None:
        mock_queue.return_value = {"status": "queued"}
        state: dict[str, Any] = {
            "version": 1,
            "seen_review_ids": [],
            "seen_comment_ids": [],
            "seen_review_requests": [],
            "review_decisions": {},
        }
        pr_details = {
            "repo": "mcarmody/lomin",
            "number": 5,
            "title": "feat: vision",
            "url": "https://github.com/mcarmody/lomin/pull/5",
            "author": {"login": "brockventures"},
            "reviewDecision": "NONE",
            "reviews": [],
            "reviewRequests": [],
            "comments": [
                {
                    "id": "COM_100",
                    "author": {"login": "mcarmody"},
                    "createdAt": "2026-11-02T04:15:00Z",
                    "body": "Can you check the CI logs?",
                }
            ],
        }

        stats = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
        )
        self.assertEqual(stats["comments_notified"], 1)
        self.assertEqual(mock_queue.call_count, 2)
        self.assertIn("COM_100", state["seen_comment_ids"])

        # Second run dedupes
        mock_queue.reset_mock()
        stats_dup = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
        )
        self.assertEqual(stats_dup["comments_notified"], 0)
        mock_queue.assert_not_called()

    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_check_pr_seed_only_mode(self, mock_queue: MagicMock) -> None:
        state: dict[str, Any] = {
            "version": 1,
            "seen_review_ids": [],
            "seen_comment_ids": [],
            "seen_review_requests": [],
            "review_decisions": {},
        }
        pr_details = {
            "repo": "mcarmody/lomin",
            "number": 5,
            "title": "feat: vision",
            "url": "https://github.com/mcarmody/lomin/pull/5",
            "author": {"login": "brockventures"},
            "reviewDecision": "CHANGES_REQUESTED",
            "reviews": [
                {
                    "id": "PRR_SEED",
                    "author": {"login": "mcarmody"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-11-02T04:10:04Z",
                    "body": "seed review",
                }
            ],
            "reviewRequests": [{"login": "alice"}],
            "comments": [
                {
                    "id": "COM_SEED",
                    "author": {"login": "mcarmody"},
                    "createdAt": "2026-11-02T04:15:00Z",
                    "body": "seed comment",
                }
            ],
        }

        stats = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
            seed_only=True,
        )
        self.assertEqual(stats["reviews_notified"], 0)
        self.assertEqual(stats["comments_notified"], 0)
        self.assertEqual(stats["review_requests_notified"], 0)
        mock_queue.assert_not_called()
        self.assertIn("PRR_SEED", state["seen_review_ids"])
        self.assertIn("COM_SEED", state["seen_comment_ids"])
        self.assertIn("mcarmody/lomin#5:req:alice", state["seen_review_requests"])

    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_check_pr_dry_run_mode(self, mock_queue: MagicMock) -> None:
        state: dict[str, Any] = {
            "version": 1,
            "seen_review_ids": [],
            "seen_comment_ids": [],
            "seen_review_requests": [],
            "review_decisions": {},
        }
        pr_details = {
            "repo": "mcarmody/lomin",
            "number": 5,
            "title": "feat: vision",
            "url": "https://github.com/mcarmody/lomin/pull/5",
            "author": {"login": "brockventures"},
            "reviewDecision": "CHANGES_REQUESTED",
            "reviews": [
                {
                    "id": "PRR_DRY",
                    "author": {"login": "mcarmody"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-11-02T04:10:04Z",
                    "body": "dry run",
                }
            ],
            "reviewRequests": [],
            "comments": [],
        }

        stats = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="brockventures",
            dry_run=True,
        )
        self.assertEqual(stats["reviews_notified"], 1)
        mock_queue.assert_not_called()


class TestGhPrWatcherEndToEnd(unittest.TestCase):
    """Test full poll_and_notify orchestration."""

    @patch("tools.gh_pr_watcher.get_pr_details")
    @patch("tools.gh_pr_watcher.discover_open_prs")
    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_poll_and_notify_pipeline(
        self,
        mock_outbox: MagicMock,
        mock_discover: MagicMock,
        mock_details: MagicMock,
    ) -> None:
        mock_outbox.return_value = {"status": "queued"}
        mock_discover.return_value = [
            {
                "repo": "mcarmody/lomin",
                "number": 5,
                "title": "PR 5",
                "url": "https://github.com/mcarmody/lomin/pull/5",
                "author": "brockventures",
            }
        ]
        mock_details.return_value = {
            "repo": "mcarmody/lomin",
            "number": 5,
            "title": "PR 5",
            "url": "https://github.com/mcarmody/lomin/pull/5",
            "author": {"login": "brockventures"},
            "reviewDecision": "CHANGES_REQUESTED",
            "reviews": [
                {
                    "id": "PRR_E2E_1",
                    "author": {"login": "mcarmody"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-11-02T04:10:04Z",
                    "body": "Round 3 findings",
                }
            ],
            "reviewRequests": [],
            "comments": [],
        }

        tmp_state = Path("/tmp/e2e_state_test.json")
        if tmp_state.exists():
            tmp_state.unlink()

        try:
            # First poll: notifies and creates state file
            stats = poll_and_notify(
                state_file=tmp_state,
                target_patterns=["mcarmody/lomin"],
                current_user="brockventures",
            )
            self.assertEqual(stats["prs_checked"], 1)
            self.assertEqual(stats["reviews_notified"], 1)
            self.assertEqual(mock_outbox.call_count, 2)
            self.assertTrue(tmp_state.exists())

            # Verify persisted state
            state_data = json.loads(tmp_state.read_text(encoding="utf-8"))
            self.assertIn("PRR_E2E_1", state_data["seen_review_ids"])

            # Second poll: dedupes cleanly
            mock_outbox.reset_mock()
            stats2 = poll_and_notify(
                state_file=tmp_state,
                target_patterns=["mcarmody/lomin"],
                current_user="brockventures",
            )
            self.assertEqual(stats2["prs_checked"], 1)
            self.assertEqual(stats2["reviews_notified"], 0)
            mock_outbox.assert_not_called()
        finally:
            tmp_state.unlink(missing_ok=True)


class TestGhPrWatcherAutoMerge(unittest.TestCase):
    """Test whitelisted author auto-merge and gate conditions."""

    def test_whitelisted_authors_set(self) -> None:
        """Verify all whitelisted Crab Cavern authors are present in WHITELISTED_AUTHORS."""
        expected = {
            "mcarmody",
            "arcane103",
            "aerial-bot",
            "amos-bot",
            "marvin-bot",
            "ryanbrock",
            "zero-brock",
        }
        for author in expected:
            self.assertIn(author, WHITELISTED_AUTHORS)

    def test_is_ci_passing_or_empty(self) -> None:
        """Test CI rollup status evaluator across various shapes."""
        # Empty or None
        self.assertTrue(is_ci_passing_or_empty(None))
        self.assertTrue(is_ci_passing_or_empty([]))
        self.assertTrue(is_ci_passing_or_empty({}))

        # Passing check run list
        self.assertTrue(
            is_ci_passing_or_empty([
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "NEUTRAL"},
                {"status": "COMPLETED", "conclusion": "SKIPPED"},
            ])
        )

        # Passing state list
        self.assertTrue(
            is_ci_passing_or_empty([{"state": "SUCCESS"}, {"state": "SUCCESS"}])
        )

        # Failing conclusion
        self.assertFalse(
            is_ci_passing_or_empty([
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "FAILURE"},
            ])
        )

        # Failing state
        self.assertFalse(
            is_ci_passing_or_empty([
                {"state": "SUCCESS"},
                {"state": "FAILURE"},
            ])
        )

        # In-progress / pending status
        self.assertFalse(
            is_ci_passing_or_empty([{"status": "IN_PROGRESS"}])
        )
        self.assertFalse(
            is_ci_passing_or_empty([{"state": "PENDING"}])
        )

        # Dict summaries
        self.assertTrue(is_ci_passing_or_empty({"state": "SUCCESS"}))
        self.assertFalse(is_ci_passing_or_empty({"state": "FAILURE"}))
        self.assertFalse(is_ci_passing_or_empty({"state": "PENDING"}))

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_approved_green_ci_triggers_auto_merge(
        self, mock_run_gh: MagicMock
    ) -> None:
        """Approved PR from whitelisted author with passing CI triggers squash auto-merge."""
        mock_run_gh.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "merge"],
            returncode=0,
            stdout="Merged",
            stderr="",
        )

        pr_data = {
            "number": 5,
            "author": {"login": "mcarmody"},
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
            ],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("mcarmody/lomin", 5, pr_data)
        self.assertTrue(result)
        mock_run_gh.assert_called_once_with([
            "pr",
            "merge",
            "5",
            "--repo",
            "mcarmody/lomin",
            "--squash",
            "--auto",
        ])

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_approved_empty_ci_triggers_auto_merge(
        self, mock_run_gh: MagicMock
    ) -> None:
        """Approved PR from whitelisted author with empty CI rollup triggers squash auto-merge."""
        mock_run_gh.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "merge"],
            returncode=0,
            stdout="Merged",
            stderr="",
        )

        pr_data = {
            "number": 10,
            "author": "ryanbrock",
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("brockventures/gotchi", 10, pr_data)
        self.assertTrue(result)
        mock_run_gh.assert_called_once_with([
            "pr",
            "merge",
            "10",
            "--repo",
            "brockventures/gotchi",
            "--squash",
            "--auto",
        ])

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_fallback_when_auto_disabled(
        self, mock_run_gh: MagicMock
    ) -> None:
        """When repo has auto-merge disabled, falls back to --squash --delete-branch."""
        # First call fails, second call succeeds
        mock_run_gh.side_effect = [
            subprocess.CompletedProcess(
                args=["gh", "pr", "merge", "--auto"],
                returncode=1,
                stdout="",
                stderr="pull request auto merge is not allowed for this repository",
            ),
            subprocess.CompletedProcess(
                args=["gh", "pr", "merge", "--delete-branch"],
                returncode=0,
                stdout="Merged and deleted branch",
                stderr="",
            ),
        ]

        pr_data = {
            "number": 42,
            "author": {"login": "aerial-bot"},
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [{"state": "SUCCESS"}],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("brockventures/gotchi", 42, pr_data)
        self.assertTrue(result)
        self.assertEqual(mock_run_gh.call_count, 2)
        mock_run_gh.assert_has_calls([
            call([
                "pr",
                "merge",
                "42",
                "--repo",
                "brockventures/gotchi",
                "--squash",
                "--auto",
            ]),
            call([
                "pr",
                "merge",
                "42",
                "--repo",
                "brockventures/gotchi",
                "--squash",
                "--delete-branch",
            ]),
        ])

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_fallback_fails(
        self, mock_run_gh: MagicMock
    ) -> None:
        """When both auto-merge and fallback merge fail, returns False."""
        mock_run_gh.side_effect = [
            subprocess.CompletedProcess(
                args=["gh", "pr", "merge", "--auto"],
                returncode=1,
                stdout="",
                stderr="auto merge disabled",
            ),
            subprocess.CompletedProcess(
                args=["gh", "pr", "merge", "--delete-branch"],
                returncode=1,
                stdout="",
                stderr="branch protection merge error",
            ),
        ]

        pr_data = {
            "number": 42,
            "author": {"login": "aerial-bot"},
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("brockventures/gotchi", 42, pr_data)
        self.assertFalse(result)
        self.assertEqual(mock_run_gh.call_count, 2)

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_peer_approval_without_review_decision(
        self, mock_run_gh: MagicMock
    ) -> None:
        """PR without official reviewDecision but approved by peer collaborator auto-merges."""
        mock_run_gh.return_value = subprocess.CompletedProcess(
            args=["gh", "pr", "merge"],
            returncode=0,
            stdout="Merged",
            stderr="",
        )

        pr_data = {
            "number": 15,
            "author": {"login": "amos-bot"},
            "reviewDecision": "REVIEW_REQUIRED",
            "statusCheckRollup": [],
            "latestReviews": [
                {
                    "id": "REV_PEER_1",
                    "author": {"login": "zero-brock"},
                    "state": "APPROVED",
                }
            ],
        }

        result = try_auto_merge_pr("brockventures/Zero-Agent", 15, pr_data)
        self.assertTrue(result)
        mock_run_gh.assert_called_once()

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_blocked_by_changes_requested(
        self, mock_run_gh: MagicMock
    ) -> None:
        """PR with CHANGES_REQUESTED is blocked from auto-merging."""
        pr_data = {
            "number": 5,
            "author": {"login": "mcarmody"},
            "reviewDecision": "CHANGES_REQUESTED",
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("mcarmody/lomin", 5, pr_data)
        self.assertFalse(result)
        mock_run_gh.assert_not_called()

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_blocked_by_review_with_changes_requested(
        self, mock_run_gh: MagicMock
    ) -> None:
        """PR with active CHANGES_REQUESTED in reviews is blocked even if reviewDecision is empty."""
        pr_data = {
            "number": 5,
            "author": {"login": "mcarmody"},
            "reviewDecision": "NONE",
            "statusCheckRollup": [],
            "latestReviews": [
                {
                    "author": {"login": "arcane103"},
                    "state": "CHANGES_REQUESTED",
                }
            ],
        }

        result = try_auto_merge_pr("mcarmody/lomin", 5, pr_data)
        self.assertFalse(result)
        mock_run_gh.assert_not_called()

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_blocked_by_failing_ci_conclusion(
        self, mock_run_gh: MagicMock
    ) -> None:
        """PR with failing CI conclusion blocks auto-merge."""
        pr_data = {
            "number": 5,
            "author": {"login": "mcarmody"},
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "FAILURE"}
            ],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("mcarmody/lomin", 5, pr_data)
        self.assertFalse(result)
        mock_run_gh.assert_not_called()

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_blocked_by_failing_ci_state(
        self, mock_run_gh: MagicMock
    ) -> None:
        """PR with failing CI state blocks auto-merge."""
        pr_data = {
            "number": 5,
            "author": {"login": "mcarmody"},
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [
                {"state": "FAILURE"}
            ],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("mcarmody/lomin", 5, pr_data)
        self.assertFalse(result)
        mock_run_gh.assert_not_called()

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_blocked_by_pending_ci(
        self, mock_run_gh: MagicMock
    ) -> None:
        """PR with in-progress/pending CI checks blocks auto-merge until green."""
        pr_data = {
            "number": 5,
            "author": {"login": "marvin-bot"},
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [
                {"status": "IN_PROGRESS"}
            ],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("mcarmody/lomin", 5, pr_data)
        self.assertFalse(result)
        mock_run_gh.assert_not_called()

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_non_whitelisted_author_never_merged(
        self, mock_run_gh: MagicMock
    ) -> None:
        """PR from a non-whitelisted author is never auto-merged."""
        pr_data = {
            "number": 99,
            "author": {"login": "untrusted-user"},
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("brockventures/gotchi", 99, pr_data)
        self.assertFalse(result)
        mock_run_gh.assert_not_called()

    @patch("tools.gh_pr_watcher.run_gh")
    def test_try_auto_merge_pr_dry_run(
        self, mock_run_gh: MagicMock
    ) -> None:
        """Dry-run mode returns True without calling gh pr merge."""
        pr_data = {
            "number": 12,
            "author": {"login": "mcarmody"},
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [],
            "reviews": [],
            "latestReviews": [],
        }

        result = try_auto_merge_pr("mcarmody/lomin", 12, pr_data, dry_run=True)
        self.assertTrue(result)
        mock_run_gh.assert_not_called()

    @patch("tools.gh_pr_watcher.try_auto_merge_pr")
    @patch("tools.gh_pr_watcher.queue_outbox_message")
    def test_check_pr_auto_merge_integration_and_dedupe(
        self, mock_outbox: MagicMock, mock_auto_merge: MagicMock
    ) -> None:
        """check_pr executes auto-merge for approved PRs and records state to prevent duplicate merges."""
        mock_outbox.return_value = {"status": "queued"}
        mock_auto_merge.return_value = True

        state: dict[str, Any] = {
            "version": 1,
            "seen_review_ids": [],
            "seen_comment_ids": [],
            "seen_review_requests": [],
            "review_decisions": {},
            "auto_merged_prs": [],
        }

        pr_details = {
            "repo": "mcarmody/lomin",
            "number": 5,
            "title": "feat: auto-merge test",
            "url": "https://github.com/mcarmody/lomin/pull/5",
            "author": {"login": "mcarmody"},
            "reviewDecision": "APPROVED",
            "reviews": [
                {
                    "id": "REV_APPROVED_1",
                    "author": {"login": "arcane103"},
                    "state": "APPROVED",
                    "submittedAt": "2026-11-02T04:50:00Z",
                    "body": "LGTM!",
                }
            ],
            "reviewRequests": [],
            "comments": [],
            "statusCheckRollup": [],
        }

        # First run: triggers auto-merge and records in state
        stats1 = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="zero-brock",
        )
        self.assertTrue(stats1["auto_merged"])
        mock_auto_merge.assert_called_once_with(
            repo="mcarmody/lomin",
            pr_number=5,
            pr_data=pr_details,
            dry_run=False,
        )
        self.assertIn("mcarmody/lomin#5", state["auto_merged_prs"])

        # Second run: does not re-merge already merged PR
        mock_auto_merge.reset_mock()
        stats2 = check_pr(
            pr_details=pr_details,
            state=state,
            current_user="zero-brock",
        )
        self.assertFalse(stats2["auto_merged"])
        mock_auto_merge.assert_not_called()

        # If changes are requested subsequently, removes from auto_merged_prs
        pr_details["reviewDecision"] = "CHANGES_REQUESTED"
        check_pr(
            pr_details=pr_details,
            state=state,
            current_user="zero-brock",
        )
        self.assertNotIn("mcarmody/lomin#5", state["auto_merged_prs"])

    def test_ensure_schedule_registered_idempotent(self) -> None:
        """ensure_schedule_registered adds gh_pr_watcher with */3 * * * * and is idempotent."""
        tmp_sched = Path("/tmp/test_gh_pr_watcher_schedule.json")
        tmp_sched.write_text(json.dumps([{"id": "existing_job"}]), encoding="utf-8")
        try:
            added1 = ensure_schedule_registered(tmp_sched)
            self.assertTrue(added1)
            entries = json.loads(tmp_sched.read_text(encoding="utf-8"))
            self.assertEqual(len(entries), 2)
            watcher_entry = [e for e in entries if e.get("id") == "gh_pr_watcher"][0]
            self.assertEqual(watcher_entry["cron_expression"], "*/3 * * * *")
            self.assertEqual(watcher_entry["interval_seconds"], 180)
            self.assertTrue(watcher_entry["enabled"])

            added2 = ensure_schedule_registered(tmp_sched)
            self.assertFalse(added2)
        finally:
            tmp_sched.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
