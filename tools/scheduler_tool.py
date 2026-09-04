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

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import tools.bridge_state as bs

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = bs.DATA_DIR
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
        "channel_id": 1544955535722545253,
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
    },
    {
        "id": "daily_birthday_reminder",
        "name": "Daily Birthday Reminder",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 7,
        "minute_pt": 0,
        "prompt": "Check for friend & family birthdays today using /workspace/tools/birthday_reminder.py --quiet. If someone has a birthday today, post the reminder with the interactive text button.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 14400
    },
    {
        "id": "weekly_social_last_seen_review",
        "name": "Weekly Social & Last Seen Review",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 6,  # Sunday
        "hour_pt": 20,
        "minute_pt": 30,
        "prompt": "Review the past week's social events, calendar, and text messages using /workspace/tools/social_last_seen_review.py --quiet. Post only if qualifying social events or interactions are identified, and ask for confirmation before updating Last Seen.",
        "catchup_if_missed": False
    },
    {
        "id": "monthly_core_friends_reconnect",
        "name": "Monthly Core Friends Social Planning Reminder",
        "enabled": True,
        "schedule_type": "monthly",
        "day_of_month": 1,
        "hour_pt": 9,
        "minute_pt": 0,
        "prompt": "Check for local Core friends we have not seen in at least 8 weeks using /workspace/tools/core_friends_reminder.py --quiet. Post the reminder to help plan social gatherings.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 86400
    },
    {
        "id": "morning_topic_rotation",
        "name": "Crab Cavern Morning Topic Rotation",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 9,
        "minute_pt": 30,
        "prompt": "Run the Crab Cavern morning rotation dispatcher using /workspace/tools/morning_dispatcher.py --dispatch.",
        "catchup_if_missed": False
    },
    {
        "id": "antigravity_check",
        "name": "Antigravity CLI Check",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 10,
        "minute_pt": 0,
        "prompt": "Check for Antigravity CLI updates using /workspace/tools/update_antigravity.py.",
        "catchup_if_missed": False
    },
    {
        "id": "ha_battery_check",
        "name": "Home Assistant IoT Battery Watchdog",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 0,  # Monday
        "hour_pt": 10,
        "minute_pt": 0,
        "prompt": "Run the Home Assistant IoT battery watchdog check using /workspace/tools/ha_battery_check.py.",
        "catchup_if_missed": False
    },
    {
        "id": "nas_storage_check",
        "name": "Synology Storage & Array Health Check",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 2,  # Wednesday
        "hour_pt": 10,
        "minute_pt": 0,
        "prompt": "Run the Synology storage & array health check using /workspace/tools/nas_storage_check.py.",
        "catchup_if_missed": False
    },
    {
        "id": "ha_update_check",
        "name": "Home Assistant Stable Update Check",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 4,  # Friday
        "hour_pt": 10,
        "minute_pt": 30,
        "prompt": "Run the Home Assistant stable update check using /workspace/tools/ha_update_check.py.",
        "catchup_if_missed": False
    },
    {
        "id": "dockhand_update",
        "name": "Dockhand Image Check",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 6,  # Sunday
        "hour_pt": 11,
        "minute_pt": 0,
        "prompt": "Run the Dockhand container image check using /workspace/tools/dockhand_update.py.",
        "catchup_if_missed": False
    },
    {
        "id": "weekly_proactive_digest",
        "name": "Option B Weekly Proactive Digest",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 6,  # Sunday
        "hour_pt": 8,
        "minute_pt": 0,
        "prompt": "Run the Option B weekly proactive digest using /workspace/tools/weekly_digest.py.",
        "catchup_if_missed": False
    },
    {
        "id": "plex_weekly_digest",
        "name": "Plex Weekly New Media Digest",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 6,  # Sunday
        "hour_pt": 9,
        "minute_pt": 0,
        "prompt": "Run the Plex weekly new media digest using /workspace/tools/plex_weekly_digest.py.",
        "catchup_if_missed": False
    },
    {
        "id": "daily_token_budget_report",
        "name": "Daily Token & AI Ultra Budget Report",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 23,
        "minute_pt": 59,
        "prompt": "Run the daily token & Google AI Ultra compute budget usage report using /workspace/tools/sidecars.py token_report.",
        "catchup_if_missed": False
    },
    {
        "id": "monthly_hardcode_regex_audit",
        "name": "Monthly Hardcoded Rule & Regex Audit",
        "enabled": True,
        "schedule_type": "monthly",
        "day_of_month": 1,
        "hour_pt": 4,
        "minute_pt": 30,
        "prompt": "Run the monthly hardcoded rule & regex audit using /workspace/tools/sidecars.py code_audit. Present findings and architectural recommendations for eliminating brittle heuristics.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 86400
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

    elif stype == "monthly":
        dom = job.get("day_of_month", 1)
        h = job.get("hour_pt", 9)
        m = job.get("minute_pt", 0)
        try:
            target = now_pt.replace(day=dom, hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            target = now_pt.replace(day=1, hour=h, minute=m, second=0, microsecond=0)
        if target <= now_pt:
            # Advance to 1st of next month
            new_year = now_pt.year + (1 if now_pt.month == 12 else 0)
            new_month = 1 if now_pt.month == 12 else now_pt.month + 1
            target = target.replace(year=new_year, month=new_month, day=dom)
        return target.timestamp()

    elif stype == "one_shot":
        return job.get("target_ts", now_pt.timestamp())

    return now_pt.timestamp() + 3600

SIDECAR_ALIASES = {
    "heartbeat_sweep": ["heartbeat", "heartbeat_sweep"],
    "nightly_triage": ["triage", "nightly_triage"],
    "nas_logs": ["nas_logs"],
    "dreaming": ["dream", "dreaming"],
    "session_rollover": ["session_rollover"],
    "plex_cleanup": ["plex", "plex_cleanup"],
    "dated_reminders": ["reminders", "dated_reminders"],
    "ev9_monitor": ["ev9", "ev9_monitor"],
    "marketing_sweep": ["marketing", "marketing_sweep"],
    "memory_doctor": ["doctor", "memory_doctor"],
    "daily_birthday_reminder": ["daily_birthday_reminder", "birthday_reminder", "birthdays"],
    "weekly_social_last_seen_review": ["weekly_social_review", "social_review", "weekly_social_last_seen_review"],
    "monthly_core_friends_reconnect": ["monthly_core_friends_reminder", "core_friends", "monthly_core_friends_reconnect"],
    "morning_topic_rotation": ["morning_topic_rotation", "morning_dispatcher"],
    "antigravity_check": ["update_antigravity", "antigravity_check"],
    "ha_battery_check": ["ha_battery", "ha_battery_check"],
    "nas_storage_check": ["nas_storage", "nas_storage_check"],
    "ha_update_check": ["ha_update_check"],
    "dockhand_update": ["dockhand_check", "dockhand_update"],
    "weekly_proactive_digest": ["weekly_digest", "weekly_proactive_digest"],
    "plex_weekly_digest": ["plex_weekly_digest"],
    "daily_token_budget_report": ["daily_token_report", "token_report", "daily_token_budget_report"],
    "monthly_hardcode_regex_audit": ["monthly_hardcode_regex_audit", "code_audit", "hardcode_audit", "regex_audit"]
}

def get_last_execution_for_job(job_info: str | dict) -> tuple[float | None, str | None]:
    """Retrieve the latest known execution timestamp and formatted PT string for a job across all sources."""
    job_id = job_info.get("id", job_info) if isinstance(job_info, dict) else str(job_info)
    aliases = list(SIDECAR_ALIASES.get(job_id, [job_id]))
    if job_id not in aliases:
        aliases.insert(0, job_id)

    status_file = bs.DATA_DIR / "sidecar_status.json"
    latest_epoch = None
    latest_pt = None

    if status_file.exists():
        try:
            with open(status_file, "r") as f:
                smap = json.load(f)
            for a in aliases:
                if a in smap:
                    entry = smap[a]
                    epoch = entry.get("timestamp_epoch")
                    pt_str = entry.get("timestamp_pt")
                    if epoch and (latest_epoch is None or epoch > latest_epoch):
                        latest_epoch = epoch
                        latest_pt = pt_str
        except Exception:
            pass

    if isinstance(job_info, dict):
        sched_epoch = job_info.get("last_run_ts")
        sched_pt = job_info.get("last_run_at")
        if sched_epoch and (latest_epoch is None or sched_epoch > latest_epoch):
            latest_epoch = sched_epoch
            latest_pt = sched_pt

    return latest_epoch, latest_pt

def load_schedule() -> list[dict]:
    """Load schedule.json, seeding defaults if missing and reconciling with sidecar_status.json."""
    bs.DATA_DIR.mkdir(parents=True, exist_ok=True)
    sched_file = bs.DATA_DIR / "schedule.json"
    if not sched_file.exists():
        jobs = [dict(d) for d in DEFAULT_JOBS]
        for j in jobs:
            last_epoch, last_pt = get_last_execution_for_job(j)
            if last_epoch:
                j["last_run_ts"] = last_epoch
                j["last_run_at"] = last_pt
            j["next_run_ts"] = calculate_next_run(j)
        save_schedule(jobs)
        return jobs

    try:
        with open(sched_file, "r") as f:
            jobs = json.load(f)
        
        # Ensure any new defaults exist
        existing_ids = {j["id"] for j in jobs}
        updated = False
        for d in DEFAULT_JOBS:
            if d["id"] not in existing_ids:
                job_entry = dict(d)
                last_epoch, last_pt = get_last_execution_for_job(job_entry)
                if last_epoch:
                    job_entry["last_run_ts"] = last_epoch
                    job_entry["last_run_at"] = last_pt
                job_entry["next_run_ts"] = calculate_next_run(job_entry)
                jobs.append(job_entry)
                updated = True

        # Reconcile historical executions from sidecar_status.json
        for j in jobs:
            last_epoch, last_pt = get_last_execution_for_job(j)
            if last_epoch and (not j.get("last_run_ts") or last_epoch > j.get("last_run_ts", 0)):
                j["last_run_ts"] = last_epoch
                j["last_run_at"] = last_pt
                updated = True

        if updated:
            save_schedule(jobs)
        return jobs
    except Exception as e:
        log.error("Failed reading schedule.json: %s — using defaults", e)
        return DEFAULT_JOBS

def save_schedule(jobs: list[dict]):
    """Persist schedule safely with an atomic write."""
    bs.DATA_DIR.mkdir(parents=True, exist_ok=True)
    sched_file = bs.DATA_DIR / "schedule.json"
    tmp = sched_file.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(jobs, f, indent=2)
    tmp.replace(sched_file)

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
