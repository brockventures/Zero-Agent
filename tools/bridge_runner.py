"""
Zero Discord Bridge - Process Execution & PTY Engine Module
Encapsulates subprocess lifecycle, pseudo-terminal (PTY) allocation,
JSON stream parsing, mid-turn steering, and output delivery.
"""

import asyncio
import json
import os
import pty
import re
import select
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import discord

from tools.bridge_formatting import (
    format_command_preview,
    format_for_discord,
    extract_agent_response,
    chunk_text,
    scrub_credentials,
    clean_discord_latex,
    generate_concise_thread_title,
    synthesize_thread_title,
    parse_thread_title_tag,
    is_internal_cli_leak,
    strip_internal_cli_chatter,
    parse_agy_error,
    format_agy_error_message,
)
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
    is_thread_retitled,
    mark_thread_retitled,
    record_in_flight,
    clear_in_flight,
    PersistentSessionKeySet,
    get_reset_session_keys,
)

PRINT_TIMEOUT = os.getenv("AGY_PRINT_TIMEOUT", "30m")

# Global execution & steering tracking
active_master_fd = None
active_proc = None
ext_active_proc = None
ext_active_master_fd = None
steering_channels = set()     # channel/thread IDs actively being steered
reset_session_keys = PersistentSessionKeySet(get_reset_session_keys())    # session keys (persisted to disk) to reset on next turn
thread_active_tasks = {}     # thread_id -> asyncio.Task

from tools.bridge_engine import TurnCoordinator, channel_active_procs


from tools.bridge_pipeline import (
    find_new_artifacts,
    prepare_turn_prompt,
    deliver_turn_output,
    TurnTimer,
)


def kill_process_tree(proc, sig=signal.SIGTERM):
    """Safely terminate a subprocess and its entire process group."""
    if not proc or proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass




def harvest_transcript_response(conv_id: str | None) -> str | None:
    """Harvest completed response from transcript files if stdout was truncated or cut off.

    Enforces active turn boundary: stops immediately if a USER_INPUT or CHECKPOINT step is
    encountered before finding a substantive PLANNER_RESPONSE, preventing prior turn responses
    from ever being harvested or re-delivered.
    """
    if not conv_id:
        return None
    brain_dir = Path("/root/.gemini/antigravity-cli/brain") / conv_id / ".system_generated" / "logs"
    for fname in ("transcript_full.jsonl", "transcript.jsonl"):
        tpath = brain_dir / fname
        if not tpath.exists():
            continue
        try:
            with open(tpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    data = json.loads(line_s)
                    step_type = data.get("type")
                    source = data.get("source")
                    role = data.get("role")

                    # Turn boundary check: Never cross into prior conversation turns or checkpoints
                    if (
                        step_type in ("USER_INPUT", "CHECKPOINT", "user", "USER")
                        or source in ("USER_EXPLICIT", "USER")
                        or role in ("user", "USER")
                    ):
                        break

                    if step_type == "PLANNER_RESPONSE":
                        content = data.get("content")
                        if content and isinstance(content, str) and content.strip():
                            stripped = content.strip()
                            if is_internal_cli_leak(stripped):
                                continue
                            return stripped
                except Exception:
                    continue
        except Exception as e:
            print(f"[BridgeRunner] Error reading transcript {tpath}: {e}")
    return None


async def execute_agy_turn(
    prompt: str,
    status_msg: discord.Message | None,
    reply_target: discord.Message | discord.TextChannel | discord.Thread,
    attachments: list[str],
    mode: str = "home",
    channel_id: int = TARGET_CHANNEL_ID,
    author_name: str = "",
    apply_presence_fn = None,
    button_choice_fn = None,
    quick_choice_view_cls = None,
    is_last_word: bool = False,
    last_word_bot_id: str = None,
    last_word_bot_name: str = None,
    last_word_streak: int = 0,
    queued_at: float | None = None,
    is_settle_reinvocation: bool = False,
):
    """Execute a single agy CLI turn with streaming status and output delivery."""
    global active_proc, active_master_fd, ext_active_proc, ext_active_master_fd, reset_session_keys

    # Thread Escalation State (Home Turf Root Channel Only) - preserved across retry attempts
    from tools.bridge_daemons import daemon_manager
    if daemon_manager.is_dedicated_channel(channel_id):
        worker = daemon_manager.get_worker(channel_id)
        if worker:
            try:
                return await worker.execute_turn(
                    prompt=prompt,
                    status_msg=status_msg,
                    reply_target=reply_target,
                    attachments=attachments,
                    mode=mode,
                    channel_id=channel_id,
                    author_name=author_name,
                    apply_presence_fn=apply_presence_fn,
                    button_choice_fn=button_choice_fn,
                    quick_choice_view_cls=quick_choice_view_cls,
                    is_last_word=is_last_word,
                    last_word_bot_id=last_word_bot_id,
                    last_word_bot_name=last_word_bot_name,
                    last_word_streak=last_word_streak,
                    queued_at=queued_at,
                    is_settle_reinvocation=is_settle_reinvocation,
                )
            except Exception as pe:
                print(f"[BridgeRunner] ⚠️ Persistent daemon turn error in channel {channel_id}: {pe}. Ensuring worker recycled & falling back to dynamic execution...")
                try:
                    await worker.recycle()
                except Exception:
                    pass

    ch_name_label = getattr(reply_target.channel, "name", "") if (reply_target and hasattr(reply_target, "channel")) else str(channel_id)
    timer = TurnTimer(
        channel_id=channel_id,
        channel_name=ch_name_label,
        queued_at=queued_at,
    )

    escalated_to_thread = False
    delivery_target = reply_target
    notify_root_channel = None
    thread_jump_url = None
    thread = None
    rules = get_runtime_rules()
    escalation_seconds = float(rules.get("auto_thread_escalation_seconds", 180.0))
    escalation_enabled = rules.get("auto_thread_escalation_enabled", True)

    max_retries = 2
    for attempt in range(max_retries + 1):
        sync_credentials()
        master_fd, slave_fd = pty.openpty()
        cmd = ["agy", "--add-dir=/workspace"]

        conv_id = get_channel_session_id(channel_id, mode)
        sess_key = "home" if (mode == "home" and int(channel_id) == TARGET_CHANNEL_ID) else str(channel_id)

        timer.mark_compaction_start()
        current_turns = increment_session_turn(sess_key)
        should_compact, compact_reason = check_compaction_needed(conv_id, current_turns)
        eng_carry_block = ""

        if should_compact or (sess_key in reset_session_keys):
            if sess_key in reset_session_keys:
                reset_session_keys.remove(sess_key)
            old_conv_id = conv_id
            reset_session_meta(sess_key)
            clear_channel_session_id(channel_id, mode)
            conv_id = None
            if old_conv_id:
                if mode == "home":
                    try:
                        from tools.session_summarizer import generate_summary, get_carryforward_context
                        generate_summary(conv_id=old_conv_id, sess_key=sess_key)
                        carry_ctx = get_carryforward_context(sess_key=sess_key)
                        if carry_ctx:
                            prompt = f"[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\n{carry_ctx}\n\n[CURRENT USER PROMPT]: {prompt}"
                            print(f"[BridgeRunner] 🔄 Auto-compacted home session for {sess_key} ({compact_reason or 'manual reset'}) and injected carry-forward context.")
                    except Exception as e:
                        print(f"[BridgeRunner] Error injecting carry-forward context: {e}")
                else:
                    try:
                        from tools.session_summarizer import generate_summary, get_engineering_carryforward_context
                        generate_summary(conv_id=old_conv_id, sess_key=sess_key)
                        eng_ctx = get_engineering_carryforward_context(sess_key=sess_key)
                        if eng_ctx:
                            eng_carry_block = f"\n[PREVIOUS SESSION ENGINEERING DELTA]:\n{eng_ctx}\n\n"
                        print(f"[BridgeRunner] 🔄 Auto-compacted external session for channel {sess_key} ({compact_reason}) and generated engineering carry-forward.")
                    except Exception as ce:
                        print(f"[BridgeRunner] Error injecting external carry-forward: {ce}")
        timer.mark_compaction_end()

        if conv_id:
            cmd.append(f"--conversation={conv_id}")

        timer.mark_ctx_start()
        turn_prompt = prepare_turn_prompt(
            prompt=prompt,
            mode=mode,
            channel_id=channel_id,
            sess_key=sess_key,
            author_name=author_name,
            reply_target=reply_target,
            eng_carry_block=eng_carry_block,
        )
        timer.mark_ctx_end()

        cmd.extend([
            f"-p={turn_prompt}",
            "--output-format=stream-json",
            "--dangerously-skip-permissions",
            f"--print-timeout={PRINT_TIMEOUT}"
        ])

        active_model = get_active_model()
        if active_model:
            cmd.append(f"--model={active_model}")

        if mode == "home":
            update_beacon("PROCESSING", prompt, channel_id=channel_id)

        ch_title = getattr(reply_target.channel, "name", "") if (reply_target and hasattr(reply_target, "channel")) else ""
        turn_text = f"Crunching in #{ch_title}..." if ch_title else "Processing task..."
        if apply_presence_fn:
            try:
                await apply_presence_fn(custom_activity=turn_text, status_override="dnd")
            except Exception:
                pass

        output_chunks = []
        auth_detected = False
        last_status_edit = time.time()
        turn_start_time = time.time()
        current_action = "Processing..."

        # Record in-flight turn for restart recovery
        if mode == "home":
            record_in_flight(
                channel_id=channel_id,
                prompt=prompt,
                conv_id=conv_id,
                status_msg_id=status_msg.id if status_msg else None
            )

        timer.mark_boot_start()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd="/workspace",
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True
            )
            os.close(slave_fd)
            timer.mark_boot_end()
            timer.mark_send()
            channel_active_procs[channel_id] = proc
            try:
                record_in_flight(
                    channel_id=channel_id,
                    prompt=prompt,
                    conv_id=conv_id,
                    status_msg_id=status_msg.id if status_msg else None,
                    pid=proc.pid,
                )
            except Exception:
                pass
            if mode == "home" and channel_id == TARGET_CHANNEL_ID:
                active_proc = proc
            elif mode == "external":
                ext_active_proc = proc

            coord = TurnCoordinator(
                channel_id=channel_id,
                prompt=prompt,
                mode=mode,
                reply_target=delivery_target,
                status_msg=status_msg,
                conv_id=conv_id,
                escalated_to_thread=escalated_to_thread,
                thread=thread,
                notify_root_channel=notify_root_channel,
                thread_jump_url=thread_jump_url,
            )
            step_idle_timeout = float(rules.get("turn_step_idle_seconds", 90.0))
            turn_timeout_seconds = float(rules.get("turn_watchdog_seconds", 300.0))
            agent_done_window = float(rules.get("agent_done_cutoff_seconds", 15.0))
            max_turn_ceiling = float(rules.get("turn_max_ceiling_seconds", 1800.0))
            timed_out = False
            is_hard_ceiling = False
            wedged_diagnostic = None
            last_agy_error = None
            init_received = False
            line_buffer = ""

            while True:
                r, _, _ = select.select([master_fd], [], [], 0.1)

                # Post-result & completion drain cutoff
                is_cutoff, trigger_label = coord.evaluate_completion_cutoffs(agent_done_window=agent_done_window)
                if is_cutoff:
                    print(f"[BridgeRunner] ⏱️ {trigger_label} cutoff reached, but PTY held open (PID {proc.pid}). Terminating to drain output...")
                    kill_process_tree(proc, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except (asyncio.TimeoutError, Exception):
                        kill_process_tree(proc, signal.SIGKILL)
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=0.5)
                        except Exception:
                            pass
                    break

                # Early wedge detection: If process tree has been completely silent for >= 45s, probe kernel wait states
                if coord.probe_process_wedge(proc.pid, output_snippet="".join(output_chunks[-5:])):
                    wedged_diagnostic = coord.wedged_diagnostic
                    timed_out = True
                    kill_process_tree(proc, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except (asyncio.TimeoutError, Exception):
                        kill_process_tree(proc, signal.SIGKILL)
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=0.5)
                        except Exception:
                            pass
                    break

                # Bridge-level turn watchdog: enforces hard inactivity timeout so silent hangs are killed
                is_timeout, reason = coord.check_watchdog_timeout(
                    step_idle_timeout=step_idle_timeout,
                    turn_timeout_seconds=turn_timeout_seconds,
                    max_turn_ceiling=max_turn_ceiling,
                )
                if is_timeout:
                    print(f"[BridgeRunner] ⏱️ Turn watchdog exceeded ({reason}) for PID {proc.pid} in channel {channel_id}. Terminating...")
                    timed_out = True
                    is_hard_ceiling = coord.is_hard_ceiling
                    kill_process_tree(proc, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.5)
                    except (asyncio.TimeoutError, Exception):
                        kill_process_tree(proc, signal.SIGKILL)
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=0.5)
                        except Exception:
                            pass
                    break

                # Dynamic Escalation to Discord Thread (#zero-chat root only)
                if await coord.check_thread_escalation(proc, escalation_enabled, escalation_seconds):
                    escalated_to_thread = coord.escalated_to_thread
                    delivery_target = coord.delivery_target
                    status_msg = coord.status_msg
                    thread = coord.thread
                    notify_root_channel = coord.notify_root_channel
                    thread_jump_url = coord.thread_jump_url
                    active_proc = None

                if master_fd in r:
                    try:
                        data = os.read(master_fd, 8192)
                        if not data:
                            break
                        text = data.decode("utf-8", errors="replace")
                        output_chunks.append(text)
                        last_activity_time = time.time()
                        line_buffer += text

                        # Touch liveness beacon so long active turns never trip false wedge alerts
                        if mode == "home":
                            now_touch = time.time()
                        coord.touch_activity()
                        coord.maybe_touch_beacon()

                        # Extract current progress/action from stream-json or raw text using robust line buffering
                        while "\n" in line_buffer:
                            line, line_buffer = line_buffer.split("\n", 1)
                            ev = coord.handle_line(line, timer=timer)
                            if ev and (ev.get("event") == "init" or ev.get("type") == "init"):
                                init_received = True

                        # Throttle progress updates to Discord (every 1.5s)
                        ticker_enabled = rules.get("live_status_ticker_enabled", False)
                        await coord.update_status_ticker(ticker_enabled=ticker_enabled)

                        # Detect Google OAuth URL on uninitialized raw terminal boot
                        if not init_received and not auth_detected:
                            for raw_l in text.splitlines():
                                raw_s = raw_l.strip()
                                if raw_s.startswith("Please visit this URL to authorize:"):
                                    clean_l = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_s)
                                    auth_url_match = re.search(r"(https://accounts\.google\.com/o/oauth2/auth[^\s\x1b]+)", clean_l)
                                    if auth_url_match:
                                        auth_detected = True
                                        active_master_fd = master_fd
                                        url = auth_url_match.group(1)
                                        await reply_target.reply(
                                            f"🔑 **Google Authentication Required**\n\n1. Open this link: [Authorize Antigravity]({url})\n2. Log in and approve access.\n3. **Paste the authorization code directly in this channel within 60 seconds.**"
                                        )
                    except OSError:
                        break
                    if proc.returncode is not None:
                        break
                else:
                    if proc.returncode is not None:
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
                        pass

            # Parse any remaining line fragment in line_buffer
            if line_buffer.strip():
                line_s = line_buffer.strip()
                if line_s.startswith("{") and line_s.endswith("}"):
                    try:
                        ev = json.loads(line_s)
                        if ev.get("event") == "result":
                            res_cid = ev.get("result", {}).get("conversation_id")
                            if res_cid:
                                set_channel_session_id(channel_id, mode, res_cid)
                    except Exception:
                        pass

            if not auth_detected:
                while True:
                    r, _, _ = select.select([master_fd], [], [], 0.05)
                    if not r or master_fd not in r:
                        break
                    try:
                        data = os.read(master_fd, 8192)
                        if not data:
                            break
                        output_chunks.append(data.decode("utf-8", errors="replace"))
                    except OSError:
                        break
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                kill_process_tree(proc, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except (asyncio.TimeoutError, Exception):
                    kill_process_tree(proc, signal.SIGKILL)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except Exception:
                        pass
            else:
                def _reset_active_fd():
                    global active_master_fd
                    if active_master_fd == master_fd:
                        try:
                            os.close(master_fd)
                        except OSError:
                            pass
                        active_master_fd = None
                asyncio.get_event_loop().call_later(65, _reset_active_fd)
                return

        finally:
            coord.cleanup_process(proc=proc, br_module=sys.modules[__name__])
            if mode == "home" and channel_id == TARGET_CHANNEL_ID:
                active_proc = None
            elif mode == "external":
                ext_active_proc = None

            if apply_presence_fn:
                try:
                    await apply_presence_fn()
                except Exception:
                    pass

        if channel_id in steering_channels:
            steering_channels.discard(channel_id)
            for fpath in attachments:
                try:
                    os.unlink(fpath)
                except Exception:
                    pass
            return

        full_raw = "".join(output_chunks).strip()
        is_transient_auth = any(sig.lower() in full_raw.lower() for sig in [
            "Eligibility check failed",
            "failed to get profile picture",
            "failed to get user info",
            "authentication failed or timed out",
            "timeout waiting for response"
        ])
        is_retryable_stall = timed_out and not is_hard_ceiling
        if (is_transient_auth or is_retryable_stall) and attempt < max_retries:
            reason_label = "Step inactivity / API stall" if is_retryable_stall else "Transient Google auth/API handshake hiccup"
            print(f"[BridgeRunner] 🔄 {reason_label} on attempt {attempt+1}/{max_retries}. Retrying in 1.5s...")
            if status_msg:
                try:
                    await status_msg.edit(content=f"⏳ *{reason_label}, retrying... ({attempt+1}/{max_retries})*")
                except Exception:
                    pass
            await asyncio.sleep(1.5)
            continue

        # Clean up temporary attachment files
        for fpath in attachments:
            try:
                os.unlink(fpath)
            except Exception:
                pass
        break

    full_raw = "".join(output_chunks).strip()
    active_cid = get_channel_session_id(channel_id, mode) or conv_id
    final_text = extract_agent_response(full_raw, conv_id=active_cid)

    # Fallback to on-disk transcript, leak scrubbing, or error beacon via TurnCoordinator
    final_text, _ = coord.harvest_fallback(
        final_text,
        active_cid=active_cid,
        proc=proc,
        turn_timeout_seconds=turn_timeout_seconds,
    )

    if coord.timed_out:
        timer.status = "TIMEOUT"
    elif coord.last_agy_error or (proc and proc.returncode == 3):
        timer.status = "ERROR_3_MODEL_API"
    elif proc and proc.returncode not in (0, None):
        timer.status = f"ERROR_{proc.returncode}"

    # TaskSettle Protocol: Detect and settle premature turn exits while background tasks are pending
    if (
        rules.get("task_settle_enabled", True)
        and not is_settle_reinvocation
        and not coord.timed_out
        and not coord.last_agy_error
    ):
        try:
            from tools.task_settle import evaluate_and_settle_turn

            settle_timeout = float(rules.get("task_settle_timeout_seconds", 25.0))
            was_settled, settled_text = await evaluate_and_settle_turn(
                conv_id=active_cid,
                channel_id=channel_id,
                mode=mode,
                status_msg=coord.status_msg,
                reply_target=reply_target,
                reinvoke_coro_fn=execute_agy_turn,
                timeout_seconds=settle_timeout,
                current_text=final_text,
                turn_kwargs={
                    "author_name": author_name,
                    "apply_presence_fn": apply_presence_fn,
                    "button_choice_fn": button_choice_fn,
                    "quick_choice_view_cls": quick_choice_view_cls,
                    "is_last_word": is_last_word,
                    "last_word_bot_id": last_word_bot_id,
                    "last_word_bot_name": last_word_bot_name,
                    "last_word_streak": last_word_streak,
                    "queued_at": queued_at,
                },
            )
            if was_settled:
                # The reinvoked turn finished and already delivered the substantive final output
                return settled_text
            elif settled_text:
                final_text = settled_text
        except Exception as settle_err:
            print(f"[BridgeRunner] TaskSettle error: {settle_err}")

    await deliver_turn_output(
        output_text=final_text,
        status_msg=coord.status_msg,
        reply_target=reply_target,
        mode=mode,
        channel_id=channel_id,
        conv_id=active_cid,
        turn_start_time=turn_start_time,
        button_choice_fn=button_choice_fn,
        quick_choice_view_cls=quick_choice_view_cls,
        delivery_target=coord.delivery_target,
        escalated_to_thread=coord.escalated_to_thread,
        notify_root_channel=coord.notify_root_channel,
        thread_jump_url=coord.thread_jump_url,
        is_last_word=is_last_word,
        last_word_bot_id=last_word_bot_id,
        last_word_bot_name=last_word_bot_name,
        last_word_streak=last_word_streak,
        timer=timer,
    )
