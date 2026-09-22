#!/usr/bin/env python3
"""Autonomous Queue & Health Triage Watchdog for Radarr and Sonarr.

Monitors download, import queues, and system health across Host 1 (:7878 / :8989):
1. Tier 1 (Silent Auto-Remediation):
   - Repairs root-owned download permissions (chown 1026:100) and triggers download refresh.
   - Automatically configures LAN/Docker host whitelist for AllowedHostsCheck and re-verifies.
   - Logs receipts to /workspace/data/arr_triage_audit.jsonl with 0 chat noise.
2. Tier 2 (Noise & Advisory Filter):
   - Suppresses cosmetic/advisory checks (UpdateCheck, BranchCheck, PackageMaintainerMessage, etc.).
   - Extends non-critical warning debounce to 2 hours to eliminate transient jitter.
3. Tier 3 (True Blocker Escalation):
   - Escalates genuine pipeline blockers (DownloadClientUnavailable, MissingRootFolder, DiskSpaceCheck, DB corruption).
   - Dispatches actionable alerts or resolution notices to #homelab (1544955535722545253).
4. Persists state in /workspace/data/arr_queue_state.json to prevent duplicate spam.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("/workspace/data/arr_queue_state.json")
AUDIT_LOG_FILE = Path("/workspace/data/arr_triage_audit.jsonl")

HEALTH_DEBOUNCE_CRITICAL = 900       # 15 minutes for critical outages (download client down, disk full)
HEALTH_DEBOUNCE_NON_CRITICAL = 7200  # 120 minutes (2h) grace period for unclassified warnings

HOMELAB_ALLOWED_HOSTS = (
    "localhost,127.0.0.1,127.0.0.1,127.0.0.1,*.local,*.home,*.lan,*.localdomain,"
    "host,host,sonarr,radarr,prowlarr,overseerr,seerr,maintainerr,sabnzbd,tautulli,plex"
)

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.nas_config import resolve_nas_config
except ImportError:
    from nas_config import resolve_nas_config

HOST_1_IP, _, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

SONARR_PORT = 8989
RADARR_PORT = 7878
HOMELAB_CHANNEL_ID = 1544955535722545253
DOCKER_APPDATA_DIR = os.environ.get("DOCKER_APPDATA_DIR", os.path.join("/volume1", "docker", "appdata"))

# Benign / cosmetic health check sources that do not represent functional outages or broken services
IGNORED_HEALTH_SOURCES = {
    "UpdateCheck",
    "BranchCheck",
    "PackageMaintainerMessage",
    "MetadataConsumerDeprecated",
    "ProxyFailedTest",
    "SystemTimeCheck",
    "SystemTimeOffset"
}

# Critical health checks that indicate genuine pipeline blockage or service failure
CRITICAL_HEALTH_SOURCES = {
    "DownloadClientCheck",
    "DownloadClientUnavailable",
    "MissingRootFolder",
    "DiskSpaceCheck",
    "DatabaseLocked",
    "PostgresConnectionError",
    "CorruptDatabaseCheck",
    "MountCheck"
}


def _log_triage_action(app: str, category: str, action: str, details: str, auto_remediated: bool = True):
    """Record silent triage receipts in structured audit log."""
    try:
        AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp_pt": datetime.now(PT).strftime("%Y-%m-%d %I:%M:%S %p PT"),
            "app": app,
            "category": category,
            "action": action,
            "details": details,
            "auto_remediated": auto_remediated
        }
        with open(AUDIT_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[ArrWatchdog] Warning writing triage audit log: {e}", file=sys.stderr)


def _get_api_keys() -> tuple[str, str]:
    sonarr_key = os.environ.get("SONARR_API_KEY", "")
    radarr_key = os.environ.get("RADARR_API_KEY", "")
    if sonarr_key and radarr_key:
        return sonarr_key, radarr_key

    c = [
        "ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
        f"{SSH_USER}@{HOST_1_IP}",
        f"cat {DOCKER_APPDATA_DIR}/sonarr/config.xml {DOCKER_APPDATA_DIR}/radarr/config.xml 2>/dev/null"
    ]
    try:
        res = subprocess.run(c, capture_output=True, text=True, timeout=12)
        keys = re.findall(r"<ApiKey>(.*?)</ApiKey>", res.stdout)
        if len(keys) >= 2:
            return keys[0], keys[1]
        elif len(keys) == 1:
            return keys[0], keys[0]
    except Exception as e:
        print(f"[ArrWatchdog] Warning: failed to fetch API keys over SSH: {e}", file=sys.stderr)

    return "", ""


def _ssh_cmd(cmd: str, timeout: int = 15) -> tuple[int, str, str]:
    c = [
        "ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
        f"{SSH_USER}@{HOST_1_IP}", cmd
    ]
    try:
        res = subprocess.run(c, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except Exception as e:
        return 1, "", str(e)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen_warnings": {}, "seen_health": {}, "last_check_at": None}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def fetch_queue(app: str, port: int, api_key: str) -> list[dict]:
    url = f"http://{HOST_1_IP}:{port}/api/v3/queue/details"
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[ArrWatchdog] Error querying {app} queue: {e}", file=sys.stderr)
        return []


def fetch_health(app: str, port: int, api_key: str) -> list[dict]:
    url = f"http://{HOST_1_IP}:{port}/api/v3/health"
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[ArrWatchdog] Error querying {app} health: {e}", file=sys.stderr)
        return []


def trigger_arr_refresh(port: int, api_key: str):
    url = f"http://{HOST_1_IP}:{port}/api/v3/command"
    req = urllib.request.Request(
        url,
        data=json.dumps({"name": "RefreshMonitoredDownloads"}).encode("utf-8"),
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return True
    except Exception:
        return False


def _auto_remediate_allowed_hosts(app_name: str, port: int, api_key: str) -> bool:
    """Tier 1: Silently apply homelab allowedHosts whitelist via API and trigger CheckHealth."""
    url = f"http://{HOST_1_IP}:{port}/api/v3/config/host"
    try:
        req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=8) as resp:
            cfg = json.loads(resp.read().decode())

        current_hosts = cfg.get("allowedHosts", "")
        if current_hosts and "127.0.0.1" in current_hosts and "host" in current_hosts:
            return True

        cfg["allowedHosts"] = HOMELAB_ALLOWED_HOSTS
        req_put = urllib.request.Request(
            url,
            data=json.dumps(cfg).encode("utf-8"),
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            method="PUT"
        )
        with urllib.request.urlopen(req_put, timeout=8) as put_resp:
            pass

        cmd_url = f"http://{HOST_1_IP}:{port}/api/v3/command"
        req_cmd = urllib.request.Request(
            cmd_url,
            data=json.dumps({"name": "CheckHealth"}).encode("utf-8"),
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req_cmd, timeout=8) as cmd_resp:
            pass

        _log_triage_action(
            app=app_name,
            category="health",
            action="auto_configured_allowed_hosts",
            details="Configured homelab allowedHosts whitelist and triggered CheckHealth",
            auto_remediated=True
        )
        return True
    except Exception as e:
        print(f"[ArrWatchdog] Failed to auto-remediate allowedHosts for {app_name}: {e}", file=sys.stderr)
        return False


def run_watchdog(auto_fix: bool = True, force_dispatch: bool = False) -> tuple[bool, str, list[dict]]:
    sonarr_key, radarr_key = _get_api_keys()
    state = _load_state()
    seen_warnings = state.setdefault("seen_warnings", {})
    seen_health = state.setdefault("seen_health", {})

    findings = []
    remediated = []
    active_keys = set()
    active_health_keys = set()
    health_alerts = []
    health_resolutions = []

    apps = [
        ("Radarr", RADARR_PORT, radarr_key, "movie"),
        ("Sonarr", SONARR_PORT, sonarr_key, "series")
    ]

    now_ts = time.time()
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")

    for app_name, port, key, entity_type in apps:
        # 1. Inspect queue & download imports
        queue = fetch_queue(app_name, port, key)
        for item in queue:
            tracked_status = item.get("trackedDownloadStatus", "").lower()
            status = item.get("status", "").lower()
            status_msgs = item.get("statusMessages", [])
            item_title = item.get("title") or "Unknown"
            download_id = item.get("downloadId") or str(item.get("id"))
            unique_key = f"{app_name}:{download_id}"
            active_keys.add(unique_key)

            has_error_msg = False
            msg_texts = []
            for sm in status_msgs:
                t = sm.get("title", "")
                m = sm.get("messages", [])
                full_msg = f"{t}: {'; '.join(m)}" if m else t
                msg_texts.append(full_msg)
                low_m = full_msg.lower()
                if any(w in low_m for w in ["denied", "failed to import", "unauthorized", "missing", "error"]):
                    has_error_msg = True

            is_warning = tracked_status in ("warning", "error") or has_error_msg

            # Benign metadata holds (e.g. TBA title waiting for Skyhook sync) are not queue failures
            is_only_tba = bool(msg_texts and all("tba title" in m.lower() for m in msg_texts))
            if is_only_tba and not has_error_msg:
                continue

            if not is_warning:
                continue

            entity = item.get(entity_type, {})
            target_path = entity.get("path") or ""
            nas_path = target_path
            if target_path.startswith("/data/"):
                nas_path = f"/volume1{target_path}"

            is_perm_issue = False
            owner_uid = None
            if nas_path:
                code, out, _ = _ssh_cmd(f"ls -ldn '{nas_path}' 2>/dev/null")
                if code == 0 and out.strip():
                    parts = out.strip().split()
                    if len(parts) >= 3:
                        owner_uid = parts[2]
                        if owner_uid == "0":
                            is_perm_issue = True

            remediation_done = False
            if is_perm_issue and auto_fix and nas_path:
                fix_code, _, fix_err = _ssh_cmd(f"sudo chown -R 1026:100 '{nas_path}'")
                if fix_code == 0:
                    remediation_done = True
                    trigger_arr_refresh(port, key)
                    remediated_entry = {
                        "app": app_name,
                        "title": item_title,
                        "path": nas_path,
                        "reason": "Owned by root (UID 0). Auto-remediated to 1026:100 and refreshed scan."
                    }
                    remediated.append(remediated_entry)
                    _log_triage_action(
                        app=app_name,
                        category="queue_permission",
                        action="auto_chown_uid_0",
                        details=f"Path: {nas_path} | Item: {item_title}",
                        auto_remediated=True
                    )

            if not remediation_done:
                findings.append({
                    "app": app_name,
                    "title": item_title,
                    "download_id": download_id,
                    "status": tracked_status or status,
                    "messages": msg_texts,
                    "path": nas_path,
                    "owner_uid": owner_uid,
                    "is_perm_issue": is_perm_issue
                })

        # 2. Inspect /api/v3/health with 3-tier triage
        health_items = fetch_health(app_name, port, key)
        for h in health_items:
            source = h.get("source") or "General"
            htype = (h.get("type") or "warning").lower()
            msg = h.get("message") or ""

            if htype not in ("error", "warning"):
                continue

            # Tier 1 Auto-Remediations:
            if source == "AllowedHostsCheck":
                if auto_fix:
                    if _auto_remediate_allowed_hosts(app_name, port, key):
                        remediated.append({
                            "app": app_name,
                            "title": "AllowedHostsCheck",
                            "path": "General Settings",
                            "reason": "AllowedHosts was unconfigured. Auto-applied LAN host whitelist and triggered health refresh."
                        })
                # Suppress from user-facing outage alerts
                continue

            # Tier 2 Filter: Suppress benign advisory sources and cancellations
            if source in IGNORED_HEALTH_SOURCES or "TaskCanceledException" in msg:
                continue

            hkey = f"{app_name}:{source}"
            active_health_keys.add(hkey)

            # Determine appropriate debounce threshold
            is_critical = (source in CRITICAL_HEALTH_SOURCES) or (htype == "error")
            debounce_limit = HEALTH_DEBOUNCE_CRITICAL if is_critical else HEALTH_DEBOUNCE_NON_CRITICAL

            if hkey not in seen_health:
                seen_health[hkey] = {
                    "app": app_name,
                    "source": source,
                    "type": htype,
                    "message": msg,
                    "is_critical": is_critical,
                    "first_seen_ts": now_ts,
                    "first_seen_str": now_str,
                    "alerted": False
                }
            else:
                seen_health[hkey]["message"] = msg
                seen_health[hkey]["type"] = htype
                seen_health[hkey]["is_critical"] = is_critical
                elapsed = now_ts - seen_health[hkey].get("first_seen_ts", now_ts)
                if (elapsed >= debounce_limit or force_dispatch) and not seen_health[hkey].get("alerted", False):
                    seen_health[hkey]["alerted"] = True
                    health_alerts.append({
                        "app": app_name,
                        "source": source,
                        "type": htype,
                        "message": msg,
                        "is_critical": is_critical,
                        "elapsed_mins": int(elapsed / 60),
                        "first_seen_str": seen_health[hkey].get("first_seen_str", now_str)
                    })

    # Clean up stale queue warnings
    stale_keys = [k for k in seen_warnings if k not in active_keys]
    for k in stale_keys:
        del seen_warnings[k]

    new_warnings = []
    for f in findings:
        ukey = f"{f['app']}:{f['download_id']}"
        sig = f"{f['status']}|{','.join(f['messages'])}"
        if seen_warnings.get(ukey) != sig or force_dispatch:
            seen_warnings[ukey] = sig
            new_warnings.append(f)

    # Check for resolved health issues
    stale_health_keys = [k for k in list(seen_health.keys()) if k not in active_health_keys]
    for k in stale_health_keys:
        entry = seen_health[k]
        if entry.get("alerted", False):
            dur_mins = int((now_ts - entry.get("first_seen_ts", now_ts)) / 60)
            health_resolutions.append({
                "app": entry["app"],
                "source": entry["source"],
                "message": entry["message"],
                "duration_mins": max(dur_mins, 1)
            })
        del seen_health[k]

    state["last_check_at"] = now_str
    _save_state(state)

    lines = []
    if remediated:
        lines.append("🛠️ **Arr Import Auto-Remediation Live**")
        for r in remediated:
            lines.append(f"• **{r['app']}**: `{r['title']}`")
            lines.append(f"  ──► Path: `{r['path']}`")
            lines.append(f"  ──► Action: {r['reason']}")

    if new_warnings:
        if lines:
            lines.append("")
        lines.append("⚠️ **Arr Queue Warnings Detected**")
        for w in new_warnings:
            lines.append(f"• **{w['app']}**: `{w['title']}`")
            for m in w["messages"]:
                lines.append(f"  ──► Issue: {m}")
            if w.get("is_perm_issue"):
                lines.append(f"  ──► Root cause: Directory owned by root (`{w['path']}`). Run `sudo chown -R 1026:100`.")

    if health_alerts:
        if lines:
            lines.append("")
        lines.append("🚨 **Arr Health Outage Alert (Critical Pipeline Blocker)**")
        for ha in health_alerts:
            severity = "CRITICAL" if ha.get("is_critical") else ha['type'].upper()
            lines.append(f"• **{ha['app']}**: `{ha['source']}` ({severity})")
            lines.append(f"  ──► Details: {ha['message']}")
            lines.append(f"  ──► Persisting since: {ha['first_seen_str']} ({ha['elapsed_mins']} mins ago)")

    if health_resolutions:
        if lines:
            lines.append("")
        lines.append("✅ **Arr Health Issue Resolved**")
        for hr in health_resolutions:
            lines.append(f"• **{hr['app']}**: `{hr['source']}`")
            lines.append(f"  ──► The following issue is resolved (cleared after {hr['duration_mins']} mins): {hr['message']}")

    summary_text = "\n".join(lines)
    has_activity = bool(remediated or new_warnings or health_alerts or health_resolutions)

    return has_activity, summary_text, findings + remediated + health_alerts + health_resolutions


def main():
    parser = argparse.ArgumentParser(description="Autonomous Arr Queue & Health Triage Watchdog")
    parser.add_argument("--no-auto-fix", action="store_true", help="Disable automatic root permission remediation")
    parser.add_argument("--force", action="store_true", help="Ignore state cache and evaluate all warnings")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch report via outbox if issues found")
    parser.add_argument("--quiet", action="store_true", help="Suppress output if no action taken")
    args = parser.parse_args()

    has_activity, summary, items = run_watchdog(auto_fix=not args.no_auto_fix, force_dispatch=args.force)

    if has_activity:
        print(summary)
        if args.dispatch:
            from tools.outbox import queue_outbox_message
            queue_outbox_message(
                channel="homelab",
                content=summary
            )
            # Mirror sustained health alerts or restoral notices to #server-updates
            if "Arr Health" in summary:
                queue_outbox_message(
                    channel="server-updates",
                    content=summary
                )
            print("[ArrWatchdog] Dispatched notification to outbox.", file=sys.stderr)
    elif not args.quiet:
        print("[ArrWatchdog] All Radarr and Sonarr queues and health checks nominal (0 issues).")

    sys.exit(0)


if __name__ == "__main__":
    main()
