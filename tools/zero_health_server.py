#!/usr/bin/env python3
"""Zero Health Check HTTP Server for external Cloudflare Tunnel & UptimeRobot monitoring.

Exposes a lightweight HTTP JSON status endpoint on port 8769 for zero.brock.ventures.
"""

from __future__ import annotations

import base64
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

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

try:
    from tools.nas_config import run_ssh, HOST_2_IP
except ImportError:
    try:
        from nas_config import run_ssh, HOST_2_IP
    except ImportError:
        run_ssh = None
        HOST_2_IP = os.environ.get("HOST_2_IP", "127.0.0.1")

PT_TZ = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

PID_FILE = DATA_DIR / "zero_health_server.pid"
LOG_FILE = DATA_DIR / "zero_health_server.log"
PORT = int(os.environ.get("ZERO_HEALTH_PORT", 8769))
BOOT_TIME = time.time()

IVY_CACHE: dict = {"ts": 0.0, "status_code": 200, "payload": None}
IVY_CACHE_TTL_SECONDS = 5.0

IVY_PROBE_CODE = """
import json, os, subprocess, time

res = {}
try:
    c = subprocess.check_output(["docker", "inspect", "-f", "{{.State.Status}}", "discord-ivy-agent"], timeout=3).decode().strip()
    res["container_status"] = c
except Exception as e:
    res["container_status"] = "offline"

meta_path = "os.environ.get("IVY_AGENT_DIR", "/opt/docker/discord-ivy-agent")/workspace/data/session_meta.json"
if os.path.exists(meta_path):
    try:
        with open(meta_path) as f:
            res["session_meta"] = json.load(f)
    except Exception:
        pass

cfg_path = "os.environ.get("IVY_AGENT_DIR", "/opt/docker/discord-ivy-agent")/workspace/data/runtime_config.json"
if os.path.exists(cfg_path):
    try:
        with open(cfg_path) as f:
            res["runtime_config"] = json.load(f)
    except Exception:
        pass

stats_path = "os.environ.get("IVY_AGENT_DIR", "/opt/docker/discord-ivy-agent")/workspace/data/bot_stats.json"
if os.path.exists(stats_path):
    try:
        with open(stats_path) as f:
            res["bot_stats"] = json.load(f)
    except Exception:
        pass

in_flight_path = "os.environ.get("IVY_AGENT_DIR", "/opt/docker/discord-ivy-agent")/workspace/data/in_flight_turn.json"
if os.path.exists(in_flight_path):
    try:
        with open(in_flight_path) as f:
            res["in_flight"] = json.load(f)
    except Exception:
        pass

detached_path = "os.environ.get("IVY_AGENT_DIR", "/opt/docker/discord-ivy-agent")/workspace/data/detached_tasks"
detached_count = 0
if os.path.exists(detached_path):
    try:
        for entry in os.scandir(detached_path):
            if entry.is_dir():
                mf = os.path.join(entry.path, "meta.json")
                if os.path.exists(mf):
                    with open(mf) as f:
                        m = json.load(f)
                        if m.get("status") == "running":
                            detached_count += 1
    except Exception:
        pass
res["active_detached_tasks"] = detached_count

beacon_path = "os.environ.get("IVY_AGENT_DIR", "/opt/docker/discord-ivy-agent")/workspace/data/liveness_beacon.json"
if os.path.exists(beacon_path):
    try:
        with open(beacon_path) as f:
            res["beacon"] = json.load(f)
    except Exception:
        pass

try:
    p = subprocess.check_output(["docker", "top", "discord-ivy-agent"], timeout=3).decode()
    res["daemon_running"] = "bridge.py" in p or "ivy_daemon.py" in p
except Exception:
    res["daemon_running"] = False

print(json.dumps(res))
"""


def _evaluate_ivy(force: bool = False, strict: bool = False) -> tuple[int, dict]:
    global IVY_CACHE
    now_ts = time.time()
    now_pt = datetime.now(PT_TZ)

    if not force and IVY_CACHE["payload"] is not None and (now_ts - IVY_CACHE["ts"]) < IVY_CACHE_TTL_SECONDS:
        cached_payload = IVY_CACHE["payload"]
        code = (200 if cached_payload.get("status") == "healthy" else 503) if strict else 200
        return code, cached_payload

    if run_ssh is None:
        status_code = 503 if strict else 200
        payload = {
            "status": "degraded",
            "service": "ivy-health",
            "agent": "Ivy",
            "host": os.environ.get("ZERO_HOST_NAME", "Host2"),
            "error": "nas_config.run_ssh unavailable",
            "container_status": "unreachable",
            "turn_state": "OFFLINE",
            "active_model": "Unknown",
            "model_short": "Unknown",
            "in_flight_turns": 0,
            "messages_sent": 993,
            "session_turns": 0,
            "daemon_running": False,
            "timestamp_pt": now_pt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }
        IVY_CACHE = {"ts": now_ts, "status_code": status_code, "payload": payload}
        return status_code, payload

    b64 = base64.b64encode(IVY_PROBE_CODE.strip().encode("utf-8")).decode("ascii")
    cmd = f"python3 -c \"import base64; exec(base64.b64decode('{b64}'))\""
    code, stdout, stderr = run_ssh(HOST_2_IP, cmd, timeout=4)
    if code != 0:
        status_code = 503 if strict else 200
        payload = {
            "status": "degraded",
            "service": "ivy-health",
            "agent": "Ivy",
            "host": os.environ.get("ZERO_HOST_NAME", "Host2"),
            "error": f"SSH execution failed: {stderr.strip()}",
            "container_status": "unreachable",
            "turn_state": "OFFLINE",
            "active_model": "Unknown",
            "model_short": "Unknown",
            "in_flight_turns": 0,
            "messages_sent": 993,
            "session_turns": 0,
            "daemon_running": False,
            "timestamp_pt": now_pt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }
        IVY_CACHE = {"ts": now_ts, "status_code": status_code, "payload": payload}
        return status_code, payload

    try:
        data = json.loads(stdout.strip())
    except Exception as e:
        status_code = 503 if strict else 200
        payload = {
            "status": "degraded",
            "service": "ivy-health",
            "agent": "Ivy",
            "host": os.environ.get("ZERO_HOST_NAME", "Host2"),
            "error": f"Invalid JSON from probe: {e}",
            "container_status": "unknown",
            "turn_state": "OFFLINE",
            "active_model": "Unknown",
            "model_short": "Unknown",
            "in_flight_turns": 0,
            "messages_sent": 993,
            "session_turns": 0,
            "daemon_running": False,
            "timestamp_pt": now_pt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        }
        IVY_CACHE = {"ts": now_ts, "status_code": status_code, "payload": payload}
        return status_code, payload

    c_status = data.get("container_status", "unknown")
    is_running = (c_status == "running")
    daemon_running = bool(data.get("daemon_running", False))
    raw_model = data.get("runtime_config", {}).get("model", "gemini-3.8-flash-high")
    session_turns = data.get("session_meta", {}).get("turns", 0)

    # Bot stats (All-time messages sent)
    messages_sent = 993
    if "bot_stats" in data and isinstance(data["bot_stats"], dict):
        messages_sent = data["bot_stats"].get("all_time_messages_sent", 993)

    # In-flight turns & background tasks
    in_flight_turns = 0
    in_flight_data = data.get("in_flight", {})
    if isinstance(in_flight_data, dict):
        for k, v in in_flight_data.items():
            if isinstance(v, dict) and "prompt" in v and "ts" in v:
                ts = v.get("ts", 0)
                if (now_ts - ts) < 1800:
                    in_flight_turns += 1

    active_detached = data.get("active_detached_tasks", 0)
    total_ivy_in_flight = in_flight_turns + active_detached

    model_display_names = {
        "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
        "gemini-3.8-flash": "Gemini 3.8 Flash",
        "gemini-3.7-flash-high": "Gemini 3.7 Flash (High)",
        "gemini-3.7-flash": "Gemini 3.7 Flash",
        "gemini-3.7-pro": "Gemini 3.7 Pro",
    }
    short_names = {
        "gemini-3.8-flash-high": "3.8 Flash",
        "gemini-3.8-flash": "3.8 Flash",
        "gemini-3.7-flash-high": "3.7 Flash",
        "gemini-3.7-flash": "3.7 Flash",
        "gemini-3.7-pro": "3.7 Pro",
    }

    model_display = model_display_names.get(raw_model, raw_model)
    model_short = short_names.get(raw_model, raw_model.replace("gemini-", "").replace("-high", ""))

    if is_running and daemon_running:
        if total_ivy_in_flight > 0:
            turn_state = "PROCESSING"
        elif "beacon" in data and isinstance(data["beacon"], dict) and data["beacon"].get("state"):
            turn_state = data["beacon"]["state"]
        else:
            turn_state = "IDLE"
        status = "healthy"
        status_code = 200
    elif is_running:
        turn_state = "STARTING"
        status = "degraded"
        status_code = 503 if strict else 200
    else:
        turn_state = "OFFLINE"
        status = "degraded"
        status_code = 503 if strict else 200

    payload = {
        "status": status,
        "service": "ivy-health",
        "agent": "Ivy",
        "host": os.environ.get("ZERO_HOST_NAME", "Host2"),
        "timestamp_pt": now_pt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "turn_state": turn_state,
        "container_status": c_status,
        "active_model": model_display,
        "model_short": model_short,
        "in_flight_turns": total_ivy_in_flight,
        "active_turns": in_flight_turns,
        "active_detached_tasks": active_detached,
        "messages_sent": messages_sent,
        "session_turns": session_turns,
        "daemon_running": daemon_running,
        "channel": "#baseball",
        "channel_id": 1548196929308065893,
    }
    IVY_CACHE = {"ts": now_ts, "status_code": status_code, "payload": payload}
    return status_code, payload


class ZeroHealthHandler(BaseHTTPRequestHandler):
    def _evaluate_health(self, strict: bool = False) -> tuple[int, dict]:
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

        # 5. Check in-flight active turns
        active_in_flight_turns = []
        in_flight_file = DATA_DIR / "in_flight_turn.json"
        if in_flight_file.exists():
            try:
                with open(in_flight_file, "r", encoding="utf-8") as iff:
                    if_data = json.load(iff)
                    if isinstance(if_data, dict):
                        for k, v in if_data.items():
                            if isinstance(v, dict) and "prompt" in v and "ts" in v:
                                pid = v.get("pid")
                                ts = v.get("ts", 0)
                                is_alive = False
                                if pid and pid > 0:
                                    try:
                                        os.kill(pid, 0)
                                        is_alive = True
                                    except (OSError, ProcessLookupError):
                                        pass
                                if is_alive and (now_ts - ts) < 1800:
                                    active_in_flight_turns.append({
                                        "channel_id": v.get("channel_id", k),
                                        "prompt": str(v.get("prompt", ""))[:80],
                                        "elapsed_seconds": int(now_ts - ts),
                                        "pid": pid,
                                    })
            except Exception:
                pass

        # 6. Check running detached tasks
        running_detached = []
        detached_dir = DATA_DIR / "detached_tasks"
        if detached_dir.exists():
            try:
                for entry in detached_dir.iterdir():
                    if entry.is_dir():
                        meta_file = entry / "meta.json"
                        if meta_file.exists():
                            try:
                                with open(meta_file, "r", encoding="utf-8") as mf:
                                    m = json.load(mf)
                                    if m.get("status") == "running":
                                        cpid = m.get("child_pid")
                                        wpid = m.get("worker_pid")
                                        is_alive = False
                                        for p in (cpid, wpid):
                                            if p and p > 0:
                                                try:
                                                    os.kill(p, 0)
                                                    is_alive = True
                                                    break
                                                except (OSError, ProcessLookupError):
                                                    pass
                                        if is_alive:
                                            running_detached.append({
                                                "task_id": m.get("task_id", entry.name),
                                                "name": m.get("name", ""),
                                                "channel": m.get("channel", ""),
                                                "elapsed_seconds": int(now_ts - m.get("start_time", now_ts)),
                                                "pid": cpid or wpid,
                                            })
                            except Exception:
                                pass
            except Exception:
                pass

        # 7. Check persistent daemon workers
        live_daemons = 0
        daemon_pids_file = DATA_DIR / "daemon_pids.json"
        if daemon_pids_file.exists():
            try:
                with open(daemon_pids_file, "r", encoding="utf-8") as dpf:
                    dp_data = json.load(dpf)
                    if isinstance(dp_data, dict):
                        for p in dp_data.get("pids", []):
                            if isinstance(p, int) and p > 0:
                                try:
                                    os.kill(p, 0)
                                    live_daemons += 1
                                except (OSError, ProcessLookupError):
                                    pass
            except Exception:
                pass

        # 8. Active AI model
        active_model_raw = os.environ.get("AGY_ACTIVE_MODEL", "gemini-3.8-flash-high")
        rules_file = Path("/workspace/config/runtime_rules.json")
        if rules_file.exists():
            try:
                with open(rules_file, "r", encoding="utf-8") as rf:
                    r_data = json.load(rf)
                    if r_data.get("active_model"):
                        active_model_raw = r_data["active_model"]
            except Exception:
                pass

        model_display_names = {
            "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
            "gemini-3.8-flash": "Gemini 3.8 Flash",
            "gemini-3.7-flash-high": "Gemini 3.7 Flash (High)",
            "gemini-3.7-flash": "Gemini 3.7 Flash",
            "gemini-3.7-pro": "Gemini 3.7 Pro",
        }
        short_names = {
            "gemini-3.8-flash-high": "3.8 Flash",
            "gemini-3.8-flash": "3.8 Flash",
            "gemini-3.7-flash-high": "3.7 Flash",
            "gemini-3.7-flash": "3.7 Flash",
            "gemini-3.7-pro": "3.7 Pro",
        }
        active_model_display = model_display_names.get(active_model_raw, active_model_raw)
        model_short = short_names.get(active_model_raw, active_model_raw.replace("gemini-", "").replace("-high", ""))

        # 9. Bot stats (All-time message volume)
        messages_sent = 5026
        bot_stats_file = DATA_DIR / "bot_stats.json"
        if bot_stats_file.exists():
            try:
                with open(bot_stats_file, "r", encoding="utf-8") as bsf:
                    s_data = json.load(bsf)
                    messages_sent = s_data.get("all_time_messages_sent", 5026)
            except Exception:
                pass

        # 10. Format human uptime
        hours = uptime // 3600
        minutes = (uptime % 3600) // 60
        uptime_human = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

        is_overall_healthy = gateway_healthy

        # Determine dynamic turn state
        if gateway_healthy:
            if len(active_in_flight_turns) > 0 or len(running_detached) > 0:
                turn_state = "PROCESSING"
            elif beacon_state in ("IDLE", "PROCESSING"):
                turn_state = beacon_state
            else:
                turn_state = "IDLE"
        else:
            turn_state = "OFFLINE"

        total_in_flight = len(active_in_flight_turns) + len(running_detached)

        payload = {
            "status": "healthy" if is_overall_healthy else "degraded",
            "service": "zero-health",
            "agent": "Zero",
            "host": os.environ.get("ZERO_HOST_NAME", "Host2"),
            "timestamp_pt": now_pt.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "uptime_seconds": uptime,
            "uptime_human": uptime_human,
            "turn_state": turn_state,
            "bot_status": bot_state,
            "active_model": active_model_display,
            "model_short": model_short,
            "messages_sent": messages_sent,
            "in_flight_turns": total_in_flight,
            "active_turns": len(active_in_flight_turns),
            "active_detached_tasks": len(running_detached),
            "daemon_workers": live_daemons,
            "components": {
                "discord_gateway": "connected" if gateway_healthy else "stalled_or_disconnected",
                "gateway_status": gateway_status,
                "gateway_heartbeat_age_seconds": gateway_heartbeat_age,
                "gateway_latency_ms": gateway_latency_ms,
                "turn_state": turn_state,
                "discord_bot": bot_state,
                "discord_activity": bot_activity,
                "mcp_daemon": mcp_status,
                "mail_listener": mail_status,
                "bridge_scheduler": "active",
                "daemon_workers": live_daemons,
                "active_model": active_model_display,
                "model_short": model_short,
                "messages_sent": messages_sent,
            },
            "in_flight": active_in_flight_turns,
            "detached_tasks": running_detached,
        }
        status_code = (200 if is_overall_healthy else 503) if strict else 200
        return status_code, payload

    def _parse_strict(self) -> bool:
        _, _, query = self.path.partition("?")
        return "strict=1" in query or "probe=1" in query

    def _evaluate_ivy(self, force: bool = False, strict: bool = False) -> tuple[int, dict]:
        return _evaluate_ivy(force=force, strict=strict)

    def do_HEAD(self):
        strict = self._parse_strict()
        clean_path = self.path.split("?")[0].rstrip("/")
        if clean_path in ("/api/ivy", "/ivy"):
            code, _ = self._evaluate_ivy(strict=strict)
        else:
            code, _ = self._evaluate_health(strict=strict)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def do_GET(self):
        strict = self._parse_strict()
        clean_path = self.path.split("?")[0].rstrip("/")
        if clean_path in ("/api/ivy", "/ivy"):
            status_code, payload = self._evaluate_ivy(strict=strict)
        else:
            status_code, payload = self._evaluate_health(strict=strict)
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
