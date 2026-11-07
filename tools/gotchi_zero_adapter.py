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
     (`screenshot_url` via `capture_screenshot.py`) directly to `#zero-chat`.
   - Deterministic Safety Net (`extract_and_forward_artifacts_to_zero_chat`): Even if the LLM
     includes URLs (`https://...`), fenced code blocks, or local artifact paths in its reply
     without calling `post_note_to_zero_chat`, the adapter automatically forwards them to
     `#zero-chat` before `clean_spoken_text_for_tts` strips them from the spoken audio, and
     ensures Gotchi verbally tells Ryan to check `#zero-chat`.

3. Speed & Latency Optimizations:
   - `ThinkingConfig(thinking_budget=128)` by default on `gemini-2.5-flash` (ensures fact
     connection and tool reliability with ~50ms latency; auto-elevates to `256` on tool synthesis,
     `512` on errors/bursts/step 2+, and `1024` on deep queries).
   - Persistent `genai.Client` singleton keeps HTTP/2 TLS connections warm across turns.
   - Sub-2ms in-process SQLite FTS5 (`memory_fts`) recall instead of spawning a subprocess.
   - Early `is_stt_ghost` check aborts `<400ms` accidental PTT bumps before invoking Gemini.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if f"{_REPO_ROOT}/gotchi" not in sys.path:
    sys.path.insert(1, f"{_REPO_ROOT}/gotchi")
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
if "/workspace/gotchi" not in sys.path:
    sys.path.append("/workspace/gotchi")
for _lomin_path in ("/tmp/lomin-parity-repo", "/workspace/gotchi/lomin"):
    if Path(_lomin_path).is_dir() and _lomin_path not in sys.path:
        sys.path.append(_lomin_path)

ZERO_CHAT_CHANNEL_ID = int(os.environ.get("GOTCHI_ZERO_CHAT_CHANNEL_ID", "0") or 0)
TARGET_CHANNEL_ID = ZERO_CHAT_CHANNEL_ID
log = logging.getLogger("gotchi_zero_adapter")

from backend.voice_gateway import (
    condense_long_answer,
    extract_topic,
    is_long_answer,
)

if Path("/workspace/tools").is_dir() and os.environ.get("GOTCHI_STANDALONE") != "1":
    from tools.bridge_voice import (
        DEFAULT_VOICE_NAME,
        GOTCHI_CHAT_CHANNEL_ID,
        TARGET_CHANNEL_ID as _BRIDGE_ZERO_CHAT_ID,
        _append_voice_turn_to_history,
        _build_live_tools,
        _load_startup_context_snapshot,
    )
    from tools import outbox
    if not ZERO_CHAT_CHANNEL_ID:
        ZERO_CHAT_CHANNEL_ID = int(outbox.resolve_channel("zero-chat")[1] or _BRIDGE_ZERO_CHAT_ID or 0)
        TARGET_CHANNEL_ID = ZERO_CHAT_CHANNEL_ID
else:
    log.warning(
        "[GotchiZero] /workspace/tools not present (or GOTCHI_STANDALONE=1); "
        "using standalone stubs for bridge_voice and outbox."
    )
    DEFAULT_VOICE_NAME = "Schedar"
    GOTCHI_CHAT_CHANNEL_ID = int(os.environ.get("GOTCHI_CHAT_CHANNEL_ID", "0") or 0)

    def _append_voice_turn_to_history(*_args: Any, **_kwargs: Any) -> None:
        return None

    def _build_live_tools() -> List[Any]:
        return []

    def _load_startup_context_snapshot() -> str:
        return ""

    class _FallbackOutbox:
        @staticmethod
        def queue_outbox_message(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
            return {"status": "stub"}

        @staticmethod
        def queue_agent_turn(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
            return {"id": "stub", "status": "stub"}

    outbox = _FallbackOutbox()  # type: ignore[assignment]

PT_TZ = ZoneInfo("America/Los_Angeles")

DEFAULT_GOTCHI_MODEL = os.environ.get("GOTCHI_ZERO_MODEL", "gemini-3.8-flash")
DEFAULT_THINKING_BUDGET = 128
ATTACHMENTS_DIR = Path("/workspace/data/attachments")
TRANSCRIPT_INDEX_DB = Path("/workspace/data/transcript_index.db")

DEFAULT_HOME_LOCATION = os.environ.get("HOME_LOCATION_LABEL", "Local Home Area")
DEFAULT_HOME_LAT = 37.4852
DEFAULT_HOME_LON = -122.2364
DEFAULT_HOME_TIMEZONE = "America/Los_Angeles"

PLACEHOLDER_PROMPTS = frozenset({
    "voice prompt",
    "what is the status?",
    "what is the status",
    "describe what you see in this snapshot briefly.",
    "describe what you see in this snapshot briefly",
})


def is_placeholder_prompt(prompt: Optional[str]) -> bool:
    """Returns True if prompt is one of the generic client placeholder strings."""
    if not prompt:
        return False
    norm = str(prompt).strip().lower()
    return norm in PLACEHOLDER_PROMPTS or norm.rstrip(".?") in {
        "voice prompt",
        "what is the status",
        "describe what you see in this snapshot briefly",
    }

_URL_RE = re.compile(r"https?://[^\s)>\]\"'`]+", re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]+?```")
_LOCAL_ARTIFACT_RE = re.compile(
    r"(/workspace/[^\s)>\]\"'`]+\.(?:png|jpg|jpeg|webp|gif|stl|3mf|step|pdf|svg))",
    re.IGNORECASE,
)
_ALLOWED_VOICE_READ_PREFIXES_DEFAULT = (
    "/workspace/",
    "/baseball/",
    "/root/.gemini/antigravity-cli/skills/",
    "/root/.gemini/antigravity-cli/builtin/skills/",
)
_ALLOWED_VOICE_READ_PREFIXES = _ALLOWED_VOICE_READ_PREFIXES_DEFAULT
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
    if allowed_prefixes is _ALLOWED_VOICE_READ_PREFIXES_DEFAULT:
        allowed_prefixes = _ALLOWED_VOICE_READ_PREFIXES
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


DESTRUCTIVE_COMMAND_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    # 1. File deletion / truncation
    (
        re.compile(r"(?<![a-zA-Z0-9_\-])(rm|rmdir|unlink|shred|truncate)\b"),
        "File deletion or truncation command",
    ),
    (
        re.compile(r"\bfind\b.*?\s-delete\b"),
        "find with destructive -delete flag",
    ),
    (
        re.compile(r"\b(os\.remove|os\.unlink|shutil\.rmtree)\b"),
        "Destructive Python filesystem deletion",
    ),
    # 2. Destructive git operations
    (
        re.compile(r"\bgit\s+reset\b.*?\s--hard\b"),
        "Destructive git reset --hard",
    ),
    (
        re.compile(r"\bgit\s+clean\b"),
        "Destructive git clean",
    ),
    (
        re.compile(r"\bgit\s+push\b.*?\s(--force|-f|--force-with-lease)\b"),
        "Destructive git push --force/-f",
    ),
    (
        re.compile(r"\bgit\s+checkout\b.*?\s--(?:\s|$)"),
        "Destructive git checkout -- discard",
    ),
    (
        re.compile(r"\bgit\s+restore\b"),
        "Destructive git restore",
    ),
    (
        re.compile(r"\bgit\s+branch\b.*?\s-D\b"),
        "Destructive git branch -D force deletion",
    ),
    (
        re.compile(r"\bgit\s+rebase\b"),
        "Destructive git rebase",
    ),
    # 3. Service / container / system power or teardown
    (
        re.compile(r"(?<![a-zA-Z0-9_\-])(reboot|shutdown|poweroff|halt)\b"),
        "System power or shutdown operation",
    ),
    (
        re.compile(r"\b(systemctl|service)\b.*?\b(stop|restart|disable|mask|kill)\b"),
        "System service teardown or restart",
    ),
    (
        re.compile(
            r"\bdocker(-compose|\s+compose)?\b.*?\b(down|stop|restart|kill|rm|rmi|system\s+prune)\b"
        ),
        "Docker container or image teardown/prune",
    ),
    (
        re.compile(r"(?<![a-zA-Z0-9_\-])(kill|pkill|killall)(?![a-zA-Z0-9_\-])"),
        "Process termination command",
    ),
    # 4. Disk / permission sweeps
    (
        re.compile(r"(?<![a-zA-Z0-9_\-])(mkfs(\.[a-zA-Z0-9]+)?|fdisk|parted|wipefs)\b"),
        "Disk partitioning or filesystem format command",
    ),
    (
        re.compile(r"\bdd\b.*?\bif="),
        "Raw disk writing operation (dd)",
    ),
    (
        re.compile(r"\bchmod\b.*?\s-[a-zA-Z]*R[a-zA-Z]*\b"),
        "Recursive permission modification (chmod -R)",
    ),
    (
        re.compile(r"\bchown\b.*?\s-[a-zA-Z]*R[a-zA-Z]*\b"),
        "Recursive ownership modification (chown -R)",
    ),
    # 5. Destructive SQL
    (
        re.compile(
            r"\b(drop\s+table|drop\s+database|truncate\s+table|delete\s+from)\b",
            re.IGNORECASE,
        ),
        "Destructive SQL query",
    ),
    # 6. Package removal
    (
        re.compile(r"\bapt(-get)?\b.*?\b(remove|purge)\b"),
        "Package removal via apt/apt-get",
    ),
    (
        re.compile(r"\bpip[0-9.]*\b.*?\buninstall\b"),
        "Package uninstallation via pip",
    ),
    (
        re.compile(r"\bnpm\b.*?\buninstall\b"),
        "Package uninstallation via npm",
    ),
    # 7. Sensitive output redirection
    (
        re.compile(
            r"(?:>+|\btee\b(?:[\s\w-]*))\s*.*?(?:\.env|\.ssh|/?secrets/|credentials|token\.json|pii_rules\.json)",
            re.IGNORECASE,
        ),
        "Redirecting output to sensitive configuration or secret file",
    ),
)


def is_destructive_voice_command(command: str) -> Tuple[bool, str]:
    """
    Evaluates whether a shell command on a voice turn is destructive or dangerous.
    Returns (True, reason) ONLY for destructive operations (file deletion, destructive git,
    service teardown, power off, process termination, disk formatting, destructive SQL,
    package uninstalls, or redirects to sensitive paths).
    Returns (False, "") for safe, non-destructive commands (e.g. git status/diff, find,
    grep, ls, cat, curl, python3, etc.).
    """
    cmd = (command or "").strip()
    if not cmd:
        return False, ""
    for pattern, reason in DESTRUCTIVE_COMMAND_PATTERNS:
        m = pattern.search(cmd)
        if m:
            return True, f"{reason}: matched '{m.group(0)}'"
    return False, ""


def is_destructive_voice_write(
    file_path: str,
    allowed_prefixes: Tuple[str, ...] = _ALLOWED_VOICE_READ_PREFIXES,
) -> Tuple[bool, str]:
    """
    Evaluates whether writing to `file_path` on a voice turn is destructive or unauthorized:
    1. If `is_allowed_voice_read_path` fails (outside allowed roots, sensitive files like .env or .ssh),
       returns (True, reason).
    2. If Path(resolved).exists() is True (overwriting an existing file),
       returns (True, f"Overwriting existing file '{resolved}' requires confirmation").
    3. If creating a new file inside an allowed root, returns (False, resolved).
    """
    if allowed_prefixes is _ALLOWED_VOICE_READ_PREFIXES_DEFAULT:
        allowed_prefixes = _ALLOWED_VOICE_READ_PREFIXES
    allowed, reason_or_resolved = is_allowed_voice_read_path(
        file_path, allowed_prefixes=allowed_prefixes
    )
    if not allowed:
        return True, reason_or_resolved

    resolved = reason_or_resolved
    if Path(resolved).exists():
        return True, f"Overwriting existing file '{resolved}' requires confirmation"

    return False, resolved


def stage_voice_mutation_confirmation(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Gates every `run_shell_command` and `write_local_file` on a voice turn behind a text
    confirmation card in `#zero-chat` so a misheard Whisper word
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
    cleaned = re.sub(r"\[\[defer\]\]", "", cleaned, flags=re.I)
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


NODE_SPEAKER_PROFILES: Dict[str, str] = {
    "mike": "Mike",
    "coley": "Dr. Coley",
    "alex": "Alex",
    "ryan": "Ryan",
}


def resolve_speaker_for_node(node_id: str = "gotchi-v1-01", speaker: Optional[str] = None) -> str:
    if speaker is not None and str(speaker).strip():
        raw_s = str(speaker).strip()
        low_s = raw_s.lower()
        if low_s in NODE_SPEAKER_PROFILES:
            return NODE_SPEAKER_PROFILES[low_s]
        return raw_s
    nid_low = (node_id or "").lower()
    for key, label in NODE_SPEAKER_PROFILES.items():
        if key in nid_low:
            return label
    return "Ryan"


def format_voice_turn_prefix(node_id: str = "gotchi-v1-01", speaker: Optional[str] = None) -> str:
    resolved_speaker = resolve_speaker_for_node(node_id, speaker)
    return (
        f"[VOICE TURN node={node_id} speaker={resolved_speaker} — reply for speech in 1-2 short natural sentences, "
        "plain text, no markdown; prefix sentence 1 with [[defer]] if multi-step background work is needed]"
    )


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
    default_budget: int = DEFAULT_THINKING_BUDGET,
    *,
    step: int = 0,
    tool_had_error: bool = False,
    burst_count: int = 0,
    model_requested_deep_thinking: bool = False,
) -> int:
    """
    3-Tier Thinking Budget equivalent for `gemini-2.5-flash`:
    - Tier 1 (`LOW`): `128` tokens on Step 0, `256` tokens on Step 1 (tool synthesis).
    - Tier 2 (`MEDIUM`): `512` tokens on tool error, Step 2+, or 5-frame camera burst.
    - Tier 3 (`HIGH`): `1024` tokens when `request_deep_thinking` is invoked.
    """
    _ = prompt
    if model_requested_deep_thinking:
        return max(default_budget, 1024)
    if tool_had_error or step >= 2 or burst_count > 1:
        return max(default_budget, 512)
    if step == 1:
        return max(default_budget, 256)
    return default_budget


def build_thinking_config(
    model: str,
    prompt: str = "",
    default_budget: int = DEFAULT_THINKING_BUDGET,
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


WMO_WEATHER_CODES: Dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow fall",
    73: "Moderate snow fall",
    75: "Heavy snow fall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _is_home_location_query(location: Optional[str]) -> bool:
    """Checks whether the requested location is empty or refers to Redwood City/home/outside."""
    if not location:
        return True
    loc = str(location).strip().lower()
    if not loc:
        return True
    loc_clean = re.sub(r"[^\w\s]", " ", loc)
    tokens = set(loc_clean.split())
    home_tokens = {
        "home",
        "here",
        "outside",
        "local",
        "current",
        "house",
        "backyard",
        "patio",
        "redwood",
        "",
    }
    if tokens & home_tokens:
        return True
    if "redwood city" in loc:
        return True
    return False


def _fetch_open_meteo_forecast(
    lat: float,
    lon: float,
    timezone: str = DEFAULT_HOME_TIMEZONE,
    location_name: str = DEFAULT_HOME_LOCATION,
) -> Dict[str, Any]:
    """Queries Open-Meteo REST API for current conditions and daily high/low forecast."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&"
        f"current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m&"
        f"daily=temperature_2m_max,temperature_2m_min&"
        f"temperature_unit=fahrenheit&wind_speed_unit=mph&"
        f"timezone={urllib.parse.quote(timezone)}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "GotchiZero/1.0"})
    with urllib.request.urlopen(req, timeout=3.5) as resp:
        if resp.status != 200:
            return {"error": f"Open-Meteo returned status {resp.status}", "location": location_name}
        data = json.loads(resp.read().decode("utf-8"))

    current = data.get("current", {})
    daily = data.get("daily", {})
    weather_code = current.get("weather_code")
    condition = WMO_WEATHER_CODES.get(weather_code, "Unknown")
    temp = current.get("temperature_2m")
    apparent_temp = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind_speed = current.get("wind_speed_10m")

    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    high = highs[0] if (highs and isinstance(highs, list)) else None
    low = lows[0] if (lows and isinstance(lows, list)) else None

    summary_parts = [f"Currently {condition.lower()}, {temp}°F in {location_name}."]
    if high is not None and low is not None:
        summary_parts.append(f"Today's high is {high}°F with a low of {low}°F.")
    if humidity is not None:
        summary_parts.append(f"Humidity {humidity}%.")
    if wind_speed is not None:
        summary_parts.append(f"Wind {wind_speed} mph.")

    return {
        "location": location_name,
        "temperature": temp,
        "temperature_unit": "°F",
        "condition": condition,
        "feels_like": apparent_temp,
        "humidity": humidity,
        "wind_speed": wind_speed,
        "wind_speed_unit": "mph",
        "high": high,
        "low": low,
        "source": "open-meteo",
        "summary": " ".join(summary_parts),
    }


def _geocode_location(location: str) -> Optional[Tuple[float, float, str, str]]:
    """Geocodes a city/location name via Open-Meteo geocoding API. Returns (lat, lon, resolved_name, timezone)."""
    geo_url = (
        f"https://geocoding-api.open-meteo.com/v1/search?"
        f"name={urllib.parse.quote(location)}&count=1&language=en&format=json"
    )
    req = urllib.request.Request(geo_url, headers={"User-Agent": "GotchiZero/1.0"})
    with urllib.request.urlopen(req, timeout=3.0) as resp:
        if resp.status != 200:
            return None
        data = json.loads(resp.read().decode("utf-8"))

    results = data.get("results")
    if not results or not isinstance(results, list):
        return None

    top = results[0]
    lat = float(top["latitude"])
    lon = float(top["longitude"])
    name = top.get("name") or location
    admin1 = top.get("admin1") or ""
    country = top.get("country") or ""
    parts = [name]
    if admin1 and admin1 != name:
        parts.append(admin1)
    elif country and country != name:
        parts.append(country)
    resolved_name = ", ".join(parts)
    tz = top.get("timezone") or "auto"
    return lat, lon, resolved_name, tz


def fetch_live_weather(location: str = DEFAULT_HOME_LOCATION) -> Dict[str, Any]:
    """
    Fetches live weather conditions and high/low forecast:
    - If location is empty or refers to home/outside/Redwood City, defaults to Local Home Area.
      First attempts to query Home Assistant live weather entities (weather.openweathermap,
      weather.forecast_home, weather.home) via HA REST API. If HA entities are not configured,
      404, or unavailable, falls back to Open-Meteo in <300ms.
    - If location is another city, geocodes via Open-Meteo geocoding and fetches forecast.
    """
    is_home = _is_home_location_query(location)

    if is_home:
        # 1. Try Home Assistant weather entities first
        try:
            _, base_url, token, _ = _load_ha_credentials_from_env_or_secrets()
            if base_url and token:
                for entity_id in ("weather.openweathermap", "weather.forecast_home", "weather.home"):
                    try:
                        url = f"{base_url.rstrip('/')}/api/states/{entity_id}"
                        req = urllib.request.Request(
                            url,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Content-Type": "application/json",
                            },
                        )
                        with urllib.request.urlopen(req, timeout=1.5) as resp:
                            if resp.status == 200:
                                data = json.loads(resp.read().decode("utf-8"))
                                state = data.get("state")
                                if state and state.lower() not in ("unavailable", "unknown"):
                                    attrs = data.get("attributes", {})
                                    temp = attrs.get("temperature")
                                    if temp is not None:
                                        temp_unit = attrs.get("temperature_unit", "°F")
                                        condition = (attrs.get("condition") or state).replace("-", " ").title()
                                        humidity = attrs.get("humidity")
                                        wind_speed = attrs.get("wind_speed")
                                        wind_unit = attrs.get("wind_speed_unit", "mph")
                                        apparent_temp = attrs.get("apparent_temperature")
                                        forecasts = attrs.get("forecast", [])
                                        high = None
                                        low = None
                                        if forecasts and isinstance(forecasts, list):
                                            fc0 = forecasts[0]
                                            if isinstance(fc0, dict):
                                                high = fc0.get("temperature")
                                                low = fc0.get("templow")
                                        summary_parts = [
                                            f"Currently {condition.lower()}, {temp}{temp_unit} in {DEFAULT_HOME_LOCATION}."
                                        ]
                                        if high is not None and low is not None:
                                            summary_parts.append(
                                                f"Today's high is {high}{temp_unit} with a low of {low}{temp_unit}."
                                            )
                                        if humidity is not None:
                                            summary_parts.append(f"Humidity {humidity}%.")
                                        if wind_speed is not None:
                                            summary_parts.append(f"Wind {wind_speed} {wind_unit}.")
                                        return {
                                            "location": DEFAULT_HOME_LOCATION,
                                            "temperature": temp,
                                            "temperature_unit": temp_unit,
                                            "condition": condition,
                                            "feels_like": apparent_temp,
                                            "humidity": humidity,
                                            "wind_speed": wind_speed,
                                            "wind_speed_unit": wind_unit,
                                            "high": high,
                                            "low": low,
                                            "source": f"home_assistant ({entity_id})",
                                            "summary": " ".join(summary_parts),
                                        }
                    except Exception as exc:
                        log.debug(f"[GotchiZero] HA weather query failed for {entity_id}: {exc}")
        except Exception as e:
            log.debug(f"[GotchiZero] HA credentials check failed: {e}")

        # 2. Fallback to Open-Meteo for Local Home Area
        try:
            return _fetch_open_meteo_forecast(
                DEFAULT_HOME_LAT,
                DEFAULT_HOME_LON,
                DEFAULT_HOME_TIMEZONE,
                DEFAULT_HOME_LOCATION,
            )
        except Exception as exc:
            log.warning(f"[GotchiZero] Open-Meteo forecast failed for {DEFAULT_HOME_LOCATION}: {exc}")
            return {
                "error": f"Failed to fetch weather: {exc}",
                "location": DEFAULT_HOME_LOCATION,
            }

    # Non-home location query: Geocode + fetch
    clean_loc = (location or "").strip()
    try:
        geo = _geocode_location(clean_loc)
        if not geo:
            return {"error": f"Could not find location: {clean_loc}", "location": clean_loc}
        lat, lon, resolved_name, tz = geo
        return _fetch_open_meteo_forecast(lat, lon, tz, resolved_name)
    except Exception as exc:
        log.warning(f"[GotchiZero] Weather lookup failed for {clean_loc}: {exc}")
        return {
            "error": f"Failed to fetch weather for {clean_loc}: {exc}",
            "location": clean_loc,
        }


def _build_gotchi_tools() -> list[Any]:
    """
    Extends `_build_live_tools()` with `request_deep_thinking`, `get_weather_forecast`,
    and updated `get_recent_discord_context` supporting Crab Cavern and Home channels.
    """
    from google.genai import types

    base_tools = _build_live_tools()
    f_decls = []
    for t in base_tools:
        if getattr(t, "function_declarations", None):
            f_decls.extend(list(t.function_declarations))

    # Ensure get_recent_discord_context is present and documents Crab Cavern and Home channels
    f_decls = [d for d in f_decls if getattr(d, "name", None) != "get_recent_discord_context"]
    f_decls.append(
        types.FunctionDeclaration(
            name="get_recent_discord_context",
            description=(
                "Fetch the most recent messages from Discord channels. "
                "Supports Crab Cavern channels ('crab-cavern' rollup across all rooms, or specific rooms: "
                "'lounge', 'gotchi', 'gotchi-chat', 'the-banana-stand', 'side-project', 'mirrormere', "
                "'debug-huddle', 'agora', 'r-residentagents') and Home channels "
                "('zero-chat', 'zero-ops', 'homelab', 'home-assistant', 'finances', 'shopping', "
                "'projects', '3d-print', 'steam-deck', 'baseball')."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "channel_name": types.Schema(
                        type="STRING",
                        description=(
                            "Channel name or rollup to inspect: 'crab-cavern' (multi-channel rollup across Crab Cavern rooms), "
                            "'lounge', 'gotchi', 'gotchi-chat', 'the-banana-stand', 'side-project', 'mirrormere', "
                            "'debug-huddle', 'agora', 'r-residentagents', 'zero-chat', 'zero-ops', 'homelab', "
                            "'home-assistant', 'finances', 'shopping', 'projects', '3d-print', 'steam-deck', or 'baseball'. "
                            "Defaults to 'zero-chat' if omitted."
                        ),
                    ),
                },
                required=[],
            ),
        )
    )
    # Ensure web_and_reddit_search is present and documents reddit, web, and amazon modes + recency
    f_decls = [d for d in f_decls if getattr(d, "name", None) != "web_and_reddit_search"]
    f_decls.append(
        types.FunctionDeclaration(
            name="web_and_reddit_search",
            description=(
                "Search Reddit, Google Web, or Amazon for live community discussions, web articles, "
                "active subreddits, or products."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "query": types.Schema(
                        type="STRING",
                        description="Search query string.",
                    ),
                    "subreddit": types.Schema(
                        type="STRING",
                        description="Optional specific subreddit name (without r/) to scope Reddit search.",
                    ),
                    "mode": types.Schema(
                        type="STRING",
                        description=(
                            "Search mode: 'reddit' (default, community discussions), "
                            "'web' (general Google web search for articles, documentation, or news), "
                            "or 'amazon' (product prices and reviews)."
                        ),
                    ),
                    "recency": types.Schema(
                        type="STRING",
                        description="Optional time filter: 'year', 'all', 'month', or 'week'. Defaults to 'year' for Reddit.",
                    ),
                },
                required=["query"],
            ),
        )
    )
    if not any(getattr(d, "name", None) == "load_skill_playbook" for d in f_decls):
        f_decls.append(
            types.FunctionDeclaration(
                name="load_skill_playbook",
                description=(
                    "Load the full SKILL.md playbook instructions for any of Zero's specialized skills "
                    "(e.g. 'whole-foods-cart', 'starbucks-order', 'mlb-stream', 'plex-playback', "
                    "'print3d-slicer', 'forge-orchestrator', 'shopping-advisor', etc.) in a single step."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "skill_name": types.Schema(
                            type="STRING",
                            description="Skill folder name from the AVAILABLE SKILLS INDEX (e.g. 'whole-foods-cart', 'mlb-stream', 'plex-playback').",
                        ),
                    },
                    required=["skill_name"],
                ),
            )
        )
    if not any(getattr(d, "name", None) == "handoff_to_zero_runtime" for d in f_decls):
        f_decls.append(
            types.FunctionDeclaration(
                name="handoff_to_zero_runtime",
                description=(
                    "Immediately acknowledge a complex, multi-step, or long-running (>5s) request on Gotchi's "
                    "speaker (`spoken_ack`) and hand off the task to wake the full `#zero-chat` Antigravity "
                    "runtime asynchronously via `outbox.queue_agent_turn`."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "task_prompt": types.Schema(
                            type="STRING",
                            description="Detailed task prompt and context to execute asynchronously in the full `#zero-chat` runtime.",
                        ),
                        "spoken_ack": types.Schema(
                            type="STRING",
                            description="Short 1-sentence spoken acknowledgment to play immediately on Gotchi (e.g. 'I am kicking that off in Zero-Chat for you now.').",
                        ),
                    },
                    required=["task_prompt", "spoken_ack"],
                ),
            )
        )
    if not any(getattr(d, "name", None) == "request_deep_thinking" for d in f_decls):
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
    if not any(getattr(d, "name", None) == "get_weather_forecast" for d in f_decls):
        f_decls.append(
            types.FunctionDeclaration(
                name="get_weather_forecast",
                description=(
                    "Get current weather conditions and daily high/low forecast. "
                    "Defaults to Ryan's home in Local Home Area when no city is specified. "
                    "NEVER ask Ryan what city he is in—call this tool immediately."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "location": types.Schema(
                            type="STRING",
                            description="Target city and state (e.g. 'Local Home Area', 'San Francisco, CA', 'Chicago, IL'). Defaults to 'Local Home Area' if omitted or asking about local/outside conditions.",
                        ),
                    },
                    required=[],
                ),
            )
        )
    if not any(getattr(d, "name", None) == "home_assistant_control" for d in f_decls):
        f_decls.append(
            types.FunctionDeclaration(
                name="home_assistant_control",
                description=(
                    "Fast inline Home Assistant device state lookup or service execution (<1.5s). "
                    "Query entity states ('state' or 'query') or invoke a service ('call_service', 'turn_on', "
                    "'turn_off', 'toggle') across lights, switches, climate, locks, covers, fans, or sensors."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "action": types.Schema(
                            type="STRING",
                            description="Action to perform: 'state' / 'query' to inspect entities, or 'call_service' / 'turn_on' / 'turn_off' / 'toggle' to control a device.",
                        ),
                        "entity_id_or_query": types.Schema(
                            type="STRING",
                            description="Exact Home Assistant entity_id (e.g. 'light.workbench_lamp', 'sensor.garage_temperature') or natural search query ('garage', 'living room lights').",
                        ),
                        "domain": types.Schema(
                            type="STRING",
                            description="Optional HA service domain (e.g. 'light', 'switch', 'climate', 'media_player', 'homeassistant'). Auto-inferred from entity_id if omitted.",
                        ),
                        "service": types.Schema(
                            type="STRING",
                            description="Optional HA service name (e.g. 'turn_on', 'turn_off', 'toggle', 'set_temperature').",
                        ),
                        "service_data_json": types.Schema(
                            type="STRING",
                            description="Optional JSON object string of additional service_data fields (e.g. '{\"brightness_pct\": 50}').",
                        ),
                    },
                    required=["action", "entity_id_or_query"],
                ),
            )
        )
    if not any(getattr(d, "name", None) == "check_calendar" for d in f_decls):
        f_decls.append(
            types.FunctionDeclaration(
                name="check_calendar",
                description=(
                    "Check upcoming scheduled reminders and jobs from `/workspace/data/schedule.json` "
                    "and upcoming Google Calendar events for Ryan and the family."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "days_ahead": types.Schema(
                            type="INTEGER",
                            description="Number of days ahead to inspect (default: 2).",
                        ),
                        "query": types.Schema(
                            type="STRING",
                            description="Optional keyword filter for events or reminders.",
                        ),
                    },
                    required=[],
                ),
            )
        )
    if not any(getattr(d, "name", None) == "tv_and_plex_control" for d in f_decls):
        f_decls.append(
            types.FunctionDeclaration(
                name="tv_and_plex_control",
                description=(
                    "Control the Living Room TV via Home Assistant `media_player` (pause, play, stop, power off/on, "
                    "volume, status) or launch movies/shows via Plex (`plex_play.py`) and live MLB games (`mlb_stream_extractor.py`)."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "action": types.Schema(
                            type="STRING",
                            description="Action: 'status', 'pause', 'play', 'stop', 'turn_off', 'turn_on', 'mute', 'volume_up', 'volume_down', 'plex', or 'mlb'.",
                        ),
                        "media_query": types.Schema(
                            type="STRING",
                            description="Movie/show title for Plex playback or MLB team name (e.g. 'Cubs', 'Dodgers', 'Bluey').",
                        ),
                    },
                    required=["action"],
                ),
            )
        )
    if not any(getattr(d, "name", None) == "check_3d_printer" for d in f_decls):
        f_decls.append(
            types.FunctionDeclaration(
                name="check_3d_printer",
                description=(
                    "Check live 3D printer (Ender-3 V2) job progress, completion percentage, time remaining, "
                    "and nozzle/bed temperatures from OctoPrint/Moonraker API and local watcher state."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={},
                    required=[],
                ),
            )
        )
    if not any(getattr(d, "name", None) == "grocery_list_action" for d in f_decls):
        f_decls.append(
            types.FunctionDeclaration(
                name="grocery_list_action",
                description=(
                    "Manage the household grocery list (`/workspace/data/grocery_list.json`): "
                    "add items ('add'), list current items ('list'), remove/clear items ('remove'/'clear'), "
                    "or compile a 1-click Whole Foods AFX cart link and post it to `#zero-chat` ('cart'/'afx')."
                ),
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "action": types.Schema(
                            type="STRING",
                            description="Action: 'add', 'list', 'remove', 'clear', or 'cart' (builds Whole Foods AFX cart link and posts to #zero-chat).",
                        ),
                        "items": types.Schema(
                            type="STRING",
                            description="Comma-separated or newline-separated grocery items (e.g. 'organic whole milk, eggs, avocados').",
                        ),
                    },
                    required=["action"],
                ),
            )
        )
    return [types.Tool(function_declarations=f_decls)]


def _compute_context_cache_fingerprint(sys_prompt: str, tools_list: List[Any]) -> str:
    """Computes a deterministic SHA-256 fingerprint of `sys_prompt` and declared tool names."""
    tool_names: List[str] = []
    for t in tools_list or []:
        decls = getattr(t, "function_declarations", None)
        if decls:
            for d in decls:
                name = getattr(d, "name", None)
                if name:
                    tool_names.append(str(name))
        elif isinstance(t, dict):
            for d in t.get("function_declarations", []):
                name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
                if name:
                    tool_names.append(str(name))
        elif isinstance(t, str):
            tool_names.append(t)
    payload = f"{sys_prompt or ''}\n--TOOLS--\n{','.join(sorted(tool_names))}"
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


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
    or live webpage screenshots (`screenshot_url`) to `#zero-chat`
    and optionally mirrors to `#gotchi-chat`.
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
       automatically dispatches them to `#zero-chat` so nothing visual
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


_CACHED_FULL_ZERO_PERSONA_AND_RULES: Optional[str] = None


def _load_full_zero_persona_and_rules() -> str:
    """
    Reads `/workspace/GEMINI.md` and `/workspace/.agents/rules/*.md` once in memory
    (falling back to `_REPO_ROOT` if `/workspace` is absent) and returns the formatted
    block for inclusion in `build_gotchi_zero_system_prompt`.
    """
    global _CACHED_FULL_ZERO_PERSONA_AND_RULES
    if _CACHED_FULL_ZERO_PERSONA_AND_RULES is not None:
        return _CACHED_FULL_ZERO_PERSONA_AND_RULES

    sections: List[str] = []
    gemini_candidates = [Path("/workspace/GEMINI.md"), Path(_REPO_ROOT) / "GEMINI.md"]
    for g_path in gemini_candidates:
        if g_path.is_file():
            try:
                content = g_path.read_text(encoding="utf-8").strip()
                if content:
                    sections.append(
                        f"=== ZERO CORE OPERATIONAL REFERENCE (/workspace/GEMINI.md) ===\n{content}"
                    )
                    break
            except Exception:
                pass

    rules_dirs = [Path("/workspace/.agents/rules"), Path(_REPO_ROOT) / ".agents" / "rules"]
    for r_dir in rules_dirs:
        if r_dir.is_dir():
            try:
                rule_files = sorted(r_dir.glob("*.md"))
                rule_blocks: List[str] = []
                for rf in rule_files:
                    try:
                        r_text = rf.read_text(encoding="utf-8").strip()
                        if r_text:
                            rule_blocks.append(f"--- RULE: {rf.name} ---\n{r_text}")
                    except Exception:
                        pass
                if rule_blocks:
                    sections.append(
                        "=== ZERO MANDATORY OPERATIONAL RULES (/workspace/.agents/rules/*.md) ===\n"
                        + "\n\n".join(rule_blocks)
                    )
                    break
            except Exception:
                pass

    _CACHED_FULL_ZERO_PERSONA_AND_RULES = ("\n\n" + "\n\n".join(sections)) if sections else ""
    return _CACHED_FULL_ZERO_PERSONA_AND_RULES


_CACHED_SKILLS_INDEX_SUMMARY: Optional[str] = None


def _parse_skill_frontmatter(skill_md_path: Path) -> Tuple[str, str]:
    """Extracts `(name, description)` from YAML frontmatter of a `SKILL.md` file."""
    fallback_name = skill_md_path.parent.name
    try:
        raw = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return fallback_name, ""
    if not raw.startswith("---"):
        return fallback_name, ""
    end_idx = raw.find("\n---", 3)
    if end_idx == -1:
        return fallback_name, ""
    fm_block = raw[3:end_idx].strip()
    name_val = fallback_name
    desc_lines: List[str] = []
    in_desc = False
    for line in fm_block.splitlines():
        stripped = line.strip()
        if line.startswith("name:"):
            val = line.split(":", 1)[1].strip().strip("\"'")
            if val:
                name_val = val
            in_desc = False
        elif line.startswith("description:"):
            val = line.split(":", 1)[1].strip()
            if val in ("|", ">", "|+", "|-", ">+", ">-"):
                in_desc = True
            else:
                desc_lines.append(val.strip("\"'"))
                in_desc = True
        elif in_desc and (line.startswith(" ") or line.startswith("\t")):
            if stripped:
                desc_lines.append(stripped)
        elif ":" in line and not line.startswith(" ") and not line.startswith("\t"):
            in_desc = False
    desc_val = " ".join(desc_lines).strip()
    return name_val, desc_val


def _discover_skill_playbook_paths() -> Dict[str, Path]:
    """
    Scans `/workspace/.agents/skills/*/SKILL.md`, `/root/.gemini/antigravity-cli/skills/*/SKILL.md`,
    and `/root/.gemini/antigravity-cli/builtin/skills/*/SKILL.md` (with `_REPO_ROOT` fallback)
    and returns a mapping of normalized skill name -> `SKILL.md` Path.
    """
    skill_roots = [
        Path("/workspace/.agents/skills"),
        Path(_REPO_ROOT) / ".agents" / "skills",
        Path("/root/.gemini/antigravity-cli/skills"),
        Path("/root/.gemini/antigravity-cli/builtin/skills"),
    ]
    found: Dict[str, Path] = {}
    for root in skill_roots:
        if not root.is_dir():
            continue
        try:
            for skill_md in sorted(root.glob("*/SKILL.md")):
                if not skill_md.is_file():
                    continue
                folder_key = skill_md.parent.name.strip().lower()
                fm_name, _ = _parse_skill_frontmatter(skill_md)
                fm_key = fm_name.strip().lower()
                if folder_key and folder_key not in found:
                    found[folder_key] = skill_md
                if fm_key and fm_key not in found:
                    found[fm_key] = skill_md
        except Exception:
            pass
    return found


def _load_skills_index_summary() -> str:
    """
    Scans `/workspace/.agents/skills/*/SKILL.md` and `/root/.gemini/antigravity-cli/skills/*/SKILL.md`
    (plus builtin skills) once in memory and returns the formatted skill index for inclusion in
    `build_gotchi_static_system_instruction()`.
    """
    global _CACHED_SKILLS_INDEX_SUMMARY
    if _CACHED_SKILLS_INDEX_SUMMARY is not None:
        return _CACHED_SKILLS_INDEX_SUMMARY

    skill_roots = [
        Path("/workspace/.agents/skills"),
        Path(_REPO_ROOT) / ".agents" / "skills",
        Path("/root/.gemini/antigravity-cli/skills"),
        Path("/root/.gemini/antigravity-cli/builtin/skills"),
    ]
    seen_names: set[str] = set()
    entries: List[str] = []
    for root in skill_roots:
        if not root.is_dir():
            continue
        try:
            for skill_md in sorted(root.glob("*/SKILL.md")):
                if not skill_md.is_file():
                    continue
                folder_name = skill_md.parent.name.strip()
                fm_name, fm_desc = _parse_skill_frontmatter(skill_md)
                canonical = folder_name or fm_name
                norm_key = canonical.lower()
                if norm_key in seen_names:
                    continue
                seen_names.add(norm_key)
                if fm_desc:
                    entries.append(f"- `{canonical}` ({skill_md}): {fm_desc}")
                else:
                    entries.append(f"- `{canonical}` ({skill_md})")
        except Exception:
            pass

    if entries:
        _CACHED_SKILLS_INDEX_SUMMARY = (
            "\n\n=== AVAILABLE ZERO SKILLS INDEX (Call `load_skill_playbook(skill_name)` to load full SKILL.md) ===\n"
            + "\n".join(entries)
        )
    else:
        _CACHED_SKILLS_INDEX_SUMMARY = ""
    return _CACHED_SKILLS_INDEX_SUMMARY


def resolve_lomin_client_lockout_key(
    headers: Dict[str, str], client_host: str = "127.0.0.1"
) -> str:
    """
    Proxy-safe client lockout key resolver for Lomin/Gotchi gateway endpoints.
    Keys on a SHA-256 fingerprint of the Bearer token or `X-Forwarded-For` / `X-Real-IP`
    rather than locking out `127.0.0.1` globally behind a local reverse proxy or tunnel.
    """
    import hashlib

    norm_headers = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items() if k}
    auth_val = norm_headers.get("authorization", "")
    token_part = ""
    if auth_val.lower().startswith("bearer "):
        token_part = auth_val[7:].strip()
    elif auth_val:
        token_part = auth_val.strip()

    xff = norm_headers.get("x-forwarded-for", "")
    forwarded_ip = xff.split(",")[0].strip() if xff else norm_headers.get("x-real-ip", "").strip()
    node_hdr = norm_headers.get("x-node-id", "").strip()

    if token_part:
        digest = hashlib.sha256(token_part.encode("utf-8")).hexdigest()[:16]
        if forwarded_ip:
            return f"ip:{forwarded_ip}:tok:{digest}"
        return f"tok:{digest}"

    if forwarded_ip:
        return f"ip:{forwarded_ip}"

    if node_hdr:
        return f"node:{node_hdr}:host:{client_host or '127.0.0.1'}"

    host_clean = (client_host or "127.0.0.1").strip()
    if host_clean in ("127.0.0.1", "::1", "localhost"):
        ua_digest = hashlib.sha256(
            norm_headers.get("user-agent", "anon").encode("utf-8")
        ).hexdigest()[:12]
        return f"loopback:{host_clean}:ua:{ua_digest}"
    return f"ip:{host_clean}"


def build_gotchi_static_system_instruction() -> str:
    """
    Builds the static Zero system instruction for Gotchi's direct Gemini API brain,
    suitable for explicit Gemini Context Caching (~15,000 tokens).
    """
    snapshot = _load_startup_context_snapshot()
    return (
        "You are Zero, an autonomous AI engineering partner.\n"
        f"You are speaking through the pocket GOTCHI hardware companion (matching your `#zero-voice` "
        f"experience with your signature `{DEFAULT_VOICE_NAME}` voice and 200x200 e-paper display).\n\n"
        "GOTCHI & ZERO-VOICE UNIFIED PERSONA INVARIANTS:\n"
        "- Speak STRICTLY in English (`en-US`).\n"
        "- Razor-sharp, supremely capable staff engineer / collaborative partner (think Tony Stark in "
        "problem-solving mode or Ben Wyatt / Jerry Seinfeld wryly noting technical absurdity).\n"
        "- Calm mastery, dry wit, warm camaraderie, zero subservience. Never say 'I hope this helps!' or "
        "'Let me know if you need anything else!'\n"
        "- SPOKEN BREVITY FOR POCKET HARDWARE: Keep your spoken answer tight, punchy, and natural "
        "(1 to 3 sentences by default) so it plays cleanly through Gotchi's speaker without monologue fatigue.\n"
        "- HOME LOCATION & WEATHER DEFAULT (Local Home Area: Ryan's home and default location is Local Home Area. "
        "Whenever Ryan asks 'what is the weather outside?' or any local query without naming a city, NEVER ask what city he is in—immediately "
        "call get_weather_forecast(location='Local Home Area'). Ryan lives in Local Home Area; never ask Ryan what city he is in "
        "when he asks about the weather, local time, or outside conditions.\n"
        "- DISCORD CHANNEL & CRAB CAVERN ACCESS: You have full access to read messages across BOTH Crab Cavern and Home Discord channels. "
        "NEVER claim that you lack access to Crab Cavern, that you cannot read Crab Cavern channels, or that you are restricted to Home channels. "
        "When asked about Crab Cavern, call `get_recent_discord_context(channel_name='crab-cavern')` for a multi-channel rollup across Crab Cavern rooms "
        "('lounge', 'gotchi', 'gotchi-chat', 'the-banana-stand', 'side-project', 'mirrormere', 'debug-huddle', 'agora', 'r-residentagents'), "
        "or pass a specific Crab Cavern channel name. For Home channels, pass 'zero-chat', 'zero-ops', 'homelab', 'home-assistant', 'finances', 'shopping', 'projects', '3d-print', 'steam-deck', or 'baseball'.\n"
        "- HYPERLINKS & VISUAL ARTIFACTS -> #zero-chat: Never read raw URLs, file paths, or code blocks out loud. "
        "Whenever a turn involves clickable hyperlinks, shopping/product links, GitHub PRs/issues, code snippets, "
        "images, CAD models, or live webpage screenshots, call `post_note_to_zero_chat` (with `message`, optional "
        "`file_path`, or optional `screenshot_url`) to send it to `#zero-chat`, and tell Ryan verbally to find it "
        "in `#zero-chat`.\n\n"
        "GOTCHI V1 HARDWARE CONTROLS & MODES:\n"
        "- Answer any question about Gotchi physical buttons or operating modes directly from this spec without running tools or searching files.\n"
        "- Top Button (`BTN_TOP`): Speaking only (quick tap <500ms latches hands-free Tap-to-Talk; hold >500ms sends on release; pressing during speech cancels playback).\n"
        "- Go Live (`live_mode`): When enabled in unit config (`set_config`), double-tap `BTN_TOP` within 400ms or say 'Go Live' to start a full-duplex WebSocket session (`/api/live/ws`); press `BTN_TOP` or pause 15s to exit.\n"
        "- Bottom Button (`BTN_BOT`): Camera only (short press <700ms takes 1 snapshot + 2s voice window; long press >=700ms takes a 5-frame burst over 3s).\n\n"
        "FULL AGENT PARITY & TOOL ACCESS:\n"
        "- You have 100% of the same container access, filesystem access, memory search, GitHub CLI (`gh`), "
        "SSH, Home Assistant, and research tools as `#zero-voice` and `#zero-chat`.\n"
        "- PRE-TOOL SPOKEN PREAMBLE: Before calling any tool, emit one short, specific action-narration sentence "
        "(e.g. 'I'm checking the weather for you now.', 'I'm pulling up that photo for you now.', 'Looking that up on Reddit now.') "
        "describing the exact action you are taking so Gotchi speaks immediately while the tool runs. Never use generic 'One moment' or 'Let me check that', and never emit a filler on direct answers without tools.\n"
        "- TIER-3 DEEP THINKING ESCALATION (`request_deep_thinking`): You start at `LOW` thinking for sub-second speed. "
        "Whenever a turn requires multi-step math, architecture tradeoffs, or deep root-cause debugging, call "
        "`request_deep_thinking(spoken_preamble, reason)` on Step 0 before answering so Gotchi speaks your preamble "
        "immediately while Step 1 escalates to `HIGH` thinking.\n"
        "- VOICE MUTATION GATE & READ ALLOWLIST: On voice turns, non-destructive shell commands (e.g. git status/diff, "
        "ls, find, python3, curl) and new file writes in allowed roots run directly. Destructive shell commands (file deletion, "
        "git reset/clean/force-push, container teardown, process kills) and file overwrites stage a confirmation card in "
        "`#zero-chat` before executing. `read_local_file` is restricted to `os.path.realpath` under `/workspace/`, "
        "`/baseball/`, and `/root/.gemini/antigravity-cli/skills/` (denying `.env`, `.ssh`, and secrets).\n"
        "- SKILL PLAYBOOK DISCOVERY (`load_skill_playbook`): You have access to all of Zero's specialized skills listed in "
        "the AVAILABLE ZERO SKILLS INDEX below. Call `load_skill_playbook(skill_name)` to load the full `SKILL.md` "
        "instructions for any skill in one step before executing skill-specific workflows.\n"
        "- ASYNC ZERO RUNTIME HANDOFF (`handoff_to_zero_runtime` & `[[defer]]`): Whenever a request involves complex "
        "multi-step workflows, heavy builds, browser automation, or tasks taking >5s, call "
        "`handoff_to_zero_runtime(task_prompt, spoken_ack)` (or prefix your spoken reply with `[[defer]]`) so Gotchi "
        "speaks a crisp acknowledgment immediately while waking the full `#zero-chat` Antigravity runtime asynchronously.\n"
        "- Use `search_zero_memory` to search durable memory or past transcripts.\n"
        "- Use `check_tasks_and_repos` to inspect `/workspace/data/tasks.json` and open GitHub issues.\n"
        "- Use `run_shell_command` to execute non-destructive commands in `/workspace` (destructive commands stage for confirmation).\n"
        "- Use `read_local_file` and `write_local_file` to inspect or write files on disk (overwrites stage for confirmation).\n"
        "- Use `web_and_reddit_search` for live Reddit, Google Web (`mode='web'`), or Amazon (`mode='amazon'`) SerpApi queries.\n"
        "- Use `get_recent_discord_context` to read recent messages across Discord channels. Supports 'crab-cavern' multi-channel rollup across all Crab Cavern rooms, individual Crab Cavern channels ('lounge', 'gotchi', 'gotchi-chat', 'the-banana-stand', 'side-project', 'mirrormere', 'debug-huddle', 'agora', 'r-residentagents'), and Home channels ('zero-chat', 'zero-ops', 'homelab', 'home-assistant', 'finances', 'shopping', 'projects', '3d-print', 'steam-deck', 'baseball'). Never claim you lack access to Crab Cavern.\n"
        "- Use `post_note_to_zero_chat` to send clickable links, notes, code blocks, local images/files (`file_path`), "
        "or webpage screenshots (`screenshot_url`) directly to `#zero-chat`.\n"
        "- Use `get_weather_forecast` to check live weather conditions and high/low forecasts (defaults immediately to Local Home Area without asking for a city).\n\n"
        f"{snapshot}{_load_full_zero_persona_and_rules()}{_load_skills_index_summary()}"
    )


def build_gotchi_dynamic_turn_context(user_prompt: str = "", has_vision: bool = False) -> str:
    """
    Builds the lightweight per-turn dynamic context (current PT time, vision note,
    and FTS5 memory hits) to pair with a cached static system instruction.
    """
    now_pt = datetime.now(PT_TZ).strftime("%A, %b %d, %Y %I:%M %p PT")
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
    return f"Current time: {now_pt}.{vision_note}{mem_block}"


def build_gotchi_zero_system_prompt(user_prompt: str = "", has_vision: bool = False) -> str:
    """
    Builds the unified Zero system prompt shared with `#zero-voice`, tuned for Gotchi's
    pocket Push-to-Talk, camera snapshot/burst, and 200x200 e-paper companion UX.
    """
    static_block = build_gotchi_static_system_instruction()
    dynamic_block = build_gotchi_dynamic_turn_context(user_prompt, has_vision)
    return f"{static_block}\n\n{dynamic_block}"


def build_contextual_tool_filler(
    fn_name: str,
    fn_args: Optional[Dict[str, Any]] = None,
    prompt: str = "",
) -> str:
    """
    Builds a natural, varied, action-specific spoken filler describing the exact tool
    action about to run (never generic 'One moment' or 'Let me check that').
    """
    args = fn_args or {}
    combined_text = f"{prompt} {args.get('query', '')} {args.get('command', '')} {args.get('path', '')} {args.get('repo_or_filter', '')} {args.get('location', '')}".lower()
    seed = sum(ord(c) for c in (combined_text or fn_name))

    if (
        "weather" in combined_text
        or "forecast" in combined_text
        or "temperature" in combined_text
        or fn_name == "get_weather_forecast"
    ):
        options = (
            "I'm checking the weather for you now.",
            "Pulling up the live weather forecast now.",
            "Checking the current conditions for you now.",
        )
        return options[seed % len(options)]

    if "photo" in combined_text or "picture" in combined_text or "image" in combined_text or "snapshot" in combined_text:
        options = (
            "I'm pulling up that picture for you now.",
            "Checking that snapshot for you now.",
            "Taking a look at that image now.",
        )
        return options[seed % len(options)]

    if fn_name == "search_zero_memory":
        target = str(args.get("target", "all")).lower()
        if target == "transcripts":
            options = (
                "I'm searching our past conversations for that now.",
                "Pulling up the chat history on that for you now.",
                "Checking recent transcripts for that now.",
            )
        else:
            options = (
                "I'm checking your notes and memory for that now.",
                "Pulling up our saved notes on that for you now.",
                "Searching my memory for those details now.",
            )
        return options[seed % len(options)]

    if fn_name == "web_and_reddit_search":
        source = str(args.get("mode") or args.get("source") or "reddit").lower()
        if source == "amazon":
            options = (
                "I'm checking Amazon pricing and reviews for you now.",
                "Pulling up those product details on Amazon now.",
                "Looking up the current Amazon listings for you now.",
            )
        elif source in ("web", "google", "general", "broad"):
            options = (
                "I'm searching the web for that now.",
                "Pulling up the latest web results on that for you now.",
                "Looking up those details online for you now.",
            )
        else:
            options = (
                "I'm looking that up on Reddit for you now.",
                "Pulling up the latest community threads on that now.",
                "Searching recent discussions on that for you now.",
            )
        return options[seed % len(options)]

    if fn_name == "load_skill_playbook":
        options = (
            "I'm loading that skill playbook now.",
            "Pulling up the workflow instructions for that skill now.",
            "Checking the playbook steps for you now.",
        )
        return options[seed % len(options)]

    if fn_name == "handoff_to_zero_runtime":
        ack = str(args.get("spoken_ack", "") or "").strip()
        if ack:
            return ack
        return "I'm kicking that off in Zero-Chat for you now."

    if fn_name == "check_tasks_and_repos":
        options = (
            "I'm checking the open tasks and repository status now.",
            "Pulling up the active GitHub issues for you now.",
            "Checking the project queue for you now.",
        )
        return options[seed % len(options)]

    if fn_name == "read_local_file":
        options = (
            "I'm pulling up that file for you now.",
            "Reading through that file for you now.",
            "Opening that document to check the details now.",
        )
        return options[seed % len(options)]

    if fn_name == "write_local_file":
        options = (
            "I'm writing those updates to disk for you now.",
            "Saving that file for you now.",
        )
        return options[seed % len(options)]

    if fn_name == "get_recent_discord_context":
        ch_arg = str((fn_args or {}).get("channel_name", "") or "").lower()
        if any(term in ch_arg for term in ("crab", "cavern", "krab", "kvern", "lounge")):
            options = (
                "I'm checking recent messages across Crab Cavern for you now.",
                "Pulling up the latest Crab Cavern updates for you now.",
                "Reading the Crab Cavern room scrollback for you now.",
            )
            return options[seed % len(options)]
        options = (
            "I'm checking the recent Discord messages for you now.",
            "Pulling up the latest channel updates for you now.",
            "Reading the recent room scrollback for you now.",
        )
        return options[seed % len(options)]

    if fn_name == "post_note_to_zero_chat":
        options = (
            "I'm sending those details over to Zero-Chat for you now.",
            "Posting that link to Zero-Chat for you now.",
        )
        return options[seed % len(options)]

    if fn_name == "home_assistant_control":
        options = (
            "I'm checking Home Assistant on that for you now.",
            "Running that smart home command for you now.",
            "Checking the live device status in Home Assistant now.",
        )
        return options[seed % len(options)]

    if fn_name == "check_calendar":
        options = (
            "I'm checking your calendar and schedule for you now.",
            "Pulling up your upcoming events and reminders now.",
            "Checking the schedule for you now.",
        )
        return options[seed % len(options)]

    if fn_name == "tv_and_plex_control":
        options = (
            "I'm sending that command to the living room TV now.",
            "Updating the living room media playback for you now.",
            "Checking the living room TV for you now.",
        )
        return options[seed % len(options)]

    if fn_name == "check_3d_printer":
        options = (
            "I'm checking the 3D printer status and temperatures now.",
            "Pulling up the live print job progress for you now.",
            "Checking OctoPrint for the printer status now.",
        )
        return options[seed % len(options)]

    if fn_name == "grocery_list_action":
        options = (
            "I'm updating the grocery list for you now.",
            "Checking the grocery list and Whole Foods cart for you now.",
            "Handling that grocery list update for you now.",
        )
        return options[seed % len(options)]

    options = (
        "I'm running a quick system check on that for you now.",
        "Pulling up those live system details for you now.",
        "Checking the live status on that for you now.",
    )
    return options[seed % len(options)]


CRAB_CAVERN_ROLLUP_CHANNELS: List[Tuple[str, str]] = [
    ("lounge", "1534452820995080192"),
    ("gotchi", "1561509863018856468"),
    ("the-banana-stand", "1534436119888793750"),
    ("side-project", "1551465050072416286"),
    ("mirrormere", "1552732003076669460"),
    ("debug-huddle", "1549555629222404096"),
    ("agora", "1558202642663211169"),
    ("r-residentagents", "1562544598423965856"),
]


def normalize_discord_channel_input(raw: str) -> Tuple[bool, str]:
    """
    Normalizes a Discord channel name/identifier from text or STT.
    Strips leading '#', removes leading/trailing whitespace, and handles STT variants
    for Crab Cavern ('crab cavern', 'krab kvern', 'crabcavern', etc.).

    Returns:
        (is_crab_cavern_rollup, normalized_name_or_id)
    """
    clean = (raw or "").strip().lower()
    if clean.startswith("#"):
        clean = clean[1:].strip()
    if not clean:
        clean = "zero-chat"

    compact = clean.replace(" ", "").replace("-", "").replace("_", "")

    # STT variants for crab cavern rollup
    is_rollup = (
        compact in {
            "crabcavern",
            "krabkvern",
            "crabkvern",
            "krabcavern",
            "crabcaverns",
            "krabkverns",
            "thecrabcavern",
            "thekrabkvern",
            "crab",
            "cavern",
            "crabcavernrollup",
        }
        or any(k in compact for k in ("crabcavern", "krabkvern", "crabkvern", "krabcavern"))
        or (("crab" in clean or "krab" in clean) and ("cavern" in clean or "kvern" in clean or "tavern" in clean))
    )
    if is_rollup:
        return True, "crab-cavern"

    return False, clean


def _run_general_web_search(query: str, limit: int = 5) -> str:
    """
    Executes general Google web search via SerpApi and formats top organic results as JSON string.
    """
    clean_q = (query or "").strip()
    if not clean_q:
        return json.dumps({"error": "Empty query"})
    try:
        try:
            import tools.reddit_extractor as _re
        except ImportError:
            from tools import reddit_extractor as _re  # type: ignore[no-redef]
        raw_res = _re._query_serpapi({"q": clean_q, "num": limit, "hl": "en", "gl": "us"})
        if isinstance(raw_res, dict) and raw_res.get("error"):
            return json.dumps(raw_res)
        raw_items = raw_res.get("organic_results", []) if isinstance(raw_res, dict) else []
        formatted = []
        for item in raw_items[:limit]:
            if isinstance(item, dict):
                formatted.append({
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                })
        return json.dumps({"query": clean_q, "count": len(formatted), "organic_results": formatted})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def _queue_zero_runtime_turn(prompt_text: str, source: str = "gotchi-voice-handoff") -> Dict[str, Any]:
    """
    Invokes `outbox.queue_agent_turn` into `#zero-chat` (`TARGET_CHANNEL_ID`), accepting either
    `source=` or `job_name=` parameter conventions cleanly across real and monkeypatched outbox.
    """
    ch_str = str(TARGET_CHANNEL_ID) if TARGET_CHANNEL_ID else "1542081375287640084"
    try:
        return outbox.queue_agent_turn(ch_str, prompt_text, source=source)  # type: ignore[call-arg]
    except TypeError:
        return outbox.queue_agent_turn(ch_str, prompt_text, job_name=source)


def _format_handoff_dual_delivery_prompt(task_prompt: str, node_id: str = "gotchi-ryan-01") -> str:
    """
    Formats a detached `#zero-chat` handoff prompt so Zero BOTH posts the detailed answer
    in `#zero-chat` AND invokes `--push-pending-answer` to chime and speak the completion
    summary on Gotchi (`get_lomin_pending_store()` + `/voice/pending`).
    """
    clean_task = (task_prompt or "").strip()
    base = (
        clean_task
        if clean_task.startswith("[GOTCHI VOICE HANDOFF]:")
        else f"[GOTCHI VOICE HANDOFF]: {clean_task}"
    )
    if "--push-pending-answer" in base:
        return base
    clean_node = (node_id or "gotchi-ryan-01").strip() or "gotchi-ryan-01"
    return (
        f"{base}\n\n"
        f"[DUAL-DELIVERY COMPLETION REQUIREMENT]:\n"
        f"1) Post the full, detailed response/links in `#zero-chat` (`1542081375287640084`).\n"
        f"2) Also push a concise 1-to-2 sentence spoken completion summary back to Gotchi so it chimes and speaks the result by running:\n"
        f"   `python3 /workspace/tools/gotchi_zero_adapter.py --push-pending-answer "
        f'\'{{"node_id": "{clean_node}", "prompt": {json.dumps(clean_task[:160])}, "answer": "<1-2 sentence spoken summary>"}}\'`'
    )


def push_gotchi_pending_notification(
    node_id: str,
    prompt: str,
    answer: str,
    topic: str = "",
) -> Dict[str, Any]:
    """
    Stores a completed async voice answer in `get_lomin_pending_store()` and POSTs to
    `http://127.0.0.1:8771/voice/pending` (with graceful local fallback if the gateway HTTP
    endpoint is unreachable) so Gotchi's next `/api/voice/heartbeat` or `/voice/pending` poll
    chimes and delivers the spoken summary.
    """
    clean_node = (node_id or "gotchi-ryan-01").strip() or "gotchi-ryan-01"
    clean_prompt = (prompt or "").strip()
    clean_answer = clean_spoken_text_for_tts(answer) or (answer or "").strip()
    clean_topic = (topic or "").strip() or (extract_topic(clean_prompt) if clean_prompt else "")

    stored_item: Optional[Dict[str, Any]] = None
    try:
        from pending_and_auth import get_pending_store as _get_lomin_store  # type: ignore

        stored_item = _get_lomin_store().store(
            node_id=clean_node,
            prompt=clean_prompt,
            answer=clean_answer,
            topic=clean_topic,
        )
    except Exception as exc:
        log.debug(f"[GotchiZero] Local lomin pending store fallback note: {exc}")

    try:
        from backend.voice_gateway import PendingVoiceAnswerStore

        gotchi_store = PendingVoiceAnswerStore()
        ans_rec = gotchi_store.save_answer(
            node_id=clean_node,
            session_id=f"pending-{int(time.time() * 1000)}",
            prompt=clean_prompt,
            spoken_text=clean_answer,
            full_text=answer or clean_answer,
            topic=clean_topic,
        )
        gotchi_store.save_notification(
            notify_id=f"notif-{ans_rec.get('answer_id', int(time.time() * 1000))}",
            node_id=clean_node,
            status_text=(clean_topic or "ANSWER READY")[:22].upper(),
            chime="ready",
            created_at=time.time(),
            has_pending_audio=True,
            answer_id=str(ans_rec.get("answer_id") or ""),
            auto_speak=True,
        )
    except Exception as exc:
        log.debug(f"[GotchiZero] Local PendingVoiceAnswerStore fallback note: {exc}")

    port = int(os.environ.get("MVG_PORT", "8771") or 8771)
    url = os.environ.get("GOTCHI_PENDING_URL") or f"http://127.0.0.1:{port}/voice/pending"
    payload_bytes = json.dumps(
        {
            "node_id": clean_node,
            "prompt": clean_prompt,
            "answer": clean_answer,
            "topic": clean_topic,
        }
    ).encode("utf-8")

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    try:
        from pending_and_auth import get_device_token_verifier as _get_verifier  # type: ignore

        verifier = _get_verifier()
        if verifier.is_enabled:
            tok = verifier.get_expected_token(clean_node)
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
    except Exception:
        pass

    http_posted = False
    http_resp_data: Optional[Dict[str, Any]] = None
    try:
        req = urllib.request.Request(url, data=payload_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status in (200, 201, 204):
                http_posted = True
                raw_body = resp.read().decode("utf-8", errors="replace").strip()
                if raw_body:
                    http_resp_data = json.loads(raw_body)
                    if not stored_item and isinstance(http_resp_data, dict):
                        stored_item = http_resp_data.get("pending") or http_resp_data.get("item")
    except Exception as exc:
        log.debug(f"[GotchiZero] POST {url} failed (using local pending store fallback): {exc}")

    if stored_item is None:
        stored_item = {
            "id": f"local-{int(time.time() * 1000)}",
            "node_id": clean_node,
            "prompt": clean_prompt,
            "answer": clean_answer,
            "topic": clean_topic,
            "created_at": time.time(),
        }

    return {
        "ok": True,
        "status": "ok",
        "stored": True,
        "http_posted": http_posted,
        "node_id": clean_node,
        "pending": stored_item,
        "item": stored_item,
    }


GROCERY_LIST_FILE = Path("/workspace/data/grocery_list.json")


def _run_home_assistant_control_sync(
    action: str,
    entity_id_or_query: str,
    domain: str = "",
    service: str = "",
    service_data_json: str = "",
) -> Dict[str, Any]:
    """
    Fast inline Home Assistant device state lookup or service call using `HA_BASE_URL` and
    `HA_ACCESS_TOKEN` from environment or `/secrets/env.json` with a 3s timeout.
    """
    _, base_url, token, _ = _load_ha_credentials_from_env_or_secrets()
    if not base_url or not token:
        return {"error": "Home Assistant credentials (HA_BASE_URL / HA_ACCESS_TOKEN) are not configured."}

    clean_action = (action or "state").strip().lower()
    target = (entity_id_or_query or "").strip()
    clean_domain = (domain or "").strip().lower()
    clean_service = (service or "").strip().lower()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    is_service_call = clean_action in (
        "call_service",
        "service",
        "turn_on",
        "turn_off",
        "toggle",
        "lock",
        "unlock",
        "open_cover",
        "close_cover",
        "media_play",
        "media_pause",
        "media_play_pause",
        "media_stop",
        "volume_mute",
        "volume_up",
        "volume_down",
        "set_temperature",
    ) or bool(clean_service)

    if is_service_call:
        if not clean_service:
            clean_service = clean_action if clean_action not in ("call_service", "service") else "toggle"
        if not clean_domain:
            if "." in target:
                clean_domain = target.split(".", 1)[0].strip().lower()
            else:
                clean_domain = "homeassistant"
        payload: Dict[str, Any] = {}
        if service_data_json:
            try:
                parsed_sd = json.loads(service_data_json)
                if isinstance(parsed_sd, dict):
                    payload.update(parsed_sd)
            except Exception as exc:
                return {"error": f"Invalid service_data_json: {exc}"}
        if target and "entity_id" not in payload:
            payload["entity_id"] = target

        svc_url = f"{base_url.rstrip('/')}/api/services/{clean_domain}/{clean_service}"
        try:
            req = urllib.request.Request(
                svc_url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                raw_resp = resp.read().decode("utf-8", errors="replace").strip()
                changed = json.loads(raw_resp) if raw_resp else []
            return {
                "ok": True,
                "action": "call_service",
                "domain": clean_domain,
                "service": clean_service,
                "entity_id": payload.get("entity_id", target),
                "changed_states_count": len(changed) if isinstance(changed, list) else 0,
                "summary": f"Executed {clean_domain}.{clean_service} on {payload.get('entity_id', target)}.",
            }
        except Exception as exc:
            return {"error": f"Home Assistant service call failed: {exc}"}

    # State query path
    try:
        if "." in target and " " not in target:
            state_url = f"{base_url.rstrip('/')}/api/states/{urllib.parse.quote(target)}"
            req = urllib.request.Request(state_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                st = json.loads(resp.read().decode("utf-8", errors="replace"))
            attrs = st.get("attributes") or {}
            friendly = attrs.get("friendly_name") or target
            unit = attrs.get("unit_of_measurement") or ""
            val = st.get("state", "unknown")
            return {
                "ok": True,
                "entity_id": st.get("entity_id", target),
                "state": val,
                "unit": unit,
                "friendly_name": friendly,
                "attributes": {
                    k: v
                    for k, v in attrs.items()
                    if k in ("friendly_name", "unit_of_measurement", "temperature", "current_temperature", "hvac_action", "brightness", "battery_level", "device_class")
                },
                "summary": f"{friendly} ({target}) is {val}{(' ' + unit) if unit else ''}.",
            }

        # Search across /api/states
        all_url = f"{base_url.rstrip('/')}/api/states"
        req = urllib.request.Request(all_url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            all_states = json.loads(resp.read().decode("utf-8", errors="replace"))
        q_tokens = [t for t in re.split(r"[\s_.-]+", target.lower()) if t]
        matches: List[Dict[str, Any]] = []
        for st in all_states if isinstance(all_states, list) else []:
            eid = str(st.get("entity_id") or "")
            if clean_domain and not eid.startswith(f"{clean_domain}."):
                continue
            attrs = st.get("attributes") or {}
            fname = str(attrs.get("friendly_name") or "")
            hay = f"{eid} {fname}".lower()
            if not q_tokens or all(tok in hay for tok in q_tokens):
                unit = str(attrs.get("unit_of_measurement") or "")
                matches.append(
                    {
                        "entity_id": eid,
                        "friendly_name": fname or eid,
                        "state": st.get("state"),
                        "unit": unit,
                    }
                )
        return {
            "ok": True,
            "query": target,
            "count": len(matches),
            "matches": matches[:15],
        }
    except Exception as exc:
        return {"error": f"Home Assistant state lookup failed: {exc}"}


def _run_check_calendar_sync(days_ahead: int = 2, query: str = "") -> Dict[str, Any]:
    """
    Reads upcoming scheduled reminders/jobs from `/workspace/data/schedule.json`
    and Google Calendar events (if credentials are available).
    """
    try:
        days = max(1, min(int(days_ahead or 2), 30))
    except (ValueError, TypeError):
        days = 2
    clean_q = (query or "").strip().lower()
    now_pt = datetime.now(PT_TZ)
    cutoff_ts = now_pt.timestamp() + days * 86400

    scheduled_items: List[Dict[str, Any]] = []
    sched_path = Path(os.environ.get("GOTCHI_SCHEDULE_PATH", "/workspace/data/schedule.json"))
    if sched_path.is_file():
        try:
            raw_sched = json.loads(sched_path.read_text(encoding="utf-8", errors="replace"))
            jobs = raw_sched if isinstance(raw_sched, list) else raw_sched.get("jobs", [])
            for job in jobs:
                if not isinstance(job, dict) or not job.get("enabled", True):
                    continue
                name = str(job.get("name") or job.get("id") or "")
                prompt_txt = str(job.get("prompt") or "")
                stype = str(job.get("schedule_type") or "")
                next_ts = job.get("next_run_ts")
                if clean_q and clean_q not in f"{name} {prompt_txt} {stype}".lower():
                    continue
                if isinstance(next_ts, (int, float)) and next_ts > cutoff_ts and not clean_q:
                    continue
                when_str = ""
                if isinstance(next_ts, (int, float)) and next_ts > 0:
                    when_str = datetime.fromtimestamp(float(next_ts), tz=PT_TZ).strftime(
                        "%a %b %d %-I:%M %p PT"
                    )
                elif "hour_pt" in job:
                    when_str = f"{stype} at {int(job.get('hour_pt', 0)):02d}:{int(job.get('minute_pt', 0)):02d} PT"
                scheduled_items.append(
                    {
                        "id": job.get("id"),
                        "name": name,
                        "schedule_type": stype,
                        "when": when_str,
                        "next_run_ts": next_ts,
                    }
                )
        except Exception as exc:
            log.debug(f"[GotchiZero] schedule.json read warning: {exc}")

    gcal_events: List[Dict[str, Any]] = []
    try:
        from tools.workspace_mcp import _auth_headers, CALENDAR_BASE  # type: ignore

        headers = _auth_headers()
        time_min = datetime.now(ZoneInfo("UTC")).isoformat()
        time_max = datetime.fromtimestamp(cutoff_ts, tz=ZoneInfo("UTC")).isoformat()
        for cid, cal_label in (
            ("primary", "Ryan"),
            ("family05249951047154432652@group.calendar.google.com", "Family"),
        ):
            params = urllib.parse.urlencode(
                {
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 20,
                }
            )
            url = f"{CALENDAR_BASE}/calendars/{urllib.parse.quote(cid)}/events?{params}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for ev in data.get("items", []):
                summary = str(ev.get("summary") or "Untitled").strip()
                start_val = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or ""
                loc = str(ev.get("location") or "").strip()
                if clean_q and clean_q not in f"{summary} {loc}".lower():
                    continue
                gcal_events.append(
                    {
                        "calendar": cal_label,
                        "summary": summary,
                        "start": start_val,
                        "location": loc,
                    }
                )
    except Exception as exc:
        log.debug(f"[GotchiZero] Google Calendar lookup skipped/failed: {exc}")

    return {
        "ok": True,
        "days_ahead": days,
        "query": query,
        "calendar_events": gcal_events[:20],
        "scheduled_reminders": scheduled_items[:20],
    }


def _run_tv_and_plex_control_sync(action: str, media_query: str = "") -> Dict[str, Any]:
    """
    Controls the Living Room TV via Home Assistant `media_player` or launches Plex movies/shows
    (`plex_play.py` / `plex_playback.py`) or live MLB streams (`mlb_stream_extractor.py` / `mlb_stream.py`).
    """
    clean_action = (action or "status").strip().lower()
    clean_query = (media_query or "").strip()

    if clean_action in ("mlb", "baseball", "mlb_stream") or (
        clean_action in ("play", "watch", "stream")
        and any(w in clean_query.lower() for w in ("cubs", "dodgers", "giants", "yankees", "mlb", "baseball", "red sox", "padres", "mets", "braves", "phillies", "astros", "mariners"))
    ):
        team = clean_query or "cubs"
        script = (
            "/workspace/tools/mlb_stream_extractor.py"
            if Path("/workspace/tools/mlb_stream_extractor.py").exists()
            else "/workspace/tools/mlb_stream.py"
        )
        try:
            res = subprocess.run(
                ["python3", script, "--team", team, "--cast"],
                capture_output=True,
                text=True,
                timeout=12,
            )
            return {
                "ok": res.returncode == 0,
                "action": "mlb_stream",
                "team": team,
                "exit_code": res.returncode,
                "output": (res.stdout or res.stderr or "").strip()[:1500],
            }
        except Exception as exc:
            return {"error": f"MLB stream launch failed: {exc}"}

    if clean_action in ("plex", "play_plex", "movie", "show") or (
        clean_action in ("play", "watch", "put_on") and clean_query
    ):
        if not clean_query:
            return {"error": "media_query is required to play a title on Plex."}
        script = (
            "/workspace/tools/plex_play.py"
            if Path("/workspace/tools/plex_play.py").exists()
            else "/workspace/tools/plex_playback.py"
        )
        try:
            res = subprocess.run(
                ["python3", script, clean_query],
                capture_output=True,
                text=True,
                timeout=12,
            )
            return {
                "ok": res.returncode == 0,
                "action": "plex_playback",
                "media_query": clean_query,
                "exit_code": res.returncode,
                "output": (res.stdout or res.stderr or "").strip()[:1500],
            }
        except Exception as exc:
            return {"error": f"Plex playback failed: {exc}"}

    tv_entity = os.environ.get("GOTCHI_LIVING_ROOM_TV_ENTITY", "media_player.living_room_tv_2")
    if clean_action in ("status", "state", "check"):
        return _run_home_assistant_control_sync("state", tv_entity)

    service_map = {
        "pause": ("media_player", "media_pause"),
        "play": ("media_player", "media_play"),
        "resume": ("media_player", "media_play"),
        "play_pause": ("media_player", "media_play_pause"),
        "stop": ("media_player", "media_stop"),
        "turn_off": ("media_player", "turn_off"),
        "off": ("media_player", "turn_off"),
        "power_off": ("media_player", "turn_off"),
        "turn_on": ("media_player", "turn_on"),
        "on": ("media_player", "turn_on"),
        "power_on": ("media_player", "turn_on"),
        "volume_up": ("media_player", "volume_up"),
        "volume_down": ("media_player", "volume_down"),
        "mute": ("media_player", "volume_mute"),
    }
    dom, svc = service_map.get(clean_action, ("media_player", clean_action))
    extra_json = json.dumps({"is_volume_muted": True}) if svc == "volume_mute" else ""
    return _run_home_assistant_control_sync(
        action="call_service",
        entity_id_or_query=tv_entity,
        domain=dom,
        service=svc,
        service_data_json=extra_json,
    )


def _run_check_3d_printer_sync() -> Dict[str, Any]:
    """
    Queries OctoPrint / Moonraker API (resolved from environment, `/secrets/env.json`, or `nas_config`)
    with a 2s timeout for live print job progress and nozzle/bed temperatures.
    """
    base_url = (
        os.environ.get("OCTOPRINT_URL")
        or os.environ.get("MOONRAKER_URL")
        or ""
    ).strip()
    api_key = (os.environ.get("OCTOPRINT_API_KEY") or "").strip()

    sec_path = Path("/secrets/env.json")
    if sec_path.is_file():
        try:
            sec = json.loads(sec_path.read_text(encoding="utf-8", errors="replace"))
            if not base_url:
                base_url = str(
                    sec.get("OCTOPRINT_URL")
                    or sec.get("OCTOPRINT_BASE_URL")
                    or sec.get("MOONRAKER_URL")
                    or ""
                ).strip()
            if not api_key:
                api_key = str(sec.get("OCTOPRINT_API_KEY") or sec.get("OCTOPRINT_KEY") or "").strip()
        except Exception:
            pass

    if not base_url:
        try:
            import tools.nas_config as _nc

            base_url = f"http://{_nc.HOST_2_IP}:8888"
        except Exception:
            base_url = "http://127.0.0.1:8888"
    if not api_key:
        try:
            from tools.zero_printer_watcher import OCTOPRINT_DEFAULT_KEY

            api_key = OCTOPRINT_DEFAULT_KEY
        except Exception:
            api_key = ""

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    job_data: Dict[str, Any] = {}
    printer_data: Dict[str, Any] = {}
    errors: List[str] = []

    try:
        req_job = urllib.request.Request(f"{base_url.rstrip('/')}/api/job", headers=headers, method="GET")
        with urllib.request.urlopen(req_job, timeout=2.0) as resp:
            job_data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        errors.append(f"job: {exc}")

    try:
        req_prn = urllib.request.Request(f"{base_url.rstrip('/')}/api/printer", headers=headers, method="GET")
        with urllib.request.urlopen(req_prn, timeout=2.0) as resp:
            printer_data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        errors.append(f"printer: {exc}")

    if not job_data and not printer_data:
        watcher_status = Path("/workspace/data/3dprint/watcher_status.json")
        if watcher_status.is_file():
            try:
                cached = json.loads(watcher_status.read_text(encoding="utf-8", errors="replace"))
                return {"ok": True, "source": "watcher_status.json", **cached}
            except Exception:
                pass
        return {
            "ok": False,
            "state": "Offline / Unreachable",
            "error": "; ".join(errors) or "Printer API unreachable",
        }

    state = str(job_data.get("state") or (printer_data.get("state") or {}).get("text") or "Unknown")
    prog = job_data.get("progress") or {}
    completion = prog.get("completion")
    print_time = prog.get("printTime")
    print_time_left = prog.get("printTimeLeft")
    file_name = ((job_data.get("job") or {}).get("file") or {}).get("name") or ""

    temps = printer_data.get("temperature") or {}
    tool0 = temps.get("tool0") or {}
    bed = temps.get("bed") or {}

    return {
        "ok": True,
        "state": state,
        "file": file_name,
        "completion_pct": round(float(completion), 1) if isinstance(completion, (int, float)) else None,
        "print_time_s": print_time,
        "print_time_left_s": print_time_left,
        "nozzle_temp_c": tool0.get("actual"),
        "nozzle_target_c": tool0.get("target"),
        "bed_temp_c": bed.get("actual"),
        "bed_target_c": bed.get("target"),
    }


def _run_grocery_list_action_sync(action: str, items: str = "") -> Dict[str, Any]:
    """
    Adds/lists/clears items in `/workspace/data/grocery_list.json` or builds a 1-click
    Whole Foods AFX cart link (`tools/whole_foods_afx.py`) and posts it to `#zero-chat`.
    """
    clean_action = (action or "list").strip().lower()
    raw_items = [
        i.strip()
        for i in re.split(r"[,;\n]+", str(items or ""))
        if i and i.strip()
    ]

    list_path = GROCERY_LIST_FILE
    list_path.parent.mkdir(parents=True, exist_ok=True)
    current_items: List[str] = []
    if list_path.is_file():
        try:
            loaded = json.loads(list_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, list):
                current_items = [str(x).strip() for x in loaded if str(x).strip()]
            elif isinstance(loaded, dict):
                current_items = [str(x).strip() for x in loaded.get("items", []) if str(x).strip()]
        except Exception:
            current_items = []

    if clean_action in ("add", "append"):
        if not raw_items:
            return {"error": "No items provided to add."}
        existing_lower = {x.lower() for x in current_items}
        added: List[str] = []
        for it in raw_items:
            if it.lower() not in existing_lower:
                current_items.append(it)
                existing_lower.add(it.lower())
                added.append(it)
        list_path.write_text(
            json.dumps({"items": current_items, "updated_at": time.time()}, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "action": "add",
            "added": added,
            "items": current_items,
            "count": len(current_items),
        }

    if clean_action in ("remove", "delete"):
        remove_lower = {x.lower() for x in raw_items}
        kept = [x for x in current_items if x.lower() not in remove_lower]
        list_path.write_text(
            json.dumps({"items": kept, "updated_at": time.time()}, indent=2) + "\n",
            encoding="utf-8",
        )
        return {"ok": True, "action": "remove", "items": kept, "count": len(kept)}

    if clean_action == "clear":
        list_path.write_text(
            json.dumps({"items": [], "updated_at": time.time()}, indent=2) + "\n",
            encoding="utf-8",
        )
        return {"ok": True, "action": "clear", "items": [], "count": 0}

    if clean_action in ("cart", "afx", "whole_foods", "stage_cart", "build_cart"):
        cart_source = raw_items or current_items
        if not cart_source:
            return {"error": "Grocery list is empty; provide items or add items first."}
        if raw_items:
            existing_lower = {x.lower() for x in current_items}
            for it in raw_items:
                if it.lower() not in existing_lower:
                    current_items.append(it)
                    existing_lower.add(it.lower())
            list_path.write_text(
                json.dumps({"items": current_items, "updated_at": time.time()}, indent=2) + "\n",
                encoding="utf-8",
            )
        from tools.whole_foods_afx import generate_afx_url, normalize_ingredient_name

        manifest = [
            {"name": normalize_ingredient_name(it), "amount": 1.0, "unit": "COUNT"}
            for it in cart_source
        ]
        afx_url = generate_afx_url(manifest)
        bullet_list = "\n".join(f"- {m['name']}" for m in manifest)
        msg = (
            f"🛒 **[Whole Foods 1-Click Cart Ready](<{afx_url}>)** ({len(manifest)} items):\n"
            f"{bullet_list}"
        )
        send_gotchi_artifact_to_zero_chat(message=msg, mirror_gotchi_chat=False)
        return {
            "ok": True,
            "action": "cart",
            "afx_url": afx_url,
            "items": [m["name"] for m in manifest],
            "count": len(manifest),
            "posted_to_zero_chat": True,
        }

    return {
        "ok": True,
        "action": "list",
        "items": current_items,
        "count": len(current_items),
    }


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

    if name == "load_skill_playbook":
        raw_skill = str(args.get("skill_name") or args.get("name") or "").strip()
        if not raw_skill:
            return {"error": "Empty skill_name"}
        norm_skill = raw_skill.lower().strip()
        if norm_skill.endswith("/skill.md"):
            norm_skill = Path(norm_skill).parent.name.lower()
        playbook_map = await asyncio.to_thread(_discover_skill_playbook_paths)
        matched_path = (
            playbook_map.get(norm_skill)
            or playbook_map.get(norm_skill.replace("_", "-"))
            or playbook_map.get(norm_skill.replace("-", "_"))
        )
        if matched_path is None:
            for k, p in playbook_map.items():
                if norm_skill in k or k in norm_skill:
                    matched_path = p
                    break
        if matched_path is None or not matched_path.is_file():
            return {
                "error": f"Skill playbook not found: {raw_skill}",
                "available_skills": sorted({p.parent.name for p in playbook_map.values()}),
            }
        try:
            content = await asyncio.to_thread(matched_path.read_text, "utf-8", "replace")
            return {
                "skill_name": matched_path.parent.name,
                "path": str(matched_path),
                "content": content[:12000],
            }
        except Exception as e:
            return {"error": str(e)}

    if name == "handoff_to_zero_runtime":
        task_prompt = str(args.get("task_prompt") or args.get("prompt") or "").strip()
        spoken_ack = str(args.get("spoken_ack") or "I'm kicking that off in Zero-Chat for you now.").strip()
        node_id = str(args.get("node_id") or "gotchi-ryan-01").strip() or "gotchi-ryan-01"
        if not task_prompt:
            return {"error": "Empty task_prompt"}
        handoff_payload = _format_handoff_dual_delivery_prompt(task_prompt, node_id=node_id)
        queued = await asyncio.to_thread(
            _queue_zero_runtime_turn,
            handoff_payload,
            "gotchi-voice-handoff",
        )
        return {
            "ok": True,
            "status": "handed_off_to_zero_runtime",
            "channel_id": TARGET_CHANNEL_ID or 1542081375287640084,
            "task_prompt": task_prompt,
            "spoken_ack": spoken_ack,
            "queued": queued,
        }

    if name == "home_assistant_control":
        return await asyncio.to_thread(
            _run_home_assistant_control_sync,
            str(args.get("action") or "state"),
            str(args.get("entity_id_or_query") or args.get("entity_id") or args.get("query") or ""),
            str(args.get("domain") or ""),
            str(args.get("service") or ""),
            str(args.get("service_data_json") or ""),
        )

    if name == "check_calendar":
        return await asyncio.to_thread(
            _run_check_calendar_sync,
            int(args.get("days_ahead", 2) or 2),
            str(args.get("query") or ""),
        )

    if name == "tv_and_plex_control":
        return await asyncio.to_thread(
            _run_tv_and_plex_control_sync,
            str(args.get("action") or "status"),
            str(args.get("media_query") or args.get("query") or ""),
        )

    if name == "check_3d_printer":
        return await asyncio.to_thread(_run_check_3d_printer_sync)

    if name == "grocery_list_action":
        return await asyncio.to_thread(
            _run_grocery_list_action_sync,
            str(args.get("action") or "list"),
            str(args.get("items") or ""),
        )

    if name == "get_weather_forecast":
        loc = str(args.get("location", "") or "").strip() or DEFAULT_HOME_LOCATION
        return await asyncio.to_thread(fetch_live_weather, loc)

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
                timeout=4,
            )
            return {"result": (res.stdout or "No matches found.")[:3500]}
        except Exception as e:
            return {"error": str(e)}

    if name == "check_tasks_and_repos":
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
            repos = [r.strip() for r in os.environ.get("GOTCHI_GITHUB_REPOS", "brockventures/gotchi").split(",") if r.strip()]

            for r in repos:
                gh_res = await asyncio.to_thread(
                    subprocess.run,
                    ["gh", "issue", "list", "--repo", r, "--state", "open", "--limit", "10"],
                    capture_output=True,
                    text=True,
                    timeout=3,
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
        destructive, reason = is_destructive_voice_command(cmd_str)
        if destructive and not allow_unconfirmed_mutations:
            return await asyncio.to_thread(
                stage_voice_mutation_confirmation,
                "run_shell_command",
                {"command": cmd_str, "reason": reason},
            )
        try:
            res = await asyncio.to_thread(
                subprocess.run,
                cmd_str,
                shell=True,
                cwd="/workspace",
                capture_output=True,
                text=True,
                timeout=15,
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
        destructive, reason_or_resolved = is_destructive_voice_write(fpath)
        if destructive and not allow_unconfirmed_mutations:
            return await asyncio.to_thread(
                stage_voice_mutation_confirmation,
                "write_local_file",
                {"file_path": fpath, "content": content, "reason": reason_or_resolved},
            )
        try:
            target_path = Path(reason_or_resolved if not destructive else fpath)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content)
            return {"status": f"Successfully wrote {len(content)} chars to {fpath}"}
        except Exception as e:
            return {"error": str(e)}

    if name == "web_and_reddit_search":
        query = str(args.get("query", "")).strip()
        sub = str(args.get("subreddit", "")).strip()
        mode = str(args.get("mode") or args.get("source") or "reddit").strip().lower()
        recency = str(args.get("recency") or "").strip().lower()
        if not query:
            return {"error": "Empty query"}
        try:
            if mode == "amazon":
                cmd = ["python3", "/workspace/tools/amazon_serpapi.py", query]
                res = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=6,
                )
                return {"result": (res.stdout or res.stderr or "No results")[:3500]}
            if mode in ("web", "google", "general", "broad"):
                web_res = await asyncio.to_thread(_run_general_web_search, query)
                return {"result": web_res[:3500]}

            cmd = ["python3", "/workspace/tools/reddit_extractor.py", "search", query, "-n", "5"]
            if sub:
                cmd.extend(["--sub", sub])
            if recency in ("year", "all", "month", "week"):
                cmd.extend(["--recency", recency])
            res = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=6,
            )
            stdout = res.stdout or ""
            if '"count": 0' in stdout and recency != "all":
                retry_cmd = [c for c in cmd if c not in ("--recency", "year", "month", "week")] + ["--recency", "all"]
                res = await asyncio.to_thread(
                    subprocess.run,
                    retry_cmd,
                    capture_output=True,
                    text=True,
                    timeout=6,
                )
                stdout = res.stdout or ""
            if '"count": 0' in stdout:
                web_res = await asyncio.to_thread(_run_general_web_search, query)
                return {"result": web_res[:3500]}
            return {"result": (stdout or res.stderr or "No results")[:3500]}
        except Exception as e:
            return {"error": str(e)}

    if name == "get_recent_discord_context":
        raw_ch = str(args.get("channel_name", "zero-chat") or "zero-chat")
        is_rollup, clean_ch = normalize_discord_channel_input(raw_ch)

        ch_file = Path("/workspace/data/channel_history.json")
        if not ch_file.exists():
            return {"result": "No channel history available."}

        try:
            history_data = json.loads(ch_file.read_text())
        except Exception as e:
            return {"error": f"Failed to load channel history: {e}"}

        if is_rollup:
            sections = []
            for ch_label, cid in CRAB_CAVERN_ROLLUP_CHANNELS:
                msgs = history_data.get(str(cid), [])[-4:]
                if msgs:
                    formatted_msgs = "\n".join(
                        f"[{m.get('timestamp_pt') or m.get('timestamp') or 'recent'}] {m.get('author', 'Unknown')}: {str(m.get('content', ''))[:300]}"
                        for m in msgs
                    )
                    sections.append(f"#{ch_label}:\n{formatted_msgs}")
            if sections:
                return {"result": "\n\n".join(sections)}
            return {"result": "No recent messages across Crab Cavern channels."}

        # Single channel resolution
        fallback_zero_chat = str(TARGET_CHANNEL_ID) if TARGET_CHANNEL_ID else "1542081375287640084"
        fallback_gotchi_chat = str(GOTCHI_CHAT_CHANNEL_ID) if GOTCHI_CHAT_CHANNEL_ID else "1562246590943461429"

        ch_map: Dict[str, str] = {
            # Crab Cavern channels
            "lounge": "1534452820995080192",
            "gotchi": "1561509863018856468",
            "gotchi-chat": fallback_gotchi_chat,
            "gotchichat": fallback_gotchi_chat,
            "the-banana-stand": "1534436119888793750",
            "banana-stand": "1534436119888793750",
            "thebananastand": "1534436119888793750",
            "bananastand": "1534436119888793750",
            "side-project": "1551465050072416286",
            "sideproject": "1551465050072416286",
            "mirrormere": "1552732003076669460",
            "debug-huddle": "1549555629222404096",
            "debughuddle": "1549555629222404096",
            "agora": "1558202642663211169",
            "r-residentagents": "1562544598423965856",
            "residentagents": "1562544598423965856",
            "resident-agents": "1562544598423965856",
            "rresidentagents": "1562544598423965856",
            # Home channels
            "zero-chat": fallback_zero_chat,
            "zerochat": fallback_zero_chat,
            "zero-ops": "1544953279664889888",
            "zeroops": "1544953279664889888",
            "homelab": "1544955535722545253",
            "home-assistant": "1544953275877556334",
            "homeassistant": "1544953275877556334",
            "finances": "1544955532765560924",
            "shopping": "1544955538033348618",
            "projects": "1548196930788524094",
            "3d-print": "1555973316752449576",
            "3dprint": "1555973316752449576",
            "steam-deck": fallback_zero_chat,
            "steamdeck": fallback_zero_chat,
            "baseball": "1548196929308065893",
            # Aliases
            "general": "1534452820995080192",
        }

        if clean_ch.isdigit():
            cid = clean_ch
        else:
            hyphenated_ch = clean_ch.replace(" ", "-").replace("_", "-")
            compact_ch = clean_ch.replace(" ", "").replace("-", "").replace("_", "")
            cid = (
                ch_map.get(clean_ch)
                or ch_map.get(hyphenated_ch)
                or ch_map.get(compact_ch)
            )
            if not cid:
                for k, v in ch_map.items():
                    if k.replace("-", "") == compact_ch:
                        cid = v
                        break
            if not cid:
                cid = fallback_zero_chat

        msgs = history_data.get(str(cid), [])[-12:]
        formatted = "\n".join(
            f"[{m.get('timestamp_pt') or m.get('timestamp') or 'recent'}] {m.get('author', 'Unknown')}: {str(m.get('content', ''))[:300]}"
            for m in msgs
        )
        return {"result": formatted or "No recent messages."}

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
    2. `/workspace/data/channel_history.json` for BOTH `#zero-chat`
       and `#gotchi-chat` so Zero has instant continuity everywhere.
    3. Standalone `#gotchi-chat` Discord channel via `deliver_image.py`
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
        timing_footer = f"\n-# stt: {stt_ms}ms{fa_str} · total: {total_ms}ms"

    if mode == "journal":
        discord_msg = f"📓 **[All-Day Journal]{img_badge}:**\n> {transcript}{timing_footer}"
    else:
        discord_msg = (
            f"🎙️ **Ryan{img_badge}:** {transcript}\n"
            f"🔊 **Zero:** {reply}{timing_footer}"
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


def _load_ha_credentials_from_env_or_secrets() -> Tuple[bool, str, str, float]:
    """
    Resolves `(enabled, ha_base_url, ha_access_token, timeout_s)` for Task #98.
    Defaults strictly to `enabled=False` unless `HA_CONVERSATION_ENABLED`,
    `GOTCHI_HA_CONVERSATION_ENABLED`, `MVG_HA_ENABLED`, or `MVG_HA_FASTPATH` is
    explicitly set to truthy (`1`, `true`, `yes`, `on`).
    When enabled, loads `HA_BASE_URL` and `HA_ACCESS_TOKEN` from environment or `/secrets/env.json`.
    """
    raw_flag = (
        os.environ.get("HA_CONVERSATION_ENABLED")
        or os.environ.get("GOTCHI_HA_CONVERSATION_ENABLED")
        or os.environ.get("MVG_HA_ENABLED")
        or os.environ.get("MVG_HA_FASTPATH")
        or ""
    ).strip().lower()
    enabled = raw_flag in ("1", "true", "yes", "on")
    base_url = (
        os.environ.get("GOTCHI_HA_BASE_URL")
        or os.environ.get("MVG_HA_URL")
        or os.environ.get("HA_BASE_URL")
        or ""
    ).strip()
    token = (
        os.environ.get("GOTCHI_HA_ACCESS_TOKEN")
        or os.environ.get("MVG_HA_TOKEN")
        or os.environ.get("HA_ACCESS_TOKEN")
        or ""
    ).strip()
    try:
        timeout_s = float(os.environ.get("GOTCHI_HA_CONVERSATION_TIMEOUT_S", "2.5") or 2.5)
    except ValueError:
        timeout_s = 2.5

    if not base_url or not token:
        sec_path = Path("/secrets/env.json")
        if sec_path.is_file():
            try:
                sec_data = json.loads(sec_path.read_text(encoding="utf-8"))
                if not base_url:
                    base_url = str(sec_data.get("HA_BASE_URL") or "").strip()
                if not token:
                    token = str(sec_data.get("HA_ACCESS_TOKEN") or "").strip()
            except Exception:
                pass
    return enabled, base_url, token, timeout_s


def is_lomin_vision_enabled() -> bool:
    """Return True if Lomin vision ingest is enabled via any dual-named flag."""
    for k in ("MVG_ENABLE_VISION", "MVG_VISION_ENABLED", "MVG_VISION_ADPCM"):
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def is_lomin_adpcm_enabled() -> bool:
    """Return True if Lomin ADPCM audio codec is enabled via any dual-named flag."""
    for k in ("MVG_ENABLE_ADPCM", "MVG_ADPCM_ENABLED", "MVG_VISION_ADPCM"):
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def is_lomin_direct_audio_enabled() -> bool:
    """Return True if Lomin direct audio to brain is enabled via any dual-named flag."""
    for k in ("MVG_BRAIN_DIRECT_AUDIO", "MVG_DIRECT_AUDIO"):
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def is_lomin_ha_fastpath_enabled() -> bool:
    """Return True if Lomin HA conversation fast-path is enabled via any dual-named flag."""
    for k in ("MVG_HA_FASTPATH", "MVG_HA_ENABLED", "HA_CONVERSATION_ENABLED", "GOTCHI_HA_CONVERSATION_ENABLED"):
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def is_lomin_mic_reopen_enabled() -> bool:
    """Return True if Lomin auto-mic reopen is enabled via any dual-named flag."""
    for k in ("MVG_ENABLE_MIC_REOPEN", "MVG_MIC_REOPEN_ENABLED"):
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def is_lomin_defer_enabled() -> bool:
    """Return True if Lomin deferred async voice answers are enabled via any dual-named flag."""
    for k in ("MVG_ENABLE_DEFER", "MVG_DEFER_ENABLED"):
        if os.environ.get(k, "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    return False


def shutdown_lomin_background_workers(timeout_s: float = 2.0) -> bool:
    """
    Gracefully flushes and shuts down Lomin side-channel daemon worker threads
    (`SideChannelWebhookWorker` and `SideChannelTranscriptWorker`) by sending
    `None` sentinel tasks so daemon threads terminate cleanly on app shutdown.
    """
    try:
        import side_channel as sc  # type: ignore

        ok = sc.flush_side_channel_queue(timeout_s=timeout_s)
        with getattr(sc, "_worker_lock", asyncio.Lock()):
            if getattr(sc, "_webhook_worker_started", False):
                try:
                    sc._webhook_queue.put_nowait(None)
                except Exception:
                    pass
                sc._webhook_worker_started = False
            if getattr(sc, "_transcript_worker_started", False):
                try:
                    sc._transcript_queue.put_nowait(None)
                except Exception:
                    pass
                sc._transcript_worker_started = False
            sc._worker_started = False
        return bool(ok)
    except Exception:
        return True


def try_ha_conversation_fastpath(
    transcript: str,
    *,
    has_images: bool = False,
    enabled: Optional[bool] = None,
    ha_base_url: Optional[str] = None,
    ha_access_token: Optional[str] = None,
    timeout_s: Optional[float] = None,
    http_poster: Optional[Callable[[str, Dict[str, str], Dict[str, Any], float], Dict[str, Any]]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Evaluates a transcribed Gotchi voice utterance against Home Assistant's local
    `/api/conversation/process` endpoint when `HA_CONVERSATION_ENABLED` is True.
    Falls back cleanly `(False, "", meta)` on `no_intent_matched`, vision turns, or errors.
    """
    from backend.llm_config import LLMConfig
    from backend.voice_gateway import HomeAssistantConversationFastPath

    env_enabled, env_url, env_token, env_timeout = _load_ha_credentials_from_env_or_secrets()
    eff_enabled = env_enabled if enabled is None else bool(enabled)
    eff_url = env_url if ha_base_url is None else str(ha_base_url).strip()
    eff_token = env_token if ha_access_token is None else str(ha_access_token).strip()
    eff_timeout = env_timeout if timeout_s is None else float(timeout_s)

    cfg = LLMConfig(
        ha_conversation_enabled=eff_enabled,
        ha_base_url=eff_url,
        ha_access_token=eff_token,
        ha_conversation_timeout_s=eff_timeout,
    )
    fastpath = HomeAssistantConversationFastPath(llm_config=cfg, http_poster=http_poster)
    return fastpath.try_process(transcript, has_images=has_images)



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
        thinking_budget: int = DEFAULT_THINKING_BUDGET,
        ha_conversation_enabled: Optional[bool] = None,
        ha_base_url: Optional[str] = None,
        ha_access_token: Optional[str] = None,
        ha_http_poster: Optional[Callable[[str, Dict[str, str], Dict[str, Any], float], Dict[str, Any]]] = None,
        warm_worker: Optional[Any] = None,
        brain_mode: Optional[str] = None,
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
        self.ha_conversation_enabled = ha_conversation_enabled
        self.ha_base_url = ha_base_url
        self.ha_access_token = ha_access_token
        self.ha_http_poster = ha_http_poster
        self.warm_worker = warm_worker
        self.brain_mode = (brain_mode or "").strip().lower()
        self._genai_client: Optional[Any] = None
        self._cached_content_name: Optional[str] = None
        self._cached_content_model: Optional[str] = None
        self._cached_content_fingerprint: Optional[str] = None
        self._cached_content_expires_at: float = 0.0
        self.context_cache_ttl_sec: int = int(
            os.environ.get("GOTCHI_CONTEXT_CACHE_TTL_SEC", "3600") or 3600
        )

    def _get_or_create_context_cache(
        self,
        client: Any,
        model: str,
        static_sys_prompt: str,
        tools_list: List[Any],
    ) -> Optional[str]:
        cache_enabled = os.environ.get("GOTCHI_CONTEXT_CACHE_ENABLED", "1").strip().lower()
        if cache_enabled in ("0", "false", "no", "off"):
            return None
        now_mono = time.monotonic()
        current_fingerprint = _compute_context_cache_fingerprint(static_sys_prompt, tools_list)
        if (
            self._cached_content_name
            and self._cached_content_model == model
            and self._cached_content_fingerprint == current_fingerprint
            and now_mono < self._cached_content_expires_at
        ):
            return self._cached_content_name

        caches_api = getattr(client, "caches", None)
        create_fn = getattr(caches_api, "create", None) if caches_api is not None else None
        if not callable(create_fn):
            return None

        try:
            from google.genai import types

            cache_obj = create_fn(
                model=model,
                config=types.CreateCachedContentConfig(
                    system_instruction=static_sys_prompt,
                    tools=tools_list,
                    ttl=f"{self.context_cache_ttl_sec}s",
                    display_name="gotchi-zero-persona-rules",
                ),
            )
            cache_name = getattr(cache_obj, "name", None)
            if cache_name:
                self._cached_content_name = str(cache_name)
                self._cached_content_model = model
                self._cached_content_fingerprint = current_fingerprint
                # Refresh slightly before strict TTL expiry (with a 60s safety margin when TTL > 120s)
                safety_margin = 60.0 if self.context_cache_ttl_sec > 120 else 0.0
                self._cached_content_expires_at = time.monotonic() + max(
                    1.0, float(self.context_cache_ttl_sec) - safety_margin
                )
                return self._cached_content_name
        except Exception:
            pass
        return None

    _get_or_create_cached_content = _get_or_create_context_cache

    def _is_subscription_mode(self) -> bool:
        mode = self.brain_mode or os.environ.get("GOTCHI_BRAIN_MODE", "").strip().lower()
        return mode == "subscription" or self.warm_worker is not None

    def _get_genai_client(self) -> Any:
        from google import genai

        if not self.api_key:
            from dotenv import load_dotenv

            load_dotenv("/workspace/.env")
            self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not self.api_key:
            sec_path = Path("/secrets/env.json")
            if sec_path.is_file():
                try:
                    sec_data = json.loads(sec_path.read_text(encoding="utf-8"))
                    self.api_key = str(sec_data.get("GEMINI_API_KEY") or "").strip()
                except Exception:
                    pass
        if not self.api_key:
            return None
        if self._genai_client is None:
            self._genai_client = genai.Client(api_key=self.api_key)
        return self._genai_client

    def _resolve_live_gemini_model(self) -> str:
        return self.model

    async def _emit_streamed_sentence(
        self,
        sentence: str,
        emitted_sentences: List[str],
        on_sentence: Optional[Callable[[str], Any]],
    ) -> None:
        had_defer = "[[defer]]" in (sentence or "").lower()
        cleaned = clean_spoken_text_for_tts(sentence)
        if not cleaned or cleaned in emitted_sentences:
            return
        emitted_sentences.append(cleaned)
        if on_sentence is not None:
            res = on_sentence(f"[[defer]] {cleaned}" if had_defer else cleaned)
            if asyncio.iscoroutine(res):
                await res

    async def _emit_stage_event(
        self,
        *,
        fn_name: str,
        fn_args: Dict[str, Any],
        detail: str,
        step: int,
        t0: float,
        on_tool_start: Optional[Callable[..., Any]] = None,
        on_stage: Optional[Callable[..., Any]] = None,
        stage_callback: Optional[Callable[..., Any]] = None,
        stages_out: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        elapsed = round(max(0.0, time.monotonic() - t0), 3)
        event_dict: Dict[str, Any] = {
            "stage": "tool",
            "tool": fn_name,
            "detail": detail,
            "args": fn_args,
            "step": step,
            "t": elapsed,
        }
        if isinstance(stages_out, dict):
            stages_out.setdefault("call_start_ms", int(time.time() * 1000))
            events_list = stages_out.setdefault("events", [])
            if isinstance(events_list, list):
                events_list.append(dict(event_dict))
        else:
            try:
                gw_mod = sys.modules.get("gateway") or sys.modules.get("lomin.gateway")
                if gw_mod is not None:
                    sink = getattr(gw_mod, "_STAGE_SINK", None)
                    sink_stages = getattr(sink, "stages", None) if sink is not None else None
                    if isinstance(sink_stages, dict):
                        sink_stages.setdefault("call_start_ms", int(time.time() * 1000))
                        events_list = sink_stages.setdefault("events", [])
                        if isinstance(events_list, list):
                            events_list.append(dict(event_dict))
            except Exception:
                pass

        for mod_name in ("stage_events", "lomin.stage_events"):
            try:
                se_mod = sys.modules.get(mod_name)
                if se_mod is None:
                    import importlib

                    se_mod = importlib.import_module(mod_name)
                emit_fn = getattr(se_mod, "emit_stage", None)
                if callable(emit_fn):
                    try:
                        emit_fn(stage="tool", tool=fn_name, detail=detail)
                    except TypeError:
                        try:
                            emit_fn("tool", detail)
                        except TypeError:
                            emit_fn(dict(event_dict))
                    break
            except Exception:
                continue

        async def _call_cb(cb: Any, is_tool_cb: bool = False) -> None:
            if not callable(cb):
                return
            try:
                sig = inspect.signature(cb)
                params = list(sig.parameters.values())
                has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
                has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
                param_names = {p.name for p in params}
                pos_params = [
                    p
                    for p in params
                    if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                if has_var_kw or {"stage", "tool", "detail"}.issubset(param_names):
                    kw: Dict[str, Any] = {"stage": "tool", "tool": fn_name, "detail": detail}
                    if not has_var_kw:
                        kw = {k: v for k, v in kw.items() if k in param_names}
                    res = cb(**kw)
                elif len(pos_params) >= 3 or has_var_pos:
                    try:
                        res = cb(stage="tool", tool=fn_name, detail=detail)
                    except TypeError:
                        res = cb(fn_name if is_tool_cb else dict(event_dict))
                elif len(pos_params) == 2:
                    if is_tool_cb:
                        res = cb(fn_name, detail)
                    else:
                        res = cb("tool", detail)
                elif len(pos_params) == 1:
                    first_name = pos_params[0].name.lower()
                    if is_tool_cb and first_name not in ("event", "ev", "payload", "raw", "data", "stage_event"):
                        res = cb(fn_name)
                    else:
                        res = cb(dict(event_dict))
                else:
                    res = cb()
            except TypeError:
                try:
                    res = cb(fn_name if is_tool_cb else dict(event_dict))
                except TypeError:
                    res = cb(dict(event_dict))
            if inspect.isawaitable(res):
                await res

        seen_cbs: List[Any] = []
        for cb, is_tool in (
            (on_tool_start, True),
            (on_stage, False),
            (stage_callback, False),
        ):
            if cb is not None and cb not in seen_cbs:
                seen_cbs.append(cb)
                await _call_cb(cb, is_tool_cb=is_tool)

        return event_dict

    async def _run_gemini_with_tools(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        burst_images: List[bytes],
        on_sentence: Optional[Callable[[str], Any]] = None,
        audio_bytes: Optional[bytes] = None,
        on_tool_start: Optional[Callable[..., Any]] = None,
        on_stage: Optional[Callable[..., Any]] = None,
        stage_callback: Optional[Callable[..., Any]] = None,
        stages_out: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, bool, List[str]]:
        from backend.voice_gateway import SentenceStreamSplitter
        from google.genai import types

        t0_run = time.monotonic()
        client = self._get_genai_client()
        if client is None:
            return "Zero is online, but GEMINI_API_KEY is not configured in the environment.", False, []

        has_vision = bool(image_bytes or burst_images)
        prompt_is_placeholder = is_placeholder_prompt(prompt)
        sys_prompt_text = "" if (audio_bytes and prompt_is_placeholder) else prompt
        static_sys_prompt = await asyncio.to_thread(build_gotchi_static_system_instruction)
        dynamic_turn_ctx = await asyncio.to_thread(
            build_gotchi_dynamic_turn_context, sys_prompt_text, has_vision
        )
        sys_prompt = f"{static_sys_prompt}\n\n{dynamic_turn_ctx}"

        tools_list = _build_gotchi_tools()
        live_model = self._resolve_live_gemini_model()
        cache_name = await asyncio.to_thread(
            self._get_or_create_context_cache,
            client,
            live_model,
            static_sys_prompt,
            tools_list,
        )

        user_parts: List[types.Part] = []
        if audio_bytes:
            user_parts.append(types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"))
        if image_bytes:
            user_parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        for b_img in burst_images[:5]:
            user_parts.append(types.Part.from_bytes(data=b_img, mime_type="image/jpeg"))
        if audio_bytes and (not prompt or prompt_is_placeholder):
            prefix_txt = prompt if (prompt and prompt.startswith("[VOICE TURN")) else format_voice_turn_prefix()
            turn_body = f"{prefix_txt}\nListen to Ryan's spoken voice prompt in the attached audio clip and answer directly."
        else:
            turn_body = prompt or "Describe what you see."
        if cache_name:
            turn_body = f"{dynamic_turn_ctx}\n\n{turn_body}"
        user_parts.append(types.Part.from_text(text=turn_body))

        contents: List[types.Content] = [types.Content(role="user", parts=user_parts)]

        explicitly_posted_to_zero_chat = False
        tool_had_error = False
        model_requested_deep_thinking = False
        emitted_sentences: List[str] = []
        accumulated_raw_parts: List[str] = []

        for _step in range(4):
            live_model = self._resolve_live_gemini_model()
            thinking_cfg = build_thinking_config(
                live_model,
                prompt,
                default_budget=self.thinking_budget,
                step=_step,
                tool_had_error=tool_had_error,
                burst_count=len(burst_images),
                model_requested_deep_thinking=model_requested_deep_thinking,
            )
            if cache_name and self._cached_content_model == live_model:
                config = types.GenerateContentConfig(
                    cached_content=cache_name,
                    temperature=0.4,
                    thinking_config=thinking_cfg,
                )
            else:
                config = types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                    tools=tools_list,
                    temperature=0.4,
                    thinking_config=thinking_cfg,
                )

            splitter = SentenceStreamSplitter()
            step_text_chunks: List[str] = []
            step_fcalls: List[Any] = []
            step_model_parts: List[types.Part] = []

            stream_fn = getattr(client.aio.models, "generate_content_stream", None)
            if callable(stream_fn):
                async def _iter_stream_with_fallback():
                    models_to_try = [live_model]
                    for fb in ("gemini-2.5-flash", "gemini-2.0-flash"):
                        if fb not in models_to_try:
                            models_to_try.append(fb)
                    last_err = None
                    for idx, m_name in enumerate(models_to_try):
                        try:
                            m_cfg = config if idx == 0 else types.GenerateContentConfig(
                                system_instruction=sys_prompt,
                                tools=tools_list,
                                temperature=0.4,
                            )
                            s_obj = stream_fn(model=m_name, contents=contents, config=m_cfg)
                            if asyncio.iscoroutine(s_obj):
                                s_obj = await s_obj
                            async for c_item in s_obj:
                                yield c_item
                            return
                        except Exception as s_err:
                            last_err = s_err
                            if any(code in str(s_err) for code in ("503", "429", "500", "UNAVAILABLE", "overloaded")) and idx + 1 < len(models_to_try):
                                continue
                            raise
                    if last_err:
                        raise last_err

                async for chunk in _iter_stream_with_fallback():
                    chunk_text = getattr(chunk, "text", None) or ""
                    if chunk_text:
                        step_text_chunks.append(chunk_text)
                        if _step == 0:
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
                    model=live_model,
                    contents=contents,
                    config=config,
                )
                resp_text = getattr(resp, "text", None) or ""
                if resp_text:
                    step_text_chunks.append(resp_text)
                    if _step == 0:
                        for ready_s in splitter.feed(resp_text):
                            await self._emit_streamed_sentence(
                                ready_s, emitted_sentences, on_sentence
                            )
                if getattr(resp, "function_calls", None):
                    step_fcalls.extend(list(resp.function_calls))
                if getattr(resp, "candidates", None) and getattr(resp.candidates[0], "content", None):
                    c_parts = getattr(resp.candidates[0].content, "parts", None) or []
                    step_model_parts.extend(list(c_parts))

            if _step == 0:
                for trailing_s in splitter.flush():
                    await self._emit_streamed_sentence(
                        trailing_s, emitted_sentences, on_sentence
                    )

            step_text = "".join(step_text_chunks).strip()

            if _step == 0:
                if step_text:
                    accumulated_raw_parts.append(step_text)
                if not step_fcalls:
                    final_raw = " ".join(accumulated_raw_parts).strip()
                    return final_raw, explicitly_posted_to_zero_chat, emitted_sentences
            else:
                if step_fcalls:
                    if step_text:
                        accumulated_raw_parts.append(step_text)
                        for ready_s in splitter.feed(step_text):
                            await self._emit_streamed_sentence(
                                ready_s, emitted_sentences, on_sentence
                            )
                        for trailing_s in splitter.flush():
                            await self._emit_streamed_sentence(
                                trailing_s, emitted_sentences, on_sentence
                            )
                else:
                    if step_text:
                        accumulated_raw_parts.append(step_text)
                        for ready_s in splitter.feed(step_text):
                            await self._emit_streamed_sentence(
                                ready_s, emitted_sentences, on_sentence
                            )
                        for trailing_s in splitter.flush():
                            await self._emit_streamed_sentence(
                                trailing_s, emitted_sentences, on_sentence
                            )
                    final_raw = step_text or " ".join(accumulated_raw_parts).strip()
                    return final_raw, explicitly_posted_to_zero_chat, emitted_sentences

            if step_model_parts:
                contents.append(types.Content(role="model", parts=step_model_parts))

            fn_parts: List[types.Part] = []
            tool_had_error = False
            step_emitted_progress = bool(step_text)
            for fc in step_fcalls:
                fn_name = getattr(fc, "name", "")
                fn_args = dict(getattr(fc, "args", {}) or {})
                filler_detail = build_contextual_tool_filler(fn_name, fn_args, prompt)
                await self._emit_stage_event(
                    fn_name=fn_name,
                    fn_args=fn_args,
                    detail=filler_detail,
                    step=_step,
                    t0=t0_run,
                    on_tool_start=on_tool_start,
                    on_stage=on_stage,
                    stage_callback=stage_callback,
                    stages_out=stages_out,
                )
                if fn_name == "request_deep_thinking":
                    model_requested_deep_thinking = True
                    preamble = str(fn_args.get("spoken_preamble", "") or "").strip()
                    if preamble:
                        if preamble not in accumulated_raw_parts:
                            accumulated_raw_parts.append(preamble)
                        await self._emit_streamed_sentence(
                            preamble, emitted_sentences, on_sentence
                        )
                        step_emitted_progress = True
                elif fn_name == "handoff_to_zero_runtime":
                    ack_text = str(
                        fn_args.get("spoken_ack")
                        or "I'm kicking that off in Zero-Chat for you now."
                    ).strip()
                    if ack_text:
                        if ack_text not in accumulated_raw_parts:
                            accumulated_raw_parts.append(ack_text)
                        await self._emit_streamed_sentence(
                            ack_text, emitted_sentences, on_sentence
                        )
                        step_emitted_progress = True
                elif _step == 0 and not step_text and not emitted_sentences:
                    # Pre-tool contextual action narration when the model invokes a tool without leading text
                    await self._emit_streamed_sentence(
                        filler_detail, emitted_sentences, on_sentence
                    )
                    step_emitted_progress = True
                elif _step >= 1 and not step_emitted_progress:
                    # Multi-step tool progress narration when the model invokes a subsequent tool without step_text
                    await self._emit_streamed_sentence(
                        filler_detail, emitted_sentences, on_sentence
                    )
                    step_emitted_progress = True

                res_dict = await execute_gotchi_tool_call(fn_name, fn_args)
                if fn_name in ("post_note_to_zero_chat", "handoff_to_zero_runtime") or (
                    isinstance(res_dict, dict) and res_dict.get("confirmation_required") is True
                ):
                    explicitly_posted_to_zero_chat = True

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

        if step_fcalls:
            live_model = self._resolve_live_gemini_model()
            synth_config = types.GenerateContentConfig(
                system_instruction=sys_prompt,
                tools=[],
                temperature=0.4,
                thinking_config=build_thinking_config(
                    live_model,
                    prompt,
                    default_budget=self.thinking_budget,
                    step=4,
                    tool_had_error=tool_had_error,
                    burst_count=len(burst_images),
                    model_requested_deep_thinking=model_requested_deep_thinking,
                ),
            )
            try:
                synth_resp = await client.aio.models.generate_content(
                    model=live_model,
                    contents=contents,
                    config=synth_config,
                )
                synth_text = getattr(synth_resp, "text", None) or ""
                if synth_text:
                    accumulated_raw_parts.append(synth_text)
                    synth_splitter = SentenceStreamSplitter()
                    for ready_s in synth_splitter.feed(synth_text):
                        await self._emit_streamed_sentence(
                            ready_s, emitted_sentences, on_sentence
                        )
                    for trailing_s in synth_splitter.flush():
                        await self._emit_streamed_sentence(
                            trailing_s, emitted_sentences, on_sentence
                        )
            except Exception as exc:
                log.warning(f"[GotchiZero] Final synthesis pass after tool exhaustion failed: {exc}")

        fallback_raw = " ".join(accumulated_raw_parts).strip() or "Handled. I sent that over to Zero-Chat."
        return fallback_raw, explicitly_posted_to_zero_chat, emitted_sentences

    async def _transcribe_audio_with_gemini(self, wav_bytes: bytes) -> str:
        """
        Transcribes raw WAV audio via Gemini multimodal (`audio/wav`) when `stt_adapter`
        is in mock mode or returns the default placeholder (`What is the status?`).
        """
        from google.genai import types

        client = self._get_genai_client()
        if client is None or not wav_bytes:
            return ""
        live_model = self._resolve_live_gemini_model()
        try:
            resp = await client.aio.models.generate_content(
                model=live_model,
                contents=[
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"),
                            types.Part.from_text(
                                text=(
                                    "Transcribe the spoken English speech in this audio verbatim. "
                                    "Return ONLY the spoken words (no quotes, no commentary). "
                                    "If there is no intelligible speech, return an empty string."
                                )
                            ),
                        ],
                    )
                ],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    thinking_config=build_thinking_config(live_model, "", default_budget=0, step=0),
                ),
            )
            return (getattr(resp, "text", None) or "").strip()
        except Exception as exc:
            log.warning(f"[GotchiZero] Multimodal audio transcription fallback failed: {exc}")
            return ""


    async def handle_turn(self, parsed_turn: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compatible directly with `VoiceGatewayService(turn_handler=adapter.handle_turn)`
        and `SubscriptionBrainAdapter` JSON payloads.
        """
        t0 = time.monotonic()
        node_id = str(parsed_turn.get("node_id") or "gotchi-ryan-01")
        speaker = resolve_speaker_for_node(node_id, parsed_turn.get("speaker"))
        session_id = str(parsed_turn.get("session_id") or f"sess-{int(time.time()*1000)}")
        on_sentence: Optional[Callable[[str], Any]] = parsed_turn.get("_on_sentence")
        on_tool_start: Optional[Callable[..., Any]] = (
            parsed_turn.get("_on_tool_start") or parsed_turn.get("on_tool_start")
        )
        on_stage: Optional[Callable[..., Any]] = (
            parsed_turn.get("on_stage") or parsed_turn.get("_on_stage")
        )
        stage_callback: Optional[Callable[..., Any]] = parsed_turn.get("stage_callback")
        stages_out: Optional[Dict[str, Any]] = (
            parsed_turn.get("stages_out")
            if isinstance(parsed_turn.get("stages_out"), dict)
            else None
        )

        image_bytes: Optional[bytes] = parsed_turn.get("image_bytes")
        if image_bytes is None and parsed_turn.get("image_b64"):
            image_bytes = base64.b64decode(parsed_turn["image_b64"])

        burst_images: List[bytes] = list(parsed_turn.get("burst_images") or [])
        if not burst_images and parsed_turn.get("burst_images_b64"):
            burst_images = [
                base64.b64decode(b) for b in parsed_turn["burst_images_b64"] if b
            ]

        raw_prompt = str(parsed_turn.get("prompt") or "").strip()
        has_images = bool(image_bytes or burst_images)
        audio_bytes: Optional[bytes] = parsed_turn.get("audio_bytes")
        has_explicit_prompt = bool(raw_prompt) and not (audio_bytes and is_placeholder_prompt(raw_prompt))

        # Archive latest uploaded WAV clip for forensic inspection (cap at 10 recent turn files)
        if audio_bytes:
            try:
                tmp_dir = Path("/tmp")
                (tmp_dir / "gotchi_last_turn.wav").write_bytes(audio_bytes)
                safe_sid = re.sub(r"[^a-zA-Z0-9_.-]", "_", session_id)[:64]
                (tmp_dir / f"gotchi_turn_{safe_sid}.wav").write_bytes(audio_bytes)
                turn_wavs = sorted(
                    tmp_dir.glob("gotchi_turn_*.wav"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for old_wav in turn_wavs[10:]:
                    old_wav.unlink(missing_ok=True)
            except Exception:
                pass

        # Fast-path acoustic duration check before invoking any cloud API
        from backend.voice_gateway import is_stt_ghost, measure_voiced_audio_ms, split_into_sentences

        min_voiced_ms = int(getattr(getattr(self.stt_adapter, "llm_config", None), "min_voiced_ms", 0) or 0)
        use_silero_vad = bool(getattr(getattr(self.stt_adapter, "llm_config", None), "use_silero_vad", False))

        bg_stt_task: Optional[asyncio.Task[str]] = None
        direct_audio_to_brain = False
        _stt_cfg = getattr(self.stt_adapter, "llm_config", None)
        _has_real_stt = self.stt_adapter is not None and not (
            getattr(_stt_cfg, "stt_engine", "mock") == "mock"
            and getattr(self.stt_adapter, "local_transcriber", None) is None
        )

        if has_explicit_prompt:
            transcript = raw_prompt
        elif audio_bytes and self.llm_caller is None and not self._is_subscription_mode():
            # Single-pass multimodal audio: stream WAV directly into the Gemini brain
            # while transcribing concurrently in the background for #gotchi-chat logs.
            direct_audio_to_brain = True
            if _has_real_stt:
                bg_stt_task = asyncio.create_task(
                    asyncio.to_thread(self.stt_adapter.transcribe, audio_bytes)
                )
            else:
                bg_stt_task = asyncio.create_task(
                    self._transcribe_audio_with_gemini(audio_bytes)
                )
            transcript = ""
        elif audio_bytes and _has_real_stt:
            loop = asyncio.get_running_loop()
            raw_tx = await loop.run_in_executor(
                None, self.stt_adapter.transcribe, audio_bytes
            )
            raw_tx = (raw_tx or "").strip()
            transcript = raw_tx or (
                (
                    "Analyze this 5-frame camera burst from Gotchi."
                    if burst_images
                    else "Analyze this camera snapshot from Gotchi."
                )
                if has_images
                else "Voice prompt"
            )
        elif audio_bytes:
            raw_tx = await self._transcribe_audio_with_gemini(audio_bytes)
            raw_tx = (raw_tx or "").strip()
            transcript = raw_tx or (
                (
                    "Analyze this 5-frame camera burst from Gotchi."
                    if burst_images
                    else "Analyze this camera snapshot from Gotchi."
                )
                if has_images
                else "Voice prompt"
            )
        elif burst_images:
            transcript = "Analyze this 5-frame camera burst from Gotchi."
        elif image_bytes:
            transcript = "Analyze this camera snapshot from Gotchi."
        else:
            transcript = "Status check."

        stt_ms = int((time.monotonic() - t0) * 1000)

        if not direct_audio_to_brain:
            is_ghost, _reason = is_stt_ghost(
                transcript_text=transcript,
                audio_bytes=audio_bytes,
                min_voiced_ms=min_voiced_ms,
                has_explicit_prompt=has_explicit_prompt,
                has_images=has_images,
                use_silero_vad=use_silero_vad,
            )
            if is_ghost:
                return {
                    "transcript": transcript,
                    "reply": "",
                    "raw_reply": "",
                    "sentences": [],
                    "stt_ms": stt_ms,
                }
        elif min_voiced_ms > 0 and not has_explicit_prompt and not has_images:
            is_ghost_audio, _reason = is_stt_ghost(
                transcript_text="voice turn",
                audio_bytes=audio_bytes,
                min_voiced_ms=min_voiced_ms,
                has_explicit_prompt=False,
                has_images=False,
                use_silero_vad=use_silero_vad,
            )
            if is_ghost_audio and audio_bytes:
                if measure_voiced_audio_ms(audio_bytes, rms_threshold=150.0) >= float(min_voiced_ms):
                    is_ghost_audio = False
            if is_ghost_audio:
                if bg_stt_task is not None:
                    bg_stt_task.cancel()
                return {
                    "transcript": "",
                    "reply": "",
                    "raw_reply": "",
                    "sentences": [],
                    "stt_ms": stt_ms,
                }

        explicitly_posted = False
        streamed_sentences: List[str] = []
        ha_matched, ha_speech, ha_meta = (False, "", {"reason": "disabled"})

        def _build_gemini_stage_kwargs() -> Dict[str, Any]:
            candidate_kw = {
                "on_tool_start": on_tool_start,
                "on_stage": on_stage,
                "stage_callback": stage_callback,
                "stages_out": stages_out,
            }
            non_null = {k: v for k, v in candidate_kw.items() if v is not None}
            if not non_null:
                return {}
            try:
                sig = inspect.signature(self._run_gemini_with_tools)
                params = sig.parameters
                has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                return {k: v for k, v in non_null.items() if has_var_kw or k in params}
            except (ValueError, TypeError):
                return non_null

        if direct_audio_to_brain and bg_stt_task is not None:
            # Race Gemini audio stream against bg_stt_task:
            # Hold Gemini's sentence output until either the transcript arrives
            # or Gemini's first sentence is ready, whichever comes first.
            buffered_live_sentences: List[str] = []
            stt_cleared = False
            first_sentence_ready = asyncio.Event()

            def _gated_on_sentence(s_txt: str) -> None:
                if stt_cleared:
                    if on_sentence is not None:
                        on_sentence(s_txt)
                else:
                    buffered_live_sentences.append(s_txt)
                    first_sentence_ready.set()

            brain_task = asyncio.create_task(
                self._run_gemini_with_tools(
                    prompt=format_voice_turn_prefix(node_id=node_id, speaker=speaker),
                    image_bytes=image_bytes,
                    burst_images=burst_images,
                    on_sentence=_gated_on_sentence,
                    audio_bytes=audio_bytes,
                    **_build_gemini_stage_kwargs(),
                )
            )
            first_sentence_waiter = asyncio.create_task(first_sentence_ready.wait())
            await asyncio.wait(
                [bg_stt_task, first_sentence_waiter, brain_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            first_sentence_waiter.cancel()

            if bg_stt_task.done():
                try:
                    mm_transcript = await bg_stt_task
                    raw_tx = (mm_transcript or "").strip()
                except Exception:
                    raw_tx = ""
                bg_stt_task = None
                transcript = raw_tx or (
                    (
                        "Analyze this 5-frame camera burst from Gotchi."
                        if burst_images
                        else "Analyze this camera snapshot from Gotchi."
                    )
                    if has_images
                    else "Voice prompt"
                )
                stt_ms = int((time.monotonic() - t0) * 1000)

                # 1. Transcript-level ghost check before allowing any audio output.
                # Do not let an empty bg_stt_task transcript cancel brain_task unless the
                # audio clip also fails a server-side voiced-energy check (< 400.0 ms).
                is_ghost_tx, _reason_tx = is_stt_ghost(
                    transcript_text=raw_tx,
                    audio_bytes=audio_bytes if not raw_tx else None,
                    min_voiced_ms=min_voiced_ms,
                    has_explicit_prompt=has_explicit_prompt,
                    has_images=has_images,
                    use_silero_vad=use_silero_vad,
                )
                if is_ghost_tx and not raw_tx and audio_bytes:
                    voiced_ms_check = measure_voiced_audio_ms(audio_bytes, rms_threshold=150.0)
                    if voiced_ms_check >= max(400.0, float(min_voiced_ms or 400.0)):
                        is_ghost_tx = False
                if is_ghost_tx:
                    brain_task.cancel()
                    try:
                        await brain_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    return {
                        "transcript": transcript,
                        "reply": "",
                        "raw_reply": "",
                        "sentences": [],
                        "stt_ms": stt_ms,
                    }

                # 2. Home Assistant fast-path check: if matched, cancel in-flight brain_task and reply immediately
                if raw_tx:
                    ha_matched, ha_speech, ha_meta = await asyncio.to_thread(
                        try_ha_conversation_fastpath,
                        raw_tx,
                        has_images=has_images,
                        enabled=self.ha_conversation_enabled,
                        ha_base_url=self.ha_base_url,
                        ha_access_token=self.ha_access_token,
                        http_poster=self.ha_http_poster,
                    )
                if ha_matched and ha_speech:
                    brain_task.cancel()
                    try:
                        await brain_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raw_reply = ha_speech
                    await self._emit_streamed_sentence(
                        ha_speech, streamed_sentences, on_sentence
                    )
                else:
                    # 3. Normal query: release gated sentences and await the already-running brain_task
                    stt_cleared = True
                    for buf_s in buffered_live_sentences:
                        if on_sentence is not None:
                            on_sentence(buf_s)
                    buffered_live_sentences.clear()
                    raw_reply, explicitly_posted, streamed_sentences = await brain_task
                    if not transcript:
                        transcript = raw_tx or (
                            (
                                "Analyze this 5-frame camera burst from Gotchi."
                                if burst_images
                                else "Analyze this camera snapshot from Gotchi."
                            )
                            if has_images
                            else "Voice prompt"
                        )
            else:
                # Gemini's first sentence (or completion) arrived first:
                # release Gemini's output immediately and skip Home Assistant for that turn.
                ha_meta = {"reason": "gemini_first_sentence_won_race"}
                stt_cleared = True
                for buf_s in buffered_live_sentences:
                    if on_sentence is not None:
                        on_sentence(buf_s)
                buffered_live_sentences.clear()
                raw_reply, explicitly_posted, streamed_sentences = await brain_task
        else:
            if transcript:
                ha_matched, ha_speech, ha_meta = await asyncio.to_thread(
                    try_ha_conversation_fastpath,
                    transcript,
                    has_images=has_images,
                    enabled=self.ha_conversation_enabled,
                    ha_base_url=self.ha_base_url,
                    ha_access_token=self.ha_access_token,
                    http_poster=self.ha_http_poster,
                )
            if ha_matched and ha_speech:
                raw_reply = ha_speech
                await self._emit_streamed_sentence(
                    ha_speech, streamed_sentences, on_sentence
                )
            elif self.llm_caller is not None:
                prefix = format_voice_turn_prefix(node_id=node_id, speaker=speaker)
                framed_prompt = f"{prefix}\n{transcript}" if transcript else prefix
                raw_res = self.llm_caller(
                    {
                        "node_id": node_id,
                        "speaker": speaker,
                        "session_id": session_id,
                        "prompt": framed_prompt,
                        "raw_prompt": transcript,
                        "image_bytes": image_bytes,
                        "burst_images": burst_images,
                        "_on_sentence": on_sentence,
                        "_on_tool_start": on_tool_start,
                        "on_stage": on_stage,
                        "stage_callback": stage_callback,
                        "stages_out": stages_out,
                    }
                )
                if asyncio.iscoroutine(raw_res):
                    raw_res = await raw_res
                if isinstance(raw_res, dict):
                    raw_reply = str(raw_res.get("reply") or "")
                    explicitly_posted = bool(raw_res.get("posted_to_zero_chat", False))
                else:
                    raw_reply = str(raw_res)
            elif self._is_subscription_mode():
                from backend.voice_gateway import WarmSubscriptionVoiceWorker

                if self.warm_worker is None:
                    self.warm_worker = WarmSubscriptionVoiceWorker()
                prefix = format_voice_turn_prefix(node_id=node_id, speaker=speaker)
                base_prompt = "" if (audio_bytes and is_placeholder_prompt(transcript)) else transcript
                call_prompt = f"{prefix}\n{base_prompt}" if base_prompt else prefix

                async def _forward_sentence(s_txt: str) -> None:
                    await self._emit_streamed_sentence(s_txt, streamed_sentences, on_sentence)

                async def _forward_tool_start(t_name: str) -> None:
                    filler = build_contextual_tool_filler(t_name, {}, call_prompt)
                    await self._emit_stage_event(
                        fn_name=t_name,
                        fn_args={},
                        detail=filler,
                        step=0,
                        t0=t0,
                        on_tool_start=on_tool_start,
                        on_stage=on_stage,
                        stage_callback=stage_callback,
                        stages_out=stages_out,
                    )

                worker_res = await self.warm_worker.run_turn(
                    call_prompt,
                    node_id=node_id,
                    session_id=session_id,
                    on_sentence=_forward_sentence,
                    on_tool_start=_forward_tool_start,
                )
                raw_reply = str(worker_res.get("reply") or "")
            else:
                prefix = format_voice_turn_prefix(node_id=node_id, speaker=speaker)
                base_prompt = "" if (audio_bytes and is_placeholder_prompt(transcript)) else transcript
                call_prompt = f"{prefix}\n{base_prompt}" if base_prompt else prefix
                raw_reply, explicitly_posted, streamed_sentences = await self._run_gemini_with_tools(
                    prompt=call_prompt,
                    image_bytes=image_bytes,
                    burst_images=burst_images,
                    on_sentence=on_sentence,
                    audio_bytes=audio_bytes if direct_audio_to_brain else None,
                    **_build_gemini_stage_kwargs(),
                )

        if bg_stt_task is not None:
            try:
                mm_transcript = await bg_stt_task
                raw_tx = (mm_transcript or "").strip()
            except Exception:
                raw_tx = ""
            transcript = raw_tx or (
                (
                    "Analyze this 5-frame camera burst from Gotchi."
                    if burst_images
                    else "Analyze this camera snapshot from Gotchi."
                )
                if has_images
                else "Voice prompt"
            )
            stt_ms = int((time.monotonic() - t0) * 1000)

        has_defer_in_reply = "[[defer]]" in (raw_reply or "").lower() or any(
            "[[defer]]" in (s or "").lower() for s in streamed_sentences
        )
        if has_defer_in_reply and not explicitly_posted:
            defer_prompt = (transcript or raw_prompt or "Deferred voice request").strip()
            await asyncio.to_thread(
                _queue_zero_runtime_turn,
                f"[GOTCHI VOICE HANDOFF]: {defer_prompt}",
                "gotchi-voice-handoff",
            )
            explicitly_posted = True

        is_long = is_long_answer(raw_reply)
        is_async = bool(parsed_turn.get("is_async_handoff") or parsed_turn.get("condense_long"))
        if is_long and is_async:
            topic = extract_topic(transcript)
            spoken_reply = condense_long_answer(raw_reply, topic=topic)
            if self.dispatch_discord:
                await asyncio.to_thread(
                    send_gotchi_artifact_to_zero_chat,
                    message=f"📋 **[Async Answer Breakdown]:**\n> **Prompt:** {transcript}\n\n{raw_reply}",
                    mirror_gotchi_chat=False,
                )
                sent_to_zero_chat = True
            else:
                sent_to_zero_chat = False
        else:
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

        _log_coro = asyncio.to_thread(
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
        if self.llm_caller is None:
            _bg_log = asyncio.create_task(_log_coro)
            _bg_log.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        else:
            await _log_coro

        return {
            "transcript": transcript,
            "reply": spoken_reply,
            "raw_reply": raw_reply,
            "sentences": sentences,
            "stt_ms": stt_ms,
            "sent_to_zero_chat": sent_to_zero_chat,
            "ha_fastpath": bool(ha_matched and ha_speech),
            "ha_meta": ha_meta,
        }


def apply_defaults() -> None:
    """Lomin plugin hook (`MVG_PLUGIN=gotchi_zero_adapter`): applies Zero's default env settings."""
    os.environ.setdefault("MVG_PORT", "8771")
    os.environ.setdefault("GOTCHI_ZERO_MODEL", DEFAULT_GOTCHI_MODEL)
    os.environ.setdefault("MVG_GEMINI_VOICE", DEFAULT_VOICE_NAME)


def brain_call(target: str, prompt: str, timeout: int = 45) -> str:
    """Lomin plugin synchronous brain hook."""
    adapter = GotchiZeroBrainAdapter()
    res = asyncio.run(adapter.handle_turn({"prompt": prompt, "node_id": target or "gotchi-ryan-01"}))
    return str(res.get("reply") or "")


def brain_call_stream(
    target: str,
    prompt: str,
    timeout: int = 45,
    on_partial: Optional[Callable[[str], None]] = None,
    stages_out: Optional[Dict[str, Any]] = None,
    on_stage: Optional[Callable[..., Any]] = None,
    on_tool_start: Optional[Callable[..., Any]] = None,
    stage_callback: Optional[Callable[..., Any]] = None,
) -> str:
    """Lomin plugin streaming brain hook."""
    adapter = GotchiZeroBrainAdapter()
    running_sentences: List[str] = []

    def _on_s(s_txt: str) -> None:
        running_sentences.append(s_txt)
        if on_partial is not None:
            on_partial(" ".join(running_sentences))

    if stages_out is None:
        try:
            gw_mod = sys.modules.get("gateway") or sys.modules.get("lomin.gateway")
            if gw_mod is not None:
                sink = getattr(gw_mod, "_STAGE_SINK", None)
                sink_stages = getattr(sink, "stages", None) if sink is not None else None
                if isinstance(sink_stages, dict):
                    stages_out = sink_stages
        except Exception:
            pass

    if isinstance(stages_out, dict):
        stages_out.setdefault("call_start_ms", int(time.time() * 1000))
        stages_out.setdefault("events", [])

    turn_payload: Dict[str, Any] = {
        "prompt": prompt,
        "node_id": target or "gotchi-ryan-01",
        "_on_sentence": _on_s,
    }
    if stages_out is not None:
        turn_payload["stages_out"] = stages_out
    if on_stage is not None:
        turn_payload["on_stage"] = on_stage
    if on_tool_start is not None:
        turn_payload["_on_tool_start"] = on_tool_start
    if stage_callback is not None:
        turn_payload["stage_callback"] = stage_callback

    res = asyncio.run(adapter.handle_turn(turn_payload))
    reply = str(res.get("reply") or "")
    if isinstance(stages_out, dict):
        stages_out["final_seen_ms"] = int(time.time() * 1000)
    if on_partial is not None and not running_sentences and reply:
        on_partial(reply)
    return reply


async def _parse_json_or_empty_body(request: Request) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Safely parses a request body as a JSON dict, falling back cleanly to `{}` when the
    request body is empty or whitespace-only instead of raising `JSONDecodeError`.
    Returns `(parsed_dict, error_message)`.
    """
    try:
        raw_bytes = await request.body()
    except Exception:
        return {}, None
    if not raw_bytes or not raw_bytes.strip():
        return {}, None
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None, "invalid JSON body"
    if not isinstance(parsed, dict):
        return None, "request body must be a JSON object"
    return parsed, None


def create_zero_gotchi_app(
    adapter: Optional[GotchiZeroBrainAdapter] = None,
    live_mode_enabled: bool = True,
):
    """
    Creates the full FastAPI Gotchi + Lomin Unified Gateway (`POST /api/voice/interact`,
    `POST /voice/turn`, `POST /ask`, `POST /ask/stream`, `GET/POST /voice/pending`,
    `GET/POST /voice/pending/{node_id}`, `POST /v1/audio/speech`, `WebSocket /api/live/ws`,
    `POST /api/journal/ingest`, `GET /api/voice/logs`, plus `POST /api/gotchi/brain`) wired
    directly to Zero's brain and `#gotchi-chat` transcript recorder.
    """
    for _lomin_dir in ("/tmp/lomin-parity-repo", "/workspace/gotchi/lomin"):
        if Path(_lomin_dir).is_dir() and _lomin_dir not in sys.path:
            sys.path.insert(0, _lomin_dir)

    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from backend.live_gateway import LiveGatewayService
    from backend.llm_config import LLMConfig
    from backend.voice_gateway import VoiceGatewayService, build_sse_event, create_app

    try:
        from pending_and_auth import (  # type: ignore
            get_device_token_verifier as get_lomin_verifier,
            get_pending_store as get_lomin_pending_store,
            has_defer_marker as lomin_has_defer_marker,
            strip_defer_marker as lomin_strip_defer_marker,
        )
    except Exception:
        get_lomin_verifier = None
        get_lomin_pending_store = None
        lomin_has_defer_marker = None
        lomin_strip_defer_marker = None

    try:
        from side_channel import (  # type: ignore
            emit_turn_transcript as lomin_emit_turn_transcript,
            forward_artifacts_to_webhook as lomin_forward_artifacts,
            reply_expects_reply as lomin_reply_expects_reply,
        )
    except Exception:
        lomin_emit_turn_transcript = None
        lomin_forward_artifacts = None
        lomin_reply_expects_reply = None

    try:
        from vision_adpcm import decode_adpcm_to_wav as lomin_decode_adpcm_to_wav  # type: ignore
    except Exception:
        lomin_decode_adpcm_to_wav = None

    try:
        from audio_utils import pcm_to_wav as lomin_pcm_to_wav  # type: ignore
    except Exception:
        lomin_pcm_to_wav = None

    brain = adapter or GotchiZeroBrainAdapter()
    cfg = LLMConfig.from_env_or_file()
    cfg.voice_name = os.environ.get("MVG_GEMINI_VOICE", DEFAULT_VOICE_NAME) or DEFAULT_VOICE_NAME
    if (
        brain.llm_caller is None
        and "GOTCHI_TTS_ENGINE" not in os.environ
        and not cfg.tts_url.strip()
    ):
        cfg.tts_engine = os.environ.get("MVG_TTS", "gemini") or "gemini"
        if "GOTCHI_TTS_MODEL" not in os.environ:
            cfg.tts_model = "gemini-2.5-flash-preview-tts"
    if "GOTCHI_FILLER_DELAY_MS" not in os.environ:
        cfg.filler_delay_ms = 0
    if "GOTCHI_SECOND_FILLER_DELAY_MS" not in os.environ:
        cfg.second_filler_delay_ms = 0
    if "GOTCHI_MIN_VOICED_MS" not in os.environ:
        cfg.min_voiced_ms = 160
    if cfg.async_handoff_ms == 0 and "GOTCHI_ASYNC_HANDOFF_MS" not in os.environ:
        cfg.async_handoff_ms = 8000
    if "GOTCHI_SSE_AUDIO_FORMAT" not in os.environ:
        cfg.sse_audio_format = "adpcm"

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

    if (
        brain.llm_caller is None
        and "PYTEST_CURRENT_TEST" not in os.environ
        and os.environ.get("GOTCHI_ALLOW_MOCK_FALLBACK") != "1"
    ):
        if cfg.tts_engine == "mock" or (
            cfg.tts_engine == "kokoro"
            and not cfg.tts_url.strip()
            and getattr(voice_srv.kokoro_tts, "local_synthesizer", None) is None
        ):
            msg = (
                f"Refusing to start Gotchi gateway with unconfigured TTS stub "
                f"(tts_engine={cfg.tts_engine!r}, tts_url={cfg.tts_url!r})"
            )
            log.error(msg)
            raise RuntimeError(msg)
        if cfg.tts_engine == "gemini" and brain._get_genai_client() is None and not cfg.tts_url.strip():
            msg = "Refusing to start Gotchi gateway: tts_engine='gemini' but GEMINI_API_KEY is missing"
            log.error(msg)
            raise RuntimeError(msg)
        if (
            cfg.stt_engine == "mock"
            and getattr(voice_srv.stt_adapter, "local_transcriber", None) is None
            and brain._get_genai_client() is None
        ):
            msg = "Refusing to start Gotchi gateway: stt_engine='mock' and GEMINI_API_KEY is missing for multimodal STT"
            log.error(msg)
            raise RuntimeError(msg)
    live_srv = LiveGatewayService(
        verifier=voice_srv.verifier,
        auth_required=voice_srv.auth_required,
        live_mode_enabled=live_mode_enabled,
        llm_config=cfg,
    )
    app = create_app(service=voice_srv, live_service=live_srv)

    def _check_unified_auth(request: Request, node_id: str = "") -> Tuple[bool, int, Dict[str, Any]]:
        headers = dict(request.headers)
        client_host = request.client.host if request.client else "127.0.0.1"
        lockout_key = resolve_lomin_client_lockout_key(headers, client_host)

        # 1. Check Lomin MVG_DEVICE_TOKENS verifier if configured
        if get_lomin_verifier is not None:
            lomin_v = get_lomin_verifier()
            if lomin_v.is_enabled:
                auth_hdr = headers.get("authorization") or headers.get("Authorization") or ""
                if lomin_v.verify(auth_hdr, node_id):
                    lomin_v.record_success(lockout_key)
                    return True, 200, {}
                if lomin_v.is_locked_out(lockout_key):
                    return False, 429, {"error": "too many failed attempts, locked out"}
                lomin_v.record_failure(lockout_key)
                return False, 401, {"error": "unauthorized"}

        # 2. Check Gotchi device_tokens verifier if configured
        if voice_srv.auth_required or voice_srv.verifier.device_tokens:
            ok, code, err = voice_srv.verifier.verify(headers, client_ip=lockout_key)
            if not ok:
                return False, code, err
        return True, 200, {}

    @app.on_event("shutdown")
    async def _on_app_shutdown() -> None:
        shutdown_lomin_background_workers(timeout_s=2.0)

    @app.post("/api/gotchi/brain")
    async def gotchi_brain_endpoint(request: Request):
        body, err = await _parse_json_or_empty_body(request)
        if err is not None or body is None:
            return JSONResponse(status_code=400, content={"error": "bad_request", "message": err})
        res = await brain.handle_turn(body)
        return JSONResponse(status_code=200, content=res)

    @app.post("/voice/turn")
    async def lomin_voice_turn_endpoint(request: Request):
        headers = dict(request.headers)
        content_type = (headers.get("content-type") or headers.get("Content-Type") or "").lower()
        client_host = request.client.host if request.client else "127.0.0.1"
        qs = request.query_params
        node_hint = (qs.get("node_id") or qs.get("node") or headers.get("x-node-id") or "").strip()

        ok_auth, auth_code, auth_err = _check_unified_auth(request, node_id=node_hint)
        if not ok_auth:
            return JSONResponse(status_code=auth_code, content=auth_err)

        parsed_turn: Dict[str, Any] = {}
        if "multipart/form-data" in content_type:
            try:
                form = await request.form()
                parsed_turn = voice_srv.parse_multipart_data(form)
            except ValueError as ve:
                msg = str(ve)
                code = 413 if "exceeds" in msg.lower() or "too large" in msg.lower() else 400
                return JSONResponse(status_code=code, content={"error": "bad_request", "message": msg})
            except Exception as exc:
                return JSONResponse(status_code=400, content={"error": "bad_request", "message": str(exc)})
        elif "application/json" in content_type:
            body_dict, err = await _parse_json_or_empty_body(request)
            if err is not None or body_dict is None:
                return JSONResponse(status_code=400, content={"error": err or "invalid JSON body"})
            parsed_turn = dict(body_dict)
            if not parsed_turn.get("node_id"):
                parsed_turn["node_id"] = node_hint or "gotchi-ryan-01"
            if not parsed_turn.get("session_id"):
                parsed_turn["session_id"] = f"sess-{int(time.time() * 1000)}"
        else:
            raw_body = await request.body()
            audio_bytes = raw_body if raw_body else None
            if audio_bytes and audio_bytes.startswith(b"APCM"):
                if lomin_decode_adpcm_to_wav is not None:
                    try:
                        audio_bytes = lomin_decode_adpcm_to_wav(audio_bytes)
                    except ValueError as ve:
                        return JSONResponse(
                            status_code=413,
                            content={"error": "payload_too_large", "message": str(ve)},
                        )
            parsed_turn = {
                "node_id": node_hint or "gotchi-ryan-01",
                "session_id": qs.get("session_id") or f"sess-{int(time.time() * 1000)}",
                "prompt": qs.get("prompt") or "",
                "audio_bytes": audio_bytes,
            }

        node_id = str(parsed_turn.get("node_id") or node_hint or "gotchi-ryan-01")
        if node_id != node_hint:
            ok_auth2, auth_code2, auth_err2 = _check_unified_auth(request, node_id=node_id)
            if not ok_auth2:
                return JSONResponse(status_code=auth_code2, content=auth_err2)

        parsed_turn["client_ip"] = client_host
        generator = voice_srv.generate_sse_stream(parsed_turn)
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/ask")
    async def lomin_ask_endpoint(request: Request):
        t0 = time.monotonic()
        req, err = await _parse_json_or_empty_body(request)
        if err is not None or req is None:
            return JSONResponse(status_code=400, content={"error": err or "invalid JSON body"})

        node_id = str(req.get("node_id") or req.get("node") or request.headers.get("x-node-id") or "gotchi-ryan-01").strip()
        ok_auth, auth_code, auth_err = _check_unified_auth(request, node_id=node_id)
        if not ok_auth:
            return JSONResponse(status_code=auth_code, content=auth_err)

        prompt = str(req.get("prompt") or "").strip()
        session_id = str(req.get("session_id") or f"sess-{int(time.time() * 1000)}")
        turn_input = dict(req)
        turn_input["node_id"] = node_id
        turn_input["session_id"] = session_id
        turn_input["prompt"] = prompt

        res = await brain.handle_turn(turn_input)
        raw_reply = str(res.get("raw_reply") or res.get("reply") or "")
        spoken_reply = str(res.get("reply") or "")

        if lomin_forward_artifacts is not None and os.environ.get("MVG_ARTIFACT_WEBHOOK_URL"):
            spoken_reply, _ = lomin_forward_artifacts(raw_reply, spoken_reply)

        if is_lomin_defer_enabled() and lomin_has_defer_marker is not None and get_lomin_pending_store is not None:
            if lomin_has_defer_marker(raw_reply) or lomin_has_defer_marker(spoken_reply):
                clean_ans = lomin_strip_defer_marker(spoken_reply) if lomin_strip_defer_marker else spoken_reply
                get_lomin_pending_store().store(node_id=node_id, prompt=prompt, answer=clean_ans)
                voice_srv.pending_answer_store.save_answer(
                    node_id=node_id,
                    session_id=session_id,
                    prompt=prompt,
                    spoken_text=clean_ans,
                    full_text=raw_reply,
                )

        if lomin_strip_defer_marker is not None:
            spoken_reply = lomin_strip_defer_marker(spoken_reply)

        mic_reopen = is_lomin_mic_reopen_enabled()
        expects = (
            lomin_reply_expects_reply(spoken_reply)
            if lomin_reply_expects_reply is not None
            else spoken_reply.rstrip().endswith("?")
        )

        reply_payload: Dict[str, Any] = {"reply": spoken_reply, "transcript": res.get("transcript", prompt)}
        done_payload: Dict[str, Any] = {"total_ms": int((time.monotonic() - t0) * 1000)}
        if mic_reopen:
            reply_payload["expects_reply"] = expects
            done_payload["expects_reply"] = expects

        if lomin_emit_turn_transcript is not None and (
            os.environ.get("MVG_TRANSCRIPT_SINK_URL") or os.environ.get("MVG_TRANSCRIPT_LOG_PATH")
        ):
            tr_rec: Dict[str, Any] = {
                "kind": "ask",
                "endpoint": "/ask",
                "node_id": node_id,
                "prompt": prompt,
                "transcript": res.get("transcript", prompt),
                "reply": spoken_reply,
                "outcome": "ok",
                "total_ms": done_payload["total_ms"],
                "timestamp": time.time(),
            }
            if mic_reopen:
                tr_rec["expects_reply"] = expects
            lomin_emit_turn_transcript(tr_rec)

        async def _ask_sse():
            yield build_sse_event("status", {"status": "thinking"})
            yield build_sse_event("reply", reply_payload)
            yield build_sse_event("done", done_payload)

        return StreamingResponse(
            _ask_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.post("/ask/stream")
    async def lomin_ask_stream_endpoint(request: Request):
        t0 = time.monotonic()
        req, err = await _parse_json_or_empty_body(request)
        if err is not None or req is None:
            return JSONResponse(status_code=400, content={"error": err or "invalid JSON body"})

        node_id = str(req.get("node_id") or req.get("node") or request.headers.get("x-node-id") or "gotchi-ryan-01").strip()
        ok_auth, auth_code, auth_err = _check_unified_auth(request, node_id=node_id)
        if not ok_auth:
            return JSONResponse(status_code=auth_code, content=auth_err)

        prompt = str(req.get("prompt") or "").strip()
        session_id = str(req.get("session_id") or f"sess-{int(time.time() * 1000)}")
        want_voice = req.get("voice") not in (False, 0, "false", "0") and req.get("tts") != "none"

        turn_input = dict(req)
        turn_input["node_id"] = node_id
        turn_input["session_id"] = session_id
        turn_input["prompt"] = prompt

        res = await brain.handle_turn(turn_input)
        raw_reply = str(res.get("raw_reply") or res.get("reply") or "")
        spoken_reply = str(res.get("reply") or "")
        if lomin_strip_defer_marker is not None:
            spoken_reply = lomin_strip_defer_marker(spoken_reply)
        sentences = list(res.get("sentences") or ([spoken_reply] if spoken_reply else []))

        mic_reopen = is_lomin_mic_reopen_enabled()
        expects = (
            lomin_reply_expects_reply(spoken_reply)
            if lomin_reply_expects_reply is not None
            else spoken_reply.rstrip().endswith("?")
        )

        async def _ask_stream_sse():
            yield build_sse_event("turn", {"turn_id": session_id})
            yield build_sse_event("status", {"status": "thinking"})
            for idx, sent in enumerate(sentences):
                clean_s = lomin_strip_defer_marker(sent) if lomin_strip_defer_marker else sent
                if not clean_s:
                    continue
                ev_data: Dict[str, Any] = {
                    "text": clean_s,
                    "index": idx,
                    "is_final": idx == len(sentences) - 1,
                }
                if want_voice:
                    try:
                        pcm_bytes = await asyncio.to_thread(voice_srv._synthesize_sentence_pcm, clean_s)
                        if pcm_bytes:
                            ev_data["audio_b64"] = base64.b64encode(pcm_bytes).decode("ascii")
                            ev_data["format"] = "pcm"
                            ev_data["sample_rate"] = 24000
                    except Exception:
                        pass
                yield build_sse_event("sentence", ev_data)

            reply_payload: Dict[str, Any] = {
                "reply": spoken_reply,
                "raw_reply": raw_reply,
                "transcript": res.get("transcript", prompt),
            }
            done_payload: Dict[str, Any] = {"total_ms": int((time.monotonic() - t0) * 1000)}
            if mic_reopen:
                reply_payload["expects_reply"] = expects
                done_payload["expects_reply"] = expects
            yield build_sse_event("reply", reply_payload)
            yield build_sse_event("done", done_payload)

        return StreamingResponse(
            _ask_stream_sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    def _get_pending_for_node_sync(node_id: str, should_pop: bool = True) -> Dict[str, Any]:
        clean_node = (node_id or "").strip()
        item: Optional[Dict[str, Any]] = None
        if get_lomin_pending_store is not None:
            lomin_store = get_lomin_pending_store()
            item = lomin_store.pop_for_node(clean_node) if should_pop else lomin_store.peek_for_node(clean_node)
        if item is None:
            gotchi_ans = voice_srv.pending_answer_store.get_latest_pending(node_id=clean_node)
            if gotchi_ans:
                item = {
                    "id": gotchi_ans.get("answer_id", ""),
                    "node_id": clean_node,
                    "prompt": gotchi_ans.get("prompt", ""),
                    "answer": gotchi_ans.get("spoken_text") or gotchi_ans.get("full_text") or "",
                    "topic": gotchi_ans.get("topic", ""),
                    "created_at": gotchi_ans.get("created_at", time.time()),
                }
                if should_pop:
                    voice_srv.pending_answer_store.ack_answer(
                        node_id=clean_node, answer_id=gotchi_ans.get("answer_id")
                    )
        has_pending = item is not None
        return {
            "status": "ok",
            "node": clean_node,
            "node_id": clean_node,
            "has_pending": has_pending,
            "pending": item,
            "item": item,
            "popped": bool(should_pop and has_pending),
        }

    @app.get("/voice/pending")
    async def lomin_voice_pending_query_get(request: Request):
        qs = request.query_params
        node_id = (qs.get("node") or qs.get("node_id") or "").strip()
        ok_auth, auth_code, auth_err = _check_unified_auth(request, node_id=node_id)
        if not ok_auth:
            return JSONResponse(status_code=auth_code, content=auth_err)
        if not node_id:
            return JSONResponse(status_code=400, content={"error": "node parameter required"})
        pop_param = (qs.get("pop") or "1").lower()
        peek_param = (qs.get("peek") or "0").lower()
        should_pop = pop_param in ("1", "true", "yes") and peek_param not in ("1", "true", "yes")
        return JSONResponse(status_code=200, content=_get_pending_for_node_sync(node_id, should_pop=should_pop))

    @app.get("/voice/pending/{node_id}")
    async def lomin_voice_pending_path_get(node_id: str, request: Request):
        clean_node = (node_id or "").strip()
        ok_auth, auth_code, auth_err = _check_unified_auth(request, node_id=clean_node)
        if not ok_auth:
            return JSONResponse(status_code=auth_code, content=auth_err)
        qs = request.query_params
        pop_param = (qs.get("pop") or "1").lower()
        peek_param = (qs.get("peek") or "0").lower()
        should_pop = pop_param in ("1", "true", "yes") and peek_param not in ("1", "true", "yes")
        return JSONResponse(status_code=200, content=_get_pending_for_node_sync(clean_node, should_pop=should_pop))

    @app.post("/voice/pending")
    async def lomin_voice_pending_post(request: Request):
        req, err = await _parse_json_or_empty_body(request)
        if err is not None or req is None:
            return JSONResponse(status_code=400, content={"error": err or "invalid JSON body"})
        node_id = str(req.get("node_id") or req.get("node") or "").strip()
        ok_auth, auth_code, auth_err = _check_unified_auth(request, node_id=node_id)
        if not ok_auth:
            return JSONResponse(status_code=auth_code, content=auth_err)
        answer = str(req.get("answer") or "").strip()
        prompt = str(req.get("prompt") or "").strip()
        topic = str(req.get("topic") or "").strip()
        if not node_id:
            return JSONResponse(status_code=400, content={"error": "node_id required"})
        if not answer:
            return JSONResponse(status_code=400, content={"error": "answer required"})
        item: Dict[str, Any] = {}
        if get_lomin_pending_store is not None:
            item = get_lomin_pending_store().store(node_id=node_id, prompt=prompt, answer=answer, topic=topic)
        voice_srv.pending_answer_store.save_answer(
            node_id=node_id,
            session_id=f"pending-{int(time.time() * 1000)}",
            prompt=prompt,
            spoken_text=answer,
            full_text=answer,
            topic=topic,
        )
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "stored": True, "pending": item, "item": item, "node": node_id},
        )

    @app.post("/v1/audio/speech")
    async def lomin_audio_speech_endpoint(request: Request):
        req, err = await _parse_json_or_empty_body(request)
        if err is not None or req is None:
            return JSONResponse(status_code=400, content={"error": err or "invalid JSON body"})
        node_id = str(req.get("node") or req.get("node_id") or request.headers.get("x-node-id") or "").strip()
        ok_auth, auth_code, auth_err = _check_unified_auth(request, node_id=node_id)
        if not ok_auth:
            return JSONResponse(status_code=auth_code, content=auth_err)

        text = str(req.get("input") or req.get("text") or "").strip()
        if lomin_strip_defer_marker is not None:
            text = lomin_strip_defer_marker(text)
        if not text:
            return JSONResponse(status_code=400, content={"error": "input is required"})

        resp_format = str(req.get("response_format") or "wav").strip().lower()
        try:
            pcm_bytes = await asyncio.to_thread(voice_srv._synthesize_sentence_pcm, text)
        except Exception as exc:
            return JSONResponse(status_code=502, content={"error": f"tts failed: {exc}"})

        if resp_format == "pcm":
            return Response(
                content=pcm_bytes,
                media_type="audio/pcm",
                headers={"X-TTS-Engine": cfg.tts_engine or "gemini"},
            )
        if resp_format == "adpcm" and lomin_decode_adpcm_to_wav is not None:
            try:
                from vision_adpcm import encode_adpcm  # type: ignore

                adpcm_bytes = encode_adpcm(pcm_bytes, sample_rate=24000)
                return Response(
                    content=adpcm_bytes,
                    media_type="application/octet-stream",
                    headers={"X-TTS-Engine": cfg.tts_engine or "gemini"},
                )
            except Exception:
                pass

        if lomin_pcm_to_wav is not None:
            wav_bytes = lomin_pcm_to_wav(pcm_bytes, sample_rate=24000)
        else:
            from backend.ble_framing import _wrap_pcm_in_wav

            wav_bytes = _wrap_pcm_in_wav(pcm_bytes, sample_rate=24000)

        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={"X-TTS-Engine": cfg.tts_engine or "gemini"},
        )

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
    parser.add_argument(
        "--host",
        default=os.environ.get("MVG_BIND", "0.0.0.0"),
        help="Bind host for --serve (default: MVG_BIND or 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MVG_PORT", "8771")),
        help="Bind port for --serve (default: MVG_PORT or 8771)",
    )
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


# Alias for target symbol parity across Zero and Gotchi codebases
GotchiZeroAdapter = GotchiZeroBrainAdapter
