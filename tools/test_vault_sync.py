#!/usr/bin/env python3
"""
test_vault_sync.py — Unit Tests for Multi-Instance Sync Guards & Anti-Clobber Protection
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TOOLS_DIR = os.path.join(_REPO_ROOT, "tools")
_HOOKS_DIR = os.path.join(_REPO_ROOT, ".agents", "hooks")
for _p in (_REPO_ROOT, _TOOLS_DIR, _HOOKS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import vault_sync
from batch_guard import _maybe_trigger_workstation_auto_wake


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True, capture_output=True)
    return path


def test_is_zero_prime_env_override(monkeypatch):
    monkeypatch.setenv("ZERO_IS_PRIME", "1")
    assert vault_sync.is_zero_prime() is True

    monkeypatch.setenv("ZERO_IS_PRIME", "0")
    assert vault_sync.is_zero_prime() is False

    monkeypatch.delenv("ZERO_IS_PRIME", raising=False)
    monkeypatch.setattr(os, "name", "nt")
    assert vault_sync.is_zero_prime() is False


def test_get_git_file_commit_timestamps(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    mem_dir = repo / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)

    f1 = mem_dir / "doc1.md"
    f1.write_text("first commit")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit 1"], cwd=str(repo), check=True, capture_output=True)

    time.sleep(0.05)
    f2 = mem_dir / "doc2.md"
    f2.write_text("second commit")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "commit 2"], cwd=str(repo), check=True, capture_output=True)

    timestamps = vault_sync._get_git_file_commit_timestamps(repo)
    assert "memory/doc1.md" in timestamps
    assert "memory/doc2.md" in timestamps
    assert isinstance(timestamps["memory/doc1.md"], float)
    assert isinstance(timestamps["memory/doc2.md"], float)

    # Empty/invalid directory returns empty dict
    assert vault_sync._get_git_file_commit_timestamps(tmp_path / "nonexistent") == {}


def test_load_and_save_last_pull_ts(tmp_path):
    ws = tmp_path / "workspace"
    assert vault_sync._load_last_pull_ts(ws) == 0.0

    vault_sync._save_last_pull_ts(sha="abc1234", file_count=7, workspace_dir=ws)
    loaded_ts = vault_sync._load_last_pull_ts(ws)
    assert loaded_ts > 0.0

    pull_file = ws / "data" / "last_vault_pull.json"
    assert pull_file.exists()
    data = json.loads(pull_file.read_text(encoding="utf-8"))
    assert data["sha"] == "abc1234"
    assert data["file_count"] == 7
    assert "synced_at" in data


def test_sync_vault_stale_replica_anti_clobber(tmp_path):
    """Stale replica (is_prime=False) with older file mtime cannot overwrite newer remote file and cannot delete remote-only files."""
    vault_repo = _init_git_repo(tmp_path / "vault")
    workspace = tmp_path / "workspace"

    # Setup remote repo with two files
    r_mem = vault_repo / "memory" / "private"
    r_mem.mkdir(parents=True, exist_ok=True)
    (r_mem / "note.md").write_text("Remote Newer Content V2")
    (r_mem / "remote_only.md").write_text("Remote Only Memory")
    subprocess.run(["git", "add", "-A"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remote init"], cwd=str(vault_repo), check=True, capture_output=True)

    remote_ts = vault_sync._get_git_file_commit_timestamps(vault_repo)["memory/private/note.md"]

    # Setup local workspace with older version of note.md, and missing remote_only.md
    l_mem = workspace / "memory" / "private"
    l_mem.mkdir(parents=True, exist_ok=True)
    local_stale_file = l_mem / "note.md"
    local_stale_file.write_text("Local Stale Content V1")
    # Force local mtime to be older than remote commit timestamp
    stale_mtime = remote_ts - 100.0
    os.utime(local_stale_file, (stale_mtime, stale_mtime))

    res = vault_sync.sync_vault(
        repo_dir=vault_repo,
        workspace_dir=workspace,
        push=False,
        fetch_remote=False,
        prune=True,
        is_prime=False,
    )

    assert res["ok"] is True
    # 1. Stale file must be skipped and hydrated locally
    assert res["skipped_stale_files_count"] == 1
    assert "private/note.md" in res["skipped_stale_files"]
    assert (vault_repo / "memory" / "private" / "note.md").read_text() == "Remote Newer Content V2"
    assert local_stale_file.read_text() == "Remote Newer Content V2"

    # 2. Remote-only file must be preserved and hydrated locally
    assert res["preserved_remote_only_files_count"] == 1
    assert "private/remote_only.md" in res["preserved_remote_only_files"]
    assert (vault_repo / "memory" / "private" / "remote_only.md").exists() is True
    assert (workspace / "memory" / "private" / "remote_only.md").read_text() == "Remote Only Memory"


def test_sync_vault_replica_newer_and_new_files(tmp_path):
    """Replica (is_prime=False) with newly created or newer files merges cleanly alongside remote files."""
    vault_repo = _init_git_repo(tmp_path / "vault")
    workspace = tmp_path / "workspace"

    # Setup remote repo
    r_mem = vault_repo / "memory" / "public"
    r_mem.mkdir(parents=True, exist_ok=True)
    (r_mem / "existing.md").write_text("Remote Original")
    subprocess.run(["git", "add", "-A"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remote init"], cwd=str(vault_repo), check=True, capture_output=True)

    remote_ts = vault_sync._get_git_file_commit_timestamps(vault_repo)["memory/public/existing.md"]

    # Local workspace has:
    # 1. existing.md updated with a NEWER timestamp
    # 2. brand_new.md created locally
    l_mem = workspace / "memory" / "public"
    l_mem.mkdir(parents=True, exist_ok=True)
    local_updated = l_mem / "existing.md"
    local_updated.write_text("Local Updated Version Newer")
    newer_mtime = remote_ts + 50.0
    os.utime(local_updated, (newer_mtime, newer_mtime))

    local_brand_new = l_mem / "brand_new.md"
    local_brand_new.write_text("Brand New Local Memo")

    res = vault_sync.sync_vault(
        repo_dir=vault_repo,
        workspace_dir=workspace,
        push=False,
        fetch_remote=False,
        prune=False,
        is_prime=False,
    )

    assert res["ok"] is True
    assert res["skipped_stale_files_count"] == 0
    assert (vault_repo / "memory" / "public" / "existing.md").read_text() == "Local Updated Version Newer"
    assert (vault_repo / "memory" / "public" / "brand_new.md").read_text() == "Brand New Local Memo"
    assert res["changed_files"] > 0


def test_pull_vault_preserves_uncommitted_local_edits(tmp_path):
    """pull_vault() hydrates remote files while preserving uncommitted local edits modified after last pull."""
    vault_repo = _init_git_repo(tmp_path / "vault")
    workspace = tmp_path / "workspace"

    # Remote has updated file and brand new file
    r_mem = vault_repo / "memory" / "vault"
    r_mem.mkdir(parents=True, exist_ok=True)
    (r_mem / "spec.md").write_text("Remote Spec V2")
    (r_mem / "new_remote.md").write_text("Remote New Document")
    subprocess.run(["git", "add", "-A"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "remote spec"], cwd=str(vault_repo), check=True, capture_output=True)

    commit_ts = vault_sync._get_git_file_commit_timestamps(vault_repo)["memory/vault/spec.md"]

    # Local recorded pull 100 seconds ago
    pull_ts = time.time() - 100.0
    pull_file = workspace / "data" / "last_vault_pull.json"
    pull_file.parent.mkdir(parents=True, exist_ok=True)
    pull_file.write_text(json.dumps({"timestamp": pull_ts, "sha": "old_sha", "file_count": 1}))

    # Local has an uncommitted newer edit to spec.md made after last pull and after remote commit
    l_mem = workspace / "memory" / "vault"
    l_mem.mkdir(parents=True, exist_ok=True)
    local_spec = l_mem / "spec.md"
    local_spec.write_text("My Uncommitted Local Edit")
    local_edit_ts = max(pull_ts, commit_ts) + 50.0
    os.utime(local_spec, (local_edit_ts, local_edit_ts))

    res = vault_sync.pull_vault(
        repo_dir=vault_repo,
        workspace_dir=workspace,
        pull_remote=False,
        preserve_newer_local=True,
    )

    assert res["ok"] is True
    assert res["preserved_local_edits_count"] == 1
    assert "vault/spec.md" in res["preserved_local_edits"]
    assert local_spec.read_text() == "My Uncommitted Local Edit"
    # new remote file was hydrated
    assert (workspace / "memory" / "vault" / "new_remote.md").read_text() == "Remote New Document"


def test_pull_vault_schedule_hydration_on_non_prime(tmp_path, monkeypatch):
    """On non-Prime replicas, pull_vault hydrates both config/schedule.json and data/schedule.json."""
    monkeypatch.setenv("ZERO_IS_PRIME", "0")
    vault_repo = _init_git_repo(tmp_path / "vault")
    workspace = tmp_path / "workspace"

    r_cfg = vault_repo / "config"
    r_cfg.mkdir(parents=True, exist_ok=True)
    (r_cfg / "schedule.json").write_text(json.dumps({"cron": ["0 1 * * *"]}))
    subprocess.run(["git", "add", "-A"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "schedule commit"], cwd=str(vault_repo), check=True, capture_output=True)

    res = vault_sync.pull_vault(
        repo_dir=vault_repo,
        workspace_dir=workspace,
        pull_remote=False,
    )

    assert res["ok"] is True
    assert (workspace / "config" / "schedule.json").exists()
    assert (workspace / "data" / "schedule.json").exists()
    assert json.loads((workspace / "data" / "schedule.json").read_text()) == {"cron": ["0 1 * * *"]}


def test_auto_sync_workstation_if_stale(tmp_path, monkeypatch):
    """auto_sync_workstation_if_stale skips on Prime and when fresh (<6h), triggers when stale."""
    ws = tmp_path / "workspace"
    repo = _init_git_repo(tmp_path / "repo")

    # 1. Skips on Prime
    monkeypatch.setenv("ZERO_IS_PRIME", "1")
    res_prime = vault_sync.auto_sync_workstation_if_stale(workspace_dir=ws, repo_dir=repo)
    assert res_prime["skipped"] is True
    assert res_prime["reason"] == "zero_prime"

    # 2. Skips when fresh (<6h)
    monkeypatch.setenv("ZERO_IS_PRIME", "0")
    pull_file = ws / "data" / "last_vault_pull.json"
    pull_file.parent.mkdir(parents=True, exist_ok=True)
    pull_file.write_text(json.dumps({"timestamp": time.time() - 3600}))  # 1 hr old
    res_fresh = vault_sync.auto_sync_workstation_if_stale(max_age_hours=6.0, workspace_dir=ws, repo_dir=repo)
    assert res_fresh["skipped"] is True
    assert res_fresh["reason"] == "fresh"

    # 3. Triggers when stale (>6h)
    pull_file.write_text(json.dumps({"timestamp": time.time() - 30000}))  # ~8.3 hrs old
    (repo / "memory").mkdir(parents=True, exist_ok=True)
    (repo / "memory" / "fresh_doc.md").write_text("Hello from vault")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add fresh doc"], cwd=str(repo), check=True, capture_output=True)

    res_stale = vault_sync.auto_sync_workstation_if_stale(max_age_hours=6.0, workspace_dir=ws, repo_dir=repo)
    assert res_stale["skipped"] is False
    assert res_stale["vault_pull"]["ok"] is True
    assert (ws / "memory" / "fresh_doc.md").read_text() == "Hello from vault"


def test_batch_guard_workstation_auto_wake(tmp_path):
    """Test batch_guard._maybe_trigger_workstation_auto_wake debounces and updates timestamp."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    pull_file = ws / "data" / "last_vault_pull.json"

    with patch("subprocess.Popen") as mock_popen:
        _maybe_trigger_workstation_auto_wake(str(ws))
        # Should have created the file and attempted to spawn
        assert pull_file.exists()
        data = json.loads(pull_file.read_text(encoding="utf-8"))
        assert data.get("status") == "waking"
        ts_first = data.get("timestamp")
        assert ts_first > 0.0

        # Second immediate call should be debounced (<21600s) and not touch anything
        mock_popen.reset_mock()
        _maybe_trigger_workstation_auto_wake(str(ws))
        mock_popen.assert_not_called()


def test_memory_manager_dreaming_sync_vault_integration(tmp_path, monkeypatch):
    """Test that memory_manager.run_dreaming_consolidation integrates vault_sync."""
    from tools import memory_manager

    # Mock dependencies to isolate dreaming consolidation pass
    monkeypatch.setattr(memory_manager, "CHANNEL_HISTORY_PATH", tmp_path / "nonexistent_ch.json")
    monkeypatch.setattr(memory_manager, "rebuild_indexes", lambda **kwargs: None)
    monkeypatch.setattr(memory_manager, "compact_decisions_log", lambda **kwargs: {"removed": 0, "kept": 0})
    monkeypatch.setattr(memory_manager, "CC_DECISIONS_FILE", tmp_path / "decisions.md")

    mock_sync = MagicMock(return_value={"ok": True, "changed_files": 2})
    monkeypatch.setattr(vault_sync, "sync_vault", mock_sync)

    ok, rep = memory_manager.run_dreaming_consolidation(dry_run=False, hours_back=0)
    assert ok is True
    assert "Synced memory, schedule.json, and encrypted secrets to `Zero-Vault`." in rep
    mock_sync.assert_called_once_with(push=True)

    # Test error handling when sync raises
    mock_sync_err = MagicMock(side_effect=RuntimeError("sync failed"))
    monkeypatch.setattr(vault_sync, "sync_vault", mock_sync_err)
    ok_err, rep_err = memory_manager.run_dreaming_consolidation(dry_run=False, hours_back=0)
    assert ok_err is False
