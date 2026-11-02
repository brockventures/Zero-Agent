"""
test_bridge_early_ack.py — Unit tests for 2-stage initial response & threaded deep follow-up.

Verifies:
1. AgyStreamParser captures and pops deferred ack findings ending in [[defer]]
   before clearing accumulated segment on tool steps (both step 1 and after 1-3 tool steps).
2. AgyStreamParser strips stray [[defer]] tokens from get_final_response.
3. TurnCoordinator.maybe_send_early_ack() is completely disabled when early_ack_enabled=False
   and purges buffered parser token.
4. TurnCoordinator.maybe_send_early_ack() delivers preliminary finding when enabled,
   records early_ack_msg, and points delivery_target to sent_m so downstream deep-dive
   responses thread natively as replies to the first message.
5. TurnCoordinator.maybe_send_early_ack() returns False and sends no message when stream_parser
   has no deferred ack even after early_ack_seconds and >= 4 tool calls.
"""

import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pathlib import Path
import sys
worktree_root = str(Path(__file__).resolve().parent.parent)
if worktree_root not in sys.path:
    sys.path.insert(0, worktree_root)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

from tools.bridge_engine import TurnCoordinator
from tools.bridge_pipeline import TurnTimer, prepare_turn_prompt
from tools.bridge_stream import AgyStreamParser


class TestBridgeEarlyAck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reply_target = MagicMock()
        self.mock_sent_msg = MagicMock(name="SentDiscordMessage")
        self.reply_target.reply = AsyncMock(return_value=self.mock_sent_msg)
        self.coordinator = TurnCoordinator(
            channel_id=123456789,
            prompt="Investigate network failure",
            mode="home",
            reply_target=self.reply_target,
            status_msg=None,
            conv_id="conv-early-ack-123",
        )

    def test_parser_captures_deferred_ack_on_step_1(self):
        """Verify preliminary finding with [[defer]] is captured before first tool call."""
        parser = AgyStreamParser(conv_id="conv-1")
        # Step 1: Agent drafts initial observation with [[defer]]
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Checking routing tables now... [[defer]]",
            },
        })
        self.assertEqual(parser.deferred_ack_text, "")

        # Tool step arrives: accumulated_segment should be inspected and captured before clear
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "run_command",
            },
        })
        self.assertEqual(parser.deferred_ack_text, "Checking routing tables now...")
        self.assertEqual(parser.accumulated_segment, [])

        # pop_deferred_ack returns and clears the token
        popped = parser.pop_deferred_ack()
        self.assertEqual(popped, "Checking routing tables now...")
        self.assertEqual(parser.deferred_ack_text, "")
        self.assertEqual(parser.pop_deferred_ack(), "")

    def test_parser_captures_deferred_ack_after_multiple_tool_steps(self):
        """Verify preliminary finding after 1-3 tool steps is captured and preserved."""
        parser = AgyStreamParser(conv_id="conv-2")

        # Step 1: Initial tool call without defer marker
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "view_file",
            },
        })
        self.assertEqual(parser.deferred_ack_text, "")

        # Step 2: Second tool call without defer marker
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "grep_search",
            },
        })
        self.assertEqual(parser.deferred_ack_text, "")

        # Step 3: Agent drafts preliminary findings after quick probe
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Found root cause candidate in auth config. Verifying against logs... [[DEFER]]",
            },
        })

        # Step 4: Heavy tool step begins
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "run_command",
            },
        })
        self.assertEqual(
            parser.deferred_ack_text,
            "Found root cause candidate in auth config. Verifying against logs...",
        )

        # Subsequent tool steps do not overwrite the captured finding
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "Running additional grep...",
            },
        })
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "grep_search",
            },
        })
        self.assertEqual(
            parser.deferred_ack_text,
            "Found root cause candidate in auth config. Verifying against logs...",
        )

        popped = parser.pop_deferred_ack()
        self.assertEqual(
            popped,
            "Found root cause candidate in auth config. Verifying against logs...",
        )
        self.assertEqual(parser.deferred_ack_text, "")

    def test_parser_captures_deferred_ack_on_top_level_tool_event(self):
        """Verify top-level ev_type in ('tool', 'tool_call', 'tool_use') triggers capture."""
        parser = AgyStreamParser(conv_id="conv-top-level")
        parser.process_event({
            "type": "agent_response",
            "step_update": {
                "step_type": "agent_response",
                "text": "Initial scan complete. [[defer]]",
            },
        })
        parser.process_event({
            "event": "tool_call",
            "tool_name": "run_command",
        })
        self.assertEqual(parser.pop_deferred_ack(), "Initial scan complete.")

    def test_parser_strips_stray_defer_token_in_final_response(self):
        """Verify get_final_response strips [[defer]] case-insensitively."""
        parser = AgyStreamParser(conv_id="conv-3")
        parser.process_event({
            "type": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "text_delta": "All tests passed cleanly and service restored. [[defer]]",
            },
        })
        final_resp = parser.get_final_response()
        self.assertEqual(final_resp, "All tests passed cleanly and service restored.")
        self.assertNotIn("[[defer]]", final_resp.lower())

    async def test_maybe_send_early_ack_disabled(self):
        """Verify early_ack_enabled=False discards token and returns False without sending."""
        parser = AgyStreamParser(conv_id="conv-4")
        parser.deferred_ack_text = "Preliminary finding to be discarded"

        sent = await self.coordinator.maybe_send_early_ack(
            stream_parser=parser,
            early_ack_enabled=False,
        )
        self.assertFalse(sent)
        self.assertFalse(self.coordinator.early_ack_sent)
        self.assertIsNone(self.coordinator.early_ack_msg)
        self.assertEqual(self.coordinator.delivery_target, self.reply_target)
        self.reply_target.reply.assert_not_called()
        # Verify parser token was discarded
        self.assertEqual(parser.pop_deferred_ack(), "")

    async def test_maybe_send_early_ack_with_mock_parser_disabled(self):
        """Verify mock parser pop_deferred_ack is called even if parser is a MagicMock."""
        mock_parser = MagicMock()
        mock_parser.pop_deferred_ack.return_value = "Mocked defer"

        sent = await self.coordinator.maybe_send_early_ack(
            stream_parser=mock_parser,
            early_ack_enabled=False,
        )
        self.assertFalse(sent)
        mock_parser.pop_deferred_ack.assert_called_once()
        self.reply_target.reply.assert_not_called()

    async def test_maybe_send_early_ack_sends_initial_finding_and_threads(self):
        """Verify maybe_send_early_ack sends finding and updates delivery_target to sent_m."""
        parser = AgyStreamParser(conv_id="conv-5")
        parser.deferred_ack_text = "Identified faulty subnet configuration. Proceeding to fix."

        sent = await self.coordinator.maybe_send_early_ack(
            stream_parser=parser,
            early_ack_enabled=True,
        )
        self.assertTrue(sent)
        self.assertTrue(self.coordinator.early_ack_sent)
        self.assertEqual(self.coordinator.early_ack_msg, self.mock_sent_msg)
        self.assertEqual(self.coordinator.delivery_target, self.mock_sent_msg)
        self.assertEqual(self.coordinator.reply_target, self.mock_sent_msg)
        self.reply_target.reply.assert_called_once()
        self.assertIn("Identified faulty subnet configuration", self.reply_target.reply.call_args[0][0])

        # Second call is idempotent and does not resend
        self.reply_target.reply.reset_mock()
        sent_again = await self.coordinator.maybe_send_early_ack(
            stream_parser=parser,
            early_ack_enabled=True,
        )
        self.assertFalse(sent_again)
        self.reply_target.reply.assert_not_called()

    async def test_maybe_send_early_ack_send_only_target(self):
        """Verify delivery target with only .send() (no .reply()) is handled cleanly."""
        send_target = MagicMock(spec=["send"])
        send_target.send = AsyncMock(return_value=self.mock_sent_msg)

        coord = TurnCoordinator(
            channel_id=987654321,
            prompt="Investigate something",
            mode="home",
            reply_target=send_target,
            status_msg=None,
            conv_id="conv-send-only",
        )
        parser = AgyStreamParser(conv_id="conv-6")
        parser.deferred_ack_text = "Preliminary finding for send-only target."

        sent = await coord.maybe_send_early_ack(
            stream_parser=parser,
            early_ack_enabled=True,
        )
        self.assertTrue(sent)
        self.assertTrue(coord.early_ack_sent)
        self.assertEqual(coord.delivery_target, self.mock_sent_msg)
        send_target.send.assert_called_once()

    async def test_maybe_send_early_ack_progress_checkin_fallback(self):
        """Verify maybe_send_early_ack returns False and sends no message when stream_parser has no deferred ack even after early_ack_seconds and >= 4 tool calls."""
        timer = TurnTimer(channel_id=123456789)
        timer.num_tools = 4

        # Simulate turn running for 200 seconds
        self.coordinator.turn_start_time = time.time() - 200.0

        # With stream_parser=None
        sent = await self.coordinator.maybe_send_early_ack(
            stream_parser=None,
            timer=timer,
            early_ack_enabled=True,
            early_ack_seconds=180.0,
        )
        self.assertFalse(sent)
        self.assertFalse(self.coordinator.early_ack_sent)
        self.assertIsNone(self.coordinator.early_ack_msg)
        self.reply_target.reply.assert_not_called()

        # With stream_parser having no deferred ack
        parser = AgyStreamParser(conv_id="conv-no-ack")
        sent_parser = await self.coordinator.maybe_send_early_ack(
            stream_parser=parser,
            timer=timer,
            early_ack_enabled=True,
            early_ack_seconds=180.0,
        )
        self.assertFalse(sent_parser)
        self.assertFalse(self.coordinator.early_ack_sent)
        self.assertIsNone(self.coordinator.early_ack_msg)
        self.reply_target.reply.assert_not_called()

    async def test_maybe_send_early_ack_defaults_to_disabled(self):
        """Verify maybe_send_early_ack defaults early_ack_enabled to False."""
        parser = AgyStreamParser(conv_id="conv-default-disabled")
        parser.deferred_ack_text = "Preliminary finding"
        sent = await self.coordinator.maybe_send_early_ack(stream_parser=parser)
        self.assertFalse(sent)
        self.assertFalse(self.coordinator.early_ack_sent)
        self.reply_target.reply.assert_not_called()
        self.assertEqual(parser.pop_deferred_ack(), "")

    def test_runtime_rules_early_ack_defaults(self):
        """Verify runtime_rules.json includes early_ack_enabled=True and early_ack_seconds=180.0."""
        rules_path = Path(worktree_root) / "config" / "runtime_rules.json"
        self.assertTrue(rules_path.exists())
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = json.load(f)
        self.assertIn("early_ack_enabled", rules)
        self.assertIn("early_ack_seconds", rules)
        self.assertIs(rules["early_ack_enabled"], True)
        self.assertEqual(rules["early_ack_seconds"], 180.0)

    def test_prepare_turn_prompt_includes_defer_guidance_when_enabled(self):
        """Verify prepare_turn_prompt includes single unified FORGE investigation guidance without redundant [[defer]] prompt instructions."""
        with patch("tools.bridge_pipeline.get_runtime_rules", return_value={"early_ack_enabled": True}):
            home_prompt = prepare_turn_prompt(
                prompt="Diagnose the redis connection timeout",
                mode="home",
                channel_id=123456789,
                sess_key="test-sess",
            )
            self.assertNotIn("[[defer]]", home_prompt)
            self.assertIn("1-3 quick initial checks", home_prompt)
            self.assertIn("forge.py job start", home_prompt)
            self.assertIn("contradiction check", home_prompt)

    def test_prepare_turn_prompt_omits_defer_guidance_when_disabled(self):
        """Verify prepare_turn_prompt omits FORGE early-ack investigation guidance when early_ack_enabled is False."""
        with patch("tools.bridge_pipeline.get_runtime_rules", return_value={"early_ack_enabled": False}):
            home_prompt = prepare_turn_prompt(
                prompt="Diagnose the redis connection timeout",
                mode="home",
                channel_id=123456789,
                sess_key="test-sess",
            )
            self.assertNotIn("[[defer]]", home_prompt)
            self.assertNotIn("1-3 quick initial checks", home_prompt)
            self.assertNotIn("forge.py job start", home_prompt)

    def test_prepare_turn_prompt_external_mode_toggling(self):
        """Verify prepare_turn_prompt toggles unified FORGE investigation guidance in external mode."""
        with patch("tools.bridge_pipeline.get_runtime_rules", return_value={"early_ack_enabled": True}):
            ext_prompt_enabled = prepare_turn_prompt(
                prompt="Review PR #456",
                mode="external",
                channel_id=987654321,
                sess_key="ext-sess",
            )
            self.assertNotIn("[[defer]]", ext_prompt_enabled)
            self.assertIn("forge.py job start", ext_prompt_enabled)

        with patch("tools.bridge_pipeline.get_runtime_rules", return_value={"early_ack_enabled": False}):
            ext_prompt_disabled = prepare_turn_prompt(
                prompt="Review PR #456",
                mode="external",
                channel_id=987654321,
                sess_key="ext-sess",
            )
            self.assertNotIn("[[defer]]", ext_prompt_disabled)
            self.assertNotIn("forge.py job start", ext_prompt_disabled)

    def test_prepare_turn_prompt_includes_detached_investigation_and_contradiction_check(self):
        """Verify prepare_turn_prompt includes forge.py job start handoff and contradiction-check guidance."""
        with patch("tools.bridge_pipeline.get_runtime_rules", return_value={"early_ack_enabled": True}):
            home_prompt = prepare_turn_prompt(
                prompt="Diagnose the redis connection timeout",
                mode="home",
                channel_id=123456789,
                sess_key="test-sess",
            )
            self.assertIn("forge.py job start", home_prompt)
            self.assertIn("contradiction check", home_prompt)

        with patch("tools.bridge_pipeline.get_runtime_rules", return_value={"early_ack_enabled": False}):
            home_prompt_disabled = prepare_turn_prompt(
                prompt="Diagnose the redis connection timeout",
                mode="home",
                channel_id=123456789,
                sess_key="test-sess",
            )
            self.assertNotIn("forge.py job start", home_prompt_disabled)
            self.assertNotIn("contradiction check", home_prompt_disabled)


if __name__ == "__main__":
    unittest.main()
