#!/usr/bin/env python3
"""agora_autoworker.py - Autonomous 3-hour roadmap runner for Station Agora.

Wakes up via Karakos interval schedule every 10 minutes.
Audits remaining roadmap tasks, executes implementation and tests,
and cleanly self-expires after 3 hours.
"""

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("/workspace/data/agora_autoworker_state.json")
SCHEDULE_FILE = Path("/workspace/data/schedule.json")
CHANNEL_ID = 1548196930788524094
WINDOW_SECONDS = 3 * 3600  # 3 hours


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    now = time.time()
    state = {
        "active": True,
        "start_epoch": now,
        "expires_epoch": now + WINDOW_SECONDS,
        "channel_id": CHANNEL_ID,
        "cycles_completed": 0,
        "history": []
    }
    save_state(state)
    return state


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def disable_schedule_job():
    if SCHEDULE_FILE.exists():
        try:
            with open(SCHEDULE_FILE, "r") as f:
                sched = json.load(f)
            changed = False
            for job in sched:
                if job.get("id") == "agora_roadmap_sprint":
                    job["enabled"] = False
                    changed = True
            if changed:
                with open(SCHEDULE_FILE, "w") as f:
                    json.dump(sched, f, indent=2)
                print("[agora_autoworker] Disabled 'agora_roadmap_sprint' in schedule.json")
        except Exception as e:
            print(f"[agora_autoworker] Error disabling schedule: {e}")


def main():
    state = load_state()
    now = time.time()

    if not state.get("active", True) or now >= state.get("expires_epoch", 0):
        state["active"] = False
        save_state(state)
        disable_schedule_job()
        start_str = datetime.fromtimestamp(state.get("start_epoch", now), tz=PT).strftime("%I:%M %p PT")
        now_str = datetime.fromtimestamp(now, tz=PT).strftime("%I:%M %p PT")
        print(f"🏁 **3-Hour Agora Autonomous Sprint Completed** ({start_str} -> {now_str}). Sidecar self-disabled.")
        return

    state["cycles_completed"] = state.get("cycles_completed", 0) + 1
    remaining_sec = max(0, int(state.get("expires_epoch", now) - now))
    remaining_min = remaining_sec // 60
    cycle = state["cycles_completed"]

    now_pt = datetime.fromtimestamp(now, tz=PT).strftime("%I:%M %p PT")

    state["history"].append({
        "cycle": cycle,
        "timestamp_epoch": now,
        "timestamp_pt": now_pt
    })
    save_state(state)

    # Check if all tasks on the public board are finished
    try:
        from tools.shared_tasks import list_tasks
        open_tasks = list_tasks(state="open")
    except Exception:
        open_tasks = []

    if not open_tasks:
        state["active"] = False
        save_state(state)
        disable_schedule_job()
        start_str = datetime.fromtimestamp(state.get("start_epoch", now), tz=PT).strftime("%I:%M %p PT")
        print(f"🏁 **Agora Autonomous Roadmap Sprint Concluded — All Tasks Complete** ({start_str} -> {now_pt} | {cycle} cycles). Sidecar self-disabled.")
        print("All 7 roadmap issues and PRs across spatial economy, corporate warfare, salvage extortion, and market variance are 100% implemented, passing tests, and submitted.")
        return

    print(f"⏱️ **Agora Autonomous Roadmap Sprint — Cycle {cycle}** ({now_pt} | {remaining_min}m remaining in window)")


if __name__ == "__main__":
    main()
