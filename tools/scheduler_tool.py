#!/usr/bin/env python3
"""Persistent Sidecar Scheduler.

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
        "id": "quant_daily_sweep",
        "name": "Multi-Venue Quant Paper Trading Sweep",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 8,
        "minute_pt": 0,
        "prompt": "Run multi-venue quantitative paper trading expansion sweep across all 4 tracks: python3 /workspace/tools/sidecars.py quant. Silent background execution (alerts #vault only on error).",
        "catchup_if_missed": False,
        "quiet_hours": None,
    },
    {
        "id": "actual_bank_sync",
        "name": "Actual Budget Daily Bank Sync",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 6,
        "minute_pt": 0,
        "prompt": "Run automated Actual Budget bank sync using /workspace/tools/sidecars.py actual_sync. Silent sidecar (silent execution on routine sync, alert only on MFA reauth or failure).",
        "catchup_if_missed": False,
    },
    {
        "id": "cash_sweep_monitor",
        "name": "Monthly Cash Sweep Policy Monitor",
        "enabled": True,
        "schedule_type": "monthly",
        "day_of_month": 2,
        "hour_pt": 9,
        "minute_pt": 0,
        "prompt": "Run monthly cash sweep policy evaluation using /workspace/tools/sidecars.py cash_sweep. Post liquid reserve status and recommended sweep amount to #finances.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 86400,
    },
    {
        "id": "monthly_variance_digest",
        "name": "Monthly Financial Model Variance Digest",
        "enabled": True,
        "schedule_type": "monthly",
        "day_of_month": 3,
        "hour_pt": 9,
        "minute_pt": 15,
        "prompt": "Run monthly financial model variance evaluation using /workspace/tools/sidecars.py variance_digest. Post actuals vs model comparison to #finances.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 86400,
    },

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
        "id": "gemini_context_digest",
        "name": "Daily Zero & Ivy Context Email Digest",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 6,
        "minute_pt": 30,
        "prompt": "Send and auto-archive the Zero & Ivy daily context digest email using /workspace/tools/sidecars.py context_digest --quiet.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 14400
    },
    {
        "id": "daily_quota_card",
        "name": "Daily Gemini Quota Card",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 7,
        "minute_pt": 0,
        "prompt": "Display the Gemini weekly quota progress bar card using /workspace/tools/sidecars.py quota.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 14400
    },
    {
        "id": "daily_birthday_reminder",
        "name": "Daily Birthday Reminder",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 7,
        "minute_pt": 15,
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
        "hour_pt": 20,  # 8:00 PM PT
        "minute_pt": 0,
        "prompt": "Run the Option B weekly proactive digest using /workspace/tools/weekly_digest.py.",
        "catchup_if_missed": False
    },
    {
        "id": "plex_weekly_digest",
        "name": "Plex Weekly New Media Digest",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 4,  # Friday
        "hour_pt": 16,  # 4:00 PM PT
        "minute_pt": 0,
        "prompt": "Run the Plex weekly new media digest using /workspace/tools/plex_weekly_digest.py.",
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
    },
    {
        "id": "host2_backup",
        "name": "Host 2 Local USB Backup",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 3,
        "minute_pt": 30,
        "prompt": "Run the Host 2 local USB backup using /workspace/tools/sidecars.py backup_host2. Silent sidecar execution (silent unless error).",
        "catchup_if_missed": True,
        "catchup_window_seconds": 14400
    },
    {
        "id": "host1_backup",
        "name": "Host 1 Local USB Backup",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 3,
        "minute_pt": 45,
        "prompt": "Run the Host 1 local USB backup using /workspace/tools/sidecars.py backup_host1. Silent sidecar execution (silent unless error).",
        "catchup_if_missed": True,
        "catchup_window_seconds": 14400
    },
    {
        "id": "bridge_watchdog",
        "name": "Bridge Liveness Watchdog",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 300,
        "prompt": "Run the bridge liveness & gateway watchdog check using /workspace/tools/sidecars.py bridge_watchdog. Auto-heals and alerts only if degraded (silent when nominal).",
        "catchup_if_missed": False
    },
    {
        "id": "pid_watchdog",
        "name": "PID & Process Headroom Watchdog",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 900,
        "channel_id": 1544953279664889888,
        "prompt": "Run the Host 2 and Host 1 PID & process headroom watchdog using /workspace/tools/sidecars.py pid_watchdog. Silent execution unless warning or critical threshold exceeded.",
        "catchup_if_missed": False
    },
    {
        "id": "agora_roguelike_pm",
        "name": "Agora Roguelike PM & Board Coordinator",
        "enabled": False,
        "schedule_type": "interval",
        "interval_seconds": 900,
        "channel_id": 1558202642663211169,
        "prompt": "Run the Agora Roguelike PM board assessment and autonomous merge check using python3 /workspace/tools/sidecars.py agora_pm. Post updates to #agora when PRs or assignments change (silent when nominal).",
        "catchup_if_missed": False,
        "quiet_hours": [23, 7]
    },
    {
        "id": "gotchi_sprint",
        "name": "GOTCHI Software Sprint Coordinator",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 900,
        "channel_id": 1561509863018856468,
        "prompt": "Run the GOTCHI software sprint cycle using python3 /workspace/tools/sidecars.py gotchi_sprint --force. Execute the next open software issue on brockventures/gotchi or output [NO_REPLY] when idle.",
        "catchup_if_missed": False,
        "quiet_hours": [23, 7],
        "model_override": "gemini-4-argon-medium"
    },
    {
        "id": "agora_roguelike_engineer",
        "name": "Agora Roguelike Engineering Execution",
        "enabled": False,
        "schedule_type": "interval",
        "interval_seconds": 900,
        "channel_id": 1558202642663211169,
        "prompt": "Run the Agora Roguelike engineering execution check using python3 /workspace/tools/sidecars.py agora_engineer.",
        "catchup_if_missed": False,
        "quiet_hours": [23, 7]
    },
    {
        "id": "kalshi_paper_bot",
        "name": "Kalshi Weather Quant Paper Bot",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 5,
        "minute_pt": 15,
        "prompt": "Run the daily Kalshi weather quant paper bot scan and settlement: python3 /workspace/tools/sidecars.py kalshi",
        "catchup_if_missed": False,
        "quiet_hours": None
    },
    {
        "id": "kalshi_evening_review",
        "name": "Kalshi Paper Trading Evening Review & Model Sync",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 18,
        "minute_pt": 45,
        "prompt": "Run the Kalshi paper trading evening settlement and review: python3 /workspace/tools/sidecars.py kalshi_evening",
        "catchup_if_missed": False,
        "quiet_hours": [23, 7]
    },
    {
        "id": "weekly_meal_proposal",
        "name": "Weekly 3-Dinner Meal Proposal",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 6,  # Sunday
        "hour_pt": 19,
        "minute_pt": 0,
        "channel_id": 1544955538033348618,
        "prompt": "Run weekly 3-dinner meal proposal using /workspace/tools/sidecars.py meal_proposal. Post dinner rotation options to #shopping.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 14400
    },
    {
        "id": "weekly_grocery_staging",
        "name": "Weekly Whole Foods Grocery Staging",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 0,  # Monday
        "hour_pt": 9,
        "minute_pt": 0,
        "channel_id": 1544955538033348618,
        "prompt": "Run weekly grocery cart compilation using /workspace/tools/sidecars.py groceries. Ingest recipes and todo.shopping_list, and post the 1-click Whole Foods AFX cart to #shopping.",
        "catchup_if_missed": False
    },
    {
        "id": "token_budget_watchdog",
        "name": "Daily Token Budget Watchdog",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 23,
        "minute_pt": 45,
        "channel_id": 1544953279664889888,
        "prompt": "Run daily token budget watchdog evaluation using /workspace/tools/sidecars.py token_budget_watchdog --dispatch.",
        "catchup_if_missed": False
    },
    {
        "id": "reddit_fantasy_morning",
        "name": "Reddit Fantasy Baseball Morning Scanner",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 8,
        "minute_pt": 5,
        "channel_id": 1542081375287640084,
        "prompt": "Run the Reddit fantasy baseball morning discovery scan using /workspace/tools/sidecars.py reddit_fantasy. Scan r/fantasybaseball for active dilemma comments, generate draft replies, and post candidate cards to #zero-chat.",
        "catchup_if_missed": False
    },
    {
        "id": "reddit_fantasy_afternoon",
        "name": "Reddit Fantasy Baseball Afternoon Scanner",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 14,
        "minute_pt": 0,
        "channel_id": 1542081375287640084,
        "prompt": "Run the Reddit fantasy baseball afternoon discovery scan using /workspace/tools/sidecars.py reddit_fantasy. Scan r/fantasybaseball for active dilemma comments, generate draft replies, and post candidate cards to #zero-chat.",
        "catchup_if_missed": False
    },
    {
        "id": "kalshi_night_audit",
        "name": "Kalshi Nightly Settlement & Quant Standup",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 21,
        "minute_pt": 45,
        "prompt": "Run the Kalshi nightly settlement, calibration audit, and GitHub sync: python3 /workspace/tools/sidecars.py kalshi_night",
        "catchup_if_missed": False,
        "quiet_hours": [23, 7]
    },
    {
        "id": "kometa_audit",
        "name": "Kometa Post-Run Audit",
        "enabled": True,
        "schedule_type": "daily",
        "hour_pt": 2,
        "minute_pt": 15,
        "channel_id": 1544955535722545253,
        "prompt": "Run the Kometa post-run log audit using /workspace/tools/sidecars.py kometa_audit. Audit meta.log on Host 1 and report status.",
        "catchup_if_missed": False
    },
    {
        "id": "arr_queue_watchdog",
        "name": "Arr Queue & Health Watchdog",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 3600,
        "channel_id": 1544955535722545253,
        "prompt": "Run the Arr queue & health watchdog using /workspace/tools/sidecars.py arr_queue.",
        "catchup_if_missed": False
    },
    {
        "id": "prowlarr_watchdog",
        "name": "Prowlarr Indexer Watchdog",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 3600,
        "channel_id": 1544955535722545253,
        "prompt": "Run the Prowlarr indexer health watchdog using /workspace/tools/sidecars.py prowlarr.",
        "catchup_if_missed": False
    },
    {
        "id": "sabnzbd_watchdog",
        "name": "SABnzbd Watchdog",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 3600,
        "channel_id": 1544955535722545253,
        "prompt": "Run the SABnzbd downloader & unpack watchdog using /workspace/tools/sidecars.py sabnzbd.",
        "catchup_if_missed": False
    },
    {
        "id": "nest_lock_watchdog",
        "name": "Nest Lock Watchdog",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 3600,
        "channel_id": 1544953275877556334,
        "prompt": "Run the Nest lock & Homebridge self-healing watchdog using /workspace/tools/sidecars.py nest_lock.",
        "catchup_if_missed": False
    },
    {
        "id": "ha_reauth_watchdog",
        "name": "HA Re-Auth Watchdog",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 3600,
        "channel_id": 1544953275877556334,
        "prompt": "Run the Home Assistant integration & re-auth watchdog using /workspace/tools/sidecars.py ha_reauth.",
        "catchup_if_missed": False
    },
    {
        "id": "google_tasks_sync",
        "name": "Google Tasks Two-Way Sync",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 1800,
        "prompt": "Run the Google Tasks two-way sync using /workspace/tools/sidecars.py tasks_sync. Silent sidecar execution (silent unless error).",
        "catchup_if_missed": False
    },
    {
        "id": "quota_poller",
        "name": "10-Minute Quota Usage Poller",
        "enabled": True,
        "schedule_type": "interval",
        "interval_seconds": 600,
        "prompt": "Run the 10-minute ground-truth quota usage poller using /workspace/tools/sidecars.py quota_poller. Captures live remaining fractions and cycle burn deltas.",
        "catchup_if_missed": False
    },
    {
        "id": "personal_data_sync",
        "name": "Weekly Personal Data & Gaming Profile Sync",
        "enabled": True,
        "schedule_type": "weekly",
        "day_of_week": 6,  # Sunday
        "hour_pt": 4,
        "minute_pt": 0,
        "channel_id": 1544953279664889888,
        "prompt": "Run the weekly personal data and gaming profile sync using /workspace/tools/sidecars.py personal_data_sync --quiet. Syncs Steam, Xbox, Switch 2, Amazon orders, and Plex watch history into durable memory and re-indexes transcript_index.db.",
        "catchup_if_missed": True,
        "catchup_window_seconds": 14400
    }
]

INTERNAL_SYSTEM_MAINTENANCE_JOBS = {
    "session_rollover",
    "dreaming",
    "host1_backup",
    "host2_backup",
    "bridge_watchdog",
    "pid_watchdog",
    "plex_cleanup",
    "kalshi_paper_bot",
    "quota_poller",
    "personal_data_sync",
}


def calculate_next_run(job: dict, from_ts: float | None = None) -> float:
    """Compute the next Unix timestamp for a job."""
    now_ts = from_ts or time.time()
    now_pt = datetime.fromtimestamp(now_ts, tz=PT)
    stype = job.get("schedule_type", "daily")

    if stype == "interval":
        interval = job.get("interval_seconds", 7200)
        last = job.get("last_run_ts")
        if last and (last + interval) > now_ts:
            nxt = float(last + interval)
        else:
            nxt = float(now_ts + interval)

        quiet = job.get("quiet_hours")

        if quiet and len(quiet) == 2:
            start_h, end_h = quiet
            nxt_pt = datetime.fromtimestamp(nxt, tz=PT)
            in_quiet = (start_h <= nxt_pt.hour < end_h) if start_h < end_h else (nxt_pt.hour >= start_h or nxt_pt.hour < end_h)
            if in_quiet:
                resume_pt = nxt_pt.replace(hour=end_h, minute=0, second=0, microsecond=0)
                if resume_pt <= nxt_pt:
                    resume_pt += timedelta(days=1)
                nxt = resume_pt.timestamp()
        return nxt

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
    "quant_daily_sweep": ["quant_daily_sweep", "quant_expansion_sweep", "quant"],
    "actual_bank_sync": ["actual_bank_sync", "actual_sync"],
    "cash_sweep_monitor": ["cash_sweep_monitor", "cash_sweep", "sweep"],
    "monthly_variance_digest": ["monthly_variance_digest", "variance_digest", "variance"],
    "agora_roguelike_pm": ["agora_roguelike_pm", "agora_pm"],
    "agora_roguelike_engineer": ["agora_roguelike_engineer", "agora_eng", "agora_engineer"],
    "gotchi_sprint": ["gotchi_sprint", "gotchi"],
    "heartbeat_sweep": ["heartbeat", "heartbeat_sweep"],
    "nightly_triage": ["triage", "nightly_triage"],
    "nas_logs": ["nas_logs"],
    "dreaming": ["dream", "dreaming"],
    "session_rollover": ["session_rollover", "rollover"],
    "plex_cleanup": ["plex", "plex_cleanup"],
    "dated_reminders": ["reminders", "dated_reminders"],
    "ev9_monitor": ["ev9", "ev9_monitor"],
    "marketing_sweep": ["marketing", "marketing_sweep"],
    "memory_doctor": ["doctor", "memory_doctor"],
    "daily_birthday_reminder": ["daily_birthday_reminder", "birthday_reminder", "birthdays"],
    "weekly_social_last_seen_review": ["weekly_social_review", "social_review", "weekly_social_last_seen_review"],
    "monthly_core_friends_reconnect": ["monthly_core_friends_reminder", "core_friends", "monthly_core_friends_reconnect"],
    "morning_topic_rotation": ["morning_topic_rotation", "morning_dispatcher", "morning"],
    "antigravity_check": ["update_antigravity", "antigravity_check"],
    "ha_battery_check": ["ha_battery", "ha_battery_check"],
    "ha_anomaly_check": ["ha_anomaly_check", "ha_anomaly", "anomaly"],
    "nas_storage_check": ["nas_storage", "nas_storage_check"],
    "ha_update_check": ["ha_update_check"],
    "dockhand_update": ["dockhand_check", "dockhand_update"],
    "weekly_proactive_digest": ["weekly_digest", "weekly_proactive_digest"],
    "plex_weekly_digest": ["plex_weekly_digest"],
    "monthly_hardcode_regex_audit": ["monthly_hardcode_regex_audit", "code_audit", "hardcode_audit", "regex_audit"],
    "host2_backup": ["host2_backup", "backup_host2", "backup2"],
    "host1_backup": ["host1_backup", "backup_host1", "backup1"],
    "bridge_watchdog": ["bridge_watchdog"],
    "pid_watchdog": ["pid_watchdog", "pid"],
    "kalshi_paper_bot": ["kalshi_paper_bot", "kalshi", "kalshi_bot"],
    "kalshi_evening_review": ["kalshi_evening_review", "kalshi_evening", "kalshi_review"],
    "kalshi_night_audit": ["kalshi_night_audit", "kalshi_night", "kalshi_audit"],
    "reddit_fantasy_morning": ["reddit_fantasy_morning", "reddit_fantasy_scanner", "reddit_fantasy"],
    "reddit_fantasy_afternoon": ["reddit_fantasy_afternoon", "reddit_fantasy_scanner", "reddit_fantasy"],
    "weekly_meal_proposal": ["weekly_meal_proposal", "meal_proposal"],
    "weekly_grocery_staging": ["weekly_grocery_staging", "grocery_staging", "groceries"],
    "token_budget_watchdog": ["token_budget_watchdog", "token_budget"],
    "arr_queue_watchdog": ["arr_queue_watchdog", "arr_queue", "arr"],
    "prowlarr_watchdog": ["prowlarr_watchdog", "prowlarr", "indexers"],
    "sabnzbd_watchdog": ["sabnzbd_watchdog", "sabnzbd", "sab"],
    "nest_lock_watchdog": ["nest_lock_watchdog", "nest_lock", "lock_watchdog", "lock_heal"],
    "ha_reauth_watchdog": ["ha_reauth_watchdog", "ha_reauth", "ha_auth"],
    "kometa_audit": ["kometa_audit", "kometa"],
    "google_tasks_sync": ["google_tasks_sync", "tasks_sync"],
    "quota_poller": ["quota_poller", "quota_poll", "poller_quota"],
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

    out = [f"📅 **Sidecar Schedule** — Current Time: `{now_pt.strftime('%I:%M %p PT')}`\n"]
    for j in sorted_jobs:
        status = "🟢 Active" if j.get("enabled", True) else "⏸️ Paused"
        next_dt = datetime.fromtimestamp(j["next_run_ts"], tz=PT)
        time_diff = next_dt - now_pt
        total_sec = int(time_diff.total_seconds())
        if total_sec < 0:
            rel = "due now"
        else:
            hours, remainder = divmod(total_sec, 3600)
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
