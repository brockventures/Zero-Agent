#!/usr/bin/env python3
"""
Unit test suite for tools/session_summarizer.py.
Validates clean_dialogue_content, extract_recent_dialogue, and summary formatting.
"""

import sys
import unittest
from pathlib import Path

WORKSPACE = Path("/workspace")
sys.path.insert(0, str(WORKSPACE))

from tools.session_summarizer import clean_dialogue_content


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


if __name__ == "__main__":
    unittest.main()
