#!/usr/bin/env python3
"""
Zero Standby Replica Sync Script
1. Pulls latest code, tools, and skills from GitHub (git pull origin main).
2. Streams an air-gapped memory & state snapshot directly from Host 2 (.84) over SSH/SFTP.
3. Strictly excludes .env, /secrets/, Discord bot tokens, and active SQLite DBs.
4. Debounces execution to run at most once per 12 hours unless --force is specified.
"""
import sys
import os
import io
import time
import json
import argparse
import subprocess
import tarfile
from pathlib import Path

# Paths
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT / "tools"))

from nas_config import DOCKER_ROOT, HOST_2_IP as HOST2_IP, SSH_KEY, SSH_PORT, SSH_USER as HOST2_USER

TIMESTAMP_FILE = WORKSPACE_ROOT / "data" / "last_standby_sync.json"
SSH_KEY_PATH = Path(SSH_KEY)
HOST2_PORT = int(SSH_PORT)

# Whitelisted files to pull from Host 2 data directory
WHITELISTED_DATA_FILES = [
    "config/runtime_rules.json",
    "data/active_topics.json",
    "data/bot_status.json",
    "data/canonical_gifs.json",
    "data/gif_history.json",
    "data/marketing_senders.json",
    "data/morning_rotation_history.json",
    "data/reminders.json",
    "data/runtime_config.json",
    "data/schedule.json",
    "data/sidecar_status.json",
    "data/steam_games.json",
    "data/tasks.json",
]

# Strict blacklist security check
BLOCKED_PATTERNS = [
    ".env",
    "secrets",
    "banana_credentials",
    "turn_queue",
    "external_turn_queue",
    "in_flight_turn",
    "sessions.json",
    ".sqlite",
    ".db",
]

def is_recent_sync(hours: float = 12.0) -> bool:
    """Check if a successful sync occurred within the last `hours`."""
    if not TIMESTAMP_FILE.exists():
        return False
    try:
        with open(TIMESTAMP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        last_ts = data.get("timestamp", 0)
        return (time.time() - last_ts) < (hours * 3600)
    except Exception:
        return False

def record_sync_success(file_count: int, git_pulled: bool):
    """Save sync metadata and timestamp."""
    TIMESTAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files_synced": file_count,
        "git_pulled": git_pulled,
        "status": "success"
    }
    with open(TIMESTAMP_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

def run_git_pull() -> bool:
    """Pull latest code from GitHub."""
    print("[Sync] Checking GitHub repository (git pull origin main)...")
    try:
        res = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            timeout=15
        )
        if res.returncode == 0:
            print(f"[Sync] Git pull clean: {res.stdout.strip()}")
            return True
        else:
            print(f"[Sync] Git pull notice: {res.stderr.strip()}")
            return False
    except Exception as e:
        print(f"[Sync] Git pull skipped: {e}")
        return False

def sync_host2_snapshot() -> int:
    """Stream memory and state from Host 2 over SSH tar stream."""
    import paramiko

    if not SSH_KEY_PATH.exists():
        raise FileNotFoundError(f"SSH key not found at {SSH_KEY_PATH}")

    print(f"[Sync] Connecting to Host 2 ({HOST2_IP}:{HOST2_PORT})...")
    key = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_PATH))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(
            HOST2_IP,
            port=HOST2_PORT,
            username=HOST2_USER,
            pkey=key,
            timeout=6
        )
    except Exception as e:
        print(f"[Sync] Could not reach Host 2 ({HOST2_IP}): {e}")
        print("[Sync] Host 2 may be offline or unreachable on LAN. Standby files unchanged.")
        return 0

    # Build tar command for whitelisted targets
    # -h dereferences symlinks so local Windows receives full files
    targets = ["memory"] + WHITELISTED_DATA_FILES
    targets_str = " ".join(targets)
    base_dir = f"{DOCKER_ROOT.rstrip('/')}/discord-agy-agent"
    tar_cmd = (
        f"if [ -d {base_dir}/workspace/memory ]; then "
        f"tar -czh -C {base_dir}/workspace {targets_str} 2>/dev/null; "
        f"else tar -czh -C {base_dir} {targets_str} 2>/dev/null; fi"
    )

    t0 = time.time()
    stdin, stdout, stderr = client.exec_command(tar_cmd)
    raw_data = stdout.read()
    client.close()
    elapsed = time.time() - t0

    if not raw_data:
        print("[Sync] No data received from Host 2 tar command.")
        return 0

    print(f"[Sync] Streamed {len(raw_data):,} bytes in {elapsed:.2f}s. Extracting into workspace...")

    extracted_count = 0
    with tarfile.open(fileobj=io.BytesIO(raw_data), mode="r:gz") as tar:
        for member in tar.getmembers():
            # Security guard: never extract blacklisted files
            if any(blocked in member.name.lower() for blocked in BLOCKED_PATTERNS):
                print(f"[Security Guard] Blocked extracted member: {member.name}")
                continue
            
            # Destination safety check
            dest_path = WORKSPACE_ROOT / member.name
            try:
                dest_path.resolve().relative_to(WORKSPACE_ROOT.resolve())
            except ValueError:
                print(f"[Security Guard] Path traversal attempt blocked: {member.name}")
                continue

            tar.extract(member, path=str(WORKSPACE_ROOT))
            if member.isfile():
                extracted_count += 1

    print(f"[Sync] Successfully extracted {extracted_count} state & memory files.")
    return extracted_count

def main():
    parser = argparse.ArgumentParser(description="Zero Standby Replica Sync")
    parser.add_argument("--force", action="store_true", help="Force sync ignoring the 12-hour debounce")
    args = parser.parse_args()

    print("==================================================")
    print("Zero Standby Replica Sync (Workstation Shard)")
    print("==================================================")

    if not args.force and is_recent_sync(hours=12.0):
        with open(TIMESTAMP_FILE, "r", encoding="utf-8") as f:
            meta = json.load(f)
        last_iso = meta.get("timestamp_iso", "recently")
        print(f"[Sync] Standby state is already fresh (last sync: {last_iso}).")
        print("[Sync] Exiting cleanly (0ms). Use --force to override debounce.")
        return 0

    # 1. Pull code from GitHub
    git_success = run_git_pull()

    # 2. Pull memory & state snapshot from Host 2
    try:
        file_count = sync_host2_snapshot()
        record_sync_success(file_count, git_success)
        print(f"[Sync] Complete. Standby replica updated successfully ({file_count} files).")
    except Exception as e:
        print(f"[Sync] Failed during Host 2 snapshot sync: {e}")
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())
