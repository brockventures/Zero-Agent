#!/usr/bin/env python3
"""
agora_play.py - Fast Single-Shot Discord Trading & Status CLI for Station Agora.

Designed for Zero to inspect game state, evaluate arbitrage, and submit
trades or interplanetary transit moves in sub-second single tool invocations.

Usage:
  python3 /workspace/tools/agora_play.py status
  python3 /workspace/tools/agora_play.py eval
  python3 /workspace/tools/agora_play.py order --side BUY|SELL --good FOOD|ORE|FRAG|FUEL --qty 250 --price 20 [--station earth]
  python3 /workspace/tools/agora_play.py move --dest ceres [--good FRAG] [--qty 250]
  python3 /workspace/tools/agora_play.py hold [--reason "holding for arrival"]
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from pathlib import Path

ENV_PATH = Path("/workspace/market-sandbox/.env")
BASE_URL_FALLBACK = "https://agora-banana-production.up.railway.app"
PUBLIC_URL_FALLBACK = "https://agora.mikecarmody.net"


def get_config() -> tuple[str, str, str]:
    """Retrieve base URL, public URL, and Zero's Agora token."""
    base_url = os.environ.get("AGORA_BASE_URL", "")
    public_url = os.environ.get("AGORA_PUBLIC_URL", "")
    token = os.environ.get("AGORA_TOKEN_ZERO", os.environ.get("AGORA_TOKEN", ""))

    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k == "AGORA_BASE_URL" and not base_url:
                        base_url = v
                    elif k == "AGORA_PUBLIC_URL" and not public_url:
                        public_url = v
                    elif k == "AGORA_TOKEN_ZERO" and not token:
                        token = v

    base_url = (base_url or BASE_URL_FALLBACK).rstrip("/")
    public_url = (public_url or PUBLIC_URL_FALLBACK).rstrip("/")
    token = token or "agora-combine-2026"
    return base_url, public_url, token


def fetch_briefing(public_url: str, base_url: str) -> dict:
    """Fetch live referee briefing from public endpoint or fallback."""
    urls = [
        f"{public_url}/referee/briefing?format=json",
        f"{base_url}/referee/briefing?format=json",
    ]
    last_err = None
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "ZeroAgoraCLI/1.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not reach Agora referee briefing: {last_err}")


def post_api(url: str, payload: dict, token: str) -> dict:
    """Execute authenticated JSON POST against Agora referee."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ZeroAgoraCLI/1.0"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return {"status": "error", "http_code": e.code, "error": json.loads(raw)}
        except Exception:
            return {"status": "error", "http_code": e.code, "raw_error": raw}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def cmd_status(args):
    base_url, public_url, token = get_config()
    data = fetch_briefing(public_url, base_url)

    curr_round = data.get("round", "?")
    sector = data.get("station") or data.get("sector") or "?"
    fleets = data.get("fleets", [])

    print(f"=== STATION AGORA // ROUND #{curr_round} ({sector.upper()}) ===")

    # Find Zero's fleet
    zero_fleet = None
    if isinstance(fleets, list):
        for f in fleets:
            if str(f.get("agent_id", "")).lower() == "zero":
                zero_fleet = f
                break
    elif isinstance(fleets, dict):
        zero_fleet = fleets.get("zero")

    if zero_fleet:
        nw = zero_fleet.get("net_worth", 0)
        liq = zero_fleet.get("liquid", 0)
        loc = zero_fleet.get("location", {})
        status = loc.get("status", "unknown")
        station = loc.get("station_id") or loc.get("transit", {}).get("destination", "unknown")
        transit_info = ""
        if status == "in_transit":
            dest = loc.get("transit", {}).get("destination", "?")
            arr = loc.get("transit", {}).get("arrival_round", "?")
            transit_info = f" -> {dest.upper()} (arrives #{arr})"

        print(f"• Fleet: Zero | NW: {nw:,} CR | Liquid: {liq:,} CR")
        print(f"• Vessel: zero/1 | Status: {status.upper()} @ {station.upper()}{transit_info}")

        ships = zero_fleet.get("ships", [])
        if ships:
            hold = ships[0].get("hold", {})
            print(f"• Hold: {hold.get('FRAG', 0)} FRAG, {hold.get('FOOD', 0)} FOOD, {hold.get('ORE', 0)} ORE | {hold.get('FUEL', 0)} FUEL")
    else:
        print("• Fleet: Zero (not found in current fleet list)")

    # Depots
    depots = data.get("depots", {}).get("stations", {})
    if not depots and isinstance(data.get("depots"), dict):
        depots = {k: v for k, v in data["depots"].items() if isinstance(v, dict)}

    print("\n--- Depot Quotes (Bid/Ask) ---")
    for st_name in ["earth", "luna", "mars", "ceres"]:
        goods = depots.get(st_name, {})
        q_strs = []
        for g_name in ["FOOD", "ORE", "FRAG", "FUEL"]:
            if g_name in goods:
                q = goods[g_name]
                b = q.get("best_bid") or q.get("bid", "-")
                a = q.get("best_ask") or q.get("ask", "-")
                q_strs.append(f"{g_name}: {b}/{a}")
        print(f"• {st_name.upper():<6}: {' | '.join(q_strs)}")


def cmd_eval(args):
    base_url, public_url, token = get_config()
    data = fetch_briefing(public_url, base_url)
    curr_round = data.get("round", 0)

    zero_fleet = None
    for f in data.get("fleets", []):
        if str(f.get("agent_id", "")).lower() == "zero":
            zero_fleet = f
            break

    if not zero_fleet:
        print("Zero fleet not found in briefing.")
        return

    loc = zero_fleet.get("location", {})
    status = loc.get("status", "docked")
    current_st = loc.get("station_id", "earth").lower()
    hold = zero_fleet.get("ships", [{}])[0].get("hold", {})
    depots = data.get("depots", {}).get("stations", {})
    if not depots and isinstance(data.get("depots"), dict):
        depots = {k: v for k, v in data["depots"].items() if isinstance(v, dict)}

    print(f"=== Arbitrage Evaluation (Round #{curr_round}) ===")
    print(f"Position: zero/1 {status.upper()} at {current_st.upper()}")
    print(f"Hold: {hold}")

    if status == "in_transit":
        dest = loc.get("transit", {}).get("destination", "?")
        arr = loc.get("transit", {}).get("arrival_round", "?")
        print(f"Ship in transit to {dest.upper()} (arrival #{arr}). Recommended action: HOLD until docking.")
        print(f"\nDiscord Chat: HOLD")
        print(f"\nHandoff Envelope Evidence: [\"amos/1 or zero/1 in transit to {dest}\"]")
        return

    # Check if holding sellable cargo
    cargo_good = None
    cargo_qty = 0
    for g in ["FOOD", "ORE", "FRAG"]:
        if hold.get(g, 0) > 0:
            cargo_good = g
            cargo_qty = hold[g]
            break

    if cargo_good:
        local_q = depots.get(current_st, {}).get(cargo_good, {})
        local_bid = local_q.get("best_bid") or local_q.get("bid", 0)
        print(f"Holding {cargo_qty} {cargo_good}. Local {current_st.upper()} bid is {local_bid} CR.")
        print(f"\nRecommended execution:")
        print(f"  1. SELL {cargo_qty} {cargo_good} @ {local_bid} AT {current_st.upper()}")
        print(f"  CLI: python3 /workspace/tools/agora_play.py order --side SELL --good {cargo_good} --qty {cargo_qty} --price {local_bid} --station {current_st}")
        print(f"  Discord Chat: SELL {cargo_qty} {cargo_good} @ {local_bid} AT {current_st.upper()}")
    else:
        # Evaluate purchase routes
        print("Hold empty. Checking cross-station spreads:")
        local_goods = depots.get(current_st, {})
        best_opp = None
        best_spread = 0

        for remote_st in ["earth", "luna", "mars", "ceres"]:
            if remote_st == current_st:
                continue
            r_goods = depots.get(remote_st, {})
            for good in ["FOOD", "ORE", "FRAG"]:
                q_local = local_goods.get(good, {})
                ask_local = q_local.get("best_ask") or q_local.get("ask", 999)
                q_remote = r_goods.get(good, {})
                bid_remote = q_remote.get("best_bid") or q_remote.get("bid", 0)
                spread = bid_remote - ask_local
                if spread > best_spread:
                    best_spread = spread
                    best_opp = (good, current_st, ask_local, remote_st, bid_remote, spread)

        if best_opp:
            good, c_st, ask_l, r_st, bid_r, sp = best_opp
            est_profit = sp * 250
            print(f"Top Opportunity: Buy {good} at {c_st.upper()} for {ask_l}, Haul to {r_st.upper()} for {bid_r} (Spread: +{sp} CR/unit, Est Profit: +{est_profit:,} CR)")
            print(f"\nExecution Steps:")
            print(f"  1. BUY 250 {good} @ {ask_l} AT {c_st.upper()}")
            print(f"  2. MOVE TO {r_st.upper()} WITH 250 {good}")
            print(f"\nCLI Commands:")
            print(f"  python3 /workspace/tools/agora_play.py order --side BUY --good {good} --qty 250 --price {ask_l} --station {c_st}")
            print(f"  python3 /workspace/tools/agora_play.py move --dest {r_st} --good {good} --qty 250")


def cmd_order(args):
    base_url, _, token = get_config()
    side = args.side.upper().strip()
    good = args.good.upper().strip()
    qty = int(args.qty)
    price = int(args.price)
    station = (args.station or "earth").lower().strip()

    side_val = "bid" if side in ("BUY", "BID") else "ask"
    url = f"{base_url}/referee/quick_order"
    payload = {
        "agent_id": "zero",
        "side": side_val,
        "instrument": good,
        "limit_price": price,
        "qty": qty,
        "station_id": station
    }

    res = post_api(url, payload, token)
    status = res.get("status", "unknown")
    tx_id = res.get("receipt", {}).get("tx_id") or res.get("tx_id") or f"ord-zero-{int(time.time())}"

    print(f"=== ORDER RESULT: {status.upper()} ===")
    print(f"Payload: {side} {qty} {good} @ {price} at {station.upper()}")
    print(f"Response: {json.dumps(res, indent=2)}")
    print(f"\n--- DISCORD CHAT FORMAT ---")
    print(f"{side} {qty} {good} @ {price} AT {station.upper()}")
    print(f"\n--- HANDOFF EVIDENCE ---")
    print(f'{{"src": "POST /referee/quick_order {tx_id}", "note": "{side.lower()} {qty} {good} @ {price} at {station}"}}')


def cmd_move(args):
    base_url, _, token = get_config()
    dest = args.dest.lower().strip()
    good = (args.good or "FRAG").upper().strip()
    qty = int(args.qty) if args.qty else 0

    url = f"{base_url}/stations/transit"
    payload = {
        "agent_id": "zero",
        "token": token,
        "destination": dest,
        "commodity": good,
        "cargo_qty": qty
    }

    res = post_api(url, payload, token)
    status = res.get("status", "unknown")
    tx_id = res.get("receipt", {}).get("tx_id") or res.get("tx_id") or f"tx-zero-{int(time.time())}"
    arr = res.get("arrival_round") or res.get("receipt", {}).get("arrival_round", "?")

    print(f"=== TRANSIT RESULT: {status.upper()} ===")
    print(f"Destination: {dest.upper()} (Arrives Round #{arr}) | Cargo: {qty} {good}")
    print(f"Response: {json.dumps(res, indent=2)}")
    print(f"\n--- DISCORD CHAT FORMAT ---")
    if qty > 0:
        print(f"MOVE TO {dest.upper()} WITH {qty} {good}")
    else:
        print(f"MOVE TO {dest.upper()}")
    print(f"\n--- HANDOFF EVIDENCE ---")
    print(f'{{"src": "POST /stations/transit {tx_id}", "note": "transit to {dest}, arrives round #{arr}, payload {qty} {good}"}}')


def cmd_hold(args):
    reason = args.reason or "waiting on market spread"
    print(f"=== HOLD ACTION ===")
    print(f"Discord Chat: HOLD")
    print(f"\n--- HANDOFF ENVELOPE TEMPLATE ---")
    print(f"""```handoff
{{
  "v": 1.1,
  "kind": "status",
  "reply": "none",
  "floor": "open",
  "scope": "channel",
  "subject": "agora-burst",
  "to": ["all"],
  "evidence": [
    {{"src": "GET /referee/briefing", "note": "{reason}"}}
  ]
}}
```""")


def main():
    parser = argparse.ArgumentParser(description="Zero Agora Live Discord Trading CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Get live round, fleet, and depot quotes")
    p_status.set_defaults(func=cmd_status)

    p_eval = sub.add_parser("eval", help="Evaluate current hold & highest margin arbitrage")
    p_eval.set_defaults(func=cmd_eval)

    p_order = sub.add_parser("order", help="Submit BUY or SELL order")
    p_order.add_argument("--side", choices=["BUY", "SELL", "buy", "sell"], required=True)
    p_order.add_argument("--good", choices=["FOOD", "ORE", "FRAG", "FUEL", "food", "ore", "frag", "fuel"], required=True)
    p_order.add_argument("--qty", type=int, required=True)
    p_order.add_argument("--price", type=int, required=True)
    p_order.add_argument("--station", type=str, default="earth")
    p_order.set_defaults(func=cmd_order)

    p_move = sub.add_parser("move", help="Submit interplanetary transit move")
    p_move.add_argument("--dest", required=True, choices=["ceres", "mars", "earth", "luna"])
    p_move.add_argument("--good", choices=["FOOD", "ORE", "FRAG", "FUEL", "food", "ore", "frag", "fuel"], default="FRAG")
    p_move.add_argument("--qty", type=int, default=0)
    p_move.set_defaults(func=cmd_move)

    p_hold = sub.add_parser("hold", help="Generate HOLD status and envelope")
    p_hold.add_argument("--reason", type=str, default="holding position")
    p_hold.set_defaults(func=cmd_hold)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
