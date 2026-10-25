#!/usr/bin/env python3
"""Zero Health Check HTTP Server for external Cloudflare Tunnel & UptimeRobot monitoring.

Exposes a lightweight HTTP JSON status endpoint on port 8769 for zero.brock.ventures.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse
from zoneinfo import ZoneInfo

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

try:
    import tools.zero_dashboard as zero_dashboard
except ImportError:
    try:
        import zero_dashboard
    except ImportError:
        zero_dashboard = None


def _get_zero_dashboard():
    global zero_dashboard
    try:
        if zero_dashboard is not None:
            zero_dashboard = importlib.reload(zero_dashboard)
        else:
            try:
                import tools.zero_dashboard as zero_dashboard
            except ImportError:
                import zero_dashboard
    except Exception:
        pass
    return zero_dashboard

try:
    from tools.nas_config import run_ssh, HOST_2_IP
except ImportError:
    try:
        from nas_config import run_ssh, HOST_2_IP
    except ImportError:
        run_ssh = None
        HOST_2_IP = os.environ.get("NAS_HOST_2_IP", os.environ.get("HOST_2_IP", "127.0.0.1"))

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

ivy_dir = os.environ.get("IVY_AGENT_DIR", os.path.join(os.environ.get("NAS_DOCKER_ROOT", os.path.join(os.sep, "volume1", "docker")), "discord-ivy-agent"))
if not os.path.exists(ivy_dir):
    for candidate in [os.path.join(os.environ.get("NAS_DOCKER_ROOT", os.path.join(os.sep, "volume1", "docker")), "discord-ivy-agent"), "/opt/docker/discord-ivy-agent"]:
        if os.path.exists(candidate):
            ivy_dir = candidate
            break

data_dir = os.path.join(ivy_dir, "workspace", "data")
meta_path = os.path.join(data_dir, "session_meta.json")
if os.path.exists(meta_path):
    try:
        with open(meta_path) as f:
            res["session_meta"] = json.load(f)
    except Exception:
        pass

cfg_path = os.path.join(data_dir, "runtime_config.json")
if os.path.exists(cfg_path):
    try:
        with open(cfg_path) as f:
            res["runtime_config"] = json.load(f)
    except Exception:
        pass

stats_path = os.path.join(data_dir, "bot_stats.json")
if os.path.exists(stats_path):
    try:
        with open(stats_path) as f:
            res["bot_stats"] = json.load(f)
    except Exception:
        pass

in_flight_path = os.path.join(data_dir, "in_flight_turn.json")
if os.path.exists(in_flight_path):
    try:
        with open(in_flight_path) as f:
            res["in_flight"] = json.load(f)
    except Exception:
        pass

detached_path = os.path.join(data_dir, "detached_tasks")
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

beacon_path = os.path.join(data_dir, "liveness_beacon.json")
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
        "gemini-4-argon-high": "Gemini 4 Argon (High)",
        "gemini-4-argon-medium": "Gemini 4 Argon (Medium)",
        "gemini-4-argon-low": "Gemini 4 Argon (Low)",
        "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
        "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
        "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
        "gemini-3.8-flash": "Gemini 3.8 Flash",
        "gemini-3.7-flash-high": "Gemini 3.7 Flash (High)",
        "gemini-3.7-flash": "Gemini 3.7 Flash",
        "gemini-3.7-pro": "Gemini 3.7 Pro",
        "claude-sonnet-5-5-high": "Claude Sonnet 5.5 (High)",
        "claude-sonnet-5-5-medium": "Claude Sonnet 5.5 (Medium)",
        "claude-opus-5-5-high": "Claude Opus 5.5 (High)",
    }
    short_names = {
        "gemini-4-argon-high": "4 Argon",
        "gemini-4-argon-medium": "4 Argon Med",
        "gemini-4-argon-low": "4 Argon Low",
        "gemini-3.8-flash-high": "3.8 Flash",
        "gemini-3.8-flash-medium": "3.8 Flash Med",
        "gemini-3.8-flash-low": "3.8 Flash Low",
        "gemini-3.8-flash": "3.8 Flash",
        "gemini-3.7-flash-high": "3.7 Flash",
        "gemini-3.7-flash": "3.7 Flash",
        "gemini-3.7-pro": "3.7 Pro",
        "claude-sonnet-5-5-high": "Sonnet 5.5",
        "claude-sonnet-5-5-medium": "Sonnet 5.5 Med",
        "claude-opus-5-5-high": "Opus 5.5",
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
            "gemini-4-argon-high": "Gemini 4 Argon (High)",
            "gemini-4-argon-medium": "Gemini 4 Argon (Medium)",
            "gemini-4-argon-low": "Gemini 4 Argon (Low)",
            "gemini-3.8-flash-high": "Gemini 3.8 Flash (High)",
            "gemini-3.8-flash-medium": "Gemini 3.8 Flash (Medium)",
            "gemini-3.8-flash-low": "Gemini 3.8 Flash (Low)",
            "gemini-3.8-flash": "Gemini 3.8 Flash",
            "gemini-3.7-flash-high": "Gemini 3.7 Flash (High)",
            "gemini-3.7-flash": "Gemini 3.7 Flash",
            "gemini-3.7-pro": "Gemini 3.7 Pro",
            "claude-sonnet-5-5-high": "Claude Sonnet 5.5 (High)",
            "claude-sonnet-5-5-medium": "Claude Sonnet 5.5 (Medium)",
            "claude-opus-5-5-high": "Claude Opus 5.5 (High)",
        }
        short_names = {
            "gemini-4-argon-high": "4 Argon",
            "gemini-4-argon-medium": "4 Argon Med",
            "gemini-4-argon-low": "4 Argon Low",
            "gemini-3.8-flash-high": "3.8 Flash",
            "gemini-3.8-flash-medium": "3.8 Flash Med",
            "gemini-3.8-flash-low": "3.8 Flash Low",
            "gemini-3.8-flash": "3.8 Flash",
            "gemini-3.7-flash-high": "3.7 Flash",
            "gemini-3.7-flash": "3.7 Flash",
            "gemini-3.7-pro": "3.7 Pro",
            "claude-sonnet-5-5-high": "Sonnet 5.5",
            "claude-sonnet-5-5-medium": "Sonnet 5.5 Med",
            "claude-opus-5-5-high": "Opus 5.5",
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

    @staticmethod
    def _is_public_landing_host(host_header: str) -> bool:
        clean_host = (host_header or "").split(":")[0].strip().lower()
        return clean_host in ("brock.ventures", "www.brock.ventures")

    @staticmethod
    def _render_public_landing_html() -> str:
        return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="Content-Security-Policy" content="upgrade-insecure-requests" />
  <link rel="icon" type="image/png" sizes="64x64" href="/favicon.png?v=2" />
  <link rel="shortcut icon" type="image/png" href="/favicon.ico?v=2" />
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=2" />
  <title>Brock Ventures · Media & Library Portal</title>
  <style>
    :root {
      --bg: #0b0f19;
      --card: rgba(17, 24, 39, 0.82);
      --card-hover: rgba(30, 41, 59, 0.92);
      --border: rgba(148, 163, 184, 0.16);
      --border-hover: rgba(56, 189, 248, 0.48);
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #38bdf8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      background: radial-gradient(circle at 50% 0%, #172554 0%, var(--bg) 60%);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 2rem 1.25rem;
    }
    .container {
      width: 100%;
      max-width: 780px;
    }
    header {
      text-align: center;
      margin-bottom: 2.5rem;
    }
    .brand-row {
      display: inline-flex;
      align-items: center;
      gap: 0.65rem;
      margin-bottom: 0.95rem;
    }
    .brand-avatar {
      width: 42px;
      height: 42px;
      border-radius: 50%;
      border: 1.5px solid rgba(56, 189, 248, 0.55);
      box-shadow: 0 0 14px rgba(56, 189, 248, 0.28);
      object-fit: cover;
    }
    .badge {
      display: inline-block;
      font-size: 0.78rem;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--accent);
      background: rgba(56, 189, 248, 0.12);
      border: 1px solid rgba(56, 189, 248, 0.25);
      padding: 0.35rem 0.85rem;
      border-radius: 999px;
    }
    h1 {
      font-size: 2.1rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      margin-bottom: 0.5rem;
    }
    p.subtitle {
      color: var(--muted);
      font-size: 1rem;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 1.15rem;
    }
    a.card {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      text-decoration: none;
      color: inherit;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.5rem;
      backdrop-filter: blur(12px);
      transition: transform 0.18s ease, border-color 0.18s ease, background 0.18s ease;
    }
    a.card:hover {
      transform: translateY(-3px);
      background: var(--card-hover);
      border-color: var(--border-hover);
    }
    .icon {
      font-size: 1.9rem;
      margin-bottom: 1rem;
    }
    .card h2 {
      font-size: 1.2rem;
      font-weight: 600;
      margin-bottom: 0.45rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .arrow {
      color: var(--accent);
      font-size: 1.1rem;
      transition: transform 0.18s ease;
    }
    a.card:hover .arrow {
      transform: translateX(3px);
    }
    .card p {
      color: var(--muted);
      font-size: 0.9rem;
      line-height: 1.45;
      margin-bottom: 1.1rem;
    }
    .host-tag {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.76rem;
      color: #cbd5e1;
      background: rgba(15, 23, 42, 0.7);
      padding: 0.3rem 0.55rem;
      border-radius: 6px;
      align-self: flex-start;
    }
    .guide-section {
      margin-top: 2.25rem;
      padding-top: 2rem;
      border-top: 1px solid var(--border);
    }
    .guide-header {
      font-size: 1.05rem;
      font-weight: 600;
      color: var(--text);
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .guide-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1.15rem;
    }
    .guide-box {
      background: rgba(15, 23, 42, 0.65);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1.35rem;
    }
    .guide-box h3 {
      font-size: 1rem;
      font-weight: 600;
      margin-bottom: 0.75rem;
      color: var(--accent);
      display: flex;
      align-items: center;
      gap: 0.45rem;
    }
    .guide-steps {
      list-style: none;
      counter-reset: step-counter;
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
    }
    .guide-steps li {
      counter-increment: step-counter;
      font-size: 0.87rem;
      color: var(--muted);
      line-height: 1.45;
      display: flex;
      align-items: flex-start;
      gap: 0.6rem;
    }
    .guide-steps li::before {
      content: counter(step-counter);
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--accent);
      background: rgba(56, 189, 248, 0.14);
      border: 1px solid rgba(56, 189, 248, 0.3);
      min-width: 20px;
      height: 20px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-top: 0.1rem;
      flex-shrink: 0;
    }
    .guide-steps strong {
      color: var(--text);
      font-weight: 600;
    }
    footer {
      text-align: center;
      margin-top: 2.5rem;
      font-size: 0.8rem;
      color: #64748b;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand-row">
        <img class="brand-avatar" src="/apple-touch-icon.png?v=2" alt="Brock Ventures" />
        <div class="badge">Brock Ventures</div>
      </div>
      <h1>Media &amp; Library Directory</h1>
      <p class="subtitle">Quick links for movies, TV requests, digital books, and automated downloads.</p>
    </header>
    <div class="grid">
      <a class="card" href="https://plexrequests.ryanbrock.org" target="_blank" rel="noopener">
        <div>
          <div class="icon">🎬</div>
          <h2>Seerr <span class="arrow">→</span></h2>
          <p>Request movies and TV series for Plex and track active downloads.</p>
        </div>
        <span class="host-tag">plexrequests.ryanbrock.org</span>
      </a>
      <a class="card" href="https://app.plex.tv" target="_blank" rel="noopener">
        <div>
          <div class="icon">🍿</div>
          <h2>Plex <span class="arrow">→</span></h2>
          <p>Stream movies, shows, and live channels in the Plex web player.</p>
        </div>
        <span class="host-tag">app.plex.tv</span>
      </a>
      <a class="card" href="https://bookrequests.ryanbrock.org" target="_blank" rel="noopener">
        <div>
          <div class="icon">🔖</div>
          <h2>Shelfmark <span class="arrow">→</span></h2>
          <p>Search and grab new books &amp; audiobooks directly into the library.</p>
        </div>
        <span class="host-tag">bookrequests.ryanbrock.org</span>
      </a>
      <a class="card" href="https://books.ryanbrock.org" target="_blank" rel="noopener">
        <div>
          <div class="icon">📚</div>
          <h2>Books Library <span class="arrow">→</span></h2>
          <p>Browse, read in-browser, or send EPUBs &amp; audiobooks to your e-reader via Calibre-Web.</p>
        </div>
        <span class="host-tag">books.ryanbrock.org</span>
      </a>
    </div>
    <section class="guide-section">
      <div class="guide-header">✨ First-Time Setup Guide</div>
      <div class="guide-grid">
        <div class="guide-box">
          <h3>🍿 Getting Connected to Plex &amp; Seerr</h3>
          <ol class="guide-steps">
            <li><span>Create a free account at <strong>plex.tv</strong> (Google, Apple, or email) and let Ryan know the email address you used so he can send a server invite.</span></li>
            <li><span>Click the link in the invite email to accept library access, then sign in to <strong>Plex</strong> on your TV, phone, or browser.</span></li>
            <li><span><em>Tip:</em> In your Plex app sidebar, pin our <strong>Movies</strong> and <strong>TV Shows</strong> libraries (and unpin Plex's free ad-supported rows) so our catalog stays front and center.</span></li>
            <li><span>To request new movies or shows, open <strong>Seerr</strong> above and click <strong>Sign In with Plex</strong> using that same account.</span></li>
          </ol>
        </div>
        <div class="guide-box">
          <h3>📚 Getting Connected to Books &amp; Shelfmark</h3>
          <ol class="guide-steps">
            <li><span>Ping Ryan for a <strong>Books Library</strong> username and temporary password (that single login works on both <strong>Books Library</strong> and <strong>Shelfmark</strong>).</span></li>
            <li><span>Sign in to <strong>Books Library</strong> to read in your browser, download EPUBs/audiobooks, or update your password under your profile.</span></li>
            <li><span><strong>Send to Kindle:</strong> Add your <em>@kindle.com</em> address in your Books Library profile and approve our sender address in your Amazon Content &amp; Devices settings for 1-click wireless delivery.</span></li>
            <li><span>Missing a title? Sign in to <strong>Shelfmark</strong> to search and grab any book or audiobook automatically into the library.</span></li>
          </ol>
        </div>
      </div>
    </section>
    <footer>
      Brock Ventures Portal
    </footer>
  </div>
</body>
</html>"""

    def do_HEAD(self):
        strict = self._parse_strict()
        clean_path = self.path.split("?")[0].rstrip("/")
        host_header = self.headers.get("Host", "").lower()
        if self._is_public_landing_host(host_header):
            clean_host = (host_header or "").split(":")[0].strip().lower()
            if clean_host == "brock.ventures":
                self.send_response(301)
                self.send_header("Location", f"https://www.brock.ventures{self.path or '/'}")
                self.end_headers()
                return
            if clean_path in ("", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            elif clean_path in ("/favicon.ico", "/favicon.png", "/apple-touch-icon.png"):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            return
        if clean_path in ("/api/ivy", "/ivy"):
            code, _ = self._evaluate_ivy(strict=strict)
            content_type = "application/json"
        elif clean_path in ("/api/avatar", "/avatar.png", "/zero_avatar.png"):
            code = 200
            content_type = "image/png"
        elif clean_path in ("/api/telemetry", "/api/context", "/api/sessions"):
            code = 200
            content_type = "application/json"
        elif clean_path in ("/api/health", "/health", "/api/status", "/status"):
            code, _ = self._evaluate_health(strict=strict)
            content_type = "application/json"
        elif clean_path in ("", "/", "/dashboard"):
            accept_header = self.headers.get("Accept", "")
            if "probe." in host_header or "status." in host_header:
                code, _ = self._evaluate_health(strict=strict)
                content_type = "application/json"
            elif (clean_path == "/dashboard" or "text/html" in accept_header) and "format=json" not in self.path and not strict:
                code = 200
                content_type = "text/html; charset=utf-8"
            else:
                code, _ = self._evaluate_health(strict=strict)
                content_type = "application/json"
        else:
            code, _ = self._evaluate_health(strict=strict)
            content_type = "application/json"

        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        strict = self._parse_strict()
        clean_path = self.path.split("?")[0].rstrip("/")
        query_str = self.path.partition("?")[2]
        query_params = urllib.parse.parse_qs(query_str) if query_str else {}
        host_header = self.headers.get("Host", "").lower()
        accept_header = self.headers.get("Accept", "")

        # Public Landing Portal Guard: brock.ventures / www.brock.ventures serves ONLY the static directory portal
        if self._is_public_landing_host(host_header):
            clean_host = (host_header or "").split(":")[0].strip().lower()
            if clean_host == "brock.ventures":
                self.send_response(301)
                self.send_header("Location", f"https://www.brock.ventures{self.path or '/'}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if clean_path in ("", "/"):
                body = self._render_public_landing_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", "upgrade-insecure-requests")
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                self.send_header("Cache-Control", "public, max-age=300")
                self.end_headers()
                self.wfile.write(body)
            elif clean_path in ("/favicon.ico", "/favicon.png", "/apple-touch-icon.png"):
                icon_file = (
                    DATA_DIR / "portal_apple_touch_icon.png"
                    if clean_path == "/apple-touch-icon.png"
                    else DATA_DIR / "portal_favicon.png"
                )
                if not icon_file.exists():
                    icon_file = DATA_DIR / "ryan_avatar.png"
                if icon_file.exists():
                    icon_bytes = icon_file.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(icon_bytes)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(icon_bytes)
                else:
                    self.send_response(204)
                    self.end_headers()
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not Found")
            return

        is_ivy = (
            "ivy." in host_header
            or query_params.get("agent", [""])[0].lower() == "ivy"
            or clean_path.startswith("/ivy")
            or clean_path == "/api/ivy"
        )
        agent = "ivy" if is_ivy else "zero"

        # 1. Ivy Health Endpoint (explicit)
        if clean_path in ("/api/ivy", "/ivy") and "text/html" not in accept_header:
            status_code, payload = self._evaluate_ivy(strict=strict)
            body = json.dumps(payload, indent=2).encode("utf-8")
            content_type = "application/json"

        # Avatar Endpoint
        elif clean_path in ("/api/avatar", "/avatar.png", "/zero_avatar.png", "/ivy_avatar.png"):
            if agent == "ivy" or clean_path == "/ivy_avatar.png":
                avatar_path = Path("/workspace/data/ivy_avatar.png")
            else:
                avatar_path = Path("/workspace/data/zero_avatar.png")
                if not avatar_path.exists():
                    avatar_path = Path("/workspace/assets/zero_avatar.png")

            if avatar_path.exists():
                try:
                    with open(avatar_path, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                except Exception:
                    pass
            self.send_response(404)
            self.end_headers()
            return

        # 2. Telemetry & Context API
        elif clean_path in ("/api/telemetry", "/api/context"):
            session_id = query_params.get("session", [None])[0]
            channel_name = query_params.get("channel", [None])[0]
            compacted_id = query_params.get("compacted_id", [None])[0]
            target = channel_name or session_id
            zd = _get_zero_dashboard()
            if zd:
                payload = zd.get_telemetry_payload(target=target, compacted_id=compacted_id, agent=agent)
            else:
                payload = {"error": "zero_dashboard module unavailable"}
            body = json.dumps(payload, indent=2).encode("utf-8")
            content_type = "application/json"
            status_code = 200

        # 3. Available Sessions List API
        elif clean_path in ("/api/sessions",):
            zd = _get_zero_dashboard()
            if zd:
                payload = {"sessions": zd.get_sessions_list(agent=agent)}
            else:
                payload = {"sessions": []}
            body = json.dumps(payload, indent=2).encode("utf-8")
            content_type = "application/json"
            status_code = 200

        # 3b. Shared Hybrid Search & Embedding Proxy (eliminates duplicate 250MB+ ONNX loads in CLI tools)
        elif clean_path in ("/api/search",):
            q = query_params.get("q", [""])[0]
            target = query_params.get("target", ["all"])[0]
            mode = query_params.get("mode", ["hybrid"])[0]
            try:
                limit = int(query_params.get("limit", ["10"])[0])
            except ValueError:
                limit = 10
            try:
                rrf_k = int(query_params.get("rrf_k", ["60"])[0])
            except ValueError:
                rrf_k = 60
            try:
                from tools.transcript_index import TranscriptIndexer
                indexer = TranscriptIndexer(use_proxy=False)
                try:
                    payload = indexer.search(q, target=target, limit=limit, mode=mode, rrf_k=rrf_k)
                finally:
                    indexer.close()
                status_code = 200
            except Exception as e:
                payload = {"error": str(e)}
                status_code = 500
            body = json.dumps(payload, indent=2).encode("utf-8")
            content_type = "application/json"

        elif clean_path in ("/api/embed",):
            q = query_params.get("q", [""])[0]
            try:
                import numpy as np
                from tools.transcript_index import get_embedding_model
                model = get_embedding_model()
                if model is None:
                    payload = {"error": "FastEmbed model unavailable"}
                    status_code = 503
                else:
                    q_emb = list(model.embed([q]))[0]
                    emb_b64 = base64.b64encode(np.array(q_emb, dtype=np.float32).tobytes()).decode("ascii")
                    payload = {"embedding_b64": emb_b64}
                    status_code = 200
            except Exception as e:
                payload = {"error": str(e)}
                status_code = 500
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"

        elif clean_path in ("/api/gif_rank",):
            q = query_params.get("q", [""])[0]
            try:
                import numpy as np
                from tools.transcript_index import get_embedding_model
                from tools.gif_tool import load_canonical_registry
                model = get_embedding_model()
                reg = load_canonical_registry()
                if model is None or not reg:
                    payload = {"error": "Model or canonical registry unavailable"}
                    status_code = 503
                else:
                    global _GIF_MATRIX_CACHE
                    if "_GIF_MATRIX_CACHE" not in globals() or _GIF_MATRIX_CACHE.get("count") != len(reg):
                        texts = [
                            f"{g.get('title', '')} | {g.get('character', '')} | {g.get('franchise', '')} | {g.get('situation', '')} | {' '.join(g.get('vibes', []))} | {' '.join(g.get('tags', []))}"
                            for g in reg
                        ]
                        embs = np.array(list(model.embed(texts)), dtype=np.float32)
                        norms = np.linalg.norm(embs, axis=1, keepdims=True)
                        norms[norms == 0] = 1.0
                        _GIF_MATRIX_CACHE = {
                            "count": len(reg),
                            "ids": [g.get("id", "") for g in reg],
                            "matrix": embs / norms,
                        }
                    q_vec = np.array(list(model.embed([q]))[0], dtype=np.float32)
                    q_norm = float(np.linalg.norm(q_vec))
                    if q_norm > 0:
                        q_vec = q_vec / q_norm
                    sims = np.dot(_GIF_MATRIX_CACHE["matrix"], q_vec)
                    scores = {
                        gid: round(float(sim), 4)
                        for gid, sim in zip(_GIF_MATRIX_CACHE["ids"], sims)
                        if gid
                    }
                    payload = {"scores": scores}
                    status_code = 200
            except Exception as e:
                payload = {"error": str(e)}
                status_code = 500
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"

        # 4. Health JSON Endpoint
        elif clean_path in ("/api/health", "/health", "/api/status", "/status"):
            if agent == "ivy":
                status_code, payload = self._evaluate_ivy(strict=strict)
            else:
                status_code, payload = self._evaluate_health(strict=strict)
            body = json.dumps(payload, indent=2).encode("utf-8")
            content_type = "application/json"

        # 5. Root / Dashboard Endpoint
        elif clean_path in ("", "/", "/dashboard", "/ivy"):
            if "probe." in host_header or "status." in host_header:
                # Dedicated probe host: always return JSON health status without dashboard UI leakage
                if agent == "ivy":
                    status_code, payload = self._evaluate_ivy(strict=strict)
                else:
                    status_code, payload = self._evaluate_health(strict=strict)
                body = json.dumps(payload, indent=2).encode("utf-8")
                content_type = "application/json"
            elif (clean_path in ("/dashboard", "/ivy") or "text/html" in accept_header) and "format=json" not in query_str and not strict:
                # Browser requesting visual dashboard UI
                zd = _get_zero_dashboard()
                if zd:
                    html_content = zd.render_dashboard_html(agent=agent)
                    body = html_content.encode("utf-8")
                    content_type = "text/html; charset=utf-8"
                    status_code = 200
                else:
                    if agent == "ivy":
                        status_code, payload = self._evaluate_ivy(strict=strict)
                    else:
                        status_code, payload = self._evaluate_health(strict=strict)
                    body = json.dumps(payload, indent=2).encode("utf-8")
                    content_type = "application/json"
            else:
                # Default for root API consumers (Homepage customapi widget, curl, monitoring probes)
                if agent == "ivy":
                    status_code, payload = self._evaluate_ivy(strict=strict)
                else:
                    status_code, payload = self._evaluate_health(strict=strict)
                body = json.dumps(payload, indent=2).encode("utf-8")
                content_type = "application/json"

        # 6. Fallback
        else:
            if agent == "ivy":
                status_code, payload = self._evaluate_ivy(strict=strict)
            else:
                status_code, payload = self._evaluate_health(strict=strict)
            body = json.dumps(payload, indent=2).encode("utf-8")
            content_type = "application/json"

        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
