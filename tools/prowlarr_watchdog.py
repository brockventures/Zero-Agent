#!/usr/bin/env python3
"""Prowlarr Indexer Health & Rate-Limit Watchdog.

Audits Prowlarr on Host 1 (:9696):
1. Detects indexers disabled due to failures or VIP expirations.
2. Flags active backoff cooldowns (disabledTill, failureCount).
3. Inspects Prowlarr system health warnings.
4. Dispatches actionable alerts to #homelab (1544955535722545253).
5. Deduplicates state via /workspace/data/prowlarr_watchdog_state.json.
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
STATE_FILE = Path("/workspace/data/prowlarr_watchdog_state.json")

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "admin")

try:
    from tools.sidecars import _resolve_nas_config
    HOST_1_IP, _, SSH_PORT = _resolve_nas_config()
except Exception:
    HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    SSH_PORT = os.environ.get("NAS_SSH_PORT", "22")

PROWLARR_PORT = 9696
DOCKER_APPDATA_DIR = os.environ.get("DOCKER_APPDATA_DIR", os.path.join("/volume1", "docker", "appdata"))

# Known intentionally disabled indexers (e.g. nzb.su pending Cloudflare resolver)
KNOWN_DISABLED = {"Nzb.su"}


def _get_api_key() -> str:
    key = os.environ.get("PROWLARR_API_KEY", "")
    if key:
        return key

    c = [
        "ssh", "-i", SSH_KEY, "-p", str(SSH_PORT),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
        f"{SSH_USER}@{HOST_1_IP}",
        f"cat {DOCKER_APPDATA_DIR}/prowlarr/config.xml 2>/dev/null"
    ]
    try:
        res = subprocess.run(c, capture_output=True, text=True, timeout=12)
        m = re.search(r"<ApiKey>(.*?)</ApiKey>", res.stdout)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"[ProwlarrWatchdog] Warning: failed to fetch key over SSH: {e}", file=sys.stderr)

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


def check_prowlarr(force: bool = False) -> tuple[bool, str, list[dict]]:
    api_key = _get_api_key()
    state = _load_state()
    seen_issues = state.setdefault("seen_issues", {})
    issues = []
    active_keys = set()

    # 1. Fetch Indexers
    indexers = {}
    try:
        url = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/indexer"
        req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for idx in data:
                indexers[idx.get("id")] = idx
                name = idx.get("name", "Unknown")
                enabled = idx.get("enable", False)
                if not enabled and name not in KNOWN_DISABLED:
                    ukey = f"disabled:{idx.get('id')}"
                    active_keys.add(ukey)
                    issues.append({
                        "type": "disabled",
                        "name": name,
                        "detail": "Indexer is disabled in Prowlarr (likely due to repeated auth/API failures).",
                        "key": ukey
                    })
    except Exception as e:
        return False, f"⚠️ Error querying Prowlarr indexers: {e}", []

    # 2. Fetch Indexer Status (Temporary backoffs / rate limits)
    try:
        url = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/indexerstatus"
        req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for st in data:
                idx_id = st.get("indexerId")
                name = indexers.get(idx_id, {}).get("name", f"ID {idx_id}")
                disabled_till = st.get("disabledTill")
                fails = st.get("failureCount", 0)
                if fails > 5 or disabled_till:
                    ukey = f"backoff:{idx_id}"
                    active_keys.add(ukey)
                    issues.append({
                        "type": "backoff",
                        "name": name,
                        "detail": f"Throttled/Backing off (failures: {fails}, backoff until: {disabled_till}).",
                        "key": ukey
                    })
    except Exception as e:
        print(f"[ProwlarrWatchdog] Warning checking indexerstatus: {e}", file=sys.stderr)

    # 3. Fetch Health
    try:
        url = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/health"
        req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for h in data:
                src = h.get("source", "")
                msg = h.get("message", "")
                typ = h.get("type", "warning")
                # Exclude TaskCanceledException known noise
                if "TaskCanceledException" in msg:
                    continue
                ukey = f"health:{src}:{msg[:30]}"
                active_keys.add(ukey)
                issues.append({
                    "type": typ,
                    "name": src,
                    "detail": msg,
                    "key": ukey
                })
    except Exception as e:
        print(f"[ProwlarrWatchdog] Warning checking health: {e}", file=sys.stderr)

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
        return False, "(nominal - 0 Prowlarr indexer failures)", issues

    lines = ["⚠️ **Prowlarr Indexer Health Warning**"]
    for i in new_issues:
        lines.append(f"• **{i['name']}**")
        lines.append(f"  ──► {i['detail']}")
    lines.append("\n*Action:* Check Prowlarr (:9696) Indexers & VIP status.")

    return True, "\n".join(lines), new_issues


def main():
    parser = argparse.ArgumentParser(description="Prowlarr Indexer Health Watchdog")
    parser.add_argument("--force", action="store_true", help="Ignore state cache")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch alert to #homelab outbox")
    parser.add_argument("--quiet", action="store_true", help="Suppress nominal output")
    args = parser.parse_args()

    has_activity, summary, items = check_prowlarr(force=args.force)

    if has_activity:
        print(summary)
        if args.dispatch:
            from tools.outbox import queue_outbox_message
            queue_outbox_message(channel="homelab", content=summary)
            print("[ProwlarrWatchdog] Dispatched to #homelab outbox.", file=sys.stderr)
    elif not args.quiet:
        print("[ProwlarrWatchdog] All Prowlarr indexers nominal.")

    sys.exit(0)


if __name__ == "__main__":
    main()
