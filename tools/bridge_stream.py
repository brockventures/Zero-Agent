"""
bridge_stream.py — Stream Protocol, Event Ingestion & Session Transcript Harvesting.

Core Responsibilities:
1. Stream Parsing: Deterministic NDJSON event stream parser (`AgyStreamParser`)
   for `agy --output-format stream-json`.
2. Transcript Recovery: On-disk transcript parser (`harvest_transcript_response`)
   to salvage responses if output pipes are prematurely severed.
3. Command & Action Previews: Format tool and shell execution previews for status tickers.
4. Structured Error Triage: Parse and format `AGY_ERROR: {...}` payloads from model failures.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from tools.bridge_safety import is_internal_cli_leak, strip_internal_cli_chatter


def format_command_preview(cmd_raw: str, max_len: int = 80) -> str:
    """Format command string for Discord status previews, stripping SSH boilerplate and showing host."""
    lines_list = cmd_raw.strip().splitlines()
    first_line = lines_list[0].strip() if lines_list else ""
    host_1 = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
    host_2 = os.environ.get("NAS_HOST_2_IP", "127.0.0.1")

    if first_line.startswith("ssh "):
        host_tag = ""
        if host_1 in first_line:
            host_tag = f"[{host_1}]"
        elif host_2 in first_line:
            host_tag = f"[{host_2}]"

        parts = re.split(rf"(?:{re.escape(host_1)}|{re.escape(host_2)})\s+", first_line, maxsplit=1)
        if len(parts) > 1:
            inner_cmd = parts[1].strip().strip('"').strip("'")
            snip = inner_cmd[:max_len]
            return f"Running {host_tag}: {snip}..."

    snip = first_line[:max_len]
    return f"Running: {snip}..."


def reconcile_step_segments(
    earlier: str,
    latest: str,
    is_interrupted: bool = False,
) -> str:
    """Reconcile two consecutive agent response segments across a step boundary or stream interruption.

    Handles three recovery patterns:
    1. Full Redraft: The new step restarts from the beginning (matching opening line/prefix or subsuming earlier).
    2. Line/Bullet Restart: The new step restarts from the cut-off line or bullet at the end of `earlier`.
    3. Tail Continuation: The new step emits only the remaining tail after the cutoff (with suffix/prefix overlap dedup).
    """
    e_strip = (earlier or "").strip()
    l_strip = (latest or "").strip()
    if not e_strip:
        return l_strip
    if not l_strip:
        return e_strip

    # Pre-system wait stub discard
    if (len(e_strip) < 40 or len(e_strip.split()) < 6) and len(l_strip) >= 80 and len(l_strip) > len(e_strip) * 2:
        return l_strip

    # 1. Full Redraft Detection: latest subsumes earlier or restarts with the same opening prefix/header
    if e_strip in l_strip or (len(e_strip) > 35 and e_strip[:35] in l_strip):
        return l_strip
    if l_strip in e_strip:
        return e_strip

    first_e_line = e_strip.splitlines()[0].strip()
    first_l_line = l_strip.splitlines()[0].strip()
    if (
        (e_strip.startswith("🍌") and l_strip.startswith("🍌"))
        or (first_e_line and first_l_line and first_e_line == first_l_line)
        or (len(e_strip) >= 24 and len(l_strip) >= 24 and e_strip[:24] == l_strip[:24])
    ):
        return l_strip

    # Trailing generic task-completion stub discard
    if (
        any(
            k in l_strip.lower()
            for k in [
                "standing by",
                "no background tasks",
                "migrations and evaluations are complete",
                "evaluations are complete",
            ]
        )
        and len(e_strip) > len(l_strip) * 2
    ):
        return e_strip

    # 2. Line / Bullet Restart Detection (model restarted from the cut-off bullet/line at the tail of `earlier`)
    e_lines = earlier.rstrip().splitlines()
    if e_lines:
        last_e_line = e_lines[-1].strip()
        # Check if last line was cut off or shares a bullet/numbered prefix with the start of `l_strip`
        bullet_match_e = re.match(r"^([-*•]\s+(?:\*\*)?\d+\.|[-*•]\s+\*\*[^*]+\*\*|\d+\.\s+|###?\s+)", last_e_line)
        bullet_match_l = re.match(r"^([-*•]\s+(?:\*\*)?\d+\.|[-*•]\s+\*\*[^*]+\*\*|\d+\.\s+|###?\s+)", l_strip)
        if (
            (bullet_match_e and bullet_match_l and bullet_match_e.group(1) == bullet_match_l.group(1))
            or (len(last_e_line) >= 10 and l_strip.startswith(last_e_line[:10]))
        ):
            prefix_body = "\n".join(e_lines[:-1]).rstrip()
            return f"{prefix_body}\n{l_strip}" if prefix_body else l_strip

    # 3. Suffix/Prefix Overlap Deduplication (model backed up a few words/chars when continuing)
    max_ov = min(len(e_strip), len(l_strip), 300)
    for k in range(max_ov, 3, -1):
        if e_strip.endswith(l_strip[:k]):
            return e_strip + l_strip[k:]

    # 4. Mid-Sentence Tail Continuation (when interrupted or `earlier` lacks terminal punctuation)
    ends_incomplete = not e_strip.endswith((".", "!", "?", ":", "```", ")", "]", "*", "`", '"', "'"))
    if is_interrupted or ends_incomplete:
        if l_strip.startswith((".", ",", ";", ":", ")", "]")):
            return f"{e_strip}{l_strip}"
        if earlier.endswith("\n") or latest.startswith("\n"):
            return f"{e_strip}\n{l_strip}"
        return f"{e_strip} {l_strip}"

    return f"{e_strip}\n\n{l_strip}"


def harvest_transcript_response(
    conv_id: Optional[str],
    brain_base_dir: Optional[Path | str] = None,
    min_step_index: int = -1,
) -> Optional[str]:
    """Harvest completed response from transcript files if stdout was truncated or cut off.

    Enforces active turn boundary: stops immediately if a USER_INPUT or CHECKPOINT step is
    encountered (or if step_index <= min_step_index) before finding a substantive
    PLANNER_RESPONSE, preventing prior turn responses from ever being harvested or re-delivered.
    """
    if not conv_id:
        return None
    base = Path(brain_base_dir) if brain_base_dir else Path("/root/.gemini/antigravity-cli/brain")
    brain_dir = base / str(conv_id) / ".system_generated" / "logs"
    for fname in ("transcript_full.jsonl", "transcript.jsonl"):
        tpath = brain_dir / fname
        if not tpath.exists():
            continue
        try:
            with open(tpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            candidates = []
            saw_interruption = False
            for line in reversed(lines):
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    data = json.loads(line_s)
                    sidx = data.get("step_index")
                    if min_step_index >= 0 and sidx is not None and int(sidx) <= min_step_index:
                        break
                    step_type = data.get("type")
                    source = data.get("source")
                    role = data.get("role")

                    # Turn boundary check: Never cross into prior conversation turns or checkpoints
                    if (
                        step_type in ("USER_INPUT", "CHECKPOINT", "user", "USER")
                        or source in ("USER_EXPLICIT", "USER")
                        or role in ("user", "USER")
                    ):
                        break

                    if step_type == "ERROR_MESSAGE" or "stream was interrupted" in line_s.lower():
                        saw_interruption = True
                        continue

                    if step_type == "PLANNER_RESPONSE":
                        # Intermediate steps with tool calls are pre-tool scratchpad chatter, not final responses
                        if data.get("tool_calls"):
                            continue
                        content = data.get("content")
                        if content and isinstance(content, str) and content.strip():
                            stripped = content.strip()
                            if is_internal_cli_leak(stripped):
                                continue
                            candidates.append(stripped)
                except Exception:
                    continue

            if not candidates:
                continue

            if len(candidates) == 1:
                return candidates[0]

            # Reconcile candidates in chronological order (oldest to newest within active turn)
            chronological = list(reversed(candidates))
            reconciled = chronological[0]
            for nxt in chronological[1:]:
                reconciled = reconcile_step_segments(reconciled, nxt, is_interrupted=saw_interruption)
            return reconciled
        except Exception as e:
            print(f"[BridgeStream] Error reading transcript {tpath}: {e}")
    return None


def get_latest_transcript_step_index(
    conv_id: Optional[str],
    brain_base_dir: Optional[Path | str] = None,
) -> int:
    """Return the highest step_index currently recorded in the session transcript, or -1."""
    if not conv_id:
        return -1
    base = Path(brain_base_dir) if brain_base_dir else Path("/root/.gemini/antigravity-cli/brain")
    brain_dir = base / str(conv_id) / ".system_generated" / "logs"
    for fname in ("transcript_full.jsonl", "transcript.jsonl"):
        tpath = brain_dir / fname
        if not tpath.exists():
            continue
        try:
            with open(tpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    data = json.loads(line_s)
                    sidx = data.get("step_index")
                    if sidx is not None:
                        return int(sidx)
                except Exception:
                    continue
        except Exception:
            continue
    return -1


def check_transcript_turn_status(
    conv_id: Optional[str],
    min_step_index: int = -1,
    brain_base_dir: Optional[Path | str] = None,
) -> dict:
    """Inspect transcript to determine if the active turn has concluded or is waiting on a tool/task."""
    if not conv_id:
        return {"is_complete": False, "content": None, "reason": "no_conv_id"}
    base = Path(brain_base_dir) if brain_base_dir else Path("/root/.gemini/antigravity-cli/brain")
    brain_dir = base / str(conv_id) / ".system_generated" / "logs"
    for fname in ("transcript_full.jsonl", "transcript.jsonl"):
        tpath = brain_dir / fname
        if not tpath.exists():
            continue
        try:
            with open(tpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    data = json.loads(line_s)
                    sidx = data.get("step_index")
                    if min_step_index >= 0 and sidx is not None and int(sidx) <= min_step_index:
                        return {"is_complete": False, "content": None, "reason": "stale_prior_turn_step", "step_index": sidx}
                    stype = data.get("type")
                    source = data.get("source")
                    role = data.get("role")

                    if (
                        stype in ("USER_INPUT", "CHECKPOINT", "user", "USER")
                        or source in ("USER_EXPLICIT", "USER")
                        or role in ("user", "USER")
                    ):
                        return {"is_complete": False, "content": None, "reason": "user_input_boundary"}

                    if stype == "PLANNER_RESPONSE":
                        tool_calls = data.get("tool_calls", [])
                        if tool_calls:
                            return {
                                "is_complete": False,
                                "content": None,
                                "has_active_tool": True,
                                "reason": "tool_calls_pending",
                                "tool_count": len(tool_calls),
                                "step_index": sidx,
                            }

                        content = (data.get("content") or "").strip()
                        if content:
                            if any(phrase in content.lower() for phrase in (
                                "no tools were called",
                                "summary of accomplishments will be delivered",
                                "will wait for it to finish",
                                "i have launched the command",
                                "completing silently",
                                "executing silently",
                            )):
                                return {
                                    "is_complete": False,
                                    "content": None,
                                    "reason": "intermediate_task_wait",
                                    "step_index": sidx,
                                }

                        cleaned_test = strip_internal_cli_chatter(content).strip() if content else ""
                        if content and not is_internal_cli_leak(content) and cleaned_test and not is_internal_cli_leak(cleaned_test):
                            return {
                                "is_complete": True,
                                "content": content,
                                "step_index": sidx,
                                "reason": "complete",
                            }
                        return {"is_complete": False, "content": None, "reason": "empty_or_leak_content"}

                    if stype in ("GENERIC", "tool"):
                        return {
                            "is_complete": False,
                            "content": None,
                            "has_active_tool": True,
                            "reason": "tool_output_pending",
                            "step_index": sidx,
                        }
                except Exception:
                    continue
        except Exception as e:
            print(f"[BridgeStream] Error checking transcript status {tpath}: {e}")
    return {"is_complete": False, "content": None, "reason": "no_entries"}


class AgyStreamParser:
    """Deterministic state-machine parser for agy stream-json output."""

    def __init__(self, conv_id: Optional[str] = None):
        self.conv_id = conv_id
        self.accumulated_segment: list[str] = []
        self.last_substantive_response: str = ""
        self.interrupted_draft: str = ""
        self.final_result_response: str = ""
        self.error_response: str = ""
        self.is_explicit_silence: bool = False
        self.current_agent_step_index: Optional[int] = None
        self.interrupted_since_last_step: bool = False

    @staticmethod
    def _is_silence_or_placeholder(text: str) -> bool:
        if not text or not isinstance(text, str):
            return True
        return is_internal_cli_leak(text)

    def _rotate_accumulated_segment(self, interrupted: bool = False) -> None:
        """Rotate current accumulated_segment into interrupted_draft (if interrupted) or last_substantive_response."""
        curr = "".join(self.accumulated_segment).strip()
        if interrupted:
            if curr and not self._is_silence_or_placeholder(curr):
                if self.interrupted_draft and not self._is_silence_or_placeholder(self.interrupted_draft):
                    self.interrupted_draft = reconcile_step_segments(
                        self.interrupted_draft, curr, is_interrupted=True
                    )
                else:
                    self.interrupted_draft = curr
            self.accumulated_segment.clear()
            self.last_substantive_response = ""
            self.interrupted_since_last_step = True
            return

        if curr and not self._is_silence_or_placeholder(curr):
            if self.last_substantive_response and not self._is_silence_or_placeholder(
                self.last_substantive_response
            ):
                self.last_substantive_response = reconcile_step_segments(
                    self.last_substantive_response,
                    curr,
                    is_interrupted=self.interrupted_since_last_step,
                )
            else:
                self.last_substantive_response = curr
        self.accumulated_segment.clear()

    def process_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return

        ev_type = event.get("event") or event.get("type")

        # 1. Init event: extract conversation_id if present
        if ev_type == "init" or "conversation_id" in event:
            cid = event.get("conversation_id")
            if cid:
                self.conv_id = cid

        # 2. Result event: contains overall turn summary
        if ev_type == "result" or "result" in event:
            res = event.get("result", {})
            if isinstance(res, dict):
                if res.get("response") and not is_internal_cli_leak(str(res["response"])):
                    self.final_result_response = res["response"]
                if res.get("error"):
                    self.error_response = f"Error: {res.get('error')}"
                if res.get("conversation_id"):
                    self.conv_id = res["conversation_id"]
            elif isinstance(event.get("response"), str) and not is_internal_cli_leak(event["response"]):
                self.final_result_response = event["response"]

        # 3. Step update event
        elif ev_type == "step_update" or "step_update" in event:
            step = event.get("step_update", {})
            if not isinstance(step, dict):
                return

            stype = step.get("step_type")
            tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")

            if (stype in ("tool",) or tname):
                # Tool execution: clear pre-tool narration and any stale intermediate result, preserving substantive response across system tasks
                self.accumulated_segment.clear()
                self.interrupted_draft = ""
                self.final_result_response = ""
                self.is_explicit_silence = False
                self.current_agent_step_index = None

            elif stype in ("system_message", "system", "error_message", "error"):
                # Asynchronous system event or stream interruption: rotate substantive draft for reconciliation
                step_str = json.dumps(step).lower()
                is_int = (
                    "interrupted" in step_str
                    or stype in ("error_message", "error")
                    or step.get("state") == "ERROR"
                )
                self._rotate_accumulated_segment(interrupted=is_int)
                self.current_agent_step_index = None

            elif stype == "agent_response":
                sidx = step.get("step_index")
                if sidx is not None:
                    try:
                        sidx_int = int(sidx)
                        if (
                            self.current_agent_step_index is not None
                            and sidx_int != self.current_agent_step_index
                        ):
                            self._rotate_accumulated_segment(
                                interrupted=self.interrupted_since_last_step
                            )
                        self.current_agent_step_index = sidx_int
                    except (ValueError, TypeError):
                        pass

                delta = step.get("text_delta") or step.get("text") or step.get("content")
                if delta and isinstance(delta, str):
                    self.accumulated_segment.append(delta)

                if step.get("state") == "DONE":
                    curr = "".join(self.accumulated_segment).strip()
                    if curr:
                        if self._is_silence_or_placeholder(curr):
                            if curr.lower() in (
                                "[no_reply]",
                                "no_reply",
                                "[no_op]",
                                "no_op",
                                "reply:none",
                                "reply: none",
                                "none",
                            ) or is_internal_cli_leak(curr):
                                self.is_explicit_silence = True
                        else:
                            self.is_explicit_silence = False

        elif ev_type in ("tool", "tool_call", "tool_use"):
            self.accumulated_segment.clear()
            self.interrupted_draft = ""
            self.final_result_response = ""
            self.is_explicit_silence = False
            self.current_agent_step_index = None

        elif ev_type in ("system_message", "system", "error", "error_message"):
            ev_str = json.dumps(event).lower()
            is_int = (
                "interrupted" in ev_str
                or ev_type in ("error", "error_message")
                or event.get("type") == "ERROR_MESSAGE"
            )
            self._rotate_accumulated_segment(interrupted=is_int)
            self.current_agent_step_index = None

        elif ev_type in ("content", "message", "text", "delta"):
            content = event.get("content") or event.get("text") or event.get("delta")
            if content and isinstance(content, str):
                self.accumulated_segment.append(content)

    def process_line(self, line: str) -> bool:
        line_s = line.strip()
        if not line_s:
            return False

        # Find JSON boundaries
        start = line_s.find("{")
        end = line_s.rfind("}") + 1
        if start != -1 and end > start:
            json_substr = line_s[start:end]
            try:
                ev = json.loads(json_substr)
                self.process_event(ev)
                return True
            except Exception:
                decoder = json.JSONDecoder()
                idx = start
                parsed_any = False
                while idx < len(line_s):
                    while idx < len(line_s) and line_s[idx] != "{":
                        idx += 1
                    if idx >= len(line_s):
                        break
                    try:
                        ev, end_idx = decoder.raw_decode(line_s, idx)
                        self.process_event(ev)
                        parsed_any = True
                        idx = end_idx
                    except Exception:
                        idx += 1
                return parsed_any
        return False

    def get_final_response(self, fallback_result: str = "") -> str:
        curr = "".join(self.accumulated_segment).strip()

        # Check if the current segment is an explicit silence request or internal CLI leak
        if curr and (
            curr.lower()
            in (
                "[no_reply]",
                "no_reply",
                "[no_op]",
                "no_op",
                "reply:none",
                "reply: none",
                "none",
            )
            or is_internal_cli_leak(curr)
        ):
            if not self.last_substantive_response or self._is_silence_or_placeholder(
                self.last_substantive_response
            ):
                return "[NO_REPLY]"

        clean_curr = ""
        if curr and not self._is_silence_or_placeholder(curr):
            clean_curr = re.sub(
                r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", curr, flags=re.IGNORECASE
            ).strip()
            clean_curr = strip_internal_cli_chatter(clean_curr)
            if self._is_silence_or_placeholder(clean_curr):
                clean_curr = ""

        prior_draft = self.last_substantive_response or self.interrupted_draft
        clean_last = ""
        if prior_draft and not self._is_silence_or_placeholder(prior_draft):
            clean_last = re.sub(
                r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$",
                "",
                prior_draft,
                flags=re.IGNORECASE,
            ).strip()
            clean_last = strip_internal_cli_chatter(clean_last)
            if self._is_silence_or_placeholder(clean_last):
                clean_last = ""

        # 1. Both prior draft (pre-interruption/pre-system-event) and current segment exist
        if clean_last and clean_curr:
            return reconcile_step_segments(
                clean_last,
                clean_curr,
                is_interrupted=self.interrupted_since_last_step or bool(self.interrupted_draft),
            )

        # 2. Only current segment exists
        if clean_curr:
            return clean_curr

        # 3. Only last substantive response exists (not an uncompleted interrupted fragment)
        if self.last_substantive_response and clean_last:
            return clean_last

        # 3. Check final result response from agy
        frr = self.final_result_response.strip() or fallback_result.strip()
        if frr:
            if frr.lower() in (
                "[no_reply]",
                "no_reply",
                "[no_op]",
                "no_op",
                "reply:none",
                "reply: none",
                "none",
            ) or is_internal_cli_leak(frr):
                if not self.last_substantive_response or self._is_silence_or_placeholder(
                    self.last_substantive_response
                ):
                    return "[NO_REPLY]"
            clean_frr = re.sub(
                r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", frr, flags=re.IGNORECASE
            ).strip()
            clean_frr = strip_internal_cli_chatter(clean_frr)
            if clean_frr and not self._is_silence_or_placeholder(clean_frr):
                return clean_frr

        if self.is_explicit_silence:
            return "[NO_REPLY]"

        if self.error_response:
            return self.error_response

        return ""


def parse_agy_error(text: str) -> Optional[dict]:
    """Extract and parse structured AGY_ERROR payload from output or stderr stream.

    The Antigravity CLI emits:
    AGY_ERROR: {"canonical_status":..., "code":..., "retryable":..., "error_id":..., "short_error":...}
    on stderr and exits with code 3 on agent/model API failures.
    """
    if not text:
        return None

    for line in text.splitlines():
        if "AGY_ERROR:" in line:
            payload_str = line.split("AGY_ERROR:", 1)[1].strip()
            try:
                data = json.loads(payload_str)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
            m = re.search(r"\{.*?\}", payload_str)
            if m:
                try:
                    data = json.loads(m.group(0))
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
    return None


def format_agy_error_message(err: dict, elapsed_sec: int = 0, pid_str: str = "") -> str:
    """Format structured AGY_ERROR payload into an actionable Discord diagnostic."""
    canonical = err.get("canonical_status") or err.get("status") or "API_FAILURE"
    code = err.get("code") or err.get("http_code") or err.get("grpc_code") or ""
    retryable = err.get("retryable")
    error_id = err.get("error_id") or err.get("id") or ""
    msg = (
        err.get("short_error")
        or err.get("message")
        or err.get("error")
        or "Upstream model API failure"
    )

    header = "⚠️ **Model API Failure (CLI Exit Code 3):**"
    lines = [header, f"\n{msg}\n"]

    status_str = f"`{canonical}`"
    if code:
        status_str += f" (Code {code})"
    lines.append(f"• **Status:** {status_str}")

    if error_id:
        lines.append(f"• **Error ID:** `{error_id}`")
    if retryable is not None:
        lines.append(f"• **Retryable:** {'Yes' if retryable else 'No'}")
    if elapsed_sec:
        lines.append(f"• **Elapsed:** {elapsed_sec}s")
    if pid_str:
        lines.append(f"• **Process:** `{pid_str}`")

    return "\n".join(lines)
