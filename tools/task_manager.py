#!/usr/bin/env python3
"""Project and Task Management for Ivy-AG.

Maintains /workspace/data/tasks.json:
- Tracks P1, P2, and P3/parking-lot project items.
- Supports list, add, update, delete, clear_completed.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
TASKS_FILE = DATA_DIR / "tasks.json"

log = logging.getLogger("task_manager")

def _try_sync():
    try:
        from tools.google_tasks_sync import sync_tasks
        sync_tasks(quiet=True)
    except Exception:
        pass

def task_manage(action: str = "list", title: str = "", priority: str | None = None, status: str | None = None, task_id: int | None = None) -> dict:
    """Manage lightweight task tracker.
    action: 'list' | 'add' | 'update' | 'delete' | 'clear_completed'
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tasks = []
    if TASKS_FILE.exists():
        try:
            with open(TASKS_FILE) as f:
                tasks = json.load(f)
        except Exception:
            tasks = []

    action = action.lower().strip()
    if action == "list":
        return {"ok": True, "count": len(tasks), "tasks": tasks}

    if action == "add":
        if not title:
            return {"ok": False, "error": "title required to add task"}
        new_id = max([t.get("id", 0) for t in tasks], default=0) + 1
        new_task = {
            "id": new_id,
            "title": title.strip(),
            "priority": (priority or "p2").lower(),
            "status": (status or "pending").lower(),
            "updated": datetime.now(PT).strftime("%Y-%m-%d %H:%M"),
        }
        tasks.append(new_task)
        with open(TASKS_FILE, "w") as f:
            json.dump(tasks, f, indent=2)
        _try_sync()
        return {"ok": True, "action": "added", "task": new_task}

    if action == "update":
        if task_id is None:
            return {"ok": False, "error": "task_id required to update"}
        for t in tasks:
            if t.get("id") == task_id:
                if title:
                    t["title"] = title.strip()
                if priority is not None:
                    t["priority"] = priority.lower()
                if status is not None:
                    t["status"] = status.lower()
                t["updated"] = datetime.now(PT).strftime("%Y-%m-%d %H:%M")
                with open(TASKS_FILE, "w") as f:
                    json.dump(tasks, f, indent=2)
                _try_sync()
                return {"ok": True, "action": "updated", "task": t}
        return {"ok": False, "error": f"task_id {task_id} not found"}

    if action == "delete":
        if task_id is None:
            return {"ok": False, "error": "task_id required to delete"}
        before = len(tasks)
        tasks = [t for t in tasks if t.get("id") != task_id]
        if len(tasks) == before:
            return {"ok": False, "error": f"task_id {task_id} not found"}
        with open(TASKS_FILE, "w") as f:
            json.dump(tasks, f, indent=2)
        _try_sync()
        return {"ok": True, "action": "deleted", "task_id": task_id}

    if action == "clear_completed":
        before = len(tasks)
        tasks = [t for t in tasks if t.get("status") != "completed"]
        cleared = before - len(tasks)
        with open(TASKS_FILE, "w") as f:
            json.dump(tasks, f, indent=2)
        _try_sync()
        return {"ok": True, "action": "cleared_completed", "cleared_count": cleared}

    if action == "sync":
        try:
            from tools.google_tasks_sync import sync_tasks
            return sync_tasks()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": f"unknown action {action}"}

def format_tasks_summary() -> str:
    """Format active tasks for Discord display."""
    res = task_manage("list")
    tasks = res.get("tasks", [])
    if not tasks:
        return "📋 **Project & Task Tracker**: No tasks currently recorded."

    p1s = [t for t in tasks if t.get("priority") == "p1" and t.get("status") != "completed"]
    p2s = [t for t in tasks if t.get("priority") == "p2" and t.get("status") != "completed"]
    p3s = [t for t in tasks if (t.get("priority") == "p3" or t.get("status") == "parking_lot") and t.get("status") != "completed"]

    out = ["📋 **Project & Task Tracker**\n"]
    if p1s:
        out.append("**🔥 Priority 1 (Active / High Impact):**")
        for t in p1s:
            out.append(f"- `#{t['id']}` **{t['title']}** ({t['status']})")
        out.append("")

    if p2s:
        out.append("**📌 Priority 2 (Backlog / Planned):**")
        for t in p2s:
            out.append(f"- `#{t['id']}` {t['title']}")
        out.append("")

    if p3s:
        out.append("**📦 Parking Lot / Future Explorations:**")
        for t in p3s:
            out.append(f"- `#{t['id']}` {t['title']}")

    return "\n".join(out)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Zero Project & Task Management")
    subparsers = parser.add_subparsers(dest="action", help="Action to perform")

    # summary
    subparsers.add_parser("summary", help="Format tasks summary for Discord")

    # list
    subparsers.add_parser("list", help="List all tasks as JSON")

    # add
    add_p = subparsers.add_parser("add", help="Add a new task")
    add_p.add_argument("title", help="Task title")
    add_p.add_argument("--priority", "-p", default="p2", help="Priority (p1, p2, p3)")
    add_p.add_argument("--status", "-s", default="pending", help="Status (pending, in_progress, completed, etc.)")

    # update
    upd_p = subparsers.add_parser("update", help="Update an existing task")
    upd_p.add_argument("task_id", type=int, help="Task ID")
    upd_p.add_argument("--title", "-t", default=None, help="New title")
    upd_p.add_argument("--priority", "-p", default=None, help="New priority")
    upd_p.add_argument("--status", "-s", default=None, help="New status")

    # delete
    del_p = subparsers.add_parser("delete", help="Delete a task")
    del_p.add_argument("task_id", type=int, help="Task ID")

    # clear_completed
    subparsers.add_parser("clear_completed", help="Clear completed tasks")

    # sync
    subparsers.add_parser("sync", help="Two-way sync with Google Tasks")

    args = parser.parse_args()

    action = args.action or "summary"
    if action == "summary":
        print(format_tasks_summary())
    elif action == "list":
        print(json.dumps(task_manage("list"), indent=2))
    elif action == "add":
        print(json.dumps(task_manage("add", title=args.title, priority=args.priority, status=args.status), indent=2))
    elif action == "update":
        print(json.dumps(task_manage("update", task_id=args.task_id, title=args.title, priority=args.priority, status=args.status), indent=2))
    elif action == "delete":
        print(json.dumps(task_manage("delete", task_id=args.task_id), indent=2))
    elif action == "clear_completed":
        print(json.dumps(task_manage("clear_completed"), indent=2))
    elif action == "sync":
        print(json.dumps(task_manage("sync"), indent=2))

