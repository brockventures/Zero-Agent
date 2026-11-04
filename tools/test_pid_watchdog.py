#!/usr/bin/env python3
"""Unit tests for nas_config SSH connect_timeout propagation and pid_watchdog health checks."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS_DIR))
sys.path.insert(0, str(_TOOLS_DIR.parent))

def _load_local_module(name: str):
    spec = importlib.util.spec_from_file_location(name, _TOOLS_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

nas_config = _load_local_module("nas_config")
pid_watchdog = _load_local_module("pid_watchdog")


def test_build_ssh_cmd_default_and_custom_connect_timeout():
    """Verify build_ssh_cmd defaults to ConnectTimeout=15 and respects custom overrides."""
    cmd_default = nas_config.build_ssh_cmd("198.51.100.10", "echo ok", user="testuser", port="22")
    assert "ConnectTimeout=15" in cmd_default

    cmd_custom = nas_config.build_ssh_cmd(
        "198.51.100.10", "echo ok", user="testuser", port="22", connect_timeout=22
    )
    assert "ConnectTimeout=22" in cmd_custom


def test_run_ssh_propagates_connect_timeout_to_initial_and_fallback():
    """Verify run_ssh passes connect_timeout to both initial and mux-bypass fallback calls."""
    fail_res = MagicMock(returncode=255, stdout="", stderr="Connection timed out during banner exchange")
    ok_res = MagicMock(returncode=0, stdout="ok\n", stderr="")

    with (
        patch.object(nas_config.subprocess, "run", side_effect=[fail_res, ok_res]) as mock_run,
        patch.object(nas_config, "evict_ssh_mux_socket", return_value=True) as mock_evict,
    ):
        code, out, err = nas_config.run_ssh(
            "198.51.100.10",
            "uptime",
            timeout=25,
            connect_timeout=15,
            user="testuser",
            port="22",
        )

    assert code == 0
    assert out == "ok\n"
    assert err == ""
    assert mock_run.call_count == 2
    mock_evict.assert_called_once_with("198.51.100.10", user="testuser", port="22")

    first_cmd = mock_run.call_args_list[0][0][0]
    second_cmd = mock_run.call_args_list[1][0][0]
    assert "ConnectTimeout=15" in first_cmd
    assert "ControlMaster=no" not in first_cmd
    assert "ConnectTimeout=15" in second_cmd
    assert "ControlMaster=no" in second_cmd


def test_run_ssh_timeout_expired_fallback_propagates_custom_connect_timeout():
    """Verify run_ssh passes custom connect_timeout on subprocess.TimeoutExpired mux-bypass retry."""
    ok_res = MagicMock(returncode=0, stdout="recovered\n", stderr="")

    with (
        patch.object(
            nas_config.subprocess,
            "run",
            side_effect=[subprocess.TimeoutExpired(cmd=["ssh"], timeout=40), ok_res],
        ) as mock_run,
        patch.object(nas_config, "evict_ssh_mux_socket", return_value=True),
    ):
        code, out, _ = nas_config.run_ssh(
            "198.51.100.11",
            "uptime",
            timeout=40,
            connect_timeout=18,
            user="testuser",
            port="22",
        )

    assert code == 0
    assert out == "recovered\n"
    assert mock_run.call_count == 2
    for call in mock_run.call_args_list:
        cmd = call[0][0]
        assert "ConnectTimeout=18" in cmd


def test_check_host_pid_health_retries_and_passes_connect_timeout():
    """Verify check_host_pid_health passes timeout=25, connect_timeout=15 and retries cleanly."""
    sample_output = "62755\n4194304\n0.15 0.20 0.18 2/850 12345\n142\n"

    with (
        patch.object(
            pid_watchdog,
            "run_ssh",
            side_effect=[
                (255, "", "Connection timed out during banner exchange"),
                (0, sample_output, ""),
            ],
        ) as mock_ssh,
        patch.object(pid_watchdog, "evict_ssh_mux_socket", return_value=True) as mock_evict,
        patch.object(pid_watchdog.time, "sleep") as mock_sleep,
    ):
        res = pid_watchdog.check_host_pid_health("198.51.100.10", "NasHost01")

    assert res["status"] == "nominal"
    assert res["threads_max"] == 62755
    assert res["threads_active"] == 850
    assert res["proc_count"] == 142
    assert res["last_pid"] == 12345

    assert mock_ssh.call_count == 2
    first_kwargs = mock_ssh.call_args_list[0][1]
    second_kwargs = mock_ssh.call_args_list[1][1]

    assert first_kwargs["timeout"] == 25
    assert first_kwargs["connect_timeout"] == 15
    assert first_kwargs["disable_mux"] is False
    assert first_kwargs["retry_without_mux_on_timeout"] is True

    assert second_kwargs["timeout"] == 25
    assert second_kwargs["connect_timeout"] == 15
    assert second_kwargs["disable_mux"] is True
    assert second_kwargs["retry_without_mux_on_timeout"] is False

    mock_evict.assert_called_once()
    mock_sleep.assert_called_once_with(2.0)
