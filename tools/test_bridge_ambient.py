# ruff: noqa: F401, I001
"""
Unit tests for tools/bridge_ambient.py.
Exercises Brock guild public gating, Crab Cavern ambient routing, tag gating, and Banana Watcher directives.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
if _REPO_ROOT in sys.path:
    sys.path.remove(_REPO_ROOT)
sys.path.insert(0, _REPO_ROOT)
from tools.bridge_ambient import (
    BANANA_WATCHER_BOT_ID,
    route_external_message,
    should_suppress_ambient_reply,
    warm_channel_history,
)
from tools.bridge_state import OWNER_USER_ID
from datetime import UTC


class TestBridgeAmbient(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.silence_patcher = patch(
            "tools.bridge_state.is_crab_cavern_silenced", return_value=(False, "")
        )
        self.silence_patcher.start()
        self.recent_msgs_patcher = patch(
            "tools.channel_history.get_recent_messages", return_value=[]
        )
        self.recent_msgs_patcher.start()

    def tearDown(self):
        self.recent_msgs_patcher.stop()
        self.silence_patcher.stop()
        super().tearDown()

    async def test_brock_guild_public_channel_unauthorized(self):
        msg = MagicMock()
        msg.guild.id = 1210466877835313152  # Brock Guild
        msg.author.id = 999999999  # Not owner
        msg.author.bot = False
        msg.channel.id = 1453427860793463000  # #seerr-requests-and-chat
        msg.content = "@Zero check this"

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        with patch("tools.bridge_ambient.is_brock_guild", return_value=True):
            handled = await route_external_message(
                msg, bot, "@Zero check this", "GuestUser", home_queue, ext_queue
            )
            self.assertTrue(handled)
            self.assertTrue(home_queue.empty())
            self.assertTrue(ext_queue.empty())

    async def test_brock_guild_public_channel_authorized(self):
        msg = MagicMock()
        msg.guild.id = 1210466877835313152  # Brock Guild
        msg.author.id = OWNER_USER_ID  # Ryan Brock
        msg.author.bot = False
        msg.channel.id = 1453427860793463000  # #seerr-requests-and-chat
        msg.channel.name = "seerr-requests-and-chat"
        msg.content = "<@1542285964213358633> what is the status of Severance?"
        msg.attachments = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=True),
            patch("tools.channel_history.format_channel_context", return_value=""),
        ):
            handled = await route_external_message(
                msg, bot, msg.content, "Ryan", home_queue, ext_queue
            )
            self.assertTrue(handled)
            self.assertFalse(home_queue.empty())
            item = await home_queue.get()
            self.assertIn("BROCK DISCORD PUBLIC CHANNEL", item["prompt"])
            self.assertIn("what is the status of Severance?", item["prompt"])

    async def test_banana_watcher_concluded_summary_directive(self):
        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = BANANA_WATCHER_BOT_ID
        msg.author.bot = True
        msg.channel.id = 1534436119888793750  # #the-banana-stand
        msg.channel.name = "the-banana-stand"
        msg.content = "🍌 **Discussion Concluded**: Topic `orbital-routing` has reached resolution. <@1542285964213358633> dispatch summary to lounge."
        msg.attachments = []
        msg.reference = None

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
            handled = await route_external_message(
                msg, bot, msg.content, "Banana Watcher", home_queue, ext_queue
            )
            self.assertTrue(handled)
            self.assertFalse(ext_queue.empty())
            item = await ext_queue.get()
            self.assertIn(
                "RULE 7 CONCLUDED DISCUSSION EXECUTIVE SUMMARY", item["prompt"]
            )
            self.assertIn("orbital-routing", item["prompt"])

    async def test_ambient_classifier_below_threshold_buffers_silently(self):
        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = 11111111
        msg.author.bot = False
        msg.channel.id = 1534452820995080192  # #lounge
        msg.channel.name = "lounge"
        msg.content = "What was the score of the baseball game yesterday?"
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        rules = {
            "ambient_classifier_enabled": True,
            "ambient_relevance_threshold": 0.80,
            "channel_tag_requirements": {},
        }

        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch(
                "tools.classifier.score_relevance_with_reaction",
                return_value=(0.25, None),
            ),
        ):
            handled = await route_external_message(
                msg, bot, msg.content, "Ian", home_queue, ext_queue, rules=rules
            )
            self.assertTrue(handled)
            # Below 0.80 -> buffered silently, queue remains empty
            self.assertTrue(ext_queue.empty())

    async def test_ambient_classifier_applies_lightweight_reaction(self):
        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = 11111111
        msg.author.bot = False
        msg.channel.id = 1534452820995080192  # #lounge
        msg.channel.name = "lounge"
        msg.content = "That container crash log is pure comedy gold"
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []
        msg.add_reaction = AsyncMock()

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        rules = {
            "ambient_classifier_enabled": True,
            "ambient_relevance_threshold": 0.80,
            "channel_tag_requirements": {},
        }

        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch(
                "tools.classifier.score_relevance_with_reaction",
                return_value=(0.30, "😂"),
            ),
        ):
            handled = await route_external_message(
                msg, bot, msg.content, "Ian", home_queue, ext_queue, rules=rules
            )
            self.assertTrue(handled)
            self.assertTrue(ext_queue.empty())
            msg.add_reaction.assert_awaited_once_with("😂")

    async def test_bare_zero_text_in_tag_gated_channel(self):
        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = 179407724335988736
        msg.author.bot = False
        msg.channel.id = 1551465050072416286  # #side-project
        msg.channel.name = "side-project"
        msg.content = (
            "Zero, can you update so you respond to vocative Zero text as well?"
        )
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        rules = {
            "channel_tag_requirements": {
                "1551465050072416286": ["1543285916506783799", "1543462881624858624"]
            },
            "ambient_classifier_enabled": False,
        }

        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch("tools.channel_history.format_channel_context", return_value=""),
        ):
            handled = await route_external_message(
                msg, bot, msg.content, "Ryan", home_queue, ext_queue, rules=rules
            )
            self.assertTrue(handled)
            self.assertFalse(ext_queue.empty())
            item = await ext_queue.get()
            self.assertIn(
                "can you update so you respond to vocative Zero text as well?",
                item["prompt"],
            )

    async def test_external_group_mid_turn_steering(self):
        """Verify mid-turn steering in shared/group channels formats prompt, signals SIGINT, and enqueues steer payload."""
        import signal
        from tools.bridge_runner import (
            channel_active_procs,
            channel_active_prompts,
            steering_channels,
        )

        channel_id = 1534452820995080192  # #lounge
        mock_proc = MagicMock()
        mock_proc.returncode = None
        channel_active_procs[channel_id] = mock_proc
        channel_active_prompts[channel_id] = "Drafting breakdown of Reddit skill"

        msg = MagicMock()
        msg.guild.id = 1534436119888793748
        msg.author.id = 1468012353206354197
        msg.author.name = "Amos"
        msg.author.display_name = "Amos"
        msg.author.bot = True
        msg.channel.id = channel_id
        msg.channel.name = "lounge"
        msg.content = (
            "<@1542285964213358633>, how do you read Reddit? old.reddit.com gives 403."
        )
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        bot.mentions = [bot.user]
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        try:
            with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
                handled = await route_external_message(
                    msg, bot, msg.content, "Amos", home_queue, ext_queue
                )
                self.assertTrue(handled)
                mock_proc.send_signal.assert_called_once_with(signal.SIGINT)
                self.assertIn(channel_id, steering_channels)
                self.assertFalse(ext_queue.empty())
                item = await ext_queue.get()
                self.assertTrue(item.get("is_steer"))
                self.assertEqual(item.get("channel_id"), channel_id)
                self.assertIn("[MID-TURN GROUP CONVERSATION UPDATE]", item["prompt"])
                self.assertIn("Drafting breakdown of Reddit skill", item["prompt"])
                self.assertIn("how do you read Reddit?", item["prompt"])
        finally:
            channel_active_procs.pop(channel_id, None)
            channel_active_prompts.pop(channel_id, None)
            steering_channels.discard(channel_id)

    async def test_crab_cavern_silenced_drops_messages(self):
        """Verify that when Crab Cavern is silenced, any message arriving from outside Brock guild is dropped immediately."""
        self.silence_patcher.stop()  # Stop default un-silenced patcher
        try:
            with patch(
                "tools.bridge_state.is_crab_cavern_silenced",
                return_value=(True, "Silenced until Tuesday 8 PM PT"),
            ):
                msg = MagicMock()
                msg.guild.id = 1534436119888793740
                msg.author.id = 1468012353206354197
                msg.author.bot = True
                msg.channel.id = 1534436119888793750  # #the-banana-stand
                msg.channel.name = "the-banana-stand"
                msg.content = "<@1542285964213358633> urgent task"
                msg.attachments = []
                msg.reference = None

                bot = MagicMock()
                bot.user.id = 1542285964213358633
                home_queue = asyncio.Queue()
                ext_queue = asyncio.Queue()

                with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
                    handled = await route_external_message(
                        msg, bot, msg.content, "Amos", home_queue, ext_queue
                    )
                    self.assertTrue(handled)
                    self.assertTrue(home_queue.empty())
                    self.assertTrue(ext_queue.empty())
        finally:
            self.silence_patcher.start()

    async def test_reply_to_message_with_attachment_downloads_parent_attachment(self):
        """Verify replying to an older message that has an image attachment automatically downloads and passes the parent attachment."""
        parent_att = MagicMock()
        parent_att.filename = "haos_thermals.jpg"
        parent_att.save = AsyncMock()

        parent_msg = MagicMock()
        parent_msg.id = 1565093592026841219
        parent_msg.attachments = [parent_att]

        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = 169260920550195200
        msg.author.bot = False
        msg.channel.id = 1534452820995080192  # #lounge
        msg.channel.name = "lounge"
        msg.content = "Hmmm <@1542285964213358633> how are your temps if you're on a similar tier CPU"
        msg.attachments = []
        msg.reference = MagicMock()
        msg.reference.message_id = parent_msg.id
        msg.reference.resolved = None
        msg.channel.fetch_message = AsyncMock(return_value=parent_msg)
        msg.role_mentions = []
        msg.mentions = []

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
            handled = await route_external_message(
                msg, bot, msg.content, "Arcane", home_queue, ext_queue
            )
            self.assertTrue(handled)
            msg.channel.fetch_message.assert_awaited_once_with(parent_msg.id)
            parent_att.save.assert_awaited_once()
            self.assertFalse(ext_queue.empty())
            item = await ext_queue.get()
            self.assertEqual(len(item["attachments"]), 1)
            self.assertTrue(item["attachments"][0].endswith("_ref_haos_thermals.jpg"))
            self.assertIn("[CRITICAL IMAGE INPUT INVARIANT]:", item["prompt"])

    async def test_external_admin_commands_reject_non_owner_and_bots(self):
        """Verify non-owner humans and peer bots cannot execute !reset, !pause, !unpause, !halt, or !resume in external channels."""
        from tools.bridge_ambient import channel_last_bot_reply

        bot = MagicMock()
        bot.user.id = 1542285964213358633

        cases = [
            ("!reset", False, 111222333, "ExternalUser"),
            ("!pause Amos 10", False, 111222333, "ExternalUser"),
            ("!unpause Amos", False, 111222333, "ExternalUser"),
            ("!halt", False, 111222333, "ExternalUser"),
            ("!resume", False, 111222333, "ExternalUser"),
            ("!halt trading right now please", True, 1468012353206354197, "PeerBot"),
            ("!pause Amos for 10 minutes", True, OWNER_USER_ID, "SpoofedBotFlag"),
        ]

        for content, is_bot, author_id, author_name in cases:
            channel_last_bot_reply.clear()
            msg = MagicMock()
            msg.guild.id = 1534436119888793740
            msg.author.id = author_id
            msg.author.bot = is_bot
            msg.channel.id = 1534452820995080192  # #lounge
            msg.channel.name = "lounge"
            msg.content = content
            msg.attachments = []
            msg.reference = None
            msg.role_mentions = []
            msg.mentions = []
            msg.reply = AsyncMock()

            home_queue = asyncio.Queue()
            ext_queue = asyncio.Queue()

            with (
                patch("tools.bridge_ambient.is_brock_guild", return_value=False),
                patch(
                    "tools.bridge_ambient.resolve_is_reply_to_zero", return_value=True
                ),
                patch("tools.bridge_ambient.clear_channel_session_id") as mock_clear,
                patch("tools.last_word_protocol.pause_bot") as mock_pause,
                patch("tools.last_word_protocol.unpause_bot") as mock_unpause,
                patch("tools.agora_kill_switch.trigger_kill_switch") as mock_kill,
            ):
                handled = await route_external_message(
                    msg, bot, content, author_name, home_queue, ext_queue
                )
                self.assertTrue(handled)
                self.assertTrue(ext_queue.empty())
                mock_clear.assert_not_called()
                mock_pause.assert_not_called()
                mock_unpause.assert_not_called()
                mock_kill.assert_not_called()
                msg.reply.assert_awaited_once()
                self.assertIn("restricted to the bot owner", msg.reply.call_args[0][0])

    async def test_external_admin_commands_allow_owner(self):
        """Verify OWNER_USER_ID (non-bot) can still execute !reset, !pause, and !halt in external channels."""
        bot = MagicMock()
        bot.user.id = 1542285964213358633

        msg = MagicMock()
        msg.guild.id = 1534436119888793740
        msg.author.id = OWNER_USER_ID
        msg.author.bot = False
        msg.channel.id = 1534452820995080192
        msg.channel.name = "lounge"
        msg.attachments = []
        msg.reference = None
        msg.role_mentions = []
        msg.mentions = []
        msg.reply = AsyncMock()

        home_queue = asyncio.Queue()
        ext_queue = asyncio.Queue()

        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch("tools.bridge_ambient.resolve_is_reply_to_zero", return_value=True),
            patch("tools.bridge_ambient.clear_channel_session_id") as mock_clear,
        ):
            handled = await route_external_message(
                msg, bot, "!reset", "Owner", home_queue, ext_queue
            )
            self.assertTrue(handled)
            mock_clear.assert_called_once_with(msg.channel.id, "external")

        msg.reply.reset_mock()
        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch("tools.bridge_ambient.resolve_is_reply_to_zero", return_value=True),
            patch(
                "tools.agora_kill_switch.trigger_kill_switch",
                return_value={"message": "🛑 Halted"},
            ) as mock_kill,
        ):
            handled = await route_external_message(
                msg, bot, "!halt", "Owner", home_queue, ext_queue
            )
            self.assertTrue(handled)
            mock_kill.assert_called_once_with(
                initiator="Owner", action="halt", channel_id=msg.channel.id
            )
            msg.reply.assert_awaited_once_with("🛑 Halted")

    async def test_warm_classifier_stderr_devnull_fd_closure_and_cold_stdin_devnull(
        self,
    ):
        """Verify WarmClassifierDaemon uses stderr=DEVNULL, closes all FDs on shutdown, and cold fallback passes stdin=DEVNULL."""
        import subprocess
        import tempfile
        from tools.classifier import WarmClassifierDaemon, score_relevance_with_reaction

        with tempfile.TemporaryDirectory() as tmpdir:
            daemon = WarmClassifierDaemon(work_dir=tmpdir, max_turns=4)
            mock_proc = MagicMock()
            mock_proc.pid = 42424
            mock_proc.poll.return_value = None
            mock_proc.stdin = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.stderr = MagicMock()
            mock_proc.stdin.close.side_effect = OSError("already closed")

            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                daemon.start()
                mock_popen.assert_called_once()
                self.assertEqual(
                    mock_popen.call_args.kwargs.get("stderr"), subprocess.DEVNULL
                )

                daemon.shutdown()
                mock_proc.stdin.close.assert_called_once()
                mock_proc.stdout.close.assert_called_once()
                mock_proc.stderr.close.assert_called_once()

        with patch("subprocess.run") as mock_run:
            res = MagicMock()
            res.stdout = "0.42 | REACT: 🦀"
            mock_run.return_value = res
            score, rx = score_relevance_with_reaction(
                "Checking how the docker bridge handles reconnects here",
                author="Ian",
                use_warm=False,
            )
            self.assertEqual(score, 0.42)
            self.assertEqual(rx, "🦀")
            self.assertEqual(mock_run.call_args.kwargs.get("stdin"), subprocess.DEVNULL)

    async def test_concurrent_ambient_reaction_isolation(self):
        """Verify concurrent ambient channels never steal or overwrite each other's reactions."""
        import threading

        barrier = threading.Barrier(2, timeout=5.0)

        def fake_run(cmd, **kwargs):
            prompt = cmd[-1]
            barrier.wait()
            res = MagicMock()
            if "channel_alpha_payload" in prompt:
                res.stdout = "0.30 | REACT: 🔥"
            else:
                res.stdout = "0.25 | REACT: 🦀"
            return res

        def make_msg(ch_id: int, content: str):
            m = MagicMock()
            m.id = ch_id + 1
            m.guild.id = 1534436119888793740
            m.author.id = 11111111
            m.author.bot = False
            m.channel.id = ch_id
            m.channel.name = f"ch-{ch_id}"
            m.content = content
            m.attachments = []
            m.reference = None
            m.role_mentions = []
            m.mentions = []
            m.add_reaction = AsyncMock()
            return m

        msg_a = make_msg(
            1534452820995080192, "Thoughts on channel_alpha_payload performance today"
        )
        msg_b = make_msg(
            1534452820995080193, "Thoughts on channel_beta_payload reliability today"
        )
        bot = MagicMock()
        bot.user.id = 1542285964213358633
        rules = {
            "ambient_classifier_enabled": True,
            "ambient_relevance_threshold": 0.80,
            "channel_tag_requirements": {},
        }

        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch("tools.classifier._should_use_warm_classifier", return_value=False),
            patch("subprocess.run", side_effect=fake_run),
        ):
            await asyncio.gather(
                route_external_message(
                    msg_a,
                    bot,
                    msg_a.content,
                    "Ian",
                    asyncio.Queue(),
                    asyncio.Queue(),
                    rules=rules,
                ),
                route_external_message(
                    msg_b,
                    bot,
                    msg_b.content,
                    "Alex",
                    asyncio.Queue(),
                    asyncio.Queue(),
                    rules=rules,
                ),
            )

        msg_a.add_reaction.assert_awaited_once_with("🔥")
        msg_b.add_reaction.assert_awaited_once_with("🦀")

    async def test_vocative_regex_code_blocks_and_prose_non_triggering(self):
        """Verify fenced code blocks, mid-sentence zero commas, and prose starters like 'Zero tests failed.' do not trigger."""
        from tools.classifier import (
            contains_zero_mention,
            score_relevance_with_reaction,
        )

        non_mentions = [
            "Zero tests failed.",
            "Zero rows returned.",
            "if count hits zero, retry",
            "return zero, err",
            "value is zero: fallback to default",
            "```python\n@zero\nZero: run tests\nhey zero\n```",
            "~~~bash\n@zero check logs\n~~~",
            '```handoff\n{"to": "amos", "subject": "@zero"}\n```\nAll done.',
        ]
        for text in non_mentions:
            self.assertFalse(
                contains_zero_mention(text),
                f"False positive vocative match on: {text!r}",
            )

        valid_mentions = [
            "Zero: please check the logs",
            "Zero, please check the logs",
            "All green. Zero: deploy when ready",
            "hey zero, can you take a look?",
            "@zero check this build",
        ]
        for text in valid_mentions:
            self.assertTrue(
                contains_zero_mention(text), f"Expected vocative match on: {text!r}"
            )

        # Verify route_external_message does not enqueue on prose false positive or code block @zero
        bot = MagicMock()
        bot.user.id = 1542285964213358633
        rules = {"ambient_classifier_enabled": False, "channel_tag_requirements": {}}

        for text in (
            "Zero tests failed.",
            "if count hits zero, retry",
            "```python\n@zero\nZero: wake up\n```",
        ):
            msg = MagicMock()
            msg.id = 7777
            msg.guild.id = 1534436119888793740
            msg.author.id = 11111111
            msg.author.bot = False
            msg.channel.id = 1534452820995080192
            msg.channel.name = "lounge"
            msg.content = text
            msg.attachments = []
            msg.reference = None
            msg.role_mentions = []
            msg.mentions = []
            ext_queue = asyncio.Queue()
            with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
                handled = await route_external_message(
                    msg, bot, text, "Ian", asyncio.Queue(), ext_queue, rules=rules
                )
                self.assertTrue(handled)
                self.assertTrue(
                    ext_queue.empty(),
                    f"Should not enqueue on non-vocative text: {text!r}",
                )

        # Also verify score_relevance_with_reaction does not fast-path 1.0 on 'Zero tests failed.'
        with patch("subprocess.run") as mock_run:
            res = MagicMock()
            res.stdout = "0.10"
            mock_run.return_value = res
            score, rx = score_relevance_with_reaction(
                "Zero tests failed.", author="Ian", use_warm=False
            )
            self.assertEqual(score, 0.0)  # < 4 words non-directive drop, never 1.0
            self.assertIsNone(rx)
            mock_run.assert_not_called()

    async def test_handoff_and_peer_precedence_over_vocative_and_snowflake_override(
        self,
    ):
        """Verify floor:closed, to:other, and peer vocatives drop before Zero vocatives, while <@bot_id> overrides."""
        from tools.classifier import score_relevance_with_reaction

        closed_with_vocative = 'Zero: FYI we wrapped this up.\n```handoff\n{"floor": "closed", "reply": "none"}\n```'
        peer_env_with_vocative = '@zero mentioned in changelog.\n```handoff\n{"to": "amos", "floor": "open", "reply": "required"}\n```'
        peer_vocative_with_zero = "Hey Amos, ask zero about that migration later"
        closed_with_snowflake = '<@1542285964213358633> override needed.\n```handoff\n{"to": "amos", "floor": "closed", "reply": "none"}\n```'

        # 1. Classifier-level precedence
        with patch("subprocess.run") as mock_run:
            self.assertEqual(
                score_relevance_with_reaction(closed_with_vocative, use_warm=False),
                (0.0, None),
            )
            self.assertEqual(
                score_relevance_with_reaction(peer_env_with_vocative, use_warm=False),
                (0.0, None),
            )
            self.assertEqual(
                score_relevance_with_reaction(peer_vocative_with_zero, use_warm=False),
                (0.0, None),
            )
            self.assertEqual(
                score_relevance_with_reaction(closed_with_snowflake, use_warm=False),
                (1.0, None),
            )
            mock_run.assert_not_called()

        # 2. route_external_message-level precedence
        bot = MagicMock()
        bot.user.id = 1542285964213358633
        rules = {"ambient_classifier_enabled": False, "channel_tag_requirements": {}}

        def make_msg(content: str):
            m = MagicMock()
            m.id = 8888
            m.guild.id = 1534436119888793740
            m.author.id = 1468012353206354197
            m.author.bot = False
            m.channel.id = 1534436119888793750
            m.channel.name = "the-banana-stand"
            m.content = content
            m.attachments = []
            m.reference = None
            m.role_mentions = []
            m.mentions = []
            return m

        for suppressed_text in (
            closed_with_vocative,
            peer_env_with_vocative,
            peer_vocative_with_zero,
        ):
            ext_queue = asyncio.Queue()
            with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
                handled = await route_external_message(
                    make_msg(suppressed_text),
                    bot,
                    suppressed_text,
                    "Ian",
                    asyncio.Queue(),
                    ext_queue,
                    rules=rules,
                )
                self.assertTrue(handled)
                self.assertTrue(
                    ext_queue.empty(), f"Expected suppression for: {suppressed_text!r}"
                )

        override_queue = asyncio.Queue()
        with patch("tools.bridge_ambient.is_brock_guild", return_value=False):
            handled = await route_external_message(
                make_msg(closed_with_snowflake),
                bot,
                closed_with_snowflake,
                "Ian",
                asyncio.Queue(),
                override_queue,
                rules=rules,
            )
            self.assertTrue(handled)
            self.assertFalse(
                override_queue.empty(),
                "Explicit <@bot_id> snowflake must override closed peer handoff",
            )

    async def test_get_ambient_memory_context_async_to_thread_and_airgap(self):
        """Verify get_ambient_memory_context_async runs via asyncio.to_thread off the event loop and preserves air-gap filtering."""
        import tools.bridge_pipeline as bp

        offloaded = []
        orig_to_thread = asyncio.to_thread

        async def spy_to_thread(fn, *args, **kwargs):
            offloaded.append((fn, args, kwargs))
            return await orig_to_thread(fn, *args, **kwargs)

        fake_matches = {
            "memory": [
                {
                    "file_path": "/workspace/memory/private/network_secrets.md",
                    "file_name": "network_secrets.md",
                    "title": "Private Topology",
                    "category": "private",
                    "snippet": "Strictly private internal architecture note.",
                },
                {
                    "file_path": "/workspace/memory/vault/secret_keys.md",
                    "file_name": "secret_keys.md",
                    "title": "Vault Enclave",
                    "category": "vault",
                    "snippet": "Strictly isolated vault record.",
                },
                {
                    "file_path": "/workspace/memory/public/shared_patterns.md",
                    "file_name": "shared_patterns.md",
                    "title": "Shared Patterns",
                    "category": "public",
                    "snippet": "Public engineering pattern safe for external channels.",
                },
            ]
        }

        mock_indexer = MagicMock()
        mock_indexer.search.return_value = fake_matches

        with (
            patch(
                "tools.transcript_index.TranscriptIndexer", return_value=mock_indexer
            ),
            patch("tools.bridge_pipeline.asyncio.to_thread", side_effect=spy_to_thread),
        ):
            ext_ctx = await bp.get_ambient_memory_context_async(
                "How do we structure shared engineering patterns?",
                mode="external",
                channel_id=1534452820995080192,
                limit=4,
            )
            self.assertEqual(len(offloaded), 1)
            self.assertIs(offloaded[0][0], bp.get_ambient_memory_context)
            self.assertIn("Shared Patterns", ext_ctx)
            self.assertNotIn("Private Topology", ext_ctx)
            self.assertNotIn("Vault Enclave", ext_ctx)

            home_ctx = await bp.get_ambient_memory_context_async(
                "How do we structure shared engineering patterns?",
                mode="home",
                channel_id=1542081375287640084,
                limit=4,
            )
            self.assertEqual(len(offloaded), 2)
            self.assertIn("Private Topology", home_ctx)
            self.assertIn("Shared Patterns", home_ctx)

    async def test_sticky_thread_threshold_lowering_and_guardrails(self):
        """Verify zero_spoke_recently lowers ambient wake threshold to 0.45 without bypassing classifier."""
        from datetime import datetime, timezone

        now_dt = datetime.now(UTC)
        recent_zero_history = [
            {
                "author": "Zero",
                "is_bot": True,
                "content": "The worker pool drained cleanly after the last restart.",
                "timestamp": now_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
        ]

        def make_msg(content: str):
            m = MagicMock()
            m.id = 9101
            m.guild.id = 1534436119888793740
            m.author.id = 11111111
            m.author.bot = False
            m.channel.id = 1534452820995080192  # #lounge
            m.channel.name = "lounge"
            m.content = content
            m.created_at = now_dt
            m.attachments = []
            m.reference = None
            m.role_mentions = []
            m.mentions = []
            m.add_reaction = AsyncMock()
            return m

        bot = MagicMock()
        bot.user.id = 1542285964213358633
        base_rules = {
            "ambient_classifier_enabled": True,
            "ambient_relevance_threshold": 0.80,
            "channel_tag_requirements": {},
        }
        continuation = "The worker pool is still hitting timeouts under batch load."

        # 1. Score 0.55 in [0.45, 0.80): wakes when zero_spoke_recently=True and marks is_ambient_chime_in=True
        ext_queue_sticky = asyncio.Queue()
        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch(
                "tools.channel_history.get_recent_messages",
                return_value=recent_zero_history,
            ),
            patch("tools.channel_history.format_channel_context", return_value=""),
            patch(
                "tools.classifier.score_relevance_with_reaction",
                return_value=(0.55, None),
            ) as mock_score,
        ):
            handled = await route_external_message(
                make_msg(continuation),
                bot,
                continuation,
                "Ian",
                asyncio.Queue(),
                ext_queue_sticky,
                rules=base_rules,
            )
            self.assertTrue(handled)
            self.assertFalse(ext_queue_sticky.empty())
            item = await ext_queue_sticky.get()
            self.assertTrue(item.get("is_ambient_chime_in"))
            self.assertTrue(mock_score.call_args.kwargs.get("zero_spoke_recently"))

        # 2. Same score 0.55 stays silent when zero_spoke_recently=False
        ext_queue_cold = asyncio.Queue()
        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch("tools.channel_history.get_recent_messages", return_value=[]),
            patch(
                "tools.classifier.score_relevance_with_reaction",
                return_value=(0.55, None),
            ) as mock_score,
        ):
            handled = await route_external_message(
                make_msg(continuation),
                bot,
                continuation,
                "Ian",
                asyncio.Queue(),
                ext_queue_cold,
                rules=base_rules,
            )
            self.assertTrue(handled)
            self.assertTrue(ext_queue_cold.empty())
            self.assertFalse(
                mock_score.call_args.kwargs.get("zero_spoke_recently", False)
            )

        # 3. Score < 0.45 still suppresses and applies lightweight reaction even when zero_spoke_recently=True
        msg_rx = make_msg(continuation)
        ext_queue_low = asyncio.Queue()
        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch(
                "tools.channel_history.get_recent_messages",
                return_value=recent_zero_history,
            ),
            patch(
                "tools.classifier.score_relevance_with_reaction",
                return_value=(0.35, "👀"),
            ),
        ):
            handled = await route_external_message(
                msg_rx,
                bot,
                continuation,
                "Ian",
                asyncio.Queue(),
                ext_queue_low,
                rules=base_rules,
            )
            self.assertTrue(handled)
            self.assertTrue(ext_queue_low.empty())
            msg_rx.add_reaction.assert_awaited_once_with("👀")

        # 4. Misconfigured ambient_sticky_threshold > base_threshold is clamped to base_threshold
        clamped_rules = {
            "ambient_classifier_enabled": True,
            "ambient_relevance_threshold": 0.70,
            "ambient_sticky_threshold": 0.95,
            "channel_tag_requirements": {},
        }
        ext_queue_clamped = asyncio.Queue()
        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch(
                "tools.channel_history.get_recent_messages",
                return_value=recent_zero_history,
            ),
            patch("tools.channel_history.format_channel_context", return_value=""),
            patch(
                "tools.classifier.score_relevance_with_reaction",
                return_value=(0.72, None),
            ),
        ):
            handled = await route_external_message(
                make_msg(continuation),
                bot,
                continuation,
                "Ian",
                asyncio.Queue(),
                ext_queue_clamped,
                rules=clamped_rules,
            )
            self.assertTrue(handled)
            self.assertFalse(ext_queue_clamped.empty())

        # 5. Direct mention sets is_ambient_chime_in=False
        ext_queue_direct = asyncio.Queue()
        direct_text = "<@1542285964213358633> check the worker pool timeouts."
        with (
            patch("tools.bridge_ambient.is_brock_guild", return_value=False),
            patch("tools.channel_history.format_channel_context", return_value=""),
        ):
            handled = await route_external_message(
                make_msg(direct_text),
                bot,
                direct_text,
                "Ian",
                asyncio.Queue(),
                ext_queue_direct,
                rules=base_rules,
            )
            self.assertTrue(handled)
            self.assertFalse(ext_queue_direct.empty())
            direct_item = await ext_queue_direct.get()
            self.assertFalse(direct_item.get("is_ambient_chime_in"))

        # 6. Steered ambient sticky turn propagates is_ambient_chime_in=True
        from tools.bridge_runner import (
            channel_active_procs,
            channel_active_prompts,
            steering_channels,
        )

        ch_id = 1534452820995080192
        mock_proc = MagicMock()
        mock_proc.returncode = None
        channel_active_procs[ch_id] = mock_proc
        channel_active_prompts[ch_id] = "Checking batch queue state"
        ext_queue_steer = asyncio.Queue()
        try:
            with (
                patch("tools.bridge_ambient.is_brock_guild", return_value=False),
                patch(
                    "tools.channel_history.get_recent_messages",
                    return_value=recent_zero_history,
                ),
                patch(
                    "tools.classifier.score_relevance_with_reaction",
                    return_value=(0.55, None),
                ),
            ):
                handled = await route_external_message(
                    make_msg(continuation),
                    bot,
                    continuation,
                    "Ian",
                    asyncio.Queue(),
                    ext_queue_steer,
                    rules=base_rules,
                )
                self.assertTrue(handled)
                self.assertFalse(ext_queue_steer.empty())
                steer_item = await ext_queue_steer.get()
                self.assertTrue(steer_item.get("is_steer"))
                self.assertTrue(steer_item.get("is_ambient_chime_in"))
        finally:
            channel_active_procs.pop(ch_id, None)
            channel_active_prompts.pop(ch_id, None)
            steering_channels.discard(ch_id)

    async def test_additive_only_reply_gate_and_false_positive_guards(self):
        """Verify bare agreement ambient replies are suppressed while substantive, non-handoff, and direct replies pass."""
        from tools.classifier import is_additive_reply, is_bare_agreement_reply

        bare_agreements = [
            "Agreed.",
            "100%",
            "this",
            "same",
            "makes sense",
            "good point",
            "fair point",
            "lgtm",
            "+1",
            "spot on!",
            "totally agree",
            "sounds good",
            "well said",
            "seconded",
            "yep, agreed",
            "yeah, totally makes sense!",
            "@Amos +1, good point",
            "<@1468012353206354197> 100%, spot on.",
            'Agreed!\n```handoff\n{"v": 1.1, "kind": "ack", "reply": "none", "floor": "open"}\n```',
            '<!-- banana-envelope {"v": 1} -->\nSounds good.',
            '```handoff\n{"v": 1.1, "floor": "open"}\n```\n@Marvin yep, makes sense.',
        ]
        for text in bare_agreements:
            self.assertTrue(
                is_bare_agreement_reply(text), f"Expected bare agreement for: {text!r}"
            )
            self.assertFalse(
                is_additive_reply(text), f"Expected non-additive for: {text!r}"
            )
            self.assertTrue(
                should_suppress_ambient_reply(text, is_ambient_chime_in=True),
                f"Expected ambient suppression for: {text!r}",
            )
            self.assertFalse(
                should_suppress_ambient_reply(text, is_ambient_chime_in=False),
                f"Direct reply must never be suppressed: {text!r}",
            )

        additive_or_preserved = [
            "Agreed — we should also check timeout X",
            "Makes sense, though we'll need to flush the queue first.",
            "LGTM, just watch the retry backoff in bridge_runner.py.",
            "Agreed.\n```python\nprint('retry_ms=500')\n```",
            'Agreed.\n```json\n{"timeout": 30}\n```',
            "Agreed, see https://github.com/org/repo/pull/42",
            "Makes sense?",
            "Agreed, should we deploy this now?",
            "[REACT: 🔥]",
            "Agreed [REACT: 👍]",
        ]
        for text in additive_or_preserved:
            self.assertFalse(
                is_bare_agreement_reply(text),
                f"False positive bare agreement on: {text!r}",
            )
            self.assertTrue(is_additive_reply(text), f"Expected additive for: {text!r}")
            self.assertFalse(
                should_suppress_ambient_reply(text, is_ambient_chime_in=True),
                f"Must not suppress additive/preserved reply: {text!r}",
            )

        self.assertFalse(is_bare_agreement_reply(""))
        self.assertFalse(is_additive_reply(""))
        self.assertFalse(should_suppress_ambient_reply("", is_ambient_chime_in=True))


if __name__ == "__main__":
    unittest.main()
