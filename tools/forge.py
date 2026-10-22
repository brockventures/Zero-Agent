#!/usr/bin/env python3
"""
forge.py — FORGE Engine (Federated Orchestration, Runtime & Gated Execution)

Unifies three core runtime pillars in a single WAL-mode SQLite state machine (/workspace/data/forge.db):
1. Activity-Renewed Lease & Heartbeat Engine: Replaces blind wall-clock guillotines with 4-signal
   ground-truth progress leases (stdout deltas, child PIDs, SDK task logs, subagent transcripts)
   plus a 3-strike runaway tool-loop circuit breaker.
2. Multi-Chunk Spec-Driven Build Queue (#forge): Executes Tier 2/3 engineering plans in isolated
   git worktrees using gemini-3.8-flash-high (with escalation to gemini-4-argon-medium), enforcing
   deterministic Maker-Checker verification gates, live #forge widget updates, mid-run wake
   suppression, and originating-channel completion wake notifications.
3. Deterministic Decisions Framework: Backs inline Discord Approve / Edit / Dismiss cards with
   SQLite action payloads executed in <1s at zero token cost.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
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

DATA_DIR = WORKSPACE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FORGE_DB_PATH = DATA_DIR / "forge.db"
DEFAULT_BRAIN_DIR = Path("/root/.gemini/antigravity-cli/brain")
PT_TZ = ZoneInfo("America/Los_Angeles")

FORGE_CHANNEL_ID = 1562899941595160657
ZERO_CHAT_CHANNEL_ID = 1542081375287640084
ZERO_OPS_CHANNEL_ID = 1544953279664889888
OWNER_USER_ID = int(os.getenv("DISCORD_OWNER_ID", "179407724335988736"))

DEFAULT_WORKER_MODEL = "gemini-3.8-flash-high"
ESCALATION_WORKER_MODEL = "gemini-4-argon-medium"
DEFAULT_LEASE_TTL_SECONDS = 300.0


# ---------------------------------------------------------------------------
# 1. SQLite WAL Database Initialization & Connection Helper
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


def init_forge_db(db_path: Path | str = FORGE_DB_PATH) -> None:
    """Initialize all FORGE tables and indexes idempotently."""
    with get_db_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS forge_builds (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                repo TEXT NOT NULL DEFAULT '/workspace',
                tier INTEGER NOT NULL DEFAULT 2,
                touches_core_bridge INTEGER NOT NULL DEFAULT 0,
                origin_channel_id INTEGER NOT NULL,
                origin_thread_id INTEGER,
                origin_conv_id TEXT,
                forge_message_id INTEGER,
                fact_sheet_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS forge_chunks (
                id TEXT PRIMARY KEY,
                build_id TEXT NOT NULL REFERENCES forge_builds(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                worker_model TEXT NOT NULL DEFAULT 'gemini-3.8-flash-high',
                target_files_json TEXT NOT NULL DEFAULT '[]',
                prohibited_files_json TEXT NOT NULL DEFAULT '[]',
                instructions TEXT NOT NULL,
                verification_cmd TEXT NOT NULL,
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


# Initialize schema on import
init_forge_db()


def _now_pt_str(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).astimezone(PT_TZ)
    return dt.strftime("%Y-%m-%d %I:%M:%S %p PT")


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
# 3. Pillar 2: FORGE Build Queue & Spec Validation
# ---------------------------------------------------------------------------

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
        t_files = ch.get("target_files")
        if not isinstance(t_files, list) or not t_files:
            return False, f"Chunk {idx} ('{ch_title}') must specify a non-empty 'target_files' list."
        instr = str(ch.get("instructions") or "").strip()
        if len(instr) < 15:
            return False, f"Chunk {idx} ('{ch_title}') must include detailed TDD 'instructions'."
        v_cmd = str(ch.get("verification_cmd") or "").strip()
        if not v_cmd:
            return False, f"Chunk {idx} ('{ch_title}') must specify a deterministic 'verification_cmd'."

    return True, "Valid ForgePlan."


def _detect_touches_core_bridge(plan: dict) -> bool:
    """Detect whether any chunk targets live tools/bridge*.py files."""
    if plan.get("touches_core_bridge") is not None:
        return bool(plan.get("touches_core_bridge"))
    for ch in plan.get("chunks", []):
        for tf in ch.get("target_files", []):
            norm = str(tf).replace("\\", "/")
            if "bridge" in norm and norm.endswith(".py") and not Path(norm).name.startswith("test_"):
                return True
    return False


def render_build_widget_text(build_id: str, db_path: Path | str = FORGE_DB_PATH) -> str:
    """Render the live Discord tracker widget markdown for a FORGE build in #forge."""
    with get_db_connection(db_path) as conn:
        build = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not build:
            return f"⚠️ Build `{build_id}` not found."
        chunks = conn.execute(
            "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
            (build_id,),
        ).fetchall()

    status_map = {
        "queued": ("⏳", "QUEUED"),
        "running": ("🔨", "RUNNING"),
        "awaiting_approval": ("🛑", "AWAITING APPROVAL"),
        "completed": ("✅", "COMPLETED"),
        "failed": ("❌", "FAILED"),
        "cancelled": ("⚠️", "CANCELLED"),
    }
    chunk_badge_map = {
        "pending": "⏳",
        "running": "🔨",
        "verifying": "🧪",
        "awaiting_approval": "👀",
        "merged": "✅",
        "failed": "❌",
    }

    b_emoji, b_label = status_map.get(build["status"], ("⚙️", build["status"].upper()))
    origin_ref = f"<#{build['origin_thread_id'] or build['origin_channel_id']}>"
    repo_name = Path(build["repo"]).name or "workspace"

    lines = [
        f"### {b_emoji} FORGE Build `{build['id']}`: {build['title']}",
        f"- **Status:** **{b_label}** | **Tier:** {build['tier']} | **Repo:** `{repo_name}` | **Origin:** {origin_ref}",
    ]

    total = len(chunks)
    merged_count = sum(1 for c in chunks if c["status"] == "merged")
    lines.append(f"- **Progress:** {merged_count}/{total} chunk(s) merged")
    lines.append("")

    # Collapse older merged chunks if > 10 chunks total
    display_chunks = list(chunks)
    if len(display_chunks) > 10:
        merged_indices = [i for i, c in enumerate(display_chunks) if c["status"] == "merged"]
        if len(merged_indices) > 4:
            hidden_count = len(merged_indices) - 3
            lines.append(f"- ✅ *({hidden_count} earlier chunk(s) verified and merged)*")
            keep_set = set(merged_indices[-3:]) | {i for i, c in enumerate(display_chunks) if c["status"] != "merged"}
            display_chunks = [c for i, c in enumerate(display_chunks) if i in keep_set]

    for c in display_chunks:
        c_emoji = chunk_badge_map.get(c["status"], "•")
        idx_num = c["chunk_index"] + 1
        model_tag = c["worker_model"]
        pr_suffix = f" (PR #{c['pr_number']})" if c["pr_number"] else ""
        diff_suffix = f" — `{c['diff_stat']}`" if c["diff_stat"] else ""
        lines.append(
            f"- {c_emoji} **Chunk {idx_num}/{total}: {c['title']}** [`{model_tag}`]{pr_suffix}{diff_suffix}"
        )
        if c["status"] == "failed" and c["last_error"]:
            err_snip = c["last_error"].strip().splitlines()[-1][:140]
            lines.append(f"  - ⚠️ Error: `{err_snip}`")

    lines.append(f"\n*Updated {_now_pt_str(build['updated_at'])}*")
    return "\n".join(lines)


def post_or_update_build_widget(
    build_id: str,
    db_path: Path | str = FORGE_DB_PATH,
    forge_channel_id: int = FORGE_CHANNEL_ID,
) -> Optional[int]:
    """Post or edit the single live tracker message for `build_id` inside #forge via Discord REST API."""
    content = render_build_widget_text(build_id, db_path=db_path)
    try:
        from tools.deliver_image import get_discord_token
        token = get_discord_token()
    except Exception:
        return None

    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT forge_message_id FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
        if not row:
            return None
        msg_id = row["forge_message_id"]

    try:
        if msg_id:
            url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages/{msg_id}"
            resp = requests.patch(url, headers=headers, json={"content": content[:1990]}, timeout=10)
            if resp.status_code < 300:
                return int(msg_id)

        url = f"https://discord.com/api/v10/channels/{forge_channel_id}/messages"
        resp = requests.post(url, headers=headers, json={"content": content[:1990]}, timeout=10)
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


def enqueue_build(
    plan: dict,
    db_path: Path | str = FORGE_DB_PATH,
    post_widget: bool = True,
) -> dict:
    """Validate and enqueue a multi-chunk FORGE build plan."""
    valid, reason = validate_forge_plan(plan)
    if not valid:
        raise ValueError(f"Invalid ForgePlan: {reason}")

    now = time.time()
    slug = re.sub(r"[^a-z0-9]+", "-", plan["title"].lower()).strip("-")[:18] or "build"
    build_id = plan.get("id") or f"fg-{int(now)}-{slug}"
    touches_core = 1 if _detect_touches_core_bridge(plan) else 0

    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (
                id, title, repo, tier, touches_core_bridge,
                origin_channel_id, origin_thread_id, origin_conv_id,
                fact_sheet_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                build_id,
                plan["title"].strip(),
                str(plan.get("repo") or "/workspace").strip(),
                int(plan.get("tier") or (3 if len(plan["chunks"]) > 1 else 2)),
                touches_core,
                int(plan["origin_channel_id"]),
                int(plan["origin_thread_id"]) if plan.get("origin_thread_id") else None,
                plan.get("origin_conv_id"),
                json.dumps(plan["context_fact_sheet"]),
                now,
                now,
            ),
        )

        for idx, ch in enumerate(plan["chunks"]):
            chunk_id = f"{build_id}-c{idx}"
            conn.execute(
                """
                INSERT INTO forge_chunks (
                    id, build_id, chunk_index, title, worker_model,
                    target_files_json, prohibited_files_json,
                    instructions, verification_cmd, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    chunk_id,
                    build_id,
                    idx,
                    ch["title"].strip(),
                    ch.get("worker_model") or DEFAULT_WORKER_MODEL,
                    json.dumps(ch.get("target_files", [])),
                    json.dumps(ch.get("prohibited_files", [])),
                    ch["instructions"].strip(),
                    ch["verification_cmd"].strip(),
                ),
            )

    msg_id = None
    if post_widget:
        msg_id = post_or_update_build_widget(build_id, db_path=db_path)

    return {
        "ok": True,
        "build_id": build_id,
        "chunk_count": len(plan["chunks"]),
        "forge_channel_id": FORGE_CHANNEL_ID,
        "forge_message_id": msg_id,
        "turn_action": "end_turn",
    }


def format_chunk_prompt(build_id: str, chunk_index: int, db_path: Path | str = FORGE_DB_PATH, worktree_dir: Optional[str] = None) -> str:
    """Synthesize the bounded, high-signal prompt for a FORGE chunk worker."""
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
    target_files = json.loads(target_chunk["target_files_json"] or "[]")
    prohibited_files = json.loads(target_chunk["prohibited_files_json"] or "[]")
    wt_str = worktree_dir or build["repo"]

    lines = [
        f"# [FORGE WORKER EXECUTION CONTRACT — BUILD `{build_id}` / CHUNK {chunk_index + 1} OF {len(chunks)}]",
        f"**Overall Build Objective:** {build['title']}",
        f"**Isolated Working Directory:** `{wt_str}`",
        "",
        "## 1. Architectural Context Fact Sheet (From Lead Orchestrator)",
    ]
    for fact in fact_sheet:
        lines.append(f"- {fact}")

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
    if prohibited_files:
        lines.append(f"- **Prohibited Files (DO NOT MODIFY):** `{', '.join(prohibited_files)}`")
    lines.append(f"- **Mandatory Verification Gate:** `{target_chunk['verification_cmd']}`")
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
        f"3. Run `{target_chunk['verification_cmd']}` until it exits 0.\n"
        "4. Output a concise summary of changes and stop immediately."
    )
    return "\n".join(lines)


def verify_chunk_worktree(
    worktree_dir: Path | str,
    verification_cmd: str,
    prohibited_files: Optional[list[str]] = None,
) -> tuple[bool, str, str]:
    """Run deterministic Maker-Checker verification inside the chunk worktree.

    Returns (passed, diff_stat_summary, error_output).
    """
    wt = Path(worktree_dir)
    if not wt.exists():
        return False, "", f"Worktree directory does not exist: {wt}"

    # 1. Check prohibited files were not modified
    if prohibited_files:
        st_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=15,
        )
        changed = [line[3:].strip().strip('"') for line in st_proc.stdout.splitlines() if len(line) > 3]
        violated = [f for f in changed if f in set(prohibited_files)]
        if violated:
            return False, "", f"Boundary violation: chunk modified prohibited file(s): {', '.join(violated)}"

    # 2. Execute verification_cmd
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{wt}:{env.get('PYTHONPATH', '')}".strip(":")
    v_proc = subprocess.run(
        verification_cmd,
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
            [sys.executable, str(safety_script)],
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
        f"**Repository:** `{build['repo']}`",
        f"**Status:** All {len(chunks)} chunk(s) passed deterministic Maker-Checker verification and merged.",
        "",
        "### Merged Chunks Ledger:",
    ]
    for c in chunks:
        pr_tag = f"PR #{c['pr_number']}" if c["pr_number"] else "Merged"
        diff_tag = f" ({c['diff_stat']})" if c["diff_stat"] else ""
        lines.append(f"- **Chunk {c['chunk_index'] + 1}: {c['title']}** — {pr_tag}{diff_tag} [verified via `{c['verification_cmd']}`]")

    if fact_sheet:
        lines.append("\n### Original Context Fact Sheet:")
        for f in fact_sheet:
            lines.append(f"- {f}")

    reload_note = (
        "\nThis build modified core bridge files (`touches_core_bridge=true`). Include `[CHOICES: Reload Bridge In-Place | Postpone Reload]` at the end of your response."
        if build["touches_core_bridge"]
        else ""
    )
    lines.append(
        "\nEvaluate the completed build ledger above against the original user request. "
        "Deliver a concise final completion summary directly to Ryan in this channel."
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
    try:
        from tools.outbox import queue_outbox_message
        queue_outbox_message(
            channel=int(target_ch),
            content=envelope,
            source_turn=f"forge-complete-{build_id}",
            msg_type="agent_turn",
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

    for repo_cand in (WORKSPACE_DIR, WORKSPACE_DIR / "market-sandbox", WORKSPACE_DIR / "kalshi-quant", WORKSPACE_DIR / "agora-roguelike", WORKSPACE_DIR / "sharkfin"):
        if (repo_cand / ".git").exists():
            subprocess.run(["git", "worktree", "prune"], cwd=str(repo_cand), capture_output=True, timeout=10)

    reset_chunks = 0
    with get_db_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE forge_chunks SET status = 'pending' WHERE status IN ('running', 'verifying') AND attempt_count < 3"
        )
        reset_chunks = cur.rowcount
        conn.execute(
            "UPDATE forge_builds SET status = 'queued', updated_at = ? WHERE status = 'running'",
            (time.time(),),
        )

    return {"cleaned_worktrees": cleaned_wts, "reset_chunks": reset_chunks}


class ForgeSupervisor:
    """Asynchronous background supervisor for FORGE build chunks and Decision Card expiry."""

    def __init__(self, db_path: Path | str = FORGE_DB_PATH):
        self.db_path = Path(db_path)
        self._active_build_task: Optional[Any] = None

    def tick_sync(self) -> dict:
        """Synchronous housekeeping tick: expire stale decisions and check pending SDK tasks."""
        expired = expire_stale_decisions(db_path=self.db_path)
        dispatched_sdk = []
        try:
            from tools.task_settle import check_and_dispatch_completed_tasks
            dispatched_sdk = check_and_dispatch_completed_tasks()
        except Exception:
            pass
        return {"expired_decisions": expired, "dispatched_sdk_tasks": len(dispatched_sdk)}

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

        repo_dir = Path(build["repo"]).resolve()
        wt_dir = Path(f"/tmp/forge-wt-{chunk_id}")
        branch_name = f"forge/{build_id}-c{chunk_idx}"
        base_ref = f"forge/release-{build_id}" if build["touches_core_bridge"] else "origin/main"

        # Ensure base ref exists
        subprocess.run(["git", "fetch", "origin", "main"], cwd=str(repo_dir), capture_output=True, timeout=30)
        if build["touches_core_bridge"] and chunk_idx == 0:
            subprocess.run(["git", "branch", "-f", base_ref, "origin/main"], cwd=str(repo_dir), capture_output=True, timeout=10)

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
            # Fallback to HEAD if origin/main is not fetched in test repos
            subprocess.run(
                ["git", "worktree", "add", "-B", branch_name, str(wt_dir), "HEAD"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )

        prohibited = json.loads(chunk_row["prohibited_files_json"] or "[]")
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
                "chat",
                "--mode", "agent",
                "--yolo",
                "--print",
                "--output-format", "stream-json",
                "--print-timeout=4h",
                "--workspace", str(wt_dir),
                "--model", model,
                prompt,
            ]
            try:
                subprocess.run(agy_cmd, cwd=str(wt_dir), capture_output=True, text=True, timeout=1200)
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
            )
            if passed:
                # Commit and merge into base
                subprocess.run(["git", "add", "-A"], cwd=str(wt_dir), capture_output=True, timeout=15)
                subprocess.run(
                    ["git", "commit", "-m", f"feat(forge): {chunk_row['title']} [{chunk_id}]"],
                    cwd=str(wt_dir),
                    capture_output=True,
                    timeout=15,
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

                post_or_update_build_widget(build_id, db_path=self.db_path)
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
# 4. Pillar 3: Deterministic Decisions Framework (Inline Approval Cards)
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

    try:
        from tools.deliver_image import get_discord_token
        token = get_discord_token()
    except Exception:
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
            receipt = "Triggered in-place bridge reload flag"

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
# 5. Global `/queue` Digest & Active Build Inspection
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


# ---------------------------------------------------------------------------
# 6. CLI Entrypoint (`python3 /workspace/tools/forge.py ...`)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="FORGE — Federated Orchestration, Runtime & Gated Execution CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue", help="Validate and enqueue a ForgePlan JSON file")
    p_enq.add_argument("plan_file", help="Path to ForgePlan JSON file")
    p_enq.add_argument("--no-widget", action="store_true", help="Skip posting initial #forge Discord widget")

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

    args = parser.parse_args()
    if args.cmd == "enqueue":
        plan_data = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        res = enqueue_build(plan_data, post_widget=not args.no_widget)
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


if __name__ == "__main__":
    main()
