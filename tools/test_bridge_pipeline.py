"""Unit tests for tools.bridge_pipeline (TurnTimer, prepare_turn_prompt, deliver_turn_output)."""

import asyncio
from pathlib import Path
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
from tools.bridge_pipeline import (
    TurnTimer,
    prepare_turn_prompt,
    deliver_turn_output,
    find_new_artifacts,
    extract_reply_context,
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

    def test_extract_reply_context_resolved(self):
        msg = MagicMock()
        ref = MagicMock()
        ref.message_id = 999888777
        resolved = MagicMock()
        resolved.author.display_name = "Ryan"
        resolved.author.bot = False
        resolved.clean_content = "Can you check this?"
        resolved.created_at = None
        ref.resolved = resolved
        msg.reference = ref

        ctx = extract_reply_context(msg, channel_id=123)
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["author"], "Ryan")
        self.assertEqual(ctx["content"], "Can you check this?")
        self.assertFalse(ctx["is_bot"])

    def test_extract_reply_context_channel_history_fallback(self):
        msg = MagicMock()
        ref = MagicMock()
        ref.message_id = 111222333
        ref.resolved = None
        msg.reference = ref

        with patch("tools.channel_history._history_store", {"123": [{"id": 111222333, "author": "Zero", "is_bot": True, "content": "Proposal for transit", "timestamp": "2026-09-30 20:00:00 UTC"}]}):
            ctx = extract_reply_context(msg, channel_id=123)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx["author"], "Zero")
            self.assertEqual(ctx["content"], "Proposal for transit")
            self.assertTrue(ctx["is_bot"])

    def test_prepare_turn_prompt_with_reply_home(self):
        msg = MagicMock()
        ref = MagicMock()
        ref.message_id = 999
        resolved = MagicMock()
        resolved.author.display_name = "Ryan"
        resolved.author.bot = False
        resolved.clean_content = "What broke earlier?"
        resolved.created_at = None
        ref.resolved = resolved
        msg.reference = ref

        prompt = prepare_turn_prompt(
            prompt="It was the sqlite log driver hanging DSM.",
            mode="home",
            channel_id=1542081375287640084,
            sess_key="home",
            reply_target=msg,
        )
        self.assertIn("[DISCORD MESSAGE REPLY CONTEXT]:", prompt)
        self.assertIn("Replied-To Author: Ryan", prompt)
        self.assertIn("What broke earlier?", prompt)
        self.assertIn("Implicit Thread Anchor:", prompt)

    def test_prepare_turn_prompt_with_reply_external(self):
        msg = MagicMock()
        ref = MagicMock()
        ref.message_id = 888
        resolved = MagicMock()
        resolved.author.display_name = "Dr. Coley"
        resolved.author.bot = False
        resolved.clean_content = "In English please."
        resolved.created_at = None
        ref.resolved = resolved
        msg.reference = ref

        with patch("tools.channel_history.format_channel_context", return_value="[Channel context]"):
            prompt = prepare_turn_prompt(
                prompt="The loan liquidity pipeline dried up.",
                mode="external",
                channel_id=1534452820995080192,
                sess_key="1534452820995080192",
                author_name="Ryan",
                reply_target=msg,
            )
            self.assertIn("[DISCORD MESSAGE REPLY CONTEXT]:", prompt)
            self.assertIn("Replied-To Author: Dr. Coley", prompt)
            self.assertIn("In English please.", prompt)
            self.assertIn("(replying to Dr. Coley)", prompt)


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

    async def test_deliver_external_suppresses_cli_leak(self):
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1534452820995080192, channel_name="lounge")

        leak_msg = (
            "An asynchronous background task is currently running: e660bca3-f3aa-4000-8e36-fc31186cf566/task-1269 (./game/tests/run.sh).\n"
            "Continuing silent background execution until completion."
        )

        await deliver_turn_output(
            output_text=leak_msg,
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

    async def test_deliver_home_with_nested_bracket_choices(self):
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        mock_view_cls = MagicMock()
        mock_btn_fn = MagicMock()

        await deliver_turn_output(
            output_text="Sprint updated.\n\n[CHOICES: Continue Sprint: [UI-3] Unified Plan Architecture | Continue Sprint: [UI-4] Obsidian Glass Elevation | Inspect Backlog]",
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
        self.assertIn("Sprint updated.", call_kwargs["content"])
        self.assertNotIn("[CHOICES:", call_kwargs["content"])
        self.assertNotIn("Unified Plan Architecture", call_kwargs["content"])
        self.assertIsNotNone(call_kwargs["view"])
        mock_view_cls.assert_called_once_with([
            "Continue Sprint: [UI-3] Unified Plan Architecture",
            "Continue Sprint: [UI-4] Obsidian Glass Elevation",
            "Inspect Backlog",
        ], mock_btn_fn)

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

    async def test_deliver_home_autonomous_system_turn_honors_explicit_no_reply(self):
        # Autonomous background turns (status_msg=None, reply_target=TextChannel without .reply)
        # must cleanly suppress explicit [NO_REPLY] deduplication without posting Turn Incomplete.
        channel_target = MagicMock(spec=["send", "id"])
        channel_target.id = 1542081375287640084
        channel_target.send = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        sent = await deliver_turn_output(
            output_text="[NO_REPLY]",
            status_msg=None,
            reply_target=channel_target,
            mode="home",
            channel_id=1542081375287640084,
            conv_id="conv-1",
            turn_start_time=time.time(),
            timer=timer,
        )
        self.assertEqual(sent, [])
        channel_target.send.assert_not_called()
        self.assertEqual(timer.status, "NO_REPLY")


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

    async def test_deliver_external_strips_handoff_envelope_in_non_banana_channel(self):
        """Verify external messages to non-banana channels have handoff envelopes stripped and do not claim mutex."""
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        lounge_cid = 1534452820995080192
        timer = TurnTimer(channel_id=lounge_cid, channel_name="lounge")

        env_block = (
            "```handoff\n"
            "{\n"
            '  "v": 1.1,\n'
            '  "kind": "status",\n'
            '  "floor": "open",\n'
            '  "reply": "optional",\n'
            '  "subject": "lounge-chat"\n'
            "}\n"
            "```"
        )
        body = "Clean conversational discussion in #lounge without any banana protocol clutter."
        full_msg = f"{body}\n\n{env_block}"

        with patch("tools.banana.claim") as mock_claim, patch("tools.banana.release") as mock_release:
            await deliver_turn_output(
                output_text=full_msg,
                status_msg=status_msg,
                reply_target=reply_target,
                mode="external",
                channel_id=lounge_cid,
                conv_id="conv-lounge-1",
                turn_start_time=time.time(),
                timer=timer,
            )

            # Banana mutex must NEVER be claimed outside #the-banana-stand
            mock_claim.assert_not_called()
            mock_release.assert_not_called()

        status_msg.edit.assert_awaited_once()
        delivered_content = status_msg.edit.call_args[1]["content"]
        self.assertEqual(delivered_content, body)
        self.assertNotIn("```handoff", delivered_content)
        self.assertNotIn("lounge-chat", delivered_content)

    async def test_deliver_external_lounge_2081_chars_delivers_all_chunks_no_truncation(self):
        """Verify 2,081 char message in #lounge delivers complete text across chunks without lossy truncation."""
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1534452820995080192, channel_name="lounge")

        long_paragraph = "Paragraph one explaining the pre-tool hooks and deterministic filters in detail. " * 26
        last_sentence = "If you are leveling up Aerial, search_guard and batch_guard save your sanity."
        full_msg = f"{long_paragraph}\n\n{last_sentence}"
        full_msg = full_msg[:2081]
        self.assertEqual(len(full_msg), 2081)

        await deliver_turn_output(
            output_text=full_msg,
            status_msg=status_msg,
            reply_target=reply_target,
            mode="external",
            channel_id=1534452820995080192,
            conv_id="conv-lounge-1",
            turn_start_time=time.time(),
            timer=timer,
        )

        # Status message was edited with chunk 0
        status_msg.edit.assert_awaited_once()
        chunk0 = status_msg.edit.call_args[1]["content"]
        self.assertLessEqual(len(chunk0), 1980)

        # Verify chunk 1 was dispatched to deliver the remaining text without lossy cutting
        self.assertTrue(reply_target.reply.called or reply_target.send.called)
        sent_chunk1 = (reply_target.reply.call_args or reply_target.send.call_args)[0][0]
        # Full content is preserved across chunks: chunk0 has the beginning, chunk1 has the end
        self.assertTrue(chunk0.startswith("Paragraph one"))
        self.assertTrue(full_msg.endswith(sent_chunk1.strip()))

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

    async def test_deliver_external_suppresses_no_reply_even_with_physical_mention(self):
        """Verify external [NO_REPLY] is suppressed completely and never replaced with 'Acknowledged. Standing by.'."""
        status_msg = AsyncMock()
        reply_target = AsyncMock()
        timer = TurnTimer(channel_id=1534436119888793750, channel_name="the-banana-stand")

        with patch("tools.banana.claim") as mock_claim, patch("tools.banana.release") as mock_release:
            await deliver_turn_output(
                output_text="[NO_REPLY]",
                status_msg=status_msg,
                reply_target=reply_target,
                mode="external",
                channel_id=1534436119888793750,
                conv_id="conv-banana-2",
                turn_start_time=time.time(),
                is_physical_mention=True,
                timer=timer,
            )

            mock_claim.assert_not_called()
            mock_release.assert_not_called()

        status_msg.edit.assert_not_called()
        reply_target.reply.assert_not_called()
        status_msg.delete.assert_awaited_once()




class TestAmbientMemoryPreFetch(unittest.TestCase):
    def test_get_ambient_memory_context_skips_trivial(self):
        from tools.bridge_pipeline import get_ambient_memory_context
        self.assertEqual(get_ambient_memory_context("ping"), "")
        self.assertEqual(get_ambient_memory_context("^"), "")
        self.assertEqual(get_ambient_memory_context("what?"), "")
        self.assertEqual(get_ambient_memory_context("/new"), "")

    def test_get_ambient_memory_context_home_match(self):
        from tools.bridge_pipeline import get_ambient_memory_context
        # Query with substantive terms known to exist in memory
        res = get_ambient_memory_context("Synology Docker log driver hang DSM", mode="home")
        if res:  # If index is populated
            self.assertIn("[ACTIVE MEMORY CONTEXT]:", res)
            self.assertIn("Memory Grounding Directive", res)

    def test_get_ambient_memory_context_air_gap_external(self):
        from tools.bridge_pipeline import get_ambient_memory_context
        # In external mode, private files must never be included
        res = get_ambient_memory_context("Agora game strategy and fleet allocation", mode="external")
        if res:
            self.assertNotIn("/workspace/memory/private/", res)
            self.assertNotIn("/workspace/memory/vault/", res)

    def test_prepare_turn_prompt_external_injects_ambient_memory(self):
        from tools.bridge_pipeline import prepare_turn_prompt
        prompt = prepare_turn_prompt(
            "Synology Docker log driver hang DSM",
            mode="external",
            channel_id=1534452820995080192,
            sess_key="1534452820995080192",
            author_name="Ryan",
        )
        if "[ACTIVE MEMORY CONTEXT]:" in prompt:
            mem_block = prompt.split("[ACTIVE MEMORY CONTEXT]:")[1].split("[INBOUND MESSAGE")[0]
            self.assertIn("Memory Grounding Directive", mem_block)
            self.assertNotIn("(/workspace/memory/private/", mem_block)
            self.assertNotIn("(/workspace/memory/vault/", mem_block)

    def test_get_ambient_memory_context_dual_query_coreference(self):
        from tools.bridge_pipeline import get_ambient_memory_context
        # Query with pronoun-heavy follow up + previous user prompt
        res = get_ambient_memory_context(
            "can you check the setting on that?",
            mode="home",
            previous_user_prompt="What specialty coffee brewer and grinder do we use?",
        )
        if res:
            self.assertIn("[ACTIVE MEMORY CONTEXT]:", res)
            self.assertIn("amazon_purchasing_patterns", res)


class TestBridgeEmojiReactions(unittest.IsolatedAsyncioTestCase):

    async def test_deliver_standalone_emoji_reaction(self):
        """Verify standalone [REACT: <emoji>] applies reaction, deletes status_msg, and sends no text."""
        status_msg = AsyncMock()
        status_msg.delete = AsyncMock()
        reply_target = AsyncMock()
        reply_target.add_reaction = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        res = await deliver_turn_output(
            output_text="[REACT: 👍]",
            status_msg=status_msg,
            reply_target=reply_target,
            mode="home",
            channel_id=1542081375287640084,
            conv_id="conv-react-1",
            turn_start_time=time.time(),
            timer=timer,
        )

        reply_target.add_reaction.assert_awaited_once_with("👍")
        status_msg.delete.assert_awaited_once()
        self.assertEqual(res, [])
        self.assertEqual(timer.status, "SUCCESS")

    async def test_deliver_text_with_emoji_reactions(self):
        """Verify [REACT: <emoji>] paired with text applies reaction and strips tag from deliverable."""
        status_msg = None
        reply_target = AsyncMock()
        reply_target.add_reaction = AsyncMock()
        reply_target.reply = AsyncMock()
        timer = TurnTimer(channel_id=1542081375287640084, channel_name="zero-chat")

        res = await deliver_turn_output(
            output_text="[REACT: 🎯, 🚀]\nPipeline tests are passing cleanly!",
            status_msg=status_msg,
            reply_target=reply_target,
            mode="home",
            channel_id=1542081375287640084,
            conv_id="conv-react-2",
            turn_start_time=time.time(),
            timer=timer,
        )

        self.assertEqual(reply_target.add_reaction.call_count, 2)
        reply_target.add_reaction.assert_any_await("🎯")
        reply_target.add_reaction.assert_any_await("🚀")
        reply_target.reply.assert_awaited_once()
        delivered_content = reply_target.reply.call_args[0][0]
        self.assertIn("Pipeline tests are passing cleanly!", delivered_content)
        self.assertNotIn("[REACT", delivered_content)
        self.assertEqual(timer.status, "SUCCESS")


if __name__ == "__main__":
    unittest.main()

