"""Unit tests for BRIEF singleton daemon, quiet-hours delivery gate, and CLI wiring."""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.brief import cli, daemon
from tools.brief.config import DEFAULT_CHANNEL_ID, BriefConfig, set_kill_switch
from tools.brief.daemon import (
    BriefDaemonRunner,
    BriefDeliveryGate,
    deferred_deliveries_path,
    delivery_budget_path,
    get_daemon_pid,
    get_daemon_status,
    is_quiet_hours,
    lock_file_path,
    pid_file_path,
    run_daemon_loop,
    should_deliver_response,
)
from tools.brief.harness import DryRunHarness
from tools.brief.queue import BriefQueue
from tools.brief.router import BriefRouter
from tools.brief.watchers import CursorStore
from tools.daemon_singleton import acquire_singleton_lock, release_singleton_lock

_PT = ZoneInfo("America/Los_Angeles")


def _make_cfg(
    tmp_path: Path,
    *,
    dry_run: bool = True,
    max_proactive: int = 12,
    quiet_start: str = "22:00",
    quiet_end: str = "07:00",
) -> BriefConfig:
    return BriefConfig(
        allowlisted_emails=["owner@example.com"],
        gmail_interval_seconds=120,
        calendar_interval_seconds=300,
        router_interval_seconds=15,
        max_proactive_messages_per_day=max_proactive,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        dry_run=dry_run,
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        prompts_dir=REPO_ROOT / "prompts" / "brief",
    )


def _pt_ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=_PT).timestamp()


def test_is_quiet_hours_overnight_and_same_day(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, quiet_start="22:00", quiet_end="07:00")

    assert is_quiet_hours(cfg, _pt_ts(2026, 11, 23, 21, 59)) is False
    assert is_quiet_hours(cfg, _pt_ts(2026, 11, 23, 22, 0)) is True
    assert is_quiet_hours(cfg, _pt_ts(2026, 11, 23, 23, 30)) is True
    assert is_quiet_hours(cfg, _pt_ts(2026, 11, 24, 0, 15)) is True
    assert is_quiet_hours(cfg, _pt_ts(2026, 11, 24, 6, 59)) is True
    assert is_quiet_hours(cfg, _pt_ts(2026, 11, 24, 7, 0)) is False
    assert is_quiet_hours(cfg, _pt_ts(2026, 11, 24, 14, 0)) is False

    same_day = _make_cfg(tmp_path, quiet_start="13:00", quiet_end="15:00")
    assert is_quiet_hours(same_day, _pt_ts(2026, 11, 24, 12, 59)) is False
    assert is_quiet_hours(same_day, _pt_ts(2026, 11, 24, 13, 0)) is True
    assert is_quiet_hours(same_day, _pt_ts(2026, 11, 24, 14, 59)) is True
    assert is_quiet_hours(same_day, _pt_ts(2026, 11, 24, 15, 0)) is False


def test_should_deliver_response_classification() -> None:
    assert should_deliver_response("") == (False, "ignore")
    assert should_deliver_response("   \n ") == (False, "ignore")
    assert should_deliver_response("IGNORE") == (False, "ignore")
    assert should_deliver_response("ignore: promo email") == (False, "ignore")
    assert should_deliver_response("[NO_REPLY]") == (False, "ignore")
    assert should_deliver_response("[no_reply] nothing to surface") == (False, "ignore")
    assert should_deliver_response("Verdict: IGNORE") == (False, "ignore")
    assert should_deliver_response(json.dumps({"verdict": "IGNORE", "summary": "noise"})) == (
        False,
        "ignore",
    )

    assert should_deliver_response("INFORM: Flight delayed 30m") == (True, "inform")
    assert should_deliver_response("PROPOSE: Reply to vendor") == (True, "propose")
    assert should_deliver_response("TASK: Track permit approval") == (True, "task")
    assert should_deliver_response("Morning brief ready.") == (True, "summary")
    assert should_deliver_response(
        json.dumps({"verdict": "PROPOSE_CALENDAR", "summary": "Reschedule sync"})
    ) == (True, "propose")


def test_delivery_gate_quiet_hours_defer_bypass_and_flush(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, max_proactive=5)
    sent_messages: list[tuple[int, str]] = []
    gate = BriefDeliveryGate(
        cfg,
        post_fn=lambda ch, msg: sent_messages.append((ch, msg)) or True,
    )

    night_ts = _pt_ts(2026, 11, 23, 23, 15)
    # Normal proactive message is deferred during quiet hours.
    res_defer = gate.deliver(
        "INFORM: Package shipped https://example.com/track/123",
        tag="intake/gmail/pkg-1",
        event={"id": 10, "source": "gmail"},
        now=night_ts,
    )
    assert res_defer["delivered"] is False
    assert res_defer["status"] == "deferred_quiet_hours"
    assert len(sent_messages) == 0

    deferred_file = deferred_deliveries_path(cfg)
    assert deferred_file.is_file()
    deferred_data = json.loads(deferred_file.read_text(encoding="utf-8"))
    assert len(deferred_data["items"]) == 1
    assert "<https://example.com/track/123>" in deferred_data["items"][0]["content"]

    # Flushing during quiet hours leaves items untouched.
    early_flush = gate.flush_deferred(now=_pt_ts(2026, 11, 24, 6, 30))
    assert early_flush["flushed"] == 0
    assert early_flush["remaining"] == 1
    assert early_flush["skipped"] == "quiet_hours"

    # URGENT and owner-initiated messages bypass quiet hours immediately.
    res_urgent = gate.deliver(
        "URGENT: Early school dismissal at 1:30 PM PT",
        tag="intake/gmail/school-1",
        event={"id": 11, "source": "gmail"},
        now=night_ts + 60.0,
    )
    assert res_urgent["delivered"] is True

    res_owner = gate.deliver(
        "Here is your requested summary.",
        tag="intake/discord/brief",
        event={"id": 12, "source": "discord", "trusted": True},
        now=night_ts + 120.0,
    )
    assert res_owner["delivered"] is True
    assert len(sent_messages) == 2

    # Post-quiet-hours flush at 07:05 PT sends deferred item and clears queue.
    morning_ts = _pt_ts(2026, 11, 24, 7, 5)
    flushed = gate.flush_deferred(now=morning_ts)
    assert flushed["flushed"] == 1
    assert flushed["remaining"] == 0
    assert len(sent_messages) == 3
    assert "<https://example.com/track/123>" in sent_messages[2][1]

    after_data = json.loads(deferred_file.read_text(encoding="utf-8"))
    assert after_data["items"] == []


def test_delivery_gate_daily_proactive_cap_and_live_discord_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path, dry_run=False, max_proactive=2)
    captured: list[dict[str, Any]] = []

    class _FakeResp:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"id": "101"}'

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        captured.append(
            {
                "url": req.full_url,
                "headers": {k.lower(): v for k, v in req.header_items()},
                "body": json.loads(req.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _FakeResp()

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "brief-test-token")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    gate = BriefDeliveryGate(cfg)
    day1 = _pt_ts(2026, 11, 24, 10, 0)

    assert gate.deliver("INFORM: First https://example.com/1", now=day1)["delivered"] is True
    assert gate.deliver("INFORM: Second", now=day1 + 10.0)["delivered"] is True
    capped = gate.deliver("INFORM: Third should be capped", now=day1 + 20.0)
    assert capped["delivered"] is False
    assert capped["status"] == "suppressed_budget"
    assert capped["budget_used"] == 2
    assert capped["budget_limit"] == 2

    # Urgent and owner-initiated messages still deliver when budget is exhausted.
    assert gate.deliver("URGENT: Server down", now=day1 + 30.0)["delivered"] is True
    assert gate.deliver("Reply to owner", owner_initiated=True, now=day1 + 40.0)["delivered"] is True
    assert len(captured) == 4
    assert captured[0]["url"].endswith(f"/channels/{DEFAULT_CHANNEL_ID}/messages")
    assert captured[0]["headers"]["authorization"] == "Bot brief-test-token"
    assert "<https://example.com/1>" in captured[0]["body"]["content"]

    # Budget is tracked per PT day and resets next day.
    budget_json = json.loads(delivery_budget_path(cfg).read_text(encoding="utf-8"))
    assert budget_json["days"]["2026-11-24"]["proactive_sent"] == 2
    day2 = _pt_ts(2026, 11, 25, 9, 0)
    assert gate.deliver("INFORM: Next day works", now=day2)["delivered"] is True


def test_daemon_runner_kill_switch_pauses_cleanly(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    queue.enqueue_event(
        source="gmail",
        key="thread-ks-1",
        target_tag="intake/gmail/thread-ks-1",
        external_id="msg-ks-1",
        sender="owner@example.com",
        body="Keep me queued",
        received_at=_pt_ts(2026, 11, 24, 9, 0),
    )
    set_kill_switch(cfg, True, reason="pause-test")

    class _ForbidWatcher:
        def poll_once(self) -> dict[str, Any]:
            raise AssertionError("Watcher must not poll while kill switch is active")

    class _ForbidRouter:
        def process_one(self, *, now: float | None = None) -> dict[str, Any] | None:
            raise AssertionError("Router must not route while kill switch is active")

    def _forbid_watchdog(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Watchdog must not run while kill switch is active")

    runner = BriefDaemonRunner(
        cfg,
        queue=queue,
        gmail_watcher=_ForbidWatcher(),  # type: ignore[arg-type]
        calendar_watcher=_ForbidWatcher(),  # type: ignore[arg-type]
        router=_ForbidRouter(),  # type: ignore[arg-type]
        watchdog_fn=_forbid_watchdog,
    )
    tick = runner.run_tick(now=_pt_ts(2026, 11, 24, 9, 5))
    assert tick["paused"] is True
    assert tick["reason"] == "kill_switch_active"
    assert queue.get_queue_stats()["pending"] == 1


def test_daemon_runner_single_tick_cadence_and_error_isolation(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    cursors = CursorStore(cfg.cursors_path)

    # Enqueue one promo (IGNORE) and one urgent email (INFORM).
    t0 = _pt_ts(2026, 11, 24, 18, 5)
    queue.enqueue_event(
        source="gmail",
        key="thread-promo",
        target_tag="intake/gmail/thread-promo",
        external_id="msg-promo",
        sender="deals@example.com",
        body="Weekend Flash Sale 40% Off unsubscribe anytime",
        received_at=t0 - 10,
    )
    queue.enqueue_event(
        source="gmail",
        key="thread-school",
        target_tag="intake/gmail/thread-school",
        external_id="msg-school",
        sender="school@example.org",
        body="URGENT early dismissal at 1:30 PM PT",
        received_at=t0 - 5,
    )

    gmail_calls = 0
    cal_calls = 0
    wd_calls = 0

    class _FakeGmail:
        def poll_once(self) -> dict[str, Any]:
            nonlocal gmail_calls
            gmail_calls += 1
            if gmail_calls == 1:
                raise RuntimeError("transient gmail 503")
            return {"status": "ok", "enqueued": 0}

    class _FakeCalendar:
        def poll_once(self) -> dict[str, Any]:
            nonlocal cal_calls
            cal_calls += 1
            return {"status": "ok", "enqueued": 0}

    def _fake_watchdog(_cfg: BriefConfig, _q: BriefQueue, **_kw: Any) -> dict[str, Any]:
        nonlocal wd_calls
        wd_calls += 1
        return {"healthy": True, "alert_dispatched": False}

    sent: list[tuple[int, str]] = []
    gate = BriefDeliveryGate(cfg, post_fn=lambda ch, msg: sent.append((ch, msg)) or True)
    router = BriefRouter(cfg, queue=queue, harness=DryRunHarness(), now_fn=lambda: t0)

    runner = BriefDaemonRunner(
        cfg,
        queue=queue,
        cursors=cursors,
        gmail_watcher=_FakeGmail(),  # type: ignore[arg-type]
        calendar_watcher=_FakeCalendar(),  # type: ignore[arg-type]
        router=router,
        delivery_gate=gate,
        watchdog_fn=_fake_watchdog,
        now_fn=lambda: t0,
    )

    # Tick 1 at 18:05 PT: morning brief + daily review enqueued, gmail fails safely,
    # calendar + router + watchdog all still run.
    tick1 = runner.run_tick(now=t0)
    assert tick1["paused"] is False
    assert "gmail" in tick1["errors"]
    assert gmail_calls == 1
    assert cal_calls == 1
    assert wd_calls == 1
    assert tick1["timers"]["morning_brief"] is not None
    assert tick1["timers"]["daily_review"] is not None
    # 2 gmail + 2 timers routed in drain loop; promo IGNORE suppressed, 3 delivered.
    assert tick1["routed"] == 4
    assert tick1["delivered"] == 3
    assert len(sent) == 3

    # Tick 2 at +15s: only router runs (gmail=120s, calendar=300s, watchdog=900s).
    tick2 = runner.run_tick(now=t0 + 15.0)
    assert gmail_calls == 1
    assert cal_calls == 1
    assert wd_calls == 1
    assert tick2["timers"] == {}
    assert tick2["routed"] == 0

    # Tick 3 at +125s: gmail becomes due again and recovers without crashing.
    tick3 = runner.run_tick(now=t0 + 125.0)
    assert gmail_calls == 2
    assert cal_calls == 1
    assert tick3["errors"] == {}


def test_singleton_lock_and_cli_daemon_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_cfg(tmp_path)
    lock_path = lock_file_path(cfg)
    pid_path = pid_file_path(cfg)

    # Reconcile cleans stale PID file when no daemon process is running.
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999\n", encoding="utf-8")
    assert get_daemon_pid(cfg) is None
    assert not pid_path.exists()

    # CLI `daemon status --json`
    rc_status = cli.main(
        [
            "--state-dir",
            str(cfg.state_dir),
            "--memory-dir",
            str(cfg.memory_dir),
            "daemon",
            "status",
            "--json",
        ]
    )
    assert rc_status == 0
    daemon_st = json.loads(capsys.readouterr().out)
    assert daemon_st["running"] is False
    assert daemon_st["pid"] is None
    assert daemon_st["lock_held"] is False

    # CLI `status --json` includes daemon summary
    rc_top_status = cli.main(
        [
            "--state-dir",
            str(cfg.state_dir),
            "--memory-dir",
            str(cfg.memory_dir),
            "status",
            "--json",
        ]
    )
    assert rc_top_status == 0
    top_st = json.loads(capsys.readouterr().out)
    assert top_st["daemon"]["running"] is False
    assert top_st["daemon"]["pid"] is None

    # If lock is already held, run_daemon_loop refuses duplicate start.
    held = acquire_singleton_lock(lock_path)
    assert held is not None
    try:
        assert get_daemon_status(cfg)["lock_held"] is True
        assert run_daemon_loop(cfg, max_ticks=1) is False
    finally:
        release_singleton_lock(held)

    # CLI `daemon run --dry-run --max-ticks 1` executes one tick cleanly.
    ticks_executed = 0

    class _StubRunner:
        def __init__(self, _cfg: BriefConfig) -> None:
            pass

        def run_tick(self) -> dict[str, Any]:
            nonlocal ticks_executed
            ticks_executed += 1
            return {"paused": False}

    monkeypatch.setattr(daemon, "BriefDaemonRunner", _StubRunner)
    rc_run = cli.main(
        [
            "--state-dir",
            str(cfg.state_dir),
            "--memory-dir",
            str(cfg.memory_dir),
            "daemon",
            "run",
            "--dry-run",
            "--max-ticks",
            "1",
        ]
    )
    assert rc_run == 0
    assert ticks_executed == 1
    assert not pid_path.exists()


def test_daemon_runner_stages_proposals_immediately_after_each_routed_event(
    tmp_path: Path,
) -> None:
    from tools.brief.gate import BriefApprovalGate, write_proposal_atomic

    cfg = _make_cfg(tmp_path, dry_run=True, quiet_start="00:00", quiet_end="00:00")
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()
    t0 = _pt_ts(2026, 11, 24, 10, 0)

    ev1_id = queue.enqueue_event(
        source="gmail",
        key="thread-ev1",
        target_tag="intake/gmail/thread-ev1",
        external_id="msg-ev1",
        sender="owner@example.com",
        body="Please schedule event 1",
        received_at=t0 - 20,
    )
    ev2_id = queue.enqueue_event(
        source="gmail",
        key="thread-ev2",
        target_tag="intake/gmail/thread-ev2",
        external_id="msg-ev2",
        sender="owner@example.com",
        body="Please schedule event 2",
        received_at=t0 - 10,
    )
    assert ev1_id is not None and ev2_id is not None

    call_log: list[str] = []
    staged_cards: list[dict[str, Any]] = []

    class _NoopWatcher:
        def poll_once(self) -> dict[str, Any]:
            return {"status": "ok", "enqueued": 0}

    class _ProposalWritingRouter:
        def __init__(self) -> None:
            self._idx = 0

        def process_one(self, *, now: float | None = None) -> dict[str, Any] | None:
            del now
            self._idx += 1
            if self._idx == 1:
                call_log.append("route:ev1")
                write_proposal_atomic(
                    cfg.proposals_pending_dir,
                    {
                        "id": "prop-from-ev1",
                        "action": "calendar.create",
                        "args": {
                            "summary": "Event One",
                            "start": "2026-11-25T10:00:00-08:00",
                            "end": "2026-11-25T11:00:00-08:00",
                        },
                        "reason": "Scheduled from event 1.",
                        "expires_at": "2026-11-26T18:00:00-08:00",
                        "from_session": "intake/gmail/thread-ev1",
                        "urgent": False,
                    },
                )
                return {
                    "status": "done",
                    "event_id": ev1_id,
                    "source": "gmail",
                    "tag": "intake/gmail/thread-ev1",
                    "response": "INFORM: Proposed Event One.",
                }
            if self._idx == 2:
                call_log.append("route:ev2")
                write_proposal_atomic(
                    cfg.proposals_pending_dir,
                    {
                        "id": "prop-from-ev2",
                        "action": "calendar.create",
                        "args": {
                            "summary": "Event Two",
                            "start": "2026-11-25T14:00:00-08:00",
                            "end": "2026-11-25T15:00:00-08:00",
                        },
                        "reason": "Scheduled from event 2.",
                        "expires_at": "2026-11-26T18:00:00-08:00",
                        "from_session": "intake/gmail/thread-ev2",
                        "urgent": False,
                    },
                )
                return {
                    "status": "done",
                    "event_id": ev2_id,
                    "source": "gmail",
                    "tag": "intake/gmail/thread-ev2",
                    "response": "INFORM: Proposed Event Two.",
                }
            return None

    real_gate = BriefApprovalGate(
        cfg,
        queue=queue,
        stage_card_fn=lambda **kw: staged_cards.append(kw)
        or {"decision_id": f"dec-{kw['payload']['proposal_id']}"},
        now_fn=lambda: t0,
    )

    class _SpyGate:
        def scan_pending_proposals(self, now: float | None = None) -> dict[str, Any]:
            res = real_gate.scan_pending_proposals(now=now)
            call_log.append(f"scan:{','.join(res['staged_ids']) or 'none'}")
            return res

        def expire_stale_proposals(self, now: float | None = None) -> list[str]:
            call_log.append("expire")
            return real_gate.expire_stale_proposals(now=now)

    delivered_msgs: list[tuple[int, str]] = []
    runner = BriefDaemonRunner(
        cfg,
        queue=queue,
        gmail_watcher=_NoopWatcher(),  # type: ignore[arg-type]
        calendar_watcher=_NoopWatcher(),  # type: ignore[arg-type]
        router=_ProposalWritingRouter(),  # type: ignore[arg-type]
        delivery_gate=BriefDeliveryGate(
            cfg, post_fn=lambda ch, msg: delivered_msgs.append((ch, msg)) or True
        ),
        approval_gate=_SpyGate(),  # type: ignore[arg-type]
        watchdog_fn=lambda _c, _q, **_kw: {"healthy": True},
        now_fn=lambda: t0,
    )
    # Prevent morning_brief / daily_review timers from enqueuing extra events
    runner._last_morning_brief_date = "2026-11-24"
    runner._last_daily_review_date = "2026-11-24"

    tick = runner.run_tick(now=t0)
    assert tick["errors"] == {}
    assert tick["routed"] == 2
    assert tick["delivered"] == 2
    # Crucial invariant: prop-from-ev1 was scanned & staged immediately after ev1, before ev2 routed
    assert call_log == [
        "route:ev1",
        "scan:prop-from-ev1",
        "route:ev2",
        "scan:prop-from-ev2",
        "expire",
        "scan:none",
    ]
    assert tick["gate"]["staged"] == 2
    assert tick["gate"]["staged_ids"] == ["prop-from-ev1", "prop-from-ev2"]
    assert len(staged_cards) == 2

    # Verify per-event scan error does not abort remaining queued events
    class _FailingOnceGate:
        def __init__(self) -> None:
            self.calls = 0

        def scan_pending_proposals(self, now: float | None = None) -> dict[str, Any]:
            del now
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient gate scan error")
            return {
                "scanned": 1,
                "staged": 1,
                "skipped_existing": 0,
                "rejected": 0,
                "expired": 0,
                "deferred_quiet_hours": 0,
                "deferred_budget": 0,
                "staged_ids": ["prop-recovered"],
                "rejected_ids": [],
                "expired_ids": [],
                "errors": [],
            }

        def expire_stale_proposals(self, now: float | None = None) -> list[str]:
            del now
            return []

    class _TwoEventRouter:
        def __init__(self) -> None:
            self.count = 0

        def process_one(self, *, now: float | None = None) -> dict[str, Any] | None:
            del now
            self.count += 1
            if self.count <= 2:
                return {
                    "status": "done",
                    "event_id": self.count,
                    "source": "gmail",
                    "tag": "intake/gmail/t",
                    "response": "INFORM: ok",
                }
            return None

    runner2 = BriefDaemonRunner(
        cfg,
        queue=queue,
        gmail_watcher=_NoopWatcher(),  # type: ignore[arg-type]
        calendar_watcher=_NoopWatcher(),  # type: ignore[arg-type]
        router=_TwoEventRouter(),  # type: ignore[arg-type]
        delivery_gate=BriefDeliveryGate(cfg, post_fn=lambda _ch, _msg: True),
        approval_gate=_FailingOnceGate(),  # type: ignore[arg-type]
        watchdog_fn=lambda _c, _q, **_kw: {"healthy": True},
        now_fn=lambda: t0 + 30.0,
    )
    runner2._last_morning_brief_date = "2026-11-24"
    runner2._last_daily_review_date = "2026-11-24"

    tick_err = runner2.run_tick(now=t0 + 30.0)
    assert tick_err["routed"] == 2
    assert "gate" in tick_err["errors"]
    assert tick_err["gate"]["staged"] >= 1
    assert "prop-recovered" in tick_err["gate"]["staged_ids"]
