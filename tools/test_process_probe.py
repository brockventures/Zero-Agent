#!/usr/bin/env python3
"""Unit tests for tools.process_probe."""

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
if "/workspace" not in sys.path:
    sys.path.append("/workspace")
from tools.process_probe import (
    get_process_children,
    inspect_single_process,
    scan_tail_for_prompts,
    diagnose_process_tree,
    INTERACTIVE_PROMPT_PATTERNS,
)


class TestProcessProbe(unittest.TestCase):

    def test_scan_tail_for_prompts(self):
        # Prompt detections
        self.assertIsNotNone(scan_tail_for_prompts("Need to install packages\nOk to proceed? (y)\n"))
        self.assertIsNotNone(scan_tail_for_prompts("2. On the linking page copy link.\nPaste the link: "))
        self.assertIsNotNone(scan_tail_for_prompts("Enter passphrase for key: "))
        self.assertIsNotNone(scan_tail_for_prompts("Do you want to continue? [Y/n] "))
        
        # Non-prompt outputs
        self.assertIsNone(scan_tail_for_prompts("Compilation finished successfully.\nExited 0."))
        self.assertIsNone(scan_tail_for_prompts("Running tests in parallel...\nAll 5 passed."))
        self.assertIsNone(scan_tail_for_prompts(""))

    def test_inspect_current_process(self):
        my_pid = os.getpid()
        info = inspect_single_process(my_pid)
        self.assertTrue(info["exists"])
        self.assertEqual(info["pid"], my_pid)
        self.assertTrue(len(info["name"]) > 0)

    def test_inspect_nonexistent_process(self):
        info = inspect_single_process(9999999)
        self.assertFalse(info["exists"])
        self.assertEqual(info["pid"], 9999999)

    def test_detect_stdin_wedged_subprocess(self):
        # Spawn a python child that blocks on stdin
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('READY', flush=True); sys.stdin.readline()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait for child to reach readline
            line = proc.stdout.readline()
            self.assertIn("READY", line)
            time.sleep(0.2)

            diag = diagnose_process_tree(proc.pid, output_buffer="Paste the link: ")
            self.assertTrue(diag["exists"])
            self.assertTrue(diag["is_wedged"])
            self.assertTrue(diag["is_interactive_stdin"])
            self.assertIn("wedged", diag["summary"].lower())
        finally:
            if proc.stdin: proc.stdin.close()
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
            proc.kill()
            proc.wait()

    def test_detect_normal_running_process(self):
        # Spawn a process sleeping
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            diag = diagnose_process_tree(proc.pid)
            self.assertTrue(diag["exists"])
            # While sleeping in time.sleep, it's not waiting on STDIN
            self.assertFalse(diag["is_interactive_stdin"])
        finally:
            if proc.stdin: proc.stdin.close()
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
            proc.kill()
            proc.wait()

    def test_cpu_ticks_and_progress_detection(self):
        from tools.process_probe import get_process_cpu_ticks, is_process_making_progress, reap_stale_agy_processes
        my_pid = os.getpid()
        ticks = get_process_cpu_ticks(my_pid)
        self.assertIsNotNone(ticks)
        self.assertGreaterEqual(ticks, 0)

        # Process executing CPU work should be detected as making progress
        progress = is_process_making_progress(my_pid, sample_window=0.01)
        # Should return a boolean without error
        self.assertIsInstance(progress, bool)

        # Dry-run reap with allowed pid should not reap current or active processes
        reaped = reap_stale_agy_processes(allowed_active_pids={my_pid}, dry_run=True)
        self.assertEqual(len(reaped), 0)

    def test_pipe_read_child_with_tty_stdin_not_flagged_as_stdin_wedged(self):
        """Verify that child processes reading internal IPC pipes under a PTY are NOT flagged as stdin wedged."""
        import pty
        master, slave = pty.openpty()
        try:
            parent = subprocess.Popen(
                [sys.executable, "-c", """
import subprocess, time
p = subprocess.Popen(['python3', '-c', 'import os; r, w = os.pipe(); os.read(r, 100)'])
time.sleep(5)
"""],
                stdin=slave,
                stdout=slave,
                stderr=slave
            )
            time.sleep(0.3)
            diag = diagnose_process_tree(parent.pid)
            self.assertTrue(diag["exists"])
            # Child is reading an internal pipe without interactive prompts; must NOT be flagged as interactive STDIN!
            self.assertFalse(diag["is_interactive_stdin"], f"False positive interactive stdin on internal pipe: {diag}")
        finally:
            parent.kill()
            parent.wait()
            os.close(slave)
            os.close(master)

    def test_reaper_does_not_reap_young_child_on_old_turn(self):
        """Verify that reap_stale_agy_processes does not reap a turn when the culprit child is younger than 60s."""
        from unittest.mock import patch
        from tools.process_probe import reap_stale_agy_processes

        fake_diag = {
            "root_pid": 12345,
            "exists": True,
            "is_wedged": True,
            "is_interactive_stdin": True,
            "summary": "Subprocess 'git' (PID 12346) is wedged",
            "prompt": "Password: ",
            "culprit": {"pid": 12346, "name": "git", "is_waiting_stdin": True},
            "tree": []
        }

        # Root agy is 75s old, but child git is only 5s old!
        def fake_age(pid):
            if pid == 12345:
                return 75.0
            if pid == 12346:
                return 5.0
            return 10.0

        with patch("tools.process_probe.Path.iterdir") as mock_iterdir, \
             patch("tools.process_probe.get_process_age", side_effect=fake_age), \
             patch("tools.process_probe.diagnose_process_tree", return_value=fake_diag), \
             patch("tools.process_probe.is_process_making_progress", return_value=False):

            class FakeProcPath:
                def __init__(self, pid_str):
                    self.name = pid_str
                def is_dir(self): return True
                def __truediv__(self, sub):
                    class FakeFile:
                        def exists(self): return True
                        def read_bytes(self): return b"agy --conversation=123"
                    return FakeFile()

            mock_iterdir.return_value = [FakeProcPath("12345")]
            reaped = reap_stale_agy_processes(allowed_active_pids={12345}, dry_run=True)
            # The child process was only 5s old, so it must NOT be reaped!
            self.assertEqual(len(reaped), 0, f"Young child should not cause parent reap: {reaped}")

    def test_is_agy_command_narrow_matching(self):
        """Verify narrow agy executable matching handles wrappers and rejects substring false positives."""
        from unittest.mock import patch
        from tools.process_probe import is_agy_command, reap_stale_agy_processes

        # True positives (direct, absolute path, nice/env/ionice wrappers)
        self.assertTrue(is_agy_command(b"agy --conversation=123"))
        self.assertTrue(is_agy_command(b"/usr/local/bin/agy\x00--print=hello\x00"))
        self.assertTrue(is_agy_command(b"nice\x00-n\x0015\x00/usr/local/bin/agy\x00--print=hi"))
        self.assertTrue(is_agy_command(b"/usr/bin/env\x00FOO=1\x00nice\x00-10\x00agy\x00--print=hi"))
        self.assertTrue(is_agy_command(b"ionice\x00-c\x002\x00-n\x007\x00/usr/local/bin/agy\x00--print=hi"))

        # True negatives (substrings in paths/arguments or different executable names)
        self.assertFalse(is_agy_command(b"python3\x00/workspace/tools/bridge.py\x00--dir=/root/.gemini/antigravity-cli"))
        self.assertFalse(is_agy_command(b"bash\x00-c\x00cat /root/.gemini/antigravity-cli/logs/run.log"))
        self.assertFalse(is_agy_command(b"grep\x00agy\x00notes.txt"))
        self.assertFalse(is_agy_command(b"my-agy-helper\x00--flag"))
        self.assertFalse(is_agy_command(b""))

        class FakeProcPath:
            def __init__(self, pid_str, raw_cmd):
                self.name = pid_str
                self._raw_cmd = raw_cmd
            def is_dir(self): return True
            def __truediv__(self, sub):
                raw_cmd = self._raw_cmd
                class FakeFile:
                    def exists(self): return True
                    def read_bytes(self): return raw_cmd
                return FakeFile()

        fake_diag = {"root_pid": 88801, "exists": True, "is_wedged": False, "is_interactive_stdin": False, "culprit": None}
        with patch("tools.process_probe.Path.iterdir", return_value=[
                FakeProcPath("88801", b"python3\x00/workspace/tools/sidecars.py\x00/root/.gemini/antigravity-cli"),
                FakeProcPath("88802", b"nice\x00-n\x0015\x00/usr/local/bin/agy\x00--print=orphan"),
            ]), \
             patch("tools.process_probe.get_active_forge_pids", return_value=set()), \
             patch("tools.process_probe.get_process_children", return_value=[]), \
             patch("tools.process_probe.get_process_age", return_value=450.0), \
             patch("tools.process_probe.diagnose_process_tree", return_value=fake_diag), \
             patch("tools.process_probe.is_process_making_progress", return_value=False):
            reaped = reap_stale_agy_processes(allowed_active_pids={999999}, dry_run=True)

        reaped_pids = [r["pid"] for r in reaped]
        self.assertNotIn(88801, reaped_pids, "Non-agy python process with 'antigravity' in args must never be reaped")
        self.assertIn(88802, reaped_pids, "Wrapped nice agy orphan must be detected and reaped")

    def test_deadlocked_child_progress_returns_false_and_reaping_is_reachable(self):
        """Verify is_process_making_progress returns False when a child is deadlocked with 0 CPU delta."""
        from unittest.mock import patch
        from tools.process_probe import (
            is_process_making_progress,
            reap_stale_agy_processes,
            _LAST_CPU_SAMPLES,
        )

        test_pid = 91001
        child_pid = 91002
        _LAST_CPU_SAMPLES.pop(test_pid, None)

        lock_diag = {
            "root_pid": test_pid,
            "exists": True,
            "is_wedged": True,
            "is_interactive_stdin": False,
            "summary": "Subprocess 'worker' (PID 91002) is deadlocked on thread futex/mutex (futex_wait)",
            "culprit": {
                "pid": child_pid,
                "name": "worker",
                "wchan": "futex_wait",
                "is_waiting_lock": True,
                "is_waiting_stdin": False,
            },
            "tree": [],
        }

        # Seed prior CPU sample and keep ticks flat while a child exists
        _LAST_CPU_SAMPLES[test_pid] = (time.time() - 5.0, 500)
        with patch("tools.process_probe.get_process_children", return_value=[child_pid]), \
             patch("tools.process_probe.get_process_tree_cpu_ticks", return_value=500), \
             patch("tools.process_probe.diagnose_process_tree", return_value=lock_diag):
            self.assertFalse(
                is_process_making_progress(test_pid, sample_window=0.0),
                "Deadlocked child with 0 CPU delta must NOT be considered making progress",
            )

        # When CPU ticks DO advance, progress must return True
        with patch("tools.process_probe.get_process_children", return_value=[child_pid]), \
             patch("tools.process_probe.get_process_tree_cpu_ticks", return_value=525), \
             patch("tools.process_probe.diagnose_process_tree", return_value=lock_diag):
            self.assertTrue(
                is_process_making_progress(test_pid, sample_window=0.0),
                "Advancing process-tree CPU ticks must return True",
            )

        # Verify futex-deadlock reaping is now actually reachable end-to-end in reap_stale_agy_processes
        _LAST_CPU_SAMPLES[test_pid] = (time.time() - 5.0, 525)

        class FakeProcPath:
            def __init__(self, pid_str):
                self.name = pid_str
            def is_dir(self): return True
            def __truediv__(self, sub):
                class FakeFile:
                    def exists(self): return True
                    def read_bytes(self): return b"/usr/local/bin/agy\x00--print=locked"
                return FakeFile()

        with patch("tools.process_probe.Path.iterdir", return_value=[FakeProcPath(str(test_pid))]), \
             patch("tools.process_probe.get_active_forge_pids", return_value=set()), \
             patch("tools.process_probe.get_process_children", side_effect=lambda p: [child_pid] if p == test_pid else []), \
             patch("tools.process_probe.get_process_tree_cpu_ticks", return_value=525), \
             patch("tools.process_probe.get_process_age", return_value=150.0), \
             patch("tools.process_probe.diagnose_process_tree", return_value=lock_diag), \
             patch("tools.process_probe.time.sleep"):
            reaped = reap_stale_agy_processes(allowed_active_pids={test_pid}, dry_run=True)

        self.assertEqual(len(reaped), 1)
        self.assertEqual(reaped[0]["pid"], test_pid)
        self.assertIn("Deadlocked on thread futex/mutex", reaped[0]["reason"])
        _LAST_CPU_SAMPLES.pop(test_pid, None)

    def test_reaper_exempts_detached_forge_job_descendants_with_cycle_protection(self):
        """Verify active forge_jobs PIDs and their descendant trees (even with cycles) are exempt from 300s reaping."""
        import sqlite3
        import tempfile
        from unittest.mock import patch
        from tools.process_probe import (
            get_active_forge_pids,
            expand_allowed_pids,
            reap_stale_agy_processes,
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "forge.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE forge_jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    worker_pid INTEGER,
                    child_pid INTEGER
                )
                """
            )
            conn.execute("INSERT INTO forge_jobs VALUES ('job-run', 'running', 50001, 50002)")
            conn.execute("INSERT INTO forge_jobs VALUES ('job-start', 'starting', 50004, NULL)")
            conn.execute("INSERT INTO forge_jobs VALUES ('job-done', 'completed', 60001, 60002)")
            conn.commit()
            conn.close()

            with patch("tools.forge.forge_supervisor._active_build_procs", {}):
                active_forge = get_active_forge_pids(forge_db_path=db_path)

            self.assertEqual(active_forge, {50001, 50002, 50004})

            # Cycle-safe descendant expansion: 50002 -> 50003 -> 50002
            def fake_children(p):
                if p == 50002:
                    return [50003]
                if p == 50003:
                    return [50002]
                return []

            with patch("tools.process_probe.get_process_children", side_effect=fake_children):
                expanded = expand_allowed_pids(active_forge)
            self.assertEqual(expanded, {50001, 50002, 50003, 50004})

            class FakeProcPath:
                def __init__(self, pid_str, raw_cmd):
                    self.name = pid_str
                    self._raw_cmd = raw_cmd
                def is_dir(self): return True
                def __truediv__(self, sub):
                    raw_cmd = self._raw_cmd
                    class FakeFile:
                        def exists(self): return True
                        def read_bytes(self): return raw_cmd
                    return FakeFile()

            idle_diag = {"exists": True, "is_wedged": False, "is_interactive_stdin": False, "culprit": None}
            with patch("tools.process_probe.Path.iterdir", return_value=[
                    FakeProcPath("50003", b"/usr/local/bin/agy\x00--print=detached-job-subagent"),
                    FakeProcPath("70001", b"/usr/local/bin/agy\x00--print=true-orphan"),
                ]), \
                 patch("tools.process_probe.get_active_forge_pids", return_value=active_forge), \
                 patch("tools.process_probe.get_process_children", side_effect=fake_children), \
                 patch("tools.process_probe.get_process_age", return_value=420.0), \
                 patch("tools.process_probe.diagnose_process_tree", return_value=idle_diag), \
                 patch("tools.process_probe.is_process_making_progress", return_value=False):
                reaped = reap_stale_agy_processes(allowed_active_pids={999999}, dry_run=True)

            reaped_pids = [r["pid"] for r in reaped]
            self.assertNotIn(50003, reaped_pids, "Detached forge_jobs descendant agy must NOT be reaped at 300s")
            self.assertIn(70001, reaped_pids, "Untracked orphan agy at 420s with 0 activity must be reaped")


if __name__ == "__main__":
    unittest.main()
