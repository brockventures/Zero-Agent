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
from collections import deque
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
    harvest_transcript_response,
    AgyStreamParser,
    chunk_text,
    scrub_credentials,
    clean_discord_latex,
    generate_concise_thread_title,
    parse_agy_error,
    format_agy_error_message,
    is_internal_cli_leak,
)

PRINT_TIMEOUT = os.getenv("AGY_PRINT_TIMEOUT", "30m")

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

from tools.bridge_pipeline import (
    TurnTimer,
    prepare_turn_prompt,
    deliver_turn_output,
    find_new_artifacts,
)

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
        self.recent_stderr: deque[str] = deque(maxlen=50)
        self.last_agy_error: dict | None = None
        self.started_at: float = 0.0
        self.turn_count: int = 0
        self.last_turn_at: float = 0.0

    async def _drain_stderr(self):
        """Continuously drain stderr so pipe buffer never deadlocks the Go process,
        and capture structured AGY_ERROR payloads for instant triage.
        """
        try:
            while self.proc and self.proc.stderr:
                line_bytes = await self.proc.stderr.readline()
                if not line_bytes:
                    break
                line_s = line_bytes.decode("utf-8", errors="replace").strip()
                if line_s:
                    self.recent_stderr.append(line_s)
                    if "AGY_ERROR:" in line_s:
                        err_payload = parse_agy_error(line_s)
                        if err_payload:
                            self.last_agy_error = err_payload
                            print(f"[BridgeDaemon] 🚨 Captured AGY_ERROR in #{self.name}: {err_payload}")
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
        queued_at: float | None = None,
    ):
        """Execute a conversational turn on the persistent warm worker via stdin/stdout streaming."""
        from tools.bridge_runner import (
            channel_active_procs,
            steering_channels,
            reset_session_keys,
        )
        import tools.bridge_runner as br

        timer = TurnTimer(
            channel_id=self.channel_id,
            channel_name=self.name,
            queued_at=queued_at,
        )

        async with self.lock:
            # 1. Compaction / Reset Check
            timer.mark_compaction_start()
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
                        except Exception as ce:
                            print(f"[BridgeDaemon] Error generating external carry-forward context: {ce}")
            timer.mark_compaction_end()

            # Ingest any pending carry-forward summaries from overnight rollover if not already injected
            if "[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:" not in prompt and "[PREVIOUS SESSION ENGINEERING DELTA]:" not in prompt:
                if self.mode == "home":
                    try:
                        from tools.session_summarizer import get_carryforward_context
                        carry_ctx = get_carryforward_context(sess_key=self.sess_key)
                        if carry_ctx:
                            prompt = f"[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\n{carry_ctx}\n\n[CURRENT USER PROMPT]: {prompt}"
                            print(f"[BridgeDaemon] 📥 Injected pending carry-forward context for #{self.name} from overnight rollover.")
                    except Exception as e:
                        print(f"[BridgeDaemon] Error checking pending carry-forward context: {e}")
                else:
                    try:
                        from tools.session_summarizer import get_engineering_carryforward_context
                        eng_ctx = get_engineering_carryforward_context(sess_key=self.sess_key)
                        if eng_ctx:
                            eng_carry_block = f"\n[PREVIOUS SESSION ENGINEERING DELTA]:\n{eng_ctx}\n\n"
                            print(f"[BridgeDaemon] 📥 Injected pending engineering carry-forward delta for #{self.name} from overnight rollover.")
                    except Exception as ce:
                        print(f"[BridgeDaemon] Error checking pending engineering carry-forward context: {ce}")

            # 2. Ensure worker is running & responsive
            timer.mark_boot_start()
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
            timer.mark_boot_end()

            # 3. Format Prompt
            timer.mark_ctx_start()
            prepared_prompt = prepare_turn_prompt(
                prompt=prompt,
                mode=self.mode,
                channel_id=self.channel_id,
                sess_key=self.sess_key,
                author_name=author_name,
                reply_target=reply_target,
                eng_carry_block=eng_carry_block,
            )
            timer.mark_ctx_end()

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

            self.last_agy_error = None
            turn_start_time = time.time()
            last_status_edit = time.time()
            last_beacon_touch = time.time()
            last_activity_time = time.time()
            last_probe_time = time.time()
            current_action = "Processing..."
            output_response = None
            wedged_diagnostic = None

            # Thread Escalation State (#zero-chat root only)
            rules = get_runtime_rules()
            watchdog_timeout = float(rules.get("turn_watchdog_seconds", 300.0))
            max_turn_ceiling = float(rules.get("turn_max_ceiling_seconds", 1800.0))
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
                timer.mark_send()
                self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                await self.proc.stdin.drain()

                # 6. Stream events from stdout
                stream_parser = AgyStreamParser(conv_id=self.conv_id)
                output_response = ""
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

                    line_bytes = None
                    while line_bytes is None:
                        try:
                            line_bytes = await asyncio.wait_for(
                                self.proc.stdout.readline(), timeout=5.0
                            )
                            last_activity_time = time.time()
                        except asyncio.TimeoutError:
                            now_wait = time.time()
                            silence_dur = now_wait - last_activity_time
                            watchdog_timeout = float(rules.get("turn_watchdog_seconds", 300.0))
                            max_turn_ceiling = float(rules.get("turn_max_ceiling_seconds", 1200.0))

                            # Check for wedged interactive subprocess at >= 45s of silence
                            if (now_wait - last_activity_time) >= 45.0 and (now_wait - last_probe_time) >= 10.0 and self.proc and self.proc.pid:
                                last_probe_time = now_wait
                                try:
                                    from tools.process_probe import diagnose_process_tree
                                    diag = diagnose_process_tree(self.proc.pid)
                                    if diag.get("is_interactive_stdin"):
                                        print(
                                            f"[BridgeDaemon] 🚨 Wedged interactive subprocess detected for #{self.name} "
                                            f"(PID {self.proc.pid}): {diag['summary']}. Aborting turn early..."
                                        )
                                        wedged_diagnostic = diag
                                        culprit = diag.get("culprit") or {}
                                        c_name = culprit.get("name") or culprit.get("cmdline") or f"PID {self.proc.pid}"
                                        c_wchan = culprit.get("wchan") or "unknown"
                                        c_pid = culprit.get("pid") or self.proc.pid
                                        output_response = (
                                            f"⚠️ **Subprocess Wedged on Interactive Input:**\n\n"
                                            f"{diag['summary']}\n\n"
                                            f"• **Culprit:** `{c_name}` (PID {c_pid})\n"
                                            f"• **Kernel Wait Channel:** `{c_wchan}`\n"
                                            f"• **Diagnostic:** A tool spawned an interactive command without automated flags. Subprocess was terminated after {int(now_wait - turn_start_time)}s of silence to prevent an indefinite hang."
                                        )
                                        await self.recycle()
                                        break
                                except Exception as pe:
                                    print(f"[BridgeDaemon] Error probing process tree: {pe}")

                            # Activity-based watchdog check:
                            # 1. Idle silence timeout (no output/tool events for watchdog_timeout seconds)
                            # 2. Hard absolute ceiling (max_turn_ceiling) to catch indefinite loops
                            if silence_dur >= watchdog_timeout or (now_wait - turn_start_time) >= max_turn_ceiling:
                                reason = (
                                    f"silence/inactivity limit of {int(watchdog_timeout)}s"
                                    if silence_dur >= watchdog_timeout
                                    else f"hard ceiling of {int(max_turn_ceiling)}s"
                                )
                                print(
                                    f"[BridgeDaemon] ⚠️ Watchdog timeout: #{self.name} worker exceeded {reason}. Recycling worker..."
                                )
                                await self.recycle()
                                raise TimeoutError(
                                    f"Turn watchdog timeout ({reason}) exceeded in #{self.name}"
                                )

                    if wedged_diagnostic is not None:
                        break

                    if not line_bytes:
                        # Allow stderr drain task a moment to capture any final AGY_ERROR payload
                        await asyncio.sleep(0.05)
                        err_detail = ""
                        if self.last_agy_error:
                            err_detail = f": {self.last_agy_error}"
                        elif self.proc and self.proc.returncode is not None:
                            err_detail = f" (exit code {self.proc.returncode})"
                        print(f"[BridgeDaemon] ⚠️ Persistent worker for #{self.name} exited unexpectedly{err_detail}.")
                        await self.recycle()
                        if self.last_agy_error:
                            output_response = format_agy_error_message(
                                self.last_agy_error,
                                elapsed_sec=int(time.time() - turn_start_time),
                                pid_str=f"PID {self.proc.pid}" if self.proc else "",
                            )
                            break
                        raise RuntimeError(
                            f"Persistent worker for #{self.name} terminated unexpectedly{err_detail}"
                        )

                    line_s = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line_s:
                        continue

                    if line_s.startswith("{") and line_s.endswith("}"):
                        try:
                            ev = json.loads(line_s)
                            stream_parser.process_event(ev)
                            ev_name = ev.get("event")
                            if ev_name == "step_update":
                                step = ev.get("step_update", {})
                                stype = step.get("step_type")
                                sstate = step.get("state")
                                tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")
                                if stype == "tool" and tname and sstate != "DONE":
                                    timer.mark_event(tool_name=tname)
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
                                        timer.mark_event(is_token=True)
                                        current_action = "Drafting response..."
                            elif ev_name == "result":
                                timer.mark_result()
                                res_data = ev.get("result", {})
                                output_response = stream_parser.get_final_response(
                                    fallback_result=res_data.get("response", "")
                                )
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
                timer.finish(f"FAILED: {te}")
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

            # Fallback to on-disk transcript if output was empty or default placeholder and not explicit silence
            active_cid = self.conv_id or (
                thread.id
                if (escalated_to_thread and "thread" in locals() and thread and hasattr(thread, "id"))
                else None
            )
            is_empty_or_placeholder = (
                not output_response or output_response.startswith("*(") or len(output_response.strip()) == 0
            )
            is_leak = is_internal_cli_leak(output_response)
            is_silence = output_response.strip() in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP", "reply:none", "reply: none")

            if is_empty_or_placeholder or is_leak or (self.mode == "home" and is_silence):
                harvested = harvest_transcript_response(active_cid)
                if harvested and not is_internal_cli_leak(harvested):
                    print(
                        f"[BridgeDaemon] 🌾 Harvested response from on-disk transcript for session {active_cid} ({len(harvested)} chars)."
                    )
                    output_response = f"⚠️ *(Recovered from session transcript following process cutoff)*\n\n{harvested}"
                elif self.mode == "home":
                    output_response = "⚠️ **Turn Incomplete:** Agent process completed turn without generating text output."

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
                timer=timer,
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

    async def proactive_nightly_recycle(self):
        """Proactively recycle and re-warm all persistent workers overnight (e.g. during 2:00 AM PT rollover).

        Immediately terminates yesterday's workers, boots fresh warm sessions, and evicts
        their session keys from pending reset_session_keys so the first morning turns incur 0ms reset penalty.
        """
        from tools.bridge_state import reset_session_meta, remove_reset_session_key
        import tools.bridge_runner as br

        async def _recycle_one(w):
            try:
                old_cid = w.conv_id
                reset_session_meta(w.sess_key)
                await w.recycle(new_conv_id=None)
                # Ensure reset_session_keys is cleared for this persistent worker
                if hasattr(br, "reset_session_keys"):
                    br.reset_session_keys.discard(w.sess_key)
                remove_reset_session_key(w.sess_key)
                print(f"[BridgeDaemon] 🌙 Proactively recycled & warmed worker #{w.name} overnight (old_conv={old_cid}, new_conv={w.conv_id}).")
            except Exception as err:
                print(f"[BridgeDaemon] ⚠️ Error recycling worker #{w.name} during overnight rollover: {err}")

        tasks = [_recycle_one(w) for w in self.workers.values()]
        if tasks:
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

