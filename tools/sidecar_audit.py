#!/usr/bin/env python3
"""Sidecar Lifecycle & Schedule Auditor for Zero (Ivy-AG).

Ensures all scheduled sidecars follow the 5-layer lifecycle protocol:
1. Script Exists & Executable on Disk
2. Unified Runner Hook in tools/sidecars.py
3. Registration in data/schedule.json & tools/scheduler_tool.py
4. Discord On-Demand Command Hook in tools/bridge_handlers.py
5. Anti-Contention & PT Timezone Alignment
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path("/workspace")
DATA_DIR = WORKSPACE / "data"
SCHEDULE_FILE = DATA_DIR / "schedule.json"
SIDECARS_FILE = WORKSPACE / "tools" / "sidecars.py"
HANDLERS_FILE = WORKSPACE / "tools" / "bridge_handlers.py"
PT = ZoneInfo("America/Los_Angeles")

VALID_SCHEDULE_TYPES = {"interval", "daily", "weekly", "monthly"}


def extract_script_path(prompt: str) -> Path | None:
    """Extract script file path from a job prompt string."""
    if "[INTERNAL_SESSION_ROLLOVER]" in prompt:
        return None
    m = re.search(r"(/[a-zA-Z0-9_\-\./]+\.(?:py|sh))", prompt)
    if m:
        return Path(m.group(1))
    return None


def extract_sidecar_action(prompt: str) -> str | None:
    """Extract action name if prompt delegates to tools/sidecars.py."""
    m = re.search(r"sidecars\.py\s+([a-zA-Z0-9_\-]+)", prompt)
    if m:
        return m.group(1)
    return None


def get_sidecars_py_actions() -> set[str]:
    """Parse supported actions handled in tools/sidecars.py."""
    actions = set()
    if not SIDECARS_FILE.exists():
        return actions
    try:
        content = SIDECARS_FILE.read_text()
        matches = re.findall(r'elif\s+action\s*(?:==|\bin\b)\s*\(?([\'"][a-zA-Z0-9_\-]+[\'"](?:\s*,\s*[\'"][a-zA-Z0-9_\-]+[\'"])*)\)?', content)
        for group in matches:
            for act in re.findall(r'[\'"]([a-zA-Z0-9_\-]+)[\'"]', group):
                actions.add(act)
        if re.search(r'if\s+action\s*==\s*[\'"]([a-zA-Z0-9_\-]+)[\'"]', content):
            first = re.search(r'if\s+action\s*==\s*[\'"]([a-zA-Z0-9_\-]+)[\'"]', content).group(1)
            actions.add(first)
    except Exception as e:
        print(f"[Auditor] Error reading sidecars.py: {e}", file=sys.stderr)
    return actions


def get_bridge_triggers() -> set[str]:
    """Parse on-demand command triggers from tools/bridge_handlers.py."""
    triggers = set()
    if not HANDLERS_FILE.exists():
        return triggers
    try:
        content = HANDLERS_FILE.read_text()
        matches = re.findall(r'["\'](![a-zA-Z0-9_\-]+)["\']\s*:', content)
        for t in matches:
            triggers.add(t.lstrip("!"))
    except Exception as e:
        print(f"[Auditor] Error reading bridge_handlers.py: {e}", file=sys.stderr)
    return triggers


def audit_sidecars(verbose: bool = False) -> tuple[bool, list[dict]]:
    """Audit all registered jobs in data/schedule.json against disk and system hooks."""
    if not SCHEDULE_FILE.exists():
        print(f"❌ Schedule file missing: {SCHEDULE_FILE}")
        return False, []

    try:
        with open(SCHEDULE_FILE, "r") as f:
            jobs = json.load(f)
    except Exception as e:
        print(f"❌ Malformed schedule.json: {e}")
        return False, []

    sidecars_actions = get_sidecars_py_actions()
    bridge_triggers = get_bridge_triggers()

    results = []
    has_critical_failures = False
    seen_ids = set()
    time_slots: dict[tuple, list[str]] = {}

    for idx, job in enumerate(jobs):
        jid = job.get("id")
        jname = job.get("name", f"Job #{idx+1}")
        stype = job.get("schedule_type")
        prompt = job.get("prompt", "")
        enabled = job.get("enabled", True)

        issues = []
        warnings = []

        # 1. ID & Type validation
        if not jid or not re.match(r"^[a-zA-Z0-9_\-]+$", jid):
            issues.append("Invalid or missing 'id'")
        elif jid in seen_ids:
            issues.append(f"Duplicate id: '{jid}'")
        seen_ids.add(jid)

        if stype not in VALID_SCHEDULE_TYPES:
            issues.append(f"Invalid schedule_type: '{stype}' (must be one of {VALID_SCHEDULE_TYPES})")

        # 2. Timing validation
        hour_pt = job.get("hour_pt")
        min_pt = job.get("minute_pt")
        if stype == "daily":
            if hour_pt is None or not (0 <= hour_pt <= 23):
                issues.append("Missing or invalid hour_pt (0-23)")
            if min_pt is None or not (0 <= min_pt <= 59):
                issues.append("Missing or invalid minute_pt (0-59)")
            if hour_pt is not None and min_pt is not None and enabled:
                time_slots.setdefault((hour_pt, min_pt), []).append(jid)

        elif stype == "weekly":
            dow = job.get("day_of_week")
            if dow is None or not (0 <= dow <= 6):
                issues.append("Missing or invalid day_of_week (0=Mon, 6=Sun)")
            if hour_pt is None or not (0 <= hour_pt <= 23):
                issues.append("Missing or invalid hour_pt (0-23)")
            if min_pt is None or not (0 <= min_pt <= 59):
                issues.append("Missing or invalid minute_pt (0-59)")
            if dow is not None and hour_pt is not None and min_pt is not None and enabled:
                time_slots.setdefault((f"D{dow}", hour_pt, min_pt), []).append(jid)

        elif stype == "monthly":
            dom = job.get("day_of_month")
            if dom is None or not (1 <= dom <= 31):
                issues.append("Missing or invalid day_of_month (1-31)")
            if hour_pt is None or not (0 <= hour_pt <= 23):
                issues.append("Missing or invalid hour_pt (0-23)")
            if min_pt is None or not (0 <= min_pt <= 59):
                issues.append("Missing or invalid minute_pt (0-59)")

        elif stype == "interval":
            ival = job.get("interval_seconds")
            if ival is None or ival <= 0:
                issues.append("Missing or invalid interval_seconds (>0)")

        # 3. Catchup Window Policy
        if job.get("catchup_if_missed"):
            cwin = job.get("catchup_window_seconds")
            if not cwin or cwin <= 0:
                warnings.append("catchup_if_missed is True but catchup_window_seconds is unset or <=0")
            elif cwin > 86400 * 2:
                warnings.append(f"Excessive catchup_window_seconds: {cwin}s (>48h)")

        # 4. Target Script Existence
        script_path = extract_script_path(prompt)
        if script_path:
            if not script_path.exists():
                issues.append(f"Target script does not exist: {script_path}")
            elif not os.access(script_path, os.R_OK):
                issues.append(f"Target script not readable: {script_path}")
        elif prompt != "[INTERNAL_SESSION_ROLLOVER]":
            warnings.append("Prompt does not reference an explicit script path (.py/.sh)")

        # 5. Sidecars.py Hook Check
        sidecar_act = extract_sidecar_action(prompt)
        if sidecar_act:
            if sidecar_act not in sidecars_actions:
                warnings.append(f"Action '{sidecar_act}' not registered in tools/sidecars.py CLI dispatcher")
        
        # 6. Discord Trigger Hook Check
        trigger_match = None
        candidates = [jid, sidecar_act]
        if jid:
            candidates.extend(jid.split("_"))
            if "reminder" in jid:
                candidates.extend(["reminders", "birthdays", "birthday"])
            if "digest" in jid:
                candidates.append("digest")
            if "token" in jid:
                candidates.extend(["tokens", "token_report"])
            if "log" in jid:
                candidates.extend(["logs", "nas_logs"])
        if script_path:
            candidates.append(script_path.stem)
            candidates.extend(script_path.stem.split("_"))

        for candidate in candidates:
            if candidate and candidate in bridge_triggers:
                trigger_match = candidate
                break
        if not trigger_match and enabled and prompt != "[INTERNAL_SESSION_ROLLOVER]":
            warnings.append(f"No on-demand !<action> trigger mapped in tools/bridge_handlers.py (e.g. !{sidecar_act or jid})")

        status = "FAIL" if issues else ("WARN" if warnings else "PASS")
        if status == "FAIL":
            has_critical_failures = True

        results.append({
            "id": jid,
            "name": jname,
            "schedule_type": stype,
            "status": status,
            "issues": issues,
            "warnings": warnings,
            "script": str(script_path) if script_path else None
        })

    # 7. Check for Contention / Exact Collisions
    collision_warnings = []
    for slot, job_ids in time_slots.items():
        if len(job_ids) > 1:
            collision_warnings.append(f"Exact timing collision at {slot}: jobs {job_ids}")

    return not has_critical_failures, results, collision_warnings


def print_audit_report(results: list[dict], collisions: list[str], verbose: bool = False):
    """Print clean terminal audit summary."""
    now_pt = datetime.now(tz=PT).strftime("%Y-%m-%d %I:%M %p PT")
    print(f"\n📋 **Sidecar Schedule & Lifecycle Audit Report** ({now_pt})\n")
    print(f"{'Status':<8} {'ID':<30} {'Type':<10} {'Details'}")
    print("-" * 80)

    pass_count = sum(1 for r in results if r["status"] == "PASS")
    warn_count = sum(1 for r in results if r["status"] == "WARN")
    fail_count = sum(1 for r in results if r["status"] == "FAIL")

    for r in results:
        status_icon = "🟢 PASS" if r["status"] == "PASS" else ("🟡 WARN" if r["status"] == "WARN" else "🔴 FAIL")
        details = ""
        if r["issues"]:
            details = "ERR: " + "; ".join(r["issues"])
        elif r["warnings"]:
            details = "WARN: " + "; ".join(r["warnings"])
        else:
            details = f"Script: {r['script'] or 'internal'}"

        print(f"{status_icon:<8} {r['id']:<30} {r['schedule_type']:<10} {details}")

    if collisions:
        print("\n⚠️ **Schedule Collisions Detected (Resource Contention Risk):**")
        for c in collisions:
            print(f"  • {c}")

    print("-" * 80)
    print(f"Summary: {len(results)} total sidecars audited | {pass_count} Passed | {warn_count} Warnings | {fail_count} Critical Failures\n")


def print_schedule_matrix():
    """Print 24-hour visual schedule breakdown in Pacific Time."""
    if not SCHEDULE_FILE.exists():
        print(f"Schedule file not found: {SCHEDULE_FILE}")
        return

    with open(SCHEDULE_FILE) as f:
        jobs = json.load(f)

    print("\n🕒 **24-Hour Pacific Time Schedule Matrix**\n")
    matrix = {h: [] for h in range(24)}
    intervals = []

    for j in jobs:
        if not j.get("enabled", True):
            continue
        stype = j.get("schedule_type")
        if stype in ("daily", "weekly", "monthly") and j.get("hour_pt") is not None:
            h = j["hour_pt"]
            m = j.get("minute_pt", 0)
            tag = f"{j['id']} ({stype} @ {h:02d}:{m:02d})"
            matrix[h].append(tag)
        elif stype == "interval":
            sec = j.get("interval_seconds", 0)
            intervals.append(f"{j['id']} (every {sec // 60}m)")

    for h in range(24):
        time_str = f"{h:02d}:00 PT"
        entries = matrix[h]
        if entries:
            print(f"[{time_str}] -> " + ", ".join(entries))
        else:
            print(f"[{time_str}] -> (idle)")

    if intervals:
        print("\n🔁 **Interval Jobs (Dynamic):**")
        for i in intervals:
            print(f"  • {i}")
    print()


def test_job(job_id: str, timeout: int = 45):
    """Execute a dry-run or manual execution of a specific job."""
    if not SCHEDULE_FILE.exists():
        print("Schedule file missing.")
        return 1

    with open(SCHEDULE_FILE) as f:
        jobs = json.load(f)

    match = next((j for j in jobs if j.get("id") == job_id), None)
    if not match:
        print(f"❌ Job id '{job_id}' not found in schedule.json")
        return 1

    prompt = match.get("prompt", "")
    script_path = extract_script_path(prompt)
    if not script_path or not script_path.exists():
        print(f"❌ No executable script found for prompt: {prompt}")
        return 1

    print(f"⏳ Running test for '{job_id}' ({script_path}) with {timeout}s timeout...")
    start = time.time()
    try:
        res = subprocess.run(
            [sys.executable, str(script_path), "--test"],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        duration = time.time() - start
        if res.returncode == 0:
            print(f"🟢 Success ({duration:.2f}s)! Exit code 0.")
            if res.stdout.strip():
                print(f"Output:\n{res.stdout.strip()[:500]}")
            return 0
        else:
            print(f"🔴 Failed with exit code {res.returncode} ({duration:.2f}s)")
            if res.stderr.strip():
                print(f"Error:\n{res.stderr.strip()[:500]}")
            return res.returncode
    except subprocess.TimeoutExpired:
        print(f"🔴 Timed out after {timeout}s!")
        return 124
    except Exception as e:
        print(f"🔴 Execution error: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="Zero Sidecar Schedule & Lifecycle Auditor")
    subparsers = parser.add_subparsers(dest="command")

    audit_parser = subparsers.add_parser("audit", help="Audit all registered sidecars")
    audit_parser.add_argument("-v", "--verbose", action="store_true", help="Show verbose debugging info")

    matrix_parser = subparsers.add_parser("matrix", help="Show 24-hour visual schedule breakdown")

    test_parser = subparsers.add_parser("test", help="Test-run a specific sidecar by id")
    test_parser.add_argument("job_id", help="ID of job to test")
    test_parser.add_argument("--timeout", type=int, default=45, help="Execution timeout in seconds")

    args = parser.parse_args()

    if args.command in ("audit", None):
        success, results, collisions = audit_sidecars(verbose=getattr(args, "verbose", False))
        print_audit_report(results, collisions)
        sys.exit(0 if success else 1)
    elif args.command == "matrix":
        print_schedule_matrix()
    elif args.command == "test":
        sys.exit(test_job(args.job_id, timeout=args.timeout))


if __name__ == "__main__":
    main()
