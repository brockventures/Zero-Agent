"""
Zero Discord Bridge - Discord Event Handlers, Routing & Dispatcher Module
Encapsulates all Discord bot event listeners (on_ready, on_message, on_interaction),
interaction buttons, thread turn dispatchers, queue workers, presence management,
and in-place reload execution.
"""

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
from zoneinfo import ZoneInfo
import discord

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
channel_active_tasks = {}       # channel_id -> asyncio.Task
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


class ChoiceButton(discord.ui.Button):
    def __init__(self, label: str, custom_id: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        # Handled by global on_interaction to avoid double execution
        pass


class QuickChoiceView(discord.ui.View):
    def __init__(self, options: list[str], callback_fn=None, timeout: float = None):
        super().__init__(timeout=timeout)
        for idx, opt in enumerate(options[:5]):
            clean_label = opt.strip()
            if clean_label:
                self.add_item(ChoiceButton(label=clean_label[:80], custom_id=f"choice:{clean_label[:80]}"))


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


async def execute_bridge_reload(bot: discord.Client = None, channel=None, initiator: str = "user", force: bool = True, reason: str = "Manual in-place bridge reload requested"):
    """Execute clean in-place bridge reload without deadlock."""
    from tools.bridge_state import record_restart_intent
    record_restart_intent(reason, initiator=initiator)
    if channel:
        try:
            await channel.send("🔄 Reloading Zero bridge in-place...")
        except Exception:
            pass

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

    if bot:
        try:
            await bot.close()
        except Exception:
            pass

    # Ensure /app/bridge.py stays synchronized with /workspace/tools/bridge.py before execv
    try:
        ws_bridge = Path("/workspace/tools/bridge.py")
        app_bridge = Path("/app/bridge.py")
        if ws_bridge.exists() and app_bridge.exists():
            ws_bytes = ws_bridge.read_bytes()
            if app_bridge.read_bytes() != ws_bytes:
                app_bridge.write_bytes(ws_bytes)
                print("[Bridge] Synchronized /workspace/tools/bridge.py -> /app/bridge.py before reload")
    except Exception as se:
        print(f"[Bridge] Notice checking /app/bridge.py sync: {se}")

    os.execv(sys.executable, [sys.executable] + sys.argv)


async def execute_container_restart(channel=None, initiator: str = "user", reason: str = "Manual Docker container restart requested"):
    """Execute detached Docker container restart on Host 2 via SSH."""
    from tools.bridge_state import record_restart_intent
    record_restart_intent(reason, initiator=initiator)
    if channel:
        try:
            await channel.send("🔄 **Restarting Zero Docker container on Host 2 over SSH...**\n• Full cgroup wipe & clean PID 1 reinitialization.")
        except Exception:
            pass

    import subprocess
    ssh_key = "/secrets/id_ed25519"
    ssh_port = os.getenv("NAS_SSH_PORT", "22")
    ssh_user = os.getenv("NAS_USER", "root")
    host_2 = os.getenv("NAS_HOST_2_IP", "127.0.0.1")

    restart_cmd = [
        "ssh", "-i", ssh_key, "-p", ssh_port, "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
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


async def warm_channel_history(
    channel,
    limit: int = 25,
    bot: discord.Client = None,
    turn_queue = None,
    ext_turn_queue = None,
    reload_fn = None,
):
    """Prefetch recent messages from Discord channel to initialize history buffer and recover unhandled turns."""
    global PROCESSED_BACKLOG_MSG_IDS
    if not channel or not hasattr(channel, "history"):
        return
    try:
        from tools.channel_history import record_message
        msgs = []
        async for m in channel.history(limit=limit):
            msgs.append(m)
        msgs.reverse()
        for m in msgs:
            author_name = m.author.display_name or m.author.name
            reply_id = m.reference.message_id if m.reference else None
            record_message(
                channel_id=channel.id,
                channel_name=getattr(channel, "name", str(channel.id)),
                author_name=author_name,
                is_bot=m.author.bot,
                content=m.content,
                msg_id=m.id,
                reply_to_id=reply_id,
                timestamp=m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(m, "created_at") else None
            )
        print(f"[Bridge] Warmed channel history for #{getattr(channel, 'name', channel.id)}: {len(msgs)} messages loaded.")

        # Backlog Recovery Guard: Check for unhandled user messages in home channels sent during reconnect or downtime
        if is_home_channel(channel) and bot and turn_queue and msgs:
            now_ts = time.time()
            last_msg = msgs[-1]
            if not last_msg.author.bot and last_msg.content.strip():
                msg_age = (now_ts - last_msg.created_at.timestamp()) if hasattr(last_msg, "created_at") else 0
                if 0 <= msg_age <= 900:  # sent within the last 15 minutes
                    if last_msg.id not in PROCESSED_BACKLOG_MSG_IDS:
                        PROCESSED_BACKLOG_MSG_IDS.add(last_msg.id)
                        author_name = last_msg.author.display_name or last_msg.author.name
                        print(f"[Bridge] 🔄 Recovered unhandled home message from {author_name} sent during downtime/reconnect ({last_msg.id}): \"{last_msg.content[:80]}\"")
                        asyncio.create_task(
                            handle_message(
                                msg=last_msg,
                                bot=bot,
                                home_turn_queue=turn_queue,
                                ext_turn_queue=ext_turn_queue,
                                reload_fn=reload_fn,
                            )
                        )
    except Exception as e:
        print(f"[Bridge] Error warming channel history for {channel.id}: {e}")


async def handle_button_choice(choice_text: str, interaction: discord.Interaction, turn_queue, reload_fn=None):
    global PROCESSED_INTERACTIONS
    if interaction.id in PROCESSED_INTERACTIONS:
        return
    PROCESSED_INTERACTIONS.add(interaction.id)
    if len(PROCESSED_INTERACTIONS) > 500:
        PROCESSED_INTERACTIONS.clear()
        PROCESSED_INTERACTIONS.add(interaction.id)

    # Intercept container restart button choices directly
    if is_container_restart_intent(choice_text):
        await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
        await execute_container_restart(interaction.channel, initiator=interaction.user.display_name or interaction.user.name, reason=f"Choice button '{choice_text}' selected")
        return

    # Intercept in-place reload button choices directly
    if is_reload_intent(choice_text):
        await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
        if reload_fn:
            await reload_fn(interaction.channel, initiator=interaction.user.display_name or interaction.user.name, force=True, reason=f"Choice button '{choice_text}' selected")
        else:
            await execute_bridge_reload(interaction.client, interaction.channel, initiator=interaction.user.display_name or interaction.user.name, force=True, reason=f"Choice button '{choice_text}' selected")
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
                home_turn_queue.task_done(item)
                continue

            cid = int(channel_id) if str(channel_id).isdigit() else channel_id
            q = get_channel_queue(cid)
            await q.put(item)
            home_turn_queue.task_done(item)

            existing_task = channel_active_tasks.get(cid)
            if existing_task is None or existing_task.done():
                t = asyncio.create_task(
                    run_channel_turn_worker(
                        cid, q, bot, presence_fn, button_choice_fn, quick_choice_view_cls, reload_fn
                    )
                )
                channel_active_tasks[cid] = t
    finally:
        for cid, t in list(channel_active_tasks.items()):
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
        ext_turn_queue.task_done(item)

        existing_task = channel_active_tasks.get(cid)
        if existing_task is None or existing_task.done():
            t = asyncio.create_task(
                run_channel_turn_worker(
                    cid, q, bot, presence_fn, button_choice_fn, quick_choice_view_cls, reload_fn
                )
            )
            channel_active_tasks[cid] = t


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

    if not is_home:
        # Public channels in Brock Discord (e.g. #seerr-requests-and-chat, #server-updates, #seerr-notifications)
        # Strict Rule: Only respond to messages directly from Ryan Brock (owner), explicitly tagging Zero.
        if is_brock_guild(msg):
            if msg.author.bot:
                return

            is_owner = (msg.author.id == OWNER_USER_ID)
            bot_id = str(bot.user.id) if bot.user else "1542285964213358633"
            is_tagged = (
                (bot.user and bot.user in msg.mentions) or
                f"<@{bot_id}>" in content or
                f"<@!{bot_id}>" in content or
                re.search(r"(?:^|[\s,;/])(?:hey\s+)?@?zero(?:\b|[!?:,/])", content, re.IGNORECASE) is not None
            )

            if not (is_owner and is_tagged):
                return

            # Ryan explicitly invoked Zero in a public Brock Discord channel
            cleaned = content
            cleaned = re.sub(rf"<@!?{bot_id}>", "", cleaned)
            cleaned = re.sub(r"^(hey\s+)?zero[:,\s]*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"@zero\b", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"^(hey\s+)?robot[:,\s]*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"@robot\b", "", cleaned, flags=re.IGNORECASE)
            cleaned = cleaned.strip()

            saved_attachments = []
            if msg.attachments:
                for att in msg.attachments:
                    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", att.filename)
                    dest = ATTACHMENTS_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
                    try:
                        await att.save(dest)
                        saved_attachments.append(str(dest))
                    except Exception as e:
                        print(f"[Bridge] Failed saving attachment: {e}")

            if cleaned.lower() in ("!reset", "/reset", "!new", "/new"):
                clear_channel_session_id(msg.channel.id, "home")
                await msg.reply("🔄 Conversation session reset for this channel.")
                return

            if is_container_restart_intent(cleaned):
                ch_name = getattr(msg.channel, "name", str(msg.channel.id))
                await execute_container_restart(msg.channel, initiator=author_name, reason=f"Manual Docker container restart requested via #{ch_name}")
                return

            if is_reload_intent(cleaned):
                ch_name = getattr(msg.channel, "name", str(msg.channel.id))
                if reload_fn:
                    await reload_fn(msg.channel, initiator=author_name, force=True, reason=f"Manual in-place bridge reload requested via #{ch_name}")
                else:
                    await execute_bridge_reload(bot, msg.channel, initiator=author_name, force=True, reason=f"Manual in-place bridge reload requested via #{ch_name}")
                return

            prompt_content = cleaned
            is_lazy = bool(
                not prompt_content or
                re.fullmatch(r"[\^\s\.\?!]+", prompt_content) or
                prompt_content.lower() in ("^", "^^", "^^^", "this", "look", "see", "what?", "check this")
            )
            if is_lazy and not saved_attachments:
                prompt_content = (
                    "[OPERATIONAL DIRECTIVE - LAZY TYPER ADDRESSING]:\n"
                    f"The user sent a minimal prompt ('{content.strip()}').\n"
                    "Humans are lazy typers: review recent conversation/channel history to identify the topic, question, or task at hand and address it directly."
                )

            if saved_attachments:
                image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
                has_images = any(Path(p).suffix.lower() in image_exts for p in saved_attachments)
                image_directive = (
                    "\n\n[CRITICAL IMAGE INPUT INVARIANT]:\n"
                    "One or more images are attached to this message. You MUST parse and inspect every image (using view_file) and understand its visual contents, error traces, screenshots, or diagrams as part of the primary input, EVEN IF the accompanying text message made no mention of the image."
                    if has_images else ""
                )
                hint = "\n\n[Attached file(s) available via view_file tool]:\n" + "\n".join(f"- {p}" for p in saved_attachments)
                if is_lazy:
                    prompt_content = (
                        f"[OPERATIONAL DIRECTIVE - LAZY TYPER & ATTACHMENT ADDRESSING]:\n"
                        f"The user sent an attachment with a minimal prompt ('{content.strip()}').\n"
                        "Humans are lazy typers: review the attached image/file(s) and recent conversation history to address the active topic directly."
                    )
                prompt_content = (prompt_content or "Please inspect the attached file(s) and assist.") + hint + image_directive

            if not prompt_content:
                return

            ch_name = getattr(msg.channel, "name", str(msg.channel.id))
            ch_ctx_block = ""
            try:
                from tools.channel_history import format_channel_context
                ch_ctx = format_channel_context(msg.channel.id, limit=10, exclude_msg_id=msg.id)
                if ch_ctx:
                    ch_ctx_block = f"\n{ch_ctx}\n\n"
            except Exception:
                pass

            public_prompt = (
                f"[OPERATIONAL DIRECTIVE - BROCK DISCORD PUBLIC CHANNEL #{ch_name}]:\n"
                f"You are responding directly to Ryan in a public channel on Brock Discord.\n"
                f"• Be direct, concise, and helpful (single Discord message, max 2000 characters).\n"
                f"• Public-Safe Etiquette: Do NOT reveal sensitive credentials, tokens, private IPs, or personal family information.\n\n"
                f"{ch_ctx_block}"
                f"{prompt_content}"
            )

            await home_turn_queue.put({
                "prompt": public_prompt,
                "status_msg": None,
                "reply_target": msg,
                "attachments": saved_attachments,
                "is_steer": False,
                "mode": "home",
                "channel_id": msg.channel.id,
                "author_name": author_name,
                "queued_at": time.perf_counter(),
            })
            return

        # Ignore automated notification / webhook channels unless directly tagged
        ch_name = getattr(msg.channel, "name", "").lower()
        if (msg.channel.id in READONLY_NOTIFICATION_CHANNELS or ch_name in ("server-updates", "downloads")) and not (bot.user and bot.user in msg.mentions):
            return

        # Crab Cavern & External / Shared Space Mode (Crab Cavern Protocol)
        from tools.channel_history import is_handoff_addressed_to_zero
        handoff_for_zero = is_handoff_addressed_to_zero(content)

        is_banana_watcher = (
            getattr(msg.author, "id", None) == BANANA_WATCHER_BOT_ID or
            str(getattr(msg.author, "id", "")) == str(BANANA_WATCHER_BOT_ID) or
            "banana watcher" in author_name.lower()
        )
        is_banana_summary_prompt = bool(
            is_banana_watcher and
            re.search(r"🍌 \*\*Discussion Concluded\*\*: Topic `(?P<subject>[^`]+)` has reached resolution", content)
        )
        is_banana_stall_prompt = bool(
            is_banana_watcher and
            re.search(r"🍌 \*\*Topic Stalled\*\*: Topic `(?P<subject>[^`]+)`", content)
        )
        is_banana_loop_prompt = bool(
            is_banana_watcher and
            re.search(r"🍌 \*\*Loop Warning\*\*: Topic `(?P<subject>[^`]+)`", content)
        )
        if is_banana_summary_prompt or is_banana_stall_prompt or is_banana_loop_prompt:
            handoff_for_zero = True

        # Envelope evaluation & topic resolution
        try:
            from tools.handoff import parse_envelope
            envelope = parse_envelope(content)
            if envelope:
                import importlib
                import tools.topic_tracker
                importlib.reload(tools.topic_tracker)
                tools.topic_tracker.check_and_resolve_topic(envelope, content, author_name, msg.id, msg.channel.id)
                floor_state = str(envelope.get("floor") or "").lower()
                # floor: closed is the explicit state indicating "do not reply"
                if floor_state == "closed" and not handoff_for_zero:
                    print(f"[Bridge] Suppressed turn: floor is closed per envelope from {author_name}")
                    return
        except Exception as te:
            print(f"[Bridge] Error checking topic resolution: {te}")

        # 1. Loop prevention for peer bots (Amos, Marvin, etc.)
        # Banana Watcher is an automated channel watchdog/referee, exempt from Last Word Protocol and cascade cooldown
        if msg.author.bot and not is_banana_watcher:
            # Check Last Word Protocol active cooldown or in-flight turn
            try:
                from tools.last_word_protocol import is_bot_paused, is_last_word_in_flight
                paused, rem, rec = is_bot_paused(msg.channel.id, msg.author.id, author_name)
                if paused or is_last_word_in_flight(msg.channel.id, str(msg.author.id)) or is_last_word_in_flight(msg.channel.id, author_name):
                    print(f"[Bridge] Suppressed message from bot {author_name} ({msg.author.id}) in channel {msg.channel.id}: paused under Last Word Protocol ({rem:.0f}s remaining)")
                    return
            except Exception as pe:
                print(f"[Bridge] Error checking bot cooldown: {pe}")

            if re.search(r"\b(staying silent|remaining silent|stay silent|no ask|nothing outstanding|standing by|room quiet|silence boundaries|no-op)\b", content, re.IGNORECASE):
                print(f"[Bridge] Dropped bot status/silence narration message from {author_name} in channel {msg.channel.id}")
                return

            words = [w for w in content.split() if any(c.isalnum() for c in w)]
            if len(words) < 4 and not handoff_for_zero:
                return

            now = time.time()
            last_bot_reply = channel_last_bot_reply.get(msg.channel.id, 0)
            if (now - last_bot_reply < 4.0) and not handoff_for_zero:
                print(f"[Bridge] Suppressed bot reply due to 4s cascade cooldown in channel {msg.channel.id}")
                return
            channel_last_bot_reply[msg.channel.id] = now

        # Channel-specific tag enforcement
        rules = get_runtime_rules()
        channel_tag_requirements = rules.get("channel_tag_requirements", {})
        req_tag = channel_tag_requirements.get(str(msg.channel.id))
        is_tagged_role = False
        role_ids = [str(r.id) for r in getattr(msg, "role_mentions", [])]
        is_robot_tagged = (
            "<@&1543285916506783799>" in content or
            "1543285916506783799" in role_ids or
            "<@&1542294519914037341>" in content or
            "1542294519914037341" in role_ids or
            re.search(r"(?:^|[\s,;])@robot\b", content, re.IGNORECASE) is not None or
            re.search(r"^(?:hey\s+)?robot[:,\s]", content, re.IGNORECASE) is not None
        )
        is_team_tagged = (
            "<@&1543462881624858624>" in content or
            "1543462881624858624" in role_ids or
            re.search(r"(?:^|[\s,;])@team\b", content, re.IGNORECASE) is not None or
            re.search(r"^(?:hey\s+)?team[:,\s]", content, re.IGNORECASE) is not None
        )

        if req_tag:
            bot_id = str(bot.user.id) if bot.user else "1542285964213358633"
            allowed_tags = req_tag if isinstance(req_tag, list) else [req_tag]
            allowed_tag_strs = [f"<@&{t}>" for t in allowed_tags]
            has_required_tag = (
                any(t_str in content for t_str in allowed_tag_strs) or
                any(str(t) in role_ids for t in allowed_tags)
            )
            is_direct_bot_ping = (
                (bot.user and bot.user in msg.mentions) or
                f"<@{bot_id}>" in content or
                f"<@!{bot_id}>" in content or
                re.search(r"(?:^|[\s,;])@zero\b", content, re.IGNORECASE) is not None or
                is_robot_tagged or
                is_team_tagged
            )
            if not has_required_tag and not is_direct_bot_ping:
                print(f"[Bridge] Message in channel {msg.channel.id} ignored: missing required tag(s) {allowed_tag_strs}")
                return
            is_tagged_role = True

        # Addressing Gate
        bot_id = str(bot.user.id) if bot.user else "1542285964213358633"
        bot_mention_1 = f"<@{bot_id}>"
        bot_mention_2 = f"<@!{bot_id}>"

        is_reply_to_zero = False
        if msg.reference and msg.reference.resolved:
            ref = msg.reference.resolved
            if hasattr(ref, "author") and bot.user and ref.author.id == bot.user.id:
                is_reply_to_zero = True

        # Conversational follow-up detection:
        # If a human responds in the channel shortly after Zero spoke (within 300s)
        # without explicitly addressing another peer/user, check if it is a follow-up turn.
        is_conversational_follow_up = False
        if not msg.author.bot and not is_reply_to_zero:
            try:
                from tools.classifier import is_explicitly_addressed_to_other
                if not is_explicitly_addressed_to_other(content):
                    from tools.channel_history import get_recent_messages
                    prev_msgs = get_recent_messages(msg.channel.id, limit=5, exclude_msg_id=msg.id)
                    if prev_msgs:
                        last_msg = prev_msgs[-1]
                        last_author = str(last_msg.get("author", "")).lower()
                        if last_author == "zero" or (last_msg.get("is_bot") and "zero" in last_author):
                            now_ts = msg.created_at.timestamp() if hasattr(msg, "created_at") else time.time()
                            last_ts_str = last_msg.get("timestamp")
                            last_msg_ts = 0.0
                            if last_ts_str:
                                try:
                                    last_dt = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                                    last_msg_ts = last_dt.timestamp()
                                except Exception:
                                    pass

                            elapsed = (now_ts - last_msg_ts) if last_msg_ts > 0 else 0
                            if 0 <= elapsed <= 300:
                                last_content = last_msg.get("content", "")
                                asked_question = ("?" in last_content[-400:]) or ('"reply": "optional"' in last_content) or ('"floor": "open"' in last_content)
                                is_action_or_affirmative = bool(re.search(
                                    r"^(?:yep|yeah|yes|sure|go ahead|sounds good|do it|proceed|approved|lgtm|update|check|push|fix|please|pls|thanks|thank you|can you|could you|what about|how about|also|no|nope|wait|try|use|make|let|revert|rollback|deploy|show|tell|why|explain|see|look|run|stop|start|restart|reload|clean|add|remove|delete|set|get|test|verify|option|choice|step)\b",
                                    content.strip(),
                                    re.IGNORECASE
                                ))
                                is_direct_query = bool(re.search(
                                    r"^(?:done|status|ready|finished|how'?s|is\s+it|did\s+it|any\s+update|which|where|what)\b",
                                    content.strip(),
                                    re.IGNORECASE
                                )) or ("?" in content and len(content.split()) <= 15)
                                if asked_question or is_action_or_affirmative or is_direct_query:
                                    is_conversational_follow_up = True
                                    print(f"[Bridge] Conversational follow-up to Zero detected from {author_name} ({elapsed:.1f}s after Zero's turn) in channel {msg.channel.id}")
            except Exception as fe:
                print(f"[Bridge] Warning evaluating conversational follow-up: {fe}")

        is_mentioned = (
            is_tagged_role or
            (bot.user and bot.user in msg.mentions) or
            bot_mention_1 in content or
            bot_mention_2 in content or
            is_robot_tagged or
            is_team_tagged or
            re.search(r"(?:^|[\s,;/])(?:hey\s+)?@?zero(?:\b|[!?:,/])", content, re.IGNORECASE) is not None or
            is_reply_to_zero or
            handoff_for_zero or
            is_conversational_follow_up
        )
        if not is_mentioned:
            if rules.get("ambient_classifier_enabled", False):
                try:
                    from tools.classifier import score_relevance
                    score = await asyncio.to_thread(score_relevance, content, author_name)
                    threshold = float(rules.get("ambient_relevance_threshold", 0.80))
                    if score >= threshold:
                        print(f"[Bridge] Ambient classifier scored {score:.2f} >= {threshold:.2f} for {author_name}. Triggering chime-in.")
                    else:
                        print(f"[Bridge] Ambient classifier scored {score:.2f} < {threshold:.2f} for {author_name} (buffered, remaining silent)")
                        return
                except Exception as ce:
                    print(f"[Bridge] Error running ambient classifier: {ce}")
                    return
            else:
                print(f"[Bridge] Buffered message from {author_name} in channel {msg.channel.id} (passive observer mode)")
                return

        # Clean mentions from prompt (only bot user and configured bot role mentions)
        target_role_ids = {"1543462881624858624", "1543285916506783799", "1542294519914037341"}
        if req_tag:
            if isinstance(req_tag, list):
                target_role_ids.update(str(t) for t in req_tag)
            else:
                target_role_ids.add(str(req_tag))

        # Check if other entities/bots are also mentioned in the message
        other_mentions = [
            m for m in re.findall(r"<@!?([0-9]+)>", content)
            if m != bot_id
        ]

        is_banana_directive = is_banana_summary_prompt or is_banana_stall_prompt or is_banana_loop_prompt

        if other_mentions:
            # Preserve @Zero so multi-agent scope parsing sees that Zero was directly addressed
            cleaned = re.sub(rf"<@!?{bot_id}>", "@Zero", content)
        elif is_banana_directive:
            # Preserve @Zero so LLM knows Zero is explicitly addressed by Banana Watcher
            cleaned = re.sub(rf"<@!?{bot_id}>", "@Zero", content)
        else:
            cleaned = re.sub(rf"<@!?{bot_id}>", "", content)

        # Strip leading addressing role mentions
        for rid in target_role_ids:
            cleaned = re.sub(rf"^\s*<@&{rid}>\s*", "", cleaned)
        # Convert inline role mentions to readable names rather than stripping to empty strings
        cleaned = re.sub(r"<@&1543462881624858624>", "@team", cleaned)
        cleaned = re.sub(r"<@&1543285916506783799>", "@robot", cleaned)
        cleaned = re.sub(r"<@&1542294519914037341>", "@robot", cleaned)
        for rid in target_role_ids:
            cleaned = re.sub(rf"<@&{rid}>", "", cleaned)

        # In multi-mention messages or Banana Watcher prompts, preserve explicit @Zero; only strip leading zero callout when sole addressee
        if not other_mentions and not is_banana_directive:
            cleaned = re.sub(r"^(hey\s+)?zero[:,\s]*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"@zero\b", "", cleaned, flags=re.IGNORECASE)
        elif is_banana_directive:
            cleaned = re.sub(r"@Zero\s*\(@Zero\):?", "@Zero:", cleaned, flags=re.IGNORECASE)

        cleaned = re.sub(r"^(hey\s+)?robot[:,\s]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"@robot\b", "", cleaned, flags=re.IGNORECASE)

        # Convert peer mentions to human-readable names for LLM scope parsing
        try:
            from tools.handoff import TARGET_MENTIONS
            for name, mention_str in TARGET_MENTIONS.items():
                if name not in ("zero", "robot", "team"):
                    m_id = mention_str.strip("<@!>")
                    cleaned = re.sub(rf"<@!?{m_id}>", f"@{name.title()}", cleaned)
        except Exception:
            pass

        cleaned = cleaned.strip()

        if is_banana_summary_prompt:
            m_subj = re.search(r"🍌 \*\*Discussion Concluded\*\*: Topic `(?P<subject>[^`]+)` has reached resolution", content)
            concluded_subj = m_subj.group("subject") if m_subj else "concluded-topic"
            cleaned += (
                f"\n\n[CRITICAL OPERATIONAL DIRECTIVE - RULE 7 CONCLUDED DISCUSSION EXECUTIVE SUMMARY]:\n"
                f"Banana Watcher has officially declared topic `{concluded_subj}` concluded in #the-banana-stand.\n"
                f"In accordance with Crab Cavern Ratified Peer Operating Rule 7, you MUST execute the following:\n"
                f"1. Review recent channel context to understand the problem, debate, and final consensus reached on `{concluded_subj}`.\n"
                f"2. Synthesize a concise executive summary (strictly under 250 words) structured as:\n"
                f"   • Problem / Motivation\n"
                f"   • Resolution / Ratified Consensus\n"
                f"   • Shipped Artifacts & PR/Code references\n"
                f"   CRITICAL STYLE REQUIREMENT: Write strictly in plain language, without overly complex industry lingo, dense academic jargon, or acronym walls.\n"
                f"3. Dispatch the executive summary directly to #lounge (<#1534452820995080192>) via outbox:\n"
                f"   python3 /workspace/tools/outbox.py --channel lounge --message \"<summary_text>\"\n"
                f"4. If durable decisions or architecture were agreed upon, log the resolution to /workspace/memory/crab_cavern/decisions.md.\n"
                f"5. Acknowledge in #the-banana-stand with STRICTLY and ONLY '🍌' (a single banana emoji).\n"
                f"   CRITICAL: Do NOT output the summary text body into #the-banana-stand!"
            )

        if is_banana_stall_prompt:
            m_subj = re.search(r"🍌 \*\*Topic Stalled\*\*: Topic `(?P<subject>[^`]+)`", content)
            stalled_subj = m_subj.group("subject") if m_subj else "stalled-topic"
            cleaned += (
                f"\n\n[CRITICAL OPERATIONAL DIRECTIVE - BANANA WATCHER TOPIC STALLED NUDGE]:\n"
                f"Banana Watcher has flagged that topic `{stalled_subj}` has stalled without resolution in #the-banana-stand.\n"
                f"In accordance with Crab Cavern Ratified Peer Operating Rule 6 ('Ship it or track it: never let agreed proposals drop on the floor'):\n"
                f"1. Review recent channel context on `{stalled_subj}`.\n"
                f"2. If the discussion concluded, reached consensus, or should be parked:\n"
                f"   Reply directly in #the-banana-stand with: '🍌 Parking `{stalled_subj}`, Banana Watcher: <brief reason>' with handoff envelope (kind: 'resolution', floor: 'closed', reply: 'none').\n"
                f"3. If there is an actionable proposal or pending task agreed upon, record a task ticket in /workspace/data/tasks.json, log to /workspace/memory/crab_cavern/decisions.md, and post the ticket details.\n"
                f"4. You MUST respond to Banana Watcher. NEVER emit [NO_REPLY] to a Banana Watcher nudge."
            )

        if is_banana_loop_prompt:
            m_subj = re.search(r"🍌 \*\*Loop Warning\*\*: Topic `(?P<subject>[^`]+)`", content)
            loop_subj = m_subj.group("subject") if m_subj else "loop-topic"
            cleaned += (
                f"\n\n[CRITICAL OPERATIONAL DIRECTIVE - BANANA WATCHER LOOP WARNING]:\n"
                f"Banana Watcher has flagged topic `{loop_subj}` for excessive turns without consensus.\n"
                f"Clamp the loop immediately: synthesize a terminal conclusion, close the floor (`floor: 'closed'`), or park the topic.\n"
                f"You MUST respond to Banana Watcher. NEVER emit [NO_REPLY] to a Banana Watcher prompt."
            )
        saved_attachments = []
        if msg.attachments:
            for att in msg.attachments:
                safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", att.filename)
                dest = ATTACHMENTS_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
                try:
                    await att.save(dest)
                    saved_attachments.append(str(dest))
                except Exception as e:
                    print(f"[Bridge] Failed saving attachment: {e}")

        if cleaned.lower() in ("!reset", "/reset", "!new", "/new"):
            clear_channel_session_id(msg.channel.id, "external")
            await msg.reply("🔄 Session reset for this channel.")
            return

        # Operator command: pause / unpause responses to a bot in this channel
        pause_match = re.search(
            r"^(?:!pause|/pause|pause\s+responses?\s+to|pause\s+responding\s+to|pause)\s+([a-zA-Z0-9_-]+)(?:\s+(?:for\s+)?(\d+)\s*(?:m|min|mins|minutes)?)?$",
            cleaned,
            re.IGNORECASE
        )
        if pause_match:
            target_bot = pause_match.group(1).strip()
            mins = int(pause_match.group(2)) if pause_match.group(2) else int(rules.get("last_word_pause_minutes", 3))
            from tools.last_word_protocol import pause_bot
            rec = pause_bot(
                channel_id=msg.channel.id,
                bot_id=target_bot if target_bot.isdigit() else None,
                bot_name=target_bot if not target_bot.isdigit() else None,
                duration_seconds=mins * 60.0,
                reason=f"Operator command from {author_name}"
            )
            await msg.reply(f"⏸️ Responses to **{rec.get('bot_name', target_bot)}** in <#{msg.channel.id}> paused for {mins} minutes (until {rec.get('pause_until_pt')}).")
            return

        unpause_match = re.search(
            r"^(?:!unpause|/unpause|resume\s+responses?\s+to|resume\s+responding\s+to|unpause)\s+([a-zA-Z0-9_-]+)$",
            cleaned,
            re.IGNORECASE
        )
        if unpause_match:
            target_bot = unpause_match.group(1).strip()
            from tools.last_word_protocol import unpause_bot
            removed = unpause_bot(msg.channel.id, target_bot)
            if removed:
                await msg.reply(f"▶️ Responses to **{target_bot}** in <#{msg.channel.id}> resumed.")
            else:
                await msg.reply(f"ℹ️ **{target_bot}** was not currently paused in <#{msg.channel.id}>.")
            return

        # Operator emergency kill switch: !halt / !resume for Station Agora trading
        agora_halt_match = re.search(r"^(?:!halt|/halt|!stop|/stop)(?:\s+(.*))?$", cleaned.strip(), re.IGNORECASE)
        if agora_halt_match:
            from tools.agora_kill_switch import trigger_kill_switch
            res = trigger_kill_switch(initiator=author_name, action="halt", channel_id=msg.channel.id)
            await msg.reply(res["message"])
            return

        agora_resume_match = re.search(r"^(?:!resume|/resume|!start|/start)(?:\s+(.*))?$", cleaned.strip(), re.IGNORECASE)
        if agora_resume_match:
            from tools.agora_kill_switch import trigger_kill_switch
            res = trigger_kill_switch(initiator=author_name, action="resume", channel_id=msg.channel.id)
            await msg.reply(res["message"])
            return

        if is_container_restart_intent(cleaned):
            if msg.author.id != OWNER_USER_ID:
                await msg.reply("⚠️ Administrative container restart commands are restricted to the bot owner.")
                return
            await execute_container_restart(msg.channel, initiator=author_name, reason="Manual Docker container restart requested via Discord (external channel)")
            return

        if is_reload_intent(cleaned):
            if msg.author.id != OWNER_USER_ID:
                await msg.reply("⚠️ Administrative bridge commands are restricted to the bot owner.")
                return
            if reload_fn:
                await reload_fn(msg.channel, initiator=author_name, force=True, reason="Manual in-place bridge reload requested via Discord (external channel)")
            else:
                await execute_bridge_reload(bot, msg.channel, initiator=author_name, force=True, reason="Manual in-place bridge reload requested via Discord (external channel)")
            return

        # Check for minimal / lazy pings (e.g. role mentions only, ^, ^^, or short pointer phrases)
        is_lazy_pointer = bool(
            not cleaned or
            re.fullmatch(r"[\^\s\.\?!]+", cleaned) or
            cleaned.lower() in ("^", "^^", "^^^", "this", "look", "see", "what?", "check this")
        )
        if is_lazy_pointer and not saved_attachments:
            cleaned = (
                f"[OPERATIONAL DIRECTIVE - LAZY TYPER ADDRESSING]:\n"
                f"The user sent a minimal ping ('{content.strip()}').\n"
                f"Humans are lazy typers: you MUST read back up the recent messages in Discord channel context to identify and address the active topic, question, problem, link, or proposal at hand immediately."
            )

        if saved_attachments:
            image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
            has_images = any(Path(p).suffix.lower() in image_exts for p in saved_attachments)
            image_directive = (
                "\n\n[CRITICAL IMAGE INPUT INVARIANT]:\n"
                "One or more images are attached to this message. You MUST parse and inspect every image (using view_file) and understand its visual contents, error traces, screenshots, or diagrams as part of the primary input, EVEN IF the accompanying text message made no mention of the image."
                if has_images else ""
            )
            hint = "\n\n[Attached file(s) available via view_file tool]:\n" + "\n".join(f"- {p}" for p in saved_attachments)
            if is_lazy_pointer:
                cleaned = (
                    f"[OPERATIONAL DIRECTIVE - LAZY TYPER & ATTACHMENT ADDRESSING]:\n"
                    f"The user sent an attachment with a minimal prompt ('{content.strip()}').\n"
                    f"Humans are lazy typers: review the attached image/file(s) and recent channel context to address the active topic directly."
                )
            cleaned = (cleaned or "Please inspect the attached file(s) and assist.") + hint + image_directive

        if not cleaned:
            await msg.reply("What's up? Give me something interesting to work on.")
            return

        try:
            await msg.channel.typing()
        except Exception:
            pass
        author_name = msg.author.display_name or msg.author.name

        # Group Chat Mid-Turn Steering Check (Channel-scoped)
        target_proc = channel_active_procs.get(msg.channel.id)
        if target_proc is not None and target_proc.returncode is None:
            steering_channels.add(msg.channel.id)
            try:
                target_proc.send_signal(signal.SIGINT)
            except Exception as se:
                print(f"[Bridge] Warning sending SIGINT for external group steering in channel {msg.channel.id}: {se}")

            steer_prompt = (
                f"[MID-TURN GROUP CONVERSATION UPDATE]\n"
                f"While you were drafting your reply, a new message arrived in the channel from {author_name}:\n"
                f"\"{cleaned}\"\n\n"
                f"CRITICAL INSTRUCTIONS FOR REVISED TURN:\n"
                f"1. Absorb this new context immediately. If your in-progress thought is now obsolete, answered, or contradicted, pivot cleanly or yield.\n"
                f"2. If you were emitting a ```handoff block, ensure it is completely valid, closed JSON or omitted entirely.\n"
                f"3. Keep your output concise (strictly under 2,000 chars) and respond naturally to the CURRENT state of the room."
            )
            print(f"[Bridge] Silently steering in-flight external turn for new message from {author_name}")
            await ext_turn_queue.put({
                "prompt": steer_prompt,
                "status_msg": None,
                "reply_target": msg,
                "attachments": saved_attachments,
                "is_steer": True,
                "mode": "external",
                "channel_id": msg.channel.id,
                "author_name": author_name,
                "queued_at": time.perf_counter(),
            })
            return

        is_last_word = False
        last_word_streak = 0
        if msg.author.bot and not is_banana_watcher and rules.get("last_word_protocol_enabled", True):
            lw_channels = rules.get("last_word_channels", [1534452820995080192])
            if not lw_channels or msg.channel.id in lw_channels:
                try:
                    from tools.last_word_protocol import check_last_word_condition, build_last_word_prompt_injection, mark_last_word_in_flight
                    lw_thresh = int(rules.get("last_word_threshold", 4))
                    is_last_word, last_word_streak = check_last_word_condition(
                        msg.channel.id, msg.author.id, author_name, threshold=lw_thresh
                    )
                    if is_last_word:
                        lw_mins = int(rules.get("last_word_pause_minutes", 30))
                        cleaned += build_last_word_prompt_injection(author_name, last_word_streak, lw_mins)
                        mark_last_word_in_flight(msg.channel.id, str(msg.author.id))
                        mark_last_word_in_flight(msg.channel.id, author_name)
                        print(f"[Bridge] Last Word Protocol engaged for {author_name} in channel {msg.channel.id} (streak: {last_word_streak}, pause: {lw_mins}m)")
                except Exception as lwe:
                    print(f"[Bridge] Error checking Last Word Protocol condition: {lwe}")

        await ext_turn_queue.put({
            "prompt": cleaned,
            "status_msg": None,
            "reply_target": msg,
            "attachments": saved_attachments,
            "is_steer": False,
            "mode": "external",
            "channel_id": msg.channel.id,
            "author_name": author_name,
            "is_last_word": is_last_word,
            "last_word_bot_id": str(msg.author.id) if msg.author.bot else None,
            "last_word_bot_name": author_name if msg.author.bot else None,
            "last_word_streak": last_word_streak,
            "queued_at": time.perf_counter(),
        })
        return

    # Handle in-flight OAuth interactive token piping
    if br.active_master_fd is not None:
        if msg.author.id != OWNER_USER_ID or msg.channel.id != TARGET_CHANNEL_ID:
            return
        try:
            print(f"[Antigravity] Piping user input to PTY fd={br.active_master_fd}: {content}")
            os.write(br.active_master_fd, (content + "\n").encode("utf-8"))
            await msg.reply("Auth code received! Finishing authentication...")
        except Exception as e:
            await msg.reply(f"Error forwarding auth code: {e}")
        return

    # Handle session reset commands
    if content.lower() in ("!reset", "/reset", "!new", "/new"):
        sess_key = "home" if (msg.channel.id == TARGET_CHANNEL_ID) else str(msg.channel.id)
        clear_channel_session_id(msg.channel.id, "home")
        br.reset_session_keys.discard(sess_key)
        await msg.reply("🔄 Conversation session reset for this channel/thread. Your next message will start a fresh session.")
        return

    # Operator command: pause / unpause responses to a bot (default: #lounge)
    pause_match = re.search(
        r"^(?:!pause|/pause|pause\s+responses?\s+to|pause\s+responding\s+to|pause)\s+([a-zA-Z0-9_-]+)(?:\s+(?:for\s+)?(\d+)\s*(?:m|min|mins|minutes)?)?(?:\s+(?:in\s+)?(?:channel\s+)?([a-zA-Z0-9_-]+))?$",
        content,
        re.IGNORECASE
    )
    if pause_match:
        target_bot = pause_match.group(1).strip()
        mins = int(pause_match.group(2)) if pause_match.group(2) else int(get_runtime_rules().get("last_word_pause_minutes", 3))
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
            reason=f"Operator command from {author_name}"
        )
        await msg.reply(f"⏸️ Responses to **{rec.get('bot_name', target_bot)}** in <#{target_ch}> paused for {mins} minutes (until {rec.get('pause_until_pt')}).")
        return

    unpause_match = re.search(
        r"^(?:!unpause|/unpause|resume\s+responses?\s+to|resume\s+responding\s+to|unpause)\s+([a-zA-Z0-9_-]+)(?:\s+(?:in\s+)?(?:channel\s+)?([a-zA-Z0-9_-]+))?$",
        content,
        re.IGNORECASE
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
        return

    # Operator emergency kill switch: !halt / !resume for Station Agora trading
    agora_halt_match = re.search(r"^(?:!halt|/halt|!stop|/stop)(?:\s+(.*))?$", content.strip(), re.IGNORECASE)
    if agora_halt_match:
        from tools.agora_kill_switch import trigger_kill_switch
        res = trigger_kill_switch(initiator=author_name, action="halt", channel_id=msg.channel.id)
        await msg.reply(res["message"])
        return

    agora_resume_match = re.search(r"^(?:!resume|/resume|!start|/start)(?:\s+(.*))?$", content.strip(), re.IGNORECASE)
    if agora_resume_match:
        from tools.agora_kill_switch import trigger_kill_switch
        res = trigger_kill_switch(initiator=author_name, action="resume", channel_id=msg.channel.id)
        await msg.reply(res["message"])
        return

    if is_container_restart_intent(content):
        ch_name = getattr(msg.channel, "name", "zero-chat")
        await execute_container_restart(msg.channel, initiator=author_name, reason=f"Manual Docker container restart requested via #{ch_name}")
        return

    if is_reload_intent(content):
        ch_name = getattr(msg.channel, "name", "zero-chat")
        if reload_fn:
            await reload_fn(msg.channel, initiator=author_name, force=True, reason=f"Manual in-place bridge reload requested via #{ch_name}")
        else:
            await execute_bridge_reload(bot, msg.channel, initiator=author_name, force=True, reason=f"Manual in-place bridge reload requested via #{ch_name}")
        return

    # Handle BananaWatcher commands
    if content.lower().startswith("!bananawatcher") or content.lower().startswith("!banana-watcher") or content.lower().startswith("/bananawatcher"):
        from tools.banana_watcher import get_daemon_status, load_state, check_channel_and_evaluate, start_daemon, stop_daemon
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
        return

    # Handle model query & switching commands
    if content.lower().startswith("!model") or content.lower().startswith("/model"):
        current_model = active_model_getter() if active_model_getter else get_active_model()
        parts = content.split(maxsplit=1)
        if len(parts) == 1:
            models_help = (
                f"🤖 **Current Active Model:** `{current_model}`\n\n"
                "**Available Models & Aliases:**\n"
                "• `3.8` or `flash` → `gemini-3.8-flash-high` *(Default, fast & smart)*\n"
                "• `3.8-med` or `3.8-medium` → `gemini-3.8-flash-medium`\n"
                "• `3.8-lite`, `3.8-low` or `flash-low` → `gemini-3.8-flash-low` *(Lightweight & fast)*\n"
                "• `3.7` or `3.7-flash` → `gemini-3.7-flash-high`\n"
                "• `3.7-lite` or `3.7-low` → `gemini-3.7-flash-low`\n"
                "• `3.6` or `3.6-flash` → `gemini-3.6-flash-high`\n"
                "• `3.1-pro` or `pro` → `gemini-3.1-pro-high` *(Deep reasoning / complex refactors)*\n"
                "• `sonnet` or `claude` → `claude-sonnet-4-6` *(Claude Thinking model)*\n"
                "• `opus` → `claude-opus-4-6-thinking` *(Claude Opus Thinking)*\n"
                "• `gpt` → `gpt-oss-120b-medium`\n\n"
                "*Switch model with:* `!model <name>` (e.g. `!model 3.8`, `!model pro`, `!model flash-low`)"
            )
            await msg.reply(models_help)
            return

        target_m = parts[1].strip()
        aliases = {
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
            "3.1-pro-low": "gemini-3.1-pro-low",
            "sonnet": "claude-sonnet-4-6",
            "claude": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6-thinking",
            "gpt": "gpt-oss-120b-medium",
            "gpt-oss": "gpt-oss-120b-medium"
        }
        resolved = aliases.get(target_m.lower(), target_m)
        if active_model_setter:
            active_model_setter(resolved)
        else:
            set_active_model(resolved)
        await msg.reply(f"🔄 Switched active model to **`{resolved}`** for subsequent turns (persisted across restarts).")
        return

    # On-demand sidecar triggers
    triggers = {
        "!heartbeat": ("⏳ *Running on-demand Heartbeat Sweep...*", "Run the infrastructure heartbeat check using /workspace/tools/sidecars.py heartbeat. Report the status cleanly."),
        "/heartbeat": ("⏳ *Running on-demand Heartbeat Sweep...*", "Run the infrastructure heartbeat check using /workspace/tools/sidecars.py heartbeat. Report the status cleanly."),
        "!triage": ("⏳ *Running on-demand Nightly Triage & Briefing...*", "Run the nightly agenda & inbox triage briefing using /workspace/tools/sidecars.py triage. Present tomorrow's calendar agenda and priority unread emails."),
        "/triage": ("⏳ *Running on-demand Nightly Triage & Briefing...*", "Run the nightly agenda & inbox triage briefing using /workspace/tools/sidecars.py triage. Present tomorrow's calendar agenda and priority unread emails."),
        "!logs": ("⏳ *Running on-demand NAS Log Review...*", "Run the autonomous NAS log review and triage using /workspace/tools/nas_log_triage.py. If issues are found, investigate root causes, apply safe code/config fixes autonomously, and present one-click options for any needed approvals. If clean, report the crisp one-liner."),
        "/logs": ("⏳ *Running on-demand NAS Log Review...*", "Run the autonomous NAS log review and triage using /workspace/tools/nas_log_triage.py. If issues are found, investigate root causes, apply safe code/config fixes autonomously, and present one-click options for any needed approvals. If clean, report the crisp one-liner."),
        "!plex": ("⏳ *Running on-demand Plex Transcode Cleanup...*", "Run the Plex transcode cache cleanup using /workspace/tools/sidecars.py plex. Report status."),
        "/plex": ("⏳ *Running on-demand Plex Transcode Cleanup...*", "Run the Plex transcode cache cleanup using /workspace/tools/sidecars.py plex. Report status."),
        "!reminders": ("⏳ *Checking dated reminders...*", "Check dated one-shot reminders using /workspace/tools/sidecars.py reminders. Report any due reminders."),
        "/reminders": ("⏳ *Checking dated reminders...*", "Check dated one-shot reminders using /workspace/tools/sidecars.py reminders. Report any due reminders."),
        "!ev9": ("⏳ *Running on-demand EV9 Monitor...*", "Run the Kia EV9 listing monitor using /workspace/tools/sidecars.py ev9 --force. Display the latest listings digest."),
        "/ev9": ("⏳ *Running on-demand EV9 Monitor...*", "Run the Kia EV9 listing monitor using /workspace/tools/sidecars.py ev9 --force. Display the latest listings digest."),
        "!marketing": ("⏳ *Running promotional marketing sweep...*", "Run the promotional email marketing sweep using /workspace/tools/sidecars.py marketing."),
        "/marketing": ("⏳ *Running promotional marketing sweep...*", "Run the promotional email marketing sweep using /workspace/tools/sidecars.py marketing."),
        "!doctor": ("⏳ *Running Memory Doctor audit...*", "Run the memory store audit pass using /workspace/tools/sidecars.py doctor."),
        "/doctor": ("⏳ *Running Memory Doctor audit...*", "Run the memory store audit pass using /workspace/tools/sidecars.py doctor."),
        "!digest": ("⏳ *Generating Option B Weekly Digest...*", "Generate and post the Option B Weekly Proactive Digest using /workspace/tools/weekly_digest.py. Present upcoming maintenance, 30-day renewals, and cash-flow deltas cleanly."),
        "/digest": ("⏳ *Generating Option B Weekly Digest...*", "Generate and post the Option B Weekly Proactive Digest using /workspace/tools/weekly_digest.py. Present upcoming maintenance, 30-day renewals, and cash-flow deltas cleanly."),
        "!tasks": ("⏳ *Fetching project and task tracker...*", "Show active projects and tasks using /workspace/tools/task_manager.py summary."),
        "/tasks": ("⏳ *Fetching project and task tracker...*", "Show active projects and tasks using /workspace/tools/task_manager.py summary."),
        "!tasks-sync": ("⏳ *Syncing tasks with Google Tasks...*", "Run two-way task sync with Google Tasks using /workspace/tools/sidecars.py tasks_sync. Report any changes cleanly."),
        "/tasks-sync": ("⏳ *Syncing tasks with Google Tasks...*", "Run two-way task sync with Google Tasks using /workspace/tools/sidecars.py tasks_sync. Report any changes cleanly."),
        "!projects": ("⏳ *Fetching project and task tracker...*", "Show active projects and tasks using /workspace/tools/task_manager.py summary."),
        "/projects": ("⏳ *Fetching project and task tracker...*", "Show active projects and tasks using /workspace/tools/task_manager.py summary."),
        "!schedule": ("⏳ *Fetching sidecar schedule...*", "Show the current sidecar schedule using /workspace/tools/scheduler_tool.py summary."),
        "/schedule": ("⏳ *Fetching sidecar schedule...*", "Show the current sidecar schedule using /workspace/tools/scheduler_tool.py summary."),
        "!arr_queue": ("⏳ *Checking Radarr & Sonarr queues...*", "Run the Arr queue watchdog check using /workspace/tools/sidecars.py arr_queue --force. Report findings and auto-remediations cleanly."),
        "/arr_queue": ("⏳ *Checking Radarr & Sonarr queues...*", "Run the Arr queue watchdog check using /workspace/tools/sidecars.py arr_queue --force. Report findings and auto-remediations cleanly."),
        "!queue": ("⏳ *Checking Radarr & Sonarr queues...*", "Run the Arr queue watchdog check using /workspace/tools/sidecars.py arr_queue --force. Report findings and auto-remediations cleanly."),
        "/queue": ("⏳ *Checking Radarr & Sonarr queues...*", "Run the Arr queue watchdog check using /workspace/tools/sidecars.py arr_queue --force. Report findings and auto-remediations cleanly."),
        "!reauth": ("⏳ *Auditing Home Assistant integration auth & setup...*", "Run the Home Assistant integration & re-auth watchdog using /workspace/tools/sidecars.py ha_reauth --force. Report status."),
        "/reauth": ("⏳ *Auditing Home Assistant integration auth & setup...*", "Run the Home Assistant integration & re-auth watchdog using /workspace/tools/sidecars.py ha_reauth --force. Report status."),
        "!prowlarr": ("⏳ *Checking Prowlarr indexer health & backoffs...*", "Run the Prowlarr indexer health check using /workspace/tools/sidecars.py prowlarr --force. Report status."),
        "/prowlarr": ("⏳ *Checking Prowlarr indexer health & backoffs...*", "Run the Prowlarr indexer health check using /workspace/tools/sidecars.py prowlarr --force. Report status."),
        "!sabnzbd": ("⏳ *Checking SABnzbd queue & recent history...*", "Run the SABnzbd downloader check using /workspace/tools/sidecars.py sabnzbd --force. Report status."),
        "/sabnzbd": ("⏳ *Checking SABnzbd queue & recent history...*", "Run the SABnzbd downloader check using /workspace/tools/sidecars.py sabnzbd --force. Report status."),
        "!kometa_audit": ("⏳ *Auditing recent Kometa run logs...*", "Run the Kometa post-run log audit using /workspace/tools/sidecars.py kometa_audit --force. Report status."),
        "/kometa_audit": ("⏳ *Auditing recent Kometa run logs...*", "Run the Kometa post-run log audit using /workspace/tools/sidecars.py kometa_audit --force. Report status."),
        "!cubs": ("⏳ *Checking Cubs game schedule...*", "Check the Chicago Cubs game schedule using /workspace/tools/sidecars.py cubs --test. Present matchup, start time, and streaming options."),
        "/cubs": ("⏳ *Checking Cubs game schedule...*", "Check the Chicago Cubs game schedule using /workspace/tools/sidecars.py cubs --test. Present matchup, start time, and streaming options."),
        "!sidecars": ("⏳ *Fetching sidecar execution health...*", "Show recent sidecar execution health and failures using /workspace/tools/sidecars.py status."),
        "/sidecars": ("⏳ *Fetching sidecar execution health...*", "Show recent sidecar execution health and failures using /workspace/tools/sidecars.py status."),
        "!mcp": ("⏳ *Checking MCP daemon status...*", "Show persistent MCP daemon status and endpoint health using /workspace/tools/mcp_daemon.py status."),
        "/mcp": ("⏳ *Checking MCP daemon status...*", "Show persistent MCP daemon status and endpoint health using /workspace/tools/mcp_daemon.py status."),
        "!mail": ("⏳ *Checking inbound mail listener status...*", "Show inbound email listener status using /workspace/tools/zero_mail_listener.py status."),
        "/mail": ("⏳ *Checking inbound mail listener status...*", "Show inbound email listener status using /workspace/tools/zero_mail_listener.py status."),
        "!morning": ("⏳ *Running Crab Cavern morning rotation dispatcher...*", "Run the Crab Cavern morning rotation dispatcher using /workspace/tools/morning_dispatcher.py --dispatch."),
        "/morning": ("⏳ *Running Crab Cavern morning rotation dispatcher...*", "Run the Crab Cavern morning rotation dispatcher using /workspace/tools/morning_dispatcher.py --dispatch."),
        "!birthdays": ("⏳ *Checking birthdays today...*", "Check for friend & family birthdays today using /workspace/tools/birthday_reminder.py. Post any birthdays."),
        "/birthdays": ("⏳ *Checking birthdays today...*", "Check for friend & family birthdays today using /workspace/tools/birthday_reminder.py. Post any birthdays."),
        "!standup": ("⏳ *Running Market Sandbox autonomous standup dispatcher...*", "Run the Market Sandbox autonomous daily standup using /workspace/tools/market_standup.py --dispatch."),
        "/standup": ("⏳ *Running Market Sandbox autonomous standup dispatcher...*", "Run the Market Sandbox autonomous daily standup using /workspace/tools/market_standup.py --dispatch."),
        "!market_standup": ("⏳ *Running Market Sandbox autonomous standup dispatcher...*", "Run the Market Sandbox autonomous daily standup using /workspace/tools/market_standup.py --dispatch."),
        "/market_standup": ("⏳ *Running Market Sandbox autonomous standup dispatcher...*", "Run the Market Sandbox autonomous daily standup using /workspace/tools/market_standup.py --dispatch."),
        "!steering": ("⏳ *Running AGORA Steering briefing dispatcher...*", "Run the daily AGORA Steering meeting briefing using /workspace/tools/agora_steering.py --dispatch."),
        "/steering": ("⏳ *Running AGORA Steering briefing dispatcher...*", "Run the daily AGORA Steering meeting briefing using /workspace/tools/agora_steering.py --dispatch."),
        "!agora_steering": ("⏳ *Running AGORA Steering briefing dispatcher...*", "Run the daily AGORA Steering meeting briefing using /workspace/tools/agora_steering.py --dispatch."),
        "/agora_steering": ("⏳ *Running AGORA Steering briefing dispatcher...*", "Run the daily AGORA Steering meeting briefing using /workspace/tools/agora_steering.py --dispatch."),
        "!code_audit": ("⏳ *Running on-demand Hardcoded Rule & Regex Audit...*", "Run the monthly hardcoded rule & regex audit using /workspace/tools/sidecars.py code_audit. Present findings and architectural recommendations for eliminating brittle heuristics."),
        "/code_audit": ("⏳ *Running on-demand Hardcoded Rule & Regex Audit...*", "Run the monthly hardcoded rule & regex audit using /workspace/tools/sidecars.py code_audit. Present findings and architectural recommendations for eliminating brittle heuristics."),
        "!hardcode_audit": ("⏳ *Running on-demand Hardcoded Rule & Regex Audit...*", "Run the monthly hardcoded rule & regex audit using /workspace/tools/sidecars.py code_audit. Present findings and architectural recommendations for eliminating brittle heuristics."),
        "/hardcode_audit": ("⏳ *Running on-demand Hardcoded Rule & Regex Audit...*", "Run the monthly hardcoded rule & regex audit using /workspace/tools/sidecars.py code_audit. Present findings and architectural recommendations for eliminating brittle heuristics."),
        "!weekly_social_last_seen_review": ("⏳ *Reviewing social events and last seen updates...*", "Review the past week's social events, calendar, and text messages using /workspace/tools/social_last_seen_review.py."),
        "/weekly_social_last_seen_review": ("⏳ *Reviewing social events and last seen updates...*", "Review the past week's social events, calendar, and text messages using /workspace/tools/social_last_seen_review.py."),
        "!social_review": ("⏳ *Reviewing social events and last seen updates...*", "Review the past week's social events, calendar, and text messages using /workspace/tools/social_last_seen_review.py."),
        "/social_review": ("⏳ *Reviewing social events and last seen updates...*", "Review the past week's social events, calendar, and text messages using /workspace/tools/social_last_seen_review.py."),
        "!monthly_core_friends_reconnect": ("⏳ *Checking core friends reconnect list...*", "Check for local Core friends we have not seen in at least 8 weeks using /workspace/tools/core_friends_reminder.py."),
        "/monthly_core_friends_reconnect": ("⏳ *Checking core friends reconnect list...*", "Check for local Core friends we have not seen in at least 8 weeks using /workspace/tools/core_friends_reminder.py."),
        "!core_friends": ("⏳ *Checking core friends reconnect list...*", "Check for local Core friends we have not seen in at least 8 weeks using /workspace/tools/core_friends_reminder.py."),
        "/core_friends": ("⏳ *Checking core friends reconnect list...*", "Check for local Core friends we have not seen in at least 8 weeks using /workspace/tools/core_friends_reminder.py."),
        "!antigravity_check": ("⏳ *Checking for Antigravity updates...*", "Check for Antigravity CLI updates using /workspace/tools/update_antigravity.py."),
        "/antigravity_check": ("⏳ *Checking for Antigravity updates...*", "Check for Antigravity CLI updates using /workspace/tools/update_antigravity.py."),
        "!ha_battery_check": ("⏳ *Running Home Assistant IoT battery check...*", "Run the Home Assistant IoT battery watchdog check using /workspace/tools/ha_battery_check.py."),
        "/ha_battery_check": ("⏳ *Running Home Assistant IoT battery check...*", "Run the Home Assistant IoT battery watchdog check using /workspace/tools/ha_battery_check.py."),
        "!nas_storage_check": ("⏳ *Checking Synology NAS storage and array health...*", "Run the Synology storage & array health check using /workspace/tools/nas_storage_check.py."),
        "/nas_storage_check": ("⏳ *Checking Synology NAS storage and array health...*", "Run the Synology storage & array health check using /workspace/tools/nas_storage_check.py."),
        "!ha_update_check": ("⏳ *Checking for Home Assistant updates...*", "Run the Home Assistant stable update check using /workspace/tools/ha_update_check.py."),
        "/ha_update_check": ("⏳ *Checking for Home Assistant updates...*", "Run the Home Assistant stable update check using /workspace/tools/ha_update_check.py."),
        "!dockhand_update": ("⏳ *Checking Dockhand container updates...*", "Run the Dockhand container image check using /workspace/tools/dockhand_update.py."),
        "/dockhand_update": ("⏳ *Checking Dockhand container updates...*", "Run the Dockhand container image check using /workspace/tools/dockhand_update.py."),
        "!meals": ("⏳ *Generating 3-dinner meal proposal...*", "Run the weekly 3-dinner meal proposal using /workspace/tools/sidecars.py meal_proposal. Propose the 3 dinners for Sunday, Tuesday, and Thursday nights with interactive swap buttons."),
        "/meals": ("⏳ *Generating 3-dinner meal proposal...*", "Run the weekly 3-dinner meal proposal using /workspace/tools/sidecars.py meal_proposal. Propose the 3 dinners for Sunday, Tuesday, and Thursday nights with interactive swap buttons."),
        "!mealplan": ("⏳ *Generating 3-dinner meal proposal...*", "Run the weekly 3-dinner meal proposal using /workspace/tools/sidecars.py meal_proposal. Propose the 3 dinners for Sunday, Tuesday, and Thursday nights with interactive swap buttons."),
        "/mealplan": ("⏳ *Generating 3-dinner meal proposal...*", "Run the weekly 3-dinner meal proposal using /workspace/tools/sidecars.py meal_proposal. Propose the 3 dinners for Sunday, Tuesday, and Thursday nights with interactive swap buttons."),
        "!grocery": ("⏳ *Compiling weekly Whole Foods delivery cart...*", "Run the weekly Whole Foods grocery staging using /workspace/tools/sidecars.py grocery_staging. Ingest pending items from Home Assistant ('todo.shopping_list') and due recurring staples, then post the 1-click cart link."),
        "/grocery": ("⏳ *Compiling weekly Whole Foods delivery cart...*", "Run the weekly Whole Foods grocery staging using /workspace/tools/sidecars.py grocery_staging. Ingest pending items from Home Assistant ('todo.shopping_list') and due recurring staples, then post the 1-click cart link.")
    }

    cmd_key = content.lower().split()[0] if content else ""

    # On-Demand Thread Rename Command
    if is_thread_channel and cmd_key in ("/title", "!title", "/rename", "!rename"):
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
        return

    if cmd_key in triggers:
        status_text, prompt_text = triggers[cmd_key]
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
