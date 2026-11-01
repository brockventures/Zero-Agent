"""Unit tests for Social Review, Core Friends, and Birthday Decision Card staging

and structured Partner column resolution.
"""

import csv
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path = [p for p in sys.path if p not in ("/workspace", "/workspace/tools")]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT))

for _m in list(sys.modules.keys()):
    if _m == "tools" or _m.startswith("tools."):
        del sys.modules[_m]

import tools

tools.__path__ = [str(REPO_ROOT / "tools")]

from tools import birthday_reminder, core_friends_reminder, social_last_seen_review
from tools.forge import (
    _ensure_decision_action_clarity,
    _validate_decision_shell_payload,
    format_decision_card_text,
    init_forge_db,
    render_decision_card_embed,
    stage_decision,
)


# ==============================================================================
# 1. Social Last Seen Review Decision Card Tests
# ==============================================================================


def test_social_last_seen_review_stages_decision_card(tmp_path: Path):
    """Verify format_review_message stages one atomic Decision Card per contact with action clarity."""
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

        # Verify staged Decision Cards - one per contact
        assert mock_stage.call_count == 2

        # Card 1: Alex Rivers
        call_1 = mock_stage.call_args_list[0][1]
        assert call_1["category"] == "social_review"
        assert "Alex Rivers" in call_1["title"]
        assert call_1["handler_type"] == "shell"
        assert call_1["channel_id"] == 1542081375287640084
        assert call_1["post_to_discord"] is True
        assert "**Approve Action:** Update Alex Rivers's Last Seen date to 2026-10-25 in master contacts and Google Sheet." in call_1["summary"]
        assert call_1["payload"]["command"] == "python3 /workspace/tools/social_last_seen_review.py --apply-name 'Alex Rivers'"
        ok_cmd, err_cmd = _validate_decision_shell_payload("shell", call_1["payload"])
        assert ok_cmd is True, err_cmd

        # Card 2: Jordan Hayes
        call_2 = mock_stage.call_args_list[1][1]
        assert call_2["category"] == "social_review"
        assert "Jordan Hayes" in call_2["title"]
        assert call_2["handler_type"] == "shell"
        assert call_2["channel_id"] == 1542081375287640084
        assert call_2["post_to_discord"] is True
        assert "**Approve Action:** Update Jordan Hayes's Last Seen date to 2026-10-26 in master contacts and Google Sheet." in call_2["summary"]
        assert call_2["payload"]["command"] == "python3 /workspace/tools/social_last_seen_review.py --apply-name 'Jordan Hayes'"
        ok_cmd2, err_cmd2 = _validate_decision_shell_payload("shell", call_2["payload"])
        assert ok_cmd2 is True, err_cmd2

        # Verify pending file written
        assert test_pending.exists()
        with open(test_pending, "r", encoding="utf-8") as f:
            staged_data = json.load(f)
        assert len(staged_data) == 2


def test_social_last_seen_review_apply_single_name(tmp_path: Path):
    """Verify apply_pending_updates with target_name updates only the specified contact."""
    test_csv = tmp_path / "friends_master.csv"
    test_pending = tmp_path / "pending_last_seen.json"
    fieldnames = [
        "Name", "Partner", "Core", "Out of Town", "Last Seen", "Birthday",
        "Phone Number", "Email Address", "Physical Address", "Notes & Connections",
    ]
    rows = [
        {
            "Name": "Alex Rivers", "Partner": "", "Core": "TRUE", "Out of Town": "FALSE",
            "Last Seen": "2026-05-01", "Birthday": "01-01", "Phone Number": "",
            "Email Address": "", "Physical Address": "", "Notes & Connections": "",
        },
        {
            "Name": "Jordan Hayes", "Partner": "", "Core": "TRUE", "Out of Town": "FALSE",
            "Last Seen": "-", "Birthday": "02-02", "Phone Number": "",
            "Email Address": "", "Physical Address": "", "Notes & Connections": "",
        },
    ]
    with open(test_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    pending_data = [
        {"name": "Alex Rivers", "proposed_last_seen": "2026-10-25"},
        {"name": "Jordan Hayes", "proposed_last_seen": "2026-10-26"},
    ]
    with open(test_pending, "w", encoding="utf-8") as f:
        json.dump(pending_data, f, indent=2)

    with (
        patch.object(social_last_seen_review, "CSV_PATH", test_csv),
        patch.object(social_last_seen_review, "PENDING_FILE", test_pending),
        patch.object(social_last_seen_review, "MD_PATH", tmp_path / "nonexistent.md"),
        patch("push_friends_to_sheets.push_to_google_sheets", create=True),
    ):
        # 1. Apply single contact update
        ok, msg = social_last_seen_review.apply_pending_updates(target_name="Alex Rivers")
        assert ok is True
        assert "Alex Rivers" in msg

        # Verify CSV state: Alex updated, Jordan not updated
        with open(test_csv, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        csv_map = {r["Name"]: r["Last Seen"] for r in reader}
        assert csv_map["Alex Rivers"] == "2026-10-25"
        assert csv_map["Jordan Hayes"] == "-"

        # Verify pending file still contains Jordan Hayes
        assert test_pending.exists()
        with open(test_pending, "r", encoding="utf-8") as f:
            remaining = json.load(f)
        assert len(remaining) == 1
        assert remaining[0]["name"] == "Jordan Hayes"

        # 2. Apply remaining contact update
        ok2, msg2 = social_last_seen_review.apply_pending_updates(target_name="Jordan Hayes")
        assert ok2 is True
        assert "Jordan Hayes" in msg2

        with open(test_csv, "r", encoding="utf-8") as f:
            reader2 = list(csv.DictReader(f))
        csv_map2 = {r["Name"]: r["Last Seen"] for r in reader2}
        assert csv_map2["Jordan Hayes"] == "2026-10-26"

        # Verify pending file removed when empty
        assert not test_pending.exists()


def test_social_last_seen_review_apply_name_not_found(tmp_path: Path):
    """Verify apply_pending_updates returns error if target_name is not in pending file."""
    test_pending = tmp_path / "pending_last_seen.json"
    with open(test_pending, "w", encoding="utf-8") as f:
        json.dump([{"name": "Alex Rivers", "proposed_last_seen": "2026-10-25"}], f)

    with patch.object(social_last_seen_review, "PENDING_FILE", test_pending):
        ok, msg = social_last_seen_review.apply_pending_updates(target_name="Nonexistent Contact")
        assert ok is False
        assert "No pending Last Seen update found for 'Nonexistent Contact'" in msg


def test_forge_ensure_decision_action_clarity_direct(tmp_path: Path):
    """Verify _ensure_decision_action_clarity preserves explicit action lines and derives missing ones."""
    # 1. Existing explicit Approve Action line preserved
    explicit_summary = "Candidate review.\n\n**Approve Action:** Update contacts master."
    res = _ensure_decision_action_clarity(explicit_summary, "shell", {"command": "echo 1"})
    assert res == explicit_summary

    # 2. Shell command derived
    res_shell = _ensure_decision_action_clarity("Review candidate.", "shell", {"command": "python3 script.py"})
    assert "**Approve Action:** Run `python3 script.py`" in res_shell

    # 3. Explicit payload action overrides default derivation
    res_custom = _ensure_decision_action_clarity(
        "Deploy update.", "shell", {"command": "deploy.sh", "action": "Deploy v2 stack to production"}
    )
    assert "**Approve Action:** Deploy v2 stack to production" in res_custom

    # 4. Email handler derived
    res_mail = _ensure_decision_action_clarity("Send notice.", "send_mail", {"to": "ops@example.com", "subject": "Alert"})
    assert "**Approve Action:** Send email to `ops@example.com` (Subject: Alert)" in res_mail

    # 5. Docker compose handler derived
    res_docker = _ensure_decision_action_clarity("Restart service.", "docker_compose", {"service": "homeassistant", "action": "restart"})
    assert "**Approve Action:** Execute `docker compose restart homeassistant`" in res_docker

    # 6. GitHub PR merge derived
    res_pr = _ensure_decision_action_clarity("Merge branch.", "gh_merge", {"pr_number": 42})
    assert "**Approve Action:** Merge PR #42" in res_pr

    # 7. Bridge reload derived
    res_reload = _ensure_decision_action_clarity("Modified bridge.", "bridge_reload", {})
    assert "**Approve Action:** Trigger in-place bridge reload" in res_reload

    # 8. File delete derived
    res_del = _ensure_decision_action_clarity("Clean scratch.", "file_delete", {"paths": ["/tmp/a.log", "/tmp/b.log"]})
    assert "**Approve Action:** Delete /tmp/a.log, /tmp/b.log" in res_del

    # 9. Forge enqueue derived
    res_enq = _ensure_decision_action_clarity("Plan ready.", "forge_enqueue", {"title": "Build Sensor Hub"})
    assert "**Approve Action:** Enqueue FORGE build `Build Sensor Hub`" in res_enq


def test_forge_stage_decision_enforces_action_clarity_in_db_and_embed(tmp_path: Path):
    """Verify stage_decision enriches summary with **Approve Action:** and renders in embeds."""
    db_p = tmp_path / "forge_test.db"
    init_forge_db(db_p)

    staged = stage_decision(
        category="homelab",
        title="Restart Plex Container",
        summary="Plex Transcoder wedged on GPU session.",
        handler_type="docker_compose",
        payload={"service": "plex", "action": "restart"},
        channel_id=1542081375287640084,
        db_path=db_p,
    )
    assert staged["ok"] is True

    # Check embed rendering
    embed = render_decision_card_embed({
        "status": "pending",
        "category": "homelab",
        "title": "Restart Plex Container",
        "summary": "Plex Transcoder wedged on GPU session.",
        "handler_type": "docker_compose",
        "payload_json": json.dumps({"service": "plex", "action": "restart"}),
    })
    assert "**Approve Action:** Execute `docker compose restart plex`" in embed["description"]

    # Check text formatting
    text = format_decision_card_text({
        "status": "pending",
        "category": "homelab",
        "title": "Restart Plex Container",
        "summary": "Plex Transcoder wedged on GPU session.",
        "handler_type": "docker_compose",
        "payload_json": json.dumps({"service": "plex", "action": "restart"}),
    })
    assert "**Approve Action:** Execute `docker compose restart plex`" in text


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
        assert "python3 /workspace/tools/task_manager.py add" in payload["command"]
        assert "Reach out to Jordan & Taylor Blake" in payload["command"]
        assert payload["cwd"] == "/workspace"
        assert "**Approve Action:** Create follow-up task to reach out to Jordan & Taylor Blake" in kwargs["summary"]

        ok_cmd, err_cmd = _validate_decision_shell_payload("shell", payload)
        assert ok_cmd is True, err_cmd


def test_core_friends_stages_atomic_cards_for_multiple_friends(tmp_path: Path):
    """Verify check_core_friends_unseen stages separate atomic Decision Cards for each friend."""
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
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-01-01",
            "Birthday": "07-15",
            "Phone Number": "555-0101",
            "Email Address": "jordan@example.com",
            "Physical Address": "101 Documentation St",
            "Notes & Connections": "Local close friend.",
        },
        {
            "Name": "Morgan Stone",
            "Partner": "",
            "Core": "TRUE",
            "Out of Town": "FALSE",
            "Last Seen": "2026-02-01",
            "Birthday": "08-20",
            "Phone Number": "555-0102",
            "Email Address": "morgan@example.com",
            "Physical Address": "102 Documentation St",
            "Notes & Connections": "Local close friend.",
        },
    ]
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with (
        patch.object(core_friends_reminder, "CSV_PATH", csv_file),
        patch("tools.forge.stage_decision", side_effect=[
            {"ok": True, "id": "dec-core-1"},
            {"ok": True, "id": "dec-core-2"},
        ]) as mock_stage,
    ):
        has_friends, msg, qualifying = core_friends_reminder.check_core_friends_unseen(
            weeks=8, as_of_date="2026-10-30", stage=True
        )

        assert has_friends is True
        assert len(qualifying) == 2
        assert mock_stage.call_count == 2
        assert core_friends_reminder.LAST_DECISION_STAGED is True
        assert all(p.get("_decision_staged") is True for p in qualifying)

        calls = mock_stage.call_args_list
        # Call 1: Jordan Blake
        kwargs1 = calls[0][1]
        assert kwargs1["category"] == "core_friends"
        assert "Jordan Blake" in kwargs1["title"]
        assert "**Approve Action:** Create follow-up task to reach out to Jordan Blake" in kwargs1["summary"]
        assert "Reach out to Jordan Blake" in kwargs1["payload"]["command"]

        # Call 2: Morgan Stone
        kwargs2 = calls[1][1]
        assert kwargs2["category"] == "core_friends"
        assert "Morgan Stone" in kwargs2["title"]
        assert "**Approve Action:** Create follow-up task to reach out to Morgan Stone" in kwargs2["summary"]
        assert "Reach out to Morgan Stone" in kwargs2["payload"]["command"]


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
