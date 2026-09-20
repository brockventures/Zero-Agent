#!/usr/bin/env python3
"""Regression test suite for Kalshi CLOB orderbook depth, VWAP, and slippage modeling."""
import unittest
from tools.kalshi_paper_bot import calculate_vwap_and_slippage

class TestKalshiCLOBMicrostructure(unittest.TestCase):
    def setUp(self):
        # Sample orderbook_fp structure:
        # no_dollars represents bids for NO.
        # Buying YES takes NO bids: ask = 1.0 - no_bid.
        # yes_dollars represents bids for YES.
        # Buying NO takes YES bids: ask = 1.0 - yes_bid.
        self.sample_ob = {
            "no_dollars": [
                ["0.7000", "10.00"],  # Ask YES = 0.30, 10 avail
                ["0.6900", "15.00"],  # Ask YES = 0.31, 15 avail
                ["0.6800", "20.00"],  # Ask YES = 0.32, 20 avail
                ["0.6500", "50.00"]   # Ask YES = 0.35, 50 avail
            ],
            "yes_dollars": [
                ["0.2500", "12.00"],  # Ask NO = 0.75, 12 avail
                ["0.2400", "18.00"],  # Ask NO = 0.76, 18 avail
                ["0.2000", "30.00"]   # Ask NO = 0.80, 30 avail
            ]
        }

    def test_single_level_fill_zero_slippage(self):
        # Request 5 YES contracts when top level has 10 @ 0.30
        ok, msg, fill = calculate_vwap_and_slippage(
            self.sample_ob, side="YES", target_contracts=5, model_prob=0.50, min_edge=0.08, max_slippage=0.03
        )
        self.assertTrue(ok)
        self.assertEqual(fill["filled_contracts"], 5)
        self.assertEqual(fill["vwap"], 0.30)
        self.assertEqual(fill["slippage"], 0.0)
        self.assertEqual(fill["top_price"], 0.30)
        self.assertAlmostEqual(fill["total_cost"], 1.50, places=2)

    def test_multi_level_vwap_slippage(self):
        # Request 20 YES contracts: 10 @ 0.30 + 10 @ 0.31
        # Total cost: 10*0.30 + 10*0.31 = 3.00 + 3.10 = 6.10
        # VWAP = 6.10 / 20 = 0.3050
        # Slippage = 0.3050 - 0.3000 = +0.0050
        ok, msg, fill = calculate_vwap_and_slippage(
            self.sample_ob, side="YES", target_contracts=20, model_prob=0.50, min_edge=0.08, max_slippage=0.03
        )
        self.assertTrue(ok)
        self.assertEqual(fill["filled_contracts"], 20)
        self.assertEqual(fill["vwap"], 0.3050)
        self.assertEqual(fill["slippage"], 0.0050)
        self.assertEqual(fill["total_cost"], 6.10)

    def test_slippage_tolerance_capping(self):
        # max_slippage = 0.02.
        # Level 1: 0.30 (diff 0.00) -> 10 contracts
        # Level 2: 0.31 (diff 0.01) -> 15 contracts
        # Level 3: 0.32 (diff 0.02) -> 20 contracts
        # Level 4: 0.35 (diff 0.05 > 0.02) -> EXCLUDED
        # If target = 100 contracts, should fill 10 + 15 + 20 = 45 contracts and stop.
        ok, msg, fill = calculate_vwap_and_slippage(
            self.sample_ob, side="YES", target_contracts=100, model_prob=0.60, min_edge=0.05, max_slippage=0.02
        )
        self.assertTrue(ok)
        self.assertEqual(fill["filled_contracts"], 45)
        self.assertLessEqual(fill["slippage"], 0.02)

    def test_marginal_negative_ev_rejection(self):
        # If model_prob = 0.32:
        # Level 1 (0.30): fee = 0.0147, total = 0.3147, net edge = +0.0053 > 0.001 -> takes 10
        # Level 2 (0.31): fee = 0.0150, total = 0.3250 > model_prob (0.32) -> negative marginal EV, stops!
        ok, msg, fill = calculate_vwap_and_slippage(
            self.sample_ob, side="YES", target_contracts=100, model_prob=0.32, min_edge=0.001, max_slippage=0.10
        )
        self.assertTrue(ok)
        self.assertEqual(fill["filled_contracts"], 10)

    def test_rejection_when_hurdle_not_met(self):
        # Model prob 0.33, top ask 0.30. Gross edge 0.03.
        # Min edge required = 0.08 (+8%).
        ok, msg, fill = calculate_vwap_and_slippage(
            self.sample_ob, side="YES", target_contracts=5, model_prob=0.33, min_edge=0.08, max_slippage=0.03
        )
        self.assertFalse(ok)
        self.assertIn("below hurdle", msg)

    def test_buy_no_vwap(self):
        # Buying NO takes YES bids:
        # Level 1: 0.25 -> ask 0.75, 12 avail
        # Level 2: 0.24 -> ask 0.76, 18 avail
        # Target 20 contracts: 12 @ 0.75 + 8 @ 0.76
        # Cost = 12*0.75 (9.00) + 8*0.76 (6.08) = 15.08
        # VWAP = 15.08 / 20 = 0.7540
        ok, msg, fill = calculate_vwap_and_slippage(
            self.sample_ob, side="NO", target_contracts=20, model_prob=0.90, min_edge=0.05, max_slippage=0.03
        )
        self.assertTrue(ok)
        self.assertEqual(fill["filled_contracts"], 20)
        self.assertEqual(fill["vwap"], 0.7540)
        self.assertEqual(fill["total_cost"], 15.08)

if __name__ == "__main__":
    unittest.main()
