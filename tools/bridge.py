#!/usr/bin/env python3
"""
Zero Discord Bridge - Main Entrypoint & Bot Orchestrator
Decomposed into isolated, testable modules:
- bridge_formatting.py: Discord message formatting, cards, LaTeX & credential scrubbers.
- bridge_state.py: Session mappings, turn tracking, compaction detection & persistent queues.
- bridge_runner.py: Subprocess lifecycle, pseudo-terminal (PTY) engine & JSON stream parsing.
- bridge_scheduler.py: Karakos persistent background scheduler & sidecar dispatchers.
- bridge_handlers.py: Discord bot event handlers, routing, thread workers & queues.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
import discord
from discord import app_commands
from discord.ext import commands

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

BOT_BOOT_TIME = time.time()

def _handle_sigterm(signum, frame):
    print("[Bridge] Received SIGTERM, exiting gracefully for restart...")
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_sigterm)

def _handle_sigchld(signum, frame):
    """Reap orphaned child processes to prevent zombies."""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid <= 0:
                break
        except (ChildProcessError, OSError):
            break

try:
    signal.signal(signal.SIGCHLD, _handle_sigchld)
except Exception as e:
    print(f"[Bridge] Warning setting SIGCHLD reaper: {e}")

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

# Re-exports for backward compatibility with existing tests and scripts
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
    TARGET_CHANNEL_ID,
    OWNER_USER_ID,
    PersistentTurnQueue,
    get_active_model,
    set_active_model,
    is_reload_intent,
    sync_credentials,
    clear_channel_session_id,
    PT_TZ,
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
    find_new_artifacts,
    execute_agy_turn,
    channel_active_procs,
    steering_channels,
    reset_session_keys,
    thread_active_tasks,
)
from tools.bridge_handlers import (
    ChoiceButton,
    QuickChoiceView,
    apply_bot_presence,
    execute_bridge_reload,
    is_bridge_busy,
    warm_channel_history,
    handle_button_choice,
    run_thread_turn_worker,
    queue_worker,
    external_queue_worker,
    handle_on_ready,
    handle_message,
)
from tools.bridge_scheduler import (
    KarakosScheduler,
    dispatch_scheduled_prompt,
)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ACTIVE_MODEL = get_active_model()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Persistent Queues
home_turn_queue = PersistentTurnQueue(QUEUE_FILE)
turn_queue = home_turn_queue  # alias for backward compatibility
ext_turn_queue = PersistentTurnQueue(EXT_QUEUE_FILE)

queue_worker_task = None
ext_queue_worker_task = None
scheduler = None


def _get_active_model():
    global ACTIVE_MODEL
    return ACTIVE_MODEL


def _set_active_model(model_name: str):
    global ACTIVE_MODEL
    ACTIVE_MODEL = model_name
    set_active_model(model_name)


async def _execute_reload(channel=None, initiator: str = "user", force: bool = True, reason: str = "Manual in-place bridge reload requested"):
    await execute_bridge_reload(bot, channel=channel, initiator=initiator, force=force, reason=reason)


async def _apply_presence(custom_activity: str = None, status_override: str = None):
    await apply_bot_presence(bot, custom_activity=custom_activity, status_override=status_override)


async def _button_choice_callback(choice_text: str, interaction: discord.Interaction):
    await handle_button_choice(choice_text, interaction, turn_queue=home_turn_queue, reload_fn=_execute_reload)


def _is_busy() -> list[str]:
    return is_bridge_busy(home_turn_queue, ext_turn_queue)


async def _dispatch_scheduled(prompt: str, job_name: str = "Sidecar", channel_id: int | None = None):
    await dispatch_scheduled_prompt(
        prompt=prompt,
        job_name=job_name,
        bot=bot,
        turn_queue=home_turn_queue,
        apply_presence_fn=_apply_presence,
        quick_choice_view_cls=QuickChoiceView,
        button_choice_fn=_button_choice_callback,
        channel_id=channel_id,
    )


def _start_queue_workers():
    global queue_worker_task, ext_queue_worker_task
    if queue_worker_task is None or queue_worker_task.done():
        queue_worker_task = asyncio.create_task(
            queue_worker(
                home_turn_queue=home_turn_queue,
                bot=bot,
                presence_fn=_apply_presence,
                button_choice_fn=_button_choice_callback,
                quick_choice_view_cls=QuickChoiceView,
                reload_fn=_execute_reload,
            )
        )
    if ext_queue_worker_task is None or ext_queue_worker_task.done():
        ext_queue_worker_task = asyncio.create_task(
            external_queue_worker(
                ext_turn_queue=ext_turn_queue,
                bot=bot,
                presence_fn=_apply_presence,
                button_choice_fn=_button_choice_callback,
                quick_choice_view_cls=QuickChoiceView,
                reload_fn=_execute_reload,
            )
        )


async def _start_scheduler():
    global scheduler
    if scheduler is None:
        scheduler = KarakosScheduler(
            dispatch_fn=_dispatch_scheduled,
            bot=bot,
            is_busy_fn=_is_busy,
            reload_fn=_execute_reload,
            presence_fn=_apply_presence,
        )
        await scheduler.start()
        print("[Antigravity] Karakos-style persistent JSON scheduler initialized.")


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"[Bridge] Successfully synced {len(synced)} slash commands with Discord.")
    except Exception as se:
        print(f"[Bridge] Warning syncing application command tree: {se}")

    await handle_on_ready(
        bot=bot,
        turn_queue=home_turn_queue,
        ext_turn_queue=ext_turn_queue,
        start_workers_fn=_start_queue_workers,
        start_scheduler_fn=_start_scheduler,
        presence_fn=_apply_presence,
    )


# --------------------------------------------------------------------------
# Discord Slash Commands (Application Commands)
# --------------------------------------------------------------------------

@bot.tree.command(name="new", description="Start a new threaded conversation with Zero")
@app_commands.describe(prompt="Initial prompt or task for the new thread (optional)")
async def slash_new(interaction: discord.Interaction, prompt: str = ""):
    """Spawn a new thread and begin a fresh conversation session."""
    await interaction.response.defer()
    clean_prompt = prompt.strip()
    task_title = generate_concise_thread_title(clean_prompt) if clean_prompt else "New Conversation"

    ch = interaction.channel
    is_home = (ch.id == TARGET_CHANNEL_ID) or (isinstance(ch, discord.Thread) and getattr(ch, "parent_id", None) == TARGET_CHANNEL_ID)
    selected_queue = home_turn_queue if is_home else ext_turn_queue
    mode = "home" if is_home else "external"

    if isinstance(ch, discord.Thread):
        clear_channel_session_id(ch.id, mode)
        reset_session_keys.discard(str(ch.id))
        if clean_prompt:
            await interaction.followup.send(f"🔄 Reset session for this thread. Starting task: **{clean_prompt}**")
            await selected_queue.put({
                "prompt": clean_prompt,
                "status_msg": None,
                "reply_target": ch,
                "attachments": [],
                "is_steer": False,
                "mode": mode,
                "channel_id": ch.id,
                "is_thread_task": True
            })
        else:
            await interaction.followup.send("🔄 Reset session for this thread. Send your next message to start fresh.")
        return

    try:
        if clean_prompt:
            resp = await interaction.followup.send(f"🧵 Starting new thread for: **{clean_prompt}**...")
        else:
            resp = await interaction.followup.send(f"🧵 Starting new threaded conversation...")

        thread = await resp.create_thread(name=f"🧵 {task_title}", auto_archive_duration=1440)
        await resp.edit(content=f"🧵 *Spawned new conversation thread:* {thread.mention} *(#{getattr(ch, 'name', 'chat')} remains free for other tasks)*")

        clear_channel_session_id(thread.id, mode)

        if clean_prompt:
            await selected_queue.put({
                "prompt": clean_prompt,
                "status_msg": None,
                "reply_target": thread,
                "attachments": [],
                "is_steer": False,
                "mode": mode,
                "channel_id": thread.id,
                "is_thread_task": True
            })
        else:
            await thread.send("👋 Started a fresh conversation thread. What would you like to work on?")
    except Exception as e:
        print(f"[Bridge] Error creating slash command thread: {e}")
        await interaction.followup.send(f"⚠️ Failed to create thread: {e}")


@bot.tree.command(name="reset", description="Reset the conversation session for the current channel or thread")
async def slash_reset(interaction: discord.Interaction):
    ch_id = interaction.channel_id
    is_home = (ch_id == TARGET_CHANNEL_ID) or (isinstance(interaction.channel, discord.Thread) and getattr(interaction.channel, "parent_id", None) == TARGET_CHANNEL_ID)
    mode = "home" if is_home else "external"
    clear_channel_session_id(ch_id, mode)
    sess_key = "home" if (ch_id == TARGET_CHANNEL_ID) else str(ch_id)
    reset_session_keys.discard(sess_key)
    await interaction.response.send_message("🔄 Conversation session reset for this channel/thread. Your next message will start a fresh session.")


@bot.tree.command(name="model", description="View or switch the active AI model")
@app_commands.describe(model_name="Model name or alias (e.g. 3.7, pro, sonnet, opus, flash-low)")
async def slash_model(interaction: discord.Interaction, model_name: str = ""):
    current_model = _get_active_model()
    if not model_name:
        models_help = (
            f"🤖 **Current Active Model:** `{current_model}`\n\n"
            "**Available Models & Aliases:**\n"
            "• `3.8` or `flash` → `gemini-3.8-flash-high` *(Default, fast & smart)*\n"
            "• `3.8-med` or `3.8-medium` → `gemini-3.8-flash-medium`\n"
            "• `3.8-lite`, `3.8-low` or `flash-low` → `gemini-3.8-flash-low` *(Lightweight & fast)*\n"
            "• `3.7` or `3.7-flash` → `gemini-3.7-flash-high`\n"
            "• `3.7-lite` or `3.7-low` → `gemini-3.7-flash-low`\n"
            "• `3.6` or `3.6-flash` → `gemini-3.6-flash-high`\n"
            "• `3.1-pro` or `pro` → `gemini-3.1-pro-high`\n"
            "• `sonnet` or `claude` → `claude-sonnet-4-6`\n"
            "• `opus` → `claude-opus-4-6-thinking`\n"
            "• `gpt` → `gpt-oss-120b-medium`\n"
        )
        await interaction.response.send_message(models_help)
        return

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
    resolved = aliases.get(model_name.lower().strip(), model_name.strip())
    _set_active_model(resolved)
    await interaction.response.send_message(f"🔄 Switched active model to **`{resolved}`** for subsequent turns (persisted across restarts).")


@bot.tree.command(name="logs", description="Run on-demand NAS log review across containers")
@app_commands.describe(since="Time range to scan logs for (default: 24h)")
async def slash_logs(interaction: discord.Interaction, since: str = "24h"):
    await interaction.response.defer()
    try:
        from tools.sidecars import run_sidecar_job, run_nas_log_review
        ok, rep, _ = run_sidecar_job("nas_logs", "NAS Log Review", run_nas_log_review, since=since)
        chunks = chunk_text(rep, limit=1950)
        for i, c in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(c)
            else:
                await interaction.channel.send(c)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error running NAS log review: {e}")


@bot.tree.command(name="triage", description="Run on-demand nightly agenda & inbox triage briefing")
async def slash_triage(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        from tools.sidecars import run_sidecar_job, run_nightly_triage
        ok, rep, _ = run_sidecar_job("triage", "Nightly Triage & Briefing", run_nightly_triage)
        chunks = chunk_text(rep, limit=1950)
        for i, c in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(c)
            else:
                await interaction.channel.send(c)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error running triage briefing: {e}")


@bot.tree.command(name="heartbeat", description="Run infrastructure heartbeat check")
async def slash_heartbeat(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        from tools.sidecars import run_sidecar_job, run_heartbeat_sweep
        ok, rep, _ = run_sidecar_job("heartbeat", "Heartbeat Sweep", run_heartbeat_sweep)
        chunks = chunk_text(rep, limit=1950)
        for i, c in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(c)
            else:
                await interaction.channel.send(c)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error running heartbeat sweep: {e}")


@bot.tree.command(name="tasks", description="Show active projects and task tracker")
async def slash_tasks(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        res = subprocess.run(["python3", "/workspace/tools/task_manager.py", "summary"], capture_output=True, text=True, timeout=15)
        out = res.stdout.strip() or res.stderr.strip() or "No tasks found."
        chunks = chunk_text(out, limit=1950)
        for i, c in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(c)
            else:
                await interaction.channel.send(c)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error querying tasks: {e}")


@bot.tree.command(name="sidecars", description="Show sidecar execution health and recent status")
async def slash_sidecars(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        from tools.sidecars import format_sidecar_status_report
        rep = format_sidecar_status_report()
        chunks = chunk_text(rep, limit=1950)
        for i, c in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(c)
            else:
                await interaction.channel.send(c)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error querying sidecars: {e}")


@bot.tree.command(name="title", description="Rename the current Discord thread")
@app_commands.describe(new_title="New title for the thread")
async def slash_title(interaction: discord.Interaction, new_title: str):
    ch = interaction.channel
    if not isinstance(ch, discord.Thread):
        await interaction.response.send_message("⚠️ `/title` can only be used inside a Discord Thread.", ephemeral=True)
        return
    clean_name = new_title.strip()
    if not clean_name.startswith("🧵"):
        clean_name = f"🧵 {clean_name}"
    try:
        await ch.edit(name=clean_name[:100])
        await interaction.response.send_message(f"✅ Renamed thread to: **{clean_name}**")
    except Exception as e:
        await interaction.response.send_message(f"⚠️ Failed to rename thread: {e}", ephemeral=True)


@bot.listen("on_interaction")
async def on_button_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        cid = interaction.data.get("custom_id", "")
        if cid.startswith("choice:"):
            choice_text = cid[7:]
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception:
                pass
            try:
                if interaction.message:
                    view = discord.ui.View.from_message(interaction.message)
                    for item in view.children:
                        item.disabled = True
                    await interaction.message.edit(view=view)
            except Exception:
                pass
            await _button_choice_callback(choice_text, interaction)


@bot.event
async def on_message(msg: discord.Message):
    await handle_message(
        msg=msg,
        bot=bot,
        home_turn_queue=home_turn_queue,
        ext_turn_queue=ext_turn_queue,
        reload_fn=_execute_reload,
        active_model_getter=_get_active_model,
        active_model_setter=_set_active_model,
    )


if __name__ == "__main__":
    if not TOKEN:
        print("[Antigravity] ERROR: DISCORD_BOT_TOKEN is not set.")
        exit(1)
    bot.run(TOKEN)
