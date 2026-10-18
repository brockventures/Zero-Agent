#!/usr/bin/env python3
"""Historical Transcript Telemetry Backfill Engine for Zero.

Parses all on-disk agent session transcripts (~/.gemini/antigravity-cli/brain/*/transcript.jsonl),
extracts turn-level token trajectories and tool counts, resolves parent channels,
and backfills historical usage into /workspace/data/telemetry.db.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_telemetry")

BRAIN_DIR = Path(os.environ.get("GEMINI_BRAIN_DIR", "/root/.gemini/antigravity-cli/brain"))
DB_PATH = Path(os.environ.get("TELEMETRY_DB_PATH", "/workspace/data/telemetry.db"))
SESSIONS_FILE = Path("/workspace/data/sessions.json")
SESSION_META_FILE = Path("/workspace/data/session_metadata.json")

KNOWN_CHANNELS: Dict[int, str] = {
    1542081375287640084: "zero-chat",
    1534436119888793750: "the-banana-stand",
    1534452820995080192: "lounge",
    1549555629222404096: "debug-huddle",
    1551465050072416286: "side-project",
    1552732003076669460: "mirrormere",
    1330447543477338202: "server-updates",
    1544955535722545253: "homelab",
    1550577908811178095: "brock-house",
    1550577910757458015: "vault",
    1548196929308065893: "baseball",
    1210466877835313155: "seerr-notifications",
    1453427860793463000: "seerr-requests-and-chat",
    1544953279664889888: "zero-ops",
    1544955538033348618: "shopping",
    1544953277592899615: "steam-deck",
    1544953275877556334: "home-assistant",
    1544955532765560924: "finances",
    1548196930788524094: "projects",
    1555973316752449576: "3d-print",
    1558202642663211169: "agora",
}
NAME_TO_CHID = {v: k for k, v in KNOWN_CHANNELS.items()}
EXTERNAL_CHANNEL_IDS = {
    1534436119888793750,
    1534452820995080192,
    1549555629222404096,
    1551465050072416286,
    1552732003076669460,
}

# Common sidecar intent phrases
SIDECAR_TERMS = [
    "Memory Consolidation",
    "synthesizing session carry-forward",
    "evaluating inbound chat messages",
    "reaction GIF selector",
    "Synthesize this resolution",
    "Hourly Standup",
    "Briefing Synthesizer",
    "Compactor",
    "session carry-forward context",
    "triaging Ryan's unread inbox",
    "Zero Mail Listener",
]

P_SESS = re.compile(r"\[Session:\s*([0-9]{15,20})\]")
P_GIF = re.compile(r"\[GIF Cadence Tracker \(Channel:\s*([0-9]{15,20})\)\]")
P_CRAB = re.compile(r"\[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT\]")
P_LOUNGE = re.compile(r"#lounge|in lounge", re.IGNORECASE)
P_BANANA = re.compile(r"#the-banana-stand|banana stand", re.IGNORECASE)
P_CH_HEADER = re.compile(
    r"(?:RECENT CHANNEL HISTORY \(#([a-z0-9-]+)|BROCK DISCORD (?:PUBLIC CHANNEL )?#([a-z0-9-]+)|Channel:\s*#?([a-z0-9-]+))",
    re.IGNORECASE,
)


def init_db(conn: sqlite3.Connection):
    """Ensure table exists and add is_backfill column if missing."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            channel_id INTEGER,
            channel_name TEXT,
            parent_channel_id INTEGER,
            parent_channel_name TEXT,
            is_thread INTEGER DEFAULT 0,
            thread_id INTEGER,
            conv_id TEXT,
            mode TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            thinking_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            is_backfill INTEGER DEFAULT 0
        );
        """
    )
    # Safely ensure is_backfill column exists for existing tables
    try:
        conn.execute("ALTER TABLE token_usage ADD COLUMN is_backfill INTEGER DEFAULT 0;")
    except sqlite3.OperationalError:
        pass

    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp ON token_usage (timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_parent ON token_usage (parent_channel_name);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_channel ON token_usage (channel_name);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_conv ON token_usage (conv_id);")


def load_channel_mappings() -> Dict[str, Tuple[Optional[int], str]]:
    """Build reverse lookup of conv_id -> (channel_id, channel_name) from persistent state."""
    mapping: Dict[str, Tuple[Optional[int], str]] = {}

    if SESSION_META_FILE.exists():
        try:
            with open(SESSION_META_FILE, "r", encoding="utf-8") as f:
                sm = json.load(f)
                for k, v in sm.items():
                    if not isinstance(v, dict):
                        continue
                    chid = int(k) if str(k).isdigit() else (1542081375287640084 if k == "home" else None)
                    chname = KNOWN_CHANNELS.get(chid, f"channel-{k}")
                    for field in ["conv_id", "parent_conv_id"]:
                        cid = v.get(field)
                        if cid:
                            mapping[str(cid)] = (chid, chname)
                    for h in v.get("conv_history", []):
                        if h:
                            mapping[str(h)] = (chid, chname)
        except Exception as e:
            logger.warning(f"Error loading session_metadata.json: {e}")

    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                for k, cid in s.items():
                    if cid:
                        chid = int(k) if str(k).isdigit() else (1542081375287640084 if k == "home" else None)
                        chname = KNOWN_CHANNELS.get(chid, f"channel-{k}")
                        mapping[str(cid)] = (chid, chname)
        except Exception as e:
            logger.warning(f"Error loading sessions.json: {e}")

    return mapping


def classify_conversation(
    conv_id: str,
    first_prompt: str,
    text_sample: str,
    step_count: int,
    mapped_ch: Optional[Tuple[Optional[int], str]],
) -> Tuple[Optional[int], str, str]:
    """Classify channel_id, channel_name, and mode ('home', 'external', 'sidecar')."""
    if mapped_ch:
        chid, chname = mapped_ch
        mode = "external" if chid in EXTERNAL_CHANNEL_IDS else "home"
        return chid, chname, mode

    def resolve_sidecar_task(prompt: str) -> str:
        if "Memory Consolidation" in prompt:
            return "memory-consolidation"
        if "evaluating inbound chat messages" in prompt:
            return "inbound-classifier"
        if "synthesizing session carry-forward" in prompt or "session carry-forward context" in prompt:
            return "session-compactor"
        if "triaging Ryan's unread inbox" in prompt or "Zero Mail Listener" in prompt:
            return "mail-triage"
        if "reaction GIF selector" in prompt:
            return "gif-selector"
        if "Synthesize this resolution" in prompt or "Hourly Standup" in prompt or "Briefing" in prompt:
            return "standup-synthesizer"
        if "pong" in prompt or "benchmark" in prompt:
            return "benchmark-ping"
        return "internal-daemon"

    if any(st in first_prompt for st in SIDECAR_TERMS) or step_count < 10:
        return None, resolve_sidecar_task(first_prompt), "sidecar"

    m = P_SESS.search(text_sample) or P_GIF.search(text_sample)
    if m:
        chid = int(m.group(1))
        chname = KNOWN_CHANNELS.get(chid, f"channel-{chid}")
        mode = "external" if chid in EXTERNAL_CHANNEL_IDS else "home"
        return chid, chname, mode

    if P_CRAB.search(first_prompt):
        if P_LOUNGE.search(text_sample):
            return 1534452820995080192, "lounge", "external"
        return 1534436119888793750, "the-banana-stand", "external"

    m_hdr = P_CH_HEADER.search(text_sample)
    if m_hdr:
        cname = (m_hdr.group(1) or m_hdr.group(2) or m_hdr.group(3) or "").lower()
        if cname in NAME_TO_CHID:
            k_chid = NAME_TO_CHID[cname]
            mode = "external" if k_chid in EXTERNAL_CHANNEL_IDS else "home"
            return k_chid, cname, mode

    return 1542081375287640084, "zero-chat", "home"



def _parse_proto_varints(b: bytes) -> dict:
    fields = {}
    i = 0
    while i < len(b):
        key = 0
        shift = 0
        while i < len(b):
            byte = b[i]
            i += 1
            key |= (byte & 0x7f) << shift
            if not (byte & 0x80):
                break
            shift += 7
        fnum = key >> 3
        wtype = key & 7
        if wtype == 0:
            val = 0
            shift = 0
            while i < len(b):
                byte = b[i]
                i += 1
                val |= (byte & 0x7f) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            fields[fnum] = val
        elif wtype == 2:
            length = 0
            shift = 0
            while i < len(b):
                byte = b[i]
                i += 1
                length |= (byte & 0x7f) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            sub_bytes = b[i:i+length]
            i += length
            fields[fnum] = sub_bytes
        elif wtype == 1:
            i += 8
        elif wtype == 5:
            i += 4
        else:
            break
    return fields


def extract_sqlite_step_tokens(conv_id: str) -> dict:
    """Extract exact token telemetry from internal agy conversations/<uuid>.db."""
    db_file = Path(f"/root/.gemini/antigravity-cli/conversations/{conv_id}.db")
    if not db_file.exists():
        return {}
    results = {}
    try:
        conn = sqlite3.connect(str(db_file), timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx;")
        for idx, meta in cur.fetchall():
            mfields = _parse_proto_varints(meta)
            if 9 in mfields and isinstance(mfields[9], bytes):
                sub = _parse_proto_varints(mfields[9])
                in_tok = sub.get(2, 0)
                out_tok = sub.get(3, 0)
                think_tok = sub.get(9, 0)
                txt_tok = sub.get(10, 0)
                results[idx] = {
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "thinking_tokens": think_tok,
                    "text_tokens": txt_tok,
                    "total_tokens": in_tok + out_tok,
                }
        conn.close()
    except Exception as e:
        logger.debug(f"Error reading sqlite tokens for {conv_id}: {e}")
    return results

def extract_turns_from_transcript(tpath: str, conv_id: Optional[str] = None) -> Tuple[List[Dict[str, Any]], str, str, int]:
    """Parse transcript.jsonl into grouped turns with computed token estimates."""
    turns_steps: List[List[Dict[str, Any]]] = []
    current_turn: List[Dict[str, Any]] = []
    first_prompt = ""
    text_sample_parts = []
    total_steps = 0

    with open(tpath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line_s = line.strip()
            if not line_s:
                continue
            total_steps += 1
            if len(text_sample_parts) < 30:
                text_sample_parts.append(line_s)
            try:
                obj = json.loads(line_s)
            except Exception:
                continue

            stype = obj.get("type")
            source = obj.get("source")
            is_user = (stype == "USER_INPUT" or source == "USER_EXPLICIT")

            if is_user:
                if not first_prompt:
                    first_prompt = (obj.get("content") or "").strip()
                if current_turn:
                    turns_steps.append(current_turn)
                    current_turn = []

            current_turn.append(obj)

        if current_turn:
            turns_steps.append(current_turn)

    text_sample = "\n".join(text_sample_parts)
    turns_data: List[Dict[str, Any]] = []
    running_context = 10000  # Baseline system prompt overhead

    sqlite_tokens = extract_sqlite_step_tokens(conv_id) if conv_id else {}

    for turn in turns_steps:
        in_tok = 0
        out_tok = 0
        think_tok = 0
        cache_tok = 0
        ts = 0.0
        exact_match = None

        for step in turn:
            step_idx = step.get("step_index")
            if step_idx is not None and step_idx in sqlite_tokens:
                exact_match = sqlite_tokens[step_idx]
                break

        for step in turn:
            created = step.get("created_at")
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    ts = dt.timestamp()
                except Exception:
                    pass

            content = step.get("content") or ""
            thinking = step.get("thinking") or ""
            tc = step.get("tool_calls") or []
            stype = step.get("type")
            source = step.get("source")

            step_in = max(10, len(content) // 4)
            step_out = 0
            step_think = len(thinking) // 4

            if tc:
                step_out += len(json.dumps(tc)) // 4

            if stype == "PLANNER_RESPONSE" or source == "MODEL":
                step_out += max(10, len(content) // 4)
                in_tok += running_context
                if running_context > 32000:
                    cache_tok += int(running_context * 0.85)
                running_context += (step_out + step_think)
            else:
                running_context += step_in

            out_tok += step_out
            think_tok += step_think

        if ts == 0.0:
            ts = time.time()

        if exact_match:
            in_tok = exact_match["input_tokens"]
            out_tok = exact_match["output_tokens"]
            think_tok = exact_match["thinking_tokens"]
            tot_turn = exact_match["total_tokens"]
        else:
            tot_turn = in_tok + out_tok

        turns_data.append({
            "timestamp": ts,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_tokens": cache_tok,
            "thinking_tokens": think_tok,
            "total_tokens": tot_turn,
            "step_count": len(turn),
        })

    return turns_data, first_prompt, text_sample, total_steps


def backfill_transcripts(dry_run: bool = False, force: bool = False) -> Dict[str, Any]:
    """Execute complete historical backfill into telemetry.db."""
    t0 = time.time()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    init_db(conn)

    # Gather existing state in telemetry.db
    # Never duplicate or overwrite live wire recordings (is_backfill = 0)
    cur = conn.cursor()
    live_convs: Dict[str, float] = {}
    for cid, min_ts in cur.execute(
        "SELECT conv_id, MIN(timestamp) FROM token_usage WHERE is_backfill = 0 AND conv_id IS NOT NULL GROUP BY conv_id"
    ).fetchall():
        if cid:
            live_convs[cid] = min_ts or 0.0

    existing_convs: Dict[str, float] = {}
    if not force:
        for cid, min_ts in cur.execute(
            "SELECT conv_id, MIN(timestamp) FROM token_usage WHERE conv_id IS NOT NULL GROUP BY conv_id"
        ).fetchall():
            if cid:
                existing_convs[cid] = min_ts or 0.0

    conv_mapping = load_channel_mappings()
    transcripts = glob.glob(str(BRAIN_DIR / "*" / ".system_generated" / "logs" / "transcript.jsonl"))

    stats = {
        "transcripts_scanned": len(transcripts),
        "transcripts_backfilled": 0,
        "transcripts_skipped": 0,
        "turns_inserted": 0,
        "steps_processed": 0,
        "tokens_by_channel": {},
        "turns_by_channel": {},
        "duration_s": 0.0,
        "dry_run": dry_run,
    }

    insert_rows = []

    for tpath in transcripts:
        conv_id = Path(tpath).parts[-4]
        earliest_existing_ts = existing_convs.get(conv_id)
        live_earliest = live_convs.get(conv_id)

        turns_data, first_prompt, text_sample, total_steps = extract_turns_from_transcript(tpath, conv_id=conv_id)
        if not turns_data:
            stats["transcripts_skipped"] += 1
            continue

        chid, chname, mode = classify_conversation(
            conv_id=conv_id,
            first_prompt=first_prompt,
            text_sample=text_sample,
            step_count=total_steps,
            mapped_ch=conv_mapping.get(conv_id),
        )

        turns_to_insert = []
        for t in turns_data:
            # If live records exist for this session, never insert turns at or after live recordings
            if live_earliest is not None and t["timestamp"] >= (live_earliest - 30.0):
                continue
            # If conversation already in telemetry.db and not forcing, skip
            if earliest_existing_ts is not None and not force and t["timestamp"] >= (earliest_existing_ts - 5.0):
                continue
            turns_to_insert.append(t)

        if not turns_to_insert:
            stats["transcripts_skipped"] += 1
            continue

        stats["transcripts_backfilled"] += 1
        stats["steps_processed"] += total_steps

        for t in turns_to_insert:
            parent_name = "internal" if mode == "sidecar" else chname
            parent_id = None if mode == "sidecar" else chid
            insert_rows.append((
                t["timestamp"],
                chid,
                chname,
                parent_id,
                parent_name,
                0,     # is_thread
                None,  # thread_id
                conv_id,
                mode,
                t["input_tokens"],
                t["output_tokens"],
                t["cache_read_tokens"],
                t["thinking_tokens"],
                t["total_tokens"],
                1,     # is_backfill
            ))
            stats["turns_inserted"] += 1
            group_key = f"daemon:{chname}" if mode == "sidecar" else chname
            stats["turns_by_channel"][group_key] = stats["turns_by_channel"].get(group_key, 0) + 1
            stats["tokens_by_channel"][group_key] = stats["tokens_by_channel"].get(group_key, 0) + t["total_tokens"]

    if not dry_run and insert_rows:
        with conn:
            if force:
                # If force, clear previously backfilled rows
                conn.execute("DELETE FROM token_usage WHERE is_backfill = 1;")
            conn.executemany(
                """
                INSERT INTO token_usage (
                    timestamp, channel_id, channel_name,
                    parent_channel_id, parent_channel_name,
                    is_thread, thread_id, conv_id, mode,
                    input_tokens, output_tokens, cache_read_tokens,
                    thinking_tokens, total_tokens, is_backfill
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                insert_rows,
            )

    conn.close()
    stats["duration_s"] = round(time.time() - t0, 3)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Zero Historical Transcript Telemetry Backfill CLI")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing to SQLite")
    parser.add_argument("--force", action="store_true", help="Re-backfill and replace earlier backfill entries")
    args = parser.parse_args()

    logger.info("Starting historical transcript telemetry backfill...")
    results = backfill_transcripts(dry_run=args.dry_run, force=args.force)

    print(json.dumps(results, indent=2))
    logger.info(
        f"Backfill finished: {results['transcripts_backfilled']} transcripts backfilled, "
        f"{results['turns_inserted']} turns inserted in {results['duration_s']}s."
    )


if __name__ == "__main__":
    main()
