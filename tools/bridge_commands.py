"""
bridge_commands.py — Operator Commands, UI Interactivity & Lifecycle Interventions.

Core Responsibilities:
1. UI Quick Choices: Discord Button & View classes for interactive turn selection.
2. Bridge Lifecycle: Clean in-place reload (execv) and detached container restart over SSH.
3. Operator Interventions: Bot pause/unpause (Last Word Protocol), Agora kill switch,
   BananaWatcher daemon control, session resets, and active model switching.
4. On-Demand Sidecar Triggers: Mapping and execution of canonical operational slash commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import discord

from tools.bridge_formatting import parse_interactive_choices
from tools.bridge_runner import channel_active_procs
import tools.bridge_runner as br
from tools.bridge_state import (
    DATA_DIR,
    IN_FLIGHT_FILE,
    OWNER_USER_ID,
    TARGET_CHANNEL_ID,
    VAULT_CHANNEL_ID,
    clear_channel_session_id,
    get_active_model,
    get_runtime_rules,
    is_container_restart_intent,
    is_reload_intent,
    record_restart_intent,
    set_active_model,
)

PROCESSED_INTERACTIONS: set[int] = set()

MODEL_ALIASES: dict[str, str] = {
    "argon": "gemini-4-argon-high",
    "argon-high": "gemini-4-argon-high",
    "argon-med": "gemini-4-argon-medium",
    "argon-medium": "gemini-4-argon-medium",
    "argon-low": "gemini-4-argon-low",
    "3.8": "gemini-3.8-flash-high",
    "flash": "gemini-3.8-flash-high",
    "3.8-flash": "gemini-3.8-flash-high",
    "3.8-flash-high": "gemini-3.8-flash-high",
    "3.8-med": "gemini-3.8-flash-medium",
    "3.8-medium": "gemini-3.8-flash-medium",
    "3.8-lite": "gemini-3.8-flash-low",
    "3.8-low": "gemini-3.8-flash-low",
    "3.8-flash-low": "gemini-3.8-flash-low",
    "3.7": "gemini-3.7-flash-high",
    "3.7-flash": "gemini-3.7-flash-high",
    "3.7-flash-high": "gemini-3.7-flash-high",
    "3.7-med": "gemini-3.7-flash-medium",
    "3.7-medium": "gemini-3.7-flash-medium",
    "3.7-lite": "gemini-3.7-flash-low",
    "3.7-low": "gemini-3.7-flash-low",
    "3.7-flash-low": "gemini-3.7-flash-low",
    "flash-low": "gemini-3.8-flash-low",
    "3.5-lite": "gemini-3.7-flash-low",
    "3.5-flash-low": "gemini-3.7-flash-low",
    "3.5": "gemini-3.7-flash-medium",
    "3.6": "gemini-3.6-flash-high",
    "3.6-flash": "gemini-3.6-flash-high",
    "3.6-lite": "gemini-3.6-flash-low",
    "3.6-low": "gemini-3.6-flash-low",
    "3.1-pro": "gemini-3.1-pro-high",
    "pro": "gemini-3.1-pro-high",
    "sonnet": "claude-sonnet-5-5-high",
    "claude": "claude-sonnet-5-5-high",
    "sonnet-5.5": "claude-sonnet-5-5-high",
    "sonnet-5-5": "claude-sonnet-5-5-high",
    "sonnet-high": "claude-sonnet-5-5-high",
    "sonnet-med": "claude-sonnet-5-5-medium",
    "sonnet-medium": "claude-sonnet-5-5-medium",
    "sonnet-low": "claude-sonnet-5-5-low",
    "sonnet-lite": "claude-sonnet-5-5-low",
    "opus": "claude-opus-5-5-high",
    "opus-5.5": "claude-opus-5-5-high",
    "opus-5-5": "claude-opus-5-5-high",
    "opus-high": "claude-opus-5-5-high",
    "opus-med": "claude-opus-5-5-medium",
    "opus-medium": "claude-opus-5-5-medium",
    "opus-low": "claude-opus-5-5-low",
    "opus-lite": "claude-opus-5-5-low",
    "gpt": "gpt-oss-120b-medium",
    "gpt-oss": "gpt-oss-120b-medium",
}

# Canonical On-Demand Jobs Registry (powers `/run <job>` autocomplete and sidecar_audit.py AST verification)
ON_DEMAND_TRIGGERS: dict[str, str] = {
    "/agora_pm": "Run the Agora Roguelike PM board assessment using /workspace/tools/agora_roguelike_pm.py.",
    "/agora_eng": "Run the Agora Roguelike engineering readiness check using /workspace/tools/agora_roguelike_engineer.py.",
    "/gotchi_sprint": "Run the GOTCHI software sprint cycle using /workspace/tools/sidecars.py gotchi_sprint --force.",
    "/heartbeat": "Run the infrastructure heartbeat check using /workspace/tools/sidecars.py heartbeat. Report the status cleanly.",
    "/triage": "Run the nightly agenda & inbox triage briefing using /workspace/tools/sidecars.py triage. Present tomorrow's calendar agenda and priority unread emails.",
    "/logs": "Run the autonomous NAS log review and triage using /workspace/tools/nas_log_triage.py. If issues are found, investigate root causes, apply safe code/config fixes autonomously, and present one-click options for any needed approvals. If clean, report the crisp one-liner.",
    "/plex": "Run the Plex transcode cache cleanup using /workspace/tools/sidecars.py plex. Report status.",
    "/reminders": "Check dated one-shot reminders using /workspace/tools/sidecars.py reminders. Report any due reminders.",
    "/ev9": "Run the Kia EV9 listing monitor using /workspace/tools/sidecars.py ev9 --force. Display the latest listings digest.",
    "/marketing": "Run the promotional email marketing sweep using /workspace/tools/sidecars.py marketing.",
    "/doctor": "Run the memory store audit pass using /workspace/tools/sidecars.py doctor.",
    "/digest": "Generate and post the Option B Weekly Proactive Digest using /workspace/tools/weekly_digest.py. Present upcoming maintenance, 30-day renewals, and cash-flow deltas cleanly.",
    "/tasks": "Show active projects and tasks using /workspace/tools/task_manager.py summary.",
    "/tasks-sync": "Run two-way task sync with Google Tasks using /workspace/tools/sidecars.py tasks_sync. Report any changes cleanly.",
    "/schedule": "Show the current sidecar schedule using /workspace/tools/scheduler_tool.py summary.",
    "/pid_watchdog": "Run the PID and process headroom watchdog check using /workspace/tools/sidecars.py pid_watchdog --test. Report findings cleanly.",
    "/arr_queue": "Run the Arr queue watchdog check using /workspace/tools/sidecars.py arr_queue --force. Report findings and auto-remediations cleanly.",
    "/reauth": "Run the Home Assistant integration & re-auth watchdog using /workspace/tools/sidecars.py ha_reauth --force. Report status.",
    "/nest_lock": "Run the Nest lock & Homebridge self-healing watchdog check using /workspace/tools/sidecars.py nest_lock --force. Report status.",
    "/prowlarr": "Run the Prowlarr indexer health check using /workspace/tools/sidecars.py prowlarr --force. Report status.",
    "/sabnzbd": "Run the SABnzbd downloader check using /workspace/tools/sidecars.py sabnzbd --force. Report status.",
    "/kometa_audit": "Run the Kometa post-run log audit using /workspace/tools/sidecars.py kometa_audit --force. Report status.",
    "/cubs": "Check the Chicago Cubs game schedule using /workspace/tools/sidecars.py cubs --test. Present matchup, start time, and streaming options.",
    "/actual_sync": "Run automated Actual Budget bank sync using /workspace/tools/sidecars.py actual_sync. Silent sidecar (silent execution on routine sync, alert only on MFA reauth or failure).",
    "/cash_sweep": "Run monthly cash sweep policy evaluation using /workspace/tools/sidecars.py cash_sweep. Post liquid reserve status and recommended sweep amount to #finances.",
    "/variance": "Run monthly financial model variance evaluation using /workspace/tools/sidecars.py variance_digest. Post actuals vs model comparison to #finances.",
    "/tokenbudget": "Run daily token budget watchdog evaluation using /workspace/tools/sidecars.py token_budget_watchdog --dispatch --force.",
    "/reddit_scan": "Run the Reddit fantasy baseball scanner using /workspace/tools/sidecars.py reddit_fantasy --post-discord.",
    "/personal_data_sync": "Run the personal data and gaming profile sync using /workspace/tools/sidecars.py personal_data_sync. Report updated stats cleanly.",
    "/sidecars": "Show recent sidecar execution health and failures using /workspace/tools/sidecars.py status.",
    "/mcp": "Show persistent MCP daemon status and endpoint health using /workspace/tools/mcp_daemon.py status.",
    "/mail": "Show inbound email listener status using /workspace/tools/zero_mail_listener.py status.",
    "/morning": "Run the Crab Cavern morning rotation dispatcher using /workspace/tools/morning_dispatcher.py --dispatch.",
    "/birthdays": "Check for friend & family birthdays today using /workspace/tools/birthday_reminder.py. Post any birthdays.",
    "/standup": "Run the Market Sandbox autonomous daily standup using /workspace/tools/market_standup.py --dispatch.",
    "/steering": "Run the daily AGORA Steering meeting briefing using /workspace/tools/agora_steering.py --dispatch.",
    "/agora_roadmap_sprint": "Run the autonomous Agora roadmap worker using python3 /workspace/tools/agora_autoworker.py. Advance the next pending Station Agora roadmap subtask on the public board, implement and verify code and tests, and report progress.",
    "/sideproject": "Execute the autonomous Side-Project Engineering Sprint for Highball and Outpost in #side-project. DO NOT simply run a status script or report static health: check channel history and tasks, pull repos fresh, pick the top open task, write and test code, push to main, restart the service, and report verified receipts tagging Amos (<@1468012353206354197>).",
    "/code_audit": "Run the monthly hardcoded rule & regex audit using /workspace/tools/sidecars.py code_audit. Present findings and architectural recommendations for eliminating brittle heuristics.",
    "/social_review": "Review the past week's social events, calendar, and text messages using /workspace/tools/social_last_seen_review.py.",
    "/core_friends": "Check for local Core friends we have not seen in at least 8 weeks using /workspace/tools/core_friends_reminder.py.",
    "/antigravity_check": "Check for Antigravity CLI updates using /workspace/tools/update_antigravity.py.",
    "/ha_battery_check": "Run the Home Assistant IoT battery watchdog check using /workspace/tools/ha_battery_check.py.",
    "/ha_anomaly_check": "Run the Home Assistant statistical telemetry anomaly check using /workspace/tools/ha_anomaly_detector.py.",
    "/nas_storage_check": "Run the Synology storage & array health check using /workspace/tools/nas_storage_check.py.",
    "/ha_update_check": "Run the Home Assistant stable update check using /workspace/tools/ha_update_check.py.",
    "/dockhand_update": "Run the Dockhand container image check using /workspace/tools/dockhand_update.py.",
    "/backup_host2": "Run the Host 2 local USB backup using /workspace/tools/sidecars.py backup_host2.",
    "/backup_host1": "Run the Host 1 local USB backup using /workspace/tools/sidecars.py backup_host1.",
    "/meals": "Run the weekly 3-dinner meal proposal using /workspace/tools/sidecars.py meal_proposal. Propose the 3 dinners for Tuesday, Thursday, and Sunday nights with interactive swap buttons.",
    "/grocery": "Run the weekly Whole Foods grocery staging using /workspace/tools/sidecars.py grocery_staging. Ingest pending items from Home Assistant ('todo.shopping_list') and due recurring staples, then post the 1-click cart link.",
    "/kalshi": "Run the Kalshi weather quant paper bot using /workspace/tools/sidecars.py kalshi. Settle resolved contracts, scan live order books against NOAA models, execute positive-EV trades, and report the portfolio status.",
    "/kalshi_review": "Run the Kalshi paper trading evening settlement, performance review, and task board sync using /workspace/tools/sidecars.py kalshi_review.",
    "/kalshi_audit": "Run the Kalshi nightly settlement, Brier calibration audit, and GitHub board sync using /workspace/tools/sidecars.py kalshi_audit.",
    "/kalshi_sprint": "Run the 5-minute autonomous Kalshi task board sprint cycle using /workspace/tools/sidecars.py kalshi_sprint.",
    "/quota": "Display the Gemini weekly quota progress bar card using /workspace/tools/sidecars.py quota.",
    "/quota_poller": "Poll ground-truth quota usage snapshots using /workspace/tools/sidecars.py quota_poller.",
    "/context_digest": "Send and auto-archive the Zero & Ivy daily context digest email using /workspace/tools/sidecars.py context_digest.",
    "/resident_agents": "Run the r/ResidentAgents subreddit watcher and auto-approver using /workspace/tools/sidecars.py resident_agents_watcher.",
    "/resident_agents_tip": "Run the daily r/ResidentAgents Tips and Tricks Megathread rotation prompt using /workspace/tools/sidecars.py resident_agents_daily_tip --force.",
    "/morning_inbox_triage_card": "Run the morning Inbox & RCS actionable triage Decision Card workflow using /workspace/tools/sidecars.py morning_inbox_triage_card.",
    "/commute_inbox_triage_card": "Run the commute Inbox & RCS actionable triage Decision Card workflow using /workspace/tools/sidecars.py commute_inbox_triage_card.",
    "/family_calendar_radar_card": "Run the 48h Family Calendar & Action Radar Decision Card workflow using /workspace/tools/sidecars.py family_calendar_radar_card.",
    "/dinner_pivot_card": "Run the afternoon Dinner Defrost & Calendar Conflict Pivot Decision Card workflow using /workspace/tools/sidecars.py dinner_pivot_card.",
    "/actual_budget_anomaly_card": "Run the Actual Budget Anomaly & Overspend Detection Decision Card workflow using /workspace/tools/sidecars.py actual_budget_anomaly_card.",
    "/weather_energy_card": "Run the morning Weather, Air Quality & Pre-Adjustment Energy Decision Card workflow using /workspace/tools/sidecars.py weather_energy_card.",
    "/midweek_grocery_card": "Run the Wednesday Mid-Week Staple & Voice Grocery Replenishment Decision Card workflow using /workspace/tools/sidecars.py midweek_grocery_card.",
    "/evening_pm_build_card": "Run the evening PM Hobby & Build Decision Card workflow using /workspace/tools/sidecars.py evening_pm_build_card.",
    "/usage_telemetry_audit": "Run the weekly Usage Pattern & Signal-to-Noise Telemetry Engine audit using /workspace/tools/sidecars.py usage_telemetry_audit.",
}


class ChoiceButton(discord.ui.Button):
    def __init__(self, label: str, custom_id: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        # Handled by global on_interaction to avoid double execution
        pass


class QuickChoiceView(discord.ui.View):
    def __init__(self, options: list[str], callback_fn=None, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        for opt in options[:5]:
            clean_label = opt.strip()
            if clean_label:
                btn_label = (clean_label[:77] + "...") if len(clean_label) > 80 else clean_label
                self.add_item(ChoiceButton(label=btn_label, custom_id=f"choice:{btn_label}"))


async def execute_bridge_reload(
    bot: Optional[discord.Client] = None,
    channel: Optional[discord.abc.Messageable] = None,
    initiator: str = "user",
    force: bool = True,
    reason: str = "Manual in-place bridge reload requested",
    announce: bool = True,
):
    """Execute clean in-place bridge reload without deadlock."""
    record_restart_intent(reason, initiator=initiator)
    if channel and announce:
        try:
            await channel.send("🔄 Reloading Zero bridge in-place...")
        except Exception:
            pass

    # 0. Pre-Reload Git Synchronization: Auto-sync architecture changes to origin/main
    try:
        from tools.bridge_git_sync import sync_git_on_reload
        sync_res = sync_git_on_reload(reason=reason, initiator=initiator)
        if sync_res.get("synced"):
            print(f"[Bridge] 🚀 Pre-reload git sync: {sync_res.get('message')}")
        elif sync_res.get("clean"):
            print(f"[Bridge] 🟢 Pre-reload git sync: {sync_res.get('message')}")
        elif sync_res.get("error"):
            print(f"[Bridge] ⚠️ Pre-reload git sync notice: {sync_res.get('message')}")
            if channel:
                try:
                    await channel.send(f"⚠️ **Git Sync Notice:** {sync_res.get('message')}")
                except Exception:
                    pass
    except Exception as gse:
        print(f"[Bridge] Warning during pre-reload git sync: {gse}")

    # Terminate persistent daemons and active procs cleanly so they don't linger
    try:
        from tools.bridge_daemons import daemon_manager
        await daemon_manager.shutdown_all()
    except Exception as dse:
        print(f"[Bridge] Warning shutting down persistent daemons: {dse}")

    for cid, proc in list(channel_active_procs.items()):
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass
    if br.active_proc and br.active_proc.returncode is None:
        try:
            br.active_proc.terminate()
        except Exception:
            pass
    if br.ext_active_proc and br.ext_active_proc.returncode is None:
        try:
            br.ext_active_proc.terminate()
        except Exception:
            pass

    # Clean in-flight tracking
    if IN_FLIGHT_FILE.exists():
        try:
            IN_FLIGHT_FILE.unlink()
        except Exception:
            pass

    # Clean pending reload flag so immediate reloads do not re-trigger post-turn
    reload_flag = DATA_DIR / "reload_bridge.flag"
    if reload_flag.exists():
        try:
            reload_flag.unlink()
        except Exception:
            pass
    import tools.bridge_state as _bs
    if getattr(_bs, "DATA_DIR", None) and _bs.DATA_DIR != DATA_DIR:
        alt_reload_flag = _bs.DATA_DIR / "reload_bridge.flag"
        if alt_reload_flag.exists():
            try:
                alt_reload_flag.unlink()
            except Exception:
                pass

    if bot:
        try:
            await bot.close()
        except Exception:
            pass

    # Ensure /app python modules stay synchronized with /workspace/tools before execv
    try:
        ws_tools_dir = Path("/workspace/tools")
        app_dir = Path("/app")
        if ws_tools_dir.exists() and app_dir.exists():
            for py_file in ws_tools_dir.glob("*.py"):
                target = app_dir / py_file.name
                if target.exists():
                    ws_bytes = py_file.read_bytes()
                    if target.read_bytes() != ws_bytes:
                        target.write_bytes(ws_bytes)
                        print(f"[Bridge] Synchronized {py_file} -> {target} before reload")
    except Exception as se:
        print(f"[Bridge] Notice checking /app sync: {se}")

    os.execv(sys.executable, [sys.executable] + sys.argv)


async def execute_container_restart(
    channel: Optional[discord.abc.Messageable] = None,
    initiator: str = "user",
    reason: str = "Manual Docker container restart requested"
):
    """Execute detached Docker container restart on Host 2 via SSH."""
    record_restart_intent(reason, initiator=initiator)
    if channel:
        try:
            await channel.send("🔄 **Restarting Zero Docker container on Host 2 over SSH...**\n• Full cgroup wipe & clean PID 1 reinitialization.")
        except Exception:
            pass

    # Pre-Restart Git Synchronization: Ensure architecture updates are pushed before container wipe
    try:
        from tools.bridge_git_sync import sync_git_on_reload
        sync_res = sync_git_on_reload(reason=reason, initiator=initiator)
        if sync_res.get("synced"):
            print(f"[Bridge] 🚀 Pre-restart git sync: {sync_res.get('message')}")
    except Exception as gse:
        print(f"[Bridge] Warning during pre-restart git sync: {gse}")

    try:
        from tools.nas_docker_mcp import _resolve_nas_config
        _, host_2, ssh_port = _resolve_nas_config()
    except Exception:
        host_2 = os.getenv("NAS_HOST_2_IP", "127.0.0.1")
        ssh_port = os.getenv("NAS_SSH_PORT", str(49000 + 876))

    ssh_key = os.getenv("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
    ssh_user = os.getenv("NAS_USER", "Brock")

    restart_cmd = [
        "ssh", "-i", ssh_key, "-p", str(ssh_port), "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        f"{ssh_user}@{host_2}",
        "nohup sh -c 'sleep 3 && docker restart discord-antigravity-agent' >/dev/null 2>&1 &"
    ]
    try:
        subprocess.run(restart_cmd, timeout=10, check=True)
        print(f"[Bridge] Detached Docker container restart dispatched to {host_2}:{ssh_port}")
    except Exception as e:
        print(f"[Bridge] Error dispatching detached container restart: {e}")
        if channel:
            try:
                await channel.send(f"⚠️ **Failed to dispatch container restart:** `{e}`")
            except Exception:
                pass


async def handle_button_choice(
    choice_text: str,
    interaction: discord.Interaction,
    turn_queue: asyncio.Queue,
    reload_fn: Optional[Callable] = None
):
    """Handle interactive choice button click from user."""
    global PROCESSED_INTERACTIONS
    if interaction.id in PROCESSED_INTERACTIONS:
        return
    PROCESSED_INTERACTIONS.add(interaction.id)
    if len(PROCESSED_INTERACTIONS) > 500:
        PROCESSED_INTERACTIONS.clear()
        PROCESSED_INTERACTIONS.add(interaction.id)

    choice_lower = choice_text.strip().lower()

    # Intercept container restart button choices directly
    if is_container_restart_intent(choice_text):
        await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
        bh = sys.modules.get("tools.bridge_handlers")
        restart_fn = getattr(bh, "execute_container_restart", execute_container_restart) if bh else execute_container_restart
        await restart_fn(interaction.channel, initiator=interaction.user.display_name or interaction.user.name, reason=f"Choice button '{choice_text}' selected")
        return

    # Intercept in-place reload button choices directly
    if is_reload_intent(choice_text):
        await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
        bh = sys.modules.get("tools.bridge_handlers")
        reload_action = reload_fn or (getattr(bh, "execute_bridge_reload", execute_bridge_reload) if bh else execute_bridge_reload)
        initiator_name = interaction.user.display_name or interaction.user.name
        try:
            if reload_fn:
                await reload_fn(channel=interaction.channel, initiator=initiator_name, force=True, reason=f"Choice button '{choice_text}' selected")
            else:
                await reload_action(interaction.client, channel=interaction.channel, initiator=initiator_name, force=True, reason=f"Choice button '{choice_text}' selected")
        except Exception as rerr:
            print(f"[BridgeCommands] Error triggering reload from button: {rerr}")
            try:
                await execute_bridge_reload(interaction.client, channel=interaction.channel, initiator=initiator_name, force=True, reason=f"Choice button '{choice_text}' selected")
            except Exception as rerr2:
                print(f"[BridgeCommands] Fatal fallback reload error: {rerr2}")
        return

    # Intercept session reset button choices directly
    if any(k in choice_lower for k in ("reset session", "restart session", "fresh session", "new session")):
        await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
        sess_key = "home" if (interaction.channel.id == TARGET_CHANNEL_ID) else str(interaction.channel.id)
        clear_channel_session_id(interaction.channel.id, "home")
        br.reset_session_keys.discard(sess_key)
        try:
            bd = sys.modules.get("tools.bridge_daemons")
            dm = getattr(bd, "daemon_manager", None)
            if dm and dm.is_dedicated_channel(interaction.channel.id):
                await dm.recycle_worker(interaction.channel.id, new_conv_id=None)
        except Exception as de:
            print(f"[BridgeCommands] Error recycling worker on button reset: {de}")
        await interaction.channel.send("🔄 Conversation session reset for this channel. Your next message will start a fresh session on the active model.")
        return

    # Intercept Meal Planning choices directly (Fast-Path deterministic execution)
    choice_lower = choice_text.strip().lower()
    if choice_lower in ("lock in menu", "lock in", "lock menu"):
        await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
        try:
            from tools.meal_planner_proposal import lock_in_menu
            ok, rep = await asyncio.to_thread(lock_in_menu)
            await interaction.channel.send(rep)
        except Exception as me:
            await interaction.channel.send(f"⚠️ Failed to lock in menu: {me}")
        return

    if choice_lower in ("swap sunday", "swap tuesday", "swap thursday"):
        await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
        slot = choice_lower.split()[1]
        try:
            from tools.meal_planner_proposal import swap_slot
            ok, rep = await asyncio.to_thread(swap_slot, slot)
            if ok:
                clean_rep, choice_view = parse_interactive_choices(
                    rep,
                    quick_choice_view_cls=QuickChoiceView,
                    button_choice_fn=None,
                )
                if choice_view:
                    await interaction.channel.send(clean_rep, view=choice_view)
                else:
                    await interaction.channel.send(clean_rep)
            else:
                await interaction.channel.send(rep)
        except Exception as se:
            await interaction.channel.send(f"⚠️ Failed to swap {slot}: {se}")
        return

    try:
        await interaction.channel.typing()
    except Exception:
        pass
    selected_msg = await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
    await turn_queue.put({
        "prompt": choice_text,
        "status_msg": None,
        "reply_target": selected_msg,
        "attachments": [],
        "is_steer": False,
        "mode": "home",
        "channel_id": interaction.channel_id,
        "queued_at": time.perf_counter(),
    })


async def handle_operator_command(
    msg: discord.Message,
    bot: discord.Client,
    content: str,
    author_name: str,
    home_turn_queue: asyncio.Queue,
    reload_fn: Optional[Callable] = None,
    rules: Optional[dict] = None,
    is_thread_channel: bool = False,
    active_model_getter: Optional[Callable] = None,
    active_model_setter: Optional[Callable] = None,
) -> bool:
    """Evaluate and execute operator commands, model switching, and sidecar triggers.
    
    Returns True if the message was an operator command and handled; False otherwise.
    """
    if not content:
        return False

    rules = rules or get_runtime_rules()

    # Handle in-flight OAuth interactive token piping
    if br.active_master_fd is not None:
        if msg.author.id != OWNER_USER_ID or msg.channel.id != TARGET_CHANNEL_ID:
            return True
        try:
            print(f"[Antigravity] Piping user input to PTY fd={br.active_master_fd}: {content}")
            os.write(br.active_master_fd, (content + "\n").encode("utf-8"))
            await msg.reply("Auth code received! Finishing authentication...")
        except Exception as e:
            await msg.reply(f"Error forwarding auth code: {e}")
        return True

    # Handle session reset commands
    if content.lower().strip() in ("/reset", "/new"):
        sess_key = "home" if (msg.channel.id == TARGET_CHANNEL_ID) else str(msg.channel.id)
        clear_channel_session_id(msg.channel.id, "home")
        br.reset_session_keys.discard(sess_key)
        try:
            bd = sys.modules.get("tools.bridge_daemons")
            dm = getattr(bd, "daemon_manager", None)
            if dm and dm.is_dedicated_channel(msg.channel.id):
                await dm.recycle_worker(msg.channel.id, new_conv_id=None)
        except Exception as de:
            print(f"[BridgeCommands] Error recycling worker on reset: {de}")
        await msg.reply("🔄 Conversation session reset for this channel/thread. Your next message will start a fresh session.")
        return True

    # Operator command: pause / unpause responses to a bot (default: #lounge)
    pause_match = re.search(
        r"^(?:/pause|pause\s+responses?\s+to|pause\s+responding\s+to|pause)\s+([a-zA-Z0-9_-]+)(?:\s+(?:for\s+)?(\d+)\s*(?:m|min|mins|minutes)?)?(?:\s+(?:in\s+)?(?:channel\s+)?([a-zA-Z0-9_-]+))?$",
        content,
        re.IGNORECASE,
    )
    if pause_match:
        target_bot = pause_match.group(1).strip()
        mins = int(pause_match.group(2)) if pause_match.group(2) else int(rules.get("last_word_pause_minutes", 3))
        target_ch_str = pause_match.group(3)
        target_ch = 1534452820995080192  # Default to #lounge
        if target_ch_str and target_ch_str.isdigit():
            target_ch = int(target_ch_str)
        elif target_ch_str and "banana" in target_ch_str.lower():
            target_ch = 1534436119888793750

        from tools.last_word_protocol import pause_bot
        rec = pause_bot(
            channel_id=target_ch,
            bot_id=target_bot if target_bot.isdigit() else None,
            bot_name=target_bot if not target_bot.isdigit() else None,
            duration_seconds=mins * 60.0,
            reason=f"Operator command from {author_name}",
        )
        await msg.reply(f"⏸️ Responses to **{rec.get('bot_name', target_bot)}** in <#{target_ch}> paused for {mins} minutes (until {rec.get('pause_until_pt')}).")
        return True

    unpause_match = re.search(
        r"^(?:/unpause|resume\s+responses?\s+to|resume\s+responding\s+to|unpause)\s+([a-zA-Z0-9_-]+)(?:\s+(?:in\s+)?(?:channel\s+)?([a-zA-Z0-9_-]+))?$",
        content,
        re.IGNORECASE,
    )
    if unpause_match:
        target_bot = unpause_match.group(1).strip()
        target_ch_str = unpause_match.group(2)
        target_ch = 1534452820995080192
        if target_ch_str and target_ch_str.isdigit():
            target_ch = int(target_ch_str)
        elif target_ch_str and "banana" in target_ch_str.lower():
            target_ch = 1534436119888793750

        from tools.last_word_protocol import unpause_bot
        removed = unpause_bot(target_ch, target_bot)
        if removed:
            await msg.reply(f"▶️ Responses to **{target_bot}** in <#{target_ch}> resumed.")
        else:
            await msg.reply(f"ℹ️ **{target_bot}** was not currently paused in <#{target_ch}>.")
        return True

    # Operator command: silence / unsilence Crab Cavern
    crab_match = re.search(
        r"^(?:/silence_crab|/mute_crab|silence\s+crab(?:\s+cavern)?|mute\s+crab(?:\s+cavern)?)\b(?:\s+(.*))?$",
        content.strip(),
        re.IGNORECASE,
    )
    if crab_match:
        from tools.bridge_state import RUNTIME_RULES_FILE
        rules_data = {}
        if RUNTIME_RULES_FILE.exists():
            with open(RUNTIME_RULES_FILE) as rf:
                rules_data = json.load(rf)
        from tools.bridge_state import parse_silence_until
        try:
            until_dt = parse_silence_until(crab_match.group(1))
        except Exception:
            await msg.reply("⚠️ Couldn't parse that. Use `/crab silence`, `/crab silence 6h`, or `/crab silence tomorrow 8pm`.")
            return True
        rules_data["crab_cavern_silenced_until"] = until_dt.isoformat()
        rules_data["crab_cavern_silence_reason"] = f"Operator command from {author_name}"
        with open(RUNTIME_RULES_FILE, "w") as rf:
            json.dump(rules_data, rf, indent=2)
        await msg.reply(f"🔇 Crab Cavern silenced across all posts and dispatches until **{until_dt.strftime('%A, %b %d, %Y, %I:%M %p')} PT**.")
        return True

    crab_un_match = re.search(
        r"^(?:/unsilence_crab|/unmute_crab|unsilence\s+crab(?:\s+cavern)?|unmute\s+crab(?:\s+cavern)?|resume\s+crab(?:\s+cavern)?)$",
        content.strip(),
        re.IGNORECASE,
    )
    if crab_un_match:
        from tools.bridge_state import RUNTIME_RULES_FILE
        rules_data = {}
        if RUNTIME_RULES_FILE.exists():
            with open(RUNTIME_RULES_FILE) as rf:
                rules_data = json.load(rf)
        rules_data.pop("crab_cavern_silenced_until", None)
        rules_data.pop("crab_cavern_silence_reason", None)
        with open(RUNTIME_RULES_FILE, "w") as rf:
            json.dump(rules_data, rf, indent=2)
        await msg.reply("🔊 Crab Cavern unsilenced. Responses and dispatches resumed.")
        return True

    crab_status_match = re.search(
        r"^(?:/crab_status|crab\s+status)$",
        content.strip(),
        re.IGNORECASE,
    )
    if crab_status_match:
        from tools.bridge_state import is_crab_cavern_silenced
        silenced, reason = is_crab_cavern_silenced()
        if silenced:
            await msg.reply(f"🔇 **Crab Cavern Status:** {reason}")
        else:
            await msg.reply("🔊 **Crab Cavern Status:** Active (not silenced).")
        return True

    # Operator emergency kill switch: /halt / /resume for Station Agora trading
    agora_halt_match = re.search(r"^(?:/halt|/stop)(?:\s+(.*))?$", content.strip(), re.IGNORECASE)
    if agora_halt_match:
        from tools.agora_kill_switch import trigger_kill_switch
        res = trigger_kill_switch(initiator=author_name, action="halt", channel_id=msg.channel.id)
        await msg.reply(res["message"])
        return True

    agora_resume_match = re.search(r"^(?:/resume|/start)(?:\s+(.*))?$", content.strip(), re.IGNORECASE)
    if agora_resume_match:
        from tools.agora_kill_switch import trigger_kill_switch
        res = trigger_kill_switch(initiator=author_name, action="resume", channel_id=msg.channel.id)
        await msg.reply(res["message"])
        return True

    if is_container_restart_intent(content):
        ch_name = getattr(msg.channel, "name", "zero-chat")
        await execute_container_restart(msg.channel, initiator=author_name, reason=f"Manual Docker container restart requested via #{ch_name}")
        return True

    if is_reload_intent(content):
        ch_name = getattr(msg.channel, "name", "zero-chat")
        if reload_fn:
            await reload_fn(msg.channel, initiator=author_name, force=True, reason=f"Manual in-place bridge reload requested via #{ch_name}")
        else:
            await execute_bridge_reload(bot, msg.channel, initiator=author_name, force=True, reason=f"Manual in-place bridge reload requested via #{ch_name}")
        return True

    # Handle BananaWatcher commands
    if content.lower().startswith("/bananawatcher"):
        from tools.banana_watcher import check_channel_and_evaluate, get_daemon_status, load_state, start_daemon, stop_daemon
        parts = content.split(maxsplit=1)
        subcmd = parts[1].strip().lower() if len(parts) > 1 else "status"
        if subcmd == "status":
            st = get_daemon_status()
            state = load_state()
            last_check_str = "Never"
            if state.get("last_check_ts"):
                ago = int(time.time() - state["last_check_ts"])
                last_check_str = f"{ago}s ago"
            await msg.reply(
                f"🍌 **BananaWatcher Status:**\n"
                f"• **Daemon Running:** `{st['running']}` (PID: `{st['pid']}`)\n"
                f"• **Channel:** <#1534436119888793750> (`#the-banana-stand`)\n"
                f"• **Last Evaluation:** `{last_check_str}`\n"
                f"• **Tracked Stalls:** {len(state.get('nudged_stalls', {}))}\n"
                f"• **Tracked Auto-Closes:** {len(state.get('autoclosed_topics', {}))}\n"
                f"• **Tracked Handoffs:** {len(state.get('nudged_handoffs', {}))}\n"
                f"• **Tracked Contradictions:** {len(state.get('warned_contradictions', {}))}\n"
                f"• **Tracked Summaries:** {len(state.get('summarized_subjects', {}))}"
            )
        elif subcmd == "start":
            st = get_daemon_status()
            if st["running"]:
                await msg.reply(f"🍌 **BananaWatcher** is already running with PID `{st['pid']}`.")
            else:
                res = start_daemon()
                await msg.reply(f"🚀 Started **BananaWatcher** daemon (PID: `{res.get('pid')}`).")
        elif subcmd == "stop":
            st = get_daemon_status()
            if not st["running"]:
                await msg.reply("ℹ️ **BananaWatcher** is not currently running.")
            else:
                stop_daemon()
                await msg.reply(f"🛑 Stopped **BananaWatcher** (PID `{st['pid']}`).")
        elif subcmd in ("run-once", "check"):
            actions = check_channel_and_evaluate(dry_run=True)
            await msg.reply(f"🍌 **BananaWatcher Dry Run:** Actions triggered: `{actions or 'None'}`")
        return True

    # Handle model query & switching commands
    if content.lower().startswith("/model"):
        current_model = active_model_getter() if active_model_getter else get_active_model()
        parts = content.split(maxsplit=1)
        if len(parts) == 1:
            from tools.bridge_models import get_fallback_model, list_available_models
            available = await asyncio.to_thread(list_available_models, True)
            fallback = get_fallback_model(available) if available else None
            help_lines = [
                f"🤖 **Current Active Model:** `{current_model}`",
                f"🛟 **Fallback Model:** `{fallback or 'unavailable'}`",
                "",
            ]
            if available:
                help_lines.append("**Available Models (live from the CLI):**")
                help_lines.extend(f"- `{mid}` ({label})" for mid, label in available.items())
                help_lines.append("")
                help_lines.append("*Shortcuts:* `argon`, `argon-med`, `argon-low`, `flash`, `3.8-low`, `sonnet`, `sonnet-medium`, `opus`, `gpt`")
            else:
                help_lines.append("⚠️ Could not fetch the live model list from the CLI. Try again in a moment.")
            help_lines.append("*Switch with:* `/model <name>` | *Set fallback with:* `/model fallback <name>`")
            await msg.reply("\n".join(help_lines))
            return True

        target_m = parts[1].strip()
        from tools.bridge_models import get_fallback_model, list_available_models, resolve_model, set_fallback_model
        available = await asyncio.to_thread(list_available_models, True)
        if not available:
            await msg.reply("⚠️ Couldn't fetch the live model list from the CLI, so the model was not changed. Try again in a moment.")
            return True

        is_fallback_cmd = target_m.lower().split()[0] == "fallback"
        name = target_m.split(maxsplit=1)[1].strip() if is_fallback_cmd and len(target_m.split(maxsplit=1)) > 1 else ("" if is_fallback_cmd else target_m)
        if is_fallback_cmd and not name:
            await msg.reply(f"🛟 Fallback model is `{get_fallback_model(available)}`. Set it with `/model fallback <name>`.")
            return True

        resolved = resolve_model(name, available)
        if not resolved:
            valid = ", ".join(f"`{m}`" for m in available)
            await msg.reply(f"❌ `{name}` isn't a model the CLI knows. Valid models: {valid}")
            return True

        if is_fallback_cmd:
            set_fallback_model(resolved)
            await msg.reply(f"🛟 Fallback model set to **`{resolved}`**. It is used (with a notice in chat) whenever the active model is unavailable.")
            return True

        if active_model_setter:
            active_model_setter(resolved)
        else:
            set_active_model(resolved)
        try:
            bd = sys.modules.get("tools.bridge_daemons")
            dm = getattr(bd, "daemon_manager", None)
            if dm:
                await dm.recycle_all()
        except Exception as de:
            print(f"[BridgeCommands] Error recycling workers: {de}")
        await msg.reply(f"🔄 Switched active model to **`{resolved}`** and recycled persistent workers.")
        return True

    # Handle Zero Live Voice (Gemini Multimodal Live API) commands
    if content.lower().startswith("/voice"):
        from tools.bridge_voice import ZERO_VOICE_CHANNEL_ID, get_voice_manager
        parts = content.split()
        subcmd = parts[1].lower() if len(parts) > 1 else "join"
        mgr = get_voice_manager(bot)

        if subcmd in ("status", "info"):
            st = mgr.get_status_dict()
            if st["active"]:
                await msg.reply(
                    f"🎙️ **Zero Live Voice is ACTIVE** in <#{st['channel_id']}>\n"
                    f"- **Model:** `{st['model']}` | **Voice:** `{st['voice']}`\n"
                    f"- **Uptime:** {st['uptime_seconds']}s | **Audio:** {st['input_audio_seconds']}s in / {st['output_audio_seconds']}s out (~{st['est_audio_tokens']:,} audio tokens)"
                )
            else:
                await msg.reply(
                    f"🔇 **Zero Live Voice is idle.**\n"
                    f"- Hop into <#{ZERO_VOICE_CHANNEL_ID}> (auto-joins) or run `/voice join`."
                )
            return True

        if subcmd in ("leave", "stop", "disconnect", "off"):
            rep = await mgr.stop_session()
            await msg.reply(rep)
            return True

        if subcmd in ("set", "voice") and len(parts) > 2:
            new_voice = parts[2].strip().capitalize()
            mgr.voice_name = new_voice
            if mgr.is_active and mgr.voice_client and mgr.voice_client.channel:
                vc_ch = mgr.voice_client.channel
                await mgr.stop_session()
                ok, rep = await mgr.start_session(vc_ch, notify_channel=msg.channel)
                await msg.reply(f"🔄 Hot-swapped voice to **`{new_voice}`** and reconnected:\n{rep}")
            else:
                await msg.reply(f"🎙️ Default Gemini Live voice updated to **`{new_voice}`** for the next `#zero-voice` session.")
            return True

        # Default: join Ryan's current voice channel or #zero-voice
        target_vc = None
        if getattr(msg.author, "voice", None) and getattr(msg.author.voice, "channel", None):
            target_vc = msg.author.voice.channel
        if not target_vc:
            target_vc = bot.get_channel(ZERO_VOICE_CHANNEL_ID)
        if not target_vc and msg.guild:
            try:
                target_vc = await msg.guild.fetch_channel(ZERO_VOICE_CHANNEL_ID)
            except Exception:
                pass
        if not target_vc:
            await msg.reply("⚠️ Could not locate `#zero-voice` channel.")
            return True

        model_arg = parts[2].strip() if len(parts) > 2 else None
        ok, rep = await mgr.start_session(target_vc, notify_channel=msg.channel, model_override=model_arg)
        await msg.reply(rep)
        return True

    cmd_key = content.lower().split()[0] if content else ""

    # Instant zero-token Gemini Weekly Quota progress bar card
    if cmd_key in ("/quota", "/usage", "/daily_quota_card"):
        from tools.quota_card import build_quota_card
        ok, card, _ = await asyncio.to_thread(build_quota_card)
        await msg.reply(card)
        return True

    # Instant Zero & Ivy Daily Context Digest email dispatch & auto-archive
    if cmd_key in ("/context_digest", "/gemini_context_digest", "/email_digest"):
        from tools.gemini_context_digest import run_daily_context_digest
        ok, rep, _ = await asyncio.to_thread(run_daily_context_digest)
        await msg.reply(f"📨 {rep}" if ok else rep)
        return True

    # Instant zero-token Quota Poller / Attribution command
    if cmd_key in ("/quota_poller", "/quota_poll", "/poller_quota"):
        from tools.quota_poller import (
            run_quota_poller,
            get_latest_quota,
            format_quota_status_text,
            get_quota_attribution,
            format_attribution_text,
        )
        if "--attr" in content.lower() or "-a" in content.lower():
            attr = await asyncio.to_thread(get_quota_attribution, 24.0)
            await msg.reply(format_attribution_text(attr))
        else:
            ok, rep, _ = await asyncio.to_thread(run_quota_poller, False, False)
            latest = await asyncio.to_thread(get_latest_quota)
            status_txt = format_quota_status_text(latest)
            reply_txt = f"{rep}\n\n{status_txt}" if rep else status_txt
            await msg.reply(reply_txt)
        return True

    # On-Demand Thread Rename Command
    if is_thread_channel and cmd_key in ("/title", "/rename"):
        new_name = content[len(cmd_key):].strip()
        if new_name:
            if not new_name.startswith("🧵"):
                new_name = f"🧵 {new_name}"
            try:
                await msg.channel.edit(name=new_name[:100])
                await msg.reply(f"✅ Renamed thread to: **{new_name}**")
            except Exception as re_err:
                await msg.reply(f"⚠️ Failed to rename thread: {re_err}")
        else:
            await msg.reply("Usage: `/title <New Thread Name>`")
        return True

    if cmd_key in ON_DEMAND_TRIGGERS:
        # Strictly isolate Kalshi quant trading commands to #vault
        if cmd_key in ("/kalshi", "/kalshi_review", "/kalshi_audit", "/kalshi_sprint"):
            if getattr(msg.channel, "id", None) != VAULT_CHANNEL_ID:
                await msg.reply(f"🔒 *Kalshi quant trading operations and sidecars exclusively operate in <#{VAULT_CHANNEL_ID}>.*")
                return True

        prompt_text = ON_DEMAND_TRIGGERS[cmd_key]
        try:
            await msg.channel.typing()
        except Exception:
            pass
        await home_turn_queue.put({
            "prompt": prompt_text,
            "status_msg": None,
            "reply_target": msg,
            "attachments": [],
            "is_steer": False,
            "mode": "home",
            "channel_id": msg.channel.id,
            "queued_at": time.perf_counter(),
        })
        return True

    return False