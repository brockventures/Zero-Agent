#!/usr/bin/env python3
"""
Unit tests for tools/transcript_scrubber.py (Option B - Tool Output Scrubbing).
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

WORKSPACE = Path("/workspace")
sys.path.insert(0, str(WORKSPACE))

from tools.transcript_scrubber import scrub_transcript_tool_outputs, sync_transcript_chunks


class TestTranscriptScrubber(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.brain_root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scrub_nonexistent_session(self):
        saved = scrub_transcript_tool_outputs("nonexistent-conv", brain_root=self.brain_root)
        self.assertEqual(saved, 0)

    def test_scrub_preserves_user_input_and_prunes_large_tools(self):
        conv_id = "test-conv-1"
        logs_dir = self.brain_root / conv_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        full_path = logs_dir / "transcript_full.jsonl"

        large_tool_output = "The command exited with code 0.\nOutput:\n" + ("x" * 5000)
        large_thinking = "Evaluating options...\n" + ("y" * 1000)

        steps = [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Check disk space on NAS"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "thinking": large_thinking, "tool_calls": [{"name": "run_command", "args": {"cmd": "df -h"}}]},
            {"step_index": 2, "type": "GENERIC", "source": "MODEL", "content": large_tool_output},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "source": "MODEL", "content": "Disk space is at 45%."},
            {"step_index": 4, "type": "GENERIC", "source": "MODEL", "content": "Short output ok."},
        ]

        raw_jsonl = "\n".join(json.dumps(s) for s in steps) + "\n"
        transcript_path.write_text(raw_jsonl, encoding="utf-8")
        full_path.write_text(raw_jsonl, encoding="utf-8")

        # Create chunks dir
        chunk_dir = logs_dir / "chunks" / "transcript"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        (chunk_dir / "00000000.jsonl").write_bytes(transcript_path.read_bytes())

        saved = scrub_transcript_tool_outputs(conv_id, max_output_len=200, grace_turns=0, brain_root=self.brain_root)
        self.assertGreater(saved, 4000)

        # Verify scrubbed transcript
        scrubbed_lines = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(scrubbed_lines), 5)

        # Step 0: User input intact
        self.assertEqual(scrubbed_lines[0]["content"], "Check disk space on NAS")

        # Step 1: Thinking scrubbed, tool_calls intact
        self.assertEqual(scrubbed_lines[1]["thinking"], "[Thinking scrubbed]")
        self.assertEqual(scrubbed_lines[1]["tool_calls"][0]["name"], "run_command")

        # Step 2: Tool output pruned
        self.assertIn("The command exited with code 0.", scrubbed_lines[2]["content"])
        self.assertIn("[Tool output scrubbed for context optimization:", scrubbed_lines[2]["content"])
        self.assertNotIn("xxxxx", scrubbed_lines[2]["content"])

        # Step 3: Final agent response intact
        self.assertEqual(scrubbed_lines[3]["content"], "Disk space is at 45%.")

        # Step 4: Short tool output (<200) kept intact
        self.assertEqual(scrubbed_lines[4]["content"], "Short output ok.")

        # Full transcript remains untouched
        self.assertEqual(full_path.read_text(encoding="utf-8"), raw_jsonl)

        # Chunks were synchronized
        chunk_files = list(chunk_dir.glob("*.jsonl"))
        self.assertGreater(len(chunk_files), 0)
        reconstructed = b"".join(cf.read_bytes() for cf in sorted(chunk_files))
        self.assertEqual(reconstructed, transcript_path.read_bytes())

    def test_grace_window_preserves_most_recent_turn(self):
        """Verify that grace_turns=1 preserves the latest turn verbatim while scrubbing older turns."""
        conv_id = "test-conv-grace"
        logs_dir = self.brain_root / conv_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        full_path = logs_dir / "transcript_full.jsonl"

        turn1_tool_output = "The command exited with code 0.\nOutput:\n" + ("a" * 4000)
        turn2_tool_output = "The command exited with code 0.\nOutput:\n" + ("b" * 4000)

        steps = [
            # Turn 1 (Older turn)
            {"step_index": 0, "type": "USER_INPUT", "content": "Turn 1 prompt"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "thinking": "Thinking 1 " * 50, "tool_calls": [{"name": "run_command"}]},
            {"step_index": 2, "type": "GENERIC", "content": turn1_tool_output},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Turn 1 answer"},
            # Turn 2 (Most recent turn)
            {"step_index": 4, "type": "USER_INPUT", "content": "Turn 2 prompt"},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "thinking": "Thinking 2 " * 50, "tool_calls": [{"name": "run_command"}]},
            {"step_index": 6, "type": "GENERIC", "content": turn2_tool_output},
            {"step_index": 7, "type": "PLANNER_RESPONSE", "content": "Turn 2 answer"},
        ]

        raw_jsonl = "\n".join(json.dumps(s) for s in steps) + "\n"
        transcript_path.write_text(raw_jsonl, encoding="utf-8")
        full_path.write_text(raw_jsonl, encoding="utf-8")

        # Scrub with grace_turns=1
        saved = scrub_transcript_tool_outputs(conv_id, max_output_len=200, grace_turns=1, brain_root=self.brain_root)
        self.assertGreater(saved, 3500)

        scrubbed_lines = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(scrubbed_lines), 8)

        # Turn 1: Tool output scrubbed & thinking scrubbed
        self.assertEqual(scrubbed_lines[1]["thinking"], "[Thinking scrubbed]")
        self.assertIn("[Tool output scrubbed for context optimization:", scrubbed_lines[2]["content"])
        self.assertNotIn("aaaaa", scrubbed_lines[2]["content"])

        # Turn 2: Step 6 tool output preserved 100% verbatim!
        self.assertEqual(scrubbed_lines[6]["content"], turn2_tool_output)
        self.assertIn("bbbbb", scrubbed_lines[6]["content"])
        self.assertNotIn("[Tool output scrubbed", scrubbed_lines[6]["content"])

    def test_single_turn_grace_window_preserves_all(self):
        """Verify that a single-turn session is entirely preserved when grace_turns=1."""
        conv_id = "test-conv-single"
        logs_dir = self.brain_root / conv_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        full_path = logs_dir / "transcript_full.jsonl"

        turn1_tool_output = "The command exited with code 0.\nOutput:\n" + ("z" * 4000)

        steps = [
            {"step_index": 0, "type": "USER_INPUT", "content": "Only turn"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "thinking": "Thinking " * 50, "tool_calls": [{"name": "run_command"}]},
            {"step_index": 2, "type": "GENERIC", "content": turn1_tool_output},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Only answer"},
        ]

        raw_jsonl = "\n".join(json.dumps(s) for s in steps) + "\n"
        transcript_path.write_text(raw_jsonl, encoding="utf-8")
        full_path.write_text(raw_jsonl, encoding="utf-8")

        saved = scrub_transcript_tool_outputs(conv_id, max_output_len=200, grace_turns=1, brain_root=self.brain_root)
        self.assertEqual(saved, 0)

        scrubbed_lines = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(scrubbed_lines[2]["content"], turn1_tool_output)


if __name__ == "__main__":
    unittest.main()
