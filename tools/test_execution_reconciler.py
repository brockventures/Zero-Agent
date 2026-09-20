#!/usr/bin/env python3
"""Regression test suite for Kalshi API v2 order state reconciler, partial fills, and fallback cascades."""
import json
import sqlite3
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo
from tools.kalshi_paper_bot import (
    reconcile_live_orders_and_positions,
    http_get_json
)
from tools.baseball_daily_quant import load_live_team_baselines

PT = ZoneInfo("America/Los_Angeles")

class TestKalshiExecutionReconciler(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("""
                CREATE TABLE portfolio (
                    id INTEGER PRIMARY KEY,
                    starting_balance REAL,
                    cash REAL,
                    realized_pnl REAL,
                    last_updated TEXT
                )
            """)
            self.conn.execute("INSERT INTO portfolio VALUES (1, 1000.0, 950.0, 0.0, '2026-09-19T12:00:00')")
            self.conn.execute("""
                CREATE TABLE positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT UNIQUE,
                    title TEXT,
                    side TEXT,
                    contracts INTEGER,
                    entry_price REAL,
                    total_cost REAL,
                    opened_at TEXT,
                    status TEXT,
                    settled_at TEXT,
                    settled_price REAL,
                    realized_pnl REAL,
                    model_prob REAL,
                    category TEXT
                )
            """)
            self.conn.execute("""
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    ticker TEXT,
                    side TEXT,
                    action TEXT,
                    contracts INTEGER,
                    price REAL,
                    fee REAL,
                    status TEXT,
                    reason TEXT
                )
            """)

    def test_partial_fill_reconciliation(self):
        now_str = datetime.now(PT).isoformat()
        with self.conn:
            self.conn.execute("""
                INSERT INTO orders (id, timestamp, ticker, side, action, contracts, price, fee, status, reason)
                VALUES (101, ?, 'KXHIGH-PARTIAL', 'YES', 'BUY_POST_ONLY', 100, 0.50, 0.00, 'RESTING', 'Post-Only Maker')
            """, (now_str,))

        # Remote Kalshi API reports 40 contracts filled, order still resting for remainder
        remote_orders = [{
            "order_id": 101,
            "ticker": "KXHIGH-PARTIAL",
            "status": "resting",
            "filled_count": 40,
            "contracts": 100
        }]

        stats = reconcile_live_orders_and_positions(self.conn, remote_orders=remote_orders, remote_positions=[])
        self.assertEqual(stats["partial_fills"], 1)

        # Local resting order contracts should decrease to 60
        cur = self.conn.execute("SELECT contracts, reason FROM orders WHERE id = 101")
        row = cur.fetchone()
        self.assertEqual(row["contracts"], 60)
        self.assertIn("Reconciled partial fill (40 filled)", row["reason"])

        # Position should be opened for 40 filled contracts ($20 cost)
        cur = self.conn.execute("SELECT * FROM positions WHERE ticker = 'KXHIGH-PARTIAL'")
        pos = cur.fetchone()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["contracts"], 40)
        self.assertEqual(pos["total_cost"], 20.00)

    def test_full_fill_reconciliation(self):
        now_str = datetime.now(PT).isoformat()
        with self.conn:
            self.conn.execute("""
                INSERT INTO orders (id, timestamp, ticker, side, action, contracts, price, fee, status, reason)
                VALUES (102, ?, 'KXHIGH-FULL', 'YES', 'BUY_POST_ONLY', 50, 0.40, 0.00, 'RESTING', 'Post-Only Maker')
            """, (now_str,))

        remote_orders = [{
            "order_id": 102,
            "ticker": "KXHIGH-FULL",
            "status": "executed",
            "filled_count": 50,
            "contracts": 50
        }]

        stats = reconcile_live_orders_and_positions(self.conn, remote_orders=remote_orders, remote_positions=[])
        self.assertEqual(stats["reconciled_orders"], 1)

        # Local order marked FILLED
        cur = self.conn.execute("SELECT status FROM orders WHERE id = 102")
        self.assertEqual(cur.fetchone()["status"], "FILLED")

        # Position opened for 50 contracts
        cur = self.conn.execute("SELECT contracts, total_cost FROM positions WHERE ticker = 'KXHIGH-FULL'")
        pos = cur.fetchone()
        self.assertEqual(pos["contracts"], 50)
        self.assertEqual(pos["total_cost"], 20.00)

    def test_orphan_order_pruned_and_cash_refunded(self):
        # Order placed 15 minutes ago that doesn't exist on remote Kalshi exchange
        old_time = (datetime.now(PT) - timedelta(minutes=15)).isoformat()
        with self.conn:
            self.conn.execute("""
                INSERT INTO orders (id, timestamp, ticker, side, action, contracts, price, fee, status, reason)
                VALUES (103, ?, 'KXHIGH-ORPHAN', 'YES', 'BUY_POST_ONLY', 100, 0.50, 0.00, 'RESTING', 'Post-Only Maker')
            """, (old_time,))

        stats = reconcile_live_orders_and_positions(self.conn, remote_orders=[], remote_positions=[])
        self.assertEqual(stats["orphans_pruned"], 1)
        self.assertEqual(stats["cash_refunded"], 50.00)

        # Order marked CANCELLED
        cur = self.conn.execute("SELECT status, reason FROM orders WHERE id = 103")
        row = cur.fetchone()
        self.assertEqual(row["status"], "CANCELLED")
        self.assertIn("orphan pruned", row["reason"])

        # Cash restored from 950 -> 1000
        cur = self.conn.execute("SELECT cash FROM portfolio WHERE id = 1")
        self.assertEqual(cur.fetchone()["cash"], 1000.0)

    def test_http_retry_and_fallback_cascade(self):
        # Primary URL fails with HTTP 503, fallback URL succeeds
        call_urls = []

        def mock_urlopen(req, timeout=8):
            url = req.full_url if hasattr(req, "full_url") else req
            call_urls.append(url)
            if "primary.weather.gov" in url:
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
            # Fallback URL returns mock JSON
            resp = MagicMock()
            resp.read.return_value = json.dumps({"fallback_success": True}).encode()
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            data = http_get_json(
                "https://primary.weather.gov/grid",
                max_retries=2,
                fallback_urls=["https://fallback.mesonet.edu/grid"]
            )
            self.assertTrue(data.get("fallback_success"))
            self.assertTrue(any("fallback.mesonet.edu" in u for u in call_urls))

    def test_database_cache_fallback(self):
        # When PostgreSQL connection drops, load_live_team_baselines returns fallback baselines gracefully
        with patch("tools.baseball_daily_quant.get_db_connection", side_effect=Exception("PostgreSQL host unreachable")):
            baselines = load_live_team_baselines()
            self.assertIsInstance(baselines, dict)
            self.assertGreater(len(baselines), 0)
            self.assertIn("LAD", baselines)

if __name__ == "__main__":
    unittest.main()
