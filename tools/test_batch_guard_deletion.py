import importlib.util
from pathlib import Path
from unittest.mock import patch

HOOK_PATH = Path("/workspace/.agents/hooks/batch_guard.py")
spec = importlib.util.spec_from_file_location("batch_guard", HOOK_PATH)
batch_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch_guard)


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
            assert "File Deletion Gate Triggered" in reason


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
    cmd = "sqlite3 /workspace/data/forge.db \"UPDATE decision_cards SET status = 'approved' WHERE action_type = 'file_delete'\""
    ok, reason = batch_guard.check_tool_use(
        "run_command", {"CommandLine": cmd}, state_file=state_file, cwd="/workspace"
    )
    assert ok is False
    assert "Tamper Blocked" in reason


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
