#!/usr/bin/env python3
"""
Unit test suite for tools/session_summarizer.py.
Validates clean_dialogue_content, extract_recent_dialogue, and summary formatting.
"""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.session_summarizer as session_summarizer
from tools.session_summarizer import (
    _extract_prior_milestones_from_transcript,
    _fetch_channel_scoped_forge_builds,
    _resolve_channel_id_for_session,
    clean_dialogue_content,
    generate_summary,
)


class TestSessionSummarizer(unittest.TestCase):

    def test_clean_user_dialogue_with_system_time_and_gif(self):
        raw = (
            "<USER_REQUEST>\n"
            "[System Time & Timezone]: Current time is Friday, Sep 04, 2026 12:16 AM PT (America/Los_Angeles).\n"
            "• Note: System VM clock and runtime metadata are UTC (07:16:16 UTC).\n"
            "• Rule: ALWAYS use Pacific Time (PT). Never quote raw UTC timestamps or assume raw UTC is local time.\n\n"
            "[GIF Cadence Tracker (Channel: 1544953279664889888)]: 1 message(s) since last reaction GIF in this channel.\n"
            "• Target Cadence: ~1 in 5-7 messages (use: python3 /workspace/tools/gif_tool.py \"<query>\").\n"
            "• Status: Nominal (1/5-7 turns).\n"
            "• Contextual Overrides:\n"
            "  - Serious / Critical Override: If the message/topic is serious, urgent, an outage, data entry, or sensitive, override and SKIP the GIF regardless of count.\n"
            "  - Social / Banter Override: If the exchange is particularly social, humorous, or banter-laden, you may include a GIF even if count < 5.\n\n"
            "I want to ask you here if we can troubleshoot our current issues with the new persistent channels model.\n"
            "</USER_REQUEST>\n"
            "<ADDITIONAL_METADATA>\n"
            "The current local time is: 2026-09-04T07:16:20Z.\n"
            "</ADDITIONAL_METADATA>\n"
            "<USER_SETTINGS_CHANGE>\n"
            "The user changed setting `Model Selection` from None to Gemini 3.8 Flash (High).\n"
            "</USER_SETTINGS_CHANGE>"
        )
        cleaned = clean_dialogue_content(raw, is_user=True)
        self.assertEqual(
            cleaned,
            "I want to ask you here if we can troubleshoot our current issues with the new persistent channels model."
        )

    def test_clean_user_dialogue_with_carryforward_and_current_prompt(self):
        raw = (
            "<USER_REQUEST>\n"
            "[System Time & Timezone]: Current time is Friday, Sep 04, 2026 12:25 AM PT (America/Los_Angeles).\n"
            "• Note: System VM clock and runtime metadata are UTC (07:25:24 UTC).\n"
            "• Rule: ALWAYS use Pacific Time (PT).\n\n"
            "[GIF Cadence Tracker (Channel: 123)]: 2 message(s) since last reaction GIF.\n"
            "• Target Cadence: ~1 in 5-7 messages.\n\n"
            "[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\n"
            "<!-- Smart Rolling Compaction Generated 2026-09-04 12:25 AM PT -->\n"
            "## 1. Compacted Earlier Session History\n"
            "- Did stuff\n\n"
            "[CURRENT USER PROMPT]: Fix compaction first.\n\n"
            "Is your suggestion basically to revert back to the way this worked up until a few hours ago?\n"
            "</USER_REQUEST>"
        )
        cleaned = clean_dialogue_content(raw, is_user=True)
        self.assertEqual(
            cleaned,
            "Fix compaction first.\n\nIs your suggestion basically to revert back to the way this worked up until a few hours ago?"
        )

    def test_clean_user_dialogue_with_mid_turn_steering(self):
        raw = (
            "<USER_REQUEST>\n"
            "[System Time & Timezone]: Current time is Friday, Sep 04, 2026 12:27 AM PT (America/Los_Angeles).\n\n"
            "[USER MID-TURN STEERING UPDATE]\n"
            "The user provided new instructions while you were in the middle of executing:\n"
            "\"To be clear this was the option a and b I was talking about. You came up with these in Zero-Chat\n\n"
            "[Attached file(s) available via view_file tool]:\n"
            "- /workspace/data/attachments/d84612bc_Screenshot_20260904-002704.png\"\n\n"
            "CRITICAL INSTRUCTIONS FOR REVISED TURN:\n"
            "1. Absorb this directive immediately.\n"
            "</USER_REQUEST>"
        )
        cleaned = clean_dialogue_content(raw, is_user=True)
        self.assertIn("To be clear this was the option a and b I was talking about.", cleaned)
        self.assertNotIn("CRITICAL INSTRUCTIONS FOR REVISED TURN", cleaned)
        self.assertNotIn("[USER MID-TURN STEERING UPDATE]", cleaned)

    def test_clean_external_inbound_message(self):
        raw = (
            "[CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT]\n"
            "You are Zero, an autonomous systems engineering co-pilot...\n\n"
            "[System Time & Timezone]: Current time is Friday, Sep 04, 2026 12:16 AM PT.\n\n"
            "[INBOUND MESSAGE from Alex]: How is the migration going?"
        )
        cleaned = clean_dialogue_content(raw, is_user=True)
        self.assertEqual(cleaned, "How is the migration going?")

    def test_clean_zero_response(self):
        raw = (
            "Here is the forensic breakdown.\n\n"
            "[CHOICES: Option 1 | Option 2]\n\n"
            "[System Context] Current Local Time: Friday, September 04, 2026, 12:28:13 AM PDT"
        )
        cleaned = clean_dialogue_content(raw, is_user=False)
        self.assertEqual(cleaned, "Here is the forensic breakdown.")

    def test_generate_summary_parent_stamping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with patch.object(session_summarizer, "DATA_DIR", tmp_path), \
                 patch.object(session_summarizer, "BRAIN_DIR", tmp_path / "brain"):
                summary = generate_summary(
                    conv_id="conv-parent-abc",
                    sess_key="test_key",
                    dry_run=True,
                    db_path=tmp_path / "nonexistent_forge.db",
                )
                self.assertIn("[Parent: conv-parent-abc]", summary)
                self.assertIn("• **Parent Session UUID:** `conv-parent-abc`", summary)

    def test_resolve_channel_id_for_session(self):
        self.assertEqual(_resolve_channel_id_for_session("home"), 1542081375287640084)
        self.assertEqual(_resolve_channel_id_for_session("zero-chat"), 1542081375287640084)
        self.assertEqual(_resolve_channel_id_for_session("chan_1534452820995080192"), 1534452820995080192)
        self.assertEqual(_resolve_channel_id_for_session("1534452820995080192"), 1534452820995080192)
        self.assertEqual(_resolve_channel_id_for_session("lounge"), 1534452820995080192)
        self.assertIsNone(_resolve_channel_id_for_session("unknown_nonexistent_session_xyz"))
        self.assertIsNone(_resolve_channel_id_for_session(""))

    def test_fetch_channel_scoped_forge_builds_strict_isolation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "forge.db"
            now = time.time()
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE forge_builds (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL,
                        origin_channel_id INTEGER NOT NULL,
                        created_at REAL NOT NULL
                    )
                    """
                )
                # Build for home (#zero-chat: 1542081375287640084)
                conn.execute(
                    "INSERT INTO forge_builds (id, title, status, origin_channel_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("fg-home-1", "Home Build Alpha", "completed", 1542081375287640084, now - 600),
                )
                # Older build > 12h for home (should be excluded)
                conn.execute(
                    "INSERT INTO forge_builds (id, title, status, origin_channel_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("fg-home-old", "Stale Home Build", "completed", 1542081375287640084, now - 20 * 3600),
                )
                # Build for another channel (#lounge: 1534452820995080192)
                conn.execute(
                    "INSERT INTO forge_builds (id, title, status, origin_channel_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("fg-lounge-1", "Lounge Widget Build", "running", 1534452820995080192, now - 300),
                )
                # Build for #baseball (1548196929308065893)
                conn.execute(
                    "INSERT INTO forge_builds (id, title, status, origin_channel_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("fg-baseball-1", "Sabermetric Engine Build", "completed", 1548196929308065893, now - 100),
                )
                conn.commit()

            home_builds = _fetch_channel_scoped_forge_builds("home", db_path=db_path)
            self.assertEqual(len(home_builds), 1)
            self.assertIn("fg-home-1", home_builds[0])
            self.assertIn("Home Build Alpha", home_builds[0])
            for b in home_builds:
                self.assertNotIn("fg-lounge-1", b)
                self.assertNotIn("fg-baseball-1", b)
                self.assertNotIn("fg-home-old", b)

            lounge_builds = _fetch_channel_scoped_forge_builds("chan_1534452820995080192", db_path=db_path)
            self.assertEqual(len(lounge_builds), 1)
            self.assertIn("fg-lounge-1", lounge_builds[0])
            self.assertNotIn("fg-home-1", lounge_builds[0])

            unknown_builds = _fetch_channel_scoped_forge_builds("unknown_session_key", db_path=db_path)
            self.assertEqual(unknown_builds, [])

    def test_older_turns_and_multi_compaction_milestones_survive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            brain_dir = tmp_path / "brain"
            data_dir = tmp_path / "data"
            conv_id = "conv-multi-compact-1"
            log_dir = brain_dir / conv_id / ".system_generated" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)

            transcript_file = log_dir / "transcript_full.jsonl"
            carryforward_turn = (
                "<USER_REQUEST>\n"
                "[PREVIOUS SESSION CARRY-FORWARD CONTEXT]:\n"
                "<!-- Smart Rolling Compaction Generated 2026-11-02 09:00 PM PT [Session: home] -->\n"
                "## 1. Compacted Earlier Session History (Milestones)\n"
                "• **Parent Session UUID:** `conv-prior-000`\n"
                "- Completed Phase 1 database schema migration.\n"
                "- Configured LAN reverse proxy ACLs.\n\n"
                "## 2. Recent Verbatim Dialogue (Line-by-Line Context)\n"
                "• **Ryan:** Keep going.\n\n"
                "[CURRENT USER PROMPT]: Start Phase 2 older work.\n"
                "</USER_REQUEST>"
            )

            lines = [
                json.dumps({"type": "USER_INPUT", "content": carryforward_turn, "created_at": "2026-11-03T01:00:00Z"}),
                json.dumps({"type": "PLANNER_RESPONSE", "content": "Shipped Phase 2 older deliverable A.", "created_at": "2026-11-03T01:01:00Z"}),
                json.dumps({"type": "USER_INPUT", "content": "Now run older task B.", "created_at": "2026-11-03T01:02:00Z"}),
                json.dumps({"type": "PLANNER_RESPONSE", "content": "Completed older task B verification.", "created_at": "2026-11-03T01:03:00Z"}),
            ]
            # Add 10 recent turns (5 user + 5 zero) that belong in Section 2
            for i in range(1, 6):
                lines.append(
                    json.dumps({"type": "USER_INPUT", "content": f"Recent user prompt {i}", "created_at": f"2026-11-03T02:0{i}:00Z"})
                )
                lines.append(
                    json.dumps({"type": "PLANNER_RESPONSE", "content": f"Recent Zero reply {i}", "created_at": f"2026-11-03T02:1{i}:00Z"})
                )
            transcript_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            # Also create a channel-scoped FORGE DB
            db_path = tmp_path / "forge.db"
            now = time.time()
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE forge_builds (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL,
                        origin_channel_id INTEGER NOT NULL,
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO forge_builds (id, title, status, origin_channel_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("fg-home-99", "Fix Rolling Compaction", "completed", 1542081375287640084, now - 120),
                )
                conn.execute(
                    "INSERT INTO forge_builds (id, title, status, origin_channel_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("fg-other-88", "Unrelated Channel Build", "completed", 1534452820995080192, now - 60),
                )
                conn.commit()

            extracted_prior = _extract_prior_milestones_from_transcript(transcript_file, "home")
            self.assertIn("- Completed Phase 1 database schema migration.", extracted_prior)
            self.assertIn("- Configured LAN reverse proxy ACLs.", extracted_prior)

            with patch.object(session_summarizer, "BRAIN_DIR", brain_dir), \
                 patch.object(session_summarizer, "DATA_DIR", data_dir), \
                 patch("tools.session_summarizer.subprocess.Popen", side_effect=OSError("Force deterministic fallback")):
                summary = generate_summary(
                    conv_id=conv_id,
                    sess_key="home",
                    dry_run=False,
                    db_path=db_path,
                )

            sec1 = summary.split("## 2. Recent Verbatim Dialogue")[0]
            sec2 = summary.split("## 2. Recent Verbatim Dialogue")[1].split("## 3. Active Directives")[0]

            # Prior milestones survived into Section 1
            self.assertIn("- Completed Phase 1 database schema migration.", sec1)
            self.assertIn("- Configured LAN reverse proxy ACLs.", sec1)
            # Older turns (dialogue[:-10]) were synthesized into Section 1 instead of duplicating recent turns
            self.assertIn("Completed older task B verification.", sec1)
            self.assertIn("Shipped Phase 2 older deliverable A.", sec1)
            self.assertNotIn("Recent Zero reply 5", sec1)
            # Channel-scoped FORGE build for home is included, while other channel build is excluded
            self.assertIn("fg-home-99", sec1)
            self.assertNotIn("fg-other-88", sec1)

            # Section 2 contains the 10 recent turns and not the older turns
            self.assertIn("Recent user prompt 1", sec2)
            self.assertIn("Recent Zero reply 5", sec2)
            self.assertNotIn("Shipped Phase 2 older deliverable A.", sec2)

            # Milestones file was persisted and survives a subsequent compaction even if transcript lacks carryforward block
            milestones_file = data_dir / "milestones_home.md"
            self.assertTrue(milestones_file.exists())
            persisted = milestones_file.read_text(encoding="utf-8")
            self.assertIn("- Completed Phase 1 database schema migration.", persisted)
            self.assertIn("fg-home-99", persisted)


if __name__ == "__main__":
    unittest.main()
