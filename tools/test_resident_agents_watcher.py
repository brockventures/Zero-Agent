#!/usr/bin/env python3
"""Unit tests for tools/resident_agents_watcher.py."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import patch

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.resident_agents_watcher import (
    classify_item_needs_approval,
    detect_desired_flair,
    format_notification_card,
    load_state,
    run_resident_agents_watcher,
    save_state,
)


def test_detect_desired_flair() -> None:
    assert detect_desired_flair("[Rig Breakdown] Amos: one persona, four sessions") == "[Rig Breakdown]"
    assert detect_desired_flair("[Projects] Gotchi Companion") == "[Projects]"
    assert detect_desired_flair("[Post-Mortem] Silent SQLite Lock") == "[Post-Mortem]"
    assert detect_desired_flair("[Discussion] How does your agent's Memory work?") == "[Discussion]"
    assert detect_desired_flair("General question without prefix") is None


def test_classify_item_needs_approval() -> None:
    assert classify_item_needs_approval({"approved": True, "removed": False, "spam": False}) is False
    assert classify_item_needs_approval({"approved": False, "removed_by_category": "reddit", "banned_by": True}) is True
    assert classify_item_needs_approval({"approved": False, "spam": True}) is True
    assert classify_item_needs_approval({"approved": False, "removed": False, "spam": False}) is False


def test_format_notification_card_single_and_multi() -> None:
    single = format_notification_card(
        [
            {
                "fullname": "t3_1xbsb9i",
                "kind": "t3",
                "author": "AmosStationBot",
                "title": "[Rig Breakdown] Amos: one persona, four sessions, two boxes",
                "body": "Runtime. Each agent is a Claude Code session running in a tmux PTY.",
                "url": "https://www.reddit.com/r/ResidentAgents/comments/1xbsb9i/rig_breakdown_amos/",
                "flair": "[Rig Breakdown]",
                "auto_approved": True,
            }
        ]
    )
    assert "AmosStationBot" in single
    assert "*(auto-approved from spam filter)*" in single
    assert "<https://www.reddit.com/r/ResidentAgents/comments/1xbsb9i/rig_breakdown_amos/>" in single

    multi = format_notification_card(
        [
            {
                "fullname": "t3_1xbsb9i",
                "kind": "t3",
                "author": "AmosStationBot",
                "title": "[Rig Breakdown] Amos",
                "body": "Post body",
                "url": "https://www.reddit.com/r/ResidentAgents/comments/1xbsb9i/",
                "flair": "[Rig Breakdown]",
                "auto_approved": True,
            },
            {
                "fullname": "t1_abc123",
                "kind": "t1",
                "author": "XVX016_Aerial",
                "title": "",
                "link_title": "How does your agent's Memory work?",
                "body": "We run PostgreSQL 18 + pgvector.",
                "url": "https://www.reddit.com/r/ResidentAgents/comments/1xbrm0p/how_does/abc123/",
                "flair": None,
                "auto_approved": False,
            },
        ]
    )
    assert "2 New Community Items" in multi
    assert "AmosStationBot" in multi
    assert "XVX016_Aerial" in multi


def test_state_persistence_and_runner(tmp_path: Path) -> None:
    fake_state_file = tmp_path / "state.json"
    with patch("tools.resident_agents_watcher.STATE_FILE", fake_state_file):
        st = load_state()
        assert st["seen_ids"] == []
        st["seen_ids"] = ["t3_1", "t1_2"]
        save_state(st)
        reloaded = load_state()
        assert reloaded["seen_ids"] == ["t3_1", "t1_2"]

        with patch(
            "tools.resident_agents_watcher.poll_and_remediate_via_pixel",
            return_value={
                "ok": True,
                "skipped_printer": False,
                "new_items": [
                    {
                        "fullname": "t3_1xbsb9i",
                        "kind": "t3",
                        "author": "AmosStationBot",
                        "title": "[Rig Breakdown] Amos",
                        "body": "Amos breakdown",
                        "url": "https://www.reddit.com/r/ResidentAgents/comments/1xbsb9i/",
                        "flair": "[Rig Breakdown]",
                        "auto_approved": True,
                    }
                ],
                "approved_items": ["t3_1xbsb9i"],
            },
        ), patch("tools.resident_agents_watcher.queue_outbox_message") as mock_enq:
            ok, msg, extra = run_resident_agents_watcher(test=False, quiet=True, post_discord=True)
            assert ok is True
            assert msg == ""
            assert mock_enq.called
