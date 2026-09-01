#!/usr/bin/env python3
"""
Hardened Search Safety Guard (Antigravity PreToolUse Hook)
Deterministically intercepts and blocks un-scoped, root-level, and bulk-data searches
across grep_search, find_by_name, and shell commands to prevent container crashes.
"""
import sys
import os
import json
import re

BLOCKED_EXACT_ROOTS = {
    "",
    "/",
    "/workspace",
    "/workspace/data",
    "/workspace/car_monitor_data",
    "/root",
    "/docker",
    "/data",
    "/archives"
}

BLOCKED_TREE_PREFIXES = (
    "/workspace/data/",
    "/workspace/car_monitor_data/",
    "/archives/",
    "/root/",
    "/docker/",
)

SAFE_EXEMPT_FILES = {
    "/workspace/data/gif_history.json",
    "/workspace/data/schedule.json",
    "/workspace/data/reminders.json",
    "/workspace/data/channel_history.json",
    "/workspace/data/topic_tracker.json",
    "/workspace/data/steam_games.json",
    "/workspace/data/friends_and_family_master.csv",
    "/workspace/data/friends_and_family_from_sheet.csv",
}

def normalize_path(path_str: str, cwd: str = "/workspace") -> str:
    if not path_str:
        return ""
    expanded = os.path.expanduser(path_str.strip())
    if not os.path.isabs(expanded):
        expanded = os.path.normpath(os.path.join(cwd, expanded))
    else:
        expanded = os.path.normpath(expanded)
    return expanded

def check_search_path(target_path: str, cwd: str) -> tuple[bool, str]:
    if not target_path:
        return False, "Search path is empty. Please specify a targeted subdirectory (e.g. /workspace/tools)."
    
    norm = normalize_path(target_path, cwd)

    if norm in SAFE_EXEMPT_FILES:
        return True, ""

    if norm in BLOCKED_EXACT_ROOTS:
        return False, (
            f"Search Blocked: Unscoped search on '{target_path}' (resolved: '{norm}') is prohibited. "
            f"Please scope SearchPath to a dedicated subdirectory (e.g. /workspace/tools, /workspace/config, /workspace/.agents, /workspace/memory)."
        )

    for prefix in BLOCKED_TREE_PREFIXES:
        if norm.startswith(prefix.rstrip("/")):
            if os.path.isfile(norm):
                return True, ""
            return False, (
                f"Search Blocked: Recursive search inside '{norm}' is prohibited because this directory contains bulk data/archives. "
                f"Inspect specific files directly or use dedicated accessor scripts in /workspace/tools."
            )

    return True, ""

def check_command_line(cmd: str, cwd: str) -> tuple[bool, str]:
    if not cmd:
        return True, ""

    # 1. Detect recursive grep/rg/ag/ack patterns
    has_recursive_grep = bool(re.search(r'\b(grep\s+-[a-zA-Z]*r[a-zA-Z]*|rg|ag|ack)\b', cmd))
    has_find = bool(re.search(r'\bfind\b', cmd))
    has_git_grep = bool(re.search(r'\bgit\s+grep\b', cmd))

    if not (has_recursive_grep or has_find or has_git_grep):
        return True, ""

    # 2. Check find commands
    if has_find:
        if re.search(r'\bfind\s+(/|/workspace|\.|\*)\b', cmd) and "-maxdepth" not in cmd:
            norm_cwd = normalize_path(".", cwd)
            if norm_cwd in BLOCKED_EXACT_ROOTS or "-name" not in cmd:
                return False, (
                    f"Command Blocked: 'find' on root directory or without '-maxdepth' is prohibited. "
                    f"Always bound find commands (e.g., 'find /workspace/tools -maxdepth 2 -name \"*.py\"')."
                )

    # 3. Check recursive grep / rg / ag
    if has_recursive_grep:
        tokens = cmd.split()
        # Check if there is an explicit target or if it defaults to cwd
        targets = []
        for token in tokens:
            cleaned = token.strip('"\'')
            if cleaned.startswith("-"):
                continue
            if cleaned in ("grep", "rg", "ag", "ack"):
                continue
            # Assume any non-flag token could be query or target path
            norm = normalize_path(cleaned, cwd)
            if norm in BLOCKED_EXACT_ROOTS:
                return False, (
                    f"Command Blocked: Unbounded recursive search targeting '{cleaned}' (resolved: '{norm}') is prohibited. "
                    f"Target a specific subfolder: e.g. 'grep -rn \"pattern\" /workspace/tools/'."
                )

    return True, ""

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
        cwd = payload.get("cwd", "/workspace")

        if name == "grep_search":
            search_path = args.get("SearchPath", "")
            allowed, reason = check_search_path(search_path, cwd)
            if not allowed:
                print(json.dumps({"decision": "deny", "reason": reason}))
                return

        elif name == "find_by_name":
            search_dir = args.get("SearchDirectory", "")
            max_depth = args.get("MaxDepth")
            allowed, reason = check_search_path(search_dir, cwd)
            if not allowed:
                if normalize_path(search_dir, cwd) == "/workspace" and max_depth is not None and int(max_depth) <= 2:
                    pass
                else:
                    print(json.dumps({"decision": "deny", "reason": reason}))
                    return

        elif name == "run_command":
            cmd = args.get("CommandLine", "")
            cmd_cwd = args.get("Cwd", cwd)
            allowed, reason = check_command_line(cmd, cmd_cwd)
            if not allowed:
                print(json.dumps({"decision": "deny", "reason": reason}))
                return

        print(json.dumps({"decision": "allow"}))

    except Exception as e:
        print(json.dumps({"decision": "deny", "reason": f"Safety Guard Evaluation Error: {str(e)}"}))

if __name__ == "__main__":
    main()
