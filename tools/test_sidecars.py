# ruff: noqa
"""Unit tests for sidecars.py (marketing sweep, heartbeat sweeps, retry guards, recovery alerts)."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

WORKTREE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKTREE_ROOT) in sys.path:
    sys.path.remove(str(WORKTREE_ROOT))
sys.path.insert(0, str(WORKTREE_ROOT))

import tools.sidecars as sidecars


SAMPLE_SENDERS = [
    {"name": "Delta Dental", "email": "email@member-deltadental.org", "count": 1},
    {"name": "GrowFit Camp", "email": "redwoodcity@growfitpe.com", "count": 1},
    {"name": "Loop@stanfordalumni.org", "email": "loop@stanfordalumni.org", "count": 1},
    {"name": "Nyjeri Whipps, Stanford Engineering Fund", "email": "engineeringfund@stanford.edu", "count": 1},
    {"name": "Raley's Something Extra", "email": "se-email@raleysstores.com", "count": 1},
    {"name": "RENPHO US", "email": "hello@mail.renpho.com", "count": 1},
    {"name": "RENPHO US", "email": "information@mail.renpho.com", "count": 1},
    {"name": "Senator Alex Padilla", "email": "donotreply@padilla.senate.gov", "count": 1},
    {"name": "Whole Foods Market", "email": "wholefoodsmarket@mail.wholefoodsmarket.com", "count": 1},
    {"name": "Wing & Barrel Ranch", "email": "membership@wingandbarrelranch.com", "count": 1},
]


def _setup_isolated_paths(monkeypatch, tmp_path: Path):
    senders_file = tmp_path / "marketing_senders.json"
    unsub_file = tmp_path / "marketing_unsubscribed.json"
    last_post_file = tmp_path / "last_sidecar_post.json"
    state_file = tmp_path / "marketing_sweep_last_run"

    monkeypatch.setattr(sidecars, "MARKETING_SENDERS_FILE", str(senders_file))
    monkeypatch.setattr(sidecars, "MARKETING_UNSUBSCRIBED_FILE", str(unsub_file))
    monkeypatch.setattr(sidecars, "LAST_SIDECAR_POST_FILE", str(last_post_file))
    monkeypatch.setattr(sidecars, "MARKETING_STATE_FILE", str(state_file))
    return senders_file, unsub_file, last_post_file, state_file


def test_unsubscribe_marketing_senders_by_indices_and_emails(monkeypatch, tmp_path):
    senders_file, unsub_file, _, _ = _setup_isolated_paths(monkeypatch, tmp_path)
    senders_file.write_text(json.dumps(SAMPLE_SENDERS, indent=2), encoding="utf-8")

    def fake_unsub_and_trash(addr: str, name: str = "") -> dict:
        return {
            "name": name or addr,
            "email": addr,
            "trashed_count": 3,
            "message_ids": ["m1", "m2", "m3"],
            "unsubscribe_url": f"https://example.com/unsub?e={addr}",
            "unsubscribe_http_ok": True,
            "filter_created": True,
        }

    monkeypatch.setattr(sidecars, "_unsubscribe_and_trash_sender", fake_unsub_and_trash)

    res = sidecars.unsubscribe_marketing_senders([5, 6, 8])
    assert res["ok"] is True
    expected = {
        "se-email@raleysstores.com",
        "hello@mail.renpho.com",
        "information@mail.renpho.com",
        "donotreply@padilla.senate.gov",
    }
    assert set(res["emails"]) == expected
    assert res["trashed_total"] == 12

    persisted = json.loads(unsub_file.read_text(encoding="utf-8"))
    assert expected.issubset(set(persisted["emails"]))
    assert len(persisted["senders"]) == 4


def test_unsubscribe_and_trash_sender_gmail_api_flow(monkeypatch):
    import workspace_mcp

    monkeypatch.setattr(workspace_mcp, "_auth_headers", lambda: {"Authorization": "Bearer test-token"})
    calls = []

    class DummyResp:
        def __init__(self, payload: dict, status: int = 200):
            self._bytes = json.dumps(payload).encode("utf-8")
            self.status = status

        def read(self):
            return self._bytes

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=15):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        method = getattr(req, "method", "GET") or "GET"
        calls.append((method, url))
        if "/messages?" in url:
            return DummyResp({"messages": [{"id": "msg_101"}, {"id": "msg_102"}]})
        if "/messages/msg_101?format=metadata" in url:
            return DummyResp({
                "payload": {
                    "headers": [
                        {"name": "List-Unsubscribe", "value": "<https://unsub.example.org/oneclick>"},
                        {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
                    ]
                }
            })
        if url.startswith("https://unsub.example.org/oneclick"):
            return DummyResp({"ok": True}, status=200)
        if "/settings/filters" in url:
            return DummyResp({"id": "flt_1"}, status=200)
        if url.endswith("/trash"):
            return DummyResp({"id": "trashed"}, status=200)
        return DummyResp({})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = sidecars._unsubscribe_and_trash_sender("se-email@raleysstores.com", "Raley's Something Extra")

    assert res["email"] == "se-email@raleysstores.com"
    assert res["trashed_count"] == 2
    assert res["unsubscribe_url"] == "https://unsub.example.org/oneclick"
    assert res["unsubscribe_http_ok"] is True
    assert res["filter_created"] is False
    assert not any("/settings/filters" in u for _, u in calls)
    assert any(m == "POST" and u == "https://unsub.example.org/oneclick" for m, u in calls)
    assert sum(1 for m, u in calls if m == "POST" and u.endswith("/trash")) == 2


def test_run_marketing_sweep_excludes_unsubscribed_and_saves_post(monkeypatch, tmp_path):
    senders_file, unsub_file, last_post_file, _ = _setup_isolated_paths(monkeypatch, tmp_path)

    unsub_file.write_text(
        json.dumps({
            "emails": [
                "se-email@raleysstores.com",
                "hello@mail.renpho.com",
                "information@mail.renpho.com",
                "donotreply@padilla.senate.gov",
            ],
            "senders": [],
        }),
        encoding="utf-8",
    )

    fake_messages = [
        {"id": "1", "from": '"Delta Dental" <email@member-deltadental.org>'},
        {"id": "2", "from": '"Raley\'s Something Extra" <se-email@raleysstores.com>'},
        {"id": "3", "from": '"RENPHO US" <hello@mail.renpho.com>'},
        {"id": "4", "from": '"Senator Alex Padilla" <donotreply@padilla.senate.gov>'},
        {"id": "5", "from": '"Whole Foods Market" <wholefoodsmarket@mail.wholefoodsmarket.com>'},
    ]
    monkeypatch.setattr(
        sidecars,
        "gmail_search",
        lambda q, max_results=50: json.dumps({"ok": True, "messages": fake_messages}),
    )

    should_post, msg = sidecars.run_marketing_sweep(force=True)
    assert should_post is True
    assert "Delta Dental" in msg
    assert "Whole Foods Market" in msg
    assert "Raley's Something Extra" not in msg
    assert "RENPHO US" not in msg
    assert "Senator Alex Padilla" not in msg

    saved_senders = json.loads(senders_file.read_text(encoding="utf-8"))
    saved_emails = {s["email"] for s in saved_senders}
    assert saved_emails == {
        "email@member-deltadental.org",
        "wholefoodsmarket@mail.wholefoodsmarket.com",
    }

    assert last_post_file.exists()
    last_post = json.loads(last_post_file.read_text(encoding="utf-8"))
    assert last_post["job_id"] == "marketing"
    assert "Biweekly Marketing Report" in last_post["message"]
    assert len(last_post["senders"]) == 2


def test_get_recent_marketing_report_context(monkeypatch, tmp_path):
    senders_file, _, last_post_file, _ = _setup_isolated_paths(monkeypatch, tmp_path)

    # Empty when files do not exist
    assert sidecars.get_recent_marketing_report_context() == ""

    senders_file.write_text(json.dumps(SAMPLE_SENDERS[:3], indent=2), encoding="utf-8")
    sidecars.save_last_sidecar_post(
        job_id="marketing",
        title="Biweekly Marketing Report — Nov 08",
        message="📬 **Biweekly Marketing Report** — Nov 08\n1. Delta Dental — 1 email",
        senders=SAMPLE_SENDERS[:3],
    )

    ctx = sidecars.get_recent_marketing_report_context()
    assert "Biweekly Marketing Report" in ctx
    assert "1. Delta Dental <email@member-deltadental.org>" in ctx
    assert "2. GrowFit Camp <redwoodcity@growfitpe.com>" in ctx
    assert "3. Loop@stanfordalumni.org <loop@stanfordalumni.org>" in ctx


def _setup_heartbeat_env(monkeypatch, tmp_path: Path):
    state_file = tmp_path / "heartbeat_state.json"
    monkeypatch.setattr(sidecars, "HEARTBEAT_STATE_FILE", str(state_file))
    monkeypatch.setattr(sidecars, "HEARTBEAT_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        sidecars,
        "_resolve_nas_config",
        lambda: ("198.51.100.10", "198.51.100.11", "22"),
    )
    monkeypatch.setattr(sidecars, "check_slzb_stable_release", lambda force=False: {})
    return state_file


def test_heartbeat_transient_failure_cleared_on_retry(monkeypatch, tmp_path):
    state_file = _setup_heartbeat_env(monkeypatch, tmp_path)
    calls = []

    def fake_collect(host_1_ip: str, host_2_ip: str):
        calls.append((host_1_ip, host_2_ip))
        if len(calls) == 1:
            return [f"Host1 ({host_1_ip}) container `homeassistant` is exited"]
        return []

    monkeypatch.setattr(sidecars, "_collect_heartbeat_failures", fake_collect)

    healthy, report = sidecars.run_heartbeat_sweep()

    assert healthy is True
    assert len(calls) == 2
    assert "🟢 **Heartbeat Sweep Healthy**" in report
    assert sidecars.is_heartbeat_recovery_report(report) is False

    if state_file.exists():
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        assert saved.get("degraded") is False


def test_heartbeat_persistent_failure_persists_degraded(monkeypatch, tmp_path):
    state_file = _setup_heartbeat_env(monkeypatch, tmp_path)
    calls = []

    def fake_collect(host_1_ip: str, host_2_ip: str):
        calls.append((host_1_ip, host_2_ip))
        return [f"Home Assistant port `8123` unreachable on Host1 (`{host_1_ip}`)"]

    monkeypatch.setattr(sidecars, "_collect_heartbeat_failures", fake_collect)

    healthy, report = sidecars.run_heartbeat_sweep()

    assert healthy is False
    assert len(calls) == 2
    assert "🚨 **Heartbeat Alert — Infrastructure Degraded**" in report
    assert "Home Assistant port `8123` unreachable on Host1 (`198.51.100.10`)" in report
    assert sidecars.is_heartbeat_recovery_report(report) is False

    assert state_file.exists()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["degraded"] is True
    assert isinstance(saved["degraded_since_ts"], (int, float))
    assert saved["alert_count"] == 1
    assert "Home Assistant port `8123` unreachable on Host1 (`198.51.100.10`)" in saved["last_failures"]

    # Subsequent persistent sweep increments alert_count and preserves degraded_since_ts
    prev_ts = saved["degraded_since_ts"]
    healthy2, report2 = sidecars.run_heartbeat_sweep()
    assert healthy2 is False
    assert sidecars.is_heartbeat_recovery_report(report2) is False
    saved2 = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved2["degraded"] is True
    assert saved2["degraded_since_ts"] == prev_ts
    assert saved2["alert_count"] == 2


def test_heartbeat_recovery_and_subsequent_healthy_sweep(monkeypatch, tmp_path):
    state_file = _setup_heartbeat_env(monkeypatch, tmp_path)

    past_ts = time.time() - 1440.0  # 24 minutes ago
    initial_state = {
        "degraded": True,
        "degraded_since_ts": past_ts,
        "degraded_since_pt": "2026-11-22 07:35 AM PT",
        "last_failures": ["Home Assistant port `8123` unreachable on Host1 (`198.51.100.10`)"],
        "alert_count": 2,
    }
    state_file.write_text(json.dumps(initial_state, indent=2), encoding="utf-8")

    monkeypatch.setattr(sidecars, "_collect_heartbeat_failures", lambda h1, h2: [])

    # c) Healthy sweep after degraded=True returns recovery report
    healthy, report = sidecars.run_heartbeat_sweep()

    assert healthy is True
    assert sidecars.is_heartbeat_recovery_report(report) is True
    assert "🟢 **Heartbeat Recovered — Infrastructure Back Online**" in report
    assert "Outage detected: 2026-11-22 07:35 AM PT" in report
    assert "24m" in report

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["degraded"] is False
    assert saved["last_failures"] == []
    assert saved["alert_count"] == 0
    assert saved["recovered_at_pt"] is not None

    # d) Subsequent healthy sweep returns routine healthy message where is_heartbeat_recovery_report is False
    healthy2, report2 = sidecars.run_heartbeat_sweep()
    assert healthy2 is True
    assert sidecars.is_heartbeat_recovery_report(report2) is False
    assert "🟢 **Heartbeat Sweep Healthy**" in report2
    assert "Heartbeat Recovered" not in report2


def test_format_downtime():
    assert sidecars._format_downtime(0) == "< 1m"
    assert sidecars._format_downtime(45) == "< 1m"
    assert sidecars._format_downtime(60) == "1m"
    assert sidecars._format_downtime(1440) == "24m"
    assert sidecars._format_downtime(3600) == "1h"
    assert sidecars._format_downtime(4320) == "1h 12m"
    assert sidecars._format_downtime(86400) == "1d"
    assert sidecars._format_downtime(90000) == "1d 1h"


def test_heartbeat_state_corrupt_file_handling(monkeypatch, tmp_path):
    state_file = tmp_path / "heartbeat_state.json"
    state_file.write_text("{bad-json-syntax---", encoding="utf-8")
    monkeypatch.setattr(sidecars, "HEARTBEAT_STATE_FILE", str(state_file))

    state = sidecars._load_heartbeat_state()
    assert state["degraded"] is False
    assert state["alert_count"] == 0


def test_is_heartbeat_recovery_report():
    assert sidecars.is_heartbeat_recovery_report("🟢 **Heartbeat Recovered — Infrastructure Back Online** [2026-11-22 08:00 AM PT]") is True
    assert sidecars.is_heartbeat_recovery_report("🟢 **Heartbeat Sweep Healthy** — 2026-11-22 08:00 AM PT") is False
    assert sidecars.is_heartbeat_recovery_report("🚨 **Heartbeat Alert — Infrastructure Degraded**") is False
    assert sidecars.is_heartbeat_recovery_report("") is False
    assert sidecars.is_heartbeat_recovery_report(None) is False


def test_summarize_heartbeat_failure_and_recovery_details(monkeypatch, tmp_path):
    state_file = _setup_heartbeat_env(monkeypatch, tmp_path)

    raw_mcp_failure = (
        "Persistent MCP Daemon degraded or stopped: "
        "{'google-workspace': {'running': False, 'healthy': False, 'pid': 101}, "
        "'home-assistant': {'running': True, 'healthy': True, 'pid': 102}, "
        "'plex': {'running': False, 'healthy': False}}"
    )
    assert sidecars._summarize_heartbeat_failure(raw_mcp_failure) == (
        "Persistent MCP Daemon (google-workspace, plex)"
    )
    assert sidecars._summarize_heartbeat_failure("Persistent MCP Daemon degraded or stopped: {}") == (
        "Persistent MCP Daemon degraded or stopped"
    )
    assert sidecars._summarize_heartbeat_failure("Persistent MCP Daemon degraded or stopped: None") == (
        "Persistent MCP Daemon degraded or stopped"
    )
    assert sidecars._summarize_heartbeat_failure("Persistent MCP Daemon degraded or stopped: {broken_syntax") == (
        "Persistent MCP Daemon degraded or stopped"
    )
    long_failure = "Host1 (198.51.100.10) error: " + ("x" * 200)
    summarized_long = sidecars._summarize_heartbeat_failure(long_failure)
    assert len(summarized_long) == 180
    assert summarized_long.endswith("...")

    past_ts = time.time() - 900.0
    initial_state = {
        "degraded": True,
        "degraded_since_ts": past_ts,
        "degraded_since_pt": "2026-11-23 05:00 AM PT",
        "last_failures": [
            "Home Assistant port `8123` unreachable on Host1 (`198.51.100.10`)",
            raw_mcp_failure,
        ],
        "alert_count": 1,
    }
    state_file.write_text(json.dumps(initial_state, indent=2), encoding="utf-8")
    monkeypatch.setattr(sidecars, "_collect_heartbeat_failures", lambda h1, h2: [])

    healthy, report = sidecars.run_heartbeat_sweep()
    assert healthy is True
    assert sidecars.is_heartbeat_recovery_report(report) is True
    assert "Recovered:" in report
    assert "- Home Assistant port `8123` unreachable on Host1 (`198.51.100.10`)" in report
    assert "- Persistent MCP Daemon (google-workspace, plex)" in report


def test_heartbeat_update_state_false_and_nightly_triage(monkeypatch, tmp_path):
    state_file = _setup_heartbeat_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sidecars,
        "_collect_heartbeat_failures",
        lambda h1, h2: ["Dockhand port `3866` unreachable on Host2 (`198.51.100.11`)"],
    )

    healthy, report = sidecars.run_heartbeat_sweep(update_state=False)
    assert healthy is False
    assert "🚨 **Heartbeat Alert — Infrastructure Degraded**" in report
    assert not state_file.exists()

    # Check run_nightly_triage passes update_state=False
    observed_kwargs = []

    def fake_sweep(*args, **kwargs):
        observed_kwargs.append(kwargs)
        return True, "healthy"

    monkeypatch.setattr(sidecars, "run_heartbeat_sweep", fake_sweep)
    monkeypatch.setattr(sidecars, "calendar_list_events", lambda *a, **kw: json.dumps({"events": []}))
    monkeypatch.setattr(sidecars, "gmail_search", lambda *a, **kw: json.dumps({"messages": []}))
    monkeypatch.setattr(sidecars, "get_sidecar_health_summary", lambda **kw: {"active_failures": [], "latest_by_job": {}})
    monkeypatch.setattr(sidecars, "nas_docker", lambda *a, **kw: json.dumps({"ok": True, "output": ""}))

    sidecars.run_nightly_triage()
    assert observed_kwargs == [{"update_state": False}]


def test_heartbeat_duplicate_alert_suppression_and_realert(monkeypatch, tmp_path):
    state_file = _setup_heartbeat_env(monkeypatch, tmp_path)
    current_failures = ["Home Assistant port `8123` unreachable on Host1 (`198.51.100.10`)"]
    monkeypatch.setattr(sidecars, "_collect_heartbeat_failures", lambda h1, h2: list(current_failures))

    # 1. Initial failure alerts
    healthy1, report1 = sidecars.run_heartbeat_sweep()
    assert healthy1 is False
    assert "🚨 **Heartbeat Alert — Infrastructure Degraded**" in report1
    saved1 = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved1["alert_count"] == 1
    first_alert_ts = saved1["last_alert_ts"]
    assert isinstance(first_alert_ts, (int, float))

    # 2. Same failure within HEARTBEAT_REPEAT_ALERT_SECONDS is suppressed
    healthy2, report2 = sidecars.run_heartbeat_sweep()
    assert healthy2 is False
    assert report2 == ""
    saved2 = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved2["alert_count"] == 2
    assert saved2["last_alert_ts"] == first_alert_ts

    # 3. Changed failure triggers immediate re-alert
    current_failures[:] = [
        "Home Assistant port `8123` unreachable on Host1 (`198.51.100.10`)",
        "Plex port `32400` unreachable on Host1 (`198.51.100.10`)",
    ]
    healthy3, report3 = sidecars.run_heartbeat_sweep()
    assert healthy3 is False
    assert "Plex port `32400` unreachable" in report3
    saved3 = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved3["alert_count"] == 3

    # 4. Same failures in different order is suppressed
    current_failures.reverse()
    healthy4, report4 = sidecars.run_heartbeat_sweep()
    assert healthy4 is False
    assert report4 == ""

    # 5. Elapsed repeat interval triggers re-alert
    aged_state = json.loads(state_file.read_text(encoding="utf-8"))
    aged_state["last_alert_ts"] = time.time() - (sidecars.HEARTBEAT_REPEAT_ALERT_SECONDS + 10)
    state_file.write_text(json.dumps(aged_state, indent=2), encoding="utf-8")

    healthy5, report5 = sidecars.run_heartbeat_sweep()
    assert healthy5 is False
    assert "🚨 **Heartbeat Alert — Infrastructure Degraded**" in report5


def test_log_execution_heartbeat_degraded_recheck_cadence(monkeypatch, tmp_path):
    try:
        import tools.scheduler_tool as sched_mod
    except ImportError:
        import scheduler_tool as sched_mod
    try:
        import tools.bridge_scheduler as bshed_mod
    except ImportError:
        import bridge_scheduler as bshed_mod

    assert sidecars.HEARTBEAT_DEGRADED_RECHECK_SECONDS == 300

    log_file = tmp_path / "sidecar_execution_log.json"
    status_file = tmp_path / "sidecar_status.json"
    sched_file = tmp_path / "schedule.json"

    monkeypatch.setattr(sidecars, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sidecars, "EXECUTION_LOG_FILE", log_file)
    monkeypatch.setattr(sidecars, "EXECUTION_STATUS_FILE", status_file)
    monkeypatch.setattr(sched_mod, "SCHEDULE_FILE", sched_file)

    initial_jobs = [
        {
            "id": "heartbeat_sweep",
            "name": "Heartbeat Sweep",
            "schedule_type": "interval",
            "interval_seconds": 7200,
            "enabled": True,
            "next_run_ts": 0,
        }
    ]
    sched_file.write_text(json.dumps(initial_jobs, indent=2), encoding="utf-8")

    # Degraded run (warning/error) schedules 5m (300s) recheck and sets active_recheck_seconds
    entry_err = sidecars.log_execution("heartbeat", "Heartbeat Sweep", "warning", 1.2, summary="Degraded")
    jobs_after_err = json.loads(sched_file.read_text(encoding="utf-8"))
    assert jobs_after_err[0]["next_run_ts"] == entry_err["timestamp_epoch"] + sidecars.HEARTBEAT_DEGRADED_RECHECK_SECONDS
    assert jobs_after_err[0]["active_recheck_seconds"] == 300
    assert bshed_mod.should_run_job(jobs_after_err[0], jobs_after_err[0]["next_run_ts"], status_map={}) == (True, "on_time")

    # Anti-double-run guard still blocks a premature run within min_period (< 300 * 0.7 = 210s)
    premature_job = dict(jobs_after_err[0], next_run_ts=entry_err["timestamp_epoch"] + 150)
    should_run_early, early_reason = bshed_mod.should_run_job(premature_job, premature_job["next_run_ts"], status_map={})
    assert should_run_early is False
    assert "already ran in current period" in early_reason

    # Bool guard: active_recheck_seconds=True is ignored in favor of base interval_seconds (7200s)
    bool_recheck_job = dict(jobs_after_err[0], active_recheck_seconds=True)
    should_run_bool, bool_reason = bshed_mod.should_run_job(bool_recheck_job, bool_recheck_job["next_run_ts"], status_map={})
    assert should_run_bool is False
    assert "already ran in current period" in bool_reason

    # Healthy run restores normal 2h (7200s) cadence and removes active_recheck_seconds
    entry_ok = sidecars.log_execution("heartbeat", "Heartbeat Sweep", "ok", 0.8, summary="Healthy")
    jobs_after_ok = json.loads(sched_file.read_text(encoding="utf-8"))
    assert "active_recheck_seconds" not in jobs_after_ok[0]
    assert jobs_after_ok[0]["next_run_ts"] == entry_ok["timestamp_epoch"] + 7200


