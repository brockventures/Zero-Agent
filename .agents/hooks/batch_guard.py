#!/usr/bin/env python3
"""
Tool Batching Safety Guard (Antigravity PreToolUse Hook)
Enforces the Tool Roundtrip Minimization & Batch Execution Invariant across:
1. Shell inspection commands (run_command).
2. Repetitive file reading / small line slicing (view_file).
3. Excessive unverified single-file replacements (replace_file_content).

Forces the agent to:
- Bundle shell diagnostics into compound commands or Python scratch scripts in /workspace/scratch/.
- Read files in full (up to 800 lines) instead of small 30-50 line slices.
- Bundle multi-file checks into scratch scripts.
- Use write_to_file or script-based transforms for extensive file modifications.
"""
import sys
import os
import json
import time
import re

STATE_FILE = "/tmp/batch_guard_state.json"
MAX_CONSECUTIVE_SHELL_INSPECTIONS = 3
MAX_CONSECUTIVE_SAME_FILE_VIEWS = 2
MAX_CONSECUTIVE_FILE_VIEWS = 4
MAX_CONSECUTIVE_SAME_FILE_EDITS = 3
TURN_RESET_IDLE_SECONDS = 45.0


def load_state(state_file: str = STATE_FILE) -> dict:
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "last_time": 0.0,
        "cmd_inspections": 0,
        "consecutive_file_views": 0,
        "last_viewed_file": "",
        "same_file_view_count": 0,
        "last_edited_file": "",
        "same_file_edit_count": 0,
    }


def save_state(state: dict, state_file: str = STATE_FILE):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def is_batched_or_mutating_cmd(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    if " && " in cmd_clean or " || " in cmd_clean or "; " in cmd_clean or "\n" in cmd_clean:
        return True
    if "scratch/" in cmd_clean or "test_" in cmd_clean or "pytest" in cmd_clean:
        return True
    if re.search(r"python[0-9.]*\s+(-c|-|/workspace/|/root/)", cmd_clean):
        return True
    if re.search(r"\b(git|touch|mkdir|rm|cp|mv|chmod|kill|pkill|restart|reboot|compose)\b", cmd_clean):
        return True
    return False


def is_single_shell_inspection(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    ssh_match = re.search(r"ssh\s+.*?(['\"])(.*?)\1\s*$", cmd_clean)
    target_cmd = ssh_match.group(2).strip() if ssh_match else cmd_clean
    if is_batched_or_mutating_cmd(target_cmd):
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


def check_tool_use(name: str, args: dict, state_file: str = STATE_FILE) -> tuple[bool, str]:
    now = time.time()
    state = load_state(state_file)

    # Reset state on idle turn boundary
    if now - state.get("last_time", 0.0) > TURN_RESET_IDLE_SECONDS:
        state = {
            "last_time": now,
            "cmd_inspections": 0,
            "consecutive_file_views": 0,
            "last_viewed_file": "",
            "same_file_view_count": 0,
            "last_edited_file": "",
            "same_file_edit_count": 0,
        }
    state["last_time"] = now

    # 1. run_command check
    if name == "run_command":
        cmd = args.get("CommandLine", "")
        if is_batched_or_mutating_cmd(cmd):
            # Batch script, tests, or mutating commands reset all warning counters
            state["cmd_inspections"] = 0
            state["consecutive_file_views"] = 0
            state["same_file_view_count"] = 0
            state["same_file_edit_count"] = 0
            save_state(state, state_file)
            return True, ""

        if is_single_shell_inspection(cmd):
            state["cmd_inspections"] = state.get("cmd_inspections", 0) + 1
            if state["cmd_inspections"] > MAX_CONSECUTIVE_SHELL_INSPECTIONS:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['cmd_inspections']} consecutive single-line inspection commands in serial. "
                    "Please bundle your diagnostic checks into a compound one-liner or a single Python scratch script in /workspace/scratch/ "
                    "to minimize tool roundtrips."
                )
        else:
            state["cmd_inspections"] = 0

        save_state(state, state_file)
        return True, ""

    # 2. view_file check
    if name == "view_file":
        target_file = args.get("AbsolutePath", "")
        state["cmd_inspections"] = 0

        # Check repetitive same-file views
        if target_file and target_file == state.get("last_viewed_file"):
            state["same_file_view_count"] = state.get("same_file_view_count", 0) + 1
            if state["same_file_view_count"] > MAX_CONSECUTIVE_SAME_FILE_VIEWS:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['same_file_view_count']} consecutive view_file calls on '{target_file}'. "
                    "view_file supports viewing up to 800 lines in a single call. Omit StartLine/EndLine to view the file in one shot, "
                    "or write a targeted Python scratch script in /workspace/scratch/ to inspect AST/regexes in a single pass."
                )
        else:
            state["last_viewed_file"] = target_file
            state["same_file_view_count"] = 1

        # Check total consecutive file views across files
        state["consecutive_file_views"] = state.get("consecutive_file_views", 0) + 1
        if state["consecutive_file_views"] > MAX_CONSECUTIVE_FILE_VIEWS:
            save_state(state, state_file)
            return False, (
                f"Batch Invariant Triggered: Detected {state['consecutive_file_views']} consecutive serial view_file calls. "
                "Please bundle multi-file inspections into a single Python scratch script in /workspace/scratch/ "
                "to inspect multiple files in a single pass."
            )

        save_state(state, state_file)
        return True, ""

    # 3. replace_file_content check
    if name == "replace_file_content":
        target_file = args.get("TargetFile", "")
        state["cmd_inspections"] = 0
        state["consecutive_file_views"] = 0
        state["same_file_view_count"] = 0

        if target_file and target_file == state.get("last_edited_file"):
            state["same_file_edit_count"] = state.get("same_file_edit_count", 0) + 1
            if state["same_file_edit_count"] > MAX_CONSECUTIVE_SAME_FILE_EDITS:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['same_file_edit_count']} consecutive replace_file_content calls on '{target_file}'. "
                    "For extensive edits or multiple modifications in the same file, use write_to_file or a Python script in /workspace/scratch/ "
                    "to apply changes in a single pass."
                )
        else:
            state["last_edited_file"] = target_file
            state["same_file_edit_count"] = 1

        save_state(state, state_file)
        return True, ""

    # 4. write_to_file resets everything
    if name == "write_to_file":
        state = {
            "last_time": now,
            "cmd_inspections": 0,
            "consecutive_file_views": 0,
            "last_viewed_file": "",
            "same_file_view_count": 0,
            "last_edited_file": "",
            "same_file_edit_count": 0,
        }
        save_state(state, state_file)
        return True, ""

    save_state(state, state_file)
    return True, ""


# Backward compatibility for existing test suite
def check_batching(cmd: str) -> tuple[bool, str]:
    return check_tool_use("run_command", {"CommandLine": cmd})


def is_single_inspection(cmd: str) -> bool:
    return is_single_shell_inspection(cmd)


def is_batched_or_mutating(cmd: str) -> bool:
    return is_batched_or_mutating_cmd(cmd)


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print(json.dumps({"decision": "allow"}))
            return
        payload = json.loads(raw)
        tool_call = payload.get("toolCall", {})
        name = tool_call.get("name", "")
        args = tool_call.get("args", {})

        allowed, reason = check_tool_use(name, args)
        if not allowed:
            print(json.dumps({"decision": "deny", "reason": reason}))
            return

        print(json.dumps({"decision": "allow"}))
    except Exception:
        # Failsafe: never crash the agent turn on hook bug
        print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()
