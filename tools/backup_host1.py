#!/usr/bin/env python3
"""
backup_host1.py — Automated local backup runner for Host 1 stateful services.
Targets the ext4 USB volume at /volumeUSB1/usbshare/backups/.
Backs up:
  - postgres-arr (PostgreSQL pg_dumpall: Sonarr, Radarr, Prowlarr)
  - homeassistant (HA config, .storage, automations, credentials + SQLite DB)
  - docker-appdata (docker-compose.yml + critical lightweight configs)
Enforces a 14-day rolling retention policy.
"""

import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
RETENTION_DAYS = 14

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_PORT = os.environ.get("NAS_SSH_PORT", os.environ.get("NAS_SSH_PORT", "22"))
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")
HOST_1_IP = os.environ.get("NAS_HOST_1_IP", os.environ.get("NAS_HOST_1_IP", "127.0.0.1"))

def run_ssh(cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    ssh_cmd = [
        "ssh", "-i", str(SSH_KEY),
        "-p", str(SSH_PORT),
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=5",
        f"{SSH_USER}@{HOST_1_IP}",
        cmd
    ]
    try:
        res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s: {cmd[:60]}"
    except Exception as e:
        return -1, "", f"SSH execution exception: {e}"

def prune_old_backups(dir_path: str, days: int = RETENTION_DAYS) -> int:
    cmd = f"find {dir_path} -type f -mtime +{days} -delete 2>/dev/null"
    code, _, _ = run_ssh(cmd)
    return code

def backup_postgres_arr(ts: str) -> tuple[bool, str]:
    dest = f"/volumeUSB1/usbshare/backups/postgres-arr/arr_{ts}.sql.gz"
    cmd = f"sudo docker exec postgres-arr pg_dumpall -U postgres | gzip > '{dest}'"
    code, stdout, stderr = run_ssh(cmd, timeout=180)
    if code != 0:
        return False, f"pg_dumpall failed (code {code}): {stderr.strip()}"
    
    code, size_out, _ = run_ssh(f"ls -lh '{dest}' | awk '{{print $5}}'")
    return True, f"postgres-arr -> {dest} ({size_out.strip()})"

def backup_home_assistant(ts: str) -> tuple[bool, str]:
    dest_config = f"/volumeUSB1/usbshare/backups/homeassistant/ha_config_{ts}.tar.gz"
    dest_db = f"/volumeUSB1/usbshare/backups/homeassistant/ha_db_{ts}.db"

    # 1. Config tarball (.storage, automations, cloud, yaml)
    cmd_tar = (
        f"sudo tar --exclude='.git' --exclude='.cache' --exclude='backups' "
        f"--exclude='*.db*' --exclude='*.log' "
        f"-czf '{dest_config}' -C /docker/homeassistant/config . 2>/dev/null"
    )
    code, _, stderr = run_ssh(cmd_tar, timeout=120)
    if code != 0:
        return False, f"ha config tar failed (code {code}): {stderr.strip()}"

    # 2. SQLite WAL-safe backup of history database
    cmd_db = f"sudo sqlite3 /docker/homeassistant/config/home-assistant_v2.db \".backup '{dest_db}'\""
    code_db, _, stderr_db = run_ssh(cmd_db, timeout=180)
    if code_db != 0:
        return False, f"ha sqlite db backup failed (code {code_db}): {stderr_db.strip()}"

    code, size_out, _ = run_ssh(f"ls -lh '{dest_config}' '{dest_db}' | awk '{{print $9, $5}}'")
    return True, f"homeassistant -> {dest_config} & {dest_db}"

def backup_docker_appdata(ts: str) -> tuple[bool, str]:
    dest_compose = f"/volumeUSB1/usbshare/backups/docker-appdata/docker-compose_{ts}.yml"
    dest_tar = f"/volumeUSB1/usbshare/backups/docker-appdata/appdata_critical_{ts}.tar.gz"

    # 1. Copy root compose stack
    cmd_cp = f"cp /docker/appdata/docker-compose.yml '{dest_compose}'"
    code_cp, _, stderr_cp = run_ssh(cmd_cp, timeout=30)
    if code_cp != 0:
        return False, f"docker-compose copy failed (code {code_cp}): {stderr_cp.strip()}"

    # 2. Tar critical lightweight appdata configs (maintainerr, seerr, tautulli, dockhand)
    # Exclude internal backups, cache, transcode, and logs to avoid disk stall
    cmd_tar = (
        f"sudo tar --exclude='backups' --exclude='cache' --exclude='Transcode' "
        f"--exclude='logs' --exclude='*.log' "
        f"-czf '{dest_tar}' -C /docker/appdata "
        f"maintainerr seerr tautulli dockhand 2>/dev/null"
    )
    code, _, stderr = run_ssh(cmd_tar, timeout=180)
    if code != 0:
        return False, f"appdata tar failed (code {code}): {stderr.strip()}"

    code, size_out, _ = run_ssh(f"ls -lh '{dest_compose}' '{dest_tar}' | awk '{{print $9, $5}}'")
    return True, f"docker-appdata -> {dest_compose} & {dest_tar}"

def main():
    quiet = "--quiet" in sys.argv or "-q" in sys.argv
    now = datetime.now(PT)
    ts = now.strftime("%Y%m%d_%H%M%S")
    date_human = now.strftime("%Y-%m-%d %I:%M %p PT")
    if not quiet:
        print(f"[{date_human}] Starting Host 1 Local Backup to /volumeUSB1/usbshare/...")

    dirs = ["postgres-arr", "homeassistant", "docker-appdata"]
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
        print("• Backing up Home Assistant (config + credentials)...")
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
