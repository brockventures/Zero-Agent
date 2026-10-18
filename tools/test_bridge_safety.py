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
    is_transient_auth_error,
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
        self.assertTrue(is_internal_cli_leak("An asynchronous task is already running. Please do not call any more tools until the task finishes. Wait silently for the next event."))
        self.assertTrue(is_internal_cli_leak("An asynchronous background task is currently running: e660bca3-f3aa-4000-8e36-fc31186cf566/task-1269 (./game/tests/run.sh).\nContinuing silent background execution until completion."))
        self.assertTrue(is_internal_cli_leak("WaitMsBeforeAsync: 10000 has expired. Command is still running. Will wait for completion."))
        self.assertTrue(is_internal_cli_leak("No response text needed while waiting for background task."))
        self.assertTrue(is_internal_cli_leak("Continuing silent background execution until completion."))
        self.assertTrue(is_internal_cli_leak("- Awaiting isolated execution results.\n\n- Awaiting grid search results."))
        self.assertTrue(is_internal_cli_leak("Awaiting grid search results."))
        self.assertTrue(is_internal_cli_leak("- Awaiting task execution."))

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

        leak1 = "An asynchronous task is already running. Please do not call any more tools until the task finishes. Wait silently for the next event."
        self.assertEqual(strip_internal_cli_chatter(leak1), "")
        leak2 = "An asynchronous background task is currently running: e660bca3-f3aa-4000-8e36-fc31186cf566/task-1269 (./game/tests/run.sh).\nContinuing silent background execution until completion."
        self.assertEqual(strip_internal_cli_chatter(leak2), "")

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


    def test_auth_eligibility_leak_detection(self):
        err1 = 'Error: Eligibility check failed: failed to get profile picture: Get "https://lh3.googleusercontent.com/a/test": net/http: TLS handshake timeout'
        err2 = 'Error: Eligibility check failed: Get "https://www.googleapis.com/oauth2/v2/userinfo": EOF'
        self.assertTrue(is_internal_cli_leak(err1))
        self.assertTrue(is_internal_cli_leak(err2))
        self.assertTrue(is_transient_auth_error(err1))
        self.assertTrue(is_transient_auth_error(err2))

        # Substantive responses discussing auth failures must NOT be dropped as leaks
        autopsy_msg = (
            "### Network Autopsy & Investigation Receipts\n\n"
            "The gateway stalled during DNS resolution. Specifically, when querying "
            "https://www.googleapis.com/oauth2/v2/userinfo the TLS handshake timed out due to "
            "upstream packet loss on eth4. We verified that both interfaces were attempting to "
            "claim the same CIDR simultaneously. Once the conflict was resolved, traffic resumed nominal throughput."
        )
        self.assertFalse(is_internal_cli_leak(autopsy_msg))

        valid = 'Host 1 is fully up and running. Coordinators reconnected.'
        self.assertFalse(is_internal_cli_leak(valid))
        self.assertFalse(is_transient_auth_error(valid))

    def test_substantive_wait_discussion_not_leak(self):
        msg = (
            "### The Proposed Protection Architecture\n\n"
            "1. Pre-Flight Task Audit:\n"
            "Before executing reload, inspect running tasks.\n\n"
            "2. Interactive Gate:\n"
            "With interactive choices: Force Reload Now or Wait for Tasks to Settle or Abort Reload\n\n"
            "Selecting Wait for Tasks to Settle touches the reload flag, and we wait for background tasks to settle cleanly."
        )
        self.assertFalse(is_internal_cli_leak(msg))


    def test_antigravity_async_task_completion_and_json_scrubbing(self):
        # Exact reproduction of Antigravity task completion injection with stdout and JSON block
        raw_task_echo = (
            "An async task 6341716f-8aa2-4937-9a24-66f2d2189c8b/task-618 has completed with exit code: 0\n"
            "Log file content:\n"
            "Uploading V2 Preview to Discord #3d-print...\n"
            "Image delivery result: {'success': True, 'file_path': '/workspace/data/3dprint/zero_casual_robot_v2_preview.png'}\n"
            "Copying V2 STL and profiles to Host 2 OrcaSlicer directory...\n"
            "Running OrcaSlicer on Host 2...\n"
            "Staging G-code on OctoPrint...\n"
            "=== FINAL RESULTS ===\n"
            "{\n"
            '  "image_delivery": {\n'
            '    "success": true\n'
            "  },\n"
            '  "state": "Printing"\n'
            "}\n\n"
            "Preview render is live in [#3d-print](<https://discord.com/channels/1330447543477338202/1555973316752449576>).\n\n"
            "V2 completely closes the topological holes that caused V1 to print like hollow papercraft."
        )

        stripped = strip_internal_cli_chatter(raw_task_echo)
        self.assertNotIn("An async task", stripped)
        self.assertNotIn("Log file content:", stripped)
        self.assertNotIn("Uploading V2", stripped)
        self.assertNotIn("=== FINAL RESULTS ===", stripped)
        self.assertNotIn('"image_delivery"', stripped)
        self.assertTrue(stripped.startswith("Preview render is live in"))
        self.assertIn("V2 completely closes the topological holes", stripped)

        # Standalone task notification should be fully neutralized and marked as leak
        standalone_task = (
            "An async task 6341716f-8aa2-4937-9a24-66f2d2189c8b/task-744 has completed with exit code: 0\n"
            "Log file content:\n"
            "Ran 75 tests in 0.443s\n\n"
            "OK\n\n"
            "Log: file:///root/.gemini/antigravity-cli/brain/task-744.log\n"
        )
        self.assertTrue(is_internal_cli_leak(standalone_task))
        self.assertEqual(strip_internal_cli_chatter(standalone_task), "")


if __name__ == "__main__":
    unittest.main()
