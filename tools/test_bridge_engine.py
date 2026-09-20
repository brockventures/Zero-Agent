"""
test_bridge_engine.py — Unit tests for TurnCoordinator & unified bridge execution engine.
"""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.bridge_engine import TurnCoordinator


class TestBridgeEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reply_target = MagicMock()
        self.coordinator = TurnCoordinator(
            channel_id=1542081375287640084,
            prompt="Hello Zero",
            mode="home",
            reply_target=self.reply_target,
            status_msg=None,
            conv_id="conv-123",
        )

    def test_initialization(self):
        self.assertEqual(self.coordinator.channel_id, 1542081375287640084)
        self.assertEqual(self.coordinator.prompt, "Hello Zero")
        self.assertEqual(self.coordinator.delivery_target, self.reply_target)
        self.assertFalse(self.coordinator.escalated_to_thread)
        self.assertFalse(self.coordinator.timed_out)

    def test_touch_activity(self):
        old_time = self.coordinator.last_activity_time - 5.0
        self.coordinator.last_activity_time = old_time
        self.coordinator.touch_activity()
        self.assertGreater(self.coordinator.last_activity_time, old_time)

    def test_watchdog_timeout_evaluation(self):
        now = time.time()

        # Case 1: Active, nominal
        self.coordinator.turn_start_time = now - 10.0
        self.coordinator.last_activity_time = now - 5.0
        timed_out, reason = self.coordinator.check_watchdog_timeout(
            step_idle_timeout=90.0, turn_timeout_seconds=300.0, max_turn_ceiling=1800.0
        )
        self.assertFalse(timed_out)
        self.assertEqual(reason, "")

        # Case 2: Step idle timeout (silence >= 90s)
        self.coordinator.last_activity_time = now - 95.0
        timed_out, reason = self.coordinator.check_watchdog_timeout(
            step_idle_timeout=90.0, turn_timeout_seconds=300.0, max_turn_ceiling=1800.0
        )
        self.assertTrue(timed_out)
        self.assertIn("90s step idle", reason)
        self.assertFalse(self.coordinator.is_hard_ceiling)

        # Case 3: Hard ceiling timeout (turn running >= 1800s)
        self.coordinator.turn_start_time = now - 1805.0
        self.coordinator.last_activity_time = now - 5.0
        timed_out, reason = self.coordinator.check_watchdog_timeout(
            step_idle_timeout=90.0, turn_timeout_seconds=300.0, max_turn_ceiling=1800.0
        )
        self.assertTrue(timed_out)
        self.assertIn("1800s hard ceiling", reason)
        self.assertTrue(self.coordinator.is_hard_ceiling)

    def test_completion_cutoffs(self):
        now = time.time()

        # Case 1: Result event received >= 1.5s ago
        self.coordinator.result_received_at = now - 1.6
        self.coordinator.agent_response_done_at = None
        cutoff, label = self.coordinator.evaluate_completion_cutoffs(agent_done_window=15.0)
        self.assertTrue(cutoff)
        self.assertEqual(label, "Result event")

        # Case 2: Agent response DONE >= 15.0s ago AND quiescent
        self.coordinator.result_received_at = None
        self.coordinator.agent_response_done_at = now - 15.5
        self.coordinator.last_activity_time = now - 15.5
        cutoff, label = self.coordinator.evaluate_completion_cutoffs(agent_done_window=15.0)
        self.assertTrue(cutoff)
        self.assertIn("Agent response DONE", label)
        self.assertIn("15s quiescent", label)

        # Case 3: Agent response DONE recent (< 15.0s) -> Do not terminate prematurely!
        self.coordinator.result_received_at = None
        self.coordinator.agent_response_done_at = now - 2.0
        self.coordinator.last_activity_time = now - 1.0
        cutoff, label = self.coordinator.evaluate_completion_cutoffs(agent_done_window=15.0)
        self.assertFalse(cutoff)

    def test_process_stream_events(self):
        # 1. init event
        init_ev = {"event": "init", "conversation_id": "conv-new-999"}
        with patch("tools.bridge_engine.set_channel_session_id") as mock_set_sess:
            self.coordinator.process_stream_event(init_ev)
            self.assertEqual(self.coordinator.conv_id, "conv-new-999")
            mock_set_sess.assert_called_once_with(self.coordinator.channel_id, "home", "conv-new-999")

        # 2. step_update for tool (resets agent_response_done_at and updates preview)
        self.coordinator.agent_response_done_at = time.time()
        tool_ev = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "IN_PROGRESS",
                "tool_name": "run_command",
                "tool_info": {"parameters": {"CommandLine": "python3 -m unittest"}},
            },
        }
        self.coordinator.process_stream_event(tool_ev)
        self.assertIsNone(self.coordinator.agent_response_done_at)
        self.assertIn("Running", self.coordinator.current_action)

        # 3. step_update for agent_response (tracks delta and sets completion on DONE)
        resp_ev_delta = {
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "state": "IN_PROGRESS",
                "text_delta": "Working on it...",
            },
        }
        self.coordinator.process_stream_event(resp_ev_delta)
        self.assertTrue(self.coordinator.had_substantive_delta)
        self.assertEqual(self.coordinator.current_action, "Drafting response...")

        resp_ev_done = {
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "state": "DONE",
            },
        }
        self.coordinator.process_stream_event(resp_ev_done)
        self.assertIsNotNone(self.coordinator.agent_response_done_at)

        # 4. result event
        result_ev = {
            "event": "result",
            "result": {"conversation_id": "conv-new-999", "status": "SUCCESS"},
        }
        self.coordinator.process_stream_event(result_ev)
        self.assertIsNotNone(self.coordinator.result_received_at)
        self.assertEqual(self.coordinator.current_action, "Finalizing output...")

    @patch("tools.bridge_engine.diagnose_process_tree")
    def test_probe_process_wedge(self, mock_diag):
        now = time.time()
        # Case 1: Silence < 45s -> No probe
        self.coordinator.last_activity_time = now - 10.0
        self.assertFalse(self.coordinator.probe_process_wedge(1234))
        mock_diag.assert_not_called()

        # Case 2: Silence >= 45s, not interactive stdin
        self.coordinator.last_activity_time = now - 50.0
        self.coordinator.last_probe_time = now - 15.0
        mock_diag.return_value = {"is_interactive_stdin": False}
        self.assertFalse(self.coordinator.probe_process_wedge(1234))
        mock_diag.assert_called_once()

        # Case 3: Silence >= 45s, interactive stdin detected!
        mock_diag.reset_mock()
        mock_diag.return_value = {
            "is_interactive_stdin": True,
            "summary": "Process wedged waiting on interactive STDIN",
            "culprit": {"name": "npx nxapi", "pid": 1235, "wchan": "n_tty_read"},
        }
        self.coordinator.last_probe_time = now - 15.0
        self.assertTrue(self.coordinator.probe_process_wedge(1234))
        self.assertTrue(self.coordinator.timed_out)
        self.assertIsNotNone(self.coordinator.wedged_diagnostic)

    def test_format_diagnostic_beacon(self):
        # Wedged
        self.coordinator.wedged_diagnostic = {
            "summary": "Wedged on interactive input",
            "culprit": {"name": "test_cmd", "pid": 555, "wchan": "n_tty_read"},
        }
        beacon = self.coordinator.format_diagnostic_beacon(proc_pid=555)
        self.assertIn("Subprocess Wedged on Interactive Input", beacon)
        self.assertIn("test_cmd", beacon)

        # Timed out
        self.coordinator.wedged_diagnostic = None
        self.coordinator.timed_out = True
        beacon = self.coordinator.format_diagnostic_beacon(proc_pid=555, turn_timeout_seconds=300)
        self.assertIn("Turn Timed Out", beacon)
        self.assertIn("300s", beacon)

        # API Failure (exit code 3)
        self.coordinator.timed_out = False
        beacon = self.coordinator.format_diagnostic_beacon(proc_pid=555, returncode=3)
        self.assertIn("Model API Failure (CLI Exit Code 3)", beacon)

        # Process exit non-zero
        beacon = self.coordinator.format_diagnostic_beacon(proc_pid=555, returncode=137)
        self.assertIn("Process terminated with exit code 137", beacon)

    def test_handle_line(self):
        # 1. JSON line
        json_line = '{"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "view_file", "tool_info": {"parameters": {"AbsolutePath": "/test/file.py"}}}}'
        ev = self.coordinator.handle_line(json_line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.get("event"), "step_update")
        self.assertIn("Reading: file.py", self.coordinator.current_action)

        # 2. Terminal action bullet
        bullet_line = "● Fetching git commit status..."
        self.coordinator.handle_line(bullet_line)
        self.assertEqual(self.coordinator.current_action, bullet_line)

        # 3. AGY_ERROR line
        agy_err_line = 'AGY_ERROR: {"canonical_status": "RESOURCE_EXHAUSTED", "code": 429, "retryable": false, "error_id": "err-1", "short_error": "Rate limit"}'
        self.coordinator.handle_line(agy_err_line)
        self.assertIsNotNone(self.coordinator.last_agy_error)
        self.assertEqual(self.coordinator.last_agy_error.get("code"), 429)

    async def test_update_status_ticker(self):
        mock_msg = AsyncMock()
        self.coordinator.status_msg = mock_msg
        self.coordinator.current_action = "\x1b[32mSearching files...\x1b[0m"

        # Disabled ticker
        self.coordinator.last_status_edit = time.time() - 2.0
        updated = await self.coordinator.update_status_ticker(ticker_enabled=False)
        self.assertFalse(updated)
        mock_msg.edit.assert_not_awaited()

        # Enabled ticker
        updated = await self.coordinator.update_status_ticker(ticker_enabled=True)
        self.assertTrue(updated)
        mock_msg.edit.assert_awaited_once_with(content="⏳ *Searching files...*")

    @patch("tools.bridge_engine.harvest_transcript_response")
    def test_harvest_fallback(self, mock_harvest):
        # Case 1: Nominal substantive text
        res, was_harvested = self.coordinator.harvest_fallback("Hello Ryan")
        self.assertEqual(res, "Hello Ryan")
        self.assertFalse(was_harvested)
        mock_harvest.assert_not_called()

        # Case 2: External silence sentinel -> maps to [NO_REPLY]
        coord_ext = TurnCoordinator(
            channel_id=123, prompt="ping", mode="external", reply_target=self.reply_target, status_msg=None, conv_id=None
        )
        res, was_harvested = coord_ext.harvest_fallback("[NO_REPLY]")
        self.assertEqual(res, "[NO_REPLY]")
        self.assertFalse(was_harvested)

        # Case 3: Home empty output with recoverable transcript
        mock_harvest.return_value = "Recovered response text from disk."
        res, was_harvested = self.coordinator.harvest_fallback("")
        self.assertTrue(was_harvested)
        self.assertIn("Recovered from session transcript", res)
        self.assertIn("Recovered response text from disk.", res)

        # Case 4: Home empty output without transcript -> error beacon
        mock_harvest.return_value = None
        res, was_harvested = self.coordinator.harvest_fallback("")
        self.assertFalse(was_harvested)
        self.assertIn("⚠️ **Turn Incomplete:**", res)

    async def test_execute_stream_loop_success(self):
        reader = AsyncMock()
        lines = [
            b'{"event":"step_update","step_update":{"step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"ls"}}}}\n',
            b'{"event":"result","result":{"conversation_id":"conv-warm-222","status":"SUCCESS","response":"Loop test done"}}\n',
            b''
        ]
        line_iter = iter(lines)
        reader.readline = AsyncMock(side_effect=lambda: next(line_iter))

        parser = MagicMock()
        parser.get_final_response.return_value = "Loop test done"
        timer = MagicMock()
        proc = MagicMock()
        proc.pid = 9999

        resp = await self.coordinator.execute_stream_loop(
            proc=proc,
            stdout_reader=reader,
            stream_parser=parser,
            timer=timer,
            rules={"live_status_ticker_enabled": False},
        )
        self.assertEqual(resp, "Loop test done")
        self.assertEqual(self.coordinator.conv_id, "conv-warm-222")

    async def test_execute_stream_loop_eof_with_agy_error(self):
        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=b"")

        parser = MagicMock()
        timer = MagicMock()
        proc = MagicMock()
        proc.pid = 8888
        proc.returncode = 3

        agy_err = {
            "canonical_status": "RESOURCE_EXHAUSTED",
            "code": 429,
            "retryable": False,
            "error_id": "err-test-eof",
            "short_error": "Out of tokens",
        }
        mock_recycle = AsyncMock()

        resp = await self.coordinator.execute_stream_loop(
            proc=proc,
            stdout_reader=reader,
            stream_parser=parser,
            timer=timer,
            rules={"live_status_ticker_enabled": False},
            on_recycle=mock_recycle,
            get_last_agy_error=lambda: agy_err,
        )
        mock_recycle.assert_awaited_once()
        self.assertIn("⚠️ **Model API Failure (CLI Exit Code 3):**", resp)
        self.assertIn("Out of tokens", resp)

    @patch("tools.bridge_engine.update_beacon")
    @patch("tools.bridge_engine.clear_in_flight")
    def test_cleanup_process(self, mock_clear_in_flight, mock_update_beacon):
        from tools.bridge_engine import channel_active_procs
        proc = MagicMock()
        proc.returncode = 0
        channel_active_procs[self.coordinator.channel_id] = proc

        mock_br = MagicMock()
        mock_br.active_proc = proc

        self.coordinator.cleanup_process(proc=proc, br_module=mock_br)
        self.assertNotIn(self.coordinator.channel_id, channel_active_procs)
        self.assertIsNone(mock_br.active_proc)
        mock_clear_in_flight.assert_called_once_with(self.coordinator.channel_id)
        mock_update_beacon.assert_called_once_with("IDLE", "")


if __name__ == "__main__":
    unittest.main()

