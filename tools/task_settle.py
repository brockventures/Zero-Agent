#!/usr/bin/env python3
"""
task_settle.py — TaskSettle Protocol: Background Task State Supervisor.

Core Responsibilities:
1. Ground-Truth Task Tracking: Deterministically extracts launched vs completed
   background tasks directly from session transcripts and message logs (zero regex guess-work).
2. Settle Horizon Window: If agy exits prematurely with active background tasks pending,
   holds the Discord turn open for a bounded grace window (default: 25.0s).
3. Self-Healing Reinvocation: If tasks complete within the grace window, re-invokes
   agy with the completion payload to produce and deliver the final substantive response.
4. Daemon / Hang Protection: If tasks exceed the grace window, gracefully releases the
   turn to Discord without deadlocking or hanging.
5. Hard Loop Breaker: Enforces max 1 auto-reinvocation per user turn to prevent latency cascades.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

DEFAULT_BRAIN_DIR = Path("/root/.gemini/antigravity-cli/brain")
DEFAULT_SETTLE_TIMEOUT = 25.0


def extract_turn_lines(lines: list[str]) -> list[str]:
    """Scan transcript lines in reverse to extract strictly the active turn."""
    last_user_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i].strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            step_type = d.get("type")
            source = d.get("source")
            role = d.get("role")
            if (
                step_type in ("USER_INPUT", "CHECKPOINT", "user", "USER")
                or source in ("USER_EXPLICIT", "USER")
                or role in ("user", "USER")
            ):
                last_user_idx = i
                break
        except Exception:
            continue

    if last_user_idx == -1:
        return lines
    return lines[last_user_idx:]


def get_turn_pending_tasks(
    conv_id: str,
    brain_dir: Path | str = DEFAULT_BRAIN_DIR,
    transcript_lines: Optional[list[str]] = None,
) -> list[str]:
    """Deterministically extract background tasks launched in current turn that have not completed."""
    b_dir = Path(brain_dir)
    conv_path = b_dir / conv_id

    if transcript_lines is None:
        tpath = conv_path / ".system_generated" / "logs" / "transcript.jsonl"
        if not tpath.exists():
            return []
        try:
            with open(tpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"[TaskSettle] Warning reading transcript for {conv_id}: {e}")
            return []
    else:
        lines = transcript_lines

    turn_lines = extract_turn_lines(lines)

    # 1. Identify all background tasks launched in this turn
    launched_tasks: list[str] = []
    completed_in_transcript: set[str] = set()

    for line in turn_lines:
        line_s = line.strip()
        if not line_s:
            continue
        try:
            d = json.loads(line_s)
            content = str(d.get("content", ""))
            # Launch pattern: "Tool is running as a background task with task id: <tid>"
            for m in re.finditer(r"Tool is running as a background task with task id:\s*([^\s\n]+)", content):
                full_tid = m.group(1).strip("\"'")
                clean_tid = full_tid.split("/")[-1]
                if clean_tid not in launched_tasks:
                    launched_tasks.append(clean_tid)

            # Completion pattern in transcript: "Task id \"<tid>\" finished" or "was canceled"
            for m in re.finditer(r"Task id \"?([^\s\n\"]+)\"? (?:finished|was canceled)", content):
                full_tid = m.group(1).strip("\"'")
                clean_tid = full_tid.split("/")[-1]
                completed_in_transcript.add(clean_tid)
        except Exception:
            continue

    if not launched_tasks:
        return []

    # 2. Check disk message logs for completion events
    msg_dir = conv_path / ".system_generated" / "messages"
    completed_in_messages: set[str] = set()

    if msg_dir.exists():
        for mf in glob.glob(str(msg_dir / "*.json")):
            if os.path.basename(mf) == "read.json":
                continue
            try:
                with open(mf, "r", encoding="utf-8", errors="replace") as fp:
                    md = json.load(fp)
                    sender = str(md.get("sender", ""))
                    content = str(md.get("content", ""))
                    for tid in launched_tasks:
                        if tid in sender or f'"{tid}" finished' in content or f'"{tid}" was canceled' in content:
                            completed_in_messages.add(tid)
            except Exception:
                pass

    all_completed = completed_in_transcript | completed_in_messages
    pending = [tid for tid in launched_tasks if tid not in all_completed]
    return pending


def is_task_completed(
    conv_id: str,
    task_id: str,
    brain_dir: Path | str = DEFAULT_BRAIN_DIR,
) -> bool:
    """Check if a specific task ID has a completion record on disk or in transcript."""
    clean_tid = task_id.split("/")[-1]
    conv_path = Path(brain_dir) / conv_id
    msg_dir = conv_path / ".system_generated" / "messages"

    if msg_dir.exists():
        for mf in glob.glob(str(msg_dir / "*.json")):
            if os.path.basename(mf) == "read.json":
                continue
            try:
                with open(mf, "r", encoding="utf-8", errors="replace") as fp:
                    md = json.load(fp)
                    sender = str(md.get("sender", ""))
                    content = str(md.get("content", ""))
                    if clean_tid in sender or f'"{clean_tid}" finished' in content or f'"{clean_tid}" was canceled' in content:
                        return True
            except Exception:
                pass

    tpath = conv_path / ".system_generated" / "logs" / "transcript.jsonl"
    if tpath.exists():
        try:
            with open(tpath, "r", encoding="utf-8", errors="replace") as f:
                for line in reversed(f.readlines()[-30:]):
                    if f'"{clean_tid}" finished' in line or f'"{clean_tid}" was canceled' in line:
                        return True
        except Exception:
            pass

    return False


async def wait_for_tasks_to_settle(
    conv_id: str,
    pending_tasks: list[str],
    brain_dir: Path | str = DEFAULT_BRAIN_DIR,
    timeout_seconds: float = DEFAULT_SETTLE_TIMEOUT,
    poll_interval: float = 0.5,
) -> tuple[bool, float, list[str]]:
    """Poll for pending tasks to settle within a bounded grace window.
    
    Returns: (all_settled, elapsed_seconds, still_pending)
    """
    start_t = time.perf_counter()
    clean_targets = [t.split("/")[-1] for t in pending_tasks]

    while (time.perf_counter() - start_t) < timeout_seconds:
        still_pending = [
            tid for tid in clean_targets
            if not is_task_completed(conv_id, tid, brain_dir=brain_dir)
        ]
        if not still_pending:
            elapsed = time.perf_counter() - start_t
            return True, elapsed, []
        await asyncio.sleep(poll_interval)

    elapsed = time.perf_counter() - start_t
    still_pending = [
        tid for tid in clean_targets
        if not is_task_completed(conv_id, tid, brain_dir=brain_dir)
    ]
    return len(still_pending) == 0, elapsed, still_pending


async def evaluate_and_settle_turn(
    conv_id: Optional[str],
    channel_id: int,
    mode: str,
    status_msg: Any,
    reply_target: Any,
    reinvoke_coro_fn: Callable[..., Coroutine[Any, Any, Any]],
    timeout_seconds: float = DEFAULT_SETTLE_TIMEOUT,
    brain_dir: Path | str = DEFAULT_BRAIN_DIR,
    current_text: str = "",
    turn_kwargs: Optional[Dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    """Evaluates whether the turn suffered premature exit with pending tasks,
    settles them within the bounded window, and re-invokes agy if resolved.
    
    Returns: (was_settled, final_delivered_text)
    """
    if not conv_id:
        return False, None

    pending = get_turn_pending_tasks(conv_id, brain_dir=brain_dir)
    if not pending:
        return False, None

    print(
        f"[TaskSettle] 🛡️ Caught premature exit for conversation {conv_id} with "
        f"{len(pending)} pending task(s): {pending}. Settle horizon: {timeout_seconds:.1f}s."
    )

    all_settled, elapsed, still_pending = await wait_for_tasks_to_settle(
        conv_id=conv_id,
        pending_tasks=pending,
        brain_dir=brain_dir,
        timeout_seconds=timeout_seconds,
    )

    if not all_settled:
        print(
            f"[TaskSettle] ⏱️ Settle window ({timeout_seconds:.1f}s) expired with task(s) "
            f"still pending: {still_pending}. Treating as long-running daemon / build. "
            "Releasing turn without reinvocation."
        )
        return False, None

    print(
        f"[TaskSettle] ✅ Task(s) {pending} settled successfully in {elapsed:.1f}s! "
        "Re-invoking agent to deliver complete substantive response..."
    )

    # Prompt re-invoking agy to process completion message and deliver final response
    reinvoke_prompt = (
        f"[TaskSettle Protocol]: Background task(s) {', '.join(pending)} completed successfully. "
        "Review the task output now in your context and deliver your final, complete, and substantive "
        "response to the user. Do NOT emit any waiting or interim chatter; deliver strictly the finished deliverable."
    )

    kwargs = dict(turn_kwargs or {})
    kwargs["is_settle_reinvocation"] = True

    try:
        reinvoke_res = await reinvoke_coro_fn(
            prompt=reinvoke_prompt,
            status_msg=status_msg,
            reply_target=reply_target,
            attachments=[],
            mode=mode,
            channel_id=channel_id,
            **kwargs,
        )
        return True, str(reinvoke_res) if reinvoke_res is not None else None
    except Exception as e:
        print(f"[TaskSettle] Error during reinvocation for {conv_id}: {e}")
        return False, None
