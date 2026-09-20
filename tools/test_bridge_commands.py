"""
Unit tests for tools/bridge_commands.py.
Exercises UI choice views, operator commands, model switching, sidecar triggers, and lifecycle hooks.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.bridge_commands import (
    MODEL_ALIASES,
    ON_DEMAND_TRIGGERS,
    ChoiceButton,
    QuickChoiceView,
    handle_button_choice,
    handle_operator_command,
)
from tools.bridge_state import VAULT_CHANNEL_ID


class TestBridgeCommands(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        from tools.bridge_commands import PROCESSED_INTERACTIONS
        PROCESSED_INTERACTIONS.clear()

    def tearDown(self):
        from tools.bridge_commands import PROCESSED_INTERACTIONS
        PROCESSED_INTERACTIONS.clear()

    def test_quick_choice_view_structure(self):
        options = ["Option A", "Option B", "Option C", "Option D", "Option E", "Option F"]
        view = QuickChoiceView(options)
        # Max 5 buttons
        self.assertEqual(len(view.children), 5)
        self.assertEqual(view.children[0].label, "Option A")
        self.assertEqual(view.children[0].custom_id, "choice:Option A")

    async def test_handle_button_choice_normal(self):
        turn_queue = asyncio.Queue()
        mock_interaction = MagicMock()
        mock_interaction.id = 99887766
        mock_interaction.channel_id = 999
        mock_interaction.channel.typing = AsyncMock()
        mock_interaction.channel.send = AsyncMock()

        await handle_button_choice("Deploy Staging", mock_interaction, turn_queue)
        self.assertFalse(turn_queue.empty())
        item = await turn_queue.get()
        self.assertEqual(item["prompt"], "Deploy Staging")
        self.assertEqual(item["mode"], "home")

    async def test_handle_operator_command_reset(self):
        msg = MagicMock()
        msg.content = "!reset"
        msg.channel.id = 123
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()

        with patch("tools.bridge_commands.clear_channel_session_id") as mock_clear:
            handled = await handle_operator_command(msg, bot, "!reset", "Ryan", queue)
            self.assertTrue(handled)
            mock_clear.assert_called_once_with(123, "home")
            msg.reply.assert_awaited_once()

    async def test_handle_operator_command_model_switch(self):
        msg = MagicMock()
        msg.content = "!model 3.8"
        msg.channel.id = 123
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()
        mock_setter = MagicMock()

        handled = await handle_operator_command(
            msg, bot, "!model 3.8", "Ryan", queue, active_model_setter=mock_setter
        )
        self.assertTrue(handled)
        mock_setter.assert_called_once_with("gemini-3.8-flash-high")
        msg.reply.assert_awaited_once()

    async def test_handle_operator_command_sidecar_trigger(self):
        msg = MagicMock()
        msg.content = "!heartbeat"
        msg.channel.id = 123
        msg.channel.typing = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()

        handled = await handle_operator_command(msg, bot, "!heartbeat", "Ryan", queue)
        self.assertTrue(handled)
        self.assertFalse(queue.empty())
        item = await queue.get()
        self.assertIn("Run the infrastructure heartbeat check", item["prompt"])

    async def test_handle_operator_command_kalshi_isolation(self):
        msg = MagicMock()
        msg.content = "!kalshi"
        msg.channel.id = 123  # Not VAULT_CHANNEL_ID
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()

        handled = await handle_operator_command(msg, bot, "!kalshi", "Ryan", queue)
        self.assertTrue(handled)
        self.assertTrue(queue.empty())
        msg.reply.assert_awaited_once()
        self.assertIn("exclusively operate in", msg.reply.call_args[0][0])

    async def test_handle_operator_command_agora_kill_switch(self):
        msg = MagicMock()
        msg.content = "!halt Agora trading emergency"
        msg.channel.id = 123
        msg.reply = AsyncMock()

        bot = MagicMock()
        queue = asyncio.Queue()

        with patch("tools.agora_kill_switch.trigger_kill_switch", return_value={"message": "🛑 Agora trading halted."}) as mock_kill:
            handled = await handle_operator_command(msg, bot, "!halt Agora trading emergency", "Ryan", queue)
            self.assertTrue(handled)
            mock_kill.assert_called_once_with(initiator="Ryan", action="halt", channel_id=123)
            msg.reply.assert_awaited_once_with("🛑 Agora trading halted.")


if __name__ == "__main__":
    unittest.main()
