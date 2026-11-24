"""Unit tests for BRIEF config, scaffold, runtime_rules registration, and SQLite WAL queue."""

from __future__ import annotations

import json
import sqlite3
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.brief.config import (
    DEFAULT_CHANNEL_ID,
    BriefConfig,
    ensure_memory_scaffold,
    is_kill_switch_active,
    set_kill_switch,
    touch_heartbeat,
)
from tools.brief.queue import BriefQueue, list_sessions


def test_runtime_rules_registers_brief_channel() -> None:
    rules_path = REPO_ROOT / "config" / "runtime_rules.json"
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    assert DEFAULT_CHANNEL_ID in data["home_channel_ids"]
    # Existing fields remain intact
    assert data["operations_category_id"] == 1544953274363412533
    assert data["forge_channel_id"] == 1562899941595160657


def test_brief_toml_example_is_valid_and_pii_free() -> None:
    example_path = REPO_ROOT / "config" / "brief.toml.example"
    raw = example_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(raw)
    assert parsed["channel"]["channel_id"] == 1574655816236269569
    assert parsed["owner"]["allowlisted_emails"] == ["owner@example.com"]
    assert parsed["owner"]["vip_senders"] == ["partner@example.com"]
    assert parsed["limits"]["timezone"] == "America/Los_Angeles"
    assert parsed["limits"]["max_overnight_bypasses"] == 3
    assert parsed["limits"]["imminent_event_minutes"] == 60
    assert parsed["filtering"]["automated_domain_allowlist"] == ["alerts.example.org"]
    assert parsed["filtering"]["blocked_domains"] == ["marketing.example.net"]
    assert parsed["sessions"]["email_grain"] == "thread"
    assert parsed["safety"]["dry_run"] is True


def test_config_wave1_fields_defaults_and_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRIEF_OWNER_EMAILS", raising=False)
    defaults_cfg = BriefConfig()
    assert defaults_cfg.vip_senders == []
    assert defaults_cfg.max_overnight_bypasses == 3
    assert defaults_cfg.imminent_event_minutes == 60
    assert defaults_cfg.automated_domain_allowlist == []
    assert defaults_cfg.blocked_domains == []

    # Backward compatibility: legacy TOML without new sections loads safe defaults
    legacy_toml = tmp_path / "legacy.toml"
    legacy_toml.write_text(
        '[owner]\nallowlisted_emails = ["owner@example.com"]\n',
        encoding="utf-8",
    )
    legacy_cfg = BriefConfig.load(
        config_path=legacy_toml,
        state_dir=tmp_path / "s1",
        memory_dir=tmp_path / "m1",
        secrets_path=tmp_path / "no-secrets.json",
    )
    assert legacy_cfg.vip_senders == []
    assert legacy_cfg.max_overnight_bypasses == 3
    assert legacy_cfg.imminent_event_minutes == 60
    assert legacy_cfg.automated_domain_allowlist == []
    assert legacy_cfg.blocked_domains == []

    wave1_toml = tmp_path / "wave1.toml"
    wave1_toml.write_text(
        '[owner]\n'
        'allowlisted_emails = ["owner@example.com"]\n'
        'vip_senders = ["VIP Partner <Partner@Example.COM>", "school@example.org", "partner@example.com"]\n'
        '[limits]\n'
        'max_overnight_bypasses = 2\n'
        'imminent_event_minutes = 45\n'
        '[filtering]\n'
        'automated_domain_allowlist = [" @Konstella.COM ", ".alerts.example.org.", "konstella.com"]\n'
        'blocked_domains = ["@Promo.Example.NET", "promo.example.net", "  ads.example.com  "]\n',
        encoding="utf-8",
    )
    wave1_cfg = BriefConfig.load(
        config_path=wave1_toml,
        state_dir=tmp_path / "s2",
        memory_dir=tmp_path / "m2",
        secrets_path=tmp_path / "no-secrets.json",
    )
    assert wave1_cfg.vip_senders == ["partner@example.com", "school@example.org"]
    assert wave1_cfg.max_overnight_bypasses == 2
    assert wave1_cfg.imminent_event_minutes == 45
    assert wave1_cfg.automated_domain_allowlist == ["konstella.com", "alerts.example.org"]
    assert wave1_cfg.blocked_domains == ["promo.example.net", "ads.example.com"]


def test_layered_prompts_exist_and_cover_core_contracts() -> None:
    prompts_dir = REPO_ROOT / "prompts" / "brief"
    for name in ("base.md", "intake.md", "task.md", "review.md"):
        p = prompts_dir / name
        assert p.is_file(), f"Missing prompt file: {p}"
        assert p.read_text(encoding="utf-8").strip()

    base = (prompts_dir / "base.md").read_text(encoding="utf-8")
    assert "PT" in base
    assert "trusted=false" in base
    assert "preferences.md" in base


def test_config_load_precedence_and_email_merging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    # Write both memory and state configs to verify memory_dir wins over state_dir
    (state_dir / "config.toml").write_text(
        '[owner]\nallowlisted_emails = ["state@example.com"]\n[safety]\ndry_run = false\n',
        encoding="utf-8",
    )
    (memory_dir / "config.toml").write_text(
        '[owner]\nallowlisted_emails = ["MemoryOwner@Example.COM"]\n'
        '[polling]\ngmail_interval_seconds = 90\n'
        '[safety]\ndry_run = true\n',
        encoding="utf-8",
    )

    secrets_file = tmp_path / "env.json"
    secrets_file.write_text(
        json.dumps({"OWNER_EMAIL": "secret-owner@example.org"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BRIEF_OWNER_EMAILS", "env1@example.com, MemoryOwner@Example.COM")

    cfg = BriefConfig.load(
        state_dir=state_dir,
        memory_dir=memory_dir,
        secrets_path=secrets_file,
    )
    assert cfg.config_path == memory_dir / "config.toml"
    assert cfg.gmail_interval_seconds == 90
    assert cfg.dry_run is True
    assert cfg.allowlisted_emails == [
        "memoryowner@example.com",
        "env1@example.com",
        "secret-owner@example.org",
    ]
    assert cfg.db_path == state_dir / "inbox.db"
    assert cfg.cursors_path == state_dir / "cursors.json"
    assert cfg.heartbeats_dir == state_dir / "heartbeats"
    assert cfg.kill_switch_path == state_dir / "kill_switch.flag"
    assert cfg.prompts_dir == REPO_ROOT / "prompts" / "brief"


def test_config_load_handles_missing_secrets_and_explicit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRIEF_OWNER_EMAILS", raising=False)
    explicit = tmp_path / "custom.toml"
    explicit.write_text(
        '[limits]\nmax_events_per_hour = 10\n',
        encoding="utf-8",
    )
    cfg = BriefConfig.load(
        config_path=explicit,
        state_dir=tmp_path / "state",
        memory_dir=tmp_path / "memory",
        secrets_path=tmp_path / "does_not_exist.json",
    )
    assert cfg.max_events_per_hour == 10
    assert cfg.allowlisted_emails == []

    with pytest.raises(FileNotFoundError):
        BriefConfig.load(config_path=tmp_path / "missing.toml")


def test_kill_switch_heartbeat_and_memory_scaffold(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    assert is_kill_switch_active(state_dir) is False

    flag_path = set_kill_switch(state_dir, True, reason="manual pause")
    assert flag_path == state_dir / "kill_switch.flag"
    assert is_kill_switch_active(state_dir) is True
    assert "manual pause" in flag_path.read_text(encoding="utf-8")

    set_kill_switch(state_dir, False)
    assert is_kill_switch_active(state_dir) is False

    hb_path = touch_heartbeat(state_dir / "heartbeats", "router", now=1700000123.5)
    assert hb_path == state_dir / "heartbeats" / "router.hb"
    assert float(hb_path.read_text(encoding="utf-8").strip()) == pytest.approx(1700000123.5)

    mem_dir = tmp_path / "memory"
    ensure_memory_scaffold(mem_dir)
    assert (mem_dir / "intake").is_dir()
    assert (mem_dir / "task").is_dir()
    assert (mem_dir / "preferences.md").is_file()
    assert (mem_dir / "people.md").is_file()

    # Idempotent: does not overwrite customized preferences.md
    (mem_dir / "preferences.md").write_text("custom prefs", encoding="utf-8")
    ensure_memory_scaffold(mem_dir)
    assert (mem_dir / "preferences.md").read_text(encoding="utf-8") == "custom prefs"


def test_queue_wal_mode_and_server_side_trust(tmp_path: Path) -> None:
    db_path = tmp_path / "brief" / "inbox.db"
    q = BriefQueue(db_path, allowlisted_senders=["Owner@Example.COM"])
    q.init_db()

    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(mode).lower() == "wal"

    ev_owner = q.enqueue_event(
        source="gmail",
        key="thread-1",
        target_tag="intake/gmail/thread-1",
        external_id="msg-1",
        sender="Display Name <owner@example.com>",
        body="Hello",
        received_at=100.0,
    )
    ev_ext = q.enqueue_event(
        source="gmail",
        key="thread-2",
        target_tag="intake/gmail/thread-2",
        external_id="msg-2",
        sender="School Office <school-office@example.org>",
        body="Newsletter",
        received_at=101.0,
    )
    ev_timer = q.enqueue_event(
        source="timer",
        key="morning-brief",
        target_tag="timer/morning-brief",
        external_id="morning-2026-11-23",
        sender="",
        body="{}",
        received_at=102.0,
    )
    ev_dup = q.enqueue_event(
        source="gmail",
        key="thread-1",
        target_tag="intake/gmail/thread-1",
        external_id="msg-1",
        sender="owner@example.com",
        body="Duplicate",
        received_at=103.0,
    )

    assert ev_owner is not None
    assert ev_ext is not None
    assert ev_timer is not None
    assert ev_dup is None

    c1 = q.claim_next_event(now=200.0)
    c2 = q.claim_next_event(now=200.0)
    c3 = q.claim_next_event(now=200.0)
    c4 = q.claim_next_event(now=200.0)

    assert c1 is not None and c1["id"] == ev_owner and c1["trusted"] is True
    assert c2 is not None and c2["id"] == ev_ext and c2["trusted"] is False
    assert c3 is not None and c3["id"] == ev_timer and c3["trusted"] is True
    assert c4 is None


def test_queue_claim_filtering_retries_and_stuck_recovery(tmp_path: Path) -> None:
    db_path = tmp_path / "inbox.db"
    q = BriefQueue(db_path, allowlisted_senders=["owner@example.com"])
    q.init_db()

    id_gmail = q.enqueue_event(
        source="gmail",
        key="t1",
        target_tag="intake/gmail/t1",
        external_id="g1",
        sender="someone@example.com",
        body="mail",
        received_at=10.0,
    )
    id_discord = q.enqueue_event(
        source="discord",
        key="brief",
        target_tag="intake/discord/brief",
        external_id="d1",
        sender="owner@example.com",
        body="hi",
        received_at=20.0,
    )
    assert id_gmail is not None and id_discord is not None

    # Excluding gmail lets discord be claimed even though gmail arrived earlier
    claimed_discord = q.claim_next_event(now=30.0, exclude_sources=["gmail"])
    assert claimed_discord is not None
    assert claimed_discord["id"] == id_discord
    assert q.mark_done(id_discord, now=35.0) is True
    assert q.count_done_since(30.0) == 1
    assert q.count_done_since(36.0) == 0

    # Retry then terminal failure
    c = q.claim_next_event(now=40.0)
    assert c is not None and c["id"] == id_gmail and c["attempts"] == 1
    assert (
        q.mark_failed_or_retry(id_gmail, "transient", max_retries=2, retry_delay_seconds=10.0, now=41.0)
        == "pending"
    )
    # Not ready before next_retry_at=51.0
    assert q.claim_next_event(now=50.0) is None
    c_retry = q.claim_next_event(now=51.0)
    assert c_retry is not None and c_retry["attempts"] == 2
    assert q.mark_failed_or_retry(id_gmail, "fatal", max_retries=2, now=52.0) == "failed"

    failed = q.list_failed_events()
    assert len(failed) == 1
    assert failed[0]["id"] == id_gmail
    assert failed[0]["error"] == "fatal"

    # Stuck delivery recovery: respects max_retries
    id_stuck_requeue = q.enqueue_event(
        source="calendar",
        key="ev1",
        target_tag="intake/calendar/ev1",
        external_id="c1",
        sender="owner@example.com",
        body="meeting",
        received_at=60.0,
    )
    id_stuck_fail = q.enqueue_event(
        source="calendar",
        key="ev2",
        target_tag="intake/calendar/ev2",
        external_id="c2",
        sender="owner@example.com",
        body="meeting2",
        received_at=61.0,
    )
    assert id_stuck_requeue is not None and id_stuck_fail is not None

    q.claim_next_event(now=100.0)  # ev1, attempts=1
    q.claim_next_event(now=100.0)  # ev2, attempts=1
    # Simulate ev2 failing once already so attempts becomes 2 when claimed again
    q.mark_failed_or_retry(id_stuck_fail, "err1", max_retries=2, retry_delay_seconds=0.0, now=101.0)
    q.claim_next_event(now=101.0)  # ev2, attempts=2

    recovered = q.recover_stuck_deliveries(stuck_seconds=300.0, max_retries=2, now=500.0)
    assert recovered == 2

    # ev1 was requeued and can now be claimed; ev2 became failed
    reclaimed = q.claim_next_event(now=501.0)
    assert reclaimed is not None and reclaimed["id"] == id_stuck_requeue
    assert q.mark_dropped(id_stuck_requeue, "no-op", now=502.0) is True

    stats = q.get_queue_stats()
    assert stats["done"] == 1
    assert stats["dropped"] == 1
    assert stats["failed"] == 2
    assert stats["pending"] == 0
    assert stats["delivering"] == 0
    assert stats["total"] == 4


def test_session_crud_and_list_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "inbox.db"
    q = BriefQueue(db_path)
    q.init_db()

    assert q.get_session("intake/gmail/t1") is None

    s1 = q.upsert_session("intake/gmail/t1", "conv-1", increment_turns=1, now=100.0)
    assert s1["conversation_id"] == "conv-1"
    assert s1["turn_count"] == 1

    s1_next = q.upsert_session("intake/gmail/t1", increment_turns=2, now=110.0)
    assert s1_next["conversation_id"] == "conv-1"
    assert s1_next["turn_count"] == 3

    q.upsert_session("intake/discord/brief", "conv-2", turn_count=5, now=120.0)
    sessions = list_sessions(db_path)
    assert [s["tag"] for s in sessions] == ["intake/discord/brief", "intake/gmail/t1"]

    assert q.clear_session_conversation("intake/discord/brief", reset_turn_count=True, now=130.0)
    cleared = q.get_session("intake/discord/brief")
    assert cleared is not None
    assert cleared["conversation_id"] is None
    assert cleared["turn_count"] == 0
    assert q.get_queue_stats()["sessions"] == 2
