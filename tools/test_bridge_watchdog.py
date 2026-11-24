#!/usr/bin/env python3
"""
Unit test suite for tools/bridge_watchdog.py.
Validates detection of stale gateway heartbeat, stuck PROCESSING states, and automated self-healing.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.append(str(WORKSPACE))

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

        with patch("tools.bridge_watchdog.reap_stale_agy_processes", return_value=[]), \
             patch("tools.bridge_watchdog.check_brief_daemon_supervision", return_value={"checked": True, "status": "running", "pid": 123, "restarted": False}), \
             patch("tools.bridge_watchdog.check_subdaemons_supervision", return_value={"checked": True, "status": "running", "daemons": {}, "restarted": []}):
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

        with patch("tools.bridge_watchdog.reap_stale_agy_processes", return_value=[]), \
             patch("tools.bridge_watchdog.check_brief_daemon_supervision", return_value={"checked": True, "status": "running", "pid": 123, "restarted": False}), \
             patch("tools.bridge_watchdog.check_subdaemons_supervision", return_value={"checked": True, "status": "running", "daemons": {}, "restarted": []}):
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
             patch("tools.bridge_watchdog.get_all_active_pids", return_value=set()), \
             patch("tools.bridge_watchdog.check_brief_daemon_supervision", return_value={"checked": True, "status": "running", "pid": 123, "restarted": False}), \
             patch("tools.bridge_watchdog.check_subdaemons_supervision", return_value={"checked": True, "status": "running", "daemons": {}, "restarted": []}):
            healthy, summary, details = bwatch.check_bridge_health(auto_heal=True)
            self.assertFalse(healthy)
            self.assertIn("PROCESSING", summary)
            self.assertTrue(bwatch.RELOAD_FLAG.exists())

    def _write_brief_config(self) -> tuple[Path, Path, Path]:
        mem_dir = self.temp_path / "brief_memory"
        state_dir = self.temp_path / "brief_state"
        mem_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = mem_dir / "config.toml"
        cfg_path.write_text(
            '[channel]\nchannel_id = 1574655816236269569\n[safety]\ndry_run = true\n',
            encoding="utf-8",
        )
        return cfg_path, state_dir, mem_dir

    def test_brief_supervision_skips_in_vulcan_mode(self):
        cfg_path, state_dir, mem_dir = self._write_brief_config()
        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=True), \
             patch("tools.brief.daemon.ensure_brief_daemon_running") as m_ens:
            res = bwatch.check_brief_daemon_supervision(
                auto_heal=True,
                config_path=cfg_path,
                state_dir=state_dir,
                memory_dir=mem_dir,
            )
        self.assertEqual(res, {"checked": False, "status": "skipped_vulcan"})
        m_ens.assert_not_called()

    def test_brief_supervision_skips_when_unconfigured(self):
        missing_cfg = self.temp_path / "missing_config.toml"
        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=False), \
             patch("tools.brief.daemon.ensure_brief_daemon_running") as m_ens:
            res = bwatch.check_brief_daemon_supervision(
                auto_heal=True,
                config_path=missing_cfg,
                state_dir=self.temp_path / "brief_state",
                memory_dir=self.temp_path / "brief_memory",
            )
        self.assertEqual(res, {"checked": False, "status": "unconfigured"})
        m_ens.assert_not_called()

    def test_brief_supervision_respects_kill_switch(self):
        cfg_path, state_dir, mem_dir = self._write_brief_config()
        (state_dir / "kill_switch.flag").write_text("paused\n", encoding="utf-8")
        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=False), \
             patch("tools.brief.daemon.ensure_brief_daemon_running") as m_ens, \
             patch("tools.brief.watchdog.run_watchdog") as m_wd:
            res = bwatch.check_brief_daemon_supervision(
                auto_heal=True,
                config_path=cfg_path,
                state_dir=state_dir,
                memory_dir=mem_dir,
            )
        self.assertEqual(
            res,
            {"checked": True, "status": "paused_kill_switch", "restarted": False},
        )
        m_ens.assert_not_called()
        m_wd.assert_not_called()

    def test_brief_supervision_auto_restarts_dead_daemon_without_reload_flag(self):
        cfg_path, state_dir, mem_dir = self._write_brief_config()
        now = time.time()
        bwatch.BEACON_FILE.write_text(json.dumps({
            "gateway_heartbeat": now - 10,
            "gateway_status": "connected",
            "gateway_latency_ms": 20.0,
            "state": "IDLE",
        }))

        orig_supervise = bwatch.check_brief_daemon_supervision

        def fake_supervise(auto_heal: bool = True):
            return orig_supervise(
                auto_heal=auto_heal,
                config_path=cfg_path,
                state_dir=state_dir,
                memory_dir=mem_dir,
            )

        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=False), \
             patch("tools.bridge_watchdog.reap_stale_agy_processes", return_value=[]), \
             patch("tools.brief.daemon.get_daemon_pid", side_effect=[None, 54321]), \
             patch("tools.brief.daemon.ensure_brief_daemon_running", return_value=True) as m_ens, \
             patch("tools.brief.watchdog.run_watchdog", return_value={"healthy": True, "alert_dispatched": False}) as m_wd, \
             patch("tools.bridge_watchdog.check_brief_daemon_supervision", side_effect=fake_supervise), \
             patch("tools.bridge_watchdog.check_subdaemons_supervision", return_value={"checked": True, "status": "running", "daemons": {}, "restarted": []}):
            healthy, summary, details = bwatch.check_bridge_health(auto_heal=True)

        self.assertTrue(healthy)
        self.assertFalse(bwatch.RELOAD_FLAG.exists())
        self.assertFalse(details["self_healed"])
        self.assertEqual(details["brief_supervision"]["status"], "restarted")
        self.assertEqual(details["brief_supervision"]["pid"], 54321)
        self.assertTrue(details["brief_supervision"]["restarted"])
        self.assertIn("BRIEF", summary)
        m_ens.assert_called_once()
        m_wd.assert_called_once()
        self.assertFalse(m_wd.call_args.kwargs["dry_run"])

    def test_brief_supervision_no_heal_reports_stopped_and_survives_watchdog_error(self):
        cfg_path, state_dir, mem_dir = self._write_brief_config()
        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=False), \
             patch("tools.brief.daemon.get_daemon_pid", return_value=None), \
             patch("tools.brief.daemon.ensure_brief_daemon_running") as m_ens, \
             patch("tools.brief.watchdog.run_watchdog", side_effect=RuntimeError("wd boom")) as m_wd:
            res = bwatch.check_brief_daemon_supervision(
                auto_heal=False,
                config_path=cfg_path,
                state_dir=state_dir,
                memory_dir=mem_dir,
            )

        self.assertTrue(res["checked"])
        self.assertEqual(res["status"], "stopped")
        self.assertIsNone(res["pid"])
        self.assertFalse(res["restarted"])
        self.assertEqual(res["watchdog_error"], "wd boom")
        m_ens.assert_not_called()
        m_wd.assert_called_once()
        self.assertTrue(m_wd.call_args.kwargs["dry_run"])

    def test_subdaemons_supervision_skips_in_vulcan_mode(self):
        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=True), \
             patch("tools.mcp_daemon.get_status") as m_mcp, \
             patch("tools.zero_mail_listener.get_status") as m_mail, \
             patch("tools.amazon_order_listener.get_status") as m_amz, \
             patch("tools.zero_health_server.is_running") as m_hlth, \
             patch("tools.banana_watcher.get_daemon_status") as m_ban:
            res = bwatch.check_subdaemons_supervision(auto_heal=True)

        self.assertEqual(
            res,
            {"checked": False, "status": "skipped_vulcan", "daemons": {}, "restarted": []},
        )
        m_mcp.assert_not_called()
        m_mail.assert_not_called()
        m_amz.assert_not_called()
        m_hlth.assert_not_called()
        m_ban.assert_not_called()

    def test_subdaemons_supervision_auto_restarts_dead_without_reload_flag(self):
        now = time.time()
        bwatch.BEACON_FILE.write_text(json.dumps({
            "gateway_heartbeat": now - 10,
            "gateway_status": "connected",
            "gateway_latency_ms": 20.0,
            "state": "IDLE",
        }))

        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=False), \
             patch("tools.bridge_watchdog.reap_stale_agy_processes", return_value=[]), \
             patch("tools.bridge_watchdog.check_brief_daemon_supervision", return_value={"checked": True, "status": "running", "pid": 123, "restarted": False}), \
             patch("tools.mcp_daemon.get_status", return_value={"running": True, "pid": 1001}), \
             patch("tools.mcp_daemon.ensure_mcp_daemon_running") as m_mcp_ens, \
             patch("tools.zero_mail_listener.get_status", side_effect=[{"running": False, "pid": 99999}, {"running": True, "pid": 1002}]), \
             patch("tools.zero_mail_listener.ensure_mail_listener_running", return_value=True) as m_mail_ens, \
             patch("tools.amazon_order_listener.get_status", return_value={"running": True, "pid": 1003}), \
             patch("tools.amazon_order_listener.ensure_amazon_listener_running") as m_amz_ens, \
             patch("tools.zero_health_server.is_running", side_effect=[None, 1004]), \
             patch("tools.zero_health_server.ensure_health_server_running", return_value=None) as m_hlth_ens, \
             patch("tools.banana_watcher.get_daemon_status", side_effect=[{"running": False, "pid": None}, {"running": True, "pid": 1005}]), \
             patch("tools.banana_watcher.ensure_banana_watcher_running", return_value=None) as m_ban_ens:
            healthy, summary, details = bwatch.check_bridge_health(auto_heal=True)

        self.assertTrue(healthy)
        self.assertFalse(bwatch.RELOAD_FLAG.exists())
        self.assertFalse(details["self_healed"])

        sup = details["subdaemon_supervision"]
        self.assertTrue(sup["checked"])
        self.assertEqual(sup["status"], "restarted")
        self.assertEqual(
            sup["restarted"],
            ["zero_mail_listener", "zero_health_server", "banana_watcher"],
        )
        self.assertEqual(sup["daemons"]["mcp_daemon"]["status"], "running")
        self.assertEqual(sup["daemons"]["zero_mail_listener"]["pid"], 1002)
        self.assertEqual(sup["daemons"]["zero_health_server"]["pid"], 1004)
        self.assertEqual(sup["daemons"]["banana_watcher"]["pid"], 1005)
        self.assertIn("zero_mail_listener", summary)
        self.assertIn("zero_health_server", summary)
        self.assertIn("banana_watcher", summary)

        m_mcp_ens.assert_not_called()
        m_amz_ens.assert_not_called()
        m_mail_ens.assert_called_once()
        m_hlth_ens.assert_called_once()
        m_ban_ens.assert_called_once()

    def test_subdaemons_supervision_no_heal_reports_stopped(self):
        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=False), \
             patch("tools.mcp_daemon.get_status", return_value={"running": False, "pid": 888}), \
             patch("tools.mcp_daemon.ensure_mcp_daemon_running") as m_mcp_ens, \
             patch("tools.zero_mail_listener.get_status", return_value={"running": True, "pid": 2002}), \
             patch("tools.zero_mail_listener.ensure_mail_listener_running") as m_mail_ens, \
             patch("tools.amazon_order_listener.get_status", return_value={"running": False, "pid": None}), \
             patch("tools.amazon_order_listener.ensure_amazon_listener_running") as m_amz_ens, \
             patch("tools.zero_health_server.is_running", return_value=None), \
             patch("tools.zero_health_server.ensure_health_server_running") as m_hlth_ens, \
             patch("tools.banana_watcher.get_daemon_status", return_value={"running": True, "pid": 2005}), \
             patch("tools.banana_watcher.ensure_banana_watcher_running") as m_ban_ens:
            res = bwatch.check_subdaemons_supervision(auto_heal=False)

        self.assertTrue(res["checked"])
        self.assertEqual(res["status"], "degraded")
        self.assertEqual(res["restarted"], [])
        self.assertEqual(res["daemons"]["mcp_daemon"]["status"], "stopped")
        self.assertEqual(res["daemons"]["amazon_order_listener"]["status"], "stopped")
        self.assertEqual(res["daemons"]["zero_health_server"]["status"], "stopped")
        self.assertEqual(res["daemons"]["zero_mail_listener"]["status"], "running")
        self.assertEqual(res["daemons"]["banana_watcher"]["status"], "running")
        m_mcp_ens.assert_not_called()
        m_mail_ens.assert_not_called()
        m_amz_ens.assert_not_called()
        m_hlth_ens.assert_not_called()
        m_ban_ens.assert_not_called()

    def test_subdaemons_supervision_isolates_per_daemon_exceptions_and_failed_heals(self):
        with patch("tools.bridge_watchdog.is_vulcan_agent", return_value=False), \
             patch("tools.mcp_daemon.get_status", side_effect=RuntimeError("mcp probe boom")), \
             patch("tools.zero_mail_listener.get_status", return_value={"running": False, "pid": None}), \
             patch("tools.zero_mail_listener.ensure_mail_listener_running", return_value=False), \
             patch("tools.amazon_order_listener.get_status", side_effect=[{"running": False, "pid": None}, {"running": True, "pid": 3003}]), \
             patch("tools.amazon_order_listener.ensure_amazon_listener_running", return_value=True), \
             patch("tools.zero_health_server.is_running", return_value=3004), \
             patch("tools.banana_watcher.get_daemon_status", side_effect=[{"running": False, "pid": None}, {"running": False, "pid": None}]), \
             patch("tools.banana_watcher.ensure_banana_watcher_running", side_effect=OSError("spawn failed")):
            res = bwatch.check_subdaemons_supervision(auto_heal=True)

        self.assertTrue(res["checked"])
        self.assertEqual(res["status"], "degraded")
        self.assertEqual(res["restarted"], ["amazon_order_listener"])
        self.assertEqual(res["daemons"]["mcp_daemon"]["status"], "error")
        self.assertIn("mcp probe boom", res["daemons"]["mcp_daemon"]["error"])
        self.assertEqual(res["daemons"]["zero_mail_listener"]["status"], "restart_failed")
        self.assertEqual(res["daemons"]["amazon_order_listener"]["status"], "restarted")
        self.assertEqual(res["daemons"]["zero_health_server"]["status"], "running")
        self.assertEqual(res["daemons"]["banana_watcher"]["status"], "error")
        self.assertIn("spawn failed", res["daemons"]["banana_watcher"]["error"])


if __name__ == "__main__":
    unittest.main()
