"""Unit tests for Hearth Host 2 auto-deploy script and FORGE post-merge hook."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
elif sys.path[0] != str(_REPO_ROOT):
    sys.path.remove(str(_REPO_ROOT))
    sys.path.insert(0, str(_REPO_ROOT))
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

from tools import deploy_hearth
from tools import forge as forge_mod

TEST_HOST2_IP = "198.51.100.84"
TEST_SSH_PORT = "2222"
TEST_SSH_USER = "testuser"
TEST_SSH_KEY = "/tmp/test_id_ed25519"
TEST_DOCKER_ROOT = "/srv/test-docker"


def _seed_build(
    db_path: Path,
    build_id: str,
    *,
    repo: str = "/workspace/repos/mirrormere",
    status: str = "completed",
    origin_channel_id: int = 1544955535722545253,
    origin_thread_id: int | None = None,
    plan_extra: dict | None = None,
    title: str = "Test Hearth Build",
) -> None:
    now = time.time()
    plan = {"title": title, "repo": repo, "chunks": [{"title": "c0"}]}
    if plan_extra:
        plan.update(plan_extra)
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (
                id, title, objective, repo, tier, touches_core_bridge,
                origin_channel_id, origin_thread_id, plan_json, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 2, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                build_id,
                title,
                title,
                repo,
                origin_channel_id,
                origin_thread_id,
                json.dumps(plan),
                status,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO forge_chunks (
                id, build_id, chunk_index, title, target_files_json,
                instructions, status
            ) VALUES (?, ?, 0, 'Chunk 0', '["README.md"]', 'Update README', 'merged')
            """,
            (f"{build_id}-c0", build_id),
        )


def test_mirrormere_completion_triggers_start_job(tmp_path: Path) -> None:
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    _seed_build(db_path, "fg-hearth-1", repo="/workspace/repos/mirrormere", origin_thread_id=1599999999999999999)

    with (
        patch.object(forge_mod, "start_job", return_value={"task_id": "dt-123-autodeploy"}) as mock_start,
        patch("tools.outbox.queue_outbox_message") as mock_outbox,
    ):
        ok = forge_mod.dispatch_build_completion_to_origin("fg-hearth-1", db_path=db_path)

    assert ok is True
    mock_start.assert_called_once_with(
        command="python3 /workspace/tools/deploy_hearth.py",
        channel="1599999999999999999",
        name="Auto-Deploy Hearth to Host 2",
        timeout=600,
        cwd=str(forge_mod.WORKSPACE_DIR),
        notify_on_success=True,
        synthesize=True,
        db_path=db_path,
    )
    mock_outbox.assert_called_once()
    envelope = mock_outbox.call_args.kwargs["content"]
    assert "Auto-deploy triggered" in envelope
    assert "dt-123-autodeploy" in envelope


@pytest.mark.parametrize("sibling_status", ["queued", "running", "awaiting_approval"])
def test_mirrormere_completion_defers_when_sibling_active(tmp_path: Path, sibling_status: str) -> None:
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    _seed_build(db_path, "fg-hearth-done", repo="/workspace/repos/mirrormere", status="completed")
    _seed_build(db_path, "fg-hearth-next", repo="/workspace/repos/mirrormere/", status=sibling_status)

    with (
        patch.object(forge_mod, "start_job") as mock_start,
        patch("tools.outbox.queue_outbox_message") as mock_outbox,
    ):
        ok = forge_mod.dispatch_build_completion_to_origin("fg-hearth-done", db_path=db_path)

    assert ok is True
    mock_start.assert_not_called()
    mock_outbox.assert_called_once()
    envelope = mock_outbox.call_args.kwargs["content"]
    assert "Auto-deploy was deferred" in envelope


def test_mirrormere_completion_does_not_defer_for_finished_sibling(tmp_path: Path) -> None:
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    _seed_build(db_path, "fg-hearth-old", repo="/workspace/repos/mirrormere", status="completed")
    _seed_build(db_path, "fg-hearth-fail", repo="/workspace/repos/mirrormere", status="failed")
    _seed_build(db_path, "fg-hearth-cur", repo="/workspace/repos/mirrormere", status="completed")

    with patch.object(forge_mod, "start_job", return_value={"task_id": "dt-456-autodeploy"}) as mock_start:
        res = forge_mod._maybe_trigger_post_merge_autodeploy("fg-hearth-cur", db_path=db_path)

    assert res == {"status": "triggered", "task_id": "dt-456-autodeploy"}
    mock_start.assert_called_once()


def test_mirrormere_auto_deploy_opt_out_skips_start_job(tmp_path: Path) -> None:
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    _seed_build(
        db_path,
        "fg-hearth-optout",
        repo="/workspace/repos/mirrormere",
        plan_extra={"auto_deploy": False},
    )

    with (
        patch.object(forge_mod, "start_job") as mock_start,
        patch("tools.outbox.queue_outbox_message") as mock_outbox,
    ):
        res = forge_mod._maybe_trigger_post_merge_autodeploy("fg-hearth-optout", db_path=db_path)
        ok = forge_mod.dispatch_build_completion_to_origin("fg-hearth-optout", db_path=db_path)

    assert res == {"status": "none"}
    assert ok is True
    mock_start.assert_not_called()
    envelope = mock_outbox.call_args.kwargs["content"]
    assert "Auto-deploy triggered" not in envelope
    assert "Auto-deploy was deferred" not in envelope


def test_non_hearth_repo_does_not_autodeploy(tmp_path: Path) -> None:
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    _seed_build(db_path, "fg-workspace-1", repo="/workspace")
    _seed_build(db_path, "fg-ember-1", repo="/workspace/data/repos/ember")

    with (
        patch.object(forge_mod, "start_job") as mock_start,
        patch("tools.outbox.queue_outbox_message") as mock_outbox,
    ):
        assert forge_mod._maybe_trigger_post_merge_autodeploy("fg-workspace-1", db_path=db_path) == {"status": "none"}
        assert forge_mod._maybe_trigger_post_merge_autodeploy("fg-ember-1", db_path=db_path) == {"status": "none"}
        assert forge_mod.dispatch_build_completion_to_origin("fg-workspace-1", db_path=db_path) is True

    mock_start.assert_not_called()
    assert mock_outbox.call_count == 1


def test_deploy_hearth_contains_no_hardcoded_topology_or_volume_paths() -> None:
    src = (_TOOLS_DIR / "deploy_hearth.py").read_text(encoding="utf-8")

    assert not re.search(r"\b192\.168\.\d{1,3}\.\d{1,3}\b", src)
    assert not re.search(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", src)
    assert not re.search(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", src)
    assert not re.search(r"/volume\d+", src, re.IGNORECASE)


def test_deploy_hearth_paths_ssh_opts_and_execution(tmp_path: Path) -> None:
    repo_dir = tmp_path / "mirrormere"
    (repo_dir / "deploy" / "ical-bridge").mkdir(parents=True)
    (repo_dir / "widgets" / "media-center").mkdir(parents=True)

    popen_calls: list[list[str]] = []

    def _fake_popen(cmd, *args, **kwargs):
        popen_calls.append(list(cmd))
        proc = MagicMock()
        proc.__enter__.return_value = proc
        proc.__exit__.return_value = False
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        proc.stderr.read.return_value = "" if kwargs.get("text") else b""
        proc.returncode = 0
        proc.wait.return_value = 0
        proc.communicate.return_value = ("", "") if kwargs.get("text") else (b"", b"")
        return proc

    with (
        patch.object(deploy_hearth.nas_config, "HOST_2_IP", TEST_HOST2_IP),
        patch.object(deploy_hearth.nas_config, "HOST_2_DOCKER_ROOT", TEST_DOCKER_ROOT),
        patch.object(deploy_hearth.nas_config, "SSH_PORT", TEST_SSH_PORT),
        patch.object(deploy_hearth.nas_config, "SSH_USER", TEST_SSH_USER),
        patch.object(deploy_hearth.nas_config, "SSH_KEY", TEST_SSH_KEY),
        patch.object(deploy_hearth.nas_config, "run_ssh", return_value=(0, "HEARTH_DEPLOY_OK\n", "")) as mock_ssh,
        patch.object(deploy_hearth.subprocess, "Popen", side_effect=_fake_popen),
    ):
        opts = deploy_hearth.get_ssh_opts()
        assert opts[:4] == ["-i", TEST_SSH_KEY, "-p", TEST_SSH_PORT]

        res = deploy_hearth.deploy_hearth(repo_dir=repo_dir)

    assert res["status"] == "deployed"
    assert res["host"] == TEST_HOST2_IP
    assert res["paths"]["build_src_dir"] == f"{TEST_DOCKER_ROOT}/hearth/build-src"
    assert res["paths"]["ical_bridge_dir"] == f"{TEST_DOCKER_ROOT}/hearth/deploy/ical-bridge"
    assert res["paths"]["media_center_dir"] == f"{TEST_DOCKER_ROOT}/hearth/widgets/media-center"
    assert res["paths"]["compose_file"] == f"{TEST_DOCKER_ROOT}/hearth/deploy/compose.hearth.yaml"

    ssh_cmds = [c for c in popen_calls if c[0] == "ssh"]
    assert len(ssh_cmds) == 3
    for sc in ssh_cmds:
        assert "-i" in sc and TEST_SSH_KEY in sc
        assert f"{TEST_SSH_USER}@{TEST_HOST2_IP}" in sc

    mock_ssh.assert_called_once()
    host_arg, remote_cmd = mock_ssh.call_args[0]
    assert host_arg == TEST_HOST2_IP
    assert f"{TEST_DOCKER_ROOT}/hearth/deploy/compose.hearth.yaml" in remote_cmd
    assert "up -d --build --force-recreate hearth-ical-bridge hearth-core" in remote_cmd
    assert "http://127.0.0.1:8088/healthz" in remote_cmd
    assert mock_ssh.call_args.kwargs["timeout"] == 600
