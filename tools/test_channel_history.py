#!/usr/bin/env python3
"""
test_channel_history.py - Unit tests for cross-channel context awareness in Crab Cavern.
"""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
elif sys.path[0] != _REPO_ROOT:
    sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from collections import deque
from unittest.mock import patch
import tools.channel_history as ch

class TestChannelHistoryCrossChannel(unittest.TestCase):
    def setUp(self):
        self.save_patcher = patch.object(ch, "save_history")
        self.save_patcher.start()
        self.orig_store = {k: deque( (dict(m) for m in q), maxlen=ch.MAX_HISTORY_PER_CHANNEL ) for k, q in ch._history_store.items()}
        ch._history_store.clear()

    def tearDown(self):
        self.save_patcher.stop()
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
        """Messages in lounge should include the-banana-stand cross-awareness, and vice versa."""
        lounge_id = "1534452820995080192"
        agent_chat_id = "1534436119888793750"
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        ch._history_store[lounge_id] = deque([
            {"id": 10, "channel_id": lounge_id, "channel_name": "lounge", "author": "Ryan", "is_bot": False, "content": "Lounge topic", "timestamp": now_str}
        ])
        ch._history_store[agent_chat_id] = deque([
            {"id": 20, "channel_id": agent_chat_id, "channel_name": "the-banana-stand", "author": "Amos", "is_bot": True, "content": "Robot banter", "timestamp": now_str}
        ])

        # Formatted from lounge
        lounge_ctx = ch.format_channel_context(lounge_id, limit=5, peer_limit=5)
        self.assertIn("RECENT CHANNEL HISTORY (#lounge", lounge_ctx)
        self.assertIn("CROSS-CHANNEL AWARENESS (#the-banana-stand", lounge_ctx)
        self.assertIn("Robot banter", lounge_ctx)

        # Formatted from the-banana-stand
        ac_ctx = ch.format_channel_context(agent_chat_id, limit=5, peer_limit=5)
        self.assertIn("RECENT CHANNEL HISTORY (#the-banana-stand", ac_ctx)
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
            {"id": 2, "channel_id": agent_chat_id, "channel_name": "the-banana-stand", "author": "Amos", "is_bot": True, "content": "Ancient history", "timestamp": old_time}
        ])

        ctx = ch.format_channel_context(lounge_id, limit=5, max_peer_age_seconds=3600)
        self.assertIn("Recent lounge", ctx)
        self.assertNotIn("CROSS-CHANNEL AWARENESS", ctx)

    def test_thread_parent_channel_resolution(self):
        """A thread under lounge should identify the-banana-stand as a peer via parent_channel_id."""
        lounge_id = "1534452820995080192"
        thread_id = "999888777666"
        agent_chat_id = "1534436119888793750"
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        ch._history_store[thread_id] = deque([
            {"id": 100, "channel_id": thread_id, "channel_name": "thread-task", "author": "Ryan", "is_bot": False, "content": "Thread message", "timestamp": now_str}
        ])
        ch._history_store[agent_chat_id] = deque([
            {"id": 200, "channel_id": agent_chat_id, "channel_name": "the-banana-stand", "author": "Amos", "is_bot": True, "content": "Peer activity", "timestamp": now_str}
        ])

        ctx = ch.format_channel_context(thread_id, limit=5, peer_limit=5, parent_channel_id=lounge_id)
        self.assertIn("RECENT CHANNEL HISTORY (#thread-task", ctx)
        self.assertIn("CROSS-CHANNEL AWARENESS (#the-banana-stand", ctx)
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

    def test_reply_formatting(self):
        """Messages with reply_to should display replying to author and snippet."""
        ch_id = "1534452820995080192"
        ch.record_message(
            channel_id=ch_id,
            channel_name="lounge",
            author_name="Zero",
            is_bot=True,
            content="Detailed breakdown of federal student loan policy.",
            msg_id=500,
            timestamp="2026-09-30 20:50:00 UTC",
        )
        ch.record_message(
            channel_id=ch_id,
            channel_name="lounge",
            author_name="Ryan",
            is_bot=False,
            content="In English please.",
            msg_id=501,
            reply_to_id=500,
            timestamp="2026-09-30 20:55:00 UTC",
        )
        ctx = ch.format_channel_context(ch_id, limit=2)
        self.assertIn('Ryan (replying to @Zero: "Detailed breakdown of federal student..."): In English please.', ctx)

    def test_reaction_recording_and_formatting(self):
        """Reactions added/removed via record_reaction should render in [Reacts: ...] annotations."""
        ch_id = "1534452820995080192"
        ch.record_message(
            channel_id=ch_id,
            channel_name="lounge",
            author_name="Zero",
            is_bot=True,
            content="Shipped the new fix.",
            msg_id=600,
            timestamp="2026-10-22 16:00:00 UTC",
        )
        self.assertTrue(ch.record_reaction(ch_id, 600, "👍", "Ryan", added=True))
        self.assertTrue(ch.record_reaction(ch_id, 600, "👍", "Mike", added=True))
        self.assertTrue(ch.record_reaction(ch_id, 600, "🔥", "Aerial", added=True))

        ctx = ch.format_channel_context(ch_id, limit=1)
        self.assertIn("Shipped the new fix. [Reacts: 👍 (Ryan, Mike), 🔥 (Aerial)]", ctx)

        # Removing a reaction updates the list and prunes empty emoji keys
        self.assertTrue(ch.record_reaction(ch_id, 600, "👍", "Ryan", added=False))
        self.assertTrue(ch.record_reaction(ch_id, 600, "🔥", "Aerial", added=False))
        ctx_after = ch.format_channel_context(ch_id, limit=1)
        self.assertIn("Shipped the new fix. [Reacts: 👍 (Mike)]", ctx_after)
        self.assertNotIn("🔥", ctx_after)


class TestChannelHistoryUnseenDelta(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.save_patcher = patch.object(ch, "save_history")
        self.save_patcher.start()
        self.orig_store = {
            k: deque((dict(m) for m in q), maxlen=ch.MAX_HISTORY_PER_CHANNEL)
            for k, q in ch._history_store.items()
        }
        self.orig_wm_file = ch.CHANNEL_WATERMARKS_FILE
        self.orig_watermarks = dict(ch._seen_watermarks)
        ch.CHANNEL_WATERMARKS_FILE = self.temp_path / "channel_seen_watermarks.json"
        ch._history_store.clear()
        ch._seen_watermarks.clear()

    def tearDown(self):
        self.save_patcher.stop()
        ch.CHANNEL_WATERMARKS_FILE = self.orig_wm_file
        ch._seen_watermarks.clear()
        ch._seen_watermarks.update(self.orig_watermarks)
        ch._history_store.clear()
        ch._history_store.update(self.orig_store)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_first_boot_initializes_watermark_without_dumping_old_history(self):
        """When no watermark exists yet, initialize safely and return empty string."""
        ch_id = "1542081375287640084"
        ch.record_message(ch_id, "zero-chat", "Ryan", False, "Old message 1", msg_id=100, timestamp="2026-11-20 18:00:00 UTC")
        ch.record_message(ch_id, "zero-chat", "Zero", True, "Old reply 1", msg_id=101, timestamp="2026-11-20 18:01:00 UTC")
        ch.record_message(ch_id, "zero-chat", "Ryan", False, "Current prompt", msg_id=102, timestamp="2026-11-20 18:05:00 UTC")

        delta = ch.format_unseen_delta_context(ch_id, exclude_msg_id=102)
        self.assertEqual(delta, "")
        self.assertEqual(ch.get_channel_watermark(ch_id), 101)
        self.assertTrue(ch.CHANNEL_WATERMARKS_FILE.exists())

    def test_normal_back_and_forth_returns_empty_delta(self):
        """Normal user -> Zero -> user turns with no interleaved posts yield empty delta."""
        ch_id = "1542081375287640084"
        ch.record_message(ch_id, "zero-chat", "Ryan", False, "Ping", msg_id=200, timestamp="2026-11-20 18:00:00 UTC")
        ch.mark_channel_seen(ch_id, msg_id=200)
        ch.record_message(ch_id, "zero-chat", "Zero", True, "Pong", msg_id=201, timestamp="2026-11-20 18:00:05 UTC")
        ch.mark_channel_seen(ch_id, msg_id=201)
        ch.record_message(ch_id, "zero-chat", "Ryan", False, "Thanks", msg_id=202, timestamp="2026-11-20 18:00:10 UTC")

        delta = ch.format_unseen_delta_context(ch_id, exclude_msg_id=202)
        self.assertEqual(delta, "")

    def test_sidecar_outbox_posts_between_turns_injected_in_pt_with_exclude(self):
        """Sidecar, outbox, and Decision Card messages after watermark are formatted in PT; exclude_msg_id is omitted."""
        ch_id = "1542081375287640084"
        ch.record_message(ch_id, "zero-chat", "Zero", True, "Previous reply", msg_id=300, timestamp="2026-09-03 04:00:00 UTC")
        ch.mark_channel_seen(ch_id, msg_id=300)

        # Interleaved posts between turns (04:12:11 UTC -> 09:12:11 PM PT)
        ch.record_message(ch_id, "zero-chat", "Zero", True, "📋 [Outbox] Morning briefing ready.", msg_id=301, timestamp="2026-09-03 04:12:11 UTC")
        ch.record_message(ch_id, "zero-chat", "Zero", True, "🪪 Decision Card: Approve NAS backup?", msg_id=302, timestamp="2026-09-03 04:15:00 UTC")
        # Inbound user message triggering next turn
        ch.record_message(ch_id, "zero-chat", "Ryan", False, "Approve it", msg_id=303, timestamp="2026-09-03 04:16:00 UTC")

        delta = ch.format_unseen_delta_context(ch_id, exclude_msg_id=303)
        self.assertIn("--- UNSEEN CHANNEL MESSAGES SINCE LAST TURN (#zero-chat, 2 messages, PT timezone) ---", delta)
        self.assertIn("--- END UNSEEN CHANNEL MESSAGES ---", delta)
        self.assertIn("[09:12:11 PM PT] Zero (bot): 📋 [Outbox] Morning briefing ready.", delta)
        self.assertIn("[09:15:00 PM PT] Zero (bot): 🪪 Decision Card: Approve NAS backup?", delta)
        self.assertNotIn("Previous reply", delta)
        self.assertNotIn("Approve it", delta)

    def test_mark_channel_seen_advances_monotonically_and_persists_atomically(self):
        """Watermark advances monotonically, defaults to latest recorded ID when msg_id=None, and reloads from disk."""
        ch_id = "1542081375287640084"
        ch.record_message(ch_id, "zero-chat", "Ryan", False, "Msg A", msg_id=400, timestamp="2026-11-20 18:00:00 UTC")
        ch.record_message(ch_id, "zero-chat", "Zero", True, "Msg B", msg_id=405, timestamp="2026-11-20 18:01:00 UTC")

        self.assertEqual(ch.mark_channel_seen(ch_id, msg_id=405), 405)
        # Should never regress to an older message ID
        self.assertEqual(ch.mark_channel_seen(ch_id, msg_id=400), 405)

        ch.record_message(ch_id, "zero-chat", "Zero", True, "Msg C", msg_id=410, timestamp="2026-11-20 18:02:00 UTC")
        self.assertEqual(ch.mark_channel_seen(ch_id), 410)

        # Verify atomic persistence & reload
        ch._seen_watermarks.clear()
        loaded = ch.load_watermarks()
        self.assertEqual(loaded.get(ch_id), 410)

    def test_deque_position_fallback_for_none_and_non_monotonic_ids(self):
        """Messages with None or non-monotonic IDs positioned after watermark entry in deque are included."""
        ch_id = "1542081375287640084"
        ch._history_store[ch_id] = deque([
            {"id": 500, "channel_id": ch_id, "channel_name": "zero-chat", "author": "Zero", "is_bot": True, "content": "Seen turn", "timestamp": "2026-11-20 18:00:00 UTC"},
            {"id": None, "channel_id": ch_id, "channel_name": "zero-chat", "author": "Zero", "is_bot": True, "content": "Sidecar no id", "timestamp": "2026-11-20 18:01:00 UTC"},
            {"id": 450, "channel_id": ch_id, "channel_name": "zero-chat", "author": "Zero", "is_bot": True, "content": "Non-monotonic post", "timestamp": "2026-11-20 18:02:00 UTC"},
            {"id": 502, "channel_id": ch_id, "channel_name": "zero-chat", "author": "Zero", "is_bot": True, "content": "Later post", "timestamp": "2026-11-20 18:03:00 UTC"},
        ])
        ch.mark_channel_seen(ch_id, msg_id=500)

        delta = ch.format_unseen_delta_context(ch_id)
        self.assertIn("3 messages", delta)
        self.assertIn("Sidecar no id", delta)
        self.assertIn("Non-monotonic post", delta)
        self.assertIn("Later post", delta)
        self.assertNotIn("Seen turn", delta)

    def test_max_chars_truncation_keeps_newest_messages(self):
        """When unseen messages exceed max_chars, keep newest messages within budget."""
        ch_id = "1542081375287640084"
        ch.mark_channel_seen(ch_id, msg_id=600)
        for i in range(1, 6):
            ch.record_message(
                ch_id,
                "zero-chat",
                "Zero",
                True,
                f"Alert #{i}: " + ("x" * 70),
                msg_id=600 + i,
                timestamp=f"2026-11-20 18:0{i}:00 UTC",
            )

        delta = ch.format_unseen_delta_context(ch_id, max_chars=340)
        self.assertLessEqual(len(delta), 340)
        self.assertIn("Alert #5:", delta)
        self.assertNotIn("Alert #1:", delta)
        self.assertIn("--- END UNSEEN CHANNEL MESSAGES ---", delta)


if __name__ == "__main__":
    unittest.main()
