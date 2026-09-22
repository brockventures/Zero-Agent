#!/usr/bin/env python3
"""MLB Daily Matchup Quant & Kalshi Arbitrage Engine.

Powered by Ivy's PostgreSQL database on Host 2 (Host 2:5433).
Calculates Log5 single-game win probabilities for every MLB matchup using
starting pitcher probables, Stuff+ ratings, team offensive wRC+, and home field
advantage. Identifies positive-EV mispricings on Kalshi KXMLBGAME markets and
executes paper trades into /workspace/data/kalshi_paper.db.
"""

import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import psycopg2

sys.path.insert(0, "/workspace")
from tools.kalshi_paper_bot import get_db, execute_trade, print_status, load_domain_params

PT = ZoneInfo("America/Los_Angeles")
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"


def get_db_connection():
    host = os.environ.get("IVY_DB_HOST")
    if not host:
        try:
            from tools.sidecars import HOST_2_IP
            host = HOST_2_IP
        except Exception:
            host = "127.0.0.1"
    try:
        return psycopg2.connect(
            host=host,
            port=5433,
            dbname="baseball_data",
            user="myuser",
            password="mypassword",
            connect_timeout=4
        )
    except Exception:
        if host != "127.0.0.1":
            return psycopg2.connect(
                host="127.0.0.1",
                port=5433,
                dbname="baseball_data",
                user="myuser",
                password="mypassword",
                connect_timeout=4
            )
        raise

# Team code aliases in Kalshi tickers
KALSHI_ALIASES = {
    "OAK": "ATH",
    "ATH": "ATH",
    "CWS": "CWS",
    "CHW": "CWS",
    "CHC": "CHC",
    "LAD": "LAD",
    "LAA": "LAA",
    "NYY": "NYY",
    "NYM": "NYM",
    "SF": "SF",
    "SD": "SD",
    "TB": "TB",
    "KC": "KC"
}

def load_live_team_baselines():
    """Ingest live 2026 runs scored/allowed from Host 2 PostgreSQL actual_stats and compute Pythagenpat."""
    baselines = {}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            WITH batting AS (
                SELECT team, SUM(runs) as rs, AVG(wrc_plus) as wrc
                FROM actual_stats
                WHERE year = 2026 AND stats_type = 'bat' AND team IS NOT NULL AND team != '- - -' AND team != ''
                GROUP BY team
            ),
            pitching AS (
                SELECT team, SUM(er) as er
                FROM actual_stats
                WHERE year = 2026 AND stats_type = 'pit' AND team IS NOT NULL AND team != '- - -' AND team != ''
                GROUP BY team
            )
            SELECT b.team, b.rs, p.er, b.wrc
            FROM batting b
            JOIN pitching p ON b.team = p.team;
        """)
        for r in cur.fetchall():
            team, rs, er, wrc = r
            if rs and er and float(rs) > 0 and float(er) > 0:
                rs_val = float(rs)
                ra_est = float(er) * 1.09 # Convert earned runs to estimated total runs allowed
                pyth = (rs_val ** 1.83) / ((rs_val ** 1.83) + (ra_est ** 1.83))
                # Map aliases
                mapped_team = KALSHI_ALIASES.get(team, team)
                baselines[mapped_team] = round(pyth, 3)
        cur.close()
        conn.close()
        # Cache successful database baselines to disk for resilience
        if baselines:
            try:
                Path("/workspace/data/cached_team_baselines.json").write_text(json.dumps(baselines, indent=2))
            except Exception:
                pass
    except Exception as e:
        print(f"Warning: Failed to load live team baselines from PostgreSQL: {e}")
        cache_file = Path("/workspace/data/cached_team_baselines.json")
        if cache_file.exists():
            try:
                baselines = json.loads(cache_file.read_text())
                print(f"ℹ️ Loaded {len(baselines)} team baselines from disk cache fallback.")
            except Exception:
                pass
        
    # Standard MLB league average fallback if database empty
    if not baselines:
        baselines = {"LAD": 0.605, "NYY": 0.595, "MIL": 0.575, "SD": 0.560, "CHC": 0.540, "CWS": 0.566}
    return baselines

TEAM_BASELINES = load_live_team_baselines()

def load_park_factors():
    """Load venue run factors from mlb_park_factors on Host 2 PostgreSQL."""
    pf = {}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT team, runs FROM mlb_park_factors;")
        for r in cur.fetchall():
            team, runs_factor = r
            if team and runs_factor:
                pf[KALSHI_ALIASES.get(team, team)] = float(runs_factor) / 100.0
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to load park factors: {e}")
    return pf

def load_bullpen_eras():
    """Load 2026 bullpen run prevention (RP ERA) from actual_stats on Host 2 PostgreSQL."""
    bp = {}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT team, SUM(er) as er, SUM(innings_pitched) as ip
            FROM actual_stats
            WHERE year = 2026 AND stats_type = 'pit' AND pitcher_type = 'RP' AND team IS NOT NULL
            GROUP BY team;
        """)
        for r in cur.fetchall():
            team, er, ip = r
            if team and ip and float(ip) > 10.0:
                era = (float(er) * 9.0) / float(ip)
                bp[KALSHI_ALIASES.get(team, team)] = round(era, 2)
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to load bullpen ERAs: {e}")
    return bp

def load_team_platoon_splits():
    """Load team offensive platoon splits (OPS differential vs LHP) from PostgreSQL."""
    splits = {}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT a.team, AVG(p.ops_diff) as avg_ops_diff
            FROM player_platoon_splits p
            JOIN actual_stats a ON p.player_id = a.player_id
            WHERE p.stats_type = 'bat' AND a.year = 2026 AND a.team IS NOT NULL
            GROUP BY a.team;
        """)
        for r in cur.fetchall():
            team, ops_diff = r
            if team and ops_diff is not None:
                splits[KALSHI_ALIASES.get(team, team)] = float(ops_diff)
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to load platoon splits: {e}")
    return splits

def load_probables_and_stuff():
    """Load today's starting pitcher probables and Stuff+ from Host 2 PostgreSQL."""
    now_pt = datetime.now(PT)
    date_str = f"{now_pt.month}/{now_pt.day}" # e.g. "9/19"
    
    probables = {}
    stuff_ratings = {}
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Load probables
        cur.execute("""
            SELECT team, opp, side, sp_name, sp_throws 
            FROM mlb_probables_grid 
            WHERE game_date = %s;
        """, (date_str,))
        for r in cur.fetchall():
            team, opp, side, sp, throws = r
            probables[team] = {"opp": opp, "side": side, "sp": sp, "throws": throws}
            
        # Load Stuff+ averages
        cur.execute("""
            SELECT player_name, stuff_plus 
            FROM pitcher_stuff_plus 
            WHERE stuff_plus IS NOT NULL;
        """)
        for r in cur.fetchall():
            name, sp_val = r
            if name:
                stuff_ratings[name.lower()] = float(sp_val)
                
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Database query failed: {e}")
        
    return probables, stuff_ratings

def calculate_game_run_expectancy(away_team, home_team, sp_away, sp_home, throws_away, throws_home,
                                   stuff_ratings, bp_eras, park_factors, platoon_splits, baselines=None):
    """Compute empirical game run-expectancy and single-game win probability.
    
    Integrates:
    - SP expected run suppression from Stuff+ (58% game weight)
    - Bullpen ERA from Host 2 actual_stats (42% game weight)
    - Lineup platoon splits vs. starter throwing hand (LHP vs RHP)
    - Venue run factors from mlb_park_factors
    - Pythagenpat non-linear run-to-win conversion
    """
    if baselines is None:
        baselines = TEAM_BASELINES
        
    base_a = baselines.get(away_team, 0.50)
    base_h = baselines.get(home_team, 0.50)
    
    # 1. Starting Pitcher Expected Run Prevention (xERA from Stuff+)
    stuff_a = stuff_ratings.get(sp_away.lower(), 100.0) if sp_away else 100.0
    xera_sp_a = max(2.50, min(6.00, 4.10 - (stuff_a - 100.0) * 0.035))
    
    stuff_h = stuff_ratings.get(sp_home.lower(), 100.0) if sp_home else 100.0
    xera_sp_h = max(2.50, min(6.00, 4.10 - (stuff_h - 100.0) * 0.035))
    
    # 2. Bullpen Run Prevention (RP ERA from actual_stats)
    bp_a = bp_eras.get(away_team, 4.15)
    bp_h = bp_eras.get(home_team, 4.15)
    
    # Blended RA9 (58% SP / 42% RP)
    ra9_pitching_a = 0.58 * xera_sp_a + 0.42 * bp_a
    ra9_pitching_h = 0.58 * xera_sp_h + 0.42 * bp_h
    
    # 3. Offense True Talent & Platoon Splits
    rs_base_a = 4.45 * math.sqrt(max(0.01, base_a / max(0.01, 1.0 - base_a)))
    rs_base_h = 4.45 * math.sqrt(max(0.01, base_h / max(0.01, 1.0 - base_h)))
    
    # Platoon split against opposing SP throwing hand
    split_a = platoon_splits.get(away_team, 0.0) if str(throws_home or "").upper().startswith("L") else 0.0
    rs_adj_a = rs_base_a * (1.0 + split_a)
    
    split_h = platoon_splits.get(home_team, 0.0) if str(throws_away or "").upper().startswith("L") else 0.0
    rs_adj_h = rs_base_h * (1.0 + split_h)
    
    # 4. Venue Park Factor & Home Field Advantage
    pf = park_factors.get(home_team, 1.00)
    hfa_runs = 1.05  # ~0.22 runs advantage for home team
    
    # Expected game runs scored: RS * (RA9_opp / LeagueAvgRA9) * ParkFactor
    league_ra9 = 4.45
    exp_runs_a = (rs_adj_a * ra9_pitching_h / league_ra9) * pf
    exp_runs_h = (rs_adj_h * ra9_pitching_a / league_ra9) * pf * hfa_runs
    
    # 5. Pythagenpat Win Probability
    total_runs = max(1.0, exp_runs_a + exp_runs_h)
    exp = total_runs ** 0.285
    p_a_unbounded = (exp_runs_a ** exp) / ((exp_runs_a ** exp) + (exp_runs_h ** exp))
    p_away = min(0.95, max(0.05, p_a_unbounded))
    p_home = round(1.0 - p_away, 4)
    p_away = round(p_away, 4)
    
    return p_away, p_home, round(exp_runs_a, 2), round(exp_runs_h, 2)

def get_open_game_markets():
    """Fetch active KXMLBGAME markets from Kalshi."""
    now_pt = datetime.now(PT)
    today_key = now_pt.strftime("%y%b%d").upper() # e.g. 26SEP19
    
    try:
        url = f"{KALSHI_API}/markets?series_ticker=KXMLBGAME&status=open&limit=100"
        req = urllib.request.Request(url, headers={"User-Agent": "ZeroQuant/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            markets = data.get("markets", [])
            # Filter for today's games
            return [m for m in markets if today_key in m.get("ticker", "")]
    except Exception as e:
        print(f"Error fetching Kalshi game markets: {e}")
        return []

def evaluate_daily_matchups(min_edge=0.05):
    """Scan all today's MLB games, compute fair value, and identify positive-EV plays."""
    probables, stuff_ratings = load_probables_and_stuff()
    park_factors = load_park_factors()
    bp_eras = load_bullpen_eras()
    platoon_splits = load_team_platoon_splits()
    markets = get_open_game_markets()
    
    opportunities = []
    
    # Map markets by ticker/matchup
    for m in markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")
        yes_ask = float(m.get("yes_ask_dollars") or 0.0)
        yes_bid = float(m.get("yes_bid_dollars") or 0.0)
        
        # Parse team from ticker e.g. KXMLBGAME-26SEP192110SFLAD-LAD -> LAD
        parts = ticker.split("-")
        if len(parts) < 3:
            continue
        team_code = parts[2].upper()
        matchup_str = parts[1][11:] # strip 26SEP19HHMM -> e.g. SFLAD (AwaySF HomeLAD)
        
        # Determine home and away
        # In Kalshi tickers, format is usually AWAY + HOME e.g. SFLAD -> SF @ LAD
        if len(matchup_str) >= 4:
            # Match against known team codes
            team_a = None
            team_b = None
            for code in TEAM_BASELINES:
                if matchup_str.startswith(code):
                    team_a = code
                    team_b = matchup_str[len(code):]
                    break
            if not team_a or team_b not in TEAM_BASELINES:
                continue
                
            away_team = team_a
            home_team = team_b
            
            # Starting pitcher info & handedness
            prob_away = probables.get(away_team, {})
            prob_home = probables.get(home_team, {})
            sp_away = prob_away.get("sp")
            sp_home = prob_home.get("sp")
            throws_away = prob_away.get("throws", "R")
            throws_home = prob_home.get("throws", "R")
            
            # Empirical Run-Expectancy & Platoon Splits (Issue #4)
            p_away, p_home, runs_away, runs_home = calculate_game_run_expectancy(
                away_team, home_team, sp_away, sp_home, throws_away, throws_home,
                stuff_ratings, bp_eras, park_factors, platoon_splits
            )
            
            p_model = p_home if team_code == home_team else p_away
            target_sp = sp_home if team_code == home_team else sp_away
            opp_sp = sp_away if team_code == home_team else sp_home
            
            # 1. Check BUY YES
            if 0.05 <= yes_ask <= 0.95:
                edge_yes = p_model - yes_ask
                if edge_yes >= min_edge:
                    opportunities.append({
                        "game_id": parts[1],
                        "ticker": ticker,
                        "title": title,
                        "team": team_code,
                        "opp": away_team if team_code == home_team else home_team,
                        "side": "YES",
                        "model_prob": p_model,
                        "market_price": yes_ask,
                        "edge": edge_yes,
                        "sp": target_sp,
                        "opp_sp": opp_sp
                    })
                    
            # 2. Check BUY NO
            if 0.05 <= yes_bid <= 0.95:
                no_ask = round(1.0 - yes_bid, 3)
                p_model_no = 1.0 - p_model
                edge_no = p_model_no - no_ask
                if edge_no >= min_edge:
                    opportunities.append({
                        "game_id": parts[1],
                        "ticker": ticker,
                        "title": title,
                        "team": team_code,
                        "opp": away_team if team_code == home_team else home_team,
                        "side": "NO",
                        "model_prob": p_model_no,
                        "market_price": no_ask,
                        "edge": edge_no,
                        "sp": target_sp,
                        "opp_sp": opp_sp
                    })
                    
    # Best Execution: strictly select the single highest-edge vehicle per game matchup
    best_by_game = {}
    for opp in sorted(opportunities, key=lambda x: x["edge"], reverse=True):
        gid = opp["game_id"]
        if gid not in best_by_game:
            best_by_game[gid] = opp
            
    return sorted(best_by_game.values(), key=lambda x: x["edge"], reverse=True)

def settle_games(conn):
    """Check Kalshi API for finalized MLB games and settle positions."""
    cur = conn.execute("SELECT * FROM positions WHERE status = 'OPEN' AND ticker LIKE 'KXMLBGAME%'")
    open_games = cur.fetchall()
    if not open_games:
        return 0, 0.0
        
    settled = 0
    total_pnl = 0.0
    now_str = datetime.now(PT).isoformat()
    
    for pos in open_games:
        ticker = pos["ticker"]
        try:
            url = f"{KALSHI_API}/markets/{ticker}"
            req = urllib.request.Request(url, headers={"User-Agent": "ZeroQuant/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                m = data.get("market", {})
                status = m.get("status")
                result = (m.get("result") or "").lower()
                
                if status in ["finalized", "closed", "determined", "settled"] and result in ["yes", "no"]:
                    won = (pos["side"].lower() == result)
                    settled_price = 1.00 if won else 0.00
                    payout = pos["contracts"] * settled_price
                    realized_pnl = round(payout - pos["total_cost"], 2)
                    
                    with conn:
                        conn.execute("""
                            UPDATE positions
                            SET status = 'RESOLVED', settled_at = ?, settled_price = ?, realized_pnl = ?
                            WHERE id = ?
                        """, (now_str, settled_price, realized_pnl, pos["id"]))
                        conn.execute("""
                            UPDATE portfolio
                            SET cash = cash + ?, realized_pnl = realized_pnl + ?, last_updated = ?
                            WHERE id = 1
                        """, (payout, realized_pnl, now_str))
                    settled += 1
                    total_pnl += realized_pnl
        except Exception:
            pass
            
    return settled, total_pnl

def main():
    parser = argparse.ArgumentParser(description="MLB Daily Matchup Quant & Kalshi Arbitrage")
    parser.add_argument("command", choices=["scan", "trade", "settle", "status"], default="scan", nargs="?", help="Action to execute")
    parser.add_argument("--min-edge", type=float, default=0.05, help="Minimum EV edge (default 0.05 / +5%)")
    args = parser.parse_args()
    
    conn = get_db()
    
    if args.command == "settle":
        count, pnl = settle_games(conn)
        print(f"Settled {count} MLB game markets. Realized P&L: ${pnl:+.2f}")
        return
        
    if args.command in ["scan", "trade"]:
        print("Evaluating today's MLB daily games against Ivy's sabermetric database...")
        opps = evaluate_daily_matchups(args.min_edge)
        
        print(f"\nFound {len(opps)} positive-EV daily matchup opportunities (Threshold: +{args.min_edge*100:.0f}% edge):")
        for i, opp in enumerate(opps, 1):
            print(f"[{i}] {opp['team']} vs {opp['opp']} -> {opp['ticker']}")
            print(f"    Action: BUY {opp['side']} @ ${opp['market_price']:.2f} | Model: {opp['model_prob']*100:.1f}% | Edge: {opp['edge']*100:+.1f}%")
            if opp.get("sp"):
                print(f"    Pitcher: {opp['sp']} (vs {opp['opp_sp']})")
                
            if args.command == "trade":
                success, msg = execute_trade(conn, {
                    "ticker": opp["ticker"],
                    "title": opp["title"],
                    "side": opp["side"],
                    "market_price": opp["market_price"],
                    "edge": opp["edge"],
                    "model_prob": opp["model_prob"],
                    "category": "mlb_daily"
                })
                status_flag = "EXEC" if success else "SKIP"
                print(f"    [{status_flag}]: {msg}")
                
        print()
        print_status(conn)
        return
        
    if args.command == "status":
        print_status(conn)

if __name__ == "__main__":
    main()
