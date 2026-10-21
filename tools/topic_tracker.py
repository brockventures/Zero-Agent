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

# Ephemeral / operational noise patterns that should never be written to durable memory
_EPHEMERAL_NOISE_RE = re.compile(
    r"(?:\b(?:BUY|SELL)\s+\d+\s+[A-Z_]+\s+@\s+\d+|\bMOVE\s+TO\s+[A-Z0-9_-]+|\bHOLD\s+AT\s+[A-Z0-9_-]+"
    r"|STATION AGORA // COMBINE ROUND"
    r"|^(?:yielding the floor|standing by|acknowledged\.?\s*standing by|closing this one|no action needed|nothing more is needed)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def is_ephemeral_memory_noise(text: str, subject: str = "") -> bool:
    """Deterministic gate (#1) to veto ephemeral CI/build/trading/floor-yield noise before memory ingestion."""
    s = (text or "").strip()
    if not s or len(s) < 25:
        return True
    subj_low = (subject or "").strip().lower()
    if subj_low in ("", "none", "null", "status", "ping", "heartbeat", "agora-live-burst"):
        return True
    if _EPHEMERAL_NOISE_RE.search(s):
        return True
    # Pure PR/commit/CI status bump with no architectural substance
    if len(s) < 160 and re.search(
        r"\b(?:auto-merge armed|all checks passing|ci passed|merged pr #\d+|rebased onto main)\b",
        s,
        re.IGNORECASE,
    ):
        return True
    return False


def upsert_decision_resolution(
    subject: str,
    author_name: str,
    kind: str,
    summary_text: str,
    timestamp_str: str | None = None,
    decisions_path: Path = MEM_DECISIONS,
) -> bool:
    """Append or supersede a resolution in decisions.md (#1 noise gate + #3 explicit subject superseding)."""
    subj = (subject or "general-finding").strip()
    if is_ephemeral_memory_noise(summary_text, subj):
        return False

    now_pt_str = timestamp_str or datetime.now(PT).strftime("%Y-%m-%d %I:%M:%S %p PT")
    new_block = (
        f"### Resolution: `{subj}` ({now_pt_str})\n"
        f"* **Author**: {author_name}\n"
        f"* **Kind**: `{kind}`\n"
        f"* **Summary**: {summary_text.strip()}\n"
    )

    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    if not decisions_path.exists():
        decisions_path.write_text(f"# Crab Cavern Architectural Decisions\n\n{new_block}", encoding="utf-8")
        return True

    raw = decisions_path.read_text(encoding="utf-8", errors="replace")
    norm_summary = re.sub(r"\s+", " ", summary_text.strip()).lower()
    if norm_summary in re.sub(r"\s+", " ", raw).lower():
        return False

    parts = re.split(r"(?=\n### Resolution:)", raw)
    header = parts[0]
    blocks = parts[1:]

    # For specific named topics (not generic catch-alls), supersede older interim blocks of the same subject
    generic_subjects = {"general", "general-finding", "status", "lounge-chat"}
    kept_blocks = []
    superseded_count = 0
    for b in blocks:
        m = re.search(r"### Resolution:\s*`([^`]+)`", b)
        b_subj = m.group(1).strip() if m else ""
        if subj.lower() not in generic_subjects and b_subj.lower() == subj.lower():
            superseded_count += 1
            continue
        kept_blocks.append(b.strip())

    if superseded_count > 0:
        new_block = (
            f"### Resolution: `{subj}` ({now_pt_str})\n"
            f"* **Author**: {author_name}\n"
            f"* **Kind**: `{kind}` *(supersedes {superseded_count} prior `{subj}` entry/entries)*\n"
            f"* **Summary**: {summary_text.strip()}\n"
        )

    kept_blocks.append(new_block.strip())
    updated_text = header.rstrip() + "\n\n" + "\n\n".join(kept_blocks) + "\n"
    decisions_path.write_text(updated_text, encoding="utf-8")

    dot_mem = Path("/workspace/.agents/memory/crab_cavern/decisions.md")
    if dot_mem.parent.exists() and decisions_path == MEM_DECISIONS:
        try:
            dot_mem.write_text(updated_text, encoding="utf-8")
        except Exception:
            pass
    return True

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

    # Internalize resolution cleanly without posting synthetic emoji reactions

    # 2. Append or supersede in durable memory in /workspace/memory/crab_cavern/decisions.md
    try:
        clean_text = re.sub(r"```handoff.*?```", "", content, flags=re.DOTALL).strip()
        if is_ephemeral_memory_noise(clean_text, subject or ""):
            return False
        summary_text = _summarize_resolution(clean_text, subject or "general-finding", author_name)
        wrote = upsert_decision_resolution(
            subject=subject or "general-finding",
            author_name=author_name,
            kind=kind or "finding",
            summary_text=summary_text,
        )
        if wrote:
            print(f"[TopicTracker] Internalized '{subject}' into {MEM_DECISIONS}")
        return wrote
    except Exception as e:
        print(f"[TopicTracker] Error internalizing topic: {e}")
        return False

if __name__ == "__main__":
    print("Topic tracker ready.")
