
import pytest
from unittest.mock import patch
from tools.token_budget_watchdog import (
    evaluate_daily_token_budget,
    dispatch_token_budget_alert,
    DAILY_BUDGET_CEILING,
    WEEKLY_TOKEN_CEILING,
)

def test_budget_constants():
    assert DAILY_BUDGET_CEILING == 458_365_162
    assert WEEKLY_TOKEN_CEILING == 3_208_556_134

@patch("tools.token_budget_watchdog.get_evaluation_window")
@patch("tools.token_budget_watchdog.query_zero_tokens")
@patch("tools.token_budget_watchdog.query_ivy_tokens")
@patch("tools.token_budget_watchdog.query_rolling_7d_total")
def test_evaluate_nominal(mock_7d, mock_ivy, mock_zero, mock_window):
    mock_window.return_value = (1000.0, 2000.0, "Test Day")
    mock_zero.return_value = (50_000_000, 50, [])
    mock_ivy.return_value = (10_000_000, 10)
    mock_7d.return_value = 500_000_000

    report = evaluate_daily_token_budget("today")
    assert report["fleet_wire_tokens"] == 60_000_000
    assert report["is_exceeded"] is False
    assert report["is_warning"] is False
    assert report["alert_text"] == ""

@patch("tools.token_budget_watchdog.get_evaluation_window")
@patch("tools.token_budget_watchdog.query_zero_tokens")
@patch("tools.token_budget_watchdog.query_ivy_tokens")
@patch("tools.token_budget_watchdog.query_rolling_7d_total")
def test_evaluate_warning_85_percent(mock_7d, mock_ivy, mock_zero, mock_window):
    mock_window.return_value = (1000.0, 2000.0, "Test Day")
    # 85% of 458,365,162 is ~389.6M
    mock_zero.return_value = (390_000_000, 200, [{"channel": "zero-chat", "wire_tokens": 390_000_000, "turns": 200}])
    mock_ivy.return_value = (5_000_000, 10)
    mock_7d.return_value = 1_000_000_000

    report = evaluate_daily_token_budget("today")
    assert report["is_exceeded"] is False
    assert report["is_warning"] is True
    assert "🟡 **Daily Token Budget Approaching Ceiling" in report["alert_text"]

@patch("tools.token_budget_watchdog.get_evaluation_window")
@patch("tools.token_budget_watchdog.query_zero_tokens")
@patch("tools.token_budget_watchdog.query_ivy_tokens")
@patch("tools.token_budget_watchdog.query_rolling_7d_total")
def test_evaluate_exceeded(mock_7d, mock_ivy, mock_zero, mock_window):
    mock_window.return_value = (1000.0, 2000.0, "Test Day")
    mock_zero.return_value = (460_000_000, 300, [{"channel": "zero-chat", "wire_tokens": 460_000_000, "turns": 300}])
    mock_ivy.return_value = (10_000_000, 20)
    mock_7d.return_value = 2_000_000_000

    report = evaluate_daily_token_budget("today")
    assert report["is_exceeded"] is True
    assert report["is_warning"] is False
    assert "⚠️ **Daily Token Budget Exceeded" in report["alert_text"]

@patch("tools.token_budget_watchdog.queue_outbox_message")
@patch("tools.token_budget_watchdog.flush_pending_messages")
def test_dispatch_suppression_and_warning(mock_flush, mock_queue):
    mock_queue.return_value = True
    
    # Nominal report -> suppressed
    report_nom = {"is_exceeded": False, "is_warning": False, "fleet_wire_tokens": 50_000_000, "pct_daily": 10.9}
    assert dispatch_token_budget_alert(report_nom) is False

    # Warning report -> dispatched
    report_warn = {"is_exceeded": False, "is_warning": True, "alert_text": "🟡 Warning", "fleet_wire_tokens": 400_000_000}
    assert dispatch_token_budget_alert(report_warn) is True
