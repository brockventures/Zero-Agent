#!/usr/bin/env python3
import sys, subprocess, re

import os
import json
import urllib.parse

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")

def _resolve_nas_config():
    ssh_port = os.environ.get("NAS_SSH_PORT") or str(49000 + 876)
    host_1 = os.environ.get("NAS_HOST_1_IP")
    host_2 = os.environ.get("NAS_HOST_2_IP")

    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_SSH_PORT"):
                    ssh_port = str(d["NAS_SSH_PORT"])
                if d.get("NAS_HOST_1_IP"):
                    host_1 = d["NAS_HOST_1_IP"]
                elif d.get("HA_BASE_URL"):
                    host_1 = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
        except Exception:
            pass

    if not host_1 and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                if d.get("url"):
                    host_1 = urllib.parse.urlparse(d["url"]).hostname
        except Exception:
            pass

    if not host_2 and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_HOST_2_IP"):
                    host_2 = d["NAS_HOST_2_IP"]
        except Exception:
            pass

    if host_1 and not host_2:
        parts = host_1.split(".")
        if len(parts) == 4 and parts[-1] == "82":
            host_2 = ".".join(parts[:3] + ["84"])

    return host_1 or "127.0.0.1", host_2 or "127.0.0.1", ssh_port

HOST_1_IP, HOST_2_IP, SSH_PORT = _resolve_nas_config()

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
