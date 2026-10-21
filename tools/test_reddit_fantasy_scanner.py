import sys
sys.path.insert(0, "/workspace")
"""Unit tests for Reddit Fantasy Baseball Scanner sidecar."""

import unittest
from unittest.mock import MagicMock, patch

from tools.reddit_fantasy_scanner import (
    generate_draft_comment,
    format_draft_card,
    load_state,
    save_state,
    run_reddit_fantasy_scanner,
)


class TestRedditFantasyScanner(unittest.TestCase):

    def test_draft_comment_length_and_syntax(self):
        """Verify draft comments adhere to 2 sentences and anti-AI syntax rules."""
        sample_questions = [
            ("Who are your sleeper Closer candidates for next year?", "user1"),
            ("Which 4 players should I keep in our 12-team league?", "user2"),
            ("Should I trade Cole Ragans for Jackson Merrill in dynasty?", "user3"),
            ("General question about waiver wire strategy", "user4"),
        ]

        for q, author in sample_questions:
            draft = generate_draft_comment(q, author)
            self.assertIsNotNone(draft)
            # Must not contain em dashes
            self.assertNotIn("—", draft)
            # Count sentences (approximately by period)
            sentences = [s.strip() for s in draft.split(".") if s.strip()]
            self.assertLessEqual(len(sentences), 2, f"Draft exceeded 2 sentences: {draft}")

    def test_score_candidate_subcomment_and_reply_disqualified(self):
        """Verify sub-comments (depth > 0) and reply-to-dilemma comments are strictly disqualified."""
        from tools.reddit_fantasy_scanner import score_candidate
        sub_cand = {
            "text": "Should I trade pitcher X for batter Y in a 12-team keeper?",
            "reply_count": 0,
            "depth": 1,
            "time_ago": "10m ago",
        }
        score_sub, reasons_sub = score_candidate(sub_cand)
        self.assertLess(score_sub, 0)
        self.assertTrue(any("Sub-comment Disqualified" in r for r in reasons_sub))

        reply_cand = {
            "text": "Cease ended the year injured, right? I would definitely do this otherwise, and probably still would.",
            "reply_count": 1,
            "depth": 0,
            "time_ago": "1d ago",
        }
        score_rep, reasons_rep = score_candidate(reply_cand)
        self.assertLess(score_rep, 0)
        self.assertTrue(any("Disqualified" in r for r in reasons_rep))

    def test_format_draft_card(self):
        """Verify card formatting contains expected sections, excludes Quality Score, and preserves full comment text."""
        long_intro = "In our 12-team points league where we keep 8 players forever with no round penalties, I am trying to decide my last two keeper spots and whether to make a 2-for-1 trade years ago.\n"
        full_text = long_intro + ("\n".join([f"{i}. Player {i}" for i in range(1, 12)])) + "\n\nWho should I keep between Merrill and Caminero?"
        cand = {
            "thread_title": "Daily Anything Goes Thread - Oct 11",
            "thread_url": "https://www.reddit.com/r/fantasybaseball/comments/123/",
            "author": "TestManager",
            "text": full_text,
            "quality_score": 115.0,
        }
        draft = "Merrill has the safer contact floor with a .270-ish average. I would lock him in."
        card = format_draft_card(cand, draft)

        self.assertIn("r/fantasybaseball Discovery & Draft Card", card)
        self.assertNotIn("Quality Score:", card)
        self.assertIn("u/TestManager", card)
        self.assertIn("12-team points league", card)
        self.assertIn("Who should I keep between Merrill and Caminero?", card)
        self.assertNotIn("...", card)
        self.assertIn(draft, card)
        self.assertIn("https://www.reddit.com/r/fantasybaseball/comments/123/", card)

    @patch("tools.reddit_fantasy_scanner.scan_reddit_via_pixel")
    def test_run_sidecar_contract(self, mock_scan):
        """Verify sidecar contract return types (bool, str, dict)."""
        mock_scan.return_value = [
            {
                "thread_title": "Daily Thread",
                "thread_url": "https://reddit.com/1",
                "author": "Alice",
                "text": "Trade advice: should I trade pitcher X for batter Y?",
                "raw_id": "test_id_1"
            }
        ]

        ok, report, extra = run_reddit_fantasy_scanner(test=True, quiet=True)
        self.assertTrue(ok)
        self.assertIn("Discovery & Draft Card", report)
        self.assertIn("selected", extra)
        self.assertIn("draft", extra)

    def test_format_draft_card_multiline_blockquote(self):
        """Verify multiline dilemmas get proper '>' prefix on every line for Discord."""
        cand = {
            "thread_title": "Daily Anything Goes Thread",
            "thread_url": "https://www.reddit.com/r/fantasybaseball/comments/456/",
            "author": "DynastyOwner",
            "text": "First line of dilemma.\n\nSecond line after blank line.\nThird line.",
        }
        draft = "Hold your young talent. Do not trade picks. Stay patient."
        card = format_draft_card(cand, draft)

        self.assertIn("> First line of dilemma.", card)
        self.assertIn(">\n> Second line after blank line.", card)
        self.assertIn("> Third line.", card)
        self.assertNotIn('> *"', card)

    def test_score_candidate_unanswered_dynasty_ranks_high(self):
        """Verify unanswered top-level dynasty dilemmas receive high quality scores."""
        from tools.reddit_fantasy_scanner import score_candidate
        cand = {
            "text": "In a 12-team dynasty rebuild, should I trade my veteran closers for young pitching prospects or hold for deadline value?",
            "reply_count": 0,
            "depth": 0,
            "time_ago": "25m ago",
        }
        score, reasons = score_candidate(cand)
        self.assertGreaterEqual(score, 85)
        self.assertTrue(any("Unanswered" in r for r in reasons))
        self.assertTrue(any("League Context" in r for r in reasons))

    def test_score_candidate_crowded_penalized(self):
        """Verify crowded comments with 3+ replies are heavily penalized."""
        from tools.reddit_fantasy_scanner import score_candidate
        cand = {
            "text": "Trade advice: should I trade pitcher X for batter Y?",
            "reply_count": 4,
            "depth": 0,
            "time_ago": "1h ago",
        }
        score, reasons = score_candidate(cand)
        self.assertLess(score, 60)
        self.assertTrue(any("Crowded" in r for r in reasons))

    def test_score_candidate_off_topic_disqualified(self):
        """Verify off-topic non-baseball queries are disqualified with negative scores."""
        from tools.reddit_fantasy_scanner import score_candidate
        cand = {
            "text": "Should I start Mahomes or pick up another quarterback for fantasy football?",
            "reply_count": 0,
            "depth": 0,
            "time_ago": "10m ago",
        }
        score, reasons = score_candidate(cand)
        self.assertLess(score, 0)
        self.assertTrue(any("Off-Topic" in r for r in reasons))

    @patch("tools.reddit_fantasy_scanner.scan_reddit_via_pixel")
    def test_runner_ranks_candidates_by_quality(self, mock_scan):
        """Verify runner picks the highest-scored candidate rather than the first DOM element."""
        mock_scan.return_value = [
            {
                # First in DOM, but low quality (brief, crowded, 3 replies)
                "thread_title": "Daily Thread",
                "thread_url": "https://reddit.com/1",
                "author": "LazyPoster",
                "text": "drop him?",
                "raw_id": "cand_1",
                "reply_count": 3,
                "depth": 1,
            },
            {
                # Second in DOM, but high quality (unanswered, dynasty context, top-level)
                "thread_title": "Daily Thread",
                "thread_url": "https://reddit.com/1",
                "author": "ThoughtfulManager",
                "text": "In our 14-team keeper league, should I keep Merrill at $5 or Caminero at $3 for long-term category floor?",
                "raw_id": "cand_2",
                "reply_count": 0,
                "depth": 0,
                "time_ago": "20m ago",
            },
        ]

        ok, report, extra = run_reddit_fantasy_scanner(test=True, quiet=True)
        self.assertTrue(ok)
        selected = extra.get("selected", {})
        # Must select ThoughtfulManager over LazyPoster
        self.assertEqual(selected.get("author"), "ThoughtfulManager")
        self.assertIn("ThoughtfulManager", report)

    def test_old_reddit_top_level_with_child_parent_button_not_disqualified(self):
        """Verify Old Reddit top-level comment whose child reply has a 'parent' link is kept at depth=0."""
        import re
        raw_entry_text = (
            "[–]AverageKhorneWorship 10 man points league yahoo 2 points 23 hours ago \n\n"
            "Tolle or Seth Hernandez?\n\n"
            "permalinkembedsavereportreply\n\n"
            "[–]Penstripedsox 1 point 18 hours ago \n\n"
            "who is seth hernandez? tolle is a top 20 dynasty pitcher already.\n\n"
            "permalinkembedsaveparentreportreply"
        )
        own_entry_text = re.split(r"\n\s*\[[–-]\]", raw_entry_text.strip())[0]
        self.assertIsNone(re.search(r"permalink.*?parent.*?reply", own_entry_text.lower()))

        child_entry_text = (
            "[–]Penstripedsox 1 point 18 hours ago \n\n"
            "who is seth hernandez? tolle is a top 20 dynasty pitcher already.\n\n"
            "permalinkembedsaveparentreportreply"
        )
        own_child_text = re.split(r"\n\s*\[[–-]\]", child_entry_text.strip())[0]
        self.assertIsNotNone(re.search(r"permalink.*?parent.*?reply", own_child_text.lower()))

        from tools.reddit_fantasy_scanner import score_candidate
        meta_score, _ = score_candidate({"text": "Can we make this weekly?", "reply_count": 0, "depth": 0, "time_ago": "2h ago"})
        self.assertLess(meta_score, 0)
        versus_score, _ = score_candidate({"text": "Tolle or Seth Hernandez?", "reply_count": 0, "depth": 0, "time_ago": "2h ago"})
        self.assertGreaterEqual(versus_score, 60)


if __name__ == "__main__":
    unittest.main()
