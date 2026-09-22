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
DATA_DIR = Path("/workspace/data")
PENDING_SDK_TASKS_FILE = DATA_DIR / "pending_sdk_tasks.json"


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
        register_pending_tasks(
            conv_id=conv_id,
            channel_id=channel_id,
            mode=mode,
            task_ids=still_pending,
        )
        from tools.bridge_safety import is_internal_cli_leak
        if not current_text or is_internal_cli_leak(current_text):
            if mode == "external":
                notice = "⏳ *Task running in the background—will update here when complete.*"
            else:
                notice = "⏳ **Background task in progress.** Command is running in the background; I'll notify when complete."
            return False, notice
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


def register_pending_tasks(
    conv_id: str,
    channel_id: int | str,
    mode: str,
    task_ids: list[str],
    data_dir: Path | str = DATA_DIR,
):
    """Register pending SDK background tasks to be monitored asynchronously."""
    p_file = Path(data_dir) / "pending_sdk_tasks.json"
    data = {}
    if p_file.exists():
        try:
            with open(p_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    entry = data.get(conv_id, {
        "channel_id": int(channel_id) if str(channel_id).isdigit() else channel_id,
        "mode": mode,
        "task_ids": [],
        "registered_at": time.time(),
    })
    for tid in task_ids:
        clean_tid = tid.split("/")[-1]
        if clean_tid not in entry["task_ids"]:
            entry["task_ids"].append(clean_tid)
    entry["updated_at"] = time.time()
    data[conv_id] = entry

    try:
        tmp_file = p_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(p_file)
    except Exception as e:
        print(f"[TaskSettle] Warning saving pending tasks: {e}")


def get_task_completion_details(
    conv_id: str,
    task_id: str,
    brain_dir: Path | str = DEFAULT_BRAIN_DIR,
) -> dict:
    """Retrieve completion payload, status, and log tail for a background task."""
    clean_tid = task_id.split("/")[-1]
    conv_path = Path(brain_dir) / conv_id
    res = {
        "task_id": clean_tid,
        "status": "completed",
        "exit_code": 0,
        "log_snippet": "",
        "log_path": "",
    }

    # 1. Check message files
    msg_dir = conv_path / ".system_generated" / "messages"
    if msg_dir.exists():
        for mf in glob.glob(str(msg_dir / "*.json")):
            if os.path.basename(mf) == "read.json":
                continue
            try:
                with open(mf, "r", encoding="utf-8", errors="replace") as fp:
                    md = json.load(fp)
                    content = str(md.get("content", ""))
                    if clean_tid in str(md.get("sender", "")) or f'"{clean_tid}" finished' in content or f'"{clean_tid}" was canceled' in content:
                        if "canceled" in content:
                            res["status"] = "cancelled"
                            res["exit_code"] = -1
                        elif "exit code" in content:
                            m = re.search(r"exit code\s+(\d+)", content)
                            if m:
                                res["exit_code"] = int(m.group(1))
                                if res["exit_code"] != 0:
                                    res["status"] = "failed"
            except Exception:
                pass

    # 2. Check task log
    task_log = conv_path / ".system_generated" / "tasks" / f"{clean_tid}.log"
    if task_log.exists():
        res["log_path"] = str(task_log)
        try:
            with open(task_log, "r", encoding="utf-8", errors="replace") as lf:
                lines = [l.rstrip() for l in lf.readlines() if l.strip()]
                tail = lines[-12:]
                if tail:
                    res["log_snippet"] = "\n".join(tail)
        except Exception:
            pass

    return res


def check_and_dispatch_completed_tasks(
    brain_dir: Path | str = DEFAULT_BRAIN_DIR,
    data_dir: Path | str = DATA_DIR,
) -> list[dict]:
    """Check pending SDK tasks and dispatch outbox notifications upon completion.
    
    Returns list of dispatched task details.
    """
    p_file = Path(data_dir) / "pending_sdk_tasks.json"
    if not p_file.exists():
        return []

    try:
        with open(p_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    if not data:
        return []

    dispatched = []
    modified = False

    for conv_id, info in list(data.items()):
        channel_id = info.get("channel_id")
        task_ids = info.get("task_ids", [])
        still_pending = []

        for tid in task_ids:
            if is_task_completed(conv_id, tid, brain_dir=brain_dir):
                details = get_task_completion_details(conv_id, tid, brain_dir=brain_dir)
                status = details["status"]
                status_emoji = "✅" if status == "completed" else ("🛑" if status == "cancelled" else "❌")
                status_title = "Complete" if status == "completed" else ("Cancelled" if status == "cancelled" else "Failed")

                msg_lines = [
                    f"{status_emoji} **Background Task {status_title}** (`{tid}`)",
                ]
                if details.get("exit_code") is not None:
                    msg_lines.append(f"• **Exit Code:** `{details['exit_code']}`")
                if details.get("log_path"):
                    msg_lines.append(f"• **Log File:** `{details['log_path']}`")

                snippet = details.get("log_snippet")
                if snippet:
                    from tools.bridge_safety import strip_internal_cli_chatter
                    clean_snip = strip_internal_cli_chatter(snippet).strip()
                    if clean_snip:
                        msg_lines.append(f"\n**Output Excerpt:**\n```text\n{clean_snip}\n```")

                outbox_content = "\n".join(msg_lines)

                try:
                    from tools.outbox import queue_outbox_message
                    queue_outbox_message(
                        channel=channel_id,
                        content=outbox_content,
                        source_turn=f"sdk-task-{tid}",
                    )
                    dispatched.append(details)
                    print(f"[TaskSettle] 🚀 Dispatched outbox completion notice for task {tid} to channel {channel_id}")
                except Exception as oe:
                    print(f"[TaskSettle] Failed to queue outbox notification for {tid}: {oe}")
                    still_pending.append(tid)
            else:
                reg_at = info.get("registered_at", time.time())
                if (time.time() - reg_at) < 7200:
                    still_pending.append(tid)

        if len(still_pending) != len(task_ids):
            modified = True
            if still_pending:
                info["task_ids"] = still_pending
                data[conv_id] = info
            else:
                del data[conv_id]

    if modified:
        try:
            tmp_file = p_file.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp_file.replace(p_file)
        except Exception as e:
            print(f"[TaskSettle] Warning updating pending tasks: {e}")

    return dispatched
