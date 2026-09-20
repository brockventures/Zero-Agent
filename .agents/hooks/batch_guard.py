#!/usr/bin/env python3
"""
Tool Batching Safety Guard (Antigravity PreToolUse Hook)
Enforces the Tool Roundtrip Minimization & Batch Execution Invariant.
Detects and blocks excessive consecutive single-line inspection commands,
forcing the agent to bundle multi-step diagnostics into scratch scripts or compound one-liners.
"""
import sys
import os
import json
import time
import re

STATE_FILE = "/tmp/batch_guard_state.json"
MAX_CONSECUTIVE_INSPECTIONS = 3
TURN_RESET_IDLE_SECONDS = 45.0

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"count": 0, "last_time": 0.0}


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def is_batched_or_mutating(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    if " && " in cmd_clean or " || " in cmd_clean or "; " in cmd_clean or "\n" in cmd_clean:
        return True
    if "scratch/" in cmd_clean or "test_" in cmd_clean or "pytest" in cmd_clean:
        return True
    if re.search(r"python[0-9.]*\s+(-c|-|/workspace/)", cmd_clean):
        return True
    if re.search(r"\b(git|touch|mkdir|rm|cp|mv|chmod|kill|pkill|restart|reboot|compose)\b", cmd_clean):
        return True
    return False


def is_single_inspection(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    ssh_match = re.search(r"ssh\s+.*?(['\"])(.*?)\1\s*$", cmd_clean)
    target_cmd = ssh_match.group(2).strip() if ssh_match else cmd_clean
    if is_batched_or_mutating(target_cmd):
        return False
    inspection_starters = (
        "docker ps", "docker inspect", "docker logs",
        "cat", "head", "tail", "grep", "rg", "ag", "find",
        "ls", "df", "free", "uptime", "vmstat", "iostat",
        "crontab", "ss", "netstat", "ps", "which", "echo"
    )
    tokens = target_cmd.split()
    first_word = tokens[0] if tokens else ""
    first_two = " ".join(tokens[:2]) if len(tokens) >= 2 else ""
    return first_word in inspection_starters or first_two in inspection_starters


def check_batching(cmd: str) -> tuple[bool, str]:
    now = time.time()
    state = load_state()
    if now - state.get("last_time", 0.0) > TURN_RESET_IDLE_SECONDS:
        state["count"] = 0
    state["last_time"] = now
    if is_single_inspection(cmd):
        state["count"] += 1
        if state["count"] > MAX_CONSECUTIVE_INSPECTIONS:
            save_state(state)
            return False, (
                f"Batch Invariant Triggered: Detected {state['count']} consecutive single-line inspection commands in serial. "
                "Please bundle your diagnostic checks into a compound one-liner or a single Python scratch script in /workspace/scratch/ "
                "to minimize tool roundtrips."
            )
    else:
        state["count"] = 0
    save_state(state)
    return True, ""


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({"decision": "allow"}))
            return
        payload = json.loads(raw)
        tool_call = payload.get("toolCall", {})
        if tool_call.get("name") == "run_command":
            cmd = tool_call.get("args", {}).get("CommandLine", "")
            allowed, reason = check_batching(cmd)
            if not allowed:
                print(json.dumps({"decision": "deny", "reason": reason}))
                return
        print(json.dumps({"decision": "allow"}))
    except Exception:
        print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()
