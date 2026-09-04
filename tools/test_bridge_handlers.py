#!/usr/bin/env python3
"""
Unit test suite for bridge_handlers.py (Discord Bot Event Handlers, Routing & Dispatch).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import tools.bridge_handlers as bh
import tools.bridge_state as bs
import tools.bridge_runner as br


class TestBridgeHandlers(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_attachments_dir = bh.ATTACHMENTS_DIR
        self.orig_in_flight = bs.IN_FLIGHT_FILE
        self.orig_restart_intent = bs.RESTART_INTENT_FILE

        import tools.channel_history as ch
        self.orig_history_file = ch.CHANNEL_HISTORY_FILE
        self.orig_history_store = {k: v.copy() for k, v in ch._history_store.items()}
        ch.CHANNEL_HISTORY_FILE = self.temp_path / "channel_history.json"
        ch._history_store.clear()

        bs.DATA_DIR = self.temp_path
        bh.ATTACHMENTS_DIR = self.temp_path / "attachments"
        bh.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        bs.IN_FLIGHT_FILE = self.temp_path / "in_flight_turn.json"
        bh.IN_FLIGHT_FILE = self.temp_path / "in_flight_turn.json"
        bs.RESTART_INTENT_FILE = self.temp_path / "restart_intent.json"

    def tearDown(self):
        import tools.channel_history as ch
        ch.CHANNEL_HISTORY_FILE = self.orig_history_file
        ch._history_store = self.orig_history_store

        bs.DATA_DIR = self.orig_data_dir
        bh.ATTACHMENTS_DIR = self.orig_attachments_dir
        bs.IN_FLIGHT_FILE = self.orig_in_flight
        bh.IN_FLIGHT_FILE = self.orig_in_flight
        bs.RESTART_INTENT_FILE = self.orig_restart_intent
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_choice_button_and_view(self):
        options = ["Option A", "Option B", "Option C"]
        view = bh.QuickChoiceView(options)
        self.assertEqual(len(view.children), 3)
        self.assertEqual(view.children[0].label, "Option A")
        self.assertEqual(view.children[0].custom_id, "choice:Option A")

    def test_is_bridge_busy_detection(self):
        mock_home = MagicMock()
        mock_home.empty.return_value = True
        mock_ext = MagicMock()
        mock_ext.empty.return_value = True

        br.active_proc = None
        br.ext_active_proc = None
        busy = bh.is_bridge_busy(mock_home, mock_ext)
        self.assertEqual(busy, [])

        mock_proc = MagicMock()
        mock_proc.returncode = None
        br.active_proc = mock_proc
        busy = bh.is_bridge_busy(mock_home, mock_ext)
        self.assertIn("#zero-chat", busy)

        br.active_proc = None
        br.ext_active_proc = mock_proc
        busy = bh.is_bridge_busy(mock_home, mock_ext)
        self.assertIn("Crab Cavern", busy)
        br.ext_active_proc = None

    async def test_handle_button_choice_reload_interception(self):
        mock_interaction = MagicMock()
        mock_interaction.id = 12345
        mock_interaction.user.display_name = "Ryan"
        mock_interaction.channel.send = AsyncMock()

        reload_mock = AsyncMock()
        turn_queue = AsyncMock()

        await bh.handle_button_choice("reload bridge in-place", mock_interaction, turn_queue, reload_fn=reload_mock)
        reload_mock.assert_awaited_once()
        turn_queue.put.assert_not_awaited()

    async def test_handle_button_choice_turn_enqueue(self):
        mock_interaction = MagicMock()
        mock_interaction.id = 99999
        mock_interaction.channel_id = 1542081375287640084
        mock_interaction.channel.typing = MagicMock()
        mock_msg = MagicMock()
        mock_interaction.channel.send = AsyncMock(return_value=mock_msg)

        turn_queue = AsyncMock()
        await bh.handle_button_choice("Run diagnostic sweep", mock_interaction, turn_queue, reload_fn=None)
        turn_queue.put.assert_awaited_once()
        item = turn_queue.put.call_args[0][0]
        self.assertEqual(item["prompt"], "Run diagnostic sweep")
        self.assertEqual(item["mode"], "home")

    async def test_robot_tag_addressing(self):
        # Verify classifier recognizes @robot and role tag
        from tools.classifier import ZERO_TAGS
        import re
        self.assertTrue(any(re.search(p, "hey @robot can you check this?", re.I) for p in ZERO_TAGS))
        self.assertTrue(any(re.search(p, "hey <@&1542294519914037341> what is up?", re.I) for p in ZERO_TAGS))
        self.assertTrue(any(re.search(p, "@robot status report", re.I) for p in ZERO_TAGS))

        # Verify message routing treats @robot and role tag as addressed
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        # Message in Crab Cavern with @robot
        mock_msg = MagicMock()
        mock_msg.channel.id = 1534436119888793750  # the-banana-stand (formerly agent-chat)
        mock_msg.channel.name = "the-banana-stand"
        mock_msg.author.id = 1210466877835313155
        mock_msg.author.bot = False
        mock_msg.author.display_name = "Arbiter"
        mock_msg.content = "@robot what are the system specs?"
        mock_msg.created_at.timestamp.return_value = time.time()
        mock_msg.role_mentions = []
        mock_msg.mentions = []
        mock_msg.reference = None

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"ambient_classifier_enabled": False}):
            await bh.handle_message(mock_msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()
            call_args = turn_queue.put.call_args[0][0]
            self.assertIn("what are the system specs?", call_args["prompt"])
            self.assertEqual(call_args["mode"], "external")

        # Message in Crab Cavern with role tag
        mock_msg2 = MagicMock()
        mock_msg2.channel.id = 1534436119888793750  # the-banana-stand (formerly agent-chat)
        mock_msg2.channel.name = "the-banana-stand"
        mock_msg2.author.id = 1210466877835313155
        mock_msg2.author.bot = False
        mock_msg2.author.display_name = "Arbiter"
        mock_msg2.content = "<@&1542294519914037341> what are the system specs?"
        mock_msg2.created_at.timestamp.return_value = time.time()
        mock_msg2.role_mentions = []
        mock_msg2.mentions = []
        mock_msg2.reference = None

        turn_queue2 = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"ambient_classifier_enabled": False}):
            await bh.handle_message(mock_msg2, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue2)
            turn_queue2.put.assert_awaited_once()
            call_args2 = turn_queue2.put.call_args[0][0]
            self.assertIn("what are the system specs?", call_args2["prompt"])
            self.assertEqual(call_args2["mode"], "external")

    async def test_conversational_follow_up_without_direct_tag(self):
        """Verify that an untagged human reply following Zero's question is routed as a conversational follow-up."""
        from datetime import datetime, timezone
        from tools.channel_history import record_message

        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        ch_id = 1534436119888793750
        now = time.time()
        past_ts = datetime.fromtimestamp(now - 30, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Zero previously asked a question
        record_message(
            channel_id=ch_id,
            channel_name="the-banana-stand",
            author_name="Zero",
            is_bot=True,
            content="Want me to push the 0.80 threshold bump and #lounge mention-gate to the branch next?",
            msg_id=1111111111,
            timestamp=past_ts
        )

        # Ryan replies without explicit @Zero tag
        mock_msg = MagicMock()
        mock_msg.id = 2222222222
        mock_msg.channel.id = ch_id
        mock_msg.channel.name = "the-banana-stand"
        mock_msg.author.id = 1210466877835313155
        mock_msg.author.bot = False
        mock_msg.author.display_name = "Ryan"
        mock_msg.content = "Yep update the draft. Then pass it to Amos for review"
        mock_msg.created_at.timestamp.return_value = now
        mock_msg.role_mentions = []
        mock_msg.mentions = []
        mock_msg.reference = None

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"ambient_classifier_enabled": False}):
            await bh.handle_message(mock_msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()
            call_args = turn_queue.put.call_args[0][0]
            self.assertEqual(call_args["prompt"], "Yep update the draft. Then pass it to Amos for review")
            self.assertEqual(call_args["mode"], "external")

    def test_peer_address_vs_referential_mentions(self):
        """Verify is_explicitly_addressed_to_other distinguishes addressees from references."""
        from tools.classifier import is_explicitly_addressed_to_other

        # Prepositional / referential mentions should NOT count as addressed to peer
        self.assertFalse(is_explicitly_addressed_to_other("Yep update the draft. Then pass it to Amos for review"))
        self.assertFalse(is_explicitly_addressed_to_other("Can we check with Amos?"))
        self.assertFalse(is_explicitly_addressed_to_other("What does Amos think about rate limits?"))
        self.assertFalse(is_explicitly_addressed_to_other("Did Ian approve the PR?"))
        self.assertFalse(is_explicitly_addressed_to_other("Let's review Marvin's logs"))

        # Actual addressees should count as True
        self.assertTrue(is_explicitly_addressed_to_other("@amos can you check this?"))
        self.assertTrue(is_explicitly_addressed_to_other("<@1468012353206354197> your turn"))
        self.assertTrue(is_explicitly_addressed_to_other("Amos: what do you think?"))
        self.assertTrue(is_explicitly_addressed_to_other("Hey Marvin, thoughts?"))
        self.assertTrue(is_explicitly_addressed_to_other("Marvin check this out"))

    async def test_home_turf_channel_routing_without_mention(self):
        """Verify that channels in DEFAULT_HOME_CHANNELS (e.g. steam-deck) are routed to home_turn_queue without @Zero."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        mock_msg = MagicMock()
        mock_msg.id = 3333333333
        mock_msg.channel.id = 1544953277592899615  # #steam-deck
        mock_msg.channel.name = "steam-deck"
        mock_msg.author.id = 1210466877835313155
        mock_msg.author.bot = False
        mock_msg.author.display_name = "Ryan"
        mock_msg.content = "Done now?"
        mock_msg.created_at.timestamp.return_value = time.time()
        mock_msg.role_mentions = []
        mock_msg.mentions = []
        mock_msg.reference = None

        home_queue = AsyncMock()
        ext_queue = AsyncMock()
        await bh.handle_message(mock_msg, mock_bot, home_turn_queue=home_queue, ext_turn_queue=ext_queue)
        home_queue.put.assert_awaited_once()
        call_args = home_queue.put.call_args[0][0]
        self.assertEqual(call_args["prompt"], "Done now?")
        self.assertEqual(call_args["mode"], "home")
        self.assertEqual(call_args["channel_id"], 1544953277592899615)
        ext_queue.put.assert_not_awaited()

    async def test_dedicated_operations_channels_route_to_home(self):
        """Verify that #finances, #homelab, and #shopping are routed to home_turn_queue without @Zero."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        for cid, cname in [
            (1544955532765560924, "finances"),
            (1544955535722545253, "homelab"),
            (1544955538033348618, "shopping")
        ]:
            mock_msg = MagicMock()
            mock_msg.id = 7777777777
            mock_msg.channel.id = cid
            mock_msg.channel.name = cname
            mock_msg.channel.category_id = 1544953274363412533
            mock_msg.author.id = 1210466877835313155
            mock_msg.author.bot = False
            mock_msg.author.display_name = "Ryan"
            mock_msg.content = f"Status update in #{cname}?"
            mock_msg.created_at.timestamp.return_value = time.time()
            mock_msg.role_mentions = []
            mock_msg.mentions = []
            mock_msg.reference = None

            home_queue = AsyncMock()
            ext_queue = AsyncMock()
            await bh.handle_message(mock_msg, mock_bot, home_turn_queue=home_queue, ext_turn_queue=ext_queue)
            home_queue.put.assert_awaited_once()
            call_args = home_queue.put.call_args[0][0]
            self.assertEqual(call_args["mode"], "home")
            self.assertEqual(call_args["channel_id"], cid)
            ext_queue.put.assert_not_awaited()

    async def test_conversational_follow_up_direct_query(self):
        """Verify that short questions like 'Done now?' are recognized as conversational follow-ups in external channels."""
        from datetime import datetime, timezone
        from tools.channel_history import record_message

        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        ch_id = 1534436119888793750  # external channel
        now = time.time()
        past_ts = datetime.fromtimestamp(now - 45, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Zero spoke 45 seconds ago (without ending with a ?)
        record_message(
            channel_id=ch_id,
            channel_name="the-banana-stand",
            author_name="Zero",
            is_bot=True,
            content="I'll ping you the second Ratchet & Clank finishes flushing to flash so you can pull the trigger.",
            msg_id=4444444444,
            timestamp=past_ts
        )

        mock_msg = MagicMock()
        mock_msg.id = 5555555555
        mock_msg.channel.id = ch_id
        mock_msg.channel.name = "the-banana-stand"
        mock_msg.author.id = 1210466877835313155
        mock_msg.author.bot = False
        mock_msg.author.display_name = "Ryan"
        mock_msg.content = "Done now?"
        mock_msg.created_at.timestamp.return_value = now
        mock_msg.role_mentions = []
        mock_msg.mentions = []
        mock_msg.reference = None

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"ambient_classifier_enabled": False}):
            await bh.handle_message(mock_msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()
            call_args = turn_queue.put.call_args[0][0]
            self.assertEqual(call_args["prompt"], "Done now?")
            self.assertEqual(call_args["mode"], "external")

    def test_slash_commands_registration(self):
        """Verify that native Discord Slash Commands are registered in bot.tree."""
        from tools.bridge import bot
        cmd_names = {cmd.name for cmd in bot.tree.get_commands()}
        expected_commands = {"new", "reset", "model", "logs", "triage", "heartbeat", "tasks", "sidecars", "title"}
        for exp in expected_commands:
            self.assertIn(exp, cmd_names, f"Expected slash command '/{exp}' to be registered on bot.tree")

    async def test_in_flight_retry_circuit_breaker_on_ready(self):
        """Verify that repeated in-flight failures (attempts >= 2) trigger the hang circuit breaker on startup."""
        in_flight_data = {
            "channel_id": 12345,
            "status_msg_id": 99999,
            "prompt": "Hang prompt",
            "attempts": 2
        }
        with open(bs.IN_FLIGHT_FILE, "w") as f:
            json.dump(in_flight_data, f)

        mock_msg = AsyncMock()
        mock_channel = MagicMock()
        mock_channel.fetch_message = AsyncMock(return_value=mock_msg)

        mock_bot = MagicMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.tree.sync = AsyncMock()

        # Call handle_on_ready with mocked background helpers
        with patch("tools.mcp_daemon.ensure_mcp_daemon_running"), \
             patch("tools.zero_mail_listener.ensure_mail_listener_running"), \
             patch("tools.zero_health_server.ensure_health_server_running"), \
             patch("tools.bridge_daemons.daemon_manager.start_all"):
            await bh.handle_on_ready(
                mock_bot,
                turn_queue=AsyncMock(),
                ext_turn_queue=AsyncMock(),
                start_workers_fn=MagicMock(),
                start_scheduler_fn=AsyncMock()
            )

        # Assert status message was updated with circuit breaker warning
        mock_msg.edit.assert_awaited_once()
        edit_content = mock_msg.edit.call_args[1].get("content") or mock_msg.edit.call_args[0][0]
        self.assertIn("prevent an execution loop", edit_content)
        self.assertFalse(bs.IN_FLIGHT_FILE.exists())

    async def test_live_status_ticker_placeholder_suppression(self):
        """Verify that synthetic status placeholders are suppressed when live_status_ticker_enabled is False."""
        mock_target = AsyncMock()
        mock_target.reply = AsyncMock()
        mock_target.channel = MagicMock()
        mock_target.channel.typing = MagicMock()
        mock_target.typing = MagicMock()

        item = {
            "prompt": "Test ops command",
            "status_msg": None,
            "reply_target": mock_target,
            "attachments": [],
            "channel_id": 1544953279664889888, # #zero-ops
            "mode": "home"
        }

        # 1. Thread worker: ticker disabled -> no reply placeholder
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"live_status_ticker_enabled": False}), \
             patch("tools.bridge_handlers.execute_agy_turn") as mock_exec:
            await bh.run_thread_turn_worker(item, MagicMock())
            mock_target.reply.assert_not_awaited()
            mock_exec.assert_awaited_once()
            self.assertIsNone(mock_exec.call_args[0][1])  # status_msg is None

        # 2. Thread worker: ticker enabled -> reply placeholder spawned
        mock_target.reply.reset_mock()
        mock_placeholder = AsyncMock()
        mock_target.reply.return_value = mock_placeholder
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"live_status_ticker_enabled": True}), \
             patch("tools.bridge_handlers.execute_agy_turn") as mock_exec:
            await bh.run_thread_turn_worker(item, MagicMock())
            mock_target.reply.assert_awaited_once_with("⏳ *Processing task...*")
            mock_exec.assert_awaited_once()
            self.assertEqual(mock_exec.call_args[0][1], mock_placeholder)

    async def test_channel_tag_requirements_plain_text_name_mention(self):
        """Verify that mentioning 'Zero' in plain text passes channel-specific tag gating (e.g. in #lounge)."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        lounge_id = 1534452820995080192
        now = time.time()

        # 1. Plain text mention of "Zero" in #lounge -> should be accepted
        msg_named = MagicMock()
        msg_named.id = 100001
        msg_named.channel.id = lounge_id
        msg_named.channel.name = "lounge"
        msg_named.author.id = 1210466877294518272
        msg_named.author.bot = False
        msg_named.author.display_name = "Ryan"
        msg_named.content = "Zero, what do you think of this?"
        msg_named.created_at.timestamp.return_value = now
        msg_named.role_mentions = []
        msg_named.mentions = []
        msg_named.reference = None

        turn_queue = AsyncMock()
        rules = {
            "channel_tag_requirements": {str(lounge_id): "1543285916506783799"},
            "ambient_classifier_enabled": False
        }
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg_named, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()
            call_args = turn_queue.put.call_args[0][0]
            self.assertEqual(call_args["prompt"], "what do you think of this?")

        # 2. Unaddressed message without "Zero" or role tag in #lounge -> should be ignored
        msg_unaddressed = MagicMock()
        msg_unaddressed.id = 100002
        msg_unaddressed.channel.id = lounge_id
        msg_unaddressed.channel.name = "lounge"
        msg_unaddressed.author.id = 1210466877294518272
        msg_unaddressed.author.bot = False
        msg_unaddressed.author.display_name = "Ryan"
        msg_unaddressed.content = "Just general ambient chatter in lounge."
        msg_unaddressed.created_at.timestamp.return_value = now
        msg_unaddressed.role_mentions = []
        msg_unaddressed.mentions = []
        msg_unaddressed.reference = None

        turn_queue_ignored = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg_unaddressed, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue_ignored)
            turn_queue_ignored.put.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

