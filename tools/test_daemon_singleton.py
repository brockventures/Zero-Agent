#!/usr/bin/env python3
# ruff: noqa
"""
Unit tests for tools/daemon_singleton.py.
Validates strict argv /proc matching, flock exclusivity, stale PID reconciliation,
and duplicate termination.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.daemon_singleton import (
    acquire_singleton_lock,
    find_live_daemon_pids,
    is_lock_held,
    reconcile_live_daemon_pid,
    release_singleton_lock,
    terminate_duplicate_daemons,
)


class TestDaemonSingleton(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.mock_proc = self.root / "proc"
        self.mock_proc.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_mock_proc_entry(
        self,
        pid: int,
        argv: list[str],
        tgid: int | None = None,
    ) -> Path:
        pdir = self.mock_proc / str(pid)
        pdir.mkdir(parents=True, exist_ok=True)

        # Write null-byte separated argv to cmdline
        cmdline_bytes = b"\x00".join(arg.encode("utf-8") for arg in argv) + b"\x00"
        (pdir / "cmdline").write_bytes(cmdline_bytes)

        # Write status file with Tgid
        tgid_val = tgid if tgid is not None else pid
        status_text = f"Name:\tpython3\nTgid:\t{tgid_val}\nPid:\t{pid}\n"
        (pdir / "status").write_text(status_text, encoding="utf-8")

        return pdir

    def test_strict_argv_proc_matching(self):
        # 1. Genuine daemon process: banana_watcher.py _daemon
        self._create_mock_proc_entry(
            pid=1001,
            argv=["/usr/bin/python3", "/workspace/tools/banana_watcher.py", "_daemon", "--interval=30"],
        )

        # 2. CLI status check: banana_watcher.py status (should NOT match)
        self._create_mock_proc_entry(
            pid=1002,
            argv=["python3", "tools/banana_watcher.py", "status"],
        )

        # 3. python -c wrapper referencing banana_watcher (should NOT match)
        self._create_mock_proc_entry(
            pid=1003,
            argv=["python3", "-c", "import tools.banana_watcher as bw; bw.run_gateway_daemon()"],
        )

        # 4. Pytest running test_banana_watcher.py (should NOT match)
        self._create_mock_proc_entry(
            pid=1004,
            argv=["pytest", "tools/test_banana_watcher.py"],
        )

        # 5. Secondary thread of 1001 where Tgid != pid (should NOT match)
        self._create_mock_proc_entry(
            pid=1005,
            argv=["/usr/bin/python3", "/workspace/tools/banana_watcher.py", "_daemon"],
            tgid=1001,
        )

        # 6. Another genuine daemon: amazon_order_listener.py run
        self._create_mock_proc_entry(
            pid=1006,
            argv=["python3", "-u", "/workspace/tools/amazon_order_listener.py", "run"],
        )

        # 7. Daemon started via python -m tools.mcp_daemon run
        self._create_mock_proc_entry(
            pid=1007,
            argv=["/usr/bin/python3", "-m", "tools.mcp_daemon", "run"],
        )

        # Verify banana_watcher matches ONLY 1001
        bw_pids = find_live_daemon_pids("banana_watcher.py", ("_daemon",), proc_root=self.mock_proc)
        self.assertEqual(bw_pids, [1001])

        # Verify exclude_pid filters out 1001
        bw_pids_ex = find_live_daemon_pids(
            "banana_watcher.py",
            ("_daemon",),
            proc_root=self.mock_proc,
            exclude_pid=1001,
        )
        self.assertEqual(bw_pids_ex, [])

        # Verify amazon_order_listener matches ONLY 1006
        aol_pids = find_live_daemon_pids("amazon_order_listener.py", ("run",), proc_root=self.mock_proc)
        self.assertEqual(aol_pids, [1006])

        # Verify mcp_daemon matches ONLY 1007
        mcp_pids = find_live_daemon_pids("mcp_daemon.py", ("run",), proc_root=self.mock_proc)
        self.assertEqual(mcp_pids, [1007])

    def test_flock_exclusivity(self):
        lock_file = self.root / "daemon.lock"

        self.assertFalse(is_lock_held(lock_file))

        handle1 = acquire_singleton_lock(lock_file)
        self.assertIsNotNone(handle1)
        self.assertTrue(lock_file.exists())
        self.assertEqual(lock_file.read_text().strip(), str(os.getpid()))

        self.assertTrue(is_lock_held(lock_file))

        # Concurrent acquire must fail
        handle2 = acquire_singleton_lock(lock_file)
        self.assertIsNone(handle2)

        # Release lock
        release_singleton_lock(handle1)
        self.assertFalse(is_lock_held(lock_file))

        # Re-acquire succeeds
        handle3 = acquire_singleton_lock(lock_file)
        self.assertIsNotNone(handle3)
        release_singleton_lock(handle3)

    def test_reconcile_live_daemon_pid(self):
        pid_file = self.root / "daemon.pid"
        lock_file = self.root / "daemon.lock"

        # Case 1: Stale PID in file, but live instance exists in /proc
        self._create_mock_proc_entry(
            pid=2001,
            argv=["python3", "/workspace/tools/banana_watcher.py", "_daemon"],
        )
        pid_file.write_text("99999\n")

        reconciled = reconcile_live_daemon_pid(
            pid_file=pid_file,
            lock_path=lock_file,
            script_name="banana_watcher.py",
            subcommands=("_daemon",),
            proc_root=self.mock_proc,
        )
        self.assertEqual(reconciled, 2001)
        self.assertEqual(pid_file.read_text().strip(), "2001")

        # Case 2: PID file already points to valid live instance
        reconciled_again = reconcile_live_daemon_pid(
            pid_file=pid_file,
            lock_path=lock_file,
            script_name="banana_watcher.py",
            subcommands=("_daemon",),
            proc_root=self.mock_proc,
        )
        self.assertEqual(reconciled_again, 2001)

        # Case 3: No live instance exists, stale PID file should be unlinked
        (self.mock_proc / "2001" / "cmdline").unlink()
        (self.mock_proc / "2001" / "status").unlink()
        (self.mock_proc / "2001").rmdir()

        reconciled_dead = reconcile_live_daemon_pid(
            pid_file=pid_file,
            lock_path=lock_file,
            script_name="banana_watcher.py",
            subcommands=("_daemon",),
            proc_root=self.mock_proc,
        )
        self.assertIsNone(reconciled_dead)
        self.assertFalse(pid_file.exists())

    def test_terminate_duplicate_daemons(self):
        self._create_mock_proc_entry(
            pid=3001,
            argv=["python3", "/workspace/tools/banana_watcher.py", "_daemon"],
        )
        self._create_mock_proc_entry(
            pid=3002,
            argv=["python3", "/workspace/tools/banana_watcher.py", "_daemon"],
        )
        self._create_mock_proc_entry(
            pid=3003,
            argv=["python3", "/workspace/tools/banana_watcher.py", "_daemon"],
        )

        with patch("os.kill") as mock_kill:
            terminated = terminate_duplicate_daemons(
                script_name="banana_watcher.py",
                subcommands=("_daemon",),
                keep_pid=3001,
                proc_root=self.mock_proc,
            )
            self.assertEqual(sorted(terminated), [3002, 3003])
            mock_kill.assert_any_call(3002, signal.SIGTERM)
            mock_kill.assert_any_call(3003, signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
