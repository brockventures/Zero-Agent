"""Unit tests for tools/backup_host1.py WordPress, Smart Home NVMe backup & USB mountpoint guard."""

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
    """Verify /proc/mounts check returning 1 causes SystemExit(1) before any backup runs."""
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
        patch.object(backup_host1, "backup_plex_artwork") as m_plex,
        pytest.raises(SystemExit) as exc_info,
    ):
        backup_host1.main()

    assert exc_info.value.code == 1
    assert ssh_calls == [("grep -q ' /volumeUSB1/usbshare ' /proc/mounts", 15)]
    m_arr.assert_not_called()
    m_ha.assert_not_called()
    m_app.assert_not_called()
    m_wp.assert_not_called()
    m_plex.assert_not_called()
    captured = capsys.readouterr()
    assert "/volumeUSB1/usbshare" in captured.err


def test_main_creates_and_prunes_wordpress_dir():
    """Verify main() creates/prunes wordpress and plex backup dirs and runs both backups."""
    ssh_cmds: list[str] = []

    def fake_run_ssh(cmd: str, timeout: int = 180):
        ssh_cmds.append(cmd)
        if cmd == "grep -q ' /volumeUSB1/usbshare ' /proc/mounts":
            return 0, "", ""
        return 0, "100G 20G 80G 20% /volumeUSB1/usbshare\n", ""

    with (
        patch.object(sys, "argv", ["backup_host1.py", "--quiet"]),
        patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh),
        patch.object(backup_host1, "backup_postgres_arr", return_value=(True, "ok")),
        patch.object(backup_host1, "backup_home_assistant", return_value=(True, "ok")),
        patch.object(backup_host1, "backup_docker_appdata", return_value=(True, "ok")),
        patch.object(backup_host1, "backup_wordpress", return_value=(True, "ok")) as m_wp,
        patch.object(backup_host1, "backup_plex_artwork", return_value=(True, "ok")) as m_plex,
        pytest.raises(SystemExit) as exc_info,
    ):
        backup_host1.main()

    assert exc_info.value.code == 0
    m_wp.assert_called_once()
    m_plex.assert_called_once()
    assert any("mkdir -p /volumeUSB1/usbshare/backups/wordpress" in c for c in ssh_cmds)
    assert any("find /volumeUSB1/usbshare/backups/wordpress" in c for c in ssh_cmds)
    assert any("mkdir -p /volumeUSB1/usbshare/backups/plex" in c for c in ssh_cmds)
    assert any("find /volumeUSB1/usbshare/backups/plex" in c for c in ssh_cmds)


def test_backup_home_assistant_success():
    """Verify NVMe HA config tar, HA/Zigbee SQLite backups, and radios/Homebridge tar."""
    calls: list[tuple[str, int | None]] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        calls.append((cmd, timeout))
        if f"ha_config_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
            return 0, "", ""
        if f"ha_db_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 0, "", ""
        if "zigbee.db" in cmd and "sudo sqlite3" in cmd:
            return 0, "", ""
        if f"smarthome_radios_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
            return 0, "", ""
        if cmd.startswith("ls -lh"):
            return 0, "ha_config 15M ha_db 120M zigbee_db 1.2M smarthome_radios 45M\n", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_home_assistant(ts)

    assert ok is True
    assert f"/volumeUSB1/usbshare/backups/homeassistant/ha_config_{ts}.tar.gz" in msg
    assert f"/volumeUSB1/usbshare/backups/homeassistant/ha_db_{ts}.db" in msg
    assert f"/volumeUSB1/usbshare/backups/homeassistant/zigbee_db_{ts}.db" in msg
    assert f"/volumeUSB1/usbshare/backups/homeassistant/smarthome_radios_{ts}.tar.gz" in msg
    assert "45M" in msg

    cfg_cmd, cfg_timeout = calls[0]
    assert "--warning=no-file-changed" in cfg_cmd
    assert "--exclude='.git'" in cfg_cmd
    assert "--exclude='.cache'" in cfg_cmd
    assert "--exclude='backups'" in cfg_cmd
    assert "--exclude='*.db*'" in cfg_cmd
    assert "--exclude='*.log'" in cfg_cmd
    assert "-C '/volume2/docker/homeassistant/config' ." in cfg_cmd
    assert cfg_timeout == 120

    db_cmd, db_timeout = calls[1]
    assert "sudo sqlite3 '/volume2/docker/homeassistant/config/home-assistant_v2.db'" in db_cmd
    assert f".backup '/volumeUSB1/usbshare/backups/homeassistant/ha_db_{ts}.db'" in db_cmd
    assert db_timeout == 240

    zb_cmd, zb_timeout = calls[2]
    assert "sudo test -f '/volume2/docker/homeassistant/config/zigbee.db'" in zb_cmd
    assert f".backup '/volumeUSB1/usbshare/backups/homeassistant/zigbee_db_{ts}.db'" in zb_cmd
    assert zb_timeout == 120

    radios_cmd, radios_timeout = calls[3]
    assert "--warning=no-file-changed" in radios_cmd
    assert "--exclude='*.lock'" in radios_cmd
    assert "--exclude='homebridge/node_modules'" in radios_cmd
    assert "--exclude='homebridge/homebridge.log'" in radios_cmd
    assert "--exclude='*.log'" in radios_cmd
    assert "-C '/volume2/docker'" in radios_cmd
    for member in (
        "homeassistant/matter-server",
        "homeassistant/otbr-data",
        "homeassistant/otbr-start.sh",
        "homeassistant/zwave-js-ui",
        "homeassistant/go2rtc.yaml",
        "homebridge",
    ):
        assert member in radios_cmd
    assert radios_timeout == 180

    ls_cmd, _ = calls[4]
    assert f"zigbee_db_{ts}.db" in ls_cmd


def test_backup_home_assistant_skips_missing_zigbee_db():
    """Verify missing zigbee.db is skipped cleanly without error or ls entry."""
    calls: list[str] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        calls.append(cmd)
        if f"ha_config_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
            return 0, "", ""
        if f"ha_db_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 0, "", ""
        if "zigbee.db" in cmd:
            return 0, "__MISSING__\n", ""
        if f"smarthome_radios_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
            return 0, "", ""
        if cmd.startswith("ls -lh"):
            return 0, "ha_config 15M ha_db 120M smarthome_radios 45M\n", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_home_assistant(ts)

    assert ok is True
    assert f"zigbee_db_{ts}.db" not in msg
    assert f"smarthome_radios_{ts}.tar.gz" in msg
    ls_cmd = next(c for c in calls if c.startswith("ls -lh"))
    assert f"zigbee_db_{ts}.db" not in ls_cmd


def test_backup_home_assistant_allows_tar_rc_1_fails_rc_2():
    """Verify GNU tar rc=1 succeeds for config/radios tars and rc=2 cleans up partial tar."""
    ts = "20261125_034500"

    def make_fake_ssh(cfg_rc: int, radios_rc: int, cmds: list[str]):
        def fake_run_ssh(cmd: str, timeout: int = 180):
            cmds.append(cmd)
            if f"ha_config_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
                return cfg_rc, "", "file changed" if cfg_rc == 1 else "fatal config tar error"
            if f"ha_db_{ts}.db" in cmd and "sudo sqlite3" in cmd:
                return 0, "", ""
            if "zigbee.db" in cmd:
                return 0, "", ""
            if f"smarthome_radios_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
                return radios_rc, "", "file changed" if radios_rc == 1 else "fatal radios tar error"
            if cmd.startswith("ls -lh"):
                return 0, "ok 10M\n", ""
            if cmd.startswith("rm -f"):
                return 0, "", ""
            raise AssertionError(f"Unexpected SSH command: {cmd}")

        return fake_run_ssh

    cmds_rc1: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(1, 1, cmds_rc1)):
        ok1, msg1 = backup_host1.backup_home_assistant(ts)
    assert ok1 is True
    assert "homeassistant ->" in msg1

    cmds_cfg_rc2: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(2, 0, cmds_cfg_rc2)):
        ok_cfg2, msg_cfg2 = backup_host1.backup_home_assistant(ts)
    assert ok_cfg2 is False
    assert "code 2" in msg_cfg2
    assert f"rm -f '/volumeUSB1/usbshare/backups/homeassistant/ha_config_{ts}.tar.gz'" in cmds_cfg_rc2
    assert not any("sudo sqlite3" in c for c in cmds_cfg_rc2)

    cmds_radios_rc2: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(0, 2, cmds_radios_rc2)):
        ok_rad2, msg_rad2 = backup_host1.backup_home_assistant(ts)
    assert ok_rad2 is False
    assert "code 2" in msg_rad2
    assert f"rm -f '/volumeUSB1/usbshare/backups/homeassistant/smarthome_radios_{ts}.tar.gz'" in cmds_radios_rc2


def test_backup_home_assistant_db_failure_cleans_up():
    """Verify HA DB and Zigbee DB backup failures remove partial files and return (False, ...)."""
    ts = "20261125_034500"

    cmds_ha_fail: list[str] = []

    def fake_ha_db_fail(cmd: str, timeout: int = 180):
        cmds_ha_fail.append(cmd)
        if f"ha_config_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
            return 0, "", ""
        if f"ha_db_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 5, "", "database is locked"
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_ha_db_fail):
        ok_ha, msg_ha = backup_host1.backup_home_assistant(ts)
    assert ok_ha is False
    assert "code 5" in msg_ha
    assert f"rm -f '/volumeUSB1/usbshare/backups/homeassistant/ha_db_{ts}.db'" in cmds_ha_fail
    assert not any("smarthome_radios" in c for c in cmds_ha_fail)

    cmds_zb_fail: list[str] = []

    def fake_zb_fail(cmd: str, timeout: int = 180):
        cmds_zb_fail.append(cmd)
        if f"ha_config_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
            return 0, "", ""
        if f"ha_db_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 0, "", ""
        if "zigbee.db" in cmd:
            return 1, "", "Error: disk I/O error"
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_zb_fail):
        ok_zb, msg_zb = backup_host1.backup_home_assistant(ts)
    assert ok_zb is False
    assert "code 1" in msg_zb
    assert f"rm -f '/volumeUSB1/usbshare/backups/homeassistant/zigbee_db_{ts}.db'" in cmds_zb_fail
    assert not any("smarthome_radios" in c for c in cmds_zb_fail)


def test_backup_postgres_arr_success():
    """Verify pipefail pg_dumpall, >= 1024B size verification, and return message."""
    calls: list[tuple[str, int | None]] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        calls.append((cmd, timeout))
        if "pg_dumpall" in cmd:
            return 0, "", ""
        if cmd.startswith("stat -c %s"):
            return 0, "4096\n", ""
        if cmd.startswith("ls -lh"):
            return 0, "4.0K\n", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_postgres_arr(ts)

    assert ok is True
    assert msg == f"postgres-arr -> /volumeUSB1/usbshare/backups/postgres-arr/arr_{ts}.sql.gz (4.0K)"

    dump_cmd, dump_timeout = calls[0]
    assert "bash -o pipefail -c" in dump_cmd
    assert "sudo docker exec postgres-arr pg_dumpall -U postgres | gzip >" in dump_cmd
    assert f"/volumeUSB1/usbshare/backups/postgres-arr/arr_{ts}.sql.gz" in dump_cmd
    assert dump_timeout == 180

    stat_cmd, _ = calls[1]
    assert f"stat -c %s '/volumeUSB1/usbshare/backups/postgres-arr/arr_{ts}.sql.gz'" in stat_cmd


def test_backup_postgres_arr_dump_failure_cleans_up():
    """Verify non-zero pipefail pg_dumpall removes dest and returns (False, ...)."""
    cmds: list[str] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        cmds.append(cmd)
        if "pg_dumpall" in cmd:
            return 2, "", "pg_dumpall: error: connection failed"
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_postgres_arr(ts)

    assert ok is False
    assert "code 2" in msg
    assert f"rm -f '/volumeUSB1/usbshare/backups/postgres-arr/arr_{ts}.sql.gz'" in cmds
    assert not any(c.startswith("stat -c %s") for c in cmds)


def test_backup_postgres_arr_small_dump_fails_and_cleans_up():
    """Verify dump < 1024 bytes triggers rm -f cleanup and returns (False, ...)."""
    cmds: list[str] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        cmds.append(cmd)
        if "pg_dumpall" in cmd:
            return 0, "", ""
        if cmd.startswith("stat -c %s"):
            return 0, "512\n", ""
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_postgres_arr(ts)

    assert ok is False
    assert "512" in msg
    assert f"rm -f '/volumeUSB1/usbshare/backups/postgres-arr/arr_{ts}.sql.gz'" in cmds


def test_backup_docker_appdata_success():
    """Verify compose copy, Wave 2 + Wave 4 NVMe SQLite checkpoints, and multi--C tar archive."""
    calls: list[tuple[str, int | None]] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        calls.append((cmd, timeout))
        if cmd.startswith("cp "):
            return 0, "", ""
        if "PRAGMA wal_checkpoint" in cmd:
            return 0, "", ""
        if "sudo tar" in cmd:
            return 0, "", ""
        if cmd.startswith("ls -lh"):
            return 0, "docker-compose 4K appdata_critical 18M\n", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_docker_appdata(ts)

    assert ok is True
    assert backup_host1.HOMEPAGE_NVME_ROOT == "/volume2/docker/homepage"
    dest_compose = f"/volumeUSB1/usbshare/backups/docker-appdata/docker-compose_{ts}.yml"
    dest_tar = f"/volumeUSB1/usbshare/backups/docker-appdata/appdata_critical_{ts}.tar.gz"
    assert msg == f"docker-appdata -> {dest_compose} & {dest_tar}"

    cp_cmd, cp_timeout = calls[0]
    assert cp_cmd == f"cp '{backup_host1.DOCKER_ROOT}/appdata/docker-compose.yml' '{dest_compose}'"
    assert cp_timeout == 30

    ckpt_cmd, ckpt_timeout = calls[1]
    assert ckpt_timeout == 30
    assert f"'{backup_host1.DOCKER_ROOT}/appdata/dockhand/db/dockhand.db'" not in ckpt_cmd
    for db_path in (
        "'/volume2/docker/appdata/dockhand/db/dockhand.db'",
        "'/volume2/docker/appdata/seerr/db/db.sqlite3'",
        "'/volume2/docker/appdata/tautulli/tautulli.db'",
        "'/volume2/docker/appdata/maintainerr/maintainerr.sqlite'",
        "'/volume2/docker/appdata/bazarr/db/bazarr.db'",
        "'/volume2/docker/appdata/sabnzbd/admin/history1.db'",
        "'/volume2/docker/appdata/calibre-web-automated/app.db'",
        "'/volume2/docker/appdata/calibre-web-automated/cwa.db'",
        "'/volume2/docker/appdata/calibre-web-automated/gdrive.db'",
        "'/volume2/docker/appdata/shelfmark/users.db'",
    ):
        assert db_path in ckpt_cmd

    tar_cmd, tar_timeout = calls[2]
    assert tar_timeout == 180
    assert "--warning=no-file-changed" in tar_cmd
    for excl in (
        "--exclude='backups'",
        "--exclude='backup'",
        "--exclude='Backups'",
        "--exclude='cache'",
        "--exclude='.cache'",
        "--exclude='MediaCover'",
        "--exclude='Transcode'",
        "--exclude='logs'",
        "--exclude='log'",
        "--exclude='*.log'",
    ):
        assert excl in tar_cmd
    assert f"-C '{backup_host1.DOCKER_ROOT}/appdata' dockhand" not in tar_cmd
    expected_members = (
        "-C '/volume2/docker/appdata' "
        "maintainerr seerr tautulli bazarr dockhand calibre-web-automated shelfmark "
        "sabnzbd/sabnzbd.ini sabnzbd/admin/history1.db glances/conf/glances.conf "
        "sonarr/config.xml radarr/config.xml prowlarr/config.xml "
        "-C '/volume2/docker/homepage' config"
    )
    assert expected_members in tar_cmd


def test_backup_docker_appdata_allows_tar_rc_1_fails_rc_2():
    """Verify GNU tar rc=1 succeeds and rc=2 removes dest_tar and returns (False, ...)."""
    ts = "20261125_034500"

    def make_fake_ssh(tar_rc: int, cmds: list[str]):
        def fake_run_ssh(cmd: str, timeout: int = 180):
            cmds.append(cmd)
            if cmd.startswith("cp "):
                return 0, "", ""
            if "PRAGMA wal_checkpoint" in cmd:
                return 0, "", ""
            if "sudo tar" in cmd:
                return tar_rc, "", "file changed" if tar_rc == 1 else "fatal tar error"
            if cmd.startswith("ls -lh"):
                return 0, "ok 10M\n", ""
            if cmd.startswith("rm -f"):
                return 0, "", ""
            raise AssertionError(f"Unexpected SSH command: {cmd}")

        return fake_run_ssh

    cmds_rc1: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(1, cmds_rc1)):
        ok1, msg1 = backup_host1.backup_docker_appdata(ts)
    assert ok1 is True
    assert "docker-appdata ->" in msg1

    cmds_rc2: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(2, cmds_rc2)):
        ok2, msg2 = backup_host1.backup_docker_appdata(ts)
    assert ok2 is False
    assert "code 2" in msg2
    assert f"rm -f '/volumeUSB1/usbshare/backups/docker-appdata/appdata_critical_{ts}.tar.gz'" in cmds_rc2


def test_backup_docker_appdata_compose_copy_failure_cleans_up():
    """Verify failed docker-compose.yml copy removes dest_compose and aborts before checkpoint/tar."""
    cmds: list[str] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        cmds.append(cmd)
        if cmd.startswith("cp "):
            return 1, "", "cp: cannot stat"
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_docker_appdata(ts)

    assert ok is False
    assert "code 1" in msg
    assert f"rm -f '/volumeUSB1/usbshare/backups/docker-appdata/docker-compose_{ts}.yml'" in cmds
    assert not any("PRAGMA wal_checkpoint" in c for c in cmds)
    assert not any("sudo tar" in c for c in cmds)


def test_backup_plex_artwork_success():
    """Verify NVMe Plex library/blobs SQLite .backup and Kometa/Posterizarr configs tarball."""
    calls: list[tuple[str, int | None]] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        calls.append((cmd, timeout))
        if f"plex_library_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 0, "", ""
        if f"plex_blobs_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 0, "", ""
        if f"plex_artwork_configs_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
            return 0, "", ""
        if cmd.startswith("ls -lh"):
            return 0, "plex_library 250M plex_blobs 80M plex_artwork_configs 12M\n", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_plex_artwork(ts)

    assert ok is True
    assert f"/volumeUSB1/usbshare/backups/plex/plex_library_{ts}.db" in msg
    assert f"/volumeUSB1/usbshare/backups/plex/plex_blobs_{ts}.db" in msg
    assert f"/volumeUSB1/usbshare/backups/plex/plex_artwork_configs_{ts}.tar.gz" in msg
    assert "250M" in msg

    lib_cmd, lib_timeout = calls[0]
    assert (
        "sudo sqlite3 '/volume2/docker/appdata/plex/Plug-in Support/Databases/com.plexapp.plugins.library.db'"
        in lib_cmd
    )
    assert f".backup '/volumeUSB1/usbshare/backups/plex/plex_library_{ts}.db'" in lib_cmd
    assert lib_timeout == 300

    blobs_cmd, blobs_timeout = calls[1]
    assert (
        "sudo test -f '/volume2/docker/appdata/plex/Plug-in Support/Databases/com.plexapp.plugins.library.blobs.db'"
        in blobs_cmd
    )
    assert f".backup '/volumeUSB1/usbshare/backups/plex/plex_blobs_{ts}.db'" in blobs_cmd
    assert blobs_timeout == 300

    tar_cmd, tar_timeout = calls[2]
    assert "--ignore-failed-read" in tar_cmd
    assert "--warning=no-file-changed" in tar_cmd
    for excl in ("--exclude='*.log'", "--exclude='Logs'", "--exclude='Cache'", "--exclude='temp'"):
        assert excl in tar_cmd
    assert "-C '/volume2/docker/appdata'" in tar_cmd
    for member in (
        "plex/Preferences.xml",
        "kometa/radarr_arrapi_patch.py",
        "kometa/config/config.yml",
        "kometa/config/collection_movies.yml",
        "kometa/config/overlay_movies.yml",
        "kometa/config/overlay_tv.yml",
        "posterizarr/config.json",
        "posterizarr/scheduler.json",
        "posterizarr/config",
        "posterizarr/database",
    ):
        assert member in tar_cmd
    assert tar_timeout == 180

    ls_cmd, _ = calls[3]
    assert f"plex_blobs_{ts}.db" in ls_cmd


def test_backup_plex_artwork_skips_missing_blobs_db():
    """Verify missing com.plexapp.plugins.library.blobs.db is skipped cleanly without ls entry."""
    calls: list[str] = []
    ts = "20261125_034500"

    def fake_run_ssh(cmd: str, timeout: int = 180):
        calls.append(cmd)
        if f"plex_library_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 0, "", ""
        if "library.blobs.db" in cmd:
            return 0, "__MISSING__\n", ""
        if f"plex_artwork_configs_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
            return 0, "", ""
        if cmd.startswith("ls -lh"):
            return 0, "plex_library 250M plex_artwork_configs 12M\n", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_run_ssh):
        ok, msg = backup_host1.backup_plex_artwork(ts)

    assert ok is True
    assert f"plex_library_{ts}.db" in msg
    assert f"plex_blobs_{ts}.db" not in msg
    assert f"plex_artwork_configs_{ts}.tar.gz" in msg
    ls_cmd = next(c for c in calls if c.startswith("ls -lh"))
    assert f"plex_blobs_{ts}.db" not in ls_cmd


def test_backup_plex_artwork_db_failure_cleans_up():
    """Verify library DB and blobs DB failures remove partial files and return (False, ...)."""
    ts = "20261125_034500"

    cmds_lib_fail: list[str] = []

    def fake_lib_fail(cmd: str, timeout: int = 180):
        cmds_lib_fail.append(cmd)
        if f"plex_library_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 5, "", "database is locked"
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_lib_fail):
        ok_lib, msg_lib = backup_host1.backup_plex_artwork(ts)

    assert ok_lib is False
    assert "code 5" in msg_lib
    assert f"rm -f '/volumeUSB1/usbshare/backups/plex/plex_library_{ts}.db'" in cmds_lib_fail
    assert not any("library.blobs.db" in c for c in cmds_lib_fail)
    assert not any("sudo tar" in c for c in cmds_lib_fail)

    cmds_blobs_fail: list[str] = []

    def fake_blobs_fail(cmd: str, timeout: int = 180):
        cmds_blobs_fail.append(cmd)
        if f"plex_library_{ts}.db" in cmd and "sudo sqlite3" in cmd:
            return 0, "", ""
        if "library.blobs.db" in cmd:
            return 1, "", "disk I/O error"
        if cmd.startswith("rm -f"):
            return 0, "", ""
        raise AssertionError(f"Unexpected SSH command: {cmd}")

    with patch.object(backup_host1, "run_ssh", side_effect=fake_blobs_fail):
        ok_blobs, msg_blobs = backup_host1.backup_plex_artwork(ts)

    assert ok_blobs is False
    assert "code 1" in msg_blobs
    assert f"rm -f '/volumeUSB1/usbshare/backups/plex/plex_blobs_{ts}.db'" in cmds_blobs_fail
    assert not any("sudo tar" in c for c in cmds_blobs_fail)


def test_backup_plex_artwork_allows_tar_rc_1_fails_rc_2():
    """Verify GNU tar rc=1 succeeds and rc=2 cleans up dest_tar and returns (False, ...)."""
    ts = "20261125_034500"

    def make_fake_ssh(tar_rc: int, cmds: list[str]):
        def fake_run_ssh(cmd: str, timeout: int = 180):
            cmds.append(cmd)
            if f"plex_library_{ts}.db" in cmd and "sudo sqlite3" in cmd:
                return 0, "", ""
            if "library.blobs.db" in cmd:
                return 0, "", ""
            if f"plex_artwork_configs_{ts}.tar.gz" in cmd and "sudo tar" in cmd:
                return tar_rc, "", "file changed" if tar_rc == 1 else "fatal tar error"
            if cmd.startswith("ls -lh"):
                return 0, "ok 10M\n", ""
            if cmd.startswith("rm -f"):
                return 0, "", ""
            raise AssertionError(f"Unexpected SSH command: {cmd}")

        return fake_run_ssh

    cmds_rc1: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(1, cmds_rc1)):
        ok1, msg1 = backup_host1.backup_plex_artwork(ts)
    assert ok1 is True
    assert "plex ->" in msg1

    cmds_rc2: list[str] = []
    with patch.object(backup_host1, "run_ssh", side_effect=make_fake_ssh(2, cmds_rc2)):
        ok2, msg2 = backup_host1.backup_plex_artwork(ts)
    assert ok2 is False
    assert "code 2" in msg2
    assert f"rm -f '/volumeUSB1/usbshare/backups/plex/plex_artwork_configs_{ts}.tar.gz'" in cmds_rc2
