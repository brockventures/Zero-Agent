#!/usr/bin/env python3
"""Regression test suite for Kalshi dynamic adverse selection filter, multi-level OFI, and Avellaneda-Stoikov skew."""
import sqlite3
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from tools.kalshi_paper_bot import (
    calculate_multi_level_ofi,
    calculate_avellaneda_stoikov_quote,
    evaluate_toxic_flow_cancellation,
    submit_post_only_order,
    process_resting_orders
)

PT = ZoneInfo("America/Los_Angeles")

class TestKalshiAdverseSelectionOFI(unittest.TestCase):
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

    def test_multi_level_ofi_calculation(self):
        # Baseline order book:
        # YES bids at 0.25 (10 contracts), 0.24 (20 contracts), 0.23 (30 contracts)
        # YES asks (derived from NO bids: ask = 1.0 - no_bid):
        # NO bids at 0.70 (ask YES = 0.30, 10 contracts), 0.69 (ask YES = 0.31, 15 contracts)
        ob_prev = {
            "yes_dollars": [["0.25", "10.0"], ["0.24", "20.0"], ["0.23", "30.0"]],
            "no_dollars": [["0.70", "10.0"], ["0.69", "15.0"]]
        }
        
        # Scenario A: Toxic selling pressure against YES bids (Level 0 bid swept from 10 to 2, Ask deepened by 20)
        # Delta B0 = -8.0, Delta A0 = +20.0 => I0 = -8.0 - 20.0 = -28.0
        ob_toxic = {
            "yes_dollars": [["0.25", "2.0"], ["0.24", "20.0"], ["0.23", "30.0"]],
            "no_dollars": [["0.70", "30.0"], ["0.69", "15.0"]]
        }
        ofi_toxic = calculate_multi_level_ofi(ob_prev, ob_toxic, side="YES")
        self.assertLess(ofi_toxic, -15.0)
        self.assertEqual(ofi_toxic, -28.0)

        # Scenario B: Bullish buying pressure (YES bids deepen by 15, Ask level lifted)
        ob_bullish = {
            "yes_dollars": [["0.25", "25.0"], ["0.24", "20.0"], ["0.23", "30.0"]],
            "no_dollars": [["0.70", "0.0"], ["0.69", "15.0"]]
        }
        ofi_bull = calculate_multi_level_ofi(ob_prev, ob_bullish, side="YES")
        self.assertGreater(ofi_bull, 15.0)

    def test_avellaneda_stoikov_inventory_skew(self):
        base_bid = 0.50
        # 0 inventory -> quote remains unshaded
        q0 = calculate_avellaneda_stoikov_quote(base_bid, inventory=0)
        self.assertEqual(q0, 0.50)

        # Inventory buildup -> quotes shade downward monotonically
        q10 = calculate_avellaneda_stoikov_quote(base_bid, inventory=10, gamma=0.05, sigma=0.20)
        q25 = calculate_avellaneda_stoikov_quote(base_bid, inventory=25, gamma=0.05, sigma=0.20)
        q50 = calculate_avellaneda_stoikov_quote(base_bid, inventory=50, gamma=0.05, sigma=0.20)

        self.assertLess(q10, q0)
        self.assertLess(q25, q10)
        self.assertLess(q50, q25)
        # Verify exact Avellaneda-Stoikov formula: skew = q * gamma * sigma^2 = 10 * 0.05 * 0.04 = 0.02
        self.assertAlmostEqual(q10, 0.48, places=4)
        self.assertAlmostEqual(q50, 0.40, places=4)

    def test_toxic_flow_cancellation_and_cash_refund(self):
        # Place resting order
        now_str = datetime.now(PT).isoformat()
        with self.conn:
            self.conn.execute("UPDATE portfolio SET cash = 950.0 WHERE id = 1")
            self.conn.execute("""
                INSERT INTO orders (timestamp, ticker, side, action, contracts, price, fee, status, reason)
                VALUES (?, 'KXHIGH-OFI-TEST', 'YES', 'BUY_POST_ONLY', 100, 0.50, 0.00, 'RESTING', 'Post-Only')
            """, (now_str,))

        cur = self.conn.execute("SELECT * FROM orders WHERE ticker = 'KXHIGH-OFI-TEST'")
        order = cur.fetchone()

        ob_prev = {
            "yes_dollars": [["0.50", "50.0"]],
            "no_dollars": [["0.50", "20.0"]]
        }
        # Toxic book event: Bid wiped out to 5 contracts (-45), Ask flooded (+30)
        ob_toxic = {
            "yes_dollars": [["0.50", "5.0"]],
            "no_dollars": [["0.50", "50.0"]]
        }

        cancelled, ofi_val, reason = evaluate_toxic_flow_cancellation(
            self.conn, order, ob_prev, ob_toxic, ofi_threshold=-10.0
        )
        self.assertTrue(cancelled)
        self.assertIn("Toxic flow / adverse OFI detected", reason)

        # Order must be marked CANCELLED
        cur = self.conn.execute("SELECT status, reason FROM orders WHERE id = ?", (order["id"],))
        updated_order = cur.fetchone()
        self.assertEqual(updated_order["status"], "CANCELLED")

        # Earmarked cash ($50.00) must be 100% refunded to portfolio (950 + 50 = 1000)
        cur = self.conn.execute("SELECT cash FROM portfolio WHERE id = 1")
        self.assertEqual(cur.fetchone()["cash"], 1000.0)

    def test_process_resting_orders_toxic_flow_integration(self):
        # Insert resting order into DB
        now_str = datetime.now(PT).isoformat()
        with self.conn:
            self.conn.execute("UPDATE portfolio SET cash = 900.0 WHERE id = 1")
            self.conn.execute("""
                INSERT INTO orders (timestamp, ticker, side, action, contracts, price, fee, status, reason)
                VALUES (?, 'KXHIGH-SFO-REST', 'YES', 'BUY_POST_ONLY', 100, 0.50, 0.00, 'RESTING', 'Post-Only')
            """, (now_str,))

        ob_prev = {"yes_dollars": [["0.50", "40.0"]], "no_dollars": [["0.50", "10.0"]]}
        ob_curr = {"yes_dollars": [["0.50", "5.0"]], "no_dollars": [["0.50", "40.0"]]}

        orderbook_pairs = {
            "KXHIGH-SFO-REST": (ob_prev, ob_curr)
        }

        cancelled_count, filled_count = process_resting_orders(
            self.conn, max_age_minutes=15, force_fill=False, orderbook_pairs=orderbook_pairs
        )
        self.assertEqual(cancelled_count, 1)
        self.assertEqual(filled_count, 0)

        # Check DB state
        cur = self.conn.execute("SELECT status FROM orders WHERE ticker = 'KXHIGH-SFO-REST'")
        self.assertEqual(cur.fetchone()["status"], "CANCELLED")

        # Cash restored
        cur = self.conn.execute("SELECT cash FROM portfolio WHERE id = 1")
        self.assertEqual(cur.fetchone()["cash"], 950.0)

if __name__ == "__main__":
    unittest.main()
