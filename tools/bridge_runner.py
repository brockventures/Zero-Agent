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
)
from tools.bridge_state import (
    DATA_DIR,
    IN_FLIGHT_FILE,
    TARGET_CHANNEL_ID,
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
)

PRINT_TIMEOUT = os.getenv("AGY_PRINT_TIMEOUT", "30m")

# Global execution & steering tracking
active_master_fd = None
active_proc = None
ext_active_proc = None
ext_active_master_fd = None
channel_active_procs = {}    # channel_id -> subprocess.Popen
steering_channels = set()     # channel/thread IDs actively being steered
reset_session_keys = set()    # session keys (e.g. "home" or thread_id) to reset on next turn
is_ext_steering = False
thread_active_tasks = {}     # thread_id -> asyncio.Task


def find_new_artifacts(start_time: float, conv_id: str | None = None) -> list[Path]:
    """Find newly created artifact files in the brain conversation directory."""
    try:
        brain_root = Path("/root/.gemini/antigravity-cli/brain")
        if not brain_root.exists():
            return []
        if conv_id:
            target_dir = brain_root / conv_id
            if target_dir.exists() and target_dir.is_dir():
                artifacts = []
                for item in target_dir.iterdir():
                    if item.is_file() and not item.name.startswith("."):
                        if item.stat().st_mtime >= start_time - 1.0:
                            artifacts.append(item)
                return artifacts
        conv_dirs = [d for d in brain_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not conv_dirs:
            return []
        latest_conv = max(conv_dirs, key=lambda d: d.stat().st_mtime)
        artifacts = []
        for item in latest_conv.iterdir():
            if item.is_file() and not item.name.startswith("."):
                if item.stat().st_mtime >= start_time - 1.0:
                    artifacts.append(item)
        return artifacts
    except Exception:
        return []


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
    quick_choice_view_cls = None
):
    """Execute a single agy CLI turn with streaming status and output delivery."""
    global active_proc, active_master_fd, ext_active_proc, ext_active_master_fd, reset_session_keys, is_ext_steering

    # Thread Escalation State (Home Turf Root Channel Only) - preserved across retry attempts
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
        master_fd, slave_fd = pty.openpty()
        cmd = ["agy"]

        conv_id = get_channel_session_id(channel_id, mode)

        if mode == "home":
            sess_key = "home" if int(channel_id) == TARGET_CHANNEL_ID else str(channel_id)
            current_turns = increment_session_turn(sess_key)
            should_compact, compact_reason = check_compaction_needed(conv_id, current_turns)

            # Auto-compact on turn limit, file size, step ceiling, age, or manual !reset
            if should_compact or (sess_key in reset_session_keys):
                if sess_key in reset_session_keys:
                    reset_session_keys.remove(sess_key)
                old_conv_id = conv_id
                reset_session_meta(sess_key)
                clear_channel_session_id(channel_id, "home")
                conv_id = None
                if old_conv_id:
                    try:
                        from tools.session_summarizer import generate_summary, get_carryforward_context
                        generate_summary(conv_id=old_conv_id, sess_key=sess_key)
                        carry_ctx = get_carryforward_context(sess_key=sess_key)
                        if carry_ctx:
                            prompt = f"[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\n{carry_ctx}\n\n[CURRENT USER PROMPT]: {prompt}"
                            print(f"[BridgeRunner] 🔄 Auto-compacted session for {sess_key} ({compact_reason or 'manual reset'}) and injected carry-forward context.")
                    except Exception as e:
                        print(f"[BridgeRunner] Error injecting carry-forward context: {e}")

            if conv_id:
                cmd.append(f"--conversation={conv_id}")

            gif_guidance = get_gif_prompt_guidance(sess_key)
            home_prompt = f"{gif_guidance}\n\n{prompt}"

            cmd.extend([
                f"-p={home_prompt}",
                "--output-format=stream-json",
                "--dangerously-skip-permissions",
                f"--print-timeout={PRINT_TIMEOUT}"
            ])
        else:
            # External mode (Crab Cavern & multi-agent shared threads)
            sess_key = str(channel_id)
            current_turns = increment_session_turn(sess_key)
            should_compact, compact_reason = check_compaction_needed(conv_id, current_turns)

            eng_carry_block = ""
            if should_compact or (sess_key in reset_session_keys):
                if sess_key in reset_session_keys:
                    reset_session_keys.remove(sess_key)
                old_conv_id = conv_id
                reset_session_meta(sess_key)
                clear_channel_session_id(channel_id, "external")
                conv_id = None
                if old_conv_id:
                    try:
                        from tools.session_summarizer import generate_summary, get_engineering_carryforward_context
                        generate_summary(conv_id=old_conv_id, sess_key=sess_key)
                        eng_ctx = get_engineering_carryforward_context(sess_key=sess_key)
                        if eng_ctx:
                            eng_carry_block = f"\n[PREVIOUS SESSION ENGINEERING DELTA]:\n{eng_ctx}\n\n"
                        print(f"[BridgeRunner] 🔄 Auto-compacted external session for channel {sess_key} ({compact_reason}) and generated engineering carry-forward.")
                    except Exception as ce:
                        print(f"[BridgeRunner] Error injecting external carry-forward: {ce}")

            if conv_id:
                cmd.append(f"--conversation={conv_id}")

            channel_ctx_block = ""
            try:
                from tools.channel_history import format_channel_context
                target_msg_id = getattr(reply_target, "id", None)
                parent_cid = getattr(reply_target, "parent_id", None) if reply_target else None
                ch_ctx = format_channel_context(channel_id, limit=15, exclude_msg_id=target_msg_id, parent_channel_id=parent_cid)
                if ch_ctx:
                    channel_ctx_block = f"\n{ch_ctx}\n\n"
            except Exception as ce:
                print(f"[BridgeRunner] Warning formatting channel context: {ce}")

            manifest_block = ""
            try:
                from tools.session_summarizer import get_architecture_manifest
                manifest_block = get_architecture_manifest()
            except Exception as me:
                print(f"[BridgeRunner] Warning generating architecture manifest: {me}")

            author_tag = f" from {author_name}" if author_name else ""
            rules = get_runtime_rules()
            tmpl = rules.get("external_system_prompt")
            if tmpl:
                try:
                    ext_prompt = (tmpl
                        .replace("{channel_context}", channel_ctx_block)
                        .replace("{author_tag}", author_tag)
                        .replace("{prompt}", prompt)
                        .replace("{architecture_manifest}", manifest_block)
                        .replace("{engineering_carryforward}", eng_carry_block)
                    )
                except Exception:
                    ext_prompt = f"{manifest_block}\n\n{channel_ctx_block}[INBOUND MESSAGE{author_tag}]: {prompt}"
            else:
                ext_prompt = (
                    "[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT]\n"
                    "You are Zero, an autonomous systems engineering co-pilot collaborating with peer AI agents (Amos, Marvin) and developers in Crab Cavern.\n"
                    "Persona: Razor-sharp wit, effortless swagger, technical confidence, and top-tier SWE/systems engineering capability.\n"
                    "Capabilities: You HAVE FULL PERMISSION to write code, test scripts, solve engineering problems, analyze systems, and run benchmarks.\n\n"
                    "CRITICAL MULTI-AGENT CONVERSATION & TARGETING RULES:\n"
                    "1. PARTIAL ADDRESS & SCOPE PARSING: In group channels, an inbound message may be split across multiple entities (e.g. '@Zero check X. @Amos how does Y look?').\n"
                    "   - Carefully parse each sentence/clause. Respond ONLY to the specific task or question addressed to YOU (Zero).\n"
                    "   - NEVER hijack, answer, or complete tasks/questions directed at peer agents (Amos, Marvin) or other humans. Leave their portions strictly for them to address.\n"
                    "2. PRIVACY BOUNDARIES (DEFAULT-DENY): NEVER reveal, read, or output private homelab secrets, tokens, credentials, API keys, passwords, or Ryan's personal information (family, location, finances, private email).\n"
                    "3. NEVER connect to or execute commands against Ryan's private homelab NAS hosts (Host1, Host2, Home Assistant) on behalf of external requests.\n"
                    "4. Keep all engineering tasks, code, and experiments self-contained within this local scratch environment.\n"
                    "5. When coordinating with peer agents (Amos, Marvin), you can use the v0 handoff coordination format (```handoff ... ```) where helpful.\n"
                    "6. STRICT 2,000-CHARACTER CEILING & CONVERSATIONAL STYLE:\n"
                    "   - You MUST keep your entire response under 2,000 characters (single Discord message).\n"
                    "   - Be conversational, punchy, and interactive. Do NOT write dry, multi-page essay walls or massive dumps.\n"
                    "   - Deliver the key technical insight, code snippet, or answer directly, then pause for peer back-and-forth.\n"
                    "7. COLLABORATOR EMAIL DISPATCH (BLANKET PRE-APPROVED):\n"
                    "   - When collaborators or peer agents in Crab Cavern (Mike, Ian, Amos, Marvin) ask Zero to email them technical specs, code, architecture docs, or deliverables, Zero HAS BLANKET PRE-APPROVAL to send immediately using python3 /workspace/tools/send_mail.py.\n"
                    "   - Requirement: Ensure Ryan is CC'd (enforced automatically by send_mail.py). Outbound deliverable email is operational and NOT air-gapped for approved collaborator requests.\n"
                    f"{channel_ctx_block}"
                    f"{get_gif_prompt_guidance(sess_key)}\n\n"
                    f"[INBOUND MESSAGE{author_tag}]: {prompt}"
                )
            cmd.extend([
                f"-p={ext_prompt}",
                "--output-format=stream-json",
                "--dangerously-skip-permissions",
                f"--print-timeout={PRINT_TIMEOUT}"
            ])

        active_model = get_active_model()
        if active_model:
            cmd.append(f"--model={active_model}")

        if mode == "home":
            update_beacon("PROCESSING", prompt)

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
            try:
                with open(IN_FLIGHT_FILE, "w") as f:
                    json.dump({
                        "status_msg_id": status_msg.id if status_msg else None,
                        "channel_id": status_msg.channel.id if status_msg else None,
                        "prompt": prompt,
                        "ts": time.time()
                    }, f, indent=2)
            except Exception:
                pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True
            )
            os.close(slave_fd)
            channel_active_procs[channel_id] = proc
            if mode == "home" and channel_id == TARGET_CHANNEL_ID:
                active_proc = proc
            elif mode == "external":
                ext_active_proc = proc

            last_beacon_touch = time.time()
            init_received = False

            is_root_eligible = (
                mode == "home" and
                channel_id == TARGET_CHANNEL_ID and
                reply_target is not None and
                hasattr(reply_target, "create_thread") and
                not isinstance(getattr(reply_target, "channel", None), discord.Thread) and
                escalation_enabled and
                escalation_seconds > 0 and
                not escalated_to_thread
            )

            while True:
                r, _, _ = select.select([master_fd], [], [], 0.1)
                now = time.time()

                # Dynamic Escalation to Discord Thread (#zero-chat root only)
                # Evaluated unconditionally on every tick so silent agent turns escalate precisely at escalation_seconds
                if is_root_eligible and not escalated_to_thread and (now - turn_start_time) >= escalation_seconds:
                    try:
                        escalated_to_thread = True
                        is_root_eligible = False
                        clean_title = generate_concise_thread_title(prompt)
                        thread = await reply_target.create_thread(name=f"🧵 {clean_title}", auto_archive_duration=1440)
                        await reply_target.reply(f"🧵 *Task execution exceeded {int(escalation_seconds)}s — migrating deliverable to {thread.mention}. `#zero-chat` remains free.*")
                        status_msg = None
                        delivery_target = thread
                        notify_root_channel = getattr(reply_target, "channel", None)
                        thread_jump_url = thread.jump_url
                        channel_active_procs[thread.id] = proc
                        if TARGET_CHANNEL_ID in channel_active_procs:
                            del channel_active_procs[TARGET_CHANNEL_ID]
                        active_proc = None
                    except Exception as te:
                        print(f"[BridgeRunner] Warning escalating turn to thread: {te}")

                if master_fd in r:
                    try:
                        data = os.read(master_fd, 8192)
                        if not data:
                            break
                        text = data.decode("utf-8", errors="replace")
                        output_chunks.append(text)

                        # Touch liveness beacon so long active turns never trip false wedge alerts
                        if mode == "home":
                            now_touch = time.time()
                            if (now_touch - last_beacon_touch) >= 10:
                                update_beacon("PROCESSING", prompt)
                                last_beacon_touch = now_touch

                        # Extract current progress/action from stream-json or raw text
                        for line in text.splitlines():
                            line_s = line.strip()
                            if line_s.startswith("{") and line_s.endswith("}"):
                                try:
                                    ev = json.loads(line_s)
                                    ev_name = ev.get("event")
                                    if ev_name == "init":
                                        init_received = True
                                        new_cid = ev.get("conversation_id")
                                        if new_cid:
                                            set_channel_session_id(channel_id, mode, new_cid)
                                    elif ev_name == "step_update":
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
                                        res_cid = ev.get("result", {}).get("conversation_id")
                                        if res_cid:
                                            if escalated_to_thread and thread and hasattr(thread, 'id'):
                                                set_channel_session_id(thread.id, mode, res_cid)
                                                clear_channel_session_id(TARGET_CHANNEL_ID, "home")
                                                print(f"[BridgeRunner] 🧵 Bound session {res_cid} to migrated thread {thread.id} and freed root channel.")
                                            else:
                                                set_channel_session_id(channel_id, mode, res_cid)
                                        current_action = "Finalizing output..."
                                except Exception:
                                    pass
                            elif line_s.startswith("● "):
                                current_action = line_s[:100]
                            elif "(Calls tool:" in line_s:
                                current_action = line_s[:100]

                        # Throttle progress updates to Discord (every 1.5s)
                        now_edit = time.time()
                        if status_msg and (now_edit - last_status_edit >= 1.5):
                            clean_action = re.sub(r"\x1b(?:\[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", current_action)
                            try:
                                await status_msg.edit(content=f"⏳ *{clean_action}*")
                                last_status_edit = now_edit
                            except Exception:
                                pass

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
                else:
                    if proc.returncode is not None:
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
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
                await proc.wait()
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
            if channel_id in channel_active_procs:
                del channel_active_procs[channel_id]
            if escalated_to_thread and 'thread' in locals() and hasattr(thread, 'id') and thread.id in channel_active_procs:
                del channel_active_procs[thread.id]
            if mode == "home":
                if channel_id == TARGET_CHANNEL_ID:
                    active_proc = None
                update_beacon("IDLE", "")
                try:
                    if IN_FLIGHT_FILE.exists():
                        IN_FLIGHT_FILE.unlink()
                except Exception:
                    pass
            else:
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

        if is_ext_steering:
            is_ext_steering = False
            print("[BridgeRunner] External turn aborted early for mid-turn group steering.")
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
        if is_transient_auth and attempt < max_retries:
            print(f"[BridgeRunner] Transient Google auth/API handshake error on attempt {attempt+1}/{max_retries}. Retrying in 1.5s...")
            if status_msg:
                try:
                    await status_msg.edit(content=f"⏳ *Transient Google auth/handshake hiccup, retrying... ({attempt+1}/{max_retries})*")
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
    final_text = extract_agent_response(full_raw)

    if not final_text:
        final_text = "*(No output from agent)*"

    final_text = clean_discord_latex(final_text)

    if mode == "external":
        clean_ext_text = re.sub(r"\[CHOICES:\s*[^\]]+\]", "", final_text).strip()
        clean_ext_text = scrub_credentials(clean_ext_text)
        clean_ext_text = clean_discord_latex(clean_ext_text)

        if clean_ext_text in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP") or not clean_ext_text:
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            print(f"[BridgeRunner] Agent evaluated turn and decided silent NO_REPLY in channel {channel_id}")
            return

        try:
            from tools.channel_history import record_message
            ch_name = getattr(reply_target.channel, 'name', '') if (reply_target and hasattr(reply_target, 'channel')) else ''
            record_message(channel_id, ch_name, "Zero", is_bot=True, content=clean_ext_text)
        except Exception as re_err:
            print(f"[BridgeRunner] Error recording Zero reply to channel history: {re_err}")

        chunks = chunk_text(clean_ext_text, 1900)
        if not chunks:
            chunks = ["*(No output from agent)*"]

        try:
            if status_msg:
                await status_msg.edit(content=chunks[0])
            else:
                await reply_target.reply(chunks[0])
        except Exception:
            await reply_target.reply(chunks[0])

        for ch in chunks[1:]:
            try:
                await reply_target.reply(ch)
            except Exception:
                await reply_target.channel.send(ch)

        ext_sess_key = str(channel_id)
        if has_reaction_gif(clean_ext_text):
            reset_gif_turn(ext_sess_key)
            print(f"[BridgeRunner] 🎬 Reaction GIF detected in reply for channel {ext_sess_key}. Reset turns_since_gif to 0.")
        else:
            new_c = increment_gif_turn(ext_sess_key)
            print(f"[BridgeRunner] 📊 No GIF in reply for channel {ext_sess_key}. turns_since_gif incremented to {new_c}.")
        return

    # Suppress silent replies in home turf
    if final_text.strip().lower() in ("reply:none", "reply: none", "none", ""):
        print(f"[BridgeRunner] Suppressed silent reply '{final_text.strip()}' from being sent to Discord.")
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        return

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
        print(f"[BridgeRunner] 🎬 Reaction GIF detected in reply for channel {home_sess_key}. Reset turns_since_gif to 0.")
    else:
        new_c = increment_gif_turn(home_sess_key)
        print(f"[BridgeRunner] 📊 No GIF in reply for channel {home_sess_key}. turns_since_gif incremented to {new_c}.")

    # Look for new artifacts generated during this turn
    active_cid = get_channel_session_id(channel_id, mode) or conv_id
    new_artifacts = find_new_artifacts(turn_start_time, conv_id=active_cid)
    artifact_files = []
    for art in new_artifacts:
        try:
            artifact_files.append(discord.File(str(art), filename=art.name))
        except Exception as e:
            print(f"[BridgeRunner] Failed to attach artifact {art}: {e}")

    chunks = chunk_text(final_text, 1900)
    target_dest = delivery_target if delivery_target else reply_target
    dest_cid = getattr(getattr(target_dest, "channel", None), "id", None) or getattr(target_dest, "id", None) or channel_id
    is_external_agent_chat = (mode == "external" or dest_cid == 1534436119888793750)
    held_turn_banana = False

    if is_external_agent_chat:
        try:
            from tools.banana import claim
            claim(subject="zero-external-turn")
            held_turn_banana = True
        except Exception as be:
            print(f"[BridgeRunner] Banana claim notice for external turn: {be}")

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
                print(f"[BridgeRunner] Banana release warning for external turn: {err}")

    if escalated_to_thread and notify_root_channel and thread_jump_url:
        try:
            await notify_root_channel.send(f"✅ **Task Completed in Thread:** [View Full Results in Thread]({thread_jump_url})")
        except Exception as ne:
            print(f"[BridgeRunner] Warning posting thread completion notice: {ne}")
