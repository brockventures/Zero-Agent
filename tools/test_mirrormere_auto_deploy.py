#!/usr/bin/env python3
"""Unit tests for tools/mirrormere_auto_deploy.py."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
if str(_TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR.parent))

import mirrormere_auto_deploy as mad  # noqa: E402

SHA_OLD = "1111111111111111111111111111111111111111"
SHA_NEW = "2222222222222222222222222222222222222222"
TEST_HOST2_IP = "198.51.100.84"
TEST_DOCKER_ROOT = "/srv/test-docker"


def test_get_upstream_head_sha_success() -> None:
    fake_proc = SimpleNamespace(
        returncode=0,
        stdout=f"{SHA_NEW}\trefs/heads/main\n",
        stderr="",
    )
    with patch.object(mad.subprocess, "run", return_value=fake_proc) as mock_run:
        sha = mad.get_upstream_head_sha()

    assert sha == SHA_NEW
    mock_run.assert_called_once_with(
        ["git", "ls-remote", "https://github.com/azylman/mirrormere.git", "refs/heads/main"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_get_upstream_head_sha_failure() -> None:
    fake_proc = SimpleNamespace(returncode=128, stdout="", stderr="fatal: repository not found")
    with patch.object(mad.subprocess, "run", return_value=fake_proc):
        with pytest.raises(RuntimeError, match="git ls-remote failed"):
            mad.get_upstream_head_sha()


def test_load_and_save_deploy_state(tmp_path: Path) -> None:
    state_file = tmp_path / "state" / "mirrormere_deploy_state.json"
    assert mad.load_deploy_state(state_file) == {}

    mad.save_deploy_state(state_file, {"last_deployed_sha": SHA_OLD, "updated_at": 1700000000})
    loaded = mad.load_deploy_state(state_file)
    assert loaded["last_deployed_sha"] == SHA_OLD
    assert loaded["updated_at"] == 1700000000


def test_sync_once_initial_seed_when_host2_matches_upstream(tmp_path: Path) -> None:
    state_file = tmp_path / "mirrormere_deploy_state.json"
    ls_remote_proc = SimpleNamespace(returncode=0, stdout=f"{SHA_NEW}\trefs/heads/main\n", stderr="")
    ssh_rev_parse = SimpleNamespace(returncode=0, stdout=f"{SHA_NEW}\n", stderr="")

    with (
        patch.object(mad.nas_config, "HOST_2_IP", TEST_HOST2_IP),
        patch.object(mad.nas_config, "DOCKER_ROOT", TEST_DOCKER_ROOT),
        patch.object(mad.subprocess, "run", return_value=ls_remote_proc),
        patch.object(mad.nas_config, "run_ssh", return_value=ssh_rev_parse) as mock_ssh,
    ):
        result = mad.sync_once(state_path=state_file)

    assert result == {"status": "seeded", "sha": SHA_NEW}
    mock_ssh.assert_called_once()
    saved = mad.load_deploy_state(state_file)
    assert saved["last_deployed_sha"] == SHA_NEW


def test_sync_once_noop_when_sha_matches(tmp_path: Path) -> None:
    state_file = tmp_path / "mirrormere_deploy_state.json"
    mad.save_deploy_state(state_file, {"last_deployed_sha": SHA_NEW, "updated_at": 1700000000})
    ls_remote_proc = SimpleNamespace(returncode=0, stdout=f"{SHA_NEW}\trefs/heads/main\n", stderr="")

    with (
        patch.object(mad.subprocess, "run", return_value=ls_remote_proc),
        patch.object(mad.nas_config, "run_ssh") as mock_ssh,
    ):
        result = mad.sync_once(state_path=state_file)

    assert result == {"status": "up_to_date", "sha": SHA_NEW}
    mock_ssh.assert_not_called()


def test_sync_once_triggers_deploy_when_upstream_advances(tmp_path: Path) -> None:
    state_file = tmp_path / "mirrormere_deploy_state.json"
    mad.save_deploy_state(state_file, {"last_deployed_sha": SHA_OLD, "updated_at": 1700000000})
    ls_remote_proc = SimpleNamespace(returncode=0, stdout=f"{SHA_NEW}\trefs/heads/main\n", stderr="")
    ssh_deploy_proc = SimpleNamespace(returncode=0, stdout="HEALTH_OK\n", stderr="")

    with (
        patch.object(mad.nas_config, "HOST_2_IP", TEST_HOST2_IP),
        patch.object(mad.nas_config, "DOCKER_ROOT", TEST_DOCKER_ROOT),
        patch.object(mad.subprocess, "run", return_value=ls_remote_proc),
        patch.object(mad.nas_config, "run_ssh", return_value=ssh_deploy_proc) as mock_ssh,
    ):
        result = mad.sync_once(state_path=state_file)

    assert result == {"status": "deployed", "sha": SHA_NEW}
    mock_ssh.assert_called_once()
    host_arg, cmd_arg = mock_ssh.call_args[0]
    assert host_arg == TEST_HOST2_IP
    assert f"cd {TEST_DOCKER_ROOT}/hearth && git fetch origin main && git reset --hard {SHA_NEW}" in cmd_arg
    assert f"sudo /usr/local/bin/docker build -t ghcr.io/azylman/mirrormere:latest {TEST_DOCKER_ROOT}/hearth" in cmd_arg
    assert "sudo /usr/local/bin/docker compose -f compose.hearth.yaml up -d hearth-ical-bridge hearth-core" in cmd_arg
    assert "http://127.0.0.1:8099/healthz" in cmd_arg
    assert "http://127.0.0.1:8088/healthz" in cmd_arg
    assert mock_ssh.call_args[1]["timeout"] == 300

    saved = mad.load_deploy_state(state_file)
    assert saved["last_deployed_sha"] == SHA_NEW


def test_sync_once_dry_run_does_not_execute_ssh_or_modify_state(tmp_path: Path) -> None:
    state_file = tmp_path / "mirrormere_deploy_state.json"
    mad.save_deploy_state(state_file, {"last_deployed_sha": SHA_OLD, "updated_at": 1700000000})
    ls_remote_proc = SimpleNamespace(returncode=0, stdout=f"{SHA_NEW}\trefs/heads/main\n", stderr="")

    with (
        patch.object(mad.nas_config, "HOST_2_IP", TEST_HOST2_IP),
        patch.object(mad.nas_config, "DOCKER_ROOT", TEST_DOCKER_ROOT),
        patch.object(mad.subprocess, "run", return_value=ls_remote_proc),
        patch.object(mad.nas_config, "run_ssh") as mock_ssh,
    ):
        result = mad.sync_once(state_path=state_file, dry_run=True)

    assert result == {"status": "dry_run", "sha": SHA_NEW}
    mock_ssh.assert_not_called()
    saved = mad.load_deploy_state(state_file)
    assert saved["last_deployed_sha"] == SHA_OLD


def test_sync_once_ssh_failure_does_not_advance_state(tmp_path: Path) -> None:
    state_file = tmp_path / "mirrormere_deploy_state.json"
    mad.save_deploy_state(state_file, {"last_deployed_sha": SHA_OLD, "updated_at": 1700000000})
    ls_remote_proc = SimpleNamespace(returncode=0, stdout=f"{SHA_NEW}\trefs/heads/main\n", stderr="")
    ssh_fail_proc = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="Health check failed for 127.0.0.1:8099/healthz or 127.0.0.1:8088/healthz",
    )

    with (
        patch.object(mad.nas_config, "HOST_2_IP", TEST_HOST2_IP),
        patch.object(mad.nas_config, "DOCKER_ROOT", TEST_DOCKER_ROOT),
        patch.object(mad.subprocess, "run", return_value=ls_remote_proc),
        patch.object(mad.nas_config, "run_ssh", return_value=ssh_fail_proc),
    ):
        with pytest.raises(RuntimeError, match="Hearth deploy on Host 2 failed"):
            mad.sync_once(state_path=state_file)

    saved = mad.load_deploy_state(state_file)
    assert saved["last_deployed_sha"] == SHA_OLD
