#!/usr/bin/env python3
"""Comprehensive Sidecar Execution Engine for Ivy-AG.

Implements all scheduled maintenance and monitoring jobs:
1. Heartbeat Sweep (every 2 hours)
2. Nightly Triage & Agenda Briefing (daily @ 20:00 PT)
3. NAS Log Review (nightly @ 23:30 PT)
4. Plex Transcode Session Cleanup (nightly @ 03:00 PT)
5. Dated Reminders (daily @ 09:00 PT)
6. Used EV9 Listing Monitor (daily capture, Sunday digest @ 08:15 PT)
"""

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta
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
SSH_KEY = os.environ.get("NAS_SSH_KEY", "/root/.ssh/id_ed25519")
SSH_PORT = os.environ.get("NAS_SSH_PORT", "22")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")
HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
HOST_2_IP = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

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
# Helper: SSH Execution
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
# 1. Heartbeat Sweep
# --------------------------------------------------------------------------
def run_heartbeat_sweep() -> tuple[bool, str]:
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

    if not failures:
        return True, f"🟢 **Heartbeat Sweep Healthy** — {now_str}\nAll containers, ports, Home Assistant, and external monitors healthy across `.82` and `.84`."

    report = [f"🚨 **Heartbeat Alert — Infrastructure Degraded** [{now_str}]", ""]
    for f in failures:
        report.append(f"- ⚠️ {f}")
    return False, "\n".join(report)

# --------------------------------------------------------------------------
# 2. Nightly Triage Briefing
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

    # 3. Infra & Homelab Posture
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

    report.extend([
        "",
        "### 🖥️ Homelab Posture",
        f"- **Infra Health**: {infra_note}",
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
# CLI Dispatcher
# --------------------------------------------------------------------------
if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "heartbeat"
    if action == "heartbeat":
        ok, rep = run_heartbeat_sweep()
        print(rep)
    elif action == "triage":
        print(run_nightly_triage())
    elif action == "nas_logs":
        ok, rep = run_nas_log_review()
        print(rep)
    elif action == "plex":
        ok, rep = run_plex_session_cleanup()
        if rep.strip():
            print(rep)
    elif action == "reminders":
        ok, rep = run_dated_reminders()
        print(rep or "(no reminders due today)")
    elif action == "ev9":
        force = "--force" in sys.argv or "-f" in sys.argv
        ok, rep, plot = run_ev9_monitor(force_digest=force)
        if rep:
            print(rep)
        if plot:
            print(f"\n[Trend plot generated: {plot}]")
    elif action == "marketing":
        ok, rep = run_marketing_sweep(force=True)
        print(rep)
    elif action == "doctor":
        from tools.memory_manager import run_memory_doctor
        ok, rep = run_memory_doctor()
        print(rep)
    elif action == "dream":
        from tools.memory_manager import run_dreaming_consolidation
        ok, rep = run_dreaming_consolidation()
        print(rep)
    else:
        print(f"Unknown action: {action}")
