#!/usr/bin/env python3
"""
test_channel_history.py - Unit tests for cross-channel context awareness in Crab Cavern.
"""

import unittest
from datetime import datetime, timezone, timedelta
from collections import deque
import tools.channel_history as ch

class TestChannelHistoryCrossChannel(unittest.TestCase):
    def setUp(self):
        self.orig_store = dict(ch._history_store)
        ch._history_store.clear()

    def tearDown(self):
        ch._history_store.clear()
        ch._history_store.update(self.orig_store)

    def test_single_channel_formatting_unlinked(self):
        """Unlinked channels (e.g. home channel) should only show active channel history."""
        ch_id = "1542081375287640084"
        ch._history_store[ch_id] = deque([
            {"id": 1, "channel_id": ch_id, "channel_name": "zero-chat", "author": "Ryan", "is_bot": False, "content": "Hello home", "timestamp": "2026-09-03 00:00:00 UTC"}
        ])
        ctx = ch.format_channel_context(ch_id, limit=5, include_linked_channels=True)
        self.assertIn("RECENT CHANNEL HISTORY (#zero-chat", ctx)
        self.assertNotIn("CROSS-CHANNEL AWARENESS", ctx)

    def test_linked_channels_cross_awareness(self):
        """Messages in lounge should include agent-chat cross-awareness, and vice versa."""
        lounge_id = "1534452820995080192"
        agent_chat_id = "1534436119888793750"
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        ch._history_store[lounge_id] = deque([
            {"id": 10, "channel_id": lounge_id, "channel_name": "lounge", "author": "Ryan", "is_bot": False, "content": "Lounge topic", "timestamp": now_str}
        ])
        ch._history_store[agent_chat_id] = deque([
            {"id": 20, "channel_id": agent_chat_id, "channel_name": "agent-chat", "author": "Amos", "is_bot": True, "content": "Robot banter", "timestamp": now_str}
        ])

        # Formatted from lounge
        lounge_ctx = ch.format_channel_context(lounge_id, limit=5, peer_limit=5)
        self.assertIn("RECENT CHANNEL HISTORY (#lounge", lounge_ctx)
        self.assertIn("CROSS-CHANNEL AWARENESS (#agent-chat", lounge_ctx)
        self.assertIn("Robot banter", lounge_ctx)

        # Formatted from agent-chat
        ac_ctx = ch.format_channel_context(agent_chat_id, limit=5, peer_limit=5)
        self.assertIn("RECENT CHANNEL HISTORY (#agent-chat", ac_ctx)
        self.assertIn("CROSS-CHANNEL AWARENESS (#lounge", ac_ctx)
        self.assertIn("Lounge topic", ac_ctx)

    def test_recency_filter(self):
        """Peer messages older than max_peer_age_seconds should be pruned."""
        lounge_id = "1534452820995080192"
        agent_chat_id = "1534436119888793750"
        now_utc = datetime.now(timezone.utc)
        old_time = (now_utc - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S UTC")
        recent_time = (now_utc - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S UTC")

        ch._history_store[lounge_id] = deque([
            {"id": 1, "channel_id": lounge_id, "channel_name": "lounge", "author": "Ryan", "is_bot": False, "content": "Recent lounge", "timestamp": recent_time}
        ])
        ch._history_store[agent_chat_id] = deque([
            {"id": 2, "channel_id": agent_chat_id, "channel_name": "agent-chat", "author": "Amos", "is_bot": True, "content": "Ancient history", "timestamp": old_time}
        ])

        ctx = ch.format_channel_context(lounge_id, limit=5, max_peer_age_seconds=3600)
        self.assertIn("Recent lounge", ctx)
        self.assertNotIn("CROSS-CHANNEL AWARENESS", ctx)

    def test_thread_parent_channel_resolution(self):
        """A thread under lounge should identify agent-chat as a peer via parent_channel_id."""
        lounge_id = "1534452820995080192"
        thread_id = "999888777666"
        agent_chat_id = "1534436119888793750"
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        ch._history_store[thread_id] = deque([
            {"id": 100, "channel_id": thread_id, "channel_name": "thread-task", "author": "Ryan", "is_bot": False, "content": "Thread message", "timestamp": now_str}
        ])
        ch._history_store[agent_chat_id] = deque([
            {"id": 200, "channel_id": agent_chat_id, "channel_name": "agent-chat", "author": "Amos", "is_bot": True, "content": "Peer activity", "timestamp": now_str}
        ])

        ctx = ch.format_channel_context(thread_id, limit=5, peer_limit=5, parent_channel_id=lounge_id)
        self.assertIn("RECENT CHANNEL HISTORY (#thread-task", ctx)
        self.assertIn("CROSS-CHANNEL AWARENESS (#agent-chat", ctx)
        self.assertIn("Peer activity", ctx)

    def test_pt_timestamp_conversion(self):
        """UTC timestamps must be converted to Pacific Time (PT) in channel context."""
        ch_id = "1534452820995080192"
        # 04:12:11 UTC on Sep 3 = 09:12:11 PM PT on Sep 2 (PDT = UTC-7)
        ch._history_store[ch_id] = deque([
            {"id": 55, "channel_id": ch_id, "channel_name": "lounge", "author": "Zero", "is_bot": True, "content": "Checking time", "timestamp": "2026-09-03 04:12:11 UTC"}
        ])
        ctx = ch.format_channel_context(ch_id, limit=1)
        self.assertIn("[09:12:11 PM PT] Zero (bot): Checking time", ctx)
        self.assertIn("PT timezone", ctx)

if __name__ == "__main__":
    unittest.main()
