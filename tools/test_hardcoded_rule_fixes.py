#!/usr/bin/env python3
"""Regression test suite verifying elimination of hardcoded rules across tools and sidecars."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

import sys
from pathlib import Path
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

PT = ZoneInfo("America/Los_Angeles")

class TestHardcodedRuleFixes(unittest.TestCase):

    def test_classifier_directive_prefilter_bypass(self):
        """Verify short engineering directives bypass the < 4 words prefilter."""
        from tools.classifier import score_relevance

        # Mock subprocess.run inside score_relevance to return 0.90
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "Score: 0.90"
            mock_run.return_value = mock_proc

            # Directives under 4 words without '?' should proceed to model scoring
            score_ship = score_relevance("ship it")
            self.assertEqual(score_ship, 0.90)

            score_tests = score_relevance("run tests")
            self.assertEqual(score_tests, 0.90)

            score_push = score_relevance("push the branch")
            self.assertEqual(score_push, 0.90)

            score_go = score_relevance("go ahead")
            self.assertEqual(score_go, 0.90)

        # Casual short chatter should still drop at pre-filter without model call
        with patch("subprocess.run") as mock_run:
            score_chatter = score_relevance("lol haha")
            self.assertEqual(score_chatter, 0.0)
            mock_run.assert_not_called()

    def test_thread_titling_rate_limit_collision_removed(self):
        """Verify rate limit and 429 prompts do not get misattributed to Arr Media Indexer."""
        from tools.bridge_formatting import generate_concise_thread_title

        title_429 = generate_concise_thread_title("We are hitting a 429 error on the Discord gateway")
        self.assertNotIn("Arr Media Indexer", title_429)

        title_rate = generate_concise_thread_title("Investigate rate limit policies for the Gemini API")
        self.assertNotIn("Arr Media Indexer", title_rate)

        # Sonarr/Prowlarr specific prompts still match
        title_sonarr = generate_concise_thread_title("Check sonarr logs for download failures")
        self.assertEqual(title_sonarr, "Arr Media Indexer and Server Alerts")

    def test_couple_display_formatting_clean(self):
        """Verify format_couple_display_name works cleanly without name exclusions."""
        from tools.core_friends_reminder import format_couple_display_name

        p1 = {"Name": "Bob Smith", "Notes & Connections": "Married to Alice Smith"}
        p2 = {"Name": "Alice Smith", "Notes & Connections": "Married to Bob Smith"}
        combined = format_couple_display_name(p1, p2)
        self.assertEqual(combined, "Bob & Alice Smith")

        # Couple with different last names
        p3 = {"Name": "Charlie Brown"}
        p4 = {"Name": "Lucy Van Pelt"}
        combined_diff = format_couple_display_name(p3, p4)
        self.assertEqual(combined_diff, "Charlie Brown & Lucy Van Pelt")

    def test_market_standup_dynamic_agenda(self):
        """Verify market standup generates dynamic agendas from state."""
        from tools.market_standup import synthesize_standing_agenda

        state = {
            "open_prs": [
                {"number": 2, "title": "docs: wire specification", "author": {"login": "ryan"}}
            ],
            "recent_commits": [
                "`2d8f19f` Merge pull request #1 from amos/ledger-schema (Ryan)"
            ]
        }
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("offline")
            agenda = synthesize_standing_agenda(state)
            self.assertIn("PR #2: docs: wire specification", agenda)
            self.assertNotIn("Amos — Ledger DDL & SQLite genesis state", agenda)

    def test_weekly_digest_dynamic_reminders_and_cashflow(self):
        """Verify weekly digest queries dynamic sources without hardcoded lists."""
        from tools.weekly_digest import get_upcoming_reminders_and_renewals, get_recent_cashflow_rows

        now = datetime(2026, 9, 3, 10, 0, tzinfo=PT)
        renewals = get_upcoming_reminders_and_renewals(now, days=30)
        self.assertTrue(len(renewals) > 0)
        self.assertTrue(any("SmartThings" in r for r in renewals))

        rows = get_recent_cashflow_rows()
        self.assertTrue(len(rows) >= 3)
        self.assertIn("Item           Amount   Trend ", rows[0])

    def test_sidecars_email_triage_dynamic(self):
        """Verify nightly triage parses dates and categorizes without hardcoded haircut rules."""
        from tools.sidecars import _triage_unread_emails

        test_emails = [
            {
                "from": "Dental Clinic <reception@dental.com>",
                "subject": "Appointment Confirmation",
                "snippet": "Your cleaning is reserved for 10:30 AM on Friday, Oct 2 with Dr. Lee."
            },
            {
                "from": "Morning Brew <crew@morningbrew.com>",
                "subject": "Daily Tech Digest",
                "snippet": "Here is what happened in tech today."
            }
        ]
        cal, prio, news, trans = _triage_unread_emails(test_emails)
        self.assertTrue(any("10:30 AM" in c for c in cal))
        self.assertTrue(any("Morning Brew" in n for n in news))

    def test_session_summarizer_dynamic_milestones(self):
        """Verify session summarizer extracts dynamic milestones rather than static strings."""
        from tools.session_summarizer import synthesize_session_milestones

        dialogue = [
            {"speaker": "Ryan", "content": "Let's fix the classifier prefilter bug."},
            {"speaker": "Zero", "content": "Fixed and verified classifier prefilter bypass."}
        ]
        ms, dr, ed = synthesize_session_milestones(dialogue, "test_session")
        self.assertIn("classifier prefilter", ms.lower())
        self.assertNotIn("Google Takeout & Profile: Deep synthesis completed", ms)


if __name__ == "__main__":
    unittest.main()
