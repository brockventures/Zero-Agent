#!/usr/bin/env python3
"""
Zero Discord Bridge - Main Entrypoint & Bot Orchestrator
Decomposed into isolated, testable modules:
- bridge_formatting.py: Discord message formatting, cards, LaTeX & credential scrubbers.
- bridge_state.py: Session mappings, turn tracking, compaction detection & persistent queues.
- bridge_runner.py: Subprocess lifecycle, pseudo-terminal (PTY) engine & JSON stream parsing.
- bridge_scheduler.py: Persistent background scheduler & sidecar dispatchers.
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
    is_container_restart_intent,
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
    execute_container_restart,
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
    BridgeScheduler,
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


async def _execute_reload(*args, **kwargs):
    target_channel = kwargs.pop("channel", None)
    initiator = kwargs.pop("initiator", "user")
    force = kwargs.pop("force", True)
    reason = kwargs.pop("reason", "Manual in-place bridge reload requested")

    if args:
        # If first arg is a discord Client/Bot instance, target channel is second arg
        if isinstance(args[0], (discord.Client, discord.AutoShardedClient)):
            if len(args) > 1:
                target_channel = args[1]
        else:
            target_channel = args[0]
            if len(args) > 1 and "initiator" not in kwargs:
                initiator = args[1]

    await execute_bridge_reload(bot, channel=target_channel, initiator=str(initiator), force=force, reason=str(reason))


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
        scheduler = BridgeScheduler(
            dispatch_fn=_dispatch_scheduled,
            bot=bot,
            is_busy_fn=_is_busy,
            reload_fn=_execute_reload,
            presence_fn=_apply_presence,
            quick_choice_view_cls=QuickChoiceView,
            button_choice_fn=_button_choice_callback,
        )
        await scheduler.start()
        print("[Antigravity] Persistent JSON bridge scheduler initialized.")


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
        reload_fn=_execute_reload,
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
                "is_thread_task": True,
                "queued_at": time.perf_counter(),
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
                "is_thread_task": True,
                "queued_at": time.perf_counter(),
            })
        else:
            await thread.send("👋 Started a fresh conversation thread. What would you like to work on?")
    except Exception as e:
        print(f"[Bridge] Error creating slash command thread: {e}")
        await interaction.followup.send(f"⚠️ Failed to create thread: {e}")


@bot.tree.command(name="reset", description="Reset the conversation session for the current channel or thread")
async def slash_reset(interaction: discord.Interaction):
    await interaction.response.defer()
    ch_id = interaction.channel_id
    is_home = (ch_id == TARGET_CHANNEL_ID) or (isinstance(interaction.channel, discord.Thread) and getattr(interaction.channel, "parent_id", None) == TARGET_CHANNEL_ID)
    mode = "home" if is_home else "external"
    clear_channel_session_id(ch_id, mode)
    sess_key = "home" if (ch_id == TARGET_CHANNEL_ID) else str(ch_id)
    reset_session_keys.discard(sess_key)
    try:
        bd = sys.modules.get("tools.bridge_daemons")
        dm = getattr(bd, "daemon_manager", None)
        if dm and dm.is_dedicated_channel(ch_id):
            await dm.recycle_worker(ch_id, new_conv_id=None)
    except Exception as de:
        print(f"[SlashReset] Error recycling worker on reset: {de}")
    await interaction.followup.send("🔄 Conversation session reset for this channel/thread. Your next message will start a fresh session.")


@bot.tree.command(name="model", description="View or switch the active AI model")
@app_commands.describe(model_name="Model name or alias (e.g. argon, argon-low, flash, sonnet, opus, 3.8-low)")
async def slash_model(interaction: discord.Interaction, model_name: str = ""):
    await interaction.response.defer()
    current_model = _get_active_model()
    from tools.bridge_models import get_fallback_model, list_available_models, resolve_model, set_fallback_model
    available = await asyncio.to_thread(list_available_models, True)
    fallback = get_fallback_model(available) if available else None

    if not model_name:
        help_lines = [
            f"🤖 **Current Active Model:** `{current_model}`",
            f"🛟 **Fallback Model:** `{fallback or 'unavailable'}`",
            "",
        ]
        if available:
            help_lines.append("**Available Models (live from the CLI):**")
            help_lines.extend(f"- `{mid}` ({label})" for mid, label in available.items())
            help_lines.append("")
            help_lines.append("*Shortcuts:* `argon`, `argon-med`, `argon-low`, `flash`, `3.8-low`, `sonnet`, `opus`, `gpt`")
        else:
            help_lines.append("⚠️ Could not fetch the live model list from the CLI. Try again in a moment.")
        help_lines.append("*Switch with:* `/model <name>` | *Set fallback with:* `/model fallback <name>`")
        await interaction.followup.send("\n".join(help_lines))
        return

    if not available:
        await interaction.followup.send("⚠️ Couldn't fetch the live model list from the CLI, so the model was not changed. Try again in a moment.")
        return

    target_m = model_name.strip()
    is_fallback_cmd = target_m.lower().split()[0] == "fallback"
    name = target_m.split(maxsplit=1)[1].strip() if is_fallback_cmd and len(target_m.split(maxsplit=1)) > 1 else ("" if is_fallback_cmd else target_m)
    if is_fallback_cmd and not name:
        await interaction.followup.send(f"🛟 Fallback model is `{get_fallback_model(available)}`. Set it with `/model fallback <name>`.")
        return

    resolved = resolve_model(name, available)
    if not resolved:
        valid = ", ".join(f"`{m}`" for m in available)
        await interaction.followup.send(f"❌ `{name}` isn't a model the CLI knows. Valid models: {valid}")
        return

    if is_fallback_cmd:
        set_fallback_model(resolved)
        await interaction.followup.send(f"🛟 Fallback model set to **`{resolved}`**. It is used (with a notice in chat) whenever the active model is unavailable.")
        return

    _set_active_model(resolved)
    try:
        bd = sys.modules.get("tools.bridge_daemons")
        dm = getattr(bd, "daemon_manager", None)
        if dm:
            await dm.recycle_all()
    except Exception as de:
        print(f"[SlashModel] Error recycling workers: {de}")
    await interaction.followup.send(f"🔄 Switched active model to **`{resolved}`** and recycled persistent workers.")


@slash_model.autocomplete("model_name")
async def slash_model_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    from tools.bridge_models import CACHE_TTL_SECONDS, _read_cache, list_available_models
    cache = _read_cache()
    available = cache["models"] if cache and isinstance(cache.get("models"), dict) else {}
    # Trigger non-blocking background cache refresh if stale or missing so autocomplete returns in <5ms
    if not cache or (time.time() - cache.get("fetched_at", 0)) >= CACHE_TTL_SECONDS:
        asyncio.create_task(asyncio.to_thread(list_available_models, False))

    choices: list[app_commands.Choice[str]] = []
    shortcuts = [
        ("argon (Gemini 4 Argon High)", "argon"),
        ("argon-med (Gemini 4 Argon Medium)", "argon-med"),
        ("argon-low (Gemini 4 Argon Low)", "argon-low"),
        ("flash (Gemini 3.8 Flash High)", "flash"),
        ("flash-medium (Gemini 3.8 Flash Medium)", "flash-medium"),
        ("flash-low (Gemini 3.8 Flash Low)", "flash-low"),
        ("sonnet (Claude Sonnet 5.5 High)", "sonnet"),
        ("sonnet-medium (Claude Sonnet 5.5 Medium)", "sonnet-medium"),
        ("opus (Claude Opus 5.5 High)", "opus"),
        ("gpt (GPT-OSS 120B Medium)", "gpt"),
    ]
    cur = current.lower().strip()
    for label, val in shortcuts:
        if not cur or cur in label.lower() or cur in val.lower():
            choices.append(app_commands.Choice(name=label[:100], value=val))
    if available:
        for mid, label in available.items():
            if not cur or cur in mid.lower() or cur in label.lower():
                if not any(c.value == mid for c in choices):
                    choices.append(app_commands.Choice(name=f"{mid} ({label})"[:100], value=mid))
    return choices[:25]



@bot.tree.command(name="logs", description="Run on-demand NAS log review across containers")
@app_commands.describe(since="Time range to scan logs for (default: 24h)")
async def slash_logs(interaction: discord.Interaction, since: str = "24h"):
    await interaction.response.defer()
    try:
        from tools.sidecars import run_sidecar_job, run_nas_log_review
        ok, rep, _ = await asyncio.to_thread(run_sidecar_job, "nas_logs", "NAS Log Review", run_nas_log_review, since=since)
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
        ok, rep, _ = await asyncio.to_thread(run_sidecar_job, "triage", "Nightly Triage & Briefing", run_nightly_triage)
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
        ok, rep, _ = await asyncio.to_thread(run_sidecar_job, "heartbeat", "Heartbeat Sweep", run_heartbeat_sweep)
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
        res = await asyncio.to_thread(subprocess.run, ["python3", "/workspace/tools/task_manager.py", "summary"], capture_output=True, text=True, timeout=15)
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
        from tools.sidecars import format_sidecar_status_summary
        rep = await asyncio.to_thread(format_sidecar_status_summary)
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
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    try:
        from tools.bridge_voice import handle_voice_state_update
        await handle_voice_state_update(bot, member, before, after)
    except Exception as ve:
        print(f"[Bridge] Error in on_voice_state_update: {ve}")


@bot.tree.command(name="voice", description="Join, leave, or check Zero's Gemini Live voice session in #zero-voice")
@app_commands.describe(action="join, leave, or status (default: join)", model="Optional Live model override (default: gemini-3.8-live)")
async def slash_voice(interaction: discord.Interaction, action: str = "join", model: str = ""):
    await interaction.response.defer()
    from tools.bridge_voice import ZERO_VOICE_CHANNEL_ID, get_voice_manager
    mgr = get_voice_manager(bot)
    act = action.lower().strip()

    if act in ("status", "info"):
        st = mgr.get_status_dict()
        if st["active"]:
            await interaction.followup.send(
                f"🎙️ **Zero Live Voice is ACTIVE** in <#{st['channel_id']}>\n"
                f"- **Model:** `{st['model']}` | **Voice:** `{st['voice']}`\n"
                f"- **Uptime:** {st['uptime_seconds']}s | **Audio:** {st['input_audio_seconds']}s in / {st['output_audio_seconds']}s out (~{st['est_audio_tokens']:,} audio tokens)"
            )
        else:
            await interaction.followup.send(
                f"🔇 **Zero Live Voice is idle.** Hop into <#{ZERO_VOICE_CHANNEL_ID}> (auto-joins) or run `/voice join`."
            )
        return

    if act in ("leave", "stop", "disconnect", "off"):
        rep = await mgr.stop_session()
        await interaction.followup.send(rep)
        return

    target_vc = None
    if getattr(interaction.user, "voice", None) and getattr(interaction.user.voice, "channel", None):
        target_vc = interaction.user.voice.channel
    if not target_vc:
        target_vc = bot.get_channel(ZERO_VOICE_CHANNEL_ID)
    if not target_vc and interaction.guild:
        try:
            target_vc = await interaction.guild.fetch_channel(ZERO_VOICE_CHANNEL_ID)
        except Exception:
            pass
    if not target_vc:
        await interaction.followup.send("⚠️ Could not locate `#zero-voice` channel.")
        return

    ok, rep = await mgr.start_session(target_vc, notify_channel=interaction.channel, model_override=model.strip() or None)
    await interaction.followup.send(rep)


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
