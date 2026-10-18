#!/usr/bin/env python3
"""Daily Token Budget Watchdog Sidecar.

Monitors combined fleet token consumption (Zero + Ivy) against the 458.4M daily allocation (4x average benchmark).
Emits a structured warning alert to #zero-ops via tools/outbox.py if the preceding
day's burn exceeded the 458,365,162 token budget ceiling.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Ensure /workspace is in pythonpath
WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from tools.ivy_telemetry import _run_host2_python
from tools.outbox import flush_pending_messages, queue_outbox_message

logger = logging.getLogger("token_budget_watchdog")

PT_TZ = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("/workspace/data/telemetry.db")

DAILY_BUDGET_CEILING = 62_857_143  # ~62.9M wire tokens / day (Argon-weighted weekly ceiling / 7)
WEEKLY_TOKEN_CEILING = 440_000_000  # 440M weekly wire ceiling (calibrated to Argon 55% @ 240M empirical burn)
DEFAULT_TARGET_CHANNEL = "zero-ops"
DEFAULT_TARGET_CHANNEL_ID = 1544953279664889888


def get_evaluation_window(
    target_day: Optional[str] = None,
) -> Tuple[float, float, str]:
    """Calculate start/end timestamps and label in Pacific Time."""
    now_pt = datetime.now(PT_TZ)

    if target_day == "today":
        start_dt = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = now_pt
        day_label = start_dt.strftime("%A, %B %-d")
    elif target_day == "yesterday":
        y_dt = now_pt - timedelta(days=1)
        start_dt = y_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = y_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        day_label = start_dt.strftime("%A, %B %-d")
    elif target_day:
        # Expected YYYY-MM-DD
        dt = datetime.strptime(target_day, "%Y-%m-%d").replace(tzinfo=PT_TZ)
        start_dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        day_label = start_dt.strftime("%A, %B %-d")
    else:
        # Default: if late evening (>= 20:00 PT), evaluate today concluding; otherwise yesterday
        if now_pt.hour >= 20:
            start_dt = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt = now_pt
            day_label = f"today ({start_dt.strftime('%b %-d')})"
        else:
            y_dt = now_pt - timedelta(days=1)
            start_dt = y_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt = y_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            day_label = f"yesterday ({start_dt.strftime('%b %-d')})"

    return start_dt.timestamp(), end_dt.timestamp(), day_label


def query_zero_tokens(
    start_ts: float, end_ts: float
) -> Tuple[int, int, List[Dict[str, Any]]]:
    """Query Zero token totals and per-channel breakdown from telemetry.db."""
    if not DB_PATH.exists():
        return 0, 0, []

    channels = []
    total_wire = 0
    total_turns = 0

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(
            """
            SELECT 
                COALESCE(parent_channel_name, channel_name, 'internal') as ch,
                COUNT(*) as turn_count,
                SUM(total_tokens) as wire_tokens,
                SUM(input_tokens) as in_tokens,
                SUM(output_tokens) as out_tokens,
                SUM(cache_read_tokens) as cache_tokens
            FROM token_usage
            WHERE timestamp >= ? AND timestamp <= ?
            GROUP BY ch
            ORDER BY wire_tokens DESC;
            """,
            (start_ts, end_ts),
        )

        for r in cur.fetchall():
            d = dict(r)
            wt = d["wire_tokens"] or 0
            tc = d["turn_count"] or 0
            total_wire += wt
            total_turns += tc
            channels.append({
                "channel": d["ch"],
                "wire_tokens": wt,
                "input_tokens": d["in_tokens"] or 0,
                "output_tokens": d["out_tokens"] or 0,
                "cache_tokens": d["cache_tokens"] or 0,
                "turns": tc,
            })
        conn.close()
    except Exception as e:
        logger.error(f"Error querying Zero tokens: {e}")

    return total_wire, total_turns, channels


def query_ivy_tokens(start_ts: float, end_ts: float) -> Tuple[int, int]:
    """Query Ivy's token burn from Host 2 for the time window."""
    script = f"""
import os, glob, json, time
from datetime import datetime

start_ts = {start_ts}
end_ts = {end_ts}
brain_dir = os.path.join(os.sep, 'volume1', 'docker', 'discord-ivy-agent', 'gemini', 'antigravity-cli', 'brain')

total_turns = 0
total_bytes = 0

if os.path.exists(brain_dir):
    for d in os.listdir(brain_dir):
        p = os.path.join(brain_dir, d)
        if os.path.isdir(p) and len(d) == 36:
            tpath = os.path.join(p, '.system_generated', 'logs', 'transcript.jsonl')
            if os.path.exists(tpath):
                mtime = os.path.getmtime(tpath)
                if mtime >= start_ts:
                    sz = os.path.getsize(tpath)
                    # Quick turn scan
                    turns = 0
                    try:
                        with open(tpath, 'r', encoding='utf-8', errors='replace') as f:
                            for line in f:
                                if '"USER_INPUT"' in line or '"USER_EXPLICIT"' in line:
                                    turns += 1
                    except Exception:
                        pass
                    if turns > 0:
                        total_turns += turns
                        total_bytes += sz

# Standard Ivy token formula
est_tokens = max(0, total_bytes // 3)
print(json.dumps({{'wire_tokens': est_tokens, 'turns': total_turns}}))
"""
    try:
        res = _run_host2_python(script)
        if res and isinstance(res, dict):
            return int(res.get("wire_tokens", 0)), int(res.get("turns", 0))
    except Exception as e:
        logger.warning(f"Failed to query Ivy tokens from Host 2: {e}")

    return 0, 0


def get_weekly_reset_start_ts(now_pt: Optional[datetime] = None) -> float:
    """Return timestamp of the most recent Monday 2:14 PM PT weekly quota reset."""
    now_dt = now_pt or datetime.now(PT_TZ)
    days_since_monday = now_dt.weekday()  # Monday == 0
    candidate = (now_dt - timedelta(days=days_since_monday)).replace(
        hour=14, minute=14, second=0, microsecond=0
    )
    if candidate > now_dt:
        candidate -= timedelta(days=7)
    return candidate.timestamp()


def query_rolling_7d_total() -> int:
    """Query wire token grand total for fleet since the Monday 2:14 PM PT weekly quota reset."""
    since_ts = get_weekly_reset_start_ts()
    zero_wire = 0
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
            cur = conn.cursor()
            cur.execute(
                "SELECT SUM(total_tokens) FROM token_usage WHERE timestamp >= ?",
                (since_ts,),
            )
            row = cur.fetchone()
            zero_wire = row[0] or 0
            conn.close()
        except Exception as e:
            logger.error(f"Error querying weekly quota total: {e}")

    elapsed_days = max(0.5, (time.time() - since_ts) / 86400.0)
    ivy_cycle = int(1_600_000 * min(7.0, elapsed_days))
    return zero_wire + ivy_cycle


def evaluate_daily_token_budget(
    target_day: Optional[str] = None,
    budget_ceiling: int = DAILY_BUDGET_CEILING,
) -> Dict[str, Any]:
    """Evaluate token usage against daily budget and compile audit report."""
    start_ts, end_ts, day_label = get_evaluation_window(target_day)

    zero_wire, zero_turns, channels = query_zero_tokens(start_ts, end_ts)
    ivy_wire, ivy_turns = query_ivy_tokens(start_ts, end_ts)

    fleet_wire = zero_wire + ivy_wire
    fleet_turns = zero_turns + ivy_turns

    pct_daily = round((fleet_wire / budget_ceiling) * 100, 1) if budget_ceiling else 0.0
    is_exceeded = fleet_wire > budget_ceiling
    is_warning = (fleet_wire >= int(budget_ceiling * 0.85)) and not is_exceeded
    overage = max(0, fleet_wire - budget_ceiling)

    rolling_7d = query_rolling_7d_total()
    pct_weekly = round((rolling_7d / WEEKLY_TOKEN_CEILING) * 100, 1)

    # Format warning message if exceeded or warning threshold reached (>=85%)
    alert_lines = []
    if is_exceeded or is_warning:
        icon = "⚠️" if is_exceeded else "🟡"
        status_label = "Exceeded" if is_exceeded else "Approaching Ceiling"
        alert_lines = [
            f"{icon} **Daily Token Budget {status_label}: {fleet_wire:,} / {budget_ceiling // 1_000_000}M ({pct_daily}%)**",
            (
                f"The combined fleet exceeded the **{budget_ceiling // 1_000_000}M daily ceiling** by **{overage:,} tokens** for {day_label} (Pacific Time)."
                if is_exceeded else
                f"The combined fleet reached **{pct_daily}%** of the **{budget_ceiling // 1_000_000}M daily ceiling** for {day_label} (Pacific Time)."
            ),
            "",
            "### 📊 Daily Channel Burn Breakdown",
        ]
        for c in channels[:6]:
            c_pct = round((c["wire_tokens"] / fleet_wire) * 100, 1) if fleet_wire else 0.0
            alert_lines.append(
                f"- **#{c['channel']}:** {c['wire_tokens']:,} tokens ({c_pct}% of day | {c['turns']} turns)"
            )
        if ivy_wire > 0:
            ivy_pct = round((ivy_wire / fleet_wire) * 100, 1) if fleet_wire else 0.0
            alert_lines.append(
                f"- **Ivy (#baseball):** {ivy_wire:,} tokens ({ivy_pct}% of day | {ivy_turns} turns)"
            )

        alert_lines.extend([
            "",
            "### 📈 Rolling 7-Day Context",
            f"- **Cumulative 7-Day Burn:** {rolling_7d:,} / {WEEKLY_TOKEN_CEILING / 1e9:.3f}B ({pct_weekly}% of weekly ceiling)",
            f"- **Fleet Turn Velocity:** {fleet_turns} turns today",
            "- **Risk Assessment:** High sustained burn velocity accelerates approach to Google Gemini weekly subscription rate limits.",
        ])

    alert_text = "\n".join(alert_lines)

    return {
        "status": "warning" if is_exceeded else "ok",
        "day_label": day_label,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "fleet_wire_tokens": fleet_wire,
        "budget_ceiling": budget_ceiling,
        "pct_daily": pct_daily,
        "is_exceeded": is_exceeded,
        "is_warning": is_warning,
        "overage_tokens": overage,
        "zero_tokens": zero_wire,
        "zero_turns": zero_turns,
        "ivy_tokens": ivy_wire,
        "ivy_turns": ivy_turns,
        "total_turns": fleet_turns,
        "rolling_7d_tokens": rolling_7d,
        "pct_weekly": pct_weekly,
        "channels": channels,
        "alert_text": alert_text,
    }


def dispatch_token_budget_alert(
    report: Dict[str, Any],
    channel: str = DEFAULT_TARGET_CHANNEL,
    force: bool = False,
) -> bool:
    """Dispatch warning alert to Discord outbox if exceeded, warning threshold hit, or forced."""
    if not report.get("is_exceeded") and not report.get("is_warning") and not force:
        ceiling_m = report.get("budget_ceiling", DAILY_BUDGET_CEILING) // 1_000_000
        logger.info(
            f"Token burn nominal: {report.get('fleet_wire_tokens'):,} / {ceiling_m}M ({report.get('pct_daily')}%). Suppressing notification."
        )
        return False

    alert_text = report.get("alert_text")
    if not alert_text:
        ceiling_m = report.get("budget_ceiling", DAILY_BUDGET_CEILING) // 1_000_000
        weekly_b = f"{WEEKLY_TOKEN_CEILING / 1e9:.3f}B"
        # If forced nominal alert
        alert_text = (
            f"📊 **Daily Token Budget Nominal: {report.get('fleet_wire_tokens'):,} / {ceiling_m}M ({report.get('pct_daily')}%)**\n"
            f"Daily consumption for {report.get('day_label')} is well within allocation. "
            f"Rolling 7-day is at **{report.get('pct_weekly')}%** of the {weekly_b} weekly ceiling. ✅"
        )

    try:
        ok = queue_outbox_message(channel, alert_text)
        if ok:
            flush_pending_messages()
            logger.info(f"Dispatched token budget alert to #{channel}")
            return True
        else:
            logger.warning(f"Failed to queue token budget alert to #{channel}")
            return False
    except Exception as e:
        logger.error(f"Error dispatching token budget alert: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Daily Token Budget Watchdog")
    parser.add_argument("--check", action="store_true", help="Evaluate budget without dispatching alert")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch alert to Discord if budget is exceeded")
    parser.add_argument("--force", action="store_true", help="Force dispatch alert even if nominal")
    parser.add_argument("--channel", default=DEFAULT_TARGET_CHANNEL, help="Target Discord channel (default: zero-ops)")
    parser.add_argument("--day", choices=["today", "yesterday"], default=None, help="Target calendar day")
    parser.add_argument("--json", action="store_true", help="Output full JSON evaluation")

    args = parser.parse_args()

    report = evaluate_daily_token_budget(target_day=args.day)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status_sym = "⚠️ EXCEEDED" if report["is_exceeded"] else "🟢 NOMINAL"
        print(
            f"[{status_sym}] Fleet Wire: {report['fleet_wire_tokens']:,} / {report['budget_ceiling']:,} "
            f"({report['pct_daily']}%) for {report['day_label']} PT (Turns: {report['total_turns']})"
        )
        if report["is_exceeded"]:
            print("\n" + report["alert_text"])

    if args.dispatch or args.force:
        dispatched = dispatch_token_budget_alert(report, channel=args.channel, force=args.force)
        print(f"Dispatch status: {dispatched}")

    sys.exit(0 if not report["is_exceeded"] else 1)


if __name__ == "__main__":
    main()
