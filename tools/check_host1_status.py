#!/usr/bin/env python3
import os, sys, time, socket, subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.nas_docker_mcp import _resolve_nas_config
except ImportError:
    from nas_docker_mcp import _resolve_nas_config

PT = ZoneInfo('America/Los_Angeles')
_h1, _, _port = _resolve_nas_config()
HOST = _h1
SSH_PORT = int(_port)
SSH_KEY = os.environ.get('NAS_SSH_KEY', '/secrets/id_ed25519' if os.path.exists('/secrets/id_ed25519') else '/root/.ssh/id_ed25519')
SSH_USER = os.environ.get('NAS_SSH_USER', 'Brock')

PORTS = {
    22: 'SSH',
    5000: 'DSM HTTP',
    5001: 'DSM HTTPS',
    8123: 'Home Assistant',
    32400: 'Plex',
}

def check_port(port: int, timeout: float = 1.0) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((HOST, port))
        s.close()
        return True
    except Exception:
        return False

def get_pt_time() -> str:
    return datetime.now(PT).strftime('%I:%M:%S %p PT')

def check_ssh_cmd(cmd: str, timeout: int = 5) -> tuple[int, str]:
    try:
        res = subprocess.run(
            [
                'ssh', '-i', SSH_KEY, '-p', str(SSH_PORT),
                '-o', 'StrictHostKeyChecking=no',
                '-o', 'ConnectTimeout=4',
                '-o', 'BatchMode=yes',
                f'{SSH_USER}@{HOST}', cmd
            ],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return res.returncode, res.stdout.strip()
    except Exception as e:
        return -1, str(e)

def main():
    print(f"[{get_pt_time()}] Monitoring Host 1 ({HOST}) shutdown & reboot over 5 minutes...")
    start_time = time.time()
    total_duration = 300
    poll_interval = 15

    saw_offline = False
    saw_online_again = False

    while time.time() - start_time < total_duration:
        now_pt = get_pt_time()
        results = {name: check_port(port) for port, name in PORTS.items()}
        open_count = sum(1 for v in results.values() if v)

        if open_count == 0:
            if not saw_offline:
                print(f"[{now_pt}] 🔌 Host 1 is FULLY POWERED OFF. All network ports closed.")
                saw_offline = True
        else:
            if saw_offline and not saw_online_again:
                print(f"[{now_pt}] 🟢 Host 1 is BOOTING BACK UP! Ports detected: {[name for name, status in results.items() if status]}")
                saw_online_again = True
            status_str = ', '.join([f"{name}: {'UP' if st else 'DOWN'}" for name, st in results.items()])
            print(f"[{now_pt}] Probe: {status_str}")

        time.sleep(poll_interval)

    print("\n" + "="*50)
    print(f"[{get_pt_time()}] 5-MINUTE STATUS REPORT FOR HOST 1 (DS423+)")
    print("="*50)

    final_results = {name: check_port(port, timeout=1.5) for port, name in PORTS.items()}
    any_open = any(final_results.values())

    if not any_open:
        print("• Hardware State: POWERED OFF (or in post-stage).")
        print("• Action: If the blue power LED is now dark, press the power button once to boot.")
    else:
        print("• Hardware State: ONLINE / RUNNING.")
        for name, status in final_results.items():
            print(f"  - {name}: {'🟢 Reachable' if status else '🔴 Unreachable'}")

        code, out = check_ssh_cmd("uptime; docker ps --format '{{.Names}}: {{.Status}}'")
        if code == 0:
            print("\n• System Telemetry:")
            lines = out.splitlines()
            if lines:
                print(f"  - Uptime: {lines[0]}")
            containers = [l for l in lines[1:] if l.strip()]
            print(f"  - Active Containers ({len(containers)}):")
            for c in containers[:8]:
                print(f"    • {c}")
        else:
            print(f"\n• SSH Status: {out}")

    print("="*50)

if __name__ == '__main__':
    main()
