#!/usr/bin/env python3
"""
provision_vulcan.py — Host 2 Container Provisioner for Vulcan (`discord-vulcan-agent`).

Stages `$NAS_DOCKER_ROOT/discord-vulcan-agent/` on Host 2 using `tools/nas_config.py`
with zero hardcoded IPs or topology literals.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "tools"), "/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools import nas_config  # noqa: E402
from tools.bridge_state import FORGE_CHANNEL_ID, VULCAN_USER_ID  # noqa: E402

CONTAINER_NAME = "discord-vulcan-agent"
IMAGE_NAME = "discord-agy-agent-antigravity-agent:latest"

VULCAN_AGENTS_MD = """# Vulcan (**V**erified **U**nattended **L**abor & **C**ode **A**nvil **N**ode) — #forge Supervisor & Builder

## Identity & Role

You are **Vulcan** (**V**erified **U**nattended **L**abor & **C**ode **A**nvil **N**ode) — the autonomous master smith, build supervisor, and dedicated resident engineer of `#forge` (`1562899941595160657`) on Host 2 (`discord-vulcan-agent`).

- **Orchestration Hierarchy:** You collaborate with **Zero** (Lead Systems & Infrastructure Orchestrator) and **Ivy** (Lead Baseball & Sabermetric Orchestrator), supervising the **FORGE** (**F**ederated **O**rchestration, **R**untime & **G**ated **E**xecution) build queue and answering engineering/build inquiries directly inside `#forge`.
- **Execution Floor:** You supervise detached `git worktree` chunk workers (`/tmp/forge-wt-<chunk_id>`) and maintain deterministic Maker-Checker verification gates (`ruff`, `pytest`, `py_compile`).
- **Persona & Demeanor:** Stoic, surgical, industrial precision. Report build statuses, verification diagnostics, and queue states concisely with clickable file/symbol links.
"""


def render_vulcan_compose_yaml(docker_root: Optional[str] = None) -> str:
    """Render the docker-compose.yml content for `discord-vulcan-agent` on Host 2."""
    d_root = (docker_root or nas_config.DOCKER_ROOT).rstrip("/")
    zero_dir = f"{d_root}/discord-agy-agent"
    vulcan_dir = f"{d_root}/{CONTAINER_NAME}"

    return f"""version: "3.8"

services:
  {CONTAINER_NAME}:
    image: {IMAGE_NAME}
    container_name: {CONTAINER_NAME}
    restart: unless-stopped
    mem_limit: 2000m
    cpu_shares: 256
    environment:
      - AGENT_ROLE=vulcan
      - AGENT_NAME=Vulcan
      - DISCORD_CHANNEL_ID={FORGE_CHANNEL_ID}
      - VULCAN_BOT_USER_ID={VULCAN_USER_ID}
      - TZ=America/Los_Angeles
    env_file:
      - {vulcan_dir}/.env
    volumes:
      - {zero_dir}/workspace:/workspace
      - {zero_dir}/secrets:/secrets
      - {zero_dir}/app/bridge.py:/app/bridge.py
      - {zero_dir}/app/entrypoint.sh:/app/entrypoint.sh
      - {vulcan_dir}/agents.md:/root/.gemini/AGENTS.md:ro
      - {vulcan_dir}/agents.md:/root/.gemini/GEMINI.md:ro
"""


def build_remote_staging_Payload(
    docker_root: Optional[str] = None,
    discord_bot_token: Optional[str] = None,
) -> dict[str, str]:
    """Build the dictionary of files to stage inside `$NAS_DOCKER_ROOT/discord-vulcan-agent/`."""
    d_root = (docker_root or nas_config.DOCKER_ROOT).rstrip("/")
    vulcan_dir = f"{d_root}/{CONTAINER_NAME}"
    compose_yml = render_vulcan_compose_yaml(docker_root=d_root)

    env_lines = [
        "AGENT_ROLE=vulcan",
        "AGENT_NAME=Vulcan",
        f"DISCORD_CHANNEL_ID={FORGE_CHANNEL_ID}",
        f"VULCAN_BOT_USER_ID={VULCAN_USER_ID}",
    ]
    if discord_bot_token:
        env_lines.append(f"DISCORD_BOT_TOKEN={discord_bot_token}")
        env_lines.append(f"VULCAN_DISCORD_TOKEN={discord_bot_token}")

    return {
        f"{vulcan_dir}/docker-compose.yml": compose_yml,
        f"{vulcan_dir}/agents.md": VULCAN_AGENTS_MD,
        f"{vulcan_dir}/.env.defaults": "\n".join(env_lines) + "\n",
    }


def resolve_vulcan_bot_token() -> str:
    """Resolve Vulcan's Discord bot token from environment or /secrets/env.json if present."""
    for k in ("VULCAN_DISCORD_TOKEN", "VULCAN_BOT_TOKEN", "FORGE_BOT_TOKEN"):
        val = os.getenv(k, "").strip()
        if val:
            return val
    sec_path = Path("/secrets/env.json")
    if sec_path.exists():
        try:
            data = json.loads(sec_path.read_text(encoding="utf-8"))
            for k in ("VULCAN_DISCORD_TOKEN", "VULCAN_BOT_TOKEN", "FORGE_BOT_TOKEN"):
                val = str(data.get(k) or "").strip()
                if val:
                    return val
        except Exception:
            pass
    return ""


def provision_vulcan_on_host2(
    dry_run: bool = False,
    start_container: bool = False,
    docker_root: Optional[str] = None,
) -> dict:
    """Stage `$NAS_DOCKER_ROOT/discord-vulcan-agent/` on Host 2 via `nas_config.run_ssh`."""
    d_root = (docker_root or nas_config.DOCKER_ROOT).rstrip("/")
    vulcan_dir = f"{d_root}/{CONTAINER_NAME}"
    token = resolve_vulcan_bot_token()
    files = build_remote_staging_Payload(docker_root=d_root, discord_bot_token=token)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "host": nas_config.HOST_2_IP,
            "vulcan_dir": vulcan_dir,
            "files": files,
        }

    encoded_files = {
        path: base64.b64encode(content.encode("utf-8")).decode("ascii")
        for path, content in files.items()
    }
    payload_b64 = base64.b64encode(json.dumps(encoded_files).encode("utf-8")).decode("ascii")

    remote_py = (
        "import base64, json, os, pathlib\n"
        f"vulcan_dir = pathlib.Path({vulcan_dir!r})\n"
        "vulcan_dir.mkdir(parents=True, exist_ok=True)\n"
        f"data = json.loads(base64.b64decode({payload_b64!r}).decode('utf-8'))\n"
        "for fpath, b64_txt in data.items():\n"
        "    p = pathlib.Path(fpath)\n"
        "    p.parent.mkdir(parents=True, exist_ok=True)\n"
        "    p.write_text(base64.b64decode(b64_txt).decode('utf-8'), encoding='utf-8')\n"
        "env_p = vulcan_dir / '.env'\n"
        "def_p = vulcan_dir / '.env.defaults'\n"
        "if not env_p.exists() and def_p.exists():\n"
        "    env_p.write_text(def_p.read_text(encoding='utf-8'), encoding='utf-8')\n"
        "print('STAGED_OK')\n"
    )
    remote_cmd = f"python3 -c {json.dumps(remote_py)}"
    if start_container:
        remote_cmd += f" && cd {vulcan_dir} && sudo /usr/local/bin/docker-compose up -d"

    res = nas_config.run_ssh(nas_config.HOST_2_IP, remote_cmd, timeout=30)
    ok = res.returncode == 0 and "STAGED_OK" in (res.stdout or "")
    return {
        "ok": ok,
        "returncode": res.returncode,
        "stdout": (res.stdout or "").strip(),
        "stderr": (res.stderr or "").strip(),
        "vulcan_dir": vulcan_dir,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Provision discord-vulcan-agent on Host 2.")
    parser.add_argument("--dry-run", action="store_true", help="Print staged configuration without SSH execution.")
    parser.add_argument("--start", action="store_true", help="Start container via docker-compose up -d after staging.")
    args = parser.parse_args(argv)

    result = provision_vulcan_on_host2(dry_run=args.dry_run, start_container=args.start)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
