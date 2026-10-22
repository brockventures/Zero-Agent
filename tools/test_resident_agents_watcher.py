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
    SEEDING_MAX_POSTS_PER_DAY,
    SEEDING_MIN_POST_INTERVAL_SEC,
    SEEDING_MIN_SCORE,
    SEEDING_SCAN_INTERVAL_SEC,
    build_upvote_targets_for_reply,
    check_and_assign_daily_links,
    classify_item_needs_approval,
    detect_desired_flair,
    evaluate_seeding_candidates_llm,
    find_unassigned_daily_links_message,
    format_daily_link_assignment_prompt,
    format_daily_tip_prompt,
    format_notification_card,
    format_peer_side_project_card,
    format_seeding_card,
    generate_seeding_comment_draft,
    get_daily_link_assignee,
    get_daily_tip_assignee,
    is_eligible_seeding_metadata,
    load_state,
    parse_candidate_links,
    run_resident_agents_daily_links,
    run_resident_agents_daily_tip,
    run_resident_agents_watcher,
    save_state,
    score_seeding_thread,
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


def test_daily_link_parsing_and_assignment(tmp_path: Path) -> None:
    fake_state_file = tmp_path / "state.json"

    # Verify daily link assignee is staggered by +1 from daily tip assignee so one bot never gets both on the same day
    for d in ("2026-10-21", "2026-10-22", "2026-10-23"):
        tip_slot = get_daily_tip_assignee(d)
        link_slot = get_daily_link_assignee(d)
        assert tip_slot["agent"] != link_slot["agent"]

    amos_text = (
        "## r/ResidentAgents link candidates · 2026-10-22\n"
        "- **L1** · [SQLite WAL Concurrency in Multi-Agent Daemons](<https://example.com/sqlite-wal>) · Hacker News\n"
        "  -# Deep dive on busy_timeout and lock contention.\n"
        "- `L2`: https://arxiv.org/abs/2610.12345 — Episodic memory pruning benchmarks.\n"
        "L3 - [Nomad Task Driver Isolation](<https://example.com/nomad>) — cgroup v2 memory caps.\n"
    )
    parsed = parse_candidate_links(amos_text)
    assert len(parsed) == 3
    assert [c["label"] for c in parsed] == ["L1", "L2", "L3"]
    assert "Deep dive on busy_timeout" in parsed[0]["line"]

    # Meta-messages without URLs (like Amos explaining the cron setup) must be ignored
    meta_msg = {
        "id": 1001,
        "author": "Amos",
        "is_bot": True,
        "content": "- **What:** up to 6 agent-related links from my news archive, each labelled `L1` to `L6`.",
        "timestamp": "2026-10-21 22:39:13 UTC",
    }
    real_msg = {
        "id": 1002,
        "author": "Amos",
        "is_bot": True,
        "content": amos_text,
        "timestamp": "2026-10-22 17:30:05 UTC",
    }
    found = find_unassigned_daily_links_message([meta_msg, real_msg], assigned_msg_ids=[])
    assert found is not None
    assert found["msg_id"] == "1002"
    assert len(found["candidates"]) == 3

    with patch("tools.resident_agents_watcher.STATE_FILE", fake_state_file), patch(
        "tools.resident_agents_watcher.get_recent_messages",
        return_value=[meta_msg, real_msg],
    ), patch("tools.resident_agents_watcher.queue_outbox_message") as mock_enq:
        ok, msg, extra = run_resident_agents_daily_links(
            test=False,
            quiet=True,
            post_discord=True,
            target_date="2026-10-22",
        )
        assert ok is True
        assert extra.get("assigned") is True
        assert mock_enq.call_count == 1
        dispatched_card = mock_enq.call_args.kwargs["content"]
        expected_slot = get_daily_link_assignee("2026-10-22")
        assert f"<@{expected_slot['snowflake']}>" in dispatched_card
        assert "`L1`" in dispatched_card and "`L3`" in dispatched_card

        # Second run on same date / message ID must not re-assign
        ok2, msg2, extra2 = run_resident_agents_daily_links(
            test=False,
            quiet=True,
            post_discord=True,
            target_date="2026-10-22",
        )
        assert ok2 is True
        assert extra2.get("assigned") is False
        assert mock_enq.call_count == 1


def test_seeding_thread_scoring_and_draft_generation() -> None:
    now_ts = 1792630000.0
    high_signal_post = {
        "name": "t3_seed1",
        "fullname": "t3_seed1",
        "subreddit": "LocalLLaMA",
        "author": "some_builder",
        "title": "How are you handling long-term memory and cron schedules for an always-on home server agent?",
        "selftext": "I have a persistent agent running in Docker 24/7 with Claude Code, but context compaction keeps dropping state.",
        "created_utc": now_ts - 3600,
        "num_comments": 8,
        "stickied": False,
        "locked": False,
        "over_18": False,
        "url": "https://www.reddit.com/r/LocalLLaMA/comments/seed1/how_are_you/",
    }
    assert SEEDING_MAX_POSTS_PER_DAY == 1
    assert SEEDING_MIN_POST_INTERVAL_SEC >= 43200
    assert SEEDING_SCAN_INTERVAL_SEC >= 14400

    # Pure structural metadata checks (no regex/keyword heuristics)
    ok_meta, _ = is_eligible_seeding_metadata(high_signal_post, now_utc=now_ts)
    assert ok_meta is True
    ok_locked, _ = is_eligible_seeding_metadata({**high_signal_post, "locked": True}, now_utc=now_ts)
    assert ok_locked is False

    workplace_post = {
        "name": "t3_work1",
        "fullname": "t3_work1",
        "subreddit": "ClaudeAI",
        "author": "marmelab",
        "title": "When non-dev colleagues build internal apps with Claude Code, where do you host them?",
        "selftext": "Some of my colleagues aren't developers, but they build internal apps with Claude Code on their laptops and want them self-hosted at work.",
        "created_utc": now_ts - 1800,
        "num_comments": 5,
        "url": "https://www.reddit.com/r/ClaudeAI/comments/work1/when_nondev/",
    }

    # Verify Flash LLM semantic evaluator selects genuine resident-agent thread and drafts grounded reply
    mock_approve_json = {
        "selected_index": 1,
        "score": 94.0,
        "rationale": "Directly discusses long-term memory compaction and cron scheduling for a 24/7 home server agent.",
        "comment_draft": (
            "Ran into this exact wall once I moved from one-off chat tabs to running a resident agent 24/7 in Docker on my home NAS. "
            "Splitting recent verbatim turns from a local SQLite FTS5 index over markdown notes stopped the context rot—how are you deciding what gets compacted vs kept verbatim?"
        ),
    }
    with patch("tools.llm_sidecar.call_sidecar_llm_json", return_value=mock_approve_json):
        eval_res = evaluate_seeding_candidates_llm([workplace_post, high_signal_post], now_utc=now_ts)
        assert eval_res is not None
        assert eval_res["candidate"]["fullname"] == "t3_seed1"
        assert eval_res["score"] == 94.0
        assert "resident agent" in eval_res["draft"].lower()
        assert "r/residentagents" not in eval_res["draft"].lower()

        score, reasons = score_seeding_thread(high_signal_post, now_utc=now_ts)
        assert score >= SEEDING_MIN_SCORE

    # Verify Flash LLM semantic evaluator cleanly rejects off-topic workplace / non-resident threads
    mock_reject_json = {
        "selected_index": None,
        "score": 15.0,
        "rationale": "Thread is about enterprise hosting for internal colleague web apps, not persistent resident agents.",
        "comment_draft": "",
    }
    with patch("tools.llm_sidecar.call_sidecar_llm_json", return_value=mock_reject_json):
        assert evaluate_seeding_candidates_llm([workplace_post], now_utc=now_ts) is None
        work_score, _ = score_seeding_thread(workplace_post, now_utc=now_ts)
        assert work_score == 0.0
        assert generate_seeding_comment_draft(workplace_post) is None

    cand = {
        "fullname": "t3_seed1",
        "subreddit": "LocalLLaMA",
        "author": "some_builder",
        "title": high_signal_post["title"],
        "selftext": high_signal_post["selftext"],
        "url": high_signal_post["url"],
        "score": 94.0,
    }
    card = format_seeding_card(cand, mock_approve_json["comment_draft"], posted_url=cand["url"])
    assert "u/prime416" in card
    assert "r/LocalLLaMA" in card
    assert "Auto-Posted" in card


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
