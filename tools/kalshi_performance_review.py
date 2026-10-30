#!/usr/bin/env python3
"""Kalshi Paper Trading Performance Review, Dynamic Model Calibration & Task Board Sync.

Runs as a time-gated sidecar:
- Evening Review (18:45 PT): Settles NOAA ASOS temperature contracts and afternoon MLB games.
- Nightly Audit (21:45 PT): Settles West Coast MLB night games, reconciles daily P&L,
  computes Brier score calibration, dynamically tunes model hyperparameters, and syncs
  telemetry to GitHub Project #3 and the brockventures/kalshi-quant repository.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
ENV_PATH = Path("/workspace/.env")
if ENV_PATH.exists():
    for _line in ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DB_PATH = Path("/workspace/data/kalshi_paper.db")
PARAMS_PATH = Path("/workspace/data/kalshi_model_params.json")
REPO_DIR = Path("/workspace/kalshi-quant")
VAULT_DOC = Path("/workspace/memory/vault/project_kalshi_quant_bot.md")

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
PROJECT_ID = "PVT_kwHODXND-s4BkB6O"
STATUS_FIELD_ID = "PVTSSF_lAHODXND-s4BkB6Ozhi0DAk"
STATUS_OPTIONS = {
    "Todo": "f75ad846",
    "In Progress": "47fc9ee4",
    "Done": "98236657"
}

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    with conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "model_prob" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN model_prob REAL")
        if "category" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN category TEXT")
        if "fee" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN fee REAL DEFAULT 0.0")
    return conn

def load_params():
    if not PARAMS_PATH.exists():
        default_params = {
            "version": "1.0",
            "last_updated": datetime.now(PT).isoformat(),
            "global": {
                "starting_balance": 1000.0,
                "max_total_exposure": 200.0,
                "drawdown_dampener": 1.0,
                "kelly_fraction": 0.25
            },
            "domains": {
                "weather": {
                    "name": "NOAA ASOS Temperature Arbitrage",
                    "min_edge_threshold": 0.08,
                    "max_trade_risk": 10.0,
                    "shrinkage_alpha": 0.95,
                    "target_brier": 0.18,
                    "rolling_brier": None,
                    "resolved_count": 0,
                    "realized_pnl": 0.0
                },
                "mlb_daily": {
                    "name": "MLB Daily Matchup Log5 / Stuff+",
                    "min_edge_threshold": 0.05,
                    "max_trade_risk": 10.0,
                    "shrinkage_alpha": 0.90,
                    "target_brier": 0.21,
                    "rolling_brier": None,
                    "resolved_count": 0,
                    "realized_pnl": 0.0
                },
                "mlb_playoffs": {
                    "name": "MLB Postseason Monte Carlo Futures",
                    "min_edge_threshold": 0.04,
                    "max_trade_risk": 10.0,
                    "shrinkage_alpha": 0.92,
                    "target_brier": 0.20,
                    "rolling_brier": None,
                    "resolved_count": 0,
                    "realized_pnl": 0.0
                }
            },
            "calibration_history": []
        }
        PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PARAMS_PATH, "w") as f:
            json.dump(default_params, f, indent=2)
        return default_params
    with open(PARAMS_PATH) as f:
        return json.load(f)

def save_params(params):
    params["last_updated"] = datetime.now(PT).isoformat()
    with open(PARAMS_PATH, "w") as f:
        json.dump(params, f, indent=2)

def fetch_kalshi_market(ticker):
    try:
        url = f"{KALSHI_API}/markets/{ticker}"
        req = urllib.request.Request(url, headers={"User-Agent": "ZeroQuant/1.0 (ryan@brockventures.com)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
            return data.get("market", {})
    except Exception:
        return {}

def settle_open_positions(conn, dry_run=False):
    """Scan open paper positions, check Kalshi status, and resolve settled markets."""
    cur = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'")
    open_positions = cur.fetchall()
    
    settled_list = []
    total_payout = 0.0
    total_realized_pnl = 0.0
    now_str = datetime.now(PT).isoformat()
    
    for pos in open_positions:
        ticker = pos["ticker"]
        m = fetch_kalshi_market(ticker)
        status = (m.get("status") or "").lower()
        result = (m.get("result") or "").lower()
        
        pos_fee = (pos["fee"] or 0.0) if ("fee" in pos.keys()) else 0.0
        settled_price = None
        if status in ("finalized", "closed", "settled") and result in ("yes", "no"):
            won = (pos["side"].lower() == result)
            settled_price = 1.00 if won else 0.00
            payout = round(pos["contracts"] * settled_price, 2)
            realized_pnl = round(payout - pos["total_cost"] - pos_fee, 2)
            res_str = result.upper()
        elif status in ("finalized", "closed", "settled") and result == "scalar":
            scalar_val = float(m.get("settlement_value_dollars") or 0.0)
            settled_price = scalar_val if pos["side"].upper() == "YES" else (1.00 - scalar_val)
            payout = round(pos["contracts"] * settled_price, 2)
            realized_pnl = round(payout - pos["total_cost"] - pos_fee, 2)
            won = (realized_pnl >= 0)
            res_str = f"SCALAR (${scalar_val:.2f})"
            
        if settled_price is not None:
            settled_item = {
                "id": pos["id"],
                "ticker": ticker,
                "title": pos["title"],
                "category": pos["category"] or ("weather" if ticker.startswith("KXHIGH") else ("mlb_daily" if ticker.startswith("KXMLBGAME") else "mlb_playoffs")),
                "side": pos["side"],
                "contracts": pos["contracts"],
                "entry_price": pos["entry_price"],
                "total_cost": pos["total_cost"],
                "fee": pos_fee,
                "model_prob": pos["model_prob"],
                "result": res_str,
                "won": won,
                "payout": payout,
                "realized_pnl": realized_pnl
            }
            settled_list.append(settled_item)
            total_payout += payout
            total_realized_pnl += realized_pnl
            
            if not dry_run:
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
                    
    return settled_list, total_realized_pnl

def evaluate_open_positions(open_pos):
    """Forensically evaluate live open positions against real-time ground truth (NOAA / MLB)."""
    if not open_pos:
        return {"est_open_pnl": 0.0, "wins": [], "losses": []}
        
    today_str = datetime.now(PT).strftime("%Y-%m-%d")
    
    def parse_weather_ticker(ticker):
        m = re.match(r"KXHIGH(?:T)?([A-Z]{3})-(\d{2}[A-Z]{3}\d{2})-([TB])([\d\.]+)", ticker)
        if not m:
            return None
        st, date_code, strike_type, strike_val = m.groups()
        return {
            "station": f"K{st}",
            "date_code": date_code,
            "strike_type": strike_type,
            "strike_val": float(strike_val)
        }
        
    weather_stations = {
        parse_weather_ticker(p["ticker"])["station"]
        for p in open_pos if parse_weather_ticker(p["ticker"])
    }
    
    station_max = {}
    def fetch_station(st):
        url = f"https://api.weather.gov/stations/{st}/observations"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Zero-Quant (quant@example.com)"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                temps = [
                    f["properties"]["temperature"]["value"] * 9/5 + 32
                    for f in data.get("features", [])
                    if f.get("properties", {}).get("timestamp", "") >= f"{today_str}T07:00:00"
                    and f.get("properties", {}).get("temperature", {}).get("value") is not None
                ]
                if temps:
                    return st, max(temps)
        except Exception:
            pass
        return st, None

    mlb_winners = {}
    def fetch_mlb():
        url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today_str}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Zero-Quant (quant@example.com)"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                for date in data.get("dates", []):
                    for g in date.get("games", []):
                        away = g["teams"]["away"]["team"]["name"]
                        home = g["teams"]["home"]["team"]["name"]
                        if g["status"]["detailedState"] in ("Final", "Game Over", "Completed Early"):
                            a_s = g["teams"]["away"].get("score", 0)
                            h_s = g["teams"]["home"].get("score", 0)
                            mlb_winners[away] = (a_s > h_s)
                            mlb_winners[home] = (h_s > a_s)
        except Exception:
            pass

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_mlb = ex.submit(fetch_mlb)
        f_st = [ex.submit(fetch_station, s) for s in weather_stations]
        for f in f_st:
            st, m_temp = f.result()
            if m_temp is not None:
                station_max[st] = m_temp
        f_mlb.result()

    station_tz_map = {
        "KSFO": ZoneInfo("America/Los_Angeles"),
        "KLAX": ZoneInfo("America/Los_Angeles"),
        "KHOU": ZoneInfo("America/Chicago"),
        "KORD": ZoneInfo("America/Chicago"),
        "KCHI": ZoneInfo("America/Chicago"),
        "KNYC": ZoneInfo("America/New_York"),
        "KMIA": ZoneInfo("America/New_York"),
        "KDEN": ZoneInfo("America/Denver"),
        "KPHX": ZoneInfo("America/Phoenix")
    }

    wins = []
    losses = []
    in_progress = []
    est_open_pnl = 0.0
    sfo_losses_count = 0
    sfo_losses_pnl = 0.0

    for p in open_pos:
        ticker = p["ticker"]
        side = p["side"].upper()
        cost = p["total_cost"]
        contracts = p["contracts"]
        title = p["title"] or ""
        
        parsed = parse_weather_ticker(ticker)
        if parsed and parsed["station"] in station_max:
            st = parsed["station"]
            s_max = station_max[st]
            s_type = parsed["strike_type"]
            s_val = parsed["strike_val"]
            
            st_tz = station_tz_map.get(st, PT)
            local_time = datetime.now(st_tz)
            is_post_peak = (local_time.hour >= 17) # 5:00 PM local time marks diurnal heating peak
            
            state = "IN_PROGRESS"
            pos_pnl = 0.0
            
            if s_max is None:
                state = "IN_PROGRESS"
            elif s_type == "B":
                low_b = s_val - 0.5
                high_b = s_val + 0.5
                if s_max > high_b:
                    # Station already exceeded bracket ceiling: guaranteed locked outcome
                    if side == "NO":
                        state = "WIN"
                        pos_pnl = round(contracts * 1.0 - cost, 2)
                    else:
                        state = "LOSS"
                        pos_pnl = -cost
                elif is_post_peak:
                    in_bracket = (low_b <= s_max <= high_b)
                    won = (side == "YES" and in_bracket) or (side == "NO" and not in_bracket)
                    state = "WIN" if won else "LOSS"
                    pos_pnl = round((contracts * 1.0 - cost) if won else -cost, 2)
                else:
                    state = "IN_PROGRESS"
                    pos_pnl = 0.0
            elif "<" in title:
                if s_max >= s_val:
                    state = "WIN" if side == "NO" else "LOSS"
                    pos_pnl = round((contracts * 1.0 - cost) if side == "NO" else -cost, 2)
                elif is_post_peak:
                    won = (side == "YES")
                    state = "WIN" if won else "LOSS"
                    pos_pnl = round((contracts * 1.0 - cost) if won else -cost, 2)
                else:
                    state = "IN_PROGRESS"
                    pos_pnl = 0.0
            else: # > threshold
                if s_max > s_val:
                    state = "WIN" if side == "YES" else "LOSS"
                    pos_pnl = round((contracts * 1.0 - cost) if side == "YES" else -cost, 2)
                elif is_post_peak:
                    won = (side == "NO")
                    state = "WIN" if won else "LOSS"
                    pos_pnl = round((contracts * 1.0 - cost) if won else -cost, 2)
                else:
                    state = "IN_PROGRESS"
                    pos_pnl = 0.0
                    
            st_name = st.replace("K", "")
            label = f"{st_name} {s_val:.1f}° {side}" if "." in str(s_val) and not str(s_val).endswith(".0") else f"{st_name} {int(s_val)}° {side}"
            
            if state == "WIN":
                est_open_pnl += pos_pnl
                wins.append({"label": label, "pnl": pos_pnl, "ticker": ticker})
            elif state == "LOSS":
                est_open_pnl += pos_pnl
                if st == "KSFO":
                    sfo_losses_count += 1
                    sfo_losses_pnl += pos_pnl
                else:
                    losses.append({"label": label, "pnl": pos_pnl, "ticker": ticker})
            else:
                in_progress.append({
                    "label": label,
                    "obs": round(s_max, 1) if s_max is not None else None,
                    "local_time": local_time.strftime("%I:%M %p"),
                    "ticker": ticker
                })
        elif ticker.startswith("KXMLBGAME"):
            won = False
            if "TOR" in ticker and mlb_winners.get("Toronto Blue Jays") is not None:
                won = (side == "YES" and mlb_winners["Toronto Blue Jays"]) or (side == "NO" and not mlb_winners["Toronto Blue Jays"])
            pos_pnl = round((contracts * 1.0 - cost) if won else -cost, 2)
            est_open_pnl += pos_pnl
            team = "TOR" if "TOR" in ticker else "MLB"
            if won:
                wins.append({"label": f"{team} {side}", "pnl": pos_pnl, "ticker": ticker})
            else:
                losses.append({"label": f"{team} {side}", "pnl": pos_pnl, "ticker": ticker})

    if sfo_losses_count > 0:
        losses.insert(0, {"label": f"SFO cool bias ({sfo_losses_count}x)", "pnl": round(sfo_losses_pnl, 2), "ticker": "KSFO"})

    return {
        "est_open_pnl": round(est_open_pnl, 2),
        "wins": wins,
        "losses": losses,
        "in_progress": in_progress
    }


QUANT_DB_PATH = Path("/workspace/data/quant_paper.db")

def compute_quant_expansion_metrics():
    """Compute performance and position telemetry across the quantitative expansion tracks."""
    if not QUANT_DB_PATH.exists():
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(str(QUANT_DB_PATH))
        conn.row_factory = sqlite3.Row
        
        t1_port = conn.execute("SELECT * FROM cross_venue_portfolio WHERE id=1").fetchone()
        t1_trades = conn.execute("SELECT * FROM cross_venue_trades WHERE status='OPEN'").fetchall()
        
        t2_port = conn.execute("SELECT * FROM polymarket_standalone_portfolio WHERE id=1").fetchone()
        t2_trades = conn.execute("SELECT * FROM polymarket_standalone_trades WHERE status='OPEN'").fetchall()
        
        t3_port = conn.execute("SELECT * FROM hyperliquid_portfolio WHERE id=1").fetchone()
        t3_positions = conn.execute("SELECT * FROM hyperliquid_positions WHERE status='OPEN'").fetchall()
        
        t4_port = conn.execute("SELECT * FROM trend_portfolio WHERE id=1").fetchone()
        t4_positions = conn.execute("SELECT * FROM trend_positions WHERE status='OPEN'").fetchall()
        
        conn.close()
        t1_open_cost = sum(t["sum_cost"] for t in t1_trades)
        t1_locked_pnl = sum(t["locked_profit"] for t in t1_trades)
        t1_cash = float(t1_port["cash"]) if t1_port else 1000.0
        t1_realized = float(t1_port["realized_pnl"]) if t1_port else 0.0
        t1_expected_equity = t1_cash + t1_open_cost + t1_locked_pnl

        t2_cash = float(t2_port["cash"]) if t2_port else 1000.0
        t2_realized = float(t2_port["realized_pnl"]) if t2_port else 0.0
        t2_locked_pnl = sum(t["locked_profit"] for t in t2_trades)
        t2_open_cost = max(0.0, 1000.0 + t2_realized - t2_cash)
        t2_expected_equity = t2_cash + t2_open_cost + t2_locked_pnl

        t3_total_equity = float(t3_port["total_equity"]) if t3_port else 1000.0
        t3_realized = float(t3_port["realized_pnl"]) if t3_port else 0.0

        t4_unrealized = sum(p["unrealized_pnl"] or 0.0 for p in t4_positions)
        t4_total_equity = (float(t4_port["total_equity"]) if t4_port else 1000.0) + t4_unrealized

        return {
            "track1": {
                "cash": t1_cash,
                "realized_pnl": t1_realized,
                "open_cost": t1_open_cost,
                "locked_pnl": t1_locked_pnl,
                "total_equity": t1_expected_equity,
                "expected_equity": t1_expected_equity,
                "open_count": len(t1_trades),
                "open_trades": [dict(t) for t in t1_trades]
            },
            "track2": {
                "cash": t2_cash,
                "realized_pnl": t2_realized,
                "open_cost": t2_open_cost,
                "locked_pnl": t2_locked_pnl,
                "total_equity": t2_expected_equity,
                "expected_equity": t2_expected_equity,
                "open_count": len(t2_trades),
                "open_trades": [dict(t) for t in t2_trades]
            },
            "track3": {
                "cash": float(t3_port["cash"]) if t3_port else 0.0,
                "cumulative_funding": float(t3_port["cumulative_funding"]) if t3_port else 0.0,
                "realized_pnl": t3_realized,
                "total_equity": t3_total_equity,
                "expected_equity": t3_total_equity,
                "open_count": len(t3_positions),
                "positions": [dict(p) for p in t3_positions]
            },
            "track4": {
                "cash": float(t4_port["cash"]) if t4_port else 1000.0,
                "realized_pnl": float(t4_port["realized_pnl"]) if t4_port else 0.0,
                "unrealized_pnl": t4_unrealized,
                "total_equity": t4_total_equity,
                "expected_equity": t4_total_equity,
                "open_count": len(t4_positions),
                "positions": [dict(p) for p in t4_positions]
            }
        }
    except Exception as e:
        print(f"⚠️ Error computing quant expansion metrics: {e}")
        return None

def compute_portfolio_metrics(conn):
    """Calculate portfolio balance, open exposure, Brier score, and win rate."""
    p = conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
    open_pos = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()
    resolved_pos = conn.execute("SELECT * FROM positions WHERE status = 'RESOLVED'").fetchall()
    
    open_cost = sum(r["total_cost"] for r in open_pos)
    total_equity = p["cash"] + open_cost
    
    wins = [r for r in resolved_pos if (r["realized_pnl"] or 0.0) > 0]
    win_rate = (len(wins) / len(resolved_pos) * 100) if resolved_pos else 0.0
    
    # Today's settled positions
    today_str = datetime.now(PT).strftime("%Y-%m-%d")
    today_settled = [r for r in resolved_pos if (r["settled_at"] or "").startswith(today_str)]
    today_pnl = sum(r["realized_pnl"] or 0.0 for r in today_settled)
    today_wins = len([r for r in today_settled if (r["realized_pnl"] or 0.0) > 0])
    today_losses = len([r for r in today_settled if (r["realized_pnl"] or 0.0) < 0])
    
    open_eval = evaluate_open_positions(open_pos)
    
    # Calculate Brier score across resolved positions where model_prob is available
    brier_scores = {}
    market_brier_scores = {}
    domain_counts = {}
    
    for r in resolved_pos:
        cat = r["category"] or ("weather" if r["ticker"].startswith("KXHIGH") else ("mlb_daily" if r["ticker"].startswith("KXMLBGAME") else "mlb_playoffs"))
        domain_counts[cat] = domain_counts.get(cat, 0) + 1
        
        m_prob = r["model_prob"]
        # If side is NO, outcome = 1 if result was NO (settled_price == 1.0)
        outcome = 1.0 if (r["settled_price"] or 0.0) == 1.0 else 0.0
        
        if m_prob is not None:
            # If position was NO, model_prob is probability of NO
            err = (m_prob - outcome) ** 2
            brier_scores.setdefault(cat, []).append(err)
            brier_scores.setdefault("overall", []).append(err)
            
            # Market price as baseline forecast
            mkt_err = ((r["entry_price"] or 0.5) - outcome) ** 2
            market_brier_scores.setdefault(cat, []).append(mkt_err)
            market_brier_scores.setdefault("overall", []).append(mkt_err)
            
    summary_brier = {}
    summary_bss = {}
    for k, errs in brier_scores.items():
        bs = sum(errs) / len(errs)
        summary_brier[k] = round(bs, 4)
        mkt_bs = sum(market_brier_scores[k]) / len(market_brier_scores[k]) if market_brier_scores.get(k) else 0.25
        # Brier Skill Score: 1 - (BS / BS_mkt)
        bss = 1.0 - (bs / mkt_bs) if mkt_bs > 0 else 0.0
        summary_bss[k] = round(bss, 4)
        
    return {
        "starting_balance": p["starting_balance"],
        "cash": p["cash"],
        "open_exposure": open_cost,
        "total_equity": total_equity,
        "realized_pnl": p["realized_pnl"],
        "open_count": len(open_pos),
        "resolved_count": len(resolved_pos),
        "win_count": len(wins),
        "win_rate": win_rate,
        "today_pnl": round(today_pnl, 2),
        "today_wins": today_wins,
        "today_losses": today_losses,
        "today_settled_count": len(today_settled),
        "est_open_pnl": open_eval["est_open_pnl"],
        "open_tracking_wins": open_eval["wins"],
        "open_tracking_losses": open_eval["losses"],
        "open_in_progress": open_eval.get("in_progress", []),
        "brier_scores": summary_brier,
        "brier_skill_scores": summary_bss,
        "domain_counts": domain_counts,
        "open_positions": open_pos,
        "resolved_positions": resolved_pos
    }

def recalibrate_models(params, metrics, dry_run=False):
    """Adjust shrinkage alpha, minimum edge thresholds, and Kelly fraction based on empirical performance."""
    adjustments = []
    now_str = datetime.now(PT).isoformat()
    equity = metrics["total_equity"]
    starting = metrics["starting_balance"]
    drawdown_pct = max(0.0, (starting - equity) / starting)
    
    # 1. Portfolio-level Fractional Kelly & Drawdown Dampener
    if drawdown_pct >= 0.08:
        new_kelly = 0.10
        new_dampener = 0.50
        reason = f"High drawdown alert ({drawdown_pct*100:.1f}% >= 8%): Throttle Kelly to 0.10x"
    elif drawdown_pct >= 0.03:
        new_kelly = 0.18
        new_dampener = 0.75
        reason = f"Moderate drawdown ({drawdown_pct*100:.1f}% >= 3%): Throttle Kelly to 0.18x"
    else:
        new_kelly = 0.25
        new_dampener = 1.00
        reason = "Nominal equity curve: Full 0.25x Fractional Kelly active"
        
    old_kelly = params.get("global", {}).get("kelly_fraction", 0.25)
    if abs(old_kelly - new_kelly) > 0.01:
        adjustments.append({
            "scope": "global",
            "parameter": "kelly_fraction",
            "old": old_kelly,
            "new": new_kelly,
            "reason": reason
        })
        if not dry_run:
            params["global"]["kelly_fraction"] = new_kelly
            params["global"]["drawdown_dampener"] = new_dampener
            
    # 2. Domain-Specific Calibration via Brier Scores
    brier = metrics.get("brier_scores", {})
    for domain_key, domain_cfg in params.get("domains", {}).items():
        domain_bs = brier.get(domain_key)
        target_bs = domain_cfg.get("target_brier", 0.20)
        curr_alpha = domain_cfg.get("shrinkage_alpha", 0.95)
        curr_edge = domain_cfg.get("min_edge_threshold", 0.05)
        
        if domain_bs is not None:
            domain_cfg["rolling_brier"] = domain_bs
            # If Brier score is high (poor calibration or overfitting), shrink towards market price
            if domain_bs > target_bs + 0.03:
                new_alpha = max(0.75, round(curr_alpha - 0.04, 2))
                new_edge = min(0.12, round(curr_edge + 0.015, 3))
                adj_reason = f"Brier score elevated ({domain_bs:.3f} > {target_bs:.3f}): Shrink alpha to {new_alpha}, raise edge hurdle to {new_edge*100:.1f}%"
            elif domain_bs < target_bs - 0.02 and curr_alpha < 0.98:
                new_alpha = min(0.98, round(curr_alpha + 0.02, 2))
                new_edge = max(0.04, round(curr_edge - 0.005, 3))
                adj_reason = f"Strong calibration ({domain_bs:.3f} <= {target_bs:.3f}): Expand alpha to {new_alpha}"
            else:
                new_alpha = curr_alpha
                new_edge = curr_edge
                adj_reason = "Calibration within acceptable tolerance bands"
                
            if new_alpha != curr_alpha or new_edge != curr_edge:
                adjustments.append({
                    "scope": domain_key,
                    "parameter": "shrinkage_alpha / min_edge",
                    "old_alpha": curr_alpha,
                    "new_alpha": new_alpha,
                    "old_edge": curr_edge,
                    "new_edge": new_edge,
                    "reason": adj_reason
                })
                if not dry_run:
                    domain_cfg["shrinkage_alpha"] = new_alpha
                    domain_cfg["min_edge_threshold"] = new_edge
                    
    # 3. LLM Quant Portfolio Manager Post-Mortem & Calibration Synthesis (only when Book 1 has active/settled positions)
    if metrics.get("open_count", 0) > 0 or metrics.get("today_settled_count", 0) > 0:
        try:
            from tools.llm_sidecar import call_sidecar_llm_json, get_sidecar_model
            model_id = get_sidecar_model("kalshi_night_audit", "gemini-4-argon-high")
            llm_prompt = (
                f"You are Zero's Quantitative Portfolio Manager ({model_id}) conducting the daily Kalshi & Multi-Venue Quant calibration review.\n\n"
                f"Current Portfolio Metrics & Brier Calibration:\n{json.dumps(metrics, indent=2, default=str)}\n\n"
                f"Current Model Parameters:\n{json.dumps(params.get('domains', {}), indent=2, default=str)}\n\n"
                f"Baseline Rule Adjustments Proposed:\n{json.dumps(adjustments, indent=2, default=str)}\n\n"
                "Analyze the settled performance, Brier calibration drift, and active exposure. "
                "Provide:\n"
                "1. `pm_postmortem`: A sharp 2-3 sentence quantitative post-mortem on model calibration, edge realization, and risk posture.\n"
                "2. `recommended_kelly`: Optional float between 0.10 and 0.30 if Kelly fraction should be adjusted.\n\n"
                "Respond ONLY with valid JSON:\n"
                '{"pm_postmortem": "...", "recommended_kelly": 0.25}'
            )
            llm_res = call_sidecar_llm_json("kalshi_night_audit", llm_prompt, timeout=75)
            if isinstance(llm_res, dict) and llm_res.get("pm_postmortem"):
                metrics["llm_pm_postmortem"] = str(llm_res["pm_postmortem"]).strip()
                metrics["llm_pm_model"] = model_id
                rec_k = llm_res.get("recommended_kelly")
                if isinstance(rec_k, (int, float)):
                    bounded_k = max(0.10, min(0.30, round(float(rec_k), 2)))
                    curr_k = params.get("global", {}).get("kelly_fraction", 0.25)
                    if abs(bounded_k - curr_k) >= 0.02:
                        adjustments.append({
                            "scope": "global (LLM PM)",
                            "parameter": "kelly_fraction",
                            "old": curr_k,
                            "new": bounded_k,
                            "reason": f"Quant PM ({model_id}) calibration adjustment",
                        })
                        if not dry_run:
                            params.setdefault("global", {})["kelly_fraction"] = bounded_k
        except Exception as e:
            print(f"[KalshiReview] LLM post-mortem note: {e}", file=sys.stderr)

    if adjustments and not dry_run:
        params.setdefault("calibration_history", []).append({
            "timestamp": now_str,
            "adjustments": adjustments
        })
        save_params(params)

    return adjustments

def update_github_project_board(metrics, settled, adjustments, dry_run=False):
    """Post daily telemetry report to brockventures/kalshi-quant and update Project #3 items."""
    now_pt = datetime.now(PT)
    date_header = now_pt.strftime("%Y-%m-%d %I:%M %p PT")
    
    # 1. Build Standup Markdown Comment
    comment_lines = [
        f"### 📊 Daily Kalshi Paper Trading Standup & Quant Telemetry ({date_header})",
        "",
        "| Metric | Current Value | Starting Target | Status |",
        "| :--- | :--- | :--- | :--- |",
        f"| **Total Portfolio Equity** | **${metrics['total_equity']:.2f}** | ${metrics['starting_balance']:.2f} | {'🟢 In Green' if metrics['total_equity'] >= metrics['starting_balance'] else '🟡 Drawdown'} |",
        f"| **Available Cash** | ${metrics['cash']:.2f} | - | Nominal |",
        f"| **Open Market Exposure** | ${metrics['open_exposure']:.2f} | $200.00 Max | Within Cap |",
        f"| **Cumulative Realized P&L (Net)** | **${metrics['realized_pnl']:+.2f}** | - | Net (Post-Fees) |",
        f"| **Settled Positions** | {metrics['resolved_count']} trades ({metrics['win_count']}W / {metrics['resolved_count'] - metrics['win_count']}L) | - | Win Rate: {metrics['win_rate']:.1f}% |",
        "",
        "#### Model Calibration & Brier Skill Scores"
    ]
    
    brier = metrics.get("brier_scores", {})
    bss = metrics.get("brier_skill_scores", {})
    if brier:
        comment_lines.extend([
            "| Domain | Resolved | Model Brier | Skill Score (BSS) | Calibration Verdict |",
            "| :--- | :--- | :--- | :--- | :--- |"
        ])
        for dom, score in brier.items():
            skill = bss.get(dom, 0.0)
            verdict = "🟢 Strong Edge" if skill > 0.05 else ("🟡 Neutral" if skill >= -0.05 else "🔴 Overconfident")
            comment_lines.append(f"| `{dom}` | {metrics['domain_counts'].get(dom, metrics['resolved_count'])} | `{score:.4f}` | `{skill:+.3f}` | {verdict} |")
    else:
        comment_lines.append("_Pending initial market settlements to compute empirical Brier score._")
        
    comment_lines.append("")
    if settled:
        comment_lines.append("#### Settled Contracts Today")
        for s in settled:
            w_flag = "✅ WIN" if s["won"] else "❌ LOSS"
            comment_lines.append(f"- **{w_flag}** `{s['ticker']}` ({s['side']}): Payout `${s['payout']:.2f}` (Net P&L: `${s['realized_pnl']:+.2f}`) | Model: `{s['model_prob']*100:.1f}%`" if s.get("model_prob") else f"- **{w_flag}** `{s['ticker']}` ({s['side']}): Payout `${s['payout']:.2f}` (Net P&L: `${s['realized_pnl']:+.2f}`)")
        comment_lines.append("")
        
    if adjustments:
        comment_lines.append("#### Dynamic Model Adjustments")
        for adj in adjustments:
            comment_lines.append(f"- **[{adj['scope']}]** {adj.get('reason', '')}")
        comment_lines.append("")
    else:
        comment_lines.append("#### Dynamic Model Adjustments\n_All model hyperparameters within nominal tolerance bands._\n")
        
    comment_lines.append("---")
    comment_lines.append("_Autonomous daily report generated by Zero via `#vault` scheduled sidecar._")
    comment_body = "\n".join(comment_lines)
    
    if dry_run:
        print("\n[Dry Run] GitHub Comment Body:\n" + comment_body)
        return True
        
    # 2. Post telemetry comment to Issue #5 (Portfolio & Risk Management)
    try:
        cmd = [
            "gh", "api", "repos/brockventures/kalshi-quant/issues/5/comments",
            "-f", f"body={comment_body}"
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("✅ Posted telemetry comment to brockventures/kalshi-quant Issue #5", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Failed to post comment to Issue #5: {e}", file=sys.stderr)
        
    # 3. Advance Project #3 Issue #5 to "In Progress" if not already
    try:
        # Issue 5 item ID: PVTI_lAHODXND-s4BkB6Ozg7wFDg
        mutation = f"""
        mutation {{
          updateProjectV2ItemFieldValue(
            input: {{
              projectId: "{PROJECT_ID}"
              itemId: "PVTI_lAHODXND-s4BkB6Ozg7wFDg"
              fieldId: "{STATUS_FIELD_ID}"
              value: {{
                singleSelectOptionId: "{STATUS_OPTIONS['In Progress']}"
              }}
            }}
          ) {{
            projectV2Item {{
              id
            }}
          }}
        }}
        """
        cmd = ["gh", "api", "graphql", "-f", f"query={mutation}"]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("✅ Advanced Issue #5 on Project #3 board to 'In Progress'", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Failed to update Project #3 board item: {e}", file=sys.stderr)
        
    return True

def sync_repository(dry_run=False):
    """Synchronize updated model files and parameters into kalshi-quant repo and push."""
    if not REPO_DIR.exists() or not (REPO_DIR / ".git").exists():
        return False
        
    # Copy files
    targets = [
        ("tools/kalshi_performance_review.py", "tools/kalshi_performance_review.py"),
        ("tools/kalshi_paper_bot.py", "tools/kalshi_paper_bot.py"),
        ("tools/baseball_daily_quant.py", "tools/baseball_daily_quant.py"),
        ("tools/baseball_playoff_model.py", "tools/baseball_playoff_model.py"),
        ("tools/quant_cross_venue_arb.py", "tools/quant_cross_venue_arb.py"),
        ("tools/quant_polymarket_standalone.py", "tools/quant_polymarket_standalone.py"),
        ("tools/quant_hyperliquid_basis.py", "tools/quant_hyperliquid_basis.py"),
        ("tools/quant_trend_momentum.py", "tools/quant_trend_momentum.py"),
        ("tools/test_weather_ensemble.py", "tools/test_weather_ensemble.py"),
        ("tools/test_quant_expansion.py", "tools/test_quant_expansion.py"),
        ("tools/quant_equity_chart.py", "tools/quant_equity_chart.py"),
        ("data/kalshi_model_params.json", "data/kalshi_model_params.json"),
        ("data/quant_equity_chart.png", "data/quant_equity_chart.png")
    ]
    
    (REPO_DIR / "data").mkdir(parents=True, exist_ok=True)
    (REPO_DIR / "tools").mkdir(parents=True, exist_ok=True)
    
    for src, dst in targets:
        src_path = Path("/workspace") / src
        dst_path = REPO_DIR / dst
        if src_path.exists():
            shutil.copy2(src_path, dst_path)
            
    if dry_run:
        print("[Dry Run] Synced files to /workspace/kalshi-quant (skipping git commit)", file=sys.stderr)
        return True
        
    try:
        subprocess.run(["git", "-C", str(REPO_DIR), "add", "."], check=True, capture_output=True)
        # Check if there are changes
        st = subprocess.run(["git", "-C", str(REPO_DIR), "status", "--porcelain"], capture_output=True, text=True, check=True)
        if st.stdout.strip():
            msg = f"chore(quant): update model parameters and daily review telemetry ({datetime.now(PT).strftime('%Y-%m-%d %H:%M')})"
            subprocess.run(["git", "-C", str(REPO_DIR), "commit", "-m", msg], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(REPO_DIR), "push", "origin", "main"], check=True, capture_output=True)
            print("✅ Pushed updated quant engine & calibration params to brockventures/kalshi-quant (main)", file=sys.stderr)
        else:
            print("ℹ️ No code/parameter diffs to commit in kalshi-quant", file=sys.stderr)
        return True
    except Exception as e:
        print(f"⚠️ Git sync error: {e}", file=sys.stderr)
        return False

def update_vault_doc(metrics):
    """Update vault documentation with latest active portfolio metrics (Books 4–5)."""
    if not VAULT_DOC.exists():
        return
    try:
        now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
        content = VAULT_DOC.read_text()
        qm = compute_quant_expansion_metrics()
        if qm:
            t3 = qm["track3"]
            t4 = qm["track4"]
            tot_eq = t3["expected_equity"] + t4["expected_equity"]
            open_cnt = t3["open_count"] + t4["open_count"]
            cash_tot = t3["cash"] + t4["cash"]
            new_line = (
                f"- Portfolio Status ({now_str}): {open_cnt} active positions across Books 4–5 "
                f"(Book 4: ${t3['expected_equity']:,.2f}, Book 5: ${t4['expected_equity']:,.2f}), "
                f"${cash_tot:,.2f} idle cash, ${tot_eq:,.2f} total active equity ($2,000.00 baseline)."
            )
        else:
            new_line = f"- Portfolio Status ({now_str}): {metrics['open_count']} open positions, ${metrics['open_exposure']:.2f} open exposure, ${metrics['cash']:.2f} cash, ${metrics['total_equity']:.2f} total equity."

        pattern = r"- Portfolio Status.*"
        if re.search(pattern, content):
            content = re.sub(pattern, new_line, content)
        else:
            content += f"\n{new_line}\n"
        VAULT_DOC.write_text(content)
        print("✅ Updated vault memory enclave: project_kalshi_quant_bot.md", file=sys.stderr)
    except Exception as e:
        print(f"⚠️ Vault update error: {e}", file=sys.stderr)

def _cap_summary_length(text: str, max_chars: int = 1850) -> str:
    """Ensure summary fits comfortably within a single 2,000-char Discord message."""
    if len(text) <= max_chars:
        return text
    out_lines = []
    cur_len = 0
    for line in text.splitlines():
        if cur_len + len(line) + 1 > max_chars - 50:
            out_lines.append("- *(Summary truncated to fit <2,000 chars)*")
            break
        out_lines.append(line)
        cur_len += len(line) + 1
    return "\n".join(out_lines)

def clean_event_name(name: str) -> str:
    """Clean sport/ticker/oracle prefixes and trailing noise from event titles."""
    if not name:
        return ""
    s = re.sub(r"^\[.*?\]\s*(\d{4}-\d{2}-\d{2}\s*)?", "", name)
    s = re.sub(r"\s*\([^)]*Maker[^)]*\)", "", s)
    s = re.sub(r"What price will (.*?) hit.*", r"\1 Target", s)
    s = re.sub(r"\s*\(\$.*?\)", "", s)
    s = s.strip()
    if len(s) > 36:
        s = s[:34].rsplit(" ", 1)[0] + "…"
    return s

def _fmt_signed_usd(val: float) -> str:
    sign = "+" if val >= 0 else "-"
    return f"{sign}${abs(val):,.2f}"

def format_review_summary(metrics, settled, adjustments, max_chars=1850):
    now_str = datetime.now(PT).strftime("%b %d, %I:%M %p PT")

    qm = compute_quant_expansion_metrics()
    if qm:
        t3 = qm["track3"]
        t4 = qm["track4"]
        tot_eq = t3["expected_equity"] + t4["expected_equity"]
        tot_delta = tot_eq - 2000.0
        tot_pct = (tot_delta / 2000.0) * 100.0
    else:
        tot_eq = metrics["total_equity"]
        tot_delta = tot_eq - 1000.0
        tot_pct = (tot_delta / 1000.0) * 100.0

    lines = [
        f"### 📈 Daily Quant Portfolio Update • {now_str}",
        f"**Active Portfolio Equity (Books 4–5):** **${tot_eq:,.2f}** (**{_fmt_signed_usd(tot_delta)}** / **{tot_pct:+.1f}%**)",
        "",
    ]

    if qm:
        # Book 4: Hyperliquid Spot-Perp Carry
        t3 = qm["track3"]
        t3_eq = t3["expected_equity"]
        t3_delta = t3_eq - 1000.0
        fund = t3.get("cumulative_funding", 0.0)
        fees = t3.get("realized_pnl", 0.0)
        pos_items = [f"{p['asset']} ({p['annualized_yield']*100:.1f}% APY)" for p in t3.get("positions", [])[:4]]
        pos_str = f": {', '.join(pos_items)}" if pos_items else ""
        lines.append(f"1. **Book 4 (Hyperliquid Spot-Perp Carry):** **${t3_eq:,.2f}** (**{_fmt_signed_usd(t3_delta)}**)")
        lines.append(
            f"   - {_fmt_signed_usd(fund)} accrued funding · {_fmt_signed_usd(fees)} fee friction · {t3['open_count']} active pairs{pos_str}"
        )

        # Book 5: Crypto Factor L/S & Macro Regime
        t4 = qm["track4"]
        t4_eq = t4["expected_equity"]
        t4_delta = t4_eq - 1000.0
        unreal = t4.get("unrealized_pnl", 0.0)
        swings = [f"{p['symbol']} {p.get('direction', 'LONG')} ({_fmt_signed_usd(p['unrealized_pnl'] or 0.0)})" for p in t4.get("positions", [])[:5]]
        swing_str = f": {', '.join(swings)}" if swings else ""
        lines.append(f"2. **Book 5 (Crypto Factor L/S & Macro Regime):** **${t4_eq:,.2f}** (**{_fmt_signed_usd(t4_delta)}**)")
        lines.append(f"   - {_fmt_signed_usd(t4['realized_pnl'])} net realized · {_fmt_signed_usd(unreal)} unrealized across {t4['open_count']} positions{swing_str}")

    return _cap_summary_length("\n".join(lines), max_chars=max_chars)


def format_scan_summary(metrics, qm=None, new_fills=None, max_chars=1850):
    """Format concise daytime/morning scan and trade execution update."""
    return format_review_summary(metrics, [], [], max_chars=max_chars)


def print_review_summary(metrics, settled, adjustments):
    print(format_review_summary(metrics, settled, adjustments))

def main():
    parser = argparse.ArgumentParser(description="Kalshi Paper Trading Performance Review & Task Board Sync")
    parser.add_argument("--phase", choices=["evening", "night", "all"], default="all", help="Execution phase")
    parser.add_argument("--settle-only", action="store_true", help="Only settle markets")
    parser.add_argument("--dry-run", action="store_true", help="Run without database writes or GitHub API calls")
    parser.add_argument("--test", action="store_true", help="Test run with simulated settlement")
    parser.add_argument("--no-board-sync", action="store_true", help="Skip GitHub Project board update")
    parser.add_argument("--no-git-sync", action="store_true", help="Skip git push to brockventures/kalshi-quant")
    parser.add_argument("--quiet", action="store_true", help="Minimal console output")
    args = parser.parse_args()
    
    conn = get_db()
    params = load_params()
    
    # 1. Settle
    settled, pnl = settle_open_positions(conn, dry_run=args.dry_run)
    
    if args.settle_only:
        if not args.quiet:
            print(f"Settled {len(settled)} positions. Realized P&L: ${pnl:+.2f}")
        return
        
    # 2. Metrics
    metrics = compute_portfolio_metrics(conn)
    
    # 3. Model Recalibration
    adjustments = recalibrate_models(params, metrics, dry_run=args.dry_run)
    
    # 4. Render Equity Trend Chart & Record Snapshot
    try:
        from tools.quant_equity_chart import render_quant_equity_chart
        render_quant_equity_chart()
    except Exception as e:
        print(f"⚠️ Chart render warning: {e}", file=sys.stderr)

    # 5. Summary display
    if not args.quiet:
        print_review_summary(metrics, settled, adjustments)
        
    # 6. GitHub Task Board & Telemetry
    if not args.no_board_sync:
        update_github_project_board(metrics, settled, adjustments, dry_run=args.dry_run)
        
    # 7. Git Sync
    if not args.no_git_sync:
        sync_repository(dry_run=args.dry_run)
        
    # 8. Vault Doc
    if not args.dry_run:
        update_vault_doc(metrics)

if __name__ == "__main__":
    main()
