#!/usr/bin/env python3
"""Regression test suite for all regex fixes across tools, sidecars, and skills."""

import ast
import re
import sys
import unittest
from pathlib import Path

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

from tools.classifier import extract_classifier_score
from tools.bridge_formatting import format_for_discord
from tools.email_sanitizer import sanitize_email_text, sanitize_discord_display
from tools.zero_mail_listener import sanitize_text as zero_sanitize_text
from tools.core_friends_reminder import find_partner
import tools.sidecar_audit as sa


class TestRegexFixes(unittest.TestCase):

    def test_1_classifier_score_unanchored_tokens(self):
        """Verify token numbers like 1500 tokens do not fake 1.0 scores."""
        raw_stdout = (
            "Loaded model gemini-3.8-flash-low (1500 tokens).\n"
            "Score: 0.25"
        )
        score = extract_classifier_score(raw_stdout)
        self.assertEqual(score, 0.25)

        # Standalone floats
        self.assertEqual(extract_classifier_score("0.85"), 0.85)
        self.assertEqual(extract_classifier_score("0.0"), 0.0)
        self.assertEqual(extract_classifier_score("1.0"), 1.0)
        self.assertEqual(extract_classifier_score("1"), 1.0)
        self.assertEqual(extract_classifier_score("Relevance score: 0.70"), 0.70)
        self.assertIsNone(extract_classifier_score("Random error with 500 status"))

    def test_2_bridge_task_envelope_lookahead_no_swallow(self):
        """Verify task envelope stripping does not gobble lowercase user output."""
        raw_output = (
            "An asynchronous task has completed: task-123 (State: DONE)\n"
            "Result payload: 42\nTask output: [Success]\n\n"
            "here is lowercase user content that must never be deleted."
        )
        cleaned = format_for_discord(raw_output)
        self.assertNotIn("An asynchronous task has completed", cleaned)
        self.assertIn("here is lowercase user content that must never be deleted.", cleaned)

    def test_3_session_summarizer_generic_types_preservation(self):
        """Verify session summarizer does not strip C++/Rust/Java generic types."""
        tag_pat = re.compile(
            r"</?(?:USER_REQUEST|SYSTEM_MESSAGE|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM|TASK_OUTPUT)(?:\s+[^>]*)?>"
        )
        sample = (
            "<USER_REQUEST>\n"
            "Implement Vector<T> and Map<KEY, VALUE> with List<ITEM> and <NULL> check.\n"
            "</USER_REQUEST>"
        )
        cleaned = tag_pat.sub("", sample).strip()
        self.assertNotIn("<USER_REQUEST>", cleaned)
        self.assertNotIn("</USER_REQUEST>", cleaned)
        self.assertIn("Vector<T>", cleaned)
        self.assertIn("Map<KEY, VALUE>", cleaned)
        self.assertIn("List<ITEM>", cleaned)
        self.assertIn("<NULL>", cleaned)

    def test_4_workspace_mcp_email_keyed_extraction(self):
        """Verify user_ryan.md email extraction prioritizes primary email keys over vendor emails."""
        from tools.workspace_mcp import _get_email_defaults
        sender, notify = _get_email_defaults()
        self.assertTrue(notify)
        self.assertIn("ryan", notify.lower())
        self.assertIn("@", notify)

    def test_5_html_sanitizer_math_and_shell_preservation(self):
        """Verify mathematical inequalities and shell redirects are preserved while HTML is stripped."""
        math_text = "Formula: x < 5 and y > 10."
        self.assertIn("x < 5 and y > 10", sanitize_email_text(math_text))
        self.assertIn("x < 5 and y > 10", zero_sanitize_text(math_text))

        shell_text = "Execute: cat < input.txt > output.txt"
        self.assertIn("cat < input.txt > output.txt", sanitize_email_text(shell_text))
        self.assertIn("cat < input.txt > output.txt", zero_sanitize_text(shell_text))

        html_text = "<p>Real message <!-- comment --> <script>evil()</script></p>"
        clean = sanitize_email_text(html_text)
        self.assertIn("Real message", clean)
        self.assertNotIn("evil()", clean)
        self.assertNotIn("comment", clean)

    def test_6_bridge_role_mention_preservation(self):
        """Verify user role mentions are preserved while bot trigger roles are stripped."""
        bot_id = "1542285964213358633"
        target_role_ids = {"1543462881624858624", "1542294519914037341"}

        user_content = "Hey Zero, check permissions for <@&987654321012345678>."
        cleaned = re.sub(rf"<@!?{bot_id}>", "", user_content)
        for rid in target_role_ids:
            cleaned = re.sub(rf"<@&{rid}>", "", cleaned)
        cleaned = re.sub(r"^(hey\s+)?zero[:,\s]*", "", cleaned, flags=re.IGNORECASE).strip()

        self.assertIn("<@&987654321012345678>", cleaned)

        # Bot trigger role ping
        bot_content = "<@&1543462881624858624> run tests"
        cleaned_bot = re.sub(rf"<@!?{bot_id}>", "", bot_content)
        for rid in target_role_ids:
            cleaned_bot = re.sub(rf"<@&{rid}>", "", cleaned_bot)
        self.assertEqual(cleaned_bot.strip(), "run tests")

    def test_7_sidecar_audit_ast(self):
        """Verify AST parsing reliably finds all sidecars and triggers."""
        actions = sa.get_sidecars_py_actions()
        self.assertIn("heartbeat", actions)
        self.assertIn("triage", actions)
        self.assertIn("nas_logs", actions)
        self.assertIn("plex", actions)
        self.assertIn("ev9", actions)
        self.assertGreaterEqual(len(actions), 20)

        triggers = sa.get_bridge_triggers()
        self.assertIn("heartbeat", triggers)
        self.assertIn("triage", triggers)
        self.assertIn("logs", triggers)
        self.assertGreaterEqual(len(triggers), 15)

    def test_8_core_friends_partner_business_disqualification(self):
        """Verify business partner / firm phrases are rejected as romantic partners."""
        contacts = [
            {"Name": "Alice Smith", "Notes & Connections": "Business partner Bob Jones at Startup.", "Physical Address": "10 Main St"},
            {"Name": "Bob Jones", "Notes & Connections": "Startup co-founder.", "Physical Address": "10 Main St"},
            {"Name": "Charlie Brown", "Notes & Connections": "Managing partner Dave Miller at Law Firm.", "Physical Address": ""},
            {"Name": "Dave Miller", "Notes & Connections": "Lawyer.", "Physical Address": ""},
            {"Name": "Eva Green", "Notes & Connections": "Partner to Frank Castle.", "Physical Address": ""},
            {"Name": "Frank Castle", "Notes & Connections": "Partner Eva Green.", "Physical Address": ""},
        ]
        # Business partner should be None
        self.assertIsNone(find_partner(contacts[0], contacts))
        # Managing partner should be None
        self.assertIsNone(find_partner(contacts[2], contacts))
        # Romantic partner should match
        eva_partner = find_partner(contacts[4], contacts)
        self.assertIsNotNone(eva_partner)
        self.assertEqual(eva_partner["Name"], "Frank Castle")


if __name__ == "__main__":
    unittest.main()
