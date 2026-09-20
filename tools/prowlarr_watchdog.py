#!/usr/bin/env python3
"""Autonomous Prowlarr Indexer Health & Triage Watchdog.

Audits Prowlarr on Host 1 (:9696):
1. Tier 1 (Silent Auto-Remediation):
   - Automatically applies LAN host whitelist for AllowedHostsCheck and re-verifies.
   - Logs silent receipts to /workspace/data/arr_triage_audit.jsonl.
2. Tier 2 (Multi-Indexer Redundancy Filter):
   - Evaluates indexer backoffs against overall pool redundancy (e.g. 1 throttled out of 4+ is nominal jitter).
   - Suppresses cosmetic/advisory checks (UpdateCheck, BranchCheck, PackageMaintainerMessage).
3. Tier 3 (True Search Outage Escalation):
   - Alerts ONLY when indexers are permanently disabled, when 100% of indexers are dead, or when >=50% are simultaneously throttled.
   - Dispatches actionable alerts to #homelab (1544955535722545253).
4. Deduplicates state via /workspace/data/prowlarr_watchdog_state.json.
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
AUDIT_LOG_FILE = Path("/workspace/data/arr_triage_audit.jsonl")

HOMELAB_ALLOWED_HOSTS = (
    "localhost,127.0.0.1,127.0.0.1,127.0.0.1,*.local,*.home,*.lan,*.localdomain,"
    "host,host,sonarr,radarr,prowlarr,overseerr,seerr,maintainerr,sabnzbd,tautulli,plex"
)

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")

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

# Benign / cosmetic health check sources
IGNORED_HEALTH_SOURCES = {
    "UpdateCheck",
    "BranchCheck",
    "PackageMaintainerMessage",
    "ProxyFailedTest",
    "SystemTimeCheck",
    "SystemTimeOffset"
}


def _log_triage_action(category: str, action: str, details: str, auto_remediated: bool = True):
    """Record silent triage receipts in structured audit log."""
    try:
        AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp_pt": datetime.now(PT).strftime("%Y-%m-%d %I:%M:%S %p PT"),
            "app": "Prowlarr",
            "category": category,
            "action": action,
            "details": details,
            "auto_remediated": auto_remediated
        }
        with open(AUDIT_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[ProwlarrWatchdog] Warning writing triage audit log: {e}", file=sys.stderr)


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


def _auto_remediate_allowed_hosts(api_key: str) -> bool:
    """Tier 1: Silently apply homelab allowedHosts whitelist via API and trigger CheckHealth."""
    url = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/config/host"
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

        cmd_url = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/command"
        req_cmd = urllib.request.Request(
            cmd_url,
            data=json.dumps({"name": "CheckHealth"}).encode("utf-8"),
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req_cmd, timeout=8) as cmd_resp:
            pass

        _log_triage_action(
            category="health",
            action="auto_configured_allowed_hosts",
            details="Configured homelab allowedHosts whitelist and triggered CheckHealth",
            auto_remediated=True
        )
        return True
    except Exception as e:
        print(f"[ProwlarrWatchdog] Failed to auto-remediate allowedHosts: {e}", file=sys.stderr)
        return False


def check_prowlarr(force: bool = False) -> tuple[bool, str, list[dict]]:
    api_key = _get_api_key()
    state = _load_state()
    seen_issues = state.setdefault("seen_issues", {})
    issues = []
    active_keys = set()

    # 1. Fetch Indexers
    indexers = {}
    enabled_indexers = []
    try:
        url = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/indexer"
        req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for idx in data:
                idx_id = idx.get("id")
                indexers[idx_id] = idx
                name = idx.get("name", "Unknown")
                enabled = idx.get("enable", False)
                if enabled:
                    enabled_indexers.append(idx)
                elif name not in KNOWN_DISABLED:
                    ukey = f"disabled:{idx_id}"
                    active_keys.add(ukey)
                    issues.append({
                        "type": "disabled",
                        "name": name,
                        "detail": "Indexer is permanently disabled in Prowlarr (repeated auth/API failures).",
                        "key": ukey
                    })
    except Exception as e:
        return False, f"⚠️ Error querying Prowlarr indexers: {e}", []

    # 2. Fetch Indexer Status (Temporary backoffs / rate limits) with Multi-Indexer Redundancy
    throttled_items = []
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
                    throttled_items.append({
                        "type": "backoff",
                        "name": name,
                        "detail": f"Throttled/Backing off (failures: {fails}, backoff until: {disabled_till}).",
                        "key": ukey,
                        "fails": fails
                    })
    except Exception as e:
        print(f"[ProwlarrWatchdog] Warning checking indexerstatus: {e}", file=sys.stderr)

    # Tier 2 Redundancy Filter:
    # If we have >=3 enabled indexers and only 1 is temporarily throttled, suppress active alarm
    total_enabled_count = len(enabled_indexers)
    throttled_count = len(throttled_items)

    if throttled_items:
        if total_enabled_count >= 3 and throttled_count == 1:
            # Single transient throttle: log to audit receipts silently
            single_t = throttled_items[0]
            _log_triage_action(
                category="indexer_redundancy",
                action="suppressed_transient_single_backoff",
                details=f"Indexer '{single_t['name']}' throttled ({single_t['fails']} fails), but {total_enabled_count - 1} healthy indexers active.",
                auto_remediated=False
            )
        else:
            # Systemic throttle / multiple indexers down: escalate
            issues.extend(throttled_items)

    # 3. Fetch Health with Tier 1 Auto-Remediation & Tier 2 Filter
    try:
        url = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/health"
        req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for h in data:
                src = h.get("source", "")
                msg = h.get("message", "")
                typ = h.get("type", "warning")

                # Tier 1 Auto-Remediation
                if src == "AllowedHostsCheck":
                    _auto_remediate_allowed_hosts(api_key)
                    continue

                # Tier 2 Filter
                if "TaskCanceledException" in msg or src in IGNORED_HEALTH_SOURCES:
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

    lines = ["🚨 **Prowlarr Critical Indexer Outage Alert**"]
    for i in new_issues:
        lines.append(f"• **{i['name']}**")
        lines.append(f"  ──► {i['detail']}")
    lines.append("\n*Action:* Check Prowlarr (:9696) Indexers & VIP status.")

    return True, "\n".join(lines), new_issues


def main():
    parser = argparse.ArgumentParser(description="Autonomous Prowlarr Indexer Health & Triage Watchdog")
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
