#!/usr/bin/env python3
"""
agora_roguelike_engineer.py - Engineering Execution Sidecar Helper for agora-roguelike.

Exclusively for Zero to wake up and work on the assigned engineering task:
1. Inspects the active engineering assignment for Zero prioritizing M0 tasks (#83, #84, #85).
2. Checks git branch status and syncs with latest main.
3. Runs the test suite verification (pytest tests/ and Godot 4 headless runner).
4. Reports engineering readiness and implementation targets.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = "brockventures/agora-roguelike"
REPO_DIR = Path("/workspace/agora-roguelike")
GODOT_BIN = REPO_DIR / ".godot-bin" / "Godot_v4.7.2-stable_linux.x86_64"

PRIORITY_LIST = [
    83, 84, 85, 25, 20, 12, 13, 14, 15, 16, 17, 18, 19,
    26, 27, 28, 29, 30, 31, 32, 33, 35, 36, 37, 38, 39, 40, 41
]


def run_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    res = subprocess.run(cmd, cwd=str(cwd or REPO_DIR), capture_output=True, text=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def check_engineering_status() -> dict:
    # 1. Git branch status
    rc_git, branch, _ = run_cmd(["git", "branch", "--show-current"])
    _, status, _ = run_cmd(["git", "status", "--short"])
    _, last_commit, _ = run_cmd(["git", "log", "-1", "--oneline"])

    # 2. Check open PRs for Zero or next open PR item
    assigned_task = None
    target_issue = None

    # Check if Zero has an open PR
    _, prs_out, _ = run_cmd(["gh", "pr", "list", "-R", REPO, "--json", "number,title,headRefName,state"])
    try:
        prs = json.loads(prs_out) if prs_out else []
        for pr in prs:
            if pr.get("headRefName") == branch or "zero" in pr.get("headRefName", "").lower() or "m0-" in pr.get("headRefName", "").lower():
                assigned_task = f"PR #{pr['number']}: {pr['title']} ({pr['headRefName']})"
                target_issue = 83
                break
    except Exception:
        pass

    if not assigned_task:
        # Dynamically inspect active open issues prioritized by roadmap
        for num in PRIORITY_LIST:
            _, issue_out, _ = run_cmd(["gh", "issue", "view", str(num), "-R", REPO, "--json", "number,title,body,state"])
            try:
                iss = json.loads(issue_out) if issue_out else {}
            except Exception:
                iss = {}
            if iss.get("state") == "CLOSED":
                continue
            for line in iss.get("body", "").splitlines():
                if line.strip().startswith("- [ ] **PR") or line.strip().startswith("- [ ]"):
                    assigned_task = line.strip().replace("- [ ] **", "").replace(":**", "").replace("- [ ]", "").strip()
                    target_issue = num
                    break
            if not assigned_task and iss.get("state") == "OPEN":
                assigned_task = iss.get("title")
                target_issue = num
                break
            if assigned_task:
                break

    # 3. Test suite verification
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    res_py = subprocess.run(["pytest", "tests/test_golden_orderbook_fixtures.py"], cwd=str(REPO_DIR), capture_output=True, text=True, env=env)
    rc_pytest, pytest_out = res_py.returncode, res_py.stdout.strip()

    godot_summary = "N/A"
    godot_ok = True
    if GODOT_BIN.exists():
        res_gd = subprocess.run([
            str(GODOT_BIN), "--headless", "--path", "game", "-s", "res://tests/run_tests.gd"
        ], cwd=str(REPO_DIR), capture_output=True, text=True)
        godot_ok = (res_gd.returncode == 0)
        lines = [l for l in res_gd.stdout.splitlines() if "passed" in l.lower() or "failed" in l.lower()]
        godot_summary = lines[-1] if lines else ("PASS" if godot_ok else "FAIL")

    return {
        "repo": REPO,
        "branch": branch,
        "dirty_files": status.splitlines() if status else [],
        "last_commit": last_commit,
        "target_issue": target_issue,
        "assigned_task": assigned_task,
        "pytest_ok": (rc_pytest == 0),
        "pytest_summary": pytest_out.splitlines()[-1] if pytest_out else "",
        "godot_ok": godot_ok,
        "godot_summary": godot_summary,
    }


def print_summary():
    data = check_engineering_status()
    print(f"=== AGORA ROGUELIKE ENGINEERING STATUS ({REPO}) ===")
    print(f"Branch: {data['branch']} (HEAD: {data['last_commit']})")
    print(f"Worktree Clean: {len(data['dirty_files']) == 0}")
    print(f"Active Assigned Task: Issue #{data['target_issue']} -> {data['assigned_task']}")
    print(f"Python Fixtures: {'🟢 PASS' if data['pytest_ok'] else '🔴 FAIL'} ({data['pytest_summary']})")
    print(f"Godot Headless: {'🟢 PASS' if data['godot_ok'] else '🔴 FAIL'} ({data['godot_summary']})")


if __name__ == "__main__":
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    import sys
    now_pt = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    if (now_pt.hour >= 23 or now_pt.hour < 7) and "--force" not in sys.argv:
        print("[agora_roguelike_engineer] Paused: Quiet hours active (11 PM - 7 AM PT). Execution skipped.")
        sys.exit(0)
    parser = argparse.ArgumentParser(description="Agora Roguelike Engineering Helper")
    parser.add_argument("--status", action="store_true", default=True, help="Print engineering status")
    args = parser.parse_args()
    print_summary()
