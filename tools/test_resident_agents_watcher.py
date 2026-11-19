#!/usr/bin/env python3
"""Unit tests for tools/resident_agents_watcher.py."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import types
from unittest.mock import MagicMock, patch

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_TOOLS_DIR = str(Path(__file__).resolve().parent)
for _p in (_REPO_ROOT, _TOOLS_DIR, "/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.append(_p)
if sys.path[0] != _REPO_ROOT:
    sys.path.insert(0, _REPO_ROOT)

from tools.resident_agents_watcher import (
    KNOWN_PEER_ACCOUNTS,
    MEGATHREAD_FULLNAME,
    MEGATHREAD_TITLE,
    REDDIT_HANDLE_TO_DISCORD_SNOWFLAKE,
    RESIDENT_AGENTS_CHANNEL_ID,
    SEEDING_MAX_POSTS_PER_DAY,
    SEEDING_MIN_POST_INTERVAL_SEC,
    SEEDING_MIN_SCORE,
    SEEDING_SCAN_INTERVAL_SEC,
    TRANSIENT_POLL_ALERT_COOLDOWN_SECONDS,
    TRANSIENT_POLL_ALERT_MIN_SECONDS,
    TRANSIENT_POLL_ALERT_MIN_STRIKES,
    _is_transient_pixel_poll_error,
    build_upvote_targets_for_reply,
    classify_item_needs_approval,
    compute_seeding_velocity_priority,
    detect_desired_flair,
    evaluate_seeding_candidates_llm,
    extract_top_comments_from_thread_json,
    find_unassigned_daily_links_message,
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
    post_reddit_comment_human_paced,
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
    assert (
        build_upvote_targets_for_reply(
            "t1_xyz",
            "t3_ext999",
            target_author="ExternalUser1",
            parent_post_author="ExternalUser2",
        )
        == ["t1_xyz", "t3_ext999"]
    )
    # Zero cross-bot vote-ring guard: never upvote megathread or any KNOWN_PEER_ACCOUNTS
    assert build_upvote_targets_for_reply("t3_1xbzkxr") == []
    for peer in KNOWN_PEER_ACCOUNTS:
        assert build_upvote_targets_for_reply("t1_abc", "t3_ext123", target_author=peer) == []
        assert build_upvote_targets_for_reply("t1_abc", "t3_ext123", parent_post_author=peer) == []
        assert build_upvote_targets_for_reply("t1_abc", "t3_ext123", target_author=f"u/{peer.upper()}") == []

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
    assert "upvote" not in card.lower()


def test_daily_tip_rotation_and_dispatcher(tmp_path: Path) -> None:
    fake_state_file = tmp_path / "state.json"
    s1 = get_daily_tip_assignee("2026-10-21")
    s2 = get_daily_tip_assignee("2026-10-22")
    assert {s1["agent"], s2["agent"]} == {"Aerial", "Zero"}

    prompt = format_daily_tip_prompt(s1, "2026-10-21")
    assert MEGATHREAD_TITLE in prompt
    assert MEGATHREAD_FULLNAME in prompt
    assert f"<@{s1['snowflake']}>" in prompt
    assert "upvote" not in prompt.lower()
    assert "--post-comment" in prompt
    assert "--thread-url" in prompt
    assert "--thing-id" in prompt
    assert "urllib" in prompt.lower()

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
    assert SEEDING_MAX_POSTS_PER_DAY == 2
    assert SEEDING_MIN_POST_INTERVAL_SEC >= 21600
    assert SEEDING_SCAN_INTERVAL_SEC >= 14400

    # Structural metadata and velocity checks
    ok_meta, _ = is_eligible_seeding_metadata(high_signal_post, now_utc=now_ts)
    assert ok_meta is True
    ok_locked, _ = is_eligible_seeding_metadata({**high_signal_post, "locked": True}, now_utc=now_ts)
    assert ok_locked is False

    # Unseasoned brand-new post (<30m old) must be rejected so AutoMod/mods filter spam first
    brand_new_post = {
        **high_signal_post,
        "created_utc": now_ts - 300,
        "score": 1,
        "ups": 1,
        "num_comments": 1,
        "is_rising": False,
    }
    ok_new, new_reason = is_eligible_seeding_metadata(brand_new_post, now_utc=now_ts)
    assert ok_new is False
    assert "Unseasoned brand-new post" in new_reason

    # Auto-moderated / removed posts must be rejected immediately
    ok_rem, rem_reason = is_eligible_seeding_metadata(
        {**high_signal_post, "removed_by_category": "moderator"},
        now_utc=now_ts,
    )
    assert ok_rem is False
    assert "Removed/Auto-moderated" in rem_reason

    # No-traction /new thread (1 pt, 0 comments, not rising) should be filtered out
    dead_new_post = {
        **high_signal_post,
        "created_utc": now_ts - 7200,
        "score": 1,
        "ups": 1,
        "num_comments": 0,
        "is_rising": False,
    }
    ok_dead, dead_reason = is_eligible_seeding_metadata(dead_new_post, now_utc=now_ts)
    assert ok_dead is False
    assert "No traction" in dead_reason

    # Rising feed threads with real traction rank higher in velocity priority
    rising_post = {**dead_new_post, "is_rising": True, "score": 6, "num_comments": 4}
    ok_rising, _ = is_eligible_seeding_metadata(rising_post, now_utc=now_ts)
    assert ok_rising is True
    assert compute_seeding_velocity_priority(rising_post, now_utc=now_ts) > compute_seeding_velocity_priority(
        high_signal_post, now_utc=now_ts
    )

    # Top comment extraction for piggybacking on bustling threads
    fake_thread_json = [
        {"kind": "Listing", "data": {"children": []}},
        {
            "kind": "Listing",
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "name": "t1_auto",
                            "author": "AutoModerator",
                            "body": "Pinned mod note",
                            "score": 1,
                            "stickied": True,
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "name": "t1_top1",
                            "author": "keen_hacker",
                            "body": "The real killer is tool output staying in the prompt across 50 turns.",
                            "score": 42,
                            "stickied": False,
                        },
                    },
                ]
            },
        },
    ]
    top_comments = extract_top_comments_from_thread_json(fake_thread_json, limit=2)
    assert len(top_comments) == 1
    assert top_comments[0]["fullname"] == "t1_top1"
    assert top_comments[0]["author"] == "keen_hacker"

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

    # Verify Flash High LLM semantic evaluator selects genuine resident-agent thread (or top comment) and drafts grounded reply
    bustling_post = {
        **high_signal_post,
        "num_comments": 22,
        "top_comments": top_comments,
    }
    mock_approve_json = {
        "selected_index": 1,
        "reply_to_fullname": "t1_top1",
        "score": 94.0,
        "rationale": "Directly discusses long-term memory compaction and tool output bloat in persistent agent sessions.",
        "comment_draft": (
            "Tool stdout lingering in the context window was the #1 thing that blew up my resident agent sessions too. "
            "Are you stripping raw tool payloads after each turn or summarizing them inline before the next tool call?"
        ),
    }
    with patch("tools.llm_sidecar.call_sidecar_llm_json", return_value=mock_approve_json):
        eval_res = evaluate_seeding_candidates_llm([workplace_post, bustling_post], now_utc=now_ts)
        assert eval_res is not None
        assert eval_res["candidate"]["fullname"] == "t3_seed1"
        assert eval_res["reply_to_fullname"] == "t1_top1"
        assert eval_res["candidate"]["reply_target_comment"]["author"] == "keen_hacker"
        assert eval_res["score"] == 94.0
        assert "resident agent" in eval_res["draft"].lower()
        assert "r/residentagents" not in eval_res["draft"].lower()

        score, reasons = score_seeding_thread(bustling_post, now_utc=now_ts)
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


def test_is_transient_pixel_poll_error_markers() -> None:
    assert _is_transient_pixel_poll_error(subprocess.TimeoutExpired(cmd="adb", timeout=8)) is True
    assert _is_transient_pixel_poll_error(RuntimeError("Command 'adb shell input keyevent 224' timed out after 3 seconds")) is True
    assert _is_transient_pixel_poll_error(RuntimeError("Timeout 25000ms exceeded")) is True
    assert _is_transient_pixel_poll_error(RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED")) is True
    assert _is_transient_pixel_poll_error(RuntimeError("BrowserType.connect_over_cdp: ECONNREFUSED")) is True
    assert _is_transient_pixel_poll_error(RuntimeError("Reddit session cookies expired")) is False
    assert _is_transient_pixel_poll_error(ValueError("Missing modhash")) is False


def test_transient_pixel_poll_error_suppression_and_outage_alert(tmp_path: Path) -> None:
    fake_state_file = tmp_path / "state_err.json"
    fake_now = [1795000000.0]

    with patch("tools.resident_agents_watcher.STATE_FILE", fake_state_file), patch(
        "tools.resident_agents_watcher.time.time", side_effect=lambda: fake_now[0]
    ), patch(
        "tools.resident_agents_watcher.check_and_assign_daily_links", return_value=None
    ), patch(
        "tools.resident_agents_watcher.poll_and_remediate_via_pixel",
        side_effect=subprocess.TimeoutExpired(cmd="adb shell input keyevent 224", timeout=8),
    ):
        # Strikes 1..5 (including past old 3-strike cutoff): suppressed silently, logged to sidecar telemetry summary
        for expected_strike in range(1, 6):
            ok, msg, extra = run_resident_agents_watcher(test=False, quiet=True, post_discord=True)
            assert ok is True
            assert msg == ""
            assert extra.get("transient_error_suppressed") is True
            assert extra.get("is_transient") is True
            assert extra.get("consecutive_poll_errors") == expected_strike
            assert "Suppressed transient Pixel poll error" in extra.get("summary", "")
            fake_now[0] += 300.0

        # Even if >= 2 hours pass with fewer than TRANSIENT_POLL_ALERT_MIN_STRIKES (e.g. quiet hours), keep suppressing
        fake_now[0] += TRANSIENT_POLL_ALERT_MIN_SECONDS + 60.0
        ok_q, msg_q, extra_q = run_resident_agents_watcher(test=False, quiet=True, post_discord=True)
        assert ok_q is True
        assert msg_q == ""
        assert extra_q.get("transient_error_suppressed") is True

        # Advance to extended multi-hour outage threshold (TRANSIENT_POLL_ALERT_MIN_STRIKES and >= 2h)
        st = json.loads(fake_state_file.read_text(encoding="utf-8"))
        st["consecutive_poll_errors"] = TRANSIENT_POLL_ALERT_MIN_STRIKES - 1
        st["first_poll_error_ts"] = fake_now[0] - TRANSIENT_POLL_ALERT_MIN_SECONDS - 10.0
        fake_state_file.write_text(json.dumps(st), encoding="utf-8")

        ok_alert, msg_alert, extra_alert = run_resident_agents_watcher(test=False, quiet=True, post_discord=True)
        assert ok_alert is False
        assert "extended outage" in msg_alert.lower()
        assert "timed out" in msg_alert.lower()
        assert extra_alert.get("consecutive_poll_errors") == TRANSIENT_POLL_ALERT_MIN_STRIKES

        # Next 5-minute cycle during cooldown is suppressed again instead of spamming #zero-chat
        fake_now[0] += 300.0
        ok_cool, msg_cool, extra_cool = run_resident_agents_watcher(test=False, quiet=True, post_discord=True)
        assert ok_cool is True
        assert msg_cool == ""
        assert extra_cool.get("transient_error_suppressed") is True

        # After cooldown elapses, persistent multi-hour outage can alert again
        fake_now[0] += TRANSIENT_POLL_ALERT_COOLDOWN_SECONDS + 5.0
        ok_realert, msg_realert, _ = run_resident_agents_watcher(test=False, quiet=True, post_discord=True)
        assert ok_realert is False
        assert "extended outage" in msg_realert.lower()

    # Genuine non-transient failure alerts immediately without suppression
    with patch("tools.resident_agents_watcher.STATE_FILE", fake_state_file), patch(
        "tools.resident_agents_watcher.check_and_assign_daily_links", return_value=None
    ), patch(
        "tools.resident_agents_watcher.poll_and_remediate_via_pixel",
        side_effect=RuntimeError("Reddit session cookies expired"),
    ):
        ok_nt, msg_nt, extra_nt = run_resident_agents_watcher(test=False, quiet=True, post_discord=True)
        assert ok_nt is False
        assert "Reddit session cookies expired" in msg_nt
        assert extra_nt.get("is_transient") is False


def test_post_reddit_comment_human_paced(tmp_path: Path) -> None:
    # Missing cookies file returns clean error
    missing_res = post_reddit_comment_human_paced(
        thread_url="https://www.reddit.com/r/ResidentAgents/comments/1xbzkxr/tips/",
        thing_id="t3_1xbzkxr",
        text="Test comment",
        cookies_path=str(tmp_path / "does_not_exist.json"),
    )
    assert missing_res["ok"] is False
    assert "not found" in missing_res["error"].lower()

    cookies_file = tmp_path / "ra_zero_cookies.json"
    cookies_file.write_text(
        json.dumps(
            [
                {
                    "name": "reddit_session",
                    "value": "dummy_token",
                    "domain": ".reddit.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "no_restriction",
                }
            ]
        ),
        encoding="utf-8",
    )

    mock_page = MagicMock()
    eval_calls = []

    def fake_evaluate(script, arg=None):
        eval_calls.append((script, arg))
        if "scrollBy" in script:
            return None
        return {
            "json": {
                "errors": [],
                "data": {
                    "things": [
                        {
                            "data": {
                                "name": "t1_new999",
                                "permalink": "/r/ResidentAgents/comments/1xbzkxr/tips/new999/",
                            }
                        }
                    ]
                },
            }
        }

    mock_page.evaluate.side_effect = fake_evaluate
    mock_context = MagicMock()
    mock_context.new_page.return_value = mock_page
    mock_browser = MagicMock()
    mock_browser.new_context.return_value = mock_context
    mock_pw = MagicMock()
    mock_pw.chromium.launch.return_value = mock_browser
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_pw
    mock_cm.__exit__.return_value = False

    sleeps = []
    mock_pw_mod = types.ModuleType("playwright")
    mock_sync_api = types.ModuleType("playwright.sync_api")
    mock_sync_api.sync_playwright = MagicMock(return_value=mock_cm)
    mock_pw_mod.sync_api = mock_sync_api

    with patch.dict(sys.modules, {"playwright": mock_pw_mod, "playwright.sync_api": mock_sync_api}), patch(
        "tools.resident_agents_watcher.time.sleep", side_effect=lambda s: sleeps.append(s)
    ):
        res = post_reddit_comment_human_paced(
            thread_url="https://www.reddit.com/r/ResidentAgents/comments/1xbzkxr/tips/",
            thing_id="t3_1xbzkxr",
            text="Concrete pattern -> fix -> question?",
            cookies_path=str(cookies_file),
            min_dwell_sec=12.0,
            max_dwell_sec=25.0,
        )

    assert res["ok"] is True
    assert res["comment_id"] == "t1_new999"
    assert res["comment_url"] == "https://www.reddit.com/r/ResidentAgents/comments/1xbzkxr/tips/new999/"
    assert 12.0 <= res["dwell_sec"] <= 25.0

    # Verify thread opened on old.reddit.com with cookies loaded
    mock_context.add_cookies.assert_called_once()
    loaded_cookies = mock_context.add_cookies.call_args[0][0]
    assert loaded_cookies[0]["sameSite"] == "None"
    mock_page.goto.assert_called_once_with(
        "https://old.reddit.com/r/ResidentAgents/comments/1xbzkxr/tips/",
        timeout=30000,
        wait_until="domcontentloaded",
    )

    # Verify organic scroll calls and reading dwell delay (excluding initial 1-2s settle sleep)
    scroll_calls = [c for c in eval_calls if "scrollBy" in c[0]]
    submit_calls = [c for c in eval_calls if "api/comment" in c[0]]
    assert len(scroll_calls) >= 3
    assert len(submit_calls) == 1
    assert "/api/vote" not in submit_calls[0][0]
    assert 12.0 - 1e-6 <= sum(sleeps[1:]) <= 25.0 + 1e-6
    mock_browser.close.assert_called_once()


def test_seeding_dwell_and_no_auto_vote_in_poll() -> None:
    import inspect
    from tools.resident_agents_watcher import poll_and_remediate_via_pixel

    src = inspect.getsource(poll_and_remediate_via_pixel)
    assert "/api/vote" not in src
    assert "_human_scroll_and_dwell(page, min_dwell_sec=10.0, max_dwell_sec=20.0)" in src

