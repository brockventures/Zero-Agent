#!/usr/bin/env python3
"""
Unit test suite for bridge_handlers.py (Discord Bot Event Handlers, Routing & Dispatch).
"""

import asyncio
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

        bs.DATA_DIR = self.temp_path
        bh.ATTACHMENTS_DIR = self.temp_path / "attachments"
        bh.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        bs.IN_FLIGHT_FILE = self.temp_path / "in_flight_turn.json"
        bs.RESTART_INTENT_FILE = self.temp_path / "restart_intent.json"

    def tearDown(self):
        bs.DATA_DIR = self.orig_data_dir
        bh.ATTACHMENTS_DIR = self.orig_attachments_dir
        bs.IN_FLIGHT_FILE = self.orig_in_flight
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
        mock_msg.channel.id = 1534436119888793750  # agent-chat
        mock_msg.channel.name = "agent-chat"
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
        mock_msg2.channel.id = 1534436119888793750  # agent-chat
        mock_msg2.channel.name = "agent-chat"
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

    def test_slash_commands_registration(self):
        """Verify that native Discord Slash Commands are registered in bot.tree."""
        from tools.bridge import bot
        cmd_names = {cmd.name for cmd in bot.tree.get_commands()}
        expected_commands = {"new", "reset", "model", "logs", "triage", "heartbeat", "tasks", "sidecars", "title"}
        for exp in expected_commands:
            self.assertIn(exp, cmd_names, f"Expected slash command '/{exp}' to be registered on bot.tree")


if __name__ == "__main__":
    unittest.main()
