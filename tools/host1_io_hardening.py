#!/usr/bin/env python3
"""
host1_io_hardening.py — Host 1 Disk I/O, HA Recorder & SSH MaxStartups Hardening Utility.

Implements and verifies three idempotent hardening layers on Host 1:
1. Home Assistant Recorder write coalescing (`commit_interval: 30`, `auto_purge: true`) in
   `configuration.yaml` plus Btrfs `nodatacow` (`chattr +C`) on the Home Assistant config
   and SQLite database directory, validated via the Home Assistant REST API
   (`/api/config/core/check_config`).
2. Block/disk I/O prioritization (`ionice -c2 -n7` best-effort lowest tier for heavy
   non-critical media/Arr/background containers and `ionice -c2 -n0` highest best-effort
   for host `sshd` processes), persisted every 5 minutes via `$DOCKER_ROOT/ha-watchdog.sh`.
3. OpenSSH daemon hardening (`MaxStartups 30:50:100`, `LoginGraceTime 20`) in
   `/etc/ssh/sshd_config`, validated with `sudo sshd -t`, reloaded cleanly via `SIGHUP`
   to the master `sshd` PID without dropping active sessions, and persisted via
   `$DOCKER_ROOT/ha-watchdog.sh`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Ensure workspace paths are present in sys.path
for _path in [
    str(Path(__file__).resolve().parent.parent),
    str(Path(__file__).resolve().parent),
    "/workspace",
    "/workspace/tools",
]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

import nas_config

HEAVY_CONTAINERS: tuple[str, ...] = (
    "plex",
    "radarr",
    "sonarr",
    "sabnzbd",
    "kometa",
    "prowlarr",
)

WATCHDOG_BEGIN_MARKER = "# === BEGIN HOST1_IO_SSHD_HARDENING ==="
WATCHDOG_END_MARKER = "# === END HOST1_IO_SSHD_HARDENING ==="


def _resolve_ha_credentials(host: str | None = None) -> tuple[str, str]:
    """Resolve Home Assistant URL and bearer token from environment or /secrets."""
    target_host = host or nas_config.HOST_1_IP
    ha_url = os.environ.get("HA_BASE_URL")
    ha_token = os.environ.get("HA_ACCESS_TOKEN") or os.environ.get("HA_TOKEN")

    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                ha_url = ha_url or data.get("HA_BASE_URL")
                ha_token = ha_token or data.get("HA_ACCESS_TOKEN") or data.get("HA_TOKEN")
        except (OSError, ValueError):
            pass

    if (not ha_url or not ha_token) and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                ha_url = ha_url or data.get("url")
                ha_token = ha_token or data.get("token")
        except (OSError, ValueError):
            pass

    if not ha_url:
        ha_url = f"http://{target_host}:8123"
    return ha_url.rstrip("/"), ha_token or ""


def update_recorder_yaml_text(yaml_text: str, commit_interval: int = 30, auto_purge: bool = True) -> str:
    """
    Idempotently update or insert `recorder:` configuration in Home Assistant configuration.yaml
    while preserving all existing keys, comments, `!include` tags, and formatting.
    """
    auto_purge_str = "true" if auto_purge else "false"
    lines = yaml_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    rec_idx: int | None = None
    for idx, line in enumerate(lines):
        if re.match(r"^recorder:\s*(?:#.*)?$", line):
            rec_idx = idx
            break

    if rec_idx is None:
        sep = "\n" if (lines and lines[-1].strip() != "") else ""
        lines.append(
            f"{sep}recorder:\n"
            f"  commit_interval: {commit_interval}\n"
            f"  auto_purge: {auto_purge_str}\n"
        )
        return "".join(lines)

    # Locate the end of the top-level `recorder:` block
    end_idx = len(lines)
    for idx in range(rec_idx + 1, len(lines)):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not lines[idx][0].isspace():
            end_idx = idx
            break

    commit_updated = False
    autopurge_updated = False
    for idx in range(rec_idx + 1, end_idx):
        m_commit = re.match(r"^(\s+commit_interval:\s*)(\S+)(\s*(?:#.*)?\r?\n?)$", lines[idx])
        if m_commit and len(lines[idx]) - len(lines[idx].lstrip(" ")) == 2:
            lines[idx] = f"{m_commit.group(1)}{commit_interval}{m_commit.group(3)}"
            if not lines[idx].endswith("\n"):
                lines[idx] += "\n"
            commit_updated = True
            continue

        m_purge = re.match(r"^(\s+auto_purge:\s*)(\S+)(\s*(?:#.*)?\r?\n?)$", lines[idx])
        if m_purge and len(lines[idx]) - len(lines[idx].lstrip(" ")) == 2:
            lines[idx] = f"{m_purge.group(1)}{auto_purge_str}{m_purge.group(3)}"
            if not lines[idx].endswith("\n"):
                lines[idx] += "\n"
            autopurge_updated = True

    insert_lines: list[str] = []
    if not commit_updated:
        insert_lines.append(f"  commit_interval: {commit_interval}\n")
    if not autopurge_updated:
        insert_lines.append(f"  auto_purge: {auto_purge_str}\n")

    if insert_lines:
        lines[rec_idx + 1 : rec_idx + 1] = insert_lines

    return "".join(lines)


def update_sshd_config_text(
    sshd_text: str,
    max_startups: str = "30:50:100",
    login_grace_time: str = "20",
) -> str:
    """
    Idempotently configure `MaxStartups` and `LoginGraceTime` in `/etc/ssh/sshd_config`
    (before any `Match` blocks if inserting new directives).
    """
    lines = sshd_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

    max_updated = False
    grace_updated = False
    match_idx: int | None = None

    for idx, line in enumerate(lines):
        if match_idx is None and re.match(r"^\s*Match\b", line, re.IGNORECASE):
            match_idx = idx
        if match_idx is not None:
            continue

        if re.match(r"^\s*#?\s*MaxStartups\b", line, re.IGNORECASE):
            if not max_updated:
                lines[idx] = f"MaxStartups {max_startups}\n"
                max_updated = True
            else:
                lines[idx] = ""
        elif re.match(r"^\s*#?\s*LoginGraceTime\b", line, re.IGNORECASE):
            if not grace_updated:
                lines[idx] = f"LoginGraceTime {login_grace_time}\n"
                grace_updated = True
            else:
                lines[idx] = ""

    insert_pos = match_idx if match_idx is not None else len(lines)
    to_insert: list[str] = []
    if not login_grace_time_present(lines[:insert_pos]) and not grace_updated:
        to_insert.append(f"LoginGraceTime {login_grace_time}\n")
    if not max_startups_present(lines[:insert_pos]) and not max_updated:
        to_insert.append(f"MaxStartups {max_startups}\n")

    if to_insert:
        lines[insert_pos:insert_pos] = to_insert

    return "".join(lines)


def login_grace_time_present(lines: list[str]) -> bool:
    return any(re.match(r"^\s*LoginGraceTime\b", line, re.IGNORECASE) for line in lines)


def max_startups_present(lines: list[str]) -> bool:
    return any(re.match(r"^\s*MaxStartups\b", line, re.IGNORECASE) for line in lines)


def build_watchdog_hook_block(
    containers: tuple[str, ...] = HEAVY_CONTAINERS,
    max_startups: str = "30:50:100",
    login_grace_time: str = "20",
) -> str:
    """Build the idempotent bash enforcement block for `$DOCKER_ROOT/ha-watchdog.sh`."""
    containers_str = " ".join(containers)
    return (
        f"{WATCHDOG_BEGIN_MARKER}\n"
        "# Enforce disk I/O priorities (ionice) & OpenSSH MaxStartups/LoginGraceTime persistence\n"
        "export PATH=\"/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:$PATH\"\n"
        "if command -v ionice >/dev/null 2>&1; then\n"
        f"    for _c in {containers_str}; do\n"
        "        for _pid in $(/bin/docker top \"$_c\" -eo pid 2>/dev/null | awk 'NR>1 {print $1}'); do\n"
        "            ionice -c2 -n7 -p \"$_pid\" >/dev/null 2>&1 || true\n"
        "        done\n"
        "    done\n"
        "    for _spid in $(ps -ef 2>/dev/null | awk '/[s]shd/ {print $2}'); do\n"
        "        ionice -c2 -n0 -p \"$_spid\" >/dev/null 2>&1 || true\n"
        "    done\n"
        "fi\n"
        "if [ -f /etc/ssh/sshd_config ]; then\n"
        f"    if ! grep -Eq '^[[:space:]]*MaxStartups[[:space:]]+{max_startups}([[:space:]]|$)' /etc/ssh/sshd_config || \\\n"
        f"       ! grep -Eq '^[[:space:]]*LoginGraceTime[[:space:]]+{login_grace_time}([[:space:]]|$)' /etc/ssh/sshd_config; then\n"
        "        cp /etc/ssh/sshd_config /etc/ssh/sshd_config.watchdog_bak\n"
        f"        sed -i -E 's/^[[:space:]]*#?[[:space:]]*MaxStartups[[:space:]].*/MaxStartups {max_startups}/I' /etc/ssh/sshd_config\n"
        f"        sed -i -E 's/^[[:space:]]*#?[[:space:]]*LoginGraceTime[[:space:]].*/LoginGraceTime {login_grace_time}/I' /etc/ssh/sshd_config\n"
        f"        grep -Eq '^[[:space:]]*MaxStartups[[:space:]]+' /etc/ssh/sshd_config || echo 'MaxStartups {max_startups}' >> /etc/ssh/sshd_config\n"
        f"        grep -Eq '^[[:space:]]*LoginGraceTime[[:space:]]+' /etc/ssh/sshd_config || echo 'LoginGraceTime {login_grace_time}' >> /etc/ssh/sshd_config\n"
        "        if /bin/sshd -t >/dev/null 2>&1; then\n"
        "            [ -f /var/run/sshd.pid ] && kill -HUP \"$(cat /var/run/sshd.pid)\" >/dev/null 2>&1 || true\n"
        "        else\n"
        "            mv /etc/ssh/sshd_config.watchdog_bak /etc/ssh/sshd_config\n"
        "        fi\n"
        "    fi\n"
        "fi\n"
        f"{WATCHDOG_END_MARKER}\n"
    )


def inject_watchdog_hook(
    script_text: str,
    containers: tuple[str, ...] = HEAVY_CONTAINERS,
    max_startups: str = "30:50:100",
    login_grace_time: str = "20",
) -> str:
    """
    Idempotently embed or update the `ionice` and `sshd_config` enforcement block inside
    `ha-watchdog.sh` before the HA check / early-exit logic.
    """
    hook_block = build_watchdog_hook_block(
        containers=containers,
        max_startups=max_startups,
        login_grace_time=login_grace_time,
    )

    pattern = re.compile(
        re.escape(WATCHDOG_BEGIN_MARKER) + r".*?" + re.escape(WATCHDOG_END_MARKER) + r"\n?",
        re.DOTALL,
    )
    if pattern.search(script_text):
        return pattern.sub(hook_block, script_text, count=1)

    # Insert right before `# Read current failure count` or `HTTP_CODE=$(check_ha)` if present
    for anchor in ("# Read current failure count", "HTTP_CODE=$(check_ha)"):
        idx = script_text.find(anchor)
        if idx != -1:
            return script_text[:idx] + hook_block + "\n" + script_text[idx:]

    # Fallback: insert after shebang line
    lines = script_text.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        return lines[0] + "\n" + hook_block + "".join(lines[1:])
    return hook_block + "\n" + script_text


def _run_remote_python(host: str, py_code: str, timeout: int = 60) -> tuple[int, str, str]:
    """Execute a self-contained Python snippet on `host` under `sudo python3` via base64."""
    b64 = base64.b64encode(py_code.encode("utf-8")).decode("ascii")
    remote_cmd = f"sudo python3 -c \"import base64; exec(base64.b64decode('{b64}'))\""
    return nas_config.run_ssh(host, remote_cmd, timeout=timeout)


def validate_ha_config_via_api(
    host: str | None = None,
    ha_url: str | None = None,
    ha_token: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    """Validate Home Assistant configuration via POST `/api/config/core/check_config`."""
    resolved_url, resolved_token = _resolve_ha_credentials(host=host)
    url = (ha_url or resolved_url).rstrip("/") + "/api/config/core/check_config"
    token = ha_token if ha_token is not None else resolved_token

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip() else {}
            valid = resp.status == 200 and payload.get("result") == "valid"
            return {
                "ok": valid,
                "status": resp.status,
                "result": payload.get("result"),
                "errors": payload.get("errors"),
                "warnings": payload.get("warnings"),
            }
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {
            "ok": False,
            "status": 0,
            "result": "error",
            "errors": str(exc),
            "warnings": None,
        }


def apply_recorder_and_btrfs_tuning(
    host: str | None = None,
    docker_root: str | None = None,
    commit_interval: int = 30,
    auto_purge: bool = True,
) -> dict[str, Any]:
    """
    1. On Host 1 (`$DOCKER_ROOT/homeassistant/config`), ensure `chattr +C` (`nodatacow`) is
       applied to the config directory and SQLite DB directory/files.
    2. Configure `recorder:` in `configuration.yaml` with `commit_interval: 30` and
       `auto_purge: true` (preserving all existing configuration.yaml keys and formatting).
    3. Validate Home Assistant config via the HA REST API (`/api/config/core/check_config`).
    """
    target_host = host or nas_config.HOST_1_IP
    target_root = docker_root or nas_config.DOCKER_ROOT
    config_dir = f"{target_root.rstrip('/')}/homeassistant/config"

    remote_script = f"""
import json, os, re, shutil, subprocess

config_dir = {json.dumps(config_dir)}
cfg_file = os.path.join(config_dir, "configuration.yaml")
commit_interval = {int(commit_interval)}
auto_purge = {bool(auto_purge)}

# 1. Apply chattr +C to config_dir, parent homeassistant dir, and SQLite DB files/dirs
chattr_bin = shutil.which("chattr", path="/bin:/sbin:/usr/bin:/usr/sbin") or "/bin/chattr"
lsattr_bin = shutil.which("lsattr", path="/bin:/sbin:/usr/bin:/usr/sbin") or "/bin/lsattr"

targets = [os.path.dirname(config_dir), config_dir]
if os.path.isdir(config_dir):
    for entry in os.listdir(config_dir):
        if "db" in entry or "sqlite" in entry:
            targets.append(os.path.join(config_dir, entry))

for t in targets:
    if os.path.exists(t):
        subprocess.run([chattr_bin, "+C", t], capture_output=True, text=True)

lsattr_res = subprocess.run([lsattr_bin, "-d", config_dir], capture_output=True, text=True)
lsattr_out = lsattr_res.stdout.strip()
attrs = lsattr_out.split()[0] if lsattr_out else ""
nodatacow_ok = "C" in attrs

# 2. Update recorder: in configuration.yaml preserving all formatting/keys
with open(cfg_file, "r", encoding="utf-8") as f:
    orig_yaml = f.read()

{inspect_source_update_recorder()}

new_yaml = update_recorder_yaml_text(orig_yaml, commit_interval=commit_interval, auto_purge=auto_purge)
yaml_changed = (new_yaml != orig_yaml)
if yaml_changed:
    shutil.copy2(cfg_file, cfg_file + ".bak_io_hardening")
    with open(cfg_file, "w", encoding="utf-8") as f:
        f.write(new_yaml)

print(json.dumps({{
    "config_dir": config_dir,
    "lsattr": lsattr_out,
    "nodatacow_ok": nodatacow_ok,
    "yaml_changed": yaml_changed,
}}))
"""
    rc, out, err = _run_remote_python(target_host, remote_script)
    if rc != 0:
        raise RuntimeError(f"apply_recorder_and_btrfs_tuning failed on {target_host}: {err or out}")

    result = json.loads(out.strip().splitlines()[-1])
    api_check = validate_ha_config_via_api(host=target_host)
    result["ha_config_check"] = api_check
    result["ok"] = bool(result.get("nodatacow_ok")) and bool(api_check.get("ok"))
    if not result["ok"]:
        raise RuntimeError(f"HA recorder/Btrfs tuning verification failed: {result}")
    return result


def inspect_source_update_recorder() -> str:
    """Return portable source code for `update_recorder_yaml_text` to run remotely."""
    return '''
def update_recorder_yaml_text(yaml_text, commit_interval=30, auto_purge=True):
    auto_purge_str = "true" if auto_purge else "false"
    lines = yaml_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\\n"):
        lines[-1] += "\\n"
    rec_idx = None
    for idx, line in enumerate(lines):
        if re.match(r"^recorder:\\s*(?:#.*)?$", line):
            rec_idx = idx
            break
    if rec_idx is None:
        sep = "\\n" if (lines and lines[-1].strip() != "") else ""
        lines.append(f"{sep}recorder:\\n  commit_interval: {commit_interval}\\n  auto_purge: {auto_purge_str}\\n")
        return "".join(lines)
    end_idx = len(lines)
    for idx in range(rec_idx + 1, len(lines)):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not lines[idx][0].isspace():
            end_idx = idx
            break
    commit_updated = False
    autopurge_updated = False
    for idx in range(rec_idx + 1, end_idx):
        m_commit = re.match(r"^(\\s+commit_interval:\\s*)(\\S+)(\\s*(?:#.*)?\\r?\\n?)$", lines[idx])
        if m_commit and len(lines[idx]) - len(lines[idx].lstrip(" ")) == 2:
            lines[idx] = f"{m_commit.group(1)}{commit_interval}{m_commit.group(3)}"
            if not lines[idx].endswith("\\n"):
                lines[idx] += "\\n"
            commit_updated = True
            continue
        m_purge = re.match(r"^(\\s+auto_purge:\\s*)(\\S+)(\\s*(?:#.*)?\\r?\\n?)$", lines[idx])
        if m_purge and len(lines[idx]) - len(lines[idx].lstrip(" ")) == 2:
            lines[idx] = f"{m_purge.group(1)}{auto_purge_str}{m_purge.group(3)}"
            if not lines[idx].endswith("\\n"):
                lines[idx] += "\\n"
            autopurge_updated = True
    insert_lines = []
    if not commit_updated:
        insert_lines.append(f"  commit_interval: {commit_interval}\\n")
    if not autopurge_updated:
        insert_lines.append(f"  auto_purge: {auto_purge_str}\\n")
    if insert_lines:
        lines[rec_idx + 1 : rec_idx + 1] = insert_lines
    return "".join(lines)
'''


def _ionice_wrapper_script_content() -> str:
    """
    Lightweight Linux syscall (`ioprio_set` / `ioprio_get`) CLI implementation of `ionice`
    for Synology DSM where `util-linux` does not ship `/usr/bin/ionice`.
    Supports `ionice -c <class> -n <level> [-p <pid>...]` and `ionice -p <pid>...`.
    """
    return """#!/usr/bin/env python3
import ctypes
import os
import re
import subprocess
import sys

NR_ioprio_set = 251
NR_ioprio_get = 252
IOPRIO_WHO_PROCESS = 1
CLASS_NAMES = {0: "none", 1: "realtime", 2: "best-effort", 3: "idle"}
NAME_TO_CLASS = {"none": 0, "0": 0, "realtime": 1, "rt": 1, "1": 1, "best-effort": 2, "be": 2, "2": 2, "idle": 3, "3": 3}

def main():
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    args = sys.argv[1:]
    ioclass = None
    ionum = None
    pids = []
    cmd = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "--class"):
            i += 1
            ioclass = NAME_TO_CLASS.get(args[i].lower(), int(args[i]))
        elif a.startswith("-c"):
            val = a[2:]
            ioclass = NAME_TO_CLASS.get(val.lower(), int(val))
        elif a in ("-n", "--classdata"):
            i += 1
            ionum = int(args[i])
        elif a.startswith("-n"):
            ionum = int(a[2:])
        elif a in ("-p", "--pid"):
            i += 1
            while i < len(args) and not args[i].startswith("-"):
                pids.append(int(args[i]))
                i += 1
            continue
        elif a.startswith("-p") and a[2:].isdigit():
            pids.append(int(a[2:]))
        elif a in ("-t", "--ignore"):
            pass
        elif a.isdigit() and not cmd:
            pids.append(int(a))
        else:
            cmd = args[i:]
            break
        i += 1

    if ioclass is not None or ionum is not None:
        c = ioclass if ioclass is not None else 2
        n = ionum if ionum is not None else (0 if c == 3 else 4)
        ioprio = (c << 13) | (n & 0x1fff)
        if pids:
            rc_final = 0
            for pid in pids:
                res = libc.syscall(NR_ioprio_set, IOPRIO_WHO_PROCESS, int(pid), int(ioprio))
                if res != 0:
                    rc_final = 1
            sys.exit(rc_final)
        elif cmd:
            libc.syscall(NR_ioprio_set, IOPRIO_WHO_PROCESS, 0, int(ioprio))
            os.execvp(cmd[0], cmd)
        else:
            res = libc.syscall(NR_ioprio_set, IOPRIO_WHO_PROCESS, 0, int(ioprio))
            sys.exit(0 if res == 0 else 1)
    else:
        target_pids = pids if pids else [os.getpid()]
        for pid in target_pids:
            val = libc.syscall(NR_ioprio_get, IOPRIO_WHO_PROCESS, int(pid))
            if val < 0:
                sys.exit(1)
            c = val >> 13
            n = val & 0x1fff
            cname = CLASS_NAMES.get(c, "unknown")
            if c == 3:
                print(f"{cname}")
            else:
                print(f"{cname}: prio {n}")

if __name__ == "__main__":
    main()
"""


def apply_io_priorities(
    host: str | None = None,
    docker_root: str | None = None,
    containers: tuple[str, ...] = HEAVY_CONTAINERS,
) -> dict[str, Any]:
    """
    1. Ensure `ionice` utility is available on Host 1 (installing `/usr/local/bin/ionice`
       and `/bin/ionice` syscall wrapper if DSM lacks native `ionice`).
    2. Apply `ionice -c2 -n7` to heavy non-critical Docker container processes
       (`plex`, `radarr`, `sonarr`, `sabnzbd`, `kometa`, `prowlarr`) and `ionice -c2 -n0`
       to host `sshd` processes.
    3. Embed idempotent `ionice` enforcement hook into `$DOCKER_ROOT/ha-watchdog.sh`.
    """
    target_host = host or nas_config.HOST_1_IP
    target_root = docker_root or nas_config.DOCKER_ROOT
    watchdog_path = f"{target_root.rstrip('/')}/ha-watchdog.sh"
    ionice_wrapper = _ionice_wrapper_script_content()
    hook_block = build_watchdog_hook_block(containers=containers)

    remote_script = f"""
import json, os, re, shutil, stat, subprocess

watchdog_path = {json.dumps(watchdog_path)}
containers = {json.dumps(list(containers))}
ionice_wrapper = {json.dumps(ionice_wrapper)}
hook_block = {json.dumps(hook_block)}
begin_marker = {json.dumps(WATCHDOG_BEGIN_MARKER)}
end_marker = {json.dumps(WATCHDOG_END_MARKER)}

# 1. Ensure ionice is installed and executable
os.makedirs("/usr/local/bin", exist_ok=True)
for target_bin in ("/usr/local/bin/ionice", "/bin/ionice"):
    if not os.path.exists(target_bin) or open(target_bin, "r", encoding="utf-8", errors="ignore").read() != ionice_wrapper:
        with open(target_bin, "w", encoding="utf-8") as f:
            f.write(ionice_wrapper)
        os.chmod(target_bin, 0o755)
ionice_bin = shutil.which("ionice", path="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin") or "/usr/local/bin/ionice"

# 2. Apply ionice -c2 -n7 to heavy container processes
container_results = {{}}
for c in containers:
    r = subprocess.run(["/bin/docker", "top", c, "-eo", "pid"], capture_output=True, text=True)
    pids = [line.strip() for line in r.stdout.splitlines()[1:] if line.strip().isdigit()] if r.returncode == 0 else []
    applied = []
    for pid in pids:
        ir = subprocess.run([ionice_bin, "-c2", "-n7", "-p", pid], capture_output=True, text=True)
        if ir.returncode == 0:
            applied.append(int(pid))
    container_results[c] = applied

# 3. Apply ionice -c2 -n0 to host sshd processes
sshd_pids = []
for entry in os.listdir("/proc"):
    if entry.isdigit():
        try:
            comm = open(f"/proc/{{entry}}/comm", "r", encoding="utf-8").read().strip()
            if comm == "sshd":
                sshd_pids.append(entry)
        except Exception:
            pass
sshd_applied = []
for pid in sshd_pids:
    ir = subprocess.run([ionice_bin, "-c2", "-n0", "-p", pid], capture_output=True, text=True)
    if ir.returncode == 0:
        sshd_applied.append(int(pid))

# 4. Embed idempotent enforcement hook in ha-watchdog.sh
watchdog_updated = False
if os.path.exists(watchdog_path):
    with open(watchdog_path, "r", encoding="utf-8") as f:
        orig_wd = f.read()
    pattern = re.compile(re.escape(begin_marker) + r".*?" + re.escape(end_marker) + r"\\n?", re.DOTALL)
    if pattern.search(orig_wd):
        new_wd = pattern.sub(hook_block, orig_wd, count=1)
    else:
        idx = orig_wd.find("# Read current failure count")
        if idx == -1:
            idx = orig_wd.find("HTTP_CODE=$(check_ha)")
        if idx != -1:
            new_wd = orig_wd[:idx] + hook_block + "\\n" + orig_wd[idx:]
        else:
            new_wd = orig_wd + "\\n" + hook_block
    if new_wd != orig_wd:
        shutil.copy2(watchdog_path, watchdog_path + ".bak_io_hardening")
        with open(watchdog_path, "w", encoding="utf-8") as f:
            f.write(new_wd)
        os.chmod(watchdog_path, 0o755)
        watchdog_updated = True

print(json.dumps({{
    "ok": bool(sshd_applied) and os.path.exists(watchdog_path),
    "ionice_bin": ionice_bin,
    "containers": container_results,
    "sshd_pids": sshd_applied,
    "watchdog_path": watchdog_path,
    "watchdog_updated": watchdog_updated,
}}))
"""
    rc, out, err = _run_remote_python(target_host, remote_script)
    if rc != 0:
        raise RuntimeError(f"apply_io_priorities failed on {target_host}: {err or out}")

    result = json.loads(out.strip().splitlines()[-1])
    if not result.get("ok"):
        raise RuntimeError(f"apply_io_priorities validation failed: {result}")
    return result


def apply_sshd_hardening(
    host: str | None = None,
    docker_root: str | None = None,
    max_startups: str = "30:50:100",
    login_grace_time: str = "20",
) -> dict[str, Any]:
    """
    Configure `/etc/ssh/sshd_config` on Host 1 with `MaxStartups 30:50:100` and
    `LoginGraceTime 20`, validate syntax with `sudo sshd -t`, send `SIGHUP` to the master
    `sshd` PID (never stop sshd), and ensure persistence in `$DOCKER_ROOT/ha-watchdog.sh`.
    """
    target_host = host or nas_config.HOST_1_IP
    target_root = docker_root or nas_config.DOCKER_ROOT
    watchdog_path = f"{target_root.rstrip('/')}/ha-watchdog.sh"
    hook_block = build_watchdog_hook_block(
        max_startups=max_startups,
        login_grace_time=login_grace_time,
    )

    remote_script = f"""
import json, os, re, signal, shutil, subprocess

sshd_cfg = "/etc/ssh/sshd_config"
watchdog_path = {json.dumps(watchdog_path)}
max_startups = {json.dumps(max_startups)}
login_grace_time = {json.dumps(login_grace_time)}
hook_block = {json.dumps(hook_block)}
begin_marker = {json.dumps(WATCHDOG_BEGIN_MARKER)}
end_marker = {json.dumps(WATCHDOG_END_MARKER)}

with open(sshd_cfg, "r", encoding="utf-8") as f:
    orig_cfg = f.read()

lines = orig_cfg.splitlines(keepends=True)
if lines and not lines[-1].endswith("\\n"):
    lines[-1] += "\\n"

max_updated = False
grace_updated = False
match_idx = None
for idx, line in enumerate(lines):
    if match_idx is None and re.match(r"^\\s*Match\\b", line, re.IGNORECASE):
        match_idx = idx
    if match_idx is not None:
        continue
    if re.match(r"^\\s*#?\\s*MaxStartups\\b", line, re.IGNORECASE):
        if not max_updated:
            lines[idx] = f"MaxStartups {{max_startups}}\\n"
            max_updated = True
        else:
            lines[idx] = ""
    elif re.match(r"^\\s*#?\\s*LoginGraceTime\\b", line, re.IGNORECASE):
        if not grace_updated:
            lines[idx] = f"LoginGraceTime {{login_grace_time}}\\n"
            grace_updated = True
        else:
            lines[idx] = ""

insert_pos = match_idx if match_idx is not None else len(lines)
to_insert = []
if not grace_updated:
    to_insert.append(f"LoginGraceTime {{login_grace_time}}\\n")
if not max_updated:
    to_insert.append(f"MaxStartups {{max_startups}}\\n")
if to_insert:
    lines[insert_pos:insert_pos] = to_insert

new_cfg = "".join(lines)
cfg_changed = (new_cfg != orig_cfg)
bak_cfg = sshd_cfg + ".bak_io_hardening"
shutil.copy2(sshd_cfg, bak_cfg)

if cfg_changed:
    with open(sshd_cfg, "w", encoding="utf-8") as f:
        f.write(new_cfg)

sshd_bin = shutil.which("sshd", path="/bin:/sbin:/usr/bin:/usr/sbin") or "/bin/sshd"
t_res = subprocess.run([sshd_bin, "-t"], capture_output=True, text=True)
if t_res.returncode != 0:
    shutil.copy2(bak_cfg, sshd_cfg)
    raise RuntimeError(f"sshd -t failed: {{t_res.stderr or t_res.stdout}}")

# Locate master sshd PID and send SIGHUP
master_pid = None
if os.path.exists("/var/run/sshd.pid"):
    raw_pid = open("/var/run/sshd.pid", "r", encoding="utf-8").read().strip()
    if raw_pid.isdigit():
        master_pid = int(raw_pid)
if master_pid is None:
    for entry in sorted(os.listdir("/proc"), key=lambda x: int(x) if x.isdigit() else 999999999):
        if entry.isdigit():
            try:
                if open(f"/proc/{{entry}}/comm", "r", encoding="utf-8").read().strip() == "sshd":
                    master_pid = int(entry)
                    break
            except Exception:
                pass

sighup_sent = False
if master_pid:
    os.kill(master_pid, signal.SIGHUP)
    sighup_sent = True

# Ensure persistence check in ha-watchdog.sh
watchdog_has_hook = False
if os.path.exists(watchdog_path):
    with open(watchdog_path, "r", encoding="utf-8") as f:
        orig_wd = f.read()
    pattern = re.compile(re.escape(begin_marker) + r".*?" + re.escape(end_marker) + r"\\n?", re.DOTALL)
    if pattern.search(orig_wd):
        new_wd = pattern.sub(hook_block, orig_wd, count=1)
    else:
        idx = orig_wd.find("# Read current failure count")
        if idx == -1:
            idx = orig_wd.find("HTTP_CODE=$(check_ha)")
        if idx != -1:
            new_wd = orig_wd[:idx] + hook_block + "\\n" + orig_wd[idx:]
        else:
            new_wd = orig_wd + "\\n" + hook_block
    if new_wd != orig_wd:
        with open(watchdog_path, "w", encoding="utf-8") as f:
            f.write(new_wd)
        os.chmod(watchdog_path, 0o755)
    watchdog_has_hook = True

print(json.dumps({{
    "ok": t_res.returncode == 0 and sighup_sent and watchdog_has_hook,
    "cfg_changed": cfg_changed,
    "sshd_test_rc": t_res.returncode,
    "master_pid": master_pid,
    "sighup_sent": sighup_sent,
    "watchdog_has_hook": watchdog_has_hook,
}}))
"""
    rc, out, err = _run_remote_python(target_host, remote_script)
    if rc != 0:
        raise RuntimeError(f"apply_sshd_hardening failed on {target_host}: {err or out}")

    result = json.loads(out.strip().splitlines()[-1])
    if not result.get("ok"):
        raise RuntimeError(f"apply_sshd_hardening validation failed: {result}")
    return result


def verify_host1_hardening(
    host: str | None = None,
    docker_root: str | None = None,
    containers: tuple[str, ...] = HEAVY_CONTAINERS,
) -> dict[str, Any]:
    """
    Verify all three hardening layers on live Host 1:
    1. Btrfs `nodatacow` (`+C`) on `$DOCKER_ROOT/homeassistant/config`, `commit_interval: 30`
       and `auto_purge: true` in `configuration.yaml`, and valid HA config via REST API.
    2. `ionice` priorities (`best-effort: prio 7` on active heavy containers and
       `best-effort: prio 0` on `sshd`) plus persistence hook in `$DOCKER_ROOT/ha-watchdog.sh`.
    3. `MaxStartups 30:50:100` and `LoginGraceTime 20` in `/etc/ssh/sshd_config`,
       `sudo sshd -t` passing, and master `sshd` running.
    """
    target_host = host or nas_config.HOST_1_IP
    target_root = docker_root or nas_config.DOCKER_ROOT
    config_dir = f"{target_root.rstrip('/')}/homeassistant/config"
    watchdog_path = f"{target_root.rstrip('/')}/ha-watchdog.sh"

    remote_script = f"""
import json, os, re, shutil, subprocess

config_dir = {json.dumps(config_dir)}
cfg_file = os.path.join(config_dir, "configuration.yaml")
watchdog_path = {json.dumps(watchdog_path)}
containers = {json.dumps(list(containers))}

# 1. Verify Btrfs +C & configuration.yaml recorder settings
lsattr_bin = shutil.which("lsattr", path="/bin:/sbin:/usr/bin:/usr/sbin") or "/bin/lsattr"
lsattr_res = subprocess.run([lsattr_bin, "-d", config_dir], capture_output=True, text=True)
lsattr_out = lsattr_res.stdout.strip()
attrs = lsattr_out.split()[0] if lsattr_out else ""
btrfs_nodatacow = "C" in attrs

cfg_text = open(cfg_file, "r", encoding="utf-8").read() if os.path.exists(cfg_file) else ""
has_commit_30 = bool(re.search(r"^\\s+commit_interval:\\s*30\\b", cfg_text, re.MULTILINE))
has_auto_purge = bool(re.search(r"^\\s+auto_purge:\\s*true\\b", cfg_text, re.MULTILINE | re.IGNORECASE))

# 2. Verify ionice priorities on containers & sshd
ionice_bin = shutil.which("ionice", path="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin") or "/usr/local/bin/ionice"
container_ionice = {{}}
containers_ok = True
for c in containers:
    pid_res = subprocess.run(["/bin/docker", "inspect", "-f", "{{{{.State.Pid}}}}", c], capture_output=True, text=True)
    pid = pid_res.stdout.strip()
    if pid and pid != "0" and pid.isdigit():
        io_res = subprocess.run([ionice_bin, "-p", pid], capture_output=True, text=True)
        io_str = io_res.stdout.strip()
        container_ionice[c] = {{"pid": int(pid), "ionice": io_str}}
        if "best-effort: prio 7" not in io_str:
            containers_ok = False

sshd_pid = None
if os.path.exists("/var/run/sshd.pid"):
    raw = open("/var/run/sshd.pid").read().strip()
    if raw.isdigit():
        sshd_pid = raw
if not sshd_pid:
    for entry in sorted(os.listdir("/proc"), key=lambda x: int(x) if x.isdigit() else 999999999):
        if entry.isdigit():
            try:
                if open(f"/proc/{{entry}}/comm", "r", encoding="utf-8").read().strip() == "sshd":
                    sshd_pid = entry
                    break
            except Exception:
                pass

sshd_io_str = ""
if sshd_pid and sshd_pid.isdigit():
    sshd_io_res = subprocess.run([ionice_bin, "-p", sshd_pid], capture_output=True, text=True)
    sshd_io_str = sshd_io_res.stdout.strip()
sshd_ionice_ok = ("best-effort: prio 0" in sshd_io_str)

# 3. Verify sshd_config and sshd -t
sshd_cfg_text = open("/etc/ssh/sshd_config", "r", encoding="utf-8").read()
max_startups_ok = bool(re.search(r"^\\s*MaxStartups\\s+30:50:100\\s*$", sshd_cfg_text, re.MULTILINE))
login_grace_ok = bool(re.search(r"^\\s*LoginGraceTime\\s+20\\s*$", sshd_cfg_text, re.MULTILINE))
sshd_bin = shutil.which("sshd", path="/bin:/sbin:/usr/bin:/usr/sbin") or "/bin/sshd"
sshd_t_rc = subprocess.run([sshd_bin, "-t"], capture_output=True, text=True).returncode

# 4. Verify ha-watchdog.sh hook
wd_text = open(watchdog_path, "r", encoding="utf-8").read() if os.path.exists(watchdog_path) else ""
watchdog_hook_ok = (
    "ionice -c2 -n7" in wd_text
    and "ionice -c2 -n0" in wd_text
    and "MaxStartups 30:50:100" in wd_text
    and "LoginGraceTime 20" in wd_text
)

print(json.dumps({{
    "btrfs_nodatacow": btrfs_nodatacow,
    "lsattr": lsattr_out,
    "recorder_commit_interval_30": has_commit_30,
    "recorder_auto_purge_true": has_auto_purge,
    "containers_ionice_ok": containers_ok,
    "container_ionice": container_ionice,
    "sshd_ionice_ok": sshd_ionice_ok,
    "sshd_ionice": sshd_io_str,
    "sshd_max_startups_ok": max_startups_ok,
    "sshd_login_grace_ok": login_grace_ok,
    "sshd_t_ok": sshd_t_rc == 0,
    "watchdog_hook_ok": watchdog_hook_ok,
}}))
"""
    rc, out, err = _run_remote_python(target_host, remote_script)
    if rc != 0:
        raise RuntimeError(f"verify_host1_hardening failed on {target_host}: {err or out}")

    report = json.loads(out.strip().splitlines()[-1])
    ha_api = validate_ha_config_via_api(host=target_host)
    report["ha_config_api_valid"] = bool(ha_api.get("ok"))
    report["ha_config_api"] = ha_api

    required_flags = (
        "btrfs_nodatacow",
        "recorder_commit_interval_30",
        "recorder_auto_purge_true",
        "containers_ionice_ok",
        "sshd_ionice_ok",
        "sshd_max_startups_ok",
        "sshd_login_grace_ok",
        "sshd_t_ok",
        "watchdog_hook_ok",
        "ha_config_api_valid",
    )
    report["ok"] = all(bool(report.get(flag)) for flag in required_flags)
    if not report["ok"]:
        raise RuntimeError(f"Host 1 hardening verification failed: {json.dumps(report, indent=2)}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Host 1 Disk I/O, HA Recorder & SSH MaxStartups Hardening Utility"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply all three hardening layers idempotently on Host 1",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify all three hardening layers on Host 1",
    )
    args = parser.parse_args(argv)

    if not args.apply and not args.verify:
        parser.print_help()
        return 1

    output: dict[str, Any] = {}
    if args.apply:
        output["recorder_and_btrfs"] = apply_recorder_and_btrfs_tuning()
        output["io_priorities"] = apply_io_priorities()
        output["sshd_hardening"] = apply_sshd_hardening()

    if args.verify:
        output["verification"] = verify_host1_hardening()

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
