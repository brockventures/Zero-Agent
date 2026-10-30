#!/usr/bin/env python3
"""Unit tests for sidecar health fixes:
- nas_storage_check live metric formatting, SSH flags, timeout, and Host 2 local fallback
- dockhand_update quiet Decision Card staging and stdout suppression
- ha_update_check quiet Decision Card staging and stdout suppression
- update_antigravity quiet Decision Card staging and stdout suppression
"""

import os
import subprocess
import sys
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Prioritize worktree directory over /workspace in sys.path
sys.path = [p for p in sys.path if p not in ("/workspace", "/workspace/tools")]
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
sys.path.insert(0, REPO_ROOT)

import tools.dockhand_update as dockhand_update
import tools.ha_update_check as ha_update_check
import tools.nas_storage_check as nas_storage_check
import tools.update_antigravity as update_antigravity

# RFC 5737 documentation IPs for unit test isolation
DOC_HOST_1 = "198.51.100.82"
DOC_HOST_2 = "203.0.113.84"


# ==============================================================================
# 1. nas_storage_check tests
# ==============================================================================


def test_nas_storage_check_ssh_flags():
    """Verify SSH commands contain required connect timeout and host key flags."""
    assert "-o" in nas_storage_check.SSH_82
    assert "StrictHostKeyChecking=no" in nas_storage_check.SSH_82
    assert "ConnectTimeout=8" in nas_storage_check.SSH_82

    assert "-o" in nas_storage_check.SSH_84
    assert "StrictHostKeyChecking=no" in nas_storage_check.SSH_84
    assert "ConnectTimeout=8" in nas_storage_check.SSH_84


def test_nas_storage_check_live_metric_formatting():
    """Verify check_host returns live parsed capacity and RAID status."""
    df_output = (
        "Filesystem           Size  Used Avail Use% Mounted on\n"
        "/dev/vg1/volume_1     14T  9.5T  4.5T  68% /volume1\n"
    )
    mdstat_output = (
        "Personalities : [raid5]\n"
        "md2 : active raid5 sata1p5[0] sata2p5[1] sata3p5[2] sata4p5[3]\n"
        "      35118743040 blocks super 1.2 level 5, 64k chunk, algorithm 2 [4/4] [UUUU]\n"
    )

    def fake_get_out(ssh_cmd, cmd_str):
        if "df -h" in cmd_str:
            return df_output
        if "proc/mdstat" in cmd_str:
            return mdstat_output
        return ""

    with patch.object(nas_storage_check, "get_out", side_effect=fake_get_out):
        issues, status_line = nas_storage_check.check_host("NasHost01 (.82)", ["ssh", DOC_HOST_1], threshold=85)
        assert issues == []
        assert "68% used (4.5T free)" in status_line
        assert "RAID5 [UUUU]" in status_line


def test_nas_storage_check_high_capacity_alert():
    """Verify alert is flagged when usage exceeds threshold."""
    df_output = (
        "Filesystem           Size  Used Avail Use% Mounted on\n"
        "/dev/vg1/volume_1     14T   13T  1.0T  92% /volume1\n"
    )
    mdstat_output = (
        "md2 : active raid1 sata1p5[2] sata2p5[1]\n"
        "      15615154816 blocks super 1.2 [2/2] [UU]\n"
    )

    def fake_get_out(ssh_cmd, cmd_str):
        if "df -h" in cmd_str:
            return df_output
        if "proc/mdstat" in cmd_str:
            return mdstat_output
        return ""

    with patch.object(nas_storage_check, "get_out", side_effect=fake_get_out):
        issues, status_line = nas_storage_check.check_host("NasHost02 (.84)", ["ssh", DOC_HOST_2], threshold=85)
        assert len(issues) == 1
        assert "Storage High" in issues[0]
        assert "92%" in issues[0]
        assert "92% used (1.0T free)" in status_line
        assert "RAID1 [UU]" in status_line


def test_nas_storage_check_degraded_raid_alert():
    """Verify alert is flagged when RAID status is degraded."""
    df_output = (
        "Filesystem           Size  Used Avail Use% Mounted on\n"
        "/dev/vg1/volume_1     14T  7.0T  7.0T  50% /volume1\n"
    )
    mdstat_output = (
        "Personalities : [raid1]\n"
        "md2 : active raid1 sata1p5[2] sata2p5[1]\n"
        "      15615154816 blocks super 1.2 [2/1] [U_]\n"
    )

    def fake_get_out(ssh_cmd, cmd_str):
        if "df -h" in cmd_str:
            return df_output
        if "proc/mdstat" in cmd_str:
            return mdstat_output
        return ""

    with patch.object(nas_storage_check, "get_out", side_effect=fake_get_out):
        issues, status_line = nas_storage_check.check_host("NasHost02 (.84)", ["ssh", DOC_HOST_2], threshold=85)
        assert len(issues) == 1
        assert "RAID Array Degraded" in issues[0]
        assert "RAID1 [U_]" in status_line


def test_nas_storage_check_get_out_timeout_and_fallback():
    """Verify get_out sets timeout=12 and falls back to local execution on Host 2."""
    with patch("subprocess.check_output") as mock_check_output:
        # 1. Normal SSH call passes timeout=12
        mock_check_output.return_value = b"sample output"
        out = nas_storage_check.get_out(["ssh", DOC_HOST_1], "df -h /volume1")
        assert out == "sample output"
        assert mock_check_output.call_args.kwargs.get("timeout") == 12

    # 2. Host 2 fallback when SSH fails
    with patch("subprocess.check_output") as mock_check_output:
        def fake_check(cmd, **kwargs):
            assert kwargs.get("timeout") == 12
            if isinstance(cmd, list):
                # SSH command fails
                raise subprocess.SubprocessError("SSH failed")
            # Local shell fallback succeeds
            return b"local mdstat output"

        mock_check_output.side_effect = fake_check
        # Call with Host 2 command
        with patch.object(nas_storage_check, "HOST_2_IP", DOC_HOST_2):
            res = nas_storage_check.get_out(["ssh", f"user@{DOC_HOST_2}"], "cat /proc/mdstat")
            assert res == "local mdstat output"

    # 3. Host 1 does not fall back to local when SSH fails
    with patch("subprocess.check_output") as mock_check_output:
        mock_check_output.side_effect = subprocess.SubprocessError("SSH failed")
        with patch.object(nas_storage_check, "HOST_2_IP", DOC_HOST_2):
            res = nas_storage_check.get_out(["ssh", f"user@{DOC_HOST_1}"], "cat /proc/mdstat")
            assert res == ""


def test_run_storage_check_quiet_healthy_silence(capsys):
    """Verify run_storage_check prints nothing when quiet=True and healthy."""
    with patch.object(nas_storage_check, "check_host") as mock_check:
        mock_check.side_effect = [
            ([], "• Host1 (.82): 68% used (11 TB free) — RAID5 [UUUU]"),
            ([], "• Host2 (.84): 49% used (7.2 TB free) — RAID1 [UU]"),
        ]
        nas_storage_check.run_storage_check(quiet=True)
        captured = capsys.readouterr()
        assert captured.out == ""


def test_run_storage_check_non_quiet_live_output(capsys):
    """Verify run_storage_check prints live parsed metrics when quiet=False."""
    with patch.object(nas_storage_check, "check_host") as mock_check:
        mock_check.side_effect = [
            ([], "• Host1 (.82): 65% used (12 TB free) — RAID5 [UUUU]"),
            ([], "• Host2 (.84): 42% used (8.1 TB free) — RAID1 [UU]"),
        ]
        nas_storage_check.run_storage_check(quiet=False)
        captured = capsys.readouterr()
        assert "Synology Storage & RAID Status Healthy" in captured.out
        assert "• Host1 (.82): 65% used (12 TB free) — RAID5 [UUUU]" in captured.out
        assert "• Host2 (.84): 42% used (8.1 TB free) — RAID1 [UU]" in captured.out


# ==============================================================================
# 2. dockhand_update tests
# ==============================================================================


def test_dockhand_update_quiet_suppresses_stdout_on_stage_decision(capsys):
    """Verify dockhand_update suppresses stdout when quiet=True and stage_decision succeeds."""
    with (
        patch.object(dockhand_update, "get_hub_latest_digest", return_value=("sha256:1111111111111111111", "2026-10-30")),
        patch.object(dockhand_update, "get_host_digest", side_effect=["sha256:0000000000000000000", "sha256:1111111111111111111"]),
        patch("tools.forge.stage_decision", return_value={"id": 42}) as mock_stage,
    ):
        dockhand_update.check_updates(quiet=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        mock_stage.assert_called_once()
        kwargs = mock_stage.call_args.kwargs
        assert kwargs["category"] == "ops_upgrade"
        assert kwargs["post_to_discord"] is True


def test_dockhand_update_quiet_prints_on_stage_decision_failure(capsys):
    """Verify dockhand_update prints fallback choices if stage_decision raises an exception."""
    with (
        patch.object(dockhand_update, "get_hub_latest_digest", return_value=("sha256:1111111111111111111", "2026-10-30")),
        patch.object(dockhand_update, "get_host_digest", side_effect=["sha256:0000000000000000000", "sha256:1111111111111111111"]),
        patch("tools.forge.stage_decision", side_effect=RuntimeError("DB locked")),
    ):
        dockhand_update.check_updates(quiet=True)
        captured = capsys.readouterr()
        assert "Dockhand Update Available" in captured.out
        assert "[CHOICES:" in captured.out
        assert "DB locked" in captured.out


def test_dockhand_update_non_quiet_prints_update(capsys):
    """Verify dockhand_update prints update information when quiet=False."""
    with (
        patch.object(dockhand_update, "get_hub_latest_digest", return_value=("sha256:1111111111111111111", "2026-10-30")),
        patch.object(dockhand_update, "get_host_digest", side_effect=["sha256:0000000000000000000", "sha256:1111111111111111111"]),
        patch("tools.forge.stage_decision", return_value={"id": 42}),
    ):
        dockhand_update.check_updates(quiet=False)
        captured = capsys.readouterr()
        assert "Dockhand Update Available" in captured.out
        assert "Host1 (.82)" in captured.out
        assert "Update ready to install" in captured.out


def test_dockhand_update_up_to_date(capsys):
    """Verify dockhand_update prints up to date when quiet=False and nothing when quiet=True."""
    with (
        patch.object(dockhand_update, "get_hub_latest_digest", return_value=("sha256:1111111111111111111", "2026-10-30")),
        patch.object(dockhand_update, "get_host_digest", return_value="sha256:1111111111111111111"),
    ):
        # Quiet
        dockhand_update.check_updates(quiet=True)
        assert capsys.readouterr().out == ""

        # Non-quiet
        dockhand_update.check_updates(quiet=False)
        assert "Dockhand is up to date" in capsys.readouterr().out


# ==============================================================================
# 3. ha_update_check tests
# ==============================================================================


def test_ha_update_check_quiet_suppresses_stdout_on_stage_decision(capsys):
    """Verify ha_update_check suppresses stdout when quiet=True and stage_decision succeeds."""
    with (
        patch.object(ha_update_check, "get_installed_ha_version", return_value="2026.10.1"),
        patch.object(ha_update_check, "get_latest_ha_stable", return_value="2026.10.3"),
        patch.object(ha_update_check, "is_patch_stable", return_value=True),
        patch("tools.forge.stage_decision", return_value={"id": 43}) as mock_stage,
    ):
        ha_update_check.check_updates(quiet=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        mock_stage.assert_called_once()
        kwargs = mock_stage.call_args.kwargs
        assert kwargs["category"] == "ha_upgrade"
        assert kwargs["channel_id"] == ha_update_check.HOME_ASSISTANT_CHANNEL_ID
        assert kwargs["post_to_discord"] is True


def test_ha_update_check_quiet_prints_on_stage_decision_failure(capsys):
    """Verify ha_update_check prints fallback choices if stage_decision raises an exception."""
    with (
        patch.object(ha_update_check, "get_installed_ha_version", return_value="2026.10.1"),
        patch.object(ha_update_check, "get_latest_ha_stable", return_value="2026.10.3"),
        patch.object(ha_update_check, "is_patch_stable", return_value=True),
        patch("tools.forge.stage_decision", side_effect=RuntimeError("Discord unreachable")),
    ):
        ha_update_check.check_updates(quiet=True)
        captured = capsys.readouterr()
        assert "Smart Home Stack Updates Available" in captured.out
        assert "[CHOICES:" in captured.out
        assert "Discord unreachable" in captured.out


def test_ha_update_check_non_quiet_prints_update(capsys):
    """Verify ha_update_check prints update when quiet=False."""
    with (
        patch.object(ha_update_check, "get_installed_ha_version", return_value="2026.10.1"),
        patch.object(ha_update_check, "get_latest_ha_stable", return_value="2026.10.3"),
        patch.object(ha_update_check, "is_patch_stable", return_value=True),
        patch("tools.forge.stage_decision", return_value={"id": 43}),
    ):
        ha_update_check.check_updates(quiet=False)
        captured = capsys.readouterr()
        assert "Smart Home Stack Updates Available" in captured.out
        assert "`v2026.10.1` → `v2026.10.3`" in captured.out


def test_ha_update_check_up_to_date(capsys):
    """Verify ha_update_check up to date handling."""
    with (
        patch.object(ha_update_check, "get_installed_ha_version", return_value="2026.10.3"),
        patch.object(ha_update_check, "get_latest_ha_stable", return_value="2026.10.3"),
    ):
        ha_update_check.check_updates(quiet=True)
        assert capsys.readouterr().out == ""

        ha_update_check.check_updates(quiet=False)
        assert "Smart Home Stack is up to date" in capsys.readouterr().out


# ==============================================================================
# 4. update_antigravity tests
# ==============================================================================


def test_update_antigravity_quiet_suppresses_stdout_on_stage_decision(capsys):
    """Verify update_antigravity suppresses stdout when quiet=True and stage_decision succeeds."""
    with (
        patch.object(update_antigravity, "get_current_version", return_value="1.0.0"),
        patch.object(update_antigravity, "get_latest_version", return_value="1.1.0"),
        patch("tools.forge.stage_decision", return_value={"id": 44}) as mock_stage,
    ):
        update_antigravity.check_updates(quiet=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        mock_stage.assert_called_once()
        kwargs = mock_stage.call_args.kwargs
        assert kwargs["category"] == "ops_upgrade"
        assert kwargs["post_to_discord"] is True


def test_update_antigravity_quiet_prints_on_stage_decision_failure(capsys):
    """Verify update_antigravity prints fallback choices if stage_decision raises an exception."""
    with (
        patch.object(update_antigravity, "get_current_version", return_value="1.0.0"),
        patch.object(update_antigravity, "get_latest_version", return_value="1.1.0"),
        patch("tools.forge.stage_decision", side_effect=RuntimeError("Staging failure")),
    ):
        update_antigravity.check_updates(quiet=True)
        captured = capsys.readouterr()
        assert "New Antigravity CLI Release Available" in captured.out
        assert "[CHOICES:" in captured.out
        assert "Staging failure" in captured.out


def test_update_antigravity_non_quiet_prints_update(capsys):
    """Verify update_antigravity prints update information when quiet=False."""
    with (
        patch.object(update_antigravity, "get_current_version", return_value="1.0.0"),
        patch.object(update_antigravity, "get_latest_version", return_value="1.1.0"),
        patch("tools.forge.stage_decision", return_value={"id": 44}),
    ):
        update_antigravity.check_updates(quiet=False)
        captured = capsys.readouterr()
        assert "New Antigravity CLI Release Available" in captured.out
        assert "• **Installed Version:** `v1.0.0`" in captured.out
        assert "• **Latest Release:** `v1.1.0`" in captured.out


def test_update_antigravity_up_to_date(capsys):
    """Verify update_antigravity up to date handling."""
    with (
        patch.object(update_antigravity, "get_current_version", return_value="1.1.0"),
        patch.object(update_antigravity, "get_latest_version", return_value="1.1.0"),
    ):
        update_antigravity.check_updates(quiet=True)
        assert capsys.readouterr().out == ""

        update_antigravity.check_updates(quiet=False)
        assert "Antigravity CLI is up to date" in capsys.readouterr().out
