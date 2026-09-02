"""
Zero Discord Bridge - Karakos Background Scheduler & Sidecar Dispatcher Module
Encapsulates all persistent JSON-backed cron/schedule evaluation, sidecar execution,
anti-storm guards, liveness wedge detection, and outbox queue flushing.
"""

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import discord

from tools.bridge_state import (
    DATA_DIR,
    BEACON_FILE,
    BOT_STATUS_FILE,
    TARGET_CHANNEL_ID,
    PT_TZ,
    update_beacon,
    get_channel_session_id,
)
from tools.bridge_formatting import (
    chunk_text,
    parse_interactive_choices,
)
import tools.bridge_runner as br

LAST_SCHEDULED_DISPATCH = {}
_last_bot_status_mtime = 0.0


async def dispatch_scheduled_prompt(
    prompt: str,
    job_name: str = "Sidecar",
    bot: discord.Client = None,
    turn_queue = None,
    apply_presence_fn = None,
    quick_choice_view_cls = None,
    button_choice_fn = None,
):
    """Inject a scheduled sidecar prompt into the message queue with anti-storm guard."""
    global LAST_SCHEDULED_DISPATCH

    # Anti-storm guard: prevent any job from dispatching more than once per 5 minutes
    now_ts = time.time()
    last_disp = LAST_SCHEDULED_DISPATCH.get(job_name, 0)
    if (now_ts - last_disp) < 300:
        print(f"[Scheduler] Anti-storm block: {job_name} attempted re-trigger within 300s (last: {now_ts - last_disp:.1f}s ago). Dropping.")
        return
    LAST_SCHEDULED_DISPATCH[job_name] = now_ts

    if prompt == "[INTERNAL_SESSION_ROLLOVER]" or job_name in ("Session Rollover", "Daily Session Rollover"):
        # Generate carry-forward summary BEFORE resetting session
        err_msg = None
        try:
            from tools.session_summarizer import generate_summary
            home_cid = get_channel_session_id(TARGET_CHANNEL_ID, "home")
            if home_cid:
                generate_summary(conv_id=home_cid, sess_key="home")
            print("[Scheduler] Generated carry-forward summary before session rollover.")
        except Exception as e:
            err_msg = f"⚠️ **Daily Session Rollover Error**: Failed generating carry-forward summary: {e}"
            print(f"[Scheduler] Error generating rollover summary: {e}")

        br.reset_session_keys.add("home")
        if err_msg and bot:
            ch = bot.get_channel(TARGET_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(err_msg)
                except Exception:
                    pass
        print("[Scheduler] Executed daily session rollover at 2:00 AM PT (silent on success, reset_session_keys added 'home').")
        return

    # Heartbeat sweep: silent execution unless degraded
    if job_name == "Heartbeat Sweep" or "sidecars.py heartbeat" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_heartbeat_sweep
            healthy, report, _ = run_sidecar_job("heartbeat", "Heartbeat Sweep", run_heartbeat_sweep)
            if not healthy and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    await ch.send(report)
        except Exception as e:
            print(f"[Scheduler] Heartbeat sweep execution error: {e}")
        return

    # Dated reminders: silent execution unless a reminder is due today
    if job_name == "Dated Reminders" or "sidecars.py reminders" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_dated_reminders
            has_due, rep, _ = run_sidecar_job("reminders", "Dated Reminders", run_dated_reminders)
            if has_due and rep and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    await ch.send(rep)
            else:
                print("[Scheduler] Dated reminders checked: 0 due today (silent).")
        except Exception as e:
            print(f"[Scheduler] Dated reminders execution error: {e}")
        return

    # EV9 listing monitor: silent execution on Mon-Sat; only posts Sunday digest
    if job_name == "EV9 Listing Monitor" or "sidecars.py ev9" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_ev9_monitor
            has_digest, rep, plot_path = run_sidecar_job("ev9", "EV9 Listing Monitor", run_ev9_monitor, force_digest=False)
            if has_digest and rep and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    file = discord.File(plot_path) if plot_path and os.path.exists(plot_path) else None
                    await ch.send(rep, file=file)
            else:
                print("[Scheduler] EV9 monitor: daily capture completed (silent; no digest or trend plot).")
        except Exception as e:
            print(f"[Scheduler] EV9 monitor execution error: {e}")
        return

    # Marketing email sweep: biweekly promotional sweep
    if job_name in ("Biweekly Marketing Sweep", "Marketing Email Sweep") or "sidecars.py marketing" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_marketing_sweep
            should_post, rep, _ = run_sidecar_job("marketing", "Marketing Email Sweep", run_marketing_sweep, force=False)
            if should_post and rep and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    await ch.send(rep)
        except Exception as e:
            print(f"[Scheduler] Marketing sweep execution error: {e}")
        return

    # Nightly triage & agenda briefing
    if job_name in ("Nightly Triage & Briefing", "Nightly Triage") or "sidecars.py triage" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_nightly_triage
            ok, rep, _ = run_sidecar_job("triage", "Nightly Triage & Briefing", run_nightly_triage)
            if rep and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    for chunk in chunk_text(rep):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] Nightly triage execution error: {e}")
        return

    # Nightly NAS log review
    if job_name in ("NAS Log Review", "NAS Log Review Check") or "sidecars.py nas_logs" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_nas_log_review
            ok, rep, _ = run_sidecar_job("nas_logs", "NAS Log Review", run_nas_log_review)
            if rep and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    for chunk in chunk_text(rep):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] NAS log review execution error: {e}")
        return

    # Plex transcode cache cleanup: silent unless warning/error
    if job_name == "Plex Transcode Cleanup" or "sidecars.py plex" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_plex_session_cleanup
            ok, rep, _ = run_sidecar_job("plex", "Plex Transcode Cleanup", run_plex_session_cleanup)
            if not ok and rep and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    await ch.send(rep)
        except Exception as e:
            print(f"[Scheduler] Plex transcode cleanup execution error: {e}")
        return

    # Dreaming memory consolidation: headless nightly consolidation
    if job_name in ("Dreaming Memory Consolidation", "Dreaming Consolidation") or "sidecars.py dream" in prompt:
        try:
            from tools.memory_manager import run_dreaming_consolidation
            from tools.sidecars import run_sidecar_job
            ok, rep, _ = run_sidecar_job("dream", "Dreaming Consolidation", run_dreaming_consolidation)
            print(f"[Scheduler] Dreaming memory consolidation completed: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Dreaming consolidation execution error: {e}")
        return

    # Memory doctor weekly audit
    if job_name in ("Memory Doctor Audit", "Memory Doctor") or "sidecars.py doctor" in prompt:
        try:
            from tools.memory_manager import run_memory_doctor
            from tools.sidecars import run_sidecar_job
            ok, rep, _ = run_sidecar_job("doctor", "Memory Doctor Audit", run_memory_doctor)
            if rep and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    for chunk in chunk_text(rep):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] Memory doctor audit execution error: {e}")
        return

    # Antigravity CLI update check: silent execution unless a new version is available
    if "update_antigravity.py" in prompt or job_name == "Antigravity CLI Check" or job_name == "Antigravity Release Check":
        try:
            from tools.sidecars import run_sidecar_job, run_antigravity_check
            ok, out, _ = run_sidecar_job("update_antigravity", "Antigravity CLI Check", run_antigravity_check)
            if out and out.strip() and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    if choice_view:
                        await ch.send(clean_content, view=choice_view)
                    else:
                        await ch.send(clean_content)
            else:
                print("[Scheduler] Antigravity CLI check: up to date (silent).")
        except Exception as e:
            print(f"[Scheduler] Antigravity CLI check execution error: {e}")
        return

    # Dockhand container image check: silent execution unless an update is available
    if "dockhand_update.py" in prompt or job_name in ("Dockhand Image Check", "Dockhand Image Update Check"):
        try:
            from tools.sidecars import run_sidecar_job, run_dockhand_update_check
            ok, out, _ = run_sidecar_job("dockhand_check", "Dockhand Image Check", run_dockhand_update_check)
            if out and out.strip() and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    if choice_view:
                        await ch.send(clean_content, view=choice_view)
                    else:
                        await ch.send(clean_content)
            else:
                print("[Scheduler] Dockhand image check: up to date on both NAS hosts (silent).")
        except Exception as e:
            print(f"[Scheduler] Dockhand image check execution error: {e}")
        return

    # Home Assistant stable update check
    if "ha_update_check.py" in prompt or job_name in ("Home Assistant Stable Update Check", "HA Update Check"):
        try:
            from tools.sidecars import run_sidecar_job, run_ha_update_check
            ok, out, _ = run_sidecar_job("ha_update_check", "HA Update Check", run_ha_update_check)
            if out and out.strip() and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    if choice_view:
                        await ch.send(clean_content, view=choice_view)
                    else:
                        await ch.send(clean_content)
        except Exception as e:
            print(f"[Scheduler] HA update check execution error: {e}")
        return

    # Home Assistant IoT battery watchdog: silent unless low battery (<15%)
    if "ha_battery_check.py" in prompt or job_name in ("Home Assistant IoT Battery Watchdog", "HA Battery Check"):
        try:
            from tools.sidecars import run_sidecar_job, run_ha_battery_check
            ok, out, _ = run_sidecar_job("ha_battery", "HA Battery Check", run_ha_battery_check)
            if not ok and out and out.strip() and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    await ch.send(out)
        except Exception as e:
            print(f"[Scheduler] HA battery check execution error: {e}")
        return

    # Synology storage & RAID health check: silent unless volume >85% or RAID degraded
    if "nas_storage_check.py" in prompt or job_name in ("Synology Storage & Array Health Check", "NAS Storage Check"):
        try:
            from tools.sidecars import run_sidecar_job, run_nas_storage_check
            ok, out, _ = run_sidecar_job("nas_storage", "NAS Storage Check", run_nas_storage_check)
            if not ok and out and out.strip() and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    await ch.send(out)
        except Exception as e:
            print(f"[Scheduler] NAS storage check execution error: {e}")
        return

    # Option B Weekly Proactive Digest
    if "weekly_digest.py" in prompt or job_name in ("Option B Weekly Proactive Digest", "Weekly Proactive Digest"):
        try:
            res = subprocess.run(["python3", "/workspace/tools/weekly_digest.py"], capture_output=True, text=True, timeout=60)
            out = res.stdout.strip()
            if out and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    for chunk in chunk_text(out):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] Weekly digest execution error: {e}")
        return

    # Plex Weekly New Media Digest
    if "plex_weekly_digest.py" in prompt or job_name == "Plex Weekly New Media Digest":
        try:
            res = subprocess.run(["python3", "/workspace/tools/plex_weekly_digest.py", "post", "--tag-all"], capture_output=True, text=True, timeout=60)
            out = res.stdout.strip()
            if out:
                print(f"[Scheduler] Plex weekly digest executed: {out[:100]}")
        except Exception as e:
            print(f"[Scheduler] Plex weekly digest execution error: {e}")
        return

    # Crab Cavern Morning Topic Rotation
    if "morning_dispatcher.py" in prompt or job_name in ("Crab Cavern Morning Topic Rotation", "Morning Topic Rotation"):
        try:
            from tools.morning_dispatcher import dispatch_morning_topic
            res = dispatch_morning_topic(dry_run=False)
            print(f"[Scheduler] Crab Cavern morning rotation dispatched: status={res.get('status')}, outbox_id={res.get('outbox_id')}")
        except Exception as e:
            print(f"[Scheduler] Crab Cavern morning rotation dispatch error: {e}")
        return

    # Daily birthday reminder: silent execution unless someone has a birthday today
    if "birthday_reminder.py" in prompt or job_name in ("Daily Birthday Reminder", "Birthday Reminder") or "sidecars.py birthdays" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_birthday_reminders
            ok, out, extra = run_sidecar_job("daily_birthday_reminder", "Daily Birthday Reminder", run_birthday_reminders)
            has_bday = extra.get("has_items", False) if isinstance(extra, dict) else bool(out and out.strip())
            if has_bday and out and out.strip() and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    if choice_view:
                        await ch.send(clean_content, view=choice_view)
                    else:
                        await ch.send(clean_content)
            else:
                print("[Scheduler] Daily birthday reminder checked: 0 birthdays today (silent).")
        except Exception as e:
            print(f"[Scheduler] Daily birthday reminder execution error: {e}")
        return

    # Weekly social & last seen review: silent execution unless qualifying events found
    if "social_last_seen_review.py" in prompt or job_name in ("Weekly Social & Last Seen Review", "Weekly Social Review") or "sidecars.py social_review" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_social_last_seen_review
            ok, out, extra = run_sidecar_job("weekly_social_review", "Weekly Social & Last Seen Review", run_social_last_seen_review)
            has_events = extra.get("has_items", False) if isinstance(extra, dict) else bool(out and out.strip())
            if has_events and out and out.strip() and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    if choice_view:
                        await ch.send(clean_content, view=choice_view)
                    else:
                        await ch.send(clean_content)
            else:
                print("[Scheduler] Weekly social review checked: 0 updates (silent).")
        except Exception as e:
            print(f"[Scheduler] Weekly social review execution error: {e}")
        return

    # Monthly core friends reconnect reminder: silent execution unless unseen core friends found
    if "core_friends_reminder.py" in prompt or job_name in ("Monthly Core Friends Social Planning Reminder", "Monthly Core Friends Social Reminder", "Core Friends Reminder") or "sidecars.py core_friends" in prompt:
        try:
            from tools.sidecars import run_sidecar_job, run_core_friends_reminder
            ok, out, extra = run_sidecar_job("monthly_core_friends_reminder", "Monthly Core Friends Social Reminder", run_core_friends_reminder)
            has_friends = extra.get("has_items", False) if isinstance(extra, dict) else bool(out and out.strip())
            if has_friends and out and out.strip() and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    if choice_view:
                        await ch.send(clean_content, view=choice_view)
                    else:
                        await ch.send(clean_content)
            else:
                print("[Scheduler] Monthly core friends reminder checked: all caught up (silent).")
        except Exception as e:
            print(f"[Scheduler] Monthly core friends reminder execution error: {e}")
        return

    # Daily token & AI Ultra compute budget report
    if "token_report" in prompt or job_name in ("Daily Token & AI Ultra Budget Report", "Daily Token Budget Report", "Token Report"):
        try:
            from tools.sidecars import run_sidecar_job, run_token_report
            ok, rep, _ = run_sidecar_job("daily_token_report", "Daily Token Budget Report", run_token_report)
            if rep and bot:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    for chunk in chunk_text(rep):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] Token report execution error: {e}")
        return

    if bot and turn_queue:
        ch = bot.get_channel(TARGET_CHANNEL_ID)
        if not ch:
            try:
                ch = await bot.fetch_channel(TARGET_CHANNEL_ID)
            except Exception as e:
                print(f"[Scheduler] Could not fetch channel {TARGET_CHANNEL_ID}: {e}")
                return
        status_msg = await ch.send(f"⏱️ **[Scheduled: {job_name}]** *Starting execution...*")
        await turn_queue.put({
            "prompt": prompt,
            "status_msg": status_msg,
            "reply_target": status_msg,
            "attachments": [],
            "is_steer": False,
            "mode": "home",
            "channel_id": TARGET_CHANNEL_ID
        })


def should_run_job(job: dict, now_ts: float) -> tuple[bool, str]:
    """
    Evaluates whether a scheduled job that has reached or exceeded its next_run_ts
    should execute right now, checking ground-truth execution history across all logs.

    Returns:
        (should_run: bool, reason: str)
    """
    if not job.get("enabled", True):
        return False, "disabled"

    next_ts = job.get("next_run_ts")
    if not next_ts:
        return False, "missing_next_run_ts"

    if now_ts < next_ts:
        return False, "not_due"

    # Query ground-truth execution time from sidecar_status.json and schedule.json
    try:
        from tools.scheduler_tool import get_last_execution_for_job
        last_run, _ = get_last_execution_for_job(job)
    except Exception:
        last_run = job.get("last_run_ts", 0)

    last_run = last_run or 0
    stype = job.get("schedule_type", "daily")
    window = job.get("catchup_window_seconds", 7200)
    min_period = 86400 * 0.7 if stype == "daily" else (7 * 86400 * 0.7 if stype == "weekly" else window)

    # 1. Guard against double-running if it already ran for this slot or in the current schedule period
    if (last_run >= next_ts - 300) or (last_run and (now_ts - last_run) < min_period):
        return False, f"already ran in current period (last_run_ts={last_run})"

    overdue = now_ts - next_ts

    # 2. If within normal scheduler loop interval jitter (within 60s)
    if overdue <= 60:
        return True, "on_time"

    # 3. Job is overdue (>60s past scheduled next_run_ts, e.g. due to restart/offline downtime)
    if not job.get("catchup_if_missed", False):
        return False, f"overdue ({overdue:.0f}s late) with catchup_if_missed=False"

    if overdue > window:
        return False, f"overdue ({overdue:.0f}s late) exceeding catchup window ({window}s)"

    return True, f"catchup within window ({overdue:.0f}s late <= {window}s)"


class KarakosScheduler:
    """Karakos-style persistent JSON-backed background scheduler for sidecars."""
    def __init__(
        self,
        dispatch_fn,
        bot: discord.Client = None,
        is_busy_fn = None,
        reload_fn = None,
        presence_fn = None
    ):
        self.dispatch_fn = dispatch_fn
        self.bot = bot
        self.is_busy_fn = is_busy_fn
        self.reload_fn = reload_fn
        self.presence_fn = presence_fn
        self._running = False
        self._task = None

    async def _evaluate_and_dispatch_jobs(self, is_startup: bool = False):
        """Evaluate all jobs in schedule.json, strictly enforcing catchup window rules and persistence."""
        try:
            from tools.scheduler_tool import load_schedule, save_schedule, calculate_next_run
            jobs = load_schedule()
            now_ts = time.time()
            jobs_to_dispatch = []
            updated = False

            for j in jobs:
                if not j.get("enabled", True):
                    continue

                next_ts = j.get("next_run_ts")
                if not next_ts:
                    j["next_run_ts"] = calculate_next_run(j, from_ts=now_ts)
                    updated = True
                    continue

                if now_ts >= next_ts:
                    should_run, reason = should_run_job(j, now_ts)

                    # Advance next_run_ts to future occurrence regardless of run or skip
                    if j.get("schedule_type") == "one_shot":
                        j["enabled"] = False
                        j["next_run_ts"] = None
                    else:
                        j["next_run_ts"] = calculate_next_run(j, from_ts=now_ts)

                    prefix = "[KarakosScheduler Startup]" if is_startup else "[KarakosScheduler]"
                    if should_run:
                        j["last_run_ts"] = now_ts
                        j["last_run_at"] = datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M %p PT")
                        print(f"{prefix} Triggering {j['name']} ({reason}, next: {j['next_run_ts']})")
                        jobs_to_dispatch.append((j["prompt"], j["name"]))
                    else:
                        print(f"{prefix} Skipping {j['name']} ({reason}, advanced next to: {j['next_run_ts']})")

                    updated = True

            # CRITICAL: Persist schedule to disk BEFORE dispatching
            if updated:
                save_schedule(jobs)

            # Dispatch all eligible jobs
            for prompt, name in jobs_to_dispatch:
                try:
                    await self.dispatch_fn(prompt, job_name=name)
                except Exception as de:
                    print(f"[KarakosScheduler] Error dispatching {name}: {de}")

        except Exception as e:
            print(f"[KarakosScheduler] Error evaluating jobs: {e}")

    async def start(self):
        self._running = True
        # Evaluate missed/due jobs on startup
        await self._evaluate_and_dispatch_jobs(is_startup=True)
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        global _last_bot_status_mtime
        while self._running:
            try:
                now_ts = time.time()
                await self._evaluate_and_dispatch_jobs(is_startup=False)

                # Liveness Beacon & Wedge Check (Karakos Pattern: >420s unbroken silence while PROCESSING)
                if BEACON_FILE.exists():
                    try:
                        with open(BEACON_FILE) as bf:
                            bdata = json.load(bf)
                        if bdata.get("state") == "PROCESSING":
                            has_running_proc = (
                                (br.active_proc is not None and br.active_proc.returncode is None) or
                                (br.ext_active_proc is not None and br.ext_active_proc.returncode is None)
                            )
                            if not has_running_proc:
                                # Stale beacon from before restart or completed turn: reset silently
                                update_beacon("IDLE", "")
                            else:
                                silence = now_ts - bdata.get("ts", now_ts)
                                if silence > 420 and not bdata.get("alerted"):
                                    print(f"[WedgeCheck] Agent silent for {silence:.0f}s during active turn!")
                                    bdata["alerted"] = True
                                    with open(BEACON_FILE, "w") as bf:
                                        json.dump(bdata, bf)
                                    if self.bot:
                                        ch = self.bot.get_channel(TARGET_CHANNEL_ID)
                                        if ch:
                                            await ch.send(f"⚠️ **Wedge Alert:** Agent has been silent for >{int(silence/60)}m without output. Prompt: `{bdata.get('prompt')}`")
                    except Exception:
                        pass

                # Bot Presence & Custom Status Monitor
                if BOT_STATUS_FILE.exists():
                    try:
                        mtime = BOT_STATUS_FILE.stat().st_mtime
                        if mtime > _last_bot_status_mtime:
                            _last_bot_status_mtime = mtime
                            busy = (
                                (br.active_proc is not None and br.active_proc.returncode is None) or
                                (br.ext_active_proc is not None and br.ext_active_proc.returncode is None)
                            )
                            if not busy and self.presence_fn:
                                await self.presence_fn()
                    except Exception:
                        pass

                # Outbox Queue Flusher (Cross-Channel Asynchronous Dispatch)
                try:
                    from tools.outbox import flush_pending_messages
                    pending_outbox = flush_pending_messages()
                    for omsg in pending_outbox:
                        target_cid = omsg.get("channel_id")
                        if target_cid and self.bot:
                            target_channel = self.bot.get_channel(target_cid) or await self.bot.fetch_channel(target_cid)
                            if target_channel:
                                is_agent_chat = (target_cid == 1534436119888793750 or str(omsg.get("channel")) == "agent-chat")
                                if is_agent_chat:
                                    try:
                                        from tools.banana import claim, release
                                        claim(subject=omsg.get("id", "outbox-dispatch"))
                                    except Exception as be:
                                        print(f"[Outbox] Warning claiming Banana: {be}")
                                    try:
                                        await target_channel.send(omsg.get("content", ""))
                                    finally:
                                        try:
                                            release()
                                        except Exception as err:
                                            print(f"[Outbox] Warning releasing Banana: {err}")
                                else:
                                    await target_channel.send(omsg.get("content", ""))
                                print(f"[Outbox] Dispatched message {omsg.get('id')} to #{omsg.get('channel')} ({target_cid})")
                except Exception as oe:
                    print(f"[Bridge] Error flushing outbox queue: {oe}")

                # Zero-downtime bridge reload trigger
                reload_flag = DATA_DIR / "reload_bridge.flag"
                if reload_flag.exists():
                    flag_age = 0
                    try:
                        flag_stat = reload_flag.stat()
                        flag_age = time.time() - flag_stat.st_mtime
                    except Exception:
                        pass
                    busy = self.is_busy_fn() if self.is_busy_fn else []
                    if not busy or flag_age > 20.0:
                        try:
                            reload_flag.unlink()
                        except Exception:
                            pass
                        print(f"[Bridge] Reload flag detected (age={flag_age:.1f}s, busy={busy}). Executing in-place reload...")
                        if self.reload_fn:
                            await self.reload_fn(None, initiator="scheduler", force=True, reason="Scheduler reload flag trigger")
            except Exception as e:
                print(f"[KarakosScheduler] Error in loop: {e}")

            await asyncio.sleep(15)
