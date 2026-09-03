#!/usr/bin/env python3
"""
channel_history.py - Multi-Agent Channel Message Buffer & Context Builder
Maintains a rolling sliding window of channel messages for multi-agent awareness,
enables passive observation without unsolicited interruption, and formats
recent channel history for LLM turns.
"""

import json
import time
import re
from pathlib import Path
from datetime import datetime, timezone
from collections import deque

DATA_DIR = Path("/workspace/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHANNEL_HISTORY_FILE = DATA_DIR / "channel_history.json"
MAX_HISTORY_PER_CHANNEL = 40

# Linked channel groups for cross-room awareness (e.g., Crab Cavern)
LINKED_CHANNEL_GROUPS = [
    {
        "1534452820995080192": "lounge",
        "1534436119888793750": "agent-chat",
    }
]

# In-memory deque store: channel_id (str) -> deque of message dicts
_history_store: dict[str, deque] = {}
_last_save_time = 0.0

def load_history() -> dict[str, deque]:
    """Load persistent history from disk into memory."""
    global _history_store
    if CHANNEL_HISTORY_FILE.exists():
        try:
            with open(CHANNEL_HISTORY_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                for ch_id, msgs in raw.items():
                    _history_store[str(ch_id)] = deque(msgs[-MAX_HISTORY_PER_CHANNEL:], maxlen=MAX_HISTORY_PER_CHANNEL)
        except Exception as e:
            print(f"[ChannelHistory] Warning: Could not load history from {CHANNEL_HISTORY_FILE}: {e}")
    return _history_store

def save_history(force: bool = False):
    """Persist history to disk, debounced to at most once every 5 seconds unless forced."""
    global _last_save_time
    now = time.time()
    if not force and (now - _last_save_time) < 5.0:
        return
    try:
        serializable = {ch_id: list(q) for ch_id, q in _history_store.items()}
        tmp = CHANNEL_HISTORY_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
        tmp.replace(CHANNEL_HISTORY_FILE)
        _last_save_time = now
    except Exception as e:
        print(f"[ChannelHistory] Warning: Could not save history to {CHANNEL_HISTORY_FILE}: {e}")

def record_message(
    channel_id: int | str,
    channel_name: str,
    author_name: str,
    is_bot: bool,
    content: str,
    msg_id: int | None = None,
    reply_to_id: int | None = None,
    timestamp: str | None = None
) -> dict:
    """Record an incoming or outgoing message into the channel history buffer."""
    ch_key = str(channel_id)
    if ch_key not in _history_store:
        _history_store[ch_key] = deque(maxlen=MAX_HISTORY_PER_CHANNEL)

    # Avoid duplicate message IDs if already recorded
    if msg_id is not None:
        for existing in _history_store[ch_key]:
            if existing.get("id") == msg_id:
                return existing

    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    entry = {
        "id": msg_id,
        "channel_id": str(channel_id),
        "channel_name": channel_name,
        "author": author_name,
        "is_bot": is_bot,
        "content": content,
        "reply_to": reply_to_id,
        "timestamp": timestamp
    }

    _history_store[ch_key].append(entry)
    save_history()
    return entry

def get_recent_messages(channel_id: int | str, limit: int = 15, exclude_msg_id: int | None = None) -> list[dict]:
    """Retrieve recent messages for a channel up to limit, oldest to newest."""
    ch_key = str(channel_id)
    if ch_key not in _history_store:
        load_history()

    msgs = list(_history_store.get(ch_key, []))
    if exclude_msg_id is not None:
        msgs = [m for m in msgs if m.get("id") != exclude_msg_id]
    return msgs[-limit:]

def format_channel_context(
    channel_id: int | str,
    limit: int = 15,
    exclude_msg_id: int | None = None,
    max_chars: int = 4000,
    include_linked_channels: bool = True,
    peer_limit: int = 10,
    max_peer_age_seconds: int = 3600,
    parent_channel_id: int | str | None = None
) -> str:
    """Format recent channel history into a clean, chronological context block for LLM turns,
    including linked peer channels (e.g. #lounge <-> #agent-chat) when configured."""
    recent = get_recent_messages(channel_id, limit=limit, exclude_msg_id=exclude_msg_id)
    active_block = ""
    if recent:
        lines = []
        total_len = 0
        # Walk newest to oldest to enforce max_chars budget, then reverse
        for m in reversed(recent):
            ts = m.get("timestamp", "").split(" ")[1] if " " in m.get("timestamp", "") else m.get("timestamp", "")
            author_label = f"{m.get('author', 'Unknown')}" + (" (bot)" if m.get("is_bot") else "")
            content = m.get("content", "").strip()
            # Truncate individual overly long message snippets if needed
            if len(content) > 600:
                content = content[:590] + "... [truncated]"
            line = f"[{ts}] {author_label}: {content}"
            if total_len + len(line) > max_chars:
                break
            lines.append(line)
            total_len += len(line)

        lines.reverse()
        ch_name = recent[-1].get("channel_name", "")
        header = f"--- RECENT CHANNEL HISTORY (#{ch_name or channel_id}, last {len(lines)} messages) ---"
        footer = "--- END RECENT CHANNEL HISTORY ---"
        active_block = f"{header}\n" + "\n".join(lines) + f"\n{footer}"

    peer_blocks = []
    if include_linked_channels:
        ch_key = str(channel_id)
        p_key = str(parent_channel_id) if parent_channel_id else None
        now_ts = datetime.now(timezone.utc)
        for group in LINKED_CHANNEL_GROUPS:
            if ch_key in group or (p_key and p_key in group):
                for peer_id, peer_name in group.items():
                    if peer_id == ch_key or (p_key and peer_id == p_key):
                        continue
                    peer_msgs = get_recent_messages(peer_id, limit=peer_limit)
                    if not peer_msgs:
                        continue

                    valid_peer_msgs = []
                    for pm in peer_msgs:
                        raw_ts = pm.get("timestamp")
                        if raw_ts and max_peer_age_seconds > 0:
                            try:
                                msg_dt = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                                if (now_ts - msg_dt).total_seconds() > max_peer_age_seconds:
                                    continue
                            except Exception:
                                pass
                        valid_peer_msgs.append(pm)

                    if not valid_peer_msgs:
                        continue

                    p_lines = []
                    p_len = 0
                    for pm in reversed(valid_peer_msgs):
                        ts = pm.get("timestamp", "").split(" ")[1] if " " in pm.get("timestamp", "") else pm.get("timestamp", "")
                        author_label = f"{pm.get('author', 'Unknown')}" + (" (bot)" if pm.get("is_bot") else "")
                        content = pm.get("content", "").strip()
                        if len(content) > 600:
                            content = content[:590] + "... [truncated]"
                        line = f"[{ts}] {author_label}: {content}"
                        if p_len + len(line) > 2500:
                            break
                        p_lines.append(line)
                        p_len += len(line)

                    p_lines.reverse()
                    p_header = f"--- CROSS-CHANNEL AWARENESS (#{peer_name or peer_id}, last {len(p_lines)} messages) ---"
                    p_footer = f"--- END CROSS-CHANNEL AWARENESS (#{peer_name or peer_id}) ---"
                    peer_blocks.append(f"{p_header}\n" + "\n".join(p_lines) + f"\n{p_footer}")

    blocks = [b for b in [active_block] + peer_blocks if b]
    return "\n\n".join(blocks)

def is_handoff_addressed_to_zero(content: str) -> bool:
    """
    Check if a message contains a v0 handoff block explicitly targeting Zero
    or requiring response from Zero.
    """
    try:
        from tools.handoff import parse_envelope
        env = parse_envelope(content)
        if not env:
            return False

        # Target or to specified
        target = str(env.get("to") or env.get("target") or env.get("recipient") or "").lower()
        if "zero" in target:
            return True

        # Subject or evidence referencing Zero
        subj = str(env.get("subject") or "").lower()
        if "zero" in subj and env.get("reply") != "none":
            return True

        # If reply is required and no specific other target is named
        if env.get("reply") == "required" and not target:
            return True

    except Exception:
        pass
    return False

# Initialize on module import
load_history()
