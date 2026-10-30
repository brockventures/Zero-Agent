#!/usr/bin/env python3
"""Nest x Yale Lock & Homebridge Self-Healing Watchdog.

Monitors the end-to-end communication chain:
Home Assistant <-> HomeKit Controller <-> Homebridge (homebridge-nest) <-> Google Nest Cloud.

Detection:
1. Entity State: Checks if lock.entryway_lock is 'unavailable', 'unknown', or stuck in 'unlocking'/'locking' (>25s).
2. Bridge Logs: Checks Host 1 Homebridge logs for Google Nest auth failures (ETIMEDOUT, token rejection).

Remediation:
1. Restarts Homebridge on Host 1 with compose semantics.
2. Reloads HomeKit Controller config entry (01K53AH7KZKS98251N1XESJ901) in Home Assistant.
3. Verifies device recovery and re-dispatches unlock if an in-flight arrival unlock was interrupted.
4. Alerts #home-assistant and #zero-chat with forensic receipts.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

try:
    import tools.nas_config as nas_config
    from tools.nas_config import HOST_1_IP, run_ssh
except ImportError:
    try:
        import nas_config
        from nas_config import HOST_1_IP, run_ssh
    except ImportError:
        nas_config = None
        HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
        run_ssh = None

try:
    from tools.ha_battery_check import get_ha_config
except ImportError:
    try:
        from ha_battery_check import get_ha_config
    except ImportError:
        def get_ha_config():
            return f"http://{HOST_1_IP}:8123", ""

try:
    from tools.outbox import queue_outbox_message
except ImportError:
    try:
        from outbox import queue_outbox_message
    except ImportError:
        queue_outbox_message = None

PT = ZoneInfo("America/Los_Angeles")
STATE_FILE = Path("/workspace/data/nest_lock_state.json")
HOME_ASSISTANT_CHANNEL_ID = 1544953275877556334
ZERO_CHAT_CHANNEL_ID = 1542081375287640084
HOMEKIT_CONFIG_ENTRY_ID = "01K53AH7KZKS98251N1XESJ901"
LOCK_ENTITY_ID = "lock.entryway_lock"

COOLDOWN_SECONDS = 180  # 3 minutes minimum between restart attempts
TRANSITION_TIMEOUT_SECONDS = 25.0  # Physical bolt throws in ~2.5s; >25s means stuck
MAX_CONSECUTIVE_FAILURES = 3
VERIFY_TIMEOUT_SECONDS = 65.0  # Homebridge + homebridge-nest startup takes ~42-60s
VERIFY_POLL_INTERVAL_SECONDS = 5.0

FATAL_LOG_SIGNATURES = [
    "Access token acquisition via googleAuth failed",
    "Auth failed: access token specified in Homebridge configuration rejected",
]


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_heal_ts": 0.0,
        "consecutive_failures": 0,
        "last_heal_reason": None,
        "last_status": "healthy",
        "history": [],
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def get_ha_entity_state(base_url: str, token: str, entity_id: str = LOCK_ENTITY_ID) -> dict | None:
    url = f"{base_url}/api/states/{entity_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[NestWatchdog] Error fetching entity state for {entity_id}: {e}")
        return None


def reload_ha_config_entry(base_url: str, token: str, entry_id: str = HOMEKIT_CONFIG_ENTRY_ID) -> bool:
    url = f"{base_url}/api/services/homeassistant/reload_config_entry"
    payload = json.dumps({"entry_id": entry_id}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[NestWatchdog] Error reloading HA config entry {entry_id}: {e}")
        return False


def call_ha_lock_unlock(base_url: str, token: str, entity_id: str = LOCK_ENTITY_ID) -> bool:
    url = f"{base_url}/api/services/lock/unlock"
    payload = json.dumps({"entity_id": entity_id}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[NestWatchdog] Error dispatching lock.unlock for {entity_id}: {e}")
        return False


def create_ha_persistent_notification(base_url: str, token: str, message: str, title: str):
    url = f"{base_url}/api/services/persistent_notification/create"
    payload = json.dumps({"message": message, "title": title, "notification_id": "nest_lock_self_heal"}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"[NestWatchdog] Error creating persistent notification: {e}")


def check_log_errors(logs_text: str) -> str | None:
    """Scan Homebridge logs for active fatal errors, ignoring lines superseded by initialization."""
    lines = logs_text.splitlines()
    last_init_idx = -1
    last_error_idx = -1
    matched_err = None

    for idx, line in enumerate(lines):
        if "Initializing Nest platform" in line or 'initing lock (P) "Entryway Lock"' in line:
            last_init_idx = idx
        for sig in FATAL_LOG_SIGNATURES:
            if sig in line:
                last_error_idx = idx
                matched_err = sig
                break

    # Only treat as active failure if the error occurred after the last successful device initialization
    if last_error_idx != -1 and last_error_idx > last_init_idx:
        return matched_err
    return None


def audit_lock_health(
    base_url: str | None = None,
    token: str | None = None,
    host1_ip: str = HOST_1_IP,
) -> tuple[bool, str, dict]:
    """Perform non-destructive health audit of entryway lock and bridge."""
    if not base_url or not token:
        base_url, token = get_ha_config()

    details = {
        "entity_id": LOCK_ENTITY_ID,
        "state": None,
        "transition_seconds": 0.0,
        "log_error": None,
        "container_status": None,
    }

    # 1. Inspect HA Lock State
    entity_data = get_ha_entity_state(base_url, token, LOCK_ENTITY_ID)
    if not entity_data:
        return False, "Failed to query Home Assistant lock entity", details

    state_val = entity_data.get("state")
    details["state"] = state_val
    last_changed_str = entity_data.get("last_changed")

    if state_val in ("unavailable", "unknown"):
        return False, f"Lock entity is currently {state_val}", details

    if state_val in ("unlocking", "locking") and last_changed_str:
        try:
            # Parse ISO timestamp
            dt_changed = datetime.fromisoformat(last_changed_str.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - dt_changed).total_seconds()
            details["transition_seconds"] = elapsed
            if elapsed > TRANSITION_TIMEOUT_SECONDS:
                return False, f"Lock stuck in {state_val} for {elapsed:.1f}s (threshold: {TRANSITION_TIMEOUT_SECONDS}s)", details
        except Exception as e:
            print(f"[NestWatchdog] Timestamp parse warning: {e}")

    # 2. Inspect Homebridge Logs on Host 1
    if run_ssh:
        code, out, _ = run_ssh(host1_ip, "sudo docker inspect homebridge --format '{{.State.Status}}'")
        if code == 0:
            details["container_status"] = out.strip()
            if details["container_status"] != "running":
                return False, f"Homebridge container is {details['container_status']} (not running)", details
        else:
            return False, "Failed to reach Host 1 via SSH to inspect Homebridge container", details

        # Check recent logs with --tail to avoid Synology driver hangs and --since to ignore stale historical errors
        code, log_out, _ = run_ssh(host1_ip, "sudo docker logs --tail 40 --since 10m homebridge")
        if code == 0:
            log_err = check_log_errors(log_out)
            if log_err:
                details["log_error"] = log_err
                return False, f"Homebridge log error: {log_err}", details

    return True, f"Nominal: {LOCK_ENTITY_ID} is {state_val}", details


def wait_for_lock_recovery(
    base_url: str,
    token: str,
    entity_id: str = LOCK_ENTITY_ID,
    entry_id: str = HOMEKIT_CONFIG_ENTRY_ID,
    timeout_seconds: float = VERIFY_TIMEOUT_SECONDS,
    poll_interval: float = VERIFY_POLL_INTERVAL_SECONDS,
) -> str:
    """Reload HomeKit Controller entry and poll until lock entity leaves unavailable/unknown/transition state."""
    reload_ok = reload_ha_config_entry(base_url, token, entry_id)
    if not reload_ok:
        print("[NestWatchdog] Warning: reload_config_entry returned non-200, checking state anyway")

    time.sleep(3.0)
    recovered_data = get_ha_entity_state(base_url, token, entity_id)
    new_state = recovered_data.get("state") if recovered_data else "unknown"
    if new_state not in ("unavailable", "unknown", "unlocking", "locking"):
        return new_state

    elapsed = 3.0
    poll_count = 0
    while elapsed < timeout_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval
        poll_count += 1
        if poll_count % 3 == 0:
            reload_ha_config_entry(base_url, token, entry_id)
        recovered_data = get_ha_entity_state(base_url, token, entity_id)
        new_state = recovered_data.get("state") if recovered_data else "unknown"
        if new_state not in ("unavailable", "unknown", "unlocking", "locking"):
            return new_state

    return new_state


def execute_self_heal(
    reason: str,
    base_url: str | None = None,
    token: str | None = None,
    host1_ip: str = HOST_1_IP,
    force: bool = False,
    reissue_unlock: bool = False,
) -> tuple[bool, str]:
    """Execute container restart, config reload, state verification, and notifications."""
    if not base_url or not token:
        base_url, token = get_ha_config()

    state = load_state()
    now_ts = time.time()
    last_heal = state.get("last_heal_ts", 0.0)
    consec = state.get("consecutive_failures", 0)

    if not force:
        if (now_ts - last_heal) < COOLDOWN_SECONDS:
            remaining = COOLDOWN_SECONDS - (now_ts - last_heal)
            return False, f"Cooldown active: {remaining:.0f}s remaining before next heal allowed"
        if consec >= MAX_CONSECUTIVE_FAILURES:
            return False, f"Circuit breaker tripped: {consec} consecutive heal attempts failed"

    print(f"[NestWatchdog] Initiating self-heal. Reason: {reason}")

    # 1. Restart Homebridge container on Host 1
    if not run_ssh:
        return False, "SSH execution tool run_ssh unavailable"

    code, out, err = run_ssh(
        host1_ip,
        f"docker compose -f {nas_config.DOCKER_ROOT}/appdata/docker-compose.yml restart homebridge || sudo docker restart homebridge",
    )
    if code != 0:
        state["consecutive_failures"] = consec + 1
        state["last_status"] = "error"
        save_state(state)
        return False, f"Failed to restart homebridge container: {err.strip() or out.strip()}"

    # 2. Wait initial 7s, then reload HomeKit Controller and poll readiness up to 65s
    time.sleep(7.0)
    new_state = wait_for_lock_recovery(base_url, token, LOCK_ENTITY_ID, HOMEKIT_CONFIG_ENTRY_ID)

    if new_state in ("unavailable", "unknown", "unlocking", "locking"):
        state["consecutive_failures"] = consec + 1
        state["last_status"] = "error"
        save_state(state)
        return False, f"Self-heal verification failed: entity remains in {new_state}"

    # 5. If interrupted during unlock, re-issue unlock command
    unlock_reissued = False
    if reissue_unlock:
        print("[NestWatchdog] Re-issuing interrupted unlock command...")
        if call_ha_lock_unlock(base_url, token, LOCK_ENTITY_ID):
            unlock_reissued = True
            time.sleep(3.0)
            rec_unlock = get_ha_entity_state(base_url, token, LOCK_ENTITY_ID)
            new_state = rec_unlock.get("state") if rec_unlock else new_state

    # 6. Record successful heal
    state["last_heal_ts"] = now_ts
    state["consecutive_failures"] = 0
    state["last_heal_reason"] = reason
    state["last_status"] = "healed"
    state.setdefault("history", []).append({
        "timestamp_pt": datetime.now(PT).strftime("%Y-%m-%d %I:%M:%S %p PT"),
        "reason": reason,
        "restored_state": new_state,
        "unlock_reissued": unlock_reissued,
    })
    state["history"] = state["history"][-20:]  # Keep last 20 events
    save_state(state)

    # 7. Deliver Notifications
    now_str = datetime.now(PT).strftime("%I:%M %p PT")
    msg_lines = [
        "🛠️ **[Self-Heal] Entryway Lock & Homebridge Restored**",
        f"- **Trigger:** {reason}",
        "- **Remediation:** Bounced `homebridge` container and reloaded HomeKit Controller config entry.",
        f"- **Status at {now_str}:** `{LOCK_ENTITY_ID}` is healthy (`{new_state}`).",
    ]
    if unlock_reissued:
        msg_lines.append("- **Arrival Action:** Re-dispatched `lock.unlock` successfully.")

    alert_content = "\n".join(msg_lines)

    if queue_outbox_message and force:
        queue_outbox_message(
            channel=str(HOME_ASSISTANT_CHANNEL_ID),
            content=alert_content,
            source_turn="nest_lock_watchdog",
            force=True,
        )
        if unlock_reissued:
            queue_outbox_message(
                channel=str(ZERO_CHAT_CHANNEL_ID),
                content=alert_content,
                source_turn="nest_lock_watchdog",
                force=True,
            )

    create_ha_persistent_notification(
        base_url,
        token,
        message=f"Homebridge and {LOCK_ENTITY_ID} automatically restored ({reason}). Live state: {new_state}.",
        title="Nest Lock Self-Heal",
    )

    return True, f"Successfully restored {LOCK_ENTITY_ID} to '{new_state}'"


def run_watchdog(force_heal: bool = False) -> tuple[bool, str, dict]:
    """Top-level entrypoint suitable for sidecar scheduler and CLI runs."""
    base_url, token = get_ha_config()
    is_healthy, reason, details = audit_lock_health(base_url, token)

    if is_healthy and not force_heal:
        state = load_state()
        if state.get("consecutive_failures", 0) > 0 or state.get("last_status") != "healthy":
            state["consecutive_failures"] = 0
            state["last_status"] = "healthy"
            save_state(state)
        return True, reason, details

    trigger_reason = "Forced manual remediation" if force_heal else reason
    reissue = details.get("state") == "unlocking"

    heal_ok, heal_msg = execute_self_heal(
        reason=trigger_reason,
        base_url=base_url,
        token=token,
        force=force_heal,
        reissue_unlock=reissue,
    )
    details["heal_ok"] = heal_ok
    details["heal_msg"] = heal_msg

    if heal_ok:
        return True, f"Self-healed: {heal_msg}", details
    return False, f"Heal failed: {heal_msg}", details


def main():
    parser = argparse.ArgumentParser(description="Nest Lock & Homebridge Self-Healing Watchdog")
    parser.add_argument("--check", action="store_true", help="Audit health only without executing remediation")
    parser.add_argument("--heal", action="store_true", help="Force self-healing execution regardless of current state")
    args = parser.parse_args()

    if args.check:
        base_url, token = get_ha_config()
        healthy, reason, details = audit_lock_health(base_url, token)
        print(f"Health: {'HEALTHY' if healthy else 'DEGRADED'}")
        print(f"Reason: {reason}")
        print(f"Details: {json.dumps(details, indent=2)}")
        sys.exit(0 if healthy else 1)

    healthy, msg, details = run_watchdog(force_heal=args.heal)
    print(f"Result: {msg}")
    print(f"Details: {json.dumps(details, indent=2)}")
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
