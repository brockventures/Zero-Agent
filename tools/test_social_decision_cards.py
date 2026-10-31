"""Unit tests for Social Review, Core Friends, and Birthday Decision Card staging

and structured Partner column resolution.
"""

import csv
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import birthday_reminder, core_friends_reminder, social_last_seen_review
from tools.forge import _validate_decision_shell_payload


# ==============================================================================
# 1. Social Last Seen Review Decision Card Tests
# ==============================================================================


def test_social_last_seen_review_stages_decision_card(tmp_path: Path):
    """Verify format_review_message stages a Decision Card when updates are identified."""
    test_pending = tmp_path / "pending_last_seen.json"
    updates = [
        {
            "name": "Alex Rivers",
            "location": "Local Cafe",
            "source": "Calendar",
            "event_summary": "Coffee catch-up",
            "event_date": "2026-10-25",
            "current_last_seen": "2026-05-01",
            "proposed_last_seen": "2026-10-25",
        },
        {
            "name": "Jordan Hayes",
            "source": "Text Message",
            "event_summary": "Project discussion",
            "event_date": "2026-10-26",
            "current_last_seen": "-",
            "proposed_last_seen": "2026-10-26",
        },
    ]

    with (
        patch.object(social_last_seen_review, "PENDING_FILE", test_pending),
        patch.object(social_last_seen_review, "DATA_DIR", tmp_path),
        patch("tools.forge.stage_decision", return_value={"ok": True, "id": "dec-123"}) as mock_stage,
    ):
        has_events, msg = social_last_seen_review.format_review_message(updates, stage=True)

        assert has_events is True
        assert "Alex Rivers" in msg
        assert "Jordan Hayes" in msg
        assert "[CHOICES:" not in msg  # Upgraded away from legacy choices tag
        assert social_last_seen_review.LAST_DECISION_STAGED is True
        assert all(u.get("_decision_staged") is True for u in updates)

        # Verify staged Decision Card parameters
        assert mock_stage.called
        kwargs = mock_stage.call_args[1]
        assert kwargs["category"] == "social_review"
        assert kwargs["title"] == "Confirm & Apply Last Seen Updates"
        assert kwargs["handler_type"] == "shell"
        assert kwargs["channel_id"] == 1542081375287640084
        assert kwargs["post_to_discord"] is True

        payload = kwargs["payload"]
        assert payload["command"] == "python3 /workspace/tools/social_last_seen_review.py --apply"
        assert payload["cwd"] == "/workspace"

        # Validate shell command syntax
        ok_cmd, err_cmd = _validate_decision_shell_payload("shell", payload)
        assert ok_cmd is True, err_cmd

        # Verify pending file written
        assert test_pending.exists()
        with open(test_pending, "r", encoding="utf-8") as f:
            staged_data = json.load(f)
        assert len(staged_data) == 2


def test_social_last_seen_review_empty_updates_no_decision_card():
    """Verify format_review_message does not stage Decision Card when updates are empty."""
    with patch("tools.forge.stage_decision") as mock_stage:
        has_events, msg = social_last_seen_review.format_review_message([], stage=True)
        assert has_events is False
        assert "No qualifying social events" in msg
        assert not mock_stage.called
        assert social_last_seen_review.LAST_DECISION_STAGED is False


def test_social_last_seen_review_fallback_on_stage_failure(tmp_path: Path):
    """Verify format_review_message falls back to [CHOICES] if Decision Card staging fails."""
    test_pending = tmp_path / "pending_last_seen.json"
    updates = [
        {
            "name": "Morgan Stone",
            "source": "Calendar",
            "event_summary": "Lunch catch-up",
            "event_date": "2026-10-25",
            "current_last_seen": "2026-06-01",
            "proposed_last_seen": "2026-10-25",
        }
    ]

    with (
        patch.object(social_last_seen_review, "PENDING_FILE", test_pending),
        patch.object(social_last_seen_review, "DATA_DIR", tmp_path),
        patch("tools.forge.stage_decision", side_effect=RuntimeError("SQLite locked")),
    ):
        has_events, msg = social_last_seen_review.format_review_message(updates, stage=True)
        assert has_events is True
        assert "Morgan Stone" in msg
        assert "[CHOICES: Confirm & Apply Last Seen Updates | Ignore Last Seen Updates]" in msg
        assert social_last_seen_review.LAST_DECISION_STAGED is False
        assert all(u.get("_decision_staged") is False for u in updates)


# ==============================================================================
# 2. Structured 'Partner' Column Resolution Tests
# ==============================================================================


def test_core_friends_structured_partner_column_direct():
    """Verify find_partner resolves partner directly from structured Partner column."""
    contacts = [
        {
            "Name": "Alex Vance",
            "Partner": "Eli Vance",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Physical Address": "100 Documentation Blvd",
            "Notes & Connections": "Colleague in physics research lab.",
        },
        {
            "Name": "Eli Vance",
            "Partner": "Alex Vance",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Physical Address": "100 Documentation Blvd",
            "Notes & Connections": "Lab director.",
        },
    ]

    partner = core_friends_reminder.find_partner(contacts[0], contacts)
    assert partner is not None
    assert partner["Name"] == "Eli Vance"

    display_name = core_friends_reminder.format_couple_display_name(contacts[0], partner)
    assert display_name == "Alex & Eli Vance"


def test_core_friends_structured_partner_column_reciprocal():
    """Verify find_partner resolves reciprocal partner when one contact has Partner empty."""
    contacts = [
        {
            "Name": "Casey Miller",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Physical Address": "200 Documentation Way",
            "Notes & Connections": "Architecture designer.",
        },
        {
            "Name": "Riley Miller",
            "Partner": "Casey Miller",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Physical Address": "200 Documentation Way",
            "Notes & Connections": "Civil engineer.",
        },
    ]

    partner = core_friends_reminder.find_partner(contacts[0], contacts)
    assert partner is not None
    assert partner["Name"] == "Riley Miller"

    display_name = core_friends_reminder.format_couple_display_name(contacts[0], partner)
    assert display_name == "Casey & Riley Miller"


def test_core_friends_structured_partner_not_in_contacts_list():
    """Verify find_partner creates synthetic record if Partner is named but has no separate row."""
    contacts = [
        {
            "Name": "Sam Taylor",
            "Partner": "Taylor Brooks",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Notes & Connections": "Solo contact entry.",
        }
    ]

    partner = core_friends_reminder.find_partner(contacts[0], contacts)
    assert partner is not None
    assert partner["Name"] == "Taylor Brooks"

    display_name = core_friends_reminder.format_couple_display_name(contacts[0], partner)
    assert display_name == "Sam Taylor & Taylor Brooks"


def test_core_friends_fallback_to_notes_regex_when_partner_column_empty():
    """Verify find_partner falls back to notes regex patterns when Partner column is absent."""
    contacts = [
        {
            "Name": "David Smith",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Notes & Connections": "Married to Sarah Smith; close childhood friend.",
        },
        {
            "Name": "Sarah Smith",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Notes & Connections": "Pediatrician.",
        },
    ]

    partner = core_friends_reminder.find_partner(contacts[0], contacts)
    assert partner is not None
    assert partner["Name"] == "Sarah Smith"

    display_name = core_friends_reminder.format_couple_display_name(contacts[0], partner)
    assert display_name == "David & Sarah Smith"


# ==============================================================================
# 3. Core Friends Decision Card Staging Tests
# ==============================================================================


def test_core_friends_stages_decision_card(tmp_path: Path):
    """Verify check_core_friends_unseen stages Decision Card when qualifying friends found."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name",
        "Partner",
        "Core",
        "Out of Town",
        "Last Seen",
        "Birthday",
        "Phone Number",
        "Email Address",
        "Physical Address",
        "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Jordan Blake",
            "Partner": "Taylor Blake",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-01-01",  # >8 weeks ago as of 2026-10-30
            "Birthday": "07-15",
            "Phone Number": "555-0101",
            "Email Address": "jordan@example.com",
            "Physical Address": "101 Documentation St",
            "Notes & Connections": "Local close friend.",
        },
        {
            "Name": "Taylor Blake",
            "Partner": "Jordan Blake",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-01-01",
            "Birthday": "08-20",
            "Phone Number": "555-0102",
            "Email Address": "taylor@example.com",
            "Physical Address": "101 Documentation St",
            "Notes & Connections": "Local close friend.",
        },
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(core_friends_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision", return_value={"ok": True, "id": "dec-core-1"}) as mock_stage,
    ):
        has_friends, msg, qualifying = core_friends_reminder.check_core_friends_unseen(
            weeks=8, as_of_date="2026-10-30", stage=True
        )

        assert has_friends is True
        assert len(qualifying) == 1  # Jordan & Taylor paired together
        assert "Jordan & Taylor Blake" in msg
        assert "[CHOICES:" not in msg  # Upgraded to Decision Card
        assert core_friends_reminder.LAST_DECISION_STAGED is True
        assert qualifying[0].get("_decision_staged") is True

        assert mock_stage.called
        kwargs = mock_stage.call_args[1]
        assert kwargs["category"] == "core_friends"
        assert kwargs["channel_id"] == 1542081375287640084
        assert kwargs["post_to_discord"] is True
        assert kwargs["handler_type"] == "shell"

        payload = kwargs["payload"]
        assert "python3 /workspace/tools/core_friends_reminder.py --weeks 8" in payload["command"]
        assert payload["cwd"] == "/workspace"

        ok_cmd, err_cmd = _validate_decision_shell_payload("shell", payload)
        assert ok_cmd is True, err_cmd


def test_core_friends_no_unseen_no_decision_card(tmp_path: Path):
    """Verify check_core_friends_unseen does not stage Decision Card when all seen recently."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name",
        "Partner",
        "Core",
        "Out of Town",
        "Last Seen",
        "Birthday",
        "Phone Number",
        "Email Address",
        "Physical Address",
        "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Recent Friend",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-10-28",  # Seen 2 days ago
            "Birthday": "07-15",
            "Phone Number": "555-0101",
            "Email Address": "recent@example.com",
            "Physical Address": "101 Documentation St",
            "Notes & Connections": "Saw recently.",
        }
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(core_friends_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        has_friends, msg, qualifying = core_friends_reminder.check_core_friends_unseen(
            weeks=8, as_of_date="2026-10-30", stage=True
        )

        assert has_friends is False
        assert len(qualifying) == 0
        assert "All local Core friends have been seen" in msg
        assert not mock_stage.called
        assert core_friends_reminder.LAST_DECISION_STAGED is False


def test_core_friends_fallback_on_stage_failure(tmp_path: Path):
    """Verify check_core_friends_unseen falls back to [CHOICES] when stage_decision raises."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name",
        "Partner",
        "Core",
        "Out of Town",
        "Last Seen",
        "Birthday",
        "Phone Number",
        "Email Address",
        "Physical Address",
        "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Unseen Friend",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-01-01",
            "Birthday": "07-15",
            "Phone Number": "555-0101",
            "Email Address": "unseen@example.com",
            "Physical Address": "101 Documentation St",
            "Notes & Connections": "Haven't seen in a while.",
        }
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(core_friends_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision", side_effect=RuntimeError("Discord unreachable")),
    ):
        has_friends, msg, qualifying = core_friends_reminder.check_core_friends_unseen(
            weeks=8, as_of_date="2026-10-30", stage=True
        )

        assert has_friends is True
        assert len(qualifying) == 1
        assert "[CHOICES: View Full Friends Sheet | Remind Me in 2 Weeks | Dismiss]" in msg
        assert core_friends_reminder.LAST_DECISION_STAGED is False
        assert qualifying[0].get("_decision_staged") is False


# ==============================================================================
# 4. Birthday Reminder Decision Card & Partner Resolution Tests
# ==============================================================================


def test_birthday_reminder_structured_partner_resolution(tmp_path: Path):
    """Verify check_birthdays resolves partner from Partner column and includes in alert."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name",
        "Partner",
        "Core",
        "Out of Town",
        "Last Seen",
        "Birthday",
        "Phone Number",
        "Email Address",
        "Physical Address",
        "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Avery Chen",
            "Partner": "Jordan Chen",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-09-01",
            "Birthday": "10-30",
            "Phone Number": "555-0199",
            "Email Address": "avery@example.com",
            "Physical Address": "500 Documentation Ave",
            "Notes & Connections": "Avid marathon runner.",
        }
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(birthday_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision", return_value={"ok": True, "id": "dec-bday-1"}) as mock_stage,
    ):
        has_bday, msg, matched = birthday_reminder.check_birthdays(target_date="10-30", stage=True)

        assert has_bday is True
        assert len(matched) == 1
        assert matched[0]["Partner"] == "Jordan Chen"
        assert "Avery Chen" in msg
        assert "Jordan Chen" in msg
        assert "[CHOICES:" not in msg  # Upgraded to Decision Card
        assert birthday_reminder.LAST_DECISION_STAGED is True
        assert matched[0].get("_decision_staged") is True

        assert mock_stage.called
        kwargs = mock_stage.call_args[1]
        assert kwargs["category"] == "birthday"
        assert "Avery Chen" in kwargs["title"]
        assert kwargs["channel_id"] == 1542081375287640084
        assert kwargs["post_to_discord"] is True
        assert kwargs["handler_type"] == "shell"

        payload = kwargs["payload"]
        assert "python3 /workspace/tools/birthday_reminder.py --date 10-30" in payload["command"]
        assert payload["cwd"] == "/workspace"

        ok_cmd, err_cmd = _validate_decision_shell_payload("shell", payload)
        assert ok_cmd is True, err_cmd


def test_birthday_reminder_no_birthdays_no_card(tmp_path: Path):
    """Verify check_birthdays does not stage Decision Card when no birthdays match."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name",
        "Partner",
        "Core",
        "Out of Town",
        "Last Seen",
        "Birthday",
        "Phone Number",
        "Email Address",
        "Physical Address",
        "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Summer Friend",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-09-01",
            "Birthday": "07-04",
            "Phone Number": "555-0155",
            "Email Address": "summer@example.com",
            "Physical Address": "500 Documentation Ave",
            "Notes & Connections": "Summer birthdays.",
        }
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(birthday_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        has_bday, msg, matched = birthday_reminder.check_birthdays(target_date="10-30", stage=True)

        assert has_bday is False
        assert len(matched) == 0
        assert "No birthdays on" in msg
        assert not mock_stage.called
        assert birthday_reminder.LAST_DECISION_STAGED is False


def test_birthday_reminder_fallback_on_stage_failure(tmp_path: Path):
    """Verify check_birthdays falls back to [CHOICES] if stage_decision raises."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name",
        "Partner",
        "Core",
        "Out of Town",
        "Last Seen",
        "Birthday",
        "Phone Number",
        "Email Address",
        "Physical Address",
        "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Fallback Friend",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-09-01",
            "Birthday": "10-30",
            "Phone Number": "555-0144",
            "Email Address": "fallback@example.com",
            "Physical Address": "500 Documentation Ave",
            "Notes & Connections": "Birthday alert test.",
        }
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(birthday_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision", side_effect=RuntimeError("Database lock")),
    ):
        has_bday, msg, matched = birthday_reminder.check_birthdays(target_date="10-30", stage=True)

        assert has_bday is True
        assert len(matched) == 1
        assert '[CHOICES: Text Fallback "Happy birthday!" | Remind Me Later]' in msg
        assert birthday_reminder.LAST_DECISION_STAGED is False
        assert matched[0].get("_decision_staged") is False


# ==============================================================================
# 5. Pytest Default & Explicit Staging Flag Tests
# ==============================================================================


def test_social_last_seen_review_pytest_default_suppresses_staging(tmp_path: Path):
    """Under pytest, format_review_message with stage=None does NOT stage a Decision Card."""
    test_pending = tmp_path / "pending_last_seen.json"
    updates = [
        {
            "name": "Alex Rivers",
            "source": "Calendar",
            "event_summary": "Coffee catch-up",
            "event_date": "2026-10-25",
            "current_last_seen": "2026-05-01",
            "proposed_last_seen": "2026-10-25",
        }
    ]

    with (
        patch.object(social_last_seen_review, "PENDING_FILE", test_pending),
        patch.object(social_last_seen_review, "DATA_DIR", tmp_path),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        has_events, msg = social_last_seen_review.format_review_message(updates)  # stage=None default

        assert has_events is True
        assert not mock_stage.called
        assert social_last_seen_review.LAST_DECISION_STAGED is False
        assert updates[0].get("_decision_staged") is False
        assert "[CHOICES:" in msg


def test_core_friends_pytest_default_suppresses_staging(tmp_path: Path):
    """Under pytest, check_core_friends_unseen with stage=None does NOT stage a Decision Card."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name", "Partner", "Core", "Out of Town", "Last Seen", "Birthday",
        "Phone Number", "Email Address", "Physical Address", "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Jordan Blake",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-01-01",
            "Birthday": "07-15",
            "Phone Number": "555-0101",
            "Email Address": "jordan@example.com",
            "Physical Address": "101 Documentation St",
            "Notes & Connections": "Local close friend.",
        }
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(core_friends_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        has_friends, msg, qualifying = core_friends_reminder.check_core_friends_unseen(
            weeks=8, as_of_date="2026-10-30"  # stage=None default
        )

        assert has_friends is True
        assert len(qualifying) == 1
        assert not mock_stage.called
        assert core_friends_reminder.LAST_DECISION_STAGED is False
        assert qualifying[0].get("_decision_staged") is False
        assert "[CHOICES:" in msg


def test_birthday_reminder_pytest_default_suppresses_staging(tmp_path: Path):
    """Under pytest, check_birthdays with stage=None does NOT stage a Decision Card."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name", "Partner", "Core", "Out of Town", "Last Seen", "Birthday",
        "Phone Number", "Email Address", "Physical Address", "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Avery Chen",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-09-01",
            "Birthday": "10-30",
            "Phone Number": "555-0199",
            "Email Address": "avery@example.com",
            "Physical Address": "500 Documentation Ave",
            "Notes & Connections": "Avid marathon runner.",
        }
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(birthday_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        has_bday, msg, matched = birthday_reminder.check_birthdays(target_date="10-30")  # stage=None default

        assert has_bday is True
        assert len(matched) == 1
        assert not mock_stage.called
        assert birthday_reminder.LAST_DECISION_STAGED is False
        assert matched[0].get("_decision_staged") is False
        assert "[CHOICES:" in msg


def test_explicit_stage_false_suppresses_staging(tmp_path: Path):
    """Explicit stage=False suppresses staging regardless of environment."""
    csv_file = tmp_path / "friends_master.csv"
    fieldnames = [
        "Name", "Partner", "Core", "Out of Town", "Last Seen", "Birthday",
        "Phone Number", "Email Address", "Physical Address", "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Avery Chen",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-09-01",
            "Birthday": "10-30",
            "Phone Number": "555-0199",
            "Email Address": "avery@example.com",
            "Physical Address": "500 Documentation Ave",
            "Notes & Connections": "Avid marathon runner.",
        }
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(birthday_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        has_bday, msg, matched = birthday_reminder.check_birthdays(target_date="10-30", stage=False)

        assert has_bday is True
        assert not mock_stage.called
        assert birthday_reminder.LAST_DECISION_STAGED is False
        assert matched[0].get("_decision_staged") is False


def test_should_stage_helper_matrix():
    """Verify _should_stage across all three modules."""
    for mod in (birthday_reminder, social_last_seen_review, core_friends_reminder):
        assert mod._should_stage(True) is True
        assert mod._should_stage(False) is False
        # Running under pytest, so stage=None must return False
        assert mod._should_stage(None) is False
