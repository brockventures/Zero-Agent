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
RUNTIME_RULES_FILE = Path("/workspace/config/runtime_rules.json")

READONLY_NOTIFICATION_CHANNELS = {
    1330447543477338202,  # #server-updates
    1210466877835313155,  # #downloads
}

TARGET_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "1542081375287640084"))
OWNER_USER_ID = int(os.getenv("DISCORD_OWNER_ID", "1210466877294518272"))

OPERATIONS_CATEGORY_ID = 1544953274363412533
HOMELAB_CHANNEL_ID = 1544955535722545253
DEFAULT_HOME_CHANNELS = {
    TARGET_CHANNEL_ID,
    1544953275877556334,  # #home-assistant
    1544953277592899615,  # #steam-deck
    1544953279664889888,  # #zero-ops / #harness-management
    1544955532765560924,  # #finances
    HOMELAB_CHANNEL_ID,    # #homelab
    1544955538033348618,  # #shopping
}
RETITLED_THREADS_FILE = DATA_DIR / "retitled_threads.json"


def is_home_channel(channel) -> bool:
    """Check if a Discord channel or thread belongs to Zero's home turf."""
    if not channel:
        return False

    rules = get_runtime_rules()
    ops_cat_id = rules.get("operations_category_id", OPERATIONS_CATEGORY_ID)
    home_ch_ids = set(rules.get("home_channel_ids", DEFAULT_HOME_CHANNELS))

    ch_id = getattr(channel, "id", None)
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
                "reason": reason,
                "initiator": initiator,
                "timestamp": time.time(),
                "formatted_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }, f, indent=2)
    except Exception as e:
        print(f"[BridgeState] Error recording restart intent: {e}")


def record_in_flight(channel_id: int | str, prompt: str, conv_id: str = None, status_msg_id: int = None):
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

        data[cid_str] = {
            "conv_id": conv_id,
            "status_msg_id": status_msg_id,
            "channel_id": int(channel_id) if str(channel_id).isdigit() else channel_id,
            "prompt": prompt,
            "attempts": attempts,
            "ts": time.time()
        }
        # Maintain top-level fields for legacy callers/tests checking single dict
        data["prompt"] = prompt
        data["channel_id"] = int(channel_id) if str(channel_id).isdigit() else channel_id
        data["conv_id"] = conv_id
        data["attempts"] = attempts
        data["ts"] = time.time()
        data["status_msg_id"] = status_msg_id

        tmp = IN_FLIGHT_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(IN_FLIGHT_FILE)
    except Exception as e:
        print(f"[BridgeState] Error recording in-flight: {e}")


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
        "max_parallel_workers": 3,
        "ambient_classifier_enabled": True,
        "ambient_relevance_threshold": 0.80,
        "auto_thread_escalation_enabled": False,
        "auto_thread_escalation_seconds": 180.0,
        "external_system_prompt": None
    }
    if RUNTIME_RULES_FILE.exists():
        try:
            with open(RUNTIME_RULES_FILE) as f:
                d = json.load(f)
                defaults.update(d)
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


def get_gif_prompt_guidance(sess_key: str) -> str:
    """Generate prompt guidance block for GIF cadence, diversity, and contextual overrides."""
    count = get_gif_turn_count(sess_key)
    status_str = "⚠️ DUE (>=5 turns without GIF)" if count >= 5 else f"Nominal ({count}/5-7 turns)"

    diversity_lines = []
    try:
        from tools.gif_tool import get_cooldown_summary
        summary = get_cooldown_summary()
        cds = summary.get("cooldowns", {})
        if cds:
            cd_items = [
                f"{v['display_name']} (BLOCKED - {v['distance']}/{v['threshold']})"
                for v in cds.values()
            ]
            diversity_lines.append(f"• Active Franchise Cooldown(s): [{', '.join(cd_items)}].")
        eligible = summary.get("eligible", [])
        if eligible:
            diversity_lines.append(f"• Comedic Rotation: {', '.join(eligible[:6])}.")
        if cds:
            diversity_lines.append("• Diversity Rule: Do NOT query cooled-down franchises. Rotate across eligible comedic universes.")
    except Exception:
        pass

    diversity_block = ("\n" + "\n".join(diversity_lines)) if diversity_lines else ""

    return (
        f"[GIF Cadence Tracker (Channel: {sess_key})]: {count} message(s) since last reaction GIF in this channel.\n"
        f"• Target Cadence: ~1 in 5-7 messages (use: python3 /workspace/tools/gif_tool.py \"<query>\")."
        f"{diversity_block}\n"
        f"• Status: {status_str}.\n"
        f"• Contextual Overrides:\n"
        f"  - Serious / Critical Override: If the message/topic is serious, urgent, an outage, data entry, or sensitive, override and SKIP the GIF regardless of count.\n"
        f"  - Social / Banter Override: If the exchange is particularly social, humorous, or banter-laden, you may include a GIF even if count < 5."
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
        if d.get(key) != conv_id:
            d[key] = conv_id
            tmp = SESSIONS_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(d, f, indent=2)
            tmp.replace(SESSIONS_FILE)
            print(f"[BridgeState] Persisted session mapping: {key} -> {conv_id}")
            set_session_metadata(key, {"conv_id": conv_id, "last_active": int(time.time())})
    except Exception as e:
        print(f"[BridgeState] Failed persisting session mapping: {e}")


def clear_channel_session_id(channel_id: int | str, mode: str, target_channel_id: int = TARGET_CHANNEL_ID):
    """Clear conversation ID mapping and reset session metadata."""
    try:
        key = "home" if (mode == "home" and int(channel_id) == target_channel_id) else str(channel_id)
        if SESSIONS_FILE.exists():
            with open(SESSIONS_FILE) as f:
                d = json.load(f)
            if key in d:
                del d[key]
                tmp = SESSIONS_FILE.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(d, f, indent=2)
                tmp.replace(SESSIONS_FILE)
                print(f"[BridgeState] Cleared session mapping for: {key}")
        reset_session_meta(key)
    except Exception as e:
        print(f"[BridgeState] Failed clearing session mapping: {e}")


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

        data = {
            "state": state,
            "ts": time.time(),
            "time_pt": datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT"),
            "prompt": clean_prompt[:120] if clean_prompt else "",
            "channel_id": int(channel_id) if str(channel_id).isdigit() else channel_id
        }
        tmp = BEACON_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        tmp.replace(BEACON_FILE)
    except Exception:
        pass


def is_reload_intent(text: str) -> bool:
    """Detect explicit commands or natural language requests to restart/reload the bot/bridge."""
    clean = text.strip().lower()
    if not clean:
        return False

    # Exact slash/bang commands
    if clean in ("!reload", "/reload", "!restart", "/restart", "!reboot", "/reboot"):
        return True

    # Single-word requests
    if clean in ("reload", "restart", "reboot"):
        return True

    # Common natural language phrases
    pattern = r"^(hey\s+zero[,:\s]*)?(please\s+)?(can\s+you\s+)?(do\s+(a\s+)?)?(restart|reload|reboot)(\s+(yourself|the\s+bridge|the\s+container|container|now|zero|bridge))?(\s+(now|in-place|in\s+place))?[.!?]*$"
    if re.match(pattern, clean, re.IGNORECASE):
        return True

    # Common conversational phrases
    if clean in (
        "yes restart", "yes reload", "yes reboot",
        "restart please", "reload please",
        "restart container now", "restart bridge now",
        "reload bridge now", "restart now", "reload now",
        "reboot now", "restart the container", "reload the bridge",
        "restart the bridge", "restart yourself please",
        "reboot yourself", "reboot the container",
        "reload bridge in-place", "reload bridge in place",
        "restart bridge in-place", "restart bridge in place",
        "reload in-place", "reload in place"
    ):
        return True

    return False


def sync_credentials():
    """Mirror OAuth token to persistent storage locations."""
    src = "/root/.gemini/antigravity-cli/antigravity-oauth-token"
    dsts = [
        "/root/.gemini/antigravity-oauth-token.bak",
        "/root/.config/antigravity/antigravity-oauth-token"
    ]
    if os.path.exists(src):
        for dst in dsts:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            except Exception as e:
                print(f"[BridgeState] Failed to mirror token to {dst}: {e}")
