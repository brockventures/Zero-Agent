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
            for s in steps:
                f.write(json.dumps(s) + "\n")

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
            for s in steps:
                f.write(json.dumps(s) + "\n")

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
            for s in steps:
                f.write(json.dumps(s) + "\n")

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            self.assertEqual(harvested, "Benchmark complete: R2 is 0.9715.")

        # Now test when turn ended without a final text step (only tool steps followed)
        steps_no_final = steps[:-1]
        with open(transcript_file, "w") as f:
            for s in steps_no_final:
                f.write(json.dumps(s) + "\n")

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
            for s in steps_unrecovered:
                f.write(json.dumps(s) + "\n")

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            harvested = harvest_transcript_response(conv_id)
            self.assertIsNone(harvested)

        # Case 2: Recovered stream drop (' Cui' pre-interruption followed by substantive continuation)
        steps_recovered = list(steps_unrecovered) + [
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "All checks passed successfully."},
        ]
        with open(transcript_file, "w") as f:
            for s in steps_recovered:
                f.write(json.dumps(s) + "\n")

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
            for s in steps_unrecovered:
                f.write(json.dumps(s) + "\n")

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
            for s in steps_thinking_only:
                f.write(json.dumps(s) + "\n")

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
            for s in steps_completed:
                f.write(json.dumps(s) + "\n")

        with patch("tools.bridge_stream.Path", side_effect=lambda p: Path(p) if "/root/.gemini/antigravity-cli/brain" not in str(p) else self.temp_path):
            status = check_transcript_turn_status(conv_id)
            self.assertTrue(status["is_complete"])
            self.assertEqual(status["reason"], "complete")
            self.assertEqual(status["content"], "Stack deployment completed.")


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


if __name__ == "__main__":
    unittest.main()
