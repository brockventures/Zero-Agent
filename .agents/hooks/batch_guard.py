#!/usr/bin/env python3
"""
Tool Batching Safety Guard (Antigravity PreToolUse Hook)
Enforces the Tool Roundtrip Minimization & Batch Execution Invariant using strictly
CONSECUTIVE anti-pattern counters (zero cumulative turn-level step or scratch-write caps):
1. Consecutive single-line shell inspection commands (run_command), including sed/awk/wc slicing.
2. Consecutive inline Python heredoc / -c probing loops (MAX_CONSECUTIVE_INLINE_PYTHON = 3).
3. Consecutive repetitive file reading / narrow line slicing (view_file), with wide-slice (>=350 lines) allowance.
4. Consecutive unverified single-file replacements (replace_file_content).
5. Consecutive unbatched git lifecycle commands (git add / commit / push / gh pr create).
"""
import sys
import os
import json
import time
import re

STATE_FILE = "/tmp/batch_guard_state.json"
MAX_CONSECUTIVE_SHELL_INSPECTIONS = 3
MAX_CONSECUTIVE_INLINE_PYTHON = 2
MAX_CONSECUTIVE_SAME_FILE_VIEWS = 2
MAX_WIDE_SAME_FILE_VIEWS = 4
WIDE_VIEW_MIN_LINES = 350
MAX_CONSECUTIVE_FILE_VIEWS = 6
MAX_CONSECUTIVE_SAME_FILE_EDITS = 3
MAX_CONSECUTIVE_GIT_CMDS = 2
MAX_VERSIONED_SCRATCH_SWEEPS = 3
MAX_SAME_SCRATCH_SCRIPT_RUNS = 3
MAX_INLINE_PRODUCTION_FILES = 2
TURN_RESET_IDLE_SECONDS = 45.0
TURN_STATE_EXPIRY_SECONDS = 900.0

_VERSIONED_SCRATCH_SWEEP_RE = re.compile(
    r"(?:^|/)(?:scratch|brain/[^/]+/scratch)/(?P<stem>[a-zA-Z0-9_-]*?(?:sim|sweep|grid|solve|tune|calib)[a-zA-Z0-9_-]*?)_v\d+\.py$",
    re.IGNORECASE,
)

_SCRATCH_SCRIPT_EXEC_RE = re.compile(
    r"\bpython[0-9.]*\s+(?P<script>[^\s;\"'|&>]*?(?:/scratch/|^scratch/)[^\s;\"'|&>]+?\.py)\b"
)

_HEAVY_CLI_BENCHMARK_OR_MIGRATE_RE = re.compile(
    r"(?:run_unified_benchmark_gate\.py|--migrate-db\b|fast_migrate_[a-zA-Z0-9_-]*\.py\b|backfill_[a-zA-Z0-9_-]*?splits[a-zA-Z0-9_-]*\.py\b)"
)


def _default_state(now: float = 0.0) -> dict:
    return {
        "last_time": now,
        "cmd_inspections": 0,
        "consecutive_inline_python": 0,
        "consecutive_file_views": 0,
        "last_viewed_file": "",
        "same_file_view_count": 0,
        "last_edited_file": "",
        "same_file_edit_count": 0,
        "consecutive_git_cmds": 0,
        "agora_eval_calls": 0,
        "versioned_scratch_sweeps": [],
        "inline_prod_files": [],
        "scratch_script_runs": {},
    }


def is_forge_worktree_or_exempt_path(target_file: str, cwd: str = "/workspace") -> bool:
    """Return True if target_file is inside a FORGE worktree, scratch, memory, data, or unit test file."""
    if not target_file:
        return True
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return True
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return True
    norm_target = os.path.abspath(os.path.join(norm_cwd, target_file)).replace("\\", "/")
    exempt_prefixes = (
        "/tmp/",
        "/workspace/scratch/",
        "/workspace/memory/",
        "/workspace/data/",
        "/root/.gemini/",
    )
    if norm_target.startswith(exempt_prefixes):
        return True
    if "/scratch/" in norm_target or "/memory/" in norm_target or "/brain/" in norm_target:
        return True
    base = os.path.basename(norm_target)
    if base.startswith("test_") or base.endswith("_test.py") or "/tests/" in norm_target:
        return True
    return False


def load_state(state_file: str = STATE_FILE) -> dict:
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                data = json.load(f)
                base = _default_state(data.get("last_time", 0.0))
                for k in base:
                    if k in data:
                        base[k] = data[k]
                return base
        except Exception:
            pass
    return _default_state(0.0)


def save_state(state: dict, state_file: str = STATE_FILE):
    try:
        with open(state_file, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def is_unscoped_pytest(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    if " && " in cmd_clean or " || " in cmd_clean or "; " in cmd_clean or "\n" in cmd_clean:
        return False
    m = re.match(r"^(?:pytest|python[0-9.]*\s+-m\s+pytest)(?:\s+(.*))?$", cmd_clean)
    if not m:
        return False
    args = m.group(1) or ""
    tokens = args.split()
    if not tokens:
        return True
    if any(t.endswith(".py") or ".py::" in t or t.startswith("-k") or t.startswith("-m") for t in tokens):
        return False
    if "-k" in tokens or "-m" in tokens:
        return False
    generic_targets = {"tests", "tests/", ".", "./"}
    return all(t.startswith("-") or t in generic_targets for t in tokens)


def is_single_git_lifecycle_cmd(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    if " && " in cmd_clean or " || " in cmd_clean or "; " in cmd_clean or "\n" in cmd_clean:
        return False
    git_lifecycle_pattern = r"^(?:git\s+(?:add|commit|push|checkout\s+-b|switch\s+-c)|gh\s+pr\s+create)\b"
    return bool(re.search(git_lifecycle_pattern, cmd_clean))


def is_inline_python_probe(cmd: str) -> bool:
    """Detect inline python -c or heredoc (python3 - << 'EOF') probing commands, even when chained with &&."""
    cmd_clean = cmd.strip()
    first_line = cmd_clean.splitlines()[0].strip() if cmd_clean else ""
    if bool(re.match(r"^python[0-9.]*\s+(?:-c\b|-\s*<<|<<)", first_line)):
        return True
    unquoted = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", cmd_clean)
    if " && " in unquoted or " || " in unquoted or "; " in unquoted:
        return False
    return False


def is_unbounded_ha_sqlite_query(cmd: str) -> bool:
    """Detect SQLite queries against home-assistant_v2.db that lack last_updated_ts bounds or read-only mode."""
    if "home-assistant_v2.db" not in cmd:
        return False
    if "SELECT" not in cmd.upper():
        return False
    if "last_updated_ts >" not in cmd and "last_updated_ts >=" not in cmd:
        return True
    return False


_DESTRUCTIVE_GIT_WIPE_RE = re.compile(
    r"\bgit\s+(?:"
    r"reset\s+--hard\b|"
    r"checkout-index\s+-[a-zA-Z]*f|"
    r"clean\s+-[a-zA-Z]*f|"
    r"checkout\s+(?:[^\s;|&&]+\s+)?--\s+(?:\.|\$\([^)]+\)|(?:\.agents|tools|config|docs)\b)|"
    r"restore\s+(?:--worktree\s+|--source=[^\s]+\s+)*(?:\.|(?:\.agents|tools|config|docs)\b)"
    r")"
)


def is_destructive_git_working_tree_wipe(cmd: str, cwd: str = "/workspace") -> bool:
    """Block destructive git working-tree wipes in /workspace (e.g. git reset --hard, git checkout-index -f, blanket directory checkout)."""
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return False
    unquoted = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", cmd)
    return bool(_DESTRUCTIVE_GIT_WIPE_RE.search(unquoted))


def is_batched_or_mutating_cmd(cmd: str) -> bool:
    cmd_clean = cmd.strip()
    if is_inline_python_probe(cmd_clean):
        return False
    unquoted = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", cmd_clean)
    if " && " in unquoted or " || " in unquoted or "; " in unquoted or "\n" in unquoted:
        return True
    if "scratch/" in cmd_clean or "test_" in cmd_clean or "pytest" in cmd_clean:
        return True
    if re.search(r"python[0-9.]*\s+(/workspace/|/baseball/|/root/)", cmd_clean):
        return True
    if re.search(r"\b(git|touch|mkdir|rm|cp|mv|chmod|kill|pkill|restart|reboot|compose)\b", unquoted):
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
        "sed", "awk", "wc", "cut", "sort", "uniq", "stat",
        "ls", "df", "free", "uptime", "vmstat", "iostat",
        "crontab", "ss", "netstat", "ps", "which", "echo"
    )
    tokens = target_cmd.split()
    first_word = tokens[0] if tokens else ""
    first_two = " ".join(tokens[:2]) if len(tokens) >= 2 else ""
    return first_word in inspection_starters or first_two in inspection_starters


def is_ouroboros_polling_loop(cmd: str) -> bool:
    """Detect manual shell busy-wait process polling loops (e.g. while pgrep -f ...; do sleep ...)."""
    if not cmd:
        return False
    pattern = (
        r"\b(?:while|until)\s+(?:!\s*)?(?:pgrep|ps\s+|pidof|kill\s+-0)\b[^;\"\n']*?[;\n]+\s*do\b[\s\S]*?\bsleep\s+[0-9.]+"
    )
    return bool(re.search(pattern, cmd))


def is_proc_fd_stream_read(cmd: str) -> bool:
    """Detect tail/cat/head/grep against open container stdio FIFO pipes (/proc/1/fd/* or /proc/self/fd/*)."""
    if not cmd:
        return False
    pattern = r"\b(?:tail|cat|head|grep|egrep|fgrep|rg|sed|awk)\b[^\n;|]*?/proc/(?:1|self|\d+)/fd/[012]\b"
    return bool(re.search(pattern, cmd))


def check_readonly_shared_mount(target_file: str, cwd: str = "/workspace") -> tuple[bool, str]:
    """Block attempts to edit or overwrite files residing on Zero's read-only /opt/zero shared mount."""
    if not target_file:
        return True, ""
    norm_cwd = os.path.abspath(cwd or "/workspace")
    abs_path = os.path.abspath(os.path.join(norm_cwd, target_file))
    real_path = os.path.realpath(abs_path).replace("\\", "/")
    if real_path.startswith("/opt/zero/"):
        return False, (
            f"Read-Only Shared Mount Guard: '{target_file}' resolves to '{real_path}' (owned by Zero and mounted read-only into discord-ivy-agent). "
            "Do NOT attempt local edits or replace the symlink via cp/rm (which severs upstream updates). "
            "Escalate the issue to Zero in #zero-ops (1544953279664889888) by running:\n"
            "python3 /workspace/tools/outbox.py --channel zero-ops --message \"<@1542285964213358633> [Upstream Fix Request] <file & root cause>\" --force && python3 /workspace/tools/outbox.py --flush"
        )
    if os.path.exists(real_path) and not os.access(real_path, os.W_OK):
        return False, (
            f"Read-Only File Guard: '{target_file}' ('{real_path}') is read-only inside this container. "
            "Do NOT bypass read-only mounts. Escalate the required change to Zero in #zero-ops (1544953279664889888) via:\n"
            "python3 /workspace/tools/outbox.py --channel zero-ops --message \"<@1542285964213358633> [Upstream Fix Request] <file & root cause>\" --force && python3 /workspace/tools/outbox.py --flush"
        )
    return True, ""


def is_opt_zero_symlink_tamper(cmd: str) -> bool:
    """Detect shell commands attempting to overwrite or unlink /opt/zero symlinks inside discord-ivy-agent."""
    if not cmd or not os.path.exists("/opt/zero/workspace"):
        return False
    if "/opt/zero/" in cmd and re.search(r"\b(?:cp|mv|rm|unlink|sed\s+-i|chmod|chown)\b", cmd):
        return True
    if re.search(r"\bcp\s+--remove-destination\b", cmd):
        return True
    return False


def _check_inline_production_edit_gate(
    target_file: str,
    cwd: str,
    state: dict,
    state_file: str,
) -> tuple[bool, str]:
    """Enforce FORGE Tier 2/3 Gate: block interactive turns outside /tmp/forge-wt-* from mutating >2 production files inline."""
    if is_forge_worktree_or_exempt_path(target_file, cwd=cwd):
        return True, ""
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    norm_target = os.path.abspath(os.path.join(norm_cwd, target_file)).replace("\\", "/")
    prod_files = list(state.get("inline_prod_files", []))
    if norm_target not in prod_files:
        prod_files.append(norm_target)
    state["inline_prod_files"] = prod_files
    if len(prod_files) > MAX_INLINE_PRODUCTION_FILES:
        save_state(state, state_file)
        short_list = ", ".join(os.path.basename(p) for p in prod_files)
        return False, (
            f"FORGE Tier 2/3 Gate Triggered: Interactive turn attempted to mutate {len(prod_files)} distinct production files inline "
            f"({short_list}; inline Tier 1 limit: {MAX_INLINE_PRODUCTION_FILES}). "
            "Multi-file features and refactors must be decomposed into a validated ForgePlan JSON (e.g. scratch/forge_plan.json) "
            "and enqueued via 'python3 /workspace/tools/forge.py enqueue scratch/forge_plan.json' so #forge workers execute them in isolated git worktrees."
        )
    return True, ""


def is_heavy_inline_benchmark_or_migration(cmd: str, cwd: str = "/workspace") -> bool:
    """Detect heavy multi-minute benchmark or database migration commands run inline outside FORGE."""
    if not cmd:
        return False
    if os.environ.get("FORGE_WORKER_ACTIVE") == "1":
        return False
    norm_cwd = os.path.abspath(cwd or "/workspace").replace("\\", "/")
    if norm_cwd.startswith("/tmp/forge-wt-"):
        return False
    if "forge.py" in cmd or "detached_runner.py" in cmd or "modal_harness.py" in cmd:
        return False
    return bool(_HEAVY_CLI_BENCHMARK_OR_MIGRATE_RE.search(cmd))


def check_tool_use(name: str, args: dict, state_file: str = STATE_FILE, cwd: str = "/workspace") -> tuple[bool, str]:
    now = time.time()
    state = load_state(state_file)
    idle_delta = now - state.get("last_time", 0.0)

    # Full turn state expiry after extended inactivity (15m); short idle gaps (45s)
    # reset only short-window consecutive anti-polling counters while preserving turn-level
    # FORGE gates (inline_prod_files, versioned_scratch_sweeps, scratch_script_runs).
    if idle_delta > TURN_STATE_EXPIRY_SECONDS:
        state = _default_state(now)
    elif idle_delta > TURN_RESET_IDLE_SECONDS:
        preserved_prod = list(state.get("inline_prod_files", []))
        preserved_sweeps = list(state.get("versioned_scratch_sweeps", []))
        preserved_runs = dict(state.get("scratch_script_runs", {}))
        state = _default_state(now)
        state["inline_prod_files"] = preserved_prod
        state["versioned_scratch_sweeps"] = preserved_sweeps
        state["scratch_script_runs"] = preserved_runs
    state["last_time"] = now

    # 0. Runtime schedule tool is prohibited in Discord bridge turns
    if name == "schedule":
        return False, (
            "Schedule Tool Prohibited in Bridge Turns: Do not invoke the runtime 'schedule' timer tool to poll background tasks. "
            "Stop calling tools silently and let the background task completion notification wake you automatically."
        )

    # 1. run_command check
    if name == "run_command":
        cmd = args.get("CommandLine", "")
        if "forge.py" in cmd and ("enqueue" in cmd or "job" in cmd):
            state["inline_prod_files"] = []
            state["scratch_script_runs"] = {}
        elif "detached_runner.py" in cmd and "start" in cmd:
            state["scratch_script_runs"] = {}

        # A00. Heavy Inline Benchmark / Migration Gate (Require FORGE job start or enqueue)
        if is_heavy_inline_benchmark_or_migration(cmd, cwd=cwd):
            return False, (
                f"FORGE Background Offload Gate Triggered: Heavy benchmark or database migration command ('{cmd[:90]}') "
                "cannot be executed inline via bare 'run_command' (which blocks the interactive turn for 10–30 minutes). "
                "Launch it as a tracked FORGE background job via:\n"
                "python3 /workspace/tools/forge.py job start --name \"<Job Name>\" --command \"<cmd>\"\n"
                "(or enqueue a multi-chunk ForgePlan via 'python3 /workspace/tools/forge.py enqueue <plan.json>')."
            )

        # A00b. Repeated Same-Scratch-Script Loop Gate (Prevent iterative scratch/*.py overwrite-and-rerun loops)
        if os.environ.get("FORGE_WORKER_ACTIVE") != "1" and not os.path.abspath(cwd or "/workspace").replace("\\", "/").startswith("/tmp/forge-wt-"):
            if "forge.py" not in cmd and "detached_runner.py" not in cmd:
                scratch_m = _SCRATCH_SCRIPT_EXEC_RE.search(cmd)
                if scratch_m:
                    raw_script = scratch_m.group("script")
                    norm_script = os.path.abspath(os.path.join(cwd or "/workspace", raw_script)).replace("\\", "/")
                    runs_map = dict(state.get("scratch_script_runs", {}))
                    run_cnt = int(runs_map.get(norm_script, 0)) + 1
                    runs_map[norm_script] = run_cnt
                    state["scratch_script_runs"] = runs_map
                    if run_cnt > MAX_SAME_SCRATCH_SCRIPT_RUNS:
                        save_state(state, state_file)
                        return False, (
                            f"FORGE Scratch Loop Gate Triggered: Executed '{os.path.basename(norm_script)}' {run_cnt} times in this turn "
                            f"(inline limit: {MAX_SAME_SCRATCH_SCRIPT_RUNS}). Do not run iterative edit-and-rerun loops on the same scratch script "
                            "inline in chat. Consolidate parameter sweeps into a single vectorized pass, offload to Modal, or launch long jobs via:\n"
                            "python3 /workspace/tools/forge.py job start --name \"<Job Name>\" --command \"python3 " + norm_script + "\""
                        )

        # Agora Fast-Path Invariant
        if re.search(r"agora_play\.py\s+(?:order|move)\b", cmd):
            return False, (
                "Agora Fast-Path Invariant: Direct CLI order/move commands are prohibited in conversational turns. "
                "Orders and vessel movement must be emitted directly via Discord chat syntax (BUY/SELL <qty> <good> @ <px> AT <st>) "
                "so agora_announcer.py submits them. Keep turns to zero tools."
            )
        if re.search(r"agora_play\.py\s+(?:status|eval)\b", cmd):
            agora_calls = state.get("agora_eval_calls", 0) + 1
            state["agora_eval_calls"] = agora_calls
            if agora_calls > 1:
                save_state(state, state_file)
                return False, (
                    f"Agora Fast-Path Invariant: Detected {agora_calls} agora_play inspection calls in a single turn. "
                    "The Discord round announcement already embeds all quotes. Keep Agora turns to 0 tools (or at most 1 fast eval call)."
                )
            save_state(state, state_file)
            return True, ""

        # A0. Ouroboros Process-Polling Loop Guard
        if is_ouroboros_polling_loop(cmd):
            return False, (
                "Ouroboros Polling Guard Triggered: Manual shell process-polling loops ('while ... pgrep/ps ... sleep') "
                "are prohibited because 'pgrep -f' matches its own bash -c wrapper and deadlocks the turn. "
                "For SDK background tasks, stop calling tools silently and let the task completion event wake you; "
                "for jobs >90 seconds, use 'python3 /workspace/tools/forge.py job start ...' (or detached_runner.py)."
            )

        # A0a. Container /proc/1/fd FIFO Stream Read Guard
        if is_proc_fd_stream_read(cmd):
            return False, (
                "Proc FD Stream Guard Triggered: Reading container stdio pipes (/proc/1/fd/* or /proc/self/fd/*) "
                "with tail/cat/head/grep blocks indefinitely on open FIFO streams and wedges the turn. "
                "Inspect log files in /workspace/logs/ or /tmp/ directly instead."
            )

        # A0a-2. Shared /opt/zero Symlink Tamper Guard
        if is_opt_zero_symlink_tamper(cmd):
            return False, (
                "Read-Only Shared Mount Guard: Do NOT unlink or overwrite /opt/zero symlinks via shell commands (cp --remove-destination / rm), "
                "as that permanently severs discord-ivy-agent from Zero's upstream fixes. "
                "Escalate the required change to Zero in #zero-ops (1544953279664889888) via:\n"
                "python3 /workspace/tools/outbox.py --channel zero-ops --message \"<@1542285964213358633> [Upstream Fix Request] <file & root cause>\" --force && python3 /workspace/tools/outbox.py --flush"
            )

        # A0b. Unbounded Home Assistant Recorder SQLite Guard
        if is_unbounded_ha_sqlite_query(cmd):
            return False, (
                "HA Recorder SQLite Guard Triggered: Querying 'home-assistant_v2.db' without an explicit 'last_updated_ts >' "
                "lower bound causes multi-minute table scans and write-lock stalls. Prefer the Home Assistant REST API "
                "(/api/states or /api/history/period) or include an indexed 'last_updated_ts >' filter with '-readonly'."
            )

        # A0c. Destructive Git Working-Tree Wipe Guard
        if is_destructive_git_working_tree_wipe(cmd, cwd):
            return False, (
                "Destructive Git Working-Tree Wipe Blocked: Commands like 'git reset --hard', 'git checkout-index -f', "
                "'git clean -f', or blanket directory checkouts ('git checkout origin/main -- tools .agents') clobber "
                "uncommitted changes from concurrent sessions/sidecars in /workspace. Commit or 'git stash' dirty files "
                "explicitly before pulling/rebasing."
            )

        # A. Unscoped test suite check (Ban bare pytest in interactive turns)
        if is_unscoped_pytest(cmd):
            return False, (
                f"Batch Invariant Triggered: Unscoped full test suite execution ('{cmd}'). "
                "Scope test execution to the specific test file or function under test (e.g. 'pytest tests/test_feature.py' or 'pytest -k test_name') "
                "to avoid multi-minute turn latency. Let GitHub Actions CI run the full regression suite in the cloud."
            )

        # B. Consecutive inline Python probe check (python3 -c / heredoc loops)
        if is_inline_python_probe(cmd):
            state["consecutive_inline_python"] = state.get("consecutive_inline_python", 0) + 1
            if state["consecutive_inline_python"] > MAX_CONSECUTIVE_INLINE_PYTHON:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['consecutive_inline_python']} consecutive inline Python probe commands "
                    f"(limit: {MAX_CONSECUTIVE_INLINE_PYTHON}). Consolidate multi-step diagnostics or parameter sweeps into a single "
                    "reusable script in scratch/ instead of serial inline heredocs."
                )
            state["cmd_inspections"] = 0
            state["consecutive_file_views"] = 0
            state["same_file_view_count"] = 0
            state["same_file_edit_count"] = 0
            state["consecutive_git_cmds"] = 0
            save_state(state, state_file)
            return True, ""
        else:
            state["consecutive_inline_python"] = 0

        # C. Serial unbatched git lifecycle check
        if is_single_git_lifecycle_cmd(cmd):
            state["consecutive_git_cmds"] = state.get("consecutive_git_cmds", 0) + 1
            if state["consecutive_git_cmds"] > MAX_CONSECUTIVE_GIT_CMDS:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['consecutive_git_cmds']} consecutive unbatched git lifecycle commands in serial. "
                    "Please bundle git operations (e.g. 'git add <files> && git commit -m \"...\" && git push && gh pr create') into a single compound "
                    "command or script to minimize tool roundtrips."
                )
            state["cmd_inspections"] = 0
            state["consecutive_file_views"] = 0
            state["same_file_view_count"] = 0
            state["same_file_edit_count"] = 0
            state["agora_eval_calls"] = 0
            save_state(state, state_file)
            return True, ""
        else:
            state["consecutive_git_cmds"] = 0

        if is_batched_or_mutating_cmd(cmd):
            state["cmd_inspections"] = 0
            state["consecutive_file_views"] = 0
            state["same_file_view_count"] = 0
            state["same_file_edit_count"] = 0
            state["agora_eval_calls"] = 0
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

    # 2. view_file / read_file check
    if name in ("view_file", "read_file"):
        target_file = args.get("AbsolutePath") or args.get("Path") or ""
        start_line = args.get("StartLine")
        end_line = args.get("EndLine")
        is_wide_slice = (start_line is None and end_line is None) or (
            isinstance(start_line, int) and isinstance(end_line, int) and (end_line - start_line) >= WIDE_VIEW_MIN_LINES
        )
        state["cmd_inspections"] = 0
        state["consecutive_inline_python"] = 0

        if target_file and target_file == state.get("last_viewed_file"):
            state["same_file_view_count"] = state.get("same_file_view_count", 0) + 1
            allowed_same = MAX_WIDE_SAME_FILE_VIEWS if is_wide_slice else MAX_CONSECUTIVE_SAME_FILE_VIEWS
            if state["same_file_view_count"] > allowed_same:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['same_file_view_count']} consecutive {name} calls on '{target_file}'. "
                    f"{name} supports viewing up to 800 lines in a single call. Omit StartLine/EndLine to view the file in one shot, "
                    "or write a targeted Python scratch script in /workspace/scratch/ to inspect AST/regexes in a single pass."
                )
        else:
            state["last_viewed_file"] = target_file
            state["same_file_view_count"] = 1

        state["consecutive_file_views"] = state.get("consecutive_file_views", 0) + 1
        if state["consecutive_file_views"] > MAX_CONSECUTIVE_FILE_VIEWS:
            save_state(state, state_file)
            return False, (
                f"Batch Invariant Triggered: Detected {state['consecutive_file_views']} consecutive serial {name} calls. "
                "Please bundle multi-file inspections into a single Python scratch script in /workspace/scratch/ "
                "to inspect multiple files in a single pass."
            )

        save_state(state, state_file)
        return True, ""

    # 3. replace_file_content / edit_file check
    if name in ("replace_file_content", "edit_file"):
        target_file = args.get("TargetFile") or args.get("Path") or ""
        state["cmd_inspections"] = 0
        state["consecutive_inline_python"] = 0
        state["consecutive_file_views"] = 0
        state["same_file_view_count"] = 0

        ok_ro, ro_reason = check_readonly_shared_mount(target_file, cwd)
        if not ok_ro:
            return False, ro_reason

        ok_tier, tier_reason = _check_inline_production_edit_gate(target_file, cwd, state, state_file)
        if not ok_tier:
            return False, tier_reason

        if target_file and target_file == state.get("last_edited_file"):
            state["same_file_edit_count"] = state.get("same_file_edit_count", 0) + 1
            if state["same_file_edit_count"] > MAX_CONSECUTIVE_SAME_FILE_EDITS:
                save_state(state, state_file)
                return False, (
                    f"Batch Invariant Triggered: Detected {state['same_file_edit_count']} consecutive {name} calls on '{target_file}'. "
                    "For extensive edits or multiple modifications in the same file, use write_file or a Python script in /workspace/scratch/ "
                    "to apply changes in a single pass."
                )
        else:
            state["last_edited_file"] = target_file
            state["same_file_edit_count"] = 1

        save_state(state, state_file)
        return True, ""

    # 4. write_to_file / write_file resets consecutive counters and blocks LLM-in-the-loop versioned scratch sweep loops (sim_v1..v14.py)
    if name in ("write_to_file", "write_file"):
        target_file = args.get("TargetFile") or args.get("Path") or ""
        state["cmd_inspections"] = 0
        state["consecutive_inline_python"] = 0
        state["consecutive_file_views"] = 0
        state["last_viewed_file"] = ""
        state["same_file_view_count"] = 0
        state["last_edited_file"] = ""
        state["same_file_edit_count"] = 0
        state["consecutive_git_cmds"] = 0

        ok_ro, ro_reason = check_readonly_shared_mount(target_file, cwd)
        if not ok_ro:
            return False, ro_reason

        if target_file and _VERSIONED_SCRATCH_SWEEP_RE.search(target_file):
            sweeps = list(state.get("versioned_scratch_sweeps", []))
            if target_file not in sweeps:
                sweeps.append(target_file)
            state["versioned_scratch_sweeps"] = sweeps
            if len(sweeps) > MAX_VERSIONED_SCRATCH_SWEEPS:
                save_state(state, state_file)
                return False, (
                    f"Batch & Modal Offload Invariant Triggered: Detected {len(sweeps)} versioned scratch sweep scripts "
                    f"({', '.join(os.path.basename(s) for s in sweeps[-4:])}) in a single turn (limit: {MAX_VERSIONED_SCRATCH_SWEEPS}). "
                    "Do not run iterative LLM-in-the-loop scratch simulation sweeps (sim_v1..vN.py) that burn the 60-minute turn ceiling. "
                    "Evaluate all parameter combinations inside a single vectorized Python loop or offload multi-combo sweeps to "
                    "'/baseball/big-projections/modal_harness.py --mode grid' (or 'forge.py job start' / detached_runner.py for jobs >90s)."
                )

        ok_tier, tier_reason = _check_inline_production_edit_gate(target_file, cwd, state, state_file)
        if not ok_tier:
            return False, tier_reason

        save_state(state, state_file)
        return True, ""

    save_state(state, state_file)
    return True, ""


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
        cwd = payload.get("cwd", "/workspace")

        # 1. Unified Search Safety Gate (in-process, avoiding a second Python subprocess spawn)
        if name in ("grep_search", "find_by_name", "run_command"):
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from search_guard import check_search_path, check_command_line, normalize_path
                if name == "grep_search":
                    s_ok, s_reason = check_search_path(args.get("SearchPath", ""), cwd)
                    if not s_ok:
                        print(json.dumps({"decision": "deny", "reason": s_reason}))
                        return
                elif name == "find_by_name":
                    search_dir = args.get("SearchDirectory", "")
                    max_depth = args.get("MaxDepth")
                    s_ok, s_reason = check_search_path(search_dir, cwd)
                    if not s_ok:
                        if not (normalize_path(search_dir, cwd) == "/workspace" and max_depth is not None and int(max_depth) <= 2):
                            print(json.dumps({"decision": "deny", "reason": s_reason}))
                            return
                elif name == "run_command":
                    s_ok, s_reason = check_command_line(args.get("CommandLine", ""), args.get("Cwd", cwd))
                    if not s_ok:
                        print(json.dumps({"decision": "deny", "reason": s_reason}))
                        return
            except Exception:
                pass

        # 2. Tool Batching Guard
        allowed, reason = check_tool_use(name, args, cwd=cwd)
        if not allowed:
            print(json.dumps({"decision": "deny", "reason": reason}))
            return

        resp = {"decision": "allow"}
        if name == "run_command" and not args.get("IsDaemon", False):
            wait_ms = args.get("WaitMsBeforeAsync", 0)
            if isinstance(wait_ms, (int, float)) and wait_ms < 10000:
                resp["overwrite"] = {"WaitMsBeforeAsync": 10000}

        print(json.dumps(resp))
    except Exception:
        print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()
