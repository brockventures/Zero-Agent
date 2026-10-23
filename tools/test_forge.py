"""
test_forge.py — Comprehensive Unit Tests for FORGE v1.1 Engine & Zero-Vault Encryption.

Covers:
1. Activity-Renewed Lease & 4-Signal Heartbeat Tracker (SDK tasks, subagent transcripts, runaway loops).
2. ForgePlan Validation, Build `objective`, Per-Chunk `repo`, Turn-0 `target_symbols` AST Hydration,
   `shared_context` Exemplars, and Pre-Enqueue Target-File Overlap Deduplication.
3. Optional `verification_cmd` with Automatic Baseline Fallback + Prohibited File Boundary Guard.
4. Native `forge.py job` Detached Runner Lifecycle (`start_job`, `_run_job_worker`, `get_job_status`, `cancel_job`).
5. Decisions Framework (Approve / Edit / Dismiss, TTL Expiry, Privacy Filtering in /queue).
6. Zero-Vault AES-256-GCM Secret Archive Encryption & Round-Trip Decryption + Platform Rule Size Guard.
"""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import tools.forge as forge_mod
from tools.forge import (
    FORGE_CHANNEL_ID,
    OWNER_USER_ID,
    ZERO_CHAT_CHANNEL_ID,
    ZERO_OPS_CHANNEL_ID,
    HeartbeatTracker,
    _read_job_meta,
    _run_job_worker,
    _write_job_meta,
    build_default_verification_cmd,
    cancel_job,
    check_runaway_tool_loop,
    check_target_file_overlap,
    dismiss_decision,
    dispatch_build_completion_to_origin,
    enqueue_build,
    execute_decision,
    expire_stale_decisions,
    extract_symbol_snippet,
    format_chunk_prompt,
    format_origin_completion_envelope,
    format_queue_digest,
    get_job_status,
    init_forge_db,
    list_jobs,
    prune_completed_build_widgets,
    render_build_widget_embed,
    render_build_widget_text,
    stage_decision,
     validate_forge_plan,
    verify_chunk_worktree,
)
from tools.vault_sync import decrypt_secrets_archive, encrypt_secrets_archive


class TestForgeEngine(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_forge.db"
        init_forge_db(self.db_path)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_01_validate_and_enqueue_forge_plan_v1_1_features(self):
        # Missing context_fact_sheet should fail validation
        bad_plan = {
            "title": "Bad Plan",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "repo": "/workspace",
            "context_fact_sheet": [],
            "chunks": [],
        }
        valid, reason = validate_forge_plan(bad_plan)
        self.assertFalse(valid)
        self.assertIn("context_fact_sheet", reason)

        # Valid 2-chunk plan with v1.1 features:
        # - `objective` field
        # - `shared_context` exemplars
        # - `target_symbols` AST pre-hydration
        # - optional `verification_cmd` (omitted on Chunk 2)
        # - per-chunk `repo`
        good_plan = {
            "title": "Upgrade Telemetry & Queue Engine",
            "objective": "Eliminate turn-0 discovery reads by pre-hydrating AST symbol slices into chunk prompts.",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "origin_thread_id": 1599999999999999999,
            "repo": "/workspace",
            "tier": 3,
            "shared_context": [
                "Exemplar: Always open SQLite connections via get_db_connection(db_path)."
            ],
            "context_fact_sheet": [
                "Use WAL mode on SQLite connections.",
                "Never block interactive channel turns over 90 seconds.",
            ],
            "chunks": [
                {
                    "title": "Build SQLite Schema",
                    "repo": "/workspace",
                    "worker_model": "gemini-3.8-flash-high",
                    "target_files": ["tools/forge.py"],
                    "target_symbols": ["validate_forge_plan"],
                    "shared_context": ["Chunk Exemplar: Return (bool, str) from validators."],
                    "prohibited_files": ["tools/bridge.py"],
                    "instructions": "Create SQLite WAL schema and connection helpers with full unit tests.",
                    "verification_cmd": "pytest tools/test_forge.py",
                },
                {
                    "title": "Wire Bridge Slash Command",
                    "worker_model": "gemini-3.8-flash-medium",
                    "target_files": ["tools/bridge.py"],
                    "prohibited_files": [],
                    "instructions": "Add /queue slash command to bridge.py and verify output formatting.",
                    # Omit verification_cmd to test v1.1 optional fallback!
                },
            ],
        }
        valid, reason = validate_forge_plan(good_plan)
        self.assertTrue(valid, reason)

        res = enqueue_build(good_plan, db_path=self.db_path, post_widget=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["chunk_count"], 2)
        self.assertEqual(res["forge_channel_id"], FORGE_CHANNEL_ID)
        build_id = res["build_id"]

        # Test Amos's Queue-Level Target-File Overlap Guard
        overlapping_plan = dict(good_plan)
        overlapping_plan["title"] = "Conflicting Concurrent Build"
        overlaps = check_target_file_overlap(overlapping_plan, db_path=self.db_path)
        self.assertTrue(len(overlaps) >= 1)
        self.assertEqual(overlaps[0]["file"], "tools/forge.py")
        with self.assertRaises(ValueError) as ctx:
            enqueue_build(overlapping_plan, db_path=self.db_path, post_widget=False)
        self.assertIn("Target file overlap detected", str(ctx.exception))

        # Verify widget rendering and chunk prompt hydration
        widget = render_build_widget_text(build_id, db_path=self.db_path)
        self.assertIn("Upgrade Telemetry & Queue Engine", widget)
        self.assertIn("Eliminate turn-0 discovery reads", widget)
        self.assertIn("0/2 chunk(s) merged", widget)
        self.assertIn("gemini-3.8-flash-high", widget)

        embed = render_build_widget_embed(build_id, db_path=self.db_path)
        self.assertEqual(embed["color"], 0xF59E0B)
        self.assertIn("Upgrade Telemetry & Queue Engine", embed["title"])
        self.assertIn("**0/2** chunks", embed["description"])
        self.assertIn("1.** Build SQLite Schema", embed["description"])

        # Verify 1h TTL auto-delete for completed build cards
        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE forge_builds SET status = 'completed', forge_message_id = 123456789, updated_at = ? WHERE id = ?",
                (time.time() - 3700.0, build_id),
            )
        pruned = prune_completed_build_widgets(ttl_seconds=3600.0, db_path=self.db_path, delete_remote=False)
        self.assertIn(build_id, pruned)
        with forge_mod.get_db_connection(self.db_path) as conn:
            row_after = conn.execute("SELECT forge_message_id FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
            self.assertIsNone(row_after["forge_message_id"])

        prompt_c0 = format_chunk_prompt(build_id, 0, db_path=self.db_path, worktree_dir="/workspace")
        self.assertIn("Use WAL mode on SQLite connections.", prompt_c0)
        self.assertIn("**Overall Build Objective (Why):** Eliminate turn-0 discovery reads", prompt_c0)
        self.assertIn("Exemplar: Always open SQLite connections", prompt_c0)
        self.assertIn("Chunk Exemplar: Return (bool, str) from validators.", prompt_c0)
        self.assertIn("Pre-Hydrated Target Symbol Slices (Turn-0 Fast-Path Context):", prompt_c0)
        self.assertIn("def validate_forge_plan", prompt_c0)
        self.assertIn("**Prohibited Files (DO NOT MODIFY):** `tools/bridge.py`", prompt_c0)
        self.assertIn("DO NOT implement the following upcoming chunks", prompt_c0)
        self.assertIn("Chunk 2: Wire Bridge Slash Command", prompt_c0)

        # Verify Chunk 1 (index 1) auto-synthesizes baseline verification_cmd when omitted
        prompt_c1 = format_chunk_prompt(build_id, 1, db_path=self.db_path, worktree_dir="/workspace")
        self.assertIn("python3 -m py_compile", prompt_c1)

        # Verify originating-channel completion envelope
        env_text = format_origin_completion_envelope(build_id, db_path=self.db_path)
        self.assertIn(build_id, env_text)
        self.assertIn("touches_core_bridge=true", env_text)
        self.assertIn("auto-baseline", env_text)

        with patch("tools.outbox.queue_outbox_message") as mock_outbox:
            ok = dispatch_build_completion_to_origin(build_id, db_path=self.db_path)
            self.assertTrue(ok)
            mock_outbox.assert_called_once()
            self.assertEqual(mock_outbox.call_args.kwargs["channel"], 1599999999999999999)
            self.assertEqual(mock_outbox.call_args.kwargs["msg_type"], "agent_turn")

    def test_02_decisions_framework_and_privacy_filter(self):
        d_ops = stage_decision(
            category="homelab",
            title="Restart Z-Wave JS UI",
            summary="Node 14 ping timed out; run scoped compose restart.",
            handler_type="shell",
            payload={"command": "echo 'restarted zwave'", "cwd": "/tmp"},
            channel_id=ZERO_OPS_CHANNEL_ID,
            db_path=self.db_path,
        )
        d_priv = stage_decision(
            category="email",
            title="Send Contractor Reply",
            summary="Confirm Tuesday 9:00 AM PT window.",
            handler_type="shell",
            payload={"command": "echo 'sent mail'", "cwd": "/tmp"},
            channel_id=ZERO_CHAT_CHANNEL_ID,
            db_path=self.db_path,
        )

        digest_ops = format_queue_digest(request_channel_id=ZERO_OPS_CHANNEL_ID, db_path=self.db_path)
        self.assertIn("Restart Z-Wave JS UI", digest_ops)
        self.assertNotIn("Send Contractor Reply", digest_ops)

        digest_priv = format_queue_digest(request_channel_id=ZERO_CHAT_CHANNEL_ID, db_path=self.db_path)
        self.assertIn("Restart Z-Wave JS UI", digest_priv)
        self.assertIn("Send Contractor Reply", digest_priv)

        unauth = execute_decision(d_ops["decision_id"], user_id=999999, db_path=self.db_path)
        self.assertFalse(unauth["ok"])

        exec_res = execute_decision(d_ops["decision_id"], user_id=OWNER_USER_ID, db_path=self.db_path)
        self.assertTrue(exec_res["ok"])
        self.assertEqual(exec_res["status"], "approved")
        self.assertIn("restarted zwave", exec_res["receipt"])
        self.assertIn("Approved & Executed", exec_res["formatted_card"])

        dis_res = dismiss_decision(d_priv["decision_id"], user_id=OWNER_USER_ID, db_path=self.db_path)
        self.assertTrue(dis_res["ok"])
        self.assertEqual(dis_res["status"], "dismissed")

        d_exp = stage_decision(
            category="homelab",
            title="Stale Action",
            summary="Should expire immediately.",
            handler_type="shell",
            payload={"command": "true"},
            channel_id=ZERO_OPS_CHANNEL_ID,
            ttl_seconds=-10.0,
            db_path=self.db_path,
        )
        expired = expire_stale_decisions(db_path=self.db_path)
        self.assertIn(d_exp["decision_id"], expired)

    def test_03_heartbeat_tracker_and_runaway_loop_breaker(self):
        brain_root = Path(self.tmp_dir.name) / "brain"
        conv_id = "conv-hb-001"
        tasks_dir = brain_root / conv_id / ".system_generated" / "tasks"
        logs_dir = brain_root / conv_id / ".system_generated" / "logs"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        tracker = HeartbeatTracker(conv_id=conv_id, brain_dir=brain_root)
        self.assertFalse(tracker.poll_disk_progress())

        task_log = tasks_dir / "task-1.log"
        task_log.write_text("Step 1 output...\n", encoding="utf-8")
        self.assertTrue(tracker.poll_disk_progress())
        self.assertFalse(tracker.poll_disk_progress())
        task_log.write_text("Step 1 output...\nStep 2 output...\n", encoding="utf-8")
        self.assertTrue(tracker.poll_disk_progress())

        tfile = logs_dir / "transcript.jsonl"
        entries = [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "fix bug"},
        ]
        for _ in range(3):
            entries.append({
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest broken.py"}}],
            })
            entries.append({
                "type": "GENERIC",
                "content": "Command exited with code 1.\nError: module broken not found",
            })
        tfile.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        is_loop, reason = check_runaway_tool_loop(conv_id, brain_dir=brain_root, threshold=3)
        self.assertTrue(is_loop)
        self.assertIn("Runaway tool loop", reason)

    def test_04_verify_chunk_worktree_and_optional_baseline_fallback(self):
        wt_dir = Path(self.tmp_dir.name) / "wt_repo"
        wt_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Zero"], cwd=str(wt_dir), check=True, capture_output=True)

        (wt_dir / "allowed.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
        (wt_dir / "prohibited.py").write_text("y = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt_dir), check=True, capture_output=True)

        # Modify prohibited file -> must fail boundary guard
        (wt_dir / "prohibited.py").write_text("y = 2\n", encoding="utf-8")
        passed, _, err = verify_chunk_worktree(wt_dir, "true", prohibited_files=["prohibited.py"])
        self.assertFalse(passed)
        self.assertIn("Boundary violation", err)

        # Restore prohibited file -> no changes in worktree -> must fail when require_changes=True
        subprocess.run(["git", "checkout", "prohibited.py"], cwd=str(wt_dir), check=True, capture_output=True)
        passed_empty, _, err_empty = verify_chunk_worktree(
            wt_dir,
            "true",
            prohibited_files=["prohibited.py"],
            target_files=["allowed.py"],
            require_changes=True,
        )
        self.assertFalse(passed_empty)
        self.assertIn("zero file changes", err_empty)

        # Modify allowed file -> passes even with empty verification_cmd (baseline fallback)
        (wt_dir / "allowed.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
        passed, diff_stat, err = verify_chunk_worktree(
            wt_dir,
            "",
            prohibited_files=["prohibited.py"],
            target_files=["allowed.py"],
            require_changes=True,
        )
        self.assertTrue(passed, err)
        self.assertIn("1 file changed", diff_stat)

        # Verify AST symbol extractor on allowed.py
        snip = extract_symbol_snippet(wt_dir / "allowed.py", "greet")
        self.assertIn("def greet():", snip)

    def test_05_native_forge_job_runner(self):
        tasks_tmp = Path(self.tmp_dir.name) / "tasks"
        tasks_tmp.mkdir(parents=True, exist_ok=True)
        with patch.object(forge_mod, "TASKS_DIR", tasks_tmp), patch("tools.outbox.queue_outbox_message") as mock_outbox:
            task_id = "dt-test-native-job"
            t_dir = tasks_tmp / task_id
            t_dir.mkdir(parents=True, exist_ok=True)
            log_file = t_dir / "run.log"
            log_file.write_text("Job output line 1\n", encoding="utf-8")

            meta = {
                "task_id": task_id,
                "name": "Native Forge Job",
                "command": "echo ok",
                "channel": "zero-chat",
                "timeout": 60,
                "cwd": "/workspace",
                "status": "starting",
                "notify_on_success": False,
                "log_file": str(log_file),
                "start_time": time.time(),
            }
            _write_job_meta(task_id, meta, db_path=self.db_path)

            mock_proc = MagicMock()
            mock_proc.wait.return_value = 0
            mock_proc.pid = 88888
            with patch("subprocess.Popen", return_value=mock_proc):
                _run_job_worker(task_id, db_path=self.db_path)

            mock_outbox.assert_not_called()
            st = get_job_status(task_id, db_path=self.db_path)
            self.assertEqual(st["status"], "completed")
            self.assertEqual(st["exit_code"], 0)
            jobs = list_jobs(limit=5, db_path=self.db_path)
            self.assertTrue(any(j["task_id"] == task_id for j in jobs))

    def test_06_vault_aes256_gcm_roundtrip_and_rule_platform_limits(self):
        sec_dir = Path(self.tmp_dir.name) / "mock_secrets"
        sec_dir.mkdir(parents=True, exist_ok=True)
        (sec_dir / "env.json").write_text('{"MOCK_KEY": "super-secret"}', encoding="utf-8")
        (sec_dir / "pii_rules.json").write_text('{"patterns": []}', encoding="utf-8")
        key_file = sec_dir / ".vault_master.key"
        out_enc = Path(self.tmp_dir.name) / "secrets.tar.gz.enc"

        enc_res = encrypt_secrets_archive(secrets_dir=sec_dir, output_file=out_enc, key_path=key_file)
        self.assertEqual(enc_res["included_count"], 2)
        self.assertTrue(out_enc.exists())
        raw_cipher = out_enc.read_bytes()
        self.assertNotIn(b"super-secret", raw_cipher)

        members = decrypt_secrets_archive(encrypted_file=out_enc, key_path=key_file)
        self.assertEqual(sorted(members), ["env.json", "pii_rules.json"])

        agents_md = Path("/workspace/agents.md")
        self.assertLessEqual(agents_md.stat().st_size, 24000)
        for rule_file in Path("/workspace/.agents/rules").glob("*.md"):
            self.assertLessEqual(rule_file.stat().st_size, 24000, f"{rule_file.name} exceeds 24KB limit")

    def test_07_ivy_channel_and_repo_resolution(self):
        with patch.dict(forge_mod.os.environ, {"DISCORD_CHANNEL_ID": "1548196929308065893"}):
            self.assertEqual(forge_mod.get_default_origin_channel(), "baseball")

        # Verify in_flight_turn.json ancestor PID fallback when DISCORD_CHANNEL_ID is unset
        mock_in_flight_dir = Path(self.tmp_dir.name)
        in_flight_file = mock_in_flight_dir / "in_flight_turn.json"
        in_flight_file.write_text(
            json.dumps(
                {
                    "1561509863018856468": {
                        "channel_id": 1561509863018856468,
                        "pid": forge_mod.os.getpid(),
                        "ts": time.time(),
                        "prompt": "test gotchi",
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(forge_mod.os.environ, {}, clear=False):
            forge_mod.os.environ.pop("DISCORD_CHANNEL_ID", None)
            with patch.object(forge_mod, "DATA_DIR", mock_in_flight_dir):
                self.assertEqual(forge_mod.get_default_origin_channel(), "1561509863018856468")
                self.assertTrue(forge_mod._is_channel_turn_actively_in_flight("1561509863018856468"))

        ivy_plan = {
            "title": "Ivy Projection Engine Upgrade",
            "repo": "/workspace",
            "origin_channel_id": 1548196929308065893,
            "context_fact_sheet": ["Maintain 100% endogenous BIG baseline."],
            "chunks": [
                {
                    "title": "Update ivy_daemon.py",
                    "target_files": ["tools/ivy_daemon.py"],
                    "instructions": "Implement activity lease renewal in ivy_daemon.py.",
                }
            ],
        }
        enq = enqueue_build(ivy_plan, db_path=self.db_path, post_widget=False)
        self.assertTrue(enq["ok"])
        embed = render_build_widget_embed(enq["build_id"], db_path=self.db_path)
        self.assertIn("`Ivy-Agent`", embed["description"])
        self.assertIn("<#1548196929308065893>", embed["description"])

        header = forge_mod.format_forge_channel_header(db_path=self.db_path)
        self.assertIn("**F**ederated **O**rchestration, **R**untime & **G**ated **E**xecution", header)
        self.assertIn("**Active FORGE Builds:**", header)
        self.assertIn("Ivy Projection Engine Upgrade", header)
        self.assertIn("**Active Background Tasks:**", header)
        self.assertIn("**Pending Action Approvals (Decisions):**", header)

        # Verify shared FORGE_DB_PATH resolution prefers /baseball/data/forge.db when /baseball/data exists
        with patch.object(forge_mod, "Path") as mock_path_cls:
            pass
        self.assertTrue(str(forge_mod.resolve_default_forge_db_path()).endswith("forge.db"))


if __name__ == "__main__":
    unittest.main()
