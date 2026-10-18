import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
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

    def test_min_std_floor(self):
        # Even with identical model agreement, min_std must be respected
        models = [("ECMWF_IFS", 65.0), ("GFS_Global", 65.0), ("NWS_Period", 65.0)]
        mean, dynamic_std = compute_ensemble_spread(models, base_std=2.0, min_std=1.8)
        self.assertEqual(mean, 65.0)
        self.assertGreaterEqual(dynamic_std, 1.8)

    def test_tail_risk_filter_bounds(self):
        from tools.kalshi_paper_bot import MIN_CONTRACT_PRICE, MAX_CONTRACT_PRICE
        self.assertEqual(MIN_CONTRACT_PRICE, 0.15)
        self.assertEqual(MAX_CONTRACT_PRICE, 0.92)

    def test_open_meteo_integration(self):
        from tools.kalshi_paper_bot import get_open_meteo_forecast
        models = get_open_meteo_forecast(37.62, -122.38)
        # Should return list of tuples with ECMWF, GFS, ICON, GEM
        labels = [m[0] for m in models]
        self.assertIn("ECMWF_IFS", labels)
        self.assertIn("GFS_Global", labels)
        for label, temp in models:
            self.assertGreater(temp, 40.0)
            self.assertLess(temp, 115.0)

    def test_coastal_negative_skew(self):
        from tools.kalshi_paper_bot import calculate_bracket_prob
        # Under symmetric Gaussian (skew=0.0), P(>70 | mean=66, std=2)
        p_sym_hot = calculate_bracket_prob("greater", 70.0, mean=66.0, std=2.0, skew=0.0)
        # Under negative coastal skew (skew=-1.8), right tail must be compressed
        p_skew_hot = calculate_bracket_prob("greater", 70.0, mean=66.0, std=2.0, skew=-1.8)
        self.assertLess(p_skew_hot, p_sym_hot)
        
        # Conversely, cold bracket (<64) should capture more probability mass
        p_sym_cold = calculate_bracket_prob("less", 64.0, mean=66.0, std=2.0, skew=0.0)
        p_skew_cold = calculate_bracket_prob("less", 64.0, mean=66.0, std=2.0, skew=-1.8)
        self.assertGreater(p_skew_cold, p_sym_cold)


    def test_weather_safeguards_parsed_strike_types(self):
        from tools.kalshi_paper_bot import evaluate_weather_safeguards, parse_strike_bounds
        # 1. Safeguard 4: 91-92° bracket ('range') when forecast is 91.34°F -> blocked!
        st_type, bounds = parse_strike_bounds("Will the maximum temperature be 91-92° on Oct 13, 2026?", "KXHIGHTHOU-26OCT13-B91.5")
        self.assertEqual(st_type, "range")
        sg = evaluate_weather_safeguards("HOU", st_type, bounds, effective_high=91.34, is_onshore_flow=False)
        self.assertTrue(sg["is_near_bracket_center"])

        # Distant bracket (95-96° when forecast is 91.34°F) -> not blocked
        st_far, b_far = parse_strike_bounds("Will the maximum temperature be 95-96° on Oct 13, 2026?", "KXHIGHTHOU-26OCT13-B95.5")
        sg_far = evaluate_weather_safeguards("HOU", st_far, b_far, effective_high=91.34, is_onshore_flow=False)
        self.assertFalse(sg_far["is_near_bracket_center"])

        # 2. Safeguard 1a/1b: SFO '<75°' ('less') and LAX '>92°' ('greater') under onshore flow
        st_sfo, b_sfo = parse_strike_bounds("Will the maximum temperature be <75°?", "KXHIGHTSFO-T75")
        self.assertEqual(st_sfo, "less")
        self.assertTrue(evaluate_weather_safeguards("SFO", st_sfo, b_sfo, 70.0, True)["sfo_tail_blocked"])

        st_lax, b_lax = parse_strike_bounds("Will the maximum temperature be >92°?", "KXHIGHLAX-T92")
        self.assertEqual(st_lax, "greater")
        self.assertTrue(evaluate_weather_safeguards("LAX", st_lax, b_lax, 80.0, True)["lax_tail_blocked"])

if __name__ == "__main__":
    unittest.main()
