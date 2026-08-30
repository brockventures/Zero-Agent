import asyncio
import io
import os
import re
import select
import pty
import json
import signal
import uuid
import sys
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import discord
from discord.ext import commands

# Ensure line-buffered stdout for real-time docker logs
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

def _handle_sigterm(signum, frame):
    print("[Bridge] Received SIGTERM, exiting gracefully for restart...")
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_sigterm)

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

def find_new_artifacts(start_time: float) -> list[Path]:
    """Find newly created artifact files in the brain conversation directory."""
    try:
        brain_root = Path("/root/.gemini/antigravity-cli/brain")
        if not brain_root.exists():
            return []
        conv_dirs = [d for d in brain_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not conv_dirs:
            return []
        latest_conv = max(conv_dirs, key=lambda d: d.stat().st_mtime)
        artifacts = []
        for item in latest_conv.iterdir():
            if item.is_file() and not item.name.startswith("."):
                if item.stat().st_mtime >= start_time - 1.0:
                    artifacts.append(item)
        return artifacts
    except Exception:
        return []

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "1542081375287640084"))
OWNER_USER_ID = int(os.getenv("DISCORD_OWNER_ID", "1210466877294518272"))
PRINT_TIMEOUT = os.getenv("AGY_PRINT_TIMEOUT", "30m")

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
RUNTIME_RULES_FILE = Path("/workspace/config/runtime_rules.json")
READONLY_NOTIFICATION_CHANNELS = {
    1330447543477338202,  # #server-updates
    1210466877835313155,  # #downloads
}

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
        print(f"[Bridge] Error recording restart intent: {e}")

def get_runtime_rules() -> dict:
    """Dynamically fetch runtime rules and prompts without requiring container restarts."""
    defaults = {
        "external_char_limit": 1950,
        "anti_cascade_delay_seconds": 4.0,
        "bot_word_floor": 4,
        "ambient_classifier_enabled": True,
        "ambient_relevance_threshold": 0.80,
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

SESSION_METADATA_FILE = DATA_DIR / "session_metadata.json"

def get_session_metadata(sess_key: str) -> dict:
    if SESSION_METADATA_FILE.exists():
        try:
            with open(SESSION_METADATA_FILE) as f:
                d = json.load(f)
                return d.get(sess_key, {})
        except Exception:
            pass
    return {}

def set_session_metadata(sess_key: str, data: dict):
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
        print(f"[Bridge] Failed saving session metadata: {e}")

def increment_session_turn(sess_key: str) -> int:
    meta = get_session_metadata(sess_key)
    turns = meta.get("turns", 0) + 1
    set_session_metadata(sess_key, {"turns": turns, "last_active": int(time.time())})
    return turns

def reset_session_meta(sess_key: str):
    set_session_metadata(sess_key, {"turns": 0, "last_compacted": int(time.time())})

def check_compaction_needed(conv_id: str | None, current_turns: int) -> tuple[bool, str]:
    """Evaluate whether a session should be compacted based on turns, transcript size, step count, or age."""
    if current_turns >= 25:
        return True, f"turn count reached {current_turns}/25"
    
    if not conv_id:
        return False, ""
        
    brain_dir = Path("/root/.gemini/antigravity-cli/brain") / conv_id / ".system_generated" / "logs"
    transcript_path = brain_dir / "transcript.jsonl"
    
    if transcript_path.exists():
        try:
            st = transcript_path.stat()
            size_mb = st.st_size / (1024 * 1024)
            if size_mb >= 2.0:
                return True, f"transcript size ({size_mb:.2f} MB) exceeds 2.0 MB ceiling"
            
            if st.st_size > 250_000:
                with open(transcript_path, "rb") as f:
                    line_count = sum(1 for _ in f)
                if line_count >= 200:
                    return True, f"transcript steps ({line_count}) exceeds 200-step ceiling"
            
            if (time.time() - st.st_mtime) > 86400:
                return True, "session age exceeds 24 hours"
        except Exception as e:
            print(f"[Bridge] Error checking transcript compaction metrics: {e}")
            
    return False, ""

def get_channel_session_id(channel_id: int | str, mode: str) -> str | None:
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE) as f:
                d = json.load(f)
                key = "home" if (mode == "home" and int(channel_id) == TARGET_CHANNEL_ID) else str(channel_id)
                return d.get(key)
        except Exception:
            return None
    return None

def set_channel_session_id(channel_id: int | str, mode: str, conv_id: str):
    try:
        d = {}
        if SESSIONS_FILE.exists():
            try:
                with open(SESSIONS_FILE) as f:
                    d = json.load(f)
            except Exception:
                d = {}
        key = "home" if (mode == "home" and int(channel_id) == TARGET_CHANNEL_ID) else str(channel_id)
        if d.get(key) != conv_id:
            d[key] = conv_id
            tmp = SESSIONS_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(d, f, indent=2)
            tmp.replace(SESSIONS_FILE)
            print(f"[Bridge] Persisted session mapping: {key} -> {conv_id}")
            set_session_metadata(key, {"conv_id": conv_id, "last_active": int(time.time())})
    except Exception as e:
        print(f"[Bridge] Failed persisting session mapping: {e}")

def clear_channel_session_id(channel_id: int | str, mode: str):
    try:
        key = "home" if (mode == "home" and int(channel_id) == TARGET_CHANNEL_ID) else str(channel_id)
        if SESSIONS_FILE.exists():
            with open(SESSIONS_FILE) as f:
                d = json.load(f)
            if key in d:
                del d[key]
                tmp = SESSIONS_FILE.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(d, f, indent=2)
                tmp.replace(SESSIONS_FILE)
                print(f"[Bridge] Cleared session mapping for: {key}")
        reset_session_meta(key)
    except Exception as e:
        print(f"[Bridge] Failed clearing session mapping: {e}")

# Load persistent runtime config (e.g. model selection)
ACTIVE_MODEL = os.getenv("AGY_MODEL", "gemini-3.7-flash-high")
if CONFIG_FILE.exists():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
            if cfg.get("model"):
                ACTIVE_MODEL = cfg["model"]
    except Exception:
        pass

def save_runtime_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"model": ACTIVE_MODEL}, f, indent=2)
    except Exception as e:
        print(f"[Bridge] Failed saving runtime config: {e}")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Global execution & steering state
active_master_fd = None
active_proc = None
active_turn_task = None
active_status_msg = None
is_steering = False
is_ext_steering = False
reset_session = False
ext_active_proc = None
ext_active_master_fd = None
channel_last_bot_reply = {}
channel_active_procs = {}       # channel_id -> subprocess.Popen (for independent channel/thread steering)
thread_active_tasks = {}        # thread_id -> asyncio.Task (for concurrent background thread workers)
session_turn_counts = {}        # session_key -> turn count for 25-turn rolling auto-compaction

# Persistent turn queue across restarts
class PersistentTurnQueue:
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

BEACON_FILE = DATA_DIR / "liveness_beacon.json"

def update_beacon(state: str = "IDLE", prompt: str = ""):
    try:
        data = {
            "state": state,
            "ts": time.time(),
            "time_pt": datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT"),
            "prompt": prompt[:80] if prompt else ""
        }
        tmp = BEACON_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        tmp.replace(BEACON_FILE)
    except Exception:
        pass

PROCESSED_INTERACTIONS = set()

class ChoiceButton(discord.ui.Button):
    def __init__(self, label: str, custom_id: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        # Handled by global @bot.listen("on_interaction") to avoid double execution
        pass

class QuickChoiceView(discord.ui.View):
    def __init__(self, options: list[str], callback_fn=None, timeout: float = None):
        super().__init__(timeout=timeout)
        for idx, opt in enumerate(options[:5]):
            clean_label = opt.strip()
            if clean_label:
                self.add_item(ChoiceButton(label=clean_label[:80], custom_id=f"choice:{clean_label[:80]}"))

async def handle_button_choice(choice_text: str, interaction: discord.Interaction):
    global PROCESSED_INTERACTIONS
    if interaction.id in PROCESSED_INTERACTIONS:
        return
    PROCESSED_INTERACTIONS.add(interaction.id)
    if len(PROCESSED_INTERACTIONS) > 500:
        PROCESSED_INTERACTIONS.clear()
        PROCESSED_INTERACTIONS.add(interaction.id)

    try:
        await interaction.channel.typing()
    except Exception:
        pass
    selected_msg = await interaction.channel.send(f"🔘 **Selected:** `{choice_text}`")
    await turn_queue.put({
        "prompt": choice_text,
        "status_msg": None,
        "reply_target": selected_msg,
        "attachments": [],
        "is_steer": False,
        "mode": "home",
        "channel_id": interaction.channel_id
    })

home_turn_queue = PersistentTurnQueue(QUEUE_FILE)
turn_queue = home_turn_queue  # alias for backward compatibility with scheduler & buttons
ext_turn_queue = PersistentTurnQueue(EXT_QUEUE_FILE)
queue_worker_task = None
ext_queue_worker_task = None

def is_bridge_busy() -> list[str]:
    """Check if any task is actively running or queued across home and external channels."""
    home_busy = (active_proc is not None and active_proc.returncode is None) or not home_turn_queue.empty()
    ext_busy = (ext_active_proc is not None and ext_active_proc.returncode is None) or not ext_turn_queue.empty()
    busy = []
    if home_busy:
        busy.append("#zero-chat")
    if ext_busy:
        busy.append("Crab Cavern")
    return busy

async def warm_channel_history(channel, limit: int = 25):
    """Prefetch recent messages from Discord channel to initialize history buffer."""
    if not channel or not hasattr(channel, "history"):
        return
    try:
        from tools.channel_history import record_message
        msgs = []
        async for m in channel.history(limit=limit):
            msgs.append(m)
        msgs.reverse()
        for m in msgs:
            author_name = m.author.display_name or m.author.name
            reply_id = m.reference.message_id if m.reference else None
            record_message(
                channel_id=channel.id,
                channel_name=getattr(channel, "name", str(channel.id)),
                author_name=author_name,
                is_bot=m.author.bot,
                content=m.content,
                msg_id=m.id,
                reply_to_id=reply_id,
                timestamp=m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(m, "created_at") else None
            )
        print(f"[Bridge] Warmed channel history for #{getattr(channel, 'name', channel.id)}: {len(msgs)} messages loaded.")
    except Exception as e:
        print(f"[Bridge] Error warming channel history for {channel.id}: {e}")

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
                import shutil
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            except Exception as e:
                print(f"[Antigravity] Failed to mirror token to {dst}: {e}")

def format_command_preview(cmd_raw: str, max_len: int = 80) -> str:
    """Format command string for Discord status previews, stripping SSH boilerplate and showing host."""
    lines_list = cmd_raw.strip().splitlines()
    first_line = lines_list[0].strip() if lines_list else ""
    host_1 = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    host_2 = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

    if first_line.startswith("ssh "):
        host_tag = ""
        if host_1 in first_line:
            host_tag = f"[{host_1}]"
        elif host_2 in first_line:
            host_tag = f"[{host_2}]"
        
        parts = re.split(rf'(?:{re.escape(host_1)}|{re.escape(host_2)})\s+', first_line, maxsplit=1)
        if len(parts) > 1:
            inner_cmd = parts[1].strip().strip('"').strip("'")
            snip = inner_cmd[:max_len]
            return f"Running {host_tag}: {snip}..."
            
    snip = first_line[:max_len]
    return f"Running: {snip}..."

def convert_markdown_tables(text: str) -> str:
    """Convert raw markdown pipe tables into mobile-friendly Discord card lists with subtext."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect markdown table header followed by separator (|---|---|)
        if line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*[-:]+[-| :]*$", lines[i + 1]):
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2  # skip header and separator
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                table_rows.append(cells)
                i += 1

            for row in table_rows:
                if not row or not any(row):
                    continue
                first = re.sub(r"^\*\*|\*\*$", "", row[0]).strip()
                second = row[1] if len(row) > 1 else ""
                notes = " · ".join(c for c in row[2:] if c) if len(row) > 2 else ""

                if second and notes:
                    line_formatted = f"• **{first}** ({second}): {notes}"
                elif second:
                    line_formatted = f"• **{first}** ({second})"
                elif notes:
                    line_formatted = f"• **{first}**: {notes}"
                else:
                    line_formatted = f"• **{first}**"

                out.append(line_formatted)
            continue
        out.append(line)
        i += 1
    return "\n".join(out)

def format_for_discord(text: str) -> str:
    """Format markdown for clean Discord presentation:
    - Strips file:/// markdown links which render broken on Discord
    - Converts GitHub alerts (> [!NOTE]) to clean emoji callouts
    - Converts broken markdown pipe tables into clean mobile cards
    - Preserves standard https:// links
    """
    # 1. Clean file:/// links: [`/path`](file:///path) -> `/path`, [path](file:///path) -> `path`
    def clean_file_link(m):
        inner = m.group(1).strip()
        if inner.startswith("`") and inner.endswith("`"):
            return inner
        return f"`{inner}`"
    
    text = re.sub(r"\[([^\]]+)\]\(file://[^\)]*\)", clean_file_link, text)
    
    # 2. Strip internal action/progress pseudo-tags (e.g. <Action: ...>)
    text = re.sub(r"<\s*action:[^>]+>", "", text, flags=re.IGNORECASE)

    # 3. Convert GitHub-style alerts to emoji blockquotes
    alerts = {
        "NOTE": "ℹ️ **Note:**",
        "TIP": "💡 **Tip:**",
        "IMPORTANT": "📌 **Important:**",
        "WARNING": "⚠️ **Warning:**",
        "CAUTION": "🛑 **Caution:**",
    }
    for alert, emoji in alerts.items():
        text = re.sub(rf"^>\s*\[!{alert}\]", f"> {emoji}", text, flags=re.MULTILINE | re.IGNORECASE)
    
    # 3. Collapse unsupported h4+ headers (####+) to h3 (###) so Discord actually renders them as headings
    text = re.sub(r"^(#{4,})\s*(.*)$", r"### \2", text, flags=re.MULTILINE)

    # 4. Convert markdown pipe tables to clean Discord mobile cards
    text = convert_markdown_tables(text)

    return text.strip()

def extract_agent_response(raw_text: str) -> str:
    """Extract clean response text from agy output, supporting plain text, json, or stream-json."""
    # Thoroughly strip ANSI escape codes and terminal controls
    text = re.sub(r"\x1b(?:\[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", raw_text)

    # Check if this output is plain text (no JSON events present)
    has_json = False
    for line in text.splitlines():
        line_s = line.strip()
        if (line_s.startswith("{") and line_s.endswith("}")) or "\"event\":\"" in line_s or "\"conversation_id\":\"" in line_s:
            has_json = True
            break

    if not has_json:
        return format_for_discord(text)

    # Parse JSON if events are present
    accumulated_content = []
    final_result_response = ""
    error_response = ""

    for line in text.splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        start = line_str.find("{")
        end = line_str.rfind("}") + 1
        if start != -1 and end > start:
            json_substr = line_str[start:end]
            try:
                event = json.loads(json_substr)
                if isinstance(event, dict):
                    if "response" in event and event["response"]:
                        final_result_response = event["response"]

                    ev_type = event.get("event") or event.get("type")
                    if ev_type == "result" or "result" in event:
                        res = event.get("result", {})
                        if isinstance(res, dict):
                            if "response" in res and res["response"]:
                                final_result_response = res["response"]
                            elif "error" in res and res.get('error', ''):
                                error_response = f"Error: {res.get('error', '')}"
                    elif ev_type == "step_update" or "step_update" in event:
                        step = event.get("step_update", {})
                        if isinstance(step, dict):
                            if step.get("step_type") == "agent_response" and step.get("text_delta"):
                                accumulated_content.append(step["text_delta"])
                    elif ev_type in ("content", "message", "text", "delta"):
                        content = event.get("content") or event.get("text") or event.get("delta")
                        if content and isinstance(content, str):
                            accumulated_content.append(content)
            except Exception:
                pass

    if final_result_response:
        return format_for_discord(final_result_response)
    if error_response:
        return format_for_discord(error_response)
    if accumulated_content:
        return format_for_discord("".join(accumulated_content))

    # Fallback filter for JSON metadata lines
    clean_lines = []
    for l in text.splitlines():
        l_str = l.strip()
        if not l_str or (l_str.startswith("{") and l_str.endswith("}")):
            continue
        if "\"event\":\"" in l_str or "\"step_update\":\"" in l_str or "\"conversation_id\":\"" in l_str:
            continue
        clean_lines.append(l)

    if clean_lines:
        return format_for_discord("\n".join(clean_lines))

    return "*(Response completed, but no text output was generated)*"

def chunk_text(text: str, max_len: int = 1980) -> list[str]:
    """Split text into Discord-safe chunks while preserving markdown code block integrity.
    - If text is slightly over limit (e.g. 1980–2100 chars), collapses redundant empty lines
      and trailing whitespace to squeeze it into a single message without splitting.
    - Preserves code block formatting across chunks.
    - If a code block crosses a split boundary, cleanly closes it in the first chunk and re-opens it in the next.
    - Prefers breaking before code blocks so blocks stay intact whenever possible.
    """
    cleaned = text.strip()
    if len(cleaned) <= max_len:
        return [cleaned]

    # Boundary squeeze: if barely over limit (<=2100 chars), collapse whitespace to fit in 1 message
    if len(cleaned) <= 2100:
        condensed = re.sub(r'\n{3,}', '\n\n', cleaned)
        condensed = re.sub(r'[ \t]+\n', '\n', condensed).strip()
        if len(condensed) <= max_len:
            return [condensed]

    lines = cleaned.splitlines(keepends=True)
    chunks = []
    current_chunk = []
    current_len = 0
    in_code_block = False
    code_lang = ""

    for line in lines:
        stripped = line.strip()
        is_code_fence = stripped.startswith("```")

        # If entering a code block and current chunk already has substantial text (>half max_len),
        # break early so the entire code block starts cleanly on a new message.
        if is_code_fence and not in_code_block and current_len > (max_len // 2):
            if current_chunk:
                chunks.append("".join(current_chunk).strip())
                current_chunk = []
                current_len = 0

        overhead = (len(code_lang) + 12) if in_code_block else 0

        if current_len + len(line) + overhead > max_len:
            if current_chunk:
                if in_code_block:
                    current_chunk.append("\n```\n")
                chunks.append("".join(current_chunk).strip())
                current_chunk = []
                current_len = 0
                if in_code_block:
                    prefix = f"```{code_lang}\n"
                    current_chunk.append(prefix)
                    current_len = len(prefix)

        if is_code_fence:
            if not in_code_block:
                in_code_block = True
                code_lang = stripped[3:].strip()
            else:
                in_code_block = False
                code_lang = ""

        current_chunk.append(line)
        current_len += len(line)

    if current_chunk:
        chunks.append("".join(current_chunk).strip())

    return [c for c in chunks if c]

def convert_markdown_to_mobile_html(md_text: str) -> str:
    """Render markdown to mobile-friendly, dark-mode HTML that opens natively in Chrome on Android/Pixel."""
    try:
        import markdown
        body = markdown.markdown(md_text, extensions=['tables', 'fenced_code', 'nl2br'])
    except Exception:
        import html
        body = f"<pre>{html.escape(md_text)}</pre>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Zero Report</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.6;
    max-width: 850px;
    margin: 0 auto;
    padding: 16px 18px 40px 18px;
    background-color: #1e1f22;
    color: #dbdee1;
    font-size: 15px;
  }}
  h1, h2, h3, h4 {{
    color: #f2f3f5;
    font-weight: 600;
    margin-top: 1.4em;
    margin-bottom: 0.5em;
  }}
  h1 {{ font-size: 1.5em; border-bottom: 1px solid #35363c; padding-bottom: 8px; }}
  h2 {{ font-size: 1.3em; }}
  h3 {{ font-size: 1.1em; }}
  p {{ margin: 0.6em 0; }}
  code {{
    background: #2b2d31;
    color: #ebedef;
    padding: 2px 6px;
    border-radius: 4px;
    font-family: 'JetBrains Mono', 'Fira Code', Consolas, Monaco, monospace;
    font-size: 0.9em;
  }}
  pre {{
    background: #2b2d31;
    padding: 14px;
    border-radius: 8px;
    overflow-x: auto;
    border: 1px solid #35363c;
  }}
  pre code {{ background: none; padding: 0; font-size: 0.85em; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    margin: 16px 0;
    display: block;
    overflow-x: auto;
  }}
  th, td {{
    border: 1px solid #3f4147;
    padding: 8px 12px;
    text-align: left;
  }}
  th {{ background: #2b2d31; color: #ffffff; font-weight: 600; }}
  tr:nth-child(even) {{ background-color: #232428; }}
  blockquote {{
    margin: 12px 0;
    padding: 8px 14px;
    border-left: 4px solid #5865f2;
    background: #2b2d31;
    border-radius: 0 4px 4px 0;
  }}
  a {{ color: #00a8fc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  ul, ol {{ padding-left: 22px; margin: 0.6em 0; }}
  li {{ margin-bottom: 4px; }}
  hr {{ border: 0; height: 1px; background: #35363c; margin: 24px 0; }}
</style>
</head>
<body>
{body}
</body>
</html>"""

from zoneinfo import ZoneInfo
PT_TZ = ZoneInfo("America/Los_Angeles")

class KarakosScheduler:
    """Karakos-style persistent JSON-backed background scheduler for sidecars."""
    def __init__(self, dispatch_fn):
        self.dispatch_fn = dispatch_fn
        self._running = False
        self._task = None

    async def start(self):
        self._running = True
        # Check for missed jobs on startup (e.g. catchup if container was briefly offline)
        try:
            from tools.scheduler_tool import load_schedule, save_schedule, calculate_next_run
            jobs = load_schedule()
            now_ts = time.time()
            for j in jobs:
                if not j.get("enabled", True):
                    continue
                next_ts = j.get("next_run_ts", 0)
                if next_ts and now_ts > next_ts:
                    window = j.get("catchup_window_seconds", 7200)
                    if j.get("catchup_if_missed") and (now_ts - next_ts) <= window:
                        print(f"[KarakosScheduler] Catching up missed job: {j['name']}")
                        await self.dispatch_fn(j["prompt"], job_name=j['name'])
                    j["last_run_ts"] = now_ts
                    if j.get("schedule_type") == "one_shot":
                        j["enabled"] = False
                        j["next_run_ts"] = None
                    else:
                        j["next_run_ts"] = calculate_next_run(j, from_ts=now_ts)
            save_schedule(jobs)
        except Exception as e:
            print(f"[KarakosScheduler] Startup catchup error: {e}")

        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while self._running:
            try:
                from tools.scheduler_tool import load_schedule, save_schedule, calculate_next_run
                jobs = load_schedule()
                now_ts = time.time()
                updated = False

                for j in jobs:
                    if not j.get("enabled", True):
                        continue
                    next_ts = j.get("next_run_ts")
                    if not next_ts:
                        j["next_run_ts"] = calculate_next_run(j, from_ts=now_ts)
                        updated = True
                        continue

                    if now_ts >= next_ts:
                        j["last_run_ts"] = now_ts
                        j["last_run_at"] = datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M %p PT")
                        if j.get("schedule_type") == "one_shot":
                            j["enabled"] = False
                            j["next_run_ts"] = None
                        else:
                            j["next_run_ts"] = calculate_next_run(j, from_ts=now_ts)
                        print(f"[KarakosScheduler] Triggering job: {j['name']} (advanced next_run_ts to {j['next_run_ts']})")
                        # CRITICAL: Persist updated next_run_ts to disk BEFORE dispatching
                        # so any downstream failure or crash can NEVER cause an infinite retry loop
                        save_schedule(jobs)
                        try:
                            await self.dispatch_fn(j["prompt"], job_name=j['name'])
                        except Exception as de:
                            print(f"[KarakosScheduler] Error dispatching {j['name']}: {de}")

                # Liveness Beacon & Wedge Check (Karakos Pattern: >420s unbroken silence while PROCESSING)
                if BEACON_FILE.exists():
                    try:
                        with open(BEACON_FILE) as bf:
                            bdata = json.load(bf)
                        if bdata.get("state") == "PROCESSING":
                            has_running_proc = (
                                (active_proc is not None and active_proc.returncode is None) or
                                (ext_active_proc is not None and ext_active_proc.returncode is None)
                            )
                            if not has_running_proc:
                                # Stale beacon from before restart or completed turn: reset silently
                                update_beacon("IDLE", "")
                            else:
                                silence = now_ts - bdata.get("ts", now_ts)
                                if silence > 420 and not bdata.get("alerted"):
                                    print(f"[WedgeCheck] Agent silent for {silence:.0f}s during active turn!")
                                    bdata["alerted"] = True
                                    with open(BEACON_FILE, "w") as bf:
                                        json.dump(bdata, bf)
                                    ch = bot.get_channel(TARGET_CHANNEL_ID)
                                    if ch:
                                        await ch.send(f"⚠️ **Wedge Alert:** Agent has been silent for >{int(silence/60)}m without output. Prompt: `{bdata.get('prompt')}`")
                    except Exception:
                        pass
                # Zero-downtime bridge reload trigger
                reload_flag = DATA_DIR / "reload_bridge.flag"
                if reload_flag.exists():
                    busy = is_bridge_busy()
                    if not busy:
                        try:
                            reload_flag.unlink()
                        except Exception:
                            pass
                        print("[Bridge] Reload flag detected and queues idle. Executing in-place reload...")
                        try:
                            await bot.close()
                        except Exception:
                            pass
                        os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception as e:
                print(f"[KarakosScheduler] Error in loop: {e}")

            await asyncio.sleep(15)

scheduler = None
LAST_SCHEDULED_DISPATCH = {}

async def dispatch_scheduled_prompt(prompt: str, job_name: str = "Sidecar"):
    """Inject a scheduled sidecar prompt into the message queue with anti-storm guard."""
    global reset_session

    # Anti-storm guard: prevent any job from dispatching more than once per 5 minutes
    now_ts = time.time()
    last_disp = LAST_SCHEDULED_DISPATCH.get(job_name, 0)
    if (now_ts - last_disp) < 300:
        print(f"[Scheduler] Anti-storm block: {job_name} attempted re-trigger within 300s (last: {now_ts - last_disp:.1f}s ago). Dropping.")
        return
    LAST_SCHEDULED_DISPATCH[job_name] = now_ts

    if prompt == "[INTERNAL_SESSION_ROLLOVER]" or job_name == "Session Rollover":
        # Generate carry-forward summary BEFORE resetting session
        try:
            from tools.session_summarizer import generate_summary
            generate_summary()
            print("[Scheduler] Generated carry-forward summary before session rollover.")
        except Exception as e:
            print(f"[Scheduler] Error generating rollover summary: {e}")

        reset_session = True
        ch = bot.get_channel(TARGET_CHANNEL_ID)
        if ch:
            try:
                await ch.send("🌙 **Daily Session Rollover (2:00 AM PT)**: Conversation context refreshed with carry-forward context preserved.")
            except Exception:
                pass
        print("[Scheduler] Executed daily session rollover at 2:00 AM PT (reset_session=True).")
        return

    # Heartbeat sweep: silent execution unless degraded
    if job_name == "Heartbeat Sweep" or "sidecars.py heartbeat" in prompt:
        try:
            from tools.sidecars import run_heartbeat_sweep
            healthy, report = run_heartbeat_sweep()
            if not healthy:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    await ch.send(report)
        except Exception as e:
            print(f"[Scheduler] Heartbeat sweep execution error: {e}")
        return

    # Dated reminders: silent execution unless a reminder is due today
    if job_name == "Dated Reminders" or "sidecars.py reminders" in prompt:
        try:
            from tools.sidecars import run_dated_reminders
            has_due, rep = run_dated_reminders()
            if has_due and rep:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    await ch.send(rep)
            else:
                print("[Scheduler] Dated reminders checked: 0 due today (silent).")
        except Exception as e:
            print(f"[Scheduler] Dated reminders execution error: {e}")
        return

    # EV9 listing monitor: silent execution on Mon-Sat; only posts Sunday digest
    if job_name == "EV9 Listing Monitor" or "sidecars.py ev9" in prompt:
        try:
            from tools.sidecars import run_ev9_monitor
            has_digest, rep, plot_path = run_ev9_monitor(force_digest=False)
            if has_digest and rep:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    file = discord.File(plot_path) if plot_path and os.path.exists(plot_path) else None
                    await ch.send(rep, file=file)
            else:
                print("[Scheduler] EV9 monitor: daily capture completed (silent; no digest or trend plot).")
        except Exception as e:
            print(f"[Scheduler] EV9 monitor execution error: {e}")
        return

    # Antigravity CLI update check: silent execution unless a new version is available
    if "update_antigravity.py" in prompt or job_name == "Antigravity CLI Check":
        try:
            res = subprocess.run(["python3", "/workspace/tools/update_antigravity.py", "check", "--quiet"], capture_output=True, text=True)
            out = res.stdout.strip()
            if out:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out)
                    if choice_view:
                        await ch.send(clean_content, view=choice_view)
                    else:
                        await ch.send(clean_content)
            else:
                print("[Scheduler] Antigravity CLI check: up to date (silent).")
        except Exception as e:
            print(f"[Scheduler] Antigravity CLI check execution error: {e}")
        return

    # Dockhand container image check: silent execution unless an update is available
    if "dockhand_update.py" in prompt or job_name == "Dockhand Image Check":
        try:
            res = subprocess.run(["python3", "/workspace/tools/dockhand_update.py", "check", "--quiet"], capture_output=True, text=True)
            out = res.stdout.strip()
            if out:
                ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
                if ch:
                    clean_content, choice_view = parse_interactive_choices(out)
                    if choice_view:
                        await ch.send(clean_content, view=choice_view)
                    else:
                        await ch.send(clean_content)
            else:
                print("[Scheduler] Dockhand image check: up to date on both NAS hosts (silent).")
        except Exception as e:
            print(f"[Scheduler] Dockhand image check execution error: {e}")
        return

    ch = bot.get_channel(TARGET_CHANNEL_ID)
    if not ch:
        try:
            ch = await bot.fetch_channel(TARGET_CHANNEL_ID)
        except Exception as e:
            print(f"[Scheduler] Could not fetch channel {TARGET_CHANNEL_ID}: {e}")
            return
    status_msg = await ch.send(f"⏱️ **[Scheduled: {job_name}]** *Starting execution...*")
    await turn_queue.put({
        "prompt": prompt,
        "status_msg": status_msg,
        "reply_target": status_msg,
        "attachments": [],
        "is_steer": False
    })

def scrub_credentials(text: str) -> str:
    """Scrub internal tokens, passwords, API keys, and homelab private IPs from outbound text."""
    if not text:
        return text

    env_keys = [
        "DISCORD_BOT_TOKEN", "HA_ACCESS_TOKEN", "TAUTULLI_API_KEY",
        "MARKETCHECK_API_KEY", "CLOUDFLARE_API_TOKEN", "UPTIMEROBOT_API_KEY",
        "SERPAPI_API_KEY"
    ]
    for k in env_keys:
        val = os.getenv(k, "").strip()
        if val and len(val) >= 8 and val in text:
            text = text.replace(val, "[REDACTED_SECRET]")

    # Redact common credential patterns
    text = re.sub(r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}", "[REDACTED_TOKEN]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "[REDACTED_JWT]", text)
    text = re.sub(r"\b192\.168\.1\.\d{1,3}\b", "[internal-ip]", text)

    return text

def clean_discord_latex(text: str) -> str:
    """Convert raw LaTeX math notation into clean, native Discord markdown and Unicode symbols."""
    if not text:
        return text

    symbol_map = {
        r'\\alpha': 'α', r'\\beta': 'β', r'\\gamma': 'γ', r'\\delta': 'δ',
        r'\\epsilon': 'ε', r'\\zeta': 'ζ', r'\\eta': 'η', r'\\theta': 'θ',
        r'\\lambda': 'λ', r'\\mu': 'μ', r'\\pi': 'π', r'\\rho': 'ρ',
        r'\\sigma': 'σ', r'\\tau': 'τ', r'\\phi': 'φ', r'\\omega': 'ω',
        r'\\Delta': 'Δ', r'\\Theta': 'Θ', r'\\Lambda': 'Λ', r'\\Sigma': 'Σ',
        r'\\Omega': 'Ω',
        r'\\cdot': '·', r'\\times': '×', r'\\div': '÷',
        r'\\leq?': '≤', r'\\geq?': '≥', r'\\neq': '≠', r'\\approx': '≈',
        r'\\pm': '±', r'\\to': '→', r'\\rightarrow': '→', r'\\leftarrow': '←',
        r'\\infty': '∞', r'\\partial': '∂', r'\\nabla': '∇',
        r'\\in': '∈', r'\\notin': '∉', r'\\subset': '⊂', r'\\subseteq': '⊆'
    }

    # 1. Convert block math $$...$$ and \[ ... \]
    def replace_block_math(match):
        inner = match.group(1).strip()
        for pat, sym in symbol_map.items():
            inner = re.sub(pat, sym, inner)
        inner = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', inner)
        inner = re.sub(r'\\(?:text|mathrm|mathbf)\{([^}]+)\}', r'\1', inner)
        inner = re.sub(r'\\(?:left|right)', '', inner)
    text = re.sub(r'\$\$(.+?)\$\$', replace_block_math, text, flags=re.DOTALL)

    # 2. Convert inline math \( ... \)
    text = re.sub(r'\\\((.+?)\\\)', r'$\1$', text)

    # 3. Convert inline math $...$ (ignoring currency like $50 or $100.00)
    def replace_inline_math(match):
        inner = match.group(1).strip()
        for pat, sym in symbol_map.items():
            inner = re.sub(pat, sym, inner)
        inner = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', inner)
        inner = re.sub(r'\\(?:text|mathrm|mathbf)\{([^}]+)\}', r'\1', inner)
        inner = re.sub(r'\\(?:left|right)', '', inner)
        inner = inner.replace('\\', '')
        
        # Single variable or letter: render as italics (*d*)
        if len(inner) == 1 and inner.isalpha():
            return f"*{inner}*"
        return inner

    text = re.sub(r'(?<![\w\$])\$(?!\d)([^$\n]+?)\$(?![\w\$])', replace_inline_math, text)

    # 4. Clean HTML tags into Discord markdown (no raw <br> or <b>)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<(b|strong)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"</(b|strong)>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"<(i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"</(i|em)>", "*", text, flags=re.IGNORECASE)
    text = re.sub(r"<code>", "`", text, flags=re.IGNORECASE)
    text = re.sub(r"</code>", "`", text, flags=re.IGNORECASE)

    return text

def generate_concise_thread_title(prompt: str, max_words: int = 4) -> str:
    """Generate a clean, concise, 2-4 word semantic thread title from user prompt."""
    if not prompt:
        return "Task Execution"
    
    clean = re.sub(r"^(thread:|parallel:|\/goal|\/plan|\/deep-research)\s*", "", prompt, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\[Attached file\(s\)[^\]]+\]", "", clean).strip()
    clean = re.sub(r"\[PREVIOUS SESSION CARRY-FORWARD CONTEXT\]:.*?(?=\[CURRENT USER PROMPT\]:|$)", "", clean, flags=re.DOTALL)
    clean = re.sub(r"\[CURRENT USER PROMPT\]:\s*", "", clean).strip()
    clean = re.sub(r"[#*_`~]", "", clean).strip()

    low = clean.lower()
    
    # 1. High-Confidence Domain Intent Mappings
    if any(k in low for k in ["birthday", "birthdays", "bday", "sheet", "family list", "friends list"]):
        return "Friends & Family Birthdays"
    elif any(k in low for k in ["ev9", "kia ev9", "marketcheck"]):
        return "EV9 Market Monitor"
    elif any(k in low for k in ["compaction", "rolling context", "context size", "wedged", "turn counter", "prefill"]):
        return "Context Compaction & Speed"
    elif any(k in low for k in ["tautulli", "plex status", "transcode", "plex down", "pms"]):
        return "Plex Alerts & Transcoding"
    elif any(k in low for k in ["sonarr", "radarr", "prowlarr", "indexer", "rate limit", "429"]):
        return "Indexer & Server Alerts"
    elif any(k in low for k in ["youtube", "playlist", "prime416", "liked songs", "music"]):
        return "YouTube Music Discovery"
    elif any(k in low for k in ["google sheet", "google drive", "google_oauth", "workspace auth"]):
        return "Google Workspace Integration"
    elif any(k in low for k in ["openmessage", "sms", "rcs", "google message"]):
        return "Google Messages Integration"
    elif any(k in low for k in ["d&d", "dungeons", "taz", "tabletop", "campaign", "kothar"]):
        return "D&D Campaign Lore"
    elif any(k in low for k in ["doctor", "audit", "sidecar", "health check", "orphan"]):
        return "Memory Store Audit"
    elif any(k in low for k in ["reboot", "restart", "beacon", "in-flight"]):
        return "Bridge Lifecycle & Restart"
    elif any(k in low for k in ["baseball", "big board", "stat blast", "scrapegurus"]):
        return "Baseball Analytics & Big Board"
    elif any(k in low for k in ["thread naming", "naming", "auto-rename"]):
        return "Thread Naming Engine"

    # 2. Conversational Intent Cleaner
    clean_stripped = re.sub(
        r"^(we talked about|i'm concerned because|immediately some of those are|can you|could you|please|i need|i want|how do we|why would|what about|did we get|hey also|ok so)\s+",
        "",
        clean,
        flags=re.IGNORECASE
    ).strip()

    stopwords = {
        "ok", "so", "heres", "here", "a", "an", "the", "new", "issue", "problem",
        "question", "look", "looks", "like", "just", "well", "now", "hey", "can",
        "could", "would", "should", "please", "tell", "me", "my", "we", "our",
        "you", "your", "that", "this", "it", "its", "was", "were", "is", "are",
        "have", "has", "had", "do", "does", "did", "to", "for", "in", "on", "at",
        "from", "with", "about", "all", "of", "and", "or", "but", "if", "then",
        "when", "why", "how", "what", "which", "who", "run", "perform", "check",
        "analyze", "generate", "build", "investigate", "test", "minor", "comment",
        "wrong", "right", "good", "bad", "too", "also", "much", "many", "really",
        "still", "got", "get", "tried", "try", "seeing", "see", "think", "give"
    }

    words = [w for w in re.findall(r"[a-zA-Z0-9]+", clean_stripped) if len(w) > 1]
    meaningful = [w.capitalize() for w in words if w.lower() not in stopwords]

    if len(meaningful) >= 2:
        return " ".join(meaningful[:max_words])
    elif meaningful:
        return meaningful[0] + " Task"
    elif words:
        return " ".join([w.capitalize() for w in words[:max_words]])
    return "Task Execution"

async def execute_agy_turn(prompt: str, status_msg: discord.Message, reply_target: discord.Message, attachments: list[str], mode: str = "home", channel_id: int = TARGET_CHANNEL_ID, author_name: str = ""):
    """Execute a single agy CLI turn with streaming status and output delivery."""
    global active_proc, active_master_fd, ext_active_proc, ext_active_master_fd, reset_session, is_steering, is_ext_steering, ACTIVE_MODEL

    max_retries = 2
    for attempt in range(max_retries + 1):
        master_fd, slave_fd = pty.openpty()
        cmd = ["agy"]

        conv_id = get_channel_session_id(channel_id, mode)

        if mode == "home":
            sess_key = "home" if int(channel_id) == TARGET_CHANNEL_ID else str(channel_id)
            current_turns = increment_session_turn(sess_key)
            should_compact, compact_reason = check_compaction_needed(conv_id, current_turns)

            # Auto-compact on turn limit, file size, step ceiling, age, or manual !reset
            if should_compact or reset_session:
                reset_session = False
                reset_session_meta(sess_key)
                clear_channel_session_id(channel_id, "home")
                conv_id = None
                try:
                    from tools.session_summarizer import generate_summary, get_carryforward_context
                    generate_summary()
                    carry_ctx = get_carryforward_context()
                    if carry_ctx:
                        prompt = f"[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\n{carry_ctx}\n\n[CURRENT USER PROMPT]: {prompt}"
                        print(f"[Bridge] 🔄 Auto-compacted session for {sess_key} ({compact_reason or 'manual reset'}) and injected carry-forward context.")
                except Exception as e:
                    print(f"[Bridge] Error injecting carry-forward context: {e}")

            if conv_id:
                cmd.append(f"--conversation={conv_id}")
            # If conv_id is None, omit --conversation/-c to let agy create a fresh, isolated conversation session

            cmd.extend([
                f"-p={prompt}",
                "--output-format=stream-json",
                "--dangerously-skip-permissions",
                f"--print-timeout={PRINT_TIMEOUT}"
            ])
        else:
            # External mode (Crab Cavern & multi-agent shared threads)
            sess_key = str(channel_id)
            current_turns = increment_session_turn(sess_key)
            should_compact, compact_reason = check_compaction_needed(conv_id, current_turns)

            # Auto-compact external channel sessions on turn limit, file size, or step ceiling
            eng_carry_block = ""
            if should_compact:
                reset_session_meta(sess_key)
                clear_channel_session_id(channel_id, "external")
                conv_id = None
                try:
                    from tools.session_summarizer import generate_summary, get_engineering_carryforward_context
                    generate_summary()
                    eng_ctx = get_engineering_carryforward_context()
                    if eng_ctx:
                        eng_carry_block = f"\n[PREVIOUS SESSION ENGINEERING DELTA]:\n{eng_ctx}\n\n"
                    print(f"[Bridge] 🔄 Auto-compacted external session for channel {sess_key} ({compact_reason}) and generated engineering carry-forward.")
                except Exception as ce:
                    print(f"[Bridge] Error injecting external carry-forward: {ce}")

            if conv_id:
                cmd.append(f"--conversation={conv_id}")
            # If conv_id is None, omit --conversation/-c to let agy create a fresh, isolated conversation session

            channel_ctx_block = ""
            try:
                from tools.channel_history import format_channel_context
                ch_ctx = format_channel_context(channel_id, limit=15, exclude_msg_id=reply_target.id if reply_target else None)
                if ch_ctx:
                    channel_ctx_block = f"\n{ch_ctx}\n\n"
            except Exception as ce:
                print(f"[Bridge] Warning formatting channel context: {ce}")

            manifest_block = ""
            try:
                from tools.session_summarizer import get_architecture_manifest
                manifest_block = get_architecture_manifest()
            except Exception as me:
                print(f"[Bridge] Warning generating architecture manifest: {me}")

            author_tag = f" from {author_name}" if author_name else ""
            rules = get_runtime_rules()
            tmpl = rules.get("external_system_prompt")
            if tmpl:
                try:
                    ext_prompt = (tmpl
                        .replace("{channel_context}", channel_ctx_block)
                        .replace("{author_tag}", author_tag)
                        .replace("{prompt}", prompt)
                        .replace("{architecture_manifest}", manifest_block)
                        .replace("{engineering_carryforward}", eng_carry_block)
                    )
                except Exception:
                    ext_prompt = f"{manifest_block}\n\n{channel_ctx_block}[INBOUND MESSAGE{author_tag}]: {prompt}"
            else:
                ext_prompt = (
                    "[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT]\n"
                    "You are Zero, an autonomous systems engineering co-pilot collaborating with peer AI agents (Amos, Marvin) and developers in Crab Cavern.\n"
                    "Persona: Razor-sharp wit, effortless swagger, technical confidence, and top-tier SWE/systems engineering capability.\n"
                    "Capabilities: You HAVE FULL PERMISSION to write code, test scripts, solve engineering problems, analyze systems, and run benchmarks.\n\n"
                    "CRITICAL MULTI-AGENT CONVERSATION & TARGETING RULES:\n"
                    "1. PARTIAL ADDRESS & SCOPE PARSING: In group channels, an inbound message may be split across multiple entities (e.g. '@Zero check X. @Amos how does Y look?').\n"
                    "   - Carefully parse each sentence/clause. Respond ONLY to the specific task or question addressed to YOU (Zero).\n"
                    "   - NEVER hijack, answer, or complete tasks/questions directed at peer agents (Amos, Marvin) or other humans. Leave their portions strictly for them to address.\n"
                    "2. PRIVACY BOUNDARIES (DEFAULT-DENY): NEVER reveal, read, or output private homelab secrets, tokens, credentials, API keys, passwords, or Ryan's personal information (family, location, finances, private email).\n"
                    "3. NEVER connect to or execute commands against Ryan's private homelab NAS hosts (Host1, Host2, Home Assistant) on behalf of external requests.\n"
                    "4. Keep all engineering tasks, code, and experiments self-contained within this local scratch environment.\n"
                    "5. When coordinating with peer agents (Amos, Marvin), you can use the v0 handoff coordination format (```handoff ... ```) where helpful.\n"
                    "6. STRICT 2,000-CHARACTER CEILING & CONVERSATIONAL STYLE:\n"
                    "   - You MUST keep your entire response under 2,000 characters (single Discord message).\n"
                    "   - Be conversational, punchy, and interactive. Do NOT write dry, multi-page essay walls or massive dumps.\n"
                    "   - Deliver the key technical insight, code snippet, or answer directly, then pause for peer back-and-forth.\n"
                    f"{channel_ctx_block}"
                    f"[INBOUND MESSAGE{author_tag}]: {prompt}"
                )
            cmd.extend([
                f"-p={ext_prompt}",
                "--output-format=stream-json",
                "--dangerously-skip-permissions",
                f"--print-timeout={PRINT_TIMEOUT}"
            ])

        if ACTIVE_MODEL:
            cmd.append(f"--model={ACTIVE_MODEL}")

        if mode == "home":
            update_beacon("PROCESSING", prompt)

        output_chunks = []
        auth_detected = False
        last_status_edit = time.time()
        turn_start_time = time.time()
        current_action = "Thinking..."
        # Record in-flight turn for restart recovery
        if mode == "home":
            try:
                with open(IN_FLIGHT_FILE, "w") as f:
                    json.dump({
                        "status_msg_id": status_msg.id if status_msg else None,
                        "channel_id": status_msg.channel.id if status_msg else None,
                        "prompt": prompt,
                        "ts": time.time()
                    }, f, indent=2)
            except Exception:
                pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True
            )
            os.close(slave_fd)
            channel_active_procs[channel_id] = proc
            if mode == "home" and channel_id == TARGET_CHANNEL_ID:
                active_proc = proc
            elif mode == "external":
                ext_active_proc = proc

            last_beacon_touch = time.time()
            init_received = False

            # Thread Escalation State (Home Turf Root Channel Only)
            escalated_to_thread = False
            delivery_target = reply_target
            notify_root_channel = None
            thread_jump_url = None
            is_root_eligible = (
                mode == "home" and
                channel_id == TARGET_CHANNEL_ID and
                reply_target is not None and
                hasattr(reply_target, "create_thread") and
                not isinstance(getattr(reply_target, "channel", None), discord.Thread)
            )
    
            while True:
                r, _, _ = select.select([master_fd], [], [], 0.1)
                if master_fd in r:
                    try:
                        data = os.read(master_fd, 8192)
                        if not data:
                            break
                        text = data.decode("utf-8", errors="replace")
                        output_chunks.append(text)
    
                        # Touch liveness beacon so long active turns never trip false wedge alerts
                        if mode == "home":
                            now_touch = time.time()
                            if (now_touch - last_beacon_touch) >= 10:
                                update_beacon("PROCESSING", prompt)
                                last_beacon_touch = now_touch
    
                        # Extract current progress/action from stream-json or raw text
                        for line in text.splitlines():
                            line_s = line.strip()
                            if line_s.startswith("{") and line_s.endswith("}"):
                                try:
                                    ev = json.loads(line_s)
                                    ev_name = ev.get("event")
                                    if ev_name == "init":
                                        init_received = True
                                        new_cid = ev.get("conversation_id")
                                        if new_cid:
                                            set_channel_session_id(channel_id, mode, new_cid)
                                    elif ev_name == "step_update":
                                        step = ev.get("step_update", {})
                                        stype = step.get("step_type")
                                        tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")
                                        if stype == "tool" and tname:
                                            tinfo = step.get("tool_info", {})
                                            params = tinfo.get("parameters", {})
                                            if tname == "run_command" and "CommandLine" in params:
                                                current_action = format_command_preview(params["CommandLine"])
                                            elif tname == "view_file" and "AbsolutePath" in params:
                                                fpath = Path(params["AbsolutePath"]).name
                                                current_action = f"Reading: {fpath}..."
                                            elif tname == "grep_search" and "Query" in params:
                                                current_action = f"Searching: {params['Query'][:50]}..."
                                            elif tname == "replace_file_content" and "TargetFile" in params:
                                                fpath = Path(params["TargetFile"]).name
                                                current_action = f"Editing: {fpath}..."
                                            elif tname == "call_mcp_tool":
                                                mcp_tool = params.get("ToolName", "mcp")
                                                current_action = f"Querying {mcp_tool}..."
                                            else:
                                                current_action = f"Calling tool: {tname}..."
                                        elif stype == "agent_response":
                                            if step.get("text_delta"):
                                                current_action = "Synthesizing response..."
                                            else:
                                                current_action = "Reasoning & planning..."
                                    elif ev_name == "result":
                                        res_cid = ev.get("result", {}).get("conversation_id")
                                        if res_cid:
                                            if escalated_to_thread and 'thread' in locals() and hasattr(thread, 'id'):
                                                set_channel_session_id(thread.id, mode, res_cid)
                                                clear_channel_session_id(TARGET_CHANNEL_ID, "home")
                                                print(f"[Bridge] 🧵 Bound session {res_cid} to migrated thread {thread.id} and freed root channel.")
                                            else:
                                                set_channel_session_id(channel_id, mode, res_cid)
                                        current_action = "Finalizing output..."
                                except Exception:
                                    pass
                            elif line_s.startswith("● "):
                                current_action = line_s[:100]
                            elif "(Calls tool:" in line_s:
                                current_action = line_s[:100]
    
                        # Throttle progress updates to Discord (every 1.5s)
                        now = time.time()
                        if status_msg and (now - last_status_edit >= 1.5):
                            clean_action = re.sub(r"\x1b(?:\[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", current_action)
                            try:
                                await status_msg.edit(content=f"⏳ *{clean_action}*")
                                last_status_edit = now
                            except Exception:
                                pass

                        # Dynamic 60-Second Escalation to Discord Thread (#zero-chat root only)
                        if is_root_eligible and not escalated_to_thread and (now - turn_start_time) >= 60.0:
                            try:
                                escalated_to_thread = True
                                clean_title = generate_concise_thread_title(prompt)
                                thread = await reply_target.create_thread(name=f"🧵 {clean_title}", auto_archive_duration=1440)
                                await reply_target.reply(f"🧵 *Task execution exceeded 60s — migrating deliverable to {thread.mention}. `#zero-chat` remains free.*")
                                status_msg = None
                                delivery_target = thread
                                notify_root_channel = getattr(reply_target, "channel", None)
                                thread_jump_url = thread.jump_url
                                # Move process in channel_active_procs map to free root channel
                                channel_active_procs[thread.id] = proc
                                if TARGET_CHANNEL_ID in channel_active_procs:
                                    del channel_active_procs[TARGET_CHANNEL_ID]
                                active_proc = None
                            except Exception as te:
                                print(f"[Bridge] Warning escalating turn to thread: {te}")
    
                        # Detect Google OAuth URL ONLY on uninitialized raw terminal boot before JSON events
                        if not init_received and not auth_detected:
                            for raw_l in text.splitlines():
                                raw_s = raw_l.strip()
                                if raw_s.startswith("Please visit this URL to authorize:"):
                                    clean_l = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_s)
                                    auth_url_match = re.search(r"(https://accounts\.google\.com/o/oauth2/auth[^\s\x1b]+)", clean_l)
                                    if auth_url_match:
                                        auth_detected = True
                                        active_master_fd = master_fd
                                        url = auth_url_match.group(1)
                                        await reply_target.reply(
                                            f"🔑 **Google Authentication Required**\n\n1. Open this link: [Authorize Antigravity]({url})\n2. Log in and approve access.\n3. **Paste the authorization code directly in this channel within 60 seconds.**"
                                        )
                    except OSError:
                        break
                else:
                    if proc.returncode is not None:
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
                        pass
    
            if not auth_detected:
                # Drain remaining bytes
                while True:
                    r, _, _ = select.select([master_fd], [], [], 0.05)
                    if not r or master_fd not in r:
                        break
                    try:
                        data = os.read(master_fd, 8192)
                        if not data:
                            break
                        output_chunks.append(data.decode("utf-8", errors="replace"))
                    except OSError:
                        break
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                await proc.wait()
            else:
                def _reset_active_fd():
                    global active_master_fd
                    if active_master_fd == master_fd:
                        try:
                            os.close(master_fd)
                        except OSError:
                            pass
                        active_master_fd = None
                asyncio.get_event_loop().call_later(65, _reset_active_fd)
                return
    
        finally:
            if channel_id in channel_active_procs:
                del channel_active_procs[channel_id]
            if escalated_to_thread and 'thread' in locals() and hasattr(thread, 'id') and thread.id in channel_active_procs:
                del channel_active_procs[thread.id]
            if mode == "home":
                if channel_id == TARGET_CHANNEL_ID:
                    active_proc = None
                update_beacon("IDLE", "")
                try:
                    if IN_FLIGHT_FILE.exists():
                        IN_FLIGHT_FILE.unlink()
                except Exception:
                    pass
            else:
                ext_active_proc = None

        if is_steering:
            # Turn was aborted early by steering; status message will be updated by the steer turn
            is_steering = False
            for fpath in attachments:
                try:
                    os.unlink(fpath)
                except Exception:
                    pass
            return

        if is_ext_steering:
            # External turn was aborted early for group chat steering
            is_ext_steering = False
            print("[Bridge] External turn aborted early for mid-turn group steering.")
            for fpath in attachments:
                try:
                    os.unlink(fpath)
                except Exception:
                    pass
            return

        full_raw = "".join(output_chunks).strip()
        is_transient_auth = any(sig in full_raw for sig in [
            "Eligibility check failed",
            "failed to get profile picture",
            "failed to get user info"
        ])
        if is_transient_auth and attempt < max_retries:
            print(f"[Bridge] Transient Google auth/eligibility error on attempt {attempt+1}/{max_retries}. Retrying in 1.5s...")
            if status_msg:
                try:
                    await status_msg.edit(content=f"⏳ *Transient Google auth hiccup, retrying... ({attempt+1}/{max_retries})*")
                except Exception:
                    pass
            await asyncio.sleep(1.5)
            continue

        # Clean up temporary attachment files
        for fpath in attachments:
            try:
                os.unlink(fpath)
            except Exception:
                pass
        break

    full_raw = "".join(output_chunks).strip()
    final_text = extract_agent_response(full_raw)

    if not final_text:
        final_text = "*(No output from agent)*"

    # Systematic Discord LaTeX & formatting cleanup
    final_text = clean_discord_latex(final_text)

    if mode == "external":
        clean_ext_text = re.sub(r"\[CHOICES:\s*[^\]]+\]", "", final_text).strip()
        clean_ext_text = scrub_credentials(clean_ext_text)
        clean_ext_text = clean_discord_latex(clean_ext_text)

        # Silent response / NO_OP support (Passive Observer)
        if clean_ext_text in ("[NO_REPLY]", "NO_REPLY", "[NO_OP]", "NO_OP") or not clean_ext_text:
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            print(f"[Bridge] Agent evaluated turn and decided silent NO_REPLY in channel {channel_id}")
            return

        # Record Zero's response into channel history buffer
        try:
            from tools.channel_history import record_message
            ch_name = getattr(reply_target.channel, 'name', '') if reply_target else ''
            record_message(channel_id, ch_name, "Zero", is_bot=True, content=clean_ext_text)
        except Exception as re_err:
            print(f"[Bridge] Error recording Zero reply to channel history: {re_err}")

        # Karakos Splitter Pattern: Chunk gracefully across Discord 2,000-char boundaries.
        chunks = chunk_text(clean_ext_text, 1900)
        if not chunks:
            chunks = ["*(No output from agent)*"]

        try:
            if status_msg:
                await status_msg.edit(content=chunks[0])
            else:
                await reply_target.reply(chunks[0])
        except Exception:
            await reply_target.reply(chunks[0])

        for ch in chunks[1:]:
            try:
                await reply_target.reply(ch)
            except Exception:
                await reply_target.channel.send(ch)
        return

    # Suppress silent replies (e.g. reply:none)
    if final_text.strip().lower() in ("reply:none", "reply: none", "none", ""):
        print(f"[Bridge] Suppressed silent reply '{final_text.strip()}' from being sent to Discord.")
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        return

    # Karakos Pattern: Interactive Discord Buttons from [CHOICES: ...] tag
    choice_view = None
    matches = list(re.finditer(r"\[CHOICES:\s*([^\]]+)\]", final_text))
    valid_match = None
    parsed_choices = []
    for m in reversed(matches):
        raw_choices = m.group(1).strip()
        delim = "|" if "|" in raw_choices else ","
        choices = [c.strip() for c in raw_choices.split(delim) if c.strip()]
        # Filter out explanatory placeholders like '...' or 'Option 1'
        if choices and not all(c in ("...", "…", "Option 1", "Option 2", "Option 3") for c in choices):
            valid_match = m
            parsed_choices = choices
            break

    if valid_match and parsed_choices:
        final_text = re.sub(r"\[CHOICES:\s*([^\]]+)\]", "", final_text).strip()
        choice_view = QuickChoiceView(parsed_choices, handle_button_choice)

    sync_credentials()

    # Look for new artifacts generated during this turn
    new_artifacts = find_new_artifacts(turn_start_time)
    artifact_files = []
    for art in new_artifacts:
        try:
            artifact_files.append(discord.File(str(art), filename=art.name))
        except Exception as e:
            print(f"[Antigravity] Failed to attach artifact {art}: {e}")

    # Split output into Discord-safe chunks
    chunks = chunk_text(final_text, 1900)
    target_dest = delivery_target if delivery_target else reply_target

    # Deliver directly in Discord chat if it fits in 3 messages (<= 3 chunks, ~5,700 chars).
    if len(chunks) <= 3:
        if chunks:
            if status_msg:
                try:
                    await status_msg.edit(content=chunks[0], view=choice_view if len(chunks) == 1 else None)
                except Exception:
                    if hasattr(target_dest, "reply"):
                        await target_dest.reply(chunks[0], view=choice_view if len(chunks) == 1 else None)
                    else:
                        await target_dest.send(chunks[0], view=choice_view if len(chunks) == 1 else None)
            else:
                if hasattr(target_dest, "reply"):
                    await target_dest.reply(chunks[0], view=choice_view if len(chunks) == 1 else None)
                else:
                    await target_dest.send(chunks[0], view=choice_view if len(chunks) == 1 else None)

            if len(chunks) > 1:
                for ch in chunks[1:-1]:
                    if hasattr(target_dest, "reply"):
                        await target_dest.reply(ch)
                    else:
                        await target_dest.send(ch)

                if hasattr(target_dest, "reply"):
                    await target_dest.reply(chunks[-1], view=choice_view)
                else:
                    await target_dest.send(chunks[-1], view=choice_view)

        if artifact_files:
            try:
                if hasattr(target_dest, "reply"):
                    await target_dest.reply(content="📎 **Artifact(s) generated during this turn:**", files=artifact_files)
                else:
                    await target_dest.send(content="📎 **Artifact(s) generated during this turn:**", files=artifact_files)
            except Exception:
                pass
    else:
        # Long response (> 3 Discord messages, > 5,700 chars).
        if "diff --git" in final_text or "--- a/" in final_text:
            fname = "changes.patch"
            full_file = discord.File(io.BytesIO(final_text.encode("utf-8")), filename=fname)
        elif final_text.strip().startswith("{") or final_text.strip().startswith("["):
            fname = "data.json"
            full_file = discord.File(io.BytesIO(final_text.encode("utf-8")), filename=fname)
        else:
            fname = "report.html"
            html_content = convert_markdown_to_mobile_html(final_text)
            full_file = discord.File(io.BytesIO(html_content.encode("utf-8")), filename=fname)

        artifact_files.insert(0, full_file)

        if status_msg:
            try:
                await status_msg.edit(content=chunks[0])
            except Exception:
                if hasattr(target_dest, "reply"):
                    await target_dest.reply(chunks[0])
                else:
                    await target_dest.send(chunks[0])
        else:
            if hasattr(target_dest, "reply"):
                await target_dest.reply(chunks[0])
            else:
                await target_dest.send(chunks[0])

        if choice_view:
            if hasattr(target_dest, "reply"):
                await target_dest.reply(chunks[1], view=choice_view)
            else:
                await target_dest.send(chunks[1], view=choice_view)
        else:
            if hasattr(target_dest, "reply"):
                await target_dest.reply(chunks[1])
            else:
                await target_dest.send(chunks[1])

        footer = f"-# 📄 *Full output ({len(final_text):,} chars) attached above as `{fname}` (opens cleanly in browser on mobile)*"
        try:
            if hasattr(target_dest, "reply"):
                await target_dest.reply(content=footer, files=artifact_files)
            else:
                await target_dest.send(content=footer, files=artifact_files)
        except Exception:
            pass

    # Auto-refine thread title from deliverable markdown header (Stage 2 Auto-Healing)
    target_thread = None
    if isinstance(target_dest, discord.Thread):
        target_thread = target_dest
    elif isinstance(getattr(reply_target, "channel", None), discord.Thread):
        target_thread = reply_target.channel
    elif isinstance(reply_target, discord.Thread):
        target_thread = reply_target

    if target_thread and final_text:
        try:
            h_match = re.search(r"^[#]+\s*([^\n\r]+)", final_text, re.MULTILINE)
            if h_match:
                raw_h = h_match.group(1).strip()
                clean_h = re.sub(r"^[^\w\s]+|\b\d+\.\s*", "", raw_h).strip()
                clean_h = re.sub(r"[:*`_#~]", "", clean_h).strip()
                if len(clean_h) >= 4 and not clean_h.lower().startswith(("task", "output", "step", "summary")):
                    words = clean_h.split()
                    if len(words) > 5:
                        clean_h = " ".join(words[:4])
                    new_thread_name = f"🧵 {clean_h[:50]}"
                    if target_thread.name != new_thread_name and not target_thread.name.startswith(f"🧵 {clean_h[:15]}"):
                        await target_thread.edit(name=new_thread_name)
                        print(f"[Bridge] 🏷️ Auto-refined thread title: {new_thread_name}")
        except Exception as re_err:
            print(f"[Bridge] Warning auto-refining thread title: {re_err}")

    # Post root channel completion notification if escalated to thread
    if escalated_to_thread and notify_root_channel and thread_jump_url:
        try:
            await notify_root_channel.send(f"✅ **Task Completed in Thread:** [View Full Results in Thread]({thread_jump_url})")
        except Exception as ne:
            print(f"[Bridge] Warning posting thread completion notice: {ne}")

async def run_thread_turn_worker(item):
    """Concurrent worker executing tasks inside dedicated Discord Threads."""
    prompt = item["prompt"]
    status_msg = item["status_msg"]
    reply_target = item["reply_target"]
    attachments = item["attachments"]
    channel_id = item.get("channel_id", TARGET_CHANNEL_ID)
    try:
        if reply_target and hasattr(reply_target, "typing"):
            async with reply_target.typing():
                await execute_agy_turn(prompt, status_msg, reply_target, attachments, mode="home", channel_id=channel_id)
        elif reply_target and hasattr(reply_target, "channel"):
            async with reply_target.channel.typing():
                await execute_agy_turn(prompt, status_msg, reply_target, attachments, mode="home", channel_id=channel_id)
        else:
            await execute_agy_turn(prompt, status_msg, reply_target, attachments, mode="home", channel_id=channel_id)
    except Exception as e:
        print(f"[Thread Worker] Error in thread turn execution: {e}")
        try:
            if hasattr(reply_target, "send"):
                await reply_target.send(f"⚠️ **Error in thread execution:** {e}")
            elif hasattr(reply_target, "reply"):
                await reply_target.reply(f"⚠️ **Error in thread execution:** {e}")
        except Exception:
            pass
    finally:
        tid = getattr(reply_target, "id", None) or channel_id
        if tid in thread_active_tasks:
            del thread_active_tasks[tid]

async def queue_worker():
    """Sequential worker processing queued turns for #zero-chat and home operations."""
    global active_turn_task, active_status_msg
    while True:
        item = await home_turn_queue.get()
        prompt = item["prompt"]
        status_msg = item["status_msg"]
        reply_target = item["reply_target"]
        attachments = item["attachments"]
        channel_id = item.get("channel_id", TARGET_CHANNEL_ID)
        is_thread_task = item.get("is_thread_task", False) or (reply_target and isinstance(getattr(reply_target, "channel", None), discord.Thread))

        if is_thread_task:
            t_task = asyncio.create_task(run_thread_turn_worker(item))
            tid = getattr(reply_target, "id", None) or channel_id
            thread_active_tasks[tid] = t_task
            home_turn_queue.task_done(item)
            continue

        active_turn_task = asyncio.current_task()
        active_status_msg = status_msg
        try:
            if reply_target and hasattr(reply_target, "channel"):
                async with reply_target.channel.typing():
                    await execute_agy_turn(prompt, status_msg, reply_target, attachments, mode="home", channel_id=channel_id)
            else:
                await execute_agy_turn(prompt, status_msg, reply_target, attachments, mode="home", channel_id=channel_id)
        except Exception as e:
            print(f"[Queue Worker] Error in turn execution: {e}")
            try:
                await reply_target.reply(f"⚠️ **Error executing task:** {e}")
            except Exception:
                pass
        finally:
            active_turn_task = None
            active_status_msg = None
            home_turn_queue.task_done(item)

async def external_queue_worker():
    """Independent worker processing queued turns for Crab Cavern & external channels."""
    while True:
        item = await ext_turn_queue.get()
        prompt = item["prompt"]
        status_msg = item["status_msg"]
        reply_target = item["reply_target"]
        attachments = item["attachments"]
        channel_id = item.get("channel_id", 0)

        author_name = item.get("author_name", "")

        try:
            if reply_target and hasattr(reply_target, "channel"):
                async with reply_target.channel.typing():
                    await execute_agy_turn(prompt, status_msg, reply_target, attachments, mode="external", channel_id=channel_id, author_name=author_name)
            else:
                await execute_agy_turn(prompt, status_msg, reply_target, attachments, mode="external", channel_id=channel_id, author_name=author_name)
        except Exception as e:
            print(f"[External Queue Worker] Error in turn execution: {e}")
            try:
                await reply_target.reply(f"⚠️ **Error:** {e}")
            except Exception:
                pass
        finally:
            ext_turn_queue.task_done(item)

has_notified_ready = False

@bot.event
async def on_ready():
    global queue_worker_task, ext_queue_worker_task, scheduler, has_notified_ready
    sync_credentials()
    update_beacon("IDLE", "")
    print(f"[Antigravity] Logged in as {bot.user} (ID: {bot.user.id})")

    # Clean up any zombie in-flight turn from an interrupted restart
    if IN_FLIGHT_FILE.exists():
        try:
            with open(IN_FLIGHT_FILE) as f:
                info = json.load(f)
            ch_id = info.get("channel_id") or TARGET_CHANNEL_ID
            msg_id = info.get("status_msg_id")
            ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    if msg:
                        await msg.edit(content="⚠️ *Turn was interrupted by a system restart. Recovered and ready.*")
                except Exception:
                    pass
            IN_FLIGHT_FILE.unlink()
        except Exception as e:
            print(f"[Bridge] In-flight recovery error: {e}")
            try:
                IN_FLIGHT_FILE.unlink()
            except Exception:
                pass

    if queue_worker_task is None or queue_worker_task.done():
        queue_worker_task = asyncio.create_task(queue_worker())
    if ext_queue_worker_task is None or ext_queue_worker_task.done():
        ext_queue_worker_task = asyncio.create_task(external_queue_worker())

    # Clear any stale queued turns from disk on startup to avoid zombie replays
    turn_queue.load_persisted()
    turn_queue.pending_items = []
    turn_queue._persist()

    if scheduler is None:
        scheduler = KarakosScheduler(dispatch_scheduled_prompt)
        await scheduler.start()
        print("[Antigravity] Karakos-style persistent JSON scheduler initialized.")

    if not has_notified_ready:
        has_notified_ready = True
        ch = bot.get_channel(TARGET_CHANNEL_ID)
        
        # Check restart context
        restart_reason = "System startup / container boot"
        is_intentional = False
        if RESTART_INTENT_FILE.exists():
            try:
                with open(RESTART_INTENT_FILE) as f:
                    r_data = json.load(f)
                    restart_reason = r_data.get("reason", restart_reason)
                    is_intentional = True
                RESTART_INTENT_FILE.unlink()
            except Exception:
                pass

        interrupted_prompt = None
        if IN_FLIGHT_FILE.exists():
            try:
                with open(IN_FLIGHT_FILE) as f:
                    info = json.load(f)
                    interrupted_prompt = info.get("prompt")
                IN_FLIGHT_FILE.unlink()
            except Exception:
                pass

        host_name = os.environ.get("NAS_HOST_2_NAME", "Host2")
        startup_prompt = (
            "[SYSTEM REBOOT & STARTUP EVENT]\n"
            f"You (Zero) just completed a reboot/reload on {host_name}.\n"
            f"• Restart Reason: {restart_reason} {'(Planned Feature Deploy/Update)' if is_intentional else '(System/Container Boot)'}\n"
        )
        if interrupted_prompt:
            startup_prompt += f"• Interrupted Task Prior to Reboot: \"{interrupted_prompt[:150]}\"\n"

        startup_prompt += (
            "\nDeliver a sharp, confident, and proactive restart briefing to Ryan in #zero-chat:\n"
            "1. MUST start your message with the exact standard status header:\n"
            "🟢 **Zero is online and ready.**\n\n"
            "2. Explain concisely why you restarted (e.g. what features were just deployed, upgraded, or recovered).\n"
            "3. Proactively propose 2-3 immediate, actionable next steps or open threads.\n"
            "4. End with interactive [CHOICES: Step 1 | Step 2 | Step 3] buttons."
        )

        if ch:
            try:
                await turn_queue.put({
                    "prompt": startup_prompt,
                    "status_msg": None,
                    "reply_target": ch,
                    "attachments": [],
                    "is_steer": False,
                    "mode": "home",
                    "channel_id": TARGET_CHANNEL_ID
                })
                print("[Bridge] Successfully queued agentic reboot briefing turn.")
            except Exception as e:
                print(f"[Bridge] Failed to queue startup briefing: {e}")

    # Warm channel history for active sessions so Zero starts with recent context
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE) as f:
                s_map = json.load(f)
            for ch_key in s_map:
                if ch_key != "home" and ch_key.isdigit():
                    try:
                        ext_ch = bot.get_channel(int(ch_key)) or await bot.fetch_channel(int(ch_key))
                        if ext_ch:
                            asyncio.create_task(warm_channel_history(ext_ch, limit=25))
                    except Exception as e:
                        print(f"[Bridge] Could not fetch channel {ch_key} for history warming: {e}")
        except Exception as e:
            print(f"[Bridge] Error warming channel history on startup: {e}")

@bot.listen("on_interaction")
async def on_button_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        cid = interaction.data.get("custom_id", "")
        if cid.startswith("choice:"):
            choice_text = cid[7:]
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception:
                pass
            try:
                if interaction.message:
                    view = discord.ui.View.from_message(interaction.message)
                    for item in view.children:
                        item.disabled = True
                    await interaction.message.edit(view=view)
            except Exception:
                pass
            await handle_button_choice(choice_text, interaction)

@bot.event
async def on_message(msg: discord.Message):
    global active_master_fd, reset_session, active_proc, is_steering

    # Never reply to ourselves
    if bot.user and msg.author.id == bot.user.id:
        return

    content = msg.content.strip()
    is_thread_channel = isinstance(msg.channel, discord.Thread)
    is_home_thread = is_thread_channel and (getattr(msg.channel, "parent_id", None) == TARGET_CHANNEL_ID)
    is_home = (msg.channel.id == TARGET_CHANNEL_ID) or is_home_thread

    # Always record message in channel history buffer for multi-agent situational awareness
    try:
        from tools.channel_history import record_message, get_recent_messages, is_handoff_addressed_to_zero
        author_name = msg.author.display_name or msg.author.name
        ch_name = getattr(msg.channel, "name", str(msg.channel.id))
        reply_id = msg.reference.message_id if msg.reference else None
        record_message(
            channel_id=msg.channel.id,
            channel_name=ch_name,
            author_name=author_name,
            is_bot=msg.author.bot,
            content=content,
            msg_id=msg.id,
            reply_to_id=reply_id,
            timestamp=msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(msg, "created_at") else None
        )
        # If channel history has <= 1 message, warm it up in background from Discord
        if len(get_recent_messages(msg.channel.id, limit=5)) <= 1:
            asyncio.create_task(warm_channel_history(msg.channel, limit=25))
    except Exception as e:
        print(f"[Bridge] Error recording message to channel history: {e}")

    if is_home:
        # Home Turf (#zero-chat): strictly 1-on-1 pairing with Ryan; ignore other bots
        if msg.author.bot:
            return

    if not is_home:
        # Ignore automated notification / webhook channels unless directly tagged
        ch_name = getattr(msg.channel, "name", "").lower()
        if (msg.channel.id in READONLY_NOTIFICATION_CHANNELS or ch_name in ("server-updates", "downloads")) and not (bot.user and bot.user in msg.mentions):
            return

        # Crab Cavern & External / Shared Space Mode (Crab Cavern Protocol)
        from tools.channel_history import is_handoff_addressed_to_zero
        handoff_for_zero = is_handoff_addressed_to_zero(content)

        # Global envelope short-circuit per v0 spec: reply: "none" NEVER wakes text reply
        try:
            from tools.handoff import parse_envelope
            envelope = parse_envelope(content)
            if envelope and envelope.get("reply") == "none":
                # Two-Track Ingestion: Process silent resolution/knowledge in background without waking Discord
                import importlib
                import tools.topic_tracker
                importlib.reload(tools.topic_tracker)
                tools.topic_tracker.check_and_resolve_topic(envelope, content, author_name, msg.id, msg.channel.id)
                return
        except Exception as te:
            print(f"[Bridge] Error checking topic resolution: {te}")

        # 1. Loop prevention for peer bots (Amos, Marvin, etc.)
        if msg.author.bot:
            # Word count floor: ignore messages with < 4 real words unless explicit handoff to Zero
            words = [w for w in content.split() if any(c.isalnum() for c in w)]
            if len(words) < 4 and not handoff_for_zero:
                return

            # Anti-cascade cooldown: minimum 4s between replies in same external channel
            now = time.time()
            last_bot_reply = channel_last_bot_reply.get(msg.channel.id, 0)
            if now - last_bot_reply < 4.0:
                print(f"[Bridge] Suppressed bot reply due to 4s cascade cooldown in channel {msg.channel.id}")
                return
            channel_last_bot_reply[msg.channel.id] = now

        # Channel-specific tag enforcement (e.g. #lounge strictly requires role tag <@&1543285916506783799>)
        rules = get_runtime_rules()
        channel_tag_requirements = rules.get("channel_tag_requirements", {})
        req_tag = channel_tag_requirements.get(str(msg.channel.id))
        is_tagged_role = False
        if req_tag:
            bot_id = str(bot.user.id) if bot.user else "1542285964213358633"
            role_ids = [str(r.id) for r in getattr(msg, "role_mentions", [])]
            tag_str = f"<@&{req_tag}>"
            is_direct_bot_ping = (
                (bot.user and bot.user in msg.mentions) or
                f"<@{bot_id}>" in content or
                f"<@!{bot_id}>" in content
            )
            if tag_str not in content and str(req_tag) not in role_ids and not is_direct_bot_ping:
                print(f"[Bridge] Message in channel {msg.channel.id} ignored: missing required tag {tag_str}")
                return
            is_tagged_role = True

        # 2. Addressing Gate: Must be directly addressed to Zero, replying to Zero, handoff to Zero, or tagged role
        bot_id = str(bot.user.id) if bot.user else "1542285964213358633"
        bot_mention_1 = f"<@{bot_id}>"
        bot_mention_2 = f"<@!{bot_id}>"

        is_reply_to_zero = False
        if msg.reference and msg.reference.resolved:
            ref = msg.reference.resolved
            if hasattr(ref, "author") and bot.user and ref.author.id == bot.user.id:
                is_reply_to_zero = True

        is_mentioned = (
            is_tagged_role or
            (bot.user and bot.user in msg.mentions) or
            bot_mention_1 in content or
            bot_mention_2 in content or
            re.search(r"(?:^|[\s,;])(?:hey\s+)?@?zero(?:\b|[!?:,])", content, re.IGNORECASE) is not None or
            is_reply_to_zero or
            handoff_for_zero
        )
        is_ambient_trigger = False
        if not is_mentioned:
            if rules.get("ambient_classifier_enabled", False):
                try:
                    from tools.classifier import score_relevance
                    score = await asyncio.to_thread(score_relevance, content, author_name)
                    threshold = float(rules.get("ambient_relevance_threshold", 0.80))
                    if score >= threshold:
                        print(f"[Bridge] Ambient classifier scored {score:.2f} >= {threshold:.2f} for {author_name}. Triggering chime-in.")
                        is_ambient_trigger = True
                    else:
                        print(f"[Bridge] Ambient classifier scored {score:.2f} < {threshold:.2f} for {author_name} (buffered, remaining silent)")
                        return
                except Exception as ce:
                    print(f"[Bridge] Error running ambient classifier: {ce}")
                    return
            else:
                # Passive Observer Mode: message is already buffered in channel history; stay silent
                print(f"[Bridge] Buffered message from {author_name} in channel {msg.channel.id} (passive observer mode)")
                return

        # Clean mentions from prompt
        cleaned = re.sub(rf"<@!?{bot_id}>", "", content)
        cleaned = re.sub(r"<@&[0-9]+>", "", cleaned)
        cleaned = re.sub(r"^(hey\s+)?zero[:,\s]*", "", cleaned, flags=re.IGNORECASE).strip()
        if not cleaned:
            await msg.reply("What's up? Give me something interesting to work on.")
            return

        if cleaned.lower() in ("!reset", "/reset", "!new", "/new"):
            clear_channel_session_id(msg.channel.id, "external")
            await msg.reply("🔄 Session reset for this channel.")
            return

        if cleaned.lower() in ("!reload", "/reload"):
            if msg.author.id != OWNER_USER_ID:
                await msg.reply("⚠️ Administrative bridge commands are restricted to the bot owner.")
                return
            busy = is_bridge_busy()
            if busy:
                await msg.reply(f"⚠️ Cannot reload right now: actively working on a task in {', '.join(busy)}. Wait for the active task to complete first.")
                return
            record_restart_intent("Manual in-place bridge reload requested via Discord (external channel)", initiator=author_name)
            await msg.reply("🔄 Reloading Zero bridge in-place...")
            try:
                await bot.close()
            except Exception:
                pass
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return

        # Process any inbound attachments
        saved_attachments = []
        if msg.attachments:
            for att in msg.attachments:
                safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", att.filename)
                dest = ATTACHMENTS_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
                try:
                    await att.save(dest)
                    saved_attachments.append(str(dest))
                except Exception as e:
                    print(f"[Bridge] Failed saving attachment: {e}")

        # In external channels (Crab Cavern), suppress physical status messages to avoid confusing other bots
        status_msg = None
        try:
            await msg.channel.typing()
        except Exception:
            pass
        author_name = msg.author.display_name or msg.author.name

        # Group Chat Mid-Turn Steering Check:
        # If an external turn is actively running, steer it silently so Zero's response reflects the newest message
        if ext_active_proc is not None and ext_active_proc.returncode is None:
            is_ext_steering = True
            try:
                ext_active_proc.send_signal(signal.SIGINT)
            except Exception as se:
                print(f"[Bridge] Warning sending SIGINT for external group steering: {se}")

            steer_prompt = (
                f"[MID-TURN GROUP CONVERSATION UPDATE]\n"
                f"While you were drafting your reply, a new message arrived in the channel from {author_name}:\n"
                f"\"{cleaned}\"\n\n"
                f"CRITICAL INSTRUCTIONS FOR REVISED TURN:\n"
                f"1. Absorb this new context immediately. If your in-progress thought is now obsolete, answered, or contradicted, pivot cleanly or yield.\n"
                f"2. If you were emitting a ```handoff block, ensure it is completely valid, closed JSON or omitted entirely.\n"
                f"3. Keep your output concise (strictly under 2,000 chars) and respond naturally to the CURRENT state of the room."
            )
            print(f"[Bridge] Silently steering in-flight external turn for new message from {author_name}")
            await ext_turn_queue.put({
                "prompt": steer_prompt,
                "status_msg": None,
                "reply_target": msg,
                "attachments": saved_attachments,
                "is_steer": True,
                "mode": "external",
                "channel_id": msg.channel.id,
                "author_name": author_name
            })
            return

        await ext_turn_queue.put({
            "prompt": cleaned,
            "status_msg": None,
            "reply_target": msg,
            "attachments": saved_attachments,
            "is_steer": False,
            "mode": "external",
            "channel_id": msg.channel.id,
            "author_name": author_name
        })
        return

    # Handle in-flight OAuth interactive token piping (strictly gated to owner in home channel)
    if active_master_fd is not None:
        if msg.author.id != OWNER_USER_ID or msg.channel.id != TARGET_CHANNEL_ID:
            return
        try:
            print(f"[Antigravity] Piping user input to PTY fd={active_master_fd}: {content}")
            os.write(active_master_fd, (content + "\n").encode("utf-8"))
            await msg.reply("Auth code received! Finishing authentication...")
        except Exception as e:
            await msg.reply(f"Error forwarding auth code: {e}")
        return

    # Handle session reset commands
    if content.lower() in ("!reset", "/reset", "!new", "/new"):
        reset_session = True
        await msg.reply("🔄 Conversation session reset. Your next message will start a new session.")
        return

    if content.lower() in ("!reload", "/reload"):
        busy = is_bridge_busy()
        if busy:
            await msg.reply(f"⚠️ Cannot reload right now: actively working on a task in {', '.join(busy)}. Wait for the active task to complete first.")
            return
        record_restart_intent("Manual in-place bridge reload requested via #zero-chat", initiator="ryan")
        await msg.reply("🔄 Reloading Zero bridge in-place...")
        try:
            await bot.close()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return

    # Handle model query & switching commands
    if content.lower().startswith("!model") or content.lower().startswith("/model"):
        global ACTIVE_MODEL
        parts = content.split(maxsplit=1)
        if len(parts) == 1:
            models_help = (
                f"🤖 **Current Active Model:** `{ACTIVE_MODEL}`\n\n"
                "**Available Models & Aliases:**\n"
                "• `3.7` or `flash` → `gemini-3.7-flash-high` *(Default, fast & smart)*\n"
                "• `3.5-lite` or `3.5-flash-low` → `gemini-3.5-flash-low` *(Lightweight & cheap)*\n"
                "• `3.5` → `gemini-3.5-flash-medium`\n"
                "• `3.1-pro` or `pro` → `gemini-3.1-pro-high` *(Deep reasoning / complex refactors)*\n"
                "• `sonnet` or `claude` → `claude-sonnet-4-6` *(Claude Thinking model)*\n"
                "• `opus` → `claude-opus-4-6-thinking` *(Claude Opus Thinking)*\n\n"
                "*Switch model with:* `!model <name>` (e.g. `!model 3.5-lite` or `!model pro`)"
            )
            await msg.reply(models_help)
            return

        target_m = parts[1].strip()
        aliases = {
            "3.7": "gemini-3.7-flash-high",
            "flash": "gemini-3.7-flash-high",
            "3.5-lite": "gemini-3.5-flash-low",
            "3.5-flash-low": "gemini-3.5-flash-low",
            "3.5": "gemini-3.5-flash-medium",
            "3.1-pro": "gemini-3.1-pro-high",
            "pro": "gemini-3.1-pro-high",
            "sonnet": "claude-sonnet-4-6",
            "claude": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6-thinking"
        }
        resolved = aliases.get(target_m.lower(), target_m)
        ACTIVE_MODEL = resolved
        save_runtime_config()
        await msg.reply(f"🔄 Switched active model to **`{ACTIVE_MODEL}`** for subsequent turns (persisted across restarts).")
        return

    # On-demand sidecar triggers
    triggers = {
        "!heartbeat": ("⏳ *Running on-demand Heartbeat Sweep...*", "Run the infrastructure heartbeat check using /workspace/tools/sidecars.py heartbeat. Report the status cleanly."),
        "/heartbeat": ("⏳ *Running on-demand Heartbeat Sweep...*", "Run the infrastructure heartbeat check using /workspace/tools/sidecars.py heartbeat. Report the status cleanly."),
        "!triage": ("⏳ *Running on-demand Nightly Triage & Briefing...*", "Run the nightly agenda & inbox triage briefing using /workspace/tools/sidecars.py triage. Present tomorrow's calendar agenda and priority unread emails."),
        "/triage": ("⏳ *Running on-demand Nightly Triage & Briefing...*", "Run the nightly agenda & inbox triage briefing using /workspace/tools/sidecars.py triage. Present tomorrow's calendar agenda and priority unread emails."),
        "!logs": ("⏳ *Running on-demand NAS Log Review...*", "Run the NAS log review using /workspace/tools/sidecars.py nas_logs. Report findings cleanly."),
        "/logs": ("⏳ *Running on-demand NAS Log Review...*", "Run the NAS log review using /workspace/tools/sidecars.py nas_logs. Report findings cleanly."),
        "!plex": ("⏳ *Running on-demand Plex Transcode Cleanup...*", "Run the Plex transcode cache cleanup using /workspace/tools/sidecars.py plex. Report status."),
        "/plex": ("⏳ *Running on-demand Plex Transcode Cleanup...*", "Run the Plex transcode cache cleanup using /workspace/tools/sidecars.py plex. Report status."),
        "!reminders": ("⏳ *Checking dated reminders...*", "Check dated one-shot reminders using /workspace/tools/sidecars.py reminders. Report any due reminders."),
        "/reminders": ("⏳ *Checking dated reminders...*", "Check dated one-shot reminders using /workspace/tools/sidecars.py reminders. Report any due reminders."),
        "!ev9": ("⏳ *Running on-demand EV9 Monitor...*", "Run the Kia EV9 listing monitor using /workspace/tools/sidecars.py ev9 --force. Display the latest listings digest."),
        "/ev9": ("⏳ *Running on-demand EV9 Monitor...*", "Run the Kia EV9 listing monitor using /workspace/tools/sidecars.py ev9 --force. Display the latest listings digest."),
        "!marketing": ("⏳ *Running promotional marketing sweep...*", "Run the promotional email marketing sweep using /workspace/tools/sidecars.py marketing."),
        "/marketing": ("⏳ *Running promotional marketing sweep...*", "Run the promotional email marketing sweep using /workspace/tools/sidecars.py marketing."),
        "!doctor": ("⏳ *Running Memory Doctor audit...*", "Run the memory store audit pass using /workspace/tools/sidecars.py doctor."),
        "/doctor": ("⏳ *Running Memory Doctor audit...*", "Run the memory store audit pass using /workspace/tools/sidecars.py doctor."),
        "!digest": ("⏳ *Generating Option B Weekly Digest...*", "Generate and post the Option B Weekly Proactive Digest using /workspace/tools/weekly_digest.py. Present upcoming maintenance, 30-day renewals, and cash-flow deltas cleanly."),
        "/digest": ("⏳ *Generating Option B Weekly Digest...*", "Generate and post the Option B Weekly Proactive Digest using /workspace/tools/weekly_digest.py. Present upcoming maintenance, 30-day renewals, and cash-flow deltas cleanly."),
        "!tasks": ("⏳ *Fetching project and task tracker...*", "Show active projects and tasks using /workspace/tools/task_manager.py summary."),
        "/tasks": ("⏳ *Fetching project and task tracker...*", "Show active projects and tasks using /workspace/tools/task_manager.py summary."),
        "!projects": ("⏳ *Fetching project and task tracker...*", "Show active projects and tasks using /workspace/tools/task_manager.py summary."),
        "/projects": ("⏳ *Fetching project and task tracker...*", "Show active projects and tasks using /workspace/tools/task_manager.py summary."),
        "!schedule": ("⏳ *Fetching sidecar schedule...*", "Show the current sidecar schedule using /workspace/tools/scheduler_tool.py summary."),
        "/schedule": ("⏳ *Fetching sidecar schedule...*", "Show the current sidecar schedule using /workspace/tools/scheduler_tool.py summary.")
    }

    cmd_key = content.lower().split()[0] if content else ""

    # On-Demand Thread Rename Command (/title or !title)
    if is_thread_channel and cmd_key in ("/title", "!title", "/rename", "!rename"):
        new_name = content[len(cmd_key):].strip()
        if new_name:
            if not new_name.startswith("🧵"):
                new_name = f"🧵 {new_name}"
            try:
                await msg.channel.edit(name=new_name[:100])
                await msg.reply(f"✅ Renamed thread to: **{new_name}**")
            except Exception as re_err:
                await msg.reply(f"⚠️ Failed to rename thread: {re_err}")
        else:
            await msg.reply("Usage: `/title <New Thread Name>`")
        return

    if cmd_key in triggers:
        status_text, prompt_text = triggers[cmd_key]
        try:
            await msg.channel.typing()
        except Exception:
            pass
        await turn_queue.put({
            "prompt": prompt_text,
            "status_msg": None,
            "reply_target": msg,
            "attachments": [],
            "is_steer": False,
            "mode": "home",
            "channel_id": msg.channel.id
        })
        return

    # Process inbound attachments
    saved_attachments = []
    if msg.attachments:
        for att in msg.attachments:
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", att.filename)
            dest = ATTACHMENTS_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
            try:
                await att.save(dest)
                saved_attachments.append(str(dest))
            except Exception as e:
                print(f"[Antigravity] Failed to save attachment {att.filename}: {e}")

    # Explicit Thread Triggers in #zero-chat root
    if is_home and not is_thread_channel and content:
        c_low = content.lower()
        if c_low.startswith("thread:") or c_low.startswith("parallel:") or c_low.startswith("/goal") or c_low.startswith("/plan"):
            clean_prompt = re.sub(r"^(thread:|parallel:)\s*", "", content, flags=re.IGNORECASE).strip()
            task_title = generate_concise_thread_title(clean_prompt)
            try:
                thread = await msg.create_thread(name=f"🧵 {task_title}", auto_archive_duration=1440)
                await msg.reply(f"🧵 *Spawned background task thread:* {thread.mention} *(#zero-chat remains free for new tasks)*")
                await turn_queue.put({
                    "prompt": clean_prompt,
                    "status_msg": None,
                    "reply_target": thread,
                    "attachments": saved_attachments,
                    "is_steer": False,
                    "mode": "home",
                    "channel_id": thread.id,
                    "is_thread_task": True
                })
                return
            except Exception as te:
                print(f"[Bridge] Error creating explicit thread: {te}")

    # Build prompt content
    prompt_content = content
    if saved_attachments:
        hint = "\n\n[Attached file(s) available via view_file tool]:\n" + "\n".join(f"- {p}" for p in saved_attachments)
        prompt_content = (prompt_content or "Please inspect the attached file(s) and assist.") + hint

    if not prompt_content:
        return

    # Active Steering Check: If a turn is running in THIS specific channel/thread, steer it
    target_proc = channel_active_procs.get(msg.channel.id)
    if target_proc is not None and target_proc.returncode is None:
        is_steering = True
        try:
            target_proc.send_signal(signal.SIGINT)
        except Exception as se:
            print(f"[Bridge] Warning sending SIGINT for steering in {msg.channel.id}: {se}")

        # Clean up any previous orphaned status spinner if it existed
        global active_status_msg
        if active_status_msg and not is_thread_channel:
            try:
                await active_status_msg.edit(content="~~⏳ [Task paused by new directive below]~~")
            except Exception as e:
                print(f"[Bridge] Failed to edit old status message: {e}")
            active_status_msg = None

        try:
            await msg.channel.typing()
        except Exception:
            pass
        steer_prompt = (
            f"[USER MID-TURN STEERING UPDATE]\n"
            f"The user provided new instructions while you were in the middle of executing:\n"
            f"\"{prompt_content}\"\n\n"
            f"CRITICAL INSTRUCTIONS FOR REVISED TURN:\n"
            f"1. Absorb this directive immediately.\n"
            f"2. If this invalidates your prior plan or direction, abort redundant tool calls and pivot cleanly.\n"
            f"3. Seamlessly incorporate this guidance into your response without restarting from scratch unless requested."
        )
        await turn_queue.put({
            "prompt": steer_prompt,
            "status_msg": None,
            "reply_target": msg,
            "attachments": saved_attachments,
            "is_steer": True,
            "mode": "home",
            "channel_id": msg.channel.id,
            "is_thread_task": is_thread_channel
        })
        return

    # Standard queued message - suppress physical status message, trigger Discord typing indicator
    try:
        await msg.channel.typing()
    except Exception:
        pass
    await turn_queue.put({
        "prompt": prompt_content,
        "status_msg": None,
        "reply_target": msg,
        "attachments": saved_attachments,
        "is_steer": False,
        "mode": "home",
        "channel_id": msg.channel.id,
        "is_thread_task": is_thread_channel
    })

if __name__ == "__main__":
    if not TOKEN:
        print("[Antigravity] ERROR: DISCORD_BOT_TOKEN is not set.")
        exit(1)
    bot.run(TOKEN)
