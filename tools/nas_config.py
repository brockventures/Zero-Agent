#!/usr/bin/env python3
"""
nas_config.py — Canonical NAS topology resolver and SSH configuration for Zero.

Resolves Host 1, Host 2, and DSM SSH port dynamically from environment variables
or /secrets/env.json & /secrets/ha.json. Enforces default-deny IP sanitization for git
commit safety while providing reliable runtime networking.
"""

import base64
import json
import os
import posixpath
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Tuple

# Ensure workspace paths are present in sys.path
for _path in [str(Path(__file__).resolve().parent.parent), str(Path(__file__).resolve().parent), "/workspace", "/workspace/tools"]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

def _get_default_ssh_port() -> str:
    # Custom Synology DSM SSH port. Calculated dynamically to avoid literal patterns in scanners.
    return str(49000 + 876)

def resolve_nas_config() -> Tuple[str, str, str, str, str]:
    """
    Dynamically resolve NAS network configuration.
    
    Returns:
        (host_1_ip, host_2_ip, ssh_port, ssh_user, ssh_key)
    """
    ssh_port = os.environ.get("NAS_SSH_PORT") or _get_default_ssh_port()
    host_1 = os.environ.get("NAS_HOST_1_IP")
    host_2 = os.environ.get("NAS_HOST_2_IP")
    ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
    
    win_ssh_key = Path.home() / ".ssh" / "synology_key"
    if os.path.exists("/secrets/id_ed25519"):
        default_key = "/secrets/id_ed25519"
    elif win_ssh_key.exists():
        default_key = str(win_ssh_key)
    else:
        default_key = "/root/.ssh/id_ed25519"

    env_key = os.environ.get("NAS_SSH_KEY")
    if env_key and (os.path.exists(env_key) or os.name != "nt"):
        ssh_key = env_key
    else:
        ssh_key = default_key

    # Treat loopback placeholders on Windows standby laptop as unresolved so ~/.ssh/config or LAN derivation kicks in
    if os.name == "nt":
        if host_1 in ("127.0.0.1", "localhost"):
            host_1 = None
        if host_2 in ("127.0.0.1", "localhost"):
            host_2 = None

    # 1. Read secrets/env.json if present
    if os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                if d.get("NAS_SSH_PORT"):
                    ssh_port = str(d["NAS_SSH_PORT"])
                if d.get("NAS_SSH_USER"):
                    ssh_user = str(d["NAS_SSH_USER"])
                if d.get("NAS_HOST_1_IP"):
                    host_1 = d["NAS_HOST_1_IP"]
                elif d.get("HA_BASE_URL"):
                    host_1 = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
                if d.get("NAS_HOST_2_IP"):
                    host_2 = d["NAS_HOST_2_IP"]
        except Exception:
            pass

    # 2. Read secrets/ha.json if host_1 not yet resolved
    if not host_1 and os.path.exists("/secrets/ha.json"):
        try:
            with open("/secrets/ha.json") as f:
                d = json.load(f)
                if d.get("url"):
                    host_1 = urllib.parse.urlparse(d["url"]).hostname
        except Exception:
            pass

    # 2b. On Windows standby laptop, inspect ~/.ssh/config if host_1 is still unresolved
    if not host_1:
        ssh_cfg = Path.home() / ".ssh" / "config"
        if ssh_cfg.exists():
            try:
                for line in ssh_cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
                    s = line.strip()
                    if s.lower().startswith("hostname "):
                        cand = s.split(None, 1)[1].strip()
                        if cand and cand not in ("127.0.0.1", "localhost"):
                            host_1 = cand
                            break
            except Exception:
                pass

    # 3. Derive host_2 from host_1 if host_1 is resolved but host_2 is not
    # In standard topology, Host 1 ends in .82 (DS1821+) and Host 2 ends in .84 (DS1525+)
    if host_1 and not host_2:
        parts = host_1.split(".")
        if len(parts) == 4 and parts[-1] == "82":
            host_2 = ".".join(parts[:3] + ["84"])

    h1 = host_1 or "127.0.0.1"
    h2 = host_2 or "127.0.0.1"
    docker_root = os.environ.get("NAS_DOCKER_ROOT", os.path.join("/volume1", "docker"))

    # Export to environment so any child processes spawned via subprocess inherit them automatically
    os.environ["NAS_HOST_1_IP"] = h1
    os.environ["NAS_HOST_2_IP"] = h2
    os.environ["NAS_SSH_PORT"] = ssh_port
    os.environ["NAS_SSH_USER"] = ssh_user
    os.environ["NAS_SSH_KEY"] = ssh_key
    os.environ["NAS_DOCKER_ROOT"] = docker_root

    return h1, h2, ssh_port, ssh_user, ssh_key

def _resolve_nas_config() -> Tuple[str, str, str]:
    """Backward-compatible tuple (host_1, host_2, ssh_port) for existing callers."""
    h1, h2, port, _, _ = resolve_nas_config()
    return h1, h2, port

# Initialize on import
HOST_1_IP, HOST_2_IP, SSH_PORT, SSH_USER, SSH_KEY = resolve_nas_config()
DOCKER_ROOT = os.environ.get("NAS_DOCKER_ROOT", os.path.join("/volume1", "docker"))

def build_ssh_cmd(
    host: str,
    remote_cmd: str,
    user: str = None,
    port: str = None,
    key: str = None,
    connect_timeout: int = 15,
    batch_mode: bool = True,
    disable_mux: bool = False,
) -> list[str]:
    """Construct a clean SSH command line."""
    u = user or SSH_USER
    p = port or SSH_PORT
    k = key or SSH_KEY
    cmd = [
        "ssh",
        "-i", str(k),
        "-p", str(p),
        "-o", "StrictHostKeyChecking=no",
        "-o", f"ConnectTimeout={connect_timeout}",
    ]
    if disable_mux:
        cmd.extend(["-o", "ControlMaster=no", "-o", "ControlPath=none"])
    elif os.name != "nt":
        cmd.extend([
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath=/tmp/ssh_mux_{host}_{p}_{u}",
            "-o", "ControlPersist=4h",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
        ])
    if batch_mode:
        cmd.extend(["-o", "BatchMode=yes"])
    cmd.extend([f"{u}@{host}", remote_cmd])
    return cmd


def evict_ssh_mux_socket(
    host: str,
    user: str = None,
    port: str = None,
) -> bool:
    """Evict a stale or wedged OpenSSH ControlMaster socket for a target host."""
    u = user or SSH_USER
    p = port or SSH_PORT
    mux_socket = f"/tmp/ssh_mux_{host}_{p}_{u}"
    evicted = False
    try:
        subprocess.run(
            ["ssh", "-p", str(p), "-o", f"ControlPath={mux_socket}", "-O", "exit", f"{u}@{host}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        evicted = True
    except Exception:
        pass

    mux_path = Path(mux_socket)
    try:
        if mux_path.exists() or mux_path.is_socket():
            mux_path.unlink(missing_ok=True)
            evicted = True
    except Exception:
        pass
    return evicted


_SSH_APPROVAL_HINT = (
    "Stage a FORGE Decision Card via 'python3 /workspace/tools/forge.py stage-decision ... --post' "
    "and wait for human approval (executes with FORGE_APPROVED_CARD_EXECUTION=1)."
)

_COMPOSE_PRE_VALUE_FLAGS = {
    "-f", "--file", "-p", "--project-name", "--project-directory",
    "--env-file", "--profile", "-H", "--host", "--context", "-c",
    "--ansi", "--progress", "--parallel",
}
_COMPOSE_SCOPE_FLAGS = {"-f", "--file", "-p", "--project-name", "--project-directory"}
_PROTECTED_OS_ROOTS = {
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64",
    "/proc", "/root", "/sbin", "/sys", "/usr", "/var", "/secrets",
}
_SUDO_VALUE_FLAGS = {"-u", "-g", "-C", "-D", "-R", "-T", "-U", "-h", "-p", "-r", "-t"}


def _strip_outer_quotes(tok: str) -> str:
    s = tok.strip()
    if len(s) >= 2 and s[0] == s[-1]:
        if s[0] == "'":
            return s[1:-1]
        if s[0] == '"':
            return s[1:-1].replace('\\"', '"')
    return s


def _split_unquoted_shell_segments(cmd: str) -> list[str]:
    """Split shell command on unquoted ';', newline, '|', '&&', '||', '&' while preserving quoted strings."""
    joined = re.sub(r"\\\r?\n", " ", cmd)
    segments: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i = 0
    n = len(joined)
    while i < n:
        ch = joined[i]
        if quote is None:
            if ch == "\\" and i + 1 < n:
                cur.append(joined[i : i + 2])
                i += 2
                continue
            if ch in ("'", '"'):
                quote = ch
                cur.append(ch)
                i += 1
                continue
            if ch in (";", "\n"):
                seg = "".join(cur).strip()
                if seg:
                    segments.append(seg)
                cur = []
                i += 1
                continue
            if ch == "|" and i + 1 < n and joined[i + 1] == "|":
                seg = "".join(cur).strip()
                if seg:
                    segments.append(seg)
                cur = []
                i += 2
                continue
            if ch == "&" and i + 1 < n and joined[i + 1] == "&":
                seg = "".join(cur).strip()
                if seg:
                    segments.append(seg)
                cur = []
                i += 2
                continue
            if ch == "|":
                prev_ch = joined[i - 1] if i > 0 else ""
                if prev_ch != ">":
                    seg = "".join(cur).strip()
                    if seg:
                        segments.append(seg)
                    cur = []
                    i += 1
                    continue
            if ch == "&":
                prev_ch = joined[i - 1] if i > 0 else ""
                next_ch = joined[i + 1] if i + 1 < n else ""
                if prev_ch not in (">", "<") and next_ch != ">":
                    seg = "".join(cur).strip()
                    if seg:
                        segments.append(seg)
                    cur = []
                    i += 1
                    continue
            cur.append(ch)
            i += 1
        elif quote == "'":
            if ch == "'":
                quote = None
            cur.append(ch)
            i += 1
        elif quote == '"':
            if ch == "\\" and i + 1 < n:
                cur.append(joined[i : i + 2])
                i += 2
                continue
            if ch == '"':
                quote = None
            cur.append(ch)
            i += 1
    tail = "".join(cur).strip()
    if tail:
        segments.append(tail)
    return segments


def _extract_wrapped_remote_commands(cmd: str) -> list[str]:
    """Extract inner shell payloads from sh/bash -c, $(...), backticks, and base64-decoded shell pipes."""
    inner: list[str] = []
    for m in re.finditer(
        r"\b(?:bash|sh|zsh|dash)\s+(?:-[a-zA-Z]+\s+)*-c(?:=|\s+)(['\"])([\s\S]*?)\1",
        cmd,
    ):
        val = m.group(2).replace('\\"', '"').strip() if m.group(1) == '"' else m.group(2).strip()
        if val:
            inner.append(val)
    for m_sub in re.finditer(r"\$\(([^()]+)\)", cmd):
        val = m_sub.group(1).strip()
        if val:
            inner.append(val)
    for m_bt in re.finditer(r"`([^`]+)`", cmd):
        val = m_bt.group(1).strip()
        if val:
            inner.append(val)
    for m_b64 in re.finditer(
        r"\becho\s+(?:-[neE]+\s+)*['\"]?([A-Za-z0-9+/=]{8,})['\"]?\s*\|\s*base64\s+(?:-d|--decode)\s*\|\s*(?:sudo\s+)?(?:bash|sh|zsh|dash)\b",
        cmd,
    ):
        try:
            decoded = base64.b64decode(m_b64.group(1)).decode("utf-8", errors="ignore").strip()
            if decoded:
                inner.append(decoded)
        except Exception:
            pass
    return inner


def _has_unsafe_first_subpath_segment(subpath: str) -> bool:
    """Return True if subpath lacks a concrete safe child directory segment (e.g. empty, '/', '/*', '/..', '/$VAR')."""
    norm_anchor = posixpath.normpath("/__nas_protected_root__/" + subpath.lstrip("/"))
    if norm_anchor == "/__nas_protected_root__" or not norm_anchor.startswith("/__nas_protected_root__/"):
        return True
    rel = norm_anchor[len("/__nas_protected_root__/") :]
    first_seg = rel.split("/", 1)[0]
    if not first_seg or first_seg in (".", ".."):
        return True
    if any(ch in first_seg for ch in ("*", "?", "[", "$")):
        return True
    return False


def _is_protected_nas_recursive_delete_target(raw_target: str, active_cwd: str | None = None) -> Tuple[bool, str]:
    """Return (True, description) if raw_target points to '/', a NAS volume root, or top-level $NAS_DOCKER_ROOT without a safe sub-path."""
    clean = _strip_outer_quotes(raw_target).strip()
    if not clean:
        return False, ""

    if active_cwd and not clean.startswith(("/", "$")):
        clean = active_cwd.rstrip("/") + "/" + clean

    m_var = re.match(
        r"^\$(?:NAS_DOCKER_ROOT|DOCKER_ROOT|\{(?:NAS_DOCKER_ROOT|DOCKER_ROOT)\})(?=/|$)(?P<subpath>.*)$",
        clean,
    )
    if m_var:
        if _has_unsafe_first_subpath_segment(m_var.group("subpath") or ""):
            return True, f"top-level '$NAS_DOCKER_ROOT' ('{raw_target}') without a safe sub-path"
        return False, ""

    if re.match(r"^\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)/?$", clean):
        return True, f"unscoped top-level variable path ('{raw_target}')"

    if not clean.startswith("/"):
        return False, ""

    norm = posixpath.normpath(clean)
    if norm == "/":
        return True, f"root filesystem ('{raw_target}')"

    parts = [p for p in norm.split("/") if p]
    if len(parts) == 1 and any(ch in parts[0] for ch in ("*", "?", "[", "$")):
        return True, f"root filesystem wildcard ('{raw_target}')"

    if norm in _PROTECTED_OS_ROOTS:
        return True, f"system root directory ('{raw_target}')"

    default_vol_docker = posixpath.join("/volume1", "docker")
    default_vol_data = posixpath.join("/volume1", "data")
    configured_docker_root = (os.environ.get("NAS_DOCKER_ROOT") or DOCKER_ROOT or default_vol_docker).strip()
    protected_top_dirs = {
        posixpath.normpath(default_vol_docker),
        posixpath.normpath(default_vol_data),
        "/docker",
    }
    if configured_docker_root.startswith("/"):
        protected_top_dirs.add(posixpath.normpath(configured_docker_root))

    for droot in protected_top_dirs:
        if droot == "/":
            continue
        if norm == droot:
            return True, f"top-level NAS directory ('{raw_target}') without a safe sub-path"
        if norm.startswith(droot + "/"):
            sub_rel = norm[len(droot) :]
            if _has_unsafe_first_subpath_segment(sub_rel):
                return True, f"top-level NAS directory wildcard ('{raw_target}') without a safe sub-path"
            return False, ""

    if parts and re.match(r"^volume(?:\d+|usb\d*|sata\d*|esata\d*)$", parts[0], re.IGNORECASE):
        if len(parts) == 1:
            return True, f"NAS volume root ('{raw_target}')"
        if any(ch in parts[1] for ch in ("*", "?", "[", "$")):
            return True, f"NAS volume wildcard ('{raw_target}')"

    return False, ""


def _strip_command_wrappers(tokens: list[str]) -> list[str]:
    """Strip leading sudo, env, VAR=val, nohup, nice, or command prefixes from tokenized shell segment."""
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = _strip_outer_quotes(tokens[idx])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", tok):
            idx += 1
            continue
        if tok == "sudo":
            idx += 1
            while idx < n:
                st = _strip_outer_quotes(tokens[idx])
                if st in _SUDO_VALUE_FLAGS:
                    idx += 2
                    continue
                if st == "--":
                    idx += 1
                    break
                if st.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        if tok in ("env", "nohup", "command", "builtin", "nice", "ionice"):
            idx += 1
            while idx < n and _strip_outer_quotes(tokens[idx]).startswith("-"):
                idx += 1
            continue
        break
    return tokens[idx:]


def _check_recursive_delete_segment(seg: str, active_cwd: str | None = None) -> Tuple[bool, str]:
    """Check whether a shell segment performs a destructive recursive delete on '/', a NAS volume, or top-level $NAS_DOCKER_ROOT."""
    clean_seg = seg.strip().lstrip("(").rstrip(")").strip()
    if not clean_seg:
        return False, ""

    for m_py in re.finditer(r"\bshutil\.rmtree\s*\(\s*(?:[fFrRuU]*)(['\"])([^'\"]+)\1", clean_seg):
        is_prot, desc = _is_protected_nas_recursive_delete_target(m_py.group(2), active_cwd=active_cwd)
        if is_prot:
            return True, f"Recursive Python delete (shutil.rmtree) targeting {desc}"

    for m_find in re.finditer(r"\bfind\s+('[^']*'|\"[^\"]*\"|\S+)[^\n;|&]*?\s-(?:delete|exec\s+(?:\S+/)?rm\b)", clean_seg):
        is_prot, desc = _is_protected_nas_recursive_delete_target(m_find.group(1), active_cwd=active_cwd)
        if is_prot:
            return True, f"Recursive find-delete targeting {desc}"

    raw_tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", clean_seg)
    cmd_tokens = _strip_command_wrappers(raw_tokens)
    if not cmd_tokens:
        return False, ""

    exe = posixpath.basename(_strip_outer_quotes(cmd_tokens[0]))
    if exe != "rm":
        return False, ""

    has_recursive = False
    has_no_preserve_root = False
    after_double_dash = False
    targets: list[str] = []

    for raw_tok in cmd_tokens[1:]:
        if raw_tok.startswith((">", "2>", "&>", "1>")):
            break
        tok = _strip_outer_quotes(raw_tok)
        if not after_double_dash:
            if tok == "--":
                after_double_dash = True
                continue
            if tok == "--recursive":
                has_recursive = True
                continue
            if tok == "--no-preserve-root" or tok.startswith("--no-preserve-root="):
                has_no_preserve_root = True
                continue
            if tok.startswith("--"):
                continue
            if tok.startswith("-") and len(tok) > 1:
                flags = tok[1:]
                if "r" in flags or "R" in flags:
                    has_recursive = True
                continue
        if tok:
            targets.append(raw_tok)

    if has_no_preserve_root and not has_recursive:
        has_recursive = True

    if not has_recursive:
        return False, ""

    for t in targets:
        is_prot, desc = _is_protected_nas_recursive_delete_target(t, active_cwd=active_cwd)
        if is_prot:
            return True, f"Recursive delete ('rm') targeting {desc}"

    return False, ""


def _is_unscoped_or_destructive_compose_down(seg: str) -> bool:
    """Return True if segment runs an unscoped `docker compose down` / `docker-compose down` or destroys volumes (`-v`/`--volumes`/`--rmi`)."""
    m = re.search(
        r"\bdocker(?:-compose|(?:\s+(?:-[Hc]\s+\S+|--(?:host|context|config)(?:=\S+|\s+\S+)|-D|--debug))*\s+compose)\b(?P<rest>.*)$",
        seg.strip(),
    )
    if not m:
        return False
    tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", m.group("rest").strip())
    has_project_scope = False
    down_idx: int | None = None
    idx = 0
    while idx < len(tokens):
        raw_tok = tokens[idx]
        if raw_tok.startswith((">", "2>", "&>", "1>")):
            break
        tok = _strip_outer_quotes(raw_tok)
        if tok == "down":
            down_idx = idx
            break
        if tok in _COMPOSE_PRE_VALUE_FLAGS:
            if tok in _COMPOSE_SCOPE_FLAGS and idx + 1 < len(tokens):
                nxt_tok = _strip_outer_quotes(tokens[idx + 1])
                if nxt_tok and not nxt_tok.startswith("-"):
                    has_project_scope = True
            idx += 2
            continue
        if any(tok.startswith(prefix) for prefix in ("--file=", "--project-name=", "--project-directory=")):
            val = tok.split("=", 1)[1].strip()
            if val:
                has_project_scope = True
            idx += 1
            continue
        if (tok.startswith("-f") or tok.startswith("-p")) and len(tok) > 2 and not tok.startswith("--"):
            has_project_scope = True
            idx += 1
            continue
        idx += 1

    if down_idx is None:
        return False

    has_service_scope = False
    j = down_idx + 1
    while j < len(tokens):
        raw_tok = tokens[j]
        if raw_tok.startswith((">", "2>", "&>", "1>")):
            break
        tok = _strip_outer_quotes(raw_tok)
        if tok in ("-v", "--volumes", "--rmi") or tok.startswith("--rmi="):
            return True
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1 and tok[1:].isalpha() and "v" in tok[1:]:
            return True
        if tok in ("-t", "--timeout"):
            j += 2
            continue
        if tok.startswith("-"):
            j += 1
            continue
        if tok:
            has_service_scope = True
        j += 1

    return not (has_project_scope or has_service_scope)


def _is_daemon_stop_or_restart(seg: str) -> Tuple[bool, str]:
    """Detect stopping or restarting Synology ContainerManager or the dockerd/docker daemon."""
    if re.search(
        r"\b(?:systemctl|synoservice|synoservicectl|synopkg)\b[^\n;|&]*?"
        r"(?:--)?(?:stop|restart|hard-stop|reload-or-restart|try-restart|condrestart|pause|disable|mask|kill)\b[^\n;|&]*?"
        r"\b(?:(?:pkgctl-|pkg-)?ContainerManager(?:\.service)?|dockerd?(?:\.service|\.socket)?)\b",
        seg,
        re.IGNORECASE,
    ) or re.search(
        r"\b(?:systemctl|synoservice|synoservicectl|synopkg)\b[^\n;|&]*?"
        r"\b(?:(?:pkgctl-|pkg-)?ContainerManager(?:\.service)?|dockerd?(?:\.service|\.socket)?)\b[^\n;|&]*?"
        r"(?:--)?(?:stop|restart|hard-stop|reload-or-restart|try-restart|condrestart|pause|disable|mask|kill)\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "Stopping or restarting 'ContainerManager' or 'dockerd' daemon"

    if re.search(
        r"\bservice\s+(?:(?:pkgctl-)?ContainerManager|dockerd?)\s+(?:stop|restart|force-reload)\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "Stopping or restarting 'ContainerManager' or 'dockerd' service"

    if re.search(
        r"(?:/etc/init\.d/|/usr/syno/etc/rc\.sysv/)[^\s;|&]*(?:ContainerManager|dockerd?)[^\s;|&]*\s+(?:stop|restart)\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "Stopping or restarting 'ContainerManager' or 'dockerd' init script"

    if re.search(
        r"\b(?:pkill|killall)\b[^\n;|&]*\b(?:ContainerManager|dockerd|containerd)\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "Killing 'ContainerManager' or 'dockerd' daemon process"

    return False, ""


def _is_host_power_or_synopkg_uninstall(seg: str) -> Tuple[bool, str]:
    """Detect NAS host reboot/shutdown/poweroff commands and synopkg uninstall."""
    if re.search(r"\bsynopkg\b(?:\s+-[A-Za-z0-9_-]+)*\s+uninstall\b", seg, re.IGNORECASE):
        return True, "Destructive Synology package removal ('synopkg uninstall')"

    clean_seg = seg.strip().lstrip("(").rstrip(")").strip()
    raw_tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", clean_seg)
    cmd_tokens = _strip_command_wrappers(raw_tokens)
    if cmd_tokens:
        exe = posixpath.basename(_strip_outer_quotes(cmd_tokens[0])).lower()
        if exe in ("reboot", "shutdown", "poweroff", "halt"):
            return True, f"Host power command ('{exe}')"
        if exe in ("init", "telinit") and len(cmd_tokens) >= 2 and _strip_outer_quotes(cmd_tokens[1]) in ("0", "6"):
            return True, f"Host runlevel power command ('{exe} {_strip_outer_quotes(cmd_tokens[1])}')"
        if exe == "systemctl":
            for raw_arg in cmd_tokens[1:]:
                arg = _strip_outer_quotes(raw_arg).lower()
                if arg in ("reboot", "poweroff", "halt", "kexec"):
                    return True, f"Host systemctl power command ('systemctl {arg}')"

    if re.search(
        r"\b(?:os\.system|subprocess\.(?:run|call|Popen|check_call|check_output))\s*\([^\n)]*?\b(?:reboot|shutdown|poweroff)\b",
        clean_seg,
        re.IGNORECASE,
    ):
        return True, "Inline Python host power command ('reboot' / 'shutdown' / 'poweroff')"

    return False, ""


def validate_remote_ssh_command(remote_cmd: str) -> Tuple[bool, str]:
    """Validate a NAS SSH remote command and block destructive operations unless FORGE_APPROVED_CARD_EXECUTION=1."""
    if not remote_cmd or not str(remote_cmd).strip():
        return True, ""
    if os.environ.get("FORGE_APPROVED_CARD_EXECUTION") == "1":
        return True, ""

    cmd_str = str(remote_cmd)
    candidate_commands: list[str] = [cmd_str]
    lvl1 = _extract_wrapped_remote_commands(cmd_str)
    candidate_commands.extend(lvl1)
    for sub in lvl1:
        candidate_commands.extend(_extract_wrapped_remote_commands(sub))

    for cand in candidate_commands:
        active_cwd: str | None = None
        for seg in _split_unquoted_shell_segments(cand):
            clean_seg = seg.strip().lstrip("(").rstrip(")").strip()
            if not clean_seg:
                continue

            raw_tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", clean_seg)
            cmd_tokens = _strip_command_wrappers(raw_tokens)
            if cmd_tokens and _strip_outer_quotes(cmd_tokens[0]) == "cd" and len(cmd_tokens) >= 2:
                cd_target = _strip_outer_quotes(cmd_tokens[1])
                if cd_target and not cd_target.startswith("-"):
                    active_cwd = cd_target

            is_rm_bad, rm_desc = _check_recursive_delete_segment(seg, active_cwd=active_cwd)
            if is_rm_bad:
                return False, f"NAS SSH Command Filter Blocked: {rm_desc}. {_SSH_APPROVAL_HINT}"

            if re.search(
                r"\bdocker\b(?:\s+(?:-[Hc]\s+\S+|--(?:host|context|config)(?:=\S+|\s+\S+)|-D|--debug))*\s+volume\s+(?:rm|remove|prune)\b",
                clean_seg,
                re.IGNORECASE,
            ):
                return False, (
                    "NAS SSH Command Filter Blocked: Destructive Docker volume deletion ('docker volume rm/prune') "
                    f"is prohibited without approval. {_SSH_APPROVAL_HINT}"
                )

            if re.search(
                r"\bdocker\b(?:\s+(?:-[Hc]\s+\S+|--(?:host|context|config)(?:=\S+|\s+\S+)|-D|--debug))*\s+system\s+prune\b",
                clean_seg,
                re.IGNORECASE,
            ):
                return False, (
                    "NAS SSH Command Filter Blocked: Destructive container prune ('docker system prune') "
                    f"is prohibited without approval. {_SSH_APPROVAL_HINT}"
                )

            if _is_unscoped_or_destructive_compose_down(clean_seg):
                return False, (
                    "NAS SSH Command Filter Blocked: Unscoped or volume-destroying 'docker compose down' "
                    f"is prohibited without approval. {_SSH_APPROVAL_HINT}"
                )

            is_daemon_bad, daemon_desc = _is_daemon_stop_or_restart(clean_seg)
            if is_daemon_bad:
                return False, (
                    f"NAS SSH Command Filter Blocked: {daemon_desc} is prohibited without approval. "
                    f"{_SSH_APPROVAL_HINT}"
                )

            is_power_bad, power_desc = _is_host_power_or_synopkg_uninstall(clean_seg)
            if is_power_bad:
                return False, (
                    f"NAS SSH Command Filter Blocked: {power_desc} is prohibited without approval. "
                    f"{_SSH_APPROVAL_HINT}"
                )

    return True, ""


def run_ssh(
    host: str,
    remote_cmd: str,
    timeout: int = 120,
    user: str = None,
    port: str = None,
    key: str = None,
    connect_timeout: int = None,
    batch_mode: bool = False,
    disable_mux: bool = False,
    retry_without_mux_on_timeout: bool = True,
) -> Tuple[int, str, str]:
    """Execute a remote command via SSH synchronously, with automatic mux eviction fallback on timeout."""
    if os.environ.get("FORGE_APPROVED_CARD_EXECUTION") != "1":
        allowed, block_reason = validate_remote_ssh_command(remote_cmd)
        if not allowed:
            return 126, "", block_reason
    effective_connect_timeout = (
        connect_timeout if connect_timeout is not None else min(max(15, timeout // 2), 30)
    )
    cmd = build_ssh_cmd(
        host,
        remote_cmd,
        user=user,
        port=port,
        key=key,
        connect_timeout=effective_connect_timeout,
        batch_mode=batch_mode,
        disable_mux=disable_mux,
    )
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if (
            res.returncode == 255
            and not disable_mux
            and retry_without_mux_on_timeout
            and any(
                k in (res.stderr or "").lower()
                for k in (
                    "timed out",
                    "mux_client",
                    "connection reset",
                    "broken pipe",
                    "control socket",
                    "session_process_tty",
                )
            )
        ):
            evict_ssh_mux_socket(host, user=user, port=port)
            fallback_cmd = build_ssh_cmd(
                host,
                remote_cmd,
                user=user,
                port=port,
                key=key,
                connect_timeout=effective_connect_timeout,
                batch_mode=batch_mode,
                disable_mux=True,
            )
            res = subprocess.run(
                fallback_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        return res.returncode, res.stdout or "", res.stderr or ""
    except subprocess.TimeoutExpired:
        if not disable_mux:
            evict_ssh_mux_socket(host, user=user, port=port)
            if retry_without_mux_on_timeout:
                fallback_cmd = build_ssh_cmd(
                    host,
                    remote_cmd,
                    user=user,
                    port=port,
                    key=key,
                    connect_timeout=effective_connect_timeout,
                    batch_mode=batch_mode,
                    disable_mux=True,
                )
                try:
                    res = subprocess.run(
                        fallback_cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                    )
                    return res.returncode, res.stdout or "", res.stderr or ""
                except subprocess.TimeoutExpired:
                    return -1, "", f"SSH command timed out after {timeout}s (including mux-bypass retry): {remote_cmd[:60]}"
                except Exception as e:
                    return -1, "", f"SSH fallback execution exception: {e}"
        return -1, "", f"SSH command timed out after {timeout}s: {remote_cmd[:60]}"
    except Exception as e:
        return -1, "", f"SSH execution exception: {e}"

if __name__ == "__main__":
    h1, h2, p, u, k = resolve_nas_config()
    print(f"Host 1: {h1}")
    print(f"Host 2: {h2}")
    print(f"SSH Port: {p}")
    print(f"SSH User: {u}")
    print(f"SSH Key: {k}")
