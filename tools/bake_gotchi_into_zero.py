#!/usr/bin/env python3
"""Bake Gotchi voice gateway dependencies and live supervisor startup into Zero's main container."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import nas_config

SUPERVISOR_PATH = Path("/workspace/data/run_gotchi_gateway_supervisor.py")
GOTCHI_CONFIG_FILE = "/workspace/gotchi/backend/gateway_config.json"
REQUIRED_PIP_PACKAGES = ("fastapi", "uvicorn", "python-multipart", "httpx")

REMOTE_PATCH_SCRIPT = r'''
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

docker_root = Path(os.environ.get("DOCKER_ROOT", "/docker"))
agent_dir = docker_root / "discord-agy-agent"
dockerfile = agent_dir / "Dockerfile"
entrypoint = agent_dir / "entrypoint.sh"
compose_file = agent_dir / "docker-compose.yml"
dry_run = os.environ.get("DRY_RUN", "0") == "1"
pkgs = json.loads(os.environ.get("REQUIRED_PKGS", "[]"))
ts = time.strftime("%Y%m%d%H%M%S")

result = {
    "agent_dir": str(agent_dir),
    "dry_run": dry_run,
    "dockerfile_updated": False,
    "entrypoint_updated": False,
    "rebuild_dispatched": False,
    "added_packages": [],
}

if not dockerfile.is_file() or not entrypoint.is_file() or not compose_file.is_file():
    print(json.dumps({"ok": False, "error": f"Missing Docker files in {agent_dir}"}))
    sys.exit(1)

# 1. Patch Dockerfile idempotently
df_text = dockerfile.read_text(encoding="utf-8")
missing_pkgs = [p for p in pkgs if f"    {p}" not in df_text and f" {p}" not in df_text]
if missing_pkgs:
    anchor = "    actualpy"
    if anchor not in df_text:
        print(json.dumps({"ok": False, "error": "Could not locate pip3 install anchor in Dockerfile"}))
        sys.exit(1)
    replacement_lines = ["    actualpy \\"]
    for idx, pkg in enumerate(missing_pkgs):
        suffix = " \\" if idx < len(missing_pkgs) - 1 else ""
        replacement_lines.append(f"    {pkg}{suffix}")
    new_df_text = df_text.replace(anchor, "\n".join(replacement_lines), 1)
    result["dockerfile_updated"] = True
    result["added_packages"] = missing_pkgs
    if not dry_run:
        shutil.copy2(dockerfile, agent_dir / f"Dockerfile.bak.{ts}")
        dockerfile.write_text(new_df_text, encoding="utf-8")

# 2. Patch entrypoint.sh idempotently (guarded so Vulcan never spawns Gotchi)
ep_text = entrypoint.read_text(encoding="utf-8")
marker = "# Auto-start Gotchi Voice Gateway supervisor (Zero container only)"
if marker not in ep_text:
    exec_anchor = "exec python3 /app/bridge.py"
    if exec_anchor not in ep_text:
        print(json.dumps({"ok": False, "error": "Could not locate exec anchor in entrypoint.sh"}))
        sys.exit(1)
    snippet = (
        f"{marker}\n"
        'if [ "${AGENT_ROLE:-zero}" != "vulcan" ] && [ -f /workspace/data/run_gotchi_gateway_supervisor.py ]; then\n'
        '    if ! pgrep -f "run_gotchi_gateway_supervisor.py" > /dev/null 2>&1; then\n'
        '        nohup python3 /workspace/data/run_gotchi_gateway_supervisor.py > /dev/null 2>&1 < /dev/null &\n'
        '        echo "[Gotchi-Gateway] Started voice gateway supervisor in background."\n'
        "    fi\n"
        "fi\n\n"
        f"{exec_anchor}"
    )
    new_ep_text = ep_text.replace(exec_anchor, snippet, 1)
    result["entrypoint_updated"] = True
    if not dry_run:
        shutil.copy2(entrypoint, agent_dir / f"entrypoint.sh.bak.{ts}")
        entrypoint.write_text(new_ep_text, encoding="utf-8")
        os.chmod(entrypoint, 0o755)

# 3. Dispatch detached rebuild + recreate on Host 2 so it survives Zero stopping
if not dry_run:
    log_path = "/tmp/zero_gotchi_bake_rebuild.log"
    rebuild_cmd = (
        "export PATH=/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin:$PATH; "
        f"cd {agent_dir} && "
        f"docker compose -f {compose_file} build antigravity-agent && "
        f"docker compose -f {compose_file} up -d antigravity-agent"
    )
    subprocess.Popen(
        ["nohup", "bash", "-lc", rebuild_cmd],
        stdin=subprocess.DEVNULL,
        stdout=open(log_path, "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    result["rebuild_dispatched"] = True
    result["rebuild_log"] = log_path

result["ok"] = True
print(json.dumps(result))
'''


def patch_supervisor_config(dry_run: bool = False) -> bool:
    """Ensure Gotchi gateway supervisor sets GOTCHI_LLM_CONFIG_FILE to the live Gemini config."""
    if not SUPERVISOR_PATH.is_file():
        return False
    text = SUPERVISOR_PATH.read_text(encoding="utf-8")
    target_line = f'    env.setdefault("GOTCHI_LLM_CONFIG_FILE", "{GOTCHI_CONFIG_FILE}")'
    if "GOTCHI_LLM_CONFIG_FILE" in text:
        return False
    anchor = '    env["GOTCHI_GATEWAY_PORT"] = "8771"'
    if anchor not in text:
        return False
    updated = text.replace(anchor, f"{target_line}\n{anchor}", 1)
    if not dry_run:
        SUPERVISOR_PATH.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Bake Gotchi into Zero's main container and rebuild.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files or rebuilding.")
    args = parser.parse_args()

    sup_changed = patch_supervisor_config(dry_run=args.dry_run)
    payload_b64 = base64.b64encode(REMOTE_PATCH_SCRIPT.encode("utf-8")).decode("ascii")
    pkgs_json = json.dumps(list(REQUIRED_PIP_PACKAGES))
    remote_cmd = (
        f"DOCKER_ROOT={nas_config.DOCKER_ROOT!r} "
        f"DRY_RUN={'1' if args.dry_run else '0'} "
        f"REQUIRED_PKGS={pkgs_json!r} "
        f"python3 -c \"import base64; exec(base64.b64decode('{payload_b64}'))\""
    )
    rc, out, err = nas_config.run_ssh(nas_config.HOST_2_IP, remote_cmd, timeout=30)
    if rc != 0:
        print(json.dumps({"ok": False, "supervisor_updated": sup_changed, "rc": rc, "stdout": out, "stderr": err}))
        return 1
    try:
        parsed = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        parsed = {"ok": True, "raw": out.strip()}
    parsed["supervisor_updated"] = sup_changed
    print(json.dumps(parsed, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
