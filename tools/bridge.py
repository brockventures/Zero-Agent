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
import sys
import time
import discord
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


async def _dispatch_scheduled(prompt: str, job_name: str = "Sidecar"):
    await dispatch_scheduled_prompt(
        prompt=prompt,
        job_name=job_name,
        bot=bot,
        turn_queue=home_turn_queue,
        apply_presence_fn=_apply_presence,
        quick_choice_view_cls=QuickChoiceView,
        button_choice_fn=_button_choice_callback,
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
    await handle_on_ready(
        bot=bot,
        turn_queue=home_turn_queue,
        ext_turn_queue=ext_turn_queue,
        start_workers_fn=_start_queue_workers,
        start_scheduler_fn=_start_scheduler,
        presence_fn=_apply_presence,
    )


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
