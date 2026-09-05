#!/usr/bin/env python3
"""
Daily Multi-Channel Session Rollover & Retention Engine for Zero.

Executes nightly at 2:00 AM PT:
1. Multi-channel rollover: Scans all active sessions across home channels, Crab Cavern,
   and active threads.
2. Dual-tier summaries: Generates carry-forward summaries for all active channels.
3. Persistent reset flags: Marks all active session keys for fresh conversation detachment
   on next turn, persisted to disk across bridge reloads.
4. Stale thread garbage collection: Evicts thread mappings inactive for >48 hours.
5. Brain retention: Enforces rolling 30-day retention on historical conversation folders
   and synchronizes SQLite FTS5 search index.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from tools.bridge_state import (
    DATA_DIR,
    SESSIONS_FILE,
    SESSION_METADATA_FILE,
    DEFAULT_HOME_CHANNELS,
    TARGET_CHANNEL_ID,
    get_session_metadata,
    clear_channel_session_id,
    get_channel_session_id,
)
from tools.bridge_daemons import DEDICATED_CHANNEL_CONFIGS
from tools.session_summarizer import generate_summary
from tools.brain_retention import prune_brain_sessions
import tools.bridge_runner as br


def run_daily_session_rollover(bot=None, dry_run: bool = False) -> tuple[bool, str, dict]:
    """Execute daily multi-channel session rollover and 30-day brain retention."""
    rolled_over = []
    pruned_threads = []
    errors = []
    now = time.time()

    # 1. Read all mapped sessions
    sessions_map = {}
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE) as f:
                sessions_map = json.load(f)
        except Exception as e:
            errors.append(f"Failed to read sessions.json: {e}")

    # Ensure "home" is represented if mapped or under TARGET_CHANNEL_ID
    if "home" not in sessions_map:
        home_cid = sessions_map.get(str(TARGET_CHANNEL_ID))
        if home_cid:
            sessions_map["home"] = home_cid

    # 2. Iterate each mapped session
    for sess_key, conv_id in list(sessions_map.items()):
        if not conv_id:
            continue

        is_home_alias = (sess_key == "home")
        is_known_primary = is_home_alias or (sess_key.isdigit() and int(sess_key) in DEFAULT_HOME_CHANNELS)
        is_dedicated = sess_key.isdigit() and int(sess_key) in DEDICATED_CHANNEL_CONFIGS

        # Check staleness for temporary threads (not primary and not dedicated)
        if not is_known_primary and not is_dedicated and not is_home_alias:
            meta = get_session_metadata(sess_key)
            last_active = meta.get("last_active")
            # If thread has been inactive for > 48 hours, evict from sessions.json
            if last_active and (now - last_active) > (48 * 3600):
                if not dry_run:
                    clear_channel_session_id(sess_key, "home")
                    clear_channel_session_id(sess_key, "external")
                    br.reset_session_keys.discard(sess_key)
                pruned_threads.append(sess_key)
                continue

        # Active session: Generate carry-forward context & flag for reset
        try:
            if not dry_run:
                generate_summary(conv_id=conv_id, sess_key=sess_key)
                br.reset_session_keys.add(sess_key)
            rolled_over.append(sess_key)
        except Exception as se:
            errors.append(f"Summary failed for {sess_key}: {se}")

    # Ensure "home" is always flagged for reset
    if not dry_run:
        br.reset_session_keys.add("home")
    if "home" not in rolled_over:
        rolled_over.append("home")

    # 3. Brain 30-day retention prune
    retention_stats = {}
    try:
        retention_stats = prune_brain_sessions(max_age_days=30, dry_run=dry_run)
    except Exception as exc:
        errors.append(f"Brain retention failed: {exc}")

    pruned_sessions = retention_stats.get("pruned_count", 0)
    freed_mb = retention_stats.get("freed_mb", 0.0)

    # 4. Generate structured report
    prefix = "[DRY RUN] " if dry_run else ""
    summary_lines = [
        f"🔄 {prefix}**Daily Session Rollover & Retention Complete** (2:00 AM PT):",
        f"• Rolled over **{len(rolled_over)} active session(s)**: `{', '.join(rolled_over[:6])}{'...' if len(rolled_over) > 6 else ''}`",
        f"• Pruned **{len(pruned_threads)} stale thread mapping(s)** (>48h inactive)",
        f"• Brain retention: pruned **{pruned_sessions} session(s)** older than 30 days ({freed_mb} MB freed)",
    ]
    if errors:
        summary_lines.append(f"⚠️ **Warnings:** {'; '.join(errors)}")

    report = "\n".join(summary_lines)
    success = (len(errors) == 0)

    return success, report, {
        "rolled_over": rolled_over,
        "pruned_threads": pruned_threads,
        "retention": retention_stats,
        "errors": errors
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run daily multi-channel session rollover.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without resetting or deleting files")
    parser.add_argument("--quiet", action="store_true", help="Output minimal machine-readable JSON")
    args = parser.parse_args()

    ok, rep, details = run_daily_session_rollover(dry_run=args.dry_run)
    if args.quiet:
        print(json.dumps(details))
    else:
        print(rep)
