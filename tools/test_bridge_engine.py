"""
test_bridge_engine.py — Unit tests for TurnCoordinator & unified bridge execution engine.
"""

import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.bridge_engine import TurnCoordinator


class TestBridgeEngine(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
