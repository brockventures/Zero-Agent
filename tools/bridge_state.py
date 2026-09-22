"""
Zero Discord Bridge - State, Session & Queue Persistence Module
Encapsulates all session mapping, turn tracking, compaction detection,
atomic JSON-backed queueing, beacon status, and restart intent tracking.
"""

import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT_TZ = ZoneInfo("America/Los_Angeles")

# Persistent storage directories and config files
DATA_DIR = Path("/workspace/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
ATTACHMENTS_DIR = DATA_DIR / "attachments"
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "runtime_config.json"
IN_FLIGHT_FILE = DATA_DIR / "in_flight_turn.json"
RESTART_INTENT_FILE = DATA_DIR / "restart_intent.json"
QUEUE_FILE = DATA_DIR / "turn_queue.json"
EXT_QUEUE_FILE = DATA_DIR / "external_turn_queue.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
SESSION_METADATA_FILE = DATA_DIR / "session_metadata.json"
RESET_SESSION_KEYS_FILE = DATA_DIR / "reset_session_keys.json"
BEACON_FILE = DATA_DIR / "liveness_beacon.json"
BOT_STATUS_FILE = DATA_DIR / "bot_status.json"
DAEMON_PIDS_FILE = DATA_DIR / "daemon_pids.json"
BOT_STATS_FILE = DATA_DIR / "bot_stats.json"
RUNTIME_RULES_FILE = Path("/workspace/config/runtime_rules.json")

READONLY_NOTIFICATION_CHANNELS = {
    1330447543477338202,  # #server-updates
    1210466877835313155,  # #downloads
}

TARGET_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "1542081375287640084"))
BROCK_GUILD_ID = int(os.getenv("DISCORD_BROCK_GUILD_ID", "1210466877294518272"))
OWNER_USER_ID = int(os.getenv("DISCORD_OWNER_ID", "179407724335988736"))
IVY_USER_ID = int(os.getenv("DISCORD_IVY_USER_ID", "1541205716948353074"))
BANANA_STAND_CHANNEL_ID = 1534436119888793750

OPERATIONS_CATEGORY_ID = 1544953274363412533
HOMELAB_CHANNEL_ID = 1544955535722545253
BROCK_HOUSE_CHANNEL_ID = 1550577908811178095  # #brock-house
VAULT_CHANNEL_ID = 1550577910757458015        # #vault (isolated memory tier)
DEFAULT_EXCLUDED_HOME_CHANNELS = {
    1548196929308065893,  # #baseball (dedicated to Ivy)
}
DEFAULT_HOME_CHANNELS = {
    TARGET_CHANNEL_ID,
    1544953275877556334,  # #home-assistant
    1544953277592899615,  # #steam-deck
    1544953279664889888,  # #zero-ops / #harness-management
    1544955532765560924,  # #finances
    HOMELAB_CHANNEL_ID,    # #homelab
    1544955538033348618,  # #shopping
    1548196930788524094,  # #projects
    BROCK_HOUSE_CHANNEL_ID,  # #brock-house
    VAULT_CHANNEL_ID,        # #vault
}
RETITLED_THREADS_FILE = DATA_DIR / "retitled_threads.json"


def is_home_channel(channel) -> bool:
    """Check if a Discord channel or thread belongs to Zero's home turf."""
    if not channel:
        return False

    rules = get_runtime_rules()
    ops_cat_id = rules.get("operations_category_id", OPERATIONS_CATEGORY_ID)
    home_ch_ids = set(rules.get("home_channel_ids", DEFAULT_HOME_CHANNELS))
    excluded_ch_ids = set(rules.get("excluded_home_channel_ids", DEFAULT_EXCLUDED_HOME_CHANNELS))

    ch_id = getattr(channel, "id", None)
    if ch_id in excluded_ch_ids:
        return False

    parent_id = getattr(channel, "parent_id", None)
    if parent_id and parent_id in excluded_ch_ids:
        return False

    if ch_id in home_ch_ids:
        return True

    cat_id = getattr(channel, "category_id", None)
    if cat_id == ops_cat_id:
        return True

    parent_id = getattr(channel, "parent_id", None)
    if parent_id and parent_id in home_ch_ids:
        return True

    parent = getattr(channel, "parent", None)
    if parent and getattr(parent, "category_id", None) == ops_cat_id:
        return True

    return False


def is_brock_guild(obj) -> bool:
    """Check if a message, channel, or thread belongs to the Brock Discord guild."""
    if not obj:
        return False
    guild = getattr(obj, "guild", None)
    if not guild and hasattr(obj, "channel"):
        guild = getattr(obj.channel, "guild", None)
    if isinstance(guild, (int, str)) and str(guild).isdigit():
        return int(guild) == BROCK_GUILD_ID
    guild_id = getattr(guild, "id", None)
    if isinstance(guild_id, (int, str)) and str(guild_id).isdigit():
        return int(guild_id) == BROCK_GUILD_ID
    return False


def is_thread_retitled(thread_id: int | str) -> bool:
    """Check if a thread has already received its post-turn retitle."""
    if RETITLED_THREADS_FILE.exists():
        try:
            with open(RETITLED_THREADS_FILE) as f:
                d = json.load(f)
                return str(thread_id) in d
        except Exception:
            pass
    return False


def mark_thread_retitled(thread_id: int | str):
    """Record that a thread has received its post-turn retitle."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        d = {}
        if RETITLED_THREADS_FILE.exists():
            try:
                with open(RETITLED_THREADS_FILE) as f:
                    d = json.load(f)
            except Exception:
                pass
        d[str(thread_id)] = time.time()
        if len(d) > 500:
            d = dict(sorted(d.items(), key=lambda x: x[1])[-500:])
        with open(RETITLED_THREADS_FILE, "w") as f:
            json.dump(d, f)
    except Exception as e:
        print(f"[BridgeState] Error recording retitled thread: {e}")


def record_restart_intent(reason: str, initiator: str = "user"):
    """Persist reason for restart before rebooting to enable rich startup briefings."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(RESTART_INTENT_FILE, "w") as f:
            json.dump({
                "reason": str(reason),
                "initiator": str(initiator),
                "timestamp": time.time(),
                "formatted_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }, f, indent=2)
    except Exception as e:
        print(f"[BridgeState] Error recording restart intent: {e}")


def record_in_flight(channel_id: int | str, prompt: str, conv_id: str = None, status_msg_id: int = None, pid: int = None):
    """Atomically record an in-flight turn for a channel to survive crashes/restarts."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        if IN_FLIGHT_FILE.exists():
            try:
                with open(IN_FLIGHT_FILE) as f:
                    raw = json.load(f)
                    if isinstance(raw, dict):
                        if "channel_id" in raw and "prompt" in raw and not any(isinstance(v, dict) for v in raw.values()):
                            data[str(raw.get("channel_id", TARGET_CHANNEL_ID))] = raw
                        else:
                            data = raw
            except Exception:
                data = {}
        cid_str = str(channel_id)
        prev = data.get(cid_str) or {}
        attempts = 1
        if isinstance(prev, dict) and prev.get("prompt") == prompt:
            attempts = prev.get("attempts", 1) + 1

        effective_pid = pid if pid is not None else (prev.get("pid") if isinstance(prev, dict) else None)

        entry = {
            "conv_id": conv_id,
            "status_msg_id": status_msg_id,
            "channel_id": int(channel_id) if str(channel_id).isdigit() else channel_id,
            "prompt": prompt,
            "attempts": attempts,
            "ts": time.time(),
        }
        if effective_pid is not None:
            entry["pid"] = effective_pid

        data[cid_str] = entry
        # Maintain top-level fields for legacy callers/tests checking single dict
        data["prompt"] = prompt
        data["channel_id"] = int(channel_id) if str(channel_id).isdigit() else channel_id
        data["conv_id"] = conv_id
        data["attempts"] = attempts
        data["ts"] = time.time()
        data["status_msg_id"] = status_msg_id
        if effective_pid is not None:
            data["pid"] = effective_pid

        tmp = IN_FLIGHT_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(IN_FLIGHT_FILE)
    except Exception as e:
        print(f"[BridgeState] Error recording in-flight: {e}")


def record_daemon_pids(pids: list[int] | set[int] | dict[str, int]) -> None:
    """Atomically record persistent worker PIDs for watchdog discovery."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if isinstance(pids, dict):
            pid_list = [int(v) for v in pids.values() if v is not None and str(v).isdigit()]
        else:
            pid_list = [int(p) for p in pids if p is not None and str(p).isdigit()]
        tmp = DAEMON_PIDS_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"pids": sorted(list(set(pid_list))), "updated_at": time.time()}, f, indent=2)
        tmp.replace(DAEMON_PIDS_FILE)
    except Exception as e:
        print(f"[BridgeState] Error recording daemon PIDs: {e}")


def get_daemon_pids() -> set[int]:
    """Retrieve recorded persistent worker PIDs from disk."""
    pids = set()
    if not DAEMON_PIDS_FILE.exists():
        return pids
    try:
        with open(DAEMON_PIDS_FILE) as f:
            data = json.load(f)
            if isinstance(data, dict):
                for p in data.get("pids", []):
                    if isinstance(p, int):
                        pids.add(p)
            elif isinstance(data, list):
                for p in data:
                    if isinstance(p, int):
                        pids.add(p)
    except Exception:
        pass
    return pids


def get_in_flight() -> dict:
    """Retrieve currently recorded in-flight turn data from disk."""
    if not IN_FLIGHT_FILE.exists():
        return {}
    try:
        with open(IN_FLIGHT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_all_active_pids() -> set[int]:
    """Return all active PIDs discovered across in-flight turns and daemon workers."""
    active = set(get_daemon_pids())
    try:
        in_flight = get_in_flight()
        if isinstance(in_flight, dict):
            if isinstance(in_flight.get("pid"), int):
                active.add(in_flight["pid"])
            for v in in_flight.values():
                if isinstance(v, dict) and isinstance(v.get("pid"), int):
                    active.add(v["pid"])
    except Exception:
        pass
    return active


def get_bot_stats() -> dict:
    """Retrieve persistent bot statistics including all-time message counters."""
    defaults = {
        "all_time_messages_sent": 5026,
        "all_time_turns": 776,
        "updated_at": time.time(),
    }
    if BOT_STATS_FILE.exists():
        try:
            with open(BOT_STATS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    defaults.update(d)
        except Exception:
            pass
    return defaults


def increment_bot_messages(count: int = 1) -> int:
    """Atomically increment all-time message count and persist to disk."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stats = get_bot_stats()
        stats["all_time_messages_sent"] = stats.get("all_time_messages_sent", 5026) + count
        stats["updated_at"] = time.time()
        tmp = BOT_STATS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
        tmp.replace(BOT_STATS_FILE)
        return stats["all_time_messages_sent"]
    except Exception as e:
        print(f"[BridgeState] Error incrementing bot messages: {e}")
        return 5026


def clear_in_flight(channel_id: int | str = None):
    """Atomically clear in-flight turn for a channel, unlinking if all cleared."""
    try:
        if not IN_FLIGHT_FILE.exists():
            return
        if channel_id is None:
            try:
                IN_FLIGHT_FILE.unlink()
            except Exception:
                pass
            return
        with open(IN_FLIGHT_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            cid_str = str(channel_id)
            if cid_str in data:
                del data[cid_str]
            # Check remaining channels
            rem = [k for k, v in data.items() if isinstance(v, dict) and "prompt" in v]
            if not rem:
                try:
                    IN_FLIGHT_FILE.unlink()
                except Exception:
                    pass
            else:
                first = data[rem[0]]
                data["prompt"] = first["prompt"]
                data["channel_id"] = first["channel_id"]
                data["conv_id"] = first["conv_id"]
                data["ts"] = first["ts"]
                data["status_msg_id"] = first.get("status_msg_id")
                tmp = IN_FLIGHT_FILE.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=2)
                tmp.replace(IN_FLIGHT_FILE)
    except Exception as e:
        print(f"[BridgeState] Error clearing in-flight: {e}")


def get_runtime_rules() -> dict:
    """Dynamically fetch runtime rules and prompts without requiring container restarts."""
    defaults = {
        "external_char_limit": 1950,
        "anti_cascade_delay_seconds": 4.0,
        "bot_word_floor": 4,
        "worker_memory_cap_mb": 2048,
        "max_parallel_workers": 5,
        "ambient_classifier_enabled": True,
        "ambient_relevance_threshold": 0.80,
        "auto_thread_escalation_enabled": False,
        "auto_thread_escalation_seconds": 180.0,
        "last_word_threshold": 6,
        "external_system_prompt": None,
        "external_prompt_path": "/workspace/config/prompts/crab_cavern_prompt.md",
    }
    if RUNTIME_RULES_FILE.exists():
        try:
            with open(RUNTIME_RULES_FILE) as f:
                d = json.load(f)
                defaults.update(d)
        except Exception:
            pass
    if not defaults.get("external_system_prompt"):
        prompt_path = Path(defaults.get("external_prompt_path", "/workspace/config/prompts/crab_cavern_prompt.md"))
        if prompt_path.exists():
            try:
                defaults["external_system_prompt"] = prompt_path.read_text()
            except Exception:
                pass
    return defaults


def get_session_metadata(sess_key: str) -> dict:
    """Retrieve metadata dictionary for a specific session key."""
    if SESSION_METADATA_FILE.exists():
        try:
            with open(SESSION_METADATA_FILE) as f:
                d = json.load(f)
                return d.get(sess_key, {})
        except Exception:
            pass
    return {}


def set_session_metadata(sess_key: str, data: dict):
    """Atomically set metadata fields for a specific session key."""
    try:
        d = {}
        if SESSION_METADATA_FILE.exists():
            try:
                with open(SESSION_METADATA_FILE) as f:
                    d = json.load(f)
            except Exception:
                d = {}
        cur = d.get(sess_key, {})
        cur.update(data)
        d[sess_key] = cur
        tmp = SESSION_METADATA_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
        tmp.replace(SESSION_METADATA_FILE)
    except Exception as e:
        print(f"[BridgeState] Failed saving session metadata: {e}")


def increment_session_turn(sess_key: str) -> int:
    """Increment turn counter for session and update last active timestamp."""
    meta = get_session_metadata(sess_key)
    turns = meta.get("turns", 0) + 1
    set_session_metadata(sess_key, {"turns": turns, "last_active": int(time.time())})
    return turns


def reset_session_meta(sess_key: str):
    """Reset turn counter and record compaction timestamp."""
    set_session_metadata(sess_key, {"turns": 0, "last_compacted": int(time.time())})


def get_reset_session_keys() -> set[str]:
    """Retrieve the set of session keys flagged for reset on next turn."""
    if RESET_SESSION_KEYS_FILE.exists():
        try:
            with open(RESET_SESSION_KEYS_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(str(x) for x in data)
        except Exception:
            pass
    return set()


def add_reset_session_key(key: str | int):
    """Persist a session key to be reset on next turn."""
    try:
        keys = get_reset_session_keys()
        keys.add(str(key))
        tmp = RESET_SESSION_KEYS_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(sorted(list(keys)), f, indent=2)
        tmp.replace(RESET_SESSION_KEYS_FILE)
    except Exception as e:
        print(f"[BridgeState] Error adding reset session key: {e}")


def remove_reset_session_key(key: str | int):
    """Remove a session key from the pending reset list."""
    try:
        keys = get_reset_session_keys()
        keys.discard(str(key))
        tmp = RESET_SESSION_KEYS_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(sorted(list(keys)), f, indent=2)
        tmp.replace(RESET_SESSION_KEYS_FILE)
    except Exception as e:
        print(f"[BridgeState] Error removing reset session key: {e}")


def clear_reset_session_keys():
    """Clear all pending reset session keys."""
    try:
        if RESET_SESSION_KEYS_FILE.exists():
            RESET_SESSION_KEYS_FILE.unlink()
    except Exception as e:
        print(f"[BridgeState] Error clearing reset session keys: {e}")


class PersistentSessionKeySet(set):
    """A set of session keys that automatically synchronizes additions and removals to disk."""

    def add(self, element):
        s_elem = str(element)
        super().add(s_elem)
        add_reset_session_key(s_elem)

    def remove(self, element):
        s_elem = str(element)
        super().discard(s_elem)
        remove_reset_session_key(s_elem)

    def discard(self, element):
        s_elem = str(element)
        super().discard(s_elem)
        remove_reset_session_key(s_elem)

    def clear(self):
        super().clear()
        clear_reset_session_keys()


def get_gif_turn_count(sess_key: str) -> int:
    """Retrieve number of turns since last reaction GIF was sent in this session/channel."""
    meta = get_session_metadata(sess_key)
    return meta.get("turns_since_gif", 0)


def increment_gif_turn(sess_key: str) -> int:
    """Increment the turns_since_gif counter for this session/channel."""
    meta = get_session_metadata(sess_key)
    count = meta.get("turns_since_gif", 0) + 1
    set_session_metadata(sess_key, {"turns_since_gif": count})
    return count


def reset_gif_turn(sess_key: str):
    """Reset the turns_since_gif counter for this session/channel to 0."""
    set_session_metadata(sess_key, {"turns_since_gif": 0})


def has_reaction_gif(text: str) -> bool:
    """Check if text contains a Tenor, Giphy, or direct image reaction GIF link."""
    if not text:
        return False
    return bool(
        re.search(
            r"https?://(?:www\.)?(?:tenor\.com/(?:view/|.*?-\d+)|giphy\.com/gifs/|\S+\.gif\b)",
            text,
            re.IGNORECASE,
        )
    )


def is_gif_disabled_for_channel(sess_key: str | int | None, channel_id: int | str | None = None) -> bool:
    """Check if reaction GIFs are disabled for a given channel or session key."""
    if sess_key is None and channel_id is None:
        return False

    candidates = set()
    if sess_key is not None:
        candidates.add(str(sess_key).strip().lower())
    if channel_id is not None:
        candidates.add(str(channel_id).strip().lower())

    # Hardcoded protection for #the-banana-stand
    banana_ids = {str(BANANA_STAND_CHANNEL_ID), "the-banana-stand", "banana-stand", "agent-chat"}
    if candidates.intersection(banana_ids):
        return True

    # Check configurable runtime rules
    rules = get_runtime_rules()
    disabled = rules.get("gif_disabled_channels", [])
    for item in disabled:
        if str(item).strip().lower() in candidates:
            return True

    return False


def get_gif_prompt_guidance(sess_key: str, channel_id: int | str | None = None) -> str:
    """Generate prompt guidance block for GIF cadence and contextual overrides."""
    if is_gif_disabled_for_channel(sess_key, channel_id):
        channel_label = "the-banana-stand" if str(sess_key) in (str(BANANA_STAND_CHANNEL_ID), "the-banana-stand", "banana-stand") else sess_key
        return (
            f"[GIF Policy (Channel: {channel_label})]: Reaction GIFs are STRICTLY DISABLED in this channel.\n"
            f"• Do NOT query gif_tool.py or include any reaction GIFs, Tenor links, or images.\n"
            f"• Keep responses focused strictly on technical analysis, code, and direct answers."
        )

    count = get_gif_turn_count(sess_key)
    status_str = "⚠️ DUE (>=5 turns without GIF)" if count >= 5 else f"Nominal ({count}/5-7 turns)"

    return (
        f"[GIF Cadence Tracker (Channel: {sess_key})]: {count} message(s) since last reaction GIF in this channel.\n"
        f"• Target Cadence: ~1 in 5-7 messages.\n"
        f"• Situational Query Strategy: Describe the situation or vibe of your upcoming message (use: python3 /workspace/tools/gif_tool.py \"<situation or vibe description>\"). Matches against curated situation and vibe metadata in canonical_gifs.json.\n"
        f"• Status: {status_str}.\n"
        f"• Contextual Overrides:\n"
        f"  - Serious / Critical Override: If the message/topic is serious, urgent, an outage, data entry, or sensitive, override and SKIP the GIF regardless of count.\n"
        f"  - Social / Banter Override: If the exchange is particularly social, humorous, or banter-laden, you may include a GIF even if count < 5.\n"
        f"  - Fast-Fail & Single-Shot Rule: GIF queries are strictly single-shot. If 404s or fails, bail out immediately and emit text—never inspect tool code or retry.\n"
        f"  - Formatting & Placement: Hyperlink text MUST strictly say 'GIF' (e.g. [GIF](<url>) or [GIF](url)). Always place the [GIF](<url>) link at the VERY END of your message (immediately before any [CHOICES: ...] block, never at the beginning).\n"
        f"  - Diagnostic Ping Override: If prompt is a latency/ping check ('ping', 'respond pong'), skip tool calls entirely."
    )



def check_compaction_needed(
    conv_id: str | None,
    current_turns: int,
    brain_root: Path = Path("/root/.gemini/antigravity-cli/brain")
) -> tuple[bool, str]:
    """Evaluate whether a session should be compacted based on turns, transcript size, step count, or age."""
    rules = get_runtime_rules()
    max_turns = int(rules.get("compaction_max_turns", 15))
    max_steps = int(rules.get("compaction_max_steps", 1500))
    max_mb = float(rules.get("compaction_max_mb", 2.0))

    if current_turns >= max_turns:
        return True, f"turn count reached {current_turns}/{max_turns}"

    if not conv_id:
        return False, ""

    brain_dir = brain_root / conv_id / ".system_generated" / "logs"
    transcript_path = brain_dir / "transcript.jsonl"

    if transcript_path.exists():
        try:
            st = transcript_path.stat()
            size_mb = st.st_size / (1024 * 1024)
            if size_mb >= max_mb:
                return True, f"transcript size ({size_mb:.2f} MB) exceeds {max_mb:.1f} MB ceiling"

            with open(transcript_path, "rb") as f:
                line_count = sum(1 for _ in f)
            if line_count >= max_steps:
                return True, f"transcript steps ({line_count}) exceeds {max_steps}-step ceiling"

            if (time.time() - st.st_mtime) > 86400:
                return True, "session age exceeds 24 hours"
        except Exception as e:
            print(f"[BridgeState] Error checking transcript compaction metrics: {e}")

    return False, ""


def get_channel_session_id(channel_id: int | str, mode: str, target_channel_id: int = TARGET_CHANNEL_ID) -> str | None:
    """Look up active conversation ID bound to a channel or thread."""
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE) as f:
                d = json.load(f)
                key = "home" if (mode == "home" and int(channel_id) == target_channel_id) else str(channel_id)
                return d.get(key)
        except Exception:
            return None
    return None


def set_channel_session_id(channel_id: int | str, mode: str, conv_id: str, target_channel_id: int = TARGET_CHANNEL_ID):
    """Atomically record conversation ID mapping for a channel or thread."""
    try:
        d = {}
        if SESSIONS_FILE.exists():
            try:
                with open(SESSIONS_FILE) as f:
                    d = json.load(f)
            except Exception:
                d = {}
        key = "home" if (mode == "home" and int(channel_id) == target_channel_id) else str(channel_id)
        old_conv_id = d.get(key)
        if old_conv_id != conv_id:
            d[key] = conv_id
            tmp = SESSIONS_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(d, f, indent=2)
            tmp.replace(SESSIONS_FILE)
            print(f"[BridgeState] Persisted session mapping: {key} -> {conv_id}")
            meta_update = {"conv_id": conv_id, "last_active": int(time.time())}
            if old_conv_id:
                meta_update["parent_conv_id"] = old_conv_id
                meta = get_session_metadata(key)
                hist = list(meta.get("conv_history", []))
                if not hist or hist[-1] != old_conv_id:
                    hist.append(old_conv_id)
                meta_update["conv_history"] = hist[-10:]
            set_session_metadata(key, meta_update)
    except Exception as e:
        print(f"[BridgeState] Failed persisting session mapping: {e}")


def clear_channel_session_id(channel_id: int | str, mode: str, target_channel_id: int = TARGET_CHANNEL_ID):
    """Clear conversation ID mapping and reset session metadata."""
    try:
        key = "home" if (mode == "home" and int(channel_id) == target_channel_id) else str(channel_id)
        old_conv_id = None
        if SESSIONS_FILE.exists():
            with open(SESSIONS_FILE) as f:
                d = json.load(f)
            if key in d:
                old_conv_id = d[key]
                del d[key]
                tmp = SESSIONS_FILE.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(d, f, indent=2)
                tmp.replace(SESSIONS_FILE)
                print(f"[BridgeState] Cleared session mapping for: {key}")
        reset_session_meta(key)
        if old_conv_id:
            meta = get_session_metadata(key)
            hist = list(meta.get("conv_history", []))
            if not hist or hist[-1] != old_conv_id:
                hist.append(old_conv_id)
            set_session_metadata(key, {
                "parent_conv_id": old_conv_id,
                "conv_id": None,
                "conv_history": hist[-10:]
            })
    except Exception as e:
        print(f"[BridgeState] Failed clearing session mapping: {e}")


def get_session_parent_id(sess_key: str) -> str | None:
    """Retrieve parent conversation ID for a session key if it was rotated."""
    meta = get_session_metadata(sess_key)
    return meta.get("parent_conv_id")


# Active model selection persistence
ACTIVE_MODEL = os.getenv("AGY_MODEL", "gemini-3.8-flash-high")
if CONFIG_FILE.exists():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
            if cfg.get("model"):
                ACTIVE_MODEL = cfg["model"]
    except Exception:
        pass


def get_active_model() -> str:
    global ACTIVE_MODEL
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
                if cfg.get("model"):
                    ACTIVE_MODEL = cfg["model"]
        except Exception:
            pass
    return ACTIVE_MODEL


def set_active_model(model_name: str) -> str:
    global ACTIVE_MODEL
    ACTIVE_MODEL = model_name
    save_runtime_config()
    return ACTIVE_MODEL


def save_runtime_config():
    """Save runtime settings (like active model) to persistent storage."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"model": ACTIVE_MODEL}, f, indent=2)
    except Exception as e:
        print(f"[BridgeState] Failed saving runtime config: {e}")


class PersistentTurnQueue:
    """Atomic JSON-backed async turn queue that survives restarts."""
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.queue = asyncio.Queue()
        self.pending_items = []

    def load_persisted(self):
        items = []
        if self.filepath.exists():
            try:
                with open(self.filepath) as f:
                    items = json.load(f)
            except Exception:
                items = []
        self.pending_items = []
        self._persist()
        return items

    def _persist(self):
        try:
            serializable = []
            for it in self.pending_items:
                if isinstance(it, dict) and "prompt" in it:
                    serializable.append({
                        "prompt": it["prompt"],
                        "attachments": it.get("attachments", [])
                    })
            tmp = self.filepath.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(serializable, f, indent=2)
            tmp.replace(self.filepath)
        except Exception as e:
            print(f"[TurnQueue] Persist error: {e}")

    async def put(self, item: dict):
        if isinstance(item, dict):
            item.setdefault("queued_at", time.perf_counter())
        self.pending_items.append(item)
        self._persist()
        await self.queue.put(item)

    async def get(self):
        return await self.queue.get()

    def task_done(self, item=None):
        if item is not None:
            if item in self.pending_items:
                try:
                    self.pending_items.remove(item)
                except ValueError:
                    pass
            else:
                prompt = item.get("prompt") if isinstance(item, dict) else None
                if prompt:
                    self.pending_items = [p for p in self.pending_items if p.get("prompt") != prompt]
            self._persist()
        self.queue.task_done()

    def empty(self):
        return self.queue.empty() and len(self.pending_items) == 0


def update_beacon(state: str = "IDLE", prompt: str = "", channel_id: int | str = None):
    """Update liveness beacon timestamp and state for watchdog monitoring."""
    try:
        clean_prompt = prompt or ""
        if "[CURRENT USER PROMPT]:" in clean_prompt:
            clean_prompt = clean_prompt.split("[CURRENT USER PROMPT]:", 1)[1].strip()
        elif "[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:" in clean_prompt:
            clean_prompt = re.sub(r"\[PREVIOUS SESSION CARRY-FORWARD CONTEXT\]:.*?(?=\n\n|\Z)", "", clean_prompt, flags=re.DOTALL).strip()
        clean_prompt = re.sub(r"<[^>]+>", "", clean_prompt).strip()
        clean_prompt = re.sub(r"\[System Time & Timezone\]:[^\n]*(?:\n\s*[•\-\*][^\n]*)*\n*", "", clean_prompt).strip()
        clean_prompt = re.sub(r"\[GIF Cadence Tracker[^\n]*(?:\n\s*[•\-\*][^\n]*)*\n*", "", clean_prompt).strip()
        clean_prompt = clean_prompt.strip()

        data = {}
        if BEACON_FILE.exists():
            try:
                with open(BEACON_FILE) as f:
                    data = json.load(f)
            except Exception:
                data = {}

        data.update({
            "state": state,
            "ts": time.time(),
            "time_pt": datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT"),
            "prompt": clean_prompt[:120] if clean_prompt else "",
            "channel_id": int(channel_id) if str(channel_id).isdigit() else channel_id
        })
        tmp = BEACON_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        tmp.replace(BEACON_FILE)
    except Exception:
        pass


def update_gateway_heartbeat(bot=None, status: str = "connected") -> dict:
    """Update dynamic Discord gateway heartbeat and latency in liveness_beacon.json."""
    data = {}
    try:
        if BEACON_FILE.exists():
            try:
                with open(BEACON_FILE) as f:
                    data = json.load(f)
            except Exception:
                data = {}

        now = time.time()
        latency = None
        if bot and hasattr(bot, "latency"):
            try:
                latency = round(bot.latency * 1000, 1)
            except Exception:
                pass

        data["gateway_heartbeat"] = now
        data["gateway_status"] = status
        data["gateway_latency_ms"] = latency
        data["gateway_time_pt"] = datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT")
        if "state" not in data:
            data["state"] = "IDLE"
            data["ts"] = now

        tmp = BEACON_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        tmp.replace(BEACON_FILE)
    except Exception:
        pass
    return data


def is_container_restart_intent(text: str) -> bool:
    """Detect explicit commands or natural language requests to restart the Docker container via SSH."""
    clean = text.strip().strip("'\"`“”‘’").strip().lower()
    if not clean:
        return False

    if clean in (
        "restart docker container", "restart container", "restart container now",
        "reboot docker container", "reboot container", "docker restart",
        "restart the docker container", "restart the container",
        "reboot the container", "reboot the docker container"
    ):
        return True

    pattern = r"^(hey\s+zero[,:\s]*)?(please\s+)?(can\s+you\s+)?(do\s+(a\s+)?)?(restart|reboot)\s+(the\s+)?(docker\s+container|container)(\s+now)?[.!?]*$"
    if re.match(pattern, clean, re.IGNORECASE):
        return True

    return False


def is_reload_intent(text: str) -> bool:
    """Detect explicit commands or natural language requests to restart/reload the bot/bridge in-place."""
    clean = text.strip().strip("'\"`“”‘’").strip().lower()
    if not clean:
        return False

    # Container restarts take precedence and are distinct
    if is_container_restart_intent(text):
        return False

    # Exact slash/bang commands
    if clean in ("!reload", "/reload", "!restart", "/restart", "!reboot", "/reboot"):
        return True

    # Single-word requests
    if clean in ("reload", "restart", "reboot"):
        return True

    # Common natural language phrases
    pattern = r"^(hey\s+zero[,:\s]*)?(please\s+)?(can\s+you\s+)?(do\s+(a\s+)?)?(restart|reload|reboot)(\s+(yourself|the\s+bridge|bridge|now|zero))?(\s+(now|in-place|in\s+place))?[.!?]*$"
    if re.match(pattern, clean, re.IGNORECASE):
        return True

    # Common conversational phrases
    if clean in (
        "yes restart", "yes reload", "yes reboot",
        "restart please", "reload please",
        "restart bridge now",
        "reload bridge now", "restart now", "reload now",
        "reboot now", "reload the bridge",
        "restart the bridge", "restart yourself please",
        "reboot yourself",
        "reload bridge in-place", "reload bridge in place",
        "restart bridge in-place", "restart bridge in place",
        "reload in-place", "reload in place"
    ):
        return True

    return False


def sync_credentials() -> bool:
    """Validate, sanitize, repair, and atomically mirror the Antigravity OAuth token.

    Prevents 'invalid character after top-level value' outages by:
    1. Detecting and stripping trailing bytes (e.g. non-truncated background daemon writes).
    2. Restoring from valid persistent backups if primary token is missing, empty, or corrupted.
    3. Writing atomically via temporary files and os.replace() with fsync to prevent partial reads.
    4. Mirroring clean, validated tokens to all persistent backup locations.
    """
    src = "/root/.gemini/antigravity-cli/antigravity-oauth-token"
    dsts = [
        "/root/.gemini/antigravity-oauth-token.bak",
        "/root/.config/antigravity/antigravity-oauth-token",
        "/root/.config/antigravity/antigravity-oauth-token.bak",
    ]

    def _is_valid_token(d: dict) -> bool:
        if not isinstance(d, dict):
            return False
        return "token" in d or "access_token" in d or "auth_method" in d

    def _atomic_write(path: str, content: str, mode: int = 0o600):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    parsed_data = None
    needs_repair = False

    # 1. Inspect primary source
    if os.path.exists(src):
        try:
            with open(src, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                try:
                    data = json.loads(raw)
                    if _is_valid_token(data):
                        parsed_data = data
                except json.JSONDecodeError:
                    # Trailing or leading garbage detected (e.g. non-truncated rewrite)
                    decoder = json.JSONDecoder()
                    start_brace = raw.find("{")
                    if start_brace != -1:
                        try:
                            data, end_idx = decoder.raw_decode(raw[start_brace:])
                            if _is_valid_token(data):
                                parsed_data = data
                                needs_repair = True
                                print(
                                    f"[BridgeState] Sanitized token with trailing garbage ({len(raw)} -> {end_idx} chars)"
                                )
                        except Exception as de:
                            print(f"[BridgeState] Failed to decode raw JSON token: {de}")
        except Exception as e:
            print(f"[BridgeState] Error reading token at {src}: {e}")

    # 2. If primary invalid or missing, recover from backups
    if not parsed_data:
        for b_path in dsts:
            if os.path.exists(b_path):
                try:
                    with open(b_path, "r", encoding="utf-8") as f:
                        b_raw = f.read().strip()
                    b_data = json.loads(b_raw)
                    if _is_valid_token(b_data):
                        parsed_data = b_data
                        needs_repair = True
                        print(f"[BridgeState] Recovered valid OAuth token from backup: {b_path}")
                        break
                except Exception:
                    continue

    if not parsed_data:
        print("[BridgeState] Warning: No valid OAuth token found in primary or backup locations.")
        return False

    clean_json_str = json.dumps(parsed_data, indent=2)

    # 3. Atomically repair primary if needed
    if needs_repair or not os.path.exists(src):
        try:
            _atomic_write(src, clean_json_str)
            print(f"[BridgeState] Atomically wrote sanitized token to {src}")
        except Exception as e:
            print(f"[BridgeState] Error writing sanitized token to {src}: {e}")
            return False

    # 4. Atomically mirror to backups (skipping writes if identical)
    for b_path in dsts:
        try:
            if os.path.exists(b_path):
                try:
                    with open(b_path, "r", encoding="utf-8") as f:
                        if f.read().strip() == clean_json_str.strip():
                            continue
                except Exception:
                    pass
            _atomic_write(b_path, clean_json_str)
        except Exception as e:
            print(f"[BridgeState] Failed to mirror token to {b_path}: {e}")

    return True
