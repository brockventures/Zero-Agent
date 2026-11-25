"""Unit tests for BRIEF Step 3 hash-verified Approval Gate state machine."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.brief.actions import canonical_proposal_hash, validate_action_args
from tools.brief.config import BriefConfig
from tools.brief.gate import (
    BriefApprovalGate,
    BriefGateStore,
    _build_decision_card_copy,
    _extract_dismissal_keys,
    _format_readable_pt_range,
    handle_forge_decision_approve,
    handle_forge_decision_dismiss,
    write_proposal_atomic,
)
from tools.brief.queue import BriefQueue

PT_TZ = ZoneInfo("America/Los_Angeles")


def _ts_pt(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=PT_TZ).timestamp()


def _make_config(
    tmp_path: Path,
    *,
    dry_run: bool = False,
    max_proactive: int = 0,
    max_ttl: int = 604800,
) -> BriefConfig:
    state_dir = tmp_path / "state"
    memory_dir = tmp_path / "memory"
    state_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    return BriefConfig(
        state_dir=state_dir,
        memory_dir=memory_dir,
        allowlisted_emails=["owner@example.com"],
        dry_run=dry_run,
        max_proactive_messages_per_day=max_proactive,
        gate_max_proposal_ttl_seconds=max_ttl,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        timezone="America/Los_Angeles",
    )


def _sample_create_proposal(
    proposal_id: str = "prop-cal-1",
    *,
    expires_at: str = "2026-11-24T18:00:00-08:00",
    from_session: str = "intake/gmail/thread-42",
    urgent: bool = False,
    summary: str = "Team Sync",
) -> dict[str, Any]:
    return {
        "id": proposal_id,
        "action": "calendar.create",
        "args": {
            "summary": summary,
            "start": "2026-11-25T10:00:00-08:00",
            "end": "2026-11-25T11:00:00-08:00",
            "attendees": ["coach@example.org"],
            "location": "Main Field",
        },
        "reason": "Owner requested scheduling follow-up.",
        "expires_at": expires_at,
        "from_session": from_session,
        "urgent": urgent,
    }


class _FakeCalendarApi:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.updated: list[tuple[str, str, dict[str, Any]]] = []
        self.should_raise: Exception | None = None

    def create_event(self, calendar_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.should_raise is not None:
            raise self.should_raise
        self.created.append((calendar_id, dict(payload)))
        return {
            "id": "evt-created-100",
            "htmlLink": "https://calendar.google.com/event?eid=evt-created-100",
            "summary": payload.get("summary", ""),
            "start": payload.get("start"),
            "end": payload.get("end"),
        }

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.should_raise is not None:
            raise self.should_raise
        self.updated.append((calendar_id, event_id, dict(payload)))
        return {
            "id": event_id,
            "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
            "summary": payload.get("summary", "Updated Event"),
            "start": payload.get("start"),
            "end": payload.get("end"),
        }


def _fetch_internal_events(queue: BriefQueue) -> list[dict[str, Any]]:
    with queue._tx() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE source = 'internal' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def test_atomic_write_and_ignore_tmp_and_dotfiles(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, dry_run=True)
    now_ts = _ts_pt(2026, 11, 24, 10, 0)

    # Atomic write leaves <id>.json and zero .tmp leftovers
    written = write_proposal_atomic(cfg.proposals_pending_dir, _sample_create_proposal("prop-atomic-1"))
    assert written == cfg.proposals_pending_dir / "prop-atomic-1.json"
    assert written.is_file()
    assert not (cfg.proposals_pending_dir / "prop-atomic-1.json.tmp").exists()

    # Unsafe IDs must be rejected by write_proposal_atomic
    for bad_id in ("../escape", "a/b", ".hidden", "foo..bar", ""):
        with pytest.raises(ValueError):
            write_proposal_atomic(cfg.proposals_pending_dir, {"id": bad_id})

    # Place .tmp and dotfile noise in pending/
    (cfg.proposals_pending_dir / "in-progress.json.tmp").write_text("{partial", encoding="utf-8")
    (cfg.proposals_pending_dir / ".hidden.json").write_text('{"id": "hidden"}', encoding="utf-8")

    staged_calls: list[dict[str, Any]] = []

    def _stage(**kwargs: Any) -> dict[str, Any]:
        staged_calls.append(kwargs)
        return {"ok": True, "decision_id": f"dec-{len(staged_calls)}"}

    gate = BriefApprovalGate(cfg, stage_card_fn=_stage, now_fn=lambda: now_ts)
    res1 = gate.scan_pending_proposals(now=now_ts)
    assert res1["scanned"] == 1
    assert res1["staged"] == 1
    assert res1["staged_ids"] == ["prop-atomic-1"]
    assert len(staged_calls) == 1
    assert staged_calls[0]["handler_type"] == "brief_proposal"
    assert staged_calls[0]["title"] == "Add to Calendar: Team Sync"
    summary_text = staged_calls[0]["summary"]
    assert "**When:** Wed Nov 25, 10:00 AM - 11:00 AM PT" in summary_text
    assert "**Where:** Main Field" in summary_text
    assert "**With:** coach@example.org" in summary_text
    assert "**Why:** Owner requested scheduling follow-up." in summary_text
    assert (
        '**Approve Action:** Add "Team Sync" to your calendar '
        "(Wed Nov 25, 10:00 AM - 11:00 AM PT)"
    ) in summary_text
    assert "prop-atomic-1" not in summary_text
    assert "calendar.create" not in summary_text
    assert "primary" not in summary_text
    assert "2026-11-25T" not in summary_text
    assert "**Title:**" not in summary_text
    assert "**Proposal:**" not in summary_text
    assert "Expires" not in summary_text

    # Re-scanning while file remains in pending/ MUST NOT stage duplicate Decision Cards
    res2 = gate.scan_pending_proposals(now=now_ts + 15)
    assert res2["staged"] == 0
    assert res2["skipped_existing"] == 1
    assert len(staged_calls) == 1


def test_invalid_action_and_malformed_json_rejected_and_archived(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, dry_run=True)
    now_ts = _ts_pt(2026, 11, 24, 11, 0)

    bad_gmail = {
        "id": "prop-gmail-send",
        "action": "gmail.send",
        "args": {"to": "coach@example.org", "subject": "Hi", "body": "Nope"},
        "reason": "Should be rejected by strict allowlist",
        "expires_at": "2026-11-24T18:00:00-08:00",
        "from_session": "intake/gmail/thread-99",
    }
    write_proposal_atomic(cfg.proposals_pending_dir, bad_gmail)
    (cfg.proposals_pending_dir / "corrupt.json").write_text("{not-json", encoding="utf-8")

    staged_calls: list[dict[str, Any]] = []
    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: staged_calls.append(kw) or {"decision_id": "dec-x"},
        now_fn=lambda: now_ts,
    )
    res = gate.scan_pending_proposals(now=now_ts)

    assert res["staged"] == 0
    assert res["rejected"] == 2
    assert len(staged_calls) == 0
    assert not (cfg.proposals_pending_dir / "prop-gmail-send.json").exists()
    assert not (cfg.proposals_pending_dir / "corrupt.json").exists()
    assert (cfg.proposals_archive_dir / "prop-gmail-send.rejected.json").is_file()
    assert (cfg.proposals_archive_dir / "corrupt.rejected.json").is_file()

    row = gate.gate_store.get_proposal("prop-gmail-send")
    assert row is not None
    assert row["status"] == "rejected"

    receipts = _fetch_internal_events(gate.queue)
    ext_ids = {ev["external_id"] for ev in receipts}
    assert "gate-receipt:prop-gmail-send:rejected" in ext_ids
    assert "gate-receipt:corrupt:rejected" in ext_ids


def test_quiet_hours_refuses_untrusted_urgent_while_daytime_budget_allows_urgent(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path, dry_run=True, max_proactive=1)
    quiet_ts = _ts_pt(2026, 11, 24, 23, 30)  # 23:30 PT (inside 22:00-07:00)
    day_ts = _ts_pt(2026, 11, 25, 9, 0)  # 09:00 PT (outside quiet hours)

    write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal(
            "prop-routine",
            expires_at="2026-11-26T18:00:00-08:00",
            urgent=False,
        ),
    )
    untrusted_urgent = _sample_create_proposal(
        "prop-urgent",
        expires_at="2026-11-26T18:00:00-08:00",
        urgent=True,
    )
    # Even if an LLM writes trusted=True in JSON, untrusted session must NOT bypass quiet hours.
    untrusted_urgent["trusted"] = True
    write_proposal_atomic(cfg.proposals_pending_dir, untrusted_urgent)

    staged_calls: list[dict[str, Any]] = []
    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: staged_calls.append(kw) or {"decision_id": f"dec-{len(staged_calls)}"},
        now_fn=lambda: quiet_ts,
    )

    quiet_res = gate.scan_pending_proposals(now=quiet_ts)
    assert quiet_res["staged"] == 0
    assert quiet_res["deferred_quiet_hours"] == 2
    assert (cfg.proposals_pending_dir / "prop-routine.json").is_file()
    assert (cfg.proposals_pending_dir / "prop-urgent.json").is_file()

    # Outside quiet hours, both routine (consumes max_proactive=1) and urgent stage cleanly.
    day_res = gate.scan_pending_proposals(now=day_ts)
    assert day_res["staged"] == 2
    assert sorted(day_res["staged_ids"]) == ["prop-routine", "prop-urgent"]

    # Another routine proposal on the same day must wait untouched due to daily proactive cap
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal(
            "prop-over-cap",
            expires_at="2026-11-26T18:00:00-08:00",
            urgent=False,
        ),
    )
    cap_res = gate.scan_pending_proposals(now=day_ts + 60)
    assert cap_res["staged"] == 0
    assert cap_res["deferred_budget"] == 1
    assert (cfg.proposals_pending_dir / "prop-over-cap.json").is_file()


def test_gate_quiet_hours_bypass_owner_session_vip_imminent_and_cap(tmp_path: Path) -> None:
    cfg = BriefConfig(
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        allowlisted_emails=["owner@example.com"],
        vip_senders=["vip@example.com"],
        max_overnight_bypasses=2,
        imminent_event_minutes=60,
        dry_run=True,
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        timezone="America/Los_Angeles",
    )
    quiet_ts = _ts_pt(2026, 11, 24, 23, 30)

    # 1) Owner Discord session (#brief) bypasses quiet hours and does NOT count against cap.
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal(
            "prop-owner-discord",
            expires_at="2026-11-26T18:00:00-08:00",
            from_session="intake/discord/brief",
            urgent=False,
        ),
    )

    # 2) VIP sender proposal (resolved via sender metadata) -> bypass 1 of 2.
    vip_prop = _sample_create_proposal(
        "prop-vip-1",
        expires_at="2026-11-26T18:00:00-08:00",
        from_session="intake/gmail/vip-thread",
        urgent=True,
    )
    vip_prop["sender"] = "VIP <vip@example.com>"
    write_proposal_atomic(cfg.proposals_pending_dir, vip_prop)

    # 3) Imminent calendar proposal -> bypass 2 of 2.
    cal_prop = _sample_create_proposal(
        "prop-cal-imminent",
        expires_at="2026-11-26T18:00:00-08:00",
        from_session="intake/calendar/ev-soon",
        urgent=True,
    )
    cal_prop["source"] = "calendar"
    cal_prop["starts_within_minutes"] = 25
    write_proposal_atomic(cfg.proposals_pending_dir, cal_prop)

    # 4) Third eligible overnight bypass exceeds max_overnight_bypasses=2 -> deferred.
    vip_prop_2 = _sample_create_proposal(
        "prop-vip-over-cap",
        expires_at="2026-11-26T18:00:00-08:00",
        from_session="intake/gmail/vip-thread-2",
        urgent=True,
    )
    vip_prop_2["sender"] = "vip@example.com"
    write_proposal_atomic(cfg.proposals_pending_dir, vip_prop_2)

    staged_calls: list[dict[str, Any]] = []
    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: staged_calls.append(kw) or {"decision_id": f"dec-{len(staged_calls)}"},
        now_fn=lambda: quiet_ts,
    )
    res = gate.scan_pending_proposals(now=quiet_ts)
    assert res["staged"] == 3
    assert set(res["staged_ids"]) == {
        "prop-owner-discord",
        "prop-vip-1",
        "prop-cal-imminent",
    }
    assert res["deferred_quiet_hours"] == 1
    assert (cfg.proposals_pending_dir / "prop-vip-over-cap.json").is_file()

    budget_json = json.loads(
        (cfg.state_dir / "delivery_budget.json").read_text(encoding="utf-8")
    )
    assert budget_json["days"]["2026-11-24"]["overnight_bypasses"] == 2
    assert budget_json["days"]["2026-11-24"]["owner_sent"] == 1


def test_tamper_detection_refuses_execution_quarantines_and_alerts(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, dry_run=False)
    now_ts = _ts_pt(2026, 11, 24, 12, 0)

    pending_file = write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal("prop-tamper-1"),
    )
    api = _FakeCalendarApi()
    alerts: list[str] = []

    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: {"decision_id": "dec-tamper"},
        api_client=api,
        now_fn=lambda: now_ts,
        alert_fn=lambda msg: alerts.append(msg) or True,
    )
    scan_res = gate.scan_pending_proposals(now=now_ts)
    assert scan_res["staged"] == 1

    # Tamper with the pending JSON file on disk before Approve!
    tampered_data = json.loads(pending_file.read_text(encoding="utf-8"))
    tampered_data["args"]["summary"] = "Malicious Modified Summary"
    pending_file.write_text(json.dumps(tampered_data), encoding="utf-8")

    approve_res = gate.approve_proposal("prop-tamper-1", now=now_ts + 30)
    assert approve_res["ok"] is False
    assert approve_res["status"] == "tampered"
    assert len(api.created) == 0
    assert len(alerts) == 1
    assert "Tamper detected" in alerts[0]
    assert not pending_file.exists()
    assert (cfg.proposals_archive_dir / "prop-tamper-1.tampered.json").is_file()

    row = gate.gate_store.get_proposal("prop-tamper-1")
    assert row is not None
    assert row["status"] == "tampered"

    receipts = _fetch_internal_events(gate.queue)
    assert any(ev["external_id"] == "gate-receipt:prop-tamper-1:tampered" for ev in receipts)


def test_single_execution_replay_lock_and_exact_snapshot_execution(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, dry_run=False)
    now_ts = _ts_pt(2026, 11, 24, 13, 0)

    write_proposal_atomic(cfg.proposals_pending_dir, _sample_create_proposal("prop-exec-1"))
    api = _FakeCalendarApi()
    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: {"decision_id": "dec-exec-1"},
        api_client=api,
        now_fn=lambda: now_ts,
    )
    gate.scan_pending_proposals(now=now_ts)

    first = gate.approve_proposal("prop-exec-1", now=now_ts + 10)
    assert first["ok"] is True
    assert first["status"] == "executed"
    assert first["receipt"]["event_id"] == "evt-created-100"
    assert len(api.created) == 1
    assert not (cfg.proposals_pending_dir / "prop-exec-1.json").exists()
    assert (cfg.proposals_archive_dir / "prop-exec-1.executed.json").is_file()

    # Second approval (replay) must be deterministically rejected without calling API
    second = gate.approve_proposal("prop-exec-1", now=now_ts + 20)
    assert second["ok"] is False
    assert second["status"] == "executed"
    assert len(api.created) == 1

    # Also verify pre-claimed 'executing' state prevents concurrent double execution
    with gate.gate_store._tx() as conn:
        conn.execute("UPDATE gate_proposals SET status = 'executing' WHERE id = 'prop-exec-1'")
    concurrent = gate.approve_proposal("prop-exec-1", now=now_ts + 25)
    assert concurrent["ok"] is False
    assert concurrent["status"] == "executing"
    assert len(api.created) == 1


def test_edited_approval_validation_and_forge_entrypoints(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, dry_run=False)
    now_ts = _ts_pt(2026, 11, 24, 14, 0)

    update_prop = {
        "id": "prop-upd-1",
        "action": "calendar.update",
        "args": {
            "event_id": "evt-999",
            "summary": "Initial Title",
            "start": "2026-11-25T15:00:00-08:00",
            "end": "2026-11-25T16:00:00-08:00",
        },
        "reason": "Reschedule request",
        "expires_at": "2026-11-25T18:00:00-08:00",
        "from_session": "task/20261124-schedule",
    }
    write_proposal_atomic(cfg.proposals_pending_dir, update_prop)

    captured_cards: list[dict[str, Any]] = []
    api = _FakeCalendarApi()
    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: captured_cards.append(kw) or {"decision_id": "dec-upd-1"},
        api_client=api,
        now_fn=lambda: now_ts,
    )
    gate.scan_pending_proposals(now=now_ts)
    assert len(captured_cards) == 1
    card_payload = captured_cards[0]["payload"]
    assert card_payload["args_sha256"] == canonical_proposal_hash(
        "calendar.update",
        validate_action_args("calendar.update", update_prop["args"], tz_name=cfg.timezone),
    )

    # Approve with edited payload via FORGE entrypoint (using full-payload override shape)
    edited_override = {
        "args": {
            "event_id": "evt-999",
            "summary": "Edited Title",
            "start": "2026-11-25T16:00:00-08:00",
            "end": "2026-11-25T17:00:00-08:00",
        }
    }
    res = handle_forge_decision_approve(
        card_payload,
        edited_payload=edited_override,
        api_client=api,
        now=now_ts + 60,
    )
    assert res["ok"] is True
    assert res["status"] == "edited_executed"
    assert len(api.updated) == 1
    assert api.updated[0][2]["summary"] == "Edited Title"
    assert (cfg.proposals_archive_dir / "prop-upd-1.edited_executed.json").is_file()

    # Invalid edited args on a staged proposal transition cleanly to failed (never stuck in executing)
    write_proposal_atomic(cfg.proposals_pending_dir, _sample_create_proposal("prop-bad-edit"))
    gate.scan_pending_proposals(now=now_ts + 70)
    bad_edit_res = gate.approve_proposal(
        "prop-bad-edit",
        edited_args={
            "summary": "Bad Times",
            "start": "2026-11-25T17:00:00-08:00",
            "end": "2026-11-25T16:00:00-08:00",  # end before start!
        },
        now=now_ts + 80,
    )
    assert bad_edit_res["ok"] is False
    assert bad_edit_res["status"] == "failed"
    row_bad = gate.gate_store.get_proposal("prop-bad-edit")
    assert row_bad is not None
    assert row_bad["status"] == "failed"
    assert row_bad["resolved_at"] is not None


def test_dismiss_and_expiry_archiving_and_status(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, dry_run=True)
    now_ts = _ts_pt(2026, 11, 24, 15, 0)

    write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal("prop-dismiss-1", expires_at="2026-11-26T18:00:00-08:00"),
    )
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal("prop-expire-1", expires_at="2026-11-24T15:30:00-08:00"),
    )

    captured_cards: dict[str, dict[str, Any]] = {}

    def _stage(**kw: Any) -> dict[str, Any]:
        pid = kw["payload"]["proposal_id"]
        captured_cards[pid] = kw["payload"]
        return {"decision_id": f"dec-{pid}"}

    gate = BriefApprovalGate(cfg, stage_card_fn=_stage, now_fn=lambda: now_ts)
    gate.scan_pending_proposals(now=now_ts)

    # Dismiss prop-dismiss-1 via FORGE dismiss entrypoint with owner preference note
    dismiss_res = handle_forge_decision_dismiss(
        captured_cards["prop-dismiss-1"],
        note="Prefer mornings after 10:00 PT.",
        now=now_ts + 120,
    )
    assert dismiss_res["ok"] is True
    assert dismiss_res["status"] == "dismissed"
    assert (cfg.proposals_archive_dir / "prop-dismiss-1.dismissed.json").is_file()

    # Expire prop-expire-1 once clock passes 15:30 PT
    expired_ids = gate.expire_stale_proposals(now=now_ts + 2400)
    assert expired_ids == ["prop-expire-1"]
    assert (cfg.proposals_archive_dir / "prop-expire-1.expired.json").is_file()

    receipts = _fetch_internal_events(gate.queue)
    dismiss_ev = next(ev for ev in receipts if ev["external_id"] == "gate-receipt:prop-dismiss-1:dismissed")
    assert "Prefer mornings after 10:00 PT." in dismiss_ev["body"]
    assert dismiss_ev["trusted"] == 1
    assert dismiss_ev["target_tag"] == "intake/gmail/thread-42"
    assert any(ev["external_id"] == "gate-receipt:prop-expire-1:expired" for ev in receipts)

    status = gate.get_gate_status(now=now_ts + 2400)
    assert status["dismissed"] == 1
    assert status["expired"] == 1
    assert status["staged"] == 0
    assert status["pending_files"] == 0
    assert status["total"] == 2

    # Confirm SQLite WAL mode is active on gate.db
    with sqlite3.connect(cfg.gate_db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(mode).lower() == "wal"


def test_ttl_bounds_scan_expiry_and_direct_edited_args_entrypoint(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, dry_run=False, max_ttl=3600)
    now_ts = _ts_pt(2026, 11, 24, 16, 0)

    # Already expired at scan time -> .expired.json
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal("prop-already-exp", expires_at="2026-11-24T15:59:00-08:00"),
    )
    # Exceeds gate_max_proposal_ttl_seconds (3600s) -> .rejected.json
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal("prop-over-ttl", expires_at="2026-11-24T19:00:00-08:00"),
    )
    # Valid within TTL (expires in 1800s)
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        _sample_create_proposal("prop-valid-ttl", expires_at="2026-11-24T16:30:00-08:00"),
    )

    cards: dict[str, dict[str, Any]] = {}
    api = _FakeCalendarApi()
    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: cards.setdefault(kw["payload"]["proposal_id"], kw["payload"])
        and {"decision_id": f"dec-{kw['payload']['proposal_id']}"},
        api_client=api,
        now_fn=lambda: now_ts,
    )
    res = gate.scan_pending_proposals(now=now_ts)
    assert res["expired"] == 1
    assert res["rejected"] == 1
    assert res["staged"] == 1
    assert (cfg.proposals_archive_dir / "prop-already-exp.expired.json").is_file()
    assert (cfg.proposals_archive_dir / "prop-over-ttl.rejected.json").is_file()

    # Approve via direct edited-args dict (without outer {"args": ...} wrapper)
    approve_res = handle_forge_decision_approve(
        cards["prop-valid-ttl"],
        edited_payload={
            "summary": "Direct Edited Sync",
            "start": "2026-11-25T11:00:00-08:00",
            "end": "2026-11-25T12:00:00-08:00",
        },
        api_client=api,
        now=now_ts + 60,
    )
    assert approve_res["ok"] is True
    assert approve_res["status"] == "edited_executed"
    assert api.created[0][1]["summary"] == "Direct Edited Sync"


def test_readable_pt_range_and_slim_decision_card_variants() -> None:
    # Same-day timed range
    assert (
        _format_readable_pt_range(
            "2026-12-03T10:30:00-08:00",
            "2026-12-03T11:30:00-08:00",
            "America/Los_Angeles",
        )
        == "Thu Dec 3, 10:30 AM - 11:30 AM PT"
    )
    # Cross-day timed range
    assert (
        _format_readable_pt_range(
            "2026-12-03T10:30:00-08:00",
            "2026-12-04T09:00:00-08:00",
            "America/Los_Angeles",
        )
        == "Thu Dec 3, 10:30 AM - Fri Dec 4, 9:00 AM PT"
    )
    # Date-only all-day events
    assert (
        _format_readable_pt_range("2026-12-03", "2026-12-03", "America/Los_Angeles")
        == "Thu Dec 3 (All day)"
    )
    assert (
        _format_readable_pt_range("2026-12-03", None, "America/Los_Angeles")
        == "Thu Dec 3 (All day)"
    )
    assert (
        _format_readable_pt_range("2026-12-03", "2026-12-04", "America/Los_Angeles")
        == "Thu Dec 3 - Fri Dec 4 (All day)"
    )

    # Non-urgent create without location/attendees -> compact 4-line body
    title_create, body_create = _build_decision_card_copy(
        proposal_id="prop-raw-id-99",
        action="calendar.create",
        normalized_args={
            "calendar_id": "primary",
            "summary": "Dentist Appointment",
            "start": "2026-12-03T10:30:00-08:00",
            "end": "2026-12-03T11:30:00-08:00",
        },
        reason="Confirmed via office email.",
        expires_ts=_ts_pt(2026, 12, 5, 18, 0),
        urgent=False,
        tz_name="America/Los_Angeles",
    )
    assert title_create == "Add to Calendar: Dentist Appointment"
    assert body_create.splitlines() == [
        "**When:** Thu Dec 3, 10:30 AM - 11:30 AM PT",
        "**Why:** Confirmed via office email.",
        "",
        '**Approve Action:** Add "Dentist Appointment" to your calendar (Thu Dec 3, 10:30 AM - 11:30 AM PT)',
    ]

    # Urgent calendar.update without start/end omits parenthetical and includes **[URGENT]**
    title_update, body_update = _build_decision_card_copy(
        proposal_id="prop-upd-raw-1",
        action="calendar.update",
        normalized_args={
            "calendar_id": "primary",
            "event_id": "evt-secret-123",
            "summary": "Board Call",
            "location": "Zoom",
        },
        reason="Moved to virtual link.",
        expires_ts=_ts_pt(2026, 12, 5, 18, 0),
        urgent=True,
        tz_name="America/Los_Angeles",
    )
    assert title_update == "Update Calendar: Board Call"
    assert body_update.splitlines() == [
        "**[URGENT]**",
        "**Where:** Zoom",
        "**Why:** Moved to virtual link.",
        "",
        '**Approve Action:** Update "Board Call" on your calendar',
    ]
    assert "prop-upd-raw-1" not in body_update
    assert "evt-secret-123" not in body_update
    assert "calendar.update" not in body_update
    assert "primary" not in body_update


def test_base_prompt_includes_brevity_invariant() -> None:
    base_md = (REPO_ROOT / "prompts" / "brief" / "base.md").read_text(encoding="utf-8")
    assert "5. **Brevity & Plain English:**" in base_md
    assert "1–3 short plain-English sentences" in base_md
    assert "mobile-scannable" in base_md


def test_extract_dismissal_keys_and_three_strike_silent_dismissal_lifecycle(
    tmp_path: Path,
) -> None:
    cfg = _make_config(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    now_ts = _ts_pt(2026, 11, 24, 10, 0)

    # Verify _extract_dismissal_keys derivations
    assert _extract_dismissal_keys(
        {
            "action": "calendar.create",
            "args": {"attendees": ["Coach <Coach@School.Example.ORG>"]},
            "reason": "Practice sync",
            "from_session": "intake/gmail/thread-1",
        }
    ) == ("coach@school.example.org", "calendar.create")
    assert _extract_dismissal_keys(
        {
            "action": "calendar.update",
            "args_json": json.dumps({"summary": "Promo Webinar"}),
            "reason": "Invite from events.vendor.example.com",
            "from_session": "intake/gmail/thread-2",
        }
    ) == ("events.vendor.example.com", "calendar.update")
    assert _extract_dismissal_keys(
        {
            "action": "calendar.create",
            "args": {"summary": "Deep Work Block"},
            "reason": "Hold focus block",
            "from_session": "intake/gmail/thread-3",
        }
    ) == ("", "calendar.create")

    cards: dict[str, dict[str, Any]] = {}
    gate = BriefApprovalGate(
        cfg,
        queue=queue,
        stage_card_fn=lambda **kw: cards.setdefault(
            kw["payload"]["proposal_id"],
            {"decision_id": f"dec-{kw['payload']['proposal_id']}", **kw},
        ),
        api_client=_FakeCalendarApi(),
        now_fn=lambda: now_ts,
    )

    for idx in (1, 2, 3):
        pid = f"prop-strike-{idx}"
        prop = _sample_create_proposal(
            pid,
            summary=f"Practice {idx}",
            from_session=f"intake/gmail/thread-strike-{idx}",
        )
        write_proposal_atomic(cfg.proposals_pending_dir, prop)

    scan_res = gate.scan_pending_proposals(now=now_ts)
    assert scan_res["staged"] == 3

    # 1st silent dismissal -> 0 mute candidates
    d1 = gate.dismiss_proposal("prop-strike-1", note="", now=now_ts + 10)
    assert d1["ok"] is True
    assert gate.gate_store.list_three_strike_candidates(now=now_ts + 10) == []

    # 2nd silent dismissal -> 0 mute candidates
    d2 = gate.dismiss_proposal("prop-strike-2", note="", now=now_ts + 20)
    assert d2["ok"] is True
    assert gate.gate_store.list_three_strike_candidates(now=now_ts + 20) == []

    # 3rd silent dismissal -> returns the candidate
    d3 = gate.dismiss_proposal("prop-strike-3", note="", now=now_ts + 30)
    assert d3["ok"] is True
    candidates = gate.gate_store.list_three_strike_candidates(now=now_ts + 30)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["sender_key"] == "coach@example.org"
    assert cand["domain"] == "example.org"
    assert cand["category_key"] == "calendar.create"
    assert cand["strike_count"] == 3
    assert cand["proposal_ids"] == ["prop-strike-1", "prop-strike-2", "prop-strike-3"]

    # Already-muted domains (exact domain or parent domain) are excluded
    assert (
        gate.gate_store.list_three_strike_candidates(
            now=now_ts + 30,
            muted_domains=("example.org",),
        )
        == []
    )
    assert (
        gate.gate_store.list_three_strike_candidates(
            now=now_ts + 30,
            muted_domains=("coach@example.org",),
        )
        == []
    )

    # mark_strikes_surfaced prevents re-asking on subsequent daily reviews
    marked = gate.gate_store.mark_strikes_surfaced(candidates, now=now_ts + 60)
    assert marked == 3
    assert gate.gate_store.list_three_strike_candidates(now=now_ts + 65) == []

    # One more dismissal after surfacing does not re-trigger (only 1 unsurfaced strike < 3)
    gate.gate_store.record_dismissal_strike(
        proposal_id="prop-strike-4",
        action="calendar.create",
        sender_key="coach@example.org",
        category_key="calendar.create",
        dismissed_at=now_ts + 120,
        note="",
    )
    assert gate.gate_store.list_three_strike_candidates(now=now_ts + 125) == []

    # Direct BriefGateStore category-only 3-strike tracking when sender_key is empty
    direct_store = BriefGateStore(cfg.gate_db_path)
    direct_store.init_db()
    for idx in (1, 2, 3):
        direct_store.record_dismissal_strike(
            f"prop-cat-{idx}",
            "calendar.update",
            "",
            "calendar.update",
            now_ts + 200 + idx,
        )
    cat_cands = direct_store.list_three_strike_candidates(now=now_ts + 210)
    assert len(cat_cands) == 1
    assert cat_cands[0]["kind"] == "category"
    assert cat_cands[0]["category_key"] == "calendar.update"
    assert direct_store.mark_strikes_surfaced(cat_cands, now=now_ts + 220) == 3
    assert direct_store.list_three_strike_candidates(now=now_ts + 225) == []


def test_gate_default_and_non_positive_proactive_limit_unlimited_while_positive_caps(
    tmp_path: Path,
) -> None:
    day_ts = _ts_pt(2026, 11, 24, 10, 0)

    # Default / 0 and negative limits allow unlimited (>12) proactive proposal staging.
    for label, limit_val in (("zero", 0), ("negative", -1)):
        cfg = _make_config(tmp_path / label, dry_run=True, max_proactive=limit_val)
        staged_calls: list[dict[str, Any]] = []
        gate = BriefApprovalGate(
            cfg,
            stage_card_fn=lambda **kw: staged_calls.append(kw)
            or {"decision_id": f"dec-{len(staged_calls)}"},
            now_fn=lambda: day_ts,
        )
        assert gate._can_send_proactive(day_ts) is True

        for idx in range(15):
            write_proposal_atomic(
                cfg.proposals_pending_dir,
                _sample_create_proposal(
                    f"prop-{label}-{idx:02d}",
                    expires_at="2026-11-26T18:00:00-08:00",
                    urgent=False,
                    summary=f"Sync {idx}",
                ),
            )

        res = gate.scan_pending_proposals(now=day_ts)
        assert res["staged"] == 15
        assert res["deferred_budget"] == 0
        assert len(staged_calls) == 15
        assert gate._can_send_proactive(day_ts + 60) is True

    # Explicit positive limit still enforces daily cap for routine proactive proposals.
    cfg_pos = _make_config(tmp_path / "positive", dry_run=True, max_proactive=2)
    staged_pos: list[dict[str, Any]] = []
    gate_pos = BriefApprovalGate(
        cfg_pos,
        stage_card_fn=lambda **kw: staged_pos.append(kw)
        or {"decision_id": f"dec-{len(staged_pos)}"},
        now_fn=lambda: day_ts,
    )
    for idx in range(3):
        write_proposal_atomic(
            cfg_pos.proposals_pending_dir,
            _sample_create_proposal(
                f"prop-pos-{idx:02d}",
                expires_at="2026-11-26T18:00:00-08:00",
                urgent=False,
                summary=f"Positive Cap {idx}",
            ),
        )

    res_pos = gate_pos.scan_pending_proposals(now=day_ts)
    assert res_pos["staged"] == 2
    assert res_pos["deferred_budget"] == 1
    assert gate_pos._can_send_proactive(day_ts + 60) is False

