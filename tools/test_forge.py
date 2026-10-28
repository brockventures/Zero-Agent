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
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
elif sys.path[0] != _REPO_ROOT:
    sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)
if "/workspace" not in sys.path:
    sys.path.append("/workspace")

import tools.forge as forge_mod
from tools.forge import (
    FORGE_CHANNEL_ID,
    OWNER_USER_ID,
    ZERO_CHAT_CHANNEL_ID,
    ZERO_OPS_CHANNEL_ID,
    HeartbeatTracker,
    _run_job_worker,
    _write_job_meta,
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
    post_or_update_job_widget,
    prune_completed_build_widgets,
    prune_completed_job_widgets,
    render_build_widget_embed,
    render_job_widget_embed,
    render_build_widget_text,
    stage_decision,
     validate_forge_plan,
    verify_chunk_worktree,
)
from tools.vault_sync import decrypt_secrets_archive, encrypt_secrets_archive


class TestForgeEngine(unittest.TestCase):
    def setUp(self):
        self._saved_channel_id = os.environ.pop("DISCORD_CHANNEL_ID", None)
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_forge.db"
        init_forge_db(self.db_path)

    def tearDown(self):
        if self._saved_channel_id is not None:
            os.environ["DISCORD_CHANNEL_ID"] = self._saved_channel_id
        else:
            os.environ.pop("DISCORD_CHANNEL_ID", None)
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

        # Verify live stream-json telemetry extraction and active chunk card rendering
        sample_tool_ev = json.dumps({
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "RUNNING",
                "tool_name": "edit_file",
                "thinking": "Adding live stream telemetry to #forge embed cards.",
                "tool_info": {"parameters": {"Path": "/workspace/tools/forge.py"}},
            },
        })
        live_str, step_cnt = forge_mod.extract_live_chunk_telemetry(sample_tool_ev, step_count=3)
        self.assertEqual(step_cnt, 4)
        self.assertIn("Step 4 · Editing `forge.py`", live_str)
        self.assertIn("Adding live stream telemetry", live_str)

        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE forge_chunks SET status = 'running', started_at = ?, live_status = ? WHERE build_id = ? AND chunk_index = 0",
                (time.time() - 42.0, live_str, build_id),
            )
        embed_running = render_build_widget_embed(build_id, db_path=self.db_path)
        self.assertIn("↳ ⚡", embed_running["description"])
        self.assertIn("Step 4 · Editing `forge.py`", embed_running["description"])

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

        # Verify dict-structured target_symbols ({"file": ..., "symbol": ...}) and shared_context
        # both at enqueue time and when pre-existing unnormalized JSON is stored in forge.db
        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE forge_chunks SET target_symbols_json = ?, shared_context_json = ? WHERE build_id = ? AND chunk_index = 0",
                (
                    json.dumps([{"file": "tools/forge.py", "symbol": "validate_forge_plan"}]),
                    json.dumps([{"file": "tools/forge.py", "note": "Normalize object-form target_symbols cleanly."}]),
                    build_id,
                ),
            )
        prompt_dict_syms = format_chunk_prompt(build_id, 0, db_path=self.db_path, worktree_dir="/workspace")
        self.assertIn("`tools/forge.py:validate_forge_plan`", prompt_dict_syms)
        self.assertIn("def validate_forge_plan", prompt_dict_syms)
        self.assertIn("tools/forge.py: Normalize object-form target_symbols cleanly.", prompt_dict_syms)

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
        with patch.object(forge_mod, "Path"):
            pass
        self.assertTrue(str(forge_mod.resolve_default_forge_db_path()).endswith("forge.db"))

    def test_08_cross_container_unified_db_and_job_widget_lifecycle(self):
        # 1. Test unified cross-DB querying via _query_unified_rows
        peer_db = Path(self.tmp_dir.name) / "peer_forge.db"
        init_forge_db(peer_db)

        # Primary DB has j1 (older) and j2
        t0 = time.time()
        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO forge_jobs (id, name, command, start_time, status)
                VALUES ('j1', 'Job 1', 'echo 1', ?, 'running'),
                       ('j2', 'Job 2', 'echo 2', ?, 'running')
                """,
                (t0, t0 + 10.0),
            )

        # Peer DB has j3 and j1 (newer update with end_time)
        with forge_mod.get_db_connection(peer_db) as conn:
            conn.execute(
                """
                INSERT INTO forge_jobs (id, name, command, start_time, end_time, status)
                VALUES ('j3', 'Job 3', 'echo 3', ?, ?, 'running'),
                       ('j1', 'Job 1', 'echo 1', ?, ?, 'completed')
                """,
                (t0 + 20.0, t0 + 25.0, t0, t0 + 30.0),
            )

        # Non-production DB path skips peer DBs by default
        self.assertEqual(forge_mod._get_peer_forge_db_paths(self.db_path), [])
        single_rows = forge_mod._query_unified_rows(
            "SELECT * FROM forge_jobs",
            db_path=self.db_path,
            sort_key="start_time",
            reverse=True,
        )
        self.assertEqual(len(single_rows), 2)

        # When peers are returned, _query_unified_rows merges and deduplicates
        with patch.object(forge_mod, "_get_peer_forge_db_paths", return_value=[peer_db]):
            merged = forge_mod._query_unified_rows(
                "SELECT * FROM forge_jobs",
                db_path=self.db_path,
                sort_key="start_time",
                reverse=True,
            )
            # j1, j2, j3 = 3 unique jobs
            self.assertEqual(len(merged), 3)
            # j3 has start_time = t0 + 20
            self.assertEqual(merged[0]["id"], "j3")
            self.assertEqual(merged[1]["id"], "j2")
            self.assertEqual(merged[2]["id"], "j1")
            # j1 should have the newer status from peer DB ('completed')
            self.assertEqual(merged[2]["status"], "completed")

            # Test limit
            limited = forge_mod._query_unified_rows(
                "SELECT * FROM forge_jobs",
                db_path=self.db_path,
                sort_key="start_time",
                reverse=True,
                limit=2,
            )
            self.assertEqual(len(limited), 2)
            self.assertEqual(limited[0]["id"], "j3")
            self.assertEqual(limited[1]["id"], "j2")

        # Test candidate discovery when FORGE_DB_PATH is patched
        with patch.object(forge_mod, "FORGE_DB_PATH", self.db_path):
            with patch.object(forge_mod, "PEER_FORGE_DB_CANDIDATES", [peer_db]):
                peers = forge_mod._get_peer_forge_db_paths(self.db_path)
                self.assertEqual(peers, [peer_db])

        # 2. Test render_job_widget_embed
        tasks_tmp = Path(self.tmp_dir.name) / "tasks"
        tasks_tmp.mkdir(parents=True, exist_ok=True)
        task_id = "dt-render-test-01"
        t_dir = tasks_tmp / task_id
        t_dir.mkdir(parents=True, exist_ok=True)
        log_file = t_dir / "run.log"
        log_file.write_text(
            "--- [Detached Runner] Started at 2026-10-24T00:00:00Z ---\n"
            "--- Command: pytest tests/ ---\n"
            "[Detached Runner] Starting worker\n"
            "Line A\n"
            "Line B\n"
            "Line C\n"
            "Line D\n"
            "Line E\n",
            encoding="utf-8",
        )

        job_meta = {
            "task_id": task_id,
            "name": "Integration Test Job",
            "command": "pytest tests/test_runner.py -v",
            "channel": "baseball",
            "cwd": "/volume1/baseball",
            "status": "running",
            "start_time": time.time() - 45.0,
            "log_file": str(log_file),
        }
        with patch.object(forge_mod, "TASKS_DIR", tasks_tmp):
            forge_mod._write_job_meta(task_id, job_meta, db_path=self.db_path)

            embed = render_job_widget_embed(task_id, db_path=self.db_path)
            self.assertIn("⚙️", embed["title"])
            self.assertIn("Integration Test Job", embed["title"])
            self.assertEqual(embed["color"], 0x3B82F6)
            self.assertIn("**RUNNING**", embed["description"])
            self.assertIn("`baseball`", embed["description"])
            self.assertIn("<#1548196929308065893>", embed["description"])
            self.assertIn("pytest tests/test_runner.py -v", embed["description"])
            # Should have the last 4 non-banner lines: Line B, Line C, Line D, Line E
            self.assertIn("Line B\nLine C\nLine D\nLine E", embed["description"])
            self.assertNotIn("--- [Detached Runner]", embed["description"])
            self.assertNotIn("--- Command:", embed["description"])
            self.assertNotIn("[Detached Runner] Starting worker", embed["description"])
            self.assertNotIn("Line A", embed["description"])

            # Verify single-line and total Recent Output character caps (100/line, 280 total)
            log_file.write_text(
                "--- [Detached Runner] Started ---\n"
                "--- Command: python3 script.py ---\n"
                "--- Target Channel: 123 | Timeout: 60s ---\n"
                + ("X" * 500) + "\n"
                + ("Y" * 500) + "\n"
                + ("Z" * 500) + "\n",
                encoding="utf-8",
            )
            capped_embed = render_job_widget_embed(task_id, db_path=self.db_path)
            recent_block = capped_embed["description"].split("**Recent Output:**\n```text\n")[1].split("\n```")[0]
            self.assertLessEqual(len(recent_block), 280)
            for ln in recent_block.splitlines():
                self.assertLessEqual(len(ln), 103)

            # Test unknown task embed
            unknown_embed = render_job_widget_embed("non-existent-task", db_path=self.db_path)
            self.assertIn("⚠️ Job non-existent-task not found", unknown_embed["title"])

        # 3. Test post_or_update_job_widget (POST then PATCH)
        with patch.object(forge_mod, "TASKS_DIR", tasks_tmp):
            with patch.object(forge_mod, "_get_discord_token", return_value="fake_bot_token"):
                # Initial POST: create widget
                mock_post_resp = MagicMock()
                mock_post_resp.status_code = 200
                mock_post_resp.json.return_value = {"id": "1122334455"}

                with patch("requests.post", return_value=mock_post_resp) as mock_post, patch("requests.patch") as mock_patch:
                    posted_id = post_or_update_job_widget(task_id, db_path=self.db_path)
                    self.assertEqual(posted_id, 1122334455)
                    self.assertTrue(mock_post.called)
                    self.assertFalse(mock_patch.called)

                # Verify forge_message_id is recorded in SQLite
                with forge_mod.get_db_connection(self.db_path) as conn:
                    row = conn.execute("SELECT forge_message_id FROM forge_jobs WHERE id = ?", (task_id,)).fetchone()
                    self.assertEqual(row["forge_message_id"], 1122334455)

                # Second call: PATCH existing widget
                mock_patch_resp = MagicMock()
                mock_patch_resp.status_code = 200

                with patch("requests.post") as mock_post2, patch("requests.patch", return_value=mock_patch_resp) as mock_patch2:
                    updated_id = post_or_update_job_widget(task_id, db_path=self.db_path)
                    self.assertEqual(updated_id, 1122334455)
                    self.assertFalse(mock_post2.called)
                    self.assertTrue(mock_patch2.called)

        # 4. Test 1h TTL auto-pruning of completed job widgets
        with patch.object(forge_mod, "TASKS_DIR", tasks_tmp):
            # Mark job as completed >1h ago
            with forge_mod.get_db_connection(self.db_path) as conn:
                conn.execute(
                    "UPDATE forge_jobs SET status = 'completed', end_time = ? WHERE id = ?",
                    (time.time() - 3700.0, task_id),
                )

            # Test standalone prune_completed_job_widgets
            pruned = prune_completed_job_widgets(ttl_seconds=3600.0, db_path=self.db_path, delete_remote=False)
            self.assertIn(task_id, pruned)

            # Check that forge_message_id was set to NULL
            with forge_mod.get_db_connection(self.db_path) as conn:
                row_after = conn.execute("SELECT forge_message_id FROM forge_jobs WHERE id = ?", (task_id,)).fetchone()
                self.assertIsNone(row_after["forge_message_id"])

            # Also verify prune_completed_build_widgets prunes completed, failed (>1h), and cancelled (immediate) jobs
            with forge_mod.get_db_connection(self.db_path) as conn:
                conn.execute(
                    "UPDATE forge_jobs SET status = 'cancelled', forge_message_id = 99887766, end_time = ? WHERE id = ?",
                    (time.time(), task_id),
                )
            pruned_all = prune_completed_build_widgets(ttl_seconds=3600.0, db_path=self.db_path, delete_remote=False)
            self.assertIn(task_id, pruned_all)
            with forge_mod.get_db_connection(self.db_path) as conn:
                row_all = conn.execute("SELECT forge_message_id FROM forge_jobs WHERE id = ?", (task_id,)).fetchone()
                self.assertIsNone(row_all["forge_message_id"])

            # 5. Verify remote channel sweep catches completed/failed cards and duplicate STARTING cards
            mock_get_resp = MagicMock()
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = [
                {
                    "id": str(forge_mod.FORGE_HEADER_MESSAGE_ID),
                    "timestamp": "2026-10-20T00:00:00.000000+00:00",
                    "embeds": [],
                },
                {
                    "id": "1563660794455924747",
                    "timestamp": "2099-10-24T21:09:44.811000+00:00",
                    "edited_timestamp": "2099-10-24T21:09:56.158000+00:00",
                    "embeds": [
                        {
                            "title": "✅ Doorbell Frame Grab Setup",
                            "description": "**COMPLETED** · `Zero-Agent`",
                            "footer": {"text": "dt-1792876183-doorbell · 2026-10-24 02:09:55 PM PT"},
                        }
                    ],
                },
                {
                    "id": "1563660792941912097",
                    "timestamp": "2099-10-24T21:09:44.450000+00:00",
                    "embeds": [
                        {
                            "title": "⏳ Doorbell Frame Grab Setup",
                            "description": "**STARTING** · `Zero-Agent`",
                            "footer": {"text": "dt-1792876183-doorbell · 2026-10-24 02:09:43 PM PT"},
                        }
                    ],
                },
                {
                    "id": "1563423123603324939",
                    "timestamp": "2026-10-20T00:00:00.000000+00:00",
                    "edited_timestamp": "2026-10-20T01:00:00.000000+00:00",
                    "embeds": [
                        {
                            "title": "❌ Epic 40: Refresh BIG Peak",
                            "description": "**FAILED** · `big-projections`",
                            "footer": {"text": "Build ID: fg-1792819519-epic-40 · Auto-clears in 1h"},
                        }
                    ],
                },
            ]
            mock_del_resp = MagicMock()
            mock_del_resp.status_code = 204
            with patch.object(forge_mod, "_get_discord_token", return_value="mock-token"), patch(
                "requests.get", return_value=mock_get_resp
            ), patch("requests.delete", return_value=mock_del_resp) as mock_del:
                swept = prune_completed_build_widgets(
                    ttl_seconds=3600.0,
                    db_path=self.db_path,
                    delete_remote=True,
                    force_channel_sweep=True,
                )
                self.assertIn("fg-1792819519-epic-40", swept)
                self.assertTrue(mock_del.called)

    def test_09_failure_envelope_and_job_failure_circuit_breaker(self):
        # 1. Test 3-strike build failure envelope & origin dispatch
        plan = {
            "title": "Broken Build Test",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Test failure envelope"],
            "chunks": [
                {
                    "title": "Failing Chunk",
                    "target_files": ["tools/forge.py"],
                    "instructions": "Fail verification.",
                    "verification_cmd": "pytest tools/test_forge.py",
                }
            ],
        }
        enq = enqueue_build(plan, db_path=self.db_path, post_widget=False)
        build_id = enq["build_id"]
        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE forge_chunks SET status = 'failed', last_error = 'AssertionError: expected 1 == 2' WHERE build_id = ?",
                (build_id,),
            )
            conn.execute("UPDATE forge_builds SET status = 'failed' WHERE id = ?", (build_id,))

        fail_env = forge_mod.format_origin_failure_envelope(build_id, db_path=self.db_path)
        self.assertIn("FORGE BUILD FAILURE ENVELOPE", fail_env)
        self.assertIn("Failing Chunk", fail_env)
        self.assertIn("AssertionError: expected 1 == 2", fail_env)

        with patch("tools.outbox.queue_outbox_message") as mock_outbox:
            self.assertTrue(forge_mod.dispatch_build_failure_to_origin(build_id, db_path=self.db_path))
            mock_outbox.assert_called_once()
            self.assertEqual(mock_outbox.call_args.kwargs["channel"], ZERO_OPS_CHANNEL_ID)

        # 2. Test repeated detached job failure circuit breaker
        now = time.time()
        with forge_mod.get_db_connection(self.db_path) as conn:
            for idx in (1, 2):
                conn.execute(
                    """
                    INSERT INTO forge_jobs (id, name, command, start_time, end_time, status, exit_code)
                    VALUES (?, 'Configure Hardcover', 'python3 /workspace/scratch/configure_hardcover.py', ?, ?, 'failed', 1)
                    """,
                    (f"dt-fail-{idx}", now - 60.0 + idx, now - 50.0 + idx),
                )

        ok_cb, cb_reason = forge_mod._check_recent_repeated_job_failures(
            "python3 /workspace/scratch/configure_hardcover.py",
            "Configure Hardcover",
            db_path=self.db_path,
        )
        self.assertFalse(ok_cb)
        self.assertIn("Circuit Breaker Triggered", cb_reason)
        with self.assertRaises(RuntimeError):
            forge_mod.start_job(
                command="python3 /workspace/scratch/configure_hardcover.py",
                name="Configure Hardcover",
                db_path=self.db_path,
            )

    def test_10_vulcan_worktree_profile_staging_and_restoration(self):
        # 1. Test /workspace-style repo with tracked GEMINI.md and chat rules
        wt_zero = Path(self.tmp_dir.name) / "wt_zero"
        (wt_zero / ".agents" / "rules").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(wt_zero), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(wt_zero), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Zero"], cwd=str(wt_zero), check=True, capture_output=True)

        (wt_zero / "agents.md").write_text("# Zero Persona\n", encoding="utf-8")
        (wt_zero / "GEMINI.md").symlink_to("agents.md")
        (wt_zero / ".agents" / "rules" / "discord_formatting.md").write_text("# Discord Rules\n", encoding="utf-8")
        (wt_zero / ".agents" / "rules" / "tool_roundtrip_minimization.md").write_text("# Batching\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(wt_zero), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init zero"], cwd=str(wt_zero), check=True, capture_output=True)

        snap_zero = forge_mod.stage_vulcan_worktree_profile(
            wt_zero,
            repo_dir="/workspace",
            target_files=["tools/forge.py"],
            origin_channel_id=ZERO_OPS_CHANNEL_ID,
        )
        self.assertFalse(snap_zero["is_baseball"])
        self.assertIn("Vulcan", (wt_zero / "GEMINI.md").read_text(encoding="utf-8"))
        self.assertFalse((wt_zero / ".agents" / "rules" / "discord_formatting.md").exists())
        self.assertTrue((wt_zero / ".agents" / "rules" / "tool_roundtrip_minimization.md").exists())
        self.assertTrue((wt_zero / ".agents" / "hooks.json").exists())

        forge_mod.restore_vulcan_worktree_profile(wt_zero, snap_zero)
        self.assertTrue((wt_zero / "GEMINI.md").is_symlink())
        self.assertEqual((wt_zero / "agents.md").read_text(encoding="utf-8"), "# Zero Persona\n")
        self.assertTrue((wt_zero / ".agents" / "rules" / "discord_formatting.md").exists())
        st_zero = subprocess.run(["git", "status", "--porcelain"], cwd=str(wt_zero), capture_output=True, text=True)
        self.assertEqual(st_zero.stdout.strip(), "")

        # 2. Test /baseball-style repo with zero tracked GEMINI.md or .agents/
        wt_bb = Path(self.tmp_dir.name) / "wt_baseball"
        wt_bb.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(wt_bb), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(wt_bb), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Ivy"], cwd=str(wt_bb), check=True, capture_output=True)
        (wt_bb / "engine.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(wt_bb), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init bb"], cwd=str(wt_bb), check=True, capture_output=True)

        snap_bb = forge_mod.stage_vulcan_worktree_profile(
            wt_bb,
            repo_dir="/baseball/big-projections",
            target_files=["engine.py"],
            origin_channel_id=1548196929308065893,
        )
        self.assertTrue(snap_bb["is_baseball"])
        self.assertIn("Vulcan", (wt_bb / "GEMINI.md").read_text(encoding="utf-8"))
        self.assertTrue((wt_bb / ".agents" / "hooks.json").exists())
        if Path("/baseball/data/vulcan_baseball_rules/big_projection_engine_invariants.md").exists():
            self.assertTrue((wt_bb / ".agents" / "rules" / "big_projection_engine_invariants.md").exists())

        forge_mod.restore_vulcan_worktree_profile(wt_bb, snap_bb)
        self.assertFalse((wt_bb / "GEMINI.md").exists())
        self.assertFalse((wt_bb / ".agents").exists())
        st_bb = subprocess.run(["git", "status", "--porcelain"], cwd=str(wt_bb), capture_output=True, text=True)
        self.assertEqual(st_bb.stdout.strip(), "")

    def test_11_parallel_builds_and_intra_build_parallel_chunks(self):
        # 1. Verify _chunks_are_eligible_in_parallel respects disjoint target_files, shared files, and explicit depends_on
        plan_parallel = {
            "title": "Parallel 3-Chunk Feature",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Disjoint chunks run in parallel up to 3x."],
            "chunks": [
                {
                    "title": "Chunk A (file_a.py)",
                    "target_files": ["tools/file_a.py"],
                    "instructions": "Implement module A with unit tests in file_a.py.",
                },
                {
                    "title": "Chunk B (file_b.py - disjoint)",
                    "target_files": ["tools/file_b.py"],
                    "instructions": "Implement module B with unit tests in file_b.py.",
                },
                {
                    "title": "Chunk C (file_a.py - overlaps Chunk A)",
                    "target_files": ["tools/file_a.py"],
                    "instructions": "Extend module A after Chunk A merges.",
                },
                {
                    "title": "Chunk D (file_d.py - explicit depends_on Chunk B)",
                    "target_files": ["tools/file_d.py"],
                    "depends_on": [1],
                    "instructions": "Implement module D depending on Chunk B.",
                },
            ],
        }
        enq = enqueue_build(plan_parallel, db_path=self.db_path, post_widget=False)
        build_id = enq["build_id"]
        with forge_mod.get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
                (build_id,),
            ).fetchall()

        # Initially: Chunk 0 (A) and Chunk 1 (B) are both eligible in parallel; Chunk 2 overlaps Chunk 0; Chunk 3 depends_on Chunk 1
        self.assertTrue(forge_mod._chunks_are_eligible_in_parallel(rows[0], rows, []))
        self.assertTrue(forge_mod._chunks_are_eligible_in_parallel(rows[1], rows, [rows[0]]))
        self.assertFalse(forge_mod._chunks_are_eligible_in_parallel(rows[2], rows, [rows[0]]))
        self.assertFalse(forge_mod._chunks_are_eligible_in_parallel(rows[3], rows, [rows[0], rows[1]]))

        # Once Chunk 0 and Chunk 1 mark 'merged', Chunk 2 and Chunk 3 both become eligible in parallel
        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute("UPDATE forge_chunks SET status = 'merged' WHERE build_id = ? AND chunk_index IN (0, 1)", (build_id,))
            rows_after = conn.execute(
                "SELECT * FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index ASC",
                (build_id,),
            ).fetchall()
        self.assertTrue(forge_mod._chunks_are_eligible_in_parallel(rows_after[2], rows_after, []))
        self.assertTrue(forge_mod._chunks_are_eligible_in_parallel(rows_after[3], rows_after, [rows_after[2]]))

        # 2. Verify ForgeSupervisor spawns up to max_parallel_builds (3) concurrent build workers
        for idx in range(2, 5):
            p = {
                "title": f"Concurrent Build {idx}",
                "repo": "/workspace",
                "origin_channel_id": ZERO_OPS_CHANNEL_ID,
                "context_fact_sheet": ["Parallel build test"],
                "chunks": [
                    {
                        "title": f"Chunk for Build {idx}",
                        "target_files": [f"tools/parallel_mod_{idx}.py"],
                        "instructions": f"Implement parallel_mod_{idx}.py cleanly.",
                    }
                ],
            }
            enqueue_build(p, db_path=self.db_path, post_widget=False)

        sup = forge_mod.ForgeSupervisor(db_path=self.db_path, max_parallel_builds=3, max_parallel_chunks=3)
        mock_procs = []

        def _fake_popen(*args, **kwargs):
            mp = MagicMock()
            mp.poll.return_value = None
            mock_procs.append(mp)
            return mp

        with patch("subprocess.Popen", side_effect=_fake_popen), patch("builtins.open", unittest.mock.mock_open()):
            b1 = sup.maybe_spawn_next_build_worker()
            b2 = sup.maybe_spawn_next_build_worker()
            b3 = sup.maybe_spawn_next_build_worker()
            b4 = sup.maybe_spawn_next_build_worker()

        self.assertIsNotNone(b1)
        self.assertIsNotNone(b2)
        self.assertIsNotNone(b3)
        self.assertEqual(len({b1, b2, b3}), 3)
        self.assertIsNone(b4)  # Capped at 3 concurrent builds per agent

    def test_12_active_worker_worktree_preservation_and_superseded_job_pruning(self):
        import fcntl
        import shutil

        plan = {
            "title": "Active Worker Preservation Test",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Active worker test"],
            "chunks": [
                {
                    "title": "Chunk 0",
                    "target_files": ["tools/forge.py"],
                    "instructions": "Keep worktree alive across reload.",
                }
            ],
        }
        enq = enqueue_build(plan, db_path=self.db_path, post_widget=False)
        bid = enq["build_id"]
        wt_path = Path(f"/tmp/forge-wt-{bid}-c0")
        wt_path.mkdir(parents=True, exist_ok=True)
        try:
            with forge_mod.get_db_connection(self.db_path) as conn:
                conn.execute("UPDATE forge_builds SET status = 'running' WHERE id = ?", (bid,))
                conn.execute("UPDATE forge_chunks SET status = 'running' WHERE build_id = ?", (bid,))

            lock_path = self.db_path.parent / f".forge_build_worker_{bid}.lock"
            with open(lock_path, "a+", encoding="utf-8") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    forge_mod.reconcile_forge_on_boot(db_path=self.db_path)
                    self.assertTrue(wt_path.exists())
                    with forge_mod.get_db_connection(self.db_path) as conn:
                        b_row = conn.execute("SELECT status FROM forge_builds WHERE id = ?", (bid,)).fetchone()
                        c_row = conn.execute("SELECT status FROM forge_chunks WHERE build_id = ?", (bid,)).fetchone()
                        self.assertEqual(b_row["status"], "running")
                        self.assertEqual(c_row["status"], "running")
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        finally:
            shutil.rmtree(wt_path, ignore_errors=True)

        # Verify superseded failed job is pruned immediately when a newer job in the same channel completes
        now = time.time()
        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, forge_message_id)
                VALUES (?, 'Failed Probe', 'python3 probe.py', '1542081375287640084', '/workspace', 60, 'failed', ?, ?, 777001)
                """,
                ("dt-superseded-fail", now - 30.0, now - 25.0),
            )
            conn.execute(
                """
                INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, forge_message_id)
                VALUES (?, 'Fixed Probe', 'python3 probe.py', '1542081375287640084', '/workspace', 60, 'completed', ?, ?, 777002)
                """,
                ("dt-superseded-ok", now - 20.0, now - 10.0),
            )
        pruned_sup = prune_completed_job_widgets(ttl_seconds=3600.0, db_path=self.db_path, delete_remote=False)
        self.assertIn("dt-superseded-fail", pruned_sup)
        self.assertNotIn("dt-superseded-ok", pruned_sup)

    def test_13_complexity_and_calibration_approval_thresholds(self):
        # 1. Fast Tier 2 (1 chunk, <=3 non-core files, no >90s calibration) auto-executes without requiring approval
        fast_plan = {
            "title": "Fast Single-Chunk Feature",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Fast Tier 2 build"],
            "chunks": [
                {
                    "title": "Single Chunk",
                    "target_files": ["tools/fast_mod_a.py", "tools/test_fast_mod_a.py"],
                    "instructions": "Implement fast_mod_a.py.",
                }
            ],
        }
        res_fast = enqueue_build(
            fast_plan,
            db_path=self.db_path,
            post_widget=False,
            check_approval_gate=True,
            batch_state_path="/tmp/nonexistent_batch_state.json",
        )
        self.assertTrue(res_fast["ok"])
        self.assertIn("build_id", res_fast)
        self.assertFalse(res_fast.get("requires_approval", False))

        # 2. Multi-chunk Tier 3 (>=2 chunks) automatically stages an Approve | Edit | Dismiss Decision Card
        tier3_plan = {
            "title": "Multi-Chunk Pipeline Build",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Tier 3 build"],
            "chunks": [
                {
                    "title": "Chunk 1",
                    "target_files": ["tools/t3_a.py"],
                    "instructions": "Implement t3_a.py.",
                },
                {
                    "title": "Chunk 2",
                    "target_files": ["tools/t3_b.py"],
                    "instructions": "Implement t3_b.py.",
                },
            ],
        }
        res_t3 = enqueue_build(
            tier3_plan,
            db_path=self.db_path,
            post_widget=False,
            check_approval_gate=True,
            batch_state_path="/tmp/nonexistent_batch_state.json",
        )
        self.assertTrue(res_t3["ok"])
        self.assertEqual(res_t3["status"], "awaiting_approval")
        self.assertTrue(res_t3["requires_approval"])
        self.assertIn("decision_id", res_t3)

        # Clicking Approve on the staged Decision Card deterministically enqueues the build into forge_builds
        with patch.object(forge_mod, "post_or_update_build_widget", return_value=999111), patch.object(
            forge_mod, "sync_forge_channel_header", return_value=True
        ):
            exec_res = execute_decision(res_t3["decision_id"], user_id=OWNER_USER_ID, db_path=self.db_path)
        self.assertTrue(exec_res["ok"])
        self.assertEqual(exec_res["status"], "approved")
        self.assertIn("2 chunk(s)", exec_res["receipt"])

        # 3. Wide blast radius (>=4 files) or core bridge or >90s calibration also triggers approval gate
        wide_plan = {
            "title": "Four File Refactor",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["4 files"],
            "chunks": [
                {
                    "title": "Wide Chunk",
                    "target_files": ["tools/w1.py", "tools/w2.py", "tools/w3.py", "tools/w4.py"],
                    "instructions": "Refactor 4 files.",
                }
            ],
        }
        req_wide, reasons_wide = forge_mod.evaluate_plan_approval_gate(wide_plan, batch_state_path="/tmp/nonexistent.json")
        self.assertTrue(req_wide)
        self.assertTrue(any("4 target files" in r for r in reasons_wide))

        bs_file = Path(self.tmp_dir.name) / "calib_state.json"
        now_ts = time.time()
        bs_file.write_text(
            json.dumps({"last_time": now_ts, "turn_start_time": now_ts - 120.0, "total_scratch_runs": 3}),
            encoding="utf-8",
        )
        req_cal, reasons_cal = forge_mod.evaluate_plan_approval_gate(fast_plan, batch_state_path=bs_file)
        self.assertTrue(req_cal)
        self.assertTrue(any("Post-calibration" in r for r in reasons_cal))

    def test_14_fifo_serialization_of_overlapping_builds_and_silent_hang_watchdog(self):
        # 1. Verify maybe_spawn_next_build_worker holds overlapping builds in queued state (FIFO)
        # while allowing disjoint builds to spawn in parallel, then spawns the overlapping build
        # once the first build finishes.
        plan_first = {
            "title": "Standings Build 1 (Vegas Removal)",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["First standings build"],
            "chunks": [
                {
                    "title": "Update Standings Page",
                    "target_files": ["tools/standings_page.py", "tools/predict_teams.py"],
                    "instructions": "Remove Vegas benchmark tab and round totals.",
                }
            ],
        }
        plan_overlap = {
            "title": "Standings Build 2 (Horizon Toggle)",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Second overlapping standings build"],
            "chunks": [
                {
                    "title": "Add Horizon Dropdown",
                    "target_files": ["tools/standings_page.py"],
                    "instructions": "Add Preseason / Actual to Date / Rest of Season dropdown.",
                }
            ],
        }
        plan_disjoint = {
            "title": "Unrelated Audio Build 3",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Disjoint build"],
            "chunks": [
                {
                    "title": "Update Audio Module",
                    "target_files": ["tools/audio_sink.py"],
                    "instructions": "Update audio reconnect.",
                }
            ],
        }

        r1 = enqueue_build(plan_first, db_path=self.db_path, post_widget=False)
        r2 = enqueue_build(plan_overlap, db_path=self.db_path, post_widget=False, allow_overlap=True)
        r3 = enqueue_build(plan_disjoint, db_path=self.db_path, post_widget=False)

        b1_id = r1["build_id"]
        b2_id = r2["build_id"]
        b3_id = r3["build_id"]

        sup = forge_mod.ForgeSupervisor(db_path=self.db_path, max_parallel_builds=3, max_parallel_chunks=3)

        def _fake_popen(*args, **kwargs):
            mp = MagicMock()
            mp.poll.return_value = None
            return mp

        with patch("subprocess.Popen", side_effect=_fake_popen), patch("builtins.open", unittest.mock.mock_open()):
            spawned_1 = sup.maybe_spawn_next_build_worker()
            # Second call must skip b2_id (overlaps b1_id on tools/standings_page.py) and spawn disjoint b3_id
            spawned_2 = sup.maybe_spawn_next_build_worker()
            # Third call must return None because b2_id is still blocked by active b1_id
            spawned_3 = sup.maybe_spawn_next_build_worker()

        self.assertEqual(spawned_1, b1_id)
        self.assertEqual(spawned_2, b3_id)
        self.assertIsNone(spawned_3)

        # Now mark b1_id completed and remove its active worker proc -> b2_id becomes eligible and spawns
        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute("UPDATE forge_builds SET status = 'completed' WHERE id = ?", (b1_id,))
        sup._active_build_procs.pop(b1_id, None)

        with patch("subprocess.Popen", side_effect=_fake_popen), patch("builtins.open", unittest.mock.mock_open()):
            spawned_after = sup.maybe_spawn_next_build_worker()
        self.assertEqual(spawned_after, b2_id)

        # 2. Verify non-blocking watchdog terminates silent stdout hangs inside execute_chunk_in_worktree
        wt_repo = Path(self.tmp_dir.name) / "hang_repo"
        wt_repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(wt_repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(wt_repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Zero"], cwd=str(wt_repo), check=True, capture_output=True)
        (wt_repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(wt_repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt_repo), check=True, capture_output=True)

        hang_plan = {
            "title": "Silent Hang Watchdog Test",
            "repo": str(wt_repo),
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Watchdog test"],
            "chunks": [
                {
                    "title": "Hanging Chunk",
                    "repo": str(wt_repo),
                    "target_files": ["mod.py"],
                    "instructions": "Simulate a silent DB lock hang with zero stdout.",
                }
            ],
        }
        rh = enqueue_build(hang_plan, db_path=self.db_path, post_widget=False)
        hb_id = rh["build_id"]
        with forge_mod.get_db_connection(self.db_path) as conn:
            h_chunk = conn.execute("SELECT * FROM forge_chunks WHERE build_id = ?", (hb_id,)).fetchone()

        # Spawn a real silent sleeping subprocess in place of agy and set stall timeout to 0.25s
        real_popen = subprocess.Popen

        def _sleeping_agy_popen(cmd, *args, **kwargs):
            if isinstance(cmd, list) and any("agy" in str(x) for x in cmd):
                return real_popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    *args,
                    **kwargs,
                )
            return real_popen(cmd, *args, **kwargs)

        with patch.object(forge_mod, "CHUNK_SILENT_STALL_TIMEOUT_SECONDS", 0.25), patch.object(
            forge_mod, "post_or_update_build_widget", return_value=None
        ), patch.object(forge_mod, "sync_forge_channel_header", return_value=False), patch.object(
            forge_mod, "dispatch_build_failure_to_origin", return_value=True
        ), patch("subprocess.Popen", side_effect=_sleeping_agy_popen):
            t_start = time.time()
            ok_hang = sup.execute_chunk_in_worktree(hb_id, h_chunk)
            elapsed = time.time() - t_start

        self.assertFalse(ok_hang)
        self.assertLess(elapsed, 8.0)
        with forge_mod.get_db_connection(self.db_path) as conn:
            c_after = conn.execute("SELECT status, last_error FROM forge_chunks WHERE id = ?", (h_chunk["id"],)).fetchone()
            self.assertEqual(c_after["status"], "failed")
            self.assertIn("stalled silently", c_after["last_error"])


def test_no_synthesize_short_failed_probe_suppressed_when_turn_in_flight(tmp_path, monkeypatch):
    import tools.forge as forge
    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)
    monkeypatch.setattr(forge, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge, "_is_prod_forge_env", lambda *a, **kw: False)
    monkeypatch.setattr(forge, "_is_channel_turn_actively_in_flight", lambda ch: True)

    outbox_calls = []
    import tools.outbox as outbox
    monkeypatch.setattr(outbox, "queue_outbox_message", lambda **kw: outbox_calls.append(kw))

    task_id = "dt-test-nosynth-fail"
    tdir = forge._get_job_dir(task_id)
    tdir.mkdir(parents=True, exist_ok=True)
    log_p = tdir / "run.log"
    meta = {
        "task_id": task_id,
        "name": "Quick Probe",
        "command": "python3 -c 'import sys; sys.exit(1)'",
        "channel": "baseball",
        "timeout": 60,
        "cwd": str(tmp_path),
        "status": "starting",
        "start_time": time.time(),
        "notify_on_success": False,
        "synthesize": False,
        "log_file": str(log_p),
    }
    forge._write_job_meta(task_id, meta, db_path=db_p)
    forge._run_job_worker(task_id, db_path=db_p)

    assert len(outbox_calls) == 0
    assert "Suppressing raw outbox notification" in log_p.read_text(encoding="utf-8")


def test_no_synthesize_homelab_channel_resolution(tmp_path, monkeypatch):
    import tools.forge as forge
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(forge, "DATA_DIR", data_dir)
    in_flight_file = data_dir / "in_flight_turn.json"
    in_flight = {
        "1544955535722545253": {
            "channel_id": "1544955535722545253",
            "ts": time.time(),
            "pid": os.getpid(),
        }
    }
    in_flight_file.write_text(json.dumps(in_flight), encoding="utf-8")
    assert forge._is_channel_turn_actively_in_flight("homelab") is True
    assert forge._is_channel_turn_actively_in_flight("#homelab") is True
    assert forge._is_channel_turn_actively_in_flight("unknown_channel_xyz") is False


def test_no_synthesize_failed_probe_suppressed_duration_over_25s(tmp_path, monkeypatch):
    import tools.forge as forge
    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)
    monkeypatch.setattr(forge, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge, "_is_prod_forge_env", lambda *a, **kw: False)
    monkeypatch.setattr(forge, "_is_channel_turn_actively_in_flight", lambda ch: True)

    outbox_calls = []
    import tools.outbox as outbox
    monkeypatch.setattr(outbox, "queue_outbox_message", lambda **kw: outbox_calls.append(kw))

    task_id = "dt-test-nosynth-long-fail"
    tdir = forge._get_job_dir(task_id)
    tdir.mkdir(parents=True, exist_ok=True)
    log_p = tdir / "run.log"

    base_time = 1000.0
    call_idx = [0]

    def mock_time():
        call_idx[0] += 1
        if call_idx[0] >= 3:
            return base_time + 30.0
        return base_time

    monkeypatch.setattr(time, "time", mock_time)

    meta = {
        "task_id": task_id,
        "name": "Long Probe",
        "command": "python3 -c 'import sys; sys.exit(1)'",
        "channel": "baseball",
        "timeout": 60,
        "cwd": str(tmp_path),
        "status": "starting",
        "start_time": base_time,
        "notify_on_success": False,
        "synthesize": False,
        "log_file": str(log_p),
    }
    forge._write_job_meta(task_id, meta, db_path=db_p)
    forge._run_job_worker(task_id, db_path=db_p)

    assert len(outbox_calls) == 0
    assert "Suppressing raw outbox notification" in log_p.read_text(encoding="utf-8")


def test_no_synthesize_failed_probe_suppressed_when_turn_not_in_flight(tmp_path, monkeypatch):
    import tools.forge as forge
    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)
    monkeypatch.setattr(forge, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge, "_is_prod_forge_env", lambda *a, **kw: False)
    monkeypatch.setattr(forge, "_is_channel_turn_actively_in_flight", lambda ch: False)

    outbox_calls = []
    import tools.outbox as outbox
    monkeypatch.setattr(outbox, "queue_outbox_message", lambda **kw: outbox_calls.append(kw))

    task_id = "dt-test-nosynth-not-in-flight"
    tdir = forge._get_job_dir(task_id)
    tdir.mkdir(parents=True, exist_ok=True)
    log_p = tdir / "run.log"
    meta = {
        "task_id": task_id,
        "name": "Quiet Probe Off-Turn",
        "command": "python3 -c 'import sys; sys.exit(1)'",
        "channel": "baseball",
        "timeout": 60,
        "cwd": str(tmp_path),
        "status": "starting",
        "start_time": time.time(),
        "notify_on_success": False,
        "synthesize": False,
        "log_file": str(log_p),
    }
    forge._write_job_meta(task_id, meta, db_path=db_p)
    forge._run_job_worker(task_id, db_path=db_p)

    assert len(outbox_calls) == 0
    log_content = log_p.read_text(encoding="utf-8")
    assert "Suppressing raw outbox failure banner" in log_content


def test_no_synthesize_redirected_log_captured_in_run_log(tmp_path, monkeypatch):
    import tools.forge as forge
    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)
    monkeypatch.setattr(forge, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge, "_is_prod_forge_env", lambda *a, **kw: False)
    monkeypatch.setattr(forge, "_is_channel_turn_actively_in_flight", lambda ch: True)

    outbox_calls = []
    import tools.outbox as outbox
    monkeypatch.setattr(outbox, "queue_outbox_message", lambda **kw: outbox_calls.append(kw))

    task_id = "dt-test-redirected-log"
    tdir = forge._get_job_dir(task_id)
    tdir.mkdir(parents=True, exist_ok=True)
    log_p = tdir / "run.log"
    out_file = tmp_path / "redirected_output.txt"
    meta = {
        "task_id": task_id,
        "name": "Redirect Probe",
        "command": f"python3 -c 'print(\"CUSTOM_REDIRECTED_LINE_FORGE_PROBE\")' > {out_file} 2>&1",
        "channel": "baseball",
        "timeout": 60,
        "cwd": str(tmp_path),
        "status": "starting",
        "start_time": time.time(),
        "notify_on_success": False,
        "synthesize": False,
        "log_file": str(log_p),
    }
    forge._write_job_meta(task_id, meta, db_path=db_p)
    forge._run_job_worker(task_id, db_path=db_p)

    assert "CUSTOM_REDIRECTED_LINE_FORGE_PROBE" in log_p.read_text(encoding="utf-8")


def test_absolute_vs_relative_target_files_overlap_detection(tmp_path):
    import tools.forge as forge
    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)

    # Verify _normalize_repo_target_file collapses absolute, repo-prefixed, and relative paths
    repo = "/baseball/big-projections"
    assert forge._normalize_repo_target_file(repo, "/baseball/big-projections/build_public_standings_page.py") == "build_public_standings_page.py"
    assert forge._normalize_repo_target_file(repo, "baseball/big-projections/build_public_standings_page.py") == "build_public_standings_page.py"
    assert forge._normalize_repo_target_file(repo, "./build_public_standings_page.py") == "build_public_standings_page.py"
    assert forge._normalize_repo_target_file(repo, "build_public_standings_page.py") == "build_public_standings_page.py"

    fake_repo = tmp_path / "baseball" / "big-projections"
    (fake_repo / ".git").mkdir(parents=True, exist_ok=True)
    repo_str = str(fake_repo)

    plan_a = {
        "title": "Build A (Absolute Target Path)",
        "repo": repo_str,
        "origin_channel_id": forge.BASEBALL_CHANNEL_ID,
        "context_fact_sheet": ["Test overlap normalization."],
        "chunks": [
            {
                "title": "Chunk A",
                "target_files": [f"{repo_str}/build_public_standings_page.py"],
                "instructions": "Update standings page A with full unit test coverage.",
                "verification_cmd": "pytest",
            }
        ],
    }
    res_a = forge.enqueue_build(plan_a, db_path=db_p, post_widget=False)
    assert res_a["ok"]

    plan_b = {
        "title": "Build B (Relative Target Path)",
        "repo": repo_str,
        "origin_channel_id": forge.BASEBALL_CHANNEL_ID,
        "context_fact_sheet": ["Test overlap normalization."],
        "chunks": [
            {
                "title": "Chunk B",
                "target_files": ["build_public_standings_page.py"],
                "instructions": "Update standings page B with full unit test coverage.",
                "verification_cmd": "pytest",
            }
        ],
    }
    overlaps = forge.check_target_file_overlap(plan_b, db_path=db_p)
    assert len(overlaps) == 1
    assert overlaps[0]["file"] == "build_public_standings_page.py"
    assert overlaps[0]["build_id"] == res_a["build_id"]

    # Enqueue with allow_overlap=True and verify _build_overlaps_earlier_builds also catches it
    res_b = forge.enqueue_build(plan_b, db_path=db_p, post_widget=False, allow_overlap=True)
    assert forge._build_overlaps_earlier_builds(res_b["build_id"], [res_a["build_id"]], db_path=db_p) is True


def test_execute_chunk_in_worktree_conflict_with_advanced_main_aborts_cleanly(tmp_path, monkeypatch):
    import subprocess
    import tools.forge as forge

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)
    monkeypatch.setattr(forge, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "dispatch_build_completion_to_origin", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "dispatch_build_failure_to_origin", lambda *a, **kw: None)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Ivy"], cwd=str(repo_dir), check=True, capture_output=True)
    target_f = repo_dir / "standings.py"
    target_f.write_text("LINE = 'initial'\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    plan = {
        "title": "Test Conflict Handling",
        "repo": str(repo_dir),
        "origin_channel_id": forge.BASEBALL_CHANNEL_ID,
        "context_fact_sheet": ["Test conflict handling."],
        "chunks": [
            {
                "title": "Modify standings.py",
                "target_files": ["standings.py"],
                "instructions": "Update LINE constant in standings.py and verify import.",
                "verification_cmd": "python3 -c 'import standings'",
            }
        ],
    }
    res = forge.enqueue_build(plan, db_path=db_p, post_widget=False)
    build_id = res["build_id"]

    # FakePopen simulates Vulcan editing standings.py inside wt_dir while main in repo_dir advances with a conflicting commit on attempt 1,
    # then resolving cleanly on attempt 2 once wt_dir is recreated on top of latest main.
    attempt_counter = [0]

    class FakePopen:
        def __init__(self, cmd, cwd=None, **kwargs):
            self.cwd = Path(cwd)
            attempt_counter[0] += 1
            if attempt_counter[0] == 1:
                # Advance main in repo_dir concurrently with a conflicting line change
                (repo_dir / "standings.py").write_text("LINE = 'main_advanced'\n", encoding="utf-8")
                subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "concurrent main advance"], cwd=str(repo_dir), check=True, capture_output=True)
                # Vulcan writes its change on top of old base
                (self.cwd / "standings.py").write_text("LINE = 'chunk_attempt_1'\n", encoding="utf-8")
            else:
                # On attempt 2, wt_dir was recreated on top of latest main ('main_advanced')
                assert (self.cwd / "standings.py").read_text(encoding="utf-8") == "LINE = 'main_advanced'\n"
                (self.cwd / "standings.py").write_text("LINE = 'chunk_attempt_2_resolved'\n", encoding="utf-8")
            self.stdout = iter([])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    real_popen = subprocess.Popen

    def side_effect_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd):
            return FakePopen(cmd, *args, **kwargs)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", side_effect_popen)

    sup = forge.ForgeSupervisor(db_path=db_p)
    with forge.get_db_connection(db_p) as conn:
        chunk_row = conn.execute("SELECT * FROM forge_chunks WHERE build_id = ?", (build_id,)).fetchone()

    ok = sup.execute_chunk_in_worktree(build_id, chunk_row)
    assert ok is True
    assert attempt_counter[0] == 2
    assert (repo_dir / "standings.py").read_text(encoding="utf-8") == "LINE = 'chunk_attempt_2_resolved'\n"
    st = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_dir), capture_output=True, text=True).stdout.strip()
    assert st == ""


def test_uppercase_status_and_stuck_running_embed_sweep(tmp_path, monkeypatch):
    import tools.forge as forge

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)

    plan = {
        "title": "Epic 43 Public Standings",
        "repo": "/workspace",
        "origin_channel_id": forge.BASEBALL_CHANNEL_ID,
        "context_fact_sheet": ["Test uppercase status handling."],
        "chunks": [
            {
                "title": "Chunk 1",
                "target_files": ["tools/standings.py"],
                "instructions": "Update standings page.",
                "verification_cmd": "pytest",
            }
        ],
    }
    res = forge.enqueue_build(plan, db_path=db_p, post_widget=False)
    build_id = res["build_id"]
    old_ts = time.time() - 7500.0

    with forge.get_db_connection(db_p) as conn:
        conn.execute(
            "UPDATE forge_builds SET status = 'COMPLETED', forge_message_id = 1564468993128009871, updated_at = ? WHERE id = ?",
            (old_ts, build_id),
        )
        conn.execute(
            "UPDATE forge_chunks SET status = 'MERGED' WHERE build_id = ?",
            (build_id,),
        )
        conn.execute(
            """
            INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, forge_message_id)
            VALUES (?, 'Uppercase Job', 'python3 job.py', 'baseball', '/workspace', 60, 'COMPLETED', ?, ?, 1564468993128009872)
            """,
            ("dt-upper-job", old_ts, old_ts),
        )

    embed = forge.render_build_widget_embed(build_id, db_path=db_p)
    assert embed["title"].startswith("✅")
    assert "**COMPLETED**" in embed["description"]
    assert "**1/1** chunks" in embed["description"]
    assert "Auto-clears in 1h" in embed["footer"]["text"]

    pruned = forge.prune_completed_build_widgets(ttl_seconds=3600.0, db_path=db_p, delete_remote=False)
    assert build_id in pruned
    assert "dt-upper-job" in pruned

    # Also test _sweep_stale_forge_channel_embeds when card is stuck showing 🔨 RUNNING in Discord
    # (a) for a build whose DB row is COMPLETED, and (b) for an orphaned build missing from DB and >2h old
    iso_old = "2026-10-27T01:00:00+00:00"
    fake_messages = [
        {
            "id": "1564468993128009871",
            "timestamp": iso_old,
            "embeds": [
                {
                    "title": "🔨 Epic 43 Public Standings",
                    "description": "**RUNNING** · `Ivy-Agent` · <#1548196929308065893> · **0/1** chunks",
                    "footer": {"text": f"{build_id} · Updated Oct 26"},
                }
            ],
        },
        {
            "id": "1564468993128009899",
            "timestamp": iso_old,
            "embeds": [
                {
                    "title": "🔨 Orphaned Missing Build",
                    "description": "**RUNNING** · `Ivy-Agent` · <#1548196929308065893> · **0/1** chunks",
                    "footer": {"text": "fg-missing-from-db-999 · Updated Oct 26"},
                }
            ],
        },
    ]

    deleted_urls = []

    class FakeResp:
        def __init__(self, status_code, data=None):
            self.status_code = status_code
            self._data = data

        def json(self):
            return self._data

    monkeypatch.setattr(forge.requests, "get", lambda url, **kw: FakeResp(200, fake_messages))
    monkeypatch.setattr(forge.requests, "delete", lambda url, **kw: (deleted_urls.append(url), FakeResp(204))[1])

    swept = forge._sweep_stale_forge_channel_embeds(
        cutoff=time.time() - 3600.0,
        forge_channel_id=forge.FORGE_CHANNEL_ID,
        token="fake-token",
        db_paths=[db_p],
    )
    assert build_id in swept
    assert "fg-missing-from-db-999" in swept
    assert len(deleted_urls) == 2


def test_short_job_suppresses_forge_widget_until_90s(tmp_path, monkeypatch):
    import tools.forge as forge

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)
    monkeypatch.setattr(forge, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge, "_is_prod_forge_env", lambda *a, **kw: True)
    monkeypatch.setattr(forge, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "prune_completed_job_widgets", lambda *a, **kw: [])
    monkeypatch.setattr(forge, "_is_channel_turn_actively_in_flight", lambda ch: True)

    widget_calls = []
    monkeypatch.setattr(forge, "post_or_update_job_widget", lambda tid, **kw: widget_calls.append(tid))

    # 1. Short job (<90s) that fails should NOT post a #forge widget card
    short_id = "dt-test-short-no-widget"
    tdir = forge._get_job_dir(short_id)
    tdir.mkdir(parents=True, exist_ok=True)
    log_p = tdir / "run.log"
    meta_short = {
        "task_id": short_id,
        "name": "Short 20s Probe",
        "command": "python3 -c 'import sys; sys.exit(1)'",
        "channel": "baseball",
        "timeout": 60,
        "cwd": str(tmp_path),
        "status": "starting",
        "start_time": time.time(),
        "notify_on_success": False,
        "synthesize": False,
        "log_file": str(log_p),
    }
    forge._write_job_meta(short_id, meta_short, db_path=db_p)
    forge._run_job_worker(short_id, db_path=db_p)
    assert widget_calls == []

    # 2. Long job (>=90s, non-quiet) SHOULD post a #forge widget card
    long_id = "dt-test-long-widget"
    tdir_long = forge._get_job_dir(long_id)
    tdir_long.mkdir(parents=True, exist_ok=True)
    log_long = tdir_long / "run.log"
    monkeypatch.setattr(forge, "JOB_WIDGET_MIN_DURATION_SECONDS", 0.0)
    meta_long = {
        "task_id": long_id,
        "name": "Long Pipeline Job",
        "command": "python3 -c 'print(123)'",
        "channel": "baseball",
        "timeout": 60,
        "cwd": str(tmp_path),
        "status": "starting",
        "start_time": time.time(),
        "notify_on_success": False,
        "synthesize": True,
        "log_file": str(log_long),
    }
    forge._write_job_meta(long_id, meta_long, db_path=db_p)
    forge._run_job_worker(long_id, db_path=db_p)
    assert long_id in widget_calls


if __name__ == "__main__":
    unittest.main()
