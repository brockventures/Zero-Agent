#!/usr/bin/env python3
"""MCP Persistent Daemon Manager for Zero.

Runs Google Workspace, Home Assistant, and NAS Docker MCP servers as persistent
local HTTP/SSE daemons on localhost ports:
  - 8765: Google Workspace (Gmail, Calendar)
  - 8766: Home Assistant
  - 8767: NAS Docker & SSH

Eliminates ~1.5s Python process startup latency and zombie process creation on every turn.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Ensure /workspace and /workspace/tools are on sys.path
WORKSPACE_DIR = Path("/workspace")
TOOLS_DIR = WORKSPACE_DIR / "tools"
DATA_DIR = WORKSPACE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

PID_FILE = DATA_DIR / "mcp_daemon.pid"
LOG_FILE = DATA_DIR / "mcp_daemon.log"
CONFIG_FILE = Path("/root/.gemini/config/mcp_config.json")

SERVERS_CONFIG = {
    "google-workspace": {
        "port": 8765,
        "module": "workspace_mcp",
        "description": "Google Workspace (Gmail & Calendar)",
    },
    "home-assistant": {
        "port": 8766,
        "module": "ha_mcp",
        "description": "Home Assistant IoT Integration",
    },
    "nas-docker": {
        "port": 8767,
        "module": "nas_docker_mcp",
        "description": "Multi-Host NAS Docker & SSH",
    },
    "amazon-search": {
        "port": 8768,
        "module": "amazon_serpapi",
        "description": "Amazon Search & Product Intelligence",
    },
}

def sync_mcp_config_json() -> bool:
    """Ensure /root/.gemini/config/mcp_config.json points to persistent SSE daemon URLs."""
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        mcp_servers = {}
        for name, cfg in SERVERS_CONFIG.items():
            mcp_servers[name] = {
                "disabled": False,
                "serverUrl": f"http://127.0.0.1:{cfg['port']}/sse"
            }
        
        target_content = {"mcpServers": mcp_servers}
        
        # Only write if different
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    curr = json.load(f)
                if curr == target_content:
                    return True
            except Exception:
                pass

        with open(CONFIG_FILE, "w") as f:
            json.dump(target_content, f, indent=2)
            f.write("\n")
        return True
    except Exception as e:
        print(f"[MCP Daemon] Warning: Failed to sync {CONFIG_FILE}: {e}", file=sys.stderr)
        return False

def check_port_health(port: int, timeout: float = 1.5) -> tuple[bool, str]:
    """Check if an MCP SSE endpoint is responsive."""
    url = f"http://127.0.0.1:{port}/sse"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Zero-MCP-HealthCheck/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if code == 200:
                return True, f"HTTP {code} OK"
            return False, f"HTTP {code}"
    except urllib.error.HTTPError as he:
        return False, f"HTTP {he.code}: {he.reason}"
    except Exception as e:
        # If timeout occurred on reading stream after 200, it's alive because SSE stream stays open
        err_str = str(e).lower()
        if "timed out" in err_str or "timeout" in err_str:
            return True, "SSE Connected (Stream Open)"
        return False, str(e)

def is_process_running(pid: int) -> bool:
    """Check if a process with given PID exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def get_daemon_pid() -> int | None:
    """Read the current daemon PID from PID_FILE if running."""
    if not PID_FILE.exists():
        return None
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        if is_process_running(pid):
            return pid
        else:
            PID_FILE.unlink(missing_ok=True)
            return None
    except Exception:
        PID_FILE.unlink(missing_ok=True)
        return None

async def run_servers_async():
    """Run all MCP servers concurrently inside one asyncio event loop."""
    import uvicorn
    from workspace_mcp import server as ws_server
    from ha_mcp import server as ha_server
    from nas_docker_mcp import server as nas_server
    from amazon_serpapi import server as amazon_server

    app_ws = ws_server.sse_app()
    app_ha = ha_server.sse_app()
    app_nas = nas_server.sse_app()
    app_amazon = amazon_server.sse_app()

    cfg_ws = uvicorn.Config(app_ws, host="127.0.0.1", port=SERVERS_CONFIG["google-workspace"]["port"], log_level="warning")
    cfg_ha = uvicorn.Config(app_ha, host="127.0.0.1", port=SERVERS_CONFIG["home-assistant"]["port"], log_level="warning")
    cfg_nas = uvicorn.Config(app_nas, host="127.0.0.1", port=SERVERS_CONFIG["nas-docker"]["port"], log_level="warning")
    cfg_amazon = uvicorn.Config(app_amazon, host="127.0.0.1", port=SERVERS_CONFIG["amazon-search"]["port"], log_level="warning")

    srv_ws = uvicorn.Server(cfg_ws)
    srv_ha = uvicorn.Server(cfg_ha)
    srv_nas = uvicorn.Server(cfg_nas)
    srv_amazon = uvicorn.Server(cfg_amazon)

    loop = asyncio.get_running_loop()

    def handle_stop():
        print("[MCP Daemon] Received stop signal, shutting down uvicorn servers...", flush=True)
        srv_ws.should_exit = True
        srv_ha.should_exit = True
        srv_nas.should_exit = True
        srv_amazon.should_exit = True
        srv_ws.force_exit = True
        srv_ha.force_exit = True
        srv_nas.force_exit = True
        srv_amazon.force_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, handle_stop)
        except NotImplementedError:
            pass

    print("[MCP Daemon] Starting persistent MCP SSE servers on ports 8765, 8766, 8767, 8768...", flush=True)
    await asyncio.gather(
        srv_ws.serve(),
        srv_ha.serve(),
        srv_nas.serve(),
        srv_amazon.serve()
    )
    print("[MCP Daemon] All servers stopped cleanly.", flush=True)

def run_daemon_foreground():
    """Run the daemon in the foreground."""
    sync_mcp_config_json()
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    try:
        asyncio.run(run_servers_async())
    finally:
        PID_FILE.unlink(missing_ok=True)

def start_daemon_background() -> bool:
    """Start the daemon in background if not already running."""
    pid = get_daemon_pid()
    if pid:
        print(f"[MCP Daemon] Daemon already running (PID {pid}).")
        sync_mcp_config_json()
        return True

    sync_mcp_config_json()
    log_fp = open(LOG_FILE, "a")
    proc = subprocess.Popen(
        [sys.executable, str(TOOLS_DIR / "mcp_daemon.py"), "run"],
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(WORKSPACE_DIR)
    )

    # Wait up to 4 seconds for ports to become responsive
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"[MCP Daemon] ERROR: Process exited prematurely with code {proc.returncode}. See {LOG_FILE}", file=sys.stderr)
            return False
        
        all_ok = True
        for cfg in SERVERS_CONFIG.values():
            ok, _ = check_port_health(cfg["port"], timeout=0.5)
            if not ok:
                all_ok = False
                break
        if all_ok:
            print(f"[MCP Daemon] Successfully started persistent MCP daemon (PID {proc.pid}).")
            return True
        time.sleep(0.2)

    print(f"[MCP Daemon] Started process (PID {proc.pid}), verifying health...")
    return True

def stop_daemon() -> bool:
    """Stop the running daemon process."""
    pid = get_daemon_pid()
    if not pid:
        print("[MCP Daemon] Daemon is not running.")
        return True

    print(f"[MCP Daemon] Stopping daemon (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(25):
            if not is_process_running(pid):
                break
            time.sleep(0.1)
        if is_process_running(pid):
            print(f"[MCP Daemon] Process {pid} did not exit gracefully, sending SIGKILL...")
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
    except Exception as e:
        print(f"[MCP Daemon] Error while stopping PID {pid}: {e}")

    PID_FILE.unlink(missing_ok=True)
    print("[MCP Daemon] Stopped.")
    return True

def get_status() -> dict:
    """Check full status of MCP daemon and all endpoints."""
    pid = get_daemon_pid()
    is_running = pid is not None
    
    server_status = {}
    all_healthy = is_running
    for name, cfg in SERVERS_CONFIG.items():
        ok, msg = check_port_health(cfg["port"])
        if not ok:
            all_healthy = False
        server_status[name] = {
            "port": cfg["port"],
            "url": f"http://127.0.0.1:{cfg['port']}/sse",
            "healthy": ok,
            "status": msg,
            "description": cfg["description"],
        }

    return {
        "running": is_running,
        "pid": pid,
        "healthy": all_healthy,
        "servers": server_status,
        "config_file": str(CONFIG_FILE),
    }

def print_status_summary():
    """Print a clean status report to stdout."""
    status = get_status()
    state_icon = "🟢" if (status["running"] and status["healthy"]) else ("🟡" if status["running"] else "🔴")
    print(f"{state_icon} **MCP Daemon Status**: {'RUNNING' if status['running'] else 'STOPPED'}")
    if status["pid"]:
        print(f"• **PID**: `{status['pid']}`")
    print(f"• **Config**: `{status['config_file']}`")
    print("\n**Endpoints**:")
    for name, s in status["servers"].items():
        icon = "✅" if s["healthy"] else "❌"
        print(f"  {icon} `{name}` (Port {s['port']}): {s['status']} — {s['description']}")

def ensure_mcp_daemon_running() -> bool:
    """Helper for bridge and startup routines to ensure daemon is up."""
    status = get_status()
    if status["running"] and status["healthy"]:
        sync_mcp_config_json()
        return True
    return start_daemon_background()

if __name__ == "__main__":
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if cmd == "run":
        run_daemon_foreground()
    elif cmd == "start":
        ok = start_daemon_background()
        sys.exit(0 if ok else 1)
    elif cmd == "stop":
        ok = stop_daemon()
        sys.exit(0 if ok else 1)
    elif cmd == "restart":
        stop_daemon()
        time.sleep(0.5)
        ok = start_daemon_background()
        sys.exit(0 if ok else 1)
    elif cmd == "status":
        print_status_summary()
    elif cmd == "json":
        print(json.dumps(get_status(), indent=2))
    elif cmd == "sync-config":
        ok = sync_mcp_config_json()
        print(f"MCP config sync {'succeeded' if ok else 'failed'}.")
    else:
        print(f"Usage: {sys.argv[0]} [run|start|stop|restart|status|json|sync-config]")
        sys.exit(1)
