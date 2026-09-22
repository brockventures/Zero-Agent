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


def harvest_transcript_response(conv_id: Optional[str]) -> Optional[str]:
    """Harvest completed response from transcript files if stdout was truncated or cut off.

    Enforces active turn boundary: stops immediately if a USER_INPUT or CHECKPOINT step is
    encountered before finding a substantive PLANNER_RESPONSE, preventing prior turn responses
    from ever being harvested or re-delivered.
    """
    if not conv_id:
        return None
    brain_dir = Path("/root/.gemini/antigravity-cli/brain") / str(conv_id) / ".system_generated" / "logs"
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

                    if step_type == "PLANNER_RESPONSE":
                        content = data.get("content")
                        if content and isinstance(content, str) and content.strip():
                            stripped = content.strip()
                            if is_internal_cli_leak(stripped):
                                continue
                            return stripped
                except Exception:
                    continue
        except Exception as e:
            print(f"[BridgeStream] Error reading transcript {tpath}: {e}")
    return None


class AgyStreamParser:
    """Deterministic state-machine parser for agy stream-json output."""

    def __init__(self, conv_id: Optional[str] = None):
        self.conv_id = conv_id
        self.accumulated_segment: list[str] = []
        self.last_substantive_response: str = ""
        self.final_result_response: str = ""
        self.error_response: str = ""
        self.is_explicit_silence: bool = False

    @staticmethod
    def _is_silence_or_placeholder(text: str) -> bool:
        if not text or not isinstance(text, str):
            return True
        return is_internal_cli_leak(text)

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
                if res.get("response"):
                    self.final_result_response = res["response"]
                if res.get("error"):
                    self.error_response = f"Error: {res.get('error')}"
                if res.get("conversation_id"):
                    self.conv_id = res["conversation_id"]
            elif isinstance(event.get("response"), str):
                self.final_result_response = event["response"]

        # 3. Step update event
        elif ev_type == "step_update" or "step_update" in event:
            step = event.get("step_update", {})
            if not isinstance(step, dict):
                return

            stype = step.get("step_type")
            tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")

            if (stype in ("tool",) or tname):
                # Tool execution: clear pre-tool narration
                self.accumulated_segment.clear()
                self.is_explicit_silence = False

            elif stype in ("system_message", "system"):
                # Asynchronous system event: preserve substantive content generated prior
                curr = "".join(self.accumulated_segment).strip()
                if curr and not self._is_silence_or_placeholder(curr):
                    self.last_substantive_response = curr
                self.accumulated_segment.clear()

            elif stype == "agent_response":
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
                            self.last_substantive_response = curr
                            self.is_explicit_silence = False

        elif ev_type in ("tool", "tool_call", "tool_use"):
            self.accumulated_segment.clear()
            self.is_explicit_silence = False

        elif ev_type in ("system_message", "system"):
            curr = "".join(self.accumulated_segment).strip()
            if curr and not self._is_silence_or_placeholder(curr):
                self.last_substantive_response = curr
            self.accumulated_segment.clear()

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

        # 1. Check current segment after last tool
        if curr and not self._is_silence_or_placeholder(curr):
            clean = re.sub(
                r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$", "", curr, flags=re.IGNORECASE
            ).strip()
            clean = strip_internal_cli_chatter(clean)
            if clean and not self._is_silence_or_placeholder(clean):
                return clean

        # 2. Check last substantive response before an asynchronous system message
        if self.last_substantive_response and not self._is_silence_or_placeholder(
            self.last_substantive_response
        ):
            clean = re.sub(
                r"(?:^|\n+)\s*\[(?:NO_REPLY|NO_OP)\]\s*$",
                "",
                self.last_substantive_response,
                flags=re.IGNORECASE,
            ).strip()
            clean = strip_internal_cli_chatter(clean)
            if clean and not self._is_silence_or_placeholder(clean):
                return clean

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
