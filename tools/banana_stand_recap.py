#!/usr/bin/env python3
"""
banana_stand_recap.py - Scheduled Recap of #the-banana-stand Discussions for #lounge

Runs 30 minutes after robot discussions start (10:00 AM & 4:00 PM PT):
- Inspects recent messages from #the-banana-stand (1534436119888793750) within window (default: 45m).
- Checks Crab Cavern silence state; skips if silenced.
- Stays silent if no substantive discussion occurred.
- Summarizes discussion topic, key points, and outcome into 2-3 plain-English sentences.
- Formats cleanly for #lounge (zero handoff envelopes, zero banana prefixes, URLs in < >).
- Dispatches to #lounge (1534452820995080192) via tools.outbox.queue_outbox_message.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Ensure repo root is in sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

PT_TZ = ZoneInfo("America/Los_Angeles")

BANANA_STAND_CHANNEL_ID = "1534436119888793750"
LOUNGE_CHANNEL_ID = "1534452820995080192"
DEFAULT_WINDOW_MINUTES = 45


def get_discord_token() -> str:
    """Retrieve Discord bot token from environment or /secrets/env.json."""
    token = os.getenv("BANANA_WATCHER_TOKEN")
    if not token and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json", "r", encoding="utf-8") as f:
                d = json.load(f)
                token = d.get("BANANA_WATCHER_TOKEN") or d.get("DISCORD_BOT_TOKEN")
        except Exception:
            pass
    if not token:
        token = os.getenv("DISCORD_BOT_TOKEN")
    return token or ""


def get_gemini_api_key() -> str:
    """Retrieve Gemini API key from environment or /secrets/env.json."""
    key = os.getenv("GEMINI_API_KEY")
    if not key and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json", "r", encoding="utf-8") as f:
                d = json.load(f)
                key = d.get("GEMINI_API_KEY")
        except Exception:
            pass
    return key or ""


def parse_message_timestamp(msg: dict) -> float | None:
    """Extract epoch timestamp from message dict across various formats."""
    raw_ts = msg.get("timestamp")
    if isinstance(raw_ts, (int, float)):
        return float(raw_ts)

    if isinstance(raw_ts, str):
        # Format 1: "YYYY-MM-DD HH:MM:SS UTC"
        try:
            if "UTC" in raw_ts:
                dt = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                return dt.timestamp()
        except Exception:
            pass

        # Format 2: ISO 8601 string
        try:
            iso_str = raw_ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            pass

    # Fallback to Discord snowflake ID
    msg_id = msg.get("id")
    if msg_id:
        try:
            return ((int(msg_id) >> 22) + 1420070400000) / 1000.0
        except Exception:
            pass

    return None


def fetch_messages_rest(channel_id: str, token: str, limit: int = 50) -> list[dict]:
    """Fetch messages directly from Discord REST API."""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "BananaStandRecap/1.0 (CrabCavern; https://github.com/brockventures)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    # Discord REST returns newest first; reverse to chronological order
                    return list(reversed(data))
    except Exception:
        pass
    return []


def fetch_recent_banana_stand_messages(
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    channel_id: str = BANANA_STAND_CHANNEL_ID,
    now_ts: float | None = None,
) -> list[dict]:
    """
    Fetch messages from the channel within the past window_minutes.
    Uses tools.channel_history as primary source, with Discord REST fallback.
    """
    now = time.time() if now_ts is None else now_ts
    cutoff_ts = now - (window_minutes * 60)

    # 1. Try channel_history
    raw_msgs: list[dict] = []
    try:
        import tools.channel_history as channel_history
        raw_msgs = channel_history.get_recent_messages(channel_id, limit=100)
    except Exception:
        raw_msgs = []

    # Filter channel_history messages
    window_msgs = []
    for m in raw_msgs:
        ts = parse_message_timestamp(m)
        if ts is not None and ts >= cutoff_ts:
            window_msgs.append(m)

    if window_msgs:
        return window_msgs

    # 2. If channel_history had no window messages, attempt Discord REST if token available
    token = get_discord_token()
    if token:
        try:
            rest_msgs = fetch_messages_rest(channel_id, token, limit=50)
            for m in rest_msgs:
                ts = parse_message_timestamp(m)
                if ts is not None and ts >= cutoff_ts:
                    window_msgs.append(m)
        except Exception:
            pass

    return window_msgs


def clean_message_content(content: str) -> str:
    """Strip handoff code blocks, banana prefixes, and formatting bloat."""
    if not content:
        return ""
    # Strip handoff code blocks: ```handoff ... ``` or ```json ... ```
    cleaned = re.sub(r"```(?:handoff|json)?\s*\{.*?\}\s*```", "", content, flags=re.DOTALL)
    # Strip banana prefixes/emojis
    cleaned = re.sub(r"^[🍌\s]+", "", cleaned)
    return cleaned.strip()


def wrap_urls(text: str) -> str:
    """Ensure any HTTP/HTTPS URLs are wrapped in angle brackets < >."""
    def _replace(match):
        url = match.group(1)
        trailing = ""
        while url and url[-1] in ".,!?;:":
            trailing = url[-1] + trailing
            url = url[:-1]
        return f"<{url}>{trailing}"

    return re.sub(r"(?<!<)(https?://[^\s<>]+)(?!>)", _replace, text)


def clean_summary_formatting(text: str) -> str:
    """Enforce #lounge formatting rules on generated summary."""
    # Strip any handoff blocks
    text = re.sub(r"```(?:handoff|json)?\s*\{.*?\}\s*```", "", text, flags=re.DOTALL)
    # Strip banana prefixes/emojis
    text = re.sub(r"[🍌]", "", text)
    text = re.sub(r"^\s*Banana Stand Recap:\s*", "", text, flags=re.IGNORECASE)
    # Enclose URLs in < >
    text = wrap_urls(text)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_substantive_discussion(messages: list[dict]) -> tuple[bool, list[dict]]:
    """
    Evaluate whether substantive robot discussion occurred within the window.
    Returns (is_substantive, substantive_messages).
    """
    substantive = []
    total_len = 0
    trivial_words = {"heartbeat", "ping", "pong", "ok", "ack", "seen", "status"}

    for m in messages:
        clean = clean_message_content(m.get("content", ""))
        if clean and clean.lower() not in trivial_words:
            substantive.append(m)
            total_len += len(clean)

    # Require at least one substantive message and >= 20 characters of substantive discussion
    if substantive and total_len >= 20:
        return True, substantive
    return False, []


def _heuristic_summary(messages: list[dict]) -> str:
    """Deterministic fallback summary when LLM generation is unavailable."""
    cleaned_snippets = []
    for m in messages:
        c = clean_message_content(m.get("content", ""))
        author = m.get("author", "Agent")
        if c:
            cleaned_snippets.append(f"{author}: {c}")

    if not cleaned_snippets:
        return "The robots held a brief discussion in #the-banana-stand with no action items recorded."

    first = cleaned_snippets[0].split("\n")[0]
    if len(first) > 120:
        first = first[:117] + "..."
    last = cleaned_snippets[-1].split("\n")[0]
    if len(last) > 120:
        last = last[:117] + "..."

    s1 = f"In #the-banana-stand, discussion centered on {first}."
    s2 = f"The exchange concluded with {last}."
    s3 = "No blockers or unresolved handoffs remain."
    return clean_summary_formatting(f"{s1} {s2} {s3}")


def summarize_discussion(messages: list[dict]) -> str:
    """
    Summarize banana stand robot discussion into 2-3 plain-English sentences for #lounge.
    Calls Gemini API if available, falling back to deterministic synthesis.
    """
    if not messages:
        return ""

    api_key = get_gemini_api_key()
    if api_key:
        try:
            from google import genai

            client = genai.Client(api_key=api_key)

            transcript_lines = []
            for m in messages:
                author = m.get("author", "Unknown")
                c = clean_message_content(m.get("content", ""))
                if c:
                    transcript_lines.append(f"[{author}]: {c}")
            transcript = "\n".join(transcript_lines)

            prompt = (
                "You are an engineering assistant summarizing robot collaboration discussions for humans in #lounge.\n"
                "The following is a recent conversation from #the-banana-stand between automated agents and developers.\n\n"
                "Summarize the discussion for #lounge following these strict rules:\n"
                "1. Provide exactly 2 to 3 concise sentences in plain English.\n"
                "2. State the discussion topic, key points or tradeoffs, and the final outcome or consensus.\n"
                "3. Do NOT use banana emojis (🍌), do NOT include handoff envelopes, JSON syntax, or bot prefixes.\n"
                "4. Wrap any URLs in angle brackets like <https://example.com> to suppress link previews.\n"
                "5. Output only the 2-3 sentence paragraph with no headers or bullet lists.\n\n"
                f"Transcript:\n{transcript}"
            )

            model_name = os.getenv("ACTIVE_MODEL", "gemini-2.5-flash")
            if not model_name or "gemini" not in model_name:
                model_name = "gemini-2.5-flash"

            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if resp and resp.text:
                summary = clean_summary_formatting(resp.text.strip())
                if summary:
                    return summary
        except Exception:
            pass

    return _heuristic_summary(messages)


def run_recap(
    dispatch: bool = True,
    dry_run: bool = False,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    force: bool = False,
    now_ts: float | None = None,
) -> dict:
    """
    Evaluate and optionally dispatch the #the-banana-stand recap to #lounge.
    """
    from tools.bridge_state import is_crab_cavern_silenced

    silenced, reason = is_crab_cavern_silenced()
    if silenced and not force:
        return {
            "status": "silenced",
            "message": f"Crab Cavern silenced. Recap skipped ({reason}).",
        }

    msgs = fetch_recent_banana_stand_messages(
        window_minutes=window_minutes,
        channel_id=BANANA_STAND_CHANNEL_ID,
        now_ts=now_ts,
    )
    has_substance, substantive_msgs = is_substantive_discussion(msgs)

    if not has_substance:
        return {
            "status": "silent",
            "message": "No substantive discussion occurred within window. Staying silent.",
            "message_count": len(msgs),
        }

    recap_text = summarize_discussion(substantive_msgs)
    if not recap_text:
        return {
            "status": "silent",
            "message": "Could not generate substantive recap. Staying silent.",
            "message_count": len(substantive_msgs),
        }

    if dry_run or not dispatch:
        return {
            "status": "dry_run",
            "recap": recap_text,
            "message_count": len(substantive_msgs),
        }

    from tools.outbox import queue_outbox_message

    res = queue_outbox_message(
        channel=LOUNGE_CHANNEL_ID,
        content=recap_text,
        source_turn="banana_stand_recap",
        dedupe_window=3600,
        force=True,
    )

    return {
        "status": "dispatched",
        "outbox_result": res,
        "recap": recap_text,
        "message_count": len(substantive_msgs),
    }


def main():
    parser = argparse.ArgumentParser(description="Recap #the-banana-stand robot discussion for #lounge")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch recap to #lounge via outbox")
    parser.add_argument("--dry-run", action="store_true", help="Preview recap without dispatching")
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES, help="Minutes of history to inspect (default: 45)")
    parser.add_argument("--force", action="store_true", help="Bypass Crab Cavern silence check")
    args = parser.parse_args()

    # If --dry-run is given or --dispatch is not given, run in dry-run mode
    should_dispatch = args.dispatch and not args.dry_run
    is_dry_run = args.dry_run or (not args.dispatch)

    res = run_recap(
        dispatch=should_dispatch,
        dry_run=is_dry_run,
        window_minutes=args.window_minutes,
        force=args.force,
    )
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
