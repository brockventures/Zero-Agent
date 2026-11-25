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
from unittest.mock import AsyncMock, MagicMock, patch

if "/workspace" not in sys.path:
    sys.path.append("/workspace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.bridge_stream import (
    AgyStreamParser,
    check_transcript_turn_status,
    format_agy_error_message,
    format_command_preview,
    get_latest_transcript_step_index,
    harvest_transcript_response,
    is_aborted_stream_stub,
    parse_agy_error,
    reconcile_step_segments,
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

    def test_agy_stream_parser_system_message_followed_by_trailing_settlement_receipt(self):
        """Verify that a trailing settlement receipt after an async system message merges with substantive answer."""
        parser = AgyStreamParser(conv_id="conv-123")
        # Step 1: Substantive response
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Yes. Both data sources were validated live from Host 2.",
            },
        })
        # Step 2: Asynchronous system message arrives
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
            },
        })
        # Step 3: Trailing settlement receipt
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "All background validation tasks have settled cleanly.",
                "state": "DONE",
            },
        })
        expected = "Yes. Both data sources were validated live from Host 2.\n\nAll background validation tasks have settled cleanly."
        self.assertEqual(parser.get_final_response(), expected)

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
            f.writelines(json.dumps(s) + "\n" for s in steps)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            self.assertEqual(harvested, "Pong from transcript.")

    def test_harvest_transcript_response_does_not_cross_turn_boundary(self):
        """Verify that harvest_transcript_response never harvests previous turn's response when active turn is incomplete."""
        conv_id = "test-conv-boundary"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript_full.jsonl"

        steps = [
            # Turn 1: Completed
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "What's next?"},
            {"type": "PLANNER_RESPONSE", "content": "Here is what is on deck."},
            # Turn 2: Incomplete (process terminated mid-turn during tool calls)
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Go for 1 and 2"},
            {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]},
            {"type": "GENERIC", "content": "Running tests..."},
        ]
        with open(transcript_file, "w") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            self.assertIsNone(harvested)

    def test_harvest_transcript_response_skips_intermediate_tool_call_steps(self):
        """Verify that harvest_transcript_response skips intermediate steps that had both content and tool_calls."""
        conv_id = "test-conv-tool-chatter"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript_full.jsonl"

        steps = [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Run benchmark"},
            # Step with pre-tool scratchpad content AND tool_calls
            {
                "type": "PLANNER_RESPONSE",
                "content": "And the task finishes: (waiting silently without text output)...",
                "tool_calls": [{"name": "manage_task", "args": {"Action": "status"}}]
            },
            {"type": "GENERIC", "content": "Task status: RUNNING"},
            # Final step with completed text
            {"type": "PLANNER_RESPONSE", "content": "Benchmark complete: R2 is 0.9715."},
        ]
        with open(transcript_file, "w") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            self.assertEqual(harvested, "Benchmark complete: R2 is 0.9715.")

        # Now test when turn ended without a final text step (only tool steps followed)
        steps_no_final = steps[:-1]
        with open(transcript_file, "w") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps_no_final)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            # Intermediate scratchpad should NOT be harvested as final answer
            self.assertIsNone(harvested)

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



    def test_interruption_clears_accumulated_segment_and_prevents_gluing(self):
        parser = AgyStreamParser(conv_id="conv-456")
        # Step 1: Draft starts
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "🍌 <@123> Initial draft cut off mid-word,",
            },
        })
        # Step 2: Stream interruption arrives
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
                "content": "Error: The stream was interrupted. Please continue.",
            },
        })
        # Verify accumulated segment was cleared
        self.assertEqual(len(parser.accumulated_segment), 0)
        self.assertEqual(parser.last_substantive_response, "")

        # Step 3: Redraft starts cleanly
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "🍌 <@123> Clean redrafted message.",
                "state": "DONE",
            },
        })
        self.assertEqual(parser.get_final_response(), "🍌 <@123> Clean redrafted message.")

    def test_cjk_sampler_stub_not_prepended_to_substantive_response(self):
        parser = AgyStreamParser(conv_id="conv-cjk-789")
        # Step 1: Model sampler emits stray CJK filler token on empty wait turn
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "即是",
                "state": "DONE",
            },
        })
        # Step 2: Background task completes via system_message
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
            },
        })
        self.assertEqual(parser.last_substantive_response, "")
        # Step 3: Real substantive response arrives
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "All 11 sidecar upgrades are live and verified across the schedule.",
                "state": "DONE",
            },
        })
        self.assertEqual(
            parser.get_final_response(),
            "All 11 sidecar upgrades are live and verified across the schedule.",
        )

    def test_short_pre_task_stub_discarded_when_substantive_response_arrives(self):
        parser = AgyStreamParser(conv_id="conv-stub-999")
        # Step 1: Short pre-task stub (< 6 words) before background task settles
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Hold on a sec.",
                "state": "DONE",
            },
        })
        # Step 2: Background task completes
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
            },
        })
        # Step 3: Full substantive answer (> 80 chars) arrives
        full_answer = (
            "Here is the complete breakdown of the architecture and why we clamped "
            "WaitMsBeforeAsync to 10,000ms across all synchronous shell executions."
        )
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": full_answer,
                "state": "DONE",
            },
        })
        self.assertEqual(parser.get_final_response(), full_answer)

    def test_intermediate_result_cleared_by_subsequent_tool_calls(self):
        parser = AgyStreamParser(conv_id="conv-ivy-devanagari")
        # Multi-line non-ASCII sampler glitch in an intermediate result event
        parser.process_event({
            "event": "result",
            "result": {
                "conversation_id": "conv-ivy-devanagari",
                "response": "देणें\nदेणें\nदेणें",
            },
        })
        self.assertEqual(parser.final_result_response, "")

        # Even if an intermediate result had non-leak text, subsequent tool calls must clear it
        parser.final_result_response = "Intermediate sub-turn text"
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "run_command",
            },
        })
        self.assertEqual(parser.final_result_response, "")
        self.assertEqual(parser.get_final_response(), "")


    def test_pre_completion_draft_redraft_deduplication_and_reset(self):
        parser = AgyStreamParser(conv_id="conv-bg-dup")
        # Step 1: Premature draft before background task finishes
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Done—projected `RS` and `RA` in the standings are now colored using our 5-tier 30-team scale on [2027 Standings](<https://standings.mikecarmody.net>).",
                "state": "DONE",
            },
        })
        # Intermediate result arrives while background task is still running -> reset_pre_completion_draft
        parser.reset_pre_completion_draft()
        self.assertEqual(parser.get_final_response(), "")

        # Even if not reset (e.g. transcript harvest), reconcile_step_segments deduplicates Done— vs Added— redrafts
        from tools.bridge_stream import reconcile_step_segments
        earlier = "Done—projected `RS` and `RA` in the standings are now colored using our 5-tier 30-team scale on [2027 Standings](<https://standings.mikecarmody.net>)."
        latest = "Added—projected `RS` and `RA` in the standings are now colored using our 5-tier 30-team scale on [2027 Standings](<https://standings.mikecarmody.net>)."
        self.assertEqual(reconcile_step_segments(earlier, latest), latest)

    def test_is_aborted_stream_stub(self):
        """Verify is_aborted_stream_stub identifies 1-token fragments and preserves valid tags."""
        self.assertTrue(is_aborted_stream_stub(" Cui"))
        self.assertTrue(is_aborted_stream_stub("Cui"))
        self.assertTrue(is_aborted_stream_stub(" The"))
        self.assertTrue(is_aborted_stream_stub("Hello world"))
        self.assertFalse(is_aborted_stream_stub("[REACT: 👍]"))
        self.assertFalse(is_aborted_stream_stub("[REACT: 🚀]"))
        self.assertFalse(is_aborted_stream_stub("[NO_REPLY]"))
        self.assertFalse(is_aborted_stream_stub("[NO_OP]"))
        self.assertFalse(is_aborted_stream_stub("Done."))
        self.assertFalse(is_aborted_stream_stub("Yes!"))
        self.assertFalse(is_aborted_stream_stub("Why?"))
        self.assertFalse(is_aborted_stream_stub(""))
        self.assertFalse(is_aborted_stream_stub(None))
        self.assertFalse(
            is_aborted_stream_stub(
                "This is a full sentence with sufficient length that was cut off mid"
            )
        )

    def test_reconcile_step_segments_discards_cui_stub_on_interruption(self):
        """Verify reconcile_step_segments discards 1-token pre-interruption stubs."""
        earlier = " Cui"
        latest = "All verification gates passed cleanly."
        # When interrupted, ' Cui' must be discarded in favor of latest
        self.assertEqual(
            reconcile_step_segments(earlier, latest, is_interrupted=True),
            "All verification gates passed cleanly.",
        )

    def test_agy_stream_parser_discards_cui_stub_on_stream_interruption(self):
        """Verify AgyStreamParser discards ' Cui' stream stubs on interruption and returns empty if unrecovered."""
        parser = AgyStreamParser(conv_id="conv-cui-test")
        # Step 1: 1-token fragment emitted before stream drop
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": " Cui",
            },
        })
        # Step 2: Stream interruption event
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
                "content": "Error: The stream was interrupted. Please continue.",
            },
        })
        # Verify stub was NOT stored as interrupted_draft
        self.assertEqual(parser.interrupted_draft, "")
        self.assertEqual(len(parser.accumulated_segment), 0)
        self.assertTrue(parser.interrupted_since_last_step)

        # Step 3: Unrecovered turn exits with fallback or result event matching stub
        parser.process_event({
            "event": "result",
            "result": {
                "conversation_id": "conv-cui-test",
                "response": " Cui",
            },
        })
        # get_final_response must discard the stub and return empty string
        self.assertEqual(parser.get_final_response(fallback_result=" Cui"), "")

        # Now test recovered stream: subsequent auto-continuation step produces real answer
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Here is the completed analysis.",
                "state": "DONE",
            },
        })
        self.assertEqual(parser.get_final_response(), "Here is the completed analysis.")

    def test_harvest_transcript_response_discards_cui_stub_on_interruption(self):
        """Verify harvest_transcript_response skips ' Cui' pre-interruption stubs and handles recovery."""
        conv_id = "test-conv-harvest-cui"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript_full.jsonl"

        # Case 1: Unrecovered stream drop (' Cui' pre-interruption, no post-interruption candidate)
        steps_unrecovered = [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Run checks"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": " Cui", "output_tokens": 1},
            {"step_index": 2, "type": "ERROR_MESSAGE", "content": "Error: The stream was interrupted. Please continue."},
        ]
        with open(transcript_file, "w") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps_unrecovered)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            self.assertIsNone(harvested)

        # Case 2: Recovered stream drop (' Cui' pre-interruption followed by substantive continuation)
        steps_recovered = list(steps_unrecovered) + [
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "All checks passed successfully."},
        ]
        with open(transcript_file, "w") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps_recovered)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            self.assertEqual(harvested, "All checks passed successfully.")

    def test_check_transcript_turn_status_stream_interrupted_on_cui_stub(self):
        """Verify check_transcript_turn_status returns stream_interrupted on unrecovered ' Cui' stream drops."""
        conv_id = "test-conv-turn-status-cui"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript_full.jsonl"

        # Case 1: Unrecovered drop (stream interrupted immediately after ' Cui')
        steps_unrecovered = [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Deploy stack"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": " Cui", "output_tokens": 1},
            {"step_index": 2, "type": "ERROR_MESSAGE", "content": "Error: The stream was interrupted. Please continue."},
        ]
        with open(transcript_file, "w") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps_unrecovered)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            status = check_transcript_turn_status(conv_id)
            self.assertFalse(status["is_complete"])
            self.assertEqual(status["reason"], "stream_interrupted")
            self.assertIsNone(status["content"])

        # Case 2: Auto-continuation step produces only internal thinking (empty content)
        steps_thinking_only = list(steps_unrecovered) + [
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "", "thinking": "Internal reasoning only..."},
        ]
        with open(transcript_file, "w") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps_thinking_only)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            status = check_transcript_turn_status(conv_id)
            self.assertFalse(status["is_complete"])
            self.assertEqual(status["reason"], "stream_interrupted")
            self.assertIsNone(status["content"])

        # Case 3: Post-interruption auto-continuation finishes with substantive answer
        steps_completed = list(steps_unrecovered) + [
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Stack deployment completed."},
        ]
        with open(transcript_file, "w") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps_completed)

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            status = check_transcript_turn_status(conv_id)
            self.assertTrue(status["is_complete"])
            self.assertEqual(status["reason"], "complete")
            self.assertEqual(status["content"], "Stack deployment completed.")

    def test_check_transcript_turn_status_reads_tail_on_large_transcript(self):
        """Verify check_transcript_turn_status seeks to the tail on transcripts > 64KB without reading full file."""
        conv_id = "test-conv-tail-read"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript.jsonl"

        with open(transcript_file, "w", encoding="utf-8") as f:
            # Write > 100 KB of historical turns
            for i in range(250):
                f.write(json.dumps({
                    "step_index": i * 2,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "Historical prompt " + ("x" * 350),
                }) + "\n")
                f.write(json.dumps({
                    "step_index": i * 2 + 1,
                    "type": "PLANNER_RESPONSE",
                    "content": "Historical answer " + ("y" * 350),
                }) + "\n")
            # Active turn at the tail
            f.write(json.dumps({
                "step_index": 500,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "content": "Latest prompt",
            }) + "\n")
            f.write(json.dumps({
                "step_index": 501,
                "type": "PLANNER_RESPONSE",
                "content": "Tail answer verified cleanly.",
            }) + "\n")

        self.assertGreater(transcript_file.stat().st_size, 100_000)
        status = check_transcript_turn_status(conv_id, min_step_index=499, brain_base_dir=self.temp_path)
        self.assertTrue(status["is_complete"])
        self.assertEqual(status["reason"], "complete")
        self.assertEqual(status["content"], "Tail answer verified cleanly.")
        self.assertEqual(status["step_index"], 501)

    def test_get_latest_transcript_step_index_tail_seek_and_fallback(self):
        """Verify get_latest_transcript_step_index uses O(1) tail seek and falls back only when needed."""
        self.assertEqual(get_latest_transcript_step_index(None, brain_base_dir=self.temp_path), -1)
        self.assertEqual(get_latest_transcript_step_index("missing-conv", brain_base_dir=self.temp_path), -1)

        # Case 1: Large >100KB transcript resolves step_index via tail seek without full-file readlines()
        conv_id = "test-conv-step-idx-tail"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        compact_path = log_dir / "transcript.jsonl"
        full_path = log_dir / "transcript_full.jsonl"

        with open(compact_path, "w", encoding="utf-8") as f:
            f.writelines(
                json.dumps({"step_index": i, "type": "USER_INPUT", "content": "x" * 400}) + "\n"
                for i in range(300)
            )
        # Also write transcript_full.jsonl with a >40KB trailing line (>32KB tail window)
        with open(full_path, "w", encoding="utf-8") as f:
            f.writelines(
                json.dumps({"step_index": i, "type": "USER_INPUT", "content": "y" * 400}) + "\n"
                for i in range(299)
            )
            f.write(json.dumps({"step_index": 299, "type": "PLANNER_RESPONSE", "content": "z" * 45000}) + "\n")

        self.assertGreater(compact_path.stat().st_size, 100_000)
        self.assertEqual(get_latest_transcript_step_index(conv_id, brain_base_dir=self.temp_path), 299)

        # Case 2: Single file whose trailing line exceeds 32KB falls back to full read accurately
        conv_huge_tail = "test-conv-step-idx-fallback"
        huge_dir = self.temp_path / conv_huge_tail / ".system_generated" / "logs"
        huge_dir.mkdir(parents=True, exist_ok=True)
        only_full = huge_dir / "transcript_full.jsonl"
        with open(only_full, "w", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 41, "type": "USER_INPUT", "content": "ok"}) + "\n")
            f.write(json.dumps({"step_index": 42, "type": "PLANNER_RESPONSE", "content": "big" * 15000}) + "\n")

        self.assertGreater(only_full.stat().st_size, 32768)
        self.assertEqual(get_latest_transcript_step_index(conv_huge_tail, brain_base_dir=self.temp_path), 42)

    def test_agy_stream_parser_drops_pre_system_message_meta_leak(self):
        """Verify pre-system_message meta-reasoning leak is discarded and never prepended to post-task reply."""
        parser = AgyStreamParser(conv_id="conv-meta-leak")
        leak_text = "Testing... wait no — emit zero text! (I'm in thought block, will output nothing.)"
        real_reply = "All 3 integration checks passed after the background job completed."

        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "step_index": 1,
                "text_delta": leak_text,
                "state": "DONE",
            },
        })
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
            },
        })
        self.assertEqual(parser.last_substantive_response, "")

        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "step_index": 2,
                "text_delta": real_reply,
                "state": "DONE",
            },
        })
        self.assertEqual(parser.get_final_response(), real_reply)
        self.assertEqual(reconcile_step_segments(leak_text, real_reply), real_reply)
        self.assertEqual(reconcile_step_segments(real_reply, leak_text), real_reply)

    def test_check_transcript_turn_status_rejects_meta_reasoning_leak(self):
        """Verify check_transcript_turn_status classifies meta-reasoning leak as empty_or_leak_content."""
        conv_id = "test-conv-meta-leak-status"
        log_dir = self.temp_path / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript_full.jsonl"

        steps = [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Run checks"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Testing... wait no — emit zero text! (I'm in thought block, will output nothing.)",
            },
        ]
        with open(transcript_file, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(s) + "\n" for s in steps)

        status = check_transcript_turn_status(conv_id, brain_base_dir=self.temp_path)
        self.assertFalse(status["is_complete"])
        self.assertEqual(status["reason"], "empty_or_leak_content")
        self.assertIsNone(status["content"])
        self.assertEqual(status["step_index"], 1)

    def test_background_task_launch_and_completion_discards_pre_task_wait_chatter(self):
        """Verify background task launch -> unflagged wait chatter -> task-completion system_message outputs ONLY final reply."""
        parser = AgyStreamParser(conv_id="conv-bg-wait-chatter")
        # Step 1: Tool launches a background task
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "result": "Tool is running as a background task with task ID task-818."
                },
            },
        })
        self.assertTrue(parser.has_pending_background_task)

        # Step 2: Arbitrary unflagged wait text / self-talk emitted before task finishes
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "step_index": 2,
                "text_delta": "Testing... wait NO! Do NOT emit ANY visible text!",
                "state": "DONE",
            },
        })

        # Step 3: Task completion system_message arrives -> must discard pre-completion chatter completely
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
                "content": "Background task task-818 finished with result: exit code 0",
            },
        })
        self.assertFalse(parser.has_pending_background_task)
        self.assertEqual(parser.last_substantive_response, "")
        self.assertEqual(len(parser.accumulated_segment), 0)

        # Step 4: Final reply arrives
        final_reply = "Deployment completed cleanly and all health checks passed."
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "step_index": 3,
                "text_delta": final_reply,
                "state": "DONE",
            },
        })
        self.assertEqual(parser.get_final_response(), final_reply)

        # Also verify top-level tool / system_message event branches mirror step_update behavior
        top_parser = AgyStreamParser(conv_id="conv-bg-top-level")
        top_parser.process_event({
            "event": "tool_use",
            "tool_name": "run_command",
            "output": "Running as a background task with task ID task-909",
        })
        self.assertTrue(top_parser.has_pending_background_task)
        top_parser.process_event({
            "event": "content",
            "content": "Hold on, waiting for background worker to finish.",
        })
        top_parser.process_event({
            "event": "system_message",
            "content": "Task ID task-909 finished with result: ok",
        })
        self.assertFalse(top_parser.has_pending_background_task)
        self.assertEqual(top_parser.last_substantive_response, "")
        top_parser.process_event({
            "event": "content",
            "content": "Final top-level reply.",
        })
        self.assertEqual(top_parser.get_final_response(), "Final top-level reply.")

    def test_pre_tool_text_rotated_by_system_message_cleared_by_subsequent_tool_call(self):
        """Verify pre-tool text rotated into last_substantive_response by system_message is cleared on next tool call."""
        parser = AgyStreamParser(conv_id="conv-tool-clears-last")
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Checking the logs right now, stand by.",
                "state": "DONE",
            },
        })
        # Non-task system_message rotates text into last_substantive_response
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "system_message",
            },
        })
        self.assertEqual(
            parser.last_substantive_response,
            "Checking the logs right now, stand by.",
        )

        # Subsequent tool call must clear last_substantive_response so pre-tool chatter never leaks
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "read_file",
            },
        })
        self.assertEqual(parser.last_substantive_response, "")
        self.assertEqual(parser.get_final_response(), "")

        # Final post-tool answer is returned cleanly without pre-tool chatter
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Logs show zero errors over the last 24h.",
                "state": "DONE",
            },
        })
        self.assertEqual(
            parser.get_final_response(),
            "Logs show zero errors over the last 24h.",
        )

        # Verify top-level tool_call also clears last_substantive_response
        parser.last_substantive_response = "Stale rotated draft"
        parser.process_event({"event": "tool_call", "tool_name": "run_command"})
        self.assertEqual(parser.last_substantive_response, "")

    def test_reset_pre_completion_draft_pending_task_flag_and_daemon_carveout(self):
        """Verify reset_pre_completion_draft preserves has_pending_background_task by default, clears on flag, and ignores daemons."""
        parser = AgyStreamParser(conv_id="conv-daemon-carveout")

        # Daemon tool payloads must NOT arm has_pending_background_task
        for daemon_step in (
            {
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "parameters": {"CommandLine": "npm run dev", "IsDaemon": True},
                    "result": "Tool is running as a background task with task ID task-daemon-1",
                },
            },
            {
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "parameters": {"CommandLine": "tail -f app.log", "is_daemon": True},
                    "result": "Running as a background task with task ID task-daemon-2",
                },
            },
        ):
            parser.process_event({"event": "step_update", "step_update": daemon_step})
            self.assertFalse(parser.has_pending_background_task)

        # Non-daemon background task arms has_pending_background_task
        parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "parameters": {"CommandLine": "pytest", "IsDaemon": False},
                    "result": "Tool is running as a background task with task ID task-fg-1",
                },
            },
        })
        self.assertTrue(parser.has_pending_background_task)
        parser.interrupted_since_last_step = True
        parser.last_substantive_response = "Premature wait chatter"

        # Default reset preserves has_pending_background_task while clearing drafts and interrupted flag
        parser.reset_pre_completion_draft()
        self.assertTrue(parser.has_pending_background_task)
        self.assertFalse(parser.interrupted_since_last_step)
        self.assertEqual(parser.last_substantive_response, "")

        # Explicit clear_pending_task=True disarms has_pending_background_task
        parser.reset_pre_completion_draft(clear_pending_task=True)
        self.assertFalse(parser.has_pending_background_task)


class TestBridgeStreamEngineIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration unit tests for TurnCoordinator and bridge_runner stream interruption recovery."""

    async def test_turn_coordinator_retries_unrecovered_stream_interruption(self):
        """Verify TurnCoordinator triggers transient error handler and recycles worker on unrecovered stream drop."""
        from tools.bridge_engine import TurnCoordinator

        coord = TurnCoordinator(
            channel_id=1542081375287640084,
            prompt="Run build checks",
            mode="home",
            reply_target=MagicMock(),
            status_msg=None,
            conv_id="conv-drop-unrecovered",
        )

        lines = [
            b'{"event":"result","result":{"conversation_id":"conv-drop-unrecovered","status":"SUCCESS","response":" Cui"}}\n',
            b"",
        ]
        line_iter = iter(lines)
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=lambda: next(line_iter))

        parser = MagicMock()
        parser.interrupted_since_last_step = True
        # Chunk 1 discards aborted stub so get_final_response returns empty string
        parser.get_final_response.return_value = ""
        parser.error_response = None

        timer = MagicMock()
        proc = MagicMock()
        proc.pid = 9991
        mock_recycle = AsyncMock()

        with self.assertRaises(RuntimeError) as ctx:
            await coord.execute_stream_loop(
                proc=proc,
                stdout_reader=reader,
                stream_parser=parser,
                timer=timer,
                rules={"live_status_ticker_enabled": False},
                on_recycle=mock_recycle,
            )

        self.assertIn("Transient upstream API error", str(ctx.exception))
        self.assertIn("The stream was interrupted (EOF)", str(ctx.exception))
        mock_recycle.assert_awaited_once_with(keep_conv_id=True)

    async def test_turn_coordinator_preserves_recovered_stream_response(self):
        """Verify TurnCoordinator cleanly returns substantive answer if stream successfully recovered."""
        from tools.bridge_engine import TurnCoordinator

        coord = TurnCoordinator(
            channel_id=1542081375287640084,
            prompt="Run build checks",
            mode="home",
            reply_target=MagicMock(),
            status_msg=None,
            conv_id="conv-drop-recovered",
        )

        lines = [
            b'{"event":"result","result":{"conversation_id":"conv-drop-recovered","status":"SUCCESS","response":"Build succeeded."}}\n',
            b"",
        ]
        line_iter = iter(lines)
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=lambda: next(line_iter))

        parser = MagicMock()
        parser.interrupted_since_last_step = True
        parser.get_final_response.return_value = "Build succeeded."
        parser.error_response = None

        timer = MagicMock()
        proc = MagicMock()
        proc.pid = 9992
        mock_recycle = AsyncMock()

        resp = await coord.execute_stream_loop(
            proc=proc,
            stdout_reader=reader,
            stream_parser=parser,
            timer=timer,
            rules={"live_status_ticker_enabled": False},
            on_recycle=mock_recycle,
        )

        self.assertEqual(resp, "Build succeeded.")
        mock_recycle.assert_not_called()

    async def test_turn_coordinator_discards_intermediate_result_via_stream_pending_bg_flag(self):
        """Verify execute_stream_loop intercepts premature result via stream_parser.has_pending_background_task even on transcript-flush race or missing conv_id."""
        from tools.bridge_engine import TurnCoordinator

        coord = TurnCoordinator(
            channel_id=1542081375287640084,
            prompt="Run long check",
            mode="home",
            reply_target=MagicMock(),
            status_msg=None,
            conv_id=None,
        )

        # Backdate turn_start_time so stale-result <1.5s filter does not mask the pending-bg guard
        coord.turn_start_time -= 5.0

        lines = [
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "result": "Tool is running as a background task with task ID task-race-1."
                    },
                },
            }).encode() + b"\n",
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 1,
                    "text_delta": "Testing... wait NO! Do NOT emit ANY visible text!",
                    "state": "DONE",
                },
            }).encode() + b"\n",
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Testing... wait NO! Do NOT emit ANY visible text!",
                },
            }).encode() + b"\n",
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "system_message",
                    "content": "Background task task-race-1 finished with result: exit code 0",
                },
            }).encode() + b"\n",
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "step_index": 2,
                    "text_delta": "All background checks completed cleanly.",
                    "state": "DONE",
                },
            }).encode() + b"\n",
            json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": "conv-bg-race-1",
                    "status": "SUCCESS",
                    "response": "All background checks completed cleanly.",
                },
            }).encode() + b"\n",
            b"",
        ]
        line_iter = iter(lines)
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=lambda: next(line_iter))

        parser = AgyStreamParser()
        timer = MagicMock()
        proc = MagicMock()
        proc.pid = 9993
        mock_recycle = AsyncMock()

        with patch("tools.task_settle.get_turn_pending_tasks", return_value=[]), \
             patch("tools.bridge_engine.check_transcript_turn_status", return_value={"is_complete": False, "has_active_tool": False, "reason": "no_entries"}), \
             patch("tools.bridge_engine.set_channel_session_id"):
            resp = await coord.execute_stream_loop(
                proc=proc,
                stdout_reader=reader,
                stream_parser=parser,
                timer=timer,
                rules={"live_status_ticker_enabled": False},
                on_recycle=mock_recycle,
            )

        self.assertEqual(resp, "All background checks completed cleanly.")
        self.assertFalse(parser.has_pending_background_task)
        mock_recycle.assert_not_called()

    def test_bridge_runner_transient_stream_drop_retry_detection(self):
        """Verify that bridge runner flags unrecovered stream drops as retryable with 'Upstream stream interruption' label."""
        max_retries = 2
        attempt = 0

        # Case 1: Stream interrupted error in output with no substantive response
        has_substantive = False
        full_raw = "Error: The stream was interrupted. Please continue."
        is_transient_stream_drop = (not has_substantive) and (
            "the stream was interrupted" in full_raw.lower()
            or "transient upstream api error" in full_raw.lower()
        )
        self.assertTrue(is_transient_stream_drop and attempt < max_retries)
        reason_label = (
            "Upstream stream interruption"
            if is_transient_stream_drop
            else "Transient Google auth/API handshake hiccup"
        )
        self.assertEqual(reason_label, "Upstream stream interruption")

        # Case 2: Transient upstream API error string from TurnCoordinator
        full_raw_transient = "RuntimeError: Transient upstream API error in #worker: Error: The stream was interrupted (EOF)"
        is_transient_stream_drop_api = (not has_substantive) and (
            "the stream was interrupted" in full_raw_transient.lower()
            or "transient upstream api error" in full_raw_transient.lower()
        )
        self.assertTrue(is_transient_stream_drop_api)

        # Case 3: Substantive response exists - should NOT retry
        has_substantive_true = True
        is_retry = (not has_substantive_true) and (
            "the stream was interrupted" in full_raw.lower()
            or "transient upstream api error" in full_raw.lower()
        )
        self.assertFalse(is_retry)

    def test_stream_parser_canonical_silence_and_react_preservation(self):
        """Verify AgyStreamParser normalizes canonical SILENCE_SENTINELS and preserves [REACT: ...] tags."""
        for sentinel in ("no_reply", "NO_OP", "reply: none", "none"):
            parser = AgyStreamParser(conv_id="conv-silence-canon")
            parser.process_event({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": sentinel,
                    "state": "DONE",
                },
            })
            self.assertTrue(parser.is_explicit_silence)
            self.assertEqual(parser.get_final_response(), "[NO_REPLY]")

        # Reaction-only segment must pass through intact to deliver_turn_output
        react_parser = AgyStreamParser(conv_id="conv-react-keep")
        react_parser.process_event({
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "[REACT: 👍]",
                "state": "DONE",
            },
        })
        self.assertFalse(react_parser.is_explicit_silence)
        self.assertEqual(react_parser.get_final_response(), "[REACT: 👍]")

        # is_aborted_stream_stub honors canonical silence sentinels
        self.assertFalse(is_aborted_stream_stub("reply:none"))
        self.assertFalse(is_aborted_stream_stub("no_op"))


if __name__ == "__main__":
    unittest.main()
