"""
Zero Discord Bridge - Discord Event Handlers, Routing & Dispatcher Module
Encapsulates all Discord bot event listeners (on_ready, on_message, on_interaction),
interaction buttons, thread turn dispatchers, queue workers, presence management,
and in-place reload execution.

Re-exports ambient and command symbols for 100% backward compatibility.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo
import discord

from tools.bridge_ambient import (
    BANANA_WATCHER_BOT_ID,
    PROCESSED_BACKLOG_MSG_IDS,
    channel_last_bot_reply,
    route_external_message,
    warm_channel_history,
)
from tools.bridge_commands import (
    MODEL_ALIASES,
    ON_DEMAND_TRIGGERS,
    PROCESSED_INTERACTIONS,
    ChoiceButton,
    QuickChoiceView,
    execute_bridge_reload,
    execute_container_restart,
    handle_button_choice,
    handle_operator_command,
)
from tools.bridge_state import (
    DATA_DIR,
    ATTACHMENTS_DIR,
    CONFIG_FILE,
    IN_FLIGHT_FILE,
    RESTART_INTENT_FILE,
    QUEUE_FILE,
    EXT_QUEUE_FILE,
    SESSIONS_FILE,
    SESSION_METADATA_FILE,
    BEACON_FILE,
    BOT_STATUS_FILE,
    READONLY_NOTIFICATION_CHANNELS,
    TARGET_CHANNEL_ID,
    OWNER_USER_ID,
    IVY_USER_ID,
    get_runtime_rules,
    get_channel_session_id,
    set_channel_session_id,
    clear_channel_session_id,
    increment_session_turn,
    reset_session_meta,
    check_compaction_needed,
    get_active_model,
    set_active_model,
    save_runtime_config,
    update_beacon,
    is_reload_intent,
    is_container_restart_intent,
    sync_credentials,
    PT_TZ,
    is_home_channel,
    BROCK_GUILD_ID,
    is_brock_guild,
    VAULT_CHANNEL_ID,
)
from tools.bridge_formatting import (
    format_command_preview,
    convert_markdown_tables,
    format_for_discord,
    extract_agent_response,
    chunk_text,
    convert_markdown_to_mobile_html,
    scrub_credentials,
    clean_discord_latex,
    generate_concise_thread_title,
    parse_interactive_choices,
)
from tools.bridge_runner import (
    execute_agy_turn,
    find_new_artifacts,
    channel_active_procs,
    steering_channels,
    reset_session_keys,
    thread_active_tasks,
)
import tools.bridge_runner as br


BOT_BOOT_TIME = time.time()
BANANA_WATCHER_BOT_ID = 1545924520236290198
PROCESSED_INTERACTIONS = set()
PROCESSED_BACKLOG_MSG_IDS = set()
channel_last_bot_reply = {}
active_turn_task = None
active_status_msg = None
has_notified_ready = False

# Multi-Channel Concurrency State
channel_queues = {}             # channel_id -> asyncio.Queue
channel_worker_tasks = {}       # channel_id -> asyncio.Task (worker daemon loop)
channel_active_tasks = {}       # channel_id -> asyncio.Task (active turn processing)
channel_active_status_msgs = {} # channel_id -> discord.Message
channel_concurrency_semaphore = None


def get_channel_queue(channel_id: int | str) -> asyncio.Queue:
    """Get or create dedicated FIFO queue for a specific channel or thread."""
    cid = int(channel_id) if str(channel_id).isdigit() else channel_id
    if cid not in channel_queues:
        channel_queues[cid] = asyncio.Queue()
    return channel_queues[cid]


def get_concurrency_semaphore() -> asyncio.Semaphore:
    """Shared concurrency limiter for background/secondary channels."""
    global channel_concurrency_semaphore
    if channel_concurrency_semaphore is None:
        rules = get_runtime_rules()
        max_workers = int(rules.get("max_parallel_workers", 3))
        channel_concurrency_semaphore = asyncio.Semaphore(max_workers)
    return channel_concurrency_semaphore



async def apply_bot_presence(bot: discord.Client, custom_activity: str = None, status_override: str = None):
    """Update Discord bot rich presence and custom activity string."""
    try:
        if not bot or not bot.is_ready():
            return
        if custom_activity is not None:
            activity = discord.CustomActivity(name=custom_activity[:128])
            st = discord.Status.dnd if status_override == "dnd" else (
                discord.Status.idle if status_override == "idle" else discord.Status.online
            )
            await bot.change_presence(activity=activity, status=st)
            return

        from tools.set_status import get_status
        data = get_status()
        act_text = data.get("activity_text", "Zero is online and ready.")[:128]
        act_type = str(data.get("activity_type", "custom")).lower()
        st_str = str(data.get("status", "online")).lower()

        st_map = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible
        }
        st = st_map.get(st_str, discord.Status.online)

        if act_type == "playing":
            activity = discord.Game(name=act_text)
        elif act_type == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=act_text)
        elif act_type == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=act_text)
        elif act_type == "competing":
            activity = discord.Activity(type=discord.ActivityType.competing, name=act_text)
        else:
            activity = discord.CustomActivity(name=act_text)

        await bot.change_presence(activity=activity, status=st)
    except Exception as e:
        print(f"[Bridge] Error setting bot presence: {e}")




def is_bridge_busy(home_queue=None, ext_queue=None, exclude_channel_id: int | None = None) -> list[str]:
    """Check if any task is actively running or queued across home and external channels."""
    home_busy = (br.active_proc is not None and br.active_proc.returncode is None) or (home_queue is not None and not home_queue.empty())
    ext_busy = (br.ext_active_proc is not None and br.ext_active_proc.returncode is None) or (ext_queue is not None and not ext_queue.empty())
    busy = []
    if home_busy and exclude_channel_id != TARGET_CHANNEL_ID:
        busy.append("#zero-chat")
    if ext_busy:
        busy.append("Crab Cavern")
    for cid, t in channel_active_tasks.items():
        if cid == exclude_channel_id:
            continue
        if t and not t.done():
            ch_name = "#zero-chat" if cid == TARGET_CHANNEL_ID else f"channel:{cid}"
            if ch_name not in busy:
                busy.append(ch_name)
    for cid, q in channel_queues.items():
        if cid == exclude_channel_id and q.empty():
            continue
        if not q.empty():
            ch_name = "#zero-chat" if cid == TARGET_CHANNEL_ID else f"channel:{cid}"
            if ch_name not in busy:
                busy.append(ch_name)
    return busy




async def run_thread_turn_worker(item, bot: discord.Client, presence_fn=None, button_choice_fn=None, quick_choice_view_cls=QuickChoiceView):
    """Concurrent worker executing tasks inside dedicated Discord Threads."""
    prompt = item["prompt"]
    status_msg = item["status_msg"]
    reply_target = item["reply_target"]
    attachments = item["attachments"]
    channel_id = item.get("channel_id", TARGET_CHANNEL_ID)
    rules = get_runtime_rules()
    ticker_enabled = rules.get("live_status_ticker_enabled", False)
    if ticker_enabled and not status_msg and reply_target:
        try:
            if hasattr(reply_target, "reply"):
                status_msg = await reply_target.reply("⏳ *Processing task...*")
            elif hasattr(reply_target, "send"):
                status_msg = await reply_target.send("⏳ *Processing task...*")
        except Exception:
            pass
    try:
        kwargs = {
            "mode": "home",
            "channel_id": channel_id,
            "apply_presence_fn": presence_fn,
            "button_choice_fn": button_choice_fn,
            "quick_choice_view_cls": quick_choice_view_cls,
            "queued_at": item.get("queued_at"),
        }
        if reply_target and hasattr(reply_target, "typing"):
            async with reply_target.typing():
                await execute_agy_turn(prompt, status_msg, reply_target, attachments, **kwargs)
        elif reply_target and hasattr(reply_target, "channel") and hasattr(reply_target.channel, "typing"):
            async with reply_target.channel.typing():
                await execute_agy_turn(prompt, status_msg, reply_target, attachments, **kwargs)
        else:
            await execute_agy_turn(prompt, status_msg, reply_target, attachments, **kwargs)
    except Exception as e:
        print(f"[Thread Worker] Error in thread turn execution: {e}")
        try:
            if hasattr(reply_target, "send"):
                await reply_target.send(f"⚠️ **Error in thread execution:** {e}")
            elif hasattr(reply_target, "reply"):
                await reply_target.reply(f"⚠️ **Error in thread execution:** {e}")
        except Exception:
            pass
    finally:
        tid = getattr(getattr(reply_target, "channel", None), "id", None) or getattr(reply_target, "id", None) or channel_id
        if tid in thread_active_tasks:
            del thread_active_tasks[tid]


async def run_channel_turn_worker(
    channel_id: int,
    ch_queue: asyncio.Queue,
    bot: discord.Client,
    presence_fn=None,
    button_choice_fn=None,
    quick_choice_view_cls=QuickChoiceView,
    reload_fn=None
):
    """Dedicated worker processing turns for a specific channel sequentially."""
    global active_turn_task, active_status_msg
    is_home_root = (channel_id == TARGET_CHANNEL_ID)
    sem = get_concurrency_semaphore()

    try:
        while not ch_queue.empty():
            try:
                item = ch_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            prompt = item["prompt"]
            status_msg = item.get("status_msg")
            reply_target = item.get("reply_target")
            attachments = item.get("attachments", [])

            turn_mode = item.get("mode", "home" if is_home_root else "external")
            author_name = item.get("author_name", "")

            # If live_status_ticker_enabled is False, do not spawn synthetic placeholder messages in home channels.
            # Secondary home channels remain silent with native typing indicators, exactly like #zero-chat.
            rules = get_runtime_rules()
            ticker_enabled = rules.get("live_status_ticker_enabled", False)
            if ticker_enabled and not status_msg and reply_target and not is_home_root and turn_mode == "home":
                try:
                    if hasattr(reply_target, "reply"):
                        status_msg = await reply_target.reply("⏳ *Processing task...*")
                    elif hasattr(reply_target, "send"):
                        status_msg = await reply_target.send("⏳ *Processing task...*")
                except Exception as se:
                    print(f"[Channel Worker {channel_id}] Notice creating status placeholder: {se}")

            channel_active_tasks[channel_id] = asyncio.current_task()
            if is_home_root:
                active_turn_task = asyncio.current_task()
                active_status_msg = status_msg
            if status_msg:
                channel_active_status_msgs[channel_id] = status_msg

            try:
                async def _exec():
                    kwargs = {
                        "mode": turn_mode,
                        "channel_id": channel_id,
                        "author_name": author_name,
                        "apply_presence_fn": presence_fn,
                        "button_choice_fn": button_choice_fn,
                        "quick_choice_view_cls": quick_choice_view_cls,
                        "is_last_word": item.get("is_last_word", False),
                        "last_word_bot_id": item.get("last_word_bot_id"),
                        "last_word_bot_name": item.get("last_word_bot_name"),
                        "last_word_streak": item.get("last_word_streak", 0),
                        "queued_at": item.get("queued_at"),
                    }
                    if reply_target and hasattr(reply_target, "channel") and hasattr(reply_target.channel, "typing"):
                        async with reply_target.channel.typing():
                            await execute_agy_turn(prompt, status_msg, reply_target, attachments, **kwargs)
                    elif reply_target and hasattr(reply_target, "typing"):
                        async with reply_target.typing():
                            await execute_agy_turn(prompt, status_msg, reply_target, attachments, **kwargs)
                    else:
                        await execute_agy_turn(prompt, status_msg, reply_target, attachments, **kwargs)

                # Priority Fast-Lane: Dedicated persistent channels (#zero-chat, #the-banana-stand, #lounge)
                # run immediately without semaphore gating. Secondary background channels acquire semaphore.
                from tools.bridge_daemons import is_dedicated_channel
                if is_home_root or is_dedicated_channel(channel_id):
                    await _exec()
                else:
                    async with sem:
                        await _exec()

            except Exception as e:
                print(f"[Channel Worker {channel_id}] Error in turn execution: {e}")
                try:
                    if hasattr(reply_target, "reply"):
                        await reply_target.reply(f"⚠️ **Error executing task:** {e}")
                    elif hasattr(reply_target, "send"):
                        await reply_target.send(f"⚠️ **Error executing task:** {e}")
                except Exception:
                    pass
            finally:
                channel_active_tasks.pop(channel_id, None)
                if is_home_root:
                    active_turn_task = None
                    active_status_msg = None
                channel_active_status_msgs.pop(channel_id, None)
                ch_queue.task_done()

            # Immediate post-turn check for reload flag (defer if neighbor channels are busy)
            reload_flag = DATA_DIR / "reload_bridge.flag"
            if reload_flag.exists():
                other_busy = is_bridge_busy(exclude_channel_id=channel_id)
                if other_busy:
                    print(f"[Channel Worker {channel_id}] Post-turn reload flag detected, but bridge channels are busy ({other_busy}). Deferring reload.")
                else:
                    try:
                        reload_flag.unlink()
                    except Exception:
                        pass
                    print(f"[Channel Worker {channel_id}] Post-turn reload flag detected (all channels idle). Executing in-place reload...")
                    if reload_fn:
                        await reload_fn(None, initiator="agent", force=True, reason="Post-turn in-place reload flag")
                    else:
                        await execute_bridge_reload(bot, None, initiator="agent", force=True, reason="Post-turn in-place reload flag")

    finally:
        channel_worker_tasks.pop(channel_id, None)
        channel_active_tasks.pop(channel_id, None)


async def queue_worker(home_turn_queue, bot: discord.Client, presence_fn=None, button_choice_fn=None, quick_choice_view_cls=QuickChoiceView, reload_fn=None):
    """Central home dispatcher routing turns to parallel per-channel workers."""
    try:
        while True:
            item = await home_turn_queue.get()
            reply_target = item.get("reply_target")
            channel_id = item.get("channel_id", TARGET_CHANNEL_ID)
            is_thread_task = item.get("is_thread_task", False) or (reply_target and isinstance(getattr(reply_target, "channel", None), discord.Thread))

            if is_thread_task:
                t_task = asyncio.create_task(run_thread_turn_worker(item, bot, presence_fn, button_choice_fn, quick_choice_view_cls))
                tid = getattr(getattr(reply_target, "channel", None), "id", None) or getattr(reply_target, "id", None) or channel_id
                thread_active_tasks[tid] = t_task
                home_turn_queue.task_done()
                continue

            cid = int(channel_id) if str(channel_id).isdigit() else channel_id
            q = get_channel_queue(cid)
            await q.put(item)
            home_turn_queue.task_done()

            existing_task = channel_worker_tasks.get(cid)
            if existing_task is None or existing_task.done():
                t = asyncio.create_task(
                    run_channel_turn_worker(
                        cid, q, bot, presence_fn, button_choice_fn, quick_choice_view_cls, reload_fn
                    )
                )
                channel_worker_tasks[cid] = t
    finally:
        for cid, t in list(channel_worker_tasks.items()):
            if t and not t.done():
                t.cancel()


async def external_queue_worker(ext_turn_queue, bot: discord.Client, presence_fn=None, button_choice_fn=None, quick_choice_view_cls=QuickChoiceView, reload_fn=None):
    """External dispatcher routing turns to parallel per-channel workers."""
    while True:
        item = await ext_turn_queue.get()
        channel_id = item.get("channel_id", 0)
        cid = int(channel_id) if str(channel_id).isdigit() else channel_id
        q = get_channel_queue(cid)
        await q.put(item)
        ext_turn_queue.task_done()

        existing_task = channel_worker_tasks.get(cid)
        if existing_task is None or existing_task.done():
            t = asyncio.create_task(
                run_channel_turn_worker(
                    cid, q, bot, presence_fn, button_choice_fn, quick_choice_view_cls, reload_fn
                )
            )
            channel_worker_tasks[cid] = t


async def handle_on_ready(
    bot: discord.Client,
    turn_queue,
    ext_turn_queue,
    start_workers_fn,
    start_scheduler_fn,
    presence_fn=None,
    reload_fn=None,
):
    """Handle bot startup, presence restoration, credential syncing, and reboot briefings."""
    global has_notified_ready
    sync_credentials()
    update_beacon("IDLE", "")
    if presence_fn:
        await presence_fn()
    print(f"[Antigravity] Logged in as {bot.user} (ID: {bot.user.id})")

    # Ensure Persistent MCP Daemon is active
    try:
        from tools.mcp_daemon import ensure_mcp_daemon_running, get_status
        ensure_mcp_daemon_running()
        mcp_status = get_status()
        print(f"[Antigravity] Persistent MCP Daemon active (PID: {mcp_status.get('pid')}, healthy: {mcp_status.get('healthy')}).")
    except Exception as me:
        print(f"[Bridge] Warning initializing MCP daemon: {me}")

    # Ensure Inbound Email Listener Daemon is active
    try:
        from tools.zero_mail_listener import ensure_mail_listener_running, get_status as get_mail_status
        ensure_mail_listener_running()
        mail_status = get_mail_status()
        print(f"[Antigravity] Inbound Email Listener active (PID: {mail_status.get('pid')}, target: {mail_status.get('target')}).")
    except Exception as mle:
        print(f"[Bridge] Warning initializing email listener: {mle}")

    # Ensure Zero Health Check HTTP Server is active (for zero.brock.ventures)
    try:
        from tools.zero_health_server import ensure_health_server_running, get_status as get_health_status
        ensure_health_server_running()
        health_status = get_health_status()
        print(f"[Antigravity] Zero Health Server active (PID: {health_status.get('pid')}, port: {health_status.get('port')}).")
    except Exception as hse:
        print(f"[Bridge] Warning initializing health server: {hse}")

    # Ensure Banana Watcher Daemon is running in #the-banana-stand
    try:
        from tools.banana_watcher import ensure_banana_watcher_running, get_daemon_status as get_bw_status
        ensure_banana_watcher_running()
        bw_status = get_bw_status()
        print(f"[Antigravity] Banana Watcher active (PID: {bw_status.get('pid')}).")
    except Exception as bwe:
        print(f"[Bridge] Warning initializing Banana Watcher: {bwe}")

    # Ensure Dedicated Persistent Channel Daemons are warmed up (#zero-chat, #the-banana-stand, #lounge)
    try:
        from tools.bridge_daemons import warmup_persistent_daemons
        print("[Antigravity] Warming up dedicated persistent daemons...")
        await warmup_persistent_daemons()
        print("[Antigravity] Dedicated persistent daemons warmed up.")
    except Exception as dme:
        print(f"[Bridge] Warning initializing persistent daemons: {dme}")

    # Clean up any zombie in-flight turn from an interrupted restart
    interrupted_prompt = None
    interrupted_attempts = 1
    if IN_FLIGHT_FILE.exists():
        try:
            with open(IN_FLIGHT_FILE) as f:
                info = json.load(f)
            ch_id = info.get("channel_id") or TARGET_CHANNEL_ID
            msg_id = info.get("status_msg_id")
            interrupted_prompt = info.get("prompt")
            interrupted_attempts = info.get("attempts", 1)
            ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    if msg:
                        if interrupted_attempts >= 2:
                            await msg.edit(content="⚠️ *Turn timed out or was interrupted repeatedly and was cleared from queue to prevent an execution loop. Ready.*")
                        else:
                            await msg.edit(content="⚠️ *Turn was interrupted by a system restart. Recovered and ready.*")
                except Exception:
                    pass
            IN_FLIGHT_FILE.unlink()
        except Exception as e:
            print(f"[Bridge] In-flight recovery error: {e}")
            try:
                IN_FLIGHT_FILE.unlink()
            except Exception:
                pass

    # Check for pending external auth state across restart (e.g. Nintendo Switch nxapi)
    try:
        pending_auth_files = list(DATA_DIR.glob("*_pending_auth.json"))
        for p_file in pending_auth_files:
            try:
                mtime = p_file.stat().st_mtime
                if (time.time() - mtime) < 1800:
                    service_name = p_file.stem.replace("_pending_auth", "").upper()
                    print(f"[Bridge] 🔑 Detected active {service_name} pending auth from before restart ({p_file.name}). State preserved.")
            except Exception:
                pass
    except Exception as pae:
        print(f"[Bridge] Warning scanning pending auth files: {pae}")

    if start_workers_fn:
        start_workers_fn()

    # On initial boot, load persisted turns; on gateway reconnect, preserve active in-memory queue
    if not has_notified_ready:
        if hasattr(turn_queue, "load_persisted"):
            try:
                res = turn_queue.load_persisted()
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, list):
                    for item in res:
                        if isinstance(item, dict) and item.get("prompt"):
                            q_ts = item.get("queued_at", 0)
                            if (time.time() - q_ts) < 900:
                                asyncio.create_task(turn_queue.put(item))
            except Exception as pe:
                print(f"[Bridge] Error loading persisted turn queue: {pe}")

    if start_scheduler_fn:
        await start_scheduler_fn()

    if not has_notified_ready:
        has_notified_ready = True
        ch = bot.get_channel(TARGET_CHANNEL_ID)
        
        # Check restart context
        restart_reason = "System startup / container boot"
        is_intentional = False
        if RESTART_INTENT_FILE.exists():
            try:
                with open(RESTART_INTENT_FILE) as f:
                    r_data = json.load(f)
                    restart_reason = r_data.get("reason", restart_reason)
                    is_intentional = True
                RESTART_INTENT_FILE.unlink()
            except Exception:
                pass

        host_name = os.environ.get("NAS_HOST_2_NAME", "Host2")
        startup_prompt = (
            "[SYSTEM REBOOT & STARTUP EVENT]\n"
            f"You (Zero) just completed a reboot/reload on {host_name}.\n"
            f"• Restart Reason: {restart_reason} {'(Planned Feature Deploy/Update)' if is_intentional else '(System/Container Boot)'}\n"
        )
        if interrupted_prompt:
            if interrupted_attempts >= 2:
                startup_prompt += f"• Interrupted Task Prior to Reboot: \"{interrupted_prompt[:150]}\" (cleared after {interrupted_attempts} attempts to prevent hang loop)\n"
            else:
                startup_prompt += f"• Interrupted Task Prior to Reboot: \"{interrupted_prompt[:150]}\"\n"

        startup_prompt += (
            "\nDeliver a sharp, confident, and proactive restart briefing to Ryan in #zero-chat:\n"
            "1. MUST start your message with the exact standard status header:\n"
            "🟢 **Zero is online and ready.**\n\n"
            "2. Explain concisely why you restarted (e.g. what features were just deployed, upgraded, or recovered).\n"
            "3. Proactively propose 2-3 immediate, actionable next steps or open threads.\n"
            "4. End with interactive [CHOICES: Step 1 | Step 2 | Step 3] buttons."
        )

        if ch:
            try:
                await turn_queue.put({
                    "prompt": startup_prompt,
                    "status_msg": None,
                    "reply_target": ch,
                    "attachments": [],
                    "is_steer": False,
                    "mode": "home",
                    "channel_id": TARGET_CHANNEL_ID,
                    "queued_at": time.perf_counter(),
                })
                print("[Bridge] Successfully queued agentic reboot briefing turn.")
            except Exception as e:
                print(f"[Bridge] Failed to queue startup briefing: {e}")

    # Warm channel history for active sessions and all home domain channels so Zero starts with recent context
    channels_to_warm = set()
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE) as f:
                s_map = json.load(f)
            for ch_key in s_map:
                if ch_key != "home" and str(ch_key).isdigit():
                    channels_to_warm.add(int(ch_key))
        except Exception as e:
            print(f"[Bridge] Error reading sessions for history warming: {e}")

    # Ensure all configured home channels and root #zero-chat are warmed
    rules = get_runtime_rules()
    for hc_id in rules.get("home_channel_ids", []):
        if str(hc_id).isdigit():
            channels_to_warm.add(int(hc_id))
    channels_to_warm.add(TARGET_CHANNEL_ID)

    for ch_id in channels_to_warm:
        try:
            target_ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            if target_ch:
                asyncio.create_task(
                    warm_channel_history(
                        target_ch,
                        limit=25,
                        bot=bot,
                        turn_queue=turn_queue,
                        ext_turn_queue=ext_turn_queue,
                        reload_fn=reload_fn,
                    )
                )
        except Exception as e:
            print(f"[Bridge] Could not fetch channel {ch_id} for history warming: {e}")





async def handle_message(
    msg: discord.Message,
    bot: discord.Client,
    home_turn_queue,
    ext_turn_queue,
    reload_fn=None,
    active_model_getter=None,
    active_model_setter=None,
):
    """Main routing engine for incoming Discord messages across Home and Crab Cavern."""
    global channel_last_bot_reply, active_status_msg, PROCESSED_BACKLOG_MSG_IDS

    if hasattr(msg, "id"):
        PROCESSED_BACKLOG_MSG_IDS.add(msg.id)
        if len(PROCESSED_BACKLOG_MSG_IDS) > 1000:
            PROCESSED_BACKLOG_MSG_IDS = set(list(PROCESSED_BACKLOG_MSG_IDS)[-500:])

    content = msg.content.strip() if hasattr(msg, "content") else ""
    is_thread_channel = isinstance(msg.channel, discord.Thread)
    is_home = is_home_channel(msg.channel)

    # Always record message in channel history buffer for multi-agent situational awareness
    try:
        from tools.channel_history import record_message, get_recent_messages
        author_name = msg.author.display_name or msg.author.name
        ch_name = getattr(msg.channel, "name", str(msg.channel.id))
        reply_id = msg.reference.message_id if msg.reference else None
        record_message(
            channel_id=msg.channel.id,
            channel_name=ch_name,
            author_name=author_name,
            is_bot=msg.author.bot,
            content=content,
            msg_id=msg.id,
            reply_to_id=reply_id,
            timestamp=msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(msg, "created_at") else None
        )
        if len(get_recent_messages(msg.channel.id, limit=5)) <= 1:
            asyncio.create_task(
                warm_channel_history(
                    msg.channel,
                    limit=25,
                    bot=bot,
                    turn_queue=home_turn_queue,
                    ext_turn_queue=ext_turn_queue,
                    reload_fn=reload_fn,
                )
            )
    except Exception as e:
        print(f"[Bridge] Warning recording message to history: {e}")

    # Never reply to ourselves
    if bot.user and msg.author.id == bot.user.id:
        return

    if is_home:
        # Home Turf (#zero-chat): strictly 1-on-1 pairing with Ryan; ignore other bots
        if msg.author.bot:
            return

    # Ignore messages sent prior to current process startup (avoids replaying stale backlog on restart)
    msg_ts = msg.created_at.timestamp() if hasattr(msg, "created_at") else time.time()
    now_ts = time.time()
    if is_home:
        # For home channels (#zero-chat, #zero-ops), never drop recent user commands
        # Only drop if older than 15 minutes across extended outages
        if (now_ts - msg_ts) > 900.0:
            print(f"[Bridge] Dropping stale home message from {author_name} ({now_ts - msg_ts:.1f}s old)")
            return
    else:
        # For external channels, ignore messages older than 5 minutes or sent prior to startup
        if msg_ts < (BOT_BOOT_TIME - 5.0) or (now_ts - msg_ts) > 300.0:
            return



    rules = get_runtime_rules()

    # Route external / public / ambient messages
    if not is_home:
        handled = await route_external_message(
            msg=msg,
            bot=bot,
            content=content,
            author_name=author_name,
            home_turn_queue=home_turn_queue,
            ext_turn_queue=ext_turn_queue,
            reload_fn=reload_fn,
            rules=rules,
        )
        if handled:
            return

    # Route operator commands (resets, bot pause/unpause, Agora kill switch, restarts, model switching, sidecars)
    cmd_handled = await handle_operator_command(
        msg=msg,
        bot=bot,
        content=content,
        author_name=author_name,
        home_turn_queue=home_turn_queue,
        reload_fn=reload_fn,
        rules=rules,
        is_thread_channel=is_thread_channel,
        active_model_getter=active_model_getter,
        active_model_setter=active_model_setter,
    )
    if cmd_handled:
        return

    saved_attachments = []
    if msg.attachments:
        for att in msg.attachments:
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", att.filename)
            dest = ATTACHMENTS_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
            try:
                await att.save(dest)
                saved_attachments.append(str(dest))
            except Exception as e:
                print(f"[Antigravity] Failed to save attachment {att.filename}: {e}")

    # Explicit Thread Triggers in #zero-chat root
    if is_home and not is_thread_channel and content:
        c_low = content.lower()
        if c_low.startswith("thread:") or c_low.startswith("parallel:") or c_low.startswith("/goal") or c_low.startswith("/plan"):
            clean_prompt = re.sub(r"^(thread:|parallel:)\s*", "", content, flags=re.IGNORECASE).strip()
            task_title = generate_concise_thread_title(clean_prompt)
            try:
                thread = await msg.create_thread(name=f"🧵 {task_title}", auto_archive_duration=1440)
                await msg.reply(f"🧵 *Spawned background task thread:* {thread.mention} *(#zero-chat remains free for new tasks)*")
                await home_turn_queue.put({
                    "prompt": clean_prompt,
                    "status_msg": None,
                    "reply_target": thread,
                    "attachments": saved_attachments,
                    "is_steer": False,
                    "mode": "home",
                    "channel_id": thread.id,
                    "is_thread_task": True,
                    "queued_at": time.perf_counter(),
                })
                return
            except Exception as te:
                print(f"[Bridge] Error creating explicit thread: {te}")

    # Build prompt content
    prompt_content = (content or "").strip()
    words = prompt_content.split()
    low_content = prompt_content.lower().rstrip(".?! ")
    is_lazy_home = bool(
        not prompt_content or
        re.fullmatch(r"[\^\s\.\?!]+", prompt_content) or
        low_content in (
            "^", "^^", "^^^", "this", "look", "see", "what?", "check this",
            "investigate", "check", "troubleshoot", "status", "why", "what happened",
            "fix", "fix this", "help", "update", "audit", "thoughts", "look into this"
        ) or
        (len(words) <= 2 and low_content.startswith(("check", "why", "investigate", "fix", "look", "what", "audit")))
    )
    if is_lazy_home and not saved_attachments:
        recent_ctx = ""
        try:
            from tools.channel_history import format_channel_context
            hist = format_channel_context(msg.channel.id, limit=5, include_linked_channels=False)
            if hist and hist.strip():
                recent_ctx = f"\n\n[RECENT DISCORD CHANNEL CONTEXT (Preceding messages in this channel)]:\n{hist.strip()}"
        except Exception as e:
            print(f"[Bridge] Warning formatting channel history for lazy prompt: {e}")

        lazy_context = (
            "[OPERATIONAL DIRECTIVE - LAZY TYPER ADDRESSING]:\n"
            f"The user sent a minimal prompt ('{content.strip()}').\n"
            "Humans are lazy typers: review the preceding Discord channel history below and recent conversation context to identify the topic, alert, question, or task at hand and address it directly with full technical rigor."
            f"{recent_ctx}"
        )
        prompt_content = f"{lazy_context}\n\n{prompt_content}".strip()

    if saved_attachments:
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
        has_images = any(Path(p).suffix.lower() in image_exts for p in saved_attachments)
        image_directive = (
            "\n\n[CRITICAL IMAGE INPUT INVARIANT]:\n"
            "One or more images are attached to this message. You MUST parse and inspect every image (using view_file) and understand its visual contents, error traces, screenshots, or diagrams as part of the primary input, EVEN IF the accompanying text message made no mention of the image."
            if has_images else ""
        )
        hint = "\n\n[Attached file(s) available via view_file tool]:\n" + "\n".join(f"- {p}" for p in saved_attachments)
        if is_lazy_home:
            recent_ctx = ""
            try:
                from tools.channel_history import format_channel_context
                hist = format_channel_context(msg.channel.id, limit=5, include_linked_channels=False)
                if hist and hist.strip():
                    recent_ctx = f"\n\n[RECENT DISCORD CHANNEL CONTEXT (Preceding messages in this channel)]:\n{hist.strip()}"
            except Exception:
                pass
            prompt_content = (
                f"[OPERATIONAL DIRECTIVE - LAZY TYPER & ATTACHMENT ADDRESSING]:\n"
                f"The user sent an attachment with a minimal prompt ('{content.strip()}').\n"
                "Humans are lazy typers: review the attached image/file(s) and preceding Discord channel history to address the active topic directly."
                f"{recent_ctx}"
            )
        prompt_content = (prompt_content or "Please inspect the attached file(s) and assist.") + hint + image_directive

    if not prompt_content:
        return

    # Active Steering Check in THIS channel/thread
    target_proc = channel_active_procs.get(msg.channel.id)
    if target_proc is not None and target_proc.returncode is None:
        steering_channels.add(msg.channel.id)
        try:
            target_proc.send_signal(signal.SIGINT)
        except Exception as se:
            print(f"[Bridge] Warning sending SIGINT for steering in {msg.channel.id}: {se}")

        target_st_msg = channel_active_status_msgs.get(msg.channel.id) or (active_status_msg if msg.channel.id == TARGET_CHANNEL_ID else None)
        if target_st_msg and not is_thread_channel:
            try:
                await target_st_msg.edit(content="~~⏳ [Task paused by new directive below]~~")
            except Exception as e:
                print(f"[Bridge] Failed to edit old status message: {e}")
            if msg.channel.id == TARGET_CHANNEL_ID:
                active_status_msg = None
            channel_active_status_msgs.pop(msg.channel.id, None)

        try:
            await msg.channel.typing()
        except Exception:
            pass
        steer_prompt = (
            f"[USER MID-TURN STEERING UPDATE]\n"
            f"The user provided new instructions while you were in the middle of executing:\n"
            f"\"{prompt_content}\"\n\n"
            f"CRITICAL INSTRUCTIONS FOR REVISED TURN:\n"
            f"1. Absorb this directive immediately.\n"
            f"2. If this invalidates your prior plan or direction, abort redundant tool calls and pivot cleanly.\n"
            f"3. Seamlessly incorporate this guidance into your response without restarting from scratch unless requested."
        )
        await home_turn_queue.put({
            "prompt": steer_prompt,
            "status_msg": None,
            "reply_target": msg,
            "attachments": saved_attachments,
            "is_steer": True,
            "mode": "home",
            "channel_id": msg.channel.id,
            "is_thread_task": is_thread_channel,
            "queued_at": time.perf_counter(),
        })
        return

    try:
        await msg.channel.typing()
    except Exception:
        pass
    await home_turn_queue.put({
        "prompt": prompt_content,
        "status_msg": None,
        "reply_target": msg,
        "attachments": saved_attachments,
        "is_steer": False,
        "mode": "home",
        "channel_id": msg.channel.id,
        "is_thread_task": is_thread_channel,
        "queued_at": time.perf_counter(),
    })
