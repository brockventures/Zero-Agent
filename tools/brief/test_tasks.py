"""Unit tests for BRIEF Wave 2 task handoff validator and staleness backoff engine."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.brief.tasks import (
    _TASK_SLUG_RE,
    create_or_update_task_handoff,
    evaluate_task_staleness,
    mark_tasks_nudged,
    parse_task_file,
    render_task_file,
    resolve_task_path,
    scan_personal_tasks,
    scan_tasks_for_daily_review,
    validate_task_slug,
)
from tools.brief.tasks import (
    main as tasks_cli_main,
)

PT = ZoneInfo("America/Los_Angeles")


def _pt_epoch(year: int, month: int, day: int, hour: int = 9, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, 0, tzinfo=PT).timestamp()


def test_slug_validation_and_path_traversal_rejection(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)

    valid_slug = "20261124-follow-up-dentist"
    assert _TASK_SLUG_RE.fullmatch(valid_slug) is not None
    assert validate_task_slug(valid_slug) == valid_slug
    slug, path = resolve_task_path(task_dir, valid_slug)
    assert slug == valid_slug
    assert path == (task_dir / f"{valid_slug}.md").resolve()

    bad_slugs = [
        "",
        "   ",
        "follow-up-without-date",
        "2026112-short-date",
        "20261124-a",  # too short after date prefix (requires at least 2 chars)
        "20261124-Upper-Case",
        "20261124-under_score",
        "20261124--leading-dash",
        "../20261124-escape",
        "20261124-nested/slug",
        "20261124-nested\\slug",
        "..\\20261124-escape",
        "20261124-with.dot",
        f"20261124-{'a' * 64}",  # exceeds 63-char slug tail
    ]
    for bad in bad_slugs:
        with pytest.raises(ValueError):
            validate_task_slug(bad)
        with pytest.raises(ValueError):
            create_or_update_task_handoff(task_dir, bad, summary="should fail")


def test_handoff_rejects_invalid_status_and_timestamp(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    slug = "20261124-valid-slug"

    with pytest.raises(ValueError, match="Invalid task status"):
        create_or_update_task_handoff(task_dir, slug, status="pending")

    with pytest.raises(ValueError, match="Invalid ISO-8601 timestamp"):
        create_or_update_task_handoff(
            task_dir,
            slug,
            status="open",
            next_check="not-a-timestamp",
        )


def test_atomic_create_and_update_preserves_gate_receipts_entities_and_body(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "task"
    slug = "20261124-vendor-contract"
    t0 = _pt_epoch(2026, 11, 24, 10, 30)

    created = create_or_update_task_handoff(
        task_dir,
        slug,
        status="open",
        entities=["thread:abc123", "person:Alex"],
        summary="Initial intake handoff from email thread.",
        source_session="intake/gmail/abc123",
        now=t0,
    )
    assert created["created"] is True
    assert created["status"] == "open"
    assert created["waiting_on"] == ""
    # Default +3d for open task
    assert created["next_check"] == "2026-11-27T10:30:00-08:00"
    assert created["entities"] == ["thread:abc123", "person:Alex"]
    assert created["gate_receipts"] == []
    assert created["nudge_count"] == 0
    assert created["last_nudged_at"] == ""
    assert "2026-11-24 10:30 PT" in created["body"]
    assert "Initial intake handoff from email thread." in created["body"]

    # Simulate task session recording a gate receipt directly in the file
    task_path = task_dir / f"{slug}.md"
    parsed = parse_task_file(task_path)
    fm = dict(parsed["frontmatter"])
    fm["gate_receipts"] = ["gate-receipt:prop-01:approved (event_id=cal_99)"]
    task_path.write_text(render_task_file(fm, parsed["body"]), encoding="utf-8")

    # Update existing task to blocked status without explicit next_check -> defaults to +6d
    t1 = _pt_epoch(2026, 11, 25, 14, 15)
    updated = create_or_update_task_handoff(
        task_dir,
        slug,
        status="blocked",
        waiting_on="Vendor counter-signature",
        entities=["doc:contract-v2", "thread:abc123"],
        summary="Sent calendar invite; waiting on signed PDF.",
        source_session="task/20261124-vendor-contract",
        now=t1,
    )
    assert updated["created"] is False
    assert updated["status"] == "blocked"
    assert updated["waiting_on"] == "Vendor counter-signature"
    assert updated["next_check"] == "2026-12-01T14:15:00-08:00"
    assert updated["gate_receipts"] == ["gate-receipt:prop-01:approved (event_id=cal_99)"]
    assert updated["entities"] == ["thread:abc123", "person:Alex", "doc:contract-v2"]
    assert "Initial intake handoff from email thread." in updated["body"]
    assert "Sent calendar invite; waiting on signed PDF." in updated["body"]

    # No leftover .tmp files
    assert list(task_dir.glob(".*tmp*")) == []

    # Clean close clears waiting_on and next_check while keeping receipts & history
    t2 = _pt_epoch(2026, 11, 26, 16, 0)
    closed = create_or_update_task_handoff(
        task_dir,
        slug,
        status="done",
        summary="Contract countersigned and archived.",
        now=t2,
    )
    assert closed["status"] == "done"
    assert closed["waiting_on"] == ""
    assert closed["next_check"] == ""
    assert closed["gate_receipts"] == ["gate-receipt:prop-01:approved (event_id=cal_99)"]


def test_staleness_thresholds_3d_open_vs_6d_blocked() -> None:
    t0 = _pt_epoch(2026, 11, 1, 9, 0)
    future_check = "2026-11-20T09:00:00-08:00"

    open_task = {
        "status": "open",
        "waiting_on": "",
        "next_check": future_check,
        "nudge_count": 0,
        "last_nudged_at": "",
    }
    blocked_task = {
        "status": "blocked",
        "waiting_on": "External reply",
        "next_check": future_check,
        "nudge_count": 0,
        "last_nudged_at": "",
    }

    # At +2.5 days: neither open nor blocked is stale
    t_2_5d = t0 + 2.5 * 86400
    res_open_early = evaluate_task_staleness(open_task, mtime=t0, now=t_2_5d)
    assert res_open_early["is_stale"] is False
    assert res_open_early["should_surface"] is False
    assert res_open_early["next_backoff_days"] == 3

    # At +3.0 days: open is stale (3d threshold), blocked is NOT stale (6d threshold)
    t_3d = t0 + 3.0 * 86400
    res_open_3d = evaluate_task_staleness(open_task, mtime=t0, now=t_3d)
    assert res_open_3d["is_stale"] is True
    assert res_open_3d["should_surface"] is True
    assert res_open_3d["next_backoff_days"] == 3

    res_blocked_3d = evaluate_task_staleness(blocked_task, mtime=t0, now=t_3d)
    assert res_blocked_3d["is_stale"] is False
    assert res_blocked_3d["should_surface"] is False
    assert res_blocked_3d["next_backoff_days"] == 6

    # At +5.9 days: blocked still not stale
    t_5_9d = t0 + 5.9 * 86400
    assert evaluate_task_staleness(blocked_task, mtime=t0, now=t_5_9d)["is_stale"] is False

    # At +6.0 days: blocked becomes stale
    t_6d = t0 + 6.0 * 86400
    res_blocked_6d = evaluate_task_staleness(blocked_task, mtime=t0, now=t_6d)
    assert res_blocked_6d["is_stale"] is True
    assert res_blocked_6d["should_surface"] is True
    assert res_blocked_6d["next_backoff_days"] == 6


def test_day_3_7_14_backoff_suppression_prevents_daily_nagging(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    slug = "20261101-reimburse-receipt"
    t0 = _pt_epoch(2026, 11, 1, 9, 0)

    # Create open task at Day 0 (default next_check = Day 3)
    create_or_update_task_handoff(
        task_dir,
        slug,
        status="open",
        summary="Submit travel receipt.",
        now=t0,
    )

    # Day 3: task is due and stale (nudge_count=0 -> threshold 3d)
    day3 = t0 + 3 * 86400
    scan_day3 = scan_tasks_for_daily_review(task_dir, now=day3)
    assert scan_day3["has_actionable_items"] is True
    assert len(scan_day3["stale_tasks"]) == 1
    assert scan_day3["stale_tasks"][0]["next_backoff_days"] == 3

    # Mark nudged on Day 3 -> nudge_count=1, last_nudged_at=Day 3
    nudged1 = mark_tasks_nudged(task_dir, [slug], now=day3)
    assert len(nudged1) == 1
    assert nudged1[0]["nudge_count"] == 1

    # Days 4 through 9 (1d to 6d after nudge 1): suppressed by 7d backoff window
    for offset_days in (4, 5, 8, 9):
        scan_mid = scan_tasks_for_daily_review(task_dir, now=t0 + offset_days * 86400)
        assert scan_mid["has_actionable_items"] is False
        assert scan_mid["due_tasks"] == []
        assert scan_mid["stale_tasks"] == []

    # Day 10 (7 days after Day 3 nudge): surfaces again (nudge_count=1 -> threshold 7d)
    day10 = day3 + 7 * 86400
    scan_day10 = scan_tasks_for_daily_review(task_dir, now=day10)
    assert scan_day10["has_actionable_items"] is True
    assert len(scan_day10["stale_tasks"]) == 1
    assert scan_day10["stale_tasks"][0]["next_backoff_days"] == 7

    # Mark nudged on Day 10 -> nudge_count=2, last_nudged_at=Day 10
    nudged2 = mark_tasks_nudged(task_dir, [slug], now=day10)
    assert nudged2[0]["nudge_count"] == 2

    # Day 17 (7 days after nudge 2): still suppressed by 14d backoff window
    day17 = day10 + 7 * 86400
    scan_day17 = scan_tasks_for_daily_review(task_dir, now=day17)
    assert scan_day17["has_actionable_items"] is False

    # Day 24 (14 days after nudge 2): surfaces at 14d cap
    day24 = day10 + 14 * 86400
    scan_day24 = scan_tasks_for_daily_review(task_dir, now=day24)
    assert scan_day24["has_actionable_items"] is True
    assert scan_day24["stale_tasks"][0]["next_backoff_days"] == 14


def test_clean_close_done_canceled_ignored_and_empty_dir_silent_scan(
    tmp_path: Path,
) -> None:
    empty_dir = tmp_path / "empty_task"
    empty_scan = scan_tasks_for_daily_review(empty_dir, now=_pt_epoch(2026, 11, 24, 9, 0))
    assert empty_scan == {
        "open_count": 0,
        "blocked_count": 0,
        "due_tasks": [],
        "stale_tasks": [],
        "actionable_tasks": [],
        "has_actionable_items": False,
    }

    task_dir = tmp_path / "task"
    t0 = _pt_epoch(2026, 11, 1, 9, 0)
    create_or_update_task_handoff(
        task_dir,
        "20261101-done-task",
        status="done",
        summary="Completed task.",
        now=t0,
    )
    create_or_update_task_handoff(
        task_dir,
        "20261101-canceled-task",
        status="canceled",
        summary="Canceled task.",
        now=t0,
    )

    # Even 30 days later, done/canceled tasks never count as open/blocked or surface
    t_later = t0 + 30 * 86400
    scan = scan_tasks_for_daily_review(task_dir, now=t_later)
    assert scan["open_count"] == 0
    assert scan["blocked_count"] == 0
    assert scan["has_actionable_items"] is False
    assert scan["due_tasks"] == []
    assert scan["stale_tasks"] == []


def test_cli_handoff_and_scan_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_dir = tmp_path / "task"
    rc1 = tasks_cli_main(
        [
            "handoff",
            "--task-dir",
            str(task_dir),
            "--slug",
            "20261124-cli-check",
            "--status",
            "open",
            "--next-check",
            "2020-01-01T09:00:00-08:00",
            "--entity",
            "thread:t1,person:Bob",
            "--summary",
            "Created via CLI",
        ]
    )
    assert rc1 == 0
    out1 = json.loads(capsys.readouterr().out)
    assert out1["slug"] == "20261124-cli-check"
    assert out1["entities"] == ["thread:t1", "person:Bob"]

    rc2 = tasks_cli_main(
        [
            "scan",
            "--task-dir",
            str(task_dir),
            "--mark-nudged",
        ]
    )
    assert rc2 == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["has_actionable_items"] is True
    assert len(out2["due_tasks"]) == 1

    # Verify --mark-nudged incremented nudge_count on disk
    parsed = parse_task_file(task_dir / "20261124-cli-check.md")
    assert parsed["nudge_count"] == 1
    assert parsed["last_nudged_at"] != ""


def test_scan_personal_tasks_filters_and_classifies(tmp_path: Path) -> None:
    now_ts = _pt_epoch(2026, 11, 26, 7, 0)

    # Missing or invalid file returns empty scan cleanly
    missing_scan = scan_personal_tasks(tmp_path / "missing.json", now=now_ts)
    assert missing_scan["open_personal_count"] == 0
    assert missing_scan["actionable_personal_tasks"] == []
    assert missing_scan["has_actionable_personal"] is False

    tasks_json = tmp_path / "tasks.json"
    tasks_json.write_text(
        json.dumps(
            [
                {"id": 1, "title": "Completed chore", "priority": "p1", "status": "completed"},
                {"id": 2, "title": "Retired chore", "priority": "p2", "status": "retired"},
                {"id": 3, "title": "Canceled chore", "priority": "p2", "status": "canceled"},
                {"id": 4, "title": "fix(brief): dev ticket", "priority": "p1", "status": "pending"},
                {"id": 5, "title": "Internal flagged", "priority": "p1", "status": "pending", "dev": True},
                {
                    "id": 114,
                    "title": "Replace front yard irrigation controller",
                    "priority": "p2",
                    "status": "pending",
                    "due": "2026-11-16",
                },
                {
                    "id": 116,
                    "title": "Enable HA Companion App BLE Transmitter",
                    "priority": "p2",
                    "status": "pending",
                    "due": "2026-11-26T00:00:00.000Z",
                    "target_time_pt": "Thu Nov 26 @ 10:00 AM PT",
                },
                {
                    "id": 117,
                    "title": "Urgent school form",
                    "priority": "p1",
                    "status": "pending",
                    "due": "2026-11-26",
                },
                {
                    "id": 48,
                    "title": "Rewire the BAF fans",
                    "priority": "p1",
                    "status": "pending",
                },
                {
                    "id": 115,
                    "title": "Evaluate Nest 4th-Gen Matter update",
                    "priority": "p2",
                    "status": "pending",
                    "due": "2026-12-15",
                },
                {
                    "id": 30,
                    "title": "Setup living room motion detector",
                    "priority": "p2",
                    "status": "pending",
                },
            ]
        ),
        encoding="utf-8",
    )

    res = scan_personal_tasks(tasks_json, now=now_ts)
    assert res["open_personal_count"] == 6
    assert [t["id"] for t in res["overdue"]] == [114]
    assert [t["id"] for t in res["due_today"]] == [117, 116]
    assert [t["id"] for t in res["high_priority"]] == [117, 48]
    assert [t["id"] for t in res["upcoming"]] == [115]
    assert [t["id"] for t in res["backlog"]] == [30]
    # Deduplicated actionable tasks: overdue (114) -> due_today (117, 116) -> remaining p1 (48)
    assert [t["id"] for t in res["actionable_personal_tasks"]] == [114, 117, 116, 48]
    assert len(res["all_open_personal_tasks"]) == 6
    assert res["has_actionable_personal"] is True


def test_scan_tasks_for_daily_review_merges_personal_tasks_and_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task_dir = tmp_path / "task"
    tasks_json = tmp_path / "tasks.json"
    tasks_json.write_text(
        json.dumps(
            [
                {
                    "id": 116,
                    "title": "Enable HA Companion App BLE Transmitter",
                    "priority": "p2",
                    "status": "pending",
                    "due": "2026-11-26",
                },
                {
                    "id": 48,
                    "title": "Rewire the BAF fans",
                    "priority": "p1",
                    "status": "pending",
                },
            ]
        ),
        encoding="utf-8",
    )

    # Works even when task_dir does not exist yet
    now_ts = _pt_epoch(2026, 11, 26, 7, 0)
    merged = scan_tasks_for_daily_review(
        task_dir,
        now=now_ts,
        personal_tasks_path=tasks_json,
    )
    assert merged["open_count"] == 2
    assert merged["open_personal_count"] == 2
    assert merged["has_actionable_items"] is True
    assert len(merged["due_tasks"]) == 1
    assert len(merged["actionable_tasks"]) == 2
    assert merged["personal_tasks"]["open_personal_count"] == 2

    # CLI scan with --personal-tasks-path and --mark-nudged handles personal tasks cleanly
    create_or_update_task_handoff(
        task_dir,
        "20261124-follow-up-item",
        status="open",
        next_check="2020-01-01T09:00:00-08:00",
        summary="MD task due",
    )
    rc = tasks_cli_main(
        [
            "scan",
            "--task-dir",
            str(task_dir),
            "--personal-tasks-path",
            str(tasks_json),
            "--mark-nudged",
        ]
    )
    assert rc == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["has_actionable_items"] is True
    assert cli_out["open_personal_count"] == 2
    assert len(cli_out["actionable_tasks"]) == 3
    assert parse_task_file(task_dir / "20261124-follow-up-item.md")["nudge_count"] == 1
