#!/usr/bin/env python3
"""October MLB Postseason Monte Carlo Model & Kalshi Arbitrage Engine.

Powered by Ivy's PostgreSQL sabermetric database on Host 2 (Host 2:5433).
Simulates the 12-team MLB Postseason tournament (Wild Card bo3, Division Series bo5,
LCS bo7, World Series bo7) across 10,000 iterations using dynamic bracket seeding,
playoff rotation compression (3-man rotation cycling), and bullpen leverage.
Compares model championship probabilities against live Kalshi KXMLB order books.
"""

import argparse
import json
import math
import os
import random
import sys
import urllib.request
from pathlib import Path
import psycopg2

sys.path.insert(0, "/workspace")

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

DIVISIONS = {
    "AL_EAST": ["NYY", "BAL", "BOS", "TB", "TBR", "TOR"],
    "AL_CENTRAL": ["CLE", "CWS", "CHW", "MIN", "DET", "KC", "KCR"],
    "AL_WEST": ["HOU", "SEA", "TEX", "ATH", "OAK", "LAA"],
    "NL_EAST": ["ATL", "PHI", "NYM", "MIA", "WSH"],
    "NL_CENTRAL": ["MIL", "CHC", "STL", "CIN", "PIT"],
    "NL_WEST": ["LAD", "SD", "SDP", "SF", "SFG", "ARI", "COL"]
}

ALIASES = {
    "TBR": "TB",
    "CHW": "CWS",
    "KCR": "KC",
    "OAK": "ATH",
    "SDP": "SD",
    "SFG": "SF"
}

TEAM_NAMES = {
    "LAD": "Los Angeles Dodgers",
    "MIL": "Milwaukee Brewers",
    "PHI": "Philadelphia Phillies",
    "SD": "San Diego Padres",
    "ATL": "Atlanta Braves",
    "CHC": "Chicago Cubs",
    "ARI": "Arizona Diamondbacks",
    "SF": "San Francisco Giants",
    "NYM": "New York Mets",
    "MIA": "Miami Marlins",
    "STL": "St. Louis Cardinals",
    "CIN": "Cincinnati Reds",
    "PIT": "Pittsburgh Pirates",
    "WSH": "Washington Nationals",
    "COL": "Colorado Rockies",
    
    "NYY": "New York Yankees",
    "CLE": "Cleveland Guardians",
    "HOU": "Houston Astros",
    "TEX": "Texas Rangers",
    "BOS": "Boston Red Sox",
    "TB": "Tampa Bay Rays",
    "BAL": "Baltimore Orioles",
    "TOR": "Toronto Blue Jays",
    "MIN": "Minnesota Twins",
    "DET": "Detroit Tigers",
    "CWS": "Chicago White Sox",
    "KC": "Kansas City Royals",
    "SEA": "Seattle Mariners",
    "ATH": "Oakland Athletics",
    "LAA": "Los Angeles Angels"
}

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

def resolve_dynamic_playoff_field():
    """Resolve dynamic playoff seeds 1-6 for AL and NL from Host 2 PostgreSQL standings and stats."""
    wins = {}
    pyth_ratings = {}
    rotations = {}
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Ingest team wins and runs to compute true-talent Pythagenpat
        cur.execute("""
            WITH bat AS (
                SELECT team, SUM(runs) as rs
                FROM actual_stats
                WHERE year = 2026 AND stats_type = 'bat' AND team IS NOT NULL AND team != '- - -'
                GROUP BY team
            ),
            pit AS (
                SELECT team, SUM(wins) as wins, SUM(er) as er, SUM(innings_pitched) as ip
                FROM actual_stats
                WHERE year = 2026 AND stats_type = 'pit' AND team IS NOT NULL AND team != '- - -'
                GROUP BY team
            )
            SELECT b.team, p.wins, b.rs, p.er
            FROM bat b
            JOIN pit p ON b.team = p.team;
        """)
        for r in cur.fetchall():
            raw_team, w, rs, er = r
            team = ALIASES.get(raw_team, raw_team)
            if rs and er and float(rs) > 0 and float(er) > 0:
                rs_val = float(rs)
                ra_est = float(er) * 1.09
                pyth = (rs_val ** 1.83) / ((rs_val ** 1.83) + (ra_est ** 1.83))
                wins[team] = float(w or 0)
                pyth_ratings[team] = round(pyth, 3)
                
        # 2. Query top 3 starting pitchers per team by Stuff+
        for team in list(wins.keys()):
            cur.execute("""
                SELECT a.player_name, COALESCE(p.stuff_plus, 100.0) as sp
                FROM actual_stats a
                LEFT JOIN pitcher_stuff_plus p ON LOWER(a.player_name) = LOWER(p.player_name)
                WHERE a.team = %s AND a.pitcher_type = 'SP' AND a.year = 2026 AND a.gs >= 3
                ORDER BY sp DESC
                LIMIT 3;
            """, (team,))
            starters = cur.fetchall()
            if starters:
                rotations[team] = [{"name": s[0], "stuff_plus": float(s[1])} for s in starters]
            else:
                rotations[team] = [{"name": "Ace", "stuff_plus": 105.0}, {"name": "SP2", "stuff_plus": 100.0}, {"name": "SP3", "stuff_plus": 98.0}]
                
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to load dynamic playoff data from PostgreSQL: {e}")

    if not wins:
        cache_file = Path("/workspace/data/cached_team_baselines.json")
        if cache_file.exists():
            try:
                cached_data = json.loads(cache_file.read_text())
                for t, wpct in cached_data.items():
                    mapped = ALIASES.get(t, t)
                    wins[mapped] = int(float(wpct) * 162)
                    pyth_ratings[mapped] = float(wpct)
                    if mapped not in rotations:
                        rotations[mapped] = [
                            {"name": "Ace", "stuff_plus": 105.0},
                            {"name": "SP2", "stuff_plus": 100.0},
                            {"name": "SP3", "stuff_plus": 98.0},
                        ]
            except Exception as ce:
                print(f"Warning loading disk baseline cache: {ce}")

    def build_league_seeds(league_prefix):
        divs = [d for d in DIVISIONS if d.startswith(league_prefix)]
        div_winners = []
        wild_card_pool = []
        for d in divs:
            teams = [t for t in DIVISIONS[d] if t in wins]
            if not teams:
                continue
            teams.sort(key=lambda t: (wins.get(t, 0), pyth_ratings.get(t, 0.5)), reverse=True)
            div_winners.append(teams[0])
            wild_card_pool.extend(teams[1:])
            
        div_winners.sort(key=lambda t: (wins.get(t, 0), pyth_ratings.get(t, 0.5)), reverse=True)
        wild_card_pool.sort(key=lambda t: (wins.get(t, 0), pyth_ratings.get(t, 0.5)), reverse=True)
        
        field_codes = div_winners[:3] + wild_card_pool[:3]
        field_list = []
        for i, code in enumerate(field_codes):
            field_list.append({
                "code": code,
                "name": TEAM_NAMES.get(code, code),
                "seed": i + 1,
                "league": league_prefix,
                "wins": wins.get(code, 80),
                "true_wpct": pyth_ratings.get(code, 0.520),
                "rotation": rotations.get(code, [{"name": "Ace", "stuff_plus": 105.0}])
            })
        return field_list

    return {
        "AL": build_league_seeds("AL"),
        "NL": build_league_seeds("NL")
    }

def log5_game_prob(wpct_a, wpct_b, home_adv=0.035):
    """Log5 single-game win probability for Team A hosting Team B."""
    p_a = wpct_a
    p_b = wpct_b
    base_prob = (p_a - p_a * p_b) / (p_a + p_b - 2.0 * p_a * p_b)
    return min(0.95, max(0.05, base_prob + home_adv))

def simulate_series_with_rotation(team_a, team_b, length=7, higher_seed_home=True):
    """Simulate best-of-N series with 3-man rotation cycling."""
    wins_needed = (length // 2) + 1
    wins_a = 0
    wins_b = 0
    
    rot_a = team_a.get("rotation") or [{"stuff_plus": 100.0}]
    rot_b = team_b.get("rotation") or [{"stuff_plus": 100.0}]
    
    # 3-man playoff rotation cycling order
    # Game 1: SP1, Game 2: SP2, Game 3: SP3, Game 4: SP1, Game 5: SP2, Game 6: SP1, Game 7: SP2/Bullpen
    cycle = [0, 1, min(2, len(rot_a) - 1), 0, 1, 0, 1]
    
    for game in range(1, length + 1):
        sp_idx_a = cycle[(game - 1) % len(cycle)] % len(rot_a)
        sp_idx_b = cycle[(game - 1) % len(cycle)] % len(rot_b)
        
        stuff_a = rot_a[sp_idx_a]["stuff_plus"]
        stuff_b = rot_b[sp_idx_b]["stuff_plus"]
        
        # Starter quality adjustment
        sp_mod_a = (stuff_a - 100.0) * 0.0012
        sp_mod_b = (stuff_b - 100.0) * 0.0012
        
        adj_wpct_a = min(0.85, max(0.20, team_a["true_wpct"] + sp_mod_a))
        adj_wpct_b = min(0.85, max(0.20, team_b["true_wpct"] + sp_mod_b))
        
        # Home team pattern
        if length == 3:
            a_is_home = True
        elif length == 5:
            a_is_home = game in [1, 2, 5]
        elif length == 7:
            a_is_home = game in [1, 2, 6, 7]
        else:
            a_is_home = True
            
        prob_a = log5_game_prob(adj_wpct_a, adj_wpct_b, home_adv=0.035 if a_is_home else -0.035)
        if random.random() < prob_a:
            wins_a += 1
        else:
            wins_b += 1
            
        if wins_a == wins_needed:
            return team_a
        if wins_b == wins_needed:
            return team_b
            
    return team_a if wins_a > wins_b else team_b

def simulate_full_tournament(field):
    """Simulate complete October 12-team tournament."""
    al = field["AL"]
    nl = field["NL"]
    
    # Wild Card (bo3, higher seed hosts all)
    # Seed 3 vs Seed 6, Seed 4 vs Seed 5
    al_wc1 = simulate_series_with_rotation(al[2], al[5], length=3)
    al_wc2 = simulate_series_with_rotation(al[3], al[4], length=3)
    
    nl_wc1 = simulate_series_with_rotation(nl[2], nl[5], length=3)
    nl_wc2 = simulate_series_with_rotation(nl[3], nl[4], length=3)
    
    # Division Series (bo5, 2-2-1)
    # Seed 1 vs winner(4/5), Seed 2 vs winner(3/6)
    al_ds1 = simulate_series_with_rotation(al[0], al_wc2, length=5)
    al_ds2 = simulate_series_with_rotation(al[1], al_wc1, length=5)
    
    nl_ds1 = simulate_series_with_rotation(nl[0], nl_wc2, length=5)
    nl_ds2 = simulate_series_with_rotation(nl[1], nl_wc1, length=5)
    
    # League Championship Series (bo7, 2-3-2)
    al_champ = simulate_series_with_rotation(al_ds1, al_ds2, length=7)
    nl_champ = simulate_series_with_rotation(nl_ds1, nl_ds2, length=7)
    
    # World Series (bo7, 2-3-2)
    # Team with better regular season record hosts
    if al_champ["wins"] >= nl_champ["wins"]:
        ws_champ = simulate_series_with_rotation(al_champ, nl_champ, length=7)
    else:
        ws_champ = simulate_series_with_rotation(nl_champ, al_champ, length=7)
        
    return ws_champ["code"]

def calculate_annualized_roc(edge, market_price, days_to_expiry=40):
    """Calculate Return on Capital (ROC) annualized vs capital lockup duration."""
    if market_price <= 0.0 or days_to_expiry <= 0:
        return 0.0
    nominal_return = edge / market_price
    annualized = nominal_return * (365.0 / days_to_expiry)
    return round(annualized, 4)

def run_monte_carlo(n_sims=10000, field=None):
    """Run N Monte Carlo simulations of the October tournament."""
    if field is None:
        field = resolve_dynamic_playoff_field()
        
    champions = {}
    for league in ["AL", "NL"]:
        for t in field[league]:
            champions[t["code"]] = 0
            
    for _ in range(n_sims):
        winner_code = simulate_full_tournament(field)
        champions[winner_code] = champions.get(winner_code, 0) + 1
        
    probs = {team: count / n_sims for team, count in champions.items()}
    return probs, field

def get_kalshi_odds():
    """Fetch live Kalshi World Series market odds."""
    try:
        url = f"{KALSHI_API}/markets?series_ticker=KXMLB&status=open&limit=30"
        req = urllib.request.Request(url, headers={"User-Agent": "ZeroQuant/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            markets = data.get("markets", [])
            
        odds = {}
        for m in markets:
            ticker = m.get("ticker", "")
            parts = ticker.split("-")
            if len(parts) >= 3:
                code = parts[2]
                odds[code] = {
                    "ask": float(m.get("yes_ask_dollars") or 0.0),
                    "bid": float(m.get("yes_bid_dollars") or 0.0),
                    "ticker": ticker,
                    "volume": float(m.get("volume_fp") or 0.0)
                }
        return odds
    except Exception as e:
        print(f"Error fetching Kalshi odds: {e}")
        return {}

def evaluate_edges(n_sims=10000):
    """Run simulation and compare against live Kalshi market books."""
    print(f"Running {n_sims:,} Monte Carlo simulations of the 2026 MLB Postseason (Dynamic Seeds & 3-Man Rotation Compression)...")
    model_probs, field = run_monte_carlo(n_sims)
    kalshi_odds = get_kalshi_odds()
    
    team_dict = {}
    for league in ["AL", "NL"]:
        for t in field[league]:
            team_dict[t["code"]] = t
            
    results = []
    for code, p_model in model_probs.items():
        k = kalshi_odds.get(code, {})
        mkt_ask = k.get("ask", 0.0)
        mkt_bid = k.get("bid", 0.0)
        
        edge_yes = (p_model - mkt_ask) if mkt_ask > 0 else 0.0
        edge_no = ((1.0 - p_model) - (1.0 - mkt_bid)) if mkt_bid > 0 else 0.0
        
        roc_yes = calculate_annualized_roc(edge_yes, mkt_ask, days_to_expiry=40) if mkt_ask > 0 else 0.0
        roc_no = calculate_annualized_roc(edge_no, round(1.0 - mkt_bid, 4), days_to_expiry=40) if mkt_bid > 0 else 0.0
        
        t_info = team_dict.get(code, {})
        results.append({
            "code": code,
            "name": t_info.get("name", TEAM_NAMES.get(code, code)),
            "seed": t_info.get("seed", 0),
            "league": t_info.get("league", ""),
            "model_prob": p_model,
            "mkt_ask": mkt_ask,
            "mkt_bid": mkt_bid,
            "edge_yes": edge_yes,
            "edge_no": edge_no,
            "roc_yes": roc_yes,
            "roc_no": roc_no,
            "ticker": k.get("ticker", "")
        })
        
    return sorted(results, key=lambda x: x["model_prob"], reverse=True)

def main():
    parser = argparse.ArgumentParser(description="October MLB Playoff Model")
    parser.add_argument("--sims", type=int, default=10000, help="Number of Monte Carlo simulations")
    parser.add_argument("--trade", action="store_true", help="Execute paper trades for positive-EV opportunities")
    args = parser.parse_args()
    
    results = evaluate_edges(args.sims)
    
    print("\n=========================================================================================")
    print("      ZERO & IVY 2026 MLB PLAYOFF MONTE CARLO ARBITRAGE (DYNAMIC SEEDS & ROTATION)       ")
    print("=========================================================================================")
    print(f"{'Seed':<5} | {'Team':<5} | {'Model':<7} | {'Mkt Ask':<7} | {'Mkt Bid':<7} | {'Edge YES':<9} | {'Edge NO':<9} | {'Ann. ROC':<9} | {'Top Play':<10}")
    print("-" * 90)
    
    for r in results:
        top_play = "PASS"
        best_roc = 0.0
        if r["edge_yes"] >= 0.05 and r["roc_yes"] >= 0.20:
            top_play = f"BUY YES (+{r['edge_yes']*100:.1f}%)"
            best_roc = r["roc_yes"]
        elif r["edge_no"] >= 0.05 and r["roc_no"] >= 0.20:
            top_play = f"BUY NO (+{r['edge_no']*100:.1f}%)"
            best_roc = r["roc_no"]
            
        print(f"#{r['seed']:<1} {r['league']:<2} | {r['code']:<5} | {r['model_prob']*100:5.1f}%  | {r['mkt_ask']*100:5.1f}%  | {r['mkt_bid']*100:5.1f}%  | {r['edge_yes']*100:+7.1f}%  | {r['edge_no']*100:+7.1f}%  | {best_roc*100:7.1f}%  | {top_play:<10}")
    print("=========================================================================================\n")

    if args.trade:
        from tools.kalshi_paper_bot import get_db, execute_trade, print_status
        conn = get_db()
        executed = []
        for r in results:
            ticker = r.get("ticker")
            if not ticker:
                continue
            opp = None
            if r["edge_yes"] >= 0.05 and r["roc_yes"] >= 0.20:
                opp = {
                    "city": "MLB", "forecast": 0, "ticker": ticker,
                    "title": f"Will {r['name']} win the 2026 Pro Baseball Championship?",
                    "side": "YES", "model_prob": r["model_prob"], "market_price": r["mkt_ask"],
                    "edge": r["edge_yes"], "category": "mlb_playoffs"
                }
            elif r["edge_no"] >= 0.05 and r["roc_no"] >= 0.20:
                opp = {
                    "city": "MLB", "forecast": 0, "ticker": ticker,
                    "title": f"Will {r['name']} win the 2026 Pro Baseball Championship?",
                    "side": "NO", "model_prob": 1.0 - r["model_prob"], "market_price": round(1.0 - r["mkt_bid"], 4),
                    "edge": r["edge_no"], "category": "mlb_playoffs"
                }
            if opp:
                success, msg = execute_trade(conn, opp)
                status_flag = "EXEC" if success else "SKIP"
                print(f"[{status_flag}] {opp['title']} -> {opp['side']} @ ${opp['market_price']:.3f} | Edge: {opp['edge']*100:+.1f}% | ROC: {max(r['roc_yes'], r['roc_no'])*100:.0f}% | {msg}")
                if success:
                    executed.append(opp)
        print(f"\nExecuted {len(executed)} new MLB playoff paper positions.")
        print_status(conn)

if __name__ == "__main__":
    main()
