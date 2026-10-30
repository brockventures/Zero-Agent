#!/usr/bin/env python3
"""Unit tests for Weekly Digest dynamic reminders, Personal Sync receipt, and Fantasy Scanner player extraction."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.reddit_fantasy_scanner import (
    extract_candidate_players,
    format_draft_card,
    run_reddit_fantasy_scanner,
    stage_fantasy_decision,
)
from tools.weekly_digest import (
    generate_weekly_digest,
    get_recent_cashflow_rows,
    get_upcoming_reminders_and_renewals,
)

PT = ZoneInfo("America/Los_Angeles")


# ==============================================================================
# 1. WEEKLY DIGEST DYNAMIC REMINDER TESTS
# ==============================================================================
class TestWeeklyDigestReminders:
    """Tests dynamic reminder parsing without hardcoded keyword branches."""

    def test_dynamic_reminders_clean_extraction_and_deduplication(self, tmp_path: Path):
        """Verify dynamic text extraction strips markdown/emojis and deduplicates recurring messages."""
        fake_reminders = [
            {
                "id": "rem-1",
                "due": "2026-11-05",
                "message": "⏰ **SmartThings replacement reminder** — revisit ZWA-2 stick setup before paywall.",
            },
            {
                "id": "rem-2",
                "due": "2026-11-06",
                "message": "🔑 **Work Check-in Reminder:** Don't forget to run `gcert` today to keep access fresh.",
            },
            {
                "id": "rem-3",
                "due": "2026-11-11",
                "message": "🔑 **Work Check-in Reminder:** Don't forget to run `gcert` today to keep access fresh.",
            },
            {
                "id": "rem-4",
                "due": "2026-11-12",
                "message": "🔌 **ESP32 Sensor Maintenance:** Inspect CO2 sensor wiring and Wi-Fi reception.",
            },
            {
                "id": "rem-bill",
                "due": "2026-11-08",
                "message": "💳 Flagstar mortgage autopay payment scheduled.",
            },
        ]
        rems_file = tmp_path / "reminders.json"
        rems_file.write_text(json.dumps(fake_reminders), encoding="utf-8")

        now = datetime(2026, 11, 1, 10, 0, tzinfo=PT)
        with patch("tools.weekly_digest.Path") as mock_path:
            # Route Path("/workspace/data/reminders.json") to our tmp_path
            def path_side_effect(p):
                if "reminders.json" in str(p):
                    return rems_file
                return Path(p)

            mock_path.side_effect = path_side_effect

            with patch("tools.weekly_digest.get_upcoming_birthdays_and_anniversaries", return_value=[]):
                lines = get_upcoming_reminders_and_renewals(now, days=30)

        assert len(lines) == 3
        # 1. SmartThings extracted cleanly without emojis/markdown
        assert any("SmartThings replacement reminder" in line for line in lines)
        # 2. Work Check-in extracted and deduplicated (appears only once despite 2 due dates)
        gcert_lines = [line for line in lines if "Work Check-in Reminder" in line]
        assert len(gcert_lines) == 1
        assert "Nov 06" in gcert_lines[0]
        # 3. Custom sensor maintenance included
        assert any("ESP32 Sensor Maintenance" in line for line in lines)
        # 4. Financial autopay bill reminder suppressed
        assert not any("Flagstar" in line or "autopay" in line.lower() for line in lines)


# ==============================================================================
# 2. PERSONAL DATA & GAMING PROFILE SYNC RECEIPT TESTS
# ==============================================================================
class TestWeeklyDigestPersonalSync:
    """Tests Personal Data & Gaming Profile Sync receipt inclusion in Weekly Household Radar."""

    def test_weekly_digest_includes_personal_sync_receipt(self, tmp_path: Path):
        """Verify generate_weekly_digest appends compact 1-line Personal Sync receipt when present."""
        fake_status = {
            "personal_data_sync": {
                "job_id": "personal_data_sync",
                "name": "Weekly Personal Data & Gaming Profile Sync",
                "status": "ok",
                "summary": "**Steam:** 726 games synced | **Xbox:** 153 titles, 40540 G synced | **Switch:** 38 summaries",
                "extra": {
                    "summary": "**Steam:** 726 games synced | **Xbox:** 153 titles, 40540 G synced | **Switch:** 38 summaries",
                },
            }
        }
        status_file = tmp_path / "sidecar_status.json"
        status_file.write_text(json.dumps(fake_status), encoding="utf-8")

        now = datetime(2026, 11, 1, 20, 0, tzinfo=PT)
        with patch("tools.weekly_digest.get_week_ahead_events", return_value=["• **Mon Nov 02** — Team Planning"]):
            with patch("tools.weekly_digest.get_upcoming_reminders_and_renewals", return_value=["• **Nov 06** — Renewals"]):
                with patch("tools.weekly_digest.Path") as mock_path:
                    def path_side_effect(p):
                        if "sidecar_status.json" in str(p):
                            return status_file
                        return Path(p)

                    mock_path.side_effect = path_side_effect

                    digest = generate_weekly_digest(now)

        assert "📊 **Weekly Household Radar**" in digest
        assert "🎮 **Weekly Profile Sync:**" in digest
        assert "**Steam:** 726 games synced | **Xbox:** 153 titles, 40540 G synced" in digest

    def test_weekly_digest_handles_missing_personal_sync(self, tmp_path: Path):
        """Verify generate_weekly_digest completes cleanly when personal_data_sync is absent."""
        status_file = tmp_path / "sidecar_status.json"
        status_file.write_text(json.dumps({}), encoding="utf-8")

        now = datetime(2026, 11, 1, 20, 0, tzinfo=PT)
        with patch("tools.weekly_digest.get_week_ahead_events", return_value=[]):
            with patch("tools.weekly_digest.get_upcoming_reminders_and_renewals", return_value=[]):
                with patch("tools.weekly_digest.Path") as mock_path:
                    def path_side_effect(p):
                        if "sidecar_status.json" in str(p):
                            return status_file
                        return Path(p)

                    mock_path.side_effect = path_side_effect

                    digest = generate_weekly_digest(now)

        assert "📊 **Weekly Household Radar**" in digest
        assert "🎮 **Weekly Profile Sync:**" not in digest


# ==============================================================================
# 3. ACTUAL BUDGET CASHFLOW QUERY TESTS
# ==============================================================================
class TestActualBudgetCashflow:
    """Tests Actual Budget recurring transactions query before Gmail fallback."""

    @patch("actual.Actual")
    @patch("actual.queries.get_schedules")
    @patch("actual.queries.get_payees")
    def test_cashflow_queries_actual_budget_schedules_first(self, mock_payees, mock_schedules, mock_actual_cls):
        """Verify get_recent_cashflow_rows queries Actual Budget recurring transactions dynamically."""
        mock_actual = MagicMock()
        mock_actual_cls.return_value.__enter__.return_value = mock_actual

        # Mock schedule entries
        sched1 = MagicMock()
        sched1.name = "Preschool (PCC)"
        sched1.amount = 77900
        sched1.rule = None

        sched2 = MagicMock()
        sched2.name = "Mortgage P&I"
        sched2.amount = 836000
        sched2.rule = None

        mock_schedules.return_value = [sched1, sched2]
        mock_payees.return_value = []

        with patch("tools.weekly_digest.gmail_search") as mock_gmail:
            rows = get_recent_cashflow_rows()
            # Gmail search is skipped when Actual Budget yields detected items
            mock_gmail.assert_not_called()

        assert len(rows) >= 3
        assert "Item           Amount   Trend " in rows[0]
        assert any("Preschool (PCC" in r and "779.00" in r for r in rows)
        assert any("Mortgage P&I" in r and "8360.00" in r for r in rows)

    @patch("actual.Actual", side_effect=Exception("Actual connection timeout"))
    def test_cashflow_falls_back_to_gmail_on_actual_failure(self, _mock_actual):
        """Verify get_recent_cashflow_rows falls back to Gmail search when Actual Budget fails."""
        gmail_res = json.dumps({
            "messages": [
                {
                    "from": "billing@pge.com",
                    "subject": "Your PG&E Energy Statement",
                    "snippet": "Amount due: $285.50 for current billing cycle",
                }
            ]
        })
        with patch("tools.weekly_digest.gmail_search", return_value=gmail_res) as mock_gmail:
            rows = get_recent_cashflow_rows()
            mock_gmail.assert_called_once()

        assert any("PG&E Net" in r and "285.50" in r for r in rows)


# ==============================================================================
# 4. DETERMINISTIC-FIRST PLAYER EXTRACTION TESTS
# ==============================================================================
class TestPlayerExtraction:
    """Tests fast deterministic player token matching before LLM entity extraction."""

    def test_extract_candidate_players_deterministic_nicknames(self):
        """Verify common abbreviations and nicknames resolve without spawning LLM subprocess."""
        sample_texts = [
            ("Who should I keep between PCA and Chourio in dynasty?", ["Pete Crow-Armstrong", "Jackson Chourio"]),
            ("Should I trade Cole Ragans for Jackson Merrill?", ["Cole Ragans", "Jackson Merrill"]),
            ("Thoughts on Miz vs Skenes in a 12-team points league?", ["Jacob Misiorowski", "Paul Skenes"]),
            ("Looking to buy low on Elly or Witt.", ["Elly De La Cruz", "Bobby Witt Jr."]),
        ]

        with patch("subprocess.run") as mock_subproc:
            for text, expected_players in sample_texts:
                players = extract_candidate_players(text, allow_llm_fallback=True)
                for exp in expected_players:
                    assert exp in players, f"Expected {exp} in extracted players: {players}"
            # Because all were matched deterministically, subprocess.run was never called
            mock_subproc.assert_not_called()

    def test_extract_candidate_players_deterministic_pool(self):
        """Verify custom player pool full names match deterministically without LLM."""
        pool = ["Carson Benge", "Travis Bazzana", "Chase Burns"]
        text = "Would you stash Travis Bazzana or Chase Burns on your bench?"

        with patch("subprocess.run") as mock_subproc:
            players = extract_candidate_players(text, player_pool=pool, allow_llm_fallback=True)
            mock_subproc.assert_not_called()

        assert "Travis Bazzana" in players
        assert "Chase Burns" in players

    @patch("subprocess.run")
    def test_extract_candidate_players_llm_fallback_when_zero_deterministic(self, mock_subproc):
        """Verify LLM extraction subprocess is invoked ONLY when deterministic matching finds 0 players."""
        mock_res = MagicMock()
        mock_res.stdout = '["Erick Fedde"]'
        mock_subproc.return_value = mock_res

        # Comment with an obscure name not in NICKNAME_MAP
        text = "Should I pick up this obscure starter from the waiver wire tonight?"
        players = extract_candidate_players(text, allow_llm_fallback=True)

        mock_subproc.assert_called_once()
        assert players == ["Erick Fedde"]

    @patch("subprocess.run")
    def test_extract_candidate_players_skips_llm_when_fallback_disabled(self, mock_subproc):
        """Verify allow_llm_fallback=False never spawns subprocess."""
        text = "Generic dilemma with no recognized baseball names."
        players = extract_candidate_players(text, allow_llm_fallback=False)

        mock_subproc.assert_not_called()
        assert players == []


# ==============================================================================
# 5. REDDIT FANTASY SCANNER DECISION CARD STAGING TESTS
# ==============================================================================
class TestRedditFantasyDecisionCards:
    """Tests persistent SQLite Decision Card staging in Reddit Fantasy Baseball Scanner."""

    @patch("tools.forge.stage_decision")
    def test_stage_fantasy_decision_creates_shell_card(self, mock_stage):
        """Verify stage_fantasy_decision constructs a valid shell Decision Card payload."""
        mock_stage.return_value = {"decision_id": "dec-12345-red", "status": "staged"}

        cand = {
            "author": "DynastyGuru99",
            "thread_title": "Daily Anything Goes Thread - Oct 30",
            "thread_url": "https://www.reddit.com/r/fantasybaseball/comments/1abc/",
            "text": "Should I keep Pete Crow-Armstrong or Jackson Merrill in a 12-team points league?",
        }
        draft = "Merrill has the safer contact floor with lower whiff risk. I'd lean his way."

        res = stage_fantasy_decision(cand, draft, channel_id=1542081375287640084, post_to_discord=True)
        assert res is not None
        assert res["decision_id"] == "dec-12345-red"

        mock_stage.assert_called_once()
        kws = mock_stage.call_args.kwargs
        assert kws["category"] == "reddit_fantasy"
        assert "DynastyGuru99" in kws["title"]
        assert kws["handler_type"] == "shell"
        assert kws["payload"]["command"] == 'python3 /workspace/tools/reddit_fantasy_scanner.py --post-draft --author "DynastyGuru99"'
        assert kws["payload"]["cwd"] == "/workspace"
        assert kws["post_to_discord"] is True

    @patch("tools.reddit_fantasy_scanner.scan_reddit_via_pixel")
    @patch("tools.reddit_fantasy_scanner.generate_draft_comment")
    @patch("tools.reddit_fantasy_scanner.stage_fantasy_decision")
    def test_run_reddit_fantasy_scanner_stages_decision_when_stage_true(
        self, mock_stage_dec, mock_gen_draft, mock_scan
    ):
        """Verify run_reddit_fantasy_scanner stages Decision Card and avoids duplicate outbox dispatch."""
        mock_scan.return_value = [
            {
                "author": "ActiveManager",
                "thread_title": "Anything Goes",
                "thread_url": "https://reddit.com/test",
                "text": "Trade Cole Ragans for Jackson Merrill in 12-team dynasty?",
                "raw_id": "t1_test123",
                "depth": 0,
                "reply_count": 0,
                "time_ago": "5m ago",
            }
        ]
        mock_gen_draft.return_value = "I'd hold Ragans here given pitching scarcity."
        mock_stage_dec.return_value = {"decision_id": "dec-999-red", "status": "staged"}

        with patch("tools.outbox.queue_outbox_message") as mock_outbox:
            ok, report, meta = run_reddit_fantasy_scanner(test=True, stage=True, post_discord=True)

        assert ok is True
        assert meta["decision_staged"] is True
        assert meta["selected"]["author"] == "ActiveManager"
        mock_stage_dec.assert_called_once()
        # Because stage_decision handled Discord posting (post_to_discord=True), outbox is not double-dispatched
        mock_outbox.assert_not_called()
        assert "Persistent Decision Card staged below" in report

    def test_format_draft_card_distinguishes_staged_vs_choices(self):
        """Verify format_draft_card adjusts footer depending on decision_staged boolean."""
        cand = {
            "author": "TestAuthor",
            "thread_title": "Daily Thread",
            "thread_url": "https://reddit.com/daily",
            "text": "Trade A for B?",
        }
        draft = "Take player A."

        # Staged mode
        staged_card = format_draft_card(cand, draft, decision_staged=True)
        assert "Persistent Decision Card staged below" in staged_card
        assert "[CHOICES:" not in staged_card

        # Legacy choices mode
        choices_card = format_draft_card(cand, draft, decision_staged=False)
        assert "[CHOICES: Post Draft to Reddit | Edit Draft | Skip Candidate]" in choices_card
