"""
bridge_engine.py — Shared Execution Engine & Turn Coordination Core for Zero Discord Bridge.

Unifies common execution lifecycle logic between:
- Dynamic PTY turns (tools/bridge_runner.py)
- Persistent channel daemons (tools/bridge_daemons.py)

Responsibilities:
1. Thread Escalation: Migrates long-running root turns to Discord threads cleanly.
2. Wedge & Stall Detection: Probes process tree wait-states on extended silence.
3. Stream Event Handling: Parses stream-json events, updates status previews, tracks completion.
4. Turn Recovery: Harvests on-disk transcripts and formats structured diagnostic beacons.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import discord

from tools.bridge_formatting import (
    AgyStreamParser,
    format_agy_error_message,
    format_command_preview,
    generate_concise_thread_title,
    harvest_transcript_response,
    is_internal_cli_leak,
    parse_agy_error,
)
from tools.bridge_state import (
    DATA_DIR,
    TARGET_CHANNEL_ID,
    clear_channel_session_id,
    set_channel_session_id,
    update_beacon,
)
from tools.bridge_pipeline import TurnTimer
from tools.process_probe import diagnose_process_tree

# Global active process map: channel_id -> subprocess.Popen
channel_active_procs: dict[int, Any] = {}


@dataclass
class TurnCoordinator:
    """Coordinates turn lifecycle state, timeouts, thread migration, and process watchdogs."""

    channel_id: int
    prompt: str
    mode: str
    reply_target: Any
    status_msg: Optional[discord.Message]
    conv_id: Optional[str]
    turn_start_time: float = field(default_factory=time.time)
    last_activity_time: float = field(default_factory=time.time)
    last_probe_time: float = field(default_factory=time.time)
    last_beacon_touch: float = field(default_factory=time.time)
    escalated_to_thread: bool = False
    delivery_target: Any = None
    thread: Optional[discord.Thread] = None
    notify_root_channel: Any = None
    thread_jump_url: Optional[str] = None
    wedged_diagnostic: Optional[dict] = None
    timed_out: bool = False
    is_hard_ceiling: bool = False
    last_agy_error: Optional[dict] = None
    result_received_at: Optional[float] = None
    agent_response_done_at: Optional[float] = None
    had_substantive_delta: bool = False
    current_action: str = "Processing..."

    def __post_init__(self):
        if self.delivery_target is None:
            self.delivery_target = self.reply_target

    def touch_activity(self):
        """Record that stdout/stderr data or an event was received."""
        self.last_activity_time = time.time()

    def maybe_touch_beacon(self, state: str = "PROCESSING", interval: float = 10.0):
        """Update the bridge liveness beacon if enough time has elapsed."""
        if self.mode == "home":
            now = time.time()
            if (now - self.last_beacon_touch) >= interval:
                update_beacon(state, self.prompt, channel_id=self.channel_id)
                self.last_beacon_touch = now

    async def check_thread_escalation(
        self,
        proc: Any,
        escalation_enabled: bool,
        escalation_seconds: float,
    ) -> bool:
        """Check if turn execution time exceeded threshold and escalate root channel to a thread."""
        now = time.time()
        is_root_eligible = (
            self.mode == "home"
            and self.channel_id == TARGET_CHANNEL_ID
            and self.reply_target is not None
            and hasattr(self.reply_target, "create_thread")
            and not isinstance(getattr(self.reply_target, "channel", None), discord.Thread)
            and escalation_enabled
            and escalation_seconds > 0
            and not self.escalated_to_thread
        )

        if is_root_eligible and (now - self.turn_start_time) >= escalation_seconds:
            try:
                self.escalated_to_thread = True
                clean_title = generate_concise_thread_title(self.prompt)
                self.thread = await self.reply_target.create_thread(
                    name=f"🧵 {clean_title}", auto_archive_duration=1440
                )
                await self.reply_target.reply(
                    f"🧵 *Task execution exceeded {int(escalation_seconds)}s — migrating deliverable to {self.thread.mention}. `#zero-chat` remains free.*"
                )
                self.status_msg = None
                self.delivery_target = self.thread
                self.notify_root_channel = getattr(self.reply_target, "channel", None)
                self.thread_jump_url = self.thread.jump_url
                if proc:
                    channel_active_procs[self.thread.id] = proc
                    if TARGET_CHANNEL_ID in channel_active_procs:
                        del channel_active_procs[TARGET_CHANNEL_ID]
                return True
            except Exception as te:
                print(f"[BridgeEngine] Warning escalating turn to thread: {te}")
        return False

    def probe_process_wedge(self, proc_pid: Optional[int], output_snippet: str = "") -> bool:
        """Probe process wait states for interactive stdin deadlocks after 45s of silence."""
        now = time.time()
        if not proc_pid:
            return False
        if (now - self.last_activity_time) >= 45.0 and (now - self.last_probe_time) >= 10.0:
            self.last_probe_time = now
            try:
                diag = diagnose_process_tree(proc_pid, output_buffer=output_snippet)
                if diag.get("is_interactive_stdin"):
                    print(
                        f"[BridgeEngine] 🚨 Wedged interactive subprocess detected for PID {proc_pid} "
                        f"in channel {self.channel_id}: {diag['summary']}. Terminating early..."
                    )
                    self.wedged_diagnostic = diag
                    self.timed_out = True
                    return True
            except Exception as pe:
                print(f"[BridgeEngine] Warning running process probe: {pe}")
        return False

    def check_watchdog_timeout(
        self,
        step_idle_timeout: float = 90.0,
        turn_timeout_seconds: float = 300.0,
        max_turn_ceiling: float = 1800.0,
    ) -> tuple[bool, str]:
        """Evaluate two-tier inactivity and hard turn ceilings.
        
        Returns: (is_timed_out, reason_description)
        """
        now = time.time()
        is_step_idle = (now - self.last_activity_time) >= step_idle_timeout
        is_turn_timeout = (now - self.last_activity_time) >= turn_timeout_seconds
        self.is_hard_ceiling = (now - self.turn_start_time) >= max_turn_ceiling

        if is_step_idle or is_turn_timeout or self.is_hard_ceiling:
            self.timed_out = True
            reason = (
                f"{int(step_idle_timeout)}s step idle"
                if is_step_idle
                else (
                    f"{int(turn_timeout_seconds)}s turn idle"
                    if is_turn_timeout
                    else f"{int(max_turn_ceiling)}s hard ceiling"
                )
            )
            return True, reason
        return False, ""

    def evaluate_completion_cutoffs(
        self,
        agent_done_window: float = 15.0,
    ) -> tuple[bool, str]:
        """Check if post-result or quiescent agent completion cutoffs have been satisfied.
        
        Returns: (is_cutoff_reached, trigger_label)
        """
        now = time.time()
        is_result_cutoff = (
            self.result_received_at is not None
            and (now - self.result_received_at) >= 1.5
        )
        is_agent_done_cutoff = (
            self.agent_response_done_at is not None
            and (now - self.agent_response_done_at) >= agent_done_window
            and (now - self.last_activity_time) >= agent_done_window
        )
        if is_result_cutoff:
            return True, "Result event"
        if is_agent_done_cutoff:
            return True, f"Agent response DONE ({int(agent_done_window)}s quiescent)"
        return False, ""

    def process_stream_event(
        self,
        event: dict,
        stream_parser: Optional[AgyStreamParser] = None,
        timer: Optional[TurnTimer] = None,
    ) -> None:
        """Process a structured event from agy stream-json."""
        if not isinstance(event, dict):
            return

        ev_name = event.get("event") or event.get("type")

        if stream_parser:
            stream_parser.process_event(event)

        if ev_name == "init":
            new_cid = event.get("conversation_id")
            if new_cid:
                self.conv_id = new_cid
                set_channel_session_id(self.channel_id, self.mode, new_cid)

        elif ev_name == "step_update":
            step = event.get("step_update", {})
            stype = step.get("step_type")
            sstate = step.get("state")
            tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")

            if (stype in ("tool", "system_message", "system") or tname) and sstate != "DONE":
                if timer:
                    timer.mark_event(tool_name=tname)
                self.agent_response_done_at = None
                tinfo = step.get("tool_info", {})
                params = tinfo.get("parameters", {})
                if tname == "run_command" and "CommandLine" in params:
                    self.current_action = format_command_preview(params["CommandLine"])
                elif tname == "view_file" and "AbsolutePath" in params:
                    fpath = Path(params["AbsolutePath"]).name
                    self.current_action = f"Reading: {fpath}..."
                elif tname == "grep_search" and "Query" in params:
                    self.current_action = f"Searching: {params['Query'][:50]}..."
                elif tname == "replace_file_content" and "TargetFile" in params:
                    fpath = Path(params["TargetFile"]).name
                    self.current_action = f"Editing: {fpath}..."
                elif tname == "call_mcp_tool":
                    mcp_tool = params.get("ToolName", "mcp")
                    self.current_action = f"Querying {mcp_tool}..."
                else:
                    self.current_action = f"Calling tool: {tname}..."

            elif stype == "agent_response":
                delta = step.get("text_delta") or step.get("text") or step.get("content")
                if delta and isinstance(delta, str) and delta.strip():
                    self.had_substantive_delta = True
                    if timer:
                        timer.mark_event(is_token=True)
                    self.current_action = "Drafting response..."
                if step.get("state") == "DONE" and self.had_substantive_delta:
                    if self.agent_response_done_at is None:
                        self.agent_response_done_at = time.time()

        elif ev_name == "result" or "result" in event:
            if timer:
                timer.mark_result()
            if self.result_received_at is None:
                self.result_received_at = time.time()
            res_data = event.get("result", {}) if isinstance(event.get("result"), dict) else event
            res_cid = res_data.get("conversation_id")
            if res_cid:
                if self.escalated_to_thread and self.thread and hasattr(self.thread, "id"):
                    set_channel_session_id(self.thread.id, self.mode, res_cid)
                    clear_channel_session_id(TARGET_CHANNEL_ID, "home")
                else:
                    set_channel_session_id(self.channel_id, self.mode, res_cid)
                self.conv_id = res_cid
            self.current_action = "Finalizing output..."

    def format_diagnostic_beacon(
        self,
        proc_pid: Optional[int] = None,
        returncode: Optional[int] = None,
        turn_timeout_seconds: float = 300.0,
    ) -> str:
        """Format an authoritative diagnostic report when a turn fails or drops output."""
        elapsed_sec = int(time.time() - self.turn_start_time)
        pid_str = f"PID {proc_pid}" if proc_pid else "unknown PID"

        if self.wedged_diagnostic:
            culprit = self.wedged_diagnostic.get("culprit") or {}
            c_name = culprit.get("name") or culprit.get("cmdline") or pid_str
            c_wchan = culprit.get("wchan") or "unknown"
            c_pid = culprit.get("pid") or (proc_pid if proc_pid else "?")
            return (
                f"⚠️ **Subprocess Wedged on Interactive Input:**\n\n"
                f"{self.wedged_diagnostic['summary']}\n\n"
                f"• **Culprit:** `{c_name}` (PID {c_pid})\n"
                f"• **Kernel Wait Channel:** `{c_wchan}`\n"
                f"• **Diagnostic:** A tool spawned an interactive command without automated flags. "
                f"The subprocess was terminated after {elapsed_sec}s of silence to prevent an indefinite hang."
            )
        if self.timed_out:
            return (
                f"⚠️ **Turn Timed Out:** Subprocess exceeded watchdog limit of {int(turn_timeout_seconds)}s "
                f"({pid_str}). No complete response was produced."
            )
        if self.last_agy_error or returncode == 3:
            err_dict = self.last_agy_error or {}
            if err_dict:
                return format_agy_error_message(err_dict, elapsed_sec=elapsed_sec, pid_str=pid_str)
            return (
                f"⚠️ **Model API Failure (CLI Exit Code 3):** Process terminated with exit code 3 after "
                f"{elapsed_sec}s ({pid_str}). Upstream model request failed."
            )
        if returncode not in (0, None):
            return (
                f"⚠️ **Turn Failed:** Process terminated with exit code {returncode} after {elapsed_sec}s "
                f"({pid_str}). No complete output was produced."
            )
        return (
            f"⚠️ **Turn Incomplete:** Agent process exited after {elapsed_sec}s ({pid_str}) without "
            f"generating output or a recoverable transcript."
        )
