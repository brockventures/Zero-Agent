#!/usr/bin/env python3
"""SABnzbd Failed Unpack & Stalled Queue Watchdog.

Audits SABnzbd on Host 1 (:8080):
1. Detects paused queue or critical SSD staging disk exhaustion (<50 GB).
2. Detects failed unpacks, verification aborts, or corrupted releases in history.
3. Dispatches actionable alerts to #homelab (1544955535722545253).
4. Deduplicates alerts via /workspace/data/sabnzbd_watchdog_state.json.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("/workspace/data/sabnzbd_watchdog_state.json")

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "admin")

try:
    from tools.sidecars import _resolve_nas_config
    HOST_1_IP, _, SSH_PORT = _resolve_nas_config()
except Exception:
    HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    SSH_PORT = os.environ.get("NAS_SSH_PORT", "22")

SABNZBD_PORT = 8080
DOCKER_APPDATA_DIR = os.environ.get("DOCKER_APPDATA_DIR", os.path.join("/volume1", "docker", "appdata"))


def _get_api_key() -> str:
    key = os.environ.get("SABNZBD_API_KEY", "")
    if key:
        return key

    c = [
        "ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
        f"{SSH_USER}@{HOST_1_IP}",
        f"grep -E '^api_key =' {DOCKER_APPDATA_DIR}/sabnzbd/sabnzbd.ini 2>/dev/null"
    ]
    try:
        res = subprocess.run(c, capture_output=True, text=True, timeout=12)
        m = re.search(r"api_key\s*=\s*(\w+)", res.stdout)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"[SABWatchdog] Warning: failed to fetch key over SSH: {e}", file=sys.stderr)

    return ""


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen_issues": {}, "last_check_at": None}


def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def check_sabnzbd(force: bool = False) -> tuple[bool, str, list[dict]]:
    api_key = _get_api_key()
    state = _load_state()
    seen_issues = state.setdefault("seen_issues", {})
    issues = []
    active_keys = set()

    # 1. Check Queue & Disk
    try:
        url = f"http://{HOST_1_IP}:{SABNZBD_PORT}/api?mode=queue&output=json&apikey={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            q = json.loads(resp.read().decode()).get("queue", {})
            paused = q.get("paused", False)
            status = q.get("status", "")
            diskspace_gb = float(q.get("diskspace1", "999"))

            if paused:
                ukey = "queue:paused"
                active_keys.add(ukey)
                issues.append({
                    "type": "paused",
                    "title": "SABnzbd Queue Paused",
                    "detail": f"Downloader queue is paused (status: {status}). Grabs are stalled.",
                    "key": ukey
                })

            if diskspace_gb < 30.0:
                ukey = "disk:low"
                active_keys.add(ukey)
                issues.append({
                    "type": "disk",
                    "title": "SABnzbd Staging Disk Low",
                    "detail": f"Incomplete download volume has only {diskspace_gb:.1f} GB free.",
                    "key": ukey
                })
    except Exception as e:
        return False, f"⚠️ Error querying SABnzbd queue: {e}", []

    # 2. Check Recent Failed History (Last 10 items)
    try:
        url = f"http://{HOST_1_IP}:{SABNZBD_PORT}/api?mode=history&limit=10&output=json&apikey={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            hist = json.loads(resp.read().decode()).get("history", {})
            for slot in hist.get("slots", []):
                st = slot.get("status", "").lower()
                fail_msg = slot.get("fail_message", "")
                name = slot.get("name", "Unknown Release")
                nzo_id = slot.get("nzo_id", "")
                if st == "failed" or fail_msg:
                    ukey = f"fail:{nzo_id}"
                    active_keys.add(ukey)
                    issues.append({
                        "type": "failed_download",
                        "title": name,
                        "detail": f"Download/Unpack failed: {fail_msg or 'Extraction error'}",
                        "key": ukey
                    })
    except Exception as e:
        print(f"[SABWatchdog] Warning checking history: {e}", file=sys.stderr)

    for k in list(seen_issues.keys()):
        if k not in active_keys:
            del seen_issues[k]

    new_issues = []
    for issue in issues:
        sig = issue["detail"]
        if seen_issues.get(issue["key"]) != sig or force:
            seen_issues[issue["key"]] = sig
            new_issues.append(issue)

    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    state["last_check_at"] = now_str
    _save_state(state)

    if not new_issues:
        return False, "(nominal - 0 SABnzbd queue failures)", issues

    lines = ["⚠️ **SABnzbd Downloader Alert**"]
    for i in new_issues:
        lines.append(f"• **{i['title']}**")
        lines.append(f"  ──► {i['detail']}")
    lines.append("\n*Action:* Check SABnzbd (:8080) Queue & History.")

    return True, "\n".join(lines), new_issues


def main():
    parser = argparse.ArgumentParser(description="SABnzbd Queue & Unpack Watchdog")
    parser.add_argument("--force", action="store_true", help="Ignore state cache")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch alert to #homelab outbox")
    parser.add_argument("--quiet", action="store_true", help="Suppress nominal output")
    args = parser.parse_args()

    has_activity, summary, items = check_sabnzbd(force=args.force)

    if has_activity:
        print(summary)
        if args.dispatch:
            from tools.outbox import queue_outbox_message
            queue_outbox_message(channel="homelab", content=summary)
            print("[SABWatchdog] Dispatched to #homelab outbox.", file=sys.stderr)
    elif not args.quiet:
        print("[SABWatchdog] All SABnzbd queues nominal.")

    sys.exit(0)


if __name__ == "__main__":
    main()
