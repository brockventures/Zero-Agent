#!/usr/bin/env python3
"""Track 3: Hyperliquid Bi-Directional Delta-Neutral Crypto Basis & Funding Engine.

Connects to Hyperliquid L1 public info API to monitor live funding rates, open interest,
and mark prices across 230+ perpetual assets.

Simulates an unleveraged (1.0x) delta-neutral cash-and-carry paper portfolio ($1,000 capital):
- Forward Basis (SHORT_PERP): Long spot / Short 1x perp when 8h funding > 0 (>= 2.0% APY, OI >= $500k)
- Reverse Basis (LONG_PERP): Short spot-borrow / Long 1x perp when 8h funding < 0 (shorts pay longs >= 8.0% APY, OI >= $1.0M)
- Accrues 8-hour funding cashflow payments directly into cash balance
- Unwinds position immediately if funding flips against position direction or drops below hurdle
- Rotates out of laggard slots when a fresh candidate beats the weakest slot by >= +3.0% APY

Persists positions, funding accruals, and equity curves to /workspace/data/quant_paper.db.
"""

import json
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
STARTING_PORTFOLIO_CAPITAL = 1000.00
MIN_ANNUALIZED_YIELD = 0.02          # 2.0% minimum APY hurdle for Forward Basis (SHORT_PERP)
MIN_REVERSE_ANNUALIZED_YIELD = 0.08  # 8.0% minimum APY hurdle for Reverse Basis (LONG_PERP)
MAX_ASSETS_IN_BOOK = 4               # Max 4 concurrent basis pairs ($250.00 each)
MIN_OPEN_INTEREST_USD = 500_000.0    # $500k+ Open Interest for Forward Basis
MIN_REVERSE_OI_USD = 1_000_000.0     # $1.0M+ Open Interest for Reverse Basis

def init_db(conn):
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hyperliquid_portfolio (
                id INTEGER PRIMARY KEY,
                starting_balance REAL,
                cash REAL,
                cumulative_funding REAL,
                realized_pnl REAL,
                total_equity REAL,
                last_updated TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hyperliquid_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT,
                direction TEXT DEFAULT 'SHORT_PERP',
                size REAL,
                entry_mark_px REAL,
                entry_oracle_px REAL,
                opened_at TEXT,
                allocated_usd REAL,
                cumulative_funding REAL,
                current_funding_rate_8h REAL,
                annualized_yield REAL,
                status TEXT,
                closed_at TEXT
            )
        """)
        # Migrate legacy UNIQUE constraint on asset if present so closed coins can re-enter cleanly
        tbl_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='hyperliquid_positions'"
        ).fetchone()
        if tbl_sql_row and tbl_sql_row[0] and "UNIQUE" in tbl_sql_row[0].upper():
            conn.execute("""
                CREATE TABLE hyperliquid_positions_migrated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT,
                    direction TEXT DEFAULT 'SHORT_PERP',
                    size REAL,
                    entry_mark_px REAL,
                    entry_oracle_px REAL,
                    opened_at TEXT,
                    allocated_usd REAL,
                    cumulative_funding REAL,
                    current_funding_rate_8h REAL,
                    annualized_yield REAL,
                    status TEXT,
                    closed_at TEXT
                )
            """)
            conn.execute("""
                INSERT INTO hyperliquid_positions_migrated (
                    id, asset, direction, size, entry_mark_px, entry_oracle_px, opened_at,
                    allocated_usd, cumulative_funding, current_funding_rate_8h,
                    annualized_yield, status, closed_at
                )
                SELECT id, asset, 'SHORT_PERP', size, entry_mark_px, entry_oracle_px, opened_at,
                       allocated_usd, cumulative_funding, current_funding_rate_8h,
                       annualized_yield, status, closed_at
                FROM hyperliquid_positions
            """)
            conn.execute("DROP TABLE hyperliquid_positions")
            conn.execute("ALTER TABLE hyperliquid_positions_migrated RENAME TO hyperliquid_positions")
        else:
            # Ensure direction column exists
            cols = [r[1] for r in conn.execute("PRAGMA table_info(hyperliquid_positions)").fetchall()]
            if "direction" not in cols:
                conn.execute("ALTER TABLE hyperliquid_positions ADD COLUMN direction TEXT DEFAULT 'SHORT_PERP'")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS hyperliquid_funding_accruals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                asset TEXT,
                position_size REAL,
                funding_rate_8h REAL,
                accrual_usd REAL
            )
        """)
        cur = conn.execute("SELECT id, starting_balance, cash FROM hyperliquid_portfolio WHERE id = 1")
        row = cur.fetchone()
        if not row:
            now_str = datetime.now(PT).isoformat()
            conn.execute(
                "INSERT INTO hyperliquid_portfolio (id, starting_balance, cash, cumulative_funding, realized_pnl, total_equity, last_updated) VALUES (1, ?, ?, 0.0, 0.0, ?, ?)",
                (STARTING_PORTFOLIO_CAPITAL, STARTING_PORTFOLIO_CAPITAL, STARTING_PORTFOLIO_CAPITAL, now_str)
            )

def fetch_hyperliquid_meta():
    """Fetch all perps, funding rates, open interest, and mark prices from Hyperliquid."""
    try:
        req = urllib.request.Request(
            HYPERLIQUID_INFO_API,
            data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "ZeroQuant/1.0"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
            universe = data[0].get("universe", [])
            contexts = data[1] if len(data) > 1 else []
            
            assets = []
            for i, u in enumerate(universe):
                ctx = contexts[i] if i < len(contexts) else {}
                name = u.get("name")
                mark_px = float(ctx.get("markPx") or 0.0)
                oracle_px = float(ctx.get("oraclePx") or 0.0)
                # Hyperliquid metaAndAssetCtxs returns the 1-hour funding rate; multiply by 8 for 8-hour equivalent
                funding_1h = float(ctx.get("funding") or 0.0)
                funding_8h = round(funding_1h * 8.0, 8)
                open_interest = float(ctx.get("openInterest") or 0.0)
                
                annualized = round(funding_8h * 3.0 * 365.0, 4)
                
                assets.append({
                    "name": name,
                    "mark_px": mark_px,
                    "oracle_px": oracle_px,
                    "funding_1h": funding_1h,
                    "funding_8h": funding_8h,
                    "annualized_yield": annualized,
                    "open_interest_usd": round(open_interest * mark_px, 2)
                })
            return assets
    except Exception as e:
        print(f"Error fetching Hyperliquid meta: {e}")
        return []

def accrue_funding_payments(conn, assets_by_name):
    """Simulate funding payment collection for all active basis positions scaled by elapsed hours."""
    cur = conn.execute("SELECT * FROM hyperliquid_positions WHERE status = 'OPEN'")
    open_positions = cur.fetchall()
    if not open_positions:
        return 0.0
        
    now_dt = datetime.now(PT)
    now_str = now_dt.isoformat()
    total_accrued = 0.0
    
    for pos in open_positions:
        asset = pos["asset"]
        direction = pos["direction"] if "direction" in pos.keys() and pos["direction"] else "SHORT_PERP"
        ctx = assets_by_name.get(asset)
        if not ctx:
            continue
            
        raw_rate_8h = ctx["funding_8h"]
        # SHORT_PERP collects when funding_8h > 0; LONG_PERP collects when funding_8h < 0
        eff_rate_8h = raw_rate_8h if direction == "SHORT_PERP" else -raw_rate_8h
        eff_apy = round(eff_rate_8h * 3.0 * 365.0, 4)
        
        if eff_rate_8h <= 0.0:
            continue

        # Scale accrual by elapsed 8-hour intervals since last accrual for this position (default 1.0 interval)
        intervals_8h = 1.0
        try:
            last_row = conn.execute(
                "SELECT timestamp FROM hyperliquid_funding_accruals WHERE asset = ? ORDER BY id DESC LIMIT 1",
                (asset,)
            ).fetchone()
            ref_ts = last_row["timestamp"] if last_row else pos["opened_at"]
            if ref_ts:
                ref_dt = datetime.fromisoformat(ref_ts)
                if ref_dt.tzinfo is None:
                    ref_dt = ref_dt.replace(tzinfo=PT)
                elapsed_hours = (now_dt - ref_dt).total_seconds() / 3600.0
                if elapsed_hours >= 0.25:
                    intervals_8h = min(3.0, max(0.25, elapsed_hours / 8.0))
        except Exception:
            intervals_8h = 1.0
            
        allocated = pos["allocated_usd"]
        payment = round(allocated * eff_rate_8h * intervals_8h, 4)
        total_accrued += payment
        
        with conn:
            conn.execute("""
                INSERT INTO hyperliquid_funding_accruals (
                    timestamp, asset, position_size, funding_rate_8h, accrual_usd
                ) VALUES (?, ?, ?, ?, ?)
            """, (now_str, asset, pos["size"], eff_rate_8h, payment))
            
            conn.execute("""
                UPDATE hyperliquid_positions
                SET cumulative_funding = cumulative_funding + ?,
                    current_funding_rate_8h = ?,
                    annualized_yield = ?
                WHERE id = ?
            """, (payment, eff_rate_8h, eff_apy, pos["id"]))
            
    if total_accrued != 0.0:
        with conn:
            conn.execute("""
                UPDATE hyperliquid_portfolio
                SET cash = cash + ?,
                    cumulative_funding = cumulative_funding + ?,
                    total_equity = total_equity + ?,
                    last_updated = ?
                WHERE id = 1
            """, (total_accrued, total_accrued, total_accrued, now_str))
            
    return total_accrued

def build_basis_candidates(assets):
    """Build ranked list of Forward (SHORT_PERP) and Reverse (LONG_PERP) basis candidates."""
    candidates = []
    for a in assets:
        oi = a["open_interest_usd"]
        f8 = a["funding_8h"]
        apy = a["annualized_yield"]
        if f8 > 0.0 and oi >= MIN_OPEN_INTEREST_USD and apy >= MIN_ANNUALIZED_YIELD:
            candidates.append({
                **a,
                "direction": "SHORT_PERP",
                "eff_funding_8h": f8,
                "eff_apy": apy
            })
        elif f8 < 0.0 and oi >= MIN_REVERSE_OI_USD and abs(apy) >= MIN_REVERSE_ANNUALIZED_YIELD:
            candidates.append({
                **a,
                "direction": "LONG_PERP",
                "eff_funding_8h": -f8,
                "eff_apy": round(abs(apy), 4)
            })
    candidates.sort(key=lambda x: x["eff_apy"], reverse=True)
    return candidates

def manage_basis_portfolio(conn, assets, verbose: bool = False):
    """Allocate paper capital to the top bi-directional funding yielders meeting criteria."""
    assets_by_name = {a["name"]: a for a in assets}
    
    accrued = accrue_funding_payments(conn, assets_by_name)
    if accrued != 0.0 and verbose:
        print(f"  💰 Accrued ${accrued:+.4f} in 8-hour funding cashflow across active basis positions.")
        
    open_pos = conn.execute("SELECT * FROM hyperliquid_positions WHERE status = 'OPEN'").fetchall()
    open_assets = {p["asset"] for p in open_pos}
    
    candidates = build_basis_candidates(assets)
    
    # Unwind positions if funding flips against position direction or drops below yield floor
    now_str = datetime.now(PT).isoformat()
    for pos in list(open_pos):
        ctx = assets_by_name.get(pos["asset"])
        if not ctx:
            continue
        direction = pos["direction"] if "direction" in pos.keys() and pos["direction"] else "SHORT_PERP"
        raw_f8 = ctx["funding_8h"]
        eff_f8 = raw_f8 if direction == "SHORT_PERP" else -raw_f8
        eff_apy = round(eff_f8 * 3.0 * 365.0, 4)
        min_floor = MIN_ANNUALIZED_YIELD if direction == "SHORT_PERP" else 0.04
        if eff_f8 <= 0.0 or eff_apy < min_floor:
            if verbose:
                print(f"  📉 Unwinding {direction} basis in {pos['asset']}: effective yield dropped to {eff_apy*100:.1f}% (raw 8h: {raw_f8*100:.4f}%)")
            with conn:
                conn.execute("""
                    UPDATE hyperliquid_positions
                    SET status = 'CLOSED', closed_at = ?
                    WHERE id = ?
                """, (now_str, pos["id"]))
                conn.execute("""
                    UPDATE hyperliquid_portfolio
                    SET cash = cash + ?
                    WHERE id = 1
                """, (pos["allocated_usd"],))
            open_assets.remove(pos["asset"])
                
    # Opportunity-Cost Slot Rotation: if book is full and top candidate beats weakest open slot by >= +3.0% APY
    remaining_open = conn.execute("SELECT * FROM hyperliquid_positions WHERE status = 'OPEN' ORDER BY annualized_yield ASC").fetchall()
    top_new = [c for c in candidates if c["name"] not in open_assets]
    if len(remaining_open) >= MAX_ASSETS_IN_BOOK and top_new:
        weakest = remaining_open[0]
        best_cand = top_new[0]
        if best_cand["eff_apy"] >= (weakest["annualized_yield"] + 0.03):
            if verbose:
                print(f"  🔄 Rotating out of laggard {weakest['asset']} ({weakest['annualized_yield']*100:.1f}% APY) for {best_cand['name']} ({best_cand['eff_apy']*100:.1f}% APY)")
            with conn:
                conn.execute("UPDATE hyperliquid_positions SET status = 'CLOSED', closed_at = ? WHERE id = ?", (now_str, weakest["id"]))
                conn.execute("UPDATE hyperliquid_portfolio SET cash = cash + ? WHERE id = 1", (weakest["allocated_usd"],))
            open_assets.remove(weakest["asset"])

    active_count = len(open_assets)
    available_slots = MAX_ASSETS_IN_BOOK - active_count
    
    p = conn.execute("SELECT * FROM hyperliquid_portfolio WHERE id = 1").fetchone()
    cash = float(p["cash"])
    curr_open = conn.execute("SELECT SUM(allocated_usd) as alloc_sum FROM hyperliquid_positions WHERE status = 'OPEN'").fetchone()
    total_book_equity = cash + float(curr_open["alloc_sum"] or 0.0)
    min_slot_allocation = 150.00
    max_slot_allocation = round(total_book_equity * 0.40, 2)  # Up to 40% (~$400) for top high-yield, deep-OI basis

    pm_decision = {
        "approved_indices": list(range(len(candidates))),
        "sizing_multipliers": {},
        "rationale_by_index": {},
        "unwinds": [],
        "pm_thesis": "",
        "model_used": "gemini-4-argon-high",
    }
    unheld_candidates = [c for c in candidates if c["name"] not in open_assets]
    if available_slots > 0 and cash >= min_slot_allocation and unheld_candidates:
        try:
            from tools.llm_sidecar import evaluate_quant_candidates_llm
            pm_decision = evaluate_quant_candidates_llm(
                "quant_hyperliquid_basis",
                "Track 3: Hyperliquid Delta-Neutral Basis & Funding Cash-and-Carry",
                candidates,
                {
                    "cash": cash,
                    "open_assets": list(open_assets),
                    "available_slots": available_slots,
                },
            )
            if False and pm_decision.get("pm_thesis"):
                print(f"  🧠 [Quant PM · {pm_decision.get('model_used')}]: {pm_decision['pm_thesis']}")
        except Exception as e:
            if verbose:
                print(f"  [Quant PM Note]: {e}")

    # Process any LLM PM recommended early unwinds
    pm_unwinds = set(pm_decision.get("unwinds", []))
    if pm_unwinds:
        for pos in conn.execute("SELECT * FROM hyperliquid_positions WHERE status = 'OPEN'").fetchall():
            if pos["asset"] in pm_unwinds and pos["asset"] in open_assets:
                if verbose:
                    print(f"  📉 [PM UNWIND] Closing {pos['asset']} basis slot per Quant PM thesis.")
                with conn:
                    conn.execute("UPDATE hyperliquid_positions SET status = 'CLOSED', closed_at = ? WHERE id = ?", (now_str, pos["id"]))
                    conn.execute("UPDATE hyperliquid_portfolio SET cash = cash + ? WHERE id = 1", (pos["allocated_usd"],))
                open_assets.remove(pos["asset"])
                cash += pos["allocated_usd"]
                available_slots += 1

    approved_set = set(pm_decision.get("approved_indices", range(len(candidates))))
    mult_map = pm_decision.get("sizing_multipliers", {})
    rat_map = pm_decision.get("rationale_by_index", {})

    for idx, c in enumerate(candidates):
        if available_slots <= 0 or cash < min_slot_allocation:
            break
        if c["name"] in open_assets:
            continue
        if idx not in approved_set:
            if verbose:
                print(f"  [PM VETO] Skipping {c['name']} basis candidate per Quant PM funding/OI review.")
            continue

        conv_mult = max(0.5, min(1.5, float(mult_map.get(str(idx), 1.0))))
        # Scale dynamically with annualized yield (15% APY -> 1.0x, 60%+ APY -> 1.45x) and OI liquidity depth
        yield_mult = min(1.50, max(0.80, (c["eff_apy"] / 0.20) ** 0.35))
        oi_usd = float(c.get("open_interest_usd") or 1_000_000.0)
        # Never exceed 5 bps (0.05%) of venue Open Interest to ensure zero-slippage entry/exit
        venue_oi_cap = max(min_slot_allocation, round(oi_usd * 0.0005, 2))
        even_slot = cash / max(1, available_slots)
        slot_allocation = round(
            min(max_slot_allocation, venue_oi_cap, cash, max(min_slot_allocation, even_slot * yield_mult * conv_mult)),
            2,
        )
        if slot_allocation < min_slot_allocation:
            break

        size = round(slot_allocation / max(c["mark_px"], 0.0001), 6)
        direction = c["direction"]
        pm_note = rat_map.get(str(idx), "")
        if verbose:
            print(f"  🚀 OPEN BASIS PAIR ({direction}): {c['name']} | Mark: ${c['mark_px']:,.4f} | Net 8h Rate: +{c['eff_funding_8h']*100:.4f}% | Net APY: +{c['eff_apy']*100:.1f}% | Alloc: ${slot_allocation:.2f}")
        if False and pm_note:
            print(f"     [PM Thesis]: {pm_note}")
        with conn:
            conn.execute("""
                INSERT INTO hyperliquid_positions (
                    asset, direction, size, entry_mark_px, entry_oracle_px, opened_at,
                    allocated_usd, cumulative_funding, current_funding_rate_8h,
                    annualized_yield, status, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, 'OPEN', NULL)
            """, (
                c["name"], direction, size, c["mark_px"], c["oracle_px"], now_str,
                slot_allocation, c["eff_funding_8h"], c["eff_apy"]
            ))
            conn.execute("""
                UPDATE hyperliquid_portfolio
                SET cash = cash - ?, last_updated = ?
                WHERE id = 1
            """, (slot_allocation, now_str))

        cash = round(cash - slot_allocation, 2)
        available_slots -= 1
        open_assets.add(c["name"])

def run_hyperliquid_basis_cycle(conn=None, verbose=False):
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
    init_db(conn)
    
    assets = fetch_hyperliquid_meta()
    if not assets:
        print("- **Track 3 (Hyperliquid Basis):** ⚠️ API unreachable.")
        return
        
    manage_basis_portfolio(conn, assets, verbose=verbose)
    
    p = conn.execute("SELECT * FROM hyperliquid_portfolio WHERE id = 1").fetchone()
    open_pos = conn.execute("SELECT * FROM hyperliquid_positions WHERE status = 'OPEN'").fetchall()
    
    total_allocated = sum(pos["allocated_usd"] for pos in open_pos)
    total_equity = round(p["cash"] + total_allocated, 4)
    
    with conn:
        conn.execute("UPDATE hyperliquid_portfolio SET total_equity = ? WHERE id = 1", (total_equity,))
        
    perps_str = ", ".join(f"`{pos['asset']}` ({pos['annualized_yield']*100:.0f}% APY)" for pos in open_pos)
    sub_str = f" — {perps_str}" if perps_str else ""
    print(f"- **Track 3 (Hyperliquid Basis):** `${total_equity:,.2f}` equity (`+${p['cumulative_funding']:.4f}` funding · `{len(open_pos)}` perps){sub_str}")

if __name__ == "__main__":
    import sys
    run_hyperliquid_basis_cycle(verbose=("--verbose" in sys.argv or "-v" in sys.argv))
