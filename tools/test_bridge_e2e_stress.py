#!/usr/bin/env python3
"""
End-to-End Integration and Stress Test Suite for Zero Discord Bridge.
Validates multi-channel concurrency, mid-turn steering, Karakos scheduling,
liveness wedge detection, outbox queue flushing, Banana mutex contention,
and multi-tier session compaction.
"""

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import tools.bridge_formatting as bf
import tools.bridge_state as bs
import tools.bridge_runner as br
import tools.bridge_scheduler as bshed
import tools.bridge_handlers as bh


class TestBridgeEndToEndStress(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_queue_file = bs.QUEUE_FILE
        self.orig_ext_queue_file = bs.EXT_QUEUE_FILE
        self.orig_beacon_file = bs.BEACON_FILE
        self.orig_bot_status_file = bs.BOT_STATUS_FILE
        self.orig_sessions_file = bs.SESSIONS_FILE
        self.orig_session_metadata_file = bs.SESSION_METADATA_FILE
        self.orig_in_flight_file = bs.IN_FLIGHT_FILE
        self.orig_restart_intent = bs.RESTART_INTENT_FILE

        bs.DATA_DIR = self.temp_path
        bs.QUEUE_FILE = self.temp_path / "turn_queue.json"
        bs.EXT_QUEUE_FILE = self.temp_path / "external_turn_queue.json"
        bs.BEACON_FILE = self.temp_path / "liveness_beacon.json"
        bs.BOT_STATUS_FILE = self.temp_path / "bot_status.json"
        bs.SESSIONS_FILE = self.temp_path / "sessions.json"
        bs.SESSION_METADATA_FILE = self.temp_path / "session_metadata.json"
        bs.IN_FLIGHT_FILE = self.temp_path / "in_flight_turn.json"
        bs.RESTART_INTENT_FILE = self.temp_path / "restart_intent.json"
        bh.ATTACHMENTS_DIR = self.temp_path / "attachments"
        bh.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        bshed.BEACON_FILE = bs.BEACON_FILE
        bshed.BOT_STATUS_FILE = bs.BOT_STATUS_FILE
        bshed.DATA_DIR = self.temp_path

        self.home_queue = bs.PersistentTurnQueue(bs.QUEUE_FILE)
        self.ext_queue = bs.PersistentTurnQueue(bs.EXT_QUEUE_FILE)

    def tearDown(self):
        bs.DATA_DIR = self.orig_data_dir
        bs.QUEUE_FILE = self.orig_queue_file
        bs.EXT_QUEUE_FILE = self.orig_ext_queue_file
        bs.BEACON_FILE = self.orig_beacon_file
        bs.BOT_STATUS_FILE = self.orig_bot_status_file
        bs.SESSIONS_FILE = self.orig_sessions_file
        bs.SESSION_METADATA_FILE = self.orig_session_metadata_file
        bs.IN_FLIGHT_FILE = self.orig_in_flight_file
        bs.RESTART_INTENT_FILE = self.orig_restart_intent
        bshed.BEACON_FILE = self.orig_beacon_file
        bshed.BOT_STATUS_FILE = self.orig_bot_status_file
        bshed.DATA_DIR = self.orig_data_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_01_concurrent_queue_isolation(self):
        """Stress test: verify home, external, and thread queues execute independently."""
        execution_order = []

        async def mock_turn(prompt, status_msg, reply_target, attachments, mode, channel_id, **kwargs):
            execution_order.append(f"{mode}:{prompt}")
            await asyncio.sleep(0.05)

        with patch("tools.bridge_handlers.execute_agy_turn", side_effect=mock_turn):
            await self.home_queue.put({
                "prompt": "home_task_1",
                "status_msg": None,
                "reply_target": MagicMock(),
                "attachments": [],
                "is_steer": False,
                "mode": "home",
                "channel_id": bs.TARGET_CHANNEL_ID
            })

            await self.ext_queue.put({
                "prompt": "ext_task_1",
                "status_msg": None,
                "reply_target": MagicMock(),
                "attachments": [],
                "is_steer": False,
                "mode": "external",
                "channel_id": 999999
            })

            mock_bot = MagicMock()
            home_worker = asyncio.create_task(
                bh.queue_worker(self.home_queue, mock_bot)
            )
            ext_worker = asyncio.create_task(
                bh.external_queue_worker(self.ext_queue, mock_bot)
            )

            await asyncio.sleep(0.15)
            home_worker.cancel()
            ext_worker.cancel()

            self.assertIn("home:home_task_1", execution_order)
            self.assertIn("external:ext_task_1", execution_order)

    async def test_02_mid_turn_steering_simulation(self):
        """Stress test: verify in-flight turn receives SIGINT and enqueues steering directive."""
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.send_signal = MagicMock()
        br.channel_active_procs[bs.TARGET_CHANNEL_ID] = mock_proc

        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        mock_msg = MagicMock()
        mock_msg.id = 987654
        mock_msg.author.bot = False
        mock_msg.author.name = "Ryan"
        mock_msg.author.display_name = "Ryan"
        mock_msg.author.id = bs.OWNER_USER_ID
        mock_msg.channel.id = bs.TARGET_CHANNEL_ID
        mock_msg.channel.name = "zero-chat"
        mock_msg.created_at = datetime.now()
        mock_msg.reference = None
        mock_msg.content = "Pivot: ignore the prior plan and inspect /workspace/tools directly."
        mock_msg.attachments = []
        mock_msg.channel.typing = MagicMock()

        await bh.handle_message(mock_msg, mock_bot, self.home_queue, self.ext_queue)

        mock_proc.send_signal.assert_called_with(signal.SIGINT)
        self.assertIn(bs.TARGET_CHANNEL_ID, br.steering_channels)
        self.assertFalse(self.home_queue.empty())
        steer_item = await self.home_queue.get()
        self.assertTrue(steer_item["is_steer"])
        self.assertIn("USER MID-TURN STEERING UPDATE", steer_item["prompt"])

        del br.channel_active_procs[bs.TARGET_CHANNEL_ID]
        br.steering_channels.clear()

    async def test_03_group_chat_cascades_and_handoff_gate(self):
        """Stress test: verify loop suppression, word-floor checks, and handoff bypasses."""
        mock_bot = MagicMock()
        mock_bot.user.id = 1542285964213358633

        ext_channel_id = 1534436119888793750  # #agent-chat
        bh.channel_last_bot_reply.clear()

        # Case A: Silence narration from peer bot -> dropped
        msg_silence = MagicMock()
        msg_silence.id = 1001
        msg_silence.author.bot = True
        msg_silence.author.name = "Amos"
        msg_silence.author.display_name = "Amos"
        msg_silence.author.id = 111111111111111111
        msg_silence.channel.id = ext_channel_id
        msg_silence.channel.name = "agent-chat"
        msg_silence.content = "Remaining silent per room boundaries."
        msg_silence.attachments = []
        msg_silence.reference = None
        msg_silence.created_at = datetime.now()
        await bh.handle_message(msg_silence, mock_bot, self.home_queue, self.ext_queue)
        self.assertTrue(self.ext_queue.empty())

        # Case B: <4 word message from peer bot without handoff -> dropped
        msg_short = MagicMock()
        msg_short.id = 1002
        msg_short.author.bot = True
        msg_short.author.name = "Amos"
        msg_short.author.display_name = "Amos"
        msg_short.author.id = 111111111111111111
        msg_short.channel.id = ext_channel_id
        msg_short.channel.name = "agent-chat"
        msg_short.content = "Ok sounds good"
        msg_short.attachments = []
        msg_short.reference = None
        msg_short.created_at = datetime.now()
        await bh.handle_message(msg_short, mock_bot, self.home_queue, self.ext_queue)
        self.assertTrue(self.ext_queue.empty())

        # Case C: Explicit Handoff to Zero -> Enqueued immediately
        msg_handoff = MagicMock()
        msg_handoff.id = 1003
        msg_handoff.author.bot = True
        msg_handoff.author.name = "Amos"
        msg_handoff.author.display_name = "Amos"
        msg_handoff.author.id = 111111111111111111
        msg_handoff.channel.id = ext_channel_id
        msg_handoff.channel.name = "agent-chat"
        msg_handoff.content = '```handoff\n{"to": "Zero", "task": "Check HA battery voltages"}\n```'
        msg_handoff.attachments = []
        msg_handoff.reference = None
        msg_handoff.created_at = datetime.now()
        msg_handoff.channel.typing = MagicMock()
        await bh.handle_message(msg_handoff, mock_bot, self.home_queue, self.ext_queue)
        self.assertFalse(self.ext_queue.empty())
        item = await self.ext_queue.get()
        self.assertIn("handoff", item["prompt"])

    async def test_04_karakos_scheduler_wedge_and_outbox(self):
        """Stress test: verify liveness wedge alert on silence and outbox queue flushing."""
        now = time.time()
        # Write stale beacon (>420s silence while PROCESSING)
        with open(bs.BEACON_FILE, "w") as bf_file:
            json.dump({
                "state": "PROCESSING",
                "prompt": "Run deep diagnostics",
                "ts": now - 500,
                "alerted": False
            }, bf_file)

        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.fetch_channel = AsyncMock(return_value=mock_channel)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        br.active_proc = mock_proc

        scheduler = bshed.KarakosScheduler(
            dispatch_fn=AsyncMock(),
            bot=mock_bot
        )

        with patch("tools.scheduler_tool.load_schedule", return_value=[]), \
             patch("tools.outbox.flush_pending_messages", return_value=[{
                 "id": "outbox-test-1",
                 "channel_id": 1534436119888793750,
                 "channel": "agent-chat",
                 "content": "Banana mutex test message"
             }]), \
             patch("tools.banana.claim") as mock_claim, \
             patch("tools.banana.release") as mock_release:

            loop_task = asyncio.create_task(scheduler._loop())
            scheduler._running = True
            await asyncio.sleep(0.08)
            scheduler._running = False
            loop_task.cancel()

            mock_channel.send.assert_awaited()
            # Wedge alert checked
            wedge_calls = [call for call in mock_channel.send.call_args_list if "Wedge Alert" in str(call)]
            self.assertTrue(len(wedge_calls) > 0)

            # Banana mutex claimed and released
            mock_claim.assert_called()
            mock_release.assert_called()

        br.active_proc = None

    def test_05_formatting_scrubbing_and_choices_parsing(self):
        """Stress test: verify markdown table cards, credential scrubbing, LaTeX, and CHOICES extraction."""
        raw_text = (
            "### Infrastructure Status Report\n\n"
            "| Host | Status | CPU | Memory |\n"
            "| :--- | :--- | :--- | :--- |\n"
            "| Host1 | UP | 12% | 4.2 GB |\n"
            "| Host2 | UP | 18% | 8.1 GB |\n\n"
            "Auth Token: ghp_abc1234567890defABCDEF1234567890xyz0\n"
            "Math Formula: $E = mc^2$ and $$\\sum_{i=1}^n x_i$$\n\n"
            "[CHOICES: Inspect Host 1 | Inspect Host 2 | Run Full Sweep]"
        )

        # Full formatting pipeline as executed in bridge_runner
        formatted = bf.clean_discord_latex(raw_text)
        formatted = bf.format_for_discord(formatted)
        formatted = bf.scrub_credentials(formatted)

        # 1. Credential scrubbed
        self.assertNotIn("ghp_abc1234567890defABCDEF1234567890xyz0", formatted)
        self.assertIn("[REDACTED_", formatted)

        # 2. LaTeX cleaned
        self.assertNotIn("$$", formatted)
        self.assertIn("E = mc^2", formatted)

        # 3. Table cards formatted
        self.assertIn("• **Host1** (UP): 12% · 4.2 GB", formatted)

        # 4. Choices parsed
        clean_text, view = bf.parse_interactive_choices(formatted, quick_choice_view_cls=bh.QuickChoiceView)
        self.assertIsNotNone(view)
        self.assertEqual(len(view.children), 3)
        self.assertEqual(view.children[0].label, "Inspect Host 1")
        self.assertEqual(view.children[1].label, "Inspect Host 2")
        self.assertEqual(view.children[2].label, "Run Full Sweep")
        self.assertNotIn("[CHOICES:", clean_text)

    def test_06_compaction_guard_and_session_state(self):
        """Stress test: verify 25-turn auto-compaction guard threshold and carry-forward resetting."""
        sess_key = "home"
        for i in range(24):
            t = bs.increment_session_turn(sess_key)
            self.assertEqual(t, i + 1)

        needed, reason = bs.check_compaction_needed("conv-123", current_turns=24)
        self.assertFalse(needed)

        # 25th turn breaches threshold
        t = bs.increment_session_turn(sess_key)
        self.assertEqual(t, 25)
        needed, reason = bs.check_compaction_needed("conv-123", current_turns=25)
        self.assertTrue(needed)
        self.assertIn("turn count reached 25/25", reason)

        # Reset session
        bs.reset_session_meta(sess_key)
        meta = bs.get_session_metadata(sess_key)
        self.assertEqual(meta.get("turns"), 0)


if __name__ == "__main__":
    unittest.main()
