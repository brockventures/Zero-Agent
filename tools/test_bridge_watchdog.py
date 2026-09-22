#!/usr/bin/env python3
"""
Unit test suite for tools/bridge_watchdog.py.
Validates detection of stale gateway heartbeat, stuck PROCESSING states, and automated self-healing.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

import tools.bridge_watchdog as bwatch


class TestBridgeWatchdog(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.orig_beacon = bwatch.BEACON_FILE
        self.orig_flag = bwatch.RELOAD_FLAG
        bwatch.BEACON_FILE = self.temp_path / "liveness_beacon.json"
        bwatch.RELOAD_FLAG = self.temp_path / "reload_bridge.flag"

    def tearDown(self):
        bwatch.BEACON_FILE = self.orig_beacon
        bwatch.RELOAD_FLAG = self.orig_flag
        self.temp_dir.cleanup()

    def test_check_bridge_health_nominal(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 15,
            "gateway_status": "connected",
            "gateway_latency_ms": 35.0,
            "state": "IDLE"
        }
        bwatch.BEACON_FILE.write_text(json.dumps(beacon_data))

        with patch("tools.bridge_watchdog.reap_stale_agy_processes", return_value=[]):
            healthy, summary, details = bwatch.check_bridge_health(auto_heal=True)
            self.assertTrue(healthy)
            self.assertIn("healthy", summary)
            self.assertFalse(bwatch.RELOAD_FLAG.exists())

    def test_check_bridge_health_stale_gateway_auto_heals(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 300,  # 5 minutes stale (> 180s)
            "gateway_status": "connected",
            "gateway_latency_ms": 35.0,
            "state": "IDLE"
        }
        bwatch.BEACON_FILE.write_text(json.dumps(beacon_data))

        with patch("tools.bridge_watchdog.reap_stale_agy_processes", return_value=[]):
            healthy, summary, details = bwatch.check_bridge_health(auto_heal=True)
            self.assertFalse(healthy)
            self.assertIn("stale", summary)
            self.assertTrue(bwatch.RELOAD_FLAG.exists())
            self.assertTrue(details["self_healed"])

    def test_check_bridge_health_stuck_processing_auto_heals(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 10,
            "gateway_status": "connected",
            "state": "PROCESSING",
            "ts": now - 900  # 15 minutes processing (> 600s)
        }
        bwatch.BEACON_FILE.write_text(json.dumps(beacon_data))

        with patch("tools.bridge_watchdog.reap_stale_agy_processes", return_value=[]), \
             patch("tools.bridge_watchdog.get_all_active_pids", return_value=set()):
            healthy, summary, details = bwatch.check_bridge_health(auto_heal=True)
            self.assertFalse(healthy)
            self.assertIn("PROCESSING", summary)
            self.assertTrue(bwatch.RELOAD_FLAG.exists())


if __name__ == "__main__":
    unittest.main()
