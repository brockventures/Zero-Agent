import unittest
from datetime import datetime, timezone, timedelta
from tools.kalshi_cross_bracket import (
    calculate_taker_fee,
    check_horizon,
    is_mece_or_closed_partition,
    extract_numeric_strike,
    detect_cross_bracket_arbitrage,
)


class TestCrossBracketArbitrage(unittest.TestCase):
    def test_taker_fee_calculation(self):
        # Fee formula: 0.07 * p * (1 - p)
        # At p = 0.50: 0.07 * 0.25 = 0.0175
        fee_50 = calculate_taker_fee(0.50)
        self.assertAlmostEqual(fee_50, 0.0175, places=4)
        
        # At p = 0.10: 0.07 * 0.10 * 0.90 = 0.0063
        fee_10 = calculate_taker_fee(0.10)
        self.assertAlmostEqual(fee_10, 0.0063, places=4)

    def test_horizon_gate(self):
        now = datetime.now(timezone.utc)
        
        # 5 days in future -> Pass
        t_5d = (now + timedelta(days=5)).isoformat()
        ok_5d, days_5d = check_horizon(t_5d, max_days=14)
        self.assertTrue(ok_5d)
        self.assertAlmostEqual(days_5d, 5.0, delta=0.1)
        
        # 30 days in future -> Fail
        t_30d = (now + timedelta(days=30)).isoformat()
        ok_30d, days_30d = check_horizon(t_30d, max_days=14)
        self.assertFalse(ok_30d)
        self.assertAlmostEqual(days_30d, 30.0, delta=0.1)
        
        # Past timestamp -> Fail
        t_past = (now - timedelta(days=1)).isoformat()
        ok_past, days_past = check_horizon(t_past, max_days=14)
        self.assertFalse(ok_past)

    def test_mece_partition_safety(self):
        # 1. Numeric weather ranges -> MECE
        weather_mkts = [
            {"title": "High temp at SFO: Less than 65°", "ticker": "SFO-T65"},
            {"title": "High temp at SFO: 65° to 67°", "ticker": "SFO-T67"},
            {"title": "High temp at SFO: 68° to 70°", "ticker": "SFO-T70"},
            {"title": "High temp at SFO: Greater than 70°", "ticker": "SFO-T71"},
        ]
        is_mece, p_type, _ = is_mece_or_closed_partition("High temp SFO", weather_mkts)
        self.assertTrue(is_mece)
        self.assertEqual(p_type, "numeric_range")

        # 2. Cumulative threshold strikes -> MECE
        cum_mkts = [
            {"title": "Will Grubhub App Downloads be above 130?", "ticker": "GHUB-T130"},
            {"title": "Will Grubhub App Downloads be above 135?", "ticker": "GHUB-T135"},
            {"title": "Will Grubhub App Downloads be above 140?", "ticker": "GHUB-T140"},
        ]
        is_mece, p_type, _ = is_mece_or_closed_partition("Grubhub Downloads", cum_mkts)
        self.assertTrue(is_mece)
        self.assertEqual(p_type, "cumulative_threshold")

        # 3. Closed binary matchup -> MECE
        binary_mkts = [
            {"title": "Indiana Senate: Republican", "ticker": "SEN-IN-R"},
            {"title": "Indiana Senate: Democrat", "ticker": "SEN-IN-D"},
        ]
        is_mece, p_type, _ = is_mece_or_closed_partition("Indiana Senate", binary_mkts)
        self.assertTrue(is_mece)
        self.assertEqual(p_type, "closed_binary")

        # 4. Incomplete candidate roster WITHOUT "Other/Field" -> FAILS MECE (Field Trap Guard)
        unclosed_candidates = [
            {"title": "Who will be next DNC Chair: Martin O'Malley", "ticker": "DNC-MOMA"},
            {"title": "Who will be next DNC Chair: Ben Wikler", "ticker": "DNC-BWIK"},
            {"title": "Who will be next DNC Chair: Jane Kleeb", "ticker": "DNC-JKLE"},
        ]
        is_mece, p_type, reason = is_mece_or_closed_partition("Next DNC Chair", unclosed_candidates)
        self.assertFalse(is_mece)
        self.assertEqual(p_type, "unclosed_candidate_list")
        self.assertIn("missing Field/Other", reason)

        # 5. Candidate roster WITH "Other/Field" -> PASSES MECE
        closed_candidates = unclosed_candidates + [
            {"title": "Who will be next DNC Chair: All other candidates", "ticker": "DNC-FIELD"}
        ]
        is_mece, p_type, _ = is_mece_or_closed_partition("Next DNC Chair", closed_candidates)
        self.assertTrue(is_mece)
        self.assertEqual(p_type, "named_with_field")

    def test_underround_basket_arbitrage(self):
        # Weather market with an under-round (sum Ask = $0.88)
        mkts = [
            {"ticker": "W-1", "title": "Less than 60°", "yes_ask_dollars": "0.15", "yes_bid_dollars": "0.10"},
            {"ticker": "W-2", "title": "60° to 65°", "yes_ask_dollars": "0.35", "yes_bid_dollars": "0.30"},
            {"ticker": "W-3", "title": "66° to 70°", "yes_ask_dollars": "0.25", "yes_bid_dollars": "0.20"},
            {"ticker": "W-4", "title": "Greater than 70°", "yes_ask_dollars": "0.13", "yes_bid_dollars": "0.08"},
        ]
        res = detect_cross_bracket_arbitrage(mkts, event_title="SFO High Temp")
        self.assertTrue(res["is_mece"])
        self.assertEqual(res["sum_ask"], 0.88)
        self.assertTrue(res["has_underround_arb"])
        self.assertGreater(res["arb_profit_per_share"], 0.05)

        # But if the partition was NOT MECE (missing field), under-round arb MUST NOT trigger
        fake_incomplete = [
            {"ticker": "P-1", "title": "Candidate A", "yes_ask_dollars": "0.20", "yes_bid_dollars": "0.10"},
            {"ticker": "P-2", "title": "Candidate B", "yes_ask_dollars": "0.20", "yes_bid_dollars": "0.10"},
            {"ticker": "P-3", "title": "Candidate C", "yes_ask_dollars": "0.20", "yes_bid_dollars": "0.10"},
        ]
        res_incomplete = detect_cross_bracket_arbitrage(fake_incomplete, event_title="Next Mayor")
        self.assertFalse(res_incomplete["is_mece"])
        self.assertFalse(res_incomplete["has_underround_arb"])  # Field trap avoided!

    def test_monotonicity_inversion_detection(self):
        # Strike 140 is priced higher than Strike 135 (an inversion!)
        cum_mkts = [
            {"ticker": "APP-T130", "title": "Downloads above 130", "yes_ask_dollars": "0.80", "yes_bid_dollars": "0.75"},
            {"ticker": "APP-T135", "title": "Downloads above 135", "yes_ask_dollars": "0.45", "yes_bid_dollars": "0.40"},
            {"ticker": "APP-T140", "title": "Downloads above 140", "yes_ask_dollars": "0.55", "yes_bid_dollars": "0.50"}, # Inversion: higher threshold priced higher!
        ]
        res = detect_cross_bracket_arbitrage(cum_mkts, event_title="App Downloads")
        self.assertTrue(res["is_mece"])
        self.assertEqual(res["partition_type"], "cumulative_threshold")
        self.assertTrue(res["has_monotonicity_arb"])
        self.assertEqual(len(res["monotonicity_details"]), 1)
        anomaly = res["monotonicity_details"][0]
        self.assertEqual(anomaly["lower_strike"], 135.0)
        self.assertEqual(anomaly["higher_strike"], 140.0)

    def test_overround_normalization_and_fade(self):
        # 4 brackets summing to $1.50 (heavy over-round)
        # Strike 2 is heavily inflated by retail
        mkts = [
            {"ticker": "BOX-1", "title": "Gross <$20M", "yes_ask_dollars": "0.20", "yes_bid_dollars": "0.15"},
            {"ticker": "BOX-2", "title": "Gross $20M to $30M", "yes_ask_dollars": "0.70", "yes_bid_dollars": "0.60"}, # Overinflated!
            {"ticker": "BOX-3", "title": "Gross $30M to $40M", "yes_ask_dollars": "0.35", "yes_bid_dollars": "0.30"},
            {"ticker": "BOX-4", "title": "Gross >$40M", "yes_ask_dollars": "0.25", "yes_bid_dollars": "0.20"},
        ]
        res = detect_cross_bracket_arbitrage(mkts, event_title="Weekend Box Office")
        self.assertTrue(res["is_mece"])
        self.assertEqual(res["sum_ask"], 1.50)
        self.assertTrue(res["has_overround_fade"])
        
        # Verify normalized distribution sums to ~1.0
        norm_sum = sum(d["normalized_prob"] for d in res["normalized_distribution"])
        self.assertAlmostEqual(norm_sum, 1.0, places=2)
        
        # Verify BOX-2 is selected as the top fade candidate
        best_fade = res["best_fade_bracket"]
        self.assertIsNotNone(best_fade)
        self.assertEqual(best_fade["ticker"], "BOX-2")
        self.assertAlmostEqual(best_fade["yes_ask"], 0.70)
        self.assertAlmostEqual(best_fade["normalized_prob"], round(0.70 / 1.50, 4))
        self.assertAlmostEqual(best_fade["no_ask"], 0.40) # 1.0 - 0.60 bid


if __name__ == "__main__":
    unittest.main()
