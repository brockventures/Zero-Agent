#!/usr/bin/env python3
"""
Zero Pre-Commit Security & PII Validator
Scans staged git diffs and files to block accidental commits of private IPs,
custom ports, credentials, API tokens, and personal PII.
"""

import sys
import re
import subprocess

# Define strict security detection rules
SECURITY_RULES = [
    # Private IPs & Network Infrastructure
    (r"\b192\.168\.1\.\d{1,3}\b", "Private Homelab IP (192.168.1.x)"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "Private Subnet IP (10.x.x.x)"),
    (r"\b172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}\b", "Private Docker/Subnet IP (172.16-31.x.x)"),
    (r"\b49876\b", "Custom NAS SSH Port (49876)"),
    (r"\b(ServerBrock|BrockServer2|Huetiful)\b", "Private Homelab Hostname"),
    (r"\bBrock@192\.", "Private NAS SSH User/Host string"),
    (r"/volume1/(docker|data)", "Synology NAS Volume Path"),

    # Tokens & Secrets
    (r"GOCSPX-[A-Za-z0-9_-]{28}", "Google OAuth Client Secret"),
    (r"1//0[A-Za-z0-9_-]{20,}", "Google OAuth Refresh Token"),
    (r"ya29\.[A-Za-z0-9_-]{20,}", "Google OAuth Access Token"),
    (r"\b[A-Za-z0-9_-]{24,26}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}\b", "Discord Bot Token"),
    (r"\b44685f20eeab8ab08f904a996ff1eec57c728b00a3ebed1c\b", "Crab Cavern Banana Token"),
    (r"-----BEGIN (OPENSSH|RSA|EC|DSA) PRIVATE KEY-----", "SSH Private Key"),
    (r"(ATTgGYSsrx|t8n7hduuybrw|6@60%/\?6\?0)", "AT&T Gateway / WiFi Credentials"),

    # PII & Personal Information
    (r"\b3519\s+Highland\s+Ave\b", "Personal Residential Address"),
    (r"\bRedwood\s+City,\s+CA\s+94062\b", "Personal City/Zip Location"),
    (r"\b(1-)?630-589-4477\b", "Personal Phone Number"),
    (r"\b(1-)?630-605-5700\b", "Personal Phone Number"),
    (r"\b(ryanbrock2011|emilycallen13)@gmail\.com\b", "Personal Email Address"),
    (r"\brqb@google\.com\b", "Corporate Email Address"),
    (r"\bzero@brock\.ventures\b", "Private Agent Domain Email"),
]

def get_staged_diff() -> str:
    """Retrieve the unified git diff of all staged changes."""
    try:
        res = subprocess.run(
            ["git", "-C", "/workspace", "diff", "--cached", "--unified=0"],
            capture_output=True,
            text=True,
            check=True
        )
        return res.stdout
    except Exception as e:
        print(f"❌ Error getting staged git diff: {e}", file=sys.stderr)
        return ""

def main():
    diff_text = get_staged_diff()
    if not diff_text:
        sys.exit(0)

    violations = []
    current_file = "Unknown"

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        
        # Skip scanning the validator itself to prevent regex definitions from triggering false positives
        if "validate_commit_safety.py" in current_file:
            continue

        # Only inspect newly added or modified lines
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            for pattern, desc in SECURITY_RULES:
                if re.search(pattern, content, re.IGNORECASE):
                    violations.append((current_file, desc, content.strip()))

    if violations:
        print("\n" + "="*80, file=sys.stderr)
        print("🚨 COMMIT REJECTED BY ZERO PRE-COMMIT SECURITY VALIDATOR", file=sys.stderr)
        print("="*80, file=sys.stderr)
        print(f"Found {len(violations)} security / PII violation(s) in staged changes:\n", file=sys.stderr)
        
        for file_path, desc, snippet in violations:
            print(f"  • File: {file_path}", file=sys.stderr)
            print(f"    Issue:   {desc}", file=sys.stderr)
            print(f"    Snippet: {snippet[:120]}...", file=sys.stderr)
            print("-" * 80, file=sys.stderr)

        print("\nAction Required: Remove secrets/PII, use environment variables, and re-stage.", file=sys.stderr)
        print("="*80 + "\n", file=sys.stderr)
        sys.exit(1)

    print("✅ Pre-commit security check passed: No secrets or PII detected.")
    sys.exit(0)

if __name__ == "__main__":
    main()
