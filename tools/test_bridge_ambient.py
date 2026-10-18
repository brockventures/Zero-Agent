"""
Unit tests for tools/bridge_ambient.py.
Exercises Brock guild public gating, Crab Cavern ambient routing, tag gating, and Banana Watcher directives.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
from tools.bridge_ambient import (
    BANANA_WATCHER_BOT_ID,
    route_external_message,
    warm_channel_history,
)
from tools.bridge_state import OWNER_USER_ID


class TestBridgeAmbient(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.silence_patcher = patch("tools.bridge_state.is_crab_cavern_silenced", return_value=(False, ""))
        self.silence_patcher.start()

    def tearDown(self):
        self.silence_patcher.stop()
        super().tearDown()

    async def test_brock_guild_public_channel_unauthorized(self):
        msg = MagicMock()
        msg.guild.id = 1210466877835313152  # Brock Guild
        msg.author.id = 999999999  # Not owner
        msg.author.bot = False
        msg.channel.id = 1453427860793463000  # #seerr-requests-and-chat
        msg.content = "@Zero check this"

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        with patch("tools.bridge_ambient.is_brock_guild", return_value=True):
            handled = await route_external_message(
                msg, bot, "@Zero check this", "GuestUser", home_queue, ext_queue
            )
            self.assertTrue(handled)
            self.assertTrue(home_queue.empty())
            self.assertTrue(ext_queue.empty())

    async def test_brock_guild_public_channel_authorized(self):
        msg = MagicMock()
        msg.guild.id = 1210466877835313152  # Brock Guild
        msg.author.id = OWNER_USER_ID  # Ryan Brock
        msg.author.bot = False
        msg.channel.id = 1453427860793463000  # #seerr-requests-and-chat
        msg.channel.name = "seerr-requests-and-chat"
        msg.content = "<@1542285964213358633> what is the status of Severance?"
        msg.attachments = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        with patch("tools.bridge_ambient.is_brock_guild", return_value=True), \
             patch("tools.channel_history.format_channel_context", return_value=""):
            handled = await route_external_message(
                msg, bot, msg.content, "Ryan", home_queue, ext_queue
            )
            self.assertTrue(handled)
            self.assertFalse(home_queue.empty())
            item = await home_queue.get()
            self.assertIn("BROCK DISCORD PUBLIC CHANNEL", item["prompt"])
            self.assertIn("what is the status of Severance?", item["prompt"])

    async def test_banana_watcher_concluded_summary_directive(self):
        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = BANANA_WATCHER_BOT_ID
        msg.author.bot = True
        msg.channel.id = 1534436119888793750  # #the-banana-stand
        msg.channel.name = "the-banana-stand"
        msg.content = "🍌 **Discussion Concluded**: Topic `orbital-routing` has reached resolution. <@1542285964213358633> dispatch summary to lounge."
        msg.attachments = []
        msg.reference = None

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
            handled = await route_external_message(
                msg, bot, msg.content, "Banana Watcher", home_queue, ext_queue
            )
            self.assertTrue(handled)
            self.assertFalse(ext_queue.empty())
            item = await ext_queue.get()
            self.assertIn("RULE 7 CONCLUDED DISCUSSION EXECUTIVE SUMMARY", item["prompt"])
            self.assertIn("orbital-routing", item["prompt"])

    async def test_ambient_classifier_below_threshold_buffers_silently(self):
        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = 11111111
        msg.author.bot = False
        msg.channel.id = 1534452820995080192  # #lounge
        msg.channel.name = "lounge"
        msg.content = "What was the score of the baseball game yesterday?"
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        rules = {
            "ambient_classifier_enabled": True,
            "ambient_relevance_threshold": 0.80,
            "channel_tag_requirements": {},
        }

        with patch("tools.bridge_ambient.is_brock_guild", return_value=False), \
             patch("tools.classifier.score_relevance", return_value=0.25):
            handled = await route_external_message(
                msg, bot, msg.content, "Ian", home_queue, ext_queue, rules=rules
            )
            self.assertTrue(handled)
            # Below 0.80 -> buffered silently, queue remains empty
            self.assertTrue(ext_queue.empty())

    async def test_ambient_classifier_applies_lightweight_reaction(self):
        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = 11111111
        msg.author.bot = False
        msg.channel.id = 1534452820995080192  # #lounge
        msg.channel.name = "lounge"
        msg.content = "That container crash log is pure comedy gold"
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []
        msg.add_reaction = AsyncMock()

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        rules = {
            "ambient_classifier_enabled": True,
            "ambient_relevance_threshold": 0.80,
            "channel_tag_requirements": {},
        }

        with patch("tools.bridge_ambient.is_brock_guild", return_value=False), \
             patch("tools.classifier.score_relevance", return_value=0.30), \
             patch("tools.classifier.pop_last_reaction", return_value="😂"):
            handled = await route_external_message(
                msg, bot, msg.content, "Ian", home_queue, ext_queue, rules=rules
            )
            self.assertTrue(handled)
            self.assertTrue(ext_queue.empty())
            msg.add_reaction.assert_awaited_once_with("😂")

    async def test_bare_zero_text_in_tag_gated_channel(self):
        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = 179407724335988736
        msg.author.bot = False
        msg.channel.id = 1551465050072416286  # #side-project
        msg.channel.name = "side-project"
        msg.content = "Zero can you update so you respond to bare Zero text as well?"
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        rules = {
            "channel_tag_requirements": {
                "1551465050072416286": ["1543285916506783799", "1543462881624858624"]
            },
            "ambient_classifier_enabled": False,
        }

        with patch("tools.bridge_ambient.is_brock_guild", return_value=False), \
             patch("tools.channel_history.format_channel_context", return_value=""):
            handled = await route_external_message(
                msg, bot, msg.content, "Ryan", home_queue, ext_queue, rules=rules
            )
            self.assertTrue(handled)
            self.assertFalse(ext_queue.empty())
            item = await ext_queue.get()
            self.assertIn("can you update so you respond to bare Zero text as well?", item["prompt"])

    async def test_external_group_mid_turn_steering(self):
        """Verify mid-turn steering in shared/group channels formats prompt, signals SIGINT, and enqueues steer payload."""
        import signal
        from tools.bridge_runner import channel_active_procs, channel_active_prompts, steering_channels

        channel_id = 1534452820995080192  # #lounge
        mock_proc = MagicMock()
        mock_proc.returncode = None
        channel_active_procs[channel_id] = mock_proc
        channel_active_prompts[channel_id] = "Drafting breakdown of Reddit skill"

        msg = MagicMock()
        msg.guild.id = 1534436119888793748
        msg.author.id = 1468012353206354197
        msg.author.name = "Amos"
        msg.author.display_name = "Amos"
        msg.author.bot = True
        msg.channel.id = channel_id
        msg.channel.name = "lounge"
        msg.content = "<@1542285964213358633>, how do you read Reddit? old.reddit.com gives 403."
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        bot.mentions = [bot.user]
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        try:
            with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
                handled = await route_external_message(
                    msg, bot, msg.content, "Amos", home_queue, ext_queue
                )
                self.assertTrue(handled)
                mock_proc.send_signal.assert_called_once_with(signal.SIGINT)
                self.assertIn(channel_id, steering_channels)
                self.assertFalse(ext_queue.empty())
                item = await ext_queue.get()
                self.assertTrue(item.get("is_steer"))
                self.assertEqual(item.get("channel_id"), channel_id)
                self.assertIn("[MID-TURN GROUP CONVERSATION UPDATE]", item["prompt"])
                self.assertIn("Drafting breakdown of Reddit skill", item["prompt"])
                self.assertIn("how do you read Reddit?", item["prompt"])
        finally:
            channel_active_procs.pop(channel_id, None)
            channel_active_prompts.pop(channel_id, None)
            steering_channels.discard(channel_id)

    async def test_crab_cavern_silenced_drops_messages(self):
        """Verify that when Crab Cavern is silenced, any message arriving from outside Brock guild is dropped immediately."""
        self.silence_patcher.stop()  # Stop default un-silenced patcher
        try:
            with patch("tools.bridge_state.is_crab_cavern_silenced", return_value=(True, "Silenced until Tuesday 8 PM PT")):
                msg = MagicMock()
                msg.guild.id = 1534436119888793740
                msg.author.id = 1468012353206354197
                msg.author.bot = True
                msg.channel.id = 1534436119888793750  # #the-banana-stand
                msg.channel.name = "the-banana-stand"
                msg.content = "<@1542285964213358633> urgent task"
                msg.attachments = []
                msg.reference = None

                bot = MagicMock()
                bot.user.id = 1542285964213358633
                home_queue = asyncio.Queue()
                ext_queue = asyncio.Queue()

                with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
                    handled = await route_external_message(
                        msg, bot, msg.content, "Amos", home_queue, ext_queue
                    )
                    self.assertTrue(handled)
                    self.assertTrue(home_queue.empty())
                    self.assertTrue(ext_queue.empty())
        finally:
            self.silence_patcher.start()


if __name__ == "__main__":
    unittest.main()
