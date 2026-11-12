"""
Unit tests for tools/host1_io_hardening.py.

Uses RFC 5737 documentation subnet addresses (198.51.100.x) and neutral mock paths
exclusively per .agents/rules/no_hardcoded_topology.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure workspace paths are present
for _p in [
    str(Path(__file__).resolve().parent.parent),
    str(Path(__file__).resolve().parent.parent / "tools"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import host1_io_hardening as h1

TEST_HOST_1 = "198.51.100.82"
TEST_DOCKER_ROOT = "/mock_nas/docker"


def test_update_recorder_yaml_preserves_custom_tags_and_updates_existing_block():
    sample_yaml = (
        "# Loads default set of integrations. Do not remove.\n"
        "default_config:\n\n"
        "frontend:\n"
        "  themes: !include_dir_merge_named themes\n\n"
        "automation: !include automations.yaml\n\n"
        "recorder:\n"
        "  purge_keep_days: 7\n"
        "  commit_interval: 15\n"
        "  exclude:\n"
        "    entity_globs:\n"
        "      - sensor.*_power_minute_average\n\n"
        "prometheus:\n"
        "  namespace: ha\n"
    )
    updated = h1.update_recorder_yaml_text(sample_yaml, commit_interval=30, auto_purge=True)
    assert "!include_dir_merge_named themes" in updated
    assert "automation: !include automations.yaml" in updated
    assert "  purge_keep_days: 7\n" in updated
    assert "  commit_interval: 30\n" in updated
    assert "  auto_purge: true\n" in updated
    assert "  commit_interval: 15" not in updated
    assert "prometheus:\n  namespace: ha\n" in updated

    # Idempotence check: running a second time produces identical YAML
    updated_twice = h1.update_recorder_yaml_text(updated, commit_interval=30, auto_purge=True)
    assert updated_twice == updated


def test_update_recorder_yaml_inserts_block_when_missing():
    sample_yaml = "default_config:\nautomation: !include automations.yaml\n"
    updated = h1.update_recorder_yaml_text(sample_yaml, commit_interval=30, auto_purge=True)
    assert "recorder:\n  commit_interval: 30\n  auto_purge: true\n" in updated
    assert h1.update_recorder_yaml_text(updated, commit_interval=30, auto_purge=True) == updated


def test_update_sshd_config_text_uncomments_and_is_idempotent():
    raw_sshd = (
        "Port 22\n"
        "#LoginGraceTime 2m\n"
        "PermitRootLogin no\n"
        "#MaxStartups 10:30:100\n"
        "Match User git\n"
        "  AllowTcpForwarding no\n"
    )
    updated = h1.update_sshd_config_text(raw_sshd, max_startups="30:50:100", login_grace_time="20")
    assert "LoginGraceTime 20\n" in updated
    assert "MaxStartups 30:50:100\n" in updated
    assert "#LoginGraceTime" not in updated
    assert "#MaxStartups" not in updated
    # Directives must appear before Match block
    assert updated.index("MaxStartups 30:50:100") < updated.index("Match User git")
    assert h1.update_sshd_config_text(updated) == updated


def test_inject_watchdog_hook_is_idempotent():
    orig_watchdog = (
        "#!/bin/bash\n"
        "FAILURE_FILE=\"/tmp/ha-watchdog-failures\"\n\n"
        "# Read current failure count\n"
        "HTTP_CODE=$(check_ha)\n"
    )
    injected = h1.inject_watchdog_hook(orig_watchdog)
    assert h1.WATCHDOG_BEGIN_MARKER in injected
    assert h1.WATCHDOG_END_MARKER in injected
    assert "ionice -c2 -n7" in injected
    assert "ionice -c2 -n0" in injected
    assert "MaxStartups 30:50:100" in injected
    assert "LoginGraceTime 20" in injected
    assert "ps -ef" in injected
    assert "pgrep" not in injected
    assert injected.index(h1.WATCHDOG_BEGIN_MARKER) < injected.index("# Read current failure count")

    # Running again must replace in-place without duplicating markers
    injected_again = h1.inject_watchdog_hook(injected)
    assert injected_again == injected
    assert injected_again.count(h1.WATCHDOG_BEGIN_MARKER) == 1


def test_apply_recorder_and_btrfs_tuning_calls_ssh_and_validates_api():
    mock_ssh_out = json.dumps({
        "config_dir": f"{TEST_DOCKER_ROOT}/homeassistant/config",
        "lsattr": f"---------------C-- {TEST_DOCKER_ROOT}/homeassistant/config",
        "nodatacow_ok": True,
        "yaml_changed": True,
    })
    with patch.object(h1.nas_config, "run_ssh", return_value=(0, mock_ssh_out, "")) as mock_ssh, \
         patch.object(h1, "validate_ha_config_via_api", return_value={"ok": True, "result": "valid"}) as mock_api:
        res = h1.apply_recorder_and_btrfs_tuning(
            host=TEST_HOST_1,
            docker_root=TEST_DOCKER_ROOT,
            commit_interval=30,
            auto_purge=True,
        )
        assert res["ok"] is True
        assert res["nodatacow_ok"] is True
        assert res["ha_config_check"]["result"] == "valid"
        mock_ssh.assert_called_once()
        assert mock_ssh.call_args[0][0] == TEST_HOST_1
        mock_api.assert_called_once_with(host=TEST_HOST_1)


def test_apply_io_priorities_and_sshd_hardening_and_verify():
    io_out = json.dumps({
        "ok": True,
        "ionice_bin": "/usr/local/bin/ionice",
        "containers": {c: [1001] for c in h1.HEAVY_CONTAINERS},
        "sshd_pids": [9698],
        "watchdog_path": f"{TEST_DOCKER_ROOT}/ha-watchdog.sh",
        "watchdog_updated": True,
    })
    sshd_out = json.dumps({
        "ok": True,
        "cfg_changed": True,
        "sshd_test_rc": 0,
        "master_pid": 9698,
        "sighup_sent": True,
        "watchdog_has_hook": True,
    })
    verify_out = json.dumps({
        "btrfs_nodatacow": True,
        "lsattr": f"---------------C-- {TEST_DOCKER_ROOT}/homeassistant/config",
        "recorder_commit_interval_30": True,
        "recorder_auto_purge_true": True,
        "containers_ionice_ok": True,
        "container_ionice": {c: {"pid": 1001, "ionice": "best-effort: prio 7"} for c in h1.HEAVY_CONTAINERS},
        "sshd_ionice_ok": True,
        "sshd_ionice": "best-effort: prio 0",
        "sshd_max_startups_ok": True,
        "sshd_login_grace_ok": True,
        "sshd_t_ok": True,
        "watchdog_hook_ok": True,
    })

    with patch.object(h1.nas_config, "run_ssh", side_effect=[
        (0, io_out, ""),
        (0, sshd_out, ""),
        (0, verify_out, ""),
    ]), patch.object(h1, "validate_ha_config_via_api", return_value={"ok": True, "result": "valid"}):
        res_io = h1.apply_io_priorities(host=TEST_HOST_1, docker_root=TEST_DOCKER_ROOT)
        assert res_io["ok"] is True
        assert set(res_io["containers"].keys()) == set(h1.HEAVY_CONTAINERS)

        res_sshd = h1.apply_sshd_hardening(host=TEST_HOST_1, docker_root=TEST_DOCKER_ROOT)
        assert res_sshd["ok"] is True
        assert res_sshd["sighup_sent"] is True

        res_ver = h1.verify_host1_hardening(host=TEST_HOST_1, docker_root=TEST_DOCKER_ROOT)
        assert res_ver["ok"] is True
        assert res_ver["btrfs_nodatacow"] is True
        assert res_ver["sshd_max_startups_ok"] is True


def test_verify_host1_hardening_raises_when_guardrail_missing():
    bad_verify_out = json.dumps({
        "btrfs_nodatacow": False,
        "lsattr": f"------------------ {TEST_DOCKER_ROOT}/homeassistant/config",
        "recorder_commit_interval_30": True,
        "recorder_auto_purge_true": True,
        "containers_ionice_ok": True,
        "container_ionice": {},
        "sshd_ionice_ok": True,
        "sshd_ionice": "best-effort: prio 0",
        "sshd_max_startups_ok": True,
        "sshd_login_grace_ok": True,
        "sshd_t_ok": True,
        "watchdog_hook_ok": True,
    })
    with (
        patch.object(h1.nas_config, "run_ssh", return_value=(0, bad_verify_out, "")),
        patch.object(h1, "validate_ha_config_via_api", return_value={"ok": True, "result": "valid"}),
        pytest.raises(RuntimeError, match="Host 1 hardening verification failed"),
    ):
        h1.verify_host1_hardening(host=TEST_HOST_1, docker_root=TEST_DOCKER_ROOT)
