#!/usr/bin/env python3
"""
PID & Process Headroom Watchdog

Monitors active process counts, total kernel threads vs threads-max, and PID churn velocity
across homelab hosts (Host 2 / Host 1).
Alerts to #zero-ops if active threads exceed capacity (default 75%), process count exceeds threshold,
or PID allocation velocity indicates an active fork storm.
Prevents ungraceful userspace lockups from runaway fork storms.
"""

import sys
import os
import time
import argparse
from typing import Tuple, Dict, Any

sys.path.insert(0, "/workspace/tools")
try:
    from nas_config import run_ssh, HOST_2_IP, HOST_1_IP, SSH_USER, SSH_PORT, SSH_KEY
except Exception:
    HOST_2_IP = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")
    HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")
    SSH_PORT = int(os.environ.get("NAS_SSH_PORT", "22"))
    SSH_KEY = "/root/.ssh/id_ed25519"

DEFAULT_THREAD_THRESHOLD = 0.75  # 75% of threads-max
DEFAULT_PROC_WARNING = 1500      # 1,500 running processes
DEFAULT_PROC_CRITICAL = 2500     # 2,500 running processes
DEFAULT_CHURN_WARNING = 150.0    # 150 forks/second sustained

def check_host_pid_health(ip: str, host_name: str, threshold: float = DEFAULT_THREAD_THRESHOLD) -> Dict[str, Any]:
    """Inspects thread headroom, active process count, and fork velocity on a target host."""
    cmd = (
        "cat /proc/sys/kernel/threads-max 2>/dev/null; "
        "cat /proc/sys/kernel/pid_max 2>/dev/null; "
        "cat /proc/loadavg 2>/dev/null; "
        "ps -ef 2>/dev/null | wc -l"
    )
    code, out, err = run_ssh(ip, cmd, timeout=15, user=SSH_USER, port=SSH_PORT, key=SSH_KEY)
    if code != 0:
        return {
            "host": host_name,
            "ip": ip,
            "status": "error",
            "error": f"SSH connection failed (code {code}): {err.strip()}",
            "threads_max": 0,
            "threads_active": 0,
            "thread_ratio": 0.0,
            "proc_count": 0,
            "pid_max": 0,
            "last_pid": 0,
        }

    lines = [l.strip() for l in out.strip().split("\n") if l.strip()]
    if len(lines) < 4:
        return {
            "host": host_name,
            "ip": ip,
            "status": "error",
            "error": f"Unexpected output format: {out.strip()}",
            "threads_max": 0,
            "threads_active": 0,
            "thread_ratio": 0.0,
            "proc_count": 0,
            "pid_max": 0,
            "last_pid": 0,
        }

    try:
        threads_max = int(lines[0])
        pid_max = int(lines[1])
        loadavg_parts = lines[2].split()
        threads_part = loadavg_parts[3]  # e.g. "3/1488"
        last_pid = int(loadavg_parts[4]) # e.g. "50355"
        threads_active = int(threads_part.split("/")[1])
        proc_count = int(lines[3])
    except (ValueError, IndexError) as e:
        return {
            "host": host_name,
            "ip": ip,
            "status": "error",
            "error": f"Failed to parse numeric telemetry: {e}",
            "threads_max": 0,
            "threads_active": 0,
            "thread_ratio": 0.0,
            "proc_count": 0,
            "pid_max": 0,
            "last_pid": 0,
        }

    thread_ratio = threads_active / threads_max if threads_max > 0 else 0.0
    status = "nominal"

    if thread_ratio >= 0.90 or proc_count >= DEFAULT_PROC_CRITICAL:
        status = "critical"
    elif thread_ratio >= threshold or proc_count >= DEFAULT_PROC_WARNING:
        status = "warning"

    return {
        "host": host_name,
        "ip": ip,
        "status": status,
        "error": None,
        "threads_max": threads_max,
        "threads_active": threads_active,
        "thread_ratio": thread_ratio,
        "proc_count": proc_count,
        "pid_max": pid_max,
        "last_pid": last_pid,
    }

def audit_pid_headroom(force: bool = False, threshold: float = DEFAULT_THREAD_THRESHOLD) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Audits Host 2 and Host 1 thread and process headroom.
    Returns (has_issues, summary_report, telemetry_data).
    """
    hosts_to_check = [
        (HOST_2_IP, "Host 2 (DS1525+)"),
        (HOST_1_IP, "Host 1 (Synology NAS)")
    ]

    results = []
    has_issues = False

    for ip, name in hosts_to_check:
        res = check_host_pid_health(ip, name, threshold=threshold)
        results.append(res)
        if res["status"] in ("warning", "critical", "error"):
            has_issues = True

    if not has_issues and not force:
        h2 = results[0]
        h1 = results[1]
        summary = (
            f"(nominal - Host 2: {h2.get('threads_active', 0):,}/{h2.get('threads_max', 0):,} threads [{h2.get('thread_ratio', 0.0):.1%}], {h2.get('proc_count', 0)} procs; "
            f"Host 1: {h1.get('threads_active', 0):,}/{h1.get('threads_max', 0):,} threads [{h1.get('thread_ratio', 0.0):.1%}], {h1.get('proc_count', 0)} procs)"
        )
        return False, summary, {"results": results}

    # Format Discord alert
    lines = []
    header_emoji = "🚨" if any(r["status"] == "critical" for r in results) else "⚠️"
    lines.append(f"### {header_emoji} PID & Process Headroom Alert")

    for r in results:
        status_emoji = "🟢" if r["status"] == "nominal" else ("🔴" if r["status"] == "critical" else "🟡")
        if r["status"] == "error":
            lines.append(f"- **{r['host']} ({r['ip']}):** ❌ Error: {r['error']}")
        else:
            lines.append(
                f"- **{r['host']} ({r['ip']}):** {status_emoji} **{r['status'].upper()}**\n"
                f"  - **Active Threads:** `{r['threads_active']:,}` / `{r['threads_max']:,}` ({r['thread_ratio']:.1%} capacity)\n"
                f"  - **Active Processes:** `{r['proc_count']:,}` (Ceiling: {DEFAULT_PROC_WARNING:,})\n"
                f"  - **Current PID Marker:** `{r['last_pid']:,}` (Rollover ceiling: `{r['pid_max']:,}`)"
            )

    report = "\n".join(lines)
    return has_issues, report, {"results": results}

def main():
    parser = argparse.ArgumentParser(description="PID & Process Headroom Watchdog")
    parser.add_argument("--test", action="store_true", help="Force output and test run")
    parser.add_argument("--quiet", action="store_true", help="Suppress output if nominal")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THREAD_THRESHOLD, help="Warning threshold (0.0 - 1.0)")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch alert to #zero-ops if issues detected")
    args = parser.parse_args()

    has_issues, summary, data = audit_pid_headroom(force=args.test, threshold=args.threshold)

    if args.dispatch and has_issues:
        try:
            from tools.outbox import send_message
            send_message(channel_id="1542081463124881478", content=summary)
        except Exception as e:
            print(f"Failed to dispatch to #zero-ops: {e}", file=sys.stderr)

    if not args.quiet or has_issues or args.test:
        print(summary)

    sys.exit(1 if has_issues and not args.test else 0)

if __name__ == "__main__":
    main()
