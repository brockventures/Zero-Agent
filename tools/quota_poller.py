#!/usr/bin/env python3
"""Zero (Antigravity) Quota Usage Poller & Attribution Engine.

Polls ground-truth quota fractions via `agy -p "/usage" --output-format=json`
with zero LLM token overhead. Persists snapshots to SQLite (/workspace/data/telemetry.db),
calculates cycle deltas (Δ remaining_fraction), and attributes per-channel quota burn
by joining snapshot windows against `token_usage` records.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

_WORKSPACE = Path(__file__).resolve().parent.parent
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

logger = logging.getLogger("quota_poller")

PT = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("/workspace/data/telemetry.db")

# In-memory throttle tracker for post-turn sampling
_LAST_SAMPLE_TS: float = 0.0


def get_db_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open or initialize SQLite connection with WAL mode and row factory."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None, db_path: Path | str = DB_PATH) -> None:
    """Ensure quota_snapshots table and indices exist."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                datetime_utc TEXT NOT NULL,
                bucket_id TEXT NOT NULL,
                window TEXT NOT NULL,
                remaining_fraction REAL NOT NULL,
                reset_time TEXT NOT NULL,
                prev_fraction REAL,
                delta_fraction REAL DEFAULT 0.0,
                is_reset INTEGER DEFAULT 0,
                source TEXT DEFAULT 'poller',
                extra_json TEXT
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_snapshots_bucket_ts ON quota_snapshots (bucket_id, timestamp);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_snapshots_ts ON quota_snapshots (timestamp);"
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()


def fetch_quota_buckets(timeout: int = 30) -> List[Dict[str, Any]]:
    """Fetch live quota buckets from agy CLI with zero token overhead."""
    from tools.quota_card import fetch_usage_data

    raw = fetch_usage_data(timeout=timeout)
    buckets: List[Dict[str, Any]] = []

    groups = raw.get("command", {}).get("data", {}).get("groups", [])
    for group in groups:
        group_name = str(group.get("name", ""))
        for b in group.get("buckets", []):
            b_id = b.get("id") or ("gemini-weekly" if "gemini" in group_name.lower() and b.get("window") == "weekly" else None)
            if not b_id:
                continue
            buckets.append({
                "bucket_id": b_id,
                "name": b.get("name") or b_id,
                "window": b.get("window") or "unknown",
                "remaining_fraction": float(b.get("remaining_fraction", 0.0)),
                "reset_time": str(b.get("reset_time", "")),
                "group_name": group_name,
            })

    if not buckets:
        # Fallback parser for tab-separated text response
        resp_text = raw.get("response", "")
        for line in resp_text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                model_col = parts[0].strip().lower()
                win_col = parts[1].strip().lower()
                pct_str = parts[2].strip().rstrip("%")
                reset_col = parts[3].strip()
                try:
                    frac = float(pct_str) / 100.0
                except ValueError:
                    continue
                if "gemini" in model_col:
                    b_id = "gemini-weekly" if "weekly" in win_col else "gemini-5h"
                else:
                    b_id = "3p-weekly" if "weekly" in win_col else "3p-5h"
                buckets.append({
                    "bucket_id": b_id,
                    "name": parts[0].strip(),
                    "window": "weekly" if "weekly" in win_col else "5h",
                    "remaining_fraction": frac,
                    "reset_time": reset_col,
                    "group_name": parts[0].strip(),
                })

    return buckets


def sample_quota(
    source: str = "poller",
    dry_run: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    db_path: Path | str = DB_PATH,
    custom_buckets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Capture a live quota snapshot, calculate deltas, and persist to SQLite."""
    global _LAST_SAMPLE_TS
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.timestamp()
    iso_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    should_close = False
    if conn is None and not dry_run:
        conn = get_db_connection(db_path)
        init_db(conn, db_path)
        should_close = True

    try:
        buckets = custom_buckets if custom_buckets is not None else fetch_quota_buckets()
        results: List[Dict[str, Any]] = []

        for b in buckets:
            b_id = b["bucket_id"]
            curr_frac = b["remaining_fraction"]
            curr_reset = b["reset_time"]
            window = b["window"]

            prev_frac: Optional[float] = None
            delta_frac = 0.0
            is_reset = 0

            if conn is not None:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT remaining_fraction, reset_time, timestamp
                    FROM quota_snapshots
                    WHERE bucket_id = ?
                    ORDER BY timestamp DESC
                    LIMIT 1;
                    """,
                    (b_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    prev_frac = float(row["remaining_fraction"])
                    prev_reset = str(row["reset_time"])
                    # Detect cycle reset: reset_time changed or quota increased significantly
                    if (prev_reset and curr_reset and prev_reset != curr_reset) or (curr_frac > prev_frac + 0.05):
                        is_reset = 1
                        delta_frac = 0.0
                    else:
                        # Compare against the cycle's monotonic low-water mark (minimum remaining_fraction
                        # seen in the current reset_time cycle) so small upward rolling-window ticks
                        # do not get re-counted as fresh burn when remaining_fraction steps back down.
                        cur.execute(
                            """
                            SELECT MIN(remaining_fraction) AS min_rem
                            FROM quota_snapshots
                            WHERE bucket_id = ? AND reset_time = ?;
                            """,
                            (b_id, curr_reset),
                        )
                        min_row = cur.fetchone()
                        low_water = (
                            float(min_row["min_rem"])
                            if (min_row is not None and min_row["min_rem"] is not None)
                            else prev_frac
                        )
                        delta_frac = max(0.0, low_water - curr_frac)

            snapshot_record = {
                "timestamp": ts,
                "datetime_utc": iso_utc,
                "bucket_id": b_id,
                "window": window,
                "remaining_fraction": curr_frac,
                "reset_time": curr_reset,
                "prev_fraction": prev_frac,
                "delta_fraction": delta_frac,
                "is_reset": is_reset,
                "source": source,
                "extra_json": json.dumps(b),
            }

            if conn is not None and not dry_run:
                conn.execute(
                    """
                    INSERT INTO quota_snapshots (
                        timestamp, datetime_utc, bucket_id, window,
                        remaining_fraction, reset_time, prev_fraction,
                        delta_fraction, is_reset, source, extra_json
                    ) VALUES (
                        :timestamp, :datetime_utc, :bucket_id, :window,
                        :remaining_fraction, :reset_time, :prev_fraction,
                        :delta_fraction, :is_reset, :source, :extra_json
                    );
                    """,
                    snapshot_record,
                )

            results.append(snapshot_record)

        if conn is not None and not dry_run:
            conn.commit()

        _LAST_SAMPLE_TS = ts

        return {
            "status": "success",
            "timestamp": ts,
            "datetime_utc": iso_utc,
            "source": source,
            "dry_run": dry_run,
            "snapshots": results,
        }
    finally:
        if should_close and conn is not None:
            conn.close()


def throttled_post_turn_sample(min_interval: float = 60.0) -> Optional[Dict[str, Any]]:
    """Safe post-turn hook: samples quota if at least min_interval seconds elapsed."""
    global _LAST_SAMPLE_TS
    now = time.time()
    if (now - _LAST_SAMPLE_TS) < min_interval:
        return None
    try:
        return sample_quota(source="post_turn")
    except Exception as e:
        logger.warning(f"Post-turn quota sampling failed: {e}")
        return None


def get_latest_quota(conn: Optional[sqlite3.Connection] = None, db_path: Path | str = DB_PATH) -> Dict[str, Any]:
    """Retrieve the most recent snapshot for each quota bucket."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        init_db(conn, db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT qs.*
            FROM quota_snapshots qs
            INNER JOIN (
                SELECT bucket_id, MAX(timestamp) as max_ts
                FROM quota_snapshots
                GROUP BY bucket_id
            ) latest ON qs.bucket_id = latest.bucket_id AND qs.timestamp = latest.max_ts;
            """
        )
        rows = cur.fetchall()
        buckets_out: Dict[str, Any] = {}

        now_utc = datetime.now(timezone.utc)
        for r in rows:
            d = dict(r)
            b_id = d["bucket_id"]
            rem_frac = float(d["remaining_fraction"])
            rem_pct = round(rem_frac * 100.0, 2)
            used_pct = round((1.0 - rem_frac) * 100.0, 2)

            reset_iso = d["reset_time"]
            countdown = "unknown"
            reset_pt = "unknown"

            if reset_iso:
                try:
                    clean_iso = reset_iso.replace("Z", "+00:00")
                    dt_utc = datetime.fromisoformat(clean_iso)
                    if dt_utc.tzinfo is None:
                        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                    rem_sec = max(0, int((dt_utc - now_utc).total_seconds()))
                    days = rem_sec // 86400
                    hours = (rem_sec % 86400) // 3600
                    mins = (rem_sec % 3600) // 60
                    if days > 0:
                        countdown = f"{days}d {hours}h"
                    elif hours > 0:
                        countdown = f"{hours}h {mins}m"
                    else:
                        countdown = f"{mins}m"
                    dt_pt = dt_utc.astimezone(PT)
                    h12 = dt_pt.strftime("%I").lstrip("0") or "12"
                    reset_pt = f"{dt_pt.strftime('%a')} {h12}:{dt_pt.strftime('%M %p')} PT"
                except Exception:
                    pass

            d["remaining_pct"] = rem_pct
            d["used_pct"] = used_pct
            d["countdown"] = countdown
            d["reset_pt"] = reset_pt
            buckets_out[b_id] = d

        return buckets_out
    finally:
        if should_close:
            conn.close()


def get_quota_attribution(
    hours: float = 24.0,
    bucket_id: str = "gemini-weekly",
    conn: Optional[sqlite3.Connection] = None,
    db_path: Path | str = DB_PATH,
) -> Dict[str, Any]:
    """Correlate snapshot deltas against token_usage records to attribute quota burn."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        init_db(conn, db_path)
        since_ts = time.time() - (hours * 3600.0)

        # 1. Fetch chronological snapshots for this bucket
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, timestamp, remaining_fraction, prev_fraction, delta_fraction, is_reset, source
            FROM quota_snapshots
            WHERE bucket_id = ? AND timestamp >= ?
            ORDER BY timestamp ASC;
            """,
            (bucket_id, since_ts),
        )
        snapshots = [dict(r) for r in cur.fetchall()]

        total_burn_fraction = 0.0
        channel_burn_fractions: Dict[str, float] = {}
        channel_tokens: Dict[str, Dict[str, int]] = {}
        external_burn_fraction = 0.0

        low_water_frac: Optional[float] = (
            float(snapshots[0]["remaining_fraction"]) if snapshots else None
        )
        low_water_ts: Optional[float] = (
            float(snapshots[0]["timestamp"]) if snapshots else None
        )

        for i in range(1, len(snapshots)):
            s_prev = snapshots[i - 1]
            s_curr = snapshots[i]

            curr_frac = float(s_curr.get("remaining_fraction") or 0.0)
            curr_ts = float(s_curr["timestamp"])

            if s_curr.get("is_reset", 0) or low_water_frac is None:
                low_water_frac = curr_frac
                low_water_ts = curr_ts
                continue

            # Monotonic low-water clamping: only count net new burn below the lowest
            # remaining_fraction already accounted for in the current cycle.
            if curr_frac >= low_water_frac:
                continue

            delta = low_water_frac - curr_frac
            t_start = low_water_ts if low_water_ts is not None else float(s_prev["timestamp"])
            t_end = curr_ts
            low_water_frac = curr_frac
            low_water_ts = curr_ts

            total_burn_fraction += delta

            # Query token_usage turns that occurred strictly within this interval
            cur.execute(
                """
                SELECT
                    coalesce(channel_name, 'unknown') as ch_name,
                    COUNT(*) as turn_count,
                    SUM(input_tokens) as in_tok,
                    SUM(cache_read_tokens) as cache_tok,
                    SUM(output_tokens) as out_tok,
                    SUM(thinking_tokens) as think_tok,
                    SUM(total_tokens) as tot_tok
                FROM token_usage
                WHERE timestamp > ? AND timestamp <= ?
                GROUP BY coalesce(channel_name, 'unknown');
                """,
                (t_start, t_end),
            )
            interval_turns = [dict(r) for r in cur.fetchall()]
            interval_total_tokens = sum((t["tot_tok"] or 0) for t in interval_turns)

            if interval_total_tokens > 0:
                for t in interval_turns:
                    ch = t["ch_name"]
                    ch_tokens = t["tot_tok"] or 0
                    share = ch_tokens / interval_total_tokens
                    ch_delta = delta * share

                    channel_burn_fractions[ch] = channel_burn_fractions.get(ch, 0.0) + ch_delta

                    if ch not in channel_tokens:
                        channel_tokens[ch] = {
                            "turns": 0,
                            "input_tokens": 0,
                            "cache_tokens": 0,
                            "output_tokens": 0,
                            "thinking_tokens": 0,
                            "total_tokens": 0,
                        }
                    channel_tokens[ch]["turns"] += t["turn_count"] or 0
                    channel_tokens[ch]["input_tokens"] += t["in_tok"] or 0
                    channel_tokens[ch]["cache_tokens"] += t["cache_tok"] or 0
                    channel_tokens[ch]["output_tokens"] += t["out_tok"] or 0
                    channel_tokens[ch]["thinking_tokens"] += t["think_tok"] or 0
                    channel_tokens[ch]["total_tokens"] += ch_tokens
            else:
                # Quota decreased with zero Zero turns logged: consumed by Ivy or external sessions
                external_burn_fraction += delta

        # Assemble summary breakdown
        total_pct = round(total_burn_fraction * 100.0, 3)
        ext_pct = round(external_burn_fraction * 100.0, 3)

        breakdown = []
        for ch, burn_frac in sorted(channel_burn_fractions.items(), key=lambda x: x[1], reverse=True):
            pct = round(burn_frac * 100.0, 3)
            tok_stats = channel_tokens.get(ch, {})
            share_of_burn = round((burn_frac / total_burn_fraction * 100.0), 1) if total_burn_fraction > 0 else 0.0
            breakdown.append({
                "channel": ch,
                "quota_pct_burned": pct,
                "share_of_total_burn_pct": share_of_burn,
                **tok_stats,
            })

        if external_burn_fraction > 0:
            share_ext = round((external_burn_fraction / total_burn_fraction * 100.0), 1) if total_burn_fraction > 0 else 0.0
            breakdown.append({
                "channel": "ivy_or_external",
                "quota_pct_burned": ext_pct,
                "share_of_total_burn_pct": share_ext,
                "turns": 0,
                "input_tokens": 0,
                "cache_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "total_tokens": 0,
            })

        return {
            "bucket_id": bucket_id,
            "window_hours": hours,
            "snapshot_count": len(snapshots),
            "total_quota_pct_burned": total_pct,
            "external_quota_pct_burned": ext_pct,
            "zero_quota_pct_burned": round((total_pct - ext_pct), 3),
            "breakdown": breakdown,
        }
    finally:
        if should_close:
            conn.close()


def format_quota_status_text(latest: Dict[str, Any], include_3p: bool = False) -> str:
    """Format live quota status cleanly for Discord or CLI output."""
    if not latest:
        return "⚠️ No quota snapshots recorded yet."

    lines = ["📊 **Live Antigravity Quota Status**"]
    if "gemini-weekly" in latest:
        gw = latest["gemini-weekly"]
        lines.append(
            f"- **Gemini Weekly:** `{gw['remaining_pct']}%` remaining (used {gw['used_pct']}%) — resets in {gw['countdown']} ({gw['reset_pt']})"
        )
    if "gemini-5h" in latest:
        g5 = latest["gemini-5h"]
        lines.append(f"- **Gemini 5-Hour:** `{g5['remaining_pct']}%` remaining — resets in {g5['countdown']}")
    if include_3p:
        if "3p-weekly" in latest:
            tp = latest["3p-weekly"]
            lines.append(f"- **3P Weekly (Claude/GPT):** `{tp['remaining_pct']}%` remaining — resets in {tp['countdown']} ({tp['reset_pt']})")
        if "3p-5h" in latest:
            tp5 = latest["3p-5h"]
            lines.append(f"- **3P 5-Hour:** `{tp5['remaining_pct']}%` remaining — resets in {tp5['countdown']}")

    return "\n".join(lines)


def format_attribution_text(attr: Dict[str, Any]) -> str:
    """Format attribution breakdown cleanly without pipe tables."""
    hours = attr.get("window_hours", 24)
    tot = attr.get("total_quota_pct_burned", 0.0)
    lines = [
        f"🎯 **Quota Burn Attribution (Past {hours:.0f}h)**",
        f"- **Total Burned:** `{tot:.2f}%` of weekly cap",
        f"- **Zero Channels:** `{attr.get('zero_quota_pct_burned', 0.0):.2f}%`",
        f"- **Ivy / External:** `{attr.get('external_quota_pct_burned', 0.0):.2f}%`",
        "",
        "### Channel Breakdown",
    ]
    for b in attr.get("breakdown", []):
        ch = b["channel"]
        pct = b["quota_pct_burned"]
        share = b["share_of_total_burn_pct"]
        tok = b.get("total_tokens", 0)
        tok_str = f"{tok:,} tokens" if tok > 0 else "external"
        lines.append(f"- **{ch}:** `{pct:.2f}%` burned ({share}% of cycle delta, {tok_str})")

    return "\n".join(lines)


def run_quota_poller(test: bool = False, quiet: bool = False) -> Tuple[bool, str, Dict[str, Any]]:
    """Entry point for sidecars.py, bridge_scheduler.py, and manual CLI runs."""
    try:
        res = sample_quota(source="sidecar" if not test else "test", dry_run=test)
        snapshots = res.get("snapshots", [])

        # Check if any bucket had non-zero burn delta
        max_delta = max((s.get("delta_fraction", 0.0) for s in snapshots), default=0.0)
        has_reset = any(s.get("is_reset", 0) for s in snapshots)

        gw = next((s for s in snapshots if s["bucket_id"] == "gemini-weekly"), None)
        rem_pct = round(gw["remaining_fraction"] * 100, 1) if gw else 0.0

        if test:
            msg = f"🧪 [TEST] Quota poller dry-run nominal ({len(snapshots)} buckets sampled; Gemini weekly at {rem_pct}%)."
            return True, msg, res

        if has_reset:
            msg = f"🔄 **Quota Cycle Reset Detected:** Gemini weekly replenished to `{rem_pct}%`."
            return True, msg, res

        if max_delta > 0.005:  # >= 0.5% burned in this window
            msg = f"📉 **Quota Burn Update:** Δ `{max_delta * 100:.2f}%` burned across last window (Gemini weekly remaining: `{rem_pct}%`)."
            return True, msg, res

        if quiet:
            return True, "", res

        return True, f"Nominal: Quota sampled ({len(snapshots)} buckets; Gemini weekly: {rem_pct}%).", res
    except Exception as e:
        logger.error(f"Quota poller failed: {e}", exc_info=True)
        return False, f"⚠️ Quota poller error: {e}", {"error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Zero Quota Usage Poller & Attribution Engine.")
    parser.add_argument("--test", "-t", action="store_true", help="Dry run without writing to SQLite")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress nominal output (sidecar mode)")
    parser.add_argument("--sample", "-s", action="store_true", help="Capture a live quota snapshot now")
    parser.add_argument("--status", action="store_true", help="Show current live quota status across buckets")
    parser.add_argument("--attribution", "-a", action="store_true", help="Display per-channel quota attribution")
    parser.add_argument("--hours", type=float, default=24.0, help="Hours window for attribution (default: 24)")
    parser.add_argument("--include-3p", action="store_true", help="Include 3P models (Claude/GPT) in status output")
    parser.add_argument("--json", action="store_true", help="Output raw JSON data")
    args = parser.parse_args()

    if args.status:
        latest = get_latest_quota()
        if args.json:
            print(json.dumps(latest, indent=2))
        else:
            print(format_quota_status_text(latest, include_3p=args.include_3p))
        return 0

    if args.attribution:
        attr = get_quota_attribution(hours=args.hours)
        if args.json:
            print(json.dumps(attr, indent=2))
        else:
            print(format_attribution_text(attr))
        return 0

    if args.sample or args.test:
        ok, msg, data = run_quota_poller(test=args.test, quiet=args.quiet)
        if args.json:
            print(json.dumps({"ok": ok, "message": msg, "data": data}, indent=2))
        elif msg:
            print(msg)
        return 0 if ok else 1

    # Default action: run sample
    ok, msg, data = run_quota_poller(test=False, quiet=args.quiet)
    if args.json:
        print(json.dumps({"ok": ok, "message": msg, "data": data}, indent=2))
    elif msg:
        print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
