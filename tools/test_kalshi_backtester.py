#!/usr/bin/env python3
"""Regression test suite for Kalshi historical backtester & parameter calibration."""
import unittest
from tools.kalshi_backtester import generate_historical_dataset, run_simulation, run_grid_search

class TestKalshiBacktester(unittest.TestCase):
    def setUp(self):
        self.events = generate_historical_dataset(n_events=100, seed=123)

    def test_dataset_generation(self):
        self.assertEqual(len(self.events), 500)
        first = self.events[0]
        self.assertIn("p_raw", first)
        self.assertIn("p_mkt", first)
        self.assertIn("outcome", first)
        self.assertIn(first["outcome"], [0, 1])
        self.assertGreaterEqual(first["p_raw"], 0.0)
        self.assertLessEqual(first["p_raw"], 1.0)

    def test_simulation_execution(self):
        res = run_simulation(self.events, kelly_fraction=0.25, shrinkage_alpha=0.95)
        self.assertGreater(res["final_cash"], 0.0)
        self.assertGreater(res["total_trades"], 0)
        self.assertGreater(res["brier_score"], 0.0)
        self.assertLess(res["brier_score"], 0.35)
        self.assertGreaterEqual(res["max_drawdown_pct"], 0.0)
        self.assertLessEqual(res["max_drawdown_pct"], 100.0)

    def test_grid_search_ranking(self):
        grid = run_grid_search(self.events[:50])
        self.assertEqual(len(grid), 15) # 3 kelly * 5 alphas
        best = grid[0]
        self.assertIn("kelly_fraction", best)
        self.assertIn("shrinkage_alpha", best)
        self.assertIn("total_pnl", best)

if __name__ == "__main__":
    unittest.main()
