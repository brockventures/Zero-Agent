"""
test_usage_telemetry.py - Unit tests for Usage Pattern & Signal-to-Noise Self-Tuning Telemetry Engine.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Ensure tools and workspace on sys.path
_CURRENT_DIR = Path(__file__).resolve().parent
_WORKSPACE_DIR = _CURRENT_DIR.parent
for _p in [str(_WORKSPACE_DIR), str(_CURRENT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools import usage_telemetry
from tools.usage_telemetry import (
    CHANNEL_ZERO_OPS,
    analyze_channel_and_sidecar_engagement,
    analyze_decision_card_funnel,
    apply_job_disable,
    apply_parameter_calibration,
    apply_schedule_shift,
    format_audit_summary,
    run_usage_telemetry_audit,
    synthesize_tuning_recommendations,
)

PT_TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Create a temporary SQLite database initialized with forge_decisions schema."""
    db_file = tmp_path / "test_forge.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE forge_decisions (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                handler_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                is_private INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                resolved_at REAL,
                receipt_text TEXT NOT NULL DEFAULT ''
            )
            """
        )
    return db_file


@pytest.fixture
def temp_schedule(tmp_path: Path) -> Path:
    """Create a temporary schedule.json file with sample jobs."""
    sched_file = tmp_path / "schedule.json"
    initial_jobs = [
        {
            "id": "dinner_pivot",
            "name": "Afternoon Dinner Pivot Card",
            "enabled": True,
            "schedule_type": "daily",
            "hour_pt": 15,
            "minute_pt": 30,
            "prompt": "Run afternoon dinner pivot card",
        },
        {
            "id": "family_calendar_radar",
            "name": "48h Family Calendar Radar Card",
            "enabled": True,
            "schedule_type": "daily",
            "hour_pt": 7,
            "minute_pt": 15,
            "prompt": "Run 48h calendar radar card",
        },
        {
            "id": "actual_budget_anomaly",
            "name": "Actual Budget Anomaly Card",
            "enabled": True,
            "schedule_type": "daily",
            "hour_pt": 11,
            "minute_pt": 0,
            "prompt": "Post budget anomaly report to #finances",
        },
        {
            "id": "midweek_grocery",
            "name": "Mid-Week Staple & Voice Grocery Card",
            "enabled": True,
            "schedule_type": "daily",
            "hour_pt": 10,
            "minute_pt": 0,
            "prompt": "Post grocery card to #shopping",
        },
    ]
    sched_file.write_text(json.dumps({"jobs": initial_jobs}, indent=2), encoding="utf-8")
    return sched_file


def _insert_decision(
    db_path: Path,
    decision_id: str,
    category: str,
    status: str,
    created_at: float,
    resolved_at: float | None = None,
    expires_at: float | None = None,
    handler_type: str = "shell",
    payload: dict | None = None,
    channel_id: int = 1542081375287640084,
) -> None:
    """Helper to insert a test record into forge_decisions."""
    if expires_at is None:
        expires_at = created_at + 7200.0
    if payload is None:
        payload = {"command": "echo test"}

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forge_decisions (
                id, category, title, summary, handler_type, payload_json,
                channel_id, is_private, status, expires_at, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                decision_id,
                category,
                f"Test {category} {decision_id}",
                f"Summary for {decision_id}",
                handler_type,
                json.dumps(payload),
                channel_id,
                status,
                expires_at,
                created_at,
                resolved_at,
            ),
        )


# =========================================================================
# Part 1: analyze_decision_card_funnel Tests
# =========================================================================


def test_funnel_empty_database(tmp_path: Path) -> None:
    """Empty DB or missing table returns zero metrics without errors."""
    empty_db = tmp_path / "nonexistent.db"
    res = analyze_decision_card_funnel(days=7, db_path=empty_db)
    assert res["total"] == 0
    assert res["approved"] == 0
    assert res["edited_approved"] == 0
    assert res["dismissed"] == 0
    assert res["expired"] == 0
    assert res["tap_through_rate"] == 0.0
    assert res["edit_friction_rate"] == 0.0
    assert res["dismissal_rate"] == 0.0
    assert res["expiration_rate"] == 0.0
    assert res["median_latency_seconds"] == 0.0
    assert res["approval_hours_pt"] == []
    assert res["categories"] == {}


def test_funnel_metrics_calculation(temp_db: Path) -> None:
    """Accurately calculates total, rates, median latency, and PT approval hours."""
    now = time.time()
    # 2026-10-30 18:00:00 UTC = 11:00:00 PT
    dt_11_pt = datetime(2026, 10, 30, 18, 0, 0, tzinfo=timezone.utc).timestamp()
    # 2026-10-30 20:00:00 UTC = 13:00:00 PT
    dt_13_pt = datetime(2026, 10, 30, 20, 0, 0, tzinfo=timezone.utc).timestamp()

    # Category: mealie (total: 4 -> 1 approved, 2 edited_approved, 1 dismissed)
    _insert_decision(temp_db, "dec-1", "mealie", "approved", dt_11_pt, resolved_at=dt_11_pt + 120)  # latency: 120s
    _insert_decision(temp_db, "dec-2", "mealie", "edited_approved", dt_13_pt, resolved_at=dt_13_pt + 300)  # latency: 300s
    _insert_decision(temp_db, "dec-3", "mealie", "edited_approved", dt_13_pt + 3600, resolved_at=dt_13_pt + 3960)  # latency: 360s
    _insert_decision(temp_db, "dec-4", "mealie", "dismissed", dt_11_pt + 7200, resolved_at=dt_11_pt + 7260)  # latency: 60s

    # Category: calendar (total: 4 -> 0 approved, 1 dismissed, 3 expired)
    _insert_decision(temp_db, "dec-5", "calendar", "dismissed", now - 3600, resolved_at=now - 3500)
    _insert_decision(temp_db, "dec-6", "calendar", "expired", now - 7200, resolved_at=now - 100)
    _insert_decision(temp_db, "dec-7", "calendar", "expired", now - 14400, resolved_at=now - 100)
    # Pending but expires_at has passed -> counted as expired
    _insert_decision(temp_db, "dec-8", "calendar", "pending", now - 10000, expires_at=now - 1000)

    # Older decision outside 7-day lookback window (should be excluded)
    _insert_decision(temp_db, "dec-old", "mealie", "approved", now - (10 * 86400), resolved_at=now - (10 * 86400) + 50)

    funnel = analyze_decision_card_funnel(days=7, db_path=temp_db)

    # Check overall
    assert funnel["total"] == 8
    assert funnel["approved"] == 1
    assert funnel["edited_approved"] == 2
    assert funnel["dismissed"] == 2
    assert funnel["expired"] == 3
    assert funnel["tap_through_rate"] == round(3 / 8, 4)
    assert funnel["edit_friction_rate"] == round(2 / 8, 4)
    assert funnel["dismissal_rate"] == round(2 / 8, 4)
    assert funnel["expiration_rate"] == round(3 / 8, 4)

    # Check mealie category
    m_metrics = funnel["categories"]["mealie"]
    assert m_metrics["total"] == 4
    assert m_metrics["approved"] == 1
    assert m_metrics["edited_approved"] == 2
    assert m_metrics["dismissed"] == 1
    assert m_metrics["expired"] == 0
    assert m_metrics["tap_through_rate"] == 0.75  # 3 / 4
    assert m_metrics["edit_friction_rate"] == 0.50  # 2 / 4
    assert m_metrics["dismissal_rate"] == 0.25  # 1 / 4
    assert m_metrics["expiration_rate"] == 0.0
    # Latencies: [120, 300, 360, 60] -> sorted: [60, 120, 300, 360] -> median: (120+300)/2 = 210.0
    assert m_metrics["median_latency_seconds"] == 210.0
    # Approval hours in PT: dt_11_pt is 11:00 PT, dt_13_pt is 13:00 PT, dt_13_pt+3600 is 14:00 PT
    assert m_metrics["approval_hours_pt"] == [11, 13, 14]

    # Check calendar category
    c_metrics = funnel["categories"]["calendar"]
    assert c_metrics["total"] == 4
    assert c_metrics["approved"] == 0
    assert c_metrics["edited_approved"] == 0
    assert c_metrics["dismissed"] == 1
    assert c_metrics["expired"] == 3
    assert c_metrics["tap_through_rate"] == 0.0
    assert c_metrics["expiration_rate"] == 0.75  # 3 / 4


# =========================================================================
# Part 2: analyze_channel_and_sidecar_engagement Tests
# =========================================================================


def test_engagement_human_hours_and_unengaged_channels(tmp_path: Path) -> None:
    """Verifies human active hours extraction and detection of unengaged channels."""
    history_file = tmp_path / "channel_history.json"
    sidecar_file = tmp_path / "sidecar_log.json"

    # 1. Prepare channel history
    history_data = {
        # Channel 1: Human active in evening (17:00 and 18:00 PT)
        "1542081375287640084": [
            {
                "author": "Zero",
                "is_bot": True,
                "content": "Morning triage",
                "timestamp": "2026-10-30 08:00:00 UTC",
                "reactions": {},
                "channel_name": "zero-chat",
            },
            {
                "author": "Ryan",
                "is_bot": False,
                "content": "Thanks Zero, good to go.",
                "timestamp": "2026-10-30 17:15:00 UTC",  # 10:15 PT
                "reactions": {},
                "channel_name": "zero-chat",
            },
            {
                "author": "Zero",
                "is_bot": True,
                "content": "Acknowledged.",
                "timestamp": "2026-10-30 18:05:00 UTC",  # 11:05 PT
                "reactions": {"👍": ["Ryan"]},  # Human reaction
                "channel_name": "zero-chat",
            },
        ],
        # Channel 2: Exactly 3 consecutive bot messages with 0 human interaction -> unengaged
        "1544955532765560924": [
            {
                "author": "Zero",
                "is_bot": True,
                "content": "Budget alert 1",
                "timestamp": "2026-10-29 10:00:00 UTC",
                "reactions": {},
                "channel_name": "finances",
            },
            {
                "author": "Zero",
                "is_bot": True,
                "content": "Budget alert 2",
                "timestamp": "2026-10-29 14:00:00 UTC",
                "reactions": {},
                "channel_name": "finances",
            },
            {
                "author": "Zero",
                "is_bot": True,
                "content": "Budget alert 3",
                "timestamp": "2026-10-30 10:00:00 UTC",
                "reactions": {},
                "channel_name": "finances",
            },
        ],
        # Channel 3: 4 consecutive bot messages -> unengaged (eligible for disable)
        "1544955538033348618": [
            {"author": "Zero", "is_bot": True, "content": "Grocery 1", "timestamp": "2026-10-28 10:00:00 UTC", "reactions": {}, "channel_name": "shopping"},
            {"author": "Zero", "is_bot": True, "content": "Grocery 2", "timestamp": "2026-10-29 10:00:00 UTC", "reactions": {}, "channel_name": "shopping"},
            {"author": "Zero", "is_bot": True, "content": "Grocery 3", "timestamp": "2026-10-30 08:00:00 UTC", "reactions": {}, "channel_name": "shopping"},
            {"author": "Zero", "is_bot": True, "content": "Grocery 4", "timestamp": "2026-10-30 12:00:00 UTC", "reactions": {}, "channel_name": "shopping"},
        ],
        # Channel 4: 2 bot messages (streak < 3) -> engaged/nominal
        "1550577908811178095": [
            {"author": "Zero", "is_bot": True, "content": "Dinner card 1", "timestamp": "2026-10-30 15:00:00 UTC", "reactions": {}, "channel_name": "brock-house"},
            {"author": "Zero", "is_bot": True, "content": "Dinner card 2", "timestamp": "2026-10-30 17:00:00 UTC", "reactions": {}, "channel_name": "brock-house"},
        ],
    }
    history_file.write_text(json.dumps(history_data), encoding="utf-8")

    # 2. Prepare sidecar log
    now = time.time()
    sidecar_data = [
        {"job_id": "bridge_watchdog", "status": "ok", "timestamp_epoch": now - 100},
        {"job_id": "bridge_watchdog", "status": "ok", "timestamp_epoch": now - 200},
        {"job_id": "actual_bank_sync", "status": "error", "error": "auth fail", "timestamp_epoch": now - 300},
    ]
    sidecar_file.write_text(json.dumps(sidecar_data), encoding="utf-8")

    engagement = analyze_channel_and_sidecar_engagement(
        days=7,
        channel_history_path=history_file,
        sidecar_history_path=sidecar_file,
    )

    # Active hours extracted: 17:15 UTC -> 10:15 PT (hour 10), 18:05 UTC -> 11:05 PT (hour 11)
    assert 10 in engagement["active_hours_pt"]
    assert 11 in engagement["active_hours_pt"]

    # Unengaged channels (>= 3 consecutive bot msgs)
    unengaged_ids = [ch["channel_id"] for ch in engagement["unengaged_channels"]]
    assert "1544955532765560924" in unengaged_ids  # finances (3 msgs)
    assert "1544955538033348618" in unengaged_ids  # shopping (4 msgs)
    assert "1550577908811178095" not in unengaged_ids  # brock-house (2 msgs < 3)
    assert "1542081375287640084" not in unengaged_ids  # zero-chat has human messages

    # Sidecar summary
    assert engagement["sidecars"]["total_runs"] == 3
    assert engagement["sidecars"]["total_errors"] == 1
    assert engagement["sidecars"]["by_job"]["bridge_watchdog"]["runs"] == 2


# =========================================================================
# Part 3: synthesize_tuning_recommendations Tests
# =========================================================================


def test_synthesize_recommendations_rules(temp_schedule: Path) -> None:
    """Verifies the three tuning rules: schedule shift, job disable, and parameter calibration."""
    # Build a mock funnel
    funnel = {
        "overall": {"approval_hours_pt": [18]},
        "categories": {
            # Rule 1: High Expiration (total >= 3, expiration_rate >= 0.60) -> Schedule shift
            "calendar": {
                "total": 4,
                "approved": 0,
                "edited_approved": 0,
                "dismissed": 1,
                "expired": 3,
                "expiration_rate": 0.75,
                "dismissal_rate": 0.25,
                "edit_friction_rate": 0.0,
                "approval_hours_pt": [],
            },
            # Rule 2A: High Dismissal (total >= 3, dismissal_rate >= 0.60) -> Job disable
            "finances": {
                "total": 5,
                "approved": 1,
                "edited_approved": 0,
                "dismissed": 4,
                "expired": 0,
                "expiration_rate": 0.0,
                "dismissal_rate": 0.80,
                "edit_friction_rate": 0.0,
                "approval_hours_pt": [12],
            },
            # Rule 3: High Edit Friction (edit_friction_rate >= 0.40) -> Parameter calibration
            "mealie": {
                "total": 4,
                "approved": 1,
                "edited_approved": 2,
                "dismissed": 1,
                "expired": 0,
                "expiration_rate": 0.0,
                "dismissal_rate": 0.25,
                "edit_friction_rate": 0.50,
                "approval_hours_pt": [17],
            },
        },
    }

    # Rule 2B: Unengaged channel streak >= 4 -> Job disable
    engagement = {
        "active_hours_pt": [8, 17, 18],
        "unengaged_channels": [
            {
                "channel_id": "1544955538033348618",  # shopping
                "channel_name": "shopping",
                "consecutive_bot_msgs": 4,
                "total_messages": 4,
            }
        ],
    }

    recs = synthesize_tuning_recommendations(funnel, engagement, schedule_path=temp_schedule)

    types = [r["type"] for r in recs]
    assert "schedule_shift" in types
    assert "job_disable" in types
    assert "parameter_calibration" in types

    # Inspect schedule shift rec
    shift_rec = next(r for r in recs if r["type"] == "schedule_shift")
    assert shift_rec["job_id"] == "family_calendar_radar"
    assert "--apply-shift family_calendar_radar" in shift_rec["command"]
    assert "--hour" in shift_rec["command"]
    assert shift_rec["handler_type"] == "shell"
    assert shift_rec["payload"]["command"] == shift_rec["command"]

    # Inspect job disable recs
    disable_recs = [r for r in recs if r["type"] == "job_disable"]
    disable_jobs = [r["job_id"] for r in disable_recs]
    assert "actual_budget_anomaly" in disable_jobs  # from finances dismissal
    assert "midweek_grocery" in disable_jobs  # from shopping unengaged channel

    # Inspect parameter calibration rec
    calib_rec = next(r for r in recs if r["type"] == "parameter_calibration")
    assert calib_rec["category"] == "mealie"
    assert "--calibrate mealie" in calib_rec["command"]


def test_synthesize_recommendations_clean_nominal(temp_schedule: Path) -> None:
    """When all workflows have clean metrics, returns zero recommendations."""
    funnel = {
        "overall": {"approval_hours_pt": [17]},
        "categories": {
            "mealie": {
                "total": 5,
                "approved": 4,
                "edited_approved": 1,
                "dismissed": 0,
                "expired": 0,
                "expiration_rate": 0.0,
                "dismissal_rate": 0.0,
                "edit_friction_rate": 0.20,  # < 0.40
                "approval_hours_pt": [17],
            }
        },
    }
    engagement = {
        "active_hours_pt": [17],
        "unengaged_channels": [],
    }

    recs = synthesize_tuning_recommendations(funnel, engagement, schedule_path=temp_schedule)
    assert len(recs) == 0


# =========================================================================
# Part 4: run_usage_telemetry_audit Tests
# =========================================================================


def test_run_usage_telemetry_audit_end_to_end(temp_db: Path, temp_schedule: Path, tmp_path: Path) -> None:
    """Full audit run stages tuning Decision Cards in #zero-ops and produces concise summary."""
    now = time.time()
    # Insert high expiration cards for calendar (3 of 3 expired)
    _insert_decision(temp_db, "dec-c1", "calendar", "expired", now - 3600, resolved_at=now - 100)
    _insert_decision(temp_db, "dec-c2", "calendar", "expired", now - 7200, resolved_at=now - 100)
    _insert_decision(temp_db, "dec-c3", "calendar", "expired", now - 10800, resolved_at=now - 100)

    # Channel history with 4 unengaged messages in #shopping
    hist_file = tmp_path / "hist.json"
    hist_file.write_text(
        json.dumps({
            "1544955538033348618": [
                {"author": "Zero", "is_bot": True, "content": f"Bot {i}", "timestamp": "2026-10-30 10:00:00 UTC", "reactions": {}, "channel_name": "shopping"}
                for i in range(4)
            ]
        }),
        encoding="utf-8",
    )

    ok, summary, data = run_usage_telemetry_audit(
        days=7,
        dry_run=False,
        test=True,  # Suppress Discord REST calls
        post_cards=True,
        db_path=temp_db,
        channel_history_path=hist_file,
        schedule_path=temp_schedule,
    )

    assert ok is True
    assert data["cards_staged"] >= 2
    assert len(data["decision_ids"]) == data["cards_staged"]

    # Verify staged cards in SQLite at CHANNEL_ZERO_OPS
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM forge_decisions WHERE category = 'telemetry'").fetchall()
        assert len(rows) == data["cards_staged"]
        for r in rows:
            assert r["channel_id"] == CHANNEL_ZERO_OPS
            assert r["status"] == "pending"
            payload = json.loads(r["payload_json"])
            assert "command" in payload
            # Verify single-line CLI invocation without unescaped multi-line python
            assert "\n" not in payload["command"]

    # Verify summary constraints: <= 180 words, zero pipe tables
    assert "|" not in summary
    words = summary.split()
    assert len(words) <= 180
    assert "Usage & Signal-to-Noise Telemetry Engine" in summary


def test_dry_run_does_not_stage(temp_db: Path, temp_schedule: Path, tmp_path: Path) -> None:
    """dry_run=True synthesizes recommendations but stages 0 cards in SQLite."""
    now = time.time()
    _insert_decision(temp_db, "dec-c1", "calendar", "expired", now - 3600, resolved_at=now - 100)
    _insert_decision(temp_db, "dec-c2", "calendar", "expired", now - 7200, resolved_at=now - 100)
    _insert_decision(temp_db, "dec-c3", "calendar", "expired", now - 10800, resolved_at=now - 100)

    ok, summary, data = run_usage_telemetry_audit(
        days=7,
        dry_run=True,
        test=True,
        db_path=temp_db,
        schedule_path=temp_schedule,
    )

    assert ok is True
    assert data["cards_staged"] == 0
    assert len(data["decision_ids"]) == 0
    assert len(data["recommendations"]) >= 1

    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM forge_decisions WHERE category = 'telemetry'").fetchone()
        assert rows[0] == 0


def test_format_audit_summary_bounds() -> None:
    """Verifies that format_audit_summary strictly satisfies word count and pipe prohibition."""
    funnel = {
        "total": 20,
        "approved": 10,
        "edited_approved": 4,
        "dismissed": 3,
        "expired": 3,
        "tap_through_rate": 0.70,
        "edit_friction_rate": 0.20,
    }
    engagement = {
        "active_hours_pt": [8, 9, 10, 11, 14, 15, 16, 17, 18],
        "unengaged_channels": [{"channel_name": "finances", "consecutive_bot_msgs": 4}],
    }
    recommendations = [
        {"title": f"Recommendation {i}", "reason": "High metric anomaly"} for i in range(10)
    ]

    summary = format_audit_summary(funnel, engagement, recommendations, staged_count=10)
    assert "|" not in summary
    assert len(summary.split()) <= 180


# =========================================================================
# Part 5: Action Handlers & CLI Tests
# =========================================================================


def test_apply_schedule_shift(temp_schedule: Path) -> None:
    """apply_schedule_shift modifies hour_pt and minute_pt on target job."""
    res = apply_schedule_shift("dinner_pivot", hour=18, minute=45, schedule_path=temp_schedule)
    assert res["ok"] is True
    assert res["job_id"] == "dinner_pivot"
    assert res["hour_pt"] == 18
    assert res["minute_pt"] == 45

    # Verify on disk
    sched_data = json.loads(temp_schedule.read_text(encoding="utf-8"))
    job = next(j for j in sched_data["jobs"] if j["id"] == "dinner_pivot")
    assert job["hour_pt"] == 18
    assert job["minute_pt"] == 45


def test_apply_schedule_shift_job_not_found(temp_schedule: Path) -> None:
    """apply_schedule_shift returns ok: False for missing job."""
    res = apply_schedule_shift("nonexistent_job", hour=12, minute=0, schedule_path=temp_schedule)
    assert res["ok"] is False
    assert "not found" in res["error"]


def test_apply_job_disable(temp_schedule: Path) -> None:
    """apply_job_disable sets enabled: False for target job."""
    res = apply_job_disable("actual_budget_anomaly", schedule_path=temp_schedule)
    assert res["ok"] is True
    assert res["job_id"] == "actual_budget_anomaly"
    assert res["enabled"] is False

    # Verify on disk
    sched_data = json.loads(temp_schedule.read_text(encoding="utf-8"))
    job = next(j for j in sched_data["jobs"] if j["id"] == "actual_budget_anomaly")
    assert job["enabled"] is False
    assert job["disable_reason"] == "telemetry_tuning"


def test_apply_parameter_calibration() -> None:
    """apply_parameter_calibration returns success receipt."""
    res = apply_parameter_calibration("mealie")
    assert res["ok"] is True
    assert res["target"] == "mealie"
    assert res["status"] == "calibrated"


def test_cli_execution_shift(temp_schedule: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --apply-shift flag dispatches cleanly."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "usage_telemetry.py",
            "--apply-shift",
            "family_calendar_radar",
            "--hour",
            "9",
            "--minute",
            "30",
            "--schedule-path",
            str(temp_schedule),
        ],
    )
    code = usage_telemetry.main()
    assert code == 0

    sched_data = json.loads(temp_schedule.read_text(encoding="utf-8"))
    job = next(j for j in sched_data["jobs"] if j["id"] == "family_calendar_radar")
    assert job["hour_pt"] == 9
    assert job["minute_pt"] == 30


def test_cli_execution_disable(temp_schedule: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI --disable-job flag dispatches cleanly."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "usage_telemetry.py",
            "--disable-job",
            "midweek_grocery",
            "--schedule-path",
            str(temp_schedule),
        ],
    )
    code = usage_telemetry.main()
    assert code == 0

    sched_data = json.loads(temp_schedule.read_text(encoding="utf-8"))
    job = next(j for j in sched_data["jobs"] if j["id"] == "midweek_grocery")
    assert job["enabled"] is False


def test_cli_execution_audit(temp_db: Path, temp_schedule: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI default audit execution dispatches cleanly."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "usage_telemetry.py",
            "--test",
            "--dry-run",
            "--db-path",
            str(temp_db),
            "--schedule-path",
            str(temp_schedule),
        ],
    )
    code = usage_telemetry.main()
    assert code == 0
