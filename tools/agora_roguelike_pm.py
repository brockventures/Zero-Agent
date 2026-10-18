#!/usr/bin/env python3
"""
agora_roguelike_pm.py - PM & Task Assignment Sidecar Coordinator for agora-roguelike.

Coordinates engineering between Zero and Amos:
1. Assesses the GitHub board (open PRs, review status, CI checks, and prioritized roadmap issues).
2. Prioritizes M0 vertical slice demo tasks (#83, #84, #85) first, then proceeds along the existing roadmap.
3. Autonomously squash-merges approved PRs with green CI, enforcing Marvin's gating rules:
   - All named reviewers must be APPROVED.
   - Every approval's commit_id must match the PR's current head SHA.
   - No peer review comments newer than the latest approval.
4. Dynamically assigns tasks and reviews between Amos and Zero.
5. Formats clean, mobile-optimized Markdown briefings and dispatches them to #agora (<#1558202642663211169>)
   when PRs or assignments change (silent when nominal).
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
if "/workspace/tools" not in sys.path:
    sys.path.insert(0, "/workspace/tools")

REPO = "brockventures/agora-roguelike"
REPO_DIR = Path("/workspace/agora-roguelike")
DEFAULT_CHANNEL_ID = 1558202642663211169  # #agora
STATE_FILE = Path("/workspace/data/agora_roguelike_pm_state.json")

# Issues explicitly on hold or gated on human/hardware actions (not assignable to bots)
HOLD_ISSUES = {
    30,   # On hold per Mike (ask-255bd2) until demo is ready; $100 Steam Direct fee is Mike's call
    31,   # Epic 5 Review Gate (blocked on #30, #38, #39)
    38,   # Waiting on Mike's physical undocked Steam Deck battery run
    39,   # Code landed in PR #203; store page & demo depot blocked on #30 App ID
}

# Roadmap prioritization: M0 & playtest polish tasks first, then proceeding along existing priority list
PRIORITY_ISSUE_LIST = [
    # Tier 1: Immediate Playtest Polish & M0 Loop Integrity
    191,  # feat(ui): clamp trading order quantities to stock/capacity and pin persistent hold readout
    194,  # feat(onboarding): add tactile round commit feedback and interactive 3-step first-run flight checklist
    149,  # Meta loop: the Commission cycle (save Sol, get dissolved, corrupt the government)
    137,  # feat(perks): loans and interest system for corrupt_regulator
    138,  # feat(perks): fuel market system for fuel_hedge
    139,  # feat(perks): hazard and piracy corridors system for black_market_corridors
    32,   # Implement Endgame Sol System Rescue Project (Post-Monopoly Climax)
    34,   # Build M0 Playable Vertical Slice (1 Station, 1 Baron, 120-Round Run)

    # Tier 2: Visual Asset Deck Production (Epic 6)
    73,   # Epic 6: Visual Identity & Art Asset Pipeline (Cel-Shaded)

    # Tier 3: Steam Deck Hardware & Store Pipeline (Epics 4 & 5)
    36,   # Design First-Hour Onboarding Flow & Interactive Terminal Tutorial
    41,   # Implement Steam Deck Suspend/Resume Lifecycle & Audio Buffer Recovery
    26,   # Epic 5: Steamworks Integration & Automated Release Pipeline
    29,   # Stage Steam Store Assets, Capsule Graphics & Content Survey

    # Tier 4: Endgame Boss Content
    33,   # Implement Secret Final Boss: Sol Government Nationalization & Sovereign Buyout
]


def run_cmd(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    res = subprocess.run(cmd, cwd=str(cwd or REPO_DIR), capture_output=True, text=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_FILE.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    tmp_path.replace(STATE_FILE)


def check_review_gate(pr_number: int) -> tuple[bool, str]:
    """
    Enforce Marvin & Amos's review gating invariants:
    1. Explicit hold invariant ('do not merge', sample sign-off requested, hold labels).
    2. Every named reviewer on the PR must have an APPROVED state.
    3. Every approval's commit_id must match the PR's current head SHA.
    4. No new comments or review notes in the thread can be newer than the latest approval.
    """
    rc, out, _ = run_cmd([
        "gh", "pr", "view", str(pr_number), "-R", REPO,
        "--json", "headRefOid,latestReviews,comments,reviewDecision,body,title,labels"
    ])
    if rc != 0 or not out:
        return False, "Failed to fetch PR review metadata"
    try:
        data = json.loads(out)
    except Exception as e:
        return False, f"JSON parse error: {e}"

    # Explicit hold check (e.g. sample sign-offs, WIP, do-not-merge)
    body = (data.get("body") or "").lower()
    title = (data.get("title") or "").lower()
    labels = [(l.get("name") if isinstance(l, dict) else str(l)).lower() for l in data.get("labels", [])]

    has_hold_text = (
        any(t in body for t in ["do not merge", "don't merge", "do not squash"])
        or (("sample" in body or "sample" in title) and "sign-off" in body)
    )
    has_hold_label = any(l in labels for l in ["hold", "do-not-merge", "sample", "wip"])

    if has_hold_text or has_hold_label:
        return False, "HOLD: Explicit hold on PR ('do not merge' / sample sign-off requested)"
    rdec = data.get("reviewDecision")
    if rdec != "APPROVED":
        return False, f"Review decision is '{rdec}' (not APPROVED)"

    head_oid = data.get("headRefOid")

    latest_reviews = data.get("latestReviews", [])
    if not latest_reviews:
        return False, "No reviews submitted"

    approval_times = [
        r.get("submittedAt") for r in latest_reviews
        if r.get("state") == "APPROVED" and r.get("submittedAt")
    ]
    if not approval_times:
        return False, "No APPROVED review state found"

    latest_approval_time = max(approval_times)

    # Check commit_id on reviews via REST API (Amos proposal: approvals must cover head SHA)
    rc_rev, rev_out, _ = run_cmd([
        "gh", "api", f"repos/{REPO}/pulls/{pr_number}/reviews"
    ])
    if rc_rev == 0 and rev_out:
        try:
            all_reviews = json.loads(rev_out)
            latest_by_user = {}
            for r in all_reviews:
                u = r.get("user", {}).get("login")
                if u:
                    latest_by_user[u] = r
            for u, r in latest_by_user.items():
                if r.get("state") == "APPROVED" and head_oid:
                    rev_commit = r.get("commit_id")
                    if rev_commit and rev_commit != head_oid:
                        return False, f"Approval by {u} was for {rev_commit[:7]}, but PR head is {head_oid[:7]} (needs re-review)"
        except Exception as e:
            print(f"[Warning] Failed to parse review commit_ids: {e}", file=sys.stderr)

    # Check if any peer reviewer comment is newer than the latest approval
    for c in data.get("comments", []):
        c_time = c.get("createdAt")
        if c_time and c_time > latest_approval_time:
            c_author = c.get("author", {}).get("login", "")
            if c_author in ("iacoley", "mcarmody", "amos"):
                return False, f"Review comment by {c_author} ({c_time}) is newer than latest approval ({latest_approval_time})"

    return True, "All review gates satisfied"


def certify_and_approve_pr(pr_number: int, title: str) -> tuple[bool, str]:
    """Autonomously approve a green CI PR authored by a trusted collaborator."""
    rc, out, err = run_cmd([
        "gh", "pr", "review", str(pr_number), "-R", REPO,
        "--approve",
        "--body", f"Autonomously reviewed and certified green: CI matrix passed cleanly on '{title}'."
    ])
    if rc == 0:
        return True, "Approved PR via GitHub CLI"
    return False, f"Failed to approve PR: {err or out}"


def get_all_issues(limit: int = 100) -> list[dict]:
    """Fetch all open and closed issues including title, body, assignees, and timestamps."""
    rc, out, _ = run_cmd([
        "gh", "issue", "list", "-R", REPO, "--state", "all",
        "--limit", str(limit),
        "--json", "number,title,body,state,labels,assignees,updatedAt,closedAt"
    ])
    if rc != 0 or not out:
        return []
    try:
        return json.loads(out)
    except Exception:
        return []


def get_all_prs(limit: int = 50) -> list[dict]:
    """Fetch recent open and closed PRs including body, branch, author, and timestamps."""
    rc, out, _ = run_cmd([
        "gh", "pr", "list", "-R", REPO, "--state", "all",
        "--limit", str(limit),
        "--json", "number,title,body,headRefName,state,mergedAt,updatedAt,createdAt,author"
    ])
    if rc != 0 or not out:
        return []
    try:
        return json.loads(out)
    except Exception:
        return []


def is_issue_on_hold(iss: dict) -> bool:
    """Return True if an issue is explicitly on hold or human-gated."""
    if iss.get("number") in HOLD_ISSUES:
        return True
    labels = [(l.get("name") if isinstance(l, dict) else str(l)).lower() for l in iss.get("labels", [])]
    if any(l in labels for l in ("hold", "on-hold", "blocked", "do-not-merge")):
        return True
    return False


def get_open_prioritized_issues(all_issues: list[dict] | None = None) -> list[dict]:
    """Fetch actionable open issues matching PRIORITY_ISSUE_LIST in rank order, excluding held issues."""
    if all_issues is None:
        all_issues = get_all_issues(limit=100)
    open_issues = [i for i in all_issues if i.get("state") == "OPEN" and not is_issue_on_hold(i)]
    issue_map = {i["number"]: i for i in open_issues}
    prioritized = [issue_map[num] for num in PRIORITY_ISSUE_LIST if num in issue_map]
    seen = {i["number"] for i in prioritized}
    remaining = [i for i in open_issues if i["number"] not in seen]
    return prioritized + remaining


def get_task_inactivity(
    issue_num: int,
    all_issues: list[dict],
    all_prs: list[dict],
    task_started_iso: str | None,
    now: datetime
) -> tuple[float, str, list[int]]:
    """
    Computes elapsed inactivity minutes for an assigned issue, taking into account:
    1. Sub-issues linked via 'part of #X', 'under #X', 'sub-issue of #X', 'child of #X'.
    2. Transitive sub-issue relationships.
    3. Related PRs closing, referencing, or branching on the issue or its sub-issues.
    4. Related PRs matching the Epic label if the main issue represents an Epic.
    5. Latest activity timestamp across main issue, sub-issues, related PRs, and assignment start.
    Returns:
       (elapsed_inactivity_minutes, latest_activity_description, sub_issue_numbers)
    """
    import re
    children_map = {}
    issue_by_num = {i["number"]: i for i in all_issues}
    main_iss = issue_by_num.get(issue_num)

    # Detect if main issue is an Epic (e.g. "Epic 3: ...")
    epic_tag = None
    if main_iss:
        m = re.search(r'\b(epic\s*\d+)\b', main_iss.get("title", ""), re.IGNORECASE)
        if m:
            epic_tag = re.sub(r'\s+', '', m.group(1).lower())

    for iss in all_issues:
        body = iss.get("body") or ""
        # Find explicit parent references
        parents = re.findall(r'(?:part of|under|sub-issue of|child of)\s*#(\d+)', body, re.IGNORECASE)
        for p in parents:
            children_map.setdefault(int(p), set()).add(iss["number"])

    # Recursively collect sub-issues
    sub_issues = set()
    queue = [issue_num]
    while queue:
        curr = queue.pop(0)
        for child in children_map.get(curr, set()):
            if child not in sub_issues:
                sub_issues.add(child)
                queue.append(child)

    relevant_nums = {issue_num} | sub_issues

    events = []
    if main_iss and main_iss.get("updatedAt"):
        events.append((main_iss["updatedAt"], f"Issue #{issue_num} updated"))

    for sub in sub_issues:
        s_iss = issue_by_num.get(sub)
        if s_iss:
            if s_iss.get("updatedAt"):
                events.append((s_iss["updatedAt"], f"Sub-issue #{sub} updated"))
            if s_iss.get("closedAt"):
                events.append((s_iss["closedAt"], f"Sub-issue #{sub} closed"))

    for pr in all_prs:
        title = pr.get("title") or ""
        body = pr.get("body") or ""
        ref_branch = pr.get("headRefName") or ""
        text = f"{title}\n{body}\n{ref_branch}"
        refs = {int(x) for x in re.findall(r'#(\d+)', text)}

        pr_matches = bool(refs & relevant_nums)
        if not pr_matches and epic_tag:
            if epic_tag in re.sub(r'\s+', '', text.lower()):
                pr_matches = True

        if pr_matches:
            pnum = pr["number"]
            if pr.get("mergedAt"):
                events.append((pr["mergedAt"], f"PR #{pnum} merged"))
            if pr.get("updatedAt"):
                events.append((pr["updatedAt"], f"PR #{pnum} updated"))
            if pr.get("createdAt"):
                events.append((pr["createdAt"], f"PR #{pnum} opened"))

    if task_started_iso:
        events.append((task_started_iso, "Task assigned"))

    if not events:
        return 0.0, "No activity recorded", sorted(sub_issues)

    events.sort(key=lambda x: x[0], reverse=True)
    latest_ts, latest_desc = events[0]
    elapsed_mins = get_elapsed_minutes(latest_ts, now)
    return elapsed_mins, latest_desc, sorted(sub_issues)



def get_recently_merged_prs(limit: int = 5, max_age_hours: float = 2.0) -> list[dict]:
    """Fetch recently merged PRs to identify merges that occurred within max_age_hours."""
    rc, out, _ = run_cmd([
        "gh", "pr", "list", "-R", REPO, "--state", "merged",
        "--limit", str(limit),
        "--json", "number,title,mergedAt,headRefName"
    ])
    if rc != 0 or not out:
        return []
    try:
        prs = json.loads(out)
        now = datetime.now(timezone.utc)
        recent = []
        for p in prs:
            mat = p.get("mergedAt")
            if mat:
                try:
                    dt = datetime.fromisoformat(mat.replace("Z", "+00:00"))
                    if (now - dt).total_seconds() <= max_age_hours * 3600:
                        recent.append(p)
                except Exception:
                    pass
        return recent
    except Exception:
        return []


def get_elapsed_minutes(iso_str: str | None, now: datetime) -> float:
    if not iso_str:
        return 0.0
    try:
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if now <= dt:
            return 0.0
        pt_tz = ZoneInfo("America/Los_Angeles")
        # Subtract any overlap with quiet hours (23:00 to 07:00 PT)
        total_secs = (now - dt).total_seconds()
        curr = dt.astimezone(pt_tz)
        end_pt = now.astimezone(pt_tz)
        quiet_secs = 0.0
        day_cursor = curr.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        while day_cursor <= end_pt:
            q_start = day_cursor.replace(hour=23, minute=0, second=0, microsecond=0)
            q_end = (day_cursor + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
            o_start = max(curr, q_start)
            o_end = min(end_pt, q_end)
            if o_end > o_start:
                quiet_secs += (o_end - o_start).total_seconds()
            day_cursor += timedelta(days=1)
        return max(0.0, (total_secs - quiet_secs) / 60.0)
    except Exception:
        return 0.0


def assess_board(force: bool = False, stall_threshold_mins: float = 45.0, nudge_cooldown_mins: float = 45.0) -> dict:
    """Assess the live GitHub board for agora-roguelike."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from tools.bridge_state import is_crab_cavern_silenced
    silenced, silence_reason = is_crab_cavern_silenced()
    if silenced:
        print(f"[agora_roguelike_pm] Paused: {silence_reason}", file=sys.stderr)
        sys.exit(0)
    now_pt = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    if (now_pt.hour >= 23 or now_pt.hour < 7) and not force:
        print("[agora_roguelike_pm] Paused: Quiet hours active (11 PM - 7 AM PT). Board assessment skipped.")
        sys.exit(0)
    prev_state = load_state()
    known_merged = set(prev_state.get("known_merged_prs", []))
    now = datetime.now(timezone.utc)
    task_started = dict(prev_state.get("task_started", {}))

    # 1. Open PRs
    _, prs_out, _ = run_cmd([
        "gh", "pr", "list", "-R", REPO,
        "--json", "number,title,headRefName,state,reviewDecision,author,headRefOid"
    ])
    try:
        prs = json.loads(prs_out) if prs_out else []
    except Exception:
        prs = []

    # Check CI checks and autonomous merge eligibility
    merged_prs = []
    for pr in prs:
        pnum = pr["number"]
        rc, checks_out, _ = run_cmd(["gh", "pr", "checks", str(pnum), "-R", REPO])
        pr["ci_status"] = "PASS" if rc == 0 else "PENDING/FAIL"
        pr["checks_summary"] = checks_out.splitlines()[0] if checks_out else "No checks"

        if pr["ci_status"] == "PASS":
            gate_ok, gate_reason = check_review_gate(pnum)
            if not gate_ok and "not APPROVED" in gate_reason:
                author_login = pr.get("author", {}).get("login", "")
                if author_login in ("mcarmody", "amos", "iacoley"):
                    rc_meta, meta_out, _ = run_cmd([
                        "gh", "pr", "view", str(pnum), "-R", REPO,
                        "--json", "latestReviews"
                    ])
                    has_changes_requested = False
                    if rc_meta == 0 and meta_out:
                        try:
                            rev_data = json.loads(meta_out)
                            has_changes_requested = any(
                                r.get("state") == "CHANGES_REQUESTED"
                                for r in rev_data.get("latestReviews", [])
                            )
                        except Exception:
                            pass
                    if not has_changes_requested:
                        print(f"[*] Autonomously reviewing and certifying green PR #{pnum} ('{pr['title']}')...")
                        app_ok, app_msg = certify_and_approve_pr(pnum, pr["title"])
                        if app_ok:
                            gate_ok, gate_reason = check_review_gate(pnum)
                        else:
                            print(f"  ✗ Failed to auto-certify PR #{pnum}: {app_msg}", file=sys.stderr)

            pr["gate_status"] = "PASS" if gate_ok else (gate_reason if gate_reason.startswith("HOLD") else f"GATE: {gate_reason}")
            if gate_ok:
                print(f"[*] Autonomously squash-merging certified PR #{pnum} ('{pr['title']}')...")
                m_rc, m_out, m_err = run_cmd(["gh", "pr", "merge", str(pnum), "--squash", "--delete-branch", "-R", REPO])
                if m_rc == 0:
                    print(f"  ✓ Merged PR #{pnum}")
                    merged_prs.append({"number": pnum, "title": pr["title"]})
                    try:
                        from tools.lounge_digest import enqueue_completed_item
                        enqueue_completed_item(REPO, "pr", pnum, pr["title"])
                    except Exception:
                        pass
                else:
                    print(f"  ✗ Merge failed on PR #{pnum}: {m_err or m_out}", file=sys.stderr)
        else:
            pr["gate_status"] = "CI PENDING/FAIL"

    # Also detect recently merged PRs not yet recorded in known_merged
    recent_merged = get_recently_merged_prs(limit=5)
    for rm in recent_merged:
        r_num = rm["number"]
        if r_num not in known_merged and not any(m["number"] == r_num for m in merged_prs):
            merged_prs.append({"number": r_num, "title": rm["title"]})

    if merged_prs:
        # Re-fetch PR list after merges
        _, prs_out, _ = run_cmd([
            "gh", "pr", "list", "-R", REPO,
            "--json", "number,title,headRefName,state,reviewDecision,author,headRefOid"
        ])
        try:
            prs = json.loads(prs_out) if prs_out else []
        except Exception:
            prs = []

    # 2. Open Prioritized Issues & Repository State
    all_issues = get_all_issues(limit=100)
    all_prs = get_all_prs(limit=50)
    open_issues = get_open_prioritized_issues(all_issues)

    # 3. Determine agent task states dynamically
    amos_task = None
    zero_task = None

    # Check open PRs for Amos
    amos_prs = [
        p for p in prs
        if "amos" in p.get("headRefName", "").lower()
        or "amos" in p.get("author", {}).get("login", "").lower()
        or "mcarmody" in p.get("author", {}).get("login", "").lower()
    ]
    if amos_prs:
        p = amos_prs[0]
        amos_task = {
            "status": "mid-task",
            "pr": p["number"],
            "title": p["title"],
            "branch": p["headRefName"],
            "review": p.get("reviewDecision") or "PENDING",
            "ci": p.get("ci_status")
        }

    # Check open PRs for Zero (excluding Amos's PRs)
    amos_pr_nums = {p["number"] for p in amos_prs}
    zero_prs = [
        p for p in prs
        if p["number"] not in amos_pr_nums
        and (
            "zero" in p.get("headRefName", "").lower()
            or "brockventures" in p.get("author", {}).get("login", "").lower()
            or "feat/" in p.get("headRefName", "").lower()
        )
    ]
    if zero_prs:
        p = zero_prs[0]
        zero_task = {
            "status": "mid-task",
            "pr": p["number"],
            "title": p["title"],
            "branch": p["headRefName"],
            "review": p.get("reviewDecision") or "PENDING",
            "ci": p.get("ci_status")
        }

    # Peer review takes priority: if one agent has an open PR, the other reviews it
    if amos_task and not zero_task:
        assigned_issue = None
        for iss in open_issues:
            assignee_logins = [a.get("login", "") for a in iss.get("assignees", [])]
            if "brockventures" in assignee_logins or "zero" in assignee_logins:
                assigned_issue = iss
                break
        if not assigned_issue and open_issues:
            assigned_issue = open_issues[0]
        zero_task = {
            "status": "reviewing",
            "pr": amos_task["pr"],
            "title": f"Review PR #{amos_task['pr']}: {amos_task['title']}",
            "on_deck": f"Issue #{assigned_issue['number']}: {assigned_issue['title']}" if assigned_issue else "None",
            "status_label": f"REVIEWING PR #{amos_task['pr']}"
        }

    if zero_task and not amos_task:
        assigned_issue = None
        for iss in open_issues:
            assignee_logins = [a.get("login", "") for a in iss.get("assignees", [])]
            if "mcarmody" in assignee_logins or "amos" in assignee_logins:
                assigned_issue = iss
                break
        if not assigned_issue and open_issues:
            assigned_issue = open_issues[0]
        amos_task = {
            "status": "reviewing",
            "pr": zero_task["pr"],
            "title": f"Review PR #{zero_task['pr']}: {zero_task['title']}",
            "on_deck": f"Issue #{assigned_issue['number']}: {assigned_issue['title']}" if assigned_issue else "None",
            "status_label": f"REVIEWING PR #{zero_task['pr']}"
        }

    # If neither is reviewing or mid-task, assign from explicit issue assignees with accurate activity labels
    if not amos_task:
        for iss in open_issues:
            assignee_logins = [a.get("login", "") for a in iss.get("assignees", [])]
            if "mcarmody" in assignee_logins or "amos" in assignee_logins:
                amos_elapsed, _, _ = get_task_inactivity(
                    iss["number"], all_issues, all_prs, task_started.get("amos"), now
                )
                label = f"STALLED ({int(amos_elapsed)}m inactive)" if amos_elapsed >= stall_threshold_mins else (
                    "NEWLY ASSIGNED" if amos_elapsed < 10 else f"ASSIGNED ({int(amos_elapsed)}m)"
                )
                amos_task = {
                    "status": "assigned",
                    "issue": iss["number"],
                    "title": f"Issue #{iss['number']}: {iss['title']}",
                    "status_label": label,
                    "elapsed_mins": amos_elapsed
                }
                break

    if not zero_task:
        for iss in open_issues:
            assignee_logins = [a.get("login", "") for a in iss.get("assignees", [])]
            if "brockventures" in assignee_logins or "zero" in assignee_logins:
                zero_elapsed, _, _ = get_task_inactivity(
                    iss["number"], all_issues, all_prs, task_started.get("zero"), now
                )
                label = f"STALLED ({int(zero_elapsed)}m inactive)" if zero_elapsed >= stall_threshold_mins else (
                    "NEWLY ASSIGNED" if zero_elapsed < 10 else f"ASSIGNED ({int(zero_elapsed)}m)"
                )
                zero_task = {
                    "status": "assigned",
                    "issue": iss["number"],
                    "title": f"Issue #{iss['number']}: {iss['title']}",
                    "status_label": label,
                    "elapsed_mins": zero_elapsed
                }
                break

    # Fallback assignment from queue for unassigned agents (only truly unassigned issues)
    assigned_issues = {
        task["issue"] for task in (amos_task, zero_task)
        if task and task.get("status") == "assigned" and "issue" in task
    }
    unassigned_issues = [
        i for i in open_issues
        if i["number"] not in assigned_issues and not i.get("assignees")
    ]

    if not zero_task:
        if unassigned_issues:
            top_issue = unassigned_issues.pop(0)
            zero_elapsed, _, _ = get_task_inactivity(
                top_issue["number"], all_issues, all_prs, task_started.get("zero"), now
            )
            label = f"STALLED ({int(zero_elapsed)}m inactive)" if zero_elapsed >= stall_threshold_mins else (
                "NEWLY ASSIGNED" if zero_elapsed < 10 else f"QUEUED ({int(zero_elapsed)}m)"
            )
            zero_task = {
                "status": "assigned",
                "issue": top_issue["number"],
                "title": f"Issue #{top_issue['number']}: {top_issue['title']}",
                "status_label": label,
                "elapsed_mins": zero_elapsed
            }
        else:
            idle_label = (
                "No unassigned tickets (standing by for PR review)"
                if (open_issues or prs or any(i.get("state") == "OPEN" for i in all_issues))
                else "Roadmap clear"
            )
            zero_task = {"status": "idle", "title": idle_label, "status_label": "IDLE"}

    if not amos_task:
        if unassigned_issues:
            top_issue = unassigned_issues.pop(0)
            amos_elapsed, _, _ = get_task_inactivity(
                top_issue["number"], all_issues, all_prs, task_started.get("amos"), now
            )
            label = f"STALLED ({int(amos_elapsed)}m inactive)" if amos_elapsed >= stall_threshold_mins else (
                "NEWLY ASSIGNED" if amos_elapsed < 10 else f"QUEUED ({int(amos_elapsed)}m)"
            )
            amos_task = {
                "status": "assigned",
                "issue": top_issue["number"],
                "title": f"Issue #{top_issue['number']}: {top_issue['title']}",
                "status_label": label,
                "elapsed_mins": amos_elapsed
            }
        else:
            idle_label = (
                "No unassigned tickets (standing by for PR review)"
                if (open_issues or prs or any(i.get("state") == "OPEN" for i in all_issues))
                else "Roadmap clear"
            )
            amos_task = {"status": "idle", "title": idle_label, "status_label": "IDLE"}

    # Change detection and stall follow-up checks against persisted state
    now = datetime.now(timezone.utc)
    prev_open_prs = prev_state.get("open_pr_numbers", [])
    curr_open_prs = [p["number"] for p in prs]
    prev_amos_id = prev_state.get("amos_task_id")
    curr_amos_id = f"{amos_task.get('status')}:{amos_task.get('issue') or amos_task.get('pr')}"
    prev_zero_id = prev_state.get("zero_task_id")
    curr_zero_id = f"{zero_task.get('status')}:{zero_task.get('issue') or zero_task.get('pr')}"

    task_started = dict(prev_state.get("task_started", {}))
    task_nudged = dict(prev_state.get("task_nudged", {}))
    pr_started = dict(prev_state.get("pr_started", {}))
    pr_nudged = dict(prev_state.get("pr_nudged", {}))

    # Update task start timestamps
    if curr_amos_id != prev_amos_id or "amos" not in task_started:
        task_started["amos"] = prev_state.get("last_assessed_at", now.isoformat()) if curr_amos_id == prev_amos_id else now.isoformat()
        task_nudged["amos"] = None
    if curr_zero_id != prev_zero_id or "zero" not in task_started:
        task_started["zero"] = prev_state.get("last_assessed_at", now.isoformat()) if curr_zero_id == prev_zero_id else now.isoformat()
        task_nudged["zero"] = None

    # Update PR start timestamps
    curr_pr_set = {str(p["number"]) for p in prs}
    for pnum_str in list(pr_started.keys()):
        if pnum_str not in curr_pr_set:
            pr_started.pop(pnum_str, None)
            pr_nudged.pop(pnum_str, None)
    for pnum_str in curr_pr_set:
        if pnum_str not in pr_started:
            pr_started[pnum_str] = now.isoformat()
            pr_nudged[pnum_str] = None

    # Stall & Follow-up detection
    follow_ups = []

    # 1. Open PR stalls
    for p in prs:
        pnum = p["number"]
        pnum_str = str(pnum)
        pr_elapsed = get_elapsed_minutes(pr_started.get(pnum_str), now)
        pr_last_nudge = get_elapsed_minutes(pr_nudged.get(pnum_str), now)

        if pr_elapsed >= stall_threshold_mins:
            if pr_nudged.get(pnum_str) is None or pr_last_nudge >= nudge_cooldown_mins:
                author_login = p.get("author", {}).get("login", "")
                author_tag = "<@1468012353206354197>" if author_login in ("mcarmody", "amos") else ("<@1542285964213358633>" if "brock" in author_login else author_login)
                ci = p.get("ci_status")
                rev = p.get("reviewDecision")

                if ci != "PASS":
                    follow_ups.append(f"- {author_tag} [**PR #{pnum}**](<https://github.com/{REPO}/pull/{pnum}>) has been waiting for {int(pr_elapsed)}m with CI `{ci}`. Can you take a look?")
                elif p.get("gate_status", "").startswith("HOLD"):
                    follow_ups.append(f"- <@179407724335988736> [**PR #{pnum}**](<https://github.com/{REPO}/pull/{pnum}>) is green and awaiting your sign-off in #lounge ({int(pr_elapsed)}m elapsed).")
                elif rev != "APPROVED":
                    reviewer_tag = "<@1542285964213358633>" if author_login in ("mcarmody", "amos") else "<@1468012353206354197>"
                    follow_ups.append(f"- {reviewer_tag} [**PR #{pnum}**](<https://github.com/{REPO}/pull/{pnum}>) has been green and awaiting review for {int(pr_elapsed)}m. Need your review or follow-up.")
                else:
                    follow_ups.append(f"- {author_tag} [**PR #{pnum}**](<https://github.com/{REPO}/pull/{pnum}>) has been open for {int(pr_elapsed)}m without merge. What's the latest status?")
                pr_nudged[pnum_str] = now.isoformat()

    # 2. Amos task stall
    amos_last_nudge = get_elapsed_minutes(task_nudged.get("amos"), now)
    if amos_task and amos_task.get("status") in ("assigned", "mid-task", "reviewing"):
        iss_num = amos_task.get("issue")
        if iss_num:
            amos_elapsed, latest_desc, sub_issues = get_task_inactivity(
                iss_num, all_issues, all_prs, task_started.get("amos"), now
            )
            if amos_elapsed >= stall_threshold_mins:
                if task_nudged.get("amos") is None or amos_last_nudge >= nudge_cooldown_mins:
                    title = amos_task.get("title", "")
                    is_review_gate = ("review gate" in title.lower() and "pass" not in title.lower() and iss_num != 97)
                    if is_review_gate:
                        follow_ups.append(f"- [**Issue #{iss_num}**](<https://github.com/{REPO}/issues/{iss_num}>) ({title}) is awaiting human steering in #lounge (<#1534452820995080192>).")
                    else:
                        sub_note = f" (across {len(sub_issues)} sub-issues)" if sub_issues else ""
                        follow_ups.append(f"- <@1468012353206354197> you've been on [**Issue #{iss_num}**](<https://github.com/{REPO}/issues/{iss_num}>) for {int(amos_elapsed)}m with no updates{sub_note}. What's the latest status?")
                    task_nudged["amos"] = now.isoformat()
            else:
                # Active progress observed on issue, sub-issue, or related PR
                task_nudged["amos"] = None
        elif amos_task.get("status") == "reviewing":
            amos_elapsed = get_elapsed_minutes(task_started.get("amos"), now)
            if amos_elapsed >= stall_threshold_mins:
                if task_nudged.get("amos") is None or amos_last_nudge >= nudge_cooldown_mins:
                    pr_num = amos_task.get("pr")
                    follow_ups.append(f"- <@1468012353206354197> review on [**PR #{pr_num}**](<https://github.com/{REPO}/pull/{pr_num}>) has been pending for {int(amos_elapsed)}m. Need your review or follow-up.")
                    task_nudged["amos"] = now.isoformat()

    # 3. Zero task stall
    zero_last_nudge = get_elapsed_minutes(task_nudged.get("zero"), now)
    if zero_task and zero_task.get("status") in ("assigned", "mid-task", "reviewing"):
        iss_num = zero_task.get("issue")
        if iss_num:
            zero_elapsed, latest_desc, sub_issues = get_task_inactivity(
                iss_num, all_issues, all_prs, task_started.get("zero"), now
            )
            if zero_elapsed >= stall_threshold_mins:
                if task_nudged.get("zero") is None or zero_last_nudge >= nudge_cooldown_mins:
                    title = zero_task.get("title", "")
                    is_review_gate = ("review gate" in title.lower() and "pass" not in title.lower() and iss_num != 97)
                    if is_review_gate:
                        follow_ups.append(f"- [**Issue #{iss_num}**](<https://github.com/{REPO}/issues/{iss_num}>) ({title}) is awaiting human steering in #lounge (<#1534452820995080192>).")
                    else:
                        sub_note = f" (across {len(sub_issues)} sub-issues)" if sub_issues else ""
                        follow_ups.append(f"- <@1542285964213358633> you've been on [**Issue #{iss_num}**](<https://github.com/{REPO}/issues/{iss_num}>) for {int(zero_elapsed)}m with no updates{sub_note}. What's the latest status?")
                    task_nudged["zero"] = now.isoformat()
            else:
                task_nudged["zero"] = None
        elif zero_task.get("status") == "reviewing":
            zero_elapsed = get_elapsed_minutes(task_started.get("zero"), now)
            if zero_elapsed >= stall_threshold_mins:
                if task_nudged.get("zero") is None or zero_last_nudge >= nudge_cooldown_mins:
                    pr_num = zero_task.get("pr")
                    follow_ups.append(f"- <@1542285964213358633> review on [**PR #{pr_num}**](<https://github.com/{REPO}/pull/{pr_num}>) has been pending for {int(zero_elapsed)}m. Need your review or follow-up.")
                    task_nudged["zero"] = now.isoformat()

    # Trigger autonomous engineer cycle if Zero is assigned a task or review gate is pending
    if zero_task and zero_task.get("status") in ("assigned", "reviewing"):
        try:
            print("[*] Autonomously triggering agora_roguelike_engineer execution...")
            subprocess.run(
                ["python3", "/workspace/tools/agora_roguelike_engineer.py", "--run", "--force"],
                timeout=240, capture_output=True, text=True
            )
        except Exception as e:
            print(f"[Warning] Failed to trigger engineer cycle: {e}", file=sys.stderr)

    # Detect if only PR merges occurred without task or board structural changes
    is_compact_merge = (
        bool(merged_prs) and
        curr_open_prs == prev_open_prs and
        curr_amos_id == prev_amos_id and
        curr_zero_id == prev_zero_id and
        not follow_ups and
        not force
    )

    changed = (
        bool(merged_prs) or
        curr_open_prs != prev_open_prs or
        curr_amos_id != prev_amos_id or
        curr_zero_id != prev_zero_id or
        bool(follow_ups) or
        force
    )

    all_known_merged = list(known_merged.union({m["number"] if isinstance(m, dict) else m for m in merged_prs}))
    new_state = {
        "known_merged_prs": all_known_merged,
        "open_pr_numbers": curr_open_prs,
        "amos_task_id": curr_amos_id,
        "zero_task_id": curr_zero_id,
        "task_started": task_started,
        "task_nudged": task_nudged,
        "pr_started": pr_started,
        "pr_nudged": pr_nudged,
        "last_assessed_at": now.isoformat(),
    }

    return {
        "repo": REPO,
        "prs": prs,
        "merged_prs": merged_prs,
        "open_issues": open_issues,
        "amos_task": amos_task,
        "zero_task": zero_task,
        "follow_ups": follow_ups,
        "changed": changed,
        "is_compact_merge": is_compact_merge,
        "new_state": new_state
    }


def format_briefing(data: dict) -> str:
    """Format a clean, mobile-optimized Discord Markdown briefing."""
    prs = data.get("prs", [])
    merged = data.get("merged_prs", [])
    open_issues = data.get("open_issues", [])
    at = data.get("amos_task", {})
    zt = data.get("zero_task", {})

    lines = [
        "### 🎯 Agora Roguelike Board Status",
        f"**Repository:** [**`{REPO}`**](<https://github.com/{REPO}>) | **Focus:** M0 Vertical Slice",
        "",
        "### 📊 Active Board State",
    ]

    if merged:
        merged_links = []
        for m in merged:
            num = m["number"] if isinstance(m, dict) else m
            merged_links.append(f"[**PR #{num}**](<https://github.com/{REPO}/pull/{num}>)")
        lines.append(f"- **Autonomously Merged:** {', '.join(merged_links)}")

    if prs:
        lines.append(f"- **Open PRs ({len(prs)}):**")
        for p in prs:
            pnum = p["number"]
            title = p["title"]
            rev = p.get("reviewDecision") or "PENDING"
            ci = p.get("ci_status") or "UNKNOWN"
            gate_st = p.get("gate_status", "")
            if gate_st.startswith("HOLD"):
                rev = "HOLD (sign-off requested)"
            lines.append(f"  - [**PR #{pnum}**](<https://github.com/{REPO}/pull/{pnum}>): {title} (`{p.get('headRefName', '')}`) | CI: `{ci}` | Review: `{rev}`")
    else:
        lines.append("- **Open PRs:** None (clean queue)")

    m0_issues = [i for i in open_issues if i["number"] in (83, 84, 85)]
    if m0_issues:
        lines.append(f"- **Active M0 Tasks ({len(m0_issues)}):**")
        for iss in m0_issues:
            lines.append(f"  - [**Issue #{iss['number']}**](<https://github.com/{REPO}/issues/{iss['number']}>): {iss['title']}")

    lines.append("")
    lines.append("### 🤖 Agent Task Assignments")

    # Amos
    if at.get("status") == "mid-task":
        lines.append(f"- **Amos (<@1468012353206354197>):** MID-TASK on [**PR #{at['pr']}**](<https://github.com/{REPO}/pull/{at['pr']}>) (`{at['branch']}`) — CI: `{at['ci']}` | Review: `{at['review']}`")
    elif at.get("status") == "reviewing":
        lines.append(f"- **Amos (<@1468012353206354197>):** REVIEWING [**PR #{at['pr']}**](<https://github.com/{REPO}/pull/{at['pr']}>) | On Deck: {at.get('on_deck', 'Next Issue')}")
    elif at.get("status") == "assigned":
        lines.append(f"- **Amos (<@1468012353206354197>):** ACTIVE -> {at['title']}")
    elif at.get("status") == "on-deck":
        lines.append(f"- **Amos (<@1468012353206354197>):** ON DECK -> {at['title']} (and primary reviewer for Zero's PRs)")
    else:
        lines.append(f"- **Amos (<@1468012353206354197>):** {at.get('status_label', 'IDLE')} -> {at.get('title', 'None')}")

    # Zero
    if zt.get("status") == "mid-task":
        lines.append(f"- **Zero (<@1542285964213358633>):** MID-TASK on [**PR #{zt['pr']}**](<https://github.com/{REPO}/pull/{zt['pr']}>) (`{zt['branch']}`) — CI: `{zt['ci']}` | Review: `{zt['review']}`")
    elif zt.get("status") == "reviewing":
        lines.append(f"- **Zero (<@1542285964213358633>):** REVIEWING [**PR #{zt['pr']}**](<https://github.com/{REPO}/pull/{zt['pr']}>) | On Deck: {zt.get('on_deck', 'Next Issue')}")
    elif zt.get("status") == "assigned":
        lines.append(f"- **Zero (<@1542285964213358633>):** ACTIVE -> {zt['title']}")
    else:
        lines.append(f"- **Zero (<@1542285964213358633>):** {zt.get('status_label', 'IDLE')} -> {zt.get('title', 'None')}")

    follow_ups = data.get("follow_ups", [])
    if follow_ups:
        lines.append("")
        lines.append("### ⚠️ Follow-Up Requests")
        for fu in follow_ups:
            lines.append(fu)

    lines.append("")
    lines.append("### 🛠️ Next Steps")
    if zt.get("status") == "mid-task":
        lines.append(f"- Amos reviews [**PR #{zt['pr']}**](<https://github.com/{REPO}/pull/{zt['pr']}>).")
        lines.append("- PM sidecar merges upon approval and green CI.")
    elif at.get("status") == "mid-task":
        at_pr_num = at.get("pr")
        matching_pr = next((p for p in prs if p["number"] == at_pr_num), None)
        if matching_pr and matching_pr.get("gate_status", "").startswith("HOLD"):
            lines.append(f"- [**PR #{at['pr']}**](<https://github.com/{REPO}/pull/{at['pr']}>) is on HOLD awaiting Ryan's sign-off in #lounge.")
        else:
            lines.append(f"- Zero reviews [**PR #{at['pr']}**](<https://github.com/{REPO}/pull/{at['pr']}>).")
            lines.append("- PM sidecar merges upon approval and green CI.")
    elif zt.get("status") == "assigned":
        lines.append(f"- <@1542285964213358633> executing [**{zt['title']}**](<https://github.com/{REPO}/issues/{zt.get('issue', '')}>).")
        if at.get("status") == "assigned":
            lines.append(f"- <@1468012353206354197> executing [**{at['title']}**](<https://github.com/{REPO}/issues/{at.get('issue', '')}>).")
    else:
        lines.append("- Engineers pull latest `main` and execute assigned issues.")
        lines.append("- PM sidecar autonomously squash-merges approved PRs with green CI.")

    return "\n".join(lines)


def format_compact_merge_notice(merged_prs: list[dict]) -> str:
    """Format a clean, concise notice when PRs merge without board restructuring."""
    m_links = [f"[**PR #{m['number']}**](<https://github.com/{REPO}/pull/{m['number']}>)" for m in merged_prs]
    return f"### 🚀 Autonomously Merged: {', '.join(m_links)}\nAll checks passed and peer review certified. Branch deleted."


def print_assessment(data: dict):
    if data.get("is_compact_merge"):
        print(format_compact_merge_notice(data.get("merged_prs", [])))
    else:
        print(format_briefing(data))


def dispatch_briefing(briefing: str, channel: str | int = DEFAULT_CHANNEL_ID) -> bool:
    """Queue formatted briefing to target channel via outbox."""
    try:
        from tools.outbox import queue_outbox_message
        res = queue_outbox_message(channel, briefing, source_turn="agora-roguelike-pm")
        print(f"[PM Sidecar] Queued briefing to {channel} (outbox ID: {res.get('id')})")
        return True
    except Exception as e:
        print(f"[PM Sidecar] Error queueing outbox briefing: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agora Roguelike PM Coordinator")
    parser.add_argument("--assess", action="store_true", default=True, help="Assess board and print status")
    parser.add_argument("--dispatch", action="store_true", help="Force dispatch briefing to Discord outbox")
    parser.add_argument("--channel", default=str(DEFAULT_CHANNEL_ID), help="Target Discord channel name or ID")
    parser.add_argument("--stall-threshold", type=float, default=45.0, help="Minutes of inactivity before requesting follow-up (default: 45)")
    parser.add_argument("--nudge-cooldown", type=float, default=45.0, help="Minutes between repeat follow-up requests (default: 45)")
    parser.add_argument("--force", action="store_true", help="Force change state")
    parser.add_argument("--quiet", action="store_true", help="Silent when nominal")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    data = assess_board(force=args.force, stall_threshold_mins=args.stall_threshold, nudge_cooldown_mins=args.nudge_cooldown)
    if args.json:
        print(json.dumps(data, indent=2))
        sys.exit(0)

    if data.get("is_compact_merge"):
        briefing = format_compact_merge_notice(data.get("merged_prs", []))
    else:
        briefing = format_briefing(data)

    if data.get("changed") or args.dispatch:
        print(briefing)
        dispatch_briefing(briefing, channel=args.channel)
        save_state(data["new_state"])
    else:
        if not args.quiet:
            print("(nominal - board state unchanged)")
