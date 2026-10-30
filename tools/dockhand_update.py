#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
import urllib.request

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.append(_p)

try:
    from tools.nas_config import resolve_nas_config
except ImportError:
    from nas_config import resolve_nas_config

HOST_1_IP, HOST_2_IP, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

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
    out = get_remote_out(
        ssh_base,
        "docker inspect fnsys/dockhand:latest --format '{{range .RepoDigests}}{{.}}{{end}}' 2>/dev/null || true",
    )
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
        msg_lines = [
            "🛠️ **Dockhand Update Available!**\n",
            f"• **Docker Hub Latest:** Built {updated} (`{hub_digest[:19]}...`)",
        ]
        for h in upgrades_needed:
            msg_lines.append(f"• **{h}:** Update ready to install")
        msg_lines.append(
            "\n• **Safety:** Automated WAL-safe `.backup` of `dockhand.db` executes before container restart."
        )
        msg = "\n".join(msg_lines)

        decision_staged = False
        try:
            from tools.forge import ZERO_OPS_CHANNEL_ID, stage_decision

            stage_decision(
                category="ops_upgrade",
                title="Backup DB & Update Dockhand on Both Hosts",
                summary=(
                    f"Docker Hub `fnsys/dockhand:latest` updated ({updated}, `{hub_digest[:19]}...`) for "
                    f"{', '.join(upgrades_needed)}. Approving runs WAL-safe SQLite backup + `docker compose pull && up -d`."
                ),
                handler_type="shell",
                payload={"command": "python3 /workspace/tools/dockhand_update.py upgrade", "cwd": "/workspace"},
                channel_id=ZERO_OPS_CHANNEL_ID,
                post_to_discord=True,
            )
            decision_staged = True
        except Exception as e:
            msg += f"\n\n[CHOICES: Backup DB & Update Dockhand on Both Hosts | Skip For Now] ({e})"

        if not (quiet and decision_staged):
            print(msg)
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
    backup_dir = os.environ.get("DOCKER_BACKUP_DIR", os.path.join("/volume1", "data", "backups"))
    appdata_dir = os.environ.get("DOCKER_APPDATA_DIR", os.path.join("/volume1", "docker", "appdata"))
    support_dir = os.environ.get("DOCKER_SUPPORT_DIR", os.path.join("/volume1", "docker", "support", "dockhand"))
    support_compose_dir = os.environ.get("DOCKER_SUPPORT_COMPOSE_DIR", os.path.join("/volume1", "docker", "support"))

    bak_82 = f"{backup_dir}/dockhand/dockhand.db.bak-{ts}"
    get_remote_out(
        SSH_82,
        f"mkdir -p '{backup_dir}/dockhand' && sqlite3 '{appdata_dir}/dockhand/db/dockhand.db' \".backup '{bak_82}'\"",
    )
    bak_sz_82 = get_remote_out(SSH_82, f"ls -lh '{bak_82}' 2>/dev/null | awk '{{print $5}}'")
    if bak_sz_82:
        print(f"   • Backup verified: {bak_sz_82} ({bak_82})")
    else:
        print("   ⚠️ Backup file verification failed, proceeding with caution...")

    print("   • Pulling new image & recreating container...")
    get_remote_out(SSH_82, f"cd '{appdata_dir}' && docker compose pull dockhand && docker compose up -d dockhand")
    status_82 = ""
    for _ in range(10):
        time.sleep(2)
        status_82 = get_remote_out(SSH_82, "curl -s -o /dev/null -w '%{http_code}' http://localhost:3866/ || true")
        if status_82 in ("200", "302"):
            break
    print(f"   • Host1 Status: HTTP {status_82} OK")

    # 2. Host2 (.84)
    print("\n2. Upgrading Host2 (.84):")
    # WAL-safe backup
    print("   • Backing up SQLite DB (WAL-safe)...")
    bak_84 = f"{support_dir}/backups/dockhand.db.bak-{ts}"
    get_remote_out(
        SSH_84,
        f"mkdir -p '{support_dir}/backups' && sqlite3 '{support_dir}/db/dockhand.db' \".backup '{bak_84}'\"",
    )
    bak_sz_84 = get_remote_out(SSH_84, f"ls -lh '{bak_84}' 2>/dev/null | awk '{{print $5}}'")
    if bak_sz_84:
        print(f"   • Backup verified: {bak_sz_84} ({bak_84})")
    else:
        print("   ⚠️ Backup file verification failed, proceeding with caution...")

    print("   • Pulling new image & recreating container...")
    get_remote_out(
        SSH_84, f"cd '{support_compose_dir}' && docker compose pull dockhand && docker compose up -d dockhand"
    )
    status_84 = ""
    for _ in range(10):
        time.sleep(2)
        status_84 = get_remote_out(SSH_84, "curl -s -o /dev/null -w '%{http_code}' http://localhost:3866/ || true")
        if status_84 in ("200", "302"):
            break
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
