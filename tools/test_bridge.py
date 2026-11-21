"""Unit tests for bridge pipeline prompt formatting, human name clarity, and reply gating."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
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


class TestBridgeSlashAndButtonAuth(unittest.IsolatedAsyncioTestCase):

    def _make_interaction(self, user_id: int, channel_id: int, custom_id: str = ""):
        import discord
        interaction = MagicMock()
        interaction.id = 42424242
        interaction.type = discord.InteractionType.component
        interaction.user = MagicMock()
        interaction.user.id = user_id
        interaction.user.name = "Tester"
        interaction.user.display_name = "Tester"
        interaction.channel_id = channel_id
        interaction.channel = MagicMock()
        interaction.channel.id = channel_id
        interaction.channel.category_id = None
        interaction.channel.parent_id = None
        interaction.channel.parent = None
        interaction.channel.send = AsyncMock()
        interaction.data = {"custom_id": custom_id}
        interaction.response = MagicMock()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.send_message = AsyncMock()
        interaction.response.send_modal = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()
        return interaction

    async def test_all_slash_commands_reject_non_owner(self):
        """Verify every slash command in tools/bridge.py rejects non-owner callers ephemerally before defer()."""
        import tools.bridge as bridge

        non_owner_id = 999888777666
        home_ch = bridge.TARGET_CHANNEL_ID
        invocations = [
            (bridge.slash_new, ("hello",)),
            (bridge.slash_reset, ()),
            (bridge.slash_model, ("argon",)),
            (bridge.slash_quota, ("card",)),
            (bridge.slash_reload, ("in-place",)),
            (bridge.slash_crab, ("status", "", 3)),
            (bridge.slash_run, ("heartbeat",)),
            (bridge.slash_logs, ("24h",)),
            (bridge.slash_triage, ()),
            (bridge.slash_heartbeat, ()),
            (bridge.slash_tasks, ()),
            (bridge.slash_sidecars, ()),
            (bridge.slash_title, ("New Thread Title",)),
            (bridge.slash_queue, ()),
            (bridge.slash_voice, ("status", "")),
        ]
        # Verify our test covers every registered slash command on bot.tree
        registered_names = {cmd.name for cmd in bridge.bot.tree.get_commands()}
        tested_names = {cmd.name for cmd, _ in invocations}
        self.assertEqual(registered_names, tested_names)

        for cmd, args in invocations:
            interaction = self._make_interaction(user_id=non_owner_id, channel_id=home_ch)
            await cmd.callback(interaction, *args)
            interaction.response.defer.assert_not_awaited()
            interaction.response.send_message.assert_awaited_once()
            _, kwargs = interaction.response.send_message.call_args
            self.assertTrue(kwargs.get("ephemeral"), f"/{cmd.name} denial must be ephemeral")

    async def test_home_only_slash_commands_reject_non_home_channel_for_owner(self):
        """Verify Home-only slash commands (/triage, /run, /logs, /tasks, /quota, /sidecars, /heartbeat, /voice) reject non-home channels even for OWNER_USER_ID."""
        import tools.bridge as bridge

        non_home_ch = 1534452820995080192  # #lounge (Crab Cavern)
        home_only_invocations = [
            (bridge.slash_triage, ()),
            (bridge.slash_run, ("heartbeat",)),
            (bridge.slash_logs, ("24h",)),
            (bridge.slash_tasks, ()),
            (bridge.slash_quota, ("card",)),
            (bridge.slash_sidecars, ()),
            (bridge.slash_heartbeat, ()),
            (bridge.slash_voice, ("status", "")),
        ]
        for cmd, args in home_only_invocations:
            interaction = self._make_interaction(user_id=bridge.OWNER_USER_ID, channel_id=non_home_ch)
            await cmd.callback(interaction, *args)
            interaction.response.defer.assert_not_awaited()
            interaction.response.send_message.assert_awaited_once()
            msg_text = interaction.response.send_message.call_args[0][0]
            self.assertIn("Home", msg_text)
            self.assertTrue(interaction.response.send_message.call_args[1].get("ephemeral"))

    async def test_on_button_interaction_non_owner_forge_edit_no_modal_or_db_read(self):
        """Verify non-owner clicking forge_edit: gets ephemeral denial without reading payload_json or opening ForgeDecisionEditModal."""
        import tools.bridge as bridge

        interaction = self._make_interaction(
            user_id=999888777,
            channel_id=bridge.TARGET_CHANNEL_ID,
            custom_id="forge_edit:dec-1234",
        )
        with patch("tools.forge.get_db_connection") as mock_db, \
             patch("tools.forge.ForgeDecisionEditModal") as mock_modal:
            await bridge.on_button_interaction(interaction)
            mock_db.assert_not_called()
            mock_modal.assert_not_called()
            interaction.response.send_modal.assert_not_awaited()
            interaction.message.edit.assert_not_awaited()
            interaction.response.send_message.assert_awaited_once()
            self.assertTrue(interaction.response.send_message.call_args[1].get("ephemeral"))

    async def test_on_button_interaction_non_owner_forge_approve_dismiss_choice_no_mutation(self):
        """Verify non-owner clicking forge_approve:, forge_dismiss:, or choice: cannot disable buttons, edit embeds, or execute actions."""
        import tools.bridge as bridge

        for cid in ("forge_approve:dec-55", "forge_dismiss:dec-55", "choice:Restart Zero"):
            interaction = self._make_interaction(
                user_id=999888777,
                channel_id=bridge.TARGET_CHANNEL_ID,
                custom_id=cid,
            )
            with patch("tools.forge.execute_decision") as mock_exec, \
                 patch("tools.forge.dismiss_decision") as mock_dismiss, \
                 patch("tools.bridge._button_choice_callback", new_callable=AsyncMock) as mock_choice:
                await bridge.on_button_interaction(interaction)
                mock_exec.assert_not_called()
                mock_dismiss.assert_not_called()
                mock_choice.assert_not_awaited()
                interaction.response.defer.assert_not_awaited()
                interaction.message.edit.assert_not_awaited()
                interaction.response.send_message.assert_awaited_once()
                self.assertTrue(interaction.response.send_message.call_args[1].get("ephemeral"))


class TestBridgeSignalHygiene(unittest.TestCase):

    def test_no_global_sigchld_handler_registered(self):
        """Verify tools/bridge.py does not install a global SIGCHLD reaper that races subprocess exit codes."""
        import signal
        import subprocess
        import tools.bridge as bridge

        self.assertFalse(hasattr(bridge, "_handle_sigchld"))
        bridge_src = (REPO_ROOT / "tools" / "bridge.py").read_text(encoding="utf-8")
        self.assertNotIn("SIGCHLD", bridge_src)
        self.assertEqual(signal.getsignal(signal.SIGCHLD), signal.SIG_DFL)

        # Child non-zero exit codes must be preserved without ECHILD coercion
        res = subprocess.run(
            [sys.executable, "-c", "import sys; sys.exit(42)"],
            timeout=10,
            check=False,
        )
        self.assertEqual(res.returncode, 42)


if __name__ == "__main__":
    unittest.main()
