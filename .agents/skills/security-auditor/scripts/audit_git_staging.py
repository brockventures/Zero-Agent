#!/usr/bin/env python3
"""
Audits git staging and tracking against security rules and Default-Deny .gitignore.
"""

import subprocess
import sys

FORBIDDEN_TRACKED_PATTERNS = [
    ".env", "config/google_oauth.json", "config/youtube_oauth.json",
    "memory/", ".agents/memory/", "data/", "car_monitor_data/",
    ".db", ".sqlite", ".jsonl", ".log"
]

def main():
    try:
        res = subprocess.run(["git", "-C", "/workspace", "status", "--porcelain"], capture_output=True, text=True, check=True)
        lines = res.stdout.splitlines()
    except Exception as e:
        print(f"Error checking git status: {e}")
        sys.exit(1)

    flagged = []
    for line in lines:
        status = line[:2]
        file_path = line[3:].strip()
        for pat in FORBIDDEN_TRACKED_PATTERNS:
            if pat in file_path and not file_path.endswith(".example") and not file_path.endswith(".example.json"):
                flagged.append((status, file_path, f"Matches forbidden tracked pattern: {pat}"))

    if flagged:
        print("🚨 Git Staging Audit FAILED:")
        for s, fp, reason in flagged:
            print(f"  • [{s}] {fp} — {reason}")
        sys.exit(1)
    else:
        print("✅ Git Staging Audit Passed: No sensitive directories or database files tracked.")
        sys.exit(0)

if __name__ == "__main__":
    main()
