"""Unit tests for tools.bridge_pipeline (TurnTimer, prepare_turn_prompt, deliver_turn_output)."""

import asyncio
from pathlib import Path
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.bridge_pipeline import (
    TurnTimer,
    prepare_turn_prompt,
    deliver_turn_output,
    find_new_artifacts,
)


class TestTurnTimer(unittest.TestCase):
    def test_milestone_tracking_and_summary(self):
        t_queued = time.perf_counter() - 0.50  # 500ms in queue
        timer = TurnTimer(
            channel_id=1542081375287640084,
            channel_name="zero-chat",
            queued_at=t_queued,
        )

        # Preflight
        timer.mark_compaction_start()
        time.sleep(0.01)
        timer.mark_compaction_end()

        timer.mark_ctx_start()
        time.sleep(0.01)
        timer.mark_ctx_end()

        timer.mark_boot_start()
        time.sleep(0.02)
        timer.mark_boot_end()

        # Send
        timer.mark_send()
        time.sleep(0.02)

        # First event (tool)
        timer.mark_event(tool_name="view_file")
        time.sleep(0.01)

        # First token
        timer.mark_event(is_token=True)
        time.sleep(0.01)

        # Second tool
        timer.mark_event(tool_name="replace_file_content")

        # Result
        timer.mark_result()

        # Delivery
        timer.mark_delivery_start()
        time.sleep(0.01)
        timer.mark_delivery_end()

        summary = timer.finish("SUCCESS")

        self.assertIn("[BridgeTimer:#zero-chat]", summary)
        self.assertIn("queue:", summary)
        self.assertIn("preflight:", summary)
        self.assertIn("ttft:", summary)
        self.assertIn("model:", summary)
        self.assertIn("2 tools (view_file, replace_file_content)", summary)
        self.assertIn("delivery:", summary)
        self.assertEqual(timer.status, "SUCCESS")

    def test_timer_error_status_preservation(self):
        timer = TurnTimer(channel_id=123, channel_name="test-ch")
        timer.mark_send()
        timer.mark_delivery_start()
        timer.mark_delivery_end()
        summary = timer.finish("TIMEOUT")
        self.assertIn("[TIMEOUT]", summary)
        self.assertEqual(timer.status, "TIMEOUT")


class TestBridgePipelinePrompt(unittest.TestCase):
    def test_prepare_turn_prompt_home(self):
        prompt = prepare_turn_prompt(
            prompt="Hello Zero",
            mode="home",
            channel_id=1542081375287640084,
            sess_key="home",
        )
        self.assertIn("[System Time & Timezone]:", prompt)
        self.assertIn("Pacific Time (PT)", prompt)
        self.assertIn("Hello Zero", prompt)

    def test_prepare_turn_prompt_external(self):
        with patch("tools.channel_history.format_channel_context", return_value="[Amos]: Hi"):
            prompt = prepare_turn_prompt(
                prompt="How are you?",
                mode="external",
                channel_id=1534452820995080192,
                sess_key="1534452820995080192",
                author_name="Alex",
            )
            self.assertIn("How are you?", prompt)
            self.assertIn("Pacific Time (PT)", prompt)


class TestBridgePipelineDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_deliver_external_no_reply(self):
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1534452820995080192, channel_name="lounge")

        await deliver_turn_output(
            output_text="[NO_REPLY]",
            status_msg=status_msg,
            reply_target=reply_target,
            mode="external",
            channel_id=1534452820995080192,
            conv_id="conv-1",
            turn_start_time=time.time(),
            timer=timer,
        )

        status_msg.delete.assert_awaited_once()
        reply_target.reply.assert_not_called()
        self.assertEqual(timer.status, "NO_REPLY")

    async def test_deliver_home_with_choices(self):
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        mock_view_cls = MagicMock()
        mock_btn_fn = MagicMock()

        await deliver_turn_output(
            output_text="Deploy completed. [CHOICES: Reload Bridge In-Place | Postpone]",
            status_msg=status_msg,
            reply_target=reply_target,
            mode="home",
            channel_id=1542081375287640084,
            conv_id="conv-1",
            turn_start_time=time.time(),
            button_choice_fn=mock_btn_fn,
            quick_choice_view_cls=mock_view_cls,
            timer=timer,
        )

        status_msg.edit.assert_awaited_once()
        call_kwargs = status_msg.edit.call_args[1]
        self.assertIn("Deploy completed.", call_kwargs["content"])
        self.assertNotIn("[CHOICES:", call_kwargs["content"])
        self.assertIsNotNone(call_kwargs["view"])
        self.assertEqual(timer.status, "SUCCESS")

    async def test_deliver_home_suppresses_reload_choices_when_flag_armed(self):
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        mock_view_cls = MagicMock()
        mock_btn_fn = MagicMock()

        # Simulate armed reload flag on disk
        flag_path = Path("/workspace/data/reload_bridge.flag")
        flag_path.write_text("1")
        try:
            await deliver_turn_output(
                output_text="Deploy finished and armed. [CHOICES: Reload Bridge In-Place | reload now]",
                status_msg=status_msg,
                reply_target=reply_target,
                mode="home",
                channel_id=1542081375287640084,
                conv_id="conv-1",
                turn_start_time=time.time(),
                button_choice_fn=mock_btn_fn,
                quick_choice_view_cls=mock_view_cls,
                timer=timer,
            )

            status_msg.edit.assert_awaited_once()
            call_kwargs = status_msg.edit.call_args[1]
            self.assertIn("Deploy finished and armed.", call_kwargs["content"])
            self.assertNotIn("[CHOICES:", call_kwargs["content"])
            # view should be None because all choices were reload choices and flag was armed
            self.assertIsNone(call_kwargs.get("view"))
            mock_view_cls.assert_not_called()
        finally:
            if flag_path.exists():
                flag_path.unlink()

    async def test_deliver_home_replaces_no_output_placeholder(self):
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        await deliver_turn_output(
            output_text="*(No output from agent)*",
            status_msg=status_msg,
            reply_target=reply_target,
            mode="home",
            channel_id=1542081375287640084,
            conv_id="conv-1",
            turn_start_time=time.time(),
            timer=timer,
        )

        status_msg.edit.assert_awaited_once()
        call_kwargs = status_msg.edit.call_args[1]
        self.assertIn("⚠️ **Turn Incomplete:** Agent process completed turn without generating text output.", call_kwargs["content"])


if __name__ == "__main__":
    unittest.main()
