#!/usr/bin/env python3
"""Comprehensive Sidecar Execution Engine for Zero.

Implements all scheduled maintenance and monitoring jobs:
1. Heartbeat Sweep (every 2 hours)
2. Nightly Triage & Agenda Briefing (daily @ 23:30 PT)
3. NAS Log Review (nightly @ 22:00 PT)
4. Plex Transcode Session Cleanup (nightly @ 03:00 PT)
5. Dated Reminders (daily @ 09:00 PT)
6. Used EV9 Listing Monitor (daily capture, Sunday digest @ 08:15 PT)
7. Promotional Email Marketing Sweep (biweekly Sunday @ 22:35 PT)
8. Memory Doctor Audit (weekly Sunday @ 03:00 PT)
9. Dreaming Memory Consolidation (nightly @ 01:45 PT)
10. Low Battery Check (weekly Monday @ 10:00 AM PT)
11. NAS Storage & RAID Check (weekly Wednesday @ 10:00 AM PT)
12. HA Stability Update Check (weekly Friday @ 10:30 AM PT)
13. Dockhand Image Check (weekly Sunday @ 11:00 AM PT)
14. Antigravity CLI Release Check (daily @ 10:00 AM PT)

Unified Execution Wrapper:
- Tracks start, end, duration, exit status ('ok', 'warning', 'error'), and output summaries.
- Durably persists execution history to /workspace/data/sidecar_execution_log.json.
- Maintains per-job latest status in /workspace/data/sidecar_status.json.
- Surfaces recent failures and degraded jobs automatically in the Nightly Briefing.
"""

import email.utils
import html
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("sidecars")

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR.parent))

from ha_mcp import ha_ping
from nas_docker_mcp import nas_docker
from workspace_mcp import calendar_list_events, gmail_search

PT = ZoneInfo("America/Los_Angeles")

try:
    from tools.nas_config import resolve_nas_config, _resolve_nas_config
except ImportError:
    from nas_config import resolve_nas_config, _resolve_nas_config

HOST_1_IP, HOST_2_IP, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

EXECUTION_LOG_FILE = DATA_DIR / "sidecar_execution_log.json"
EXECUTION_STATUS_FILE = DATA_DIR / "sidecar_status.json"
MAX_LOG_ENTRIES = 200

# --------------------------------------------------------------------------
# Allowlists & Constants
# --------------------------------------------------------------------------
SERVERBROCK_STOPPED_ALLOWLIST = {
    "esphome", "ai-cli", "overseerr", "baseball_db",
    "baseball_shiny_app", "baseball_shiny_pro", "baseball_shiny_dev", "baseball-scraper-1"
}

SERVERBROCK2_EXPECTED_CONTAINERS = {
    "baseball_db", "baseball_projections_analysis", "big_board_pro",
    "baseball-scraper-1", "dockhand",
    "dozzle-agent", "discord-antigravity-agent", "discord-ivy-agent"
}
SERVERBROCK2_STOPPED_ALLOWLIST = set()
BROCKSERVER2_EXPECTED_CONTAINERS = SERVERBROCK2_EXPECTED_CONTAINERS

SELF_SENDER_PATTERNS = (
    "ryan brock", "ryan.", "ryanbrock", "rqb@"
)

REMINDER_SKIP_SENDER_PATTERNS = (
    "kings swim", "kingsswimacademy", "clipper", "garden route", "gardenrouteco",
    "dawn engel", "dawnengel", "faraci"
)
REMINDER_SKIP_SUBJECT_PATTERNS = (
    "faraci", "garden route", "dawn engel", "landscape project"
)

DATED_REMINDERS = [
    {
        "id": "hdd-prices-2026-11-05",
        "due": "2026-11-05",
        "message": (
            "💾 **HDD price check reminder** — target $400 for 20TB IronWolf Pro. "
            "Check Amazon/Newegg for current pricing."
        ),
    },
]

# --------------------------------------------------------------------------
# Helper: SSH & TCP Execution
# --------------------------------------------------------------------------
def _ssh_cmd(host: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run([
        "ssh", "-i", SSH_KEY, "-p", SSH_PORT,
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{SSH_USER}@{host}", cmd
    ], capture_output=True, text=True, timeout=timeout)

def check_tcp_port(host: str, port: int, timeout: float = 3.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()

# --------------------------------------------------------------------------
# Unified Execution Engine & Logger
# --------------------------------------------------------------------------
_log_lock = threading.Lock()

def _atomic_write_json(file_path: Path, data: any):
    """Write data to a JSON file atomically via a temporary file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_name(f"{file_path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(file_path)
    except Exception as e:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        print(f"[Sidecars] Error persisting {file_path}: {e}")

def log_execution(job_id: str, name: str, status: str, duration_sec: float, summary: str = "", error: str = "", extra: dict | None = None) -> dict:
    """Durably log job execution to EXECUTION_LOG_FILE and update EXECUTION_STATUS_FILE."""
    now_pt = datetime.now(PT)
    entry = {
        "job_id": job_id,
        "name": name,
        "status": status,  # 'ok', 'warning', 'error'
        "duration_seconds": round(duration_sec, 2),
        "timestamp_iso": now_pt.isoformat(),
        "timestamp_epoch": int(now_pt.timestamp()),
        "timestamp_pt": now_pt.strftime("%Y-%m-%d %I:%M %p PT"),
        "summary": summary[:400] if summary else "",
        "error": error[:600] if error else ""
    }
    if extra:
        entry["extra"] = extra

    is_test_runner = (
        "PYTEST_CURRENT_TEST" in os.environ
        or "pytest" in sys.modules
        or ("unittest" in sys.modules and any(k.startswith("test_") or ".test_" in k for k in sys.modules))
    )
    if is_test_runner and str(EXECUTION_STATUS_FILE).startswith("/workspace/data"):
        return entry

    with _log_lock:
        # 1. Update execution log
        entries = []
        if EXECUTION_LOG_FILE.exists():
            try:
                with open(EXECUTION_LOG_FILE, "r") as f:
                    entries = json.load(f)
            except Exception:
                entries = []
        entries.insert(0, entry)
        if len(entries) > MAX_LOG_ENTRIES:
            entries = entries[:MAX_LOG_ENTRIES]
        _atomic_write_json(EXECUTION_LOG_FILE, entries)

        # 2. Update latest status map
        status_map = {}
        if EXECUTION_STATUS_FILE.exists():
            try:
                with open(EXECUTION_STATUS_FILE, "r") as f:
                    status_map = json.load(f)
            except Exception:
                status_map = {}
        status_map[job_id] = entry
        _atomic_write_json(EXECUTION_STATUS_FILE, status_map)

    # 3. Synchronize schedule.json if BridgeScheduler tracks this sidecar
    try:
        from scheduler_tool import SIDECAR_ALIASES, load_schedule, save_schedule, calculate_next_run
        sched_file = DATA_DIR / "schedule.json"
        if sched_file.exists():
            jobs = load_schedule()
            aliases = SIDECAR_ALIASES.get(job_id, [job_id])
            s_updated = False
            now_epoch = int(now_pt.timestamp())
            for j in jobs:
                jid = j.get("id", "")
                if jid == job_id or jid in aliases or job_id in SIDECAR_ALIASES.get(jid, []):
                    j["last_run_ts"] = now_epoch
                    j["last_run_at"] = now_pt.strftime("%Y-%m-%d %I:%M %p PT")
                    j["next_run_ts"] = calculate_next_run(j, from_ts=now_epoch)
                    s_updated = True
            if s_updated:
                save_schedule(jobs)
    except Exception:
        pass

    return entry

def run_sidecar_job(job_id: str, name: str, func: callable, *args, **kwargs) -> tuple[bool, str, any]:
    """Unified execution wrapper for any scheduled sidecar job.
    
    Measures duration, handles and records unhandled exceptions, persists status,
    and returns (ok: bool, message: str, extra: any).
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    now_pt = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    is_quiet_hours = (now_pt.hour >= 23 or now_pt.hour < 7)
    AGORA_QUIET_JOBS = {
        "agora_roguelike_pm", "agora_roguelike_engineer", "agora_pm", "agora_engineer", "agora_sprint", "gotchi_sprint", "gotchi"
    }
    force_run = kwargs.get("force", False) or "--force" in sys.argv
    if is_quiet_hours and job_id in AGORA_QUIET_JOBS and not force_run:
        print(f"[Sidecar] Paused: '{name}' ({job_id}) skipped during 11 PM - 7 AM PT quiet hours.")
        return True, f"Paused: Quiet hours active (11 PM - 7 AM PT). Skipped {name}.", {"quiet_hours": True}

    start_ts = time.time()
    try:
        res = func(*args, **kwargs)
        duration = time.time() - start_ts

        ok = True
        message = ""
        extra = None

        if isinstance(res, tuple):
            if len(res) == 2:
                ok, message = res
            elif len(res) >= 3:
                ok, message = res[0], res[1]
                extra = res[2]
        elif isinstance(res, str):
            ok = True
            message = res
        elif isinstance(res, dict):
            ok = res.get("ok", True) if "ok" in res else (res.get("status") != "error")
            message = res.get("message", res.get("digest", res.get("error", str(res))))
            extra = res
        elif res is None:
            ok = True
            message = ""
        else:
            ok = bool(res)
            message = str(res)

        # Domain-aware status classification
        if job_id in ("reminders", "daily_birthday_reminder", "weekly_social_review", "monthly_core_friends_reminder"):
            # (has_items, msg): having 0 items/birthdays due is normal healthy silent operation
            has_items = ok
            ok = True
            status = "ok"
            base_extra = extra if isinstance(extra, dict) else {}
            extra = {**base_extra, "has_items": has_items, "has_due": has_items}
        elif job_id == "ev9":
            # (has_digest, msg, plot): daily silent capture is normal healthy operation
            if message and any(err_tag in message for err_tag in ("⚠️", "🚨", "Error", "Exception")):
                ok = False
                status = "error"
            else:
                ok = True
                status = "ok"
        elif job_id == "marketing":
            should_post = res[0] if isinstance(res, tuple) else True
            if message and any(err_tag in message for err_tag in ("⚠️", "🚨", "Could not")):
                ok = False
                status = "warning"
            else:
                ok = True
                status = "ok"
            extra = {"should_post": should_post}
        elif job_id == "heartbeat":
            status = "ok" if ok else "warning"
        elif job_id == "nas_logs":
            status = "ok" if ok else "warning"
        elif job_id == "plex":
            status = "ok" if ok else "warning"
        elif job_id in ("host1_backup", "host2_backup"):
            status = "ok" if ok else "error"
        else:
            status = "ok" if ok else "warning"

        summary = message[:400] if message else ((extra.get("summary", "(clean run / silent)") if isinstance(extra, dict) else "(clean run / silent)") if ok else "(degraded)")
        log_execution(job_id=job_id, name=name, status=status, duration_sec=duration, summary=summary, error="" if ok else summary, extra=extra if isinstance(extra, dict) else None)
        return ok, message, extra

    except Exception as e:
        duration = time.time() - start_ts
        tb_str = traceback.format_exc()
        err_msg = f"Exception: {e}"
        log_execution(job_id=job_id, name=name, status="error", duration_sec=duration, summary=err_msg, error=tb_str)
        print(f"[Sidecars] Unhandled exception in {name} ({job_id}): {e}\n{tb_str}")
        return False, f"🚨 **{name} Exception**: {e}", None

def get_execution_history(limit: int = 50, since_hours: float | None = None) -> list[dict]:
    """Retrieve recent execution history entries."""
    if not EXECUTION_LOG_FILE.exists():
        return []
    try:
        with open(EXECUTION_LOG_FILE, "r") as f:
            entries = json.load(f)
        if since_hours is not None:
            cutoff = time.time() - (since_hours * 3600)
            entries = [e for e in entries if e.get("timestamp_epoch", 0) >= cutoff]
        return entries[:limit]
    except Exception as e:
        print(f"[Sidecars] Error reading execution log: {e}")
        return []

def get_sidecar_health_summary(since_hours: float = 24.0) -> dict:
    """Calculate summary metrics for sidecars executed within the last N hours."""
    entries = get_execution_history(limit=MAX_LOG_ENTRIES, since_hours=since_hours)
    total = len(entries)
    ok_count = sum(1 for e in entries if e.get("status") == "ok")
    warn_count = sum(1 for e in entries if e.get("status") == "warning")
    err_count = sum(1 for e in entries if e.get("status") == "error")
    failures = [e for e in entries if e.get("status") in ("warning", "error")]

    # Fetch latest status per job
    latest_map = {}
    if EXECUTION_STATUS_FILE.exists():
        try:
            with open(EXECUTION_STATUS_FILE, "r") as f:
                latest_map = json.load(f)
        except Exception:
            latest_map = {}

    cutoff = (time.time() - (since_hours * 3600)) if since_hours is not None else 0
    active_failures = [
        entry for entry in latest_map.values()
        if entry.get("status") in ("warning", "error")
        and entry.get("timestamp_epoch", 0) >= cutoff
    ]

    # STALENESS AUDIT: Check for enabled jobs that have missed their execution cadence
    stale_jobs = []
    sched_file = DATA_DIR / "schedule.json"
    if sched_file.exists():
        try:
            with open(sched_file, "r") as sf:
                sched_jobs = json.load(sf)
            now_ts = time.time()
            for sj in sched_jobs:
                if not sj.get("enabled", True):
                    continue
                jid = sj.get("id", "")
                stype = sj.get("schedule_type", "daily")

                if stype == "interval":
                    cadence = sj.get("interval_seconds", 3600)
                    threshold = max(cadence * 3.0, 7200)
                    cadence_desc = f"every {cadence // 60}m" if cadence < 3600 else f"every {cadence / 3600:.1f}h"
                elif stype == "daily":
                    cadence = 86400
                    threshold = 86400 * 2.5
                    cadence_desc = "daily"
                elif stype == "weekly":
                    cadence = 7 * 86400
                    threshold = 7 * 86400 * 2.0
                    cadence_desc = "weekly"
                elif stype == "monthly":
                    cadence = 30 * 86400
                    threshold = 35 * 86400
                    cadence_desc = "monthly"
                else:
                    continue

                try:
                    from tools.scheduler_tool import get_last_execution_for_job
                    last_epoch, last_pt = get_last_execution_for_job(sj)
                except Exception:
                    entry = latest_map.get(jid)
                    status_epoch = entry.get("timestamp_epoch", 0) if entry else 0
                    sched_epoch = int(sj.get("last_run_ts") or 0)
                    if status_epoch >= sched_epoch and status_epoch > 0:
                        last_epoch = status_epoch
                        last_pt = entry.get("timestamp_pt", "unknown")
                    elif sched_epoch > 0:
                        last_epoch = sched_epoch
                        last_pt = sj.get("last_run_at", "unknown")
                    else:
                        last_epoch = None
                        last_pt = "never"

                if not last_epoch:
                    stale_jobs.append({
                        "id": jid,
                        "name": sj.get("name", jid),
                        "last_run_pt": "never",
                        "age_hours": None,
                        "cadence_desc": cadence_desc,
                        "reason": "never executed"
                    })
                elif (now_ts - last_epoch) > threshold:
                    age_h = (now_ts - last_epoch) / 3600
                    stale_jobs.append({
                        "id": jid,
                        "name": sj.get("name", jid),
                        "last_run_pt": last_pt or "unknown",
                        "age_hours": round(age_h, 1),
                        "cadence_desc": cadence_desc,
                        "reason": f"last ran {age_h:.1f}h ago ({last_pt}), expected {cadence_desc}"
                    })
        except Exception as se:
            print(f"[Sidecars] Error evaluating staleness: {se}")

    return {
        "total_runs": total,
        "ok_count": ok_count,
        "warning_count": warn_count,
        "error_count": err_count,
        "failures": failures,
        "active_failures": active_failures,
        "stale_jobs": stale_jobs,
        "latest_by_job": latest_map,
        "all_healthy": len(active_failures) == 0 and len(stale_jobs) == 0
    }

def format_sidecar_status_summary() -> str:
    """Format human-readable sidecar execution status."""
    summary = get_sidecar_health_summary(since_hours=24.0)
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    lines = [
        f"⚙️ **Sidecar Execution Status** [{now_str}]",
        f"- **Runs (Last 24h)**: `{summary['total_runs']}` total (`{summary['ok_count']}` ok, `{summary['warning_count']}` warnings, `{summary['error_count']}` errors)",
        ""
    ]

    latest = summary.get("latest_by_job", {})
    if not latest:
        lines.append("*(No sidecar jobs recorded yet)*")
    else:
        lines.append("### 📊 Latest Status by Job")
        for jid, entry in sorted(latest.items()):
            status = entry.get("status", "unknown")
            icon = "🟢" if status == "ok" else ("⚠️" if status == "warning" else "❌")
            name = entry.get("name", jid)
            ts = entry.get("timestamp_pt", "unknown")
            dur = entry.get("duration_seconds", 0)
            lines.append(f"- {icon} **{name}** (`{jid}`): `{status.upper()}` in `{dur}s` — *{ts}*")
            if status != "ok" and entry.get("summary"):
                snip = entry['summary'].splitlines()[0][:100]
                lines.append(f"  -# *Detail:* `{snip}`")

    if summary.get("stale_jobs"):
        lines.extend([
            "",
            "### ⏳ Stale / Missed Sidecars (Cadence Warning)"
        ])
        for sj in summary["stale_jobs"]:
            lines.append(f"- ⚠️ **{sj['name']}** (`{sj['id']}`): {sj['reason']}")

    if summary["failures"]:
        lines.extend([
            "",
            "### ⚠️ Failures / Warnings (Last 24h)"
        ])
        for f in summary["failures"][:10]:
            icon = "❌" if f.get("status") == "error" else "⚠️"
            lines.append(f"- {icon} **{f.get('name')}** (*{f.get('timestamp_pt')}*): `{f.get('summary', '')[:120]}`")

    return "\n".join(lines)

format_sidecar_status_report = format_sidecar_status_summary

# --------------------------------------------------------------------------
# 1. Heartbeat Sweep
# --------------------------------------------------------------------------
def run_heartbeat_sweep() -> tuple[bool, str]:
    HOST_1_IP, HOST_2_IP, _ = _resolve_nas_config()
    failures = []
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")

    # 1. Host1 (.82) Containers (with automatic self-heal for unhealthy containers)
    sb_res = json.loads(nas_docker("ps", HOST_1_IP))
    if not sb_res.get("ok"):
        failures.append(f"Host1 ({HOST_1_IP}) Docker unreachable: {sb_res.get('error')}")
    else:
        docker_root = os.environ.get("NAS_DOCKER_ROOT", "/docker")
        for line in sb_res.get("output", "").splitlines():
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if parts:
                name = parts[0]
                status = parts[1] if len(parts) > 1 else ""
                if name in SERVERBROCK_STOPPED_ALLOWLIST:
                    continue
                if not status.startswith("Up"):
                    failures.append(f"Host1 ({HOST_1_IP}) container `{name}` is {status}")
                elif "(unhealthy)" in status.lower():
                    heal_res = json.loads(
                        nas_docker(
                            "compose_restart",
                            host=HOST_1_IP,
                            compose_dir=f"{docker_root}/appdata",
                            service=name,
                        )
                    )
                    if not heal_res.get("ok"):
                        failures.append(
                            f"Host1 ({HOST_1_IP}) container `{name}` is {status} and self-heal failed: {heal_res.get('error')}"
                        )

    # 2. Host2 (.84) Containers
    bs2_res = json.loads(nas_docker("ps", HOST_2_IP))
    if not bs2_res.get("ok"):
        failures.append(f"Host2 ({HOST_2_IP}) Docker unreachable: {bs2_res.get('error')}")
    else:
        running = {line.split("\t")[0].strip() for line in bs2_res.get("output", "").splitlines() if "\t" in line and "Up" in line}
        missing = SERVERBROCK2_EXPECTED_CONTAINERS - running
        if missing:
            failures.append(f"Host2 ({HOST_2_IP}) missing expected containers: {sorted(missing)}")

    # 3. Critical TCP Ports
    if not check_tcp_port(HOST_1_IP, 8123):
        failures.append(f"Home Assistant port `8123` unreachable on Host1 (`{HOST_1_IP}`)")
    if not check_tcp_port(HOST_1_IP, 32400):
        failures.append(f"Plex port `32400` unreachable on Host1 (`{HOST_1_IP}`)")
    if not check_tcp_port(HOST_2_IP, 3866):
        failures.append(f"Dockhand port `3866` unreachable on Host2 (`{HOST_2_IP}`)")

    # 4. Home Assistant REST Liveness
    ha_res = json.loads(ha_ping())
    if not ha_res.get("ok"):
        failures.append(f"Home Assistant REST API check failed: {ha_res.get('error')}")

    # 5. External WAN / Cloud Monitors (UptimeRobot)
    try:
        from tools.uptimerobot import uptimerobot
        ur_res = uptimerobot("get_monitors")
        if ur_res.get("ok"):
            for m in ur_res.get("result", {}).get("monitors", []):
                status = m.get("status")
                name = m.get("friendly_name")
                if status in (8, 9):
                    failures.append(f"External monitor `{name}` is DOWN per UptimeRobot (WAN / port-forward / site drop)")
    except Exception:
        pass

    # 6. Persistent MCP Daemon Health (with auto-heal on transient drop or reboot race)
    try:
        from tools.mcp_daemon import ensure_mcp_daemon_running, get_status
        mcp_stat = get_status()
        if not mcp_stat.get("healthy"):
            ensure_mcp_daemon_running()
            mcp_stat = get_status()
        if not mcp_stat.get("healthy"):
            failures.append(f"Persistent MCP Daemon degraded or stopped: {mcp_stat.get('servers')}")
    except Exception as me:
        failures.append(f"Persistent MCP Daemon check failed: {me}")

    # 7. Zero Health Server (port 8769 / probe.brock.ventures) with auto-heal
    try:
        from tools.zero_health_server import ensure_health_server_running, get_status as get_health_server_status
        hs_stat = get_health_server_status()
        if not hs_stat.get("running") or not check_tcp_port("127.0.0.1", int(hs_stat.get("port", 8769))):
            ensure_health_server_running()
            time.sleep(0.5)
            hs_stat = get_health_server_status()
        if not hs_stat.get("running") or not check_tcp_port("127.0.0.1", int(hs_stat.get("port", 8769))):
            failures.append("Zero Health Server (`zero_health_server.py` on port `8769`) is offline and failed auto-heal")
    except Exception as he:
        failures.append(f"Zero Health Server check failed: {he}")


    if not failures:
        return True, f"🟢 **Heartbeat Sweep Healthy** — {now_str}\nAll containers, ports, Home Assistant, and external monitors healthy across `.82` and `.84`."

    report = [f"🚨 **Heartbeat Alert — Infrastructure Degraded** [{now_str}]", ""]
    for f in failures:
        report.append(f"- ⚠️ {f}")
    return False, "\n".join(report)

# --------------------------------------------------------------------------
# 2. Nightly Triage Briefing (with Automated Failure Surfacing)
# --------------------------------------------------------------------------
def _synthesize_fallback_bullet(sender_clean: str, subj: str, body_text: str, inline_cal: str) -> str:
    """Synthesize an ultra-short what-it-is + action/impact bullet (~8-14 words) when LLM triage is offline."""
    combined = f"{subj} {body_text}".lower()

    fee_m = re.search(r"\$\d+(?:\.\d{2})?(?:\s*(?:per\s*[a-zA-Z]+|/[a-zA-Z]+))?", body_text, re.IGNORECASE)
    order_m = re.search(r"order(?:\s*number|\s*#)?[:\s]+([A-Z0-9-]{5,})", body_text, re.IGNORECASE)

    if "pickup" in combined or "ready for store pickup" in combined:
        ord_str = f" (#{order_m.group(1)})" if order_m else ""
        return f"- **{sender_clean} Pickup:** Order{ord_str} ready at front checkout."
    if "library" in combined and ("due" in combined or "renew" in combined):
        return f"- **{sender_clean}:** Books due soon; check if any items didn't auto-renew."
    if "finish creating" in combined or "complete your account" in combined or "set a password" in combined:
        return f"- **{sender_clean}:** Set your password to finish account setup."
    if "enrollment confirmation" in combined or "thank you for signing up" in combined:
        event_title = subj.split("|")[0].strip() if "|" in subj else subj
        fee_note = f"; pay {fee_m.group(0)} at check-in" if fee_m else ""
        cal_str = f" for {inline_cal.strip()}" if inline_cal else ""
        return f"- **{event_title}:** Confirmed{cal_str}{fee_note}."
    if "invitation" in combined or "rsvp" in combined or "punchbowl" in combined:
        event_title = re.sub(r"^(?:punchbowl\s+invitation:\s*|invitation:\s*)", "", subj, flags=re.I).strip() or "Event Invite"
        cal_str = f" ({inline_cal.strip()})" if inline_cal else ""
        return f"- **{event_title}:** Invite from {sender_clean}{cal_str} — RSVP requested."
    if any(k in combined for k in ["appointment", "reservation", "confirmed for", "booking confirmed"]):
        cal_str = f" for {inline_cal.strip()}" if inline_cal else ""
        return f"- **{sender_clean}:** Appointment confirmed{cal_str}."

    summary_desc = subj if subj else "Unread message"
    cal_suffix = f" ({inline_cal.strip()})" if inline_cal else ""
    if body_text and body_text.lower() != subj.lower():
        first_sentence = re.split(r"(?<=[.!?])\s+", body_text.strip())[0]
        if len(first_sentence) > 65:
            first_sentence = first_sentence[:62] + "..."
        return f"- **{sender_clean}:** {first_sentence}{cal_suffix}"
    return f"- **{sender_clean}:** {summary_desc}{cal_suffix}"


def _triage_unread_emails(messages: list[dict]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Triage unread emails from the past week into ultra-short what-it-is + action bullets."""
    proposed_calendar = []
    priority_lines = []
    newsletter_lines = []
    transactional_lines = []

    if not messages:
        return proposed_calendar, priority_lines, newsletter_lines, transactional_lines

    candidates = []
    for m in messages:
        sender = m.get("from", "")
        subj = m.get("subject", "").strip()
        s_low = sender.lower()
        sub_low = subj.lower()
        if any(p in s_low for p in SELF_SENDER_PATTERNS):
            continue
        if any(p in s_low for p in REMINDER_SKIP_SENDER_PATTERNS) or any(p in sub_low for p in REMINDER_SKIP_SUBJECT_PATTERNS):
            continue
        candidates.append(m)

    if not candidates:
        return proposed_calendar, priority_lines, newsletter_lines, transactional_lines

    summary_input = []
    for m in candidates[:25]:
        sender_name, sender_addr = email.utils.parseaddr(m.get("from", ""))
        sender_clean = sender_name.strip().strip('"').strip("'") or sender_addr
        body_excerpt = (m.get("body_excerpt") or m.get("snippet") or "").strip()
        summary_input.append({
            "from": sender_clean,
            "date": m.get("date", ""),
            "subject": m.get("subject", ""),
            "content": body_excerpt[:500]
        })

    prompt = (
        f"You are Zero, triaging Ryan's unread inbox from the past 7 days.\n"
        f"Unread emails:\n{json.dumps(summary_input, indent=2)}\n\n"
        f"Rules:\n"
        f"1. Keep every 'priority' bullet ULTRA-SHORT (strictly 8–14 words max, 1 short clause).\n"
        f"2. Format: '- **<2-3 Word Topic/Sender>:** <What it is + action/impact>.'\n"
        f"3. Drop filler words, street addresses, order numbers, payment methods, and secondary sub-events. Only keep the core event/topic, date/time, fee (if any), and action.\n"
        f"   Good examples:\n"
        f"   - **Library Notice:** Books due soon; check if any items didn't auto-renew.\n"
        f"   - **Pumpkins & Pancakes:** Confirmed for Sat 10/17 (10 AM); pay $25/person at check-in.\n"
        f"   - **Peninsula Community Center:** Click link to set password and finish account setup.\n"
        f"   - **CVS Pickup:** Photo order ready for pickup at Redwood City store.\n"
        f"   - **Halloween Party Invite:** Sat 10/31 @ 4:30 PM — RSVP requested.\n"
        f"4. Put digests/roundups in 'newsletters' with ONLY the name (e.g. '- **Redwood City Events**').\n"
        f"5. Put routine shipping/payment receipts in 'transactional'.\n\n"
        f"Return ONLY valid JSON matching this schema:\n"
        f"{{\n"
        f"  \"calendar_proposals\": [\"- **<Date/Time>** — *<Event>* (<Sender>)\"],\n"
        f"  \"priority\": [\"- **<Short Topic>:** <8-14 word summary + action>\"],\n"
        f"  \"newsletters\": [\"- **<Newsletter Name>**\"],\n"
        f"  \"transactional\": [\"- **<Sender>:** <Short label>\"]\n"
        f"}}"
    )
    try:
        from tools.llm_sidecar import _is_test_fast_mode, get_sidecar_model
        if not _is_test_fast_mode():
            triage_model = get_sidecar_model("nightly_triage", "gemini-3.8-flash-low")
            res = subprocess.run(
                ["agy", f"--model={triage_model}", "--disable-slash-commands", f"-p={prompt}"],
                capture_output=True, text=True, timeout=25
            )
            if res.returncode == 0 and res.stdout.strip():
                m_json = re.search(r"\{[\s\S]*\}", res.stdout)
                if m_json:
                    data = json.loads(m_json.group(0))
                    proposed_calendar = data.get("calendar_proposals", [])
                    priority_lines = data.get("priority", [])
                    for n in data.get("newsletters", []):
                        n_clean = re.sub(r"(:\*\*|\*\*:)\s*.*$", "**", str(n)).strip()
                        n_clean = re.sub(r"\s*[—–-]\s*\*.*$", "", n_clean).strip()
                        if n_clean and n_clean not in newsletter_lines:
                            newsletter_lines.append(n_clean)
                    transactional_lines = data.get("transactional", [])
                    return proposed_calendar, priority_lines, newsletter_lines, transactional_lines
    except Exception as e:
        print(f"[Triage] LLM inbox triage fallback: {e}")

    for m in candidates:
        sender = m.get("from", "")
        subj = m.get("subject", "").strip()
        snippet = (m.get("body_excerpt") or m.get("snippet") or "").strip()
        s_low = sender.lower()
        sub_low = subj.lower()
        snip_low = snippet.lower()

        sender_name, sender_addr = email.utils.parseaddr(sender)
        sender_clean = sender_name.strip().strip('"').strip("'") or sender_addr

        inline_cal = ""
        exp_match = re.search(r"(?:expiration|expires|valid until|due by|exp(?:ire)?s?):\s*(\d{1,2}/\d{1,2}/\d{2,4})", snippet, re.IGNORECASE)
        time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM))\s+(?:on\s+)?([A-Za-z]+,?\s+[A-Za-z]+\s+\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})", snippet, re.IGNORECASE)
        if exp_match:
            proposed_calendar.append(f"- **By {exp_match.group(1)}** — *{subj}* ({sender_clean})")
            inline_cal = f"📅 Due by {exp_match.group(1)}"
        elif time_match:
            proposed_calendar.append(f"- **{time_match.group(2)} @ {time_match.group(1)}** — *{subj}* ({sender_clean})")
            inline_cal = f"📅 {time_match.group(2)} @ {time_match.group(1)}"
        elif any(k in sub_low or k in snip_low for k in ["appointment", "reservation", "confirmed for", "booking confirmed", "installation window", "estimate", "invitation", "enrollment confirmation"]):
            date_m = re.search(r"\b([A-Za-z]+,\s*[A-Za-z]+\s*\d{1,2}(?:st|nd|rd|th)?|\d{1,2}/\d{1,2}/\d{2,4})\b", snippet)
            d_str = date_m.group(1) if date_m else "Upcoming"
            proposed_calendar.append(f"- **{d_str} (Proposed)** — *{subj}* ({sender_clean})")
            inline_cal = f"📅 {d_str}"

        clean_snip = html.unescape(snippet).replace("\n", " ").strip()
        clean_snip = re.sub(r"\s+", " ", clean_snip)

        is_family_or_actionable = (
            "emily" in s_low
            or any(k in sub_low or k in snip_low for k in [
                "ready for store pickup", "ready for pickup", "action required",
                "library notice", "due back soon", "enrollment confirmation",
                "finish creating", "complete your account", "invitation", "rsvp"
            ])
        )

        if not is_family_or_actionable and any(kw in s_low or kw in sub_low for kw in ["newsletter", "chronicle", "digest", "projections report", "bb pro", "daily fantasy", "roundup", "weekly", "redwood city events", "recnews"]):
            entry = f"- **{sender_clean}**"
            if entry not in newsletter_lines:
                newsletter_lines.append(entry)
        elif not is_family_or_actionable and any(kw in s_low or kw in sub_low for kw in ["amazon", "order", "delivery", "shipping", "receipt", "invoice", "dogfood", "stardust", "payment received", "tracking", "thanks for filling out"]):
            transactional_lines.append(f"- **{sender_clean}:** {subj}")
        else:
            priority_lines.append(_synthesize_fallback_bullet(sender_clean, subj, clean_snip, inline_cal))

    return proposed_calendar, priority_lines, newsletter_lines, transactional_lines


def run_nightly_triage() -> str:
    now_pt = datetime.now(PT)
    tomorrow_pt = now_pt + timedelta(days=1)
    t_start = tomorrow_pt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    t_end = tomorrow_pt.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    # 1. Calendar
    cal_res = json.loads(calendar_list_events("all", t_start, t_end, 20))
    events = cal_res.get("events", [])

    agenda_lines = []
    if events:
        for ev in events:
            raw_start = ev.get("start", "")
            time_str = "All Day"
            if "T" in raw_start:
                try:
                    dt = datetime.fromisoformat(raw_start)
                    time_str = dt.astimezone(PT).strftime("%I:%M %p")
                except Exception:
                    time_str = raw_start[:16]
            agenda_lines.append(f"- **{time_str}** — {ev.get('summary')}")
    else:
        agenda_lines.append("- *(No events scheduled for tomorrow)*")

    # 2. Gmail Triage (Scan all unread inbox emails from the past 7 days with body context)
    try:
        raw_gmail = gmail_search("in:inbox is:unread newer_than:7d", 50, include_body=True)
    except TypeError:
        raw_gmail = gmail_search("in:inbox is:unread newer_than:7d", 50)
    gmail_res = json.loads(raw_gmail)
    messages = gmail_res.get("messages", [])

    _, priority_lines, newsletter_lines, transactional_lines = _triage_unread_emails(messages)

    inbox_lines = []
    if priority_lines:
        inbox_lines.extend(priority_lines)
    else:
        inbox_lines.append("- *(No unread priority emails from the past 7 days)*")

    if transactional_lines:
        inbox_lines.append(f"*({len(transactional_lines)} routine order/system receipt(s) skipped)*")

    # 3. Scheduled Sidecar Job Failure Check
    health = get_sidecar_health_summary(since_hours=24.0)
    failure_lines = []
    active_failures = health.get("active_failures", [])
    if active_failures:
        for f in active_failures:
            icon = "❌" if f.get("status") == "error" else "⚠️"
            sum_snip = f.get("summary", "").splitlines()[0] if f.get("summary") else "Degraded or errored run"
            if len(sum_snip) > 80:
                sum_snip = sum_snip[:77] + "..."
            failure_lines.append(f"- {icon} **{f.get('name')}** (*{f.get('timestamp_pt', 'recent')}*): `{sum_snip}`")

    # 4. Infra & Homelab Posture
    infra_ok, _ = run_heartbeat_sweep()
    sb_res = json.loads(nas_docker("ps", HOST_1_IP))
    bs2_res = json.loads(nas_docker("ps", HOST_2_IP))
    sb_up = sum(1 for line in (sb_res.get("output") or "").splitlines() if "\t" in line and "Up" in line)
    bs2_up = sum(1 for line in (bs2_res.get("output") or "").splitlines() if "\t" in line and "Up" in line)
    total_jobs = len(health.get("latest_by_job", {}))

    # 5. Open P0 Emergency Issues Only (P1/P2 saved for Weekly Digest)
    p0_lines = []
    try:
        from tools.task_manager import task_manage
        from tools.google_tasks_sync import is_dev_task, sync_tasks
        try:
            sync_tasks(quiet=True)
        except Exception as sync_err:
            log.warning(f"Pre-triage Google Tasks sync warning: {sync_err}")
        t_data = task_manage("list")
        for t in t_data.get("tasks", []):
            pri = str(t.get("priority", "")).lower()
            stat = str(t.get("status", "")).lower()
            if pri == "p0" and stat not in ("completed", "retired") and not is_dev_task(t):
                p0_lines.append(f"- 🔴 P0 `#{t.get('id')}` **{t.get('title')}** (*{t.get('status')}*)")
    except Exception as e:
        log.warning(f"Failed to fetch tasks for nightly triage: {e}")

    date_header = tomorrow_pt.strftime("%A, %b %-d")
    report = [
        f"📋 **Nightly Assistant** — {date_header}",
        "",
        "### 📅 Tomorrow's Agenda",
        *agenda_lines,
        "",
        "### 📬 Unread Inbox Triage (Past 7 Days)",
        *inbox_lines,
    ]

    if newsletter_lines:
        report.extend([
            "",
            "### 📰 Newsletters *(Ask for details to summarize any item)*",
            *newsletter_lines,
        ])

    if p0_lines:
        report.extend([
            "",
            "### 🚨 Open P0 Emergencies",
            *p0_lines
        ])

    if not infra_ok or failure_lines:
        report.extend([
            "",
            "### ⚠️ Homelab & Sidecar Alerts",
        ])
        if not infra_ok:
            report.append(f"- 🚨 **Infra Health:** Degraded containers or ports detected (Host1: `{sb_up}` up | Host2: `{bs2_up}` up)")
        if failure_lines:
            report.extend(failure_lines)
    else:
        report.extend([
            "",
            f"🖥️ *Homelab: All {sb_up + bs2_up} containers & {total_jobs} sidecars healthy ✅*"
        ])

    return "\n".join(report)

# --------------------------------------------------------------------------
# 3. NAS Log Review
# --------------------------------------------------------------------------
def run_nas_log_review(since: str = "24h") -> tuple[bool, str, dict]:
    """Execute high-speed batch NAS log scanning & triage across Host 1 and Host 2."""
    import importlib
    try:
        import tools.nas_log_triage as _triage_mod
    except ImportError:
        import nas_log_triage as _triage_mod
    importlib.reload(_triage_mod)
    return _triage_mod.run_nas_log_review(since=since)


# --------------------------------------------------------------------------
# 4. Plex Transcode Session Cleanup
# --------------------------------------------------------------------------
def run_plex_session_cleanup(dry_run: bool = False) -> tuple[bool, str]:
    """Prune stale Plex transcode sessions. Silent unless there is an error/problem."""
    try:
        from tautulli import tautulli
        act = tautulli("get_activity")
        if not act.get("ok"):
            return False, f"⚠️ **Plex Cleanup Warning**: Could not query Tautulli: {act.get('error')}"

        stream_count = int((act.get("data") or {}).get("stream_count", 0))
        if stream_count > 0:
            # Active streams in progress — silently skip
            return True, ""

        # Scan for stale sessions > 60m old
        cmd = "find /docker/appdata/plex/Temp/Transcode/Sessions -maxdepth 1 -name 'plex-transcode-*' -mmin +60"
        res = _ssh_cmd(HOST_1_IP, cmd, timeout=20)
        stale_dirs = [d.strip() for d in res.stdout.splitlines() if d.strip()]

        if not stale_dirs:
            # Transcode cache clean — silent
            return True, ""

        if dry_run:
            return True, f"🎬 **Plex Cleanup (Dry Run)**: Found {len(stale_dirs)} stale directory(ies) to prune."

        # Remove stale directories
        del_cmd = "find /docker/appdata/plex/Temp/Transcode/Sessions -maxdepth 1 -name 'plex-transcode-*' -mmin +60 -exec rm -rf {} +"
        del_res = _ssh_cmd(HOST_1_IP, del_cmd, timeout=30)
        if del_res.returncode == 0:
            # Successfully pruned without errors — silent
            return True, ""
        return False, f"⚠️ **Plex Cleanup Warning**: Pruning exited with code {del_res.returncode}: {del_res.stderr.strip()}"
    except Exception as e:
        return False, f"🚨 **Plex Cleanup Exception**: {e}"

# --------------------------------------------------------------------------
# 5. Dated Reminders
# --------------------------------------------------------------------------
def run_dated_reminders(today_str: str | None = None) -> tuple[bool, str]:
    now_pt = datetime.now(PT)
    today = today_str or now_pt.strftime("%Y-%m-%d")
    state_file = DATA_DIR / "dated_reminders_fired.json"

    fired = set()
    if state_file.exists():
        try:
            with open(state_file) as f:
                fired = set(json.load(f))
        except Exception:
            pass

    reminders_file = DATA_DIR / "reminders.json"
    reminders_list = list(DATED_REMINDERS)
    if reminders_file.exists():
        try:
            with open(reminders_file) as f:
                reminders_list = json.load(f)
        except Exception:
            pass

    due = []
    for r in reminders_list:
        if r["id"] not in fired and today >= r["due"]:
            due.append(r)

    if not due:
        return False, ""

    msg = "\n\n".join(r["message"] for r in due)
    for r in due:
        fired.add(r["id"])

    try:
        with open(state_file, "w") as f:
            json.dump(sorted(fired), f, indent=2)
    except Exception:
        pass

    return True, msg

# --------------------------------------------------------------------------
# 6. Used EV9 Monitor
# --------------------------------------------------------------------------
def run_ev9_monitor(force_digest: bool = False) -> tuple[bool, str, str | None]:
    try:
        import ev9_monitor
        res = ev9_monitor.run_capture()
        if not res.get("ok"):
            return False, f"⚠️ **EV9 Monitor Error**: {res.get('error', 'Unknown error')}", None

        now_pt = datetime.now(PT)
        # Post digest on Sundays or when forced
        if force_digest or now_pt.weekday() == 6:
            digest = res.get("digest", "")
            plot_path = None
            try:
                p = ev9_monitor.generate_trend_plot("/tmp/ev9_trend.png", 30)
                if p and os.path.exists(p):
                    plot_path = p
            except Exception:
                pass
            return True, digest or "🚘 **EV9 Monitor**: Weekly capture completed (no new digest).", plot_path

        # Mon-Sat: silent background capture only. No digest, no trend chart.
        return False, "", None
    except Exception as e:
        return False, f"⚠️ **EV9 Monitor Exception**: {e}", None

# --------------------------------------------------------------------------
# 7. Marketing Email Sweep (Biweekly Sunday)
# --------------------------------------------------------------------------
MARKETING_STATE_FILE = "/workspace/data/marketing_sweep_last_run"
MARKETING_SENDERS_FILE = "/workspace/data/marketing_senders.json"
MARKETING_MIN_DAYS_BETWEEN_RUNS = 12
MARKETING_QUERY = "category:promotions newer_than:15d"
MARKETING_PROTECTED_SENDERS = ("roy cloud", "konstella", "famly", "powerschool", "homeroom")

def _marketing_due() -> bool:
    if not os.path.exists(MARKETING_STATE_FILE):
        return True
    try:
        with open(MARKETING_STATE_FILE) as f:
            last = datetime.strptime(f.read().strip(), "%Y-%m-%d").date()
        return (datetime.now(PT).date() - last).days >= MARKETING_MIN_DAYS_BETWEEN_RUNS
    except Exception:
        return True

def run_marketing_sweep(force: bool = False) -> tuple[bool, str]:
    """Biweekly promotional-inbox sweep. Returns (should_post, message)."""
    now_pt = datetime.now(PT)
    if not force and not _marketing_due():
        return False, ""

    date_str = now_pt.strftime("%b %d")
    res = json.loads(gmail_search(MARKETING_QUERY, 50))
    if not res.get("ok"):
        return True, f"📬 **Biweekly Marketing Report** — {date_str}\n⚠️ Could not search promotions: {res.get('error')}"

    msgs = res.get("messages", []) or []
    counts = {}
    for m in msgs:
        sender = m.get("from", "")
        s_low = sender.lower()
        if any(p in s_low for p in MARKETING_PROTECTED_SENDERS):
            continue
        parsed_name, parsed_addr = email.utils.parseaddr(sender)
        addr = parsed_addr.lower().strip() if parsed_addr else sender.lower().strip()
        name = parsed_name.strip().strip('"').strip("'") or addr
        entry = counts.setdefault(addr, {"name": name, "email": addr, "count": 0})
        entry["count"] += 1

    senders = sorted(counts.values(), key=lambda s: (-s["count"], s["name"].lower()))

    if not senders:
        msg = f"📬 **Biweekly Marketing Report** — {date_str}\nNo promotional emails in the last 2 weeks. ✅"
        should_post = False
    else:
        lines = [f"{i}. {s['name']} — {s['count']} email{'s' if s['count'] != 1 else ''}" for i, s in enumerate(senders, 1)]
        msg = (
            f"📬 **Biweekly Marketing Report** — {date_str}\n\n"
            f"**{len(senders)} promotional senders this period:**\n"
            + "\n".join(lines)
            + "\n\nReply with sender numbers or names to unsubscribe."
        )
        should_post = True

    try:
        os.makedirs(os.path.dirname(MARKETING_STATE_FILE), exist_ok=True)
        with open(MARKETING_STATE_FILE, "w") as f:
            f.write(now_pt.strftime("%Y-%m-%d"))
        with open(MARKETING_SENDERS_FILE, "w") as f:
            json.dump(senders, f, indent=2)
    except Exception:
        pass

    return should_post, msg

# --------------------------------------------------------------------------
# 8-14. Additional Maintenance Tasks
# --------------------------------------------------------------------------
def run_ha_battery_check(threshold: float = 15.0) -> tuple[bool, str, dict]:
    """Check IoT sensor battery levels via ha_battery_check."""
    try:
        res = subprocess.run(["python3", "/workspace/tools/ha_battery_check.py", f"--threshold={threshold}"], capture_output=True, text=True, timeout=20)
        out = res.stdout.strip()
        if res.returncode != 0 or "Failed to query" in out or "token not found" in out:
            return False, out or "⚠️ Low battery check script exited with error.", {"has_alert": False, "error": True}
        has_alert = "Low Battery Alert" in out
        return True, out or "✅ All IoT sensors healthy.", {"has_alert": has_alert}
    except Exception as e:
        return False, f"⚠️ Low battery check failed: {e}", {"has_alert": False, "error": str(e)}

def run_ha_anomaly_check(quiet: bool = False) -> tuple[bool, str, dict]:
    """Check Home Assistant + VictoriaMetrics statistical telemetry anomalies."""
    try:
        cmd = ["python3", "/workspace/tools/ha_anomaly_detector.py"]
        if quiet:
            cmd.append("--quiet")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        out = res.stdout.strip()
        if res.returncode != 0 or "Anomaly Check Failed" in out:
            return False, out or "⚠️ HA telemetry anomaly check exited with error.", {"has_alert": False, "error": True}
        has_alert = "Telemetry Anomaly Report" in out
        return True, out or "✅ All Home Assistant telemetry sensors within statistical baselines.", {"has_alert": has_alert}
    except Exception as e:
        return False, f"⚠️ HA telemetry anomaly check failed: {e}", {"has_alert": False, "error": str(e)}

def run_nas_storage_check() -> tuple[bool, str]:
    """Check NAS volume storage and RAID status."""
    try:
        res = subprocess.run(["python3", "/workspace/tools/nas_storage_check.py"], capture_output=True, text=True, timeout=30)
        out = res.stdout.strip()
        if "ALERT" in out.upper() or "DEGRADED" in out.upper():
            return False, out
        return True, out or "✅ NAS storage and RAID healthy."
    except Exception as e:
        return False, f"⚠️ NAS storage check failed: {e}"

def run_host2_backup(quiet: bool = True) -> tuple[bool, str, dict]:
    """Execute automated local backup of Host 2 to /volumeUSB1/usbshare/."""
    try:
        cmd = ["python3", "/workspace/tools/backup_host2.py"]
        if quiet:
            cmd.append("--quiet")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        out = res.stdout.strip()
        err = res.stderr.strip()
        combined = f"{out}\n{err}".strip() if err else out
        if res.returncode != 0:
            return False, f"⚠️ Host 2 backup failed (code {res.returncode}): {combined}", {"output": out, "error": err}
        return True, "", {"summary": "Host 2 local USB backup completed successfully", "output": out}
    except Exception as e:
        return False, f"⚠️ Host 2 backup exception: {e}", {"error": str(e)}

def run_host1_backup(quiet: bool = True) -> tuple[bool, str, dict]:
    """Execute automated local backup of Host 1 to /volumeUSB1/usbshare/."""
    try:
        cmd = ["python3", "/workspace/tools/backup_host1.py"]
        if quiet:
            cmd.append("--quiet")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = res.stdout.strip()
        err = res.stderr.strip()
        combined = f"{out}\n{err}".strip() if err else out
        if res.returncode != 0:
            return False, f"⚠️ Host 1 backup failed (code {res.returncode}): {combined}", {"output": out, "error": err}
        return True, "", {"summary": "Host 1 local USB backup completed successfully", "output": out}
    except Exception as e:
        return False, f"⚠️ Host 1 backup exception: {e}", {"error": str(e)}

def run_ha_update_check() -> tuple[bool, str]:
    """Check for stable mature Home Assistant updates."""
    try:
        res = subprocess.run(["python3", "/workspace/tools/ha_update_check.py", "check", "--quiet"], capture_output=True, text=True, timeout=30)
        out = res.stdout.strip()
        return True, out
    except Exception as e:
        return False, f"⚠️ HA update check failed: {e}"

def run_dockhand_update_check() -> tuple[bool, str]:
    """Check Dockhand image updates."""
    try:
        res = subprocess.run(["python3", "/workspace/tools/dockhand_update.py", "check", "--quiet"], capture_output=True, text=True, timeout=30)
        out = res.stdout.strip()
        return True, out
    except Exception as e:
        return False, f"⚠️ Dockhand update check failed: {e}"

def run_antigravity_check() -> tuple[bool, str]:
    """Check Antigravity CLI release updates."""
    try:
        res = subprocess.run(["python3", "/workspace/tools/update_antigravity.py", "check", "--quiet"], capture_output=True, text=True, timeout=30)
        out = res.stdout.strip()
        return True, out
    except Exception as e:
        return False, f"⚠️ Antigravity CLI check failed: {e}"

def run_birthday_reminders(date_str: str | None = None) -> tuple[bool, str, dict]:
    """Check for friend & family birthdays today and format interactive text reminder."""
    try:
        from tools.birthday_reminder import check_birthdays
        import tools.birthday_reminder as birthday_reminder
        has_bday, msg, _ = check_birthdays(date_str)
        decision_staged = getattr(birthday_reminder, "LAST_DECISION_STAGED", False)
        return has_bday, msg if has_bday else "", {"has_items": bool(has_bday), "decision_staged": decision_staged}
    except Exception as e:
        return False, f"⚠️ Birthday reminder check failed: {e}", {"has_items": False, "decision_staged": False}

def run_social_last_seen_review(days: int = 7) -> tuple[bool, str, dict]:
    """Review past week of calendar & communications for social gatherings and proposed Last Seen updates."""
    try:
        from tools.social_last_seen_review import identify_social_updates, format_review_message
        import tools.social_last_seen_review as social_last_seen_review
        updates = identify_social_updates(days=days)
        has_events, msg = format_review_message(updates)
        decision_staged = getattr(social_last_seen_review, "LAST_DECISION_STAGED", False)
        return has_events, msg if has_events else "", {"has_items": bool(has_events), "decision_staged": decision_staged}
    except Exception as e:
        return False, f"⚠️ Social review check failed: {e}", {"has_items": False, "decision_staged": False}

def run_core_friends_reminder(weeks: int = 8, as_of_date: str | None = None) -> tuple[bool, str, dict]:
    """Monthly check for local Core friends not seen in >= 8 weeks."""
    try:
        from tools.core_friends_reminder import check_core_friends_unseen
        import tools.core_friends_reminder as core_friends_reminder
        has_friends, msg, _ = check_core_friends_unseen(weeks=weeks, as_of_date=as_of_date)
        decision_staged = getattr(core_friends_reminder, "LAST_DECISION_STAGED", False)
        return has_friends, msg if has_friends else "", {"has_items": bool(has_friends), "decision_staged": decision_staged}
    except Exception as e:
        return False, f"⚠️ Core friends reminder check failed: {e}", {"has_items": False, "decision_staged": False}

def run_hardcode_regex_audit() -> tuple[bool, str]:
    """Monthly codebase audit for hardcoded rules and brittle regex heuristics."""
    try:
        from tools.hardcode_regex_auditor import CodebaseAuditor, format_audit_report
        auditor = CodebaseAuditor(TOOLS_DIR)
        findings = auditor.run_audit()
        report = format_audit_report(findings, TOOLS_DIR)
        return True, report
    except Exception as e:
        return False, f"⚠️ Hardcoded rule & regex audit failed: {e}"

def run_arr_queue_watchdog(auto_fix: bool = True, force: bool = False) -> tuple[bool, str]:
    """Audit Radarr & Sonarr queues for import errors and permission snags."""
    try:
        import importlib
        import tools.arr_queue_watchdog as aqw
        importlib.reload(aqw)
        has_activity, summary, _ = aqw.run_watchdog(auto_fix=auto_fix, force_dispatch=force)
        return True, summary if has_activity else "(nominal - 0 import failures)"
    except Exception as e:
        return False, f"⚠️ Arr queue watchdog failed: {e}"

def run_ha_reauth_watchdog(force: bool = False) -> tuple[bool, str]:
    """Audit Home Assistant integrations for re-auth and setup failures."""
    try:
        import importlib
        import tools.ha_reauth_watchdog as hrw
        importlib.reload(hrw)
        has_activity, summary, _ = hrw.check_ha_integrations(force=force)
        return True, summary if has_activity else "(nominal - 0 HA integration failures)"
    except Exception as e:
        return False, f"⚠️ Home Assistant re-auth watchdog failed: {e}"

def run_nest_lock_watchdog(force: bool = False) -> tuple[bool, str]:
    """Audit Nest x Yale Lock & Homebridge, auto-healing on stall or auth drop."""
    try:
        import importlib
        import tools.nest_lock_watchdog as nlw
        importlib.reload(nlw)
        healthy, summary, details = nlw.run_watchdog(force_heal=force)
        if healthy and details.get("heal_ok") and force:
            return True, f"🛠️ {summary}"
        elif healthy:
            return True, "(nominal - 0 lock failures)"
        else:
            return False, f"⚠️ Nest lock watchdog alert: {summary}"
    except Exception as e:
        return False, f"⚠️ Nest lock watchdog failed: {e}"

def run_prowlarr_watchdog(force: bool = False) -> tuple[bool, str]:
    """Audit Prowlarr indexers for disabled state, backoffs, and health errors."""
    try:
        import importlib
        import tools.prowlarr_watchdog as pw
        importlib.reload(pw)
        has_activity, summary, _ = pw.check_prowlarr(force=force)
        return True, summary if has_activity else "(nominal - 0 Prowlarr failures)"
    except Exception as e:
        return False, f"⚠️ Prowlarr watchdog failed: {e}"

def run_sabnzbd_watchdog(force: bool = False) -> tuple[bool, str]:
    """Audit SABnzbd queue, disk space, and failed unpacks."""
    try:
        import importlib
        import tools.sabnzbd_watchdog as sw
        importlib.reload(sw)
        has_activity, summary, _ = sw.check_sabnzbd(force=force)
        return True, summary if has_activity else "(nominal - 0 SABnzbd failures)"
    except Exception as e:
        return False, f"⚠️ SABnzbd watchdog failed: {e}"

def run_kometa_audit(force: bool = False) -> tuple[bool, str]:
    """Audit Kometa post-run logs for critical errors and missing TMDb IDs."""
    try:
        from tools.kometa_run_auditor import audit_kometa_log
        has_issues, summary, _ = audit_kometa_log(force=force)
        return True, summary if has_issues else "(nominal - Kometa run clean)"
    except Exception as e:
        return False, f"⚠️ Kometa log audit failed: {e}"

def run_bridge_watchdog(auto_heal: bool = True) -> tuple[bool, str]:
    """Audit Discord bridge gateway liveness, stuck processing turns, and orphan agy processes."""
    try:
        from tools.bridge_watchdog import check_bridge_health
        ok, summary, _ = check_bridge_health(auto_heal=auto_heal)
        return ok, summary
    except Exception as e:
        return False, f"⚠️ Bridge watchdog execution failed: {e}"

def run_weekly_grocery_staging() -> tuple[bool, str]:
    """Ingests Home Assistant todo.shopping_list + due staples and compiles the 1-click Whole Foods AFX cart."""
    try:
        from tools.grocery_manager import compile_weekly_manifest
        res = compile_weekly_manifest(include_due_staples=True)
        manifest = res["manifest"]
        assumed = res["assumed_in_stock"]
        url = res["url"]
        rec_names = res.get("recipe_names", [])
        meal_breakdowns = res.get("meal_breakdowns", [])
        warnings = res.get("quantity_warnings", [])
        costco = res.get("costco_items", [])
        rec_count = res.get("recipes_ingested", 0)
        rec_str = f", {rec_count} meal recipe(s)" if rec_count else ""

        lines = [
            f"🛒 **Weekly Whole Foods Cart Ready ({len(manifest)} Items)**",
            f"• Sourced: {res['tasks_ingested']} item(s) from tasks{rec_str}, {res['staples_ingested']} due staple(s)"
        ]
        if meal_breakdowns:
            lines.append("\n### 🍽️ Planned Meals & Grocery Items")
            for mb in meal_breakdowns:
                m_name = mb["name"]
                m_url = mb["url"]
                m_day = mb.get("day", "")
                m_items = mb.get("items", [])
                m_store = mb.get("store", "whole_foods")
                day_prefix = f" ({m_day})" if m_day else ""
                lines.append(f"- [**{m_name}**](<{m_url}>){day_prefix}:")
                if m_items:
                    label = "Costco Task List Items" if m_store == "costco" else "Whole Foods Cart Items"
                    lines.append(f"  - *{label}:* {', '.join(m_items)}")
                else:
                    lines.append("  - *Cart Items:* *(All ingredients in stock)*")
        elif rec_names:
            lines.append(f"🍽️ **Dinners:** {', '.join(rec_names)}")

        lines.append(f"\n🛒 [**1-Click Whole Foods Cart**](<{url}>)")
        lines.append("*Tap the link above to stage items in Amazon, choose your delivery window, and complete checkout.*")

        if warnings:
            lines.append("\n### ⚠️ Quantity & Packaging Checks")
            for w in warnings:
                lines.append(f"- {w}")

        if costco:
            lines.append(f"\n### 🛒 Separate Trip / Costco Due ({len(costco)} Items)")
            for c in costco:
                lines.append(f"- {c}")

        if assumed:
            lines.append("\n### 🧂 Assumed in Stock (Pantry)")
            lines.append(f"*{', '.join(assumed)}*")

        return True, "\n".join(lines)
    except Exception as e:
        return False, f"⚠️ Weekly grocery staging failed: {e}"

def run_weekly_meal_proposal() -> tuple[bool, str, dict]:
    """Generates the 3-dinner weekly rotation proposal for Tuesday, Thursday, and Sunday."""
    try:
        from tools.meal_planner_proposal import generate_proposal, format_proposal_markdown
        plan = generate_proposal()
        decision_staged = bool(plan.get("decision_staged", False)) if isinstance(plan, dict) else False
        return True, format_proposal_markdown(plan), {"has_items": True, "decision_staged": decision_staged}
    except Exception as e:
        return False, f"⚠️ Weekly meal planning proposal failed: {e}", {"has_items": False, "decision_staged": False}

def run_tasks_sync(quiet: bool = True, *args, **kwargs) -> tuple[bool, str, dict]:
    """Execute two-way synchronization between tasks.json and Google Tasks."""
    try:
        import importlib
        import tools.workspace_mcp
        if not hasattr(tools.workspace_mcp, "_resolve_secrets_path") or not os.path.exists(getattr(tools.workspace_mcp, "SECRETS_PATH", "")):
            importlib.reload(tools.workspace_mcp)
        from tools.google_tasks_sync import sync_tasks
        res = sync_tasks(quiet=quiet)
        ok = res.get("ok", True)
        # Completely silent sidecar: msg is empty so chat is never notified on nominal success.
        # Structured details are kept in res dict for durable execution logging.
        return ok, "", res
    except Exception as e:
        return False, f"⚠️ Google Tasks sync failed: {e}", {"error": str(e)}


def run_cubs_game_notifier(force: bool = False, test: bool = False) -> tuple[bool, str, any]:
    """Check Cubs schedule and notify ~10 minutes before first pitch."""
    try:
        from tools.cubs_notifier import check_cubs_game
        return check_cubs_game(force=force, test=test)
    except Exception as e:
        return False, f"⚠️ Cubs game notifier error: {e}", {"error": str(e)}

def run_agora_roguelike_pm(dispatch: bool = False, channel: str | int | None = None):
    """Assess the GitHub board, merge approved PRs, and assign tasks."""
    import subprocess
    cmd = ["python3", "/workspace/tools/agora_roguelike_pm.py"]
    if dispatch:
        cmd.append("--dispatch")
    if channel:
        cmd.extend(["--channel", str(channel)])
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = res.stdout.strip()
    if out == "(nominal - board state unchanged)":
        return res.returncode == 0, "", {"nominal": True}
    return res.returncode == 0, out, None


def run_gotchi_sprint(force: bool = False, test: bool = False, *args, **kwargs) -> tuple[bool, str, dict]:
    """Execute autonomous 15-minute software sprint cycle for GOTCHI (brockventures/gotchi)."""
    try:
        from tools.gotchi_sprint import execute_sprint_cycle, format_sprint_report
        data = execute_sprint_cycle(force=force, test=test)
        report = format_sprint_report(data)
        ok = data.get("status") in ("success", "skipped")
        return ok, report, data
    except Exception as e:
        return False, f"⚠️ Gotchi software sprint sidecar failed: {e}", {"error": str(e)}


def run_agora_roguelike_engineer():
    """Execute the engineering task assigned to Zero."""
    import subprocess
    res = subprocess.run(["python3", "/workspace/tools/agora_roguelike_engineer.py", "--run", "--force"], capture_output=True, text=True)
    return res.returncode == 0, res.stdout.strip(), None


def _cap_quant_summary(text: str, max_chars: int = 1850) -> str:
    """Guarantee quant sidecar update fits comfortably within 2,000 characters."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    out_lines = []
    cur_len = 0
    for line in text.splitlines():
        if cur_len + len(line) + 1 > max_chars - 50:
            out_lines.append("- *(Summary truncated to fit <2,000 chars)*")
            break
        out_lines.append(line)
        cur_len += len(line) + 1
    return "\n".join(out_lines)


def run_kalshi_paper_bot():
    """Execute unified quantitative paper trading bot (Books 1 & 2 decommissioned 2026-10-27; runs active expansion tracks)."""
    import subprocess
    try:
        # 1. Settle any remaining resolved contracts silently (Book 1 trading decommissioned)
        subprocess.run(["python3", "/workspace/tools/kalshi_paper_bot.py", "settle"], capture_output=True, text=True, check=False)
        fills = []

        # 2. Multi-Venue Quantitative Expansion (Active Books 3–5; Book 2 decommissioned)
        run_quant_expansion_sidecar("all", include_header=False)

        # 3. Generate clean, unified scan summary & refresh equity chart
        from tools.kalshi_performance_review import get_db, compute_portfolio_metrics, compute_quant_expansion_metrics, format_scan_summary
        from tools.quant_equity_chart import render_quant_equity_chart
        conn = get_db()
        metrics = compute_portfolio_metrics(conn)
        qm = compute_quant_expansion_metrics()
        conn.close()

        chart_path = str(render_quant_equity_chart())
        summary = format_scan_summary(metrics, qm=qm, new_fills=fills, max_chars=1850)
        return True, summary, {"chart_path": chart_path}
    except Exception as e:
        return False, f"⚠️ Kalshi quant sidecar error: {e}", {"error": str(e)}

def run_kalshi_performance_review(phase="all"):
    """Execute Kalshi paper trading settlement, performance review, and task board sync."""
    import subprocess
    try:
        # Trigger quant expansion sweep to settle positions, compound funding, and ratchet trailing stops (Book 1 & 2 trading decommissioned)
        subprocess.run(["python3", "/workspace/tools/sidecars.py", "quant"], capture_output=True, text=True, check=False)
        cmd = ["python3", "/workspace/tools/kalshi_performance_review.py", f"--phase={phase}"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = res.stdout.strip() if res.stdout else res.stderr.strip()
        chart_path = "/workspace/data/quant_equity_chart.png"
        try:
            from tools.quant_equity_chart import render_quant_equity_chart
            chart_path = str(render_quant_equity_chart())
        except Exception:
            pass
        return res.returncode == 0, _cap_quant_summary(output, max_chars=1850), {"chart_path": chart_path}
    except Exception as e:
        return False, f"⚠️ Kalshi review sidecar error: {e}", {"error": str(e)}

def run_kalshi_autoworker():
    """Execute autonomous 5-minute task board sprint cycle for Kalshi quant trading."""
    import subprocess
    try:
        res = subprocess.run(["python3", "/workspace/tools/kalshi_autoworker.py"], capture_output=True, text=True, check=False)
        output = res.stdout.strip() if res.stdout else res.stderr.strip()
        return res.returncode == 0, _cap_quant_summary(output, max_chars=1850), {}
    except Exception as e:
        return False, f"⚠️ Kalshi autoworker error: {e}", {"error": str(e)}

def run_quant_expansion_sidecar(target="all", include_header=True):
    """Execute multi-venue quantitative paper trading expansion tracks.
    target: 'all', 'cross_venue', 'polymarket', 'hyperliquid', 'trend'
    Note: Books 2 ('cross_venue') and 3 ('polymarket') are decommissioned from 'all' sweeps as of 2026-10-27.
    """
    import subprocess
    success = True

    if target == "cross_venue":
        res1 = subprocess.run(["python3", "/workspace/tools/quant_cross_venue_arb.py"], capture_output=True, text=True, check=False)
        if res1.returncode != 0:
            success = False

    if target == "polymarket":
        res2 = subprocess.run(["python3", "/workspace/tools/quant_polymarket_standalone.py"], capture_output=True, text=True, check=False)
        if res2.returncode != 0:
            success = False

    if target in ("all", "hyperliquid"):
        res3 = subprocess.run(["python3", "/workspace/tools/quant_hyperliquid_basis.py"], capture_output=True, text=True, check=False)
        if res3.returncode != 0:
            success = False

    if target in ("all", "trend"):
        res4 = subprocess.run(["python3", "/workspace/tools/quant_trend_momentum.py"], capture_output=True, text=True, check=False)
        if res4.returncode != 0:
            success = False

    try:
        from tools.kalshi_performance_review import get_db, compute_portfolio_metrics, compute_quant_expansion_metrics, format_scan_summary
        from tools.quant_equity_chart import render_quant_equity_chart
        conn = get_db()
        metrics = compute_portfolio_metrics(conn)
        qm = compute_quant_expansion_metrics()
        conn.close()
        chart_path = str(render_quant_equity_chart())
        summary = format_scan_summary(metrics, qm=qm, max_chars=1850)
        return success, summary, {"chart_path": chart_path}
    except Exception as e:
        return success, f"⚠️ Quant expansion scan completed (summary format error: {e})", {}

def run_sideproject_watcher(*args, **kwargs) -> tuple[bool, str, any]:
    """Execute autonomous 30-minute sprint cycle for Highball & Outpost side-projects."""
    try:
        from tools.sideproject_watcher import run_cycle
        force = kwargs.get("force", False)
        test = kwargs.get("test", False)
        return run_cycle(force=force, test=test)
    except Exception as e:
        return False, f"⚠️ Sideproject watcher error: {e}", {"error": str(e)}


def run_actual_bank_sync(test: bool = False, quiet: bool = True, notify: bool = False, *args, **kwargs) -> tuple[bool, str, dict]:
    """Execute automated Actual Budget bank sync and category rule dispatch."""
    try:
        from tools.actual_sync import sync_actual_budget
        ok, rep, meta = sync_actual_budget(test=test, quiet=quiet, notify=notify)
        # Silent sidecar: remain 100% silent on nominal/successful runs; return text only on error
        msg = rep if not ok else ""
        return ok, msg, meta or {}
    except Exception as e:
        return False, f"⚠️ Actual Budget sync sidecar failed: {e}", {"error": str(e)}


def run_cash_sweep_monitor(test: bool = False, post: bool = True, quiet: bool = False, *args, **kwargs) -> tuple[bool, str, dict]:
    """Execute monthly liquid cash sweep policy evaluation."""
    try:
        from tools.cash_sweep_monitor import evaluate_cash_sweep
        ok, rep, meta = evaluate_cash_sweep(test=test, post=post, quiet=quiet)
        return ok, rep, meta or {}
    except Exception as e:
        return False, f"⚠️ Cash sweep monitor sidecar failed: {e}", {"error": str(e)}


def run_model_variance_digest(test: bool = False, post: bool = True, quiet: bool = False, target_month: str = "", *args, **kwargs) -> tuple[bool, str, dict]:
    """Execute monthly financial model variance digest."""
    try:
        from tools.model_variance_digest import generate_variance_digest
        ok, rep, meta = generate_variance_digest(target_month=target_month, test=test, post=post, quiet=quiet)
        return ok, rep, meta or {}
    except Exception as e:
        return False, f"⚠️ Model variance digest sidecar failed: {e}", {"error": str(e)}

def run_pid_watchdog(force: bool = False, test: bool = False) -> tuple[bool, str, dict]:
    """Audit Host 2 and Host 1 kernel PID headroom and active process counts."""
    try:
        from tools.pid_watchdog import audit_pid_headroom
        has_issues, summary, data = audit_pid_headroom(force=force or test)
        msg = summary if (has_issues or force or test) else ""
        return not has_issues, msg, data
    except Exception as e:
        return False, f"⚠️ PID watchdog execution failed: {e}", {"error": str(e)}

# --------------------------------------------------------------------------
# CLI Dispatcher
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Daily Token Budget Watchdog
# --------------------------------------------------------------------------
def run_token_budget_watchdog(dispatch: bool = True, force: bool = False, target_day: str = None) -> tuple[bool, str, dict]:
    """Evaluate daily token consumption against 145M budget and alert if exceeded."""
    from tools.token_budget_watchdog import evaluate_daily_token_budget, dispatch_token_budget_alert
    try:
        report = evaluate_daily_token_budget(target_day=target_day)
        is_exceeded = report.get('is_exceeded', False)
        if is_exceeded or force:
            if dispatch:
                dispatch_token_budget_alert(report, channel='zero-ops', force=force)
            summary = f"⚠️ Daily token budget exceeded: {report.get('fleet_wire_tokens'):,} / 145M ({report.get('pct_daily')}%). Alert dispatched to #zero-ops."
            return True, summary, report
        else:
            summary = f"Daily token burn nominal: {report.get('fleet_wire_tokens'):,} / 145M ({report.get('pct_daily')}%). Rolling 7-day at {report.get('pct_weekly')}% of weekly limit. ✅"
            return True, summary, report
    except Exception as e:
        log.error(f"Token budget watchdog failed: {e}", exc_info=True)
        return False, f"Token budget watchdog error: {e}", {'error': str(e)}


def run_plex_weekly_digest(post: bool = True, tag_all: bool = True) -> tuple[bool, str]:
    """Execute Plex weekly new media digest and post/queue to #server-updates."""
    try:
        cmd = ["python3", "/workspace/tools/plex_weekly_digest.py"]
        if post:
            cmd.append("post")
        if tag_all:
            cmd.append("--tag-all")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = (res.stdout or "").strip() or (res.stderr or "").strip()
        return res.returncode == 0, out
    except Exception as e:
        return False, f"⚠️ Plex weekly digest failed: {e}"


def run_weekly_proactive_digest() -> tuple[bool, str]:
    """Generate the Option B weekly household radar & lookahead digest."""
    try:
        from tools.weekly_digest import generate_weekly_digest
        out = generate_weekly_digest()
        return True, out
    except Exception as e:
        return False, f"⚠️ Weekly proactive digest failed: {e}"


# --------------------------------------------------------------------------
# Workday Proactive Decision Cards & Self-Tuning Telemetry Engine Runners
# --------------------------------------------------------------------------
def run_morning_inbox_triage_card(dry_run: bool = False, test: bool = False, post_to_discord: bool = True) -> tuple[bool, str, dict]:
    """Execute morning Inbox & RCS actionable triage Decision Card workflow."""
    try:
        from tools.proactive_decision_cards import run_inbox_rcs_action_triage_card
        return run_inbox_rcs_action_triage_card(slot="morning", dry_run=dry_run, test=test, post_to_discord=post_to_discord)
    except Exception as e:
        log.error(f"Morning inbox triage card failed: {e}", exc_info=True)
        return False, f"⚠️ Morning inbox triage card error: {e}", {"error": str(e)}


def run_commute_inbox_triage_card(dry_run: bool = False, test: bool = False, post_to_discord: bool = True) -> tuple[bool, str, dict]:
    """Execute commute Inbox & RCS actionable triage Decision Card workflow."""
    try:
        from tools.proactive_decision_cards import run_inbox_rcs_action_triage_card
        return run_inbox_rcs_action_triage_card(slot="commute", dry_run=dry_run, test=test, post_to_discord=post_to_discord)
    except Exception as e:
        log.error(f"Commute inbox triage card failed: {e}", exc_info=True)
        return False, f"⚠️ Commute inbox triage card error: {e}", {"error": str(e)}


def run_family_calendar_radar_card(dry_run: bool = False, test: bool = False, post_to_discord: bool = True) -> tuple[bool, str, dict]:
    """Execute 48-hour Family Calendar & Action Radar Decision Card workflow."""
    try:
        from tools.proactive_decision_cards import run_family_calendar_radar_card as _run_radar
        return _run_radar(dry_run=dry_run, test=test, post_to_discord=post_to_discord)
    except Exception as e:
        log.error(f"Family calendar radar card failed: {e}", exc_info=True)
        return False, f"⚠️ Family calendar radar card error: {e}", {"error": str(e)}


def run_dinner_pivot_card(dry_run: bool = False, test: bool = False, post_to_discord: bool = True) -> tuple[bool, str, dict]:
    """Execute afternoon Dinner Defrost & Calendar Conflict Pivot Decision Card workflow."""
    try:
        from tools.proactive_decision_cards import run_dinner_pivot_card as _run_pivot
        return _run_pivot(dry_run=dry_run, test=test, post_to_discord=post_to_discord)
    except Exception as e:
        log.error(f"Dinner pivot card failed: {e}", exc_info=True)
        return False, f"⚠️ Dinner pivot card error: {e}", {"error": str(e)}


def run_actual_budget_anomaly_card(dry_run: bool = False, test: bool = False, post_to_discord: bool = True) -> tuple[bool, str, dict]:
    """Execute Actual Budget Anomaly & Overspend Detection Decision Card workflow."""
    try:
        from tools.proactive_decision_cards import run_actual_budget_anomaly_card as _run_budget
        return _run_budget(dry_run=dry_run, test=test, post_to_discord=post_to_discord)
    except Exception as e:
        log.error(f"Actual budget anomaly card failed: {e}", exc_info=True)
        return False, f"⚠️ Actual budget anomaly card error: {e}", {"error": str(e)}


def run_weather_energy_card(dry_run: bool = False, test: bool = False, post_to_discord: bool = True) -> tuple[bool, str, dict]:
    """Execute morning Weather, Air Quality & Pre-Adjustment Energy Decision Card workflow."""
    try:
        from tools.proactive_decision_cards import run_weather_energy_card as _run_weather
        return _run_weather(dry_run=dry_run, test=test, post_to_discord=post_to_discord)
    except Exception as e:
        log.error(f"Weather energy card failed: {e}", exc_info=True)
        return False, f"⚠️ Weather energy card error: {e}", {"error": str(e)}


def run_midweek_grocery_card(dry_run: bool = False, test: bool = False, post_to_discord: bool = True) -> tuple[bool, str, dict]:
    """Execute Wednesday Mid-Week Staple & Voice Grocery Replenishment Decision Card workflow."""
    try:
        from tools.proactive_decision_cards import run_midweek_grocery_card as _run_midweek
        return _run_midweek(dry_run=dry_run, test=test, post_to_discord=post_to_discord)
    except Exception as e:
        log.error(f"Midweek grocery card failed: {e}", exc_info=True)
        return False, f"⚠️ Midweek grocery card error: {e}", {"error": str(e)}


def run_evening_pm_build_card(dry_run: bool = False, test: bool = False, post_to_discord: bool = True) -> tuple[bool, str, dict]:
    """Execute evening PM Hobby & Build Decision Card workflow."""
    try:
        from tools.proactive_decision_cards import run_evening_pm_build_card as _run_pm
        return _run_pm(dry_run=dry_run, test=test, post_to_discord=post_to_discord)
    except Exception as e:
        log.error(f"Evening PM build card failed: {e}", exc_info=True)
        return False, f"⚠️ Evening PM build card error: {e}", {"error": str(e)}


def run_usage_telemetry_audit(days: int | float = 7, dry_run: bool = False, test: bool = False, post_cards: bool = True) -> tuple[bool, str, dict]:
    """Execute weekly Usage Pattern & Signal-to-Noise Telemetry Engine audit."""
    try:
        from tools.usage_telemetry import run_usage_telemetry_audit as _run_audit
        return _run_audit(days=days, dry_run=dry_run, test=test, post_cards=post_cards)
    except Exception as e:
        log.error(f"Usage telemetry audit failed: {e}", exc_info=True)
        return False, f"⚠️ Usage telemetry audit error: {e}", {"error": str(e)}


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "heartbeat"
    if action in ("cubs", "cubs_game", "cubs_notifier"):
        force = "--force" in sys.argv or "-f" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("cubs_game_notifier", "Cubs Game Day Notifier", run_cubs_game_notifier, force=force, test=test)
        if rep:
            print(rep)
    elif action in ("arr_queue", "arr_watchdog", "queue"):
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, _ = run_sidecar_job("arr_queue_watchdog", "Arr Queue Watchdog", run_arr_queue_watchdog, force=force)
        if rep and rep != "(nominal - 0 import failures)":
            print(rep)
    elif action in ("ha_reauth", "reauth"):
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, _ = run_sidecar_job("ha_reauth_watchdog", "HA Re-Auth Watchdog", run_ha_reauth_watchdog, force=force)
        if rep and rep != "(nominal - 0 HA integration failures)":
            print(rep)
    elif action in ("nest_lock", "lock_watchdog", "lock_heal"):
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, _ = run_sidecar_job("nest_lock_watchdog", "Nest Lock Watchdog", run_nest_lock_watchdog, force=force)
        if rep and rep != "(nominal - 0 lock failures)":
            print(rep)
    elif action in ("prowlarr", "indexers"):
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, _ = run_sidecar_job("prowlarr_watchdog", "Prowlarr Indexer Watchdog", run_prowlarr_watchdog, force=force)
        if rep and rep != "(nominal - 0 Prowlarr failures)":
            print(rep)
    elif action in ("sabnzbd", "sab"):
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, _ = run_sidecar_job("sabnzbd_watchdog", "SABnzbd Watchdog", run_sabnzbd_watchdog, force=force)
        if rep and rep != "(nominal - 0 SABnzbd failures)":
            print(rep)
    elif action in ("kometa_audit", "kometa"):
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, _ = run_sidecar_job("kometa_audit", "Kometa Post-Run Audit", run_kometa_audit, force=force)
        if rep and rep != "(nominal - Kometa run clean)":
            print(rep)
    elif action in ("bridge_watchdog", "bridge_check", "watchdog_bridge"):
        no_heal = "--no-heal" in sys.argv
        ok, rep, _ = run_sidecar_job("bridge_watchdog", "Bridge Liveness Watchdog", run_bridge_watchdog, auto_heal=not no_heal)
        if rep:
            print(rep)
    elif action == "heartbeat":
        ok, rep, _ = run_sidecar_job("heartbeat", "Heartbeat Sweep", run_heartbeat_sweep)
        print(rep)
    elif action == "triage":
        ok, rep, _ = run_sidecar_job("triage", "Nightly Triage & Briefing", run_nightly_triage)
        print(rep)
    elif action == "nas_logs":
        ok, rep, _ = run_sidecar_job("nas_logs", "NAS Log Review", run_nas_log_review)
        print(rep)
    elif action == "plex":
        ok, rep, _ = run_sidecar_job("plex", "Plex Session Cleanup", run_plex_session_cleanup)
        if rep.strip():
            print(rep)
    elif action == "reminders":
        ok, rep, _ = run_sidecar_job("reminders", "Dated Reminders", run_dated_reminders)
        print(rep or "(no reminders due today)")
    elif action == "birthdays":
        date_arg = sys.argv[2] if len(sys.argv) > 2 else None
        ok, rep, _ = run_sidecar_job("daily_birthday_reminder", "Daily Birthday Reminder", run_birthday_reminders, date_str=date_arg)
        if rep:
            print(rep)
    elif action == "social_review":
        days_arg = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 7
        ok, rep, _ = run_sidecar_job("weekly_social_review", "Weekly Social & Last Seen Review", run_social_last_seen_review, days=days_arg)
        if rep:
            print(rep)
    elif action in ("core_friends", "core_reconnect"):
        weeks_arg = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8
        ok, rep, _ = run_sidecar_job("monthly_core_friends_reminder", "Monthly Core Friends Social Reminder", run_core_friends_reminder, weeks=weeks_arg)
        if rep:
            print(rep)
    elif action == "ev9":
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, plot = run_sidecar_job("ev9", "EV9 Listing Monitor", run_ev9_monitor, force_digest=force)
        if rep:
            print(rep)
        if plot:
            print(f"\n[Trend plot generated: {plot}]")
    elif action == "marketing":
        ok, rep, _ = run_sidecar_job("marketing", "Marketing Email Sweep", run_marketing_sweep, force=True)
        print(rep)
    elif action == "doctor":
        from tools.memory_manager import run_memory_doctor
        ok, rep, _ = run_sidecar_job("doctor", "Memory Doctor Audit", lambda: run_memory_doctor(auto_remediate=True))
        print(rep)
    elif action == "dream":
        from tools.memory_manager import run_dreaming_consolidation
        ok, rep, _ = run_sidecar_job("dream", "Dreaming Consolidation", run_dreaming_consolidation)
        print(rep)
    elif action == "battery":
        ok, rep, _ = run_sidecar_job("ha_battery", "HA Battery Check", run_ha_battery_check)
        print(rep)
    elif action in ("ha_anomaly", "ha_anomaly_check", "anomaly"):
        ok, rep, _ = run_sidecar_job("ha_anomaly_check", "HA Telemetry Anomaly Check", run_ha_anomaly_check)
        print(rep)
    elif action == "storage":
        ok, rep, _ = run_sidecar_job("nas_storage", "NAS Storage Check", run_nas_storage_check)
        print(rep)
    elif action in ("backup_host2", "host2_backup", "backup2"):
        verbose = "-v" in sys.argv or "--verbose" in sys.argv
        ok, rep, extra = run_sidecar_job("host2_backup", "Host 2 Local USB Backup", run_host2_backup, quiet=not verbose)
        if not ok:
            print(rep)
        elif verbose and extra and isinstance(extra, dict) and extra.get("output"):
            print(extra["output"])
    elif action in ("backup_host1", "host1_backup", "backup1"):
        verbose = "-v" in sys.argv or "--verbose" in sys.argv
        ok, rep, extra = run_sidecar_job("host1_backup", "Host 1 Local USB Backup", run_host1_backup, quiet=not verbose)
        if not ok:
            print(rep)
        elif verbose and extra and isinstance(extra, dict) and extra.get("output"):
            print(extra["output"])
    elif action == "ha_update":
        ok, rep, _ = run_sidecar_job("ha_update_check", "HA Update Check", run_ha_update_check)
        print(rep or "HA up to date.")
    elif action == "dockhand":
        ok, rep, _ = run_sidecar_job("dockhand_check", "Dockhand Image Check", run_dockhand_update_check)
        print(rep or "Dockhand up to date.")
    elif action == "antigravity":
        ok, rep, _ = run_sidecar_job("update_antigravity", "Antigravity CLI Check", run_antigravity_check)
        print(rep or "Antigravity up to date.")
    elif action == "morning":
        from tools.morning_dispatcher import dispatch_morning_topic
        ok, rep, _ = run_sidecar_job("morning_topic_rotation", "Crab Cavern Morning Topic Rotation", dispatch_morning_topic)
        print(rep)
    elif action in ("market_standup", "standup"):
        from tools.market_standup import dispatch_market_standup
        ok, rep, _ = run_sidecar_job("market_standup", "Market Sandbox Autonomous Daily Standup", dispatch_market_standup)
        print(rep)
    elif action in ("hardcode_audit", "regex_audit", "code_audit"):
        ok, rep, _ = run_sidecar_job("monthly_hardcode_regex_audit", "Monthly Hardcoded Rule & Regex Audit", run_hardcode_regex_audit)
        print(rep)
    elif action in ("agora_steering", "steering"):
        from tools.agora_steering import dispatch_agora_steering
        ok, rep, _ = run_sidecar_job("agora_steering", "AGORA Daily Steering Briefing", dispatch_agora_steering)
        print(rep)
    elif action in ("rollover", "session_rollover"):
        from tools.session_rollover import run_daily_session_rollover
        ok, rep, _ = run_sidecar_job("session_rollover", "Daily Multi-Channel Session Rollover", run_daily_session_rollover)
        print(rep)
    elif action in ("grocery_staging", "grocery", "groceries", "cart"):
        ok, rep, _ = run_sidecar_job("weekly_grocery_staging", "Weekly Whole Foods Grocery Staging", run_weekly_grocery_staging)
        if rep:
            print(rep)
    elif action in ("meal_proposal", "meal_planner", "meals", "mealplan"):
        ok, rep, _ = run_sidecar_job("weekly_meal_proposal", "Weekly 3-Dinner Meal Proposal", run_weekly_meal_proposal)
        if rep:
            print(rep)
    elif action in ("tasks_sync", "sync_tasks", "task_sync"):
        ok, rep, _ = run_sidecar_job("google_tasks_sync", "Google Tasks Two-Way Sync", run_tasks_sync)
        if rep:
            print(rep)
        else:
            print("(nominal - all tasks in sync)")
    elif action in ("agora_roguelike_pm", "agora_pm"):
        do_dispatch = "--dispatch" in sys.argv
        target_ch = None
        for i, a in enumerate(sys.argv):
            if a == "--channel" and i + 1 < len(sys.argv):
                target_ch = sys.argv[i + 1]
        ok, rep, _ = run_sidecar_job(
            "agora_roguelike_pm",
            "Agora Roguelike PM & Task Assignment",
            lambda: run_agora_roguelike_pm(dispatch=do_dispatch, channel=target_ch)
        )
        if rep:
            print(rep)
        else:
            print("(nominal - board state unchanged)")
    elif action in ("gotchi_sprint", "gotchi"):
        force = "--force" in sys.argv or "-f" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("gotchi_sprint", "GOTCHI Software Sprint Coordinator", run_gotchi_sprint, force=force, test=test)
        if rep:
            print(rep)
    elif action in ("agora_roguelike_engineer", "agora_engineer", "agora_eng"):
        ok, rep, _ = run_sidecar_job("agora_roguelike_engineer", "Agora Roguelike Engineering Execution", run_agora_roguelike_engineer)
        if rep:
            print(rep)
    elif action in ("agora_sprint", "agora_autoworker", "agora_worker"):
        res = subprocess.run(["python3", "/workspace/tools/agora_autoworker.py"], capture_output=True, text=True)
        if res.stdout:
            print(res.stdout.strip())
        if res.stderr:
            print(res.stderr.strip(), file=sys.stderr)
    elif action in ("kalshi", "kalshi_paper", "kalshi_bot", "kalshi_midday"):
        ok, rep, _ = run_sidecar_job("kalshi_paper_bot", "Kalshi Weather Quant Paper Bot", run_kalshi_paper_bot)
        if rep:
            print(rep)
    elif action in ("kalshi_review", "kalshi_evening"):
        ok, rep, _ = run_sidecar_job("kalshi_evening_review", "Kalshi Paper Trading Evening Review & Model Sync", lambda: run_kalshi_performance_review(phase="evening"))
        if rep:
            print(rep)
    elif action in ("kalshi_audit", "kalshi_night"):
        ok, rep, _ = run_sidecar_job("kalshi_night_audit", "Kalshi Nightly Settlement & Quant Standup", lambda: run_kalshi_performance_review(phase="night"))
        if rep:
            print(rep)
    elif action in ("kalshi_sprint", "kalshi_autoworker", "kalshi_worker"):
        ok, rep, _ = run_sidecar_job("kalshi_quant_sprint", "Kalshi Task Board Autonomous Sprint", run_kalshi_autoworker)
        if rep:
            print(rep)
    elif action in ("quant", "quant_expansion", "quant_sweep", "quant_all"):
        ok, rep, _ = run_sidecar_job("quant_expansion_sweep", "Multi-Venue Quantitative Expansion Sweep", lambda: run_quant_expansion_sidecar("all"))
        if rep:
            print(rep)
    elif action in ("quant_cross_venue", "cross_venue"):
        ok, rep, _ = run_sidecar_job("quant_cross_venue", "Cross-Venue Arbitrage (Kalshi vs Polymarket)", lambda: run_quant_expansion_sidecar("cross_venue"))
        if rep:
            print(rep)
    elif action in ("quant_polymarket", "polymarket"):
        ok, rep, _ = run_sidecar_job("quant_polymarket", "Polymarket Standalone Inefficiencies", lambda: run_quant_expansion_sidecar("polymarket"))
        if rep:
            print(rep)
    elif action in ("quant_hyperliquid", "hyperliquid", "basis"):
        ok, rep, _ = run_sidecar_job("quant_hyperliquid", "Hyperliquid Basis Cash-and-Carry", lambda: run_quant_expansion_sidecar("hyperliquid"))
        if rep:
            print(rep)
    elif action in ("quant_trend", "trend", "momentum"):
        ok, rep, _ = run_sidecar_job("quant_trend", "Multi-Day Trend Momentum Swings", lambda: run_quant_expansion_sidecar("trend"))
        if rep:
            print(rep)
    elif action in ("sideproject_watcher", "sideproject", "sideproject_sprint"):
        force = "--force" in sys.argv or "-f" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("sideproject_watcher", "Side-Project Autonomous Sprint Watcher", run_sideproject_watcher, force=force, test=test)
        if rep:
            print(rep)
    elif action in ("hourly_agora_ping", "agora_ping", "hourly_ping"):
        from tools.hourly_agora_ping import dispatch_hourly_agora_ping
        test = "--test" in sys.argv or "--dry-run" in sys.argv
        ok, rep, _ = run_sidecar_job("hourly_agora_ping", "Hourly Agora Standup Ping", dispatch_hourly_agora_ping, dry_run=test)
        if rep:
            print(rep)
    elif action in ("actual_sync", "sync_finances", "actual_bank_sync"):
        test = "--test" in sys.argv or "-t" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv or "--force" not in sys.argv
        ok, rep, _ = run_sidecar_job("actual_bank_sync", "Actual Budget Daily Bank Sync", run_actual_bank_sync, test=test, quiet=quiet)
        if rep:
            print(rep)
        elif not quiet:
            print("(nominal - 0 new transactions)")
    elif action in ("cash_sweep", "sweep", "sweep_monitor", "cash_sweep_monitor"):
        test = "--test" in sys.argv or "-t" in sys.argv
        post = "--post" in sys.argv or "-p" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        ok, rep, _ = run_sidecar_job("cash_sweep_monitor", "Monthly Cash Sweep Policy Monitor", run_cash_sweep_monitor, test=test, post=post, quiet=quiet)
        if rep:
            print(rep)
    elif action in ("variance_digest", "variance", "budget_audit", "model_variance"):
        test = "--test" in sys.argv or "-t" in sys.argv
        post = "--post" in sys.argv or "-p" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        month = ""
        for i, a in enumerate(sys.argv):
            if a in ("--month", "-m") and i + 1 < len(sys.argv):
                month = sys.argv[i+1]
        ok, rep, _ = run_sidecar_job("monthly_variance_digest", "Monthly Financial Model Variance Digest", run_model_variance_digest, test=test, post=post, quiet=quiet, target_month=month)
        if rep:
            print(rep)
    elif action in ("token_budget_watchdog", "token_budget", "tokenwatchdog"):
        test = "--test" in sys.argv or "-t" in sys.argv
        force = "--force" in sys.argv or "-f" in sys.argv
        dispatch = "--dispatch" in sys.argv or "-d" in sys.argv or force
        day = None
        for i, a in enumerate(sys.argv):
            if a in ("--day",) and i + 1 < len(sys.argv):
                day = sys.argv[i+1]
        ok, rep, _ = run_sidecar_job("token_budget_watchdog", "Daily Token Budget Watchdog", run_token_budget_watchdog, dispatch=dispatch, force=force, target_day=day)
        if rep:
            print(rep)
    elif action in ("reddit_fantasy", "reddit_scan", "reddit_fantasy_scanner", "reddit"):
        from tools.reddit_fantasy_scanner import run_reddit_fantasy_scanner
        test = "--test" in sys.argv or "-t" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        post_discord = "--post-discord" in sys.argv or "--post" in sys.argv or "-p" in sys.argv
        ok, rep, _ = run_sidecar_job("reddit_fantasy_scanner", "Reddit Fantasy Baseball Scanner", run_reddit_fantasy_scanner, test=test, quiet=quiet, post_discord=post_discord)
        if rep:
            print(rep)
    elif action in ("pid_watchdog", "pid_check", "pids", "pid"):
        force = "--force" in sys.argv or "-f" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("pid_watchdog", "PID & Process Headroom Watchdog", run_pid_watchdog, force=force, test=test)
        if rep:
            print(rep)
    elif action in ("plex_weekly_digest", "plex_digest"):
        post = "--no-post" not in sys.argv and "--test" not in sys.argv
        ok, rep, _ = run_sidecar_job("plex_weekly_digest", "Plex Weekly New Media Digest", run_plex_weekly_digest, post=post, tag_all=True)
        if rep:
            print(rep)
    elif action in ("plex_audiobook_enricher", "audiobook_enricher", "audiobooks"):
        from tools.plex_audiobook_enricher import run_plex_audiobook_enricher
        force = "--force" in sys.argv or "-f" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        ok, rep, _ = run_sidecar_job("plex_audiobook_enricher", "Plex Audiobook Enricher & Cover Badges", run_plex_audiobook_enricher, force=force, quiet=quiet)
        if rep:
            print(rep)
    elif action in ("weekly_proactive_digest", "weekly_digest"):
        ok, rep, _ = run_sidecar_job("weekly_proactive_digest", "Option B Weekly Proactive Digest", run_weekly_proactive_digest)
        if rep:
            print(rep)
    elif action in ("quota", "daily_quota_card", "quota_card"):
        from tools.quota_card import run_quota_card
        test = "--test" in sys.argv or "-t" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        ok, rep, _ = run_sidecar_job("daily_quota_card", "Daily Gemini Quota Card", run_quota_card, test=test, quiet=quiet)
        if rep:
            print(rep)
    elif action in ("quota_poller", "quota_poll", "poller_quota"):
        from tools.quota_poller import run_quota_poller
        test = "--test" in sys.argv or "-t" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        ok, rep, _ = run_sidecar_job("quota_poller", "10-Minute Quota Usage Poller", run_quota_poller, test=test, quiet=quiet)
        if rep:
            print(rep)
    elif action in ("context_digest", "gemini_context_digest", "email_digest"):
        from tools.gemini_context_digest import run_daily_context_digest
        test = "--test" in sys.argv or "-t" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        dry_run = "--dry-run" in sys.argv
        ok, rep, _ = run_sidecar_job("gemini_context_digest", "Daily Zero & Ivy Context Email Digest", run_daily_context_digest, dry_run=dry_run, test=test, quiet=quiet)
        if rep:
            print(rep)
    elif action in ("personal_data_sync", "personal_sync", "sync_profiles"):
        from tools.personal_data_sync import run_personal_data_sync
        test = "--test" in sys.argv or "-t" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        ok, rep, _ = run_sidecar_job("personal_data_sync", "Weekly Personal Data & Gaming Profile Sync", run_personal_data_sync, test=test, quiet=quiet)
        if rep:
            print(rep)
    elif action in ("resident_agents_watcher", "resident_agents", "ra_watcher"):
        from tools.resident_agents_watcher import run_resident_agents_watcher
        test = "--test" in sys.argv or "-t" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        no_post = "--no-post" in sys.argv
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, _ = run_sidecar_job(
            "resident_agents_watcher",
            "r/ResidentAgents Watcher & Auto-Approver",
            run_resident_agents_watcher,
            test=test,
            quiet=quiet,
            post_discord=not no_post,
            force_pixel=force,
        )
        if rep:
            print(rep)
    elif action in ("resident_agents_daily_tip", "resident_agents_tip", "ra_daily_tip"):
        from tools.resident_agents_watcher import run_resident_agents_daily_tip
        test = "--test" in sys.argv or "-t" in sys.argv
        quiet = "--quiet" in sys.argv or "-q" in sys.argv
        no_post = "--no-post" in sys.argv
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, _ = run_sidecar_job(
            "resident_agents_daily_tip",
            "r/ResidentAgents Daily Tips & Tricks Megathread Rotation",
            run_resident_agents_daily_tip,
            test=test,
            quiet=quiet,
            post_discord=not no_post,
            force=force,
        )
        if rep:
            print(rep)
    elif action in ("morning_inbox_triage_card", "morning_inbox_triage"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("morning_inbox_triage_card", "Morning Inbox & RCS Triage Card", run_morning_inbox_triage_card, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action in ("commute_inbox_triage_card", "commute_inbox_triage"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("commute_inbox_triage_card", "Commute Inbox & RCS Triage Card", run_commute_inbox_triage_card, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action in ("family_calendar_radar_card", "family_calendar_radar"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("family_calendar_radar_card", "48h Family Calendar Radar Card", run_family_calendar_radar_card, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action in ("dinner_pivot_card", "dinner_pivot"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("dinner_pivot_card", "Afternoon Dinner Pivot Card", run_dinner_pivot_card, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action in ("actual_budget_anomaly_card", "budget_anomaly_card"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("actual_budget_anomaly_card", "Actual Budget Anomaly Card", run_actual_budget_anomaly_card, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action in ("weather_energy_card", "weather_energy"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("weather_energy_card", "Weather & Energy Pre-Adjustment Card", run_weather_energy_card, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action in ("midweek_grocery_card", "midweek_grocery"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("midweek_grocery_card", "Mid-Week Staple & Voice Grocery Card", run_midweek_grocery_card, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action in ("evening_pm_build_card", "evening_pm_build"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("evening_pm_build_card", "Evening PM Hobby Build Card", run_evening_pm_build_card, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action in ("usage_telemetry_audit", "usage_telemetry"):
        dry_run = "--dry-run" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("usage_telemetry_audit", "Usage Pattern & Signal-to-Noise Telemetry Audit", run_usage_telemetry_audit, dry_run=dry_run, test=test)
        if rep:
            print(rep)
    elif action == "status":
        print(format_sidecar_status_summary())
    elif action == "history":
        entries = get_execution_history(limit=20)
        print(json.dumps(entries, indent=2))
    else:
        print(f"Unknown action: {action}")
