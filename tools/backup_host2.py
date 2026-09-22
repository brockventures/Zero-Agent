#!/usr/bin/env python3
"""
backup_host2.py — Automated local backup runner for Host 2 stateful services.
Targets the ext4 USB volume at /volumeUSB1/usbshare/backups/.
Backs up:
  - baseball_db (PostgreSQL baseball_data)
  - mealie (SQLite DB + recipes + secrets)
  - openmessage (SQLite messages.db + sessions + tokens)
  - zero (memory + data + configs)
Enforces a 14-day rolling retention policy.
"""

import os
import sys
import time
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
RETENTION_DAYS = 14

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.nas_config import resolve_nas_config, run_ssh as _nas_run_ssh, DOCKER_ROOT
except ImportError:
    from nas_config import resolve_nas_config, run_ssh as _nas_run_ssh, DOCKER_ROOT

_, HOST_2_IP, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

def run_ssh(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    return _nas_run_ssh(HOST_2_IP, cmd, timeout=timeout, user=SSH_USER, port=SSH_PORT, key=SSH_KEY)

def prune_old_backups(dir_path: str, days: int = RETENTION_DAYS) -> int:
    cmd = f"find {dir_path} -type f -mtime +{days} -delete 2>/dev/null"
    code, _, _ = run_ssh(cmd)
    return code

def backup_baseball(ts: str) -> tuple[bool, str]:
    dest = f"/volumeUSB1/usbshare/backups/baseball/baseball_data_{ts}.sql.gz"
    cmd = f"sudo docker exec baseball_db pg_dump -U myuser -d baseball_data | gzip > '{dest}'"
    code, stdout, stderr = run_ssh(cmd, timeout=120)
    if code != 0:
        return False, f"pg_dump failed (code {code}): {stderr.strip()}"
    
    code, size_out, _ = run_ssh(f"ls -lh '{dest}' | awk '{{print $5}}'")
    return True, f"baseball_data -> {dest} ({size_out.strip()})"

def backup_mealie(ts: str) -> tuple[bool, str]:
    dest_db = f"/volumeUSB1/usbshare/backups/mealie/mealie_{ts}.db"
    dest_tar = f"/volumeUSB1/usbshare/backups/mealie/mealie_recipes_{ts}.tar.gz"
    
    # 1. WAL-safe SQLite backup
    cmd_db = f"sudo sqlite3 '{DOCKER_ROOT}/mealie/data/mealie.db' \".backup '{dest_db}'\""
    code, _, stderr = run_ssh(cmd_db, timeout=60)
    if code != 0:
        return False, f"mealie sqlite backup failed: {stderr.strip()}"
    
    # 2. Recipes & config tarball
    cmd_tar = f"sudo tar -czf '{dest_tar}' -C '{DOCKER_ROOT}/mealie/data' recipes .secret .session_secret 2>/dev/null"
    code, _, stderr = run_ssh(cmd_tar, timeout=60)
    if code != 0:
        return False, f"mealie tar failed: {stderr.strip()}"

    code, size_out, _ = run_ssh(f"ls -lh '{dest_db}' '{dest_tar}' | awk '{{print $9, $5}}'")
    return True, f"mealie -> {dest_db} & {dest_tar}"

def backup_openmessage(ts: str) -> tuple[bool, str]:
    dest_db = f"/volumeUSB1/usbshare/backups/openmessage/messages_{ts}.db"
    dest_tar = f"/volumeUSB1/usbshare/backups/openmessage/session_{ts}.tar.gz"

    cmd_db = f"sudo sqlite3 '{DOCKER_ROOT}/openmessage/data/messages.db' \".backup '{dest_db}'\""
    code, _, stderr = run_ssh(cmd_db, timeout=60)
    if code != 0:
        return False, f"openmessage sqlite backup failed: {stderr.strip()}"

    cmd_tar = f"sudo tar -czf '{dest_tar}' -C '{DOCKER_ROOT}/openmessage/data' session.json control.token 2>/dev/null"
    code, _, stderr = run_ssh(cmd_tar, timeout=60)
    if code != 0:
        return False, f"openmessage tar failed: {stderr.strip()}"

    return True, f"openmessage -> {dest_db} & {dest_tar}"

def backup_zero(ts: str) -> tuple[bool, str]:
    dest_tar = f"/volumeUSB1/usbshare/backups/zero/zero_state_{ts}.tar.gz"
    cmd = (
        f"sudo tar --exclude='backups' --exclude='*.log' --exclude='*.out' "
        f"--exclude='__pycache__' --exclude='scratch' --exclude='.git' "
        f"-czf '{dest_tar}' -C '{DOCKER_ROOT}/discord-agy-agent' "
        f"memory data agents.md config docker-compose.yml entrypoint.sh bridge.py 2>/dev/null"
    )
    code, _, stderr = run_ssh(cmd, timeout=120)
    if code != 0:
        return False, f"zero state backup failed: {stderr.strip()}"
    
    code, size_out, _ = run_ssh(f"ls -lh '{dest_tar}' | awk '{{print $5}}'")
    return True, f"zero -> {dest_tar} ({size_out.strip()})"

def main():
    quiet = "--quiet" in sys.argv or "-q" in sys.argv
    now = datetime.now(PT)
    ts = now.strftime("%Y%m%d_%H%M%S")
    date_human = now.strftime("%Y-%m-%d %I:%M %p PT")
    if not quiet:
        print(f"[{date_human}] Starting Host 2 Local Backup to /volumeUSB1/usbshare/...")

    dirs = ["baseball", "mealie", "openmessage", "zero"]
    for d in dirs:
        run_ssh(f"sudo mkdir -p /volumeUSB1/usbshare/backups/{d} && sudo chown -R {SSH_USER}:users /volumeUSB1/usbshare/backups/{d}")

    results = {}
    
    if not quiet:
        print("• Backing up Baseball PostgreSQL...")
    ok, msg = backup_baseball(ts)
    results["baseball"] = (ok, msg)
    if not quiet:
        print(f"  {'✅' if ok else '❌'} {msg}")

    if not quiet:
        print("• Backing up Mealie (SQLite + recipes)...")
    ok, msg = backup_mealie(ts)
    results["mealie"] = (ok, msg)
    if not quiet:
        print(f"  {'✅' if ok else '❌'} {msg}")

    if not quiet:
        print("• Backing up OpenMessage (messages.db + sessions)...")
    ok, msg = backup_openmessage(ts)
    results["openmessage"] = (ok, msg)
    if not quiet:
        print(f"  {'✅' if ok else '❌'} {msg}")

    if not quiet:
        print("• Backing up Zero Agent State (memory + data)...")
    ok, msg = backup_zero(ts)
    results["zero"] = (ok, msg)
    if not quiet:
        print(f"  {'✅' if ok else '❌'} {msg}")

    if not quiet:
        print(f"• Pruning backups older than {RETENTION_DAYS} days...")
    for d in dirs:
        prune_old_backups(f"/volumeUSB1/usbshare/backups/{d}")
    if not quiet:
        print("  ✅ Retention policy enforced.")

    _, df_out, _ = run_ssh("df -h /volumeUSB1/usbshare | tail -n 1")
    if not quiet:
        print(f"• Target Volume Status: {df_out.strip()}")

    all_ok = all(ok for ok, _ in results.values())
    if all_ok:
        if not quiet:
            print("🎉 Host 2 Backup completed successfully!")
        sys.exit(0)
    else:
        if not quiet:
            print("⚠️ Host 2 Backup completed with errors.")
        else:
            for k, (ok_val, m) in results.items():
                if not ok_val:
                    print(f"❌ {k}: {m}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
