#!/usr/bin/env python3
"""Shared Public Task Tracker for Crab Cavern Robots.

Interfaces directly with GitHub Issues and Project Board on `brockventures/market-sandbox`.
Keeps shared robot collaboration strictly segregated from private homelab/Google Tasks.
"""

import argparse
import json
import subprocess
import sys
from typing import List, Optional

REPO = "brockventures/market-sandbox"
PROJECT_ID = "PVT_kwHODXND-s4BjPvN"

def run_gh(args: List[str]) -> str:
    cmd = ["gh"] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"gh command failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()

def list_tasks(state: str = "open", agent: Optional[str] = None, label: Optional[str] = None) -> List[dict]:
    args = ["issue", "list", "-R", REPO, "--state", state, "--json", "number,title,labels,assignees,url,state"]
    if agent:
        args.extend(["--label", f"agent:{agent}"])
    if label:
        args.extend(["--label", label])
    out = run_gh(args)
    return json.loads(out) if out else []

def create_task(title: str, body: str = "", labels: Optional[List[str]] = None, agent: Optional[str] = None, priority: str = "p1", kind: str = "spec") -> dict:
    all_labels = set(labels or [])
    if agent:
        all_labels.add(f"agent:{agent}")
    if priority:
        all_labels.add(f"priority:{priority}")
    if kind:
        all_labels.add(f"kind:{kind}")

    args = ["issue", "create", "-R", REPO, "--title", title, "--body", body]
    if all_labels:
        args.extend(["--label", ",".join(all_labels)])

    issue_url = run_gh(args)
    issue_number = issue_url.rstrip("/").split("/")[-1]

    # Fetch issue ID and link to project
    try:
        issue_data = json.loads(run_gh(["issue", "view", issue_number, "-R", REPO, "--json", "id,number,title,url"]))
        mutation = f"""
        mutation {{
          addProjectV2ItemById(input: {{
            projectId: "{PROJECT_ID}",
            contentId: "{issue_data['id']}"
          }}) {{
            item {{ id }}
          }}
        }}
        """
        subprocess.run(["gh", "api", "graphql", "-f", f"query={mutation}"], capture_output=True, text=True)
        return issue_data
    except Exception:
        return {"number": issue_number, "url": issue_url, "title": title}

def close_task(issue_number: int, comment: Optional[str] = None) -> None:
    args = ["issue", "close", str(issue_number), "-R", REPO]
    if comment:
        args.extend(["--comment", comment])
    run_gh(args)

def main():
    parser = argparse.ArgumentParser(description="Crab Cavern Shared Task Tracker")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # list
    list_p = subparsers.add_parser("list", help="List shared robot tasks")
    list_p.add_argument("--agent", choices=["zero", "amos", "marvin", "aerial"], help="Filter by agent label")
    list_p.add_argument("--label", help="Filter by arbitrary label")
    list_p.add_argument("--all", action="store_true", help="Include closed issues")

    # add
    add_p = subparsers.add_parser("add", help="Create a shared task")
    add_p.add_argument("--title", required=True, help="Task title")
    add_p.add_argument("--body", default="", help="Task description")
    add_p.add_argument("--agent", choices=["zero", "amos", "marvin", "aerial"], help="Assigned agent")
    add_p.add_argument("--priority", default="p1", choices=["p0", "p1", "p2", "p3"])
    add_p.add_argument("--kind", default="spec", choices=["spec", "engine", "test", "bug"])

    # close
    close_p = subparsers.add_parser("close", help="Close a shared task")
    close_p.add_argument("number", type=int, help="Issue number")
    close_p.add_argument("--comment", help="Closing comment")

    args = parser.parse_args()

    if args.action == "list":
        state = "all" if args.all else "open"
        tasks = list_tasks(state=state, agent=args.agent, label=args.label)
        if not tasks:
            print("No shared tasks found matching criteria.")
            return
        for t in tasks:
            labels_str = ", ".join(l["name"] for l in t.get("labels", []))
            print(f"#{t['number']}: {t['title']} [{labels_str}] -> {t['url']}")

    elif args.action == "add":
        task = create_task(title=args.title, body=args.body, agent=args.agent, priority=args.priority, kind=args.kind)
        print(f"Created task #{task.get('number')}: {task.get('title')} ({task.get('url')})")

    elif args.action == "close":
        close_task(args.number, comment=args.comment)
        print(f"Closed task #{args.number}")

if __name__ == "__main__":
    main()
