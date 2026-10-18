import sqlite3
import pytest
from tools.bridge_telemetry import (
    init_telemetry_db,
    record_token_usage,
    get_token_summary,
    resolve_channel_info,
    DB_PATH,
)


class MockThread:
    def __init__(self, t_id=999111, t_name="task-test-thread", p_id=1542081375287640084, p_name="zero-chat"):
        self.id = t_id
        self.name = t_name
        self.parent_id = p_id
        self.parent = type("MockParent", (), {"id": p_id, "name": p_name})()


def test_resolve_channel_info_root():
    info = resolve_channel_info(1542081375287640084)
    assert info["is_thread"] == 0
    assert info["parent_channel_name"] == "zero-chat"
    assert info["thread_id"] is None


def test_resolve_channel_info_thread():
    mock_thread = MockThread()
    info = resolve_channel_info(1542081375287640084, thread=mock_thread, escalated_to_thread=True)
    assert info["is_thread"] == 1
    assert info["thread_id"] == 999111
    assert info["channel_name"] == "task-test-thread"
    assert info["parent_channel_name"] == "zero-chat"


def test_record_and_summary(tmp_path, monkeypatch):
    test_db = tmp_path / "test_telemetry.db"
    monkeypatch.setattr("tools.bridge_telemetry.DB_PATH", test_db)

    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 50,
        "thinking_tokens": 10,
        "total_tokens": 180,
    }

    assert record_token_usage(1542081375287640084, usage, conv_id="test-conv")
    summary = get_token_summary(hours=1.0)
    assert len(summary) == 1
    assert summary[0]["parent_channel_name"] == "zero-chat"
    assert summary[0]["total_input"] == 100
    assert summary[0]["grand_total"] == 180
