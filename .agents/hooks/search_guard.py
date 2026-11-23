#!/usr/bin/env python3
"""
Hardened Search Safety Guard (Antigravity PreToolUse Hook)
Deterministically intercepts and blocks un-scoped, root-level, and bulk-data searches
across grep_search, find_by_name, and shell commands to prevent container crashes.
"""
import json
import os
import posixpath
import re
import sys

BLOCKED_EXACT_ROOTS = {
    "",
    "/",
    "/workspace",
    "/workspace/data",
    "/workspace/car_monitor_data",
    "/root",
    "/docker",
    "/data",
    "/archives",
    "/volume1",
    "/volume2",
    "/volume3",
    "/volume4",
    "/volumeUSB1",
    "/volumeUSB2",
}

BLOCKED_TREE_PREFIXES = (
    "/workspace/data/",
    "/workspace/car_monitor_data/",
    "/archives/",
    "/root/",
    "/docker/",
)

_DU_VALUE_FLAGS = {
    "-d",
    "--max-depth",
    "-B",
    "--block-size",
    "--exclude",
    "-X",
    "--exclude-from",
    "--time",
    "--time-style",
    "--threshold",
    "-t",
}

_SUDO_VALUE_FLAGS = {"-u", "-g", "-C", "-D", "-R", "-T", "-U", "-h", "-p", "-r", "-t"}
_SSH_VALUE_FLAGS = {
    "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J",
    "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}
_NAS_DU_PATH_RE = re.compile(r"/volume(?:USB)?[0-9]+/(?:docker|data)(?:/|$|\*)", re.IGNORECASE)
_NAS_DU_VOLUME_ROOT_RE = re.compile(r"^/volume(?:USB)?[0-9]+$", re.IGNORECASE)
_NAS_AGENT_PATH_RE = re.compile(
    r"(?:^|/)(?:discord-agy-agent|discord-vulcan-agent|discord-ivy-agent)(?:/|$|\*)",
    re.IGNORECASE,
)
_NAS_DOCKER_VAR_RE = re.compile(r"\$(?:\{)?(?:NAS_)?DOCKER_ROOT(?:\})?(?=/|$|\*)")

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

def _strip_outer_quotes(tok: str) -> str:
    s = tok.strip()
    if len(s) >= 2 and s[0] == s[-1]:
        if s[0] == "'":
            return s[1:-1]
        if s[0] == '"':
            return s[1:-1].replace('\\"', '"')
    return s


def _split_shell_segments(cmd: str) -> list[str]:
    """Split a shell command on unquoted ';', newline, '&&', '||', '|', '&'."""
    joined = re.sub(r"\\\r?\n", " ", cmd)
    segments: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i = 0
    n = len(joined)
    while i < n:
        ch = joined[i]
        if quote is None:
            if ch == "\\" and i + 1 < n:
                cur.append(joined[i : i + 2])
                i += 2
                continue
            if ch in ("'", '"'):
                quote = ch
                cur.append(ch)
                i += 1
                continue
            if ch in (";", "\n"):
                seg = "".join(cur).strip()
                if seg:
                    segments.append(seg)
                cur = []
                i += 1
                continue
            if ch in ("|", "&") and i + 1 < n and joined[i + 1] == ch:
                seg = "".join(cur).strip()
                if seg:
                    segments.append(seg)
                cur = []
                i += 2
                continue
            if ch == "|":
                prev_ch = joined[i - 1] if i > 0 else ""
                if prev_ch != ">":
                    seg = "".join(cur).strip()
                    if seg:
                        segments.append(seg)
                    cur = []
                    i += 1
                    continue
            if ch == "&":
                prev_ch = joined[i - 1] if i > 0 else ""
                next_ch = joined[i + 1] if i + 1 < n else ""
                if prev_ch not in (">", "<") and next_ch != ">":
                    seg = "".join(cur).strip()
                    if seg:
                        segments.append(seg)
                    cur = []
                    i += 1
                    continue
            cur.append(ch)
            i += 1
        elif quote == "'":
            if ch == "'":
                quote = None
            cur.append(ch)
            i += 1
        else:
            if ch == "\\" and i + 1 < n:
                cur.append(joined[i : i + 2])
                i += 2
                continue
            if ch == '"':
                quote = None
            cur.append(ch)
            i += 1
    tail = "".join(cur).strip()
    if tail:
        segments.append(tail)
    return segments


def _strip_command_wrappers(tokens: list[str]) -> list[str]:
    """Strip leading env assignments, sudo, timeout, nice, ionice, nohup, and shell keywords."""
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = _strip_outer_quotes(tokens[idx])
        if tok in ("do", "then", "else", "elif", "{", "!"):
            idx += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", tok):
            idx += 1
            continue
        if tok == "sudo":
            idx += 1
            while idx < n:
                st = _strip_outer_quotes(tokens[idx])
                if st in _SUDO_VALUE_FLAGS:
                    idx += 2
                    continue
                if st == "--":
                    idx += 1
                    break
                if st.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        if tok == "timeout":
            idx += 1
            while idx < n:
                st = _strip_outer_quotes(tokens[idx])
                if st in ("-k", "--kill-after", "-s", "--signal"):
                    idx += 2
                    continue
                if st == "--":
                    idx += 1
                    break
                if st.startswith("-"):
                    idx += 1
                    continue
                idx += 1  # duration argument
                break
            continue
        if tok in ("nice", "ionice"):
            idx += 1
            while idx < n:
                st = _strip_outer_quotes(tokens[idx])
                if st in ("-n", "--adjustment", "-c", "--class", "--classdata"):
                    idx += 2
                    continue
                if st.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        if tok in ("env", "nohup", "command", "builtin", "stdbuf", "xargs"):
            idx += 1
            while idx < n:
                st = _strip_outer_quotes(tokens[idx])
                if st in ("-u", "-C", "-S", "-o", "-i", "-e"):
                    idx += 2
                    continue
                if st.startswith("-") or (tok == "env" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", st)):
                    idx += 1
                    continue
                break
            continue
        break
    return tokens[idx:]


def _extract_nested_commands(cmd: str) -> list[str]:
    """Extract subshell commands from $(...), backticks, and sh/bash -c payloads."""
    nested: list[str] = []
    for m in re.finditer(r"\$\(([^()]+)\)", cmd):
        val = m.group(1).strip()
        if val:
            nested.append(val)
    for m in re.finditer(r"`([^`]+)`", cmd):
        val = m.group(1).strip()
        if val:
            nested.append(val)
    for m in re.finditer(
        r"\b(?:bash|sh|zsh|dash)\s+(?:-[a-zA-Z]+\s+)*-c(?:=|\s+)(['\"])([\s\S]*?)\1",
        cmd,
    ):
        val = m.group(2).replace('\\"', '"').strip() if m.group(1) == '"' else m.group(2).strip()
        if val:
            nested.append(val)
    return nested


def _extract_ssh_remote_command(cmd_tokens: list[str]) -> str | None:
    """Given tokenized command starting with 'ssh', extract the remote command string if present."""
    idx = 1
    n = len(cmd_tokens)
    while idx < n:
        tok = _strip_outer_quotes(cmd_tokens[idx])
        if tok == "--":
            idx += 1
            break
        if tok in _SSH_VALUE_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        break
    if idx >= n:
        return None
    # Skip destination (user@host)
    remote_tokens = cmd_tokens[idx + 1 :]
    if not remote_tokens:
        return None
    if len(remote_tokens) == 1:
        return _strip_outer_quotes(remote_tokens[0]).strip()
    return " ".join(_strip_outer_quotes(t) for t in remote_tokens).strip()


def _is_blocked_du_target(raw_target: str, active_cwd: str) -> tuple[bool, str]:
    """Return (True, label) if a 'du' target walks a blocked root, bulk prefix, or NAS docker/agent tree."""
    clean = _strip_outer_quotes(raw_target).strip()
    if not clean:
        return False, ""

    if _NAS_DOCKER_VAR_RE.search(clean):
        return True, clean

    if _NAS_DU_PATH_RE.search(clean) or _NAS_AGENT_PATH_RE.search(clean):
        return True, clean

    # Resolve relative target against active_cwd when available
    if clean in (".", "./"):
        if not active_cwd:
            return False, ""
        candidate = active_cwd
    elif clean.startswith("~"):
        candidate = os.path.expanduser(clean)
    elif clean.startswith("/"):
        candidate = clean
    elif active_cwd:
        candidate = active_cwd.rstrip("/") + "/" + clean
    else:
        candidate = clean

    if _NAS_DU_PATH_RE.search(candidate) or _NAS_AGENT_PATH_RE.search(candidate):
        return True, candidate

    # If target contains wildcards, evaluate the concrete parent directory before the first glob
    check_paths = []
    if any(ch in candidate for ch in ("*", "?", "[")):
        prefix_before_glob = re.split(r"[*?[]", candidate, maxsplit=1)[0].rstrip("/")
        if not prefix_before_glob:
            prefix_before_glob = "/" if candidate.startswith("/") else (active_cwd or "")
        if prefix_before_glob:
            check_paths.append(prefix_before_glob)
    else:
        check_paths.append(candidate)

    for path_item in check_paths:
        if not path_item:
            continue
        if path_item.startswith("/"):
            norm = posixpath.normpath(path_item)
        elif active_cwd:
            norm = normalize_path(path_item, active_cwd)
        else:
            continue

        if norm in BLOCKED_EXACT_ROOTS or _NAS_DU_VOLUME_ROOT_RE.match(norm):
            return True, clean if clean not in (".", "./", "*") else f"{clean}' (resolved: '{norm})"
        if _NAS_DU_PATH_RE.search(norm) or _NAS_AGENT_PATH_RE.search(norm):
            return True, norm
        for prefix in BLOCKED_TREE_PREFIXES:
            prefix_clean = prefix.rstrip("/")
            if norm == prefix_clean or norm.startswith(prefix):
                return True, norm

    return False, ""


def _check_du_command(cmd: str, cwd: str, depth: int = 0) -> tuple[bool, str]:
    """Detect unbounded 'du' disk-usage walks including inside ssh, cd && du, and $(du ...)."""
    if not cmd or depth > 3:
        return True, ""

    # Inspect any nested subshells / bash -c payloads first
    for nested in _extract_nested_commands(cmd):
        allowed, reason = _check_du_command(nested, cwd, depth=depth + 1)
        if not allowed:
            return False, reason

    active_cwd = cwd
    for seg in _split_shell_segments(cmd):
        clean_seg = seg.strip().lstrip("(").rstrip(")").strip()
        if not clean_seg:
            continue

        # Also check nested subshells inside the segment (e.g. SIZE=$(du -sh ...))
        raw_tokens = re.findall(r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\S+)", clean_seg)
        cmd_tokens = _strip_command_wrappers(raw_tokens)
        if not cmd_tokens:
            continue

        exe = posixpath.basename(_strip_outer_quotes(cmd_tokens[0]))

        if exe == "cd" and len(cmd_tokens) >= 2:
            cd_target = _strip_outer_quotes(cmd_tokens[1])
            if cd_target and not cd_target.startswith("-"):
                if cd_target.startswith("~"):
                    active_cwd = os.path.expanduser(cd_target)
                elif cd_target.startswith(("/", "$")):
                    active_cwd = cd_target
                elif active_cwd:
                    active_cwd = normalize_path(cd_target, active_cwd)
                else:
                    active_cwd = cd_target
            continue

        if exe == "ssh":
            remote_cmd = _extract_ssh_remote_command(cmd_tokens)
            if remote_cmd:
                allowed, reason = _check_du_command(remote_cmd, "", depth=depth + 1)
                if not allowed:
                    return False, reason
            continue

        if exe == "find":
            m_find_du = re.search(
                r"\bfind\s+('[^']*'|\"[^\"]*\"|\S+)[^\n;|&]*?\s-exec\s+(?:\S+/)?du\b",
                clean_seg,
            )
            if m_find_du:
                blocked, label = _is_blocked_du_target(m_find_du.group(1), active_cwd)
                if blocked:
                    return False, (
                        f"Command Blocked: Unbounded 'du' disk-usage walk targeting '{label}' is prohibited. "
                        "Even summary flags like 'du -sh' or '--max-depth=0' still recursively walk every inode and stall spinning NAS HDDs. "
                        "Use 'df -h' for filesystem capacity or targeted metadata checks ('stat' / 'ls -lh' on specific files) instead."
                    )
            continue

        if exe != "du":
            continue

        after_double_dash = False
        targets: list[str] = []
        idx = 1
        while idx < len(cmd_tokens):
            raw_tok = cmd_tokens[idx]
            if re.match(r"^(?:\d*>>?|\d*<|&>>?)$", raw_tok):
                idx += 2
                continue
            if re.match(r"^(?:\d*>>?|\d*<|&>>?)", raw_tok):
                idx += 1
                continue
            tok = _strip_outer_quotes(raw_tok)
            if not after_double_dash:
                if tok == "--":
                    after_double_dash = True
                    idx += 1
                    continue
                if tok in _DU_VALUE_FLAGS:
                    idx += 2
                    continue
                if tok.startswith("-"):
                    idx += 1
                    continue
            if tok:
                targets.append(raw_tok)
            idx += 1

        if not targets:
            targets.append(".")

        for target in targets:
            blocked, label = _is_blocked_du_target(target, active_cwd)
            if blocked:
                return False, (
                    f"Command Blocked: Unbounded 'du' disk-usage walk targeting '{label}' is prohibited. "
                    "Even summary flags like 'du -sh' or '--max-depth=0' still recursively walk every inode and stall spinning NAS HDDs. "
                    "Use 'df -h' for filesystem capacity or targeted metadata checks ('stat' / 'ls -lh' on specific files) instead."
                )

    return True, ""


def check_command_line(cmd: str, cwd: str) -> tuple[bool, str]:
    if not cmd:
        return True, ""

    # 0. Check unbounded 'du' disk-usage walks (local, cd && du, $(du ...), and ssh-wrapped)
    du_allowed, du_reason = _check_du_command(cmd, cwd)
    if not du_allowed:
        return False, du_reason

    # 1. Detect recursive grep/rg/ag/ack patterns
    has_recursive_grep = bool(re.search(r'\b(grep\s+-[a-zA-Z]*r[a-zA-Z]*|rg|ag|ack)\b', cmd))
    has_find = bool(re.search(r'\bfind\b', cmd))

    # 2. Check find commands
    if has_find and re.search(r'\bfind\s+(/|/workspace|\.|\*)\b', cmd) and "-maxdepth" not in cmd:
        norm_cwd = normalize_path(".", cwd)
        if norm_cwd in BLOCKED_EXACT_ROOTS or "-name" not in cmd:
            return False, (
                "Command Blocked: 'find' on root directory or without '-maxdepth' is prohibited. "
                "Always bound find commands (e.g., 'find /workspace/tools -maxdepth 2 -name \"*.py\"')."
            )

    # 3. Check recursive grep / rg / ag
    if has_recursive_grep:
        if re.search(r'\b(grep\s+-[a-zA-Z]*r[a-zA-Z]*|rg|ag|ack)\b.*?\s+([\'"]?)(/|/volume[0-9]*/?|/volumeUSB[0-9]*/?|/workspace/?|/root/?|/docker/?|/data/?)\2(\s|$|;|\)|\||&)', cmd):
            return False, (
                "Command Blocked: Unbounded recursive search targeting root or full volume is prohibited. "
                "Target a specific subfolder (e.g. 'grep -rn \"pattern\" /docker/baseball/')."
            )
        tokens = cmd.split()
        # Check if there is an explicit target or if it defaults to cwd
        for token in tokens:
            cleaned = token.strip('"\'')
            if not cleaned or cleaned.startswith("-"):
                continue
            if cleaned in ("grep", "rg", "ag", "ack"):
                continue
            # Assume any non-flag token could be query or target path
            norm = normalize_path(cleaned, cwd)
            if norm in BLOCKED_EXACT_ROOTS or re.match(r'^/volume[0-9]*$', norm):
                return False, (
                    f"Command Blocked: Unbounded recursive search targeting '{cleaned}' (resolved: '{norm}') is prohibited. "
                    f"Target a specific subfolder: e.g. 'grep -rn \"pattern\" /docker/baseball/'."
                )
            for prefix in BLOCKED_TREE_PREFIXES:
                if norm.startswith(prefix.rstrip("/")):
                    if os.path.isfile(norm) and norm in SAFE_EXEMPT_FILES:
                        continue
                    return False, (
                        f"Command Blocked: Recursive search inside '{cleaned}' (resolved: '{norm}') is prohibited "
                        f"because it falls under bulk/system directory '{prefix}'. "
                        "Target a specific workspace subfolder (e.g. /workspace/tools, /workspace/config)."
                    )

    # 4. Check binary reverse-engineering / binary scan commands
    has_binary_inspection = bool(re.search(r'\b(strings|hexdump|objdump|readelf|nm|gdb|radare2|ghidra)\b', cmd))
    targets_binary_dir = bool(re.search(r'(/usr/local/bin|/usr/bin|/bin|/usr/sbin|/sbin)\b', cmd))
    if targets_binary_dir and (has_binary_inspection or re.search(r'\bgrep\b', cmd)):
        return False, (
            "Command Blocked: Direct grep, strings, or disassembly inspection of system binaries "
            "in /usr/local/bin or /usr/bin is prohibited. Check documentation or CLI help flags instead."
        )

    # 5. Check known interactive CLI commands that block on STDIN
    if re.search(r'\bnpx\s+', cmd) and not re.search(r'\b(-y|--yes)\b', cmd):
        return False, (
            "Command Blocked: 'npx' without '-y' / '--yes' hangs waiting for interactive package confirmation. "
            "Please invoke as 'npx -y <package>'."
        )

    if re.search(r'\b(apt|apt-get)\s+install\b', cmd) and not re.search(r'\b(-y|--yes|--assume-yes)\b', cmd):
        return False, (
            "Command Blocked: 'apt/apt-get install' without '-y' hangs waiting for confirmation. "
            "Please pass '-y'."
        )

    if re.search(r'\bnxapi\s+(nso|pctl)\s+auth\b', cmd) and "switch_auth" not in cmd:
        return False, (
            "Command Blocked: 'nxapi auth' spawns an interactive readline prompt waiting on STDIN. "
            "Use decoupled '/workspace/tools/switch_auth.js generate' instead."
        )

    # 6. Check unauthorized direct SerpApi web scraping
    if (
        re.search(r'serpapi\.com', cmd)
        and not re.search(r'(amazon_serpapi\.py|reddit_extractor\.py|serpapi_guard\.py)', cmd)
        and (
            re.search(r'engine=(google_images|google|bing|yahoo|duckduckgo)', cmd)
            or "nutritionCard" in cmd
            or "greenchef" in cmd
        )
    ):
        return False, (
            "Command Blocked: Ad-hoc web/image scraping via SerpApi is prohibited to preserve the 250-query monthly quota. "
            "SerpApi is restricted strictly to Amazon product intelligence (tools/amazon_serpapi.py) and Reddit research (tools/reddit_extractor.py). "
            "Use Playwright, Browserless, or free search tools for web/image scraping."
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

    except Exception as e:  # noqa: BLE001
        print(json.dumps({"decision": "deny", "reason": f"Safety Guard Evaluation Error: {e!s}"}))

if __name__ == "__main__":
    main()
