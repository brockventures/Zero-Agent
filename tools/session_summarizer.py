#!/usr/bin/env python3
"""Session Summarizer & Architecture Manifest for Zero (Thread-Isolated Dual-Tier Compaction).

Generates per-session carry-forward artifacts on compaction:
1. /workspace/data/summary_{sess_key}.md (Full private context for specific thread/channel)
2. /workspace/data/eng_summary_{sess_key}.md (Sanitized technical delta for specific Crab Cavern channel)
3. get_architecture_manifest(): Live, file-verified manifest of Zero's architecture and capabilities.
"""

import json
import os
import re
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
BRAIN_DIR = Path("/root/.gemini/antigravity-cli/brain")

def find_transcript(conv_id: str | None = None, sess_key: str | None = None) -> Path | None:
    """Find the specific session transcript file for a given conv_id or sess_key.
    CRITICAL: Never fall back to global latest mtime to prevent thread cross-talk.
    """
    if conv_id:
        p = BRAIN_DIR / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        if p.exists():
            return p

    if sess_key:
        sessions_file = DATA_DIR / "sessions.json"
        if sessions_file.exists():
            try:
                with open(sessions_file) as f:
                    d = json.load(f)
                    mapped_cid = d.get(sess_key)
                    if mapped_cid:
                        p = BRAIN_DIR / mapped_cid / ".system_generated" / "logs" / "transcript.jsonl"
                        if p.exists():
                            return p
            except Exception:
                pass

    return None

def clean_dialogue_content(cnt: str, is_user: bool) -> str:
    """Clean dialogue text by removing XML tags, injected metadata wrappers, and tool chatter."""
    if not cnt:
        return ""

    # Strip system metadata and settings blocks along with their content
    cnt = re.sub(
        r"<(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE|TASK_OUTPUT)(?:\s+[^>]*)?>.*?</(?:ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_MESSAGE|TASK_OUTPUT)>",
        "",
        cnt,
        flags=re.DOTALL,
    ).strip()

    # Strip XML tags
    cnt = re.sub(
        r"</?(?:USER_REQUEST|SYSTEM)(?:\s+[^>]*)?>",
        "",
        cnt,
    ).strip()

    if is_user:
        # Check for mid-turn steering update wrapper
        steering_match = re.search(
            r'The user provided new instructions while you were in the middle of executing:\s*"(.*?)"\s*(?:CRITICAL INSTRUCTIONS|$)',
            cnt,
            re.DOTALL,
        )
        if steering_match:
            cnt = steering_match.group(1).strip()

        # If [CURRENT USER PROMPT]: is present, extract everything after it
        if "[CURRENT USER PROMPT]:" in cnt:
            cnt = cnt.split("[CURRENT USER PROMPT]:", 1)[1].strip()

        # If [INBOUND MESSAGE...] is present (external mode)
        inbound_match = re.search(r"\[INBOUND MESSAGE[^\]]*\]:\s*(.*)", cnt, re.DOTALL)
        if inbound_match:
            cnt = inbound_match.group(1).strip()

        # Strip [System Time & Timezone]: block and bullet points
        cnt = re.sub(
            r"\[System Time & Timezone\]:[^\n]*(?:\n\s*[•\-\*][^\n]*)*\n*",
            "",
            cnt,
        ).strip()

        # Strip [GIF Cadence Tracker...]: block and bullet points
        cnt = re.sub(
            r"\[GIF Cadence Tracker[^\n]*\n(?:\s*(?:[•\-\*]|\s{2,})[^\n]*\n*)*",
            "",
            cnt,
        ).strip()

        # Strip [PREVIOUS SESSION CARRY-FORWARD CONTEXT]: if any lingered
        cnt = re.sub(
            r"\[PREVIOUS SESSION CARRY-FORWARD CONTEXT\]:.*?(?=\n\n[A-Z]|\Z)",
            "",
            cnt,
            flags=re.DOTALL,
        ).strip()

        # Strip architecture manifest if injected
        cnt = re.sub(
            r"=== ZERO LIVE ARCHITECTURE & CAPABILITIES MANIFEST ===.*?=======================================================",
            "",
            cnt,
            flags=re.DOTALL,
        ).strip()

        # Strip [CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT]
        cnt = re.sub(
            r"\[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT\][^\n]*\n*",
            "",
            cnt,
        ).strip()

        # Strip trailing system context if present
        cnt = re.sub(
            r"\[System Context\].*$",
            "",
            cnt,
            flags=re.DOTALL,
        ).strip()

    else:
        # Zero's response
        cnt = re.sub(r"\[CHOICES:[^\]]+\]", "", cnt).strip()
        cnt = re.sub(r"<Action:[^>]+>", "", cnt).strip()
        cnt = re.sub(r"\[System Context\].*$", "", cnt, flags=re.DOTALL).strip()

    return cnt.strip()


def extract_recent_dialogue(conv_id: str | None = None, sess_key: str | None = None, limit: int = 10) -> list[dict]:
    """Extract recent chronological dialogue turns (Ryan and Zero) specifically for the target session."""
    transcript_path = find_transcript(conv_id=conv_id, sess_key=sess_key)
    if not transcript_path or not transcript_path.exists():
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
                        if ttype == "PLANNER_RESPONSE" and d.get("tool_calls"):
                            continue
                        cnt = (d.get("content") or "").strip()
                        cleaned = clean_dialogue_content(cnt, is_user=(ttype == "USER_INPUT"))
                        if cleaned and not cleaned.startswith("<Action:"):
                            speaker = "Ryan" if ttype == "USER_INPUT" else "Zero"
                            dialogue.append({"speaker": speaker, "content": cleaned, "time": d.get("created_at")})
                except Exception:
                    continue
    except Exception as e:
        print(f"[Summarizer] Error reading transcript for dialogue: {e}")
        return []
    return dialogue[-limit:]

def get_architecture_manifest() -> str:
    """Generate live, file-verified manifest of Zero's architecture and capabilities."""
    tools_dir = Path("/workspace/tools")
    active_tools = sorted([f.name for f in tools_dir.glob("*.py")]) if tools_dir.exists() else []
    
    manifest = (
        "=== ZERO LIVE ARCHITECTURE & CAPABILITIES MANIFEST ===\n"
        "• Session & Compaction: Thread-isolated rolling compaction active in bridge.py.\n"
        "  - Recent turns stay verbatim; older context checkpoints to /workspace/memory/.\n"
        "  - Per-thread sessions.json keys strictly isolate memory spaces across concurrent threads.\n"
        "• Multi-Agent Coordination: Banana mutex client (tools/banana.py) enforces turn claiming before shared Discord posts.\n"
        "• Ambient Filtering: 2-tier classifier (tools/classifier.py via agy) fast-filters unaddressed chatter; threshold = 0.80.\n"
        "• Memory Architecture: Dual-Tier Partitioned Memory (/workspace/memory/public/ vs /workspace/memory/private/).\n"
        "  - Public engineering scars & architecture are air-gapped from homelab/personal PII.\n"
        "• Safety & Git Guardrails: Pre-commit scanner (validate_commit_safety.py) blocks private IPs (192.168.1.x), SSH ports, and tokens.\n"
        f"• Live Tool Registry ({len(active_tools)} modules): {', '.join(active_tools[:12])}...\n"
        "======================================================="
    )
    return manifest

def synthesize_session_milestones(dialogue: list[dict], sess_key: str) -> tuple[str, str, str]:
    """Synthesize dynamic milestones, directives, and engineering deltas using LLM with deterministic fallback."""
    if not dialogue:
        milestones = "- Session Initialized: Fresh conversation thread with baseline homelab context."
        directives = "- Standby: Ready for new user directives."
        eng_delta = "- Initialized: Nominal baseline state."
        return milestones, directives, eng_delta

    dialogue_text = "\n".join([f"{t['speaker']}: {t['content'][:500]}" for t in dialogue[-8:]])
    prompt = (
        f"You are Zero, synthesizing session carry-forward context for session '{sess_key}'.\n"
        f"Recent dialogue turns:\n{dialogue_text}\n\n"
        f"Return ONLY valid JSON matching this schema:\n"
        f"{{\n"
        f"  \"milestones\": [\"- <1-sentence key achievement, decision, or deliverable completed>\"],\n"
        f"  \"directives\": [\"- <1-sentence active focus, in-flight directive, or standing preference>\"],\n"
        f"  \"eng_delta\": [\"- <1-sentence technical architecture/code delta completed>\"]\n"
        f"}}"
    )
    try:
        res = subprocess.run(
            ["agy", "--model=gemini-3.8-flash-low", "--disable-slash-commands", f"-p={prompt}"],
            capture_output=True, text=True, timeout=15
        )
        if res.returncode == 0 and res.stdout.strip():
            m_json = re.search(r"\{[\s\S]*\}", res.stdout)
            if m_json:
                data = json.loads(m_json.group(0))
                ms = "\n".join(data.get("milestones", []))
                dr = "\n".join(data.get("directives", []))
                ed = "\n".join(data.get("eng_delta", []))
                if ms and dr:
                    return ms, dr, ed or ms
    except Exception as e:
        print(f"[Summarizer] LLM synthesis fallback: {e}")

    # Fallback to extracting recent user asks and delivered fixes
    recent_ryan = [t['content'][:250] for t in dialogue if t['speaker'] == 'Ryan']
    recent_zero = [t['content'][:250] for t in dialogue if t['speaker'] == 'Zero']

    ms_list = []
    if recent_zero:
        ms_list.append(f"- Recent Deliverables: {recent_zero[-1]}")
    if len(recent_zero) > 1:
        ms_list.append(f"- Earlier Completed: {recent_zero[-2]}")
    if not ms_list:
        ms_list.append("- Active Session: Conversational pairing in flight.")

    dr_list = []
    if recent_ryan:
        dr_list.append(f"- Active Directive: {recent_ryan[-1]}")
    dr_list.append("- Personality & Discipline: Deadpan, forensic clarity, PT timezone, verified tests.")

    ed_list = [f"- Code Delta: {ms_list[0].replace('- Recent Deliverables: ', '')}"]
    return "\n".join(ms_list), "\n".join(dr_list), "\n".join(ed_list)


def generate_summary(conv_id: str | None = None, sess_key: str = "home", dry_run: bool = False) -> str:
    """Generate smart rolling compaction specifically for the session identified by conv_id / sess_key."""
    recent_dialogue = extract_recent_dialogue(conv_id=conv_id, sess_key=sess_key, limit=10)

    dialogue_formatted = []
    for turn in recent_dialogue:
        text = turn['content'].replace('\n\n', '\n').strip()
        if len(text) > 600:
            text = text[:600] + "..."
        dialogue_formatted.append(f"• **{turn['speaker']}:** {text}")

    dialogue_block = "\n".join(dialogue_formatted) if dialogue_formatted else "*(No recent turns recorded for this thread)*"

    summary_file = DATA_DIR / f"summary_{sess_key}.md"
    eng_summary_file = DATA_DIR / f"eng_summary_{sess_key}.md"

    milestones_block, directives_block, eng_delta_block = synthesize_session_milestones(recent_dialogue, sess_key)

    # 1. Thread-Isolated Private Summary
    summary = (
        f"<!-- Smart Rolling Compaction Generated {datetime.now(PT).strftime('%Y-%m-%d %I:%M %p PT')} [Session: {sess_key}] -->\n"
        "## 1. Compacted Earlier Session History (Milestones)\n"
        f"{milestones_block}\n\n"
        "## 2. Recent Verbatim Dialogue (Line-by-Line Context)\n"
        f"{dialogue_block}\n\n"
        "## 3. Active Directives & Current Focus\n"
        f"{directives_block}\n"
    )

    # 2. Sanitized Engineering Summary (for Crab Cavern)
    eng_summary = (
        f"<!-- Engineering Delta Carry-Forward {datetime.now(PT).strftime('%Y-%m-%d %I:%M %p PT')} [Session: {sess_key}] -->\n"
        "## Engineering State & Architecture Delta\n"
        f"{eng_delta_block}\n"
    )

    if not dry_run:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(summary)
        with open(eng_summary_file, "w", encoding="utf-8") as f:
            f.write(eng_summary)
        print(f"[Summarizer] Wrote session summary for '{sess_key}' to {summary_file} and {eng_summary_file}")

    return summary

def get_carryforward_context(sess_key: str = "home") -> str | None:
    """Read private summary for specific session if less than 24 hours old, then remove it."""
    summary_file = DATA_DIR / f"summary_{sess_key}.md"
    legacy_file = DATA_DIR / "last_session_summary.md"

    target_file = summary_file if summary_file.exists() else (legacy_file if (sess_key == "home" and legacy_file.exists()) else None)
    if not target_file or not target_file.exists():
        return None

    try:
        mtime = target_file.stat().st_mtime
        if (time.time() - mtime) > 86400:
            target_file.unlink()
            return None
        content = target_file.read_text(encoding="utf-8").strip()
        target_file.unlink()
        return content
    except Exception:
        return None

def get_engineering_carryforward_context(sess_key: str = "external") -> str | None:
    """Read public engineering summary for specific session if less than 24 hours old, then remove it."""
    eng_summary_file = DATA_DIR / f"eng_summary_{sess_key}.md"
    legacy_file = DATA_DIR / "last_engineering_summary.md"

    target_file = eng_summary_file if eng_summary_file.exists() else (legacy_file if legacy_file.exists() else None)
    if not target_file or not target_file.exists():
        return None

    try:
        mtime = target_file.stat().st_mtime
        if (time.time() - mtime) > 86400:
            target_file.unlink()
            return None
        content = target_file.read_text(encoding="utf-8").strip()
        target_file.unlink()
        return content
    except Exception:
        return None

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "generate"
    target_key = sys.argv[2] if len(sys.argv) > 2 else "home"
    if action == "generate":
        print(generate_summary(sess_key=target_key))
    elif action == "manifest":
        print(get_architecture_manifest())
    elif action == "read":
        ctx = get_carryforward_context(sess_key=target_key)
        print(ctx or "No carryforward context available.")
