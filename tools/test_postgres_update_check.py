"""Unit tests for tools/postgres_update_check.py using RFC 5737 documentation IPs."""

from __future__ import annotations

import datetime
import importlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) in sys.path:
    sys.path.remove(str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT))

from tools import postgres_update_check


def _import_worktree_module(mod_name: str):
    """Import `tools.<mod_name>` from this worktree even if another module prepended /workspace."""
    root_str = str(_REPO_ROOT)
    if root_str in sys.path:
        sys.path.remove(root_str)
    sys.path.insert(0, root_str)
    full_name = f"tools.{mod_name}"
    existing = sys.modules.get(full_name)
    if existing is not None:
        mod_file = getattr(existing, "__file__", "") or ""
        if not mod_file.startswith(root_str):
            del sys.modules[full_name]
    return importlib.import_module(full_name)

MOCK_HOST_1_IP = "198.51.100.82"
MOCK_HOST_2_IP = "203.0.113.84"

HUB_PG16_PAYLOAD = {
    "name": "16-alpine",
    "last_updated": "2026-11-15T12:34:56.789Z",
    "digest": "sha256:manifestlist16",
    "images": [
        {"architecture": "arm64", "os": "linux", "digest": "sha256:arm64digest16"},
        {"architecture": "amd64", "os": "linux", "digest": "sha256:amd64digest16"},
        {"architecture": "amd64", "os": "windows", "digest": "sha256:windigest16"},
    ],
}

HUB_PG15_PAYLOAD = {
    "name": "15",
    "last_updated": "2026-11-16T08:00:00.000Z",
    "digest": "sha256:manifestlist15",
    "images": [
        {"architecture": "amd64", "os": "linux", "digest": "sha256:amd64digest15"},
    ],
}


class _FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def test_postgres_targets_use_split_docker_roots():
    import os

    h1 = postgres_update_check.POSTGRES_TARGETS["host1"]
    h2 = postgres_update_check.POSTGRES_TARGETS["host2"]

    assert h1["compose_dir"] == os.path.join(postgres_update_check.HOST_1_NVME_DOCKER_ROOT, "appdata")
    assert h2["compose_dir"] == os.path.join(postgres_update_check.HOST_2_DOCKER_ROOT, "baseball")
    expected_h2_backup = os.environ.get(
        "PG_HOST2_BACKUP_DIR",
        os.path.join(postgres_update_check.HOST_2_DOCKER_ROOT, "baseball", "backups", "postgres"),
    )
    assert h2["local_backup_dir"] == expected_h2_backup


def test_get_hub_tag_digests_parses_top_level_and_amd64():
    def fake_urlopen(req, timeout=15):
        assert req.full_url.endswith("/tags/16-alpine")
        return _FakeHTTPResponse(json.dumps(HUB_PG16_PAYLOAD).encode("utf-8"))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        digests, updated = postgres_update_check.get_hub_tag_digests("16-alpine")

    assert digests == {"sha256:manifestlist16", "sha256:amd64digest16"}
    assert "sha256:arm64digest16" not in digests
    assert "sha256:windigest16" not in digests
    assert updated == "2026-11-15"


def test_check_updates_current_is_quiet_and_stages_no_cards(capsys):
    def fake_urlopen(req, timeout=15):
        if req.full_url.endswith("/tags/16-alpine"):
            return _FakeHTTPResponse(json.dumps(HUB_PG16_PAYLOAD).encode("utf-8"))
        return _FakeHTTPResponse(json.dumps(HUB_PG15_PAYLOAD).encode("utf-8"))

    def fake_run_ssh(host, cmd, timeout=120):
        if "RepoDigests" in cmd:
            if host == MOCK_HOST_1_IP:
                return 0, '["postgres@sha256:amd64digest16"]', ""
            return 0, '["postgres@sha256:manifestlist15"]', ""
        if "SELECT version()" in cmd:
            if host == MOCK_HOST_1_IP:
                return 0, "PostgreSQL 16.5 on x86_64-pc-linux-musl", ""
            return 0, "PostgreSQL 15.9 on x86_64-pc-linux-gnu", ""
        return 0, "", ""

    staged = []

    with (
        patch.object(postgres_update_check.nas_config, "HOST_1_IP", MOCK_HOST_1_IP),
        patch.object(postgres_update_check.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(postgres_update_check.nas_config, "run_ssh", side_effect=fake_run_ssh),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.forge.stage_decision", side_effect=lambda **kw: staged.append(kw)),
    ):
        res = postgres_update_check.check_updates(quiet=True)

    assert staged == []
    assert res["host1"]["outdated"] is False
    assert res["host2"]["outdated"] is False
    assert capsys.readouterr().out == ""


def test_check_updates_stages_one_atomic_card_per_outdated_host(capsys):
    def fake_urlopen(req, timeout=15):
        if req.full_url.endswith("/tags/16-alpine"):
            return _FakeHTTPResponse(json.dumps(HUB_PG16_PAYLOAD).encode("utf-8"))
        return _FakeHTTPResponse(json.dumps(HUB_PG15_PAYLOAD).encode("utf-8"))

    def fake_run_ssh(host, cmd, timeout=120):
        if "RepoDigests" in cmd:
            if host == MOCK_HOST_1_IP:
                return 0, '["postgres@sha256:old16digest"]', ""
            return 0, '["postgres@sha256:old15digest"]', ""
        if "SELECT version()" in cmd:
            if host == MOCK_HOST_1_IP:
                return 0, "PostgreSQL 16.4", ""
            return 0, "PostgreSQL 15.8", ""
        return 0, "", ""

    staged = []

    def fake_stage_decision(**kwargs):
        staged.append(kwargs)
        return {"ok": True, "decision_id": f"dec-{len(staged)}"}

    with (
        patch.object(postgres_update_check.nas_config, "HOST_1_IP", MOCK_HOST_1_IP),
        patch.object(postgres_update_check.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(postgres_update_check.nas_config, "run_ssh", side_effect=fake_run_ssh),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch("tools.forge.stage_decision", side_effect=fake_stage_decision),
    ):
        res = postgres_update_check.check_updates(quiet=True)

    assert capsys.readouterr().out == ""
    assert res["host1"]["outdated"] is True
    assert res["host2"]["outdated"] is True
    assert len(staged) == 2

    assert staged[0]["category"] == "ops_upgrade"
    assert staged[0]["channel_id"] == 1544953279664889888
    assert staged[0]["handler_type"] == "shell"
    assert staged[0]["post_to_discord"] is True
    assert staged[0]["payload"] == {
        "command": "python3 /workspace/tools/postgres_update_check.py upgrade --host host1",
        "cwd": "/workspace",
    }
    assert "**Approve Action:**" in staged[0]["summary"]
    assert "pg_dumpall" in staged[0]["summary"]
    assert "postgres:16-alpine" in staged[0]["summary"]
    assert "baseball_db" not in staged[0]["summary"]

    assert staged[1]["category"] == "ops_upgrade"
    assert staged[1]["channel_id"] == 1544953279664889888
    assert staged[1]["payload"] == {
        "command": "python3 /workspace/tools/postgres_update_check.py upgrade --host host2",
        "cwd": "/workspace",
    }
    assert "**Approve Action:**" in staged[1]["summary"]
    assert "pg_dumpall" in staged[1]["summary"]
    assert "postgres:15" in staged[1]["summary"]
    assert "postgres-arr" not in staged[1]["summary"]


def test_check_eol_warning_thresholds():
    warn_close, days_close = postgres_update_check.check_eol_warning(
        "2027-11-11",
        today=datetime.date(2027, 6, 1),
    )
    assert warn_close is True
    assert days_close == 163

    warn_exact, days_exact = postgres_update_check.check_eol_warning(
        "2027-11-11",
        today=datetime.date(2027, 5, 15),
    )
    assert warn_exact is True
    assert days_exact == 180

    warn_far, days_far = postgres_update_check.check_eol_warning(
        "2027-11-11",
        today=datetime.date(2027, 5, 14),
    )
    assert warn_far is False
    assert days_far == 181


def test_check_cli_includes_eol_warning_in_card_when_within_180_days():
    def fake_urlopen(req, timeout=15):
        if req.full_url.endswith("/tags/16-alpine"):
            return _FakeHTTPResponse(json.dumps(HUB_PG16_PAYLOAD).encode("utf-8"))
        return _FakeHTTPResponse(json.dumps(HUB_PG15_PAYLOAD).encode("utf-8"))

    def fake_run_ssh(host, cmd, timeout=120):
        if "RepoDigests" in cmd:
            if host == MOCK_HOST_1_IP:
                return 0, '["postgres@sha256:amd64digest16"]', ""
            return 0, '["postgres@sha256:old15digest"]', ""
        if "SELECT version()" in cmd:
            return 0, "PostgreSQL 15.8", ""
        return 0, "", ""

    staged = []

    with (
        patch.object(postgres_update_check.nas_config, "HOST_1_IP", MOCK_HOST_1_IP),
        patch.object(postgres_update_check.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(postgres_update_check.nas_config, "run_ssh", side_effect=fake_run_ssh),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        patch.object(postgres_update_check, "check_eol_warning", side_effect=[(False, 718), (True, 90)]),
        patch("tools.forge.stage_decision", side_effect=lambda **kw: staged.append(kw) or {"ok": True}),
    ):
        rc = postgres_update_check.main(["check", "--quiet"])

    assert rc == 0
    assert len(staged) == 1
    assert "EOL Warning" in staged[0]["summary"]
    assert "90 days remaining" in staged[0]["summary"]


def test_perform_upgrade_aborts_before_compose_if_backup_fails(capsys):
    ssh_calls: list[tuple[str, str]] = []

    def fake_run_ssh(host, cmd, timeout=120):
        ssh_calls.append((host, cmd))
        return 1, "", "pg_dumpall failed: connection refused"

    with (
        patch.object(postgres_update_check.nas_config, "HOST_1_IP", MOCK_HOST_1_IP),
        patch.object(postgres_update_check.nas_config, "run_ssh", side_effect=fake_run_ssh),
    ):
        rc = postgres_update_check.main(["upgrade", "--host", "host1"])

    assert rc != 0
    assert len(ssh_calls) == 1
    assert ssh_calls[0][0] == MOCK_HOST_1_IP
    assert "pg_dumpall" in ssh_calls[0][1]
    assert "gzip -t" in ssh_calls[0][1]
    assert not any("docker compose" in cmd for _, cmd in ssh_calls)
    out = capsys.readouterr().out
    assert "aborting before Docker changes" in out


def test_perform_upgrade_happy_path_scoped_compose_and_pg_isready(capsys):
    ssh_calls: list[tuple[str, str]] = []
    ready_attempts = 0

    def fake_run_ssh(host, cmd, timeout=120):
        nonlocal ready_attempts
        ssh_calls.append((host, cmd))
        if "pg_dumpall" in cmd:
            return 0, "42M\n", ""
        if "docker compose pull db" in cmd:
            return 0, "Pulled db", ""
        if "pg_isready" in cmd:
            ready_attempts += 1
            if ready_attempts == 1:
                return 1, "accepting connections? no", ""
            return 0, "/var/run/postgresql:5432 - accepting connections", ""
        if "SELECT version()" in cmd:
            return 0, "PostgreSQL 15.10 on x86_64-pc-linux-gnu", ""
        return 0, "", ""

    with (
        patch.object(postgres_update_check.nas_config, "HOST_2_IP", MOCK_HOST_2_IP),
        patch.object(postgres_update_check.nas_config, "run_ssh", side_effect=fake_run_ssh),
        patch("time.sleep", return_value=None),
    ):
        rc = postgres_update_check.main(["upgrade", "--host", "host2"])

    assert rc == 0
    assert all(host == MOCK_HOST_2_IP for host, _ in ssh_calls)
    assert any("pg_dumpall -U myuser" in cmd and "gzip -t" in cmd for _, cmd in ssh_calls)
    assert any(
        "docker compose pull db && docker compose up -d db" in cmd
        and "/volume1/@appstore/ContainerManager/usr/bin" in cmd
        for _, cmd in ssh_calls
    )
    assert ready_attempts == 2
    out = capsys.readouterr().out
    assert "PostgreSQL 15.10" in out
    assert "42M" in out


def test_schedule_json_catchup_windows_and_postgres_monthly_job():
    schedule_path = _REPO_ROOT / "data" / "schedule.json"
    if not schedule_path.exists():
        schedule_path = Path("/workspace/data/schedule.json")
    if not schedule_path.exists():
        pytest.skip("data/schedule.json not present in worktree")
    jobs = json.loads(schedule_path.read_text(encoding="utf-8"))
    by_id = {job["id"]: job for job in jobs}

    for jid in ("ha_update_check", "dockhand_update"):
        assert by_id[jid]["catchup_if_missed"] is True
        assert by_id[jid]["catchup_window_seconds"] == 86400

    assert by_id["antigravity_check"]["catchup_if_missed"] is True
    assert by_id["antigravity_check"]["catchup_window_seconds"] == 14400

    pg_job = by_id["postgres_update_check"]
    assert pg_job["enabled"] is True
    assert pg_job["name"] == "Monthly Postgres Minor Patch Check"
    assert pg_job["schedule_type"] == "monthly"
    assert pg_job["day_of_month"] == 18
    assert pg_job["hour_pt"] == 10
    assert pg_job["minute_pt"] == 30
    assert pg_job["channel_id"] == 1544953279664889888
    assert pg_job["catchup_if_missed"] is True
    assert pg_job["catchup_window_seconds"] == 86400
    assert "postgres_update_check.py" in pg_job["prompt"]


def test_scheduler_tool_defaults_and_aliases():
    scheduler_tool = _import_worktree_module("scheduler_tool")

    defaults = {job["id"]: job for job in scheduler_tool.DEFAULT_JOBS}
    assert defaults["ha_update_check"]["catchup_if_missed"] is True
    assert defaults["ha_update_check"]["catchup_window_seconds"] == 86400
    assert defaults["dockhand_update"]["catchup_if_missed"] is True
    assert defaults["dockhand_update"]["catchup_window_seconds"] == 86400
    assert defaults["antigravity_check"]["catchup_if_missed"] is True
    assert defaults["antigravity_check"]["catchup_window_seconds"] == 14400
    assert scheduler_tool.SIDECAR_ALIASES["postgres_update_check"] == [
        "postgres_update_check",
        "postgres_update",
    ]


def test_sidecars_run_postgres_update_check_delegation():
    from subprocess import CompletedProcess

    sidecars = _import_worktree_module("sidecars")

    with patch(
        "tools.sidecars.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    ) as mock_run:
        ok, out = sidecars.run_postgres_update_check()

    assert ok is True
    assert out == ""
    mock_run.assert_called_once_with(
        ["python3", "/workspace/tools/postgres_update_check.py", "check", "--quiet"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    with patch(
        "tools.sidecars.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=0, stdout="fallback summary\n", stderr=""),
    ):
        ok, out = sidecars.run_postgres_update_check()
    assert ok is True
    assert out == "fallback summary"

    with patch(
        "tools.sidecars.subprocess.run",
        side_effect=RuntimeError("boom"),
    ):
        ok, out = sidecars.run_postgres_update_check()
    assert ok is False
    assert "boom" in out
