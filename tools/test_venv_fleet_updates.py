"""Unit tests for fleet-wide Antigravity parity check and monthly persistent venv updater."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Prioritize worktree directory over /workspace in sys.path
sys.path = [p for p in sys.path if p not in ("/workspace", "/workspace/tools")]
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
sys.path.insert(0, REPO_ROOT)

from tools import update_antigravity, venv_update_check

# RFC 5737 documentation IP for unit test isolation
DOC_HOST_2 = "203.0.113.84"


# ==============================================================================
# 1. Antigravity fleet-parity drift tests
# ==============================================================================


def test_find_fleet_drift_identifies_lagging_containers_and_ignores_unknown():
    """find_fleet_drift returns lagging containers while ignoring unknown/empty entries."""
    fleet = {
        "discord-antigravity-agent": "1.5.14",
        "discord-ivy-agent": "v1.5.13",
        "discord-vulcan-agent": "unknown",
        "extra-container": "1.0.0",
    }
    drifted = update_antigravity.find_fleet_drift("v1.5.14", fleet)
    assert drifted == {"discord-ivy-agent": "1.5.13"}

    assert update_antigravity.find_fleet_drift("unknown", fleet) == {}
    assert update_antigravity.find_fleet_drift("1.5.14", {}) == {}


def test_check_updates_stages_force_card_when_fleet_drifts_and_local_matches(capsys):
    """When Zero is on 1.5.14 but Ivy/Vulcan lag on 1.5.13, stage Decision Card with --force."""
    fleet = {
        "discord-antigravity-agent": "1.5.14",
        "discord-ivy-agent": "1.5.13",
        "discord-vulcan-agent": "1.5.13",
    }
    with (
        patch.object(update_antigravity, "get_current_version", return_value="1.5.14"),
        patch.object(update_antigravity, "get_latest_version", return_value="1.5.14"),
        patch.object(update_antigravity, "verify_fleet_versions", return_value=fleet),
        patch.object(update_antigravity, "uses_shared_binary_mount", return_value=True),
        patch("tools.forge.stage_decision", return_value={"id": 101}) as mock_stage,
    ):
        update_antigravity.check_updates(quiet=True)
        assert capsys.readouterr().out == ""
        mock_stage.assert_called_once()
        kwargs = mock_stage.call_args.kwargs
        assert kwargs["category"] == "ops_upgrade"
        assert kwargs["post_to_discord"] is True
        assert kwargs["payload"]["cwd"] == "/workspace"
        assert (
            kwargs["payload"]["command"]
            == "python3 /workspace/tools/update_antigravity.py upgrade 1.5.14 --force"
        )
        summary = kwargs["summary"]
        assert "Approve Action:" in summary
        assert "discord-ivy-agent" in summary
        assert "discord-vulcan-agent" in summary
        assert "1.5.13" in summary


def test_check_updates_silent_when_all_fleet_containers_match(capsys):
    """When local and all fleet containers match latest, no Decision Card is staged."""
    fleet = {
        "discord-antigravity-agent": "1.5.14",
        "discord-ivy-agent": "1.5.14",
        "discord-vulcan-agent": "1.5.14",
    }
    with (
        patch.object(update_antigravity, "get_current_version", return_value="1.5.14"),
        patch.object(update_antigravity, "get_latest_version", return_value="1.5.14"),
        patch.object(update_antigravity, "verify_fleet_versions", return_value=fleet),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        update_antigravity.check_updates(quiet=True)
        assert capsys.readouterr().out == ""
        mock_stage.assert_not_called()

        update_antigravity.check_updates(quiet=False)
        assert "Antigravity CLI is up to date" in capsys.readouterr().out
        mock_stage.assert_not_called()


def test_check_updates_ignores_unknown_fleet_versions_gracefully(capsys):
    """When containers report unknown (e.g. SSH unreachable) and local matches latest, stay quiet."""
    fleet = {
        "discord-antigravity-agent": "unknown",
        "discord-ivy-agent": "unknown",
        "discord-vulcan-agent": "unknown",
    }
    with (
        patch.object(update_antigravity, "get_current_version", return_value="1.5.14"),
        patch.object(update_antigravity, "get_latest_version", return_value="1.5.14"),
        patch.object(update_antigravity, "verify_fleet_versions", return_value=fleet),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        update_antigravity.check_updates(quiet=True)
        assert capsys.readouterr().out == ""
        mock_stage.assert_not_called()


# ==============================================================================
# 2. venv_update_check classification, staging, and upgrade tests
# ==============================================================================


def test_classify_outdated_splits_safe_vs_major():
    """classify_outdated separates same-major updates from major-version jumps."""
    assert venv_update_check.parse_major_version("1.26.4") == 1
    assert venv_update_check.parse_major_version("v2.0.0") == 2
    assert venv_update_check.parse_major_version("invalid") is None

    packages = [
        {"name": "matplotlib", "version": "3.8.0", "latest_version": "3.9.2"},
        {"name": "numpy", "version": "1.26.4", "latest_version": "2.1.1"},
        {"name": "fastembed", "version": "0.3.1", "latest_version": "0.4.0"},
        {"name": "pydantic", "version": "1.10.12", "latest_version": "2.9.2"},
        {"name": "weirdpkg", "version": "abc", "latest_version": "1.0.0"},
    ]
    safe, major = venv_update_check.classify_outdated(packages)
    assert [p["name"] for p in safe] == ["matplotlib", "fastembed"]
    assert [p["name"] for p in major] == ["numpy", "pydantic", "weirdpkg"]


def test_venv_check_updates_stages_decision_card_and_silent_when_up_to_date(capsys):
    """check_updates stages Decision Card with Approve Action when outdated, silent when up to date."""
    # 1. All up to date -> silent when quiet, no card staged
    with (
        patch.object(venv_update_check, "list_outdated_packages", return_value=[]),
        patch("tools.forge.stage_decision") as mock_stage,
    ):
        venv_update_check.check_updates(quiet=True)
        assert capsys.readouterr().out == ""
        mock_stage.assert_not_called()

        venv_update_check.check_updates(quiet=False)
        assert "up to date" in capsys.readouterr().out.lower()
        mock_stage.assert_not_called()

    # 2. Outdated packages across targets -> single Decision Card, silent when quiet
    outdated_map = {
        "/workspace/.venv/extras": [
            {"name": "matplotlib", "version": "3.8.0", "latest_version": "3.9.2"},
        ],
        "/workspace/.venv/modal": [],
        "/workspace/.venv/baseball": [
            {"name": "pandas", "version": "2.1.0", "latest_version": "2.2.3"},
            {"name": "numpy", "version": "1.26.4", "latest_version": "2.1.0"},
        ],
    }

    with (
        patch.object(
            venv_update_check,
            "list_outdated_packages",
            side_effect=lambda t: outdated_map.get(t["venv_path"], []),
        ),
        patch("tools.forge.stage_decision", return_value={"id": 202}) as mock_stage,
    ):
        venv_update_check.check_updates(quiet=True)
        assert capsys.readouterr().out == ""
        mock_stage.assert_called_once()
        kwargs = mock_stage.call_args.kwargs
        assert kwargs["category"] == "ops_upgrade"
        assert kwargs["title"] == "Upgrade Persistent Agent Virtualenv Packages"
        assert kwargs["handler_type"] == "shell"
        assert kwargs["payload"] == {
            "command": "python3 /workspace/tools/venv_update_check.py upgrade",
            "cwd": "/workspace",
        }
        assert kwargs["post_to_discord"] is True
        expected_action = (
            "**Approve Action:** Upgrades safe minor/patch packages across the persistent "
            "virtualenvs and runs import smoke tests (skipping major-version jumps)."
        )
        assert expected_action in kwargs["summary"]
        assert "Zero Extras" in kwargs["summary"]
        assert "Ivy Baseball" in kwargs["summary"]
        assert "1 safe, 0 major" in kwargs["summary"]
        assert "1 safe, 1 major" in kwargs["summary"]


def test_venv_perform_upgrade_safe_only_and_include_major(monkeypatch):
    """perform_upgrade upgrades safe packages only by default, respects --include-major, and runs smoke imports."""
    monkeypatch.setattr(venv_update_check.nas_config, "HOST_2_IP", DOC_HOST_2)

    outdated_by_venv = {
        "/workspace/.venv/extras": [
            {"name": "matplotlib", "version": "3.8.0", "latest_version": "3.9.2"},
            {"name": "playwright", "version": "1.45.0", "latest_version": "2.0.0"},
        ],
        "/workspace/.venv/modal": [],
        "/workspace/.venv/baseball": [
            {"name": "pandas", "version": "2.1.0", "latest_version": "2.2.3"},
            {"name": "numpy", "version": "1.26.4", "latest_version": "2.1.0"},
        ],
    }

    local_calls: list[list[str]] = []
    ssh_calls: list[tuple[str, str]] = []

    def fake_subprocess_run(cmd, **kwargs):
        local_calls.append(cmd)
        if "list" in cmd and "--outdated" in cmd:
            venv_path = cmd[0].rsplit("/bin/pip", 1)[0]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(outdated_by_venv.get(venv_path, [])),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

    def fake_run_ssh(host, remote_cmd, **kwargs):
        assert host == DOC_HOST_2
        ssh_calls.append((host, remote_cmd))
        if "list --outdated --format=json" in remote_cmd:
            return 0, json.dumps(outdated_by_venv["/workspace/.venv/baseball"]), ""
        return 0, "OK\n", ""

    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch.object(venv_update_check.nas_config, "run_ssh", side_effect=fake_run_ssh),
    ):
        res_safe = venv_update_check.perform_upgrade(include_major=False)

    assert len(res_safe["upgraded"]) == 2
    assert res_safe["skipped"] == ["Zero Modal/CAD"]
    assert res_safe["failed"] == []

    # Local upgrade only upgraded matplotlib (safe), skipped playwright (major), and ran smoke imports
    local_installs = [c for c in local_calls if "install" in c]
    assert len(local_installs) == 1
    assert local_installs[0] == [
        "/workspace/.venv/extras/bin/pip",
        "install",
        "--upgrade",
        "--no-cache-dir",
        "matplotlib",
    ]
    local_smokes = [c for c in local_calls if c[0] == "/workspace/.venv/extras/bin/python3"]
    assert len(local_smokes) == 1

    # Remote Ivy upgrade only upgraded pandas (safe), skipped numpy (major), and ran smoke imports via docker exec
    ssh_installs = [cmd for _, cmd in ssh_calls if "install --upgrade --no-cache-dir" in cmd]
    assert len(ssh_installs) == 1
    assert "/usr/local/bin/docker exec discord-ivy-agent" in ssh_installs[0]
    assert "pandas" in ssh_installs[0]
    assert "numpy" not in ssh_installs[0]
    ssh_smokes = [cmd for _, cmd in ssh_calls if "/workspace/.venv/baseball/bin/python3" in cmd]
    assert len(ssh_smokes) == 1

    # Now verify include_major=True includes major jumps as well
    local_calls.clear()
    ssh_calls.clear()
    with (
        patch("subprocess.run", side_effect=fake_subprocess_run),
        patch.object(venv_update_check.nas_config, "run_ssh", side_effect=fake_run_ssh),
    ):
        res_major = venv_update_check.perform_upgrade(include_major=True)

    assert len(res_major["upgraded"]) == 2
    assert res_major["failed"] == []
    local_installs_major = [c for c in local_calls if "install" in c]
    assert "matplotlib" in local_installs_major[0]
    assert "playwright" in local_installs_major[0]
    ssh_installs_major = [cmd for _, cmd in ssh_calls if "install --upgrade --no-cache-dir" in cmd]
    assert "pandas" in ssh_installs_major[0]
    assert "numpy" in ssh_installs_major[0]


# ==============================================================================
# 3. Sidecar runner & monthly schedule wiring tests
# ==============================================================================


def test_sidecars_run_venv_update_check_delegates_to_script():
    """sidecars.run_venv_update_check runs venv_update_check.py check --quiet with a 120s timeout."""
    from tools import sidecars

    # sidecars.py inserts /workspace onto sys.path at import time; restore worktree priority
    sys.path = [p for p in sys.path if p not in ("/workspace", "/workspace/tools")]
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
    sys.path.insert(0, REPO_ROOT)

    with patch.object(
        sidecars.subprocess,
        "run",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    ) as mock_run:
        ok, out = sidecars.run_venv_update_check()
        assert ok is True
        assert out == ""
        mock_run.assert_called_once_with(
            ["python3", "/workspace/tools/venv_update_check.py", "check", "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
        )

    with patch.object(
        sidecars.subprocess,
        "run",
        return_value=SimpleNamespace(returncode=0, stdout="fallback notice\n", stderr=""),
    ):
        ok, out = sidecars.run_venv_update_check()
        assert ok is True
        assert out == "fallback notice"

    with patch.object(
        sidecars.subprocess,
        "run",
        side_effect=RuntimeError("boom"),
    ):
        ok, out = sidecars.run_venv_update_check()
        assert ok is False
        assert "boom" in out


def test_schedule_json_contains_enabled_monthly_venv_update_check():
    """data/schedule.json defines an enabled monthly venv_update_check entry routed to #zero-ops."""
    schedule_path = os.path.join(REPO_ROOT, "data", "schedule.json")
    with open(schedule_path, encoding="utf-8") as f:
        jobs = json.load(f)

    matches = [j for j in jobs if j.get("id") == "venv_update_check"]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["name"] == "Monthly Virtualenv Package Check"
    assert entry["enabled"] is True
    assert entry["schedule_type"] == "monthly"
    assert entry["day_of_month"] == 1
    assert entry["hour_pt"] == 10
    assert entry["minute_pt"] == 15
    assert entry["channel_id"] == 1544953279664889888
    assert entry["catchup_if_missed"] is True
    assert entry["catchup_window_seconds"] == 86400
    assert "venv_update_check.py" in entry["prompt"]
