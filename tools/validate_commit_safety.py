#!/usr/bin/env python3
"""
Zero Pre-Commit Security, PII, AST & Safety Validator
1. Scans staged git diffs and files to block accidental commits of private IPs,
   custom ports, credentials, API tokens, and personal PII.
2. Validates Python syntax and AST scoping (detects module shadowing in exception handlers).
3. Executes automated unit tests for formatting, titling, and bridge safety.
"""

import sys
import os
import re
import ast
import py_compile
import subprocess
from pathlib import Path

# Define strict security detection rules
SECURITY_RULES = [
    # Private IPs & Network Infrastructure
    (r"\b192\.168\.1\.\d{1,3}\b", "Private Homelab IP (192.168.1.x)"),
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "Private Subnet IP (10.x.x.x)"),
    (r"\b172\.(1[6-9]|2[0-9]|3[0-1])\.\d{1,3}\.\d{1,3}\b", "Private Docker/Subnet IP (172.16-31.x.x)"),
    (r"\b49876\b", "Custom NAS SSH Port (49876)"),
    (r"\b(ServerBrock|ServerBrock2|BrockServer|BrockServer2|Huetiful)\b", "Private Homelab Hostname"),
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

DANGEROUS_SHADOWED_NAMES = {
    "re", "json", "os", "sys", "time", "asyncio", "discord", "uuid", "signal"
}

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

def get_staged_python_files() -> list[str]:
    """Retrieve all staged python files."""
    try:
        res = subprocess.run(
            ["git", "-C", "/workspace", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=True
        )
        files = [f.strip() for f in res.stdout.splitlines() if f.strip().endswith(".py")]
        return files
    except Exception:
        return []

def validate_python_file(rel_path: str) -> list[str]:
    """Compile and AST-check a python file for syntax errors and module shadowing."""
    full_path = Path("/workspace") / rel_path
    if not full_path.exists():
        return []

    errors = []
    # 1. Syntax / Bytecode Compilation
    try:
        py_compile.compile(str(full_path), doraise=True)
    except py_compile.PyCompileError as pe:
        errors.append(f"Syntax/Compilation Error: {pe}")
        return errors

    # 2. AST Scoping & Shadowing Check
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(full_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.ExceptHandler) and child.name:
                        if child.name in DANGEROUS_SHADOWED_NAMES:
                            errors.append(
                                f"Line {child.lineno}: ExceptHandler shadows critical module '{child.name}' inside function '{node.name}'"
                            )
    except Exception as e:
        errors.append(f"AST Parse Error: {e}")

    return errors

def run_automated_tests() -> list[str]:
    """Run regression unit test scripts."""
    test_scripts = [
        "/workspace/tools/test_bridge_safety.py",
        "/workspace/tools/test_formatter.py",
        "/workspace/tools/test_enhanced_title.py",
        "/workspace/tools/test_latex_cleaner.py",
        "/workspace/tools/test_title.py",
    ]
    failures = []
    for test_script in test_scripts:
        if os.path.exists(test_script):
            res = subprocess.run([sys.executable, test_script], capture_output=True, text=True)
            if res.returncode != 0:
                failures.append(f"Unit test failed: {os.path.basename(test_script)}\n{res.stderr.strip() or res.stdout.strip()}")
    return failures

def main():
    diff_text = get_staged_diff()
    staged_py_files = get_staged_python_files()

    violations = []
    current_file = "Unknown"

    if diff_text:
        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                current_file = line[6:]
                continue
            
            if "validate_commit_safety.py" in current_file or "scan_secrets_and_pii.py" in current_file:
                continue

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

    # Validate Python AST & Syntax on staged files
    ast_errors = []
    for py_file in staged_py_files:
        errs = validate_python_file(py_file)
        if errs:
            ast_errors.extend([(py_file, e) for e in errs])

    if ast_errors:
        print("\n" + "="*80, file=sys.stderr)
        print("🚨 COMMIT REJECTED: PYTHON SYNTAX / AST SCOPING ERRORS DETECTED", file=sys.stderr)
        print("="*80, file=sys.stderr)
        for py_file, err in ast_errors:
            print(f"  • {py_file}: {err}", file=sys.stderr)
        print("="*80 + "\n", file=sys.stderr)
        sys.exit(1)

    # Run automated test suite
    test_failures = run_automated_tests()
    if test_failures:
        print("\n" + "="*80, file=sys.stderr)
        print("🚨 COMMIT REJECTED: UNIT TESTS FAILED", file=sys.stderr)
        print("="*80, file=sys.stderr)
        for fail in test_failures:
            print(f"  • {fail}", file=sys.stderr)
        print("="*80 + "\n", file=sys.stderr)
        sys.exit(1)

    print("✅ Pre-commit validation passed: No secrets, clean AST scope, all unit tests green.")
    sys.exit(0)

if __name__ == "__main__":
    main()
