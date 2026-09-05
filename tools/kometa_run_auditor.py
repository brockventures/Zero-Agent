#!/usr/bin/env python3
"""Post-Kometa Run Log Auditor.

Runs daily post-run (02:15 AM PT, after Kometa's 1:00 AM PT run):
1. Reads the tail of Kometa's meta.log on Host 1 over SSH.
2. Extracts Critical Summary, Error Summary, and TMDb 404 missing IDs.
3. Dispatches clean report to #homelab (1544955535722545253).
4. Tracks audited run completion timestamps in /workspace/data/kometa_audit_state.json.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("/workspace/data/kometa_audit_state.json")

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "admin")

try:
    from tools.sidecars import _resolve_nas_config
    HOST_1_IP, _, SSH_PORT = _resolve_nas_config()
except Exception:
    HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    SSH_PORT = os.environ.get("NAS_SSH_PORT", "22")

DOCKER_APPDATA_DIR = os.environ.get("DOCKER_APPDATA_DIR", os.path.join("/volume1", "docker", "appdata"))


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_audited_run": None, "last_check_at": None}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def audit_kometa_log(force: bool = False) -> tuple[bool, str, dict]:
    c = [
        "ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
        f"{SSH_USER}@{HOST_1_IP}",
        f"tail -n 250 {DOCKER_APPDATA_DIR}/kometa/config/logs/meta.log 2>/dev/null"
    ]
    try:
        res = subprocess.run(c, capture_output=True, text=True, timeout=15)
        log_tail = res.stdout
    except Exception as e:
        return False, f"⚠️ Failed to read Kometa meta.log over SSH: {e}", {}

    if not log_tail.strip():
        return False, "⚠️ Kometa meta.log is empty or unreachable.", {}

    # Extract Run Completion block
    run_finish_match = re.search(r"Finished (\d{2}:\d{2}) Run.*Start Time: ([\d\-:\s]+).*Finished: ([\d\-:\s]+).*Run Time: ([\d:]+)", log_tail, re.DOTALL)
    run_sig = run_finish_match.group(0) if run_finish_match else ""

    state = _load_state()
    if run_sig and state.get("last_audited_run") == run_sig and not force:
        return False, "(nominal - run already audited)", {}

    # Parse Error & Critical tables
    errors = []
    criticals = []
    tmdb_404s = []

    in_error_summary = False
    in_critical_summary = False

    for line in log_tail.splitlines():
        if "Error Summary" in line:
            in_error_summary = True
            in_critical_summary = False
            continue
        elif "Critical Summary" in line:
            in_critical_summary = True
            in_error_summary = False
            continue
        elif "Summary" in line and ("Finished" in line or "=====" in line):
            if "Error Summary" not in line and "Critical Summary" not in line:
                in_error_summary = False
                in_critical_summary = False

        if in_error_summary:
            m = re.search(r"\|\s*(\d+)\s*\|\s*(.*?)\s*\|", line)
            if m and m.group(1) != "Count":
                cnt, msg = m.group(1), m.group(2)
                errors.append((cnt, msg))
        elif in_critical_summary:
            m = re.search(r"\|\s*(\d+)\s*\|\s*(.*?)\s*\|", line)
            if m and m.group(1) != "Count":
                cnt, msg = m.group(1), m.group(2)
                criticals.append((cnt, msg))

        # Check for 404 TMDb/TVDb errors
        if "TMDb Error: 404" in line or "404 Client Error: Not Found" in line:
            id_m = re.search(r"TMDb ID:?\s*(\d+)|collection/(\d+)", line)
            if id_m:
                tmdb_404s.append(id_m.group(1) or id_m.group(2))

    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    state["last_check_at"] = now_str
    if run_sig:
        state["last_audited_run"] = run_sig
    _save_state(state)

    # If criticals or TMDb 404s found, generate alert
    has_issues = bool(criticals or tmdb_404s or any("failed" in e[1].lower() for e in errors))
    
    lines = []
    if run_finish_match:
        lines.append(f"📊 **Kometa Post-Run Audit** ({run_finish_match.group(1)} Run, Duration: `{run_finish_match.group(4)}`)")
    else:
        lines.append("📊 **Kometa Post-Run Audit**")

    if criticals:
        lines.append("\n🚨 **Critical Failures:**")
        for cnt, msg in criticals:
            lines.append(f"• ({cnt}x) `{msg[:140]}`")

    if tmdb_404s:
        lines.append(f"\n⚠️ **Missing TMDb IDs (404):** `{', '.join(sorted(set(tmdb_404s)))}`")
        lines.append("• Recommend staging to `settings.ignore_ids` or excluding purged collection IDs.")

    if errors:
        lines.append("\n⚠️ **Error Summary:**")
        for cnt, msg in errors[:5]:
            lines.append(f"• ({cnt}x) {msg}")

    if not has_issues:
        lines.append("• All collection builders, overlays, and metadata mappings completed nominally.")

    summary = "\n".join(lines)
    return has_issues, summary, {"criticals": criticals, "errors": errors, "tmdb_404s": tmdb_404s}


def main():
    parser = argparse.ArgumentParser(description="Post-Kometa Run Log Auditor")
    parser.add_argument("--force", action="store_true", help="Ignore state cache")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch report to #homelab outbox")
    parser.add_argument("--quiet", action="store_true", help="Suppress nominal output")
    args = parser.parse_args()

    has_issues, summary, data = audit_kometa_log(force=args.force)

    if has_issues or not args.quiet:
        print(summary)
        if args.dispatch and has_issues:
            from tools.outbox import queue_outbox_message
            queue_outbox_message(channel="homelab", content=summary)
            print("[KometaAuditor] Dispatched to #homelab outbox.", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
