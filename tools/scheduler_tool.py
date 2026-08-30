#!/usr/bin/env python3
"""Karakos-Style Persistent Sidecar Scheduler for Ivy-AG.

Maintains /workspace/data/schedule.json:
- Durable across container restarts and crashes.
- Catches up missed jobs automatically on boot.
- Supports dynamic inspection and modification without restarts.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
SCHEDULE_FILE = DATA_DIR / "schedule.json"

log = logging.getLogger("scheduler_tool")

DEFAULT_JOBS = [
    {
        "id": "heartbeat_sweep",
        "name": "Heartbeat Sweep",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 7200,
        "prompt": "Run the infrastructure heartbeat check using /workspace/tools/sidecars.py heartbeat. Post only if infrastructure is degraded (silent when healthy).",
        "catchup_if_missed": False
    },
    {
        "id": "ev9_monitor",
        "name": "EV9 Listing Monitor",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 8,
        "minute_pt": 15,
        "prompt": "Run the Kia EV9 listing monitor capture using /workspace/tools/sidecars.py ev9. Post the weekly digest if Sunday.",
        "catchup_if_missed": False
    },
    {
        "id": "dated_reminders",
        "name": "Dated Reminders",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 9,
        "minute_pt": 0,
        "prompt": "Check dated one-shot reminders using /workspace/tools/sidecars.py reminders. Post only if a reminder has come due.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 14400
    },
    {
        "id": "nightly_triage",
        "name": "Nightly Triage & Briefing",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 23,
        "minute_pt": 30,
        "prompt": "Run the nightly agenda & inbox triage briefing using /workspace/tools/sidecars.py triage. Present tomorrow's calendar agenda and priority unread emails.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 7200
    },
    {
        "id": "nas_logs",
        "name": "NAS Log Review",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 22,
        "minute_pt": 0,
        "prompt": "Run the nightly NAS log review using /workspace/tools/sidecars.py nas_logs. Review container errors across Host1 (.82) and Host2 (.84) and report cleanly.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 7200
    },
    {
        "id": "dreaming",
        "name": "Dreaming Memory Consolidation",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 1,
        "minute_pt": 45,
        "prompt": "Run the nightly memory consolidation pass using /workspace/tools/sidecars.py dream. Consolidate new learnings into durable memory.",
        "catchup_if_missed": False
    },
    {
        "id": "session_rollover",
        "name": "Daily Session Rollover",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 2,
        "minute_pt": 0,
        "prompt": "[INTERNAL_SESSION_ROLLOVER]",
        "catchup_if_missed": False
    },
    {
        "id": "plex_cleanup",
        "name": "Plex Transcode Cleanup",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 3,
        "minute_pt": 0,
        "prompt": "Run the Plex transcode cache cleanup using /workspace/tools/sidecars.py plex. Prune stale sessions if streams are idle.",
        "catchup_if_missed": False
    },
    {
        "id": "memory_doctor",
        "name": "Memory Doctor Audit",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 6,  # Sunday
        "hour_pt": 3,
        "minute_pt": 0,
        "prompt": "Run the weekly memory store audit using /workspace/tools/sidecars.py doctor.",
        "catchup_if_missed": False
    },
    {
        "id": "marketing_sweep",
        "name": "Biweekly Marketing Sweep",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 6,  # Sunday
        "hour_pt": 22,
        "minute_pt": 35,
        "prompt": "Run the promotional email marketing sweep using /workspace/tools/sidecars.py marketing.",
        "catchup_if_missed": False
    }
]

def calculate_next_run(job: dict, from_ts: float | None = None) -> float:
    """Compute the next Unix timestamp for a job."""
    now_pt = datetime.fromtimestamp(from_ts or time.time(), tz=PT)
    stype = job.get("schedule_type", "daily")

    if stype == "interval":
        interval = job.get("interval_seconds", 7200)
        last = job.get("last_run_ts")
        if last:
            return last + interval
        return now_pt.timestamp() + interval

    elif stype == "daily":
        h = job.get("hour_pt", 0)
        m = job.get("minute_pt", 0)
        target = now_pt.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now_pt:
            target += timedelta(days=1)
        return target.timestamp()

    elif stype == "weekly":
        dow = job.get("day_of_week", 6)
        h = job.get("hour_pt", 0)
        m = job.get("minute_pt", 0)
        days_ahead = (dow - now_pt.weekday()) % 7
        target = now_pt.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=days_ahead)
        if target <= now_pt:
            target += timedelta(days=7)
        return target.timestamp()

    elif stype == "one_shot":
        return job.get("target_ts", now_pt.timestamp())

    return now_pt.timestamp() + 3600

def load_schedule() -> list[dict]:
    """Load schedule.json, seeding defaults if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SCHEDULE_FILE.exists():
        save_schedule(DEFAULT_JOBS)
        return DEFAULT_JOBS

    try:
        with open(SCHEDULE_FILE, "r") as f:
            jobs = json.load(f)
            # Ensure any new defaults exist
            existing_ids = {j["id"] for j in jobs}
            updated = False
            for d in DEFAULT_JOBS:
                if d["id"] not in existing_ids:
                    jobs.append(d)
                    updated = True
            if updated:
                save_schedule(jobs)
            return jobs
    except Exception as e:
        log.error("Failed reading %s: %s — using defaults", SCHEDULE_FILE, e)
        return DEFAULT_JOBS

def save_schedule(jobs: list[dict]):
    """Persist schedule safely with an atomic write."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(jobs, f, indent=2)
    tmp.replace(SCHEDULE_FILE)

def format_schedule_summary() -> str:
    """Format upcoming schedule in clean Option 3 inline bullets."""
    jobs = load_schedule()
    now_ts = time.time()
    now_pt = datetime.fromtimestamp(now_ts, tz=PT)

    # Sort jobs by next_run_ts
    for j in jobs:
        if not j.get("next_run_ts") or j["next_run_ts"] < now_ts:
            j["next_run_ts"] = calculate_next_run(j)
    save_schedule(jobs)

    sorted_jobs = sorted(jobs, key=lambda x: x.get("next_run_ts", float("inf")))

    out = [f"📅 **Karakos Sidecar Schedule** — Current Time: `{now_pt.strftime('%I:%M %p PT')}`\n"]
    for j in sorted_jobs:
        status = "🟢 Active" if j.get("enabled", True) else "⏸️ Paused"
        next_dt = datetime.fromtimestamp(j["next_run_ts"], tz=PT)
        time_diff = next_dt - now_pt
        hours, remainder = divmod(int(time_diff.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        rel = f"in {hours}h {minutes}m" if hours > 0 else f"in {minutes}m"
        time_str = next_dt.strftime("%a %b %-d at %I:%M %p PT")
        
        out.append(f"• **{j['name']}** ({status}): Next run `{time_str}` ({rel})")

    return "\n".join(out)

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if action == "summary":
        print(format_schedule_summary())
    else:
        print(json.dumps(load_schedule(), indent=2))
