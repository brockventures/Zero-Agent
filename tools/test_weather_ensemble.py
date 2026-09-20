#!/usr/bin/env python3
"""Regression test suite for Multi-Model Weather Ensemble with Dynamic Regime-Dependent Variance."""
import json
import unittest
from pathlib import Path
from tools.kalshi_paper_bot import compute_ensemble_spread, calculate_bracket_prob

class TestWeatherEnsemble(unittest.TestCase):
    def test_dynamic_variance_tight_spread(self):
        # When all models agree tightly (e.g. 70, 70, 71)
        models = [("NWS_Period", 70.0), ("Raw_Grid", 70.0), ("Hourly_HRRR", 71.0)]
        mean, dynamic_std = compute_ensemble_spread(models, base_std=2.0)
        self.assertAlmostEqual(mean, 70.33, places=1)
        # Tight agreement should compress or keep variance low
        self.assertLess(dynamic_std, 2.0)

    def test_dynamic_variance_wide_spread(self):
        # Wide model disagreement (e.g. 68 vs 76 -> marine layer / frontal uncertainty)
        models = [("NWS_Period", 68.0), ("Raw_Grid", 76.0), ("Hourly_HRRR", 72.0)]
        mean, dynamic_std = compute_ensemble_spread(models, base_std=2.0)
        self.assertEqual(mean, 72.0)
        # High spread should expand variance above baseline
        self.assertGreater(dynamic_std, 2.5)

    def test_bracket_probability_scaling(self):
        # Compare bracket probability under low std (1.2) vs high std (3.0) for a tight range [71-72]
        p_tight = calculate_bracket_prob("range", (71, 72), mean=71.5, std=1.2)
        p_wide = calculate_bracket_prob("range", (71, 72), mean=71.5, std=3.0)
        # When mean is centered on the bracket, lower variance concentrates more probability mass
        self.assertGreater(p_tight, p_wide)

    def test_schedule_12z_drop_alignment(self):
        # Verify schedule.json aligns kalshi_paper_bot with NOAA 12Z drop (05:15 AM PT)
        sched_path = Path("/workspace/data/schedule.json")
        self.assertTrue(sched_path.exists())
        with open(sched_path) as f:
            sched = json.load(f)
        paper_bot_job = next((j for j in sched if j.get("id") == "kalshi_paper_bot"), None)
        self.assertIsNotNone(paper_bot_job)
        self.assertEqual(paper_bot_job.get("hour_pt"), 5)
        self.assertEqual(paper_bot_job.get("minute_pt"), 15)

if __name__ == "__main__":
    unittest.main()
