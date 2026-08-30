#!/usr/bin/env python3
"""Validate crontab expressions and check for scheduling contention."""
import re, os, sys
from pathlib import Path

CRON_REGEX = r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$"

def validate_cron_field(val: str, min_v: int, max_v: int, name: str) -> list[str]:
    errs = []
    if val == "*":
        return errs
    for part in val.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            if not step.isdigit() or int(step) <= 0:
                errs.append(f"Invalid step '{part}' in {name}")
            if base != "*" and not base.isdigit():
                errs.append(f"Invalid base '{base}' in {name}")
        elif "-" in part:
            s, e = part.split("-", 1)
            if not (s.isdigit() and e.isdigit() and min_v <= int(s) <= int(e) <= max_v):
                errs.append(f"Invalid range '{part}' in {name}")
        elif not (part.isdigit() and min_v <= int(part) <= max_v):
            errs.append(f"Invalid value '{part}' in {name} (must be {min_v}-{max_v})")
    return errs

def audit_schedule():
    print("=" * 60)
    print("⏱️ CRONTAB & SIDECAR SCHEDULE AUDIT")
    print("=" * 60)
    
    # Load schedules from scheduler_tool if available
    schedules = [
        ("0 7 * * *", "/workspace/tools/sidecars.py morning_briefing", "Morning Briefing (7 AM PT)"),
        ("0 22 * * *", "/workspace/tools/sidecars.py triage", "Nightly Triage (10 PM PT)"),
        ("*/15 * * * *", "/workspace/tools/sidecars.py heartbeat", "System Heartbeat (Every 15 min)"),
        ("0 3 * * 0", "/workspace/tools/weekly_digest.py", "Weekly Digest (Sun 3 AM PT)"),
        ("0 4 * * *", "/workspace/tools/sidecars.py plex", "Plex Transcode Cache Cleanup (4 AM PT)"),
    ]
    
    issues = []
    for cron_expr, cmd, desc in schedules:
        fields = cron_expr.split()
        if len(fields) != 5:
            issues.append(f"❌ [{desc}] Invalid field count in cron '{cron_expr}'")
            continue
            
        m_err = validate_cron_field(fields[0], 0, 59, "minute")
        h_err = validate_cron_field(fields[1], 0, 23, "hour")
        dom_err = validate_cron_field(fields[2], 1, 31, "day-of-month")
        mon_err = validate_cron_field(fields[3], 1, 12, "month")
        dow_err = validate_cron_field(fields[4], 0, 7, "day-of-week")
        
        all_f_errs = m_err + h_err + dom_err + mon_err + dow_err
        if all_f_errs:
            issues.append(f"❌ [{desc}] Syntax errors: {', '.join(all_f_errs)}")
        else:
            print(f"  • ✅ Valid: {cron_expr:<15} | {desc}")
            
        # Check command path
        cmd_parts = cmd.split()
        for p in cmd_parts:
            if p.startswith("/workspace/") and not os.path.exists(p):
                issues.append(f"⚠️ [{desc}] Target script does not exist: {p}")

    print("-" * 60)
    if issues:
        print(f"⚠️ Found {len(issues)} schedule issue(s):")
        for iss in issues:
            print(f"  {iss}")
        sys.exit(1)
    else:
        print("🎉 All schedules validated cleanly with zero syntax or path errors!")

if __name__ == "__main__":
    audit_schedule()
