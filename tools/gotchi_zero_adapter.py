#!/usr/bin/env python3
"""
gotchi_zero_adapter.py — Unified Zero Brain, Artifact Handoff & Transcript Adapter for GOTCHI
=============================================================================================
Unifies the GOTCHI hardware companion (`brockventures/gotchi`) and `#zero-voice`
(`tools/bridge_voice.py`) into a single, low-latency Zero experience:

1. Shared Persona, Memory & Tool Parity with `#zero-voice`:
   - Reuses `_load_startup_context_snapshot()`, `_build_live_tools()`, and tool execution
     from `tools/bridge_voice.py`, plus sub-2ms in-process SQLite FTS5 (`memory_fts`) pre-fetching.
   - Supports single-shot camera snapshots (`image_bytes` / `image_b64`) and 5-frame bursts
     (`burst_images` / `burst_images_b64`) natively via multimodal `types.Part.from_bytes`.
   - Optimizes spoken output for GOTCHI's `MAX98357A` speaker and 200x200 e-paper display:
     crisp 1-to-3 sentence spoken answers in Zero's `Schedar` voice, stripped of raw markdown
     or URL clutter (`clean_spoken_text_for_tts`).

2. Guaranteed Hyperlink & Visual Artifact Handoff to `#zero-chat`:
   - Explicit Tooling (`post_note_to_zero_chat`): Supports posting clickable links, code blocks,
     local images/CAD files (`file_path` via `deliver_file`), or live webpage screenshots
     (`screenshot_url` via `capture_screenshot.py`) directly to `#zero-chat` (`1542081375287640084`).
   - Deterministic Safety Net (`extract_and_forward_artifacts_to_zero_chat`): Even if the LLM
     includes URLs (`https://...`), fenced code blocks, or local artifact paths in its reply
     without calling `post_note_to_zero_chat`, the adapter automatically forwards them to
     `#zero-chat` before `clean_spoken_text_for_tts` strips them from the spoken audio, and
     ensures Gotchi verbally tells Ryan to check `#zero-chat`.

3. Speed & Latency Optimizations:
   - `ThinkingConfig(thinking_budget=0)` by default on `gemini-2.5-flash` (matches `#zero-voice`,
     saving 1.2s–3.5s of thinking delay on conversational turns; auto-elevates to `1024` on deep queries).
   - Persistent `genai.Client` singleton keeps HTTP/2 TLS connections warm across turns.
   - Sub-2ms in-process SQLite FTS5 (`memory_fts`) recall instead of spawning a subprocess.
   - Early `is_stt_ghost` check aborts `<400ms` accidental PTT bumps before invoking Gemini.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from fastapi import Request
    from fastapi.responses import JSONResponse
except ImportError:
    Request = Any  # type: ignore
    JSONResponse = Any  # type: ignore

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
if "/workspace/gotchi" not in sys.path:
    sys.path.insert(0, "/workspace/gotchi")

from tools.bridge_state import TARGET_CHANNEL_ID
from tools.bridge_voice import (
    DEFAULT_VOICE_NAME,
    GOTCHI_CHAT_CHANNEL_ID,
    _append_voice_turn_to_history,
    _build_live_tools,
    _load_startup_context_snapshot,
)
from tools import outbox

log = logging.getLogger("gotchi_zero_adapter")
PT_TZ = ZoneInfo("America/Los_Angeles")

DEFAULT_GOTCHI_MODEL = os.environ.get("GOTCHI_ZERO_MODEL", "gemini-3.8-flash")
ATTACHMENTS_DIR = Path("/workspace/data/attachments")
TRANSCRIPT_INDEX_DB = Path("/workspace/data/transcript_index.db")

_URL_RE = re.compile(r"https?://[^\s)>\]\"'`]+", re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]+?```")
_LOCAL_ARTIFACT_RE = re.compile(
    r"(/workspace/[^\s)>\]\"'`]+\.(?:png|jpg|jpeg|webp|gif|stl|3mf|step|pdf|svg))",
    re.IGNORECASE,
)
_ALLOWED_VOICE_READ_PREFIXES = ("/workspace/", "/baseball/")
_DENIED_VOICE_READ_BASENAMES = {
    ".env",
    ".ssh",
    "id_rsa",
    "id_ed25519",
    "authorized_keys",
    "known_hosts",
    "credentials",
    "credentials.json",
    "token.json",
    "secrets.json",
    "pii_rules.json",
    "env.json",
}
_DENIED_VOICE_READ_SEGMENTS = {
    ".env",
    ".ssh",
    "secrets",
    "private",
    "vault",
}
_SPOKEN_CHAT_CUES = ("zero-chat", "zero chat", "discord", "dropped it in", "sent it to", "posted")

GOTCHI_THINKING_TELEMETRY: Dict[str, int] = {
    "LOW": 0,
    "MEDIUM": 0,
    "HIGH": 0,
    "request_deep_thinking_calls": 0,
    "tool_error_escalations": 0,
    "step2_escalations": 0,
    "burst_escalations": 0,
}


def record_thinking_telemetry(level: str, reason: str = "default_low") -> None:
    """Tracks and logs thinking tier selection and escalations to verify real-world firing rate."""
    norm_level = (level or "LOW").upper()
    if norm_level in GOTCHI_THINKING_TELEMETRY:
        GOTCHI_THINKING_TELEMETRY[norm_level] += 1
    if reason in GOTCHI_THINKING_TELEMETRY:
        GOTCHI_THINKING_TELEMETRY[reason] += 1
    if norm_level != "LOW":
        log.info(
            f"[GotchiZero] Thinking escalation -> {norm_level} (reason={reason}, telemetry={GOTCHI_THINKING_TELEMETRY})"
        )


def is_allowed_voice_read_path(
    file_path: str,
    allowed_prefixes: Tuple[str, ...] = _ALLOWED_VOICE_READ_PREFIXES,
) -> Tuple[bool, str]:
    """
    Validates `read_local_file` paths on voice turns:
    1. Resolves symlinks and `..` traversal via `os.path.realpath()` BEFORE checking prefixes.
    2. Checks that the resolved path is strictly inside an allowed root (`/workspace/` or `/baseball/`).
    3. Denies `.env`, `.env.*`, `.ssh`, `secrets`, `credentials`, `token`, and SSH keys by both
       basename and path component so secrets are never read aloud over TTS.
    """
    raw = (file_path or "").strip()
    if not raw:
        return False, "Empty file_path"

    resolved = os.path.realpath(raw)
    normalized_prefixes = tuple(
        os.path.realpath(p).rstrip("/") + "/" for p in allowed_prefixes
    )
    if not any(resolved.startswith(pref) for pref in normalized_prefixes):
        return False, f"Resolved path '{resolved}' is outside allowed voice read roots"

    # Check both raw and resolved path components & basenames for sensitive files/dirs
    for candidate in (raw, resolved):
        parts = [seg.lower() for seg in Path(candidate).parts if seg and seg != "/"]
        if not parts:
            continue
        basename = parts[-1]
        if (
            basename in _DENIED_VOICE_READ_BASENAMES
            or basename.startswith(".env.")
            or basename.endswith(".pem")
            or basename.endswith(".key")
        ):
            return False, f"Denied sensitive basename '{basename}' on voice turn"
        for seg in parts:
            if seg in _DENIED_VOICE_READ_SEGMENTS or seg.startswith(".env."):
                return False, f"Denied sensitive path segment '{seg}' on voice turn"

    return True, resolved


def stage_voice_mutation_confirmation(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Gates every `run_shell_command` and `write_local_file` on a voice turn behind a text
    confirmation card in `#zero-chat` (`1542081375287640084`) so a misheard Whisper word
    or unauthenticated speaker never executes shell commands or mutates files directly.
    """
    if tool_name == "run_shell_command":
        cmd = str(args.get("command", "")).strip()
        summary = f"📟 🔒 **[Gotchi Voice Security Gate — Confirm Shell Command]:**\n- **Tool:** `run_shell_command`\n- **Command:** `{cmd[:400]}`\nReply in `#zero-chat` to approve execution."
    else:
        fpath = str(args.get("file_path", "")).strip()
        content = str(args.get("content", ""))
        preview = content[:240].replace("`", "'")
        summary = (
            f"📟 🔒 **[Gotchi Voice Security Gate — Confirm File Write]:**\n"
            f"- **Tool:** `write_local_file`\n"
            f"- **Path:** `{fpath}` ({len(content)} chars)\n"
            f"- **Preview:** `{preview}`\n"
            "Reply in `#zero-chat` to approve writing this file."
        )

    outbox.queue_outbox_message(
        str(TARGET_CHANNEL_ID),
        summary,
        source_turn="gotchi-voice-security-gate",
        dedupe_window=5,
    )
    return {
        "confirmation_required": True,
        "status": "staged_in_zero_chat",
        "channel_id": TARGET_CHANNEL_ID,
        "tool": tool_name,
        "message": (
            f"Voice security gate: staged `{tool_name}` in #zero-chat for text confirmation "
            "instead of executing directly from Whisper audio."
        ),
    }


def clean_spoken_text_for_tts(text: str) -> str:
    """
    Cleans LLM markdown output for natural spoken TTS synthesis on Gotchi (`Schedar` voice)
    while preserving sentence punctuation for `split_into_sentences`.
    """
    if not text:
        return ""
    cleaned = text.strip()
    # Replace markdown links [label](<url>) or [label](url) with just the spoken label
    cleaned = re.sub(r"\[([^\]]+)\]\(<?https?://[^)\s>]+>?\)", r"\1", cleaned)
    # Remove bare URLs in angle brackets or plain text so TTS doesn't read 'https colon slash slash'
    cleaned = re.sub(r"<https?://[^>]+>", "", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    # Strip fenced code blocks from spoken TTS audio
    cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)
    # Strip markdown headers, bold/italic markers, and inline code backticks
    cleaned = re.sub(r"(?m)^#{1,6}\s*", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    # Collapse bullets into natural sentence pauses
    cleaned = re.sub(r"(?m)^\s*[-*•]\s+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def select_thinking_level(
    prompt: str = "",
    default_level: str = "LOW",
    *,
    step: int = 0,
    tool_had_error: bool = False,
    burst_count: int = 0,
    model_requested_deep_thinking: bool = False,
) -> str:
    """
    3-Tier Thinking Escalation Policy for `gemini-3.8-flash`:
    - Tier 1 (`LOW` — Default on Step 0 AND Step 1): Keeps single-tool lookups and normal voice
      turns at `LOW` end-to-end (~300ms synthesis).
    - Tier 2 (`MEDIUM` — Data-Driven): Escalates to `MEDIUM` only when a tool returns an error
      (`tool_had_error=True`), when multi-tool chaining reaches Step 2+ (`step >= 2`), or when
      inspecting a 5-frame camera burst (`burst_count > 1`).
    - Tier 3 (`HIGH` — Model-Directed): Escalates to `HIGH` when the Step 0 model explicitly
      invokes `request_deep_thinking(spoken_preamble, reason)`.
    """
    _ = prompt  # Whisper transcripts do not use brittle keyword lists to escalate thinking
    if model_requested_deep_thinking:
        record_thinking_telemetry("HIGH", "request_deep_thinking_calls")
        return "HIGH"
    if tool_had_error:
        record_thinking_telemetry("MEDIUM", "tool_error_escalations")
        return "MEDIUM"
    if step >= 2:
        record_thinking_telemetry("MEDIUM", "step2_escalations")
        return "MEDIUM"
    if burst_count > 1:
        record_thinking_telemetry("MEDIUM", "burst_escalations")
        return "MEDIUM"
    record_thinking_telemetry(default_level, "default_low")
    return default_level


def select_thinking_budget(
    prompt: str = "",
    default_budget: int = 0,
    *,
    step: int = 0,
    tool_had_error: bool = False,
    burst_count: int = 0,
    model_requested_deep_thinking: bool = False,
) -> int:
    """
    3-Tier Thinking Budget equivalent for `gemini-2.5-flash`:
    - Tier 1 (`LOW`): `0` tokens on Step 0 and Step 1.
    - Tier 2 (`MEDIUM`): `512` tokens on tool error, Step 2+, or 5-frame camera burst.
    - Tier 3 (`HIGH`): `1024` tokens when `request_deep_thinking` is invoked.
    """
    _ = prompt
    if model_requested_deep_thinking:
        return max(default_budget, 1024)
    if tool_had_error or step >= 2 or burst_count > 1:
        return max(default_budget, 512)
    return default_budget


def build_thinking_config(
    model: str,
    prompt: str = "",
    default_budget: int = 0,
    *,
    step: int = 0,
    tool_had_error: bool = False,
    burst_count: int = 0,
    model_requested_deep_thinking: bool = False,
) -> Any:
    """Builds the 3-tier `types.ThinkingConfig` for either Gemini 3.8 or 2.5 Flash."""
    from google.genai import types

    m_lower = (model or "").lower()
    if "3.8" in m_lower or "3.7" in m_lower or "3.6" in m_lower:
        level = select_thinking_level(
            prompt,
            "LOW",
            step=step,
            tool_had_error=tool_had_error,
            burst_count=burst_count,
            model_requested_deep_thinking=model_requested_deep_thinking,
        )
        return types.ThinkingConfig(thinking_level=level)
    budget = select_thinking_budget(
        prompt,
        default_budget,
        step=step,
        tool_had_error=tool_had_error,
        burst_count=burst_count,
        model_requested_deep_thinking=model_requested_deep_thinking,
    )
    return types.ThinkingConfig(thinking_budget=budget)


def _build_gotchi_tools() -> list[Any]:
    """
    Extends `_build_live_tools()` with the `request_deep_thinking(spoken_preamble, reason)`
    tool so the Step 0 `LOW` model can immediately stream a spoken preamble to Gotchi's speaker
    while escalating Step 1 to `HIGH` thinking.
    """
    from google.genai import types

    base_tools = _build_live_tools()
    f_decls = []
    for t in base_tools:
        if getattr(t, "function_declarations", None):
            f_decls.extend(list(t.function_declarations))
    f_decls.append(
        types.FunctionDeclaration(
            name="request_deep_thinking",
            description=(
                "Escalate from LOW thinking to HIGH thinking for complex math, multi-constraint "
                "architecture tradeoffs, or deep root-cause debugging. Streams `spoken_preamble` "
                "immediately to Gotchi's speaker while Step 1 runs with HIGH thinking."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "spoken_preamble": types.Schema(
                        type="STRING",
                        description="One short spoken sentence to play immediately while thinking (e.g. 'Give me a second to run the numbers on that.').",
                    ),
                    "reason": types.Schema(
                        type="STRING",
                        description="Brief reason why HIGH thinking is needed.",
                    ),
                },
                required=["spoken_preamble", "reason"],
            ),
        )
    )
    return [types.Tool(function_declarations=f_decls)]


_MEM_FTS_CONN: Optional[sqlite3.Connection] = None


def prefetch_zero_memory(query: str, limit: int = 4) -> str:
    """
    In-process SQLite FTS5 (`memory_fts`) memory recall with persistent read-only connection.
    Avoids spawning a Python subprocess on every Push-to-Talk turn.
    """
    global _MEM_FTS_CONN
    clean_q = (query or "").strip()
    if not clean_q or len(clean_q) < 4:
        return ""
    tokens = [
        t for t in re.findall(r"[A-Za-z0-9]{3,}", clean_q.lower())
        if t not in {"what", "when", "where", "which", "have", "from", "that", "this", "with", "your", "about"}
    ]
    if not tokens:
        return ""

    if TRANSCRIPT_INDEX_DB.exists():
        try:
            fts_query = " OR ".join(f'"{t}"*' for t in tokens[:6])
            if _MEM_FTS_CONN is None:
                _MEM_FTS_CONN = sqlite3.connect(
                    f"file:{TRANSCRIPT_INDEX_DB}?mode=ro",
                    uri=True,
                    timeout=1.5,
                    check_same_thread=False,
                )
            cur = _MEM_FTS_CONN.execute(
                """
                SELECT title, file_path, snippet(memory_fts, 5, '', '', ' ... ', 28)
                FROM memory_fts
                WHERE memory_fts MATCH ?
                ORDER BY bm25(memory_fts)
                LIMIT ?
                """,
                (fts_query, limit),
            )
            rows = cur.fetchall()
            if rows:
                formatted = [
                    f"- [{title or Path(fp).name}] ({fp}): {' '.join(str(snip).split())}"
                    for title, fp, snip in rows
                ]
                return "\n".join(formatted)[:2200]
        except Exception as exc:
            _MEM_FTS_CONN = None
            log.debug(f"[GotchiZero] In-process FTS5 recall skipped: {exc}")
    return ""


def send_gotchi_artifact_to_zero_chat(
    message: str,
    file_path: Optional[str] = None,
    screenshot_url: Optional[str] = None,
    mirror_gotchi_chat: bool = True,
) -> Dict[str, Any]:
    """
    Dispatches clickable hyperlinks, code snippets, local visual/CAD artifacts (`file_path`),
    or live webpage screenshots (`screenshot_url`) to `#zero-chat` (`1542081375287640084`)
    and optionally mirrors to `#gotchi-chat` (`1562246590943461429`).
    """
    clean_msg = (message or "").strip()
    resolved_file = (file_path or "").strip()
    clean_shot_url = (screenshot_url or "").strip()

    if clean_shot_url and not resolved_file:
        try:
            ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
            shot_out = ATTACHMENTS_DIR / f"gotchi_shot_{int(time.time()*1000)}.png"
            subprocess.run(
                [
                    "python3",
                    "/workspace/tools/capture_screenshot.py",
                    clean_shot_url,
                    "--output",
                    str(shot_out),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if shot_out.exists() and shot_out.stat().st_size > 0:
                resolved_file = str(shot_out)
        except Exception as exc:
            log.warning(f"[GotchiZero] Screenshot capture failed for {clean_shot_url}: {exc}")

    header_msg = f"📟 **[Gotchi -> #zero-chat]:**\n{clean_msg}" if clean_msg else "📟 **[Gotchi -> #zero-chat Artifact]**"
    try:
        from tools.bridge_formatting import format_for_discord

        header_msg = format_for_discord(header_msg)
    except Exception:
        pass

    file_delivered = False
    if resolved_file and Path(resolved_file).exists():
        try:
            from tools.deliver_image import deliver_file

            res_zero = deliver_file(
                resolved_file,
                channel_input=str(TARGET_CHANNEL_ID),
                caption=header_msg,
            )
            file_delivered = bool(res_zero.get("success"))
            if mirror_gotchi_chat:
                deliver_file(
                    resolved_file,
                    channel_input=str(GOTCHI_CHAT_CHANNEL_ID),
                    caption=header_msg,
                )
        except Exception as exc:
            log.warning(f"[GotchiZero] deliver_file fallback to outbox: {exc}")

    if not file_delivered:
        if not clean_msg and not resolved_file:
            return {"ok": False, "error": "Empty message and no artifact file"}
        fallback_body = header_msg
        if resolved_file:
            fallback_body += f"\n- **Artifact Path:** `{resolved_file}`"
        outbox.queue_outbox_message(
            str(TARGET_CHANNEL_ID),
            fallback_body,
            source_turn="gotchi-zero-artifact",
            dedupe_window=5,
        )
        if mirror_gotchi_chat:
            outbox.queue_outbox_message(
                str(GOTCHI_CHAT_CHANNEL_ID),
                fallback_body,
                source_turn="gotchi-zero-artifact",
                dedupe_window=5,
            )

    return {
        "ok": True,
        "status": "posted to #zero-chat",
        "channel_id": TARGET_CHANNEL_ID,
        "file_path": resolved_file or None,
        "file_delivered": file_delivered,
    }


def extract_and_forward_artifacts_to_zero_chat(
    raw_reply: str,
    spoken_reply: str,
    explicitly_posted_in_turn: bool = False,
    dispatch_discord: bool = True,
) -> Tuple[str, bool]:
    """
    Safety-net inspector for Gotchi turns:
    1. Detects if `raw_reply` contains hyperlinks (`https://...`), fenced code blocks,
       or local artifact/image paths (`/workspace/.../*.png|.stl|.3mf|...`).
    2. If `not explicitly_posted_in_turn` and artifacts/links are present in `raw_reply`,
       automatically dispatches them to `#zero-chat` (`1542081375287640084`) so nothing visual
       or clickable is lost when `clean_spoken_text_for_tts` strips URLs from the spoken audio.
    3. Ensures `spoken_reply` includes a brief verbal cue telling Ryan to check `#zero-chat`
       whenever a link or artifact was sent during the turn.
    """
    text = (raw_reply or "").strip()
    urls = _URL_RE.findall(text)
    has_code = bool(_CODE_BLOCK_RE.search(text))
    artifact_matches = _LOCAL_ARTIFACT_RE.findall(text)
    existing_artifact = next((p for p in artifact_matches if Path(p).exists()), None)

    has_shareable_content = bool(urls or has_code or existing_artifact)
    forwarded_now = False

    if has_shareable_content and not explicitly_posted_in_turn and dispatch_discord:
        send_gotchi_artifact_to_zero_chat(
            message=text,
            file_path=existing_artifact,
            mirror_gotchi_chat=False,  # Turn transcript already logs to #gotchi-chat
        )
        forwarded_now = True

    any_sent_to_zero_chat = explicitly_posted_in_turn or forwarded_now
    updated_spoken = (spoken_reply or "").strip()

    if any_sent_to_zero_chat:
        lower_spoken = updated_spoken.lower()
        if not any(cue in lower_spoken for cue in _SPOKEN_CHAT_CUES):
            cue_sentence = (
                "I sent the link to Zero-Chat."
                if urls and not existing_artifact and not has_code
                else "I sent that over to Zero-Chat."
            )
            if updated_spoken and not updated_spoken.endswith((".", "!", "?")):
                updated_spoken += "."
            updated_spoken = f"{updated_spoken} {cue_sentence}".strip()

    return updated_spoken, any_sent_to_zero_chat


def build_gotchi_zero_system_prompt(user_prompt: str = "", has_vision: bool = False) -> str:
    """
    Builds the unified Zero system prompt shared with `#zero-voice`, tuned for Gotchi's
    pocket Push-to-Talk, camera snapshot/burst, and 200x200 e-paper companion UX.
    """
    now_pt = datetime.now(PT_TZ).strftime("%A, %b %d, %Y %I:%M %p PT")
    snapshot = _load_startup_context_snapshot()
    memory_hits = prefetch_zero_memory(user_prompt) if user_prompt else ""
    mem_block = (
        f"\n\n=== PRE-FETCHED ZERO MEMORY FOR THIS TURN ===\n{memory_hits}"
        if memory_hits
        else ""
    )
    vision_note = (
        "\n- Ryan attached a live camera snapshot or 5-frame burst from Gotchi's onboard camera. "
        "Inspect the visual contents directly and answer with concrete, specific observations."
        if has_vision
        else ""
    )
    return (
        f"You are Zero, Ryan Brock's autonomous AI engineering partner running on Host 2 (.84 LAN) in the "
        f"Brock homelab (Redwood City, CA). Current time: {now_pt}.\n"
        f"Ryan is talking to you through his pocket GOTCHI hardware companion (matching your `#zero-voice` "
        f"experience with your signature `{DEFAULT_VOICE_NAME}` voice and 200x200 e-paper display).\n\n"
        "GOTCHI & ZERO-VOICE UNIFIED PERSONA INVARIANTS:\n"
        "- Speak STRICTLY in English (`en-US`).\n"
        "- Razor-sharp, supremely capable staff engineer / collaborative partner (think Tony Stark in "
        "problem-solving mode or Ben Wyatt / Jerry Seinfeld wryly noting technical absurdity).\n"
        "- Calm mastery, dry wit, warm camaraderie, zero subservience. Never say 'I hope this helps!' or "
        "'Let me know if you need anything else!'\n"
        "- SPOKEN BREVITY FOR POCKET HARDWARE: Keep your spoken answer tight, punchy, and natural "
        "(1 to 3 sentences by default) so it plays cleanly through Gotchi's speaker without monologue fatigue.\n"
        "- HYPERLINKS & VISUAL ARTIFACTS -> #zero-chat: Never read raw URLs, file paths, or code blocks out loud. "
        "Whenever a turn involves clickable hyperlinks, shopping/product links, GitHub PRs/issues, code snippets, "
        "images, CAD models, or live webpage screenshots, call `post_note_to_zero_chat` (with `message`, optional "
        "`file_path`, or optional `screenshot_url`) to send it to `#zero-chat`, and tell Ryan verbally to find it "
        "in `#zero-chat`."
        f"{vision_note}\n\n"
        "FULL AGENT PARITY & TOOL ACCESS:\n"
        "- You have 100% of the same container access, filesystem access, memory search, GitHub CLI (`gh`), "
        "SSH, Home Assistant, and research tools as `#zero-voice` and `#zero-chat`.\n"
        "- PRE-TOOL SPOKEN PREAMBLE: Before calling any tool, emit one short conversational sentence "
        "(e.g. 'Let me check that.') so Gotchi speaks immediately while the tool runs.\n"
        "- TIER-3 DEEP THINKING ESCALATION (`request_deep_thinking`): You start at `LOW` thinking for sub-second speed. "
        "Whenever a turn requires multi-step math, architecture tradeoffs, or deep root-cause debugging, call "
        "`request_deep_thinking(spoken_preamble, reason)` on Step 0 before answering so Gotchi speaks your preamble "
        "immediately while Step 1 escalates to `HIGH` thinking.\n"
        "- VOICE MUTATION GATE & READ ALLOWLIST: On voice turns, `run_shell_command` and `write_local_file` stage a "
        "confirmation card in `#zero-chat` before executing, and `read_local_file` is restricted to `os.path.realpath` "
        "under `/workspace/` and `/baseball/` (denying `.env`, `.ssh`, and secrets).\n"
        "- Use `search_zero_memory` to search durable memory or past transcripts.\n"
        "- Use `check_tasks_and_repos` to inspect `/workspace/data/tasks.json` and open GitHub issues.\n"
        "- Use `run_shell_command` to stage non-interactive bash commands in `/workspace` for confirmation.\n"
        "- Use `read_local_file` and `write_local_file` to inspect or stage file updates on disk.\n"
        "- Use `web_and_reddit_search` for live Reddit or Amazon SerpApi queries.\n"
        "- Use `get_recent_discord_context` to read recent messages across Discord channels.\n"
        "- Use `post_note_to_zero_chat` to send clickable links, notes, code blocks, local images/files (`file_path`), "
        "or webpage screenshots (`screenshot_url`) directly to `#zero-chat`.\n\n"
        f"{snapshot}{mem_block}"
    )


async def execute_gotchi_tool_call(
    name: str,
    args: Dict[str, Any],
    *,
    allow_unconfirmed_mutations: bool = False,
) -> Dict[str, Any]:
    """Executes the shared `#zero-voice` tool suite for Gotchi turns with voice security gates."""
    log.info(f"[GotchiZero] Executing tool call: {name}({args})")

    if name == "request_deep_thinking":
        preamble = str(args.get("spoken_preamble", "") or "").strip()
        reason = str(args.get("reason", "") or "model_requested").strip()
        log.info(f"[GotchiZero] Model requested HIGH thinking (reason={reason}, preamble={preamble!r})")
        return {
            "status": "escalated_to_high_thinking",
            "thinking_level": "HIGH",
            "spoken_preamble": preamble,
            "reason": reason,
        }

    if name == "search_zero_memory":
        query = str(args.get("query", "")).strip()
        target = str(args.get("target", "memory")).strip().lower()
        if target not in ("memory", "transcripts", "all"):
            target = "memory"
        if not query:
            return {"result": "Empty query."}
        if target == "memory":
            fast_hits = await asyncio.to_thread(prefetch_zero_memory, query, 6)
            if fast_hits:
                return {"result": fast_hits}
        try:
            res = await asyncio.to_thread(
                subprocess.run,
                [
                    "python3",
                    "/workspace/tools/transcript_index.py",
                    "search",
                    query,
                    "--target",
                    target,
                    "--limit",
                    "6",
                ],
                capture_output=True,
                text=True,
                timeout=12,
            )
            return {"result": (res.stdout or "No matches found.")[:3500]}
        except Exception as e:
            return {"error": str(e)}

    if name == "check_tasks_and_repos":
        repo_filter = str(args.get("repo_or_filter", "all")).strip().lower()
        out_parts: List[str] = []
        try:
            t_file = Path("/workspace/data/tasks.json")
            if t_file.exists():
                tasks = json.loads(t_file.read_text())
                pending = [t for t in tasks if t.get("status") != "completed"]
                out_parts.append(
                    "Local Pending Tasks (/workspace/data/tasks.json):\n"
                    + "\n".join(
                        f"- #{t.get('id')} [{t.get('status')}]: {t.get('title')}"
                        for t in pending[:15]
                    )
                )
            repos = ["brockventures/gotchi", "brockventures/big-projections"]
            if "sharkfin" in repo_filter or repo_filter == "all":
                repos.append("brockventures/sharkfin")
            if "home" in repo_filter or "ha" in repo_filter or repo_filter == "all":
                repos.append("brockventures/homeassistant-config")

            for r in repos:
                gh_res = await asyncio.to_thread(
                    subprocess.run,
                    ["gh", "issue", "list", "--repo", r, "--state", "open", "--limit", "10"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                if gh_res.stdout.strip():
                    out_parts.append(f"Open GitHub Issues in {r}:\n{gh_res.stdout.strip()}")
        except Exception as e:
            out_parts.append(f"Error checking tasks/repos: {e}")
        return {"result": "\n\n".join(out_parts)[:4000]}

    if name == "run_shell_command":
        cmd_str = str(args.get("command", "")).strip()
        if not cmd_str:
            return {"error": "Empty command"}
        if not allow_unconfirmed_mutations:
            return await asyncio.to_thread(
                stage_voice_mutation_confirmation,
                "run_shell_command",
                {"command": cmd_str},
            )
        try:
            res = await asyncio.to_thread(
                subprocess.run,
                cmd_str,
                shell=True,
                cwd="/workspace",
                capture_output=True,
                text=True,
                timeout=20,
            )
            combined = (res.stdout or "") + ("\nSTDERR:\n" + res.stderr if res.stderr else "")
            return {"exit_code": res.returncode, "output": combined[:4000] or "(no output)"}
        except Exception as e:
            return {"error": str(e)}

    if name == "read_local_file":
        fpath = str(args.get("file_path", "")).strip()
        max_chars = int(args.get("max_chars", 4000) or 4000)
        allowed, reason_or_resolved = is_allowed_voice_read_path(fpath)
        if not allowed:
            return {"error": f"Blocked by voice read allowlist: {reason_or_resolved}"}
        try:
            p = Path(reason_or_resolved)
            if not p.exists():
                return {"error": f"File not found: {fpath}"}
            return {"content": p.read_text(errors="replace")[:max_chars]}
        except Exception as e:
            return {"error": str(e)}

    if name == "write_local_file":
        fpath = str(args.get("file_path", "")).strip()
        content = str(args.get("content", ""))
        if not fpath:
            return {"error": "Empty file_path"}
        if not allow_unconfirmed_mutations:
            return await asyncio.to_thread(
                stage_voice_mutation_confirmation,
                "write_local_file",
                {"file_path": fpath, "content": content},
            )
        try:
            p = Path(fpath)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return {"status": f"Successfully wrote {len(content)} chars to {fpath}"}
        except Exception as e:
            return {"error": str(e)}

    if name == "web_and_reddit_search":
        query = str(args.get("query", "")).strip()
        sub = str(args.get("subreddit", "")).strip()
        mode = str(args.get("mode", "reddit")).strip().lower()
        if not query:
            return {"error": "Empty query"}
        try:
            if mode == "amazon":
                cmd = ["python3", "/workspace/tools/amazon_serpapi.py", query]
            else:
                cmd = ["python3", "/workspace/tools/reddit_extractor.py", "search", query, "-n", "5"]
                if sub:
                    cmd.extend(["--sub", sub])
            res = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=18,
            )
            return {"result": (res.stdout or res.stderr or "No results")[:3500]}
        except Exception as e:
            return {"error": str(e)}

    if name == "get_recent_discord_context":
        ch_name = str(args.get("channel_name", "zero-chat")).strip().lower()
        ch_map = {
            "zero-chat": str(TARGET_CHANNEL_ID),
            "gotchi-chat": str(GOTCHI_CHAT_CHANNEL_ID),
            "baseball": "1548196929308065893",
            "zero-ops": "1544953279664889888",
            "homelab": "1544955535722545253",
            "finances": "1544955532765560924",
            "shopping": "1544955538033348618",
            "projects": "1548196930788524094",
        }
        cid = ch_map.get(ch_name, str(TARGET_CHANNEL_ID))
        try:
            ch_file = Path("/workspace/data/channel_history.json")
            if ch_file.exists():
                msgs = json.loads(ch_file.read_text()).get(cid, [])[-12:]
                formatted = "\n".join(
                    f"[{m.get('timestamp_pt') or m.get('timestamp')}] {m.get('author')}: {str(m.get('content'))[:300]}"
                    for m in msgs
                )
                return {"result": formatted or "No recent messages."}
        except Exception as e:
            return {"error": str(e)}

    if name == "post_note_to_zero_chat":
        msg_text = str(args.get("message", "") or "").strip()
        file_path = str(args.get("file_path", "") or "").strip() or None
        screenshot_url = str(args.get("screenshot_url", "") or "").strip() or None
        res = await asyncio.to_thread(
            send_gotchi_artifact_to_zero_chat,
            msg_text,
            file_path,
            screenshot_url,
            True,
        )
        return res

    return {"error": f"Unknown function {name}"}


def record_gotchi_turn_to_channels(
    node_id: str,
    session_id: str,
    transcript: str,
    reply: str,
    mode: str = "interact",
    voice_name: str = DEFAULT_VOICE_NAME,
    image_bytes: Optional[bytes] = None,
    burst_images: Optional[List[bytes]] = None,
    stt_ms: int = 0,
    first_audio_ms: Optional[int] = None,
    total_ms: Optional[int] = None,
    dispatch_discord: bool = True,
) -> Dict[str, Any]:
    """
    Persists a completed Gotchi turn into:
    1. `/workspace/data/voice_transcripts.json` (shared with `#zero-voice`).
    2. `/workspace/data/channel_history.json` for BOTH `#zero-chat` (`1542081375287640084`)
       and `#gotchi-chat` (`1562246590943461429`) so Zero has instant continuity everywhere.
    3. Standalone `#gotchi-chat` Discord channel (`1562246590943461429`) via `deliver_image.py`
       (when camera snapshots are present) or `outbox.queue_outbox_message`.
    """
    source_tag = f"📟 {node_id}"
    if transcript:
        _append_voice_turn_to_history(
            "Ryan",
            transcript,
            source_tag=source_tag,
            extra_channel_ids=[GOTCHI_CHAT_CHANNEL_ID],
        )
    if reply:
        _append_voice_turn_to_history(
            "Zero",
            reply,
            source_tag=source_tag,
            extra_channel_ids=[GOTCHI_CHAT_CHANNEL_ID],
        )

    burst_list = list(burst_images or [])
    primary_img = image_bytes or (burst_list[0] if burst_list else None)
    saved_image_path: Optional[str] = None

    if primary_img:
        try:
            ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            img_path = ATTACHMENTS_DIR / f"gotchi_{node_id}_{ts}.jpg"
            img_path.write_bytes(primary_img)
            saved_image_path = str(img_path)
        except Exception as e:
            log.warning(f"[GotchiZero] Failed saving snapshot attachment: {e}")

    img_badge = ""
    if len(burst_list) > 1:
        img_badge = f" 📸 *({len(burst_list)}-frame burst)*"
    elif primary_img:
        img_badge = " 📸 *(camera snapshot)*"

    timing_footer = ""
    if total_ms is not None and total_ms > 0:
        fa_str = f" · first_audio: {first_audio_ms}ms" if first_audio_ms is not None else ""
        timing_footer = f"\n-# session: {session_id} · stt: {stt_ms}ms{fa_str} · total: {total_ms}ms"

    if mode == "journal":
        discord_msg = f"📓 **[{node_id} · All-Day Journal]{img_badge}:**\n> {transcript}{timing_footer}"
    else:
        discord_msg = (
            f"🎙️ **Ryan ({node_id}){img_badge}:** {transcript}\n"
            f"🔊 **Zero ({voice_name}):** {reply}{timing_footer}"
        )

    dispatched = False
    if dispatch_discord:
        if saved_image_path:
            try:
                from tools.deliver_image import deliver_image

                deliver_image(
                    saved_image_path,
                    channel_input=str(GOTCHI_CHAT_CHANNEL_ID),
                    caption=discord_msg,
                )
                dispatched = True
            except Exception as e:
                log.warning(f"[GotchiZero] Image delivery fallback to outbox: {e}")
        if not dispatched:
            outbox.queue_outbox_message(
                str(GOTCHI_CHAT_CHANNEL_ID),
                discord_msg,
                dedupe_window=5,
            )
            dispatched = True

    return {
        "ok": True,
        "node_id": node_id,
        "session_id": session_id,
        "channel_id": GOTCHI_CHAT_CHANNEL_ID,
        "saved_image_path": saved_image_path,
        "discord_message": discord_msg,
        "dispatched": dispatched,
    }


class GotchiZeroBrainAdapter:
    """
    Zero Brain Adapter for Gotchi (`POST /api/voice/interact` and `/api/gotchi/brain`).
    Shares system prompt, memory pre-fetching, tool declarations, and conversation history
    with `#zero-voice` (`tools/bridge_voice.py`).
    """

    def __init__(
        self,
        model: str = DEFAULT_GOTCHI_MODEL,
        api_key: Optional[str] = None,
        stt_adapter: Optional[Any] = None,
        llm_caller: Optional[Callable[[Dict[str, Any]], Any]] = None,
        dispatch_discord: bool = True,
        thinking_budget: int = 0,
    ):
        self.model = model
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("GEMINI_API_KEY", "").strip()
        )
        self.stt_adapter = stt_adapter
        self.llm_caller = llm_caller
        self.dispatch_discord = dispatch_discord
        self.thinking_budget = thinking_budget
        self._genai_client: Optional[Any] = None

    def _get_genai_client(self) -> Any:
        from google import genai

        if not self.api_key:
            from dotenv import load_dotenv

            load_dotenv("/workspace/.env")
            self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not self.api_key:
            return None
        if self._genai_client is None:
            self._genai_client = genai.Client(api_key=self.api_key)
        return self._genai_client

    async def _emit_streamed_sentence(
        self,
        sentence: str,
        emitted_sentences: List[str],
        on_sentence: Optional[Callable[[str], Any]],
    ) -> None:
        cleaned = clean_spoken_text_for_tts(sentence)
        if not cleaned or cleaned in emitted_sentences:
            return
        emitted_sentences.append(cleaned)
        if on_sentence is not None:
            res = on_sentence(cleaned)
            if asyncio.iscoroutine(res):
                await res

    async def _run_gemini_with_tools(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        burst_images: List[bytes],
        on_sentence: Optional[Callable[[str], Any]] = None,
    ) -> Tuple[str, bool, List[str]]:
        from backend.voice_gateway import SentenceStreamSplitter
        from google.genai import types

        client = self._get_genai_client()
        if client is None:
            return "Zero is online, but GEMINI_API_KEY is not configured in the environment.", False, []

        has_vision = bool(image_bytes or burst_images)
        sys_prompt = await asyncio.to_thread(
            build_gotchi_zero_system_prompt, prompt, has_vision
        )

        user_parts: List[types.Part] = []
        if image_bytes:
            user_parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        for b_img in burst_images[:5]:
            user_parts.append(types.Part.from_bytes(data=b_img, mime_type="image/jpeg"))
        user_parts.append(types.Part.from_text(text=prompt or "Describe what you see."))

        contents: List[types.Content] = [types.Content(role="user", parts=user_parts)]
        tools_list = _build_gotchi_tools()

        explicitly_posted_to_zero_chat = False
        tool_had_error = False
        model_requested_deep_thinking = False
        emitted_sentences: List[str] = []
        accumulated_raw_parts: List[str] = []

        for _step in range(4):
            config = types.GenerateContentConfig(
                system_instruction=sys_prompt,
                tools=tools_list,
                temperature=0.4,
                thinking_config=build_thinking_config(
                    self.model,
                    prompt,
                    default_budget=self.thinking_budget,
                    step=_step,
                    tool_had_error=tool_had_error,
                    burst_count=len(burst_images),
                    model_requested_deep_thinking=model_requested_deep_thinking,
                ),
            )

            splitter = SentenceStreamSplitter()
            step_text_chunks: List[str] = []
            step_fcalls: List[Any] = []
            step_model_parts: List[types.Part] = []

            stream_fn = getattr(client.aio.models, "generate_content_stream", None)
            if callable(stream_fn):
                stream_obj = stream_fn(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                if asyncio.iscoroutine(stream_obj):
                    stream_obj = await stream_obj
                async for chunk in stream_obj:
                    chunk_text = getattr(chunk, "text", None) or ""
                    if chunk_text:
                        step_text_chunks.append(chunk_text)
                        for ready_s in splitter.feed(chunk_text):
                            await self._emit_streamed_sentence(
                                ready_s, emitted_sentences, on_sentence
                            )
                    chunk_fcalls = getattr(chunk, "function_calls", None)
                    if chunk_fcalls:
                        step_fcalls.extend(list(chunk_fcalls))
                    cands = getattr(chunk, "candidates", None)
                    if cands and getattr(cands[0], "content", None):
                        c_parts = getattr(cands[0].content, "parts", None) or []
                        step_model_parts.extend(list(c_parts))
            else:
                resp = await client.aio.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                resp_text = getattr(resp, "text", None) or ""
                if resp_text:
                    step_text_chunks.append(resp_text)
                    for ready_s in splitter.feed(resp_text):
                        await self._emit_streamed_sentence(
                            ready_s, emitted_sentences, on_sentence
                        )
                if getattr(resp, "function_calls", None):
                    step_fcalls.extend(list(resp.function_calls))
                if getattr(resp, "candidates", None) and getattr(resp.candidates[0], "content", None):
                    c_parts = getattr(resp.candidates[0].content, "parts", None) or []
                    step_model_parts.extend(list(c_parts))

            for trailing_s in splitter.flush():
                await self._emit_streamed_sentence(
                    trailing_s, emitted_sentences, on_sentence
                )

            step_text = "".join(step_text_chunks).strip()
            if step_text:
                accumulated_raw_parts.append(step_text)

            if not step_fcalls:
                final_raw = " ".join(accumulated_raw_parts).strip()
                return final_raw, explicitly_posted_to_zero_chat, emitted_sentences

            if step_model_parts:
                contents.append(types.Content(role="model", parts=step_model_parts))

            fn_parts: List[types.Part] = []
            tool_had_error = False
            for fc in step_fcalls:
                fn_name = getattr(fc, "name", "")
                fn_args = dict(getattr(fc, "args", {}) or {})
                if fn_name in ("post_note_to_zero_chat", "run_shell_command", "write_local_file"):
                    explicitly_posted_to_zero_chat = True
                if fn_name == "request_deep_thinking":
                    model_requested_deep_thinking = True
                    preamble = str(fn_args.get("spoken_preamble", "") or "").strip()
                    if preamble:
                        if preamble not in accumulated_raw_parts:
                            accumulated_raw_parts.append(preamble)
                        await self._emit_streamed_sentence(
                            preamble, emitted_sentences, on_sentence
                        )
                elif not step_text and not emitted_sentences:
                    # Pre-tool spoken preamble fallback when the model invokes a tool without leading text
                    default_preamble = "Let me check that."
                    await self._emit_streamed_sentence(
                        default_preamble, emitted_sentences, on_sentence
                    )

                res_dict = await execute_gotchi_tool_call(fn_name, fn_args)
                if (
                    isinstance(res_dict, dict)
                    and (
                        bool(res_dict.get("error"))
                        or int(res_dict.get("exit_code", 0) or 0) != 0
                    )
                ):
                    tool_had_error = True

                fn_parts.append(
                    types.Part.from_function_response(
                        name=fn_name,
                        response=res_dict,
                    )
                )
            contents.append(types.Content(role="user", parts=fn_parts))

        fallback_raw = " ".join(accumulated_raw_parts).strip() or "Handled. I sent that over to Zero-Chat."
        return fallback_raw, explicitly_posted_to_zero_chat, emitted_sentences

    async def handle_turn(self, parsed_turn: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compatible directly with `VoiceGatewayService(turn_handler=adapter.handle_turn)`
        and `SubscriptionBrainAdapter` JSON payloads.
        """
        t0 = time.monotonic()
        node_id = str(parsed_turn.get("node_id") or "gotchi-ryan-01")
        session_id = str(parsed_turn.get("session_id") or f"sess-{int(time.time()*1000)}")
        on_sentence: Optional[Callable[[str], Any]] = parsed_turn.get("_on_sentence")

        image_bytes: Optional[bytes] = parsed_turn.get("image_bytes")
        if image_bytes is None and parsed_turn.get("image_b64"):
            image_bytes = base64.b64decode(parsed_turn["image_b64"])

        burst_images: List[bytes] = list(parsed_turn.get("burst_images") or [])
        if not burst_images and parsed_turn.get("burst_images_b64"):
            burst_images = [
                base64.b64decode(b) for b in parsed_turn["burst_images_b64"] if b
            ]

        has_explicit_prompt = bool(parsed_turn.get("prompt"))
        has_images = bool(image_bytes or burst_images)

        if has_explicit_prompt:
            transcript = str(parsed_turn["prompt"]).strip()
        elif parsed_turn.get("audio_bytes") and self.stt_adapter is not None:
            loop = asyncio.get_running_loop()
            transcript = await loop.run_in_executor(
                None, self.stt_adapter.transcribe, parsed_turn["audio_bytes"]
            )
        elif burst_images:
            transcript = "Analyze this 5-frame camera burst from Gotchi."
        elif image_bytes:
            transcript = "Analyze this camera snapshot from Gotchi."
        else:
            transcript = "Status check."

        stt_ms = int((time.monotonic() - t0) * 1000)

        # Early ghost-turn short-circuit: skip LLM & Discord logging on <400ms PTT bumps
        from backend.voice_gateway import is_stt_ghost, split_into_sentences

        is_ghost, _reason = is_stt_ghost(
            transcript_text=transcript,
            audio_bytes=parsed_turn.get("audio_bytes"),
            has_explicit_prompt=has_explicit_prompt,
            has_images=has_images,
        )
        if is_ghost:
            return {
                "transcript": transcript,
                "reply": "",
                "raw_reply": "",
                "sentences": [],
                "stt_ms": stt_ms,
            }

        explicitly_posted = False
        streamed_sentences: List[str] = []
        if self.llm_caller is not None:
            raw_res = self.llm_caller(
                {
                    "node_id": node_id,
                    "session_id": session_id,
                    "prompt": transcript,
                    "image_bytes": image_bytes,
                    "burst_images": burst_images,
                    "_on_sentence": on_sentence,
                }
            )
            if asyncio.iscoroutine(raw_res):
                raw_res = await raw_res
            if isinstance(raw_res, dict):
                raw_reply = str(raw_res.get("reply") or "")
                explicitly_posted = bool(raw_res.get("posted_to_zero_chat", False))
            else:
                raw_reply = str(raw_res)
        else:
            raw_reply, explicitly_posted, streamed_sentences = await self._run_gemini_with_tools(
                prompt=transcript,
                image_bytes=image_bytes,
                burst_images=burst_images,
                on_sentence=on_sentence,
            )

        spoken_reply = clean_spoken_text_for_tts(raw_reply) or "Got it."
        spoken_reply, sent_to_zero_chat = await asyncio.to_thread(
            extract_and_forward_artifacts_to_zero_chat,
            raw_reply,
            spoken_reply,
            explicitly_posted,
            self.dispatch_discord,
        )

        total_ms = int((time.monotonic() - t0) * 1000)
        sentences = split_into_sentences(spoken_reply)
        for s_item in streamed_sentences:
            if s_item not in sentences:
                sentences.insert(0, s_item)

        await asyncio.to_thread(
            record_gotchi_turn_to_channels,
            node_id,
            session_id,
            transcript,
            raw_reply or spoken_reply,
            str(parsed_turn.get("mode") or "interact"),
            DEFAULT_VOICE_NAME,
            image_bytes,
            burst_images,
            stt_ms,
            None,
            total_ms,
            self.dispatch_discord,
        )

        return {
            "transcript": transcript,
            "reply": spoken_reply,
            "raw_reply": raw_reply,
            "sentences": sentences,
            "stt_ms": stt_ms,
            "sent_to_zero_chat": sent_to_zero_chat,
        }


def create_zero_gotchi_app(
    adapter: Optional[GotchiZeroBrainAdapter] = None,
    live_mode_enabled: bool = True,
):
    """
    Creates the full FastAPI Gotchi Gateway (`POST /api/voice/interact`, `WebSocket /api/live/ws`,
    `POST /api/journal/ingest`, `GET /api/voice/logs`, plus `POST /api/gotchi/brain`) wired
    directly to Zero's brain and `#gotchi-chat` transcript recorder.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from backend.live_gateway import LiveGatewayService
    from backend.llm_config import LLMConfig
    from backend.voice_gateway import VoiceGatewayService, create_app

    brain = adapter or GotchiZeroBrainAdapter()
    cfg = LLMConfig.from_env_or_file()
    cfg.voice_name = DEFAULT_VOICE_NAME

    def _on_gateway_turn_logged(entry: Dict[str, Any]) -> None:
        # Ensure journal turns or non-custom-handler turns also mirror to #gotchi-chat
        if entry.get("mode") == "journal":
            record_gotchi_turn_to_channels(
                node_id=entry.get("node_id", "gotchi-ryan-01"),
                session_id=entry.get("session_id", "sess-0"),
                transcript=entry.get("transcript", ""),
                reply="",
                mode="journal",
                voice_name=entry.get("voice_name", DEFAULT_VOICE_NAME),
                dispatch_discord=brain.dispatch_discord,
            )

    voice_srv = VoiceGatewayService(
        llm_config=cfg,
        turn_handler=brain.handle_turn,
        turn_log_router=_on_gateway_turn_logged,
    )
    brain.stt_adapter = voice_srv.stt_adapter
    live_srv = LiveGatewayService(
        verifier=voice_srv.verifier,
        auth_required=voice_srv.auth_required,
        live_mode_enabled=live_mode_enabled,
        llm_config=cfg,
    )
    app = create_app(service=voice_srv, live_service=live_srv)

    @app.post("/api/gotchi/brain")
    async def gotchi_brain_endpoint(request: Request):
        body = await request.json()
        res = await brain.handle_turn(body)
        return JSONResponse(status_code=200, content=res)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Gotchi <-> Zero Unified Brain & Transcript Adapter")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read SubscriptionBrainAdapter JSON payload from stdin and write JSON reply to stdout",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run the unified Gotchi + Zero FastAPI gateway server",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for --serve (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port for --serve (default: 8765)")
    args = parser.parse_args()

    if args.stdin:
        raw_in = sys.stdin.read().strip()
        payload = json.loads(raw_in) if raw_in else {}
        adapter = GotchiZeroBrainAdapter()
        result = asyncio.run(adapter.handle_turn(payload))
        sys.stdout.write(json.dumps(result) + "\n")
        return

    if args.serve:
        import uvicorn

        app = create_zero_gotchi_app()
        uvicorn.run(app, host=args.host, port=args.port)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
