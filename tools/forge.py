#!/usr/bin/env python3
"""
forge.py — FORGE Engine (Federated Orchestration, Runtime & Gated Execution)

Unifies four core runtime pillars in a single WAL-mode SQLite state machine (/workspace/data/forge.db):
1. Activity-Renewed Lease & Heartbeat Engine: Replaces blind wall-clock guillotines with 4-signal
   ground-truth progress leases (stdout deltas, child PIDs, SDK task logs, subagent transcripts)
   plus a 3-strike runaway tool-loop circuit breaker.
2. Multi-Chunk Spec-Driven Build Queue (#forge): Executes Tier 2/3 engineering plans in isolated
   git worktrees using gemini-3.8-flash-high (with escalation to gemini-4-argon-medium), enforcing
   pre-enqueue target-file overlap guards, Turn-0 AST/symbol snippet hydration (target_symbols +
   shared_context), per-chunk multi-repo routing, optional verification_cmd with auto-baseline
   lint/test fallback, live #forge widget updates, and originating-channel completion wakeups.
3. Native Detached Job Runner (`forge.py job`): Spawns and supervises long-running non-LLM shell
   jobs (>90s) directly in SQLite (`forge_jobs`) with flock idempotency and outbox delivery,
   retiring standalone `detached_runner.py` metadata files.
4. Deterministic Decisions Framework: Backs inline Discord Approve / Edit / Dismiss cards with
   SQLite action payloads executed in <1s at zero token cost.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import glob
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import discord
import requests

WORKSPACE_DIR = Path("/workspace")
if str(WORKSPACE_DIR) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_DIR))
TOOLS_DIR = WORKSPACE_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

DATA_DIR = WORKSPACE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FORGE_LOGS_DIR = DATA_DIR / "forge_logs"
FORGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
TASKS_DIR = DATA_DIR / "detached_tasks"
TASKS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_BRAIN_DIR = Path("/root/.gemini/antigravity-cli/brain")
PT_TZ = ZoneInfo("America/Los_Angeles")

FORGE_CHANNEL_ID = 1562899941595160657
FORGE_HEADER_MESSAGE_ID = 1563755730161631280
ZERO_CHAT_CHANNEL_ID = 1542081375287640084
ZERO_OPS_CHANNEL_ID = 1544953279664889888
BASEBALL_CHANNEL_ID = 1548196929308065893
OWNER_USER_ID = int(os.getenv("DISCORD_OWNER_ID", "179407724335988736"))

DEFAULT_WORKER_MODEL = "gemini-3.8-flash-high"
ESCALATION_WORKER_MODEL = "gemini-4-argon-medium"
DEFAULT_LEASE_TTL_SECONDS = 300.0


def _build_low_priority_prefix() -> list[str]:
    """Return nice/ionice prefix when available so background FORGE workers yield CPU/IO to interactive turns."""
    prefix: list[str] = []
    if shutil.which("nice"):
        prefix.extend(["nice", "-n", "15"])
    if shutil.which("ionice"):
        prefix.extend(["ionice", "-c", "2", "-n", "7"])
    return prefix


def _apply_low_priority() -> None:
    """Lower CPU/IO scheduling priority inside a forked child process (preexec_fn)."""
    try:
        os.nice(15)
    except Exception:
        pass
    try:
        if shutil.which("ionice"):
            subprocess.run(
                ["ionice", "-c", "2", "-n", "7", "-p", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
    except Exception:
        pass


def resolve_default_forge_db_path() -> Path:
    """Resolve the shared cross-container FORGE SQLite path on Host 2 (/baseball/data/forge.db) when present."""
    env_override = os.getenv("FORGE_DB_PATH", "").strip()
    if env_override:
        return Path(env_override)
    shared_baseball_data = Path("/baseball/data")
    if shared_baseball_data.exists() and shared_baseball_data.is_dir():
        return shared_baseball_data / "forge.db"
    return DATA_DIR / "forge.db"


FORGE_DB_PATH = resolve_default_forge_db_path()

PEER_FORGE_DB_CANDIDATES: list[Path] = [
    Path("/baseball/data/forge.db"),
    Path("/opt/zero/workspace/data/forge.db"),
    DATA_DIR / "forge.db",
]


def _get_peer_forge_db_paths(primary_db_path: Path | str = FORGE_DB_PATH) -> list[Path]:
    """Return readable peer FORGE SQLite paths across containers when querying production DB."""
    primary = Path(primary_db_path)
    try:
        if primary.resolve() != FORGE_DB_PATH.resolve():
            return []
    except Exception:
        return []

    peers: list[Path] = []
    seen: set[Path] = set()
    try:
        seen.add(primary.resolve())
    except Exception:
        seen.add(primary)

    for p in PEER_FORGE_DB_CANDIDATES:
        try:
            if p.exists() and p.is_file() and os.access(p, os.R_OK):
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    peers.append(p)
        except Exception:
            pass
    return peers


def _query_unified_rows(
    sql: str,
    params: tuple = (),
    db_path: Path | str = FORGE_DB_PATH,
    sort_key: Optional[str] = None,
    reverse: bool = True,
    limit: Optional[int] = None,
) -> list[dict]:
    """Execute query across primary DB and any readable peer DBs, deduplicating rows by `id`."""
    db_paths = [Path(db_path)] + _get_peer_forge_db_paths(db_path)
    by_id: dict[str, dict] = {}
    rows_without_id: list[dict] = []

    def _row_ts(r: dict) -> float:
        for k in ("updated_at", "end_time", "start_time", "created_at"):
            v = r.get(k)
            if v is not None:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    pass
        return 0.0

    seen_paths: set[Path] = set()
    for p in db_paths:
        try:
            rp = p.resolve()
        except Exception:
            rp = p
        if rp in seen_paths:
            continue
        seen_paths.add(rp)

        try:
            with get_db_connection(p) as conn:
                fetched = conn.execute(sql, params).fetchall()
                for item in fetched:
                    d = dict(item)
                    rid = d.get("id")
                    if rid is not None:
                        rid_str = str(rid)
                        if rid_str not in by_id or _row_ts(d) >= _row_ts(by_id[rid_str]):
                            by_id[rid_str] = d
                    else:
                        rows_without_id.append(d)
        except Exception:
            pass

    combined = list(by_id.values()) + rows_without_id

    if sort_key:
        def _get_sort_val(r: dict):
            val = r.get(sort_key)
            if val is None:
                return float("-inf") if reverse else float("inf")
            try:
                return float(val)
            except (ValueError, TypeError):
                return str(val)

        combined.sort(key=_get_sort_val, reverse=reverse)

    if limit is not None and limit > 0:
        combined = combined[:limit]

    return combined


def is_ivy_container() -> bool:
    """Return True if executing inside discord-ivy-agent (#baseball)."""
    return (
        os.getenv("DISCORD_CHANNEL_ID", "").strip() == str(BASEBALL_CHANNEL_ID)
        or Path("/opt/zero/workspace").exists()
    )


def _get_ancestor_pids() -> set[int]:
    """Return the set of ancestor PIDs up to PID 1 for the current process."""
    pids = set()
    cur = os.getpid()
    for _ in range(12):
        if cur <= 1 or cur in pids:
            break
        pids.add(cur)
        try:
            stat_txt = Path(f"/proc/{cur}/stat").read_text(encoding="utf-8")
            rparen = stat_txt.rfind(")")
            if rparen != -1:
                fields = stat_txt[rparen + 2 :].split()
                if len(fields) >= 2 and fields[1].isdigit():
                    cur = int(fields[1])
                    continue
        except Exception:
            pass
        break
    return pids


def _is_channel_turn_actively_in_flight(channel_id_or_name: str, max_age_s: float = 600.0) -> bool:
    """Return True if the specified channel currently has an active in-flight agent turn."""
    in_flight_file = DATA_DIR / "in_flight_turn.json"
    if not in_flight_file.exists():
        return False
    try:
        raw_ch = str(channel_id_or_name).strip().lstrip("#")
        if raw_ch.isdigit():
            target_cid = raw_ch
        elif raw_ch == "baseball":
            target_cid = str(BASEBALL_CHANNEL_ID)
        elif raw_ch == "zero-chat":
            target_cid = str(ZERO_CHAT_CHANNEL_ID)
        elif raw_ch == "zero-ops":
            target_cid = str(ZERO_OPS_CHANNEL_ID)
        else:
            target_cid = raw_ch
            try:
                from tools.outbox import resolve_channel
                _, res_id = resolve_channel(raw_ch)
                if res_id:
                    target_cid = str(res_id)
            except Exception:
                pass
        data = json.loads(in_flight_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        now = time.time()
        entry = data.get(target_cid)
        if not isinstance(entry, dict) and str(data.get("channel_id", "")) == target_cid:
            entry = data
        if isinstance(entry, dict):
            ts = float(entry.get("ts") or 0.0)
            if (now - ts) < max_age_s:
                pid = entry.get("pid")
                if isinstance(pid, int) and pid > 1:
                    return Path(f"/proc/{pid}").exists()
                return True
    except Exception:
        pass
    return False


def get_default_origin_channel() -> str:
    """Return the default origin channel name/ID for the active container or in-flight turn."""
    env_cid = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    if env_cid == str(BASEBALL_CHANNEL_ID):
        return "baseball"
    if env_cid:
        return env_cid

    # Fallback: inspect /workspace/data/in_flight_turn.json to match ancestor agy worker PID
    # or single active in-flight turn when DISCORD_CHANNEL_ID is not set in the environment.
    in_flight_file = DATA_DIR / "in_flight_turn.json"
    if in_flight_file.exists():
        try:
            data = json.loads(in_flight_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                ancestors = _get_ancestor_pids()
                now = time.time()
                active_candidates: list[tuple[float, str]] = []
                for k, v in data.items():
                    if not str(k).isdigit() or not isinstance(v, dict):
                        continue
                    ts = float(v.get("ts") or 0.0)
                    if (now - ts) > 900.0:
                        continue
                    v_pid = v.get("pid")
                    if isinstance(v_pid, int) and v_pid in ancestors:
                        return "baseball" if str(k) == str(BASEBALL_CHANNEL_ID) else str(k)
                    active_candidates.append((ts, str(k)))
                if len(active_candidates) == 1:
                    only_cid = active_candidates[0][1]
                    return "baseball" if only_cid == str(BASEBALL_CHANNEL_ID) else only_cid
        except Exception:
            pass
    return "zero-chat"


def _get_discord_token(for_forge_channel: bool = False) -> Optional[str]:
    """Retrieve Discord bot token across Zero (/secrets) and Ivy (.env) environments.

    When `for_forge_channel=True`, prefers `VULCAN_BOT_TOKEN` / `FORGE_BOT_TOKEN` if
    configured, or Zero's bot token from `/opt/zero/.env` inside `discord-ivy-agent`.
    """
    if for_forge_channel:
        for env_key in ("VULCAN_BOT_TOKEN", "FORGE_BOT_TOKEN"):
            val = os.getenv(env_key, "").strip()
            if val:
                return val
        for sec_p in (Path("/secrets/env.json"), Path("/opt/zero/secrets/env.json")):
            if sec_p.exists():
                try:
                    sec_data = json.loads(sec_p.read_text(encoding="utf-8"))
                    for env_key in ("VULCAN_BOT_TOKEN", "FORGE_BOT_TOKEN"):
                        val = str(sec_data.get(env_key) or "").strip()
                        if val:
                            return val
                except Exception:
                    pass
    if for_forge_channel and is_ivy_container():
        zero_env = Path("/opt/zero/.env")
        if zero_env.exists():
            try:
                for line in zero_env.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("DISCORD_BOT_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
            except Exception:
                pass
    try:
        from tools.deliver_image import get_discord_token
        return get_discord_token()
    except Exception:
        pass
    for env_cand in (Path("/app/.env"), Path("/workspace/.env"), Path(".env")):
        if env_cand.exists():
            try:
                for line in env_cand.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("DISCORD_BOT_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
            except Exception:
                pass
    return None


VULCAN_WEBHOOK_CACHE_PATHS: tuple[Path, ...] = (
    Path("/baseball/data/.vulcan_webhook.json"),
    DATA_DIR / ".vulcan_webhook.json",
    Path("/opt/zero/workspace/data/.vulcan_webhook.json"),
)

VULCAN_PROFILE_PATHS: tuple[Path, ...] = (
    WORKSPACE_DIR / ".agents" / "profiles" / "vulcan" / "GEMINI.md",
    Path("/opt/zero/workspace/.agents/profiles/vulcan/GEMINI.md"),
)

VULCAN_CORE_RULES: tuple[str, ...] = (
    "tool_roundtrip_minimization.md",
    "no_server_wide_search.md",
    "no_hardcoded_topology.md",
    "silent_background_execution.md",
    "autonomous_github_management.md",
)

VULCAN_BASEBALL_RULES: tuple[str, ...] = (
    "big_projection_engine_invariants.md",
    "sabermetric_modeling_invariants.md",
    "big_board_pro_invariants.md",
    "player_id_identity_invariants.md",
    "git_and_infrastructure_hygiene.md",
)

DEFAULT_VULCAN_GEMINI_MD = """# Vulcan (Verified Unattended Labor & Code Anvil Node) — FORGE Builder Profile

You are **Vulcan**, the autonomous headless build engineer for the FORGE pipeline.
Operate strictly inside your isolated git worktree, modify only Allowed Target Files,
run the Mandatory Verification Gate before finishing, and leave verified changes uncommitted for the FORGE supervisor.
"""


def _get_vulcan_webhook(
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
) -> Optional[dict]:
    """Return Vulcan's #forge Discord Webhook credentials (`{'id': ..., 'token': ...}`) in production."""
    if int(forge_channel_id) != FORGE_CHANNEL_ID or not _is_prod_forge_env(db_path):
        return None
    for cand in VULCAN_WEBHOOK_CACHE_PATHS:
        try:
            if cand.exists() and cand.is_file():
                data = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("id") and data.get("token"):
                    return {"id": str(data["id"]), "token": str(data["token"]), "name": str(data.get("name") or "Vulcan")}
        except Exception:
            pass
    return None


def _post_forge_channel_message(
    payload: dict,
    forge_channel_id: int,
    token: Optional[str],
    db_path: Path | str = FORGE_DB_PATH,
) -> Optional[int]:
    """Post a message/embed to #forge using Vulcan's webhook profile (with bot token fallback)."""
    wh = _get_vulcan_webhook(db_path=db_path, forge_channel_id=forge_channel_id)
    if wh:
        try:
            wh_url = f"https://discord.com/api/v10/webhooks/{wh['id']}/{wh['token']}?wait=true"
            wh_payload = {"username": wh.get("name", "Vulcan"), **payload}
            resp = requests.post(wh_url, json=wh_payload, timeout=10)
            if resp.status_code < 300:
                return int(resp.json()["id"])
        except Exception:
            pass

    if not token:
        return None
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages"
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code < 300:
        return int(resp.json()["id"])
    return None


def _patch_forge_channel_message(
    msg_id: int,
    payload: dict,
    forge_channel_id: int,
    token: Optional[str],
    db_path: Path | str = FORGE_DB_PATH,
) -> tuple[bool, int]:
    """Patch an existing #forge message using Vulcan's webhook (falling back to bot token for legacy messages)."""
    wh = _get_vulcan_webhook(db_path=db_path, forge_channel_id=forge_channel_id)
    if wh:
        try:
            wh_url = f"https://discord.com/api/v10/webhooks/{wh['id']}/{wh['token']}/messages/{msg_id}"
            resp = requests.patch(wh_url, json=payload, timeout=10)
            if resp.status_code < 300:
                return True, resp.status_code
            if resp.status_code == 404 and not token:
                return False, 404
        except Exception:
            pass

    if not token:
        return False, 0
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages/{msg_id}"
    resp = requests.patch(url, headers=headers, json=payload, timeout=10)
    return (resp.status_code < 300), resp.status_code


def stage_vulcan_worktree_profile(
    worktree_dir: Path | str,
    repo_dir: Optional[Path | str] = None,
    target_files: Optional[list[str]] = None,
    origin_channel_id: Optional[int] = None,
) -> dict:
    """Inject Vulcan's isolated headless builder GEMINI.md, hooks.json, and scoped engineering rules into a worktree.

    Snapshots all pre-existing root instruction files and `.agents/` entries so `restore_vulcan_worktree_profile()`
    can restore the exact pre-run git state before `verify_chunk_worktree()` and `git commit` execute.
    Files explicitly listed in `target_files` are never overwritten or stripped.
    """
    wt = Path(worktree_dir)
    if not wt.exists():
        return {"files": {}, "had_agents_dir": False, "had_rules_dir": False, "staged_rules": []}

    def _strip_dot_slash(p: Any) -> str:
        s = str(p or "").strip()
        while s.startswith("./"):
            s = s[2:]
        return s

    norm_targets = {_strip_dot_slash(f) for f in (target_files or []) if _strip_dot_slash(f)}
    root_doc_names = {"GEMINI.md", "AGENTS.md", "agents.md"}
    root_docs_targeted = bool(norm_targets & root_doc_names)

    eff_repo_str = str(Path(repo_dir or wt).resolve())
    is_baseball = (
        eff_repo_str.startswith("/baseball")
        or (origin_channel_id is not None and int(origin_channel_id) == BASEBALL_CHANNEL_ID)
        or is_ivy_container()
    )
    allowed_rules = set(VULCAN_CORE_RULES) | (set(VULCAN_BASEBALL_RULES) if is_baseball else set())

    had_agents_dir = (wt / ".agents").exists()
    had_rules_dir = (wt / ".agents" / "rules").exists()
    had_hooks_dir = (wt / ".agents" / "hooks").exists()
    files_snap: dict[str, dict] = {}

    def _record_snap(rel_p: str) -> None:
        if rel_p in files_snap:
            return
        p = wt / rel_p
        try:
            if p.is_symlink():
                files_snap[rel_p] = {"state": "symlink", "target": os.readlink(p)}
            elif p.is_file():
                files_snap[rel_p] = {"state": "file", "bytes": p.read_bytes(), "mode": p.stat().st_mode}
            else:
                files_snap[rel_p] = {"state": "absent"}
        except Exception:
            files_snap[rel_p] = {"state": "absent"}

    hook_script_names = ("batch_guard.py", "search_guard.py", "time_context.py")
    for rdoc in ("GEMINI.md", "AGENTS.md", "agents.md", "AGY.md", ".agents/hooks.json"):
        _record_snap(rdoc)
    for hname in hook_script_names:
        _record_snap(f".agents/hooks/{hname}")

    rules_dir = wt / ".agents" / "rules"
    if rules_dir.exists() and rules_dir.is_dir():
        for entry in rules_dir.iterdir():
            if entry.is_file() or entry.is_symlink():
                _record_snap(f".agents/rules/{entry.name}")

    for rname in allowed_rules:
        _record_snap(f".agents/rules/{rname}")

    # 1. Stage Vulcan's headless builder GEMINI.md / AGENTS.md / agents.md
    if not root_docs_targeted:
        vulcan_md = DEFAULT_VULCAN_GEMINI_MD
        for cand_p in VULCAN_PROFILE_PATHS:
            if cand_p.exists() and cand_p.is_file():
                try:
                    vulcan_md = cand_p.read_text(encoding="utf-8")
                    break
                except Exception:
                    pass
        for rdoc in ("GEMINI.md", "AGENTS.md", "agents.md"):
            p = wt / rdoc
            try:
                if p.is_symlink() or p.is_file():
                    p.unlink()
                p.write_text(vulcan_md, encoding="utf-8")
            except Exception:
                pass

    # 2. Ensure .agents/hooks.json and .agents/hooks/*.py are staged (so relative 'python hooks/batch_guard.py' works in repos without tracked .agents/hooks/)
    if ".agents/hooks.json" not in norm_targets:
        for hooks_src in (
            WORKSPACE_DIR / ".agents" / "hooks.json",
            Path("/opt/zero/workspace/.agents/hooks.json"),
        ):
            if hooks_src.exists() and hooks_src.is_file():
                try:
                    (wt / ".agents").mkdir(parents=True, exist_ok=True)
                    hp = wt / ".agents" / "hooks.json"
                    if hp.is_symlink() or hp.is_file():
                        hp.unlink()
                    hp.write_bytes(hooks_src.read_bytes())
                except Exception:
                    pass
                break

    hooks_dir = wt / ".agents" / "hooks"
    hook_search_dirs = (
        WORKSPACE_DIR / ".agents" / "hooks",
        Path("/opt/zero/workspace/.agents/hooks"),
    )
    for hname in hook_script_names:
        rel_h = f".agents/hooks/{hname}"
        if rel_h in norm_targets:
            continue
        dest_h = wt / rel_h
        for hdir in hook_search_dirs:
            src_h = hdir / hname
            if src_h.exists() and src_h.is_file():
                try:
                    hooks_dir.mkdir(parents=True, exist_ok=True)
                    if dest_h.is_symlink() or dest_h.is_file():
                        dest_h.unlink()
                    dest_h.write_bytes(src_h.read_bytes())
                except Exception:
                    pass
                break

    # 3. Scope .agents/rules/ to Vulcan engineering rules (+ baseball invariants when applicable)
    rules_dir.mkdir(parents=True, exist_ok=True)
    for entry in list(rules_dir.iterdir()):
        rel_e = f".agents/rules/{entry.name}"
        if entry.name not in allowed_rules and rel_e not in norm_targets:
            try:
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
            except Exception:
                pass

    rule_search_dirs = (
        WORKSPACE_DIR / ".agents" / "rules",
        Path("/opt/zero/workspace/.agents/rules"),
        Path("/baseball/data/vulcan_baseball_rules"),
    )
    staged_rules: list[str] = []
    for rname in sorted(allowed_rules):
        rel_r = f".agents/rules/{rname}"
        if rel_r in norm_targets:
            staged_rules.append(rname)
            continue
        dest_r = wt / rel_r
        for sdir in rule_search_dirs:
            src_r = sdir / rname
            if src_r.exists() and src_r.is_file():
                try:
                    if dest_r.is_symlink() or dest_r.is_file():
                        dest_r.unlink()
                    dest_r.write_bytes(src_r.read_bytes())
                    staged_rules.append(rname)
                except Exception:
                    pass
                break

    return {
        "files": files_snap,
        "had_agents_dir": had_agents_dir,
        "had_rules_dir": had_rules_dir,
        "had_hooks_dir": had_hooks_dir,
        "staged_rules": staged_rules,
        "is_baseball": is_baseball,
    }


def restore_vulcan_worktree_profile(worktree_dir: Path | str, snapshot: dict) -> None:
    """Restore a worktree's pre-existing instruction files and `.agents/` directory after Vulcan finishes."""
    wt = Path(worktree_dir)
    if not wt.exists() or not isinstance(snapshot, dict):
        return

    files_snap: dict[str, dict] = snapshot.get("files") or {}
    for rel_p, info in files_snap.items():
        p = wt / rel_p
        state = info.get("state", "absent")
        try:
            if p.is_symlink() or p.is_file():
                p.unlink()
            if state == "symlink":
                p.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(info["target"], p)
            elif state == "file":
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(info["bytes"])
                if "mode" in info:
                    os.chmod(p, int(info["mode"]) & 0o777)
        except Exception:
            pass

    hooks_dir = wt / ".agents" / "hooks"
    if not snapshot.get("had_hooks_dir") and hooks_dir.exists():
        try:
            shutil.rmtree(hooks_dir / "__pycache__", ignore_errors=True)
            if not any(hooks_dir.iterdir()):
                hooks_dir.rmdir()
        except Exception:
            pass

    rules_dir = wt / ".agents" / "rules"
    if not snapshot.get("had_rules_dir") and rules_dir.exists():
        try:
            if not any(rules_dir.iterdir()):
                rules_dir.rmdir()
        except Exception:
            pass

    agents_dir = wt / ".agents"
    if not snapshot.get("had_agents_dir") and agents_dir.exists():
        try:
            if not any(agents_dir.iterdir()):
                agents_dir.rmdir()
        except Exception:
            pass


def _get_repo_default_branch(repo_dir: Path | str) -> str:
    """Detect the active or default branch ('main', 'master', or 'dev') for a git repository."""
    rp = Path(repo_dir)
    try:
        sym = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(rp),
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = sym.stdout.strip()
        if sym.returncode == 0 and branch and not branch.startswith("forge/"):
            return branch
    except Exception:
        pass
    for cand in ("main", "master", "dev"):
        chk = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{cand}"],
            cwd=str(rp),
            capture_output=True,
            timeout=5,
        )
        if chk.returncode == 0:
            return cand
    return "main"


# ---------------------------------------------------------------------------
# 1. SQLite WAL Database Initialization & Idempotent Schema Migrations
# ---------------------------------------------------------------------------

def get_db_connection(db_path: Path | str = FORGE_DB_PATH) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with busy timeout and foreign keys enabled."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    """Idempotently add a column to an existing SQLite table if missing."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def init_forge_db(db_path: Path | str = FORGE_DB_PATH) -> None:
    """Initialize all FORGE tables, columns, and indexes idempotently."""
    with get_db_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS forge_builds (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                objective TEXT NOT NULL DEFAULT '',
                repo TEXT NOT NULL DEFAULT '/workspace',
                tier INTEGER NOT NULL DEFAULT 2,
                touches_core_bridge INTEGER NOT NULL DEFAULT 0,
                origin_channel_id INTEGER NOT NULL,
                origin_thread_id INTEGER,
                origin_conv_id TEXT,
                forge_message_id INTEGER,
                fact_sheet_json TEXT NOT NULL DEFAULT '[]',
                shared_context_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS forge_chunks (
                id TEXT PRIMARY KEY,
                build_id TEXT NOT NULL REFERENCES forge_builds(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                repo TEXT NOT NULL DEFAULT '',
                worker_model TEXT NOT NULL DEFAULT 'gemini-3.8-flash-high',
                target_files_json TEXT NOT NULL DEFAULT '[]',
                target_symbols_json TEXT NOT NULL DEFAULT '[]',
                shared_context_json TEXT NOT NULL DEFAULT '[]',
                prohibited_files_json TEXT NOT NULL DEFAULT '[]',
                instructions TEXT NOT NULL,
                verification_cmd TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                pr_number INTEGER,
                branch_name TEXT,
                diff_stat TEXT NOT NULL DEFAULT '',
                live_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                started_at REAL,
                completed_at REAL,
                UNIQUE(build_id, chunk_index)
            );

            CREATE TABLE IF NOT EXISTS forge_decisions (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                handler_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                is_private INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                resolved_at REAL,
                receipt_text TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS forge_jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL DEFAULT '/workspace',
                channel TEXT NOT NULL DEFAULT 'zero-chat',
                origin_conv_id TEXT,
                timeout INTEGER NOT NULL DEFAULT 3600,
                status TEXT NOT NULL DEFAULT 'starting',
                worker_pid INTEGER,
                child_pid INTEGER,
                exit_code INTEGER,
                log_file TEXT NOT NULL DEFAULT '',
                forge_message_id INTEGER,
                notify_on_success INTEGER NOT NULL DEFAULT 1,
                synthesize INTEGER NOT NULL DEFAULT 1,
                start_time REAL NOT NULL,
                end_time REAL,
                duration_seconds REAL
            );

            CREATE INDEX IF NOT EXISTS idx_forge_builds_status ON forge_builds(status);
            CREATE INDEX IF NOT EXISTS idx_forge_chunks_build_status ON forge_chunks(build_id, status);
            CREATE INDEX IF NOT EXISTS idx_forge_decisions_status ON forge_decisions(status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_forge_jobs_status ON forge_jobs(status);
            """
        )
        # Idempotent v1.1 migrations for existing forge.db files
        _ensure_column(conn, "forge_builds", "objective", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "forge_builds", "shared_context_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "forge_chunks", "repo", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "forge_chunks", "target_symbols_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "forge_chunks", "shared_context_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "forge_chunks", "live_status", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "forge_chunks", "depends_on_json", "TEXT NOT NULL DEFAULT '[]'")


# Initialize schema on import
init_forge_db()


def consolidate_legacy_forge_db(
    primary_db_path: Path | str = FORGE_DB_PATH,
    legacy_db_path: Path | str = DATA_DIR / "forge.db",
) -> dict:
    """Migrate missing rows from legacy_db into primary_db and replace legacy_db with a symlink."""
    primary = Path(primary_db_path)
    legacy = Path(legacy_db_path)

    if not primary.exists() or not legacy.exists():
        return {"migrated_rows": {}, "symlinked": False, "status": "missing_db"}

    if legacy.is_symlink():
        return {"migrated_rows": {}, "symlinked": False, "status": "already_symlink"}

    try:
        if primary.resolve() == legacy.resolve():
            return {"migrated_rows": {}, "symlinked": False, "status": "same_file"}
    except Exception:
        return {"migrated_rows": {}, "symlinked": False, "status": "resolve_error"}

    tables = ("forge_builds", "forge_chunks", "forge_jobs", "forge_decisions")
    init_forge_db(primary)

    migrated_rows: dict[str, int] = {}
    legacy_conn = sqlite3.connect(str(legacy), timeout=10.0)
    primary_conn = sqlite3.connect(str(primary), timeout=10.0)
    try:
        primary_conn.execute("PRAGMA foreign_keys=OFF;")
        legacy_tables = {
            r[0] for r in legacy_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        primary_tables = {
            r[0] for r in primary_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        for table in tables:
            if table not in legacy_tables or table not in primary_tables:
                continue
            leg_cols = [r[1] for r in legacy_conn.execute(f"PRAGMA table_info({table})").fetchall()]
            pri_cols = [r[1] for r in primary_conn.execute(f"PRAGMA table_info({table})").fetchall()]
            common = [c for c in leg_cols if c in pri_cols]
            if not common:
                continue
            col_str = ", ".join(f'"{c}"' for c in common)
            placeholders = ", ".join("?" for _ in common)
            rows = legacy_conn.execute(f"SELECT {col_str} FROM {table}").fetchall()
            if rows:
                cur = primary_conn.executemany(
                    f"INSERT OR IGNORE INTO {table} ({col_str}) VALUES ({placeholders})",
                    rows,
                )
                migrated_rows[table] = cur.rowcount
            else:
                migrated_rows[table] = 0

        primary_conn.commit()
        try:
            legacy_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
    finally:
        legacy_conn.close()
        primary_conn.close()

    try:
        legacy.unlink()
    except FileNotFoundError:
        pass
    for sidecar in (Path(f"{legacy}-wal"), Path(f"{legacy}-shm")):
        try:
            sidecar.unlink(missing_ok=True)
        except Exception:
            pass

    legacy.symlink_to(primary.resolve())
    return {
        "migrated_rows": migrated_rows,
        "symlinked": True,
        "primary": str(primary.resolve()),
        "legacy": str(legacy),
    }


def _now_pt_str(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).astimezone(PT_TZ)
    return dt.strftime("%Y-%m-%d %I:%M:%S %p PT")


def _format_duration(seconds: float) -> str:
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    rem_secs = secs % 60
    if mins < 60:
        return f"{mins}m {rem_secs}s"
    hours = mins // 60
    rem_mins = mins % 60
    return f"{hours}h {rem_mins}m {rem_secs}s"


def _is_pid_running(pid: Optional[int]) -> bool:
    if not pid or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ---------------------------------------------------------------------------
# 2. Pillar 1: Activity-Renewed Lease & 4-Signal Heartbeat Engine
# ---------------------------------------------------------------------------

class HeartbeatTracker:
    """Tracks disk mtime/size across SDK tasks and subagent transcripts for an active turn."""

    def __init__(self, conv_id: Optional[str] = None, brain_dir: Path | str = DEFAULT_BRAIN_DIR):
        self.conv_id = conv_id
        self.brain_dir = Path(brain_dir)
        self.last_disk_signature: tuple[int, float, int] = (0, 0.0, 0)
        self.last_progress_time: float = time.time()

    def compute_disk_signature(self, conv_id: Optional[str] = None) -> tuple[int, float, int]:
        """Compute (file_count, max_mtime, total_bytes) across SDK task logs, messages, and subagent transcripts."""
        cid = conv_id or self.conv_id
        if not cid:
            return (0, 0.0, 0)

        file_count = 0
        max_mtime = 0.0
        total_bytes = 0

        conv_path = self.brain_dir / str(cid) / ".system_generated"
        for subdir, pattern in (("tasks", "*.log"), ("messages", "*.json"), ("logs", "*.jsonl")):
            target_dir = conv_path / subdir
            if not target_dir.exists():
                continue
            for entry in target_dir.glob(pattern):
                try:
                    st = entry.stat()
                    file_count += 1
                    if st.st_mtime > max_mtime:
                        max_mtime = st.st_mtime
                    total_bytes += st.st_size
                except OSError:
                    continue

        # Also inspect recently active subagent transcripts modified within the last 10 minutes
        now = time.time()
        if self.brain_dir.exists():
            try:
                for sub_entry in self.brain_dir.iterdir():
                    if not sub_entry.is_dir() or sub_entry.name == str(cid):
                        continue
                    sub_log = sub_entry / ".system_generated" / "logs" / "transcript.jsonl"
                    if sub_log.exists():
                        st = sub_log.stat()
                        if (now - st.st_mtime) <= 600.0:
                            file_count += 1
                            if st.st_mtime > max_mtime:
                                max_mtime = st.st_mtime
                            total_bytes += st.st_size
            except OSError:
                pass

        return (file_count, max_mtime, total_bytes)

    def poll_disk_progress(self, conv_id: Optional[str] = None) -> bool:
        """Return True if SDK task logs, message files, or subagent transcripts grew since last check."""
        sig = self.compute_disk_signature(conv_id=conv_id)
        if sig != (0, 0.0, 0) and sig != self.last_disk_signature:
            prev_files, prev_mtime, prev_bytes = self.last_disk_signature
            self.last_disk_signature = sig
            # First initialization baselines the tracker without claiming a fake delta unless recent (<15s)
            if prev_files == 0 and (time.time() - sig[1]) > 15.0:
                return False
            self.last_progress_time = time.time()
            return True
        return False


def check_runaway_tool_loop(
    conv_id: Optional[str],
    brain_dir: Path | str = DEFAULT_BRAIN_DIR,
    threshold: int = 3,
) -> tuple[bool, str]:
    """Detect if the active turn has executed `threshold` consecutive identical failing tool calls."""
    if not conv_id:
        return False, ""
    tpath = Path(brain_dir) / str(conv_id) / ".system_generated" / "logs" / "transcript.jsonl"
    if not tpath.exists():
        return False, ""

    try:
        with open(tpath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-40:]
    except Exception:
        return False, ""

    tool_signatures: list[str] = []
    error_count = 0

    for line in reversed(lines):
        line_s = line.strip()
        if not line_s:
            continue
        try:
            d = json.loads(line_s)
        except Exception:
            continue
        stype = d.get("type", "")
        source = d.get("source", "")
        if stype in ("USER_INPUT", "CHECKPOINT") or source in ("USER_EXPLICIT", "USER"):
            break
        if stype == "PLANNER_RESPONSE" and d.get("tool_calls"):
            calls = d.get("tool_calls", [])
            sig = json.dumps(calls, sort_keys=True)
            tool_signatures.append(sig)
            if len(tool_signatures) >= threshold:
                break
        elif stype in ("GENERIC", "tool"):
            content = str(d.get("content", ""))
            if "Command exited with code" in content and "Command exited with code 0" not in content:
                error_count += 1
            elif "Traceback (most recent call last)" in content or "Error:" in content:
                error_count += 1

    if len(tool_signatures) >= threshold and len(set(tool_signatures[:threshold])) == 1 and error_count >= threshold:
        return True, f"Runaway tool loop detected ({threshold} identical failing tool calls)"
    return False, ""


# ---------------------------------------------------------------------------
# 3. Pillar 2: FORGE Build Queue, Symbol Pre-Hydration & Spec Validation
# ---------------------------------------------------------------------------

def extract_symbol_snippet(
    file_path: Path | str,
    symbol_name: str,
    max_lines: int = 90,
) -> str:
    """Extract a line-numbered AST or regex snippet for `symbol_name` from `file_path`."""
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return ""
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    lines = src.splitlines()
    if not lines:
        return ""

    # 1. Try Python AST lookup for exact class/function/method boundaries
    if p.suffix == ".py":
        try:
            tree = ast.parse(src)
            target_part = symbol_name.split(".")[-1]
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == symbol_name or node.name == target_part:
                        start_idx = max(0, (getattr(node, "lineno", 1) or 1) - 1)
                        end_lineno = getattr(node, "end_lineno", start_idx + 35) or (start_idx + 35)
                        end_idx = min(len(lines), start_idx + max_lines, end_lineno)
                        numbered = [f"{i + 1:4d}: {lines[i]}" for i in range(start_idx, end_idx)]
                        if end_lineno > end_idx:
                            numbered.append(f"      ... ({end_lineno - end_idx} more lines in {node.name})")
                        return "\n".join(numbered)
        except Exception:
            pass

    # 2. Fallback to regex/substring search with surrounding context
    pattern = re.compile(rf"\b{re.escape(symbol_name.split('.')[-1])}\b")
    for idx, line in enumerate(lines):
        if pattern.search(line):
            start_idx = max(0, idx - 3)
            end_idx = min(len(lines), idx + min(max_lines, 35))
            numbered = [f"{i + 1:4d}: {lines[i]}" for i in range(start_idx, end_idx)]
            return "\n".join(numbered)

    return ""


def _normalize_file_list(raw: Any) -> list[str]:
    """Normalize target_files / prohibited_files into a deduplicated list of path strings."""
    if not raw:
        return []
    items = [raw] if isinstance(raw, (str, dict)) else (list(raw) if isinstance(raw, (list, tuple, set)) else [])
    out: list[str] = []
    for item in items:
        val = ""
        if isinstance(item, str):
            val = item.strip()
        elif isinstance(item, dict):
            val = str(item.get("file") or item.get("path") or item.get("target_file") or item.get("name") or "").strip()
        while val.startswith("./"):
            val = val[2:]
        if val == ".":
            val = ""
        if val and val not in out:
            out.append(val)
    return out


def _normalize_target_symbols(raw: Any) -> list[str]:
    """Normalize target_symbols (strings, {'file': ..., 'symbol': ...} dicts, or file->symbol maps) into ['file:symbol', ...] strings."""
    if not raw:
        return []

    out: list[str] = []

    def _add(val: str) -> None:
        v = val.strip()
        if v and v not in out:
            out.append(v)

    if isinstance(raw, str):
        _add(raw)
        return out

    if isinstance(raw, dict):
        known_descriptor_keys = {
            "file", "path", "target_file", "module",
            "symbol", "name", "function", "class", "method",
            "symbols", "names", "functions",
        }
        if any(k in raw for k in known_descriptor_keys):
            f_val = str(raw.get("file") or raw.get("path") or raw.get("target_file") or raw.get("module") or "").strip()
            multi = raw.get("symbols") or raw.get("names") or raw.get("functions")
            if isinstance(multi, (list, tuple, set)):
                for s in multi:
                    s_str = str(s or "").strip()
                    if s_str:
                        _add(f"{f_val}:{s_str}" if f_val and ":" not in s_str else s_str)
            else:
                s_val = str(
                    raw.get("symbol") or raw.get("name") or raw.get("function") or raw.get("class") or raw.get("method") or ""
                ).strip()
                if s_val:
                    _add(f"{f_val}:{s_val}" if f_val and ":" not in s_val else s_val)
        else:
            # Map of {"file.py": "sym"} or {"file.py": ["sym1", "sym2"]}
            for k, v in raw.items():
                f_key = str(k or "").strip()
                if isinstance(v, (list, tuple, set)):
                    for s in v:
                        s_str = str(s or "").strip()
                        if s_str:
                            _add(f"{f_key}:{s_str}" if f_key and ":" not in s_str else s_str)
                elif isinstance(v, str) and v.strip():
                    s_str = v.strip()
                    _add(f"{f_key}:{s_str}" if f_key and ":" not in s_str else s_str)
        return out

    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            for norm_s in _normalize_target_symbols(item):
                _add(norm_s)
        return out

    _add(str(raw))
    return out


def _normalize_shared_context(raw: Any) -> list[str]:
    """Normalize shared_context or context_fact_sheet (strings, lists of dicts, or dicts) into a list of strings."""
    if not raw:
        return []

    out: list[str] = []

    def _add(val: str) -> None:
        v = val.strip()
        if v and v not in out:
            out.append(v)

    if isinstance(raw, str):
        _add(raw)
        return out

    if isinstance(raw, dict):
        descriptor_keys = {"file", "path", "symbol", "title", "note", "description", "content", "snippet", "summary", "rule"}
        if any(k in raw for k in descriptor_keys):
            head = str(raw.get("file") or raw.get("path") or raw.get("title") or raw.get("symbol") or "").strip()
            body = str(
                raw.get("note")
                or raw.get("description")
                or raw.get("content")
                or raw.get("snippet")
                or raw.get("summary")
                or raw.get("rule")
                or ""
            ).strip()
            if head and body:
                _add(f"{head}: {body}")
            elif body:
                _add(body)
            elif head:
                _add(head)
            else:
                _add(json.dumps(raw, sort_keys=True))
        else:
            for k, v in raw.items():
                k_str = str(k or "").strip()
                v_str = str(v or "").strip() if not isinstance(v, (dict, list)) else json.dumps(v)
                if k_str and v_str:
                    _add(f"{k_str}: {v_str}")
                elif v_str:
                    _add(v_str)
        return out

    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            for norm_c in _normalize_shared_context(item):
                _add(norm_c)
        return out

    _add(str(raw))
    return out


def _normalize_repo_target_file(repo_dir: Path | str, tf: Any) -> str:
    """Normalize a target file path relative to `repo_dir` so relative and absolute notations always collide."""
    raw = str(tf or "").strip().replace("\\", "/")
    if not raw:
        return ""
    repo_norm = os.path.normpath(str(Path(str(repo_dir or "/workspace").strip()).resolve())).replace("\\", "/")
    repo_rel = repo_norm.lstrip("/")
    if raw.startswith("/"):
        cand_norm = os.path.normpath(raw).replace("\\", "/")
        if cand_norm == repo_norm:
            return ""
        if cand_norm.startswith(f"{repo_norm}/"):
            return cand_norm[len(repo_norm) + 1 :]
        return cand_norm.lstrip("/")
    rel = os.path.normpath(raw).replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    if rel == ".":
        return ""
    if repo_rel and rel.startswith(f"{repo_rel}/"):
        rel = rel[len(repo_rel) + 1 :]
    return rel


def check_target_file_overlap(
    plan: dict,
    db_path: Path | str = FORGE_DB_PATH,
) -> list[dict]:
    """Detect if any target_files in `plan` overlap with currently active (queued/running) FORGE chunks."""
    default_repo = str(Path(str(plan.get("repo") or "/workspace").strip()).resolve())
    planned_pairs: set[tuple[str, str]] = set()
    for ch in plan.get("chunks", []):
        ch_repo = str(Path(str(ch.get("repo") or default_repo).strip()).resolve())
        for tf in _normalize_file_list(ch.get("target_files", [])):
            norm_tf = _normalize_repo_target_file(ch_repo, tf)
            if norm_tf:
                planned_pairs.add((ch_repo, norm_tf))

    if not planned_pairs:
        return []

    overlaps: list[dict] = []
    with get_db_connection(db_path) as conn:
        active_rows = conn.execute(
            """
            SELECT b.id AS build_id, b.title AS build_title, b.repo AS build_repo,
                   c.id AS chunk_id, c.chunk_index, c.title AS chunk_title,
                   c.repo AS chunk_repo, c.target_files_json, c.status AS chunk_status
            FROM forge_chunks c
            JOIN forge_builds b ON c.build_id = b.id
            WHERE b.status IN ('queued', 'running', 'awaiting_approval')
              AND c.status IN ('pending', 'running', 'verifying', 'awaiting_approval')
            """
        ).fetchall()

    for r in active_rows:
        active_repo = str(Path(str(r["chunk_repo"] or r["build_repo"] or "/workspace").strip()).resolve())
        t_files = json.loads(r["target_files_json"] or "[]")
        for tf in t_files:
            norm_tf = _normalize_repo_target_file(active_repo, tf)
            if norm_tf and (active_repo, norm_tf) in planned_pairs:
                overlaps.append({
                    "repo": active_repo,
                    "file": norm_tf,
                    "build_id": r["build_id"],
                    "build_title": r["build_title"],
                    "chunk_id": r["chunk_id"],
                    "chunk_title": r["chunk_title"],
                })
    return overlaps


def validate_forge_plan(plan: dict) -> tuple[bool, str]:
    """Deterministically validate a ForgePlan JSON dictionary before enqueueing."""
    if not isinstance(plan, dict):
        return False, "Plan must be a JSON object."

    title = str(plan.get("title") or "").strip()
    if not title:
        return False, "Plan is missing required non-empty 'title'."

    origin_cid = plan.get("origin_channel_id")
    if not origin_cid or not str(origin_cid).isdigit():
        return False, "Plan is missing valid numeric 'origin_channel_id'."

    repo_str = str(plan.get("repo") or "/workspace").strip()
    repo_path = Path(repo_str).resolve()
    if not (repo_path / ".git").exists():
        return False, f"Target repo '{repo_str}' is not a valid git repository."

    fact_sheet = _normalize_shared_context(
        plan.get("context_fact_sheet")
        or plan.get("shared_context")
        or plan.get("facts")
        or plan.get("invariants")
        or ([plan["objective"]] if plan.get("objective") else [])
    )
    if not fact_sheet:
        return False, "Plan must include a non-empty 'context_fact_sheet' (or 'shared_context') list of architectural invariants."

    chunks = plan.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return False, "Plan must include a non-empty 'chunks' list."

    for idx, ch in enumerate(chunks):
        if not isinstance(ch, dict):
            return False, f"Chunk at index {idx} must be an object."
        ch_title = str(ch.get("title") or "").strip()
        if not ch_title:
            return False, f"Chunk {idx} is missing 'title'."
        ch_repo_str = str(ch.get("repo") or repo_str).strip()
        ch_repo_path = Path(ch_repo_str).resolve()
        if not (ch_repo_path / ".git").exists():
            return False, f"Chunk {idx} ('{ch_title}') repo '{ch_repo_str}' is not a valid git repository."
        t_files = _normalize_file_list(ch.get("target_files"))
        if not t_files:
            return False, f"Chunk {idx} ('{ch_title}') must specify a non-empty 'target_files' list."
        instr = str(ch.get("instructions") or "").strip()
        if len(instr) < 15:
            return False, f"Chunk {idx} ('{ch_title}') must include detailed TDD 'instructions'."
        # Note: verification_cmd is now optional (auto-falls back to deterministic baseline checks)

    return True, "Valid ForgePlan."


def build_default_verification_cmd(target_files: list[str], worktree_dir: Optional[Path | str] = None) -> str:
    """Synthesize a deterministic baseline verification command when `verification_cmd` is omitted."""
    py_files = [str(f).strip() for f in target_files if str(f).strip().endswith(".py")]
    if not py_files:
        return "true"

    cmds: list[str] = []
    quoted_py = " ".join(f'"{f}"' for f in py_files)
    cmds.append(f"python3 -m py_compile {quoted_py}")

    # Find matching or explicitly targeted test files
    test_files: list[str] = []
    for f in py_files:
        p = Path(f)
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            if f not in test_files:
                test_files.append(f)
        else:
            cand = p.parent / f"test_{p.name}"
            if worktree_dir and (Path(worktree_dir) / cand).exists():
                if str(cand) not in test_files:
                    test_files.append(str(cand))
            elif (WORKSPACE_DIR / cand).exists():
                if str(cand) not in test_files:
                    test_files.append(str(cand))

    if test_files:
        quoted_tests = " ".join(f'"{tf}"' for tf in test_files)
        cmds.append(f"pytest {quoted_tests}")

    return " && ".join(cmds)


def _detect_touches_core_bridge(plan: dict) -> bool:
    """Detect whether any chunk targets live tools/bridge*.py or ivy_daemon.py files."""
    if plan.get("touches_core_bridge") is not None:
        return bool(plan.get("touches_core_bridge"))
    for ch in plan.get("chunks", []):
        for tf in ch.get("target_files", []):
            norm = str(tf).replace("\\", "/")
            if ("bridge" in norm or "ivy_daemon" in norm) and norm.endswith(".py") and not Path(norm).name.startswith("test_"):
                return True
    return False


COMPLETED_WIDGET_TTL_SECONDS = 3600.0


def _compact_diff_stat(diff_stat: str) -> str:
    """Condense `1 file changed, 165 insertions(+), 609 deletions(-)` into `+165/-609`."""
    if not diff_stat:
        return ""
    ins = re.search(r"(\d+)\s+insertion", diff_stat)
    dels = re.search(r"(\d+)\s+deletion", diff_stat)
    if ins or dels:
        i_val = ins.group(1) if ins else "0"
        d_val = dels.group(1) if dels else "0"
        return f"+{i_val}/-{d_val}"
    return diff_stat.strip()[:28]


def extract_live_chunk_telemetry(line: str, step_count: int = 0) -> tuple[Optional[str], int]:
    """Parse a single `agy --output-format=stream-json` line into a concise live status string and updated step count.

    Returns (status_string_or_None, updated_step_count).
    """
    s = line.strip()
    if not s.startswith("{"):
        return None, step_count
    try:
        ev = json.loads(s)
    except Exception:
        return None, step_count
    if not isinstance(ev, dict):
        return None, step_count

    ev_name = ev.get("event") or ev.get("type")
    if ev_name != "step_update":
        return None, step_count

    step = ev.get("step_update")
    if not isinstance(step, dict):
        return None, step_count

    stype = step.get("step_type")
    sstate = step.get("state")
    tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")

    if (stype in ("tool", "system_message", "system") or tname) and sstate != "DONE":
        step_count += 1
        tinfo = step.get("tool_info") or {}
        params = tinfo.get("parameters") or {}
        action = f"Calling `{tname or 'tool'}`"
        if tname == "run_command":
            summary = str(params.get("toolSummary") or "").strip()
            cmd_line = str(params.get("CommandLine") or "").strip().splitlines()[0] if params.get("CommandLine") else ""
            if summary:
                action = f"Running: {summary[:55]}"
            elif cmd_line:
                action = f"Running `{cmd_line[:50]}`"
            else:
                action = "Running command..."
        elif tname in ("read_file", "view_file"):
            p_val = params.get("Path") or params.get("AbsolutePath") or ""
            fname = Path(str(p_val)).name if p_val else "file"
            action = f"Reading `{fname}`"
        elif tname in ("edit_file", "replace_file_content", "multi_replace_file_content"):
            p_val = params.get("Path") or params.get("TargetFile") or ""
            fname = Path(str(p_val)).name if p_val else "file"
            action = f"Editing `{fname}`"
        elif tname in ("write_file", "write_to_file"):
            p_val = params.get("Path") or params.get("TargetFile") or ""
            fname = Path(str(p_val)).name if p_val else "file"
            action = f"Writing `{fname}`"
        elif tname in ("grep_search", "find_by_name"):
            q_val = str(params.get("Query") or params.get("Pattern") or "").strip()
            action = f"Searching `{q_val[:40]}`" if q_val else f"Running `{tname}`"

        thought_raw = str(step.get("thinking") or step.get("thought") or "").strip()
        thought_snip = ""
        if thought_raw:
            last_line = [ln.strip() for ln in thought_raw.splitlines() if ln.strip()][-1]
            clean_t = re.sub(r"[*_`#]+", "", last_line).strip()
            if clean_t:
                thought_snip = f" — *\"{clean_t[:55]}\"*"
        return f"Step {step_count} · {action}{thought_snip}", step_count

    # Also capture standalone thinking / reasoning updates or final response drafting
    thought_delta = str(step.get("thinking_delta") or step.get("thinking") or step.get("thought") or "").strip()
    if thought_delta:
        lines_t = [ln.strip() for ln in thought_delta.splitlines() if ln.strip()]
        if lines_t:
            clean_t = re.sub(r"[*_`#]+", "", lines_t[-1]).strip()
            if len(clean_t) >= 8:
                prefix = f"Step {step_count} · " if step_count > 0 else ""
                return f"{prefix}Thinking — *\"{clean_t[:65]}\"*", step_count

    if stype == "agent_response" and sstate != "DONE":
        delta = str(step.get("text_delta") or step.get("text") or "").strip()
        if delta:
            prefix = f"Step {step_count} · " if step_count > 0 else ""
            return f"{prefix}Summarizing chunk changes...", step_count

    return None, step_count


def _format_channel_mention(channel_val: Any) -> str:
    """Format origin Discord channel as a clickable mention or readable label."""
    raw = str(channel_val).strip().lstrip("#")
    if raw.isdigit():
        return f"<#{raw}>"
    if raw == "baseball":
        return f"<#{BASEBALL_CHANNEL_ID}>"
    if raw == "zero-chat":
        return f"<#{ZERO_CHAT_CHANNEL_ID}>"
    if raw == "zero-ops":
        return f"<#{ZERO_OPS_CHANNEL_ID}>"
    return f"#{raw}" if raw else f"<#{ZERO_CHAT_CHANNEL_ID}>"


def render_build_widget_embed(build_id: str, db_path: Path | str = FORGE_DB_PATH) -> dict:
    """Render a compact native Discord Embed dict for a FORGE build in #forge."""
    with get_db_connection(db_path) as conn:
        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            return {"title": f"⚠️ Build {build_id} not found", "color": 0xEF4444}
        chunks = conn.execute(
            "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
            (build_id,),
        ).fetchall()

    status_meta = {
        "queued": ("⏳", "QUEUED", 0xF59E0B),
        "running": ("🔨", "RUNNING", 0xA855F7),
        "awaiting_approval": ("🛑", "AWAITING APPROVAL", 0xF59E0B),
        "completed": ("✅", "COMPLETED", 0x10B981),
        "failed": ("❌", "FAILED", 0xEF4444),
        "cancelled": ("⚠️", "CANCELLED", 0x6B7280),
    }
    chunk_badge_map = {
        "pending": "⏳",
        "running": "🔨",
        "verifying": "🧪",
        "awaiting_approval": "👀",
        "merged": "✅",
        "failed": "❌",
    }

    b_status = str(build["status"] or "").strip().lower()
    b_emoji, b_label, color = status_meta.get(b_status, ("⚙️", b_status.upper(), 0x3B82F6))
    origin_ref = f"<#{build['origin_thread_id'] or build['origin_channel_id']}>"
    repo_name = Path(build["repo"]).name or "Zero-Agent"
    if repo_name == "workspace":
        is_ivy_origin = int(build["origin_channel_id"]) == BASEBALL_CHANNEL_ID or os.getenv("DISCORD_CHANNEL_ID", "").strip() == str(BASEBALL_CHANNEL_ID)
        repo_name = "Ivy-Agent" if is_ivy_origin else "Zero-Agent"

    total = len(chunks)
    merged_count = sum(1 for c in chunks if str(c["status"] or "").strip().lower() == "merged")
    header_line = f"**{b_label}** · `{repo_name}` · {origin_ref} · **{merged_count}/{total}** chunks"

    display_chunks = list(chunks)
    chunk_lines: list[str] = []
    if len(display_chunks) > 8:
        merged_indices = [i for i, c in enumerate(display_chunks) if str(c["status"] or "").strip().lower() == "merged"]
        if len(merged_indices) > 3:
            hidden_count = len(merged_indices) - 2
            chunk_lines.append(f"✅ *+{hidden_count} earlier chunk(s) merged*")
            keep_set = set(merged_indices[-2:]) | {i for i, c in enumerate(display_chunks) if str(c["status"] or "").strip().lower() != "merged"}
            display_chunks = [c for i, c in enumerate(display_chunks) if i in keep_set]

    for c in display_chunks:
        c_status = str(c["status"] or "").strip().lower()
        c_emoji = chunk_badge_map.get(c_status, "•")
        idx_num = c["chunk_index"] + 1
        pr_suffix = f" (PR #{c['pr_number']})" if c["pr_number"] else ""
        compact_diff = _compact_diff_stat(c["diff_stat"])
        diff_suffix = f" (`{compact_diff}`)" if compact_diff else ""
        chunk_lines.append(f"{c_emoji} **{idx_num}.** {c['title']}{pr_suffix}{diff_suffix}")
        if c_status in ("running", "verifying"):
            live_st = (c["live_status"] if "live_status" in c.keys() and c["live_status"] else "").strip()
            elapsed_prefix = ""
            if c["started_at"]:
                elapsed_s = max(0, int(time.time() - float(c["started_at"])))
                elapsed_prefix = f"({_format_duration(elapsed_s)}) · "
            if c_status == "verifying" and not live_st.startswith("Running Maker-Checker"):
                v_cmd_short = (c["verification_cmd"] or "auto-baseline").strip()[:50]
                chunk_lines.append(f"  ↳ 🧪 {elapsed_prefix}Running Maker-Checker gate (`{v_cmd_short}`)...")
            elif live_st:
                chunk_lines.append(f"  ↳ ⚡ {elapsed_prefix}{live_st[:130]}")
        elif c_status == "failed" and c["last_error"]:
            err_snip = c["last_error"].strip().splitlines()[-1][:110]
            chunk_lines.append(f"  ⚠️ `{err_snip}`")

    desc = header_line + ("\n\n" + "\n".join(chunk_lines) if chunk_lines else "")
    footer_suffix = " · Auto-clears in 1h" if b_status in ("completed", "failed", "cancelled") else ""
    return {
        "title": f"{b_emoji} {build['title'][:220]}",
        "description": desc[:4000],
        "color": color,
        "footer": {
            "text": f"{build['id']} · Updated {_now_pt_str(build['updated_at'])}{footer_suffix}"
        },
    }


def render_build_widget_text(build_id: str, db_path: Path | str = FORGE_DB_PATH) -> str:
    """Render the CLI/text summary for a FORGE build."""
    with get_db_connection(db_path) as conn:
        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            return f"⚠️ Build `{build_id}` not found."
        chunks = conn.execute(
            "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
            (build_id,),
        ).fetchall()

    embed = render_build_widget_embed(build_id, db_path=db_path)
    total = len(chunks)
    merged_count = sum(1 for c in chunks if str(c["status"] or "").strip().lower() == "merged")
    models = sorted({c["worker_model"] for c in chunks if c["worker_model"]})
    obj_line = f"\n- **Objective:** {build['objective'].strip()}" if build["objective"] else ""
    return (
        f"### {embed['title']} (`{build['id']}`)\n"
        f"- {embed['description'].splitlines()[0]} | `{', '.join(models)}`\n"
        f"- **Progress:** {merged_count}/{total} chunk(s) merged{obj_line}\n\n"
        + "\n".join(embed["description"].splitlines()[2:])
        + f"\n\n*{embed['footer']['text']}*"
    )


def post_or_update_build_widget(
    build_id: str,
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
) -> Optional[int]:
    """Post or edit the single live Discord Embed card for `build_id` inside #forge via Vulcan's webhook or REST API v10."""
    embed = render_build_widget_embed(build_id, db_path=db_path)
    token = _get_discord_token(for_forge_channel=True)
    wh = _get_vulcan_webhook(db_path=db_path, forge_channel_id=forge_channel_id)
    if not token and not wh:
        return None

    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT forge_message_id FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return None
        msg_id = row["forge_message_id"]

    payload = {"content": "", "embeds": [embed]}
    try:
        if msg_id:
            ok_patch, _ = _patch_forge_channel_message(
                int(msg_id),
                payload=payload,
                forge_channel_id=forge_channel_id,
                token=token,
                db_path=db_path,
            )
            if ok_patch:
                return int(msg_id)

        new_msg_id = _post_forge_channel_message(
            payload=payload,
            forge_channel_id=forge_channel_id,
            token=token,
            db_path=db_path,
        )
        if new_msg_id:
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE forge_builds SET forge_message_id = ?, updated_at = ? WHERE id = ?",
                    (new_msg_id, time.time(), build_id),
                )
            return new_msg_id
    except Exception as e:
        print(f"[FORGE] Warning updating #forge widget for {build_id}: {e}")
    return None


def render_job_widget_embed(task_id: str, db_path: Path | str = FORGE_DB_PATH) -> dict:
    """Render a compact native Discord Embed dict for a detached background job in #forge."""
    meta = _read_job_meta(task_id, db_path=db_path)
    if not meta:
        return {"title": f"⚠️ Job {task_id} not found", "color": 0xEF4444}

    status = str(meta.get("status") or "starting").strip().lower()
    status_meta = {
        "starting": ("⏳", "STARTING", 0xF59E0B),
        "running": ("⚙️", "RUNNING", 0x3B82F6),
        "completed": ("✅", "COMPLETED", 0x10B981),
        "failed": ("❌", "FAILED", 0xEF4444),
        "timed_out": ("⏱️", "TIMED OUT", 0xEF4444),
        "cancelled": ("🛑", "CANCELLED", 0x6B7280),
    }
    b_emoji, b_label, color = status_meta.get(status, ("⚙️", status.upper(), 0x3B82F6))

    origin_ref = _format_channel_mention(meta.get("channel", "zero-chat"))
    cwd = meta.get("cwd", "/workspace")
    repo_name = Path(cwd).name or "Zero-Agent"
    if repo_name == "workspace":
        raw_ch = str(meta.get("channel", "")).strip().lstrip("#")
        is_ivy_origin = raw_ch in ("baseball", str(BASEBALL_CHANNEL_ID)) or os.getenv("DISCORD_CHANNEL_ID", "").strip() == str(BASEBALL_CHANNEL_ID)
        repo_name = "Ivy-Agent" if is_ivy_origin else "Zero-Agent"

    dur_val = meta.get("duration_seconds")
    if dur_val is not None:
        dur_str = _format_duration(dur_val)
    elif meta.get("start_time"):
        dur_str = _format_duration(time.time() - float(meta["start_time"]))
    else:
        dur_str = "0s"

    header_line = f"**{b_label}** · `{repo_name}` · {origin_ref} · ⏱️ {dur_str}"

    command = meta.get("command", "").strip()
    cmd_snippet = command[:180] + ("..." if len(command) > 180 else "")
    cmd_line = f"**Command:** `{cmd_snippet}`" if cmd_snippet else ""

    # Tail up to 4 non-banner lines from log_file, bounding per-line & total output length
    tail_lines = []
    log_file = meta.get("log_file")
    if log_file:
        lp = Path(log_file)
        if lp.exists():
            try:
                with open(lp, "r", encoding="utf-8", errors="replace") as lf:
                    for line in lf:
                        s = line.rstrip()
                        if (
                            s
                            and not s.startswith("--- [Detached Runner]")
                            and not s.startswith("[Detached Runner]")
                            and not s.startswith("--- Command:")
                            and not s.startswith("--- Target Channel:")
                        ):
                            tail_lines.append(s[:100] + ("..." if len(s) > 100 else ""))
            except Exception:
                pass
    tail_lines = tail_lines[-4:]

    desc_parts = [header_line]
    if cmd_line:
        desc_parts.append(cmd_line)
    if tail_lines:
        recent_out = "\n".join(tail_lines)
        if len(recent_out) > 280:
            recent_out = recent_out[:277] + "..."
        desc_parts.append("**Recent Output:**\n```text\n" + recent_out + "\n```")
    elif status in ("starting", "running"):
        desc_parts.append("*Initializing process output...*")

    desc = "\n\n".join(desc_parts)

    footer_time = meta.get("end_time") or meta.get("start_time") or time.time()
    footer_suffix = " · Auto-clears in 1h" if status in ("completed", "failed", "cancelled", "timed_out") else ""
    return {
        "title": f"{b_emoji} {meta.get('name', 'Background Task')[:220]}",
        "description": desc[:4000],
        "color": color,
        "footer": {
            "text": f"{task_id} · {_now_pt_str(footer_time)}{footer_suffix}"
        },
    }


def post_or_update_job_widget(
    task_id: str,
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
) -> Optional[int]:
    """Post or edit the single live Discord Embed card for `task_id` inside #forge via REST API v10.

    Serializes concurrent caller updates (e.g. `start_job` parent vs `_run_job_worker_locked` child)
    under an exclusive per-task `widget.lock` so a job never double-posts an orphaned `STARTING` card.
    """
    task_dir = _get_job_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    wlock_path = task_dir / "widget.lock"

    with open(wlock_path, "a+", encoding="utf-8") as wlock:
        try:
            fcntl.flock(wlock.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        try:
            meta = _read_job_meta(task_id, db_path=db_path)
            if not meta:
                return None

            embed = render_job_widget_embed(task_id, db_path=db_path)
            token = _get_discord_token(for_forge_channel=True)
            wh = _get_vulcan_webhook(db_path=db_path, forge_channel_id=forge_channel_id)
            if not token and not wh:
                return None

            msg_id = meta.get("forge_message_id")
            if not msg_id:
                try:
                    with get_db_connection(db_path) as conn:
                        row = conn.execute("SELECT forge_message_id FROM forge_jobs WHERE id = ?", (task_id,)).fetchone()
                        if row and row["forge_message_id"]:
                            msg_id = row["forge_message_id"]
                except Exception:
                    pass

            payload = {"content": "", "embeds": [embed]}
            if msg_id:
                ok_patch, patch_code = _patch_forge_channel_message(
                    int(msg_id),
                    payload=payload,
                    forge_channel_id=forge_channel_id,
                    token=token,
                    db_path=db_path,
                )
                if ok_patch:
                    return int(msg_id)
                if patch_code != 404:
                    return int(msg_id)

            new_msg_id = _post_forge_channel_message(
                payload=payload,
                forge_channel_id=forge_channel_id,
                token=token,
                db_path=db_path,
            )
            if new_msg_id:
                try:
                    with get_db_connection(db_path) as conn:
                        conn.execute(
                            "UPDATE forge_jobs SET forge_message_id = ? WHERE id = ?",
                            (new_msg_id, task_id),
                        )
                except Exception:
                    pass
                meta["forge_message_id"] = new_msg_id
                _write_job_meta(task_id, meta, db_path=db_path)
                return new_msg_id
        except Exception as e:
            print(f"[FORGE] Warning updating #forge widget for job {task_id}: {e}")
        finally:
            try:
                fcntl.flock(wlock.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    return None


_LAST_REMOTE_CHANNEL_SWEEP_TS: float = 0.0
REMOTE_CHANNEL_SWEEP_INTERVAL_SECONDS: float = 300.0
FAILED_JOB_WIDGET_TTL_SECONDS: float = 600.0
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled", "timed_out")
TERMINAL_BUILD_STATUSES = ("completed", "failed", "cancelled")


def prune_completed_job_widgets(
    ttl_seconds: float = COMPLETED_WIDGET_TTL_SECONDS,
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
    delete_remote: bool = True,
) -> list[str]:
    """Delete Discord embed cards in #forge for terminal background jobs (`completed`, `failed`, `cancelled`, `timed_out`) older than `ttl_seconds` (or 10m for failed jobs, or immediately if `cancelled` or superseded by a newer completed job in the same channel)."""
    now = time.time()
    cutoff = now - float(ttl_seconds)
    failed_cutoff = now - min(float(ttl_seconds), float(FAILED_JOB_WIDGET_TTL_SECONDS))
    pruned_ids: list[str] = []
    db_paths = [Path(db_path)] + _get_peer_forge_db_paths(db_path)

    token = _get_discord_token(for_forge_channel=True) if delete_remote else None
    headers = {"Authorization": f"Bot {token}"} if token else {}

    for target_db in db_paths:
        try:
            with get_db_connection(target_db) as conn:
                rows = conn.execute(
                    """
                    SELECT id, status, forge_message_id FROM forge_jobs
                    WHERE LOWER(status) IN ('completed', 'failed', 'cancelled', 'timed_out')
                      AND forge_message_id IS NOT NULL
                      AND (
                          LOWER(status) = 'cancelled'
                          OR COALESCE(end_time, start_time) <= ?
                          OR (LOWER(status) IN ('failed', 'timed_out') AND COALESCE(end_time, start_time) <= ?)
                          OR (
                              LOWER(status) IN ('failed', 'timed_out')
                              AND EXISTS (
                                  SELECT 1 FROM forge_jobs j2
                                  WHERE j2.channel = forge_jobs.channel
                                    AND LOWER(j2.status) = 'completed'
                                    AND j2.id != forge_jobs.id
                                    AND COALESCE(j2.end_time, j2.start_time) >= COALESCE(forge_jobs.end_time, forge_jobs.start_time)
                              )
                          )
                      )
                    """,
                    (cutoff, failed_cutoff),
                ).fetchall()
        except Exception:
            continue

        for r in rows:
            j_id = r["id"]
            msg_id = r["forge_message_id"]
            deleted_ok = not delete_remote
            if delete_remote and token and msg_id:
                try:
                    url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages/{msg_id}"
                    resp = requests.delete(url, headers=headers, timeout=10)
                    if resp.status_code in (200, 204, 404):
                        deleted_ok = True
                except Exception:
                    pass
            if deleted_ok:
                try:
                    with get_db_connection(target_db) as conn:
                        conn.execute(
                            "UPDATE forge_jobs SET forge_message_id = NULL WHERE id = ?",
                            (j_id,),
                        )
                except Exception:
                    pass
                meta_file = _get_job_dir(j_id) / "meta.json"
                if meta_file.exists():
                    try:
                        m = json.loads(meta_file.read_text(encoding="utf-8"))
                        m["forge_message_id"] = None
                        meta_file.write_text(json.dumps(m, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                if j_id not in pruned_ids:
                    pruned_ids.append(j_id)

    return pruned_ids


def _sweep_stale_forge_channel_embeds(
    cutoff: float,
    forge_channel_id: int,
    token: str,
    db_paths: list[Path],
) -> list[str]:
    """Channel-level sweep: delete terminal (`completed`, `failed`, `cancelled`, `timed_out`) #forge embed cards older than `cutoff`,
    immediately delete `cancelled` cards, and immediately delete duplicate or orphaned `STARTING` cards.
    """
    swept_ids: list[str] = []
    headers = {"Authorization": f"Bot {token}"}
    try:
        url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages?limit=50"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return swept_ids
        messages = resp.json()
        if not isinstance(messages, list):
            return swept_ids
    except Exception:
        return swept_ids

    now = time.time()
    seen_entity_ids: set[str] = set()

    for msg in messages:
        try:
            msg_id_raw = msg.get("id")
            if not msg_id_raw or int(msg_id_raw) == FORGE_HEADER_MESSAGE_ID:
                continue
            embeds = msg.get("embeds") or []
            if not embeds:
                continue
            emb = embeds[0]
            title = str(emb.get("title") or "")
            desc = str(emb.get("description") or "")
            footer_text = str((emb.get("footer") or {}).get("text") or "")
            id_match = re.search(r"(?:Build\s+ID:\s*|Job\s+ID:\s*|\b)((?:fg|dt)-[A-Za-z0-9_-]+)", footer_text)
            entity_id = id_match.group(1) if id_match else f"msg-{msg_id_raw}"

            ts_str = msg.get("edited_timestamp") or msg.get("timestamp")
            msg_ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp() if ts_str else 0.0

            is_duplicate_entity = False
            if id_match:
                if entity_id in seen_entity_ids:
                    is_duplicate_entity = True
                else:
                    seen_entity_ids.add(entity_id)

            is_cancelled = title.startswith("🛑") or desc.startswith("**CANCELLED**")
            is_failed_dt = (
                entity_id.startswith("dt-")
                and (title.startswith(("❌", "⏱️")) or desc.startswith(("**FAILED**", "**TIMED OUT**")))
            )
            is_terminal = (
                is_cancelled
                or title.startswith(("✅", "❌", "⏱️"))
                or desc.startswith(("**COMPLETED**", "**FAILED**", "**TIMED OUT**"))
            )

            db_ts = 0.0
            if id_match:
                found_in_db = False
                db_status = ""
                for target_db in db_paths:
                    try:
                        with get_db_connection(target_db) as conn:
                            if entity_id.startswith("fg-"):
                                row = conn.execute(
                                    "SELECT status, updated_at, created_at FROM forge_builds WHERE id = ?",
                                    (entity_id,),
                                ).fetchone()
                                if row:
                                    found_in_db = True
                                    db_status = str(row["status"] or "").strip().lower()
                                    db_ts = float(row["updated_at"] or row["created_at"] or 0.0)
                                    break
                            elif entity_id.startswith("dt-"):
                                row = conn.execute(
                                    "SELECT status, end_time, start_time FROM forge_jobs WHERE id = ?",
                                    (entity_id,),
                                ).fetchone()
                                if row:
                                    found_in_db = True
                                    db_status = str(row["status"] or "").strip().lower()
                                    db_ts = float(row["end_time"] or row["start_time"] or 0.0)
                                    break
                    except Exception:
                        continue

                if found_in_db:
                    if db_status == "cancelled":
                        is_cancelled = True
                    if entity_id.startswith("dt-") and db_status in ("failed", "timed_out"):
                        is_failed_dt = True
                    if db_status in ("completed", "failed", "cancelled", "timed_out"):
                        is_terminal = True
                elif msg_ts > 0 and (now - msg_ts) >= 7200.0:
                    is_terminal = True

            is_stuck_starting = (
                (title.startswith("⏳") or desc.startswith("**STARTING**"))
                and msg_ts > 0
                and (now - msg_ts) >= 120.0
            )
            failed_dt_cutoff = now - min(max(0.0, now - cutoff), float(FAILED_JOB_WIDGET_TTL_SECONDS))
            effective_ts = db_ts if (db_ts > 0 and (msg_ts <= 0 or db_ts < msg_ts)) else msg_ts

            should_delete = (
                is_duplicate_entity
                or is_cancelled
                or is_stuck_starting
                or (is_failed_dt and effective_ts > 0 and effective_ts <= failed_dt_cutoff)
                or (is_terminal and effective_ts > 0 and effective_ts <= cutoff)
            )
            if not should_delete:
                continue

            del_url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages/{msg_id_raw}"
            del_resp = requests.delete(del_url, headers=headers, timeout=10)
            if del_resp.status_code in (200, 204, 404):
                swept_ids.append(entity_id)
                if not is_duplicate_entity:
                    for target_db in db_paths:
                        try:
                            with get_db_connection(target_db) as conn:
                                conn.execute(
                                    "UPDATE forge_builds SET forge_message_id = NULL WHERE forge_message_id = ?",
                                    (int(msg_id_raw),),
                                )
                                conn.execute(
                                    "UPDATE forge_jobs SET forge_message_id = NULL WHERE forge_message_id = ?",
                                    (int(msg_id_raw),),
                                )
                        except Exception:
                            pass
        except Exception:
            continue

    return swept_ids


def prune_completed_build_widgets(
    ttl_seconds: float = COMPLETED_WIDGET_TTL_SECONDS,
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
    delete_remote: bool = True,
    force_channel_sweep: bool = False,
) -> list[str]:
    """Delete Discord embed cards in #forge for terminal builds and background jobs older than `ttl_seconds` (default 1h), or immediately when cancelled."""
    global _LAST_REMOTE_CHANNEL_SWEEP_TS
    now = time.time()
    cutoff = now - float(ttl_seconds)
    pruned_ids: list[str] = []
    db_paths = [Path(db_path)] + _get_peer_forge_db_paths(db_path)

    token = _get_discord_token(for_forge_channel=True) if delete_remote else None
    headers = {"Authorization": f"Bot {token}"} if token else {}

    for target_db in db_paths:
        try:
            with get_db_connection(target_db) as conn:
                rows = conn.execute(
                    """
                    SELECT id, status, forge_message_id FROM forge_builds
                    WHERE LOWER(status) IN ('completed', 'failed', 'cancelled')
                      AND forge_message_id IS NOT NULL
                      AND (LOWER(status) = 'cancelled' OR updated_at <= ?)
                    """,
                    (cutoff,),
                ).fetchall()
        except Exception:
            continue

        for r in rows:
            b_id = r["id"]
            msg_id = r["forge_message_id"]
            deleted_ok = not delete_remote
            if delete_remote and token and msg_id:
                try:
                    url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages/{msg_id}"
                    resp = requests.delete(url, headers=headers, timeout=10)
                    if resp.status_code in (200, 204, 404):
                        deleted_ok = True
                except Exception:
                    pass
            if deleted_ok:
                try:
                    with get_db_connection(target_db) as conn:
                        conn.execute(
                            "UPDATE forge_builds SET forge_message_id = NULL WHERE id = ?",
                            (b_id,),
                        )
                except Exception:
                    pass
                if b_id not in pruned_ids:
                    pruned_ids.append(b_id)

    pruned_job_ids = prune_completed_job_widgets(
        ttl_seconds=ttl_seconds,
        db_path=db_path,
        forge_channel_id=forge_channel_id,
        delete_remote=delete_remote,
    )
    all_pruned = pruned_ids + [jid for jid in pruned_job_ids if jid not in pruned_ids]

    if delete_remote and token and (
        force_channel_sweep or (now - _LAST_REMOTE_CHANNEL_SWEEP_TS) >= REMOTE_CHANNEL_SWEEP_INTERVAL_SECONDS
    ):
        _LAST_REMOTE_CHANNEL_SWEEP_TS = now
        swept = _sweep_stale_forge_channel_embeds(
            cutoff=cutoff,
            forge_channel_id=forge_channel_id,
            token=token,
            db_paths=db_paths,
        )
        for sid in swept:
            if sid not in all_pruned:
                all_pruned.append(sid)

    return all_pruned


def evaluate_plan_approval_gate(
    plan: dict,
    batch_state_path: Path | str = "/tmp/batch_guard_state.json",
) -> tuple[bool, list[str]]:
    """Evaluate whether a ForgePlan requires an interactive Approve | Edit | Dismiss Decision Card before execution.

    Thresholds:
    - Auto-Execute Immediately (Fast Tier 2): 1 chunk, <=3 target files, touches_core_bridge=False, no >90s calibration pass.
    - Require Approval (Stage Decision Card):
      1. Multi-chunk pipeline (>=2 chunks or tier >= 3).
      2. Wide blast radius (>=4 distinct target files across chunks) or touches_core_bridge=True.
      3. Explicit requires_approval=True or enqueued after a >90s calibration/analysis pass in the current turn.
    """
    if plan.get("approved") is True or plan.get("skip_approval") is True:
        return False, []

    reasons: list[str] = []
    if plan.get("requires_approval") is True:
        reasons.append("Explicit requires_approval flag set on plan")

    chunks = plan.get("chunks") or []
    tier = int(plan.get("tier") or (3 if len(chunks) > 1 else 2))
    if len(chunks) >= 2 or tier >= 3:
        reasons.append(f"Multi-chunk Tier 3 pipeline ({len(chunks)} chunks)")

    all_files: list[str] = []
    for ch in chunks:
        for tf in _normalize_file_list(ch.get("target_files", [])):
            if tf not in all_files:
                all_files.append(tf)
    if len(all_files) >= 4:
        reasons.append(f"Wide file blast radius ({len(all_files)} target files)")

    if _detect_touches_core_bridge(plan):
        reasons.append("Touches core bridge or hook infrastructure")

    try:
        bs_path = Path(batch_state_path)
        if bs_path.exists():
            st = json.loads(bs_path.read_text(encoding="utf-8"))
            now_ts = time.time()
            last_t = float(st.get("last_time", 0.0) or 0.0)
            start_t = float(st.get("turn_start_time", now_ts) or now_ts)
            total_scratch = int(st.get("total_scratch_runs", 0) or 0)
            if (now_ts - last_t) <= 900.0 and (now_ts - start_t) > 90.0 and total_scratch >= 2:
                reasons.append(
                    f"Post-calibration code changes ({total_scratch} calibration/scratch runs over {int(now_ts - start_t)}s)"
                )
    except Exception:
        pass

    return len(reasons) > 0, reasons


def stage_plan_approval_decision(
    plan: dict,
    reasons: list[str],
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    allow_overlap: bool = False,
) -> dict:
    """Validate a ForgePlan, persist it toforge_plans/, and stage an Approve | Edit | Dismiss Decision Card."""
    valid, reason = validate_forge_plan(plan)
    if not valid:
        raise ValueError(f"Invalid ForgePlan: {reason}")

    if not allow_overlap and not plan.get("allow_overlap"):
        overlaps = check_target_file_overlap(plan, db_path=db_path)
        if overlaps:
            ov = overlaps[0]
            raise ValueError(
                f"Target file overlap detected: '{ov['file']}' is already queued/running in build "
                f"'{ov['build_id']}' ({ov['build_title']} / {ov['chunk_title']}). "
                "Wait for the active build to complete or pass allow_overlap=True."
            )

    now = time.time()
    slug = re.sub(r"[^a-z0-9]+", "-", plan["title"].lower()).strip("-")[:24] or "build"
    plans_dir = Path(db_path).parent / "forge_plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_copy = dict(plan)
    plan_copy["approved"] = True
    if allow_overlap:
        plan_copy["allow_overlap"] = True
    plan_file_path = plans_dir / f"plan-{int(now)}-{slug}.json"
    plan_file_path.write_text(json.dumps(plan_copy, indent=2), encoding="utf-8")

    chunks = plan.get("chunks") or []
    all_files: list[str] = []
    chunk_bullets: list[str] = []
    for idx, ch in enumerate(chunks):
        t_files = _normalize_file_list(ch.get("target_files", []))
        for tf in t_files:
            if tf not in all_files:
                all_files.append(tf)
        files_str = ", ".join(f"`{Path(f).name}`" for f in t_files[:3])
        if len(t_files) > 3:
            files_str += f" (+{len(t_files) - 3} more)"
        chunk_bullets.append(f"  - **Chunk {idx + 1}:** {ch['title'].strip()} ({files_str})")

    objective = str(plan.get("objective") or plan["title"]).strip()
    summary_lines = [
        f"**Objective:** {objective}",
        f"**Gate Trigger:** {'; '.join(reasons)}",
        f"**Scope:** {len(chunks)} chunk(s) across {len(all_files)} target file(s):",
        *chunk_bullets[:6],
        "Tap **Approve** to enqueue and launch isolated `#forge` worktree workers.",
    ]
    summary_text = "\n".join(summary_lines)
    target_ch = int(plan.get("origin_thread_id") or plan["origin_channel_id"])

    dec = stage_decision(
        category="forge",
        title=f"FORGE Build Plan: {plan['title'].strip()[:85]}",
        summary=summary_text,
        handler_type="forge_enqueue",
        payload={
            "plan_file": str(plan_file_path),
            "title": plan["title"].strip(),
            "chunk_count": len(chunks),
            "allow_overlap": bool(allow_overlap or plan.get("allow_overlap")),
        },
        channel_id=target_ch,
        db_path=db_path,
        post_to_discord=post_to_discord,
    )
    return {
        "ok": True,
        "status": "awaiting_approval",
        "requires_approval": True,
        "approval_reasons": reasons,
        "decision_id": dec["decision_id"],
        "plan_file": str(plan_file_path),
        "chunk_count": len(chunks),
        "origin_channel_id": target_ch,
        "discord_message_id": dec.get("discord_message_id"),
        "turn_action": "end_turn",
    }


def enqueue_build(
    plan: dict,
    db_path: Path | str = FORGE_DB_PATH,
    post_widget: bool = True,
    allow_overlap: bool = False,
    check_approval_gate: bool = False,
    batch_state_path: Path | str = "/tmp/batch_guard_state.json",
) -> dict:
    """Validate and enqueue a multi-chunk FORGE build plan with pre-enqueue target-file overlap deduplication and complexity approval gate."""
    valid, reason = validate_forge_plan(plan)
    if not valid:
        raise ValueError(f"Invalid ForgePlan: {reason}")

    if check_approval_gate:
        needs_approval, gate_reasons = evaluate_plan_approval_gate(plan, batch_state_path=batch_state_path)
        if needs_approval:
            return stage_plan_approval_decision(
                plan,
                reasons=gate_reasons,
                db_path=db_path,
                post_to_discord=post_widget,
                allow_overlap=allow_overlap,
            )

    if not allow_overlap and not plan.get("allow_overlap"):
        overlaps = check_target_file_overlap(plan, db_path=db_path)
        if overlaps:
            ov = overlaps[0]
            raise ValueError(
                f"Target file overlap detected: '{ov['file']}' is already queued/running in build "
                f"'{ov['build_id']}' ({ov['build_title']} / {ov['chunk_title']}). "
                "Wait for the active build to complete or pass allow_overlap=True."
            )

    now = time.time()
    slug = re.sub(r"[^a-z0-9]+", "-", plan["title"].lower()).strip("-")[:18] or "build"
    build_id = plan.get("id") or f"fg-{int(now)}-{slug}"
    touches_core = 1 if _detect_touches_core_bridge(plan) else 0
    default_repo = str(plan.get("repo") or "/workspace").strip()
    objective = str(plan.get("objective") or plan["title"]).strip()
    build_shared_ctx = _normalize_shared_context(plan.get("shared_context"))
    norm_fact_sheet = _normalize_shared_context(
        plan.get("context_fact_sheet")
        or plan.get("shared_context")
        or plan.get("facts")
        or plan.get("invariants")
        or ([objective] if objective else [])
    )

    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (
                id, title, objective, repo, tier, touches_core_bridge,
                origin_channel_id, origin_thread_id, origin_conv_id,
                fact_sheet_json, shared_context_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                build_id,
                plan["title"].strip(),
                objective,
                default_repo,
                int(plan.get("tier") or (3 if len(plan["chunks"]) > 1 else 2)),
                touches_core,
                int(plan["origin_channel_id"]),
                int(plan["origin_thread_id"]) if plan.get("origin_thread_id") else None,
                plan.get("origin_conv_id"),
                json.dumps(norm_fact_sheet),
                json.dumps(build_shared_ctx),
                now,
                now,
            ),
        )

        for idx, ch in enumerate(plan["chunks"]):
            chunk_id = f"{build_id}-c{idx}"
            ch_repo = str(ch.get("repo") or default_repo).strip()
            t_files = [
                nf
                for nf in (_normalize_repo_target_file(ch_repo, f) for f in _normalize_file_list(ch.get("target_files", [])))
                if nf
            ]
            t_symbols = _normalize_target_symbols(ch.get("target_symbols"))
            ch_shared_ctx = _normalize_shared_context(ch.get("shared_context"))
            p_files = [
                nf
                for nf in (_normalize_repo_target_file(ch_repo, f) for f in _normalize_file_list(ch.get("prohibited_files", [])))
                if nf
            ]
            v_cmd = str(ch.get("verification_cmd") or "").strip()
            raw_deps = ch.get("depends_on") or []
            norm_deps = [int(d) for d in raw_deps if str(d).isdigit() and 0 <= int(d) < len(plan["chunks"])] if isinstance(raw_deps, (list, tuple, set)) else []
            conn.execute(
                """
                INSERT INTO forge_chunks (
                    id, build_id, chunk_index, title, repo, worker_model,
                    target_files_json, target_symbols_json, shared_context_json,
                    prohibited_files_json, depends_on_json, instructions, verification_cmd, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    chunk_id,
                    build_id,
                    idx,
                    ch["title"].strip(),
                    ch_repo,
                    ch.get("worker_model") or DEFAULT_WORKER_MODEL,
                    json.dumps(t_files),
                    json.dumps(t_symbols),
                    json.dumps(ch_shared_ctx),
                    json.dumps(p_files),
                    json.dumps(norm_deps),
                    ch["instructions"].strip(),
                    v_cmd,
                ),
            )

    msg_id = None
    if post_widget:
        msg_id = post_or_update_build_widget(build_id, db_path=db_path)
        sync_forge_channel_header(db_path=db_path)

    # Immediately trigger background worker if enqueued against production forge.db
    if Path(db_path).resolve() == FORGE_DB_PATH.resolve():
        try:
            forge_supervisor.maybe_spawn_next_build_worker()
        except Exception:
            pass

    return {
        "ok": True,
        "build_id": build_id,
        "chunk_count": len(plan["chunks"]),
        "forge_channel_id": FORGE_CHANNEL_ID,
        "forge_message_id": msg_id,
        "turn_action": "end_turn",
    }


def format_chunk_prompt(
    build_id: str,
    chunk_index: int,
    db_path: Path | str = FORGE_DB_PATH,
    worktree_dir: Optional[str] = None,
) -> str:
    """Synthesize the bounded, high-signal prompt for a FORGE chunk worker with Turn-0 symbol hydration."""
    with get_db_connection(db_path) as conn:
        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            raise ValueError(f"Build {build_id} not found")
        chunks = conn.execute(
            "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
            (build_id,),
        ).fetchall()

    target_chunk = next((c for c in chunks if c["chunk_index"] == chunk_index), None)
    if not target_chunk:
        raise ValueError(f"Chunk index {chunk_index} not found in build {build_id}")

    prior_chunks = [c for c in chunks if c["chunk_index"] < chunk_index]
    upcoming_chunks = [c for c in chunks if c["chunk_index"] > chunk_index]
    fact_sheet = _normalize_shared_context(json.loads(build["fact_sheet_json"] or "[]"))
    build_shared_ctx = _normalize_shared_context(
        json.loads(build["shared_context_json"] or "[]") if "shared_context_json" in build.keys() else []
    )
    target_files = _normalize_file_list(json.loads(target_chunk["target_files_json"] or "[]"))
    target_symbols = _normalize_target_symbols(
        json.loads(target_chunk["target_symbols_json"] or "[]") if "target_symbols_json" in target_chunk.keys() else []
    )
    chunk_shared_ctx = _normalize_shared_context(
        json.loads(target_chunk["shared_context_json"] or "[]") if "shared_context_json" in target_chunk.keys() else []
    )
    prohibited_files = _normalize_file_list(json.loads(target_chunk["prohibited_files_json"] or "[]"))

    effective_repo = (target_chunk["repo"] if "repo" in target_chunk.keys() and target_chunk["repo"] else build["repo"]) or "/workspace"
    wt_str = worktree_dir or effective_repo
    wt_path = Path(wt_str)
    objective_text = (build["objective"] if "objective" in build.keys() and build["objective"] else build["title"]).strip()
    effective_v_cmd = (target_chunk["verification_cmd"] or "").strip() or build_default_verification_cmd(target_files, wt_path)

    lines = [
        f"# [FORGE WORKER EXECUTION CONTRACT — BUILD `{build_id}` / CHUNK {chunk_index + 1} OF {len(chunks)}]",
        f"**Build Title:** {build['title']}",
        f"**Overall Build Objective (Why):** {objective_text}",
        f"**Target Repository:** `{effective_repo}`",
        f"**Isolated Working Directory:** `{wt_str}`",
        "",
        "## 1. Architectural Context Fact Sheet (From Lead Orchestrator)",
    ]
    for fact in fact_sheet:
        lines.append(f"- {fact}")

    combined_shared = list(build_shared_ctx) + [x for x in chunk_shared_ctx if x not in build_shared_ctx]
    if combined_shared:
        lines.append("\n### Shared Context & Code Exemplars:")
        for snippet in combined_shared:
            lines.append(f"- {snippet}")

    lines.append("\n## 2. Completed Chunks Ledger (Already Landed)")
    if not prior_chunks:
        lines.append("- *(Chunk 1 of build — no prior chunks)*")
    else:
        for pc in prior_chunks:
            pr_str = f"PR #{pc['pr_number']}" if pc["pr_number"] else "staged"
            diff_str = f" (`{pc['diff_stat']}`)" if pc["diff_stat"] else ""
            lines.append(f"- ✅ **Chunk {pc['chunk_index'] + 1} ({pc['title']}):** {pr_str}{diff_str}")

    lines.append(f"\n## 3. Active Chunk Specification (Chunk {chunk_index + 1}: {target_chunk['title']})")
    lines.append(f"- **Allowed Target Files:** `{', '.join(target_files)}`")
    if target_symbols:
        lines.append(f"- **Target Symbols:** `{', '.join(target_symbols)}`")
    if prohibited_files:
        lines.append(f"- **Prohibited Files (DO NOT MODIFY):** `{', '.join(prohibited_files)}`")
    lines.append(f"- **Mandatory Verification Gate:** `{effective_v_cmd}`")

    # Pre-hydrate target_symbols AST/line-slice snippets so Flash workers skip discovery reads
    if target_symbols:
        hydrated_blocks: list[str] = []
        search_roots = [wt_path]
        if Path(effective_repo).resolve() != wt_path.resolve():
            search_roots.append(Path(effective_repo))
        for sym in target_symbols:
            sym_str = str(sym).strip()
            if not sym_str:
                continue
            # Support optional "path/to/file.py:symbol_name" or search across target_files
            if ":" in sym_str and not sym_str.startswith(":"):
                f_hint, s_name = sym_str.split(":", 1)
                cand_files = [f_hint.strip()]
            else:
                s_name = sym_str
                cand_files = list(target_files)
            for rel_f in cand_files:
                snippet = ""
                for root in search_roots:
                    cand_p = (root / rel_f).resolve()
                    if cand_p.exists():
                        snippet = extract_symbol_snippet(cand_p, s_name)
                        if snippet:
                            hydrated_blocks.append(f"#### `{rel_f}` — `{s_name}`\n```python\n{snippet}\n```")
                            break
                if snippet:
                    break
        if hydrated_blocks:
            lines.append("\n### Pre-Hydrated Target Symbol Slices (Turn-0 Fast-Path Context):")
            lines.extend(hydrated_blocks)

    lines.append("\n### Implementation Instructions:")
    lines.append(target_chunk["instructions"])

    if target_chunk["last_error"]:
        lines.append("\n### ⚠️ Previous Attempt Failure Traceback (Fix This Root Cause):")
        lines.append(f"```text\n{target_chunk['last_error'][-1500:]}\n```")

    lines.append("\n## 4. Remaining Chunks Boundary Guard")
    if not upcoming_chunks:
        lines.append("- *(This is the final chunk of the build.)*")
    else:
        lines.append("DO NOT implement the following upcoming chunks in this turn (listed strictly for boundary awareness):")
        for uc in upcoming_chunks:
            lines.append(f"- ⏳ Chunk {uc['chunk_index'] + 1}: {uc['title']}")

    lines.append(
        "\n## 5. Mandatory Execution Discipline\n"
        f"1. Operate strictly inside `{wt_str}`.\n"
        "2. Implement ONLY the Active Chunk Specification and its unit tests.\n"
        f"3. Run `{effective_v_cmd}` until it exits 0.\n"
        "4. Output a concise summary of changes and stop immediately."
    )
    return "\n".join(lines)


def verify_chunk_worktree(
    worktree_dir: Path | str,
    verification_cmd: Optional[str] = None,
    prohibited_files: Optional[list[str]] = None,
    target_files: Optional[list[str]] = None,
    require_changes: bool = False,
) -> tuple[bool, str, str]:
    """Run deterministic Maker-Checker verification inside the chunk worktree.

    If `verification_cmd` is empty or omitted, automatically runs baseline py_compile +
    matching unit tests on `target_files` (or modified files in git status).
    Returns (passed, diff_stat_summary, error_output).
    """
    wt = Path(worktree_dir)
    if not wt.exists():
        return False, "", f"Worktree directory does not exist: {wt}"

    st_proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(wt),
        capture_output=True,
        text=True,
        timeout=15,
    )
    changed = [line[3:].strip().strip('"') for line in st_proc.stdout.splitlines() if len(line) > 3]

    if require_changes and not changed:
        return False, "", "Worker produced zero file changes in worktree (git status --porcelain is empty)."

    # 1. Check prohibited files were not modified
    if prohibited_files:
        violated = [f for f in changed if f in set(prohibited_files)]
        if violated:
            return False, "", f"Boundary violation: chunk modified prohibited file(s): {', '.join(violated)}"

    # 2. Execute verification_cmd (or synthesized baseline gate if omitted)
    effective_cmd = (verification_cmd or "").strip()
    if not effective_cmd:
        files_to_check = list(target_files) if target_files else changed
        effective_cmd = build_default_verification_cmd(files_to_check, worktree_dir=wt)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{wt}:{env.get('PYTHONPATH', '')}".strip(":")
    v_proc = subprocess.Popen(
        effective_cmd,
        shell=True,
        cwd=str(wt),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=_apply_low_priority if hasattr(os, "nice") else None,
    )
    try:
        v_stdout, v_stderr = v_proc.communicate(timeout=VERIFY_CMD_TIMEOUT_SECONDS)
        v_ret = v_proc.returncode
    except subprocess.TimeoutExpired:
        _kill_process_group(v_proc)
        try:
            v_stdout, v_stderr = v_proc.communicate(timeout=5)
        except Exception:
            v_stdout, v_stderr = "", ""
        combined = f"{v_stdout}\n{v_stderr}".strip()
        return (
            False,
            "",
            f"Verification command timed out after {int(VERIFY_CMD_TIMEOUT_SECONDS)}s:\n{combined[-1500:]}",
        )
    if v_ret != 0:
        combined = f"{v_stdout}\n{v_stderr}".strip()
        return False, "", f"Verification command failed (exit {v_ret}):\n{combined[-1500:]}"

    # 3. Run commit safety scanner if present
    safety_script = wt / "tools" / "validate_commit_safety.py"
    if not safety_script.exists():
        safety_script = WORKSPACE_DIR / "tools" / "validate_commit_safety.py"
    if safety_script.exists():
        s_proc = subprocess.run(
            [sys.executable, str(safety_script), str(wt)],
            cwd=str(wt),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if s_proc.returncode != 0:
            combined = f"{s_proc.stdout}\n{s_proc.stderr}".strip()
            return False, "", f"Commit safety scan failed:\n{combined[-1000:]}"

    # 4. Collect short diff stat
    diff_proc = subprocess.run(
        ["git", "diff", "--shortstat"],
        cwd=str(wt),
        capture_output=True,
        text=True,
        timeout=10,
    )
    diff_stat = diff_proc.stdout.strip() or "verified clean"
    return True, diff_stat, ""


def format_origin_completion_envelope(build_id: str, db_path: Path | str = FORGE_DB_PATH) -> str:
    """Format the structured FORGE Completion Envelope sent to the originating channel Orchestrator."""
    with get_db_connection(db_path) as conn:
        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            return ""
        chunks = conn.execute(
            "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
            (build_id,),
        ).fetchall()

    fact_sheet = json.loads(build["fact_sheet_json"] or "[]")
    lines = [
        f"[FORGE BUILD COMPLETION ENVELOPE — `{build['id']}`]",
        f"**Build Title:** {build['title']}",
    ]
    if "objective" in build.keys() and build["objective"] and build["objective"].strip() != build["title"].strip():
        lines.append(f"**Objective:** {build['objective'].strip()}")
    lines.extend([
        f"**Repository:** `{build['repo']}`",
        f"**Status:** All {len(chunks)} chunk(s) passed deterministic Maker-Checker verification and merged.",
        "",
        "### Merged Chunks Ledger:",
    ])
    for c in chunks:
        pr_tag = f"PR #{c['pr_number']}" if c["pr_number"] else "Merged"
        diff_tag = f" ({c['diff_stat']})" if c["diff_stat"] else ""
        v_label = c["verification_cmd"] or "auto-baseline"
        lines.append(f"- **Chunk {c['chunk_index'] + 1}: {c['title']}** — {pr_tag}{diff_tag} [verified via `{v_label}`]")

    if fact_sheet:
        lines.append("\n### Original Context Fact Sheet:")
        for f in fact_sheet:
            lines.append(f"- {f}")

    reload_note = (
        "\nThis build modified core bridge files (`touches_core_bridge=true`). A FORGE Decision Card (`bridge_reload`) has been posted to this channel so Ryan can approve an in-place reload with one click."
        if build["touches_core_bridge"]
        else ""
    )
    lines.append(
        "\nEvaluate the completed build ledger above against the original user request. "
        "Deliver a concise final completion summary directly to Ryan in this channel focusing on what changed for him (do NOT recite commit SHAs, test pass counts, or internal verification gates unless asked)."
        + reload_note
    )
    return "\n".join(lines)


def format_origin_failure_envelope(build_id: str, db_path: Path | str = FORGE_DB_PATH) -> str:
    """Format the structured FORGE Failure Envelope sent to the originating channel Orchestrator when a build 3-strikes."""
    with get_db_connection(db_path) as conn:
        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            return ""
        chunks = conn.execute(
            "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
            (build_id,),
        ).fetchall()

    failed_chunk = next((c for c in chunks if c["status"] == "failed"), None)
    lines = [
        f"[FORGE BUILD FAILURE ENVELOPE — `{build['id']}`]",
        f"**Build Title:** {build['title']}",
        f"**Repository:** `{build['repo']}`",
        "**Status:** ❌ Build halted after exhausting 3 Maker-Checker attempts (including Argon Medium escalation).",
    ]
    if failed_chunk:
        lines.extend([
            f"**Failed Chunk:** Chunk {failed_chunk['chunk_index'] + 1} of {len(chunks)} — **{failed_chunk['title']}**",
            f"**Verification Gate:** `{failed_chunk['verification_cmd'] or 'auto-baseline'}`",
        ])
        if failed_chunk["last_error"]:
            lines.append(f"\n### Final Verification Error Traceback:\n```text\n{failed_chunk['last_error'][-1200:]}\n```")

    lines.append(
        "\nInspect the failure traceback above, diagnose why the chunk failed Maker-Checker verification, "
        "and either enqueue a corrected `ForgePlan` or report the concrete blocker concisely to Ryan in this channel."
    )
    return "\n".join(lines)


def dispatch_build_failure_to_origin(build_id: str, db_path: Path | str = FORGE_DB_PATH) -> bool:
    """Wake the Argon Orchestrator in the originating channel/thread when a FORGE build fails after 3 attempts."""
    with get_db_connection(db_path) as conn:
        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            return False

    envelope = format_origin_failure_envelope(build_id, db_path=db_path)
    target_ch = build["origin_thread_id"] or build["origin_channel_id"]
    try:
        from tools.outbox import queue_outbox_message
        queue_outbox_message(
            channel=int(target_ch),
            content=envelope,
            source_turn=f"forge-failed-{build_id}",
            msg_type="agent_turn",
            force=True,
        )
        return True
    except Exception as e:
        print(f"[FORGE] Failed dispatching build failure to origin {target_ch}: {e}")
        return False


def dispatch_build_completion_to_origin(build_id: str, db_path: Path | str = FORGE_DB_PATH) -> bool:
    """Wake the Argon Orchestrator in the originating channel/thread when all chunks complete."""
    with get_db_connection(db_path) as conn:
        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            return False

    envelope = format_origin_completion_envelope(build_id, db_path=db_path)
    target_ch = build["origin_thread_id"] or build["origin_channel_id"]
    if build["touches_core_bridge"]:
        try:
            stage_decision(
                category="bridge_reload",
                title=f"Reload Bridge After Build {build_id}",
                summary=f"FORGE build `{build_id}` (**{build['title']}**) modified core bridge files (`touches_core_bridge=true`). Approve to trigger an in-place bridge reload.",
                handler_type="bridge_reload",
                payload={"build_id": build_id, "reason": f"FORGE build {build_id} ({build['title']})"},
                channel_id=int(target_ch),
                db_path=db_path,
                post_to_discord=True,
            )
        except Exception as de:
            print(f"[FORGE] Warning staging bridge_reload decision card for {build_id}: {de}")
    try:
        from tools.outbox import queue_outbox_message
        queue_outbox_message(
            channel=int(target_ch),
            content=envelope,
            source_turn=f"forge-complete-{build_id}",
            msg_type="agent_turn",
            force=True,
        )
        return True
    except Exception as e:
        print(f"[FORGE] Failed dispatching build completion to origin {target_ch}: {e}")
        return False


def _get_active_build_worker_ids(db_path: Path | str = FORGE_DB_PATH) -> set[str]:
    """Return all build_ids across local and peer data dirs whose per-build worker flock is currently held."""
    active_ids: set[str] = set()
    db_dirs = {Path(db_path).parent} | {p.parent for p in _get_peer_forge_db_paths(db_path)}
    for ddir in db_dirs:
        if not ddir.exists():
            continue
        try:
            for p in ddir.glob(".forge_build_worker_*.lock"):
                name = p.name
                for pref in (".forge_build_worker_ivy_", ".forge_build_worker_"):
                    if name.startswith(pref) and name.endswith(".lock"):
                        bid = name[len(pref):-len(".lock")]
                        if bid and _is_build_worker_lock_held(p):
                            active_ids.add(bid)
                        break
        except Exception:
            pass
    return active_ids


def reconcile_forge_on_boot(db_path: Path | str = FORGE_DB_PATH) -> dict:
    """Clean up stale /tmp/forge-wt-* worktrees and reset interrupted running chunks on startup, preserving active worker worktrees."""
    if _is_prod_forge_env(db_path):
        try:
            consolidate_legacy_forge_db(primary_db_path=db_path)
        except Exception as e:
            print(f"[FORGE] consolidate_legacy_forge_db error: {e}", file=sys.stderr)

    active_build_ids = _get_active_build_worker_ids(db_path)
    cleaned_wts = []
    for wt_path in glob.glob("/tmp/forge-wt-*"):
        wt_base = os.path.basename(wt_path)
        if any(wt_base.startswith(f"forge-wt-{bid}-") for bid in active_build_ids):
            continue
        try:
            shutil.rmtree(wt_path, ignore_errors=True)
            cleaned_wts.append(wt_path)
        except Exception:
            pass

    for repo_cand in (
        WORKSPACE_DIR,
        WORKSPACE_DIR / "market-sandbox",
        WORKSPACE_DIR / "kalshi-quant",
        WORKSPACE_DIR / "agora-roguelike",
        WORKSPACE_DIR / "sharkfin",
        Path("/baseball"),
        Path("/baseball/big-projections"),
    ):
        if (repo_cand / ".git").exists():
            subprocess.run(["git", "worktree", "prune"], cwd=str(repo_cand), capture_output=True, timeout=10)

    reset_chunks = 0
    pruned_jobs = 0
    with get_db_connection(db_path) as conn:
        running_builds = conn.execute(
            "SELECT id FROM forge_builds WHERE status IN ('running', 'queued')"
        ).fetchall()
        stale_build_ids = [r["id"] for r in running_builds if r["id"] not in active_build_ids]
        if stale_build_ids:
            placeholders = ",".join("?" for _ in stale_build_ids)
            cur = conn.execute(
                f"UPDATE forge_chunks SET status = 'pending' WHERE status IN ('running', 'verifying') AND attempt_count < 3 AND build_id IN ({placeholders})",
                tuple(stale_build_ids),
            )
            reset_chunks = cur.rowcount
            conn.execute(
                f"UPDATE forge_builds SET status = 'queued', updated_at = ? WHERE status = 'running' AND id IN ({placeholders})",
                (time.time(), *stale_build_ids),
            )
        elif not active_build_ids:
            cur = conn.execute(
                "UPDATE forge_chunks SET status = 'pending' WHERE status IN ('running', 'verifying') AND attempt_count < 3"
            )
            reset_chunks = cur.rowcount
            conn.execute(
                "UPDATE forge_builds SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (time.time(),),
            )
        # Reconcile stale/dead forge_jobs and remove leaked /tmp test fixtures
        del_cur = conn.execute("DELETE FROM forge_jobs WHERE log_file LIKE '/tmp/%' OR id LIKE 'dt-test-%'")
        pruned_jobs += del_cur.rowcount
        active_jobs = conn.execute(
            "SELECT id, status, channel, worker_pid, child_pid, start_time FROM forge_jobs WHERE status IN ('starting', 'running')"
        ).fetchall()
        now_ts = time.time()
        in_ivy = is_ivy_container()
        for j in active_jobs:
            j_ch = str(j["channel"] or "").strip().lstrip("#")
            job_belongs_to_ivy = j_ch in ("baseball", str(BASEBALL_CHANNEL_ID))
            if job_belongs_to_ivy != in_ivy:
                # PID namespaces are container-local; do not reap the peer container's jobs
                continue
            wpid = j["worker_pid"]
            cpid = j["child_pid"]
            alive = _is_pid_running(cpid) or _is_pid_running(wpid)
            if not alive and (now_ts - float(j["start_time"] or 0)) > 15.0:
                conn.execute(
                    "UPDATE forge_jobs SET status = 'unknown_exit', end_time = ? WHERE id = ?",
                    (now_ts, j["id"]),
                )
                pruned_jobs += 1

    return {"cleaned_worktrees": cleaned_wts, "reset_chunks": reset_chunks, "pruned_jobs": pruned_jobs}


MAX_PARALLEL_BUILDS_PER_AGENT = 3
MAX_PARALLEL_CHUNKS_PER_BUILD = 3
CHUNK_WORKER_TIMEOUT_SECONDS = 1200.0
CHUNK_SILENT_STALL_TIMEOUT_SECONDS = 300.0
VERIFY_CMD_TIMEOUT_SECONDS = 300.0


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Force-terminate a subprocess and its entire process group so orphaned grandchildren cannot hold pipes open."""
    try:
        if hasattr(os, "killpg") and proc.pid and proc.pid > 1:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def _resolve_latest_branch_ref(repo_dir: Path | str, default_branch: str) -> str:
    """Resolve the newest starting ref between local `<default_branch>` and `origin/<default_branch>` after fetch."""
    rp = Path(repo_dir)
    try:
        subprocess.run(["git", "fetch", "origin", default_branch], cwd=str(rp), capture_output=True, timeout=30)
    except Exception:
        pass
    chk_origin = subprocess.run(
        ["git", "rev-parse", "--verify", f"origin/{default_branch}"],
        cwd=str(rp),
        capture_output=True,
    )
    chk_local = subprocess.run(
        ["git", "rev-parse", "--verify", default_branch],
        cwd=str(rp),
        capture_output=True,
    )
    has_origin = chk_origin.returncode == 0
    has_local = chk_local.returncode == 0
    if has_origin and has_local:
        # If local default_branch contains origin/default_branch (i.e. local is equal or ahead), use local default_branch
        anc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"origin/{default_branch}", default_branch],
            cwd=str(rp),
            capture_output=True,
        )
        if anc.returncode == 0:
            return default_branch
        return f"origin/{default_branch}"
    if has_origin:
        return f"origin/{default_branch}"
    if has_local:
        return default_branch
    return "HEAD"


def _get_build_target_pairs(
    conn: sqlite3.Connection,
    build_id: str,
) -> set[tuple[str, str]]:
    """Return the set of `(resolved_repo_path, normalized_target_file)` across all chunks of `build_id`."""
    b_row = conn.execute("SELECT repo FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
    default_repo = str(Path(str((b_row["repo"] if b_row else None) or "/workspace").strip()).resolve())
    c_rows = conn.execute(
        "SELECT repo, target_files_json FROM forge_chunks WHERE build_id = ?",
        (build_id,),
    ).fetchall()
    pairs: set[tuple[str, str]] = set()
    for r in c_rows:
        ch_repo = str(Path(str(r["repo"] or default_repo).strip()).resolve())
        for tf in _normalize_file_list(json.loads(r["target_files_json"] or "[]")):
            norm_tf = _normalize_repo_target_file(ch_repo, tf)
            if norm_tf:
                pairs.add((ch_repo, norm_tf))
    return pairs


def _build_overlaps_earlier_builds(
    cand_build_id: str,
    earlier_build_ids: set[str] | list[str],
    db_path: Path | str = FORGE_DB_PATH,
) -> bool:
    """Return True if `cand_build_id` shares any `(repo, target_file)` with any build in `earlier_build_ids`."""
    other_ids = [str(bid) for bid in earlier_build_ids if str(bid) and str(bid) != str(cand_build_id)]
    if not other_ids:
        return False
    with get_db_connection(db_path) as conn:
        cand_pairs = _get_build_target_pairs(conn, cand_build_id)
        if not cand_pairs:
            return False
        for obid in other_ids:
            if cand_pairs & _get_build_target_pairs(conn, obid):
                return True
    return False


def _acquire_repo_git_lock(repo_dir: Path | str):
    """Acquire an exclusive file lock for git ref/branch/worktree mutations on `repo_dir`."""
    rp = Path(repo_dir).resolve()
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", str(rp)).strip("_")
    lock_path = DATA_DIR / f".forge_git_repo_{slug}.lock"
    fh = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def _release_file_lock(fh) -> None:
    if not fh:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


def _is_build_worker_lock_held(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    try:
        with open(lock_path, "a+", encoding="utf-8") as lf:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            else:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                return False
    except Exception:
        return False


def _chunks_are_eligible_in_parallel(
    cand_chunk: sqlite3.Row,
    all_chunks: list[sqlite3.Row],
    running_chunks: list[sqlite3.Row],
) -> bool:
    """Return True if `cand_chunk` has all explicit dependencies merged and shares zero target files with unmerged earlier or currently running chunks."""
    cand_idx = int(cand_chunk["chunk_index"])
    deps_raw = cand_chunk["depends_on_json"] if "depends_on_json" in cand_chunk.keys() else "[]"
    try:
        explicit_deps = {int(x) for x in json.loads(deps_raw or "[]")}
    except Exception:
        explicit_deps = set()

    merged_indices = {int(c["chunk_index"]) for c in all_chunks if c["status"] == "merged"}
    if not explicit_deps.issubset(merged_indices):
        return False

    cand_repo = str(Path(str(cand_chunk["repo"] or "/workspace").strip()).resolve())
    cand_files = {
        nf
        for nf in (_normalize_repo_target_file(cand_repo, f) for f in json.loads(cand_chunk["target_files_json"] or "[]"))
        if nf
    }

    # Implicit dependency: if an earlier unmerged chunk touches any of the same files in the same repo, wait for it
    for c in all_chunks:
        c_idx = int(c["chunk_index"])
        if c_idx >= cand_idx or c["status"] == "merged":
            continue
        c_repo = str(Path(str(c["repo"] or "/workspace").strip()).resolve())
        if c_repo != cand_repo:
            continue
        c_files = {
            nf
            for nf in (_normalize_repo_target_file(c_repo, f) for f in json.loads(c["target_files_json"] or "[]"))
            if nf
        }
        if cand_files & c_files:
            return False

    # Also ensure zero overlap with any currently running chunk in this build
    for rc in running_chunks:
        rc_repo = str(Path(str(rc["repo"] or "/workspace").strip()).resolve())
        if rc_repo != cand_repo:
            continue
        rc_files = {
            nf
            for nf in (_normalize_repo_target_file(rc_repo, f) for f in json.loads(rc["target_files_json"] or "[]"))
            if nf
        }
        if cand_files & rc_files:
            return False

    return True


class ForgeSupervisor:
    """Asynchronous background supervisor for FORGE build chunks (up to 3 parallel builds x 3 parallel chunks) and Decision Card expiry."""

    def __init__(
        self,
        db_path: Path | str = FORGE_DB_PATH,
        max_parallel_builds: int = MAX_PARALLEL_BUILDS_PER_AGENT,
        max_parallel_chunks: int = MAX_PARALLEL_CHUNKS_PER_BUILD,
    ):
        self.db_path = Path(db_path)
        self.max_parallel_builds = max_parallel_builds
        self.max_parallel_chunks = max_parallel_chunks
        self._active_build_procs: dict[str, subprocess.Popen] = {}
        self._state_lock = threading.Lock()

    def _build_lock_path(self, build_id: str) -> Path:
        in_ivy = is_ivy_container()
        prefix = ".forge_build_worker_ivy_" if in_ivy else ".forge_build_worker_"
        return self.db_path.parent / f"{prefix}{build_id}.lock"

    def _count_active_build_workers(self) -> set[str]:
        """Return the set of build_ids currently holding an active per-build worker flock for this container."""
        in_ivy = is_ivy_container()
        prefix = ".forge_build_worker_ivy_" if in_ivy else ".forge_build_worker_"
        active_ids: set[str] = set()
        for bid, proc in list(self._active_build_procs.items()):
            if proc.poll() is None:
                active_ids.add(bid)
            else:
                self._active_build_procs.pop(bid, None)
        try:
            for p in self.db_path.parent.glob(f"{prefix}*.lock"):
                bid = p.name[len(prefix):-len(".lock")]
                if _is_build_worker_lock_held(p):
                    active_ids.add(bid)
        except Exception:
            pass
        return active_ids

    def tick_sync(self) -> dict:
        """Synchronous housekeeping tick: expire stale decisions, prune 1h completed #forge cards, sync header, dispatch SDK tasks, and advance queued builds."""
        expired = expire_stale_decisions(db_path=self.db_path)
        pruned_widgets = prune_completed_build_widgets(db_path=self.db_path)
        sync_forge_channel_header(db_path=self.db_path)
        dispatched_sdk = []
        try:
            from tools.task_settle import check_and_dispatch_completed_tasks
            dispatched_sdk = check_and_dispatch_completed_tasks()
        except Exception:
            pass

        spawned_builds: list[str] = []
        while len(self._count_active_build_workers()) < self.max_parallel_builds:
            b_id = self.maybe_spawn_next_build_worker()
            if not b_id:
                break
            spawned_builds.append(b_id)

        return {
            "expired_decisions": expired,
            "pruned_widgets": pruned_widgets,
            "dispatched_sdk_tasks": len(dispatched_sdk),
            "spawned_build": spawned_builds[0] if spawned_builds else None,
            "spawned_builds": spawned_builds,
        }

    def maybe_spawn_next_build_worker(self) -> Optional[str]:
        """If a build for this container is queued/running in SQLite and active build workers < max_parallel_builds, spawn a detached worker."""
        active_ids = self._count_active_build_workers()
        if len(active_ids) >= self.max_parallel_builds:
            return None

        in_ivy = is_ivy_container()
        with get_db_connection(self.db_path) as conn:
            if in_ivy:
                rows = conn.execute(
                    "SELECT id FROM forge_builds WHERE status IN ('queued', 'running') AND origin_channel_id = ? ORDER BY created_at ASC, rowid ASC",
                    (BASEBALL_CHANNEL_ID,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM forge_builds WHERE status IN ('queued', 'running') AND origin_channel_id != ? ORDER BY created_at ASC, rowid ASC",
                    (BASEBALL_CHANNEL_ID,),
                ).fetchall()

        target_build_id: Optional[str] = None
        earlier_incomplete_ids: set[str] = set(active_ids)
        for r in rows:
            cand_id = r["id"]
            if cand_id in active_ids or _is_build_worker_lock_held(self._build_lock_path(cand_id)):
                earlier_incomplete_ids.add(cand_id)
                continue
            if _build_overlaps_earlier_builds(cand_id, earlier_incomplete_ids, db_path=self.db_path):
                # Hold overlapping follow-up build in queued state until earlier build finishes and merges
                earlier_incomplete_ids.add(cand_id)
                continue
            target_build_id = cand_id
            break

        if not target_build_id:
            return None

        log_path = FORGE_LOGS_DIR / f"build-{target_build_id}.log"
        lf_out = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_build_worker", target_build_id, "--db-path", str(self.db_path)],
            cwd=str(WORKSPACE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=lf_out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._active_build_procs[target_build_id] = proc
        return target_build_id

    def run_build_pipeline(self, build_id: str) -> bool:
        """Execute pending chunks for `build_id` with up to `max_parallel_chunks` concurrent Vulcan worktree workers."""
        lock_path = self._build_lock_path(build_id)
        lock_fh = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            lock_fh.close()
            return False

        try:
            with get_db_connection(self.db_path) as conn:
                build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
                if not build or build["status"] in ("cancelled", "failed", "completed"):
                    return False
                chunks = conn.execute(
                    "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
                    (build_id,),
                ).fetchall()

            # Initialize release branch for each distinct repo in the build if no chunks have merged yet
            any_merged = any(c["status"] == "merged" for c in chunks)
            if not any_merged:
                repos_seen: set[Path] = set()
                for c in chunks:
                    eff_r = Path((c["repo"] if "repo" in c.keys() and c["repo"] else build["repo"]) or "/workspace").resolve()
                    repos_seen.add(eff_r)
                for r_dir in repos_seen:
                    git_lk = _acquire_repo_git_lock(r_dir)
                    try:
                        def_br = _get_repo_default_branch(r_dir)
                        staging_ref = f"forge/release-{build_id}"
                        start_ref = _resolve_latest_branch_ref(r_dir, def_br)
                        subprocess.run(["git", "branch", "-f", staging_ref, start_ref], cwd=str(r_dir), capture_output=True, timeout=10)
                    finally:
                        _release_file_lock(git_lk)

            active_futures: dict[Any, sqlite3.Row] = {}
            failed_pipeline = False

            with ThreadPoolExecutor(max_workers=self.max_parallel_chunks) as pool:
                while True:
                    with get_db_connection(self.db_path) as conn:
                        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
                        if not build or build["status"] in ("cancelled", "failed", "completed"):
                            failed_pipeline = build is None or build["status"] != "completed"
                            break
                        all_chunks = conn.execute(
                            "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
                            (build_id,),
                        ).fetchall()

                    unmerged = [c for c in all_chunks if c["status"] != "merged"]
                    if not unmerged and not active_futures:
                        with get_db_connection(self.db_path) as conn:
                            conn.execute(
                                "UPDATE forge_builds SET status = 'completed', updated_at = ? WHERE id = ?",
                                (time.time(), build_id),
                            )
                        post_or_update_build_widget(build_id, db_path=self.db_path)
                        dispatch_build_completion_to_origin(build_id, db_path=self.db_path)
                        return True

                    in_flight_ids = {r["id"] for r in active_futures.values()}
                    running_rows = [c for c in all_chunks if c["id"] in in_flight_ids]

                    for cand in all_chunks:
                        if len(active_futures) >= self.max_parallel_chunks:
                            break
                        if cand["status"] != "pending" or cand["id"] in in_flight_ids:
                            continue
                        if _chunks_are_eligible_in_parallel(cand, all_chunks, running_rows):
                            fut = pool.submit(self.execute_chunk_in_worktree, build_id, cand)
                            active_futures[fut] = cand
                            in_flight_ids.add(cand["id"])
                            running_rows.append(cand)

                    if not active_futures:
                        # Fallback if remaining pending chunks exist but none matched parallel eligibility
                        fallback_cand = next((c for c in all_chunks if c["status"] == "pending"), None)
                        if fallback_cand:
                            fut = pool.submit(self.execute_chunk_in_worktree, build_id, fallback_cand)
                            active_futures[fut] = fallback_cand
                        else:
                            break

                    done, _ = wait(list(active_futures.keys()), return_when=FIRST_COMPLETED)
                    for fut in done:
                        active_futures.pop(fut, None)
                        try:
                            ok = fut.result()
                        except Exception:
                            ok = False
                        if not ok:
                            failed_pipeline = True

                    if failed_pipeline:
                        # Wait for any remaining in-flight chunks to finish cleanly before exiting
                        for rem_fut in list(active_futures.keys()):
                            try:
                                rem_fut.result()
                            except Exception:
                                pass
                        return False

            return not failed_pipeline
        finally:
            _release_file_lock(lock_fh)
            if _is_prod_forge_env(self.db_path):
                try:
                    self.maybe_spawn_next_build_worker()
                except Exception:
                    pass

    def execute_chunk_in_worktree(self, build_id: str, chunk_row: sqlite3.Row) -> bool:
        """Execute a single FORGE chunk inside an isolated git worktree with Maker-Checker verification."""
        chunk_id = chunk_row["id"]
        chunk_idx = int(chunk_row["chunk_index"])
        with self._state_lock, get_db_connection(self.db_path) as conn:
            build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
            if not build or build["status"] in ("cancelled", "failed"):
                return False
            conn.execute(
                "UPDATE forge_builds SET status = 'running', updated_at = ? WHERE id = ?",
                (time.time(), build_id),
            )
            conn.execute(
                "UPDATE forge_chunks SET status = 'running', started_at = ? WHERE id = ?",
                (time.time(), chunk_id),
            )

        post_or_update_build_widget(build_id, db_path=self.db_path)

        effective_repo = (chunk_row["repo"] if "repo" in chunk_row.keys() and chunk_row["repo"] else build["repo"]) or "/workspace"
        repo_dir = Path(effective_repo).resolve()
        default_branch = _get_repo_default_branch(repo_dir)
        wt_dir = Path(f"/tmp/forge-wt-{chunk_id}")
        branch_name = f"forge/{build_id}-c{chunk_idx}"
        staging_ref = f"forge/release-{build_id}"

        base_ref = staging_ref
        build_log_path = FORGE_LOGS_DIR / f"build-{build_id}.log"

        def _log_build(msg: str) -> None:
            try:
                FORGE_LOGS_DIR.mkdir(parents=True, exist_ok=True)
                with open(build_log_path, "a", encoding="utf-8") as lf:
                    lf.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}] {msg}\n")
                    lf.flush()
            except Exception:
                pass

        def _ensure_chunk_worktree(force_recreate: bool = False) -> None:
            if not force_recreate and wt_dir.exists() and (wt_dir / ".git").exists():
                return
            git_lk = _acquire_repo_git_lock(repo_dir)
            try:
                latest_def_ref = _resolve_latest_branch_ref(repo_dir, default_branch)
                chk_stg = subprocess.run(["git", "rev-parse", "--verify", staging_ref], cwd=str(repo_dir), capture_output=True)
                if chk_stg.returncode != 0:
                    subprocess.run(["git", "branch", "-f", staging_ref, latest_def_ref], cwd=str(repo_dir), capture_output=True, timeout=10)
                else:
                    stg_is_anc = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", staging_ref, latest_def_ref],
                        cwd=str(repo_dir),
                        capture_output=True,
                        timeout=10,
                    )
                    if stg_is_anc.returncode == 0:
                        subprocess.run(["git", "branch", "-f", staging_ref, latest_def_ref], cwd=str(repo_dir), capture_output=True, timeout=10)

                shutil.rmtree(wt_dir, ignore_errors=True)
                subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)
                wt_res = subprocess.run(
                    ["git", "worktree", "add", "--force", "-B", branch_name, str(wt_dir), base_ref],
                    cwd=str(repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if wt_res.returncode != 0:
                    subprocess.run(
                        ["git", "worktree", "add", "--force", "-B", branch_name, str(wt_dir), "HEAD"],
                        cwd=str(repo_dir),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                def_is_anc = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", latest_def_ref, branch_name],
                    cwd=str(repo_dir),
                    capture_output=True,
                    timeout=10,
                )
                if def_is_anc.returncode != 0 and wt_dir.exists():
                    rb_init = subprocess.run(
                        ["git", "rebase", latest_def_ref],
                        cwd=str(wt_dir),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if rb_init.returncode != 0:
                        subprocess.run(["git", "rebase", "--abort"], cwd=str(wt_dir), capture_output=True, timeout=10)
            finally:
                _release_file_lock(git_lk)

        _ensure_chunk_worktree(force_recreate=True)

        prohibited = json.loads(chunk_row["prohibited_files_json"] or "[]")
        target_files = json.loads(chunk_row["target_files_json"] or "[]")
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            _ensure_chunk_worktree(force_recreate=not wt_dir.exists())
            model = ESCALATION_WORKER_MODEL if attempt == max_attempts else (chunk_row["worker_model"] or DEFAULT_WORKER_MODEL)
            with self._state_lock, get_db_connection(self.db_path) as conn:
                conn.execute(
                    "UPDATE forge_chunks SET attempt_count = ?, worker_model = ?, status = 'running', live_status = ? WHERE id = ?",
                    (attempt, model, f"Booting Vulcan ({model})...", chunk_id),
                )
            post_or_update_build_widget(build_id, db_path=self.db_path)
            _log_build(
                f"[Attempt {attempt}/{max_attempts}] Starting chunk {chunk_id} (index {chunk_idx}: '{chunk_row['title']}') model={model}"
            )

            prompt = format_chunk_prompt(build_id, chunk_idx, db_path=self.db_path, worktree_dir=str(wt_dir))
            agy_cmd = _build_low_priority_prefix() + [
                "/usr/local/bin/agy",
                f"--add-dir={wt_dir}",
                f"-p={prompt}",
                "--output-format=stream-json",
                "--dangerously-skip-permissions",
                "--print-timeout=4h",
                f"--model={model}",
            ]
            profile_snapshot = stage_vulcan_worktree_profile(
                wt_dir,
                repo_dir=repo_dir,
                target_files=target_files,
                origin_channel_id=int(build["origin_channel_id"]) if build["origin_channel_id"] else None,
            )
            try:
                vulcan_env = os.environ.copy()
                vulcan_env["FORGE_WORKER_ACTIVE"] = "1"
                proc = subprocess.Popen(
                    agy_cmd,
                    cwd=str(wt_dir),
                    env=vulcan_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                tail_lines: list[str] = []
                step_cnt = 0
                last_widget_patch = time.time()
                start_ts = time.time()
                last_activity_ts = [start_ts]
                timed_out_reason: list[Optional[str]] = [None]
                watchdog_stop = threading.Event()

                def _chunk_watchdog() -> None:
                    while not watchdog_stop.wait(0.15):
                        if proc.poll() is not None:
                            return
                        now_w = time.time()
                        if (now_w - start_ts) > CHUNK_WORKER_TIMEOUT_SECONDS:
                            timed_out_reason[0] = (
                                f"Chunk worker exceeded {int(CHUNK_WORKER_TIMEOUT_SECONDS)}s execution timeout"
                            )
                            _kill_process_group(proc)
                            return
                        if (now_w - last_activity_ts[0]) > CHUNK_SILENT_STALL_TIMEOUT_SECONDS:
                            timed_out_reason[0] = (
                                f"Chunk worker stalled silently with zero stdout output for "
                                f"{int(CHUNK_SILENT_STALL_TIMEOUT_SECONDS)}s"
                            )
                            _kill_process_group(proc)
                            return

                wd_thread = threading.Thread(target=_chunk_watchdog, daemon=True)
                wd_thread.start()
                try:
                    if proc.stdout:
                        try:
                            for raw_line in proc.stdout:
                                now_ts = time.time()
                                last_activity_ts[0] = now_ts
                                tail_lines.append(raw_line)
                                if len(tail_lines) > 80:
                                    tail_lines.pop(0)
                                status_str, step_cnt = extract_live_chunk_telemetry(raw_line, step_count=step_cnt)
                                if status_str and (now_ts - last_widget_patch >= 12.0 or step_cnt == 1):
                                    last_widget_patch = now_ts
                                    with self._state_lock, get_db_connection(self.db_path) as conn:
                                        conn.execute(
                                            "UPDATE forge_chunks SET live_status = ? WHERE id = ?",
                                            (status_str[:220], chunk_id),
                                        )
                                        conn.execute(
                                            "UPDATE forge_builds SET updated_at = ? WHERE id = ?",
                                            (now_ts, build_id),
                                        )
                                    post_or_update_build_widget(build_id, db_path=self.db_path)
                        except (ValueError, OSError):
                            if not timed_out_reason[0]:
                                raise
                finally:
                    watchdog_stop.set()
                    wd_thread.join(timeout=1.0)

                if timed_out_reason[0]:
                    tail_last_15 = "".join(tail_lines[-15:]).rstrip()
                    _log_build(
                        f"[Attempt {attempt}/{max_attempts}] Worker timed out: {timed_out_reason[0]} (steps: {step_cnt})\n"
                        f"Tail lines (last 15):\n{tail_last_15}"
                    )
                    raise TimeoutError(timed_out_reason[0])
                ret_code = proc.wait(timeout=30)
                tail_last_15 = "".join(tail_lines[-15:]).rstrip()
                _log_build(
                    f"[Attempt {attempt}/{max_attempts}] Worker finished with exit code {ret_code} (steps: {step_cnt})\n"
                    f"Tail lines (last 15):\n{tail_last_15}"
                )
                if ret_code != 0:
                    err_msg = "".join(tail_lines).strip() or f"agy exited {ret_code}"
                    with self._state_lock, get_db_connection(self.db_path) as conn:
                        conn.execute("UPDATE forge_chunks SET last_error = ? WHERE id = ?", (err_msg[-1500:], chunk_id))
            except Exception as e:
                if not timed_out_reason[0]:
                    tail_last_15 = "".join(tail_lines[-15:]).rstrip()
                    _log_build(
                        f"[Attempt {attempt}/{max_attempts}] Worker error: {e} (steps: {step_cnt})\n"
                        f"Tail lines (last 15):\n{tail_last_15}"
                    )
                with self._state_lock, get_db_connection(self.db_path) as conn:
                    conn.execute("UPDATE forge_chunks SET last_error = ? WHERE id = ?", (str(e), chunk_id))
                continue
            finally:
                restore_vulcan_worktree_profile(wt_dir, profile_snapshot)

            with self._state_lock, get_db_connection(self.db_path) as conn:
                conn.execute("UPDATE forge_chunks SET status = 'verifying', live_status = '' WHERE id = ?", (chunk_id,))
            post_or_update_build_widget(build_id, db_path=self.db_path)

            passed, diff_stat, err_out = verify_chunk_worktree(
                wt_dir,
                chunk_row["verification_cmd"],
                prohibited_files=prohibited,
                target_files=target_files,
                require_changes=True,
            )
            v_status = "PASSED" if passed else "FAILED"
            _log_build(
                f"[Attempt {attempt}/{max_attempts}] verify_chunk_worktree result: {v_status}\n"
                f"Command: {chunk_row['verification_cmd']}\n"
                f"Diff stat: {diff_stat}\n"
                f"Verification output:\n{err_out.strip()}"
            )
            if passed:
                git_lk = _acquire_repo_git_lock(repo_dir)
                try:
                    diff_proc = subprocess.run(
                        ["git", "diff", "--shortstat", "HEAD"],
                        cwd=str(wt_dir),
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if diff_proc.stdout.strip():
                        diff_stat = diff_proc.stdout.strip()
                    subprocess.run(["git", "add", "-A"], cwd=str(wt_dir), capture_output=True, timeout=30)
                    try:
                        commit_proc = subprocess.run(
                            ["git", "commit", "-m", f"feat(forge): {chunk_row['title']} [{chunk_id}]"],
                            cwd=str(wt_dir),
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        if commit_proc.returncode != 0:
                            c_err = f"{commit_proc.stdout}\n{commit_proc.stderr}".strip()
                            _log_build(f"[Attempt {attempt}/{max_attempts}] Git commit failed (code {commit_proc.returncode}): {c_err}")
                            with self._state_lock, get_db_connection(self.db_path) as conn:
                                conn.execute(
                                    "UPDATE forge_chunks SET last_error = ? WHERE id = ?",
                                    (f"git commit / pre-commit hook failed (exit {commit_proc.returncode}):\n{c_err[-1400:]}", chunk_id),
                                )
                            continue
                    except Exception as ce:
                        _log_build(f"[Attempt {attempt}/{max_attempts}] Git commit exception: {ce}")
                        with self._state_lock, get_db_connection(self.db_path) as conn:
                            conn.execute(
                                "UPDATE forge_chunks SET last_error = ? WHERE id = ?",
                                (f"git commit failed/timed out: {ce}", chunk_id),
                            )
                        continue

                    # If a parallel sibling chunk already advanced base_ref since this worktree branched,
                    # rebase this chunk's commit onto the latest base_ref before fast-forwarding base_ref.
                    anc_chk = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", base_ref, branch_name],
                        cwd=str(repo_dir),
                        capture_output=True,
                        timeout=10,
                    )
                    if anc_chk.returncode != 0:
                        rb_proc = subprocess.run(
                            ["git", "rebase", base_ref],
                            cwd=str(wt_dir),
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        if rb_proc.returncode != 0:
                            subprocess.run(["git", "rebase", "--abort"], cwd=str(wt_dir), capture_output=True, timeout=10)
                            shutil.rmtree(wt_dir, ignore_errors=True)
                            subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)
                            rb_err = f"{rb_proc.stdout}\n{rb_proc.stderr}".strip()
                            _log_build(f"[Attempt {attempt}/{max_attempts}] Git rebase onto {base_ref} failed: {rb_err}")
                            with self._state_lock, get_db_connection(self.db_path) as conn:
                                conn.execute(
                                    "UPDATE forge_chunks SET last_error = ? WHERE id = ?",
                                    (f"git rebase onto {base_ref} failed:\n{rb_err}"[-1400:], chunk_id),
                                )
                            continue

                    # Also ensure the chunk worktree is rebased onto the latest default_branch if default_branch advanced in repo_dir
                    anc_def = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", default_branch, branch_name],
                        cwd=str(repo_dir),
                        capture_output=True,
                        timeout=10,
                    )
                    if anc_def.returncode != 0:
                        rb_def = subprocess.run(
                            ["git", "rebase", default_branch],
                            cwd=str(wt_dir),
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        if rb_def.returncode != 0:
                            subprocess.run(["git", "rebase", "--abort"], cwd=str(wt_dir), capture_output=True, timeout=10)
                            shutil.rmtree(wt_dir, ignore_errors=True)
                            subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)
                            rb_def_err = f"{rb_def.stdout}\n{rb_def.stderr}".strip()
                            _log_build(f"[Attempt {attempt}/{max_attempts}] Git rebase onto {default_branch} failed: {rb_def_err}")
                            with self._state_lock, get_db_connection(self.db_path) as conn:
                                conn.execute(
                                    "UPDATE forge_chunks SET last_error = ? WHERE id = ?",
                                    (f"git rebase onto {default_branch} failed:\n{rb_def_err}"[-1400:], chunk_id),
                                )
                            continue

                    subprocess.run(
                        ["git", "branch", "-f", base_ref, branch_name],
                        cwd=str(repo_dir),
                        capture_output=True,
                        timeout=10,
                    )

                    with self._state_lock, get_db_connection(self.db_path) as conn:
                        other_unmerged = conn.execute(
                            "SELECT COUNT(*) AS cnt FROM forge_chunks WHERE build_id = ? AND id != ? AND status != 'merged'",
                            (build_id, chunk_id),
                        ).fetchone()["cnt"]

                    # If final chunk completed and not touching core bridge, sync staging branch into local default branch
                    # BEFORE marking the chunk/build completed so a merge/cherry-pick conflict never leaves repo_dir dirty or drops commits
                    if other_unmerged == 0 and not build["touches_core_bridge"]:
                        for abort_cmd in (
                            ["git", "cherry-pick", "--abort"],
                            ["git", "merge", "--abort"],
                            ["git", "rebase", "--abort"],
                        ):
                            subprocess.run(abort_cmd, cwd=str(repo_dir), capture_output=True, timeout=10)

                        sync_err = ""
                        try:
                            anc_main = subprocess.run(
                                ["git", "merge-base", "--is-ancestor", default_branch, base_ref],
                                cwd=str(repo_dir),
                                capture_output=True,
                                timeout=10,
                            )
                            if anc_main.returncode == 0:
                                ff_proc = subprocess.run(
                                    ["git", "merge", "--ff-only", base_ref],
                                    cwd=str(repo_dir),
                                    capture_output=True,
                                    text=True,
                                    timeout=20,
                                )
                                if ff_proc.returncode != 0:
                                    cp_proc = subprocess.run(
                                        ["git", "cherry-pick", f"{default_branch}..{base_ref}"],
                                        cwd=str(repo_dir),
                                        capture_output=True,
                                        text=True,
                                        timeout=30,
                                    )
                                    if cp_proc.returncode != 0:
                                        subprocess.run(["git", "cherry-pick", "--abort"], cwd=str(repo_dir), capture_output=True, timeout=10)
                                        sync_err = f"git merge --ff-only and cherry-pick failed:\n{ff_proc.stdout}\n{ff_proc.stderr}\n{cp_proc.stdout}\n{cp_proc.stderr}".strip()
                            else:
                                cp_proc = subprocess.run(
                                    ["git", "cherry-pick", f"{default_branch}..{base_ref}"],
                                    cwd=str(repo_dir),
                                    capture_output=True,
                                    text=True,
                                    timeout=30,
                                )
                                if cp_proc.returncode != 0:
                                    subprocess.run(["git", "cherry-pick", "--abort"], cwd=str(repo_dir), capture_output=True, timeout=10)
                                    sync_err = f"git cherry-pick {default_branch}..{base_ref} failed:\n{cp_proc.stdout}\n{cp_proc.stderr}".strip()
                        except Exception as e:
                            subprocess.run(["git", "cherry-pick", "--abort"], cwd=str(repo_dir), capture_output=True, timeout=10)
                            sync_err = f"git merge/cherry-pick {base_ref} failed/timed out: {e}"

                        if sync_err:
                            print(f"[FORGE] Warning: {sync_err}", file=sys.stderr)
                            shutil.rmtree(wt_dir, ignore_errors=True)
                            subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)
                            with self._state_lock, get_db_connection(self.db_path) as conn:
                                conn.execute(
                                    "UPDATE forge_chunks SET last_error = ? WHERE id = ?",
                                    (sync_err[-1400:], chunk_id),
                                )
                            continue

                    shutil.rmtree(wt_dir, ignore_errors=True)
                    subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)

                    with self._state_lock, get_db_connection(self.db_path) as conn:
                        conn.execute(
                            "UPDATE forge_chunks SET status = 'merged', branch_name = ?, diff_stat = ?, completed_at = ?, live_status = '', last_error = '' WHERE id = ?",
                            (branch_name, diff_stat, time.time(), chunk_id),
                        )
                        remaining = conn.execute(
                            "SELECT COUNT(*) AS cnt FROM forge_chunks WHERE build_id = ? AND status != 'merged'",
                            (build_id,),
                        ).fetchone()["cnt"]
                        if remaining == 0:
                            conn.execute(
                                "UPDATE forge_builds SET status = 'completed', updated_at = ? WHERE id = ?",
                                (time.time(), build_id),
                            )

                    if remaining == 0 and not build["touches_core_bridge"]:
                        try:
                            subprocess.run(["git", "push", "origin", default_branch], cwd=str(repo_dir), capture_output=True, timeout=60)
                        except Exception as e:
                            print(f"[FORGE] Warning: git push origin {default_branch} failed/timed out: {e}", file=sys.stderr)
                finally:
                    _release_file_lock(git_lk)

                _log_build(f"Chunk {chunk_id} successfully merged into {branch_name} (diff: {diff_stat})")
                post_or_update_build_widget(build_id, db_path=self.db_path)
                sync_forge_channel_header(db_path=self.db_path)
                if remaining == 0:
                    dispatch_build_completion_to_origin(build_id, db_path=self.db_path)
                return True
            else:
                with self._state_lock, get_db_connection(self.db_path) as conn:
                    conn.execute("UPDATE forge_chunks SET last_error = ? WHERE id = ?", (err_out, chunk_id))

        # Exhausted all attempts
        _log_build(f"Chunk {chunk_id} failed after exhausting all {max_attempts} attempts.")
        git_lk = _acquire_repo_git_lock(repo_dir)
        try:
            shutil.rmtree(wt_dir, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)
        finally:
            _release_file_lock(git_lk)
        with self._state_lock, get_db_connection(self.db_path) as conn:
            conn.execute("UPDATE forge_chunks SET status = 'failed', completed_at = ? WHERE id = ?", (time.time(), chunk_id))
            conn.execute("UPDATE forge_builds SET status = 'failed', updated_at = ? WHERE id = ?", (time.time(), build_id))
        post_or_update_build_widget(build_id, db_path=self.db_path)
        sync_forge_channel_header(db_path=self.db_path)
        dispatch_build_failure_to_origin(build_id, db_path=self.db_path)
        return False


forge_supervisor = ForgeSupervisor()


# ---------------------------------------------------------------------------
# 4. Pillar 3: Native Detached Background Job Runner (`forge_jobs`)
# ---------------------------------------------------------------------------

def _get_job_dir(task_id: str) -> Path:
    return TASKS_DIR / task_id


def _is_prod_forge_env(db_path: Path | str = FORGE_DB_PATH) -> bool:
    """Return True if executing against the production FORGE SQLite DB and TASKS_DIR."""
    try:
        return (
            Path(db_path).resolve() == FORGE_DB_PATH.resolve()
            and TASKS_DIR.resolve() == (WORKSPACE_DIR / "data" / "detached_tasks").resolve()
        )
    except Exception:
        return False


def _read_job_meta(task_id: str, db_path: Path | str = FORGE_DB_PATH) -> Optional[dict]:
    """Read job metadata from `forge_jobs` in SQLite (with legacy `meta.json` fallback)."""
    meta_file = _get_job_dir(task_id) / "meta.json"
    meta = None
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        with get_db_connection(db_path) as conn:
            row = conn.execute("SELECT * FROM forge_jobs WHERE id = ?", (task_id,)).fetchone()
            if row:
                d = dict(row)
                d["task_id"] = d["id"]
                d["notify_on_success"] = bool(d.get("notify_on_success"))
                d["synthesize"] = bool(d.get("synthesize", 1))
                if meta:
                    for field in ("status", "exit_code", "end_time", "duration_seconds", "forge_message_id", "worker_pid", "child_pid"):
                        if d.get(field) is not None:
                            meta[field] = d[field]
                    return meta
                return d
    except Exception:
        pass
    return meta


def _write_job_meta(task_id: str, meta: dict, db_path: Path | str = FORGE_DB_PATH) -> None:
    """Persist job metadata to `TASKS_DIR/<id>/meta.json` and sync to `forge_jobs` when in production."""
    task_dir = _get_job_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    meta_file = task_dir / "meta.json"
    if meta.get("forge_message_id") is None and meta_file.exists():
        try:
            existing = json.loads(meta_file.read_text(encoding="utf-8"))
            if existing.get("forge_message_id") is not None:
                meta["forge_message_id"] = existing["forge_message_id"]
        except Exception:
            pass
    temp_file = task_dir / "meta.json.tmp"
    temp_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    temp_file.replace(meta_file)

    is_prod_dir = (TASKS_DIR.resolve() == (WORKSPACE_DIR / "data" / "detached_tasks").resolve())
    is_custom_db = (Path(db_path).resolve() != FORGE_DB_PATH.resolve())
    if is_prod_dir or is_custom_db:
        try:
            with get_db_connection(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO forge_jobs (
                        id, name, command, cwd, channel, timeout, status,
                        worker_pid, child_pid, exit_code, log_file, forge_message_id,
                        notify_on_success, synthesize, start_time, end_time, duration_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        worker_pid=excluded.worker_pid,
                        child_pid=excluded.child_pid,
                        exit_code=excluded.exit_code,
                        end_time=excluded.end_time,
                        duration_seconds=excluded.duration_seconds,
                        forge_message_id=COALESCE(excluded.forge_message_id, forge_jobs.forge_message_id)
                    """,
                    (
                        task_id,
                        meta.get("name", "Background Task"),
                        meta.get("command", ""),
                        meta.get("cwd", "/workspace"),
                        str(meta.get("channel", "zero-chat")),
                        int(meta.get("timeout", 3600)),
                        meta.get("status", "starting"),
                        meta.get("worker_pid"),
                        meta.get("child_pid"),
                        meta.get("exit_code"),
                        str(meta.get("log_file", "")),
                        meta.get("forge_message_id"),
                        1 if meta.get("notify_on_success") else 0,
                        1 if meta.get("synthesize", True) else 0,
                        float(meta.get("start_time") or time.time()),
                        meta.get("end_time"),
                        meta.get("duration_seconds"),
                    ),
                )
        except Exception:
            pass


def _is_job_worker_flock_held(task_id: str) -> bool:
    lock_path = _get_job_dir(task_id) / "worker.lock"
    if not lock_path.exists():
        return False
    try:
        with open(lock_path, "a+", encoding="utf-8") as lf:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            else:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                return False
    except Exception:
        return False


def _is_job_active(meta: dict) -> bool:
    status = meta.get("status")
    if status not in ("starting", "running"):
        return False
    task_id = meta.get("task_id") or meta.get("id") or ""
    if task_id and _is_job_worker_flock_held(task_id):
        return True
    child_pid = meta.get("child_pid")
    worker_pid = meta.get("worker_pid")
    if _is_pid_running(child_pid) or _is_pid_running(worker_pid):
        return True
    start_time = meta.get("start_time") or 0
    if status == "starting" and (time.time() - float(start_time)) < 5.0:
        return True
    return False


def _find_active_duplicate_job(command: str, cwd: str, db_path: Path | str = FORGE_DB_PATH) -> Optional[dict]:
    norm_cmd = command.strip()
    norm_cwd = cwd.strip()
    if TASKS_DIR.exists():
        for entry in sorted(TASKS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not entry.is_dir():
                continue
            meta = _read_job_meta(entry.name, db_path=db_path)
            if not meta:
                continue
            if meta.get("command", "").strip() == norm_cmd and meta.get("cwd", "/workspace").strip() == norm_cwd:
                if _is_job_active(meta):
                    return meta
    return None


def _check_recent_repeated_job_failures(
    command: str,
    name: str,
    db_path: Path | str = FORGE_DB_PATH,
    window_seconds: float = 600.0,
    max_failures: int = 2,
) -> tuple[bool, str]:
    """Circuit breaker: block spawning detached jobs if the same command or job name failed >= max_failures times in the last window_seconds."""
    now = time.time()
    cutoff = now - window_seconds
    norm_cmd = command.strip()
    norm_name = name.strip().lower()
    generic_names = {"", "ad-hoc background task", "background task", "job", "test"}

    script_match = re.search(r"\b(?:python[0-9.]*|bash|sh)\s+([^\s;\"'|&>]+\.(?:py|sh))\b", norm_cmd)
    target_script = script_match.group(1) if script_match else ""

    try:
        with get_db_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, name, command, exit_code, end_time
                FROM forge_jobs
                WHERE status IN ('failed', 'timed_out') AND start_time >= ?
                ORDER BY start_time DESC
                """,
                (cutoff,),
            ).fetchall()
    except Exception:
        return True, ""

    matching_failures = []
    for r in rows:
        r_cmd = str(r["command"] or "").strip()
        r_name = str(r["name"] or "").strip().lower()
        same_cmd = (r_cmd == norm_cmd)
        same_script = bool(target_script and target_script in r_cmd)
        same_name = bool(norm_name and norm_name not in generic_names and r_name == norm_name)
        if same_cmd or same_script or same_name:
            matching_failures.append(r)

    if len(matching_failures) >= max_failures:
        recent_ids = ", ".join(r["id"] for r in matching_failures[:3])
        return False, (
            f"FORGE Detached Job Failure Circuit Breaker Triggered: Job '{name or norm_cmd[:60]}' has already failed "
            f"{len(matching_failures)} times in the last {int(window_seconds // 60)} minutes ({recent_ids}). "
            "Do NOT repeatedly edit and re-fire 'forge.py job start' in a loop. "
            "Verify your fix locally with a targeted test, enqueue a structured ForgePlan via 'forge.py enqueue', "
            "or surface the concrete root-cause blocker to Ryan."
        )
    return True, ""


def start_job(
    command: str,
    channel: str = "",
    name: str = "",
    timeout: int = 3600,
    cwd: str = "/workspace",
    notify_on_success: bool = True,
    synthesize: bool = True,
    allow_duplicate: bool = False,
    db_path: Path | str = FORGE_DB_PATH,
) -> dict:
    """Spawn a detached background shell job backed by `forge_jobs` and return its metadata immediately."""
    channel = channel or get_default_origin_channel()
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    enqueue_lock_path = TASKS_DIR / ".enqueue.lock"

    with open(enqueue_lock_path, "a+", encoding="utf-8") as elock:
        fcntl.flock(elock.fileno(), fcntl.LOCK_EX)
        try:
            if not allow_duplicate:
                existing = _find_active_duplicate_job(command, cwd, db_path=db_path)
                if existing:
                    dup_meta = dict(existing)
                    dup_meta["deduplicated"] = True
                    return dup_meta

                ok_cb, cb_reason = _check_recent_repeated_job_failures(command, name, db_path=db_path)
                if not ok_cb:
                    raise RuntimeError(cb_reason)

            name_clean = name.strip() or "Ad-Hoc Background Task"
            slug = re.sub(r"[^a-zA-Z0-9_-]", "", name_clean.lower().replace(" ", "-"))[:20] or "job"
            task_id = f"dt-{int(time.time())}-{slug}"
            task_dir = _get_job_dir(task_id)
            if task_dir.exists():
                task_id = f"dt-{int(time.time() * 1000)}-{slug}"
                task_dir = _get_job_dir(task_id)
            task_dir.mkdir(parents=True, exist_ok=True)

            log_file = task_dir / "run.log"
            log_file.touch(exist_ok=True)

            meta = {
                "task_id": task_id,
                "name": name_clean,
                "command": command,
                "channel": channel,
                "timeout": int(timeout),
                "cwd": cwd,
                "status": "starting",
                "worker_pid": None,
                "child_pid": None,
                "start_time": time.time(),
                "start_time_iso": datetime.now(timezone.utc).isoformat(),
                "end_time": None,
                "duration_seconds": None,
                "exit_code": None,
                "notify_on_success": bool(notify_on_success),
                "synthesize": bool(synthesize),
                "log_file": str(log_file),
            }
            _write_job_meta(task_id, meta, db_path=db_path)

            worker_cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                "_job_worker",
                task_id,
                "--db-path",
                str(db_path),
            ]
            proc = subprocess.Popen(
                worker_cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            time.sleep(0.15)
            updated_meta = _read_job_meta(task_id, db_path=db_path) or meta
            if not updated_meta.get("worker_pid"):
                updated_meta["worker_pid"] = proc.pid
                _write_job_meta(task_id, updated_meta, db_path=db_path)
            if not (not bool(notify_on_success) and not bool(synthesize)):
                sync_forge_channel_header(db_path=db_path)

            return updated_meta
        finally:
            fcntl.flock(elock.fileno(), fcntl.LOCK_UN)


def _run_job_worker(task_id: str, db_path: Path | str = FORGE_DB_PATH) -> None:
    """Internal entrypoint executed in the detached background job worker process."""
    meta = _read_job_meta(task_id, db_path=db_path)
    if not meta:
        sys.exit(1)

    task_dir = _get_job_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_file_path = task_dir / "worker.lock"
    lock_fh = open(lock_file_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        lock_fh.close()
        return

    try:
        _run_job_worker_locked(task_id, meta, db_path=db_path)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


JOB_WIDGET_MIN_DURATION_SECONDS: float = 90.0


def _run_job_worker_locked(task_id: str, meta: dict, db_path: Path | str = FORGE_DB_PATH) -> None:
    meta["worker_pid"] = os.getpid()
    meta["status"] = "running"
    _write_job_meta(task_id, meta, db_path=db_path)
    is_quiet_probe = not bool(meta.get("notify_on_success", True)) and not bool(meta.get("synthesize", True))
    if _is_prod_forge_env(db_path) and not is_quiet_probe:
        sync_forge_channel_header(db_path=db_path)

    command = meta["command"]
    timeout = meta.get("timeout", 3600)
    cwd = meta.get("cwd", "/workspace")
    channel = meta.get("channel", "zero-chat")
    name = meta.get("name", "Background Task")
    log_file_path = Path(meta["log_file"])

    start_ts = time.time()
    exit_code = -1
    timed_out = False
    cancelled = False

    with open(log_file_path, "a", encoding="utf-8") as lf:
        lf.write(f"--- [Detached Runner] Started at {datetime.now(timezone.utc).isoformat()} ---\n")
        lf.write(f"--- Command: {command} ---\n")
        lf.write(f"--- Target Channel: {channel} | Timeout: {timeout}s ---\n\n")
        lf.flush()

        try:
            worker_env = os.environ.copy()
            if "/workspace" not in worker_env.get("PYTHONPATH", ""):
                worker_env["PYTHONPATH"] = f"/workspace:{worker_env.get('PYTHONPATH', '')}".strip(":")
            child_proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                env=worker_env,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=_apply_low_priority if hasattr(os, "nice") else None,
            )
            meta["child_pid"] = child_proc.pid
            _write_job_meta(task_id, meta, db_path=db_path)

            try:
                while True:
                    elapsed = time.time() - start_ts
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)
                    wait_slice = min(15.0, remaining)
                    try:
                        exit_code = child_proc.wait(timeout=wait_slice)
                        break
                    except subprocess.TimeoutExpired:
                        lf.flush()
                        fresh = _read_job_meta(task_id, db_path=db_path)
                        if fresh and fresh.get("status") == "cancelled":
                            cancelled = True
                            try:
                                os.killpg(os.getpgid(child_proc.pid), signal.SIGTERM)
                            except Exception:
                                pass
                            exit_code = -15
                            break
                        elapsed_now = time.time() - start_ts
                        if _is_prod_forge_env(db_path) and not is_quiet_probe and elapsed_now >= JOB_WIDGET_MIN_DURATION_SECONDS:
                            post_or_update_job_widget(task_id, db_path=db_path)
            except subprocess.TimeoutExpired:
                timed_out = True
                lf.write(f"\n--- [Detached Runner] ERROR: Task exceeded {timeout}s timeout. Terminating... ---\n")
                lf.flush()
                try:
                    os.killpg(os.getpgid(child_proc.pid), signal.SIGTERM)
                    time.sleep(1.0)
                    if child_proc.poll() is None:
                        os.killpg(os.getpgid(child_proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                exit_code = -99
        except Exception as ex:
            lf.write(f"\n--- [Detached Runner] Process spawn error: {ex} ---\n")
            lf.flush()
            exit_code = -1

    end_ts = time.time()
    duration = end_ts - start_ts

    fresh_meta = _read_job_meta(task_id, db_path=db_path) or meta
    if fresh_meta.get("status") == "cancelled":
        cancelled = True

    final_status = "cancelled" if cancelled else ("timed_out" if timed_out else ("completed" if exit_code == 0 else "failed"))
    fresh_meta["status"] = final_status
    fresh_meta["exit_code"] = exit_code
    fresh_meta["end_time"] = end_ts
    fresh_meta["duration_seconds"] = duration
    _write_job_meta(task_id, fresh_meta, db_path=db_path)
    if _is_prod_forge_env(db_path):
        should_post_terminal_card = bool(
            fresh_meta.get("forge_message_id")
            or (not is_quiet_probe and duration >= JOB_WIDGET_MIN_DURATION_SECONDS)
        )
        if should_post_terminal_card:
            post_or_update_job_widget(task_id, db_path=db_path)
        if final_status == "completed":
            prune_completed_job_widgets(db_path=db_path)
    sync_forge_channel_header(db_path=db_path)

    # Check if log_file_path has zero non-header lines due to stdout/stderr file redirection in command
    try:
        if log_file_path.exists():
            log_content = log_file_path.read_text(encoding="utf-8", errors="replace")
            non_header_lines = [
                line for line in log_content.splitlines()
                if line.strip() and not (
                    line.strip().startswith("--- [Detached Runner]")
                    or line.strip().startswith("--- Command:")
                    or line.strip().startswith("--- Target Channel:")
                )
            ]
            if not non_header_lines:
                redir_target = None
                for m in re.finditer(r'(?:^|\s)[12]?>>?\s*([^\s;&|>]+)', command):
                    cand = m.group(1).strip().strip("'\"")
                    if cand not in ("/dev/null", "&1", "&2"):
                        redir_target = cand
                        break
                if redir_target:
                    redir_path = Path(redir_target)
                    if not redir_path.is_absolute():
                        redir_path = Path(cwd) / redir_path
                    if redir_path.exists() and redir_path.is_file():
                        redir_lines = redir_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        tail_lines = redir_lines[-40:]
                        if tail_lines:
                            with open(log_file_path, "a", encoding="utf-8") as lf:
                                lf.write("\n".join(tail_lines) + "\n")
    except Exception:
        pass

    dur_str = _format_duration(duration)
    notify_on_success = fresh_meta.get("notify_on_success", False)
    if not notify_on_success:
        try:
            from tools.bridge_state import get_runtime_rules
            notify_on_success = get_runtime_rules().get("detached_notify_on_success", False)
        except Exception:
            pass

    if final_status == "completed" and not notify_on_success:
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n[Detached Runner] Task completed successfully in {dur_str}. Suppressing success notification (failure-only policy active).\n")
        return

    # Suppress redundant outbox synthesis alerts ONLY when a short (<25s) task COMPLETED successfully
    # while the originating channel's interactive turn is still actively in-flight and polling it directly.
    # NEVER suppress failed or timed_out task wakeups, because an agent that spawns a detached job and ends
    # its turn immediately will otherwise never be woken up when the job crashes at 2s.
    if final_status == "completed" and duration < 25.0 and _is_channel_turn_actively_in_flight(channel):
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(
                f"\n[Detached Runner] Short task ({dur_str}, status={final_status}) finished while "
                f"origin channel ({channel}) turn is still actively in-flight. "
                "Suppressing duplicate outbox notification so active turn handles result inline.\n"
            )
        return

    synthesize = fresh_meta.get("synthesize", True)
    is_quiet_probe = not notify_on_success and not synthesize
    # When --no-synthesize is explicitly passed and the originating channel's
    # interactive turn is still actively in-flight, suppress raw outbox failure dumps so
    # the active turn can inspect and recover inline without spamming Discord.
    if not synthesize and _is_channel_turn_actively_in_flight(channel):
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(
                f"\n[Detached Runner] --no-synthesize probe ({dur_str}, status={final_status}) finished while "
                f"origin channel ({channel}) turn is still actively in-flight. "
                "Suppressing raw outbox notification so active turn handles result inline.\n"
            )
        return

    # Internal diagnostic probes launched with --silent-on-success --no-synthesize must never post
    # raw failure banners to Discord even if the originating channel turn has already finished.
    if is_quiet_probe:
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(
                f"\n[Detached Runner] Quiet probe ({dur_str}, status={final_status}) finished while "
                f"origin channel ({channel}) turn is not in-flight. "
                "Suppressing raw outbox failure banner to prevent false-alarm Discord spam.\n"
            )
        return

    persona_name = "Ivy" if str(channel).strip().lstrip("#") in ("baseball", str(BASEBALL_CHANNEL_ID)) else "Zero"
    if synthesize and final_status in ("completed", "failed", "timed_out"):
        status_banner = (
            "DETACHED TASK COMPLETE"
            if final_status == "completed"
            else ("DETACHED TASK TIMED OUT" if final_status == "timed_out" else "DETACHED TASK FAILED")
        )
        directive_text = (
            f"The background task has finished successfully. Review the execution log at {log_file_path}, "
            f"extract the key results, metrics, or anomalies, and deliver a concise, natural executive briefing directly to Ryan in character as {persona_name}."
            if final_status == "completed"
            else (
                f"The background task exited with status '{final_status}' (exit code {exit_code}). "
                f"Inspect the execution log at {log_file_path}, diagnose the failure, fix the underlying bug or script issue if appropriate, "
                f"and either relaunch or report the root cause concisely to Ryan in character as {persona_name}. "
                f"If this task failure was already handled inline during your previous turn, emit [NO_REPLY]."
            )
        )
        synthesis_prompt = f"""[SYSTEM NOTIFICATION: {status_banner}]
Task Name: {name} (ID: {task_id})
Duration: {dur_str} | Exit Code: {exit_code}
Log Path: {log_file_path}

Directive:
{directive_text}
"""
        try:
            from tools.outbox import queue_agent_turn
            queue_agent_turn(
                channel=channel,
                prompt=synthesis_prompt,
                job_name=f"Detached Task: {name}",
            )
            with open(log_file_path, "a", encoding="utf-8") as lf:
                lf.write(f"\n[Detached Runner] 🟢 Dispatched autonomous agent synthesis turn ({final_status}) via Outbox.\n")
            return
        except Exception as se:
            with open(log_file_path, "a", encoding="utf-8") as lf:
                lf.write(f"\n[Detached Runner] Warning: Agent synthesis queue failed ({se}). Falling back to direct outbox message.\n")

    log_snippet = ""
    try:
        with open(log_file_path, "r", encoding="utf-8", errors="replace") as lf:
            lines = lf.readlines()
            filtered = [
                line.rstrip() for line in lines
                if line.strip() and not line.strip().startswith("--- [Detached Runner]")
            ]
            tail = filtered[-12:]
            if tail:
                log_snippet = "\n```text\n" + "\n".join(tail) + "\n```"
    except Exception:
        pass

    if final_status == "completed":
        header = f"✅ **Background Task Complete: {name}** ({dur_str})"
    elif final_status == "timed_out":
        header = f"⏱️ **Background Task Timed Out: {name}** (Limit: {timeout}s)"
    elif final_status == "cancelled":
        header = f"🛑 **Background Task Cancelled: {name}** (after {dur_str})"
    else:
        header = f"❌ **Background Task Failed: {name}** (Exit Code: {exit_code}, after {dur_str})"

    msg_lines = [header]
    if log_snippet:
        snippet_header = "**Output:**" if final_status == "completed" else "**Error Excerpt:**"
        msg_lines.append(f"{snippet_header}{log_snippet}")

    msg_lines.append(f"• *Inspect full logs:* `python3 /workspace/tools/detached_runner.py logs {task_id}`")
    outbox_body = "\n".join(msg_lines)

    try:
        from tools.outbox import queue_outbox_message
        queue_outbox_message(
            channel=channel,
            content=outbox_body,
            source_turn=f"detached-{task_id}",
        )
    except Exception as oe:
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n[Detached Runner] Failed to dispatch outbox message: {oe}\n")


def cancel_job(task_id: str, db_path: Path | str = FORGE_DB_PATH) -> dict:
    """Cancel an active background job and immediately prune its #forge embed card."""
    meta = _read_job_meta(task_id, db_path=db_path)
    if not meta:
        return {"error": f"Task '{task_id}' not found."}

    child_pid = meta.get("child_pid")
    worker_pid = meta.get("worker_pid")

    meta["status"] = "cancelled"
    meta["end_time"] = meta.get("end_time") or time.time()
    _write_job_meta(task_id, meta, db_path=db_path)

    killed = False
    for pid in (child_pid, worker_pid):
        if pid and _is_pid_running(pid):
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
                killed = True
            except Exception:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    killed = True
                except Exception:
                    pass

    if _is_prod_forge_env(db_path):
        prune_completed_job_widgets(db_path=db_path)
        sync_forge_channel_header(db_path=db_path)

    return {"task_id": task_id, "status": "cancelled", "process_killed": killed}


def get_job_status(task_id: str, db_path: Path | str = FORGE_DB_PATH) -> dict:
    """Get status of a specific detached job."""
    meta = _read_job_meta(task_id, db_path=db_path)
    if not meta:
        return {"error": f"Task '{task_id}' not found."}

    child_pid = meta.get("child_pid")
    worker_pid = meta.get("worker_pid")
    is_alive = _is_pid_running(child_pid) or _is_pid_running(worker_pid)

    if meta["status"] == "running" and not is_alive:
        meta["status"] = "unknown_exit"

    meta["is_running"] = is_alive
    if is_alive:
        meta["current_duration"] = _format_duration(time.time() - float(meta["start_time"]))
    elif meta.get("duration_seconds"):
        meta["current_duration"] = _format_duration(float(meta["duration_seconds"]))

    return meta


def list_jobs(limit: int = 15, db_path: Path | str = FORGE_DB_PATH) -> list[dict]:
    """List recent detached jobs from SQLite (or TASKS_DIR fallback)."""
    tasks: list[dict] = []
    seen: set[str] = set()
    try:
        rows = _query_unified_rows(
            "SELECT * FROM forge_jobs",
            db_path=db_path,
            sort_key="start_time",
            reverse=True,
            limit=limit,
        )
        for d in rows:
            d["task_id"] = d["id"]
            d["is_running"] = _is_pid_running(d.get("child_pid")) or _is_pid_running(d.get("worker_pid"))
            tasks.append(d)
            seen.add(d["id"])
    except Exception:
        pass

    if len(tasks) < limit and TASKS_DIR.exists():
        for entry in sorted(TASKS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if entry.is_dir() and entry.name not in seen:
                meta = _read_job_meta(entry.name, db_path=db_path)
                if meta:
                    meta["is_running"] = _is_pid_running(meta.get("child_pid"))
                    tasks.append(meta)
                    if len(tasks) >= limit:
                        break
    return tasks


# ---------------------------------------------------------------------------
# 5. Pillar 4: Deterministic Decisions Framework (Inline Approval Cards)
# ---------------------------------------------------------------------------

def _validate_decision_shell_payload(handler_type: str, payload: dict) -> tuple[bool, str]:
    """Pre-flight validate `shell` Decision Card payloads so malformed bash/python quoting fails at stage time rather than on Approve click."""
    if handler_type != "shell":
        return True, ""
    cmd = str(payload.get("command") or "").strip()
    if not cmd:
        return False, "Decision Card with handler 'shell' requires a non-empty 'command' in payload."

    # 1. Bash syntax pre-flight (`bash -n`)
    try:
        chk = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True, timeout=5)
        if chk.returncode != 0:
            err_msg = (chk.stderr or chk.stdout or "syntax error").strip().splitlines()[0]
            return False, (
                f"Decision Card shell command has invalid bash syntax ({err_msg}). "
                "Write multi-line Python/shell logic to a standalone script in /workspace/scratch/<name>.py and stage 'python3 /workspace/scratch/<name>.py' instead."
            )
    except Exception:
        pass

    # 2. If `python3 -c` is used inline, block multi-line inline scripts or verify AST syntax via shlex
    if re.search(r"\bpython[0-9.]*\s+-c\b", cmd):
        if "\n" in cmd or '"""' in cmd or "'''" in cmd:
            return False, (
                "Decision Card 'shell' payload contains a multi-line or triple-quoted inline 'python3 -c' string, "
                "which breaks under '/bin/sh -c' quoting when clicked. Write the script to '/workspace/scratch/<name>.py' "
                "and stage 'python3 /workspace/scratch/<name>.py' as the command."
            )
        try:
            import shlex
            tokens = shlex.split(cmd)
            for idx, tok in enumerate(tokens):
                if tok == "-c" and idx + 1 < len(tokens) and idx > 0 and "python" in tokens[idx - 1]:
                    ast.parse(tokens[idx + 1])
        except SyntaxError as e:
            return False, (
                f"Decision Card inline 'python3 -c' payload has a Python SyntaxError ({e.msg} at line {e.lineno}). "
                "Write the script to '/workspace/scratch/<name>.py' and stage 'python3 /workspace/scratch/<name>.py' instead."
            )
        except ValueError as e:
            return False, f"Decision Card shell command has unbalanced quotes ({e})."

    return True, ""


def stage_decision(
    category: str,
    title: str,
    summary: str,
    handler_type: str,
    payload: dict,
    channel_id: int = ZERO_CHAT_CHANNEL_ID,
    ttl_seconds: float = 7200.0,
    is_private: Optional[bool] = None,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = False,
) -> dict:
    """Stage a deterministic Decision Card in SQLite (and optionally post directly to Discord)."""
    ok_val, val_err = _validate_decision_shell_payload(handler_type.strip(), payload)
    if not ok_val:
        raise ValueError(val_err)
    now = time.time()
    slug = re.sub(r"[^a-z0-9]+", "", category.lower())[:8] or "act"
    decision_id = f"dec-{int(now * 1000) % 100000000}-{slug}"
    priv_flag = 1 if (is_private if is_private is not None else (int(channel_id) == ZERO_CHAT_CHANNEL_ID)) else 0

    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forge_decisions (
                id, category, title, summary, handler_type, payload_json,
                channel_id, is_private, status, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                decision_id,
                category.strip(),
                title.strip(),
                summary.strip(),
                handler_type.strip(),
                json.dumps(payload),
                int(channel_id),
                priv_flag,
                now + float(ttl_seconds),
                now,
            ),
        )

    if post_to_discord:
        _post_decision_card_rest(decision_id, db_path=db_path)

    return {
        "ok": True,
        "decision_id": decision_id,
        "channel_id": int(channel_id),
        "expires_at": now + float(ttl_seconds),
    }


def format_decision_card_text(decision: sqlite3.Row | dict) -> str:
    """Format the Discord markdown text for a staged Decision Card."""
    status = decision["status"]
    cat = str(decision["category"]).upper()
    if status == "pending":
        exp_str = _now_pt_str(decision["expires_at"])
        return (
            f"### 🛡️ Decision Required [{cat}]: {decision['title']}\n"
            f"{decision['summary']}\n"
            f"- **ID:** `{decision['id']}` | **Handler:** `{decision['handler_type']}` | **Expires:** {exp_str}"
        )
    if status in ("approved", "edited_approved"):
        res_str = _now_pt_str(decision["resolved_at"] or time.time())
        label = "Approved & Executed (Edited)" if status == "edited_approved" else "Approved & Executed"
        receipt = f"\n- **Receipt:** {decision['receipt_text']}" if decision["receipt_text"] else ""
        return (
            f"### ✅ {label} [{cat}]: {decision['title']}\n"
            f"{decision['summary']}\n"
            f"- **ID:** `{decision['id']}` | **Resolved:** {res_str}{receipt}"
        )
    if status == "dismissed":
        res_str = _now_pt_str(decision["resolved_at"] or time.time())
        return (
            f"### 🛑 Dismissed [{cat}]: {decision['title']}\n"
            f"~{decision['summary']}~\n"
            f"- **ID:** `{decision['id']}` | **Dismissed:** {res_str}"
        )
    if status == "expired":
        return (
            f"### ⏱️ Expired [{cat}]: {decision['title']}\n"
            f"~{decision['summary']}~\n"
            f"- **ID:** `{decision['id']}` | **Status:** Expired without action"
        )
    return (
        f"### ❌ Execution Failed [{cat}]: {decision['title']}\n"
        f"{decision['summary']}\n"
        f"- **ID:** `{decision['id']}` | **Error:** `{decision['receipt_text'][:200]}`"
    )


def _post_decision_card_rest(decision_id: str, db_path: Path | str = FORGE_DB_PATH) -> Optional[int]:
    """Post a staged Decision Card with interactive buttons via Discord REST API v10."""
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (decision_id,)).fetchone()
        if not row:
            return None

    token = _get_discord_token()
    if not token:
        return None

    text = format_decision_card_text(row)
    components = [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 3, "label": "Approve", "custom_id": f"forge_approve:{decision_id}"},
                {"type": 2, "style": 2, "label": "Edit", "custom_id": f"forge_edit:{decision_id}"},
                {"type": 2, "style": 4, "label": "Dismiss", "custom_id": f"forge_dismiss:{decision_id}"},
            ],
        }
    ]
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{row['channel_id']}/messages"
    try:
        resp = requests.post(url, headers=headers, json={"content": text[:1990], "components": components}, timeout=10)
        if resp.status_code < 300:
            msg_id = int(resp.json()["id"])
            with get_db_connection(db_path) as conn:
                conn.execute("UPDATE forge_decisions SET message_id = ? WHERE id = ?", (msg_id, decision_id))
            return msg_id
    except Exception as e:
        print(f"[FORGE] Warning posting decision card {decision_id}: {e}")
    return None


def execute_decision(
    decision_id: str,
    user_id: int = OWNER_USER_ID,
    override_payload: Optional[dict] = None,
    db_path: Path | str = FORGE_DB_PATH,
) -> dict:
    """Deterministically execute a staged Decision Card payload in <1s with zero LLM tokens."""
    if int(user_id) != OWNER_USER_ID:
        return {"ok": False, "error": "Unauthorized user ID."}

    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (decision_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Decision {decision_id} not found."}
        if row["status"] != "pending":
            return {"ok": False, "error": f"Decision {decision_id} is already {row['status']}."}
        if time.time() > float(row["expires_at"]):
            conn.execute(
                "UPDATE forge_decisions SET status = 'expired', resolved_at = ? WHERE id = ?",
                (time.time(), decision_id),
            )
            return {"ok": False, "error": f"Decision {decision_id} has expired."}

        payload = override_payload if override_payload is not None else json.loads(row["payload_json"] or "{}")
        if override_payload is not None:
            conn.execute(
                "UPDATE forge_decisions SET payload_json = ? WHERE id = ?",
                (json.dumps(override_payload), decision_id),
            )

    handler = row["handler_type"]
    ok = False
    receipt = ""

    try:
        if handler == "shell":
            cmd = payload.get("command", "")
            cwd = payload.get("cwd", "/workspace")
            proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=60)
            ok = proc.returncode == 0
            out = (proc.stdout or proc.stderr or "").strip()
            receipt = f"Exit 0 (`{out[:120]}`)" if ok and out else ("Exit 0" if ok else f"Exit {proc.returncode}: {out[:160]}")

        elif handler == "send_mail":
            to_addr = payload["to"]
            subj = payload["subject"]
            body = payload["body"]
            cmd = [sys.executable, str(WORKSPACE_DIR / "tools" / "send_mail.py"), "--to", to_addr, "--subject", subj, "--body", body]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            ok = proc.returncode == 0
            receipt = f"Email dispatched to `{to_addr}`" if ok else f"send_mail failed: {proc.stderr[:160]}"

        elif handler == "docker_compose":
            service = payload["service"]
            action = payload.get("action", "up -d")
            compose_dir = payload.get("compose_dir", "/docker")
            cmd = f"docker compose {action} {service}"
            proc = subprocess.run(cmd, shell=True, cwd=compose_dir, capture_output=True, text=True, timeout=60)
            ok = proc.returncode == 0
            receipt = f"`{cmd}` succeeded" if ok else f"`{cmd}` failed: {proc.stderr[:160]}"

        elif handler == "gh_merge":
            pr_num = int(payload["pr_number"])
            repo_dir = payload.get("repo", "/workspace")
            from tools.repo_ops import merge_pr_and_cleanup
            res = merge_pr_and_cleanup(Path(repo_dir), pr_num)
            ok = bool(res.get("merged"))
            receipt = f"Merged PR #{pr_num} in `{Path(repo_dir).name}`"

        elif handler == "bridge_reload":
            reload_flag = DATA_DIR / "reload_bridge.flag"
            reload_flag.write_text(str(time.time()), encoding="utf-8")
            ok = True
            receipt = "Triggered in-place bridge reload"

        elif handler == "file_delete":
            cmd = payload.get("command", "")
            paths = payload.get("paths") or payload.get("files") or ([] if not payload.get("path") else [payload["path"]])
            if cmd:
                cwd = payload.get("cwd", "/workspace")
                proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=60)
                ok = proc.returncode == 0
                out = (proc.stdout or proc.stderr or "").strip()
                receipt = f"Deleted via `{cmd[:80]}`" if ok else f"Exit {proc.returncode}: {out[:160]}"
            elif paths and payload.get("auto_delete"):
                removed = []
                for p_str in paths:
                    p_obj = Path(p_str)
                    if p_obj.is_dir():
                        shutil.rmtree(p_obj)
                        removed.append(str(p_obj))
                    elif p_obj.exists() or p_obj.is_symlink():
                        p_obj.unlink()
                        removed.append(str(p_obj))
                ok = True
                receipt = f"Deleted {len(removed)} path(s): `{', '.join(removed)[:120]}`"
            else:
                ok = True
                target_label = ", ".join(str(x) for x in paths) if paths else str(payload.get("target", "target files"))
                receipt = f"Approved file deletion for `{target_label[:120]}`"

        elif handler == "forge_enqueue":
            plan_file = payload.get("plan_file") or payload.get("plan_path")
            plan_data = payload.get("plan")
            if plan_file and not plan_data:
                p_obj = Path(plan_file)
                if not p_obj.is_absolute():
                    p_obj = WORKSPACE_DIR / p_obj
                plan_data = json.loads(p_obj.read_text(encoding="utf-8"))
            if not isinstance(plan_data, dict):
                return {"ok": False, "error": "forge_enqueue payload requires 'plan_file' or 'plan' dict."}
            allow_overlap = bool(payload.get("allow_overlap", False))
            enq_res = enqueue_build(
                plan_data,
                db_path=db_path,
                post_widget=True,
                allow_overlap=allow_overlap,
                check_approval_gate=False,
            )
            ok = bool(enq_res.get("ok"))
            if ok:
                receipt = f"Enqueued FORGE build `{enq_res['build_id']}` ({enq_res.get('chunk_count', 1)} chunk(s)) in <#{FORGE_CHANNEL_ID}>"
            else:
                receipt = f"FORGE enqueue failed: {enq_res.get('error', 'unknown error')}"

        elif handler in ("noop", "approval"):
            ok = True
            receipt = str(payload.get("receipt") or payload.get("note") or "Approved by Ryan")

        else:
            return {"ok": False, "error": f"Unsupported handler_type: {handler}"}
    except Exception as e:
        ok = False
        receipt = str(e)

    final_status = ("edited_approved" if override_payload is not None else "approved") if ok else "failed"
    now = time.time()
    with get_db_connection(db_path) as conn:
        conn.execute(
            "UPDATE forge_decisions SET status = ?, resolved_at = ?, receipt_text = ? WHERE id = ?",
            (final_status, now, receipt, decision_id),
        )
        updated_row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (decision_id,)).fetchone()

    return {
        "ok": ok,
        "decision_id": decision_id,
        "handler_type": handler,
        "channel_id": int(row["channel_id"]),
        "status": final_status,
        "receipt": receipt,
        "formatted_card": format_decision_card_text(updated_row) if updated_row else "",
    }


def dismiss_decision(
    decision_id: str,
    user_id: int = OWNER_USER_ID,
    db_path: Path | str = FORGE_DB_PATH,
) -> dict:
    """Dismiss a pending Decision Card without executing its payload."""
    if int(user_id) != OWNER_USER_ID:
        return {"ok": False, "error": "Unauthorized user ID."}
    now = time.time()
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (decision_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Decision not found."}
        conn.execute(
            "UPDATE forge_decisions SET status = 'dismissed', resolved_at = ?, receipt_text = 'Dismissed by user' WHERE id = ?",
            (now, decision_id),
        )
        updated_row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (decision_id,)).fetchone()

    return {
        "ok": True,
        "decision_id": decision_id,
        "status": "dismissed",
        "formatted_card": format_decision_card_text(updated_row) if updated_row else "",
    }


def expire_stale_decisions(db_path: Path | str = FORGE_DB_PATH) -> list[str]:
    """Expire pending Decision Cards whose TTL has passed."""
    now = time.time()
    expired_ids = []
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM forge_decisions WHERE status = 'pending' AND expires_at < ?",
            (now,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE forge_decisions SET status = 'expired', resolved_at = ? WHERE id = ?",
                (now, r["id"]),
            )
            expired_ids.append(r["id"])
    return expired_ids


class ForgeDecisionEditModal(discord.ui.Modal):
    """Native Discord Modal allowing Ryan to edit staged action parameters before execution."""

    def __init__(self, decision_id: str, current_payload: dict, db_path: Path | str = FORGE_DB_PATH):
        super().__init__(title=f"Edit Staged Action ({decision_id[:12]})")
        self.decision_id = decision_id
        self.db_path = db_path
        self.payload_input = discord.ui.TextInput(
            label="Action Payload (JSON)",
            style=discord.TextStyle.paragraph,
            default=json.dumps(current_payload, indent=2)[:3900],
            required=True,
        )
        self.add_item(self.payload_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            new_payload = json.loads(self.payload_input.value)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Invalid JSON in edited payload: `{e}`", ephemeral=True)
            return

        res = execute_decision(
            self.decision_id,
            user_id=interaction.user.id,
            override_payload=new_payload,
            db_path=self.db_path,
        )
        if interaction.message and res.get("formatted_card"):
            try:
                view = discord.ui.View.from_message(interaction.message)
                for item in view.children:
                    item.disabled = True
                await interaction.message.edit(content=res["formatted_card"], view=view)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 6. Global `/queue` Digest & Active Build Inspection
# ---------------------------------------------------------------------------

def has_active_forge_builds(db_path: Path | str = FORGE_DB_PATH) -> list[str]:
    """Return list of active build IDs currently queued or running in FORGE."""
    try:
        with get_db_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT id, title FROM forge_builds WHERE status IN ('queued', 'running')"
            ).fetchall()
            return [f"{r['id']} ({r['title']})" for r in rows]
    except Exception:
        return []


def format_queue_digest(
    request_channel_id: Optional[int] = None,
    db_path: Path | str = FORGE_DB_PATH,
) -> str:
    """Render the unified /queue summary covering #forge builds, background jobs, and pending decisions."""
    is_private_channel = (request_channel_id is not None and int(request_channel_id) == ZERO_CHAT_CHANNEL_ID)
    expire_stale_decisions(db_path=db_path)

    builds = _query_unified_rows(
        "SELECT * FROM forge_builds WHERE status IN ('queued', 'running', 'awaiting_approval')",
        db_path=db_path,
        sort_key="created_at",
        reverse=True,
        limit=10,
    )
    recent_builds = _query_unified_rows(
        "SELECT * FROM forge_builds WHERE status IN ('completed', 'failed')",
        db_path=db_path,
        sort_key="updated_at",
        reverse=True,
        limit=3,
    )
    jobs = _query_unified_rows(
        "SELECT * FROM forge_jobs WHERE status IN ('starting', 'running')",
        db_path=db_path,
        sort_key="start_time",
        reverse=True,
        limit=10,
    )
    if is_private_channel:
        decisions = _query_unified_rows(
            "SELECT * FROM forge_decisions WHERE status = 'pending'",
            db_path=db_path,
            sort_key="created_at",
            reverse=False,
        )
    else:
        decisions = _query_unified_rows(
            "SELECT * FROM forge_decisions WHERE status = 'pending' AND is_private = 0",
            db_path=db_path,
            sort_key="created_at",
            reverse=False,
        )

    lines = [f"### 🛠️ FORGE Queue & Decisions Rollup (<#{FORGE_CHANNEL_ID}>)"]

    # 1. Active Builds
    lines.append("\n**Active FORGE Builds:**")
    if not builds:
        lines.append("- *No active multi-chunk builds in queue.*")
    else:
        for b in builds:
            lines.append(f"- 🔨 **`{b['id']}` — {b['title']}** (`{b['status'].upper()}` | Origin: <#{b['origin_thread_id'] or b['origin_channel_id']}>)")

    # 2. Active Background Jobs
    lines.append("\n**Active Background Tasks:**")
    if not jobs:
        lines.append("- *No detached background tasks currently running.*")
    else:
        for j in jobs:
            elapsed = int(time.time() - float(j["start_time"]))
            lines.append(f"- ⚙️ **`{j['id']}` — {j['name']}** (`{j['status']}`, {elapsed}s elapsed)")

    # 3. Pending Decision Cards
    lines.append("\n**Pending Action Approvals (Decisions):**")
    if not decisions:
        lines.append("- *Zero pending approval cards.*")
    else:
        for d in decisions:
            lines.append(
                f"- 🛡️ **`{d['id']}` [{d['category'].upper()}] {d['title']}** in <#{d['channel_id']}> — {d['summary'][:100]}"
            )

    # 4. Recent Completed Builds
    if recent_builds:
        lines.append("\n**Recent Completed Builds:**")
        for rb in recent_builds:
            icon = "✅" if rb["status"] == "completed" else "❌"
            lines.append(f"- {icon} `{rb['id']}`: {rb['title']} ({_now_pt_str(rb['updated_at'])})")

    return "\n".join(lines)


def format_forge_channel_header(db_path: Path | str = FORGE_DB_PATH) -> str:
    """Render the live #forge top header message with backronym and active queues."""
    builds = _query_unified_rows(
        "SELECT * FROM forge_builds WHERE status IN ('queued', 'running', 'awaiting_approval')",
        db_path=db_path,
        sort_key="created_at",
        reverse=True,
        limit=10,
    )
    jobs = _query_unified_rows(
        "SELECT * FROM forge_jobs WHERE status IN ('starting', 'running')",
        db_path=db_path,
        sort_key="start_time",
        reverse=True,
        limit=10,
    )
    decisions = _query_unified_rows(
        "SELECT * FROM forge_decisions WHERE status = 'pending' AND is_private = 0",
        db_path=db_path,
        sort_key="created_at",
        reverse=False,
    )

    lines = [
        "### ⚒️ VULCAN (**V**erified **U**nattended **L**abor & **C**ode **A**nvil **N**ode)\n*FORGE Build Floor — **F**ederated **O**rchestration, **R**untime & **G**ated **E**xecution*",
        "",
        "**Active FORGE Builds:**",
    ]
    if not builds:
        lines.append("- *No active multi-chunk builds in queue.*")
    else:
        for b in builds:
            lines.append(
                f"- 🔨 **`{b['id']}` — {b['title']}** (`{b['status'].upper()}` · <#{b['origin_thread_id'] or b['origin_channel_id']}>)"
            )

    lines.append("\n**Active Background Tasks:**")
    if not jobs:
        lines.append("- *No detached background tasks currently running.*")
    else:
        for j in jobs:
            ch_ref = _format_channel_mention(j["channel"])
            lines.append(f"- ⚙️ **`{j['id']}` — {j['name']}** (`{j['status']}` · {ch_ref})")

    lines.append("\n**Pending Action Approvals (Decisions):**")
    if not decisions:
        lines.append("- *Zero pending approval cards.*")
    else:
        for d in decisions:
            lines.append(
                f"- 🛡️ **`{d['id']}` [{d['category'].upper()}] {d['title']}** in <#{d['channel_id']}> — {d['summary'][:100]}"
            )

    return "\n".join(lines)


def _merge_remote_active_embeds_into_header(
    content: str,
    forge_channel_id: int,
    token: str,
) -> str:
    """Supplement header content with any active build/job embeds currently live in #forge from an unmounted peer container."""
    headers = {"Authorization": f"Bot {token}"}
    try:
        resp = requests.get(
            f"https://discord.com/api/v10/channels/{forge_channel_id}/messages?limit=30",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200 or not isinstance(resp.json(), list):
            return content
        messages = resp.json()
    except Exception:
        return content

    remote_build_lines: list[str] = []
    remote_job_lines: list[str] = []
    seen_ids: set[str] = set()

    for msg in messages:
        try:
            if int(msg.get("id", 0)) == FORGE_HEADER_MESSAGE_ID:
                continue
            embeds = msg.get("embeds") or []
            if not embeds:
                continue
            emb = embeds[0]
            title = str(emb.get("title") or "").strip()
            desc = str(emb.get("description") or "").strip()
            footer_text = str((emb.get("footer") or {}).get("text") or "")
            id_match = re.search(r"(?:Build\s+ID:\s*|Job\s+ID:\s*|\b)((?:fg|dt)-[A-Za-z0-9_-]+)", footer_text)
            if not id_match:
                continue
            entity_id = id_match.group(1)
            if entity_id in seen_ids or f"`{entity_id}`" in content:
                seen_ids.add(entity_id)
                continue
            seen_ids.add(entity_id)

            first_line = desc.splitlines()[0] if desc else ""
            ch_match = re.search(r"(<#\d+>)", first_line)
            ch_ref = ch_match.group(1) if ch_match else "`#forge`"
            clean_title = re.sub(r"^[^\w\s]+\s*", "", title).strip()

            if entity_id.startswith("fg-") and desc.startswith(("**RUNNING**", "**QUEUED**", "**AWAITING APPROVAL**")):
                st_label = first_line.split("**")[1] if "**" in first_line else "RUNNING"
                remote_build_lines.append(f"- 🔨 **`{entity_id}` — {clean_title}** (`{st_label}` · {ch_ref})")
            elif entity_id.startswith("dt-") and desc.startswith("**RUNNING**"):
                remote_job_lines.append(f"- ⚙️ **`{entity_id}` — {clean_title}** (`running` · {ch_ref})")
        except Exception:
            continue

    if remote_build_lines:
        if "- *No active multi-chunk builds in queue.*" in content:
            content = content.replace(
                "- *No active multi-chunk builds in queue.*",
                "\n".join(remote_build_lines),
            )
        else:
            content = content.replace(
                "**Active FORGE Builds:**\n",
                "**Active FORGE Builds:**\n" + "\n".join(remote_build_lines) + "\n",
                1,
            )

    if remote_job_lines:
        if "- *No detached background tasks currently running.*" in content:
            content = content.replace(
                "- *No detached background tasks currently running.*",
                "\n".join(remote_job_lines),
            )
        else:
            content = content.replace(
                "**Active Background Tasks:**\n",
                "**Active Background Tasks:**\n" + "\n".join(remote_job_lines) + "\n",
                1,
            )

    return content


def sync_forge_channel_header(
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
    header_message_id: int = FORGE_HEADER_MESSAGE_ID,
    force: bool = False,
) -> bool:
    """Update the #forge header message in place whenever active builds, tasks, or decisions change."""
    if not force and Path(db_path).resolve() != FORGE_DB_PATH.resolve():
        return False

    if not force:
        try:
            prune_completed_build_widgets(db_path=db_path, forge_channel_id=forge_channel_id)
        except Exception:
            pass

    token = _get_discord_token(for_forge_channel=True)
    content = format_forge_channel_header(db_path=db_path)
    if not force and token and _is_prod_forge_env(db_path):
        content = _merge_remote_active_embeds_into_header(content, forge_channel_id, token)

    cache_file = DATA_DIR / ".forge_header_cache.txt"
    if not force and cache_file.exists():
        try:
            if cache_file.read_text(encoding="utf-8") == content:
                return False
        except Exception:
            pass

    wh = _get_vulcan_webhook(db_path=db_path, forge_channel_id=forge_channel_id)
    if not token and not wh:
        return False

    rendered_payload = f"{content}\n\n*Last synced: {_now_pt_str()}*"
    try:
        ok_patch, _ = _patch_forge_channel_message(
            int(header_message_id),
            payload={"content": rendered_payload[:1990], "embeds": []},
            forge_channel_id=forge_channel_id,
            token=token,
            db_path=db_path,
        )
        if ok_patch:
            cache_file.write_text(content, encoding="utf-8")
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# 7. CLI Entrypoint (`python3 /workspace/tools/forge.py ...`)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="FORGE — Federated Orchestration, Runtime & Gated Execution CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue", help="Validate and enqueue a ForgePlan JSON file")
    p_enq.add_argument("plan_file", help="Path to ForgePlan JSON file")
    p_enq.add_argument("--no-widget", action="store_true", help="Skip posting initial #forge Discord widget")
    p_enq.add_argument("--allow-overlap", action="store_true", help="Allow enqueueing even if target_files overlap an active build")

    p_st = sub.add_parser("status", help="Show FORGE queue digest or specific build status")
    p_st.add_argument("--build-id", default="", help="Specific build ID")
    p_st.add_argument("--channel-id", type=int, default=ZERO_OPS_CHANNEL_ID)

    p_dec = sub.add_parser("stage-decision", help="Stage an inline Decision Card")
    p_dec.add_argument("--category", required=True)
    p_dec.add_argument("--title", required=True)
    p_dec.add_argument("--summary", required=True)
    p_dec.add_argument("--handler", required=True)
    p_dec.add_argument("--payload", required=True, help="JSON string payload")
    p_dec.add_argument("--channel-id", type=int, default=ZERO_CHAT_CHANNEL_ID)
    p_dec.add_argument("--ttl", type=float, default=7200.0)
    p_dec.add_argument("--post", action="store_true", help="Post immediately via Discord REST API")

    sub.add_parser("reconcile", help="Run boot reconciliation and worktree cleanup")

    # Native `forge.py job <action>` subcommands (absorbs detached_runner.py)
    p_job = sub.add_parser("job", help="Manage detached background shell jobs in forge_jobs")
    job_sub = p_job.add_subparsers(dest="job_action", required=True)

    j_start = job_sub.add_parser("start", help="Spawn a detached background job")
    j_start.add_argument("--command", "-c", required=True, help="Shell command to execute")
    j_start.add_argument("--channel", default="", help="Originating Discord channel (defaults to DISCORD_CHANNEL_ID or zero-chat)")
    j_start.add_argument("--name", "-n", default="", help="Human-readable name for the job")
    j_start.add_argument("--timeout", "-t", type=int, default=3600, help="Timeout in seconds")
    j_start.add_argument("--cwd", default="/workspace", help="Working directory")
    j_start.add_argument("--notify-on-success", action="store_true", default=True)
    j_start.add_argument("--silent-on-success", dest="notify_on_success", action="store_false")
    j_start.add_argument("--no-synthesize", dest="synthesize", action="store_false", default=True)
    j_start.add_argument("--allow-duplicate", action="store_true", default=False)

    j_status = job_sub.add_parser("status", help="Check status of a detached job")
    j_status.add_argument("task_id", help="Job ID")

    j_list = job_sub.add_parser("list", help="List recent detached jobs")
    j_list.add_argument("--limit", type=int, default=10)

    j_cancel = job_sub.add_parser("cancel", help="Cancel a running detached job")
    j_cancel.add_argument("task_id", help="Job ID")

    j_logs = job_sub.add_parser("logs", help="Tail logs for a detached job")
    j_logs.add_argument("task_id", help="Job ID")
    j_logs.add_argument("--lines", "-n", type=int, default=30)

    # Internal workers
    p_bw = sub.add_parser("_build_worker", help=argparse.SUPPRESS)
    p_bw.add_argument("build_id", help="Build ID")
    p_bw.add_argument("--db-path", default=str(FORGE_DB_PATH))

    p_jw = sub.add_parser("_job_worker", help=argparse.SUPPRESS)
    p_jw.add_argument("task_id", help="Task ID")
    p_jw.add_argument("--db-path", default=str(FORGE_DB_PATH))

    args = parser.parse_args()
    if args.cmd == "enqueue":
        try:
            plan_data = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
            res = enqueue_build(
                plan_data,
                post_widget=not args.no_widget,
                allow_overlap=args.allow_overlap,
                check_approval_gate=True,
            )
            print(json.dumps(res, indent=2))
        except Exception:
            try:
                bs_path = Path("/tmp/batch_guard_state.json")
                if bs_path.exists():
                    bs_data = json.loads(bs_path.read_text(encoding="utf-8"))
                    if isinstance(bs_data, dict) and bs_data.get("offload_dispatched"):
                        bs_data["offload_dispatched"] = False
                        bs_data["offload_kind"] = ""
                        bs_path.write_text(json.dumps(bs_data), encoding="utf-8")
            except Exception:
                pass
            raise
    elif args.cmd == "status":
        if args.build_id:
            print(render_build_widget_text(args.build_id))
        else:
            print(format_queue_digest(request_channel_id=args.channel_id))
    elif args.cmd == "stage-decision":
        res = stage_decision(
            category=args.category,
            title=args.title,
            summary=args.summary,
            handler_type=args.handler,
            payload=json.loads(args.payload),
            channel_id=args.channel_id,
            ttl_seconds=args.ttl,
            post_to_discord=args.post,
        )
        print(json.dumps(res, indent=2))
    elif args.cmd == "reconcile":
        print(json.dumps(reconcile_forge_on_boot(), indent=2))
    elif args.cmd == "_build_worker":
        sup = ForgeSupervisor(db_path=args.db_path)
        sup.run_build_pipeline(args.build_id)
    elif args.cmd == "_job_worker":
        _run_job_worker(args.task_id, db_path=args.db_path)
    elif args.cmd == "job":
        if args.job_action == "start":
            res = start_job(
                command=args.command,
                channel=args.channel,
                name=args.name,
                timeout=args.timeout,
                cwd=args.cwd,
                notify_on_success=args.notify_on_success,
                synthesize=args.synthesize,
                allow_duplicate=args.allow_duplicate,
            )
            print(json.dumps(res, indent=2))
        elif args.job_action == "status":
            print(json.dumps(get_job_status(args.task_id), indent=2))
        elif args.job_action == "list":
            print(json.dumps(list_jobs(limit=args.limit), indent=2))
        elif args.job_action == "cancel":
            print(json.dumps(cancel_job(args.task_id), indent=2))
        elif args.job_action == "logs":
            meta = _read_job_meta(args.task_id)
            if not meta:
                print(f"Error: Task '{args.task_id}' not found.", file=sys.stderr)
                sys.exit(1)
            log_path = Path(meta["log_file"])
            if not log_path.exists():
                print("Log file empty or not yet created.")
                sys.exit(0)
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                tail = lines[-args.lines:] if len(lines) > args.lines else lines
                sys.stdout.write("".join(tail))


if __name__ == "__main__":
    main()
