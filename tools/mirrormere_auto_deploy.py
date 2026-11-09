#!/usr/bin/env python3
"""Mirrormere-to-Hearth Auto-Deploy Sync Pipeline.

Polls upstream azylman/mirrormere main branch and automatically deploys new
commits to Host 2 Hearth stack via SSH + Docker Compose while preserving local
untracked/ignored configuration and data.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Ensure tools directory is importable when invoked directly or via pytest
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
if str(_TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR.parent))

import nas_config  # noqa: E402

DEFAULT_STATE_PATH = Path("/workspace/data/mirrormere_deploy_state.json")
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def get_upstream_head_sha(
    repo: str = "azylman/mirrormere",
    branch: str = "main",
) -> str:
    """Return the 40-char commit SHA of the upstream GitHub branch via git ls-remote."""
    url = f"https://github.com/{repo}.git"
    ref = f"refs/heads/{branch}"
    res = subprocess.run(
        ["git", "ls-remote", url, ref],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise RuntimeError(f"git ls-remote failed (rc={res.returncode}): {stderr}")

    stdout = (res.stdout or "").strip()
    if not stdout:
        raise RuntimeError(f"git ls-remote returned empty output for {url} {ref}")

    first_token = stdout.splitlines()[0].split()[0].strip()
    if not SHA_RE.match(first_token):
        raise ValueError(f"Invalid commit SHA from git ls-remote: {first_token!r}")
    return first_token.lower()


def load_deploy_state(state_path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Load persisted deployment state JSON if present, else return empty dict."""
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_deploy_state(state_path: Path, state: dict[str, Any]) -> None:
    """Persist deployment state JSON atomically."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload.setdefault("updated_at", int(time.time()))
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(state_path)


def get_host2_current_sha() -> str | None:
    """Query the current deployed git HEAD SHA in the Host 2 hearth checkout."""
    hearth_dir = f"{nas_config.DOCKER_ROOT}/hearth"
    remote_cmd = f"cd {hearth_dir} && git rev-parse HEAD"
    res = nas_config.run_ssh(nas_config.HOST_2_IP, remote_cmd, timeout=30)
    if res.returncode != 0:
        return None
    candidate = (res.stdout or "").strip().splitlines()
    if not candidate:
        return None
    sha = candidate[-1].strip().lower()
    return sha if SHA_RE.match(sha) else None


def deploy_hearth_on_host2(target_sha: str, dry_run: bool = False) -> dict[str, Any]:
    """Reset Host 2 hearth repo to target_sha, rebuild image, roll out compose, and verify healthz."""
    if not SHA_RE.match(target_sha):
        raise ValueError(f"Invalid target_sha: {target_sha!r}")

    hearth_dir = f"{nas_config.DOCKER_ROOT}/hearth"
    deploy_dir = f"{hearth_dir}/deploy"
    compose_file = f"{deploy_dir}/compose.hearth.yaml"

    remote_cmd = (
        f"set -e; "
        f"cd {hearth_dir} && git fetch origin main && git reset --hard {target_sha} && "
        f"sudo /usr/local/bin/docker build -t ghcr.io/azylman/mirrormere:latest {hearth_dir} && "
        f"cd {deploy_dir} && "
        f"sudo /usr/local/bin/docker compose -f compose.hearth.yaml up -d hearth-ical-bridge hearth-core && "
        f"for i in $(seq 1 15); do "
        f"  if curl -fsS http://127.0.0.1:8099/healthz >/dev/null && "
        f"     curl -fsS http://127.0.0.1:8088/healthz >/dev/null; then "
        f"    echo HEALTH_OK; exit 0; "
        f"  fi; "
        f"  sleep 2; "
        f"done; "
        f"echo 'Health check failed for 127.0.0.1:8099/healthz or 127.0.0.1:8088/healthz' >&2; exit 1"
    )

    if dry_run:
        return {
            "status": "dry_run",
            "sha": target_sha,
            "host": nas_config.HOST_2_IP,
            "hearth_dir": hearth_dir,
            "compose_file": compose_file,
            "command": remote_cmd,
        }

    res = nas_config.run_ssh(nas_config.HOST_2_IP, remote_cmd, timeout=300)
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        stdout = (res.stdout or "").strip()
        raise RuntimeError(
            f"Hearth deploy on Host 2 failed (rc={res.returncode}): {stderr or stdout}"
        )

    return {
        "status": "deployed",
        "sha": target_sha,
        "host": nas_config.HOST_2_IP,
        "hearth_dir": hearth_dir,
        "compose_file": compose_file,
        "stdout": (res.stdout or "").strip(),
    }


def sync_once(
    state_path: Path = DEFAULT_STATE_PATH,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Check upstream mirrormere HEAD and deploy to Host 2 when changed."""
    upstream_sha = get_upstream_head_sha()
    state = load_deploy_state(state_path)
    last_sha = state.get("last_deployed_sha")

    if not last_sha and not force:
        host2_sha = get_host2_current_sha()
        if host2_sha == upstream_sha:
            if not dry_run:
                save_deploy_state(
                    state_path,
                    {
                        "last_deployed_sha": upstream_sha,
                        "updated_at": int(time.time()),
                    },
                )
            return {"status": "seeded", "sha": upstream_sha}

    if last_sha == upstream_sha and not force:
        return {"status": "up_to_date", "sha": upstream_sha}

    if dry_run:
        deploy_hearth_on_host2(upstream_sha, dry_run=True)
        return {"status": "dry_run", "sha": upstream_sha}

    deploy_hearth_on_host2(upstream_sha, dry_run=False)
    save_deploy_state(
        state_path,
        {
            "last_deployed_sha": upstream_sha,
            "updated_at": int(time.time()),
        },
    )
    return {"status": "deployed", "sha": upstream_sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirrormere-to-Hearth Auto-Deploy Sync Pipeline"
    )
    parser.add_argument(
        "--check",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Check upstream and preview actions without executing remote deploy or saving state",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force deploy even if state matches upstream HEAD",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to JSON deploy state file",
    )
    args = parser.parse_args(argv)

    result = sync_once(
        state_path=args.state_file,
        dry_run=args.dry_run,
        force=args.force,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
