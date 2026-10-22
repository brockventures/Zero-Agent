"""
Zero Discord Bridge - Persistent Background Scheduler & Sidecar Dispatcher Module
Encapsulates all persistent JSON-backed cron/schedule evaluation, sidecar execution,
anti-storm guards, liveness wedge detection, and outbox queue flushing.
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
import discord

from tools.bridge_state import (
    DATA_DIR,
    BEACON_FILE,
    BOT_STATUS_FILE,
    TARGET_CHANNEL_ID,
    HOMELAB_CHANNEL_ID,
    FINANCES_CHANNEL_ID,
    VAULT_CHANNEL_ID,
    PT_TZ,
    update_beacon,
)
from tools.bridge_formatting import (
    chunk_text,
    parse_interactive_choices,
)
import tools.bridge_runner as br

LAST_SCHEDULED_DISPATCH = {}
_last_bot_status_mtime = 0.0


async def _run_sidecar_job_async(job_id: str, job_name: str, func, *args, **kwargs):
    """Run synchronous sidecar jobs in a worker thread to keep the asyncio event loop unblocked."""
    import sys as _sys
    import importlib as _importlib
    if "tools.workspace_mcp" in _sys.modules:
        _wmcp = _sys.modules["tools.workspace_mcp"]
        if not hasattr(_wmcp, "_resolve_secrets_path") or not os.path.exists(getattr(_wmcp, "SECRETS_PATH", "")):
            try:
                _importlib.reload(_wmcp)
            except Exception:
                pass
    from tools.sidecars import run_sidecar_job
    return await asyncio.to_thread(run_sidecar_job, job_id, job_name, func, *args, **kwargs)


async def _send_chunked_content(channel, content: str, choice_view=None, max_len: int = 1900, file_path: str | None = None):
    """Defensively chunk text and send each piece, attaching choice_view and optional file_path to the final chunk."""
    chunks = chunk_text(content, max_len=max_len)
    for idx, chunk in enumerate(chunks):
        is_last = (idx == len(chunks) - 1)
        view = choice_view if is_last else None
        discord_file = None
        if is_last and file_path and Path(file_path).exists():
            discord_file = discord.File(str(file_path), filename=Path(file_path).name)
        kwargs = {}
        if view is not None:
            kwargs["view"] = view
        if discord_file is not None:
            kwargs["file"] = discord_file
        await channel.send(chunk, **kwargs)


INTERNAL_SYSTEM_MAINTENANCE_NAMES = {
    "Session Rollover",
    "Daily Session Rollover",
    "Dreaming Memory Consolidation",
    "Dreaming Consolidation",
    "Host 2 Local USB Backup",
    "Host 2 Backup",
    "Host 1 Local USB Backup",
    "Host 1 Backup",
    "Bridge Liveness Watchdog",
    "Bridge Watchdog",
    "Plex Transcode Cleanup",
    "Kalshi Weather Quant Paper Bot",
    "kalshi_paper_bot",
    "Multi-Venue Quant Paper Trading Sweep",
    "Multi-Venue Quantitative Expansion Sweep",
    "quant_daily_sweep",
    "Kalshi Nightly Settlement & Quant Standup",
    "kalshi_night_audit",
    "r/ResidentAgents Watcher & Auto-Approver",
    "resident_agents_watcher",
    "r/ResidentAgents Daily Tips & Tricks Megathread Rotation",
    "resident_agents_daily_tip",
}

INTERNAL_SYSTEM_MAINTENANCE_JOBS = {
    "session_rollover",
    "dreaming",
    "host1_backup",
    "host2_backup",
    "bridge_watchdog",
    "pid_watchdog",
    "plex_cleanup",
    "kalshi_paper_bot",
    "quant_daily_sweep",
    "quant_expansion_sweep",
    "kalshi_night_audit",
    "resident_agents_watcher",
    "resident_agents_daily_tip",
}


async def dispatch_scheduled_prompt(
    prompt: str,
    job_name: str = "Sidecar",
    bot: discord.Client = None,
    turn_queue = None,
    apply_presence_fn = None,
    quick_choice_view_cls = None,
    button_choice_fn = None,
    channel_id: int | None = None,
    job_id: str | None = None,
):
    """Inject a scheduled sidecar prompt into the message queue with anti-storm guard."""
    global LAST_SCHEDULED_DISPATCH


    # Strictly isolate all Kalshi quant paper trading sidecars to #vault
    if (job_id and "kalshi" in job_id.lower()) or "kalshi" in job_name.lower() or "kalshi" in prompt.lower():
        channel_id = VAULT_CHANNEL_ID

    # Anti-storm guard: prevent any job from dispatching more than once per 5 minutes
    now_ts = time.time()
    storm_key = job_id or job_name
    last_disp = LAST_SCHEDULED_DISPATCH.get(storm_key, 0)
    if (now_ts - last_disp) < 300:
        print(f"[Scheduler] Anti-storm block: {job_name} attempted re-trigger within 300s (last: {now_ts - last_disp:.1f}s ago). Dropping.")
        return
    LAST_SCHEDULED_DISPATCH[storm_key] = now_ts
    LAST_SCHEDULED_DISPATCH[job_name] = now_ts

    async def get_dest_channel(default_cid: int = TARGET_CHANNEL_ID):
        if not bot:
            return None
        dest_id = channel_id or default_cid
        try:
            return bot.get_channel(dest_id) or await bot.fetch_channel(dest_id)
        except Exception as e:
            print(f"[Scheduler] Could not fetch channel {dest_id}: {e}")
            return None

    if job_id == "session_rollover" or prompt == "[INTERNAL_SESSION_ROLLOVER]" or job_name in ("Session Rollover", "Daily Session Rollover"):
        try:
            from tools.session_rollover import run_daily_session_rollover
            ok, rep, details = await _run_sidecar_job_async("session_rollover", "Daily Session Rollover", run_daily_session_rollover, bot=bot, dry_run=False)
            if not ok and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(rep)
            rolled_count = len(details.get("rolled_over", [])) if isinstance(details, dict) else 0
            print(f"[Scheduler] Executed daily multi-channel session rollover at 2:00 AM PT (ok={ok}, rolled_over={rolled_count}).")

            # Proactively recycle and re-warm all persistent worker daemons overnight
            try:
                from tools.bridge_daemons import daemon_manager
                await daemon_manager.proactive_nightly_recycle()
                print("[Scheduler] 🟢 Proactively recycled and re-warmed all persistent worker daemons.")
            except Exception as de:
                print(f"[Scheduler] Warning proactively recycling persistent daemons: {de}")
        except Exception as e:
            print(f"[Scheduler] Error executing daily multi-channel session rollover: {e}")
            if bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(f"⚠️ **Daily Session Rollover Error**: {e}")
        return

    # Heartbeat sweep: silent execution unless degraded
    if job_name == "Heartbeat Sweep" or "sidecars.py heartbeat" in prompt:
        try:
            import importlib
            import tools.sidecars
            importlib.reload(tools.sidecars)
            healthy, report, _ = await _run_sidecar_job_async("heartbeat", "Heartbeat Sweep", tools.sidecars.run_heartbeat_sweep)
            if not healthy and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(report)
        except Exception as e:
            print(f"[Scheduler] Heartbeat sweep execution error: {e}")
        return

    # Dated reminders: silent execution unless a reminder is due today
    if job_name == "Dated Reminders" or "sidecars.py reminders" in prompt:
        try:
            from tools.sidecars import run_dated_reminders
            has_due, rep, _ = await _run_sidecar_job_async("reminders", "Dated Reminders", run_dated_reminders)
            if has_due and rep and bot:
                ch = await get_dest_channel()
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
            from tools.sidecars import run_ev9_monitor
            has_digest, rep, plot_path = await _run_sidecar_job_async("ev9", "EV9 Listing Monitor", run_ev9_monitor, force_digest=False)
            if has_digest and rep and bot:
                ch = await get_dest_channel()
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
            from tools.sidecars import run_marketing_sweep
            ok, rep, extra = await _run_sidecar_job_async("marketing", "Marketing Email Sweep", run_marketing_sweep, force=False)
            should_post = extra.get("should_post", False) if isinstance(extra, dict) else (ok and bool(rep and rep.strip()))
            if should_post and rep and rep.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(rep)
            else:
                print("[Scheduler] Marketing sweep: off-week or not due yet (silent).")
        except Exception as e:
            print(f"[Scheduler] Marketing sweep execution error: {e}")
        return

    # Nightly triage & agenda briefing
    if job_name in ("Nightly Triage & Briefing", "Nightly Triage") or "sidecars.py triage" in prompt:
        try:
            from tools.sidecars import run_nightly_triage
            ok, rep, _ = await _run_sidecar_job_async("triage", "Nightly Triage & Briefing", run_nightly_triage)
            if rep and bot:
                ch = await get_dest_channel()
                if ch:
                    for chunk in chunk_text(rep):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] Nightly triage execution error: {e}")
        return

    # Nightly NAS log review: autonomous closed-loop triage
    if job_name in ("NAS Log Review", "NAS Log Review Check") or "sidecars.py nas_logs" in prompt or "nas_log_triage" in prompt:
        try:
            import importlib
            import tools.nas_log_triage
            importlib.reload(tools.nas_log_triage)
            run_nas_log_review = tools.nas_log_triage.run_nas_log_review
            format_autonomous_triage_prompt = tools.nas_log_triage.format_autonomous_triage_prompt
            ok, rep, extra = await _run_sidecar_job_async("nas_logs", "NAS Log Review", run_nas_log_review)
            total_issues = extra.get("total_issues", 0) if isinstance(extra, dict) else 0

            ch = await get_dest_channel(HOMELAB_CHANNEL_ID)
            if total_issues == 0:
                # Nominal state: Crisp 1-line confirmation (or silent if empty)
                if rep and bot and ch:
                    await ch.send(rep)
                print(f"[Scheduler] NAS log review: 0 actionable errors across all containers (nominal).")
                return

            # Candidate issues flagged: hand off to Zero for autonomous forensic investigation & safe remediation
            if bot and turn_queue:
                target_cid = HOMELAB_CHANNEL_ID
                dest_ch = await get_dest_channel(target_cid)
                if not dest_ch:
                    print(f"[Scheduler] Could not fetch channel {target_cid}")
                    return

                triage_prompt = format_autonomous_triage_prompt(extra)
                await turn_queue.put({
                    "prompt": triage_prompt,
                    "status_msg": None,
                    "reply_target": dest_ch,
                    "attachments": [],
                    "is_steer": False,
                    "mode": "home",
                    "channel_id": target_cid
                })
            elif rep and bot and ch:
                for chunk in chunk_text(rep):
                    await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] NAS log review execution error: {e}")
        return


    # Plex transcode cache cleanup: silent unless warning/error
    if job_name == "Plex Transcode Cleanup" or "sidecars.py plex" in prompt:
        try:
            from tools.sidecars import run_plex_session_cleanup
            ok, rep, _ = await _run_sidecar_job_async("plex", "Plex Transcode Cleanup", run_plex_session_cleanup)
            if not ok and rep and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(rep)
        except Exception as e:
            print(f"[Scheduler] Plex transcode cleanup execution error: {e}")
        return

    # Dreaming memory consolidation: headless nightly consolidation
    if job_name in ("Dreaming Memory Consolidation", "Dreaming Consolidation") or "sidecars.py dream" in prompt:
        try:
            from tools.memory_manager import run_dreaming_consolidation
            ok, rep, _ = await _run_sidecar_job_async("dream", "Dreaming Consolidation", run_dreaming_consolidation)
            print(f"[Scheduler] Dreaming memory consolidation completed: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Dreaming consolidation execution error: {e}")
        return

    # Memory doctor weekly audit
    if job_name in ("Memory Doctor Audit", "Memory Doctor") or "sidecars.py doctor" in prompt:
        try:
            from tools.memory_manager import run_memory_doctor
            ok, rep, _ = await _run_sidecar_job_async("doctor", "Memory Doctor Audit", lambda: run_memory_doctor(auto_remediate=True))
            if rep and bot:
                ch = await get_dest_channel()
                if ch:
                    for chunk in chunk_text(rep):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] Memory doctor audit execution error: {e}")
        return

    # Antigravity CLI update check: silent execution unless a new version is available
    if "update_antigravity.py" in prompt or job_name == "Antigravity CLI Check" or job_name == "Antigravity Release Check":
        try:
            from tools.sidecars import run_antigravity_check
            ok, out, _ = await _run_sidecar_job_async("update_antigravity", "Antigravity CLI Check", run_antigravity_check)
            if out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print("[Scheduler] Antigravity CLI check: up to date (silent).")
        except Exception as e:
            print(f"[Scheduler] Antigravity CLI check execution error: {e}")
        return

    # Dockhand container image check: silent execution unless an update is available
    if "dockhand_update.py" in prompt or job_name in ("Dockhand Image Check", "Dockhand Image Update Check"):
        try:
            from tools.sidecars import run_dockhand_update_check
            ok, out, _ = await _run_sidecar_job_async("dockhand_check", "Dockhand Image Check", run_dockhand_update_check)
            if out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print("[Scheduler] Dockhand image check: up to date on both NAS hosts (silent).")
        except Exception as e:
            print(f"[Scheduler] Dockhand image check execution error: {e}")
        return

    # Reddit fantasy baseball scanner: surface high-signal dilemma draft card to #zero-chat
    if "reddit_fantasy" in prompt or "reddit_scan" in prompt or "Reddit Fantasy Baseball" in job_name:
        try:
            from tools.reddit_fantasy_scanner import run_reddit_fantasy_scanner
            ok, out, _ = await _run_sidecar_job_async("reddit_fantasy_scanner", "Reddit Fantasy Baseball Scanner", run_reddit_fantasy_scanner)
            if ok and out and not out.startswith("No new") and not out.startswith("Scanned active") and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print(f"[Scheduler] Reddit fantasy baseball scanner checked: {out}")
        except Exception as e:
            print(f"[Scheduler] Reddit fantasy scanner execution error: {e}")
        return

    # Home Assistant stable update check
    if "ha_update_check.py" in prompt or job_name in ("Home Assistant Stable Update Check", "HA Update Check"):
        try:
            from tools.sidecars import run_ha_update_check
            ok, out, _ = await _run_sidecar_job_async("ha_update_check", "HA Update Check", run_ha_update_check)
            if out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
        except Exception as e:
            print(f"[Scheduler] HA update check execution error: {e}")
        return

    # Home Assistant IoT battery watchdog: silent unless low battery (<15%)
    if "ha_battery_check.py" in prompt or job_name in ("Home Assistant IoT Battery Watchdog", "HA Battery Check"):
        try:
            from tools.sidecars import run_ha_battery_check
            ok, out, _ = await _run_sidecar_job_async("ha_battery", "HA Battery Check", run_ha_battery_check)
            if (("Low Battery Alert" in out) or not ok) and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
        except Exception as e:
            print(f"[Scheduler] HA battery check execution error: {e}")
        return

    # Home Assistant statistical telemetry anomaly check: silent unless zombie or MAD outlier detected
    if "ha_anomaly_detector.py" in prompt or job_name in ("Home Assistant Telemetry Anomaly Check", "HA Telemetry Anomaly Check"):
        try:
            from tools.sidecars import run_ha_anomaly_check
            ok, out, _ = await _run_sidecar_job_async("ha_anomaly_check", "HA Telemetry Anomaly Check", lambda: run_ha_anomaly_check(quiet=True))
            if (("Telemetry Anomaly Report" in out) or not ok) and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
        except Exception as e:
            print(f"[Scheduler] HA anomaly check execution error: {e}")
        return

    # Synology storage & RAID health check: silent unless volume >85% or RAID degraded
    if "nas_storage_check.py" in prompt or job_name in ("Synology Storage & Array Health Check", "NAS Storage Check"):
        try:
            from tools.sidecars import run_nas_storage_check
            ok, out, _ = await _run_sidecar_job_async("nas_storage", "NAS Storage Check", run_nas_storage_check)
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
        except Exception as e:
            print(f"[Scheduler] NAS storage check execution error: {e}")
        return

    # Prowlarr indexer health check: silent unless indexers disabled or throttling
    if "prowlarr" in prompt or job_name in ("Prowlarr Indexer Health Watchdog", "Prowlarr Watchdog"):
        try:
            from tools.sidecars import run_prowlarr_watchdog
            ok, out, _ = await _run_sidecar_job_async("prowlarr_watchdog", "Prowlarr Indexer Watchdog", run_prowlarr_watchdog)
            if out and out != "(nominal - 0 Prowlarr failures)" and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] Prowlarr indexers checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Prowlarr watchdog execution error: {e}")
        return

    # SABnzbd downloader & unpack check: silent unless issues found
    if "sabnzbd" in prompt or job_name in ("SABnzbd Downloader & Unpack Watchdog", "SABnzbd Watchdog"):
        try:
            from tools.sidecars import run_sabnzbd_watchdog
            ok, out, _ = await _run_sidecar_job_async("sabnzbd_watchdog", "SABnzbd Watchdog", run_sabnzbd_watchdog)
            if out and out != "(nominal - 0 SABnzbd failures)" and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] SABnzbd checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] SABnzbd watchdog execution error: {e}")
        return

    # Arr queue & import check: silent unless import issues found
    if "arr_queue" in prompt or job_name in ("Arr Queue & Import Watchdog", "Arr Queue Watchdog", "Arr Queue & Health Watchdog"):
        try:
            from tools.sidecars import run_arr_queue_watchdog
            ok, out, _ = await _run_sidecar_job_async("arr_queue_watchdog", "Arr Queue Watchdog", run_arr_queue_watchdog)
            if out and out != "(nominal - 0 import failures)" and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] Arr queue checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Arr queue watchdog execution error: {e}")
        return

    # Home Assistant integration & re-auth check: silent unless integrations fail
    if "ha_reauth" in prompt or job_name in ("HA Integration & Re-Auth Watchdog", "HA Re-Auth Watchdog"):
        try:
            from tools.sidecars import run_ha_reauth_watchdog
            ok, out, _ = await _run_sidecar_job_async("ha_reauth_watchdog", "HA Re-Auth Watchdog", run_ha_reauth_watchdog)
            if out and out != "(nominal - 0 HA integration failures)" and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] HA re-auth checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] HA re-auth watchdog execution error: {e}")
        return

    # Nest x Yale Lock & Homebridge self-healing watchdog: silent unless self-healing triggers or errors
    if "nest_lock" in prompt or job_name in ("Nest Lock & Homebridge Self-Healing Watchdog", "Nest Lock Watchdog"):
        try:
            from tools.sidecars import run_nest_lock_watchdog
            ok, out, _ = await _run_sidecar_job_async("nest_lock_watchdog", "Nest Lock Watchdog", run_nest_lock_watchdog)
            if out and out != "(nominal - 0 lock failures)" and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] Nest lock checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Nest lock watchdog execution error: {e}")
        return

    # Kometa post-run audit: silent unless critical issues found
    if "kometa" in prompt or job_name in ("Kometa Post-Run Audit", "Kometa Audit"):
        try:
            from tools.sidecars import run_kometa_audit
            ok, out, _ = await _run_sidecar_job_async("kometa_audit", "Kometa Post-Run Audit", run_kometa_audit)
            if out and out != "(nominal - Kometa run clean)" and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] Kometa audit checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Kometa audit execution error: {e}")
        return

    # Cubs game day notifier: silent unless game starting within ~10-15m
    if "cubs_game" in prompt or "cubs_notifier" in prompt or job_name in ("Cubs Game Day Notifier", "Cubs Game Alert"):
        try:
            from tools.sidecars import run_cubs_game_notifier
            ok, out, _ = await _run_sidecar_job_async("cubs_game_notifier", "Cubs Game Day Notifier", run_cubs_game_notifier)
            if out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print("[Scheduler] Cubs game notifier checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Cubs game notifier execution error: {e}")
        return

    # Google Tasks two-way sync: silent unless tasks updated or error
    if "tasks_sync" in prompt or "google_tasks" in prompt or job_name in ("Google Tasks Two-Way Sync", "Google Tasks Sync"):
        try:
            from tools.sidecars import run_tasks_sync
            ok, out, _ = await _run_sidecar_job_async("google_tasks_sync", "Google Tasks Two-Way Sync", run_tasks_sync)
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] Google Tasks sync checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Google Tasks sync execution error: {e}")
        return

    # Host 2 Local USB Backup: silent unless error
    if "backup_host2" in prompt or job_name in ("Host 2 Local USB Backup", "Host 2 Backup"):
        try:
            from tools.sidecars import run_host2_backup
            ok, out, _ = await _run_sidecar_job_async("host2_backup", "Host 2 Local USB Backup", run_host2_backup)
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] Host 2 USB backup completed: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Host 2 backup execution error: {e}")
        return

    # Host 1 Local USB Backup: silent unless error
    if "backup_host1" in prompt or job_name in ("Host 1 Local USB Backup", "Host 1 Backup"):
        try:
            from tools.sidecars import run_host1_backup
            ok, out, _ = await _run_sidecar_job_async("host1_backup", "Host 1 Local USB Backup", run_host1_backup)
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] Host 1 USB backup completed: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Host 1 backup execution error: {e}")
        return

    # PID & Process Headroom Watchdog: silent unless warning/critical
    if "pid_watchdog" in prompt or job_name in ("PID & Process Headroom Watchdog", "PID Watchdog"):
        try:
            from tools.sidecars import run_pid_watchdog
            ok, out, _ = await _run_sidecar_job_async("pid_watchdog", "PID & Process Headroom Watchdog", run_pid_watchdog)
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] PID watchdog checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] PID watchdog execution error: {e}")
        return

    # Bridge Liveness Watchdog: silent unless error / degraded
    if "bridge_watchdog" in prompt or job_name in ("Bridge Liveness Watchdog", "Bridge Watchdog"):
        try:
            from tools.sidecars import run_bridge_watchdog
            ok, out, _ = await _run_sidecar_job_async("bridge_watchdog", "Bridge Liveness Watchdog", run_bridge_watchdog)
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print("[Scheduler] Bridge watchdog checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Bridge watchdog execution error: {e}")
        return

    # Plex Weekly New Media Digest
    if job_id == "plex_weekly_digest" or "plex_weekly_digest.py" in prompt or job_name == "Plex Weekly New Media Digest":
        try:
            from tools.sidecars import run_plex_weekly_digest
            ok, out, _ = await _run_sidecar_job_async("plex_weekly_digest", "Plex Weekly New Media Digest", run_plex_weekly_digest, post=True, tag_all=True)
            if out:
                print(f"[Scheduler] Plex weekly digest executed: {out[:100]}")
        except Exception as e:
            print(f"[Scheduler] Plex weekly digest execution error: {e}")
        return

    # Weekly 3-Dinner Meal Proposal
    if job_id == "weekly_meal_proposal" or "meal_proposal" in prompt or "meal_planner" in prompt or job_name in ("Weekly 3-Dinner Meal Proposal", "Weekly Meal Proposal"):
        try:
            from tools.sidecars import run_weekly_meal_proposal
            ok, out, _ = await _run_sidecar_job_async("weekly_meal_proposal", "Weekly 3-Dinner Meal Proposal", run_weekly_meal_proposal)
            if out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print("[Scheduler] Weekly meal proposal checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Weekly meal proposal execution error: {e}")
        return

    # Weekly Whole Foods Grocery Staging
    if job_id == "weekly_grocery_staging" or "sidecars.py groceries" in prompt or "grocery_staging" in prompt or job_name in ("Weekly Whole Foods Grocery Staging", "Weekly Grocery Staging"):
        try:
            from tools.sidecars import run_weekly_grocery_staging
            ok, out, _ = await _run_sidecar_job_async("weekly_grocery_staging", "Weekly Whole Foods Grocery Staging", run_weekly_grocery_staging)
            if out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print("[Scheduler] Weekly grocery staging checked: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Weekly grocery staging execution error: {e}")
        return

    # Option B Weekly Proactive Digest
    if job_id == "weekly_proactive_digest" or ("weekly_digest.py" in prompt and "plex_weekly_digest" not in prompt) or job_name in ("Option B Weekly Proactive Digest", "Weekly Proactive Digest"):
        try:
            from tools.sidecars import run_weekly_proactive_digest
            ok, out, _ = await _run_sidecar_job_async("weekly_proactive_digest", "Option B Weekly Proactive Digest", run_weekly_proactive_digest)
            if out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    for chunk in chunk_text(out):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] Weekly digest execution error: {e}")
        return


    # Crab Cavern Morning Topic Rotation
    if job_id == "morning_topic_rotation" or "morning_dispatcher.py" in prompt or job_name in ("Crab Cavern Morning Topic Rotation", "Morning Topic Rotation"):
        try:
            from tools.morning_dispatcher import dispatch_morning_topic
            ok, rep, extra = await _run_sidecar_job_async("morning_topic_rotation", "Crab Cavern Morning Topic Rotation", dispatch_morning_topic, dry_run=False)
            res = extra if isinstance(extra, dict) else {}
            print(f"[Scheduler] Crab Cavern morning rotation dispatched: status={res.get('status')}, outbox_id={res.get('outbox_id')}")
        except Exception as e:
            print(f"[Scheduler] Crab Cavern morning rotation dispatch error: {e}")
        return

    # Daily Gemini Quota Card: posts progress bar card to #zero-chat at 7:00 AM PT
    if (
        job_id == "daily_quota_card"
        or "quota_card.py" in prompt
        or ("sidecars.py quota" in prompt and "quota_poller" not in prompt)
        or job_name in ("Daily Gemini Quota Card", "Daily Quota Card")
    ):
        try:
            from tools.quota_card import run_quota_card
            ok, out, _ = await _run_sidecar_job_async("daily_quota_card", "Daily Gemini Quota Card", run_quota_card)
            if out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            print(f"[Scheduler] Daily Gemini quota card dispatched: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Daily Gemini quota card execution error: {e}")
        return

    # Daily Zero & Ivy Context Email Digest: sends and auto-archives to Gmail (silent unless error)
    if job_id == "gemini_context_digest" or "gemini_context_digest.py" in prompt or "sidecars.py context_digest" in prompt or job_name in ("Daily Zero & Ivy Context Email Digest", "Gemini Context Digest"):
        try:
            from tools.gemini_context_digest import run_daily_context_digest
            ok, out, _ = await _run_sidecar_job_async(
                "gemini_context_digest",
                "Daily Zero & Ivy Context Email Digest",
                lambda: run_daily_context_digest(quiet=True),
            )
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await ch.send(out)
            else:
                print(f"[Scheduler] Daily Zero & Ivy context email digest dispatched & archived: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Daily Zero & Ivy context email digest error: {e}")
        return

    # 10-Minute Quota Usage Poller: polls ground-truth quota fractions silently into SQLite
    if job_id == "quota_poller" or "quota_poller.py" in prompt or "sidecars.py quota_poller" in prompt or job_name in ("10-Minute Quota Usage Poller", "Quota Usage Poller"):
        try:
            from tools.quota_poller import run_quota_poller
            ok, out, _ = await _run_sidecar_job_async("quota_poller", "10-Minute Quota Usage Poller", run_quota_poller, quiet=True)
            print(f"[Scheduler] 10-Minute quota poller sampled silently: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] 10-Minute quota poller execution error: {e}")
        return

    # Weekly Personal Data & Gaming Profile Sync: silent unless degraded/error
    if job_id == "personal_data_sync" or "personal_data_sync" in prompt or "sidecars.py personal_sync" in prompt or job_name == "Weekly Personal Data & Gaming Profile Sync":
        try:
            from tools.personal_data_sync import run_personal_data_sync
            ok, out, _ = await _run_sidecar_job_async("personal_data_sync", "Weekly Personal Data & Gaming Profile Sync", run_personal_data_sync, quiet=True)
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await _send_chunked_content(ch, out)
            else:
                print(f"[Scheduler] Personal data sync completed silently: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Personal data sync execution error: {e}")
        return

    # r/ResidentAgents Daily Tips & Tricks Megathread Rotation
    if job_id == "resident_agents_daily_tip" or "resident_agents_daily_tip" in prompt or "resident_agents_tip" in prompt or "Megathread Rotation" in job_name:
        try:
            import importlib
            import tools.resident_agents_watcher
            importlib.reload(tools.resident_agents_watcher)
            RESIDENT_AGENTS_CHANNEL_ID = tools.resident_agents_watcher.RESIDENT_AGENTS_CHANNEL_ID
            run_resident_agents_daily_tip = tools.resident_agents_watcher.run_resident_agents_daily_tip
            ok, out, extra = await _run_sidecar_job_async(
                "resident_agents_daily_tip",
                "r/ResidentAgents Daily Tips & Tricks Megathread Rotation",
                lambda: run_resident_agents_daily_tip(test=False, quiet=True, post_discord=True),
            )
            slot = extra.get("slot", {}) if isinstance(extra, dict) else {}
            print(f"[Scheduler] r/ResidentAgents daily tip rotation: ok={ok}, agent={slot.get('agent')}")
            if ok and slot.get("agent") == "Zero" and not (isinstance(extra, dict) and extra.get("skipped_duplicate")) and bot and turn_queue:
                dest_ch = await get_dest_channel(RESIDENT_AGENTS_CHANNEL_ID)
                if dest_ch:
                    await turn_queue.put({
                        "prompt": (
                            "It is Zero's turn on the daily r/ResidentAgents Tips and Tricks Megathread rotation (`t3_1xbzkxr`). "
                            "Post a new top-level engineering recipe comment (`Problem / Scar -> Pattern / Fix -> Question`) as `u/RA_Zero` "
                            "to `https://www.reddit.com/r/ResidentAgents/comments/1xbzkxr/discussion_tips_and_tricks_megathread/`, "
                            "approve it via `prime416`, and share the comment link in `#r-residentagents`."
                        ),
                        "status_msg": None,
                        "reply_target": dest_ch,
                        "attachments": [],
                        "is_steer": False,
                        "mode": "external",
                        "channel_id": RESIDENT_AGENTS_CHANNEL_ID,
                    })
        except Exception as e:
            print(f"[Scheduler] r/ResidentAgents daily tip rotation error: {e}")
        return

    # r/ResidentAgents Subreddit Watcher & Auto-Approver: zero-token background poller
    if job_id == "resident_agents_watcher" or "resident_agents_watcher" in prompt or "r/ResidentAgents Watcher" in job_name:
        try:
            import importlib
            import tools.resident_agents_watcher
            importlib.reload(tools.resident_agents_watcher)
            RESIDENT_AGENTS_CHANNEL_ID = tools.resident_agents_watcher.RESIDENT_AGENTS_CHANNEL_ID
            run_resident_agents_watcher = tools.resident_agents_watcher.run_resident_agents_watcher
            ok, out, extra = await _run_sidecar_job_async(
                "resident_agents_watcher",
                "r/ResidentAgents Watcher & Auto-Approver",
                lambda: run_resident_agents_watcher(test=False, quiet=True, post_discord=True),
            )
            link_assign = extra.get("link_assignment") if isinstance(extra, dict) else None
            if (
                isinstance(link_assign, dict)
                and link_assign.get("assigned")
                and link_assign.get("slot", {}).get("agent") == "Zero"
                and bot
                and turn_queue
            ):
                dest_ch = await get_dest_channel(RESIDENT_AGENTS_CHANNEL_ID)
                if dest_ch:
                    cand_lines = "\n".join(f"- {c['line']}" for c in link_assign.get("candidates", []))
                    await turn_queue.put({
                        "prompt": (
                            "It is Zero's turn on the daily r/ResidentAgents Link Post rotation. "
                            "Amos posted today's candidate links (`L1`–`L6`):\n"
                            f"{cand_lines}\n\n"
                            "Pick the highest-signal candidate link from the list above, submit a new post to `r/ResidentAgents` as `u/RA_Zero` "
                            "with a bracketed flair prefix (`[Discussion]`, `[Projects]`, `[Post-Mortem]`, or `[Rig Breakdown]`) and a sharp technical take or question, "
                            "and share the live Reddit post link in `#r-residentagents`."
                        ),
                        "status_msg": None,
                        "reply_target": dest_ch,
                        "attachments": [],
                        "is_steer": False,
                        "mode": "external",
                        "channel_id": RESIDENT_AGENTS_CHANNEL_ID,
                    })
            if not ok and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    await _send_chunked_content(ch, out)
            else:
                new_cnt = len(extra.get("new_items", [])) if isinstance(extra, dict) else 0
                app_cnt = len(extra.get("approved_items", [])) if isinstance(extra, dict) else 0
                print(f"[Scheduler] r/ResidentAgents watcher checked: ok={ok}, new={new_cnt}, approved={app_cnt}")
        except Exception as e:
            print(f"[Scheduler] r/ResidentAgents watcher execution error: {e}")
        return

    # Daily birthday reminder: silent execution unless someone has a birthday today
    if job_id == "daily_birthday_reminder" or "birthday_reminder.py" in prompt or job_name in ("Daily Birthday Reminder", "Birthday Reminder") or "sidecars.py birthdays" in prompt:
        try:
            from tools.sidecars import run_birthday_reminders
            ok, out, extra = await _run_sidecar_job_async("daily_birthday_reminder", "Daily Birthday Reminder", run_birthday_reminders)
            has_bday = extra.get("has_items", False) if isinstance(extra, dict) else bool(out and out.strip())
            if has_bday and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print("[Scheduler] Daily birthday reminder checked: 0 birthdays today (silent).")
        except Exception as e:
            print(f"[Scheduler] Daily birthday reminder execution error: {e}")
        return

    # Weekly social & last seen review: silent execution unless qualifying events found
    if job_id == "weekly_social_last_seen_review" or "social_last_seen_review.py" in prompt or job_name in ("Weekly Social & Last Seen Review", "Weekly Social Review") or "sidecars.py social_review" in prompt:
        try:
            from tools.sidecars import run_social_last_seen_review
            ok, out, extra = await _run_sidecar_job_async("weekly_social_review", "Weekly Social & Last Seen Review", run_social_last_seen_review)
            has_events = extra.get("has_items", False) if isinstance(extra, dict) else bool(out and out.strip())
            if has_events and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print("[Scheduler] Weekly social review checked: 0 updates (silent).")
        except Exception as e:
            print(f"[Scheduler] Weekly social review execution error: {e}")
        return

    # Monthly core friends reconnect reminder: silent execution unless unseen core friends found
    if job_id == "monthly_core_friends_reconnect" or "core_friends_reminder.py" in prompt or job_name in ("Monthly Core Friends Social Planning Reminder", "Monthly Core Friends Social Reminder", "Core Friends Reminder") or "sidecars.py core_friends" in prompt:
        try:
            from tools.sidecars import run_core_friends_reminder
            ok, out, extra = await _run_sidecar_job_async("monthly_core_friends_reminder", "Monthly Core Friends Social Reminder", run_core_friends_reminder)
            has_friends = extra.get("has_items", False) if isinstance(extra, dict) else bool(out and out.strip())
            if has_friends and out and out.strip() and bot:
                ch = await get_dest_channel()
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out, quick_choice_view_cls, button_choice_fn)
                    await _send_chunked_content(ch, clean_content, choice_view)
            else:
                print("[Scheduler] Monthly core friends reminder checked: all caught up (silent).")
        except Exception as e:
            print(f"[Scheduler] Monthly core friends reminder execution error: {e}")
        return

    # Market Sandbox Autonomous Daily Standup
    if job_id == "market_standup" or "market_standup.py" in prompt or job_name in ("Market Sandbox Autonomous Daily Standup", "Market Standup"):
        try:
            from tools.market_standup import dispatch_market_standup
            ok, rep, extra = await _run_sidecar_job_async("market_standup", "Market Sandbox Autonomous Daily Standup", dispatch_market_standup, dry_run=False)
            print(f"[Scheduler] Market Sandbox standup dispatched: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Market Sandbox standup dispatch error: {e}")
        return

    # AGORA Daily Steering Briefing
    if job_id == "agora_steering" or "agora_steering.py" in prompt or job_name in ("AGORA Daily Steering Briefing", "AGORA Steering"):
        try:
            from tools.agora_steering import dispatch_agora_steering
            ok, rep, extra = await _run_sidecar_job_async("agora_steering", "AGORA Daily Steering Briefing", dispatch_agora_steering, dry_run=False)
            print(f"[Scheduler] AGORA steering briefing dispatched: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] AGORA steering briefing dispatch error: {e}")
        return

    # Agora Roguelike PM & Board Coordinator: silent execution unless board state changed (dispatches to #agora)
    if job_id == "agora_roguelike_pm" or "agora_roguelike_pm" in prompt or "agora_pm" in prompt or job_name in ("Agora Roguelike PM & Board Coordinator", "Agora Roguelike PM", "Agora Roguelike PM & Task Assignment"):
        try:
            from tools.sidecars import run_agora_roguelike_pm
            dest_cid = channel_id or 1558202642663211169  # #agora
            ok, rep, extra = await _run_sidecar_job_async(
                "agora_roguelike_pm",
                "Agora Roguelike PM & Task Assignment",
                lambda: run_agora_roguelike_pm(dispatch=False, channel=dest_cid)
            )
            print(f"[Scheduler] Agora Roguelike PM completed: ok={ok}, nominal={extra.get('nominal') if isinstance(extra, dict) else False}")
        except Exception as e:
            print(f"[Scheduler] Agora Roguelike PM execution error: {e}")
        return

    # GOTCHI Software Sprint Coordinator: runs every 15m and queues an agent turn when open tasks exist
    if job_id == "gotchi_sprint" or "gotchi_sprint" in prompt or job_name in ("GOTCHI Software Sprint Coordinator", "Gotchi Software Sprint"):
        try:
            from tools.sidecars import run_gotchi_sprint
            ok, rep, extra = await _run_sidecar_job_async(
                "gotchi_sprint",
                "GOTCHI Software Sprint Coordinator",
                lambda: run_gotchi_sprint(force=False, test=False)
            )
            print(f"[Scheduler] GOTCHI Software Sprint completed: ok={ok}")
            if not ok and rep and bot:
                ch = await get_dest_channel(default_cid=1561509863018856468)
                if ch:
                    await ch.send(f"⚠️ **GOTCHI Software Sprint Failure**:\n```\n{rep}\n```")
        except Exception as e:
            print(f"[Scheduler] GOTCHI Software Sprint execution error: {e}")
        return

    # Agora Roguelike Engineering Execution: runs 3m after PM sidecar
    if job_id == "agora_roguelike_engineer" or "agora_roguelike_engineer" in prompt or "agora_engineer" in prompt or job_name in ("Agora Roguelike Engineering Execution", "Agora Roguelike Engineer"):
        try:
            from tools.sidecars import run_agora_roguelike_engineer
            ok, rep, extra = await _run_sidecar_job_async(
                "agora_roguelike_engineer",
                "Agora Roguelike Engineering Execution",
                run_agora_roguelike_engineer
            )
            print(f"[Scheduler] Agora Roguelike Engineer completed: ok={ok}")
            if not ok and rep and bot:
                ch = await get_dest_channel(default_cid=1558202642663211169)
                if ch:
                    await ch.send(f"⚠️ **Agora Roguelike Engineering Failure**:\n```\n{rep}\n```")
        except Exception as e:
            print(f"[Scheduler] Agora Roguelike Engineer execution error: {e}")
        return

    # Monthly Hardcoded Rule & Regex Audit
    if job_id == "monthly_hardcode_regex_audit" or "code_audit" in prompt or "hardcode_regex_audit" in prompt or job_name in ("Monthly Hardcoded Rule & Regex Audit", "Hardcode Regex Audit"):
        try:
            from tools.sidecars import run_hardcode_regex_audit
            ok, rep, _ = await _run_sidecar_job_async("monthly_hardcode_regex_audit", "Monthly Hardcoded Rule & Regex Audit", run_hardcode_regex_audit)
            if rep and bot:
                ch = await get_dest_channel()
                if ch:
                    for chunk in chunk_text(rep):
                        await ch.send(chunk)
        except Exception as e:
            print(f"[Scheduler] Monthly hardcoded rule audit execution error: {e}")
        return

    # Actual Budget automated daily bank sync: silent execution unless error (alerts to #finances)
    if job_id == "actual_bank_sync" or "actual_sync" in prompt or "actual_bank_sync" in prompt or job_name in ("Actual Budget Daily Bank Sync", "Actual Budget Bank Sync"):
        try:
            from tools.sidecars import run_actual_bank_sync
            ok, rep, extra = await _run_sidecar_job_async("actual_bank_sync", "Actual Budget Daily Bank Sync", run_actual_bank_sync)
            if not ok and rep and bot:
                ch = await get_dest_channel(default_cid=FINANCES_CHANNEL_ID)
                if ch:
                    await ch.send(rep)
            else:
                print("[Scheduler] Actual Budget bank sync: nominal (silent).")
        except Exception as e:
            print(f"[Scheduler] Actual Budget bank sync execution error: {e}")
            if bot:
                ch = await get_dest_channel(default_cid=FINANCES_CHANNEL_ID)
                if ch:
                    await ch.send(f"⚠️ **Actual Budget Sync Error**: {e}")
        return

    # Kalshi Paper Trading Evening Review & Model Sync (sole daily consolidated Discord update to #vault)
    if job_id == "kalshi_evening_review" or "sidecars.py kalshi_evening" in prompt or "sidecars.py kalshi_review" in prompt or job_name == "Kalshi Paper Trading Evening Review & Model Sync":
        try:
            from tools.sidecars import run_kalshi_performance_review
            ok, rep, extra = await _run_sidecar_job_async(
                "kalshi_evening_review",
                "Kalshi Paper Trading Evening Review & Model Sync",
                lambda: run_kalshi_performance_review(phase="evening"),
            )
            chart_path = extra.get("chart_path") if isinstance(extra, dict) else "/workspace/data/quant_equity_chart.png"
            if rep and rep.strip() and bot:
                ch = await get_dest_channel(default_cid=VAULT_CHANNEL_ID)
                if ch:
                    await _send_chunked_content(ch, rep, file_path=chart_path)
            print(f"[Scheduler] Kalshi evening review completed: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Kalshi evening review execution error: {e}")
        return

    # Kalshi Nightly Settlement & Quant Standup (silent background settlement & recalibration; alerts #vault only on error)
    if job_id == "kalshi_night_audit" or "sidecars.py kalshi_night" in prompt or "sidecars.py kalshi_audit" in prompt or job_name == "Kalshi Nightly Settlement & Quant Standup":
        try:
            from tools.sidecars import run_kalshi_performance_review
            ok, rep, _ = await _run_sidecar_job_async(
                "kalshi_night_audit",
                "Kalshi Nightly Settlement & Quant Standup",
                lambda: run_kalshi_performance_review(phase="night"),
            )
            if not ok and rep and rep.strip() and bot:
                ch = await get_dest_channel(default_cid=VAULT_CHANNEL_ID)
                if ch:
                    await _send_chunked_content(ch, f"⚠️ **Kalshi Night Audit Error:**\n{rep}")
            else:
                print(f"[Scheduler] Kalshi night audit completed: ok={ok} (silent background).")
        except Exception as e:
            print(f"[Scheduler] Kalshi night audit execution error: {e}")
        return

    # Kalshi Weather Quant Paper Bot (silent morning trade execution; alerts #vault only on error)
    if job_id == "kalshi_paper_bot" or prompt.strip().endswith("sidecars.py kalshi") or "sidecars.py kalshi_bot" in prompt or job_name in ("Kalshi Weather Quant Paper Bot", "kalshi_paper_bot"):
        try:
            from tools.sidecars import run_kalshi_paper_bot
            ok, rep, _ = await _run_sidecar_job_async("kalshi_paper_bot", "Kalshi Weather Quant Paper Bot", run_kalshi_paper_bot)
            if not ok and rep and rep.strip() and bot:
                ch = await get_dest_channel(default_cid=VAULT_CHANNEL_ID)
                if ch:
                    await _send_chunked_content(ch, f"⚠️ **Kalshi Morning Scan Error:**\n{rep}")
            else:
                print(f"[Scheduler] Kalshi paper bot completed: ok={ok} (silent background).")
        except Exception as e:
            print(f"[Scheduler] Kalshi paper bot execution error: {e}")
        return

    # Multi-Venue Quant Paper Trading Sweep (silent morning 4-track sweep; alerts #vault only on error)
    if job_id in ("quant_daily_sweep", "quant_expansion_sweep") or "sidecars.py quant" in prompt or job_name in ("Multi-Venue Quant Paper Trading Sweep", "Multi-Venue Quantitative Expansion Sweep"):
        try:
            from tools.sidecars import run_quant_expansion_sidecar
            ok, rep, _ = await _run_sidecar_job_async(
                "quant_expansion_sweep",
                "Multi-Venue Quantitative Expansion Sweep",
                lambda: run_quant_expansion_sidecar("all"),
            )
            if not ok and rep and rep.strip() and bot:
                ch = await get_dest_channel(default_cid=VAULT_CHANNEL_ID)
                if ch:
                    await _send_chunked_content(ch, f"⚠️ **Quant Daily Sweep Error:**\n{rep}")
            else:
                print(f"[Scheduler] Quant daily sweep completed: ok={ok} (silent background).")
        except Exception as e:
            print(f"[Scheduler] Quant daily sweep execution error: {e}")
        return

    # Monthly Cash Sweep Policy Monitor
    if job_id == "cash_sweep_monitor" or "cash_sweep" in prompt or "sidecars.py sweep" in prompt or job_name == "Monthly Cash Sweep Policy Monitor":
        try:
            from tools.sidecars import run_cash_sweep_monitor
            ok, rep, extra = await _run_sidecar_job_async("cash_sweep_monitor", "Monthly Cash Sweep Policy Monitor", run_cash_sweep_monitor, post=True)
            print(f"[Scheduler] Cash sweep monitor completed: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Cash sweep monitor execution error: {e}")
        return

    # Monthly Financial Model Variance Digest
    if job_id == "monthly_variance_digest" or "variance_digest" in prompt or "sidecars.py variance" in prompt or job_name == "Monthly Financial Model Variance Digest":
        try:
            from tools.sidecars import run_model_variance_digest
            ok, rep, extra = await _run_sidecar_job_async("monthly_variance_digest", "Monthly Financial Model Variance Digest", run_model_variance_digest, post=True)
            print(f"[Scheduler] Model variance digest completed: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Model variance digest execution error: {e}")
        return

    # Daily Token Budget Watchdog
    if job_id == "token_budget_watchdog" or "token_budget" in prompt or "token_budget_watchdog" in prompt or job_name == "Daily Token Budget Watchdog":
        try:
            from tools.sidecars import run_token_budget_watchdog
            ok, rep, extra = await _run_sidecar_job_async("token_budget_watchdog", "Daily Token Budget Watchdog", run_token_budget_watchdog, dispatch=True)
            print(f"[Scheduler] Token budget watchdog completed: ok={ok}")
        except Exception as e:
            print(f"[Scheduler] Token budget watchdog execution error: {e}")
        return

    if bot and turn_queue:
        target_cid = channel_id or TARGET_CHANNEL_ID
        ch = await get_dest_channel()
        if not ch:
            print(f"[Scheduler] Could not fetch channel {target_cid}")
            return

        # Do not send noisy interstitial "scheduled" placeholder messages into Discord scrollback
        mode = "home" if target_cid == TARGET_CHANNEL_ID else "external"
        _model_override = "gemini-3.8-flash-low"
        try:
            from tools.scheduler_tool import load_schedule as _load_sched
            for _j in _load_sched():
                if (job_id and _j.get("id") == job_id) or (job_name and _j.get("name") == job_name):
                    if _j.get("model_override"):
                        _model_override = _j["model_override"]
                    break
        except Exception:
            pass
        await turn_queue.put({
            "prompt": prompt,
            "status_msg": None,
            "reply_target": ch,
            "attachments": [],
            "is_steer": False,
            "mode": mode,
            "channel_id": target_cid,
            "model_override": _model_override,
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

    quiet = job.get("quiet_hours")

    if quiet and len(quiet) == 2:
        start_h, end_h = quiet
        now_pt = datetime.fromtimestamp(now_ts, tz=PT_TZ)
        in_quiet = (start_h <= now_pt.hour < end_h) if start_h < end_h else (now_pt.hour >= start_h or now_pt.hour < end_h)
        if in_quiet:
            return False, f"quiet_hours_{start_h}_to_{end_h}_pt"

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

    if stype == "daily":
        min_period = 86400 * 0.7
        double_run_guard = (last_run >= next_ts - 300)
    elif stype == "weekly":
        min_period = 21600  # 6h guard: prevents same-slot double runs without blocking scheduled morning runs after prior-day manual tests
        double_run_guard = (last_run >= next_ts - 300)
    elif stype == "interval":
        interval = job.get("interval_seconds", 3600)
        min_period = interval * 0.7
        double_run_guard = (last_run >= next_ts)
    else:
        min_period = window
        double_run_guard = (last_run >= next_ts - 300)

    # 1. Guard against double-running if it already ran for this slot or in the current schedule period
    if double_run_guard or (last_run and (now_ts - last_run) < min_period):
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


class BridgeScheduler:
    """Persistent JSON-backed background scheduler for sidecars."""
    def __init__(
        self,
        dispatch_fn,
        bot: discord.Client = None,
        is_busy_fn = None,
        reload_fn = None,
        presence_fn = None,
        quick_choice_view_cls = None,
        button_choice_fn = None,
    ):
        self.dispatch_fn = dispatch_fn
        self.bot = bot
        self.is_busy_fn = is_busy_fn
        self.reload_fn = reload_fn
        self.presence_fn = presence_fn
        self.quick_choice_view_cls = quick_choice_view_cls
        self.button_choice_fn = button_choice_fn
        self._running = False
        self._task = None
        self._heartbeat_task = None
        self._active_job_tasks = set()

    async def _safe_dispatch(self, prompt: str, name: str, cid: int | None, job_id: str | None = None):
        """Safely execute a single scheduled job dispatch in a decoupled background task."""
        try:
            try:
                await self.dispatch_fn(prompt, job_name=name, channel_id=cid, job_id=job_id)
            except TypeError:
                try:
                    await self.dispatch_fn(prompt, job_name=name, channel_id=cid)
                except TypeError:
                    await self.dispatch_fn(prompt, job_name=name)
        except asyncio.CancelledError:
            pass
        except Exception as de:
            print(f"[BridgeScheduler] Error dispatching {name}: {de}")

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

                    prefix = "[BridgeScheduler Startup]" if is_startup else "[BridgeScheduler]"
                    if should_run:
                        j["last_run_ts"] = now_ts
                        j["last_run_at"] = datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M %p PT")
                        print(f"{prefix} Triggering {j['name']} ({reason}, next: {j['next_run_ts']})")
                        jobs_to_dispatch.append((j["prompt"], j["name"], j.get("channel_id"), j.get("id")))
                    else:
                        print(f"{prefix} Skipping {j['name']} ({reason}, advanced next to: {j['next_run_ts']})")

                    updated = True

            # CRITICAL: Persist schedule to disk BEFORE dispatching
            if updated:
                save_schedule(jobs)

            # Dispatch all eligible jobs in decoupled background tasks
            for item in jobs_to_dispatch:
                prompt, name = item[0], item[1]
                cid = item[2] if len(item) > 2 else None
                jid = item[3] if len(item) > 3 else None
                task = asyncio.create_task(self._safe_dispatch(prompt, name, cid, job_id=jid))
                self._active_job_tasks.add(task)
                task.add_done_callback(self._active_job_tasks.discard)

            if jobs_to_dispatch:
                await asyncio.sleep(0)  # Yield so newly created tasks start execution immediately

        except Exception as e:
            print(f"[BridgeScheduler] Error evaluating jobs: {e}")

    async def _heartbeat_loop(self):
        """Dedicated, isolated heartbeat loop running every 15s regardless of main loop activities."""
        while self._running:
            try:
                from tools.bridge_state import update_gateway_heartbeat
                gw_status = "connected" if (self.bot and getattr(self.bot, "is_ready", lambda: False)()) else "disconnected"
                update_gateway_heartbeat(self.bot, status=gw_status)
            except Exception:
                pass
            try:
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break

    async def start(self):
        self._running = True
        # Evaluate missed/due jobs on startup
        await self._evaluate_and_dispatch_jobs(is_startup=True)
        self._task = asyncio.create_task(self._loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for t in list(self._active_job_tasks):
            t.cancel()

    async def flush_outbox_queue(self):
        """Outbox Queue Flusher (Cross-Channel Asynchronous Dispatch)
        
        Safely drains pending outbox messages with:
        - Mandatory chunking via chunk_text(..., max_len=1900) so no single Discord payload exceeds 2,000 chars.
        - Preservation of interactive choice buttons on the final chunk.
        - Banana mutex protection for #the-banana-stand across all chunks.
        - Per-message error isolation so one failure never aborts or drops remaining outbox items.
        - Dead-Letter Queue (failed.jsonl) logging on unhandled delivery errors.
        """
        try:
            from tools.outbox import flush_pending_messages
            pending_outbox = flush_pending_messages()
            if not pending_outbox:
                return

            for omsg in pending_outbox:
                omsg_id = omsg.get("id", "unknown-outbox")
                ch_name = omsg.get("channel", "unknown")
                target_cid = omsg.get("channel_id")
                if not target_cid or not self.bot:
                    continue

                try:
                    target_channel = self.bot.get_channel(target_cid) or await self.bot.fetch_channel(target_cid)
                    if not target_channel:
                        print(f"[Outbox] Warning: Channel {target_cid} not found for {omsg_id}")
                        continue

                    # Check if this outbox item requests an autonomous agent synthesis turn
                    if omsg.get("type") == "agent_turn" or omsg.get("is_agent_turn"):
                        if hasattr(self, "dispatch_fn") and self.dispatch_fn:
                            try:
                                await self.dispatch_fn(
                                    prompt=omsg.get("content", ""),
                                    job_name=omsg.get("source", "Detached Task Synthesis"),
                                    channel_id=target_cid,
                                    job_id=f"outbox_turn_{omsg_id}",
                                )
                                print(f"[Outbox] 🟢 Dispatched agent synthesis turn for {omsg_id} to #{ch_name} ({target_cid}).")
                                continue
                            except Exception as de:
                                print(f"[Outbox] Warning: Failed to dispatch agent turn ({de}). Suppressing prompt dump.")
                        print(f"[Outbox] Warning: Agent turn dispatch unavailable for {omsg_id}. Suppressing raw prompt to chat.")
                        continue

                    is_banana_stand = (target_cid == 1534436119888793750 or str(ch_name) in ("the-banana-stand", "agent-chat"))
                    raw_content = omsg.get("content", "")

                    if is_banana_stand:
                        try:
                            from tools.banana import claim, release
                            claim(subject=omsg_id)
                        except Exception as be:
                            print(f"[Outbox] Warning claiming Banana: {be}")
                        try:
                            # Defensively chunk to guarantee <=1900 chars per Discord REST send call
                            chunks = chunk_text(raw_content, max_len=1900)
                            for ch in chunks:
                                await target_channel.send(ch)
                        finally:
                            try:
                                release()
                            except Exception as err:
                                print(f"[Outbox] Warning releasing Banana: {err}")
                    else:
                        clean_content, choice_view = parse_interactive_choices(
                            raw_content,
                            self.quick_choice_view_cls,
                            self.button_choice_fn,
                        )
                        chunks = chunk_text(clean_content, max_len=1900)
                        for idx, ch in enumerate(chunks):
                            is_last = (idx == len(chunks) - 1)
                            view = choice_view if is_last else None
                            if view:
                                await target_channel.send(ch, view=view)
                            else:
                                await target_channel.send(ch)

                    print(f"[Outbox] Dispatched message {omsg_id} to #{ch_name} ({target_cid})")
                    try:
                        from tools.outbox import record_dispatched_history
                        record_dispatched_history(omsg)
                    except Exception as he:
                        print(f"[Outbox] Warning recording history for {omsg_id}: {he}")
                except Exception as item_err:
                    print(f"[Outbox] Error delivering message {omsg_id} to #{ch_name}: {item_err}")
                    try:
                        dlq_file = DATA_DIR / "outbox" / "failed.jsonl"
                        dlq_file.parent.mkdir(parents=True, exist_ok=True)
                        failed_entry = dict(omsg)
                        failed_entry["failed_at"] = time.time()
                        failed_entry["error"] = str(item_err)
                        with open(dlq_file, "a", encoding="utf-8") as df:
                            df.write(json.dumps(failed_entry) + "\n")
                    except Exception:
                        pass
        except Exception as oe:
            print(f"[Bridge] Error flushing outbox queue: {oe}")

    async def _loop(self):
        global _last_bot_status_mtime
        while self._running:
            try:
                now_ts = time.time()
                await self._evaluate_and_dispatch_jobs(is_startup=False)

                # Dynamic Discord Gateway Heartbeat Verification
                try:
                    from tools.bridge_state import update_gateway_heartbeat
                    gw_status = "connected" if (self.bot and getattr(self.bot, "is_ready", lambda: False)()) else "disconnected"
                    update_gateway_heartbeat(self.bot, status=gw_status)
                except Exception as he:
                    pass

                # Autonomous Subprocess Watchdog & Reaper
                try:
                    from tools.process_probe import reap_stale_agy_processes
                    from tools.bridge_state import get_all_active_pids
                    allowed_pids = set(get_all_active_pids())
                    if br.active_proc and getattr(br.active_proc, "pid", None):
                        allowed_pids.add(br.active_proc.pid)
                    if br.ext_active_proc and getattr(br.ext_active_proc, "pid", None):
                        allowed_pids.add(br.ext_active_proc.pid)
                    for p in getattr(br, "channel_active_procs", {}).values():
                        if p and getattr(p, "pid", None):
                            allowed_pids.add(p.pid)
                    try:
                        from tools.bridge_daemons import daemon_manager
                        for w in daemon_manager.workers.values():
                            if w.proc and getattr(w.proc, "pid", None):
                                allowed_pids.add(w.proc.pid)
                    except Exception:
                        pass
                    reap_stale_agy_processes(allowed_active_pids=allowed_pids)
                except Exception as re_err:
                    pass

                # Liveness Beacon & Wedge Check (Scheduler Pattern: >420s unbroken silence while PROCESSING)
                if BEACON_FILE.exists():
                    try:
                        with open(BEACON_FILE) as bf:
                            bdata = json.load(bf)
                        if bdata.get("state") == "PROCESSING":
                            target_cid = bdata.get("channel_id") or TARGET_CHANNEL_ID
                            target_proc = getattr(br, "channel_active_procs", {}).get(target_cid)
                            if target_proc is None and target_cid == TARGET_CHANNEL_ID:
                                target_proc = br.active_proc
                            has_running_proc = target_proc is not None and getattr(target_proc, "returncode", None) is None
                            if not has_running_proc:
                                # Stale beacon from completed turn: reset silently
                                update_beacon("IDLE", "")
                            else:
                                beacon_ts = bdata.get("ts")
                                if not isinstance(beacon_ts, (int, float)) or beacon_ts <= 0:
                                    update_beacon("IDLE", "")
                                    continue
                                silence = now_ts - beacon_ts
                                if silence < 0 or silence > 3600:
                                    print(f"[WedgeCheck] Stale/invalid beacon timestamp (silence={silence:.0f}s). Resetting to IDLE.")
                                    update_beacon("IDLE", "")
                                    continue
                                if silence > 420 and not bdata.get("alerted"):
                                    print(f"[WedgeCheck] Agent silent for {silence:.0f}s during active turn in channel {target_cid}!")
                                    bdata["alerted"] = True
                                    with open(BEACON_FILE, "w") as bf:
                                        json.dump(bdata, bf)
                                    if self.bot:
                                        ch = self.bot.get_channel(target_cid)
                                        if not ch:
                                            try:
                                                ch = await self.bot.fetch_channel(target_cid)
                                            except Exception:
                                                ch = self.bot.get_channel(TARGET_CHANNEL_ID)
                                        if ch:
                                            p_text = bdata.get('prompt') or '(empty)'
                                            await ch.send(f"⚠️ **Wedge Alert:** Agent has been silent for >{int(silence/60)}m without output. Prompt: `{p_text}`")
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
                                (br.ext_active_proc is not None and br.ext_active_proc.returncode is None) or
                                any(p is not None and getattr(p, "returncode", None) is None for p in getattr(br, "channel_active_procs", {}).values())
                            )
                            if not busy and self.presence_fn:
                                await self.presence_fn()
                    except Exception:
                        pass

                # FORGE Supervisor Tick: expire stale Decision Cards & dispatch completed SDK tasks via Outbox
                try:
                    from tools.forge import forge_supervisor
                    forge_supervisor.tick_sync()
                except Exception:
                    try:
                        from tools.task_settle import check_and_dispatch_completed_tasks
                        check_and_dispatch_completed_tasks()
                    except Exception:
                        pass

                # Outbox Queue Flusher (Cross-Channel Asynchronous Dispatch)
                await self.flush_outbox_queue()

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
                    if not busy:
                        try:
                            reload_flag.unlink()
                        except Exception:
                            pass
                        print(f"[Bridge] Reload flag detected (age={flag_age:.1f}s, bridge idle). Executing in-place reload...")
                        if self.reload_fn:
                            await self.reload_fn(None, initiator="scheduler", force=True, reason="Scheduler reload flag trigger")
                    else:
                        print(f"[Bridge] Reload flag detected (age={flag_age:.1f}s), but bridge is busy ({busy}). Deferring reload until idle...")
            except Exception as e:
                print(f"[BridgeScheduler] Error in loop: {e}")

            await asyncio.sleep(15)

