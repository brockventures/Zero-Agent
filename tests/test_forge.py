"""
Unit tests for FORGE 150-Line Whole-Class Symbol Guard, Retry Approval Bypass, and Silent Failure-Retry Envelope.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.forge import (
    MAX_WHOLE_CLASS_SYMBOL_LINES,
    _check_oversized_class_symbol,
    _is_explicit_approval_prompt,
    evaluate_plan_approval_gate,
    format_origin_failure_envelope,
    get_db_connection,
    init_forge_db,
    run_goldfish_plan_preflight,
    validate_forge_plan,
)


def _init_dummy_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)


# ==============================================================================
# 1. 150-Line Whole-Class Symbol Guard Tests
# ==============================================================================


def test_oversized_class_symbol_guard_rejects_large_class(tmp_path: Path) -> None:
    """_check_oversized_class_symbol returns an error string when a whole class exceeds MAX_WHOLE_CLASS_SYMBOL_LINES."""
    assert MAX_WHOLE_CLASS_SYMBOL_LINES == 150
    py_file = tmp_path / "service.py"
    # Create class with 200 lines
    class_lines = ["class VoiceGatewayService:", "    def __init__(self):", "        pass"]
    for i in range(197):
        class_lines.append(f"    def method_{i}(self):\n        return {i}")
    py_file.write_text("\n".join(class_lines), encoding="utf-8")

    err = _check_oversized_class_symbol(py_file, "VoiceGatewayService", max_class_lines=MAX_WHOLE_CLASS_SYMBOL_LINES)
    assert err is not None
    assert "oversized whole-class target" in err
    assert "VoiceGatewayService" in err
    assert "service.py" in err
    assert "pin a specific method" in err
    assert "explicit line range" in err


def test_oversized_class_symbol_guard_allows_small_class(tmp_path: Path) -> None:
    """_check_oversized_class_symbol returns None when class is within the line limit."""
    py_file = tmp_path / "small.py"
    class_lines = ["class SmallService:", "    def __init__(self):", "        pass"]
    for i in range(20):
        class_lines.append(f"    def action_{i}(self):\n        pass")
    py_file.write_text("\n".join(class_lines), encoding="utf-8")

    err = _check_oversized_class_symbol(py_file, "SmallService", max_class_lines=150)
    assert err is None


def test_oversized_class_symbol_guard_allows_dotted_method(tmp_path: Path) -> None:
    """_check_oversized_class_symbol returns None when targeting a dotted method on a large class."""
    py_file = tmp_path / "service.py"
    class_lines = ["class BigService:"]
    for i in range(250):
        class_lines.append(f"    def method_{i}(self):\n        pass")
    py_file.write_text("\n".join(class_lines), encoding="utf-8")

    # Dotted method must bypass the whole-class guard
    err = _check_oversized_class_symbol(py_file, "BigService.method_42", max_class_lines=150)
    assert err is None


def test_oversized_class_symbol_guard_allows_explicit_line_range(tmp_path: Path) -> None:
    """_check_oversized_class_symbol returns None when explicit line ranges are pinned."""
    py_file = tmp_path / "service.py"
    class_lines = ["class BigService:"]
    for i in range(250):
        class_lines.append(f"    def method_{i}(self):\n        pass")
    py_file.write_text("\n".join(class_lines), encoding="utf-8")

    assert _check_oversized_class_symbol(py_file, "BigService:10-50", max_class_lines=150) is None
    assert _check_oversized_class_symbol(py_file, "10-50", max_class_lines=150) is None
    assert _check_oversized_class_symbol(py_file, "L10-L50", max_class_lines=150) is None


def test_oversized_class_symbol_guard_non_python_or_missing_file(tmp_path: Path) -> None:
    """_check_oversized_class_symbol safely ignores non-Python or non-existent files."""
    md_file = tmp_path / "README.md"
    md_file.write_text("# Documentation\n\nSome notes.\n", encoding="utf-8")
    assert _check_oversized_class_symbol(md_file, "SomeClass") is None

    missing = tmp_path / "nonexistent.py"
    assert _check_oversized_class_symbol(missing, "SomeClass") is None


def test_goldfish_preflight_rejects_oversized_class_symbol(tmp_path: Path) -> None:
    """run_goldfish_plan_preflight and validate_forge_plan reject plans targeting oversized whole classes."""
    _init_dummy_git_repo(tmp_path)
    module_py = tmp_path / "big_gateway.py"
    lines = ["class VoiceGatewayService:"]
    for i in range(200):
        lines.append(f"    def handle_{i}(self):\n        return {i}")
    module_py.write_text("\n".join(lines), encoding="utf-8")

    subprocess.run(["git", "add", "big_gateway.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    plan = {
        "title": "Voice Gateway Refactor",
        "objective": "Refactor Voice Gateway Service",
        "origin_channel_id": "123456789",
        "repo": str(tmp_path),
        "context_fact_sheet": ["Voice gateway handles audio streams"],
        "chunks": [
            {
                "title": "Chunk 0: Refactor Gateway",
                "target_files": ["big_gateway.py"],
                "target_symbols": ["big_gateway.py:VoiceGatewayService"],
                "instructions": "Refactor the VoiceGatewayService methods cleanly.",
            }
        ],
    }

    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is False
    assert any("oversized whole-class target" in f for f in findings)
    assert any("VoiceGatewayService" in f for f in findings)

    valid, reason = validate_forge_plan(plan)
    assert valid is False
    assert "Goldfish Plan Pre-Flight Gate failed" in reason
    assert "oversized whole-class target" in reason


def test_goldfish_preflight_allows_pinned_oversized_class_symbol(tmp_path: Path) -> None:
    """run_goldfish_plan_preflight and validate_forge_plan succeed when dotted method or range is pinned."""
    _init_dummy_git_repo(tmp_path)
    module_py = tmp_path / "big_gateway.py"
    lines = ["class VoiceGatewayService:"]
    for i in range(200):
        lines.append("    def handle_voice(self):\n        return 1")
    module_py.write_text("\n".join(lines), encoding="utf-8")

    subprocess.run(["git", "add", "big_gateway.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    plan = {
        "title": "Voice Gateway Refactor",
        "objective": "Refactor Voice Gateway Service",
        "origin_channel_id": "123456789",
        "repo": str(tmp_path),
        "context_fact_sheet": ["Voice gateway handles audio streams"],
        "chunks": [
            {
                "title": "Chunk 0: Refactor Gateway Method",
                "target_files": ["big_gateway.py"],
                "target_symbols": ["big_gateway.py:VoiceGatewayService.handle_voice"],
                "instructions": "Refactor VoiceGatewayService.handle_voice cleanly.",
            }
        ],
    }

    passed, findings = run_goldfish_plan_preflight(plan)
    assert passed is True
    assert findings == []

    valid, reason = validate_forge_plan(plan)
    assert valid is True
    assert reason == "Valid ForgePlan."


# ==============================================================================
# 2. Retry Directives & Approval Gate Bypass Tests
# ==============================================================================


def test_retry_explicit_approval_prompt_matches() -> None:
    """_is_explicit_approval_prompt matches retry keywords and build failure envelopes."""
    assert _is_explicit_approval_prompt("retry") is True
    assert _is_explicit_approval_prompt("re-try") is True
    assert _is_explicit_approval_prompt("queue retry") is True
    assert _is_explicit_approval_prompt("[FORGE BUILD FAILURE ENVELOPE — `fg-123`]") is True
    assert _is_explicit_approval_prompt("[forge build failure envelope — fg-456]") is True
    assert _is_explicit_approval_prompt("approve") is True
    assert _is_explicit_approval_prompt("ship it") is True
    assert _is_explicit_approval_prompt("queue fix") is True
    assert _is_explicit_approval_prompt("how does this function work?") is False
    assert _is_explicit_approval_prompt("") is False


def test_evaluate_plan_approval_gate_auto_starts_on_is_retry() -> None:
    """evaluate_plan_approval_gate bypasses Decision Card staging when plan['is_retry'] is True."""
    # Multi-chunk plan normally requires approval (Tier 3)
    plan = {
        "title": "Multi-Chunk Retry Build",
        "origin_channel_id": "123456789",
        "chunks": [
            {"title": "Chunk 0", "target_files": ["a.py"]},
            {"title": "Chunk 1", "target_files": ["b.py"]},
        ],
        "is_retry": True,
    }
    needs_approval, reasons = evaluate_plan_approval_gate(plan)
    assert needs_approval is False
    assert reasons == []


def test_evaluate_plan_approval_gate_auto_starts_on_retry_of_build_id() -> None:
    """evaluate_plan_approval_gate bypasses Decision Card staging when plan['retry_of_build_id'] is set."""
    plan = {
        "title": "Multi-Chunk Retry Build",
        "origin_channel_id": "123456789",
        "chunks": [
            {"title": "Chunk 0", "target_files": ["a.py"]},
            {"title": "Chunk 1", "target_files": ["b.py"]},
        ],
        "retry_of_build_id": "fg-prior-failed-build-1",
    }
    needs_approval, reasons = evaluate_plan_approval_gate(plan)
    assert needs_approval is False
    assert reasons == []


def test_evaluate_plan_approval_gate_auto_starts_on_failure_envelope_in_turn(tmp_path: Path) -> None:
    """evaluate_plan_approval_gate auto-starts when active in_flight_turn.json has a failure envelope."""
    in_flight_file = tmp_path / "in_flight_turn.json"
    in_flight_data = {
        "123456789": {
            "prompt": "[FORGE BUILD FAILURE ENVELOPE — `fg-1234`]\nBuild halted after exhausting 3 attempts.",
        }
    }
    in_flight_file.write_text(json.dumps(in_flight_data), encoding="utf-8")

    plan = {
        "title": "Multi-Chunk Corrected Build",
        "origin_channel_id": "123456789",
        "chunks": [
            {"title": "Chunk 0", "target_files": ["a.py"]},
            {"title": "Chunk 1", "target_files": ["b.py"]},
        ],
    }
    needs_approval, reasons = evaluate_plan_approval_gate(plan, in_flight_path=in_flight_file)
    assert needs_approval is False
    assert reasons == []


def test_evaluate_plan_approval_gate_auto_starts_retry_on_recent_failed_build_in_db(tmp_path: Path) -> None:
    """evaluate_plan_approval_gate auto-starts when forge_builds has a failed build within 7200s."""
    db_file = tmp_path / "forge_test.db"
    init_forge_db(db_file)

    now = time.time()
    with get_db_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (
                id, title, repo, tier, touches_core_bridge, origin_channel_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("fg-recent-fail", "Prior Build", "/workspace", 2, 0, 99887766, "failed", now - 300, now - 300),
        )

    plan = {
        "title": "Multi-Chunk Build Retry",
        "origin_channel_id": 99887766,
        "repo": "/workspace",
        "chunks": [
            {"title": "Chunk 0", "target_files": ["a.py"]},
            {"title": "Chunk 1", "target_files": ["b.py"]},
        ],
    }
    needs_approval, reasons = evaluate_plan_approval_gate(plan, db_path=db_file)
    assert needs_approval is False
    assert reasons == []


def test_evaluate_plan_approval_gate_expired_failed_build_retry_requires_approval(tmp_path: Path) -> None:
    """evaluate_plan_approval_gate does NOT auto-start when prior failed build is older than 7200s."""
    db_file = tmp_path / "forge_test.db"
    init_forge_db(db_file)

    now = time.time()
    with get_db_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (
                id, title, repo, tier, touches_core_bridge, origin_channel_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("fg-old-fail", "Old Build", "/workspace", 2, 0, 99887766, "failed", now - 8000, now - 7500),
        )

    plan = {
        "title": "Multi-Chunk Build Non-Retry",
        "origin_channel_id": 99887766,
        "repo": "/workspace",
        "chunks": [
            {"title": "Chunk 0", "target_files": ["a.py"]},
            {"title": "Chunk 1", "target_files": ["b.py"]},
        ],
    }
    needs_approval, reasons = evaluate_plan_approval_gate(plan, db_path=db_file)
    assert needs_approval is True
    assert any("Multi-chunk" in r for r in reasons)


def test_evaluate_plan_approval_gate_requires_approval_overrides_retry() -> None:
    """plan['requires_approval'] = True overrides retry bypass flags."""
    plan = {
        "title": "Explicit Approval Required",
        "origin_channel_id": "123456789",
        "is_retry": True,
        "retry_of_build_id": "fg-prior-fail",
        "requires_approval": True,
        "chunks": [
            {"title": "Chunk 0", "target_files": ["a.py"]},
            {"title": "Chunk 1", "target_files": ["b.py"]},
        ],
    }
    needs_approval, reasons = evaluate_plan_approval_gate(plan)
    assert needs_approval is True
    assert "Explicit requires_approval flag set on plan" in reasons


# ==============================================================================
# 3. Format Origin Failure Envelope Tests
# ==============================================================================


def test_format_origin_failure_envelope_includes_silent_retry_instructions(tmp_path: Path) -> None:
    """format_origin_failure_envelope instructs setting is_retry and retry_of_build_id, mandating [NO_REPLY]."""
    db_file = tmp_path / "forge_test.db"
    init_forge_db(db_file)

    now = time.time()
    build_id = "fg-failed-envelope-test"
    with get_db_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (
                id, title, repo, tier, touches_core_bridge, origin_channel_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (build_id, "Failing Test Build", "/workspace", 2, 0, 123456789, "failed", now, now),
        )
        conn.execute(
            """
            INSERT INTO forge_chunks (
                id, build_id, chunk_index, title, instructions, target_files_json,
                verification_cmd, status, last_error, attempt_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{build_id}-c0",
                build_id,
                0,
                "Failing Chunk",
                "Execute steps",
                '["a.py"]',
                "pytest tests/test_a.py",
                "failed",
                "AssertionError: Expected 42 but got 0",
                3,
            ),
        )

    envelope = format_origin_failure_envelope(build_id, db_path=db_file)
    assert f"[FORGE BUILD FAILURE ENVELOPE — `{build_id}`]" in envelope
    assert '"is_retry": true' in envelope
    assert f'"retry_of_build_id": "{build_id}"' in envelope
    assert "[NO_REPLY]" in envelope
    assert "human input" in envelope.lower()


def test_format_origin_failure_envelope_missing_build_returns_empty(tmp_path: Path) -> None:
    """format_origin_failure_envelope returns empty string for nonexistent build."""
    db_file = tmp_path / "forge_test.db"
    init_forge_db(db_file)
    assert format_origin_failure_envelope("nonexistent-id", db_path=db_file) == ""
