#!/usr/bin/env python3
"""
Periodic Sidecar Ticker for #agora (1558202642663211169) piecewise FORGE builds.

Checks:
1. Auto-syncs local `main` commits (merged by FORGE) to `origin/main` and closes completed GitHub issues.
2. Checks whether an active FORGE build is currently running/queued for agora-roguelike (and heals orphaned builds after container restarts).
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


def heal_and_check_active_forge_build() -> str | None:
    if not FORGE_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(FORGE_DB))
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, status, updated_at FROM forge_builds "
            "WHERE status IN ('queued', 'running', 'verifying', 'pending_approval') "
            "AND (repo LIKE '%agora-roguelike%' OR title LIKE '%AGORA-F%') "
            "ORDER BY created_at DESC LIMIT 5"
        )
        rows = cur.fetchall()
        now = time.time()
        active_label = None
        for bid, title, status, updated_at in rows:
            age = now - float(updated_at or now)
            # Check if a running build was orphaned by a container restart (>25 min stale or missing worktree)
            wt_exists = any(Path("/tmp").glob(f"forge-wt-{bid}*"))
            if status == "running" and (age > 1500 or (age > 180 and not wt_exists)):
                cur.execute("UPDATE forge_builds SET status = 'failed' WHERE id = ?", (bid,))
                cur.execute(
                    "UPDATE forge_chunks SET status = 'failed', last_error = 'Orphaned or timed out; auto-healed by ticker' WHERE build_id = ?",
                    (bid,),
                )
                conn.commit()
                continue
            if active_label is None:
                active_label = f"{bid} ({title} — {status})"
        conn.close()
        return active_label
    except Exception as exc:
        print(f"DB check error: {exc}", file=sys.stderr)
        return None


def sync_merged_commits_and_issues(data: dict) -> bool:
    changed = False
    # Check git log on local main for merged AGORA-F* commits
    log_res = subprocess.run(
        ["git", "-C", REPO_DIR, "log", "-n", "25", "--oneline", "main"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    log_text = log_res.stdout if log_res.returncode == 0 else ""

    # Push local main if ahead of origin/main
    subprocess.run(
        ["git", "-C", REPO_DIR, "push", "origin", "main"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    res = subprocess.run(
        ["gh", "issue", "list", "-R", REPO, "--state", "all", "--limit", "100", "--json", "number,state"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    state_map = {}
    if res.returncode == 0 and res.stdout.strip():
        state_map = {i["number"]: i["state"] for i in json.loads(res.stdout)}

    for t in data.get("tasks", []):
        slug = t.get("slug", "")
        inum = t.get("github_issue")
        if slug and f"{slug}:" in log_text:
            if inum and state_map.get(inum) == "OPEN":
                subprocess.run(
                    ["gh", "issue", "close", str(inum), "-R", REPO, "--comment", f"Completed and merged ({slug})."],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                state_map[inum] = "CLOSED"
            if t.get("status") != "completed":
                t["status"] = "completed"
                changed = True
        elif inum in state_map and state_map[inum] == "CLOSED" and t.get("status") != "completed":
            t["status"] = "completed"
            changed = True

    return changed


def enqueue_task_to_forge(task: dict) -> tuple[bool, str]:
    slug = task["slug"]
    issue_num = task["github_issue"]
    existing_targets = [
        tf for tf in task["target_files"]
        if (Path(REPO_DIR) / tf).exists() and tf != "game/localization/agora_strings.csv"
    ]
    if not existing_targets:
        existing_targets = ["game/core/run_controller.gd", "game/tests/run_tests.gd"]

    plan_dir = Path("/workspace/data/forge_plans")
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / f"forge_plan_{slug.lower()}.json"

    # Use custom pre-authored plan file if already present on disk for AGORA-F5
    if slug == "AGORA-F5" and plan_file.exists():
        plan = json.loads(plan_file.read_text())
    else:
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
                "Do NOT add unreferenced keys to game/localization/agora_strings.csv (test_localization.gd enforces strict 1:1 usage).",
            ],
            "shared_context": [
                f"Planned files for {slug}: {', '.join(existing_targets)}",
            ],
            "chunks": [
                {
                    "chunk_index": 0,
                    "title": f"{slug}: {task['title']}",
                    "repo": REPO_DIR,
                    "worker_model": "gemini-4-carbon-high",
                    "target_files": existing_targets[:6],
                    "target_symbols": [],
                    "prohibited_files": ["game/localization/agora_strings.csv"],
                    "shared_context": [task["summary"]],
                    "instructions": (
                        f"Implement {slug} (GitHub Issue #{issue_num}): {task['title']}.\n\n"
                        f"{task['summary']}\n\n"
                        f"Target files to update: {', '.join(existing_targets[:6])}.\n"
                        "Keep changes concise and verify with the headless Godot test runner before committing."
                    ),
                    "verification_cmd": task["test_cmd"],
                }
            ],
        }
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
    if not QUEUE_PATH.exists():
        print("No agora_forge_queue.json found.")
        return

    data = json.loads(QUEUE_PATH.read_text())
    changed = sync_merged_commits_and_issues(data)

    active_build = heal_and_check_active_forge_build()
    if active_build:
        if changed:
            QUEUE_PATH.write_text(json.dumps(data, indent=2))
        print(f"Active FORGE build in progress: {active_build}. Standing by.")
        return

    # If no active build is running, any task still marked "in_progress" was interrupted/failed and should be retried
    for t in data.get("tasks", []):
        if t.get("status") == "in_progress":
            t["status"] = "pending"
            changed = True

    if changed:
        QUEUE_PATH.write_text(json.dumps(data, indent=2))

    next_task = next((t for t in data.get("tasks", []) if t.get("status") == "pending"), None)
    if not next_task:
        print("All structured tasks in agora_forge_queue.json are completed!")
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
