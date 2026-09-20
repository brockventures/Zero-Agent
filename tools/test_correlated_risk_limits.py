#!/usr/bin/env python3
"""Regression test suite for Kalshi correlated portfolio Kelly sizing, sector caps, and intraday circuit breaker."""
import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from tools.kalshi_paper_bot import (
    calculate_covariance_scaling,
    check_exposure_ceilings,
    check_intraday_circuit_breaker,
    submit_post_only_order,
    execute_trade
)

PT = ZoneInfo("America/Los_Angeles")

class TestKalshiCorrelatedRiskLimits(unittest.TestCase):
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

    def test_covariance_scaling_independent_market(self):
        # Empty portfolio -> 1.0 scaling (no penalty)
        scaling = calculate_covariance_scaling("KXHIGH-26SEP19-ORD", [])
        self.assertEqual(scaling, 1.0)

    def test_covariance_scaling_correlated_cluster(self):
        # When ORD already exists on 26SEP19, MDW on same date and cluster receives covariance penalty
        open_positions = [{
            "ticker": "KXHIGH-26SEP19-ORD-T70",
            "total_cost": 10.0,
            "category": "weather"
        }]
        scaling = calculate_covariance_scaling("KXHIGH-26SEP19-MDW-T72", open_positions, max_trade_risk=10.0)
        self.assertLess(scaling, 0.60)
        self.assertGreaterEqual(scaling, 0.10)

    def test_sector_exposure_ceiling_rejection(self):
        # Insert $145.00 worth of open weather positions
        now_str = datetime.now(PT).isoformat()
        with self.conn:
            self.conn.execute("""
                INSERT INTO positions (ticker, title, side, contracts, entry_price, total_cost, opened_at, status, model_prob, category)
                VALUES ('KXHIGH-EXISTING', 'Existing Weather', 'YES', 290, 0.50, 145.00, ?, 'OPEN', 0.60, 'weather')
            """, (now_str,))

        # Try to allocate $10.00 more to weather (145 + 10 = 155 > 150 ceiling)
        ok, msg = check_exposure_ceilings(self.conn, "KXHIGH-NEW", "weather", allocated_risk=10.0, max_cap=150.00)
        self.assertFalse(ok)
        self.assertIn("Sector exposure limit hit for weather", msg)

    def test_settlement_date_exposure_ceiling(self):
        # Insert $146.00 worth of open positions for 26SEP19 settlement date
        now_str = datetime.now(PT).isoformat()
        with self.conn:
            self.conn.execute("""
                INSERT INTO positions (ticker, title, side, contracts, entry_price, total_cost, opened_at, status, model_prob, category)
                VALUES ('KXMLB-26SEP19-EXISTING', 'MLB Game', 'YES', 292, 0.50, 146.00, ?, 'OPEN', 0.60, 'mlb_daily')
            """, (now_str,))

        # Try to allocate $8.00 more to same date (146 + 8 = 154 > 150 ceiling)
        ok, msg = check_exposure_ceilings(self.conn, "KXHIGH-26SEP19-ORD", "weather", allocated_risk=8.0, max_cap=150.00)
        self.assertFalse(ok)
        self.assertIn("Settlement date exposure limit hit for 26SEP19", msg)

    def test_intraday_circuit_breaker_trips_and_cancels_orders(self):
        today_str = datetime.now(PT).strftime("%Y-%m-%d")
        now_str = datetime.now(PT).isoformat()
        
        # 1. Insert daily resolved losing trade with -$55.00 realized PnL
        with self.conn:
            self.conn.execute("""
                INSERT INTO positions (ticker, title, side, contracts, entry_price, total_cost, opened_at, status, settled_at, settled_price, realized_pnl, model_prob, category)
                VALUES ('SETTLED-LOSS', 'Losing Trade', 'YES', 100, 0.55, 55.00, ?, 'RESOLVED', ?, 0.00, -55.00, 0.70, 'weather')
            """, (now_str, f"{today_str}T10:00:00"))
            
            # 2. Insert an active resting order
            self.conn.execute("UPDATE portfolio SET cash = 945.0 WHERE id = 1")
            self.conn.execute("""
                INSERT INTO orders (timestamp, ticker, side, action, contracts, price, fee, status, reason)
                VALUES (?, 'RESTING-TEST', 'YES', 'BUY_POST_ONLY', 100, 0.50, 0.00, 'RESTING', 'Post-Only Maker')
            """, (now_str,))

        # Verify circuit breaker trips immediately
        tripped, cb_msg = check_intraday_circuit_breaker(self.conn, max_loss_dollar=50.00)
        self.assertTrue(tripped)
        self.assertIn("Daily loss of $55.00 exceeded stop-loss threshold", cb_msg)

        # Verify resting order was cancelled by autonomous kill switch
        cur = self.conn.execute("SELECT status, reason FROM orders WHERE ticker = 'RESTING-TEST'")
        ord_row = cur.fetchone()
        self.assertEqual(ord_row["status"], "CANCELLED")
        self.assertIn("Circuit breaker tripped", ord_row["reason"])

        # Verify cash refunded to portfolio (945 + 50 = 995)
        cur = self.conn.execute("SELECT cash FROM portfolio WHERE id = 1")
        self.assertEqual(cur.fetchone()["cash"], 995.0)

        # Verify new order placement is blocked while halted
        opp = {
            "ticker": "NEW-ATTEMPT",
            "title": "New attempt",
            "side": "YES",
            "market_price": 0.50,
            "edge": 0.10,
            "model_prob": 0.60,
            "category": "weather"
        }
        ok, msg = submit_post_only_order(self.conn, opp)
        self.assertFalse(ok)
        self.assertIn("Trading halted", msg)

if __name__ == "__main__":
    unittest.main()
