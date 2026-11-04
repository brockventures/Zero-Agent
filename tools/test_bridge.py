"""Unit tests for bridge pipeline prompt formatting, human name clarity, and reply gating."""

import asyncio
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path = [p for p in sys.path if p != "/workspace"]
sys.path.insert(0, str(REPO_ROOT))

# Purge any previously imported tools modules from sys.modules
for mod_name in list(sys.modules.keys()):
    if mod_name == "tools" or mod_name.startswith("tools."):
        del sys.modules[mod_name]

from tools.bridge_pipeline import prepare_turn_prompt
from tools.bridge_formatting import format_for_discord
from tools.bridge_ambient import route_external_message


class TestBridgePromptAndFormatting(unittest.IsolatedAsyncioTestCase):

    def test_resumed_external_prompt_plain_names_guidance(self):
        """Verify resumed external turn prompt instructs using plain human names and peer bot snowflakes."""
        with patch("tools.bridge_state.get_channel_session_id", return_value="conv-test-1"), \
             patch("tools.bridge_state.get_reset_session_keys", return_value=set()), \
             patch("tools.bridge_stream.get_latest_transcript_step_index", return_value=2), \
             patch("tools.channel_history.format_channel_context", return_value=""):
            prompt = prepare_turn_prompt(
                prompt="Checking in on task status",
                mode="external",
                channel_id=1534452820995080192,
                sess_key="1534452820995080192",
                author_name="Ryan",
            )
            self.assertIn("CRAB CAVERN MULTI-AGENT COLLABORATION ENVIRONMENT — RESUMED TURN", prompt)
            self.assertIn("plain names (Mike, Dr. Coley, Alex, Ryan)", prompt)
            self.assertIn("reserve physical Discord snowflakes (<@ID>) strictly for waking peer bots (Amos, Marvin, Aerial)", prompt)

    def test_crab_cavern_prompt_file_rule_19(self):
        """Verify config/prompts/crab_cavern_prompt.md contains clarified Rule 19."""
        prompt_path = REPO_ROOT / "config" / "prompts" / "crab_cavern_prompt.md"
        if not prompt_path.exists():
            prompt_path = Path("/workspace/config/prompts/crab_cavern_prompt.md")
        self.assertTrue(prompt_path.exists())
        content = prompt_path.read_text()
        self.assertIn("19. PHYSICAL SNOWFLAKE ADDRESSING DISCIPLINE", content)
        self.assertIn("plain names (Mike, Dr. Coley, Alex, Ryan)", content)
        self.assertIn("waking peer bots (Amos, Marvin, Aerial)", content)

    def test_midsentence_snowflake_unwrapping(self):
        """Verify mid-sentence backticked mentions are unwrapped to live snowflakes."""
        text = "Hey `<@1468012353206354197>` what do you think about `<#1534436119888793750>`?"
        cleaned = format_for_discord(text)
        self.assertIn("<@1468012353206354197>", cleaned)
        self.assertNotIn("`<@1468012353206354197>`", cleaned)
        self.assertIn("<#1534436119888793750>", cleaned)
        self.assertNotIn("`<#1534436119888793750>`", cleaned)

    def test_strip_trailing_streaming_cursor_glyphs(self):
        """Verify trailing Unicode block-cursor glyphs (▋ U+258B, █ U+2588, etc.) are stripped while preserving code spans."""
        # 1. Trailing cursor at the end of a message
        self.assertEqual(
            format_for_discord("Deployment completed cleanly. ▋"),
            "Deployment completed cleanly.",
        )
        self.assertEqual(
            format_for_discord("All checks passed!▋\n"),
            "All checks passed!",
        )
        # 2. Trailing cursor immediately before [CHOICES: ...]
        self.assertEqual(
            format_for_discord("Ready to deploy. ▋\n\n[CHOICES: Deploy | Cancel]"),
            "Ready to deploy.\n\n[CHOICES: Deploy | Cancel]",
        )
        # 3. Intentional block glyphs inside inline code and fenced code blocks are preserved
        code_msg = "Progress bar: `[████████▋  ] 85%` and inline `▋`:\n```\n████▋\n```▋"
        formatted_code = format_for_discord(code_msg)
        self.assertIn("`[████████▋  ] 85%`", formatted_code)
        self.assertIn("`▋`", formatted_code)
        self.assertIn("```\n████▋\n```", formatted_code)
        self.assertFalse(formatted_code.endswith("▋"))

    async def test_route_external_message_reply_to_other_ignored(self):
        """Verify inline replies to other users return True immediately without chiming in."""
        msg = MagicMock()
        msg.author.bot = False
        msg.author.id = 179407724335988736
        msg.channel.id = 1534452820995080192
        msg.reference = MagicMock()
        msg.reference.message_id = 999111
        msg.reference.resolved = None
        msg.mentions = []
        msg.role_mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633

        home_q = asyncio.Queue()
        ext_q = asyncio.Queue()

        with patch("tools.bridge_ambient.resolve_is_reply_to_zero", return_value=False):
            handled = await route_external_message(
                msg=msg,
                bot=bot,
                content="Sounds good, let's proceed.",
                author_name="Ryan",
                home_turn_queue=home_q,
                ext_turn_queue=ext_q,
                rules={"ambient_classifier_enabled": True},
            )
            self.assertTrue(handled)
            self.assertTrue(ext_q.empty())


if __name__ == "__main__":
    unittest.main()
