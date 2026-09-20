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

from tools.bridge_ambient import (
    BANANA_WATCHER_BOT_ID,
    route_external_message,
    warm_channel_history,
)
from tools.bridge_state import OWNER_USER_ID


class TestBridgeAmbient(unittest.IsolatedAsyncioTestCase):

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


if __name__ == "__main__":
    unittest.main()
