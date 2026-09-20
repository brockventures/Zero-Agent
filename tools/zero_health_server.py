#!/usr/bin/env python3
"""Zero Health Check HTTP Server for external Cloudflare Tunnel & UptimeRobot monitoring.

Exposes a lightweight HTTP JSON status endpoint on port 8769 for zero.brock.ventures.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

PT_TZ = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

PID_FILE = DATA_DIR / "zero_health_server.pid"
LOG_FILE = DATA_DIR / "zero_health_server.log"
PORT = int(os.environ.get("ZERO_HEALTH_PORT", 8769))
BOOT_TIME = time.time()


class ZeroHealthHandler(BaseHTTPRequestHandler):
    def _evaluate_health(self) -> tuple[int, dict]:
        now_ts = time.time()
        now_pt = datetime.now(PT_TZ)
        uptime = int(now_ts - BOOT_TIME)

        # 1. Read dynamic Discord gateway liveness & heartbeat from liveness_beacon.json
        beacon_file = DATA_DIR / "liveness_beacon.json"
        gateway_healthy = False
        gateway_heartbeat_age = None
        gateway_status = "disconnected"
        gateway_latency_ms = None
        beacon_state = "IDLE"

        if beacon_file.exists():
            try:
                with open(beacon_file) as f:
                    bdata = json.load(f)
                    gh = bdata.get("gateway_heartbeat")
                    beacon_state = bdata.get("state", "IDLE")
                    gateway_status = bdata.get("gateway_status", "disconnected")
                    gateway_latency_ms = bdata.get("gateway_latency_ms")
                    if isinstance(gh, (int, float)) and gh > 0:
                        gateway_heartbeat_age = int(now_ts - gh)
                        if gateway_heartbeat_age <= 180 and gateway_status == "connected":
                            gateway_healthy = True
            except Exception:
                pass

        # 2. Read bot presence status
        bot_status_file = DATA_DIR / "bot_status.json"
        bot_state = "online" if gateway_healthy else "degraded"
        bot_activity = "Zero is online and ready."
        if bot_status_file.exists():
            try:
                with open(bot_status_file) as f:
                    d = json.load(f)
                    bot_state = d.get("status", bot_state)
                    bot_activity = d.get("activity_text", bot_activity)
            except Exception:
                pass

        # 3. Check MCP daemon liveness
        mcp_pid_file = DATA_DIR / "mcp_daemon.pid"
        mcp_status = "offline"
        if mcp_pid_file.exists():
            try:
                pid = int(mcp_pid_file.read_text().strip())
                os.kill(pid, 0)
                mcp_status = "running"
            except Exception:
                mcp_status = "stale_pid"

        # 4. Check Mail listener liveness
        mail_pid_file = DATA_DIR / "zero_mail_listener.pid"
        mail_status = "offline"
        if mail_pid_file.exists():
            try:
                pid = int(mail_pid_file.read_text().strip())
                os.kill(pid, 0)
                mail_status = "running"
            except Exception:
                mail_status = "stale_pid"

        is_overall_healthy = gateway_healthy

        payload = {
            "status": "healthy" if is_overall_healthy else "degraded",
            "service": "zero-health",
            "agent": "Zero",
            "host": os.environ.get("ZERO_HOST_NAME", "Host2"),
            "timestamp_pt": now_pt.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "uptime_seconds": uptime,
            "components": {
                "discord_gateway": "connected" if gateway_healthy else "stalled_or_disconnected",
                "gateway_status": gateway_status,
                "gateway_heartbeat_age_seconds": gateway_heartbeat_age,
                "gateway_latency_ms": gateway_latency_ms,
                "turn_state": beacon_state,
                "discord_bot": bot_state,
                "discord_activity": bot_activity,
                "mcp_daemon": mcp_status,
                "mail_listener": mail_status,
                "bridge_scheduler": "active"
            }
        }
        status_code = 200 if is_overall_healthy else 503
        return status_code, payload

    def do_HEAD(self):
        code, _ = self._evaluate_health()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def do_GET(self):
        status_code, payload = self._evaluate_health()
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if os.environ.get("DEBUG"):
            super().log_message(format, *args)


def run_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ZeroHealthHandler)
    PID_FILE.write_text(str(os.getpid()))
    try:
        server.serve_forever()
    finally:
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except Exception:
                pass


def is_running() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        try:
            PID_FILE.unlink()
        except Exception:
            pass
        return None


def start_daemon():
    pid = is_running()
    if pid:
        return {"ok": True, "status": "already_running", "pid": pid, "port": PORT}

    with open(LOG_FILE, "a") as log_out:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--foreground"],
            stdout=log_out,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

    time.sleep(0.5)
    pid = is_running()
    return {"ok": True, "status": "started", "pid": pid or proc.pid, "port": PORT}


def stop_daemon():
    pid = is_running()
    if not pid:
        return {"ok": True, "status": "not_running"}
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:
            pass
    return {"ok": True, "status": "stopped", "pid": pid}


def ensure_health_server_running():
    if not is_running():
        start_daemon()


def get_status() -> dict:
    pid = is_running()
    return {
        "ok": True,
        "running": pid is not None,
        "pid": pid,
        "port": PORT,
        "url": f"http://127.0.0.1:{PORT}/"
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--foreground":
        run_server()
    elif len(sys.argv) > 1 and sys.argv[1] == "stop":
        print(json.dumps(stop_daemon(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(get_status(), indent=2))
    else:
        print(json.dumps(start_daemon(), indent=2))
