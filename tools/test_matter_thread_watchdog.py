#!/usr/bin/env python3
"""Unit tests for Matter-over-Thread auto-recovery watchdog sidecar.

Uses RFC 3849 (2001:db8::/32) and RFC 5737 (198.51.100.0/24) documentation
addresses exclusively.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
TOOLS_DIR = str(Path(__file__).resolve().parent)
for _p in (REPO_ROOT, TOOLS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tools.matter_thread_watchdog as mtw
from tools import nas_config, sidecars


def test_strip_ansi_and_extract_sdk_ipv6():
    sample_logs = (
        "2026-11-23 08:00:01 INFO [matter_server] \x1b[35m<Node:31>\x1b[0m "
        "The SDK is communicating with the device using 2001:db8:1::10\n"
        "2026-11-23 08:05:12 INFO [matter_server] \x1b[1;36m<Node:36>\x1b[0m "
        "The SDK is communicating with the device using 2001:db8:2::36\n"
        "2026-11-23 08:10:44 INFO [matter_server] \x1b[35m<Node:36>\x1b[0m "
        "The SDK is communicating with the device using 2001:db8:2::99\n"
    )
    assert mtw.strip_ansi("\x1b[35m<Node:36>\x1b[0m") == "<Node:36>"
    assert mtw.extract_sdk_ipv6_from_logs(sample_logs, 31) == "2001:db8:1::10"
    assert mtw.extract_sdk_ipv6_from_logs(sample_logs, 36) == "2001:db8:2::99"
    assert mtw.extract_sdk_ipv6_from_logs(sample_logs, 99) is None
    assert mtw.extract_sdk_ipv6_from_logs("", 31) is None


def test_format_matter_service_name():
    svc = mtw.format_matter_service_name(0x1234ABCD5678EF01, 36)
    assert svc == "1234ABCD5678EF01-0000000000000024._matter._tcp.local."
    svc_zero = mtw.format_matter_service_name(1, 31)
    assert svc_zero == "0000000000000001-000000000000001F._matter._tcp.local."


def test_is_tracked_thread_battery_node():
    # IKEA MYGGSPRAY with Thread diagnostics and battery percent
    ikea_thread = {
        "node_id": 31,
        "attributes": {
            "0/40/1": "IKEA of Sweden",
            "0/47/12": 180,
            "0/53/0": 100,
        },
    }
    # IKEA battery sensor without Thread diagnostics cluster
    ikea_bat_only = {
        "node_id": 32,
        "attributes": {
            "0/40/1": "IKEA of Sweden",
            "0/47/11": 0,
        },
    }
    # Generic Thread battery sensor (non-IKEA)
    generic_thread = {
        "node_id": 33,
        "attributes": {
            "0/40/1": "Aqara",
            "0/47/12": 150,
            "0/53/1": 3,
        },
    }
    # Mains-powered Thread plug (no battery attributes 0/47/11 or 0/47/12)
    mains_thread = {
        "node_id": 34,
        "attributes": {
            "0/40/1": "Eve Systems",
            "0/53/0": 10,
        },
    }
    # Wi-Fi thermostat with backup battery (not Thread, not IKEA)
    wifi_thermostat = {
        "node_id": 35,
        "attributes": {
            "0/40/1": "Ecobee",
            "0/47/12": 200,
            "0/54/0": 1,
        },
    }

    assert mtw.is_tracked_thread_battery_node(ikea_thread) is True
    assert mtw.is_tracked_thread_battery_node(ikea_bat_only) is True
    assert mtw.is_tracked_thread_battery_node(generic_thread) is True
    assert mtw.is_tracked_thread_battery_node(mains_thread) is False
    assert mtw.is_tracked_thread_battery_node(wifi_thermostat) is False
    assert mtw.is_tracked_thread_battery_node({}) is False


def test_silent_auto_recovery_and_ipv6_cache_persistence(tmp_path: Path):
    state_file = tmp_path / "matter_state.json"
    initial_state = {
        "nodes": {
            "31": {
                "ipv6": "2001:db8::31",
                "label": "Entryway Motion Sensor",
                "battery_pct": 90,
                "consecutive_failures": 2,
                "last_recovered_ts": None,
                "last_alert_ts": None,
                "alerted": False,
            }
        }
    }
    mtw.save_state(initial_state, state_file)
    loaded = mtw.load_state(state_file)
    assert loaded["nodes"]["31"]["ipv6"] == "2001:db8::31"

    results = [
        {
            "node_id": 31,
            "label": "Entryway Motion Sensor",
            "ipv6": "2001:db8::131",
            "battery_pct": 88,
            "was_offline": True,
            "available": True,
            "recovered": True,
        }
    ]
    new_state, alert = mtw.evaluate_watchdog_state(results, loaded, now_ts=5000.0)
    assert alert is None
    node_entry = new_state["nodes"]["31"]
    assert node_entry["ipv6"] == "2001:db8::131"
    assert node_entry["battery_pct"] == 88
    assert node_entry["consecutive_failures"] == 0
    assert node_entry["last_recovered_ts"] == 5000.0
    assert node_entry["alerted"] is False


def test_three_strike_escalation_cooldown_and_recovery_notice():
    state: dict = {"nodes": {}}
    failing_result = [
        {
            "node_id": 36,
            "label": "Deck Motion Sensor",
            "ipv6": "2001:db8::36",
            "battery_pct": 42,
            "was_offline": True,
            "available": False,
            "recovered": False,
        }
    ]

    # Strike 1: silent
    state, alert1 = mtw.evaluate_watchdog_state(failing_result, state, now_ts=1000.0)
    assert alert1 is None
    assert state["nodes"]["36"]["consecutive_failures"] == 1

    # Strike 2: silent
    state, alert2 = mtw.evaluate_watchdog_state(failing_result, state, now_ts=1300.0)
    assert alert2 is None
    assert state["nodes"]["36"]["consecutive_failures"] == 2

    # Strike 3: alerts cleanly without raw IP, entity ID, or em dash
    state, alert3 = mtw.evaluate_watchdog_state(failing_result, state, now_ts=1600.0)
    assert alert3 is not None
    assert "Deck Motion Sensor" in alert3
    assert "42%" in alert3
    assert "2001:db8" not in alert3
    assert "\u2014" not in alert3
    assert "binary_sensor." not in alert3
    assert state["nodes"]["36"]["alerted"] is True

    # Strike 4 within 12h cooldown: suppressed
    state, alert4 = mtw.evaluate_watchdog_state(failing_result, state, now_ts=1600.0 + 3600.0)
    assert alert4 is None
    assert state["nodes"]["36"]["consecutive_failures"] == 4

    # Strike 5 after 12h cooldown: alerts again
    state, alert5 = mtw.evaluate_watchdog_state(failing_result, state, now_ts=1600.0 + 43200.0)
    assert alert5 is not None
    assert "Deck Motion Sensor" in alert5

    # Node comes back online after prior alert -> brief recovery confirmation
    recovered_result = [
        {
            "node_id": 36,
            "label": "Deck Motion Sensor",
            "ipv6": "2001:db8::36",
            "battery_pct": None,
            "was_offline": True,
            "available": True,
            "recovered": True,
        }
    ]
    state, rec_alert = mtw.evaluate_watchdog_state(
        recovered_result, state, now_ts=1600.0 + 44000.0
    )
    assert rec_alert is not None
    assert "Deck Motion Sensor" in rec_alert
    assert "42%" in rec_alert
    assert "2001:db8" not in rec_alert
    assert "\u2014" not in rec_alert
    assert state["nodes"]["36"]["alerted"] is False
    assert state["nodes"]["36"]["consecutive_failures"] == 0


def test_run_matter_thread_watchdog_end_to_end_and_cache_injection(tmp_path: Path):
    state_file = tmp_path / "watchdog_state.json"
    mtw.save_state(
        {
            "nodes": {
                "31": {
                    "ipv6": "2001:db8::31",
                    "label": "Entryway Motion Sensor",
                    "battery_pct": 85,
                    "consecutive_failures": 0,
                    "last_recovered_ts": None,
                    "last_alert_ts": None,
                    "alerted": False,
                }
            }
        },
        state_file,
    )

    calls: list[tuple[str, str]] = []

    def fake_ssh(host: str, cmd: str) -> tuple[int, str, str]:
        calls.append((host, cmd))
        # Verify single base64-encoded python3 script carries cached IPv6 fallback
        assert "base64.b64decode" in cmd
        b64_part = cmd.split('b64decode("', 1)[1].split('")', 1)[0]
        decoded_script = base64.b64decode(b64_part).decode("utf-8")
        assert "2001:db8::31" in decoded_script
        assert "_matter._tcp.local." in decoded_script
        compile(decoded_script, "<remote_host1>", "exec")
        p1_b64 = decoded_script.split("PHASE1_B64 = '", 1)[1].split("'", 1)[0]
        p2_b64 = decoded_script.split("PHASE2_B64 = '", 1)[1].split("'", 1)[0]
        p1_code = base64.b64decode(p1_b64).decode("utf-8")
        p2_code = base64.b64decode(p2_b64).decode("utf-8")
        compile(p1_code, "<phase1>", "exec")
        compile(p2_code, "<phase2>", "exec")
        assert "http://127.0.0.1:5580/ws" in p1_code
        assert "5540" in p2_code
        assert "ServiceInfo" in p2_code

        return (
            0,
            json.dumps(
                {
                    "compressed_fabric_id": 123456789,
                    "dry_run": False,
                    "results": [
                        {
                            "node_id": 31,
                            "label": "Entryway Motion Sensor",
                            "ipv6": "2001:db8::31",
                            "battery_pct": 84,
                            "was_offline": True,
                            "poked": True,
                            "available": True,
                            "recovered": True,
                        },
                        {
                            "node_id": 36,
                            "label": "Deck Motion Sensor",
                            "ipv6": "2001:db8::36",
                            "battery_pct": 76,
                            "was_offline": False,
                            "poked": False,
                            "available": True,
                            "recovered": False,
                        },
                    ],
                }
            ),
            "",
        )

    with patch.object(nas_config, "HOST_1_IP", "198.51.100.82"):
        out = mtw.run_matter_thread_watchdog(
            dry_run=False,
            state_path=state_file,
            ssh_runner=fake_ssh,
        )

    assert len(calls) == 1
    assert calls[0][0] == "198.51.100.82"
    assert out == ""

    persisted = mtw.load_state(state_file)
    assert persisted["nodes"]["31"]["battery_pct"] == 84
    assert persisted["nodes"]["36"]["ipv6"] == "2001:db8::36"
    assert persisted["nodes"]["31"]["consecutive_failures"] == 0


def test_run_matter_thread_watchdog_ssh_failure_and_sidecar_dispatch(tmp_path: Path, capsys):
    def failing_ssh(host: str, cmd: str) -> tuple[int, str, str]:
        return 255, "", "Connection refused"

    with (
        patch.object(nas_config, "HOST_1_IP", "198.51.100.82"),
        pytest.raises(RuntimeError, match="SSH execution failed"),
    ):
        mtw.run_matter_thread_watchdog(
            state_path=tmp_path / "state.json",
            ssh_runner=failing_ssh,
        )

    with patch.object(mtw, "run_matter_thread_watchdog", return_value="") as mock_wd:
        ok, rep, _ = sidecars.run_sidecar_job(
            "matter_thread_watchdog",
            "Matter-over-Thread Sensor Recovery Watchdog",
            mtw.run_matter_thread_watchdog,
            dry_run=True,
        )
        assert ok is True
        assert rep == ""
        mock_wd.assert_called_once_with(dry_run=True)

    # Dry-run CLI --json path should not mutate state on disk
    dry_state = tmp_path / "dry_state.json"
    with (
        patch.object(mtw, "DEFAULT_STATE_PATH", dry_state),
        patch.object(nas_config, "HOST_1_IP", "198.51.100.82"),
        patch.object(
            nas_config,
            "run_ssh",
            return_value=(
                0,
                json.dumps({"compressed_fabric_id": 1, "dry_run": True, "results": []}),
                "",
            ),
        ),
    ):
        rc = mtw.main(["--dry-run", "--json"])
        assert rc == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["dry_run"] is True
        assert parsed["alert"] == ""
    assert not dry_state.exists()
