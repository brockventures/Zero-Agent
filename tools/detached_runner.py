#!/usr/bin/env python3
"""
detached_runner.py - Backwards-compatible delegation shim to tools/forge.py (FORGE v1.1).

All detached background task logic (`start_job`, `get_job_status`, `cancel_job`, `list_jobs`,
`_run_job_worker`) is now consolidated natively inside `/workspace/tools/forge.py`.
This module preserves full CLI and Python API compatibility (`start_task`, `get_task_status`,
`cancel_task`, `list_tasks`, `_run_worker`, `_write_meta`, `_read_meta`, `_format_duration`,
and `TASKS_DIR` monkeypatching in unit tests).
"""

import argparse
import json
import sys
from pathlib import Path

WORKSPACE_DIR = Path("/workspace")
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))
TOOLS_DIR = WORKSPACE_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from tools import forge
from tools.forge import _format_duration  # noqa: F401

TASKS_DIR = Path("/workspace/data/detached_tasks")
TASKS_DIR.mkdir(parents=True, exist_ok=True)


def _sync_tasks_dir() -> None:
    """Propagate any runtime or unit-test monkeypatching of TASKS_DIR into tools.forge."""
    forge.TASKS_DIR = TASKS_DIR


def _read_meta(task_id: str) -> dict | None:
    _sync_tasks_dir()
    return forge._read_job_meta(task_id)


def _write_meta(task_id: str, meta: dict) -> None:
    _sync_tasks_dir()
    forge._write_job_meta(task_id, meta)


def start_task(
    command: str,
    channel: str = "zero-chat",
    name: str = "",
    timeout: int = 3600,
    cwd: str = "/workspace",
    notify_on_success: bool = True,
    synthesize: bool = True,
    allow_duplicate: bool = False,
) -> dict:
    """Spawn a detached background task via FORGE's native job runner."""
    _sync_tasks_dir()
    return forge.start_job(
        command=command,
        channel=channel,
        name=name,
        timeout=timeout,
        cwd=cwd,
        notify_on_success=notify_on_success,
        synthesize=synthesize,
        allow_duplicate=allow_duplicate,
    )


def _run_worker(task_id: str) -> None:
    """Internal entrypoint executed in the detached background worker process."""
    _sync_tasks_dir()
    forge._run_job_worker(task_id)


def cancel_task(task_id: str) -> dict:
    """Cancel an active background task via FORGE."""
    _sync_tasks_dir()
    return forge.cancel_job(task_id)


def get_task_status(task_id: str) -> dict:
    """Get status of a specific task via FORGE."""
    _sync_tasks_dir()
    return forge.get_job_status(task_id)


def list_tasks(limit: int = 15) -> list[dict]:
    """List recent detached tasks via FORGE."""
    _sync_tasks_dir()
    return forge.list_jobs(limit=limit)


def main() -> None:
    parser = argparse.ArgumentParser(description="Decoupled background task runner (FORGE delegation shim).")
    subparsers = parser.add_subparsers(dest="action", required=True)

    start_p = subparsers.add_parser("start", help="Spawn a detached background task")
    start_p.add_argument("--command", "-c", required=True, help="Shell command to execute")
    start_p.add_argument("--channel", default="zero-chat", help="Originating Discord channel (default: zero-chat)")
    start_p.add_argument("--name", "-n", default="", help="Human-readable name for the task")
    start_p.add_argument("--timeout", "-t", type=int, default=3600, help="Timeout in seconds (default: 3600)")
    start_p.add_argument("--cwd", default="/workspace", help="Working directory (default: /workspace)")
    start_p.add_argument("--notify-on-success", action="store_true", default=True)
    start_p.add_argument("--silent-on-success", dest="notify_on_success", action="store_false")
    start_p.add_argument("--no-synthesize", dest="synthesize", action="store_false", default=True)
    start_p.add_argument("--allow-duplicate", action="store_true", default=False)

    status_p = subparsers.add_parser("status", help="Check status of a detached task")
    status_p.add_argument("task_id", help="Task ID")

    list_p = subparsers.add_parser("list", help="List recent detached tasks")
    list_p.add_argument("--limit", type=int, default=10, help="Max tasks to show")

    cancel_p = subparsers.add_parser("cancel", help="Cancel a running detached task")
    cancel_p.add_argument("task_id", help="Task ID")

    logs_p = subparsers.add_parser("logs", help="Tail logs for a task")
    logs_p.add_argument("task_id", help="Task ID")
    logs_p.add_argument("--lines", "-n", type=int, default=30, help="Number of lines to tail")

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
            synthesize=args.synthesize,
            allow_duplicate=args.allow_duplicate,
        )
        print(json.dumps(res, indent=2))
    elif args.action == "_worker":
        _run_worker(args.task_id)
    elif args.action == "status":
        print(json.dumps(get_task_status(args.task_id), indent=2))
    elif args.action == "list":
        print(json.dumps(list_tasks(limit=args.limit), indent=2))
    elif args.action == "cancel":
        print(json.dumps(cancel_task(args.task_id), indent=2))
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
