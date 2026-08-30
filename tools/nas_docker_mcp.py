#!/usr/bin/env python3
"""Multi-Host NAS Docker & SSH MCP Server for Ivy-AG.

Enforces compose-only mutations, global single-command lock, and strict safety bounds.
Hosts:
  - 127.0.0.1 (Host1)
  - 127.0.0.1 (Host2)
"""

import json
import os
import shlex
import subprocess
import threading
from mcp.server.mcpserver import MCPServer

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/root/.ssh/id_ed25519")
SSH_PORT = os.environ.get("NAS_SSH_PORT", "22")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")

HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
HOST_2_IP = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

HOSTS = {
    HOST_1_IP: HOST_1_IP,
    "host1": HOST_1_IP,
    "host1": HOST_1_IP,
    HOST_2_IP: HOST_2_IP,
    "host2": HOST_2_IP,
    "host2": HOST_2_IP,
}

COMPOSE_ROOT = os.environ.get("NAS_COMPOSE_ROOT", "/docker/")

READ_TIMEOUT = 60
MUTATE_TIMEOUT = 30
BUILD_TIMEOUT = 600

_docker_lock = threading.Lock()

server = MCPServer("nas-docker")

def _ssh(host: str, remote_cmd: str, timeout: int) -> subprocess.CompletedProcess:
    cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-p", SSH_PORT,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{SSH_USER}@{host}",
        remote_cmd
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def _validate_dir(compose_dir: str) -> str:
    if not compose_dir:
        raise ValueError("compose_dir is required for compose_* actions")
    clean = os.path.normpath(compose_dir.strip())
    if not clean.startswith(COMPOSE_ROOT):
        raise ValueError(f"compose_dir must be an absolute path under {COMPOSE_ROOT}")
    return clean

@server.tool()
def nas_docker(action: str, host: str = None, compose_dir: str = "", service: str = "", tail: int = 80) -> str:
    """Execute Docker or Compose commands on configured NAS host."""
    if not host:
        host = HOST_1_IP
    normalized_host = HOSTS.get(host.lower())
    if not normalized_host:
        return json.dumps({
            "ok": False,
            "error": f"Unknown host '{host}'. Allowed: {sorted(list(set(HOSTS.values())))}"
        })
    target = normalized_host

    action = (action or "").strip().lower()

    try:
        tail = max(1, min(int(tail or 80), 400))
    except (TypeError, ValueError):
        tail = 80

    svc = shlex.quote(service) if service else ""

    try:
        if action == "ps":
            remote = "docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}'"
            timeout = READ_TIMEOUT
        elif action == "disk":
            remote = "df -h /volume1 | tail -1; echo '---'; docker system df"
            timeout = READ_TIMEOUT
        elif action == "logs":
            if not service:
                return json.dumps({"ok": False, "error": "logs requires a container name in 'service'"})
            remote = f"docker logs --tail {tail} {svc} 2>&1"
            timeout = READ_TIMEOUT
        elif action == "compose_ps":
            d = shlex.quote(_validate_dir(compose_dir))
            remote = f"cd {d} && docker compose ps"
            timeout = READ_TIMEOUT
        elif action == "compose_logs":
            d = shlex.quote(_validate_dir(compose_dir))
            remote = f"cd {d} && docker compose logs --tail {tail} {svc} 2>&1"
            timeout = READ_TIMEOUT
        elif action == "compose_stop":
            d = shlex.quote(_validate_dir(compose_dir))
            remote = f"cd {d} && docker compose stop {svc}"
            timeout = MUTATE_TIMEOUT
        elif action == "compose_start":
            d = shlex.quote(_validate_dir(compose_dir))
            remote = f"cd {d} && docker compose start {svc}"
            timeout = MUTATE_TIMEOUT
        elif action == "compose_restart":
            d = shlex.quote(_validate_dir(compose_dir))
            remote = f"cd {d} && docker compose restart {svc}"
            timeout = MUTATE_TIMEOUT
        elif action == "compose_pull":
            d = shlex.quote(_validate_dir(compose_dir))
            remote = f"cd {d} && docker compose pull {svc}"
            timeout = BUILD_TIMEOUT
        elif action == "compose_up":
            d = shlex.quote(_validate_dir(compose_dir))
            remote = f"cd {d} && docker compose up -d {svc}"
            timeout = BUILD_TIMEOUT
        else:
            return json.dumps({
                "ok": False,
                "error": f"Invalid action '{action}'. Compose actions only (no bare docker kill/stop)."
            })
    except ValueError as e:
        return json.dumps({"ok": False, "error": str(e)})

    # Enforce single command in-flight
    if not _docker_lock.acquire(blocking=False):
        return json.dumps({
            "ok": False,
            "error": "Another Docker command is currently in flight on the NAS. Wait and retry."
        })

    try:
        r = _ssh(target, remote, timeout)
        return json.dumps({
            "ok": r.returncode == 0,
            "host": target,
            "action": action,
            "exit_code": r.returncode,
            "output": (r.stdout or r.stderr or "").strip()[-4000:]
        })
    except subprocess.TimeoutExpired:
        return json.dumps({
            "ok": False,
            "host": target,
            "action": action,
            "error": f"TIMEOUT after {timeout}s. STOP: do not retry immediately."
        })
    except Exception as e:
        return json.dumps({"ok": False, "host": target, "action": action, "error": str(e)})
    finally:
        _docker_lock.release()

if __name__ == "__main__":
    server.run(transport="stdio")
