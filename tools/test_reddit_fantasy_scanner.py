from pathlib import Path
import sys
import unittest
from unittest.mock import patch

for _p in ("/workspace", str(Path(__file__).resolve().parent.parent)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

from tools.reddit_fantasy_scanner import (  # noqa: E402
    generate_draft_comment,
    format_draft_card,
    post_draft_reply_via_pixel,
    run_reddit_fantasy_scanner,
    stage_fantasy_decision,
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
        self.assertIn("[CHOICES: Post Draft to Reddit | Edit Draft | Skip Candidate]", card)

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
        """Verify multiline dilemmas get proper '>' prefix on every line and collapse excessive blank lines between short items."""
        cand = {
            "thread_title": "Daily Anything Goes Thread",
            "thread_url": "https://www.reddit.com/r/fantasybaseball/comments/456/",
            "author": "DynastyOwner",
            "text": "First line of dilemma.\n\nSecond line after blank line.\nThird line.",
        }
        draft = "Hold your young talent. Do not trade picks. Stay patient."
        card = format_draft_card(cand, draft)

        self.assertIn("> First line of dilemma.", card)
        self.assertIn("> Second line after blank line.", card)
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

    @patch("tools.reddit_fantasy_scanner.scan_reddit_via_pixel")
    @patch("tools.outbox.dispatch_via_rest")
    @patch("tools.outbox.flush_pending_messages")
    @patch("tools.outbox.queue_outbox_message")
    @patch("tools.reddit_fantasy_scanner.stage_fantasy_decision")
    @patch("tools.reddit_fantasy_scanner.save_state")
    def test_production_run_posts_full_outbox_card_with_choices_by_default(
        self, mock_save, mock_stage, mock_outbox, mock_flush, mock_dispatch, mock_scan
    ):
        """Verify non-test runs post the full unclipped card via outbox with [CHOICES: ...] buttons by default."""
        mock_scan.return_value = [
            {
                "thread_title": "Daily Anything Goes Thread",
                "thread_url": "https://www.reddit.com/r/fantasybaseball/comments/999/",
                "author": "Shiny_Rattata",
                "text": "In our 12-team keeper league, should I trade Ragans for Merrill or hold my pitching depth?",
                "raw_id": "t1_shiny_999",
                "reply_count": 0,
                "depth": 0,
                "time_ago": "15m ago",
            }
        ]
        mock_flush.return_value = [{"id": "outbox-1", "channel": "zero-chat", "content": "card"}]
        ok, card, extra = run_reddit_fantasy_scanner(test=False, post_discord=True)
        self.assertTrue(ok)
        self.assertFalse(extra.get("decision_staged"))
        self.assertIn("⚾ **r/fantasybaseball Discovery & Draft Card**", card)
        self.assertIn("[CHOICES: Post Draft to Reddit | Edit Draft | Skip Candidate]", card)
        self.assertNotIn("Persistent Decision Card staged below", card)
        mock_stage.assert_not_called()
        mock_outbox.assert_called_once()
        mock_flush.assert_called_once()
        mock_dispatch.assert_called_once()
        mock_save.assert_called_once()
        saved_state = mock_save.call_args[0][0]
        self.assertEqual(saved_state["last_candidate"]["author"], "Shiny_Rattata")
        self.assertTrue(saved_state["last_draft"])

    @patch("tools.forge.stage_decision")
    def test_stage_fantasy_decision_includes_approve_action_and_cli_args(self, mock_forge_stage):
        """Verify stage_fantasy_decision builds an explicit Approve Action without truncating text at 280 chars and passes thread_url, thing_id, author, and draft."""
        mock_forge_stage.return_value = {"ok": True, "decision_id": "dec-456-reddit"}
        long_dilemma = (
            "Should I keep Chourio or Merrill in a 12-team 5x5 roto league? "
            + ("Here is extra league context that pushes the dilemma well beyond 280 characters. " * 4)
            + "END_OF_UNCLIPPED_DILEMMA"
        )
        cand = {
            "thread_title": "Nightly Anything Goes Thread",
            "thread_url": "https://www.reddit.com/r/fantasybaseball/comments/abc123/nightly/",
            "author": "KeeperKing",
            "text": long_dilemma,
            "raw_id": "t1_xyz789",
        }
        draft = "Merrill gives you the higher batting average floor, while Chourio has the 30/30 ceiling."
        res = stage_fantasy_decision(cand, draft, post_to_discord=True)
        self.assertIsNotNone(res)
        mock_forge_stage.assert_called_once()
        kwargs = mock_forge_stage.call_args.kwargs
        self.assertEqual(kwargs["category"], "reddit_fantasy")
        self.assertIn("END_OF_UNCLIPPED_DILEMMA", kwargs["summary"])
        self.assertIn("**Approve Action:** Post this reply to `r/fantasybaseball` via `u/prime416`", kwargs["summary"])
        cmd = kwargs["payload"]["command"]
        self.assertIn("--post-draft", cmd)
        self.assertIn("--author KeeperKing", cmd)
        self.assertIn("--thread-url https://www.reddit.com/r/fantasybaseball/comments/abc123/nightly/", cmd)
        self.assertIn("--thing-id t1_xyz789", cmd)
        self.assertIn("--draft", cmd)

    @patch("tools.pixel_browser.pixel_session")
    @patch("playwright.sync_api.sync_playwright")
    def test_post_draft_reply_via_pixel_upvotes_and_posts_comment(self, mock_pw, mock_pixel_session):
        """Verify post_draft_reply_via_pixel evaluates Old Reddit API upvote + comment and returns permalink."""
        mock_pixel_session.return_value.__enter__.return_value = "http://127.0.0.1:9223"
        mock_page = mock_pw.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value.contexts[0].pages[0]
        mock_page.url = "https://old.reddit.com/r/fantasybaseball/"
        mock_page.evaluate.return_value = {
            "ok": True,
            "permalink": "https://www.reddit.com/r/fantasybaseball/comments/abc123/nightly/new_reply_1/",
            "thing_id": "t1_xyz789",
        }

        ok, permalink = post_draft_reply_via_pixel(
            thread_url="https://www.reddit.com/r/fantasybaseball/comments/abc123/nightly/",
            author="KeeperKing",
            draft_text="Hold Merrill for the elite contact floor.",
            thing_id="t1_xyz789",
        )
        self.assertTrue(ok)
        self.assertIn("/comments/abc123/nightly/new_reply_1/", permalink)
        mock_page.evaluate.assert_called_once()
        eval_args = mock_page.evaluate.call_args[0][1]
        self.assertEqual(eval_args["author"], "KeeperKing")
        self.assertEqual(eval_args["thingId"], "t1_xyz789")
        self.assertEqual(eval_args["draftText"], "Hold Merrill for the elite contact floor.")
        self.assertTrue(eval_args["jsonUrl"].startswith("https://old.reddit.com/r/fantasybaseball/comments/abc123/nightly.json"))

    @patch("tools.reddit_fantasy_scanner.post_draft_reply_via_pixel")
    @patch("tools.reddit_fantasy_scanner.load_state")
    def test_main_post_draft_executes_live_reply_with_state_fallback(self, mock_load_state, mock_post_pixel):
        """Verify CLI --post-draft invokes post_draft_reply_via_pixel using CLI args or state fallback."""
        from tools.reddit_fantasy_scanner import main

        mock_load_state.return_value = {
            "last_candidate": {
                "author": "StateManager",
                "thread_url": "https://www.reddit.com/r/fantasybaseball/comments/state123/",
                "raw_id": "t1_statecomment",
            },
            "last_draft": "Fallback draft from state.",
        }
        mock_post_pixel.return_value = (
            True,
            "https://www.reddit.com/r/fantasybaseball/comments/state123/reply999/",
        )

        with patch.object(sys, "argv", ["reddit_fantasy_scanner.py", "--post-draft", "--author", "StateManager"]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)

        mock_post_pixel.assert_called_once_with(
            thread_url="https://www.reddit.com/r/fantasybaseball/comments/state123/",
            author="StateManager",
            draft_text="Fallback draft from state.",
            thing_id="t1_statecomment",
        )


if __name__ == "__main__":
    unittest.main()
