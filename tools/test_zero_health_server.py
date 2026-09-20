#!/usr/bin/env python3
"""
Unit test suite for tools/zero_health_server.py.
Validates dynamic gateway heartbeat evaluation, degraded HTTP 503 response, and healthy HTTP 200 response.
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

import tools.zero_health_server as zhealth


class TestZeroHealthServer(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        zhealth.DATA_DIR = self.temp_path

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_health_evaluation_healthy(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 10,
            "gateway_status": "connected",
            "gateway_latency_ms": 42.5,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()

        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["components"]["discord_gateway"], "connected")
        self.assertLessEqual(payload["components"]["gateway_heartbeat_age_seconds"], 15)

    def test_health_evaluation_stale_gateway_heartbeat_503(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 240,  # 4 minutes old (> 180s)
            "gateway_status": "connected",
            "gateway_latency_ms": 42.5,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()

        self.assertEqual(code, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["components"]["discord_gateway"], "stalled_or_disconnected")
        self.assertGreater(payload["components"]["gateway_heartbeat_age_seconds"], 180)

    def test_health_evaluation_disconnected_gateway_503(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 10,
            "gateway_status": "disconnected",
            "gateway_latency_ms": None,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()

        self.assertEqual(code, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["components"]["discord_gateway"], "stalled_or_disconnected")


if __name__ == "__main__":
    unittest.main()
