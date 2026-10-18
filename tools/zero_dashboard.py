#!/usr/bin/env python3
"""Zero Live Context & Telemetry Dashboard Engine.

Aggregates local token telemetry, prompt cache efficiency, and step-by-step
context trajectories for zero_health_server.py. Exposes pure-local JSON API
endpoints and an ultra-modern 90/10 slate web dashboard without token costs or latency.
Data is organized strictly by Discord Channel, presenting current active sessions
and stitched compacted predecessor sessions side-by-side.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from tools.backfill_telemetry import extract_sqlite_step_tokens

logger = logging.getLogger("zero_dashboard")

PT_TZ = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
DB_PATH = DATA_DIR / "telemetry.db"
BRAIN_DIR = Path("/root/.gemini/antigravity-cli/brain")
SESSIONS_FILE = DATA_DIR / "sessions.json"

MODEL_LIMITS = {
    "gemini-4-argon-high": {"limit": 1000000, "tripwire": 800000, "name": "Gemini 4 Argon (High)"},
    "gemini-4-argon-medium": {"limit": 1000000, "tripwire": 800000, "name": "Gemini 4 Argon (Medium)"},
    "gemini-4-argon-low": {"limit": 1000000, "tripwire": 800000, "name": "Gemini 4 Argon (Low)"},
    "gemini-3.8-flash-high": {"limit": 1000000, "tripwire": 800000, "name": "Gemini 3.8 Flash (High)"},
    "gemini-3.8-flash-medium": {"limit": 1000000, "tripwire": 800000, "name": "Gemini 3.8 Flash (Medium)"},
    "gemini-3.8-flash-low": {"limit": 1000000, "tripwire": 800000, "name": "Gemini 3.8 Flash (Low)"},
    "gemini-3.8-flash": {"limit": 1000000, "tripwire": 800000, "name": "Gemini 3.8 Flash"},
    "gemini-3.7-flash-high": {"limit": 256000, "tripwire": 160000, "name": "Gemini 3.7 Flash (High)"},
    "gemini-3.7-flash": {"limit": 256000, "tripwire": 160000, "name": "Gemini 3.7 Flash"},
    "gemini-3.7-pro": {"limit": 128000, "tripwire": 100000, "name": "Gemini 3.7 Pro"},
    "claude-sonnet-5-5-high": {"limit": 200000, "tripwire": 160000, "name": "Claude Sonnet 5.5 (High)"},
    "claude-sonnet-5-5-medium": {"limit": 200000, "tripwire": 160000, "name": "Claude Sonnet 5.5 (Medium)"},
    "claude-sonnet-5-5-low": {"limit": 200000, "tripwire": 160000, "name": "Claude Sonnet 5.5 (Low)"},
    "claude-opus-5-5-high": {"limit": 200000, "tripwire": 160000, "name": "Claude Opus 5.5 (High)"},
    "claude-opus-5-5-medium": {"limit": 200000, "tripwire": 160000, "name": "Claude Opus 5.5 (Medium)"},
    "claude-opus-5-5-low": {"limit": 200000, "tripwire": 160000, "name": "Claude Opus 5.5 (Low)"},
    "claude-sonnet-4-6": {"limit": 160000, "tripwire": 130000, "name": "Claude Sonnet 4.6"},
    "claude-opus-4-6": {"limit": 160000, "tripwire": 130000, "name": "Claude Opus 4.6"},
}

DEFAULT_MODEL_CONFIG = {"limit": 1000000, "tripwire": 800000, "name": "Gemini 4 Argon (High)"}
WEEKLY_TOKEN_CEILING = 440000000  # Empirical Argon-weighted weekly ceiling (~440M wire tokens)

# Dedicated 24/7 persistent warm worker daemons (tools/bridge_daemons.py:DEDICATED_CHANNEL_CONFIGS)
PERSISTENT_CHANNELS = {
    "zero-chat",
    "the-banana-stand",
    "lounge",
}

# Autonomous internal daemon tasks to exclude from Discord channel selectors
DAEMON_TASK_NAMES = {
    "session-compactor",
    "inbound-classifier",
    "gif-selector",
    "mail-triage",
    "memory-consolidation",
    "standup-synthesizer",
    "benchmark-ping",
    "internal-daemon",
    "sidecar",
    "internal",
}


def get_avatar_data_uri(agent: str = "zero") -> str:
    """Load agent's official avatar image as base64 data URI."""
    if agent == "ivy":
        paths = [
            DATA_DIR / "ivy_avatar.png",
            Path("/workspace/assets/ivy_avatar.png"),
        ]
        fallback_svg = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="45" fill="%23059669"/><text x="50" y="65" font-size="45" text-anchor="middle" fill="white">⚾</text></svg>'
    else:
        paths = [
            Path("/workspace/assets/zero_avatar_animated.gif"),
            DATA_DIR / "zero_avatar.png",
            Path("/workspace/assets/zero_avatar.png"),
        ]
        fallback_svg = "data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><circle cx=%2250%22 cy=%2250%22 r=%2240%22 stroke=%22%2306b6d4%22 stroke-width=%228%22 fill=%22none%22/><line x1=%2225%22 y1=%2275%22 x2=%2275%22 y2=%2225%22 stroke=%22%2306b6d4%22 stroke-width=%228%22/></svg>"

    for p in paths:
        if p.exists():
            try:
                mime = "image/gif" if p.suffix.lower() == ".gif" else "image/png"
                with open(p, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                    return f"data:{mime};base64,{b64}"
            except Exception:
                pass
    return fallback_svg



def format_channel_display_name(name: str, is_persistent: bool) -> str:
    """Format channel display name marking persistent channels with [P]."""
    clean = name.lstrip("#")
    if is_persistent:
        return f"[P] #{clean}"
    return f"#{clean}"


def get_active_model_config() -> Dict[str, Any]:
    """Retrieve active model and its context thresholds."""
    active_model_raw = os.environ.get("AGY_ACTIVE_MODEL", "gemini-4-argon-high")
    rules_file = Path("/workspace/config/runtime_rules.json")
    if rules_file.exists():
        try:
            with open(rules_file, "r", encoding="utf-8") as rf:
                r_data = json.load(rf)
                if r_data.get("active_model"):
                    active_model_raw = r_data["active_model"]
        except Exception:
            pass
    return MODEL_LIMITS.get(active_model_raw, DEFAULT_MODEL_CONFIG)


def get_channel_map() -> Dict[str, str]:
    """Retrieve dynamic channel ID to channel name mapping from database and defaults."""
    channels = {
        "home": "zero-chat",
        "1542081375287640084": "zero-chat",
        "1544953277592899615": "steam-deck",
        "1534436119888793750": "the-banana-stand",
        "1534452820995080192": "lounge",
        "1330447543477338202": "server-updates",
        "1544955535722545253": "homelab",
        "1550577908811178095": "brock-house",
        "1550577910757458015": "vault",
        "1548196929308065893": "baseball",
        "1210466877835313155": "seerr-notifications",
        "1453427860793463000": "seerr-requests-and-chat",
        "1544953279664889888": "zero-ops",
        "1544955538033348618": "shopping",
        "1544953275877556334": "home-assistant",
        "1548196930788524094": "projects",
        "1555973316752449576": "3d-print",
        "1544955532765560924": "finances",
        "1558202642663211169": "agora",
        "1549555629222404096": "debug-huddle",
        "1551465050072416286": "side-project",
        "1552732003076669460": "mirrormere",
    }
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=3.0)
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT channel_id, channel_name FROM token_usage WHERE channel_id IS NOT NULL AND channel_name IS NOT NULL;")
            for cid, cname in cur.fetchall():
                if cid and cname:
                    channels[str(cid)] = cname
            conn.close()
        except Exception:
            pass
    return channels


def get_active_sessions_map() -> Tuple[Dict[str, str], Dict[str, str]]:
    """Retrieve active session mappings: (ch_id_to_conv, ch_name_to_conv)."""
    channel_map = get_channel_map()
    ch_id_to_conv = {}
    ch_name_to_conv = {}
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                ch_id_to_conv = json.load(f)
        except Exception:
            pass
    for ch_id, conv_id in ch_id_to_conv.items():
        if conv_id:
            cname = channel_map.get(str(ch_id), f"channel-{ch_id}")
            ch_name_to_conv[cname] = str(conv_id)
    return ch_id_to_conv, ch_name_to_conv


def parse_session_transcript(
    conv_id: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], int, List[Dict[str, Any]]]:
    """Parse step trajectory, tool statistics, and compaction events directly from on-disk transcript.jsonl.
    
    Returns: (timeline_events, tool_usage_counts, total_estimated_tokens, compactions)
    """
    timeline: List[Dict[str, Any]] = []
    tool_usage: Dict[str, int] = {}
    compactions: List[Dict[str, Any]] = []
    running_tokens = 10000  # Baseline system prompt overhead
    sqlite_tokens = extract_sqlite_step_tokens(conv_id) if conv_id else {}

    # Search for matching brain directory by prefix or exact match
    tpath = BRAIN_DIR / str(conv_id) / ".system_generated" / "logs" / "transcript.jsonl"
    if not tpath.exists():
        # Try matching prefix
        matches = list(BRAIN_DIR.glob(f"*{str(conv_id)[:8]}*"))
        if matches:
            tpath = matches[0] / ".system_generated" / "logs" / "transcript.jsonl"

    if not tpath.exists():
        return timeline, tool_usage, running_tokens, compactions

    try:
        with open(tpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    d = json.loads(line_s)
                    stype = d.get("type")
                    source = d.get("source")
                    step_idx = d.get("step_index", len(timeline))
                    created = d.get("created_at", "")
                    content = d.get("content") or ""
                    thinking = d.get("thinking") or ""
                    tool_calls = d.get("tool_calls") or []

                    dt_str = ""
                    dt_short = ""
                    epoch_ts = 0.0
                    if created:
                        try:
                            utc_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            pt_dt = utc_dt.astimezone(PT_TZ)
                            dt_str = pt_dt.strftime("%I:%M:%S %p PT")
                            dt_short = pt_dt.strftime("%I:%M %p")
                            epoch_ts = pt_dt.timestamp()
                        except Exception:
                            dt_str = created

                    # Detect Compaction / Checkpoint
                    is_compaction = (stype == "CHECKPOINT") or (
                        source == "SYSTEM" and "# Resuming from a compaction" in content
                    )
                    if is_compaction:
                        tokens_before = running_tokens
                        compaction_summary_tokens = max(500, len(content) // 4)
                        running_tokens = 10000 + compaction_summary_tokens
                        recovered = max(0, tokens_before - running_tokens)

                        comp_record = {
                            "step": step_idx,
                            "time": dt_str,
                            "time_short": dt_short,
                            "timestamp": epoch_ts,
                            "tokens_before": tokens_before,
                            "tokens_after": running_tokens,
                            "recovered_tokens": recovered,
                        }
                        compactions.append(comp_record)

                        timeline.append(
                            {
                                "step": step_idx,
                                "category": "compaction",
                                "tool": "compaction",
                                "title": "Session Compaction Checkpoint",
                                "detail": f"Reclaimed {recovered:,} tokens (Context reset from {tokens_before:,} to {running_tokens:,})",
                                "time": dt_str,
                                "time_short": dt_short,
                                "timestamp": epoch_ts,
                                "status": "DONE",
                                "tokens_est": running_tokens,
                                "tokens_before": tokens_before,
                                "recovered_tokens": recovered,
                                "is_compaction": True,
                            }
                        )
                        continue

                    # Estimate token footprint for step
                    delta_tokens = max(10, len(content) // 4 + len(thinking) // 4)
                    if tool_calls:
                        delta_tokens += len(json.dumps(tool_calls)) // 4
                    running_tokens += delta_tokens

                    if stype == "USER_INPUT":
                        timeline.append(
                            {
                                "step": step_idx,
                                "category": "user",
                                "tool": "user",
                                "title": "User Request",
                                "detail": (content[:140] + "...") if len(content) > 140 else content,
                                "time": dt_str,
                                "time_short": dt_short,
                                "timestamp": epoch_ts,
                                "status": "DONE",
                                "tokens_est": running_tokens,
                                "is_compaction": False,
                            }
                        )
                    elif tool_calls and len(tool_calls) > 0:
                        tc = tool_calls[0]
                        tname = tc.get("name") or tc.get("tool") or "tool"
                        args = tc.get("args") or {}
                        tool_usage[tname] = tool_usage.get(tname, 0) + 1

                        detail = ""
                        if tname == "view_file":
                            detail = str(args.get("AbsolutePath", "")).strip('"')
                        elif tname == "run_command":
                            detail = str(args.get("CommandLine", "")).strip('"')[:90]
                        elif tname == "search_web":
                            detail = str(args.get("query", "")).strip('"')
                        elif tname in ("replace_file_content", "write_to_file"):
                            detail = str(args.get("TargetFile", "")).strip('"')
                        elif tname == "manage_task":
                            act = args.get("Action", "")
                            tid = args.get("TaskId", "")
                            detail = f"{act} {tid}".strip()
                        else:
                            summary = tc.get("toolSummary", "")
                            detail = str(summary or args)[:70]

                        timeline.append(
                            {
                                "step": step_idx,
                                "category": "tool",
                                "tool": tname,
                                "title": f"Tool: {tname}",
                                "detail": detail,
                                "time": dt_str,
                                "time_short": dt_short,
                                "timestamp": epoch_ts,
                                "status": d.get("status", "DONE"),
                                "tokens_est": running_tokens,
                                "is_compaction": False,
                            }
                        )
                    elif stype == "PLANNER_RESPONSE":
                        if step_idx in sqlite_tokens:
                            running_tokens = sqlite_tokens[step_idx]["total_tokens"]
                        snippet = (
                            thinking[:120] if thinking else (content[:120] if content else "Reasoning pass")
                        )
                        timeline.append(
                            {
                                "step": step_idx,
                                "category": "reasoning",
                                "tool": "reasoning",
                                "title": "Planner Reasoning",
                                "detail": snippet.strip(),
                                "time": dt_str,
                                "time_short": dt_short,
                                "timestamp": epoch_ts,
                                "status": "DONE",
                                "tokens_est": running_tokens,
                                "is_compaction": False,
                            }
                        )
                except Exception:
                    continue
    except Exception as e:
        logger.error(f"Error parsing transcript {tpath}: {e}")

    return timeline, tool_usage, running_tokens, compactions


def build_chart_points(timeline: List[Dict[str, Any]], max_points: int = 50) -> List[Dict[str, Any]]:
    """Downsample trajectory events for SVG timeline chart while preserving critical inflection points."""
    if not timeline:
        return []
    if len(timeline) <= max_points:
        return [
            {
                "step": e["step"],
                "tokens": e["tokens_est"],
                "label": e.get("tool", e["category"]),
                "time": e.get("time_short", ""),
                "timestamp": e.get("timestamp", 0),
                "is_compaction": e.get("is_compaction", False),
                "is_rollover": e.get("is_rollover", False),
                "tokens_before": e.get("tokens_before", 0),
                "recovered": e.get("recovered_tokens", 0),
                "session_id": e.get("session_id", ""),
            }
            for e in timeline
        ]

    critical_indices = {0, len(timeline) - 1}
    for idx, e in enumerate(timeline):
        if e.get("is_compaction"):
            if idx > 0:
                critical_indices.add(idx - 1)
            critical_indices.add(idx)

    stride = max(1, len(timeline) // max_points)
    selected_indices = set(critical_indices)
    for i in range(0, len(timeline), stride):
        selected_indices.add(i)

    sorted_indices = sorted(selected_indices)
    return [
        {
            "step": timeline[i]["step"],
            "tokens": timeline[i]["tokens_est"],
            "label": timeline[i].get("tool", timeline[i]["category"]),
            "time": timeline[i].get("time_short", ""),
            "timestamp": timeline[i].get("timestamp", 0),
            "is_compaction": timeline[i].get("is_compaction", False),
            "is_rollover": timeline[i].get("is_rollover", False),
            "tokens_before": timeline[i].get("tokens_before", 0),
            "recovered": timeline[i].get("recovered_tokens", 0),
            "session_id": timeline[i].get("session_id", ""),
        }
        for i in sorted_indices
    ]


def get_session_details(conv_id: str, model_config: Dict[str, Any], include_timeline: bool = True) -> Dict[str, Any]:
    """Fetch complete session details from database and on-disk transcript."""
    limit = model_config["limit"]
    tripwire = model_config["tripwire"]

    turns: List[Dict[str, Any]] = []
    tot_in, tot_out, tot_cache, tot_think, tot_all = 0, 0, 0, 0, 0
    latest_turn_input = 0
    ch_name = "session"
    last_active_ts = 0.0

    if DB_PATH.exists() and conv_id:
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, timestamp, channel_name, parent_channel_name, input_tokens, output_tokens,
                       cache_read_tokens, thinking_tokens, total_tokens
                FROM token_usage
                WHERE conv_id = ?
                ORDER BY timestamp ASC;
                """,
                (conv_id,),
            )
            rows = cur.fetchall()
            for i, r in enumerate(rows):
                d = dict(r)
                dt = datetime.fromtimestamp(d["timestamp"], PT_TZ)
                in_tok = d["input_tokens"] or 0
                out_tok = d["output_tokens"] or 0
                c_tok = d["cache_read_tokens"] or 0
                th_tok = d["thinking_tokens"] or 0
                t_tok = d["total_tokens"] or 0
                last_active_ts = d["timestamp"]

                tot_in += in_tok
                tot_out += out_tok
                tot_cache += c_tok
                tot_think += th_tok
                tot_all += t_tok
                latest_turn_input = in_tok
                ch_name = d["channel_name"] or d["parent_channel_name"] or ch_name

                eval_total = in_tok + c_tok
                c_rate = round(c_tok / eval_total * 100, 1) if eval_total > 0 else 0.0

                turns.append(
                    {
                        "turn_num": i + 1,
                        "time_pt": dt.strftime("%I:%M:%S %p PT"),
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cache_read_tokens": c_tok,
                        "thinking_tokens": th_tok,
                        "total_tokens": t_tok,
                        "cache_hit_rate": c_rate,
                    }
                )
            conn.close()
        except Exception as e:
            logger.error(f"Error fetching turns for session {conv_id}: {e}")

    timeline, tool_stats, est_tokens, compactions = ([], {}, 0, [])
    if include_timeline and conv_id:
        timeline, tool_stats, est_tokens, compactions = parse_session_transcript(conv_id)

    current_tokens = est_tokens if est_tokens > 0 else latest_turn_input
    fill_pct = round((current_tokens / limit) * 100, 1) if limit > 0 else 0.0
    headroom = max(0, limit - current_tokens)
    tripwire_headroom = max(0, tripwire - current_tokens)

    eval_total_sess = tot_in + tot_cache
    overall_cache_rate = round(tot_cache / eval_total_sess * 100, 1) if eval_total_sess > 0 else 0.0
    cache_leverage = round(eval_total_sess / tot_in, 1) if tot_in > 0 else 1.0

    chart_points = build_chart_points(timeline, max_points=50) if include_timeline else []
    dt_last = datetime.fromtimestamp(last_active_ts, PT_TZ) if last_active_ts > 0 else None
    last_active_pt = dt_last.strftime("%b %d %I:%M %p PT") if dt_last else "Just started"

    is_persistent = ch_name in PERSISTENT_CHANNELS
    lane_type = "persistent" if is_persistent else "dynamic"
    lane_label = "24/7 Warm Daemon" if is_persistent else "Dynamic Ephemeral Worker"

    return {
        "conv_id": conv_id,
        "channel_name": ch_name,
        "display_name": format_channel_display_name(ch_name, is_persistent),
        "is_persistent": is_persistent,
        "lane_type": lane_type,
        "lane_label": lane_label,
        "current_tokens": current_tokens,
        "fill_pct": fill_pct,
        "headroom_tokens": headroom,
        "tripwire_headroom": tripwire_headroom,
        "is_above_tripwire": current_tokens >= tripwire,
        "total_input": tot_in,
        "total_output": tot_out,
        "total_cache": tot_cache,
        "total_thinking": tot_think,
        "grand_total": tot_all,
        "cache_hit_rate": overall_cache_rate,
        "cache_leverage": cache_leverage,
        "turn_count": len(turns),
        "step_count": len(timeline),
        "last_active_timestamp": last_active_ts,
        "last_active_pt": last_active_pt,
        "turns": turns,
        "timeline": timeline,
        "chart_points": chart_points,
        "tool_stats": tool_stats,
        "compactions": compactions,
        "compaction_count": len(compactions),
        "total_tokens_recovered": sum(c.get("recovered_tokens", 0) for c in compactions),
    }


def stitch_session_timelines(
    session_records: List[Dict[str, Any]],
    model_config: Dict[str, Any],
    label: str = "stitched-predecessors",
) -> Dict[str, Any]:
    """Stitch multiple session transcripts into a continuous chronological timeline."""
    combined_timeline: List[Dict[str, Any]] = []
    total_tools: Dict[str, int] = {}
    total_compactions: List[Dict[str, Any]] = []
    turns_combined: List[Dict[str, Any]] = []
    tot_in, tot_out, tot_cache, tot_think, tot_all = 0, 0, 0, 0, 0
    step_counter = 0

    for idx, s in enumerate(session_records):
        cid = s["conv_id"]
        tl, tools, tok, comp = parse_session_transcript(cid)
        for k, v in tools.items():
            total_tools[k] = total_tools.get(k, 0) + v
        for c in comp:
            total_compactions.append(c)

        tot_in += s.get("total_input", 0)
        tot_out += s.get("total_output", 0)
        tot_cache += s.get("total_cache", 0)
        tot_think += s.get("total_thinking", 0)
        tot_all += s.get("grand_total", 0)

        # Insert session rollover event between sessions
        if idx > 0 and combined_timeline:
            prev_last = combined_timeline[-1]
            first_next = tl[0] if tl else None
            ts = first_next["timestamp"] if first_next and first_next.get("timestamp") else prev_last.get("timestamp", 0)
            t_str = first_next["time"] if first_next else prev_last.get("time", "")
            t_short = first_next["time_short"] if first_next else prev_last.get("time_short", "")
            step_counter += 1
            rec = max(0, prev_last.get("tokens_est", 10000) - 10000)
            rollover_event = {
                "step": step_counter,
                "category": "compaction",
                "tool": "compaction",
                "title": f"Session Rollover ({session_records[idx-1]['conv_id'][:8]} → {cid[:8]})",
                "detail": f"Session archived ({session_records[idx-1].get('turn_count', 0)} turns). Fresh context handoff.",
                "time": t_str,
                "time_short": t_short,
                "timestamp": ts,
                "status": "DONE",
                "tokens_est": 10000,
                "tokens_before": prev_last.get("tokens_est", 10000),
                "recovered_tokens": rec,
                "is_compaction": True,
                "is_rollover": True,
                "session_id": cid,
            }
            total_compactions.append(rollover_event)
            combined_timeline.append(rollover_event)

        for it in tl:
            step_counter += 1
            it_c = dict(it)
            it_c["step"] = step_counter
            it_c["session_id"] = cid
            combined_timeline.append(it_c)

    pts = build_chart_points(combined_timeline, max_points=120)
    eval_tot = tot_in + tot_cache
    c_rate = round(tot_cache / eval_tot * 100, 1) if eval_tot > 0 else 0.0
    c_lev = round(eval_tot / tot_in, 1) if tot_in > 0 else 1.0

    return {
        "conv_id": label,
        "is_active": False,
        "step_count": len(combined_timeline),
        "turn_count": sum(s.get("turn_count", 0) for s in session_records),
        "total_input": tot_in,
        "total_output": tot_out,
        "total_cache": tot_cache,
        "total_thinking": tot_think,
        "grand_total": tot_all,
        "cache_hit_rate": c_rate,
        "cache_leverage": c_lev,
        "timeline": combined_timeline,
        "chart_points": pts,
        "tool_stats": total_tools,
        "compactions": total_compactions,
        "compaction_count": len(total_compactions),
        "total_tokens_recovered": sum(c.get("recovered_tokens", 0) for c in total_compactions),
        "turns": turns_combined,
    }


def get_channel_rollups(hours: float = 168.0) -> List[Dict[str, Any]]:
    """Get 7-day token summary across all persistent Discord channels."""
    if not DB_PATH.exists():
        return []
    since = time.time() - (hours * 3600.0)
    rollups = []
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                parent_channel_name,
                COUNT(*) as turn_count,
                SUM(is_thread) as thread_turn_count,
                SUM(input_tokens) as total_input,
                SUM(output_tokens) as total_output,
                SUM(cache_read_tokens) as total_cache,
                SUM(thinking_tokens) as total_thinking,
                SUM(total_tokens) as grand_total
            FROM token_usage
            WHERE timestamp >= ? AND (mode != 'sidecar' OR mode IS NULL) AND parent_channel_name NOT IN ('sidecar', 'internal')
            GROUP BY parent_channel_name
            ORDER BY grand_total DESC;
            """,
            (since,),
        )
        # Check if live quota attribution data is available
        attr_map = {}
        try:
            from tools.quota_poller import get_quota_attribution
            attr_data = get_quota_attribution(hours=hours)
            for b in attr_data.get("breakdown", []):
                attr_map[b["channel"]] = b
        except Exception:
            pass

        for r in cur.fetchall():
            d = dict(r)
            ch = d["parent_channel_name"] or "unknown"
            tot_eval = (d["total_input"] or 0) + (d["total_cache"] or 0)
            cache_rate = (
                round((d["total_cache"] or 0) / tot_eval * 100, 1) if tot_eval > 0 else 0.0
            )
            is_persistent = ch in PERSISTENT_CHANNELS
            d["is_persistent"] = is_persistent
            d["display_name"] = format_channel_display_name(ch, is_persistent)
            d["cache_hit_rate"] = cache_rate
            # Prefer ground-truth attributed quota burn if non-zero, fallback to ceiling estimate
            if ch in attr_map and attr_map[ch].get("quota_pct_burned", 0.0) > 0.0:
                d["pct_weekly"] = round(attr_map[ch]["quota_pct_burned"], 2)
                d["pct_weekly_ground_truth"] = True
                d["share_of_total_burn_pct"] = attr_map[ch].get("share_of_total_burn_pct", 0.0)
            else:
                d["pct_weekly"] = round(((d.get("grand_total") or 0) / WEEKLY_TOKEN_CEILING) * 100, 2)
                d["pct_weekly_ground_truth"] = False
            rollups.append(d)
        conn.close()
    except Exception as e:
        logger.error(f"Error querying channel rollups: {e}")
    return rollups


def get_daemon_rollups(hours: float = 168.0) -> List[Dict[str, Any]]:
    """Get 7-day token summary across autonomous internal daemon tasks."""
    if not DB_PATH.exists():
        return []
    since = time.time() - (hours * 3600.0)
    rollups = []
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                channel_name as task_name,
                COUNT(*) as turn_count,
                SUM(input_tokens) as total_input,
                SUM(output_tokens) as total_output,
                SUM(cache_read_tokens) as total_cache,
                SUM(thinking_tokens) as total_thinking,
                SUM(total_tokens) as grand_total
            FROM token_usage
            WHERE timestamp >= ? AND (mode = 'sidecar' OR parent_channel_name IN ('sidecar', 'internal'))
            GROUP BY channel_name
            ORDER BY grand_total DESC;
            """,
            (since,),
        )
        for r in cur.fetchall():
            d = dict(r)
            task = d["task_name"] or "internal-daemon"
            tot_eval = (d["total_input"] or 0) + (d["total_cache"] or 0)
            cache_rate = (
                round((d["total_cache"] or 0) / tot_eval * 100, 1) if tot_eval > 0 else 0.0
            )
            d["display_name"] = task
            d["cache_hit_rate"] = cache_rate
            d["pct_weekly"] = round(((d.get("grand_total") or 0) / WEEKLY_TOKEN_CEILING) * 100, 2)
            rollups.append(d)
        conn.close()
    except Exception as e:
        logger.error(f"Error querying daemon rollups: {e}")
    return rollups


def get_sessions_list(only_active: bool = True, agent: str = "zero") -> List[Dict[str, Any]]:
    """Return all available sessions from telemetry.db and sessions.json, sorted by recency."""
    if agent == "ivy":
        try:
            import tools.ivy_telemetry as ivy_telemetry
            import importlib
            ivy_telemetry = importlib.reload(ivy_telemetry)
            return ivy_telemetry.get_ivy_sessions_list()
        except Exception as e:
            logger.exception("Failed to load Ivy sessions list: %s", e)
            return []

    channel_map = get_channel_map()
    _, active_conv_map = get_active_sessions_map()

    sessions = []
    seen = set()

    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 
                    conv_id,
                    channel_name,
                    parent_channel_name,
                    COUNT(*) as turn_count,
                    MAX(timestamp) as last_active,
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(cache_read_tokens) as total_cache,
                    SUM(thinking_tokens) as total_thinking,
                    SUM(total_tokens) as grand_total
                FROM token_usage
                WHERE conv_id IS NOT NULL AND conv_id != ""
                GROUP BY conv_id
                ORDER BY last_active DESC
                LIMIT 50;
                """
            )
            for r in cur.fetchall():
                d = dict(r)
                cid = d["conv_id"]
                seen.add(cid)
                tot_eval = (d["total_input"] or 0) + (d["total_cache"] or 0)
                cache_hit_rate = (
                    round((d["total_cache"] or 0) / tot_eval * 100, 1) if tot_eval > 0 else 0.0
                )
                dt = datetime.fromtimestamp(d["last_active"], PT_TZ)
                
                ch = d["channel_name"] or d["parent_channel_name"] or "session"
                is_active = (cid in active_conv_map.values())
                is_persistent = ch in PERSISTENT_CHANNELS

                sessions.append(
                    {
                        "conv_id": cid,
                        "channel_name": ch,
                        "display_name": format_channel_display_name(ch, is_persistent),
                        "is_persistent": is_persistent,
                        "turn_count": d["turn_count"],
                        "last_active_timestamp": d["last_active"],
                        "last_active_pt": dt.strftime("%b %d %I:%M %p PT"),
                        "total_input": d["total_input"] or 0,
                        "total_output": d["total_output"] or 0,
                        "total_cache": d["total_cache"] or 0,
                        "total_thinking": d["total_thinking"] or 0,
                        "grand_total": d["grand_total"] or 0,
                        "pct_weekly": round(((d.get("grand_total") or 0) / WEEKLY_TOKEN_CEILING) * 100, 2),
                        "cache_hit_rate": cache_hit_rate,
                        "is_active": is_active,
                    }
                )
            conn.close()
        except Exception as e:
            logger.error(f"Error querying sessions from telemetry.db: {e}")

    for ch_name, conv_id in active_conv_map.items():
        if conv_id and conv_id not in seen:
            seen.add(conv_id)
            sessions.append(
                {
                    "conv_id": conv_id,
                    "channel_name": ch_name,
                    "display_name": format_channel_display_name(ch_name, ch_name in PERSISTENT_CHANNELS),
                    "is_persistent": ch_name in PERSISTENT_CHANNELS,
                    "turn_count": 0,
                    "last_active_timestamp": 0,
                    "last_active_pt": "Just started",
                    "total_input": 0,
                    "total_output": 0,
                    "total_cache": 0,
                    "total_thinking": 0,
                    "grand_total": 0,
                    "cache_hit_rate": 0.0,
                    "is_active": True,
                },
            )

    if only_active and active_conv_map:
        sessions = [s for s in sessions if s.get("conv_id") in active_conv_map.values()]

    sessions.sort(key=lambda s: (s["last_active_timestamp"], s["turn_count"]), reverse=True)
    return sessions


def get_telemetry_payload(target: Optional[str] = None, compacted_id: Optional[str] = None, agent: str = "zero") -> Dict[str, Any]:
    """Compile channel-centric telemetry payload presenting active and stitched compacted sessions side-by-side."""
    if agent == "ivy":
        try:
            import tools.ivy_telemetry as ivy_telemetry
            import importlib
            ivy_telemetry = importlib.reload(ivy_telemetry)
            return ivy_telemetry.get_ivy_telemetry(compacted_id=compacted_id)
        except Exception as e:
            logger.exception("Failed to load Ivy telemetry payload: %s", e)
            return {
                "status": "degraded",
                "agent": "ivy",
                "error": str(e),
                "generated_at_pt": datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p %Z"),
                "model": DEFAULT_MODEL_CONFIG,
                "channel": {"name": "baseball", "display_name": "#baseball", "lane_type": "dedicated", "lane_label": "Sabermetric Assistant GM", "is_persistent": True},
                "channels": [{"name": "baseball", "display_name": "#baseball", "lane_type": "dedicated", "lane_label": "Sabermetric Assistant GM", "is_persistent": True}],
                "active_session": None,
                "compacted_sessions": [],
                "sessions": [],
                "channel_rollups": [],
                "daemon_rollups": [],
            }

    channel_map = get_channel_map()
    ch_id_to_conv, ch_name_to_conv = get_active_sessions_map()
    model_config = get_active_model_config()

    channel_sessions_raw: Dict[str, List[Dict[str, Any]]] = {}
    conv_to_channel: Dict[str, str] = {}

    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 
                    COALESCE(parent_channel_name, channel_name) as ch_raw,
                    conv_id,
                    COUNT(*) as turn_count,
                    MIN(timestamp) as start_timestamp,
                    MAX(timestamp) as last_active,
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(cache_read_tokens) as total_cache,
                    SUM(thinking_tokens) as total_thinking,
                    SUM(total_tokens) as grand_total
                FROM token_usage
                WHERE conv_id IS NOT NULL AND conv_id != ''
                GROUP BY ch_raw, conv_id
                ORDER BY start_timestamp ASC;
                """
            )
            for r in cur.fetchall():
                d = dict(r)
                ch = d["ch_raw"]
                cid = d["conv_id"]
                if not ch or ch in DAEMON_TASK_NAMES:
                    continue

                for cname, a_cid in ch_name_to_conv.items():
                    if a_cid == cid:
                        ch = cname
                        break

                conv_to_channel[cid] = ch
                if ch not in channel_sessions_raw:
                    channel_sessions_raw[ch] = []

                tot_eval = (d["total_input"] or 0) + (d["total_cache"] or 0)
                cache_rate = round((d["total_cache"] or 0) / tot_eval * 100, 1) if tot_eval > 0 else 0.0
                dt = datetime.fromtimestamp(d["last_active"], PT_TZ)

                channel_sessions_raw[ch].append(
                    {
                        "conv_id": cid,
                        "turn_count": d["turn_count"],
                        "start_timestamp": d["start_timestamp"],
                        "last_active_timestamp": d["last_active"],
                        "last_active_pt": dt.strftime("%b %d %I:%M %p PT"),
                        "total_input": d["total_input"] or 0,
                        "total_output": d["total_output"] or 0,
                        "total_cache": d["total_cache"] or 0,
                        "total_thinking": d["total_thinking"] or 0,
                        "grand_total": d["grand_total"] or 0,
                        "cache_hit_rate": cache_rate,
                    }
                )
            conn.close()
        except Exception as e:
            logger.error(f"Error querying channel sessions: {e}")

    for cname, cid in ch_name_to_conv.items():
        conv_to_channel[cid] = cname
        if cname not in channel_sessions_raw:
            channel_sessions_raw[cname] = [
                {
                    "conv_id": cid,
                    "turn_count": 0,
                    "start_timestamp": time.time(),
                    "last_active_timestamp": time.time(),
                    "last_active_pt": "Just started",
                    "total_input": 0,
                    "total_output": 0,
                    "total_cache": 0,
                    "total_thinking": 0,
                    "grand_total": 0,
                    "cache_hit_rate": 0.0,
                }
            ]

    # Resolve target channel
    selected_ch = "zero-ops"
    if target:
        if target in channel_sessions_raw:
            selected_ch = target
        elif target in conv_to_channel:
            selected_ch = conv_to_channel[target]
        else:
            clean_t = target.lstrip("#")
            for ch in channel_sessions_raw:
                if ch.lstrip("#") == clean_t:
                    selected_ch = ch
                    break

    if selected_ch not in channel_sessions_raw and channel_sessions_raw:
        selected_ch = list(channel_sessions_raw.keys())[0]

    # Build channel list for selector
    channels_list: List[Dict[str, Any]] = []
    for ch_name, s_list in channel_sessions_raw.items():
        is_p = ch_name in PERSISTENT_CHANNELS
        active_cid = ch_name_to_conv.get(ch_name)
        if not active_cid and s_list:
            active_cid = s_list[-1]["conv_id"]

        compacted_list = [s for s in s_list if s["conv_id"] != active_cid]
        active_entry = next((s for s in s_list if s["conv_id"] == active_cid), None)
        active_turns = active_entry["turn_count"] if active_entry else 0
        last_ts = max((s["last_active_timestamp"] for s in s_list), default=0)

        dt_l = datetime.fromtimestamp(last_ts, PT_TZ) if last_ts > 0 else None
        last_pt = dt_l.strftime("%b %d %I:%M %p PT") if dt_l else "Active"

        channels_list.append(
            {
                "name": ch_name,
                "display_name": format_channel_display_name(ch_name, is_p),
                "is_persistent": is_p,
                "lane_type": "persistent" if is_p else "dynamic",
                "lane_label": "24/7 Warm Daemon" if is_p else "Dynamic Ephemeral Worker",
                "active_conv_id": active_cid,
                "active_turns": active_turns,
                "compacted_count": len(compacted_list),
                "last_active_timestamp": last_ts,
                "last_active_pt": last_pt,
            }
        )

    persistent_channels = sorted([c for c in channels_list if c["is_persistent"]], key=lambda c: c["name"])
    dynamic_channels = sorted([c for c in channels_list if not c["is_persistent"]], key=lambda c: c["last_active_timestamp"], reverse=True)
    all_channels_sorted = persistent_channels + dynamic_channels

    # Determine Active and Predecessor sessions for selected channel
    s_list_for_ch = channel_sessions_raw.get(selected_ch, [])
    active_cid_for_ch = ch_name_to_conv.get(selected_ch)
    if not active_cid_for_ch and s_list_for_ch:
        active_cid_for_ch = s_list_for_ch[-1]["conv_id"]

    predecessors_raw = [s for s in s_list_for_ch if s["conv_id"] != active_cid_for_ch]
    # Bound to last 25 predecessors chronologically for full 30d lifecycle coverage
    if len(predecessors_raw) > 25:
        predecessors_raw = predecessors_raw[-25:]

    active_session_details = (
        get_session_details(active_cid_for_ch, model_config, include_timeline=True)
        if active_cid_for_ch
        else None
    )
    if active_session_details:
        active_session_details["is_active"] = True

    # Build stitched predecessor timeline & summary
    stitched_compacted_session = None
    compacted_summary: Dict[str, Any] = {
        "count": len(predecessors_raw),
        "turn_count": sum(s.get("turn_count", 0) for s in predecessors_raw),
        "step_count": 0,
        "total_input": sum(s.get("total_input", 0) for s in predecessors_raw),
        "total_output": sum(s.get("total_output", 0) for s in predecessors_raw),
        "total_cache": sum(s.get("total_cache", 0) for s in predecessors_raw),
        "total_tokens_recovered": 0,
        "cache_hit_rate": 0.0,
        "cache_leverage": 1.0,
        "span_pt": "--",
        "sessions": [
            {
                "conv_id": s["conv_id"],
                "turn_count": s["turn_count"],
                "last_active_pt": s["last_active_pt"],
            }
            for s in predecessors_raw
        ],
    }

    if predecessors_raw:
        stitched_compacted_session = stitch_session_timelines(
            predecessors_raw, model_config, label="stitched-predecessors"
        )
        first_dt = datetime.fromtimestamp(predecessors_raw[0]["start_timestamp"], PT_TZ).strftime("%b %d %I:%M %p")
        last_dt = datetime.fromtimestamp(predecessors_raw[-1]["last_active_timestamp"], PT_TZ).strftime("%b %d %I:%M %p PT")
        compacted_summary["step_count"] = stitched_compacted_session["step_count"]
        compacted_summary["total_tokens_recovered"] = stitched_compacted_session["total_tokens_recovered"]
        compacted_summary["cache_hit_rate"] = stitched_compacted_session["cache_hit_rate"]
        compacted_summary["cache_leverage"] = stitched_compacted_session["cache_leverage"]
        compacted_summary["span_pt"] = f"{first_dt} – {last_dt}"

    # Build full channel lifecycle timeline (predecessors + active combined)
    full_channel_session = None
    all_channel_records = list(predecessors_raw)
    if active_session_details:
        active_record = next((s for s in s_list_for_ch if s["conv_id"] == active_cid_for_ch), None)
        if active_record:
            all_channel_records.append(active_record)
        else:
            all_channel_records.append(active_session_details)

    if len(all_channel_records) > 1:
        full_channel_session = stitch_session_timelines(
            all_channel_records, model_config, label="full-channel-lifecycle"
        )
    elif active_session_details:
        full_channel_session = active_session_details

    is_ch_persistent = selected_ch in PERSISTENT_CHANNELS
    ch_info = {
        "name": selected_ch,
        "display_name": format_channel_display_name(selected_ch, is_ch_persistent),
        "is_persistent": is_ch_persistent,
        "lane_type": "persistent" if is_ch_persistent else "dynamic",
        "lane_label": "24/7 Warm Daemon" if is_ch_persistent else "Dynamic Ephemeral Worker",
        "active_conv_id": active_cid_for_ch,
        "compacted_count": len(predecessors_raw),
    }

    selected_session = active_session_details or stitched_compacted_session or {}

    live_quota = {}
    live_attr = {}
    try:
        from tools.quota_poller import get_latest_quota, get_quota_attribution
        live_quota = get_latest_quota()
        live_attr = get_quota_attribution(hours=168.0)
    except Exception as qe:
        logger.warning(f"Failed to fetch live quota for dashboard: {qe}")

    return {
        "status": "healthy",
        "generated_at_pt": datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT"),
        "model": {
            "name": model_config["name"],
            "limit": model_config["limit"],
            "tripwire": model_config["tripwire"],
            "tripwire_pct": round((model_config["tripwire"] / model_config["limit"]) * 100, 1),
        },
        "channel": ch_info,
        "channels": all_channels_sorted,
        "active_session": active_session_details,
        "compacted_summary": compacted_summary,
        "stitched_compacted_session": stitched_compacted_session,
        "full_channel_session": full_channel_session,
        # Backward compatibility
        "selected_compacted_session": stitched_compacted_session,
        "compacted_sessions": [
            {
                "conv_id": s["conv_id"],
                "turn_count": s["turn_count"],
                "last_active_pt": s["last_active_pt"],
                "last_active_timestamp": s["last_active_timestamp"],
                "total_input": s["total_input"],
                "total_output": s["total_output"],
                "total_cache": s["total_cache"],
                "total_thinking": s["total_thinking"],
                "grand_total": s["grand_total"],
                "cache_hit_rate": s["cache_hit_rate"],
                "is_active": False,
            }
            for s in predecessors_raw
        ],
        "selected_session": selected_session,
        "weekly_token_ceiling": WEEKLY_TOKEN_CEILING,
        "sessions": get_sessions_list(only_active=False),
        "channel_rollups": get_channel_rollups(),
        "daemon_rollups": get_daemon_rollups(),
        "quota": live_quota,
        "quota_attribution": live_attr,
    }


def render_dashboard_html(agent: str = "zero") -> str:
    """Generate self-contained, responsive 90/10 slate HTML dashboard."""
    is_ivy = (agent == "ivy")
    avatar_uri = get_avatar_data_uri(agent=agent)
    page_title = "Ivy // Sabermetric Context & Telemetry" if is_ivy else "Zero // Context Window & Token Telemetry"
    brand_name = "IVY" if is_ivy else "ZERO"
    brand_tag = "SABERMETRIC GM" if is_ivy else "TELEMETRY"
    brand_tag_class = "bg-emerald-500/10 text-emerald-400 border-emerald-500/20" if is_ivy else "bg-indigo-500/10 text-indigo-400 border-indigo-500/20"
    avatar_border_class = "border-emerald-500/40 shadow-emerald-500/10" if is_ivy else "border-indigo-500/40 shadow-indigo-500/10"
    header_meta = "DS1525+ (Host 2) · #baseball" if is_ivy else "DS1525+ (Host 2) · Port 8769"
    model_name = "Gemini 3.8 Flash (High)" if is_ivy else "Gemini 3.8 Flash"
    default_channel_name = "#baseball" if is_ivy else "#zero-ops"
    rollup_title = "7-Day Token Summary" if is_ivy else "Channel Token Distribution (7-Day)"
    rollup_subtitle = "Cumulative tokens and cache performance for #baseball." if is_ivy else "Cumulative tokens across channels."
    if is_ivy:
        channel_picker_html = '''      <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Channel:</span>
      <span class="px-3 py-1.5 rounded-lg bg-slate-900 border border-emerald-500/30 text-emerald-400 font-mono text-xs font-semibold flex items-center gap-1.5"><span class="h-2 w-2 rounded-full bg-emerald-400"></span>#baseball</span>'''
        daemons_card_html = ""
    else:
        channel_picker_html = '''      <label for="channel-select" class="text-xs font-semibold text-slate-400 uppercase tracking-wider whitespace-nowrap">Channel:</label>
      <div class="relative w-full sm:w-80">
        <select id="channel-select" onchange="onChannelChange(this.value)" class="w-full bg-slate-900 border border-slate-700/80 hover:border-slate-600 text-slate-100 text-xs rounded-lg px-3 py-2 pr-8 font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500/50 focus:border-indigo-500 transition-colors shadow-sm cursor-pointer appearance-none">
          <option value="">Loading channels...</option>
        </select>
        <div class="pointer-events-none absolute inset-y-0 right-0 flex items-center px-2 text-slate-400">
          <svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" /></svg>
        </div>
      </div>'''
        daemons_card_html = '''        <!-- Daemon & Background Tasks Card -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-sm space-y-4">
          <div class="border-b border-slate-800/80 pb-3">
            <h3 class="text-sm font-bold text-white tracking-tight">Daemon & Background Compute</h3>
            <p class="text-xs text-slate-400 mt-0.5">Autonomous internal workloads (compactors, classifiers, mail triage).</p>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead class="text-slate-400 border-b border-slate-800 text-[11px]">
                <tr>
                  <th class="pb-2">Worker / Task</th>
                  <th class="pb-2">Runs</th>
                  <th class="pb-2 text-right">Total Tokens</th>
                  <th class="pb-2 text-right">% Weekly</th>
                </tr>
              </thead>
              <tbody id="daemons-table-body" class="divide-y divide-slate-800/50">
                <tr><td colspan="3" class="py-4 text-center text-slate-500">Loading tasks...</td></tr>
              </tbody>
            </table>
          </div>
        </div>'''
    alt_dash_link = (
        '<a href="https://zero.brock.ventures/" class="hidden sm:flex items-center gap-1.5 text-xs text-indigo-400 bg-indigo-500/10 hover:bg-indigo-500/20 border border-indigo-500/30 px-2.5 py-1.5 rounded-lg transition-colors font-mono cursor-pointer"><span class="h-1.5 w-1.5 rounded-full bg-indigo-400"></span>⚡ Zero Dash</a>'
        if is_ivy
        else '<a href="https://ivy.brock.ventures/" class="hidden sm:flex items-center gap-1.5 text-xs text-emerald-400 bg-emerald-500/10 hover:bg-emerald-500/20 border border-emerald-500/30 px-2.5 py-1.5 rounded-lg transition-colors font-mono cursor-pointer"><span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span>⚾ Ivy Dash</a>'
    )
    body_selection = "selection:bg-emerald-500/30 selection:text-emerald-200" if is_ivy else "selection:bg-indigo-500/30 selection:text-indigo-200"

    html = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{PAGE_TITLE}}</title>
  <link rel="icon" href="{{AVATAR_URI}}">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            slate: {
              850: '#111827',
              900: '#0f172a',
              950: '#020617',
            }
          },
          fontFamily: {
            mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace']
          }
        }
      }
    }
  </script>
  <style>
    .tabular-nums { font-variant-numeric: tabular-nums; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #020617; }
    ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #475569; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 font-sans min-h-screen antialiased {{BODY_SELECTION}}">

  <!-- Top Navigation & Header -->
  <header class="border-b border-slate-800 bg-slate-950/80 backdrop-blur sticky top-0 z-50 px-4 lg:px-8 py-3.5">
    <div class="max-w-7xl mx-auto flex flex-col md:flex-row md:items-center justify-between gap-4">
      
      <!-- Brand & Agent Status -->
      <div class="flex items-center gap-3.5">
        <img src="{{AVATAR_URI}}" alt="{{BRAND_NAME}}" class="h-9 w-9 rounded-lg border {{AVATAR_BORDER_CLASS}} object-cover shadow-sm bg-slate-900">
        <div>
          <div class="flex items-center gap-2">
            <h1 class="font-bold text-base tracking-tight text-white">{{BRAND_NAME}}</h1>
            <span class="text-xs px-2 py-0.5 rounded-full font-mono font-medium {{BRAND_TAG_CLASS}}">{{BRAND_TAG}}</span>
            <span id="gateway-badge" class="flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
              <span class="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
              ONLINE
            </span>
          </div>
          <p class="text-xs text-slate-400 font-mono" id="header-meta">{{HEADER_META}}</p>
        </div>
      </div>

      <!-- Center Controls: Discord Channel Selector -->
      <div class="flex items-center gap-2.5 flex-1 max-w-md">
        {{CHANNEL_PICKER_HTML}}
      </div>

      <!-- Right Header Actions -->
      <div class="flex items-center gap-3 text-xs">
        {{ALT_DASH_LINK}}
        <div class="hidden sm:flex items-center gap-1.5 text-slate-400 bg-slate-900 px-2.5 py-1.5 rounded-lg border border-slate-800">
          <span class="text-slate-500">Model:</span>
          <span id="header-model" class="font-medium text-slate-200">{{MODEL_NAME}}</span>
        </div>
        <button onclick="fetchData()" id="refresh-btn" class="flex items-center gap-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 px-3 py-1.5 rounded-lg border border-slate-700 transition font-medium shadow-sm cursor-pointer">
          <span id="refresh-icon">↻</span> Refresh
        </button>
      </div>

    </div>
  </header>



  <!-- Main Container -->
  <main class="max-w-7xl mx-auto px-4 lg:px-8 py-6 space-y-6">

    <!-- Channel Jurisdiction & Sessions Banner -->
    <section class="bg-slate-900/90 border border-slate-800 rounded-xl p-4 sm:p-5 shadow-sm flex flex-col md:flex-row md:items-center justify-between gap-4">
      <div class="flex items-center gap-3 flex-wrap">
        <div class="text-xl sm:text-2xl font-bold font-mono text-white tracking-tight" id="banner-channel-name">{{DEFAULT_CHANNEL_NAME}}</div>
        <span id="banner-lane-badge" class="px-2.5 py-1 rounded text-xs font-mono font-semibold bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">⚙️ Dynamic Ephemeral Worker</span>
        <span class="text-xs text-slate-400 font-mono" id="banner-summary">Organized by Channel · Side-by-Side Lifecycle</span>
      </div>
      
      <!-- Session Switcher / Indicator Pills -->
      <div class="flex items-center gap-2 flex-wrap text-xs font-mono" id="banner-session-pills">
        <!-- Populated via JS -->
      </div>
    </section>

    <!-- Live Account Quota Telemetry (Zero-Token Ground-Truth) -->
    <section class="bg-slate-900 border border-slate-800 rounded-xl p-4 sm:p-5 shadow-sm space-y-3" id="live-quota-section">
      <div class="flex items-center justify-between border-b border-slate-800/80 pb-3 flex-wrap gap-2">
        <div class="flex items-center gap-2">
          <span class="text-sm font-semibold text-slate-100 flex items-center gap-1.5 font-mono">
            <span class="h-2 w-2 rounded-full bg-cyan-400"></span> Ground-Truth Account Quota Telemetry
          </span>
          <span class="text-[11px] px-2 py-0.5 rounded bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 font-mono font-medium">Headless · Zero Token Cost</span>
        </div>
        <div class="text-xs text-slate-400 font-mono" id="quota-last-sampled">Live from agy /usage</div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <!-- Gemini Weekly -->
        <div class="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3 space-y-1.5">
          <div class="flex items-center justify-between text-xs font-mono">
            <span class="text-slate-400 font-medium">Gemini Weekly</span>
            <span class="font-bold text-sky-400" id="quota-gw-rem">--%</span>
          </div>
          <div class="relative w-full h-2 bg-slate-900 rounded-full overflow-hidden border border-slate-800">
            <div id="quota-gw-bar" class="h-full bg-sky-500 rounded-full transition-all duration-300" style="width: 0%"></div>
          </div>
          <div class="flex justify-between text-[11px] text-slate-500 font-mono pt-0.5">
            <span id="quota-gw-used">--% used</span>
            <span id="quota-gw-reset" class="text-slate-400">Resets: --</span>
          </div>
        </div>
        <!-- Gemini 5-Hour -->
        <div class="bg-slate-950/60 border border-slate-800/80 rounded-lg p-3 space-y-1.5">
          <div class="flex items-center justify-between text-xs font-mono">
            <span class="text-slate-400 font-medium">Gemini 5-Hour</span>
            <span class="font-bold text-indigo-400" id="quota-g5-rem">--%</span>
          </div>
          <div class="relative w-full h-2 bg-slate-900 rounded-full overflow-hidden border border-slate-800">
            <div id="quota-g5-bar" class="h-full bg-indigo-500 rounded-full transition-all duration-300" style="width: 0%"></div>
          </div>
          <div class="flex justify-between text-[11px] text-slate-500 font-mono pt-0.5">
            <span id="quota-g5-label">5h Rolling Window</span>
            <span id="quota-g5-reset" class="text-slate-400">Resets: --</span>
          </div>
        </div>
      </div>
    </section>

    <!-- Side-by-Side Session Comparison Grid -->
    <section class="grid grid-cols-1 lg:grid-cols-2 gap-5" id="side-by-side-comparison">
      
      <!-- Left Column: 🟢 Current Active Session -->
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-sm space-y-4 relative overflow-hidden" id="active-session-card">
        <div class="flex items-center justify-between border-b border-slate-800/80 pb-3">
          <div class="flex items-center gap-2">
            <span class="h-2.5 w-2.5 rounded-full bg-emerald-400 animate-pulse"></span>
            <h3 class="text-sm font-bold text-white tracking-tight">Active Session Lifecycle</h3>
            <span class="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">LIVE</span>
          </div>
          <span id="active-session-id" class="text-xs font-mono text-slate-400 truncate max-w-[180px]">ID: --</span>
        </div>

        <!-- Metrics Grid -->
        <div class="grid grid-cols-2 gap-4">
          <!-- Active Context Fill -->
          <div class="space-y-1.5">
            <span class="text-[11px] font-medium text-slate-400">Context Window Fill</span>
            <div class="flex items-baseline gap-1.5">
              <span id="active-tokens" class="text-2xl font-bold font-mono text-white tabular-nums">0</span>
              <span id="active-limit" class="text-xs font-mono text-slate-400">/ 1.0M</span>
            </div>
            <div class="relative w-full h-2 bg-slate-950 rounded-full overflow-hidden border border-slate-800">
              <div id="active-progress-bar" class="h-full bg-indigo-500 rounded-full transition-all duration-300" style="width: 0%"></div>
              <div class="absolute top-0 bottom-0 w-0.5 bg-amber-400 z-10" style="left: 80%" title="Tripwire (800k)"></div>
            </div>
            <p id="active-headroom" class="text-[11px] text-slate-400 font-mono">1,000,000 tokens remaining</p>
          </div>

          <!-- Active Prompt Cache -->
          <div class="space-y-1.5">
            <span class="text-[11px] font-medium text-slate-400">Prompt Cache Hit Rate</span>
            <div class="flex items-baseline gap-1.5">
              <span id="active-cache-rate" class="text-2xl font-bold font-mono text-emerald-400 tabular-nums">0.0%</span>
              <span id="active-cache-leverage" class="text-xs font-mono text-slate-400">1.0x</span>
            </div>
            <div class="text-[11px] text-slate-400 font-mono pt-1">
              Cached: <strong id="active-cached-tok" class="text-slate-200">0</strong>
            </div>
            <div class="text-[11px] text-slate-400 font-mono">
              Fresh Input: <strong id="active-fresh-tok" class="text-slate-200">0</strong>
            </div>
          </div>
        </div>

        <!-- Activity Footprint & Compactions -->
        <div class="pt-3 border-t border-slate-800/80 flex items-center justify-between text-xs font-mono">
          <div class="space-x-3 text-slate-400">
            <span>Steps: <strong id="active-steps" class="text-slate-200">0</strong></span>
            <span>Turns: <strong id="active-turns" class="text-slate-200">0</strong></span>
            <span id="active-checkpoint-status" class="text-indigo-400 font-medium">Nominal Headroom</span>
          </div>
          <button onclick="focusTrajectory('active')" id="btn-focus-active" class="px-2.5 py-1 rounded bg-indigo-600/30 hover:bg-indigo-600/50 text-indigo-300 border border-indigo-500/40 text-[11px] transition-colors cursor-pointer font-medium">
            Active Trajectory ↓
          </button>
        </div>
      </div>

      <!-- Right Column: 🗜 Stitched Compacted Predecessors -->
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-5 shadow-sm space-y-4 relative overflow-hidden" id="compacted-session-card">
        <div class="flex items-center justify-between border-b border-slate-800/80 pb-3">
          <div class="flex items-center gap-2">
            <span class="h-2.5 w-2.5 rounded-full bg-amber-400"></span>
            <h3 class="text-sm font-bold text-white tracking-tight">Compacted History (Unified)</h3>
            <span id="compacted-count-pill" class="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/20">STITCHED</span>
          </div>
          <span id="compacted-span-label" class="text-xs font-mono text-slate-400 truncate max-w-[200px]">--</span>
        </div>

        <!-- Compacted Metrics Grid (Unified Across All Predecessors) -->
        <div id="compacted-content" class="space-y-4">
          <div class="grid grid-cols-2 gap-4">
            <!-- Cumulative Execution Footprint -->
            <div class="space-y-1.5">
              <span class="text-[11px] font-medium text-slate-400">Predecessor Execution Depth</span>
              <div class="flex items-baseline gap-1.5">
                <span id="compacted-steps-val" class="text-2xl font-bold font-mono text-slate-200 tabular-nums">0</span>
                <span class="text-xs font-mono text-slate-400">steps across</span>
                <span id="compacted-turns-val" class="text-base font-bold font-mono text-slate-300">0</span>
                <span class="text-xs font-mono text-slate-400">turns</span>
              </div>
              <div class="relative w-full h-2 bg-slate-950 rounded-full overflow-hidden border border-slate-800">
                <div id="compacted-progress-bar" class="h-full bg-amber-500 rounded-full" style="width: 100%"></div>
              </div>
              <p id="compacted-reclaimed" class="text-[11px] text-amber-400 font-mono">Reclaimed 0 tokens</p>
            </div>

            <!-- Cumulative Lifetime Cache -->
            <div class="space-y-1.5">
              <span class="text-[11px] font-medium text-slate-400">Cumulative Prompt Cache</span>
              <div class="flex items-baseline gap-1.5">
                <span id="compacted-cache-rate" class="text-2xl font-bold font-mono text-emerald-400 tabular-nums">0.0%</span>
                <span id="compacted-cache-leverage" class="text-xs font-mono text-slate-400">1.0x</span>
              </div>
              <div class="text-[11px] text-slate-400 font-mono pt-1">
                Cached: <strong id="compacted-cached-tok" class="text-slate-200">0</strong>
              </div>
              <div class="text-[11px] text-slate-400 font-mono">
                Total Output: <strong id="compacted-output-tok" class="text-slate-200">0</strong>
              </div>
            </div>
          </div>

          <!-- Stitched Predecessor Sessions Pills & Action Button -->
          <div class="pt-3 border-t border-slate-800/80 flex items-center justify-between text-xs font-mono gap-2 flex-wrap">
            <div class="flex items-center gap-1.5 flex-wrap" id="compacted-subpills">
              <!-- Populated via JS -->
            </div>
            <button onclick="focusTrajectory('compacted')" id="btn-focus-compacted" class="px-2.5 py-1 rounded bg-amber-600/30 hover:bg-amber-600/50 text-amber-300 border border-amber-500/40 text-[11px] transition-colors cursor-pointer font-medium whitespace-nowrap">
              Inspect Stitched History ↓
            </button>
          </div>
        </div>

        <!-- Empty State Placeholder if channel has 0 compacted sessions -->
        <div id="compacted-empty-state" class="hidden py-6 text-center space-y-2">
          <p class="text-xs font-mono text-slate-400">Initial Session Lifecycle</p>
          <p class="text-[11px] text-slate-500 max-w-sm mx-auto">
            No compacted predecessor sessions for this channel yet. When context reaches tripwire (800k) and compacts, predecessor benchmarks will display here side-by-side.
          </p>
        </div>

      </div>

    </section>

    <!-- Context Growth Timeline (Hero Feature) -->
    <section class="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-sm space-y-4">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-800/80 pb-4">
        <div>
          <div class="flex items-center gap-2.5 flex-wrap">
            <h2 class="text-base font-bold text-white tracking-tight">Context Window Growth Timeline</h2>
            <!-- 3-Way Trajectory Mode Switcher -->
            <div class="inline-flex rounded-lg border border-slate-800 bg-slate-950 p-0.5 text-xs font-mono">
              <button id="tab-trajectory-full" onclick="focusTrajectory('full')" class="px-2.5 py-1 rounded-md bg-cyan-600 text-white font-medium transition-colors cursor-pointer">
                🌐 Full Channel Lifecycle
              </button>
              <button id="tab-trajectory-active" onclick="focusTrajectory('active')" class="px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition-colors cursor-pointer">
                🟢 Active Session
              </button>
              <button id="tab-trajectory-compacted" onclick="focusTrajectory('compacted')" class="px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition-colors cursor-pointer">
                🗜 Compacted History (Stitched)
              </button>
            </div>
            <span id="chart-compaction-badge" class="hidden text-xs font-mono text-rose-400 bg-rose-500/10 border border-rose-500/20 px-2 py-0.5 rounded font-semibold"></span>
          </div>
          <p class="text-xs text-slate-400 mt-1">Chronological token accumulation, compaction drops, session rollovers, and active tripwire boundary.</p>
        </div>
        <div class="flex items-center gap-3 text-xs font-mono flex-wrap">
          <!-- Time Range Selector Buttons: 24h (default), 7d, 30d, All -->
          <div class="inline-flex rounded-lg border border-slate-800 bg-slate-950 p-0.5 text-xs font-mono" id="time-range-controls">
            <button id="range-btn-24h" onclick="setTimeRange('24h')" class="px-2.5 py-1 rounded-md bg-indigo-600 text-white font-medium transition-colors cursor-pointer">
              24h
            </button>
            <button id="range-btn-7d" onclick="setTimeRange('7d')" class="px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition-colors cursor-pointer">
              7d
            </button>
            <button id="range-btn-30d" onclick="setTimeRange('30d')" class="px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition-colors cursor-pointer">
              30d
            </button>
            <button id="range-btn-all" onclick="setTimeRange('all')" class="px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition-colors cursor-pointer">
              All
            </button>
          </div>
          <div class="flex items-center gap-1.5 text-slate-400">
            <span class="h-2 w-2 rounded-full bg-indigo-500"></span> Context Growth
          </div>
          <div class="flex items-center gap-1.5 text-rose-400">
            <span class="h-2 w-2 rounded-full bg-rose-500"></span> Compaction / Rollover
          </div>
          <div class="flex items-center gap-1.5 text-amber-400" id="tripwire-legend">
            <span class="h-0.5 w-3 bg-amber-400"></span> Tripwire
          </div>
          <button id="toggle-scale-btn" onclick="toggleChartScale()" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-indigo-400 border border-slate-700 rounded text-xs font-mono transition-colors cursor-pointer">
            Scale: Dynamic Zoom
          </button>
        </div>
      </div>

      <!-- SVG Chart Container -->
      <div class="w-full h-64 relative pt-2" id="chart-container">
        <svg id="timeline-svg" class="w-full h-full overflow-visible" preserveAspectRatio="none">
          <!-- Dynamically populated by JS -->
        </svg>
      </div>

      <!-- Tool breakdown tags -->
      <div class="pt-3 border-t border-slate-800/80 flex flex-wrap items-center gap-2 text-xs" id="tool-tags-container">
        <span class="text-slate-400 font-mono">Tools Invoked:</span>
      </div>
    </section>

    <!-- Step-by-Step Trajectory Stream & Turn Table -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

      <!-- Left 2 Cols: Step-by-Step Trajectory Stream -->
      <section class="lg:col-span-2 bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-sm space-y-4">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-800/80 pb-4">
          <div>
            <div class="flex items-center gap-2">
              <h3 class="text-sm font-bold text-white tracking-tight">Step Trajectory Stream</h3>
              <span id="stream-session-tag" class="text-xs font-mono text-slate-400 bg-slate-950 px-2 py-0.5 rounded border border-slate-800">Session: --</span>
            </div>
            <p class="text-xs text-slate-400 mt-0.5">Chronological execution steps, tool invocations, and compactions.</p>
          </div>
          <!-- Filter Buttons -->
          <div class="flex items-center gap-1 text-xs font-mono bg-slate-950 p-1 rounded-lg border border-slate-800">
            <button onclick="filterSteps('all')" id="btn-filter-all" class="px-2.5 py-1 rounded bg-slate-800 text-white font-medium">All</button>
            <button onclick="filterSteps('tool')" id="btn-filter-tool" class="px-2.5 py-1 rounded text-slate-400 hover:text-slate-200">Tools</button>
            <button onclick="filterSteps('reasoning')" id="btn-filter-reasoning" class="px-2.5 py-1 rounded text-slate-400 hover:text-slate-200">Thinking</button>
            <button onclick="filterSteps('user')" id="btn-filter-user" class="px-2.5 py-1 rounded text-slate-400 hover:text-slate-200">User</button>
          </div>
        </div>

        <!-- Steps List Container (max height with scrollbar) -->
        <div id="steps-container" class="space-y-2.5 max-h-[520px] overflow-y-auto pr-1">
          <div class="py-8 text-center text-slate-500 font-mono text-xs">Loading execution stream...</div>
        </div>
      </section>

      <!-- Right 1 Col: Turn History & Channel Rollups -->
      <section class="space-y-6">
        
        <!-- Turn Metrics Table -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-sm space-y-4">
          <div class="border-b border-slate-800/80 pb-3 flex items-center justify-between">
            <div>
              <h3 class="text-sm font-bold text-white tracking-tight">Turn Token Breakdown</h3>
              <p class="text-xs text-slate-400 mt-0.5">Prompt cache efficiency per turn.</p>
            </div>
            <span id="turn-count-badge" class="text-xs font-mono bg-slate-950 text-indigo-400 px-2 py-0.5 rounded border border-slate-800">0 turns</span>
          </div>
          <div class="overflow-x-auto max-h-56 overflow-y-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead class="text-slate-400 border-b border-slate-800 text-[11px] sticky top-0 bg-slate-900">
                <tr>
                  <th class="pb-2">Turn</th>
                  <th class="pb-2">Input</th>
                  <th class="pb-2">Cache</th>
                  <th class="pb-2 text-right">Hit %</th>
                </tr>
              </thead>
              <tbody id="turns-table-body" class="divide-y divide-slate-800/50">
                <tr><td colspan="4" class="py-4 text-center text-slate-500">Loading turns...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- 7-Day Channel Token Rollup -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-sm space-y-4">
          <div class="border-b border-slate-800/80 pb-3 flex items-center justify-between">
            <div>
              <h3 class="text-sm font-bold text-white tracking-tight">{{ROLLUP_TITLE}}</h3>
              <p class="text-xs text-slate-400 mt-0.5">{{ROLLUP_SUBTITLE}}</p>
            </div>
            <span id="channel-rollup-total-badge" class="text-xs font-mono bg-slate-950 text-indigo-400 px-2 py-0.5 rounded border border-slate-800 font-semibold">-- tokens</span>
          </div>
          <div class="overflow-x-auto max-h-48 overflow-y-auto">
            <table class="w-full text-left text-xs font-mono">
              <thead class="text-slate-400 border-b border-slate-800 text-[11px] sticky top-0 bg-slate-900">
                <tr>
                  <th class="pb-2">Channel</th>
                  <th class="pb-2">Turns</th>
                  <th class="pb-2 text-right">Total Tokens</th>
                  <th class="pb-2 text-right">% Weekly</th>
                  <th class="pb-2 text-right">Hit Rate</th>
                </tr>
              </thead>
              <tbody id="channels-table-body" class="divide-y divide-slate-800/50">
                <tr><td colspan="5" class="py-4 text-center text-slate-500">Loading channels...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

{{DAEMONS_CARD_HTML}}

      </section>

    </div>

  </main>

  <script>
    let currentPayload = null;
    let selectedChannel = null;
    let focusedTrajectoryMode = 'full'; // 'full' is default view, or 'active', 'compacted'
    let currentTimeRange = '24h'; // '24h' is default view, or '7d', '30d', 'all'
    let autoScale = true;
    let activeFilter = 'all';

    function formatTokens(num) {
      if (!num || num === 0) return '0';
      if (num >= 1000000000) return (num / 1000000000).toFixed(2) + 'B';
      if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
      if (num >= 1000) return (num / 1000).toFixed(1) + 'k';
      return num.toLocaleString();
    }

    function setTimeRange(range) {
      currentTimeRange = range;
      ['24h', '7d', '30d', 'all'].forEach(r => {
        const btn = document.getElementById(`range-btn-${r}`);
        if (btn) {
          if (r === range) {
            btn.className = 'px-2.5 py-1 rounded-md bg-indigo-600 text-white font-medium transition-colors cursor-pointer';
          } else {
            btn.className = 'px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition-colors cursor-pointer';
          }
        }
      });
      renderFocusedTrajectory();
    }

    const CURRENT_AGENT = "{{CURRENT_AGENT}}";

    async function fetchData(customChannel = null) {
      const icon = document.getElementById('refresh-icon');
      if (icon) icon.classList.add('animate-spin');
      
      try {
        const ch = customChannel || selectedChannel || '';
        let url = '/api/telemetry';
        const params = [];
        if (CURRENT_AGENT === 'ivy') {
          params.push('agent=ivy');
        }
        if (ch) {
          params.push(`channel=${encodeURIComponent(ch)}`);
          params.push(`session=${encodeURIComponent(ch)}`);
        }
        if (params.length > 0) url += '?' + params.join('&');

        const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        
        currentPayload = await res.json();
        renderDashboard(currentPayload);
      } catch (err) {
        console.error('Fetch error:', err);
      } finally {
        if (icon) icon.classList.remove('animate-spin');
      }
    }


    function onChannelChange(chName) {
      if (!chName || chName === selectedChannel) return;
      selectedChannel = chName;
      focusedTrajectoryMode = 'full';
      try {
        const u = new URL(window.location);
        u.searchParams.set('channel', chName);
        window.history.replaceState({}, '', u);
      } catch (e) {}
      fetchData(chName);
    }

    function focusTrajectory(mode) {
      focusedTrajectoryMode = mode;
      updateTrajectoryTabs();
      renderFocusedTrajectory();
    }

    function toggleChartScale() {
      autoScale = !autoScale;
      const btn = document.getElementById('toggle-scale-btn');
      if (btn) btn.textContent = autoScale ? 'Scale: Dynamic Zoom' : 'Scale: Full 1.0M Window';
      renderFocusedTrajectory();
    }

    function updateTrajectoryTabs() {
      const tabActive = document.getElementById('tab-trajectory-active');
      const tabCompacted = document.getElementById('tab-trajectory-compacted');
      const tabFull = document.getElementById('tab-trajectory-full');
      
      const resetClass = 'px-2.5 py-1 rounded-md text-slate-400 hover:text-slate-200 transition-colors cursor-pointer';
      if (tabActive) tabActive.className = resetClass;
      if (tabCompacted) tabCompacted.className = resetClass;
      if (tabFull) tabFull.className = resetClass;

      if (focusedTrajectoryMode === 'full' && tabFull) {
        tabFull.className = 'px-2.5 py-1 rounded-md bg-cyan-600 text-white font-medium transition-colors cursor-pointer';
      } else if (focusedTrajectoryMode === 'active' && tabActive) {
        tabActive.className = 'px-2.5 py-1 rounded-md bg-indigo-600 text-white font-medium transition-colors cursor-pointer';
      } else if (focusedTrajectoryMode === 'compacted' && tabCompacted) {
        tabCompacted.className = 'px-2.5 py-1 rounded-md bg-amber-600 text-white font-medium transition-colors cursor-pointer';
      }
    }

    function renderFocusedTrajectory() {
      if (!currentPayload) return;
      const m = currentPayload.model;
      const activeSess = currentPayload.active_session;
      const compSess = currentPayload.stitched_compacted_session || currentPayload.selected_compacted_session;
      const fullSess = currentPayload.full_channel_session;

      let targetSess = activeSess;
      let tagLabel = 'Active Session';
      if (focusedTrajectoryMode === 'compacted' && compSess) {
        targetSess = compSess;
        tagLabel = `Compacted History (${currentPayload.compacted_summary ? currentPayload.compacted_summary.count : 0} Stitched)`;
      } else if (focusedTrajectoryMode === 'full' && fullSess) {
        targetSess = fullSess;
        tagLabel = 'Full Channel Lifecycle (Unified)';
      }

      if (!targetSess) return;

      document.getElementById('stream-session-tag').textContent = `View: ${tagLabel}`;
      
      const chartCompBadge = document.getElementById('chart-compaction-badge');
      if (chartCompBadge) {
        if (targetSess.compaction_count > 0) {
          chartCompBadge.classList.remove('hidden');
          const recK = Math.round((targetSess.total_tokens_recovered || 0) / 1000);
          chartCompBadge.textContent = `🗜 ${targetSess.compaction_count} Compactions/Rollovers (${recK}k Reclaimed)`;
        } else {
          chartCompBadge.classList.add('hidden');
        }
      }

      renderTimelineChart(targetSess.chart_points || [], m.limit, m.tripwire, targetSess.compactions || [], currentTimeRange);

      const toolTags = document.getElementById('tool-tags-container');
      toolTags.innerHTML = '<span class="text-slate-400 font-mono">Tools Invoked:</span>';
      if (targetSess.tool_stats && Object.keys(targetSess.tool_stats).length > 0) {
        for (const [toolName, count] of Object.entries(targetSess.tool_stats)) {
          const pill = document.createElement('span');
          pill.className = 'bg-slate-950 border border-slate-800 text-slate-300 px-2 py-0.5 rounded font-mono text-[11px]';
          pill.innerHTML = `<strong>${toolName}</strong>: ${count}`;
          toolTags.appendChild(pill);
        }
      } else {
        toolTags.innerHTML += '<span class="text-slate-500 font-mono">None yet</span>';
      }

      renderStepsList(targetSess.timeline || []);

      const turnsBody = document.getElementById('turns-table-body');
      turnsBody.innerHTML = '';
      const turns = targetSess.turns || [];
      document.getElementById('turn-count-badge').textContent = `${targetSess.turn_count || turns.length} turns`;
      if (turns.length > 0) {
        turns.forEach(t => {
          const tr = document.createElement('tr');
          tr.className = 'hover:bg-slate-850/50';
          tr.innerHTML = `
            <td class="py-2 font-medium text-slate-300">#${t.turn_num}</td>
            <td class="py-2 text-slate-300">${t.input_tokens.toLocaleString()}</td>
            <td class="py-2 text-indigo-400">${t.cache_read_tokens.toLocaleString()}</td>
            <td class="py-2 text-right font-semibold text-emerald-400">${t.cache_hit_rate}%</td>
          `;
          turnsBody.appendChild(tr);
        });
      } else {
        turnsBody.innerHTML = '<tr><td colspan="4" class="py-4 text-center text-slate-500 font-mono">Continuous Multi-Session Stream</td></tr>';
      }
    }

    function renderDashboard(data) {
      if (!data) return;
      const ch = data.channel || {};
      const m = data.model || {};
      const activeS = data.active_session || {};
      const compSummary = data.compacted_summary || {};
      const stitchedComp = data.stitched_compacted_session;
      const fullSess = data.full_channel_session;

      selectedChannel = ch.name;

      document.getElementById('header-model').textContent = m.name || 'Gemini 3.8 Flash';

      // 0. Render Live Account Quota Cards
      const q = data.quota || {};
      if (q['gemini-weekly']) {
        const gw = q['gemini-weekly'];
        const remElem = document.getElementById('quota-gw-rem');
        if (remElem) remElem.textContent = `${gw.remaining_pct}%`;
        const barElem = document.getElementById('quota-gw-bar');
        if (barElem) barElem.style.width = `${gw.remaining_pct}%`;
        const usedElem = document.getElementById('quota-gw-used');
        if (usedElem) usedElem.textContent = `${gw.used_pct}% used`;
        const rstElem = document.getElementById('quota-gw-reset');
        if (rstElem) rstElem.textContent = `Resets: ${gw.countdown} (${gw.reset_pt})`;
      }
      if (q['gemini-5h']) {
        const g5 = q['gemini-5h'];
        const remElem = document.getElementById('quota-g5-rem');
        if (remElem) remElem.textContent = `${g5.remaining_pct}%`;
        const barElem = document.getElementById('quota-g5-bar');
        if (barElem) barElem.style.width = `${g5.remaining_pct}%`;
        const rstElem = document.getElementById('quota-g5-reset');
        if (rstElem) rstElem.textContent = `Resets: in ${g5.countdown}`;
      }


      // 1. Channel Selector
      const select = document.getElementById('channel-select');
      if (select) {
        select.innerHTML = '';
      
      const persistentGroup = document.createElement('optgroup');
      persistentGroup.label = '⚡ Persistent Daemon Channels';
      const dynamicGroup = document.createElement('optgroup');
      dynamicGroup.label = '⚙️ Dynamic Active Channels';

      (data.channels || []).forEach(chan => {
        const opt = document.createElement('option');
        opt.value = chan.name;
        const compStr = chan.compacted_count > 0 ? ` · ${chan.compacted_count} compacted` : '';
        const turnsStr = chan.active_turns === 1 ? '1 turn' : `${chan.active_turns} turns`;
        opt.textContent = `${chan.display_name} (${turnsStr}${compStr})`;
        if (chan.name === ch.name) opt.selected = true;

        if (chan.is_persistent) {
          persistentGroup.appendChild(opt);
        } else {
          dynamicGroup.appendChild(opt);
        }
      });

      if (persistentGroup.children.length > 0) select.appendChild(persistentGroup);
      if (dynamicGroup.children.length > 0) select.appendChild(dynamicGroup);
      }

      // 2. Channel Jurisdiction Banner
      document.getElementById('banner-channel-name').textContent = ch.display_name || `#${ch.name}`;
      const laneBadge = document.getElementById('banner-lane-badge');
      if (laneBadge) {
        if (ch.is_persistent) {
          laneBadge.className = 'px-2.5 py-1 rounded text-xs font-mono font-semibold bg-indigo-500/10 text-indigo-400 border border-indigo-500/30';
          laneBadge.textContent = '⚡ 24/7 Warm Daemon';
        } else {
          laneBadge.className = 'px-2.5 py-1 rounded text-xs font-mono font-semibold bg-cyan-500/10 text-cyan-400 border border-cyan-500/30';
          laneBadge.textContent = '⚙️ Dynamic Ephemeral Worker';
        }
      }
      document.getElementById('banner-summary').textContent = `${compSummary.count || 0} Compacted Predecessors (Stitched) · Side-by-Side View`;

      // Session pills in banner
      const bannerPills = document.getElementById('banner-session-pills');
      bannerPills.innerHTML = '';
      if (activeS && activeS.conv_id) {
        const p1 = document.createElement('span');
        p1.className = 'px-2.5 py-1 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
        p1.textContent = `🟢 Active: ${activeS.conv_id.slice(0, 8)} (${activeS.turn_count || 0} turns)`;
        bannerPills.appendChild(p1);
      }
      if (compSummary.count > 0) {
        const p2 = document.createElement('span');
        p2.className = 'px-2.5 py-1 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20';
        p2.textContent = `🗜 ${compSummary.count} Predecessors Stitched (${compSummary.turn_count} turns)`;
        bannerPills.appendChild(p2);
      }

      // 3. Left Card: 🟢 Current Active Session
      if (activeS && activeS.conv_id) {
        document.getElementById('active-session-id').textContent = `ID: ${activeS.conv_id.slice(0, 8)}...`;
        const curTok = activeS.current_tokens || 0;
        const limitTok = m.limit || 1000000;
        const fillPct = activeS.fill_pct || 0;
        document.getElementById('active-tokens').textContent = curTok.toLocaleString();
        document.getElementById('active-limit').textContent = `/ ${(limitTok / 1000).toFixed(0)}k (${fillPct}%)`;
        document.getElementById('active-headroom').textContent = `${(activeS.headroom_tokens || 0).toLocaleString()} tokens remaining`;
        
        const bar = document.getElementById('active-progress-bar');
        bar.style.width = `${Math.min(100, fillPct)}%`;
        bar.className = activeS.is_above_tripwire ? 'h-full bg-amber-500 rounded-full transition-all duration-300' : 'h-full bg-indigo-500 rounded-full transition-all duration-300';

        document.getElementById('active-cache-rate').textContent = `${activeS.cache_hit_rate}%`;
        document.getElementById('active-cache-leverage').textContent = `${activeS.cache_leverage}x`;
        document.getElementById('active-cached-tok').textContent = (activeS.total_cache || 0).toLocaleString();
        document.getElementById('active-fresh-tok').textContent = (activeS.total_input || 0).toLocaleString();

        document.getElementById('active-steps').textContent = activeS.step_count || 0;
        document.getElementById('active-turns').textContent = activeS.turn_count || 0;

        const ckStatus = document.getElementById('active-checkpoint-status');
        if (activeS.compaction_count > 0) {
          const recK = Math.round((activeS.total_tokens_recovered || 0) / 1000);
          ckStatus.textContent = `🗜 ${activeS.compaction_count} Compactions (${recK}k recovered)`;
          ckStatus.className = 'text-rose-400 font-semibold';
        } else {
          ckStatus.textContent = 'Nominal Headroom';
          ckStatus.className = 'text-indigo-400 font-medium';
        }
      }

      // 4. Right Card: 🗜 Stitched Compacted Predecessors (Unified)
      const compContent = document.getElementById('compacted-content');
      const compEmpty = document.getElementById('compacted-empty-state');
      const tabCompacted = document.getElementById('tab-trajectory-compacted');
      const tabFull = document.getElementById('tab-trajectory-full');

      if (compSummary.count > 0 && stitchedComp) {
        compContent.classList.remove('hidden');
        compEmpty.classList.add('hidden');
        if (tabCompacted) tabCompacted.classList.remove('hidden');
        if (tabFull) tabFull.classList.remove('hidden');

        document.getElementById('compacted-count-pill').textContent = `${compSummary.count} PREDECESSORS STITCHED`;
        document.getElementById('compacted-span-label').textContent = compSummary.span_pt;

        document.getElementById('compacted-steps-val').textContent = compSummary.step_count.toLocaleString();
        document.getElementById('compacted-turns-val').textContent = compSummary.turn_count;

        const recTok = compSummary.total_tokens_recovered || 0;
        document.getElementById('compacted-reclaimed').textContent = `Reclaimed ${recTok.toLocaleString()} tokens across rollovers & compactions`;

        document.getElementById('compacted-cache-rate').textContent = `${compSummary.cache_hit_rate}%`;
        document.getElementById('compacted-cache-leverage').textContent = `${compSummary.cache_leverage}x`;
        document.getElementById('compacted-cached-tok').textContent = (compSummary.total_cache || 0).toLocaleString();
        document.getElementById('compacted-output-tok').textContent = (compSummary.total_output || 0).toLocaleString();

        // Render mini session badges
        const subpills = document.getElementById('compacted-subpills');
        subpills.innerHTML = '<span class="text-slate-500 font-mono text-[11px]">Sessions:</span>';
        (compSummary.sessions || []).forEach(sess => {
          const sp = document.createElement('span');
          sp.className = 'px-1.5 py-0.5 rounded bg-slate-950 text-slate-300 border border-slate-800 text-[10px] font-mono';
          sp.textContent = `${sess.conv_id.slice(0, 8)} (${sess.turn_count}t)`;
          subpills.appendChild(sp);
        });

      } else {
        compContent.classList.add('hidden');
        compEmpty.classList.remove('hidden');
        if (tabCompacted) tabCompacted.classList.add('hidden');
        if (tabFull) tabFull.classList.remove('hidden');
      }

      // 5. Render Timeline and Steps Stream for currently focused mode
      updateTrajectoryTabs();
      renderFocusedTrajectory();

      // 6. Render 7-Day Channel Rollup Table & Total Badge
      const chanBody = document.getElementById('channels-table-body');
      chanBody.innerHTML = '';
      let sum7dTokens = 0;
      (data.channel_rollups || []).forEach(chItem => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-850/50';
        const tokVal = chItem.grand_total || 0;
        sum7dTokens += tokVal;
        const totalTokStr = formatTokens(tokVal);
        const fullTokStr = tokVal.toLocaleString();
        const weeklyCeil = data.weekly_token_ceiling || 1025801595;
        const pctWeekly = chItem.pct_weekly !== undefined ? chItem.pct_weekly : (((tokVal) / weeklyCeil) * 100).toFixed(2);
        const gtTitle = chItem.pct_weekly_ground_truth ? ' title="Ground-truth attributed quota burn across cycle"' : '';
        const gtBadge = chItem.pct_weekly_ground_truth ? ' <span class="text-[10px] text-cyan-400 font-mono" title="Attributed from live agy quota deltas">🎯</span>' : '';
        tr.innerHTML = `
          <td class="py-2 text-slate-300 font-medium">${chItem.display_name}</td>
          <td class="py-2 text-slate-400">${chItem.turn_count || chItem.total_turns || 0}</td>
          <td class="py-2 text-right font-semibold text-slate-200" title="${fullTokStr} tokens">${totalTokStr}</td>
          <td class="py-2 text-right font-semibold text-sky-400"${gtTitle}>${pctWeekly}%${gtBadge}</td>
          <td class="py-2 text-right font-semibold text-emerald-400">${chItem.cache_hit_rate}%</td>
        `;
        chanBody.appendChild(tr);
      });
      const rollupBadge = document.getElementById('channel-rollup-total-badge');
      if (rollupBadge) {
        const weeklyCeil = data.weekly_token_ceiling || 1025801595;
        const sumPct = ((sum7dTokens / weeklyCeil) * 100).toFixed(1);
        rollupBadge.textContent = `${formatTokens(sum7dTokens)} Total (${sumPct}% Weekly)`;
      }

      // 7. Render Daemon Rollup Table
      const daemonBody = document.getElementById('daemons-table-body');
      if (daemonBody) {
        daemonBody.innerHTML = '';
      (data.daemon_rollups || []).forEach(d => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-slate-850/50';
        const weeklyCeil = data.weekly_token_ceiling || 1025801595;
        const dPct = d.pct_weekly !== undefined ? d.pct_weekly : ((((d.grand_total || 0)) / weeklyCeil) * 100).toFixed(2);
        tr.innerHTML = `
          <td class="py-2 text-slate-300 font-medium">${d.display_name}</td>
          <td class="py-2 text-slate-400">${d.turn_count}</td>
          <td class="py-2 text-right font-semibold text-slate-200">${(d.grand_total || 0).toLocaleString()}</td>
          <td class="py-2 text-right font-semibold text-sky-400">${dPct}%</td>
        `;
        daemonBody.appendChild(tr);
      });
      }
    }

    function renderTimelineChart(rawPoints, limit, tripwire, compactions, timeRange = '24h') {
      const svg = document.getElementById('timeline-svg');
      if (!svg) return;
      svg.innerHTML = '';

      const rect = svg.getBoundingClientRect();
      const W = rect.width || 800;
      const H = rect.height || 256;
      const padLeft = 65;
      const padRight = 25;
      const padTop = 20;
      const padBottom = 42;
      const plotW = W - padLeft - padRight;
      const plotH = H - padTop - padBottom;

      // 1. Determine Window Boundaries in Epoch Seconds
      const nowSec = Date.now() / 1000;
      const latestRawTs = (rawPoints && rawPoints.length > 0) ? Math.max(...rawPoints.map(p => p.timestamp || 0)) : nowSec;
      const effectiveNow = Math.max(nowSec, latestRawTs);

      let windowDuration = 24 * 3600;
      if (timeRange === '7d') windowDuration = 7 * 86400;
      else if (timeRange === '30d') windowDuration = 30 * 86400;
      else if (timeRange === 'all') windowDuration = null;

      let windowStart, windowEnd;
      if (windowDuration !== null) {
        windowEnd = effectiveNow;
        windowStart = effectiveNow - windowDuration;
      } else {
        const minTs = (rawPoints && rawPoints.length > 0) ? Math.min(...rawPoints.map(p => p.timestamp || effectiveNow)) : effectiveNow - 86400;
        windowStart = minTs;
        windowEnd = effectiveNow;
        if (windowEnd - windowStart < 3600) windowStart = windowEnd - 3600;
      }

      // 2. Filter Points and inject continuity anchors
      const pointsInWindow = (rawPoints || []).filter(p => p.timestamp && p.timestamp >= windowStart && p.timestamp <= windowEnd);
      let baselinePoint = null;
      if (rawPoints && rawPoints.length > 0) {
        for (let i = rawPoints.length - 1; i >= 0; i--) {
          if (rawPoints[i].timestamp && rawPoints[i].timestamp < windowStart) {
            baselinePoint = rawPoints[i];
            break;
          }
        }
      }

      let displayPoints = [];
      if (pointsInWindow.length > 0) {
        if (baselinePoint) {
          displayPoints.push({
            timestamp: windowStart,
            tokens: baselinePoint.tokens,
            step: baselinePoint.step,
            label: baselinePoint.label,
            time: 'Window Start',
            is_anchor: true,
            is_compaction: false,
          });
        }
        displayPoints = displayPoints.concat(pointsInWindow);
        const lastInside = pointsInWindow[pointsInWindow.length - 1];
        if (lastInside.timestamp < windowEnd - 60) {
          displayPoints.push({
            timestamp: windowEnd,
            tokens: lastInside.tokens,
            step: lastInside.step,
            label: 'Now',
            time: 'Now',
            is_anchor: true,
            is_compaction: false,
          });
        }
      } else if (baselinePoint) {
        displayPoints.push({
          timestamp: windowStart,
          tokens: baselinePoint.tokens,
          step: baselinePoint.step,
          label: 'Baseline',
          time: 'Window Start',
          is_anchor: true,
          is_compaction: false,
        });
        displayPoints.push({
          timestamp: windowEnd,
          tokens: baselinePoint.tokens,
          step: baselinePoint.step,
          label: 'Now',
          time: 'Now',
          is_anchor: true,
          is_compaction: false,
        });
      }

      // 3. Y-Axis Scale
      const peakTokens = Math.max(...displayPoints.map(p => Math.max(p.tokens, p.tokens_before || 0)), 10000);
      let maxTokens = limit;
      if (autoScale) {
        const headroomCeil = Math.ceil(peakTokens * 1.25 / 10000) * 10000;
        maxTokens = Math.max(headroomCeil, 20000);
        if (peakTokens > tripwire * 0.9) maxTokens = limit;
      }

      // 4. SVG Definitions (Gradients)
      const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
      defs.innerHTML = `
        <linearGradient id="indigo-grad" x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" stop-color="#6366f1" stop-opacity="0.3"/>
          <stop offset="100%" stop-color="#6366f1" stop-opacity="0.0"/>
        </linearGradient>
      `;
      svg.appendChild(defs);

      // 5. Y-Axis Gridlines & Labels
      const yTicks = 4;
      for (let i = 0; i <= yTicks; i++) {
        const val = Math.round((maxTokens / yTicks) * i);
        const y = padTop + (1 - (val / maxTokens)) * plotH;
        
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', padLeft);
        line.setAttribute('y1', y);
        line.setAttribute('x2', W - padRight);
        line.setAttribute('y2', y);
        line.setAttribute('stroke', '#1e293b');
        line.setAttribute('stroke-dasharray', '3 3');
        svg.appendChild(line);

        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', padLeft - 10);
        text.setAttribute('y', y + 3.5);
        text.setAttribute('fill', '#64748b');
        text.setAttribute('font-size', '10');
        text.setAttribute('font-family', 'ui-monospace, SFMono-Regular, monospace');
        text.setAttribute('text-anchor', 'end');
        text.textContent = val >= 1000 ? `${(val / 1000).toFixed(val % 1000 === 0 ? 0 : 1)}k` : val;
        svg.appendChild(text);
      }

      // Y-axis line
      const yAxisLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      yAxisLine.setAttribute('x1', padLeft);
      yAxisLine.setAttribute('y1', padTop);
      yAxisLine.setAttribute('x2', padLeft);
      yAxisLine.setAttribute('y2', H - padBottom);
      yAxisLine.setAttribute('stroke', '#334155');
      svg.appendChild(yAxisLine);

      // Tripwire line if in visible range
      const tripwireY = padTop + (1 - (tripwire / maxTokens)) * plotH;
      const tripwireLegend = document.getElementById('tripwire-legend');
      if (tripwireY >= padTop && tripwireY <= H - padBottom) {
        if (tripwireLegend) tripwireLegend.style.opacity = '1';
        const twLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        twLine.setAttribute('x1', padLeft);
        twLine.setAttribute('y1', tripwireY);
        twLine.setAttribute('x2', W - padRight);
        twLine.setAttribute('y2', tripwireY);
        twLine.setAttribute('stroke', '#fbbf24');
        twLine.setAttribute('stroke-width', '1.2');
        twLine.setAttribute('stroke-dasharray', '4 4');
        svg.appendChild(twLine);

        const twText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        twText.setAttribute('x', W - padRight - 6);
        twText.setAttribute('y', tripwireY - 4);
        twText.setAttribute('fill', '#fbbf24');
        twText.setAttribute('font-size', '9');
        twText.setAttribute('font-family', 'monospace');
        twText.setAttribute('text-anchor', 'end');
        twText.textContent = `TRIPWIRE (${Math.round(tripwire/1000)}k)`;
        svg.appendChild(twText);
      } else {
        if (tripwireLegend) tripwireLegend.style.opacity = autoScale ? '0.4' : '1';
      }

      // X-Axis line
      const xAxisLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      xAxisLine.setAttribute('x1', padLeft);
      xAxisLine.setAttribute('y1', H - padBottom);
      xAxisLine.setAttribute('x2', W - padRight);
      xAxisLine.setAttribute('y2', H - padBottom);
      xAxisLine.setAttribute('stroke', '#334155');
      xAxisLine.setAttribute('stroke-width', '1.5');
      svg.appendChild(xAxisLine);

      // 6. X-Axis Ticks & Two-Line Pacific Time Labels
      function getX(ts) {
        const span = windowEnd - windowStart || 1;
        const clamped = Math.max(windowStart, Math.min(windowEnd, ts));
        return padLeft + ((clamped - windowStart) / span) * plotW;
      }

      let numXTicks = 5;
      if (timeRange === '7d') numXTicks = 7;
      else if (timeRange === '30d') numXTicks = 6;

      for (let i = 0; i < numXTicks; i++) {
        const fraction = i / (numXTicks - 1 || 1);
        const tickTs = windowStart + fraction * (windowEnd - windowStart);
        const tickX = padLeft + fraction * plotW;

        // Subtle vertical grid line
        const gridLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        gridLine.setAttribute('x1', tickX);
        gridLine.setAttribute('y1', padTop);
        gridLine.setAttribute('x2', tickX);
        gridLine.setAttribute('y2', H - padBottom);
        gridLine.setAttribute('stroke', '#1e293b');
        gridLine.setAttribute('stroke-dasharray', '2 2');
        svg.appendChild(gridLine);

        // Tick mark
        const tick = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        tick.setAttribute('x1', tickX);
        tick.setAttribute('y1', H - padBottom);
        tick.setAttribute('x2', tickX);
        tick.setAttribute('y2', H - padBottom + 5);
        tick.setAttribute('stroke', '#475569');
        svg.appendChild(tick);

        // Date/Time formatting in Pacific Time
        const d = new Date(tickTs * 1000);
        const timeStr = d.toLocaleTimeString('en-US', { timeZone: 'America/Los_Angeles', hour: 'numeric', minute: '2-digit' });
        const dateStr = d.toLocaleDateString('en-US', { timeZone: 'America/Los_Angeles', month: 'short', day: 'numeric' });
        const isLast = (i === numXTicks - 1);

        let line1 = timeStr;
        let line2 = dateStr;
        if (timeRange === '24h') {
          line1 = isLast ? `${timeStr} (Now)` : timeStr;
          line2 = dateStr;
        } else if (timeRange === '7d') {
          line1 = isLast ? `${dateStr} (Today)` : dateStr;
          line2 = timeStr;
        } else if (timeRange === '30d') {
          line1 = dateStr;
          line2 = isLast ? 'Today' : '';
        } else {
          line1 = dateStr;
          line2 = timeStr;
        }

        const tEl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        tEl.setAttribute('x', tickX);
        tEl.setAttribute('y', H - padBottom + 16);
        tEl.setAttribute('font-family', 'ui-monospace, SFMono-Regular, monospace');
        tEl.setAttribute('text-anchor', i === 0 ? 'start' : (isLast ? 'end' : 'middle'));

        const tspan1 = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
        tspan1.setAttribute('x', tickX);
        tspan1.setAttribute('fill', isLast ? '#818cf8' : '#cbd5e1');
        tspan1.setAttribute('font-size', '10');
        tspan1.setAttribute('font-weight', isLast ? '600' : 'normal');
        tspan1.textContent = line1;
        tEl.appendChild(tspan1);

        if (line2) {
          const tspan2 = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
          tspan2.setAttribute('x', tickX);
          tspan2.setAttribute('dy', '12');
          tspan2.setAttribute('fill', '#64748b');
          tspan2.setAttribute('font-size', '9');
          tspan2.textContent = line2;
          tEl.appendChild(tspan2);
        }
        svg.appendChild(tEl);
      }

      // Empty State Overlay if 0 active points in window
      if (displayPoints.length === 0 || (pointsInWindow.length === 0 && !baselinePoint)) {
        const emptyText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        emptyText.setAttribute('x', padLeft + plotW / 2);
        emptyText.setAttribute('y', padTop + plotH / 2 - 8);
        emptyText.setAttribute('fill', '#64748b');
        emptyText.setAttribute('font-size', '12');
        emptyText.setAttribute('font-family', 'ui-monospace, SFMono-Regular, monospace');
        emptyText.setAttribute('text-anchor', 'middle');
        emptyText.textContent = `No channel activity in selected ${timeRange.toUpperCase()} window`;
        svg.appendChild(emptyText);

        const subText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        subText.setAttribute('x', padLeft + plotW / 2);
        subText.setAttribute('y', padTop + plotH / 2 + 12);
        subText.setAttribute('fill', '#475569');
        subText.setAttribute('font-size', '10');
        subText.setAttribute('font-family', 'ui-monospace, SFMono-Regular, monospace');
        subText.setAttribute('text-anchor', 'middle');
        subText.textContent = 'Extend range to 7d or 30d to view historical trajectory';
        svg.appendChild(subText);
        return;
      }

      // 7. Calculate Point Coordinates
      const coords = displayPoints.map((p) => {
        const x = getX(p.timestamp);
        const y = padTop + (1 - (p.tokens / maxTokens)) * plotH;
        return {
          x,
          y,
          tokens: p.tokens,
          step: p.step,
          label: p.label,
          time: p.time,
          timestamp: p.timestamp,
          is_anchor: p.is_anchor || false,
          is_compaction: p.is_compaction || false,
          is_rollover: p.is_rollover || false,
          tokens_before: p.tokens_before || 0,
          recovered: p.recovered || 0,
          session_id: p.session_id || '',
        };
      });

      // 8. Stair-Step Trajectory Path
      let pathD = '';
      coords.forEach((pt, i) => {
        if (i === 0) {
          pathD += `M ${pt.x} ${pt.y}`;
        } else {
          if (pt.is_compaction && pt.tokens_before) {
            const peakY = padTop + (1 - (pt.tokens_before / maxTokens)) * plotH;
            pathD += ` L ${pt.x} ${peakY} L ${pt.x} ${pt.y}`;
          } else {
            pathD += ` L ${pt.x} ${coords[i-1].y} L ${pt.x} ${pt.y}`;
          }
        }
      });

      const lastX = coords[coords.length - 1].x;
      const firstX = coords[0].x;
      const baseAreaY = H - padBottom;
      const areaD = `${pathD} L ${lastX} ${baseAreaY} L ${firstX} ${baseAreaY} Z`;

      const areaPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      areaPath.setAttribute('d', areaD);
      areaPath.setAttribute('fill', 'url(#indigo-grad)');
      svg.appendChild(areaPath);

      const strokePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      strokePath.setAttribute('d', pathD);
      strokePath.setAttribute('fill', 'none');
      strokePath.setAttribute('stroke', '#6366f1');
      strokePath.setAttribute('stroke-width', '2');
      svg.appendChild(strokePath);

      // 9. Render Inflection Points, Compaction drops, and Tooltips
      coords.forEach((pt) => {
        if (pt.is_anchor) return; // Skip rendering circles on synthetic window anchors

        if (pt.is_compaction) {
          const peakY = padTop + (1 - (pt.tokens_before / maxTokens)) * plotH;
          const dropLine = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          dropLine.setAttribute('x1', pt.x);
          dropLine.setAttribute('y1', peakY);
          dropLine.setAttribute('x2', pt.x);
          dropLine.setAttribute('y2', pt.y);
          dropLine.setAttribute('stroke', pt.is_rollover ? '#f59e0b' : '#f43f5e');
          dropLine.setAttribute('stroke-width', '2');
          dropLine.setAttribute('stroke-dasharray', '3 3');
          svg.appendChild(dropLine);

          const cPre = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
          cPre.setAttribute('cx', pt.x);
          cPre.setAttribute('cy', peakY);
          cPre.setAttribute('r', '4.5');
          cPre.setAttribute('fill', pt.is_rollover ? '#f59e0b' : '#f43f5e');
          cPre.setAttribute('stroke', '#ffffff');
          cPre.setAttribute('stroke-width', '1.5');
          const preTitle = document.createElementNS('http://www.w3.org/2000/svg', 'title');
          preTitle.textContent = `${pt.is_rollover ? 'Session Rollover' : 'Compaction Checkpoint'} Before: ${pt.tokens_before.toLocaleString()} tok (${pt.time || ''})`;
          cPre.appendChild(preTitle);
          svg.appendChild(cPre);

          const cPost = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
          cPost.setAttribute('cx', pt.x);
          cPost.setAttribute('cy', pt.y);
          cPost.setAttribute('r', '4');
          cPost.setAttribute('fill', '#10b981');
          cPost.setAttribute('stroke', '#020617');
          cPost.setAttribute('stroke-width', '1.5');
          const postTitle = document.createElementNS('http://www.w3.org/2000/svg', 'title');
          postTitle.textContent = `Reclaimed ${pt.recovered.toLocaleString()} tokens → Reset to ${pt.tokens.toLocaleString()} tok`;
          cPost.appendChild(postTitle);
          svg.appendChild(cPost);

          const recText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          recText.setAttribute('x', Math.min(W - padRight - 5, pt.x + 8));
          recText.setAttribute('y', (peakY + pt.y) / 2 + 4);
          recText.setAttribute('fill', pt.is_rollover ? '#f59e0b' : '#f43f5e');
          recText.setAttribute('font-size', '9');
          recText.setAttribute('font-weight', 'bold');
          recText.setAttribute('font-family', 'monospace');
          const dropLabel = pt.is_rollover ? `-${Math.round((pt.recovered || 0) / 1000)}k (Rollover)` : `-${Math.round((pt.recovered || 0) / 1000)}k (Reset)`;
          recText.textContent = dropLabel;
          svg.appendChild(recText);
        } else {
          const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
          circle.setAttribute('cx', pt.x);
          circle.setAttribute('cy', pt.y);
          circle.setAttribute('r', '2.5');
          circle.setAttribute('fill', '#818cf8');
          circle.setAttribute('stroke', '#020617');
          circle.setAttribute('stroke-width', '1');
          const tip = document.createElementNS('http://www.w3.org/2000/svg', 'title');
          tip.textContent = `${pt.label} · Step #${pt.step} · ${pt.tokens.toLocaleString()} tokens · ${pt.time || ''}`;
          circle.appendChild(tip);
          svg.appendChild(circle);
        }
      });
    }

    function renderStepsList(steps) {
      const container = document.getElementById('steps-container');
      if (!container) return;
      container.innerHTML = '';

      const filtered = steps.filter(s => {
        if (activeFilter === 'all') return true;
        if (activeFilter === 'tool') return s.category === 'tool';
        if (activeFilter === 'reasoning') return s.category === 'reasoning';
        if (activeFilter === 'user') return s.category === 'user';
        return true;
      });

      if (filtered.length === 0) {
        container.innerHTML = '<div class="py-8 text-center text-slate-500 font-mono text-xs">No matching steps</div>';
        return;
      }

      filtered.forEach(st => {
        const item = document.createElement('div');
        item.className = 'p-3 rounded-lg border border-slate-800 bg-slate-950/60 hover:bg-slate-950 transition-colors flex items-start justify-between gap-3 text-xs';
        
        let icon = '⚡';
        let badgeColor = 'bg-slate-800 text-slate-300';
        if (st.is_rollover) {
          icon = '🔄';
          badgeColor = 'bg-amber-500/10 text-amber-400 border border-amber-500/20';
        } else if (st.is_compaction) {
          icon = '🗜';
          badgeColor = 'bg-rose-500/10 text-rose-400 border border-rose-500/20';
        } else if (st.category === 'user') {
          icon = '👤';
          badgeColor = 'bg-indigo-500/10 text-indigo-400 border border-indigo-500/20';
        } else if (st.category === 'tool') {
          icon = '🔧';
          badgeColor = 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20';
        } else if (st.category === 'reasoning') {
          icon = '🧠';
          badgeColor = 'bg-purple-500/10 text-purple-400 border border-purple-500/20';
        }

        const sessBadge = st.session_id ? `<span class="px-1.5 py-0.5 rounded font-mono text-[9px] bg-slate-900 text-slate-400 border border-slate-800">${st.session_id.slice(0, 8)}</span>` : '';

        item.innerHTML = `
          <div class="flex items-start gap-2.5 flex-1 min-w-0">
            <span class="text-sm mt-0.5">${icon}</span>
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-bold text-white font-mono text-[11px]">#${st.step}</span>
                ${sessBadge}
                <span class="px-1.5 py-0.5 rounded font-mono text-[10px] font-medium ${badgeColor}">${st.title}</span>
                <span class="text-[11px] text-slate-500 font-mono">${st.time}</span>
              </div>
              <p class="text-slate-300 mt-1 font-mono text-xs truncate" title="${st.detail || ''}">${st.detail || '--'}</p>
            </div>
          </div>
          <div class="text-right whitespace-nowrap pl-2">
            <span class="text-[10px] font-mono text-slate-400">~${(st.tokens_est || 0).toLocaleString()} tok</span>
          </div>
        `;
        container.appendChild(item);
      });
    }

    function filterSteps(cat) {
      activeFilter = cat;
      ['all', 'tool', 'reasoning', 'user'].forEach(c => {
        const btn = document.getElementById(`btn-filter-${c}`);
        if (btn) {
          if (c === cat) {
            btn.className = 'px-2.5 py-1 rounded bg-slate-800 text-white font-medium';
          } else {
            btn.className = 'px-2.5 py-1 rounded text-slate-400 hover:text-slate-200';
          }
        }
      });
      renderFocusedTrajectory();
    }

    setInterval(() => {
      fetchData();
    }, 5000);

    try {
      const urlParams = new URLSearchParams(window.location.search);
      const initCh = urlParams.get('channel') || urlParams.get('target');
      if (initCh) selectedChannel = initCh;
    } catch (e) {}

    fetchData(selectedChannel);
  </script>
</body>
</html>
"""
    return (
        html.replace("{{AVATAR_URI}}", avatar_uri)
        .replace("{{PAGE_TITLE}}", page_title)
        .replace("{{BRAND_NAME}}", brand_name)
        .replace("{{BRAND_TAG}}", brand_tag)
        .replace("{{BRAND_TAG_CLASS}}", brand_tag_class)
        .replace("{{AVATAR_BORDER_CLASS}}", avatar_border_class)
        .replace("{{HEADER_META}}", header_meta)
        .replace("{{MODEL_NAME}}", model_name)
        .replace("{{DEFAULT_CHANNEL_NAME}}", default_channel_name)
        .replace("{{CHANNEL_PICKER_HTML}}", channel_picker_html)
        .replace("{{DAEMONS_CARD_HTML}}", daemons_card_html)
        .replace("{{ROLLUP_TITLE}}", rollup_title)
        .replace("{{ROLLUP_SUBTITLE}}", rollup_subtitle)
        .replace("{{ALT_DASH_LINK}}", alt_dash_link)
        .replace("{{BODY_SELECTION}}", body_selection)
        .replace("{{CURRENT_AGENT}}", agent)
    )
