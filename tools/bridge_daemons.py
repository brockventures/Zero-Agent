"""
Zero Discord Bridge - Persistent Channel Worker Daemons Module
Manages warm, 24/7 persistent agy CLI instances in stream-json mode for dedicated
high-velocity channels (#zero-chat, #the-banana-stand, #lounge).
Eliminates the ~4.5s cold start down to ~150ms first-token latency while preserving
channel context isolation and air-gap boundaries.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import discord

from tools.bridge_state import (
    DATA_DIR,
    IN_FLIGHT_FILE,
    TARGET_CHANNEL_ID,
    PT_TZ,
    get_runtime_rules,
    get_channel_session_id,
    set_channel_session_id,
    clear_channel_session_id,
    increment_session_turn,
    reset_session_meta,
    check_compaction_needed,
    get_active_model,
    update_beacon,
    sync_credentials,
    get_gif_turn_count,
    increment_gif_turn,
    reset_gif_turn,
    has_reaction_gif,
    get_gif_prompt_guidance,
    record_in_flight,
    clear_in_flight,
)
from tools.bridge_formatting import (
    format_command_preview,
    format_for_discord,
    extract_agent_response,
    chunk_text,
    scrub_credentials,
    clean_discord_latex,
    generate_concise_thread_title,
)

PRINT_TIMEOUT = os.getenv("AGY_PRINT_TIMEOUT", "10m")

BANANA_STAND_CHANNEL_ID = 1534436119888793750
LOUNGE_CHANNEL_ID = 1534452820995080192

DEDICATED_CHANNEL_CONFIGS = {
    TARGET_CHANNEL_ID: {
        "name": "zero-chat",
        "mode": "home",
        "sess_key": "home",
    },
    BANANA_STAND_CHANNEL_ID: {
        "name": "the-banana-stand",
        "mode": "external",
        "sess_key": str(BANANA_STAND_CHANNEL_ID),
    },
    LOUNGE_CHANNEL_ID: {
        "name": "lounge",
        "mode": "external",
        "sess_key": str(LOUNGE_CHANNEL_ID),
    },
}


def is_dedicated_channel(channel_id: int | str) -> bool:
    """Check if channel ID belongs to the dedicated 24/7 persistent daemons."""
    try:
        cid = int(channel_id)
    except (ValueError, TypeError):
        return False
    return cid in DEDICATED_CHANNEL_CONFIGS


def is_persistent_daemons_enabled() -> bool:
    """Check runtime rules to see if persistent worker daemons are enabled."""
    rules = get_runtime_rules()
    return bool(rules.get("persistent_daemons_enabled", True))


def prepare_turn_prompt(
    prompt: str,
    mode: str,
    channel_id: int,
    sess_key: str,
    author_name: str = "",
    reply_target: discord.Message | discord.TextChannel | discord.Thread | None = None,
    eng_carry_block: str = "",
) -> str:
    """Format prompt with time context, gif cadence guidance, channel context, and air-gapped system prompts."""
    now_utc = datetime.now(timezone.utc)
    now_pt = now_utc.astimezone(PT_TZ)
    gif_guidance = get_gif_prompt_guidance(sess_key)

    if mode == "home":
        time_guidance = (
            f"[System Time & Timezone]: Current time is {now_pt.strftime('%A, %b %d, %Y %I:%M %p PT')} (America/Los_Angeles).\n"
            f"• Note: System VM clock and runtime metadata are UTC ({now_utc.strftime('%H:%M:%S UTC')}).\n"
            f"• Rule: ALWAYS use Pacific Time (PT). Never quote raw UTC timestamps or assume raw UTC is local time."
        )
        return f"{time_guidance}\n\n{gif_guidance}\n\n{prompt}"

    # External mode (Crab Cavern & multi-agent shared channels)
    channel_ctx_block = ""
    try:
        from tools.channel_history import format_channel_context
        target_msg_id = getattr(reply_target, "id", None)
        parent_cid = getattr(reply_target, "parent_id", None) if reply_target else None
        ch_ctx = format_channel_context(channel_id, limit=15, exclude_msg_id=target_msg_id, parent_channel_id=parent_cid)
        if ch_ctx:
            channel_ctx_block = f"\n{ch_ctx}\n\n"
    except Exception as ce:
        print(f"[BridgeDaemon] Warning formatting channel context: {ce}")

    manifest_block = ""
    try:
        from tools.session_summarizer import get_architecture_manifest
        manifest_block = get_architecture_manifest()
    except Exception as me:
        print(f"[BridgeDaemon] Warning generating architecture manifest: {me}")

    author_tag = f" from {author_name}" if author_name else ""
    time_block = (
        f"[System Time & Timezone]: Current time is {now_pt.strftime('%A, %b %d, %Y %I:%M %p PT')} (America/Los_Angeles).\n"
        f"• Note: System VM clock and runtime metadata are UTC ({now_utc.strftime('%H:%M:%S UTC')}).\n"
        f"• Rule: ALWAYS use Pacific Time (PT). Ryan Brock, the team, and all Crab Cavern operations are on Pacific Time.\n"
        f"• Never quote raw UTC timestamps or assume raw UTC is local time (e.g. 04:00 UTC = 9:00 PM PT previous day during PDT)."
    )

    rules = get_runtime_rules()
    tmpl = rules.get("external_system_prompt")
    if tmpl:
        try:
            ext_prompt = (
                tmpl.replace("{channel_context}", channel_ctx_block)
                .replace("{author_tag}", author_tag)
                .replace("{prompt}", prompt)
                .replace("{architecture_manifest}", manifest_block)
                .replace("{engineering_carryforward}", eng_carry_block)
                .replace("{time_context}", time_block)
                .replace("{gif_guidance}", gif_guidance)
            )
            if "{time_context}" not in tmpl and time_block not in ext_prompt:
                ext_prompt = f"{time_block}\n\n{ext_prompt}"
            if "{gif_guidance}" not in tmpl and "[GIF Cadence Tracker" not in ext_prompt:
                ext_prompt = f"{gif_guidance}\n\n{ext_prompt}"
            return ext_prompt
        except Exception:
            return f"{time_block}\n\n{gif_guidance}\n\n{manifest_block}\n\n{channel_ctx_block}[INBOUND MESSAGE{author_tag}]: {prompt}"
    else:
        return (
            "[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT]\n"
            "You are Zero, an autonomous systems engineering co-pilot collaborating with peer AI agents (Amos, Marvin) and developers in Crab Cavern.\n\n"
            f"{channel_ctx_block}"
            f"{time_block}\n\n"
            f"{gif_guidance}\n\n"
            f"[INBOUND MESSAGE{author_tag}]: {prompt}"
        )


async def deliver_turn_output(
    output_text: str,
    status_msg: discord.Message | None,
    reply_target: discord.Message | discord.TextChannel | discord.Thread,
    mode: str,
    channel_id: int,
    conv_id: str | None,
    turn_start_time: float,
    button_choice_fn=None,
    quick_choice_view_cls=None,
    delivery_target: discord.Message | discord.TextChannel | discord.Thread | None = None,
    escalated_to_thread: bool = False,
    notify_root_channel=None,
    thread_jump_url: str | None = None,
    is_last_word: bool = False,
    last_word_bot_id: str | None = None,
    last_word_bot_name: str | None = None,
    last_word_streak: int = 0,
):
    """Unified Discord response delivery engine across persistent daemons and dynamic turns."""
    from tools.bridge_runner import find_new_artifacts

    final_text = clean_discord_latex(output_text or "*(No output from agent)*")

    if mode == "external":
        clean_ext_text = re.sub(r"\[CHOICES:\s*[^\]]+\]", "", final_text).strip()
        clean_ext_text = scrub_credentials(clean_ext_text)
        clean_ext_text = clean_discord_latex(clean_ext_text)

        if clean_ext_text in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP", "*(No output from agent)*") or not clean_ext_text:
            if is_last_word and (last_word_bot_id or last_word_bot_name):
                try:
                    from tools.last_word_protocol import pause_bot
                    rules = get_runtime_rules()
                    pause_sec = float(rules.get("last_word_pause_minutes", 3)) * 60.0
                    pause_bot(
                        channel_id=channel_id,
                        bot_id=last_word_bot_id,
                        bot_name=last_word_bot_name,
                        duration_seconds=pause_sec,
                        reason=f"Last Word Protocol triggered after {last_word_streak} uninterrupted messages"
                    )
                except Exception as lwe:
                    print(f"[BridgeDaemon] Error setting Last Word pause on NO_REPLY: {lwe}")
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            print(f"[BridgeDaemon] Suppressed empty, [NO_REPLY], or placeholder in external channel {channel_id}")
            return

        try:
            from tools.channel_history import record_message
            ch_name = getattr(reply_target.channel, 'name', '') if (reply_target and hasattr(reply_target, 'channel')) else ''
            record_message(channel_id, ch_name, "Zero", is_bot=True, content=clean_ext_text)
        except Exception as re_err:
            print(f"[BridgeDaemon] Error recording Zero reply to channel history: {re_err}")

        chunks = chunk_text(clean_ext_text, 1900)
        if not chunks or (len(chunks) == 1 and chunks[0] == "*(No output from agent)*"):
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            print(f"[BridgeDaemon] Suppressed empty chunks in external channel {channel_id}")
            return

        target_dest = delivery_target if delivery_target else reply_target
        try:
            if status_msg:
                await status_msg.edit(content=chunks[0])
            else:
                await target_dest.reply(chunks[0])
        except Exception:
            await target_dest.reply(chunks[0])

        for ch in chunks[1:]:
            try:
                await target_dest.reply(ch)
            except Exception:
                await target_dest.channel.send(ch)

        # Trigger Last Word Protocol cooldown once response is delivered
        if is_last_word and (last_word_bot_id or last_word_bot_name):
            try:
                from tools.last_word_protocol import pause_bot
                rules = get_runtime_rules()
                pause_sec = float(rules.get("last_word_pause_minutes", 3)) * 60.0
                pause_bot(
                    channel_id=channel_id,
                    bot_id=last_word_bot_id,
                    bot_name=last_word_bot_name,
                    duration_seconds=pause_sec,
                    reason=f"Last Word Protocol triggered after {last_word_streak} uninterrupted messages"
                )
                print(f"[BridgeDaemon] Last Word Protocol: paused responses to {last_word_bot_name} ({last_word_bot_id}) in channel {channel_id} for {pause_sec/60:.0f}m.")
            except Exception as lwe:
                print(f"[BridgeDaemon] Error triggering Last Word Protocol pause: {lwe}")

        ext_sess_key = str(channel_id)
        if has_reaction_gif(clean_ext_text):
            reset_gif_turn(ext_sess_key)
            print(f"[BridgeDaemon] 🎬 Reaction GIF detected in reply for channel {ext_sess_key}. Reset turns_since_gif to 0.")
        else:
            new_c = increment_gif_turn(ext_sess_key)
            print(f"[BridgeDaemon] 📊 No GIF in reply for channel {ext_sess_key}. turns_since_gif incremented to {new_c}.")
        return

    # In Home Turf, silence tags are invalid - flag as incomplete turn so Ryan is never ghosted
    if final_text.strip() in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP", "reply:none", "reply: none"):
        final_text = "⚠️ **Turn Incomplete:** Received unexpected silent reply tag in home turf pairing mode."

    # Parse [CHOICES: ...] interactive buttons
    choice_view = None
    matches = list(re.finditer(r"\[CHOICES:\s*([^\]]+)\]", final_text))
    valid_match = None
    parsed_choices = []
    for m in reversed(matches):
        raw_choices = m.group(1).strip()
        delim = "|" if "|" in raw_choices else ","
        choices = [c.strip() for c in raw_choices.split(delim) if c.strip()]
        if choices and not all(c in ("...", "…", "Option 1", "Option 2", "Option 3") for c in choices):
            valid_match = m
            parsed_choices = choices
            break

    if valid_match and parsed_choices and quick_choice_view_cls and button_choice_fn:
        final_text = re.sub(r"\[CHOICES:\s*([^\]]+)\]", "", final_text).strip()
        choice_view = quick_choice_view_cls(parsed_choices, button_choice_fn)

    sync_credentials()

    home_sess_key = "home" if int(channel_id) == TARGET_CHANNEL_ID else str(channel_id)
    if has_reaction_gif(final_text):
        reset_gif_turn(home_sess_key)
        print(f"[BridgeDaemon] 🎬 Reaction GIF detected in reply for channel {home_sess_key}. Reset turns_since_gif to 0.")
    else:
        new_c = increment_gif_turn(home_sess_key)
        print(f"[BridgeDaemon] 📊 No GIF in reply for channel {home_sess_key}. turns_since_gif incremented to {new_c}.")

    # Look for new artifacts generated during this turn
    active_cid = get_channel_session_id(channel_id, mode) or conv_id
    new_artifacts = find_new_artifacts(turn_start_time, conv_id=active_cid)
    artifact_files = []
    for art in new_artifacts:
        try:
            artifact_files.append(discord.File(str(art), filename=art.name))
        except Exception as e:
            print(f"[BridgeDaemon] Failed to attach artifact {art}: {e}")

    chunks = chunk_text(final_text, 1900)
    target_dest = delivery_target if delivery_target else reply_target
    dest_cid = getattr(getattr(target_dest, "channel", None), "id", None) or getattr(target_dest, "id", None) or channel_id
    is_banana_stand = (int(dest_cid) == BANANA_STAND_CHANNEL_ID) if str(dest_cid).isdigit() else False
    held_turn_banana = False

    if is_banana_stand:
        try:
            from tools.banana import claim
            claim(subject="zero-external-turn")
            held_turn_banana = True
        except Exception as be:
            print(f"[BridgeDaemon] Banana claim notice for the-banana-stand turn: {be}")

    try:
        if chunks:
            if status_msg:
                try:
                    await status_msg.edit(content=chunks[0], view=choice_view if len(chunks) == 1 else None)
                except Exception:
                    if hasattr(target_dest, "reply"):
                        await target_dest.reply(chunks[0], view=choice_view if len(chunks) == 1 else None)
                    else:
                        await target_dest.send(chunks[0], view=choice_view if len(chunks) == 1 else None)
            else:
                if hasattr(target_dest, "reply"):
                    await target_dest.reply(chunks[0], view=choice_view if len(chunks) == 1 else None)
                else:
                    await target_dest.send(chunks[0], view=choice_view if len(chunks) == 1 else None)

            if len(chunks) > 1:
                for ch in chunks[1:-1]:
                    if hasattr(target_dest, "reply"):
                        await target_dest.reply(ch)
                    else:
                        await target_dest.send(ch)

                if hasattr(target_dest, "reply"):
                    await target_dest.reply(chunks[-1], view=choice_view)
                else:
                    await target_dest.send(chunks[-1], view=choice_view)

        if artifact_files:
            try:
                if hasattr(target_dest, "reply"):
                    await target_dest.reply(content="📎 **Artifact(s) generated during this turn:**", files=artifact_files)
                else:
                    await target_dest.send(content="📎 **Artifact(s) generated during this turn:**", files=artifact_files)
            except Exception:
                pass
    finally:
        if held_turn_banana:
            try:
                from tools.banana import release
                release()
            except Exception as err:
                print(f"[BridgeDaemon] Banana release warning for external turn: {err}")

    if escalated_to_thread and notify_root_channel and thread_jump_url:
        try:
            await notify_root_channel.send(f"✅ **Task Completed in Thread:** [View Full Results in Thread]({thread_jump_url})")
        except Exception as ne:
            print(f"[BridgeDaemon] Warning posting thread completion notice: {ne}")


class PersistentChannelWorker:
    """A dedicated, 24/7 warm agy CLI instance bound to a specific Discord channel."""

    def __init__(self, channel_id: int, name: str, mode: str, sess_key: str):
        self.channel_id = channel_id
        self.name = name
        self.mode = mode
        self.sess_key = sess_key
        self.proc: asyncio.subprocess.Process | None = None
        self.conv_id: str | None = None
        self.is_ready: bool = False
        self.lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.stderr_task: asyncio.Task | None = None
        self.started_at: float = 0.0
        self.turn_count: int = 0
        self.last_turn_at: float = 0.0

    async def _drain_stderr(self):
        """Continuously drain stderr so pipe buffer never deadlocks the Go process."""
        try:
            while self.proc and self.proc.stderr:
                line = await self.proc.stderr.readline()
                if not line:
                    break
        except (asyncio.CancelledError, Exception):
            pass

    async def start(self):
        """Boot the persistent agy CLI instance in stream-json mode and register init."""
        async with self.start_lock:
            if self.is_ready and self.proc is not None and self.proc.returncode is None:
                return

            self.is_ready = False
            self.conv_id = get_channel_session_id(self.channel_id, self.mode)
            cmd = [
                "agy",
                "--add-dir=/workspace",
                "--input-format=stream-json",
                "--output-format=stream-json",
                "--dangerously-skip-permissions",
                f"--print-timeout={PRINT_TIMEOUT}",
                "--print=",
            ]
            if self.conv_id:
                cmd.append(f"--conversation={self.conv_id}")

            active_model = get_active_model()
            if active_model:
                cmd.append(f"--model={active_model}")

            print(f"[BridgeDaemon] Spawning persistent worker for #{self.name} (conv={self.conv_id or 'new'})...")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd="/workspace",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.started_at = time.time()

            try:
                init_line = await asyncio.wait_for(proc.stdout.readline(), timeout=35.0)
                if not init_line:
                    raise RuntimeError(f"Worker for #{self.name} exited before emitting init")
                init_data = json.loads(init_line.decode("utf-8").strip())
                cid = init_data.get("conversation_id")
                if cid:
                    self.conv_id = cid
                    set_channel_session_id(self.channel_id, self.mode, cid)
                self.proc = proc
                self.is_ready = True
                print(f"[BridgeDaemon] 🟢 Warm worker ready for #{self.name} (PID: {self.proc.pid}, Conv: {self.conv_id})")
            except Exception as e:
                print(f"[BridgeDaemon] ❌ Failed to initialize worker for #{self.name}: {e}")
                try:
                    proc.kill()
                except Exception:
                    pass
                self.proc = None
                self.is_ready = False
                raise

            self.stderr_task = asyncio.create_task(self._drain_stderr())

    async def shutdown(self):
        """Gracefully terminate worker process."""
        self.is_ready = False
        if self.stderr_task and not self.stderr_task.done():
            self.stderr_task.cancel()
            self.stderr_task = None

        if self.proc:
            try:
                self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    self.proc.kill()
            except Exception:
                pass
            self.proc = None
            print(f"[BridgeDaemon] Worker #{self.name} terminated.")

    async def recycle(self, new_conv_id: str | None = None):
        """Recycle worker process with an optional new conversation ID (e.g. post-compaction)."""
        await self.shutdown()
        if new_conv_id:
            self.conv_id = new_conv_id
            set_channel_session_id(self.channel_id, self.mode, new_conv_id)
        else:
            self.conv_id = None
            clear_channel_session_id(self.channel_id, self.mode)
        await self.start()

    async def execute_turn(
        self,
        prompt: str,
        status_msg: discord.Message | None,
        reply_target: discord.Message | discord.TextChannel | discord.Thread,
        attachments: list[str],
        author_name: str = "",
        apply_presence_fn=None,
        button_choice_fn=None,
        quick_choice_view_cls=None,
        reload_fn=None,
        is_last_word: bool = False,
        last_word_bot_id: str | None = None,
        last_word_bot_name: str | None = None,
        last_word_streak: int = 0,
    ):
        """Execute a conversational turn on the persistent warm worker via stdin/stdout streaming."""
        from tools.bridge_runner import (
            channel_active_procs,
            steering_channels,
            reset_session_keys,
        )
        import tools.bridge_runner as br

        async with self.lock:
            # 1. Compaction / Reset Check
            current_turns = increment_session_turn(self.sess_key)
            should_compact, compact_reason = check_compaction_needed(self.conv_id, current_turns)
            eng_carry_block = ""

            if should_compact or (self.sess_key in reset_session_keys):
                if self.sess_key in reset_session_keys:
                    reset_session_keys.remove(self.sess_key)
                old_conv_id = self.conv_id
                reset_session_meta(self.sess_key)
                await self.recycle(new_conv_id=None)

                if old_conv_id:
                    if self.mode == "home":
                        try:
                            from tools.session_summarizer import generate_summary, get_carryforward_context
                            generate_summary(conv_id=old_conv_id, sess_key=self.sess_key)
                            carry_ctx = get_carryforward_context(sess_key=self.sess_key)
                            if carry_ctx:
                                prompt = f"[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\n{carry_ctx}\n\n[CURRENT USER PROMPT]: {prompt}"
                                print(f"[BridgeDaemon] 🔄 Auto-compacted home worker #{self.name} ({compact_reason or 'manual reset'}).")
                        except Exception as e:
                            print(f"[BridgeDaemon] Error generating home carry-forward context: {e}")
                    else:
                        try:
                            from tools.session_summarizer import generate_summary, get_engineering_carryforward_context
                            generate_summary(conv_id=old_conv_id, sess_key=self.sess_key)
                            eng_ctx = get_engineering_carryforward_context(sess_key=self.sess_key)
                            if eng_ctx:
                                eng_carry_block = f"\n[PREVIOUS SESSION ENGINEERING DELTA]:\n{eng_ctx}\n\n"
                                print(f"[BridgeDaemon] 🔄 Auto-compacted external worker #{self.name} ({compact_reason or 'manual reset'}).")
                        except Exception as ce:
                            print(f"[BridgeDaemon] Error generating external carry-forward context: {ce}")

            # Option B: Pre-turn Tool Output Scrubbing hook
            if self.conv_id:
                try:
                    from tools.transcript_scrubber import scrub_transcript_tool_outputs
                    scrub_transcript_tool_outputs(self.conv_id)
                except Exception as se:
                    print(f"[BridgeDaemon] Warning scrubbing transcript for {self.conv_id}: {se}")

            # 2. Ensure worker is running & responsive
            if not self.is_ready or self.proc is None or self.proc.returncode is not None:
                await self.start()

            # Non-blocking pre-turn drain of any stray residual bytes in stdout buffer
            if self.proc and self.proc.stdout:
                try:
                    buf = getattr(self.proc.stdout, "_buffer", None)
                    if buf and isinstance(buf, (bytearray, list)):
                        discarded_len = len(buf)
                        buf.clear()
                        if discarded_len > 0:
                            print(f"[BridgeDaemon] 🧹 Drained {discarded_len} residual bytes from #{self.name} stdout buffer.")
                except Exception as de:
                    print(f"[BridgeDaemon] Warning draining residual stdout buffer: {de}")

            # 3. Format Prompt
            prepared_prompt = prepare_turn_prompt(
                prompt=prompt,
                mode=self.mode,
                channel_id=self.channel_id,
                sess_key=self.sess_key,
                author_name=author_name,
                reply_target=reply_target,
                eng_carry_block=eng_carry_block,
            )

            # 4. Set global process hooks for mid-turn steering and presence
            channel_active_procs[self.channel_id] = self.proc
            if self.mode == "home":
                br.active_proc = self.proc
                update_beacon("PROCESSING", prompt, channel_id=self.channel_id)
                record_in_flight(
                    channel_id=self.channel_id,
                    prompt=prompt,
                    conv_id=self.conv_id,
                    status_msg_id=status_msg.id if status_msg else None,
                )
            else:
                br.ext_active_proc = self.proc

            ch_title = getattr(reply_target.channel, "name", "") if (reply_target and hasattr(reply_target, "channel")) else self.name
            turn_text = f"Crunching in #{ch_title}..." if ch_title else "Processing task..."
            if apply_presence_fn:
                try:
                    await apply_presence_fn(custom_activity=turn_text, status_override="dnd")
                except Exception:
                    pass

            turn_start_time = time.time()
            last_status_edit = time.time()
            last_beacon_touch = time.time()
            current_action = "Processing..."
            output_response = None

            # Thread Escalation State (#zero-chat root only)
            rules = get_runtime_rules()
            watchdog_timeout = float(rules.get("turn_watchdog_seconds", 300.0))
            escalation_seconds = float(rules.get("auto_thread_escalation_seconds", 180.0))
            escalation_enabled = rules.get("auto_thread_escalation_enabled", True)
            escalated_to_thread = False
            delivery_target = reply_target
            notify_root_channel = None
            thread_jump_url = None
            thread = None

            is_root_eligible = (
                self.mode == "home"
                and self.channel_id == TARGET_CHANNEL_ID
                and reply_target is not None
                and hasattr(reply_target, "create_thread")
                and not isinstance(getattr(reply_target, "channel", None), discord.Thread)
                and escalation_enabled
                and escalation_seconds > 0
            )

            try:
                # 5. Send NDJSON user message on stdin
                payload = {"event": "user", "message": {"content": prepared_prompt}}
                self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                await self.proc.stdin.drain()

                # 6. Stream events from stdout
                while True:
                    now = time.time()
                    if (
                        is_root_eligible
                        and not escalated_to_thread
                        and (now - turn_start_time) >= escalation_seconds
                    ):
                        try:
                            escalated_to_thread = True
                            is_root_eligible = False
                            clean_title = generate_concise_thread_title(prompt)
                            thread = await reply_target.create_thread(
                                name=f"🧵 {clean_title}", auto_archive_duration=1440
                            )
                            await reply_target.reply(
                                f"🧵 *Task execution exceeded {int(escalation_seconds)}s — migrating deliverable to {thread.mention}. `#zero-chat` remains free.*"
                            )
                            status_msg = None
                            delivery_target = thread
                            notify_root_channel = getattr(reply_target, "channel", None)
                            thread_jump_url = thread.jump_url
                            channel_active_procs[thread.id] = self.proc
                            if TARGET_CHANNEL_ID in channel_active_procs:
                                del channel_active_procs[TARGET_CHANNEL_ID]
                        except Exception as te:
                            print(f"[BridgeDaemon] Warning escalating turn to thread: {te}")

                    try:
                        line_bytes = await asyncio.wait_for(
                            self.proc.stdout.readline(), timeout=watchdog_timeout
                        )
                    except asyncio.TimeoutError:
                        print(
                            f"[BridgeDaemon] ⚠️ Watchdog timeout: #{self.name} worker exceeded {watchdog_timeout}s without output. Recycling worker..."
                        )
                        await self.recycle()
                        raise TimeoutError(
                            f"Turn watchdog timeout ({watchdog_timeout}s) exceeded in #{self.name}"
                        )

                    if not line_bytes:
                        print(f"[BridgeDaemon] ⚠️ Persistent worker for #{self.name} exited unexpectedly.")
                        await self.recycle()
                        raise RuntimeError(
                            f"Persistent worker for #{self.name} terminated unexpectedly"
                        )

                    line_s = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line_s:
                        continue

                    if line_s.startswith("{") and line_s.endswith("}"):
                        try:
                            ev = json.loads(line_s)
                            ev_name = ev.get("event")
                            if ev_name == "step_update":
                                step = ev.get("step_update", {})
                                stype = step.get("step_type")
                                tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")
                                if stype == "tool" and tname:
                                    tinfo = step.get("tool_info", {})
                                    params = tinfo.get("parameters", {})
                                    if tname == "run_command" and "CommandLine" in params:
                                        current_action = format_command_preview(params["CommandLine"])
                                    elif tname == "view_file" and "AbsolutePath" in params:
                                        fpath = Path(params["AbsolutePath"]).name
                                        current_action = f"Reading: {fpath}..."
                                    elif tname == "grep_search" and "Query" in params:
                                        current_action = f"Searching: {params['Query'][:50]}..."
                                    elif tname == "replace_file_content" and "TargetFile" in params:
                                        fpath = Path(params["TargetFile"]).name
                                        current_action = f"Editing: {fpath}..."
                                    elif tname == "call_mcp_tool":
                                        mcp_tool = params.get("ToolName", "mcp")
                                        current_action = f"Querying {mcp_tool}..."
                                    else:
                                        current_action = f"Calling tool: {tname}..."
                                elif stype == "agent_response":
                                    if step.get("text_delta"):
                                        current_action = "Drafting response..."
                            elif ev_name == "result":
                                res_data = ev.get("result", {})
                                output_response = res_data.get("response", "")
                                res_cid = res_data.get("conversation_id")
                                if res_cid:
                                    if escalated_to_thread and thread and hasattr(thread, "id"):
                                        set_channel_session_id(thread.id, self.mode, res_cid)
                                        # Root channel recycles to a fresh session
                                        clear_channel_session_id(TARGET_CHANNEL_ID, "home")
                                        await self.recycle(new_conv_id=None)
                                        print(
                                            f"[BridgeDaemon] 🧵 Bound session {res_cid} to migrated thread {thread.id} and recycled root worker."
                                        )
                                    else:
                                        self.conv_id = res_cid
                                        set_channel_session_id(self.channel_id, self.mode, res_cid)
                                break
                        except Exception:
                            pass

                    now_edit = time.time()
                    if status_msg and (now_edit - last_status_edit >= 1.5):
                        ticker_enabled = get_runtime_rules().get("live_status_ticker_enabled", False)
                        if ticker_enabled:
                            clean_action = re.sub(
                                r"\x1b(?:\[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", current_action
                            )
                            try:
                                await status_msg.edit(content=f"⏳ *{clean_action}*")
                                last_status_edit = now_edit
                            except Exception:
                                pass

                    now_touch = time.time()
                    if self.mode == "home" and (now_touch - last_beacon_touch >= 10):
                        update_beacon("PROCESSING", prompt, channel_id=self.channel_id)
                        last_beacon_touch = now_touch

            except Exception as te:
                print(f"[BridgeDaemon] ⚠️ Turn execution failed in #{self.name}: {te}. Recycling worker to purge pipe state...")
                try:
                    await self.recycle()
                except Exception as rec_err:
                    print(f"[BridgeDaemon] Error recycling worker after failure: {rec_err}")
                raise
            finally:
                if self.channel_id in channel_active_procs:
                    del channel_active_procs[self.channel_id]
                if (
                    escalated_to_thread
                    and "thread" in locals()
                    and hasattr(thread, "id")
                    and thread.id in channel_active_procs
                ):
                    del channel_active_procs[thread.id]
                if self.mode == "home":
                    br.active_proc = None
                    try:
                        clear_in_flight(self.channel_id)
                    except Exception:
                        pass
                else:
                    br.ext_active_proc = None

                if not any(p and p.returncode is None for p in channel_active_procs.values()):
                    update_beacon("IDLE", "")

                if apply_presence_fn:
                    try:
                        await apply_presence_fn()
                    except Exception:
                        pass

            # Clean attachments
            for fpath in attachments:
                try:
                    os.unlink(fpath)
                except Exception:
                    pass

            # Steering check
            if self.channel_id in steering_channels:
                steering_channels.discard(self.channel_id)
                print(f"[BridgeDaemon] Mid-turn steering executed in #{self.name}. Discarding stale turn response.")
                return

            self.turn_count += 1
            self.last_turn_at = time.time()

            # Deliver response output to Discord
            await deliver_turn_output(
                output_text=output_response or "*(No output from agent)*",
                status_msg=status_msg,
                reply_target=reply_target,
                mode=self.mode,
                channel_id=self.channel_id,
                conv_id=self.conv_id,
                turn_start_time=turn_start_time,
                button_choice_fn=button_choice_fn,
                quick_choice_view_cls=quick_choice_view_cls,
                delivery_target=delivery_target,
                escalated_to_thread=escalated_to_thread,
                notify_root_channel=notify_root_channel,
                thread_jump_url=thread_jump_url,
                is_last_word=is_last_word,
                last_word_bot_id=last_word_bot_id,
                last_word_bot_name=last_word_bot_name,
                last_word_streak=last_word_streak,
            )


class PersistentDaemonManager:
    """Manages the lifecycle of dedicated persistent workers."""

    def __init__(self):
        self.workers: dict[int, PersistentChannelWorker] = {}
        for cid, cfg in DEDICATED_CHANNEL_CONFIGS.items():
            self.workers[cid] = PersistentChannelWorker(
                channel_id=cid,
                name=cfg["name"],
                mode=cfg["mode"],
                sess_key=cfg["sess_key"],
            )

    def is_dedicated_channel(self, channel_id: int | str) -> bool:
        if not is_persistent_daemons_enabled():
            return False
        return is_dedicated_channel(channel_id)

    def get_worker(self, channel_id: int | str) -> PersistentChannelWorker | None:
        try:
            cid = int(channel_id)
        except (ValueError, TypeError):
            return None
        return self.workers.get(cid)

    async def start_all(self):
        """Warm up all dedicated persistent daemons on bot ready."""
        if not is_persistent_daemons_enabled():
            print("[BridgeDaemon] Persistent worker daemons disabled in runtime rules.")
            return
        tasks = [w.start() for w in self.workers.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for w, res in zip(self.workers.values(), results):
            if isinstance(res, Exception):
                print(f"[BridgeDaemon] ⚠️ Failed to warm worker #{w.name}: {res}")

    async def shutdown_all(self):
        """Cleanly terminate all persistent workers before bridge reload."""
        tasks = [w.shutdown() for w in self.workers.values()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def recycle_worker(self, channel_id: int | str, new_conv_id: str | None = None):
        """Recycle a specific persistent worker."""
        worker = self.get_worker(channel_id)
        if worker:
            await worker.recycle(new_conv_id=new_conv_id)

    async def recycle_all(self):
        """Recycle all persistent workers (e.g. after model switch)."""
        tasks = [w.recycle() for w in self.workers.values()]
        await asyncio.gather(*tasks, return_exceptions=True)


# Global singleton daemon manager
daemon_manager = PersistentDaemonManager()


async def warmup_persistent_daemons():
    """Ensure dedicated persistent daemons are warmed up and ready before turn execution."""
    if os.getenv("TESTING") == "1" or not is_persistent_daemons_enabled():
        return
    await daemon_manager.start_all()


def ensure_persistent_daemons_running():
    """Ensure dedicated persistent daemons are warmed up in the background (legacy sync entrypoint)."""
    if os.getenv("TESTING") == "1" or not is_persistent_daemons_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(daemon_manager.start_all())
    except RuntimeError:
        pass

