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
SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")

def _resolve_nas_config():
    ssh_port = os.environ.get("NAS_SSH_PORT", "22")
    host_1 = os.environ.get("NAS_HOST_1_IP")
    host_2 = os.environ.get("NAS_HOST_2_IP")

    if not host_1 and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_HOST_1_IP"):
                    host_1 = d["NAS_HOST_1_IP"]
                elif d.get("HA_BASE_URL"):
                    host_1 = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
        except Exception:
            pass

    if not host_1 and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                if d.get("url"):
                    host_1 = urllib.parse.urlparse(d["url"]).hostname
        except Exception:
            pass

    if not host_2 and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_HOST_2_IP"):
                    host_2 = d["NAS_HOST_2_IP"]
        except Exception:
            pass

    if host_1 and not host_2:
        parts = host_1.split(".")
        if len(parts) == 4 and parts[-1] == "82":
            host_2 = ".".join(parts[:3] + ["84"])

    return host_1 or "127.0.0.1", host_2 or "127.0.0.1", ssh_port

HOST_1_IP, HOST_2_IP, SSH_PORT = _resolve_nas_config()

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

BROCKSERVER2_EXPECTED_CONTAINERS = {
    "baseball_db", "baseball_shiny_app", "baseball_shiny_pro",
    "baseball_shiny_dev", "baseball-scraper-1", "dockhand",
    "dozzle-agent", "discord-antigravity-agent"
}

REMINDER_SKIP_SENDER_PATTERNS = (
    "user@example.com", "work@example.com", "kings swim",
    "kingsswimacademy", "clipper", "garden route", "gardenrouteco",
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
            ok = res.get("ok", True)
            message = res.get("message", res.get("digest", str(res)))
            extra = res
        elif res is None:
            ok = True
            message = ""
        else:
            ok = bool(res)
            message = str(res)

        # Domain-aware status classification
        if job_id == "reminders":
            # (has_due, msg): having 0 reminders due is normal healthy operation
            has_due = ok
            ok = True
            status = "ok"
            extra = {"has_due": has_due}
        elif job_id == "ev9":
            # (has_digest, msg, plot): daily silent capture is normal healthy operation
            if message and any(err_tag in message for err_tag in ("⚠️", "🚨", "Error", "Exception")):
                ok = False
                status = "error"
            else:
                ok = True
                status = "ok"
        elif job_id == "marketing":
            if message and any(err_tag in message for err_tag in ("⚠️", "🚨", "Could not")):
                ok = False
                status = "warning"
            else:
                ok = True
                status = "ok"
        elif job_id == "heartbeat":
            status = "ok" if ok else "warning"
        elif job_id == "nas_logs":
            status = "ok" if ok else "warning"
        elif job_id == "plex":
            status = "ok" if ok else "warning"
        else:
            status = "ok" if ok else "warning"

        summary = message[:400] if message else ("(clean run / silent)" if ok else "(degraded)")
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

    return {
        "total_runs": total,
        "ok_count": ok_count,
        "warning_count": warn_count,
        "error_count": err_count,
        "failures": failures,
        "latest_by_job": latest_map,
        "all_healthy": total > 0 and len(failures) == 0
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
        missing = BROCKSERVER2_EXPECTED_CONTAINERS - running
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

    # 2. Gmail Triage (inbox-only with intentional reminder filter)
    gmail_res = json.loads(gmail_search("in:inbox is:unread", 20))
    messages = gmail_res.get("messages", [])

    inbox_lines = []
    proposed_calendar = []

    if messages:
        for m in messages:
            sender = m.get("from", "")
            subj = m.get("subject", "")
            snippet = m.get("snippet", "")
            s_low = sender.lower()
            sub_low = subj.lower()
            snip_low = snippet.lower()

            # Skip intentional self-reminders
            if any(p in s_low for p in REMINDER_SKIP_SENDER_PATTERNS):
                continue
            if any(p in sub_low for p in REMINDER_SKIP_SUBJECT_PATTERNS):
                continue

            sender_clean = re.sub(r"<.*?>", "", sender).strip() or sender
            snip_display = snippet
            if len(snip_display) > 85:
                snip_display = snip_display[:82] + "..."
            inbox_lines.append(f"- **{sender_clean}**: *\"{subj}\"*\n  -# {snip_display}")

            # Intelligent Calendar Proposal Detection
            if "redwood barber" in s_low or "appointment is reserved" in sub_low:
                time_match = re.search(r"(\d{1,2}:\d{2}\s*(?:AM|PM))\s+on\s+([A-Za-z]+,\s*[A-Za-z]+\s*\d{1,2})", snippet, re.IGNORECASE)
                if time_match:
                    proposed_calendar.append(f"- **{time_match.group(2)} @ {time_match.group(1)}** — *Haircut with Javier* (Redwood Barber Co.)")
                else:
                    proposed_calendar.append("- **Mon, Aug 31 @ 9:00 AM** — *Haircut with Javier* (Redwood Barber Co.)")
            elif "makeup token" in sub_low or "swim lesson" in snip_low:
                exp_match = re.search(r"Expiration:\s*(\d{2}/\d{2}/\d{4})", snippet)
                if exp_match:
                    proposed_calendar.append(f"- **By {exp_match.group(1)}** — *Rosie Swim Lesson Makeup Token Expiration* (King's Swim Academy)")
            elif "estimate" in sub_low and ("wednesday 9/9" in snip_low or "tuesday 9/8" in snip_low):
                proposed_calendar.append("- **Wed, Sep 9 (Proposed)** — *Heat Pump / AC Installation Window* (EM Energy & Air)")

    if not inbox_lines:
        inbox_lines.append("- *(Inbox clean / zero priority unread items)*")

    # 3. Scheduled Sidecar Job Failure Check (Automated Surfacing)
    health = get_sidecar_health_summary(since_hours=24.0)
    failure_lines = []
    if health["failures"]:
        for f in health["failures"]:
            icon = "❌" if f.get("status") == "error" else "⚠️"
            sum_snip = f.get("summary", "").splitlines()[0] if f.get("summary") else "Degraded or errored run"
            if len(sum_snip) > 80:
                sum_snip = sum_snip[:77] + "..."
            failure_lines.append(f"- {icon} **{f.get('name')}** (*{f.get('timestamp_pt', 'last 24h')}*): `{sum_snip}`")

    # 4. Infra & Homelab Posture
    infra_ok, _ = run_heartbeat_sweep()
    infra_note = "All containers & network ports reporting healthy." if infra_ok else "Degraded containers detected (see alert)."

    # Homelab container counts
    sb_res = json.loads(nas_docker("ps", HOST_1_IP))
    bs2_res = json.loads(nas_docker("ps", HOST_2_IP))
    sb_up = sum(1 for line in (sb_res.get("output") or "").splitlines() if "\t" in line and "Up" in line)
    bs2_up = sum(1 for line in (bs2_res.get("output") or "").splitlines() if "\t" in line and "Up" in line)

    # Baseball pipeline refresh timestamp
    bb_res = _ssh_cmd(HOST_2_IP, "cat /docker/baseball/shiny_app/data/last_refresh.txt 2>/dev/null || echo 'unavailable'")
    bb_ts = bb_res.stdout.strip() if bb_res.returncode == 0 and bb_res.stdout.strip() else "unavailable"

    # Sidecar summary line
    if health["total_runs"] > 0:
        if health["all_healthy"]:
            sidecar_note = f"All {health['total_runs']} scheduled runs succeeded in last 24h. ✅"
        else:
            sidecar_note = f"⚠️ {health['ok_count']}/{health['total_runs']} runs passed ({len(health['failures'])} flagged/failed in last 24h)"
    else:
        sidecar_note = "No runs logged in last 24h."

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

    if proposed_calendar:
        report.extend([
            "",
            "### 🗓️ Proposed Calendar Events",
            *proposed_calendar
        ])

    if failure_lines:
        report.extend([
            "",
            "### ⚠️ Scheduled Job Warnings & Failures (Last 24h)",
            *failure_lines
        ])

    report.extend([
        "",
        "### 🖥️ Homelab Posture",
        f"- **Infra Health**: {infra_note}",
        f"- **Scheduled Sidecars**: {sidecar_note}",
        f"- **Active Containers**: Host1 ({HOST_1_IP}): `{sb_up}` up | Host2 ({HOST_2_IP}): `{bs2_up}` up",
        f"- **Baseball Pipeline Refresh**: `{bb_ts}`"
    ])
    return "\n".join(report)

# --------------------------------------------------------------------------
# 3. NAS Log Review
# --------------------------------------------------------------------------
def run_nas_log_review(since: str = "24h") -> tuple[bool, str]:
    now_pt = datetime.now(PT).strftime("%A %b %d %Y, %I:%M %p PT")
    flagged = []
    scanned = 0

    # Noise filter regex
    noise_re = re.compile(
        r"(libusb_init failed|TaskCanceledException|TVDb convert warning|OpenSubtitles|forecast_solar|Matter Node 2|connection reset by peer|socket\.timeout|\[EnvUpdateCheck\]|DNSSD packet parsing)",
        re.IGNORECASE
    )

    for host, label in [(HOST_1_IP, f"Host1 ({HOST_1_IP})"), (HOST_2_IP, f"Host2 ({HOST_2_IP})")]:
        ps_res = json.loads(nas_docker("ps", host))
        if not ps_res.get("ok"):
            continue
        for line in ps_res.get("output", "").splitlines():
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if parts and parts[1].startswith("Up"):
                cname = parts[0]
                if host == HOST_1_IP and cname in SERVERBROCK_STOPPED_ALLOWLIST:
                    continue
                scanned += 1

                # Query logs for candidate errors
                cmd = f"docker logs --since {since} --tail 200 {cname} 2>&1 | grep -iE 'panic|fatal|segfault|oom|killed|error|exception|failed|failure|traceback|critical' | tail -20"
                try:
                    res = _ssh_cmd(host, cmd, timeout=20)
                    out = res.stdout.strip()
                    if out:
                        # Apply noise filtering
                        real_errors = [l for l in out.splitlines() if not noise_re.search(l)]
                        if real_errors:
                            flagged.append({
                                "host": label,
                                "container": cname,
                                "count": len(real_errors),
                                "sample": real_errors[-3:]
                            })
                except Exception:
                    pass

    if not flagged:
        return True, f"🗄️ **NAS Log Review** — {now_pt}\nScanned the last {since} of logs for {scanned} running containers across `{HOST_1_IP}` and `{HOST_2_IP}` — no actionable errors or panics. ✅"

    report = [f"🗄️ **NAS Log Review — Flagged Issues** [{now_pt}]", f"Scanned {scanned} containers; found issues in {len(flagged)}:\n"]
    for f in flagged:
        report.append(f"**{f['container']}** ({f['host']}) — {f['count']} error line(s):")
        for s in f["sample"]:
            report.append(f"  `{s[:120]}`")
        report.append("")
    return False, "\n".join(report)

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
        return False, "Marketing sweep not due yet (last run within 12 days)."

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
        addr_match = re.search(r"<([^>]+)>", sender)
        addr = addr_match.group(1).lower() if addr_match else sender.lower()
        name = re.sub(r"<.*?>", "", sender).strip() or addr
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
def run_ha_battery_check(threshold: float = 15.0) -> tuple[bool, str]:
    """Check IoT sensor battery levels via ha_battery_check."""
    try:
        res = subprocess.run(["python3", "/workspace/tools/ha_battery_check.py", f"--threshold={threshold}"], capture_output=True, text=True, timeout=20)
        out = res.stdout.strip()
        if "Low Battery Alert" in out:
            return False, out
        return True, out or "✅ All IoT sensors healthy."
    except Exception as e:
        return False, f"⚠️ Low battery check failed: {e}"

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

def run_ha_update_check() -> tuple[bool, str]:
    """Check for stable mature Home Assistant updates."""
    try:
        res = subprocess.run(["python3", "/workspace/tools/ha_update_check.py", "--quiet"], capture_output=True, text=True, timeout=30)
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

# --------------------------------------------------------------------------
# CLI Dispatcher
# --------------------------------------------------------------------------
if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "heartbeat"
    if action == "heartbeat":
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
    elif action == "ha_update":
        ok, rep, _ = run_sidecar_job("ha_update_check", "HA Update Check", run_ha_update_check)
        print(rep or "HA up to date.")
    elif action == "dockhand":
        ok, rep, _ = run_sidecar_job("dockhand_check", "Dockhand Image Check", run_dockhand_update_check)
        print(rep or "Dockhand up to date.")
    elif action == "antigravity":
        ok, rep, _ = run_sidecar_job("update_antigravity", "Antigravity CLI Check", run_antigravity_check)
        print(rep or "Antigravity up to date.")
    elif action == "status":
        print(format_sidecar_status_summary())
    elif action == "history":
        entries = get_execution_history(limit=20)
        print(json.dumps(entries, indent=2))
    else:
        print(f"Unknown action: {action}")
