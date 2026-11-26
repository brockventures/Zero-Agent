"""Unit tests for BRIEF pure-Python watchdog and operator CLI."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Self

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.brief import cli
from tools.brief.config import (
    DEFAULT_CHANNEL_ID,
    BriefConfig,
    is_kill_switch_active,
    touch_heartbeat,
)
from tools.brief.queue import BriefQueue
from tools.brief.watchdog import check_brief_health, post_brief_alert, run_watchdog


def _make_cfg(tmp_path: Path, *, dry_run: bool = True) -> BriefConfig:
    return BriefConfig(
        allowlisted_emails=["owner@example.com"],
        gmail_interval_seconds=120,
        calendar_interval_seconds=300,
        router_interval_seconds=15,
        dry_run=dry_run,
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        prompts_dir=REPO_ROOT / "prompts" / "brief",
    )


def test_check_brief_health_stale_and_corrupt_heartbeats(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()

    now = 1795500000.0
    # Exactly at 3x threshold is still healthy; > 3x is stale.
    touch_heartbeat(cfg.heartbeats_dir, "router", now=now - 45.0)
    # Also verify .ts heartbeat files are supported alongside .hb.
    cfg.heartbeats_dir.mkdir(parents=True, exist_ok=True)
    (cfg.heartbeats_dir / "gmail_watcher.ts").write_text(f"{now - 361.0}\n", encoding="utf-8")
    (cfg.heartbeats_dir / "calendar_watcher.ts").write_text("not-a-float\n", encoding="utf-8")

    health = check_brief_health(cfg, queue, now=now)
    assert health["healthy"] is False
    assert health["heartbeats"]["router"]["status"] == "ok"
    assert health["stale_heartbeats"] == ["gmail_watcher"]
    assert health["corrupt_heartbeats"] == ["calendar_watcher"]
    assert "PT" in health["checked_at_pt"]

    # Fix both heartbeats -> healthy.
    (cfg.heartbeats_dir / "gmail_watcher.ts").write_text(f"{now - 100.0}\n", encoding="utf-8")
    (cfg.heartbeats_dir / "calendar_watcher.ts").write_text(f"{now - 200.0}\n", encoding="utf-8")
    healthy_report = check_brief_health(cfg, queue, now=now)
    assert healthy_report["healthy"] is True
    assert healthy_report["stale_heartbeats"] == []
    assert healthy_report["corrupt_heartbeats"] == []


def test_check_brief_health_missing_expected_and_failed_events(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()

    # Cursor exists but gmail_watcher heartbeat is missing -> flagged.
    cfg.cursors_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.cursors_path.write_text(json.dumps({"gmail_history_id": "12345"}), encoding="utf-8")

    # Enqueue and permanently fail an event.
    ev_id = queue.enqueue_event(
        source="gmail",
        key="thread-fail-1",
        target_tag="intake/gmail/thread-fail-1",
        external_id="msg-fail-1",
        sender="someone@example.com",
        body="test failure",
        received_at=1795499000.0,
    )
    assert ev_id is not None
    claimed = queue.claim_next_event(now=1795499100.0)
    assert claimed is not None
    outcome = queue.mark_failed_or_retry(ev_id, "agy timeout", max_retries=1, now=1795499200.0)
    assert outcome == "failed"

    health = check_brief_health(cfg, queue, now=1795499300.0)
    assert health["healthy"] is False
    assert "gmail_watcher" in health["missing_heartbeats"]
    assert health["failed_count"] == 1
    assert health["failed_events"][0]["id"] == ev_id


def test_post_brief_alert_dry_run_cooldown_and_formatting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "watchdog_alert_state.json"

    def _forbid_urlopen(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry_run=True must not perform network I/O")

    monkeypatch.setattr(urllib.request, "urlopen", _forbid_urlopen)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    t0 = 1795500000.0
    assert (
        post_brief_alert(
            "Alert with link https://example.com/status",
            dry_run=True,
            state_path=state_path,
            cooldown_seconds=1800,
            now=t0,
        )
        is True
    )
    # Identical alert within cooldown_seconds is suppressed.
    assert (
        post_brief_alert(
            "Alert with link https://example.com/status",
            dry_run=True,
            state_path=state_path,
            cooldown_seconds=1800,
            now=t0 + 600.0,
        )
        is False
    )
    # Distinct alert is sent immediately.
    assert (
        post_brief_alert(
            "Different alert",
            dry_run=True,
            state_path=state_path,
            cooldown_seconds=1800,
            now=t0 + 600.0,
        )
        is True
    )
    # Original alert fires again once cooldown expires.
    assert (
        post_brief_alert(
            "Alert with link https://example.com/status",
            dry_run=True,
            state_path=state_path,
            cooldown_seconds=1800,
            now=t0 + 1801.0,
        )
        is True
    )


def test_post_brief_alert_live_discord_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "watchdog_alert_state.json"
    captured: list[dict[str, Any]] = []

    class _FakeResp:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"id": "999"}'

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        headers = {k.lower(): v for k, v in req.header_items()}
        captured.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": headers,
                "body": json.loads(req.data.decode("utf-8")) if req.data else None,
                "timeout": timeout,
            }
        )
        return _FakeResp()

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-bot-token")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    long_msg = "Check https://example.com/runbook " + ("x" * 2200)
    sent = post_brief_alert(
        long_msg,
        channel_id=DEFAULT_CHANNEL_ID,
        dry_run=False,
        state_path=state_path,
        cooldown_seconds=1800,
        now=1795500000.0,
    )
    assert sent is True
    assert len(captured) == 1
    req_info = captured[0]
    assert req_info["method"] == "POST"
    assert req_info["url"] == f"https://discord.com/api/v10/channels/{DEFAULT_CHANNEL_ID}/messages"
    assert req_info["headers"]["authorization"] == "Bot test-bot-token"
    content = req_info["body"]["content"]
    assert "<https://example.com/runbook>" in content
    assert len(content) <= 1900


def test_run_watchdog_dedupes_ongoing_incident_across_minutes(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, dry_run=True)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=cfg.allowlisted_emails)
    queue.init_db()

    t0 = 1795500000.0
    touch_heartbeat(cfg.heartbeats_dir, "router", now=t0 - 120.0)

    first = run_watchdog(cfg, queue, dry_run=True, now=t0)
    assert first["healthy"] is False
    assert first["alert_dispatched"] is True
    assert "router" in str(first["alert_message"])
    assert "PT" in str(first["alert_message"])

    # 60s later, same stale router incident must be suppressed even though PT clock changed.
    second = run_watchdog(cfg, queue, dry_run=True, now=t0 + 60.0)
    assert second["healthy"] is False
    assert second["alert_dispatched"] is False
    assert second["alert_suppressed"] is True

    # Adding a failed event changes the incident fingerprint and alerts immediately.
    ev_id = queue.enqueue_event(
        source="gmail",
        key="thread-new-fail",
        target_tag="intake/gmail/thread-new-fail",
        external_id="msg-new-fail",
        sender="someone@example.com",
        body="boom",
        received_at=t0,
    )
    assert ev_id is not None
    queue.claim_next_event(now=t0)
    queue.mark_failed_or_retry(ev_id, "boom", max_retries=1, now=t0 + 65.0)
    third = run_watchdog(cfg, queue, dry_run=True, now=t0 + 70.0)
    assert third["alert_dispatched"] is True
    assert "Failed queue events: 1" in str(third["alert_message"])


def test_cli_kill_switch_preserves_queue_and_blocks_poll_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    memory_dir = tmp_path / "memory"
    cfg = BriefConfig(state_dir=state_dir, memory_dir=memory_dir)
    queue = BriefQueue(cfg.db_path, allowlisted_senders=["owner@example.com"])
    queue.init_db()
    ev_id = queue.enqueue_event(
        source="gmail",
        key="thread-ks",
        target_tag="intake/gmail/thread-ks",
        external_id="msg-ks-1",
        sender="owner@example.com",
        body="Please keep me queued",
        received_at=1795500000.0,
    )
    assert ev_id is not None

    rc = cli.main(
        [
            "--state-dir",
            str(state_dir),
            "--memory-dir",
            str(memory_dir),
            "kill-switch",
            "on",
            "--reason",
            "maintenance",
        ]
    )
    assert rc == 0
    assert is_kill_switch_active(cfg) is True
    assert queue.get_queue_stats()["pending"] == 1
    capsys.readouterr()

    def _forbid_poll(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("poll-once must not poll watchers when kill switch is active")

    monkeypatch.setattr(cli.GmailWatcher, "poll_once", _forbid_poll)
    monkeypatch.setattr(cli.CalendarWatcher, "poll_once", _forbid_poll)

    rc_poll = cli.main(
        [
            "poll-once",
            "--state-dir",
            str(state_dir),
            "--memory-dir",
            str(memory_dir),
            "--source",
            "all",
            "--dry-run",
        ]
    )
    assert rc_poll == 0
    assert "kill switch is active" in capsys.readouterr().out.lower()

    rc_off = cli.main(
        ["--state-dir", str(state_dir), "--memory-dir", str(memory_dir), "kill-switch", "off"]
    )
    assert rc_off == 0
    assert is_kill_switch_active(cfg) is False
    assert queue.get_queue_stats()["pending"] == 1


def test_cli_morning_brief_route_once_status_watchdog_and_replay(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "state"
    memory_dir = tmp_path / "memory"

    # Enqueue morning-brief + daily-review via CLI (both should succeed; second morning-brief is dup).
    assert cli.main(["--state-dir", str(state_dir), "--memory-dir", str(memory_dir), "morning-brief"]) == 0
    first_mb = json.loads(capsys.readouterr().out)
    assert first_mb["event_id"] is not None
    assert first_mb["duplicate"] is False

    assert cli.main(["--state-dir", str(state_dir), "--memory-dir", str(memory_dir), "morning-brief"]) == 0
    second_mb = json.loads(capsys.readouterr().out)
    assert second_mb["event_id"] is None
    assert second_mb["duplicate"] is True

    assert cli.main(["--state-dir", str(state_dir), "--memory-dir", str(memory_dir), "daily-review"]) == 0
    review_out = json.loads(capsys.readouterr().out)
    assert review_out["event_id"] is not None

    # Drain all pending events with route-once --dry-run.
    assert (
        cli.main(
            [
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "route-once",
                "--dry-run",
            ]
        )
        == 0
    )
    routed = json.loads(capsys.readouterr().out)
    assert routed["processed"] == 2
    assert {item["tag"] for item in routed["outcomes"]} == {"timer/morning-brief", "internal/review"}

    # Check status --json reports queue stats, sessions, cursors, kill_switch, and health.
    assert (
        cli.main(
            [
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "status",
                "--json",
            ]
        )
        == 0
    )
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["queue"]["done"] == 2
    assert status_payload["queue"]["pending"] == 0
    assert len(status_payload["sessions"]) == 2
    assert status_payload["kill_switch"]["active"] is False
    assert status_payload["cursors"]["exists"] is False
    assert status_payload["health"]["healthy"] is True

    # Run watchdog --dry-run.
    assert (
        cli.main(
            [
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "watchdog",
                "--dry-run",
            ]
        )
        == 0
    )
    wd_payload = json.loads(capsys.readouterr().out)
    assert wd_payload["healthy"] is True
    assert wd_payload["alert_dispatched"] is False

    # Run replay command.
    assert cli.main(["replay"]) == 0
    replay_out = capsys.readouterr().out
    assert "Overall: PASS" in replay_out


def test_cli_gate_status_mint_oauth_urls_and_exchange_oauth_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno

    from tools.brief.actions import READONLY_OAUTH_SCOPES, WRITE_OAUTH_SCOPES

    state_dir = tmp_path / "state"
    memory_dir = tmp_path / "memory"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    readonly_tok = secrets_dir / "brief_readonly_token.json"
    write_tok = secrets_dir / "brief_write_token.json"
    oauth_client = secrets_dir / "google_oauth.json"
    oauth_client.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "cli-test-client.apps.googleusercontent.com",
                    "client_secret": "CLI_TEST_CLIENT_SECRET_DO_NOT_LEAK",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )

    cfg_path = memory_dir / "config.toml"
    memory_dir.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "\n".join(
            [
                "[oauth]",
                f'readonly_token_path = "{readonly_tok}"',
                "allow_shared_readonly_fallback = false",
                "",
                "[gate]",
                f'write_token_path = "{write_tok}"',
                "allow_shared_oauth_fallback = false",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # 1. gate-status before token files exist
    assert (
        cli.main(
            [
                "--config",
                str(cfg_path),
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "gate-status",
                "--json",
            ]
        )
        == 0
    )
    gs_json = json.loads(capsys.readouterr().out)
    assert gs_json["readonly_token_configured"] is False
    assert gs_json["readonly_token_path"] == str(readonly_tok)
    assert gs_json["allow_shared_readonly_fallback"] is False
    assert gs_json["write_token_configured"] is False
    assert gs_json["write_token_path"] == str(write_tok)
    assert gs_json["allow_shared_oauth_fallback"] is False

    # 2. mint-oauth-urls includes readonly_token_path and updated WRITE_OAUTH_SCOPES
    assert (
        cli.main(
            [
                "--config",
                str(cfg_path),
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "mint-oauth-urls",
                "--oauth-client-path",
                str(oauth_client),
                "--json",
            ]
        )
        == 0
    )
    mint_payload = json.loads(capsys.readouterr().out)
    assert mint_payload["readonly_token_path"] == str(readonly_tok)
    assert mint_payload["write_token_path"] == str(write_tok)
    assert mint_payload["write_scopes"] == WRITE_OAUTH_SCOPES
    assert "gmail.compose" in mint_payload["write_url"]
    assert "CLI_TEST_CLIENT_SECRET_DO_NOT_LEAK" not in json.dumps(mint_payload)

    # 3. exchange-oauth-code for readonly and write roles
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
        del timeout
        raw_body = req.data.decode("utf-8") if req.data else ""
        if "code=ro-auth-code" in raw_body:
            return _FakeResp(
                {
                    "access_token": "ya29.ro-access",
                    "refresh_token": "RO_REFRESH_SECRET_NEVER_PRINT",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": READONLY_OAUTH_SCOPES,
                }
            )
        if "code=wr-auth-code" in raw_body:
            return _FakeResp(
                {
                    "access_token": "ya29.wr-access",
                    "refresh_token": "WR_REFRESH_SECRET_NEVER_PRINT",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": WRITE_OAUTH_SCOPES,
                }
            )
        if "code=bad-scope-code" in raw_body:
            return _FakeResp(
                {
                    "access_token": "ya29.bad-access",
                    "refresh_token": "BAD_REFRESH",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": f"{WRITE_OAUTH_SCOPES} https://www.googleapis.com/auth/gmail.send",
                }
            )
        raise AssertionError(f"Unexpected token request body: {raw_body}")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    assert (
        cli.main(
            [
                "--config",
                str(cfg_path),
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "exchange-oauth-code",
                "--role",
                "readonly",
                "--code",
                "http://localhost/?code=ro-auth-code&scope=ignored",
                "--oauth-client-path",
                str(oauth_client),
                "--json",
            ]
        )
        == 0
    )
    ro_out = capsys.readouterr().out
    ro_json = json.loads(ro_out)
    assert ro_json["ok"] is True
    assert ro_json["role"] == "readonly"
    assert ro_json["output_path"] == str(readonly_tok)
    assert ro_json["written"] is True
    assert "RO_REFRESH_SECRET_NEVER_PRINT" not in ro_out
    assert "CLI_TEST_CLIENT_SECRET_DO_NOT_LEAK" not in ro_out
    assert readonly_tok.is_file()
    assert (readonly_tok.stat().st_mode & 0o777) == 0o600

    staged_write = tmp_path / "staged" / "brief_write_token.json"
    assert (
        cli.main(
            [
                "--config",
                str(cfg_path),
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "exchange-oauth-code",
                "--role",
                "write",
                "--code",
                "wr-auth-code",
                "--output",
                str(staged_write),
                "--oauth-client-path",
                str(oauth_client),
            ]
        )
        == 0
    )
    wr_text = capsys.readouterr().out
    assert "BRIEF Google OAuth write token saved" in wr_text
    assert str(staged_write) in wr_text
    assert "WR_REFRESH_SECRET_NEVER_PRINT" not in wr_text
    assert "CLI_TEST_CLIENT_SECRET_DO_NOT_LEAK" not in wr_text
    assert staged_write.is_file()
    assert (staged_write.stat().st_mode & 0o777) == 0o600

    # Copy staged token to configured path and verify gate-status text & JSON reflect both tokens
    write_tok.write_text(staged_write.read_text(encoding="utf-8"), encoding="utf-8")
    assert (
        cli.main(
            [
                "--config",
                str(cfg_path),
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "gate-status",
            ]
        )
        == 0
    )
    gs_text = capsys.readouterr().out
    assert f"Read-Only Token: configured ({readonly_tok})" in gs_text
    assert f"Write Token    : configured ({write_tok})" in gs_text

    # 4. Forbidden gmail.send scope fails closed without writing output
    rejected_out = tmp_path / "rejected_write.json"
    assert (
        cli.main(
            [
                "--config",
                str(cfg_path),
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "exchange-oauth-code",
                "--role",
                "write",
                "--code",
                "bad-scope-code",
                "--output",
                str(rejected_out),
                "--oauth-client-path",
                str(oauth_client),
                "--json",
            ]
        )
        == 1
    )
    err_json = json.loads(capsys.readouterr().out)
    assert err_json["ok"] is False
    assert "prohibited scope" in err_json["error"].lower()
    assert not rejected_out.exists()

    # 5. Read-only /secrets mount (EROFS) returns graceful --output staging hint without secrets
    def _raise_erofs(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(cli, "exchange_oauth_authorization_code", _raise_erofs)
    assert (
        cli.main(
            [
                "--config",
                str(cfg_path),
                "--state-dir",
                str(state_dir),
                "--memory-dir",
                str(memory_dir),
                "exchange-oauth-code",
                "--role",
                "write",
                "--code",
                "wr-auth-code",
                "--oauth-client-path",
                str(oauth_client),
            ]
        )
        == 1
    )
    captured_ro = capsys.readouterr()
    assert "--output" in captured_ro.err
    assert "read-only" in captured_ro.err.lower()
    assert "CLI_TEST_CLIENT_SECRET_DO_NOT_LEAK" not in captured_ro.err
