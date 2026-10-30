#!/usr/bin/env python3
"""Unit tests for OpenSSH ControlMaster mux eviction and fallback in nas_config and pid_watchdog."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "/workspace")

import tools.nas_config as nas_config  # noqa: E402
import tools.pid_watchdog as pid_watchdog  # noqa: E402


def test_build_ssh_cmd_disable_mux_adds_control_flags():
    cmd_default = nas_config.build_ssh_cmd("198.51.100.10", "uptime", user="TestUser", port="2222")
    assert "ControlMaster=no" not in cmd_default
    assert "ControlPath=none" not in cmd_default

    cmd_no_mux = nas_config.build_ssh_cmd(
        "198.51.100.10",
        "uptime",
        user="TestUser",
        port="2222",
        disable_mux=True,
    )
    assert "ControlMaster=no" in cmd_no_mux
    assert "ControlPath=none" in cmd_no_mux


def test_run_ssh_evicts_mux_and_retries_without_mux_on_timeout():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "-O" in cmd and "exit" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "ControlMaster=no" not in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=20)
        return SimpleNamespace(returncode=0, stdout="FALLBACK_OK\n", stderr="")

    with patch("tools.nas_config.subprocess.run", side_effect=fake_run):
        rc, out, err = nas_config.run_ssh(
            "198.51.100.10",
            "uptime",
            timeout=20,
            user="TestUser",
            port="2222",
        )

    assert rc == 0
    assert out == "FALLBACK_OK\n"
    assert err == ""
    # 1: primary ssh call, 2: ssh -O exit mux eviction, 3: fallback ssh with ControlMaster=no
    assert len(calls) == 3
    assert "-O" in calls[1]
    assert "ControlMaster=no" in calls[2]
    assert "ControlPath=none" in calls[2]


def test_pid_watchdog_uses_transport_alert_header_when_only_ssh_errors():
    fake_err = {
        "host": "AppHost02",
        "ip": "198.51.100.20",
        "status": "error",
        "error": "SSH connection failed (code -1): timed out",
        "threads_max": 0,
        "threads_active": 0,
        "thread_ratio": 0.0,
        "proc_count": 0,
        "pid_max": 0,
        "last_pid": 0,
    }
    fake_nom = {
        "host": "NasHost01",
        "ip": "198.51.100.10",
        "status": "nominal",
        "error": None,
        "threads_max": 60000,
        "threads_active": 1500,
        "thread_ratio": 0.025,
        "proc_count": 300,
        "pid_max": 65536,
        "last_pid": 1234,
    }

    with patch("tools.pid_watchdog.check_host_pid_health", side_effect=[fake_err, fake_nom]):
        has_issues, report, _ = pid_watchdog.audit_pid_headroom(force=False)

    assert has_issues is True
    assert "### ⚠️ PID Watchdog SSH Transport Alert" in report
    assert "PID & Process Headroom Alert" not in report
