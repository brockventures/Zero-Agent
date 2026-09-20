#!/usr/bin/env python3
"""Kalshi Autonomous Paper Trading Bot & Weather Arbitrage Engine.

Monitors live Kalshi prediction markets against deterministic signals (NOAA NWS model forecasts),
computes Bayesian/Gaussian fair value vs. central limit order book prices, and executes
zero-risk simulated paper trades with fee and slippage accounting.
"""

import argparse
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
DB_PATH = Path("/workspace/data/kalshi_paper.db")

# NOAA station and Kalshi series mapping
STATIONS = {
    "SFO": {
        "city": "San Francisco",
        "station": "KSFO",
        "grid_url": "https://api.weather.gov/gridpoints/MTR/85,105/forecast",
        "series": "KXHIGHTSFO",
        "std_dev": 1.8
    },
    "LAX": {
        "city": "Los Angeles",
        "station": "KLAX",
        "grid_url": "https://api.weather.gov/gridpoints/LOX/154,44/forecast",
        "series": "KXHIGHLAX",
        "std_dev": 2.0
    },
    "CHI": {
        "city": "Chicago",
        "station": "KORD",
        "grid_url": "https://api.weather.gov/gridpoints/LOT/73,74/forecast",
        "series": "KXHIGHTCHI",
        "std_dev": 2.2
    },
    "HOU": {
        "city": "Houston",
        "station": "KHOU",
        "grid_url": "https://api.weather.gov/gridpoints/HGX/65,97/forecast",
        "series": "KXHIGHTHOU",
        "std_dev": 1.9
    }
}

# Risk Management Caps (Default fallbacks)
STARTING_BALANCE = 1000.00
MAX_TRADE_RISK = 10.00       # $10 max per position
MAX_TOTAL_EXPOSURE = 200.00  # $200 max open risk
MIN_EDGE_THRESHOLD = 0.08    # +8% EV edge required to trade

def load_domain_params(domain="weather"):
    """Load dynamic calibration parameters and risk limits from kalshi_model_params.json."""
    params_path = Path("/workspace/data/kalshi_model_params.json")
    if params_path.exists():
        try:
            with open(params_path) as f:
                data = json.load(f)
                dom = data.get("domains", {}).get(domain, {})
                glob = data.get("global", {})
                return {
                    "min_edge": dom.get("min_edge_threshold", 0.08),
                    "max_trade_risk": dom.get("max_trade_risk", 10.00),
                    "shrinkage_alpha": dom.get("shrinkage_alpha", 0.95),
                    "max_total_exposure": glob.get("max_total_exposure", 200.00),
                    "kelly_fraction": glob.get("kelly_fraction", 0.25),
                    "max_slippage": dom.get("max_slippage_tolerance", glob.get("max_slippage_tolerance", 0.03))
                }
        except Exception:
            pass
    return {
        "min_edge": 0.08,
        "max_trade_risk": 10.00,
        "shrinkage_alpha": 0.95,
        "max_total_exposure": 200.00,
        "kelly_fraction": 0.25,
        "max_slippage": 0.03
    }

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY,
                starting_balance REAL,
                cash REAL,
                realized_pnl REAL,
                last_updated TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT UNIQUE,
                title TEXT,
                side TEXT,
                contracts INTEGER,
                entry_price REAL,
                total_cost REAL,
                opened_at TEXT,
                status TEXT,
                settled_at TEXT,
                settled_price REAL,
                realized_pnl REAL,
                model_prob REAL,
                category TEXT
            )
        """)
        # Ensure schema migrations if columns missing
        cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        if "model_prob" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN model_prob REAL")
        if "category" not in cols:
            conn.execute("ALTER TABLE positions ADD COLUMN category TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                side TEXT,
                action TEXT,
                contracts INTEGER,
                price REAL,
                fee REAL,
                status TEXT,
                reason TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                city TEXT,
                ticker TEXT,
                title TEXT,
                model_prob REAL,
                market_ask REAL,
                market_bid REAL,
                edge REAL,
                action TEXT
            )
        """)
        
        # Init portfolio if empty
        cur = conn.execute("SELECT * FROM portfolio WHERE id = 1")
        if not cur.fetchone():
            now_str = datetime.now(PT).isoformat()
            conn.execute(
                "INSERT INTO portfolio (id, starting_balance, cash, realized_pnl, last_updated) VALUES (1, ?, ?, 0.0, ?)",
                (STARTING_BALANCE, STARTING_BALANCE, now_str)
            )
    return conn

def normal_cdf(x, mean, std):
    return 0.5 * (1.0 + math.erf((x - mean) / (std * math.sqrt(2.0))))

def http_get_json(url, headers=None, timeout=8, max_retries=3, fallback_urls=None):
    """Fetch JSON with exponential backoff, jitter, and automated URL fallback cascade."""
    if headers is None:
        headers = {"User-Agent": "ZeroQuant/1.0 (ryan@brockventures.com)", "Accept": "application/json"}
    urls_to_try = [url]
    if fallback_urls:
        urls_to_try.extend(fallback_urls)
        
    last_err = None
    for target_url in urls_to_try:
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                last_err = e
                # Check HTTP error status: abort retries on 4xx (except 429)
                status_code = getattr(e, "code", None)
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    break
                if attempt < max_retries - 1:
                    sleep_time = min(4.0, (1.5 ** attempt) * 0.3 + random.uniform(0.05, 0.25))
                    time.sleep(sleep_time)
                    
    raise last_err or RuntimeError(f"Failed to fetch {url} and all fallback URLs")

def get_noaa_forecast(config):
    """Fetch current day forecasted high temperature from NOAA NWS."""
    try:
        data = http_get_json(config["grid_url"], headers={"User-Agent": "ZeroQuant/1.0 (ryan@brockventures.com)", "Accept": "application/geo+json"})
        periods = data.get("properties", {}).get("periods", [])
        for p in periods:
            # We want today's high (daytime period)
            if p.get("isDaytime", False):
                temp = p.get("temperature")
                if temp is not None:
                    return float(temp), p.get("shortForecast", "")
        # Fallback to first period if daytime flag missing
        if periods and periods[0].get("temperature") is not None:
            return float(periods[0].get("temperature")), periods[0].get("shortForecast", "")
    except Exception as e:
        pass
    return None, None

def get_kalshi_markets(series_ticker):
    """Fetch active markets for a given series on Kalshi."""
    try:
        url = f"{KALSHI_API}/markets?series_ticker={series_ticker}&status=open&limit=30"
        data = http_get_json(url)
        return data.get("markets", [])
    except Exception:
        return []

def parse_strike_bounds(title, ticker):
    """Extract strike bounds from title (e.g. '72-73°', '<70°', '>77°')."""
    # Check for <
    if "<" in title or "less" in title.lower():
        parts = title.split("<")
        if len(parts) > 1:
            val_str = parts[1].split("°")[0].strip()
            try:
                return "less", float(val_str)
            except ValueError:
                pass
    # Check for >
    if ">" in title or "greater" in title.lower():
        parts = title.split(">")
        if len(parts) > 1:
            val_str = parts[1].split("°")[0].strip()
            try:
                return "greater", float(val_str)
            except ValueError:
                pass
    # Check for range X-Y°
    if "-" in title:
        import re
        m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)°", title)
        if m:
            return "range", (float(m.group(1)), float(m.group(2)))
    return None, None

def calculate_bracket_prob(strike_type, bounds, mean, std):
    """Compute Gaussian probability mass for the bracket."""
    if strike_type == "less":
        # e.g. <70 means 69 or less (cutoff 69.5)
        return normal_cdf(bounds - 0.5, mean, std)
    elif strike_type == "greater":
        # e.g. >77 means 78 or greater (cutoff 77.5)
        return 1.0 - normal_cdf(bounds + 0.5, mean, std)
    elif strike_type == "range":
        low, high = bounds
        # range low to high means [low - 0.5, high + 0.5]
        p_high = normal_cdf(high + 0.5, mean, std)
        p_low = normal_cdf(low - 0.5, mean, std)
        return max(0.0, p_high - p_low)
    return 0.0


def compute_ensemble_spread(models, base_std=2.0):
    """Compute ensemble mean and regime-dependent dynamic standard deviation from model spread."""
    if not models:
        return None, base_std
    temps = [m[1] for m in models]
    mean = sum(temps) / len(temps)
    if len(temps) > 1:
        spread = max(temps) - min(temps)
        variance = sum((t - mean) ** 2 for t in temps) / (len(temps) - 1)
        sample_std = math.sqrt(variance)
        # Regime-dependent blend: compresses when spread is narrow, expands when spread is wide
        dynamic_std = round(min(3.8, max(1.1, 0.50 * sample_std + 0.50 * base_std + (spread - 1.5) * 0.20)), 2)
    else:
        dynamic_std = base_std
    return round(mean, 2), dynamic_std

def get_ensemble_weather_forecast(config):
    """Ingest numerical guidance across NWS period, 12Z/00Z raw model grid, and hourly HRRR/NBM."""
    models = []
    # 1. Period forecast
    fc_p, _ = get_noaa_forecast(config)
    if fc_p is not None:
        models.append(("NWS_Period", float(fc_p)))
        
    # 2. Raw numerical grid maxTemperature
    try:
        raw_url = config.get("grid_url", "").replace("/forecast", "")
        if raw_url:
            data = http_get_json(raw_url, headers={"User-Agent": "ZeroQuant/1.0", "Accept": "application/geo+json"})
            max_t = data.get("properties", {}).get("maxTemperature", {}).get("values", [])
            if max_t and max_t[0].get("value") is not None:
                c_val = float(max_t[0]["value"])
                models.append(("Raw_Grid", round(c_val * 1.8 + 32.0, 1)))
    except Exception:
        pass
        
    # 3. High-resolution hourly guidance (HRRR/NBM hourly curve peak)
    try:
        hourly_url = config.get("grid_url", "") + "/hourly"
        data = http_get_json(hourly_url, headers={"User-Agent": "ZeroQuant/1.0", "Accept": "application/geo+json"})
        periods = data.get("properties", {}).get("periods", [])
        t_hourly = [float(p["temperature"]) for p in periods[:16] if p.get("temperature") is not None]
        if t_hourly:
            models.append(("Hourly_HRRR", round(max(t_hourly), 1)))
    except Exception:
        pass
        
    base_std = config.get("std_dev", 2.0)
    mean, dynamic_std = compute_ensemble_spread(models, base_std=base_std)
    return mean, dynamic_std, models

def get_asos_observed_high(station_code):
    """Fetch today's maximum observed temperature from NOAA ASOS station."""
    try:
        url = f"https://api.weather.gov/stations/{station_code}/observations"
        req = urllib.request.Request(url, headers={"User-Agent": "ZeroQuant/1.0", "Accept": "application/geo+json"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
            obs = data.get("features", [])
            temps_f = []
            now_pt = datetime.now(PT)
            for o in obs[:24]:
                props = o.get("properties", {})
                ts_str = props.get("timestamp")
                val_c = props.get("temperature", {}).get("value")
                if ts_str and val_c is not None:
                    # Convert to deg F
                    val_f = (val_c * 9.0 / 5.0) + 32.0
                    temps_f.append(val_f)
            if temps_f:
                return round(max(temps_f), 1)
    except Exception:
        pass
    return None

def scan_markets():
    """Scan all tracked cities, calculate fair value, and identify mispricings."""
    opportunities = []
    now_pt = datetime.now(PT)
    today_str = now_pt.strftime("%y%b%d").upper() # e.g. 26SEP19
    
    params = load_domain_params("weather")
    min_edge = params.get("min_edge", MIN_EDGE_THRESHOLD)
    alpha = params.get("shrinkage_alpha", 0.95)
    
    print(f"[{now_pt.strftime('%Y-%m-%d %I:%M:%S %p PT')}] Scanning Kalshi weather CLOB against NOAA Multi-Model Ensemble (Edge Hurdle: +{min_edge*100:.1f}%, Shrinkage α: {alpha:.2f})...")
    
    for city_code, cfg in STATIONS.items():
        # Ingest multi-model numerical ensemble (Issue #7)
        ensemble_high, dynamic_std, guidance_models = get_ensemble_weather_forecast(cfg)
        if ensemble_high is None:
            continue
            
        # Ingest ground-truth ASOS observed temperature floor (Issue #2)
        observed_floor = get_asos_observed_high(cfg["station"])
        effective_high = max(ensemble_high, observed_floor) if observed_floor else ensemble_high

        markets = get_kalshi_markets(cfg["series"])
        # Filter for today's event markets
        today_markets = [m for m in markets if today_str in m.get("event_ticker", "") or today_str in m.get("ticker", "")]
        if not today_markets:
            today_markets = markets[:8] # fallback to upcoming
            
        for m in today_markets:
            ticker = m.get("ticker")
            title = m.get("title")
            yes_ask = float(m.get("yes_ask_dollars") or 0.0)
            yes_bid = float(m.get("yes_bid_dollars") or 0.0)
            
            strike_type, bounds = parse_strike_bounds(title, ticker)
            if not strike_type:
                continue
                
            p_raw = calculate_bracket_prob(strike_type, bounds, effective_high, dynamic_std)
            # Apply shrinkage calibration against market midpoint/ask
            ref_mkt = yes_ask if yes_ask > 0.0 else 0.50
            p_model = round(alpha * p_raw + (1.0 - alpha) * ref_mkt, 4)
            
            # 1. Check BUY YES edge
            if yes_ask > 0.0 and yes_ask < 0.95:
                edge_yes = p_model - yes_ask
                if edge_yes >= min_edge:
                    opportunities.append({
                        "city": cfg["city"],
                        "forecast": forecast_high,
                        "ticker": ticker,
                        "title": title,
                        "side": "YES",
                        "model_prob": p_model,
                        "market_price": yes_ask,
                        "edge": edge_yes,
                        "category": "weather",
                        "market_obj": m
                    })
                    
            # 2. Check BUY NO edge
            if yes_bid > 0.05 and yes_bid <= 1.0:
                no_ask = round(1.0 - yes_bid, 2)
                p_model_no = 1.0 - p_model
                edge_no = p_model_no - no_ask
                if edge_no >= min_edge:
                    opportunities.append({
                        "city": cfg["city"],
                        "forecast": forecast_high,
                        "ticker": ticker,
                        "title": title,
                        "side": "NO",
                        "model_prob": p_model_no,
                        "market_price": no_ask,
                        "edge": edge_no,
                        "category": "weather",
                        "market_obj": m
                    })
                    
    return opportunities

def get_clob_orderbook(ticker, depth=10):
    """Fetch live CLOB orderbook depth from Kalshi API."""
    try:
        url = f"{KALSHI_API}/markets/{ticker}/orderbook?depth={depth}"
        data = http_get_json(url)
        return data.get("orderbook_fp")
    except Exception:
        return None

def calculate_vwap_and_slippage(orderbook_fp, side, target_contracts, model_prob, min_edge=0.08, max_slippage=0.03):
    """Compute depth-weighted entry price (VWAP), taker fees, and true slippage from Kalshi CLOB depth.
    
    In Kalshi binary markets:
    - Buying YES takes resting NO bids: ask_yes = 1.0 - bid_no
    - Buying NO takes resting YES bids: ask_no = 1.0 - bid_yes
    """
    if not orderbook_fp:
        return False, "Orderbook depth data unavailable", None

    side_norm = side.upper()
    raw_bids = orderbook_fp.get("no_dollars" if side_norm == "YES" else "yes_dollars", [])
    if not raw_bids:
        return False, f"No resting liquidity available on opposite book to buy {side_norm}", None

    # Parse bids: sort descending by opposite bid price (lowest ask price first)
    bids = []
    for item in raw_bids:
        try:
            p_val = float(item[0])
            s_val = float(item[1])
            if s_val > 0:
                bids.append((p_val, s_val))
        except (ValueError, IndexError):
            continue

    if not bids:
        return False, "Zero valid resting orders on opposite book", None

    bids.sort(key=lambda x: x[0], reverse=True)
    top_ask = round(1.0 - bids[0][0], 4)

    total_filled = 0
    total_cost = 0.0
    total_fees = 0.0
    remaining = target_contracts

    for p_bid, size in bids:
        ask_price = round(1.0 - p_bid, 4)
        if ask_price <= 0.0 or ask_price >= 1.0:
            continue

        # Slippage check against top-of-book ask
        if (ask_price - top_ask) > (max_slippage + 1e-6):
            break

        # Taker fee at this price level: ~0.07 * p * (1-p)
        level_fee_per_contract = 0.07 * ask_price * (1.0 - ask_price)

        # Marginal EV check: if ask + fee >= model_prob, negative EV
        marginal_edge = model_prob - ask_price - level_fee_per_contract
        if marginal_edge <= 0.0:
            break

        avail_contracts = int(math.floor(size))
        if avail_contracts <= 0:
            continue

        take = min(avail_contracts, remaining)
        total_filled += take
        total_cost += take * ask_price
        total_fees += take * level_fee_per_contract
        remaining -= take

        if remaining <= 0:
            break

    if total_filled <= 0:
        return False, "Insufficient book depth or zero positive-EV contracts within slippage tolerance", None

    vwap = total_cost / total_filled
    avg_fee_per_contract = total_fees / total_filled
    net_edge = model_prob - vwap - avg_fee_per_contract
    slippage = vwap - top_ask

    if net_edge < min_edge:
        return False, f"Net edge after slippage/fees (+{net_edge*100:.1f}%) below hurdle (+{min_edge*100:.1f}%)", None

    return True, "OK", {
        "filled_contracts": total_filled,
        "vwap": round(vwap, 4),
        "total_cost": round(total_cost, 2),
        "total_fee": round(max(0.01, total_fees), 2),
        "slippage": round(slippage, 4),
        "top_price": top_ask,
        "net_edge": round(net_edge, 4)
    }

AIRPORT_CLUSTERS = {
    "ORD": "CHI", "MDW": "CHI", "CHIC": "CHI",
    "JFK": "NYC", "LGA": "NYC", "EWR": "NYC", "NYC": "NYC",
    "LAX": "LAX", "BUR": "LAX", "LGB": "LAX",
    "SFO": "BAY", "OAK": "BAY", "SJC": "BAY",
    "MIA": "MIA", "FLL": "MIA",
    "DFW": "TEX", "DAL": "TEX", "IAH": "TEX", "HOU": "TEX"
}

MAX_SECTOR_EXPOSURE = 150.00  # Hard ceiling: max 15% of $1000 capital on single sector
MAX_DATE_EXPOSURE = 150.00    # Hard ceiling: max 15% of $1000 capital on single settlement date
INTRADAY_MAX_LOSS = 50.00     # Hard stop-loss: 5% ($50) daily mark-to-market loss

def extract_settlement_date(ticker: str) -> str:
    """Extract settlement date token from ticker (e.g. 26SEP19 from KXHIGH-26SEP19-T70)."""
    m = re.search(r'(\d{2}[A-Z]{3}\d{2})', ticker.upper())
    if m:
        return m.group(1)
    return datetime.now(PT).strftime("%Y-%m-%d")

def extract_station_or_cluster(ticker: str) -> str:
    ticker_up = ticker.upper()
    for code, cluster in AIRPORT_CLUSTERS.items():
        if code in ticker_up:
            return cluster
    return ticker.split("-")[0] if "-" in ticker else ticker

def calculate_covariance_scaling(ticker: str, open_positions: list[dict], max_trade_risk: float = 10.0) -> float:
    """Calculate joint covariance penalty factor based on intra-city strike brackets and regional clusters."""
    if not open_positions:
        return 1.0
        
    date = extract_settlement_date(ticker)
    cluster = extract_station_or_cluster(ticker)
    
    total_penalty = 0.0
    for pos in open_positions:
        pos_ticker = pos.get("ticker", "")
        pos_date = extract_settlement_date(pos_ticker)
        pos_cluster = extract_station_or_cluster(pos_ticker)
        pos_cost = float(pos.get("total_cost", 0.0) or 0.0)
        
        # Intra-city strike brackets on the same date share ~0.90 correlation
        if date == pos_date and cluster == pos_cluster:
            rho = 0.90
        elif cluster == pos_cluster:
            rho = 0.80
        elif date == pos_date and pos.get("category") == ("weather" if ticker.startswith("KXHIGH") else "mlb_daily"):
            rho = 0.35
        else:
            rho = 0.0
            
        if rho > 0.0:
            weight = min(2.0, pos_cost / max(1.0, max_trade_risk))
            total_penalty += rho * weight
            
    scaling = 1.0 / (1.0 + total_penalty)
    return round(max(0.10, min(1.0, scaling)), 4)

def check_exposure_ceilings(conn, ticker: str, category: str, allocated_risk: float, max_cap: float = 150.00) -> tuple[bool, str]:
    """Enforce hard ceilings of max 15% ($150) capital at risk on any single sector or settlement date."""
    # 1. Sector check
    cur = conn.execute("SELECT SUM(total_cost) as total FROM positions WHERE status = 'OPEN' AND category = ?", (category,))
    row = cur.fetchone()
    current_sector = (row["total"] if row and row["total"] else 0.0)
    if current_sector + allocated_risk > max_cap:
        return False, f"Sector exposure limit hit for {category} (${current_sector:.2f} + ${allocated_risk:.2f} > ${max_cap:.2f})"
        
    # 2. Settlement date check
    target_date = extract_settlement_date(ticker)
    cur = conn.execute("SELECT ticker, total_cost FROM positions WHERE status = 'OPEN'")
    date_total = 0.0
    for p in cur.fetchall():
        if extract_settlement_date(p["ticker"]) == target_date:
            date_total += p["total_cost"]
            
    if date_total + allocated_risk > max_cap:
        return False, f"Settlement date exposure limit hit for {target_date} (${date_total:.2f} + ${allocated_risk:.2f} > ${max_cap:.2f})"
        
    return True, "Exposure ceilings nominal"

def check_intraday_circuit_breaker(conn, max_loss_dollar: float = INTRADAY_MAX_LOSS) -> tuple[bool, str]:
    """Immediate autonomous kill switch: cancels all resting orders and halts trading if daily loss exceeds limit."""
    today_str = datetime.now(PT).strftime("%Y-%m-%d")
    
    # Realized loss today
    cur = conn.execute("""
        SELECT SUM(realized_pnl) as daily_pnl 
        FROM positions 
        WHERE status = 'RESOLVED' AND settled_at LIKE ?
    """, (f"{today_str}%",))
    row = cur.fetchone()
    daily_pnl = row["daily_pnl"] if row and row["daily_pnl"] is not None else 0.0
    
    if daily_pnl <= -max_loss_dollar:
        # Trip the circuit breaker! Autonomous kill switch actions:
        now_str = datetime.now(PT).isoformat()
        cur = conn.execute("SELECT id, contracts, price FROM orders WHERE status = 'RESTING'")
        resting = cur.fetchall()
        total_refund = sum(r["contracts"] * r["price"] for r in resting)
        
        with conn:
            conn.execute("""
                UPDATE orders 
                SET status = 'CANCELLED', reason = reason || ' | Circuit breaker tripped (Daily loss >= $50)'
                WHERE status = 'RESTING'
            """)
            if total_refund > 0:
                conn.execute("UPDATE portfolio SET cash = cash + ?, last_updated = ? WHERE id = 1", (total_refund, now_str))
                
        return True, f"Daily loss of ${abs(daily_pnl):.2f} exceeded stop-loss threshold (${max_loss_dollar:.2f}). Kill switch activated: resting orders cancelled, trading halted."
        
    return False, "Circuit breaker nominal"

def execute_trade(conn, opp):
    """Simulate execution of a paper trade with CLOB depth, VWAP fill, and Fractional Kelly sizing."""
    now_str = datetime.now(PT).isoformat()
    ticker = opp["ticker"]
    title = opp["title"]
    side = opp["side"]
    price = opp["market_price"]
    edge = opp["edge"]
    category = opp.get("category") or ("weather" if ticker.startswith("KXHIGH") else ("mlb_daily" if ticker.startswith("KXMLBGAME") else "mlb_playoffs"))
    model_prob = opp.get("model_prob", price + edge)
    
    # Circuit breaker check: halt immediately if daily loss threshold breached
    tripped, cb_msg = check_intraday_circuit_breaker(conn)
    if tripped:
        return False, f"Trading halted: {cb_msg}"
    
    # Check if position already exists
    cur = conn.execute("SELECT * FROM positions WHERE ticker = ? AND status = 'OPEN'", (ticker,))
    if cur.fetchone():
        return False, "Position already open"
        
    params = load_domain_params(category)
    k_fraction = params.get("kelly_fraction", 0.25)
    max_risk = params.get("max_trade_risk", 10.00)
    max_exposure = params.get("max_total_exposure", 200.00)
    min_edge = params.get("min_edge", MIN_EDGE_THRESHOLD)
    max_slippage = params.get("max_slippage", 0.03)
    
    # Check total exposure
    cur = conn.execute("SELECT SUM(total_cost) as total FROM positions WHERE status = 'OPEN'")
    row = cur.fetchone()
    current_exposure = row["total"] or 0.0
    if current_exposure + max_risk > max_exposure:
        return False, f"Total exposure limit hit (${current_exposure:.2f}/${max_exposure:.2f})"
        
    # Check cash
    cur = conn.execute("SELECT cash FROM portfolio WHERE id = 1")
    cash = cur.fetchone()["cash"]
    if cash < 1.00:
        return False, f"Insufficient cash (${cash:.2f})"
        
    # Fractional Kelly sizing: f* = (p - price) / (1 - price)
    if price < 1.0 and model_prob > price:
        f_star = (model_prob - price) / (1.0 - price)
        kelly_allocation = cash * f_star * k_fraction
        allocated_risk = min(max_risk, max(1.00, kelly_allocation))
    else:
        allocated_risk = max_risk
        
    # Apply correlated portfolio covariance penalty scaling
    cur = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'")
    open_positions = [dict(r) for r in cur.fetchall()]
    cov_scale = calculate_covariance_scaling(ticker, open_positions, max_trade_risk=max_risk)
    allocated_risk = allocated_risk * cov_scale
    
    # Sector and settlement date hard ceilings (max 15% / $150)
    ok_ceil, ceil_msg = check_exposure_ceilings(conn, ticker, category, allocated_risk, max_cap=MAX_SECTOR_EXPOSURE)
    if not ok_ceil:
        return False, ceil_msg
        
    allocated_risk = min(allocated_risk, cash)
    target_contracts = max(1, int(allocated_risk / price))
    
    # Ingest full CLOB order book depth (Issue #3)
    ob_data = get_clob_orderbook(ticker, depth=10)
    if ob_data:
        ok_fill, fill_msg, fill_info = calculate_vwap_and_slippage(
            ob_data, side, target_contracts, model_prob, min_edge=min_edge, max_slippage=max_slippage
        )
        if not ok_fill:
            return False, f"CLOB execution rejected: {fill_msg}"
        contracts = fill_info["filled_contracts"]
        actual_price = fill_info["vwap"]
        total_cost = fill_info["total_cost"]
        fee = fill_info["total_fee"]
        slippage = fill_info["slippage"]
        net_edge = fill_info["net_edge"]
    else:
        # Fallback if orderbook endpoint unavailable
        contracts = target_contracts
        actual_price = price
        total_cost = round(contracts * price, 2)
        fee = round(max(0.01, 0.07 * price * (1.0 - price) * contracts), 2)
        slippage = 0.0
        net_edge = edge
        
    if total_cost + fee > cash:
        return False, f"Cost exceeds cash (${total_cost + fee:.2f} > ${cash:.2f})"

    with conn:
        # Deduct cash
        conn.execute("UPDATE portfolio SET cash = cash - ?, last_updated = ? WHERE id = 1", (total_cost + fee, now_str))
        # Insert position
        conn.execute("""
            INSERT INTO positions (ticker, title, side, contracts, entry_price, total_cost, opened_at, status, model_prob, category)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
        """, (ticker, title, side, contracts, actual_price, total_cost, now_str, model_prob, category))
        # Log order
        conn.execute("""
            INSERT INTO orders (timestamp, ticker, side, action, contracts, price, fee, status, reason)
            VALUES (?, ?, ?, 'BUY', ?, ?, ?, 'FILLED', ?)
        """, (now_str, ticker, side, contracts, actual_price, fee, f"EV Edge: {net_edge*100:+.1f}% net (Model: {model_prob*100:.1f}%, VWAP: ${actual_price:.4f}, Slip: ${slippage:+.4f}, Fee: ${fee:.2f}, Kelly: ${allocated_risk:.2f})"))
        
    return True, f"Bought {contracts}x {side} @ ${actual_price:.4f} (Total: ${total_cost:.2f}, Fee: ${fee:.2f}, Slip: ${slippage:+.4f}, Net Edge: {net_edge*100:+.1f}%)"

def parse_clob_book(orderbook_fp, side="YES"):
    """Parse raw Kalshi orderbook into sorted (price, size) bids and asks for the requested side."""
    if not orderbook_fp:
        return [], []
    raw_bids = orderbook_fp.get("yes_dollars" if side.upper() == "YES" else "no_dollars", [])
    raw_opp = orderbook_fp.get("no_dollars" if side.upper() == "YES" else "yes_dollars", [])
    bids = []
    for item in raw_bids:
        try:
            p, s = float(item[0]), float(item[1])
            if s > 0:
                bids.append((p, s))
        except (ValueError, IndexError):
            continue
    bids.sort(key=lambda x: x[0], reverse=True)
    asks = []
    for item in raw_opp:
        try:
            p, s = float(item[0]), float(item[1])
            if s > 0:
                asks.append((round(1.0 - p, 4), s))
        except (ValueError, IndexError):
            continue
    asks.sort(key=lambda x: x[0])
    return bids, asks

def calculate_multi_level_ofi(orderbook_prev, orderbook_curr, side="YES", levels=3, weights=(1.0, 0.5, 0.25)):
    """Compute multi-level Order Flow Imbalance (OFI) across top N price levels.
    
    Positive OFI indicates net buy pressure (book expanding upward).
    Negative OFI indicates net sell pressure (book thinning/swept downward).
    """
    if not orderbook_prev or not orderbook_curr:
        return 0.0
    bids_prev, asks_prev = parse_clob_book(orderbook_prev, side)
    bids_curr, asks_curr = parse_clob_book(orderbook_curr, side)
    
    total_ofi = 0.0
    for k in range(levels):
        w = weights[k] if k < len(weights) else weights[-1]
        # Bid delta
        bp_prev = bids_prev[k] if k < len(bids_prev) else None
        bp_curr = bids_curr[k] if k < len(bids_curr) else None
        if bp_prev and bp_curr:
            if bp_curr[0] > bp_prev[0]:
                delta_b = bp_curr[1]
            elif bp_curr[0] == bp_prev[0]:
                delta_b = bp_curr[1] - bp_prev[1]
            else:
                delta_b = -bp_prev[1]
        elif bp_curr:
            delta_b = bp_curr[1]
        elif bp_prev:
            delta_b = -bp_prev[1]
        else:
            delta_b = 0.0
            
        # Ask delta
        ap_prev = asks_prev[k] if k < len(asks_prev) else None
        ap_curr = asks_curr[k] if k < len(asks_curr) else None
        if ap_prev and ap_curr:
            if ap_curr[0] < ap_prev[0]:
                delta_a = ap_curr[1]
            elif ap_curr[0] == ap_prev[0]:
                delta_a = ap_curr[1] - ap_prev[1]
            else:
                delta_a = -ap_prev[1]
        elif ap_curr:
            delta_a = ap_curr[1]
        elif ap_prev:
            delta_a = -ap_prev[1]
        else:
            delta_a = 0.0
            
        total_ofi += w * (delta_b - delta_a)
    return round(total_ofi, 4)

def calculate_avellaneda_stoikov_quote(base_price, inventory, gamma=0.05, sigma=0.20, time_horizon=1.0):
    """Dynamically shade resting limit quote downward as same-contract inventory builds up.
    
    Reservation price: r(s, q) = s - q * gamma * sigma^2 * tau
    """
    if inventory == 0:
        return round(base_price, 4)
    skew = inventory * gamma * (sigma ** 2) * time_horizon
    shaded = base_price - skew
    return round(max(0.01, min(0.99, shaded)), 4)

def evaluate_toxic_flow_cancellation(conn, order, prev_book, curr_book, ofi_threshold=-10.0, book_thinning_ratio=0.50):
    """Sub-minute order retraction triggering immediate cancellation when OFI detects toxic flow."""
    if order["status"] != "RESTING":
        return False, 0.0, "Order not in RESTING state"
        
    side = order["side"]
    ofi = calculate_multi_level_ofi(prev_book, curr_book, side=side)
    bids_prev, _ = parse_clob_book(prev_book, side)
    bids_curr, _ = parse_clob_book(curr_book, side)
    
    is_thinned = False
    if bids_prev and bids_curr:
        prev_top_size = bids_prev[0][1]
        curr_top_size = bids_curr[0][1]
        if prev_top_size > 0 and (curr_top_size / prev_top_size) < book_thinning_ratio:
            is_thinned = True
            
    # Trigger cancellation if OFI indicates aggressive taker aggression against position or book thins drastically
    if ofi <= ofi_threshold or is_thinned:
        order_id = order["id"]
        contracts = order["contracts"]
        price = order["price"]
        total_cost = round(contracts * price, 2)
        now_str = datetime.now(PT).isoformat()
        reason_detail = f"Toxic flow / adverse OFI detected (OFI: {ofi:+.2f}{', Book thinned >50%' if is_thinned else ''})"
        with conn:
            conn.execute("""
                UPDATE orders
                SET status = 'CANCELLED', reason = reason || ' | ' || ?
                WHERE id = ?
            """, (reason_detail, order_id))
            conn.execute("UPDATE portfolio SET cash = cash + ?, last_updated = ? WHERE id = 1", (total_cost, now_str))
        return True, ofi, reason_detail
        
    return False, ofi, "Flow nominal"

def submit_post_only_order(conn, opp, timeout_minutes=15):
    """Submit a passive post-only resting limit order at the bid (0% maker fee) with timeout expiry and Avellaneda-Stoikov inventory skew."""
    now_str = datetime.now(PT).isoformat()
    ticker = opp["ticker"]
    title = opp.get("title", ticker)
    side = opp["side"]
    base_price = opp.get("market_bid") or opp["market_price"]
    edge = opp.get("edge", 0.08)
    category = opp.get("category") or ("weather" if ticker.startswith("KXHIGH") else ("mlb_daily" if ticker.startswith("KXMLBGAME") else "mlb_playoffs"))
    model_prob = opp.get("model_prob", base_price + edge)
    
    # Circuit breaker check: halt immediately if daily loss threshold breached
    tripped, cb_msg = check_intraday_circuit_breaker(conn)
    if tripped:
        return False, f"Trading halted: {cb_msg}"
        
    cur = conn.execute("SELECT * FROM positions WHERE ticker = ? AND status = 'OPEN'", (ticker,))
    if cur.fetchone():
        return False, "Position already open"
        
    cur = conn.execute("SELECT * FROM orders WHERE ticker = ? AND status = 'RESTING'", (ticker,))
    if cur.fetchone():
        return False, "Resting limit order already pending"
        
    params = load_domain_params(category)
    k_fraction = params.get("kelly_fraction", 0.25)
    max_risk = params.get("max_trade_risk", 10.00)
    max_exposure = params.get("max_total_exposure", 200.00)
    
    # Query current position inventory for this ticker to apply Avellaneda-Stoikov inventory skew
    cur = conn.execute("SELECT SUM(contracts) as total_qty FROM positions WHERE ticker = ? AND status = 'OPEN'", (ticker,))
    inv_row = cur.fetchone()
    inventory = inv_row["total_qty"] if inv_row and inv_row["total_qty"] else 0
    price = calculate_avellaneda_stoikov_quote(base_price, inventory)
    
    cur = conn.execute("SELECT SUM(total_cost) as total FROM positions WHERE status = 'OPEN'")
    current_exposure = cur.fetchone()["total"] or 0.0
    if current_exposure + max_risk > max_exposure:
        return False, f"Total exposure limit hit (${current_exposure:.2f}/${max_exposure:.2f})"
        
    cur = conn.execute("SELECT cash FROM portfolio WHERE id = 1")
    cash = cur.fetchone()["cash"]
    if cash < 1.00:
        return False, f"Insufficient cash (${cash:.2f})"
        
    if price < 1.0 and model_prob > price:
        f_star = (model_prob - price) / (1.0 - price)
        allocated_risk = min(max_risk, min(cash, max(1.00, cash * f_star * k_fraction)))
    else:
        allocated_risk = min(max_risk, cash)
        
    # Apply correlated portfolio covariance penalty scaling
    cur = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'")
    open_positions = [dict(r) for r in cur.fetchall()]
    cov_scale = calculate_covariance_scaling(ticker, open_positions, max_trade_risk=max_risk)
    allocated_risk = allocated_risk * cov_scale
    
    # Sector and settlement date hard ceilings (max 15% / $150)
    ok_ceil, ceil_msg = check_exposure_ceilings(conn, ticker, category, allocated_risk, max_cap=MAX_SECTOR_EXPOSURE)
    if not ok_ceil:
        return False, ceil_msg
        
    allocated_risk = min(allocated_risk, cash)
    contracts = max(1, int(allocated_risk / price))
    total_cost = round(contracts * price, 2)
    
    if total_cost > cash:
        return False, f"Cost exceeds cash (${total_cost:.2f} > ${cash:.2f})"
        
    with conn:
        # Earmark cash
        conn.execute("UPDATE portfolio SET cash = cash - ?, last_updated = ? WHERE id = 1", (total_cost, now_str))
        # Insert resting order
        conn.execute("""
            INSERT INTO orders (timestamp, ticker, side, action, contracts, price, fee, status, reason)
            VALUES (?, ?, ?, 'BUY_POST_ONLY', ?, ?, 0.00, 'RESTING', ?)
        """, (now_str, ticker, side, contracts, price, f"Post-Only Maker @ bid (0% fee, {timeout_minutes}m timeout, Model: {model_prob*100:.1f}%, InvSkew: {inventory}pos)"))
        
    return True, f"Submitted post-only maker order for {contracts}x {side} @ ${price:.2f} (0% fee, {timeout_minutes}m timeout)"

def process_resting_orders(conn, max_age_minutes=15, force_fill=False, orderbook_pairs=None):
    """Scan and process pending resting limit orders: fills active orders, cancels toxic flow or timed-out orders."""
    cur = conn.execute("SELECT * FROM orders WHERE status = 'RESTING'")
    resting_orders = cur.fetchall()
    if not resting_orders:
        return 0, 0
        
    cancelled_count = 0
    filled_count = 0
    now_dt = datetime.now(PT)
    now_str = now_dt.isoformat()
    
    for order in resting_orders:
        order_id = order["id"]
        ticker = order["ticker"]
        contracts = order["contracts"]
        price = order["price"]
        side = order["side"]
        total_cost = round(contracts * price, 2)
        
        # Check toxic flow if orderbook snapshots provided
        if orderbook_pairs and ticker in orderbook_pairs:
            prev_b, curr_b = orderbook_pairs[ticker]
            cancelled, ofi_val, cancel_msg = evaluate_toxic_flow_cancellation(conn, order, prev_b, curr_b)
            if cancelled:
                cancelled_count += 1
                continue
        
        try:
            order_dt = datetime.fromisoformat(order["timestamp"])
            age_mins = (now_dt - order_dt).total_seconds() / 60.0
        except Exception:
            age_mins = 0.0
            
        if age_mins >= max_age_minutes:
            with conn:
                conn.execute("""
                    UPDATE orders
                    SET status = 'CANCELLED', reason = reason || ' | Expired 15m timeout'
                    WHERE id = ?
                """, (order_id,))
                conn.execute("UPDATE portfolio SET cash = cash + ?, last_updated = ? WHERE id = 1", (total_cost, now_str))
            cancelled_count += 1
        elif force_fill or age_mins >= 3.0:
            category = "weather" if ticker.startswith("KXHIGH") else ("mlb_daily" if ticker.startswith("KXMLBGAME") else "mlb_playoffs")
            with conn:
                conn.execute("UPDATE orders SET status = 'FILLED' WHERE id = ?", (order_id,))
                conn.execute("""
                    INSERT OR IGNORE INTO positions (ticker, title, side, contracts, entry_price, total_cost, opened_at, status, model_prob, category)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """, (ticker, ticker, side, contracts, price, total_cost, now_str, price + 0.08, category))
            filled_count += 1
            
    return cancelled_count, filled_count

def reconcile_live_orders_and_positions(conn, remote_orders=None, remote_positions=None):
    """Reconcile local SQLite orders and positions against live Kalshi API state.
    
    Handles partial fills, orphan/phantom resting orders, and settled positions.
    """
    now_dt = datetime.now(PT)
    now_str = now_dt.isoformat()
    
    reconciled_orders = 0
    reconciled_positions = 0
    orphans_pruned = 0
    partial_fills = 0
    cash_refunded = 0.0
    
    if remote_orders is None:
        try:
            res = http_get_json(f"{KALSHI_API}/portfolio/orders?status=resting,executed,canceled")
            remote_orders = res.get("orders", [])
        except Exception:
            remote_orders = []
            
    if remote_positions is None:
        try:
            res = http_get_json(f"{KALSHI_API}/portfolio/positions")
            remote_positions = res.get("market_positions", [])
        except Exception:
            remote_positions = []
            
    remote_orders_by_id = {str(o.get("order_id")): o for o in remote_orders if o.get("order_id")}
    remote_orders_by_ticker = {o.get("ticker"): o for o in remote_orders if o.get("ticker")}
    
    # 1. Reconcile local RESTING orders
    cur = conn.execute("SELECT * FROM orders WHERE status = 'RESTING'")
    local_resting = cur.fetchall()
    
    for order in local_resting:
        order_id = order["id"]
        ticker = order["ticker"]
        contracts = order["contracts"]
        price = order["price"]
        side = order["side"]
        
        rem = remote_orders_by_id.get(str(order_id)) or remote_orders_by_ticker.get(ticker)
        
        if rem:
            rem_status = (rem.get("status") or "").lower()
            rem_filled = rem.get("filled_count", 0) or rem.get("filled_contracts", 0)
            
            if rem_status in ["executed", "filled"] or rem_filled >= contracts:
                category = "weather" if ticker.startswith("KXHIGH") else ("mlb_daily" if ticker.startswith("KXMLBGAME") else "mlb_playoffs")
                total_cost = round(contracts * price, 2)
                with conn:
                    conn.execute("UPDATE orders SET status = 'FILLED', reason = reason || ' | Reconciled: filled' WHERE id = ?", (order_id,))
                    conn.execute("""
                        INSERT OR IGNORE INTO positions (ticker, title, side, contracts, entry_price, total_cost, opened_at, status, model_prob, category)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                    """, (ticker, ticker, side, contracts, price, total_cost, now_str, price + 0.08, category))
                reconciled_orders += 1
            elif rem_status in ["canceled", "cancelled", "expired"]:
                refund = round(contracts * price, 2)
                with conn:
                    conn.execute("UPDATE orders SET status = 'CANCELLED', reason = reason || ' | Reconciled: remote cancelled' WHERE id = ?", (order_id,))
                    conn.execute("UPDATE portfolio SET cash = cash + ?, last_updated = ? WHERE id = 1", (refund, now_str))
                reconciled_orders += 1
                cash_refunded += refund
            elif rem_filled > 0 and rem_filled < contracts:
                rem_remaining = contracts - rem_filled
                filled_cost = round(rem_filled * price, 2)
                category = "weather" if ticker.startswith("KXHIGH") else ("mlb_daily" if ticker.startswith("KXMLBGAME") else "mlb_playoffs")
                with conn:
                    conn.execute("UPDATE orders SET contracts = ?, reason = reason || ? WHERE id = ?", 
                                 (rem_remaining, f" | Reconciled partial fill ({rem_filled} filled)", order_id))
                    conn.execute("""
                        INSERT OR IGNORE INTO positions (ticker, title, side, contracts, entry_price, total_cost, opened_at, status, model_prob, category)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                    """, (ticker, ticker, side, rem_filled, price, filled_cost, now_str, price + 0.08, category))
                partial_fills += 1
        else:
            try:
                order_dt = datetime.fromisoformat(order["timestamp"])
                age_mins = (now_dt - order_dt).total_seconds() / 60.0
            except Exception:
                age_mins = 0.0
                
            if age_mins >= 3.0:
                refund = round(contracts * price, 2)
                with conn:
                    conn.execute("UPDATE orders SET status = 'CANCELLED', reason = reason || ' | Reconciled: orphan pruned' WHERE id = ?", (order_id,))
                    conn.execute("UPDATE portfolio SET cash = cash + ?, last_updated = ? WHERE id = 1", (refund, now_str))
                orphans_pruned += 1
                cash_refunded += refund
                
    # 2. Reconcile local OPEN positions against remote positions
    remote_pos_by_ticker = {p.get("ticker"): p for p in remote_positions if p.get("ticker")}
    cur = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'")
    local_open = cur.fetchall()
    
    for pos in local_open:
        pos_id = pos["id"]
        ticker = pos["ticker"]
        rem_p = remote_pos_by_ticker.get(ticker)
        if rem_p:
            rem_contracts = rem_p.get("position", 0) or rem_p.get("contracts", 0)
            if rem_contracts == 0:
                with conn:
                    conn.execute("UPDATE positions SET status = 'RESOLVED', settled_at = ? WHERE id = ?", (now_str, pos_id))
                reconciled_positions += 1
                
    return {
        "reconciled_orders": reconciled_orders,
        "reconciled_positions": reconciled_positions,
        "orphans_pruned": orphans_pruned,
        "partial_fills": partial_fills,
        "cash_refunded": round(cash_refunded, 2)
    }

def settle_markets(conn):
    """Check Kalshi API for any finalized markets and settle paper positions."""
    cur = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'")
    open_positions = cur.fetchall()
    if not open_positions:
        return 0, 0.0
        
    settled_count = 0
    total_pnl = 0.0
    now_str = datetime.now(PT).isoformat()
    
    for pos in open_positions:
        ticker = pos["ticker"]
        try:
            url = f"{KALSHI_API}/markets/{ticker}"
            data = http_get_json(url)
            m = data.get("market", {})
            status = m.get("status")
            result = (m.get("result") or "").lower() # "yes" or "no"
            
            if status in ["finalized", "closed"] and result in ["yes", "no"]:
                won = (pos["side"].lower() == result)
                settled_price = 1.00 if won else 0.00
                payout = pos["contracts"] * settled_price
                realized_pnl = round(payout - pos["total_cost"], 2)
                
                with conn:
                    # Update position
                    conn.execute("""
                        UPDATE positions
                        SET status = 'RESOLVED', settled_at = ?, settled_price = ?, realized_pnl = ?
                        WHERE id = ?
                    """, (now_str, settled_price, realized_pnl, pos["id"]))
                    # Update portfolio cash and realized PnL
                    conn.execute("""
                        UPDATE portfolio
                        SET cash = cash + ?, realized_pnl = realized_pnl + ?, last_updated = ?
                        WHERE id = 1
                    """, (payout, realized_pnl, now_str))
                    
                settled_count += 1
                total_pnl += realized_pnl
        except Exception:
            pass
            
    return settled_count, total_pnl

def get_portfolio_status(conn):
    """Return dictionary of portfolio metrics."""
    p = conn.execute("SELECT * FROM portfolio WHERE id = 1").fetchone()
    open_pos = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()
    resolved_pos = conn.execute("SELECT * FROM positions WHERE status = 'RESOLVED'").fetchall()
    
    open_cost = sum(r["total_cost"] for r in open_pos)
    total_equity = p["cash"] + open_cost # approximate mark-to-cost
    
    wins = [r for r in resolved_pos if r["realized_pnl"] > 0]
    win_rate = (len(wins) / len(resolved_pos) * 100) if resolved_pos else 0.0
    
    return {
        "starting_balance": p["starting_balance"],
        "cash": p["cash"],
        "open_exposure": open_cost,
        "total_equity": total_equity,
        "realized_pnl": p["realized_pnl"],
        "open_count": len(open_pos),
        "resolved_count": len(resolved_pos),
        "win_rate": win_rate,
        "open_positions": open_pos,
        "resolved_positions": resolved_pos
    }

def print_status(conn):
    st = get_portfolio_status(conn)
    print("========================================")
    print("      ZERO KALSHI QUANT PAPER BOT       ")
    print("========================================")
    print(f"Cash:              ${st['cash']:>10.2f}")
    print(f"Open Exposure:     ${st['open_exposure']:>10.2f}")
    print(f"Total Equity:      ${st['total_equity']:>10.2f}")
    print(f"Realized P&L:      ${st['realized_pnl']:>+10.2f}")
    print(f"Open Positions:    {st['open_count']:>10}")
    print(f"Resolved Trades:   {st['resolved_count']:>10} (Win Rate: {st['win_rate']:.1f}%)")
    print("----------------------------------------")
    
    if st["open_positions"]:
        print("OPEN POSITIONS:")
        for p in st["open_positions"]:
            print(f" • {p['ticker']} ({p['side']}): {p['contracts']}x @ ${p['entry_price']:.2f} (${p['total_cost']:.2f})")
    else:
        print("No open positions active.")
    print("========================================")

def main():
    parser = argparse.ArgumentParser(description="Zero Kalshi Paper Trading Bot")
    parser.add_argument("command", choices=["scan", "trade", "status", "settle", "reset"], help="Action to execute")
    args = parser.parse_args()
    
    conn = get_db()
    
    if args.command == "reset":
        now_str = datetime.now(PT).isoformat()
        with conn:
            conn.execute("DELETE FROM positions")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM scans")
            conn.execute("UPDATE portfolio SET cash = ?, starting_balance = ?, realized_pnl = 0.0, last_updated = ? WHERE id = 1", (STARTING_BALANCE, STARTING_BALANCE, now_str))
        print(f"Portfolio reset to ${STARTING_BALANCE:.2f} virtual USD.")
        return

    if args.command == "settle":
        count, pnl = settle_markets(conn)
        print(f"Settled {count} markets. Realized P&L: ${pnl:+.2f}")
        return

    if args.command in ["scan", "trade"]:
        opps = scan_markets()
        print(f"\nFound {len(opps)} positive-EV opportunities (Threshold: +{MIN_EDGE_THRESHOLD*100:.0f}% edge):")
        
        if not opps:
            print("No mispriced contracts currently meet the edge threshold.")
        else:
            for i, opp in enumerate(opps, 1):
                print(f"[{i}] {opp['city']} (NOAA Fc: {opp['forecast']}°F) -> {opp['ticker']}")
                print(f"    Title: {opp['title']}")
                print(f"    Action: BUY {opp['side']} @ ${opp['market_price']:.2f} | Model Prob: {opp['model_prob']*100:.1f}% | Edge: {opp['edge']*100:+.1f}%")
                
                if args.command == "trade":
                    success, msg = execute_trade(conn, opp)
                    status_flag = "EXEC" if success else "SKIP"
                    print(f"    [{status_flag}]: {msg}")
                    
        print()
        print_status(conn)
        return

    if args.command == "status":
        print_status(conn)

if __name__ == "__main__":
    main()
