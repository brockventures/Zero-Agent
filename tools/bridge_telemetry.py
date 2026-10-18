"""Bridge Token Telemetry Engine.

Tracks, stores, and aggregates LLM token telemetry directly from agy wire streams.
Stores normalized turn-level usage in SQLite (/workspace/data/telemetry.db).
Separates dynamic threads from persistent root channels for clean rollups
and low-cardinality time-series exports.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bridge_telemetry")

DB_PATH = Path("/workspace/data/telemetry.db")

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


def get_db_connection() -> sqlite3.Connection:
    """Open or initialize SQLite connection with WAL mode."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_telemetry_db():
    """Ensure telemetry tables and indices exist."""
    with get_db_connection() as conn:
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
                total_tokens INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp ON token_usage (timestamp);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_parent ON token_usage (parent_channel_name);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_channel ON token_usage (channel_name);"
        )


def resolve_channel_info(
    channel_id: int | str,
    reply_target: Any = None,
    thread: Any = None,
    escalated_to_thread: bool = False,
) -> Dict[str, Any]:
    """Resolve normalized channel metadata, identifying threads vs persistent root channels."""
    cid = int(channel_id) if str(channel_id).isdigit() else 0
    fallback_name = KNOWN_CHANNELS.get(cid, f"channel-{channel_id}")

    # Case 1: Explicit escalated thread
    if escalated_to_thread and thread:
        t_id = getattr(thread, "id", None)
        t_name = getattr(thread, "name", "escalated-thread")
        parent = getattr(thread, "parent", None)
        p_id = getattr(thread, "parent_id", getattr(parent, "id", cid))
        p_name = getattr(parent, "name", KNOWN_CHANNELS.get(p_id, "zero-chat"))
        return {
            "channel_id": cid,
            "channel_name": t_name,
            "parent_channel_id": p_id,
            "parent_channel_name": p_name,
            "is_thread": 1,
            "thread_id": t_id,
        }

    # Case 2: Inspect reply_target (discord.Message, discord.TextChannel, discord.Thread)
    target_ch = getattr(reply_target, "channel", reply_target)
    if target_ch is not None and hasattr(target_ch, "parent"):
        # It's a Discord thread
        t_id = getattr(target_ch, "id", None)
        t_name = getattr(target_ch, "name", f"thread-{t_id}")
        parent = getattr(target_ch, "parent", None)
        p_id = getattr(target_ch, "parent_id", getattr(parent, "id", cid))
        p_name = getattr(parent, "name", KNOWN_CHANNELS.get(p_id, fallback_name))
        return {
            "channel_id": cid,
            "channel_name": t_name,
            "parent_channel_id": p_id,
            "parent_channel_name": p_name,
            "is_thread": 1,
            "thread_id": t_id,
        }

    # Case 3: Standard text channel
    ch_name = getattr(target_ch, "name", fallback_name)
    return {
        "channel_id": cid,
        "channel_name": ch_name,
        "parent_channel_id": cid,
        "parent_channel_name": ch_name,
        "is_thread": 0,
        "thread_id": None,
    }


def record_token_usage(
    channel_id: int | str,
    usage: Dict[str, Any],
    conv_id: Optional[str] = None,
    mode: str = "home",
    reply_target: Any = None,
    thread: Any = None,
    escalated_to_thread: bool = False,
    timestamp: Optional[float] = None,
) -> bool:
    """Record turn token usage into SQLite."""
    if not usage or not isinstance(usage, dict):
        return False

    ts = timestamp or time.time()
    ch_meta = resolve_channel_info(
        channel_id=channel_id,
        reply_target=reply_target,
        thread=thread,
        escalated_to_thread=escalated_to_thread,
    )

    in_tokens = int(usage.get("input_tokens") or 0)
    out_tokens = int(usage.get("output_tokens") or 0)
    cache_tokens = int(usage.get("cache_read_tokens") or 0)
    think_tokens = int(usage.get("thinking_tokens") or 0)
    tot_tokens = int(usage.get("total_tokens") or (in_tokens + out_tokens + cache_tokens + think_tokens))

    try:
        init_telemetry_db()
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO token_usage (
                    timestamp, channel_id, channel_name,
                    parent_channel_id, parent_channel_name,
                    is_thread, thread_id, conv_id, mode,
                    input_tokens, output_tokens, cache_read_tokens,
                    thinking_tokens, total_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    ts,
                    ch_meta["channel_id"],
                    ch_meta["channel_name"],
                    ch_meta["parent_channel_id"],
                    ch_meta["parent_channel_name"],
                    ch_meta["is_thread"],
                    ch_meta["thread_id"],
                    conv_id,
                    mode,
                    in_tokens,
                    out_tokens,
                    cache_tokens,
                    think_tokens,
                    tot_tokens,
                ),
            )
        logger.info(
            f"[BridgeTelemetry] Recorded {tot_tokens} tokens for #{ch_meta['parent_channel_name']} "
            f"(thread={bool(ch_meta['is_thread'])}, conv={conv_id})"
        )
        return True
    except Exception as e:
        logger.error(f"[BridgeTelemetry] Failed recording token usage: {e}", exc_info=True)
        return False


def get_token_summary(hours: float = 24.0) -> List[Dict[str, Any]]:
    """Retrieve token summary rolled up by parent channel over the given time window."""
    init_telemetry_db()
    since = time.time() - (hours * 3600.0)
    with get_db_connection() as conn:
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
            WHERE timestamp >= ?
            GROUP BY parent_channel_name
            ORDER BY grand_total DESC;
            """,
            (since,),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
