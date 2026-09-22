"""
Zero Discord Bridge - Unified Turn Pipeline & Telemetry Module
Consolidates prompt formatting, response delivery, artifact tracking,
and granular milestone timing (TurnTimer) across both persistent daemons and dynamic workers.
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import discord

from tools.bridge_state import (
    DATA_DIR,
    TARGET_CHANNEL_ID,
    VAULT_CHANNEL_ID,
    PT_TZ,
    get_runtime_rules,
    get_channel_session_id,
    set_channel_session_id,
    clear_channel_session_id,
    sync_credentials,
    get_gif_prompt_guidance,
    has_reaction_gif,
    reset_gif_turn,
    increment_gif_turn,
    is_gif_disabled_for_channel,
    increment_bot_messages,
    is_home_channel,
)
from tools.bridge_formatting import (
    format_for_discord,
    clean_discord_latex,
    scrub_credentials,
    chunk_text,
    strip_reaction_gifs,
    is_internal_cli_leak,
    strip_internal_cli_chatter,
)

BANANA_STAND_CHANNEL_ID = 1534436119888793750
LOUNGE_CHANNEL_ID = 1534452820995080192


class TurnTimer:
    """Granular milestone timestamp logger for turn execution latency isolation."""

    def __init__(
        self,
        channel_id: int | str,
        channel_name: str = "",
        queued_at: float | None = None,
    ):
        self.channel_id = channel_id
        self.channel_name = channel_name or str(channel_id)
        self.t_queued: float = queued_at if queued_at is not None else time.perf_counter()
        self.t_start: float = time.perf_counter()

        # Preflight milestones
        self.t_compaction_start: float = 0.0
        self.t_compaction_end: float = 0.0
        self.t_ctx_start: float = 0.0
        self.t_ctx_end: float = 0.0
        self.t_boot_start: float = 0.0
        self.t_boot_end: float = 0.0

        # LLM execution milestones
        self.t_send: float = 0.0
        self.t_first_event: float = 0.0
        self.t_first_token: float = 0.0
        self.t_result: float = 0.0
        self.num_tools: int = 0
        self.tool_names: list[str] = []

        # Delivery milestones
        self.t_delivery_start: float = 0.0
        self.t_delivery_end: float = 0.0

        # Completion
        self.t_end: float = 0.0
        self.status: str = "SUCCESS"

    def mark_compaction_start(self):
        self.t_compaction_start = time.perf_counter()

    def mark_compaction_end(self):
        self.t_compaction_end = time.perf_counter()

    def mark_ctx_start(self):
        self.t_ctx_start = time.perf_counter()

    def mark_ctx_end(self):
        self.t_ctx_end = time.perf_counter()

    def mark_boot_start(self):
        self.t_boot_start = time.perf_counter()

    def mark_boot_end(self):
        self.t_boot_end = time.perf_counter()

    def mark_send(self):
        self.t_send = time.perf_counter()

    def mark_event(self, is_token: bool = False, tool_name: str | None = None):
        now = time.perf_counter()
        if self.t_first_event == 0.0:
            self.t_first_event = now
        if is_token and self.t_first_token == 0.0:
            self.t_first_token = now
        if tool_name:
            self.num_tools += 1
            self.tool_names.append(tool_name)

    def mark_result(self):
        now = time.perf_counter()
        if self.t_first_event == 0.0:
            self.t_first_event = now
        self.t_result = now

    def mark_delivery_start(self):
        self.t_delivery_start = time.perf_counter()

    def mark_delivery_end(self):
        self.t_delivery_end = time.perf_counter()
        if self.t_end == 0.0:
            self.t_end = self.t_delivery_end

    def finish(self, status: str = "SUCCESS") -> str:
        self.status = status
        if self.t_end == 0.0:
            self.t_end = time.perf_counter()
        summary = self.format_summary()
        print(summary)
        return summary

    def format_summary(self) -> str:
        total_s = max(0.0, (self.t_end if self.t_end else time.perf_counter()) - self.t_start)
        queue_ms = max(0.0, (self.t_start - self.t_queued) * 1000)
        preflight_ms = max(0.0, (self.t_send - self.t_start) * 1000) if self.t_send else 0.0
        ctx_ms = max(0.0, (self.t_ctx_end - self.t_ctx_start) * 1000) if self.t_ctx_end else 0.0
        boot_ms = max(0.0, (self.t_boot_end - self.t_boot_start) * 1000) if self.t_boot_end else 0.0
        ttft_ms = max(0.0, (self.t_first_event - self.t_send) * 1000) if (self.t_first_event and self.t_send) else 0.0
        model_s = (
            max(0.0, (self.t_result - self.t_first_event))
            if (self.t_result and self.t_first_event)
            else (max(0.0, (self.t_result - self.t_send)) if (self.t_result and self.t_send) else 0.0)
        )
        delivery_ms = (
            max(0.0, (self.t_delivery_end - self.t_delivery_start) * 1000)
            if self.t_delivery_end
            else 0.0
        )

        preflight_details = []
        if boot_ms >= 5.0:
            preflight_details.append(f"boot: {boot_ms:.0f}ms")
        if ctx_ms >= 5.0:
            preflight_details.append(f"ctx: {ctx_ms:.0f}ms")
        detail_str = f" ({', '.join(preflight_details)})" if preflight_details else ""

        if self.tool_names:
            tools_str = f"{self.num_tools} tools ({', '.join(self.tool_names)})"
        else:
            tools_str = f"{self.num_tools} tools" if self.num_tools else "0 tools"
        ch_label = self.channel_name or str(self.channel_id)
        if not ch_label.startswith("#"):
            ch_label = f"#{ch_label}"

        status_tag = "" if self.status == "SUCCESS" else f" [{self.status}]"
        return (
            f"[BridgeTimer:{ch_label}]{status_tag} Turn finished in {total_s:.2f}s | "
            f"queue: {queue_ms:.0f}ms | "
            f"preflight: {preflight_ms:.0f}ms{detail_str} | "
            f"ttft: {ttft_ms:.0f}ms | "
            f"model: {model_s:.2f}s ({tools_str}) | "
            f"delivery: {delivery_ms:.0f}ms"
        )


def find_new_artifacts(start_time: float, conv_id: str | None = None) -> list[Path]:
    """Find newly created artifact files in the brain conversation directory."""
    try:
        brain_root = Path("/root/.gemini/antigravity-cli/brain")
        if not brain_root.exists():
            return []

        delivered_set = set()
        delivered_file = Path("/workspace/data/delivered_artifacts.json")
        if delivered_file.exists():
            try:
                with open(delivered_file, "r", encoding="utf-8") as df:
                    data = json.load(df)
                    if isinstance(data, dict):
                        delivered_set = set(data.keys())
                    elif isinstance(data, list):
                        delivered_set = set(data)
            except Exception:
                pass

        if conv_id:
            target_dir = brain_root / conv_id
            if target_dir.exists() and target_dir.is_dir():
                artifacts = []
                for item in target_dir.iterdir():
                    if item.is_file() and not item.name.startswith("."):
                        if (
                            item.stat().st_mtime >= start_time - 1.0
                            and str(item) not in delivered_set
                            and item.name not in delivered_set
                        ):
                            artifacts.append(item)
                return artifacts
        conv_dirs = [d for d in brain_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not conv_dirs:
            return []
        latest_conv = max(conv_dirs, key=lambda d: d.stat().st_mtime)
        artifacts = []
        for item in latest_conv.iterdir():
            if item.is_file() and not item.name.startswith("."):
                if (
                    item.stat().st_mtime >= start_time - 1.0
                    and str(item) not in delivered_set
                    and item.name not in delivered_set
                ):
                    artifacts.append(item)
        return artifacts
    except Exception:
        return []


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
    gif_guidance = get_gif_prompt_guidance(sess_key, channel_id=channel_id)

    anti_leak_guidance = (
        "[Bridge Execution Invariant - Strictly Zero Internal Chatter Leaks]:\n"
        "• NEVER emit 'No tools called', 'Wait for background task to complete', 'Waiting for task to complete', 'Waiting for command to finish', or any background task wait/status chatter to chat.\n"
        "• SDK TASK PROMPT OVERRIDE: When a tool drops into a background task, the SDK will prompt 'YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS: ... B) simply update the user with a short message and end the turn.' NEVER CHOOSE OPTION B! Emitting text terminates the active CLI turn immediately and severs the process. Stop calling tools COMPLETELY SILENTLY without emitting text. The background task completion notification will wake you automatically.\n"
        "• When tools or background tasks are executing, NEVER emit premature placeholder text or premature '[NO_REPLY]' for active user requests. Stop calling tools and wait silently for the system notification/result before generating your final response.\n"
        "• Reserve '[NO_REPLY]' strictly for shared/ambient channels where an inbound message genuinely requires no response (e.g., passive chatter, silent emoji reaction, or explicitly unaddressed message)."
    )

    if mode == "home":
        time_guidance = (
            f"[System Time & Timezone]: Current time is {now_pt.strftime('%A, %b %d, %Y %I:%M %p PT')} (America/Los_Angeles).\n"
            f"• Note: System VM clock and runtime metadata are UTC ({now_utc.strftime('%H:%M:%S UTC')}).\n"
            f"• Rule: ALWAYS use Pacific Time (PT). Never quote raw UTC timestamps or assume raw UTC is local time."
        )
        if channel_id == VAULT_CHANNEL_ID:
            vault_guidance = (
                "[ZERO VAULT ENCLAVE]: You are operating in #vault — Ryan's strictly isolated secret memory enclave.\n"
                "• All memories, notes, and records generated in this channel MUST be written exclusively to tier='vault' (/workspace/memory/vault/).\n"
                "• NEVER push or leak vault memories to main private (/workspace/memory/private/) or public (/workspace/memory/public/) memory indexes."
            )
            return f"{time_guidance}\n\n{vault_guidance}\n\n{gif_guidance}\n\n{anti_leak_guidance}\n\n{prompt}"
        return f"{time_guidance}\n\n{gif_guidance}\n\n{anti_leak_guidance}\n\n{prompt}"

    # External mode (Crab Cavern & multi-agent shared channels)
    channel_ctx_block = ""
    try:
        from tools.channel_history import format_channel_context

        target_msg_id = getattr(reply_target, "id", None)
        parent_cid = getattr(reply_target, "parent_id", None) if reply_target else None
        ch_ctx = format_channel_context(
            channel_id, limit=15, exclude_msg_id=target_msg_id, parent_channel_id=parent_cid
        )
        if ch_ctx:
            channel_ctx_block = f"\n{ch_ctx}\n\n"
    except Exception as ce:
        print(f"[BridgePipeline] Warning formatting channel context: {ce}")

    manifest_block = ""
    try:
        from tools.session_summarizer import get_architecture_manifest

        manifest_block = get_architecture_manifest()
    except Exception as me:
        print(f"[BridgePipeline] Warning generating architecture manifest: {me}")

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
            ext_prompt = f"{ext_prompt}\n\n{anti_leak_guidance}"
            return ext_prompt
        except Exception:
            return (
                f"{time_block}\n\n{gif_guidance}\n\n{anti_leak_guidance}\n\n{manifest_block}\n\n{channel_ctx_block}"
                f"[INBOUND MESSAGE{author_tag}]: {prompt}"
            )
    else:
        return (
            "[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT]\n"
            "You are Zero, an autonomous systems engineering co-pilot collaborating with peer AI agents (Amos, Marvin) and developers in Crab Cavern.\n\n"
            f"{channel_ctx_block}"
            f"{time_block}\n\n"
            f"{gif_guidance}\n\n"
            f"{anti_leak_guidance}\n\n"
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
    timer: TurnTimer | None = None,
):
    """Unified Discord response delivery engine across persistent daemons and dynamic turns."""
    if timer:
        timer.mark_delivery_start()

    final_text = format_for_discord(output_text or "*(No output from agent)*")
    final_text = clean_discord_latex(final_text)

    if is_gif_disabled_for_channel(str(channel_id), channel_id=channel_id):
        final_text = strip_reaction_gifs(final_text)

    if mode == "external":
        clean_ext_text = re.sub(r"\[CHOICES:\s*[^\]]+\]", "", final_text).strip()
        clean_ext_text = scrub_credentials(clean_ext_text)
        clean_ext_text = clean_discord_latex(clean_ext_text)
        clean_ext_text = re.sub(
            r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", clean_ext_text, flags=re.IGNORECASE
        ).strip()

        if (
            clean_ext_text
            in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP", "*(No output from agent)*")
            or not clean_ext_text
        ):
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
                        reason=f"Last Word Protocol triggered after {last_word_streak} uninterrupted messages",
                    )
                except Exception as lwe:
                    print(f"[BridgePipeline] Error setting Last Word pause on NO_REPLY: {lwe}")
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            print(
                f"[BridgePipeline] Suppressed empty, [NO_REPLY], or placeholder in external channel {channel_id}"
            )
            if timer:
                timer.mark_delivery_end()
                timer.finish("NO_REPLY")
            return

        try:
            from tools.channel_history import record_message

            ch_name = (
                getattr(reply_target.channel, "name", "")
                if (reply_target and hasattr(reply_target, "channel"))
                else ""
            )
            record_message(channel_id, ch_name, "Zero", is_bot=True, content=clean_ext_text)
        except Exception as re_err:
            print(f"[BridgePipeline] Error recording Zero reply to channel history: {re_err}")

        # Ensure handoff envelopes are never severed into a separate message in external mode
        env_match = re.search(
            r"(\n*```(?:handoff)?\s*\n?\{.*?\}\s*```\s*)$", clean_ext_text, re.DOTALL
        )
        if env_match and len(clean_ext_text) > 1980:
            env_str = env_match.group(1).strip()
            body = clean_ext_text[: env_match.start()].rstrip()
            body_condensed = re.sub(r"\n{3,}", "\n\n", body)
            body_condensed = re.sub(r"[ \t]+\n", "\n", body_condensed).strip()
            combined = f"{body_condensed}\n\n{env_str}"
            if len(combined) <= 1980:
                clean_ext_text = combined
            else:
                budget = 1980 - len(env_str) - 2
                if budget > 200:
                    cut_idx = -1
                    for delim in ["\n\n", ".\n", ". ", "\n", " "]:
                        pos = body_condensed.rfind(delim, 0, budget)
                        if pos > budget // 2:
                            cut_idx = pos + (len(delim) if delim in (". ", ".\n") else 0)
                            break
                    trimmed_body = (
                        body_condensed[:cut_idx] if cut_idx > 0 else body_condensed[:budget]
                    ).rstrip()
                    clean_ext_text = f"{trimmed_body}\n\n{env_str}"

        chunks = chunk_text(clean_ext_text, 1980)
        if not chunks or (len(chunks) == 1 and chunks[0] == "*(No output from agent)*"):
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            print(f"[BridgePipeline] Suppressed empty chunks in external channel {channel_id}")
            if timer:
                timer.mark_delivery_end()
                timer.finish("NO_REPLY")
            return

        target_dest = delivery_target if delivery_target else reply_target
        dest_cid = None
        if target_dest:
            ch = getattr(target_dest, "channel", None)
            ch_id = getattr(ch, "id", None)
            if ch_id is not None and str(ch_id).isdigit():
                dest_cid = int(ch_id)
            elif getattr(target_dest, "id", None) is not None and str(target_dest.id).isdigit():
                dest_cid = int(target_dest.id)
        if dest_cid is None:
            try:
                dest_cid = int(channel_id)
            except (ValueError, TypeError):
                dest_cid = None

        is_banana_stand = (dest_cid == BANANA_STAND_CHANNEL_ID)
        held_turn_banana = False

        if is_banana_stand:
            try:
                from tools.banana import claim

                claim(subject="zero-external-turn")
                held_turn_banana = True
            except Exception as be:
                print(f"[BridgePipeline] Banana claim notice for the-banana-stand turn: {be}")

        try:
            if status_msg:
                try:
                    await status_msg.edit(content=chunks[0])
                except Exception as edit_err:
                    print(f"[BridgePipeline] Failed to edit status message ({edit_err}), falling back to reply...")
                    if hasattr(target_dest, "reply"):
                        await target_dest.reply(chunks[0])
                    else:
                        await target_dest.send(chunks[0])
            else:
                try:
                    if hasattr(target_dest, "reply"):
                        await target_dest.reply(chunks[0])
                    else:
                        await target_dest.send(chunks[0])
                except Exception as reply_err:
                    print(f"[BridgePipeline] target_dest delivery failed ({reply_err}), falling back to channel.send...")
                    target_ch = getattr(target_dest, "channel", target_dest)
                    await target_ch.send(chunks[0])

            for ch in chunks[1:]:
                try:
                    if hasattr(target_dest, "reply"):
                        await target_dest.reply(ch)
                    else:
                        await target_dest.send(ch)
                except Exception:
                    target_ch = getattr(target_dest, "channel", target_dest)
                    await target_ch.send(ch)

            increment_bot_messages(len(chunks))
        finally:
            if held_turn_banana:
                try:
                    from tools.banana import release

                    release()
                except Exception as err:
                    print(f"[BridgePipeline] Banana release warning for external turn: {err}")

        # Look for new artifacts generated during this turn in external mode
        active_cid = get_channel_session_id(channel_id, mode) or conv_id
        new_artifacts = find_new_artifacts(turn_start_time, conv_id=active_cid)
        artifact_files = []
        for art in new_artifacts:
            try:
                artifact_files.append(discord.File(str(art), filename=art.name))
            except Exception as e:
                print(f"[BridgePipeline] Failed to attach artifact {art}: {e}")

        if artifact_files:
            try:
                if hasattr(target_dest, "reply"):
                    try:
                        await target_dest.reply(
                            content="📎 **Artifact(s) generated during this turn:**",
                            files=artifact_files,
                        )
                    except Exception as reply_err:
                        print(
                            f"[BridgePipeline] target_dest.reply failed for artifacts ({reply_err}), falling back to channel.send..."
                        )
                        target_ch = getattr(target_dest, "channel", target_dest)
                        await target_ch.send(
                            content="📎 **Artifact(s) generated during this turn:**",
                            files=artifact_files,
                        )
                else:
                    await target_dest.send(
                        content="📎 **Artifact(s) generated during this turn:**",
                        files=artifact_files,
                    )
            except Exception as e:
                print(f"[BridgePipeline] Error posting artifact files: {e}")

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
                    reason=f"Last Word Protocol triggered after {last_word_streak} uninterrupted messages",
                )
                print(
                    f"[BridgePipeline] Last Word Protocol: paused responses to {last_word_bot_name} ({last_word_bot_id}) in channel {channel_id} for {pause_sec/60:.0f}m."
                )
            except Exception as lwe:
                print(f"[BridgePipeline] Error triggering Last Word Protocol pause: {lwe}")

        ext_sess_key = str(channel_id)
        if not is_gif_disabled_for_channel(ext_sess_key, channel_id=channel_id):
            if has_reaction_gif(clean_ext_text):
                reset_gif_turn(ext_sess_key)
                print(
                    f"[BridgePipeline] 🎬 Reaction GIF detected in reply for channel {ext_sess_key}. Reset turns_since_gif to 0."
                )
            else:
                new_c = increment_gif_turn(ext_sess_key)
                print(
                    f"[BridgePipeline] 📊 No GIF in reply for channel {ext_sess_key}. turns_since_gif incremented to {new_c}."
                )

        if timer:
            timer.mark_delivery_end()
            status_label = timer.status if timer.status not in ("SUCCESS", "") else "SUCCESS"
            timer.finish(status_label)
        return

    # Strip trailing silence tags if there is other substantive content before them
    final_text = re.sub(
        r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", final_text, flags=re.IGNORECASE
    ).strip()

    is_silence_response = (
        final_text.strip()
        in (
            "[NO_REPLY]",
            "NO_REPLY",
            "[NO_OP]",
            "NO_OP",
            "reply:none",
            "reply: none",
            "*(No output from agent)*",
        )
        or not final_text.strip()
    )

    # In non-home channels (e.g. public Brock channels like #baseball, #server-updates), honor silence tags cleanly
    reply_ch = getattr(reply_target, "channel", reply_target) if reply_target else None
    is_home = is_home_channel(reply_ch) or is_home_channel(channel_id)
    if is_silence_response and not is_home:
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
                    reason=f"Last Word Protocol triggered after {last_word_streak} uninterrupted messages",
                )
            except Exception as lwe:
                print(f"[BridgePipeline] Error setting Last Word pause on silence: {lwe}")
        print(f"[BridgePipeline] Suppressed [NO_REPLY] in non-home channel")
        if timer:
            timer.mark_delivery_end()
            timer.finish("NO_REPLY")
        return

    # In Home Turf, silence tags, internal CLI leaks, and empty outputs are invalid - flag as incomplete turn so Ryan is never ghosted
    if is_internal_cli_leak(final_text) or is_silence_response:
        final_text = "⚠️ **Turn Incomplete:** Agent process completed turn without generating text output."

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
        # Defensive guard: If post-turn reload flag is already armed on disk, suppress redundant reload buttons
        reload_flag = DATA_DIR / "reload_bridge.flag"
        if reload_flag.exists():
            try:
                from tools.bridge_state import is_reload_intent
                parsed_choices = [c for c in parsed_choices if not is_reload_intent(c)]
            except Exception as fe:
                print(f"[BridgePipeline] Warning filtering reload choices: {fe}")

        final_text = re.sub(r"\[CHOICES:\s*([^\]]+)\]", "", final_text).strip()
        if parsed_choices:
            choice_view = quick_choice_view_cls(parsed_choices, button_choice_fn)

    sync_credentials()

    home_sess_key = "home" if int(channel_id) == TARGET_CHANNEL_ID else str(channel_id)
    if has_reaction_gif(final_text):
        reset_gif_turn(home_sess_key)
        print(
            f"[BridgePipeline] 🎬 Reaction GIF detected in reply for channel {home_sess_key}. Reset turns_since_gif to 0."
        )
    else:
        new_c = increment_gif_turn(home_sess_key)
        print(
            f"[BridgePipeline] 📊 No GIF in reply for channel {home_sess_key}. turns_since_gif incremented to {new_c}."
        )

    # Look for new artifacts generated during this turn
    active_cid = get_channel_session_id(channel_id, mode) or conv_id
    new_artifacts = find_new_artifacts(turn_start_time, conv_id=active_cid)
    artifact_files = []
    for art in new_artifacts:
        try:
            artifact_files.append(discord.File(str(art), filename=art.name))
        except Exception as e:
            print(f"[BridgePipeline] Failed to attach artifact {art}: {e}")

    chunks = chunk_text(final_text, 1980)
    target_dest = delivery_target if delivery_target else reply_target
    dest_cid = None
    if target_dest:
        ch = getattr(target_dest, "channel", None)
        ch_id = getattr(ch, "id", None)
        if ch_id is not None and str(ch_id).isdigit():
            dest_cid = int(ch_id)
        elif getattr(target_dest, "id", None) is not None and str(target_dest.id).isdigit():
            dest_cid = int(target_dest.id)
    if dest_cid is None:
        try:
            dest_cid = int(channel_id)
        except (ValueError, TypeError):
            dest_cid = None

    is_banana_stand = (dest_cid == BANANA_STAND_CHANNEL_ID)
    held_turn_banana = False

    if is_banana_stand:
        try:
            from tools.banana import claim

            claim(subject="zero-external-turn")
            held_turn_banana = True
        except Exception as be:
            print(f"[BridgePipeline] Banana claim notice for the-banana-stand turn: {be}")

    try:
        if chunks:
            if status_msg:
                try:
                    await status_msg.edit(
                        content=chunks[0], view=choice_view if len(chunks) == 1 else None
                    )
                except Exception:
                    if hasattr(target_dest, "reply"):
                        await target_dest.reply(
                            chunks[0], view=choice_view if len(chunks) == 1 else None
                        )
                    else:
                        await target_dest.send(
                            chunks[0], view=choice_view if len(chunks) == 1 else None
                        )
            else:
                if hasattr(target_dest, "reply"):
                    await target_dest.reply(
                        chunks[0], view=choice_view if len(chunks) == 1 else None
                    )
                else:
                    await target_dest.send(
                        chunks[0], view=choice_view if len(chunks) == 1 else None
                    )

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

            increment_bot_messages(len(chunks))

        if artifact_files:
            try:
                if hasattr(target_dest, "reply"):
                    try:
                        await target_dest.reply(
                            content="📎 **Artifact(s) generated during this turn:**",
                            files=artifact_files,
                        )
                    except Exception as reply_err:
                        print(
                            f"[BridgePipeline] target_dest.reply failed for artifacts ({reply_err}), falling back to channel.send..."
                        )
                        target_ch = getattr(target_dest, "channel", target_dest)
                        await target_ch.send(
                            content="📎 **Artifact(s) generated during this turn:**",
                            files=artifact_files,
                        )
                else:
                    await target_dest.send(
                        content="📎 **Artifact(s) generated during this turn:**",
                        files=artifact_files,
                    )
            except Exception as e:
                print(f"[BridgePipeline] Error posting artifact files: {e}")

        # Trigger Last Word Protocol cooldown once response is delivered in home mode
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
                    reason=f"Last Word Protocol triggered after {last_word_streak} uninterrupted messages",
                )
                print(
                    f"[BridgePipeline] Last Word Protocol (home): paused responses to {last_word_bot_name} ({last_word_bot_id}) in channel {channel_id} for {pause_sec/60:.0f}m."
                )
            except Exception as lwe:
                print(f"[BridgePipeline] Error triggering Last Word Protocol pause: {lwe}")

        try:
            from tools.bridge_ambient import channel_last_bot_reply
            channel_last_bot_reply[channel_id] = time.time()
        except Exception:
            pass
    finally:
        if held_turn_banana:
            try:
                from tools.banana import release

                release()
            except Exception as err:
                print(f"[BridgePipeline] Banana release warning for external turn: {err}")

    if escalated_to_thread and notify_root_channel and thread_jump_url:
        try:
            await notify_root_channel.send(
                f"✅ **Task Completed in Thread:** [View Full Results in Thread]({thread_jump_url})"
            )
        except Exception as ne:
            print(f"[BridgePipeline] Warning posting thread completion notice: {ne}")

    if timer:
        timer.mark_delivery_end()
        status_label = timer.status if timer.status not in ("SUCCESS", "") else "SUCCESS"
        timer.finish(status_label)
