#!/usr/bin/env python3
# ruff: noqa: BLE001, EXE001
"""
Nightly Brain Retention & Stale Thread Cleanup Engine for Zero.

Executes nightly at 2:00 AM PT:
1. Stale thread garbage collection: Evicts temporary thread mappings inactive for >48 hours
   while preserving active warm sessions across home, dedicated, and Crab Cavern channels.
2. Brain retention: Enforces rolling 30-day retention on historical conversation folders
   and synchronizes SQLite FTS5 search index.
"""

import argparse
import json
import sys
import time
from pathlib import Path

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import tools.bridge_runner as br
from tools.brain_retention import prune_brain_sessions
from tools.bridge_daemons import DEDICATED_CHANNEL_CONFIGS
from tools.bridge_state import (
    DEFAULT_HOME_CHANNELS,
    SESSIONS_FILE,
    clear_channel_session_id,
    get_session_metadata,
)


def run_daily_session_rollover(bot=None, dry_run: bool = False) -> tuple[bool, str, dict]:
    """Execute nightly stale thread cleanup (>48h inactive) and 30-day brain retention."""
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

    # 2. Iterate each mapped session to prune stale temporary threads (>48h inactive)
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
        f"🔄 {prefix}**Nightly Brain Retention & Stale Thread Cleanup Complete** (2:00 AM PT):",
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
        "errors": errors,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run nightly brain retention and stale thread cleanup.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without resetting or deleting files")
    parser.add_argument("--quiet", action="store_true", help="Output minimal machine-readable JSON")
    args = parser.parse_args()

    ok, rep, details = run_daily_session_rollover(dry_run=args.dry_run)
    if args.quiet:
        print(json.dumps(details))
    else:
        print(rep)
