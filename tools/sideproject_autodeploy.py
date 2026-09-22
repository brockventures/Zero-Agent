#!/usr/bin/env python3
"""
Side-Project Auto-Deploy Daemon (Highball & Outpost)
Automatically tracks and pulls latest changes from origin/main for:
  - mcarmody/highball (port 8001)
  - mcarmody/outpost  (port 8000)
Reloads Uvicorn services when python files change and auto-heals crashed servers.
Allows Amos and Zero to push to GitHub and have the live sites update instantly.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT_TZ = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
PID_FILE = DATA_DIR / "sideproject_autodeploy.pid"
LOG_FILE = DATA_DIR / "sideproject_autodeploy.log"
STATE_FILE = DATA_DIR / "sideproject_autodeploy_state.json"

SERVICES = {
    "highball": {
        "repo_dir": Path("/workspace/scratch/highball"),
        "port": 8001,
        "health_url": "http://127.0.0.1:8001/health",
        "pid_file": Path("/workspace/scratch/highball/server.pid"),
        "log_file": Path("/workspace/scratch/highball/server.log"),
        "start_cmd": [sys.executable, "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8001"],
    },
    "outpost": {
        "repo_dir": Path("/workspace/scratch/outpost"),
        "port": 8000,
        "health_url": "http://127.0.0.1:8000/health",
        "pid_file": Path("/workspace/scratch/outpost/server.pid"),
        "log_file": Path("/workspace/scratch/outpost/server.log"),
        "start_cmd": [sys.executable, "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"],
    },
}


def log(msg: str):
    ts = datetime.now(PT_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_check_ts": 0, "repos": {}}


def save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(state, indent=2))
    temp_file.replace(STATE_FILE)


def is_service_healthy(health_url: str, timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(health_url, headers={"User-Agent": "AutoDeployWatcher/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_service_pid(service_name: str) -> int | None:
    cfg = SERVICES[service_name]
    pid_path = cfg["pid_file"]
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, OSError):
            return None
    return None


def restart_service(service_name: str) -> bool:
    cfg = SERVICES[service_name]
    repo_dir = cfg["repo_dir"]
    pid_file = cfg["pid_file"]
    log_file = cfg["log_file"]

    # Terminate existing process if any
    old_pid = get_service_pid(service_name)
    if old_pid:
        log(f"[{service_name}] Stopping existing process PID {old_pid}...")
        try:
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(15):
                time.sleep(0.2)
                try:
                    os.kill(old_pid, 0)
                except OSError:
                    break
            else:
                os.kill(old_pid, signal.SIGKILL)
        except Exception as e:
            log(f"[{service_name}] Error killing PID {old_pid}: {e}")

    # Launch new process
    log(f"[{service_name}] Starting uvicorn server in {repo_dir} on port {cfg['port']}...")
    try:
        log_f = open(log_file, "a")
        proc = subprocess.Popen(
            cfg["start_cmd"],
            cwd=str(repo_dir),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
        time.sleep(1.5)
        healthy = is_service_healthy(cfg["health_url"])
        log(f"[{service_name}] Started with PID {proc.pid}. Health check: {'PASS' if healthy else 'PENDING'}")
        return True
    except Exception as e:
        log(f"[{service_name}] Failed to start service: {e}")
        return False


def check_and_sync_repo(service_name: str) -> dict:
    cfg = SERVICES[service_name]
    repo_dir = cfg["repo_dir"]
    res = {
        "service": service_name,
        "updated": False,
        "old_commit": None,
        "new_commit": None,
        "commit_message": None,
        "restarted": False,
        "healthy": False,
        "error": None,
    }

    if not repo_dir.exists():
        res["error"] = f"Directory {repo_dir} does not exist"
        return res

    try:
        # Get current local commit
        old_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), text=True
        ).strip()
        res["old_commit"] = old_commit[:7]

        # Fetch remote origin/main
        subprocess.check_call(
            ["git", "fetch", "origin", "main"],
            cwd=str(repo_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )

        remote_commit = subprocess.check_output(
            ["git", "rev-parse", "origin/main"], cwd=str(repo_dir), text=True
        ).strip()

        if old_commit != remote_commit:
            log(f"[{service_name}] New commits detected! {old_commit[:7]} -> {remote_commit[:7]}")
            # Fast-forward pull
            pull_out = subprocess.check_output(
                ["git", "pull", "--ff-only", "origin", "main"],
                cwd=str(repo_dir),
                text=True,
                timeout=15,
            )
            res["updated"] = True
            res["new_commit"] = remote_commit[:7]

            commit_msg = subprocess.check_output(
                ["git", "log", "-1", "--pretty=%B", remote_commit],
                cwd=str(repo_dir),
                text=True,
            ).strip().splitlines()[0]
            res["commit_message"] = commit_msg
            log(f"[{service_name}] Pulled: {commit_msg}")

            # Check which files changed
            diff_files = subprocess.check_output(
                ["git", "diff", "--name-only", old_commit, remote_commit],
                cwd=str(repo_dir),
                text=True,
            ).splitlines()

            py_changed = any(f.endswith(".py") or f in ("pyproject.toml", "requirements.txt") for f in diff_files)
            if py_changed:
                log(f"[{service_name}] Python backend files changed ({len(diff_files)} files). Restarting Uvicorn...")
                restart_service(service_name)
                res["restarted"] = True
            else:
                log(f"[{service_name}] Static/template files changed ({len(diff_files)} files). Dynamic reload active.")
        else:
            # No new commits, but ensure service is alive
            if not is_service_healthy(cfg["health_url"]):
                log(f"[{service_name}] Health check failed while idle. Triggering auto-heal restart...")
                restart_service(service_name)
                res["restarted"] = True

        res["healthy"] = is_service_healthy(cfg["health_url"])
    except subprocess.TimeoutExpired:
        res["error"] = "Git operation timed out"
        log(f"[{service_name}] Git operation timed out")
    except Exception as e:
        res["error"] = str(e)
        log(f"[{service_name}] Error syncing repo: {e}")

    return res


def run_cycle() -> dict:
    state = load_state()
    cycle_results = {}
    for svc in SERVICES:
        res = check_and_sync_repo(svc)
        cycle_results[svc] = res
        state["repos"][svc] = {
            "commit": res.get("new_commit") or res.get("old_commit"),
            "healthy": res.get("healthy", False),
            "last_updated": datetime.now(PT_TZ).isoformat(),
        }
    state["last_check_ts"] = time.time()
    save_state(state)
    return cycle_results


def get_daemon_status() -> dict:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return {"running": True, "pid": pid}
        except (ValueError, OSError):
            return {"running": False, "pid": None}
    return {"running": False, "pid": None}


def start_daemon(interval: int = 30) -> dict:
    st = get_daemon_status()
    if st["running"]:
        return {"ok": True, "status": "already_running", "pid": st["pid"]}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_f = open(LOG_FILE, "a")
    cmd = [sys.executable, str(Path(__file__).resolve()), "_daemon", f"--interval={interval}"]
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True)
    PID_FILE.write_text(str(proc.pid))
    time.sleep(0.5)
    st = get_daemon_status()
    return {"ok": True, "status": "started", "pid": st["pid"] or proc.pid}


def stop_daemon() -> dict:
    st = get_daemon_status()
    if not st["running"]:
        return {"ok": True, "status": "not_running"}
    try:
        os.kill(st["pid"], signal.SIGTERM)
        for _ in range(15):
            time.sleep(0.2)
            try:
                os.kill(st["pid"], 0)
            except OSError:
                break
        else:
            os.kill(st["pid"], signal.SIGKILL)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:
            pass
    return {"ok": True, "status": "stopped", "pid": st["pid"]}


def daemon_loop(interval: int = 30):
    log(f"Auto-Deploy Daemon starting (polling interval: {interval}s)...")

    def _sig_handler(signum, frame):
        log(f"Caught signal {signum}. Exiting cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    while True:
        try:
            run_cycle()
        except Exception as e:
            log(f"Unhandled exception in daemon cycle: {e}")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Side-Project Auto-Deploy Daemon (Highball & Outpost)")
    parser.add_argument("command", choices=["start", "stop", "status", "run-once", "_daemon"], default="status", nargs="?")
    parser.add_argument("--interval", type=int, default=30, help="Polling interval in seconds (default: 30)")
    args = parser.parse_args()

    if args.command == "status":
        st = get_daemon_status()
        state = load_state()
        last_check_str = "Never"
        if state.get("last_check_ts"):
            ago = int(time.time() - state["last_check_ts"])
            last_check_str = f"{ago}s ago"

        print("🚀 Side-Project Auto-Deploy Daemon Status:")
        print(f"  Daemon Running: {st['running']} (PID: {st['pid']})")
        print(f"  Last Check: {last_check_str}")
        for svc, cfg in SERVICES.items():
            pid = get_service_pid(svc)
            healthy = is_service_healthy(cfg["health_url"])
            last_commit = "unknown"
            try:
                last_commit = subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"], cwd=str(cfg["repo_dir"]), text=True
                ).strip()
            except Exception:
                pass
            print(f"  [{svc.upper()}] Port: {cfg['port']} | PID: {pid} | Health: {'ONLINE' if healthy else 'DEGRADED'} | Commit: {last_commit}")

    elif args.command == "run-once":
        results = run_cycle()
        print("Cycle results:")
        for svc, res in results.items():
            print(f"  [{svc}] Updated: {res['updated']} | Restarted: {res['restarted']} | Healthy: {res['healthy']} | Commit: {res.get('new_commit') or res.get('old_commit')} | Error: {res['error']}")

    elif args.command == "start":
        res = start_daemon(interval=args.interval)
        print(f"Start result: {res}")

    elif args.command == "stop":
        res = stop_daemon()
        print(f"Stop result: {res}")

    elif args.command == "_daemon":
        daemon_loop(interval=args.interval)


if __name__ == "__main__":
    main()
