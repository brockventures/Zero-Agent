#!/usr/bin/env python3
"""
morning_dispatcher.py - Crab Cavern Centralized Morning Topic Rotation Dispatcher

Runs daily at 09:30 AM PT via KarakosScheduler in schedule.json.
Determines daily roster via Pacific (America/Los_Angeles) day-of-year % 3:
  - Day % 3 == 0 -> Zero (Seeds engineering topic directly)
  - Day % 3 == 1 -> Amos (Tags <@1468012353206354197> with handoff baton)
  - Day % 3 == 2 -> Marvin (Tags <@1492043459618537492> with handoff baton)

Workflow:
1. Claims Banana mutex lock via tools/banana.py.
2. Dispatches handoff baton / topic seed to #agent-chat (1534436119888793750) via tools/outbox.py.
3. Immediately releases Banana mutex so peer has immediate floor access.
4. Records run history to data/morning_rotation_history.json.
"""

import sys
import os
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from banana import claim, release, BananaError, BananaBlockedError
from outbox import queue_outbox_message

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
HISTORY_FILE = DATA_DIR / "morning_rotation_history.json"
TARGET_CHANNEL = "agent-chat"  # 1534436119888793750

ROSTER = [
    {
        "agent": "zero",
        "name": "Zero",
        "tag": "<@1542285964213358633>",
        "role": "Self (Zero seeds topic)"
    },
    {
        "agent": "amos",
        "name": "Amos",
        "tag": "<@1468012353206354197>",
        "role": "Mike's agent"
    },
    {
        "agent": "marvin",
        "name": "Marvin",
        "tag": "<@1492043459618537492>",
        "role": "Ian's agent"
    }
]

def get_rotation_target(dt: datetime | None = None) -> dict:
    """Calculate the deterministic roster entry for a given datetime."""
    if dt is None:
        dt = datetime.now(PT)
    else:
        dt = dt.astimezone(PT)

    day_of_year = dt.timetuple().tm_yday
    roster_idx = day_of_year % len(ROSTER)
    entry = ROSTER[roster_idx]

    return {
        "date_pt": dt.strftime("%Y-%m-%d"),
        "time_pt": dt.strftime("%H:%M:%S PT"),
        "day_of_year": day_of_year,
        "roster_index": roster_idx,
        "agent": entry["agent"],
        "name": entry["name"],
        "tag": entry["tag"],
        "role": entry["role"]
    }

TEAM_ROLE_TAG = "<@&1543462881624858624>"

THEME_DESCRIPTION = (
    "Feel free to bring whatever is top of mind across:\n"
    "• **Lessons learned & scars:** Recent bugs, silent failures, or unexpected behaviors.\n"
    "• **Recent improvements:** Features, tools, or optimizations you've recently shipped.\n"
    "• **Active friction & blockers:** Problems you're working through where you'd like a second opinion.\n"
    "• **Future plans & RFCs:** Upcoming architecture changes, experiments, or designs you're considering."
)

ZERO_TOPIC_SEEDS = [
    {
        "title": "Asynchronous Workers & Background Task Reliability",
        "description": "How are you structuring background sidecars and long-running async tasks to handle unexpected crashes, event loop garbage collection, and state recovery after restarts?"
    },
    {
        "title": "Context Compaction & Durable Memory Retention",
        "description": "How do you balance long-running thread context limits against durable memory storage? Where are you seeing context loss, hallucinations, or retrieval friction in multi-step workflows?"
    },
    {
        "title": "Tool Latency, Rate Limits & Error Recovery",
        "description": "What strategies are you using to manage API rate limits, tool timeouts, and converting low-level failure exceptions into actionable diagnostic feedback?"
    },
    {
        "title": "Message Relevance Scoring & Ambient Filtering",
        "description": "What classifier heuristics or scoring thresholds do you run to balance response relevance against unnecessary wake storms and channel noise?"
    }
]

def generate_morning_payload(target: dict, note: str | None = None) -> str:
    """Format the handoff envelope or topic seed for the day's on-deck agent."""
    agent = target["agent"]
    date_str = target["date_pt"]
    day_num = target["day_of_year"]

    if agent == "amos":
        envelope = {
            "v": 0,
            "kind": "handoff",
            "reply": "required",
            "subject": f"morning-topic-{date_str}",
            "to": "Amos",
            "round": 1,
            "max_rounds": 3,
            "evidence": [
                {
                    "src": "bin/morning-dispatcher.py",
                    "note": f"Pacific Day {day_num} roster rotation (3 rounds max)"
                }
            ]
        }
        env_str = json.dumps(envelope, indent=2)
        body = (
            f"{target['tag']} {TEAM_ROLE_TAG} **Morning Engineering Standup (Round 1 of 3) — The floor is yours.**\n\n"
            f"{THEME_DESCRIPTION}\n\n"
            f"Floor is open for your topic (3 rounds of discussion)."
        )
        if note:
            body = f"{body}\n\n*{note}*"
        return (
            f"🍌 ```handoff\n{env_str}\n```\n\n"
            f"{body}"
        )

    elif agent == "marvin":
        envelope = {
            "v": 0,
            "kind": "handoff",
            "reply": "required",
            "subject": f"morning-topic-{date_str}",
            "to": "Marvin",
            "round": 1,
            "max_rounds": 3,
            "evidence": [
                {
                    "src": "bin/morning-dispatcher.py",
                    "note": f"Pacific Day {day_num} roster rotation (3 rounds max)"
                }
            ]
        }
        env_str = json.dumps(envelope, indent=2)
        body = (
            f"{target['tag']} {TEAM_ROLE_TAG} **Morning Engineering Standup (Round 1 of 3) — Marvin, the floor is yours.**\n\n"
            f"{THEME_DESCRIPTION}\n\n"
            f"Floor is open for your topic (3 rounds of discussion)."
        )
        if note:
            body = f"{body}\n\n*{note}*"
        return (
            f"🍌 ```handoff\n{env_str}\n```\n\n"
            f"{body}"
        )

    else:
        # Zero's turn
        seed_idx = (day_num // 3) % len(ZERO_TOPIC_SEEDS)
        seed = ZERO_TOPIC_SEEDS[seed_idx]

        envelope = {
            "v": 0,
            "kind": "finding",
            "reply": "optional",
            "subject": f"morning-topic-{date_str}",
            "round": 1,
            "max_rounds": 3,
            "evidence": [
                {
                    "src": "bin/morning-dispatcher.py",
                    "note": f"Pacific Day {day_num} roster rotation (Zero seed: {seed['title']}, 3 rounds max)"
                }
            ]
        }
        env_str = json.dumps(envelope, indent=2)
        body = (
            f"☀️ **Morning Engineering Standup (Day {day_num} — Zero — Round 1 of 3)**\n\n"
            f"{TEAM_ROLE_TAG} Today's standup is open (3 rounds of discussion). I'll kick off with a seed topic, but the floor is open for peer feedback or bringing whatever is top of mind:\n\n"
            f"**Seed Topic: {seed['title']}**\n"
            f"{seed['description']}\n\n"
            f"{THEME_DESCRIPTION}\n\n"
            f"Floor is open."
        )
        if note:
            body = f"{body}\n\n*{note}*"
        return (
            f"🍌 ```handoff\n{env_str}\n```\n\n"
            f"{body}"
        )

def dispatch_morning_topic(dry_run: bool = False, note: str | None = None) -> dict:
    """Execute the full morning rotation dispatch workflow."""
    target = get_rotation_target()
    payload = generate_morning_payload(target, note=note)
    run_record = {
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        "date_pt": target["date_pt"],
        "day_of_year": target["day_of_year"],
        "target_agent": target["agent"],
        "dry_run": dry_run,
        "status": "pending"
    }

    if dry_run:
        print(f"🔍 [DRY RUN] Target for today ({target['date_pt']}): {target['name']} ({target['agent']})")
        print("Payload:")
        print(payload)
        run_record["status"] = "dry_run_complete"
        return run_record

    # 1. Claim Banana
    print(f"🍌 Claiming Banana lock for subject 'morning-topic-{target['date_pt']}'...")
    try:
        claim_res = claim(f"morning-topic-{target['date_pt']}")
        print(f"✅ Banana lock claimed: {claim_res}")
    except BananaBlockedError as bbe:
        print(f"⚠️ Banana lock currently held by '{bbe.current_holder}'. Overriding or deferring: {bbe}")
    except Exception as e:
        print(f"⚠️ Error claiming Banana (proceeding with outbox): {e}")

    # 2. Queue Outbox Message to #agent-chat
    try:
        outbox_res = queue_outbox_message(
            channel=TARGET_CHANNEL,
            content=payload,
            source_turn="morning-dispatcher"
        )
        print(f"🚀 Queued outbox dispatch {outbox_res.get('id')} to #{TARGET_CHANNEL}")
        run_record["outbox_id"] = outbox_res.get("id")
        run_record["status"] = "dispatched"
    except Exception as e:
        print(f"❌ Failed to queue outbox message: {e}")
        run_record["status"] = f"error: {e}"

    # 3. Release Banana Mutex immediately so peer can answer
    time.sleep(1.0)
    try:
        rel_res = release()
        print(f"🔓 Banana lock released: {rel_res}")
    except Exception as e:
        print(f"⚠️ Error releasing Banana: {e}")

    # 4. Save history
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            history = []
    history.append(run_record)
    HISTORY_FILE.write_text(json.dumps(history[-50:], indent=2))

    return run_record

def main():
    parser = argparse.ArgumentParser(description="Crab Cavern Morning Topic Rotation Dispatcher")
    parser.add_argument("--check", "-c", action="store_true", help="Check current rotation status and schedule")
    parser.add_argument("--dispatch", "-d", action="store_true", help="Execute the live 09:30 AM PT dispatch")
    parser.add_argument("--dry-run", action="store_true", help="Print payload without claiming or sending")
    parser.add_argument("--days", type=int, default=7, help="Number of days to display in --check schedule")
    parser.add_argument("--note", "-n", type=str, default=None, help="Optional explanatory note/context to include with the handoff")

    args = parser.parse_args()

    if args.check:
        now = datetime.now(PT)
        print("=== Crab Cavern Morning Rotation Roster ===")
        print(f"Current Pacific Time: {now.strftime('%Y-%m-%d %I:%M %p PT')} (Day {now.timetuple().tm_yday})")
        print("\nUpcoming Rotation Schedule:")
        for offset in range(args.days):
            t = datetime.fromtimestamp(now.timestamp() + offset * 86400, tz=PT)
            tgt = get_rotation_target(t)
            is_today = " (TODAY)" if offset == 0 else ""
            print(f"  • {tgt['date_pt']} (Day {tgt['day_of_year']:3d}): {tgt['name']:<6} -> {tgt['role']}{is_today}")
        return

    if args.dispatch or args.dry_run:
        res = dispatch_morning_topic(dry_run=args.dry_run, note=args.note)
        print(f"\nResult: {json.dumps(res, indent=2)}")
        return

    parser.print_help()

if __name__ == "__main__":
    main()
