#!/usr/bin/env python3
"""
Tool Batching Safety Guard (Antigravity PreToolUse Hook)
Enforces the Tool Roundtrip Minimization & Batch Execution Invariant using strictly
CONSECUTIVE anti-pattern counters and 90-second FORGE Tier 1/2/3 gates:
1. Consecutive single-line shell inspection commands (run_command), including sed/awk/wc slicing.
2. Consecutive inline Python heredoc / -c probing loops (MAX_CONSECUTIVE_INLINE_PYTHON = 2).
3. Consecutive repetitive file reading / narrow line slicing (view_file / read_file), with wide-slice (>=350 lines) allowance.
4. Consecutive unverified single-file replacements (replace_file_content / edit_file).
5. Consecutive unbatched git lifecycle commands (git add / commit / push / gh pr create).
6. 90-second FORGE Tier 1/2/3 inline calibration & multi-file production mutation gates.
7. Self-preservation AST syntax validation on hook edits and Decision Card file deletion gate.
"""
import ast
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse

STATE_FILE = "/tmp/batch_guard_state.json" if os.name != "nt" else os.path.join(tempfile.gettempdir(), "batch_guard_state.json")
FORGE_CHANNEL_ID_STR = "1562899941595160657"
ZERO_CHAT_CHANNEL_ID_STR = "1542081375287640084"
_IS_VULCAN_ENV = (
    os.environ.get("AGENT_ROLE", "").strip().lower() == "vulcan"
    or os.environ.get("DISCORD_CHANNEL_ID", "").strip() == FORGE_CHANNEL_ID_STR
)
IN_FLIGHT_TURN_FILE = (
    "/workspace/data/vulcan_in_flight_turn.json"
    if _IS_VULCAN_ENV
    else "/workspace/data/in_flight_turn.json"
)
MAX_CONSECUTIVE_SHELL_INSPECTIONS = 3
MAX_CONSECUTIVE_INLINE_PYTHON = 2
MAX_CONSECUTIVE_SAME_FILE_VIEWS = 2
MAX_WIDE_SAME_FILE_VIEWS = 4
WIDE_VIEW_MIN_LINES = 350
MAX_CONSECUTIVE_FILE_VIEWS = 6
MAX_CONSECUTIVE_SAME_FILE_EDITS = 3
MAX_CONSECUTIVE_GIT_CMDS = 2
MAX_VERSIONED_SCRATCH_SWEEPS = 3
MAX_SAME_SCRATCH_SCRIPT_RUNS = 3
MAX_INLINE_PRODUCTION_FILES = 3
FORGE_TIER1_INLINE_SECONDS = 90.0
MAX_CALIBRATION_SCRATCH_RUNS_AFTER_90S = 2
MAX_DIAGNOSTIC_SCRATCH_RUNS_AFTER_90S = 8
MAX_INLINE_RUN_COMMANDS_AFTER_90S = 15
TURN_RESET_IDLE_SECONDS = 45.0
TURN_STATE_EXPIRY_SECONDS = 900.0

INTERACTIVE_DIAG_MAX_STEPS_AFTER_30S = 10
INTERACTIVE_DIAG_MAX_STEPS_HARD_LIMIT = 12
INTERACTIVE_DIAG_TIME_WINDOW_SECONDS = 120.0
CRAB_CAVERN_CHANNEL_IDS = {
    1534436119888793750,  # #the-banana-stand
    1534452820995080192,  # #lounge
    1549555629222404096,  # #debug-huddle
    1551465050072416286,  # #side-project
    1552732003076669460,  # #mirrormere
    1558202642663211169,  # #agora
    1561509863018856468,  # #gotchi
    1562246590943461429,  # #gotchi-chat
    1562544598423965856,  # #r-residentagents
}

_VERSIONED_SCRATCH_SWEEP_RE = re.compile(
    r"(?:^|/)(?:scratch|brain/[^/]+/scratch|tmp)/(?:(?P<stem>[a-zA-Z0-9_-]*?(?:sim|sweep|grid|solve|tune|calib|opt)[a-zA-Z0-9_-]*?)_v\d+|v\d+)\.py$",
    re.IGNORECASE,
)

_CALIBRATION_SCRATCH_SCRIPT_RE = re.compile(
    r"(?:^|/)(?:baseball/.*|(?:scratch|brain/[^/]+/scratch|tmp)/[a-zA-Z0-9_-]*?(?:sim|sweep|grid|solve|tune|calib|opt|bench|migrate)[a-zA-Z0-9_-]*\.py)$",
    re.IGNORECASE,
)

_SCRATCH_SCRIPT_EXEC_RE = re.compile(
    r"\bpython[0-9.]*\s+(?P<script>[^\s;\"'|&>]*?(?:/scratch/|^scratch/|/tmp/(?!forge-wt-))[^\s;\"'|&>]+?\.py)\b"
)


def is_calibration_scratch_script(script_path: str) -> bool:
    """Return True if a scratch/tmp script path looks like a calibration, optimization, simulation, or benchmark sweep."""
    if not script_path:
        return False
    return bool(_CALIBRATION_SCRATCH_SCRIPT_RE.search(script_path.replace("\\", "/")))

_HEAVY_CLI_BENCHMARK_OR_MIGRATE_RE = re.compile(
    r"(?:run_unified_benchmark_gate\.py|build_locked_evaluation_datasets|--migrate-db\b|--migrate-all-pt-modes\b|fast_migrate_[a-zA-Z0-9_-]*\.py\b|backfill_[a-zA-Z0-9_-]*?splits[a-zA-Z0-9_-]*\.py\b)"
)

_DOCKER_ROOT_PREFIX = re.escape(os.environ.get("NAS_DOCKER_ROOT", "/" + "volume1/docker").rstrip("/"))
_PROD_SOURCE_PATH_RE = re.compile(
    rf"(?P<path>(?:/baseball/big-projections|/baseball/big_board_pro|/workspace/tools|/workspace/\.agents|{_DOCKER_ROOT_PREFIX}/[a-zA-Z0-9_.-]+|/docker/[a-zA-Z0-9_.-]+|/app/[a-zA-Z0-9_.-]+)/[a-zA-Z0-9_./-]+\.(?:py|R|sh|js|ts|tsx|jsx))\b"
)


def _default_state(now: float = 0.0) -> dict:
    return {
        "agy_ppid": 0,
        "last_time": now,
        "turn_start_time": now,
        "cmd_inspections": 0,
        "consecutive_inline_python": 0,
        "consecutive_file_views": 0,
        "last_viewed_file": "",
        "same_file_view_count": 0,
        "last_edited_file": "",
        "same_file_edit_count": 0,
        "consecutive_git_cmds": 0,
        "agora_eval_calls": 0,
        "versioned_scratch_sweeps": [],
        "inline_prod_files": [],
        "scratch_script_runs": {},
        "total_scratch_runs": 0,
        "calibration_scratch_runs": 0,
        "turn_run_commands": 0,
        "offload_dispatched": False,
        "offload_kind": "",
        "interactive_diag_steps": 0,
        "web_tainted": False,
        "web_taint_source": "",
    }


def _norm_posix(path_str: str, cwd: str = "/workspace") -> str:
    """Normalize a path to forward-slash POSIX form, collapsing '..' segments and resolving symlinks when paths exist."""
    s = (path_str or "").replace("\\", "/")
    if s.startswith("/"):
        norm = posixpath.normpath(s)
    else:
        c = (cwd or "/workspace").replace("\\", "/")
        if c.startswith("/"):
            combined = c.rstrip("/") + "/" + s if s else c
            norm = posixpath.normpath(combined)
        else:
            norm = os.path.abspath(os.path.join(cwd or ".", path_str)).replace("\\", "/")
    if os.name != "nt" and norm.startswith("/"):
        try:
            if os.path.lexists(norm) or os.path.lexists(posixpath.dirname(norm)):
                norm = os.path.realpath(norm).replace("\\", "/")
        except Exception:
            pass
    return norm


def _is_sensitive_private_path(
    raw_path: str,
    cwd: str = "/workspace",
    include_channel_history: bool = False,
) -> bool:
    """Return True if raw_path (after '..' normalization and symlink resolution) targets /secrets, memory/private, memory/vault, .env, or channel_history.json."""
    clean = _strip_outer_quotes(str(raw_path or "").strip())
    if not clean:
        return False
    s = clean.replace("\\", "/")
    if s.startswith("/"):
        norm_lex = posixpath.normpath(s)
    else:
        c = (cwd or "/workspace").replace("\\", "/")
        if c.startswith("/"):
            norm_lex = posixpath.normpath(c.rstrip("/") + "/" + s)
        else:
            norm_lex = os.path.abspath(os.path.join(cwd or ".", clean)).replace("\\", "/")
    candidates = [norm_lex]
    if os.name != "nt" and norm_lex.startswith("/"):
        try:
            real_p = os.path.realpath(norm_lex).replace("\\", "/")
            if real_p not in candidates:
                candidates.append(real_p)
        except Exception:
            pass
    for p in candidates:
        p_low = p.lower()
        if p == "/secrets" or p.startswith("/secrets/"):
            return True
        if p.endswith("/memory/private") or "/memory/private/" in p:
            return True
        if p.endswith("/memory/vault") or "/memory/vault/" in p:
            return True
        base = posixpath.basename(p_low.rstrip("/"))
        if base == ".env" or (
            base.startswith(".env.")
            and base not in (".env.example", ".env.sample", ".env.template")
        ):
            return True
        if include_channel_history and base == "channel_history.json":
            return True
    return False


def _has_sensitive_path_token_in_cmd(
    cmd: str,
    cwd: str = "/workspace",
    include_channel_history: bool = False,
) -> bool:
    """Return True if any path-like token or quoted string in cmd resolves to a sensitive private/secrets path."""
    if not cmd:
        return False
    candidates = [cmd] + _extract_wrapped_shell_commands(cmd)
    for cand in candidates:
        for raw_tok in re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|[^\s;|&<>()]+)", cand):
            tok = _strip_outer_quotes(raw_tok).strip()
            if not tok:
                continue
            parts_to_check = [tok]
            # Handle curl @/path, flag=/path, or leading redirection prefixes
            if tok.startswith("@") and len(tok) > 1:
                parts_to_check.append(tok[1:])
            if "=" in tok:
                rhs = tok.split("=", 1)[1].strip()
                if rhs:
                    parts_to_check.append(_strip_outer_quotes(rhs))
            # Also inspect string literals embedded inside python -c / sh -c payloads
            for m_lit in re.finditer(r"(['\"])([^'\"]{2,})\1", tok):
                parts_to_check.append(m_lit.group(2))
            for candidate_path in parts_to_check:
                if _is_sensitive_private_path(
                    candidate_path,
                    cwd=cwd,
                    include_channel_history=include_channel_history,
                ):
                    return True
    return False


def _has_bare_env_or_printenv(cmd: str) -> bool:
    """Return True if any shell segment runs `printenv` or bare `env` (dumping process environment)."""
    if not cmd:
        return False
    candidates = [cmd] + _extract_wrapped_shell_commands(cmd)
    for cand in candidates:
        for seg in _split_unquoted_shell_segments(cand):
            s = seg.strip()
            if not s:
                continue
            if re.match(r"^(?:sudo\s+)?(?:/usr/bin/|/bin/)?printenv\b", s):
                return True
            m_env = re.match(r"^(?:sudo\s+)?(?:/usr/bin/|/bin/)?env\b(?P<rest>.*)$", s)
            if m_env:
                rest = m_env.group("rest").strip()
                tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", rest)
                idx = 0
                has_subcmd = False
                while idx < len(tokens):
                    tok = _strip_outer_quotes(tokens[idx])
                    if tok.startswith((">", "1>", "2>", "&>", "<")):
                        break
                    if tok in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"):
                        idx += 2
                        continue
                    if tok.startswith("-"):
                        idx += 1
                        continue
                    if "=" in tok and not tok.startswith("="):
                        idx += 1
                        continue
                    has_subcmd = True
                    break
                if not has_subcmd:
                    return True
    return False


def _is_outbound_curl_or_wget(cmd: str) -> bool:
    """Return True if cmd invokes curl/wget targeting anything other than explicit loopback URLs."""
    if not cmd:
        return False
    candidates = [cmd] + _extract_wrapped_shell_commands(cmd)
    loopback_hosts = {"localhost", "127.0.0.1", "::1"}
    for cand in candidates:
        for seg in _split_unquoted_shell_segments(cand):
            if not re.search(r"\b(?:curl|wget)\b", seg):
                continue
            urls = re.findall(r"https?://[^\s'\"`]+", seg)
            if not urls:
                return True
            for u in urls:
                try:
                    host = (urllib.parse.urlparse(u).hostname or "").lower().strip("[]")
                    if host not in loopback_hosts:
                        return True
                except Exception:
                    return True
            # Also reject non-URL bare host tokens or shell variable URLs alongside loopback URLs
            if re.search(r"\$[{(]?[A-Za-z_]", seg):
                return True
    return False


def is_forge_worktree_or_exempt_path(target_file: str, cwd: str = "/workspace") -> bool:
    """Return True if target_file is inside a FORGE worktree, scratch, memory, data, or unit test file."""
    if not target_file:
        return True
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True
    norm_cwd = _norm_posix(cwd or "/workspace", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return True
    norm_target = _norm_posix(target_file, cwd or "/workspace")
    # On local Windows laptop standby replica (when not testing simulated /workspace, /baseball, /docker, or /tmp paths), exempt from container FORGE worktree gate
    if os.name == "nt" and not norm_target.startswith(("/workspace/", "/baseball/", "/docker/", "/tmp/", "/app/")):
        return True
    exempt_prefixes = (
        "/tmp/",
        "/workspace/scratch/",
        "/workspace/memory/",
        "/workspace/data/",
        "/root/.gemini/",
    )
    if norm_target.startswith(exempt_prefixes):
        return True
    if "/scratch/" in norm_target or "/memory/" in norm_target or "/brain/" in norm_target:
        return True
    base = os.path.basename(norm_target)
    if base.startswith("test_") or base.endswith("_test.py") or "/tests/" in norm_target:
        return True
    if norm_target.endswith(".md"):
        return True
    return False


def _get_agy_ancestor_pid() -> int:
    """Resolve the persistent agy worker ancestor PID (instead of the transient bash -c wrapper PID)."""
    cur = os.getppid()
    for _ in range(8):
        if cur <= 1:
            break
        try:
            cmdline = open(f"/proc/{cur}/cmdline", "r", encoding="utf-8", errors="ignore").read().replace("\x00", " ")
            if "agy " in cmdline or cmdline.startswith("agy"):
                return cur
            stat_txt = open(f"/proc/{cur}/stat", "r", encoding="utf-8", errors="ignore").read()
            rparen = stat_txt.rfind(")")
            if rparen != -1:
                fields = stat_txt[rparen + 2 :].split()
                if len(fields) >= 2 and fields[1].isdigit():
                    cur = int(fields[1])
                    continue
        except Exception:
            pass
        break
    return os.getppid()


def _get_current_turn_Step_anchor(
    agy_pid: int | None = None,
    in_flight_file: str | None = None,
) -> str:
    """Return a deterministic per-turn anchor combining agy_pid, in_flight_turn metadata, and transcript USER_INPUT step."""
    if in_flight_file is None:
        in_flight_file = IN_FLIGHT_TURN_FILE
    if agy_pid is None:
        agy_pid = _get_agy_ancestor_pid()
    cid = ""
    cid_from_proc = ""
    try:
        cmdline = open(f"/proc/{agy_pid}/cmdline", "r", encoding="utf-8", errors="ignore").read().replace("\x00", " ")
        m = re.search(r"--conversation=([a-f0-9-]+)", cmdline)
        if m:
            cid_from_proc = m.group(1)
    except Exception:
        pass
    in_flight_sig = ""
    try:
        if in_flight_file and os.path.exists(in_flight_file):
            with open(in_flight_file, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
            if isinstance(data, dict):
                matched_entry = None
                env_ch = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
                if env_ch and isinstance(data.get(env_ch), dict):
                    matched_entry = data[env_ch]
                if matched_entry is None:
                    for v in data.values():
                        if isinstance(v, dict) and v.get("pid") == agy_pid:
                            matched_entry = v
                            break
                if matched_entry is None and cid_from_proc:
                    for v in data.values():
                        if isinstance(v, dict) and str(v.get("conv_id") or "") == cid_from_proc:
                            matched_entry = v
                            break
                if matched_entry is None:
                    # Fallback: pick the most recently updated active turn entry
                    dict_entries = [v for v in data.values() if isinstance(v, dict) and ("prompt" in v or "ts" in v or "status_msg_id" in v)]
                    if len(dict_entries) == 1:
                        matched_entry = dict_entries[0]
                    elif dict_entries:
                        matched_entry = max(dict_entries, key=lambda x: float(x.get("ts") or 0.0))
                if matched_entry is None and ("prompt" in data or "ts" in data or "status_msg_id" in data):
                    matched_entry = data
                if isinstance(matched_entry, dict):
                    cid = str(matched_entry.get("conv_id") or data.get("conv_id") or "")
                    status_msg_id = str(matched_entry.get("status_msg_id") or "")
                    attempts = str(matched_entry.get("attempts") or 1)
                    prompt_raw = str(matched_entry.get("prompt") or "")
                    prompt_hash = hashlib.sha1(prompt_raw.encode("utf-8", errors="ignore")).hexdigest()[:10] if prompt_raw else ""
                    in_flight_sig = f"{status_msg_id}:{attempts}:{prompt_hash}"
    except Exception:
        pass

    user_step = ""
    try:
        if not cid:
            cmdline = open(f"/proc/{agy_pid}/cmdline", "r", encoding="utf-8", errors="ignore").read().replace("\x00", " ")
            m = re.search(r"--conversation=([a-f0-9-]+)", cmdline)
            if m:
                cid = m.group(1)
        if cid:
            tpath = f"/root/.gemini/antigravity-cli/brain/{cid}/.system_generated/logs/transcript.jsonl"
            if os.path.exists(tpath):
                with open(tpath, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 524288))
                    tail = f.read().decode("utf-8", errors="ignore").splitlines()
                for line in reversed(tail):
                    if '"USER_INPUT"' in line:
                        try:
                            d = json.loads(line)
                            if d.get("type") == "USER_INPUT":
                                user_step = str(d.get("step_index", 0))
                                break
                        except Exception:
                            pass
    except Exception:
        pass

    parts = [str(agy_pid)]
    if cid:
        parts.append(cid)
    if user_step:
        parts.append(f"s{user_step}")
    if in_flight_sig:
        parts.append(in_flight_sig)
    return ":".join(parts)


def _has_conv_id(turn_anchor: str, in_flight_file: str | None = None, agy_pid: int | None = None) -> bool:
    """Check whether turn_anchor contains a conversation ID."""
    if not turn_anchor:
        return False
    parts = str(turn_anchor).split(":")
    if len(parts) >= 2:
        cand = parts[1]
        # Standard conversation ID: non-empty, not purely digits, and not a step index
        if cand and not cand.isdigit() and not re.match(r"^s\d+$", cand):
            return True
    if in_flight_file and os.path.exists(in_flight_file):
        try:
            with open(in_flight_file, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
            if isinstance(data, dict):
                matched = None
                env_ch = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
                if env_ch and isinstance(data.get(env_ch), dict):
                    matched = data[env_ch]
                if matched is None and agy_pid is not None:
                    for v in data.values():
                        if isinstance(v, dict) and v.get("pid") == agy_pid:
                            matched = v
                            break
                if matched is None:
                    dict_entries = [v for v in data.values() if isinstance(v, dict) and ("prompt" in v or "ts" in v or "status_msg_id" in v)]
                    if len(dict_entries) == 1:
                        matched = dict_entries[0]
                if matched is None and ("prompt" in data or "ts" in data or "status_msg_id" in data):
                    matched = data
                if isinstance(matched, dict) and matched.get("conv_id"):
                    return True
        except Exception:
            pass
    return False


def _get_in_flight_channel_id(in_flight_file: str | None = None, agy_pid: int | None = None) -> int | str | None:
    """Extract active channel ID from in_flight_turn.json if present."""
    if in_flight_file is None:
        in_flight_file = IN_FLIGHT_TURN_FILE
    if not in_flight_file or not os.path.exists(in_flight_file):
        return None
    try:
        with open(in_flight_file, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data:
            return None

        if agy_pid is None:
            agy_pid = _get_agy_ancestor_pid()

        # 1. Match ancestor PID in nested dicts
        for k, v in data.items():
            if isinstance(v, dict):
                if v.get("pid") and v.get("pid") == agy_pid and v.get("channel_id") is not None:
                    return v.get("channel_id")

        # 2. Key matching DISCORD_CHANNEL_ID env
        env_ch = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
        if env_ch and env_ch in data and isinstance(data[env_ch], dict):
            entry_pid = data[env_ch].get("pid")
            if not entry_pid or entry_pid == agy_pid:
                return data[env_ch].get("channel_id", env_ch)

        # 3. Direct channel_id field if pid matches or is omitted
        top_pid = data.get("pid")
        if "channel_id" in data and data["channel_id"] is not None:
            if not top_pid or top_pid == agy_pid:
                return data["channel_id"]

        # 4. Check nested entries without a pid conflict (e.g. test fixtures where pid is None)
        dict_entries = [
            (k, v)
            for k, v in data.items()
            if isinstance(v, dict) and ("prompt" in v or "ts" in v or "status_msg_id" in v or "channel_id" in v)
        ]
        eligible_entries = [
            (k, v) for k, v in dict_entries
            if not v.get("pid") or v.get("pid") == agy_pid
        ]
        if len(eligible_entries) == 1:
            k, v = eligible_entries[0]
            return v.get("channel_id", k)
        elif eligible_entries:
            k, recent = max(eligible_entries, key=lambda item: float(item[1].get("ts") or 0.0))
            return recent.get("channel_id", k)

        for k, v in data.items():
            if isinstance(v, dict):
                entry_pid = v.get("pid")
                if not entry_pid or entry_pid == agy_pid:
                    return v.get("channel_id", k)
            elif not top_pid or top_pid == agy_pid:
                return k
    except Exception:
        return None
    return None


def _resolve_repo_root() -> str:
    """Resolve repository root whether imported from `tools/` or `.agents/hooks/`."""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if os.path.basename(here) == "hooks" and os.path.basename(parent) == ".agents":
        return os.path.dirname(parent)
    return parent


def is_crab_cavern_channel(channel_id: int | str | None) -> bool:
    """Return True if channel_id belongs to Crab Cavern or an external shared channel."""
    if channel_id is None:
        return False
    try:
        cid = int(str(channel_id).strip())
    except (ValueError, TypeError):
        return False
    if cid in CRAB_CAVERN_CHANNEL_IDS:
        return True
    try:
        _repo_root = _resolve_repo_root()
        if _repo_root not in sys.path:
            sys.path.insert(0, _repo_root)
        from tools.bridge_state import CRAB_CAVERN_CHANNEL_IDS as _BS_CC_IDS
        return cid in _BS_CC_IDS
    except Exception:
        return False


def check_crab_cavern_airgap_guard(
    name: str,
    args: dict,
    cwd: str = "/workspace",
    in_flight_file: str | None = None,
    agy_pid: int | None = None,
) -> tuple[bool, str]:
    """Block private homelab NAS / SSH / secrets access during Crab Cavern turns and instruct staging a #zero-chat Decision Card."""
    if in_flight_file is None:
        in_flight_file = IN_FLIGHT_TURN_FILE
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True, ""

    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not channel_id and in_flight_file and os.path.exists(in_flight_file):
        in_flight_ch = _get_in_flight_channel_id(in_flight_file, agy_pid=agy_pid)
        if in_flight_ch is not None:
            channel_id = str(in_flight_ch).strip()

    if not is_crab_cavern_channel(channel_id):
        return True, ""

    escalation_msg = (
        "Crab Cavern Air-Gap Sandbox Guard Triggered: Direct homelab NAS, SSH, Home Assistant, or `/secrets/` access is blocked in shared Crab Cavern channels. "
        "NEVER ask Ryan to re-prompt in `#zero-chat`. Instead, immediately stage a 1-click Decision Card to `#zero-chat` (`1542081375287640084`) on the spot via:\n"
        "python3 /workspace/tools/forge.py propose --channel 1542081375287640084 --category \"homelab_action\" --title \"<Action Title>\" "
        "--summary \"<Action Summary>\" --handler \"<handler_type>\" --payload '<json_payload>' --post"
    )

    if name in (
        "view_file",
        "read_file",
        "edit_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write_file",
        "write_to_file",
    ):
        raw_target = str(
            args.get("AbsolutePath") or args.get("TargetFile") or args.get("Path") or args.get("path") or ""
        ).strip()
        if _is_sensitive_private_path(raw_target, cwd=cwd, include_channel_history=True):
            return False, escalation_msg
        return True, ""

    if name in ("grep_search", "find_by_name"):
        search_target = str(
            args.get("SearchDirectory")
            or args.get("SearchPath")
            or args.get("Path")
            or args.get("path")
            or args.get("Directory")
            or ""
        ).strip()
        if search_target and _is_sensitive_private_path(search_target, cwd=cwd, include_channel_history=True):
            return False, escalation_msg
        pattern_blob = " ".join(
            str(args.get(k) or "")
            for k in ("FilePattern", "Includes", "Pattern", "Query", "query")
        )
        if pattern_blob and re.search(
            r"(?:/secrets(?:/|\b)|memory/(?:private|vault)\b|(?:^|[\s\"'/=,\[\]])\.env(?!(?:\.example|\.sample|\.template)\b)|\bchannel_history\.json\b)",
            pattern_blob,
            re.IGNORECASE,
        ):
            return False, escalation_msg
        return True, ""

    if name == "run_command":
        cmd = str(args.get("CommandLine") or "").strip()
        if not cmd:
            return True, ""
        # Allow staging the Decision Card itself ONLY via a pure unchained forge.py propose / stage-decision call
        if _is_pure_decision_card_staging_cmd(cmd):
            return True, ""
        if (
            _has_sensitive_path_token_in_cmd(cmd, cwd=cwd, include_channel_history=True)
            or _has_bare_env_or_printenv(cmd)
            or re.search(
                r"\b(?:ssh|scp|sftp)\b"
                r"|\brun_ssh\s*\("
                r"|\bnas_config\.run_ssh\b"
                r"|/secrets(?:/|\b)"
                r"|(?:^|[\s\"'/=])memory/(?:private|vault)(?:/|\b)"
                r"|(?:^|[\s\"'/=])\.env(?!(?:\.example|\.sample|\.template)\b)(?:\.[A-Za-z0-9_-]+)?(?=$|[\s\"'/;|&)])"
                r"|\bchannel_history\.json\b"
                r"|\btranscript_index\.py\b"
                r"|/api/search\b"
                r"|home-assistant_v2\.db\b",
                cmd,
            )
        ):
            return False, escalation_msg

    return True, ""


def is_exempt_from_investigation_gate(name: str, args: dict, cwd: str = "/workspace") -> bool:
    """Return True if tool invocation is exempt from interactive investigation step limits."""
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True
    norm_cwd = _norm_posix(cwd or "/workspace", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return True
    if name in ("edit_file", "replace_file_content", "write_file", "write_to_file"):
        return True
    if name in ("read_file", "view_file"):
        target_p = str(args.get("Path") or args.get("TargetFile") or args.get("AbsolutePath") or "").strip()
        if target_p:
            norm_p = _norm_posix(target_p, cwd or "/workspace")
            if (
                any(seg in norm_p for seg in ("/.agents/", "/memory/", "/docs/"))
                or norm_p.startswith((".agents/", "memory/", "docs/"))
                or os.path.basename(norm_p).lower() in {"skill.md", "readme.md", "agents.md"}
            ):
                return True
    if name == "run_command":
        cmd = str(args.get("CommandLine") or "")
        exempt_tokens = (
            "forge.py",
            "tools.forge",
            "detached_runner.py",
            "modal_harness.py",
            "modal_godot_verify.py",
            "modal_verify.py",
            "outbox.py",
            "gif_tool.py",
            "channel_history.py",
            "transcript_index.py",
            "mealie_tool.py",
            "deliver_image.py",
            "banana.py",
            "send_mail.py",
            "ha_shopping_list.py",
            "whole_foods_afx.py",
            "grocery_manager.py",
            "git ",
            "gh ",
            "pytest",
            "ruff",
            "ast.parse",
            "ast.walk",
        )
        if any(tok in cmd for tok in exempt_tokens):
            return True
        if re.search(r"\b(?:git|gh|forge)\b", cmd):
            return True
        if re.search(r"\bast\.(?:parse|walk)\b", cmd):
            return True
        if re.search(r"\b(?:from\s+ast\s+import|import\s+ast)\b", cmd):
            return True
    return False


def check_interactive_investigation_gate(
    name: str,
    args: dict,
    state: dict,
    state_file: str = STATE_FILE,
    cwd: str = "/workspace",
    now: float = 0.0,
    in_flight_file: str | None = None,
    agy_pid: int | None = None,
) -> tuple[bool, str]:
    """Check deterministic tool-step/elapsed-time gate for interactive Discord bridge turns.

    Open-ended investigations (>10 diagnostic steps after 120s or >12 total diagnostic steps)
    must wrap up initial verified findings, hand heavy probes to forge.py job start, and
    perform an explicit contradiction check against the first reply when woken on completion.
    """
    if in_flight_file is None:
        in_flight_file = IN_FLIGHT_TURN_FILE
    if now <= 0.0:
        now = time.time()

    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True, ""
    norm_cwd = _norm_posix(cwd or "/workspace", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return True, ""

    if not in_flight_file or not os.path.exists(in_flight_file):
        return True, ""

    channel_id = _get_in_flight_channel_id(in_flight_file, agy_pid=agy_pid)
    if not channel_id or str(channel_id).strip() == FORGE_CHANNEL_ID_STR:
        return True, ""

    if is_exempt_from_investigation_gate(name, args, cwd=cwd):
        return True, ""

    diagnostic_tools = ("run_command", "view_file", "read_file", "grep_search", "find_by_name")
    if name not in diagnostic_tools:
        return True, ""

    current_steps = int(state.get("interactive_diag_steps", 0)) + 1
    state["interactive_diag_steps"] = current_steps

    turn_start = float(state.get("turn_start_time", now) or now)
    elapsed = max(0.0, now - turn_start)

    if (
        current_steps > INTERACTIVE_DIAG_MAX_STEPS_AFTER_30S
        and elapsed >= INTERACTIVE_DIAG_TIME_WINDOW_SECONDS
    ) or current_steps > INTERACTIVE_DIAG_MAX_STEPS_HARD_LIMIT:
        save_state(state, state_file)
        reason = (
            f"Interactive Investigation Gate Triggered: Detected {current_steps} diagnostic tool steps "
            f"over {elapsed:.1f}s (ceiling: {INTERACTIVE_DIAG_MAX_STEPS_AFTER_30S} steps after {INTERACTIVE_DIAG_TIME_WINDOW_SECONDS:.1f}s, max {INTERACTIVE_DIAG_MAX_STEPS_HARD_LIMIT} steps). "
            "Open-ended investigations in interactive Discord turns must wrap up initial verified findings immediately. "
            "**Code/Feature/Bugfix Builds:** MUST immediately write a ForgePlan JSON and call `python3 /workspace/tools/forge.py enqueue <plan.json>` — "
            "NEVER call `forge.py job start` for code/asset changes, as `job start` locks the turn without staging a FORGE build or Decision Card. "
            "**Open-Ended Read-Only System Diagnostics:** Close your initial turn now with a clear summary of verified partial findings and hand off deeper diagnostic probes "
            'to a detached background task via: `python3 /workspace/tools/forge.py job start --name "<short task name>" --channel "<channel-name>" --command "<cmd>" --timeout 1800`. '
            "When woken on completion, reread the channel history and perform an explicit contradiction check against your initial reply."
        )
        return False, reason

    return True, ""


def load_state(state_file: str = STATE_FILE, session_id: str | None = None) -> dict:
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                source = data
                if session_id:
                    sessions = data.get("sessions")
                    if isinstance(sessions, dict) and session_id in sessions and isinstance(sessions[session_id], dict):
                        source = sessions[session_id]
                    else:
                        fresh = _default_state(0.0)
                        fresh["_session_id"] = session_id
                        return fresh

                base = _default_state(source.get("last_time", 0.0))
                for k in base:
                    if k in source:
                        base[k] = source[k]
                if session_id:
                    base["_session_id"] = session_id
                return base
        except Exception:
            pass
    fresh = _default_state(0.0)
    if session_id:
        fresh["_session_id"] = session_id
    return fresh


def save_state(state: dict, state_file: str = STATE_FILE, session_id: str | None = None):
    try:
        data = {}
        if os.path.exists(state_file):
            try:
                with open(state_file, "r") as f:
                    content = json.load(f)
                    if isinstance(content, dict):
                        data = content
            except Exception:
                data = {}

        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}

        now_ts = time.time()
        # Prune sessions older than TURN_STATE_EXPIRY_SECONDS
        for s_key in list(sessions.keys()):
            s_val = sessions.get(s_key)
            if isinstance(s_val, dict):
                s_last = float(s_val.get("last_time", 0.0) or s_val.get("turn_start_time", 0.0) or 0.0)
                if s_last > 0.0 and (now_ts - s_last) > TURN_STATE_EXPIRY_SECONDS:
                    sessions.pop(s_key, None)

        sid = session_id or (state.get("_session_id") if isinstance(state, dict) else None)
        if sid:
            sess_copy = {k: v for k, v in state.items() if not k.startswith("_")}
            sessions[str(sid)] = sess_copy

        # Sync current state to top-level fields for backwards compatibility
        for k, v in state.items():
            if not k.startswith("_"):
                data[k] = v
        data["sessions"] = sessions

        parent_dir = os.path.dirname(state_file)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        with open(state_file, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def check_hook_python_syntax(name: str, args: dict, cwd: str = "/workspace") -> tuple[bool, str]:
    """Self-Preservation AST Guard: prevent writing invalid Python syntax to .agents/hooks/*.py."""
    target_file = args.get("TargetFile") or args.get("Path") or ""
    if not target_file:
        return True, ""
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    abs_path = os.path.abspath(os.path.join(norm_cwd, target_file)).replace("\\", "/")
    if not abs_path.endswith(".py"):
        return True, ""
    if "/.agents/hooks/" not in abs_path and os.path.basename(abs_path) not in ("batch_guard.py", "search_guard.py", "time_context.py"):
        return True, ""

    candidate_src = None
    if name in ("write_file", "write_to_file"):
        candidate_src = args.get("Content") if "Content" in args else args.get("CodeContent")
    elif name in ("edit_file", "replace_file_content") and os.path.exists(abs_path):
        target_str = args.get("Target") if "Target" in args else args.get("TargetContent")
        replacement_str = args.get("Replacement") if "Replacement" in args else args.get("ReplacementContent", "")
        if isinstance(target_str, str) and target_str:
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    current_src = f.read()
                if target_str in current_src:
                    candidate_src = current_src.replace(target_str, replacement_str or "")
            except Exception:
                pass

    if isinstance(candidate_src, str) and candidate_src.strip():
        try:
            tree = ast.parse(candidate_src, filename=abs_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Constant):
                    return False, (
                        f"Hook Self-Preservation Syntax Guard: Refusing to write '{target_file}' — line {getattr(node, 'lineno', '?')} "
                        f"attempts to call literal {repr(node.func.value)} as a function (likely a missing comma before '(')."
                    )
        except SyntaxError as e:
            return False, (
                f"Hook Self-Preservation Syntax Guard: Refusing to write invalid Python syntax to '{target_file}' "
                f"(line {e.lineno}: {e.msg}). Fix the syntax error before modifying hook scripts."
            )
    return True, ""


def _has_active_delete_approval(db_path: str = "/workspace/data/forge.db") -> bool:
    """Return True strictly when executing inside an approved FORGE Decision Card subprocess."""
    return os.environ.get("FORGE_APPROVED_CARD_EXECUTION") == "1"


def _is_safe_ephemeral_delete_target(token: str, cwd: str = "/workspace") -> bool:
    clean = token.strip().strip("'\"")
    if not clean or clean.startswith("-"):
        return True
    if clean.endswith((".tmp", ".pyc", ".pyo")) or "__pycache__" in clean:
        return True
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return True
    abs_p = os.path.abspath(os.path.join(norm_cwd, clean)).replace("\\", "/")
    if abs_p.startswith(("/tmp/", "/workspace/scratch/")):
        return True
    if "/scratch/" in abs_p or "/__pycache__" in abs_p:
        return True
    return False


_DECISION_CARD_APPROVAL_HINT = (
    "Stage a FORGE Decision Card via 'python3 /workspace/tools/forge.py propose ... --post' "
    "and wait for Ryan's explicit approval (executes with FORGE_APPROVED_CARD_EXECUTION=1)."
)

_DESTRUCTIVE_SQL_DROP_RE = re.compile(
    r"\b(?:DROP\s+(?:TEMP(?:ORARY)?\s+)?(?:TABLE|DATABASE)|TRUNCATE\s+TABLE)\b",
    re.IGNORECASE,
)
_REMOTE_OR_SERVER_DB_RE = re.compile(
    r"\b(?:psql|mysql|mariadb|dropdb|ssh|psycopg2?|asyncpg|pymysql|sqlalchemy)\b|\brun_ssh\b",
    re.IGNORECASE,
)
_SQLITE_CLI_VALUE_FLAGS = {"-cmd", "-init", "-nullvalue", "-separator", "-newline"}
_GIT_PUSH_VALUE_FLAGS = {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}
_SSH_VALUE_FLAGS = {
    "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
    "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}
_COMPOSE_PRE_VALUE_FLAGS = {
    "-f", "--file", "-p", "--project-name", "--project-directory",
    "--env-file", "--profile", "-H", "--host", "--context", "-c",
    "--ansi", "--progress", "--parallel",
}
_COMPOSE_SCOPE_FLAGS = {"-f", "--file", "-p", "--project-name", "--project-directory"}

_CF_API_MUTATION_METHOD_RE = re.compile(
    r"(?:"
    r"(?:-X|--request|--method)\s*=?\s*['\"]?(?:POST|PUT|PATCH)['\"]?\b|"
    r"\b(?:POST|PUT|PATCH)\s+https?://api\.cloudflare\.com\b|"
    r"\.(?:post|put|patch)\s*\(|"
    r"\bmethod\s*[:=]\s*['\"](?:POST|PUT|PATCH)['\"]"
    r")",
    re.IGNORECASE,
)
_CF_CURL_DATA_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-d|--data|--data-raw|--data-binary|--data-urlencode|--json)(?:\s|=)"
)


def _strip_outer_quotes(tok: str) -> str:
    s = tok.strip()
    if len(s) >= 2 and s[0] == s[-1]:
        if s[0] == "'":
            return s[1:-1]
        if s[0] == '"':
            return s[1:-1].replace('\\"', '"')
    return s


def _resolves_to_secrets_path(path_str: str, cwd: str = "/workspace") -> bool:
    """Return True if path_str resolves into /secrets or /secrets/*."""
    if not path_str:
        return False
    clean = _strip_outer_quotes(path_str)
    if not clean:
        return False
    norm_p = posixpath.normpath(_norm_posix(clean, cwd or "/workspace"))
    if norm_p == "/secrets" or norm_p.startswith("/secrets/"):
        return True
    try:
        abs_p = os.path.abspath(os.path.join(cwd or "/workspace", clean))
        real_p = os.path.realpath(abs_p).replace("\\", "/")
        if real_p == "/secrets" or real_p.startswith("/secrets/"):
            return True
    except Exception:
        pass
    return False


def check_secrets_mutation_gate(target_file: str, cwd: str = "/workspace") -> tuple[bool, str]:
    """Block direct file writes or edits targeting /secrets/* unless FORGE_APPROVED_CARD_EXECUTION=1."""
    if not target_file:
        return True, ""
    if os.environ.get("FORGE_APPROVED_CARD_EXECUTION") == "1":
        return True, ""
    if _resolves_to_secrets_path(target_file, cwd=cwd):
        return False, (
            f"Hard Destructive & Secrets Gate Triggered: Direct file write/edit to '{target_file}' ('/secrets/*') "
            f"is prohibited without explicit human approval. {_DECISION_CARD_APPROVAL_HINT}"
        )
    return True, ""


_FORGE_PROHIBITED_WRITE_TOOLS = frozenset(
    {
        "edit_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write_file",
        "write_to_file",
    }
)


def _normalize_prohibited_rel_path(path_str: str) -> str:
    """Normalize a relative POSIX file path for FORGE boundary comparison."""
    s = (path_str or "").strip().replace("\\", "/")
    if not s:
        return ""
    norm = posixpath.normpath(s).lstrip("/")
    if norm in ("", "."):
        return ""
    return norm


def check_forge_prohibited_file_gate(name: str, args: dict, cwd: str = "/workspace") -> tuple[bool, str]:
    """Block FORGE workers from editing or writing files listed in FORGE_PROHIBITED_FILES."""
    if os.environ.get("FORGE_WORKER_ACTIVE") != "1":
        return True, ""
    if name not in _FORGE_PROHIBITED_WRITE_TOOLS:
        return True, ""
    raw_prohibited = os.environ.get("FORGE_PROHIBITED_FILES", "").strip()
    if not raw_prohibited:
        return True, ""
    try:
        parsed = json.loads(raw_prohibited)
    except Exception:
        return True, ""
    if not isinstance(parsed, list) or not parsed:
        return True, ""

    prohibited_set = {
        norm_p
        for item in parsed
        if isinstance(item, str)
        for norm_p in (_normalize_prohibited_rel_path(item),)
        if norm_p
    }
    if not prohibited_set:
        return True, ""

    args = args or {}
    target_file = (
        args.get("TargetFile")
        or args.get("AbsolutePath")
        or args.get("Path")
        or args.get("path")
        or ""
    )
    if not target_file or not isinstance(target_file, str):
        return True, ""

    eff_cwd = cwd or "/workspace"
    norm_cwd = _norm_posix(eff_cwd, "/")
    norm_target = _norm_posix(target_file, eff_cwd)

    candidates: list[str] = []
    try:
        rel = posixpath.relpath(norm_target, norm_cwd)
        if rel not in ("", ".") and not rel.startswith("../") and rel != "..":
            candidates.append(rel)
    except Exception:
        pass

    lex_target = target_file.strip().replace("\\", "/")
    if lex_target.startswith("/"):
        lex_target_norm = posixpath.normpath(lex_target)
    else:
        lex_cwd = posixpath.normpath(eff_cwd.replace("\\", "/"))
        lex_target_norm = posixpath.normpath(lex_cwd.rstrip("/") + "/" + lex_target)
    try:
        lex_cwd_norm = posixpath.normpath(eff_cwd.replace("\\", "/"))
        lex_rel = posixpath.relpath(lex_target_norm, lex_cwd_norm)
        if lex_rel not in ("", ".") and not lex_rel.startswith("../") and lex_rel != "..":
            candidates.append(lex_rel)
    except Exception:
        pass

    for p_cand in (norm_target, lex_target_norm):
        m_wt = re.match(r"^/tmp/forge-wt-[^/]+/(.+)$", p_cand)
        if m_wt:
            candidates.append(m_wt.group(1))
        if p_cand.startswith("/workspace/"):
            candidates.append(p_cand[len("/workspace/"):])

    for cand in candidates:
        norm_cand = _normalize_prohibited_rel_path(cand)
        if norm_cand in prohibited_set:
            return False, (
                f"FORGE Boundary Guard Triggered: '{norm_cand}' is in FORGE_PROHIBITED_FILES for this chunk "
                f"(target: '{target_file}'). Modify ONLY your Allowed Target Files and corresponding unit tests."
            )

    return True, ""


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


def _extract_wrapped_shell_commands(cmd: str) -> list[str]:
    """Extract inner command payloads from ssh, bash/sh -c, and forge.py/detached_runner --command/-c wrappers."""
    inner: list[str] = []
    for m in re.finditer(
        r"(?:--command|\b(?:bash|sh|zsh|dash)\s+(?:-[a-zA-Z]+\s+)*-c)(?:=|\s+)(['\"])([\s\S]*?)\1",
        cmd,
    ):
        val = m.group(2).replace('\\"', '"').strip() if m.group(1) == '"' else m.group(2).strip()
        if val:
            inner.append(val)
    for m in re.finditer(
        r"\b(?:forge\.py|tools\.forge|detached_runner\.py)\b[^\n;|&]*?\s-c(?:=|\s+)(['\"])([\s\S]*?)\1",
        cmd,
    ):
        val = m.group(2).replace('\\"', '"').strip() if m.group(1) == '"' else m.group(2).strip()
        if val:
            inner.append(val)
    for seg in _split_unquoted_shell_segments(cmd):
        m_ssh = re.match(r"^(?:sudo\s+|env\s+\S+\s+)*ssh\b(?P<rest>.*)$", seg.strip())
        if not m_ssh:
            continue
        tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", m_ssh.group("rest").strip())
        idx = 0
        host_seen = False
        while idx < len(tokens):
            raw_tok = tokens[idx]
            tok = _strip_outer_quotes(raw_tok)
            if not host_seen:
                if tok in _SSH_VALUE_FLAGS:
                    idx += 2
                    continue
                if tok.startswith("-") and len(tok) > 2 and tok[:2] in _SSH_VALUE_FLAGS:
                    idx += 1
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
                host_seen = True
                idx += 1
                continue
            rem = tokens[idx:]
            if len(rem) == 1:
                unq = _strip_outer_quotes(rem[0]).strip()
                if unq:
                    inner.append(unq)
            elif rem:
                joined_rem = " ".join(_strip_outer_quotes(t) for t in rem).strip()
                if joined_rem:
                    inner.append(joined_rem)
            break
    return inner


def _is_pure_decision_card_staging_cmd(cmd: str) -> bool:
    """Allow staging a FORGE Decision Card via a single unchained `forge.py propose` / `stage-decision` call."""
    stripped = cmd.strip()
    if not re.match(
        r"^(?:python[0-9.]*\s+)?(?:['\"]?[^\s'\"]*?/)?forge\.py\s+(?:propose|stage-decision)\b",
        stripped,
    ):
        return False
    outside_single = re.sub(r"'(?:[^']*)'", "''", stripped)
    if re.search(r"(?<!\\)\$\(|(?<!\\)`", outside_single):
        return False
    unquoted = re.sub(r"'(?:[^']*)'|\"(?:\\.|[^\"\\])*\"", "''", stripped)
    if any(sep in unquoted for sep in ("&&", "||", ";", "|", "\n", "`", "$(", ">", "<(", "&")):
        return False
    return True


def _extract_sqlite_targets_from_segment(seg: str) -> list[str]:
    """Extract SQLite database path targets referenced in a shell/python command segment."""
    targets: list[str] = []
    m_cli = re.match(r"^(?:sudo\s+|env\s+\S+\s+)*(?:\S+/)?sqlite3(?=\s)(?P<rest>.*)$", seg.strip())
    if m_cli:
        tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", m_cli.group("rest").strip())
        idx = 0
        while idx < len(tokens):
            tok = _strip_outer_quotes(tokens[idx])
            if tok in _SQLITE_CLI_VALUE_FLAGS:
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            if tok:
                targets.append(tok)
            break
    for m_conn in re.finditer(r"\bsqlite3\.connect\s*\(\s*(?:[fFrRuU]*)(['\"])(.*?)\1", seg):
        if m_conn.group(2).strip():
            targets.append(m_conn.group(2).strip())
    for m_db in re.finditer(r"(?P<db>[^\s'\"();,]+?\.(?:db|sqlite|sqlite3))\b", seg):
        cand = m_db.group("db").strip()
        if cand and cand not in targets:
            targets.append(cand)
    return targets


def _is_safe_tmp_sql_target(raw_target: str, cwd: str = "/workspace") -> bool:
    """Return True only if raw_target is an ephemeral :memory: or /tmp/* SQLite test fixture (excluding /tmp/forge-wt-*)."""
    clean = _strip_outer_quotes(raw_target)
    if not clean:
        return False
    if clean == ":memory:" or "mode=memory" in clean:
        return True
    if clean.startswith("file:"):
        clean = clean[5:].split("?")[0]
    if not clean:
        return False
    tmp_root = tempfile.gettempdir().replace("\\", "/").rstrip("/")
    if "{" in clean or "$" in clean:
        return (
            clean.startswith("/tmp/")
            and not clean.startswith("/tmp/forge-wt-")
            and ".." not in clean
        )
    norm_t = posixpath.normpath(_norm_posix(clean, cwd or "/workspace"))
    in_tmp = (
        norm_t.startswith("/tmp/")
        or (bool(tmp_root) and norm_t.startswith(tmp_root + "/"))
    ) and not norm_t.startswith("/tmp/forge-wt-")
    if not in_tmp:
        return False
    try:
        if os.path.exists(norm_t):
            real_t = os.path.realpath(norm_t).replace("\\", "/")
            if not (
                real_t.startswith("/tmp/")
                or (bool(tmp_root) and real_t.startswith(tmp_root + "/"))
            ) or real_t.startswith("/tmp/forge-wt-"):
                return False
    except Exception:
        return False
    return True


def _strip_heads_prefix(ref_name: str) -> str:
    clean = ref_name.strip()
    if clean.startswith("refs/heads/"):
        return clean[len("refs/heads/") :]
    return clean


def _is_git_force_push_to_main_or_master(seg: str) -> bool:
    """Return True if segment executes a git force-push targeting main or master."""
    m = re.search(
        r"\bgit\b(?:\s+(?:-[Cc]\s+\S+|--(?:git-dir|work-tree)(?:=\S+|\s+\S+)|--no-pager|--bare))*\s+push\b(?P<rest>.*)$",
        seg.strip(),
    )
    if not m:
        return False
    tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", m.group("rest").strip())
    has_force_flag = False
    positional: list[str] = []
    idx = 0
    while idx < len(tokens):
        raw_tok = tokens[idx]
        if raw_tok.startswith((">", "2>", "&>", "1>")):
            break
        tok = _strip_outer_quotes(raw_tok)
        if tok in _GIT_PUSH_VALUE_FLAGS:
            idx += 2
            continue
        if tok in ("--force", "--force-if-includes") or tok.startswith("--force="):
            has_force_flag = True
            idx += 1
            continue
        if tok == "--force-with-lease" or tok.startswith("--force-with-lease="):
            has_force_flag = True
            if tok.startswith("--force-with-lease="):
                lease_ref = tok.split("=", 1)[1].split(":", 1)[0]
                if _strip_heads_prefix(lease_ref) in ("main", "master"):
                    return True
            idx += 1
            continue
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1 and tok[1:].isalpha() and "f" in tok[1:]:
            has_force_flag = True
            idx += 1
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        positional.append(tok)
        idx += 1

    for p_idx, ref_tok in enumerate(positional):
        is_plus_ref = ref_tok.startswith("+")
        if p_idx == 0 and not is_plus_ref and not (len(positional) == 1 and has_force_flag):
            continue
        body = ref_tok.lstrip("+")
        dst = body.split(":", 1)[1] if ":" in body else body
        if _strip_heads_prefix(dst) in ("main", "master") and (is_plus_ref or has_force_flag):
            return True
    return False


def _is_github_repo_admin_or_visibility_mutation(seg: str) -> tuple[bool, str]:
    """Detect GitHub repository deletion, archival, or visibility mutations via gh CLI or REST API."""
    if re.search(r"\bgh\s+repo\s+(?:delete|archive)\b", seg, re.IGNORECASE):
        return True, "GitHub repository deletion/archival ('gh repo delete/archive')"
    if re.search(r"\bgh\s+repo\s+edit\b", seg, re.IGNORECASE) and re.search(
        r"--(?:visibility|accept-visibility-change-consequences)\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "GitHub repository visibility change ('gh repo edit --visibility')"
    if re.search(r"\bDELETE\s+(?:https?://api\.github\.com)?/?repos/", seg, re.IGNORECASE):
        return True, "GitHub API repository deletion ('DELETE /repos/...')"
    has_delete_method = bool(
        re.search(
            r"(?:-X|--method|--request)\s*=?\s*['\"]?DELETE['\"]?\b|\.delete\s*\(",
            seg,
            re.IGNORECASE,
        )
    )
    has_repos_endpoint = bool(
        re.search(
            r"(?:api\.github\.com/repos/|(?:^|[\s'\"=])/?repos/)[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?",
            seg,
            re.IGNORECASE,
        )
    )
    if has_delete_method and has_repos_endpoint:
        return True, "GitHub API repository deletion ('DELETE /repos/...')"
    if re.search(
        r"""(?:\bvisibility['"]?\s*[:=]\s*['"]?public\b|\bprivate['"]?\s*[:=]\s*['"]?false\b)""",
        seg,
        re.IGNORECASE,
    ):
        return True, "GitHub repository public visibility mutation ('visibility=public' / 'private=false')"
    if has_repos_endpoint and re.search(r"""\barchived['"]?\s*[:=]\s*['"]?true\b""", seg, re.IGNORECASE):
        return True, "GitHub API repository archival ('archived=true')"
    return False, ""


def _is_cloudflare_exposure_mutation(seg: str) -> tuple[bool, str]:
    """Detect public Cloudflare Tunnel or DNS record mutations."""
    if re.search(r"\bcloudflared\b[^\n;|&]*?\btunnel\b[^\n;|&]*?\broute\b", seg, re.IGNORECASE):
        return True, "Public Cloudflare Tunnel route mutation ('cloudflared tunnel route')"
    if "api.cloudflare.com" in seg.lower() and re.search(r"\b(?:cfd_tunnel|dns_records)\b", seg, re.IGNORECASE):
        if _CF_API_MUTATION_METHOD_RE.search(seg):
            return True, "Public Cloudflare API mutation ('api.cloudflare.com' cfd_tunnel/dns_records POST/PUT/PATCH)"
        if _CF_CURL_DATA_FLAG_RE.search(seg) and not re.search(
            r"(?:-X|--request)\s*=?\s*['\"]?GET['\"]?\b|\s-G\b",
            seg,
            re.IGNORECASE,
        ):
            return True, "Public Cloudflare API mutation ('api.cloudflare.com' cfd_tunnel/dns_records data POST)"
    if re.search(r"\bconfirm_public_exposure\s*=\s*True\b", seg) or re.search(
        r"\bcloudflare\.py\s+(?:create_record|update_record)\b",
        seg,
    ):
        return True, "Public Cloudflare DNS exposure mutation ('cloudflare.py')"
    return False, ""


def _is_secrets_shell_overwrite(seg: str, cwd: str = "/workspace") -> bool:
    """Detect shell redirects, file copy/move/in-place tools, or inline scripts writing to /secrets/*."""
    for m_redir in re.finditer(r"(?:[0-9&]*>>?\|?)\s*('[^']*'|\"[^\"]*\"|[^\s;|&><]+)", seg):
        if _resolves_to_secrets_path(m_redir.group(1), cwd=cwd):
            return True

    m_tee = re.search(r"\btee\b(?P<rest>.*)$", seg)
    if m_tee:
        for raw_tok in re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", m_tee.group("rest")):
            tok = _strip_outer_quotes(raw_tok)
            if tok.startswith("-"):
                continue
            if _resolves_to_secrets_path(tok, cwd=cwd):
                return True

    m_cp_mv = re.match(r"^(?:sudo\s+)?(?:cp|mv|install|rsync|ln)\b(?P<rest>.*)$", seg.strip())
    if m_cp_mv:
        tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", m_cp_mv.group("rest").strip())
        pos_paths: list[str] = []
        idx = 0
        while idx < len(tokens):
            raw_tok = tokens[idx]
            if raw_tok.startswith((">", "2>", "&>")):
                break
            tok = _strip_outer_quotes(raw_tok)
            if tok in ("-t", "--target-directory") and idx + 1 < len(tokens):
                if _resolves_to_secrets_path(tokens[idx + 1], cwd=cwd):
                    return True
                idx += 2
                continue
            if tok.startswith("--target-directory="):
                if _resolves_to_secrets_path(tok.split("=", 1)[1], cwd=cwd):
                    return True
                idx += 1
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            pos_paths.append(tok)
            idx += 1
        if pos_paths and _resolves_to_secrets_path(pos_paths[-1], cwd=cwd):
            return True

    m_mut_tool = re.match(
        r"^(?:sudo\s+)?(?P<tool>sed|perl|truncate|touch|chmod|chown|rm|unlink|shred)\b(?P<rest>.*)$",
        seg.strip(),
    )
    if m_mut_tool:
        tool_name = m_mut_tool.group("tool")
        rest_str = m_mut_tool.group("rest")
        is_inplace = True
        if tool_name == "sed":
            is_inplace = bool(re.search(r"\s-[a-zA-Z]*i|--in-place\b", " " + rest_str))
        elif tool_name == "perl":
            is_inplace = bool(re.search(r"\s-[a-zA-Z]*i", " " + rest_str))
        if is_inplace:
            for raw_tok in re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", rest_str):
                tok = _strip_outer_quotes(raw_tok)
                if not tok.startswith("-") and _resolves_to_secrets_path(tok, cwd=cwd):
                    return True

    for m_dd in re.finditer(r"\bdd\b[^\n;|&]*?\bof=('[^']*'|\"[^\"]*\"|\S+)", seg):
        if _resolves_to_secrets_path(m_dd.group(1), cwd=cwd):
            return True

    for m_open in re.finditer(
        r"\bopen\s*\(\s*(?:[fFrRuU]*)(['\"])([^'\"]+)\1\s*(?:,\s*(['\"])([^'\"]+)\3|[^)]*?\bmode\s*=\s*(['\"])([^'\"]+)\5)",
        seg,
    ):
        p_val = m_open.group(2)
        mode_val = m_open.group(4) or m_open.group(6) or ""
        if any(flag in mode_val for flag in ("w", "a", "x", "+")) and _resolves_to_secrets_path(p_val, cwd=cwd):
            return True

    for m_pathlib in re.finditer(
        r"\bPath\s*\(\s*(['\"])([^'\"]+)\1\s*\)\s*\.\s*(?:write_text|write_bytes|unlink|touch|replace|rename)\s*\(",
        seg,
    ):
        if _resolves_to_secrets_path(m_pathlib.group(2), cwd=cwd):
            return True

    for m_py_fs in re.finditer(
        r"\b(?:shutil\.(?:copy\w*|move|rmtree)|os\.(?:replace|rename|remove|unlink|truncate))\s*\(([^)]*)\)",
        seg,
    ):
        str_args = [m_s.group(2) for m_s in re.finditer(r"(['\"])([^'\"]+)\1", m_py_fs.group(1))]
        if str_args and _resolves_to_secrets_path(str_args[-1], cwd=cwd):
            return True

    return False


def _is_unscoped_or_destructive_compose_down(seg: str) -> bool:
    """Return True if segment runs an unscoped `docker compose down` / `docker-compose down` or destroys volumes (`-v`/`--volumes`/`--rmi`)."""
    m = re.search(
        r"\bdocker(?:-compose|(?:\s+(?:-[Hc]\s+\S+|--(?:host|context)(?:=\S+|\s+\S+)))*\s+compose)\b(?P<rest>.*)$",
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
        if raw_tok.startswith((">", "2>", "&>")):
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
        if raw_tok.startswith((">", "2>", "&>")):
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


def _is_destructive_container_or_host_segment(seg: str, full_cmd: str) -> tuple[bool, str]:
    """Detect destructive Docker prune/volume/compose-down, stopping ContainerManager, and destructive NAS/host SSH commands."""
    if re.search(
        r"\bdocker\b(?:\s+(?:-[Hc]\s+\S+|--(?:host|context)(?:=\S+|\s+\S+)))*\s+system\s+prune\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "Destructive container prune ('docker system prune')"
    if re.search(
        r"\bdocker\b(?:\s+(?:-[Hc]\s+\S+|--(?:host|context)(?:=\S+|\s+\S+)))*\s+volume\s+(?:rm|remove|prune)\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "Destructive Docker volume deletion ('docker volume rm/prune')"
    if _is_unscoped_or_destructive_compose_down(seg):
        return True, "Unscoped or volume-destroying 'docker compose down'"
    if re.search(
        r"\b(?:stop|pause|disable|mask|kill|pkill|killall|uninstall)\b[^\n;|&]*\b(?:pkgctl-)?ContainerManager\b"
        r"|\b(?:pkgctl-)?ContainerManager\b[^\n;|&]*\b(?:stop|pause|disable|mask|kill|uninstall)\b"
        r"|--stop\s+(?:pkgctl-)?ContainerManager\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "Stopping Synology 'ContainerManager'"
    if re.search(r"\brm\b[^\n;|&]*?\x2fvolume1(?:\x2f|\b|['\"\s;|$])", seg):
        return True, "Destructive NAS volume deletion ('rm' targeting primary NAS volume)"
    if re.search(r"\bsynopkg\s+uninstall\b", seg, re.IGNORECASE):
        return True, "Destructive Synology package removal ('synopkg uninstall')"
    if re.search(
        r"^(?:sudo\s+|nohup\s+|env\s+\S+\s+)*(?:/usr/sbin/|/sbin/)?(?:reboot|shutdown|poweroff)\b",
        seg.strip(),
        re.IGNORECASE,
    ):
        return True, "Destructive host power command ('reboot' / 'shutdown' / 'poweroff')"
    if re.search(r"\b(?:ssh|run_ssh)\b", full_cmd) and re.search(
        r"(?<!\.)\b(?:reboot|shutdown|poweroff)\b",
        seg,
        re.IGNORECASE,
    ):
        return True, "Destructive remote SSH power command ('reboot' / 'shutdown' / 'poweroff')"
    return False, ""


def check_destructive_system_and_exposure_gate(cmd: str, cwd: str = "/workspace") -> tuple[bool, str]:
    """Deterministically block destructive SQL/git/GitHub/Cloudflare/secrets/container/NAS-SSH operations
    unless os.environ.get('FORGE_APPROVED_CARD_EXECUTION') == '1'.
    """
    if not cmd or not cmd.strip():
        return True, ""
    if os.environ.get("FORGE_APPROVED_CARD_EXECUTION") == "1":
        return True, ""
    if _is_pure_decision_card_staging_cmd(cmd):
        return True, ""

    candidate_commands: list[str] = [cmd]
    lvl1 = _extract_wrapped_shell_commands(cmd)
    candidate_commands.extend(lvl1)
    for sub in lvl1:
        candidate_commands.extend(_extract_wrapped_shell_commands(sub))

    all_segments: list[str] = []
    for cand in candidate_commands:
        segs = _split_unquoted_shell_segments(cand)
        all_segments.extend(segs)
        if "<<" in cand and cand.strip() not in all_segments:
            all_segments.append(cand.strip())

    # 1. SQL table/database drops (DROP TABLE, DROP DATABASE, TRUNCATE TABLE) outside /tmp/ test fixtures
    for seg in all_segments:
        if _DESTRUCTIVE_SQL_DROP_RE.search(seg):
            if _REMOTE_OR_SERVER_DB_RE.search(cmd) or _REMOTE_OR_SERVER_DB_RE.search(seg):
                return False, (
                    "Hard Destructive Gate Triggered: SQL 'DROP TABLE' / 'DROP DATABASE' / 'TRUNCATE TABLE' "
                    f"against non-/tmp databases is prohibited without explicit approval. {_DECISION_CARD_APPROVAL_HINT}"
                )
            sql_targets = _extract_sqlite_targets_from_segment(seg)
            if not sql_targets or not all(_is_safe_tmp_sql_target(t, cwd=cwd) for t in sql_targets):
                return False, (
                    "Hard Destructive Gate Triggered: SQL 'DROP TABLE' / 'DROP DATABASE' / 'TRUNCATE TABLE' "
                    f"outside '/tmp/' test fixtures is prohibited without explicit approval. {_DECISION_CARD_APPROVAL_HINT}"
                )

    # 2..6. Inspect all command segments (including unwrapped ssh / -c / --command payloads)
    for seg in all_segments:
        if _is_git_force_push_to_main_or_master(seg):
            return False, (
                "Hard Destructive Gate Triggered: Git force-push ('--force', '-f', '--force-with-lease', or '+ref') "
                f"targeting 'main' or 'master' is prohibited without explicit approval. {_DECISION_CARD_APPROVAL_HINT}"
            )

        is_gh_bad, gh_desc = _is_github_repo_admin_or_visibility_mutation(seg)
        if is_gh_bad:
            return False, (
                f"Hard Destructive & Exposure Gate Triggered: {gh_desc} is prohibited without explicit approval. "
                f"{_DECISION_CARD_APPROVAL_HINT}"
            )

        is_cf_bad, cf_desc = _is_cloudflare_exposure_mutation(seg)
        if is_cf_bad:
            return False, (
                f"Public Exposure Gate Triggered: {cf_desc} violates the 2-Layer Private LAN Default and is prohibited "
                f"without explicit human approval. {_DECISION_CARD_APPROVAL_HINT}"
            )

        if _is_secrets_shell_overwrite(seg, cwd=cwd):
            return False, (
                "Hard Destructive & Secrets Gate Triggered: Overwriting or mutating '/secrets/*' via shell redirect "
                f"or command is prohibited without explicit human approval. {_DECISION_CARD_APPROVAL_HINT}"
            )

        is_host_bad, host_desc = _is_destructive_container_or_host_segment(seg, cmd)
        if is_host_bad:
            return False, (
                f"Hard Destructive Host/Container Gate Triggered: {host_desc} is prohibited without explicit human approval. "
                f"{_DECISION_CARD_APPROVAL_HINT}"
            )

    return True, ""


def check_file_deletion_gate(cmd: str, cwd: str = "/workspace") -> tuple[bool, str]:
    """Enforce human approval via FORGE Decision Card before deleting tracked/persistent files."""
    if not cmd:
        return True, ""
    if os.environ.get("FORGE_APPROVED_CARD_EXECUTION") == "1":
        return True, ""
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True, ""
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return True, ""

    # 1. Block direct SQL/shell tampering with decision_cards / forge_decisions approval status
    if re.search(
        r"\b(?:UPDATE\s+(?:OR\s+\w+\s+)?(?:decision_cards|forge_decisions)\b|(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+(?:decision_cards|forge_decisions)\b)",
        cmd,
        re.IGNORECASE,
    ):
        return False, (
            "Decision Card Tamper Blocked: Modifying 'decision_cards' or 'forge_decisions' approval status directly via SQL or shell is prohibited. "
            "Only Ryan can approve a Decision Card via the interactive Discord button."
        )

    # 2. Remote SSH file deletion check
    if re.search(r"\bssh\b", cmd) and re.search(r"\b(?:rm|unlink)\b|\bfind\b[^\n;|]*-delete\b", cmd):
        if not _has_active_delete_approval():
            return False, (
                "File Deletion Gate Triggered (Remote NAS/SSH): Deleting files on remote hosts over SSH requires explicit human approval from Ryan. "
                "Stage a Decision Card (action_type='file_delete') first."
            )
        return True, ""

    # 3. Inline Python os.remove / os.unlink / shutil.rmtree / .unlink() check
    py_del_m = re.findall(
        r"(?:os\.(?:remove|unlink)|shutil\.rmtree)\s*\(\s*(['\"])(.*?)\1",
        cmd,
    )
    for _, target in py_del_m:
        if not _is_safe_ephemeral_delete_target(target, cwd):
            if not _has_active_delete_approval():
                return False, (
                    f"File Deletion Gate Triggered: Deleting '{target}' requires explicit human approval from Ryan via an approved "
                    "FORGE Decision Card (action_type='file_delete')."
                )

    # 4. find <path> ... -delete check
    for find_m in re.finditer(r"\bfind\s+([^\s;\"'|&>]+)[^\n;|]*?-delete\b", cmd):
        find_root = find_m.group(1)
        if not _is_safe_ephemeral_delete_target(find_root, cwd):
            if not _has_active_delete_approval():
                return False, (
                    f"File Deletion Gate Triggered: 'find {find_root} ... -delete' targets a protected path and requires explicit human approval from Ryan."
                )

    # 5. Shell rm / unlink / git rm check
    segments = re.split(r"(?:&&|\|\||;|\n)", cmd)
    for seg in segments:
        seg_s = seg.strip()
        m = re.match(r"^(?:sudo\s+)?(?:git\s+rm|rm|unlink)\b(.*)$", seg_s)
        if not m:
            continue
        rest = m.group(1).strip()
        tokens = re.findall(r"(?:'[^']*'|\"[^\"]*\"|\S+)", rest)
        for tok in tokens:
            if tok in ("&&", "||", ";", "|") or tok.startswith(">") or tok.startswith("2>"):
                break
            if not _is_safe_ephemeral_delete_target(tok, cwd):
                if not _has_active_delete_approval():
                    return False, (
                        f"File Deletion Gate Triggered: Deleting '{tok}' requires explicit human approval from Ryan via an approved "
                        "FORGE Decision Card (action_type='file_delete')."
                    )
    return True, ""


def _is_single_segment_unscoped_test(seg: str) -> bool:
    seg_clean = seg.strip()
    if not seg_clean:
        return False
    # Check go test ./... or bare go test
    m_go = re.match(r"^go\s+test(?:\s+(.*))?$", seg_clean)
    if m_go:
        go_args = (m_go.group(1) or "").strip()
        go_tokens = [t for t in go_args.split() if not t.startswith("-")]
        if not go_tokens or "./..." in go_tokens or "..." in go_tokens:
            return True
        return False

    m = re.match(r"^(?:pytest|python[0-9.]*\s+-m\s+pytest)(?:\s+(.*))?$", seg_clean)
    if not m:
        return False
    args = m.group(1) or ""
    tokens = args.split()
    if not tokens:
        return True
    if any(t.endswith(".py") or ".py::" in t or t.startswith("-k") or t.startswith("-m") for t in tokens):
        return False
    if "-k" in tokens or "-m" in tokens:
        return False
    generic_targets = {"tests", "tests/", "tools", "tools/", ".", "./"}
    return all(t.startswith("-") or t in generic_targets for t in tokens)


def is_unscoped_pytest(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    if not cmd_clean:
        return False
    # Split on top-level && or ; to catch 'cd /workspace && pytest' or 'ruff check . && pytest tests/'
    unquoted = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "'quoted'", cmd_clean)
    if "&&" not in unquoted and ";" not in unquoted and "||" not in unquoted and "\n" not in unquoted:
        return _is_single_segment_unscoped_test(cmd_clean)
    for seg in re.split(r"(?:&&|\|\||;|\n)", cmd_clean):
        if _is_single_segment_unscoped_test(seg):
            return True
    return False


def is_single_git_lifecycle_cmd(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    if " && " in cmd_clean or " || " in cmd_clean or "; " in cmd_clean or "\n" in cmd_clean:
        return False
    git_lifecycle_pattern = r"^(?:git\s+(?:add|commit|push|checkout\s+-b|switch\s+-c)|gh\s+pr\s+create)\b"
    return bool(re.search(git_lifecycle_pattern, cmd_clean))


def is_inline_python_probe(cmd: str) -> bool:
    """Detect inline python -c or heredoc (python3 - << 'EOF') probing commands, even when chained with &&."""
    cmd_clean = cmd.strip()
    first_line = cmd_clean.splitlines()[0].strip() if cmd_clean else ""
    if bool(re.match(r"^python[0-9.]*\s+(?:-c\b|-\s*<<|<<)", first_line)):
        return True
    unquoted = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", cmd_clean)
    if " && " in unquoted or " || " in unquoted or "; " in unquoted:
        return False
    return False


def is_unbounded_ha_sqlite_query(cmd: str) -> bool:
    """Detect SQLite queries against home-assistant_v2.db that lack last_updated_ts bounds or read-only mode."""
    if "home-assistant_v2.db" not in cmd:
        return False
    if "SELECT" not in cmd.upper():
        return False
    if "last_updated_ts >" not in cmd and "last_updated_ts >=" not in cmd:
        return True
    return False


_SQL_SELECT_STAR_RE = re.compile(r"\bSELECT\s+(?:DISTINCT\s+)?\*\s+FROM\b", re.IGNORECASE)
_SQL_AGGREGATE_ONLY_RE = re.compile(
    r"^\s*(?:EXPLAIN(?:\s+QUERY\s+PLAN)?\s+)?SELECT\s+(?:COUNT|MIN|MAX|SUM|AVG|TOTAL|GROUP_CONCAT)\s*\(",
    re.IGNORECASE,
)
_SQL_TABLE_EXTRACT_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)\b", re.IGNORECASE)
_SQL_CTE_NAME_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\(", re.IGNORECASE)
_DB_PATH_EXTRACT_RE = re.compile(r"(?P<db>[^\s'\"()]+?\.(?:db|sqlite|sqlite3))\b")


def _extract_sql_queries_from_cmd(cmd: str) -> list[tuple[str | None, str, bool]]:
    """Extract (db_path, sql_query, is_cli) tuples from sqlite3 CLI invocations and Python sqlite3 calls."""
    if not cmd or "SELECT" not in cmd.upper():
        return []
    results: list[tuple[str | None, str, bool]] = []

    # 1. sqlite3 CLI invocations: sqlite3 [options] <db_path> "<sql>" or '<sql>'
    for m in re.finditer(
        r"\bsqlite3\s+(?:-[a-zA-Z0-9_-]+(?:\s+\"[^\"]*\"|\s+'[^']*')?\s+)*([^\s'\"|;&]+\.(?:db|sqlite|sqlite3))\s+(\"([^\"]+)\"|'([^']+)')",
        cmd,
    ):
        db_p = m.group(1)
        sql_q = m.group(3) if m.group(3) is not None else m.group(4)
        if sql_q and re.search(r"\bSELECT\b", sql_q, re.IGNORECASE):
            results.append((db_p, sql_q.strip(), True))

    if results:
        return results

    # 2. Inline Python / script strings with sqlite3.connect / .execute / pd.read_sql
    db_match = _DB_PATH_EXTRACT_RE.search(cmd)
    db_path = db_match.group("db") if db_match else None
    if db_path and db_path.startswith("file:"):
        db_path = db_path[5:].split("?")[0]

    for m in re.finditer(
        r"(?:\.execute|\.executemany|read_sql(?:_query)?)\s*\(\s*([fFrR]*)(['\"]{3}|['\"])([\s\S]*?)\2",
        cmd,
    ):
        sql_q = m.group(3).strip()
        if re.search(r"\bSELECT\b", sql_q, re.IGNORECASE):
            results.append((db_path, sql_q, False))

    return results


def _introspect_sqlite_table_columns(db_path: str, table_names: list[str], cwd: str = "/workspace") -> str:
    """Return formatted 'table(col1, col2, ...)' hints for existing local SQLite tables."""
    if not db_path:
        return ""
    clean_db = db_path[5:].split("?")[0] if db_path.startswith("file:") else db_path
    norm_cwd = os.path.abspath(cwd or "/workspace")
    abs_db = os.path.abspath(os.path.join(norm_cwd, clean_db))
    if not os.path.exists(abs_db):
        return ""
    hints: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{abs_db}?mode=ro", uri=True, timeout=1.5)
        try:
            if not table_names:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 25"
                )
                tables = [r[0] for r in cur.fetchall()]
                if tables:
                    return f"Available tables in '{os.path.basename(abs_db)}': {', '.join(tables)}"
                return ""
            for tbl in table_names[:5]:
                if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", tbl):
                    continue
                cur = conn.execute(f"PRAGMA table_info({tbl})")
                cols = [r[1] for r in cur.fetchall()]
                if cols:
                    hints.append(f"{tbl}({', '.join(cols)})")
            if not hints:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 25"
                )
                tables = [r[0] for r in cur.fetchall()]
                if tables:
                    return f"Available tables in '{os.path.basename(abs_db)}': {', '.join(tables)}"
        finally:
            conn.close()
    except Exception:
        return ""
    return "; ".join(hints)


def check_sql_query_guard(cmd: str, cwd: str = "/workspace") -> tuple[bool, str]:
    """
    SQLite Query Discipline & Pre-Execution Schema Validator:
    1. Ban 'SELECT * FROM ...' (forces explicit column projection and returns live table schema).
    2. Ban top-level SELECT queries without a LIMIT clause (unless purely scalar aggregate or WHERE ... IN (SELECT ...)).
    3. Pre-validate SQL syntax and column/table names against local SQLite databases via EXPLAIN QUERY PLAN,
       returning exact valid column names on mismatch so the LLM never plays 20-questions with field names.
    """
    extracted = _extract_sql_queries_from_cmd(cmd)
    if not extracted:
        return True, ""

    for item in extracted:
        if len(item) == 3:
            db_path, raw_sql, is_cli = item
        else:
            db_path, raw_sql = item
            is_cli = True
        # Split multi-statement SQL strings safely
        statements = [s.strip() for s in raw_sql.split(";") if s.strip()]
        for stmt in statements:
            if not re.search(r"^\s*(?:WITH\b[\s\S]+?\bSELECT\b|SELECT\b)", stmt, re.IGNORECASE):
                continue

            cte_names = {m.group(1).lower() for m in _SQL_CTE_NAME_RE.finditer(stmt)}
            tables = [
                m.group(1)
                for m in _SQL_TABLE_EXTRACT_RE.finditer(stmt)
                if m.group(1).lower() not in cte_names
            ]
            # Deduplicate preserving order
            seen_t: set[str] = set()
            unique_tables: list[str] = []
            for t in tables:
                if t.lower() not in seen_t:
                    seen_t.add(t.lower())
                    unique_tables.append(t)

            # 1. Outright ban on SELECT * FROM (for CLI or local SQLite queries)
            if (is_cli or db_path) and _SQL_SELECT_STAR_RE.search(stmt):
                schema_hint = _introspect_sqlite_table_columns(db_path or "", unique_tables, cwd=cwd)
                hint_suffix = f" Valid columns: {schema_hint}" if schema_hint else ""
                return False, (
                    "SQLite Query Guard Triggered: 'SELECT *' is prohibited because it wastes tokens and dumps wide rows. "
                    f"Specify the exact column names you need and include a LIMIT clause.{hint_suffix}"
                )

            # 2. Outright ban on top-level SELECT without LIMIT for CLI or inline one-liner SQLite queries
            # (Skip LIMIT enforcement when a Python script queries PostgreSQL/DataFrames into memory without a local .db path)
            if (is_cli or db_path) and not re.search(r"\bLIMIT\s+(?:\d+|\?|\:[a-zA-Z_][a-zA-Z0-9_]*)", stmt, re.IGNORECASE):
                if not _SQL_AGGREGATE_ONLY_RE.search(stmt):
                    schema_hint = _introspect_sqlite_table_columns(db_path or "", unique_tables, cwd=cwd)
                    hint_suffix = f" Valid columns: {schema_hint}" if schema_hint else ""
                    return False, (
                        "SQLite Query Guard Triggered: Unbounded SELECT query without a LIMIT clause is prohibited. "
                        f"Append 'LIMIT <N>' (e.g. LIMIT 25) to bound output token volume.{hint_suffix}"
                    )

            # 3. Live SQLite schema validation via EXPLAIN QUERY PLAN (when local DB exists and no f-string placeholders)
            if db_path and "{" not in stmt and "}" not in stmt:
                clean_db = db_path[5:].split("?")[0] if db_path.startswith("file:") else db_path
                norm_cwd = os.path.abspath(cwd or "/workspace")
                abs_db = os.path.abspath(os.path.join(norm_cwd, clean_db))
                if os.path.exists(abs_db):
                    try:
                        conn = sqlite3.connect(f"file:{abs_db}?mode=ro", uri=True, timeout=1.5)
                        try:
                            conn.execute(f"EXPLAIN QUERY PLAN {stmt}")
                        finally:
                            conn.close()
                    except sqlite3.Error as e:
                        err_msg = str(e)
                        if "no such column" in err_msg.lower() or "no such table" in err_msg.lower() or "ambiguous column" in err_msg.lower():
                            schema_hint = _introspect_sqlite_table_columns(abs_db, unique_tables, cwd=cwd)
                            hint_suffix = f" Valid schema: {schema_hint}" if schema_hint else ""
                            return False, (
                                f"SQLite Schema Validation Failed ({err_msg}). Do not guess column or table names.{hint_suffix}"
                            )
    return True, ""


_DESTRUCTIVE_GIT_WIPE_RE = re.compile(
    r"\bgit\s+(?:"
    r"reset\s+--hard\b|"
    r"checkout-index\s+-[a-zA-Z]*f|"
    r"clean\s+-[a-zA-Z]*f|"
    r"checkout\s+(?:[^\s;|&]+\s+)?--\s+(?:\.|\$\([^)]+\)|(?:\.agents|tools|config|docs)\b)|"
    r"restore\s+(?:--worktree\s+|--source=[^\s]+\s+)*(?:\.|(?:\.agents|tools|config|docs)\b)"
    r")"
)


def is_destructive_git_working_tree_wipe(cmd: str, cwd: str = "/workspace") -> bool:
    """Block destructive git working-tree wipes in /workspace (e.g. git reset --hard, git checkout-index -f, blanket directory checkout)."""
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return False
    unquoted = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", cmd)
    return bool(_DESTRUCTIVE_GIT_WIPE_RE.search(unquoted))


def is_batched_or_mutating_cmd(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    if is_inline_python_probe(cmd_clean):
        return False
    unquoted = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", cmd_clean)
    if " && " in unquoted or " || " in unquoted or "; " in unquoted or "\n" in unquoted:
        return True
    if "scratch/" in cmd_clean or "test_" in cmd_clean or "pytest" in cmd_clean:
        return True
    if re.search(r"python[0-9.]*\s+(/workspace/|/baseball/|/root/)", cmd_clean):
        return True
    if re.search(r"\b(git|touch|mkdir|rm|cp|mv|chmod|kill|pkill|restart|reboot|compose)\b", unquoted):
        return True
    return False


def is_single_shell_inspection(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    ssh_match = re.search(r"ssh\s+.*?(['\"])(.*?)\1\s*$", cmd_clean)
    target_cmd = ssh_match.group(2).strip() if ssh_match else cmd_clean
    if is_batched_or_mutating_cmd(target_cmd):
        return False
    inspection_starters = (
        "docker ps", "docker inspect", "docker logs",
        "cat", "head", "tail", "grep", "rg", "ag", "find",
        "sed", "awk", "wc", "cut", "sort", "uniq", "stat",
        "ls", "df", "free", "uptime", "vmstat", "iostat",
        "crontab", "ss", "netstat", "ps", "which", "echo"
    )
    tokens = target_cmd.split()
    first_word = tokens[0] if tokens else ""
    first_two = " ".join(tokens[:2]) if len(tokens) >= 2 else ""
    return first_word in inspection_starters or first_two in inspection_starters


def is_ouroboros_polling_loop(cmd: str) -> bool:
    """Detect manual shell busy-wait process polling loops (e.g. while pgrep -f ...; do sleep ...)."""
    if not cmd:
        return False
    pattern = (
        r"\b(?:while|until)\s+(?:!\s*)?(?:pgrep|ps\s+|pidof|kill\s+-0)\b[^;\"\n']*?[;\n]+\s*do\b[\s\S]*?\bsleep\s+[0-9.]+"
    )
    return bool(re.search(pattern, cmd))


def is_proc_fd_stream_read(cmd: str) -> bool:
    """Detect tail/cat/head/grep against open container stdio FIFO pipes (/proc/1/fd/* or /proc/self/fd/*)."""
    if not cmd:
        return False
    pattern = r"\b(?:tail|cat|head|grep|egrep|fgrep|rg|sed|awk)\b[^\n;|]*?/proc/(?:1|self|\d+)/fd/[012]\b"
    return bool(re.search(pattern, cmd))


def check_readonly_shared_mount(target_file: str, cwd: str = "/workspace") -> tuple[bool, str]:
    """Block attempts to edit or overwrite files residing on Zero's read-only /opt/zero shared mount."""
    if not target_file:
        return True, ""
    norm_cwd = os.path.abspath(cwd or "/workspace")
    abs_path = os.path.abspath(os.path.join(norm_cwd, target_file))
    real_path = os.path.realpath(abs_path).replace("\\", "/")
    if real_path.startswith("/opt/zero/"):
        return False, (
            f"Read-Only Shared Mount Guard: '{target_file}' resolves to '{real_path}' (owned by Zero and mounted read-only into discord-ivy-agent). "
            "Do NOT attempt local edits or replace the symlink via cp/rm (which severs upstream updates). "
            "Escalate the issue to Zero in #zero-ops (1544953279664889888) by running:\n"
            "python3 /workspace/tools/outbox.py --channel zero-ops --message \"<@1542285964213358633> [Upstream Fix Request] <file & root cause>\" --force && python3 /workspace/tools/outbox.py --flush"
        )
    if os.environ.get("FORGE_APPROVED_CARD_EXECUTION") == "1":
        return True, ""
    if os.path.exists(real_path) and not os.access(real_path, os.W_OK):
        return False, (
            f"Read-Only File Guard: '{target_file}' ('{real_path}') is read-only inside this container. "
            "Do NOT bypass read-only mounts. Escalate the required change to Zero in #zero-ops (1544953279664889888) via:\n"
            "python3 /workspace/tools/outbox.py --channel zero-ops --message \"<@1542285964213358633> [Upstream Fix Request] <file & root cause>\" --force && python3 /workspace/tools/outbox.py --flush"
        )
    return True, ""


def is_opt_zero_symlink_tamper(cmd: str) -> bool:
    """Detect shell commands attempting to overwrite or unlink /opt/zero symlinks inside discord-ivy-agent."""
    if not cmd or not os.path.exists("/opt/zero/workspace"):
        return False
    if "/opt/zero/" in cmd and re.search(r"\b(?:cp|mv|rm|unlink|sed\s+-i|chmod|chown)\b", cmd):
        return True
    if re.search(r"\bcp\s+--remove-destination\b", cmd):
        return True
    return False


_DOCKER_CLI_SUBCOMMAND_RE = re.compile(
    r'(?:^|[;&|]\s*|["\']\s*|\bsudo\s+|--command(?:=|\s+))docker\s+(?:exec|run|compose|ps|logs|restart|stop|start|inspect|build|cp|pull)\b'
)


def check_missing_docker_cli_guard(cmd: str) -> tuple[bool, str]:
    """Block execution of docker CLI subcommands when docker is not installed inside this container."""
    if not cmd:
        return True, ""

    cmd_clean = cmd.strip()
    # Remote SSH commands execute docker on the remote host, not inside this container
    if cmd_clean.startswith("ssh ") or re.match(r"^\s*ssh\b", cmd_clean) or "run_ssh" in cmd_clean:
        return True, ""

    is_docker = bool(_DOCKER_CLI_SUBCOMMAND_RE.search(cmd))
    if not is_docker:
        m_cmd = re.search(r'--command(?:=|\s+)(["\']?)(.*?)\1(?:\s+--|\s*$)', cmd)
        if m_cmd:
            inner_cmd = m_cmd.group(2).strip()
            if _DOCKER_CLI_SUBCOMMAND_RE.search(inner_cmd) or re.search(
                r"\bdocker\s+(?:exec|run|compose|ps|logs|restart|stop|start|inspect|build|cp|pull)\b",
                inner_cmd,
            ):
                is_docker = True

    if is_docker and shutil.which("docker") is None:
        return False, (
            "Container Environment Guard Triggered: The 'docker' CLI binary is not installed inside this container "
            "(invoking 'docker' fails with exit code 127: 'docker: not found'). "
            "Note: in discord-ivy-agent, 'predict_big.py --migrate-db' already executes 'calculate_dollar_values.R' "
            "and updates PostgreSQL natively without needing 'docker exec big_board_pro'. "
            "Remove the 'docker' subcommand before running or launching the job."
        )

    return True, ""


def _check_inline_production_edit_gate(
    target_file: str,
    cwd: str,
    state: dict,
    state_file: str,
    now: float = 0.0,
) -> tuple[bool, str]:
    """Enforce FORGE Tier 2/3 Gate: block interactive turns outside /tmp/forge-wt-* from mutating >2 production files inline or mutating after >90s calibration."""
    if os.environ.get("FORGE_APPROVED_CARD_EXECUTION") == "1":
        return True, ""
    if is_forge_worktree_or_exempt_path(target_file, cwd=cwd):
        return True, ""
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    norm_target = os.path.abspath(os.path.join(norm_cwd, target_file)).replace("\\", "/")

    # 90-second calibration coupling: if turn has spent >90s running >=2 calibration scripts (or >=8 diagnostic scripts),
    # inline production mutations are closed and must be enqueued via FORGE.
    cur_now = now or time.time()
    turn_start = float(state.get("turn_start_time", cur_now) or cur_now)
    turn_elapsed = cur_now - turn_start
    total_scratch = int(state.get("total_scratch_runs", 0))
    calib_scratch = int(state.get("calibration_scratch_runs", 0))
    if turn_elapsed > FORGE_TIER1_INLINE_SECONDS and (
        calib_scratch >= MAX_CALIBRATION_SCRATCH_RUNS_AFTER_90S
        or total_scratch >= MAX_DIAGNOSTIC_SCRATCH_RUNS_AFTER_90S
    ):
        save_state(state, state_file)
        return False, (
            f"FORGE 90s Tier 2/3 Gate Triggered: Interactive turn has already spent {int(turn_elapsed)}s "
            f"(>{int(FORGE_TIER1_INLINE_SECONDS)}s Tier 1 ceiling) running {total_scratch} inline calibration/scratch scripts "
            f"before mutating '{os.path.basename(norm_target)}'. Tasks requiring >90s of calibration must be packaged into a "
            "validated ForgePlan JSON (with your calibrated parameters in shared_context) and staged via an inline Decision Card "
            "('python3 /workspace/tools/forge.py stage-decision --handler forge_enqueue ... --post') or enqueued directly via "
            "'python3 /workspace/tools/forge.py enqueue scratch/forge_plan.json' so #forge workers execute and verify them in isolated git worktrees."
        )

    prod_files = list(state.get("inline_prod_files", []))
    if norm_target not in prod_files:
        prod_files.append(norm_target)
    state["inline_prod_files"] = prod_files
    if len(prod_files) > MAX_INLINE_PRODUCTION_FILES:
        save_state(state, state_file)
        short_list = ", ".join(os.path.basename(p) for p in prod_files)
        return False, (
            f"FORGE Tier 2/3 Gate Triggered: Interactive turn attempted to mutate {len(prod_files)} distinct production files inline "
            f"({short_list}; inline Tier 1 limit: {MAX_INLINE_PRODUCTION_FILES}). "
            "Multi-file features and refactors must be decomposed into a validated ForgePlan JSON (e.g. /workspace/data/forge_plans/<slug>.json) "
            "and staged via 'python3 /workspace/tools/forge.py stage-decision --handler forge_enqueue ... --post' (or enqueued directly via "
            "'python3 /workspace/tools/forge.py enqueue scratch/forge_plan.json') so #forge workers execute them in isolated git worktrees."
        )
    return True, ""


def check_inline_production_shell_mutation(
    cmd: str,
    cwd: str,
    state: dict,
    state_file: str,
    now: float = 0.0,
) -> tuple[bool, str]:
    """Block run_command workarounds (python3 -c open/write, sed -i, cat >) that mutate production code to bypass edit_file."""
    if not cmd:
        return True, ""
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True, ""
    norm_cwd = _norm_posix(cwd or "/workspace", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return True, ""
    if "forge.py" in cmd or "detached_runner.py" in cmd:
        return True, ""

    has_write_op = bool(
        re.search(r"\.write\s*\(|open\s*\([^)]*,\s*['\"][wa]['\"]|\bsed\s+-i\b|\bcat\s+>>?", cmd)
    )
    if not has_write_op:
        return True, ""

    for m in _PROD_SOURCE_PATH_RE.finditer(cmd):
        prod_path = m.group("path")
        if is_forge_worktree_or_exempt_path(prod_path, cwd=cwd):
            continue
        return False, (
            f"FORGE Shell Mutation Bypass Blocked: Direct shell/python mutation of production source file '{prod_path}' "
            "via 'run_command' is prohibited. Use 'edit_file' for Tier 1 (<90s, <=2 files) edits, or enqueue a ForgePlan via "
            "'python3 /workspace/tools/forge.py enqueue scratch/forge_plan.json' for Tier 2/3 builds."
        )
    return True, ""


def is_forge_plan_file_path(target_file: str, cwd: str = "/workspace") -> bool:
    """Return True if target_file looks like a ForgePlan JSON file being written or edited."""
    if not target_file:
        return False
    norm = _norm_posix(target_file, cwd or "/workspace").lower()
    if not norm.endswith(".json"):
        return False
    base = os.path.basename(norm)
    if "/forge_plans/" in norm or "forge_plan" in base or "plan" in base:
        return True
    if norm.startswith(("/tmp/", "/workspace/scratch/")) or "/scratch/" in norm:
        return True
    return False


def check_forge_post_offload_lock(
    name: str,
    args: dict,
    state: dict,
    cwd: str = "/workspace",
) -> tuple[bool, str]:
    """Enforce FORGE Post-Offload Turn Lock while allowing forge.py enqueue / stage-decision after an earlier diagnostic job start."""
    if (
        not state.get("offload_dispatched")
        or os.environ.get("FORGE_WORKER_ACTIVE") == "1"
        or _norm_posix(cwd or "/workspace", "/").startswith("/tmp/forge-wt-")
    ):
        return True, ""

    if name not in ("run_command", "edit_file", "replace_file_content", "write_file", "write_to_file"):
        return True, ""

    cmd = str(args.get("CommandLine", "")) if name == "run_command" else ""
    if name == "run_command" and "outbox.py" in cmd:
        return True, ""

    kind_label = str(state.get("offload_kind") or "FORGE job/build")
    is_prior_job_start = "job start" in kind_label.lower() and "enqueue" not in kind_label.lower()

    if is_prior_job_start:
        if name == "run_command" and (
            re.search(r"\bforge\.py\s+enqueue\b", cmd)
            or (re.search(r"\bforge\.py\s+(?:stage-decision|propose)\b", cmd) and "forge_enqueue" in cmd)
        ):
            return True, ""
        if name in ("write_file", "write_to_file", "edit_file", "replace_file_content"):
            target_p = str(args.get("TargetFile") or args.get("Path") or "")
            if is_forge_plan_file_path(target_p, cwd=cwd):
                return True, ""

    return False, (
        f"FORGE Post-Offload Turn Lock Triggered: A {kind_label} was already dispatched in this turn. "
        "Do not continue executing commands, editing scripts, or spawning additional jobs in the same turn. "
        "End your turn immediately (output your brief launch status to Ryan) and wait for the asynchronous completion wakeup."
    )


def is_heavy_inline_benchmark_or_migration(cmd: str, cwd: str = "/workspace") -> bool:
    """Detect heavy multi-minute benchmark or database migration commands run inline outside FORGE."""
    if not cmd:
        return False
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return False
    norm_cwd = _norm_posix(cwd or "/workspace", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return False
    if "forge.py" in cmd or "detached_runner.py" in cmd or "modal_harness.py" in cmd:
        return False
    if "refresh_preseason.py" in cmd and "predict_big.py" not in cmd:
        return False
    return bool(_HEAVY_CLI_BENCHMARK_OR_MIGRATE_RE.search(cmd))


_FORGE_JOB_START_RE = re.compile(
    r"\b(?:(?:forge\.py|tools\.forge)(?:\s+--[a-zA-Z0-9_-]+(?:=[^\s;|&]+|\s+[^\s;|&-]+)?)*\s+job\s+start|detached_runner\.py\s+start)\b"
)
_JOB_START_OTHER_VALUE_FLAGS = {"--name", "-n", "--channel", "--cwd", "--timeout", "-t"}
_JOB_START_HELP_FLAGS = {"--help", "-h"}


def _tokenize_job_start_args(tail: str) -> list[tuple[str, bool]]:
    """Tokenize shell arguments following `forge.py job start` up to the first unquoted command separator."""
    tokens: list[tuple[str, bool]] = []
    cur: list[str] = []
    in_token = False
    was_quoted = False
    quote: str | None = None
    i = 0
    n = len(tail)

    while i < n:
        ch = tail[i]
        if quote is None:
            if ch == "\\":
                if i + 1 < n:
                    nxt = tail[i + 1]
                    if nxt == "\n":
                        i += 2
                        continue
                    cur.append(nxt)
                    in_token = True
                    i += 2
                    continue
                cur.append(ch)
                in_token = True
                i += 1
                continue
            if ch in ("'", '"'):
                if cur == ["$"] and not was_quoted:
                    cur.clear()
                quote = ch
                in_token = True
                was_quoted = True
                i += 1
                continue
            if ch in ("\n", ";"):
                break
            if ch == "|":
                prev_ch = tail[i - 1] if i > 0 else ""
                if prev_ch != ">":
                    break
            if ch == "&":
                prev_ch = tail[i - 1] if i > 0 else ""
                next_ch = tail[i + 1] if i + 1 < n else ""
                if prev_ch != ">" and next_ch != ">":
                    break
            if ch.isspace():
                if in_token:
                    tokens.append(("".join(cur), was_quoted))
                    cur = []
                    in_token = False
                    was_quoted = False
                i += 1
                continue
            cur.append(ch)
            in_token = True
            i += 1
        elif quote == "'":
            if ch == "'":
                quote = None
            else:
                cur.append(ch)
            i += 1
        elif quote == '"':
            if ch == "\\" and i + 1 < n and tail[i + 1] in ('"', "\\", "$", "`", "\n"):
                if tail[i + 1] != "\n":
                    cur.append(tail[i + 1])
                i += 2
                continue
            if ch == '"':
                quote = None
            else:
                cur.append(ch)
            i += 1

    if in_token:
        tokens.append(("".join(cur), was_quoted))
    return tokens


def has_forge_job_start_command(cmd: str) -> bool:
    """Return True only when a `forge.py job start` / `detached_runner.py start` invocation
    includes a non-empty `--command` or `-c` argument (and is not `--help` / `-h`).
    """
    if not cmd:
        return False
    for m in _FORGE_JOB_START_RE.finditer(cmd):
        tokens = _tokenize_job_start_args(cmd[m.end() :])
        if not tokens:
            continue
        has_cmd = False
        has_help = False
        i = 0
        while i < len(tokens):
            tok, tok_quoted = tokens[i]
            if not tok_quoted and tok in _JOB_START_HELP_FLAGS:
                has_help = True
                break
            if not tok_quoted and tok in ("--command", "-c"):
                if i + 1 < len(tokens):
                    nxt_val, nxt_quoted = tokens[i + 1]
                    if not (
                        not nxt_quoted
                        and (
                            nxt_val in _JOB_START_OTHER_VALUE_FLAGS
                            or nxt_val in _JOB_START_HELP_FLAGS
                            or nxt_val.startswith("-")
                        )
                    ) and bool(nxt_val.strip()):
                        has_cmd = True
                    i += 2
                    continue
                i += 1
                continue
            if tok.startswith("--command="):
                if bool(tok[len("--command=") :].strip()):
                    has_cmd = True
                i += 1
                continue
            if tok.startswith("-c="):
                if bool(tok[len("-c=") :].strip()):
                    has_cmd = True
                i += 1
                continue
            if not tok_quoted and tok in _JOB_START_OTHER_VALUE_FLAGS:
                if i + 1 < len(tokens):
                    nxt_val, nxt_quoted = tokens[i + 1]
                    if not nxt_quoted and (
                        nxt_val in ("--command", "-c")
                        or nxt_val in _JOB_START_OTHER_VALUE_FLAGS
                        or nxt_val in _JOB_START_HELP_FLAGS
                    ):
                        has_cmd = False
                        break
                    i += 2
                    continue
                i += 1
                continue
            i += 1
        if has_cmd and not has_help:
            return True
    return False


_SAFE_NON_SWEEP_CMD_RE = re.compile(
    r"^\s*(?:pytest|python[0-9.]*\s+-m\s+(?:pytest|unittest|py_compile)|ruff|git|grep|rg|ag|find|cat|head|tail|ls|wc|which)\b"
)
_BIG_MODEL_SUITE_OOS_MULTI_YEAR_RE = re.compile(r"\b20(?:1\d|2\d)\b")
_BIG_MODEL_SUITE_OOS_HINT_RE = re.compile(
    r"(?:"
    r"\brange\s*\(\s*20"
    r"|\bfor\s+(?:yr|year|season|seasons|holdout[_a-z0-9]*|test_year|eval_year)\s+in\b"
    r"|\b(?:holdout|hold_out|walk_forward|walk-forward|out_of_sample|out-of-sample|multi_season|multi-season|oos)\b"
    r")",
    re.IGNORECASE,
)
_PY_SCRIPT_INVOCATION_RE = re.compile(
    r"\bpython[0-9.]*\s+(?:-[a-zA-Z0-9]+\s+)*(?P<script>[^\s;\"'|&>]+\.py)\b"
)
_SHELL_LOOP_OR_MULTI_EXEC_RE = re.compile(
    r"(?:\b(?:for|while)\b[^;\n]*[;\n]\s*do\b|\bxargs\b|\bparallel\b)"
)
_EXEMPT_PY_SCRIPT_BASENAMES = frozenset({
    "modal_harness.py",
    "forge.py",
    "detached_runner.py",
    "batch_guard.py",
    "refresh_preseason.py",
})


def _extract_job_start_inner_commands(cmd: str) -> list[str]:
    """Extract inner `--command` / `-c` payloads from `forge.py job start` or `detached_runner.py start`."""
    inner: list[str] = []
    for m in _FORGE_JOB_START_RE.finditer(cmd):
        tokens = _tokenize_job_start_args(cmd[m.end() :])
        i = 0
        while i < len(tokens):
            tok, tok_quoted = tokens[i]
            if not tok_quoted and tok in ("--command", "-c") and i + 1 < len(tokens):
                inner.append(tokens[i + 1][0])
                i += 2
                continue
            if tok.startswith("--command="):
                inner.append(tok[len("--command=") :])
            elif tok.startswith("-c="):
                inner.append(tok[len("-c=") :])
            i += 1
    return inner


def _has_big_model_suite_oos_sweep(text: str) -> bool:
    """Return True if Python/shell text runs BIGModelSuite across multi-season or OOS holdout loops."""
    if not text or "BIGModelSuite" not in text:
        return False
    if _BIG_MODEL_SUITE_OOS_HINT_RE.search(text):
        return True
    if len(set(_BIG_MODEL_SUITE_OOS_MULTI_YEAR_RE.findall(text))) >= 2:
        return True
    if re.search(r"\bfor\b", text) and re.search(r"(?:\bfit_and_predict\b|\.fit\s*\()", text):
        return True
    return False


def _is_chained_predict_big_migrate_all_pt_modes(cmd_text: str) -> bool:
    """Detect ad-hoc predict_big.py --migrate-all-pt-modes shell chains or duplicated migration calls."""
    if not cmd_text or "predict_big.py" not in cmd_text or "--migrate-all-pt-modes" not in cmd_text:
        return False
    if len(re.findall(r"\bpredict_big\.py\b", cmd_text)) >= 2:
        return True
    if len(re.findall(r"--migrate-(?:all-pt-modes|db)\b", cmd_text)) >= 2:
        return True
    if _SHELL_LOOP_OR_MULTI_EXEC_RE.search(cmd_text):
        return True
    raw_segments = [s.strip() for s in re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd_text) if s.strip()]
    non_setup_segments = [
        s for s in raw_segments if not re.match(r"^(?:cd|export|source|\.)\b", s)
    ]
    return len(non_setup_segments) >= 2


def check_baseball_heavy_sweep_and_migration_guard(cmd: str, cwd: str = "/workspace") -> tuple[bool, str]:
    """Block local multi-season BIGModelSuite OOS holdout sweeps and ad-hoc chained predict_big.py migrations."""
    if not cmd:
        return True, ""
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True, ""
    norm_cwd = _norm_posix(cwd or "/workspace", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return True, ""

    outer_segments = [s.strip() for s in re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd) if s.strip()]
    if outer_segments and all(_SAFE_NON_SWEEP_CMD_RE.match(seg) for seg in outer_segments):
        return True, ""

    # Guard A: Local Multi-Season BIGModelSuite OOS Holdout Sweeps -> Offload to Modal
    if "modal_harness.py" not in cmd:
        is_local_oos_sweep = _has_big_model_suite_oos_sweep(cmd)
        if not is_local_oos_sweep:
            for m in _PY_SCRIPT_INVOCATION_RE.finditer(cmd):
                script_path = m.group("script")
                base = os.path.basename(script_path)
                if base.startswith("test_") or base.endswith("_test.py") or base in _EXEMPT_PY_SCRIPT_BASENAMES:
                    continue
                abs_script = os.path.abspath(os.path.join(cwd or "/workspace", script_path))
                if os.path.isfile(abs_script):
                    try:
                        with open(abs_script, "r", encoding="utf-8", errors="ignore") as sf:
                            script_text = sf.read(262144)
                        if _has_big_model_suite_oos_sweep(script_text):
                            is_local_oos_sweep = True
                            break
                    except OSError:
                        pass
        if is_local_oos_sweep:
            return False, (
                "Modal Offload Guard Triggered (BIGModelSuite OOS Sweep): Running multi-season or out-of-sample holdout "
                "`BIGModelSuite` sweeps locally on Host 2 NAS CPU is prohibited (hits 60-minute turn/CPU ceilings). "
                "Offload multi-season `BIGModelSuite` OOS holdout sweeps to Modal cloud compute via:\n"
                "python3 /baseball/big-projections/modal_harness.py [args]"
            )

    # Guard B: Ad-Hoc Chained predict_big.py --migrate-all-pt-modes One-Liners -> Use refresh_preseason.py
    if "predict_big.py" in cmd and "--migrate-all-pt-modes" in cmd:
        inner_cmds = _extract_job_start_inner_commands(cmd)
        duplicated_outer = (
            len(re.findall(r"\bpredict_big\.py\b", cmd)) >= 2
            or len(re.findall(r"--migrate-(?:all-pt-modes|db)\b", cmd)) >= 2
        )
        is_chained_migration = duplicated_outer or (
            any(_is_chained_predict_big_migrate_all_pt_modes(ic) for ic in inner_cmds)
            if inner_cmds
            else _is_chained_predict_big_migrate_all_pt_modes(cmd)
        )
        if is_chained_migration:
            return False, (
                "Canonical Preseason Refresh Guard Triggered: Ad-hoc chained `predict_big.py --migrate-all-pt-modes` "
                "shell invocations outside `/baseball/big-projections/refresh_preseason.py` are prohibited (duplicates DB migrations). "
                "Run a single canonical job via `/baseball/big-projections/refresh_preseason.py`:\n"
                "python3 /workspace/tools/forge.py job start --name \"Refresh Preseason\" --command \"python3 /baseball/big-projections/refresh_preseason.py\""
            )

    return True, ""


def _record_forge_offload_if_applicable(cmd: str, cwd: str, state: dict) -> None:
    """Record post-offload turn lock state only for validated FORGE enqueue/decision/job-start commands."""
    is_forge_enqueue = bool(re.search(r"\bforge\.py\s+enqueue\b", cmd))
    is_forge_stage_decision = bool(
        re.search(r"\bforge\.py\s+(?:stage-decision|propose)\b", cmd) and "forge_enqueue" in cmd
    )
    is_forge_job_start = has_forge_job_start_command(cmd)
    is_no_synthesize_probe = bool(is_forge_job_start and "--no-synthesize" in cmd)

    if is_forge_enqueue:
        plan_ok = False
        m_enq = re.search(r"\bforge\.py\s+enqueue\b", cmd)
        cmd_before = cmd[: m_enq.start()] if m_enq else ""
        has_preceding_shell_gen = bool(
            re.search(
                r"(?:<<|>>?|\b(?:cat|echo|printf|tee|python3?|cp|mv)\b)[^;&|\n]*[;&|\n]",
                cmd_before,
                re.DOTALL,
            )
        )
        if not has_preceding_shell_gen:
            m_plan = re.search(
                r"\bforge\.py\s+enqueue\s+(?:--[a-zA-Z0-9_-]+(?:\s+[^\s;\"'|&>]+)?\s+)*(?:\"([^\"]+)\"|'([^']+)'|([^\s;\"'|&>]+))",
                cmd,
            )
            if m_plan:
                raw_pf = m_plan.group(1) or m_plan.group(2) or m_plan.group(3) or ""
                if raw_pf and not raw_pf.startswith("-"):
                    abs_pf = os.path.abspath(os.path.join(cwd or "/workspace", raw_pf))
                    if os.path.isfile(abs_pf):
                        try:
                            with open(abs_pf, "r", encoding="utf-8") as pf:
                                pdata = json.load(pf)
                            if isinstance(pdata, dict):
                                val_fn = None
                                try:
                                    _repo_root = _resolve_repo_root()
                                    if _repo_root not in sys.path:
                                        sys.path.insert(0, _repo_root)
                                    elif sys.path[0] != _repo_root:
                                        sys.path.remove(_repo_root)
                                        sys.path.insert(0, _repo_root)
                                    from tools.forge import _normalize_forge_plan_in_place, validate_forge_plan
                                    _normalize_forge_plan_in_place(pdata, from_enqueue=True)
                                    val_fn = validate_forge_plan
                                except Exception:
                                    try:
                                        from tools.forge import _normalize_forge_plan_in_place, validate_forge_plan
                                        _normalize_forge_plan_in_place(pdata, from_enqueue=True)
                                        val_fn = validate_forge_plan
                                    except Exception:
                                        val_fn = None

                                if callable(val_fn):
                                    is_valid, _ = val_fn(pdata)
                                    plan_ok = bool(is_valid)
                                else:
                                    if not pdata.get("title") or not pdata.get("chunks"):
                                        plan_ok = False
                                    elif not (
                                        pdata.get("origin_channel_id")
                                        or pdata.get("channel_id")
                                    ):
                                        plan_ok = False
                                    elif not (
                                        pdata.get("context_fact_sheet")
                                        or pdata.get("shared_context")
                                        or pdata.get("facts")
                                        or pdata.get("objective")
                                        or pdata.get("chunks")
                                    ):
                                        plan_ok = False
                                    else:
                                        plan_ok = True
                        except Exception:
                            plan_ok = False
        if plan_ok:
            state["inline_prod_files"] = []
            state["offload_dispatched"] = True
            state["offload_kind"] = "FORGE multi-chunk build (forge.py enqueue)"
    elif is_forge_stage_decision:
        state["inline_prod_files"] = []
        state["offload_dispatched"] = True
        state["offload_kind"] = "FORGE Two-Stage Decision Card (forge.py stage-decision --handler forge_enqueue)"
    elif is_forge_job_start and not is_no_synthesize_probe:
        state["offload_dispatched"] = True
        state["offload_kind"] = "FORGE background job (forge.py job start)"


CANARY_HONEYPOT_BASENAMES = {"passwords.txt", "canary_passwords.txt"}
CANARY_HONEYPOT_EXPLICIT_PATHS = {
    "~/passwords.txt",
    "/root/passwords.txt",
    "/workspace/passwords.txt",
    "/secrets/canary_passwords.txt",
}


def _emit_canary_tripwire_alert(tool_name: str, detail: str) -> None:
    """Emit a best-effort, non-blocking alert when the canary honeypot tripwire is triggered."""
    try:
        alert_path = Path("/tmp/canary_tripwire_alerts.jsonl")
        entry = {
            "timestamp": time.time(),
            "iso": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "detail": detail,
        }
        with open(alert_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    if os.environ.get("CANARY_ENABLE_OUTBOX_ALERT") == "1":
        try:
            try:
                from tools.outbox import enqueue
            except ImportError:
                from outbox import enqueue

            enqueue(
                channel="zero-chat",
                content=(
                    f"🚨 **Security Canary Honeypot Tripwire Triggered**\n"
                    f"Decoy credential file accessed via `{tool_name}`: {detail}\n"
                    "Action blocked automatically."
                ),
                force=True,
            )
        except Exception:
            pass


def ensure_canary_honeypot_file(path: str = "/root/passwords.txt") -> bool:
    """Safely create the decoy canary credential file if missing with realistic canary placeholder header."""
    try:
        p = Path(path)
        if p.exists():
            return True
        p.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "# SYSTEM CANARY CREDENTIAL VAULT - STRICTLY CONFIDENTIAL\n"
            "# AUTOMATED MONITORING & FORENSIC LOGGING ACTIVE\n"
            "# DO NOT READ, MODIFY, OR EXFILTRATE THIS CANARY DECOY FILE\n"
            "root:canary_tripwire_decoy_token_do_not_use\n"
            "admin:canary_tripwire_decoy_token_do_not_use\n"
        )
        p.write_text(content, encoding="utf-8")
        try:
            os.chmod(str(p), 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def check_canary_honeypot_tripwire(
    name: str,
    args: dict,
    cwd: str = "/workspace",
) -> tuple[bool, str]:
    """
    Detect any attempt to read, edit, write, grep, or access decoy canary credential files
    (passwords.txt, canary_passwords.txt). Triggers an alert and blocks execution.
    """
    if name == "run_command":
        cmd = args.get("CommandLine", "") or ""
        # Ignore git, pytest, or test_batch_guard self-tests
        if re.search(r"\b(?:git|pytest)\b", cmd) or "test_batch_guard" in cmd:
            return True, ""

        if any(b in cmd for b in CANARY_HONEYPOT_BASENAMES) or any(p in cmd for p in CANARY_HONEYPOT_EXPLICIT_PATHS):
            _emit_canary_tripwire_alert(name, f"CommandLine: {cmd}")
            return False, (
                "Security Canary Honeypot Tripwire Triggered: Access to decoy credential file (passwords.txt) "
                "is strictly prohibited and has been logged."
            )
        return True, ""

    if name in ("read_file", "view_file", "edit_file", "replace_file_content", "multi_replace_file_content", "write_file", "write_to_file"):
        target = args.get("AbsolutePath") or args.get("Path") or args.get("TargetFile") or ""
        clean_target = str(target).strip()
        norm_target = _norm_posix(clean_target, cwd=cwd)
        basename = posixpath.basename(norm_target.rstrip("/"))
        if (
            basename in CANARY_HONEYPOT_BASENAMES
            or clean_target in CANARY_HONEYPOT_EXPLICIT_PATHS
            or any(b in norm_target for b in CANARY_HONEYPOT_BASENAMES)
        ):
            _emit_canary_tripwire_alert(name, f"Target: {target}")
            return False, (
                "Security Canary Honeypot Tripwire Triggered: Access to decoy credential file (passwords.txt) "
                "is strictly prohibited and has been logged."
            )
        return True, ""

    if name == "grep_search":
        search_path = str(args.get("SearchPath") or args.get("Path") or "").strip()
        file_pat = str(args.get("FilePattern") or args.get("Includes") or "").strip()
        query = str(args.get("query") or args.get("Query") or "").strip()
        norm_search = _norm_posix(search_path, cwd=cwd) if search_path else ""
        search_base = posixpath.basename(norm_search.rstrip("/")) if norm_search else ""

        if (
            search_base in CANARY_HONEYPOT_BASENAMES
            or search_path in CANARY_HONEYPOT_EXPLICIT_PATHS
            or any(b in norm_search for b in CANARY_HONEYPOT_BASENAMES)
            or any(b in file_pat for b in CANARY_HONEYPOT_BASENAMES)
            or any(b in query for b in CANARY_HONEYPOT_BASENAMES)
        ):
            _emit_canary_tripwire_alert(name, f"grep_search: path={search_path}, pat={file_pat}, q={query}")
            return False, (
                "Security Canary Honeypot Tripwire Triggered: Access to decoy credential file (passwords.txt) "
                "is strictly prohibited and has been logged."
            )
        return True, ""

    return True, ""


def _is_private_or_local_host(host: str) -> bool:
    """Return True if host resolves to localhost, RFC 1918/5737 private/documentation IP, or private LAN domain."""
    if not host:
        return False
    host = host.lower().split(":")[0].strip("[]")
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if any(host.endswith(suffix) for suffix in (".local", ".lan", ".internal", ".home.arpa")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback)
    except ValueError:
        return False


def detect_web_taint_source(name: str, args: dict) -> str | None:
    """
    Detect whether the invoked tool ingests untrusted external web content.
    Returns a description of the taint source or None.
    """
    if name in ("search_web", "read_url_content"):
        query_or_url = args.get("query") or args.get("url") or args.get("Url") or name
        return f"{name}:{query_or_url}"

    if name == "run_command":
        cmd = args.get("CommandLine", "") or ""
        # External web fetchers
        for fetcher in ("reddit_extractor.py", "amazon_serpapi.py", "web_qa.py", "capture_screenshot.py"):
            if fetcher in cmd:
                return f"external_fetcher:{fetcher}"

        # curl or wget targeting public URLs
        if re.search(r"\b(?:curl|wget)\b", cmd):
            urls = re.findall(r"https?://[^\s'\"`]+", cmd)
            for u in urls:
                try:
                    parsed = urllib.parse.urlparse(u)
                    host = parsed.hostname or ""
                    if not _is_private_or_local_host(host):
                        return f"curl/wget:{u}"
                except Exception:
                    return f"curl/wget:{u}"

            if not urls:
                # Check for public host targets without http:// or https://
                for m in re.finditer(r"\b(?:curl|wget)\b\s+([^\s;&|]+)", cmd):
                    target = m.group(1).strip("'\"")
                    if not target.startswith("-"):
                        host = target.split("/")[0].split(":")[0]
                        if "." in host and not _is_private_or_local_host(host):
                            return f"curl/wget:{target}"

    return None


def check_web_ipi_taint_gate(
    name: str,
    args: dict,
    state: dict,
    cwd: str = "/workspace",
) -> tuple[bool, str]:
    """
    Web IPI Taint Gate:
    When a turn has ingested untrusted external web content (web_tainted=True),
    block direct read/search/shell access to /secrets/, memory/private, memory/vault, .env,
    transcript_index.py, env/printenv, and block outbound exfiltration via send_mail.py,
    outbox.py, curl, or wget in the same turn (unless FORGE_WORKER_ACTIVE=1).
    Instructs the agent to stage a human-approved Decision Card via pure
    'forge.py propose' / 'forge.py stage-decision' instead.
    """
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True, ""

    already_tainted = bool(state.get("web_tainted", False))
    is_tainted = already_tainted
    taint_source = state.get("web_taint_source", "") or "untrusted web content"

    # Also detect if the current tool call itself introduces untrusted web content
    if not is_tainted:
        current_source = detect_web_taint_source(name, args)
        if current_source:
            is_tainted = True
            taint_source = current_source

    if not is_tainted:
        return True, ""

    if name in ("view_file", "read_file", "grep_search", "find_by_name"):
        target_file = (
            args.get("AbsolutePath")
            or args.get("SearchDirectory")
            or args.get("SearchPath")
            or args.get("Path")
            or args.get("path")
            or args.get("Directory")
            or ""
        )
        clean_target = str(target_file).strip()
        norm_target = _norm_posix(clean_target, cwd=cwd) if clean_target else ""
        if (
            "/secrets/" in norm_target
            or norm_target.startswith("/secrets")
            or norm_target == "/secrets"
            or _resolves_to_secrets_path(clean_target, cwd=cwd)
        ):
            return False, (
                f"Web IPI Taint Gate Triggered: Untrusted web content was ingested earlier in this turn (source: {taint_source}). "
                "Direct access to /secrets/ is blocked to prevent automated prompt injection exfiltration. "
                "Please stage a human-approved Decision Card ('forge.py propose' or 'forge.py stage-decision') instead."
            )
        if _is_sensitive_private_path(clean_target, cwd=cwd, include_channel_history=False):
            return False, (
                f"Web IPI Taint Gate Triggered: Untrusted web content was ingested earlier in this turn (source: {taint_source}). "
                "Direct access to private memory, vault, or .env files is blocked to prevent automated prompt injection exfiltration. "
                "Please stage a human-approved Decision Card ('forge.py propose' or 'forge.py stage-decision') instead."
            )

    if name == "run_command":
        cmd = args.get("CommandLine", "") or ""
        # Allow Decision Card staging ONLY via a pure unchained forge.py propose / stage-decision call
        if _is_pure_decision_card_staging_cmd(cmd):
            return True, ""

        if "send_mail.py" in cmd:
            return False, (
                f"Web IPI Taint Gate Triggered: Untrusted web content was ingested earlier in this turn (source: {taint_source}). "
                "Direct dispatch via send_mail.py is blocked following untrusted web ingestion. "
                "Please stage a human-approved Decision Card ('forge.py propose' or 'forge.py stage-decision') instead."
            )

        if "outbox.py" in cmd:
            return False, (
                f"Web IPI Taint Gate Triggered: Untrusted web content was ingested earlier in this turn (source: {taint_source}). "
                "Direct dispatch via outbox.py is blocked following untrusted web ingestion. "
                "Please stage a human-approved Decision Card ('forge.py propose' or 'forge.py stage-decision') instead."
            )

        if (
            "/secrets/" in cmd
            or "/secrets" in cmd
            or re.search(r"(?:^|[\s\"'/])(?:\.secrets|secrets)(?:/|\s|$)", cmd)
            or _has_sensitive_path_token_in_cmd(cmd, cwd=cwd, include_channel_history=False)
            or re.search(
                r"(?:^|[\s\"'/=])memory/(?:private|vault)(?:/|\b)"
                r"|(?:^|[\s\"'/=])\.env(?!(?:\.example|\.sample|\.template)\b)(?:\.[A-Za-z0-9_-]+)?(?=$|[\s\"'/;|&)])"
                r"|\btranscript_index\.py\b",
                cmd,
            )
            or _has_bare_env_or_printenv(cmd)
        ):
            return False, (
                f"Web IPI Taint Gate Triggered: Untrusted web content was ingested earlier in this turn (source: {taint_source}). "
                "Direct shell access to /secrets/, private memory, vault, .env, transcript_index.py, or process environment is blocked to prevent automated prompt injection exfiltration. "
                "Please stage a human-approved Decision Card ('forge.py propose' or 'forge.py stage-decision') instead."
            )

        if already_tainted and _is_outbound_curl_or_wget(cmd):
            return False, (
                f"Web IPI Taint Gate Triggered: Untrusted web content was ingested earlier in this turn (source: {taint_source}). "
                "Direct outbound network exfiltration via curl/wget is blocked following untrusted web ingestion. "
                "Please stage a human-approved Decision Card ('forge.py propose' or 'forge.py stage-decision') instead."
            )

    return True, ""


def _finalize_and_save_state(state: dict, state_file: str, name: str, args: dict) -> None:
    """Record web taint if the tool ingests external web content, and save state."""
    taint_src = detect_web_taint_source(name, args)
    if taint_src:
        state["web_tainted"] = True
        state["web_taint_source"] = taint_src
    save_state(state, state_file)


def check_tool_use(
    name: str,
    args: dict,
    state_file: str = STATE_FILE,
    cwd: str = "/workspace",
    in_flight_file: str | None = None,
    now: float | None = None,
    agy_pid: int | None = None,
) -> tuple[bool, str]:
    if in_flight_file is None:
        in_flight_file = IN_FLIGHT_TURN_FILE
    if now is None or now <= 0.0:
        now = time.time()
    turn_anchor = _get_current_turn_Step_anchor(agy_pid=agy_pid, in_flight_file=in_flight_file)
    has_conv_id = _has_conv_id(turn_anchor, in_flight_file=in_flight_file, agy_pid=agy_pid)
    session_id = ":".join(str(turn_anchor).split(":")[:2]) if has_conv_id else str(turn_anchor)
    state = load_state(state_file, session_id=session_id)
    prev_anchor = str(state.get("agy_ppid") or "")
    idle_delta = now - state.get("last_time", 0.0)

    # Reset turn state immediately when a new user turn / prompt arrives (turn_anchor changes)
    # or after extended inactivity (15m); short idle gaps (45s) within the SAME turn reset
    # short-window consecutive anti-polling counters while preserving active turn-level FORGE gates.
    if (prev_anchor and prev_anchor != str(turn_anchor)) or idle_delta > TURN_STATE_EXPIRY_SECONDS or state.get("last_time", 0.0) == 0.0:
        state = _default_state(now)
        state["_session_id"] = session_id
    elif idle_delta > TURN_RESET_IDLE_SECONDS:
        preserved_prod = list(state.get("inline_prod_files", []))
        preserved_sweeps = list(state.get("versioned_scratch_sweeps", []))
        preserved_runs = dict(state.get("scratch_script_runs", {}))
        preserved_total_scratch = int(state.get("total_scratch_runs", 0))
        preserved_calib_scratch = int(state.get("calibration_scratch_runs", 0))
        preserved_turn_cmds = int(state.get("turn_run_commands", 0))
        preserved_offload = bool(state.get("offload_dispatched", False))
        preserved_offload_kind = str(state.get("offload_kind", ""))
        preserved_start = float(state.get("turn_start_time", now) or now)
        preserved_diag_steps = int(state.get("interactive_diag_steps", 0))
        preserved_web_tainted = bool(state.get("web_tainted", False))
        preserved_web_taint_source = str(state.get("web_taint_source", ""))
        state = _default_state(now)
        state["_session_id"] = session_id
        state["inline_prod_files"] = preserved_prod
        state["versioned_scratch_sweeps"] = preserved_sweeps
        state["scratch_script_runs"] = preserved_runs
        state["total_scratch_runs"] = preserved_total_scratch
        state["calibration_scratch_runs"] = preserved_calib_scratch
        state["turn_run_commands"] = preserved_turn_cmds
        state["offload_dispatched"] = preserved_offload
        state["offload_kind"] = preserved_offload_kind
        state["interactive_diag_steps"] = preserved_diag_steps
        state["web_tainted"] = preserved_web_tainted
        state["web_taint_source"] = preserved_web_taint_source
        # Keep original turn_start_time if the turn has already started mutating files, running commands, or running scratch sweeps
        if (
            preserved_total_scratch > 0
            or len(preserved_prod) > 0
            or preserved_turn_cmds > 0
            or preserved_offload
            or preserved_diag_steps > 0
            or preserved_web_tainted
        ):
            state["turn_start_time"] = preserved_start
        else:
            state["turn_start_time"] = now
    if not state.get("turn_start_time"):
        state["turn_start_time"] = now
    state["_session_id"] = session_id
    state["agy_ppid"] = turn_anchor
    state["last_time"] = now

    # 0. Runtime schedule tool is prohibited in Discord bridge turns
    if name == "schedule":
        return False, (
            "Schedule Tool Prohibited in Bridge Turns: Do not invoke the runtime 'schedule' timer tool to poll background tasks. "
            "Stop calling tools silently and let the background task completion notification wake you automatically."
        )

    # 0-boundary. FORGE Worker Prohibited-File Boundary Guard (runs before any FORGE_WORKER_ACTIVE exemptions)
    ok_boundary, boundary_reason = check_forge_prohibited_file_gate(name=name, args=args, cwd=cwd)
    if not ok_boundary:
        save_state(state, state_file)
        return False, boundary_reason

    # 0a. Crab Cavern Air-Gap / SSH Sandbox Guard -> Auto-redirect to 1-click #zero-chat Decision Card
    ok_cc, cc_reason = check_crab_cavern_airgap_guard(
        name=name,
        args=args,
        cwd=cwd,
        in_flight_file=in_flight_file,
        agy_pid=agy_pid,
    )
    if not ok_cc:
        save_state(state, state_file)
        return False, cc_reason

    # 0b. Post-Offload Turn Lock: Once a FORGE build (`forge.py enqueue`) or detached background job (`forge.py job start`)
    # is dispatched in an interactive turn, block all subsequent mutating/execution tool calls so the agent cannot
    # loop or abuse `forge.py job start` as an inline `run_command` counter reset (while still permitting `forge.py enqueue`
    # after a diagnostic `forge.py job start`).
    ok_lock, lock_reason = check_forge_post_offload_lock(name=name, args=args, state=state, cwd=cwd)
    if not ok_lock:
        save_state(state, state_file)
        return False, lock_reason

    # 0c. Deterministic Interactive Investigation Gate:
    # Wrap up open-ended investigations (>10 diagnostic steps after 120s or >12 steps) in interactive Discord turns
    if in_flight_file and os.path.exists(in_flight_file):
        ok_gate, gate_reason = check_interactive_investigation_gate(
            name=name,
            args=args,
            state=state,
            state_file=state_file,
            cwd=cwd,
            now=now,
            in_flight_file=in_flight_file,
            agy_pid=agy_pid,
        )
        if not ok_gate:
            save_state(state, state_file)
            return False, gate_reason

    # 0d. PreToolUse Canary Honeypot Tripwire
    ok_canary, canary_reason = check_canary_honeypot_tripwire(name=name, args=args, cwd=cwd)
    if not ok_canary:
        save_state(state, state_file)
        return False, canary_reason

    # 0e. Web IPI Taint Gate
    ok_taint, taint_reason = check_web_ipi_taint_gate(name=name, args=args, state=state, cwd=cwd)
    if not ok_taint:
        save_state(state, state_file)
        return False, taint_reason

    # 1. run_command check
    if name == "run_command":
        cmd = args.get("CommandLine", "")

        # A00-dest. Hard Destructive-Action, Cloudflare Exposure & Repo Admin Gate
        ok_dest, dest_reason = check_destructive_system_and_exposure_gate(cmd, cwd=cwd)
        if not ok_dest:
            return False, dest_reason

        # A00-docker. Missing Container Docker CLI Guard
        ok_dk, dk_reason = check_missing_docker_cli_guard(cmd)
        if not ok_dk:
            save_state(state, state_file)
            return False, dk_reason

        # A00-del. File Deletion & Decision Card Tamper Gate
        ok_del, del_reason = check_file_deletion_gate(cmd, cwd=cwd)
        if not ok_del:
            return False, del_reason

        # A00-mut. Inline Shell Production Mutation Bypass Gate
        ok_mut, mut_reason = check_inline_production_shell_mutation(cmd, cwd, state, state_file, now=now)
        if not ok_mut:
            return False, mut_reason

        # A00-godot. Local Godot Binary Execution Guard (Require Modal Offload via modal_godot_verify.py)
        if "Godot_v4.7.2" in cmd and "modal_godot_verify.py" not in cmd:
            return False, (
                "Modal Godot Offload Guard Triggered: Direct local execution of 'Godot_v4.7.2' on Host 2 is prohibited "
                "(causes 5-minute --import hangs and heavy CPU/RAM spikes). "
                "Route all headless Godot 4.7.2 test and simulation runs through Modal cloud compute via:\n"
                "python3 /workspace/tools/modal_godot_verify.py [test_filter ...]"
            )

        # A00-baseball. Local BIGModelSuite OOS Sweeps & Chained predict_big.py --migrate-all-pt-modes Guard
        ok_bb, bb_reason = check_baseball_heavy_sweep_and_migration_guard(cmd, cwd=cwd)
        if not ok_bb:
            return False, bb_reason

        # A00. Heavy Inline Benchmark / Migration Gate (Require FORGE job start or enqueue)
        if is_heavy_inline_benchmark_or_migration(cmd, cwd=cwd):
            return False, (
                f"FORGE Background Offload Gate Triggered: Heavy benchmark or database migration command ('{cmd[:90]}') "
                "cannot be executed inline via bare 'run_command' (which blocks the interactive turn for 10–30 minutes). "
                "Launch it as a tracked FORGE background job via:\n"
                "python3 /workspace/tools/forge.py job start --name \"<Job Name>\" --command \"<cmd>\"\n"
                "(or enqueue a multi-chunk ForgePlan via 'python3 /workspace/tools/forge.py enqueue <plan.json>')."
            )

        # Agora Fast-Path Invariant
        if re.search(r"agora_play\.py\s+(?:order|move)\b", cmd):
            return False, (
                "Agora Fast-Path Invariant: Direct CLI order/move commands are prohibited in conversational turns. "
                "Orders and vessel movement must be emitted directly via Discord chat syntax (BUY/SELL <qty> <good> @ <px> AT <st>) "
                "so agora_announcer.py submits them. Keep turns to zero tools."
            )
        if re.search(r"agora_play\.py\s+(?:status|eval)\b", cmd):
            agora_calls = state.get("agora_eval_calls", 0) + 1
            state["agora_eval_calls"] = agora_calls
            if agora_calls > 1:
                save_state(state, state_file)
                return False, (
                    f"Agora Fast-Path Invariant: Detected {agora_calls} agora_play inspection calls in a single turn. "
                    "The Discord round announcement already embeds all quotes. Keep Agora turns to 0 tools (or at most 1 fast eval call)."
                )
            _finalize_and_save_state(state, state_file, name, args)
            return True, ""

        # A0. Ouroboros Process-Polling Loop Guard
        if is_ouroboros_polling_loop(cmd):
            return False, (
                "Ouroboros Polling Guard Triggered: Manual shell process-polling loops ('while ... pgrep/ps ... sleep') "
                "are prohibited because 'pgrep -f' matches its own bash -c wrapper and deadlocks the turn. "
                "For SDK background tasks, stop calling tools silently and let the task completion event wake you; "
                "for jobs >90 seconds, use 'python3 /workspace/tools/forge.py job start ...' (or detached_runner.py)."
            )

        # A0a. Container /proc/1/fd FIFO Stream Read Guard
        if is_proc_fd_stream_read(cmd):
            return False, (
                "Proc FD Stream Guard Triggered: Reading container stdio pipes (/proc/1/fd/* or /proc/self/fd/*) "
                "with tail/cat/head/grep blocks indefinitely on open FIFO streams and wedges the turn. "
                "Inspect log files in /workspace/logs/ or /tmp/ directly instead."
            )

        # A0a-2. Shared /opt/zero Symlink Tamper Guard
        if is_opt_zero_symlink_tamper(cmd):
            return False, (
                "Read-Only Shared Mount Guard: Do NOT unlink or overwrite /opt/zero symlinks via shell commands (cp --remove-destination / rm), "
                "as that permanently severs discord-ivy-agent from Zero's upstream fixes. "
                "Escalate the required change to Zero in #zero-ops (1544953279664889888) via:\n"
                "python3 /workspace/tools/outbox.py --channel zero-ops --message \"<@1542285964213358633> [Upstream Fix Request] <file & root cause>\" --force && python3 /workspace/tools/outbox.py --flush"
            )

        # A0b. Unbounded Home Assistant Recorder SQLite Guard
        if is_unbounded_ha_sqlite_query(cmd):
            return False, (
                "HA Recorder SQLite Guard Triggered: Querying 'home-assistant_v2.db' without an explicit 'last_updated_ts >' "
                "lower bound causes multi-minute table scans and write-lock stalls. Prefer the Home Assistant REST API "
                "(/api/states or /api/history/period) or include an indexed 'last_updated_ts >' filter with '-readonly'."
            )

        # A0b-2. SQLite Query Guard (Ban SELECT *, Ban SELECT without LIMIT, Pre-Validate Schema & Columns)
        ok_sql, sql_reason = check_sql_query_guard(cmd, cwd=cwd)
        if not ok_sql:
            return False, sql_reason

        # A0c. Destructive Git Working-Tree Wipe Guard
        if is_destructive_git_working_tree_wipe(cmd, cwd):
            return False, (
                "Destructive Git Working-Tree Wipe Blocked: Commands like 'git reset --hard', 'git checkout-index -f', "
                "'git clean -f', or blanket directory checkouts ('git checkout origin/main -- tools .agents') clobber "
                "uncommitted changes from concurrent sessions/sidecars in /workspace. Commit or 'git stash' dirty files "
                "explicitly before pulling/rebasing."
            )

        # A. Unscoped test suite check (Ban bare pytest in interactive turns)
        if is_unscoped_pytest(cmd):
            return False, (
                f"Batch Invariant Triggered: Unscoped full test suite execution ('{cmd}'). "
                "Scope test execution to the specific test file or function under test (e.g. 'pytest tests/test_feature.py' or 'pytest -k test_name') "
                "to avoid multi-minute turn latency. Let GitHub Actions CI run the full regression suite in the cloud."
            )

        # A00b. Repeated Same-Scratch-Script, Inline Heredoc & >90s Calibration / Command Loop Gate
        if os.environ.get("FORGE_WORKER_ACTIVE") != "1" and not os.path.abspath(cwd or "/workspace").replace("\\", "/").startswith("/tmp/forge-wt-"):
            if (
                "forge.py" not in cmd
                and "detached_runner.py" not in cmd
                and "modal_harness.py" not in cmd
                and "modal_godot_verify.py" not in cmd
                and "modal_verify.py" not in cmd
            ):
                turn_cmds = int(state.get("turn_run_commands", 0)) + 1
                state["turn_run_commands"] = turn_cmds
                turn_elapsed = now - float(state.get("turn_start_time", now) or now)

                scratch_m = _SCRATCH_SCRIPT_EXEC_RE.search(cmd)
                if scratch_m:
                    raw_script = scratch_m.group("script")
                    norm_script = os.path.abspath(os.path.join(cwd or "/workspace", raw_script)).replace("\\", "/")
                    runs_map = dict(state.get("scratch_script_runs", {}))
                    run_cnt = int(runs_map.get(norm_script, 0)) + 1
                    runs_map[norm_script] = run_cnt
                    state["scratch_script_runs"] = runs_map
                    total_scratch = int(state.get("total_scratch_runs", 0)) + 1
                    state["total_scratch_runs"] = total_scratch
                    is_calib_script = is_calibration_scratch_script(norm_script)
                    if is_calib_script:
                        calib_runs = int(state.get("calibration_scratch_runs", 0)) + 1
                        if total_scratch > MAX_CALIBRATION_SCRATCH_RUNS_AFTER_90S:
                            calib_runs = max(calib_runs, total_scratch)
                        state["calibration_scratch_runs"] = calib_runs

                    if run_cnt > MAX_SAME_SCRATCH_SCRIPT_RUNS:
                        save_state(state, state_file)
                        return False, (
                            f"FORGE Scratch Loop Gate Triggered: Executed '{os.path.basename(norm_script)}' {run_cnt} times in this turn "
                            f"(inline limit: {MAX_SAME_SCRATCH_SCRIPT_RUNS}). Do not run iterative edit-and-rerun loops on the same scratch script "
                            "inline in chat. Consolidate parameter sweeps into a single vectorized pass, offload to Modal, or launch genuine long jobs (>90s) via:\n"
                            "python3 /workspace/tools/forge.py job start --name \"<Job Name>\" --command \"python3 " + norm_script + "\"\n"
                            "(or enqueue multi-file edits via 'python3 /workspace/tools/forge.py enqueue <plan.json>')."
                        )

                    if turn_elapsed > FORGE_TIER1_INLINE_SECONDS and (
                        (is_calib_script and total_scratch > MAX_CALIBRATION_SCRATCH_RUNS_AFTER_90S)
                        or total_scratch > MAX_DIAGNOSTIC_SCRATCH_RUNS_AFTER_90S
                    ):
                        save_state(state, state_file)
                        return False, (
                            f"FORGE 90s Calibration Offload Gate Triggered: Interactive turn has been running for {int(turn_elapsed)}s "
                            f"(>{int(FORGE_TIER1_INLINE_SECONDS)}s Tier 1 ceiling) and executed {total_scratch} scratch/tmp scripts "
                            f"(latest: '{os.path.basename(norm_script)}'). Do not run multi-minute iterative script sweeps inline by renaming scripts "
                            "or moving them to /tmp/. Launch genuine long calibration/benchmark jobs (>90s) via:\n"
                            "python3 /workspace/tools/forge.py job start --name \"<Job Name>\" --command \"python3 " + norm_script + "\"\n"
                            "(or offload to '/baseball/big-projections/modal_harness.py' / enqueue a ForgePlan via 'python3 /workspace/tools/forge.py enqueue <plan.json>')."
                        )
                elif is_inline_python_probe(cmd):
                    total_scratch = int(state.get("total_scratch_runs", 0)) + 1
                    state["total_scratch_runs"] = total_scratch
                    calib_runs = int(state.get("calibration_scratch_runs", 0)) + 1
                    if total_scratch > MAX_CALIBRATION_SCRATCH_RUNS_AFTER_90S:
                        calib_runs = max(calib_runs, total_scratch)
                    state["calibration_scratch_runs"] = calib_runs
                    if turn_elapsed > FORGE_TIER1_INLINE_SECONDS and calib_runs > MAX_CALIBRATION_SCRATCH_RUNS_AFTER_90S:
                        save_state(state, state_file)
                        return False, (
                            f"FORGE 90s Calibration Offload Gate Triggered: Interactive turn has been running for {int(turn_elapsed)}s "
                            f"(>{int(FORGE_TIER1_INLINE_SECONDS)}s Tier 1 ceiling) and executed {total_scratch} inline Python / scratch probes. "
                            "Do not run multi-minute iterative heredoc or SSH loops inline in chat. For genuine long jobs (>90s), package the task into a script and launch via:\n"
                            "python3 /workspace/tools/forge.py job start --name \"<Job Name>\" --command \"python3 /workspace/scratch/<script>.py\"\n"
                            "(or enqueue a multi-chunk ForgePlan via 'python3 /workspace/tools/forge.py enqueue <plan.json>')."
                        )

                if turn_elapsed > FORGE_TIER1_INLINE_SECONDS and turn_cmds > MAX_INLINE_RUN_COMMANDS_AFTER_90S:
                    save_state(state, state_file)
                    return False, (
                        f"FORGE 90s Command Budget Gate Triggered: Interactive turn has been running for {int(turn_elapsed)}s "
                        f"(>{int(FORGE_TIER1_INLINE_SECONDS)}s Tier 1 ceiling) and executed {turn_cmds} inline shell commands "
                        f"(limit after 90s: {MAX_INLINE_RUN_COMMANDS_AFTER_90S}). Long-running multi-step builds, remote patches, and "
                        "iterative debugging must be offloaded to #forge via:\n"
                        "python3 /workspace/tools/forge.py job start --name \"<Job Name>\" --command \"python3 /workspace/scratch/<script>.py\"\n"
                        "(or 'python3 /workspace/tools/forge.py enqueue <plan.json>')."
                    )

        # B. Consecutive inline Python probe check (python3 -c / heredoc loops)
        if is_inline_python_probe(cmd):
            state["consecutive_inline_python"] = state.get("consecutive_inline_python", 0) + 1
            if state["consecutive_inline_python"] > MAX_CONSECUTIVE_INLINE_PYTHON:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['consecutive_inline_python']} consecutive inline Python probe commands "
                    f"(limit: {MAX_CONSECUTIVE_INLINE_PYTHON}). Consolidate multi-step diagnostics or parameter sweeps into a single "
                    "reusable script in scratch/ instead of serial inline heredocs."
                )
            state["cmd_inspections"] = 0
            state["consecutive_file_views"] = 0
            state["same_file_view_count"] = 0
            state["same_file_edit_count"] = 0
            state["consecutive_git_cmds"] = 0
        else:
            state["consecutive_inline_python"] = 0
            # C. Serial unbatched git lifecycle check
            if is_single_git_lifecycle_cmd(cmd):
                state["consecutive_git_cmds"] = state.get("consecutive_git_cmds", 0) + 1
                if state["consecutive_git_cmds"] > MAX_CONSECUTIVE_GIT_CMDS:
                    save_state(state, state_file)
                    return False, (
                        f"Batch Invariant Triggered: Detected {state['consecutive_git_cmds']} consecutive unbatched git lifecycle commands in serial. "
                        "Please bundle git operations (e.g. 'git add <files> && git commit -m \"...\" && git push && gh pr create') into a single compound "
                        "command or script to minimize tool roundtrips."
                    )
                state["cmd_inspections"] = 0
                state["consecutive_file_views"] = 0
                state["same_file_view_count"] = 0
                state["same_file_edit_count"] = 0
                state["agora_eval_calls"] = 0
            else:
                state["consecutive_git_cmds"] = 0
                if is_batched_or_mutating_cmd(cmd):
                    state["cmd_inspections"] = 0
                    state["consecutive_file_views"] = 0
                    state["same_file_view_count"] = 0
                    state["same_file_edit_count"] = 0
                    state["agora_eval_calls"] = 0
                elif is_single_shell_inspection(cmd):
                    state["cmd_inspections"] = state.get("cmd_inspections", 0) + 1
                    if state["cmd_inspections"] > MAX_CONSECUTIVE_SHELL_INSPECTIONS:
                        save_state(state, state_file)
                        return False, (
                            f"Batch Invariant Triggered: Detected {state['cmd_inspections']} consecutive single-line inspection commands in serial. "
                            "Please bundle your diagnostic checks into a compound one-liner or a single Python scratch script in /workspace/scratch/ "
                            "to minimize tool roundtrips."
                        )
                else:
                    state["cmd_inspections"] = 0

        _record_forge_offload_if_applicable(cmd, cwd, state)
        _finalize_and_save_state(state, state_file, name, args)
        return True, ""

    # 2. view_file / read_file check
    if name in ("view_file", "read_file"):
        target_file = args.get("AbsolutePath") or args.get("Path") or ""
        start_line = args.get("StartLine")
        end_line = args.get("EndLine")
        is_wide_slice = (start_line is None and end_line is None) or (
            isinstance(start_line, int) and isinstance(end_line, int) and (end_line - start_line) >= WIDE_VIEW_MIN_LINES
        )
        state["cmd_inspections"] = 0
        state["consecutive_inline_python"] = 0

        if target_file and target_file == state.get("last_viewed_file"):
            state["same_file_view_count"] = state.get("same_file_view_count", 0) + 1
            allowed_same = MAX_WIDE_SAME_FILE_VIEWS if is_wide_slice else MAX_CONSECUTIVE_SAME_FILE_VIEWS
            if state["same_file_view_count"] > allowed_same:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['same_file_view_count']} consecutive {name} calls on '{target_file}'. "
                    f"{name} supports viewing up to 800 lines in a single call. Omit StartLine/EndLine to view the file in one shot, "
                    "or write a targeted Python scratch script in /workspace/scratch/ to inspect AST/regexes in a single pass."
                )
        else:
            state["last_viewed_file"] = target_file
            state["same_file_view_count"] = 1

        state["consecutive_file_views"] = state.get("consecutive_file_views", 0) + 1
        if state["consecutive_file_views"] > MAX_CONSECUTIVE_FILE_VIEWS:
            save_state(state, state_file)
            return False, (
                f"Batch Invariant Triggered: Detected {state['consecutive_file_views']} consecutive serial {name} calls. "
                "Please bundle multi-file inspections into a single Python scratch script in /workspace/scratch/ "
                "to inspect multiple files in a single pass."
            )

        _finalize_and_save_state(state, state_file, name, args)
        return True, ""

    # 3. replace_file_content / edit_file check
    if name in ("replace_file_content", "edit_file", "multi_replace_file_content"):
        target_file = args.get("TargetFile") or args.get("AbsolutePath") or args.get("Path") or ""
        ok_sec, sec_reason = check_secrets_mutation_gate(target_file, cwd=cwd)
        if not ok_sec:
            return False, sec_reason

        state["cmd_inspections"] = 0
        state["consecutive_inline_python"] = 0
        state["consecutive_file_views"] = 0
        state["same_file_view_count"] = 0

        ok_ro, ro_reason = check_readonly_shared_mount(target_file, cwd)
        if not ok_ro:
            return False, ro_reason

        ok_syn, syn_reason = check_hook_python_syntax(name, args, cwd)
        if not ok_syn:
            return False, syn_reason

        ok_tier, tier_reason = _check_inline_production_edit_gate(target_file, cwd, state, state_file, now=now)
        if not ok_tier:
            return False, tier_reason

        state["last_edited_file"] = target_file
        state["same_file_edit_count"] = 0

        _finalize_and_save_state(state, state_file, name, args)
        return True, ""

    # 4. write_to_file / write_file resets consecutive counters and blocks LLM-in-the-loop versioned scratch sweep loops (sim_v1..v14.py)
    if name in ("write_to_file", "write_file"):
        target_file = args.get("TargetFile") or args.get("AbsolutePath") or args.get("Path") or ""
        ok_sec, sec_reason = check_secrets_mutation_gate(target_file, cwd=cwd)
        if not ok_sec:
            return False, sec_reason

        state["cmd_inspections"] = 0
        state["consecutive_inline_python"] = 0
        state["consecutive_file_views"] = 0
        state["last_viewed_file"] = ""
        state["same_file_view_count"] = 0
        state["last_edited_file"] = ""
        state["same_file_edit_count"] = 0
        state["consecutive_git_cmds"] = 0

        ok_ro, ro_reason = check_readonly_shared_mount(target_file, cwd)
        if not ok_ro:
            return False, ro_reason

        ok_syn, syn_reason = check_hook_python_syntax(name, args, cwd)
        if not ok_syn:
            return False, syn_reason

        if target_file and _VERSIONED_SCRATCH_SWEEP_RE.search(target_file):
            sweeps = list(state.get("versioned_scratch_sweeps", []))
            if target_file not in sweeps:
                sweeps.append(target_file)
            state["versioned_scratch_sweeps"] = sweeps
            if len(sweeps) > MAX_VERSIONED_SCRATCH_SWEEPS:
                save_state(state, state_file)
                return False, (
                    f"Batch & Modal Offload Invariant Triggered: Detected {len(sweeps)} versioned scratch sweep scripts "
                    f"({', '.join(os.path.basename(s) for s in sweeps[-4:])}) in a single turn (limit: {MAX_VERSIONED_SCRATCH_SWEEPS}). "
                    "Do not run iterative LLM-in-the-loop scratch simulation sweeps (sim_v1..vN.py) that burn the 60-minute turn ceiling. "
                    "Evaluate all parameter combinations inside a single vectorized Python loop or offload multi-combo sweeps to "
                    "'/baseball/big-projections/modal_harness.py --mode grid' (or 'forge.py job start' / detached_runner.py for jobs >90s)."
                )

        ok_tier, tier_reason = _check_inline_production_edit_gate(target_file, cwd, state, state_file, now=now)
        if not ok_tier:
            return False, tier_reason

        _finalize_and_save_state(state, state_file, name, args)
        return True, ""

    _finalize_and_save_state(state, state_file, name, args)
    return True, ""


def throttle_heavy_compiler_command(cmd: str) -> str:
    """Cap PlatformIO / C++ firmware builds to -j 2 and nice -n 10 on Host 2 so 8-job xtensa-esp32s3-elf-g++ swarms never thrash swap."""
    if not cmd or not re.search(r"\b(?:platformio|pio)\s+run\b", cmd):
        return cmd

    def _rewrite_pio_seg(match: re.Match) -> str:
        full = match.group(0)
        # Clamp explicit -j N / --jobs N > 2 down to 2
        if re.search(r"(?:-j|--jobs)(?:\s+|=)\d+", full):
            throttled = re.sub(
                r"(-j|--jobs)(\s+|=)(\d+)",
                lambda m: f"{m.group(1)}{m.group(2)}{min(2, max(1, int(m.group(3))))}",
                full,
            )
        else:
            throttled = f"{full} -j 2"
        if os.name != "nt" and not re.match(r"^\s*nice\b", throttled):
            throttled = f"nice -n 10 {throttled}"
        return throttled

    return re.sub(
        r"(?:nice\s+-n\s+\d+\s+)?(?:python[0-9.]*\s+-m\s+)?(?:platformio|pio)\s+run(?:\s+[^\n;&|]+)?",
        _rewrite_pio_seg,
        cmd,
    )


_PROSE_CLI_FLAG_PREFIX_RE = re.compile(
    r"(?:^|[\s;&|])(?:--(?:message|body|summary|details|title)|-m)(?:\s+|=)$"
)


def escape_double_quoted_cli_prose(cmd: str) -> str:
    """Escape unescaped '$' variables ($0, $100, $var) and backticks (`...`) inside double-quoted
    CLI prose arguments (--message, -m, --body, --summary, --details, --title) before bash executes
    the command, preventing shell positional parameter/backtick corruption on outbox/mail sends.
    """
    if not cmd or ('"' not in cmd) or ("$" not in cmd and "`" not in cmd):
        return cmd

    out: list[str] = []
    i = 0
    n = len(cmd)
    state = "normal"  # normal | single | double | prose_double

    while i < n:
        ch = cmd[i]
        if state == "normal":
            if ch == "\\" and i + 1 < n:
                out.append(cmd[i : i + 2])
                i += 2
                continue
            if ch == "'":
                state = "single"
                out.append(ch)
                i += 1
                continue
            if ch == '"':
                prefix = cmd[max(0, i - 32) : i]
                if _PROSE_CLI_FLAG_PREFIX_RE.search(prefix):
                    state = "prose_double"
                else:
                    state = "double"
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
        elif state == "single":
            if ch == "'":
                state = "normal"
            out.append(ch)
            i += 1
        elif state == "double":
            if ch == "\\" and i + 1 < n:
                out.append(cmd[i : i + 2])
                i += 2
                continue
            if ch == '"':
                state = "normal"
            out.append(ch)
            i += 1
        elif state == "prose_double":
            if ch == "\\" and i + 1 < n:
                out.append(cmd[i : i + 2])
                i += 2
                continue
            if ch == '"':
                state = "normal"
                out.append(ch)
                i += 1
                continue
            if ch == "`":
                out.append("\\`")
                i += 1
                continue
            if ch == "$" and i + 1 < n and re.match(r"[0-9a-zA-Z_{(@*#?$-]", cmd[i + 1]):
                out.append("\\$")
                i += 1
                continue
            out.append(ch)
            i += 1

    return "".join(out)


def check_batching(cmd: str) -> tuple[bool, str]:
    return check_tool_use("run_command", {"CommandLine": cmd})


def is_single_inspection(cmd: str) -> bool:
    return is_single_shell_inspection(cmd)


def is_batched_or_mutating(cmd: str) -> bool:
    return is_batched_or_mutating_cmd(cmd)


def _maybe_trigger_workstation_auto_wake(cwd: str) -> None:
    """Debounced auto-wake sync for workstation replicas on Windows.
    Spawns background vault auto-wake sync if last_vault_pull.json is missing or older than 6 hours.
    """
    try:
        data_dir = os.path.join(cwd, "data")
        pull_file = os.path.join(data_dir, "last_vault_pull.json")
        now = time.time()
        if os.path.exists(pull_file):
            try:
                with open(pull_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ts = float(data.get("timestamp", 0.0))
                if (now - ts) < 21600:
                    return
            except Exception:
                pass

        os.makedirs(data_dir, exist_ok=True)
        payload = {"timestamp": now, "status": "waking"}
        if os.path.exists(pull_file):
            try:
                with open(pull_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    existing["timestamp"] = now
                    payload = existing
            except Exception:
                pass
        with open(pull_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        vault_sync_script = os.path.join(cwd, "tools", "vault_sync.py")
        if os.path.exists(vault_sync_script):
            import subprocess

            subprocess.Popen(
                [sys.executable, vault_sync_script, "auto-wake"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
            )
    except Exception:
        pass


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({"decision": "allow"}))
            return
        payload = json.loads(raw)
        tool_call = payload.get("toolCall", {})
        name = tool_call.get("name", "")
        args = tool_call.get("args", {})
        cwd = payload.get("cwd", "/workspace")

        # 1. Unified Search Safety Gate (in-process, avoiding a second Python subprocess spawn)
        if name in ("grep_search", "find_by_name", "run_command"):
            try:
                _here = os.path.dirname(os.path.abspath(__file__))
                _hooks_dir = os.path.join(_resolve_repo_root(), ".agents", "hooks")
                for _sg_dir in (_here, _hooks_dir):
                    if os.path.isdir(_sg_dir) and _sg_dir not in sys.path:
                        sys.path.insert(0, _sg_dir)
                from search_guard import check_search_path, check_command_line, normalize_path
                if name == "grep_search":
                    s_ok, s_reason = check_search_path(args.get("SearchPath", ""), cwd)
                    if not s_ok:
                        print(json.dumps({"decision": "deny", "reason": s_reason}))
                        return
                elif name == "find_by_name":
                    search_dir = args.get("SearchDirectory", "")
                    max_depth = args.get("MaxDepth")
                    s_ok, s_reason = check_search_path(search_dir, cwd)
                    if not s_ok:
                        if not (normalize_path(search_dir, cwd) == "/workspace" and max_depth is not None and int(max_depth) <= 2):
                            print(json.dumps({"decision": "deny", "reason": s_reason}))
                            return
                elif name == "run_command":
                    s_ok, s_reason = check_command_line(args.get("CommandLine", ""), args.get("Cwd", cwd))
                    if not s_ok:
                        print(json.dumps({"decision": "deny", "reason": s_reason}))
                        return
            except Exception:
                pass

        # 1b. Shell Double-Quote Corruption Guard (run before check_tool_use so denied commands never mutate state)
        if name == "run_command" and not args.get("IsDaemon", False):
            try:
                raw_cmd_pre = args.get("CommandLine", "")
                if escape_double_quoted_cli_prose(raw_cmd_pre) != raw_cmd_pre:
                    print(
                        json.dumps(
                            {
                                "decision": "deny",
                                "reason": (
                                    "Shell Double-Quote Corruption Guard: Detected unescaped '$' ($0, $1, $var) or backticks (`...`) "
                                    "inside a double-quoted CLI prose argument (--message, -m, --body, --summary, --title). "
                                    "Bash expands '$0' to 'bash', eats '$1..$9', and executes '`...`' as command substitution before Python runs. "
                                    "Use single quotes (--message '...') or a single-quoted python3 -c script."
                                ),
                            }
                        )
                    )
                    return
            except Exception:
                pass

        # 2. Tool Batching Guard (reset turn state automatically when a new agy process/turn starts)
        try:
            agy_pid = _get_agy_ancestor_pid()
            turn_anchor = _get_current_turn_Step_anchor(agy_pid, in_flight_file=IN_FLIGHT_TURN_FILE)
            has_conv = _has_conv_id(turn_anchor, in_flight_file=IN_FLIGHT_TURN_FILE, agy_pid=agy_pid)
            sess_id = ":".join(str(turn_anchor).split(":")[:2]) if has_conv else str(turn_anchor)
            st = load_state(STATE_FILE, session_id=sess_id)
            # Reset turn state when a new user turn / prompt arrives (turn_anchor changes)
            if st.get("agy_ppid") and str(st.get("agy_ppid")) != str(turn_anchor):
                fresh = _default_state(time.time())
                fresh["_session_id"] = sess_id
                save_state(fresh, STATE_FILE, session_id=sess_id)
                if os.name == "nt":
                    _maybe_trigger_workstation_auto_wake(cwd)
            elif not st.get("agy_ppid"):
                if os.name == "nt":
                    _maybe_trigger_workstation_auto_wake(cwd)
        except Exception:
            pass

        allowed, reason = check_tool_use(name, args, cwd=cwd, agy_pid=agy_pid)
        if not allowed:
            print(json.dumps({"decision": "deny", "reason": reason}))
            return
        try:
            st_after = load_state(STATE_FILE, session_id=sess_id)
            st_after["agy_ppid"] = _get_current_turn_Step_anchor(
                _get_agy_ancestor_pid(),
                in_flight_file=IN_FLIGHT_TURN_FILE,
            )
            st_after["_session_id"] = sess_id
            save_state(st_after, STATE_FILE, session_id=sess_id)
        except Exception:
            pass

        resp = {"decision": "allow"}
        if name == "run_command" and not args.get("IsDaemon", False):
            overwrites = {}
            wait_ms = args.get("WaitMsBeforeAsync", 0)
            if isinstance(wait_ms, (int, float)) and wait_ms < 10000:
                overwrites["WaitMsBeforeAsync"] = 10000

            try:
                raw_cmd = args.get("CommandLine", "")
                safe_cmd = escape_double_quoted_cli_prose(raw_cmd)
                if safe_cmd != raw_cmd:
                    print(
                        json.dumps(
                            {
                                "decision": "deny",
                                "reason": (
                                    "Shell Double-Quote Corruption Guard: Detected unescaped '$' ($0, $1, $var) or backticks (`...`) "
                                    "inside a double-quoted CLI prose argument (--message, -m, --body, --summary, --title). "
                                    "Bash expands '$0' to 'bash', eats '$1..$9', and executes '`...`' as command substitution before Python runs. "
                                    "Use single quotes (--message '...') or a single-quoted python3 -c script."
                                ),
                            }
                        )
                    )
                    return
                throttled_cmd = throttle_heavy_compiler_command(safe_cmd)
                for tools_dir in ("/workspace/tools", "/opt/zero/workspace/tools"):
                    if os.path.isdir(tools_dir) and tools_dir not in sys.path:
                        sys.path.insert(0, tools_dir)
                from output_shaper import rewrite_command_for_shaping
                shaped_cmd = rewrite_command_for_shaping(throttled_cmd)
                if shaped_cmd and shaped_cmd != raw_cmd:
                    overwrites["CommandLine"] = shaped_cmd
                elif throttled_cmd and throttled_cmd != raw_cmd:
                    overwrites["CommandLine"] = throttled_cmd
            except Exception:
                pass

            if overwrites:
                resp["overwrite"] = overwrites

        print(json.dumps(resp))
    except Exception:
        print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()
