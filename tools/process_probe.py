#!/usr/bin/env python3
"""Process Probe & Kernel Wait-State Diagnostic Tool for Zero.

Forensically inspects Linux /proc filesystem for a target PID and its entire descendant tree.
Identifies whether a process is wedged, why it is blocked (kernel wchan), whether it is
waiting on interactive STDIN, and extracts the exact prompt or blocking condition.
"""

import os
import re
import sys
import time
import signal
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

INTERACTIVE_PROMPT_PATTERNS = [
    re.compile(r"(?:password|passphrase).*\s*:", re.IGNORECASE),
    re.compile(r"\[y/n\]", re.IGNORECASE),
    re.compile(r"\(y/n\)", re.IGNORECASE),
    re.compile(r"ok to proceed\?\s*(\([y/n]\))?", re.IGNORECASE),
    re.compile(r"paste\s+the\s+link\s*:", re.IGNORECASE),
    re.compile(r"enter\s+(?:code|token|key|pin|selection)\s*:", re.IGNORECASE),
    re.compile(r"(?:continue|proceed)\?\s*:", re.IGNORECASE),
    re.compile(r"press\s+\[?enter\]?\s+to\s+continue", re.IGNORECASE),
]

STDIN_WAIT_CHANNELS = {
    "n_tty_read",
    "read_chan",
    "tty_read",
    "pipe_read",
}

NETWORK_WAIT_CHANNELS = {
    "sk_wait_data",
    "tcp_recvmsg",
    "inet_csk_accept",
    "sock_recvmsg",
    "unix_stream_data_wait",
}

LOCK_WAIT_CHANNELS = {
    "futex_wait",
    "futex_wait_queue_me",
    "do_sigtimedwait",
    "down_read",
    "down_write",
    "mutex_lock",
}


def get_process_children(pid: int) -> List[int]:
    """Recursively collect all descendant child PIDs for a process."""
    children = []
    task_dir = Path(f"/proc/{pid}/task")
    if not task_dir.exists():
        return children

    direct_children = set()
    try:
        for tid_dir in task_dir.iterdir():
            children_file = tid_dir / "children"
            if children_file.exists():
                try:
                    with open(children_file, "r") as f:
                        for cp in f.read().split():
                            if cp.isdigit():
                                direct_children.add(int(cp))
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass

    # Fallback to scanning all /proc if /proc/<pid>/task/<tid>/children is empty
    if not direct_children:
        try:
            for p in Path("/proc").iterdir():
                if p.is_dir() and p.name.isdigit():
                    c_pid = int(p.name)
                    stat_file = p / "stat"
                    if stat_file.exists():
                        try:
                            with open(stat_file, "r") as f:
                                parts = f.read().split()
                                if len(parts) > 3 and parts[3] == str(pid):
                                    direct_children.add(c_pid)
                        except (OSError, PermissionError):
                            pass
        except (OSError, PermissionError):
            pass

    for cp in sorted(direct_children):
        children.append(cp)
        children.extend(get_process_children(cp))

    return sorted(list(set(children)))


def inspect_single_process(pid: int) -> Dict[str, Any]:
    """Inspect Linux /proc for a single PID."""
    proc_dir = Path(f"/proc/{pid}")
    info: Dict[str, Any] = {
        "pid": pid,
        "exists": False,
        "cmdline": "",
        "name": "",
        "state": "",
        "wchan": "",
        "stdin_target": "",
        "is_waiting_stdin": False,
        "is_waiting_network": False,
        "is_waiting_lock": False,
        "is_waiting_child": False,
        "diagnosis": "",
    }

    if not proc_dir.exists():
        return info

    info["exists"] = True

    # 1. Read cmdline & name
    try:
        with open(proc_dir / "cmdline", "rb") as f:
            raw = f.read().replace(b"\x00", b" ").strip()
            info["cmdline"] = raw.decode("utf-8", errors="replace")
    except Exception:
        pass

    try:
        with open(proc_dir / "comm", "r") as f:
            info["name"] = f.read().strip()
    except Exception:
        pass

    # 2. Read state from /proc/<pid>/status
    try:
        with open(proc_dir / "status", "r") as f:
            for line in f:
                if line.startswith("State:"):
                    info["state"] = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass

    # 3. Read wait channel from /proc/<pid>/wchan
    try:
        with open(proc_dir / "wchan", "r") as f:
            info["wchan"] = f.read().strip()
    except Exception:
        pass

    # 4. Read stdin descriptor (/proc/<pid>/fd/0)
    try:
        fd0 = proc_dir / "fd" / "0"
        if fd0.is_symlink():
            info["stdin_target"] = os.readlink(fd0)
    except Exception:
        pass

    # 5. Classify wait channel
    wchan = info["wchan"]
    if wchan in ("n_tty_read", "read_chan", "tty_read"):
        info["is_waiting_stdin"] = True
        info["diagnosis"] = f"Blocked on interactive STDIN read in kernel ({wchan})"
    elif wchan in NETWORK_WAIT_CHANNELS:
        info["is_waiting_network"] = True
        info["diagnosis"] = f"Blocked waiting on network socket I/O ({wchan})"
    elif wchan in LOCK_WAIT_CHANNELS:
        info["is_waiting_lock"] = True
        info["diagnosis"] = f"Blocked waiting on thread mutex or futex lock ({wchan})"
    elif wchan in ("do_wait", "wait4"):
        info["is_waiting_child"] = True
        info["diagnosis"] = f"Waiting on child process ({wchan})"
    elif wchan == "pipe_read":
        # Internal IPC pipe wait (e.g. git reading from helper child, or pipeline stdout)
        info["diagnosis"] = f"Waiting on IPC pipe read ({wchan})"

    return info


def scan_tail_for_prompts(text_buffer: str) -> Optional[str]:
    """Scan the tail of a process output buffer for interactive prompt patterns."""
    lines = [l.strip() for l in text_buffer.splitlines() if l.strip()]
    if not lines:
        return None

    # Check last 5 non-empty lines
    for line in reversed(lines[-5:]):
        for pat in INTERACTIVE_PROMPT_PATTERNS:
            if pat.search(line):
                return line

    return None


def diagnose_process_tree(root_pid: int, output_buffer: str = "") -> Dict[str, Any]:
    """Perform a forensic inspection of a process and all its children.
    
    Returns a comprehensive diagnostic dict explaining if and why the process tree is wedged.
    """
    root_info = inspect_single_process(root_pid)
    if not root_info["exists"]:
        return {
            "root_pid": root_pid,
            "exists": False,
            "is_wedged": False,
            "summary": f"Process PID {root_pid} does not exist.",
            "culprit": None,
            "tree": [],
        }

    all_pids = [root_pid] + get_process_children(root_pid)
    tree_info = [inspect_single_process(p) for p in all_pids if p]

    prompt_detected = scan_tail_for_prompts(output_buffer)

    culprit = None
    # Look for leaf processes blocked on STDIN, network, or locks.
    # Note: Root runner process (e.g. agy) waiting on internal pipes during normal inference
    # must never be flagged as an interactive tool block unless an explicit prompt was emitted.
    for p in reversed(tree_info):
        if p["pid"] == root_pid and len(tree_info) > 1:
            continue
        if p["pid"] == root_pid and not prompt_detected:
            continue
        if p["is_waiting_stdin"] or (prompt_detected and ("wait" in p["wchan"] or "read" in p["wchan"])):
            culprit = p
            break

    if not culprit:
        for p in reversed(tree_info):
            if p["pid"] == root_pid:
                continue
            if p["is_waiting_network"] or p["is_waiting_lock"]:
                culprit = p
                break

    is_wedged = culprit is not None or prompt_detected is not None
    summary_parts = []

    if culprit:
        c_name = culprit["name"] or culprit["cmdline"] or f"PID {culprit['pid']}"
        if culprit["is_waiting_stdin"] or prompt_detected:
            summary_parts.append(
                f"Subprocess '{c_name}' (PID {culprit['pid']}) is wedged waiting on interactive STDIN ({culprit['wchan']})."
            )
            if prompt_detected:
                summary_parts.append(f"Last output prompt: '{prompt_detected}'")
        elif culprit["is_waiting_network"]:
            summary_parts.append(
                f"Subprocess '{c_name}' (PID {culprit['pid']}) is wedged waiting on network socket I/O ({culprit['wchan']})."
            )
        elif culprit["is_waiting_lock"]:
            summary_parts.append(
                f"Subprocess '{c_name}' (PID {culprit['pid']}) is deadlocked on thread futex/mutex ({culprit['wchan']})."
            )
    elif prompt_detected:
        summary_parts.append(f"Process tree appears wedged on interactive prompt: '{prompt_detected}'.")
    else:
        summary_parts.append(f"Process tree (Root PID {root_pid}) running with {len(tree_info)} active process(es).")

    return {
        "root_pid": root_pid,
        "exists": True,
        "is_wedged": is_wedged,
        "is_interactive_stdin": bool(culprit and culprit["is_waiting_stdin"] or prompt_detected),
        "summary": " ".join(summary_parts),
        "prompt": prompt_detected,
        "culprit": culprit,
        "tree": tree_info,
    }


def get_process_age(pid: int) -> float | None:
    """Return process age in seconds."""
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        return None
    try:
        mtime = proc_dir.stat().st_mtime
        age = time.time() - mtime
        if age >= 0:
            return age
    except (OSError, PermissionError):
        pass
    try:
        with open(proc_dir / "stat", "r") as f:
            stat_parts = f.read().split()
            starttime_ticks = int(stat_parts[21])
            clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            with open("/proc/uptime", "r") as uf:
                uptime_sec = float(uf.read().split()[0])
            process_uptime = uptime_sec - (starttime_ticks / clk_tck)
            return max(0.0, process_uptime)
    except Exception:
        pass
    return None


def get_process_cpu_ticks(pid: int) -> Optional[int]:
    """Read total CPU ticks (utime + stime + cutime + cstime) from /proc/<pid>/stat."""
    stat_file = Path(f"/proc/{pid}/stat")
    if not stat_file.exists():
        return None
    try:
        stat_str = stat_file.read_text()
        rparen = stat_str.rfind(")")
        if rparen == -1:
            return None
        rest = stat_str[rparen + 1:].split()
        if len(rest) > 14:
            utime = int(rest[11])
            stime = int(rest[12])
            cutime = int(rest[13])
            cstime = int(rest[14])
            return utime + stime + cutime + cstime
    except Exception:
        pass
    return None


def get_process_tree_cpu_ticks(pid: int) -> int:
    """Sum CPU ticks for root PID and all its running children."""
    total = get_process_cpu_ticks(pid) or 0
    for cpid in get_process_children(pid):
        total += get_process_cpu_ticks(cpid) or 0
    return total


_LAST_CPU_SAMPLES: Dict[int, Tuple[float, int]] = {}


def is_process_making_progress(pid: int, sample_window: float = 0.05) -> bool:
    """Check if process or its descendants are executing commands, running tools, or consuming CPU.

    Returns True if:
    1. The process has active running child processes (e.g. running bash, python, pytest, git).
    2. CPU ticks have increased since last check or increase over a brief sample window.
    """
    children = get_process_children(pid)
    if len(children) > 0:
        return True

    curr_ticks = get_process_tree_cpu_ticks(pid)
    now = time.time()

    if pid in _LAST_CPU_SAMPLES:
        prev_ts, prev_ticks = _LAST_CPU_SAMPLES[pid]
        _LAST_CPU_SAMPLES[pid] = (now, curr_ticks)
        if curr_ticks > prev_ticks:
            return True

    if sample_window > 0:
        time.sleep(sample_window)
        next_ticks = get_process_tree_cpu_ticks(pid)
        _LAST_CPU_SAMPLES[pid] = (time.time(), next_ticks)
        return next_ticks > curr_ticks

    _LAST_CPU_SAMPLES[pid] = (now, curr_ticks)
    return False


def reap_stale_agy_processes(
    max_age_seconds: float = 600.0,
    allowed_active_pids: set[int] | None = None,
    dry_run: bool = False,
    max_stall_seconds: float = 600.0,
) -> list[dict]:
    """Scan and forcibly terminate only true failure loops or dead wedged agy subprocesses.

    Crucially, tasks making active forward progress (running tools, spawning child processes,
    consuming CPU ticks) are NEVER terminated regardless of total wall-clock elapsed time.

    Reaps ONLY:
    1. Processes wedged on interactive console STDIN (e.g. password, confirmation prompt) for >= 60s.
    2. Processes deadlocked on kernel futexes/locks with 0 CPU delta and 0 children for >= 120s.
    3. Untracked orphaned processes (not in allowed/in-flight) with 0 children and 0 CPU progress for >= 300s.
    4. Allowed processes that have experienced complete unbroken silence / stall (0 CPU delta, 0 children) for >= max_stall_seconds.
    """
    reaped = []
    allowed = set(allowed_active_pids or set())
    if not allowed:
        try:
            from tools.bridge_state import get_all_active_pids
            allowed = set(get_all_active_pids())
        except Exception:
            pass

    daemon_pids = set()
    try:
        from tools.bridge_state import get_daemon_pids
        daemon_pids = set(get_daemon_pids())
    except Exception:
        pass

    current_pid = os.getpid()

    try:
        for p in Path("/proc").iterdir():
            if not p.is_dir() or not p.name.isdigit():
                continue
            pid = int(p.name)
            if pid == current_pid or pid == 1:
                continue

            try:
                cmdline_file = p / "cmdline"
                if not cmdline_file.exists():
                    continue
                cmdline = cmdline_file.read_bytes().replace(b"\x00", b" ").decode(errors="ignore").strip()
                if not cmdline:
                    continue

                is_agy = ("agy" in cmdline.split()[0] if cmdline.split() else False) or ("antigravity" in cmdline) or ("agy " in cmdline)
                if not is_agy:
                    continue

                age = get_process_age(pid)
                if age is None:
                    continue

                is_allowed = pid in allowed or pid in daemon_pids
                is_daemon = pid in daemon_pids or ("--input-format=stream-json" in cmdline and "--print=" in cmdline)
                diag = diagnose_process_tree(pid)
                culprit = diag.get("culprit")
                culprit_pid = culprit.get("pid") if (culprit and isinstance(culprit, dict)) else pid
                culprit_age = get_process_age(culprit_pid) if culprit_pid != pid else age
                if culprit_age is None:
                    culprit_age = age

                # Check if process tree is actively making forward progress (tools running, CPU burning)
                has_progress = is_process_making_progress(pid, sample_window=0.05)

                should_reap = False
                reason = ""

                # Interactive STDIN wedge: requires true interactive stdin / prompt,
                # the culprit process itself must be >= 60s old, AND the tree is not making forward progress
                is_wedged_interactive = bool(
                    diag.get("is_interactive_stdin")
                    and culprit_age >= 60.0
                    and not has_progress
                )

                if is_wedged_interactive:
                    should_reap = True
                    reason = f"Wedged on interactive STDIN ({diag.get('summary')})"
                elif not has_progress:
                    # No active child tools and no CPU progress
                    is_lock_deadlock = bool(culprit and culprit.get("is_waiting_lock") and culprit_age >= 120.0)

                    if is_lock_deadlock:
                        should_reap = True
                        reason = f"Deadlocked on thread futex/mutex with 0 CPU delta ({culprit.get('wchan')})"
                    elif not is_allowed and not is_daemon and age >= 300.0:
                        # Untracked orphan process with zero activity
                        should_reap = True
                        reason = f"Untracked orphan process with zero activity (age: {age:.0f}s)"
                    elif is_allowed and not is_daemon and age >= 1800.0:
                        # Allowed non-daemon turn exceeding hard ceiling with zero active progress
                        should_reap = True
                        reason = f"Stalled turn exceeded hard ceiling with zero activity ({age:.0f}s >= 1800s)"
                else:
                    # Process is making forward progress (has children or CPU advancing)
                    should_reap = False

                if should_reap:
                    entry = {
                        "pid": pid,
                        "cmdline": cmdline[:120],
                        "age_seconds": age,
                        "reason": reason,
                        "reaped": False
                    }
                    if not dry_run:
                        print(f"[ProcessReaper] 🚨 Reaping stale agy process PID {pid} ({reason})...")
                        try:
                            for child_pid in get_process_children(pid):
                                try:
                                    os.kill(child_pid, signal.SIGTERM)
                                except OSError:
                                    pass
                            try:
                                os.kill(pid, signal.SIGTERM)
                            except OSError:
                                pass

                            time.sleep(0.3)

                            for child_pid in get_process_children(pid):
                                try:
                                    os.kill(child_pid, signal.SIGKILL)
                                except OSError:
                                    pass
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except OSError:
                                pass
                            entry["reaped"] = True
                        except Exception as k_err:
                            entry["error"] = str(k_err)

                    reaped.append(entry)

            except (OSError, PermissionError):
                continue
    except Exception as e:
        print(f"[ProcessReaper] Error scanning /proc: {e}")

    return reaped


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 process_probe.py <PID | reap>", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "reap":
        reaped_list = reap_stale_agy_processes()
        print(f"Reaped {len(reaped_list)} stale process(es):")
        for r in reaped_list:
            print(f"• PID {r['pid']}: {r['reason']}")
        sys.exit(0)

    target_pid = int(sys.argv[1])
    diag = diagnose_process_tree(target_pid)
    print(f"PID: {diag['root_pid']} | Wedged: {diag['is_wedged']}")
    print(f"Summary: {diag['summary']}")
    if diag["culprit"]:
        print(f"Culprit: PID {diag['culprit']['pid']} ({diag['culprit']['name']}) -> {diag['culprit']['diagnosis']}")
