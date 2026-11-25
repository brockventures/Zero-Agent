"""Unit tests for tools/backup_host1.py WordPress backup & USB mountpoint guard."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent


def _load_backup_host1():
    spec = importlib.util.spec_from_file_location("backup_host1", _TOOLS_DIR / "backup_host1.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backup_host1"] = mod
    spec.loader.exec_module(mod)
    return mod


backup_host1 = _load_backup_host1()


def test_backup_wordpress_success():
    """Verify pipefail MariaDB wp01/wp02 dump, >= 1024B check, tar cache exclusions, and return msg."""
    calls: list[tuple[str, int | None]] = []

    def fake_run_ssh(cmd: str, timeout: int = 180):
        calls.append((cmd, timeout))
        if "mariadb-dump" in cmd:
            return 0, "", ""
        if cmd.startswith("stat -c %s"):
            return 0, "4096\n", ""
        if "sudo tar" in cmd:
            return 0, "", ""
        if cmd.startswith("ls -lh"):
            return 0, "wp_dbs_20261125_034500.sql.gz 4.0K wp_files_20261125_034500.tar.gz 12M\n", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_wordpress("20261125_034500")

    assert ok is True
    assert "/volumeUSB1/usbshare/backups/wordpress/wp_dbs_20261125_034500.sql.gz" in msg
    assert "/volumeUSB1/usbshare/backups/wordpress/wp_files_20261125_034500.tar.gz" in msg
    assert "12M" in msg

    dump_cmd, dump_timeout = calls[0]
    assert "bash -o pipefail -c" in dump_cmd
    assert "wordpress-mariadb" in dump_cmd
    assert "MARIADB_ROOT_PASSWORD" in dump_cmd
    assert "mariadb-dump" in dump_cmd
    assert "--databases wp01 wp02" in dump_cmd
    assert dump_timeout == 180

    stat_cmd, _ = calls[1]
    assert "stat -c %s" in stat_cmd

    tar_cmd, tar_timeout = calls[2]
    assert "--warning=no-file-changed" in tar_cmd
    assert "--exclude='sites/*/wp-content/cache/*'" in tar_cmd
    assert "--exclude='sites/*/wp-content/upgrade-temp-backup/*'" in tar_cmd
    assert "--exclude='@eaDir'" in tar_cmd
    assert "--exclude='*.log'" in tar_cmd
    assert "-C '/volume2/docker/wordpress'" in tar_cmd
    assert "docker-compose.yml .env apache php sites" in tar_cmd
    assert tar_timeout == 240


def test_backup_wordpress_dump_failure_cleans_up():
    """Verify non-zero pipefail MariaDB dump removes dest_db and returns (False, ...)."""
    cmds: list[str] = []

    def fake_run_ssh(cmd: str, timeout: int = 180):
        cmds.append(cmd)
        if "mariadb-dump" in cmd:
            return 2, "", "ERROR 1045: Access denied"
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_wordpress("20261125_034500")

    assert ok is False
    assert "code 2" in msg
    assert any(
        c == "rm -f '/volumeUSB1/usbshare/backups/wordpress/wp_dbs_20261125_034500.sql.gz'"
        for c in cmds
    )
    assert not any("sudo tar" in c for c in cmds)


def test_backup_wordpress_small_dump_fails_and_cleans_up():
    """Verify dump < 1024 bytes triggers rm -f cleanup and returns (False, ...)."""
    cmds: list[str] = []

    def fake_run_ssh(cmd: str, timeout: int = 180):
        cmds.append(cmd)
        if "mariadb-dump" in cmd:
            return 0, "", ""
        if cmd.startswith("stat -c %s"):
            return 0, "512\n", ""
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_wordpress("20261125_034500")

    assert ok is False
    assert "512" in msg
    assert any(
        c == "rm -f '/volumeUSB1/usbshare/backups/wordpress/wp_dbs_20261125_034500.sql.gz'"
        for c in cmds
    )
    assert not any("sudo tar" in c for c in cmds)


def test_backup_wordpress_allows_tar_rc_1_fails_rc_2():
    """Verify GNU tar rc=1 succeeds and rc=2 cleans up dest_tar and returns (False, ...)."""

    def make_fake_ssh(tar_rc: int, cmds: list[str]):
        def fake_run_ssh(cmd: str, timeout: int = 180):
            cmds.append(cmd)
            if "mariadb-dump" in cmd:
                return 0, "", ""
            if cmd.startswith("stat -c %s"):
                return 0, "2048\n", ""
            if "sudo tar" in cmd:
                return tar_rc, "", "file changed" if tar_rc == 1 else "fatal tar error"
            if cmd.startswith("ls -lh"):
                return 0, "wp_dbs 2K wp_files 10M\n", ""
            if cmd.startswith("rm -f"):
                return 0, "", ""
            raise AssertionError(f"Unexpected SSH command: {cmd}")

        return fake_run_ssh

    cmds_rc1: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(1, cmds_rc1)):
        ok1, msg1 = backup_host1.backup_wordpress("20261125_034500")
    assert ok1 is True
    assert "wordpress ->" in msg1

    cmds_rc2: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(2, cmds_rc2)):
        ok2, msg2 = backup_host1.backup_wordpress("20261125_034500")
    assert ok2 is False
    assert "code 2" in msg2
    assert any(
        c == "rm -f '/volumeUSB1/usbshare/backups/wordpress/wp_files_20261125_034500.tar.gz'"
        for c in cmds_rc2
    )


def test_main_aborts_when_usb_not_mounted(capsys):
    """Verify mountpoint -q returning 1 causes SystemExit(1) before any backup runs."""
    ssh_calls: list[tuple[str, int | None]] = []

    def fake_run_ssh(cmd: str, timeout: int = 180):
        ssh_calls.append((cmd, timeout))
        return 1, "", ""

    with (
        patch.object(sys, "argv", ["backup_host1.py"]),
        patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh),
        patch.object(backup_host1, "backup_postgres_arr") as m_arr,
        patch.object(backup_host1, "backup_home_assistant") as m_ha,
        patch.object(backup_host1, "backup_docker_appdata") as m_app,
        patch.object(backup_host1, "backup_wordpress") as m_wp,
        pytest.raises(SystemExit) as exc_info,
    ):
        backup_host1.main()

    assert exc_info.value.code == 1
    assert ssh_calls == [("mountpoint -q /volumeUSB1/usbshare", 15)]
    m_arr.assert_not_called()
    m_ha.assert_not_called()
    m_app.assert_not_called()
    m_wp.assert_not_called()
    captured = capsys.readouterr()
    assert "/volumeUSB1/usbshare" in captured.err


def test_main_creates_and_prunes_wordpress_dir():
    """Verify main() creates/prunes wordpress backup dir and runs backup_wordpress."""
    ssh_cmds: list[str] = []

    def fake_run_ssh(cmd: str, timeout: int = 180):
        ssh_cmds.append(cmd)
        if cmd == "mountpoint -q /volumeUSB1/usbshare":
            return 0, "", ""
        return 0, "100G 20G 80G 20% /volumeUSB1/usbshare\n", ""

    with (
        patch.object(sys, "argv", ["backup_host1.py", "--quiet"]),
        patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh),
        patch.object(backup_host1, "backup_postgres_arr", return_value=(True, "ok")),
        patch.object(backup_host1, "backup_home_assistant", return_value=(True, "ok")),
        patch.object(backup_host1, "backup_docker_appdata", return_value=(True, "ok")),
        patch.object(backup_host1, "backup_wordpress", return_value=(True, "ok")) as m_wp,
        pytest.raises(SystemExit) as exc_info,
    ):
        backup_host1.main()

    assert exc_info.value.code == 0
    m_wp.assert_called_once()
    assert any("mkdir -p /volumeUSB1/usbshare/backups/wordpress" in c for c in ssh_cmds)
    assert any("find /volumeUSB1/usbshare/backups/wordpress" in c for c in ssh_cmds)
