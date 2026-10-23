import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock
import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from nest_lock_watchdog import (
    check_log_errors,
    audit_lock_health,
    execute_self_heal,
    load_state,
    save_state,
    TRANSITION_TIMEOUT_SECONDS,
    COOLDOWN_SECONDS,
)


def test_check_log_errors_clean():
    clean_logs = """
[10/5/2026, 8:37:54 AM] Loaded plugin: homebridge-nest@4.6.10
[10/5/2026, 8:37:54 AM] [Nest] Initializing Nest platform...
[10/5/2026, 8:38:00 AM] [Nest] initing lock (P) "Entryway Lock": deviceId: 00177A00000EA8C7
"""
    assert check_log_errors(clean_logs) is None


def test_check_log_errors_stale_ignored():
    stale_error_logs = """
[10/3/2026, 3:20:49 PM] [Nest] Access token acquisition via googleAuth failed (code ETIMEDOUT).
[10/3/2026, 3:20:49 PM] [Nest] Auth failed: access token specified in Homebridge configuration rejected
[10/5/2026, 8:37:54 AM] [Nest] Initializing Nest platform...
[10/5/2026, 8:38:00 AM] [Nest] initing lock (P) "Entryway Lock": deviceId: 00177A00000EA8C7
"""
    # The error is before the latest platform init, so it should be ignored
    assert check_log_errors(stale_error_logs) is None


def test_check_log_errors_active_detected():
    active_error_logs = """
[10/5/2026, 8:37:54 AM] [Nest] Initializing Nest platform...
[10/5/2026, 8:38:00 AM] [Nest] initing lock (P) "Entryway Lock": deviceId: 00177A00000EA8C7
[10/5/2026, 9:15:00 AM] [Nest] Access token acquisition via googleAuth failed (code ETIMEDOUT).
[10/5/2026, 9:15:00 AM] [Nest] Auth failed: access token specified in Homebridge configuration rejected
"""
    # The error is after the latest platform init
    err = check_log_errors(active_error_logs)
    assert err is not None
    assert "googleAuth failed" in err or "Auth failed" in err


@patch("nest_lock_watchdog.get_ha_entity_state")
@patch("nest_lock_watchdog.run_ssh")
def test_audit_lock_health_nominal(mock_ssh, mock_get_ha):
    mock_get_ha.return_value = {
        "state": "locked",
        "last_changed": datetime.now(timezone.utc).isoformat(),
    }
    mock_ssh.side_effect = [
        (0, "running\n", ""),  # inspect status
        (0, "[Nest] Initializing Nest platform...\n[Nest] initing lock\n", ""),  # logs
    ]

    healthy, reason, details = audit_lock_health("http://fake-ha", "fake-token")
    assert healthy is True
    assert "Nominal" in reason
    assert details["state"] == "locked"


@patch("nest_lock_watchdog.get_ha_entity_state")
def test_audit_lock_health_unavailable(mock_get_ha):
    mock_get_ha.return_value = {"state": "unavailable"}

    healthy, reason, details = audit_lock_health("http://fake-ha", "fake-token")
    assert healthy is False
    assert "unavailable" in reason


@patch("nest_lock_watchdog.get_ha_entity_state")
def test_audit_lock_health_stuck_unlocking(mock_get_ha):
    old_time = datetime.now(timezone.utc) - timedelta(seconds=TRANSITION_TIMEOUT_SECONDS + 10)
    mock_get_ha.return_value = {
        "state": "unlocking",
        "last_changed": old_time.isoformat(),
    }

    healthy, reason, details = audit_lock_health("http://fake-ha", "fake-token")
    assert healthy is False
    assert "stuck in unlocking" in reason
    assert details["transition_seconds"] >= TRANSITION_TIMEOUT_SECONDS


@patch("nest_lock_watchdog.get_ha_entity_state")
@patch("nest_lock_watchdog.run_ssh")
def test_audit_lock_health_fresh_unlocking_nominal(mock_ssh, mock_get_ha):
    # Lock just started unlocking 3 seconds ago (normal operation)
    fresh_time = datetime.now(timezone.utc) - timedelta(seconds=3)
    mock_get_ha.return_value = {
        "state": "unlocking",
        "last_changed": fresh_time.isoformat(),
    }
    mock_ssh.side_effect = [
        (0, "running\n", ""),
        (0, "[Nest] Initializing Nest platform...\n[Nest] initing lock\n", ""),
    ]

    healthy, reason, details = audit_lock_health("http://fake-ha", "fake-token")
    assert healthy is True
    assert details["transition_seconds"] < TRANSITION_TIMEOUT_SECONDS


@patch("nest_lock_watchdog.load_state")
def test_execute_self_heal_cooldown(mock_load):
    import time
    mock_load.return_value = {
        "last_heal_ts": time.time() - 30,  # 30 seconds ago (< 180s)
        "consecutive_failures": 0,
    }

    ok, msg = execute_self_heal("test", "http://fake-ha", "fake-token", force=False)
    assert ok is False
    assert "Cooldown active" in msg


@patch("nest_lock_watchdog.load_state")
def test_execute_self_heal_circuit_breaker(mock_load):
    mock_load.return_value = {
        "last_heal_ts": 0,
        "consecutive_failures": 3,
    }

    ok, msg = execute_self_heal("test", "http://fake-ha", "fake-token", force=False)
    assert ok is False
    assert "Circuit breaker tripped" in msg


@patch("nest_lock_watchdog.load_state")
@patch("nest_lock_watchdog.save_state")
@patch("nest_lock_watchdog.run_ssh")
@patch("nest_lock_watchdog.reload_ha_config_entry")
@patch("nest_lock_watchdog.get_ha_entity_state")
@patch("nest_lock_watchdog.time.sleep")
@patch("nest_lock_watchdog.queue_outbox_message")
@patch("nest_lock_watchdog.create_ha_persistent_notification")
def test_execute_self_heal_success(
    mock_notif, mock_outbox, mock_sleep, mock_get_ha, mock_reload, mock_ssh, mock_save, mock_load
):
    mock_load.return_value = {"last_heal_ts": 0, "consecutive_failures": 0, "history": []}
    mock_ssh.return_value = (0, "Restarting homebridge\n", "")
    mock_reload.return_value = True
    mock_get_ha.return_value = {"state": "locked"}

    ok, msg = execute_self_heal("stuck in unlocking", "http://fake-ha", "fake-token", force=True)
    assert ok is True
    assert "Successfully restored" in msg
    mock_ssh.assert_called_once()
    mock_reload.assert_called_once()
    mock_save.assert_called_once()


@patch("nest_lock_watchdog.load_state")
@patch("nest_lock_watchdog.save_state")
@patch("nest_lock_watchdog.run_ssh")
@patch("nest_lock_watchdog.reload_ha_config_entry")
@patch("nest_lock_watchdog.get_ha_entity_state")
@patch("nest_lock_watchdog.call_ha_lock_unlock")
@patch("nest_lock_watchdog.time.sleep")
@patch("nest_lock_watchdog.queue_outbox_message")
@patch("nest_lock_watchdog.create_ha_persistent_notification")
def test_execute_self_heal_reissue_unlock(
    mock_notif, mock_outbox, mock_sleep, mock_unlock, mock_get_ha, mock_reload, mock_ssh, mock_save, mock_load
):
    mock_load.return_value = {"last_heal_ts": 0, "consecutive_failures": 0, "history": []}
    mock_ssh.return_value = (0, "Restarting homebridge\n", "")
    mock_reload.return_value = True
    # First returns locked, then after unlock returns unlocked
    mock_get_ha.side_effect = [{"state": "locked"}, {"state": "unlocked"}]
    mock_unlock.return_value = True

    ok, msg = execute_self_heal(
        "stuck in unlocking",
        "http://fake-ha",
        "fake-token",
        force=True,
        reissue_unlock=True,
    )
    assert ok is True
    assert "unlocked" in msg
    mock_unlock.assert_called_once()



@patch("nest_lock_watchdog.get_ha_entity_state")
@patch("nest_lock_watchdog.run_ssh")
@patch("nest_lock_watchdog.load_state")
@patch("nest_lock_watchdog.save_state")
def test_run_watchdog_resets_failures_when_healthy(mock_save, mock_load, mock_ssh, mock_get_ha):
    from nest_lock_watchdog import run_watchdog
    mock_get_ha.return_value = {
        "state": "locked",
        "last_changed": datetime.now(timezone.utc).isoformat(),
    }
    mock_ssh.side_effect = [
        (0, "running", ""),
        (0, "", ""),
    ]
    mock_load.return_value = {
        "last_heal_ts": 0,
        "consecutive_failures": 3,
        "last_status": "error",
    }

    healthy, reason, details = run_watchdog(force_heal=False)
    assert healthy is True
    assert "Nominal" in reason
    mock_save.assert_called_once_with({
        "last_heal_ts": 0,
        "consecutive_failures": 0,
        "last_status": "healthy",
    })


@patch("nest_lock_watchdog.load_state")
@patch("nest_lock_watchdog.save_state")
@patch("nest_lock_watchdog.run_ssh")
@patch("nest_lock_watchdog.reload_ha_config_entry")
@patch("nest_lock_watchdog.get_ha_entity_state")
@patch("nest_lock_watchdog.time.sleep")
@patch("nest_lock_watchdog.queue_outbox_message")
@patch("nest_lock_watchdog.create_ha_persistent_notification")
def test_execute_self_heal_delayed_readiness_polling(
    mock_notif, mock_outbox, mock_sleep, mock_get_ha, mock_reload, mock_ssh, mock_save, mock_load
):
    mock_load.return_value = {"last_heal_ts": 0, "consecutive_failures": 0, "history": []}
    mock_ssh.return_value = (0, "Restarting homebridge\n", "")
    mock_reload.return_value = True
    # Simulate Homebridge taking ~25s to boot: 3 unavailable polls, then locked on the 4th poll
    mock_get_ha.side_effect = [
        {"state": "unavailable"},
        {"state": "unavailable"},
        {"state": "unavailable"},
        {"state": "locked"},
    ]

    ok, msg = execute_self_heal("Lock entity is currently unavailable", "http://fake-ha", "fake-token", force=True)
    assert ok is True
    assert "Successfully restored" in msg
    assert mock_get_ha.call_count == 4
    # Initial reload + periodic retry on 3rd poll
    assert mock_reload.call_count == 2

