#!/usr/bin/env python3
"""
backup_host1.py — Automated local backup runner for Host 1 stateful services.
Targets the ext4 USB volume at /volumeUSB1/usbshare/backups/.
Backs up:
  - postgres-arr (PostgreSQL pg_dumpall: Sonarr, Radarr, Prowlarr)
  - homeassistant (Volume 2 NVMe HA config, .storage, SQLite/Zigbee DBs, Matter/OTBR/Z-Wave/go2rtc & Homebridge)
  - docker-appdata (docker-compose.yml + critical lightweight configs)
  - wordpress (Volume 2 NVMe MariaDB wp01/wp02 dumps + site files & stack configs)
Enforces a 14-day rolling retention policy.
"""

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
RETENTION_DAYS = 14
HA_NVME_ROOT = "/volume2/docker/homeassistant"
HOMEBRIDGE_NVME_ROOT = "/volume2/docker/homebridge"

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.nas_config import DOCKER_ROOT, resolve_nas_config
    from tools.nas_config import run_ssh as _nas_run_ssh
except ImportError:
    from nas_config import DOCKER_ROOT, resolve_nas_config
    from nas_config import run_ssh as _nas_run_ssh

HOST_1_IP, _, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

def run_ssh(cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    return _nas_run_ssh(HOST_1_IP, cmd, timeout=timeout, user=SSH_USER, port=SSH_PORT, key=SSH_KEY)

def prune_old_backups(dir_path: str, days: int = RETENTION_DAYS) -> int:
    cmd = f"find {dir_path} -type f -mtime +{days} -delete 2>/dev/null"
    code, _, _ = run_ssh(cmd)
    return code

def backup_postgres_arr(ts: str) -> tuple[bool, str]:
    dest = f"/volumeUSB1/usbshare/backups/postgres-arr/arr_{ts}.sql.gz"
    cmd = f"sudo docker exec postgres-arr pg_dumpall -U postgres | gzip > '{dest}'"
    code, _, stderr = run_ssh(cmd, timeout=180)
    if code != 0:
        run_ssh(f"rm -f '{dest}'")
        return False, f"pg_dumpall failed (code {code}): {stderr.strip()}"
    
    _, size_out, _ = run_ssh(f"ls -lh '{dest}' | awk '{{print $5}}'")
    return True, f"postgres-arr -> {dest} ({size_out.strip()})"

def backup_home_assistant(ts: str) -> tuple[bool, str]:
    ha_backup_dir = "/volumeUSB1/usbshare/backups/homeassistant"
    dest_config = f"{ha_backup_dir}/ha_config_{ts}.tar.gz"
    dest_db = f"{ha_backup_dir}/ha_db_{ts}.db"
    dest_zigbee = f"{ha_backup_dir}/zigbee_db_{ts}.db"
    dest_radios = f"{ha_backup_dir}/smarthome_radios_{ts}.tar.gz"

    # 1. Config tarball (.storage, automations, cloud, yaml) from Volume 2 NVMe
    cmd_tar = (
        f"sudo tar --warning=no-file-changed --exclude='.git' --exclude='.cache' --exclude='backups' "
        f"--exclude='*.db*' --exclude='*.log' "
        f"-czf '{dest_config}' -C '{HA_NVME_ROOT}/config' ."
    )
    code, _, stderr = run_ssh(cmd_tar, timeout=120)
    if code not in (0, 1):
        run_ssh(f"rm -f '{dest_config}'")
        return False, f"ha config tar failed (code {code}): {stderr.strip()}"

    # 2. SQLite WAL-safe backup of history database
    cmd_db = f"sudo sqlite3 '{HA_NVME_ROOT}/config/home-assistant_v2.db' \".backup '{dest_db}'\""
    code_db, _, stderr_db = run_ssh(cmd_db, timeout=240)
    if code_db != 0:
        run_ssh(f"rm -f '{dest_db}'")
        return False, f"ha sqlite db backup failed (code {code_db}): {stderr_db.strip()}"

    # 3. SQLite WAL-safe backup of Zigbee coordinator database (if present)
    zigbee_src = f"{HA_NVME_ROOT}/config/zigbee.db"
    cmd_zigbee = (
        f"if sudo test -f '{zigbee_src}'; then "
        f"sudo sqlite3 '{zigbee_src}' \".backup '{dest_zigbee}'\"; "
        f"else echo '__MISSING__'; fi"
    )
    code_zb, stdout_zb, stderr_zb = run_ssh(cmd_zigbee, timeout=120)
    if code_zb != 0:
        run_ssh(f"rm -f '{dest_zigbee}'")
        return False, f"zigbee sqlite db backup failed (code {code_zb}): {stderr_zb.strip()}"
    zigbee_backed_up = "__MISSING__" not in (stdout_zb or "")

    # 4. Smart-home companion & radio configs tarball (Matter, OTBR, Z-Wave, go2rtc, Homebridge)
    nvme_docker_parent = HA_NVME_ROOT.rsplit("/", 1)[0]
    ha_dir_name = HA_NVME_ROOT.rsplit("/", 1)[-1]
    hb_dir_name = HOMEBRIDGE_NVME_ROOT.rsplit("/", 1)[-1]
    cmd_radios = (
        f"sudo tar --warning=no-file-changed --exclude='*.lock' "
        f"--exclude='{hb_dir_name}/node_modules' --exclude='{hb_dir_name}/homebridge.log' --exclude='*.log' "
        f"-czf '{dest_radios}' -C '{nvme_docker_parent}' "
        f"{ha_dir_name}/matter-server {ha_dir_name}/otbr-data {ha_dir_name}/otbr-start.sh "
        f"{ha_dir_name}/zwave-js-ui {ha_dir_name}/go2rtc.yaml {hb_dir_name}"
    )
    code_radios, _, stderr_radios = run_ssh(cmd_radios, timeout=180)
    if code_radios not in (0, 1):
        run_ssh(f"rm -f '{dest_radios}'")
        return False, f"smarthome radios tar failed (code {code_radios}): {stderr_radios.strip()}"

    backed_up = [dest_config, dest_db]
    if zigbee_backed_up:
        backed_up.append(dest_zigbee)
    backed_up.append(dest_radios)
    ls_targets = " ".join(f"'{p}'" for p in backed_up)
    _, size_out, _ = run_ssh(f"ls -lh {ls_targets} | awk '{{print $9, $5}}'")
    summary = " & ".join(backed_up)
    size_suffix = f" ({size_out.strip()})" if size_out.strip() else ""
    return True, f"homeassistant -> {summary}{size_suffix}"

def backup_docker_appdata(ts: str) -> tuple[bool, str]:
    dest_compose = f"/volumeUSB1/usbshare/backups/docker-appdata/docker-compose_{ts}.yml"
    dest_tar = f"/volumeUSB1/usbshare/backups/docker-appdata/appdata_critical_{ts}.tar.gz"

    # 1. Copy root compose stack
    cmd_cp = f"cp '{DOCKER_ROOT}/appdata/docker-compose.yml' '{dest_compose}'"
    code_cp, _, stderr_cp = run_ssh(cmd_cp, timeout=30)
    if code_cp != 0:
        run_ssh(f"rm -f '{dest_compose}'")
        return False, f"docker-compose copy failed (code {code_cp}): {stderr_cp.strip()}"

    # 2. Checkpoint SQLite WAL databases before archiving (Dockhand, Seerr, Tautulli, Maintainerr)
    cmd_ckpt = (
        f"for db in "
        f"'{DOCKER_ROOT}/appdata/dockhand/db/dockhand.db' "
        f"'{DOCKER_ROOT}/appdata/seerr/db/db.sqlite3' "
        f"'{DOCKER_ROOT}/appdata/tautulli/tautulli.db' "
        f"'{DOCKER_ROOT}/appdata/maintainerr/maintainerr.sqlite'; do "
        f"[ -f \"$db\" ] && sudo sqlite3 \"$db\" 'PRAGMA wal_checkpoint(PASSIVE);' >/dev/null 2>&1 || true; "
        f"done"
    )
    run_ssh(cmd_ckpt, timeout=30)

    # 3. Tar critical lightweight appdata configs (maintainerr, seerr, tautulli, dockhand)
    # Exclude internal backups, cache, transcode, and logs to avoid disk stall;
    # use --warning=no-file-changed and allow GNU tar exit code 1 (live WAL/file mutation during read)
    cmd_tar = (
        f"sudo tar --warning=no-file-changed --exclude='backups' --exclude='cache' --exclude='Transcode' "
        f"--exclude='logs' --exclude='*.log' "
        f"-czf '{dest_tar}' -C '{DOCKER_ROOT}/appdata' "
        f"maintainerr seerr tautulli dockhand"
    )
    code, _, stderr = run_ssh(cmd_tar, timeout=180)
    if code not in (0, 1):
        run_ssh(f"rm -f '{dest_tar}'")
        return False, f"appdata tar failed (code {code}): {stderr.strip()}"

    _, _, _ = run_ssh(f"ls -lh '{dest_compose}' '{dest_tar}' | awk '{{print $9, $5}}'")
    return True, f"docker-appdata -> {dest_compose} & {dest_tar}"


def backup_wordpress(ts: str) -> tuple[bool, str]:
    dest_db = f"/volumeUSB1/usbshare/backups/wordpress/wp_dbs_{ts}.sql.gz"
    dest_tar = f"/volumeUSB1/usbshare/backups/wordpress/wp_files_{ts}.tar.gz"
    wp_root = "/volume2/docker/wordpress"

    # 1. Dump wp01 & wp02 from MariaDB with pipefail so container errors aren't masked by gzip
    cmd_db = (
        "bash -o pipefail -c "
        "\"sudo docker exec wordpress-mariadb sh -c "
        "'MYSQL_PWD=\\\"\\$MARIADB_ROOT_PASSWORD\\\" mariadb-dump "
        "--single-transaction --quick --routines --triggers --databases wp01 wp02' "
        f"| gzip > '{dest_db}'\""
    )
    code_db, _, stderr_db = run_ssh(cmd_db, timeout=180)
    if code_db != 0:
        run_ssh(f"rm -f '{dest_db}'")
        return False, f"wordpress mariadb-dump failed (code {code_db}): {stderr_db.strip()}"

    # 2. Verify compressed dump archive is non-trivial (>= 1024 bytes)
    code_stat, size_bytes_out, _ = run_ssh(f"stat -c %s '{dest_db}' 2>/dev/null || echo 0")
    try:
        dump_bytes = int(size_bytes_out.strip().splitlines()[-1]) if size_bytes_out.strip() else 0
    except ValueError:
        dump_bytes = 0
    if code_stat != 0 or dump_bytes < 1024:
        run_ssh(f"rm -f '{dest_db}'")
        return False, f"wordpress mariadb-dump archive too small ({dump_bytes} bytes)"

    # 3. Archive stack configs & site files, excluding disposable caches and logs
    cmd_tar = (
        f"sudo tar --warning=no-file-changed "
        f"--exclude='sites/*/wp-content/cache/*' "
        f"--exclude='sites/*/wp-content/upgrade-temp-backup/*' "
        f"--exclude='@eaDir' --exclude='*.log' "
        f"-czf '{dest_tar}' -C '{wp_root}' docker-compose.yml .env apache php sites"
    )
    code_tar, _, stderr_tar = run_ssh(cmd_tar, timeout=240)
    if code_tar not in (0, 1):
        run_ssh(f"rm -f '{dest_tar}'")
        return False, f"wordpress tar failed (code {code_tar}): {stderr_tar.strip()}"

    _, size_out, _ = run_ssh(f"ls -lh '{dest_db}' '{dest_tar}' | awk '{{print $9, $5}}'")
    return True, f"wordpress -> {dest_db} & {dest_tar} ({size_out.strip()})"


def main():
    quiet = "--quiet" in sys.argv or "-q" in sys.argv

    mount_code, _, mount_err = run_ssh("grep -q ' /volumeUSB1/usbshare ' /proc/mounts", timeout=15)
    if mount_code != 0:
        err_detail = f" ({mount_err.strip()})" if mount_err.strip() else ""
        print(
            f"❌ USB backup target /volumeUSB1/usbshare is not mounted (code {mount_code}){err_detail}; "
            f"aborting to protect rootfs.",
            file=sys.stderr,
        )
        sys.exit(1)

    now = datetime.now(PT)
    ts = now.strftime("%Y%m%d_%H%M%S")
    date_human = now.strftime("%Y-%m-%d %I:%M %p PT")
    if not quiet:
        print(f"[{date_human}] Starting Host 1 Local Backup to /volumeUSB1/usbshare/...")

    dirs = ["postgres-arr", "homeassistant", "docker-appdata", "wordpress"]
    for d in dirs:
        run_ssh(f"sudo mkdir -p /volumeUSB1/usbshare/backups/{d} && sudo chown -R {SSH_USER}:users /volumeUSB1/usbshare/backups/{d}")

    # Remove temporary test files
    run_ssh("rm -f /volumeUSB1/usbshare/backups/postgres-arr/test_arr.sql.gz /volumeUSB1/usbshare/backups/homeassistant/ha_config_test.tar.gz /volumeUSB1/usbshare/backups/docker-appdata/docker-compose_test.yml /volumeUSB1/usbshare/backups/docker-appdata/appdata_critical_test.tar.gz")

    results = {}

    if not quiet:
        print("• Backing up Postgres-Arr (Sonarr/Radarr/Prowlarr)...")
    ok, msg = backup_postgres_arr(ts)
    results["postgres_arr"] = (ok, msg)
    if not quiet:
        print(f"  {'✅' if ok else '❌'} {msg}")

    if not quiet:
        print("• Backing up Home Assistant & Smart Home stack (Volume 2 NVMe)...")
    ok, msg = backup_home_assistant(ts)
    results["home_assistant"] = (ok, msg)
    if not quiet:
        print(f"  {'✅' if ok else '❌'} {msg}")

    if not quiet:
        print("• Backing up Docker Compose & Critical AppData...")
    ok, msg = backup_docker_appdata(ts)
    results["docker_appdata"] = (ok, msg)
    if not quiet:
        print(f"  {'✅' if ok else '❌'} {msg}")

    if not quiet:
        print("• Backing up WordPress (Volume 2 NVMe MariaDB + site files)...")
    ok, msg = backup_wordpress(ts)
    results["wordpress"] = (ok, msg)
    if not quiet:
        print(f"  {'✅' if ok else '❌'} {msg}")

    if not quiet:
        print(f"• Pruning backups older than {RETENTION_DAYS} days...")
    for d in dirs:
        prune_old_backups(f"/volumeUSB1/usbshare/backups/{d}")
    if not quiet:
        print("  ✅ Retention policy enforced.")

    # Fix ownership
    run_ssh(f"sudo chown -R {SSH_USER}:users /volumeUSB1/usbshare/backups")

    _, df_out, _ = run_ssh("df -h /volumeUSB1/usbshare | tail -n 1")
    if not quiet:
        print(f"• Target Volume Status: {df_out.strip()}")

    all_ok = all(ok for ok, _ in results.values())
    if all_ok:
        if not quiet:
            print("🎉 Host 1 Backup completed successfully!")
        sys.exit(0)
    else:
        if not quiet:
            print("⚠️ Host 1 Backup completed with errors.")
        else:
            for k, (ok_val, m) in results.items():
                if not ok_val:
                    print(f"❌ {k}: {m}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
