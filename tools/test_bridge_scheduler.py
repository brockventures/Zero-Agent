#!/usr/bin/env python3
"""
Unit test suite for bridge_scheduler.py (Karakos Background Scheduler & Sidecar Dispatcher).
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

        with patch("tools.session_summarizer.generate_summary") as mock_sum:
            await bshed.dispatch_scheduled_prompt("[INTERNAL_SESSION_ROLLOVER]", "Daily Session Rollover")
            self.assertIn("home", br.reset_session_keys)

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

    async def test_scheduler_lifecycle(self):
        dispatch_mock = AsyncMock()
        scheduler = bshed.KarakosScheduler(dispatch_fn=dispatch_mock)
        with patch("tools.scheduler_tool.load_schedule", return_value=[]):
            await scheduler.start()
            self.assertTrue(scheduler._running)
            await scheduler.stop()
            self.assertFalse(scheduler._running)


if __name__ == "__main__":
    unittest.main()


