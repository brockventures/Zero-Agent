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
_ALLOWED_VOICE_READ_PREFIXES_DEFAULT = ("/workspace/", "/baseball/")
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
        f"You are Zero, an autonomous AI engineering partner. "
        f"Current time: {now_pt}.\n"
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
        "in `#zero-chat`.\n"
        f"{vision_note}\n\n"
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
        "`#zero-chat` before executing. `read_local_file` is restricted to `os.path.realpath` under `/workspace/` and "
        "`/baseball/` (denying `.env`, `.ssh`, and secrets).\n"
        "- Use `search_zero_memory` to search durable memory or past transcripts.\n"
        "- Use `check_tasks_and_repos` to inspect `/workspace/data/tasks.json` and open GitHub issues.\n"
        "- Use `run_shell_command` to execute non-destructive commands in `/workspace` (destructive commands stage for confirmation).\n"
        "- Use `read_local_file` and `write_local_file` to inspect or write files on disk (overwrites stage for confirmation).\n"
        "- Use `web_and_reddit_search` for live Reddit or Amazon SerpApi queries.\n"
        "- Use `get_recent_discord_context` to read recent messages across Discord channels. Supports 'crab-cavern' multi-channel rollup across all Crab Cavern rooms, individual Crab Cavern channels ('lounge', 'gotchi', 'gotchi-chat', 'the-banana-stand', 'side-project', 'mirrormere', 'debug-huddle', 'agora', 'r-residentagents'), and Home channels ('zero-chat', 'zero-ops', 'homelab', 'home-assistant', 'finances', 'shopping', 'projects', '3d-print', 'steam-deck', 'baseball'). Never claim you lack access to Crab Cavern.\n"
        "- Use `post_note_to_zero_chat` to send clickable links, notes, code blocks, local images/files (`file_path`), "
        "or webpage screenshots (`screenshot_url`) directly to `#zero-chat`.\n"
        "- Use `get_weather_forecast` to check live weather conditions and high/low forecasts (defaults immediately to Local Home Area without asking for a city).\n\n"
        f"{snapshot}{_load_full_zero_persona_and_rules()}{mem_block}"
    )


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
        source = str(args.get("source", "reddit")).lower()
        if source == "amazon":
            options = (
                "I'm checking Amazon pricing and reviews for you now.",
                "Pulling up those product details on Amazon now.",
                "Looking up the current Amazon listings for you now.",
            )
        else:
            options = (
                "I'm looking that up on Reddit for you now.",
                "Pulling up the latest community threads on that now.",
                "Searching recent discussions on that for you now.",
            )
        return options[seed % len(options)]

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
                timeout=5,
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
                timeout=6,
            )
            return {"result": (res.stdout or res.stderr or "No results")[:3500]}
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

    async def _run_gemini_with_tools(
        self,
        prompt: str,
        image_bytes: Optional[bytes],
        burst_images: List[bytes],
        on_sentence: Optional[Callable[[str], Any]] = None,
        audio_bytes: Optional[bytes] = None,
    ) -> Tuple[str, bool, List[str]]:
        from backend.voice_gateway import SentenceStreamSplitter
        from google.genai import types

        client = self._get_genai_client()
        if client is None:
            return "Zero is online, but GEMINI_API_KEY is not configured in the environment.", False, []

        has_vision = bool(image_bytes or burst_images)
        prompt_is_placeholder = is_placeholder_prompt(prompt)
        sys_prompt_text = "" if (audio_bytes and prompt_is_placeholder) else prompt
        sys_prompt = await asyncio.to_thread(
            build_gotchi_zero_system_prompt, sys_prompt_text, has_vision
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
            user_parts.append(
                types.Part.from_text(
                    text=f"{prefix_txt}\nListen to Ryan's spoken voice prompt in the attached audio clip and answer directly."
                )
            )
        else:
            user_parts.append(types.Part.from_text(text=prompt or "Describe what you see."))

        contents: List[types.Content] = [types.Content(role="user", parts=user_parts)]
        tools_list = _build_gotchi_tools()

        explicitly_posted_to_zero_chat = False
        tool_had_error = False
        model_requested_deep_thinking = False
        emitted_sentences: List[str] = []
        accumulated_raw_parts: List[str] = []

        for _step in range(4):
            live_model = self._resolve_live_gemini_model()
            config = types.GenerateContentConfig(
                system_instruction=sys_prompt,
                tools=tools_list,
                temperature=0.4,
                thinking_config=build_thinking_config(
                    live_model,
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
                    model=live_model,
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
                    model=live_model,
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
                    # Pre-tool contextual action narration when the model invokes a tool without leading text
                    default_preamble = build_contextual_tool_filler(fn_name, fn_args, prompt)
                    await self._emit_streamed_sentence(
                        default_preamble, emitted_sentences, on_sentence
                    )

                res_dict = await execute_gotchi_tool_call(fn_name, fn_args)
                if fn_name == "post_note_to_zero_chat" or (
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

                worker_res = await self.warm_worker.run_turn(
                    call_prompt,
                    node_id=node_id,
                    session_id=session_id,
                    on_sentence=_forward_sentence,
                    on_tool_start=parsed_turn.get("_on_tool_start"),
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
) -> str:
    """Lomin plugin streaming brain hook."""
    adapter = GotchiZeroBrainAdapter()
    running_sentences: List[str] = []

    def _on_s(s_txt: str) -> None:
        running_sentences.append(s_txt)
        if on_partial is not None:
            on_partial(" ".join(running_sentences))

    res = asyncio.run(
        adapter.handle_turn(
            {
                "prompt": prompt,
                "node_id": target or "gotchi-ryan-01",
                "_on_sentence": _on_s,
            }
        )
    )
    reply = str(res.get("reply") or "")
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
