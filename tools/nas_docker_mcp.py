#!/usr/bin/env python3
"""Multi-Host NAS Docker & SSH MCP Server for Zero.

Enforces compose-only mutations, global single-command lock, and strict safety bounds.
"""

import json
import os
import re
import shlex
import subprocess
import threading
import urllib.parse
from mcp.server.mcpserver import MCPServer

SSH_KEY = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
SSH_USER = os.environ.get("NAS_SSH_USER", "Brock")

def _resolve_nas_config():
    ssh_port = os.environ.get("NAS_SSH_PORT", "22")
    host_1 = os.environ.get("NAS_HOST_1_IP")
    host_2 = os.environ.get("NAS_HOST_2_IP")

    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_SSH_PORT"):
                    ssh_port = str(d["NAS_SSH_PORT"])
                if d.get("NAS_HOST_1_IP"):
                    host_1 = d["NAS_HOST_1_IP"]
                elif d.get("HA_BASE_URL"):
                    host_1 = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
        except Exception:
            pass

    if not host_1 and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                if d.get("url"):
                    host_1 = urllib.parse.urlparse(d["url"]).hostname
        except Exception:
            pass

    if not host_2 and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_HOST_2_IP"):
                    host_2 = d["NAS_HOST_2_IP"]
        except Exception:
            pass

    if host_1 and not host_2:
        parts = host_1.split(".")
        if len(parts) == 4 and parts[-1] == "82":
            host_2 = ".".join(parts[:3] + ["84"])

    return host_1 or "127.0.0.1", host_2 or "127.0.0.1", ssh_port

HOST_1_IP, HOST_2_IP, SSH_PORT = _resolve_nas_config()

def _get_hosts():
    h1, h2, _ = _resolve_nas_config()
    return {
        h1: h1,
        "host1": h1,
        
        h2: h2,
        "host2": h2,
        
        "127.0.0.1": h1,
    }

COMPOSE_ROOT = os.environ.get("NAS_COMPOSE_ROOT", "/docker/")

READ_TIMEOUT = 60
MUTATE_TIMEOUT = 30
BUILD_TIMEOUT = 600

_docker_lock = threading.Lock()

server = MCPServer("nas-docker")

def _ssh(host: str, remote_cmd: str, timeout: int) -> subprocess.CompletedProcess:
    _, _, port = _resolve_nas_config()
    cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-p", port,
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
    hosts = _get_hosts()
    h1, _, _ = _resolve_nas_config()
    if not host:
        host = h1
    normalized_host = hosts.get(host.lower())
    if not normalized_host:
        return json.dumps({
            "ok": False,
            "error": f"Unknown host '{host}'. Allowed: {sorted(list(set(hosts.values())))}"
        })
    target = normalized_host

    action = (action or "").strip().lower()

    try:
        tail = max(1, min(int(tail or 80), 400))
    except Exception:
        tail = 80

    if action == "ps":
        remote_cmd = "docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'"
        timeout = READ_TIMEOUT
    elif action == "ps_all":
        remote_cmd = "docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'"
        timeout = READ_TIMEOUT
    elif action == "logs":
        if not service:
            return json.dumps({"ok": False, "error": "service name is required for logs"})
        clean_svc = re.sub(r"[^a-zA-Z0-9_.-]", "", service)
        remote_cmd = f"docker logs --tail {tail} {clean_svc}"
        timeout = READ_TIMEOUT
    elif action == "compose_ps":
        try:
            cdir = _validate_dir(compose_dir)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        remote_cmd = f"cd {shlex.quote(cdir)} && docker-compose ps"
        timeout = READ_TIMEOUT
    elif action == "compose_up":
        try:
            cdir = _validate_dir(compose_dir)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        svc_arg = f" {shlex.quote(service)}" if service else ""
        remote_cmd = f"cd {shlex.quote(cdir)} && docker-compose up -d{svc_arg}"
        timeout = MUTATE_TIMEOUT
    elif action == "compose_down":
        try:
            cdir = _validate_dir(compose_dir)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        remote_cmd = f"cd {shlex.quote(cdir)} && docker-compose down"
        timeout = MUTATE_TIMEOUT
    elif action == "compose_restart":
        try:
            cdir = _validate_dir(compose_dir)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        svc_arg = f" {shlex.quote(service)}" if service else ""
        remote_cmd = f"cd {shlex.quote(cdir)} && docker-compose restart{svc_arg}"
        timeout = MUTATE_TIMEOUT
    elif action == "compose_pull":
        try:
            cdir = _validate_dir(compose_dir)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        svc_arg = f" {shlex.quote(service)}" if service else ""
        remote_cmd = f"cd {shlex.quote(cdir)} && docker-compose pull{svc_arg}"
        timeout = BUILD_TIMEOUT
    elif action == "compose_build":
        try:
            cdir = _validate_dir(compose_dir)
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)})
        svc_arg = f" {shlex.quote(service)}" if service else ""
        remote_cmd = f"cd {shlex.quote(cdir)} && docker-compose build{svc_arg}"
        timeout = BUILD_TIMEOUT
    else:
        return json.dumps({
            "ok": False,
            "error": f"Unsupported action '{action}'. Allowed: ps, ps_all, logs, compose_ps, compose_up, compose_down, compose_restart, compose_pull, compose_build"
        })

    is_mutate = action.startswith("compose_") and action not in ("compose_ps",)

    if is_mutate:
        acquired = _docker_lock.acquire(timeout=5)
        if not acquired:
            return json.dumps({"ok": False, "error": "Another Docker mutation is currently in progress. Try again."})
        try:
            res = _ssh(target, remote_cmd, timeout)
        finally:
            _docker_lock.release()
    else:
        try:
            res = _ssh(target, remote_cmd, timeout)
        except subprocess.TimeoutExpired:
            return json.dumps({"ok": False, "error": f"Command timed out after {timeout}s on {target}"})
        except Exception as e:
            return json.dumps({"ok": False, "error": f"SSH error: {e}"})

    if res.returncode != 0:
        return json.dumps({
            "ok": False,
            "returncode": res.returncode,
            "error": res.stderr.strip() or f"Command exited with code {res.returncode}",
            "output": res.stdout.strip()
        })

    return json.dumps({
        "ok": True,
        "host": target,
        "action": action,
        "output": res.stdout.strip()
    })

if __name__ == "__main__":
    import sys
    port = 8767
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=")[1])
        elif arg == "--port" and sys.argv.index(arg) + 1 < len(sys.argv):
            port = int(sys.argv[sys.argv.index(arg) + 1])
    if "--sse" in sys.argv:
        server.run(transport="sse", host="127.0.0.1", port=port)
    else:
        server.run(transport="stdio")

