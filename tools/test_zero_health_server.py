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

    def test_health_evaluation_stale_gateway_heartbeat(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 240,  # 4 minutes old (> 180s)
            "gateway_status": "connected",
            "gateway_latency_ms": 42.5,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        # Default mode returns 200 with turn_state OFFLINE for clean dashboard rendering
        code, payload = handler._evaluate_health()
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["turn_state"], "OFFLINE")
        self.assertEqual(payload["components"]["discord_gateway"], "stalled_or_disconnected")
        self.assertGreater(payload["components"]["gateway_heartbeat_age_seconds"], 180)

        # Strict mode returns 503 for monitoring checks
        code_strict, _ = handler._evaluate_health(strict=True)
        self.assertEqual(code_strict, 503)

    def test_health_evaluation_disconnected_gateway(self):
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
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["turn_state"], "OFFLINE")
        self.assertEqual(payload["components"]["discord_gateway"], "stalled_or_disconnected")

        code_strict, _ = handler._evaluate_health(strict=True)
        self.assertEqual(code_strict, 503)

    def test_health_evaluation_enriched_status_fields(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 5,
            "gateway_status": "connected",
            "gateway_latency_ms": 35.0,
            "state": "PROCESSING"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        # Write dummy in-flight turn with self PID
        in_flight_data = {
            "123": {
                "channel_id": 123,
                "prompt": "Test prompt",
                "ts": now - 20,
                "pid": os.getpid()
            }
        }
        (self.temp_path / "in_flight_turn.json").write_text(json.dumps(in_flight_data))

        # Write dummy detached tasks (1 running)
        detached_dir = self.temp_path / "detached_tasks" / "task_abc"
        detached_dir.mkdir(parents=True, exist_ok=True)
        (detached_dir / "meta.json").write_text(json.dumps({
            "task_id": "task_abc",
            "status": "running",
            "child_pid": os.getpid(),
            "start_time": now - 50
        }))

        # Write dummy daemon pids
        (self.temp_path / "daemon_pids.json").write_text(json.dumps({"pids": [os.getpid()]}))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()

        self.assertEqual(code, 200)
        self.assertEqual(payload["turn_state"], "PROCESSING")
        # in_flight_turns should be sum of 1 active turn + 1 detached task = 2
        self.assertEqual(payload["in_flight_turns"], 2)
        self.assertEqual(payload["active_turns"], 1)
        self.assertEqual(payload["active_detached_tasks"], 1)
        self.assertEqual(payload["daemon_workers"], 1)
        self.assertIn("active_model", payload)
        self.assertIn("model_short", payload)
        self.assertIn("messages_sent", payload)
        self.assertIn("uptime_human", payload)
        self.assertEqual(len(payload["in_flight"]), 1)
        self.assertEqual(payload["in_flight"][0]["prompt"], "Test prompt")

    def test_health_evaluation_custom_bot_stats(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 5,
            "gateway_status": "connected",
            "gateway_latency_ms": 12.0,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))
        (self.temp_path / "bot_stats.json").write_text(json.dumps({"all_time_messages_sent": 2450}))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()

        self.assertEqual(code, 200)
        self.assertEqual(payload["messages_sent"], 2450)
        self.assertEqual(payload["components"]["messages_sent"], 2450)

    @patch("tools.zero_health_server.run_ssh")
    def test_ivy_evaluation_success(self, mock_ssh):
        zhealth.IVY_CACHE = {"ts": 0.0, "status_code": 200, "payload": None}
        mock_output = json.dumps({
            "container_status": "running",
            "session_meta": {"turns": 14},
            "runtime_config": {"model": "gemini-3.8-flash-high"},
            "bot_stats": {"all_time_messages_sent": 820},
            "in_flight": {"ch_1": {"prompt": "Analyze Ohtani", "ts": time.time() - 10}},
            "active_detached_tasks": 1,
            "daemon_running": True
        })
        mock_ssh.return_value = (0, mock_output, "")

        code, payload = zhealth._evaluate_ivy(force=True)

        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["agent"], "Ivy")
        self.assertEqual(payload["turn_state"], "PROCESSING")
        self.assertEqual(payload["container_status"], "running")
        self.assertEqual(payload["model_short"], "3.8 Flash")
        self.assertEqual(payload["session_turns"], 14)
        # Sum of 1 active turn + 1 detached task = 2
        self.assertEqual(payload["in_flight_turns"], 2)
        self.assertEqual(payload["active_turns"], 1)
        self.assertEqual(payload["active_detached_tasks"], 1)
        self.assertEqual(payload["messages_sent"], 820)
        self.assertTrue(payload["daemon_running"])

    @patch("tools.zero_health_server.run_ssh")
    def test_ivy_evaluation_failure_offline_200_and_strict_503(self, mock_ssh):
        zhealth.IVY_CACHE = {"ts": 0.0, "status_code": 200, "payload": None}
        mock_ssh.return_value = (1, "", "Connection refused")

        # Default mode returns 200 with turn_state OFFLINE
        code, payload = zhealth._evaluate_ivy(force=True)
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["turn_state"], "OFFLINE")
        self.assertEqual(payload["container_status"], "unreachable")
        self.assertIn("SSH execution failed", payload["error"])

        # Strict mode returns 503
        code_strict, _ = zhealth._evaluate_ivy(force=True, strict=True)
        self.assertEqual(code_strict, 503)

    @patch("tools.zero_health_server.run_ssh")
    def test_ivy_evaluation_caching(self, mock_ssh):
        zhealth.IVY_CACHE = {"ts": 0.0, "status_code": 200, "payload": None}
        mock_output = json.dumps({
            "container_status": "running",
            "session_meta": {"turns": 14},
            "runtime_config": {"model": "gemini-3.7-flash-high"},
            "daemon_running": True
        })
        mock_ssh.return_value = (0, mock_output, "")

        code1, payload1 = zhealth._evaluate_ivy(force=True)
        self.assertEqual(mock_ssh.call_count, 1)

        # Immediate second call should hit cache without calling run_ssh again
        code2, payload2 = zhealth._evaluate_ivy(force=False)
        self.assertEqual(mock_ssh.call_count, 1)
        self.assertEqual(code1, code2)
        self.assertEqual(payload1, payload2)


if __name__ == "__main__":
    unittest.main()
