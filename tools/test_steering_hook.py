# ruff: noqa: I001
"""
test_steering_hook.py — Subprocess and unit tests for .agents/hooks/steering_hook.py
and .agents/hooks.json registration.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".agents"
HOOK_SCRIPT = AGENTS_DIR / "hooks" / "steering_hook.py"
HOOKS_JSON_PATH = AGENTS_DIR / "hooks.json"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(AGENTS_DIR / "hooks") not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR / "hooks"))

import steering_hook
from tools.bridge_steering import enqueue_soft_steer, peek_pending_steers


def _run_hook_subprocess(
    stdin_text: str,
    *,
    queue_dir: Path,
    env_overrides: dict[str, str | None] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Invoke hooks/steering_hook.py from .agents/ cwd just like Antigravity CLI."""
    env = os.environ.copy()
    env.pop("FORGE_BUILD_ID", None)
    env.pop("FORGE_CHUNK_INDEX", None)
    env.pop("DISCORD_CHANNEL_ID", None)
    env["ZERO_STEERING_QUEUE_DIR"] = str(queue_dir)

    if env_overrides:
        for key, val in env_overrides.items():
            if val is None:
                env.pop(key, None)
            else:
                env[key] = val

    proc = subprocess.run(
        [sys.executable, "hooks/steering_hook.py"],
        input=stdin_text,
        text=True,
        capture_output=True,
        cwd=str(AGENTS_DIR),
        env=env,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, f"Hook failed with stderr={proc.stderr!r}"
    parsed = json.loads(proc.stdout.strip())
    assert isinstance(parsed, dict)
    return proc.returncode, parsed


def test_hooks_json_registers_hybrid_turn_steering() -> None:
    data = json.loads(HOOKS_JSON_PATH.read_text(encoding="utf-8"))
    cfg = data["hybrid-turn-steering"]
    assert cfg["enabled"] is True

    pre_inv = cfg["PreInvocation"]
    assert isinstance(pre_inv, list) and len(pre_inv) == 1
    assert pre_inv[0]["type"] == "command"
    assert pre_inv[0]["command"] == "python3 hooks/steering_hook.py || py hooks/steering_hook.py"
    assert pre_inv[0]["timeout"] == 10

    pre_tool = cfg["PreToolUse"]
    assert isinstance(pre_tool, list) and len(pre_tool) == 1
    assert pre_tool[0]["matcher"] == (
        "write_file|write_to_file|edit_file|replace_file_content|run_command"
    )
    assert len(pre_tool[0]["hooks"]) == 1
    assert pre_tool[0]["hooks"][0]["type"] == "command"
    assert pre_tool[0]["hooks"][0]["command"] == "python3 hooks/steering_hook.py || py hooks/steering_hook.py"
    assert pre_tool[0]["hooks"][0]["timeout"] == 10


def test_pre_invocation_injects_and_consumes_pending_steers(tmp_path: Path) -> None:
    ch_id = "1542081375287640084"
    enqueue_soft_steer(
        ch_id,
        "Include memory pressure too",
        author_name="Ryan",
        original_task="Check NAS CPU",
        queue_dir=tmp_path,
    )
    enqueue_soft_steer(
        ch_id,
        "Summarize in 3 bullets",
        author_name="Ryan",
        queue_dir=tmp_path,
    )

    payload = json.dumps({"hookEventName": "PreInvocation"})
    _, resp = _run_hook_subprocess(
        payload,
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": ch_id},
    )

    assert resp["terminationBehavior"] == ""
    steps = resp["injectSteps"]
    assert len(steps) == 1
    msg = steps[0]["userMessage"]
    assert "[MID-TURN USER STEERING UPDATE]" in msg
    assert "Include memory pressure too" in msg
    assert "Summarize in 3 bullets" in msg
    assert "Check NAS CPU" in msg

    # Queue is now consumed; second PreInvocation returns empty {}
    assert peek_pending_steers(ch_id, queue_dir=tmp_path) == []
    _, resp2 = _run_hook_subprocess(
        payload,
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": ch_id},
    )
    assert resp2 == {}


@pytest.mark.parametrize(
    "tool_name",
    ["write_file", "write_to_file", "edit_file", "replace_file_content", "run_command"],
)
def test_pre_tool_use_denies_mutating_tools_and_prevents_duplicate_pre_invocation(
    tmp_path: Path, tool_name: str
) -> None:
    ch_id = "1544953275877556334"
    enqueue_soft_steer(
        ch_id,
        "Do not edit config yet; inspect logs first",
        author_name="Ryan",
        queue_dir=tmp_path,
    )

    tool_payload = json.dumps(
        {
            "hookEventName": "PreToolUse",
            "toolCall": {"name": tool_name, "args": {}},
        }
    )
    _, resp = _run_hook_subprocess(
        tool_payload,
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": ch_id},
    )

    assert resp["decision"] == "deny"
    assert resp["reason"].startswith(
        "Paused stale mutating tool call for incoming user steering update. Pivot immediately:\n\n"
    )
    assert "Do not edit config yet; inspect logs first" in resp["reason"]

    # Atomic single-delivery: subsequent PreInvocation & PreToolUse must NOT duplicate
    _, next_inv = _run_hook_subprocess(
        json.dumps({"hookEventName": "PreInvocation"}),
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": ch_id},
    )
    assert next_inv == {}

    _, next_tool = _run_hook_subprocess(
        tool_payload,
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": ch_id},
    )
    assert next_tool == {"decision": "allow"}


@pytest.mark.parametrize(
    "tool_name",
    ["read_file", "view_file", "grep_search", "find_by_name", "search_web"],
)
def test_pre_tool_use_allows_read_only_tools_and_preserves_steer_for_pre_invocation(
    tmp_path: Path, tool_name: str
) -> None:
    ch_id = "1551465050072416286"
    enqueue_soft_steer(
        ch_id,
        "Also check the error timestamp",
        author_name="Ryan",
        queue_dir=tmp_path,
    )

    tool_payload = json.dumps(
        {
            "hookEventName": "PreToolUse",
            "toolCall": {"name": tool_name, "args": {}},
        }
    )
    _, tool_resp = _run_hook_subprocess(
        tool_payload,
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": ch_id},
    )
    assert tool_resp == {"decision": "allow"}

    # Pending steer remains untouched until PreInvocation runs after the read
    assert len(peek_pending_steers(ch_id, queue_dir=tmp_path)) == 1

    _, inv_resp = _run_hook_subprocess(
        json.dumps({"hookEventName": "PreInvocation"}),
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": ch_id},
    )
    assert "injectSteps" in inv_resp
    assert "Also check the error timestamp" in inv_resp["injectSteps"][0]["userMessage"]
    assert peek_pending_steers(ch_id, queue_dir=tmp_path) == []


def test_concurrent_hook_subprocesses_deliver_exactly_once(tmp_path: Path) -> None:
    ch_id = "1561509863018856468"
    enqueue_soft_steer(ch_id, "Single delivery steer", queue_dir=tmp_path)

    inv_payload = json.dumps({"hookEventName": "PreInvocation"})
    tool_payload = json.dumps(
        {"hookEventName": "PreToolUse", "toolCall": {"name": "run_command", "args": {}}}
    )
    tasks = [inv_payload, tool_payload, inv_payload, tool_payload]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda p: _run_hook_subprocess(
                    p, queue_dir=tmp_path, env_overrides={"DISCORD_CHANNEL_ID": ch_id}
                )[1],
                tasks,
            )
        )

    deliveries = [
        r for r in results if "injectSteps" in r or r.get("decision") == "deny"
    ]
    assert len(deliveries) == 1
    assert peek_pending_steers(ch_id, queue_dir=tmp_path) == []


@pytest.mark.parametrize(
    "forge_env",
    [
        {"FORGE_BUILD_ID": "fg-123"},
        {"FORGE_CHUNK_INDEX": "0"},
        {"FORGE_BUILD_ID": "fg-123", "FORGE_CHUNK_INDEX": "2"},
    ],
)
def test_forge_worker_env_bypasses_steering_without_consuming(
    tmp_path: Path, forge_env: dict[str, str]
) -> None:
    ch_id = "1562899941595160657"
    enqueue_soft_steer(ch_id, "Should not be consumed by FORGE", queue_dir=tmp_path)

    env_overrides: dict[str, str | None] = {"DISCORD_CHANNEL_ID": ch_id, **forge_env}
    _, inv_resp = _run_hook_subprocess(
        json.dumps({"hookEventName": "PreInvocation"}),
        queue_dir=tmp_path,
        env_overrides=env_overrides,
    )
    assert inv_resp == {}

    _, tool_resp = _run_hook_subprocess(
        json.dumps({"hookEventName": "PreToolUse", "toolCall": {"name": "write_file", "args": {}}}),
        queue_dir=tmp_path,
        env_overrides=env_overrides,
    )
    assert tool_resp == {"decision": "allow"}
    assert len(peek_pending_steers(ch_id, queue_dir=tmp_path)) == 1


@pytest.mark.parametrize("channel_val", [None, "", "   "])
def test_missing_discord_channel_id_bypasses_cleanly(
    tmp_path: Path, channel_val: str | None
) -> None:
    ch_id = "1542081375287640084"
    enqueue_soft_steer(ch_id, "Untouched steer", queue_dir=tmp_path)

    _, inv_resp = _run_hook_subprocess(
        json.dumps({"hookEventName": "PreInvocation"}),
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": channel_val},
    )
    assert inv_resp == {}

    _, tool_resp = _run_hook_subprocess(
        json.dumps({"hookEventName": "PreToolUse", "toolCall": {"name": "edit_file", "args": {}}}),
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": channel_val},
    )
    assert tool_resp == {"decision": "allow"}
    assert len(peek_pending_steers(ch_id, queue_dir=tmp_path)) == 1


@pytest.mark.parametrize(
    ("raw_stdin", "expected"),
    [
        ("", {}),
        ("   \n", {}),
        ("{not valid json", {}),
        ('{"hookEventName": "PreToolUse", "broken": ', {"decision": "allow"}),
        ("[]", {}),
        ('"just a string"', {}),
        ('{"hookEventName": "PostToolUse"}', {}),
    ],
)
def test_fail_open_on_malformed_or_unexpected_stdin(
    tmp_path: Path, raw_stdin: str, expected: dict[str, Any]
) -> None:
    _, resp = _run_hook_subprocess(
        raw_stdin,
        queue_dir=tmp_path,
        env_overrides={"DISCORD_CHANNEL_ID": "1542081375287640084"},
    )
    assert resp == expected


def test_fail_open_on_internal_queue_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "1542081375287640084")
    monkeypatch.delenv("FORGE_BUILD_ID", raising=False)
    monkeypatch.delenv("FORGE_CHUNK_INDEX", raising=False)
    monkeypatch.setenv("ZERO_STEERING_QUEUE_DIR", str(tmp_path))

    with patch("tools.bridge_steering.consume_pending_steers", side_effect=RuntimeError("disk boom")):
        monkeypatch.setattr(
            sys.stdin,
            "read",
            lambda: json.dumps(
                {"hookEventName": "PreToolUse", "toolCall": {"name": "write_file", "args": {}}}
            ),
        )
        steering_hook.main()
        assert json.loads(capsys.readouterr().out.strip()) == {"decision": "allow"}

        monkeypatch.setattr(sys.stdin, "read", lambda: json.dumps({"hookEventName": "PreInvocation"}))
        steering_hook.main()
        assert json.loads(capsys.readouterr().out.strip()) == {}
