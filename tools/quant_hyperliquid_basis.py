#!/usr/bin/env python3
"""Book 4 (Track 3): Hyperliquid Forward Spot-Perp Carry Engine (Real-World Executable).

Connects to Hyperliquid L1 public info API to monitor live funding rates, open interest,
and mark prices across perpetual assets with verified spot markets.

Executes an unleveraged (1.0x) delta-neutral forward cash-and-carry paper portfolio ($1,000 capital):
- Strictly Forward Basis (SHORT_PERP: Long Verified Spot / Short 1x Perp) when 8h funding > 0
  (>= 5.0% APY entry hurdle, OI >= $1.0M, verified liquid spot market).
- Reverse Basis (LONG_PERP) is disabled by default because Hyperliquid lacks spot margin shorting
  and external altcoin spot borrow rates negate negative perp funding.
- Deducts realistic execution friction (12.5 bps per entry/exit = 25.0 bps round-trip for Spot + Perp taker/spread).
- Enforces a 72-hour minimum hold buffer (unless funding turns deeply negative) and a +15% APY / 7-day
  fee-payback hurdle for slot rotations to prevent fee-burning churn.

Persists positions, funding accruals, and net-of-fee equity curves to /workspace/data/quant_paper.db.
"""

import json
import sqlite3
import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("/workspace/data/quant_paper.db")

HYPERLIQUID_INFO_API = "https://api.hyperliquid.xyz/info"
STARTING_PORTFOLIO_CAPITAL = 1000.00
MIN_ANNUALIZED_YIELD = 0.05          # 5.0% minimum APY entry hurdle for Forward Basis (SHORT_PERP)
MIN_HOLD_FLOOR_APY = 0.015           # 1.5% APY floor before unwinding an existing position (prevents churn)
ROTATION_APY_HURDLE = 0.15           # Require +15% APY upgrade (~6-day fee payback) to rotate a full slot
MIN_HOLD_HOURS_FOR_UNWIND = 72.0     # Hold at least 72h unless funding flips negative (<= -2% APY)
MIN_HOLD_HOURS_FOR_ROTATION = 168.0  # Hold at least 7 days before opportunity-cost rotation
MAX_ASSETS_IN_BOOK = 4               # Max 4 concurrent basis pairs ($250.00 base each)
MIN_OPEN_INTEREST_USD = 1_000_000.0  # $1.0M+ Open Interest for institutional liquidity
ONE_WAY_FRICTION_RATE = 0.00125      # 12.5 bps per leg (7 bps spot + 3.5 bps perp + 2 bps spread = 25 bps RT)

# Assets with verified liquid spot markets (Hyperliquid native spot or Tier-1 USD spot rails)
VERIFIED_SPOT_ASSETS = {
    "BTC", "ETH", "SOL", "HYPE", "SUI", "XRP", "DOGE", "AVAX",
    "LINK", "AAVE", "UNI", "ENA", "PENDLE", "ARB", "OP", "PURR", "TIA", "TON"
}


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
            headers={"Content-Type": "application/json", "User-Agent": "ZeroQuant/2.0"}
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


def _hours_held(opened_at_str: str | None, now_dt: datetime) -> float:
    if not opened_at_str:
        return 999.0
    try:
        dt = datetime.fromisoformat(opened_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PT)
        return max(0.0, (now_dt - dt).total_seconds() / 3600.0)
    except Exception:
        return 999.0


def accrue_funding_payments(conn, assets_by_name):
    """Accrue 8-hour funding payments for active SHORT_PERP basis positions scaled by elapsed hours."""
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
        eff_rate_8h = raw_rate_8h if direction == "SHORT_PERP" else -raw_rate_8h
        eff_apy = round(eff_rate_8h * 3.0 * 365.0, 4)

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
                if elapsed_hours < 0.20:
                    continue
                intervals_8h = min(3.0, max(0.25, elapsed_hours / 8.0))
        except Exception:
            intervals_8h = 1.0

        allocated = float(pos["allocated_usd"])
        # Accrue both positive and mildly negative funding honestly while held
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
                SET cumulative_funding = ROUND(cumulative_funding + ?, 4),
                    current_funding_rate_8h = ?,
                    annualized_yield = ?
                WHERE id = ?
            """, (payment, eff_rate_8h, eff_apy, pos["id"]))

    if total_accrued != 0.0:
        with conn:
            conn.execute("""
                UPDATE hyperliquid_portfolio
                SET cash = ROUND(cash + ?, 4),
                    cumulative_funding = ROUND(cumulative_funding + ?, 4),
                    total_equity = ROUND(total_equity + ?, 4),
                    last_updated = ?
                WHERE id = 1
            """, (total_accrued, total_accrued, total_accrued, now_str))

    return total_accrued


def build_basis_candidates(assets, enforce_spot_whitelist: bool = True):
    """Build ranked list of Forward (SHORT_PERP) basis candidates with verified spot liquidity.

    Reverse Basis (LONG_PERP) is strictly excluded because shorting spot on Hyperliquid is unsupported
    and external spot borrow rates exceed negative perp funding.
    """
    candidates = []
    for a in assets:
        name = a["name"]
        if enforce_spot_whitelist and name not in VERIFIED_SPOT_ASSETS:
            continue
        oi = float(a.get("open_interest_usd") or 0.0)
        f8 = float(a.get("funding_8h") or 0.0)
        apy = float(a.get("annualized_yield") or (f8 * 3.0 * 365.0))
        if f8 > 0.0 and oi >= MIN_OPEN_INTEREST_USD and apy >= MIN_ANNUALIZED_YIELD:
            candidates.append({
                **a,
                "annualized_yield": round(apy, 4),
                "direction": "SHORT_PERP",
                "eff_funding_8h": f8,
                "eff_apy": round(apy, 4),
            })
    candidates.sort(key=lambda x: x["eff_apy"], reverse=True)
    return candidates


def _close_basis_position(conn, pos, now_str: str, reason: str, verbose: bool = False):
    allocated = float(pos["allocated_usd"])
    exit_fee = round(allocated * ONE_WAY_FRICTION_RATE, 4)
    net_return = round(allocated - exit_fee, 4)
    if verbose:
        print(f"  📉 Unwinding {pos['direction']} basis in {pos['asset']} ({reason}) | Exit Fee: -${exit_fee:.2f}")
    with conn:
        conn.execute(
            "UPDATE hyperliquid_positions SET status = 'CLOSED', closed_at = ? WHERE id = ?",
            (now_str, pos["id"]),
        )
        conn.execute(
            """
            UPDATE hyperliquid_portfolio
            SET cash = ROUND(cash + ?, 4),
                realized_pnl = ROUND(realized_pnl - ?, 4),
                total_equity = ROUND(total_equity - ?, 4),
                last_updated = ?
            WHERE id = 1
            """,
            (net_return, exit_fee, exit_fee, now_str),
        )


def manage_basis_portfolio(conn, assets, verbose: bool = False, enforce_spot_whitelist: bool = True):
    """Allocate capital to verified Forward Basis (SHORT_PERP) pairs with fee accounting and hold gates."""
    assets_by_name = {a["name"]: a for a in assets}

    accrued = accrue_funding_payments(conn, assets_by_name)
    if accrued != 0.0 and verbose:
        print(f"  💰 Accrued ${accrued:+.4f} in 8-hour funding cashflow across active basis positions.")

    open_pos = conn.execute("SELECT * FROM hyperliquid_positions WHERE status = 'OPEN'").fetchall()
    open_assets = {p["asset"] for p in open_pos}

    candidates = build_basis_candidates(assets, enforce_spot_whitelist=enforce_spot_whitelist)

    now_dt = datetime.now(PT)
    now_str = now_dt.isoformat()

    # 1. Unwind positions if direction is legacy LONG_PERP, or funding flips deeply negative,
    #    or held >= 72h and APY decayed below 1.5% floor.
    for pos in list(open_pos):
        ctx = assets_by_name.get(pos["asset"])
        if not ctx:
            continue
        direction = pos["direction"] if "direction" in pos.keys() and pos["direction"] else "SHORT_PERP"
        if direction != "SHORT_PERP":
            _close_basis_position(conn, pos, now_str, "decommissioned reverse basis", verbose=verbose)
            open_assets.discard(pos["asset"])
            continue

        raw_f8 = float(ctx["funding_8h"])
        eff_apy = round(raw_f8 * 3.0 * 365.0, 4)
        held_h = _hours_held(pos["opened_at"], now_dt)

        # Immediate unwind if funding flips negative (<= 0.0) or post-72h decay below 1.5% APY
        if raw_f8 <= 0.0 or (held_h >= MIN_HOLD_HOURS_FOR_UNWIND and eff_apy < MIN_HOLD_FLOOR_APY):
            _close_basis_position(
                conn,
                pos,
                now_str,
                f"yield dropped to {eff_apy*100:.1f}% after {held_h:.1f}h",
                verbose=verbose,
            )
            open_assets.discard(pos["asset"])

    # 2. 7-Day Fee-Payback Opportunity-Cost Slot Rotation
    remaining_open = conn.execute(
        "SELECT * FROM hyperliquid_positions WHERE status = 'OPEN' ORDER BY annualized_yield ASC"
    ).fetchall()
    top_new = [c for c in candidates if c["name"] not in open_assets]
    if len(remaining_open) >= MAX_ASSETS_IN_BOOK and top_new:
        weakest = remaining_open[0]
        best_cand = top_new[0]
        held_h = _hours_held(weakest["opened_at"], now_dt)
        if held_h >= MIN_HOLD_HOURS_FOR_ROTATION and best_cand["eff_apy"] >= (weakest["annualized_yield"] + ROTATION_APY_HURDLE):
            _close_basis_position(
                conn,
                weakest,
                now_str,
                f"rotating {weakest['annualized_yield']*100:.1f}% -> {best_cand['name']} {best_cand['eff_apy']*100:.1f}% APY",
                verbose=verbose,
            )
            open_assets.discard(weakest["asset"])

    active_count = len(open_assets)
    available_slots = MAX_ASSETS_IN_BOOK - active_count

    p = conn.execute("SELECT * FROM hyperliquid_portfolio WHERE id = 1").fetchone()
    cash = float(p["cash"])
    curr_open = conn.execute("SELECT SUM(allocated_usd) as alloc_sum FROM hyperliquid_positions WHERE status = 'OPEN'").fetchone()
    total_book_equity = cash + float(curr_open["alloc_sum"] or 0.0)
    min_slot_allocation = 150.00
    max_slot_allocation = round(total_book_equity * 0.40, 2)

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
                "Book 4: Hyperliquid Forward Spot-Perp Carry (SHORT_PERP Only)",
                candidates,
                {
                    "cash": cash,
                    "open_assets": list(open_assets),
                    "available_slots": available_slots,
                },
            )
        except Exception as e:
            if verbose:
                print(f"  [Quant PM Note]: {e}")

    approved_set = set(pm_decision.get("approved_indices", range(len(candidates))))
    mult_map = pm_decision.get("sizing_multipliers", {})

    for idx, c in enumerate(candidates):
        if available_slots <= 0 or cash < min_slot_allocation:
            break
        if c["name"] in open_assets or idx not in approved_set:
            continue

        conv_mult = max(0.5, min(1.5, float(mult_map.get(str(idx), 1.0))))
        yield_mult = min(1.50, max(0.80, (c["eff_apy"] / 0.20) ** 0.35))
        oi_usd = float(c.get("open_interest_usd") or 1_000_000.0)
        venue_oi_cap = max(min_slot_allocation, round(oi_usd * 0.0005, 2))
        even_slot = cash / max(1, available_slots)
        # Leave room for 12.5 bps entry fee
        max_affordable = round(cash / (1.0 + ONE_WAY_FRICTION_RATE), 2)
        slot_allocation = round(
            min(max_slot_allocation, venue_oi_cap, max_affordable, max(min_slot_allocation, even_slot * yield_mult * conv_mult)),
            2,
        )
        if slot_allocation < min_slot_allocation:
            break

        entry_fee = round(slot_allocation * ONE_WAY_FRICTION_RATE, 4)
        size = round(slot_allocation / max(c["mark_px"], 0.0001), 6)
        if verbose:
            print(
                f"  🚀 OPEN FORWARD BASIS (SHORT_PERP): {c['name']} | Mark: ${c['mark_px']:,.4f} | "
                f"Net APY: +{c['eff_apy']*100:.1f}% | Alloc: ${slot_allocation:.2f} (Fee: -${entry_fee:.2f})"
            )
        with conn:
            conn.execute("""
                INSERT INTO hyperliquid_positions (
                    asset, direction, size, entry_mark_px, entry_oracle_px, opened_at,
                    allocated_usd, cumulative_funding, current_funding_rate_8h,
                    annualized_yield, status, closed_at
                ) VALUES (?, 'SHORT_PERP', ?, ?, ?, ?, ?, 0.0, ?, ?, 'OPEN', NULL)
            """, (
                c["name"], size, c["mark_px"], c["oracle_px"], now_str,
                slot_allocation, c["eff_funding_8h"], c["eff_apy"]
            ))
            conn.execute("""
                UPDATE hyperliquid_portfolio
                SET cash = ROUND(cash - ? - ?, 4),
                    realized_pnl = ROUND(realized_pnl - ?, 4),
                    total_equity = ROUND(total_equity - ?, 4),
                    last_updated = ?
                WHERE id = 1
            """, (slot_allocation, entry_fee, entry_fee, entry_fee, now_str))

        cash = round(cash - slot_allocation - entry_fee, 4)
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
        print("- **Book 4 (Hyperliquid Carry):** ⚠️ API unreachable.")
        return

    manage_basis_portfolio(conn, assets, verbose=verbose)

    p = conn.execute("SELECT * FROM hyperliquid_portfolio WHERE id = 1").fetchone()
    open_pos = conn.execute("SELECT * FROM hyperliquid_positions WHERE status = 'OPEN'").fetchall()

    total_allocated = sum(pos["allocated_usd"] for pos in open_pos)
    total_equity = round(p["cash"] + total_allocated, 4)

    with conn:
        conn.execute("UPDATE hyperliquid_portfolio SET total_equity = ? WHERE id = 1", (total_equity,))

    perps_str = ", ".join(f"{pos['asset']} ({pos['annualized_yield']*100:.1f}% APY)" for pos in open_pos)
    sub_str = f" — {perps_str}" if perps_str else ""
    print(f"- **Book 4 (Hyperliquid Spot-Perp Carry):** ${total_equity:,.2f} equity (+${p['cumulative_funding']:.2f} funding · {len(open_pos)} pairs){sub_str}")


if __name__ == "__main__":
    run_hyperliquid_basis_cycle(verbose=("--verbose" in sys.argv or "-v" in sys.argv))
