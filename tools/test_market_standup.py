#!/usr/bin/env python3
"""
test_market_standup.py - Unit tests for Market Standup Discord evening chat ingestion and agenda synthesis.
"""

import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json

from tools.market_standup import (
    datetime_to_snowflake,
    get_previous_evening_window,
    format_chat_transcript,
    fetch_evening_discord_messages,
    dispatch_market_standup,
    PT,
    DISCORD_EPOCH,
)


class TestMarketStandup(unittest.TestCase):
    def test_datetime_to_snowflake(self):
        """Verify Discord snowflake timestamp encoding."""
        # 2026-01-01 00:00:00 UTC
        dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        ms = int(dt.timestamp() * 1000)
        expected = (ms - DISCORD_EPOCH) << 22
        self.assertEqual(datetime_to_snowflake(dt), expected)

    def test_get_previous_evening_window(self):
        """Verify the previous day 7:00 PM to 11:59:59 PM PT window calculation."""
        test_now = datetime(2026, 9, 4, 19, 0, 0, tzinfo=PT)
        start_pt, end_pt = get_previous_evening_window(test_now)
        
        self.assertEqual(start_pt.year, 2026)
        self.assertEqual(start_pt.month, 9)
        self.assertEqual(start_pt.day, 3)
        self.assertEqual(start_pt.hour, 19)
        self.assertEqual(start_pt.minute, 0)
        self.assertEqual(start_pt.second, 0)

        self.assertEqual(end_pt.year, 2026)
        self.assertEqual(end_pt.month, 9)
        self.assertEqual(end_pt.day, 3)
        self.assertEqual(end_pt.hour, 23)
        self.assertEqual(end_pt.minute, 59)
        self.assertEqual(end_pt.second, 59)

    def test_format_chat_transcript(self):
        """Verify message formatting, thread tags, and character capping."""
        sample_msgs = [
            {
                "author": {"username": "Amos"},
                "content": "Ledger verified clean with double-entry invariant.",
            },
            {
                "author": {"username": "Zero"},
                "content": "Wire spec PR #2 is posted and ready for review.",
                "is_thread": True,
            },
            {
                "author": {"username": "Marvin"},
                "content": "X" * 600,  # Long content to test truncation
            },
        ]
        transcript = format_chat_transcript(sample_msgs, max_chars=1000)
        self.assertIn("Amos: Ledger verified clean", transcript)
        self.assertIn("[Thread] Zero: Wire spec PR #2", transcript)
        self.assertIn("Marvin: " + ("X" * 400) + "...", transcript)

    @patch("urllib.request.urlopen")
    def test_fetch_evening_discord_messages_pagination(self, mock_urlopen):
        """Verify Discord message fetching handles pagination and bounds checking."""
        batch_1 = [
            {"id": "100000000000000001", "content": "msg 1", "author": {"username": "A"}},
            {"id": "100000000000000002", "content": "msg 2", "author": {"username": "B"}},
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(batch_1).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        start_dt = datetime(2026, 9, 3, 19, 0, tzinfo=PT)
        end_dt = datetime(2026, 9, 3, 23, 59, tzinfo=PT)

        with patch("tools.market_standup.datetime_to_snowflake", side_effect=[100, 200000000000000000]):
            msgs = fetch_evening_discord_messages(
                channel_id="123",
                start_pt=start_dt,
                end_pt=end_dt,
                token="test_token"
            )
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["content"], "msg 1")

    def test_fetch_evening_discord_messages_no_token(self):
        """Verify fetch returns empty list when no token is available."""
        with patch("tools.market_standup.get_discord_bot_token", return_value=""):
            msgs = fetch_evening_discord_messages(channel_id="123", token="")
            self.assertEqual(msgs, [])

    @patch("tools.market_standup.fetch_evening_discord_messages")
    @patch("tools.market_standup.get_repo_state")
    @patch("tools.market_standup.synthesize_standing_agenda")
    def test_dispatch_market_standup_test_mode(self, mock_synth, mock_repo, mock_fetch):
        """Verify test mode runs without claiming banana mutex or queueing outbox."""
        mock_repo.return_value = {"open_prs": [], "recent_commits": []}
        mock_fetch.return_value = [{"id": "1", "content": "test", "author": {"username": "Zero"}}]
        mock_synth.return_value = "1. Step 1\n2. Step 2\n3. Step 3"

        with patch("tools.market_standup.claim") as mock_claim, \
             patch("tools.market_standup.queue_outbox_message") as mock_queue:
            res = dispatch_market_standup(test_mode=True, quiet=True)
            self.assertEqual(res["status"], "ok")
            self.assertTrue(res["test"])
            self.assertEqual(res["evening_messages"], 1)
            mock_claim.assert_not_called()
            mock_queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
