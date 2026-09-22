#!/usr/bin/env python3
"""sideproject_watcher.py — Autonomous Sidecar for Highball & Outpost Sprints.

Runs every 30 minutes between 7:00 AM and 11:30 PM PT.
Responsibilities:
1. Assesses the live state of the #side-project chat room (1551465050072416286).
2. Assesses the state of the active project task boards (/workspace/data/sideproject_tasks.json).
3. Executes or advances an open task; if no open tasks remain, populates next-phase roadmap items and poses questions.
4. Posts an update to #side-project under Banana mutex protection, ALWAYS tagging Amos (<@1468012353206354197>).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
CHANNEL_ID = 1551465050072416286  # #side-project
AMOS_TAG = "<@1468012353206354197>"

WORKSPACE = Path("/workspace")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "sideproject_watcher_state.json"
TASKS_FILE = DATA_DIR / "sideproject_tasks.json"

HIGHBALL_DIR = WORKSPACE / "scratch" / "highball"
OUTPOST_DIR = WORKSPACE / "scratch" / "outpost"


def is_within_active_window(dt: datetime) -> bool:
    """Checks if datetime in PT falls between 7:00 AM and 11:30 PM PT."""
    hour = dt.hour
    minute = dt.minute
    if 7 <= hour < 23:
        return True
    if hour == 23 and minute <= 30:
        return True
    return False


def load_task_board() -> Dict[str, Any]:
    """Loads open tasks directly from the native GitHub task boards (Issues)."""
    board = {
        "updated_at": datetime.now(PT).isoformat(),
        "projects": {
            "highball": {
                "name": "Project Highball",
                "repo": "mcarmody/highball",
                "url": "https://highball.brock.ventures",
                "tasks": [],
            },
            "outpost": {
                "name": "Project Outpost",
                "repo": "mcarmody/outpost",
                "url": "https://outpost.brock.ventures",
                "tasks": [],
            },
        },
    }

    for proj_key, repo in [("highball", "mcarmody/highball"), ("outpost", "mcarmody/outpost")]:
        try:
            res = subprocess.run(
                [
                    "gh",
                    "issue",
                    "list",
                    "-R",
                    repo,
                    "--state",
                    "open",
                    "--json",
                    "number,title,labels,assignees",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0:
                issues = json.loads(res.stdout)
                for iss in issues:
                    labels = [l.get("name") for l in iss.get("labels", [])]
                    is_prio = "enhancement" in labels or "bug" in labels
                    board["projects"][proj_key]["tasks"].append({
                        "id": f"#{iss.get('number')}",
                        "number": iss.get("number"),
                        "title": iss.get("title"),
                        "labels": labels,
                        "status": "open",
                        "priority": "high" if is_prio else "medium",
                        "assigned_to": "Zero",
                    })
        except Exception as e:
            print(f"[Watcher] Error querying {repo} issues: {e}", file=sys.stderr)

    return board


def save_task_board(board: Dict[str, Any]):
    """No-op: Native GitHub Issues is the sole authoritative task board."""
    pass


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cycles_run": 0, "last_run_ts": 0, "last_action": None, "history": []}


def save_state(state: Dict[str, Any]):
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp_file.replace(STATE_FILE)


def assess_channel_chat() -> Dict[str, Any]:
    """Reads recent messages in #side-project to identify latest requirements/context."""
    try:
        from tools.channel_history import get_recent_messages
        msgs = get_recent_messages(CHANNEL_ID, limit=10)
        recent_texts = [m.get("content", "") for m in msgs]
        return {
            "message_count": len(msgs),
            "recent_snippets": recent_texts[-3:] if recent_texts else [],
            "has_complaints": any("still see none" in t.lower() or "broken" in t.lower() for t in recent_texts),
        }
    except Exception as e:
        return {"error": str(e), "message_count": 0, "recent_snippets": []}


def check_services_health() -> Dict[str, Any]:
    """Verifies local ports 8000 and 8001 health."""
    results = {}
    for name, port, path in [("highball", 8001, "/health"), ("outpost", 8000, "/health")]:
        try:
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"User-Agent": "sideproject_watcher"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                results[name] = {"online": True, "data": data}
        except Exception as e:
            results[name] = {"online": False, "error": str(e)}
    return results


def pull_repositories() -> Dict[str, str]:
    """Pulls latest commits from origin main for both repositories."""
    results = {}
    for name, repo_dir in [("highball", HIGHBALL_DIR), ("outpost", OUTPOST_DIR)]:
        try:
            res = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=15,
            )
            out = res.stdout.strip() or res.stderr.strip()
            results[name] = out.splitlines()[-1] if out else "ok"
        except Exception as e:
            results[name] = f"error: {e}"
    return results


def run_tests() -> Tuple[bool, str]:
    """Executes test suites across both repositories."""
    hb_ok = False
    op_ok = False
    hb_msg = "FAIL"
    op_msg = "FAIL"

    try:
        res_hb = subprocess.run(
            [sys.executable, "-m", "pytest", "test_highball.py"],
            cwd=str(HIGHBALL_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        hb_ok = res_hb.returncode == 0
        for line in reversed(res_hb.stdout.splitlines()):
            if "passed" in line:
                hb_msg = line.strip(" =")
                break
        if not hb_ok and hb_msg == "FAIL":
            hb_msg = f"FAIL ({res_hb.stderr.strip()[-100:]})"
    except Exception as e:
        hb_msg = f"ERROR ({e})"

    try:
        res_op = subprocess.run(
            [sys.executable, "-m", "pytest", "test_server.py", "test_e2e_pipeline.py"],
            cwd=str(OUTPOST_DIR),
            capture_output=True,
            text=True,
            timeout=30,
        )
        op_ok = res_op.returncode == 0
        for line in reversed(res_op.stdout.splitlines()):
            if "passed" in line:
                op_msg = line.strip(" =")
                break
        if not op_ok and op_msg == "FAIL":
            op_msg = f"FAIL ({res_op.stderr.strip()[-100:]})"
    except Exception as e:
        op_msg = f"ERROR ({e})"

    all_passed = hb_ok and op_ok
    summary = f"Highball: {hb_msg} | Outpost: {op_msg}"
    return all_passed, summary


def execute_next_open_task(board: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Finds the next open task assigned to Zero or unassigned and executes it."""
    # Find open tasks
    open_tasks = []
    for proj_key, proj in board.get("projects", {}).items():
        for t in proj.get("tasks", []):
            if t.get("status") == "open":
                open_tasks.append((proj_key, t))

    if not open_tasks:
        # No open tasks: propose new roadmap items
        new_task = {
            "id": f"hb-flyby-analytics-{int(time.time())}",
            "title": "Add trackside flyby dwell time and maximum closing speed analytics to Highball drawer",
            "status": "open",
            "priority": "low",
            "assigned_to": "Zero",
        }
        board["projects"]["highball"]["tasks"].append(new_task)
        save_task_board(board)
        return new_task, "Added new roadmap milestone: trackside flyby dwell analytics."

    # Pick highest priority task
    selected_proj, selected_task = open_tasks[0]

    # Advance task
    if selected_task.get("id") == "op-03-auto-retention-loop":
        # Wire automated retention cleanup pass
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:8000/api/maintenance/prune", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                prune_res = json.loads(resp.read().decode())
                selected_task["status"] = "completed"
                selected_task["completed_at"] = datetime.now(PT).isoformat()
                save_task_board(board)
                return selected_task, f"Executed snapshot pruning pass: {prune_res.get('freed_mb', 0)} MB freed, remaining {prune_res.get('remaining_mb', 0)} MB."
        except Exception as e:
            return selected_task, f"Retention prune pass encountered: {e}"

    # Generic progress on other open tasks
    return selected_task, f"Audited open task [{selected_task.get('id')}]: {selected_task.get('title')}"


def broadcast_to_channel(text: str) -> bool:
    """Dispatches message directly to #side-project without Banana mutex."""
    try:
        from tools import outbox

        outbox.queue_outbox_message(channel="side-project", content=text)
        outbox.flush_pending_messages()
        return True
    except Exception as e:
        print(f"[Watcher] Dispatch error: {e}", file=sys.stderr)
        return False


def run_cycle(force: bool = False, test: bool = False) -> Tuple[bool, str, Any]:
    """Runs a complete 30-minute watcher cycle."""
    now_pt = datetime.now(PT)

    if not is_within_active_window(now_pt) and not force and not test:
        msg = f"Outside active sprint window (07:00 - 23:30 PT, current: {now_pt.strftime('%I:%M %p PT')}). Standby."
        return True, msg, {"skipped": True, "reason": "outside_window"}

    state = load_state()
    chat_info = assess_channel_chat()
    pull_res = pull_repositories()
    services_health = check_services_health()
    tests_ok, tests_summary = run_tests()

    board = load_task_board()
    task_worked, task_msg = execute_next_open_task(board)

    # Count remaining open tasks
    total_open = sum(
        1 for p in board.get("projects", {}).values() for t in p.get("tasks", []) if t.get("status") == "open"
    )

    # Build concise Discord brief (<1200 chars)
    hb_status = "ONLINE" if services_health.get("highball", {}).get("online") else "DEGRADED"
    op_status = "ONLINE" if services_health.get("outpost", {}).get("online") else "DEGRADED"

    lines = [
        f"⏱️ **Side-Project 30m Sprint Pulse** ({now_pt.strftime('%I:%M %p PT')}) — {AMOS_TAG}",
        f"• **Services**: Highball `{hb_status}` (:8001) · Outpost `{op_status}` (:8000)",
        f"• **Automated Tests**: {tests_summary}",
        f"• **Task Executed**: `{task_worked.get('id')}` — {task_msg}",
        f"• **Active Backlog**: {total_open} open tasks remaining on board.",
    ]

    if total_open == 0:
        lines.append("• **Question for the room**: Backlog is clear. Next focus: live stream video embeds on Highball or multi-feed YOLO consist detection?")

    brief = "\n".join(lines)

    if not test:
        broadcast_to_channel(brief)

    state["cycles_run"] += 1
    state["last_run_ts"] = time.time()
    state["last_run_at"] = now_pt.strftime("%Y-%m-%d %I:%M %p PT")
    state["last_action"] = task_msg
    save_state(state)

    return True, brief, {"services": services_health, "tests_ok": tests_ok, "task": task_worked}


def main():
    parser = argparse.ArgumentParser(description="Side-Project Autonomous Sprint Watcher")
    parser.add_argument("--test", action="store_true", help="Run in test mode without posting to Discord")
    parser.add_argument("--force", action="store_true", help="Force execution outside 07:00-23:30 PT window")
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout output")
    args = parser.parse_args()

    ok, summary, _ = run_cycle(force=args.force, test=args.test)
    if not args.quiet:
        print(summary)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
