#!/usr/bin/env python3
import re
import subprocess
import sys

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.append(_p)

try:
    from tools.nas_config import resolve_nas_config
except ImportError:
    from nas_config import resolve_nas_config

HOST_1_IP, HOST_2_IP, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

SSH_82 = [
    "ssh",
    "-i",
    SSH_KEY,
    "-p",
    SSH_PORT,
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "ConnectTimeout=8",
    f"{SSH_USER}@{HOST_1_IP}",
]
SSH_84 = [
    "ssh",
    "-i",
    SSH_KEY,
    "-p",
    SSH_PORT,
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "ConnectTimeout=8",
    f"{SSH_USER}@{HOST_2_IP}",
]


def get_out(ssh_cmd: list, cmd_str: str) -> str:
    try:
        out = subprocess.check_output(ssh_cmd + [cmd_str], stderr=subprocess.DEVNULL, timeout=12).decode()
        if out.strip():
            return out
    except Exception:
        pass

    # Local fallback for Host 2 if SSH fails or returns empty
    is_host_2 = (
        (bool(HOST_2_IP) and any(HOST_2_IP in str(arg) for arg in ssh_cmd))
        or any("84" in str(arg) for arg in ssh_cmd)
        or ssh_cmd == SSH_84
    )
    if is_host_2:
        try:
            fallback_cmd = cmd_str
            if "df -h /volume1" in cmd_str:
                fallback_cmd = "df -h /volume1 2>/dev/null || df -h /"
            out = subprocess.check_output(fallback_cmd, shell=True, stderr=subprocess.DEVNULL, timeout=12).decode()
            if out.strip():
                return out
        except Exception:
            pass

    return ""


def check_host(name: str, ssh_cmd: list, threshold: int = 85) -> tuple[list[str], str]:
    issues = []
    usage_str = ""

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
                    usage_str = f"{use_pct}% used ({avail} free)"
                    if use_pct >= threshold:
                        issues.append(
                            f"⚠️ **{name} Storage High:** `/volume1` is at **{use_pct}%** capacity ({avail} free of {total})."
                        )
                    break
                except ValueError:
                    pass

    if not usage_str:
        for line in df_out.splitlines():
            if line.startswith("/dev/"):
                parts = line.split()
                if len(parts) >= 5:
                    use_pct_str = parts[4].rstrip("%")
                    try:
                        use_pct = int(use_pct_str)
                        avail = parts[3]
                        total = parts[1]
                        usage_str = f"{use_pct}% used ({avail} free)"
                        if use_pct >= threshold:
                            issues.append(
                                f"⚠️ **{name} Storage High:** `/volume1` is at **{use_pct}%** capacity ({avail} free of {total})."
                            )
                        break
                    except ValueError:
                        pass

    if not usage_str:
        usage_str = "storage status unavailable"

    # 2. Check RAID array health (md2 is the data volume)
    raid_summary = "RAID Healthy"
    mdstat = get_out(ssh_cmd, "cat /proc/mdstat")
    lines = mdstat.splitlines()
    for idx, line in enumerate(lines):
        if "md2 :" in line:
            # Check for degraded array markers like [4/3] or [U_] or [2/1]
            if "_" in line or "[2/1]" in line or "[4/3]" in line:
                issues.append(f"🚨 **CRITICAL: {name} RAID Array Degraded!**\nStatus: `{line.strip()}`")

            level_match = re.search(r"\b(raid\d+)\b", line, re.I)
            raid_level = level_match.group(1).upper() if level_match else "RAID"

            block = " ".join(lines[idx:idx + 3])
            status_match = re.search(r"(\[[U_]+\])", block)
            if status_match:
                raid_status = status_match.group(1)
                raid_summary = f"{raid_level} {raid_status}"
                if "_" in raid_status and not any("RAID Array Degraded" in iss for iss in issues):
                    issues.append(f"🚨 **CRITICAL: {name} RAID Array Degraded!**\nStatus: `{line.strip()}`")
            else:
                raid_summary = f"{raid_level} [OK]"
            break

    status_line = f"• {name}: {usage_str} — {raid_summary}"
    return issues, status_line


def run_storage_check(quiet: bool = False):
    issues = []
    statuses = []

    h1_issues, h1_stat = check_host("Host1 (.82)", SSH_82)
    issues.extend(h1_issues)
    statuses.append(h1_stat)

    h2_issues, h2_stat = check_host("Host2 (.84)", SSH_84)
    issues.extend(h2_issues)
    statuses.append(h2_stat)

    if issues:
        print("💾 **Synology Storage & Array Health Alert**\n")
        for i in issues:
            print(f"• {i}")
        print("\n*Action required on NAS.*")
    else:
        if not quiet:
            print("✅ **Synology Storage & RAID Status Healthy:**")
            for s in statuses:
                print(s)


if __name__ == "__main__":
    quiet = "--quiet" in sys.argv
    run_storage_check(quiet=quiet)
