#!/usr/bin/env python3
"""Track 4: Bi-Directional Multi-Day Trend Momentum & Volatility Squeeze Swing Engine.

Implements dual-SMA macro regime filtering (20/50 SMA on 90+ day history), Donchian/Keltner
volatility squeeze breakouts, trend pullback entries, ATR hysteresis exit buffers, 24-hour
anti-churn cooldowns, and cross-asset beta alignment across crypto and multi-sector ETFs/equities.

Strategy Rules:
1. True 20/50 SMA Macro Regime & Anti-Exhaustion Entry Gate:
   - Pulls 90-day crypto candles and 6-month (~126 trading day) equity candles so 50 SMA is live.
   - LONG Swings: Price > 20 SMA, 20 SMA >= 50 SMA (Bull Regime), not overextended (< 2.4x ATR above 20 SMA),
     triggered by Volatility Squeeze / 20d Donchian breakout, 20 SMA trend pullback bounce, or confirmed velocity.
   - SHORT Breakdown Swings: Price < 20 SMA, 20 SMA <= 50 SMA (Bear Regime), not overextended (< 2.4x ATR below 20 SMA).
2. ATR Hysteresis Exit Buffer & Trailing Stops:
   - 2.0x ATR chandelier trailing stop from peak favorable excursion.
   - Regime-loss exit requires either a 0.50x ATR breach across the 20 SMA or a price cross + velocity reversal,
     preventing intraday noise from prematurely shaking out multi-day swings.
3. Anti-Churn & Directional Beta Discipline:
   - Enforces a 24-hour symbol cooldown after any exit.
   - Prevents self-hedging within correlated US Tech/Index beta (never simultaneously LONG mega-cap tech and SHORT QQQ/SPY).
   - Evaluates LLM PM approval BEFORE executing laggard rotations.

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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("/workspace/data/quant_paper.db")

HYPERLIQUID_INFO_API = "https://api.hyperliquid.xyz/info"
STARTING_TREND_CAPITAL = 1000.00
MAX_ALLOCATION_PER_POSITION = 350.00  # Up to 35% of book on high-conviction, low-ATR breakouts
MIN_SLOT_ALLOCATION = 100.00
MAX_OPEN_POSITIONS = 5
COOLDOWN_HOURS = 24.0

CRYPTO_UNIVERSE = ["BTC", "ETH", "SOL", "HYPE", "XRP", "SUI", "AVAX", "LINK", "DOGE"]
EQUITIES_UNIVERSE = ["QQQ", "SPY", "NVDA", "AAPL", "MSFT", "AMZN", "META", "GLD", "TLT", "XLE", "IBIT"]

TECH_BETA_GROUP = {"QQQ", "SPY", "NVDA", "AAPL", "MSFT", "AMZN", "META"}
INDEX_ETFS = {"QQQ", "SPY"}


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


def fetch_hyperliquid_candles(coin, days=90):
    """Fetch 90 days of daily OHLCV candles from Hyperliquid public API so 50 SMA is genuinely populated."""
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


def fetch_equity_candles(symbol, days=90):
    """Fetch 6 months (~126 trading days) of daily candles from Yahoo Finance chart API."""
    try:
        yf_range = "6mo" if days >= 50 else "2mo"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={yf_range}"
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
    """Compute ATR, 20 SMA, 50 SMA, Donchian 20-bar channels, volume expansion, and Keltner Squeeze."""
    if len(candles) < 20:
        return None

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [float(c.get("volume") or 0.0) for c in candles]

    recent_20 = closes[-20:]
    sma_20 = sum(recent_20) / 20.0
    prev_20 = closes[-21:-1] if len(closes) >= 21 else recent_20
    sma_20_prev = sum(prev_20) / len(prev_20)
    sma_20_slope = sma_20 - sma_20_prev

    var_20 = sum((p - sma_20) ** 2 for p in recent_20) / 20.0
    std_20 = math.sqrt(var_20)

    bb_upper = sma_20 + (2.0 * std_20)
    bb_lower = sma_20 - (2.0 * std_20)

    tr_list = []
    for i in range(1, len(candles)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    raw_atr = sum(tr_list[-14:]) / 14.0 if len(tr_list) >= 14 else (highs[-1] - lows[-1])
    atr_14 = max(raw_atr, closes[-1] * 0.005)

    kc_upper = sma_20 + (1.5 * atr_14)
    kc_lower = sma_20 - (1.5 * atr_14)

    is_squeeze_on = (bb_lower >= kc_lower) and (bb_upper <= kc_upper)
    mom_14 = (closes[-1] - closes[-14]) / atr_14 if len(closes) >= 14 and atr_14 > 0 else 0.0
    mom_5 = (closes[-1] - closes[-5]) / atr_14 if len(closes) >= 5 and atr_14 > 0 else mom_14

    if len(closes) >= 50:
        sma_50 = sum(closes[-50:]) / 50.0
        is_bull_regime = (closes[-1] > sma_20) and (sma_20 >= sma_50 * 0.995)
        is_bear_regime = (closes[-1] < sma_20) and (sma_20 <= sma_50 * 1.005)
    else:
        sma_50 = sma_20
        is_bull_regime = closes[-1] > sma_20 and closes[-1] > sma_50
        is_bear_regime = closes[-1] < sma_20 and closes[-1] < sma_50

    prior_highs = highs[-21:-1] if len(highs) >= 21 else highs[:-1]
    prior_lows = lows[-21:-1] if len(lows) >= 21 else lows[:-1]
    donchian_high_20 = max(prior_highs) if prior_highs else highs[-1]
    donchian_low_20 = min(prior_lows) if prior_lows else lows[-1]

    avg_vol_20 = sum(volumes[-21:-1]) / 20.0 if len(volumes) >= 21 else (sum(volumes) / max(1, len(volumes)))
    vol_ratio = (volumes[-1] / avg_vol_20) if avg_vol_20 > 0 else 1.0

    dist_from_sma20_atr = (closes[-1] - sma_20) / atr_14 if atr_14 > 0 else 0.0
    is_donchian_bull_breakout = closes[-1] >= (donchian_high_20 * 0.997)
    is_donchian_bear_breakdown = closes[-1] <= (donchian_low_20 * 1.003)
    is_pullback_bounce = (
        is_bull_regime
        and (0.05 <= dist_from_sma20_atr <= 1.25)
        and (mom_5 >= 0.10 or closes[-1] >= closes[-2])
        and (sma_20_slope >= -0.05 * atr_14)
    )

    return {
        "current_price": closes[-1],
        "sma_20": _round_px(sma_20),
        "sma_50": _round_px(sma_50),
        "sma_20_slope": round(sma_20_slope, 4),
        "atr": _round_px(atr_14),
        "momentum_velocity": round(mom_14, 3),
        "mom_5": round(mom_5, 3),
        "dist_from_sma20_atr": round(dist_from_sma20_atr, 3),
        "vol_ratio": round(vol_ratio, 2),
        "donchian_high_20": _round_px(donchian_high_20),
        "donchian_low_20": _round_px(donchian_low_20),
        "is_donchian_bull_breakout": is_donchian_bull_breakout,
        "is_donchian_bear_breakdown": is_donchian_bear_breakdown,
        "is_pullback_bounce": is_pullback_bounce,
        "is_squeeze_on": is_squeeze_on,
        "is_bull_regime": is_bull_regime,
        "is_bear_regime": is_bear_regime,
        "bb_upper": _round_px(bb_upper),
        "kc_upper": _round_px(kc_upper)
    }


def _close_position(conn, pos, exit_px: float, realized: float, now_str: str, reason: str, verbose: bool = True):
    direction = pos["direction"] if "direction" in pos.keys() and pos["direction"] else "LONG"
    sym = pos["symbol"]
    if verbose:
        print(
            f"  🔄 EXIT {direction} SWING: {sym} {reason} | "
            f"Entry: ${pos['entry_price']:,.2f} -> Exit: ${exit_px:,.2f} | P&L: ${realized:+.2f}"
        )
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


def update_open_positions(conn, market_data):
    """Update mark-to-market, enforce 2.0x ATR trailing stops, and apply 0.5x ATR hysteresis on regime exits."""
    cur = conn.execute("SELECT * FROM trend_positions WHERE status = 'OPEN'")
    open_pos = cur.fetchall()
    now_str = datetime.now(PT).isoformat()

    # Determine dominant US Tech benchmark regime (QQQ/SPY) to resolve any legacy self-hedged conflicts
    qqq_tech = market_data.get("QQQ") or market_data.get("SPY")
    tech_benchmark_dir = None
    if qqq_tech:
        if qqq_tech.get("is_bull_regime"):
            tech_benchmark_dir = "LONG"
        elif qqq_tech.get("is_bear_regime"):
            tech_benchmark_dir = "SHORT"

    open_tech_dirs = {
        (p["direction"] if "direction" in p.keys() and p["direction"] else "LONG")
        for p in open_pos
        if p["symbol"] in TECH_BETA_GROUP
    }
    has_tech_self_hedge = len(open_tech_dirs) > 1

    for pos in open_pos:
        sym = pos["symbol"]
        direction = pos["direction"] if "direction" in pos.keys() and pos["direction"] else "LONG"
        tech = market_data.get(sym)
        if not tech:
            continue

        cur_px = tech["current_price"]
        atr = tech["atr"]
        mom = tech["momentum_velocity"]
        sma_20 = tech["sma_20"]
        sma_50 = tech.get("sma_50", sma_20)

        if direction == "LONG":
            peak_px = max(pos["peak_price"], cur_px)
            new_stop = _round_px(max(pos["trailing_stop"], peak_px - (2.0 * atr)))
            unrealized = round((cur_px - pos["entry_price"]) * pos["size"], 2)
            hit_stop = cur_px <= new_stop
            # ATR Hysteresis: require 0.50x ATR breakdown below 20 SMA, or < 20 SMA with negative velocity, or 20/50 bear cross
            breached_sma20_buffer = cur_px <= (sma_20 - (0.50 * atr))
            below_sma20_and_fading = (cur_px < sma_20) and (mom <= -0.15)
            macro_bear_flip = (not tech["is_bull_regime"]) and (sma_20 < sma_50 * 0.995 or mom <= -0.25)
            lost_regime = breached_sma20_buffer or below_sma20_and_fading or macro_bear_flip
            exit_px = new_stop if hit_stop else cur_px
            realized = round((exit_px - pos["entry_price"]) * pos["size"], 2)
        else:
            peak_px = min(pos["peak_price"], cur_px)
            new_stop = _round_px(min(pos["trailing_stop"], peak_px + (2.0 * atr)))
            unrealized = round((pos["entry_price"] - cur_px) * pos["size"], 2)
            hit_stop = cur_px >= new_stop
            breached_sma20_buffer = cur_px >= (sma_20 + (0.50 * atr))
            above_sma20_and_rallying = (cur_px > sma_20) and (mom >= -0.15)
            macro_bull_flip = (not tech["is_bear_regime"]) and (sma_20 > sma_50 * 1.005 or mom >= -0.25)
            lost_regime = breached_sma20_buffer or above_sma20_and_rallying or macro_bull_flip
            exit_px = new_stop if hit_stop else cur_px
            realized = round((pos["entry_price"] - exit_px) * pos["size"], 2)

        # Resolve contradictory Tech Beta self-hedging (e.g. simultaneously LONG AAPL/MSFT/AMZN and SHORT QQQ/NVDA)
        beta_conflict_exit = False
        if has_tech_self_hedge and sym in TECH_BETA_GROUP:
            if tech_benchmark_dir and direction != tech_benchmark_dir:
                beta_conflict_exit = True
            elif not tech_benchmark_dir and direction == "SHORT" and sym in INDEX_ETFS:
                beta_conflict_exit = True

        if hit_stop or lost_regime or beta_conflict_exit:
            if hit_stop:
                reason = f"hit trailing stop ${new_stop:,.2f}"
            elif beta_conflict_exit:
                reason = f"beta self-hedge unwind vs Tech benchmark ({tech_benchmark_dir or 'NEUTRAL'}, SMA20: ${sma_20:,.2f}, SMA50: ${sma_50:,.2f})"
            else:
                reason = f"regime/hysteresis breakdown (Mom: {mom:+.2f}, Price: ${cur_px:,.2f} vs SMA20: ${sma_20:,.2f} / SMA50: ${sma_50:,.2f})"
            _close_position(conn, pos, exit_px, realized, now_str, reason, verbose=True)
        else:
            with conn:
                conn.execute("""
                    UPDATE trend_positions
                    SET current_price = ?, peak_price = ?, trailing_stop = ?,
                        atr = ?, unrealized_pnl = ?
                    WHERE id = ?
                """, (cur_px, peak_px, new_stop, atr, unrealized, pos["id"]))


def get_recently_closed_symbols(conn, cooldown_hours: float = COOLDOWN_HOURS) -> set:
    """Return symbols closed within the last `cooldown_hours` to prevent same-day wash-churn."""
    now_pt = datetime.now(PT)
    cutoff = now_pt - timedelta(hours=cooldown_hours)
    rows = conn.execute(
        "SELECT symbol, closed_at FROM trend_positions WHERE status = 'CLOSED' AND closed_at IS NOT NULL"
    ).fetchall()
    cooldown_syms = set()
    for r in rows:
        try:
            closed_dt = datetime.fromisoformat(r["closed_at"])
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=PT)
            if closed_dt >= cutoff:
                cooldown_syms.add(r["symbol"])
        except Exception:
            continue
    return cooldown_syms


def evaluate_candidate_signal(sym: str, data: dict):
    """Score a candidate using breakout/pullback structure and anti-exhaustion guards.

    Returns (direction, strategy_name, composite_score) or None if disqualified.
    """
    mom_14 = float(data.get("momentum_velocity", 0.0))
    mom_5 = float(data.get("mom_5", mom_14))
    dist_atr = float(data.get("dist_from_sma20_atr", 0.0))
    vol_ratio = float(data.get("vol_ratio", 1.0))
    is_squeeze = bool(data.get("is_squeeze_on", False))
    is_donchian_bull = bool(data.get("is_donchian_bull_breakout", False))
    is_donchian_bear = bool(data.get("is_donchian_bear_breakdown", False))
    is_pullback = bool(data.get("is_pullback_bounce", False))

    # Reject overextended blow-off spikes (> 2.4x ATR stretched from 20 SMA unless inside a tight squeeze)
    if abs(dist_atr) > 2.40 and not is_squeeze:
        return None

    if data.get("is_bull_regime") and (mom_14 >= 0.45 or (is_pullback and mom_14 >= 0.30)):
        if mom_5 < -0.35:
            return None  # Reject falling knives mid-pullback
        if is_squeeze and (is_donchian_bull or mom_5 >= 0.25):
            strat = "volatility_squeeze"
            bonus = 0.55
        elif is_donchian_bull and vol_ratio >= 1.10:
            strat = "donchian_breakout"
            bonus = 0.45
        elif is_pullback:
            strat = "trend_pullback"
            bonus = 0.35
        elif mom_14 >= 0.55 and dist_atr <= 1.85:
            strat = "dual_momentum"
            bonus = 0.15
        else:
            return None

        vol_bonus = 0.20 if vol_ratio >= 1.20 else 0.0
        extension_penalty = max(0.0, (dist_atr - 1.50) * 0.35)
        score = round(min(2.20, abs(mom_14)) + bonus + vol_bonus - extension_penalty, 3)
        return ("LONG", strat, score)

    if data.get("is_bear_regime") and mom_14 <= -1.25:
        if mom_5 > 0.35:
            return None  # Reject short-squeeze bounces
        if is_squeeze and (is_donchian_bear or mom_5 <= -0.25):
            strat = "bear_squeeze_breakdown"
            bonus = 0.55
        elif is_donchian_bear and vol_ratio >= 1.10:
            strat = "bear_donchian_breakdown"
            bonus = 0.45
        elif mom_14 <= -1.40 and abs(dist_atr) <= 1.95:
            strat = "bear_momentum"
            bonus = 0.15
        else:
            return None

        vol_bonus = 0.20 if vol_ratio >= 1.20 else 0.0
        extension_penalty = max(0.0, (abs(dist_atr) - 1.50) * 0.35)
        score = round(min(2.20, abs(mom_14)) + bonus + vol_bonus - extension_penalty, 3)
        return ("SHORT", strat, score)

    return None


def passes_beta_alignment(sym: str, direction: str, open_pos, market_data: dict) -> bool:
    """Prevent self-hedging within US Tech beta (never mix LONG and SHORT across QQQ/SPY/Mega-Cap Tech)."""
    if sym in TECH_BETA_GROUP:
        # Check benchmark gate: do not short single-name tech if QQQ & SPY are both in Bull Regime
        qqq = market_data.get("QQQ")
        spy = market_data.get("SPY")
        if direction == "SHORT" and qqq and spy and qqq.get("is_bull_regime") and spy.get("is_bull_regime"):
            return False
        if direction == "LONG" and qqq and spy and qqq.get("is_bear_regime") and spy.get("is_bear_regime"):
            return False

        # Never oppose existing open positions inside TECH_BETA_GROUP
        for p in open_pos:
            if p["symbol"] in TECH_BETA_GROUP:
                p_dir = p["direction"] if "direction" in p.keys() and p["direction"] else "LONG"
                if p_dir != direction:
                    return False

    # Crypto benchmark gate: do not take standard altcoin longs if BTC is in a confirmed Bear Regime breakdown
    if data_sym := market_data.get(sym):
        if data_sym.get("asset_class") == "crypto" and sym != "BTC":
            btc = market_data.get("BTC")
            if btc and direction == "LONG" and btc.get("is_bear_regime") and float(btc.get("momentum_velocity", 0.0)) <= -1.0:
                return False
            if btc and direction == "SHORT" and btc.get("is_bull_regime") and float(btc.get("momentum_velocity", 0.0)) >= 1.0:
                return False

    return True


def run_trend_momentum_scan(conn=None, verbose: bool = False):
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
    init_db(conn)

    market_data = {}

    for coin in CRYPTO_UNIVERSE:
        candles = fetch_hyperliquid_candles(coin, days=90)
        tech = calculate_technical_indicators(candles)
        if tech:
            tech["asset_class"] = "crypto"
            market_data[coin] = tech

    for eq in EQUITIES_UNIVERSE:
        candles = fetch_equity_candles(eq, days=90)
        tech = calculate_technical_indicators(candles)
        if tech:
            tech["asset_class"] = "equity"
            market_data[eq] = tech

    update_open_positions(conn, market_data)

    open_pos = conn.execute("SELECT * FROM trend_positions WHERE status = 'OPEN'").fetchall()
    open_symbols = {p["symbol"] for p in open_pos}
    cooldown_symbols = get_recently_closed_symbols(conn, cooldown_hours=COOLDOWN_HOURS)
    now_str = datetime.now(PT).isoformat()

    # Screen for Bi-Directional Momentum & Breakout Leaders
    candidates = []
    for sym, data in market_data.items():
        if sym in cooldown_symbols and sym not in open_symbols:
            continue
        sig = evaluate_candidate_signal(sym, data)
        if not sig:
            continue
        direction, strat_name, score = sig
        if not passes_beta_alignment(sym, direction, open_pos, market_data):
            continue
        data["selected_strategy"] = strat_name
        candidates.append((sym, direction, data, score))

    candidates.sort(key=lambda x: x[3], reverse=True)

    # Filter strictly to unheld, non-cooldown candidates for new entry & rotation evaluation
    unheld_candidates = [c for c in candidates if c[0] not in open_symbols and c[0] not in cooldown_symbols]

    p = conn.execute("SELECT * FROM trend_portfolio WHERE id = 1").fetchone()
    cash = float(p["cash"])
    available_slots = MAX_OPEN_POSITIONS - len(open_pos)

    # Pre-evaluate Quant PM gate whenever we have unheld candidates (either for open slots OR potential laggard rotation)
    pm_decision = {
        "approved_indices": list(range(len(unheld_candidates))),
        "sizing_multipliers": {},
        "rationale_by_index": {},
        "unwinds": [],
        "pm_thesis": "",
        "model_used": "gemini-4-argon-high",
    }
    if unheld_candidates:
        cand_dicts = [
            {
                "symbol": sym,
                "direction": direction,
                "strategy": tech.get("selected_strategy", "dual_momentum"),
                "score": round(score, 3),
                "price": tech["current_price"],
                "sma_20": tech["sma_20"],
                "sma_50": tech["sma_50"],
                "momentum_velocity": tech["momentum_velocity"],
                "dist_from_sma20_atr": tech.get("dist_from_sma20_atr", 0.0),
                "vol_ratio": tech.get("vol_ratio", 1.0),
                "is_squeeze_on": tech["is_squeeze_on"],
                "asset_class": tech["asset_class"],
            }
            for sym, direction, tech, score in unheld_candidates
        ]
        try:
            from tools.llm_sidecar import evaluate_quant_candidates_llm
            pm_decision = evaluate_quant_candidates_llm(
                "quant_trend_momentum",
                "Track 4: Bi-Directional Multi-Day Trend Momentum & Volatility Squeeze",
                cand_dicts,
                {
                    "cash": cash,
                    "open_symbols": list(open_symbols),
                    "cooldown_symbols": list(cooldown_symbols),
                    "available_slots": max(1, available_slots),
                },
            )
        except Exception as e:
            if verbose:
                print(f"  [Quant PM Note]: {e}")

    approved_set = set(pm_decision.get("approved_indices", range(len(unheld_candidates))))
    mult_map = pm_decision.get("sizing_multipliers", {})
    rat_map = pm_decision.get("rationale_by_index", {})

    approved_unheld = [
        (idx, cand) for idx, cand in enumerate(unheld_candidates) if idx in approved_set
    ]

    # Pre-Validated Opportunity-Cost Laggard Rotation:
    # Only rotate out a laggard if all 5 slots are full AND we have an already-PM-approved replacement candidate
    # that beats the weakest underwater position by >= +0.50 composite score.
    if open_pos and approved_unheld and len(open_pos) >= MAX_OPEN_POSITIONS:
        _best_idx, (best_sym, best_dir, _best_tech, best_score) = approved_unheld[0]

        def _pos_strength(pos_row):
            t_info = market_data.get(pos_row["symbol"], {})
            vel = abs(float(t_info.get("momentum_velocity", 0.0)))
            unr = float(pos_row["unrealized_pnl"] or 0.0)
            return (1 if unr >= 0 else 0, vel, unr)

        weakest_pos = sorted(open_pos, key=_pos_strength)[0]
        w_sym = weakest_pos["symbol"]
        w_vel = abs(float(market_data.get(w_sym, {}).get("momentum_velocity", 0.0)))
        w_unr = float(weakest_pos["unrealized_pnl"] or 0.0)
        if w_unr < 0.0 and best_score >= (w_vel + 0.50):
            exit_px = float(market_data.get(w_sym, {}).get("current_price") or weakest_pos["current_price"])
            if (weakest_pos["direction"] or "LONG") == "LONG":
                realized = round((exit_px - weakest_pos["entry_price"]) * weakest_pos["size"], 2)
            else:
                realized = round((weakest_pos["entry_price"] - exit_px) * weakest_pos["size"], 2)
            reason = f"laggard rotation (vel {w_vel:.2f}, unreal ${w_unr:+.2f}) for PM-approved {best_sym} ({best_dir} score {best_score:.2f})"
            _close_position(conn, weakest_pos, exit_px, realized, now_str, reason, verbose=verbose)
            cooldown_symbols.add(w_sym)
            open_pos = conn.execute("SELECT * FROM trend_positions WHERE status = 'OPEN'").fetchall()
            open_symbols = {p["symbol"] for p in open_pos}
            available_slots = MAX_OPEN_POSITIONS - len(open_pos)
            p = conn.execute("SELECT * FROM trend_portfolio WHERE id = 1").fetchone()
            cash = float(p["cash"])

    for idx, (sym, direction, tech, _score) in approved_unheld:
        if available_slots <= 0 or cash < MIN_SLOT_ALLOCATION:
            break
        if sym in open_symbols or sym in cooldown_symbols:
            continue
        if not passes_beta_alignment(sym, direction, open_pos, market_data):
            continue

        conv_mult = max(0.5, min(1.5, float(mult_map.get(str(idx), 1.0))))
        pm_note = rat_map.get(str(idx), "")
        cur_px = tech["current_price"]
        atr = tech["atr"]

        open_alloc_sum = sum(
            float(pos["allocated_usd"])
            for pos in conn.execute("SELECT allocated_usd FROM trend_positions WHERE status='OPEN'").fetchall()
        )
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

        strat = tech.get("selected_strategy") or (
            ("volatility_squeeze" if tech["is_squeeze_on"] else "dual_momentum")
            if direction == "LONG"
            else ("bear_squeeze_breakdown" if tech["is_squeeze_on"] else "bear_momentum")
        )
        if direction == "LONG":
            trailing_stop = _round_px(cur_px - (2.0 * atr))
        else:
            trailing_stop = _round_px(cur_px + (2.0 * atr))

        size = round(alloc / cur_px, 6)

        if verbose:
            print(
                f"  🚀 ENTER {direction} SWING: {sym} ({tech['asset_class'].upper()}) | "
                f"Strategy: {strat} | Price: ${cur_px:,.2f} | SMA20/50: ${tech['sma_20']:,.2f}/${tech['sma_50']:,.2f} | "
                f"14d Mom: {tech['momentum_velocity']:+.2f} | Stop: ${trailing_stop:,.2f} | Alloc: ${alloc:.2f}"
            )
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
        open_pos = conn.execute("SELECT * FROM trend_positions WHERE status = 'OPEN'").fetchall()

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
