#!/usr/bin/env python3
"""Unit tests for tools/quota_poller.py."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_WORKSPACE = Path(__file__).resolve().parent.parent
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
if str(_WORKSPACE) not in sys.path or sys.path[0] != str(_WORKSPACE):
    sys.path.insert(0, str(_WORKSPACE))
from tools.quota_poller import (
    DAILY_QUOTA_DOLLAR_BUDGET,
    DEFAULT_RATE_CARD_MODEL,
    RATE_CARDS,
    WEEKLY_QUOTA_DOLLAR_POOL,
    compute_rate_card_cost,
    format_attribution_text,
    format_quota_status_text,
    get_latest_quota,
    get_quota_attribution,
    init_db,
    sample_quota,
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
        self.assertAlmostEqual(channels["zero-chat"]["rate_card_cost_usd"], 0.96, places=2)
        self.assertAlmostEqual(channels["lounge"]["rate_card_cost_usd"], 0.24, places=2)
        self.assertAlmostEqual(attr["total_rate_card_cost_usd"], 1.20, places=2)
        self.assertAlmostEqual(attr["rate_card_cost_usd"], 1.20, places=2)
        self.assertEqual(attr["weekly_dollar_pool"], 6000.0)
        self.assertEqual(attr["daily_dollar_budget"], 857.14)

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

    def test_monotonic_low_water_mark_prevents_jitter_double_counting(self):
        # 1. Start at 99.0%
        b1 = [{"bucket_id": "gemini-weekly", "name": "W", "window": "weekly", "remaining_fraction": 0.990, "reset_time": "2026-10-24T00:06:03Z", "group_name": "Gemini"}]
        sample_quota(source="test", conn=self.conn, db_path=self.db_path, custom_buckets=b1)

        # 2. Burn down to 98.0% (delta = 0.010)
        b2 = [{"bucket_id": "gemini-weekly", "name": "W", "window": "weekly", "remaining_fraction": 0.980, "reset_time": "2026-10-24T00:06:03Z", "group_name": "Gemini"}]
        r2 = sample_quota(source="test", conn=self.conn, db_path=self.db_path, custom_buckets=b2)
        self.assertAlmostEqual(r2["snapshots"][0]["delta_fraction"], 0.010, places=5)

        # 3. Rolling window ticks upward to 98.5% (delta = 0.0)
        b3 = [{"bucket_id": "gemini-weekly", "name": "W", "window": "weekly", "remaining_fraction": 0.985, "reset_time": "2026-10-24T00:06:03Z", "group_name": "Gemini"}]
        r3 = sample_quota(source="test", conn=self.conn, db_path=self.db_path, custom_buckets=b3)
        self.assertAlmostEqual(r3["snapshots"][0]["delta_fraction"], 0.0, places=5)

        # 4. Drops back to 97.9% — only 0.1% below the 98.0% low-water mark (NOT 0.6% from 98.5%!)
        b4 = [{"bucket_id": "gemini-weekly", "name": "W", "window": "weekly", "remaining_fraction": 0.979, "reset_time": "2026-10-24T00:06:03Z", "group_name": "Gemini"}]
        r4 = sample_quota(source="test", conn=self.conn, db_path=self.db_path, custom_buckets=b4)
        self.assertAlmostEqual(r4["snapshots"][0]["delta_fraction"], 0.001, places=5)

        attr = get_quota_attribution(hours=1.0, bucket_id="gemini-weekly", conn=self.conn, db_path=self.db_path)
        self.assertAlmostEqual(attr["total_quota_pct_burned"], 1.1, places=2)

    def test_compute_rate_card_cost_argon_and_flash(self):
        # Gemini 4 Argon: $6.00 input / $30.00 output+thinking / $0.30 cache_read per 1M
        self.assertEqual(DEFAULT_RATE_CARD_MODEL, "gemini-4-argon")
        self.assertIn("gemini-4-argon", RATE_CARDS)
        self.assertIn("gemini-3.8-flash", RATE_CARDS)

        # 1M wire tokens individual tests (Argon)
        self.assertAlmostEqual(compute_rate_card_cost(input_tokens=1_000_000, model="gemini-4-argon"), 6.00)
        self.assertAlmostEqual(compute_rate_card_cost(output_tokens=1_000_000, model="gemini-4-argon"), 30.00)
        self.assertAlmostEqual(compute_rate_card_cost(thinking_tokens=1_000_000, model="gemini-4-argon"), 30.00)
        self.assertAlmostEqual(compute_rate_card_cost(cache_read_tokens=1_000_000, model="gemini-4-argon"), 0.30)

        # Combined Argon calculation:
        # 500k input ($3.00) + 100k out ($3.00) + 100k thinking ($3.00) + 2M cache ($0.60) = $9.60
        cost_argon = compute_rate_card_cost(
            input_tokens=500_000,
            output_tokens=100_000,
            thinking_tokens=100_000,
            cache_read_tokens=2_000_000,
            model="gemini-4-argon",
        )
        self.assertAlmostEqual(cost_argon, 9.60)

        # Gemini 3.8 Flash: $0.75 input / $3.75 output+thinking / $0.075 cache_read per 1M
        self.assertAlmostEqual(compute_rate_card_cost(input_tokens=1_000_000, model="gemini-3.8-flash"), 0.75)
        self.assertAlmostEqual(compute_rate_card_cost(output_tokens=1_000_000, model="gemini-3.8-flash"), 3.75)
        self.assertAlmostEqual(compute_rate_card_cost(thinking_tokens=1_000_000, model="gemini-3.8-flash"), 3.75)
        self.assertAlmostEqual(compute_rate_card_cost(cache_read_tokens=1_000_000, model="gemini-3.8-flash"), 0.075)

        # Flash model string resolution (e.g. 'flash' resolves to 'gemini-3.8-flash')
        cost_flash = compute_rate_card_cost(
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            model="flash",
        )
        self.assertAlmostEqual(cost_flash, 4.50)

        # Default fallback to Argon
        cost_default = compute_rate_card_cost(input_tokens=1_000_000)
        self.assertAlmostEqual(cost_default, 6.00)

        # Edge cases: negatives and zeros
        self.assertAlmostEqual(compute_rate_card_cost(input_tokens=-1000, output_tokens=-500), 0.0)

    def test_quota_attribution_weights_cache_and_thinking_by_rate_card(self):
        t0 = 5000.0
        t1 = 5600.0

        # Snapshot 1 at t0
        self.conn.execute(
            """
            INSERT INTO quota_snapshots (
                timestamp, datetime_utc, bucket_id, window, remaining_fraction,
                reset_time, prev_fraction, delta_fraction, is_reset, source
            ) VALUES (?, '2026-10-16T12:00:00Z', 'gemini-weekly', 'weekly', 0.50, '2026-10-19T21:43:03Z', NULL, 0.0, 0, 'test');
            """,
            (t0,),
        )

        # Channel 1: #chat-heavy-cache has high wire tokens (1.02M) mostly in cache reads ($0.30/1M)
        # 10k in ($0.06) + 10k out ($0.30) + 1M cache ($0.30) = $0.66
        self.conn.execute(
            """
            INSERT INTO token_usage (
                timestamp, channel_name, input_tokens, output_tokens, cache_read_tokens, thinking_tokens, total_tokens
            ) VALUES (?, 'chat-heavy-cache', 10000, 10000, 1000000, 0, 1020000);
            """,
            (t0 + 100,),
        )

        # Channel 2: #deep-thinker has lower wire tokens (110k) but high thinking tokens ($30.00/1M)
        # 10k in ($0.06) + 20k out + 80k thinking ($3.00) + 0 cache = $3.06
        self.conn.execute(
            """
            INSERT INTO token_usage (
                timestamp, channel_name, input_tokens, output_tokens, cache_read_tokens, thinking_tokens, total_tokens
            ) VALUES (?, 'deep-thinker', 10000, 20000, 0, 80000, 110000);
            """,
            (t0 + 200,),
        )

        # Snapshot 2 at t1: burned 0.10 (10% of weekly quota)
        self.conn.execute(
            """
            INSERT INTO quota_snapshots (
                timestamp, datetime_utc, bucket_id, window, remaining_fraction,
                reset_time, prev_fraction, delta_fraction, is_reset, source
            ) VALUES (?, '2026-10-16T12:10:00Z', 'gemini-weekly', 'weekly', 0.40, '2026-10-19T21:43:03Z', 0.50, 0.10, 0, 'test');
            """,
            (t1,),
        )
        self.conn.commit()

        # Total cost = 0.66 + 3.06 = 3.72
        # Deep-thinker share = 3.06 / 3.72 = ~82.26%
        # Chat-heavy-cache share = 0.66 / 3.72 = ~17.74%
        with patch("time.time", return_value=t1 + 50):
            attr = get_quota_attribution(hours=1.0, bucket_id="gemini-weekly", conn=self.conn, db_path=self.db_path)

        self.assertAlmostEqual(attr["total_quota_pct_burned"], 10.0, places=2)
        self.assertAlmostEqual(attr["rate_card_cost_usd"], 3.72, places=2)
        self.assertEqual(attr["weekly_dollar_pool"], WEEKLY_QUOTA_DOLLAR_POOL)
        self.assertEqual(attr["daily_dollar_budget"], DAILY_QUOTA_DOLLAR_BUDGET)

        channels = {b["channel"]: b for b in attr["breakdown"]}
        self.assertIn("chat-heavy-cache", channels)
        self.assertIn("deep-thinker", channels)

        # Without rate cards, chat-heavy-cache would have 90% of tokens and 90% of attribution.
        # With rate cards, deep-thinker correctly receives ~82.3% of the burned quota!
        self.assertGreater(channels["deep-thinker"]["quota_pct_burned"], channels["chat-heavy-cache"]["quota_pct_burned"])
        self.assertAlmostEqual(channels["deep-thinker"]["quota_pct_burned"], 8.226, places=2)
        self.assertAlmostEqual(channels["chat-heavy-cache"]["quota_pct_burned"], 1.774, places=2)
        self.assertEqual(channels["chat-heavy-cache"]["cache_tokens"], 1_000_000)
        self.assertEqual(channels["deep-thinker"]["thinking_tokens"], 80_000)
        self.assertAlmostEqual(channels["deep-thinker"]["rate_card_cost_usd"], 3.06, places=2)
        self.assertAlmostEqual(channels["chat-heavy-cache"]["rate_card_cost_usd"], 0.66, places=2)

        # Test format_attribution_text displays rate card equivalent cost
        text = format_attribution_text(attr)
        self.assertIn("$3.72", text)
        self.assertIn("$3.06 eq.", text)
        self.assertIn("$0.66 eq.", text)

    @patch("tools.quota_poller.is_system_load_elevated", return_value=(True, 28.4))
    @patch("tools.quota_poller.sample_quota")
    def test_high_load_defers_poller_and_post_turn_sample(self, mock_sample, _mock_load):
        from tools.quota_poller import run_quota_poller, throttled_post_turn_sample
        import tools.quota_poller as qp

        qp._LAST_SAMPLE_TS = 0.0
        self.assertIsNone(throttled_post_turn_sample(min_interval=0.0))
        mock_sample.assert_not_called()

        ok, msg, data = run_quota_poller(test=False, quiet=False)
        self.assertTrue(ok)
        self.assertEqual(data.get("status"), "deferred_high_load")
        self.assertIn("28.4", msg)
        mock_sample.assert_not_called()


if __name__ == "__main__":
    unittest.main()
