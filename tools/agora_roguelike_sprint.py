#!/usr/bin/env python3
"""
agora_roguelike_sprint.py - Autonomous 15-Minute Sprint Coordinator for agora-roguelike.
Queries open issues, PR statuses, and next unassigned/open atomic PR checklist items
on brockventures/agora-roguelike.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = "brockventures/agora-roguelike"
REPO_DIR = Path("/workspace/agora-roguelike")

def run_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    res = subprocess.run(cmd, cwd=str(cwd or REPO_DIR), capture_output=True, text=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()

def check_status() -> dict:
    # 1. Open PRs
    _, prs_out, _ = run_cmd(["gh", "pr", "list", "-R", REPO, "--json", "number,title,headRefName,state,reviewDecision"])
    try:
        prs = json.loads(prs_out) if prs_out else []
    except Exception:
        prs = []

    # 2. Key issues (#3, #4, #5)
    issues_data = {}
    for num in [3, 4, 5]:
        _, issue_out, _ = run_cmd(["gh", "issue", "view", str(num), "-R", REPO, "--json", "number,title,state,body"])
        try:
            issues_data[num] = json.loads(issue_out) if issue_out else {}
        except Exception:
            issues_data[num] = {}

    return {"prs": prs, "issues": issues_data}

def print_summary():
    data = check_status()
    print(f"=== AGORA ROGUELIKE SPRINT STATUS ({REPO}) ===")
    print(f"Open PRs: {len(data['prs'])}")
    for pr in data['prs']:
        print(f"  - PR #{pr['number']}: {pr['title']} ({pr['headRefName']}) [{pr.get('reviewDecision') or 'NO REVIEW'}]")

    print("\nAssigned Work Items:")
    for num, iss in data['issues'].items():
        if not iss:
            continue
        print(f"  - Issue #{num}: {iss.get('title')} [{iss.get('state')}]")
        body = iss.get('body', '')
        for line in body.splitlines():
            if line.strip().startswith("- [ ]") or line.strip().startswith("- [x]"):
                print(f"      {line.strip()[:100]}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agora Roguelike Sprint Helper")
    parser.add_argument("--status", action="store_true", default=True, help="Print sprint status summary")
    args = parser.parse_args()
    print_summary()
