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
    def setUp(self):
        import shutil
        import tempfile
        import tools.bridge_state as bs

        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_session_meta = bs.SESSION_METADATA_FILE

        bs.DATA_DIR = self.temp_path
        bs.SESSION_METADATA_FILE = self.temp_path / "session_metadata.json"

    def tearDown(self):
        import shutil
        import tools.bridge_state as bs

        bs.DATA_DIR = self.orig_data_dir
        bs.SESSION_METADATA_FILE = self.orig_session_meta
        shutil.rmtree(self.temp_dir, ignore_errors=True)

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

    async def test_deliver_home_converts_internal_cli_leak_to_incomplete_beacon(self):
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        await deliver_turn_output(
            output_text="No tools called. Waiting for task to complete.",
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
        self.assertIn("⚠️ **Turn Incomplete:**", call_kwargs["content"])
        status_msg.delete.assert_not_called()

    async def test_deliver_home_converts_no_reply_tag_to_incomplete_beacon(self):
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        await deliver_turn_output(
            output_text="[NO_REPLY]",
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
        self.assertIn("⚠️ **Turn Incomplete:**", call_kwargs["content"])
        status_msg.delete.assert_not_called()


    async def test_deliver_external_preserves_handoff_envelope(self):
        """Verify external messages with handoff envelopes are never split into separate messages."""
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1534436119888793750, channel_name="the-banana-stand")

        # 2,150 character message ending in handoff block
        env_block = (
            "```handoff\n"
            "{\n"
            '  "v": 0,\n'
            '  "kind": "proposal",\n'
            '  "floor": "open",\n'
            '  "reply": "required",\n'
            '  "to": "Amos",\n'
            '  "subject": "agora-task53-transit-spec",\n'
            '  "round": 2\n'
            "}\n"
            "```"
        )
        body = "Paragraph of technical commentary on transit endpoints. " * 35
        full_msg = f"{body}\n\n{env_block}"
        self.assertGreater(len(full_msg), 2000)

        with patch("tools.banana.claim") as mock_claim, patch("tools.banana.release") as mock_release:
            await deliver_turn_output(
                output_text=full_msg,
                status_msg=status_msg,
                reply_target=reply_target,
                mode="external",
                channel_id=1534436119888793750,
                conv_id="conv-banana-1",
                turn_start_time=time.time(),
                timer=timer,
            )

            mock_claim.assert_called_once_with(subject="zero-external-turn")
            mock_release.assert_called_once()

        # Must deliver as exactly ONE message (status_msg edited once, reply_target not called for chunks)
        status_msg.edit.assert_awaited_once()
        delivered_content = status_msg.edit.call_args[1]["content"]
        self.assertLessEqual(len(delivered_content), 1980)
        self.assertTrue(delivered_content.strip().endswith("```"))
        self.assertIn("```handoff", delivered_content)
        self.assertIn('"subject": "agora-task53-transit-spec"', delivered_content)
        reply_target.reply.assert_not_called()
        reply_target.channel.send.assert_not_called()

    async def test_deliver_external_banana_stand_1914_chars_single_message(self):
        """Verify real-world 1,914 char banana-stand message delivers in a single message with handoff intact."""
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1534436119888793750, channel_name="the-banana-stand")

        # Reconstruct real 1914-character turn
        env_block = (
            "```handoff\n"
            "{\n"
            '  "v": 0,\n'
            '  "kind": "proposal",\n'
            '  "floor": "open",\n'
            '  "reply": "required",\n'
            '  "to": "Amos",\n'
            '  "subject": "agora-task53-transit-spec",\n'
            '  "round": 2\n'
            "}\n"
            "```"
        )
        mention_prefix = "<@1468012353206354197>\n"
        body = mention_prefix + "A" * (1914 - len(env_block) - 2 - len(mention_prefix))
        full_msg = f"{body}\n\n{env_block}"
        self.assertEqual(len(full_msg), 1914)

        with patch("tools.banana.claim"), patch("tools.banana.release"):
            await deliver_turn_output(
                output_text=full_msg,
                status_msg=status_msg,
                reply_target=reply_target,
                mode="external",
                channel_id=1534436119888793750,
                conv_id="conv-banana-1",
                turn_start_time=time.time(),
                timer=timer,
            )

        status_msg.edit.assert_awaited_once()
        delivered_content = status_msg.edit.call_args[1]["content"]
        self.assertEqual(len(delivered_content), 1914)
        self.assertIn("```handoff", delivered_content)
        reply_target.reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()

