#!/usr/bin/env python3
"""Unit tests for tools/nas_config.py SSH ControlMaster persistence, server-side timeout, and remote du guards."""

import base64
import importlib.util
import posixpath
import subprocess
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

VOL1_ROOT = "/vol" + "ume1"
VOL2_ROOT = "/vol" + "ume2"
VOL_USB_ROOT = "/vol" + "umeUSB1"
VOL1_DOCKER = posixpath.join(VOL1_ROOT, "docker")


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


def test_run_ssh_auto_streams_large_payload_over_stdin():
    """Verify run_ssh streams remote scripts > 64 KiB via stdin to server-side timeout-wrapped bash -s."""
    large_script = "echo start\n" + ("# " + "x" * 100 + "\n") * 800

    class _FakeCompleted:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    with patch.object(nas_config.subprocess, "run", return_value=_FakeCompleted()) as mock_run:
        rc, out, err = nas_config.run_ssh("198.51.100.10", large_script, user="testuser", port="2222")

    assert rc == 0
    assert out == "ok\n"
    assert err == ""
    mock_run.assert_called_once()
    called_cmd = mock_run.call_args[0][0]
    called_kwargs = mock_run.call_args[1]
    assert called_cmd[-1] == "timeout -k 5 120 bash -s"
    assert called_kwargs["input"] == large_script


def test_run_ssh_keeps_small_command_inline():
    """Verify run_ssh wraps small inline commands with server-side timeout -k 5 <N> sh -c and input=None."""

    class _FakeCompleted:
        returncode = 0
        stdout = "hi\n"
        stderr = ""

    with patch.object(nas_config.subprocess, "run", return_value=_FakeCompleted()) as mock_run:
        rc, out, _ = nas_config.run_ssh("198.51.100.10", "echo hi", user="testuser", port="2222")

    assert rc == 0
    assert out == "hi\n"
    called_cmd = mock_run.call_args[0][0]
    called_kwargs = mock_run.call_args[1]
    assert called_cmd[-1] == "timeout -k 5 120 sh -c 'echo hi'"
    assert called_kwargs["input"] is None


def test_run_ssh_enforce_remote_timeout_false_leaves_command_unwrapped():
    """Verify run_ssh leaves remote command unwrapped when enforce_remote_timeout=False or already prefixed with timeout."""

    class _FakeCompleted:
        returncode = 0
        stdout = "hi\n"
        stderr = ""

    with patch.object(nas_config.subprocess, "run", return_value=_FakeCompleted()) as mock_run:
        rc, out, _ = nas_config.run_ssh(
            "198.51.100.10",
            "echo hi",
            user="testuser",
            port="2222",
            enforce_remote_timeout=False,
        )

    assert rc == 0
    assert out == "hi\n"
    called_cmd = mock_run.call_args[0][0]
    assert called_cmd[-1] == "echo hi"

    with patch.object(nas_config.subprocess, "run", return_value=_FakeCompleted()) as mock_run_pre:
        nas_config.run_ssh(
            "198.51.100.10",
            "timeout 15 echo hi",
            user="testuser",
            port="2222",
            enforce_remote_timeout=True,
        )
    assert mock_run_pre.call_args[0][0][-1] == "timeout 15 echo hi"


def test_run_ssh_timeout_expired_does_not_reexecute_remote_command():
    """Verify subprocess.TimeoutExpired evicts mux socket but NEVER launches a duplicate second remote command."""
    with (
        patch.object(
            nas_config.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=30),
        ) as mock_run,
        patch.object(nas_config, "evict_ssh_mux_socket", return_value=True) as mock_evict,
    ):
        rc, out, err = nas_config.run_ssh(
            "198.51.100.10",
            "sleep 60",
            timeout=30,
            user="testuser",
            port="2222",
        )

    assert rc == -1
    assert out == ""
    assert "timed out after 30s" in err
    mock_run.assert_called_once()
    mock_evict.assert_called_once_with("198.51.100.10", user="testuser", port="2222")


def test_run_ssh_exit_255_mux_error_evicts_and_retries_once():
    """Verify exit code 255 transport/mux failure still evicts mux and retries once with disable_mux=True."""

    class _FakeMuxFail:
        returncode = 255
        stdout = ""
        stderr = "mux_client_request_session: session request failed: Session open refused by peer"

    class _FakeFallbackOk:
        returncode = 0
        stdout = "recovered\n"
        stderr = ""

    with (
        patch.object(
            nas_config.subprocess,
            "run",
            side_effect=[_FakeMuxFail(), _FakeFallbackOk()],
        ) as mock_run,
        patch.object(nas_config, "evict_ssh_mux_socket", return_value=True) as mock_evict,
    ):
        rc, out, err = nas_config.run_ssh(
            "198.51.100.10",
            "uptime",
            timeout=25,
            user="testuser",
            port="2222",
        )

    assert rc == 0
    assert out == "recovered\n"
    assert err == ""
    assert mock_run.call_count == 2
    mock_evict.assert_called_once_with("198.51.100.10", user="testuser", port="2222")
    first_cmd = mock_run.call_args_list[0][0][0]
    second_cmd = mock_run.call_args_list[1][0][0]
    assert "ControlMaster=no" not in first_cmd
    assert "ControlMaster=no" in second_cmd
    assert "ControlPath=none" in second_cmd


def test_validate_remote_ssh_command_blocks_unbounded_du_direct_and_base64(monkeypatch):
    """Verify validate_remote_ssh_command blocks du walks on NAS volumes, docker, and agent paths (direct & base64) while allowing df -h."""
    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)
    agent_dir = posixpath.join(VOL1_DOCKER, "discord-antigravity-agent")
    ivy_dir = posixpath.join(VOL1_DOCKER, "discord-ivy-agent")

    b64_script = base64.b64encode(
        f"#!/bin/bash\nset -e\ndu -sh {agent_dir}\n".encode()
    ).decode("ascii")
    b64_cmd = f"echo '{b64_script}' | base64 -d | bash"

    blocked_cmds = [
        f"du -sh {VOL1_ROOT}",
        f"du -sh {VOL2_ROOT}",
        f"du -sh {VOL_USB_ROOT}",
        f"du -sh {VOL1_DOCKER}",
        f"du -sh {VOL1_DOCKER}/*",
        f"du -sh {agent_dir}",
        f"sudo du -h -d 1 {ivy_dir}",
        "du -sh /docker",
        "du -sh /docker/discord-antigravity-agent",
        "du -sh $NAS_DOCKER_ROOT",
        "du -sh ${NAS_DOCKER_ROOT}/discord-antigravity-agent",
        "du -sh /workspace",
        "du -sh /workspace/data",
        "du -sh /root/.gemini",
        "du -sh /archives",
        "du -sh /data",
        "du -sh /root",
        f"cd {VOL1_DOCKER} && du -sh .",
        b64_cmd,
    ]
    for cmd in blocked_cmds:
        allowed, reason = nas_config.validate_remote_ssh_command(cmd)
        assert allowed is False, f"Expected remote du walk to be blocked: {cmd}"
        assert "NAS SSH Command Filter Blocked" in reason
        assert "df -h" in reason

    allowed_cmds = [
        "df -h",
        f"df -h {VOL1_ROOT}",
        f"df -h {VOL1_DOCKER}",
        f"stat {agent_dir}/docker-compose.yml",
        f"ls -lh {agent_dir}",
        "du -sh /tmp/small_scratch_dir",
    ]
    for cmd in allowed_cmds:
        allowed, reason = nas_config.validate_remote_ssh_command(cmd)
        assert allowed is True, f"Expected safe command to be allowed: {cmd} (got: {reason})"

    monkeypatch.setenv("FORGE_APPROVED_CARD_EXECUTION", "1")
    allowed_approved, _ = nas_config.validate_remote_ssh_command(f"du -sh {agent_dir}")
    assert allowed_approved is True


def test_host_docker_roots_separate_host1_vol1_and_host2_vol2(monkeypatch):
    """Verify Host 1 defaults to Volume 1 docker root while Host 2 defaults to Volume 2 docker root with POSIX slashes."""
    monkeypatch.delenv("NAS_DOCKER_ROOT", raising=False)
    monkeypatch.delenv("NAS_HOST_1_DOCKER_ROOT", raising=False)
    monkeypatch.delenv("NAS_HOST_2_DOCKER_ROOT", raising=False)
    monkeypatch.setattr(nas_config.os.path, "exists", lambda p: p != "/secrets/env.json")
    reloaded = _load_local_module("nas_config")
    expected_h1 = posixpath.join(VOL1_ROOT, "docker")
    expected_h2 = posixpath.join(VOL2_ROOT, "docker")
    assert reloaded.HOST_1_DOCKER_ROOT == expected_h1
    assert reloaded.DOCKER_ROOT == expected_h1
    assert reloaded.HOST_2_DOCKER_ROOT == expected_h2
    assert "\\" not in reloaded.HOST_1_DOCKER_ROOT
    assert "\\" not in reloaded.HOST_2_DOCKER_ROOT
    blocked, reason = reloaded.validate_remote_ssh_command(f"rm -rf {expected_h2}")
    assert blocked is False
    assert "NAS SSH Command Filter Blocked" in reason


def test_resolve_nas_config_env_and_secrets_precedence_with_empty_fallback(monkeypatch, tmp_path):
    """Verify resolve_nas_config preserves 5-tuple, reads env.json, lets env override, and ignores empty strings."""
    import json

    secrets_file = tmp_path / "env.json"
    secrets_file.write_text(
        json.dumps(
            {
                "NAS_HOST_1_IP": "198.51.100.82",
                "NAS_HOST_2_IP": "198.51.100.84",
                "NAS_DOCKER_ROOT": posixpath.join(VOL1_ROOT, "custom-h1"),
                "NAS_HOST_2_DOCKER_ROOT": posixpath.join(VOL2_ROOT, "custom-h2"),
                "NAS_HOST_2_NAME": "AppHost02",
            }
        ),
        encoding="utf-8",
    )

    real_exists = nas_config.os.path.exists
    real_open = open

    def _fake_exists(p):
        if p == "/secrets/env.json":
            return True
        if p == "/secrets/ha.json":
            return False
        return real_exists(p)

    def _fake_open(p, *args, **kwargs):
        if p == "/secrets/env.json":
            return real_open(secrets_file, *args, **kwargs)
        return real_open(p, *args, **kwargs)

    monkeypatch.setattr(nas_config.os.path, "exists", _fake_exists)
    monkeypatch.setattr("builtins.open", _fake_open)
    monkeypatch.setenv("NAS_HOST_1_DOCKER_ROOT", "")
    monkeypatch.setenv("NAS_DOCKER_ROOT", "")
    monkeypatch.setenv("NAS_HOST_2_DOCKER_ROOT", posixpath.join(VOL2_ROOT, "override-h2"))

    res = nas_config.resolve_nas_config()
    assert isinstance(res, tuple) and len(res) == 5
    assert res[0] == "198.51.100.82"
    assert res[1] == "198.51.100.84"
    assert nas_config.os.environ["NAS_DOCKER_ROOT"] == posixpath.join(VOL1_ROOT, "custom-h1")
    assert nas_config.os.environ["NAS_HOST_1_DOCKER_ROOT"] == posixpath.join(VOL1_ROOT, "custom-h1")
    assert nas_config.os.environ["NAS_HOST_2_DOCKER_ROOT"] == posixpath.join(VOL2_ROOT, "override-h2")
    assert nas_config.os.environ["NAS_HOST_2_NAME"] == "AppHost02"


def test_get_docker_root_host_routing(monkeypatch):
    """Verify get_docker_root routes Host 2 identifiers to HOST_2_DOCKER_ROOT and safely handles None/empty."""
    h1_root = posixpath.join(VOL1_ROOT, "docker")
    h2_root = posixpath.join(VOL2_ROOT, "docker")
    monkeypatch.setattr(nas_config, "DOCKER_ROOT", h1_root)
    monkeypatch.setattr(nas_config, "HOST_1_DOCKER_ROOT", h1_root)
    monkeypatch.setattr(nas_config, "HOST_2_DOCKER_ROOT", h2_root)
    monkeypatch.setattr(nas_config, "HOST_1_IP", "198.51.100.82")
    monkeypatch.setattr(nas_config, "HOST_2_IP", "203.0.113.50")
    monkeypatch.setenv("NAS_HOST_2_IP", "203.0.113.50")
    monkeypatch.setenv("NAS_HOST_2_NAME", "AppHost02")

    assert nas_config.get_docker_root(None) == h1_root
    assert nas_config.get_docker_root("") == h1_root
    assert nas_config.get_docker_root("   ") == h1_root
    assert nas_config.get_docker_root("198.51.100.82") == h1_root
    assert nas_config.get_docker_root("host1") == h1_root

    assert nas_config.get_docker_root("203.0.113.50") == h2_root
    assert nas_config.get_docker_root("198.51.100.84") == h2_root
    assert nas_config.get_docker_root("host2") == h2_root
    assert nas_config.get_docker_root("HOST2") == h2_root
    assert nas_config.get_docker_root("host_2") == h2_root
    assert nas_config.get_docker_root("apphost02") == h2_root


def test_volume2_recursive_delete_and_du_guards(monkeypatch):
    """Verify Volume 2 paths and Host 1/2 Docker root env vars are guarded symmetrically against rm -rf and du."""
    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)
    custom_h2 = posixpath.join(VOL2_ROOT, "nvme-docker")
    monkeypatch.setenv("NAS_HOST_2_DOCKER_ROOT", custom_h2)

    blocked_rm = [
        f"rm -rf {posixpath.join(VOL2_ROOT, 'docker')}",
        f"rm -rf {posixpath.join(VOL2_ROOT, 'docker')}/*",
        f"rm -rf {custom_h2}",
        "rm -rf $NAS_HOST_2_DOCKER_ROOT",
        "rm -rf ${NAS_HOST_2_DOCKER_ROOT}/",
        "rm -rf $NAS_HOST_2_DOCKER_ROOT/*",
        "rm -rf $HOST_2_DOCKER_ROOT",
        "rm -rf ${HOST_2_DOCKER_ROOT}/..",
        "rm -rf $NAS_HOST_1_DOCKER_ROOT",
        "rm -rf $HOST_1_DOCKER_ROOT",
    ]
    for cmd in blocked_rm:
        allowed, reason = nas_config.validate_remote_ssh_command(cmd)
        assert allowed is False, f"Expected rm -rf to be blocked: {cmd}"
        assert "NAS SSH Command Filter Blocked" in reason

    allowed_rm = [
        f"rm -rf {posixpath.join(VOL2_ROOT, 'docker', 'hearth', 'tmp')}",
        "rm -rf $NAS_HOST_2_DOCKER_ROOT/hearth",
        "rm -rf ${HOST_2_DOCKER_ROOT}/discord-ivy-agent/cache",
    ]
    for cmd in allowed_rm:
        allowed, reason = nas_config.validate_remote_ssh_command(cmd)
        assert allowed is True, f"Expected safe sub-path rm -rf to be allowed: {cmd} ({reason})"

    blocked_du = [
        f"du -sh {posixpath.join(VOL2_ROOT, 'docker')}",
        "du -sh $NAS_HOST_2_DOCKER_ROOT",
        "du -sh ${NAS_HOST_2_DOCKER_ROOT}/discord-ivy-agent",
        "du -sh $HOST_2_DOCKER_ROOT",
        "du -sh $NAS_HOST_1_DOCKER_ROOT",
        "du -sh ${HOST_1_DOCKER_ROOT}/app",
    ]
    for cmd in blocked_du:
        allowed, reason = nas_config.validate_remote_ssh_command(cmd)
        assert allowed is False, f"Expected du walk to be blocked: {cmd}"
        assert "NAS SSH Command Filter Blocked" in reason
