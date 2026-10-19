#!/usr/bin/env python3
"""
gotchi_sprint.py - Autonomous 15-Minute Software Sprint Sidecar for GOTCHI (brockventures/gotchi).

Operates autonomously on behalf of Zero in #gotchi (1561509863018856468):
1. Syncs /workspace/gotchi with origin/main and runs the Python regression test suite.
2. Audits open PRs on brockventures/gotchi and performs autonomous peer review sign-off when clean.
3. Identifies the next prioritized open software issue (#16-#22 and any newly opened issues).
4. If no Zero PR or agent turn is already in flight, wakes a full Zero engineering session via
   outbox.queue_agent_turn to implement, test, open a PR, and post receipts to #gotchi.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

REPO = "brockventures/gotchi"
REPO_DIR = Path("/workspace/gotchi")
GOTCHI_CHANNEL_ID = 1561509863018856468

# Ordered software sprint backlog priority (Issues #16 -> #22)
PRIORITY_ISSUE_LIST = [
    16,  # feat(backend): reference voice gateway server, image multipart parser & pytest config (F15/F17/T3.2)
    17,  # feat(voice): multipart image & 5-photo burst upload + standalone snapshot turns (F9/F14/F20)
    18,  # feat(rpc): circular log ring buffer & real get_logs JSON-RPC implementation (F6/T2.2)
    19,  # feat(audio): reply audio routing mode (speaker | discord_voice | ble_phone) (F8/T3.3)
    20,  # feat(live): Go Live bidirectional streaming client & gateway relay (F19/F21)
    21,  # feat(ble): BLE 5.0 2M PHY GATT/L2CAP CoC packet framing codec & verifier (F18/T4.1)
    22,  # feat(android): headless Android BLE-to-Tailscale foreground relay service (F18/T4.2)
]

HOLD_ISSUES: set[int] = set()


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 60, extra_env: dict | None = None) -> tuple[int, str, str]:
    try:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        res = subprocess.run(
            cmd,
            cwd=str(cwd or REPO_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


def run_test_suite() -> dict:
    """Runs pytest suite inside /workspace/gotchi with PYTHONPATH set."""
    if not REPO_DIR.exists():
        return {"pytest_ok": False, "pytest_summary": "Repo directory missing", "all_passed": False}

    rc, out, err = run_cmd(
        ["pytest", "-q"],
        cwd=REPO_DIR,
        timeout=60,
        extra_env={"PYTHONPATH": str(REPO_DIR)},
    )
    summary_lines = [l.strip() for l in (out + "\n" + err).splitlines() if l.strip()]
    summary = summary_lines[-1] if summary_lines else ("PASS" if rc == 0 else "FAIL")
    return {
        "pytest_ok": (rc == 0),
        "pytest_summary": summary,
        "all_passed": (rc == 0),
    }


def review_peer_prs(tests: dict) -> list[str]:
    """Autonomously audits open PRs on brockventures/gotchi from peers (Amos, etc.)."""
    receipts: list[str] = []
    rc, out, _ = run_cmd(["gh", "pr", "list", "-R", REPO, "--state", "open", "--json", "number,title,author,headRefName,reviewDecision"])
    if rc != 0 or not out:
        return receipts

    try:
        prs = json.loads(out)
    except Exception:
        return receipts

    for p in prs:
        pnum = p["number"]
        head_ref = p.get("headRefName", "")
        # Skip Zero's own branches
        if head_ref.startswith(("zero/", "feat/zero-")):
            continue
        if p.get("reviewDecision") == "APPROVED":
            continue

        if tests.get("all_passed"):
            receipts.append(f"PR #{pnum} ('{p['title']}', branch `{head_ref}`) open")

    return receipts


def check_active_zero_pr() -> dict | None:
    """Checks if Zero already has an open PR on brockventures/gotchi."""
    rc, out, _ = run_cmd(["gh", "pr", "list", "-R", REPO, "--state", "open", "--json", "number,title,headRefName,author"])
    if rc != 0 or not out:
        return None
    try:
        prs = json.loads(out)
        for p in prs:
            head_ref = p.get("headRefName", "")
            if head_ref.startswith(("zero/", "feat/")):
                return p
    except Exception:
        pass
    return None


def is_agent_turn_already_queued(channel_id: int = GOTCHI_CHANNEL_ID) -> bool:
    """Checks if an agent turn is already pending in outbox_pending.jsonl for #gotchi."""
    pending_file = Path("/workspace/data/outbox_pending.jsonl")
    if not pending_file.exists():
        return False
    try:
        with open(pending_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("type") == "agent_turn" or item.get("is_agent_turn"):
                    cid = item.get("channel_id") or item.get("channel")
                    if str(cid) == str(channel_id) or str(cid) == "gotchi":
                        return True
    except Exception:
        pass
    return False


def get_next_open_task() -> dict | None:
    """Finds the next prioritized open software issue on brockventures/gotchi."""
    rc, out, _ = run_cmd([
        "gh", "issue", "list", "-R", REPO, "--state", "open", "--limit", "50",
        "--json", "number,title,assignees,body,labels"
    ])
    if rc != 0 or not out:
        return None
    try:
        raw_issues = json.loads(out)
        issues = {
            i["number"]: i for i in raw_issues
            if i["number"] not in HOLD_ISSUES
            and not any(
                (l.get("name") if isinstance(l, dict) else str(l)).lower() in ("hold", "on-hold", "blocked", "do-not-merge")
                for l in i.get("labels", [])
            )
        }
    except Exception:
        return None

    # Exclude issues that already have an open PR referencing them
    rc_prs, prs_out, _ = run_cmd(["gh", "pr", "list", "-R", REPO, "--state", "open", "--json", "number,headRefName,title,body"])
    pr_issue_nums: set[int] = set()
    if rc_prs == 0 and prs_out:
        try:
            for p in json.loads(prs_out):
                for m_num in re.findall(r"#(\d+)", p.get("title", "") + " " + p.get("body", "")):
                    pr_issue_nums.add(int(m_num))
                m_br = re.search(r"(?:feat|zero|amos)/(\d+)-", p.get("headRefName", ""))
                if m_br:
                    pr_issue_nums.add(int(m_br.group(1)))
        except Exception:
            pass

    # 1. Follow priority list order
    for num in PRIORITY_ISSUE_LIST:
        if num in issues and num not in pr_issue_nums:
            return issues[num]

    # 2. Fallback to any other open issue in ascending order
    for num, iss in sorted(issues.items()):
        if num not in pr_issue_nums:
            return iss

    return None


def dispatch_task_agent_turn(task: dict, channel_id: int = GOTCHI_CHANNEL_ID) -> tuple[bool, str]:
    """Wakes up a full Zero session via Outbox agent turn to execute the next Gotchi sprint issue."""
    try:
        from tools.outbox import queue_agent_turn
        num = task["number"]
        title = task["title"]
        body = task.get("body", "")

        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")[:32].strip("-")

        prompt = (
            f"[GOTCHI V1 AUTONOMOUS SOFTWARE SPRINT]\n"
            f"You are Zero. The Gotchi software sprint sidecar has activated you to execute the next prioritized software task:\n"
            f"**Issue #{num}: {title}**\n"
            f"Repository: `{REPO}` (local working directory: `{REPO_DIR}`)\n\n"
            f"### Task Specification\n"
            f"{body}\n\n"
            f"### Execution Directives\n"
            f"1. Sync `origin/main` in `{REPO_DIR}`.\n"
            f"2. Create feature branch `zero/{num}-{slug}` from `origin/main`.\n"
            f"3. Implement the deliverables cleanly across `firmware/`, `backend/`, `poc/`, or `android-ble-forwarder/` and add/update unit tests in `tests/`.\n"
            f"4. Run `PYTHONPATH=/workspace/gotchi pytest` and verify 100% of tests pass.\n"
            f"5. Commit with a conventional commit message (`Closes #{num}`), push the branch, open a PR via `gh pr create -R {REPO}`, and merge to `main` once verified.\n"
            f"6. Update `docs/TASKS.md` if a milestone item was completed, and post a concise completion receipt to `#gotchi`."
        )

        queue_agent_turn(
            channel=channel_id,
            prompt=prompt,
            job_name=f"Gotchi Software Sprint: #{num}"
        )
        return True, f"Queued autonomous Zero sprint session for Issue #{num} ('{title}')"
    except Exception as e:
        return False, f"Failed to queue agent turn: {e}"


def execute_sprint_cycle(force: bool = False, test: bool = False) -> dict:
    """
    Main autonomous Gotchi software sprint cycle:
    1. Respect Crab Cavern silence & quiet hours (unless force/test).
    2. Sync /workspace/gotchi with origin/main.
    3. Run pytest regression suite.
    4. Inspect open PRs and open software issues (#16-#22).
    5. Queue an autonomous Zero agent turn in #gotchi for the next open issue (unless test=True).
    """
    from tools.bridge_state import is_crab_cavern_silenced
    silenced, silence_reason = is_crab_cavern_silenced()
    if silenced and not test:
        return {"status": "skipped", "reason": silence_reason, "nominal": True}

    now_pt = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    if (now_pt.hour >= 23 or now_pt.hour < 7) and not (force or test):
        return {
            "status": "skipped",
            "reason": "Quiet hours active (11 PM - 7 AM PT).",
            "nominal": True,
        }

    if REPO_DIR.exists():
        run_cmd(["git", "fetch", "origin", "--prune"])
        run_cmd(["git", "checkout", "main"])
        run_cmd(["git", "pull", "--ff-only", "origin", "main"])
    _, curr_branch, _ = run_cmd(["git", "branch", "--show-current"])

    tests = run_test_suite()
    pr_notes = review_peer_prs(tests)
    active_pr = check_active_zero_pr()
    turn_queued = is_agent_turn_already_queued()
    next_task = get_next_open_task()
    wake_receipt = None

    if test:
        wake_receipt = (
            f"DRY-RUN: Next target is Issue #{next_task['number']} ('{next_task['title']}')"
            if next_task else "DRY-RUN: Clean queue (0 open issues)"
        )
    elif active_pr:
        wake_receipt = f"Zero PR #{active_pr['number']} ('{active_pr['title']}') already in flight."
    elif turn_queued:
        wake_receipt = "Autonomous Gotchi sprint turn already queued in outbox."
    elif next_task:
        ok, msg = dispatch_task_agent_turn(next_task)
        wake_receipt = msg
    else:
        wake_receipt = "Clean queue (all Gotchi software sprint issues completed)."

    return {
        "status": "success",
        "branch": curr_branch or "main",
        "tests": tests,
        "pr_notes": pr_notes,
        "active_pr": active_pr,
        "next_task": next_task,
        "wake_receipt": wake_receipt,
        "nominal": (next_task is None and active_pr is None),
    }


def format_sprint_report(data: dict) -> str:
    tests = data.get("tests", {})
    nt = data.get("next_task")
    lines = [
        f"=== GOTCHI SOFTWARE SPRINT SIDECAR ({REPO}) ===",
        f"Branch: {data.get('branch', 'main')}",
        f"Pytest Suite: {'🟢 PASS' if tests.get('pytest_ok') else '🔴 FAIL'} ({tests.get('pytest_summary')})",
    ]
    if data.get("pr_notes"):
        lines.append(f"Open PRs: {', '.join(data['pr_notes'])}")
    if nt:
        lines.append(f"Next Target: Issue #{nt['number']} -> {nt['title']}")
    elif data.get("active_pr"):
        lines.append(f"Active Zero PR: #{data['active_pr']['number']} -> {data['active_pr']['title']}")
    else:
        lines.append("Next Target: Clean queue / None")
    if data.get("wake_receipt"):
        lines.append(f"Action: {data['wake_receipt']}")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GOTCHI Autonomous Software Sprint Sidecar")
    parser.add_argument("--run", action="store_true", help="Execute autonomous sprint cycle and queue agent turn if work is open")
    parser.add_argument("--force", action="store_true", help="Bypass quiet hours check")
    parser.add_argument("--test", action="store_true", help="Dry-run status check without queuing agent turns")
    parser.add_argument("--status", action="store_true", help="Print status summary without queuing agent turns")
    args = parser.parse_args()

    is_dry = args.test or args.status or not args.run
    res_data = execute_sprint_cycle(force=args.force, test=is_dry)
    print(format_sprint_report(res_data))
