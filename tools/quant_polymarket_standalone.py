#!/usr/bin/env python3
"""Track 2: Polymarket Standalone Structural Inefficiencies Engine.

Exploits two structural arbitrage mechanisms native to Polymarket's CTF CLOB:
1. Combinatorial Negative-Risk Underrounds & Overrounds:
   - Strategy A (Taker Underround YES): Sum(Ask_YES) < $0.995 -> Buy all YES outcomes for <$1.00.
   - Strategy B (Active-Leg Overround NO): Sum(Bid_YES) > $1.005 across K active legs -> Buy NO on the K active legs; at least K-1 are guaranteed to pay $1.00.
   - Strategy C (Zero-Vigorish 3-Way Maker Underround): In 3-way negRisk sports moneylines where Taker Sum(Ask_YES) <= $1.000, passive maker placement on the widest leg locks in a $0.990 basket (+1.0% edge).
2. Oracle Latency & Strike Dominance Sniping:
   - One-Touch Barrier Latency: Compares Coinbase real-time spot against Polymarket one-touch 'reach/dip' barriers.
   - Strike Monotonicity Dominance: Detects inverted strike ladders where a higher upward strike has a higher Bid than a lower strike's Ask (Buy YES low strike + Buy NO high strike for <$1.00).

Persists paper trades to /workspace/data/quant_paper.db under an isolated $1,000.00 paper portfolio.
"""

import json
import os
import re
import sqlite3
import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("/workspace/data/quant_paper.db")

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com/events"
COINBASE_TICKER_API = "https://api.coinbase.com/v2/prices"
MAX_HORIZON_DAYS = 14.0

def init_db(conn):
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS polymarket_standalone_portfolio (
                id INTEGER PRIMARY KEY,
                starting_balance REAL,
                cash REAL,
                realized_pnl REAL,
                last_updated TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS polymarket_standalone_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                strategy TEXT,
                event_title TEXT,
                market_count INTEGER,
                basket_json TEXT,
                sum_ask REAL,
                locked_profit REAL,
                net_edge REAL,
                status TEXT,
                settled_at TEXT,
                realized_pnl REAL
            )
        """)
        cur = conn.execute("SELECT id, starting_balance, cash FROM polymarket_standalone_portfolio WHERE id = 1")
        row = cur.fetchone()
        if not row:
            now_str = datetime.now(PT).isoformat()
            conn.execute(
                "INSERT INTO polymarket_standalone_portfolio (id, starting_balance, cash, realized_pnl, last_updated) VALUES (1, 1000.0, 1000.0, 0.0, ?)",
                (now_str,)
            )

def fetch_json(url, timeout=7):
    headers = {"User-Agent": "ZeroQuant/1.0 (ryan@brockventures.com)", "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def get_live_crypto_spots():
    """Fetch real-time spot prices from Coinbase public ticker for major assets."""
    spots = {}
    for coin in ["BTC", "ETH", "SOL"]:
        try:
            url = f"{COINBASE_TICKER_API}/{coin}-USD/spot"
            data = fetch_json(url)
            spots[coin] = float(data.get("data", {}).get("amount", 0.0))
        except Exception:
            pass
    return spots

def parse_price(val_str):
    """Parse numeric price strings supporting $, commas, and k/m suffixes."""
    val_str = val_str.lower().replace(",", "").replace("$", "").strip()
    if val_str.endswith("k"):
        return float(val_str[:-1]) * 1000.0
    elif val_str.endswith("m"):
        return float(val_str[:-1]) * 1000000.0
    return float(val_str)

def get_all_active_events():
    """Ingest active Polymarket events across top volume, sports, soccer, crypto, and politics."""
    urls = [
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&order=volume24hr&ascending=false",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=sports&order=volume24hr&ascending=false",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=soccer&order=volume24hr&ascending=false",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=nfl",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=mlb",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=nba",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=nhl",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=crypto&order=volume24hr&ascending=false",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=politics",
        f"{POLYMARKET_GAMMA}?limit=100&active=true&closed=false&tag_slug=pop-culture"
    ]
    all_events = []
    for u in urls:
        try:
            data = fetch_json(u)
            all_events.extend(data)
        except Exception:
            pass
            
    seen = set()
    unique = []
    for e in all_events:
        eid = e.get("id")
        if eid and eid not in seen:
            seen.add(eid)
            unique.append(e)
    return unique

def is_within_horizon(event, max_days=MAX_HORIZON_DAYS):
    """Enforce maximum resolution horizon (default 14 days / 1-2 weeks).
    Rejects far-future multi-month/year lockups (e.g. 2027 Champion futures).
    """
    title = event.get("title", "")
    current_year = datetime.now(PT).year
    for y in range(current_year + 1, current_year + 10):
        if str(y) in title:
            return False

    end_date_str = event.get("endDate")
    if not end_date_str:
        markets = event.get("markets", [])
        if markets:
            end_date_str = markets[0].get("endDate")

    if not end_date_str:
        return True

    try:
        clean_str = end_date_str.replace("Z", "+00:00")
        end_dt = datetime.fromisoformat(clean_str)
        now_utc = datetime.now(timezone.utc)
        days = (end_dt - now_utc).total_seconds() / 86400.0
        return 0.0 <= days <= max_days
    except Exception:
        return True

def sweep_negative_risk_underrounds(events=None):
    """Scan mutually exclusive (negRisk or winner-take-all) events for underrounds and overrounds."""
    if events is None:
        events = get_all_active_events()

    opportunities = []

    for ev in events:
        if not is_within_horizon(ev):
            continue
        markets = ev.get("markets", [])
        if len(markets) < 3:
            continue

        title = ev.get("title", "")
        t_lower = title.lower()
        is_neg_risk = bool(ev.get("negRisk") or ev.get("enableNegRisk"))

        # Must be either native negRisk or explicit single-winner tournament/election/bucket
        if not is_neg_risk and not any(w in t_lower for w in ["champion", "winner", "division", "mvp", "nominee", "president", "chancellor", "price on", "tweets"]):
            continue
        # Exclude non-exclusive multi-selection props or sub-inning markets
        if any(neg in t_lower for neg in ["player props", "margin", "points", "inning", "first 5", "by...?", "more markets"]):
            continue

        real_candidates = []
        has_untraded = False

        for m in markets:
            q = m.get("question", "")
            q_lower = q.lower()
            best_ask_raw = m.get("bestAsk")
            best_bid_raw = m.get("bestBid")

            # Skip dummy placeholder markets
            is_placeholder = any(p in q_lower for p in ["another team", "other", "team a", "team b", "team c", "team d", "team e"])
            if is_placeholder and (best_ask_raw is None or float(best_ask_raw) >= 0.99 or best_bid_raw is None or float(best_bid_raw) == 0.0):
                continue

            # Require a valid ask on every outcome; allow bestBid=None on extreme tail outcomes (<= 2c ask)
            if best_ask_raw is None:
                has_untraded = True
                break

            ask = float(best_ask_raw)
            bid = float(best_bid_raw) if best_bid_raw is not None else 0.0

            if ask <= 0.0 or ask > 1.0 or bid < 0.0 or (ask - bid) > 0.30:
                has_untraded = True
                break

            leg_liq = float(m.get("liquidityNum") or m.get("liquidity") or 10000.0)
            real_candidates.append({
                "market_id": m.get("id"),
                "question": q,
                "ask": ask,
                "bid": bid,
                "no_ask": round(1.0 - bid, 4) if bid > 0.0 else 1.0,
                "liquidity_usd": leg_liq,
            })

        if has_untraded or len(real_candidates) < 3:
            continue

        N = len(real_candidates)
        sum_ask = sum(c["ask"] for c in real_candidates)
        min_leg_liq = min(c.get("liquidity_usd", 10000.0) for c in real_candidates)
        # Cap ticket at 2.5% of thinnest leg's liquidity pool (bounded between $40 and $275) for zero-slippage fill
        basket_fillable_usd = round(min(275.0, max(40.0, min_leg_liq * 0.025)), 2)
        
        # Strategy A: Combinatorial Underround (Buy YES Basket)
        # Condition: Sum(Ask_YES) < $0.995 (Guaranteed payout $1.00, 0 bps Polymarket CLOB fee)
        net_cost_a = round(sum_ask, 4)
        if net_cost_a < 0.995:
            net_edge_a = round(1.00 - net_cost_a, 4)
            if net_edge_a <= 0.25:
                opportunities.append({
                    "strategy": "combinatorial_underround_yes",
                    "event_title": title,
                    "market_count": N,
                    "basket": real_candidates,
                    "sum_ask": round(sum_ask, 4),
                    "total_cost": net_cost_a,
                    "net_edge": net_edge_a,
                    "max_fillable_usd": basket_fillable_usd,
                })
        elif N == 3 and is_neg_risk and net_cost_a <= 1.000:
            # Strategy C: Zero-Vigorish 3-Way Match Moneyline Maker Underround
            # When 3-way soccer moneyline has 0% taker overround (Sum(Ask) == 1.000), posting a passive
            # maker bid (+1c above bestBid) on the widest leg captures a $0.990 complete set (+1.0% edge).
            widest_spread = max(c["ask"] - c["bid"] for c in real_candidates)
            if widest_spread >= 0.01:
                maker_cost = round(net_cost_a - 0.01, 4)
                net_edge_c = round(1.00 - maker_cost, 4)
                opportunities.append({
                    "strategy": "combinatorial_maker_underround_yes",
                    "event_title": f"{title} (3-Way Maker)",
                    "market_count": N,
                    "basket": real_candidates,
                    "sum_ask": maker_cost,
                    "total_cost": maker_cost,
                    "net_edge": net_edge_c,
                    "max_fillable_usd": basket_fillable_usd,
                })

        # Strategy B: Active-Leg Combinatorial Overround (Buy NO Basket on Active Bid Legs)
        # Only buy NO on the K outcomes with resting Bid >= $0.005. Since at most 1 outcome can win,
        # at least K-1 of our K NO contracts are guaranteed to resolve to $1.00.
        active_bid_legs = [c for c in real_candidates if c["bid"] >= 0.005]
        K = len(active_bid_legs)
        if K >= 3:
            sum_active_bid = sum(c["bid"] for c in active_bid_legs)
            net_edge_b = round(sum_active_bid - 1.00, 4)
            if 0.005 <= net_edge_b <= 0.25:
                total_no_cost = round(sum(c["no_ask"] for c in active_bid_legs), 4)
                unit_cost = round(total_no_cost / (K - 1), 4)
                opportunities.append({
                    "strategy": "combinatorial_overround_no",
                    "event_title": title,
                    "market_count": K,
                    "basket": active_bid_legs,
                    "sum_ask": unit_cost,
                    "total_cost": unit_cost,
                    "net_edge": round(1.00 - unit_cost, 4)
                })

    return opportunities

def scan_oracle_latency_opportunities(events=None):
    """Detect stale Polymarket one-touch barriers vs. live Coinbase spot and strike dominance inversions."""
    if events is None:
        events = get_all_active_events()
        
    opportunities = []
    spots = get_live_crypto_spots()
        
    for ev in events:
        if not is_within_horizon(ev):
            continue
        title = ev.get("title", "")
        t_lower = title.lower()
        
        if any(neg in t_lower for neg in ["gas", "airdrop", "market cap", "reserve", "ethena", "etf", "flip", "dominance"]):
            continue
            
        coin = None
        if re.search(r"\b(bitcoin|btc)\b", t_lower):
            coin = "BTC"
        elif re.search(r"\b(ethereum|eth)\b", t_lower):
            coin = "ETH"
        elif re.search(r"\b(solana|sol)\b", t_lower):
            coin = "SOL"
            
        if not coin:
            continue
            
        spot = spots.get(coin) if spots else None
        markets = ev.get("markets", [])
        reach_ladder = []
        
        for m in markets:
            q = m.get("question", "")
            q_lower = q.lower()
            if any(neg in q_lower for neg in ["gas", "airdrop", "market cap"]):
                continue
                
            best_bid_raw = m.get("bestBid")
            best_ask_raw = m.get("bestAsk")
            if best_ask_raw is None or best_bid_raw is None:
                continue
                
            yes_ask = float(best_ask_raw)
            yes_bid = float(best_bid_raw)
            
            # Strict One-Touch Barrier vs. European Point-in-Time Expiry Filter:
            if re.search(r"\b(?:be\s+above|be\s+below|close\s+above|close\s+below|above\s+\$?[\d,\.]+[km]?\s+on|below\s+\$?[\d,\.]+[km]?\s+on)\b", q_lower):
                continue

            reach_m = re.search(r"\b(?:reach|hit|touch)\s+\$?([\d,\.]+[km]?)", q, re.IGNORECASE)
            dip_m = re.search(r"\b(?:dip|drop|fall)\s+to\s+\$?([\d,\.]+[km]?)", q, re.IGNORECASE)
            
            if reach_m:
                try:
                    strike = parse_price(reach_m.group(1))
                    if 0.001 <= yes_ask <= 0.99 and 0.001 <= yes_bid <= 0.99:
                        reach_ladder.append({
                            "market_id": m.get("id"),
                            "question": q,
                            "strike": strike,
                            "ask": yes_ask,
                            "bid": yes_bid
                        })
                    if spot and spot >= strike * 1.005 and 0.05 < yes_ask <= 0.85:
                        edge = round(0.99 - yes_ask, 4)
                        opportunities.append({
                            "strategy": "oracle_latency_snipe",
                            "event_title": f"{title} - {q}",
                            "market_count": 1,
                            "basket": [{"market_id": m.get("id"), "coin": coin, "side": "YES", "ask": yes_ask, "spot": spot, "strike": strike}],
                            "sum_ask": yes_ask,
                            "total_cost": yes_ask,
                            "net_edge": edge
                        })
                except Exception:
                    pass
                    
            if dip_m and spot:
                try:
                    strike = parse_price(dip_m.group(1))
                    if spot <= strike * 0.995 and 0.05 < yes_ask <= 0.85:
                        edge = round(0.99 - yes_ask, 4)
                        opportunities.append({
                            "strategy": "oracle_latency_snipe",
                            "event_title": f"{title} - {q}",
                            "market_count": 1,
                            "basket": [{"market_id": m.get("id"), "coin": coin, "side": "YES", "ask": yes_ask, "spot": spot, "strike": strike}],
                            "sum_ask": yes_ask,
                            "total_cost": yes_ask,
                            "net_edge": edge
                        })
                except Exception:
                    pass

        # Check Strike Monotonicity / Dominance Inversions across upward reach strikes:
        # If Strike_High > Strike_Low, P(Reach High) <= P(Reach Low).
        # If Bid(High) > Ask(Low), buying YES(Low) @ Ask(Low) + NO(High) @ (1 - Bid(High)) costs < $1.00
        # and guarantees >= $1.00 payout ($2.00 if price lands between Low and High).
        if len(reach_ladder) >= 2:
            reach_ladder.sort(key=lambda x: x["strike"])
            for i in range(len(reach_ladder)):
                for j in range(i + 1, len(reach_ladder)):
                    low_leg = reach_ladder[i]
                    high_leg = reach_ladder[j]
                    if high_leg["strike"] > low_leg["strike"] and high_leg["bid"] > low_leg["ask"]:
                        no_high_ask = round(1.0 - high_leg["bid"], 4)
                        pair_cost = round(low_leg["ask"] + no_high_ask, 4)
                        dom_edge = round(1.00 - pair_cost, 4)
                        if dom_edge >= 0.001:
                            opportunities.append({
                                "strategy": "strike_dominance_arb",
                                "event_title": f"{title} (${int(low_leg['strike']):,} YES / ${int(high_leg['strike']):,} NO)",
                                "market_count": 2,
                                "basket": [
                                    {"market_id": low_leg["market_id"], "side": "YES", "strike": low_leg["strike"], "ask": low_leg["ask"]},
                                    {"market_id": high_leg["market_id"], "side": "NO", "strike": high_leg["strike"], "ask": no_high_ask}
                                ],
                                "sum_ask": pair_cost,
                                "total_cost": pair_cost,
                                "net_edge": max(dom_edge, 0.005)
                            })
                    
    return opportunities

def settle_resolved_polymarket_trades(conn, verbose: bool = False):
    """Settle OPEN Polymarket standalone trades whose markets have closed/resolved."""
    init_db(conn)
    open_trades = conn.execute("SELECT * FROM polymarket_standalone_trades WHERE status = 'OPEN'").fetchall()
    if not open_trades:
        return []

    now_str = datetime.now(PT).isoformat()
    settled = []
    for tr in open_trades:
        try:
            basket = json.loads(tr["basket_json"] or "[]")
        except Exception:
            basket = []
        if not basket:
            continue
        m_id = basket[0].get("market_id")
        if not m_id:
            continue
        try:
            m_data = fetch_json(f"https://gamma-api.polymarket.com/markets/{m_id}", timeout=4)
            if m_data.get("closed") is True or m_data.get("archived") is True:
                net_edge = float(tr["net_edge"] or 0.01)
                locked_pnl = float(tr["locked_profit"] or 0.0)
                contracts = round(locked_pnl / max(net_edge, 0.0001))
                cost = round(contracts * float(tr["sum_ask"]), 2)
                payout = round(cost + locked_pnl, 2)
                with conn:
                    conn.execute(
                        "UPDATE polymarket_standalone_trades SET status = 'SETTLED', settled_at = ?, realized_pnl = ? WHERE id = ?",
                        (now_str, locked_pnl, tr["id"])
                    )
                    conn.execute(
                        "UPDATE polymarket_standalone_portfolio SET cash = ROUND(cash + ?, 2), realized_pnl = ROUND(realized_pnl + ?, 2), last_updated = ? WHERE id = 1",
                        (payout, locked_pnl, now_str)
                    )
                settled.append({"id": tr["id"], "title": tr["event_title"], "pnl": locked_pnl})
        except Exception:
            pass
    if settled and verbose:
        tot_pnl = sum(s["pnl"] for s in settled)
        print(f"  💰 Settled {len(settled)} Polymarket standalone trade(s) (Realized P&L: +${tot_pnl:.2f})")
    return settled

def run_polymarket_standalone_scan(conn=None, execute=True, verbose=False):
    now_pt = datetime.now(PT)
    if verbose:
        print(f"[{now_pt.strftime('%Y-%m-%d %I:%M:%S %p PT')}] Scanning Polymarket Standalone Structural Inefficiencies...")
    
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
    init_db(conn)
    settle_resolved_polymarket_trades(conn, verbose=verbose)
    
    events = get_all_active_events()
    underrounds = sweep_negative_risk_underrounds(events)
    snipes = scan_oracle_latency_opportunities(events)
    if verbose:
        print(f"  Ingested {len(events)} events -> {len(underrounds)} underrounds/overrounds, {len(snipes)} snipes/dominance.")
    
    all_opps = sorted(underrounds + snipes, key=lambda x: x["net_edge"], reverse=True)
    
    row = conn.execute("SELECT cash, starting_balance, realized_pnl FROM polymarket_standalone_portfolio WHERE id = 1").fetchone()
    current_cash = float(row["cash"]) if row else 1000.0
    realized_pnl = float(row["realized_pnl"]) if row else 0.0
    
    existing_trades = conn.execute("SELECT strategy, event_title FROM polymarket_standalone_trades WHERE status = 'OPEN'").fetchall()
    open_keys = {(r["strategy"], r["event_title"]) for r in existing_trades}

    pm_decision = {
        "approved_indices": list(range(len(all_opps))),
        "sizing_multipliers": {},
        "rationale_by_index": {},
        "pm_thesis": "",
        "model_used": "gemini-4-argon-high",
    }
    try:
        from tools.llm_sidecar import evaluate_quant_candidates_llm
        pm_decision = evaluate_quant_candidates_llm(
            "quant_polymarket_standalone",
            "Track 2: Polymarket Standalone Structural Inefficiencies",
            all_opps,
            {"cash": current_cash, "open_positions_count": len(open_keys), "open_titles": [k[1] for k in list(open_keys)[:10]]},
        )
        if verbose and pm_decision.get("pm_thesis"):
            print(f"  🧠 [Quant PM · {pm_decision.get('model_used')}]: {pm_decision['pm_thesis']}")
    except Exception as e:
        if verbose:
            print(f"  [Quant PM Note]: {e}")

    approved_set = set(pm_decision.get("approved_indices", range(len(all_opps))))
    mult_map = pm_decision.get("sizing_multipliers", {})
    rat_map = pm_decision.get("rationale_by_index", {})

    executed = []
    for idx, opp in enumerate(all_opps):
        key = (opp["strategy"], opp["event_title"])
        conv_mult = float(mult_map.get(str(idx), 1.0))
        pm_note = rat_map.get(str(idx), "")
        if verbose:
            label = (
                "UNDERROUND" if "underround" in opp["strategy"] else
                "OVERROUND" if "overround" in opp["strategy"] else
                "DOMINANCE" if "dominance" in opp["strategy"] else "SNIPE"
            )
            print(f"  🚨 {label}: {opp['event_title']} | Cost: ${opp['total_cost']:.3f} | Net Edge: +{opp['net_edge']*100:.1f}%")
            if pm_note:
                print(f"     [PM Thesis]: {pm_note}")

        if idx not in approved_set:
            if verbose:
                print("     [PM VETO]: Skipped by Quant PM due to oracle/resolution ambiguity or spread trap.")
            continue

        if execute and key not in open_keys and current_cash >= 25.0:
            # Dynamic Profit-Maximizing Sizing with High-Edge Snipe Reserve:
            # Reserve $100 cash exclusively for high-edge (>= 2.5%) dominance/oracle snipes;
            # deploy surplus cash dynamically into underrounds/overrounds scaled by edge & conviction.
            live_book_eq = 1000.0 + realized_pnl
            edge_val = float(opp.get("net_edge") or 0.01)
            is_high_edge = (edge_val >= 0.025) or ("snipe" in opp["strategy"]) or ("dominance" in opp["strategy"])
            snipe_reserve = 0.0 if is_high_edge else 100.0
            deployable_cash = max(0.0, current_cash - snipe_reserve)
            if deployable_cash < 25.0:
                continue

            if edge_val >= 0.03:
                base_pct = 0.24
            elif edge_val >= 0.015:
                base_pct = 0.18
            else:
                base_pct = 0.14  # ~$140 per 3-way maker underround vs old $50 flat cap

            venue_cap = float(opp.get("max_fillable_usd") or 250.0)
            target_ticket = live_book_eq * base_pct * max(0.5, min(1.5, conv_mult))
            max_ticket = min(venue_cap, deployable_cash, max(30.0, target_ticket))
            contracts = int(max_ticket / max(opp["total_cost"], 0.01))
            if contracts < 1:
                continue

            sum_cost = round(contracts * opp["total_cost"], 2)
            locked_pnl = round(contracts * opp["net_edge"], 2)
            now_str = datetime.now(PT).isoformat()

            with conn:
                conn.execute("""
                    INSERT INTO polymarket_standalone_trades (
                        timestamp, strategy, event_title, market_count,
                        basket_json, sum_ask, locked_profit, net_edge,
                        status, settled_at, realized_pnl
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, NULL)
                """, (
                    now_str, opp["strategy"], opp["event_title"], opp["market_count"],
                    json.dumps(opp["basket"]), opp["sum_ask"], locked_pnl, opp["net_edge"]
                ))
                current_cash = round(current_cash - sum_cost, 2)
                conn.execute(
                    "UPDATE polymarket_standalone_portfolio SET cash = ?, last_updated = ? WHERE id = 1",
                    (current_cash, now_str)
                )
                open_keys.add(key)
                executed.append({
                    "strategy": opp["strategy"],
                    "title": opp["event_title"],
                    "contracts": contracts,
                    "sum_cost": sum_cost,
                    "locked_pnl": locked_pnl
                })

    open_rows = conn.execute("SELECT locked_profit FROM polymarket_standalone_trades WHERE status = 'OPEN'").fetchall()
    open_locked = sum(r["locked_profit"] for r in open_rows)
    open_cost = max(0.0, 1000.0 + realized_pnl - current_cash)
    exp_eq = current_cash + open_cost + open_locked
    fill_note = f" · New fills: {len(executed)}" if executed else ""
    print(f"- **Track 2 (Polymarket Standalone):** `${exp_eq:.2f}` expected equity (`${current_cash:.2f}` cash · `{len(open_rows)}` open · `+${open_locked:.2f}` locked{fill_note})")
    return all_opps

if __name__ == "__main__":
    import sys
    run_polymarket_standalone_scan(execute=True, verbose=("--verbose" in sys.argv or "-v" in sys.argv))
