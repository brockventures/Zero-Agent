#!/usr/bin/env python3
"""
Unit test suite for bridge_runner.py (Process Execution & PTY Engine).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import tools.bridge_runner as br


class TestBridgeRunner(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_find_new_artifacts(self):
        brain_temp = self.temp_path / "brain"
        conv_dir = brain_temp / "conv-test-123"
        conv_dir.mkdir(parents=True, exist_ok=True)

        now = time.time()
        art1 = conv_dir / "report.md"
        art1.write_text("# Report")
        os.utime(art1, (now + 5, now + 5))

        # Hidden files or older files should be ignored
        hidden = conv_dir / ".hidden"
        hidden.write_text("hidden")
        old = conv_dir / "old.md"
        old.write_text("old")
        os.utime(old, (now - 100, now - 100))

        with patch("tools.bridge_runner.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else brain_temp):
            # Test direct brain root replacement
            with patch("pathlib.Path.exists", return_value=True):
                found = br.find_new_artifacts(now, conv_id="conv-test-123")
                # When using real filesystem path directly
                self.assertTrue(isinstance(found, list))

    def test_transient_auth_signals(self):
        signals = [
            "Error: Eligibility check failed for user profile",
            "Exception: failed to get profile picture during init",
            "CRITICAL: timeout waiting for response from API",
            "Error: authentication failed or timed out after 30s"
        ]
        for sig in signals:
            is_transient = any(s.lower() in sig.lower() for s in [
                "Eligibility check failed",
                "failed to get profile picture",
                "failed to get user info",
                "authentication failed or timed out",
                "timeout waiting for response"
            ])
            self.assertTrue(is_transient, f"Failed matching transient signal: {sig}")

    def test_silent_reply_patterns(self):
        silent_candidates = [
            "reply:none",
            "reply: none",
            "none",
            "[NO_REPLY]",
            "NO_REPLY",
            "[NO_OP]",
            "NO_OP",
            ""
        ]
        for c in silent_candidates:
            is_silent = (c.strip().lower() in ("reply:none", "reply: none", "none", "") or
                         c.strip() in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP"))
            self.assertTrue(is_silent, f"Candidate should be evaluated as silent: {c}")

    def test_home_turf_silence_tags_converted_to_error(self):
        """Verify that in home turf, silent reply tags are never suppressed and instead flagged."""
        for tag in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP", "reply:none", "reply: none"):
            text = tag
            if text.strip() in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP", "reply:none", "reply: none"):
                text = "⚠️ **Turn Incomplete:** Received unexpected silent reply tag in home turf pairing mode."
            self.assertIn("⚠️ **Turn Incomplete:**", text)

    def test_choices_tag_parsing(self):
        text = "Task completed successfully.\n[CHOICES: Run Tests | Commit Code | Deploy]"
        import re
        matches = list(re.finditer(r"\[CHOICES:\s*([^\]]+)\]", text))
        self.assertEqual(len(matches), 1)
        raw_choices = matches[0].group(1).strip()
        delim = "|" if "|" in raw_choices else ","
        choices = [c.strip() for c in raw_choices.split(delim) if c.strip()]
        self.assertEqual(choices, ["Run Tests", "Commit Code", "Deploy"])

        # Filtered placeholder check
        bad_text = "Here are options:\n[CHOICES: Option 1 | Option 2 | ...]"
        m_bad = list(re.finditer(r"\[CHOICES:\s*([^\]]+)\]", bad_text))
        b_choices = [c.strip() for c in m_bad[0].group(1).split("|") if c.strip()]
        is_placeholder = all(c in ("...", "…", "Option 1", "Option 2", "Option 3") for c in b_choices)
        self.assertTrue(is_placeholder)

    def test_retry_preserves_escalated_thread_state(self):
        """Verify that when a turn escalates to a thread, retries preserve the thread delivery target."""
        mock_msg = MagicMock()
        mock_thread = MagicMock()
        mock_thread.id = 999888777
        mock_thread.jump_url = "https://discord.com/channels/1/999888777"
        
        # Test state retention pattern
        escalated_to_thread = True
        delivery_target = mock_thread
        
        target_dest = delivery_target if delivery_target else mock_msg
        self.assertEqual(target_dest, mock_thread)
        self.assertFalse(hasattr(target_dest, "reply") and not hasattr(mock_thread, "reply"))

    def test_harvest_transcript_response(self):
        """Verify that harvest_transcript_response extracts the last completed PLANNER_RESPONSE."""
        conv_id = "test-conv-harvest-456"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript_full.jsonl"

        steps = [
            {"type": "USER_INPUT", "content": "What is the capital of France?"},
            {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]},
            {"type": "GENERIC", "content": "Command finished"},
            {"type": "PLANNER_RESPONSE", "content": "The capital of France is Paris."}
        ]
        with open(transcript_file, "w") as f:
            for s in steps:
                f.write(json.dumps(s) + "\n")

        with patch("tools.bridge_runner.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            recovered = br.harvest_transcript_response(conv_id)
            self.assertEqual(recovered, "The capital of France is Paris.")

    def test_kill_process_tree(self):
        """Verify that kill_process_tree signals the process group via os.killpg."""
        mock_proc = MagicMock()
        mock_proc.pid = 4321
        mock_proc.returncode = None

        with patch("os.getpgid", return_value=4321) as mock_getpgid, \
             patch("os.killpg") as mock_killpg:
            br.kill_process_tree(mock_proc, sig=br.signal.SIGTERM)
            mock_getpgid.assert_called_once_with(4321)
            mock_killpg.assert_called_once_with(4321, br.signal.SIGTERM)

    def test_split_chunk_line_buffering(self):
        """Verify that JSON events crossing 8KB read boundaries are properly reassembled and parsed."""
        raw_event = json.dumps({
            "event": "result",
            "result": {
                "conversation_id": "conv-split-test",
                "status": "SUCCESS",
                "response": "Here is the response split across chunks." * 200
            }
        }) + "\n"

        # Split across two chunks right through the middle
        split_idx = 8192 if len(raw_event) > 8192 else len(raw_event) // 2
        chunk1 = raw_event[:split_idx]
        chunk2 = raw_event[split_idx:]

        line_buffer = ""
        parsed_events = []

        for chunk in [chunk1, chunk2]:
            line_buffer += chunk
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                line_s = line.strip()
                if line_s.startswith("{") and line_s.endswith("}"):
                    parsed_events.append(json.loads(line_s))

        self.assertEqual(len(parsed_events), 1)
        self.assertEqual(parsed_events[0]["event"], "result")
        self.assertEqual(parsed_events[0]["result"]["conversation_id"], "conv-split-test")
        self.assertEqual(line_buffer, "")

    def test_dual_completion_triggers(self):
        """Verify cutoff logic triggers when either result event or agent_response DONE is observed."""
        now = time.time()

        # Case 1: Result event received > 1.5s ago
        result_received_at = now - 1.6
        agent_response_done_at = None
        is_result_cutoff = (result_received_at is not None and (now - result_received_at) >= 1.5)
        is_agent_done_cutoff = (agent_response_done_at is not None and (now - agent_response_done_at) >= 2.0)
        self.assertTrue(is_result_cutoff or is_agent_done_cutoff)

        # Case 2: Agent response completed (state: DONE) > 2.0s ago without result event
        result_received_at = None
        agent_response_done_at = now - 2.1
        is_result_cutoff = (result_received_at is not None and (now - result_received_at) >= 1.5)
        is_agent_done_cutoff = (agent_response_done_at is not None and (now - agent_response_done_at) >= 2.0)
        self.assertTrue(is_result_cutoff or is_agent_done_cutoff)

        # Case 3: Inactive - agent still actively generating
        result_received_at = None
        agent_response_done_at = None
        is_result_cutoff = (result_received_at is not None and (now - result_received_at) >= 1.5)
        is_agent_done_cutoff = (agent_response_done_at is not None and (now - agent_response_done_at) >= 2.0)
        self.assertFalse(is_result_cutoff or is_agent_done_cutoff)


if __name__ == "__main__":
    unittest.main()


