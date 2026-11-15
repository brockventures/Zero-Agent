#!/usr/bin/env python3
# ruff: noqa
"""
tools/daemon_singleton.py
Reusable singleton locking and /proc live-process guard helpers.
Prevents duplicate listener/watchdog daemon spawns via OS flock exclusivity
and strict argv-based /proc inspection.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import signal
import time


def find_live_daemon_pids(
    script_name: str,
    subcommands: tuple[str, ...] = ("_daemon", "run"),
    proc_root: str | Path = "/proc",
    exclude_pid: int | None = None,
) -> list[int]:
    """
    Inspect /proc/<pid>/cmdline and /proc/<pid>/status to find live daemon instances.

    - Splits cmdline on null bytes into argv tokens (rejects naive substring matching
      such as 'python -c', 'pytest', or 'agy' invocations).
    - Verifies status 'Tgid == pid' to skip non-leader threads.
    - Matches only Python processes whose script argv token basename matches script_name
      and whose remaining argv contains one of subcommands.
    """
    proc_path = Path(proc_root)
    if not proc_path.exists():
        return []

    # Normalize accepted script basenames (e.g. 'banana_watcher.py' or 'banana_watcher')
    if script_name.endswith(".py"):
        accepted_names = {script_name, script_name[:-3]}
    else:
        accepted_names = {script_name, f"{script_name}.py"}

    live_pids: list[int] = []

    try:
        entries = list(proc_path.iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue

        if exclude_pid is not None and pid == exclude_pid:
            continue

        status_file = entry / "status"
        cmdline_file = entry / "cmdline"

        try:
            # 1. Verify Tgid == pid in status to avoid non-leader threads
            status_text = status_file.read_text(encoding="utf-8", errors="ignore")
            tgid = None
            for line in status_text.splitlines():
                if line.startswith("Tgid:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            tgid = int(parts[1])
                        except ValueError:
                            pass
                    break
            if tgid is not None and tgid != pid:
                continue

            # 2. Inspect argv tokens from cmdline
            raw = cmdline_file.read_bytes()
            if not raw:
                continue

            tokens = [t.decode("utf-8", errors="replace") for t in raw.split(b"\x00") if t]
            if not tokens:
                continue

            first_token_name = Path(tokens[0]).name.lower()
            if not (
                first_token_name == "python"
                or first_token_name.startswith(("python3", "python2", "python."))
            ):
                continue

            # Parse argv after python interpreter
            matched = False
            script_idx = -1

            for idx in range(1, len(tokens)):
                tok = tokens[idx]
                if tok == "-c":
                    # python -c is an inline code string, never the daemon script
                    break
                if tok == "-m":
                    # python -m module_name
                    if idx + 1 < len(tokens):
                        mod_name = tokens[idx + 1]
                        mod_base = mod_name.split(".")[-1]
                        if mod_base in accepted_names or f"{mod_base}.py" in accepted_names:
                            script_idx = idx + 1
                            matched = True
                    break
                if tok.startswith("-"):
                    # Interpreter options like -u, -W, -O
                    continue

                # First positional argument is the script file
                base = Path(tok).name
                if base in accepted_names:
                    script_idx = idx
                    matched = True
                break

            if not matched or script_idx < 0:
                continue

            remaining_argv = tokens[script_idx + 1 :]
            if subcommands and not any(sub in remaining_argv for sub in subcommands):
                continue

            live_pids.append(pid)

        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue

    return sorted(live_pids)


def acquire_singleton_lock(lock_path: str | Path):
    """
    Open/create lock file and acquire non-blocking flock(LOCK_EX | LOCK_NB).
    Writes current PID to the locked file and returns open file handle.
    Returns None if already locked.
    """
    path = Path(lock_path)
    fd = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path, "a+")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.seek(0)
        fd.truncate()
        fd.write(f"{os.getpid()}\n")
        fd.flush()
        return fd
    except (BlockingIOError, OSError):
        if fd is not None:
            try:
                fd.close()
            except Exception:
                pass
        return None


def release_singleton_lock(lock_handle) -> None:
    """Safely unlock and close the lock handle."""
    if lock_handle is None:
        return
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_handle.close()
    except Exception:
        pass


def is_lock_held(lock_path: str | Path) -> bool:
    """Probe whether another process holds LOCK_EX on lock_path."""
    path = Path(lock_path)
    if not path.exists():
        return False
    try:
        with open(path, "a+") as fd:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                return False
            except (BlockingIOError, OSError) as e:
                if (
                    getattr(e, "errno", None) in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES)
                    or isinstance(e, BlockingIOError)
                ):
                    return True
                return False
    except OSError:
        return False


def reconcile_live_daemon_pid(
    pid_file: str | Path,
    lock_path: str | Path,
    script_name: str,
    subcommands: tuple[str, ...],
    proc_root: str | Path = "/proc",
) -> int | None:
    """
    Check pid_file using strict argv verification, discover live PIDs via
    find_live_daemon_pids, reconcile pid_file when a live instance exists,
    or unlink stale pid_file when none is running.
    """
    pid_file = Path(pid_file)
    lock_path = Path(lock_path)
    live_pids = find_live_daemon_pids(
        script_name=script_name,
        subcommands=subcommands,
        proc_root=proc_root,
    )

    recorded_pid = None
    if pid_file.exists():
        try:
            content = pid_file.read_text(encoding="utf-8").strip()
            if content.isdigit():
                recorded_pid = int(content)
        except OSError:
            pass

    if recorded_pid is not None and recorded_pid in live_pids:
        return recorded_pid

    if live_pids:
        # Prefer PID in lock_path if it is alive, otherwise use first live PID
        lock_pid = None
        if lock_path.exists():
            try:
                l_content = lock_path.read_text(encoding="utf-8").strip()
                if l_content.isdigit():
                    lock_pid = int(l_content)
            except OSError:
                pass

        chosen_pid = lock_pid if lock_pid is not None and lock_pid in live_pids else live_pids[0]
        try:
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(f"{chosen_pid}\n", encoding="utf-8")
        except OSError:
            pass
        return chosen_pid

    # No live instance running, clean up stale pid_file
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def terminate_duplicate_daemons(
    script_name: str,
    subcommands: tuple[str, ...],
    keep_pid: int | None = None,
    proc_root: str | Path = "/proc",
) -> list[int]:
    """Safely SIGTERM (with brief wait + SIGKILL fallback) any extra live instances other than keep_pid."""
    live_pids = find_live_daemon_pids(
        script_name=script_name,
        subcommands=subcommands,
        proc_root=proc_root,
    )
    my_pid = os.getpid()
    to_kill = [p for p in live_pids if p != keep_pid and p != my_pid]
    if not to_kill:
        return []

    is_real_proc = str(proc_root) == "/proc"
    terminated = []
    for pid in to_kill:
        try:
            os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
        except ProcessLookupError:
            if not is_real_proc:
                terminated.append(pid)
        except OSError:
            pass

    if not is_real_proc:
        return terminated

    remaining = set(terminated)
    for _ in range(10):
        if not remaining:
            break
        time.sleep(0.05)
        for pid in list(remaining):
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, OSError):
                remaining.discard(pid)

    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    return terminated
