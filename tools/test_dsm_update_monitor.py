#!/usr/bin/env python3
# ruff: noqa
"""
Unit tests for tools/dsm_update_monitor.py using RFC 5737 documentation IPs.
Verifies:
- --check (/etc.defaults/VERSION + chroot /host /usr/syno/sbin/synoupgrade --check)
- 1.task hotfix drift remediation (action=#schedule:dsm_autoupdate_hotfix# -> action=#schedule:dsm_autoupdate_notify#)
- Atomic SQLite Decision Card staging in #zero-chat (1542081375287640084)
- --apply --host <1|2> (synoupgrade --download then synoupgrade --start in background via chroot)
- --ensure-adb (chmod -R 666 /dev/bus/usb/*, pixel_adb.sh update, :5037 server, tcp:9223 forward)
- Weekly schedule entry in data/schedule.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import dsm_update_monitor

MOCK_HOST_1_IP = "198.51.100.82"
MOCK_HOST_2_IP = "198.51.100.84"

SAMPLE_VERSION_HOST1 = """majorversion="7"
minorversion="3"
major="7"
minor="3"
micro="2"
buildphase="GM"
buildnumber="86009"
smallfixnumber="4"
nano="4"
base="86009"
productversion="7.3.2"
os_name="DSM"
"""

SAMPLE_VERSION_HOST2 = """majorversion="7"
minorversion="4"
major="7"
minor="4"
micro="1"
buildphase="GM"
buildnumber="90080"
smallfixnumber="0"
nano="0"
base="90080"
productversion="7.4.1"
os_name="DSM"
"""

SAMPLE_TASK_DRIFT = """id=1
type=weekly
action=#schedule:dsm_autoupdate_hotfix#
name=DSM Auto Update
state=enabled
"""

SAMPLE_TASK_CLEAN = """id=1
type=weekly
action=#schedule:dsm_autoupdate_notify#
name=DSM Auto Update
state=enabled
"""

SAMPLE_CHECK_AVAILABLE = """UPGRADE_CHECKNEWDSM
Result: {"available":true,"reboot":"now","restart":"some","type":"nano","version":"DSM 7.3.2-86009 Update 5","version_details":{"buildnumber":86009,"isSecurityVersion":true,"major":7,"micro":2,"minor":3,"nano":5,"os_name":"DSM"}}
"""

SAMPLE_CHECK_NONE = """UPGRADE_CHECKNEWDSM
Result: {"available":false}
"""


def test_parse_version_and_synoupgrade_check():
    v1 = dsm_update_monitor.parse_version_file(SAMPLE_VERSION_HOST1)
    assert v1["current_version"] == "DSM 7.3.2-86009 Update 4"

    v2 = dsm_update_monitor.parse_version_file(SAMPLE_VERSION_HOST2)
    assert v2["current_version"] == "DSM 7.4.1-90080"

    c1 = dsm_update_monitor.parse_synoupgrade_check(SAMPLE_CHECK_AVAILABLE)
    assert c1["available"] is True
    assert c1["version"] == "DSM 7.3.2-86009 Update 5"
    assert c1["reboot"] == "now"

    c2 = dsm_update_monitor.parse_synoupgrade_check(SAMPLE_CHECK_NONE)
    assert c2["available"] is False


def test_schedule_hotfix_drift_remediation():
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        return 0, "", ""

    with patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh):
        remediated, action = dsm_update_monitor.check_and_remediate_schedule_task(
            MOCK_HOST_1_IP,
            task_content=SAMPLE_TASK_DRIFT,
        )
        assert remediated is True
        assert action == "action=#schedule:dsm_autoupdate_notify#"
        assert len(ssh_calls) == 1
        assert ssh_calls[0][0] == MOCK_HOST_1_IP
        assert "action=#schedule:dsm_autoupdate_hotfix#" in ssh_calls[0][1]
        assert "action=#schedule:dsm_autoupdate_notify#" in ssh_calls[0][1]
        assert "/usr/syno/etc/synoschedule.d/root/1.task" in ssh_calls[0][1]

        ssh_calls.clear()
        remediated_clean, action_clean = dsm_update_monitor.check_and_remediate_schedule_task(
            MOCK_HOST_2_IP,
            task_content=SAMPLE_TASK_CLEAN,
        )
        assert remediated_clean is False
        assert action_clean == "action=#schedule:dsm_autoupdate_notify#"
        assert len(ssh_calls) == 0


def test_check_cli_and_decision_card_staging(capsys):
    ssh_calls: list[tuple[str, str]] = []
    staged_cards: list[dict] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        if "===TASK_SPLIT===" in cmd:
            if host == MOCK_HOST_1_IP:
                out = (
                    f"{SAMPLE_VERSION_HOST1}\n===TASK_SPLIT===\n"
                    f"{SAMPLE_TASK_DRIFT}\n===UPGRADE_SPLIT===\n"
                    f"{SAMPLE_CHECK_AVAILABLE}"
                )
                return 0, out, ""
            if host == MOCK_HOST_2_IP:
                out = (
                    f"{SAMPLE_VERSION_HOST2}\n===TASK_SPLIT===\n"
                    f"{SAMPLE_TASK_CLEAN}\n===UPGRADE_SPLIT===\n"
                    f"{SAMPLE_CHECK_NONE}"
                )
                return 0, out, ""
        return 0, "", ""

    def fake_stage_decision(**kwargs):
        staged_cards.append(kwargs)
        return {"ok": True, "decision_id": "dec-12345678-dsmupda", "channel_id": kwargs.get("channel_id")}

    with (
        patch.object(dsm_update_monitor.nas_config, "HOST_1_IP", MOCK_HOST_1_IP),
        patch.object(dsm_update_monitor.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh),
        patch("tools.forge.stage_decision", side_effect=fake_stage_decision),
    ):
        rc = dsm_update_monitor.main(["--check"])
        assert rc == 0

    captured = capsys.readouterr().out
    assert "DSM 7.3.2-86009 Update 4" in captured
    assert "DSM 7.3.2-86009 Update 5" in captured
    assert "Remediated 1.task hotfix -> notify" in captured

    # Verify Decision Card staging parameters
    assert len(staged_cards) == 1
    card = staged_cards[0]
    assert card["channel_id"] == 1542081375287640084
    assert card["handler_type"] == "shell_cmd"
    assert card["payload"] == {
        "command": "python3 /workspace/tools/dsm_update_monitor.py --apply --host 1"
    }
    assert "**Approve Action:**" in card["summary"]
    assert "downloads and applies the DSM update" in card["summary"]
    assert "reboots that NAS" in card["summary"]


def test_apply_cli_runs_download_and_start():
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        return 0, "started", ""

    with (
        patch.object(dsm_update_monitor.nas_config, "HOST_1_IP", MOCK_HOST_1_IP),
        patch.object(dsm_update_monitor.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh),
    ):
        rc1 = dsm_update_monitor.main(["--apply", "--host", "1"])
        assert rc1 == 0
        assert len(ssh_calls) == 1
        assert ssh_calls[0][0] == MOCK_HOST_1_IP
        assert "chroot /host" in ssh_calls[0][1]
        assert "synoupgrade --download" in ssh_calls[0][1]
        assert "synoupgrade --start" in ssh_calls[0][1]

        ssh_calls.clear()
        rc2 = dsm_update_monitor.main(["--apply", "--host", "2"])
        assert rc2 == 0
        assert len(ssh_calls) == 1
        assert ssh_calls[0][0] == MOCK_HOST_2_IP


def test_ensure_adb_updates_usb_and_rc_script():
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        return (
            0,
            "36261FDJG0003L device product:husky\n36261FDJG0003L tcp:9223 localabstract:chrome_devtools_remote\n",
            "",
        )

    rc_script = dsm_update_monitor.build_pixel_adb_rc_script(docker_root="/mock_docker")
    assert "chmod -R 666 /dev/bus/usb/*" in rc_script
    assert "fork-server server" in rc_script
    assert "36261FDJG0003L" in rc_script
    assert "tcp:9223" in rc_script

    with (
        patch.object(dsm_update_monitor.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(dsm_update_monitor.nas_config, "DOCKER_ROOT", "/mock_docker"),
        patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh),
    ):
        res = dsm_update_monitor.ensure_host2_adb()
        assert res["ok"] is True
        assert res["device_online"] is True
        assert res["forward_active"] is True
        assert len(ssh_calls) == 1
        assert ssh_calls[0][0] == MOCK_HOST_2_IP
        assert "chmod -R 666 /dev/bus/usb/*" in ssh_calls[0][1]
        assert "/usr/local/etc/rc.d/pixel_adb.sh" in ssh_calls[0][1]
        assert "5037" in ssh_calls[0][1]
        assert "tcp:9223" in ssh_calls[0][1]


def test_schedule_json_contains_dsm_update_monitor():
    sched_path = Path(__file__).resolve().parent.parent / "data" / "schedule.json"
    entries = json.loads(sched_path.read_text(encoding="utf-8"))
    match = next((e for e in entries if e.get("id") == "dsm_update_monitor"), None)
    assert match is not None
    assert match["schedule_type"] == "weekly"
    assert match["day_of_week"] == 2  # Wednesday
    assert match["hour_pt"] == 9
    assert match["minute_pt"] == 0
    assert int(match["channel_id"]) == 1542081375287640084


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
