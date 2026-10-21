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
    MEGATHREAD_FULLNAME,
    MEGATHREAD_TITLE,
    REDDIT_HANDLE_TO_DISCORD_SNOWFLAKE,
    RESIDENT_AGENTS_CHANNEL_ID,
    build_upvote_targets_for_reply,
    classify_item_needs_approval,
    detect_desired_flair,
    format_daily_tip_prompt,
    format_notification_card,
    format_peer_side_project_card,
    get_daily_tip_assignee,
    load_state,
    run_resident_agents_daily_tip,
    run_resident_agents_watcher,
    save_state,
)


def test_reddit_handle_map_and_peer_card() -> None:
    assert RESIDENT_AGENTS_CHANNEL_ID == 1562544598423965856
    assert REDDIT_HANDLE_TO_DISCORD_SNOWFLAKE["amosstationbot"] == "1468012353206354197"
    assert REDDIT_HANDLE_TO_DISCORD_SNOWFLAKE["xvx016_aerial"] == "1542035925603713086"
    assert build_upvote_targets_for_reply("t1_xyz", "t3_1xbsb9i") == ["t1_xyz", "t3_1xbsb9i"]
    assert build_upvote_targets_for_reply("t3_1xbsb9i", "t3_1xbsb9i") == ["t3_1xbsb9i"]
    # Same-WAN vote-manipulation guard: never upvote prime416 or t3_1xbzkxr from u/RA_Zero
    assert build_upvote_targets_for_reply("t3_1xbzkxr") == []
    assert build_upvote_targets_for_reply("t1_abc", "t3_1xbzkxr", target_author="prime416") == []
    card = format_peer_side_project_card(
        [
            {
                "fullname": "t1_xyz",
                "kind": "t1",
                "author": "SomeExternalUser",
                "title": "",
                "link_title": "[Rig Breakdown] Amos",
                "link_id": "t3_1xbsb9i",
                "link_author": "AmosStationBot",
                "body": "How do you manage session handoffs?",
                "url": "https://www.reddit.com/r/ResidentAgents/comments/1xbsb9i/rig/xyz/",
            }
        ],
        thread_reply_counts={"t3_1xbsb9i": {"amosstationbot": 1}},
    )
    assert "<@1468012353206354197>" in card
    assert "1/2 replies in thread" in card
    assert "remember to upvote `t1_xyz` when replying" in card


def test_daily_tip_rotation_and_dispatcher(tmp_path: Path) -> None:
    fake_state_file = tmp_path / "state.json"
    s1 = get_daily_tip_assignee("2026-10-21")
    s2 = get_daily_tip_assignee("2026-10-22")
    s3 = get_daily_tip_assignee("2026-10-23")
    assert {s1["agent"], s2["agent"], s3["agent"]} == {"Amos", "Aerial", "Zero"}

    prompt = format_daily_tip_prompt(s1, "2026-10-21")
    assert MEGATHREAD_TITLE in prompt
    assert MEGATHREAD_FULLNAME in prompt
    assert f"<@{s1['snowflake']}>" in prompt

    with patch("tools.resident_agents_watcher.STATE_FILE", fake_state_file), patch(
        "tools.resident_agents_watcher.queue_outbox_message"
    ) as mock_enq:
        ok, msg, extra = run_resident_agents_daily_tip(test=False, quiet=True, post_discord=True, target_date="2026-10-21")
        assert ok is True
        assert msg == ""
        assert mock_enq.call_count == 1
        assert mock_enq.call_args.kwargs["channel"] == str(RESIDENT_AGENTS_CHANNEL_ID)

        # Duplicate call on same date should skip
        ok2, msg2, extra2 = run_resident_agents_daily_tip(test=False, quiet=True, post_discord=True, target_date="2026-10-21")
        assert ok2 is True
        assert extra2.get("skipped_duplicate") is True
        assert mock_enq.call_count == 1


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
            assert mock_enq.call_count == 1
            channels = [c.kwargs["channel"] for c in mock_enq.call_args_list]
            assert channels == [str(RESIDENT_AGENTS_CHANNEL_ID)]
