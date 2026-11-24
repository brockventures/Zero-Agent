"""Harness adapters for BRIEF (headless agy CLI and deterministic dry-run)."""

from __future__ import annotations

import json
import subprocess
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


class HarnessError(RuntimeError):
    """Raised when a harness invocation fails, times out, or returns malformed output."""


class BriefHarness(ABC):
    """Abstract conversation harness for BRIEF event processing."""

    @abstractmethod
    def start_session(
        self,
        system_prompt: str,
        message: str,
        model: str | None = None,
    ) -> tuple[str, str]:
        """Start a new conversation and return `(conversation_id, assistant_text)`."""

    @abstractmethod
    def send_message(
        self,
        conv_id: str,
        message: str,
        model: str | None = None,
    ) -> str:
        """Send a message to an existing conversation and return `assistant_text`."""

    @abstractmethod
    def close_session(self, conv_id: str) -> None:
        """Close or release any harness resources associated with `conv_id`."""


def _extract_text_blocks(value: Any) -> str:
    """Extract text from string or structured content block lists."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                txt = item.get("text") or item.get("content")
                if isinstance(txt, str):
                    parts.append(txt)
        return "".join(parts)
    if isinstance(value, dict):
        txt = value.get("text") or value.get("content") or value.get("response")
        if isinstance(txt, str):
            return txt
    return ""


def parse_stream_json_output(raw_stdout: str) -> tuple[str | None, str, str | None]:
    """Parse NDJSON output from `agy --output-format=stream-json`.

    Returns `(conversation_id, assistant_text, error_message)` without failing on
    partial lines, status updates, or varied stream-json event schemas.
    """
    conv_id: str | None = None
    accumulated: list[str] = []
    last_message_text = ""
    final_result_text = ""
    error_message: str | None = None

    for raw_line in raw_stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        start = line.find("{")
        end = line.rfind("}") + 1
        if start == -1 or end <= start:
            continue

        try:
            event = json.loads(line[start:end])
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(event, dict):
            continue

        for cid_key in ("conversation_id", "session_id"):
            cid_val = event.get(cid_key)
            if isinstance(cid_val, str) and cid_val.strip():
                conv_id = cid_val.strip()
                break

        ev_type = event.get("event") or event.get("type")

        if ev_type == "result" or "result" in event:
            res = event.get("result")
            if isinstance(res, dict):
                for cid_key in ("conversation_id", "session_id"):
                    cid_val = res.get(cid_key)
                    if isinstance(cid_val, str) and cid_val.strip():
                        conv_id = cid_val.strip()
                        break
                if res.get("error"):
                    error_message = str(res["error"])
                for text_key in ("response", "text", "content", "output"):
                    val = _extract_text_blocks(res.get(text_key))
                    if val.strip():
                        final_result_text = val.strip()
                        break
            elif isinstance(res, str) and res.strip():
                final_result_text = res.strip()

            if (
                not final_result_text
                and isinstance(event.get("response"), str)
                and event["response"].strip()
            ):
                final_result_text = event["response"].strip()

        elif ev_type == "step_update" or "step_update" in event:
            step = event.get("step_update")
            if isinstance(step, dict):
                stype = step.get("step_type")
                tname = step.get("tool_name") or (step.get("tool_info") or {}).get("name")
                if stype in ("tool", "tool_call", "tool_use") or tname:
                    # Drop pre-tool narration deltas so final answer stays clean
                    accumulated.clear()
                elif stype == "agent_response":
                    delta = (
                        step.get("text_delta")
                        or step.get("text")
                        or step.get("content")
                    )
                    if isinstance(delta, str) and delta:
                        accumulated.append(delta)
                elif stype in ("error", "error_message"):
                    err = step.get("error") or step.get("message") or step.get("content")
                    if err:
                        error_message = str(err)

        elif ev_type in ("tool", "tool_call", "tool_use"):
            accumulated.clear()

        elif ev_type in ("error", "error_message"):
            err = event.get("error") or event.get("message") or event.get("content")
            if err:
                error_message = str(err)

        elif ev_type in ("assistant", "message", "content", "text", "delta"):
            msg_obj = event.get("message")
            extracted = ""
            if msg_obj is not None:
                extracted = _extract_text_blocks(
                    msg_obj.get("content") if isinstance(msg_obj, dict) else msg_obj
                )
            if not extracted:
                extracted = _extract_text_blocks(
                    event.get("delta") or event.get("content") or event.get("text")
                )
            if extracted:
                if ev_type in ("delta", "content", "text"):
                    accumulated.append(extracted)
                else:
                    last_message_text = extracted.strip()
                    accumulated.append(extracted)

    joined = "".join(accumulated).strip()
    assistant_text = final_result_text or joined or last_message_text
    return conv_id, assistant_text, error_message


class AgyCliHarness(BriefHarness):
    """Subprocess adapter invoking headless `agy --output-format=stream-json`."""

    def __init__(
        self,
        *,
        agy_bin: str = "agy",
        workspace_dir: Path | str = "/workspace",
        timeout_seconds: float = 180.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.agy_bin = agy_bin
        self.workspace_dir = str(workspace_dir)
        self.timeout_seconds = float(timeout_seconds)
        self._runner = runner or subprocess.run

    def _build_command(
        self,
        prompt: str,
        *,
        conv_id: str | None = None,
        model: str | None = None,
    ) -> list[str]:
        cmd = [
            self.agy_bin,
            f"--add-dir={self.workspace_dir}",
            f"-p={prompt}",
            "--output-format=stream-json",
            "--dangerously-skip-permissions",
        ]
        if conv_id:
            cmd.append(f"--conversation={conv_id}")
        if model:
            cmd.append(f"--model={model}")
        return cmd

    def _invoke(
        self,
        prompt: str,
        *,
        conv_id: str | None = None,
        model: str | None = None,
    ) -> tuple[str | None, str]:
        cmd: Sequence[str] = self._build_command(prompt, conv_id=conv_id, model=model)
        try:
            proc = self._runner(
                list(cmd),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(
                f"agy CLI timed out after {self.timeout_seconds:.1f}s"
            ) from exc
        except OSError as exc:
            raise HarnessError(f"Failed to invoke agy CLI ({self.agy_bin}): {exc}") from exc

        parsed_cid, text, stream_err = parse_stream_json_output(proc.stdout or "")
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or stream_err or text or "unknown error"
            raise HarnessError(
                f"agy CLI exited with code {proc.returncode}: {detail[:500]}"
            )

        if stream_err and not text:
            raise HarnessError(f"agy CLI stream error: {stream_err[:500]}")

        return parsed_cid or conv_id, text

    def start_session(
        self,
        system_prompt: str,
        message: str,
        model: str | None = None,
    ) -> tuple[str, str]:
        combined_prompt = (
            f"{system_prompt.strip()}\n\n{message.strip()}"
            if system_prompt.strip()
            else message.strip()
        )
        conv_id, text = self._invoke(combined_prompt, model=model)
        if not conv_id:
            raise HarnessError("agy CLI did not emit a conversation_id in stream-json output")
        return conv_id, text

    def send_message(
        self,
        conv_id: str,
        message: str,
        model: str | None = None,
    ) -> str:
        if not conv_id:
            raise ValueError("conv_id is required for send_message")
        _, text = self._invoke(message, conv_id=conv_id, model=model)
        return text

    def close_session(self, conv_id: str) -> None:
        # Headless agy CLI sessions are persisted by ID and require no explicit close call.
        return None


_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "ignore all instructions",
    "disregard previous",
    "system override",
    "reveal secrets",
    "forward all emails",
    "exfiltrate",
    "you are now",
    "bypass safety",
)

_CALENDAR_PATTERNS = (
    "reschedule",
    "calendar",
    "moved from",
    "new time",
    "scheduling conflict",
    "appointment",
    "meeting",
)

_PROMO_PATTERNS = (
    "unsubscribe",
    "flash sale",
    "% off",
    "promo",
    "newsletter",
    "limited time offer",
    "marketing",
)

_URGENT_INFORM_PATTERNS = (
    "urgent",
    "early dismissal",
    "school pickup",
    "pickup",
    "asap",
    "morning brief",
    "daily review",
    "morning_brief",
    "daily_review",
)


def _extract_envelope_parts(message: str) -> tuple[bool, str, str]:
    """Parse `(trusted, session_tag, event_body)` from a formatted event envelope."""
    trusted = False
    session_tag = ""
    for line in message.splitlines():
        stripped = line.strip()
        if stripped.startswith("[session:") and stripped.endswith("]"):
            session_tag = stripped[len("[session:") : -1].strip()
        elif stripped.startswith("[sender:") and "trusted:" in stripped:
            trusted = "trusted: true" in stripped.lower()

    begin_marker = "--- BEGIN EVENT ---"
    end_marker = "--- END EVENT ---"
    if begin_marker in message and end_marker in message:
        after_begin = message.split(begin_marker, 1)[1]
        body = after_begin.rsplit(end_marker, 1)[0].strip()
    else:
        body = message.strip()
    return trusted, session_tag, body


def _classify_dry_run_verdict(message: str) -> tuple[str, str]:
    trusted, _, body = _extract_envelope_parts(message)
    lower = body.lower()

    if not trusted and any(pat in lower for pat in _INJECTION_PATTERNS):
        return (
            "BLOCKED_INJECTION",
            "Untrusted event attempted instruction override; isolated as data only with zero side effects.",
        )

    if any(pat in lower for pat in _CALENDAR_PATTERNS):
        return (
            "PROPOSE_CALENDAR",
            "Read-only calendar proposal drafted for owner review in PT; no external calendar mutation performed.",
        )

    if any(pat in lower for pat in _PROMO_PATTERNS) and not any(
        pat in lower for pat in _URGENT_INFORM_PATTERNS
    ):
        return (
            "IGNORE",
            "Promotional or newsletter noise; ignored without notifying #brief.",
        )

    if any(pat in lower for pat in _URGENT_INFORM_PATTERNS) or trusted:
        return (
            "INFORM",
            "High-signal or trusted update summarized in PT for #brief.",
        )

    return (
        "IGNORE",
        "Low-signal external event; ignored with zero notifications.",
    )


class DryRunHarness(BriefHarness):
    """Deterministic offline harness returning structured JSON triage verdicts."""

    def __init__(self, *, id_prefix: str = "dryrun") -> None:
        self.id_prefix = id_prefix
        self._counter = 0
        self.sessions: dict[str, list[dict[str, Any]]] = {}

    def start_session(
        self,
        system_prompt: str,
        message: str,
        model: str | None = None,
    ) -> tuple[str, str]:
        self._counter += 1
        conv_id = f"{self.id_prefix}-{self._counter:04d}-{uuid.uuid4().hex[:6]}"
        verdict, summary = _classify_dry_run_verdict(message)
        _, session_tag, _ = _extract_envelope_parts(message)
        self.sessions[conv_id] = [
            {
                "turn": 1,
                "system_prompt": system_prompt,
                "message": message,
                "model": model,
                "verdict": verdict,
            }
        ]
        payload = {
            "verdict": verdict,
            "session": session_tag,
            "turn": 1,
            "model": model or "dry-run",
            "read_only": True,
            "summary": summary,
        }
        return conv_id, json.dumps(payload, sort_keys=True)

    def send_message(
        self,
        conv_id: str,
        message: str,
        model: str | None = None,
    ) -> str:
        if not conv_id:
            raise ValueError("conv_id is required for send_message")
        history = self.sessions.setdefault(conv_id, [])
        turn = len(history) + 1
        if "flush" in message.lower() and "rotation" in message.lower():
            verdict = "FLUSHED"
            summary = "Session memory flushed prior to rotation."
        else:
            verdict, summary = _classify_dry_run_verdict(message)
        _, session_tag, _ = _extract_envelope_parts(message)
        history.append(
            {
                "turn": turn,
                "message": message,
                "model": model,
                "verdict": verdict,
            }
        )
        payload = {
            "verdict": verdict,
            "session": session_tag,
            "turn": turn,
            "model": model or "dry-run",
            "read_only": True,
            "summary": summary,
        }
        return json.dumps(payload, sort_keys=True)

    def close_session(self, conv_id: str) -> None:
        self.sessions.pop(conv_id, None)


def check_harness_live(
    harness: BriefHarness | None = None,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Verify live harness start + resume roundtrip."""
    active_harness = harness or AgyCliHarness()
    conv_id, first_reply = active_harness.start_session(
        "You are running a BRIEF harness liveness check. Reply concisely with OK.",
        "Liveness step 1: reply with OK.",
        model=model,
    )
    second_reply = active_harness.send_message(
        conv_id,
        "Liveness step 2: confirm session resume by replying with RESUMED.",
        model=model,
    )
    active_harness.close_session(conv_id)
    return {
        "ok": bool(conv_id and first_reply.strip() and second_reply.strip()),
        "conversation_id": conv_id,
        "first_reply": first_reply,
        "second_reply": second_reply,
    }
