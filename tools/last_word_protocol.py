#!/usr/bin/env python3
"""
tools/last_word_protocol.py - Last Word Protocol & Bot Cooldown Manager
Prevents infinite bot-to-bot banter loops in shared channels (#lounge, #the-banana-stand).
When Zero and a peer bot (e.g., Aerial, Amos, Marvin) exchange a threshold of uninterrupted
messages back and forth without human participation (default: 4 messages), Zero delivers
ONE final 'last word' closing message and responses to that bot are paused for a configured
duration (default: 30 minutes).
"""

import json
import os
import sys
import time
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PT_TZ = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
COOLDOWNS_FILE = DATA_DIR / "bot_cooldowns.json"

DEFAULT_THRESHOLD = 4
DEFAULT_PAUSE_MINUTES = 3
LOUNGE_CHANNEL_ID = 1534452820995080192
ZERO_BOT_ID = 1542285964213358633

# In-flight last word tracking to prevent mid-turn steering collisions
_in_flight_last_words: set[tuple[str, str]] = set()


def _normalize_key(identifier: int | str | None) -> str:
    if identifier is None:
        return ""
    return str(identifier).strip().lower()


def load_bot_cooldowns() -> dict:
    """Load persistent bot cooldowns from disk, pruning expired entries."""
    cooldowns = {}
    if COOLDOWNS_FILE.exists():
        try:
            with open(COOLDOWNS_FILE, "r", encoding="utf-8") as f:
                cooldowns = json.load(f)
        except Exception as e:
            print(f"[LastWordProtocol] Warning reading {COOLDOWNS_FILE}: {e}")
            cooldowns = {}

    now = time.time()
    modified = False
    cleaned = {}
    for ch_id, bots in cooldowns.items():
        ch_cleaned = {}
        for b_key, record in bots.items():
            pause_until = float(record.get("pause_until", 0.0))
            if pause_until > now:
                ch_cleaned[b_key] = record
            else:
                modified = True
        if ch_cleaned:
            cleaned[ch_id] = ch_cleaned

    if modified:
        save_bot_cooldowns(cleaned)
    return cleaned


def save_bot_cooldowns(data: dict):
    """Save bot cooldowns atomically to disk."""
    try:
        tmp_file = COOLDOWNS_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(COOLDOWNS_FILE)
    except Exception as e:
        print(f"[LastWordProtocol] Error writing {COOLDOWNS_FILE}: {e}")


def is_bot_paused(
    channel_id: int | str,
    bot_id: int | str | None = None,
    bot_name: str | None = None,
) -> tuple[bool, float, dict | None]:
    """
    Check if responses to a specific bot are currently paused in this channel.
    Returns: (is_paused, remaining_seconds, record_details)
    """
    cooldowns = load_bot_cooldowns()
    ch_key = str(channel_id)
    ch_cooldowns = cooldowns.get(ch_key, {})
    if not ch_cooldowns:
        return False, 0.0, None

    now = time.time()
    candidates = []
    if bot_id is not None:
        candidates.append(_normalize_key(bot_id))
    if bot_name:
        candidates.append(_normalize_key(bot_name))

    for cand in candidates:
        if cand in ch_cooldowns:
            rec = ch_cooldowns[cand]
            pause_until = float(rec.get("pause_until", 0.0))
            if pause_until > now:
                return True, (pause_until - now), rec

    # Check case-insensitive match across all keys
    for b_key, rec in ch_cooldowns.items():
        rec_name = _normalize_key(rec.get("bot_name", ""))
        rec_id = _normalize_key(rec.get("bot_id", ""))
        for cand in candidates:
            if cand and (cand == rec_name or cand == rec_id):
                pause_until = float(rec.get("pause_until", 0.0))
                if pause_until > now:
                    return True, (pause_until - now), rec

    return False, 0.0, None


def pause_bot(
    channel_id: int | str,
    bot_id: int | str | None,
    bot_name: str | None,
    duration_seconds: float = 180.0,
    reason: str = "Last Word Protocol triggered",
) -> dict:
    """Pause responses to a specific bot in a channel for duration_seconds."""
    cooldowns = load_bot_cooldowns()
    ch_key = str(channel_id)
    if ch_key not in cooldowns:
        cooldowns[ch_key] = {}

    now = time.time()
    pause_until = now + duration_seconds
    now_dt = datetime.now(PT_TZ)
    until_dt = datetime.fromtimestamp(pause_until, tz=PT_TZ)

    clean_name = str(bot_name).strip() if bot_name else (str(bot_id) if bot_id else "unknown_bot")
    clean_id = str(bot_id).strip() if bot_id else ""

    record = {
        "bot_id": clean_id,
        "bot_name": clean_name,
        "channel_id": ch_key,
        "paused_at_pt": now_dt.strftime("%Y-%m-%d %I:%M:%S %p PT"),
        "pause_until": pause_until,
        "pause_until_pt": until_dt.strftime("%Y-%m-%d %I:%M:%S %p PT"),
        "duration_minutes": round(duration_seconds / 60.0, 1),
        "reason": reason,
    }

    primary_key = _normalize_key(clean_id) if clean_id else _normalize_key(clean_name)
    cooldowns[ch_key][primary_key] = record
    if clean_name and _normalize_key(clean_name) != primary_key:
        cooldowns[ch_key][_normalize_key(clean_name)] = record

    save_bot_cooldowns(cooldowns)
    # Clear any transient in-flight marker
    clear_last_word_in_flight(channel_id, clean_id or clean_name)
    print(
        f"[LastWordProtocol] Paused bot '{clean_name}' (ID: {clean_id}) in channel {channel_id} "
        f"for {duration_seconds/60:.1f}m until {record['pause_until_pt']}"
    )
    return record


def unpause_bot(channel_id: int | str | None, bot_identifier: str) -> bool:
    """Unpause responses to a specific bot in a channel (or all channels if channel_id is None)."""
    cooldowns = load_bot_cooldowns()
    norm_ident = _normalize_key(bot_identifier)
    removed_any = False

    target_channels = [str(channel_id)] if channel_id is not None else list(cooldowns.keys())
    for ch_key in target_channels:
        ch_cooldowns = cooldowns.get(ch_key, {})
        keys_to_remove = []
        for b_key, rec in ch_cooldowns.items():
            rec_name = _normalize_key(rec.get("bot_name", ""))
            rec_id = _normalize_key(rec.get("bot_id", ""))
            if norm_ident in (b_key, rec_name, rec_id):
                keys_to_remove.append(b_key)
        for k in keys_to_remove:
            del ch_cooldowns[k]
            removed_any = True
        if ch_cooldowns:
            cooldowns[ch_key] = ch_cooldowns
        elif ch_key in cooldowns:
            del cooldowns[ch_key]

    if removed_any:
        save_bot_cooldowns(cooldowns)
        clear_last_word_in_flight(channel_id or "", bot_identifier)
        print(f"[LastWordProtocol] Unpaused bot '{bot_identifier}' in channel(s): {target_channels}")
    return removed_any


def mark_last_word_in_flight(channel_id: int | str, bot_identifier: str):
    """Mark a last word turn as actively generating to prevent mid-turn interruptions."""
    global _in_flight_last_words
    _in_flight_last_words.add((str(channel_id), _normalize_key(bot_identifier)))


def is_last_word_in_flight(channel_id: int | str, bot_identifier: str) -> bool:
    """Check if a last word turn is actively in-flight for this bot."""
    return (str(channel_id), _normalize_key(bot_identifier)) in _in_flight_last_words


def clear_last_word_in_flight(channel_id: int | str, bot_identifier: str | None = None):
    """Clear in-flight last word status."""
    global _in_flight_last_words
    ch_key = str(channel_id)
    if bot_identifier:
        _in_flight_last_words.discard((ch_key, _normalize_key(bot_identifier)))
    else:
        _in_flight_last_words = {entry for entry in _in_flight_last_words if entry[0] != ch_key}


def is_zero_message(msg: dict, zero_bot_id: int | str = ZERO_BOT_ID) -> bool:
    """Check if a message entry was authored by Zero."""
    author = str(msg.get("author", "")).strip().lower()
    if author == "zero":
        return True
    if msg.get("is_bot") and "zero" in author:
        return True
    author_id = msg.get("author_id") or msg.get("id")
    if author_id and str(author_id) == str(zero_bot_id):
        return True
    return False


def is_target_bot_message(msg: dict, bot_id: int | str | None, bot_name: str) -> bool:
    """Check if a message entry was authored by the target peer bot."""
    if not msg.get("is_bot", False):
        return False
    if is_zero_message(msg):
        return False

    author = str(msg.get("author", "")).strip().lower()
    norm_name = _normalize_key(bot_name)
    if norm_name and author == norm_name:
        return True

    msg_aid = msg.get("author_id")
    if bot_id is not None and msg_aid and str(msg_aid) == str(bot_id):
        return True
    return False


def calculate_bot_streak(
    channel_id: int | str,
    bot_id: int | str | None,
    bot_name: str,
    history_msgs: list[dict] | None = None,
    max_gap_seconds: float = 900.0,
    zero_bot_id: int | str = ZERO_BOT_ID,
) -> tuple[int, list[dict]]:
    """
    Calculate the length of the current uninterrupted back-and-forth streak strictly between
    Zero and the target bot in this channel.
    
    Rules:
    - Traverses backwards from most recent message.
    - If a human speaks (not a bot, and not Zero), the streak is immediately broken (returns).
    - If a different bot speaks (neither Zero nor Target Bot), the streak is immediately broken.
    - If the time gap between consecutive messages in the streak > max_gap_seconds (15m), broken.
    - Requires that BOTH Zero and the target bot participated in the streak.
    
    Returns: (streak_count, streak_messages)
    """
    if history_msgs is None:
        try:
            from tools.channel_history import get_recent_messages
            history_msgs = get_recent_messages(channel_id, limit=30)
        except Exception:
            history_msgs = []

    if not history_msgs:
        return 0, []

    streak_msgs = []
    prev_ts = None

    for m in reversed(history_msgs):
        raw_ts = m.get("timestamp")
        msg_ts = None
        if raw_ts:
            try:
                if "UTC" in raw_ts:
                    dt = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                    msg_ts = dt.timestamp()
            except Exception:
                pass

        if prev_ts is not None and msg_ts is not None:
            if abs(prev_ts - msg_ts) > max_gap_seconds:
                # Time gap broke the conversational momentum
                break

        is_bot = bool(m.get("is_bot", False))
        is_zero = is_zero_message(m, zero_bot_id=zero_bot_id)
        is_target = is_target_bot_message(m, bot_id=bot_id, bot_name=bot_name)

        if not is_bot and not is_zero:
            # Human message: strictly breaks bot-to-bot loop!
            break

        if is_zero or is_target:
            streak_msgs.append(m)
            if msg_ts is not None:
                prev_ts = msg_ts
        else:
            # Different bot (e.g. Amos or Marvin while checking Aerial)
            break

    # Verify both Zero and Target Bot participated in the chain
    has_zero = any(is_zero_message(m, zero_bot_id=zero_bot_id) for m in streak_msgs)
    has_target = any(is_target_bot_message(m, bot_id=bot_id, bot_name=bot_name) for m in streak_msgs)

    if has_zero and has_target:
        return len(streak_msgs), streak_msgs
    return 0, []


def check_last_word_condition(
    channel_id: int | str,
    bot_id: int | str | None,
    bot_name: str,
    threshold: int = DEFAULT_THRESHOLD,
    history_msgs: list[dict] | None = None,
) -> tuple[bool, int]:
    """
    Check whether the Last Word Protocol should trigger on this turn.
    Returns: (is_last_word, streak_count)
    """
    streak, _ = calculate_bot_streak(
        channel_id=channel_id,
        bot_id=bot_id,
        bot_name=bot_name,
        history_msgs=history_msgs,
    )
    is_last_word = streak >= threshold
    return is_last_word, streak


def build_last_word_prompt_injection(bot_name: str, streak: int, pause_minutes: int) -> str:
    """Generate the concise directive to inject into Zero's prompt for the last word turn."""
    return (
        f"\n\n[LAST WORD PROTOCOL ACTIVE]:\n"
        f"You and {bot_name} have exchanged {streak} uninterrupted back-and-forth messages in this channel.\n"
        f"Under the Last Word Protocol, this is your FINAL message before pausing responses to {bot_name}.\n"
        f"CRITICAL INSTRUCTIONS:\n"
        f"1. Deliver one sharp, witty, mic-drop closing statement that definitively concludes this banter/exchange.\n"
        f"2. Do NOT ask questions, do NOT propose new topics, and do NOT invite or prompt a reply.\n"
        f"3. Deliver only your closing statement. Once sent, responses to {bot_name} in this channel will be paused for {pause_minutes} minutes.\n"
    )


# ---------------------------------------------------------------------------
# CLI Management & Auditing
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Last Word Protocol & Bot Cooldown Manager")
    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="Show active bot cooldowns across all channels")
    p_status.add_argument("--channel", "-c", help="Optional channel ID to filter")

    # pause
    p_pause = subparsers.add_parser("pause", help="Manually pause responses to a bot")
    p_pause.add_argument("--channel", "-c", default=str(LOUNGE_CHANNEL_ID), help="Channel ID (default: Lounge)")
    p_pause.add_argument("--bot", "-b", required=True, help="Bot name or snowflake ID")
    p_pause.add_argument("--minutes", "-m", type=int, default=DEFAULT_PAUSE_MINUTES, help="Pause duration in minutes")
    p_pause.add_argument("--reason", "-r", default="Manual operator command", help="Reason for pause")

    # unpause
    p_unpause = subparsers.add_parser("unpause", help="Unpause responses to a bot")
    p_unpause.add_argument("--channel", "-c", help="Optional channel ID (default: all channels)")
    p_unpause.add_argument("--bot", "-b", required=True, help="Bot name or snowflake ID")

    # streak
    p_streak = subparsers.add_parser("streak", help="Check current uninterrupted streak with a bot")
    p_streak.add_argument("--channel", "-c", default=str(LOUNGE_CHANNEL_ID), help="Channel ID (default: Lounge)")
    p_streak.add_argument("--bot", "-b", required=True, help="Bot name")

    args = parser.parse_args()

    if args.command == "status":
        cooldowns = load_bot_cooldowns()
        if args.channel:
            cooldowns = {args.channel: cooldowns.get(str(args.channel), {})}

        now = time.time()
        print("=== Active Bot Cooldowns ===")
        found = False
        for ch_id, bots in cooldowns.items():
            for b_key, rec in bots.items():
                pause_until = float(rec.get("pause_until", 0.0))
                if pause_until > now:
                    found = True
                    rem = int(pause_until - now)
                    print(f"• Channel {ch_id} | Bot: {rec.get('bot_name')} ({rec.get('bot_id')})")
                    print(f"  Until: {rec.get('pause_until_pt')} ({rem // 60}m {rem % 60}s remaining)")
                    print(f"  Reason: {rec.get('reason')}")
        if not found:
            print("No bots currently paused.")

    elif args.command == "pause":
        rec = pause_bot(
            channel_id=args.channel,
            bot_id=args.bot if args.bot.isdigit() else None,
            bot_name=args.bot if not args.bot.isdigit() else None,
            duration_seconds=args.minutes * 60.0,
            reason=args.reason,
        )
        print(f"Paused '{args.bot}' in channel {args.channel} until {rec['pause_until_pt']}.")

    elif args.command == "unpause":
        ok = unpause_bot(channel_id=args.channel, bot_identifier=args.bot)
        if ok:
            print(f"Unpaused '{args.bot}'.")
        else:
            print(f"No active pause found for '{args.bot}'.")

    elif args.command == "streak":
        streak, msgs = calculate_bot_streak(args.channel, None, args.bot)
        print(f"Current uninterrupted streak with '{args.bot}' in {args.channel}: {streak} messages")
        for m in reversed(msgs):
            print(f"  [{m.get('author')}]: {m.get('content')[:60]}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
