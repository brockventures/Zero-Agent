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


def test_load_master_key_missing_and_pull_fails_without_generating_key(tmp_path):
    """load_master_key raises FileNotFoundError and pull_vault fails loudly without generating a bogus key."""
    import pytest

    missing_key = tmp_path / "keys" / ".vault_master.key"
    with pytest.raises(FileNotFoundError):
        vault_sync.load_master_key(key_path=missing_key)
    assert not missing_key.exists()

    # Build a vault repo containing an encrypted secrets archive (encrypted with a real key elsewhere)
    real_key = tmp_path / "real_key" / ".vault_master.key"
    src_secrets = tmp_path / "src_secrets"
    src_secrets.mkdir(parents=True, exist_ok=True)
    (src_secrets / "env.json").write_text('{"TOKEN": "test-secret"}\n', encoding="utf-8")

    vault_repo = _init_git_repo(tmp_path / "vault")
    enc_out = vault_repo / "secrets" / "secrets.tar.gz.enc"
    vault_sync.encrypt_secrets_archive(secrets_dir=src_secrets, output_file=enc_out, key_path=real_key)
    subprocess.run(["git", "add", "-A"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add encrypted secrets"], cwd=str(vault_repo), check=True, capture_output=True)

    # Pull using missing_key must fail with ok=False, descriptive error, and NOT create missing_key
    ws = tmp_path / "workspace"
    dest_secrets = tmp_path / "dest_secrets"
    res = vault_sync.pull_vault(
        repo_dir=vault_repo,
        workspace_dir=ws,
        pull_remote=False,
        key_path=missing_key,
        secrets_dir=dest_secrets,
    )
    assert res["ok"] is False
    assert "error" in res and "not found" in res["error"].lower()
    assert res["decrypted_secret_files"] == 0
    assert not missing_key.exists()
    assert not (ws / "data" / "last_vault_pull.json").exists()


def test_fallback_master_key_and_extracted_secrets_enforce_0600(tmp_path, monkeypatch):
    """Fallback master key writes and all extracted secret files must enforce 0o600 permissions."""
    import stat

    # 1. Fallback master key write when primary key_path fails with OSError
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    unwritable_parent = tmp_path / "not_a_dir"
    unwritable_parent.write_text("blocker", encoding="utf-8")
    bad_key_path = unwritable_parent / "sub" / ".vault_master.key"

    key = vault_sync.get_or_create_master_key(key_path=bad_key_path)
    assert len(key) == 32
    fallback_key = fake_home / ".gemini" / ".vault_master.key"
    assert fallback_key.exists()
    assert stat.S_IMODE(fallback_key.stat().st_mode) == 0o600

    # 2. Extracted secret files enforce 0o600
    src_secrets = tmp_path / "src_secrets"
    src_secrets.mkdir(parents=True, exist_ok=True)
    (src_secrets / "env.json").write_text('{"API_KEY": "abc"}\n', encoding="utf-8")
    (src_secrets / "private.pem").write_text("-----BEGIN TEST-----\n", encoding="utf-8")
    os.chmod(src_secrets / "env.json", 0o644)
    os.chmod(src_secrets / "private.pem", 0o644)

    enc_file = tmp_path / "secrets.tar.gz.enc"
    vault_sync.encrypt_secrets_archive(secrets_dir=src_secrets, output_file=enc_file, key_path=fallback_key)

    dest_secrets = tmp_path / "extracted_secrets"
    extracted = vault_sync.extract_secrets_archive(
        encrypted_file=enc_file,
        target_dirs=[dest_secrets],
        key_path=fallback_key,
    )
    assert set(extracted) == {"env.json", "private.pem"}
    for fname in extracted:
        mode = stat.S_IMODE((dest_secrets / fname).stat().st_mode)
        assert mode == 0o600, f"Expected 0o600 on {fname}, got {oct(mode)}"


def test_crypto_error_propagation_on_pull_and_sync(tmp_path):
    """Corrupted ciphertext / wrong key on pull and encryption failure on sync return ok=False with descriptive error."""
    # 1. pull_vault with tampered ciphertext
    key_path = tmp_path / ".vault_master.key"
    vault_sync.get_or_create_master_key(key_path=key_path)

    vault_repo = _init_git_repo(tmp_path / "vault")
    enc_file = vault_repo / "secrets" / "secrets.tar.gz.enc"
    enc_file.parent.mkdir(parents=True, exist_ok=True)
    enc_file.write_bytes(b"ZVLT1" + os.urandom(12) + b"corrupted-ciphertext-bytes")
    subprocess.run(["git", "add", "-A"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "corrupt secrets"], cwd=str(vault_repo), check=True, capture_output=True)

    pull_res = vault_sync.pull_vault(
        repo_dir=vault_repo,
        workspace_dir=tmp_path / "ws_pull",
        pull_remote=False,
        key_path=key_path,
        secrets_dir=tmp_path / "out_secrets",
    )
    assert pull_res["ok"] is False
    assert "Secrets decryption failed" in pull_res.get("error", "")

    # 2. sync_vault with corrupted key file propagates encryption error loudly
    bad_key = tmp_path / "corrupt.key"
    bad_key.write_text("not-valid-base64-key!!!\n", encoding="utf-8")
    sync_repo = _init_git_repo(tmp_path / "vault_sync")
    sync_res = vault_sync.sync_vault(
        repo_dir=sync_repo,
        workspace_dir=tmp_path / "ws_sync",
        push=False,
        fetch_remote=False,
        is_prime=True,
        key_path=bad_key,
        secrets_dir=tmp_path / "src_secrets",
    )
    assert sync_res["ok"] is False
    assert "Secrets encryption failed" in sync_res.get("error", "")


def test_git_fetch_failure_preserves_unpushed_commits_and_enforces_devnull_timeout_and_flock(tmp_path):
    """Failed git fetch never runs checkout -B; all git calls pass stdin=DEVNULL & timeout=30; flock guards sync/pull."""
    # Setup bare remote and clone
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)

    vault_repo = _init_git_repo(tmp_path / "vault")
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=str(vault_repo), check=True, capture_output=True)
    (vault_repo / "memory").mkdir(parents=True, exist_ok=True)
    (vault_repo / "memory" / "base.md").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(vault_repo), check=True, capture_output=True)

    # Create an unpushed local commit on vault_repo
    (vault_repo / "memory" / "unpushed.md").write_text("unpushed local commit", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(vault_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "unpushed local"], cwd=str(vault_repo), check=True, capture_output=True)
    unpushed_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(vault_repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    # Point origin to an invalid URL so git fetch fails
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "does_not_exist.git")],
        cwd=str(vault_repo),
        check=True,
        capture_output=True,
    )

    real_run = subprocess.run
    recorded_git_calls: list[tuple[list[str], dict]] = []

    def spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            recorded_git_calls.append((list(cmd), dict(kwargs)))
        return real_run(cmd, *args, **kwargs)

    flock_ops: list[int] = []
    real_flock = vault_sync.fcntl.flock

    def spy_flock(fd, op):
        flock_ops.append(op)
        return real_flock(fd, op)

    key_path = tmp_path / ".vault_master.key"
    ws = tmp_path / "workspace"
    with patch("vault_sync.subprocess.run", side_effect=spy_run), \
         patch.object(vault_sync.fcntl, "flock", side_effect=spy_flock):
        pull_res = vault_sync.pull_vault(
            repo_dir=vault_repo,
            workspace_dir=ws,
            pull_remote=True,
            key_path=key_path,
            secrets_dir=tmp_path / "secrets",
        )
        assert pull_res["ok"] is True

        # Unpushed local commit MUST still be HEAD (not clobbered by checkout -B)
        head_after_pull = real_run(
            ["git", "rev-parse", "HEAD"], cwd=str(vault_repo), capture_output=True, text=True, check=True
        ).stdout.strip()
        assert head_after_pull == unpushed_sha
        assert (ws / "memory" / "unpushed.md").read_text(encoding="utf-8") == "unpushed local commit"

        sync_res = vault_sync.sync_vault(
            repo_dir=vault_repo,
            workspace_dir=ws,
            push=False,
            fetch_remote=True,
            is_prime=True,
            key_path=key_path,
            secrets_dir=tmp_path / "secrets",
        )
        assert sync_res["ok"] is True

    # Verify checkout -B was NEVER called while fetch failed
    checkout_b_calls = [cmd for cmd, _ in recorded_git_calls if "checkout" in cmd and "-B" in cmd]
    assert checkout_b_calls == []

    # Verify every git subprocess call passed stdin=DEVNULL and timeout=30
    assert len(recorded_git_calls) > 0
    for cmd, kw in recorded_git_calls:
        assert kw.get("stdin") == subprocess.DEVNULL, f"Missing stdin=DEVNULL on {cmd}"
        assert kw.get("timeout") == 30, f"Expected timeout=30 on {cmd}, got {kw.get('timeout')}"

    # Verify flock LOCK_EX + LOCK_UN were invoked for both pull_vault and sync_vault
    assert flock_ops.count(vault_sync.fcntl.LOCK_EX) == 2
    assert flock_ops.count(vault_sync.fcntl.LOCK_UN) == 2
