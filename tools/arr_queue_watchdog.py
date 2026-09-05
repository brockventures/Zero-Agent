#!/usr/bin/env python3
"""Autonomous Queue Watchdog for Radarr and Sonarr.

Monitors download and import queues across Host 1 (:7878 / :8989):
1. Detects import warnings, permission denials, and stalled items.
2. Auto-remediates root-owned permission snags on Host 1 over SSH.
3. Dispatches actionable alerts or resolution notices to #homelab (1544955535722545253).
4. Persists state in /workspace/data/arr_queue_state.json to prevent duplicate spam.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("/workspace/data/arr_queue_state.json")

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "admin")

try:
    from tools.sidecars import _resolve_nas_config
    HOST_1_IP, _, SSH_PORT = _resolve_nas_config()
except Exception:
    HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    SSH_PORT = os.environ.get("NAS_SSH_PORT", "22")

SONARR_PORT = 8989
RADARR_PORT = 7878
HOMELAB_CHANNEL_ID = 1544955535722545253
DOCKER_APPDATA_DIR = os.environ.get("DOCKER_APPDATA_DIR", os.path.join("/volume1", "docker", "appdata"))


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
    return {"seen_warnings": {}, "last_check_at": None}


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


def run_watchdog(auto_fix: bool = True, force_dispatch: bool = False) -> tuple[bool, str, list[dict]]:
    sonarr_key, radarr_key = _get_api_keys()
    state = _load_state()
    seen_warnings = state.setdefault("seen_warnings", {})

    findings = []
    remediated = []
    active_keys = set()

    apps = [
        ("Radarr", RADARR_PORT, radarr_key, "movie"),
        ("Sonarr", SONARR_PORT, sonarr_key, "series")
    ]

    for app_name, port, key, entity_type in apps:
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
                    remediated.append({
                        "app": app_name,
                        "title": item_title,
                        "path": nas_path,
                        "reason": "Owned by root (UID 0). Auto-remediated to 1026:100 and refreshed scan."
                    })

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

    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
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

    summary_text = "\n".join(lines)
    has_activity = bool(remediated or new_warnings)

    return has_activity, summary_text, findings + remediated


def main():
    parser = argparse.ArgumentParser(description="Autonomous Arr Queue Watchdog")
    parser.add_argument("--no-auto-fix", action="store_true", help="Disable automatic root permission remediation")
    parser.add_argument("--force", action="store_true", help="Ignore state cache and evaluate all warnings")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch report to #homelab via outbox if issues found")
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
            print("[ArrWatchdog] Dispatched notification to #homelab outbox.", file=sys.stderr)
    elif not args.quiet:
        print("[ArrWatchdog] All Radarr and Sonarr queues nominal (0 import failures).")

    sys.exit(0)


if __name__ == "__main__":
    main()
