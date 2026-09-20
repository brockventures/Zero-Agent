#!/usr/bin/env python3
"""Regression test for Fractional Kelly sizing and drawdown dampening."""
import unittest
from tools.kalshi_paper_bot import load_domain_params
from tools.kalshi_performance_review import recalibrate_models

class TestKalshiKelly(unittest.TestCase):
    def test_kelly_params_loaded(self):
        params = load_domain_params("weather")
        self.assertIn("kelly_fraction", params)
        self.assertGreater(params["kelly_fraction"], 0.0)
        self.assertLessEqual(params["kelly_fraction"], 0.50)

    def test_drawdown_dampener(self):
        mock_params = {
            "global": {"kelly_fraction": 0.25, "drawdown_dampener": 1.0},
            "domains": {}
        }
        # Simulate 10% drawdown ($900 on $1000)
        mock_metrics = {"total_equity": 900.0, "starting_balance": 1000.0, "brier_scores": {}}
        adjs = recalibrate_models(mock_params, mock_metrics, dry_run=True)
        self.assertTrue(any(a["parameter"] == "kelly_fraction" and a["new"] == 0.10 for a in adjs))

if __name__ == "__main__":
    unittest.main()
