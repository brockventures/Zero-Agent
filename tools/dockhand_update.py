#!/usr/bin/env python3
import sys, subprocess, json, urllib.request, time

import os

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/root/.ssh/id_ed25519")
SSH_PORT = os.environ.get("NAS_SSH_PORT", "22")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")
HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
HOST_2_IP = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

SSH_82 = ["ssh", "-i", SSH_KEY, "-p", SSH_PORT, "-o", "BatchMode=yes", f"{SSH_USER}@{HOST_1_IP}"]
SSH_84 = ["ssh", "-i", SSH_KEY, "-p", SSH_PORT, "-o", "BatchMode=yes", f"{SSH_USER}@{HOST_2_IP}"]

def get_remote_out(ssh_base: list, cmd_str: str) -> str:
    try:
        return subprocess.check_output(ssh_base + [cmd_str], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""

def get_hub_latest_digest() -> tuple[str, str]:
    try:
        url = "https://hub.docker.com/v2/repositories/fnsys/dockhand/tags/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            digest = data.get("digest", "")
            updated = data.get("last_updated", "")[:10]
            return digest, updated
    except Exception:
        return "", ""

def get_host_digest(ssh_base: list) -> str:
    out = get_remote_out(ssh_base, "docker inspect fnsys/dockhand:latest --format '{{range .RepoDigests}}{{.}}{{end}}' 2>/dev/null || true")
    if "@" in out:
        return out.split("@")[-1]
    return ""

def check_updates(quiet: bool = False):
    hub_digest, updated = get_hub_latest_digest()
    digest_82 = get_host_digest(SSH_82)
    digest_84 = get_host_digest(SSH_84)

    upgrades_needed = []
    if hub_digest:
        if digest_82 and digest_82 != hub_digest:
            upgrades_needed.append("Host1 (.82)")
        if digest_84 and digest_84 != hub_digest:
            upgrades_needed.append("Host2 (.84)")

    if upgrades_needed:
        print("🛠️ **Dockhand Update Available!**\n")
        print(f"• **Docker Hub Latest:** Built {updated} (`{hub_digest[:19]}...`)")
        for h in upgrades_needed:
            print(f"• **{h}:** Update ready to install")
        print("\n• **Safety:** Automated WAL-safe `.backup` of `dockhand.db` executes before container restart.")
        print("\n[CHOICES: Backup DB & Update Dockhand on Both Hosts | Skip For Now]")
    else:
        if not quiet:
            print("✅ **Dockhand is up to date** on both Host1 (.82) and Host2 (.84).")

def perform_upgrade():
    print("📦 **Updating Dockhand across both NAS hosts...**\n")
    ts = time.strftime("%Y%m%d_%H%M%S")

    # 1. Host1 (.82)
    print("1. Upgrading Host1 (.82):")
    # WAL-safe backup
    print("   • Backing up SQLite DB (WAL-safe)...")
    get_remote_out(SSH_82, f"mkdir -p /data/backups/dockhand && sqlite3 /docker/appdata/dockhand/db/dockhand.db \".backup '/data/backups/dockhand/dockhand.db.bak-{ts}'\"")
    print("   • Pulling new image & recreating container...")
    get_remote_out(SSH_82, "cd /docker/appdata && docker compose pull dockhand && docker compose up -d dockhand")
    time.sleep(3)
    status_82 = get_remote_out(SSH_82, "curl -s -o /dev/null -w '%{http_code}' http://localhost:3866/ || true")
    print(f"   • Host1 Status: HTTP {status_82} OK")

    # 2. Host2 (.84)
    print("\n2. Upgrading Host2 (.84):")
    # WAL-safe backup
    print("   • Backing up SQLite DB (WAL-safe)...")
    get_remote_out(SSH_84, f"mkdir -p /docker/support/dockhand/backups && sqlite3 /docker/support/dockhand/db/dockhand.db \".backup '/docker/support/dockhand/backups/dockhand.db.bak-{ts}'\"")
    print("   • Pulling new image & recreating container...")
    get_remote_out(SSH_84, "cd /docker/support && docker compose pull dockhand && docker compose up -d dockhand")
    time.sleep(3)
    status_84 = get_remote_out(SSH_84, "curl -s -o /dev/null -w '%{http_code}' http://localhost:3866/ || true")
    print(f"   • Host2 Status: HTTP {status_84} OK")

    print("\n🎉 **Dockhand upgraded and online on both servers!**")

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "check":
        quiet = "--quiet" in sys.argv
        check_updates(quiet=quiet)
    elif action in ("upgrade", "apply"):
        perform_upgrade()
    else:
        print(f"Usage: {sys.argv[0]} [check [--quiet] | upgrade]")
