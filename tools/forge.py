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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
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
FORGE_HEADER_MESSAGE_ID = 1562903902188208212
ZERO_CHAT_CHANNEL_ID = 1542081375287640084
ZERO_OPS_CHANNEL_ID = 1544953279664889888
BASEBALL_CHANNEL_ID = 1548196929308065893
OWNER_USER_ID = int(os.getenv("DISCORD_OWNER_ID", "179407724335988736"))

DEFAULT_WORKER_MODEL = "gemini-3.8-flash-high"
ESCALATION_WORKER_MODEL = "gemini-4-argon-medium"
DEFAULT_LEASE_TTL_SECONDS = 300.0


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
                from tools.outbox import resolve_channel_id
                res_id = resolve_channel_id(raw_ch)
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

    When `for_forge_channel=True` inside `discord-ivy-agent`, prefers Zero's bot token
    from `/opt/zero/.env` so Ivy can post/edit #forge build cards and the #forge header
    message (which is authored by Zero and restricted by #forge channel overwrites).
    """
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


# Initialize schema on import
init_forge_db()


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


def check_target_file_overlap(
    plan: dict,
    db_path: Path | str = FORGE_DB_PATH,
) -> list[dict]:
    """Detect if any target_files in `plan` overlap with currently active (queued/running) FORGE chunks."""
    default_repo = str(Path(str(plan.get("repo") or "/workspace").strip()).resolve())
    planned_pairs: set[tuple[str, str]] = set()
    for ch in plan.get("chunks", []):
        ch_repo = str(Path(str(ch.get("repo") or default_repo).strip()).resolve())
        for tf in ch.get("target_files", []):
            norm_tf = str(tf).strip().lstrip("./")
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
            norm_tf = str(tf).strip().lstrip("./")
            if (active_repo, norm_tf) in planned_pairs:
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

    fact_sheet = plan.get("context_fact_sheet")
    if not isinstance(fact_sheet, list) or not fact_sheet or not all(isinstance(x, str) and x.strip() for x in fact_sheet):
        return False, "Plan must include a non-empty 'context_fact_sheet' list of architectural invariants."

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
        t_files = ch.get("target_files")
        if not isinstance(t_files, list) or not t_files:
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

    b_emoji, b_label, color = status_meta.get(build["status"], ("⚙️", build["status"].upper(), 0x3B82F6))
    origin_ref = f"<#{build['origin_thread_id'] or build['origin_channel_id']}>"
    repo_name = Path(build["repo"]).name or "Zero-Agent"
    if repo_name == "workspace":
        is_ivy_origin = int(build["origin_channel_id"]) == BASEBALL_CHANNEL_ID or os.getenv("DISCORD_CHANNEL_ID", "").strip() == str(BASEBALL_CHANNEL_ID)
        repo_name = "Ivy-Agent" if is_ivy_origin else "Zero-Agent"

    total = len(chunks)
    merged_count = sum(1 for c in chunks if c["status"] == "merged")
    header_line = f"**{b_label}** · `{repo_name}` · {origin_ref} · **{merged_count}/{total}** chunks"

    display_chunks = list(chunks)
    chunk_lines: list[str] = []
    if len(display_chunks) > 8:
        merged_indices = [i for i, c in enumerate(display_chunks) if c["status"] == "merged"]
        if len(merged_indices) > 3:
            hidden_count = len(merged_indices) - 2
            chunk_lines.append(f"✅ *+{hidden_count} earlier chunk(s) merged*")
            keep_set = set(merged_indices[-2:]) | {i for i, c in enumerate(display_chunks) if c["status"] != "merged"}
            display_chunks = [c for i, c in enumerate(display_chunks) if i in keep_set]

    for c in display_chunks:
        c_emoji = chunk_badge_map.get(c["status"], "•")
        idx_num = c["chunk_index"] + 1
        pr_suffix = f" (PR #{c['pr_number']})" if c["pr_number"] else ""
        compact_diff = _compact_diff_stat(c["diff_stat"])
        diff_suffix = f" (`{compact_diff}`)" if compact_diff else ""
        chunk_lines.append(f"{c_emoji} **{idx_num}.** {c['title']}{pr_suffix}{diff_suffix}")
        if c["status"] == "failed" and c["last_error"]:
            err_snip = c["last_error"].strip().splitlines()[-1][:110]
            chunk_lines.append(f"  ⚠️ `{err_snip}`")

    desc = header_line + ("\n\n" + "\n".join(chunk_lines) if chunk_lines else "")
    footer_suffix = " · Auto-clears in 1h" if build["status"] == "completed" else ""
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
    merged_count = sum(1 for c in chunks if c["status"] == "merged")
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
    """Post or edit the single live Discord Embed card for `build_id` inside #forge via REST API v10."""
    embed = render_build_widget_embed(build_id, db_path=db_path)
    token = _get_discord_token(for_forge_channel=True)
    if not token:
        return None

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT forge_message_id FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return None
        msg_id = row["forge_message_id"]

    payload = {"content": "", "embeds": [embed]}
    try:
        if msg_id:
            url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages/{msg_id}"
            resp = requests.patch(url, headers=headers, json=payload, timeout=10)
            if resp.status_code < 300:
                return int(msg_id)

        url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages"
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code < 300:
            new_msg_id = int(resp.json()["id"])
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE forge_builds SET forge_message_id = ?, updated_at = ? WHERE id = ?",
                    (new_msg_id, time.time(), build_id),
                )
            return new_msg_id
    except Exception as e:
        print(f"[FORGE] Warning updating #forge widget for {build_id}: {e}")
    return None


def prune_completed_build_widgets(
    ttl_seconds: float = COMPLETED_WIDGET_TTL_SECONDS,
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
    delete_remote: bool = True,
) -> list[str]:
    """Delete Discord embed cards in #forge for completed builds older than `ttl_seconds` (default 1h)."""
    cutoff = time.time() - float(ttl_seconds)
    pruned_ids: list[str] = []
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, forge_message_id FROM forge_builds
            WHERE status = 'completed'
              AND forge_message_id IS NOT NULL
              AND updated_at <= ?
            """,
            (cutoff,),
        ).fetchall()

    if not rows:
        return pruned_ids

    token = _get_discord_token(for_forge_channel=True) if delete_remote else None

    headers = {"Authorization": f"Bot {token}"} if token else {}
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
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE forge_builds SET forge_message_id = NULL WHERE id = ?",
                    (b_id,),
                )
            pruned_ids.append(b_id)

    return pruned_ids


def enqueue_build(
    plan: dict,
    db_path: Path | str = FORGE_DB_PATH,
    post_widget: bool = True,
    allow_overlap: bool = False,
) -> dict:
    """Validate and enqueue a multi-chunk FORGE build plan with pre-enqueue target-file overlap deduplication."""
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
    slug = re.sub(r"[^a-z0-9]+", "-", plan["title"].lower()).strip("-")[:18] or "build"
    build_id = plan.get("id") or f"fg-{int(now)}-{slug}"
    touches_core = 1 if _detect_touches_core_bridge(plan) else 0
    default_repo = str(plan.get("repo") or "/workspace").strip()
    objective = str(plan.get("objective") or plan["title"]).strip()
    build_shared_ctx = plan.get("shared_context") or []

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
                json.dumps(plan["context_fact_sheet"]),
                json.dumps(build_shared_ctx),
                now,
                now,
            ),
        )

        for idx, ch in enumerate(plan["chunks"]):
            chunk_id = f"{build_id}-c{idx}"
            ch_repo = str(ch.get("repo") or default_repo).strip()
            t_files = ch.get("target_files", [])
            v_cmd = str(ch.get("verification_cmd") or "").strip()
            conn.execute(
                """
                INSERT INTO forge_chunks (
                    id, build_id, chunk_index, title, repo, worker_model,
                    target_files_json, target_symbols_json, shared_context_json,
                    prohibited_files_json, instructions, verification_cmd, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    chunk_id,
                    build_id,
                    idx,
                    ch["title"].strip(),
                    ch_repo,
                    ch.get("worker_model") or DEFAULT_WORKER_MODEL,
                    json.dumps(t_files),
                    json.dumps(ch.get("target_symbols", [])),
                    json.dumps(ch.get("shared_context", [])),
                    json.dumps(ch.get("prohibited_files", [])),
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
    fact_sheet = json.loads(build["fact_sheet_json"] or "[]")
    build_shared_ctx = json.loads(build["shared_context_json"] or "[]") if "shared_context_json" in build.keys() else []
    target_files = json.loads(target_chunk["target_files_json"] or "[]")
    target_symbols = json.loads(target_chunk["target_symbols_json"] or "[]") if "target_symbols_json" in target_chunk.keys() else []
    chunk_shared_ctx = json.loads(target_chunk["shared_context_json"] or "[]") if "shared_context_json" in target_chunk.keys() else []
    prohibited_files = json.loads(target_chunk["prohibited_files_json"] or "[]")

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
    v_proc = subprocess.run(
        effective_cmd,
        shell=True,
        cwd=str(wt),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if v_proc.returncode != 0:
        combined = f"{v_proc.stdout}\n{v_proc.stderr}".strip()
        return False, "", f"Verification command failed (exit {v_proc.returncode}):\n{combined[-1500:]}"

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


def reconcile_forge_on_boot(db_path: Path | str = FORGE_DB_PATH) -> dict:
    """Clean up stale /tmp/forge-wt-* worktrees and reset interrupted running chunks on startup."""
    cleaned_wts = []
    for wt_path in glob.glob("/tmp/forge-wt-*"):
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


class ForgeSupervisor:
    """Asynchronous background supervisor for FORGE build chunks and Decision Card expiry."""

    def __init__(self, db_path: Path | str = FORGE_DB_PATH):
        self.db_path = Path(db_path)
        self._active_build_proc: Optional[subprocess.Popen] = None

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

        spawned_build = self.maybe_spawn_next_build_worker()
        return {
            "expired_decisions": expired,
            "pruned_widgets": pruned_widgets,
            "dispatched_sdk_tasks": len(dispatched_sdk),
            "spawned_build": spawned_build,
        }

    def maybe_spawn_next_build_worker(self) -> Optional[str]:
        """If a build for this container is queued in SQLite and no build worker is currently running, spawn a detached worker."""
        if self._active_build_proc is not None and self._active_build_proc.poll() is None:
            return None

        in_ivy = is_ivy_container()
        lock_name = ".forge_build_worker_ivy.lock" if in_ivy else ".forge_build_worker.lock"
        lock_path = self.db_path.parent / lock_name
        try:
            with open(lock_path, "a+", encoding="utf-8") as lf:
                try:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    return None
                else:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

        with get_db_connection(self.db_path) as conn:
            if in_ivy:
                row = conn.execute(
                    "SELECT id FROM forge_builds WHERE status IN ('queued', 'running') AND origin_channel_id = ? ORDER BY created_at ASC LIMIT 1",
                    (BASEBALL_CHANNEL_ID,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM forge_builds WHERE status IN ('queued', 'running') AND origin_channel_id != ? ORDER BY created_at ASC LIMIT 1",
                    (BASEBALL_CHANNEL_ID,),
                ).fetchone()
            if not row:
                return None
            build_id = row["id"]

        log_path = FORGE_LOGS_DIR / f"build-{build_id}.log"
        lf_out = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_build_worker", build_id, "--db-path", str(self.db_path)],
            cwd=str(WORKSPACE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=lf_out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._active_build_proc = proc
        return build_id

    def run_build_pipeline(self, build_id: str) -> bool:
        """Execute all pending chunks for `build_id` sequentially under an exclusive file lock."""
        in_ivy = is_ivy_container()
        lock_name = ".forge_build_worker_ivy.lock" if in_ivy else ".forge_build_worker.lock"
        lock_path = self.db_path.parent / lock_name
        lock_fh = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            lock_fh.close()
            return False

        try:
            while True:
                with get_db_connection(self.db_path) as conn:
                    build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
                    if not build or build["status"] in ("cancelled", "failed", "completed"):
                        break
                    next_chunk = conn.execute(
                        "SELECT * FROM forge_chunks WHERE build_id = ? AND status != 'merged' ORDER BY chunk_index ASC LIMIT 1",
                        (build_id,),
                    ).fetchone()
                    if not next_chunk:
                        conn.execute(
                            "UPDATE forge_builds SET status = 'completed', updated_at = ? WHERE id = ?",
                            (time.time(), build_id),
                        )
                        post_or_update_build_widget(build_id, db_path=self.db_path)
                        dispatch_build_completion_to_origin(build_id, db_path=self.db_path)
                        return True

                ok = self.execute_chunk_in_worktree(build_id, next_chunk)
                if not ok:
                    return False
            return True
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lock_fh.close()

    def execute_chunk_in_worktree(self, build_id: str, chunk_row: sqlite3.Row) -> bool:
        """Execute a single FORGE chunk inside an isolated git worktree with Maker-Checker verification."""
        chunk_id = chunk_row["id"]
        chunk_idx = int(chunk_row["chunk_index"])
        with get_db_connection(self.db_path) as conn:
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

        # Ensure base ref exists and carries prior chunks forward
        subprocess.run(["git", "fetch", "origin", default_branch], cwd=str(repo_dir), capture_output=True, timeout=30)
        if chunk_idx == 0:
            chk_ref = subprocess.run(["git", "rev-parse", "--verify", f"origin/{default_branch}"], cwd=str(repo_dir), capture_output=True)
            start_ref = f"origin/{default_branch}" if chk_ref.returncode == 0 else "HEAD"
            subprocess.run(["git", "branch", "-f", staging_ref, start_ref], cwd=str(repo_dir), capture_output=True, timeout=10)
        base_ref = staging_ref

        shutil.rmtree(wt_dir, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)
        wt_res = subprocess.run(
            ["git", "worktree", "add", "-B", branch_name, str(wt_dir), base_ref],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if wt_res.returncode != 0:
            subprocess.run(
                ["git", "worktree", "add", "-B", branch_name, str(wt_dir), "HEAD"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )

        prohibited = json.loads(chunk_row["prohibited_files_json"] or "[]")
        target_files = json.loads(chunk_row["target_files_json"] or "[]")
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            model = ESCALATION_WORKER_MODEL if attempt == max_attempts else (chunk_row["worker_model"] or DEFAULT_WORKER_MODEL)
            with get_db_connection(self.db_path) as conn:
                conn.execute(
                    "UPDATE forge_chunks SET attempt_count = ?, worker_model = ?, status = 'running' WHERE id = ?",
                    (attempt, model, chunk_id),
                )
            post_or_update_build_widget(build_id, db_path=self.db_path)

            prompt = format_chunk_prompt(build_id, chunk_idx, db_path=self.db_path, worktree_dir=str(wt_dir))
            agy_cmd = [
                "/usr/local/bin/agy",
                f"--add-dir={wt_dir}",
                f"-p={prompt}",
                "--output-format=stream-json",
                "--dangerously-skip-permissions",
                "--print-timeout=4h",
                f"--model={model}",
            ]
            try:
                agy_res = subprocess.run(agy_cmd, cwd=str(wt_dir), capture_output=True, text=True, timeout=1200)
                if agy_res.returncode != 0:
                    err_msg = (agy_res.stderr or agy_res.stdout or f"agy exited {agy_res.returncode}").strip()
                    with get_db_connection(self.db_path) as conn:
                        conn.execute("UPDATE forge_chunks SET last_error = ? WHERE id = ?", (err_msg[-1500:], chunk_id))
            except Exception as e:
                with get_db_connection(self.db_path) as conn:
                    conn.execute("UPDATE forge_chunks SET last_error = ? WHERE id = ?", (str(e), chunk_id))
                continue

            with get_db_connection(self.db_path) as conn:
                conn.execute("UPDATE forge_chunks SET status = 'verifying' WHERE id = ?", (chunk_id,))
            post_or_update_build_widget(build_id, db_path=self.db_path)

            passed, diff_stat, err_out = verify_chunk_worktree(
                wt_dir,
                chunk_row["verification_cmd"],
                prohibited_files=prohibited,
                target_files=target_files,
                require_changes=True,
            )
            if passed:
                # Collect short diff stat against base_ref before committing
                diff_proc = subprocess.run(
                    ["git", "diff", "--shortstat", base_ref],
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
                        with get_db_connection(self.db_path) as conn:
                            conn.execute(
                                "UPDATE forge_chunks SET last_error = ? WHERE id = ?",
                                (f"git commit / pre-commit hook failed (exit {commit_proc.returncode}):\n{c_err[-1400:]}", chunk_id),
                            )
                        continue
                except Exception as ce:
                    with get_db_connection(self.db_path) as conn:
                        conn.execute(
                            "UPDATE forge_chunks SET last_error = ? WHERE id = ?",
                            (f"git commit failed/timed out: {ce}", chunk_id),
                        )
                    continue
                # Fast-forward staging branch so subsequent chunks inherit this chunk's commits
                subprocess.run(
                    ["git", "branch", "-f", base_ref, branch_name],
                    cwd=str(repo_dir),
                    capture_output=True,
                    timeout=10,
                )
                shutil.rmtree(wt_dir, ignore_errors=True)
                subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)

                with get_db_connection(self.db_path) as conn:
                    conn.execute(
                        "UPDATE forge_chunks SET status = 'merged', branch_name = ?, diff_stat = ?, completed_at = ?, last_error = '' WHERE id = ?",
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

                # If final chunk completed and not touching core bridge, sync staging branch into local default branch
                if remaining == 0 and not build["touches_core_bridge"]:
                    try:
                        subprocess.run(["git", "merge", "--ff-only", base_ref], cwd=str(repo_dir), capture_output=True, timeout=20)
                    except Exception as e:
                        print(f"[FORGE] Warning: git merge --ff-only {base_ref} failed/timed out: {e}", file=sys.stderr)
                    try:
                        subprocess.run(["git", "push", "origin", default_branch], cwd=str(repo_dir), capture_output=True, timeout=60)
                    except Exception as e:
                        print(f"[FORGE] Warning: git push origin {default_branch} failed/timed out: {e}", file=sys.stderr)

                post_or_update_build_widget(build_id, db_path=self.db_path)
                sync_forge_channel_header(db_path=self.db_path)
                if remaining == 0:
                    dispatch_build_completion_to_origin(build_id, db_path=self.db_path)
                return True
            else:
                with get_db_connection(self.db_path) as conn:
                    conn.execute("UPDATE forge_chunks SET last_error = ? WHERE id = ?", (err_out, chunk_id))

        # Exhausted all attempts
        shutil.rmtree(wt_dir, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=str(repo_dir), capture_output=True, timeout=10)
        with get_db_connection(self.db_path) as conn:
            conn.execute("UPDATE forge_chunks SET status = 'failed', completed_at = ? WHERE id = ?", (time.time(), chunk_id))
            conn.execute("UPDATE forge_builds SET status = 'failed', updated_at = ? WHERE id = ?", (time.time(), build_id))
        post_or_update_build_widget(build_id, db_path=self.db_path)
        return False


forge_supervisor = ForgeSupervisor()


# ---------------------------------------------------------------------------
# 4. Pillar 3: Native Detached Background Job Runner (`forge_jobs`)
# ---------------------------------------------------------------------------

def _get_job_dir(task_id: str) -> Path:
    return TASKS_DIR / task_id


def _read_job_meta(task_id: str, db_path: Path | str = FORGE_DB_PATH) -> Optional[dict]:
    """Read job metadata from `forge_jobs` in SQLite (with legacy `meta.json` fallback)."""
    meta_file = _get_job_dir(task_id) / "meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
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
                return d
    except Exception:
        pass
    return None


def _write_job_meta(task_id: str, meta: dict, db_path: Path | str = FORGE_DB_PATH) -> None:
    """Persist job metadata to `TASKS_DIR/<id>/meta.json` and sync to `forge_jobs` when in production."""
    task_dir = _get_job_dir(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    meta_file = task_dir / "meta.json"
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
                        worker_pid, child_pid, exit_code, log_file,
                        notify_on_success, synthesize, start_time, end_time, duration_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        worker_pid=excluded.worker_pid,
                        child_pid=excluded.child_pid,
                        exit_code=excluded.exit_code,
                        end_time=excluded.end_time,
                        duration_seconds=excluded.duration_seconds
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


def _run_job_worker_locked(task_id: str, meta: dict, db_path: Path | str = FORGE_DB_PATH) -> None:
    meta["worker_pid"] = os.getpid()
    meta["status"] = "running"
    _write_job_meta(task_id, meta, db_path=db_path)

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
            )
            meta["child_pid"] = child_proc.pid
            _write_job_meta(task_id, meta, db_path=db_path)

            try:
                exit_code = child_proc.wait(timeout=timeout)
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
    sync_forge_channel_header(db_path=db_path)

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

    # Suppress redundant outbox synthesis/failure alerts when a short (<25s) task finishes
    # while the originating channel's interactive turn is still actively in-flight and polling it directly.
    if duration < 25.0 and _is_channel_turn_actively_in_flight(channel):
        with open(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(
                f"\n[Detached Runner] Short task ({dur_str}, status={final_status}) finished while "
                f"origin channel ({channel}) turn is still actively in-flight. "
                "Suppressing duplicate outbox notification so active turn handles result inline.\n"
            )
        return

    synthesize = fresh_meta.get("synthesize", True)
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
    """Cancel an active background job."""
    meta = _read_job_meta(task_id, db_path=db_path)
    if not meta:
        return {"error": f"Task '{task_id}' not found."}

    child_pid = meta.get("child_pid")
    worker_pid = meta.get("worker_pid")

    meta["status"] = "cancelled"
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
        with get_db_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM forge_jobs ORDER BY start_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
            for r in rows:
                d = dict(r)
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

    with get_db_connection(db_path) as conn:
        builds = conn.execute(
            "SELECT * FROM forge_builds WHERE status IN ('queued', 'running', 'awaiting_approval') ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        recent_builds = conn.execute(
            "SELECT * FROM forge_builds WHERE status IN ('completed', 'failed') ORDER BY updated_at DESC LIMIT 3"
        ).fetchall()
        jobs = conn.execute(
            "SELECT * FROM forge_jobs WHERE status IN ('starting', 'running') ORDER BY start_time DESC LIMIT 10"
        ).fetchall()
        if is_private_channel:
            decisions = conn.execute(
                "SELECT * FROM forge_decisions WHERE status = 'pending' ORDER BY created_at ASC"
            ).fetchall()
        else:
            decisions = conn.execute(
                "SELECT * FROM forge_decisions WHERE status = 'pending' AND is_private = 0 ORDER BY created_at ASC"
            ).fetchall()

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
    with get_db_connection(db_path) as conn:
        builds = conn.execute(
            "SELECT * FROM forge_builds WHERE status IN ('queued', 'running', 'awaiting_approval') ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        jobs = conn.execute(
            "SELECT * FROM forge_jobs WHERE status IN ('starting', 'running') ORDER BY start_time DESC LIMIT 10"
        ).fetchall()
        decisions = conn.execute(
            "SELECT * FROM forge_decisions WHERE status = 'pending' AND is_private = 0 ORDER BY created_at ASC"
        ).fetchall()

    lines = [
        "### ⚒️ FORGE — **F**ederated **O**rchestration, **R**untime & **G**ated **E**xecution",
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
            lines.append(f"- ⚙️ **`{j['id']}` — {j['name']}** (`{j['status']}` · `{j['channel']}`)")

    lines.append("\n**Pending Action Approvals (Decisions):**")
    if not decisions:
        lines.append("- *Zero pending approval cards.*")
    else:
        for d in decisions:
            lines.append(
                f"- 🛡️ **`{d['id']}` [{d['category'].upper()}] {d['title']}** in <#{d['channel_id']}> — {d['summary'][:100]}"
            )

    return "\n".join(lines)


def sync_forge_channel_header(
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
    header_message_id: int = FORGE_HEADER_MESSAGE_ID,
    force: bool = False,
) -> bool:
    """Update the #forge header message in place whenever active builds, tasks, or decisions change."""
    if not force and Path(db_path).resolve() != FORGE_DB_PATH.resolve():
        return False

    content = format_forge_channel_header(db_path=db_path)
    cache_file = DATA_DIR / ".forge_header_cache.txt"
    if not force and cache_file.exists():
        try:
            if cache_file.read_text(encoding="utf-8") == content:
                return False
        except Exception:
            pass

    token = _get_discord_token(for_forge_channel=True)
    if not token:
        return False

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages/{header_message_id}"
    try:
        resp = requests.patch(url, headers=headers, json={"content": content[:1990], "embeds": []}, timeout=10)
        if resp.status_code < 300:
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

    p_boot = sub.add_parser("reconcile", help="Run boot reconciliation and worktree cleanup")

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
        plan_data = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        res = enqueue_build(plan_data, post_widget=not args.no_widget, allow_overlap=args.allow_overlap)
        print(json.dumps(res, indent=2))
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
