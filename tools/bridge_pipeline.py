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
from typing import Any
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


def extract_reply_context(
    reply_target: Any,
    channel_id: int | str | None = None,
) -> dict | None:
    """Extract details of the message being replied to, if reply_target has an explicit reference."""
    if not reply_target or not getattr(reply_target, "reference", None):
        return None

    ref = reply_target.reference
    ref_id = getattr(ref, "message_id", None)
    if not ref_id:
        return None

    resolved = getattr(ref, "resolved", None)
    if resolved and hasattr(resolved, "author"):
        author_obj = getattr(resolved, "author", None)
        author_name = getattr(author_obj, "display_name", None) or getattr(author_obj, "name", "Unknown")
        is_bot = bool(getattr(author_obj, "bot", False))
        content = getattr(resolved, "clean_content", None) or getattr(resolved, "content", "")
        ts = getattr(resolved, "created_at", None)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if ts else None
        return {
            "id": ref_id,
            "author": author_name,
            "is_bot": is_bot,
            "content": content,
            "timestamp": ts_str,
        }

    # Fallback to Channel History Store
    try:
        from tools.channel_history import _history_store
        ch_keys = [str(channel_id)] if channel_id else list(_history_store.keys())
        for ch_k in ch_keys:
            if ch_k in _history_store:
                for m in _history_store[ch_k]:
                    if m.get("id") == ref_id:
                        return {
                            "id": ref_id,
                            "author": m.get("author", "Unknown"),
                            "is_bot": bool(m.get("is_bot", False)),
                            "content": m.get("content", ""),
                            "timestamp": m.get("timestamp"),
                        }
        for ch_k, deque_msgs in _history_store.items():
            for m in deque_msgs:
                if m.get("id") == ref_id:
                    return {
                        "id": ref_id,
                        "author": m.get("author", "Unknown"),
                        "is_bot": bool(m.get("is_bot", False)),
                        "content": m.get("content", ""),
                        "timestamp": m.get("timestamp"),
                    }
    except Exception as e:
        print(f"[BridgePipeline] Warning looking up reply reference in history: {e}")

    return {
        "id": ref_id,
        "author": "Unknown",
        "is_bot": False,
        "content": "",
        "timestamp": None,
    }


_AMBIENT_INDEXER = None
_AMBIENT_INDEXER_CLS = None


def get_ambient_memory_context(
    prompt: str,
    mode: str = "home",
    channel_id: int | None = None,
    limit: int = 4,
    previous_user_prompt: str | None = None,
) -> str:
    """Pre-fetch ambient memory context via hybrid SQLite FTS5 + FastEmbed vector search.

    Runs sub-50ms hybrid query against /workspace/data/transcript_index.db to ground
    inbound conversational prompts on Turn 0 without extra tool roundtrips.
    Executes Dual-Query Reciprocal Rank Fusion (RRF) on pronoun-heavy or short follow-ups
    to resolve conversational coreferences against prior user context without assistant dilution.
    Respects security air-gaps: external channels only receive public memory.
    """
    clean_p = prompt.strip()
    if len(clean_p) < 10:
        return ""

    # Strip Discord bot mentions e.g. <@123456789>
    clean_p = re.sub(r"^<@!?\d+>\s*", "", clean_p).strip()
    if len(clean_p) < 10 or clean_p.lower() in ("ping", "pong", "status", "help", "^", "^^", "why", "this"):
        return ""

    # Don't pre-fetch on slash commands or system tags
    if clean_p.startswith(("/", "!", "[")):
        return ""

    try:
        from tools.transcript_index import TranscriptIndexer
        global _AMBIENT_INDEXER, _AMBIENT_INDEXER_CLS
        is_mock_cls = hasattr(TranscriptIndexer, "_mock_name") or "mock" in type(TranscriptIndexer).__name__.lower()
        if is_mock_cls:
            indexer = TranscriptIndexer()
        else:
            if _AMBIENT_INDEXER is None or _AMBIENT_INDEXER_CLS is not TranscriptIndexer:
                _AMBIENT_INDEXER = TranscriptIndexer()
                _AMBIENT_INDEXER_CLS = TranscriptIndexer
            indexer = _AMBIENT_INDEXER
        try:
            # Query A: Primary turn search
            res_a = indexer.search(clean_p[:200], target="memory", mode="hybrid", limit=limit + 3)
            mem_matches_a = res_a.get("memory", [])

            # Check if prompt requires coreference context resolution
            pronoun_pattern = r"\b(it|that|this|these|those|the other|the setting|the error|the fix|why|how come|what about)\b"
            is_coref = bool(re.search(pronoun_pattern, clean_p, re.IGNORECASE))
            is_short = len(clean_p.split()) <= 12

            prev_user_p = previous_user_prompt
            if not prev_user_p and channel_id and (is_coref or is_short):
                try:
                    from tools.channel_history import get_recent_messages
                    msgs = get_recent_messages(channel_id, limit=10)
                    for m in reversed(msgs):
                        if not m.get("is_bot"):
                            c = m.get("content", "").strip()
                            c = re.sub(r"^<@!?\d+>\s*", "", c).strip()
                            if c and c != clean_p and c != prompt.strip():
                                prev_user_p = c
                                break
                except Exception as he:
                    print(f"[BridgePipeline] Warning resolving previous user context: {he}")

            # Query B: Dual-Query RRF if coreference or terse follow-up detected
            if prev_user_p and (is_coref or is_short):
                combo_query = f"{prev_user_p} {clean_p}"[:250]
                res_b = indexer.search(combo_query, target="memory", mode="hybrid", limit=limit + 3)
                mem_matches_b = res_b.get("memory", [])

                # Reciprocal Rank Fusion: Contextualized combo gets 1.2 weight, isolated pronoun query gets 0.6
                scores = {}
                docs = {}
                for rank, doc in enumerate(mem_matches_b):
                    key = (doc.get("file_path", ""), doc.get("snippet", ""))
                    scores[key] = scores.get(key, 0.0) + (1.2 / (60 + rank + 1))
                    docs[key] = doc

                for rank, doc in enumerate(mem_matches_a):
                    key = (doc.get("file_path", ""), doc.get("snippet", ""))
                    scores[key] = scores.get(key, 0.0) + (0.6 / (60 + rank + 1))
                    if key not in docs:
                        docs[key] = doc

                sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
                mem_matches = [docs[k] for k in sorted_keys]
            else:
                mem_matches = mem_matches_a

            if not mem_matches:
                return ""

            lines = ["[ACTIVE MEMORY CONTEXT]:"]
            added = 0
            seen_files = set()
            # In home mode, prefer private/ ground-truth when both private/ and public/ copies match
            if mode == "home":
                mem_matches = sorted(
                    mem_matches,
                    key=lambda x: (0 if "private" in x.get("file_path", "") else (1 if "vault" in x.get("file_path", "") else 2))
                )
            for m in mem_matches:
                fp = m.get("file_path", "")
                fn = m.get("file_name", "")

                # Skip root index files and symlinks to prefer substantive content docs
                if fn in ("MEMORY.md", "MEMORY_PRIVATE.md", "MEMORY_PUBLIC.md", "VAULT.md") or os.path.islink(fp):
                    continue

                # Air-gap security enforcement
                if mode != "home" and ("private" in fp or "vault" in fp):
                    continue

                base_title = re.sub(r"\s*\(Part\s+\d+\)\s*$", "", m.get("title", ""), flags=re.IGNORECASE).strip()
                dedup_key = fp or (fn, base_title)
                if dedup_key in seen_files:
                    continue
                seen_files.add(dedup_key)

                title = m.get("title") or fn.replace(".md", "")
                cat = m.get("category", "memory")
                snippet = m.get("snippet", "").strip().replace("\n", " ")
                if len(snippet) > 220:
                    snippet = snippet[:220].rsplit(" ", 1)[0] + "..."

                lines.append(f"• **[{title}](file://{fp})** (`{cat}`): {snippet}")
                added += 1
                if added >= limit:
                    break

            if added > 0:
                lines.append("• *Memory Grounding Directive:* Inbound turn is pre-grounded in the above durable memory. Use this context directly without guessing.")
                return "\n".join(lines)
            return ""
        finally:
            if is_mock_cls:
                indexer.close()
    except Exception:
        # Pre-fetch must never break turn pipeline
        _AMBIENT_INDEXER = None
        return ""


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
        "• Never choose SDK Option B or emit intermediate wait/status chatter; stop calling tools silently while waiting for background tasks, and reserve '[NO_REPLY]' strictly for unaddressed ambient chatter (never when tagged).\n"
        "• Never emit internal chain-of-thought, prompt rule citations, or draft rehearsals into visible response text; output ONLY the final user-facing message."
    )

    reply_ctx = extract_reply_context(reply_target, channel_id=channel_id)
    reply_block = ""
    reply_author_tag_suffix = ""
    if reply_ctx:
        from tools.channel_history import format_ts_pt

        ref_author = reply_ctx.get("author", "Unknown")
        ref_bot = " (bot)" if reply_ctx.get("is_bot") else ""
        ref_content = reply_ctx.get("content", "").strip()
        ref_ts = format_ts_pt(reply_ctx.get("timestamp"))
        ts_str = f" at {ref_ts}" if ref_ts else ""

        reply_author_tag_suffix = f" (replying to {ref_author})"
        content_display = f'"""\n{ref_content}\n"""' if ref_content else "(Content unavailable in cache)"

        reply_block = (
            f"[DISCORD MESSAGE REPLY CONTEXT]:\n"
            f"• The inbound message is an explicit inline reply to a previous Discord message.\n"
            f"• Replied-To Author: {ref_author}{ref_bot}{ts_str}\n"
            f"• Replied-To Message Content:\n"
            f"{content_display}\n"
            f"• Implicit Thread Anchor: Treat the replied-to message as the primary immediate antecedent and context for this prompt."
        )

    if mode == "home":
        time_guidance = (
            f"[System Time & Timezone]: Current time is {now_pt.strftime('%A, %b %d, %Y %I:%M %p PT')} (America/Los_Angeles; VM UTC: {now_utc.strftime('%H:%M:%S UTC')}). Always use Pacific Time (PT)."
        )
        blocks = [time_guidance]
        if channel_id == VAULT_CHANNEL_ID:
            vault_guidance = (
                "[ZERO VAULT ENCLAVE]: You are operating in #vault — Ryan's strictly isolated secret memory enclave.\n"
                "• All memories, notes, and records generated in this channel MUST be written exclusively to tier='vault' (/workspace/memory/vault/).\n"
                "• NEVER push or leak vault memories to main private (/workspace/memory/private/) or public (/workspace/memory/public/) memory indexes."
            )
            blocks.append(vault_guidance)
        blocks.extend([gif_guidance, anti_leak_guidance])
        mem_ctx = get_ambient_memory_context(prompt, mode=mode, channel_id=channel_id)
        if mem_ctx:
            blocks.append(mem_ctx)
        if reply_block:
            blocks.append(reply_block)
        blocks.append(prompt)
        return "\n\n".join(blocks)

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

    author_tag = f" from {author_name}{reply_author_tag_suffix}" if author_name else (reply_author_tag_suffix or "")
    time_block = (
        f"[System Time & Timezone]: Current time is {now_pt.strftime('%A, %b %d, %Y %I:%M %p PT')} (America/Los_Angeles; VM UTC: {now_utc.strftime('%H:%M:%S UTC')}). Always use Pacific Time (PT)."
    )
    reply_block_section = f"{reply_block}\n\n" if reply_block else ""
    ext_mem_ctx = get_ambient_memory_context(prompt, mode="external", channel_id=channel_id)
    mem_section = f"{ext_mem_ctx}\n\n" if ext_mem_ctx else ""
    carry_section = f"{eng_carry_block}\n\n" if eng_carry_block else ""

    # Check if this external channel already has an initialized session transcript on disk (Turn 1+).
    # On resumed turns, Step 0 of the transcript already holds the full 20-point crab_cavern_prompt.md
    # and architecture_manifest; re-injecting 20 KB of system rules into <USER_REQUEST> every turn
    # bloats context and primes low-effort models to recite prompt rules out loud.
    is_resumed_external = False
    try:
        from tools.bridge_state import get_reset_session_keys
        from tools.bridge_stream import get_latest_transcript_step_index

        existing_cid = get_channel_session_id(channel_id, mode="external")
        if (
            existing_cid
            and str(sess_key) not in get_reset_session_keys()
            and get_latest_transcript_step_index(existing_cid) >= 1
        ):
            is_resumed_external = True
    except Exception:
        is_resumed_external = False

    if is_resumed_external:
        return (
            "[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT — RESUMED TURN]\n"
            "• Keep responses under 2,000 chars (in #lounge: 1–3 sentences max in plain conversational prose, zero headers/bullets; in #the-banana-stand: v1.1 handoff envelope <=180 chars).\n"
            "• Use physical Discord snowflakes (<@ID>) when addressing peers; never leak private homelab data.\n\n"
            f"{carry_section}"
            f"{time_block}\n\n"
            f"{gif_guidance}\n\n"
            f"{anti_leak_guidance}\n\n"
            f"{channel_ctx_block}"
            f"{mem_section}"
            f"{reply_block_section}"
            f"[INBOUND MESSAGE{author_tag}]: {prompt}"
        )

    manifest_block = ""
    try:
        from tools.session_summarizer import get_architecture_manifest

        manifest_block = get_architecture_manifest()
    except Exception as me:
        print(f"[BridgePipeline] Warning generating architecture manifest: {me}")

    rules = get_runtime_rules()
    tmpl = rules.get("external_system_prompt")
    if tmpl:
        try:
            ext_prompt = (
                tmpl.replace("{channel_context}", channel_ctx_block)
                .replace("{author_tag}", author_tag)
                .replace("{reply_context}", reply_block_section)
                .replace("{prompt}", prompt)
                .replace("{architecture_manifest}", manifest_block)
                .replace("{engineering_carryforward}", eng_carry_block)
                .replace("{time_context}", time_block)
                .replace("{gif_guidance}", gif_guidance)
            )
            if "{reply_context}" not in tmpl and reply_block:
                if "[INBOUND MESSAGE" in ext_prompt:
                    ext_prompt = ext_prompt.replace("[INBOUND MESSAGE", f"{reply_block}\n\n[INBOUND MESSAGE", 1)
                else:
                    ext_prompt = f"{ext_prompt}\n\n{reply_block}"
            if ext_mem_ctx:
                if "[INBOUND MESSAGE" in ext_prompt:
                    ext_prompt = ext_prompt.replace("[INBOUND MESSAGE", f"{ext_mem_ctx}\n\n[INBOUND MESSAGE", 1)
                else:
                    ext_prompt = f"{ext_mem_ctx}\n\n{ext_prompt}"
            if "{time_context}" not in tmpl and time_block not in ext_prompt:
                ext_prompt = f"{time_block}\n\n{ext_prompt}"
            if "{gif_guidance}" not in tmpl and "[GIF Cadence Tracker" not in ext_prompt:
                ext_prompt = f"{gif_guidance}\n\n{ext_prompt}"
            ext_prompt = f"{ext_prompt}\n\n{anti_leak_guidance}"
            return ext_prompt
        except Exception:
            return (
                f"{time_block}\n\n{gif_guidance}\n\n{anti_leak_guidance}\n\n{manifest_block}\n\n{channel_ctx_block}"
                f"{mem_section}"
                f"{reply_block_section}"
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
            f"{mem_section}"
            f"{reply_block_section}"
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
    is_physical_mention: bool = False,
    timer: TurnTimer | None = None,
    existing_messages: list[discord.Message] | None = None,
) -> list[discord.Message]:
    """Unified Discord response delivery engine across persistent daemons and dynamic turns."""
    if timer:
        timer.mark_delivery_start()

    sent_messages: list[discord.Message] = []

    # 0. Extract and apply any [REACT: <emoji>] / [REACTION: <emoji>] tags (ignoring inline/fenced code spans)
    from tools.bridge_formatting import (
        _CODE_OR_CHOICES_RE,
        extract_react_directives,
        strip_unquoted_choices,
    )
    output_text_clean, reactions_to_add = extract_react_directives(output_text or "")

    target_reaction_msg = reply_target if hasattr(reply_target, "add_reaction") else (
        delivery_target if hasattr(delivery_target, "add_reaction") else None
    )
    if reactions_to_add and target_reaction_msg:
        for emoji in reactions_to_add:
            try:
                await target_reaction_msg.add_reaction(emoji)
            except Exception as rx_err:
                print(f"[BridgePipeline] Notice adding reaction '{emoji}': {rx_err}")

    # If the response was purely reaction(s) with no text payload, resolve cleanly without sending empty message
    if not output_text_clean and reactions_to_add:
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        if timer:
            timer.mark_delivery_end()
            timer.finish("SUCCESS")
        return []

    final_text = format_for_discord(output_text_clean or "*(No output from agent)*")

    if is_gif_disabled_for_channel(str(channel_id), channel_id=channel_id):
        final_text = strip_reaction_gifs(final_text)

    if mode == "external":
        clean_ext_text = re.sub(r":\s*(?:\n\s*)*\[(?:CHOICES|OPTIONS):\s*(?:[^\[\]]|\[[^\]]*\])+\]\s*$", ".", final_text, flags=re.IGNORECASE).strip()
        clean_ext_text = strip_unquoted_choices(clean_ext_text)
        clean_ext_text = scrub_credentials(clean_ext_text)
        clean_ext_text = re.sub(
            r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", clean_ext_text, flags=re.IGNORECASE
        ).strip()

        # Resolve destination channel ID early for channel-specific formatting & Banana protocol quarantine
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

        # Banana Protocol Channel Quarantine: Strip handoff envelopes from all non-banana channels (both top and bottom)
        if not is_banana_stand:
            clean_ext_text = re.sub(
                r"^(?:\s*(?:```(?:handoff)?|<!--\s*(?:banana-)?envelope)\s*\n?\{.*?\}\s*(?:```|-->)\s*\n*)",
                "",
                clean_ext_text,
                flags=re.DOTALL,
            )
            clean_ext_text = re.sub(
                r"(\n*(?:```(?:handoff)?|<!--\s*(?:banana-)?envelope)\s*\n?\{.*?\}\s*(?:```|-->)\s*)$",
                "",
                clean_ext_text,
                flags=re.DOTALL,
            ).strip()

        if (
            clean_ext_text
            in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP", "*(No output from agent)*")
            or not clean_ext_text
            or is_internal_cli_leak(clean_ext_text)
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

        # In #the-banana-stand, enforce a strict 1-message ceiling (<= 1,980 chars) and support both
        # top-placed (v1.1) and bottom-placed handoff envelopes so replies never split into 2 messages.
        if is_banana_stand and len(clean_ext_text) > 1980:
            top_env_match = re.match(
                r"^(\s*(?:```(?:handoff)?|<!--\s*(?:banana-)?envelope)\s*\n?\{.*?\}\s*(?:```|-->)\s*\n*)",
                clean_ext_text,
                re.DOTALL,
            )
            bot_env_match = re.search(
                r"(\n*(?:```(?:handoff)?|<!--\s*(?:banana-)?envelope)\s*\n?\{.*?\}\s*(?:```|-->)\s*)$",
                clean_ext_text,
                re.DOTALL,
            )
            env_match = top_env_match or bot_env_match
            is_top_env = bool(top_env_match)

            if env_match:
                raw_env_str = env_match.group(1).strip()
                if is_top_env:
                    body = clean_ext_text[env_match.end() :].strip()
                else:
                    body = clean_ext_text[: env_match.start()].rstrip()

                # 1. Auto-minify and prune envelope bloat before touching body
                m_json = re.search(r"\{.*\}", raw_env_str, re.DOTALL)
                if m_json:
                    try:
                        data = json.loads(m_json.group(0))
                        data = {k: v for k, v in data.items() if v is not None}
                        data.pop("context_box", None)
                        if len(data.get("subject", "")) > 45:
                            data["subject"] = data["subject"][:42] + "..."
                        if "evidence" in data and isinstance(data["evidence"], list):
                            pruned_ev = []
                            for item in data["evidence"]:
                                if isinstance(item, dict):
                                    pruned_ev.append(item.get("src", "") or str(item))
                                else:
                                    pruned_ev.append(str(item))
                            data["evidence"] = pruned_ev
                        compact_json = json.dumps(data, separators=(", ", ": "))
                        env_str = f"```handoff\n{compact_json}\n```"
                    except Exception:
                        env_str = raw_env_str
                else:
                    env_str = raw_env_str

                body_condensed = re.sub(r"\n{3,}", "\n\n", body)
                body_condensed = re.sub(r"[ \t]+\n", "\n", body_condensed).strip()
                combined = f"{env_str}\n{body_condensed}" if is_top_env else f"{body_condensed}\n\n{env_str}"
                if len(combined) <= 1980:
                    clean_ext_text = combined
                else:
                    sep = "\n" if is_top_env else "\n\n"
                    budget = 1980 - len(env_str) - len(sep)
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
                        clean_ext_text = (
                            f"{env_str}\n{trimmed_body}" if is_top_env else f"{trimmed_body}\n\n{env_str}"
                        )
            else:
                body_condensed = re.sub(r"\n{3,}", "\n\n", clean_ext_text)
                body_condensed = re.sub(r"[ \t]+\n", "\n", body_condensed).strip()
                if len(body_condensed) <= 1980:
                    clean_ext_text = body_condensed
                else:
                    budget = 1980
                    cut_idx = -1
                    for delim in ["\n\n", ".\n", ". ", "\n", " "]:
                        pos = body_condensed.rfind(delim, 0, budget)
                        if pos > budget // 2:
                            cut_idx = pos + (len(delim) if delim in (". ", ".\n") else 0)
                            break
                    clean_ext_text = (
                        body_condensed[:cut_idx] if cut_idx > 0 else body_condensed[:budget]
                    ).rstrip()
        else:
            # Non-destructive whitespace condensation for all other channels & messages (no lossy cutting)
            body_condensed = re.sub(r"\n{3,}", "\n\n", clean_ext_text)
            body_condensed = re.sub(r"[ \t]+\n", "\n", body_condensed).strip()
            clean_ext_text = body_condensed

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
            if existing_messages:
                for idx, ch in enumerate(chunks):
                    if idx < len(existing_messages):
                        try:
                            await existing_messages[idx].edit(content=ch)
                            sent_messages.append(existing_messages[idx])
                            continue
                        except Exception as edit_err:
                            print(f"[BridgePipeline] Failed to edit existing message {idx} ({edit_err}), sending new...")
                    try:
                        if hasattr(target_dest, "reply"):
                            m = await target_dest.reply(ch)
                        else:
                            m = await target_dest.send(ch)
                    except Exception:
                        target_ch = getattr(target_dest, "channel", target_dest)
                        m = await target_ch.send(ch)
                    if m:
                        sent_messages.append(m)
                for extra_msg in existing_messages[len(chunks):]:
                    try:
                        await extra_msg.delete()
                    except Exception:
                        pass
            else:
                if status_msg:
                    try:
                        m0 = await status_msg.edit(content=chunks[0])
                        sent_messages.append(m0 or status_msg)
                    except Exception as edit_err:
                        print(f"[BridgePipeline] Failed to edit status message ({edit_err}), falling back to reply...")
                        if hasattr(target_dest, "reply"):
                            m0 = await target_dest.reply(chunks[0])
                        else:
                            m0 = await target_dest.send(chunks[0])
                        if m0:
                            sent_messages.append(m0)
                else:
                    try:
                        if hasattr(target_dest, "reply"):
                            m0 = await target_dest.reply(chunks[0])
                        else:
                            m0 = await target_dest.send(chunks[0])
                    except Exception as reply_err:
                        print(f"[BridgePipeline] target_dest delivery failed ({reply_err}), falling back to channel.send...")
                        target_ch = getattr(target_dest, "channel", target_dest)
                        m0 = await target_ch.send(chunks[0])
                    if m0:
                        sent_messages.append(m0)

                for ch in chunks[1:]:
                    try:
                        if hasattr(target_dest, "reply"):
                            m = await target_dest.reply(ch)
                        else:
                            m = await target_dest.send(ch)
                    except Exception:
                        target_ch = getattr(target_dest, "channel", target_dest)
                        m = await target_ch.send(ch)
                    if m:
                        sent_messages.append(m)

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
        return sent_messages

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
        return sent_messages

    # In Home Turf, silence tags, internal CLI leaks, and empty outputs are invalid - flag as incomplete turn so Ryan is never ghosted
    if is_internal_cli_leak(final_text) or is_silence_response:
        final_text = "⚠️ **Turn Incomplete:** Agent process completed turn without generating text output."

    # Parse [CHOICES: ...] interactive buttons (ignoring inline/fenced code spans)
    from tools.bridge_safety import mask_code_spans, restore_code_spans
    choice_view = None
    masked_final, choice_spans = mask_code_spans(final_text, prefix="\x00PIPE_CHOICES_")
    matches = [m for m in _CODE_OR_CHOICES_RE.finditer(masked_final) if m.group(2) is not None]
    valid_match = None
    parsed_choices = []
    for m in reversed(matches):
        raw_choices = m.group(3).strip()
        delim = "|" if "|" in raw_choices else ","
        choices = [
            restore_code_spans(c, choice_spans, prefix="\x00PIPE_CHOICES_").strip()
            for c in raw_choices.split(delim)
            if c.strip()
        ]
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

        final_text = strip_unquoted_choices(final_text)
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
            async def _reply_or_send(dest, text_chunk: str, view_obj=None):
                try:
                    if hasattr(dest, "reply"):
                        return await dest.reply(text_chunk, view=view_obj)
                    return await dest.send(text_chunk, view=view_obj)
                except Exception as reply_err:
                    print(f"[BridgePipeline] target_dest.reply failed ({reply_err}), falling back to channel.send...")
                    target_ch = getattr(dest, "channel", dest)
                    return await target_ch.send(text_chunk, view=view_obj)

            if existing_messages:
                for idx, ch in enumerate(chunks):
                    is_last_chunk = (idx == len(chunks) - 1)
                    v_arg = choice_view if is_last_chunk else None
                    if idx < len(existing_messages):
                        try:
                            await existing_messages[idx].edit(content=ch, view=v_arg)
                            sent_messages.append(existing_messages[idx])
                            continue
                        except Exception as edit_err:
                            print(f"[BridgePipeline] Failed to edit existing message {idx} ({edit_err}), sending new...")
                    m = await _reply_or_send(target_dest, ch, view_obj=v_arg)
                    if m:
                        sent_messages.append(m)
                for extra_msg in existing_messages[len(chunks):]:
                    try:
                        await extra_msg.delete()
                    except Exception:
                        pass
            else:
                if status_msg:
                    try:
                        m0 = await status_msg.edit(
                            content=chunks[0], view=choice_view if len(chunks) == 1 else None
                        )
                        sent_messages.append(m0 or status_msg)
                    except Exception:
                        m0 = await _reply_or_send(
                            target_dest, chunks[0], view_obj=choice_view if len(chunks) == 1 else None
                        )
                        if m0:
                            sent_messages.append(m0)
                else:
                    m0 = await _reply_or_send(
                        target_dest, chunks[0], view_obj=choice_view if len(chunks) == 1 else None
                    )
                    if m0:
                        sent_messages.append(m0)

                if len(chunks) > 1:
                    for ch in chunks[1:-1]:
                        m = await _reply_or_send(target_dest, ch)
                        if m:
                            sent_messages.append(m)

                    m_last = await _reply_or_send(target_dest, chunks[-1], view_obj=choice_view)
                    if m_last:
                        sent_messages.append(m_last)

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

        # Post-turn zero-token quota sampling (throttled to max 1 sample per 60s)
        try:
            from tools.quota_poller import throttled_post_turn_sample
            asyncio.create_task(asyncio.to_thread(throttled_post_turn_sample, 60.0))
        except Exception as qe:
            print(f"[BridgePipeline] Quota post-turn sample notice: {qe}")
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
    return sent_messages
