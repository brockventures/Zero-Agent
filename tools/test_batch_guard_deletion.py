import importlib.util
from pathlib import Path
from unittest.mock import patch
import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / ".agents" / "hooks" / "batch_guard.py"
spec = importlib.util.spec_from_file_location("batch_guard", HOOK_PATH)
batch_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch_guard)


@pytest.fixture(autouse=True)
def isolate_forge_worker_env(monkeypatch):
    monkeypatch.delenv("FORGE_WORKER_ACTIVE", raising=False)
    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)


def test_safe_ephemeral_deletions_allowed(tmp_path):
    state_file = str(tmp_path / "bg_state.json")
    safe_cmds = [
        "rm -f /tmp/test_output.txt",
        "rm -rf /workspace/scratch/debug_run",
        "rm -f scratch/temp_check.py",
        "rm -rf /root/.gemini/antigravity-cli/brain/1234/scratch/foo.py",
        "rm -rf /workspace/tools/__pycache__",
        "find /tmp/test_dir -name '*.tmp' -delete",
    ]
    for cmd in safe_cmds:
        ok, reason = batch_guard.check_tool_use(
            "run_command", {"CommandLine": cmd}, state_file=state_file, cwd="/workspace"
        )
        assert ok is True, f"Expected safe command '{cmd}' to be allowed, got: {reason}"


def test_protected_deletions_blocked_without_approval(tmp_path):
    state_file = str(tmp_path / "bg_state.json")
    blocked_cmds = [
        "rm /workspace/tools/forge.py",
        "rm -rf tools/bridge.py",
        "unlink /workspace/memory/public/known_non_issues.md",
        "git rm tools/zero_printer_watcher.py",
        "rm -f /secrets/env.json",
        "rm -rf /workspace/data/forge.db",
        "find /workspace/tools -name '*.py' -delete",
        "ssh root@127.0.0.1 'rm -rf /docker/baseball/data.db'",
        "python3 -c \"import os; os.remove('/workspace/tools/forge.py')\"",
    ]
    with patch.object(batch_guard, "_has_active_delete_approval", return_value=False):
        for cmd in blocked_cmds:
            ok, reason = batch_guard.check_tool_use(
                "run_command", {"CommandLine": cmd}, state_file=state_file, cwd="/workspace"
            )
            assert ok is False, f"Expected protected deletion '{cmd}' to be blocked"
            assert (
                "File Deletion Gate Triggered" in reason
                or "Hard Destructive & Secrets Gate Triggered" in reason
            ), f"Unexpected block reason for '{cmd}': {reason}"


def test_protected_deletion_allowed_when_decision_card_approved(tmp_path):
    state_file = str(tmp_path / "bg_state.json")
    with patch.object(batch_guard, "_has_active_delete_approval", return_value=True):
        ok, reason = batch_guard.check_tool_use(
            "run_command",
            {"CommandLine": "git rm tools/deprecated_helper.py && git commit -m 'chore: remove'"},
            state_file=state_file,
            cwd="/workspace",
        )
        assert ok is True, f"Expected approved deletion to pass, got: {reason}"


def test_self_approval_tamper_blocked(tmp_path):
    state_file = str(tmp_path / "bg_state.json")
    for table in ("decision_cards", "forge_decisions"):
        cmd = f"sqlite3 /workspace/data/forge.db \"UPDATE {table} SET status = 'approved' WHERE id = '1'\""
        ok, reason = batch_guard.check_tool_use(
            "run_command", {"CommandLine": cmd}, state_file=state_file, cwd="/workspace"
        )
        assert ok is False
        assert "Tamper Blocked" in reason


def test_has_active_delete_approval_requires_approved_card_env_var(tmp_path, monkeypatch):
    import sqlite3

    db_file = tmp_path / "forge_test.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE forge_decisions (id TEXT PRIMARY KEY, category TEXT, status TEXT)"
    )
    conn.execute("INSERT INTO forge_decisions VALUES ('dec-1', 'file_delete', 'approved')")
    conn.commit()
    conn.close()

    # Historical approved file_delete rows in forge.db must NEVER unlock future deletions
    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)
    assert batch_guard._has_active_delete_approval(str(db_file)) is False

    monkeypatch.setenv("FORGE_APPROVED_CARD_EXECUTION", "0")
    assert batch_guard._has_active_delete_approval(str(db_file)) is False

    # Strictly requires FORGE_APPROVED_CARD_EXECUTION=1
    monkeypatch.setenv("FORGE_APPROVED_CARD_EXECUTION", "1")
    assert batch_guard._has_active_delete_approval(str(db_file)) is True


def test_destructive_git_working_tree_wipes_blocked(tmp_path):
    state_file = str(tmp_path / "bg_state.json")
    wipe_cmds = [
        "git reset --hard origin/main",
        "git checkout-index -f -a",
        "git clean -fd",
        "git checkout origin/main -- .agents config docs tools README.md",
        "git restore --worktree tools",
    ]
    for cmd in wipe_cmds:
        ok, reason = batch_guard.check_tool_use(
            "run_command", {"CommandLine": cmd}, state_file=state_file, cwd="/workspace"
        )
        assert ok is False, f"Expected git wipe '{cmd}' to be blocked"
        assert "Destructive Git Working-Tree Wipe Blocked" in reason
