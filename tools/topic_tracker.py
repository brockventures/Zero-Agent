#!/usr/bin/env python3
"""
topic_tracker.py - Active Topic & Resolution Tracker for Multi-Agent Collaboration
Tracks in-flight topics/questions Zero initiates and manages silent internalization
when conclusions/answers arrive with reply: none.
"""

import os
import json
import time
import re
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
TOPICS_FILE = DATA_DIR / "active_topics.json"
MEM_DECISIONS = Path("/workspace/memory/crab_cavern/decisions.md")

def _summarize_resolution(clean_text: str, subject: str, author_name: str) -> str:
    """Extract or synthesize a concise technical resolution summary using LLM when appropriate."""
    lines = [line.strip() for line in clean_text.splitlines() if line.strip()]
    if not lines:
        return "(No textual content in resolution payload)"

    # Fast path for very short messages
    if len(clean_text) <= 150:
        return " ".join(lines)

    # Use LLM model to synthesize a crisp durable memory bullet
    prompt = (
        f"Synthesize this resolution from {author_name} regarding '{subject}' into a concise 1-2 sentence "
        f"technical takeaway for durable engineering memory. Do not include introductory fluff or filler.\n\n"
        f"Text:\n{clean_text[:1500]}"
    )
    try:
        res = subprocess.run(
            [
                "agy",
                "--model=gemini-3.8-flash-low",
                "--disable-slash-commands",
                f"-p={prompt}"
            ],
            capture_output=True,
            text=True,
            timeout=15
        )
        if res.returncode == 0 and res.stdout.strip():
            # Return cleaned output
            out = res.stdout.strip()
            out = re.sub(r"^([*•\-\s]+)", "", out)
            return out
    except Exception as e:
        print(f"[TopicTracker] LLM summarization fallback: {e}")

    # Fallback to key lines
    snippet = " ".join(lines[:4])
    return snippet[:350] + ("..." if len(snippet) > 350 else "")

def _load_topics() -> dict:
    if TOPICS_FILE.exists():
        try:
            with open(TOPICS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_topics(topics: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOPICS_FILE, "w") as f:
        json.dump(topics, f, indent=2)

def record_outbound_topic(subject: str, kind: str, message_id: int | str, channel_id: int | str):
    """Record a topic Zero opened, awaiting peer answer."""
    if not subject:
        return
    topics = _load_topics()
    topics[subject] = {
        "subject": subject,
        "kind": kind,
        "message_id": str(message_id),
        "channel_id": str(channel_id),
        "status": "OPEN",
        "created_at": time.time()
    }
    _save_topics(topics)
    print(f"[TopicTracker] Registered active outbound topic: {subject}")

def check_and_resolve_topic(envelope: dict, content: str, author_name: str, message_id: int | str, channel_id: int | str) -> bool:
    """
    Check if inbound envelope resolves an open topic or carries high-value durable knowledge.
    Returns True if internalized.
    """
    if not envelope:
        return False

    subject = envelope.get("subject")
    kind = envelope.get("kind")
    topics = _load_topics()

    is_resolution = False
    if subject and subject in topics and topics[subject].get("status") == "OPEN":
        topics[subject]["status"] = "RESOLVED"
        topics[subject]["resolved_at"] = time.time()
        topics[subject]["resolved_by"] = author_name
        _save_topics(topics)
        is_resolution = True
        print(f"[TopicTracker] Open topic '{subject}' resolved by {author_name}!")
    elif kind in ("answer", "correction", "finding"):
        is_resolution = True

    if not is_resolution:
        return False

    # 1. Place non-waking acknowledgment reaction on Discord
    token = os.getenv("DISCORD_BOT_TOKEN")
    if token and message_id and channel_id:
        try:
            emoji = urllib.parse.quote("✅")
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me"
            req = urllib.request.Request(url, headers={"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (Zero, 1.0)"}, method="PUT")
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
            print(f"[TopicTracker] Placed ✅ reaction on message {message_id}")
        except Exception as re_err:
            print(f"[TopicTracker] Warning: Failed adding reaction: {re_err}")

    # 2. Append to durable memory in /workspace/memory/crab_cavern/decisions.md
    try:
        clean_text = re.sub(r"```handoff.*?```", "", content, flags=re.DOTALL).strip()
        summary_text = _summarize_resolution(clean_text, subject or "general-finding", author_name)
        now_pt_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M:%S %p PT")

        entry = f"\n\n### Resolution: `{subject or 'general-finding'}` ({now_pt_str})\n"
        entry += f"* **Author**: {author_name}\n"
        entry += f"* **Kind**: `{kind}`\n"
        entry += f"* **Summary**: {summary_text}\n"


        MEM_DECISIONS.parent.mkdir(parents=True, exist_ok=True)
        with open(MEM_DECISIONS, "a") as f:
            f.write(entry)

        dot_mem = Path("/workspace/.agents/memory/crab_cavern/decisions.md")
        if dot_mem.parent.exists():
            with open(dot_mem, "a") as f:
                f.write(entry)

        print(f"[TopicTracker] Internalized '{subject}' into {MEM_DECISIONS}")
        return True
    except Exception as e:
        print(f"[TopicTracker] Error internalizing topic: {e}")
        return False

if __name__ == "__main__":
    print("Topic tracker ready.")
