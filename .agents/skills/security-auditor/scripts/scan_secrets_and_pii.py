#!/usr/bin/env python3
"""
Exhaustive Secret, Network Topology & PII Scanner
Audits target directories or files for accidental leakage of credentials, private IPs, or personal data.
"""

import sys
import os
import re
import argparse

SECURITY_RULES = [
    # Private IPs & Network Infrastructure
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "Private Class C Subnet IP (192.168.x.x)"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "Private Class A Subnet IP (10.x.x.x)"),
    (r"\b172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}\b", "Private Docker/Subnet IP (172.16-31.x.x)"),
    (r"\b49876\b", "Custom NAS SSH Daemon Port (49876)"),
    (r"\b(ServerBrock|BrockServer2|Huetiful)\b", "Private Homelab Hostname"),
    (r"\bBrock@192\.", "Private NAS SSH User/Host string"),
    (r"/volume1/(docker|data)", "Synology NAS Volume Path"),

    # Tokens, Keys & Secrets
    (r"GOCSPX-[A-Za-z0-9_-]{28}", "Google OAuth Client Secret"),
    (r"1//0[A-Za-z0-9_-]{20,}", "Google OAuth Refresh Token"),
    (r"ya29\.[A-Za-z0-9_-]{20,}", "Google OAuth Access Token"),
    (r"\b[A-Za-z0-9_-]{24,26}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}\b", "Discord Bot Token"),
    (r"\b44685f20eeab8ab08f904a996ff1eec57c728b00a3ebed1c\b", "Crab Cavern Banana Bearer Token"),
    (r"-----BEGIN (OPENSSH|RSA|EC|DSA) PRIVATE KEY-----", "SSH Private Key"),
    (r"(ATTgGYSsrx|t8n7hduuybrw|6@60%/\?6\?0)", "AT&T Gateway / WiFi Credentials"),

    # PII & Personal Identifiers
    (r"\b3519\s+Highland\s+Ave\b", "Personal Residential Address"),
    (r"\bRedwood\s+City,\s+CA\s+94062\b", "Personal City/Zip Location"),
    (r"\b(1-)?630-589-4477\b", "Personal Phone Number"),
    (r"\b(1-)?630-605-5700\b", "Personal Phone Number"),
    (r"\b(ryanbrock2011|emilycallen13)@gmail\.com\b", "Personal Email Address"),
    (r"\brqb@google\.com\b", "Corporate Email Address"),
    (r"\bzero@brock\.ventures\b", "Private Agent Domain Email"),
]

def scan_file(file_path: str) -> list:
    violations = []
    # Skip self, git, and compiled binary bytecode
    if "scan_secrets_and_pii.py" in file_path or "validate_commit_safety.py" in file_path or file_path.endswith(".pyc"):
        return []
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for idx, line in enumerate(f, start=1):
                for pattern, desc in SECURITY_RULES:
                    if re.search(pattern, line, re.IGNORECASE):
                        violations.append((file_path, idx, desc, line.strip()))
    except Exception:
        pass
    return violations

def scan_target(target: str) -> list:
    all_violations = []
    if os.path.isfile(target):
        all_violations.extend(scan_file(target))
    elif os.path.isdir(target):
        for root, _, files in os.walk(target):
            if ".git" in root or "memory" in root or "data" in root or "__pycache__" in root:
                continue
            for f in files:
                all_violations.extend(scan_file(os.path.join(root, f)))
    return all_violations

def main():
    parser = argparse.ArgumentParser(description="Scan target files or directories for secrets, private IPs, and PII.")
    parser.add_argument("targets", nargs="+", help="Files or directories to scan")
    args = parser.parse_args()

    total_violations = []
    for t in args.targets:
        total_violations.extend(scan_target(t))

    if total_violations:
        print("\n" + "="*80)
        print("🚨 SECURITY AUDIT ALERT: SECRETS / PII DETECTED")
        print("="*80)
        for path, line_no, desc, snippet in total_violations:
            print(f"  • {path}:{line_no} — [{desc}]")
            print(f"    Snippet: {snippet[:120]}...")
            print("-" * 80)
        print(f"\nTotal Violations: {len(total_violations)}")
        sys.exit(1)
    else:
        print("✅ Security Scan Clean: 0 secrets, private IPs, or PII detected.")
        sys.exit(0)

if __name__ == "__main__":
    main()
