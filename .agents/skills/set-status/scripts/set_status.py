#!/usr/bin/env python3
"""
set_status.py - Zero Bot Discord Presence & Status Manager
Allows Zero, sidecars, and subagents to update Discord presence, custom status,
and activity indicators so peers and users know the active operating state.
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path("/workspace/data")
STATUS_FILE = DATA_DIR / "bot_status.json"

DEFAULT_STATUS = {
    "status": "online",
    "activity_type": "custom",
    "activity_text": "Zero is online and ready.",
    "is_custom": False,
    "updated_at": 0.0,
    "updated_at_iso": ""
}

VALID_STATUSES = {"online", "idle", "dnd", "invisible"}
VALID_ACTIVITY_TYPES = {"custom", "playing", "watching", "listening", "competing"}

def get_status() -> dict:
    """Read the current status configuration from disk."""
    if not STATUS_FILE.exists():
        return dict(DEFAULT_STATUS)
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in DEFAULT_STATUS.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception as e:
        print(f"[SetStatus] Warning: Could not read status file ({e}), returning default.", file=sys.stderr)
        return dict(DEFAULT_STATUS)

def set_status(
    activity: str,
    status: str = "online",
    activity_type: str = "custom",
    is_custom: bool = True
) -> dict:
    """
    Set and persist Discord bot status and activity.
    
    :param activity: Status / activity text (e.g. 'Investigating memory leak in #the-banana-stand')
    :param status: 'online', 'idle', 'dnd', or 'invisible'
    :param activity_type: 'custom', 'playing', 'watching', 'listening', 'competing'
    :param is_custom: True if set explicitly by user/agent, False if system idle default
    :return: dict of updated status
    """
    status = status.lower().strip()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}")

    activity_type = activity_type.lower().strip()
    if activity_type not in VALID_ACTIVITY_TYPES:
        raise ValueError(f"Invalid activity_type '{activity_type}'. Must be one of: {', '.join(sorted(VALID_ACTIVITY_TYPES))}")

    activity_text = activity.strip()[:128]
    now_ts = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    data = {
        "status": status,
        "activity_type": activity_type,
        "activity_text": activity_text,
        "is_custom": is_custom,
        "updated_at": now_ts,
        "updated_at_iso": now_iso
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = STATUS_FILE.with_suffix(f".tmp.{os.getpid()}")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(STATUS_FILE)
    except Exception as e:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass
        raise IOError(f"Failed to persist bot status to {STATUS_FILE}: {e}")

    return data

def reset_status() -> dict:
    """Reset status back to default online idle state."""
    return set_status(
        activity=DEFAULT_STATUS["activity_text"],
        status="online",
        activity_type="custom",
        is_custom=False
    )

def main():
    parser = argparse.ArgumentParser(
        description="Set or inspect Zero's Discord presence status and activity string.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 tools/set_status.py "Investigating memory leak in #the-banana-stand"
  python3 tools/set_status.py "Docker containers" --type watching --status dnd
  python3 tools/set_status.py --status idle
  python3 tools/set_status.py --reset
  python3 tools/set_status.py --get
"""
    )
    parser.add_argument("activity", nargs="?", default=None, help="Custom activity / presence status message")
    parser.add_argument("--status", "-s", choices=sorted(VALID_STATUSES), default=None, help="Presence state: online, idle, dnd, invisible")
    parser.add_argument("--type", "-t", dest="activity_type", choices=sorted(VALID_ACTIVITY_TYPES), default="custom", help="Activity type")
    parser.add_argument("--reset", action="store_true", help="Reset presence back to default idle status")
    parser.add_argument("--get", "-g", action="store_true", help="Print current status JSON and exit")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    if args.get:
        curr = get_status()
        if args.json:
            print(json.dumps(curr, indent=2))
        else:
            print(f"Status: {curr['status'].upper()}")
            print(f"Activity ({curr['activity_type']}): {curr['activity_text']}")
            print(f"Custom override: {curr['is_custom']}")
            print(f"Updated: {curr.get('updated_at_iso', 'N/A')}")
        return

    if args.reset:
        res = reset_status()
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"✅ Reset Discord presence to default: {res['status']} | '{res['activity_text']}'")
        return

    if args.activity is None and args.status is None:
        parser.print_help()
        sys.exit(1)

    curr = get_status()
    target_activity = args.activity if args.activity is not None else curr["activity_text"]
    target_status = args.status if args.status is not None else curr["status"]
    target_type = args.activity_type

    res = set_status(
        activity=target_activity,
        status=target_status,
        activity_type=target_type,
        is_custom=True
    )

    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"✅ Updated Discord presence: [{res['status'].upper()}] {res['activity_type'].capitalize()} -> \"{res['activity_text']}\"")

if __name__ == "__main__":
    main()
