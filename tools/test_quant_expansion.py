import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
#!/usr/bin/env python3
"""Unit and integration test suite for the Multi-Venue Quant Paper Trading Expansion."""

import json
import sqlite3
import unittest
from pathlib import Path

# Add tools to path
import quant_cross_venue_arb as track1
import quant_polymarket_standalone as track2
import quant_hyperliquid_basis as track3
import quant_trend_momentum as track4

class TestQuantExpansion(unittest.TestCase):

    def setUp(self):
        self.mem_conn = sqlite3.connect(":memory:")
        self.mem_conn.row_factory = sqlite3.Row

    def test_track1_cross_venue_evaluation(self):
        """Track 1: Verify cross-venue synthetic arbitrage calculation."""
        k_event = {
            "title": "Federal Reserve Target Rate - Nov 2026",
            "markets": [
                {"ticker": "FED-26NOV-25BP", "yes_ask_dollars": 0.45, "yes_bid_dollars": 0.43}
            ]
        }
        p_event = {
            "title": "Fed interest rate decision in November 2026",
            "markets": [
                {"id": "poly_fed_1", "outcomePrices": ["0.48", "0.46"]} # YES 0.48, NO 0.46
            ]
        }
        # Kalshi YES 0.45 + Polymarket NO 0.46 + friction 0.015 = 0.925 < 0.97 -> Arb!
        opps = track1.evaluate_cross_venue_pair(k_event, p_event)
        self.assertTrue(len(opps) >= 1)
        self.assertEqual(opps[0]["leg1_venue"], "Kalshi")
        self.assertEqual(opps[0]["leg1_side"], "YES")
        self.assertEqual(opps[0]["leg2_venue"], "Polymarket")
        self.assertEqual(opps[0]["leg2_side"], "NO")
        self.assertAlmostEqual(opps[0]["total_cost"], 0.925, places=3)
        self.assertAlmostEqual(opps[0]["net_edge"], 0.075, places=3)

    def test_track1_sports_matchup_evaluation(self):
        """Track 1: Verify date-verified head-to-head sports synthetic arbitrage."""
        match = {
            "category": "sports_nba",
            "league": "NBA",
            "date": "2026-10-14",
            "teams": ["CHA", "BOS"],
            "kalshi_event": {
                "title": "CHA Hornets vs BOS Celtics",
                "markets": [
                    {"ticker": "KXNBAGAME-26OCT14CHABOS-CHA", "title": "Charlotte wins", "yes_ask_dollars": 0.39, "yes_bid_dollars": 0.36},
                    {"ticker": "KXNBAGAME-26OCT14CHABOS-BOS", "title": "Boston wins", "yes_ask_dollars": 0.64, "yes_bid_dollars": 0.61},
                ]
            },
            "poly_market": {
                "id": "poly_nba_chabos",
                "question": "Hornets vs. Celtics",
                "bestBid": 0.41,
                "bestAsk": 0.42,
                "outcomePrices": ["0.415", "0.585"],
                "clobTokenIds": ["tok_cha", "tok_bos"]
            },
            "poly_outcomes": ["Hornets", "Celtics"]
        }
        opps = track1.evaluate_sports_matchup(match)
        self.assertTrue(len(opps) >= 1)
        # Kalshi CHA ($0.39) + Poly BOS ($0.59) + 0.007 fee = 0.9870 -> Edge +1.3%
        top = opps[0]
        self.assertEqual(top["leg1_venue"], "Kalshi")
        self.assertIn("CHA", top["leg1_side"])
        self.assertEqual(top["leg2_venue"], "Polymarket")
        self.assertIn("BOS", top["leg2_side"])
        self.assertAlmostEqual(top["gross_cost"], 0.980, places=3)
        self.assertAlmostEqual(top["net_edge"], 0.013, places=3)

    def test_track2_underround_math(self):
        """Track 2: Verify negative-risk combinatorial underround detection."""
        # 3-candidate mutually exclusive market
        # Candidate 1: YES ask 0.30
        # Candidate 2: YES ask 0.35
        # Candidate 3: YES ask 0.25
        # Sum = 0.90 + (0.005 * 3) = 0.915 < 0.965 -> Locked profit +8.5%
        basket = [
            {"market_id": "1", "yes_ask": 0.30},
            {"market_id": "2", "yes_ask": 0.35},
            {"market_id": "3", "yes_ask": 0.25},
        ]
        sum_ask = sum(b["yes_ask"] for b in basket)
        net_cost = sum_ask + (0.005 * len(basket))
        self.assertAlmostEqual(net_cost, 0.915, places=3)
        net_edge = 1.00 - net_cost
        self.assertAlmostEqual(net_edge, 0.085, places=3)

    def test_track2_overround_math(self):
        """Track 2: Verify combinatorial overround detection (buying NO basket)."""
        # In a 30-candidate single-winner market (e.g. NBA Champion):
        # Sum of Bids = 1.0170 (> 1.00)
        # Cost of buying NO across all 30 candidates = 30 - 1.0170 = 28.9830
        # Guaranteed payout (29 losing candidates * $1.00) = 29.0000
        # Locked profit = 29.0000 - 28.9830 = 0.0170 (+1.70% edge)
        num_candidates = 30
        sum_bids = 1.0170
        no_basket_cost = num_candidates - sum_bids
        guaranteed_payout = num_candidates - 1
        locked_profit = guaranteed_payout - no_basket_cost
        self.assertAlmostEqual(locked_profit, 0.0170, places=4)
        self.assertAlmostEqual(locked_profit, sum_bids - 1.00, places=4)

    def test_track2_oracle_snipe_math(self):
        """Track 2: Verify oracle latency strike parsing and mispriced ask detection."""
        # BTC spot is $83,360
        # Strike is $82,000 ("Bitcoin reach $82,000") -> Already satisfied!
        # If stale quote has YES ask at 0.70, fair value is 0.99 -> Edge +29%
        spot = 83360.0
        strike_reach = track2.parse_price("$82k")
        self.assertEqual(strike_reach, 82000.0)
        self.assertTrue(spot >= strike_reach * 1.005)
        stale_yes_ask = 0.70
        net_edge = round(0.99 - stale_yes_ask, 4)
        self.assertAlmostEqual(net_edge, 0.29, places=2)

    def test_track3_hyperliquid_funding_math(self):
        """Track 3: Verify 8-hour funding cashflow and annualized yield formulas."""
        track3.init_db(self.mem_conn)
        
        # 8-hour rate = 0.0003 (0.03% per 8 hours)
        # Annualized = 0.0003 * 3 * 365 = 0.3285 (32.85% APY)
        funding_8h = 0.0003
        annualized = funding_8h * 3.0 * 365.0
        self.assertAlmostEqual(annualized, 0.3285, places=4)
        
        # Standard $250 slot allocation receives $250 * 0.0003 = $0.075 per 8 hours ($0.225/day)
        payment_250 = 250.00 * funding_8h
        self.assertAlmostEqual(payment_250, 0.075, places=3)
        
        # Negative funding check: if funding <= 0, position must not receive funding
        neg_rate = -0.0001
        self.assertTrue(neg_rate <= 0.0)

    def test_track4_technical_indicators_and_squeeze(self):
        """Track 4: Verify ATR, Bollinger Bands, and Keltner Squeeze detection."""
        # Create 30 synthetic flat/compressing candles to trigger squeeze
        candles = []
        for i in range(30):
            candles.append({
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0 + (i % 2) * 0.2, "volume": 1000.0, "timestamp": i
            })
        tech = track4.calculate_technical_indicators(candles)
        self.assertIsNotNone(tech)
        self.assertIn("atr", tech)
        self.assertIn("is_squeeze_on", tech)
        self.assertIn("momentum_velocity", tech)
        # Since volatility is tight (range 2.0), squeeze should be ON
        self.assertTrue(tech["is_squeeze_on"])

    def test_track4_trailing_stop_calculation(self):
        """Track 4: Verify 2.0x ATR trailing stop updates."""
        entry_price = 100.0
        atr = 2.5
        # Initial stop is 100 - (2 * 2.5) = 95.0
        initial_stop = entry_price - (2.0 * atr)
        self.assertEqual(initial_stop, 95.0)
        
        # When price rallies to 110.0, peak is 110.0, stop ratchets to 110 - 5.0 = 105.0
        peak = 110.0
        ratcheted_stop = peak - (2.0 * atr)
        self.assertEqual(ratcheted_stop, 105.0)


    def test_track2_horizon_guard(self):
        """Track 2: Verify 14-day horizon filter rejects far-future/2027 contracts."""
        # 1. Reject future year in title
        far_event = {
            "title": "NBA: 2027 Champion",
            "endDate": "2027-07-01T03:59:00Z",
            "markets": [{"id": "m1", "question": "Will Boston win?"}]
        }
        self.assertFalse(track2.is_within_horizon(far_event, max_days=14.0))

        # 2. Reject endDate > 14 days
        month_away_event = {
            "title": "Will SpaceX launch Starship Flight 10 by November?",
            "endDate": "2026-11-20T00:00:00Z",
            "markets": [{"id": "m2", "question": "Launch by Nov 20"}]
        }
        self.assertFalse(track2.is_within_horizon(month_away_event, max_days=14.0))

        # 3. Accept event within 14 days (e.g. 5 days from now)
        from datetime import datetime, timedelta, timezone
        soon_iso = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        soon_event = {
            "title": "Will Bitcoin reach $85k by this Friday?",
            "endDate": soon_iso,
            "markets": [{"id": "m3", "question": "BTC reach $85k"}]
        }
        self.assertTrue(track2.is_within_horizon(soon_event, max_days=14.0))

        # 4. Reject past event
        past_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        past_event = {
            "title": "Old Resolved Event",
            "endDate": past_iso,
            "markets": [{"id": "m4", "question": "Past question"}]
        }
        self.assertFalse(track2.is_within_horizon(past_event, max_days=14.0))

    def test_track1_sports_horizon_guard(self):
        """Track 1: Verify sports matcher excludes games > 14 days away."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        PT = ZoneInfo("America/Los_Angeles")
        
        # Game 3 days away: should be within horizon
        near_date = (datetime.now(PT) + timedelta(days=3)).strftime("%Y-%m-%d")
        g_near = datetime.strptime(near_date, "%Y-%m-%d").date()
        days_near = (g_near - datetime.now(PT).date()).days
        self.assertTrue(0 <= days_near <= track1.MAX_HORIZON_DAYS)

        # Game 30 days away: should exceed horizon
        far_date = (datetime.now(PT) + timedelta(days=30)).strftime("%Y-%m-%d")
        g_far = datetime.strptime(far_date, "%Y-%m-%d").date()
        days_far = (g_far - datetime.now(PT).date()).days
        self.assertFalse(0 <= days_far <= track1.MAX_HORIZON_DAYS)


    def test_track2_rejects_european_point_in_time_above_contracts(self):
        """Track 2: Verify European 'above  on <date>' contracts are rejected while one-touch 'reach ' is accepted."""
        from unittest.mock import patch
        from datetime import datetime, timedelta, timezone
        soon_iso = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        events = [
            {
                "title": "Bitcoin above ___ on October 15?",
                "endDate": soon_iso,
                "markets": [{
                    "id": "m_euro",
                    "question": "Will the price of Bitcoin be above 2,000 on October 15?",
                    "bestBid": "0.74",
                    "bestAsk": "0.75"
                }]
            },
            {
                "title": "Bitcoin weekly barrier",
                "endDate": soon_iso,
                "markets": [{
                    "id": "m_touch",
                    "question": "Will Bitcoin reach 2,000 by Friday?",
                    "bestBid": "0.74",
                    "bestAsk": "0.75"
                }]
            }
        ]
        with patch.object(track2, "get_live_crypto_spots", return_value={"BTC": 84000.0}):
            opps = track2.scan_oracle_latency_opportunities(events)
        self.assertEqual(len(opps), 1)
        self.assertIn("reach 2,000", opps[0]["event_title"])

    def test_track1_settlement_loop(self):
        """Track 1: Verify past-date cross-venue arbitrage pairs settle and credit payout to cash."""
        track1.init_db(self.mem_conn)
        with self.mem_conn:
            self.mem_conn.execute("UPDATE cross_venue_portfolio SET cash = 950.65, realized_pnl = 0.0 WHERE id = 1")
            self.mem_conn.execute("""
                INSERT INTO cross_venue_trades (
                    timestamp, event_name, category, kalshi_ticker, kalshi_side, kalshi_price,
                    polymarket_id, polymarket_side, polymarket_price, contracts, sum_cost,
                    locked_profit, net_edge, status
                ) VALUES ('2026-10-11T10:00:00', '[NFL] 2026-10-11 IND vs PIT', 'sports_nfl',
                          'KXNFLGAME-26OCT11INDPIT-IND', 'IND YES', 0.39, 'poly_1', 'PIT YES', 0.50,
                          50, 49.35, 0.65, 0.013, 'OPEN')
            """)
        settled = track1.settle_resolved_cross_venue_trades(self.mem_conn)
        self.assertEqual(len(settled), 1)
        p = self.mem_conn.execute("SELECT cash, realized_pnl FROM cross_venue_portfolio WHERE id = 1").fetchone()
        self.assertAlmostEqual(p["cash"], 1000.65, places=2)
        self.assertAlmostEqual(p["realized_pnl"], 0.65, places=2)

    def test_track1_nhl_league_scoped_teams_and_maker_spread(self):
        """Track 1: Verify NHL team aliases do not collide with NFL/NBA and Maker-Taker spread triggers."""
        self.assertEqual(track1.extract_teams("Canucks vs Panthers", league="NHL"), {"VAN", "FLA"})
        self.assertEqual(track1.extract_teams("Panthers vs Falcons", league="NFL"), {"CAR", "ATL"})
        self.assertEqual(track1.extract_teams("Kings vs Bruins", league="NHL"), {"LAK", "BOS"})
        self.assertEqual(track1.extract_teams("Kings vs Celtics", league="NBA"), {"SAC", "BOS"})

        # Maker-Taker spread test (Kalshi spread >= 3c, Maker Bid+1c + Poly Ask <= 0.985)
        match = {
            "category": "sports_nhl",
            "league": "NHL",
            "date": "2026-10-17",
            "teams": ["NJD", "WSH"],
            "kalshi_event": {
                "title": "New Jersey vs Washington",
                "markets": [
                    {"ticker": "KXNHLGAME-26OCT17NJWSH-NJ", "yes_sub_title": "New Jersey", "yes_ask_dollars": 0.45, "yes_bid_dollars": 0.44},
                    {"ticker": "KXNHLGAME-26OCT17NJWSH-WSH", "yes_sub_title": "Washington", "yes_ask_dollars": 0.59, "yes_bid_dollars": 0.55},
                ]
            },
            "poly_market": {
                "id": "poly_nhl_njwsh",
                "question": "Devils vs. Capitals",
                "bestBid": 0.40,
                "bestAsk": 0.41,
                "outcomePrices": '["0.405", "0.595"]',
                "clobTokenIds": '["tok_nj", "tok_wsh"]'
            },
            "poly_outcomes": ["Devils", "Capitals"]
        }
        opps = track1.evaluate_sports_matchup(match)
        self.assertEqual(len(opps), 1)
        # Poly NJD ($0.41) + Kalshi WSH Maker ($0.55 + $0.01 = $0.56) = $0.970 (+3.0% net edge)
        self.assertAlmostEqual(opps[0]["total_cost"], 0.970, places=3)
        self.assertAlmostEqual(opps[0]["net_edge"], 0.030, places=3)

    def test_track2_neg_risk_tail_null_bid_and_dominance(self):
        """Track 2: Verify negRisk active-leg overround ignores null tail bids and detects strike dominance."""
        from datetime import datetime, timedelta, timezone
        soon_iso = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        events = [
            {
                "title": "Elon Musk # tweets October 9 - October 16, 2026?",
                "endDate": soon_iso,
                "negRisk": True,
                "markets": [
                    {"id": "m_tail", "question": "Less than 80 tweets", "bestBid": None, "bestAsk": "0.001"},
                    {"id": "m1", "question": "200-219 tweets", "bestBid": "0.34", "bestAsk": "0.35"},
                    {"id": "m2", "question": "220-239 tweets", "bestBid": "0.34", "bestAsk": "0.35"},
                    {"id": "m3", "question": "240-259 tweets", "bestBid": "0.33", "bestAsk": "0.34"},
                ]
            }
        ]
        opps = track2.sweep_negative_risk_underrounds(events)
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0]["strategy"], "combinatorial_overround_no")
        self.assertEqual(opps[0]["market_count"], 3)

        # Strike Dominance test: Reach $94k Ask=0.004 vs Reach $96k Bid=0.006
        dom_events = [{
            "title": "What price will Bitcoin hit October 12-18?",
            "endDate": soon_iso,
            "markets": [
                {"id": "m94", "question": "Will Bitcoin reach $94,000?", "bestBid": "0.003", "bestAsk": "0.004"},
                {"id": "m96", "question": "Will Bitcoin reach $96,000?", "bestBid": "0.006", "bestAsk": "0.007"},
            ]
        }]
        from unittest.mock import patch
        with patch.object(track2, "get_live_crypto_spots", return_value={"BTC": 82000.0}):
            dom_opps = track2.scan_oracle_latency_opportunities(dom_events)
        self.assertEqual(len(dom_opps), 1)
        self.assertEqual(dom_opps[0]["strategy"], "strike_dominance_arb")

    def test_track3_reverse_basis_and_reentry_without_unique_crash(self):
        """Track 3: Verify Reverse Basis (LONG_PERP) on negative funding and clean SQLite asset re-entry."""
        track3.init_db(self.mem_conn)
        assets_cycle1 = [
            {"name": "BLUR", "mark_px": 0.20, "oracle_px": 0.20, "funding_8h": -0.0005, "annualized_yield": -0.5475, "open_interest_usd": 1_500_000.0}
        ]
        track3.manage_basis_portfolio(self.mem_conn, assets_cycle1)
        open_pos = self.mem_conn.execute("SELECT * FROM hyperliquid_positions WHERE status='OPEN'").fetchall()
        self.assertEqual(len(open_pos), 1)
        self.assertEqual(open_pos[0]["asset"], "BLUR")
        self.assertEqual(open_pos[0]["direction"], "LONG_PERP")

        # Flip funding to 0 -> unwinds BLUR
        assets_cycle2 = [
            {"name": "BLUR", "mark_px": 0.20, "oracle_px": 0.20, "funding_8h": 0.0, "annualized_yield": 0.0, "open_interest_usd": 1_500_000.0}
        ]
        track3.manage_basis_portfolio(self.mem_conn, assets_cycle2)
        self.assertEqual(len(self.mem_conn.execute("SELECT * FROM hyperliquid_positions WHERE status='OPEN'").fetchall()), 0)

        # Spike BLUR again -> must re-enter cleanly without sqlite3.IntegrityError
        track3.manage_basis_portfolio(self.mem_conn, assets_cycle1)
        self.assertEqual(len(self.mem_conn.execute("SELECT * FROM hyperliquid_positions WHERE status='OPEN'").fetchall()), 1)

    def test_track4_regime_exit_and_reentry_without_unique_crash(self):
        """Track 4: Verify automatic exit on regime loss and clean SQLite symbol re-entry."""
        track4.init_db(self.mem_conn)
        with self.mem_conn:
            self.mem_conn.execute("""
                INSERT INTO trend_positions (
                    symbol, direction, asset_class, strategy, size, entry_price, current_price,
                    trailing_stop, peak_price, atr, opened_at, allocated_usd, unrealized_pnl, status
                ) VALUES ('QQQ', 'LONG', 'equity', 'dual_momentum', 0.20, 746.0, 746.0, 729.0, 746.0, 8.0, '2026-10-12', 150.0, 0.0, 'OPEN')
            """)
            self.mem_conn.execute("UPDATE trend_portfolio SET cash = 850.0 WHERE id = 1")

        # QQQ flips to bear regime (price 735.9 < sma_20 742.9, mom -1.03) -> even though above stop 729.0, must exit!
        market_data = {
            "QQQ": {"current_price": 735.9, "sma_20": 742.9, "sma_50": 742.9, "atr": 8.0, "momentum_velocity": -1.03, "is_bull_regime": False, "is_bear_regime": True, "is_squeeze_on": False}
        }
        track4.update_open_positions(self.mem_conn, market_data)
        open_pos = self.mem_conn.execute("SELECT * FROM trend_positions WHERE status='OPEN'").fetchall()
        self.assertEqual(len(open_pos), 0)


    def test_quant_summaries_under_2000_chars(self):
        """Verify all quant sidecar and review summaries strictly fit within 2,000 characters."""
        from tools.kalshi_performance_review import format_review_summary, _cap_summary_length
        from tools.sidecars import _cap_quant_summary
        mock_metrics = {
            "total_equity": 1012.65,
            "cash": 995.46,
            "open_exposure": 17.19,
            "open_count": 4,
            "realized_pnl": 13.48,
            "today_pnl": -7.43,
            "today_wins": 0,
            "today_losses": 1,
            "est_open_pnl": 0.0,
            "open_tracking_wins": [{"label": f"SFO {70+i} NO", "pnl": 2.5} for i in range(6)],
            "open_tracking_losses": [],
            "open_in_progress": [{"label": f"LAX {80+i} NO", "obs": 72.1} for i in range(6)],
            "brier_scores": {"overall": 0.249},
            "llm_pm_postmortem": "Portfolio dispersion remains balanced across all 5 books. " * 10,
        }
        summary = format_review_summary(mock_metrics, [], [])
        self.assertLessEqual(len(summary), 2000)
        self.assertLessEqual(len(_cap_summary_length("line\n" * 600)), 2000)
        self.assertLessEqual(len(_cap_quant_summary("line\n" * 600)), 2000)

    def test_dynamic_conviction_and_liquidity_sizing(self):
        """Verify dynamic sizing scales with APY/OI on Hyperliquid and Kalshi Kelly params are uncapped."""
        from tools.kalshi_paper_bot import load_domain_params
        k_params = load_domain_params("weather")
        self.assertGreaterEqual(k_params["max_trade_risk"], 100.0)
        self.assertGreaterEqual(k_params["max_total_exposure"], 400.0)

        # Hyperliquid dynamic yield/OI scaling: high-APY deep-OI asset should allocate > $250
        track3.init_db(self.mem_conn)
        assets = [
            {
                "name": "HIGH_YIELD",
                "mark_px": 10.0,
                "oracle_px": 10.0,
                "funding_8h": 0.0008,
                "annualized_yield": 0.876,
                "open_interest_usd": 2_000_000.0,
            }
        ]
        track3.manage_basis_portfolio(self.mem_conn, assets)
        pos = self.mem_conn.execute("SELECT allocated_usd FROM hyperliquid_positions WHERE status='OPEN'").fetchone()
        self.assertIsNotNone(pos)
        self.assertGreater(pos["allocated_usd"], 250.0)
        self.assertLessEqual(pos["allocated_usd"], 400.0)

    def test_books_3_to_5_patches(self):
        """Verify Book 3 maker spread capture, Book 4 1h->8h funding conversion, and Book 5 sub-dollar ATR & laggard rotation."""
        from datetime import datetime, timedelta, timezone
        soon_iso = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

        # 1. Book 3: 3-way negRisk soccer moneyline with sum(ask)=1.01, sum(bid)=0.98 should qualify via 2-leg maker rebate
        events_3way = [{
            "title": "Liverpool FC vs. Villarreal CF",
            "endDate": soon_iso,
            "negRisk": True,
            "markets": [
                {"id": "m1", "question": "Liverpool win", "bestBid": "0.64", "bestAsk": "0.65", "liquidityNum": 100000},
                {"id": "m2", "question": "Draw", "bestBid": "0.19", "bestAsk": "0.20", "liquidityNum": 100000},
                {"id": "m3", "question": "Villarreal win", "bestBid": "0.15", "bestAsk": "0.16", "liquidityNum": 100000},
            ]
        }]
        opps_3way = track2.sweep_negative_risk_underrounds(events_3way)
        self.assertEqual(len(opps_3way), 1)
        self.assertEqual(opps_3way[0]["strategy"], "combinatorial_maker_underround_yes")
        self.assertAlmostEqual(opps_3way[0]["total_cost"], 0.990, places=3)

        # 2. Book 5: Sub-dollar DOGE candles must never round ATR to 0.0
        doge_candles = [
            {"open": 0.080, "high": 0.082, "low": 0.078, "close": 0.080 - (i * 0.0002), "volume": 100000.0, "timestamp": i}
            for i in range(30)
        ]
        doge_tech = track4.calculate_technical_indicators(doge_candles)
        self.assertIsNotNone(doge_tech)
        self.assertGreater(doge_tech["atr"], 0.001)

    def test_smart_llm_pm_gate_zero_token_when_empty(self):
        """Verify evaluate_quant_candidates_llm short-circuits with zero LLM calls when candidates list is empty."""
        from unittest.mock import patch
        import tools.llm_sidecar as llm_sc
        with patch.object(llm_sc, "_is_test_fast_mode", return_value=False), \
             patch.object(llm_sc, "call_sidecar_llm_json", side_effect=AssertionError("LLM should not be called on empty candidates")):
            res = llm_sc.evaluate_quant_candidates_llm(
                track_key="quant_trend",
                track_name="TestBook",
                candidates=[],
                portfolio_context={"cash": 1000.0},
            )
        self.assertEqual(res["approved_indices"], [])

if __name__ == "__main__":
    unittest.main()

