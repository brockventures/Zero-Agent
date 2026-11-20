#!/usr/bin/env python3
"""OrcaSlicer 30-minute idle auto-stop sidecar for Host 2.

Checks container activity signals via a single batched SSH probe on Host 2 and
stops the container cleanly using ``docker compose stop`` after 30 minutes of
continuous inactivity.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import shlex
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("orcaslicer_idle_reaper")

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
if str(_TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR.parent))
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
if "/workspace/tools" not in sys.path:
    sys.path.append("/workspace/tools")

try:
    from tools import nas_config
except ImportError:
    import nas_config

DEFAULT_IDLE_TIMEOUT_SEC = 1800  # 30 minutes
CPU_ACTIVE_THRESHOLD_PCT = 5.0
STATE_PATH = Path("/workspace/data/orcaslicer_idle_state.json")
CONTAINER_NAME = "orcaslicer"


def _build_remote_probe_script(idle_timeout_sec: int, container_name: str = CONTAINER_NAME) -> str:
    """Build the remote Python probe script executed on Host 2."""
    return f"""
import json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone

CONTAINER = {container_name!r}
TIMEOUT_SEC = int({int(idle_timeout_sec)})
DOCKER = shutil.which("docker") or "/usr/local/bin/docker"
SS = shutil.which("ss") or "/usr/sbin/ss"
if not os.path.exists(SS) and os.path.exists("/sbin/ss"):
    SS = "/sbin/ss"

def parse_started_at(raw):
    if not raw or raw.startswith("0001-01-01"):
        return 0.0
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(\\d{{4}}-\\d{{2}}-\\d{{2}}T\\d{{2}}:\\d{{2}}:\\d{{2}})(?:\\.(\\d+))?([+-]\\d{{2}}:\\d{{2}})?$", s)
    if not m:
        return 0.0
    base, frac, tz = m.group(1), m.group(2) or "", m.group(3) or "+00:00"
    frac_us = (frac + "000000")[:6]
    dt = datetime.fromisoformat(f"{{base}}.{{frac_us}}{{tz}}")
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())

res = subprocess.run([DOCKER, "inspect", CONTAINER], capture_output=True, text=True, timeout=15)
if res.returncode != 0:
    err_l = (res.stderr or "").lower()
    if "no such" in err_l or "not found" in err_l:
        print(json.dumps({{
            "running": False,
            "uptime_sec": 0.0,
            "compose_dir": "",
            "compose_service": "",
            "cpu_pct": 0.0,
            "active_web_conns": 0,
            "cli_slicing": False,
            "recent_print_write": False,
        }}))
        sys.exit(0)
    print(json.dumps({{"error": (res.stderr or res.stdout or "docker inspect failed").strip()}}))
    sys.exit(1)

items = json.loads(res.stdout or "[]")
if not items:
    print(json.dumps({{
        "running": False,
        "uptime_sec": 0.0,
        "compose_dir": "",
        "compose_service": "",
        "cpu_pct": 0.0,
        "active_web_conns": 0,
        "cli_slicing": False,
        "recent_print_write": False,
    }}))
    sys.exit(0)

info = items[0]
state = info.get("State") or {{}}
labels = (info.get("Config") or {{}}).get("Labels") or {{}}
compose_dir = labels.get("com.docker.compose.project.working_dir", "")
compose_service = labels.get("com.docker.compose.service", "")
running = bool(state.get("Running", False))

if not running:
    print(json.dumps({{
        "running": False,
        "uptime_sec": 0.0,
        "compose_dir": compose_dir,
        "compose_service": compose_service,
        "cpu_pct": 0.0,
        "active_web_conns": 0,
        "cli_slicing": False,
        "recent_print_write": False,
    }}))
    sys.exit(0)

uptime_sec = parse_started_at(state.get("StartedAt", ""))

cpu_pct = 0.0
stats_res = subprocess.run(
    [DOCKER, "stats", "--no-stream", "--format", "{{{{.CPUPerc}}}}", CONTAINER],
    capture_output=True,
    text=True,
    timeout=15,
)
if stats_res.returncode == 0:
    raw_cpu = (stats_res.stdout or "").strip().splitlines()
    if raw_cpu:
        try:
            cpu_pct = float(raw_cpu[0].strip().rstrip("%") or 0.0)
        except ValueError:
            cpu_pct = 0.0

active_web_conns = 0
ss_res = subprocess.run(
    [SS, "-tn", "state", "established"],
    capture_output=True,
    text=True,
    timeout=10,
)
if ss_res.returncode == 0:
    for line in (ss_res.stdout or "").splitlines():
        sline = line.strip()
        if not sline or sline.startswith(("Recv-Q", "State", "Netid")):
            continue
        parts = sline.split()
        if len(parts) < 4:
            continue
        local_addr = parts[2] if len(parts) == 4 else parts[3]
        if local_addr.endswith(":3005") or local_addr.endswith(":3006"):
            active_web_conns += 1

cli_slicing = False
top_res = subprocess.run([DOCKER, "top", CONTAINER], capture_output=True, text=True, timeout=10)
if top_res.returncode == 0 and "--slice" in (top_res.stdout or ""):
    cli_slicing = True

recent_print_write = False
prints_dir = ""
for m in info.get("Mounts") or []:
    if m.get("Destination") == "/prints" and m.get("Source"):
        prints_dir = m["Source"]
        break

if prints_dir and os.path.isdir(prints_dir):
    cutoff = time.time() - TIMEOUT_SEC
    for root, _, files in os.walk(prints_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                if os.stat(fpath).st_mtime >= cutoff:
                    recent_print_write = True
                    break
            except OSError:
                pass
        if recent_print_write:
            break

print(json.dumps({{
    "running": True,
    "uptime_sec": round(uptime_sec, 2),
    "compose_dir": compose_dir,
    "compose_service": compose_service,
    "cpu_pct": cpu_pct,
    "active_web_conns": active_web_conns,
    "cli_slicing": cli_slicing,
    "recent_print_write": recent_print_write,
}}))
""".strip()


def probe_orcaslicer_activity(idle_timeout_sec: int = DEFAULT_IDLE_TIMEOUT_SEC) -> dict[str, Any]:
    """Run a single batched base64 Python probe on Host 2 and return OrcaSlicer activity signals."""
    remote_py = _build_remote_probe_script(idle_timeout_sec=idle_timeout_sec)
    b64_py = base64.b64encode(remote_py.encode("utf-8")).decode("ascii")
    remote_cmd = f"python3 -c \"import base64; exec(base64.b64decode('{b64_py}').decode('utf-8'))\""

    rc, stdout, stderr = nas_config.run_ssh(nas_config.HOST_2_IP, remote_cmd, timeout=45)

    payload: dict[str, Any] = {}
    for line in reversed((stdout or "").strip().splitlines()):
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                payload = json.loads(s)
                break
            except json.JSONDecodeError:
                continue

    if rc != 0 or not payload or "error" in payload:
        err = payload.get("error") if payload else (stderr or stdout or f"SSH exit code {rc}").strip()
        raise RuntimeError(f"OrcaSlicer activity probe failed on Host 2: {err}")

    return {
        "running": bool(payload.get("running", False)),
        "uptime_sec": float(payload.get("uptime_sec", 0.0) or 0.0),
        "compose_dir": str(payload.get("compose_dir") or ""),
        "compose_service": str(payload.get("compose_service") or ""),
        "cpu_pct": float(payload.get("cpu_pct", 0.0) or 0.0),
        "active_web_conns": int(payload.get("active_web_conns", 0) or 0),
        "cli_slicing": bool(payload.get("cli_slicing", False)),
        "recent_print_write": bool(payload.get("recent_print_write", False)),
    }


def stop_orcaslicer_compose(compose_dir: str, compose_service: str) -> tuple[bool, str]:
    """Stop the OrcaSlicer service cleanly via Docker Compose on Host 2."""
    if not compose_dir or not compose_service:
        return False, "Missing compose_dir or compose_service label on container"

    remote_cmd = (
        f"cd {shlex.quote(compose_dir)} && "
        f"sudo /usr/local/bin/docker compose stop {shlex.quote(compose_service)}"
    )
    rc, stdout, stderr = nas_config.run_ssh(nas_config.HOST_2_IP, remote_cmd, timeout=60)
    detail = (stderr or stdout or "").strip()
    if rc != 0:
        return False, detail or f"docker compose stop exited with code {rc}"
    return True, detail


def _load_state(state_path: Path) -> dict[str, Any]:
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to read %s: %s", state_path, exc)
    return {}


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(state_path)


def _clear_idle_state(state_path: Path, now_ts: float) -> None:
    state = _load_state(state_path)
    if state.get("idle_since_ts") is not None:
        state["idle_since_ts"] = None
        state["updated_at"] = now_ts
        _save_state(state_path, state)


def evaluate_and_reap(
    idle_timeout_sec: int = DEFAULT_IDLE_TIMEOUT_SEC,
    dry_run: bool = False,
    state_path: Path = STATE_PATH,
    now_ts: float | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Evaluate OrcaSlicer activity and stop via docker compose if continuously idle >= timeout."""
    ts = time.time() if now_ts is None else float(now_ts)
    probe = probe_orcaslicer_activity(idle_timeout_sec=idle_timeout_sec)

    if not probe.get("running"):
        _clear_idle_state(state_path, ts)
        return True, "", {"action": "not_running"}

    reasons: list[str] = []
    if float(probe.get("uptime_sec", 0.0)) < idle_timeout_sec:
        reasons.append("uptime_grace")
    if float(probe.get("cpu_pct", 0.0)) > CPU_ACTIVE_THRESHOLD_PCT:
        reasons.append("cpu_active")
    if int(probe.get("active_web_conns", 0)) > 0:
        reasons.append("active_web_conns")
    if bool(probe.get("cli_slicing", False)):
        reasons.append("cli_slicing")
    if bool(probe.get("recent_print_write", False)):
        reasons.append("recent_print_write")

    if reasons:
        _clear_idle_state(state_path, ts)
        return True, "", {"action": "active", "reasons": reasons, "probe": probe}

    state = _load_state(state_path)
    idle_since_ts = state.get("idle_since_ts")
    if idle_since_ts is None:
        idle_since_ts = ts
        state["idle_since_ts"] = idle_since_ts
        state["updated_at"] = ts
        _save_state(state_path, state)

    idle_sec = max(0.0, ts - float(idle_since_ts))
    if idle_sec < idle_timeout_sec:
        return True, "", {
            "action": "idle_waiting",
            "idle_sec": idle_sec,
            "idle_since_ts": float(idle_since_ts),
        }

    compose_dir = str(probe.get("compose_dir") or "")
    compose_service = str(probe.get("compose_service") or CONTAINER_NAME)

    if dry_run:
        return True, f"[DRY-RUN] Would stop idle OrcaSlicer container after {int(idle_sec)}s inactivity.", {
            "action": "dry_run_would_stop",
            "idle_sec": idle_sec,
            "compose_dir": compose_dir,
            "compose_service": compose_service,
        }

    ok, detail = stop_orcaslicer_compose(compose_dir, compose_service)
    if not ok:
        return False, f"⚠️ Failed to stop idle OrcaSlicer container: {detail}", {
            "action": "stop_failed",
            "error": detail,
            "idle_sec": idle_sec,
        }

    state["idle_since_ts"] = None
    state["last_stopped_ts"] = ts
    state["updated_at"] = ts
    _save_state(state_path, state)

    return True, "Stopped idle OrcaSlicer container after 30m inactivity.", {
        "action": "stopped",
        "idle_sec": idle_sec,
        "compose_dir": compose_dir,
        "compose_service": compose_service,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="OrcaSlicer 30-Minute Idle Auto-Stop Sidecar")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate idle state without stopping container")
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT_SEC,
        help=f"Inactivity timeout in seconds (default: {DEFAULT_IDLE_TIMEOUT_SEC})",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON result payload")
    args = parser.parse_args()

    try:
        ok, message, data = evaluate_and_reap(
            idle_timeout_sec=args.timeout_sec,
            dry_run=args.dry_run,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        ok, message, data = False, f"⚠️ OrcaSlicer idle reaper failed: {exc}", {"error": str(exc)}

    if args.json:
        print(json.dumps({"ok": ok, "message": message, "data": data}, indent=2))
    elif message:
        print(message)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
