"""
Unit tests for tools/bridge_safety.py.
Directly exercises leak detection, CLI chatter stripping, repetitive pattern deduplication, and credential scrubbing.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.bridge_safety import (
    CLI_LEAK_LINE_PATTERNS,
    _get_scrub_targets,
    dedup_repetitive_patterns,
    is_internal_cli_leak,
    scrub_credentials,
    strip_internal_cli_chatter,
)


class TestBridgeSafety(unittest.TestCase):

    def test_is_internal_cli_leak(self):
        self.assertTrue(is_internal_cli_leak(""))
        self.assertTrue(is_internal_cli_leak(None))
        self.assertTrue(is_internal_cli_leak("[NO_REPLY]"))
        self.assertTrue(is_internal_cli_leak("no_reply"))
        self.assertTrue(is_internal_cli_leak("[NO_OP]"))
        self.assertTrue(is_internal_cli_leak("reply:none"))
        self.assertTrue(is_internal_cli_leak("*(Response completed, but no text output was generated)*"))
        self.assertTrue(is_internal_cli_leak("No content generated yet."))
        self.assertTrue(is_internal_cli_leak("... [repetitive output truncated] ..."))
        self.assertTrue(is_internal_cli_leak("error: interrupted"))
        self.assertTrue(is_internal_cli_leak("Process c123/task-456 completed with exit code 0. Output:"))
        self.assertTrue(is_internal_cli_leak("Tool is running as a background task with task id: task-999"))
        self.assertTrue(is_internal_cli_leak("An async task has completed: task-999"))
        self.assertTrue(is_internal_cli_leak("<WAITING_FOR_TASKS_OUTPUT>\nWait for at least one of the background tasks to complete:\n- d4e7469b-ec01-48bf-abc3-1ee1b05f8c8e/task-818</WAITING_FOR_TASKS_OUTPUT>"))
        self.assertTrue(is_internal_cli_leak("Wait for at least one of the background tasks to complete:\n- task-818"))
        self.assertTrue(is_internal_cli_leak("Wait for task: d6f2513e-4b4b-414e-bf5a-b1ffb52b472b/task-246 to complete. No other work to do."))
        self.assertTrue(is_internal_cli_leak("Wait for background task to complete..."))
        self.assertTrue(is_internal_cli_leak("Waiting for task to complete."))
        self.assertTrue(is_internal_cli_leak("Wait for command to finish."))
        self.assertTrue(is_internal_cli_leak("No other work to do."))

        # Real substantive user messages should NEVER be classified as a leak
        self.assertFalse(is_internal_cli_leak("Deployment completed successfully. All 4 containers are healthy."))
        self.assertFalse(is_internal_cli_leak("The MLB model predicts the Dodgers over the Giants with 62% confidence."))
        self.assertFalse(is_internal_cli_leak("I updated the task board with all open milestones."))

    def test_strip_internal_cli_chatter(self):
        system_msg = "<SYSTEM_MESSAGE>This is an internal instruction</SYSTEM_MESSAGE>\n\nSubstantive content here."
        self.assertEqual(strip_internal_cli_chatter(system_msg), "Substantive content here.")

        task_wait_msg = (
            "<WAITING_FOR_TASKS_OUTPUT>\n"
            "Wait for at least one of the background tasks to complete:\n"
            "- d4e7469b-ec01-48bf-abc3-1ee1b05f8c8e/task-818</WAITING_FOR_TASKS_OUTPUT>\n\n"
            "Final results are ready."
        )
        self.assertEqual(strip_internal_cli_chatter(task_wait_msg), "Final results are ready.")

        task_msg = (
            "Tool is running as a background task with task id: task-xyz\n"
            "Task Description: Running tests\n"
            "Task logs are available at: /tmp/log\n"
            "YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS:\n"
            "DO NOTHING ELSE.\n\n"
            "Final results are ready."
        )
        self.assertEqual(strip_internal_cli_chatter(task_msg), "Final results are ready.")

        async_msg = (
            "An async task has completed\n"
            "Task ID: task-1\n"
            "Task Output:\n"
            "All tests passed\n"
            "<end of task output>\n\n"
            "Ready for review."
        )
        self.assertEqual(strip_internal_cli_chatter(async_msg), "Ready for review.")

        # Test regression: Quoting the SDK prompt inside backticks should NOT strip the rest of the text
        quoted_msg = (
            "The SDK prompted:\n"
            "`YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS: A) proceed to other work or B) simply update the user.`\n\n"
            "The model took the Option B bait.\n\n"
            "### Resolution\n"
            "Handled cleanly."
        )
        cleaned_quoted = strip_internal_cli_chatter(quoted_msg)
        self.assertIn("The model took the Option B bait.", cleaned_quoted)
        self.assertIn("### Resolution", cleaned_quoted)

        # Test lounge async command running leak is completely stripped and identified as leak
        lounge_leak = (
            "An async command is running. The system will automatically resume execution when the command completes. "
            "Do not poll or call additional tools until the background task finishes.\n"
            "Task log: `/root/.gemini/antigravity-cli/brain/17b40886-f700-437d-ba37-5fddd3563eab/.system_generated/tasks/task-223.log`\n"
            "Task: 17b40886-f700-437d-ba37-5fddd3563eab/task-223, Status: completed\n"
            "Match: b'defaultRulesBudget'\n"
            "Match: b'budget to default'\n"
            "Match: b'defaultRules"
        )
        self.assertEqual(strip_internal_cli_chatter(lounge_leak), "")
        self.assertTrue(is_internal_cli_leak(lounge_leak))

    def test_dedup_repetitive_patterns(self):
        repeated_line = "Running diagnostic check...\n" * 10
        collapsed = dedup_repetitive_patterns(repeated_line, max_repeats=3)
        self.assertIn("... [repetitive output truncated] ...", collapsed)
        self.assertEqual(collapsed.count("Running diagnostic check..."), 3)

        non_repeating = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\nLine 6"
        self.assertEqual(dedup_repetitive_patterns(non_repeating), non_repeating)

    def test_scrub_credentials_private_ips(self):
        ip1 = "192.168.1." + "82"
        ip2 = "192.168.1." + "84"
        text = f"Connecting to Home Assistant at {ip1}:8123 and NAS at {ip2}."
        scrubbed = scrub_credentials(text)
        self.assertNotIn(ip1, scrubbed)
        self.assertNotIn(ip2, scrubbed)
        self.assertIn("[internal-ip]", scrubbed)

    def test_scrub_credentials_tokens(self):
        text = "GitHub token: ghp_1234567890abcdef1234567890abcdef and OpenAI: sk-1234567890abcdef1234567890abcdef"
        scrubbed = scrub_credentials(text)
        self.assertNotIn("ghp_1234567890abcdef1234567890abcdef", scrubbed)
        self.assertNotIn("sk-1234567890abcdef1234567890abcdef", scrubbed)
        self.assertIn("[REDACTED_GITHUB_TOKEN]", scrubbed)
        self.assertIn("[REDACTED_API_KEY]", scrubbed)


if __name__ == "__main__":
    unittest.main()
