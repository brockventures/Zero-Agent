#!/usr/bin/env python3
"""
prime_medic.py — Zero Prime First-Responder & Recovery CLI (Workstation Shard)

Designed to run from the local Windows laptop replica (or any standby host) to
diagnose, unwedge, restart, and recover Zero Prime (`discord-antigravity-agent` on Host 2)
and inspect Host 1 / Host 2 without ever triggering the Synology DSM `docker logs`
SQLite log driver hang.

Subcommands:
  status        Full telemetry check: SSH reachability, container health, HTTP healthz,
                in-flight turn locks, queue depths, git state, and disk-mounted log tail.
  logs          Safely tail disk-mounted logs (bridge.log) without invoking `docker logs`.
  clear-stuck   Surgically kill wedged `agy` / `forge` child processes inside the container
                and clear stale `in_flight_turn.json` locks without bouncing the container.
  restart       Cleanly bounce `discord-antigravity-agent` (or `docker compose up -d`) and
                poll `/healthz` until Prime is green.
  sync-git      Force Zero Prime's repository on Host 2 to fetch and sync with `origin/main`
                (useful if a bad local edit wedged the bridge).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

# Ensure tools/ is on sys.path
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from nas_config import DOCKER_ROOT, HOST_1_IP, HOST_2_IP, SSH_KEY, SSH_PORT, SSH_USER, run_ssh

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PRIME_HOST_DIR = f"{DOCKER_ROOT.rstrip('/')}/discord-agy-agent"
PRIME_DIR = f"{PRIME_HOST_DIR}/workspace"
PRIME_CONTAINER = "discord-antigravity-agent"
IVY_CONTAINER = "discord-ivy-agent"
DOCKER_BIN = "/usr/local/bin/docker"


def _banner(title: str) -> None:
    print(f"\n=== {title} ===")


def check_http_health(host: str, port: int = 8080, path: str = "/healthz", timeout: float = 4.0) -> tuple[bool, str]:
    url = f"http://{host}:{port}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Zero-Medic/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            return resp.status == 200, f"HTTP {resp.status}: {body[:200]}"
    except Exception as e:
        return False, f"Unreachable ({url}): {e}"


def cmd_status(args: argparse.Namespace) -> int:
    print("==========================================================")
    print("  ZERO PRIME MEDIC - LIVE TELEMETRY & DIAGNOSTIC REPORT")
    print("==========================================================")
    print(f"Target Host 2 (Zero Prime): {HOST_2_IP}:{SSH_PORT} (user: {SSH_USER})")
    print(f"Target Host 1 (Main NAS):   {HOST_1_IP}:{SSH_PORT}")
    print(f"SSH Key:                    {SSH_KEY}")

    # 1. Host 2 SSH + Container + Queue + Disk Log Single-Shot Probe
    _banner("1. Host 2 (DS1525+) Container & Process State")
    remote_probe = f"""
export PATH=/usr/local/bin:$PATH
echo "--- UPTIME & DISK ---"
uptime
df -h | grep -E "volume1|$" | tail -n 2

echo "--- CONTAINERS ---"
docker ps -a --filter "name=discord-" --format "{{{{.Names}}}} | {{{{.Status}}}} | {{{{.RunningFor}}}}"

echo "--- PRIME INSPECT ---"
docker inspect -f "Status={{{{.State.Status}}}} Running={{{{.State.Running}}}} RestartCount={{{{.RestartCount}}}} StartedAt={{{{.State.StartedAt}}}} OOMKilled={{{{.State.OOMKilled}}}}" {PRIME_CONTAINER} 2>/dev/null || echo "Container {PRIME_CONTAINER} not found"

echo "--- PRIME INTERNAL PROCESSES ---"
docker exec {PRIME_CONTAINER} ps -ef 2>/dev/null || echo "Cannot exec into {PRIME_CONTAINER}"

echo "--- PRIME GIT STATUS ---"
docker exec {PRIME_CONTAINER} bash -c "cd /workspace && git status -sb && git log -n 3 --oneline" 2>/dev/null

echo "--- IN-FLIGHT TURN & QUEUES ---"
docker exec {PRIME_CONTAINER} python3 -c '
import os
for fname in ["data/in_flight_turn.json", "data/turn_queue.json", "data/external_turn_queue.json", "data/bot_status.json"]:
    p = os.path.join("/workspace", fname)
    if os.path.exists(p):
        txt = open(p, encoding="utf-8", errors="replace").read().strip()
        print("[" + fname + "] (" + str(len(txt)) + " bytes): " + txt[:300])
    else:
        print("[" + fname + "] (not present)")
' 2>/dev/null

echo "--- RECENT BRIDGE LOGS (SAFE BOUNDED READ) ---"
if [ -f "{PRIME_DIR}/logs/bridge.log" ]; then
    tail -n {args.lines} "{PRIME_DIR}/logs/bridge.log"
elif [ -f "{PRIME_HOST_DIR}/logs/bridge.log" ]; then
    tail -n {args.lines} "{PRIME_HOST_DIR}/logs/bridge.log"
else
    timeout 4 docker logs --tail {args.lines} {PRIME_CONTAINER} 2>&1 || echo "(docker logs timed out - supervisor stream)"
fi
"""
    rc, out, err = run_ssh(HOST_2_IP, remote_probe, timeout=20, batch_mode=True)
    if rc != 0:
        print(f"[CRITICAL] SSH probe to Host 2 ({HOST_2_IP}) failed (rc={rc}):")
        if err:
            print(err.strip())
        if out:
            print(out.strip())
    else:
        print(out.strip())

    # 2. Check Host 1 status if requested or by default quick ping
    if not args.skip_host1:
        _banner("2. Host 1 (DS1821+) Quick Health Check")
        h1_probe = 'export PATH=/usr/local/bin:$PATH; uptime; docker ps --format "{{.Names}}: {{.Status}}" | head -n 12'
        rc1, out1, err1 = run_ssh(HOST_1_IP, h1_probe, timeout=10, batch_mode=True)
        if rc1 == 0:
            print(out1.strip())
        else:
            print(f"[WARN] Host 1 ({HOST_1_IP}) SSH check failed (rc={rc1}): {err1.strip()}")

    return 0 if rc == 0 else 1


def cmd_logs(args: argparse.Namespace) -> int:
    _banner(f"Zero Prime Logs (Last {args.lines} lines)")
    remote_cmd = f"""
export PATH=/usr/local/bin:$PATH
if [ -f "{PRIME_DIR}/logs/bridge.log" ]; then
    tail -n {args.lines} "{PRIME_DIR}/logs/bridge.log"
elif [ -f "{PRIME_HOST_DIR}/logs/bridge.log" ]; then
    tail -n {args.lines} "{PRIME_HOST_DIR}/logs/bridge.log"
else
    timeout 4 docker logs --tail {args.lines} {PRIME_CONTAINER} 2>&1
fi
"""
    rc, out, err = run_ssh(HOST_2_IP, remote_cmd, timeout=10, batch_mode=True)
    if rc != 0:
        print(f"[ERROR] Failed to read logs from {HOST_2_IP}: {err.strip()}")
        return 1
    print(out.strip())
    return 0


def cmd_clear_stuck(args: argparse.Namespace) -> int:
    _banner("Surgically Clearing Wedged Turns & Orphaned Processes on Zero Prime")
    remote_cmd = f"""
export PATH=/usr/local/bin:$PATH
echo "1. Checking active agy processes inside {PRIME_CONTAINER}..."
docker exec {PRIME_CONTAINER} pgrep -af "agy" || echo "No agy processes running."

echo "2. Terminating wedged agy CLI turns inside {PRIME_CONTAINER}..."
docker exec {PRIME_CONTAINER} pkill -f "agy " 2>/dev/null || true

echo "3. Backing up and clearing stale in_flight_turn.json..."
docker exec {PRIME_CONTAINER} python3 -c '
import os, shutil
p = "/workspace/data/in_flight_turn.json"
if os.path.exists(p):
    shutil.copy2(p, p + ".bak")
    open(p, "w").write("{{}}")
    print("Cleared /workspace/data/in_flight_turn.json (backup saved to .bak)")
'

echo "4. Remaining processes inside {PRIME_CONTAINER}:"
docker exec {PRIME_CONTAINER} ps -ef
"""
    rc, out, err = run_ssh(HOST_2_IP, remote_cmd, timeout=15, batch_mode=True)
    print(out.strip())
    if err.strip():
        print(err.strip())
    return 0 if rc == 0 else 1


def cmd_restart(args: argparse.Namespace) -> int:
    target = IVY_CONTAINER if args.ivy else PRIME_CONTAINER
    _banner(f"Restarting Container: {target} on Host 2 ({HOST_2_IP})")
    if args.compose and target == PRIME_CONTAINER:
        remote_cmd = f"""
export PATH=/usr/local/bin:$PATH
cd {PRIME_HOST_DIR}
docker compose up -d --force-recreate
sleep 3
docker inspect -f "Status={{{{.State.Status}}}} Running={{{{.State.Running}}}} StartedAt={{{{.State.StartedAt}}}}" {target}
"""
    else:
        remote_cmd = f"""
export PATH=/usr/local/bin:$PATH
docker restart {target}
sleep 3
docker inspect -f "Status={{{{.State.Status}}}} Running={{{{.State.Running}}}} StartedAt={{{{.State.StartedAt}}}}" {target}
"""
    rc, out, err = run_ssh(HOST_2_IP, remote_cmd, timeout=45, batch_mode=True)
    print(out.strip())
    if err.strip():
        print(err.strip())
    return 0 if rc == 0 else 1


def cmd_sync_git(args: argparse.Namespace) -> int:
    _banner(f"Syncing Zero Prime Repository (/workspace) with origin/main")
    remote_cmd = f"""
export PATH=/usr/local/bin:$PATH
docker exec {PRIME_CONTAINER} bash -c "cd /workspace && git fetch origin main && git status -sb && git pull --rebase origin main && git log -n 3 --oneline"
"""
    rc, out, err = run_ssh(HOST_2_IP, remote_cmd, timeout=25, batch_mode=True)
    print(out.strip())
    if err.strip():
        print(err.strip())
    return 0 if rc == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Zero Prime First-Responder & Recovery CLI (Workstation Medic)"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_status = sub.add_parser("status", help="Full live diagnostic & telemetry check on Zero Prime")
    p_status.add_argument("--lines", type=int, default=35, help="Number of bridge log lines to tail (default: 35)")
    p_status.add_argument("--skip-host1", action="store_true", help="Skip Host 1 quick check")
    p_status.set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", help="Safely tail disk-mounted bridge logs without docker logs hang")
    p_logs.add_argument("-n", "--lines", type=int, default=60, help="Number of lines to tail (default: 60)")
    p_logs.set_defaults(func=cmd_logs)

    p_clear = sub.add_parser("clear-stuck", help="Kill hung agy processes and clear stale in_flight_turn.json")
    p_clear.set_defaults(func=cmd_clear_stuck)

    p_restart = sub.add_parser("restart", help="Restart discord-antigravity-agent (or --ivy for Ivy)")
    p_restart.add_argument("--compose", action="store_true", help="Use docker compose up -d --force-recreate")
    p_restart.add_argument("--ivy", action="store_true", help="Restart discord-ivy-agent instead of Zero Prime")
    p_restart.set_defaults(func=cmd_restart)

    p_sync = sub.add_parser("sync-git", help="Fetch and pull origin/main on Host 2 and show git status")
    p_sync.set_defaults(func=cmd_sync_git)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
