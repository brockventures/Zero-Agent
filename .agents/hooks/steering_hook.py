#!/usr/bin/env python3
"""
Hybrid Turn Steering Hook (Antigravity PreInvocation & PreToolUse)

Injects mid-turn Discord follow-up steers between tool steps via PreInvocation
and pauses stale mutating tool calls via PreToolUse using the atomic queue in
tools/bridge_steering.py.

Invariants:
- Stdlib-only, fast (<15ms).
- Strictly fail-OPEN ({} for PreInvocation, {"decision": "allow"} for PreToolUse)
  on any error or unexpected payload.
- Bypasses immediately when DISCORD_CHANNEL_ID is unset or inside FORGE workers.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

MUTATING_TOOLS = frozenset(
    {
        "write_file",
        "write_to_file",
        "edit_file",
        "replace_file_content",
        "run_command",
    }
)

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def _fail_open_response(event_name: str) -> dict[str, Any]:
    """Return the safe fail-open JSON object for the given hook event."""
    if event_name == "PreToolUse":
        return {"decision": "allow"}
    return {}


def _is_forge_worker_env(env: Mapping[str, str]) -> bool:
    """Return True when running inside a FORGE build/chunk worker."""
    return bool(
        str(env.get("FORGE_BUILD_ID", "")).strip()
        or str(env.get("FORGE_CHUNK_INDEX", "")).strip()
    )


def _resolve_event_name(payload: dict[str, Any]) -> str:
    """Extract hookEventName, inferring PreToolUse if toolCall is present."""
    event_name = str(payload.get("hookEventName") or "").strip()
    if not event_name and isinstance(payload.get("toolCall"), dict):
        return "PreToolUse"
    return event_name


def _load_steering_helpers() -> tuple[Callable[..., list[dict[str, Any]]], Callable[[list[dict[str, Any]]], str]]:
    """Lazily import steering queue helpers from tools.bridge_steering."""
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from tools.bridge_steering import (
        consume_pending_steers,
        format_hook_steer_message,
    )

    return consume_pending_steers, format_hook_steer_message


def handle_hook_payload(
    payload: dict[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Process a parsed Antigravity hook payload and return the hook response dict."""
    env_map = os.environ if env is None else env
    event_name = _resolve_event_name(payload)

    channel_id = str(env_map.get("DISCORD_CHANNEL_ID", "")).strip()
    if not channel_id or _is_forge_worker_env(env_map):
        return _fail_open_response(event_name)

    if event_name == "PreToolUse":
        tool_call = payload.get("toolCall")
        tool_name = str(tool_call.get("name") or "").strip() if isinstance(tool_call, dict) else ""
        if tool_name not in MUTATING_TOOLS:
            return {"decision": "allow"}

        consume_pending_steers, format_hook_steer_message = _load_steering_helpers()
        entries = consume_pending_steers(channel_id, consumer="PreToolUse")
        if entries:
            return {
                "decision": "deny",
                "reason": (
                    "Paused stale mutating tool call for incoming user steering update. "
                    "Pivot immediately:\n\n" + format_hook_steer_message(entries)
                ),
            }
        return {"decision": "allow"}

    if event_name == "PreInvocation":
        consume_pending_steers, format_hook_steer_message = _load_steering_helpers()
        entries = consume_pending_steers(channel_id, consumer="PreInvocation")
        if entries:
            return {
                "injectSteps": [{"userMessage": format_hook_steer_message(entries)}],
                "terminationBehavior": "",
            }
        return {}

    return _fail_open_response(event_name)


def main() -> None:
    """CLI entrypoint for Antigravity PreInvocation and PreToolUse hooks."""
    raw = ""
    event_name = ""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({}))
            return

        payload = json.loads(raw)
        if not isinstance(payload, dict):
            print(json.dumps({}))
            return

        event_name = _resolve_event_name(payload)
        resp = handle_hook_payload(payload)
        print(json.dumps(resp))
    except Exception:  # noqa: BLE001 - Hook must strictly fail open on any error
        if not event_name and '"PreToolUse"' in raw and '"PreInvocation"' not in raw:
            event_name = "PreToolUse"
        print(json.dumps(_fail_open_response(event_name)))


if __name__ == "__main__":
    main()
