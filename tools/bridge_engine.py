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
5. Persistent Stream Loop: Unified asynchronous stream reading for worker daemons.
6. Process & Beacon Teardown: Clean de-registration and idle beacon restoration.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import discord

from tools.bridge_formatting import generate_concise_thread_title
from tools.bridge_safety import is_internal_cli_leak
from tools.bridge_stream import (
    AgyStreamParser,
    format_agy_error_message,
    format_command_preview,
    harvest_transcript_response,
    parse_agy_error,
)
from tools.bridge_pipeline import TurnTimer
from tools.bridge_state import (
    DATA_DIR,
    TARGET_CHANNEL_ID,
    clear_channel_session_id,
    clear_in_flight,
    get_runtime_rules,
    set_channel_session_id,
    update_beacon,
)
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
    turn_start_time: float = 0.0
    last_activity_time: float = 0.0
    last_probe_time: float = 0.0
    last_beacon_touch: float = 0.0
    last_status_edit: float = 0.0
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
        now = self.turn_start_time if self.turn_start_time > 0 else time.time()
        if self.turn_start_time <= 0:
            self.turn_start_time = now
        if self.last_activity_time <= 0:
            self.last_activity_time = now
        if self.last_probe_time <= 0:
            self.last_probe_time = now
        if self.last_beacon_touch <= 0:
            self.last_beacon_touch = now
        if self.last_status_edit <= 0:
            self.last_status_edit = now

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

    async def update_status_ticker(
        self, ticker_enabled: Optional[bool] = None, force: bool = False
    ) -> bool:
        """Throttled status message edit with clean ANSI-stripped action text."""
        now = time.time()
        if self.status_msg and (force or (now - self.last_status_edit >= 1.5)):
            if ticker_enabled is None:
                ticker_enabled = get_runtime_rules().get("live_status_ticker_enabled", False)
            if ticker_enabled:
                clean_action = re.sub(
                    r"\x1b(?:\[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", self.current_action
                )
                try:
                    await self.status_msg.edit(content=f"⏳ *{clean_action}*")
                    self.last_status_edit = now
                    return True
                except Exception:
                    pass
        return False

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
                        f"in channel {self.channel_id}: {diag.get('summary', 'interactive deadlock')}. Terminating early..."
                    )
                    self.wedged_diagnostic = diag
                    self.timed_out = True
                    return True
            except Exception as pe:
                print(f"[BridgeEngine] Warning running process probe: {pe}")
        return False

    def handle_wedge_detection(self, proc_pid: Optional[int], output_snippet: str = "") -> bool:
        """Alias for probe_process_wedge."""
        return self.probe_process_wedge(proc_pid, output_snippet=output_snippet)

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

    def handle_line(
        self,
        line_s: str,
        stream_parser: Optional[AgyStreamParser] = None,
        timer: Optional[TurnTimer] = None,
    ) -> Optional[dict]:
        """Parse a single output line (JSON stream-json event or formatted terminal text)."""
        line_s = line_s.strip()
        if not line_s:
            return None

        if line_s.startswith("{") and line_s.endswith("}"):
            try:
                ev = json.loads(line_s)
                self.process_stream_event(ev, stream_parser=stream_parser, timer=timer)
                return ev
            except Exception:
                return None

        if line_s.startswith("● "):
            self.current_action = line_s[:100]
        elif "(Calls tool:" in line_s:
            self.current_action = line_s[:100]
        elif "AGY_ERROR:" in line_s:
            agy_err = parse_agy_error(line_s)
            if agy_err:
                self.last_agy_error = agy_err
        return None

    async def execute_stream_loop(
        self,
        proc: Any,
        stdout_reader: asyncio.StreamReader,
        stream_parser: AgyStreamParser,
        timer: TurnTimer,
        rules: Optional[dict] = None,
        worker_name: str = "worker",
        on_recycle: Optional[Callable[..., Any]] = None,
        get_last_agy_error: Optional[Callable[[], Optional[dict]]] = None,
    ) -> str:
        """Execute stream reading loop for a persistent worker daemon."""
        if rules is None:
            rules = get_runtime_rules()

        watchdog_timeout = float(rules.get("turn_watchdog_seconds", 300.0))
        max_turn_ceiling = float(rules.get("turn_max_ceiling_seconds", 1800.0))
        escalation_seconds = float(rules.get("auto_thread_escalation_seconds", 180.0))
        escalation_enabled = rules.get("auto_thread_escalation_enabled", True)
        ticker_enabled = rules.get("live_status_ticker_enabled", False)

        output_response = ""
        while True:
            # 1. Thread escalation check (#zero-chat root only)
            await self.check_thread_escalation(
                proc=proc,
                escalation_enabled=escalation_enabled,
                escalation_seconds=escalation_seconds,
            )

            # 2. Read next line with timeout
            line_bytes = None
            while line_bytes is None:
                try:
                    line_bytes = await asyncio.wait_for(
                        stdout_reader.readline(), timeout=5.0
                    )
                    self.touch_activity()
                except asyncio.TimeoutError:
                    now_wait = time.time()
                    proc_pid = getattr(proc, "pid", None) if proc else None

                    # Check for wedged interactive subprocess at >= 45s of silence
                    if self.probe_process_wedge(proc_pid):
                        if on_recycle:
                            await on_recycle()
                        return self.format_diagnostic_beacon(proc_pid=proc_pid)

                    # Watchdog timeout check
                    is_timeout, reason = self.check_watchdog_timeout(
                        step_idle_timeout=watchdog_timeout,
                        turn_timeout_seconds=watchdog_timeout,
                        max_turn_ceiling=max_turn_ceiling,
                    )
                    if is_timeout:
                        print(
                            f"[BridgeEngine] ⚠️ Watchdog timeout: #{worker_name} worker exceeded {reason}. Recycling..."
                        )
                        if on_recycle:
                            await on_recycle()
                        raise TimeoutError(
                            f"Turn watchdog timeout ({reason}) exceeded in #{worker_name}"
                        )

            if self.wedged_diagnostic is not None:
                break

            # 3. Handle EOF / premature termination
            if not line_bytes:
                await asyncio.sleep(0.05)
                last_err = self.last_agy_error or (
                    get_last_agy_error() if get_last_agy_error else None
                )
                proc_rc = getattr(proc, "returncode", None) if proc else None
                proc_pid = getattr(proc, "pid", None) if proc else None

                err_detail = ""
                if last_err:
                    err_detail = f": {last_err}"
                elif proc_rc is not None:
                    err_detail = f" (exit code {proc_rc})"

                print(f"[BridgeEngine] ⚠️ Persistent worker for #{worker_name} exited unexpectedly{err_detail}.")
                if on_recycle:
                    await on_recycle()
                if last_err:
                    return format_agy_error_message(
                        last_err,
                        elapsed_sec=int(time.time() - self.turn_start_time),
                        pid_str=f"PID {proc_pid}" if proc_pid else "",
                    )
                raise RuntimeError(
                    f"Persistent worker for #{worker_name} terminated unexpectedly{err_detail}"
                )

            # 4. Handle substantive line
            line_s = line_bytes.decode("utf-8", errors="replace").strip()
            if not line_s:
                continue

            ev = self.handle_line(line_s, stream_parser=stream_parser, timer=timer)
            if ev and (ev.get("event") == "result" or "result" in ev):
                res_data = ev.get("result", {}) if isinstance(ev.get("result"), dict) else ev
                output_response = stream_parser.get_final_response(
                    fallback_result=res_data.get("response", "")
                )
                res_cid = res_data.get("conversation_id")
                if res_cid:
                    if self.escalated_to_thread and self.thread and hasattr(self.thread, "id"):
                        set_channel_session_id(self.thread.id, self.mode, res_cid)
                        clear_channel_session_id(TARGET_CHANNEL_ID, "home")
                        if on_recycle:
                            await on_recycle(new_conv_id=None)
                        print(
                            f"[BridgeEngine] 🧵 Bound session {res_cid} to migrated thread {self.thread.id} and recycled root worker."
                        )
                    else:
                        self.conv_id = res_cid
                        set_channel_session_id(self.channel_id, self.mode, res_cid)
                break

            # 5. Live status ticker update
            await self.update_status_ticker(ticker_enabled=ticker_enabled)

            # 6. Beacon update
            self.maybe_touch_beacon()

        return output_response

    def harvest_fallback(
        self,
        output_response: str,
        active_cid: Optional[str] = None,
        proc: Any = None,
        turn_timeout_seconds: float = 300.0,
    ) -> tuple[str, bool]:
        """Audit response for empty, leak, or silence sentinels, harvest from transcript,
        and apply authoritative error beacons if necessary.

        Returns: (final_text, was_harvested)
        """
        cid = active_cid or self.conv_id
        if self.escalated_to_thread and self.thread and hasattr(self.thread, "id"):
            cid = cid or getattr(self.thread, "id", None)

        final_text = output_response or ""
        is_empty_or_placeholder = (
            not final_text or final_text.startswith("*(") or len(final_text.strip()) == 0
        )
        is_leak = is_internal_cli_leak(final_text)
        is_silence = final_text.strip() in (
            "[NO_REPLY]",
            "NO_REPLY",
            "[NO_OP]",
            "NO_OP",
            "reply:none",
            "reply: none",
        )

        has_process_failure = (
            self.timed_out
            or self.wedged_diagnostic
            or self.last_agy_error
            or (proc and getattr(proc, "returncode", None) not in (0, None))
        )

        # In external mode, genuine non-error silence without output maps to [NO_REPLY]
        if self.mode == "external" and (is_leak or is_silence) and not has_process_failure:
            final_text = "[NO_REPLY]"
            return final_text, False

        was_harvested = False
        if is_empty_or_placeholder or is_leak or (self.mode == "home" and is_silence):
            harvested = harvest_transcript_response(str(cid) if cid else None)
            if harvested and not is_internal_cli_leak(harvested):
                print(
                    f"[BridgeEngine] 🌾 Harvested response from on-disk transcript for session {cid} "
                    f"({len(harvested)} chars)."
                )
                final_text = f"⚠️ *(Recovered from session transcript following process cutoff)*\n\n{harvested}"
                is_empty_or_placeholder = False
                is_leak = False
                is_silence = False
                was_harvested = True

        should_emit_beacon = (
            (self.mode == "home" and (is_empty_or_placeholder or is_leak or is_silence))
            or (self.mode == "external" and has_process_failure and (is_empty_or_placeholder or is_leak or is_silence))
        )

        if should_emit_beacon:
            proc_pid = getattr(proc, "pid", None) if proc else None
            returncode = getattr(proc, "returncode", None) if proc else None
            final_text = self.format_diagnostic_beacon(
                proc_pid=proc_pid,
                returncode=returncode,
                turn_timeout_seconds=turn_timeout_seconds,
            )

        return final_text, was_harvested

    def cleanup_process(self, proc: Any = None, br_module: Any = None):
        """Clean up active process mapping, in-flight state, and restore beacon to IDLE if all workers are idle."""
        if self.channel_id in channel_active_procs:
            del channel_active_procs[self.channel_id]
        if (
            self.escalated_to_thread
            and self.thread
            and hasattr(self.thread, "id")
            and self.thread.id in channel_active_procs
        ):
            del channel_active_procs[self.thread.id]

        if self.mode == "home":
            if br_module and hasattr(br_module, "active_proc"):
                br_module.active_proc = None
            try:
                clear_in_flight(self.channel_id)
            except Exception:
                pass
        else:
            if br_module and hasattr(br_module, "ext_active_proc"):
                br_module.ext_active_proc = None

        if not any(p and getattr(p, "returncode", None) is None for p in channel_active_procs.values()):
            update_beacon("IDLE", "")

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
