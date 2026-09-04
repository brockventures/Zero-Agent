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
from datetime import datetime
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
    sync_credentials,
    PT_TZ,
    is_home_channel,
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
PROCESSED_INTERACTIONS = set()
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
    os.execv(sys.executable, [sys.executable] + sys.argv)


def is_bridge_busy(home_queue=None, ext_queue=None) -> list[str]:
    """Check if any task is actively running or queued across home and external channels."""
    home_busy = (br.active_proc is not None and br.active_proc.returncode is None) or (home_queue is not None and not home_queue.empty())
    ext_busy = (br.ext_active_proc is not None and br.ext_active_proc.returncode is None) or (ext_queue is not None and not ext_queue.empty())
    busy = []
    if home_busy:
        busy.append("#zero-chat")
    if ext_busy:
        busy.append("Crab Cavern")
    for cid, t in channel_active_tasks.items():
        if t and not t.done():
            ch_name = "#zero-chat" if cid == TARGET_CHANNEL_ID else f"channel:{cid}"
            if ch_name not in busy:
                busy.append(ch_name)
    for cid, q in channel_queues.items():
        if not q.empty():
            ch_name = "#zero-chat" if cid == TARGET_CHANNEL_ID else f"channel:{cid}"
            if ch_name not in busy:
                busy.append(ch_name)
    return busy


async def warm_channel_history(channel, limit: int = 25):
    """Prefetch recent messages from Discord channel to initialize history buffer."""
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

    # Intercept restart button choices directly
    if is_reload_intent(choice_text):
        await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
        if reload_fn:
            await reload_fn(interaction.channel, initiator=interaction.user.display_name or interaction.user.name, force=True, reason=f"Choice button '{choice_text}' selected")
        else:
            await execute_bridge_reload(interaction.client, interaction.channel, initiator=interaction.user.display_name or interaction.user.name, force=True, reason=f"Choice button '{choice_text}' selected")
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
        "channel_id": interaction.channel_id
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
        if reply_target and hasattr(reply_target, "typing"):
            async with reply_target.typing():
                await execute_agy_turn(
                    prompt, status_msg, reply_target, attachments,
                    mode="home", channel_id=channel_id,
                    apply_presence_fn=presence_fn,
                    button_choice_fn=button_choice_fn,
                    quick_choice_view_cls=quick_choice_view_cls
                )
        elif reply_target and hasattr(reply_target, "channel"):
            async with reply_target.channel.typing():
                await execute_agy_turn(
                    prompt, status_msg, reply_target, attachments,
                    mode="home", channel_id=channel_id,
                    apply_presence_fn=presence_fn,
                    button_choice_fn=button_choice_fn,
                    quick_choice_view_cls=quick_choice_view_cls
                )
        else:
            await execute_agy_turn(
                prompt, status_msg, reply_target, attachments,
                mode="home", channel_id=channel_id,
                apply_presence_fn=presence_fn,
                button_choice_fn=button_choice_fn,
                quick_choice_view_cls=quick_choice_view_cls
            )
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
                    if reply_target and hasattr(reply_target, "channel") and hasattr(reply_target.channel, "typing"):
                        async with reply_target.channel.typing():
                            await execute_agy_turn(
                                prompt, status_msg, reply_target, attachments,
                                mode=turn_mode, channel_id=channel_id, author_name=author_name,
                                apply_presence_fn=presence_fn,
                                button_choice_fn=button_choice_fn,
                                quick_choice_view_cls=quick_choice_view_cls
                            )
                    elif reply_target and hasattr(reply_target, "typing"):
                        async with reply_target.typing():
                            await execute_agy_turn(
                                prompt, status_msg, reply_target, attachments,
                                mode=turn_mode, channel_id=channel_id, author_name=author_name,
                                apply_presence_fn=presence_fn,
                                button_choice_fn=button_choice_fn,
                                quick_choice_view_cls=quick_choice_view_cls
                            )
                    else:
                        await execute_agy_turn(
                            prompt, status_msg, reply_target, attachments,
                            mode=turn_mode, channel_id=channel_id, author_name=author_name,
                            apply_presence_fn=presence_fn,
                            button_choice_fn=button_choice_fn,
                            quick_choice_view_cls=quick_choice_view_cls
                        )

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

            # Immediate post-turn check for reload flag
            reload_flag = DATA_DIR / "reload_bridge.flag"
            if reload_flag.exists():
                try:
                    reload_flag.unlink()
                except Exception:
                    pass
                print(f"[Channel Worker {channel_id}] Post-turn reload flag detected. Executing in-place reload...")
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
    presence_fn=None
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

    if start_workers_fn:
        start_workers_fn()

    # Clear any stale queued turns from disk on startup to avoid zombie replays
    turn_queue.load_persisted()
    turn_queue.pending_items = []
    turn_queue._persist()

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
                    "channel_id": TARGET_CHANNEL_ID
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
                asyncio.create_task(warm_channel_history(target_ch, limit=25))
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
    global channel_last_bot_reply, active_status_msg

    # Never reply to ourselves
    if bot.user and msg.author.id == bot.user.id:
        return

    content = msg.content.strip()
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
            asyncio.create_task(warm_channel_history(msg.channel, limit=25))
    except Exception as e:
        print(f"[Bridge] Error recording message to channel history: {e}")

    if is_home:
        # Home Turf (#zero-chat): strictly 1-on-1 pairing with Ryan; ignore other bots
        if msg.author.bot:
            return

    # Ignore messages sent prior to current process startup (avoids replaying stale backlog on restart)
    msg_ts = msg.created_at.timestamp() if hasattr(msg, "created_at") else time.time()
    if msg_ts < (BOT_BOOT_TIME - 3.0):
        return

    if not is_home:
        # Ignore automated notification / webhook channels unless directly tagged
        ch_name = getattr(msg.channel, "name", "").lower()
        if (msg.channel.id in READONLY_NOTIFICATION_CHANNELS or ch_name in ("server-updates", "downloads")) and not (bot.user and bot.user in msg.mentions):
            return

        # Crab Cavern & External / Shared Space Mode (Crab Cavern Protocol)
        from tools.channel_history import is_handoff_addressed_to_zero
        handoff_for_zero = is_handoff_addressed_to_zero(content)

        # Global envelope short-circuit per v0 spec: reply: "none" NEVER wakes text reply
        try:
            from tools.handoff import parse_envelope
            envelope = parse_envelope(content)
            if envelope and envelope.get("reply") == "none":
                import importlib
                import tools.topic_tracker
                importlib.reload(tools.topic_tracker)
                tools.topic_tracker.check_and_resolve_topic(envelope, content, author_name, msg.id, msg.channel.id)
                floor_state = str(envelope.get("floor") or "").lower()
                is_floor_open = floor_state in ("open", "free", "any")
                if not handoff_for_zero and not is_floor_open:
                    return
        except Exception as te:
            print(f"[Bridge] Error checking topic resolution: {te}")

        # 1. Loop prevention for peer bots (Amos, Marvin, etc.)
        if msg.author.bot:
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
            "<@&1542294519914037341>" in content or
            "1542294519914037341" in role_ids or
            re.search(r"(?:^|[\s,;])(?:hey\s+)?@?robot(?:\b|[!?:,])", content, re.IGNORECASE) is not None
        )

        if req_tag:
            bot_id = str(bot.user.id) if bot.user else "1542285964213358633"
            tag_str = f"<@&{req_tag}>"
            is_zero_named = bool(re.search(r"(?:^|[\s,;/])(?:hey\s+)?@?zero(?:\b|[!?:,/])", content, re.IGNORECASE))
            is_direct_bot_ping = (
                (bot.user and bot.user in msg.mentions) or
                f"<@{bot_id}>" in content or
                f"<@!{bot_id}>" in content or
                is_zero_named or
                is_robot_tagged
            )
            if tag_str not in content and str(req_tag) not in role_ids and not is_direct_bot_ping:
                print(f"[Bridge] Message in channel {msg.channel.id} ignored: missing required tag {tag_str}")
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
                    from datetime import datetime, timezone
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
        target_role_ids = {"1543462881624858624", "1542294519914037341"}
        if req_tag:
            target_role_ids.add(str(req_tag))
        cleaned = re.sub(rf"<@!?{bot_id}>", "", content)
        for rid in target_role_ids:
            cleaned = re.sub(rf"<@&{rid}>", "", cleaned)
        cleaned = re.sub(r"^(hey\s+)?zero[:,\s]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^(hey\s+)?robot[:,\s]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"@robot\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"@zero\b", "", cleaned, flags=re.IGNORECASE).strip()
        if not cleaned:
            await msg.reply("What's up? Give me something interesting to work on.")
            return

        if cleaned.lower() in ("!reset", "/reset", "!new", "/new"):
            clear_channel_session_id(msg.channel.id, "external")
            await msg.reply("🔄 Session reset for this channel.")
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

        try:
            await msg.channel.typing()
        except Exception:
            pass
        author_name = msg.author.display_name or msg.author.name

        # Group Chat Mid-Turn Steering Check
        if br.ext_active_proc is not None and br.ext_active_proc.returncode is None:
            br.is_ext_steering = True
            try:
                br.ext_active_proc.send_signal(signal.SIGINT)
            except Exception as se:
                print(f"[Bridge] Warning sending SIGINT for external group steering: {se}")

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
                "author_name": author_name
            })
            return

        await ext_turn_queue.put({
            "prompt": cleaned,
            "status_msg": None,
            "reply_target": msg,
            "attachments": saved_attachments,
            "is_steer": False,
            "mode": "external",
            "channel_id": msg.channel.id,
            "author_name": author_name
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

    if is_reload_intent(content):
        if reload_fn:
            await reload_fn(msg.channel, initiator=author_name, force=True, reason="Manual in-place bridge reload requested via #zero-chat")
        else:
            await execute_bridge_reload(bot, msg.channel, initiator=author_name, force=True, reason="Manual in-place bridge reload requested via #zero-chat")
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
        "!logs": ("⏳ *Running on-demand NAS Log Review...*", "Run the NAS log review using /workspace/tools/sidecars.py nas_logs. Report findings cleanly."),
        "/logs": ("⏳ *Running on-demand NAS Log Review...*", "Run the NAS log review using /workspace/tools/sidecars.py nas_logs. Report findings cleanly."),
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
        "!tokens": ("⏳ *Generating token budget report...*", "Run the daily token & Google AI Ultra compute budget usage report using /workspace/tools/sidecars.py token_report."),
        "/tokens": ("⏳ *Generating token budget report...*", "Run the daily token & Google AI Ultra compute budget usage report using /workspace/tools/sidecars.py token_report."),
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
        "/dockhand_update": ("⏳ *Checking Dockhand container updates...*", "Run the Dockhand container image check using /workspace/tools/dockhand_update.py.")
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
            "channel_id": msg.channel.id
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
                    "is_thread_task": True
                })
                return
            except Exception as te:
                print(f"[Bridge] Error creating explicit thread: {te}")

    # Build prompt content
    prompt_content = content
    if saved_attachments:
        hint = "\n\n[Attached file(s) available via view_file tool]:\n" + "\n".join(f"- {p}" for p in saved_attachments)
        prompt_content = (prompt_content or "Please inspect the attached file(s) and assist.") + hint

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
            "is_thread_task": is_thread_channel
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
        "is_thread_task": is_thread_channel
    })
