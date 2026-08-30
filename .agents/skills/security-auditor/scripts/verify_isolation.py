#!/usr/bin/env python3
"""
Verifies container isolation and execution boundaries.
"""

import os
import sys

def check_isolation():
    checks = []
    
    # 1. Check if secrets path is segregated
    if os.path.exists("/workspace/config/google_oauth.json"):
        checks.append(("FAIL", "Plaintext google_oauth.json found in /workspace/config/ (should only be in /secrets/)"))
    else:
        checks.append(("PASS", "No plaintext Google OAuth tokens in /workspace/config/"))

    # 2. Check if .gitignore exists and blocks memory
    if os.path.exists("/workspace/.gitignore"):
        with open("/workspace/.gitignore") as f:
            gi = f.read()
            if "memory/" in gi and "data/" in gi and ".env" in gi:
                checks.append(("PASS", ".gitignore properly enforces Default-Deny on memory/, data/, and .env"))
            else:
                checks.append(("FAIL", ".gitignore missing critical deny rules"))
    else:
        checks.append(("FAIL", "No .gitignore found in /workspace"))

    # 3. Check if validate_commit_safety hook is installed
    if os.path.exists("/workspace/.git/hooks/pre-commit"):
        checks.append(("PASS", "Pre-commit security hook is active in .git/hooks/pre-commit"))
    else:
        checks.append(("FAIL", "Pre-commit hook missing in .git/hooks/"))

    print("\n🔍 Isolation & Boundary Verification:")
    all_passed = True
    for status, desc in checks:
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon} [{status}] {desc}")
        if status != "PASS":
            all_passed = False

    return all_passed

if __name__ == "__main__":
    if check_isolation():
        print("\n🎉 Isolation status: SECURE\n")
        sys.exit(0)
    else:
        print("\n⚠️ Isolation status: VULNERABLE\n")
        sys.exit(1)
