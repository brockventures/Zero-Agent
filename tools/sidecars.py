#!/usr/bin/env python3
"""Comprehensive Sidecar Execution Engine for Zero (Ivy-AG).

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
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
        "id": "zwave-smartthings-decision-2026-09-08",
        "due": "2026-09-08",
        "message": (
            "⏰ **SmartThings replacement reminder** — you asked to revisit this in 30 days (set 08-09).\n"
            "Options: **HomeSeer Z-NET PRO** (standalone, replaces ZWA-2 stick) or **SLZB-06U + ZWA-2 combo** "
            "(bridge + stick). Dropping SmartThings before the Oct 2026 API paywall for Z-Wave blinds."
        ),
    },
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
def _atomic_write_json(file_path: Path, data: any):
    """Write data to a JSON file atomically via a temporary file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(f".tmp.{os.getpid()}")
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

    # 3. Synchronize schedule.json if KarakosScheduler tracks this sidecar
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
            extra = {"has_items": has_items, "has_due": has_items}
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

def get_recent_job_failures(since_hours: float = 24.0) -> list[dict]:
    """Retrieve all failed, degraded, or errored jobs within the last N hours."""
    entries = get_execution_history(limit=MAX_LOG_ENTRIES, since_hours=since_hours)
    return [e for e in entries if e.get("status") in ("warning", "error")]

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

    active_failures = [
        entry for entry in latest_map.values()
        if entry.get("status") in ("warning", "error")
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
                        "last_run_pt": last_pt,
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

# --------------------------------------------------------------------------
# 1. Heartbeat Sweep
# --------------------------------------------------------------------------
def run_heartbeat_sweep() -> tuple[bool, str]:
    HOST_1_IP, HOST_2_IP, _ = _resolve_nas_config()
    failures = []
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")

    # 1. Host1 (.82) Containers
    sb_res = json.loads(nas_docker("ps", HOST_1_IP))
    if not sb_res.get("ok"):
        failures.append(f"Host1 ({HOST_1_IP}) Docker unreachable: {sb_res.get('error')}")
    else:
        for line in sb_res.get("output", "").splitlines():
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if parts:
                name = parts[0]
                status = parts[1] if len(parts) > 1 else ""
                if name not in SERVERBROCK_STOPPED_ALLOWLIST and not status.startswith("Up"):
                    failures.append(f"Host1 ({HOST_1_IP}) container `{name}` is {status}")

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

    # 6. Persistent MCP Daemon Health
    try:
        from tools.mcp_daemon import get_status
        mcp_stat = get_status()
        if not mcp_stat.get("healthy"):
            failures.append(f"Persistent MCP Daemon degraded or stopped: {mcp_stat.get('servers')}")
    except Exception as me:
        failures.append(f"Persistent MCP Daemon check failed: {me}")


    if not failures:
        return True, f"🟢 **Heartbeat Sweep Healthy** — {now_str}\nAll containers, ports, Home Assistant, and external monitors healthy across `.82` and `.84`."

    report = [f"🚨 **Heartbeat Alert — Infrastructure Degraded** [{now_str}]", ""]
    for f in failures:
        report.append(f"- ⚠️ {f}")
    return False, "\n".join(report)

# --------------------------------------------------------------------------
# 2. Nightly Triage Briefing (with Automated Failure Surfacing)
# --------------------------------------------------------------------------
def _triage_unread_emails(messages: list[dict]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Triage unread emails dynamically using LLM reasoning with robust heuristic fallback."""
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
    for m in candidates[:12]:
        sender_name, sender_addr = email.utils.parseaddr(m.get("from", ""))
        sender_clean = sender_name.strip().strip('"').strip("'") or sender_addr
        summary_input.append({
            "from": sender_clean,
            "subject": m.get("subject", ""),
            "snippet": m.get("snippet", "")[:160]
        })

    prompt = (
        f"You are Zero, an executive systems engineer and assistant triaging Ryan's unread inbox.\n"
        f"Unread emails:\n{json.dumps(summary_input, indent=2)}\n\n"
        f"Return ONLY valid JSON matching this schema:\n"
        f"{{\n"
        f"  \"calendar_proposals\": [\"- **<Date/Time or Deadline>** — *<Title>* (<Sender/Vendor>)\"],\n"
        f"  \"priority\": [\"- **<Sender>**: *\\\"<Subject>\\\"* (<1-sentence key takeaway>)\"],\n"
        f"  \"newsletters\": [\"- **<Sender>**: *\\\"<Subject>\\\"*\"],\n"
        f"  \"transactional\": [\"- **<Sender>**: *\\\"<Subject>\\\"*\"]\n"
        f"}}"
    )
    try:
        res = subprocess.run(
            ["agy", "--model=gemini-3.8-flash-low", "--disable-slash-commands", f"-p={prompt}"],
            capture_output=True, text=True, timeout=15
        )
        if res.returncode == 0 and res.stdout.strip():
            m_json = re.search(r"\{[\s\S]*\}", res.stdout)
            if m_json:
                data = json.loads(m_json.group(0))
                proposed_calendar = data.get("calendar_proposals", [])
                priority_lines = data.get("priority", [])
                newsletter_lines = data.get("newsletters", [])
                transactional_lines = data.get("transactional", [])
                return proposed_calendar, priority_lines, newsletter_lines, transactional_lines
    except Exception as e:
        print(f"[Triage] LLM inbox triage fallback: {e}")

    for m in candidates:
        sender = m.get("from", "")
        subj = m.get("subject", "").strip()
        snippet = m.get("snippet", "").strip()
        s_low = sender.lower()
        sub_low = subj.lower()
        snip_low = snippet.lower()

        sender_name, sender_addr = email.utils.parseaddr(sender)
        sender_clean = sender_name.strip().strip('"').strip("'") or sender_addr

        exp_match = re.search(r"(?:expiration|expires|valid until|due by|exp(?:ire)?s?):\s*(\d{1,2}/\d{1,2}/\d{2,4})", snippet, re.IGNORECASE)
        time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM))\s+(?:on\s+)?([A-Za-z]+,?\s+[A-Za-z]+\s+\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})", snippet, re.IGNORECASE)
        if exp_match:
            proposed_calendar.append(f"- **By {exp_match.group(1)}** — *{subj}* ({sender_clean})")
        elif time_match:
            proposed_calendar.append(f"- **{time_match.group(2)} @ {time_match.group(1)}** — *{subj}* ({sender_clean})")
        elif any(k in sub_low or k in snip_low for k in ["appointment", "reservation", "confirmed for", "booking confirmed", "installation window", "estimate"]):
            date_m = re.search(r"\b([A-Za-z]+,\s*[A-Za-z]+\s*\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})\b", snippet)
            d_str = date_m.group(1) if date_m else "Upcoming"
            proposed_calendar.append(f"- **{d_str} (Proposed)** — *{subj}* ({sender_clean})")

        clean_snip = html.unescape(snippet).replace("\n", " ").strip()
        clean_snip = re.sub(r"\s+", " ", clean_snip)
        if len(clean_snip) > 85:
            clean_snip = clean_snip[:82] + "..."

        if any(kw in s_low or kw in sub_low for kw in ["newsletter", "chronicle", "digest", "projections report", "bb pro", "daily fantasy", "roundup", "weekly"]):
            newsletter_lines.append(f"- **{sender_clean}**: *\"{subj}\"*")
        elif any(kw in s_low or kw in sub_low for kw in ["amazon", "order", "delivery", "shipping", "receipt", "invoice", "dogfood", "stardust", "payment received", "tracking"]):
            transactional_lines.append(f"- **{sender_clean}**: *\"{subj}\"*")
        else:
            if clean_snip and subj and clean_snip.lower() != subj.lower():
                priority_lines.append(f"- **{sender_clean}**: *\"{subj}\"*\n  `{clean_snip}`")
            else:
                priority_lines.append(f"- **{sender_clean}**: *\"{subj or clean_snip}\"*")

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

    # 2. Gmail Triage (Categorized executive inbox triage)
    gmail_res = json.loads(gmail_search("in:inbox is:unread", 30))
    messages = gmail_res.get("messages", [])

    priority_lines = []
    newsletter_lines = []
    transactional_lines = []
    proposed_calendar = []
    self_filtered_count = 0
    vendor_filtered_count = 0

    proposed_calendar, priority_lines, newsletter_lines, transactional_lines = _triage_unread_emails(messages)

    inbox_lines = []
    if priority_lines:
        inbox_lines.extend(priority_lines)
    else:
        inbox_lines.append("- *(No urgent human action items)*")

    if newsletter_lines or transactional_lines:
        inbox_lines.append("")
        inbox_lines.append("*Digests & Low-Priority:*")
        if newsletter_lines:
            inbox_lines.append(f"- 📰 **Newsletters ({len(newsletter_lines)})**:")
            for nl in newsletter_lines:
                inbox_lines.append(f"  {nl}")
        if transactional_lines:
            inbox_lines.append(f"- 📦 **Orders & System Updates ({len(transactional_lines)})**:")
            for tl in transactional_lines:
                inbox_lines.append(f"  {tl}")

    filter_note = []
    if self_filtered_count > 0:
        filter_note.append(f"{self_filtered_count} self-sent note(s)/link(s)")
    if vendor_filtered_count > 0:
        filter_note.append(f"{vendor_filtered_count} lingering reminder(s)")
    if filter_note:
        joined_note = " and ".join(filter_note)
        inbox_lines.append(f"\n*({joined_note} filtered)*")

    # 3. Scheduled Sidecar Job Failure Check (Automated Surfacing of Active Unresolved Issues)
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
    infra_note = "All containers & network ports reporting healthy." if infra_ok else "Degraded containers detected (see alert)."

    # Homelab container counts
    sb_res = json.loads(nas_docker("ps", HOST_1_IP))
    bs2_res = json.loads(nas_docker("ps", HOST_2_IP))
    sb_up = sum(1 for line in (sb_res.get("output") or "").splitlines() if "\t" in line and "Up" in line)
    bs2_up = sum(1 for line in (bs2_res.get("output") or "").splitlines() if "\t" in line and "Up" in line)

    # Baseball pipeline refresh timestamp
    bb_path = os.environ.get("BASEBALL_REFRESH_PATH", "/docker/baseball/shiny_app/data/last_refresh.txt")
    bb_res = _ssh_cmd(HOST_2_IP, f"cat {bb_path} 2>/dev/null || cat /docker/baseball/shiny_app/data/last_refresh.txt 2>/dev/null || echo 'unavailable'")
    bb_ts = bb_res.stdout.strip() if bb_res.returncode == 0 and bb_res.stdout.strip() else "unavailable"

    # Sidecar summary line based on active registered jobs
    total_jobs = len(health.get("latest_by_job", {}))
    failing_count = len(active_failures)
    if failing_count == 0:
        sidecar_note = f"All {total_jobs} registered sidecars operational & healthy. ✅" if total_jobs > 0 else "All scheduled sidecars healthy. ✅"
    else:
        sidecar_note = f"⚠️ {total_jobs - failing_count}/{total_jobs} sidecars operational ({failing_count} currently degraded/failing)"

    # 5. Open P0 / P1 Critical Issues & Tasks
    high_pri_lines = []
    try:
        from tools.task_manager import task_manage
        t_data = task_manage("list")
        for t in t_data.get("tasks", []):
            pri = str(t.get("priority", "")).lower()
            stat = str(t.get("status", "")).lower()
            if pri in ("p0", "p1") and stat != "completed":
                badge = "🔴 P0" if pri == "p0" else "🔥 P1"
                high_pri_lines.append(f"- {badge} `#{t.get('id')}` **{t.get('title')}** (*{t.get('status')}*)")
    except Exception as e:
        log.warning(f"Failed to fetch tasks for nightly triage: {e}")

    date_header = tomorrow_pt.strftime("%A, %B %-d")
    report = [
        f"📋 **Nightly Assistant** — For {date_header}",
        "",
        "### 📅 Tomorrow's Agenda",
        *agenda_lines,
        "",
        "### 📬 Priority Inbox Triage",
        *inbox_lines,
    ]

    if high_pri_lines:
        report.extend([
            "",
            "### 🚨 Open P0/P1 Priority Issues",
            *high_pri_lines
        ])

    if proposed_calendar:
        report.extend([
            "",
            "### 🗓️ Proposed Calendar Events",
            *proposed_calendar
        ])

    if failure_lines:
        report.extend([
            "",
            "### ⚠️ Active Sidecar Warnings & Failures",
            *failure_lines
        ])

    p0_p1_note = f"🚨 {len(high_pri_lines)} active issue(s)" if high_pri_lines else "None open ✅"

    report.extend([
        "",
        "### 🖥️ Homelab Posture",
        f"- **Infra Health**: {infra_note}",
        f"- **Scheduled Sidecars**: {sidecar_note}",
        f"- **Open P0/P1 Issues**: {p0_p1_note}",
        f"- **Active Containers**: Host1 ({HOST_1_IP}): `{sb_up}` up | Host2 ({HOST_2_IP}): `{bs2_up}` up",
        f"- **Baseball Pipeline Refresh**: `{bb_ts}`"
    ])
    return "\n".join(report)

# --------------------------------------------------------------------------
# 3. NAS Log Review
# --------------------------------------------------------------------------
def _diagnose_container_errors(cname: str, errors: list[str]) -> tuple[int, str, str]:
    """Analyze filtered container errors and return (tier, plain_english_explanation, proposed_next_step)."""
    joined = " \n ".join(errors)

    # 1. Database errors
    if re.search(r"database is locked|sqlite3\.OperationalError", joined, re.I):
        return 1, "SQLite database encountered write contention and locked.", "Check for concurrent background tasks or restart the container to clear stale locks."
    if re.search(r"Npgsql\.NpgsqlException|TimeoutException.*queue", joined, re.I):
        return 1, "Database stream connection timed out while querying the queue API.", "Monitor queue responsiveness; restart the container if UI operations stall."
    if re.search(r"UNIQUE constraint failed", joined, re.I):
        return 2, "Database constraint collision occurred during metadata caching.", "Scheduled cache maintenance will resolve key duplicates automatically."

    # 2. Discord bot commands
    if re.search(r"CommandNotFound: Application command", joined, re.I):
        m = re.search(r"Application command '([^']+)' not found", joined)
        cmd_name = f"/{m.group(1)}" if m else "slash command"
        return 1, f"Discord client received an unregistered slash command ({cmd_name}).", "Sync application command definitions with Discord API."

    # 3. Subprocess / Dockhand
    if re.search(r"Go worker error.*list containers|Failed to send.*notification", joined, re.I):
        return 2, "Worker encountered errors polling container states and sending webhook notifications.", "Verify Docker socket permissions or restart Dockhand if dashboard stats stall."

    # 4. Media & Metadata (TMDb / Subtitles / Indexers)
    if re.search(r"TMDb Error:.*404|No Episode found for TMDb", joined, re.I):
        return 2, "Metadata lookup returned 404 Not Found for missing episodes on TMDb.", "Add affected TV series/specials to the exclude list if metadata is not on TMDb."
    if re.search(r"check_update.*Error trying to get releases from GitHub", joined, re.I):
        return 2, "GitHub release check timed out or hit API rate limiting.", "No action needed; automated updater will retry on the next check."
    if re.search(r"Path does not exist", joined, re.I):
        return 2, "Media indexer attempted to access an unmounted or missing file path.", "Check container volume mounts and root folder mappings."
    if re.search(r"HttpClient\.HandleFailure|SocketException.*Resource temporarily unavailable", joined, re.I):
        return 2, "Outbound HTTP or socket connection to external indexer or download client timed out.", "Automated retry will recover on the next scheduled refresh."

    # 5. Smart Home / IoT
    if re.search(r"pychromecast.*(Heartbeat timeout|Failed to connect)", joined, re.I):
        return 2, "Chromecast integration lost connection during heartbeat check to cast devices.", "Normal when TVs or Chromecasts are in standby; verify device power if unresponsive."
    if re.search(r"Nest API.*ECONNRESET|TLSSocket", joined, re.I):
        return 2, "Nest cloud event stream disconnected unexpectedly.", "Plugin reconnects automatically; check HomeKit status if accessories show No Response."
    if re.search(r"CASESession timed out|Setup for node failed", joined, re.I):
        return 2, "Matter device secure communication timed out.", "Verify physical power and wireless mesh connectivity to the Matter node."

    # 6. Novel, Unhandled, or Critical Errors (Attempt LLM diagnosis before generic fallback)
    prompt = (
        f"Container '{cname}' emitted these log errors:\n{joined[:1200]}\n\n"
        f"Provide a 1-sentence plain-English root cause explanation and a 1-sentence recommended next step.\n"
        f"Format strictly as: Explanation: <sentence> | Next Step: <sentence>"
    )
    try:
        res = subprocess.run(
            ["agy", "--model=gemini-3.8-flash-low", "--disable-slash-commands", f"-p={prompt}"],
            capture_output=True, text=True, timeout=12
        )
        if res.returncode == 0 and "Explanation:" in res.stdout:
            parts = res.stdout.strip().split("|")
            expl = parts[0].replace("Explanation:", "").strip()
            step = parts[1].replace("Next Step:", "").strip() if len(parts) > 1 else "Inspect container logs."
            tier = 1 if re.search(r"panic|fatal|segfault|\boom\b|killed|unhandled exception|syntaxerror", joined, re.I) else 2
            if expl:
                return tier, expl, step
    except Exception:
        pass

    # Generic Fallbacks
    if re.search(r"panic|fatal|segfault|\boom\b|killed|unhandled exception|syntaxerror", joined, re.I):
        return 1, f"Critical application error or unhandled crash in {cname}.", "Inspect detailed container logs with docker logs and verify configuration."
    else:
        return 2, f"Transient operational warning or non-critical error in {cname}.", "Monitor on subsequent health checks; investigate if error recurs continuously."

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

def add_dated_reminder(due_date: str, message: str, reminder_id: str = "") -> dict:
    """Dynamically register a one-shot dated reminder in /workspace/data/reminders.json."""
    reminders_file = DATA_DIR / "reminders.json"
    reminders_list = []
    if reminders_file.exists():
        try:
            with open(reminders_file) as f:
                reminders_list = json.load(f)
        except Exception:
            pass
    rid = reminder_id or f"reminder-{due_date}-{int(time.time())}"
    entry = {"id": rid, "due": due_date, "message": message}
    reminders_list.append(entry)
    with open(reminders_file, "w") as f:
        json.dump(reminders_list, f, indent=2)
    return {"ok": True, "reminder": entry}

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
    else:
        lines = [f"{i}. {s['name']} — {s['count']} email{'s' if s['count'] != 1 else ''}" for i, s in enumerate(senders, 1)]
        msg = (
            f"📬 **Biweekly Marketing Report** — {date_str}\n\n"
            f"**{len(senders)} promotional senders this period:**\n"
            + "\n".join(lines)
            + "\n\nReply with sender numbers or names to unsubscribe."
        )

    try:
        os.makedirs(os.path.dirname(MARKETING_STATE_FILE), exist_ok=True)
        with open(MARKETING_STATE_FILE, "w") as f:
            f.write(now_pt.strftime("%Y-%m-%d"))
        with open(MARKETING_SENDERS_FILE, "w") as f:
            json.dump(senders, f, indent=2)
    except Exception:
        pass

    return True, msg

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
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=360)
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

def run_birthday_reminders(date_str: str | None = None) -> tuple[bool, str]:
    """Check for friend & family birthdays today and format interactive text reminder."""
    try:
        from tools.birthday_reminder import check_birthdays
        has_bday, msg, _ = check_birthdays(date_str)
        return has_bday, msg if has_bday else ""
    except Exception as e:
        return False, f"⚠️ Birthday reminder check failed: {e}"

def run_social_last_seen_review(days: int = 7) -> tuple[bool, str]:
    """Review past week of calendar & communications for social gatherings and proposed Last Seen updates."""
    try:
        from tools.social_last_seen_review import identify_social_updates, format_review_message
        updates = identify_social_updates(days=days)
        has_events, msg = format_review_message(updates)
        return has_events, msg if has_events else ""
    except Exception as e:
        return False, f"⚠️ Social review check failed: {e}"

def run_core_friends_reminder(weeks: int = 8, as_of_date: str | None = None) -> tuple[bool, str]:
    """Monthly check for local Core friends not seen in >= 8 weeks."""
    try:
        from tools.core_friends_reminder import check_core_friends_unseen
        has_friends, msg, _ = check_core_friends_unseen(weeks=weeks, as_of_date=as_of_date)
        return has_friends, msg if has_friends else ""
    except Exception as e:
        return False, f"⚠️ Core friends reminder check failed: {e}"

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
        warnings = res.get("quantity_warnings", [])
        costco = res.get("costco_items", [])
        rec_count = res.get("recipes_ingested", 0)
        rec_str = f", {rec_count} meal recipe(s)" if rec_count else ""

        lines = [
            f"🛒 **Weekly Whole Foods Cart Ready ({len(manifest)} Items)**",
            f"• Sourced: {res['tasks_ingested']} item(s) from tasks{rec_str}, {res['staples_ingested']} due staple(s)"
        ]
        if rec_names:
            lines.append(f"🍽️ **Dinners:** {', '.join(rec_names)}")

        lines.append(f"\n🛒 [**1-Click Whole Foods Cart**](<{url}>)")
        lines.append("*Tap the link above to stage items in Amazon, choose your Friday afternoon delivery window, and complete checkout.*")

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

def run_weekly_meal_proposal() -> tuple[bool, str]:
    """Generates the 3-dinner weekly rotation proposal for Sunday, Tuesday, and Thursday."""
    try:
        from tools.meal_planner_proposal import generate_proposal, format_proposal_markdown
        plan = generate_proposal()
        return True, format_proposal_markdown(plan)
    except Exception as e:
        return False, f"⚠️ Weekly meal planning proposal failed: {e}"

def run_tasks_sync() -> tuple[bool, str, dict]:
    """Execute two-way synchronization between tasks.json and Google Tasks."""
    try:
        from tools.google_tasks_sync import sync_tasks
        res = sync_tasks(quiet=True)
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

def run_kalshi_paper_bot():
    """Execute Kalshi quant paper bot: weather and daily MLB matchups."""
    import subprocess
    try:
        outputs = []
        # 1. Settle resolved contracts
        subprocess.run(["python3", "/workspace/tools/kalshi_paper_bot.py", "settle"], check=False)
        subprocess.run(["python3", "/workspace/tools/baseball_daily_quant.py", "settle"], check=False)
        
        # 2. Weather CLOB Scan & Trade
        res_w = subprocess.run(["python3", "/workspace/tools/kalshi_paper_bot.py", "trade"], capture_output=True, text=True, check=False)
        if res_w.stdout:
            outputs.append("=== WEATHER ARBITRAGE ===\n" + res_w.stdout.strip())
            
        # 3. MLB Daily Matchups Scan & Trade
        res_m = subprocess.run(["python3", "/workspace/tools/baseball_daily_quant.py", "trade"], capture_output=True, text=True, check=False)
        if res_m.stdout:
            outputs.append("=== MLB DAILY MATCHUPS ===\n" + res_m.stdout.strip())
            
        return True, "\n\n".join(outputs), {}
    except Exception as e:
        return False, f"⚠️ Kalshi quant sidecar error: {e}", {"error": str(e)}

def run_kalshi_performance_review(phase="all"):
    """Execute Kalshi paper trading evening settlement, performance review, and task board sync."""
    import subprocess
    try:
        cmd = ["python3", "/workspace/tools/kalshi_performance_review.py", f"--phase={phase}"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = res.stdout.strip() if res.stdout else res.stderr.strip()
        return res.returncode == 0, output, {}
    except Exception as e:
        return False, f"⚠️ Kalshi review sidecar error: {e}", {"error": str(e)}

def run_kalshi_autoworker():
    """Execute autonomous 5-minute task board sprint cycle for Kalshi quant trading."""
    import subprocess
    try:
        res = subprocess.run(["python3", "/workspace/tools/kalshi_autoworker.py"], capture_output=True, text=True, check=False)
        output = res.stdout.strip() if res.stdout else res.stderr.strip()
        return res.returncode == 0, output, {}
    except Exception as e:
        return False, f"⚠️ Kalshi autoworker error: {e}", {"error": str(e)}

def run_sideproject_watcher(*args, **kwargs) -> tuple[bool, str, any]:
    """Execute autonomous 30-minute sprint cycle for Highball & Outpost side-projects."""
    try:
        from tools.sideproject_watcher import run_cycle
        force = kwargs.get("force", False)
        test = kwargs.get("test", False)
        return run_cycle(force=force, test=test)
    except Exception as e:
        return False, f"⚠️ Sideproject watcher error: {e}", {"error": str(e)}

# --------------------------------------------------------------------------
# CLI Dispatcher
# --------------------------------------------------------------------------
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
        ok, rep, _ = run_sidecar_job("doctor", "Memory Doctor Audit", run_memory_doctor)
        print(rep)
    elif action == "dream":
        from tools.memory_manager import run_dreaming_consolidation
        ok, rep, _ = run_sidecar_job("dream", "Dreaming Consolidation", run_dreaming_consolidation)
        print(rep)
    elif action == "battery":
        ok, rep, _ = run_sidecar_job("ha_battery", "HA Battery Check", run_ha_battery_check)
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
    elif action in ("grocery_staging", "grocery", "cart"):
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
    elif action in ("sideproject_watcher", "sideproject", "sideproject_sprint"):
        force = "--force" in sys.argv or "-f" in sys.argv
        test = "--test" in sys.argv or "-t" in sys.argv
        ok, rep, _ = run_sidecar_job("sideproject_watcher", "Side-Project Autonomous Sprint Watcher", run_sideproject_watcher, force=force, test=test)
        if rep:
            print(rep)
    elif action == "status":
        print(format_sidecar_status_summary())
    elif action == "history":
        entries = get_execution_history(limit=20)
        print(json.dumps(entries, indent=2))
    else:
        print(f"Unknown action: {action}")
