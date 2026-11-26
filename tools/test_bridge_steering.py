# ruff: noqa: I001
"""
test_bridge_steering.py — Unit tests for atomic steering queue, abort detection,
runtime rule helpers, and TurnCoordinator turn-phase tracking.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
elif sys.path[0] != REPO_ROOT:
    sys.path.remove(REPO_ROOT)
    sys.path.insert(0, REPO_ROOT)

from tools.bridge_engine import (
    TurnCoordinator,
    channel_turn_phases,
    clear_channel_turn_phase,
    get_channel_turn_phase,
    set_channel_turn_phase,
)
from tools.bridge_state import TARGET_CHANNEL_ID
from tools.bridge_steering import (
    cancel_pending_steer,
    consume_pending_steers,
    enqueue_soft_steer,
    format_hook_steer_message,
    get_hook_drain_timeout_seconds,
    is_hard_abort_directive,
    is_hybrid_steering_enabled,
    is_steer_pending,
    is_steering_disabled,
    peek_pending_steers,
    purge_channel_steers,
)


def test_enqueue_peek_consume_and_channel_isolation(tmp_path: Path) -> None:
    ch1 = 1542081375287640084
    ch2 = 1544953275877556334

    e1 = enqueue_soft_steer(
        ch1,
        "Also check the nginx logs",
        author_name="Ryan",
        is_group=False,
        original_task="Check host health",
        turn_seq=1,
        queue_dir=tmp_path,
    )
    e2 = enqueue_soft_steer(
        ch1,
        "And verify SSL cert expiry",
        author_name="Ryan",
        turn_seq=2,
        queue_dir=tmp_path,
    )
    e_other = enqueue_soft_steer(ch2, "Separate thread directive", queue_dir=tmp_path)

    assert e1["steer_id"] != e2["steer_id"]
    assert e1["consumed_at"] is None
    assert is_steer_pending(ch1, e1["steer_id"], queue_dir=tmp_path) is True
    assert is_steer_pending(ch2, e_other["steer_id"], queue_dir=tmp_path) is True

    peeked = peek_pending_steers(ch1, queue_dir=tmp_path)
    assert [p["steer_id"] for p in peeked] == [e1["steer_id"], e2["steer_id"]]
    assert [p["steer_id"] for p in peek_pending_steers(ch2, queue_dir=tmp_path)] == [e_other["steer_id"]]

    consumed = consume_pending_steers(ch1, consumer="hook", queue_dir=tmp_path)
    assert [c["steer_id"] for c in consumed] == [e1["steer_id"], e2["steer_id"]]
    assert all(c["consumed_at"] is not None and c["consumer"] == "hook" for c in consumed)

    # Second consume returns empty list; ch2 remains unaffected
    assert consume_pending_steers(ch1, consumer="hook", queue_dir=tmp_path) == []
    assert peek_pending_steers(ch1, queue_dir=tmp_path) == []
    assert is_steer_pending(ch1, e1["steer_id"], queue_dir=tmp_path) is False
    assert len(peek_pending_steers(ch2, queue_dir=tmp_path)) == 1


def test_env_queue_dir_override_and_cancel_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERO_STEERING_QUEUE_DIR", str(tmp_path))
    ch = 9988776655

    s1 = enqueue_soft_steer(ch, "First follow-up")
    s2 = enqueue_soft_steer(ch, "Second follow-up")

    # Cancel s1 before hook consumes it -> True
    assert cancel_pending_steer(ch, s1["steer_id"]) is True
    assert is_steer_pending(ch, s1["steer_id"]) is False
    # Canceling already-removed steer -> False
    assert cancel_pending_steer(ch, s1["steer_id"]) is False

    # Hook consumes s2
    consumed = consume_pending_steers(ch, consumer="hook")
    assert [c["steer_id"] for c in consumed] == [s2["steer_id"]]

    # Attempting SIGINT fallback cancellation after hook already consumed s2 MUST return False
    assert cancel_pending_steer(ch, s2["steer_id"]) is False


def test_purge_and_corrupt_json_resilience(tmp_path: Path) -> None:
    ch = 11223344
    # Purging non-existent channel returns 0 without creating files
    assert purge_channel_steers(ch, queue_dir=tmp_path) == 0
    assert list(tmp_path.iterdir()) == []

    enqueue_soft_steer(ch, "Directive 1", queue_dir=tmp_path)
    enqueue_soft_steer(ch, "Directive 2", queue_dir=tmp_path)
    assert (tmp_path / f"{ch}.json").exists()

    removed_count = purge_channel_steers(ch, queue_dir=tmp_path)
    assert removed_count == 2
    assert not (tmp_path / f"{ch}.json").exists()
    assert not (tmp_path / f"{ch}.lock").exists()

    # Corrupt JSON file fails safe
    (tmp_path / f"{ch}.json").write_text("{bad json", encoding="utf-8")
    assert peek_pending_steers(ch, queue_dir=tmp_path) == []
    assert consume_pending_steers(ch, queue_dir=tmp_path) == []


def test_concurrent_single_delivery_guarantee(tmp_path: Path) -> None:
    ch = 55667788
    total_steers = 40

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(enqueue_soft_steer, ch, f"directive-{i}", turn_seq=i, queue_dir=tmp_path)
            for i in range(total_steers)
        ]
        enqueued_ids = {f.result()["steer_id"] for f in futures}

    assert len(enqueued_ids) == total_steers

    # Race 12 concurrent consumers against the same channel queue
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(lambda idx: consume_pending_steers(ch, consumer=f"c-{idx}", queue_dir=tmp_path), range(12))
        )

    delivered_ids: list[str] = [entry["steer_id"] for batch in results for entry in batch]
    assert len(delivered_ids) == total_steers
    assert set(delivered_ids) == enqueued_ids
    assert peek_pending_steers(ch, queue_dir=tmp_path) == []


@pytest.mark.parametrize(
    "text",
    [
        "stop",
        "STOP!",
        "stop.",
        "cancel",
        "abort",
        "halt",
        "nevermind",
        "never mind",
        "forget it",
        "scratch that",
        "/stop",
        "/cancel",
        "/abort",
        "stop doing that",
        "cancel that",
        "wait stop",
        "wait, stop!",
        "please stop",
        "<@1234567890> stop",
        "<@!1234567890> cancel that",
    ],
)
def test_hard_abort_directive_positives(text: str) -> None:
    assert is_hard_abort_directive(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "Also check why the stop button in the UI isn't working",
        "Can you make sure we cancel the background timer when the user disconnects?",
        "Don't forget to abort the request if status is 500 and add unit tests for that path.",
        "Never mind the formatting for now, just make sure all the unit tests pass.",
        "Stop using mock_open in the test and switch to tmp_path instead.",
        "Wait, stop and check the logs first before making any changes.",
        "Please don't stop running the test suite until it finishes.",
    ],
)
def test_hard_abort_directive_negatives(text: str) -> None:
    assert is_hard_abort_directive(text) is False


def test_runtime_rules_helpers_and_formatting() -> None:
    rules_path = Path(REPO_ROOT) / "config" / "runtime_rules.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))

    # #the-banana-stand is disabled in config/runtime_rules.json
    assert is_steering_disabled(1534436119888793750, rules=rules) is True
    assert is_steering_disabled("1534436119888793750", rules=rules) is True
    assert is_steering_disabled(1542081375287640084, rules=rules) is False
    assert is_steering_disabled(None, rules=rules) is False

    assert is_hybrid_steering_enabled(rules=rules) is True
    assert is_hybrid_steering_enabled(rules={"hybrid_steering_enabled": False}) is False
    assert is_hybrid_steering_enabled(rules={}) is True

    assert get_hook_drain_timeout_seconds(rules=rules) == pytest.approx(2.5)
    assert get_hook_drain_timeout_seconds(rules={"hybrid_steering_hook_timeout_seconds": 4.0}) == pytest.approx(4.0)
    assert get_hook_drain_timeout_seconds(rules={"hybrid_steering_hook_timeout_seconds": "bad"}) == pytest.approx(2.5)
    assert get_hook_drain_timeout_seconds(rules={}) == pytest.approx(2.5)

    assert format_hook_steer_message([]) == ""
    msg = format_hook_steer_message(
        [
            {"directive": "Include disk usage too", "author_name": "Ryan", "original_task": "Check NAS CPU"},
            {"directive": "Format as markdown table", "author_name": "Amos"},
        ]
    )
    assert "[MID-TURN USER STEERING UPDATE]" in msg
    assert "Include disk usage too" in msg
    assert "Format as markdown table" in msg
    assert "Check NAS CPU" in msg


def test_turn_coordinator_phase_transitions_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERO_STEERING_QUEUE_DIR", str(tmp_path))
    ch_id = 4455667788
    clear_channel_turn_phase(ch_id)

    coord = TurnCoordinator(
        channel_id=ch_id,
        prompt="Run diagnostics",
        mode="home",
        reply_target=MagicMock(),
        status_msg=None,
        conv_id="conv-phase-1",
    )
    assert get_channel_turn_phase(ch_id) == "unknown"

    # 1. Active tool step -> tool_loop
    coord.process_stream_event(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "IN_PROGRESS",
                "tool_name": "run_command",
                "tool_info": {"parameters": {"CommandLine": "uptime"}},
            },
        }
    )
    assert get_channel_turn_phase(ch_id) == "tool_loop"
    assert channel_turn_phases[ch_id]["tool_name"] == "run_command"

    # 2. Internal leak delta must NOT switch to streaming_text
    coord.process_stream_event(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "state": "IN_PROGRESS",
                "text_delta": "<WAITING_FOR_TASKS_OUTPUT>wait</WAITING_FOR_TASKS_OUTPUT>",
            },
        }
    )
    assert get_channel_turn_phase(ch_id) == "tool_loop"

    # 3. Substantive non-leak text delta -> streaming_text
    coord.process_stream_event(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "agent_response",
                "state": "IN_PROGRESS",
                "text_delta": "Here are the diagnostics...",
            },
        }
    )
    assert get_channel_turn_phase(ch_id) == "streaming_text"

    # 4. Subsequent tool call -> back to tool_loop
    coord.process_stream_event(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "IN_PROGRESS",
                "tool_name": "view_file",
                "tool_info": {"parameters": {"AbsolutePath": "/tmp/log.txt"}},
            },
        }
    )
    assert get_channel_turn_phase(ch_id) == "tool_loop"

    # 5. Intermediate result while background task pending -> stays in tool_loop
    parser_bg = MagicMock()
    parser_bg.has_pending_background_task = True
    with patch("tools.bridge_engine.set_channel_session_id"):
        coord.process_stream_event(
            {"event": "result", "result": {"conversation_id": "conv-phase-1", "status": "SUCCESS"}},
            stream_parser=parser_bg,
        )
    assert get_channel_turn_phase(ch_id) == "tool_loop"

    # 6. Terminal result -> finalizing
    parser_done = MagicMock()
    parser_done.has_pending_background_task = False
    with patch("tools.bridge_engine.set_channel_session_id"), \
         patch("tools.task_settle.get_turn_pending_tasks", return_value=[]):
        coord.process_stream_event(
            {"event": "result", "result": {"conversation_id": "conv-phase-1", "status": "SUCCESS"}},
            stream_parser=parser_done,
        )
    assert get_channel_turn_phase(ch_id) == "finalizing"

    # 7. Cleanup clears phase and purges steers
    enqueue_soft_steer(ch_id, "Leftover steer", queue_dir=tmp_path)
    with patch("tools.bridge_engine.update_beacon"), patch("tools.bridge_engine.clear_in_flight"):
        coord.cleanup_process()
    assert get_channel_turn_phase(ch_id) == "unknown"
    assert ch_id not in channel_turn_phases
    assert peek_pending_steers(ch_id, queue_dir=tmp_path) == []


def test_thread_escalation_migrates_phase_and_cleanup_purges_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZERO_STEERING_QUEUE_DIR", str(tmp_path))
    thread_id = 777888999000

    mock_thread = MagicMock()
    mock_thread.id = thread_id
    mock_thread.mention = f"<#{thread_id}>"
    mock_thread.jump_url = f"https://discord.com/channels/1/{thread_id}"

    mock_reply = MagicMock()
    mock_reply.channel = MagicMock()
    mock_reply.create_thread = AsyncMock(return_value=mock_thread)
    mock_reply.reply = AsyncMock()

    clear_channel_turn_phase(TARGET_CHANNEL_ID)
    clear_channel_turn_phase(thread_id)

    coord = TurnCoordinator(
        channel_id=TARGET_CHANNEL_ID,
        prompt="Long running task",
        mode="home",
        reply_target=mock_reply,
        status_msg=None,
        conv_id="conv-esc-1",
    )
    coord.turn_start_time -= 300.0
    set_channel_turn_phase(TARGET_CHANNEL_ID, "tool_loop", tool_name="run_command")

    escalated = asyncio.run(
        coord.check_thread_escalation(
            proc=MagicMock(),
            escalation_enabled=True,
            escalation_seconds=180.0,
        )
    )
    assert escalated is True
    assert TARGET_CHANNEL_ID not in channel_turn_phases
    assert get_channel_turn_phase(thread_id) == "tool_loop"

    enqueue_soft_steer(TARGET_CHANNEL_ID, "Root steer", queue_dir=tmp_path)
    enqueue_soft_steer(thread_id, "Thread steer", queue_dir=tmp_path)

    with patch("tools.bridge_engine.update_beacon"), patch("tools.bridge_engine.clear_in_flight"):
        coord.cleanup_process()

    assert get_channel_turn_phase(TARGET_CHANNEL_ID) == "unknown"
    assert get_channel_turn_phase(thread_id) == "unknown"
    assert peek_pending_steers(TARGET_CHANNEL_ID, queue_dir=tmp_path) == []
    assert peek_pending_steers(thread_id, queue_dir=tmp_path) == []


def test_execute_stream_loop_restores_tool_loop_on_stale_result() -> None:
    ch_id = 6677889900
    clear_channel_turn_phase(ch_id)
    coord = TurnCoordinator(
        channel_id=ch_id,
        prompt="Test stale result phase",
        mode="home",
        reply_target=MagicMock(),
        status_msg=None,
        conv_id="conv-stale-phase",
    )
    observed_mid_phase: list[str] = []

    lines = [
        b'{"event":"result","result":{"conversation_id":"conv-stale-phase","response":""}}\n',
        b'{"event":"step_update","step_update":{"step_type":"tool","tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"ls"}}}}\n',
        b'{"event":"step_update","step_update":{"step_type":"agent_response","state":"IN_PROGRESS","text_delta":"All done"}}\n',
        b'{"event":"result","result":{"conversation_id":"conv-stale-phase","status":"SUCCESS","response":"All done"}}\n',
    ]
    line_iter = iter(lines)

    async def fake_readline() -> bytes:
        if len(observed_mid_phase) == 0 and lines:
            pass
        val = next(line_iter)
        if val.startswith(b'{"event":"step_update"') and '"tool_name"' in val.decode():
            observed_mid_phase.append(get_channel_turn_phase(ch_id))
        return val

    reader = AsyncMock()
    reader.readline = AsyncMock(side_effect=fake_readline)
    parser = MagicMock()
    parser.has_pending_background_task = False
    parser.error_response = ""
    parser.get_final_response.return_value = "All done"
    timer = MagicMock()
    proc = MagicMock()
    proc.pid = 4321

    with patch("tools.bridge_engine.set_channel_session_id"), \
         patch("tools.task_settle.get_turn_pending_tasks", return_value=[]):
        resp = asyncio.run(
            coord.execute_stream_loop(
                proc=proc,
                stdout_reader=reader,
                stream_parser=parser,
                timer=timer,
                rules={"live_status_ticker_enabled": False},
            )
        )

    assert resp == "All done"
    assert observed_mid_phase == ["tool_loop"]
    assert get_channel_turn_phase(ch_id) == "finalizing"
    clear_channel_turn_phase(ch_id)
