"""End-to-end integration tests for BRIEF Approval Gate, FORGE Decision Cards, Daemon, and CLI."""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import forge
from tools.brief import cli
from tools.brief.actions import (
    READONLY_OAUTH_SCOPES,
    WRITE_OAUTH_SCOPES,
    canonical_proposal_hash,
)
from tools.brief.config import BriefConfig, set_kill_switch
from tools.brief.daemon import BriefDaemonRunner, BriefDeliveryGate
from tools.brief.gate import BriefApprovalGate, write_proposal_atomic
from tools.brief.harness import DryRunHarness
from tools.brief.queue import BriefQueue
from tools.brief.router import BriefRouter
from tools.brief.watchdog import check_brief_health

PT_TZ = ZoneInfo("America/Los_Angeles")


def _ts_pt(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=PT_TZ).timestamp()


def _write_gate_config(
    tmp_path: Path,
    *,
    dry_run: bool = False,
    write_token_path: Path | None = None,
    allow_shared_fallback: bool = False,
    quiet_start: str = "22:00",
    quiet_end: str = "07:00",
) -> BriefConfig:
    state_dir = tmp_path / "state"
    memory_dir = tmp_path / "memory"
    state_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    token_path = write_token_path or (tmp_path / "secrets" / "brief_write_token.json")
    cfg_path = memory_dir / "config.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "[channel]",
                "channel_id = 1574655816236269569",
                "",
                "[owner]",
                'allowlisted_emails = ["owner@example.com"]',
                "",
                "[limits]",
                f'quiet_hours_start = "{quiet_start}"',
                f'quiet_hours_end = "{quiet_end}"',
                'timezone = "America/Los_Angeles"',
                "",
                "[safety]",
                f"dry_run = {'true' if dry_run else 'false'}",
                "",
                "[gate]",
                f'write_token_path = "{token_path}"',
                f"allow_shared_oauth_fallback = {'true' if allow_shared_fallback else 'false'}",
                "max_proposal_ttl_seconds = 604800",
                'default_calendar_id = "primary"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return BriefConfig.load(config_path=cfg_path, state_dir=state_dir, memory_dir=memory_dir)


def _fetch_internal_receipts(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM events WHERE source = 'internal' ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def test_end_to_end_forge_decision_approve_edited_approve_and_dismiss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    write_token_path = secrets_dir / "brief_write_token.json"
    write_token_path.write_text(
        json.dumps(
            {
                "client_id": "test-client-id.apps.googleusercontent.com",
                "client_secret": "test-write-secret-value",
                "refresh_token": "write-only-refresh-token",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scope": WRITE_OAUTH_SCOPES,
            }
        ),
        encoding="utf-8",
    )
    write_token_path.chmod(0o600)

    cfg = _write_gate_config(tmp_path, dry_run=False, write_token_path=write_token_path)
    forge_db = tmp_path / "forge.db"
    forge.init_forge_db(forge_db)
    now_ts = _ts_pt(2026, 11, 24, 10, 0)

    http_calls: list[dict[str, Any]] = []

    class _FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        raw_body = req.data.decode("utf-8") if req.data else ""
        http_calls.append(
            {
                "method": req.get_method(),
                "url": req.full_url,
                "headers": {k.lower(): v for k, v in req.header_items()},
                "body": raw_body,
                "timeout": timeout,
            }
        )
        if req.full_url == "https://oauth2.googleapis.com/token":
            assert "write-only-refresh-token" in raw_body
            return _FakeResp(
                {
                    "access_token": "ya29.write-only-access",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": WRITE_OAUTH_SCOPES,
                }
            )
        parsed = json.loads(raw_body)
        if req.get_method() == "POST":
            return _FakeResp(
                {
                    "id": "cal-evt-create-1",
                    "htmlLink": "https://calendar.google.com/event?eid=cal-evt-create-1",
                    "summary": parsed["summary"],
                    "start": parsed["start"],
                    "end": parsed["end"],
                }
            )
        if req.get_method() == "PATCH":
            return _FakeResp(
                {
                    "id": "cal-evt-existing-2",
                    "htmlLink": "https://calendar.google.com/event?eid=cal-evt-existing-2",
                    "summary": parsed.get("summary", "Updated"),
                    "start": parsed.get("start"),
                    "end": parsed.get("end"),
                }
            )
        raise AssertionError(f"Unexpected URL: {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    # Verify _ensure_decision_action_clarity derives Approve Action for bare brief_proposal summaries
    clarity_text = forge._ensure_decision_action_clarity(
        "Proposal to schedule follow-up",
        "brief_proposal",
        {
            "proposal_id": "prop-clarity",
            "action": "calendar.create",
            "args": {"summary": "Design Review"},
        },
    )
    assert "**Approve Action:**" in clarity_text
    assert "Design Review" in clarity_text

    # Write 3 proposals: create, update (for edited approve), and create (for dismiss)
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-int-create",
            "action": "calendar.create",
            "args": {
                "summary": "Parent Teacher Sync",
                "start": "2026-11-26T10:00:00-08:00",
                "end": "2026-11-26T10:30:00-08:00",
                "attendees": ["coach@example.org"],
                "location": "Room 4",
            },
            "reason": "Owner asked to confirm meeting.",
            "expires_at": "2026-11-28T18:00:00-08:00",
            "from_session": "intake/gmail/thread-101",
            "urgent": False,
        },
    )
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-int-update",
            "action": "calendar.update",
            "args": {
                "event_id": "cal-evt-existing-2",
                "summary": "Initial Title",
                "start": "2026-11-27T14:00:00-08:00",
                "end": "2026-11-27T15:00:00-08:00",
            },
            "reason": "Shift afternoon practice.",
            "expires_at": "2026-11-28T18:00:00-08:00",
            "from_session": "intake/calendar/evt-2",
            "urgent": False,
        },
    )
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-int-dismiss",
            "action": "calendar.create",
            "args": {
                "summary": "Optional Coffee",
                "start": "2026-11-29T09:00:00-08:00",
                "end": "2026-11-29T09:30:00-08:00",
            },
            "reason": "Vendor suggested intro call.",
            "expires_at": "2026-11-29T18:00:00-08:00",
            "from_session": "intake/gmail/thread-202",
            "urgent": False,
        },
    )

    stage_results: list[dict[str, Any]] = []
    stage_kwargs: list[dict[str, Any]] = []

    def _stage_in_temp_forge(**kw: Any) -> dict[str, Any]:
        # Force post_to_discord=False during tests while exercising real forge.stage_decision
        stage_kwargs.append(dict(kw))
        res = forge.stage_decision(
            category=kw["category"],
            title=kw["title"],
            summary=kw["summary"],
            handler_type=kw["handler_type"],
            payload=kw["payload"],
            channel_id=kw["channel_id"],
            ttl_seconds=kw["ttl_seconds"],
            is_private=kw["is_private"],
            db_path=forge_db,
            post_to_discord=False,
        )
        stage_results.append(res)
        return res

    gate = BriefApprovalGate(cfg, stage_card_fn=_stage_in_temp_forge, now_fn=lambda: now_ts)
    scan_res = gate.scan_pending_proposals(now=now_ts)
    assert scan_res["staged"] == 3
    assert len(stage_results) == 3

    # Verify cards stage directly in #brief without redirecting to #zero-chat and use slim copy
    for sr, kw in zip(stage_results, stage_kwargs, strict=True):
        assert sr["channel_id"] == 1574655816236269569
        assert sr["redirected_to_zero_chat"] is False
        assert "**Approve Action:**" in kw["summary"]
        assert "calendar.create" not in kw["summary"]
        assert "calendar.update" not in kw["summary"]
        assert "**Proposal:**" not in kw["summary"]
    by_id = {kw["payload"]["proposal_id"]: kw for kw in stage_kwargs}
    assert by_id["prop-int-create"]["title"] == "Add to Calendar: Parent Teacher Sync"
    assert by_id["prop-int-update"]["title"] == "Update Calendar: Initial Title"
    assert "prop-int-create" not in by_id["prop-int-create"]["summary"]
    assert "cal-evt-existing-2" not in by_id["prop-int-update"]["summary"]

    dec_create = gate.gate_store.get_proposal("prop-int-create")["decision_id"]
    dec_update = gate.gate_store.get_proposal("prop-int-update")["decision_id"]
    dec_dismiss = gate.gate_store.get_proposal("prop-int-dismiss")["decision_id"]
    assert dec_create and dec_update and dec_dismiss

    # 1. Approve create proposal through forge.execute_decision
    exec_create = forge.execute_decision(dec_create, db_path=forge_db)
    assert exec_create["ok"] is True
    assert exec_create["status"] == "approved"
    assert "calendar.create" in exec_create["receipt"]
    assert (cfg.proposals_archive_dir / "prop-int-create.executed.json").is_file()
    assert not (cfg.proposals_pending_dir / "prop-int-create.json").exists()

    # Single-execution protection via FORGE
    re_exec = forge.execute_decision(dec_create, db_path=forge_db)
    assert re_exec["ok"] is False

    # 2. Edited Approve on update proposal (override_payload lacks proposal_id/state_dir;
    #    execute_approval_card must preserve original_payload metadata)
    exec_edit = forge.execute_decision(
        dec_update,
        override_payload={
            "event_id": "cal-evt-existing-2",
            "summary": "Rescheduled Practice",
            "start": "2026-11-27T15:30:00-08:00",
            "end": "2026-11-27T16:30:00-08:00",
        },
        db_path=forge_db,
    )
    assert exec_edit["ok"] is True
    assert exec_edit["status"] == "edited_approved"
    assert (cfg.proposals_archive_dir / "prop-int-update.edited_executed.json").is_file()

    # 3. Dismiss third proposal through forge.dismiss_decision
    dismiss_res = forge.dismiss_decision(dec_dismiss, db_path=forge_db)
    assert dismiss_res["ok"] is True
    assert dismiss_res["status"] == "dismissed"
    assert (cfg.proposals_archive_dir / "prop-int-dismiss.dismissed.json").is_file()
    assert not (cfg.proposals_pending_dir / "prop-int-dismiss.json").exists()

    # Verify Calendar API requests hit the expected endpoints with edited/approved bodies
    cal_posts = [c for c in http_calls if c["method"] == "POST" and "googleapis.com/calendar" in c["url"]]
    cal_patches = [c for c in http_calls if c["method"] == "PATCH" and "googleapis.com/calendar" in c["url"]]
    assert len(cal_posts) == 1
    assert json.loads(cal_posts[0]["body"])["summary"] == "Parent Teacher Sync"
    assert json.loads(cal_posts[0]["body"])["attendees"] == [{"email": "coach@example.org"}]
    assert len(cal_patches) == 1
    assert "cal-evt-existing-2" in cal_patches[0]["url"]
    assert json.loads(cal_patches[0]["body"])["summary"] == "Rescheduled Practice"

    # Verify internal receipts in inbox.db
    receipts = _fetch_internal_receipts(cfg.db_path)
    ext_ids = {r["external_id"]: r for r in receipts}
    assert "gate-receipt:prop-int-create:executed" in ext_ids
    assert ext_ids["gate-receipt:prop-int-create:executed"]["target_tag"] == "intake/gmail/thread-101"
    assert "gate-receipt:prop-int-update:edited_executed" in ext_ids
    assert ext_ids["gate-receipt:prop-int-update:edited_executed"]["target_tag"] == "intake/calendar/evt-2"
    assert "gate-receipt:prop-int-dismiss:dismissed" in ext_ids
    assert ext_ids["gate-receipt:prop-int-dismiss:dismissed"]["target_tag"] == "intake/gmail/thread-202"


def test_forge_execute_refuses_tampered_proposal_on_disk(tmp_path: Path) -> None:
    cfg = _write_gate_config(tmp_path, dry_run=True)
    forge_db = tmp_path / "forge.db"
    forge.init_forge_db(forge_db)
    now_ts = _ts_pt(2026, 11, 24, 11, 0)

    pending_file = write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-tamper-int",
            "action": "calendar.create",
            "args": {
                "summary": "Original Title",
                "start": "2026-11-26T10:00:00-08:00",
                "end": "2026-11-26T11:00:00-08:00",
            },
            "reason": "Schedule review.",
            "expires_at": "2026-11-28T18:00:00-08:00",
            "from_session": "intake/gmail/thread-999",
            "urgent": False,
        },
    )

    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: forge.stage_decision(
            category=kw["category"],
            title=kw["title"],
            summary=kw["summary"],
            handler_type=kw["handler_type"],
            payload=kw["payload"],
            channel_id=kw["channel_id"],
            ttl_seconds=kw["ttl_seconds"],
            is_private=kw["is_private"],
            db_path=forge_db,
            post_to_discord=False,
        ),
        now_fn=lambda: now_ts,
    )
    gate.scan_pending_proposals(now=now_ts)
    row = gate.gate_store.get_proposal("prop-tamper-int")
    assert row is not None
    expected_hash = canonical_proposal_hash(
        "calendar.create",
        {
            "calendar_id": "primary",
            "summary": "Original Title",
            "start": "2026-11-26T10:00:00-08:00",
            "end": "2026-11-26T11:00:00-08:00",
        },
    )
    assert row["args_sha256"] == expected_hash

    # Tamper with proposal file on disk before clicking Approve
    tampered_data = json.loads(pending_file.read_text(encoding="utf-8"))
    tampered_data["args"]["summary"] = "Malicious Modified Summary"
    pending_file.write_text(json.dumps(tampered_data), encoding="utf-8")

    exec_res = forge.execute_decision(row["decision_id"], db_path=forge_db)
    assert exec_res["ok"] is False
    assert exec_res["status"] == "failed"
    assert "mismatch" in exec_res["error"].lower()

    after = gate.gate_store.get_proposal("prop-tamper-int")
    assert after is not None
    assert after["status"] == "tampered"
    assert (cfg.proposals_archive_dir / "prop-tamper-int.tampered.json").is_file()
    receipts = _fetch_internal_receipts(cfg.db_path)
    assert any(r["external_id"] == "gate-receipt:prop-tamper-int:tampered" for r in receipts)


def test_daemon_tick_runs_gate_scan_expiry_and_heartbeat(tmp_path: Path) -> None:
    cfg = _write_gate_config(tmp_path, dry_run=True)
    forge_db = tmp_path / "forge.db"
    forge.init_forge_db(forge_db)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()

    t0 = _ts_pt(2026, 11, 24, 14, 0)
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-daemon-exp",
            "action": "calendar.create",
            "args": {
                "summary": "Soon Expiring",
                "start": "2026-11-25T09:00:00-08:00",
                "end": "2026-11-25T10:00:00-08:00",
            },
            "reason": "Will expire on second tick.",
            "expires_at": "2026-11-24T14:01:00-08:00",
            "from_session": "intake/gmail/thread-d1",
            "urgent": False,
        },
    )

    class _NoopWatcher:
        def poll_once(self) -> dict[str, Any]:
            return {"status": "ok", "enqueued": 0}

    gate = BriefApprovalGate(
        cfg,
        queue=queue,
        stage_card_fn=lambda **kw: forge.stage_decision(
            category=kw["category"],
            title=kw["title"],
            summary=kw["summary"],
            handler_type=kw["handler_type"],
            payload=kw["payload"],
            channel_id=kw["channel_id"],
            ttl_seconds=kw["ttl_seconds"],
            is_private=kw["is_private"],
            db_path=forge_db,
            post_to_discord=False,
        ),
        now_fn=lambda: t0,
    )

    runner = BriefDaemonRunner(
        cfg,
        queue=queue,
        gmail_watcher=_NoopWatcher(),  # type: ignore[arg-type]
        calendar_watcher=_NoopWatcher(),  # type: ignore[arg-type]
        router=BriefRouter(cfg, queue=queue, harness=DryRunHarness(), now_fn=lambda: t0),
        delivery_gate=BriefDeliveryGate(cfg, post_fn=lambda _ch, _msg: True),
        approval_gate=gate,
        watchdog_fn=lambda _c, _q, **_kw: {"healthy": True},
        now_fn=lambda: t0,
    )

    tick1 = runner.run_tick(now=t0)
    assert tick1["errors"] == {}
    assert tick1["gate"] is not None
    assert tick1["gate"]["staged"] == 1
    assert tick1["gate"]["staged_ids"] == ["prop-daemon-exp"]

    gate_hb = cfg.heartbeats_dir / "gate.hb"
    assert gate_hb.is_file()
    assert float(gate_hb.read_text(encoding="utf-8").strip()) == pytest.approx(t0)

    health = check_brief_health(cfg, queue, now=t0 + 10.0)
    assert health["heartbeats"]["gate"]["status"] == "ok"

    # Tick 2 after expiry (14:02 PT) expires staged proposal via daemon gate loop
    tick2 = runner.run_tick(now=t0 + 120.0)
    assert tick2["errors"] == {}
    assert tick2["gate"]["expired_ids"] == ["prop-daemon-exp"]
    assert (cfg.proposals_archive_dir / "prop-daemon-exp.expired.json").is_file()

    # Kill switch prevents daemon gate tick from running
    set_kill_switch(cfg, True, reason="integration-test")
    paused_tick = runner.run_tick(now=t0 + 240.0)
    assert paused_tick["paused"] is True


def test_cli_gate_commands_status_and_mint_oauth_urls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _write_gate_config(
        tmp_path,
        dry_run=True,
        quiet_start="00:00",
        quiet_end="00:00",
    )
    forge_db = tmp_path / "forge.db"
    forge.init_forge_db(forge_db)

    # Read-only `status` & `gate-status` must NOT create gate.db before gate is initialized
    assert not cfg.gate_db_path.exists()
    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "status",
                "--json",
            ]
        )
        == 0
    )
    pre_status = json.loads(capsys.readouterr().out)
    assert not cfg.gate_db_path.exists()
    assert pre_status["gate"]["db_exists"] is False
    assert pre_status["gate"]["staged"] == 0
    assert pre_status["gate"]["tampered"] == 0

    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "gate-status",
            ]
        )
        == 0
    )
    assert "BRIEF Approval Gate Status" in capsys.readouterr().out
    assert not cfg.gate_db_path.exists()

    # Write 2 proposals for CLI scan, approve, and dismiss (with expires_at inside 7-day TTL)
    now_dt = datetime.now(PT_TZ)
    exp_iso = (now_dt + timedelta(days=2)).isoformat(timespec="seconds")
    ev1_start = (now_dt + timedelta(days=3)).isoformat(timespec="seconds")
    ev1_end = (now_dt + timedelta(days=3, hours=1)).isoformat(timespec="seconds")
    ev2_start = (now_dt + timedelta(days=4)).isoformat(timespec="seconds")
    ev2_end = (now_dt + timedelta(days=4, hours=1)).isoformat(timespec="seconds")

    write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-cli-1",
            "action": "calendar.create",
            "args": {
                "summary": "CLI Approved Event",
                "start": ev1_start,
                "end": ev1_end,
            },
            "reason": "CLI approve test.",
            "expires_at": exp_iso,
            "from_session": "intake/gmail/cli-1",
            "urgent": False,
        },
    )
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-cli-2",
            "action": "calendar.create",
            "args": {
                "summary": "CLI Dismissed Event",
                "start": ev2_start,
                "end": ev2_end,
            },
            "reason": "CLI dismiss test.",
            "expires_at": exp_iso,
            "from_session": "intake/gmail/cli-2",
            "urgent": False,
        },
    )

    monkeypatch.setattr(
        "tools.brief.gate._default_stage_card",
        lambda **kw: forge.stage_decision(**kw, db_path=forge_db),
    )

    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "gate-scan",
                "--dry-run",
            ]
        )
        == 0
    )
    scan_out = json.loads(capsys.readouterr().out)
    assert scan_out["staged"] == 2

    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "gate-status",
                "--json",
            ]
        )
        == 0
    )
    gs_out = json.loads(capsys.readouterr().out)
    assert gs_out["staged"] == 2
    assert len(gs_out["staged_proposals"]) == 2

    # Approve prop-cli-1 via CLI
    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "gate-approve",
                "prop-cli-1",
                "--dry-run",
            ]
        )
        == 0
    )
    app_out = json.loads(capsys.readouterr().out)
    assert app_out["ok"] is True
    assert app_out["status"] == "executed"

    # Missing proposal returns non-zero exit code
    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "gate-approve",
                "prop-does-not-exist",
                "--dry-run",
            ]
        )
        == 1
    )
    capsys.readouterr()

    # Dismiss prop-cli-2 via CLI with note; second dismiss fails with non-zero exit code
    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "gate-dismiss",
                "prop-cli-2",
                "--note",
                "Prefer afternoons",
            ]
        )
        == 0
    )
    dis_out = json.loads(capsys.readouterr().out)
    assert dis_out["ok"] is True
    assert dis_out["status"] == "dismissed"

    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "gate-dismiss",
                "prop-cli-2",
            ]
        )
        == 1
    )
    capsys.readouterr()

    # Top-level `status` shows updated gate counts in both JSON and text output
    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "status",
                "--json",
            ]
        )
        == 0
    )
    post_status = json.loads(capsys.readouterr().out)
    assert post_status["gate"]["executed"] == 1
    assert post_status["gate"]["dismissed"] == 1
    assert post_status["gate"]["staged"] == 0

    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "status",
            ]
        )
        == 0
    )
    text_status = capsys.readouterr().out
    assert "Gate         :" in text_status
    assert "executed=1" in text_status

    # mint-oauth-urls prints read-only vs write consent URLs without ever echoing client_secret
    oauth_client_path = tmp_path / "oauth_client.json"
    oauth_client_path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "brief-test-client.apps.googleusercontent.com",
                    "client_secret": "NEVER_ECHO_THIS_SECRET_VALUE",
                }
            }
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "mint-oauth-urls",
                "--oauth-client-path",
                str(oauth_client_path),
                "--json",
            ]
        )
        == 0
    )
    mint_json = json.loads(capsys.readouterr().out)
    assert mint_json["readonly_scopes"] == READONLY_OAUTH_SCOPES
    assert mint_json["write_scopes"] == WRITE_OAUTH_SCOPES
    assert "gmail.readonly" in mint_json["readonly_url"]
    assert "calendar.events" in mint_json["write_url"]
    assert "NEVER_ECHO_THIS_SECRET_VALUE" not in json.dumps(mint_json)

    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "mint-oauth-urls",
                "--oauth-client-path",
                str(oauth_client_path),
            ]
        )
        == 0
    )
    mint_text = capsys.readouterr().out
    assert "Read-Only URL" in mint_text
    assert "Write-Only URL" in mint_text
    assert "NEVER_ECHO_THIS_SECRET_VALUE" not in mint_text

    # Missing client file returns non-zero
    assert (
        cli.main(
            [
                "--config",
                str(cfg.config_path),
                "--state-dir",
                str(cfg.state_dir),
                "--memory-dir",
                str(cfg.memory_dir),
                "mint-oauth-urls",
                "--oauth-client-path",
                str(tmp_path / "missing_oauth.json"),
            ]
        )
        == 1
    )


def test_brief_edit_and_dismiss_modals_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Edit/Dismiss modal helpers, args staging, dismissal notes, and bridge modal routing."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import tools.bridge as bridge
    import tools.forge as live_forge

    cfg = _write_gate_config(tmp_path, dry_run=True)
    forge_db = tmp_path / "forge.db"
    live_forge.init_forge_db(forge_db)
    now_ts = _ts_pt(2026, 11, 24, 10, 0)

    # 1. _parse_brief_edit_modal_time handles ISO, YYYY-MM-DD HH:MM, YYYY-MM-DD HH:MM AM/PM, and date-only
    assert live_forge._parse_brief_edit_modal_time("2026-12-03T10:30:00-08:00") == "2026-12-03T10:30:00-08:00"
    assert live_forge._parse_brief_edit_modal_time("2026-12-03") == "2026-12-03"
    assert live_forge._parse_brief_edit_modal_time("2026-12-03 10:30") == "2026-12-03T10:30:00-08:00"
    assert live_forge._parse_brief_edit_modal_time("2026-12-03 10:30 AM") == "2026-12-03T10:30:00-08:00"
    assert live_forge._parse_brief_edit_modal_time("2026-12-03 2:15 PM") == "2026-12-03T14:15:00-08:00"
    with pytest.raises(ValueError):
        live_forge._parse_brief_edit_modal_time("not-a-timestamp")

    # 2. Stage two BRIEF proposals and verify scan_pending_proposals includes normalized `args`
    for pid, summary_title in (
        ("prop-modal-edit", "Dentist Visit"),
        ("prop-modal-dismiss", "Dental Cleaning Reminder"),
    ):
        write_proposal_atomic(
            cfg.proposals_pending_dir,
            {
                "id": pid,
                "action": "calendar.create",
                "args": {
                    "summary": summary_title,
                    "start": "2026-12-03T09:00:00-08:00",
                    "end": "2026-12-03T09:45:00-08:00",
                    "location": "Downtown Clinic",
                    "description": "Routine checkup",
                    "attendees": ["clinic@example.org"],
                },
                "reason": "Found reminder in email.",
                "expires_at": "2026-11-26T18:00:00-08:00",
                "from_session": f"intake/gmail/{pid}",
                "urgent": False,
            },
        )

    staged_kwargs: list[dict[str, Any]] = []
    gate = BriefApprovalGate(
        cfg,
        stage_card_fn=lambda **kw: staged_kwargs.append(dict(kw))
        or live_forge.stage_decision(
            category=kw["category"],
            title=kw["title"],
            summary=kw["summary"],
            handler_type=kw["handler_type"],
            payload=kw["payload"],
            channel_id=kw["channel_id"],
            ttl_seconds=kw["ttl_seconds"],
            is_private=kw["is_private"],
            db_path=forge_db,
            post_to_discord=False,
        ),
        now_fn=lambda: now_ts,
    )
    scan_res = gate.scan_pending_proposals(now=now_ts)
    assert scan_res["staged"] == 2
    by_pid = {kw["payload"]["proposal_id"]: kw for kw in staged_kwargs}
    assert by_pid["prop-modal-edit"]["payload"]["args"] == {
        "calendar_id": "primary",
        "summary": "Dentist Visit",
        "start": "2026-12-03T09:00:00-08:00",
        "end": "2026-12-03T09:45:00-08:00",
        "location": "Downtown Clinic",
        "description": "Routine checkup",
        "attendees": ["clinic@example.org"],
    }

    dec_edit = gate.gate_store.get_proposal("prop-modal-edit")["decision_id"]
    dec_dismiss = gate.gate_store.get_proposal("prop-modal-dismiss")["decision_id"]
    assert dec_edit and dec_dismiss

    # 3. _resolve_brief_modal_args returns payload['args'] directly and falls back to gate.db
    assert live_forge._resolve_brief_modal_args({"args": {"summary": "Direct"}}) == {"summary": "Direct"}
    fallback_args = live_forge._resolve_brief_modal_args(
        {"proposal_id": "prop-modal-edit", "state_dir": str(cfg.state_dir)}
    )
    assert fallback_args["summary"] == "Dentist Visit"
    assert fallback_args["attendees"] == ["clinic@example.org"]

    # 4. execute_decision with override_payload={'args': edited_args} preserves metadata and marks edited_executed
    exec_res = live_forge.execute_decision(
        dec_edit,
        override_payload={
            "args": {
                "calendar_id": "primary",
                "summary": "Dentist Visit (Rescheduled)",
                "start": "2026-12-03T10:30:00-08:00",
                "end": "2026-12-03T11:15:00-08:00",
                "location": "Midtown Clinic",
                "attendees": ["clinic@example.org"],
            }
        },
        db_path=forge_db,
    )
    assert exec_res["ok"] is True
    assert exec_res["status"] == "edited_approved"
    edit_row = gate.gate_store.get_proposal("prop-modal-edit")
    assert edit_row is not None
    assert edit_row["status"] == "edited_executed"
    assert (cfg.proposals_archive_dir / "prop-modal-edit.edited_executed.json").is_file()

    # 5. dismiss_decision(..., note=...) forwards note to gate receipt and updates forge_decisions.receipt_text
    dismiss_res = live_forge.dismiss_decision(
        dec_dismiss,
        note="Skip dental reminders",
        db_path=forge_db,
    )
    assert dismiss_res["ok"] is True
    assert dismiss_res["status"] == "dismissed"
    with live_forge.get_db_connection(forge_db) as conn:
        dec_row = conn.execute(
            "SELECT receipt_text FROM forge_decisions WHERE id = ?",
            (dec_dismiss,),
        ).fetchone()
    assert dec_row is not None
    assert dec_row["receipt_text"] == "Dismissed by user: Skip dental reminders"

    receipts = _fetch_internal_receipts(cfg.db_path)
    dismiss_receipts = [
        r for r in receipts if r["external_id"] == "gate-receipt:prop-modal-dismiss:dismissed"
    ]
    assert len(dismiss_receipts) == 1
    assert "Skip dental reminders" in dismiss_receipts[0]["body"]

    # 6. Bridge interaction routing opens BriefProposalEditModal / BriefProposalDismissModal for brief_proposal
    #    and preserves ForgeDecisionEditModal / instant dismiss for non-BRIEF handlers.
    dec_shell = live_forge.stage_decision(
        category="ops",
        title="Shell Test",
        summary="Run echo",
        handler_type="shell",
        payload={"command": "echo hi"},
        channel_id=live_forge.ZERO_CHAT_CHANNEL_ID,
        db_path=forge_db,
        post_to_discord=False,
    )["decision_id"]
    # Re-stage a pending brief_proposal card for modal open checks
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-modal-route",
            "action": "calendar.create",
            "args": {
                "summary": "School Pickup",
                "start": "2026-12-04T15:00:00-08:00",
                "end": "2026-12-04T15:30:00-08:00",
            },
            "reason": "Calendar sync.",
            "expires_at": "2026-11-27T18:00:00-08:00",
            "from_session": "intake/gmail/route-1",
            "urgent": False,
        },
    )
    write_proposal_atomic(
        cfg.proposals_pending_dir,
        {
            "id": "prop-modal-dismiss-submit",
            "action": "calendar.create",
            "args": {
                "summary": "Early Morning Sync",
                "start": "2026-12-04T07:30:00-08:00",
                "end": "2026-12-04T08:00:00-08:00",
            },
            "reason": "Proposed early check-in.",
            "expires_at": "2026-11-27T18:00:00-08:00",
            "from_session": "intake/gmail/route-2",
            "urgent": False,
        },
    )
    assert gate.scan_pending_proposals(now=now_ts)["staged"] == 2
    dec_brief = gate.gate_store.get_proposal("prop-modal-route")["decision_id"]
    dec_brief_dismiss = gate.gate_store.get_proposal("prop-modal-dismiss-submit")["decision_id"]
    assert dec_brief and dec_brief_dismiss

    monkeypatch.setattr(live_forge, "FORGE_DB_PATH", forge_db)

    def _make_interaction(custom_id: str) -> Any:
        inter = MagicMock()
        inter.type = bridge.discord.InteractionType.component
        inter.data = {"custom_id": custom_id}
        inter.user = SimpleNamespace(id=live_forge.OWNER_USER_ID)
        inter.response = MagicMock()
        inter.response.is_done = MagicMock(return_value=False)
        inter.response.send_modal = AsyncMock()
        inter.response.defer = AsyncMock()
        inter.response.send_message = AsyncMock()
        inter.followup = MagicMock()
        inter.followup.send = AsyncMock()
        inter.message = None
        return inter

    async def _exercise_bridge_and_modals() -> None:
        # brief_proposal edit opens BriefProposalEditModal
        inter_brief_edit = _make_interaction(f"forge_edit:{dec_brief}")
        await bridge.on_button_interaction(inter_brief_edit)
        inter_brief_edit.response.send_modal.assert_awaited_once()
        opened_edit_modal = inter_brief_edit.response.send_modal.call_args[0][0]
        assert isinstance(opened_edit_modal, live_forge.BriefProposalEditModal)
        assert opened_edit_modal.summary_input.default == "School Pickup"

        # non-BRIEF edit opens ForgeDecisionEditModal
        inter_shell_edit = _make_interaction(f"forge_edit:{dec_shell}")
        await bridge.on_button_interaction(inter_shell_edit)
        inter_shell_edit.response.send_modal.assert_awaited_once()
        assert isinstance(inter_shell_edit.response.send_modal.call_args[0][0], live_forge.ForgeDecisionEditModal)

        # brief_proposal dismiss opens BriefProposalDismissModal BEFORE defer()
        inter_brief_dismiss = _make_interaction(f"forge_dismiss:{dec_brief_dismiss}")
        await bridge.on_button_interaction(inter_brief_dismiss)
        inter_brief_dismiss.response.defer.assert_not_awaited()
        inter_brief_dismiss.response.send_modal.assert_awaited_once()
        opened_dismiss_modal = inter_brief_dismiss.response.send_modal.call_args[0][0]
        assert isinstance(opened_dismiss_modal, live_forge.BriefProposalDismissModal)

        # Submit BriefProposalDismissModal with optional note
        opened_dismiss_modal.db_path = forge_db
        opened_dismiss_modal.reason_input._value = "No meetings before 9 AM"
        submit_dismiss_inter = _make_interaction("modal_submit_dismiss")
        await opened_dismiss_modal.on_submit(submit_dismiss_inter)
        submit_dismiss_inter.response.defer.assert_awaited_once()
        dismissed_row = gate.gate_store.get_proposal("prop-modal-dismiss-submit")
        assert dismissed_row is not None
        assert dismissed_row["status"] == "dismissed"
        dismiss_modal_receipts = [
            r
            for r in _fetch_internal_receipts(cfg.db_path)
            if r["external_id"] == "gate-receipt:prop-modal-dismiss-submit:dismissed"
        ]
        assert len(dismiss_modal_receipts) == 1
        assert "No meetings before 9 AM" in dismiss_modal_receipts[0]["body"]

        # Submit BriefProposalEditModal with readable PT time
        opened_edit_modal.db_path = forge_db
        opened_edit_modal.summary_input._value = "School Pickup (Updated)"
        opened_edit_modal.start_input._value = "2026-12-04 3:30 PM"
        opened_edit_modal.end_input._value = "2026-12-04 16:00"
        opened_edit_modal.location_input._value = "North Gate"
        opened_edit_modal.description_input._value = "Early release"
        submit_edit_inter = _make_interaction("modal_submit")
        await opened_edit_modal.on_submit(submit_edit_inter)
        submit_edit_inter.response.defer.assert_awaited_once()
        route_row = gate.gate_store.get_proposal("prop-modal-route")
        assert route_row is not None
        assert route_row["status"] == "edited_executed"

        # non-BRIEF dismiss still performs instant 1-click dismiss without modal
        inter_shell_dismiss = _make_interaction(f"forge_dismiss:{dec_shell}")
        await bridge.on_button_interaction(inter_shell_dismiss)
        inter_shell_dismiss.response.send_modal.assert_not_awaited()
        inter_shell_dismiss.response.defer.assert_awaited_once()
        with live_forge.get_db_connection(forge_db) as conn:
            shell_row = conn.execute("SELECT status FROM forge_decisions WHERE id = ?", (dec_shell,)).fetchone()
        assert shell_row is not None
        assert shell_row["status"] == "dismissed"

    asyncio.run(_exercise_bridge_and_modals())
