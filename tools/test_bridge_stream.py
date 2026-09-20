"""
test_bridge_stream.py — Unit tests for AgyStreamParser, transcript harvesting, and stream protocol.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.bridge_stream import (
    AgyStreamParser,
    format_agy_error_message,
    format_command_preview,
    harvest_transcript_response,
    parse_agy_error,
)


class TestBridgeStream(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_format_command_preview(self):
        # Local command
        cmd = "python3 -m unittest discover -s tools"
        self.assertEqual(
            format_command_preview(cmd),
            "Running: python3 -m unittest discover -s tools...",
        )

        # SSH command
        with patch.dict(os.environ, {"NAS_HOST_1_IP": "nas.example.com"}):
            ssh_cmd = "ssh nas.example.com docker ps"
            preview = format_command_preview(ssh_cmd)
            self.assertIn("Running [nas.example.com]: docker ps...", preview)

    def test_agy_stream_parser_tool_clears_pre_tool_narration(self):
        parser = AgyStreamParser(conv_id="conv-123")
        # Step 1: Pre-tool narration
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "I am going to check the files.",
            },
        })
        self.assertEqual(len(parser.accumulated_segment), 1)

        # Step 2: Tool event clears pre-tool narration
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "run_command",
            },
        })
        self.assertEqual(len(parser.accumulated_segment), 0)

        # Step 3: Post-tool answer
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "All files are nominal.",
                "state": "DONE",
            },
        })
        self.assertEqual(parser.get_final_response(), "All files are nominal.")

    def test_agy_stream_parser_system_message_preserves_substantive_response(self):
        parser = AgyStreamParser(conv_id="conv-123")
        # Step 1: Substantive response
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Here is the critical analysis of the bug.",
            },
        })
        # Step 2: Asynchronous system message arrives
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
            },
        })
        self.assertEqual(parser.last_substantive_response, "Here is the critical analysis of the bug.")
        self.assertEqual(parser.get_final_response(), "Here is the critical analysis of the bug.")

    def test_agy_stream_parser_result_event(self):
        parser = AgyStreamParser(conv_id="conv-123")
        parser.process_event({
            "event": "result",
            "result": {
                "conversation_id": "conv-updated-456",
                "response": "Final completed answer.",
            },
        })
        self.assertEqual(parser.conv_id, "conv-updated-456")
        self.assertEqual(parser.get_final_response(), "Final completed answer.")

    def test_agy_stream_parser_process_line(self):
        parser = AgyStreamParser()
        line = '{"event": "init", "conversation_id": "conv-line-789"}\n'
        self.assertTrue(parser.process_line(line))
        self.assertEqual(parser.conv_id, "conv-line-789")

        # Multi-JSON per line
        multi_line = '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Part 1 "}}{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"Part 2"}}'
        self.assertTrue(parser.process_line(multi_line))
        self.assertEqual(parser.get_final_response(), "Part 1 Part 2")

    def test_harvest_transcript_response(self):
        conv_id = "test-conv-stream-harvest"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript_full.jsonl"

        steps = [
            {"type": "USER_INPUT", "content": "Ping"},
            {"type": "PLANNER_RESPONSE", "content": "Pong from transcript."},
        ]
        with open(transcript_file, "w") as f:
            for s in steps:
                f.write(json.dumps(s) + "\n")

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            self.assertEqual(harvested, "Pong from transcript.")

    def test_parse_agy_error(self):
        text = 'AGY_ERROR: {"canonical_status": "UNAVAILABLE", "code": 503, "retryable": true, "error_id": "err-503", "short_error": "Backend timeout"}\n'
        err = parse_agy_error(text)
        self.assertIsNotNone(err)
        self.assertEqual(err.get("canonical_status"), "UNAVAILABLE")
        self.assertEqual(err.get("code"), 503)
        self.assertTrue(err.get("retryable"))

        # Empty or nominal text
        self.assertIsNone(parse_agy_error("Everything is running fine."))

    def test_format_agy_error_message(self):
        err = {
            "canonical_status": "RESOURCE_EXHAUSTED",
            "code": 429,
            "retryable": False,
            "error_id": "err-quota-1",
            "short_error": "Daily quota exceeded",
        }
        msg = format_agy_error_message(err, elapsed_sec=42, pid_str="PID 12345")
        self.assertIn("Model API Failure (CLI Exit Code 3)", msg)
        self.assertIn("`RESOURCE_EXHAUSTED` (Code 429)", msg)
        self.assertIn("Daily quota exceeded", msg)
        self.assertIn("`err-quota-1`", msg)
        self.assertIn("Retryable:** No", msg)
        self.assertIn("Elapsed:** 42s", msg)
        self.assertIn("Process:** `PID 12345`", msg)


if __name__ == "__main__":
    unittest.main()
