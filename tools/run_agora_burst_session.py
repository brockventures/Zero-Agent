#!/usr/bin/env python3
"""
run_agora_burst_session.py - Orchestrator for discrete Agora burst runs.

Runs agora_announcer.py in burst mode to broadcast round kickoff bells to Discord.
Bot trading agents (Zero, Amos, Aerial) participate dynamically on-demand in
response to round broadcasts and handoff envelopes. Monitored via detached_runner.py.
"""

import os
import sys
import time
import argparse
import subprocess
import json
import urllib.request

parser = argparse.ArgumentParser(description="Agora Burst Orchestrator")
parser.add_argument("--rounds", type=int, default=10, help="Number of burst rounds to run")
parser.add_argument("--interval", type=float, default=180.0, help="Seconds per round tick (default: 180s)")
parser.add_argument("--channel", type=str, default="1534436119888793750", help="Target Discord channel ID")
parser.add_argument("--codename", type=str, default="live-burst-10", help="Optional codename for burst")
args = parser.parse_args()

ROUNDS = args.rounds
INTERVAL = args.interval
CHANNEL = args.channel
CODENAME = args.codename

PT_NOW = time.strftime("%Y-%m-%d %H:%M:%S PT")

print(f"=== [Agora Burst Orchestrator] Session Launch at {PT_NOW} ===")
print(f"Targeting: {ROUNDS} rounds @ {INTERVAL}s tick interval (Channel: {CHANNEL}, Codename: {CODENAME})")
print("Mode: Pure dynamic on-the-fly play. Zero responds live to round bells and channel envelopes.")
sys.stdout.flush()

# Run agora_announcer burst runner
announcer_cmd = [
    sys.executable,
    "/workspace/tools/agora_announcer.py",
    "--burst", str(ROUNDS),
    "--interval", str(INTERVAL),
    "--channel", CHANNEL,
    "--codename", CODENAME
]

print(f"Launching burst announcer: {' '.join(announcer_cmd)}")
sys.stdout.flush()

announcer_proc = subprocess.Popen(
    announcer_cmd,
    cwd="/workspace",
    stdout=sys.stdout,
    stderr=sys.stderr
)
announcer_ret = announcer_proc.wait()

print(f"Announcer exit code: {announcer_ret}")
sys.stdout.flush()

# Fetch and display final state
print("")
print("=== Final Sol Standings & Balances ===")
try:
    with urllib.request.urlopen("https://agora.mikecarmody.net/referee/leaderboard", timeout=5) as resp:
        lb = json.loads(resp.read().decode())
        for idx, entry in enumerate(lb.get("leaderboard", []), 1):
            print(f"#{idx} {entry.get('agent_id').upper()}: Net Worth {entry.get('net_worth', 0):,} CR | Liquid: {entry.get('liquid', 0):,} CR")
except Exception as e:
    print(f"Could not fetch leaderboard: {e}")

try:
    with urllib.request.urlopen("https://agora.mikecarmody.net/stations/locations", timeout=5) as resp:
        locs = json.loads(resp.read().decode())
        print("")
        print("Fleet Locations:")
        for loc in locs.get("locations", []):
            st = loc.get("station_id") if loc.get("status") == "docked" else f"in transit -> {loc.get('transit', {}).get('destination', '?')}"
            print(f"• {loc.get('agent_id').upper()}: {loc.get('status')} ({st})")
except Exception as e:
    print(f"Could not fetch locations: {e}")

print("")
print(f"=== [Agora Burst Orchestrator] Session Concluded at {time.strftime('%Y-%m-%d %H:%M:%S PT')} ===")
sys.exit(announcer_ret)
