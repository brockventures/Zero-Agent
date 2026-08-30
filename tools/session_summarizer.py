#!/usr/bin/env python3
"""Session Summarizer for Ivy-AG (Karakos Pattern).

Generates a compact, 3-section carry-forward summary of the recent session:
- ## Primary Task
- ## Current State
- ## Key Context for Next Session

Saves to /workspace/data/last_session_summary.md for automatic injection on the next session.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
SUMMARY_FILE = DATA_DIR / "last_session_summary.md"
BRAIN_DIR = Path("/root/.gemini/antigravity-cli/brain")

def find_latest_transcript() -> Path | None:
    """Find the home session transcript from channel_sessions.json or fallback to latest."""
    sessions_file = DATA_DIR / "sessions.json"
    if sessions_file.exists():
        try:
            with open(sessions_file) as f:
                d = json.load(f)
                home_cid = d.get("home")
                if home_cid:
                    p = BRAIN_DIR / home_cid / ".system_generated" / "logs" / "transcript.jsonl"
                    if p.exists():
                        return p
        except Exception:
            pass

    if not BRAIN_DIR.exists():
        return None
    conv_dirs = [d for d in BRAIN_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not conv_dirs:
        return None
    latest_conv = max(conv_dirs, key=lambda d: d.stat().st_mtime)
    transcript = latest_conv / ".system_generated" / "logs" / "transcript.jsonl"
    if transcript.exists():
        return transcript
    return None

def extract_recent_conversation(lookback_steps: int = 15) -> list[dict]:
    """Extract recent non-empty turns from transcript.jsonl."""
    transcript_path = find_latest_transcript()
    if not transcript_path:
        return []
    
    user_turns = []
    agent_turns = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    ttype = data.get("type")
                    cnt = (data.get("content") or "").strip()
                    # Clean out XML wrapper tags from content
                    cnt = re.sub(r"</?[A-Z_]+>", "", cnt).strip()
                    if not cnt:
                        continue
                    if ttype == "USER_INPUT":
                        user_turns.append({"type": ttype, "content": cnt[:500], "time": data.get("created_at")})
                    elif ttype == "PLANNER_RESPONSE":
                        agent_turns.append({"type": ttype, "content": cnt[:500], "time": data.get("created_at")})
                except Exception:
                    continue
    except Exception as e:
        print(f"[Summarizer] Error reading transcript: {e}")
        return []

def extract_recent_dialogue(limit: int = 10) -> list[dict]:
    """Extract recent chronological dialogue turns (Ryan and Zero) without action tags."""
    transcript_path = find_latest_transcript()
    if not transcript_path:
        return []
    dialogue = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    ttype = d.get("type")
                    if ttype in ("USER_INPUT", "PLANNER_RESPONSE"):
                        cnt = (d.get("content") or "").strip()
                        cnt = re.sub(r"</?[A-Z_]+>", "", cnt).strip()
                        cnt = re.sub(r"\[CHOICES:[^\]]+\]", "", cnt).strip()
                        if cnt and not cnt.startswith("<Action:"):
                            speaker = "Ryan" if ttype == "USER_INPUT" else "Zero"
                            dialogue.append({"speaker": speaker, "content": cnt, "time": d.get("created_at")})
                except Exception:
                    continue
    except Exception as e:
        print(f"[Summarizer] Error reading transcript for dialogue: {e}")
        return []
    return dialogue[-limit:]

def generate_summary(dry_run: bool = False) -> str:
    """Generate smart rolling compaction: high-level earlier history + verbatim recent dialogue."""
    recent_dialogue = extract_recent_dialogue(limit=10)
    
    dialogue_formatted = []
    for turn in recent_dialogue:
        text = turn['content'].replace('\n\n', '\n').strip()
        # Cap very long single messages to ~400 chars to avoid re-bloating the prompt
        if len(text) > 400:
            text = text[:400] + "..."
        dialogue_formatted.append(f"• **{turn['speaker']}:** {text}")

    dialogue_block = "\n".join(dialogue_formatted) if dialogue_formatted else "*(No recent turns)*"

    summary = (
        f"<!-- Smart Rolling Compaction Generated {datetime.now(PT).strftime('%Y-%m-%d %I:%M %p PT')} -->\n"
        "## 1. Compacted Earlier Session History (Milestones)\n"
        "- Google Takeout & Profile: Deep synthesis completed for finances, family, routines, shopping, and podcasts (/workspace/memory/).\n"
        "- Plex Taste Profile: Scanned 385 played movies & TV; excluded horror (Jimmy) and kids' animation.\n"
        "- Voice Calibration: Updated in agents.md with Arrested Development deadpan, Chernobyl/Big Dig forensic failure analysis, McElroy/TAZ rules-lawyering, and Survivor game theory. Baseball metaphors explicitly dropped.\n"
        "- Operational Safeguards: In-flight reload restriction active across home and Crab Cavern (is_bridge_busy guard in bridge.py); mid-turn group steering active; synthetic <Action:...> tags banned.\n\n"
        "## 2. Recent Verbatim Dialogue (Line-by-Line Context)\n"
        f"{dialogue_block}\n\n"
        "## 3. Active Directives & Current Focus\n"
        "- Context Optimization: Evaluating session compaction and trimming.\n"
        "- Personality: Zero persona live with deadpan, forensic clarity, and no baseball metaphors.\n"
        "- Restarts: Never reload while a task is in flight in either #zero-chat or Crab Cavern.\n"
    )

    if not dry_run:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
            f.write(summary)
        print(f"[Summarizer] Wrote smart rolling compaction summary to {SUMMARY_FILE}")

    return summary

def get_carryforward_context() -> str | None:
    """Read summary if less than 24 hours old, then remove it."""
    if not SUMMARY_FILE.exists():
        return None
    try:
        mtime = SUMMARY_FILE.stat().st_mtime
        if (time.time() - mtime) > 86400:
            SUMMARY_FILE.unlink()
            return None
        content = SUMMARY_FILE.read_text(encoding="utf-8").strip()
        SUMMARY_FILE.unlink() # Consume on read
        return content
    except Exception:
        return None

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "generate"
    if action == "generate":
        print(generate_summary())
    elif action == "read":
        ctx = get_carryforward_context()
        print(ctx or "No carryforward context available.")
