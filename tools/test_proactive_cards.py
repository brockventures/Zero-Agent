#!/usr/bin/env python3
"""Unit tests for `tools/proactive_cards.py` and the Thursday Mid-Week Grocery Check workflow."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

import pytest

_CURRENT_DIR = Path(__file__).resolve().parent
_WORKSPACE_DIR = _CURRENT_DIR.parent
for _p in [str(_CURRENT_DIR), str(_WORKSPACE_DIR)]:
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
if "tools" in sys.modules:
    sys.modules["tools"].__path__ = [str(_CURRENT_DIR)]

from tools.forge import _validate_decision_shell_payload, init_forge_db

# Re-ensure worktree is at head of sys.path after forge import
for _p in [str(_CURRENT_DIR), str(_WORKSPACE_DIR)]:
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
if "tools" in sys.modules:
    sys.modules["tools"].__path__ = [str(_CURRENT_DIR)]

from tools.proactive_cards import (  # noqa: E402
    CHANNEL_SHOPPING,
    run_midweek_grocery_card,
)


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    """Initialize a clean, isolated SQLite forge database for each test."""
    db_file = tmp_path / "forge_test.db"
    init_forge_db(db_file)
    return db_file


def test_midweek_grocery_silent_when_zero_items(test_db: Path) -> None:
    """When zero due staples or task items exist, card is strictly silent."""
    ok, summary, meta = run_midweek_grocery_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        items_override={"due_staples": [], "task_items": []},
    )
    assert ok is True
    assert summary == ""
    assert meta["cards_staged"] == 0


def test_midweek_grocery_stages_card_with_staples_and_tasks(test_db: Path) -> None:
    """Aggregates due staples and pending tasks into a single shopping Decision Card."""
    override = {
        "due_staples": [
            {"id": "milk", "name": "Organic Whole Milk", "days_elapsed": 7, "cadence_days": 7},
            {"id": "bread", "name": "Sourdough Bread", "days_elapsed": 8, "cadence_days": 7},
        ],
        "task_items": [
            {"summary": "Avocado oil spray"},
            {"summary": "Greek yogurt"},
        ],
    }

    ok, summary, meta = run_midweek_grocery_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        items_override=override,
    )
    assert ok is True
    assert meta["cards_staged"] == 1
    assert meta["due_staples_count"] == 2
    assert meta["task_items_count"] == 2
    assert meta["total_items"] == 4
    assert "Organic Whole Milk" in summary
    assert "Avocado oil spray" in summary

    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (meta["decision_id"],)).fetchone()
    conn.close()

    assert row is not None
    assert row["category"] == "shopping"
    assert row["channel_id"] == CHANNEL_SHOPPING
    assert row["handler_type"] == "shell"
    assert "**Approve Action:**" in row["summary"]

    payload = json.loads(row["payload_json"])
    valid_sh, val_err = _validate_decision_shell_payload("shell", payload)
    assert valid_sh is True, f"Shell payload invalid: {val_err}"
    assert "whole_foods_afx.py" in payload["command"]
    assert "task_manager.py add" in payload["command"]


def test_midweek_grocery_dry_run(test_db: Path) -> None:
    override = {"due_staples": [{"name": "Bananas"}], "task_items": []}
    ok, summary, meta = run_midweek_grocery_card(
        dry_run=True,
        test=True,
        db_path=test_db,
        items_override=override,
    )
    assert ok is True
    assert meta["cards_staged"] == 0
    assert meta["dry_run"] is True
    assert "Bananas" in summary


def test_proactive_cards_untruncated_15_staples_and_afx_url(test_db: Path) -> None:
    """Verifies that 15 due staples and 8 voice tasks are rendered in full with 0 truncation and a 1-click AFX URL."""
    staples = [
        {
            "id": f"staple_{i}",
            "name": f"Household Staple {i}",
            "search_query": f"365 organic staple {i}",
            "days_elapsed": 14 + i,
            "cadence_days": 7,
            "default_amount": 3 if i == 5 else 1,
            "unit": "COUNT",
            "store": "whole_foods",
        }
        for i in range(1, 16)
    ]
    tasks = [{"summary": f"Extra Grocery Item {j}"} for j in range(1, 9)]

    ok, summary, meta = run_midweek_grocery_card(
        test=True,
        db_path=test_db,
        post_to_discord=False,
        items_override={"due_staples": staples, "task_items": tasks},
    )

    assert ok is True
    assert meta["cards_staged"] == 1
    assert meta["due_staples_count"] == 15
    assert meta["task_items_count"] == 8
    assert meta["total_items"] == 23

    # Verify zero truncation
    assert "more staples" not in summary
    assert "more task items" not in summary
    for i in range(1, 16):
        assert f"Household Staple {i}" in summary
    for j in range(1, 9):
        assert f"Extra Grocery Item {j}" in summary

    # Verify multi-quantity prefix and 1-click AFX link
    assert "3x `Household Staple 5`" in summary
    assert "1-Click Whole Foods Cart (23 items)" in summary
    assert "https://www.amazon.com/afx/ingredients/landingencoded" in summary
    assert meta["afx_url"].startswith("https://www.amazon.com/afx/ingredients/landingencoded")

    # Verify SQLite row and shell payload
    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (meta["decision_id"],)).fetchone()
    conn.close()

    assert row is not None
    assert row["category"] == "shopping"
    assert row["channel_id"] == CHANNEL_SHOPPING
    payload = json.loads(row["payload_json"])
    valid_sh, val_err = _validate_decision_shell_payload("shell", payload)
    assert valid_sh is True, f"Shell payload invalid: {val_err}"
    assert payload["afx_url"] == meta["afx_url"]


def test_thursday_schedule_day_of_week() -> None:
    """Verifies that midweek_grocery_card in data/schedule.json is scheduled for Thursday (day_of_week: 3)."""
    sched_file = _WORKSPACE_DIR / "data" / "schedule.json"
    if not sched_file.exists():
        pytest.skip("data/schedule.json not found")
    jobs = json.loads(sched_file.read_text(encoding="utf-8"))
    job = next((j for j in jobs if j.get("id") == "midweek_grocery_card"), None)
    assert job is not None
    assert job["day_of_week"] == 3
    assert "Thursday" in job["name"]
    assert "Thursday" in job["prompt"]
