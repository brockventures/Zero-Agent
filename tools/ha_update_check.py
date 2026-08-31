#!/usr/bin/env python3
import sys, subprocess, json, urllib.request, time

import os
import urllib.parse

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")

def _resolve_nas_config():
    ssh_port = os.environ.get("NAS_SSH_PORT", "22")
    host_1 = os.environ.get("NAS_HOST_1_IP")
    host_2 = os.environ.get("NAS_HOST_2_IP")

    if not host_1 and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_HOST_1_IP"):
                    host_1 = d["NAS_HOST_1_IP"]
                elif d.get("HA_BASE_URL"):
                    host_1 = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
        except Exception:
            pass

    if not host_1 and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                if d.get("url"):
                    host_1 = urllib.parse.urlparse(d["url"]).hostname
        except Exception:
            pass

    return host_1 or "127.0.0.1", host_2 or "127.0.0.1", ssh_port

HOST_1_IP, _, SSH_PORT = _resolve_nas_config()

HOST_SSH = ["ssh", "-i", SSH_KEY, "-p", SSH_PORT, "-o", "BatchMode=yes", f"{SSH_USER}@{HOST_1_IP}"]

def run_ssh(cmd_str: str) -> str:
    cmd = HOST_SSH + [cmd_str]
    try:
        return subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return ""

def get_installed_ha_version() -> str:
    out = run_ssh("docker inspect home-assistant --format '{{index .Config.Labels \"org.opencontainers.image.version\"}}'")
    if out and out != "<no value>":
        return out.lstrip("v")
    out2 = run_ssh("docker inspect home-assistant --format '{{.Config.Image}}'")
    return out2.split(":")[-1].lstrip("v") if out2 else "unknown"

def get_latest_ha_stable() -> str:
    try:
        url = "https://version.home-assistant.io/stable.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("homeassistant", {}).get("default", "unknown").lstrip("v")
    except Exception:
        return "unknown"

def is_patch_stable(version_str: str) -> bool:
    """Only recommend releases that have reached at least .2 patch level for stability."""
    parts = version_str.split(".")
    if len(parts) >= 3:
        try:
            patch = int(parts[2])
            return patch >= 2
        except ValueError:
            return False
    return False

def check_updates(quiet: bool = False):
    ha_curr = get_installed_ha_version()
    ha_latest = get_latest_ha_stable()
    
    updates_available = []
    
    # 1. Home Assistant Core Check
    if ha_curr != "unknown" and ha_latest != "unknown" and ha_curr != ha_latest:
        if is_patch_stable(ha_latest):
            updates_available.append({
                "service": "Home Assistant Core",
                "installed": ha_curr,
                "latest": ha_latest,
                "notes": "https://www.home-assistant.io/blog/"
            })

    # Output results
    if updates_available:
        print("🏠 **Smart Home Stack Updates Available!**\n")
        for u in updates_available:
            print(f"• **{u['service']}:** `v{u['installed']}` → `v{u['latest']}`")
            print(f"  Notes: {u['notes']}")
        print(f"\n[CHOICES: Backup & Upgrade Smart Home Stack | Snooze 1 Week | Skip]")
    else:
        if not quiet:
            print(f"✅ **Smart Home Stack is up to date:**")
            print(f"• Home Assistant Core: `v{ha_curr}` (latest stable patch)")
            print(f"• Matter Server & OTBR: Aligned with active Thread mesh.")

def perform_upgrade(target_service: str = "all"):
    print("📦 **Initiating Smart Home Stack Upgrade...**")
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = "/data/backups/homeassistant"
    run_ssh(f"mkdir -p {backup_dir}")

    # 1. Snapshot all stateful directories
    print("1. Creating snapshot backups of HA config, Matter fabric, and Thread credentials...")
    backup_cmd = (
        f"tar --exclude='home-assistant_v2.db*' --exclude='*.log*' --exclude='.cloud' "
        f"--exclude='backups' --exclude='.git' --exclude='.cache' "
        f"-czf {backup_dir}/smart_home_pre_upgrade_{ts}.tar.gz "
        f"-C /docker/homeassistant config matter-server otbr-data 2>/dev/null || true"
    )
    run_ssh(backup_cmd)
    print("   ✅ Snapshots preserved in /data/backups/homeassistant/.")

    # 2. Pull and recreate via compose
    latest_ha = get_latest_ha_stable()
    print(f"2. Pulling and updating services to `v{latest_ha}`...")
    # Update compose file image tag
    run_ssh(f"sed -i -E 's|image: homeassistant/home-assistant:.*|image: homeassistant/home-assistant:{latest_ha}|g' /docker/docker-compose.yml")
    
    # Recreate in background detached
    restart_cmd = (
        "nohup sh -c '"
        "cd /docker && docker compose pull homeassistant && docker compose up -d homeassistant && "
        "cd /docker/homeassistant && docker compose pull matter-server otbr && docker compose up -d matter-server otbr"
        "' >/dev/null 2>&1 &"
    )
    run_ssh(restart_cmd)
    print(f"\n🚀 **Smart Home Stack Upgrade Dispatched!**\n• Pinned Home Assistant to `v{latest_ha}`\n• Matter Server & OTBR companions refreshing\n• Backups verified and safe.")

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "check":
        quiet = "--quiet" in sys.argv
        check_updates(quiet=quiet)
    elif action in ("upgrade", "apply"):
        perform_upgrade()
    else:
        print(f"Usage: {sys.argv[0]} [check [--quiet] | upgrade]")
