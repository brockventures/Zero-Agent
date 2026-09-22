#!/usr/bin/env python3
"""
Unit test suite for bridge_scheduler.py (Bridge Background Scheduler & Sidecar Dispatcher).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import tools.bridge_scheduler as bshed
import tools.bridge_state as bs
import tools.bridge_runner as br


class TestBridgeScheduler(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_data_dir = bs.DATA_DIR
        self.orig_beacon = bs.BEACON_FILE
        self.orig_bot_status = bs.BOT_STATUS_FILE

        bs.DATA_DIR = self.temp_path
        bs.BEACON_FILE = self.temp_path / "liveness_beacon.json"
        bs.BOT_STATUS_FILE = self.temp_path / "bot_status.json"

    def tearDown(self):
        bs.DATA_DIR = self.orig_data_dir
        bs.BEACON_FILE = self.orig_beacon
        bs.BOT_STATUS_FILE = self.orig_bot_status
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_anti_storm_guard(self):
        dispatch_mock = AsyncMock()
        bshed.LAST_SCHEDULED_DISPATCH.clear()

        # First dispatch should succeed
        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "OK", None)):
            await bshed.dispatch_scheduled_prompt("sidecars.py heartbeat", "Heartbeat Sweep")
            self.assertIn("Heartbeat Sweep", bshed.LAST_SCHEDULED_DISPATCH)

        # Immediate second dispatch within 300s should be dropped
        with patch("tools.sidecars.run_sidecar_job") as mock_job:
            await bshed.dispatch_scheduled_prompt("sidecars.py heartbeat", "Heartbeat Sweep")
            mock_job.assert_not_called()

    async def test_daily_session_rollover_dispatch(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        br.reset_session_keys.clear()

        with patch("tools.session_summarizer.generate_summary") as mock_sum, \
             patch("tools.bridge_daemons.daemon_manager.proactive_nightly_recycle", new_callable=AsyncMock) as mock_recycle:
            await bshed.dispatch_scheduled_prompt("[INTERNAL_SESSION_ROLLOVER]", "Daily Session Rollover")
            self.assertIn("home", br.reset_session_keys)
            mock_recycle.assert_awaited_once()

    async def test_birthday_reminder_silent(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        # When no birthdays match, run_sidecar_job returns (True, "", {"has_items": False, "has_due": False})
        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "", {"has_items": False, "has_due": False})):
            await bshed.dispatch_scheduled_prompt(
                "Check for friend & family birthdays today using /workspace/tools/birthday_reminder.py --quiet. If someone has a birthday today, post the reminder with the interactive text button.",
                "Daily Birthday Reminder",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            # Channel and queue should NOT be called (stay silent)
            mock_channel.send.assert_not_called()
            mock_turn_queue.put.assert_not_called()

    async def test_birthday_reminder_with_matches(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        bday_msg = "🎂 **Birthday Alert — Wednesday, September 02**\n\n• **Test Friend** turns a year older today!\n\n[CHOICES: Text Test \"Happy birthday!\"]"
        with patch("tools.sidecars.run_sidecar_job", return_value=(True, bday_msg, {"has_items": True, "has_due": True})):
            await bshed.dispatch_scheduled_prompt(
                "Check for friend & family birthdays today using /workspace/tools/birthday_reminder.py --quiet.",
                "Daily Birthday Reminder",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            # Channel send should be called with the alert
            mock_channel.send.assert_called_once()
            # Turn queue should not be enqueued (bypasses full LLM turn)
            mock_turn_queue.put.assert_not_called()

    async def test_host2_backup_silent(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "", {"summary": "Host 2 local USB backup completed successfully"})):
            await bshed.dispatch_scheduled_prompt(
                "Run the Host 2 local USB backup using /workspace/tools/sidecars.py backup_host2. Silent sidecar execution (silent unless error).",
                "Host 2 Local USB Backup",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            mock_channel.send.assert_not_called()
            mock_turn_queue.put.assert_not_called()

    async def test_host2_backup_error(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        err_msg = "⚠️ Host 2 backup failed (code 1): pg_dump failed"
        with patch("tools.sidecars.run_sidecar_job", return_value=(False, err_msg, {"error": "pg_dump failed"})):
            await bshed.dispatch_scheduled_prompt(
                "Run the Host 2 local USB backup using /workspace/tools/sidecars.py backup_host2. Silent sidecar execution (silent unless error).",
                "Host 2 Local USB Backup",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            mock_channel.send.assert_called_once_with(err_msg)
            mock_turn_queue.put.assert_not_called()

    async def test_host1_backup_silent(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "", {"summary": "Host 1 local USB backup completed successfully"})):
            await bshed.dispatch_scheduled_prompt(
                "Run the Host 1 local USB backup using /workspace/tools/sidecars.py backup_host1. Silent sidecar execution (silent unless error).",
                "Host 1 Local USB Backup",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            mock_channel.send.assert_not_called()
            mock_turn_queue.put.assert_not_called()

    async def test_host1_backup_error(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        err_msg = "⚠️ Host 1 backup failed (code 1): docker exec failed"
        with patch("tools.sidecars.run_sidecar_job", return_value=(False, err_msg, {"error": "docker exec failed"})):
            await bshed.dispatch_scheduled_prompt(
                "Run the Host 1 local USB backup using /workspace/tools/sidecars.py backup_host1. Silent sidecar execution (silent unless error).",
                "Host 1 Local USB Backup",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            mock_channel.send.assert_called_once_with(err_msg)
            mock_turn_queue.put.assert_not_called()


    async def test_should_run_job_on_time(self):
        now = 1788360000.0
        job = {
            "id": "test_job",
            "name": "Test Job",
            "enabled": True,
            "next_run_ts": now - 10,  # 10s late (on time)
            "catchup_if_missed": True,
            "catchup_window_seconds": 7200
        }
        should_run, reason = bshed.should_run_job(job, now)
        self.assertTrue(should_run)
        self.assertEqual(reason, "on_time")

    async def test_should_run_job_catchup_within_window(self):
        now = 1788360000.0
        job = {
            "id": "nightly_triage",
            "name": "Nightly Triage & Briefing",
            "enabled": True,
            "next_run_ts": now - 900,  # 15 minutes late
            "catchup_if_missed": True,
            "catchup_window_seconds": 7200  # 2 hours window
        }
        should_run, reason = bshed.should_run_job(job, now)
        self.assertTrue(should_run)
        self.assertIn("catchup within window", reason)

    async def test_should_run_job_catchup_window_expired(self):
        now = 1788360000.0
        job = {
            "id": "nightly_triage",
            "name": "Nightly Triage & Briefing",
            "enabled": True,
            "next_run_ts": now - 28000,  # 7.8 hours late
            "catchup_if_missed": True,
            "catchup_window_seconds": 7200  # 2 hours window
        }
        should_run, reason = bshed.should_run_job(job, now)
        self.assertFalse(should_run)
        self.assertIn("exceeding catchup window", reason)

    async def test_should_run_job_overdue_no_catchup(self):
        now = 1788360000.0
        job = {
            "id": "heartbeat_sweep",
            "name": "Heartbeat Sweep",
            "enabled": True,
            "next_run_ts": now - 300,  # 5 minutes late
            "catchup_if_missed": False
        }
        should_run, reason = bshed.should_run_job(job, now)
        self.assertFalse(should_run)
        self.assertIn("catchup_if_missed=False", reason)

    async def test_should_run_job_not_due(self):
        now = 1788360000.0
        job = {
            "id": "future_job",
            "name": "Future Job",
            "enabled": True,
            "next_run_ts": now + 600  # 10 minutes in the future
        }
        should_run, reason = bshed.should_run_job(job, now)
        self.assertFalse(should_run)
        self.assertEqual(reason, "not_due")

    async def test_should_run_job_detects_already_ran_in_sidecar_status(self):
        # Simulate previous night's execution recorded in sidecar_status.json
        status_file = bs.DATA_DIR / "sidecar_status.json"
        nightly_slot_ts = 1788330600.0  # 11:30 PM last night
        with open(status_file, "w") as f:
            json.dump({
                "triage": {
                    "job_id": "triage",
                    "name": "Nightly Triage & Briefing",
                    "timestamp_epoch": int(nightly_slot_ts + 15),  # Ran at 11:30:15 PM
                    "timestamp_pt": "2026-09-01 11:30 PM PT",
                    "status": "ok"
                }
            }, f)

        # Morning restart evaluation at 7:19 AM next morning
        morning_now = 1788358740.0
        job = {
            "id": "nightly_triage",
            "name": "Nightly Triage & Briefing",
            "enabled": True,
            "schedule_type": "daily",
            "next_run_ts": nightly_slot_ts,
            "catchup_if_missed": True,
            "catchup_window_seconds": 7200
        }
        should_run, reason = bshed.should_run_job(job, morning_now)
        # MUST NOT run because it already ran normally in the current period
        self.assertFalse(should_run)
        self.assertIn("already ran in current period", reason)

    async def test_should_run_job_weekly_not_blocked_by_earlier_manual_run(self):
        # Sunday 4:59 PM PT test run: 1788739199
        sunday_test_ts = 1788739199.0
        # Scheduled Wednesday slot: 1789008000 (Wed 8:00 PM PT, 3.11 days later)
        wednesday_slot_ts = 1789008000.0

        status_file = bs.DATA_DIR / "sidecar_status.json"
        with open(status_file, "w") as f:
            json.dump({
                "weekly_meal_proposal": {
                    "job_id": "weekly_meal_proposal",
                    "timestamp_epoch": int(sunday_test_ts),
                    "status": "ok"
                }
            }, f)

        job = {
            "id": "weekly_meal_proposal",
            "name": "Weekly 3-Dinner Meal Proposal",
            "enabled": True,
            "schedule_type": "weekly",
            "next_run_ts": wednesday_slot_ts,
            "catchup_if_missed": False
        }
        # Evaluated at exactly Wednesday 8:00 PM PT
        should_run, reason = bshed.should_run_job(job, wednesday_slot_ts)
        # MUST run on Wednesday despite Sunday test run (>24h ago)
        self.assertTrue(should_run)
        self.assertEqual(reason, "on_time")

    async def test_nas_logs_dispatch_targets_homelab_channel(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel

        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "🗄️ NAS Log Review OK", {})):
            await bshed.dispatch_scheduled_prompt(
                "Run the nightly NAS log review using /workspace/tools/sidecars.py nas_logs.",
                "NAS Log Review",
                bot=mock_bot
            )
            # Must route to HOMELAB_CHANNEL_ID (1544955535722545253)
            mock_bot.get_channel.assert_called_with(bs.HOMELAB_CHANNEL_ID)
            mock_channel.send.assert_called_once_with("🗄️ NAS Log Review OK")

    async def test_nas_logs_dispatch_respects_explicit_channel_id(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel

        custom_channel_id = 9876543210
        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "🗄️ NAS Log Review Custom", {})):
            await bshed.dispatch_scheduled_prompt(
                "Run the nightly NAS log review using /workspace/tools/sidecars.py nas_logs.",
                "NAS Log Review",
                bot=mock_bot,
                channel_id=custom_channel_id
            )
            # Must route to custom_channel_id
            mock_bot.get_channel.assert_called_with(custom_channel_id)
            mock_channel.send.assert_called_once_with("🗄️ NAS Log Review Custom")

    async def test_evaluate_and_dispatch_forwards_channel_id(self):
        dispatch_mock = AsyncMock()
        scheduler = bshed.BridgeScheduler(dispatch_fn=dispatch_mock)
        test_job = {
            "id": "nas_logs",
            "name": "NAS Log Review",
            "enabled": True,
            "schedule_type": "daily",
            "hour_pt": 22,
            "minute_pt": 0,
            "channel_id": bs.HOMELAB_CHANNEL_ID,
            "next_run_ts": 1000.0,
            "prompt": "Run the nightly NAS log review"
        }
        with patch("tools.scheduler_tool.load_schedule", return_value=[test_job]), \
             patch("tools.bridge_scheduler.should_run_job", return_value=(True, "on_time")), \
             patch("tools.scheduler_tool.save_schedule"):
            await scheduler._evaluate_and_dispatch_jobs()
            dispatch_mock.assert_called_once_with(
                "Run the nightly NAS log review",
                job_name="NAS Log Review",
                channel_id=bs.HOMELAB_CHANNEL_ID
            )

    async def test_scheduler_lifecycle(self):
        dispatch_mock = AsyncMock()
        scheduler = bshed.BridgeScheduler(dispatch_fn=dispatch_mock)
        with patch("tools.scheduler_tool.load_schedule", return_value=[]):
            await scheduler.start()
            self.assertTrue(scheduler._running)
            await scheduler.stop()
            self.assertFalse(scheduler._running)


    async def test_prowlarr_watchdog_silent(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_queue = MagicMock()
        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "(nominal - 0 Prowlarr failures)", None)) as mock_sidecar:
            await bshed.dispatch_scheduled_prompt(
                "Run the Prowlarr indexer health check using /workspace/tools/sidecars.py prowlarr.",
                job_name="Prowlarr Indexer Health Watchdog",
                bot=mock_bot,
                turn_queue=mock_queue
            )
            mock_sidecar.assert_called_once()
            # Must NOT queue an LLM turn or create status message
            mock_queue.put.assert_not_called()

    async def test_prowlarr_watchdog_alert(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.fetch_channel = AsyncMock(return_value=mock_channel)
        mock_queue = MagicMock()
        with patch("tools.sidecars.run_sidecar_job", return_value=(False, "⚠️ 1 indexer failing", None)) as mock_sidecar:
            await bshed.dispatch_scheduled_prompt(
                "Run the Prowlarr indexer health check using /workspace/tools/sidecars.py prowlarr.",
                job_name="Prowlarr Indexer Health Watchdog",
                channel_id=12345,
                bot=mock_bot,
                turn_queue=mock_queue
            )
            mock_sidecar.assert_called_once()
            mock_channel.send.assert_awaited_once_with("⚠️ 1 indexer failing")
            mock_queue.put.assert_not_called()

    async def test_marketing_sweep_off_week_silent(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.fetch_channel = AsyncMock(return_value=mock_channel)
        mock_queue = MagicMock()

        # On an off-week, run_sidecar_job returns should_post=False in extra and empty/silent message
        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "", {"should_post": False})) as mock_sidecar:
            await bshed.dispatch_scheduled_prompt(
                "Run the promotional email marketing sweep using /workspace/tools/sidecars.py marketing.",
                job_name="Biweekly Marketing Sweep",
                bot=mock_bot,
                turn_queue=mock_queue
            )
            mock_sidecar.assert_called_once()
            # Must remain completely silent
            mock_channel.send.assert_not_called()
            mock_queue.put.assert_not_called()

    async def test_marketing_sweep_on_week_posts(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.fetch_channel = AsyncMock(return_value=mock_channel)
        mock_queue = MagicMock()

        report_content = "📬 **Biweekly Marketing Report** — Sep 06\n\n**1 promotional senders this period:**\n1. Store — 3 emails"
        with patch("tools.sidecars.run_sidecar_job", return_value=(True, report_content, {"should_post": True})) as mock_sidecar:
            await bshed.dispatch_scheduled_prompt(
                "Run the promotional email marketing sweep using /workspace/tools/sidecars.py marketing.",
                job_name="Biweekly Marketing Sweep",
                bot=mock_bot,
                turn_queue=mock_queue
            )
            mock_sidecar.assert_called_once()
            mock_channel.send.assert_awaited_once_with(report_content)
            mock_queue.put.assert_not_called()


    async def test_scheduler_reload_flag_defers_when_busy(self):
        """Verify BridgeScheduler does not trigger reload if is_busy returns active channels, even if flag > 20s."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            flag = Path(tmpdir) / "reload_bridge.flag"
            flag.touch()
            # Set mtime to 30s ago
            os.utime(flag, (time.time() - 30, time.time() - 30))

            reload_mock = AsyncMock()
            scheduler = bshed.BridgeScheduler(
                dispatch_fn=AsyncMock(),
                reload_fn=reload_mock,
                is_busy_fn=lambda: ["#zero-chat"]
            )

            with patch("tools.bridge_scheduler.DATA_DIR", Path(tmpdir)):
                # Run one iteration of the scheduler loop logic
                busy = scheduler.is_busy_fn() if scheduler.is_busy_fn else []
                if not busy:
                    if flag.exists():
                        flag.unlink()
                    await reload_mock(None, initiator="scheduler", force=True)

    async def test_outbox_flush_attaches_choice_buttons(self):
        """Verify outbox messages with [CHOICES: ...] are parsed and delivered with interactive buttons."""
        from tools.bridge_handlers import QuickChoiceView
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        callback_mock = AsyncMock()

        scheduler = bshed.BridgeScheduler(
            dispatch_fn=AsyncMock(),
            bot=mock_bot,
            quick_choice_view_cls=QuickChoiceView,
            button_choice_fn=callback_mock,
        )

        outbox_msg = {
            "id": "outbox-12345",
            "channel": "shopping",
            "channel_id": 1544955538033348618,
            "content": "Weekly Menu Proposal\n\n[CHOICES: Lock In Menu | Swap Sunday | Swap Tuesday | Swap Thursday]"
        }

        with patch("tools.outbox.flush_pending_messages", return_value=[outbox_msg]):
            from tools.outbox import flush_pending_messages
            from tools.bridge_formatting import parse_interactive_choices
            pending_outbox = flush_pending_messages()
            for omsg in pending_outbox:
                target_cid = omsg.get("channel_id")
                target_channel = scheduler.bot.get_channel(target_cid)
                raw_content = omsg.get("content", "")
                clean_content, choice_view = parse_interactive_choices(
                    raw_content,
                    scheduler.quick_choice_view_cls,
                    scheduler.button_choice_fn,
                )
                if choice_view:
                    await target_channel.send(clean_content, view=choice_view)
                else:
                    await target_channel.send(clean_content)

        mock_channel.send.assert_awaited_once()
        args, kwargs = mock_channel.send.await_args
        self.assertEqual(args[0], "Weekly Menu Proposal")
        self.assertIn("view", kwargs)
        self.assertIsInstance(kwargs["view"], QuickChoiceView)
        self.assertEqual(len(kwargs["view"].children), 4)
        self.assertEqual(kwargs["view"].children[0].label, "Lock In Menu")

    async def test_outbox_flush_chunks_large_message(self):
        """Verify outbox messages > 2,000 chars (e.g. 2,527 chars) are defensively chunked into sends <= 1900 chars."""
        from tools.bridge_handlers import QuickChoiceView
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel

        scheduler = bshed.BridgeScheduler(
            dispatch_fn=AsyncMock(),
            bot=mock_bot,
            quick_choice_view_cls=QuickChoiceView,
            button_choice_fn=AsyncMock(),
        )

        # Construct 2,527 character message (exactly matching the evening incident)
        large_body = "Line of standup status details with actionable context.\n" * 45
        self.assertGreater(len(large_body), 2000)

        outbox_msg = {
            "id": "outbox-test-large",
            "channel": "zero-chat",
            "channel_id": 1542081375287640084,
            "content": large_body + "\n\n[CHOICES: Proceed | Abort]"
        }

        with patch("tools.outbox.flush_pending_messages", return_value=[outbox_msg]):
            await scheduler.flush_outbox_queue()

        self.assertGreater(mock_channel.send.await_count, 1)
        for call_args in mock_channel.send.await_args_list:
            chunk = call_args[0][0]
            self.assertLessEqual(len(chunk), 1900)

        # Choice view must be attached only to the final chunk
        last_call_kwargs = mock_channel.send.await_args_list[-1][1]
        self.assertIn("view", last_call_kwargs)
        self.assertIsInstance(last_call_kwargs["view"], QuickChoiceView)

    async def test_outbox_flush_banana_stand_chunks_large_message(self):
        """Verify large messages targeting #the-banana-stand are chunked and guarded by Banana mutex."""
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel

        scheduler = bshed.BridgeScheduler(
            dispatch_fn=AsyncMock(),
            bot=mock_bot,
        )

        large_banana_msg = "🍌 Standup RFC Proposal Content: " + ("x" * 2400)
        outbox_msg = {
            "id": "outbox-test-banana-large",
            "channel": "the-banana-stand",
            "channel_id": 1534436119888793750,
            "content": large_banana_msg
        }

        with patch("tools.outbox.flush_pending_messages", return_value=[outbox_msg]), \
             patch("tools.banana.claim") as mock_claim, \
             patch("tools.banana.release") as mock_release:
            await scheduler.flush_outbox_queue()

        mock_claim.assert_called_once_with(subject="outbox-test-banana-large")
        mock_release.assert_called_once()
        self.assertGreater(mock_channel.send.await_count, 1)
        for call_args in mock_channel.send.await_args_list:
            chunk = call_args[0][0]
            self.assertLessEqual(len(chunk), 1900)

    async def test_outbox_flush_dlq_on_send_failure(self):
        """Verify per-message failure isolation and dead-letter queue logging."""
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        # First message fails (e.g. Discord 400 or HTTP error), second succeeds
        mock_channel.send.side_effect = [Exception("HTTP 400 Bad Request: Invalid Form Body"), None]
        mock_bot.get_channel.return_value = mock_channel

        scheduler = bshed.BridgeScheduler(
            dispatch_fn=AsyncMock(),
            bot=mock_bot,
        )

        msg1 = {"id": "outbox-fail-1", "channel": "lounge", "channel_id": 1534452820995080192, "content": "bad msg"}
        msg2 = {"id": "outbox-succ-2", "channel": "lounge", "channel_id": 1534452820995080192, "content": "good msg"}

        with patch("tools.outbox.flush_pending_messages", return_value=[msg1, msg2]), \
             patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            await scheduler.flush_outbox_queue()

        # Both sends were attempted despite first error
        self.assertEqual(mock_channel.send.await_count, 2)
        # DLQ file was written
        mock_file.assert_called()

    async def test_long_running_job_does_not_block_scheduler_or_heartbeat(self):
        """Verify long-running sidecars (e.g. dreaming pass) run in background without blocking evaluate or heartbeats."""
        job_started = asyncio.Event()
        job_finish = asyncio.Event()

        async def slow_dispatch(prompt, job_name=None, channel_id=None):
            job_started.set()
            await job_finish.wait()

        mock_bot = MagicMock()
        mock_bot.is_ready.return_value = True
        mock_bot.latency = 0.05

        scheduler = bshed.BridgeScheduler(
            dispatch_fn=slow_dispatch,
            bot=mock_bot,
        )

        test_job = {
            "id": "dream",
            "name": "Dreaming Consolidation",
            "enabled": True,
            "schedule_type": "daily",
            "hour_pt": 1,
            "minute_pt": 45,
            "next_run_ts": 100.0,
            "prompt": "Run dreaming consolidation"
        }

        with patch("tools.scheduler_tool.load_schedule", return_value=[test_job]), \
             patch("tools.bridge_scheduler.should_run_job", return_value=(True, "due")), \
             patch("tools.scheduler_tool.save_schedule"):
            
            # 1. _evaluate_and_dispatch_jobs should return immediately (non-blocking)
            await scheduler._evaluate_and_dispatch_jobs()
            
            # Wait until the background task has actually started executing
            await asyncio.wait_for(job_started.wait(), timeout=1.0)
            self.assertEqual(len(scheduler._active_job_tasks), 1)

            # 2. Heartbeat loop should still update liveness_beacon while job is running
            scheduler._running = True
            with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
                await scheduler._heartbeat_loop()

            self.assertTrue(bs.BEACON_FILE.exists())
            with open(bs.BEACON_FILE) as bf:
                beacon_data = json.load(bf)
            self.assertEqual(beacon_data.get("gateway_status"), "connected")
            self.assertIsNotNone(beacon_data.get("gateway_heartbeat"))

            # 3. Clean up the slow job
            job_finish.set()
            if scheduler._active_job_tasks:
                await asyncio.gather(*list(scheduler._active_job_tasks))
            self.assertEqual(len(scheduler._active_job_tasks), 0)
            scheduler._running = False


if __name__ == "__main__":
    unittest.main()



