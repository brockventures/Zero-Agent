import sys
from pathlib import Path
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
if "/workspace/tools" not in sys.path:
    sys.path.insert(0, "/workspace/tools")

import pytest
from datetime import datetime, timezone, timedelta
from tools.agora_roguelike_pm import (
    get_task_inactivity,
    get_open_prioritized_issues,
    get_elapsed_minutes,
)


def test_get_task_inactivity_direct_sub_issue():
    now = datetime.now(timezone.utc)
    t_start = (now - timedelta(minutes=120)).isoformat()
    t_sub_update = (now - timedelta(minutes=10)).isoformat()

    all_issues = [
        {
            "number": 14,
            "title": "Epic 3: Planetary Sector Barons & Rival Syndicate AI",
            "body": "Overview of Epic 3",
            "state": "OPEN",
            "updatedAt": (now - timedelta(minutes=500)).isoformat(),
        },
        {
            "number": 111,
            "title": "Epic 3 task 0: travel loop between stations",
            "body": "Part of #14 (Epic 3: Planetary Sector Barons)",
            "state": "CLOSED",
            "updatedAt": t_sub_update,
            "closedAt": t_sub_update,
        },
    ]
    all_prs = []

    elapsed, desc, sub_issues = get_task_inactivity(14, all_issues, all_prs, t_start, now)
    assert sub_issues == [111]
    assert "Sub-issue #111" in desc
    assert 9.0 <= elapsed <= 11.0


def test_get_task_inactivity_transitive_sub_issue_and_pr():
    now = datetime.now(timezone.utc)
    t_start = (now - timedelta(minutes=300)).isoformat()
    t_pr_merge = (now - timedelta(minutes=5)).isoformat()

    all_issues = [
        {
            "number": 14,
            "title": "Epic 3: Planetary Sector Barons & Rival Syndicate AI",
            "body": "Overview",
            "state": "OPEN",
            "updatedAt": (now - timedelta(minutes=600)).isoformat(),
        },
        {
            "number": 15,
            "title": "Design Sector Baron Framework",
            "body": "Part of #14 (Epic 3)",
            "state": "OPEN",
            "updatedAt": (now - timedelta(minutes=200)).isoformat(),
        },
        {
            "number": 113,
            "title": "Baron sub-task 1",
            "body": "under #15",
            "state": "OPEN",
            "updatedAt": (now - timedelta(minutes=100)).isoformat(),
        },
    ]
    all_prs = [
        {
            "number": 114,
            "title": "feat(epic3): implement baron data model (closes #113)",
            "body": "Part of #15, under #14",
            "headRefName": "feat/baron-model",
            "state": "MERGED",
            "mergedAt": t_pr_merge,
            "updatedAt": t_pr_merge,
            "createdAt": (now - timedelta(minutes=20)).isoformat(),
        }
    ]

    elapsed, desc, sub_issues = get_task_inactivity(14, all_issues, all_prs, t_start, now)
    assert 15 in sub_issues
    assert 113 in sub_issues
    assert "PR #114 merged" in desc
    assert 4.0 <= elapsed <= 6.0


def test_get_task_inactivity_epic_tag_matching():
    now = datetime.now(timezone.utc)
    t_start = (now - timedelta(minutes=200)).isoformat()
    t_pr_merge = (now - timedelta(minutes=15)).isoformat()

    all_issues = [
        {
            "number": 14,
            "title": "Epic 3: Planetary Sector Barons & Rival Syndicate AI",
            "body": "Overview",
            "state": "OPEN",
            "updatedAt": (now - timedelta(minutes=400)).isoformat(),
        }
    ]
    # PR doesn't explicitly have #14 in text, but has docs(epic3) in title
    all_prs = [
        {
            "number": 110,
            "title": "docs(epic3): record Ryan's barons decisions",
            "body": "Notes from steering",
            "headRefName": "docs/epic3-ryan-decisions",
            "state": "MERGED",
            "mergedAt": t_pr_merge,
            "updatedAt": t_pr_merge,
            "createdAt": (now - timedelta(minutes=40)).isoformat(),
        }
    ]

    elapsed, desc, sub_issues = get_task_inactivity(14, all_issues, all_prs, t_start, now)
    assert "PR #110 merged" in desc
    assert 14.0 <= elapsed <= 16.0


def test_get_task_inactivity_stale_issue():
    now = datetime.now(timezone.utc)
    t_start = (now - timedelta(minutes=120)).isoformat()

    all_issues = [
        {
            "number": 97,
            "title": "Epic 2 balance pass: Ryan's steering from the #13 review gate",
            "body": "Part of #8",
            "state": "OPEN",
            "updatedAt": (now - timedelta(minutes=300)).isoformat(),
        }
    ]
    all_prs = []

    elapsed, desc, sub_issues = get_task_inactivity(97, all_issues, all_prs, t_start, now)
    assert sub_issues == []
    # If task was assigned 120m ago and no updates occurred since, elapsed inactivity is 120m
    assert 119.0 <= elapsed <= 121.0


def test_get_open_prioritized_issues_filtering():
    all_issues = [
        {"number": 999, "title": "Random issue", "state": "OPEN", "assignees": []},
        {"number": 105, "title": "Epic 6: re-skin", "state": "OPEN", "assignees": []},
        {"number": 83, "title": "M0 Task 1", "state": "CLOSED", "assignees": []},
        {"number": 84, "title": "M0 Task 2", "state": "OPEN", "assignees": []},
    ]
    prioritized = get_open_prioritized_issues(all_issues)
    # Closed issue 83 should NOT be present
    assert not any(i["number"] == 83 for i in prioritized)
    # 105 is higher priority than 84 in PRIORITY_ISSUE_LIST
    nums = [i["number"] for i in prioritized]
    assert nums.index(105) < nums.index(84)
    # 999 is not in PRIORITY_ISSUE_LIST, should be at the end
    assert nums[-1] == 999
