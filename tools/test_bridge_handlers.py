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

    @patch("tools.bridge_handlers.execute_container_restart", new_callable=AsyncMock)
    async def test_handle_button_choice_container_restart_interception(self, mock_container_restart):
        mock_interaction = MagicMock()
        mock_interaction.id = 54321
        mock_interaction.user.display_name = "Ryan"
        mock_interaction.channel.send = AsyncMock()

        turn_queue = AsyncMock()

        await bh.handle_button_choice("Restart Docker Container", mock_interaction, turn_queue, reload_fn=None)
        mock_container_restart.assert_awaited_once()
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

    async def test_zero_tag_no_false_positives(self):
        """Verify that common English usages like 'zero wrapping' do not trigger Zero mentions."""
        from tools.classifier import ZERO_TAGS
        import re

        # Common English phrases that should NOT match ZERO_TAGS
        false_positive_phrases = [
            "single line with zero wrapping or vertical clipping",
            "zero layout shift (CLS = 0)",
            "we have zero tolerance for bugs",
            "reduced to zero",
            "from zero to one"
        ]
        for phrase in false_positive_phrases:
            self.assertFalse(
                any(re.search(p, phrase, re.I) for p in ZERO_TAGS),
                f"Phrase '{phrase}' incorrectly matched ZERO_TAGS"
            )

        # Legitimate mentions that SHOULD match
        legit_mentions = [
            "@zero can you check this?",
            "hey zero, what's up?",
            "Zero: please run tests",
            "Zero, look at this",
            "hello zero",
            "thanks @Zero"
        ]
        for phrase in legit_mentions:
            self.assertTrue(
                any(re.search(p, phrase, re.I) for p in ZERO_TAGS),
                f"Legit mention '{phrase}' failed to match ZERO_TAGS"
            )

    async def test_lazy_typer_minimal_role_mention(self):
        """Verify that minimal role mentions (e.g. <@&1542294519914037341> ^) trigger lazy typer directive."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        mock_msg = MagicMock()
        mock_msg.channel.id = 1534436119888793750  # the-banana-stand
        mock_msg.channel.name = "the-banana-stand"
        mock_msg.author.id = 1210466877835313155
        mock_msg.author.bot = False
        mock_msg.author.display_name = "Ryan"
        mock_msg.content = "<@&1542294519914037341> ^"
        mock_msg.created_at.timestamp.return_value = time.time()
        mock_msg.role_mentions = []
        mock_msg.mentions = []
        mock_msg.attachments = []
        mock_msg.reference = None

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"ambient_classifier_enabled": False}):
            await bh.handle_message(mock_msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()
            call_args = turn_queue.put.call_args[0][0]
            self.assertIn("[OPERATIONAL DIRECTIVE - LAZY TYPER ADDRESSING]", call_args["prompt"])
            self.assertIn("Humans are lazy typers", call_args["prompt"])

    async def test_unprompted_image_attachment_handling(self):
        """Verify that image attachments without text mention inject the image input invariant."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        mock_att = AsyncMock()
        mock_att.filename = "terminal_error.png"
        mock_att.save = AsyncMock()

        mock_msg = MagicMock()
        mock_msg.channel.id = 1534436119888793750
        mock_msg.channel.name = "the-banana-stand"
        mock_msg.author.id = 1210466877835313155
        mock_msg.author.bot = False
        mock_msg.author.display_name = "Ryan"
        mock_msg.content = "<@&1542294519914037341>"
        mock_msg.created_at.timestamp.return_value = time.time()
        mock_msg.role_mentions = []
        mock_msg.mentions = []
        mock_msg.attachments = [mock_att]
        mock_msg.reference = None

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"ambient_classifier_enabled": False}):
            await bh.handle_message(mock_msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()
            call_args = turn_queue.put.call_args[0][0]
            self.assertIn("[CRITICAL IMAGE INPUT INVARIANT]", call_args["prompt"])
            self.assertIn("view_file", call_args["prompt"])
            self.assertIn("terminal_error.png", call_args["prompt"])

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
        self.assertTrue(is_explicitly_addressed_to_other("<@1542035925603713086> status?"))
        self.assertTrue(is_explicitly_addressed_to_other("Aerial: what do you think?"))
        self.assertTrue(is_explicitly_addressed_to_other("Hey Aerial, did you see this?"))
        self.assertTrue(is_explicitly_addressed_to_other("Amos: what do you think?"))
        self.assertTrue(is_explicitly_addressed_to_other("Hey Marvin, thoughts?"))
        self.assertTrue(is_explicitly_addressed_to_other("Marvin check this out"))
        self.assertTrue(is_explicitly_addressed_to_other("Alex check this out"))

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

    async def test_channel_tag_requirements_plain_text_name_ignored(self):
        """Verify that mentioning 'Zero' in plain text is ignored under channel-specific tag gating (e.g. in #lounge)."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        lounge_id = 1534452820995080192
        now = time.time()

        # 1. Plain text mention of "Zero" in #lounge without role tag -> should be ignored
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
            turn_queue.put.assert_not_awaited()

        # 2. Tagged with required role -> should be accepted
        msg_tagged = MagicMock()
        msg_tagged.id = 100002
        msg_tagged.channel.id = lounge_id
        msg_tagged.channel.name = "lounge"
        msg_tagged.author.id = 1210466877294518272
        msg_tagged.author.bot = False
        msg_tagged.author.display_name = "Ryan"
        msg_tagged.content = "<@&1543285916506783799> what do you think of this?"
        msg_tagged.created_at.timestamp.return_value = now
        role_mock = MagicMock()
        role_mock.id = 1543285916506783799
        msg_tagged.role_mentions = [role_mock]
        msg_tagged.mentions = []
        msg_tagged.reference = None

        turn_queue_tagged = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg_tagged, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue_tagged)
            turn_queue_tagged.put.assert_awaited_once()

        # 3. Unaddressed message without "Zero" or role tag in #lounge -> should be ignored
        msg_unaddressed = MagicMock()
        msg_unaddressed.id = 100003
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

        # 4. Casual sentence containing noun "robot" in #lounge -> should be ignored
        msg_casual_robot = MagicMock()
        msg_casual_robot.id = 100004
        msg_casual_robot.channel.id = lounge_id
        msg_casual_robot.channel.name = "lounge"
        msg_casual_robot.author.id = 1210466877294518272
        msg_casual_robot.author.bot = False
        msg_casual_robot.author.display_name = "Ryan"
        msg_casual_robot.content = "I don't know if either robot really knows what is going on."
        msg_casual_robot.created_at.timestamp.return_value = now
        msg_casual_robot.role_mentions = []
        msg_casual_robot.mentions = []
        msg_casual_robot.reference = None

        turn_queue_casual = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg_casual_robot, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue_casual)
            turn_queue_casual.put.assert_not_awaited()

        # 5. Explicit @robot tag in #lounge -> should be accepted
        msg_explicit_robot = MagicMock()
        msg_explicit_robot.id = 100005
        msg_explicit_robot.channel.id = lounge_id
        msg_explicit_robot.channel.name = "lounge"
        msg_explicit_robot.author.id = 1210466877294518272
        msg_explicit_robot.author.bot = False
        msg_explicit_robot.author.display_name = "Ryan"
        msg_explicit_robot.content = "@robot what is the latest status?"
        msg_explicit_robot.created_at.timestamp.return_value = now
        msg_explicit_robot.role_mentions = []
        msg_explicit_robot.mentions = []
        msg_explicit_robot.reference = None

        turn_queue_explicit = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg_explicit_robot, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue_explicit)
            turn_queue_explicit.put.assert_awaited_once()

        # 6. Explicit team role tag <@&1543462881624858624> in #lounge -> should be accepted
        msg_team_role = MagicMock()
        msg_team_role.id = 100006
        msg_team_role.channel.id = lounge_id
        msg_team_role.channel.name = "lounge"
        msg_team_role.author.id = 1210466877294518272
        msg_team_role.author.bot = False
        msg_team_role.author.display_name = "Ryan"
        msg_team_role.content = "<@&1543462881624858624> team, it's time for our art competition"
        msg_team_role.created_at.timestamp.return_value = now
        team_role_mock = MagicMock()
        team_role_mock.id = 1543462881624858624
        msg_team_role.role_mentions = [team_role_mock]
        msg_team_role.mentions = []
        msg_team_role.reference = None

        turn_queue_team = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg_team_role, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue_team)
            turn_queue_team.put.assert_awaited_once()
            call_args = turn_queue_team.put.call_args[0][0]
            self.assertEqual(call_args["prompt"], "team, it's time for our art competition")

        # 7. Explicit @team in #lounge -> should be accepted
        msg_team_word = MagicMock()
        msg_team_word.id = 100007
        msg_team_word.channel.id = lounge_id
        msg_team_word.channel.name = "lounge"
        msg_team_word.author.id = 1210466877294518272
        msg_team_word.author.bot = False
        msg_team_word.author.display_name = "Ryan"
        msg_team_word.content = "@team check this out"
        msg_team_word.created_at.timestamp.return_value = now
        msg_team_word.role_mentions = []
        msg_team_word.mentions = []
        msg_team_word.reference = None

        turn_queue_word = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg_team_word, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue_word)
            turn_queue_word.put.assert_awaited_once()

        # 8. Inline team role mention preserved rather than stripped to empty string
        msg_inline_team = MagicMock()
        msg_inline_team.id = 100008
        msg_inline_team.channel.id = lounge_id
        msg_inline_team.channel.name = "lounge"
        msg_inline_team.author.id = 1210466877294518272
        msg_inline_team.author.bot = False
        msg_inline_team.author.display_name = "Ryan"
        msg_inline_team.content = "fix your responsiveness to the <@&1543462881624858624> tag"
        msg_inline_team.created_at.timestamp.return_value = now
        msg_inline_team.role_mentions = [team_role_mock]
        msg_inline_team.mentions = []
        msg_inline_team.reference = None

        turn_queue_inline = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg_inline_team, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue_inline)
            turn_queue_inline.put.assert_awaited_once()
            call_args = turn_queue_inline.put.call_args[0][0]
            self.assertEqual(call_args["prompt"], "fix your responsiveness to the @team tag")

    async def test_multi_mention_preserves_zero_and_peer_names(self):
        """Verify that multi-recipient mentions preserve @Zero and resolve peer bot IDs to names."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        lounge_id = 1534452820995080192
        now = time.time()

        msg = MagicMock()
        msg.id = 100009
        msg.channel.id = lounge_id
        msg.channel.name = "lounge"
        msg.author.id = 1210466877294518272
        msg.author.bot = False
        msg.author.display_name = "Ryan"
        msg.content = "<@1542285964213358633> <@1492043459618537492> <@1468012353206354197> can you each download v0.6 and put it in place on your respective harnesses"
        msg.created_at.timestamp.return_value = now
        msg.role_mentions = []
        msg.mentions = [mock_bot.user]
        msg.reference = None

        turn_queue = AsyncMock()
        rules = {
            "channel_tag_requirements": {str(lounge_id): "1543285916506783799"},
            "ambient_classifier_enabled": False
        }
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()
            call_args = turn_queue.put.call_args[0][0]
            self.assertEqual(
                call_args["prompt"],
                "@Zero @Marvin @Amos can you each download v0.6 and put it in place on your respective harnesses"
            )

    async def test_home_message_not_dropped_when_sent_prior_to_boot(self):
        """Verify that recent human messages in home channels are not dropped by BOT_BOOT_TIME."""
        mock_bot = MagicMock()
        mock_bot.user = MagicMock()
        mock_bot.user.id = 1542285964213358633

        now = time.time()
        msg = MagicMock()
        msg.id = 100010
        msg.channel.id = 1544953279664889888  # zero-ops
        msg.channel.name = "zero-ops"
        msg.author.id = 1210466877294518272
        msg.author.bot = False
        msg.author.display_name = "Ryan"
        msg.content = "Check all scripts for localhost instances"
        # Sent 60 seconds before BOT_BOOT_TIME
        msg.created_at.timestamp.return_value = bh.BOT_BOOT_TIME - 60.0
        msg.reference = None

        home_queue = AsyncMock()
        await bh.handle_message(msg, mock_bot, home_turn_queue=home_queue, ext_turn_queue=AsyncMock())
        home_queue.put.assert_awaited_once()
        call_args = home_queue.put.call_args[0][0]
        self.assertEqual(call_args["prompt"], "Check all scripts for localhost instances")

    async def test_warm_channel_history_recovers_unhandled_home_turn(self):
        """Verify that warm_channel_history detects unhandled user messages from downtime and triggers them."""
        mock_bot = MagicMock()
        mock_bot.user = MagicMock()
        mock_bot.user.id = 1542285964213358633

        now = time.time()
        # Create mock message from Ryan sent 30 seconds ago
        unhandled_msg = MagicMock()
        unhandled_msg.id = 999999
        unhandled_msg.channel.id = 1544953279664889888
        unhandled_msg.channel.name = "zero-ops"
        unhandled_msg.author.id = 1210466877294518272
        unhandled_msg.author.bot = False
        unhandled_msg.author.display_name = "Ryan"
        unhandled_msg.content = "Ok can you check all of our scripts"
        unhandled_msg.created_at.timestamp.return_value = now - 30.0
        unhandled_msg.reference = None

        mock_channel = MagicMock()
        mock_channel.id = 1544953279664889888
        mock_channel.name = "zero-ops"

        # Mock channel.history iterator
        async def mock_history(limit=25):
            yield unhandled_msg

        mock_channel.history = mock_history

        home_queue = AsyncMock()
        bh.PROCESSED_BACKLOG_MSG_IDS.clear()

        with patch("tools.bridge_handlers.handle_message", new_callable=AsyncMock) as mock_handle:
            await bh.warm_channel_history(
                channel=mock_channel,
                limit=25,
                bot=mock_bot,
                turn_queue=home_queue,
                ext_turn_queue=AsyncMock()
            )
            # Give created tasks a chance to run
            await asyncio.sleep(0.05)
            mock_handle.assert_awaited_once()
            self.assertEqual(mock_handle.call_args[1]["msg"].id, 999999)

    async def test_envelope_floor_closed_suppresses_turn(self):
        """Verify that an envelope with floor: closed suppresses turn execution unless addressed to Zero."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633
        lounge_id = 1534452820995080192

        msg = MagicMock()
        msg.id = 200001
        msg.channel.id = lounge_id
        msg.channel.name = "lounge"
        msg.author.id = 1468012353206354197
        msg.author.bot = True
        msg.author.display_name = "Amos"
        msg.content = '```handoff\n{"kind": "status", "reply": "none", "floor": "closed", "to": "team"}\n```\nAll done here.'
        msg.created_at.timestamp.return_value = time.time()
        msg.role_mentions = []
        msg.mentions = []
        msg.reference = None

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={}):
            await bh.handle_message(msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_not_awaited()

    async def test_envelope_reply_none_does_not_short_circuit(self):
        """Verify that reply: none with an open or unclosed floor does not short-circuit before evaluation."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633
        lounge_id = 1534452820995080192

        msg = MagicMock()
        msg.id = 200002
        msg.channel.id = lounge_id
        msg.channel.name = "lounge"
        msg.author.id = 1210466877294518272
        msg.author.bot = False
        msg.author.display_name = "Ryan"
        msg.content = '<@1542285964213358633> ```handoff\n{"kind": "status", "reply": "none", "floor": "open"}\n```\nFYI on the new metrics.'
        msg.created_at.timestamp.return_value = time.time()
        msg.role_mentions = []
        msg.mentions = [mock_bot.user]
        msg.reference = None

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={}):
            await bh.handle_message(msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()

    async def test_banana_watcher_concluded_summary_prompt_routing(self):
        """Verify that Banana Watcher Discussion Concluded prompts bypass 4s bot cascade cooldown, preserve @Zero, and inject Rule 7 directive."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633
        banana_stand_id = 1534436119888793750

        msg = MagicMock()
        msg.id = 300001
        msg.channel.id = banana_stand_id
        msg.channel.name = "the-banana-stand"
        msg.author.id = 1545924520236290198
        msg.author.bot = True
        msg.author.name = "Banana Watcher"
        msg.author.display_name = "Banana Watcher"
        msg.content = (
            "🍌 **Discussion Concluded**: Topic `banana-protocol-pr-11` has reached resolution.\n"
            "<@1542285964213358633> (@Zero): Please synthesize and deliver a concise summary of this discussion to <#1534452820995080192> (no more than 250 words)."
        )
        msg.created_at.timestamp.return_value = time.time()
        msg.role_mentions = []
        msg.mentions = [mock_bot.user]
        msg.reference = None

        # Simulate another bot spoke 0.5s ago (triggering cascade cooldown for normal bots)
        bh.channel_last_bot_reply[banana_stand_id] = time.time()

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"last_word_protocol_enabled": True}):
            await bh.handle_message(msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()

            item = turn_queue.put.call_args[0][0]
            prompt = item["prompt"]
            # Must preserve @Zero (never strip into "():")
            self.assertIn("@Zero:", prompt)
            self.assertNotIn("():", prompt)
            # Must inject explicit Rule 7 Executive Summary directive
            self.assertIn("RULE 7 CONCLUDED DISCUSSION EXECUTIVE SUMMARY", prompt)
            self.assertIn("banana-protocol-pr-11", prompt)
            self.assertIn("python3 /workspace/tools/outbox.py --channel lounge", prompt)
            # Must not engage Last Word Protocol
            self.assertFalse(item.get("is_last_word"))

    async def test_banana_watcher_stalled_topic_prompt_routing(self):
        """Verify that Banana Watcher Topic Stalled prompts inject the stalled topic directive and bypass cooldown."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633
        banana_stand_id = 1534436119888793750

        msg = MagicMock()
        msg.id = 300002
        msg.channel.id = banana_stand_id
        msg.channel.name = "the-banana-stand"
        msg.author.id = 1545924520236290198
        msg.author.bot = True
        msg.author.name = "Banana Watcher"
        msg.author.display_name = "Banana Watcher"
        msg.content = (
            "🍌 **Topic Stalled**: Topic `outbound-redaction-mechanism` has been quiet for 10m without a resolution or ticket.\n"
            "<@&1543285916506783799> (@robot): Is this ready to land in a PR/task, or are we parking it?"
        )
        msg.created_at.timestamp.return_value = time.time()
        msg.role_mentions = []
        msg.mentions = []
        msg.reference = None

        turn_queue = AsyncMock()
        with patch("tools.bridge_handlers.get_runtime_rules", return_value={"last_word_protocol_enabled": True}):
            await bh.handle_message(msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()

            item = turn_queue.put.call_args[0][0]
            prompt = item["prompt"]
            self.assertIn("BANANA WATCHER TOPIC STALLED NUDGE", prompt)
            self.assertIn("outbound-redaction-mechanism", prompt)
            self.assertIn("NEVER emit [NO_REPLY]", prompt)

    def test_is_bridge_busy_exclude_channel(self):
        """Verify is_bridge_busy properly honors exclude_channel_id parameter."""
        mock_task = MagicMock()
        mock_task.done.return_value = False

        bh.channel_active_tasks[111] = mock_task
        bh.channel_active_tasks[222] = mock_task

        try:
            # Without exclude, both are reported
            busy_all = bh.is_bridge_busy()
            self.assertIn("channel:111", busy_all)
            self.assertIn("channel:222", busy_all)

            # Excluding 111 reports only 222
            busy_ex_111 = bh.is_bridge_busy(exclude_channel_id=111)
            self.assertNotIn("channel:111", busy_ex_111)
            self.assertIn("channel:222", busy_ex_111)

            # If 222 is done, excluding 111 reports empty
            mock_task.done.return_value = True
            self.assertEqual(bh.is_bridge_busy(exclude_channel_id=111), [])
        finally:
            bh.channel_active_tasks.pop(111, None)
            bh.channel_active_tasks.pop(222, None)

    async def test_brock_public_channel_non_owner_ignored(self):
        """Messages in Brock Discord public channels from non-owners must be ignored even if tagging Zero."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        msg = MagicMock()
        msg.id = 400001
        msg.guild.id = 1210466877294518272  # Brock Discord
        msg.channel.id = 1453427860793463000  # #seerr-requests-and-chat
        msg.channel.name = "seerr-requests-and-chat"
        msg.channel.category_id = 1210466877835313153  # Primary Server Category (not Agent Zero)
        msg.channel.parent_id = None
        msg.channel.parent = None
        msg.author.id = 999999999999999999  # NOT Ryan
        msg.author.bot = False
        msg.author.name = "OtherUser"
        msg.author.display_name = "OtherUser"
        msg.content = "<@1542285964213358633> can you download Dune 2?"
        msg.created_at.timestamp.return_value = time.time()
        msg.mentions = [mock_bot.user]
        msg.role_mentions = []
        msg.reference = None
        msg.attachments = []

        home_queue = AsyncMock()
        ext_queue = AsyncMock()
        await bh.handle_message(msg, mock_bot, home_turn_queue=home_queue, ext_turn_queue=ext_queue)
        home_queue.put.assert_not_called()
        ext_queue.put.assert_not_called()

    async def test_brock_public_channel_owner_untagged_ignored(self):
        """Messages in Brock Discord public channels from owner WITHOUT @Zero tag must be ignored."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        msg = MagicMock()
        msg.id = 400002
        msg.guild.id = 1210466877294518272  # Brock Discord
        msg.channel.id = 1453427860793463000  # #seerr-requests-and-chat
        msg.channel.name = "seerr-requests-and-chat"
        msg.channel.category_id = 1210466877835313153  # Primary Server Category (not Agent Zero)
        msg.channel.parent_id = None
        msg.channel.parent = None
        msg.author.id = 179407724335988736  # Ryan Brock (owner)
        msg.author.bot = False
        msg.author.name = "Ryan"
        msg.author.display_name = "Ryan"
        msg.content = "Just approved the latest request on Overseerr"
        msg.created_at.timestamp.return_value = time.time()
        msg.mentions = []
        msg.role_mentions = []
        msg.reference = None
        msg.attachments = []

        home_queue = AsyncMock()
        ext_queue = AsyncMock()
        await bh.handle_message(msg, mock_bot, home_turn_queue=home_queue, ext_turn_queue=ext_queue)
        home_queue.put.assert_not_called()
        ext_queue.put.assert_not_called()

    async def test_brock_public_channel_owner_tagged_dispatched_to_home_queue(self):
        """Messages in Brock Discord public channels from owner WITH @Zero tag must be dispatched to home_turn_queue."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        msg = MagicMock()
        msg.id = 400003
        msg.guild.id = 1210466877294518272  # Brock Discord
        msg.channel.id = 1453427860793463000  # #seerr-requests-and-chat
        msg.channel.name = "seerr-requests-and-chat"
        msg.channel.category_id = 1210466877835313153  # Primary Server Category (not Agent Zero)
        msg.channel.parent_id = None
        msg.channel.parent = None
        msg.author.id = 179407724335988736  # Ryan Brock (owner)
        msg.author.bot = False
        msg.author.name = "Ryan"
        msg.author.display_name = "Ryan"
        msg.content = "<@1542285964213358633> check why Dune 2 failed to import"
        msg.created_at.timestamp.return_value = time.time()
        msg.mentions = [mock_bot.user]
        msg.role_mentions = []
        msg.reference = None
        msg.attachments = []

        home_queue = AsyncMock()
        ext_queue = AsyncMock()
        await bh.handle_message(msg, mock_bot, home_turn_queue=home_queue, ext_turn_queue=ext_queue)
        ext_queue.put.assert_not_called()
        home_queue.put.assert_awaited_once()

        item = home_queue.put.call_args[0][0]
        self.assertEqual(item["mode"], "home")
        self.assertEqual(item["channel_id"], 1453427860793463000)
        self.assertIn("BROCK DISCORD PUBLIC CHANNEL #seerr-requests-and-chat", item["prompt"])
        self.assertIn("check why Dune 2 failed to import", item["prompt"])
        self.assertNotIn("<@1542285964213358633>", item["prompt"])

    async def test_lazy_typer_home_minimal_prompt_expansion(self):
        """Verify that a minimal prompt like 'Investigate' triggers lazy typer context injection with channel history."""
        from collections import deque
        from tools.channel_history import record_message, _history_store
        ch_id = 1542081375287640084 # #zero-chat
        _history_store[str(ch_id)] = deque(maxlen=20)
        record_message(
            channel_id=ch_id,
            channel_name="zero-chat",
            author_name="Zero",
            is_bot=True,
            content="⚠️ BB refresh_stats failed steps (Thu Sep 10 6:49 AM): Step 1 empty",
            msg_id=111222
        )

        mock_bot = MagicMock()
        mock_bot.user = MagicMock()
        mock_bot.user.id = 1542285964213358633

        msg = MagicMock()
        msg.id = 111223
        msg.channel.id = ch_id
        msg.channel.name = "zero-chat"
        msg.author.id = 179407724335988736  # Ryan
        msg.author.bot = False
        msg.content = "Investigate"
        msg.created_at.timestamp.return_value = time.time()
        msg.reference = None
        msg.attachments = []

        home_queue = AsyncMock()
        await bh.handle_message(msg, mock_bot, home_turn_queue=home_queue, ext_turn_queue=AsyncMock())
        home_queue.put.assert_awaited_once()
        queued_item = home_queue.put.call_args[0][0]
        prompt = queued_item["prompt"]

        self.assertIn("OPERATIONAL DIRECTIVE - LAZY TYPER ADDRESSING", prompt)
        self.assertIn("RECENT DISCORD CHANNEL CONTEXT", prompt)
        self.assertIn("BB refresh_stats failed steps", prompt)
        self.assertIn("Investigate", prompt)

    async def test_record_message_captures_bot_messages(self):
        """Verify that alerts posted by the bot user itself are recorded to channel history before returning."""
        from collections import deque
        from tools.channel_history import get_recent_messages, _history_store
        ch_id = 1542081375287640084
        _history_store[str(ch_id)] = deque(maxlen=20)

        mock_bot = MagicMock()
        mock_bot.user = MagicMock()
        mock_bot.user.id = 1542285964213358633

        alert_msg = MagicMock()
        alert_msg.id = 888999
        alert_msg.channel.id = ch_id
        alert_msg.channel.name = "zero-chat"
        alert_msg.author.id = 1542285964213358633  # Sent via bot token
        alert_msg.author.bot = True
        alert_msg.author.display_name = "Zero"
        alert_msg.content = "⚠️ BB refresh_stats alert"
        alert_msg.created_at.timestamp.return_value = time.time()
        alert_msg.reference = None

        home_queue = AsyncMock()
        await bh.handle_message(alert_msg, mock_bot, home_turn_queue=home_queue, ext_turn_queue=AsyncMock())
        # Should not trigger a reply turn
        home_queue.put.assert_not_called()

    async def test_queue_worker_dispatches_without_task_done_error(self):
        """Verify that queue_worker successfully handles items and marks task_done without TypeError."""
        mock_bot = MagicMock()
        q = asyncio.Queue()
        item = {
            "prompt": "test prompt",
            "reply_target": MagicMock(),
            "channel_id": 1542081375287640084,
            "is_thread_task": False
        }
        await q.put(item)

        # Run worker briefly and cancel
        worker_task = asyncio.create_task(
            bh.queue_worker(
                home_turn_queue=q,
                bot=mock_bot
            )
        )
        await asyncio.sleep(0.05)
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    async def test_reply_to_zero_satisfies_channel_tag_requirement(self):
        """Verify that native Discord replies to Zero bypass channel tag requirements."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        lounge_id = 1534452820995080192
        now = time.time()

        # Mock reply referencing Zero's message ID in history
        from collections import deque
        from tools.channel_history import record_message, _history_store
        _history_store[str(lounge_id)] = deque(maxlen=20)
        record_message(
            channel_id=lounge_id,
            channel_name="lounge",
            author_name="Zero",
            is_bot=True,
            content="Here is the explanation...",
            msg_id=777888
        )

        reply_msg = MagicMock()
        reply_msg.id = 777889
        reply_msg.channel.id = lounge_id
        reply_msg.channel.name = "lounge"
        reply_msg.author.id = 1210466877294518272
        reply_msg.author.bot = False
        reply_msg.author.display_name = "Arcane"
        reply_msg.content = "That just explains how you prevent tool spam, not how you prevent agy from exiting"
        reply_msg.created_at.timestamp.return_value = now
        reply_msg.role_mentions = []
        reply_msg.mentions = []
        reply_msg.reference = MagicMock()
        reply_msg.reference.resolved = None  # Unresolved by discord.py cache
        reply_msg.reference.message_id = 777888

        turn_queue = AsyncMock()
        rules = {
            "channel_tag_requirements": {str(lounge_id): "1543285916506783799"},
            "ambient_classifier_enabled": False
        }
        with patch("tools.bridge_handlers.get_runtime_rules", return_value=rules):
            await bh.handle_message(reply_msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
            turn_queue.put.assert_awaited_once()
            call_args = turn_queue.put.call_args[0][0]
            self.assertEqual(call_args["prompt"], "That just explains how you prevent tool spam, not how you prevent agy from exiting")


if __name__ == "__main__":
    unittest.main()



