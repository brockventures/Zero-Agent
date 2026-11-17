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

import sys
from datetime import datetime
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


def _is_sudo_auth_error(stderr: str) -> bool:
    """Return True when stderr indicates sudo failed authentication or lacked a TTY."""
    low = (stderr or "").lower()
    return (
        "a password is required" in low
        or "a terminal is required" in low
        or "incorrect password" in low
        or "sudo:" in low
    )


def prune_old_backups(dir_path: str, days: int = RETENTION_DAYS) -> int:
    cmd = f"find {dir_path} -type f -mtime +{days} -delete 2>/dev/null"
    code, _, _ = run_ssh(cmd)
    return code


def backup_baseball(ts: str) -> tuple[bool, str]:
    dest = f"/volumeUSB1/usbshare/backups/baseball/baseball_data_{ts}.sql.gz"
    cmd = f"bash -o pipefail -c \"sudo env PATH=/usr/local/bin:/usr/bin:/bin docker exec baseball_db pg_dump -U myuser -d baseball_data | gzip > '{dest}'\""
    code, _, stderr = run_ssh(cmd, timeout=120)
    if code != 0 or _is_sudo_auth_error(stderr):
        run_ssh(f"rm -f '{dest}'")
        return False, f"pg_dump failed (code {code}): {stderr.strip()}"

    size_code, size_bytes_out, _ = run_ssh(f"stat -c%s '{dest}' 2>/dev/null || echo 0")
    try:
        size_bytes = int(size_bytes_out.strip() or "0")
    except ValueError:
        size_bytes = 0
    if size_code != 0 or size_bytes <= 100:
        run_ssh(f"rm -f '{dest}'")
        return False, f"pg_dump produced empty/invalid archive ({size_bytes} bytes)"

    _, size_out, _ = run_ssh(f"ls -lh '{dest}' | awk '{{print $5}}'")
    return True, f"baseball_data -> {dest} ({size_out.strip()})"


def backup_mealie(ts: str) -> tuple[bool, str]:
    dest_db = f"/volumeUSB1/usbshare/backups/mealie/mealie_{ts}.db"
    dest_tar = f"/volumeUSB1/usbshare/backups/mealie/mealie_recipes_{ts}.tar.gz"

    # 1. WAL-safe SQLite backup
    cmd_db = f"sudo sqlite3 '{DOCKER_ROOT}/mealie/data/mealie.db' \".backup '{dest_db}'\""
    code, _, stderr = run_ssh(cmd_db, timeout=60)
    if code != 0:
        run_ssh(f"rm -f '{dest_db}'")
        return False, f"mealie sqlite backup failed: {stderr.strip()}"

    # 2. Recipes & config tarball
    cmd_tar = f"sudo tar --warning=no-file-changed -czf '{dest_tar}' -C '{DOCKER_ROOT}/mealie/data' recipes .secret .session_secret"
    code, _, stderr = run_ssh(cmd_tar, timeout=60)
    if code not in (0, 1) or _is_sudo_auth_error(stderr):
        run_ssh(f"rm -f '{dest_tar}'")
        return False, f"mealie tar failed (code {code}): {stderr.strip()}"

    run_ssh(f"ls -lh '{dest_db}' '{dest_tar}' | awk '{{print $9, $5}}'")
    return True, f"mealie -> {dest_db} & {dest_tar}"


def backup_openmessage(ts: str) -> tuple[bool, str]:
    dest_db = f"/volumeUSB1/usbshare/backups/openmessage/messages_{ts}.db"
    dest_tar = f"/volumeUSB1/usbshare/backups/openmessage/session_{ts}.tar.gz"

    cmd_db = f"sudo sqlite3 '{DOCKER_ROOT}/openmessage/data/messages.db' \".backup '{dest_db}'\""
    code, _, stderr = run_ssh(cmd_db, timeout=60)
    if code != 0:
        run_ssh(f"rm -f '{dest_db}'")
        return False, f"openmessage sqlite backup failed: {stderr.strip()}"

    cmd_tar = f"sudo tar --warning=no-file-changed -czf '{dest_tar}' -C '{DOCKER_ROOT}/openmessage/data' session.json control.token"
    code, _, stderr = run_ssh(cmd_tar, timeout=60)
    if code not in (0, 1) or _is_sudo_auth_error(stderr):
        run_ssh(f"rm -f '{dest_tar}'")
        return False, f"openmessage tar failed (code {code}): {stderr.strip()}"

    return True, f"openmessage -> {dest_db} & {dest_tar}"


def backup_zero(ts: str) -> tuple[bool, str]:
    dest_tar = f"/volumeUSB1/usbshare/backups/zero/zero_state_{ts}.tar.gz"
    cmd = (
        f"sudo tar --warning=no-file-changed --exclude='backups' --exclude='*.log' --exclude='*.out' "
        f"--exclude='__pycache__' --exclude='scratch' --exclude='.git' "
        f"-czf '{dest_tar}' -C '{DOCKER_ROOT}/discord-agy-agent' "
        f"memory data agents.md config docker-compose.yml entrypoint.sh bridge.py"
    )
    code, _, stderr = run_ssh(cmd, timeout=120)
    if code not in (0, 1) or _is_sudo_auth_error(stderr):
        run_ssh(f"rm -f '{dest_tar}'")
        return False, f"zero state backup failed (code {code}): {stderr.strip()}"

    _, size_out, _ = run_ssh(f"ls -lh '{dest_tar}' | awk '{{print $5}}'")
    return True, f"zero -> {dest_tar} ({size_out.strip()})"


def main():
    quiet = "--quiet" in sys.argv or "-q" in sys.argv
    now = datetime.now(PT)
    ts = now.strftime("%Y%m%d_%H%M%S")
    date_human = now.strftime("%Y-%m-%d %I:%M %p PT")
    if not quiet:
        print(f"[{date_human}] Starting Host 2 Local Backup to /volumeUSB1/usbshare/...")

    sudo_code, _, sudo_err = run_ssh("sudo -n true", timeout=15)
    if sudo_code != 0:
        err_msg = f"passwordless sudo unavailable on Host 2 (code {sudo_code}): {sudo_err.strip() or 'sudo -n true failed'}"
        if not quiet:
            print(f"❌ {err_msg}")
        else:
            print(f"❌ sudo: {err_msg}", file=sys.stderr)
        sys.exit(1)

    dirs = ["baseball", "mealie", "openmessage", "zero"]
    for d in dirs:
        mk_code, _, mk_err = run_ssh(
            f"sudo mkdir -p /volumeUSB1/usbshare/backups/{d} && sudo chown -R {SSH_USER}:users /volumeUSB1/usbshare/backups/{d}"
        )
        if mk_code != 0:
            err_msg = f"failed preparing backup directory {d} (code {mk_code}): {mk_err.strip()}"
            if not quiet:
                print(f"❌ {err_msg}")
            else:
                print(f"❌ setup: {err_msg}", file=sys.stderr)
            sys.exit(1)

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
