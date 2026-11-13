#!/usr/bin/env python3
"""
Periodic Sidecar Ticker for #agora (1558202642663211169) piecewise FORGE builds.

Checks:
1. Whether an active FORGE build is currently running/queued for agora-roguelike.
2. Whether an open PR from Zero on brockventures/agora-roguelike is awaiting CI/review/merge.
3. If the pipeline is idle, pops the next pending task from /workspace/data/agora_forge_queue.json,
   writes a validated v1.1 ForgePlan JSON, and enqueues it to #forge (1562899941595160657).
"""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = "brockventures/agora-roguelike"
REPO_DIR = "/workspace/agora-roguelike"
QUEUE_PATH = Path("/workspace/data/agora_forge_queue.json")
FORGE_DB = Path("/workspace/data/forge.db")
AGORA_CHANNEL_ID = 1558202642663211169


def has_active_forge_build() -> str | None:
    if not FORGE_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(FORGE_DB))
        cur = conn.cursor()
        cur.execute(
            "SELECT build_id, title, status FROM forge_builds "
            "WHERE status IN ('queued', 'running', 'verifying', 'pending_approval') "
            "AND (repo LIKE '%agora-roguelike%' OR title LIKE '%AGORA-F%')"
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return f"{row[0]} ({row[1]} — {row[2]})"
    except Exception:
        pass
    return None


def get_open_prs() -> list[dict]:
    res = subprocess.run(
        ["gh", "pr", "list", "-R", REPO, "--state", "open", "--json", "number,title,headRefName,reviewDecision"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return []
    return json.loads(res.stdout)


def sync_issue_states(data: dict) -> bool:
    changed = False
    res = subprocess.run(
        ["gh", "issue", "list", "-R", REPO, "--state", "all", "--limit", "100", "--json", "number,state"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode == 0 and res.stdout.strip():
        state_map = {i["number"]: i["state"] for i in json.loads(res.stdout)}
        for t in data.get("tasks", []):
            inum = t.get("github_issue")
            if inum in state_map and state_map[inum] == "CLOSED" and t.get("status") != "completed":
                t["status"] = "completed"
                changed = True
    return changed


def ensure_target_files_exist(target_files: list[str]) -> None:
    """Ensure new target files have a tracked placeholder or existing parent so preflight passes."""
    repo_root = Path(REPO_DIR)
    for rel in target_files:
        p = repo_root / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            if rel.endswith(".gd"):
                p.write_text("extends RefCounted\n")
            elif rel.endswith(".json"):
                p.write_text("{}\n")


def enqueue_task_to_forge(task: dict) -> tuple[bool, str]:
    slug = task["slug"]
    issue_num = task["github_issue"]
    existing_targets = [
        tf for tf in task["target_files"] if (Path(REPO_DIR) / tf).exists()
    ]
    if not existing_targets:
        existing_targets = ["game/core/run_controller.gd", "game/tests/run_tests.gd"]

    plan = {
        "title": f"[{slug}] {task['title']} (#{issue_num})",
        "objective": task["summary"],
        "origin_channel_id": AGORA_CHANNEL_ID,
        "origin_thread_id": None,
        "repo": REPO_DIR,
        "tier": 2,
        "touches_core_bridge": False,
        "context_fact_sheet": [
            f"GitHub Issue: #{issue_num} on {REPO} ({task['title']})",
            "Deterministic GDScript simulation rules live in game/core/ and UI rules live in game/ui/.",
            "All user-facing copy must use Loc.t() and be registered in game/localization/agora_strings.csv.",
        ],
        "shared_context": [
            f"Planned files for {slug}: {', '.join(task['target_files'])}",
            f"Reference Python implementation in agora/ where applicable and close #{issue_num} in PR body.",
        ],
        "chunks": [
            {
                "chunk_index": 0,
                "title": f"{slug}: {task['title']}",
                "repo": REPO_DIR,
                "worker_model": "gemini-4-carbon-high",
                "target_files": existing_targets,
                "target_symbols": [],
                "prohibited_files": [],
                "shared_context": [task["summary"]],
                "instructions": (
                    f"Implement {slug} (GitHub Issue #{issue_num}): {task['title']}.\n\n"
                    f"{task['summary']}\n\n"
                    f"Full target file list to create/update: {', '.join(task['target_files'])}.\n"
                    "Verify with the headless Godot test runner before committing."
                ),
                "verification_cmd": task["test_cmd"],
            }
        ],
    }
    plan_dir = Path("/workspace/data/forge_plans")
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / f"forge_plan_{slug.lower()}.json"
    plan_file.write_text(json.dumps(plan, indent=2))
    res = subprocess.run(
        ["python3", "/workspace/tools/forge.py", "enqueue", str(plan_file)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = (res.stdout + "\n" + res.stderr).strip()
    return (res.returncode == 0), out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--enqueue-now", action="store_true", help="Immediately enqueue next task if idle")
    args = ap.parse_args()

    if not QUEUE_PATH.exists():
        print("No agora_forge_queue.json found.")
        return

    data = json.loads(QUEUE_PATH.read_text())
    if sync_issue_states(data):
        QUEUE_PATH.write_text(json.dumps(data, indent=2))

    active_build = has_active_forge_build()
    if active_build:
        print(f"Active FORGE build in progress: {active_build}. Standing by.")
        return

    open_prs = get_open_prs()
    if open_prs and not args.enqueue_now:
        pr = open_prs[0]
        print(f"Open PR #{pr['number']} ({pr['title']}) is active on {REPO}. Review/land before next piece.")
        return

    next_task = next((t for t in data.get("tasks", []) if t.get("status") == "pending"), None)
    if not next_task:
        print("All structured tasks in agora_forge_queue.json are completed or in progress!")
        return

    ok, out = enqueue_task_to_forge(next_task)
    if ok:
        next_task["status"] = "in_progress"
        next_task["enqueued_at"] = time.time()
        QUEUE_PATH.write_text(json.dumps(data, indent=2))
        print(f"Enqueued {next_task['slug']} (#{next_task['github_issue']}) to #forge:\n{out}")
    else:
        print(f"Failed to enqueue {next_task['slug']}:\n{out}")
        sys.exit(1)


if __name__ == "__main__":
    main()
