#!/usr/bin/env python3
"""Regression test suite for Kalshi paper bot Net P&L (post-fees) accounting."""

import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import patch

from tools.kalshi_paper_bot import (
    get_portfolio_status,
    execute_trade
)

PT = ZoneInfo("America/Los_Angeles")

class TestKalshiNetPnL(unittest.TestCase):
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
            self.conn.execute("INSERT INTO portfolio VALUES (1, 1000.0, 1000.0, 0.0, '2026-10-05T07:00:00')")
            self.conn.execute("""
                CREATE TABLE positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT UNIQUE,
                    title TEXT,
                    side TEXT,
                    contracts INTEGER,
                    entry_price REAL,
                    total_cost REAL,
                    fee REAL DEFAULT 0.0,
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

    def tearDown(self):
        self.conn.close()

    @patch("tools.kalshi_paper_bot.get_clob_orderbook")
    def test_execute_trade_stores_fee_in_positions(self, mock_ob):
        mock_ob.return_value = None # force fallback math
        opp = {
            "ticker": "KXHIGHLAX-26OCT05-T90",
            "title": "Will Max Temp in LAX be >90°?",
            "side": "NO",
            "market_price": 0.70,
            "edge": 0.15,
            "model_prob": 0.85,
            "category": "weather"
        }
        ok, msg = execute_trade(self.conn, opp)
        self.assertTrue(ok, msg)
        
        pos = self.conn.execute("SELECT * FROM positions WHERE ticker = 'KXHIGHLAX-26OCT05-T90'").fetchone()
        self.assertIsNotNone(pos)
        self.assertGreater(pos["fee"], 0.0)
        self.assertGreater(pos["total_cost"], 0.0)
        
        # Cash should be reduced by cost + fee
        p = self.conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
        expected_cash = round(1000.0 - pos["total_cost"] - pos["fee"], 2)
        self.assertAlmostEqual(p["cash"], expected_cash, places=2)

    def test_settlement_calculates_net_pnl_post_fees(self):
        now_str = datetime.now(PT).isoformat()
        # 1. Winning trade: cost 5.00, fee 0.25, payout 10.00 -> Net P&L = +4.75 (vs +5.00 gross)
        with self.conn:
            self.conn.execute("""
                INSERT INTO positions (ticker, title, side, contracts, entry_price, total_cost, fee, opened_at, status, model_prob, category)
                VALUES ('WIN-TRADE', 'Win', 'YES', 10, 0.50, 5.00, 0.25, ?, 'OPEN', 0.65, 'weather')
            """, (now_str,))
            self.conn.execute("UPDATE portfolio SET cash = cash - 5.25 WHERE id = 1")
            
        pos = self.conn.execute("SELECT * FROM positions WHERE ticker = 'WIN-TRADE'").fetchone()
        settled_price = 1.00
        payout = pos["contracts"] * settled_price
        pos_fee = pos["fee"] or 0.0
        realized_pnl = round(payout - pos["total_cost"] - pos_fee, 2)
        
        self.assertEqual(realized_pnl, 4.75)
        
        with self.conn:
            self.conn.execute("""
                UPDATE positions SET status = 'RESOLVED', settled_at = ?, settled_price = ?, realized_pnl = ?
                WHERE id = ?
            """, (now_str, settled_price, realized_pnl, pos["id"]))
            self.conn.execute("""
                UPDATE portfolio SET cash = cash + ?, realized_pnl = realized_pnl + ? WHERE id = 1
            """, (payout, realized_pnl))
            
        p = self.conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
        self.assertAlmostEqual(p["realized_pnl"], 4.75, places=2)
        # 1000 - 5.25 + 10.00 = 1004.75
        self.assertAlmostEqual(p["cash"], 1004.75, places=2)

        # 2. Losing trade: cost 5.00, fee 0.25, payout 0.00 -> Net P&L = -5.25 (vs -5.00 gross)
        with self.conn:
            self.conn.execute("""
                INSERT INTO positions (ticker, title, side, contracts, entry_price, total_cost, fee, opened_at, status, model_prob, category)
                VALUES ('LOSS-TRADE', 'Loss', 'YES', 10, 0.50, 5.00, 0.25, ?, 'OPEN', 0.65, 'weather')
            """, (now_str,))
            self.conn.execute("UPDATE portfolio SET cash = cash - 5.25 WHERE id = 1")
            
        pos2 = self.conn.execute("SELECT * FROM positions WHERE ticker = 'LOSS-TRADE'").fetchone()
        settled_price2 = 0.00
        payout2 = pos2["contracts"] * settled_price2
        pos_fee2 = pos2["fee"] or 0.0
        realized_pnl2 = round(payout2 - pos2["total_cost"] - pos_fee2, 2)
        
        self.assertEqual(realized_pnl2, -5.25)
        
        with self.conn:
            self.conn.execute("""
                UPDATE positions SET status = 'RESOLVED', settled_at = ?, settled_price = ?, realized_pnl = ?
                WHERE id = ?
            """, (now_str, settled_price2, realized_pnl2, pos2["id"]))
            self.conn.execute("""
                UPDATE portfolio SET cash = cash + ?, realized_pnl = realized_pnl + ? WHERE id = 1
            """, (payout2, realized_pnl2))
            
        p2 = self.conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
        # Net realized P&L: 4.75 - 5.25 = -0.50
        self.assertAlmostEqual(p2["realized_pnl"], -0.50, places=2)
        # Cash: 1004.75 - 5.25 + 0 = 999.50 (starting 1000 - 0.50 net loss = 999.50)
        self.assertAlmostEqual(p2["cash"], 999.50, places=2)

    def test_live_database_net_pnl_reconciliation(self):
        live_db_path = Path("/workspace/data/kalshi_paper.db")
        if not live_db_path.exists():
            self.skipTest("Live database not present")
            
        live_conn = sqlite3.connect(str(live_db_path))
        live_conn.row_factory = sqlite3.Row
        
        st = get_portfolio_status(live_conn)
        self.assertGreaterEqual(st["resolved_count"], 0)
        self.assertIsInstance(st["realized_pnl"], float)
        self.assertGreaterEqual(st["total_fees"], 0.0)
        self.assertGreater(st["total_equity"], 0.0)
        self.assertAlmostEqual(st["total_equity"], st["cash"] + st["open_exposure"], places=2)
        
        live_conn.close()

if __name__ == "__main__":
    unittest.main()
