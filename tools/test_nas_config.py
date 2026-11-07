#!/usr/bin/env python3
"""Unit tests for tools/nas_config.py SSH ControlMaster persistence and socket eviction."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

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


def test_build_ssh_cmd_mux_persistence_flags_enabled():
    """Verify build_ssh_cmd appends ControlMaster, ControlPath, ControlPersist, and ServerAlive flags on POSIX."""
    with patch.object(nas_config.os, "name", "posix"):
        cmd = nas_config.build_ssh_cmd(
            "198.51.100.10",
            "echo ok",
            user="testuser",
            port="2222",
            disable_mux=False,
        )

    assert "ControlMaster=auto" in cmd
    assert "ControlPath=/tmp/ssh_mux_198.51.100.10_2222_testuser" in cmd
    assert "ControlPersist=4h" in cmd
    assert "ServerAliveInterval=30" in cmd
    assert "ServerAliveCountMax=3" in cmd
    assert "ControlMaster=no" not in cmd


def test_build_ssh_cmd_disable_mux():
    """Verify build_ssh_cmd disables ControlMaster and ControlPath when disable_mux=True."""
    cmd = nas_config.build_ssh_cmd(
        "198.51.100.10",
        "echo ok",
        user="testuser",
        port="2222",
        disable_mux=True,
    )

    assert "ControlMaster=no" in cmd
    assert "ControlPath=none" in cmd
    assert "ControlMaster=auto" not in cmd
    assert "ControlPersist=4h" not in cmd


def test_evict_ssh_mux_socket_passes_control_path():
    """Verify evict_ssh_mux_socket passes explicit ControlPath to ssh -O exit."""
    with patch.object(nas_config.subprocess, "run") as mock_run:
        evicted = nas_config.evict_ssh_mux_socket("198.51.100.10", user="testuser", port="2222")

    assert evicted is True
    mock_run.assert_called_once()
    called_cmd = mock_run.call_args[0][0]
    assert "-O" in called_cmd
    assert "exit" in called_cmd
    assert "ControlPath=/tmp/ssh_mux_198.51.100.10_2222_testuser" in called_cmd
