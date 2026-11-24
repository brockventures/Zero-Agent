#!/usr/bin/env python3
"""
bridge_watchdog.py — Self-Healing Bridge & Gateway Liveness Watchdog for Zero.

Runs periodically (every 5m) or on-demand to monitor:
1. Discord Gateway Heartbeat freshness in liveness_beacon.json (threshold: 180s).
2. Stuck in-flight turns (state: PROCESSING > 600s with dead/wedged process tree).
3. Stale or orphaned agy CLI processes holding slots (> 600s).
4. Automated Self-Healing: Automatically touches /workspace/data/reload_bridge.flag
   to execute an in-place reload if a wedged state is detected.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from tools.process_probe import reap_stale_agy_processes, is_process_making_progress
from tools.bridge_state import DATA_DIR, BEACON_FILE, get_all_active_pids, is_vulcan_agent

PT_TZ = ZoneInfo("America/Los_Angeles")
LOG_FILE = DATA_DIR / "bridge_watchdog.log"
RELOAD_FLAG = DATA_DIR / "reload_bridge.flag"
MAX_HEARTBEAT_AGE = 180.0  # 3 minutes
MAX_PROCESSING_SILENCE = 600.0  # 10 minutes


def log(msg: str):
    ts = datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def check_brief_daemon_supervision(
    auto_heal: bool = True,
    *,
    config_path: Path | str = Path("/workspace/memory/private/brief/config.toml"),
    state_dir: Path | str = Path("/workspace/data/brief"),
    memory_dir: Path | str = Path("/workspace/memory/private/brief"),
) -> dict:
    """Supervise the BRIEF daemon externally without triggering a Zero bridge reload."""
    if is_vulcan_agent():
        return {"checked": False, "status": "skipped_vulcan"}

    resolved_config_path = Path(config_path)
    if not resolved_config_path.exists():
        return {"checked": False, "status": "unconfigured"}

    try:
        from tools.brief.config import BriefConfig, is_kill_switch_active
        from tools.brief.daemon import ensure_brief_daemon_running, get_daemon_pid
        from tools.brief.queue import BriefQueue
        from tools.brief.watchdog import run_watchdog

        cfg = BriefConfig.load(
            config_path=resolved_config_path,
            state_dir=state_dir,
            memory_dir=memory_dir,
        )
    except Exception as cfg_err:
        log(f"⚠️ BRIEF supervision config load failed: {cfg_err}")
        return {
            "checked": True,
            "status": "error",
            "restarted": False,
            "error": str(cfg_err),
        }

    if is_kill_switch_active(cfg.kill_switch_path):
        return {
            "checked": True,
            "status": "paused_kill_switch",
            "restarted": False,
        }

    pid = get_daemon_pid(cfg)
    restarted = False
    status = "running" if pid is not None else "stopped"
    if pid is None:
        if auto_heal:
            try:
                started = ensure_brief_daemon_running(cfg)
                pid = get_daemon_pid(cfg)
                if started:
                    restarted = True
                    status = "restarted"
                    log(f"🔄 BRIEF daemon was not running; auto-restarted cleanly (PID: {pid}).")
                else:
                    status = "restart_failed"
                    log("⚠️ BRIEF daemon was not running and auto-restart returned False.")
            except Exception as start_err:
                status = "restart_failed"
                log(f"⚠️ BRIEF daemon auto-restart failed: {start_err}")
        else:
            status = "stopped"

    watchdog_result = None
    watchdog_error = None
    try:
        queue = BriefQueue(
            cfg.db_path,
            allowlisted_senders=cfg.allowlisted_emails,
        )
        watchdog_result = run_watchdog(cfg, queue, dry_run=not auto_heal)
    except Exception as wd_err:
        watchdog_error = str(wd_err)
        log(f"⚠️ BRIEF external watchdog check failed: {wd_err}")

    result = {
        "checked": True,
        "status": status,
        "pid": pid,
        "restarted": restarted,
    }
    if watchdog_result is not None:
        result["watchdog"] = watchdog_result
    if watchdog_error is not None:
        result["watchdog_error"] = watchdog_error
    return result


_SUBDAEMON_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("mcp_daemon", "tools.mcp_daemon", "get_status", "ensure_mcp_daemon_running"),
    ("zero_mail_listener", "tools.zero_mail_listener", "get_status", "ensure_mail_listener_running"),
    ("amazon_order_listener", "tools.amazon_order_listener", "get_status", "ensure_amazon_listener_running"),
    ("zero_health_server", "tools.zero_health_server", "is_running", "ensure_health_server_running"),
    ("banana_watcher", "tools.banana_watcher", "get_daemon_status", "ensure_banana_watcher_running"),
)


def _parse_subdaemon_status(raw_status) -> tuple[bool, int | None]:
    """Normalize dict or PID/bool daemon status into (is_alive, pid)."""
    if isinstance(raw_status, dict):
        if "running" in raw_status:
            alive = bool(raw_status.get("running"))
        else:
            alive = bool(raw_status.get("pid"))
        pid = raw_status.get("pid")
        return alive, pid if isinstance(pid, int) and not isinstance(pid, bool) else None
    alive = bool(raw_status)
    pid = raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
    return alive, pid


def check_subdaemons_supervision(auto_heal: bool = True) -> dict:
    """Supervise Zero's 5 internal sub-daemons independently without triggering RELOAD_FLAG."""
    if is_vulcan_agent():
        return {
            "checked": False,
            "status": "skipped_vulcan",
            "daemons": {},
            "restarted": [],
        }

    daemons: dict[str, dict] = {}
    restarted: list[str] = []

    for name, module_path, status_attr, ensure_attr in _SUBDAEMON_SPECS:
        try:
            mod = importlib.import_module(module_path)
            status_fn = getattr(mod, status_attr)
            ensure_fn = getattr(mod, ensure_attr)

            alive, pid = _parse_subdaemon_status(status_fn())
            if alive:
                daemons[name] = {
                    "status": "running",
                    "pid": pid,
                    "restarted": False,
                }
                continue

            if not auto_heal:
                daemons[name] = {
                    "status": "stopped",
                    "pid": pid,
                    "restarted": False,
                }
                continue

            ensure_res = ensure_fn()
            post_alive, post_pid = _parse_subdaemon_status(status_fn())
            if ensure_res is not False and post_alive:
                restarted.append(name)
                daemons[name] = {
                    "status": "restarted",
                    "pid": post_pid,
                    "restarted": True,
                }
                pid_suffix = f" (PID {post_pid})" if post_pid else ""
                log(f"🔄 Sub-daemon '{name}' was not running; auto-restarted cleanly{pid_suffix}.")
            else:
                daemons[name] = {
                    "status": "restart_failed",
                    "pid": post_pid,
                    "restarted": False,
                }
                log(f"⚠️ Sub-daemon '{name}' was not running and auto-restart failed.")
        except Exception as err:
            daemons[name] = {
                "status": "error",
                "restarted": False,
                "error": str(err),
            }
            log(f"⚠️ Sub-daemon '{name}' supervision check failed: {err}")

    has_unhealthy = any(
        d.get("status") in ("stopped", "restart_failed", "error")
        for d in daemons.values()
    )
    if has_unhealthy:
        overall_status = "degraded"
    elif restarted:
        overall_status = "restarted"
    else:
        overall_status = "running"

    return {
        "checked": True,
        "status": overall_status,
        "daemons": daemons,
        "restarted": restarted,
    }


def check_bridge_health(auto_heal: bool = True) -> tuple[bool, str, dict]:
    """Inspect bridge gateway, turn state, and background agy subprocesses."""
    now = time.time()
    issues = []
    actions_taken = []
    details = {
        "timestamp": now,
        "timestamp_pt": datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M:%S %p PT"),
        "gateway_healthy": True,
        "beacon_healthy": True,
        "reaped_processes": [],
        "self_healed": False
    }

    # 1. Inspect dynamic Discord Gateway Heartbeat in liveness_beacon.json
    beacon_data = {}
    if BEACON_FILE.exists():
        try:
            with open(BEACON_FILE) as f:
                beacon_data = json.load(f)
        except Exception as e:
            issues.append(f"Corrupt liveness_beacon.json: {e}")

    gw_heartbeat = beacon_data.get("gateway_heartbeat")
    gw_status = beacon_data.get("gateway_status", "unknown")
    if gw_heartbeat:
        gw_age = now - float(gw_heartbeat)
        details["gateway_heartbeat_age_seconds"] = int(gw_age)
        details["gateway_status"] = gw_status
        if gw_age > MAX_HEARTBEAT_AGE:
            issues.append(f"Discord gateway heartbeat stale ({int(gw_age)}s > {int(MAX_HEARTBEAT_AGE)}s)")
            details["gateway_healthy"] = False
        elif gw_status != "connected":
            issues.append(f"Discord gateway reported state '{gw_status}'")
            details["gateway_healthy"] = False
    else:
        issues.append("Missing gateway_heartbeat in liveness_beacon.json")
        details["gateway_healthy"] = False

    # 2. Check for wedged PROCESSING turn state
    turn_state = beacon_data.get("state", "IDLE")
    turn_ts = beacon_data.get("ts", 0)
    if turn_state == "PROCESSING" and turn_ts > 0:
        turn_silence = now - float(turn_ts)
        details["turn_silence_seconds"] = int(turn_silence)
        if turn_silence > MAX_PROCESSING_SILENCE:
            # Verify if any active process is still running and making forward progress
            active_pids = get_all_active_pids()
            has_progress = False
            for apid in active_pids:
                if is_process_making_progress(apid):
                    has_progress = True
                    break
            if not has_progress:
                issues.append(f"Turn state held in PROCESSING for {int(turn_silence)}s (> {int(MAX_PROCESSING_SILENCE)}s) with zero progress")
                details["beacon_healthy"] = False

    # 3. Reap any stale / orphaned agy CLI processes
    try:
        allowed = get_all_active_pids()
        reaped = reap_stale_agy_processes(allowed_active_pids=allowed, dry_run=not auto_heal)
        if reaped:
            details["reaped_processes"] = reaped
            actions_taken.append(f"Reaped {len(reaped)} stale/wedged agy CLI subprocess(es)")
    except Exception as re_err:
        issues.append(f"Process reaper check failed: {re_err}")

    # 4. Supervise BRIEF daemon independently (never triggers RELOAD_FLAG)
    try:
        brief_sup = check_brief_daemon_supervision(auto_heal=auto_heal)
        details["brief_supervision"] = brief_sup
        if brief_sup.get("restarted"):
            brief_pid = brief_sup.get("pid")
            pid_suffix = f" (PID {brief_pid})" if brief_pid else ""
            actions_taken.append(f"Restarted BRIEF daemon independently{pid_suffix}")
    except Exception as brief_err:
        details["brief_supervision"] = {
            "checked": True,
            "status": "error",
            "restarted": False,
            "error": str(brief_err),
        }

    # 4b. Supervise Zero's 5 internal sub-daemons independently (never triggers RELOAD_FLAG)
    try:
        sub_sup = check_subdaemons_supervision(auto_heal=auto_heal)
        details["subdaemon_supervision"] = sub_sup
        sub_daemons = sub_sup.get("daemons", {})
        for daemon_name in sub_sup.get("restarted", []):
            daemon_pid = sub_daemons.get(daemon_name, {}).get("pid")
            pid_suffix = f" (PID {daemon_pid})" if daemon_pid else ""
            actions_taken.append(f"Restarted sub-daemon '{daemon_name}' independently{pid_suffix}")
    except Exception as sub_err:
        details["subdaemon_supervision"] = {
            "checked": True,
            "status": "error",
            "daemons": {},
            "restarted": [],
            "error": str(sub_err),
        }

    # 5. Self-Healing Trigger
    is_healthy = len(issues) == 0
    if not is_healthy and auto_heal:
        log(f"🚨 Bridge Watchdog detected {len(issues)} issue(s): {'; '.join(issues)}. Arming self-healing reload flag...")
        try:
            RELOAD_FLAG.touch()
            actions_taken.append("Armed /workspace/data/reload_bridge.flag for in-place reload")
            details["self_healed"] = True
        except Exception as fe:
            issues.append(f"Failed to touch reload flag: {fe}")

    summary_parts = []
    if is_healthy:
        summary_parts.append("✅ Discord bridge & gateway healthy.")
        if gw_heartbeat:
            summary_parts.append(f"Gateway latency: {beacon_data.get('gateway_latency_ms', 0)}ms (heartbeat age: {int(now - float(gw_heartbeat))}s).")
        if actions_taken:
            summary_parts.append("\n**Actions Taken:**\n" + "\n".join(f"• {act}" for act in actions_taken))
    else:
        summary_parts.append(f"⚠️ **Bridge Degradation Detected ({len(issues)} issues)**:\n" + "\n".join(f"• {iss}" for iss in issues))
        if actions_taken:
            summary_parts.append("\n**Actions Taken:**\n" + "\n".join(f"• {act}" for act in actions_taken))

    return is_healthy, "\n".join(summary_parts), details


def main():
    auto_heal = "--no-heal" not in sys.argv
    healthy, summary, details = check_bridge_health(auto_heal=auto_heal)
    print(summary)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
