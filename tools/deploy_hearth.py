#!/usr/bin/env python3
"""Deploy Hearth (Mirrormere) stack from local repository to Host 2 over SSH."""

from __future__ import annotations

import argparse
import posixpath
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(1, str(_TOOLS_DIR))

from tools import nas_config

DEFAULT_REPO_DIR = Path("/workspace/repos/mirrormere")
HEARTH_HEALTHZ_URL = "http://127.0.0.1:8088/healthz"
DEFAULT_TAR_EXCLUDES = (".git", "__pycache__", ".pytest_cache", "node_modules")


def get_hearth_paths() -> dict[str, str]:
    """Resolve remote Host 2 Hearth directories dynamically via nas_config."""
    docker_root = getattr(nas_config, "HOST_2_DOCKER_ROOT", nas_config.DOCKER_ROOT)
    hearth_dir = posixpath.join(docker_root, "hearth")
    return {
        "hearth_dir": hearth_dir,
        "build_src_dir": posixpath.join(hearth_dir, "build-src"),
        "ical_bridge_dir": posixpath.join(hearth_dir, "deploy", "ical-bridge"),
        "media_center_dir": posixpath.join(hearth_dir, "widgets", "media-center"),
        "compose_file": posixpath.join(hearth_dir, "deploy", "compose.hearth.yaml"),
    }


def get_ssh_opts() -> list[str]:
    """Build standard SSH CLI options from nas_config without hardcoded topology."""
    return [
        "-i",
        str(nas_config.SSH_KEY),
        "-p",
        str(nas_config.SSH_PORT),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "BatchMode=yes",
    ]


def _unpack_ssh_result(res: Any) -> tuple[int, str, str]:
    """Normalize run_ssh return values across (rc, stdout, stderr) tuples and objects."""
    if isinstance(res, tuple):
        rc = int(res[0])
        stdout = str(res[1] or "") if len(res) > 1 else ""
        stderr = str(res[2] or "") if len(res) > 2 else ""
        return rc, stdout, stderr
    rc = int(getattr(res, "returncode", 1))
    stdout = str(getattr(res, "stdout", "") or "")
    stderr = str(getattr(res, "stderr", "") or "")
    return rc, stdout, stderr


def run_ssh_checked(cmd: str, timeout: int = 600) -> str:
    """Execute a command on Host 2 via nas_config.run_ssh and raise on non-zero exit."""
    res = nas_config.run_ssh(nas_config.HOST_2_IP, cmd, timeout=timeout)
    rc, stdout, stderr = _unpack_ssh_result(res)
    if stdout.strip():
        print(stdout.rstrip())
    if rc != 0:
        if stderr.strip():
            print(stderr.rstrip(), file=sys.stderr)
        raise RuntimeError(f"Remote command failed (rc={rc})")
    return stdout


def stream_tar(
    local_dir: Path,
    remote_dir: str,
    excludes: tuple[str, ...] = DEFAULT_TAR_EXCLUDES,
) -> None:
    """Stream a local directory over SSH via tar pipe into remote_dir on Host 2."""
    src_dir = Path(local_dir)
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Local sync directory does not exist: {src_dir}")

    host = nas_config.HOST_2_IP
    user = nas_config.SSH_USER
    print(f"[sync] {src_dir} -> {host}:{remote_dir}")

    tar_cmd = ["tar", "-cf", "-"]
    for pattern in excludes:
        tar_cmd.extend(["--exclude", pattern])
    tar_cmd.extend(["-C", str(src_dir), "."])

    remote_unpack = f"mkdir -p {shlex.quote(remote_dir)} && tar -xf - -C {shlex.quote(remote_dir)}"
    ssh_cmd = ["ssh", *get_ssh_opts(), f"{user}@{host}", remote_unpack]

    tar_proc = subprocess.Popen(
        tar_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ssh_proc = subprocess.Popen(
            ssh_cmd,
            stdin=tar_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if tar_proc.stdout is not None:
            tar_proc.stdout.close()

        ssh_out, ssh_err = ssh_proc.communicate(timeout=300)
        tar_err = tar_proc.stderr.read() if tar_proc.stderr is not None else b""
        tar_rc = tar_proc.wait(timeout=30)
    except Exception:
        tar_proc.kill()
        raise

    if tar_rc != 0:
        err_txt = tar_err.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Local tar failed for {src_dir} (rc={tar_rc}): {err_txt}")
    if ssh_proc.returncode != 0:
        err_txt = ssh_err.decode("utf-8", errors="replace").strip() or ssh_out.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Remote tar unpack failed for {remote_dir} (rc={ssh_proc.returncode}): {err_txt}")

    print(f"  synced {remote_dir}")


def build_remote_deploy_command(compose_file: str) -> str:
    """Construct the remote Host 2 docker compose build, recreate, and health-wait script."""
    quoted_compose = shlex.quote(compose_file)
    return (
        "set -e; "
        'DOCKER_BIN="/usr/local/bin/docker"; '
        'if [ ! -x "$DOCKER_BIN" ]; then DOCKER_BIN="docker"; fi; '
        'if "$DOCKER_BIN" info >/dev/null 2>&1; then DOCKER_CMD="$DOCKER_BIN"; '
        'else DOCKER_CMD="sudo $DOCKER_BIN"; fi; '
        f"$DOCKER_CMD compose -f {quoted_compose} up -d --build --force-recreate hearth-ical-bridge hearth-core; "
        'echo "=== WAITING FOR HEALTH ==="; '
        "healthy=0; h_core=unknown; h_ical=unknown; "
        "for i in $(seq 1 30); do "
        "  h_core=$($DOCKER_CMD inspect --format '{{.State.Health.Status}}' hearth-core 2>/dev/null || echo missing); "
        "  h_ical=$($DOCKER_CMD inspect --format '{{.State.Health.Status}}' hearth-ical-bridge 2>/dev/null || echo missing); "
        '  if [ "$h_core" = "healthy" ] && [ "$h_ical" = "healthy" ]; then '
        "    healthy=1; break; "
        "  fi; "
        "  sleep 2; "
        "done; "
        "$DOCKER_CMD ps --filter name=hearth- --format '{{.Names}} | {{.Status}} | {{.CreatedAt}}'; "
        'if [ "$healthy" -ne 1 ]; then '
        '  echo "Hearth health check timed out after 60s (hearth-core=$h_core, hearth-ical-bridge=$h_ical)" >&2; '
        "  exit 1; "
        "fi; "
        f"curl -fsS {HEARTH_HEALTHZ_URL} >/dev/null; "
        'echo "HEARTH_DEPLOY_OK"'
    )


def _get_repo_short_sha(repo_dir: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except (OSError, subprocess.SubprocessError) as err:
        print(f"[deploy] Warning: failed reading git SHA for {repo_dir}: {err}", file=sys.stderr)
    return "unknown"


def deploy_hearth(repo_dir: Path = DEFAULT_REPO_DIR, dry_run: bool = False) -> dict[str, Any]:
    """Sync mirrormere sources to Host 2, rebuild/recreate Hearth services, and verify health."""
    repo_path = Path(repo_dir)
    if not repo_path.is_dir():
        raise FileNotFoundError(f"Mirrormere repository directory not found: {repo_path}")

    paths = get_hearth_paths()
    head_sha = _get_repo_short_sha(repo_path)
    print(f"[deploy] Deploying mirrormere HEAD ({head_sha}) to Host 2")

    build_and_verify = build_remote_deploy_command(paths["compose_file"])
    if dry_run:
        return {
            "status": "dry_run",
            "sha": head_sha,
            "host": nas_config.HOST_2_IP,
            "paths": paths,
            "command": build_and_verify,
        }

    stream_tar(repo_path, paths["build_src_dir"])
    stream_tar(repo_path / "deploy" / "ical-bridge", paths["ical_bridge_dir"])
    stream_tar(repo_path / "widgets" / "media-center", paths["media_center_dir"])
    run_ssh_checked(build_and_verify, timeout=600)

    return {
        "status": "deployed",
        "sha": head_sha,
        "host": nas_config.HOST_2_IP,
        "paths": paths,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy Hearth (Mirrormere) stack to Host 2")
    parser.add_argument(
        "--repo",
        type=Path,
        default=DEFAULT_REPO_DIR,
        help="Path to local mirrormere repository checkout",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview resolved Host 2 paths and remote command without executing SSH transfers",
    )
    args = parser.parse_args(argv)

    deploy_hearth(repo_dir=args.repo, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
