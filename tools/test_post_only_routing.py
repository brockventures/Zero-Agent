#!/usr/bin/env python3
"""Regression test suite for Kalshi passive post-only resting limit order routing."""
import sqlite3
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from tools.kalshi_paper_bot import submit_post_only_order, process_resting_orders

PT = ZoneInfo("America/Los_Angeles")

class TestKalshiPostOnlyRouting(unittest.TestCase):
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
            self.conn.execute("INSERT INTO portfolio VALUES (1, 1000.0, 1000.0, 0.0, '2026-09-19T12:00:00')")
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

    def test_post_only_submission_zero_fee(self):
        opp = {
            "ticker": "TEST-KXHIGH-T70",
            "title": "Will SFO high be >= 70?",
            "side": "YES",
            "market_price": 0.28,
            "market_bid": 0.25,
            "edge": 0.12,
            "model_prob": 0.40,
            "category": "weather"
        }
        ok, msg = submit_post_only_order(self.conn, opp, timeout_minutes=15)
        self.assertTrue(ok)
        self.assertIn("0% fee", msg)
        
        # Verify order in DB
        cur = self.conn.execute("SELECT * FROM orders WHERE ticker = 'TEST-KXHIGH-T70'")
        order = cur.fetchone()
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], "RESTING")
        self.assertEqual(order["fee"], 0.00)
        self.assertEqual(order["action"], "BUY_POST_ONLY")
        
        # Cash should be earmarked
        cur = self.conn.execute("SELECT cash FROM portfolio WHERE id = 1")
        cash = cur.fetchone()["cash"]
        self.assertLess(cash, 1000.0)

    def test_resting_order_timeout_cancellation(self):
        # Insert expired resting order (placed 20 minutes ago)
        old_time = (datetime.now(PT) - timedelta(minutes=20)).isoformat()
        with self.conn:
            self.conn.execute("UPDATE portfolio SET cash = 950.0 WHERE id = 1")
            self.conn.execute("""
                INSERT INTO orders (timestamp, ticker, side, action, contracts, price, fee, status, reason)
                VALUES (?, 'OLD-TICKER', 'YES', 'BUY_POST_ONLY', 100, 0.50, 0.00, 'RESTING', 'Post-Only')
            """, (old_time,))
            
        cancelled, filled = process_resting_orders(self.conn, max_age_minutes=15, force_fill=False)
        self.assertEqual(cancelled, 1)
        self.assertEqual(filled, 0)
        
        # Verify order marked cancelled
        cur = self.conn.execute("SELECT status FROM orders WHERE ticker = 'OLD-TICKER'")
        self.assertEqual(cur.fetchone()["status"], "CANCELLED")
        
        # Cash refunded: 950 + 50 = 1000
        cur = self.conn.execute("SELECT cash FROM portfolio WHERE id = 1")
        self.assertEqual(cur.fetchone()["cash"], 1000.0)

    def test_resting_order_fill_to_position(self):
        # Insert recent resting order (placed 2 minutes ago)
        recent_time = (datetime.now(PT) - timedelta(minutes=2)).isoformat()
        with self.conn:
            self.conn.execute("UPDATE portfolio SET cash = 975.0 WHERE id = 1")
            self.conn.execute("""
                INSERT INTO orders (timestamp, ticker, side, action, contracts, price, fee, status, reason)
                VALUES (?, 'RECENT-TICKER', 'YES', 'BUY_POST_ONLY', 50, 0.50, 0.00, 'RESTING', 'Post-Only')
            """, (recent_time,))
            
        cancelled, filled = process_resting_orders(self.conn, max_age_minutes=15, force_fill=True)
        self.assertEqual(filled, 1)
        self.assertEqual(cancelled, 0)
        
        # Verify order marked filled and position opened with $0 fee
        cur = self.conn.execute("SELECT status FROM orders WHERE ticker = 'RECENT-TICKER'")
        self.assertEqual(cur.fetchone()["status"], "FILLED")
        
        cur = self.conn.execute("SELECT * FROM positions WHERE ticker = 'RECENT-TICKER'")
        pos = cur.fetchone()
        self.assertIsNotNone(pos)
        self.assertEqual(pos["status"], "OPEN")
        self.assertEqual(pos["entry_price"], 0.50)

if __name__ == "__main__":
    unittest.main()
