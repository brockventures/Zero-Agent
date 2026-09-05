#!/usr/bin/env python3
"""Home Assistant Integration Re-Auth & Setup Error Watchdog.

Audits Home Assistant config entries and persistent notifications:
1. Detects integrations in setup_error, migration_error, or requiring re-auth.
2. Filters known benign/powered-off devices (OctoPrint, Projector, IP Webcam).
3. Dispatches immediate actionable alerts to #home-assistant (1544953275877556334).
4. Deduplicates alerts using /workspace/data/ha_reauth_state.json.
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("/workspace/data/ha_reauth_state.json")
HOME_ASSISTANT_CHANNEL_ID = 1544953275877556334

# Devices that routinely enter setup_retry when powered off at the wall
BENIGN_STANDBY_DOMAINS = {"octoprint", "android_ip_webcam", "androidtv_remote"}


def _get_ha_config() -> tuple[str, str]:
    base_url = os.environ.get("HA_BASE_URL", "http://127.0.0.1:8123").rstrip("/")
    token = ""
    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("HA_BASE_URL"):
                    base_url = d["HA_BASE_URL"].rstrip("/")
                if d.get("HA_ACCESS_TOKEN"):
                    token = d["HA_ACCESS_TOKEN"]
        except Exception:
            pass
    if not token and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                if d.get("token"):
                    token = d["token"]
        except Exception:
            pass
    return base_url, token


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


def check_ha_integrations(force: bool = False) -> tuple[bool, str, list[dict]]:
    base_url, token = _get_ha_config()
    if not token:
        return False, "⚠️ Missing HA_ACCESS_TOKEN", []

    state = _load_state()
    seen_issues = state.setdefault("seen_issues", {})
    issues = []
    active_keys = set()

    # 1. Check Config Entries
    try:
        url = f"{base_url}/api/config/config_entries/entry"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            entries = json.loads(resp.read().decode())
    except Exception as e:
        return False, f"⚠️ Error querying HA config entries: {e}", []

    for e in entries:
        st = e.get("state")
        domain = e.get("domain", "")
        title = e.get("title", domain)
        entry_id = e.get("entry_id", "")
        reason = e.get("reason") or ""
        disabled = bool(e.get("disabled_by"))

        if disabled or st in ("loaded", "not_loaded"):
            continue

        # Ignore standby devices in setup_retry
        if st == "setup_retry" and domain in BENIGN_STANDBY_DOMAINS:
            continue

        is_reauth = "reauth" in str(reason).lower() or "authentication" in str(reason).lower() or st == "setup_error"
        unique_key = f"entry:{entry_id}"
        active_keys.add(unique_key)

        issues.append({
            "type": "reauth" if is_reauth else "error",
            "domain": domain,
            "title": title,
            "state": st,
            "reason": reason or f"Integration state: {st}",
            "key": unique_key
        })

    # 2. Check Persistent Notifications
    try:
        url = f"{base_url}/api/states"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            states = json.loads(resp.read().decode())
        for s in states:
            eid = s.get("entity_id", "")
            if eid.startswith("persistent_notification."):
                attrs = s.get("attributes", {})
                title = attrs.get("title") or eid
                msg = attrs.get("message") or ""
                low = f"{title} {msg}".lower()
                if any(k in low for k in ["re-authenticate", "reauth", "login", "expired", "failed to setup"]):
                    unique_key = f"notif:{eid}"
                    active_keys.add(unique_key)
                    issues.append({
                        "type": "notification",
                        "domain": "persistent_notification",
                        "title": title,
                        "state": "active",
                        "reason": msg[:180],
                        "key": unique_key
                    })
    except Exception as e:
        print(f"[HAReauthWatchdog] Warning checking notifications: {e}", file=sys.stderr)

    # Prune cleared issues from state
    for k in list(seen_issues.keys()):
        if k not in active_keys:
            del seen_issues[k]

    new_issues = []
    for issue in issues:
        sig = f"{issue['state']}|{issue['reason']}"
        if seen_issues.get(issue["key"]) != sig or force:
            seen_issues[issue["key"]] = sig
            new_issues.append(issue)

    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    state["last_check_at"] = now_str
    _save_state(state)

    if not new_issues:
        return False, "(nominal - 0 HA integration failures)", issues

    lines = ["🚨 **Home Assistant Integration Alert**"]
    for i in new_issues:
        lines.append(f"• **{i['title']}** (`{i['domain']}`)")
        lines.append(f"  ──► State: `{i['state']}`")
        lines.append(f"  ──► Detail: {i['reason']}")
    lines.append("\n*Action:* Check HA Settings ──► Devices & Services to re-authenticate or reload.")

    return True, "\n".join(lines), new_issues


def main():
    parser = argparse.ArgumentParser(description="Home Assistant Re-Auth Watchdog")
    parser.add_argument("--force", action="store_true", help="Ignore state cache")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch alert to #home-assistant outbox")
    parser.add_argument("--quiet", action="store_true", help="Suppress nominal output")
    args = parser.parse_args()

    has_activity, summary, items = check_ha_integrations(force=args.force)

    if has_activity:
        print(summary)
        if args.dispatch:
            from tools.outbox import queue_outbox_message
            queue_outbox_message(channel="home-assistant", content=summary)
            print("[HAReauthWatchdog] Dispatched to #home-assistant outbox.", file=sys.stderr)
    elif not args.quiet:
        print("[HAReauthWatchdog] All Home Assistant integrations nominal.")

    sys.exit(0)


if __name__ == "__main__":
    main()
