#!/usr/bin/env python3
# ruff: noqa
"""
Unit tests for tools/dsm_update_monitor.py using RFC 5737 documentation IPs.
Verifies:
- --check (/etc.defaults/VERSION + chroot /host /usr/syno/sbin/synoupgrade --check)
- 1.task hotfix drift remediation (action=#schedule:dsm_autoupdate_hotfix# -> action=#schedule:dsm_autoupdate_notify#)
- Atomic SQLite Decision Card staging in #zero-chat (1542081375287640084)
- --apply --host <1|2> (Control Panel SYNO.Core.Upgrade WebAPI flow + reboot in background via chroot)
- --ensure-adb (chmod -R 666 /dev/bus/usb/*, pixel_adb.sh update, :5037 server, tcp:9223 forward)
- Weekly schedule entry in data/schedule.json
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
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

        cmd1 = ssh_calls[0][1]
        assert "chroot /host sh -c \"echo '" in cmd1
        assert "' | base64 -d | sh\"" in cmd1

        match = re.search(r"chroot /host sh -c \"echo '([A-Za-z0-9+/=]+)' \| base64 -d \| sh\"", cmd1)
        assert match is not None
        decoded_script = base64.b64decode(match.group(1)).decode("utf-8")
        assert decoded_script == dsm_update_monitor.build_dsm_upgrade_script()
        assert "api=SYNO.Core.Upgrade.Server method=check version=3 user_reading=true need_auto_smallupdate=true" in decoded_script
        assert "api=SYNO.Core.Upgrade.Server method=check version=1" in decoded_script
        assert "ready_upgrade" in decoded_script
        assert "api=SYNO.Core.Upgrade.Server.Download method=start version=2 target='\"update\"' need_auto_smallupdate=true" in decoded_script
        assert "api=SYNO.Core.Upgrade.Server.Download method=start version=1" in decoded_script
        assert "api=SYNO.Core.Upgrade.Server.Download method=progress version=2 need_download_target=true" in decoded_script
        assert "api=SYNO.Core.Upgrade method=status version=1 feasibility_check=true" in decoded_script
        assert "/usr/syno/bin/synowebapi --exec api=SYNO.Core.Upgrade method=start version=1 force=true type='\"server\"'" in decoded_script
        assert "/usr/syno/sbin/synoupgrade --autoupdate" in decoded_script
        assert "api=SYNO.Core.Upgrade method=progress version=1" in decoded_script
        assert "api=SYNO.Core.System method=reboot version=1 force=true local=false" in decoded_script

        ssh_calls.clear()
        rc2 = dsm_update_monitor.main(["--apply", "--host", "2"])
        assert rc2 == 0
        assert len(ssh_calls) == 1
        assert ssh_calls[0][0] == MOCK_HOST_2_IP

        ssh_calls.clear()
        res = dsm_update_monitor.apply_dsm_update(1)
        assert res["ok"] is True
        assert "type='\"server\"'" in res["script"]
        assert "SYNO.Core.System method=reboot version=1 force=true local=false" in res["script"]


def _run_upgrade_script_with_mocks(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Execute build_dsm_upgrade_script() under POSIX sh with mocked synowebapi/synoupgrade/sleep."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_file = tmp_path / "calls.log"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    sleep_sh = bin_dir / "sleep"
    sleep_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sleep_sh.chmod(0o755)

    synowebapi_py = bin_dir / "synowebapi"
    synowebapi_py.write_text(
        f"""#!/usr/bin/env python3
import sys
from pathlib import Path

args = " ".join(sys.argv[1:])
with open({str(log_file)!r}, "a", encoding="utf-8") as f:
    f.write("synowebapi: " + args + "\\n")

state_dir = Path({str(state_dir)!r})
scenario = {scenario!r}

if "api=SYNO.Core.Upgrade.Server method=check version=3" in args:
    if scenario == "fallback":
        print('{{"success":false,"error":{{"code":103}}}}')
    elif scenario == "ready_upgrade":
        print('{{"data":{{"status":"ready_upgrade"}},"success":true}}')
    else:
        print('{{"data":{{"status":"available"}},"success":true}}')
elif "api=SYNO.Core.Upgrade.Server method=check version=1" in args:
    print('{{"data":{{"status":"available"}},"success":true}}')
elif "api=SYNO.Core.Upgrade.Server.Download method=start version=2" in args:
    if scenario == "fallback":
        print('{{"success":false}}')
    else:
        print('{{"success":true}}')
elif "api=SYNO.Core.Upgrade.Server.Download method=start version=1" in args:
    print('{{"success":true}}')
elif "api=SYNO.Core.Upgrade.Server.Download method=progress" in args:
    cnt_file = state_dir / "dl_cnt"
    cnt = int(cnt_file.read_text()) + 1 if cnt_file.exists() else 1
    cnt_file.write_text(str(cnt))
    if cnt == 1:
        print('{{"data":{{"status":"downloading","percent":"42","downloading":true,"progress":42}},"success":true}}')
    else:
        print('{{"data":{{"status":"finished","percent":"100","downloading":false,"progress":100}},"success":true}}')
elif "api=SYNO.Core.Upgrade method=status version=1 feasibility_check=true" in args:
    print('{{"data":{{"feasibility":true}},"success":true}}')
elif "api=SYNO.Core.Upgrade method=start version=1" in args:
    if scenario == "fallback":
        print('{{"success":false,"error":{{"code":400}}}}')
    else:
        print('{{"success" : true}}')
elif "api=SYNO.Core.Upgrade method=progress version=1" in args:
    cnt_file = state_dir / "upg_cnt"
    cnt = int(cnt_file.read_text()) + 1 if cnt_file.exists() else 1
    cnt_file.write_text(str(cnt))
    if cnt == 1:
        print('{{"data":{{"step":"install","percent":50}},"success":true}}')
    elif scenario == "ready_upgrade":
        print('{{"data":{{"step":"finish","percent":100}},"success":true}}')
    else:
        print('{{"data":{{"step" : "reboot","percent":100}},"success":true}}')
elif "api=SYNO.Core.System method=reboot" in args:
    print('{{"success":true}}')
""",
        encoding="utf-8",
    )
    synowebapi_py.chmod(0o755)

    synoupgrade_sh = bin_dir / "synoupgrade"
    synoupgrade_sh.write_text(
        f'#!/bin/sh\necho "synoupgrade: $*" >> {str(log_file)!r}\nexit 0\n',
        encoding="utf-8",
    )
    synoupgrade_sh.chmod(0o755)

    script = dsm_update_monitor.build_dsm_upgrade_script()
    script = script.replace("/usr/syno/bin/synowebapi", str(synowebapi_py))
    script = script.replace("/usr/syno/sbin/synoupgrade", str(synoupgrade_sh))

    proc = subprocess.run(
        ["sh"],
        input=script,
        text=True,
        capture_output=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=False,
    )
    calls = log_file.read_text(encoding="utf-8").splitlines() if log_file.exists() else []
    return proc, calls


def test_dsm_upgrade_script_download_start_server_and_reboot_flow(tmp_path: Path):
    proc, calls = _run_upgrade_script_with_mocks(tmp_path, "full_reboot")
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "synowebapi: --exec api=SYNO.Core.Upgrade.Server method=check version=3 user_reading=true need_auto_smallupdate=true",
        'synowebapi: --exec api=SYNO.Core.Upgrade.Server.Download method=start version=2 target="update" need_auto_smallupdate=true',
        "synowebapi: --exec api=SYNO.Core.Upgrade.Server.Download method=progress version=2 need_download_target=true",
        "synowebapi: --exec api=SYNO.Core.Upgrade.Server.Download method=progress version=2 need_download_target=true",
        "synowebapi: --exec api=SYNO.Core.Upgrade method=status version=1 feasibility_check=true",
        'synowebapi: --exec api=SYNO.Core.Upgrade method=start version=1 force=true type="server"',
        "synowebapi: --exec api=SYNO.Core.Upgrade method=progress version=1",
        "synowebapi: --exec api=SYNO.Core.Upgrade method=progress version=1",
        "synowebapi: --exec api=SYNO.Core.System method=reboot version=1 force=true local=false",
    ]


def test_dsm_upgrade_script_skips_download_when_ready_upgrade(tmp_path: Path):
    proc, calls = _run_upgrade_script_with_mocks(tmp_path, "ready_upgrade")
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "synowebapi: --exec api=SYNO.Core.Upgrade.Server method=check version=3 user_reading=true need_auto_smallupdate=true",
        "synowebapi: --exec api=SYNO.Core.Upgrade method=status version=1 feasibility_check=true",
        'synowebapi: --exec api=SYNO.Core.Upgrade method=start version=1 force=true type="server"',
        "synowebapi: --exec api=SYNO.Core.Upgrade method=progress version=1",
        "synowebapi: --exec api=SYNO.Core.Upgrade method=progress version=1",
    ]


def test_dsm_upgrade_script_fallbacks_to_v1_and_synoupgrade_autoupdate(tmp_path: Path):
    proc, calls = _run_upgrade_script_with_mocks(tmp_path, "fallback")
    assert proc.returncode == 0, proc.stderr
    assert "synowebapi: --exec api=SYNO.Core.Upgrade.Server method=check version=1" in calls
    assert "synowebapi: --exec api=SYNO.Core.Upgrade.Server.Download method=start version=1" in calls
    assert "synoupgrade: --autoupdate" in calls


def test_apply_dsm_update_balanced_quotes_and_valid_sh_syntax():
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        return 0, "started", ""

    with (
        patch.object(dsm_update_monitor.nas_config, "HOST_1_IP", MOCK_HOST_1_IP),
        patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh),
    ):
        res = dsm_update_monitor.apply_dsm_update(1)

    cmd = res["command"]
    assert len(ssh_calls) == 1
    assert cmd == ssh_calls[0][1]

    # Balanced quotes and valid shell tokenization (no nested single-quote break)
    assert cmd.count('"') % 2 == 0
    assert cmd.count("'") % 2 == 0
    tokens = shlex.split(cmd)
    sh_c_idx = tokens.index("-c")
    inner_pipeline = tokens[sh_c_idx + 1]
    assert re.fullmatch(r"echo '[A-Za-z0-9+/=]+' \| base64 -d \| sh", inner_pipeline)

    # Verify POSIX sh -n syntax validation passes for outer command, inner pipeline, and decoded script
    for label, shell_snippet in (
        ("outer_cmd", cmd),
        ("inner_pipeline", inner_pipeline),
        ("decoded_script", res["script"]),
    ):
        syntax_check = subprocess.run(
            ["sh", "-n"],
            input=shell_snippet,
            text=True,
            capture_output=True,
            check=False,
        )
        assert syntax_check.returncode == 0, f"{label} failed sh -n: {syntax_check.stderr}"


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
    assert "chmod -R a+rwX /dev/bus/usb" in rc_script
    assert "chmod 755" in rc_script
    assert "adb_host.log" in rc_script
    assert "start-server" in rc_script
    assert "fork-server" not in rc_script
    assert "36261FDJG0003L" in rc_script
    assert "tcp:9223" in rc_script

    with (
        patch.object(dsm_update_monitor.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(dsm_update_monitor.nas_config, "HOST_2_DOCKER_ROOT", "/mock_docker"),
        patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh),
    ):
        res = dsm_update_monitor.ensure_host2_adb()
        assert res["ok"] is True
        assert res["device_online"] is True
        assert res["forward_active"] is True
        assert len(ssh_calls) == 1
        assert ssh_calls[0][0] == MOCK_HOST_2_IP
        assert "/mock_docker/discord-agy-agent" in ssh_calls[0][1]
        assert "chmod -R a+rwX /dev/bus/usb" in ssh_calls[0][1]
        assert "adb_host.log" in ssh_calls[0][1]
        assert "/usr/local/etc/rc.d/pixel_adb.sh" in ssh_calls[0][1]
        assert "start-server" in ssh_calls[0][1]
        assert "fork-server" not in ssh_calls[0][1]
        assert "</dev/null" in ssh_calls[0][1]
        assert "5037" in ssh_calls[0][1]
        assert "tcp:9223" in ssh_calls[0][1]


def test_upgrade_setting_drift_remediation_via_synowebapi_preserves_schedule():
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        return 0, '{"success":true}', ""

    wrapped_drift = json.dumps(
        {
            "data": {
                "autoupdate_type": "hotfix-security",
                "smart_nano_enabled": True,
                "schedule": {"date_type": 0, "hour": 2, "minute": 30},
            },
            "success": True,
        }
    )

    with patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh):
        remediated, setting = dsm_update_monitor.check_and_remediate_upgrade_setting(
            MOCK_HOST_1_IP,
            setting_output=wrapped_drift,
        )

    assert remediated is True
    assert setting["autoupdate_type"] == "notify"
    assert setting["smart_nano_enabled"] is False
    assert setting["schedule"] == {"date_type": 0, "hour": 2, "minute": 30}
    assert len(ssh_calls) == 1
    assert ssh_calls[0][0] == MOCK_HOST_1_IP

    cmd = ssh_calls[0][1]
    match = re.search(r"chroot /host sh -c \"echo '([A-Za-z0-9+/=]+)' \| base64 -d \| sh\"", cmd)
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode("utf-8")
    assert "api=SYNO.Core.Upgrade.Setting method=set version=4" in decoded
    assert "autoupdate_type='\"notify\"'" in decoded
    assert "smart_nano_enabled=false" in decoded
    assert 'schedule=\'{"date_type":0,"hour":2,"minute":30}\'' in decoded
    syntax_check = subprocess.run(["sh", "-n"], input=decoded, text=True, capture_output=True, check=False)
    assert syntax_check.returncode == 0, syntax_check.stderr

    # Also test flat dict with only smart_nano_enabled=True
    ssh_calls.clear()
    flat_nano_drift = json.dumps(
        {
            "autoupdate_type": "notify",
            "smart_nano_enabled": True,
            "schedule": {"date_type": 1, "hour": 4, "minute": 0},
        }
    )
    with patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh):
        remediated2, setting2 = dsm_update_monitor.check_and_remediate_upgrade_setting(
            MOCK_HOST_2_IP,
            setting_output=flat_nano_drift,
        )
    assert remediated2 is True
    assert setting2["autoupdate_type"] == "notify"
    assert setting2["smart_nano_enabled"] is False
    assert len(ssh_calls) == 1


def test_upgrade_setting_update_conf_fallback_when_schedule_absent(tmp_path: Path):
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        return 0, "", ""

    conf_drift = 'autoupdate_type="hotfix-security"\nsmart_nano_enabled="true"\n'
    with patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh):
        remediated, setting = dsm_update_monitor.check_and_remediate_upgrade_setting(
            MOCK_HOST_1_IP,
            setting_output=conf_drift,
        )

    assert remediated is True
    assert setting["source"] == "update.conf"
    assert setting["autoupdate_type"] == "notify"
    assert setting["smart_nano_enabled"] is False
    assert len(ssh_calls) == 1

    match = re.search(r"chroot /host sh -c \"echo '([A-Za-z0-9+/=]+)' \| base64 -d \| sh\"", ssh_calls[0][1])
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode("utf-8")
    assert "/usr/syno/etc/update.conf" in decoded

    # Execute the decoded update.conf script against a temporary file to verify idempotent rewrite
    tmp_conf = tmp_path / "update.conf"
    tmp_conf.write_text(conf_drift, encoding="utf-8")
    test_script = decoded.replace("/usr/syno/etc/update.conf", str(tmp_conf))
    proc = subprocess.run(["sh"], input=test_script, text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr
    updated_text = tmp_conf.read_text(encoding="utf-8")
    assert 'autoupdate_type="notify"' in updated_text
    assert 'smart_nano_enabled="false"' in updated_text


def test_upgrade_setting_noop_when_clean_or_empty_or_unparseable():
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        return 0, "", ""

    clean_json = json.dumps(
        {
            "data": {
                "autoupdate_type": "notify",
                "smart_nano_enabled": False,
                "schedule": {"date_type": 0, "hour": 3, "minute": 0},
            },
            "success": True,
        }
    )
    clean_conf = 'autoupdate_type="notify"\nsmart_nano_enabled="false"\n'
    samples = [
        clean_json,
        clean_conf,
        "",
        "   \n",
        "chroot: can't execute '/usr/syno/bin/synowebapi': No such file or directory",
        "{not-valid-json",
        '{"success": false, "error": {"code": 119}}',
    ]

    with patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh):
        for idx, sample in enumerate(samples):
            remediated, setting = dsm_update_monitor.check_and_remediate_upgrade_setting(
                MOCK_HOST_1_IP,
                setting_output=sample,
            )
            assert remediated is False, f"sample[{idx}] unexpectedly triggered remediation"
            if idx in (0, 1):
                assert setting["autoupdate_type"] == "notify"
                assert setting["smart_nano_enabled"] is False
            else:
                assert setting == {}
    assert len(ssh_calls) == 0


def test_check_host_dsm_upgrade_setting_split_and_quiet_suppression(capsys):
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host: str, cmd: str, **kwargs):
        ssh_calls.append((host, cmd))
        if "===TASK_SPLIT===" in cmd:
            if host == MOCK_HOST_1_IP:
                setting_json = json.dumps(
                    {
                        "data": {
                            "autoupdate_type": "hotfix-security",
                            "smart_nano_enabled": True,
                            "schedule": {"date_type": 0, "hour": 2, "minute": 0},
                        },
                        "success": True,
                    }
                )
                out = (
                    f"{SAMPLE_VERSION_HOST1}\n===TASK_SPLIT===\n"
                    f"{SAMPLE_TASK_CLEAN}\n===UPGRADE_SPLIT===\n"
                    f"{SAMPLE_CHECK_NONE}\n===UPGRADE_SETTING_SPLIT===\n"
                    f"{setting_json}"
                )
                return 0, out, ""
            if host == MOCK_HOST_2_IP:
                out = (
                    f"{SAMPLE_VERSION_HOST2}\n===TASK_SPLIT===\n"
                    f"{SAMPLE_TASK_CLEAN}\n===UPGRADE_SPLIT===\n"
                    f"{SAMPLE_CHECK_NONE}\n===UPGRADE_SETTING_SPLIT===\n"
                    '{"data":{"autoupdate_type":"notify","smart_nano_enabled":false},"success":true}'
                )
                return 0, out, ""
        return 0, '{"success":true}', ""

    with (
        patch.object(dsm_update_monitor.nas_config, "HOST_1_IP", MOCK_HOST_1_IP),
        patch.object(dsm_update_monitor.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(dsm_update_monitor.nas_config, "run_ssh", side_effect=fake_run_ssh),
    ):
        rc = dsm_update_monitor.main(["--check", "--quiet"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "Remediated Upgrade.Setting -> notify, smart_nano=false" in captured

        sidecar_summary = dsm_update_monitor.run_dsm_update_sidecar(
            ensure_adb=False,
            stage_card=False,
            post_to_discord=False,
            quiet=True,
        )
        assert "Remediated Upgrade.Setting -> notify, smart_nano=false" in sidecar_summary


def test_schedule_json_contains_dsm_update_monitor():
    sched_path = Path(__file__).resolve().parent.parent / "data" / "schedule.json"
    if not sched_path.exists():
        alt_path = Path("/workspace/data/schedule.json")
        if alt_path.exists():
            sched_path = alt_path
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
