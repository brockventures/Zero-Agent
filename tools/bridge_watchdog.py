#!/usr/bin/env python3
"""
bridge_watchdog.py — Self-Healing Bridge & Gateway Liveness Watchdog for Zero.

Runs periodically (every 5m) or on-demand to monitor:
1. Discord Gateway Heartbeat freshness in liveness_beacon.json (threshold: 180s).
2. Stuck in-flight turns (state: PROCESSING > 600s with dead/wedged process tree).
3. Stale or orphaned agy CLI processes holding slots (> 600s).
4. Automated Self-Healing: Automatically touches /workspace/data/reload_bridge.flag
   to execute an in-place reload if a wedged state is detected.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from tools.process_probe import reap_stale_agy_processes, diagnose_process_tree
from tools.bridge_state import DATA_DIR, BEACON_FILE, SESSIONS_FILE

PT_TZ = ZoneInfo("America/Los_Angeles")
LOG_FILE = DATA_DIR / "bridge_watchdog.log"
RELOAD_FLAG = DATA_DIR / "reload_bridge.flag"
MAX_HEARTBEAT_AGE = 180.0  # 3 minutes
MAX_PROCESSING_SILENCE = 600.0  # 10 minutes


def log(msg: str):
    ts = datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def check_bridge_health(auto_heal: bool = True) -> tuple[bool, str, dict]:
    """Inspect bridge gateway, turn state, and background agy subprocesses."""
    now = time.time()
    issues = []
    actions_taken = []
    details = {
        "timestamp": now,
        "timestamp_pt": datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT"),
        "gateway_healthy": True,
        "beacon_healthy": True,
        "reaped_processes": [],
        "self_healed": False
    }

    # 1. Inspect dynamic Discord Gateway Heartbeat in liveness_beacon.json
    beacon_data = {}
    if BEACON_FILE.exists():
        try:
            with open(BEACON_FILE) as f:
                beacon_data = json.load(f)
        except Exception as e:
            issues.append(f"Corrupt liveness_beacon.json: {e}")

    gw_heartbeat = beacon_data.get("gateway_heartbeat")
    gw_status = beacon_data.get("gateway_status", "unknown")
    if gw_heartbeat:
        gw_age = now - float(gw_heartbeat)
        details["gateway_heartbeat_age_seconds"] = int(gw_age)
        details["gateway_status"] = gw_status
        if gw_age > MAX_HEARTBEAT_AGE:
            issues.append(f"Discord gateway heartbeat stale ({int(gw_age)}s > {int(MAX_HEARTBEAT_AGE)}s)")
            details["gateway_healthy"] = False
        elif gw_status != "connected":
            issues.append(f"Discord gateway reported state '{gw_status}'")
            details["gateway_healthy"] = False
    else:
        issues.append("Missing gateway_heartbeat in liveness_beacon.json")
        details["gateway_healthy"] = False

    # 2. Check for wedged PROCESSING turn state
    turn_state = beacon_data.get("state", "IDLE")
    turn_ts = beacon_data.get("ts", 0)
    if turn_state == "PROCESSING" and turn_ts > 0:
        turn_silence = now - float(turn_ts)
        details["turn_silence_seconds"] = int(turn_silence)
        if turn_silence > MAX_PROCESSING_SILENCE:
            issues.append(f"Turn state held in PROCESSING for {int(turn_silence)}s (> {int(MAX_PROCESSING_SILENCE)}s)")
            details["beacon_healthy"] = False

    # 3. Reap any stale / orphaned agy CLI processes
    try:
        reaped = reap_stale_agy_processes(max_age_seconds=600.0, dry_run=not auto_heal)
        if reaped:
            details["reaped_processes"] = reaped
            actions_taken.append(f"Reaped {len(reaped)} stale/wedged agy CLI subprocess(es)")
    except Exception as re_err:
        issues.append(f"Process reaper check failed: {re_err}")

    # 4. Self-Healing Trigger
    is_healthy = len(issues) == 0
    if not is_healthy and auto_heal:
        log(f"🚨 Bridge Watchdog detected {len(issues)} issue(s): {'; '.join(issues)}. Arming self-healing reload flag...")
        try:
            RELOAD_FLAG.touch()
            actions_taken.append("Armed /workspace/data/reload_bridge.flag for in-place reload")
            details["self_healed"] = True
        except Exception as fe:
            issues.append(f"Failed to touch reload flag: {fe}")

    summary_parts = []
    if is_healthy:
        summary_parts.append("✅ Discord bridge & gateway healthy.")
        if gw_heartbeat:
            summary_parts.append(f"Gateway latency: {beacon_data.get('gateway_latency_ms', 0)}ms (heartbeat age: {int(now - float(gw_heartbeat))}s).")
    else:
        summary_parts.append(f"⚠️ **Bridge Degradation Detected ({len(issues)} issues)**:\n" + "\n".join(f"• {iss}" for iss in issues))
        if actions_taken:
            summary_parts.append("\n**Actions Taken:**\n" + "\n".join(f"• {act}" for act in actions_taken))

    return is_healthy, "\n".join(summary_parts), details


def main():
    auto_heal = "--no-heal" not in sys.argv
    healthy, summary, details = check_bridge_health(auto_heal=auto_heal)
    print(summary)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
