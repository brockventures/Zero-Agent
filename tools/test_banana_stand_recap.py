#!/usr/bin/env python3
"""
test_banana_stand_recap.py - Unit Tests for #the-banana-stand Recap

Tests cover:
- Crab Cavern silenced state (and force bypass)
- Quiet window / non-substantive discussions (silence invariant)
- Dry-run execution (no outbox dispatch)
- Normal dispatch to #lounge via outbox
- #lounge formatting discipline (no banana prefixes, no handoff envelopes, URLs wrapped in < >)
- Window timestamp filtering
- Deterministic heuristic fallback
- CLI arguments
"""

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
elif sys.path[0] != _REPO_ROOT:
    sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from tools.banana_stand_recap import (
    BANANA_STAND_CHANNEL_ID,
    LOUNGE_CHANNEL_ID,
    clean_message_content,
    clean_summary_formatting,
    fetch_recent_banana_stand_messages,
    is_substantive_discussion,
    main,
    parse_message_timestamp,
    run_recap,
    summarize_discussion,
    wrap_urls,
)


@pytest.fixture(autouse=True)
def prevent_real_discord_network(monkeypatch):
    """Ensure tests never make live network calls by default."""
    monkeypatch.setattr("tools.banana_stand_recap.get_discord_token", lambda: "")
    monkeypatch.setattr("tools.banana_stand_recap.get_gemini_api_key", lambda: "")


@pytest.fixture
def mock_substantive_messages():
    now = time.time()
    return [
        {
            "id": "1573846084004872201",
            "channel_id": BANANA_STAND_CHANNEL_ID,
            "author": "Amos",
            "is_bot": True,
            "content": "🍌 Initiating discussion on AI router canary deployments and secret redaction policies.",
            "timestamp": now - 600,
        },
        {
            "id": "1573846084004872202",
            "channel_id": BANANA_STAND_CHANNEL_ID,
            "author": "Zero",
            "is_bot": True,
            "content": "Agreed, ZDR flags are unenforceable at the router layer. Host-level secret redaction is mandatory before dispatch.",
            "timestamp": now - 300,
        },
        {
            "id": "1573846084004872203",
            "channel_id": BANANA_STAND_CHANNEL_ID,
            "author": "Aerial",
            "is_bot": True,
            "content": "Consensus reached. We will keep redaction host-side and close out the canary audit with no pending PRs.\n\n```handoff\n{\"v\":1.1,\"kind\":\"resolution\",\"floor\":\"closed\"}\n```",
            "timestamp": now - 120,
        },
    ]


# -------------------------------------------------------------------------
# 1. Silenced State Tests
# -------------------------------------------------------------------------


def test_silenced_state(monkeypatch):
    """When Crab Cavern is silenced, recap must skip dispatch and report silenced status."""
    monkeypatch.setattr(
        "tools.bridge_state.is_crab_cavern_silenced",
        lambda *args, **kwargs: (True, "Quiet period active"),
    )
    mock_outbox = MagicMock()
    monkeypatch.setattr("tools.outbox.queue_outbox_message", mock_outbox)

    res = run_recap(dispatch=True)

    assert res["status"] == "silenced"
    assert "Quiet period active" in res["message"]
    mock_outbox.assert_not_called()


def test_silenced_state_bypassed_with_force(monkeypatch, mock_substantive_messages):
    """Force flag should bypass Crab Cavern silence state."""
    monkeypatch.setattr(
        "tools.bridge_state.is_crab_cavern_silenced",
        lambda *args, **kwargs: (True, "Quiet period active"),
    )
    monkeypatch.setattr(
        "tools.banana_stand_recap.fetch_recent_banana_stand_messages",
        lambda *args, **kwargs: mock_substantive_messages,
    )
    monkeypatch.setattr(
        "tools.banana_stand_recap.summarize_discussion",
        lambda msgs: "Team resolved canary router redaction. Consensus reached. No pending tasks.",
    )
    mock_outbox = MagicMock(return_value={"status": "queued", "id": "test-123"})
    monkeypatch.setattr("tools.outbox.queue_outbox_message", mock_outbox)

    res = run_recap(dispatch=True, force=True)

    assert res["status"] == "dispatched"
    mock_outbox.assert_called_once()


# -------------------------------------------------------------------------
# 2. Quiet Window / Non-Substantive Discussion Tests
# -------------------------------------------------------------------------


def test_quiet_window_empty_messages(monkeypatch):
    """Zero messages in window must produce silent status with no dispatch."""
    monkeypatch.setattr(
        "tools.bridge_state.is_crab_cavern_silenced",
        lambda *args, **kwargs: (False, ""),
    )
    monkeypatch.setattr(
        "tools.channel_history.get_recent_messages",
        lambda *args, **kwargs: [],
    )
    mock_outbox = MagicMock()
    monkeypatch.setattr("tools.outbox.queue_outbox_message", mock_outbox)

    res = run_recap(dispatch=True)

    assert res["status"] == "silent"
    assert "No substantive discussion" in res["message"]
    mock_outbox.assert_not_called()


def test_quiet_window_trivial_heartbeats_only(monkeypatch):
    """Trivial heartbeat / status / empty messages must not trigger a recap."""
    now = time.time()
    trivial_msgs = [
        {"author": "Bot1", "content": "ping", "timestamp": now - 300},
        {"author": "Bot2", "content": "pong", "timestamp": now - 200},
        {
            "author": "Bot1",
            "content": "```handoff\n{\"v\":1.1,\"kind\":\"heartbeat\"}\n```",
            "timestamp": now - 100,
        },
    ]
    monkeypatch.setattr(
        "tools.bridge_state.is_crab_cavern_silenced",
        lambda *args, **kwargs: (False, ""),
    )
    monkeypatch.setattr(
        "tools.channel_history.get_recent_messages",
        lambda *args, **kwargs: trivial_msgs,
    )
    mock_outbox = MagicMock()
    monkeypatch.setattr("tools.outbox.queue_outbox_message", mock_outbox)

    res = run_recap(dispatch=True)

    assert res["status"] == "silent"
    mock_outbox.assert_not_called()


# -------------------------------------------------------------------------
# 3. Dry-Run Tests
# -------------------------------------------------------------------------


def test_dry_run_generates_recap_without_dispatch(monkeypatch, mock_substantive_messages):
    """--dry-run generates the recap text without calling queue_outbox_message."""
    monkeypatch.setattr(
        "tools.bridge_state.is_crab_cavern_silenced",
        lambda *args, **kwargs: (False, ""),
    )
    monkeypatch.setattr(
        "tools.banana_stand_recap.fetch_recent_banana_stand_messages",
        lambda *args, **kwargs: mock_substantive_messages,
    )
    mock_summary = "Discussion covered canary secret redaction. The robots agreed to enforce redaction host-side. The topic was resolved cleanly."
    monkeypatch.setattr(
        "tools.banana_stand_recap.summarize_discussion",
        lambda msgs: mock_summary,
    )
    mock_outbox = MagicMock()
    monkeypatch.setattr("tools.outbox.queue_outbox_message", mock_outbox)

    res = run_recap(dry_run=True, dispatch=False)

    assert res["status"] == "dry_run"
    assert res["recap"] == mock_summary
    assert res["message_count"] == 3
    mock_outbox.assert_not_called()


# -------------------------------------------------------------------------
# 4. Normal Dispatch Tests
# -------------------------------------------------------------------------


def test_normal_dispatch(monkeypatch, mock_substantive_messages):
    """Normal dispatch summarizes discussion and enqueues to #lounge via outbox."""
    monkeypatch.setattr(
        "tools.bridge_state.is_crab_cavern_silenced",
        lambda *args, **kwargs: (False, ""),
    )
    monkeypatch.setattr(
        "tools.banana_stand_recap.fetch_recent_banana_stand_messages",
        lambda *args, **kwargs: mock_substantive_messages,
    )
    mock_summary = "The team evaluated AI router canary policies and host secret redaction. Consensus concluded that host-level redaction is mandatory with zero pending PRs. The topic was parked with all systems green."
    monkeypatch.setattr(
        "tools.banana_stand_recap.summarize_discussion",
        lambda msgs: mock_summary,
    )
    mock_outbox = MagicMock(return_value={"status": "queued", "id": "outbox-test-999"})
    monkeypatch.setattr("tools.outbox.queue_outbox_message", mock_outbox)

    res = run_recap(dispatch=True)

    assert res["status"] == "dispatched"
    assert res["recap"] == mock_summary
    assert res["outbox_result"] == {"status": "queued", "id": "outbox-test-999"}
    mock_outbox.assert_called_once_with(
        channel=LOUNGE_CHANNEL_ID,
        content=mock_summary,
        source_turn="banana_stand_recap",
        dedupe_window=3600,
        force=True,
    )


# -------------------------------------------------------------------------
# 5. Formatting & Cleanliness Discipline Tests
# -------------------------------------------------------------------------


def test_formatting_discipline_urls_banana_and_handoff():
    """Verify #lounge rules: no banana emoji, no handoffs, URLs wrapped in < >."""
    raw = (
        "🍌 Banana Stand Recap: The robots discussed canary deployments at https://github.com/brockventures/agora.\n"
        "They also checked <https://example.com/spec> and agreed on architecture.\n"
        "```handoff\n{\"v\":1.1,\"kind\":\"resolution\"}\n```\n"
        "No further action is required."
    )
    cleaned = clean_summary_formatting(raw)

    assert "🍌" not in cleaned
    assert "Banana Stand Recap:" not in cleaned
    assert "```" not in cleaned
    assert "handoff" not in cleaned
    # URL was wrapped
    assert "<https://github.com/brockventures/agora>" in cleaned
    # Already-wrapped URL was not double-wrapped
    assert "<<https://example.com/spec>>" not in cleaned
    assert "<https://example.com/spec>" in cleaned


def test_clean_message_content():
    """Verify clean_message_content strips handoff blocks and banana emojis."""
    raw = "🍌 Working on task.\n```handoff\n{\"kind\":\"status\"}\n```\nAll clear."
    clean = clean_message_content(raw)
    assert clean == "Working on task.\n\nAll clear."


def test_is_substantive_discussion_thresholds():
    """Verify substantive discussion requires non-trivial text length."""
    too_short = [{"content": "🍌 ok"}]
    sub, msgs = is_substantive_discussion(too_short)
    assert not sub
    assert msgs == []

    substantive = [
        {"content": "🍌 The database migration plan was reviewed and approved with zero downtime."}
    ]
    sub, msgs = is_substantive_discussion(substantive)
    assert sub
    assert len(msgs) == 1


# -------------------------------------------------------------------------
# 6. Timestamp Parsing & Window Filtering Tests
# -------------------------------------------------------------------------


def test_parse_message_timestamp():
    """Test parsing across ISO 8601, UTC string, numeric timestamp, and snowflake ID."""
    # Numeric
    assert parse_message_timestamp({"timestamp": 1795300000.5}) == 1795300000.5

    # UTC string
    utc_str = {"timestamp": "2026-11-21 15:30:00 UTC"}
    ts = parse_message_timestamp(utc_str)
    assert ts is not None and ts > 0

    # ISO string
    iso_str = {"timestamp": "2026-11-21T15:30:00.000000+00:00"}
    assert parse_message_timestamp(iso_str) == ts

    # Snowflake ID
    # Snowflake: 1573846084004872201 -> ((1573846084004872201 >> 22) + 1420070400000) / 1000.0
    snowflake_msg = {"id": "1573846084004872201"}
    sf_ts = parse_message_timestamp(snowflake_msg)
    assert sf_ts is not None and sf_ts > 1700000000


def test_fetch_recent_banana_stand_messages_filtering(monkeypatch):
    """Messages outside the window must be excluded."""
    now = 1795300000.0
    mock_history = [
        # 60 minutes old (outside 45m window)
        {"id": "1", "content": "Old message from 1 hour ago", "timestamp": now - 3600},
        # 30 minutes old (inside 45m window)
        {"id": "2", "content": "Recent discussion within 30 minutes", "timestamp": now - 1800},
        # 5 minutes old (inside 45m window)
        {"id": "3", "content": "Very recent resolution message", "timestamp": now - 300},
    ]

    monkeypatch.setattr(
        "tools.channel_history.get_recent_messages",
        lambda *args, **kwargs: mock_history,
    )

    recent = fetch_recent_banana_stand_messages(window_minutes=45, now_ts=now)
    assert len(recent) == 2
    assert recent[0]["id"] == "2"
    assert recent[1]["id"] == "3"


# -------------------------------------------------------------------------
# 7. Fallback Summarizer Test
# -------------------------------------------------------------------------


def test_heuristic_summary_fallback(mock_substantive_messages):
    """Fallback summarizer must produce clean 2-3 sentence summary without LLM."""
    summary = summarize_discussion(mock_substantive_messages)
    assert summary
    assert "🍌" not in summary
    assert "```" not in summary
    # Sentences count: roughly 2-3
    sentences = [s for s in summary.split(".") if s.strip()]
    assert 2 <= len(sentences) <= 4


# -------------------------------------------------------------------------
# 8. CLI Entrypoint Tests
# -------------------------------------------------------------------------


def test_main_cli_dry_run(monkeypatch, capsys):
    """Test CLI --dry-run invocation."""
    monkeypatch.setattr(
        "tools.bridge_state.is_crab_cavern_silenced",
        lambda *args, **kwargs: (False, ""),
    )
    monkeypatch.setattr(
        "tools.banana_stand_recap.fetch_recent_banana_stand_messages",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr("sys.argv", ["banana_stand_recap.py", "--dry-run"])

    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["status"] == "silent"


def test_main_cli_dispatch(monkeypatch, capsys, mock_substantive_messages):
    """Test CLI --dispatch invocation."""
    monkeypatch.setattr(
        "tools.bridge_state.is_crab_cavern_silenced",
        lambda *args, **kwargs: (False, ""),
    )
    monkeypatch.setattr(
        "tools.banana_stand_recap.fetch_recent_banana_stand_messages",
        lambda *args, **kwargs: mock_substantive_messages,
    )
    monkeypatch.setattr(
        "tools.banana_stand_recap.summarize_discussion",
        lambda msgs: "Team resolved canary router redaction. Consensus was reached. Topic is parked.",
    )
    mock_outbox = MagicMock(return_value={"status": "queued", "id": "cli-123"})
    monkeypatch.setattr("tools.outbox.queue_outbox_message", mock_outbox)
    monkeypatch.setattr("sys.argv", ["banana_stand_recap.py", "--dispatch"])

    main()
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["status"] == "dispatched"
    assert data["outbox_result"]["id"] == "cli-123"
