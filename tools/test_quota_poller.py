#!/usr/bin/env python3
"""Unit tests for tools/quota_poller.py."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_WORKSPACE = Path(__file__).resolve().parent.parent
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
from tools.quota_poller import (
    format_attribution_text,
    format_quota_status_text,
    get_latest_quota,
    get_quota_attribution,
    init_db,
    sample_quota,
    throttled_post_turn_sample,
)


class TestQuotaPoller(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_telemetry.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

        # Create token_usage table for attribution tests
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_id INTEGER,
                channel_name TEXT,
                parent_channel_id INTEGER,
                parent_channel_name TEXT,
                is_thread INTEGER DEFAULT 0,
                thread_id INTEGER,
                conv_id TEXT,
                mode TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                thinking_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0
            );
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_init_db_creates_table_and_indices(self):
        cur = self.conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quota_snapshots';")
        self.assertIsNotNone(cur.fetchone())

        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_quota_snapshots_bucket_ts';")
        self.assertIsNotNone(cur.fetchone())

    def test_sample_quota_delta_and_reset(self):
        # Sample 1: Baseline
        buckets_1 = [
            {
                "bucket_id": "gemini-weekly",
                "name": "Weekly Limit",
                "window": "weekly",
                "remaining_fraction": 0.50,
                "reset_time": "2026-10-19T21:43:03Z",
                "group_name": "Gemini",
            }
        ]
        res1 = sample_quota(source="test", conn=self.conn, db_path=self.db_path, custom_buckets=buckets_1)
        self.assertEqual(res1["status"], "success")
        s1 = res1["snapshots"][0]
        self.assertIsNone(s1["prev_fraction"])
        self.assertEqual(s1["delta_fraction"], 0.0)
        self.assertEqual(s1["is_reset"], 0)

        # Sample 2: Burn 5%
        buckets_2 = [
            {
                "bucket_id": "gemini-weekly",
                "name": "Weekly Limit",
                "window": "weekly",
                "remaining_fraction": 0.45,
                "reset_time": "2026-10-19T21:43:03Z",
                "group_name": "Gemini",
            }
        ]
        res2 = sample_quota(source="test", conn=self.conn, db_path=self.db_path, custom_buckets=buckets_2)
        s2 = res2["snapshots"][0]
        self.assertAlmostEqual(s2["prev_fraction"], 0.50)
        self.assertAlmostEqual(s2["delta_fraction"], 0.05)
        self.assertEqual(s2["is_reset"], 0)

        # Sample 3: Cycle Reset (remaining jumps up to 1.0)
        buckets_3 = [
            {
                "bucket_id": "gemini-weekly",
                "name": "Weekly Limit",
                "window": "weekly",
                "remaining_fraction": 1.0,
                "reset_time": "2026-10-26T21:43:03Z",
                "group_name": "Gemini",
            }
        ]
        res3 = sample_quota(source="test", conn=self.conn, db_path=self.db_path, custom_buckets=buckets_3)
        s3 = res3["snapshots"][0]
        self.assertAlmostEqual(s3["prev_fraction"], 0.45)
        self.assertEqual(s3["is_reset"], 1)
        self.assertEqual(s3["delta_fraction"], 0.0)

    def test_get_latest_quota(self):
        buckets = [
            {
                "bucket_id": "gemini-weekly",
                "name": "Weekly",
                "window": "weekly",
                "remaining_fraction": 0.30,
                "reset_time": "2026-10-19T21:43:03Z",
                "group_name": "Gemini",
            },
            {
                "bucket_id": "gemini-5h",
                "name": "5-Hour",
                "window": "5h",
                "remaining_fraction": 0.85,
                "reset_time": "2026-10-16T22:15:26Z",
                "group_name": "Gemini",
            },
        ]
        sample_quota(source="test", conn=self.conn, db_path=self.db_path, custom_buckets=buckets)
        latest = get_latest_quota(conn=self.conn, db_path=self.db_path)
        self.assertIn("gemini-weekly", latest)
        self.assertEqual(latest["gemini-weekly"]["remaining_pct"], 30.0)
        self.assertEqual(latest["gemini-weekly"]["used_pct"], 70.0)
        self.assertIn("gemini-5h", latest)
        self.assertEqual(latest["gemini-5h"]["remaining_pct"], 85.0)

    def test_get_quota_attribution(self):
        t0 = 1000.0
        t1 = 1600.0
        t2 = 2200.0

        # Snapshot 1 at t0
        self.conn.execute(
            """
            INSERT INTO quota_snapshots (
                timestamp, datetime_utc, bucket_id, window, remaining_fraction,
                reset_time, prev_fraction, delta_fraction, is_reset, source
            ) VALUES (?, '2026-10-16T00:00:00Z', 'gemini-weekly', 'weekly', 0.50, '2026-10-19T21:43:03Z', NULL, 0.0, 0, 'test');
            """,
            (t0,),
        )

        # Token usage between t0 and t1: #zero-chat spent 80k, #lounge spent 20k
        self.conn.execute(
            """
            INSERT INTO token_usage (timestamp, channel_name, input_tokens, output_tokens, total_tokens)
            VALUES (?, 'zero-chat', 60000, 20000, 80000);
            """,
            (t0 + 100,),
        )
        self.conn.execute(
            """
            INSERT INTO token_usage (timestamp, channel_name, input_tokens, output_tokens, total_tokens)
            VALUES (?, 'lounge', 15000, 5000, 20000);
            """,
            (t0 + 200,),
        )

        # Snapshot 2 at t1: burned 0.04 (4% quota)
        self.conn.execute(
            """
            INSERT INTO quota_snapshots (
                timestamp, datetime_utc, bucket_id, window, remaining_fraction,
                reset_time, prev_fraction, delta_fraction, is_reset, source
            ) VALUES (?, '2026-10-16T00:10:00Z', 'gemini-weekly', 'weekly', 0.46, '2026-10-19T21:43:03Z', 0.50, 0.04, 0, 'test');
            """,
            (t1,),
        )

        # Snapshot 3 at t2: burned 0.01 (1% quota), but NO zero turns logged (Ivy external burn)
        self.conn.execute(
            """
            INSERT INTO quota_snapshots (
                timestamp, datetime_utc, bucket_id, window, remaining_fraction,
                reset_time, prev_fraction, delta_fraction, is_reset, source
            ) VALUES (?, '2026-10-16T00:20:00Z', 'gemini-weekly', 'weekly', 0.45, '2026-10-19T21:43:03Z', 0.46, 0.01, 0, 'test');
            """,
            (t2,),
        )
        self.conn.commit()

        # Patch time.time() to t2 + 100 so since_ts covers t0..t2
        with patch("time.time", return_value=t2 + 100):
            attr = get_quota_attribution(hours=1.0, bucket_id="gemini-weekly", conn=self.conn, db_path=self.db_path)

        self.assertAlmostEqual(attr["total_quota_pct_burned"], 5.0, places=2)
        self.assertAlmostEqual(attr["zero_quota_pct_burned"], 4.0, places=2)
        self.assertAlmostEqual(attr["external_quota_pct_burned"], 1.0, places=2)

        # Check per-channel shares: zero-chat had 80% of 4% = 3.2%; lounge had 20% of 4% = 0.8%
        channels = {b["channel"]: b for b in attr["breakdown"]}
        self.assertIn("zero-chat", channels)
        self.assertIn("lounge", channels)
        self.assertIn("ivy_or_external", channels)

        self.assertAlmostEqual(channels["zero-chat"]["quota_pct_burned"], 3.2, places=2)
        self.assertAlmostEqual(channels["lounge"]["quota_pct_burned"], 0.8, places=2)
        self.assertAlmostEqual(channels["ivy_or_external"]["quota_pct_burned"], 1.0, places=2)

    def test_format_attribution_and_status(self):
        latest = {
            "gemini-weekly": {
                "remaining_pct": 28.5,
                "used_pct": 71.5,
                "countdown": "3d 4h",
                "reset_pt": "Mon 2:43 PM PT",
            },
            "gemini-5h": {
                "remaining_pct": 95.0,
                "countdown": "4h 10m",
            },
            "3p-weekly": {
                "remaining_pct": 55.0,
                "used_pct": 45.0,
                "countdown": "4d 1h",
                "reset_pt": "Tue 10:00 AM PT",
            },
            "3p-5h": {
                "remaining_pct": 80.0,
                "countdown": "2h 30m",
            },
        }
        text = format_quota_status_text(latest)
        self.assertIn("Gemini Weekly", text)
        self.assertIn("28.5%", text)
        self.assertIn("Gemini 5-Hour", text)
        self.assertNotIn("3P Weekly", text)
        self.assertNotIn("3P 5-Hour", text)

        text_with_3p = format_quota_status_text(latest, include_3p=True)
        self.assertIn("3P Weekly", text_with_3p)
        self.assertIn("3P 5-Hour", text_with_3p)

        attr_data = {
            "window_hours": 24,
            "total_quota_pct_burned": 5.0,
            "zero_quota_pct_burned": 4.0,
            "external_quota_pct_burned": 1.0,
            "breakdown": [
                {"channel": "zero-chat", "quota_pct_burned": 3.2, "share_of_total_burn_pct": 64.0, "total_tokens": 80000},
                {"channel": "ivy_or_external", "quota_pct_burned": 1.0, "share_of_total_burn_pct": 20.0, "total_tokens": 0},
            ],
        }
        attr_text = format_attribution_text(attr_data)
        self.assertIn("5.00%", attr_text)
        self.assertIn("zero-chat", attr_text)


if __name__ == "__main__":
    unittest.main()
