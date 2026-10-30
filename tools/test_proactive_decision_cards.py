#!/usr/bin/env python3
"""Unit tests for Workday Proactive Decision Cards Engine.

Verifies:
1. Comms Action Triage Card (categories, deduplication, event-driven silence, staging)
2. 48h Family Calendar Radar Card (overlaps, <15m tight buffers, prep items, event-driven silence)
3. Afternoon Dinner Pivot Card (unscheduled trigger, collision trigger, nominal silence)
4. Parser helpers and shell payload validation
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo

import pytest

# Ensure tools and workspace on sys.path
_CURRENT_DIR = Path(__file__).resolve().parent
_WORKSPACE_DIR = _CURRENT_DIR.parent
for _p in [str(_WORKSPACE_DIR), str(_CURRENT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.forge import _validate_decision_shell_payload, init_forge_db
from tools.proactive_decision_cards import (
    CHANNEL_BROCK_HOUSE,
    CHANNEL_SHOPPING,
    CHANNEL_ZERO_CHAT,
    _classify_comms_item,
    _parse_event_datetime,
    _parse_recipe_minutes,
    run_dinner_pivot_card,
    run_family_calendar_radar_card,
    run_inbox_rcs_action_triage_card,
)

PT = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    """Initialize a clean, isolated SQLite forge database for each test."""
    db_file = tmp_path / "forge_test.db"
    init_forge_db(db_file)
    return db_file


@pytest.fixture
def isolate_triage_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Direct triage_card_seen.json to an isolated temp path."""
    seen_path = tmp_path / "triage_card_seen.json"
    monkeypatch.setenv("TRIAGE_SEEN_FILE_PATH", str(seen_path))
    return seen_path


# ==============================================================================
# Helper Unit Tests
# ==============================================================================
def test_parse_recipe_minutes() -> None:
    # Direct integer and string numbers
    assert _parse_recipe_minutes(45) == 45
    assert _parse_recipe_minutes("30") == 30

    # ISO 8601 duration strings
    assert _parse_recipe_minutes("PT45M") == 45
    assert _parse_recipe_minutes("PT1H") == 60
    assert _parse_recipe_minutes("PT1H30M") == 90
    assert _parse_recipe_minutes("PT2H15M") == 135

    # Human-readable plain text
    assert _parse_recipe_minutes("45 minutes") == 45
    assert _parse_recipe_minutes("1 hr 15 mins") == 75
    assert _parse_recipe_minutes("2 hours") == 120
    assert _parse_recipe_minutes("25 min") == 25

    # Fallback and empty
    assert _parse_recipe_minutes(None) == 0
    assert _parse_recipe_minutes("") == 0
    assert _parse_recipe_minutes("unknown duration") == 0


def test_classify_comms_item() -> None:
    # 1. School/Daycare
    assert _classify_comms_item({"subject": "Preschool early dismissal reminder"}) == "school_daycare"
    assert _classify_comms_item({"snippet": "Please submit kindergarten tuition for next month"}) == "school_daycare"

    # 2. Medical
    assert _classify_comms_item({"subject": "Dr. Smith: Rosie pediatrician checkup"}) == "medical"
    assert _classify_comms_item({"snippet": "New lab results available in MyChart"}) == "medical"

    # 3. Contractor
    assert _classify_comms_item({"subject": "Apex HVAC repair estimate"}) == "contractor"
    assert _classify_comms_item({"snippet": "The plumber can come tomorrow for the service call"}) == "contractor"

    # 4. Package exception
    assert _classify_comms_item({"subject": "UPS Delivery Exception: Action required"}) == "package_exception"
    assert _classify_comms_item({"snippet": "Your package has been delayed due to weather"}) == "package_exception"

    # 5. RSVP
    assert _classify_comms_item({"subject": "Invitation: Leo's 5th Birthday Party RSVP"}) == "rsvp"
    assert _classify_comms_item({"snippet": "Please confirm attendance for the neighborhood potluck"}) == "rsvp"

    # Non-actionable noise
    assert _classify_comms_item({"subject": "Weekly Newsletter", "snippet": "Check out our top 10 articles"}) is None


def test_parse_event_datetime() -> None:
    # All-day date string
    dt_allday, is_all_day = _parse_event_datetime("2026-11-05")
    assert is_all_day is True
    assert dt_allday.tzinfo == PT
    assert dt_allday.year == 2026 and dt_allday.month == 11 and dt_allday.day == 5

    # ISO 8601 with offset (PST is UTC-8 in November)
    dt_timed, is_all_day_timed = _parse_event_datetime("2026-11-05T17:30:00-08:00")
    assert is_all_day_timed is False
    assert dt_timed.hour == 17 and dt_timed.minute == 30


# ==============================================================================
# Workflow 1: Inbox/RCS Action Triage Card Tests
# ==============================================================================
def test_inbox_rcs_triage_silent_when_zero_items(test_db: Path, isolate_triage_seen: Path) -> None:
    """When zero actionable items are detected, card is strictly silent."""
    ok, summary, meta = run_inbox_rcs_action_triage_card(
        slot="morning",
        test=True,
        db_path=test_db,
        post_to_discord=False,
        items_override=[],
    )
    assert ok is True
    assert summary == ""
    assert meta["cards_staged"] == 0
    assert meta["detected_items"] == 0


def test_inbox_rcs_triage_filters_non_actionable(test_db: Path, isolate_triage_seen: Path) -> None:
    """Items not matching the 5 actionable categories are ignored."""
    items = [
        {"id": "msg-1", "subject": "Promotional: 50% off pizza", "sender": "deals@promo.com"},
        {"id": "msg-2", "subject": "Substack Daily Digest", "sender": "writer@substack.com"},
    ]
    ok, summary, meta = run_inbox_rcs_action_triage_card(
        slot="morning",
        test=True,
        db_path=test_db,
        post_to_discord=False,
        items_override=items,
    )
    assert ok is True
    assert summary == ""
    assert meta["cards_staged"] == 0


def test_inbox_rcs_triage_stages_card_and_deduplicates(test_db: Path, isolate_triage_seen: Path) -> None:
    """Stages card for actionable items, saves seen IDs, and avoids duplicate cards on next run."""
    items = [
        {
            "id": "school-101",
            "source": "gmail",
            "sender": "director@preschool.org",
            "subject": "Preschool early dismissal on Friday at 12:30 PM",
        },
        {
            "id": "med-202",
            "source": "rcs",
            "sender": "Dr. Miller",
            "subject": "Pediatrician immunization appointment confirmation for Rosie",
        },
    ]

    # First run: stages card
    ok, summary, meta = run_inbox_rcs_action_triage_card(
        slot="morning",
        test=True,
        db_path=test_db,
        post_to_discord=False,
        items_override=items,
    )
    assert ok is True
    assert meta["cards_staged"] == 1
    assert meta["detected_items"] == 2
    assert "Preschool" in summary
    assert "Pediatrician" in summary

    # Verify staged in SQLite
    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (meta["decision_id"],)).fetchone()
    conn.close()

    assert row is not None
    assert row["category"] == "comms"
    assert row["channel_id"] == CHANNEL_ZERO_CHAT
    assert row["handler_type"] == "shell"

    payload = json.loads(row["payload_json"])
    valid_sh, val_err = _validate_decision_shell_payload("shell", payload)
    assert valid_sh is True, f"Shell payload invalid: {val_err}"

    # Verify seen file was written
    assert isolate_triage_seen.exists()
    seen_content = json.loads(isolate_triage_seen.read_text())
    assert "school-101" in seen_content["seen_ids"]
    assert "med-202" in seen_content["seen_ids"]

    # Second run with same items: deduplication makes it silent
    ok2, summary2, meta2 = run_inbox_rcs_action_triage_card(
        slot="morning",
        test=True,
        db_path=test_db,
        post_to_discord=False,
        items_override=items,
    )
    assert ok2 is True
    assert summary2 == ""
    assert meta2["cards_staged"] == 0


def test_inbox_rcs_triage_dry_run(test_db: Path, isolate_triage_seen: Path) -> None:
    """Dry run outputs preview summary but does not stage in DB or update seen."""
    items = [{
        "id": "contractor-999",
        "source": "sms",
        "sender": "Apex HVAC",
        "subject": "Contractor estimate ready for heat pump replacement",
    }]

    ok, summary, meta = run_inbox_rcs_action_triage_card(
        slot="commute",
        dry_run=True,
        test=True,
        db_path=test_db,
        items_override=items,
    )
    assert ok is True
    assert meta["cards_staged"] == 0
    assert meta["dry_run"] is True
    assert "Apex HVAC" in summary

    # DB has 0 rows
    conn = sqlite3.connect(test_db)
    count = conn.execute("SELECT COUNT(*) FROM forge_decisions").fetchone()[0]
    conn.close()
    assert count == 0
    # Seen file not created
    assert not isolate_triage_seen.exists()


# ==============================================================================
# Workflow 2: 48h Family Calendar Radar Card Tests
# ==============================================================================
def test_calendar_radar_silent_when_no_conflicts(test_db: Path) -> None:
    """Calendar radar is silent when events are spaced out and require no prep."""
    now_pt = datetime.now(PT)
    events = [
        {
            "id": "ev-1",
            "summary": "Team Sync",
            "start": (now_pt + timedelta(hours=3)).isoformat(),
            "end": (now_pt + timedelta(hours=4)).isoformat(),
        },
        {
            "id": "ev-2",
            "summary": "Grocery Run",
            "start": (now_pt + timedelta(hours=6)).isoformat(),
            "end": (now_pt + timedelta(hours=7)).isoformat(),
        },
    ]
    ok, summary, meta = run_family_calendar_radar_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        events_override=events,
    )
    assert ok is True
    assert summary == ""
    assert meta["cards_staged"] == 0


def test_calendar_radar_detects_overlap_tight_buffer_and_prep(test_db: Path) -> None:
    """Detects overlap conflict, tight buffer (<15m), and prep keyword."""
    now_pt = datetime.now(PT)
    events: list[dict[str, Any]] = [
        # Event 1 & 2 overlap
        {
            "id": "ev-overlap-1",
            "summary": "Parent Teacher Conference",
            "start": (now_pt + timedelta(hours=2)).isoformat(),
            "end": (now_pt + timedelta(hours=3)).isoformat(),
        },
        {
            "id": "ev-overlap-2",
            "summary": "Dental Cleaning",
            "start": (now_pt + timedelta(hours=2, minutes=30)).isoformat(),
            "end": (now_pt + timedelta(hours=3, minutes=30)).isoformat(),
        },
        # Event 3 starts 5m after Event 2 ends (tight buffer < 15m)
        {
            "id": "ev-tight-buffer",
            "summary": "Piano Lesson",
            "start": (now_pt + timedelta(hours=3, minutes=35)).isoformat(),
            "end": (now_pt + timedelta(hours=4, minutes=15)).isoformat(),
        },
        # Event 4 requires prep (birthday party)
        {
            "id": "ev-prep",
            "summary": "Rosie 4th Birthday Party",
            "start": (now_pt + timedelta(hours=24)).isoformat(),
            "end": (now_pt + timedelta(hours=26)).isoformat(),
        },
    ]

    ok, summary, meta = run_family_calendar_radar_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        events_override=events,
    )
    assert ok is True
    assert meta["cards_staged"] == 1
    issues = meta["issues"]
    issue_types = {iss["type"] for iss in issues}
    assert "overlap" in issue_types
    assert "tight_buffer" in issue_types
    assert "prep" in issue_types

    # Verify staged in SQLite
    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (meta["decision_id"],)).fetchone()
    conn.close()

    assert row is not None
    assert row["category"] == "calendar"
    assert row["channel_id"] == CHANNEL_BROCK_HOUSE
    assert row["handler_type"] == "shell"

    payload = json.loads(row["payload_json"])
    valid_sh, val_err = _validate_decision_shell_payload("shell", payload)
    assert valid_sh is True, f"Shell payload invalid: {val_err}"


def test_calendar_radar_dry_run(test_db: Path) -> None:
    now_pt = datetime.now(PT)
    events = [{
        "id": "ev-prep",
        "summary": "Summer Camp Check-in",
        "start": (now_pt + timedelta(hours=10)).isoformat(),
        "end": (now_pt + timedelta(hours=12)).isoformat(),
    }]
    ok, summary, meta = run_family_calendar_radar_card(
        dry_run=True,
        test=True,
        db_path=test_db,
        events_override=events,
    )
    assert ok is True
    assert meta["cards_staged"] == 0
    assert meta["dry_run"] is True
    assert "Summer Camp Check-in" in summary


# ==============================================================================
# Workflow 3: Afternoon Dinner Pivot Card Tests
# ==============================================================================
def test_dinner_pivot_silent_when_nominal_short_recipe(test_db: Path) -> None:
    """Dinner pivot remains silent when recipe is short (<=30m), even with 2 evening events."""
    now_pt = datetime.now(PT)
    today = now_pt.date()
    # 2 events in 16:30-20:00 PT
    events = [
        {
            "summary": "Rosie Swim Lesson",
            "start": datetime(today.year, today.month, today.day, 16, 45, tzinfo=PT).isoformat(),
            "end": datetime(today.year, today.month, today.day, 17, 30, tzinfo=PT).isoformat(),
        },
        {
            "summary": "Cub Scouts Meeting",
            "start": datetime(today.year, today.month, today.day, 18, 0, tzinfo=PT).isoformat(),
            "end": datetime(today.year, today.month, today.day, 19, 0, tzinfo=PT).isoformat(),
        },
    ]
    # Quick 20-minute recipe
    plan = {"name": "Quick Lemon Herb Salmon", "total_time": 20}

    ok, summary, meta = run_dinner_pivot_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        plan_override=plan,
        events_override=events,
    )
    assert ok is True
    assert summary == ""
    assert meta["cards_staged"] == 0


def test_dinner_pivot_silent_when_single_evening_event_long_recipe(test_db: Path) -> None:
    """Dinner pivot remains silent when only 1 evening event exists, despite a long recipe."""
    now_pt = datetime.now(PT)
    today = now_pt.date()
    events = [{
        "summary": "Evening Walk",
        "start": datetime(today.year, today.month, today.day, 17, 0, tzinfo=PT).isoformat(),
        "end": datetime(today.year, today.month, today.day, 17, 45, tzinfo=PT).isoformat(),
    }]
    plan = {"name": "Slow-Cooker Beef Short Ribs", "total_time": 90}

    ok, summary, meta = run_dinner_pivot_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        plan_override=plan,
        events_override=events,
    )
    assert ok is True
    assert summary == ""
    assert meta["cards_staged"] == 0


def test_dinner_pivot_stages_card_on_unscheduled(test_db: Path) -> None:
    """Dinner pivot triggers when dinner is unscheduled."""
    plan = {"unscheduled": True}

    ok, summary, meta = run_dinner_pivot_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        plan_override=plan,
        events_override=[],
    )
    assert ok is True
    assert meta["cards_staged"] == 1
    assert "unscheduled" in summary.lower()

    # Verify staged in SQLite
    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (meta["decision_id"],)).fetchone()
    conn.close()

    assert row is not None
    assert row["category"] == "mealie"
    assert row["channel_id"] == CHANNEL_SHOPPING
    assert row["handler_type"] == "shell"

    payload = json.loads(row["payload_json"])
    valid_sh, val_err = _validate_decision_shell_payload("shell", payload)
    assert valid_sh is True, f"Shell payload invalid: {val_err}"


def test_dinner_pivot_stages_card_on_density_collision(test_db: Path) -> None:
    """Dinner pivot triggers when >=2 evening events collide with a >30m recipe."""
    now_pt = datetime.now(PT)
    today = now_pt.date()
    events = [
        {
            "summary": "Soccer Practice",
            "start": datetime(today.year, today.month, today.day, 16, 30, tzinfo=PT).isoformat(),
            "end": datetime(today.year, today.month, today.day, 17, 30, tzinfo=PT).isoformat(),
        },
        {
            "summary": "School Board Meeting",
            "start": datetime(today.year, today.month, today.day, 18, 0, tzinfo=PT).isoformat(),
            "end": datetime(today.year, today.month, today.day, 19, 30, tzinfo=PT).isoformat(),
        },
    ]
    plan = {"name": "Homemade Lasagna", "total_time": 60}

    ok, summary, meta = run_dinner_pivot_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        plan_override=plan,
        events_override=events,
    )
    assert ok is True
    assert meta["cards_staged"] == 1
    assert "Homemade Lasagna" in summary
    assert "Soccer Practice" in summary

    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (meta["decision_id"],)).fetchone()
    conn.close()

    assert row is not None
    assert row["category"] == "mealie"
    assert row["channel_id"] == CHANNEL_SHOPPING
    assert row["handler_type"] == "shell"

    payload = json.loads(row["payload_json"])
    valid_sh, val_err = _validate_decision_shell_payload("shell", payload)
    assert valid_sh is True, f"Shell payload invalid: {val_err}"


def test_dinner_pivot_dry_run(test_db: Path) -> None:
    plan = {"unscheduled": True}
    ok, summary, meta = run_dinner_pivot_card(
        dry_run=True,
        test=True,
        db_path=test_db,
        plan_override=plan,
    )
    assert ok is True
    assert meta["cards_staged"] == 0
    assert meta["dry_run"] is True
    assert "unscheduled" in summary.lower()
