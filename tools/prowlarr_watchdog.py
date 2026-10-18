#!/usr/bin/env python3
"""Autonomous Prowlarr Indexer Health & Triage Watchdog.

Audits Prowlarr on Host 1 (:9696):
1. Tier 1 (Silent Auto-Remediation & Self-Healing Probe):
   - Automatically applies LAN host whitelist for AllowedHostsCheck and re-verifies.
   - Actively tests indexers in backoff/throttle via live probe (POST /api/v1/indexer/test).
   - If probe passes, self-heals Prowlarr state (PUT /api/v1/indexer/{id} and CheckHealth).
   - Logs silent receipts to /workspace/data/arr_triage_audit.jsonl.
2. Tier 2 (Multi-Indexer Redundancy Filter):
   - Evaluates indexer backoffs against overall pool redundancy (e.g. 1 throttled out of 2+ enabled
     indexers is suppressed as transient jitter if other healthy indexers remain active).
   - Suppresses cosmetic/advisory checks (UpdateCheck, BranchCheck, PackageMaintainerMessage).
3. Tier 3 (True Search Outage Escalation & Resolution Tracking):
   - Alerts ONLY when indexers are permanently disabled, when 100% of indexers are dead,
     or when multiple indexers are simultaneously failing with 0 healthy redundancy.
   - Dispatches actionable alerts to #homelab (1544955535722545253).
   - Dispatches clean resolution notices when previously alerted outages recover.
4. Deduplicates state via /workspace/data/prowlarr_watchdog_state.json.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
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
for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.nas_config import resolve_nas_config
except ImportError:
    from nas_config import resolve_nas_config

HOST_1_IP, _, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()

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
        if current_hosts and HOST_1_IP in current_hosts and "host" in current_hosts:
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


def _probe_and_self_heal_indexer(api_key: str, indexer: dict, retries: int = 1) -> bool:
    """Tier 1: Active self-healing probe.
    
    Tests indexer endpoint via Prowlarr API. If successful, resets Prowlarr's backoff state
    by saving the indexer config and refreshing health check.
    """
    idx_id = indexer.get("id")
    name = indexer.get("name", f"ID {idx_id}")
    url_test = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/indexer/test"

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url_test,
                data=json.dumps(indexer).encode("utf-8"),
                headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = getattr(resp, "status", 200)
                if status in (200, 202):
                    # Live probe passed! Indexer is responding.
                    # Self-heal Prowlarr state: PUT indexer resets backoff in SQLite, CheckHealth clears warning.
                    try:
                        url_put = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/indexer/{idx_id}"
                        req_put = urllib.request.Request(
                            url_put,
                            data=json.dumps(indexer).encode("utf-8"),
                            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                            method="PUT"
                        )
                        with urllib.request.urlopen(req_put, timeout=8) as put_resp:
                            pass

                        url_cmd = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/command"
                        req_cmd = urllib.request.Request(
                            url_cmd,
                            data=json.dumps({"name": "CheckHealth"}).encode("utf-8"),
                            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                            method="POST"
                        )
                        with urllib.request.urlopen(req_cmd, timeout=8) as cmd_resp:
                            pass
                    except Exception as reset_err:
                        print(f"[ProwlarrWatchdog] Warning during indexer state reset: {reset_err}", file=sys.stderr)

                    _log_triage_action(
                        category="indexer_self_healing",
                        action="auto_probed_and_recovered",
                        details=f"Indexer '{name}' recovered via active probe test (attempt {attempt + 1}) and backoff reset.",
                        auto_remediated=True
                    )
                    return True
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5)
            else:
                print(f"[ProwlarrWatchdog] Active probe failed for '{name}' after {retries + 1} attempts: {e}", file=sys.stderr)

    return False


def check_prowlarr(force: bool = False) -> tuple[bool, str, list[dict]]:
    api_key = _get_api_key()
    state = _load_state()
    seen_issues = state.setdefault("seen_issues", {})
    issues = []
    active_keys = set()
    resolutions = []

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

    # 2. Fetch Indexer Status (Temporary backoffs / rate limits) with Active Self-Healing & Redundancy
    throttled_items = []
    healed_indexer_ids = set()
    try:
        url = f"http://{HOST_1_IP}:{PROWLARR_PORT}/api/v1/indexerstatus"
        req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            for st in data:
                idx_id = st.get("indexerId")
                idx_obj = indexers.get(idx_id)
                name = idx_obj.get("name", f"ID {idx_id}") if idx_obj else f"ID {idx_id}"
                disabled_till = st.get("disabledTill")
                fails = st.get("failureCount", 0)
                if fails > 0 or disabled_till:
                    # Tier 1 Active Self-Healing Probe:
                    if idx_obj and _probe_and_self_heal_indexer(api_key, idx_obj):
                        healed_indexer_ids.add(idx_id)
                        continue

                    ukey = f"backoff:{idx_id}"
                    active_keys.add(ukey)
                    throttled_items.append({
                        "type": "backoff",
                        "name": name,
                        "detail": f"Throttled/Backing off (failures: {fails}, backoff until: {disabled_till}).",
                        "key": ukey,
                        "fails": fails,
                        "indexerId": idx_id
                    })
    except Exception as e:
        print(f"[ProwlarrWatchdog] Warning checking indexerstatus: {e}", file=sys.stderr)

    # Tier 2 Redundancy Filter:
    # If we have at least 2 enabled indexers and only 1 is throttled (i.e. at least 1 healthy indexer remains),
    # suppress active alarm as transient jitter/backoff and log silent receipt.
    total_enabled_count = len(enabled_indexers)
    throttled_count = len(throttled_items)
    healthy_count = total_enabled_count - throttled_count

    if throttled_items:
        if total_enabled_count >= 2 and throttled_count == 1:
            # Single transient throttle: log to audit receipts silently
            single_t = throttled_items[0]
            _log_triage_action(
                category="indexer_redundancy",
                action="suppressed_transient_single_backoff",
                details=f"Indexer '{single_t['name']}' throttled ({single_t['fails']} fails), but {healthy_count} healthy indexers active.",
                auto_remediated=False
            )
        else:
            # Systemic throttle / multiple indexers down / 100% of indexers dead: escalate
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

                # Tier 1 / Tier 2 IndexerStatusCheck handling:
                if src == "IndexerStatusCheck":
                    failing_names = []
                    m = re.search(r"failures:\s*(.*)", msg, re.IGNORECASE)
                    if m:
                        failing_names = [x.strip() for x in m.group(1).split(",") if x.strip()]

                    all_healed = True
                    for fname in failing_names:
                        matched_idx = next((idx for idx in indexers.values() if idx.get("name", "").lower() == fname.lower()), None)
                        if matched_idx:
                            mid = matched_idx.get("id")
                            if mid in healed_indexer_ids:
                                continue
                            if _probe_and_self_heal_indexer(api_key, matched_idx):
                                healed_indexer_ids.add(mid)
                                continue
                        all_healed = False

                    if all_healed:
                        continue

                    # If covered by Tier 2 redundancy filter (single indexer failing out of 2+ enabled):
                    if total_enabled_count >= 2 and len(failing_names) == 1 and healthy_count >= 1:
                        _log_triage_action(
                            category="indexer_redundancy",
                            action="suppressed_health_indexer_status_check",
                            details=f"Suppressed IndexerStatusCheck for '{failing_names[0]}'; {healthy_count} healthy indexers active.",
                            auto_remediated=False
                        )
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

    # Clean up stale issues and detect resolutions
    for k in list(seen_issues.keys()):
        if k not in active_keys:
            entry = seen_issues[k]
            was_alerted = entry.get("alerted", False) if isinstance(entry, dict) else False
            if was_alerted:
                detail = entry.get("detail", k) if isinstance(entry, dict) else str(entry)
                resolutions.append({
                    "key": k,
                    "detail": detail
                })
            del seen_issues[k]

    new_issues = []
    for issue in issues:
        sig = issue["detail"]
        old_val = seen_issues.get(issue["key"])
        old_sig = old_val.get("detail") if isinstance(old_val, dict) else old_val

        if old_sig != sig or force:
            seen_issues[issue["key"]] = {
                "detail": sig,
                "first_seen": datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT"),
                "alerted": True
            }
            new_issues.append(issue)

    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    state["last_check_at"] = now_str
    _save_state(state)

    if not new_issues and not resolutions:
        return False, "(nominal - 0 Prowlarr indexer failures)", issues

    lines = []
    if new_issues:
        lines.append("🚨 **Prowlarr Critical Indexer Outage Alert**")
        for i in new_issues:
            lines.append(f"• **{i['name']}**")
            lines.append(f"  ──► {i['detail']}")
        lines.append("\n*Action:* Check Prowlarr (:9696) Indexers & VIP status.")

    if resolutions:
        if lines:
            lines.append("")
        lines.append("✅ **Prowlarr Indexer Issue Resolved**")
        for r in resolutions:
            lines.append(f"• Issue resolved: `{r['detail']}`")

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
