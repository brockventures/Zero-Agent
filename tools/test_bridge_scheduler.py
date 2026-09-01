#!/usr/bin/env python3
"""
Unit test suite for bridge_scheduler.py (Karakos Background Scheduler & Sidecar Dispatcher).
"""

import asyncio
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
