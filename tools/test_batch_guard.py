import unittest
import os
import time
import sys

sys.path.insert(0, "/workspace/.agents/hooks")
from batch_guard import check_batching, check_tool_use, STATE_FILE, load_state, save_state


class TestBatchGuard(unittest.TestCase):
    def setUp(self):
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    def tearDown(self):
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    def test_consecutive_inspections_blocked(self):
        for i in range(3):
            allowed, reason = check_batching("docker ps")
            self.assertTrue(allowed, f"Call {i+1} should be allowed")

        allowed, reason = check_batching("ls -la /workspace")
        self.assertFalse(allowed, "4th consecutive inspection should be blocked")
        self.assertIn("Batch Invariant Triggered", reason)

    def test_sed_awk_counted_as_shell_inspections(self):
        allowed, _ = check_batching("sed -n '100,150p' /workspace/tools/foo.py")
        self.assertTrue(allowed)
        allowed, _ = check_batching("awk 'NR>=150 && NR<=200' /workspace/tools/foo.py")
        self.assertTrue(allowed)
        allowed, _ = check_batching("wc -l /workspace/tools/foo.py")
        self.assertTrue(allowed)
        allowed, reason = check_batching("head -n 50 /workspace/tools/foo.py")
        self.assertFalse(allowed)
        self.assertIn("4 consecutive single-line inspection commands", reason)

    def test_batched_command_resets_counter(self):
        for _ in range(3):
            check_batching("docker ps")

        allowed, _ = check_batching("docker ps && ls -la")
        self.assertTrue(allowed)

        allowed, _ = check_batching("docker ps")
        self.assertTrue(allowed)

    def test_scratch_script_resets_counter(self):
        for _ in range(3):
            check_batching("docker ps")

        allowed, _ = check_batching("python3 /workspace/scratch/check.py")
        self.assertTrue(allowed)

        allowed, _ = check_batching("docker ps")
        self.assertTrue(allowed)

    def test_compound_command_allowed(self):
        allowed, _ = check_batching("ssh testuser@remote-host.local 'docker ps; crontab -l' ")
        self.assertTrue(allowed)

    def test_ssh_inspection_counting(self):
        for _ in range(3):
            allowed, _ = check_batching('ssh testuser@remote-host.local "docker ps"')
            self.assertTrue(allowed)

        allowed, _ = check_batching('ssh testuser@remote-host.local "crontab -l"')
        self.assertFalse(allowed)

    def test_view_file_same_file_blocks_on_third_call(self):
        allowed, _ = check_tool_use("view_file", {"AbsolutePath": "/workspace/tools/foo.py", "StartLine": 1, "EndLine": 50})
        self.assertTrue(allowed)
        allowed, _ = check_tool_use("view_file", {"AbsolutePath": "/workspace/tools/foo.py", "StartLine": 51, "EndLine": 100})
        self.assertTrue(allowed)
        allowed, reason = check_tool_use("view_file", {"AbsolutePath": "/workspace/tools/foo.py", "StartLine": 101, "EndLine": 150})
        self.assertFalse(allowed)
        self.assertIn("3 consecutive view_file calls on '/workspace/tools/foo.py'", reason)

    def test_view_file_wide_slice_allows_up_to_four(self):
        for i in range(4):
            allowed, _ = check_tool_use(
                "view_file",
                {"AbsolutePath": "/baseball/big-projections/predict_big.py", "StartLine": i * 500 + 1, "EndLine": (i + 1) * 500},
            )
            self.assertTrue(allowed, f"Wide slice {i+1} should be allowed")
        allowed, _ = check_tool_use(
            "view_file",
            {"AbsolutePath": "/baseball/big-projections/predict_big.py", "StartLine": 2001, "EndLine": 2500},
        )
        self.assertFalse(allowed)

    def test_view_file_across_different_files_blocks_on_seventh(self):
        files = ["/a.py", "/b.py", "/c.py", "/d.py", "/e.py", "/f.py"]
        for f in files:
            allowed, _ = check_tool_use("view_file", {"AbsolutePath": f})
            self.assertTrue(allowed)
        allowed, reason = check_tool_use("view_file", {"AbsolutePath": "/g.py"})
        self.assertFalse(allowed)
        self.assertIn("7 consecutive serial view_file calls", reason)

    def test_proc_fd_stream_read_blocked(self):
        allowed, reason = check_batching("tail -n 120 /proc/1/fd/1 /proc/1/fd/2")
        self.assertFalse(allowed)
        self.assertIn("Proc FD Stream Guard Triggered", reason)

    def test_replace_file_content_same_file_blocks_on_fourth(self):
        for _ in range(3):
            allowed, _ = check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
            self.assertTrue(allowed)
        allowed, reason = check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
        self.assertFalse(allowed)
        self.assertIn("4 consecutive replace_file_content calls on '/workspace/tools/bar.py'", reason)

    def test_consecutive_inline_python_probes_blocked_after_two_and_reset_on_progress(self):
        for i in range(2):
            allowed, _ = check_batching(f"python3 - << 'EOF'\nprint({i})\nEOF")
            self.assertTrue(allowed, f"Probe {i+1} should be allowed")
        allowed, reason = check_batching("python3 -c 'print(3)'")
        self.assertFalse(allowed)
        self.assertIn("consecutive inline Python probe commands", reason)

        # Running a real script or editing a file resets consecutive_inline_python
        allowed, _ = check_batching("python3 /workspace/scratch/calibrate.py")
        self.assertTrue(allowed)
        allowed, _ = check_batching("python3 -c 'print(1)'")
        self.assertTrue(allowed)

    def test_chained_inline_python_still_counted_as_probe(self):
        allowed, _ = check_batching("python3 -c 'print(1)' && echo done")
        self.assertTrue(allowed)
        allowed, _ = check_batching("python3 -c 'print(2)' && echo done")
        self.assertTrue(allowed)
        allowed, reason = check_batching("python3 -c 'print(3)'")
        self.assertFalse(allowed)
        self.assertIn("consecutive inline Python probe commands", reason)

    def test_unbounded_ha_sqlite_query_blocked(self):
        unbounded_cmd = "sqlite3 /docker/homeassistant/config/home-assistant_v2.db \"SELECT state FROM states WHERE metadata_id = 1 ORDER BY last_updated_ts DESC LIMIT 50;\""
        allowed, reason = check_batching(unbounded_cmd)
        self.assertFalse(allowed)
        self.assertIn("HA Recorder SQLite Guard Triggered", reason)

        bounded_cmd = "sqlite3 -readonly /docker/homeassistant/config/home-assistant_v2.db \"SELECT state FROM states WHERE metadata_id = 1 AND last_updated_ts > 1700000000 ORDER BY last_updated_ts DESC LIMIT 50;\""
        allowed, _ = check_batching(bounded_cmd)
        self.assertTrue(allowed)

    def test_no_cumulative_turn_tool_call_or_scratch_write_cap(self):
        """Ensure productive deep turns (>40 tool calls and >5 scratch writes) are never artificially capped."""
        for i in range(45):
            allowed, _ = check_tool_use("write_to_file", {"TargetFile": f"/baseball/big-projections/scratch/opt_{i}.py"})
            self.assertTrue(allowed, f"Write {i+1} should be allowed")
            allowed, _ = check_batching(f"python3 /baseball/big-projections/scratch/opt_{i}.py")
            self.assertTrue(allowed, f"Run {i+1} should be allowed")

    def test_test_command_resets_file_edits(self):
        for _ in range(3):
            check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
        allowed, _ = check_tool_use("run_command", {"CommandLine": "python3 -m unittest test_bar.py"})
        self.assertTrue(allowed)
        allowed, _ = check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
        self.assertTrue(allowed)

    def test_write_to_file_resets_all_counters(self):
        for _ in range(3):
            check_tool_use("view_file", {"AbsolutePath": f"/{_}.py"})
        allowed, _ = check_tool_use("write_to_file", {"TargetFile": "/workspace/foo.py"})
        self.assertTrue(allowed)
        state = load_state()
        self.assertEqual(state["consecutive_file_views"], 0)
        self.assertEqual(state["cmd_inspections"], 0)
        self.assertEqual(state.get("consecutive_git_cmds", 0), 0)

    def test_unscoped_pytest_blocks(self):
        for cmd in ["pytest", "pytest -v", "pytest tests", "pytest tests/", "pytest ."]:
            allowed, reason = check_tool_use("run_command", {"CommandLine": cmd})
            self.assertFalse(allowed, f"Expected {cmd} to be blocked")
            self.assertIn("Unscoped full test suite execution", reason)

    def test_scoped_pytest_allows(self):
        for cmd in ["pytest tests/test_foo.py", "pytest -k test_bar", "pytest tests/test_bar.py::test_func"]:
            allowed, _ = check_tool_use("run_command", {"CommandLine": cmd})
            self.assertTrue(allowed, f"Expected {cmd} to be allowed")

    def test_serial_git_lifecycle_blocks_on_third(self):
        allowed, _ = check_tool_use("run_command", {"CommandLine": "git add file.py"})
        self.assertTrue(allowed)
        allowed, _ = check_tool_use("run_command", {"CommandLine": "git commit -m 'feat: update'"})
        self.assertTrue(allowed)
        allowed, reason = check_tool_use("run_command", {"CommandLine": "git push origin branch"})
        self.assertFalse(allowed)
        self.assertIn("3 consecutive unbatched git lifecycle commands in serial", reason)

    def test_compound_git_pipeline_allows(self):
        allowed, _ = check_tool_use("run_command", {"CommandLine": "git add file.py && git commit -m 'feat: update' && git push origin branch"})
        self.assertTrue(allowed)

    def test_agora_order_move_prohibited(self):
        for cmd in ["python3 /workspace/tools/agora_play.py order --side BUY --good ORE --qty 250", "python3 /workspace/tools/agora_play.py move --dest earth"]:
            allowed, reason = check_tool_use("run_command", {"CommandLine": cmd})
            self.assertFalse(allowed, f"Expected {cmd} to be blocked")
            self.assertIn("Agora Fast-Path Invariant", reason)

    def test_agora_status_blocks_on_second(self):
        allowed, _ = check_tool_use("run_command", {"CommandLine": "python3 /workspace/tools/agora_play.py status"})
        self.assertTrue(allowed, "First agora status should be allowed")
        allowed, reason = check_tool_use("run_command", {"CommandLine": "python3 /workspace/tools/agora_play.py eval"})
        self.assertFalse(allowed, "Second agora status/eval should be blocked")
        self.assertIn("Agora Fast-Path Invariant", reason)

    def test_versioned_scratch_sweep_loop_blocked_after_three(self):
        for v in range(1, 4):
            allowed, _ = check_tool_use(
                "write_file",
                {"Path": f"/root/.gemini/antigravity-cli/brain/0c46714e/scratch/sim_31_v{v}.py"},
            )
            self.assertTrue(allowed, f"sim_31_v{v}.py should be allowed")
        allowed, reason = check_tool_use(
            "write_file",
            {"Path": "/root/.gemini/antigravity-cli/brain/0c46714e/scratch/sim_31_v4.py"},
        )
        self.assertFalse(allowed, "4th versioned scratch sweep script should be blocked")
        self.assertIn("Modal Offload Invariant Triggered", reason)
        self.assertIn("modal_harness.py", reason)

    def test_read_file_and_edit_file_supported(self):
        allowed, _ = check_tool_use("read_file", {"Path": "/workspace/tools/foo.py", "StartLine": 1, "EndLine": 50})
        self.assertTrue(allowed)
        allowed, _ = check_tool_use("read_file", {"Path": "/workspace/tools/foo.py", "StartLine": 51, "EndLine": 100})
        self.assertTrue(allowed)
        allowed, reason = check_tool_use("read_file", {"Path": "/workspace/tools/foo.py", "StartLine": 101, "EndLine": 150})
        self.assertFalse(allowed)
        self.assertIn("3 consecutive read_file calls", reason)

        for _ in range(3):
            allowed, _ = check_tool_use("edit_file", {"Path": "/workspace/tools/bar.py"})
            self.assertTrue(allowed)
        allowed, reason = check_tool_use("edit_file", {"Path": "/workspace/tools/bar.py"})
        self.assertFalse(allowed)
        self.assertIn("4 consecutive edit_file calls", reason)

    def test_forge_tier2_tier3_gate_blocks_third_inline_production_file(self):
        # 2 production files + unit test + scratch plan allowed in Tier 1
        self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/alpha.py"})[0])
        self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/beta.py"})[0])
        self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/test_alpha.py"})[0])
        self.assertTrue(check_tool_use("write_file", {"Path": "/workspace/scratch/forge_plan.json"})[0])

        # 3rd distinct production file in interactive turn is blocked by FORGE Tier 2/3 Gate
        allowed, reason = check_tool_use("edit_file", {"Path": "/workspace/tools/gamma.py"})
        self.assertFalse(allowed)
        self.assertIn("FORGE Tier 2/3 Gate Triggered", reason)
        self.assertIn("forge.py enqueue", reason)

        # Inside an isolated /tmp/forge-wt-* worktree, multi-file edits are unrestricted
        self.assertTrue(
            check_tool_use(
                "edit_file",
                {"Path": "/tmp/forge-wt-fg-123-c0/tools/gamma.py"},
                cwd="/tmp/forge-wt-fg-123-c0",
            )[0]
        )

        # Enqueueing a FORGE plan resets the inline production file counter
        self.assertTrue(
            check_tool_use(
                "run_command",
                {"CommandLine": "python3 /workspace/tools/forge.py enqueue /workspace/scratch/forge_plan.json"},
            )[0]
        )
        self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/delta.py"})[0])


if __name__ == "__main__":
    unittest.main()
