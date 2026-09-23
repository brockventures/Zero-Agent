#!/usr/bin/env python3
"""
run_agora_burst_session.py - Orchestrator for discrete Agora burst runs.

Spawns Zero's autonomous trader client (trader_client.py --poll) and the
agora_announcer.py burst runner in lockstep. Monitored via detached_runner.py.
"""

import os
import sys
import time
import subprocess
import json
import urllib.request
from pathlib import Path

PT_NOW = time.strftime("%Y-%m-%d %H:%M:%S PT")
LOG_DIR = Path("/workspace/data")
TRADER_LOG = LOG_DIR / "trader_burst_session.log"
ROUNDS = 8
INTERVAL = 180.0

print(f"=== [Agora Burst Orchestrator] Session Launch at {PT_NOW} ===")
print(f"Targeting: {ROUNDS} rounds @ {INTERVAL}s tick interval")
sys.stdout.flush()

# 1. Spawn Zero trader client
trader_env = os.environ.copy()
trader_cmd = [
    sys.executable,
    "/workspace/market-sandbox/tools/trader_client.py",
    "--poll",
    "--max-rounds", str(ROUNDS),
    "--poll-interval", "2.0"
]

print(f"Starting Zero trader client: {' '.join(trader_cmd)}")
sys.stdout.flush()
with open(TRADER_LOG, "w") as tl:
    trader_proc = subprocess.Popen(
        trader_cmd,
        stdout=tl,
        stderr=subprocess.STDOUT,
        cwd="/workspace/market-sandbox",
        env=trader_env
    )

print(f"Trader client PID: {trader_proc.pid}. Waiting 2s for initialization...")
time.sleep(2.0)
sys.stdout.flush()

# 2. Run agora_announcer burst runner
announcer_cmd = [
    sys.executable,
    "/workspace/tools/agora_announcer.py",
    "--burst", str(ROUNDS),
    "--interval", str(INTERVAL),
    "--channel", "1534436119888793750",
    "--codename", "transit-trial"
]

print(f"Launching burst runner: {' '.join(announcer_cmd)}")
sys.stdout.flush()

announcer_res = subprocess.run(
    announcer_cmd,
    cwd="/workspace",
    capture_output=True,
    text=True
)

print(f"Announcer exit code: {announcer_res.returncode}")
if announcer_res.stdout:
    print("Announcer Output:")
    print(announcer_res.stdout)
if announcer_res.stderr:
    print("Announcer Stderr:")
    print(announcer_res.stderr)
sys.stdout.flush()

# 3. Wait for trader client to conclude cleanly
print("Waiting for trader client to finish final round and order cleanup...")
try:
    trader_exit = trader_proc.wait(timeout=30)
    print(f"Trader client terminated cleanly with exit code: {trader_exit}")
except subprocess.TimeoutExpired:
    print("Warning: Trader client timed out waiting for exit. Sending SIGTERM...")
    trader_proc.terminate()
    trader_proc.wait(timeout=5)

# 4. Fetch and display final state
print("\n=== Final Sol Standings & Balances ===")
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
        print("\nFleet Locations:")
        for loc in locs.get("locations", []):
            st = loc.get("station_id") if loc.get("status") == "docked" else f"in transit -> {loc.get('transit', {}).get('destination', '?')}"
            print(f"• {loc.get('agent_id').upper()}: {loc.get('status')} ({st})")
except Exception as e:
    print(f"Could not fetch locations: {e}")

print("\n=== Trader Client Log Excerpt ===")
if TRADER_LOG.exists():
    with open(TRADER_LOG) as tl:
        lines = tl.readlines()
        print("".join(lines[-20:]))

print(f"\n=== [Agora Burst Orchestrator] Session Concluded at {time.strftime('%Y-%m-%d %H:%M:%S PT')} ===")
sys.exit(0 if announcer_res.returncode == 0 else 1)
