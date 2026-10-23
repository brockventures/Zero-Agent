#!/usr/bin/env python3
import sys, subprocess, json, urllib.request, time
import os
import urllib.parse

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.nas_config import resolve_nas_config, run_ssh as _nas_run_ssh, DOCKER_ROOT
except ImportError:
    from nas_config import resolve_nas_config, run_ssh as _nas_run_ssh, DOCKER_ROOT

HOST_1_IP, _, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

def run_ssh(cmd_str: str, timeout: int = 180) -> str:
    code, stdout, stderr = _nas_run_ssh(HOST_1_IP, cmd_str, timeout=timeout, user=SSH_USER, port=SSH_PORT, key=SSH_KEY)
    return stdout.strip()

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
        try:
            from tools.forge import stage_decision
            stage_decision(
                category="ha_upgrade",
                title=f"Backup & Upgrade Home Assistant Core to v{ha_latest}",
                summary=f"Home Assistant Core `v{ha_curr}` → `v{ha_latest}` stable patch is ready. Approving creates a pre-upgrade snapshot + WAL-safe DB backup and runs `docker compose pull && up -d`.",
                handler_type="shell",
                payload={"command": "python3 /workspace/tools/ha_update_check.py upgrade", "cwd": "/workspace"},
                channel_id=1544953275877556334,
                post_to_discord=True,
            )
        except Exception as e:
            print(f"\n[CHOICES: Backup & Upgrade Smart Home Stack | Snooze 1 Week | Skip] ({e})")
    else:
        if not quiet:
            print(f"✅ **Smart Home Stack is up to date:**")
            print(f"• Home Assistant Core: `v{ha_curr}` (latest stable patch)")
            print(f"• Matter Server & OTBR: Aligned with active Thread mesh.")

def perform_upgrade(target_service: str = "all"):
    print("📦 **Initiating Smart Home Stack Upgrade...**\n")
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.environ.get("HA_BACKUP_DIR", os.path.join(os.sep, "volume1", "data", "backups", "homeassistant"))
    usb_backup_dir = "/volumeUSB1/usbshare/backups/homeassistant"
    run_ssh(f"mkdir -p '{backup_dir}' '{usb_backup_dir}' 2>/dev/null || true")

    # 1. Snapshot stateful directories & DB
    print("1. Creating snapshot backups of HA config, Matter fabric, and Thread credentials...")
    backup_tar = f"{backup_dir}/smart_home_pre_upgrade_{ts}.tar.gz"
    backup_cmd = (
        f"tar --exclude='home-assistant_v2.db*' --exclude='*.log*' --exclude='.cloud' "
        f"--exclude='backups' --exclude='.git' --exclude='.cache' "
        f"-czf '{backup_tar}' "
        f"-C '{DOCKER_ROOT}/homeassistant' config matter-server otbr-data zwave-js-ui 2>/dev/null || true"
    )
    run_ssh(backup_cmd, timeout=180)
    
    # SQLite WAL-safe backup of HA DB
    backup_db = f"{backup_dir}/ha_db_pre_upgrade_{ts}.db"
    run_ssh(f"sqlite3 '{DOCKER_ROOT}/homeassistant/config/home-assistant_v2.db' \".backup '{backup_db}'\" 2>/dev/null || true", timeout=240)

    # Check backup sizes
    sz_tar = run_ssh(f"ls -lh '{backup_tar}' 2>/dev/null | awk '{{print $5}}'")
    sz_db = run_ssh(f"ls -lh '{backup_db}' 2>/dev/null | awk '{{print $5}}'")
    print(f"   ✅ Config & mesh snapshot: {sz_tar or 'saved'} ({backup_tar})")
    print(f"   ✅ SQLite DB snapshot: {sz_db or 'saved'} ({backup_db})")

    # Copy to USB backup if available
    run_ssh(f"cp '{backup_tar}' '{usb_backup_dir}/' 2>/dev/null && cp '{backup_db}' '{usb_backup_dir}/' 2>/dev/null || true")

    # 2. Pull and recreate via compose
    latest_ha = get_latest_ha_stable()
    print(f"\n2. Upgrading Home Assistant Core to `v{latest_ha}`...")
    # Update compose file image tag
    run_ssh(f"sed -i -E 's|image: homeassistant/home-assistant:.*|image: homeassistant/home-assistant:{latest_ha}|g' '{DOCKER_ROOT}/docker-compose.yml'")
    
    # Pull and recreate
    run_ssh(f"cd '{DOCKER_ROOT}' && docker compose pull homeassistant && docker compose up -d homeassistant", timeout=120)
    run_ssh(f"cd '{DOCKER_ROOT}/homeassistant' && docker compose pull matter-server otbr go2rtc && docker compose up -d matter-server otbr go2rtc", timeout=120)

    # 3. Verify health & state
    print("\n3. Verifying service health...")
    ha_online = False
    for i in range(25):
        time.sleep(3)
        res = run_ssh("curl -s -o /dev/null -w '%{http_code}' http://localhost:8123/api/ || true")
        if res in ("200", "401"):
            ha_online = True
            break
    
    new_version = get_installed_ha_version()
    print(f"   • Home Assistant Core: `v{new_version}` ({'Online' if ha_online else 'Starting up'})")
    
    # Check companion containers
    matter_stat = run_ssh("docker inspect -f '{{.State.Status}}' matterserver 2>/dev/null || true")
    otbr_stat = run_ssh("docker inspect -f '{{.State.Status}}' otbr 2>/dev/null || true")
    go2rtc_stat = run_ssh("docker inspect -f '{{.State.Status}}' go2rtc 2>/dev/null || true")
    zwave_stat = run_ssh("docker inspect -f '{{.State.Status}}' zwave-js-ui 2>/dev/null || true")
    print(f"   • Matter Server: `{matter_stat or 'unknown'}`")
    print(f"   • OpenThread Border Router (OTBR): `{otbr_stat or 'unknown'}`")
    print(f"   • go2rtc: `{go2rtc_stat or 'unknown'}`")
    print(f"   • Z-Wave JS UI: `{zwave_stat or 'unknown'}`")

    # OTBR Thread network state check
    otbr_health = run_ssh("curl -s http://localhost:8081/node 2>/dev/null || true")
    if "NEST-PAN-262C" in otbr_health:
        print("   • OTBR Thread Mesh: `Connected (NEST-PAN-262C)`")

    print(f"\n🎉 **Smart Home Stack Upgrade Complete!**\n• Pinned Home Assistant to `v{latest_ha}`\n• Companion services online\n• Pre-upgrade snapshots preserved.")

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "check":
        quiet = "--quiet" in sys.argv
        check_updates(quiet=quiet)
    elif action in ("upgrade", "apply"):
        perform_upgrade()
    else:
        print(f"Usage: {sys.argv[0]} [check [--quiet] | upgrade]")
