#!/usr/bin/env python3
"""Regression test suite for MLB Sabermetric Game Model with Run-Expectancy & Platoon Splits."""
import unittest
from tools.baseball_daily_quant import calculate_game_run_expectancy

class TestBaseballSabermetricModel(unittest.TestCase):
    def setUp(self):
        self.stuff_ratings = {
            "tarik skubal": 125.0,
            "patrick corbin": 85.0,
            "average joe": 100.0
        }
        self.bp_eras = {
            "LAD": 3.40,
            "SF": 4.10,
            "COL": 5.20,
            "SD": 3.60
        }
        self.park_factors = {
            "LAD": 0.98,
            "COL": 1.15,
            "SF": 0.95,
            "SD": 0.94
        }
        self.platoon_splits = {
            "LAD": 0.040,  # +4% wRC+ vs LHP
            "SF": -0.050,  # -5% wRC+ vs LHP
            "COL": 0.010,
            "SD": 0.000
        }
        self.baselines = {
            "LAD": 0.610,
            "SF": 0.490,
            "COL": 0.380,
            "SD": 0.550
        }

    def test_ace_suppression(self):
        # Skubal (125 Stuff+) vs average pitcher (100 Stuff+)
        p_away, p_home, runs_away, runs_home = calculate_game_run_expectancy(
            away_team="SF", home_team="LAD",
            sp_away="Average Joe", sp_home="Tarik Skubal",
            throws_away="R", throws_home="L",
            stuff_ratings=self.stuff_ratings, bp_eras=self.bp_eras,
            park_factors=self.park_factors, platoon_splits=self.platoon_splits,
            baselines=self.baselines
        )
        self.assertGreater(p_home, 0.65)
        self.assertLess(runs_away, runs_home)
        self.assertAlmostEqual(p_away + p_home, 1.0, places=4)

    def test_platoon_differential(self):
        # SF has negative platoon split vs LHP (-0.050)
        # When facing LHP starter vs RHP starter, SF expected runs should decrease
        _, _, runs_vs_lhp, _ = calculate_game_run_expectancy(
            away_team="SF", home_team="SD",
            sp_away="Average Joe", sp_home="Average Joe",
            throws_away="R", throws_home="L",
            stuff_ratings=self.stuff_ratings, bp_eras=self.bp_eras,
            park_factors=self.park_factors, platoon_splits=self.platoon_splits,
            baselines=self.baselines
        )
        _, _, runs_vs_rhp, _ = calculate_game_run_expectancy(
            away_team="SF", home_team="SD",
            sp_away="Average Joe", sp_home="Average Joe",
            throws_away="R", throws_home="R",
            stuff_ratings=self.stuff_ratings, bp_eras=self.bp_eras,
            park_factors=self.park_factors, platoon_splits=self.platoon_splits,
            baselines=self.baselines
        )
        self.assertLess(runs_vs_lhp, runs_vs_rhp)

    def test_venue_park_factor_scaling(self):
        # Coors Field (1.15) should produce higher total runs than Petco Park (0.94)
        _, _, r_a_col, r_h_col = calculate_game_run_expectancy(
            away_team="SF", home_team="COL",
            sp_away="Average Joe", sp_home="Average Joe",
            throws_away="R", throws_home="R",
            stuff_ratings=self.stuff_ratings, bp_eras=self.bp_eras,
            park_factors=self.park_factors, platoon_splits=self.platoon_splits,
            baselines=self.baselines
        )
        _, _, r_a_sd, r_h_sd = calculate_game_run_expectancy(
            away_team="SF", home_team="SD",
            sp_away="Average Joe", sp_home="Average Joe",
            throws_away="R", throws_home="R",
            stuff_ratings=self.stuff_ratings, bp_eras=self.bp_eras,
            park_factors=self.park_factors, platoon_splits=self.platoon_splits,
            baselines=self.baselines
        )
        total_col = r_a_col + r_h_col
        total_sd = r_a_sd + r_h_sd
        self.assertGreater(total_col, total_sd)

    def test_home_field_advantage(self):
        # Two identical teams with identical pitchers at neutral park (1.00)
        neutral_baselines = {"A": 0.50, "B": 0.50}
        neutral_bp = {"A": 4.10, "B": 4.10}
        neutral_pf = {"B": 1.00}
        p_away, p_home, _, _ = calculate_game_run_expectancy(
            away_team="A", home_team="B",
            sp_away="Average Joe", sp_home="Average Joe",
            throws_away="R", throws_home="R",
            stuff_ratings=self.stuff_ratings, bp_eras=neutral_bp,
            park_factors=neutral_pf, platoon_splits={},
            baselines=neutral_baselines
        )
        self.assertGreater(p_home, 0.51)
        self.assertLess(p_home, 0.55)

if __name__ == "__main__":
    unittest.main()
