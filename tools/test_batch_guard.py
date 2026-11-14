import unittest
import unittest.mock
import os
import sys
import json

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_TOOLS_DIR = os.path.join(_REPO_ROOT, "tools")
_HOOKS_DIR = os.path.join(_REPO_ROOT, ".agents", "hooks")
if "/workspace" in sys.path:
    sys.path.remove("/workspace")
sys.path.insert(0, _HOOKS_DIR if os.path.isdir(_HOOKS_DIR) else "/workspace/.agents/hooks")
if os.path.isfile(os.path.join(_TOOLS_DIR, "batch_guard.py")):
    sys.path.insert(0, _TOOLS_DIR)
sys.path.insert(0, _REPO_ROOT)
from batch_guard import (
    STATE_FILE,
    check_batching,
    check_forge_post_offload_lock,
    check_interactive_investigation_gate,
    check_tool_use,
    has_forge_job_start_command,
    is_exempt_from_investigation_gate,
    load_state,
    FORGE_CHANNEL_ID_STR,
)
import pytest


@pytest.fixture(autouse=True)
def isolate_forge_worker_env(monkeypatch):
    monkeypatch.delenv("FORGE_WORKER_ACTIVE", raising=False)
    monkeypatch.delenv("DISCORD_CHANNEL_ID", raising=False)
    monkeypatch.setattr("batch_guard.IN_FLIGHT_TURN_FILE", "/nonexistent_in_flight_turn.json")


class TestBatchGuard(unittest.TestCase):
    def setUp(self):
        self._orig_state = None
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                self._orig_state = f.read()
            os.remove(STATE_FILE)
        self._docker_patcher = unittest.mock.patch("shutil.which", return_value="/usr/bin/docker")
        self._docker_patcher.start()
        self._env_patcher = unittest.mock.patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""})
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()
        self._docker_patcher.stop()
        if os.path.exists("/workspace/scratch/forge_plan.json"):
            try:
                os.remove("/workspace/scratch/forge_plan.json")
            except OSError:
                pass
        if self._orig_state is not None:
            with open(STATE_FILE, "w") as f:
                f.write(self._orig_state)
        elif os.path.exists(STATE_FILE):
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

    def test_readonly_shared_mount_blocked_with_zero_ops_escalation(self):
        from unittest.mock import patch
        with patch("os.path.realpath", return_value="/opt/zero/workspace/tools/bridge_formatting.py"):
            allowed, reason = check_tool_use("edit_file", {"Path": "/workspace/tools/bridge_formatting.py"})
            self.assertFalse(allowed)
            self.assertIn("Read-Only Shared Mount Guard", reason)
            self.assertIn("outbox.py --channel zero-ops", reason)
            self.assertIn("<@1542285964213358633>", reason)

    def test_replace_file_content_same_file_allows_multiple_surgical_edits(self):
        for _ in range(6):
            allowed, _ = check_tool_use("replace_file_content", {"TargetFile": "/workspace/tools/bar.py"})
            self.assertTrue(allowed)

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

        for _ in range(6):
            allowed, _ = check_tool_use("edit_file", {"Path": "/workspace/tools/bar.py"})
            self.assertTrue(allowed)

    def test_forge_tier2_tier3_gate_blocks_fourth_inline_production_file(self):
        # Up to 3 production files + unit test + markdown docs + scratch plan allowed in Tier 1
        with unittest.mock.patch("os.access", return_value=True):
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/alpha.py"})[0])
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/beta.py"})[0])
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/gamma.py"})[0])
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/test_alpha.py"})[0])
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/GEMINI.md"})[0])
            self.assertTrue(check_tool_use("write_file", {"Path": "/workspace/scratch/forge_plan.json"})[0])

        # 4th distinct production file in interactive turn is blocked by FORGE Tier 2/3 Gate
        allowed, reason = check_tool_use("edit_file", {"Path": "/workspace/tools/delta.py"})
        self.assertFalse(allowed)
        self.assertIn("FORGE Tier 2/3 Gate Triggered", reason)
        self.assertIn("forge.py enqueue", reason)

        # Inside an isolated /tmp/forge-wt-* worktree, multi-file edits are unrestricted
        self.assertTrue(
            check_tool_use(
                "edit_file",
                {"Path": "/tmp/forge-wt-fg-123-c0/tools/delta.py"},
                cwd="/tmp/forge-wt-fg-123-c0",
            )[0]
        )

        # Enqueueing a FORGE plan activates the Post-Offload Turn Lock so the agent must end its turn
        plan_path = "/workspace/scratch/forge_plan.json"
        valid_plan = {
            "title": "Valid Tier2 Plan",
            "origin_channel_id": "123456789",
            "repo": "/workspace",
            "context_fact_sheet": ["Fact 1"],
            "chunks": [
                {
                    "chunk_id": "c0",
                    "title": "Chunk 0",
                    "instructions": "Implement feature and tests deterministically",
                    "target_files": ["tools/test_batch_guard.py"],
                    "target_symbols": ["TestBatchGuard"],
                    "verification_gate": "pytest tools/test_batch_guard.py",
                }
            ],
        }
        os.makedirs(os.path.dirname(plan_path), exist_ok=True)
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(valid_plan, f)
        try:
            self.assertTrue(
                check_tool_use(
                    "run_command",
                    {"CommandLine": f"python3 /workspace/tools/forge.py enqueue {plan_path}"},
                )[0]
            )
            allowed_after_enqueue, lock_reason = check_tool_use("edit_file", {"Path": "/workspace/tools/epsilon.py"})
            self.assertFalse(allowed_after_enqueue)
            self.assertIn("FORGE Post-Offload Turn Lock Triggered", lock_reason)
        finally:
            if os.path.exists(plan_path):
                os.remove(plan_path)

    def test_45s_idle_reset_preserves_inline_prod_files_until_15m_expiry(self):
        from unittest.mock import patch
        base_now = 1_800_000_000.0
        with patch("time.time", return_value=base_now):
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/alpha.py"})[0])
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/beta.py"})[0])
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/gamma.py"})[0])
            # Consecutive shell inspection counter reaches 3
            self.assertTrue(check_batching("docker ps")[0])
            self.assertTrue(check_batching("ls -la /workspace")[0])
            self.assertTrue(check_batching("uptime")[0])

        # 60 seconds later (>45s TURN_RESET_IDLE_SECONDS, <900s TURN_STATE_EXPIRY_SECONDS):
        # Short-window inspection counter resets, but inline_prod_files is preserved!
        with patch("time.time", return_value=base_now + 60.0):
            self.assertTrue(check_batching("docker ps")[0])
            allowed, reason = check_tool_use("edit_file", {"Path": "/workspace/tools/delta.py"})
            self.assertFalse(allowed)
            self.assertIn("FORGE Tier 2/3 Gate Triggered", reason)

        # 1000 seconds later (>900s TURN_STATE_EXPIRY_SECONDS): full turn state expires cleanly
        with patch("time.time", return_value=base_now + 1000.0):
            self.assertTrue(check_tool_use("edit_file", {"Path": "/workspace/tools/delta.py"})[0])

    def test_repeated_same_scratch_script_loop_blocked_after_three_runs(self):
        for i in range(3):
            allowed, _ = check_batching("python3 /workspace/scratch/tune_k.py")
            self.assertTrue(allowed, f"Run {i+1} should be allowed")
        allowed, reason = check_batching("python3 /workspace/scratch/tune_k.py")
        self.assertFalse(allowed)
        self.assertIn("FORGE Scratch Loop Gate Triggered", reason)
        self.assertIn("forge.py job start", reason)

        # Launching via forge.py job start is allowed and activates the Post-Offload Turn Lock for the rest of the turn
        allowed, _ = check_batching(
            'python3 /workspace/tools/forge.py job start --name "Tune K" --command "python3 /workspace/scratch/tune_k.py"'
        )
        self.assertTrue(allowed)
        allowed_after_job, reason_after_job = check_batching("python3 /workspace/scratch/tune_k.py")
        self.assertFalse(allowed_after_job)
        self.assertIn("FORGE Post-Offload Turn Lock Triggered", reason_after_job)

    def test_heavy_inline_benchmark_and_migration_blocked_outside_forge(self):
        for heavy_cmd in [
            "python3 /baseball/big-projections/run_unified_benchmark_gate.py",
            "python3 /baseball/big-projections/predict_big.py --migrate-db",
            "python3 /baseball/big-projections/fast_migrate_v2.py",
            "python3 /baseball/big-projections/backfill_milb_splits.py",
        ]:
            allowed, reason = check_batching(heavy_cmd)
            self.assertFalse(allowed, f"Expected {heavy_cmd} to be blocked inline")
            self.assertIn("FORGE Background Offload Gate Triggered", reason)

        # Allowed when wrapped in forge.py job start or inside a FORGE worktree
        self.assertTrue(
            check_batching(
                'python3 /workspace/tools/forge.py job start --name "Benchmark" --command "python3 /baseball/big-projections/run_unified_benchmark_gate.py"'
            )[0]
        )
        self.assertTrue(
            check_tool_use(
                "run_command",
                {"CommandLine": "python3 /baseball/big-projections/run_unified_benchmark_gate.py"},
                cwd="/tmp/forge-wt-fg-999-c0",
            )[0]
        )

    def test_tmp_versioned_sweep_and_90s_calibration_offload_gate(self):
        from unittest.mock import patch
        # 1. /tmp/v1.py .. /tmp/v4.py versioned sweep loop blocked
        for v in range(1, 4):
            self.assertTrue(check_tool_use("write_file", {"Path": f"/tmp/v{v}.py"})[0])
        allowed, reason = check_tool_use("write_file", {"Path": "/tmp/v4.py"})
        self.assertFalse(allowed)
        self.assertIn("Modal Offload Invariant Triggered", reason)

        # 2. 90s Calibration Offload Gate blocks renamed scratch/tmp scripts once turn exceeds 90s
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        base_now = 1_800_100_000.0
        with patch("time.time", return_value=base_now):
            self.assertTrue(check_batching("python3 /workspace/scratch/diag_1.py")[0])
            self.assertTrue(check_batching("python3 /tmp/diag_2.py")[0])

        # At +100s (>90s Tier 1 ceiling), 3rd distinct calibration script is blocked
        with patch("time.time", return_value=base_now + 100.0):
            allowed, reason = check_batching("python3 /workspace/scratch/opt_renamed_3.py")
            self.assertFalse(allowed)
            self.assertIn("FORGE 90s Calibration Offload Gate Triggered", reason)

            # And inline production file edit is also gated because >90s calibration occurred
            allowed_edit, edit_reason = check_tool_use(
                "edit_file", {"Path": "/baseball/big-projections/hitter_components.py"}
            )
            self.assertFalse(allowed_edit)
            self.assertIn("FORGE 90s Tier 2/3 Gate Triggered", edit_reason)

    def test_shell_production_mutation_bypass_blocked(self):
        bypass_cmds = [
            'python3 -c \'with open("/baseball/big-projections/pitcher_components.py", "w") as f: f.write("x=1")\'',
            "sed -i 's/foo/bar/g' /baseball/big-projections/hitter_components.py",
            "cat >> /workspace/tools/bridge.py << 'EOF'\n# test\nEOF",
            'python3 - << \'EOF\'\nwith open("/docker/shelfmark/custom_patches/openlibrary.py", "w") as f: f.write("x=1")\nEOF',
        ]
        for cmd in bypass_cmds:
            allowed, reason = check_batching(cmd)
            self.assertFalse(allowed, f"Expected shell mutation bypass to be blocked: {cmd}")
            self.assertIn("FORGE Shell Mutation Bypass Blocked", reason)

    def test_inline_heredoc_and_command_budget_gated_after_90s(self):
        from unittest.mock import patch
        base_now = 1_800_200_000.0
        with patch("time.time", return_value=base_now):
            # 2 inline python heredoc probes + 1 curl reset
            self.assertTrue(check_batching("python3 - << 'EOF'\nprint(1)\nEOF")[0])
            self.assertTrue(check_batching("python3 - << 'EOF'\nprint(2)\nEOF")[0])
            self.assertTrue(check_batching("curl -s http://127.0.0.1:8769/health && echo ok")[0])

        # At +100s (>90s Tier 1 ceiling), 3rd inline Python probe is blocked by 90s Calibration Offload Gate
        with patch("time.time", return_value=base_now + 100.0):
            allowed, reason = check_batching("python3 - << 'EOF'\nprint(3)\nEOF")
            self.assertFalse(allowed)
            self.assertIn("FORGE 90s Calibration Offload Gate Triggered", reason)

        # Command budget gate: >15 non-probe run_command steps after 90s are blocked
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        with patch("time.time", return_value=base_now):
            for i in range(15):
                self.assertTrue(check_batching(f"curl -s http://127.0.0.1:8769/health && echo {i}")[0])
        with patch("time.time", return_value=base_now + 100.0):
            allowed, reason = check_batching("curl -s http://127.0.0.1:8769/health && echo 16")
            self.assertFalse(allowed)
            self.assertIn("FORGE 90s Command Budget Gate Triggered", reason)
            self.assertNotIn("--no-synthesize", reason)

    def test_diagnostic_scratch_scripts_allowed_up_to_8_after_90s_while_calibration_gated_at_3(self):
        from unittest.mock import patch
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        base_now = 1_800_250_000.0
        with patch("time.time", return_value=base_now):
            self.assertTrue(check_batching("python3 /workspace/scratch/diag_1.py")[0])
        with patch("time.time", return_value=base_now + 100.0):
            # Up to 8 diagnostic scratch scripts are permitted after 90s without triggering the calibration gate
            for i in range(2, 9):
                allowed, reason = check_batching(f"python3 /workspace/scratch/diag_{i}.py")
                self.assertTrue(allowed, f"Diagnostic script diag_{i}.py should be allowed after 90s, got: {reason}")

            # 9th diagnostic script after 90s triggers the 90s Calibration Offload Gate
            allowed_9, reason_9 = check_batching("python3 /workspace/scratch/diag_9.py")
            self.assertFalse(allowed_9)
            self.assertIn("FORGE 90s Calibration Offload Gate Triggered", reason_9)
            self.assertNotIn("--no-synthesize", reason_9)

        # Reset and verify that a 3rd script matching calibration naming (opt_renamed_3.py) still triggers at 3
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        with patch("time.time", return_value=base_now):
            self.assertTrue(check_batching("python3 /workspace/scratch/diag_1.py")[0])
            self.assertTrue(check_batching("python3 /workspace/scratch/diag_2.py")[0])
        with patch("time.time", return_value=base_now + 100.0):
            allowed_cal, reason_cal = check_batching("python3 /workspace/scratch/opt_renamed_3.py")
            self.assertFalse(allowed_cal)
            self.assertIn("FORGE 90s Calibration Offload Gate Triggered", reason_cal)
            allowed_edit, edit_reason = check_tool_use(
                "edit_file", {"Path": "/baseball/big-projections/hitter_components.py"}
            )
            self.assertFalse(allowed_edit)
            self.assertIn("FORGE 90s Tier 2/3 Gate Triggered", edit_reason)

    def test_hook_self_preservation_syntax_guard(self):
        allowed, reason = check_tool_use(
            "write_file",
            {
                "Path": "/workspace/.agents/hooks/batch_guard.py",
                "Content": "def broken():\n    return False (\n        'missing comma'\n    )\n",
            },
        )
        self.assertFalse(allowed)
        self.assertIn("Hook Self-Preservation Syntax Guard", reason)

    def test_sql_query_guard_blocks_select_star_missing_limit_and_invalid_columns(self):
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "baseball_test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE projections (player_id TEXT, player_name TEXT, woba REAL, hr INTEGER)")
            conn.commit()
            conn.close()

            # 1. SELECT * is blocked and returns valid columns
            allowed, reason = check_batching(f'sqlite3 {db_path} "SELECT * FROM projections LIMIT 10"')
            self.assertFalse(allowed)
            self.assertIn("'SELECT *' is prohibited", reason)
            self.assertIn("projections(player_id, player_name, woba, hr)", reason)

            # 2. SELECT without LIMIT is blocked and returns valid columns
            allowed, reason = check_batching(f'sqlite3 {db_path} "SELECT player_name, woba FROM projections WHERE hr > 20"')
            self.assertFalse(allowed)
            self.assertIn("without a LIMIT clause is prohibited", reason)
            self.assertIn("projections(player_id, player_name, woba, hr)", reason)

            # 3. Invalid column name is caught pre-execution via EXPLAIN QUERY PLAN and returns valid schema
            allowed, reason = check_batching(f'sqlite3 {db_path} "SELECT player_name, xwoba_guessed FROM projections LIMIT 10"')
            self.assertFalse(allowed)
            self.assertIn("SQLite Schema Validation Failed", reason)
            self.assertIn("no such column: xwoba_guessed", reason)
            self.assertIn("projections(player_id, player_name, woba, hr)", reason)

            # 4. Inline python sqlite3.connect(...).execute(...) with invalid column or missing LIMIT is blocked
            py_bad = f"python3 -c \"import sqlite3; c=sqlite3.connect('{db_path}'); print(c.execute('SELECT bad_col FROM projections LIMIT 5').fetchall())\""
            allowed, reason = check_batching(py_bad)
            self.assertFalse(allowed)
            self.assertIn("no such column: bad_col", reason)
            self.assertIn("projections(player_id, player_name, woba, hr)", reason)

            # 5. Scalar aggregate (SELECT COUNT(*)) without LIMIT is allowed
            allowed, _ = check_batching(f'sqlite3 {db_path} "SELECT COUNT(*) FROM projections"')
            self.assertTrue(allowed)

            # 6. Valid explicit-column query with LIMIT is allowed
            allowed, _ = check_batching(f'sqlite3 {db_path} "SELECT player_name, woba, hr FROM projections ORDER BY woba DESC LIMIT 15"')
            self.assertTrue(allowed)

    def test_agy_ancestor_pid_resolves_persistent_worker(self):
        from batch_guard import _get_agy_ancestor_pid
        pid = _get_agy_ancestor_pid()
        self.assertGreater(pid, 1)

    def test_per_turn_in_flight_reset_clears_90s_calibration_and_offload_lock(self):
        import json
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            in_flight_path = os.path.join(tmpdir, "in_flight_turn.json")
            base_now = 1_800_300_000.0

            # Turn 1: runs 2 calibration scripts and dispatches a FORGE job
            with open(in_flight_path, "w") as f:
                json.dump(
                    {
                        "channel_id": 1544955535722545253,
                        "conv_id": "conv-1111",
                        "status_msg_id": 1001,
                        "attempts": 1,
                        "prompt": "First turn prompt",
                        "ts": base_now,
                    },
                    f,
                )

            with patch("time.time", return_value=base_now):
                self.assertTrue(
                    check_tool_use(
                        "run_command",
                        {"CommandLine": "python3 /workspace/scratch/diag_a.py"},
                        in_flight_file=in_flight_path,
                    )[0]
                )
                self.assertTrue(
                    check_tool_use(
                        "run_command",
                        {"CommandLine": "python3 /workspace/scratch/diag_b.py"},
                        in_flight_file=in_flight_path,
                    )[0]
                )
                self.assertTrue(
                    check_tool_use(
                        "run_command",
                        {"CommandLine": 'python3 /workspace/tools/forge.py job start --name "Job 1" --command "echo ok"'},
                        in_flight_file=in_flight_path,
                    )[0]
                )

            # Still in Turn 1: Post-Offload Turn Lock blocks further commands
            with patch("time.time", return_value=base_now + 10.0):
                allowed_same, reason_same = check_tool_use(
                    "run_command",
                    {"CommandLine": "python3 /workspace/scratch/diag_c.py"},
                    in_flight_file=in_flight_path,
                )
                self.assertFalse(allowed_same)
                self.assertIn("FORGE Post-Offload Turn Lock Triggered", reason_same)

            # Turn 2 arrives 120s later (<15m expiry, >90s from Turn 1 start):
            # New status_msg_id / prompt in in_flight_turn.json resets turn_start_time, scratch counts, and offload lock!
            with open(in_flight_path, "w") as f:
                json.dump(
                    {
                        "channel_id": 1544955535722545253,
                        "conv_id": "conv-1111",
                        "status_msg_id": 1002,
                        "attempts": 1,
                        "prompt": "Second turn prompt",
                        "ts": base_now + 120.0,
                    },
                    f,
                )

            with patch("time.time", return_value=base_now + 120.0):
                allowed_t2, reason_t2 = check_tool_use(
                    "run_command",
                    {"CommandLine": "python3 /workspace/scratch/diag_c.py"},
                    in_flight_file=in_flight_path,
                )
                self.assertTrue(allowed_t2, f"Turn 2 should start with a clean slate, got: {reason_t2}")
                st = load_state()
                self.assertEqual(st["total_scratch_runs"], 1)
                self.assertEqual(st["turn_run_commands"], 1)
                self.assertFalse(st["offload_dispatched"])
                self.assertEqual(st["turn_start_time"], base_now + 120.0)


if __name__ == "__main__":
    unittest.main()


def test_no_synthesize_probe_does_not_trigger_post_offload_lock(tmp_path):
    state_file = str(tmp_path / "bg_state.json")
    ok1, _ = check_tool_use(
        "run_command",
        {"CommandLine": 'python3 /workspace/tools/forge.py job start --silent-on-success --no-synthesize --name "Probe" --command "python3 /tmp/p.py"'},
        state_file=state_file,
    )
    assert ok1 is True
    ok2, reason2 = check_tool_use(
        "edit_file",
        {"Path": "/workspace/scratch/fix_probe.py", "Target": "a", "Replacement": "b"},
        state_file=state_file,
    )
    assert ok2 is True, f"Expected --no-synthesize probe not to lock turn, got: {reason2}"


def test_invalid_forge_plan_does_not_lock_turn_and_outbox_allowed_after_enqueue(tmp_path):
    import json
    from tools.forge import validate_forge_plan

    state_file = str(tmp_path / "bg_state_invalid.json")
    bad_plan = tmp_path / "bad_plan.json"
    bad_plan.write_text(json.dumps({"title": "Missing Chunks"}), encoding="utf-8")

    # 1. Enqueue with invalid plan file on disk does not set offload_dispatched
    ok1, _ = check_tool_use(
        "run_command",
        {"CommandLine": f"python3 /workspace/tools/forge.py enqueue {bad_plan}"},
        state_file=state_file,
    )
    assert ok1 is True
    st_bad = load_state(state_file)
    assert st_bad.get("offload_dispatched") is False
    ok_fix, reason_fix = check_tool_use(
        "write_file",
        {"Path": str(bad_plan), "Content": "{}"},
        state_file=state_file,
    )
    assert ok_fix is True, f"Expected write_file after invalid plan not to be locked, got: {reason_fix}"

    # 1b. Enqueue with missing plan file on disk does not set offload_dispatched
    missing_plan = tmp_path / "missing_plan.json"
    ok_miss, _ = check_tool_use(
        "run_command",
        {"CommandLine": f"python3 /workspace/tools/forge.py enqueue {missing_plan}"},
        state_file=state_file,
    )
    assert ok_miss is True
    st_miss = load_state(state_file)
    assert st_miss.get("offload_dispatched") is False
    ok_edit_miss, reason_miss = check_tool_use(
        "edit_file",
        {"Path": "/workspace/tools/alpha.py"},
        state_file=state_file,
    )
    assert ok_edit_miss is True, f"Expected edit_file after missing plan not to be locked, got: {reason_miss}"

    # 1c. Inline shell generation with cat << EOF ... && forge.py enqueue does not set offload_dispatched
    inline_cmd = (
        f"cat << 'EOF' > {bad_plan}\n"
        f'{{"title": "Inline Plan"}}\n'
        f"EOF\n"
        f"python3 /workspace/tools/forge.py enqueue {bad_plan}"
    )
    ok_inline, _ = check_tool_use(
        "run_command",
        {"CommandLine": inline_cmd},
        state_file=state_file,
    )
    assert ok_inline is True
    st_inline = load_state(state_file)
    assert st_inline.get("offload_dispatched") is False
    ok_edit_inline, reason_inline = check_tool_use(
        "edit_file",
        {"Path": "/workspace/tools/alpha.py"},
        state_file=state_file,
    )
    assert ok_edit_inline is True, f"Expected edit_file after inline shell gen not to be locked, got: {reason_inline}"

    # 2. Plan with shared_context instead of context_fact_sheet passes validate_forge_plan
    valid_plan = {
        "title": "Shared Context Fallback Plan",
        "origin_channel_id": 1548196929308065893,
        "repo": "/workspace",
        "shared_context": ["Invariant 1"],
        "chunks": [
            {
                "title": "Chunk 1",
                "target_files": ["tools/forge.py"],
                "instructions": "Update forge.py validation logic and run unit tests.",
            }
        ],
    }
    is_valid, val_msg = validate_forge_plan(valid_plan)
    assert is_valid is True, f"Expected shared_context fallback to validate, got: {val_msg}"

    # 3. Once a valid pre-existing enqueue sets offload_dispatched, turn is locked but outbox.py is still allowed
    good_plan = tmp_path / "good_plan.json"
    good_plan.write_text(json.dumps(valid_plan), encoding="utf-8")
    ok_good, _ = check_tool_use(
        "run_command",
        {"CommandLine": f"python3 /workspace/tools/forge.py enqueue {good_plan}"},
        state_file=state_file,
    )
    assert ok_good is True
    st_good = load_state(state_file)
    assert st_good.get("offload_dispatched") is True

    # Mutating tool calls are now locked by Post-Offload Turn Lock
    ok_locked, lock_reason = check_tool_use(
        "edit_file",
        {"Path": "/workspace/tools/beta.py"},
        state_file=state_file,
    )
    assert ok_locked is False
    assert "FORGE Post-Offload Turn Lock Triggered" in lock_reason

    # Outbox.py is exempt from the Post-Offload Turn Lock
    ok_outbox, reason_outbox = check_tool_use(
        "run_command",
        {"CommandLine": 'python3 /workspace/tools/outbox.py --channel zero-ops --message "test" --force && python3 /workspace/tools/outbox.py --flush'},
        state_file=state_file,
    )
    assert ok_outbox is True, f"Expected outbox.py to be exempt from Post-Offload Turn Lock, got: {reason_outbox}"


def test_missing_docker_cli_guard_blocks_docker_exec_when_binary_absent(tmp_path):
    from unittest.mock import patch

    state_file = str(tmp_path / "bg_state_docker.json")
    direct_cmd = "docker exec big_board_pro Rscript calculate_dollar_values.R"
    forge_cmd = (
        'python3 /workspace/tools/forge.py job start --name "BIG Migration" '
        '--command "predict_big.py --migrate-db && docker exec big_board_pro Rscript calculate_dollar_values.R"'
    )

    # 1. When docker CLI is absent (shutil.which('docker') is None):
    with patch("shutil.which", return_value=None):
        # Direct docker exec is blocked
        ok1, reason1 = check_tool_use(
            "run_command",
            {"CommandLine": direct_cmd},
            state_file=state_file,
        )
        assert ok1 is False
        assert "Container Environment Guard Triggered" in reason1
        st1 = load_state(state_file)
        assert st1.get("offload_dispatched") is False

        # forge.py job start containing docker exec is blocked without locking turn
        ok2, reason2 = check_tool_use(
            "run_command",
            {"CommandLine": forge_cmd},
            state_file=state_file,
        )
        assert ok2 is False
        assert "Container Environment Guard Triggered" in reason2
        st2 = load_state(state_file)
        assert st2.get("offload_dispatched") is False

        # Subsequent tool calls are not locked by Post-Offload Turn Lock
        ok_next, reason_next = check_tool_use(
            "edit_file",
            {"Path": "/workspace/scratch/fix.py", "Target": "a", "Replacement": "b"},
            state_file=state_file,
        )
        assert ok_next is True, f"Expected edit_file not to be locked after denied forge command, got: {reason_next}"

    # 2. When docker CLI is present (shutil.which('docker') returns '/usr/bin/docker'):
    state_file_allowed = str(tmp_path / "bg_state_docker_allowed.json")
    with patch("shutil.which", return_value="/usr/bin/docker"):
        ok_direct, reason_direct = check_tool_use(
            "run_command",
            {"CommandLine": direct_cmd},
            state_file=state_file_allowed,
        )
        assert ok_direct is True, f"Expected direct docker command to be allowed when docker CLI exists, got: {reason_direct}"

        state_file_allowed_forge = str(tmp_path / "bg_state_docker_allowed_forge.json")
        ok_forge, reason_forge = check_tool_use(
            "run_command",
            {"CommandLine": forge_cmd},
            state_file=state_file_allowed_forge,
        )
        assert ok_forge is True, f"Expected forge command to be allowed when docker CLI exists, got: {reason_forge}"


def test_throttle_heavy_compiler_command():
    from batch_guard import throttle_heavy_compiler_command

    rewritten = throttle_heavy_compiler_command("cd /workspace/gotchi/firmware && python3 -m platformio run")
    assert "-j 2" in rewritten
    assert "nice -n 10" in rewritten

    clamped = throttle_heavy_compiler_command("pio run -e seeed_xiao_esp32s3 -j 8")
    assert "-j 2" in clamped
    assert "-j 8" not in clamped


def test_is_exempt_from_investigation_gate():
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        # FORGE worker worktree and env var exemptions
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "ls"}, cwd="/tmp/forge-wt-chunk1") is True
        with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": "1"}):
            assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "ls"}) is True

        # Mutating file operations are exempt
        assert is_exempt_from_investigation_gate("edit_file", {"Path": "/foo.py"}) is True
        assert is_exempt_from_investigation_gate("replace_file_content", {"Path": "/foo.py"}) is True
        assert is_exempt_from_investigation_gate("write_file", {"Path": "/foo.py"}) is True
        assert is_exempt_from_investigation_gate("write_to_file", {"Path": "/foo.py"}) is True

        # Detached runner, FORGE offload, lifecycle, outbox, and testing commands are exempt
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/forge.py job start --name x"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/forge.py enqueue plan.json"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/forge.py status"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/forge.py stage-decision --plan plan.json"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "forge.py status"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "forge status"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/detached_runner.py start"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/modal_harness.py --mode run"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/outbox.py --channel zero-ops"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/gif_tool.py"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "git commit -m 'feat'"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "git status"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "git branch --show-current"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "git branch -a"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "git rev-parse HEAD"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "git rev-parse --abbrev-ref HEAD"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": 'python3 -c "import ast; tree = ast.parse(open(\'tools/forge.py\').read())"'}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": 'python3 -c "import ast; [n.name for n in ast.walk(ast.parse(open(\'tools/forge.py\').read())) if isinstance(n, ast.FunctionDef)]"'}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "gh pr create --title t"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "pytest tests/test_foo.py"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "ruff check ."}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/channel_history.py --limit 20"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/transcript_index.py search 'foo'"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/mealie_tool.py list"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/deliver_image.py --file /tmp/a.png"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/banana.py"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/send_mail.py"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/ha_shopping_list.py"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/whole_foods_afx.py"}) is True
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "python3 /workspace/tools/grocery_manager.py"}) is True

        # Non-exempt diagnostic commands and read tools
        assert is_exempt_from_investigation_gate("run_command", {"CommandLine": "cat /workspace/config.json"}) is False
        assert is_exempt_from_investigation_gate("view_file", {"Path": "/workspace/tools/foo.py"}) is False
        assert is_exempt_from_investigation_gate("read_file", {"Path": "/workspace/tools/foo.py"}) is False
        assert is_exempt_from_investigation_gate("grep_search", {"SearchPath": "/workspace/tools"}) is False
        assert is_exempt_from_investigation_gate("find_by_name", {"SearchDirectory": "/workspace/tools"}) is False

        # Direct check_interactive_investigation_gate returns True when in_flight_file is missing
        ok_gate, _ = check_interactive_investigation_gate("read_file", {"Path": "/foo.py"}, state={}, in_flight_file="/nonexistent")
        assert ok_gate is True


def test_interactive_investigation_gate_steps_1_to_3_allowed_and_step_4_blocked_after_30s(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        in_flight = tmp_path / "in_flight.json"
        in_flight.write_text(json.dumps({"channel_id": 1542081375287640084, "prompt": "investigate memory spike"}))
        state_file = str(tmp_path / "bg_state.json")
        t0 = 1_800_000_000.0

        # Steps 1 to 6 are allowed
        ok1, _ = check_tool_use("read_file", {"Path": "/workspace/tools/a.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 5.0)
        assert ok1 is True
        ok2, _ = check_tool_use("grep_search", {"SearchPath": "/workspace/tools"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 10.0)
        assert ok2 is True
        ok3, _ = check_tool_use("run_command", {"CommandLine": "cat /workspace/data/log1.txt"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 20.0)
        assert ok3 is True
        ok4, _ = check_tool_use("read_file", {"Path": "/workspace/tools/b.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 30.0)
        assert ok4 is True
        ok5, _ = check_tool_use("grep_search", {"SearchPath": "/workspace/tools"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 40.0)
        assert ok5 is True
        ok6, _ = check_tool_use("run_command", {"CommandLine": "cat /workspace/data/log1b.txt"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 50.0)
        assert ok6 is True

        st = load_state(state_file)
        assert st["interactive_diag_steps"] == 6

        # Step 7 after 75 seconds is blocked with detached-investigation handoff instructions
        ok7, reason7 = check_tool_use("run_command", {"CommandLine": "cat /workspace/data/log2.txt"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 80.0)
        assert ok7 is False
        assert "Interactive Investigation Gate Triggered" in reason7
        assert "(ceiling: 6 steps after 75.0s, max 8 steps)" in reason7
        assert "Code/Feature/Bugfix Builds" in reason7
        assert "MUST immediately write a ForgePlan JSON and call `python3 /workspace/tools/forge.py enqueue <plan.json>`" in reason7
        assert "NEVER call `forge.py job start` for code/asset changes" in reason7
        assert "Open-Ended Read-Only System Diagnostics" in reason7
        assert "forge.py job start" in reason7
        assert "partial findings" in reason7
        assert "contradiction check" in reason7


def test_interactive_investigation_gate_handoff_and_lifecycle_exemptions(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        in_flight = tmp_path / "in_flight.json"
        in_flight.write_text(json.dumps({"channel_id": 1542081375287640084, "prompt": "investigate slow query"}))
        state_file = str(tmp_path / "bg_state.json")
        t0 = 1_800_000_000.0

        # Run 6 diagnostic steps
        for i in range(6):
            check_tool_use("read_file", {"Path": f"/workspace/tools/{i}.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 5.0 * (i + 1))

        # 3a. git commit is exempt even after 75s
        ok_git, r_git = check_tool_use("run_command", {"CommandLine": 'git commit -m "chore: snapshot"'}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 76.0)
        assert ok_git is True, f"git commit should be exempt from investigation gate, got: {r_git}"

        # 3b. forge.py job start is exempt and executes cleanly
        ok_forge, r_forge = check_tool_use(
            "run_command",
            {"CommandLine": 'python3 /workspace/tools/forge.py job start --name "Deep probe" --command "python3 /workspace/scratch/probe.py"'},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 77.0,
        )
        assert ok_forge is True, f"forge.py job start should be exempt from investigation gate, got: {r_forge}"

        # 3c. outbox.py status report is exempt from post-offload lock
        ok_outbox, r_outbox = check_tool_use(
            "run_command",
            {"CommandLine": 'python3 /workspace/tools/outbox.py --channel zero-ops --message "status"'},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 78.0,
        )
        assert ok_outbox is True, f"outbox.py should be exempt from post-offload lock, got: {r_outbox}"


def test_interactive_investigation_gate_forge_plan_staging_exemptions(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        in_flight = tmp_path / "in_flight.json"
        in_flight.write_text(json.dumps({"channel_id": 1542081375287640084, "prompt": "build feature"}))
        state_file = str(tmp_path / "bg_state.json")
        t0 = 1_800_000_000.0

        # Run 6 initial diagnostic steps
        for i in range(6):
            check_tool_use("read_file", {"Path": f"/workspace/tools/{i}.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 5.0 * (i + 1))

        # After 75s, forge.py status check is exempt
        ok_status, r_status = check_tool_use(
            "run_command",
            {"CommandLine": "python3 /workspace/tools/forge.py status"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 76.0,
        )
        assert ok_status is True, f"forge.py status should be exempt from investigation gate, got: {r_status}"

        # Git branch and rev-parse lookups for plan staging are exempt
        ok_branch, r_branch = check_tool_use(
            "run_command",
            {"CommandLine": "git branch --show-current"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 77.0,
        )
        assert ok_branch is True, f"git branch should be exempt from investigation gate, got: {r_branch}"

        ok_rev, r_rev = check_tool_use(
            "run_command",
            {"CommandLine": "git rev-parse --abbrev-ref HEAD"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 78.0,
        )
        assert ok_rev is True, f"git rev-parse should be exempt from investigation gate, got: {r_rev}"

        # AST symbol discovery lookup is exempt
        ok_ast, r_ast = check_tool_use(
            "run_command",
            {"CommandLine": 'python3 -c "import ast; tree = ast.parse(open(\'tools/forge.py\').read())"'},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 79.0,
        )
        assert ok_ast is True, f"AST symbol discovery should be exempt from investigation gate, got: {r_ast}"

        # Non-exempt diagnostic command is blocked after 75s ceiling
        ok_blocked, r_blocked = check_tool_use(
            "run_command",
            {"CommandLine": "cat /workspace/data/log2.txt"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 80.0,
        )
        assert ok_blocked is False
        assert "Interactive Investigation Gate Triggered" in r_blocked


def test_interactive_investigation_gate_hard_limit_after_5_steps_even_under_30s(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        state_file = str(tmp_path / "bg_state.json")
        in_flight = tmp_path / "in_flight.json"
        in_flight.write_text(json.dumps({"channel_id": 1542081375287640084, "prompt": "rapid investigation"}))
        t0 = 1_800_000_000.0

        for i in range(8):
            if i == 5:
                # Reset consecutive_file_views with an exempt write so we isolate the interactive investigation hard limit
                check_tool_use("write_file", {"Path": "/workspace/scratch/note.txt", "Content": "ok"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + i * 2.0)
            ok, r = check_tool_use("read_file", {"Path": f"/workspace/tools/{i}.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + i * 2.0 + 0.5)
            assert ok is True, f"Step {i+1} should be allowed, got: {r}"

        # Step 9 under 75s triggers the hard limit (8 steps)
        ok9, reason9 = check_tool_use("read_file", {"Path": "/workspace/tools/8.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 20.0)
        assert ok9 is False
        assert "Interactive Investigation Gate Triggered" in reason9


def test_interactive_investigation_gate_exempt_in_forge_channel(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        state_file = str(tmp_path / "bg_state.json")
        in_flight = tmp_path / "in_flight.json"
        in_flight.write_text(json.dumps({"channel_id": int(FORGE_CHANNEL_ID_STR), "prompt": "build job"}))
        t0 = 1_800_000_000.0

        # 4 diagnostic steps after 40s in #forge channel are not gated
        for i in range(4):
            ok_f, r_f = check_tool_use("read_file", {"Path": f"/workspace/tools/{i}.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + 40.0 + i)
            assert ok_f is True, f"Step {i+1} in #forge should be allowed, got: {r_f}"


def test_interactive_investigation_gate_exempt_for_forge_worker(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        state_file = str(tmp_path / "bg_state.json")
        in_flight = tmp_path / "in_flight.json"
        in_flight.write_text(json.dumps({"channel_id": 1542081375287640084, "prompt": "interactive turn"}))
        t0 = 1_800_000_000.0

        # Allowed when cwd starts with /tmp/forge-wt-
        ok_wt, _ = check_tool_use("read_file", {"Path": "/workspace/tools/x.py"}, state_file=state_file, cwd="/tmp/forge-wt-chunk1", in_flight_file=str(in_flight), now=t0 + 40.0)
        assert ok_wt is True

    # Allowed when FORGE_WORKER_ACTIVE == "1"
    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": "1"}):
        state_file2 = str(tmp_path / "bg_state2.json")
        ok_env, _ = check_tool_use("read_file", {"Path": "/workspace/tools/y.py"}, state_file=state_file2, in_flight_file=str(in_flight), now=t0 + 40.0)
        assert ok_env is True


def test_interactive_diag_steps_preserved_across_short_idle_resets(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        state_file = str(tmp_path / "bg_state.json")
        in_flight = tmp_path / "in_flight.json"
        in_flight.write_text(json.dumps({"channel_id": 1542081375287640084, "prompt": "idle test"}))
        t_idle = 1_800_100_000.0

        for i in range(5):
            check_tool_use("read_file", {"Path": f"/workspace/tools/{i}.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t_idle + i * 2.0)

        # 50s idle gap (>45s reset) within same turn preserves interactive_diag_steps and turn_start_time
        check_tool_use("read_file", {"Path": "/workspace/tools/5.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t_idle + 65.0)
        st = load_state(state_file)
        assert st["interactive_diag_steps"] == 6

        # Step 7 at 80s elapsed (>75s) is blocked
        ok7, reason7 = check_tool_use("read_file", {"Path": "/workspace/tools/6.py"}, state_file=state_file, in_flight_file=str(in_flight), now=t_idle + 80.0)
        assert ok7 is False
        assert "Interactive Investigation Gate Triggered" in reason7


def test_concurrent_turns_hook_state_isolated_per_session(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        state_file = str(tmp_path / "bg_state.json")
        in_flight = tmp_path / "in_flight.json"

        t0 = 1_800_200_000.0
        pid_ch1 = 12001
        pid_ch2 = 12002
        ch1_id = 1544955535722545253
        ch2_id = 1544955535722545254

        # Two active turns in concurrent channels with distinct conv_id and pid
        in_flight_data = {
            str(ch1_id): {
                "channel_id": ch1_id,
                "pid": pid_ch1,
                "conv_id": "conv-channel-1",
                "status_msg_id": 1001,
                "attempts": 1,
                "prompt": "Investigate DB contention in ch1",
                "ts": t0,
            },
            str(ch2_id): {
                "channel_id": ch2_id,
                "pid": pid_ch2,
                "conv_id": "conv-channel-2",
                "status_msg_id": 2001,
                "attempts": 1,
                "prompt": "Investigate HA webhook failure in ch2",
                "ts": t0,
            },
        }
        in_flight.write_text(json.dumps(in_flight_data))

        # Interleaved diagnostic steps across the two concurrent sessions (6 steps each):
        for step in range(1, 7):
            if step == 5:
                # Reset consecutive_file_views with an exempt write so we isolate the interactive investigation gate
                check_tool_use("write_file", {"Path": "/workspace/scratch/ch1.txt", "Content": "1"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + step * 2.0 - 0.5, agy_pid=pid_ch1)
                check_tool_use("write_file", {"Path": "/workspace/scratch/ch2.txt", "Content": "2"}, state_file=state_file, in_flight_file=str(in_flight), now=t0 + step * 2.0 - 0.2, agy_pid=pid_ch2)
            ok1, r1 = check_tool_use(
                "read_file",
                {"Path": f"/workspace/tools/ch1_step{step}.py"},
                state_file=state_file,
                in_flight_file=str(in_flight),
                now=t0 + step * 2.0,
                agy_pid=pid_ch1,
            )
            assert ok1 is True, f"ch1 step {step} failed: {r1}"

            ok2, r2 = check_tool_use(
                "read_file",
                {"Path": f"/workspace/tools/ch2_step{step}.py"},
                state_file=state_file,
                in_flight_file=str(in_flight),
                now=t0 + step * 2.0 + 1.0,
                agy_pid=pid_ch2,
            )
            assert ok2 is True, f"ch2 step {step} failed: {r2}"

        # Step 7 for Channel 1 at t0 + 80.0 (elapsed > 75s): triggers gate!
        ok1_7, r1_7 = check_tool_use(
            "read_file",
            {"Path": "/workspace/tools/ch1_step7.py"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 80.0,
            agy_pid=pid_ch1,
        )
        assert ok1_7 is False
        assert "Interactive Investigation Gate Triggered" in r1_7

        # Channel 2 at t0 + 14.0 (elapsed < 75s) is NOT gated by Channel 1's gate!
        ok2_7, r2_7 = check_tool_use(
            "read_file",
            {"Path": "/workspace/tools/ch2_step7.py"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 14.0,
            agy_pid=pid_ch2,
        )
        assert ok2_7 is True, f"ch2 step 7 should be allowed (<75s), got: {r2_7}"

        # Channel 2 step 8 (allowed: hard limit is >8 steps)
        ok2_8, r2_8 = check_tool_use(
            "read_file",
            {"Path": "/workspace/tools/ch2_step8.py"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 15.0,
            agy_pid=pid_ch2,
        )
        assert ok2_8 is True, f"ch2 step 8 should be allowed (step 8 of 8), got: {r2_8}"

        # Channel 2 step 9 (exceeds hard limit 8 steps): triggers gate!
        ok2_9, r2_9 = check_tool_use(
            "read_file",
            {"Path": "/workspace/tools/ch2_step9.py"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 16.0,
            agy_pid=pid_ch2,
        )
        assert ok2_9 is False
        assert "Interactive Investigation Gate Triggered" in r2_9

        # Verify state_file contains sessions map with both sessions
        with open(state_file, "r") as f:
            raw_state = json.load(f)
        assert "sessions" in raw_state
        sess1_key = f"{pid_ch1}:conv-channel-1"
        sess2_key = f"{pid_ch2}:conv-channel-2"
        assert sess1_key in raw_state["sessions"]
        assert sess2_key in raw_state["sessions"]
        assert raw_state["sessions"][sess1_key]["interactive_diag_steps"] == 7
        assert raw_state["sessions"][sess2_key]["interactive_diag_steps"] == 9

        # Verify load_state with session_id loads the respective counters
        st1 = load_state(state_file, session_id=sess1_key)
        assert st1["interactive_diag_steps"] == 7
        st2 = load_state(state_file, session_id=sess2_key)
        assert st2["interactive_diag_steps"] == 9


def test_decision_card_sql_tamper_regex_avoids_proactive_decision_cards_false_positive():
    from unittest.mock import patch
    from batch_guard import check_file_deletion_gate

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        # Commands referencing proactive_decision_cards.py alongside 'update' or 'replace' must NOT be blocked
        ok1, r1 = check_file_deletion_gate(
            'python3 -c "import ast; print(\'update proactive_decision_cards.py\')"',
            cwd="/workspace",
        )
        assert ok1 is True, f"Should not false-positive on proactive_decision_cards.py + update: {r1}"

        ok2, r2 = check_file_deletion_gate(
            "grep -n 'replace' /workspace/tools/proactive_decision_cards.py",
            cwd="/workspace",
        )
        assert ok2 is True, f"Should not false-positive on proactive_decision_cards.py + replace: {r2}"

        # Actual SQL mutation statements targeting decision_cards or forge_decisions MUST still be blocked
        ok_sql1, r_sql1 = check_file_deletion_gate(
            "sqlite3 /workspace/data/forge.db \"UPDATE decision_cards SET status='approved'\"",
            cwd="/workspace",
        )
        assert ok_sql1 is False
        assert "Decision Card Tamper Blocked" in r_sql1

        ok_sql2, r_sql2 = check_file_deletion_gate(
            "sqlite3 /workspace/data/forge.db \"INSERT OR REPLACE INTO forge_decisions VALUES (1, 'approved')\"",
            cwd="/workspace",
        )
        assert ok_sql2 is False
        assert "Decision Card Tamper Blocked" in r_sql2

        ok_sql3, r_sql3 = check_file_deletion_gate(
            "sqlite3 /workspace/data/forge.db \"REPLACE INTO decision_cards (id, status) VALUES ('x', 'approved')\"",
            cwd="/workspace",
        )
        assert ok_sql3 is False
        assert "Decision Card Tamper Blocked" in r_sql3


def test_save_state_prunes_expired_sessions(tmp_path):
    from unittest.mock import patch
    from batch_guard import save_state, TURN_STATE_EXPIRY_SECONDS

    state_file = str(tmp_path / "bg_state.json")
    t0 = 1_800_000_000.0

    with patch("time.time", return_value=t0):
        # Save session A
        st_a = {"last_time": t0, "interactive_diag_steps": 2}
        save_state(st_a, state_file, session_id="sess_a")

    # Save session B after TURN_STATE_EXPIRY_SECONDS + 10s
    t1 = t0 + TURN_STATE_EXPIRY_SECONDS + 10.0
    with patch("time.time", return_value=t1):
        st_b = {"last_time": t1, "interactive_diag_steps": 1}
        save_state(st_b, state_file, session_id="sess_b")

    with open(state_file, "r") as f:
        data = json.load(f)

    assert "sess_b" in data["sessions"]
    # sess_a must have been pruned because it is older than 900s
    assert "sess_a" not in data["sessions"]
    # Backwards-compatible top-level keys match most recent save
    assert data["interactive_diag_steps"] == 1


def test_forge_enqueue_exempt_from_prior_job_start_post_offload_lock(tmp_path):
    from unittest.mock import patch

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": ""}):
        state_file = str(tmp_path / "bg_state_job_then_enq.json")
        in_flight = tmp_path / "in_flight.json"
        in_flight.write_text(json.dumps({"channel_id": 1542081375287640084, "prompt": "fix bug"}))
        t0 = 1_800_500_000.0

        # 1. Agent calls forge.py job start earlier in the turn -> activates Post-Offload Turn Lock for job start
        ok_job, r_job = check_tool_use(
            "run_command",
            {"CommandLine": 'python3 /workspace/tools/forge.py job start --name "Diag" --command "echo diag"'},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0,
        )
        assert ok_job is True, f"job start failed: {r_job}"
        st1 = load_state(state_file)
        assert st1["offload_dispatched"] is True
        assert "job start" in st1["offload_kind"]

        # 2. Normal shell commands or non-plan production edits remain blocked by Post-Offload Turn Lock
        ok_cmd, r_cmd = check_tool_use(
            "run_command",
            {"CommandLine": "python3 /workspace/scratch/another_probe.py"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 2.0,
        )
        assert ok_cmd is False
        assert "FORGE Post-Offload Turn Lock Triggered" in r_cmd

        ok_prod_edit, r_prod_edit = check_tool_use(
            "edit_file",
            {"Path": "/workspace/tools/bridge.py"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 3.0,
        )
        assert ok_prod_edit is False
        assert "FORGE Post-Offload Turn Lock Triggered" in r_prod_edit

        # 3. Writing a ForgePlan JSON file and calling forge.py enqueue is ALLOWED despite the prior job start lock
        plan_file = tmp_path / "forge_plan.json"
        plan_payload = {
            "title": "Fix After Job Start",
            "channel_id": 1542081375287640084,
            "repo": "/workspace",
            "chunks": [
                {
                    "title": "Apply Fix",
                    "target_files": ["tools/forge.py", "tools/test_forge.py", ".agents/hooks/batch_guard.py", "README.md"],
                    "description": "Implement fix for schema normalization and add regression tests.",
                }
            ],
        }
        ok_write_plan, r_write_plan = check_tool_use(
            "write_file",
            {"Path": str(plan_file), "Content": json.dumps(plan_payload)},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 4.0,
        )
        assert ok_write_plan is True, f"Writing ForgePlan JSON should be allowed after job start, got: {r_write_plan}"
        plan_file.write_text(json.dumps(plan_payload), encoding="utf-8")

        ok_enq, r_enq = check_tool_use(
            "run_command",
            {"CommandLine": f"python3 /workspace/tools/forge.py enqueue {plan_file}"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 5.0,
        )
        assert ok_enq is True, f"forge.py enqueue should be allowed after prior job start, got: {r_enq}"

        # 4. After forge.py enqueue succeeds, offload_kind transitions to enqueue and a second enqueue is blocked
        st2 = load_state(state_file)
        assert st2["offload_dispatched"] is True
        assert "enqueue" in st2["offload_kind"]
        ok_direct, r_direct = check_forge_post_offload_lock("run_command", {"CommandLine": "ls"}, state=st2)
        assert ok_direct is False
        assert "FORGE Post-Offload Turn Lock Triggered" in r_direct

        ok_enq2, r_enq2 = check_tool_use(
            "run_command",
            {"CommandLine": f"python3 /workspace/tools/forge.py enqueue {plan_file}"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            now=t0 + 6.0,
        )
        assert ok_enq2 is False
        assert "FORGE Post-Offload Turn Lock Triggered" in r_enq2


def test_crab_cavern_airgap_guard_directs_propose_to_zero_chat(tmp_path):
    from unittest.mock import patch
    import importlib.util

    bp_path = os.path.join(_REPO_ROOT, "tools", "bridge_prompts.py")
    spec = importlib.util.spec_from_file_location("bridge_prompts", bp_path)
    bp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bp)

    assert "1542081375287640084" in bp.get_crab_cavern_escalation_directive()
    assert "forge.py propose --channel 1542081375287640084" in bp.CRAB_CAVERN_HOMELAB_ESCALATION_DIRECTIVE
    assert "forge.py propose --channel 1542081375287640084" in bp.CRAB_CAVERN_AIRGAP_RULE_TEXT
    assert "forge.py propose --channel 1542081375287640084" in bp.load_crab_cavern_system_prompt()

    with patch.dict(os.environ, {"FORGE_WORKER_ACTIVE": "", "DISCORD_CHANNEL_ID": "1534452820995080192"}):
        state_file = str(tmp_path / "bg_state_cc_airgap.json")
        in_flight = tmp_path / "in_flight_cc.json"
        in_flight.write_text(json.dumps({"channel_id": 1534452820995080192, "prompt": "restart plex on host 1"}))

        # 1. SSH / NAS command in Crab Cavern is blocked and directs staging a #zero-chat Decision Card via forge.py propose --post
        ok_ssh, reason_ssh = check_tool_use(
            "run_command",
            {"CommandLine": "ssh testuser@NasHost01 'docker restart plex'"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            cwd="/workspace",
        )
        assert ok_ssh is False
        assert "Crab Cavern Air-Gap Sandbox Guard Triggered" in reason_ssh
        assert "forge.py propose --channel 1542081375287640084" in reason_ssh
        assert "--post" in reason_ssh

        # 2. Reading /secrets/ or private memory in Crab Cavern is blocked with the same #zero-chat Decision Card directive
        ok_sec, reason_sec = check_tool_use(
            "read_file",
            {"Path": "/secrets/env.json"},
            state_file=state_file,
            in_flight_file=str(in_flight),
            cwd="/workspace",
        )
        assert ok_sec is False
        assert "forge.py propose --channel 1542081375287640084" in reason_sec
        assert "--post" in reason_sec

        # 3. Staging the Decision Card itself via forge.py propose --channel 1542081375287640084 ... --post is allowed
        ok_prop, reason_prop = check_tool_use(
            "run_command",
            {
                "CommandLine": (
                    'python3 /workspace/tools/forge.py propose --channel 1542081375287640084 '
                    '--category "homelab_action" --title "Restart Plex" --summary "Restart Plex on Host 1" '
                    '--handler "shell_cmd" --payload \'{"command": "ssh testuser@NasHost01 docker restart plex"}\' --post'
                )
            },
            state_file=state_file,
            in_flight_file=str(in_flight),
            cwd="/workspace",
        )
        assert ok_prop is True, f"Expected forge.py propose to be allowed in Crab Cavern, got: {reason_prop}"


def test_malformed_forge_job_start_without_command_does_not_lock_turn(tmp_path):
    state_file = str(tmp_path / "bg_state_malformed_job.json")
    malformed_invocations = [
        "python3 /workspace/tools/forge.py job start",
        "python3 /workspace/tools/forge.py job start --help",
        "python3 /workspace/tools/forge.py job start -h",
        'python3 /workspace/tools/forge.py job start --name "Missing Command"',
        "python3 /workspace/tools/forge.py job start --channel zero-ops --cwd /workspace",
        'python3 /workspace/tools/forge.py job start "python3 /workspace/scratch/probe.py"',
        'python3 /workspace/tools/forge.py job start "python3 -c \'print(1)\'"',
        'python3 /workspace/tools/forge.py job start --name "probe -c 1"',
        'python3 /workspace/tools/forge.py job start --cmd "echo hi"',
        "python3 /workspace/tools/forge.py job start --command",
        "python3 /workspace/tools/forge.py job start -c",
        'python3 /workspace/tools/forge.py job start --command ""',
        'python3 /workspace/tools/forge.py job start --command=""',
        'python3 /workspace/tools/forge.py job start -c=""',
        'python3 /workspace/tools/forge.py job start --command --name "Foo"',
        'python3 /workspace/tools/forge.py job start --name "Bad" && python3 -c "print(1)"',
        'bash -c "python3 /workspace/tools/forge.py job start --name Foo"',
        'python3 /workspace/tools/detached_runner.py start --name "NoCmd"',
        'python3 /workspace/tools/forge.py job start --command "echo hi" --help',
    ]

    for bad_cmd in malformed_invocations:
        assert has_forge_job_start_command(bad_cmd) is False, f"Expected False for: {bad_cmd}"
        ok, reason = check_tool_use(
            "run_command",
            {"CommandLine": bad_cmd},
            state_file=state_file,
        )
        assert ok is True, f"Expected malformed CLI call to pass through to argparse without locking: {bad_cmd} ({reason})"
        st = load_state(state_file)
        assert st.get("offload_dispatched") is False, f"Expected offload_dispatched=False after: {bad_cmd}"

        # Subsequent edits or retries must NOT be locked
        ok_edit, edit_reason = check_tool_use(
            "edit_file",
            {"Path": "/workspace/scratch/retry_probe.py", "Target": "a", "Replacement": "b"},
            state_file=state_file,
        )
        assert ok_edit is True, f"Expected subsequent edit_file not to be locked after '{bad_cmd}', got: {edit_reason}"

    # Verify all valid --command / -c forms ARE detected and DO activate Post-Offload Turn Lock
    valid_invocations = [
        'python3 /workspace/tools/forge.py job start --name "Good1" --command "python3 /workspace/scratch/p.py"',
        'python3 /workspace/tools/forge.py job start --name "Good2" -c "python3 /workspace/scratch/p.py"',
        'python3 /workspace/tools/forge.py job start --command="python3 /workspace/scratch/p.py"',
        'python3 /workspace/tools/forge.py job start -c="python3 /workspace/scratch/p.py"',
        "python3 /workspace/tools/forge.py job start -n 'Good3' -c 'echo ok && df -h'",
        'python3 /workspace/tools/detached_runner.py start -c "echo ok"',
        'python3 /workspace/tools/forge.py job start \\\n  --name "Multi" \\\n  --command "echo ok"',
    ]
    for idx, good_cmd in enumerate(valid_invocations):
        assert has_forge_job_start_command(good_cmd) is True, f"Expected True for: {good_cmd}"
        good_state_file = str(tmp_path / f"bg_state_good_job_{idx}.json")
        ok_good, reason_good = check_tool_use(
            "run_command",
            {"CommandLine": good_cmd},
            state_file=good_state_file,
        )
        assert ok_good is True, f"Expected valid job start to succeed: {good_cmd} ({reason_good})"
        st_good = load_state(good_state_file)
        assert st_good.get("offload_dispatched") is True
        assert "job start" in st_good.get("offload_kind", "")

        # Subsequent non-exempt command in same turn is locked
        ok_after, reason_after = check_tool_use(
            "run_command",
            {"CommandLine": "python3 /workspace/scratch/extra.py"},
            state_file=good_state_file,
        )
        assert ok_after is False
        assert "FORGE Post-Offload Turn Lock Triggered" in reason_after


def test_denied_forge_job_start_does_not_lock_turn(tmp_path):
    import sqlite3

    state_file = str(tmp_path / "bg_state_denied_job.json")
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE items (id INTEGER, name TEXT)")
    conn.commit()
    conn.close()

    # Job start containing prohibited `SELECT *` is denied by SQLite Query Guard and must NOT lock turn
    denied_cmd = f'python3 /workspace/tools/forge.py job start -c "sqlite3 {db_path} \'SELECT * FROM items LIMIT 5\'"'
    ok_den, reason_den = check_tool_use(
        "run_command",
        {"CommandLine": denied_cmd},
        state_file=state_file,
    )
    assert ok_den is False
    assert "SQLite Query Guard Triggered" in reason_den
    assert load_state(state_file).get("offload_dispatched") is False

    # Agent can immediately retry with fixed query in the same turn
    retry_cmd = f'python3 /workspace/tools/forge.py job start -c "sqlite3 {db_path} \'SELECT id FROM items LIMIT 5\'"'
    ok_retry, reason_retry = check_tool_use(
        "run_command",
        {"CommandLine": retry_cmd},
        state_file=state_file,
    )
    assert ok_retry is True, f"Expected retry after denied job start to succeed, got: {reason_retry}"
    assert load_state(state_file).get("offload_dispatched") is True


def test_tools_and_hooks_batch_guard_copies_are_identical(tmp_path):
    import importlib.util
    from pathlib import Path

    tools_copy = Path(_REPO_ROOT) / "tools" / "batch_guard.py"
    hooks_copy = Path(_REPO_ROOT) / ".agents" / "hooks" / "batch_guard.py"
    assert tools_copy.is_file(), f"Missing {tools_copy}"
    assert hooks_copy.is_file(), f"Missing {hooks_copy}"
    assert tools_copy.read_text(encoding="utf-8") == hooks_copy.read_text(encoding="utf-8"), (
        "tools/batch_guard.py and .agents/hooks/batch_guard.py must remain 100% identical"
    )

    spec = importlib.util.spec_from_file_location("hooks_batch_guard", str(hooks_copy))
    hooks_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks_mod)

    assert hooks_mod._resolve_repo_root() == os.path.abspath(_REPO_ROOT)
    state_file = str(tmp_path / "hooks_mod_state.json")
    ok_bad, _ = hooks_mod.check_tool_use(
        "run_command",
        {"CommandLine": 'python3 /workspace/tools/forge.py job start --name "MissingCmd"'},
        state_file=state_file,
        in_flight_file="/nonexistent_in_flight_turn.json",
    )
    assert ok_bad is True
    assert hooks_mod.load_state(state_file).get("offload_dispatched") is False

    ok_good, _ = hooks_mod.check_tool_use(
        "run_command",
        {"CommandLine": 'python3 /workspace/tools/forge.py job start --name "ValidCmd" -c "echo ok"'},
        state_file=state_file,
        in_flight_file="/nonexistent_in_flight_turn.json",
    )
    assert ok_good is True
    assert hooks_mod.load_state(state_file).get("offload_dispatched") is True


def test_modal_godot_verify_exempt_and_direct_local_godot_binary_blocked(tmp_path):
    state_file = str(tmp_path / "bg_state_godot.json")

    # 1. modal_godot_verify.py and modal_verify.py are exempt from investigation gate
    assert is_exempt_from_investigation_gate(
        "run_command",
        {"CommandLine": "python3 /workspace/tools/modal_godot_verify.py test_upgrades"},
        cwd="/workspace",
    ) is True
    assert is_exempt_from_investigation_gate(
        "run_command",
        {"CommandLine": "python3 /workspace/tools/modal_verify.py"},
        cwd="/workspace",
    ) is True

    # 2. Direct local Godot_v4.7.2 binary execution is blocked unless routed via modal_godot_verify.py
    for blocked_cmd in (
        "/workspace/agora-roguelike/.godot-bin/Godot_v4.7.2-stable_linux.x86_64 --headless --path game -s res://tests/run_tests.gd",
        "./game/tests/run.sh /workspace/agora-roguelike/.godot-bin/Godot_v4.7.2-stable_linux.x86_64",
    ):
        ok_direct, reason_direct = check_tool_use(
            "run_command",
            {"CommandLine": blocked_cmd},
            state_file=state_file,
        )
        assert ok_direct is False, f"Expected direct Godot_v4.7.2 invocation to be blocked: {blocked_cmd}"
        assert "Modal Godot Offload Guard Triggered" in reason_direct
        assert "modal_godot_verify.py" in reason_direct

    # 3. Invoking via modal_godot_verify.py (even with optional leading Godot_v4.7.2 argument) is allowed
    for allowed_cmd in (
        "python3 /workspace/tools/modal_godot_verify.py",
        "python3 /workspace/tools/modal_godot_verify.py test_rescue_goldens test_sovereign_boss",
        "python3 /workspace/tools/modal_godot_verify.py /workspace/agora-roguelike/.godot-bin/Godot_v4.7.2-stable_linux.x86_64 test_upgrades",
    ):
        ok_modal, reason_modal = check_tool_use(
            "run_command",
            {"CommandLine": allowed_cmd},
            state_file=state_file,
        )
        assert ok_modal is True, f"Expected modal_godot_verify.py invocation to be allowed: {allowed_cmd} ({reason_modal})"








