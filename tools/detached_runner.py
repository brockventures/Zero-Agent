#!/usr/bin/env python3
"""
detached_runner.py - Decoupled Background Task Runner with Outbox Delivery

Executes long-running commands (>3 minutes) completely detached from active
Discord bridge turns, logging stdout/stderr to disk and delivering completion/error
notifications to the originating Discord channel via tools/outbox.py.
"""

import os
import sys
import time
import json
import signal
import subprocess
import argparse
import re
from pathlib import Path
from datetime import datetime, timezone

TASKS_DIR = Path("/workspace/data/detached_tasks")
TASKS_DIR.mkdir(parents=True, exist_ok=True)

# Add /workspace and /workspace/tools to sys.path
WORKSPACE_DIR = Path("/workspace")
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))
TOOLS_DIR = WORKSPACE_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


def _format_duration(seconds: float) -> str:
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    rem_secs = secs % 60
    if mins < 60:
        return f"{mins}m {rem_secs}s"
    hours = mins // 60
    rem_mins = mins % 60
    return f"{hours}h {rem_mins}m {rem_secs}s"


def _is_pid_running(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _get_task_dir(task_id: str) -> Path:
    return TASKS_DIR / task_id


def _read_meta(task_id: str) -> dict | None:
    meta_file = _get_task_dir(task_id) / "meta.json"
    if not meta_file.exists():
        return None
    try:
        with open(meta_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_meta(task_id: str, meta: dict):
    task_dir = _get_task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    meta_file = task_dir / "meta.json"
    temp_file = task_dir / "meta.json.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    temp_file.replace(meta_file)


def start_task(
    command: str,
    channel: str = "zero-chat",
    name: str = "",
    timeout: int = 3600,
    cwd: str = "/workspace",
    notify_on_success: bool = False,
) -> dict:
    """Spawn a detached background task and return its metadata immediately."""
    name_clean = name.strip() or "Ad-Hoc Background Task"
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", name_clean.lower().replace(" ", "-"))[:20] or "job"
    task_id = f"dt-{int(time.time())}-{slug}"
    task_dir = _get_task_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    log_file = task_dir / "run.log"
    log_file.touch(exist_ok=True)

    meta = {
        "task_id": task_id,
        "name": name_clean,
        "command": command,
        "channel": channel,
        "timeout": int(timeout),
        "cwd": cwd,
        "status": "starting",
        "worker_pid": None,
        "child_pid": None,
        "start_time": time.time(),
        "start_time_iso": datetime.now(timezone.utc).isoformat(),
        "end_time": None,
        "duration_seconds": None,
        "exit_code": None,
        "notify_on_success": bool(notify_on_success),
        "log_file": str(log_file),
    }
    _write_meta(task_id, meta)

    # Launch detached worker process
    worker_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_worker",
        task_id,
    ]
    proc = subprocess.Popen(
        worker_cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Give worker a brief moment to record its PID
    time.sleep(0.15)
    updated_meta = _read_meta(task_id) or meta
    if not updated_meta.get("worker_pid"):
        updated_meta["worker_pid"] = proc.pid
        _write_meta(task_id, updated_meta)

    return updated_meta


def _run_worker(task_id: str):
    """Internal entrypoint executed in the detached background worker process."""
    meta = _read_meta(task_id)
    if not meta:
        sys.exit(1)

    meta["worker_pid"] = os.getpid()
    meta["status"] = "running"
    _write_meta(task_id, meta)

    command = meta["command"]
    timeout = meta.get("timeout", 3600)
    cwd = meta.get("cwd", "/workspace")
    channel = meta.get("channel", "zero-chat")
    name = meta.get("name", "Background Task")
    log_file_path = Path(meta["log_file"])

    start_ts = time.time()
    exit_code = -1
    timed_out = False
    cancelled = False

    with open(log_file_path, "a", encoding="utf-8") as lf:
        lf.write(f"--- [Detached Runner] Started at {datetime.now(timezone.utc).isoformat()} ---\n")
        lf.write(f"--- Command: {command} ---\n")
        lf.write(f"--- Target Channel: {channel} | Timeout: {timeout}s ---\n\n")
        lf.flush()

        try:
            child_proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            meta["child_pid"] = child_proc.pid
            _write_meta(task_id, meta)

            # Wait for completion or timeout
            try:
                exit_code = child_proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                lf.write(f"\n--- [Detached Runner] ERROR: Task exceeded {timeout}s timeout. Terminating... ---\n")
                lf.flush()
                try:
                    os.killpg(os.getpgid(child_proc.pid), signal.SIGTERM)
                    time.sleep(1.0)
                    if child_proc.poll() is None:
                        os.killpg(os.getpgid(child_proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                exit_code = -99

        except Exception as ex:
            lf.write(f"\n--- [Detached Runner] Process spawn error: {ex} ---\n")
            lf.flush()
            exit_code = -1

    end_ts = time.time()
    duration = end_ts - start_ts

    # Check if task was cancelled externally during execution
    fresh_meta = _read_meta(task_id) or meta
    if fresh_meta.get("status") == "cancelled":
        cancelled = True

    final_status = "cancelled" if cancelled else ("timed_out" if timed_out else ("completed" if exit_code == 0 else "failed"))
    fresh_meta["status"] = final_status
    fresh_meta["exit_code"] = exit_code
    fresh_meta["end_time"] = end_ts
    fresh_meta["duration_seconds"] = duration
    _write_meta(task_id, fresh_meta)

    dur_str = _format_duration(duration)

    # Check notification policy: failure-only by default
    notify_on_success = fresh_meta.get("notify_on_success", False)
    if not notify_on_success:
        try:
            from tools.bridge_state import get_runtime_rules
            notify_on_success = get_runtime_rules().get("detached_notify_on_success", False)
        except Exception:
            pass

    # Suppress notification if task completed successfully and notify_on_success is False
    if final_status == "completed" and not notify_on_success:
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n[Detached Runner] Task completed successfully in {dur_str}. Suppressing success notification (failure-only policy active).\n")
        return

    # Extract log tail snippet for outbox notice, filtering runner headers
    log_snippet = ""
    try:
        with open(log_file_path, "r", encoding="utf-8", errors="replace") as lf:
            lines = lf.readlines()
            filtered = [
                line.rstrip() for line in lines
                if line.strip() and not line.strip().startswith("--- [Detached Runner]")
            ]
            tail = filtered[-12:]
            if tail:
                log_snippet = "\n```text\n" + "\n".join(tail) + "\n```"
    except Exception:
        pass

    # Build clean outbox notification
    if final_status == "completed":
        header = f"✅ **Background Task Complete: {name}** ({dur_str})"
    elif final_status == "timed_out":
        header = f"⏱️ **Background Task Timed Out: {name}** (Limit: {timeout}s)"
    elif final_status == "cancelled":
        header = f"🛑 **Background Task Cancelled: {name}** (after {dur_str})"
    else:
        header = f"❌ **Background Task Failed: {name}** (Exit Code: {exit_code}, after {dur_str})"

    msg_lines = [header]
    if log_snippet:
        snippet_header = "**Output:**" if final_status == "completed" else "**Error Excerpt:**"
        msg_lines.append(f"{snippet_header}{log_snippet}")

    msg_lines.append(f"• *Inspect full logs:* `python3 /workspace/tools/detached_runner.py logs {task_id}`")

    outbox_body = "\n".join(msg_lines)

    # Deliver via Outbox
    try:
        from tools.outbox import queue_outbox_message
        queue_outbox_message(
            channel=channel,
            content=outbox_body,
            source_turn=f"detached-{task_id}",
        )
    except Exception as oe:
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n[Detached Runner] Failed to dispatch outbox message: {oe}\n")


def cancel_task(task_id: str) -> dict:
    """Cancel an active background task."""
    meta = _read_meta(task_id)
    if not meta:
        return {"error": f"Task '{task_id}' not found."}

    child_pid = meta.get("child_pid")
    worker_pid = meta.get("worker_pid")

    meta["status"] = "cancelled"
    _write_meta(task_id, meta)

    killed = False
    for pid in (child_pid, worker_pid):
        if pid and _is_pid_running(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                killed = True
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed = True
                except Exception:
                    pass

    return {"task_id": task_id, "status": "cancelled", "process_killed": killed}


def get_task_status(task_id: str) -> dict:
    """Get status of a specific task."""
    meta = _read_meta(task_id)
    if not meta:
        return {"error": f"Task '{task_id}' not found."}

    child_pid = meta.get("child_pid")
    worker_pid = meta.get("worker_pid")
    is_alive = False
    if child_pid and _is_pid_running(child_pid):
        is_alive = True
    elif worker_pid and _is_pid_running(worker_pid):
        is_alive = True

    if meta["status"] == "running" and not is_alive:
        meta["status"] = "unknown_exit"

    meta["is_running"] = is_alive
    if is_alive:
        meta["current_duration"] = _format_duration(time.time() - meta["start_time"])
    elif meta.get("duration_seconds"):
        meta["current_duration"] = _format_duration(meta["duration_seconds"])

    return meta


def list_tasks(limit: int = 15) -> list[dict]:
    """List recent detached tasks."""
    tasks = []
    if not TASKS_DIR.exists():
        return tasks

    for entry in sorted(TASKS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if entry.is_dir():
            meta = _read_meta(entry.name)
            if meta:
                child_pid = meta.get("child_pid")
                meta["is_running"] = bool(child_pid and _is_pid_running(child_pid))
                tasks.append(meta)
                if len(tasks) >= limit:
                    break
    return tasks


def main():
    parser = argparse.ArgumentParser(description="Decoupled background task runner with outbox delivery.")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # Start
    start_p = subparsers.add_parser("start", help="Spawn a detached background task")
    start_p.add_argument("--command", "-c", required=True, help="Shell command to execute")
    start_p.add_argument("--channel", help="Originating Discord channel for outbox notice (default: zero-chat)", default="zero-chat")
    start_p.add_argument("--name", "-n", help="Human-readable name for the task", default="")
    start_p.add_argument("--timeout", "-t", type=int, help="Timeout in seconds (default: 3600)", default=3600)
    start_p.add_argument("--cwd", help="Working directory (default: /workspace)", default="/workspace")
    start_p.add_argument("--notify-on-success", action="store_true", help="Send outbox notification even on successful completion (default: False, failure-only)")

    # Status
    status_p = subparsers.add_parser("status", help="Check status of a detached task")
    status_p.add_argument("task_id", help="Task ID")

    # List
    list_p = subparsers.add_parser("list", help="List recent detached tasks")
    list_p.add_argument("--limit", type=int, default=10, help="Max tasks to show")

    # Cancel
    cancel_p = subparsers.add_parser("cancel", help="Cancel a running detached task")
    cancel_p.add_argument("task_id", help="Task ID")

    # Logs
    logs_p = subparsers.add_parser("logs", help="Tail logs for a task")
    logs_p.add_argument("task_id", help="Task ID")
    logs_p.add_argument("--lines", "-n", type=int, default=30, help="Number of lines to tail")

    # Internal Worker
    worker_p = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    worker_p.add_argument("task_id", help="Task ID")

    args = parser.parse_args()

    if args.action == "start":
        res = start_task(
            command=args.command,
            channel=args.channel,
            name=args.name,
            timeout=args.timeout,
            cwd=args.cwd,
            notify_on_success=args.notify_on_success,
        )
        print(json.dumps(res, indent=2))

    elif args.action == "_worker":
        _run_worker(args.task_id)

    elif args.action == "status":
        res = get_task_status(args.task_id)
        print(json.dumps(res, indent=2))

    elif args.action == "list":
        tasks = list_tasks(limit=args.limit)
        print(json.dumps(tasks, indent=2))

    elif args.action == "cancel":
        res = cancel_task(args.task_id)
        print(json.dumps(res, indent=2))

    elif args.action == "logs":
        meta = _read_meta(args.task_id)
        if not meta:
            print(f"Error: Task '{args.task_id}' not found.", file=sys.stderr)
            sys.exit(1)
        log_path = Path(meta["log_file"])
        if not log_path.exists():
            print("Log file empty or not yet created.")
            sys.exit(0)
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            tail = lines[-args.lines:] if len(lines) > args.lines else lines
            sys.stdout.write("".join(tail))


if __name__ == "__main__":
    main()
