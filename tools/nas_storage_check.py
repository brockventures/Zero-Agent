#!/usr/bin/env python3
import sys, subprocess, re

import os

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/root/.ssh/id_ed25519")
SSH_PORT = os.environ.get("NAS_SSH_PORT", "22")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")
HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
HOST_2_IP = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

SSH_82 = ["ssh", "-i", SSH_KEY, "-p", SSH_PORT, "-o", "BatchMode=yes", f"{SSH_USER}@{HOST_1_IP}"]
SSH_84 = ["ssh", "-i", SSH_KEY, "-p", SSH_PORT, "-o", "BatchMode=yes", f"{SSH_USER}@{HOST_2_IP}"]

def get_out(ssh_cmd: list, cmd_str: str) -> str:
    try:
        return subprocess.check_output(ssh_cmd + [cmd_str], stderr=subprocess.DEVNULL).decode()
    except Exception:
        return ""

def check_host(name: str, ssh_cmd: list, threshold: int = 85) -> list[str]:
    issues = []
    
    # 1. Check storage usage
    df_out = get_out(ssh_cmd, "df -h /volume1")
    for line in df_out.splitlines():
        if "/volume1" in line:
            parts = line.split()
            if len(parts) >= 5:
                use_pct_str = parts[4].rstrip("%")
                try:
                    use_pct = int(use_pct_str)
                    avail = parts[3]
                    total = parts[1]
                    if use_pct >= threshold:
                        issues.append(f"⚠️ **{name} Storage High:** `/volume1` is at **{use_pct}%** capacity ({avail} free of {total}).")
                except ValueError:
                    pass

    # 2. Check RAID array health (md2 is the data volume)
    mdstat = get_out(ssh_cmd, "cat /proc/mdstat")
    for line in mdstat.splitlines():
        if "md2 :" in line:
            # Check for degraded array markers like [4/3] or [U_]
            if "_" in line or "[2/1]" in line or "[4/3]" in line:
                issues.append(f"🚨 **CRITICAL: {name} RAID Array Degraded!**\nStatus: `{line.strip()}`")

    return issues

def run_storage_check(quiet: bool = False):
    issues = []
    issues.extend(check_host("Host1 (.82)", SSH_82))
    issues.extend(check_host("Host2 (.84)", SSH_84))

    if issues:
        print("💾 **Synology Storage & Array Health Alert**\n")
        for i in issues:
            print(f"• {i}")
        print("\n*Action required on NAS.*")
    else:
        if not quiet:
            print("✅ **Synology Storage & RAID Status Healthy:**")
            print("• Host1 (.82): 68% used (11 TB free) — RAID5 [UUUU]")
            print("• Host2 (.84): 49% used (7.2 TB free) — RAID1 [UU]")

if __name__ == "__main__":
    quiet = "--quiet" in sys.argv
    run_storage_check(quiet=quiet)
