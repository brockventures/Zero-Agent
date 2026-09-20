#!/usr/bin/env python3
"""
tools/agora_announcer.py - Station Agora 5-minute round announcer bot.

Presides over Station Agora exchange floor. Wakes trading bots (@robot)
every 5 minutes with live market depth, mark price, and standings.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

DEFAULT_CHANNEL_ID = "1534436119888793750"  # #the-banana-stand
DEFAULT_ROBOT_ROLE_ID = "1543462881624858624"  # @Robot
REFEREE_BASE_URL = os.environ.get("AGORA_BASE_URL", "https://agora.mikecarmody.net")

FLEET_NAMES = {
    "amos": "Atlantean Paperclip Manufacturing",
    "marvin": "Ballistic Liquidation Co.",
    "zero": "Apex Vector Arbitrage",
    "aerial": "Zenith Drift Overwatch",
}


def get_bot_token() -> str:
    """Retrieve Agora Trade Terminal bot token from env, data, or secrets."""
    token = os.environ.get("AGORA_TERMINAL_BOT_TOKEN")
    if token:
        return token
    for p in ("/workspace/data/agora_token.json", "/secrets/env.json"):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tok = data.get("AGORA_TERMINAL_BOT_TOKEN", "")
                    if tok:
                        return tok
            except Exception:
                pass
    return ""


def fetch_json(endpoint: str) -> dict:
    """Fetch JSON from referee REST API."""
    url = f"{REFEREE_BASE_URL.rstrip('/')}{endpoint}"
    req = urllib.request.Request(url, headers={"User-Agent": "AgoraAnnouncer/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_announcement(round_num: int = 1) -> str:
    """Compile formatted Strategy Window checkpoint."""
    health = fetch_json("/referee/health")
    leaderboard = fetch_json("/referee/leaderboard")
    book = fetch_json("/referee/book")

    seq = health.get("seq", 0)
    floor = health.get("floor", "open")

    # Compute mark price and spread from book
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    best_bid = max([b.get("limit_price", 0) for b in bids], default=0)
    best_ask = min([a.get("limit_price", 999999) for a in asks], default=0)

    spread_str = f"{best_ask - best_bid} CR" if (best_bid and best_ask < 999999) else "N/A"
    mark_price = 28  # default base mark
    if leaderboard.get("leaderboard"):
        mark_price = leaderboard["leaderboard"][0].get("mark_price", 28)

    # Standings
    standings_parts = []
    lb_entries = leaderboard.get("leaderboard", [])
    for idx, entry in enumerate(lb_entries, 1):
        agent_id = entry.get("agent_id", "unknown")
        nw = entry.get("net_worth", 0)
        fleet = FLEET_NAMES.get(agent_id.lower())
        display_name = f"{fleet} [{agent_id.upper()}]" if fleet else agent_id.upper()
        standings_parts.append(f"#{idx} {display_name} ({nw:,} CR)")

    standings_line = " | ".join(standings_parts) if standings_parts else "No active balances"

    msg = (
        f"🔔 **Station Agora // Round {round_num} Strategy Window** (<@&{DEFAULT_ROBOT_ROLE_ID}>)\n"
        f"```text\n"
        f"STATUS: FLOOR {floor.upper()} | SEQ: #{seq} | MARK: {mark_price} CR | SPREAD: {spread_str}\n"
        f"Standings: {standings_line}\n"
        f"```\n"
        f"*Evaluate market parameters, sync `strategy_config.json`, and state your thesis.*"
    )
    return msg


def post_discord(channel_id: str, content: str, token: str) -> bool:
    """Post message directly via Discord REST API."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/brockventures/market-sandbox, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        print(f"Discord API Error ({e.code}): {err_body}", file=sys.stderr)
        return False


def build_final_bell() -> str:
    """Compile formatted final settlement announcement."""
    try:
        leaderboard = fetch_json("/referee/leaderboard")
        standings_parts = []
        lb_entries = leaderboard.get("leaderboard", [])
        for idx, entry in enumerate(lb_entries, 1):
            agent_id = entry.get("agent_id", "unknown")
            nw = entry.get("net_worth", 0)
            fleet = FLEET_NAMES.get(agent_id.lower())
            display_name = f"{fleet} [{agent_id.upper()}]" if fleet else agent_id.upper()
            standings_parts.append(f"#{idx} {display_name} ({nw:,} CR)")
        standings_line = " | ".join(standings_parts) if standings_parts else "No active balances"
    except Exception as e:
        standings_line = f"Telemetry fetch error: {e}"

    return (
        f"🏁 **Station Agora // 15-Minute Combine Concluded** (<@&{DEFAULT_ROBOT_ROLE_ID}>)\n"
        f"```text\n"
        f"STATUS: WINDOW COMPLETE | FINAL STANDINGS:\n"
        f"{standings_line}\n"
        f"```\n"
        f"*Combine session concluded. Orderbooks settling.*"
    )


def main():
    parser = argparse.ArgumentParser(description="Agora Trade Terminal Round Announcer")
    parser.add_argument("--dry-run", action="store_true", help="Print announcement without posting to Discord")
    parser.add_argument("--once", action="store_true", help="Post single announcement immediately")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL_ID, help="Target Discord channel ID")
    parser.add_argument("--round", type=int, default=1, help="Starting round number")
    parser.add_argument("--rounds", type=int, default=0, help="Total rounds to execute before final bell (0 for infinite)")
    parser.add_argument("--interval", type=int, default=300, help="Loop interval in seconds (default: 300s / 5m)")
    args = parser.parse_args()

    content = build_announcement(round_num=args.round)

    if args.dry_run:
        print("=== DRY RUN ANNOUNCEMENT ===")
        print(content)
        if args.rounds > 0:
            print("\n=== DRY RUN FINAL BELL ===")
            print(build_final_bell())
        return 0

    token = get_bot_token()
    if not token:
        print("Error: AGORA_TERMINAL_BOT_TOKEN not found in env or /secrets/env.json", file=sys.stderr)
        return 1

    if args.once:
        ok = post_discord(args.channel, content, token)
        if ok:
            print(f"Successfully posted Round {args.round} bell to channel {args.channel}")
            return 0
        return 1

    print(f"Starting Agora Round Bell loop (rounds: {args.rounds or 'infinite'}, interval: {args.interval}s)...")
    cur_round = args.round
    rounds_completed = 0
    while True:
        try:
            msg = build_announcement(round_num=cur_round)
            post_discord(args.channel, msg, token)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Broadcasted Round {cur_round}")
            rounds_completed += 1
            if args.rounds > 0 and rounds_completed >= args.rounds:
                print(f"Completed {rounds_completed} rounds. Waiting {args.interval}s for round completion before final bell...")
                time.sleep(args.interval)
                final_msg = build_final_bell()
                post_discord(args.channel, final_msg, token)
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Broadcasted Final Bell")
                break
            cur_round += 1
        except Exception as e:
            print(f"Loop error: {e}", file=sys.stderr)
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
