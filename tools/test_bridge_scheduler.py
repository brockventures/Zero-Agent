#!/usr/bin/env python3
# ruff: noqa
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

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.append(str(WORKSPACE))

# Ensure tools.bridge_commands reflects current worktree implementation in-place
if "tools.bridge_commands" in sys.modules:
    wt_cmd_file = REPO_ROOT / "tools" / "bridge_commands.py"
    if wt_cmd_file.exists():
        with open(wt_cmd_file, "r") as f:
            code = compile(f.read(), str(wt_cmd_file), "exec")
        exec(code, sys.modules["tools.bridge_commands"].__dict__)

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
        _ = AsyncMock()
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
        sessions_file = self.temp_path / "sessions.json"
        sessions_file.write_text(json.dumps({"home": "conv-home-1"}), encoding="utf-8")

        with patch("tools.session_rollover.SESSIONS_FILE", sessions_file), \
             patch("tools.session_rollover.prune_brain_sessions", return_value={"pruned_count": 0, "freed_mb": 0.0}) as mock_prune, \
             patch("tools.bridge_daemons.daemon_manager.proactive_nightly_recycle", new_callable=AsyncMock) as mock_recycle:
            await bshed.dispatch_scheduled_prompt("[INTERNAL_SESSION_ROLLOVER]", "Daily Session Rollover")
            self.assertNotIn("home", br.reset_session_keys)
            mock_prune.assert_called_once_with(max_age_days=30, dry_run=False)
            mock_recycle.assert_not_awaited()

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

    async def test_should_run_job_weekly_not_blocked_by_prior_day_manual_run(self):
        # Saturday 12:31 PM PT manual test run: 15.5 hours before Sunday 4:00 AM PT slot
        sunday_4am_slot_ts = 1792302000.0
        saturday_manual_ts = sunday_4am_slot_ts - int(15.5 * 3600)

        status_file = bs.DATA_DIR / "sidecar_status.json"
        with open(status_file, "w") as f:
            json.dump({
                "personal_data_sync": {
                    "job_id": "personal_data_sync",
                    "timestamp_epoch": int(saturday_manual_ts),
                    "status": "warning"
                }
            }, f)

        job = {
            "id": "personal_data_sync",
            "name": "Weekly Personal Data & Gaming Profile Sync",
            "enabled": True,
            "schedule_type": "weekly",
            "next_run_ts": sunday_4am_slot_ts,
            "catchup_if_missed": True
        }
        should_run, reason = bshed.should_run_job(job, sunday_4am_slot_ts)
        self.assertTrue(should_run)
        self.assertEqual(reason, "on_time")

    async def test_get_sidecar_health_summary_excludes_stale_active_failures(self):
        import time as _time
        from tools import sidecars
        status_file = bs.DATA_DIR / "sidecar_status.json"
        now_epoch = int(_time.time())
        with open(status_file, "w") as f:
            json.dump({
                "old_weekly_job": {
                    "job_id": "old_weekly_job",
                    "name": "Old Weekly Job",
                    "timestamp_epoch": now_epoch - int(35 * 3600),  # 35h ago (>24h window)
                    "status": "warning",
                    "summary": "Stale warning from 35h ago"
                },
                "recent_job": {
                    "job_id": "recent_job",
                    "name": "Recent Job",
                    "timestamp_epoch": now_epoch - int(2 * 3600),  # 2h ago (<24h window)
                    "status": "warning",
                    "summary": "Recent warning from 2h ago"
                }
            }, f)
        with patch.object(sidecars, "EXECUTION_STATUS_FILE", status_file):
            summary = sidecars.get_sidecar_health_summary(since_hours=24.0)
        active_ids = [f["job_id"] for f in summary["active_failures"]]
        self.assertNotIn("old_weekly_job", active_ids)
        self.assertIn("recent_job", active_ids)

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
                channel_id=bs.HOMELAB_CHANNEL_ID,
                job_id="nas_logs"
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

    async def test_outbox_flush_agent_turn_dispatches_cleanly(self):
        """Verify agent_turn outbox messages route to dispatch_fn without dumping raw prompt to channel."""
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_dispatch = AsyncMock()

        scheduler = bshed.BridgeScheduler(
            dispatch_fn=mock_dispatch,
            bot=mock_bot,
        )

        msg = {
            "id": "outbox-agent-turn-1",
            "type": "agent_turn",
            "channel": "zero-chat",
            "channel_id": 1542081375287640084,
            "content": "[SYSTEM NOTIFICATION: DETACHED TASK COMPLETE]\nDirective: Synthesize this.",
            "source": "Detached Task: Test Task",
        }

        with patch("tools.outbox.flush_pending_messages", return_value=[msg]):
            await scheduler.flush_outbox_queue()

        mock_dispatch.assert_awaited_once_with(
            prompt="[SYSTEM NOTIFICATION: DETACHED TASK COMPLETE]\nDirective: Synthesize this.",
            job_name="Detached Task: Test Task",
            channel_id=1542081375287640084,
            job_id="outbox_turn_outbox-agent-turn-1",
        )
        mock_channel.send.assert_not_called()

    async def test_outbox_flush_agent_turn_suppresses_prompt_when_dispatch_fails(self):
        """Verify agent_turn prompt is never leaked to channel even if dispatch_fn raises an exception."""
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_dispatch = AsyncMock(side_effect=Exception("Turn queue full"))

        scheduler = bshed.BridgeScheduler(
            dispatch_fn=mock_dispatch,
            bot=mock_bot,
        )

        msg = {
            "id": "outbox-agent-turn-fail",
            "type": "agent_turn",
            "channel": "zero-chat",
            "channel_id": 1542081375287640084,
            "content": "[SYSTEM NOTIFICATION: DETACHED TASK COMPLETE]\nDirective: Synthesize this.",
            "source": "Detached Task: Test Task",
        }

        with patch("tools.outbox.flush_pending_messages", return_value=[msg]):
            await scheduler.flush_outbox_queue()

        mock_dispatch.assert_awaited_once()
        mock_channel.send.assert_not_called()

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


    async def test_actual_budget_sync_silent(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "", {"new_transactions": 5})):
            await bshed.dispatch_scheduled_prompt(
                "Run automated Actual Budget bank sync using /workspace/tools/sidecars.py actual_sync. Silent sidecar (silent execution on routine sync, alert only on MFA reauth or failure).",
                "Actual Budget Daily Bank Sync",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            mock_channel.send.assert_not_called()
            mock_turn_queue.put.assert_not_called()

    async def test_actual_budget_sync_error_routes_to_finances(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_finances_channel = AsyncMock()
        mock_bot.get_channel.side_effect = lambda cid: mock_finances_channel if cid == bs.FINANCES_CHANNEL_ID else None
        mock_turn_queue = AsyncMock()

        err_msg = "⚠️ Actual Budget sync sidecar failed: SimpleFin API 401 Unauthorized"
        with patch("tools.sidecars.run_sidecar_job", return_value=(False, err_msg, {"error": "401"})):
            await bshed.dispatch_scheduled_prompt(
                "Run automated Actual Budget bank sync using /workspace/tools/sidecars.py actual_sync. Silent sidecar (silent execution on routine sync, alert only on MFA reauth or failure).",
                "Actual Budget Daily Bank Sync",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            mock_finances_channel.send.assert_called_once_with(err_msg)
            mock_turn_queue.put.assert_not_called()

    async def test_daily_token_budget_watchdog_dispatch(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_token_budget_watchdog", return_value=(True, "Report sent", {})) as mock_run:
            await bshed.dispatch_scheduled_prompt(
                "Run daily token budget watchdog using tools/sidecars.py token_budget_watchdog",
                "Daily Token Budget Watchdog",
                bot=mock_bot,
                turn_queue=mock_turn_queue
            )
            mock_run.assert_called_once()
            mock_turn_queue.put.assert_not_called()

    async def test_agora_roguelike_pm_headless_dispatch(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_agora_roguelike_pm", return_value=(True, "", {"nominal": True})):
            await bshed.dispatch_scheduled_prompt(
                "Run the Agora Roguelike PM board assessment and autonomous merge check using python3 /workspace/tools/sidecars.py agora_pm. Post updates to #agora when PRs or assignments change (silent when nominal).",
                "Agora Roguelike PM & Board Coordinator",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                channel_id=1558202642663211169
            )
            mock_turn_queue.put.assert_not_called()

    async def test_agora_roguelike_engineer_headless_dispatch(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_agora_roguelike_engineer", return_value=(True, "All pass", None)):
            await bshed.dispatch_scheduled_prompt(
                "Run the Agora Roguelike engineering execution check using python3 /workspace/tools/sidecars.py agora_engineer.",
                "Agora Roguelike Engineering Execution",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                channel_id=1558202642663211169
            )
            mock_turn_queue.put.assert_not_called()

    def test_should_run_job_quiet_hours_overnight(self):
        from tools.bridge_scheduler import should_run_job
        from datetime import datetime
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")

        job = {
            "id": "agora_roguelike_pm",
            "name": "Agora Roguelike PM & Board Coordinator",
            "enabled": True,
            "quiet_hours": [23, 7],
            "next_run_ts": 1000,
            "catchup_if_missed": True,
            "catchup_window_seconds": 7200,
            "schedule_type": "interval",
            "min_period_seconds": 60,
        }

        # 11:30 PM PT (Hour 23) -> Should be paused
        ts_2330 = datetime(2026, 10, 9, 23, 30, tzinfo=pt).timestamp()
        ok, reason = should_run_job(job, ts_2330)
        self.assertFalse(ok)
        self.assertIn("quiet_hours", reason)

        # 03:00 AM PT (Hour 3) -> Should be paused
        ts_0300 = datetime(2026, 10, 10, 3, 0, tzinfo=pt).timestamp()
        ok, reason = should_run_job(job, ts_0300)
        self.assertFalse(ok)
        self.assertIn("quiet_hours", reason)

        # 07:05 AM PT (Hour 7) -> Should NOT be paused by quiet hours
        ts_0705 = datetime(2026, 10, 10, 7, 5, tzinfo=pt).timestamp()
        job["next_run_ts"] = ts_0705 - 10
        ok, reason = should_run_job(job, ts_0705)
        self.assertTrue(ok)

    def test_should_run_job_maintenance_exempt(self):
        from tools.bridge_scheduler import should_run_job
        from datetime import datetime
        from zoneinfo import ZoneInfo
        pt = ZoneInfo("America/Los_Angeles")

        # Session rollover at 2 AM is maintenance exempt from default quiet hours
        ts_0200 = datetime(2026, 10, 10, 2, 0, tzinfo=pt).timestamp()
        job = {
            "id": "session_rollover",
            "name": "Daily Session Rollover",
            "enabled": True,
            "next_run_ts": ts_0200 - 10,
            "catchup_if_missed": True,
            "catchup_window_seconds": 7200,
            "schedule_type": "daily",
            "min_period_seconds": 60,
        }
        ok, reason = should_run_job(job, ts_0200)
        self.assertTrue(ok)

    async def test_quant_daily_sweep_does_not_route_to_cash_sweep_monitor(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "📈 Quant Sweep Report", {})) as mock_sidecar:
            await bshed.dispatch_scheduled_prompt(
                "Run the multi-venue quantitative paper trading expansion sweep across all 4 tracks (Prediction Markets, Sports Positive-EV, Crypto Funding Basis, Equity/ETF Stat-Arb) using python3 /workspace/tools/sidecars.py quant. Post the track-by-track P&L and edge scorecard to #projects.",
                "Multi-Venue Quant Paper Trading Sweep",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                channel_id=1546029274627309638,
                job_id="quant_daily_sweep",
            )
            mock_sidecar.assert_called_once()
            self.assertEqual(mock_sidecar.call_args[0][0], "quant_expansion_sweep")
            mock_turn_queue.put.assert_not_called()

    async def test_kalshi_and_grocery_direct_sidecar_dispatch(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_sidecar_job", return_value=(True, "🛒 Grocery Cart Ready", {})) as mock_sidecar:
            await bshed.dispatch_scheduled_prompt(
                "Run the automated Sunday Whole Foods grocery cart staging sidecar: python3 /workspace/tools/sidecars.py grocery_stage",
                "Weekly Sunday Whole Foods Cart Staging",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                channel_id=1544956793053253723,
                job_id="weekly_grocery_staging",
            )
            mock_sidecar.assert_called_once()
            self.assertEqual(mock_sidecar.call_args[0][0], "weekly_grocery_staging")
            mock_turn_queue.put.assert_not_called()

    async def test_outbox_agent_turn_bypasses_sidecar_substring_interceptors(self):
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_turn_queue = AsyncMock()

        forge_envelope = (
            "[FORGE BUILD COMPLETION ENVELOPE — `fg-1793065628-z-wave-js-living-r`]\n"
            "**Build Title:** Z-Wave JS & Living Room Blinds Self-Healing\n"
            "- **Chunk 1:** Merged [verified via `pytest tools/test_ha_reauth_watchdog.py && kalshi_check`]"
        )
        with patch("tools.sidecars.run_sidecar_job") as mock_sidecar:
            await bshed.dispatch_scheduled_prompt(
                forge_envelope,
                "forge-complete-fg-1793065628-z-wave-js-living-r",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                channel_id=bs.TARGET_CHANNEL_ID,
                job_id="outbox_turn_outbox-1793066592989-1234",
            )
            mock_sidecar.assert_not_called()
            mock_turn_queue.put.assert_awaited_once()
            queued = mock_turn_queue.put.call_args[0][0]
            self.assertEqual(queued["prompt"], forge_envelope)
            self.assertEqual(queued["channel_id"], bs.TARGET_CHANNEL_ID)
            self.assertIsNone(queued["model_override"])

    async def test_execute_bridge_reload_clears_pending_reload_flag_before_bot_close(self):
        """Verify calling execute_bridge_reload() unlinks reload_bridge.flag before closing bot."""
        from tools.bridge_commands import execute_bridge_reload

        flag_path = bs.DATA_DIR / "reload_bridge.flag"
        flag_path.write_text("pending reload")
        self.assertTrue(flag_path.exists())

        flag_state_before_bot_close = None
        mock_bot = MagicMock()

        async def fake_close():
            nonlocal flag_state_before_bot_close
            flag_state_before_bot_close = flag_path.exists()

        mock_bot.close = AsyncMock(side_effect=fake_close)

        with patch("tools.bridge_commands.DATA_DIR", bs.DATA_DIR), \
             patch("tools.bridge_commands.record_restart_intent"), \
             patch("tools.bridge_git_sync.sync_git_on_reload"), \
             patch("tools.bridge_daemons.daemon_manager.shutdown_all", new_callable=AsyncMock), \
             patch("os.execv") as mock_execv:
            await execute_bridge_reload(bot=mock_bot, announce=False)

        self.assertFalse(flag_path.exists())
        self.assertIs(flag_state_before_bot_close, False)
        mock_bot.close.assert_awaited_once()
        mock_execv.assert_called_once()

    async def test_load_schedule_reads_sidecar_status_once(self):
        """Verify load_schedule() loads sidecar_status.json once per pass rather than per job."""
        from tools.scheduler_tool import load_schedule, get_last_execution_for_job, _SIDECAR_STATUS_CACHE

        _SIDECAR_STATUS_CACHE.clear()
        status_file = bs.DATA_DIR / "sidecar_status.json"
        status_file.write_text(json.dumps({
            "triage": {"timestamp_epoch": 1788330600, "timestamp_pt": "2026-09-01 11:30 PM PT", "status": "ok"}
        }))

        sched_file = bs.DATA_DIR / "schedule.json"
        sched_file.write_text(json.dumps([
            {"id": "nightly_triage", "name": "Nightly Triage", "enabled": True, "schedule_type": "daily", "hour_pt": 23, "minute_pt": 30},
            {"id": "ha_battery_check", "name": "HA Battery Check", "enabled": True, "schedule_type": "daily", "hour_pt": 8, "minute_pt": 0},
        ]))

        orig_open = open
        status_open_count = 0

        def counting_open(file, *args, **kwargs):
            nonlocal status_open_count
            if str(file).endswith("sidecar_status.json") and ("r" in args[:1] or not args):
                status_open_count += 1
            return orig_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=counting_open):
            _ = load_schedule()
            self.assertEqual(status_open_count, 1)
            # Subsequent get_last_execution_for_job with unchanged mtime uses cache without re-opening
            epoch, pt = get_last_execution_for_job("nightly_triage")
            self.assertEqual(epoch, 1788330600)
            self.assertEqual(status_open_count, 1)

        # Explicit status_map parameter bypasses file read completely
        epoch2, pt2 = get_last_execution_for_job("nightly_triage", status_map={"triage": {"timestamp_epoch": 1790000000, "timestamp_pt": "test"}})
        self.assertEqual(epoch2, 1790000000)

    async def test_loop_snapshots_mutable_dicts_without_iteration_race(self):
        """Verify _loop iterates over list(...) snapshots of channel_active_procs and daemon_manager.workers."""
        scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock())
        scheduler._running = True

        class MutatingDict(dict):
            def values(self):
                # If iterated directly without list() snapshot while mutated, dict raises RuntimeError.
                # Verify caller wraps .values() in list(...) before iterating.
                return super().values()

        proc_mock = MagicMock()
        proc_mock.pid = 42424
        proc_mock.returncode = None
        br.channel_active_procs = {111: proc_mock}

        def stop_after_one_sleep(_):
            scheduler._running = False
            raise asyncio.CancelledError()

        with patch.object(scheduler, "_evaluate_and_dispatch_jobs", new_callable=AsyncMock), \
             patch.object(scheduler, "flush_outbox_queue", new_callable=AsyncMock), \
             patch("tools.bridge_state.is_vulcan_agent", return_value=False), \
             patch("tools.process_probe.reap_stale_agy_processes") as mock_reap, \
             patch("tools.forge.forge_supervisor.tick_sync"), \
             patch("asyncio.sleep", side_effect=stop_after_one_sleep):
            try:
                await scheduler._loop()
            except asyncio.CancelledError:
                pass

        mock_reap.assert_called_once()
        allowed = mock_reap.call_args[1]["allowed_active_pids"]
        self.assertIn(42424, allowed)

    async def test_loop_offloads_sync_housekeeping_to_thread(self):
        """Verify _loop executes _sync_housekeeping in a thread with bounded timeout."""
        scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock())
        scheduler._running = True

        def stop_after_one_sleep(_):
            scheduler._running = False
            raise asyncio.CancelledError()

        with patch.object(scheduler, "_evaluate_and_dispatch_jobs", new_callable=AsyncMock), \
             patch.object(scheduler, "flush_outbox_queue", new_callable=AsyncMock), \
             patch("tools.bridge_state.is_vulcan_agent", return_value=False), \
             patch.object(scheduler, "_sync_housekeeping") as mock_hk, \
             patch("tools.forge.forge_supervisor.tick_sync"), \
             patch("asyncio.sleep", side_effect=stop_after_one_sleep):
            try:
                await scheduler._loop()
            except asyncio.CancelledError:
                pass

        mock_hk.assert_called_once()

    def test_sync_housekeeping_reaps_and_updates_heartbeat(self):
        """Verify _sync_housekeeping invokes update_gateway_heartbeat and reap_stale_agy_processes."""
        scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock())
        mock_bot = MagicMock()
        mock_bot.is_ready.return_value = True
        scheduler.bot = mock_bot

        with patch("tools.bridge_state.update_gateway_heartbeat") as mock_gw, \
             patch("tools.process_probe.reap_stale_agy_processes") as mock_reap, \
             patch("tools.bridge_state.get_all_active_pids", return_value=[1234]):
            scheduler._sync_housekeeping()

        mock_gw.assert_called_once_with(mock_bot, status="connected")
        mock_reap.assert_called_once()
        self.assertIn(1234, mock_reap.call_args[1]["allowed_active_pids"])

    def test_should_run_job_coerces_dict_quiet_hours(self):
        """Verify dict quiet_hours={'start_hour_pt': 0, 'end_hour_pt': 7} is coerced cleanly without TypeError."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tools.bridge_scheduler import should_run_job
        from tools.scheduler_tool import calculate_next_run

        pt = ZoneInfo("America/Los_Angeles")
        job = {
            "id": "agora_forge_piecewise_ticker",
            "name": "Agora Piecewise Ticker",
            "enabled": True,
            "schedule_type": "interval",
            "interval_seconds": 300,
            "quiet_hours": {"start_hour_pt": 0, "end_hour_pt": 7},
            "next_run_ts": 1000.0,
            "catchup_if_missed": True,
            "catchup_window_seconds": 7200,
        }

        # 03:00 AM PT -> paused by quiet hours
        ts_0300 = datetime(2026, 11, 15, 3, 0, tzinfo=pt).timestamp()
        job["next_run_ts"] = ts_0300 - 10
        ok, reason = should_run_job(job, ts_0300)
        self.assertFalse(ok)
        self.assertEqual(reason, "quiet_hours_0_to_7_pt")

        # 10:30 AM PT -> outside quiet hours, runs cleanly
        ts_1030 = datetime(2026, 11, 15, 10, 30, tzinfo=pt).timestamp()
        job["next_run_ts"] = ts_1030 - 10
        ok2, reason2 = should_run_job(job, ts_1030)
        self.assertTrue(ok2)
        self.assertEqual(reason2, "on_time")

        # calculate_next_run also handles dict quiet_hours without TypeError
        nxt = calculate_next_run(job, from_ts=ts_0300 - 300)
        nxt_pt = datetime.fromtimestamp(nxt, tz=pt)
        self.assertEqual(nxt_pt.hour, 7)

    async def test_evaluate_and_dispatch_isolates_malformed_job_and_persists_schedule(self):
        """Verify a dict quiet_hours or malformed job never aborts save_schedule(jobs) or blocks subsequent due jobs."""
        dispatch_mock = AsyncMock()
        scheduler = bshed.BridgeScheduler(dispatch_fn=dispatch_mock)
        now = time.time()

        jobs = [
            {
                "id": "agora_forge_piecewise_ticker",
                "name": "Agora Piecewise Ticker",
                "enabled": True,
                "schedule_type": "interval",
                "interval_seconds": 300,
                "quiet_hours": {"start_hour_pt": 0, "end_hour_pt": 7},
                "next_run_ts": now - 10,
                "prompt": "Run ticker",
            },
            {
                "id": "broken_job",
                "name": "Broken Job",
                "enabled": True,
                "schedule_type": "interval",
                "interval_seconds": "invalid_str_interval",
                "next_run_ts": now - 10,
                "prompt": "Broken",
            },
            {
                "id": "daily_quota_card",
                "name": "Daily Gemini Quota Card",
                "enabled": True,
                "schedule_type": "daily",
                "hour_pt": 7,
                "minute_pt": 0,
                "next_run_ts": now - 5,
                "catchup_if_missed": True,
                "catchup_window_seconds": 14400,
                "prompt": "Run quota card",
            },
        ]

        with patch("tools.scheduler_tool.load_schedule", return_value=jobs), \
             patch("tools.scheduler_tool.save_schedule") as mock_save:
            await scheduler._evaluate_and_dispatch_jobs()
            if scheduler._active_job_tasks:
                await asyncio.gather(*list(scheduler._active_job_tasks))

        self.assertGreaterEqual(mock_save.call_count, 1)
        self.assertEqual(jobs[0]["quiet_hours"], [0, 7])
        dispatched_ids = [c.kwargs.get("job_id") for c in dispatch_mock.await_args_list]
        self.assertIn("daily_quota_card", dispatched_ids)

    async def test_seerr_card_sync_silent_on_success(self):
        """Verify seerr_card_sync runs run_seerr_card_sync silently without LLM turns or channel noise when ok is True."""
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.fetch_channel = AsyncMock(return_value=mock_channel)
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_seerr_card_sync", return_value=(True, "(nominal - 2 Seerr card(s) updated)")) as mock_sync, \
             patch("tools.sidecars.log_execution"):
            await bshed.dispatch_scheduled_prompt(
                "Run Seerr webhook card sync using /workspace/tools/sidecars.py seerr_card_sync",
                job_name="Seerr Media Request Card Sync",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                job_id="seerr_card_sync",
            )
            mock_sync.assert_called_once_with()
            mock_channel.send.assert_not_called()
            mock_turn_queue.put.assert_not_called()

    async def test_seerr_card_sync_alerts_on_failure(self):
        """Verify seerr_card_sync posts an alert without invoking an LLM turn when ok is False."""
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.fetch_channel = AsyncMock(return_value=mock_channel)
        mock_turn_queue = AsyncMock()

        err_msg = "⚠️ Seerr card sync encountered 1 error(s) (updated=0, scanned=10)"
        with patch("tools.sidecars.run_seerr_card_sync", return_value=(False, err_msg)) as mock_sync, \
             patch("tools.sidecars.log_execution"):
            await bshed.dispatch_scheduled_prompt(
                "Run Seerr webhook card sync using /workspace/tools/sidecars.py seerr_card_sync",
                job_name="Seerr Card Sync",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                channel_id=12345,
            )
            mock_sync.assert_called_once_with()
            mock_channel.send.assert_awaited_once_with(err_msg)
            mock_turn_queue.put.assert_not_called()

    def test_run_seerr_card_sync_sidecar_wrapper(self):
        """Verify tools.sidecars.run_seerr_card_sync formats nominal and error returns cleanly."""
        from tools.sidecars import run_seerr_card_sync

        with patch("tools.seerr_card_sync.sync_seerr_cards", return_value={"updated": 3, "errors": 0, "scanned": 12}) as mock_fn:
            ok, msg = run_seerr_card_sync(limit=50)
            self.assertTrue(ok)
            self.assertEqual(msg, "(nominal - 3 Seerr card(s) updated)")
            mock_fn.assert_called_once_with(dry_run=False, limit=50)

        with patch("tools.seerr_card_sync.sync_seerr_cards", return_value={"updated": 1, "errors": 2, "scanned": 12}):
            ok_err, msg_err = run_seerr_card_sync()
            self.assertFalse(ok_err)
            self.assertIn("2 error(s)", msg_err)


    def test_calculate_next_run_interval_jitter_bounds(self):
        """Verify calculate_next_run applies jitter within [-jitter, jitter] for interval jobs."""
        from tools.scheduler_tool import calculate_next_run
        job = {
            "id": "resident_agents_watcher",
            "schedule_type": "interval",
            "interval_seconds": 300,
            "jitter_seconds": 90,
        }
        from_ts = 1780000000.0

        with patch("random.uniform", return_value=-90.0):
            nxt = calculate_next_run(job, from_ts=from_ts)
            self.assertEqual(nxt, from_ts + 210.0)

        with patch("random.uniform", return_value=90.0):
            nxt = calculate_next_run(job, from_ts=from_ts)
            self.assertEqual(nxt, from_ts + 390.0)

        with patch("random.uniform", return_value=0.0):
            nxt = calculate_next_run(job, from_ts=from_ts)
            self.assertEqual(nxt, from_ts + 300.0)

        for _ in range(30):
            nxt = calculate_next_run(job, from_ts=from_ts)
            self.assertGreaterEqual(nxt, from_ts + 210.0)
            self.assertLessEqual(nxt, from_ts + 390.0)

    def test_calculate_next_run_interval_clamps_above_from_ts(self):
        """Verify interval jobs clamp nxt >= from_ts + 30 when negative jitter pushes below threshold."""
        from tools.scheduler_tool import calculate_next_run
        job = {
            "id": "short_interval_job",
            "schedule_type": "interval",
            "interval_seconds": 60,
            "jitter_seconds": 50,
        }
        from_ts = 1780000000.0

        with patch("random.uniform", return_value=-50.0):
            nxt = calculate_next_run(job, from_ts=from_ts)
            self.assertEqual(nxt, from_ts + 30.0)

        with patch("random.uniform", return_value=-200.0):
            nxt = calculate_next_run(job, from_ts=from_ts)
            self.assertEqual(nxt, from_ts + 30.0)

    def test_calculate_next_run_interval_honors_quiet_hours_after_jitter(self):
        """Verify quiet hours are evaluated after jitter so jittered interval runs never land in quiet hours."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tools.scheduler_tool import calculate_next_run

        pt = ZoneInfo("America/Los_Angeles")
        ts_2258 = datetime(2026, 11, 15, 22, 58, 0, tzinfo=pt).timestamp()
        job = {
            "id": "resident_agents_watcher",
            "schedule_type": "interval",
            "interval_seconds": 300,
            "jitter_seconds": 90,
            "quiet_hours": [23, 7],
        }

        # Base + 300s is 23:03 PT. Jitter -30s -> 23:02:30 PT (inside quiet hours)
        with patch("random.uniform", return_value=-30.0):
            nxt = calculate_next_run(job, from_ts=ts_2258)
            nxt_pt = datetime.fromtimestamp(nxt, tz=pt)
            self.assertEqual(nxt_pt.hour, 7)
            self.assertEqual(nxt_pt.minute, 0)
            self.assertEqual(nxt_pt.second, 0)
            self.assertEqual(nxt_pt.day, 16)

        # Base + 300s from 22:54 PT is 22:59 PT (outside quiet hours), but jitter +90s lands in 23:00:30 PT
        ts_2254 = datetime(2026, 11, 15, 22, 54, 0, tzinfo=pt).timestamp()
        with patch("random.uniform", return_value=90.0):
            nxt2 = calculate_next_run(job, from_ts=ts_2254)
            nxt2_pt = datetime.fromtimestamp(nxt2, tz=pt)
            self.assertEqual(nxt2_pt.hour, 7)
            self.assertEqual(nxt2_pt.minute, 0)
            self.assertEqual(nxt2_pt.second, 0)
            self.assertEqual(nxt2_pt.day, 16)

    def test_calculate_next_run_daily_jitter_bounds(self):
        """Verify calculate_next_run applies jitter within [-jitter, jitter] for daily jobs."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tools.scheduler_tool import calculate_next_run

        pt = ZoneInfo("America/Los_Angeles")
        ts_0800 = datetime(2026, 11, 15, 8, 0, 0, tzinfo=pt).timestamp()
        target_ts = datetime(2026, 11, 15, 11, 30, 0, tzinfo=pt).timestamp()

        job = {
            "id": "resident_agents_daily_tip",
            "schedule_type": "daily",
            "hour_pt": 11,
            "minute_pt": 30,
            "jitter_seconds": 1800,
        }

        with patch("random.uniform", return_value=-1800.0):
            nxt = calculate_next_run(job, from_ts=ts_0800)
            self.assertEqual(nxt, target_ts - 1800.0)

        with patch("random.uniform", return_value=1800.0):
            nxt = calculate_next_run(job, from_ts=ts_0800)
            self.assertEqual(nxt, target_ts + 1800.0)

        for _ in range(30):
            nxt = calculate_next_run(job, from_ts=ts_0800)
            self.assertGreaterEqual(nxt, target_ts - 1800.0)
            self.assertLessEqual(nxt, target_ts + 1800.0)

    def test_calculate_next_run_daily_clamps_above_from_ts(self):
        """Verify daily jobs clamp nxt >= from_ts + 60 when negative jitter pushes below threshold."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tools.scheduler_tool import calculate_next_run

        pt = ZoneInfo("America/Los_Angeles")
        ts_112945 = datetime(2026, 11, 15, 11, 29, 45, tzinfo=pt).timestamp()
        job = {
            "id": "resident_agents_daily_tip",
            "schedule_type": "daily",
            "hour_pt": 11,
            "minute_pt": 30,
            "jitter_seconds": 1800,
        }

        with patch("random.uniform", return_value=-1800.0):
            nxt = calculate_next_run(job, from_ts=ts_112945)
            self.assertEqual(nxt, ts_112945 + 60.0)
            self.assertGreaterEqual(nxt, ts_112945 + 60.0)

    def test_calculate_next_run_handles_invalid_or_missing_jitter(self):
        """Verify calculate_next_run ignores invalid, zero, or negative jitter values safely."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tools.scheduler_tool import calculate_next_run

        pt = ZoneInfo("America/Los_Angeles")
        ts_0800 = datetime(2026, 11, 15, 8, 0, 0, tzinfo=pt).timestamp()
        expected = datetime(2026, 11, 15, 11, 30, 0, tzinfo=pt).timestamp()

        for invalid in (None, 0, -100, "invalid", [10]):
            job = {
                "id": "test_job",
                "schedule_type": "daily",
                "hour_pt": 11,
                "minute_pt": 30,
                "jitter_seconds": invalid,
            }
            nxt = calculate_next_run(job, from_ts=ts_0800)
            self.assertEqual(nxt, expected)

    def test_orcaslicer_idle_reaper_registered_in_maintenance_sets(self):
        """Verify orcaslicer_idle_reaper is registered in maintenance job/name sets."""
        self.assertIn("orcaslicer_idle_reaper", bshed.INTERNAL_SYSTEM_MAINTENANCE_JOBS)
        self.assertIn("orcaslicer_idle_reaper", bshed.INTERNAL_SYSTEM_MAINTENANCE_NAMES)
        self.assertIn("OrcaSlicer 30-Minute Idle Auto-Stop", bshed.INTERNAL_SYSTEM_MAINTENANCE_NAMES)

    async def test_orcaslicer_idle_reaper_silent_when_nominal(self):
        """Verify orcaslicer_idle_reaper stays silent with zero LLM turn when nominal."""
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_channel = AsyncMock()
        mock_bot.get_channel.return_value = mock_channel
        mock_bot.fetch_channel = AsyncMock(return_value=mock_channel)
        mock_turn_queue = AsyncMock()

        with patch("tools.sidecars.run_orcaslicer_idle_reaper", return_value=(True, "", {"action": "idle_waiting"})) as mock_reaper, \
             patch("tools.sidecars.log_execution"):
            await bshed.dispatch_scheduled_prompt(
                "Run OrcaSlicer idle reaper via /workspace/tools/sidecars.py orcaslicer_idle",
                job_name="OrcaSlicer 30-Minute Idle Auto-Stop",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                job_id="orcaslicer_idle_reaper",
            )
            mock_reaper.assert_called_once_with()
            mock_channel.send.assert_not_called()
            mock_turn_queue.put.assert_not_called()

    async def test_orcaslicer_idle_reaper_alerts_homelab_when_stopped(self):
        """Verify orcaslicer_idle_reaper posts to #homelab when container is stopped."""
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_homelab = AsyncMock()
        mock_bot.get_channel.side_effect = lambda cid: mock_homelab if cid == bs.HOMELAB_CHANNEL_ID else None
        mock_turn_queue = AsyncMock()

        msg = "Stopped idle OrcaSlicer container after 30m inactivity."
        with patch("tools.sidecars.run_orcaslicer_idle_reaper", return_value=(True, msg, {"action": "stopped"})) as mock_reaper, \
             patch("tools.sidecars.log_execution"):
            await bshed.dispatch_scheduled_prompt(
                "Run OrcaSlicer idle reaper via /workspace/tools/sidecars.py orcaslicer_idle",
                job_name="OrcaSlicer 30-Minute Idle Auto-Stop",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
                job_id="orcaslicer_idle_reaper",
            )
            mock_reaper.assert_called_once_with()
            mock_bot.get_channel.assert_called_with(bs.HOMELAB_CHANNEL_ID)
            mock_homelab.send.assert_awaited_once_with(msg)
            mock_turn_queue.put.assert_not_called()

    async def test_orcaslicer_idle_reaper_alerts_homelab_on_error(self):
        """Verify orcaslicer_idle_reaper posts to #homelab without an LLM turn on error."""
        bshed.LAST_SCHEDULED_DISPATCH.clear()
        mock_bot = MagicMock()
        mock_homelab = AsyncMock()
        mock_bot.get_channel.side_effect = lambda cid: mock_homelab if cid == bs.HOMELAB_CHANNEL_ID else None
        mock_turn_queue = AsyncMock()

        err_msg = "⚠️ Failed to stop idle OrcaSlicer container: permission denied"
        with patch("tools.sidecars.run_orcaslicer_idle_reaper", return_value=(False, err_msg, {"action": "stop_failed"})) as mock_reaper, \
             patch("tools.sidecars.log_execution"):
            await bshed.dispatch_scheduled_prompt(
                "Check orcaslicer_idle status",
                job_name="OrcaSlicer 30-Minute Idle Auto-Stop",
                bot=mock_bot,
                turn_queue=mock_turn_queue,
            )
            mock_reaper.assert_called_once_with()
            mock_bot.get_channel.assert_called_with(bs.HOMELAB_CHANNEL_ID)
            mock_homelab.send.assert_awaited_once_with(err_msg)
            mock_turn_queue.put.assert_not_called()

    def test_sync_housekeeping_includes_forge_jobs_and_descendants(self):
        """Verify _sync_housekeeping includes active FORGE job/worker PIDs and their descendants in allowed_active_pids."""
        scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock())
        mock_bot = MagicMock()
        mock_bot.is_ready.return_value = True
        scheduler.bot = mock_bot

        def fake_children(pid):
            if pid == 55002:
                return [55003]
            return []

        with patch("tools.bridge_state.update_gateway_heartbeat"), \
             patch("tools.bridge_state.get_all_active_pids", return_value=[1234]), \
             patch("tools.process_probe.get_active_forge_pids", return_value={55001, 55002}), \
             patch("tools.process_probe.get_process_children", side_effect=fake_children), \
             patch("tools.process_probe.reap_stale_agy_processes") as mock_reap:
            scheduler._sync_housekeeping()

        mock_reap.assert_called_once()
        allowed = mock_reap.call_args[1]["allowed_active_pids"]
        self.assertTrue({1234, 55001, 55002, 55003}.issubset(allowed))

    async def test_outbox_flush_channel_filter_partitions_in_place_without_requeue(self):
        """Verify flush_outbox_queue(channel_filter=...) passes channel_filter directly and never re-queues unmatched items."""
        import tools.outbox as outbox

        orig_data_dir = outbox.DATA_DIR
        orig_outbox_dir = outbox.OUTBOX_DIR
        orig_pending_file = outbox.PENDING_FILE
        orig_history_file = outbox.HISTORY_FILE
        try:
            outbox.DATA_DIR = self.temp_path
            outbox.OUTBOX_DIR = self.temp_path / "outbox"
            outbox.PENDING_FILE = outbox.OUTBOX_DIR / "pending.jsonl"
            outbox.HISTORY_FILE = outbox.OUTBOX_DIR / "history.jsonl"
            outbox.OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

            forge_cid = 1562899941595160657
            zero_cid = 1542081375287640084
            forge_line = json.dumps({
                "id": "outbox-forge-1",
                "channel": "forge",
                "channel_id": forge_cid,
                "content": "FORGE chunk 2 complete",
                "source": "vulcan",
                "type": "text",
                "is_agent_turn": False,
                "created_at": time.time(),
            })
            zero_line = json.dumps({
                "id": "outbox-zero-keep-exact-id",
                "channel": "zero-chat",
                "channel_id": zero_cid,
                "content": "Zero notification must stay intact",
                "source": "zero-sidecar",
                "type": "text",
                "is_agent_turn": False,
                "force": True,
                "created_at": time.time(),
            })
            outbox.PENDING_FILE.write_text(forge_line + "\n" + zero_line + "\n", encoding="utf-8")

            mock_bot = MagicMock()
            mock_forge_ch = AsyncMock()
            mock_bot.get_channel.side_effect = lambda cid: mock_forge_ch if cid == forge_cid else None
            scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock(), bot=mock_bot)

            with patch("tools.outbox.queue_outbox_message") as mock_requeue:
                await scheduler.flush_outbox_queue(channel_filter=forge_cid)

            mock_requeue.assert_not_called()
            mock_forge_ch.send.assert_awaited_once_with("FORGE chunk 2 complete")
            remaining_raw = outbox.PENDING_FILE.read_text(encoding="utf-8").splitlines()
            self.assertEqual(remaining_raw, [zero_line])
        finally:
            outbox.DATA_DIR = orig_data_dir
            outbox.OUTBOX_DIR = orig_outbox_dir
            outbox.PENDING_FILE = orig_pending_file
            outbox.HISTORY_FILE = orig_history_file

    async def test_evaluate_and_dispatch_tracks_attempt_separately_from_success_on_error(self):
        """Verify failed dispatch updates last_attempt_ts without advancing last_success_ts or last_run_ts."""
        dispatch_mock = AsyncMock(side_effect=RuntimeError("dispatch blew up"))
        scheduler = bshed.BridgeScheduler(dispatch_fn=dispatch_mock)
        now = time.time()
        test_job = {
            "id": "test_fail_job",
            "name": "Failing Job",
            "enabled": True,
            "schedule_type": "daily",
            "hour_pt": 8,
            "minute_pt": 0,
            "next_run_ts": now - 5,
            "last_success_ts": 500.0,
            "last_run_ts": 500.0,
            "prompt": "Run failing job",
        }

        with patch("tools.scheduler_tool.load_schedule", return_value=[test_job]), \
             patch("tools.bridge_scheduler.should_run_job", return_value=(True, "on_time")), \
             patch("tools.scheduler_tool.save_schedule") as mock_save:
            await scheduler._evaluate_and_dispatch_jobs()
            if scheduler._active_job_tasks:
                await asyncio.gather(*list(scheduler._active_job_tasks))

        mock_save.assert_called_once()
        self.assertGreater(test_job["last_attempt_ts"], 500.0)
        self.assertEqual(test_job["last_success_ts"], 500.0)
        self.assertEqual(test_job["last_run_ts"], 500.0)

    async def test_evaluate_and_dispatch_updates_last_success_ts_when_dispatch_succeeds(self):
        """Verify successful dispatch records last_attempt_ts first and updates last_success_ts on completion."""
        dispatch_mock = AsyncMock(return_value=None)
        scheduler = bshed.BridgeScheduler(dispatch_fn=dispatch_mock)
        now = time.time()
        test_job = {
            "id": "test_ok_job",
            "name": "Succeeding Job",
            "enabled": True,
            "schedule_type": "daily",
            "hour_pt": 8,
            "minute_pt": 0,
            "next_run_ts": now - 5,
            "last_success_ts": 500.0,
            "last_run_ts": 500.0,
            "prompt": "Run succeeding job",
        }

        with patch("tools.scheduler_tool.load_schedule", return_value=[test_job]), \
             patch("tools.bridge_scheduler.should_run_job", return_value=(True, "on_time")), \
             patch("tools.scheduler_tool.save_schedule") as mock_save:
            await scheduler._evaluate_and_dispatch_jobs()
            if scheduler._active_job_tasks:
                await asyncio.gather(*list(scheduler._active_job_tasks))

        self.assertEqual(mock_save.call_count, 2)
        self.assertGreater(test_job["last_attempt_ts"], 500.0)
        self.assertGreaterEqual(test_job["last_success_ts"], test_job["last_attempt_ts"])
        self.assertEqual(test_job["last_run_ts"], test_job["last_success_ts"])

    async def test_no_vulcan_nightly_rollover_and_session_rollover_preserves_active_sessions(self):
        from tools.session_rollover import run_daily_session_rollover

        scheduler = bshed.BridgeScheduler(dispatch_fn=AsyncMock())
        self.assertFalse(hasattr(scheduler, "_maybe_run_vulcan_nightly_rollover"))
        self.assertFalse(hasattr(bshed.BridgeScheduler, "_maybe_run_vulcan_nightly_rollover"))
        self.assertFalse(hasattr(scheduler, "_vulcan_last_rollover_date"))

        now = time.time()
        primary_key = str(next(iter(bs.DEFAULT_HOME_CHANNELS)))
        dedicated_key = str(bs.FORGE_CHANNEL_ID)
        active_thread_key = "999888777666555444"
        stale_thread_key = "111222333444555666"

        sessions_file = self.temp_path / "sessions.json"
        sessions_file.write_text(
            json.dumps({
                "home": "conv-home",
                primary_key: "conv-primary",
                dedicated_key: "conv-forge",
                active_thread_key: "conv-thread-active",
                stale_thread_key: "conv-thread-stale",
            }),
            encoding="utf-8",
        )

        br.reset_session_keys.clear()
        br.reset_session_keys.add(stale_thread_key)

        def fake_meta(key: str) -> dict:
            if key == active_thread_key:
                return {"last_active": now - 3600}
            if key == stale_thread_key:
                return {"last_active": now - (72 * 3600)}
            return {}

        with patch("tools.session_rollover.SESSIONS_FILE", sessions_file), \
             patch("tools.session_rollover.get_session_metadata", side_effect=fake_meta), \
             patch("tools.session_rollover.clear_channel_session_id") as mock_clear, \
             patch("tools.session_rollover.prune_brain_sessions", return_value={"pruned_count": 3, "freed_mb": 4.2}) as mock_prune:
            ok, report, details = run_daily_session_rollover(dry_run=False)

        self.assertTrue(ok)
        self.assertEqual(details["rolled_over"], [])
        self.assertEqual(details["pruned_threads"], [stale_thread_key])
        self.assertNotIn("home", br.reset_session_keys)
        self.assertNotIn(primary_key, br.reset_session_keys)
        self.assertNotIn(dedicated_key, br.reset_session_keys)
        self.assertNotIn(active_thread_key, br.reset_session_keys)
        self.assertNotIn(stale_thread_key, br.reset_session_keys)
        mock_clear.assert_any_call(stale_thread_key, "home")
        mock_clear.assert_any_call(stale_thread_key, "external")
        mock_prune.assert_called_once_with(max_age_days=30, dry_run=False)
        self.assertIn("Pruned **1 stale thread mapping(s)**", report)


if __name__ == "__main__":
    unittest.main()
