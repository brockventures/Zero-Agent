#!/usr/bin/env python3
"""
Unit tests for Last Word Protocol (tools/last_word_protocol.py).
"""

import os
import time
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
import json

import tools.last_word_protocol as lwp


class TestLastWordProtocol(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.orig_cooldowns_file = lwp.COOLDOWNS_FILE
        lwp.COOLDOWNS_FILE = Path(self.tmp_dir.name) / "bot_cooldowns.json"
        lwp.clear_last_word_in_flight(1534452820995080192)

    def tearDown(self):
        lwp.COOLDOWNS_FILE = self.orig_cooldowns_file
        self.tmp_dir.cleanup()

    def test_streak_calculation_four_messages(self):
        """Verify 4 uninterrupted alternating messages between Zero and Aerial triggers streak=4 and last word."""
        history = [
            {"author": "Aerial", "is_bot": True, "content": "Banters 1", "timestamp": "2026-09-04 20:00:00 UTC"},
            {"author": "Zero", "is_bot": True, "content": "Reply 1", "timestamp": "2026-09-04 20:00:10 UTC"},
            {"author": "Aerial", "is_bot": True, "content": "Banters 2", "timestamp": "2026-09-04 20:00:20 UTC"},
            {"author": "Zero", "is_bot": True, "content": "Reply 2", "timestamp": "2026-09-04 20:00:30 UTC"},
        ]
        streak, msgs = lwp.calculate_bot_streak(1534452820995080192, None, "Aerial", history_msgs=history)
        self.assertEqual(streak, 4)
        is_last_word, count = lwp.check_last_word_condition(1534452820995080192, None, "Aerial", threshold=4, history_msgs=history)
        self.assertTrue(is_last_word)
        self.assertEqual(count, 4)

    def test_streak_calculation_three_messages(self):
        """Verify 3 uninterrupted messages does not trigger last word when threshold is 4."""
        history = [
            {"author": "Aerial", "is_bot": True, "content": "Banters 1", "timestamp": "2026-09-04 20:00:00 UTC"},
            {"author": "Zero", "is_bot": True, "content": "Reply 1", "timestamp": "2026-09-04 20:00:10 UTC"},
            {"author": "Aerial", "is_bot": True, "content": "Banters 2", "timestamp": "2026-09-04 20:00:20 UTC"},
        ]
        streak, _ = lwp.calculate_bot_streak(1534452820995080192, None, "Aerial", history_msgs=history)
        self.assertEqual(streak, 3)
        is_last_word, count = lwp.check_last_word_condition(1534452820995080192, None, "Aerial", threshold=4, history_msgs=history)
        self.assertFalse(is_last_word)
        self.assertEqual(count, 3)

    def test_streak_broken_by_human_message(self):
        """Verify a message from a human breaks the streak."""
        history = [
            {"author": "Aerial", "is_bot": True, "content": "Banters 1", "timestamp": "2026-09-04 20:00:00 UTC"},
            {"author": "Zero", "is_bot": True, "content": "Reply 1", "timestamp": "2026-09-04 20:00:10 UTC"},
            {"author": "Ryan", "is_bot": False, "content": "Hey guys", "timestamp": "2026-09-04 20:00:20 UTC"},
            {"author": "Aerial", "is_bot": True, "content": "Banters 2", "timestamp": "2026-09-04 20:00:30 UTC"},
            {"author": "Zero", "is_bot": True, "content": "Reply 2", "timestamp": "2026-09-04 20:00:40 UTC"},
        ]
        streak, _ = lwp.calculate_bot_streak(1534452820995080192, None, "Aerial", history_msgs=history)
        # Only the 2 messages after Ryan count
        self.assertEqual(streak, 2)
        is_last_word, _ = lwp.check_last_word_condition(1534452820995080192, None, "Aerial", threshold=4, history_msgs=history)
        self.assertFalse(is_last_word)

    def test_streak_broken_by_other_bot(self):
        """Verify a message from a different bot (e.g. Amos) breaks the streak with Aerial."""
        history = [
            {"author": "Aerial", "is_bot": True, "content": "Banters 1", "timestamp": "2026-09-04 20:00:00 UTC"},
            {"author": "Zero", "is_bot": True, "content": "Reply 1", "timestamp": "2026-09-04 20:00:10 UTC"},
            {"author": "Amos", "is_bot": True, "content": "Ledger state updated", "timestamp": "2026-09-04 20:00:20 UTC"},
            {"author": "Aerial", "is_bot": True, "content": "Banters 2", "timestamp": "2026-09-04 20:00:30 UTC"},
            {"author": "Zero", "is_bot": True, "content": "Reply 2", "timestamp": "2026-09-04 20:00:40 UTC"},
        ]
        streak, _ = lwp.calculate_bot_streak(1534452820995080192, None, "Aerial", history_msgs=history)
        self.assertEqual(streak, 2)

    def test_streak_requires_both_participants(self):
        """Verify that multiple consecutive messages from only the bot without Zero participating does not count as a back-and-forth."""
        history = [
            {"author": "Aerial", "is_bot": True, "content": "Monologue 1", "timestamp": "2026-09-04 20:00:00 UTC"},
            {"author": "Aerial", "is_bot": True, "content": "Monologue 2", "timestamp": "2026-09-04 20:00:10 UTC"},
            {"author": "Aerial", "is_bot": True, "content": "Monologue 3", "timestamp": "2026-09-04 20:00:20 UTC"},
            {"author": "Aerial", "is_bot": True, "content": "Monologue 4", "timestamp": "2026-09-04 20:00:30 UTC"},
        ]
        streak, _ = lwp.calculate_bot_streak(1534452820995080192, None, "Aerial", history_msgs=history)
        self.assertEqual(streak, 0)

    def test_streak_broken_by_time_gap(self):
        """Verify time gap > 900s breaks the streak."""
        history = [
            {"author": "Aerial", "is_bot": True, "content": "Old 1", "timestamp": "2026-09-04 18:00:00 UTC"},
            {"author": "Zero", "is_bot": True, "content": "Old 2", "timestamp": "2026-09-04 18:00:10 UTC"},
            {"author": "Aerial", "is_bot": True, "content": "New 1", "timestamp": "2026-09-04 20:00:00 UTC"},
            {"author": "Zero", "is_bot": True, "content": "New 2", "timestamp": "2026-09-04 20:00:10 UTC"},
        ]
        streak, _ = lwp.calculate_bot_streak(1534452820995080192, None, "Aerial", history_msgs=history)
        self.assertEqual(streak, 2)

    def test_pause_and_is_bot_paused(self):
        """Verify pause_bot sets cooldown and is_bot_paused detects it."""
        ch_id = 1534452820995080192
        bot_id = "1542035925603713086"
        bot_name = "Aerial"

        paused, rem, rec = lwp.is_bot_paused(ch_id, bot_id, bot_name)
        self.assertFalse(paused)

        # Pause for 30 minutes
        lwp.pause_bot(ch_id, bot_id, bot_name, duration_seconds=1800.0, reason="Test pause")

        # Now should be paused
        paused, rem, rec = lwp.is_bot_paused(ch_id, bot_id, bot_name)
        self.assertTrue(paused)
        self.assertGreater(rem, 1700.0)
        self.assertEqual(rec["bot_name"], "Aerial")

        # Lookup by name only
        paused_name, _, _ = lwp.is_bot_paused(ch_id, None, "aerial")
        self.assertTrue(paused_name)

        # Lookup by ID only
        paused_id, _, _ = lwp.is_bot_paused(ch_id, bot_id, None)
        self.assertTrue(paused_id)

        # Different bot is NOT paused
        paused_amos, _, _ = lwp.is_bot_paused(ch_id, "1468012353206354197", "Amos")
        self.assertFalse(paused_amos)

        # Different channel is NOT paused
        paused_diff_ch, _, _ = lwp.is_bot_paused(123456789, bot_id, bot_name)
        self.assertFalse(paused_diff_ch)

    def test_unpause_bot(self):
        """Verify unpause_bot clears active cooldown."""
        ch_id = 1534452820995080192
        bot_id = "1542035925603713086"
        bot_name = "Aerial"

        lwp.pause_bot(ch_id, bot_id, bot_name, duration_seconds=1800.0)
        self.assertTrue(lwp.is_bot_paused(ch_id, bot_id, bot_name)[0])

        removed = lwp.unpause_bot(ch_id, "Aerial")
        self.assertTrue(removed)

        # Now should not be paused
        self.assertFalse(lwp.is_bot_paused(ch_id, bot_id, bot_name)[0])

    def test_in_flight_tracking(self):
        """Verify mark_last_word_in_flight prevents collisions and clears cleanly."""
        ch_id = 1534452820995080192
        bot_name = "Aerial"

        self.assertFalse(lwp.is_last_word_in_flight(ch_id, bot_name))
        lwp.mark_last_word_in_flight(ch_id, bot_name)
        self.assertTrue(lwp.is_last_word_in_flight(ch_id, bot_name))

        lwp.clear_last_word_in_flight(ch_id, bot_name)
        self.assertFalse(lwp.is_last_word_in_flight(ch_id, bot_name))

    async def test_bridge_handlers_suppresses_paused_bot(self):
        """Verify bridge_handlers.handle_message drops messages from a paused bot immediately."""
        from unittest.mock import AsyncMock, MagicMock
        import tools.bridge_handlers as bh

        ch_id = 1534452820995080192
        bot_id = 1542035925603713086
        bot_name = "Aerial"

        # Pause Aerial
        lwp.pause_bot(ch_id, bot_id, bot_name, duration_seconds=1800.0)

        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        msg = MagicMock()
        msg.id = 999999
        msg.channel.id = ch_id
        msg.channel.name = "lounge"
        msg.author.id = bot_id
        msg.author.bot = True
        msg.author.display_name = bot_name
        msg.content = "Zero, what do you think of this?"
        msg.created_at.timestamp.return_value = time.time()
        msg.role_mentions = []
        msg.mentions = [mock_bot.user]
        msg.reference = None

        turn_queue = AsyncMock()
        await bh.handle_message(msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
        # Should be suppressed without queueing
        turn_queue.put.assert_not_awaited()

    async def test_bridge_handlers_operator_pause_command(self):
        """Verify operator can command pause responding to Aerial."""
        from unittest.mock import AsyncMock, MagicMock
        import tools.bridge_handlers as bh

        ch_id = 1534452820995080192
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        msg = MagicMock()
        msg.id = 999998
        msg.channel.id = ch_id
        msg.channel.name = "lounge"
        msg.author.id = 1210466877294518272  # Ryan
        msg.author.bot = False
        msg.author.display_name = "Ryan"
        msg.content = "<@1542285964213358633> pause responding to Aerial"
        msg.created_at.timestamp.return_value = time.time()
        msg.role_mentions = []
        msg.mentions = [mock_bot.user]
        msg.reference = None
        msg.reply = AsyncMock()

        turn_queue = AsyncMock()
        await bh.handle_message(msg, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
        msg.reply.assert_awaited_once()
        self.assertIn("paused", msg.reply.call_args[0][0].lower())
        self.assertTrue(lwp.is_bot_paused(ch_id, None, "Aerial")[0])

        # Now test unpause
        msg_unpause = MagicMock()
        msg_unpause.id = 999997
        msg_unpause.channel.id = ch_id
        msg_unpause.channel.name = "lounge"
        msg_unpause.author.id = 1210466877294518272
        msg_unpause.author.bot = False
        msg_unpause.author.display_name = "Ryan"
        msg_unpause.content = "<@1542285964213358633> unpause Aerial"
        msg_unpause.created_at.timestamp.return_value = time.time()
        msg_unpause.role_mentions = []
        msg_unpause.mentions = [mock_bot.user]
        msg_unpause.reference = None
        msg_unpause.reply = AsyncMock()

        await bh.handle_message(msg_unpause, mock_bot, home_turn_queue=AsyncMock(), ext_turn_queue=turn_queue)
        msg_unpause.reply.assert_awaited_once()
        self.assertIn("resumed", msg_unpause.reply.call_args[0][0].lower())
        self.assertFalse(lwp.is_bot_paused(ch_id, None, "Aerial")[0])


if __name__ == "__main__":
    unittest.main()
