#!/usr/bin/env python3
"""Regression test suite for October MLB Postseason Monte Carlo Tournament Model."""
import unittest
from tools.baseball_playoff_model import (
    resolve_dynamic_playoff_field,
    simulate_series_with_rotation,
    calculate_annualized_roc
)

class TestBaseballPlayoffModel(unittest.TestCase):
    def setUp(self):
        self.field = resolve_dynamic_playoff_field()

    def test_dynamic_bracket_resolution(self):
        # Must resolve 6 AL and 6 NL teams
        self.assertIn("AL", self.field)
        self.assertIn("NL", self.field)
        self.assertEqual(len(self.field["AL"]), 6)
        self.assertEqual(len(self.field["NL"]), 6)
        
        # Verify seeds 1-6 have team info and rotation
        for league in ["AL", "NL"]:
            for i, team_info in enumerate(self.field[league]):
                self.assertEqual(team_info["seed"], i + 1)
                self.assertIn("code", team_info)
                self.assertIn("true_wpct", team_info)
                self.assertIn("rotation", team_info)
                self.assertGreaterEqual(len(team_info["rotation"]), 1)

    def test_series_with_rotation_compression(self):
        # Best of 7 series between Seed 1 and Seed 2
        team_a = self.field["NL"][0]
        team_b = self.field["NL"][1]
        
        winner = simulate_series_with_rotation(team_a, team_b, length=7)
        self.assertIn(winner["code"], [team_a["code"], team_b["code"]])

    def test_wild_card_bo3(self):
        # Best of 3 Wild Card series
        team_a = self.field["AL"][2] # Seed 3
        team_b = self.field["AL"][5] # Seed 6
        
        winner = simulate_series_with_rotation(team_a, team_b, length=3)
        self.assertIn(winner["code"], [team_a["code"], team_b["code"]])

    def test_annualized_roc(self):
        # Edge = +0.08, price = 0.20, days = 40
        roc = calculate_annualized_roc(edge=0.08, market_price=0.20, days_to_expiry=40)
        # 0.08 / 0.20 = 0.40. Annualized: 0.40 * (365 / 40) = 3.65 (365%)
        self.assertAlmostEqual(roc, 3.65, places=2)

if __name__ == "__main__":
    unittest.main()
