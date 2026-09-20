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
    is_tty = any(t in info.get("stdin_target", "") for t in ("/dev/pts", "/dev/tty"))
    if wchan in ("n_tty_read", "read_chan", "tty_read") or (wchan == "pipe_read" and is_tty):
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
        if p["is_waiting_stdin"] or (prompt_detected and "wait" in p["wchan"]):
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


def reap_stale_agy_processes(
    max_age_seconds: float = 600.0,
    allowed_active_pids: set[int] | None = None,
    dry_run: bool = False
) -> list[dict]:
    """Scan and forcibly terminate stale or wedged agy CLI subprocesses.
    
    Terminates any agy process exceeding max_age_seconds (default: 600s / 10min)
    or deadlocked on thread locks / futexes.
    """
    import time
    import signal
    reaped = []
    allowed = allowed_active_pids or set()
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

                if pid in allowed and age < 1800.0:
                    continue

                diag = diagnose_process_tree(pid)
                is_stale_timeout = age >= max_age_seconds
                is_wedged_interactive = bool(diag.get("is_interactive_stdin") and age >= 60.0)

                if is_stale_timeout or is_wedged_interactive:
                    reason = f"Timeout exceeded ({age:.0f}s >= {max_age_seconds:.0f}s)" if is_stale_timeout else f"Wedged on interactive STDIN ({diag.get('summary')})"
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
