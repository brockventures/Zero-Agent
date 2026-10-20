#!/usr/bin/env python3
"""Track 4: Bi-Directional Multi-Day Trend Momentum & Volatility Squeeze Swing Engine.

Implements dual-momentum relative strength ranking, bear breakdown capture, and volatility
squeeze breakouts across liquid crypto (via Hyperliquid L1 candles) and major equities.

Strategy Rules:
1. Regime & Momentum Gate:
   - LONG Swings: Price > 20 SMA and 50 SMA (Bull Regime) with 14d ATR-scaled velocity > +0.50.
   - SHORT Breakdown Swings: Price < 20 SMA and 50 SMA (Bear Regime) with 14d velocity < -1.50.
2. Active Slot Rotation (Regime-Loss Exit):
   - Automatically exits LONG swings if price loses 20 SMA or velocity decays below +0.25.
   - Automatically exits SHORT swings if price reclaims 20 SMA or velocity rises above -0.50.
3. Trailing ATR Stops: 2.0x ATR trailing stop from peak favorable excursion.

Persists paper positions, trailing stops, and realized P&L to /workspace/data/quant_paper.db.
"""

import json
import math
import os
import sqlite3
import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("/workspace/data/quant_paper.db")

HYPERLIQUID_INFO_API = "https://api.hyperliquid.xyz/info"
STARTING_TREND_CAPITAL = 1000.00
MAX_ALLOCATION_PER_POSITION = 350.00  # Up to 35% of book on high-conviction, low-ATR breakouts
MIN_SLOT_ALLOCATION = 100.00
MAX_OPEN_POSITIONS = 5

CRYPTO_UNIVERSE = ["BTC", "ETH", "SOL", "AVAX", "SUI", "LINK", "DOGE"]
EQUITIES_UNIVERSE = ["QQQ", "SPY", "NVDA", "AAPL", "MSFT", "AMZN", "META"]

def init_db(conn):
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trend_portfolio (
                id INTEGER PRIMARY KEY,
                starting_balance REAL,
                cash REAL,
                realized_pnl REAL,
                total_equity REAL,
                last_updated TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trend_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                direction TEXT DEFAULT 'LONG',
                asset_class TEXT,
                strategy TEXT,
                size REAL,
                entry_price REAL,
                current_price REAL,
                trailing_stop REAL,
                peak_price REAL,
                atr REAL,
                opened_at TEXT,
                allocated_usd REAL,
                unrealized_pnl REAL,
                status TEXT,
                closed_at TEXT,
                exit_price REAL,
                realized_pnl REAL
            )
        """)
        # Migrate legacy UNIQUE constraint on symbol if present so closed symbols can re-enter cleanly
        tbl_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trend_positions'"
        ).fetchone()
        if tbl_sql_row and tbl_sql_row[0] and "UNIQUE" in tbl_sql_row[0].upper():
            conn.execute("""
                CREATE TABLE trend_positions_migrated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    direction TEXT DEFAULT 'LONG',
                    asset_class TEXT,
                    strategy TEXT,
                    size REAL,
                    entry_price REAL,
                    current_price REAL,
                    trailing_stop REAL,
                    peak_price REAL,
                    atr REAL,
                    opened_at TEXT,
                    allocated_usd REAL,
                    unrealized_pnl REAL,
                    status TEXT,
                    closed_at TEXT,
                    exit_price REAL,
                    realized_pnl REAL
                )
            """)
            conn.execute("""
                INSERT INTO trend_positions_migrated (
                    id, symbol, direction, asset_class, strategy, size, entry_price, current_price,
                    trailing_stop, peak_price, atr, opened_at, allocated_usd,
                    unrealized_pnl, status, closed_at, exit_price, realized_pnl
                )
                SELECT id, symbol, 'LONG', asset_class, strategy, size, entry_price, current_price,
                       trailing_stop, peak_price, atr, opened_at, allocated_usd,
                       unrealized_pnl, status, closed_at, exit_price, realized_pnl
                FROM trend_positions
            """)
            conn.execute("DROP TABLE trend_positions")
            conn.execute("ALTER TABLE trend_positions_migrated RENAME TO trend_positions")
        else:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(trend_positions)").fetchall()]
            if "direction" not in cols:
                conn.execute("ALTER TABLE trend_positions ADD COLUMN direction TEXT DEFAULT 'LONG'")

        cur = conn.execute("SELECT id FROM trend_portfolio WHERE id = 1")
        if not cur.fetchone():
            now_str = datetime.now(PT).isoformat()
            conn.execute(
                "INSERT INTO trend_portfolio (id, starting_balance, cash, realized_pnl, total_equity, last_updated) VALUES (1, ?, ?, 0.0, ?, ?)",
                (STARTING_TREND_CAPITAL, STARTING_TREND_CAPITAL, STARTING_TREND_CAPITAL, now_str)
            )

def fetch_hyperliquid_candles(coin, days=40):
    """Fetch daily OHLCV candles from Hyperliquid public API."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 86400 * 1000)
    try:
        req = urllib.request.Request(
            HYPERLIQUID_INFO_API,
            data=json.dumps({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": start_ms}}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "ZeroQuant/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            candles = []
            for c in data:
                candles.append({
                    "open": float(c["o"]),
                    "high": float(c["h"]),
                    "low": float(c["l"]),
                    "close": float(c["c"]),
                    "volume": float(c["v"]),
                    "timestamp": c["t"]
                })
            return candles
    except Exception:
        return []

def fetch_equity_candles(symbol, days=40):
    """Fetch daily candles from Yahoo Finance chart API with graceful fallback."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2mo"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            result = data["chart"]["result"][0]
            quotes = result["indicators"]["quote"][0]
            timestamps = result["timestamp"]
            
            candles = []
            for i, ts in enumerate(timestamps):
                c = quotes["close"][i]
                h = quotes["high"][i]
                l = quotes["low"][i]
                o = quotes["open"][i]
                v = quotes["volume"][i]
                if c is not None and h is not None and l is not None and o is not None:
                    candles.append({
                        "open": float(o), "high": float(h), "low": float(l),
                        "close": float(c), "volume": float(v or 0.0), "timestamp": ts * 1000
                    })
            return candles
    except Exception:
        return []

def _round_px(val: float) -> float:
    """Dynamic decimal precision so sub-dollar tokens (DOGE, SUI) never round ATR or stops to 0.0."""
    av = abs(val)
    if av > 0 and av < 0.5:
        return round(val, 6)
    if av < 10.0:
        return round(val, 4)
    return round(val, 2)

def calculate_technical_indicators(candles):
    """Compute ATR, 20 SMA, 50 SMA, Bollinger Bands, and Keltner Squeeze."""
    if len(candles) < 20:
        return None
        
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    
    recent_20 = closes[-20:]
    sma_20 = sum(recent_20) / 20.0
    var_20 = sum((p - sma_20) ** 2 for p in recent_20) / 20.0
    std_20 = math.sqrt(var_20)
    
    bb_upper = sma_20 + (2.0 * std_20)
    bb_lower = sma_20 - (2.0 * std_20)
    
    tr_list = []
    for i in range(1, len(candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
        
    raw_atr = sum(tr_list[-14:]) / 14.0 if len(tr_list) >= 14 else (highs[-1] - lows[-1])
    atr_14 = max(raw_atr, closes[-1] * 0.005)
    
    kc_upper = sma_20 + (1.5 * atr_14)
    kc_lower = sma_20 - (1.5 * atr_14)
    
    is_squeeze_on = (bb_lower >= kc_lower) and (bb_upper <= kc_upper)
    mom_14 = (closes[-1] - closes[-14]) / atr_14 if len(closes) >= 14 and atr_14 > 0 else 0.0
    
    sma_50 = sum(closes[-50:]) / len(closes[-50:]) if len(closes) >= 50 else sma_20
    is_bull_regime = closes[-1] > sma_20 and closes[-1] > sma_50
    is_bear_regime = closes[-1] < sma_20 and closes[-1] < sma_50
    
    return {
        "current_price": closes[-1],
        "sma_20": _round_px(sma_20),
        "sma_50": _round_px(sma_50),
        "atr": _round_px(atr_14),
        "momentum_velocity": round(mom_14, 3),
        "is_squeeze_on": is_squeeze_on,
        "is_bull_regime": is_bull_regime,
        "is_bear_regime": is_bear_regime,
        "bb_upper": _round_px(bb_upper),
        "kc_upper": _round_px(kc_upper)
    }

def update_open_positions(conn, market_data):
    """Update mark-to-market, enforce trailing stops, and rotate out of decayed regimes."""
    cur = conn.execute("SELECT * FROM trend_positions WHERE status = 'OPEN'")
    open_pos = cur.fetchall()
    now_str = datetime.now(PT).isoformat()
    
    for pos in open_pos:
        sym = pos["symbol"]
        direction = pos["direction"] if "direction" in pos.keys() and pos["direction"] else "LONG"
        tech = market_data.get(sym)
        if not tech:
            continue
            
        cur_px = tech["current_price"]
        atr = tech["atr"]
        mom = tech["momentum_velocity"]
        
        if direction == "LONG":
            peak_px = max(pos["peak_price"], cur_px)
            new_stop = _round_px(max(pos["trailing_stop"], peak_px - (2.0 * atr)))
            unrealized = round((cur_px - pos["entry_price"]) * pos["size"], 2)
            hit_stop = cur_px <= new_stop
            lost_regime = (not tech["is_bull_regime"]) or (mom < 0.25)
            exit_px = new_stop if hit_stop else cur_px
            realized = round((exit_px - pos["entry_price"]) * pos["size"], 2)
        else:
            # SHORT position: peak_price tracks lowest favorable price reached
            peak_px = min(pos["peak_price"], cur_px)
            new_stop = _round_px(min(pos["trailing_stop"], peak_px + (2.0 * atr)))
            unrealized = round((pos["entry_price"] - cur_px) * pos["size"], 2)
            hit_stop = cur_px >= new_stop
            lost_regime = (not tech["is_bear_regime"]) or (mom > -0.50)
            exit_px = new_stop if hit_stop else cur_px
            realized = round((pos["entry_price"] - exit_px) * pos["size"], 2)
            
        if hit_stop or lost_regime:
            reason = f"hit trailing stop ${new_stop:,.2f}" if hit_stop else f"regime/momentum decay (Mom: {mom:+.2f}, SMA20: ${tech['sma_20']:,.2f})"
            print(f"  🔄 EXIT {direction} SWING: {sym} {reason} | Entry: ${pos['entry_price']:,.2f} -> Exit: ${exit_px:,.2f} | P&L: ${realized:+.2f}")
            with conn:
                conn.execute("""
                    UPDATE trend_positions
                    SET status = 'CLOSED', closed_at = ?, exit_price = ?,
                        realized_pnl = ?, unrealized_pnl = 0.0
                    WHERE id = ?
                """, (now_str, exit_px, realized, pos["id"]))
                conn.execute("""
                    UPDATE trend_portfolio
                    SET cash = ROUND(cash + ? + ?, 2),
                        realized_pnl = ROUND(realized_pnl + ?, 2),
                        total_equity = ROUND(starting_balance + realized_pnl + ?, 2),
                        last_updated = ?
                    WHERE id = 1
                """, (pos["allocated_usd"], realized, realized, realized, now_str))
        else:
            with conn:
                conn.execute("""
                    UPDATE trend_positions
                    SET current_price = ?, peak_price = ?, trailing_stop = ?,
                        atr = ?, unrealized_pnl = ?
                    WHERE id = ?
                """, (cur_px, peak_px, new_stop, atr, unrealized, pos["id"]))

def run_trend_momentum_scan(conn=None, verbose: bool = False):
    now_pt = datetime.now(PT)
    
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
    init_db(conn)
    
    market_data = {}
    
    for coin in CRYPTO_UNIVERSE:
        candles = fetch_hyperliquid_candles(coin)
        tech = calculate_technical_indicators(candles)
        if tech:
            tech["asset_class"] = "crypto"
            market_data[coin] = tech
            
    for eq in EQUITIES_UNIVERSE:
        candles = fetch_equity_candles(eq)
        tech = calculate_technical_indicators(candles)
        if tech:
            tech["asset_class"] = "equity"
            market_data[eq] = tech
            
    update_open_positions(conn, market_data)
    
    # Screen for Bi-Directional Momentum Leaders (Bull Breakouts + Bear Breakdowns)
    candidates = []
    for sym, data in market_data.items():
        if data["is_bull_regime"] and data["momentum_velocity"] >= 0.50:
            candidates.append((sym, "LONG", data, abs(data["momentum_velocity"])))
        elif data["is_bear_regime"] and data["momentum_velocity"] <= -1.50:
            candidates.append((sym, "SHORT", data, abs(data["momentum_velocity"])))
            
    candidates.sort(key=lambda x: x[3], reverse=True)
    
    open_pos = conn.execute("SELECT * FROM trend_positions WHERE status = 'OPEN'").fetchall()
    open_symbols = {p["symbol"] for p in open_pos}
    now_str = datetime.now(PT).isoformat()

    # Opportunity-Cost Laggard Rotation: when all 5 slots are full (or cash < MIN_SLOT_ALLOCATION)
    # and a fresh breakout candidate beats an underwater/weak open slot by >= +0.35 velocity units, rotate out.
    fresh_candidates = [c for c in candidates if c[0] not in open_symbols]
    if open_pos and fresh_candidates and len(open_pos) >= MAX_OPEN_POSITIONS:
        best_sym, best_dir, _best_tech, best_score = fresh_candidates[0]
        # Rank open positions by unrealized P&L and current momentum strength (weakest first)
        def _pos_strength(pos_row):
            t_info = market_data.get(pos_row["symbol"], {})
            vel = abs(float(t_info.get("momentum_velocity", 0.0)))
            unr = float(pos_row["unrealized_pnl"] or 0.0)
            return (1 if unr >= 0 else 0, vel, unr)

        weakest_pos = sorted(open_pos, key=_pos_strength)[0]
        w_sym = weakest_pos["symbol"]
        w_vel = abs(float(market_data.get(w_sym, {}).get("momentum_velocity", 0.0)))
        w_unr = float(weakest_pos["unrealized_pnl"] or 0.0)
        if w_unr < 0.0 and best_score >= (w_vel + 0.35):
            exit_px = float(market_data.get(w_sym, {}).get("current_price") or weakest_pos["current_price"])
            if (weakest_pos["direction"] or "LONG") == "LONG":
                realized = round((exit_px - weakest_pos["entry_price"]) * weakest_pos["size"], 2)
            else:
                realized = round((weakest_pos["entry_price"] - exit_px) * weakest_pos["size"], 2)
            if verbose:
                print(
                    f"  🔄 ROTATE LAGGARD SWING: Closing {w_sym} (vel {w_vel:.2f}, unreal ${w_unr:+.2f}) "
                    f"for stronger leader {best_sym} ({best_dir} vel {best_score:.2f})"
                )
            with conn:
                conn.execute("""
                    UPDATE trend_positions
                    SET status = 'CLOSED', closed_at = ?, exit_price = ?,
                        realized_pnl = ?, unrealized_pnl = 0.0
                    WHERE id = ?
                """, (now_str, exit_px, realized, weakest_pos["id"]))
                conn.execute("""
                    UPDATE trend_portfolio
                    SET cash = ROUND(cash + ? + ?, 2),
                        realized_pnl = ROUND(realized_pnl + ?, 2),
                        total_equity = ROUND(starting_balance + realized_pnl + ?, 2),
                        last_updated = ?
                    WHERE id = 1
                """, (weakest_pos["allocated_usd"], realized, realized, realized, now_str))
            open_pos = conn.execute("SELECT * FROM trend_positions WHERE status = 'OPEN'").fetchall()
            open_symbols = {p["symbol"] for p in open_pos}

    available_slots = MAX_OPEN_POSITIONS - len(open_pos)
    
    p = conn.execute("SELECT * FROM trend_portfolio WHERE id = 1").fetchone()
    cash = float(p["cash"])

    cand_dicts = [
        {
            "symbol": sym,
            "direction": direction,
            "score": round(score, 3),
            "price": tech["current_price"],
            "momentum_velocity": tech["momentum_velocity"],
            "is_squeeze_on": tech["is_squeeze_on"],
            "asset_class": tech["asset_class"],
        }
        for sym, direction, tech, score in candidates
    ]

    pm_decision = {
        "approved_indices": list(range(len(candidates))),
        "sizing_multipliers": {},
        "rationale_by_index": {},
        "unwinds": [],
        "pm_thesis": "",
        "model_used": "gemini-4-argon-high",
    }
    unheld_candidates = [c for c in candidates if c[0] not in open_symbols]
    if available_slots > 0 and cash >= MIN_SLOT_ALLOCATION and unheld_candidates:
        try:
            from tools.llm_sidecar import evaluate_quant_candidates_llm
            pm_decision = evaluate_quant_candidates_llm(
                "quant_trend_momentum",
                "Track 4: Bi-Directional Multi-Day Trend Momentum & Volatility Squeeze",
                cand_dicts,
                {
                    "cash": cash,
                    "open_symbols": list(open_symbols),
                    "available_slots": available_slots,
                },
            )
            if False and pm_decision.get("pm_thesis"):
                print(f"  🧠 [Quant PM · {pm_decision.get('model_used')}]: {pm_decision['pm_thesis']}")
        except Exception as e:
            if verbose:
                print(f"  [Quant PM Note]: {e}")

    approved_set = set(pm_decision.get("approved_indices", range(len(candidates))))
    mult_map = pm_decision.get("sizing_multipliers", {})
    rat_map = pm_decision.get("rationale_by_index", {})

    for idx, (sym, direction, tech, _score) in enumerate(candidates):
        if available_slots <= 0 or cash < MIN_SLOT_ALLOCATION:
            break
        if sym in open_symbols:
            continue
        if idx not in approved_set:
            if verbose:
                print(f"  [PM VETO] Skipping {sym} ({direction}) swing per Quant PM regime review.")
            continue

        conv_mult = max(0.5, min(1.5, float(mult_map.get(str(idx), 1.0))))
        pm_note = rat_map.get(str(idx), "")
        cur_px = tech["current_price"]
        atr = tech["atr"]

        # Institutional ATR Volatility-Targeted Risk-Parity Sizing:
        # Target 1.25% base book equity at risk at the 2.0x ATR stop, scaled by momentum velocity & squeeze bonus
        open_alloc_sum = sum(float(pos["allocated_usd"]) for pos in conn.execute("SELECT allocated_usd FROM trend_positions WHERE status='OPEN'").fetchall())
        live_book_eq = max(800.0, cash + open_alloc_sum)
        mom_mult = min(1.45, max(0.75, abs(tech["momentum_velocity"]) / 1.4))
        squeeze_mult = 1.20 if tech["is_squeeze_on"] else 1.00
        risk_budget_usd = live_book_eq * 0.0125 * mom_mult * squeeze_mult * conv_mult
        stop_distance_pct = max(0.018, (2.0 * atr) / max(cur_px, 0.0001))
        vol_target_alloc = risk_budget_usd / stop_distance_pct

        max_pos_cap = min(MAX_ALLOCATION_PER_POSITION, round(live_book_eq * 0.35, 2))
        alloc = round(min(max_pos_cap, cash, max(MIN_SLOT_ALLOCATION, vol_target_alloc)), 2)
        if alloc < MIN_SLOT_ALLOCATION:
            break

        if direction == "LONG":
            trailing_stop = _round_px(cur_px - (2.0 * atr))
            strat = "volatility_squeeze" if tech["is_squeeze_on"] else "dual_momentum"
        else:
            trailing_stop = _round_px(cur_px + (2.0 * atr))
            strat = "bear_squeeze_breakdown" if tech["is_squeeze_on"] else "bear_momentum"

        size = round(alloc / cur_px, 6)

        if verbose:
            print(f"  🚀 ENTER {direction} SWING: {sym} ({tech['asset_class'].upper()}) | Strategy: {strat} | Price: ${cur_px:,.2f} | 14d Mom: {tech['momentum_velocity']:+.2f} | Stop: ${trailing_stop:,.2f} | Alloc: ${alloc:.2f}")
        if False and pm_note:
            print(f"     [PM Thesis]: {pm_note}")
        with conn:
            conn.execute("""
                INSERT INTO trend_positions (
                    symbol, direction, asset_class, strategy, size, entry_price, current_price,
                    trailing_stop, peak_price, atr, opened_at, allocated_usd,
                    unrealized_pnl, status, closed_at, exit_price, realized_pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 'OPEN', NULL, NULL, NULL)
            """, (
                sym, direction, tech["asset_class"], strat, size, cur_px, cur_px,
                trailing_stop, cur_px, atr, now_str, alloc
            ))
            conn.execute("""
                UPDATE trend_portfolio
                SET cash = ROUND(cash - ?, 2), last_updated = ?
                WHERE id = 1
            """, (alloc, now_str))

        cash = round(cash - alloc, 2)
        available_slots -= 1
        open_symbols.add(sym)
        
    p = conn.execute("SELECT * FROM trend_portfolio WHERE id = 1").fetchone()
    open_pos = conn.execute("SELECT * FROM trend_positions WHERE status = 'OPEN'").fetchall()
    total_allocated = sum(pos["allocated_usd"] for pos in open_pos)
    total_unrealized = sum(pos["unrealized_pnl"] for pos in open_pos)
    base_equity = round(p["cash"] + total_allocated, 2)
    total_equity = round(base_equity + total_unrealized, 2)
    with conn:
        conn.execute("UPDATE trend_portfolio SET total_equity = ? WHERE id = 1", (base_equity,))
    
    swings_str = ", ".join(f"`{pos['symbol']}` (`{pos['unrealized_pnl']:+.2f}`)" for pos in open_pos[:5])
    sub_str = f" — {swings_str}" if swings_str else ""
    print(f"- **Track 4 (Trend Momentum):** `${total_equity:,.2f}` equity (`{total_unrealized:+,.2f}` unreal. · `{len(open_pos)}` swings){sub_str}")

if __name__ == "__main__":
    import sys
    run_trend_momentum_scan(verbose=("--verbose" in sys.argv or "-v" in sys.argv))
