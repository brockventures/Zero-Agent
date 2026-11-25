# ruff: noqa
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
    build_default_verification_cmd,
    check_runaway_tool_loop,
    check_target_file_overlap,
    detect_repo_verification_script,
    dismiss_decision,
    dispatch_build_completion_to_origin,
    enqueue_build,
    evaluate_plan_approval_gate,
    execute_decision,
    expire_stale_decisions,
    extract_symbol_snippet,
    format_chunk_prompt,
    format_decision_card_text,
    format_origin_completion_envelope,
    format_queue_digest,
    get_job_status,
    init_forge_db,
    list_jobs,
    post_or_update_job_widget,
    prune_completed_build_widgets,
    prune_completed_job_widgets,
    render_build_widget_embed,
    render_decision_card_embed,
    render_job_widget_embed,
    render_build_widget_text,
    stage_decision,
    stage_plan_approval_decision,
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

        # Verify completed chunk and completed build card duration rendering, plus 1h TTL auto-delete
        now_done = time.time()
        with forge_mod.get_db_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE forge_chunks SET status = 'merged', started_at = ?, completed_at = ?, diff_stat = '1 file changed, 18 insertions(+), 2 deletions(-)' WHERE build_id = ? AND chunk_index = 0",
                (now_done - 3825.0, now_done - 3750.0, build_id),
            )
            conn.execute(
                "UPDATE forge_chunks SET status = 'merged', started_at = ?, completed_at = ?, diff_stat = '1 file changed, 10 insertions(+)' WHERE build_id = ? AND chunk_index = 1",
                (now_done - 3750.0, now_done - 3700.0, build_id),
            )
            conn.execute(
                "UPDATE forge_builds SET status = 'completed', forge_message_id = 123456789, updated_at = ? WHERE id = ?",
                (now_done - 3700.0, build_id),
            )
        embed_completed = render_build_widget_embed(build_id, db_path=self.db_path)
        self.assertIn("· ⏱️ 2m 5s", embed_completed["description"].splitlines()[0])
        self.assertIn("· ⏱️ 1m 15s", embed_completed["description"])
        self.assertIn("· ⏱️ 50s", embed_completed["description"])
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

        with patch("tools.outbox.queue_outbox_message") as mock_outbox, patch.object(
            forge_mod, "_post_decision_card_rest", return_value=None
        ):
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
        self.assertFalse(d_ops["redirected_to_zero_chat"])
        self.assertIsNone(d_ops["origin_channel_id"])
        self.assertIsNone(d_ops["routing_note"])
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

        d_unposted = stage_decision(
            category="bridge_reload",
            title="Reload Bridge (fg-test)",
            summary="Reload bridge in-place.",
            handler_type="bridge_reload",
            payload={"build_id": "fg-test"},
            channel_id=ZERO_CHAT_CHANNEL_ID,
            db_path=self.db_path,
            post_to_discord=False,
        )
        with patch.object(forge_mod, "_post_decision_card_rest", return_value=987654321) as mock_post:
            posted_ids = forge_mod.post_pending_decision_cards(
                channel_id=ZERO_CHAT_CHANNEL_ID,
                db_path=self.db_path,
            )
            self.assertIn(d_unposted["decision_id"], posted_ids)
            mock_post.assert_called_once()

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

    def test_04b_repo_verify_script_support(self):
        wt_dir = Path(self.tmp_dir.name) / "wt_repo_verify"
        wt_dir.mkdir(parents=True, exist_ok=True)

        # 1. detect_repo_verification_script on empty / non-repo directory
        self.assertIsNone(detect_repo_verification_script(None))
        self.assertIsNone(detect_repo_verification_script(wt_dir))

        # Add scripts/verify.sh
        scripts_dir = wt_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        verify_script = scripts_dir / "verify.sh"
        verify_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        # Default detection without FORGE_FULL_REPO_VERIFY
        self.assertEqual(detect_repo_verification_script(wt_dir), "sh scripts/verify.sh --staged")

        # Detection with FORGE_FULL_REPO_VERIFY=1
        with patch.dict(os.environ, {"FORGE_FULL_REPO_VERIFY": "1"}):
            self.assertEqual(detect_repo_verification_script(wt_dir), "sh scripts/verify.sh")

        # 2. build_default_verification_cmd with verify.sh present
        cmd_no_py = build_default_verification_cmd(["main.go"], worktree_dir=wt_dir)
        self.assertEqual(cmd_no_py, "sh scripts/verify.sh --staged")

        cmd_with_py = build_default_verification_cmd(["tools/mod.py"], worktree_dir=wt_dir)
        self.assertEqual(
            cmd_with_py,
            'python3 -m py_compile "tools/mod.py" && sh scripts/verify.sh --staged',
        )

        with patch.dict(os.environ, {"FORGE_FULL_REPO_VERIFY": "1"}):
            cmd_full = build_default_verification_cmd(["tools/mod.py"], worktree_dir=wt_dir)
            self.assertEqual(
                cmd_full,
                'python3 -m py_compile "tools/mod.py" && sh scripts/verify.sh',
            )

        # 3. verify_chunk_worktree maker-checker gate integration
        subprocess.run(["git", "init", "-b", "main"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Zero"], cwd=str(wt_dir), check=True, capture_output=True)

        marker = wt_dir / "verify_marker.txt"
        verify_script.write_text(f"#!/bin/sh\necho 'verify ran' > '{marker}'\nexit 0\n", encoding="utf-8")

        (wt_dir / "allowed.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt_dir), check=True, capture_output=True)

        # Omitted verification_cmd: invokes scripts/verify.sh automatically via default cmd
        (wt_dir / "allowed.py").write_text("x = 2\n", encoding="utf-8")
        passed, diff_stat, err = verify_chunk_worktree(
            wt_dir,
            "",
            target_files=["allowed.py"],
            require_changes=True,
        )
        self.assertTrue(passed, err)
        self.assertTrue(marker.exists())
        self.assertIn("1 file changed", diff_stat)

        # Custom verification_cmd without scripts/verify.sh: still automatically invokes scripts/verify.sh
        marker.unlink()
        (wt_dir / "allowed.py").write_text("x = 3\n", encoding="utf-8")
        passed, _, err = verify_chunk_worktree(
            wt_dir,
            "echo custom_gate_ok",
            target_files=["allowed.py"],
            require_changes=True,
        )
        self.assertTrue(passed, err)
        self.assertTrue(marker.exists())

        # Failing verify.sh blocks verification gate
        verify_script.write_text("#!/bin/sh\necho 'lint ratchet violation' >&2\nexit 1\n", encoding="utf-8")
        (wt_dir / "allowed.py").write_text("x = 4\n", encoding="utf-8")
        passed, _, err = verify_chunk_worktree(
            wt_dir,
            "",
            target_files=["allowed.py"],
            require_changes=True,
        )
        self.assertFalse(passed)
        self.assertIn("Verification command failed", err)
        self.assertIn("lint ratchet violation", err)

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
        self.assertTrue((wt_bb / ".agents" / "hooks" / "batch_guard.py").exists())
        self.assertTrue((wt_bb / ".agents" / "hooks" / "search_guard.py").exists())
        if Path("/baseball/data/vulcan_baseball_rules/big_projection_engine_invariants.md").exists():
            self.assertTrue((wt_bb / ".agents" / "rules" / "big_projection_engine_invariants.md").exists())

        forge_mod.restore_vulcan_worktree_profile(wt_bb, snap_bb)
        self.assertFalse((wt_bb / "GEMINI.md").exists())
        self.assertFalse((wt_bb / ".agents").exists())
        st_bb = subprocess.run(["git", "status", "--porcelain"], cwd=str(wt_bb), capture_output=True, text=True)
        self.assertEqual(st_bb.stdout.strip(), "")

        # 3. Test external repo with repo-native AGENTS.md and custom .agents/rules/
        wt_ext = Path(self.tmp_dir.name) / "wt_ext"
        (wt_ext / ".agents" / "rules").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(wt_ext), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(wt_ext), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Mirrormere"], cwd=str(wt_ext), check=True, capture_output=True)

        ext_agents = "# Mirrormere Agent Guidelines\nRun ./scripts/verify.sh --staged\n"
        (wt_ext / "AGENTS.md").write_text(ext_agents, encoding="utf-8")
        (wt_ext / ".agents" / "rules" / "custom_repo_rule.md").write_text("# Custom Repo Rule\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(wt_ext), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init ext"], cwd=str(wt_ext), check=True, capture_output=True)

        snap_ext = forge_mod.stage_vulcan_worktree_profile(
            wt_ext,
            repo_dir=wt_ext,
            target_files=["internal/app.go"],
        )
        self.assertFalse(snap_ext["is_baseball"])
        staged_gemini = (wt_ext / "GEMINI.md").read_text(encoding="utf-8")
        staged_agents = (wt_ext / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Vulcan", staged_gemini)
        self.assertIn("Run ./scripts/verify.sh --staged", staged_gemini)
        self.assertIn("# Repository-Specific Instructions (Preserved from Repo AGENTS.md)", staged_gemini)
        self.assertIn("Vulcan", staged_agents)
        self.assertIn("Run ./scripts/verify.sh --staged", staged_agents)
        self.assertTrue((wt_ext / ".agents" / "rules" / "custom_repo_rule.md").exists())
        for core_rule in forge_mod.VULCAN_CORE_RULES:
            self.assertTrue((wt_ext / ".agents" / "rules" / core_rule).exists())

        forge_mod.restore_vulcan_worktree_profile(wt_ext, snap_ext)
        self.assertEqual((wt_ext / "AGENTS.md").read_text(encoding="utf-8"), ext_agents)
        self.assertFalse((wt_ext / "GEMINI.md").exists())
        self.assertFalse((wt_ext / "agents.md").exists())
        self.assertTrue((wt_ext / ".agents" / "rules" / "custom_repo_rule.md").exists())
        for core_rule in forge_mod.VULCAN_CORE_RULES:
            self.assertFalse((wt_ext / ".agents" / "rules" / core_rule).exists())
        st_ext = subprocess.run(["git", "status", "--porcelain"], cwd=str(wt_ext), capture_output=True, text=True)
        self.assertEqual(st_ext.stdout.strip(), "")

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
        nonexistent_ifp = Path(self.tmp_dir.name) / "nonexistent_in_flight.json"
        res_fast = enqueue_build(
            fast_plan,
            db_path=self.db_path,
            post_widget=False,
            check_approval_gate=True,
            batch_state_path="/tmp/nonexistent_batch_state.json",
            in_flight_path=nonexistent_ifp,
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
            in_flight_path=nonexistent_ifp,
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

        # 3. Wide blast radius (>=6 production files) or core bridge or >180s (>=4 scratch runs) calibration also triggers approval gate
        wide_plan = {
            "title": "Six File Refactor",
            "repo": "/workspace",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["6 files"],
            "chunks": [
                {
                    "title": "Wide Chunk",
                    "target_files": [
                        "tools/w1.py",
                        "tools/w2.py",
                        "tools/w3.py",
                        "tools/w4.py",
                        "tools/w5.py",
                        "tools/w6.py",
                    ],
                    "instructions": "Refactor 6 files.",
                }
            ],
        }
        nonexistent_ifp = Path(self.tmp_dir.name) / "nonexistent_in_flight.json"
        req_wide, reasons_wide = forge_mod.evaluate_plan_approval_gate(
            wide_plan,
            batch_state_path="/tmp/nonexistent.json",
            in_flight_path=nonexistent_ifp,
            db_path=self.db_path,
        )
        self.assertTrue(req_wide)
        self.assertTrue(any("6 target files" in r for r in reasons_wide))

        bs_file = Path(self.tmp_dir.name) / "calib_state.json"
        now_ts = time.time()
        bs_file.write_text(
            json.dumps({"last_time": now_ts, "turn_start_time": now_ts - 200.0, "total_scratch_runs": 4}),
            encoding="utf-8",
        )
        req_cal, reasons_cal = forge_mod.evaluate_plan_approval_gate(
            fast_plan,
            batch_state_path=bs_file,
            in_flight_path=nonexistent_ifp,
            db_path=self.db_path,
        )
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
                "verification_cmd": "true",
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
                "verification_cmd": "true",
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

    # FakePopen simulates Vulcan editing standings.py inside wt_dir while main in repo_dir advances with a conflicting commit on attempt 1.
    # FORGE must abort the conflicting cherry-pick cleanly WITHOUT overwriting main_advanced, then recreate the worktree from main_advanced on attempt 2.
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
                "verification_cmd": 'pytest "tools/test_forge.py"',
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

    assert forge.JOB_WIDGET_MIN_DURATION_SECONDS == 0.0

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)
    monkeypatch.setattr(forge, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge, "_is_prod_forge_env", lambda *a, **kw: True)
    monkeypatch.setattr(forge, "sync_forge_channel_header", lambda *a, **kw: None)
    prune_calls = []
    monkeypatch.setattr(forge, "prune_completed_job_widgets", lambda *a, **kw: prune_calls.append(True) or [])
    monkeypatch.setattr(forge, "_is_channel_turn_actively_in_flight", lambda ch: True)

    widget_calls = []
    monkeypatch.setattr(forge, "post_or_update_job_widget", lambda tid, **kw: widget_calls.append(tid))

    # 1. Quiet probe (notify_on_success=False, synthesize=False) should NOT post a #forge widget card
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

    # 2. Non-quiet job SHOULD post a #forge widget card immediately on start and on completion, and prune completed widgets
    long_id = "dt-test-long-widget"
    tdir_long = forge._get_job_dir(long_id)
    tdir_long.mkdir(parents=True, exist_ok=True)
    log_long = tdir_long / "run.log"
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
    assert widget_calls.count(long_id) >= 2
    assert len(prune_calls) >= 1


def test_normalize_file_list_preserves_leading_dot_on_hidden_paths():
    import tools.forge as forge

    raw_items = [
        "./.agents/hooks/batch_guard.py",
        ".agents/rules/no_server_wide_search.md",
        "./tools/forge.py",
        {"file": "./.agents/hooks.json"},
        {"path": ".agents/skills/SKILL.md"},
    ]
    norm = forge._normalize_file_list(raw_items)
    assert norm == [
        ".agents/hooks/batch_guard.py",
        ".agents/rules/no_server_wide_search.md",
        "tools/forge.py",
        ".agents/hooks.json",
        ".agents/skills/SKILL.md",
    ]

    # Also verify _normalize_repo_target_file preserves leading dot
    assert (
        forge._normalize_repo_target_file("/workspace", "./.agents/hooks/batch_guard.py")
        == ".agents/hooks/batch_guard.py"
    )
    assert (
        forge._normalize_repo_target_file("/workspace", "/workspace/.agents/hooks/batch_guard.py")
        == ".agents/hooks/batch_guard.py"
    )
    assert (
        forge._normalize_repo_target_file("/workspace", ".agents/hooks/batch_guard.py")
        == ".agents/hooks/batch_guard.py"
    )


def test_consolidate_legacy_forge_db_migrates_rows_and_symlinks(tmp_path):
    import sqlite3
    import tools.forge as forge

    pri = tmp_path / "baseball" / "data" / "forge.db"
    leg = tmp_path / "workspace" / "data" / "forge.db"
    pri.parent.mkdir(parents=True)
    leg.parent.mkdir(parents=True)

    forge.init_forge_db(pri)
    forge.init_forge_db(leg)

    now = time.time()
    with sqlite3.connect(str(leg)) as conn:
        conn.execute(
            "INSERT INTO forge_builds (id, title, origin_channel_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("b-legacy", "Legacy Title", 100, now, now),
        )
        conn.execute(
            "INSERT INTO forge_chunks (id, build_id, chunk_index, title, instructions) VALUES (?, ?, ?, ?, ?)",
            ("c-legacy", "b-legacy", 0, "Legacy Chunk", "Do work"),
        )
        conn.execute(
            "INSERT INTO forge_jobs (id, name, command, start_time) VALUES (?, ?, ?, ?)",
            ("j-legacy", "Legacy Job", "echo test", now),
        )
        conn.execute(
            "INSERT INTO forge_decisions (id, category, title, summary, handler_type, payload_json, channel_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("d-legacy", "cat", "Legacy Decision", "summary", "handler", "{}", 100, now + 100, now),
        )
        conn.commit()

    # Pre-seed primary with an existing build to verify INSERT OR IGNORE behavior
    with sqlite3.connect(str(pri)) as conn:
        conn.execute(
            "INSERT INTO forge_builds (id, title, origin_channel_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("b-legacy", "Primary Existing Title", 100, now, now),
        )
        conn.commit()

    res = forge.consolidate_legacy_forge_db(primary_db_path=pri, legacy_db_path=leg)
    assert res["symlinked"] is True
    assert leg.is_symlink()
    assert leg.resolve() == pri.resolve()

    with sqlite3.connect(str(pri)) as conn:
        b = conn.execute("SELECT id, title FROM forge_builds WHERE id = 'b-legacy' LIMIT 1").fetchone()
        assert b[1] == "Primary Existing Title"  # Preserved original primary title via INSERT OR IGNORE
        c = conn.execute("SELECT id, title FROM forge_chunks WHERE id = 'c-legacy' LIMIT 1").fetchone()
        assert c is not None
        assert c[1] == "Legacy Chunk"
        j = conn.execute("SELECT id, name FROM forge_jobs WHERE id = 'j-legacy' LIMIT 1").fetchone()
        assert j is not None
        assert j[1] == "Legacy Job"
        d = conn.execute("SELECT id, title FROM forge_decisions WHERE id = 'd-legacy' LIMIT 1").fetchone()
        assert d is not None
        assert d[1] == "Legacy Decision"

    # Idempotent call on already-symlinked path
    res2 = forge.consolidate_legacy_forge_db(primary_db_path=pri, legacy_db_path=leg)
    assert res2["symlinked"] is False
    assert res2["status"] == "already_symlink"


def test_execute_chunk_in_worktree_persists_build_log(tmp_path, monkeypatch):
    import subprocess
    import tools.forge as forge

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)

    logs_dir = tmp_path / "forge_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(forge, "FORGE_LOGS_DIR", logs_dir)

    monkeypatch.setattr(forge, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "dispatch_build_completion_to_origin", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "dispatch_build_failure_to_origin", lambda *a, **kw: None)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Vulcan"], cwd=str(repo_dir), check=True, capture_output=True)
    target_f = repo_dir / "math_util.py"
    target_f.write_text("VAL = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    plan = {
        "title": "Log Persistence Test",
        "repo": str(repo_dir),
        "origin_channel_id": forge.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Persist non-empty build telemetry."],
        "chunks": [
            {
                "title": "Update math_util.py",
                "target_files": ["math_util.py"],
                "instructions": "Update VAL = 42 in math_util.py and verify with assertion.",
                "verification_cmd": "python3 -c 'import math_util; assert math_util.VAL == 42'",
            }
        ],
    }
    res = forge.enqueue_build(plan, db_path=db_p, post_widget=False)
    build_id = res["build_id"]

    class FakeAgProc:
        def __init__(self, cmd, cwd=None, **kwargs):
            self.cwd = Path(cwd)
            (self.cwd / "math_util.py").write_text("VAL = 42\n", encoding="utf-8")
            self.stdout = iter(["Vulcan step 1: edit math_util.py\n", "Vulcan step 2: verification ready\n"])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    real_popen = subprocess.Popen

    def fake_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd):
            return FakeAgProc(cmd, *args, **kwargs)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    sup = forge.ForgeSupervisor(db_path=db_p)
    with forge.get_db_connection(db_p) as conn:
        chunk_row = conn.execute(
            "SELECT id, chunk_index, build_id, title, repo, instructions, verification_cmd, "
            "prohibited_files_json, target_files_json, worker_model FROM forge_chunks WHERE build_id = ? LIMIT 1",
            (build_id,),
        ).fetchone()

    ok = sup.execute_chunk_in_worktree(build_id, chunk_row)
    assert ok is True

    log_path = logs_dir / f"build-{build_id}.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert len(content) > 0
    assert "Attempt 1" in content
    assert "verify_chunk_worktree result: PASSED" in content
    assert "Worker finished with exit code 0" in content


def test_chunk_split_gate_and_worktree_resume_salvage(tmp_path, monkeypatch):
    """Verify Chunk-Split Gate in validate_forge_plan and worktree preservation/salvage on worker timeout."""
    import tools.forge as forge
    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    logs_dir = tmp_path / "forge_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(forge, "FORGE_LOGS_DIR", logs_dir)
    monkeypatch.setattr(forge, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "dispatch_build_completion_to_origin", lambda *a, **kw: None)
    monkeypatch.setattr(forge, "dispatch_build_failure_to_origin", lambda *a, **kw: None)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Vulcan"], cwd=str(repo_dir), check=True, capture_output=True)
    (repo_dir / "mod_a.py").write_text("A = 1\n", encoding="utf-8")
    (repo_dir / "mod_b.py").write_text("B = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    # 1. Single-chunk plan with > 3 target_files is rejected
    over_files_plan = {
        "title": "Mega File Bundle",
        "repo": str(repo_dir),
        "origin_channel_id": forge.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Enforce atomic chunks"],
        "chunks": [
            {
                "title": "All Files in One",
                "target_files": ["a.py", "b.py", "c.py", "d.py"],
                "instructions": "Update all four files in a single chunk.",
            }
        ],
    }
    ok_f, msg_f = forge.validate_forge_plan(over_files_plan)
    assert not ok_f
    assert "Chunk-Split Gate" in msg_f
    assert "4 target_files" in msg_f

    # 2. Chunk with > 4000 chars of instructions is rejected
    over_chars_plan = {
        "title": "Mega Prose Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Enforce atomic chunks"],
        "chunks": [
            {
                "title": "Giant Instruction Block",
                "target_files": ["mod_a.py"],
                "instructions": "Implement feature: " + ("x" * 4050),
            }
        ],
    }
    ok_c, msg_c = forge.validate_forge_plan(over_chars_plan)
    assert not ok_c
    assert "Chunk-Split Gate" in msg_c
    assert "4000 chars" in msg_c

    # 3. Chunk packing 7+ numbered tasks is rejected (> 6 max)
    over_tasks_plan = {
        "title": "Seven Tasks in One Chunk",
        "repo": str(repo_dir),
        "origin_channel_id": forge.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Enforce atomic chunks"],
        "chunks": [
            {
                "title": "Everything at Once",
                "target_files": ["mod_a.py", "mod_b.py"],
                "instructions": (
                    "1. Consolidate DB tables.\n"
                    "2. Upsert 2027 contracts.\n"
                    "3. Cap backup catchers.\n"
                    "4. Update README and rebuild standings.\n"
                    "5. Run migration tests.\n"
                    "6. Clean temporary tables.\n"
                    "7. Verify final stats.\n"
                ),
            }
        ],
    }
    ok_t, msg_t = forge.validate_forge_plan(over_tasks_plan)
    assert not ok_t
    assert "Chunk-Split Gate" in msg_t
    assert "7 numbered tasks" in msg_t

    # 4. Worktree Resume across timeout retry: Attempt 1 writes mod_a.py then times out;
    # Attempt 2 sees the Worktree Resume State prompt with mod_a.py already modified and finishes mod_b.py
    valid_plan = {
        "title": "Timeout Resume Test",
        "repo": str(repo_dir),
        "origin_channel_id": forge.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Preserve worktree across timeout"],
        "chunks": [
            {
                "title": "Update mod_a and mod_b",
                "target_files": ["mod_a.py", "mod_b.py"],
                "instructions": "Set A = 10 in mod_a.py and B = 20 in mod_b.py.",
                "verification_cmd": "python3 -c 'import mod_a, mod_b; assert mod_a.A == 10 and mod_b.B == 20'",
            }
        ],
    }
    res = forge.enqueue_build(valid_plan, db_path=db_p, post_widget=False)
    build_id = res["build_id"]
    monkeypatch.setattr(forge, "CHUNK_WORKER_TIMEOUT_SECONDS", 0.3)

    prompts_seen: list[str] = []
    attempt_counter = [0]

    class TimeoutThenResumeProc:
        def __init__(self, cmd, cwd=None, **kwargs):
            attempt_counter[0] += 1
            self.att = attempt_counter[0]
            self.cwd = Path(cwd)
            self.pid = 99999
            for arg in cmd:
                if str(arg).startswith("-p="):
                    prompts_seen.append(str(arg)[3:])
            if self.att == 1:
                # Partial progress before hitting timeout
                (self.cwd / "mod_a.py").write_text("A = 10\n", encoding="utf-8")
            else:
                # Assert mod_a.py from Attempt 1 was preserved in the worktree!
                assert (self.cwd / "mod_a.py").read_text(encoding="utf-8") == "A = 10\n"
                (self.cwd / "mod_b.py").write_text("B = 20\n", encoding="utf-8")

            def _gen():
                yield f"Vulcan attempt {self.att} step 1\n"
                if self.att == 1:
                    time.sleep(0.55)
                yield f"Vulcan attempt {self.att} done\n"

            self.stdout = _gen()

        def poll(self):
            return None if self.att == 1 else 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    real_popen = subprocess.Popen

    def fake_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd):
            return TimeoutThenResumeProc(cmd, *args, **kwargs)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    sup = forge.ForgeSupervisor(db_path=db_p)
    with forge.get_db_connection(db_p) as conn:
        chunk_row = conn.execute("SELECT * FROM forge_chunks WHERE build_id = ? LIMIT 1", (build_id,)).fetchone()

    ok = sup.execute_chunk_in_worktree(build_id, chunk_row)
    assert ok is True
    assert attempt_counter[0] == 2
    assert len(prompts_seen) == 2
    assert "Worktree Resume State (Preserved From Previous Attempt)" in prompts_seen[1]
    assert "mod_a.py" in prompts_seen[1]

    # 5. Verify explicit line-range hydration, instruction line-reference auto-hydration, and fact_sheet deduplication
    big_lines = ["def giant_allocator():"] + [f"    step_{i} = {i}" for i in range(1, 150)]
    (repo_dir / "giant_mod.py").write_text("\n".join(big_lines) + "\n", encoding="utf-8")
    dup_spec = (
        "In giant_mod.py (around L120-L130), update step_120 and step_125 inside giant_allocator so that "
        "part-time catchers are capped at 0.25 share while primary catchers retain full playing time allocation "
        "across all 30 MLB depth charts and verify zero-sum win closure."
    )
    line_plan = {
        "title": "Line Range Auto-Hydration & Fact Dedup Test",
        "repo": str(repo_dir),
        "origin_channel_id": forge.FORGE_CHANNEL_ID,
        "context_fact_sheet": [dup_spec],
        "chunks": [
            {
                "title": "Patch giant_mod around L120-L130",
                "target_files": ["giant_mod.py"],
                "target_symbols": ["giant_mod.py:giant_allocator"],
                "instructions": dup_spec,
            }
        ],
    }
    res_l = forge.enqueue_build(line_plan, db_path=db_p, post_widget=False)
    prompt_l = forge.format_chunk_prompt(res_l["build_id"], 0, db_path=db_p, worktree_dir=str(repo_dir))
    assert "*(full specification in Implementation Instructions below)*" in prompt_l
    assert "L120-L130" in prompt_l
    assert "step_122 = 122" in prompt_l


def test_decision_card_native_embeds_and_explicit_approval_bypass(tmp_path):
    db_p = tmp_path / "forge.db"
    init_forge_db(db_p)

    # 1. Pending Decision Card Embed and Text
    d_staged = stage_decision(
        category="forge",
        title="Deploy Test Pipeline",
        summary="Automated build for testing native Discord embeds.",
        handler_type="shell",
        payload={"command": "echo 'deployed'", "cwd": "/tmp"},
        channel_id=ZERO_OPS_CHANNEL_ID,
        db_path=db_p,
    )
    with forge_mod.get_db_connection(db_p) as conn:
        row_pending = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (d_staged["decision_id"],)).fetchone()
    embed_pending = render_decision_card_embed(row_pending)
    assert embed_pending["title"] == "🛡️ Deploy Test Pipeline"
    assert embed_pending["color"] == 0xF59E0B
    assert "Automated build for testing native Discord embeds." in embed_pending["description"]
    assert "**Approve Action:** Run `echo 'deployed'`" in embed_pending["description"]
    assert "FORGE · Expires" in embed_pending["footer"]["text"]

    text_pending = format_decision_card_text(row_pending)
    assert text_pending.startswith("🛡️ **Deploy Test Pipeline** [FORGE · Expires")
    assert "Automated build for testing native Discord embeds." in text_pending

    # 2. Approved & Executed Decision Card Embed and Text
    exec_res = execute_decision(d_staged["decision_id"], user_id=OWNER_USER_ID, db_path=db_p)
    assert exec_res["ok"] is True
    assert exec_res["status"] == "approved"
    assert "formatted_embed" in exec_res
    embed_approved = exec_res["formatted_embed"]
    assert embed_approved["title"] == "✅ Deploy Test Pipeline"
    assert embed_approved["color"] == 0x10B981
    assert "**Result:**" in embed_approved["description"]
    assert "deployed" in embed_approved["description"]
    assert "Approved & Executed ·" in embed_approved["footer"]["text"]

    text_approved = exec_res["formatted_card"]
    assert text_approved.startswith("✅ **Deploy Test Pipeline** [Approved & Executed ·")
    assert "**Result:**" in text_approved

    # 3. Dismissed Decision Card Embed and Text
    d_dismiss = stage_decision(
        category="homelab",
        title="Reboot Gateway",
        summary="Planned maintenance reboot.",
        handler_type="shell",
        payload={"command": "true"},
        channel_id=ZERO_OPS_CHANNEL_ID,
        db_path=db_p,
    )
    dis_res = dismiss_decision(d_dismiss["decision_id"], user_id=OWNER_USER_ID, db_path=db_p)
    assert dis_res["ok"] is True
    assert dis_res["status"] == "dismissed"
    assert "formatted_embed" in dis_res
    embed_dismissed = dis_res["formatted_embed"]
    assert embed_dismissed["title"] == "🛑 Reboot Gateway"
    assert embed_dismissed["color"] == 0x6B7280
    assert "Planned maintenance reboot." in embed_dismissed["description"]
    assert "**Approve Action:** Run `true`" in embed_dismissed["description"]
    assert "Dismissed ·" in embed_dismissed["footer"]["text"]

    # 4. Expired and Failed Decision Card Embed
    d_expired = stage_decision(
        category="misc",
        title="Old Action",
        summary="Action that timed out.",
        handler_type="shell",
        payload={"command": "true"},
        channel_id=ZERO_OPS_CHANNEL_ID,
        ttl_seconds=-10.0,
        db_path=db_p,
    )
    expire_stale_decisions(db_path=db_p)
    with forge_mod.get_db_connection(db_p) as conn:
        row_exp = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (d_expired["decision_id"],)).fetchone()
    embed_exp = render_decision_card_embed(row_exp)
    assert embed_exp["title"] == "⏱️ Old Action"
    assert embed_exp["color"] == 0x6B7280
    assert "Action that timed out." in embed_exp["description"]
    assert "**Approve Action:** Run `true`" in embed_exp["description"]
    assert embed_exp["footer"]["text"] == "Expired without action"

    d_fail = stage_decision(
        category="shell",
        title="Failing Shell Command",
        summary="Command that returns non-zero.",
        handler_type="shell",
        payload={"command": "exit 42", "cwd": "/tmp"},
        channel_id=ZERO_OPS_CHANNEL_ID,
        db_path=db_p,
    )
    fail_res = execute_decision(d_fail["decision_id"], user_id=OWNER_USER_ID, db_path=db_p)
    assert fail_res["ok"] is False
    assert fail_res["status"] == "failed"
    embed_fail = fail_res["formatted_embed"]
    assert embed_fail["title"] == "❌ Failing Shell Command"
    assert embed_fail["color"] == 0xEF4444
    assert "**Error:**" in embed_fail["description"]

    # 5. Explicit Approval Bypass: plan['approved'] = True or plan['skip_approval'] = True
    multi_chunk_plan = {
        "title": "Large Multi-Chunk Pipeline",
        "repo": "/workspace",
        "origin_channel_id": ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Multi-chunk test"],
        "chunks": [
            {"title": "Chunk 1", "target_files": ["tools/m1.py"], "instructions": "c1"},
            {"title": "Chunk 2", "target_files": ["tools/m2.py"], "instructions": "c2"},
        ],
    }
    # Without bypass, multi-chunk requires approval
    nonexistent_ifp = tmp_path / "nonexistent_in_flight.json"
    req, reasons = evaluate_plan_approval_gate(
        multi_chunk_plan,
        batch_state_path="/tmp/nonexistent.json",
        in_flight_path=nonexistent_ifp,
        db_path=db_p,
    )
    assert req is True
    assert any("Multi-chunk" in r for r in reasons)

    # With approved=True WITHOUT FORGE_APPROVED_CARD_EXECUTION=1 -> MUST NOT bypass
    plan_appr = dict(multi_chunk_plan, approved=True)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FORGE_APPROVED_CARD_EXECUTION", None)
        req_a_unpriv, reasons_a_unpriv = evaluate_plan_approval_gate(
            plan_appr,
            batch_state_path="/tmp/nonexistent.json",
            in_flight_path=nonexistent_ifp,
            db_path=db_p,
        )
        assert req_a_unpriv is True
        assert any("Multi-chunk" in r for r in reasons_a_unpriv)

        # With skip_approval=True WITHOUT FORGE_APPROVED_CARD_EXECUTION=1 -> MUST NOT bypass
        plan_skip = dict(multi_chunk_plan, skip_approval=True)
        req_s_unpriv, reasons_s_unpriv = evaluate_plan_approval_gate(
            plan_skip,
            batch_state_path="/tmp/nonexistent.json",
            in_flight_path=nonexistent_ifp,
            db_path=db_p,
        )
        assert req_s_unpriv is True
        assert any("Multi-chunk" in r for r in reasons_s_unpriv)

    # With FORGE_APPROVED_CARD_EXECUTION=1 -> approved=True and skip_approval=True bypass cleanly
    with patch.dict(os.environ, {"FORGE_APPROVED_CARD_EXECUTION": "1"}):
        req_a, reasons_a = evaluate_plan_approval_gate(
            plan_appr,
            batch_state_path="/tmp/nonexistent.json",
            in_flight_path=nonexistent_ifp,
            db_path=db_p,
        )
        assert req_a is False
        assert reasons_a == []

        plan_skip = dict(multi_chunk_plan, skip_approval=True)
        req_s, reasons_s = evaluate_plan_approval_gate(
            plan_skip,
            batch_state_path="/tmp/nonexistent.json",
            in_flight_path=nonexistent_ifp,
            db_path=db_p,
        )
        assert req_s is False
        assert reasons_s == []

    # 6. Explicit Approval Directives in in_flight_turn.json
    in_flight_file = tmp_path / "in_flight_turn.json"

    test_directives = [
        "queue fix",
        "approve",
        "approved",
        "ship it",
        "do it",
        "fix it",
        "please queue fix for this issue",
        "LGTM, ship it now",
    ]
    for directive in test_directives:
        in_flight_file.write_text(json.dumps({"prompt": directive}), encoding="utf-8")
        req_d, reasons_d = evaluate_plan_approval_gate(
            multi_chunk_plan,
            batch_state_path="/tmp/nonexistent.json",
            in_flight_path=in_flight_file,
            db_path=db_p,
        )
        assert req_d is False, f"Directive '{directive}' should have bypassed approval gate"
        assert reasons_d == []

    # Target channel nested prompt in in_flight_turn.json
    in_flight_file.write_text(
        json.dumps({str(ZERO_OPS_CHANNEL_ID): {"prompt": "queue plan please"}}),
        encoding="utf-8",
    )
    req_nested, _ = evaluate_plan_approval_gate(
        multi_chunk_plan,
        batch_state_path="/tmp/nonexistent.json",
        in_flight_path=in_flight_file,
        db_path=db_p,
    )
    assert req_nested is False

    # Non-directive prompt should NOT bypass
    in_flight_file.write_text(json.dumps({"prompt": "Can you check why the home assistant entity is offline?"}), encoding="utf-8")
    req_non, _ = evaluate_plan_approval_gate(
        multi_chunk_plan,
        batch_state_path="/tmp/nonexistent.json",
        in_flight_path=in_flight_file,
        db_path=db_p,
    )
    assert req_non is True

    # 7. CLI flag parsing rejects removed self-approval flags (--approved and --skip-approval)
    import pytest

    plan_file = tmp_path / "test_plan.json"
    plan_file.write_text(json.dumps(multi_chunk_plan), encoding="utf-8")

    for forbidden_flag in ("--approved", "--skip-approval"):
        with patch("sys.argv", ["forge.py", "enqueue", str(plan_file), forbidden_flag]), \
             patch.object(forge_mod, "enqueue_build") as mock_enq_bad:
            with pytest.raises(SystemExit) as exc_info:
                forge_mod.main()
            assert exc_info.value.code == 2
            mock_enq_bad.assert_not_called()

    # 8. Edited approved status formatting
    row_edited = {
        "status": "edited_approved",
        "category": "forge",
        "title": "Edited Plan",
        "summary": "Plan edited by Ryan.",
        "receipt_text": "Enqueued cleanly",
        "resolved_at": time.time(),
    }
    embed_ed = render_decision_card_embed(row_edited)
    assert embed_ed["title"] == "✅ Edited Plan"
    assert "Approved & Executed (Edited) ·" in embed_ed["footer"]["text"]
    text_ed = format_decision_card_text(row_edited)
    assert "Approved & Executed (Edited)" in text_ed

    # 9. Route Public / Crab Cavern Decision Cards to #zero-chat & preserve origin_channel_id
    banana_stand_ch = 1534436119888793750
    gotchi_ch = 1561509863018856468

    # Test direct _resolve_decision_channel resolution
    res_crab, orig_crab = forge_mod._resolve_decision_channel(banana_stand_ch)
    assert res_crab == ZERO_CHAT_CHANNEL_ID
    assert orig_crab == banana_stand_ch

    res_home, orig_home = forge_mod._resolve_decision_channel(ZERO_OPS_CHANNEL_ID)
    assert res_home == ZERO_OPS_CHANNEL_ID
    assert orig_home is None

    # Test direct stage_decision from Crab Cavern (#the-banana-stand)
    d_crab = stage_decision(
        category="homelab",
        title="Sync Storage Node",
        summary="Sync storage node requested from banana stand.",
        handler_type="shell",
        payload={"command": "echo 'synced'"},
        channel_id=banana_stand_ch,
        db_path=db_p,
    )
    assert d_crab["channel_id"] == ZERO_CHAT_CHANNEL_ID
    assert d_crab["redirected_to_zero_chat"] is True
    assert d_crab["origin_channel_id"] == banana_stand_ch
    assert d_crab["routing_note"] == "Decision Card routed to #zero-chat (never say Click below in origin channel)"
    with forge_mod.get_db_connection(db_p) as conn:
        row_crab = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (d_crab["decision_id"],)).fetchone()
    assert row_crab["channel_id"] == ZERO_CHAT_CHANNEL_ID
    assert row_crab["is_private"] == 1
    assert row_crab["summary"].startswith(f"📍 **Origin:** <#{banana_stand_ch}>\n")
    assert f"<#{banana_stand_ch}>" in row_crab["summary"]
    embed_crab = render_decision_card_embed(row_crab)
    assert f"📍 **Origin:** <#{banana_stand_ch}>" in embed_crab["description"]

    # Verify home channel stage_decision is not redirected
    d_home = stage_decision(
        category="homelab",
        title="Sync Home Node",
        summary="Sync home node requested from ops.",
        handler_type="shell",
        payload={"command": "echo 'home'"},
        channel_id=ZERO_OPS_CHANNEL_ID,
        db_path=db_p,
    )
    assert d_home["channel_id"] == ZERO_OPS_CHANNEL_ID
    assert d_home["redirected_to_zero_chat"] is False
    assert d_home["origin_channel_id"] is None
    assert d_home["routing_note"] is None

    # Verify origin tag is not prepended if already present in summary
    d_crab_dup = stage_decision(
        category="homelab",
        title="Sync Storage Node 2",
        summary=f"📍 **Origin:** <#{banana_stand_ch}>\nAlready tagged summary.",
        handler_type="shell",
        payload={"command": "echo 'synced2'"},
        channel_id=banana_stand_ch,
        db_path=db_p,
    )
    with forge_mod.get_db_connection(db_p) as conn:
        row_crab_dup = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (d_crab_dup["decision_id"],)).fetchone()
    assert row_crab_dup["summary"].count(f"<#{banana_stand_ch}>") == 1

    # Test direct stage_decision from Crab Cavern (#gotchi) with post_to_discord mock
    with patch.object(forge_mod, "_post_decision_card_rest") as mock_post_rest:
        mock_post_rest.return_value = 998877
        d_gotchi = stage_decision(
            category="gotchi",
            title="Deploy Gotchi Sprint",
            summary="Deploy gotchi sprint updates.",
            handler_type="shell",
            payload={"command": "echo 'gotchi'"},
            channel_id=gotchi_ch,
            db_path=db_p,
            post_to_discord=True,
        )
        assert d_gotchi["channel_id"] == ZERO_CHAT_CHANNEL_ID
        assert d_gotchi["redirected_to_zero_chat"] is True
        assert d_gotchi["origin_channel_id"] == gotchi_ch
        assert d_gotchi["routing_note"] == "Decision Card routed to #zero-chat (never say Click below in origin channel)"
        assert d_gotchi["discord_message_id"] == 998877
        mock_post_rest.assert_called_once_with(d_gotchi["decision_id"], db_path=db_p)
        with forge_mod.get_db_connection(db_p) as conn:
            row_gotchi = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (d_gotchi["decision_id"],)).fetchone()
        assert row_gotchi["channel_id"] == ZERO_CHAT_CHANNEL_ID
        assert row_gotchi["is_private"] == 1
        assert row_gotchi["summary"].startswith(f"📍 **Origin:** <#{gotchi_ch}>\n")

    # Test stage_plan_approval_decision from Crab Cavern (#the-banana-stand)
    crab_plan = {
        "title": "Crab Cavern Multi-Chunk Feature",
        "repo": "/workspace",
        "origin_channel_id": banana_stand_ch,
        "context_fact_sheet": ["Build initiated from Crab Cavern"],
        "chunks": [
            {"title": "Chunk 1", "target_files": ["tools/crab1.py"], "instructions": "Implement first chunk module for crab cavern."},
            {"title": "Chunk 2", "target_files": ["tools/crab2.py"], "instructions": "Implement second chunk module for crab cavern."},
        ],
    }
    with patch.object(forge_mod, "_post_decision_card_rest") as mock_plan_post:
        mock_plan_post.return_value = 112233
        staged_plan = stage_plan_approval_decision(
            crab_plan,
            reasons=["Multi-chunk build requires human sign-off"],
            db_path=db_p,
            post_to_discord=True,
        )
        assert staged_plan["ok"] is True
        assert staged_plan["status"] == "awaiting_approval"
        assert staged_plan["requires_approval"] is True
        # Build's origin_channel_id remains the original Crab Cavern channel for completion notifications
        assert staged_plan["origin_channel_id"] == banana_stand_ch
        assert staged_plan["channel_id"] == ZERO_CHAT_CHANNEL_ID
        assert staged_plan["redirected_to_zero_chat"] is True
        assert staged_plan["routing_note"] == "Decision Card routed to #zero-chat (never say Click below in origin channel)"
        assert "zero-chat" in staged_plan.get("message", "")
        assert staged_plan["discord_message_id"] == 112233
        mock_plan_post.assert_called_once_with(staged_plan["decision_id"], db_path=db_p)

        # Test enqueue_build directly returns redirected_to_zero_chat and note when approval gate triggers for Crab Cavern
        with patch.object(forge_mod, "evaluate_plan_approval_gate", return_value=(True, ["Crab cavern complexity gate"])):
            enq_crab_res = enqueue_build(
                crab_plan,
                db_path=db_p,
                post_widget=False,
                check_approval_gate=True,
            )
            assert enq_crab_res["ok"] is True
            assert enq_crab_res["status"] == "awaiting_approval"
            assert enq_crab_res["origin_channel_id"] == banana_stand_ch
            assert enq_crab_res["channel_id"] == ZERO_CHAT_CHANNEL_ID
            assert enq_crab_res["redirected_to_zero_chat"] is True
            assert enq_crab_res["routing_note"] == "Decision Card routed to #zero-chat (never say Click below in origin channel)"
            assert "zero-chat" in enq_crab_res["message"]

        # Plan file JSON on disk retains origin_channel_id
        plan_disk_data = json.loads(Path(staged_plan["plan_file"]).read_text(encoding="utf-8"))
        assert plan_disk_data["origin_channel_id"] == banana_stand_ch

        # Staged decision row in SQLite is routed to ZERO_CHAT_CHANNEL_ID
        with forge_mod.get_db_connection(db_p) as conn:
            row_plan_dec = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (staged_plan["decision_id"],)).fetchone()
        assert row_plan_dec["channel_id"] == ZERO_CHAT_CHANNEL_ID
        assert row_plan_dec["is_private"] == 1
        assert row_plan_dec["summary"].startswith(f"📍 **Origin:** <#{banana_stand_ch}>\n")

        # When executed/approved, forge_enqueue passes the saved plan with original origin_channel_id
        with patch.object(forge_mod, "enqueue_build") as mock_enq_approved:
            mock_enq_approved.return_value = {"ok": True, "build_id": "test-crab-approved"}
            exec_res = execute_decision(staged_plan["decision_id"], user_id=OWNER_USER_ID, db_path=db_p)
            assert exec_res["ok"] is True
            assert exec_res["status"] == "approved"
            mock_enq_approved.assert_called_once()
            passed_plan = mock_enq_approved.call_args[0][0]
            assert passed_plan["origin_channel_id"] == banana_stand_ch


def test_forge_plan_alias_normalization_and_depends_on_coercion(tmp_path):
    """Verify validate_forge_plan and preflight auto-normalize chunk aliases and string depends_on."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "vulcan@example.com"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Vulcan"], cwd=str(repo_dir), check=True, capture_output=True)
    (repo_dir / "mod_a.py").write_text("x = 1\n", encoding="utf-8")
    (repo_dir / "mod_b.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    # 1. Alias normalization: files -> target_files, description/prompt -> instructions, depends_on: "c0"
    plan_aliases = {
        "title": "Alias Normalization Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Architectural fact 1"],
        "chunks": [
            {
                "title": "Chunk 0",
                "files": ["mod_a.py"],
                "description": "Implement feature A with unit tests in mod_a.py.",
            },
            {
                "title": "Chunk 1",
                "files": ["mod_b.py"],
                "prompt": "Implement feature B with unit tests in mod_b.py.",
                "depends_on": "c0",
            },
        ],
    }
    ok, msg = forge_mod.validate_forge_plan(plan_aliases)
    assert ok is True, f"Validation failed: {msg}"
    assert plan_aliases["chunks"][0]["target_files"] == ["mod_a.py"]
    assert plan_aliases["chunks"][0]["instructions"] == "Implement feature A with unit tests in mod_a.py."
    assert plan_aliases["chunks"][1]["target_files"] == ["mod_b.py"]
    assert plan_aliases["chunks"][1]["instructions"] == "Implement feature B with unit tests in mod_b.py."
    assert plan_aliases["chunks"][1]["depends_on"] == [0]

    # 2. String depends_on coercion: "chunk-0", "0", 0
    for dep_val in ("chunk-0", "0", 0, ["chunk-0"], ["0"], [0]):
        ch_test = {
            "title": "Chunk 1",
            "files": ["mod_b.py"],
            "instructions": "Implement feature B in mod_b.py.",
            "depends_on": dep_val,
        }
        all_c = [{"title": "Chunk 0", "files": ["mod_a.py"], "instructions": "Impl A"}, ch_test]
        forge_mod._normalize_chunk_dict_in_place(ch_test, 1, all_c)
        assert ch_test["depends_on"] == [0], f"Failed for dep_val={dep_val}: {ch_test['depends_on']}"

    # 3. 1-indexed "chunk-1" on chunk index 1 (when "chunk-0" is not present in the plan)
    plan_1indexed = {
        "title": "1-Indexed Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Setup Step",
                "target_files": ["mod_a.py"],
                "instructions": "Run setup steps.",
            },
            {
                "title": "Build Step",
                "target_files": ["mod_b.py"],
                "instructions": "Run build steps.",
                "depends_on": ["chunk-1"],
            },
        ],
    }
    ok_1i, msg_1i = forge_mod.validate_forge_plan(plan_1indexed)
    assert ok_1i is True, f"1-indexed validation failed: {msg_1i}"
    assert plan_1indexed["chunks"][1]["depends_on"] == [0]


def test_forge_plan_split_caps_4000_chars_and_6_tasks(tmp_path):
    """Verify relaxed split caps (4000 chars instructions and 6 numbered tasks)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "vulcan@example.com"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Vulcan"], cwd=str(repo_dir), check=True, capture_output=True)
    (repo_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    # 1. 3500 chars and 6 numbered tasks passes
    detailed_instructions = (
        "1. First task with detailed specification.\n"
        "2. Second task with detailed architectural rules.\n"
        "3. Third task with full error handling.\n"
        "4. Fourth task with edge cases and guards.\n"
        "5. Fifth task with verification commands.\n"
        "6. Sixth task with documentation updates.\n"
        + ("Additional detailed context: " * 80)
    )
    assert 2000 < len(detailed_instructions) <= 4000

    pass_plan = {
        "title": "Detailed Single-Chunk Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Atomic chunk with 3500 chars and 6 tasks"],
        "chunks": [
            {
                "title": "Detailed Chunk",
                "target_files": ["mod.py"],
                "instructions": detailed_instructions,
            }
        ],
    }
    ok, msg = forge_mod.validate_forge_plan(pass_plan)
    assert ok is True, f"Expected pass, got error: {msg}"

    # 2. > 4000 chars is rejected
    over_chars_plan = {
        "title": "Over Chars Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk",
                "target_files": ["mod.py"],
                "instructions": "x" * 4050,
            }
        ],
    }
    ok_c, msg_c = forge_mod.validate_forge_plan(over_chars_plan)
    assert ok_c is False
    assert "4000 chars" in msg_c

    # 3. > 6 numbered tasks is rejected
    seven_tasks = "\n".join(f"{i}. Task item {i}." for i in range(1, 8))
    over_tasks_plan = {
        "title": "Over Tasks Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk",
                "target_files": ["mod.py"],
                "instructions": seven_tasks,
            }
        ],
    }
    ok_t, msg_t = forge_mod.validate_forge_plan(over_tasks_plan)
    assert ok_t is False
    assert "7 numbered tasks" in msg_t


def test_extract_symbol_snippet_parenthetical_line_ranges(tmp_path):
    """Verify extract_symbol_snippet supports parenthetical line ranges in target_symbols."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "vulcan@example.com"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Vulcan"], cwd=str(repo_dir), check=True, capture_output=True)

    code_lines = [f"def func_{i}(): pass  # line {i}" for i in range(1, 40)]
    py_file = repo_dir / "models.py"
    py_file.write_text("\n".join(code_lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    # 1. extract_symbol_snippet with (lines 10-20)
    snip_lines = forge_mod.extract_symbol_snippet(py_file, "BIGModelSuite.predict_pitcher (lines 10-20)")
    assert "  10: def func_10(): pass" in snip_lines
    assert "  20: def func_20(): pass" in snip_lines
    assert "   9: def func_9(): pass" not in snip_lines
    assert "  21: def func_21(): pass" not in snip_lines

    # 2. extract_symbol_snippet with (L5-L15)
    snip_l = forge_mod.extract_symbol_snippet(py_file, "func (L5-L15)")
    assert "   5: def func_5(): pass" in snip_l
    assert "  15: def func_15(): pass" in snip_l
    assert "   4: def func_4(): pass" not in snip_l
    assert "  16: def func_16(): pass" not in snip_l

    # 3. Goldfish pre-flight passes with parenthetical line range in target_symbols
    plan_paren = {
        "title": "Parenthetical Symbols Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk with paren symbol",
                "target_files": ["models.py"],
                "target_symbols": ["models.py:BIGModelSuite.predict_pitcher (lines 10-20)"],
                "instructions": "Optimize predict_pitcher in models.py.",
            }
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_paren)
    assert ok is True, f"Preflight findings: {findings}"


def test_goldfish_plan_preflight_available_symbols_hint(tmp_path):
    """Verify run_goldfish_plan_preflight includes available top-level symbols or class methods on miss."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    service_code = """
def start_server(port: int = 8080):
    pass

def stop_server():
    pass

class ServerConfig:
    def __init__(self, host: str):
        self.host = host

    def validate(self):
        return True

    async def reload_async(self):
        pass
"""
    (repo_dir / "service.py").write_text(service_code.strip() + "\n", encoding="utf-8")

    # 1. Missing top-level symbol lists top-level functions and classes
    plan_top_miss = {
        "title": "Missing Top Symbol Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "chunks": [
            {
                "title": "Chunk missing top symbol",
                "target_files": ["service.py"],
                "target_symbols": ["service.py:restart_server"],
                "instructions": "Restart the server using existing helpers.",
            }
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_top_miss)
    assert ok is False
    assert len(findings) == 1
    assert "target symbol 'service.py:restart_server' not found in 'service.py'" in findings[0]
    assert "Available symbols in 'service.py': start_server, stop_server, ServerConfig." in findings[0]

    # 2. Missing class method lists methods on the target class
    plan_method_miss = {
        "title": "Missing Method Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "chunks": [
            {
                "title": "Chunk missing method",
                "target_files": ["service.py"],
                "target_symbols": ["service.py:ServerConfig.missing_method"],
                "instructions": "Call missing_method on ServerConfig.",
            }
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_method_miss)
    assert ok is False
    assert len(findings) == 1
    assert "target symbol 'service.py:ServerConfig.missing_method' not found in 'service.py'" in findings[0]
    assert "Available symbols in 'service.py' (methods on 'ServerConfig'): __init__, validate, reload_async." in findings[0]

    # 3. Truncation when a file has > 25 symbols
    many_funcs = "\n".join(f"def func_{i:02d}(): pass" for i in range(30))
    (repo_dir / "many.py").write_text(many_funcs + "\n", encoding="utf-8")
    plan_many = {
        "title": "Many Symbols Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "chunks": [
            {
                "title": "Chunk with many symbols",
                "target_files": ["many.py"],
                "target_symbols": ["many.py:nonexistent_func"],
                "instructions": "Do something.",
            }
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_many)
    assert ok is False
    assert len(findings) == 1
    assert "Available symbols in 'many.py':" in findings[0]
    assert "(+5 more)" in findings[0]
    assert "func_24" in findings[0]
    assert "func_25" not in findings[0]

    # 4. Explicitly marked new symbol in instructions passes without error
    plan_new = {
        "title": "New Symbol Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "chunks": [
            {
                "title": "Chunk creating new symbol",
                "target_files": ["service.py"],
                "target_symbols": ["service.py:restart_server"],
                "instructions": "Implement and create restart_server in service.py.",
            }
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_new)
    assert ok is True
    assert findings == []


def test_goldfish_plan_preflight_missing_existing_target_file(tmp_path):
    """Verify run_goldfish_plan_preflight rejects target_files that don't exist when instructions refer to them as existing."""
    repo_dir = tmp_path / "repo"
    tools_dir = repo_dir / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "forge.py").write_text("# existing forge\n", encoding="utf-8")

    # 1. Typo in target_files where instructions say 'Update tools/forge_worker.py'
    plan_missing = {
        "title": "Missing Target File Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk updating missing file",
                "target_files": ["tools/forge_worker.py"],
                "instructions": "Update tools/forge_worker.py to support new lease options.",
            }
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_missing)
    assert ok is False
    assert len(findings) == 1
    assert "target file 'tools/forge_worker.py' does not exist in repository" in findings[0]
    assert "Did you mean 'tools/forge.py'?" in findings[0]

    # 2. 'In tools/forge_worker.py' also triggers rejection
    plan_in = {
        "title": "Missing Target File In Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk in missing file",
                "target_files": ["tools/forge_worker.py"],
                "instructions": "In tools/forge_worker.py, add new helper functions.",
            }
        ],
    }
    ok_in, findings_in = forge_mod.run_goldfish_plan_preflight(plan_in)
    assert ok_in is False
    assert len(findings_in) == 1
    assert "target file 'tools/forge_worker.py' does not exist in repository" in findings_in[0]


def test_goldfish_plan_preflight_missing_verification_cmd_test_file(tmp_path):
    """Verify run_goldfish_plan_preflight rejects verification_cmd referencing non-existent test file not in target_files."""
    repo_dir = tmp_path / "repo"
    tools_dir = repo_dir / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "forge.py").write_text("# existing forge\n", encoding="utf-8")
    (tools_dir / "test_forge.py").write_text("# existing test_forge\n", encoding="utf-8")

    plan_missing_test = {
        "title": "Missing Test File Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk referencing non-existent test file",
                "target_files": ["tools/forge.py"],
                "instructions": "Implement feature in forge.py.",
                "verification_cmd": "pytest tools/test_forge_typo.py -k test_foo",
            }
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_missing_test)
    assert ok is False
    assert len(findings) == 1
    assert "verification command references test file 'tools/test_forge_typo.py' which does not exist on disk" in findings[0]
    assert "Did you mean" in findings[0]
    assert "tools/test_forge.py" in findings[0]


def test_goldfish_plan_preflight_new_file_allowance(tmp_path):
    """Verify run_goldfish_plan_preflight allows missing files when explicitly described as new or in prior chunks."""
    repo_dir = tmp_path / "repo"
    tools_dir = repo_dir / "tools"
    tools_dir.mkdir(parents=True)

    plan_new = {
        "title": "Create New File Plan",
        "repo": str(repo_dir),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Create new module tools/module_x.py",
                "target_files": ["tools/module_x.py", "tools/test_module_x.py"],
                "instructions": "Create tools/module_x.py with implementation and add unit tests in tools/test_module_x.py.",
                "verification_cmd": "pytest tools/test_module_x.py",
            },
            {
                "title": "Extend module_x in downstream chunk",
                "target_files": ["tools/module_x.py"],
                "instructions": "Update tools/module_x.py to add secondary methods.",
                "verification_cmd": "pytest tools/test_module_x.py",
            },
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_new)
    assert ok is True, f"Expected pass, got findings: {findings}"
    assert findings == []


def test_goldfish_plan_preflight_multi_branch_git_surgery_rejection():
    """Verify run_goldfish_plan_preflight rejects instructions directing multi-branch git surgery or stacked branch pushes."""
    # 1. git push origin with 2+ branch names
    plan_push = {
        "title": "Multi-Branch Push Plan",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk directing multi-branch push",
                "target_files": ["tools/forge.py"],
                "instructions": "Finish changes then run git push origin feat/branch-1 feat/branch-2 to sync both.",
            }
        ],
    }
    ok_p, findings_p = forge_mod.run_goldfish_plan_preflight(plan_push)
    assert ok_p is False
    assert len(findings_p) == 1
    assert "instruct manual multi-branch git surgery or stacked-branch propagation" in findings_p[0]
    assert "Keep stacked branch propagation out of worker prompts" in findings_p[0]

    # 2. Instructing to sync/push multiple stacked branches
    plan_sync = {
        "title": "Stacked Branch Sync Plan",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk directing stacked branch sync",
                "target_files": ["tools/forge.py"],
                "instructions": "Sync and push multiple feat/step1 and feat/step2 stacked branches.",
            }
        ],
    }
    ok_s, findings_s = forge_mod.run_goldfish_plan_preflight(plan_sync)
    assert ok_s is False
    assert "instruct manual multi-branch git surgery or stacked-branch propagation" in findings_s[0]

    # 3. Instructing cherry-picking across multiple branches
    plan_cherry = {
        "title": "Cherry-Pick Plan",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk directing cherry-pick across branches",
                "target_files": ["tools/forge.py"],
                "instructions": "Cherry-pick commits across multiple branches to keep them aligned.",
            }
        ],
    }
    ok_c, findings_c = forge_mod.run_goldfish_plan_preflight(plan_cherry)
    assert ok_c is False
    assert "instruct manual multi-branch git surgery or stacked-branch propagation" in findings_c[0]


def test_goldfish_preflight_is_new_file_in_instructions_helper():
    """Verify _is_new_file_in_instructions accurately identifies new vs existing file intent."""
    # Direct creation verbs in instructions
    assert forge_mod._is_new_file_in_instructions("tools/new.py", "Create tools/new.py with classes.") is True
    assert forge_mod._is_new_file_in_instructions("tools/new.py", "Add tools/new.py with unit tests.") is True
    assert forge_mod._is_new_file_in_instructions("tools/new.py", "Write tools/new.py to export helper.") is True
    assert forge_mod._is_new_file_in_instructions("tools/new.py", "Scaffold tools/new.py.") is True
    assert forge_mod._is_new_file_in_instructions("tools/new.py", "New file tools/new.py will be added.") is True

    # Title-based creation
    assert forge_mod._is_new_file_in_instructions("tools/new.py", "", title="Create tools/new.py") is True
    assert forge_mod._is_new_file_in_instructions("tools/new.py", "", title="Add new.py") is True

    # Existing file references
    assert forge_mod._is_new_file_in_instructions("tools/existing.py", "In tools/existing.py, add new function.") is False
    assert forge_mod._is_new_file_in_instructions("tools/existing.py", "Update tools/existing.py with new logic.") is False
    assert forge_mod._is_new_file_in_instructions("tools/existing.py", "Modify tools/existing.py to fix bug.") is False
    assert forge_mod._is_new_file_in_instructions("tools/existing.py", "Edit tools/existing.py.") is False

    # Empty or unrelated
    assert forge_mod._is_new_file_in_instructions("tools/existing.py", "") is False
    assert forge_mod._is_new_file_in_instructions("tools/existing.py", "Create tools/other.py.") is False


def test_enqueue_build_clears_offload_dispatched_on_exception(tmp_path):
    """Verify enqueue_build clears offload_dispatched in batch_guard_state.json if validation raises ValueError."""
    bs_file = tmp_path / "batch_guard_state.json"
    bs_file.write_text(json.dumps({"offload_dispatched": True, "offload_kind": "forge_enqueue"}), encoding="utf-8")

    invalid_plan = {
        "title": "Invalid Plan Without Chunks",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [],
    }

    try:
        forge_mod.enqueue_build(invalid_plan, batch_state_path=bs_file)
        assert False, "Expected ValueError"
    except ValueError:
        pass

    state_data = json.loads(bs_file.read_text(encoding="utf-8"))
    assert state_data.get("offload_dispatched") is False
    assert state_data.get("offload_kind") == ""


def test_record_forge_telemetry(tmp_path, monkeypatch):
    """Verify that FORGE worker and reviewer token telemetry is recorded into token_usage with parent_channel_name='forge' and mode='forge', and stored in chunk/build metadata."""
    import sqlite3
    import tools.forge as forge_mod

    telemetry_db = tmp_path / "telemetry.db"
    forge_db = tmp_path / "forge.db"
    forge_mod.init_forge_db(forge_db)

    # 1. Mock extract_sqlite_step_tokens to verify exact token recording for a worker invocation
    mock_step_tokens = {
        1: {
            "input_tokens": 4200,
            "output_tokens": 350,
            "thinking_tokens": 120,
            "cache_read_tokens": 300,
            "total_tokens": 4550,
        },
        3: {
            "input_tokens": 1000,
            "output_tokens": 150,
            "thinking_tokens": 30,
            "cache_read_tokens": 0,
            "total_tokens": 1150,
        },
    }

    import tools.backfill_telemetry as bt
    monkeypatch.setattr(bt, "extract_sqlite_step_tokens", lambda cid: mock_step_tokens)

    worker_res = forge_mod.record_forge_telemetry(
        build_id="bg-alpha",
        chunk_id="chk-001",
        conv_id="conv-vulcan-worker-abc",
        model="gemini-3.8-flash-high",
        role="worker",
        elapsed_s=14.2,
        db_path=telemetry_db,
    )

    assert worker_res["input_tokens"] == 5200
    assert worker_res["output_tokens"] == 500
    assert worker_res["cache_read_tokens"] == 300
    assert worker_res["thinking_tokens"] == 150
    assert worker_res["total_tokens"] == 5700
    assert worker_res["role"] == "worker"
    assert worker_res["model"] == "gemini-3.8-flash-high"
    assert worker_res["channel_name"] == "forge:bg-alpha:chk-001:worker"
    assert worker_res["parent_channel_name"] == "forge"
    assert worker_res["mode"] == "forge"
    assert worker_res["conv_id"] == "conv-vulcan-worker-abc"

    # 2. Verify fallback estimation when conv_id is None (e.g. Maker-Checker reviewer invocation)
    review_res = forge_mod.record_forge_telemetry(
        build_id="bg-alpha",
        chunk_id="chk-001",
        conv_id=None,
        model="gemini-3.8-flash-high",
        role="reviewer",
        elapsed_s=6.5,
        db_path=telemetry_db,
    )

    assert review_res["role"] == "reviewer"
    assert review_res["channel_name"] == "forge:bg-alpha:chk-001:reviewer"
    assert review_res["parent_channel_name"] == "forge"
    assert review_res["mode"] == "forge"
    assert review_res["conv_id"] == "forge-bg-alpha-chk-001"
    assert review_res["total_tokens"] > 0
    assert review_res["input_tokens"] > 0
    assert review_res["output_tokens"] > 0

    # 3. Verify SQLite token_usage rows in telemetry_db
    with sqlite3.connect(str(telemetry_db)) as tconn:
        tconn.row_factory = sqlite3.Row
        rows = tconn.execute(
            "SELECT * FROM token_usage WHERE parent_channel_name = 'forge' AND mode = 'forge' ORDER BY id ASC"
        ).fetchall()

    assert len(rows) == 2

    # Worker row
    w_row = rows[0]
    assert w_row["channel_id"] == forge_mod.FORGE_CHANNEL_ID
    assert w_row["channel_name"] == "forge:bg-alpha:chk-001:worker"
    assert w_row["parent_channel_name"] == "forge"
    assert w_row["mode"] == "forge"
    assert w_row["conv_id"] == "conv-vulcan-worker-abc"
    assert w_row["input_tokens"] == 5200
    assert w_row["output_tokens"] == 500
    assert w_row["cache_read_tokens"] == 300
    assert w_row["thinking_tokens"] == 150
    assert w_row["total_tokens"] == 5700
    assert w_row["is_backfill"] == 0

    # Reviewer row
    r_row = rows[1]
    assert r_row["channel_id"] == forge_mod.FORGE_CHANNEL_ID
    assert r_row["channel_name"] == "forge:bg-alpha:chk-001:reviewer"
    assert r_row["parent_channel_name"] == "forge"
    assert r_row["mode"] == "forge"
    assert r_row["conv_id"] == "forge-bg-alpha-chk-001"
    assert r_row["total_tokens"] == review_res["total_tokens"]

    # 4. Verify storing cumulative token stats in chunk & build metadata
    with sqlite3.connect(str(forge_db)) as fconn:
        now_ts = 1700000000.0
        fconn.execute(
            """
            INSERT INTO forge_builds (id, title, origin_channel_id, created_at, updated_at)
            VALUES ('bg-alpha', 'Test Build', 1562899941595160657, ?, ?)
            """,
            (now_ts, now_ts),
        )
        fconn.execute(
            """
            INSERT INTO forge_chunks (id, build_id, chunk_index, title, instructions)
            VALUES ('chk-001', 'bg-alpha', 0, 'Test Chunk', 'Do work')
            """
        )
        fconn.commit()

    forge_mod.update_chunk_and_build_tokens("bg-alpha", "chk-001", worker_res, db_path=forge_db)
    forge_mod.update_chunk_and_build_tokens("bg-alpha", "chk-001", review_res, db_path=forge_db)

    with sqlite3.connect(str(forge_db)) as fconn:
        fconn.row_factory = sqlite3.Row
        c_row = fconn.execute("SELECT * FROM forge_chunks WHERE id = 'chk-001'").fetchone()
        b_row = fconn.execute("SELECT * FROM forge_builds WHERE id = 'bg-alpha'").fetchone()

    expected_total = worker_res["total_tokens"] + review_res["total_tokens"]
    assert c_row["total_tokens"] == expected_total
    assert b_row["total_tokens"] == expected_total

    c_meta = json.loads(c_row["tokens_json"])
    assert c_meta["total_tokens"] == expected_total
    assert len(c_meta["history"]) == 2
    assert c_meta["history"][0]["role"] == "worker"
    assert c_meta["history"][1]["role"] == "reviewer"

    b_meta = json.loads(b_row["tokens_json"])
    assert b_meta["total_tokens"] == expected_total


def _init_test_git_repo(repo_path: Path) -> Path:
    """Helper to initialize a clean git repository for testing."""
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_path)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), capture_output=True, check=True)
    (repo_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_path), capture_output=True, check=True)
    return repo_path


def test_prepare_clean_repo_aborts_wedged_operations(tmp_path):
    """Verify _prepare_clean_repo_and_sync_base aborts any wedged git merge/rebase/cherry-pick."""
    repo = _init_test_git_repo(tmp_path / "repo")

    # Create a conflict merge state
    subprocess.run(["git", "checkout", "-b", "feat"], cwd=str(repo), capture_output=True, check=True)
    (repo / "README.md").write_text("# Feat Branch\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "Feat commit"], cwd=str(repo), capture_output=True, check=True)

    subprocess.run(["git", "checkout", "main"], cwd=str(repo), capture_output=True, check=True)
    (repo / "README.md").write_text("# Main Branch\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "Main commit"], cwd=str(repo), capture_output=True, check=True)

    # Trigger a conflicting merge
    merge_res = subprocess.run(["git", "merge", "feat"], cwd=str(repo), capture_output=True)
    assert merge_res.returncode != 0
    assert (repo / ".git" / "MERGE_HEAD").exists()

    # Call _prepare_clean_repo_and_sync_base to clear the wedged merge
    resolved_ref = forge_mod._prepare_clean_repo_and_sync_base(repo, "main")
    assert resolved_ref in ("main", "HEAD")
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_prepare_clean_repo_autostashes_dirty_tracked_files(tmp_path, monkeypatch):
    """Verify _prepare_clean_repo_and_sync_base auto-stashes dirty tracked files in repo_dir."""
    repo = _init_test_git_repo(tmp_path / "repo")

    # Dirty a tracked file
    (repo / "README.md").write_text("# Modified Content\n", encoding="utf-8")

    # When repo != WORKSPACE_DIR, auto-stash executes
    forge_mod._prepare_clean_repo_and_sync_base(repo, "main")
    diff_chk = subprocess.run(["git", "diff", "--quiet"], cwd=str(repo), capture_output=True)
    assert diff_chk.returncode == 0  # Clean after autostash

    stash_list = subprocess.run(["git", "stash", "list"], cwd=str(repo), capture_output=True, text=True)
    assert "forge-autostash-" in stash_list.stdout

    # When repo == WORKSPACE_DIR and before_merge=False, auto-stash is skipped
    (repo / "README.md").write_text("# Another Dirty Content\n", encoding="utf-8")
    monkeypatch.setattr(forge_mod, "WORKSPACE_DIR", repo)
    forge_mod._prepare_clean_repo_and_sync_base(repo, "main", before_merge=False)
    diff_chk2 = subprocess.run(["git", "diff", "--quiet"], cwd=str(repo), capture_output=True)
    assert diff_chk2.returncode != 0  # Still dirty

    # When repo == WORKSPACE_DIR and before_merge=True, auto-stash runs
    forge_mod._prepare_clean_repo_and_sync_base(repo, "main", before_merge=True)
    diff_chk3 = subprocess.run(["git", "diff", "--quiet"], cwd=str(repo), capture_output=True)
    assert diff_chk3.returncode == 0  # Clean after autostash before merge


def test_prepare_clean_repo_aligns_default_branch_to_origin(tmp_path):
    """Verify _prepare_clean_repo_and_sync_base aligns local default_branch to origin/<default_branch> when behind."""
    bare_remote = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], capture_output=True, check=True)

    local_repo = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare_remote), str(local_repo)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(local_repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(local_repo), capture_output=True, check=True)
    (local_repo / "README.md").write_text("# Initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(local_repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=str(local_repo), capture_output=True, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(local_repo), capture_output=True, check=True)

    # Push a newer commit to remote from a peer clone
    peer_repo = tmp_path / "peer"
    subprocess.run(["git", "clone", str(bare_remote), str(peer_repo)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Peer User"], cwd=str(peer_repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "peer@example.com"], cwd=str(peer_repo), capture_output=True, check=True)
    (peer_repo / "README.md").write_text("# Updated from Peer\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "Peer update"], cwd=str(peer_repo), capture_output=True, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(peer_repo), capture_output=True, check=True)
    peer_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(peer_repo), capture_output=True, text=True).stdout.strip()

    # In local_repo, main is behind origin/main
    # Calling _prepare_clean_repo_and_sync_base should fetch and align local main
    latest_ref = forge_mod._prepare_clean_repo_and_sync_base(local_repo, "main")
    assert latest_ref == "origin/main"
    local_main_head = subprocess.run(["git", "rev-parse", "main"], cwd=str(local_repo), capture_output=True, text=True).stdout.strip()
    assert local_main_head == peer_head


def test_propagate_build_branches(tmp_path):
    """Verify _propagate_build_branches deterministically creates or updates downstream branches."""
    repo = _init_test_git_repo(tmp_path / "repo")
    head_rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True).stdout.strip()

    # Propagate from JSON string in sqlite row or dict
    build_meta = {"propagate_branches_json": json.dumps(["staging", "release-candidate"])}
    propagated = forge_mod._propagate_build_branches(repo, "HEAD", build_meta)
    assert "staging" in propagated
    assert "release-candidate" in propagated

    stg_rev = subprocess.run(["git", "rev-parse", "staging"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    rc_rev = subprocess.run(["git", "rev-parse", "release-candidate"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    assert stg_rev == head_rev
    assert rc_rev == head_rev

    # Propagate from direct list in dict
    propagated2 = forge_mod._propagate_build_branches(repo, "HEAD", {"propagate_branches": ["preview"]})
    assert "preview" in propagated2
    prev_rev = subprocess.run(["git", "rev-parse", "preview"], cwd=str(repo), capture_output=True, text=True).stdout.strip()
    assert prev_rev == head_rev


def test_validate_forge_plan_propagate_branches():
    """Verify validate_forge_plan validates optional propagate_branches list."""
    base_plan = {
        "title": "Branch Propagation Plan",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["tools/forge.py"],
                "instructions": "Implement feature in forge.py.",
            }
        ],
    }

    # Valid propagate_branches
    plan_valid = dict(base_plan, propagate_branches=["staging", "feat/release-preview"])
    ok_v, reason_v = forge_mod.validate_forge_plan(plan_valid)
    assert ok_v is True

    # Invalid propagate_branches (string instead of list)
    plan_invalid_str = dict(base_plan, propagate_branches="staging")
    ok_is, reason_is = forge_mod.validate_forge_plan(plan_invalid_str)
    assert ok_is is False
    assert "must be a list of branch name strings" in reason_is

    # Invalid propagate_branches (empty item)
    plan_invalid_empty = dict(base_plan, propagate_branches=["staging", ""])
    ok_ie, reason_ie = forge_mod.validate_forge_plan(plan_invalid_empty)
    assert ok_ie is False
    assert "must be a list of branch name strings" in reason_ie


def test_post_verification_commit_preservation_in_worktree(tmp_path, monkeypatch):
    """Verify execute_chunk_in_worktree preserves verified commit wt_head_ref even if base_ref rebase conflicts."""
    repo = _init_test_git_repo(tmp_path / "repo")
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)

    build_id = "fg-test-preserve"
    chunk_id = f"{build_id}-c0"

    plan = {
        "id": build_id,
        "title": "Preserve Commit Test",
        "repo": str(repo),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Fact"],
        "propagate_branches": ["staging"],
        "chunks": [
            {
                "title": "Test Chunk",
                "target_files": ["README.md"],
                "instructions": "Update README.md with changes.",
                "verification_cmd": "true",
            }
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)

    supervisor = forge_mod.ForgeSupervisor(db_path=db_path)

    orig_popen = subprocess.Popen

    def _mock_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd):
            cwd = kwargs.get("cwd") or (args[1] if len(args) > 1 else None)
            if cwd and "forge-wt-" in str(cwd):
                # Simulate worker modifying README.md in wt_dir
                p = Path(cwd) / "README.md"
                p.write_text("# Worker Verified Content\n", encoding="utf-8")
            # Return a process that exits 0
            return orig_popen(["true"], *args, **kwargs)
        return orig_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _mock_popen)

    # In repo, advance base_ref (forge/release-fg-test-preserve) with a commit
    staging_ref = f"forge/release-{build_id}"
    subprocess.run(["git", "branch", "-f", staging_ref, "main"], cwd=str(repo), capture_output=True)

    with forge_mod.get_db_connection(db_path) as conn:
        chunk_row = conn.execute("SELECT * FROM forge_chunks WHERE id = ?", (chunk_id,)).fetchone()

    # Execute chunk in worktree
    success = supervisor.execute_chunk_in_worktree(build_id, chunk_row)
    assert success is True

    # Verify chunk was marked as merged and build completed
    with forge_mod.get_db_connection(db_path) as conn:
        c_row = conn.execute("SELECT * FROM forge_chunks WHERE id = ?", (chunk_id,)).fetchone()
        b_row = conn.execute("SELECT * FROM forge_builds WHERE id = ?", (build_id,)).fetchone()
    assert c_row["status"] == "merged"
    assert b_row["status"] == "completed"

    # Verify downstream branch propagation occurred
    stg_content = subprocess.run(["git", "show", "staging:README.md"], cwd=str(repo), capture_output=True, text=True).stdout
    assert "Worker Verified Content" in stg_content


def test_short_job_wakes_channel_when_turn_lock_active(tmp_path, monkeypatch):
    """Verify that short completed jobs (<15s) still dispatch completion wakeup when Post-Offload Turn Lock is active or notify_on_success=True, and wait up to 15s for closing origin turns."""
    import tools.forge as forge
    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "FORGE_DB_PATH", db_p)
    monkeypatch.setattr(forge, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge, "_is_prod_forge_env", lambda *a, **kw: False)
    monkeypatch.setattr(forge, "_is_channel_turn_actively_in_flight", lambda ch: True)

    bs_file = tmp_path / "batch_guard_state.json"
    bs_file.write_text(json.dumps({"offload_dispatched": True, "offload_kind": "forge.py job start"}), encoding="utf-8")

    outbox_calls = []
    import tools.outbox as outbox
    monkeypatch.setattr(outbox, "queue_agent_turn", lambda **kw: outbox_calls.append(kw))
    monkeypatch.setattr(outbox, "queue_outbox_message", lambda **kw: outbox_calls.append(kw))

    task_id = "dt-test-short-turnlock"
    tdir = forge._get_job_dir(task_id)
    tdir.mkdir(parents=True, exist_ok=True)
    log_p = tdir / "run.log"
    meta = {
        "task_id": task_id,
        "name": "Short Locked Task",
        "command": "python3 -c 'print(\"done\")'",
        "channel": "zero-chat",
        "timeout": 60,
        "cwd": str(tmp_path),
        "status": "starting",
        "start_time": time.time() - 2.0,
        "notify_on_success": True,
        "synthesize": True,
        "log_file": str(log_p),
    }
    forge._write_job_meta(task_id, meta, db_path=db_p)
    forge._run_job_worker(task_id, db_path=db_p, batch_state_path=bs_file)

    assert len(outbox_calls) > 0
    assert "Suppressing duplicate outbox notification" not in log_p.read_text(encoding="utf-8")

    # Also verify that when offload_dispatched=True in meta and _is_prod_forge_env=True, worker polls/waits for closing turn
    outbox_calls.clear()
    in_flight_checks = [0]

    def _closing_turn(ch):
        in_flight_checks[0] += 1
        return in_flight_checks[0] < 3

    monkeypatch.setattr(forge, "_is_prod_forge_env", lambda *a, **kw: True)
    monkeypatch.setattr(forge, "_is_channel_turn_actively_in_flight", _closing_turn)
    monkeypatch.setattr(forge, "post_or_update_job_widget", lambda *a, **kw: 12345)
    monkeypatch.setattr(forge, "prune_completed_job_widgets", lambda *a, **kw: [])
    monkeypatch.setattr(forge, "sync_forge_channel_header", lambda *a, **kw: None)

    empty_bs = tmp_path / "empty_batch.json"
    task_id2 = "dt-test-closing-turn-wait"
    tdir2 = forge._get_job_dir(task_id2)
    tdir2.mkdir(parents=True, exist_ok=True)
    log_p2 = tdir2 / "run.log"
    meta2 = {
        "task_id": task_id2,
        "name": "Closing Turn Wait Task",
        "command": "python3 -c 'print(\"done2\")'",
        "channel": "zero-chat",
        "timeout": 60,
        "cwd": str(tmp_path),
        "status": "starting",
        "start_time": time.time() - 1.0,
        "notify_on_success": True,
        "synthesize": True,
        "offload_dispatched": True,
        "log_file": str(log_p2),
    }
    forge._write_job_meta(task_id2, meta2, db_path=db_p)
    forge._run_job_worker(task_id2, db_path=db_p, batch_state_path=empty_bs)

    assert in_flight_checks[0] >= 3
    assert len(outbox_calls) == 1


def test_start_job_clears_offload_dispatched_on_exception(tmp_path, monkeypatch):
    """Verify start_job clears offload_dispatched in batch_guard_state.json if job spawn fails."""
    import tools.forge as forge
    bs_file = tmp_path / "batch_guard_state.json"
    bs_file.write_text(json.dumps({"offload_dispatched": True, "offload_kind": "forge.py job start"}), encoding="utf-8")

    monkeypatch.setattr(forge, "_check_recent_repeated_job_failures", lambda *a, **kw: (False, "Circuit breaker tripped"))

    try:
        forge.start_job(
            command="python3 -c 'exit(0)'",
            batch_state_path=bs_file,
            db_path=tmp_path / "forge.db",
        )
        assert False, "Expected RuntimeError from circuit breaker"
    except RuntimeError:
        pass

    state_data = json.loads(bs_file.read_text(encoding="utf-8"))
    assert state_data.get("offload_dispatched") is False
    assert state_data.get("offload_kind") == ""


def test_unscoped_pytest_guard_in_goldfish_preflight(tmp_path):
    """Verify run_goldfish_plan_preflight rejects unconstrained full-suite pytest in verification_cmd."""
    import tools.forge as forge

    unscoped_commands = [
        "pytest",
        "pytest -v",
        "pytest tests",
        "pytest tests/",
        "pytest tools",
        "pytest tools/",
        "pytest .",
        "python3 -m pytest",
        "ruff check . && pytest tests/",
    ]

    for cmd in unscoped_commands:
        plan = {
            "title": f"Plan with {cmd}",
            "repo": "/workspace",
            "origin_channel_id": "1234567890",
            "context_fact_sheet": ["Fact 1"],
            "chunks": [
                {
                    "title": "Chunk 0",
                    "target_files": ["tools/forge.py"],
                    "instructions": "Implement fix and run verification with full suite.",
                    "verification_cmd": cmd,
                }
            ],
        }
        ok, findings = forge.run_goldfish_plan_preflight(plan)
        assert not ok, f"Expected {cmd} to fail goldfish preflight"
        assert any("unscoped full-suite pytest" in f for f in findings), f"Expected unscoped finding for {cmd}"

    scoped_plan = {
        "title": "Scoped Plan",
        "repo": "/workspace",
        "origin_channel_id": "1234567890",
        "context_fact_sheet": ["Fact 1"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["tools/forge.py"],
                "instructions": "Implement scoped fix and verify.",
                "verification_cmd": 'pytest "tools/test_forge.py"',
            }
        ],
    }
    ok_scoped, findings_scoped = forge.run_goldfish_plan_preflight(scoped_plan)
    assert ok_scoped is True, f"Expected scoped plan to pass, got: {findings_scoped}"


def test_stacked_branch_per_chunk_checkout_and_isolation(tmp_path, monkeypatch):
    """Verify multi-chunk builds with stacked target_branch checkout cleanly without bleeding uncommitted files."""
    import subprocess
    import tools.forge as forge_mod

    repo = tmp_path / "stacked_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "forge@brock.ventures"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Forge Test"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("# Main\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo), check=True)

    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    supervisor = forge_mod.ForgeSupervisor(db_path=db_path, max_parallel_chunks=1)

    plan = {
        "title": "Stacked PR Multi-Chunk Build",
        "repo": str(repo),
        "origin_channel_id": "1234567890",
        "context_fact_sheet": ["Architectural invariant"],
        "chunks": [
            {
                "title": "Chunk 1: Add Feature A",
                "target_files": ["feature_a.py"],
                "target_branch": "feat/part-1",
                "instructions": "Create feature_a.py and unit test.",
                "verification_cmd": "true",
            },
            {
                "title": "Chunk 2: Add Feature B stacked on A",
                "target_files": ["feature_b.py"],
                "target_branch": "feat/part-2",
                "depends_on": [0],
                "instructions": "Create feature_b.py stacked on Part 1.",
                "verification_cmd": "true",
            },
        ],
    }

    enqueued = forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)
    build_id = enqueued["build_id"]

    with forge_mod.get_db_connection(db_path) as conn:
        chunk_rows = conn.execute(
            "SELECT id, chunk_index, target_branch FROM forge_chunks WHERE build_id = ? ORDER BY chunk_index",
            (build_id,),
        ).fetchall()

    assert len(chunk_rows) == 2
    assert chunk_rows[0]["target_branch"] == "feat/part-1"
    assert chunk_rows[1]["target_branch"] == "feat/part-2"

    # Create an uncommitted dirty test file in repo that should NOT bleed into chunk branches
    (repo / "dirty_uncommitted_test.py").write_text("# Dirty\n", encoding="utf-8")

    orig_popen = subprocess.Popen

    def _mock_popen(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd):
            cwd = kwargs.get("cwd") or (args[1] if len(args) > 1 else None)
            if cwd and "forge-wt-" in str(cwd):
                wt_p = Path(cwd)
                if "c0" in wt_p.name:
                    (wt_p / "feature_a.py").write_text("def feature_a(): return True\n", encoding="utf-8")
                elif "c1" in wt_p.name:
                    (wt_p / "feature_b.py").write_text("def feature_b(): return True\n", encoding="utf-8")
            return orig_popen(["true"], *args, **kwargs)
        return orig_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _mock_popen)

    # Execute chunk 0
    with forge_mod.get_db_connection(db_path) as conn:
        c0_row = conn.execute("SELECT * FROM forge_chunks WHERE id = ?", (f"{build_id}-c0",)).fetchone()
    ok0 = supervisor.execute_chunk_in_worktree(build_id, c0_row)
    assert ok0 is True

    # Verify feat/part-1 branch was created
    res1 = subprocess.run(["git", "rev-parse", "--verify", "feat/part-1"], cwd=str(repo), capture_output=True)
    assert res1.returncode == 0

    # Execute chunk 1
    with forge_mod.get_db_connection(db_path) as conn:
        c1_row = conn.execute("SELECT * FROM forge_chunks WHERE id = ?", (f"{build_id}-c1",)).fetchone()
    ok1 = supervisor.execute_chunk_in_worktree(build_id, c1_row)
    assert ok1 is True

    # Verify feat/part-2 branch was created
    res2 = subprocess.run(["git", "rev-parse", "--verify", "feat/part-2"], cwd=str(repo), capture_output=True)
    assert res2.returncode == 0

    # Verify default branch ('main') was NOT fast-forwarded by stacked branch build
    main_files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=str(repo), capture_output=True, text=True).stdout
    assert "feature_a.py" not in main_files
    assert "feature_b.py" not in main_files
    assert "README.md" in main_files

    # Verify feat/part-2 has feature_a and feature_b but NOT dirty_uncommitted_test.py
    feat2_files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "feat/part-2"], cwd=str(repo), capture_output=True, text=True).stdout
    assert "feature_a.py" in feat2_files
    assert "feature_b.py" in feat2_files
    assert "dirty_uncommitted_test.py" not in feat2_files


def test_normalize_subrepo_auto_resolution(tmp_path, monkeypatch):
    """Verify _normalize_forge_plan_in_place and run_goldfish_plan_preflight auto-resolve child git repos under /workspace."""
    import tools.forge as forge_mod

    ws_root = _init_test_git_repo(tmp_path / "workspace")
    sub_repo = _init_test_git_repo(ws_root / "mirrormere")
    internal_dir = sub_repo / "internal" / "game"
    internal_dir.mkdir(parents=True, exist_ok=True)
    target_go = internal_dir / "engine.go"
    target_go.write_text("package game\n\nfunc RunEngine() {}\n", encoding="utf-8")

    monkeypatch.setattr(forge_mod, "WORKSPACE_DIR", ws_root)

    # 1. Plan defaults repo to /workspace, target_files exist in /workspace/mirrormere
    plan = {
        "title": "Mirrormere Sub-Repo Feature",
        "repo": "/workspace",
        "origin_channel_id": str(forge_mod.FORGE_CHANNEL_ID),
        "context_fact_sheet": ["Auto-resolve child git repositories under /workspace"],
        "chunks": [
            {
                "title": "Update engine.go",
                "target_files": ["internal/game/engine.go"],
                "target_symbols": ["internal/game/engine.go:RunEngine"],
                "instructions": "Update internal/game/engine.go to improve tick rate.",
                "verification_cmd": "go test ./internal/game/...",
            }
        ],
    }

    forge_mod._normalize_forge_plan_in_place(plan)
    assert Path(plan["repo"]).resolve() == sub_repo.resolve()
    assert Path(plan["chunks"][0]["repo"]).resolve() == sub_repo.resolve()

    ok, msg = forge_mod.validate_forge_plan(plan)
    assert ok is True, f"Expected plan validation to pass after sub-repo resolution, got: {msg}"

    # 2. Direct call to run_goldfish_plan_preflight also auto-resolves child git repo
    plan_direct = {
        "title": "Mirrormere Direct Preflight",
        "repo": str(ws_root),
        "origin_channel_id": str(forge_mod.FORGE_CHANNEL_ID),
        "chunks": [
            {
                "title": "Update engine.go",
                "target_files": ["internal/game/engine.go"],
                "instructions": "Update internal/game/engine.go with new logic.",
            }
        ],
    }
    gf_ok, gf_findings = forge_mod.run_goldfish_plan_preflight(plan_direct)
    assert gf_ok is True, f"Expected direct preflight to pass, got: {gf_findings}"
    assert Path(plan_direct["repo"]).resolve() == sub_repo.resolve()
    assert Path(plan_direct["chunks"][0]["repo"]).resolve() == sub_repo.resolve()


def test_goldfish_preflight_missing_parent_directory_target_file(tmp_path):
    """Verify run_goldfish_plan_preflight flags target files whose parent directory does not exist in ch_repo unless marked as new."""
    import tools.forge as forge_mod

    repo_dir = _init_test_git_repo(tmp_path / "repo")

    # 1. Target file in non-existent parent directory without creation verb is flagged
    plan_missing_dir = {
        "title": "Missing Parent Dir Plan",
        "repo": str(repo_dir),
        "origin_channel_id": str(forge_mod.FORGE_CHANNEL_ID),
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Fix handler logic",
                "target_files": ["nonexistent_pkg/sub/handler.py"],
                "instructions": "Fix timeout handling in nonexistent_pkg/sub/handler.py.",
            }
        ],
    }
    ok, findings = forge_mod.run_goldfish_plan_preflight(plan_missing_dir)
    assert ok is False
    assert any("target file 'nonexistent_pkg/sub/handler.py' does not exist in repository" in f for f in findings)

    # 2. Target file in non-existent parent directory explicitly marked as a new file passes
    plan_new_in_missing_dir = {
        "title": "New File in New Dir Plan",
        "repo": str(repo_dir),
        "origin_channel_id": str(forge_mod.FORGE_CHANNEL_ID),
        "context_fact_sheet": ["Fact"],
        "chunks": [
            {
                "title": "Create nonexistent_pkg/sub/handler.py",
                "target_files": ["nonexistent_pkg/sub/handler.py"],
                "instructions": "Create nonexistent_pkg/sub/handler.py with timeout handler.",
            }
        ],
    }
    ok_new, findings_new = forge_mod.run_goldfish_plan_preflight(plan_new_in_missing_dir)
    assert ok_new is True, f"Expected new file to pass, got: {findings_new}"


def test_goldfish_preflight_rejects_manual_commit_or_push_in_verification_cmd(tmp_path):
    """Verify run_goldfish_plan_preflight rejects verification_cmd strings containing manual git commit or push subcommands."""
    import tools.forge as forge_mod

    repo_dir = _init_test_git_repo(tmp_path / "repo")
    (repo_dir / "mod.py").write_text("X = 1\n", encoding="utf-8")

    bad_cmds = [
        "pytest tools/test_forge.py && git commit -m 'done'",
        "pytest tools/test_forge.py && git push origin main",
        "git -C /workspace commit -am 'manual'",
        "python3 -m py_compile mod.py; git push",
    ]
    for bad_cmd in bad_cmds:
        plan = {
            "title": "Manual Git Commit/Push Verification Plan",
            "repo": str(repo_dir),
            "origin_channel_id": str(forge_mod.FORGE_CHANNEL_ID),
            "context_fact_sheet": ["Fact"],
            "chunks": [
                {
                    "title": "Update mod.py",
                    "target_files": ["mod.py"],
                    "instructions": "Update X = 2 in mod.py.",
                    "verification_cmd": bad_cmd,
                }
            ],
        }
        ok, findings = forge_mod.run_goldfish_plan_preflight(plan)
        assert ok is False, f"Expected verification_cmd '{bad_cmd}' to be rejected"
        assert any("manual git commit/push" in f.lower() or "git commit" in f.lower() or "git push" in f.lower() for f in findings)


def test_bridge_reload_expired_execution_and_expired_decision_feedback(tmp_path, monkeypatch):
    """Verify expired bridge_reload cards can still execute, non-reload expired cards return formatted_embed/card, and expire_stale_decisions patches Discord."""
    import tools.forge as forge_mod

    db_path = tmp_path / "forge_reload.db"
    forge_mod.init_forge_db(db_path)

    fake_data_dir = tmp_path / "forge_data"
    fake_data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(forge_mod, "DATA_DIR", fake_data_dir)

    # 1. Expired bridge_reload card (TTL passed, status still pending) executes cleanly
    d_reload_ttl = forge_mod.stage_decision(
        category="bridge_reload",
        title="Reload Bridge (fg-test-1)",
        summary="Reload bridge after build.",
        handler_type="bridge_reload",
        payload={"build_id": "fg-test-1"},
        channel_id=forge_mod.ZERO_OPS_CHANNEL_ID,
        ttl_seconds=-120.0,
        db_path=db_path,
    )
    res_reload_ttl = forge_mod.execute_decision(
        d_reload_ttl["decision_id"],
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )
    assert res_reload_ttl["ok"] is True
    assert res_reload_ttl["status"] == "approved"
    assert res_reload_ttl["handler_type"] == "bridge_reload"
    assert (fake_data_dir / "reload_bridge.flag").exists()

    # 2. Already-marked 'expired' bridge_reload card also executes cleanly when clicked
    d_reload_exp = forge_mod.stage_decision(
        category="bridge_reload",
        title="Reload Bridge (fg-test-2)",
        summary="Reload bridge after build 2.",
        handler_type="bridge_reload",
        payload={"build_id": "fg-test-2"},
        channel_id=forge_mod.ZERO_OPS_CHANNEL_ID,
        ttl_seconds=-60.0,
        db_path=db_path,
    )
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute(
            "UPDATE forge_decisions SET status = 'expired', message_id = 555666777 WHERE id = ?",
            (d_reload_exp["decision_id"],),
        )
    res_reload_exp = forge_mod.execute_decision(
        d_reload_exp["decision_id"],
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )
    assert res_reload_exp["ok"] is True
    assert res_reload_exp["status"] == "approved"
    assert "Approved & Executed" in res_reload_exp["formatted_card"]
    assert res_reload_exp["formatted_embed"]["title"].startswith("✅")

    # 3. Non-reload expired card (both TTL-expired while pending and already 'expired') returns formatted_embed & formatted_card
    d_shell_exp = forge_mod.stage_decision(
        category="homelab",
        title="Restart Service",
        summary="Run service restart.",
        handler_type="shell",
        payload={"command": "true"},
        channel_id=forge_mod.ZERO_OPS_CHANNEL_ID,
        ttl_seconds=-30.0,
        db_path=db_path,
    )
    res_shell_ttl = forge_mod.execute_decision(
        d_shell_exp["decision_id"],
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )
    assert res_shell_ttl["ok"] is False
    assert "expired" in res_shell_ttl["error"]
    assert "formatted_embed" in res_shell_ttl and res_shell_ttl["formatted_embed"]["title"].startswith("⏱️")
    assert "formatted_card" in res_shell_ttl and "Expired without action" in res_shell_ttl["formatted_card"]

    # Calling execute_decision again when status is already 'expired' also returns formatted_embed & formatted_card
    res_shell_already = forge_mod.execute_decision(
        d_shell_exp["decision_id"],
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )
    assert res_shell_already["ok"] is False
    assert "already expired" in res_shell_already["error"]
    assert "formatted_embed" in res_shell_already and res_shell_already["formatted_embed"]["title"].startswith("⏱️")
    assert "formatted_card" in res_shell_already and "Expired without action" in res_shell_already["formatted_card"]

    # 4. expire_stale_decisions updates Discord message via REST PATCH when message_id & channel_id exist
    d_stale = forge_mod.stage_decision(
        category="homelab",
        title="Stale Card With Message",
        summary="Test REST PATCH on expire.",
        handler_type="shell",
        payload={"command": "true"},
        channel_id=forge_mod.ZERO_OPS_CHANNEL_ID,
        ttl_seconds=-10.0,
        db_path=db_path,
    )
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute("UPDATE forge_decisions SET message_id = 888999000 WHERE id = ?", (d_stale["decision_id"],))

    patch_calls = []

    def _fake_patch(url, headers=None, json=None, timeout=None):
        patch_calls.append({"url": url, "json": json, "timeout": timeout})
        m = MagicMock()
        m.status_code = 200
        return m

    monkeypatch.setattr(forge_mod, "_get_discord_token", lambda *args, **kwargs: "mock-bot-token")
    monkeypatch.setattr(forge_mod.requests, "patch", _fake_patch)

    expired_list = forge_mod.expire_stale_decisions(db_path=db_path)
    assert d_stale["decision_id"] in expired_list
    assert len(patch_calls) == 1
    assert "888999000" in patch_calls[0]["url"]
    assert patch_calls[0]["json"]["embeds"][0]["title"].startswith("⏱️")
    assert all(btn["disabled"] is True for btn in patch_calls[0]["json"]["components"][0]["components"])


def test_forge_load_shed_threshold_and_build_capping(tmp_path, monkeypatch):
    """Verify FORGE_LOAD_SHED_THRESHOLD and FORGE_MIN_AVAILABLE_RAM_MB limit build/chunk worker concurrency."""
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    supervisor = forge_mod.ForgeSupervisor(db_path=db_path, max_parallel_builds=3, max_parallel_chunks=3)

    assert forge_mod.FORGE_LOAD_SHED_THRESHOLD == 12.0
    assert forge_mod.FORGE_MIN_AVAILABLE_RAM_MB == 1800.0
    assert forge_mod.MAX_PARALLEL_BUILDS_PER_AGENT == 3
    assert forge_mod.MAX_PARALLEL_CHUNKS_PER_BUILD == 1

    # 1. Test _get_current_1m_load
    monkeypatch.setattr(forge_mod.os, "getloadavg", lambda: (14.25, 9.5, 6.1))
    assert forge_mod._get_current_1m_load() == 14.25

    def _err_loadavg():
        raise OSError("Load average unavailable")

    monkeypatch.setattr(forge_mod.os, "getloadavg", _err_loadavg)
    assert forge_mod._get_current_1m_load() == 0.0

    # 2. Test _effective_max_builds & _effective_max_chunks in non-prod environment
    monkeypatch.setattr(forge_mod, "_is_prod_forge_env", lambda p: False)
    monkeypatch.setattr(forge_mod, "_get_current_1m_load", lambda: 18.0)
    monkeypatch.setattr(forge_mod, "_get_available_ram_mb", lambda: 900.0)
    monkeypatch.setattr(forge_mod, "_has_active_interactive_turns", lambda: False)
    assert supervisor._effective_max_builds() == 3
    assert supervisor._effective_max_chunks() == 3

    # 3. Test _effective_max_builds & _effective_max_chunks in prod environment
    monkeypatch.setattr(forge_mod, "_is_prod_forge_env", lambda p: True)
    monkeypatch.setattr(forge_mod, "_get_available_ram_mb", lambda: 2500.0)

    # 3a. High load (>= 12.0) caps to 1 regardless of active turns
    monkeypatch.setattr(forge_mod, "_get_current_1m_load", lambda: 12.0)
    monkeypatch.setattr(forge_mod, "_has_active_interactive_turns", lambda: False)
    assert supervisor._effective_max_builds() == 1
    assert supervisor._effective_max_chunks() == 1

    monkeypatch.setattr(forge_mod, "_get_current_1m_load", lambda: 15.5)
    monkeypatch.setattr(forge_mod, "_has_active_interactive_turns", lambda: True)
    assert supervisor._effective_max_builds() == 1
    assert supervisor._effective_max_chunks() == 1

    # 3b. Normal load (< 12.0) with active interactive turns caps to ACTIVE_CHAT_MAX_PARALLEL_*
    monkeypatch.setattr(forge_mod, "_get_current_1m_load", lambda: 4.0)
    monkeypatch.setattr(forge_mod, "_has_active_interactive_turns", lambda: True)
    assert supervisor._effective_max_builds() == forge_mod.ACTIVE_CHAT_MAX_PARALLEL_BUILDS
    assert supervisor._effective_max_chunks() == 1

    # 3c. Normal load (< 12.0) without active interactive turns returns full max_parallel_* when RAM >= 1800 MB
    monkeypatch.setattr(forge_mod, "_get_current_1m_load", lambda: 4.0)
    monkeypatch.setattr(forge_mod, "_has_active_interactive_turns", lambda: False)
    assert supervisor._effective_max_builds() == 3
    assert supervisor._effective_max_chunks() == 3

    # 3d. Low available RAM (< 1800 MB) holds 3rd build slot back (caps builds to 2 and chunks to 1)
    monkeypatch.setattr(forge_mod, "_get_available_ram_mb", lambda: 1450.0)
    assert supervisor._effective_max_builds() == 2
    assert supervisor._effective_max_chunks() == 1

    # 4. Test maybe_spawn_next_build_worker load shedding in prod
    # When load >= 12.0 and active_ids >= 1 -> returns None
    monkeypatch.setattr(supervisor, "_count_active_build_workers", lambda: {"active-build-1"})
    monkeypatch.setattr(forge_mod, "_get_current_1m_load", lambda: 13.0)
    monkeypatch.setattr(forge_mod, "_has_active_interactive_turns", lambda: False)
    assert supervisor.maybe_spawn_next_build_worker() is None

    # When load >= 12.0 and active_ids == 0, but interactive turn is active -> returns None
    monkeypatch.setattr(supervisor, "_count_active_build_workers", lambda: set())
    monkeypatch.setattr(forge_mod, "_has_active_interactive_turns", lambda: True)
    assert supervisor.maybe_spawn_next_build_worker() is None


def test_fast_staged_verify_cmd_symlinks_worktree_node_modules(tmp_path):
    """Verify _build_fast_staged_verify_cmd symlinks web/ui/node_modules from parent repo worktree and leaves git status clean."""
    main_repo = _init_test_git_repo(tmp_path / "main_repo")
    (main_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    ui_dir = main_repo / "web" / "ui" / "src"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (main_repo / "web" / "ui" / "package.json").write_text('{"name": "ui"}\n', encoding="utf-8")
    (ui_dir / "App.tsx").write_text("export const App = () => null;\n", encoding="utf-8")
    scripts_dir = main_repo / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "verify.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=str(main_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add UI and verify.sh"], cwd=str(main_repo), check=True, capture_output=True)

    # Create untracked node_modules in main_repo
    main_nm = main_repo / "web" / "ui" / "node_modules" / ".bin"
    main_nm.mkdir(parents=True, exist_ok=True)

    # Add an isolated git worktree (which starts without untracked web/ui/node_modules)
    wt_dir = tmp_path / "wt_isolated"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feat/ui-test", str(wt_dir), "HEAD"],
        cwd=str(main_repo),
        check=True,
        capture_output=True,
    )
    assert not (wt_dir / "web" / "ui" / "node_modules").exists()

    cmd = forge_mod._build_fast_staged_verify_cmd(wt_dir, changed_files=["web/ui/src/App.tsx"])
    assert (wt_dir / "web" / "ui" / "node_modules").is_symlink()
    assert (wt_dir / "web" / "ui" / "node_modules").resolve() == (main_repo / "web" / "ui" / "node_modules").resolve()
    assert "npx --no-install eslint --max-warnings=0" in cmd
    assert "npm run deadcode" in cmd
    assert "npm run typecheck" in cmd
    assert "sh scripts/verify.sh --staged" not in cmd

    # Verify mixed UI + Python diff does NOT invoke scripts/verify.sh --staged (preventing full-repo vitest coverage fallback)
    ical_dir = wt_dir / "deploy" / "ical-bridge"
    ical_dir.mkdir(parents=True, exist_ok=True)
    (ical_dir / "server.py").write_text("print('ok')\n", encoding="utf-8")
    yt_dir = wt_dir / "web" / "ui" / "src" / "widgets" / "yt-music"
    yt_dir.mkdir(parents=True, exist_ok=True)
    (yt_dir / "YtMusicWidget.tsx").write_text("export const YtMusicWidget = () => null;\n", encoding="utf-8")

    mixed_cmd = forge_mod._build_fast_staged_verify_cmd(
        wt_dir,
        changed_files=["deploy/ical-bridge/server.py", "web/ui/src/widgets/yt-music/YtMusicWidget.tsx"],
    )
    assert "scripts/verify.sh --staged" not in mixed_cmd
    assert "npx --no-install eslint --max-warnings=0" in mixed_cmd
    assert 'python3 -m py_compile "deploy/ical-bridge/server.py"' in mixed_cmd

    # Verify mixed UI + Go diff DOES invoke scripts/verify.sh --staged
    mixed_go_cmd = forge_mod._build_fast_staged_verify_cmd(
        wt_dir,
        changed_files=["internal/server/router.go", "web/ui/src/widgets/yt-music/YtMusicWidget.tsx"],
    )
    assert "scripts/verify.sh --staged" in mixed_go_cmd

    # Remove untracked test files before checking git status --porcelain for symlink hygiene
    (ical_dir / "server.py").unlink()
    (yt_dir / "YtMusicWidget.tsx").unlink()

    # Verify git status --porcelain remains 100% clean in the worktree
    st = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(wt_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    assert st.stdout.strip() == ""


def test_detect_skill_and_rule_drift_ignores_unrelated_same_line_prose_examples(tmp_path):
    """Verify unrelated prose examples on the same line as tools/forge.py do not trigger false drift alerts, while real removed symbols/flags do."""
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "forge.py").write_text(
        'def validate_forge_plan(plan):\n    return "--staged --post"\n',
        encoding="utf-8",
    )
    (tools_dir / "batch_guard.py").write_text(
        "def check_interactive_investigation_gate():\n    return True\n",
        encoding="utf-8",
    )

    rules_dir = tmp_path / ".agents" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "discord_formatting.md").write_text(
        "# Discord Formatting\n"
        "- **NEVER** cite Python file names (`build_public_standings_page.py`, `team_roster_allocator.py`, `tools/forge.py`), "
        "function/method names (`validate_forge_plan()`, `buildSplitStartingNine()`), or selectors (`#drawer-bench-body`).\n"
        "- Reserve inline backticks for file paths (`/workspace/tools/forge.py`), shell commands (`pytest`), or CLI flags (`--unrelated-flag`).\n"
        "- Avoid citing internal files like `tools/forge.py` or frontend helpers like `buildSplitStartingNine()` in chat.\n"
        "- Merge via `gh pr merge --squash --delete-branch` or run `python3 /workspace/tools/forge.py`.\n"
        "- Prevent `tools/forge.py` subprocesses from hanging on `proc.wait()`.\n",
        encoding="utf-8",
    )
    (rules_dir / "crab_cavern_coordination.md").write_text(
        "# Crab Cavern\n"
        "```bash\n"
        "python3 /workspace/tools/forge.py propose --post\n"
        "```\n"
        "- Reply confirming the card was staged for approval with a single tap (without leaking private paths).\n",
        encoding="utf-8",
    )
    (rules_dir / "tool_roundtrip_minimization.md").write_text(
        "# Tool Roundtrips (`batch_guard.py`)\n"
        "- **Interactive Investigation Gate (`check_interactive_investigation_gate()`):** Hand off long probes via `python3 /workspace/tools/forge.py job start`.\n",
        encoding="utf-8",
    )

    assert forge_mod.detect_skill_and_rule_drift(tmp_path, ["tools/forge.py"]) == []

    skills_dir = tmp_path / ".agents" / "skills" / "forge-orchestrator"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(
        "# FORGE Orchestrator\n"
        "- `forge.py` invokes `removed_helper_fn()` during preflight and passes `--stale-flag`.\n"
        "- Explicit target `tools/forge.py:removed_explicit_sym`.\n",
        encoding="utf-8",
    )

    warnings = forge_mod.detect_skill_and_rule_drift(tmp_path, ["tools/forge.py"])
    assert any("--stale-flag" in w for w in warnings)
    assert any("removed_helper_fn" in w for w in warnings)
    assert any("removed_explicit_sym" in w for w in warnings)
    assert not any("buildSplitStartingNine" in w for w in warnings)


def test_live_workspace_forge_skill_and_rule_drift_zero_false_positives():
    """Verify detect_skill_and_rule_drift reports zero warnings on tools/forge.py against live workspace skills and rules."""
    repo_root = Path(__file__).resolve().parent.parent
    warnings = forge_mod.detect_skill_and_rule_drift(repo_root, ["tools/forge.py"])
    assert warnings == []


def test_all_forge_subprocess_run_calls_specify_timeout():
    """Verify every synchronous subprocess call in tools/forge.py includes an explicit timeout keyword argument."""
    import ast

    forge_path = Path(__file__).resolve().parent / "forge.py"
    tree = ast.parse(forge_path.read_text(encoding="utf-8"), filename=str(forge_path))
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in ("run", "call", "check_call", "check_output")
            ):
                kw_names = {kw.arg for kw in node.keywords if kw.arg}
                if "timeout" not in kw_names:
                    missing.append(f"L{node.lineno}: subprocess.{node.func.attr}")
    assert missing == [], f"Unguarded subprocess calls without timeout=: {missing}"


def test_validate_remote_ssh_command_blocks_destructive_nas_commands(monkeypatch):
    """Verify validate_remote_ssh_command blocks destructive remote NAS commands while allowing safe scoped operations."""
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("wt_nas_config", repo_root / "tools" / "nas_config.py")
    nas_cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nas_cfg)

    vol1 = "/" + "volume1"
    docker_root = f"{vol1}/docker"
    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)
    monkeypatch.setenv("NAS_DOCKER_ROOT", docker_root)

    blocked_cmds = [
        # Recursive deletes on root, NAS volumes, and top-level $NAS_DOCKER_ROOT
        "rm -rf /",
        "sudo rm -fr /*",
        "rm -r -f -- /",
        "rm --recursive --force /",
        f"rm -rf {vol1}",
        f"sudo rm -rf {vol1}/",
        f"rm -rf {vol1}/*",
        "rm -rf /volume2",
        "rm -rf $NAS_DOCKER_ROOT",
        "rm -rf ${NAS_DOCKER_ROOT}/",
        'rm -rf "$NAS_DOCKER_ROOT/*"',
        f"rm -rf {docker_root}",
        f"rm -rf {docker_root}/",
        f"rm -rf {docker_root}/*",
        f"rm -rf {docker_root}/mealie/..",
        "rm -rf /docker",
        "rm -rf /docker/*",
        f"cd {vol1} && rm -rf *",
        "cd $NAS_DOCKER_ROOT && rm -rf .",
        # Docker volume rm/prune and docker system prune
        "docker volume rm mealie_data",
        "docker volume remove mealie_data",
        "docker volume prune -f",
        "sudo /usr/local/bin/docker volume prune",
        "docker system prune",
        "sudo /usr/local/bin/docker system prune -af --volumes",
        # Unscoped or volume-destroying docker compose down
        "docker compose down",
        "docker-compose down",
        "sudo /usr/local/bin/docker compose down",
        f"docker compose -f {docker_root}/mealie/docker-compose.yml down -v",
        # Stopping or restarting ContainerManager or dockerd
        "sudo systemctl stop ContainerManager",
        "sudo systemctl restart pkgctl-ContainerManager",
        "systemctl stop docker",
        "systemctl restart docker.service",
        "systemctl restart dockerd",
        "sudo synoservice --stop pkgctl-ContainerManager",
        "sudo synoservice --restart ContainerManager",
        "synoservice restart docker",
        "synopkg stop ContainerManager",
        "synopkg restart ContainerManager",
        "service docker restart",
        "pkill -f dockerd",
        # Host reboot / shutdown / poweroff and synopkg uninstall
        "reboot",
        "sudo reboot",
        "sudo /sbin/shutdown -h now",
        "/sbin/poweroff",
        "systemctl reboot",
        "sudo synopkg uninstall HyperBackup",
        "bash -c 'sudo reboot'",
    ]
    for cmd in blocked_cmds:
        allowed, reason = nas_cfg.validate_remote_ssh_command(cmd)
        assert allowed is False, f"Expected destructive SSH command to be blocked: {cmd}"
        assert "NAS SSH Command Filter Blocked" in reason

    allowed_cmds = [
        "docker ps && uptime",
        f"rm -rf {docker_root}/mealie/cache",
        "rm -rf $NAS_DOCKER_ROOT/mealie/tmp",
        "rm -rf ${NAS_DOCKER_ROOT}/plex_transcode/*",
        "rm -rf /tmp/forge_test_cache",
        f"docker compose -f {docker_root}/mealie/docker-compose.yml down",
        f"docker-compose -f {docker_root}/mealie/docker-compose.yml down",
        "docker compose -p mealie down",
        f"docker compose --project-directory {docker_root}/mealie down",
        "docker compose down mealie",
        "docker restart mealie",
        "docker stop mealie",
        "docker volume ls",
        "docker system df",
        "systemctl status docker",
        "synopkg status ContainerManager",
        "last reboot | head -n 5",
    ]
    for cmd in allowed_cmds:
        allowed, reason = nas_cfg.validate_remote_ssh_command(cmd)
        assert allowed is True, f"Expected safe/scoped SSH command to pass: {cmd} (got: {reason})"


def test_run_ssh_blocks_destructive_commands_and_honors_approved_card_bypass(monkeypatch):
    """Verify run_ssh returns returncode=126 without spawning ssh unless FORGE_APPROVED_CARD_EXECUTION=1."""
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("wt_nas_config_run", repo_root / "tools" / "nas_config.py")
    nas_cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nas_cfg)

    spawn_calls = []

    def _fake_run(cmd, **kwargs):
        spawn_calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = "approved-ok\n"
        m.stderr = ""
        return m

    monkeypatch.setattr(nas_cfg.subprocess, "run", _fake_run)
    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)

    # 1. Blocked before spawning ssh when FORGE_APPROVED_CARD_EXECUTION is not '1'
    rc, out, err = nas_cfg.run_ssh("203.0.113.10", "docker system prune -af", user="admin", port="22")
    assert rc == 126
    assert out == ""
    assert "NAS SSH Command Filter Blocked" in err
    assert spawn_calls == []

    # 2. Safe remote command spawns ssh normally
    rc_safe, out_safe, err_safe = nas_cfg.run_ssh("203.0.113.10", "docker ps", user="admin", port="22")
    assert rc_safe == 0
    assert out_safe == "approved-ok\n"
    assert err_safe == ""
    assert len(spawn_calls) == 1

    # 3. FORGE_APPROVED_CARD_EXECUTION='1' cleanly bypasses the filter for approved destructive commands
    monkeypatch.setenv("FORGE_APPROVED_CARD_EXECUTION", "1")
    ok_val, reason_val = nas_cfg.validate_remote_ssh_command("docker system prune -af")
    assert ok_val is True and reason_val == ""

    rc_app, out_app, err_app = nas_cfg.run_ssh("203.0.113.10", "docker system prune -af", user="admin", port="22")
    assert rc_app == 0
    assert out_app == "approved-ok\n"
    assert err_app == ""
    assert len(spawn_calls) == 2


def test_execute_approval_card_passes_forge_approved_card_execution_env(tmp_path, monkeypatch):
    """Verify execute_approval_card sets FORGE_APPROVED_CARD_EXECUTION=1 so approved Decision Cards pass both gates."""
    import tools.forge as forge_mod

    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("PYTHONPATH", str(repo_root))

    db_path = tmp_path / "forge_approval_env.db"
    forge_mod.init_forge_db(db_path)

    probe_py = (
        f"{sys.executable} -c \""
        "import os, sys; "
        f"sys.path.insert(0, {str(repo_root)!r}); "
        "import tools.batch_guard as bg, tools.nas_config as nc; "
        "flag = os.environ.get('FORGE_APPROVED_CARD_EXECUTION'); "
        "bg_ok, _ = bg.check_destructive_system_and_exposure_gate('docker system prune -af'); "
        "nc_ok, _ = nc.validate_remote_ssh_command('docker system prune -af'); "
        "assert flag == '1' and bg_ok is True and nc_ok is True, (flag, bg_ok, nc_ok); "
        "print('BOTH_GATES_PASSED')"
        "\""
    )

    staged = forge_mod.stage_decision(
        category="maintenance",
        title="Approved NAS Prune",
        summary="Prune unused Docker containers with human approval.",
        handler_type="shell_cmd",
        payload={"command": probe_py, "cwd": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    assert staged["ok"] is True

    res = forge_mod.execute_approval_card(
        staged["decision_id"],
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )
    assert res["ok"] is True, f"Expected approved Decision Card execution to succeed, got: {res}"
    assert res["status"] == "approved"
    assert "BOTH_GATES_PASSED" in res["receipt"]
    # Ensure approval env var did not leak into the parent process environment
    assert os.environ.get("FORGE_APPROVED_CARD_EXECUTION") is None

    # Verify docker_compose and file_delete subprocess handlers also receive FORGE_APPROVED_CARD_EXECUTION=1
    captured_envs = []

    def _capture_subproc_run(cmd, **kwargs):
        captured_envs.append((cmd, kwargs.get("env") or {}))
        m = MagicMock()
        m.returncode = 0
        m.stdout = "ok"
        m.stderr = ""
        return m

    monkeypatch.setattr(forge_mod.subprocess, "run", _capture_subproc_run)
    monkeypatch.setattr(forge_mod.shutil, "which", lambda cmd: "/usr/bin/docker" if cmd == "docker" else None)

    d_compose = forge_mod.stage_decision(
        category="homelab",
        title="Compose Restart",
        summary="Restart Mealie stack.",
        handler_type="docker_compose",
        payload={"service": "mealie", "action": "restart", "compose_dir": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    res_compose = forge_mod.execute_decision(
        d_compose["decision_id"],
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )
    assert res_compose["ok"] is True
    assert captured_envs[-1][1].get("FORGE_APPROVED_CARD_EXECUTION") == "1"

    d_del = forge_mod.stage_decision(
        category="file_delete",
        title="Approved Delete",
        summary="Remove old archive.",
        handler_type="file_delete",
        payload={"command": "rm -f /tmp/old_archive.tar", "cwd": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    res_del = forge_mod.execute_approval_card(
        d_del["decision_id"],
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )
    assert res_del["ok"] is True
    assert captured_envs[-1][1].get("FORGE_APPROVED_CARD_EXECUTION") == "1"


def test_worker_model_attempt_escalation_and_fallback_ladder():
    """Verify FORGE worker ladder escalates Hydrogen High -> Neon Medium -> Neon High and updates fallback map."""
    import tools.forge as forge_mod

    assert forge_mod.DEFAULT_WORKER_MODEL == "gemini-4-hydrogen-high"
    assert forge_mod.ESCALATION_WORKER_MODEL == "gemini-4-neon-medium"
    assert forge_mod.FINAL_ESCALATION_WORKER_MODEL == "gemini-4-neon-high"
    assert forge_mod.PREFLIGHT_ARCHITECT_MODEL == "gemini-4-neon-medium"

    assert forge_mod.WORKER_MODEL_FALLBACK_MAP["gemini-4-hydrogen-high"] == "gemini-4-neon-medium"
    assert forge_mod.WORKER_MODEL_FALLBACK_MAP["gemini-4-hydrogen-xhigh"] == "gemini-4-neon-medium"
    assert forge_mod.WORKER_MODEL_FALLBACK_MAP["gemini-4-neon-medium"] == "gemini-4-neon-high"
    assert forge_mod.WORKER_MODEL_FALLBACK_MAP["gemini-4-neon-high"] == "gemini-4-argon-high"
    assert forge_mod.WORKER_MODEL_FALLBACK_MAP["gemini-4-argon-low"] == "gemini-4-neon-medium"
    assert forge_mod.WORKER_MODEL_FALLBACK_MAP["gemini-4-argon-medium"] == "gemini-4-neon-medium"
    assert forge_mod.WORKER_MODEL_FALLBACK_MAP["gemini-4-argon-high"] == "gemini-4-neon-high"
    assert forge_mod.WORKER_MODEL_FALLBACK_MAP["gemini-4-carbon-high"] == "gemini-4-neon-high"

    assert forge_mod.resolve_worker_model_for_attempt(None, attempt=1, max_attempts=3) == "gemini-4-hydrogen-high"
    assert forge_mod.resolve_worker_model_for_attempt(None, attempt=2, max_attempts=3) == "gemini-4-neon-medium"
    assert forge_mod.resolve_worker_model_for_attempt(None, attempt=3, max_attempts=3) == "gemini-4-neon-high"
    assert forge_mod.resolve_worker_model_for_attempt("gemini-4-hydrogen-xhigh", attempt=2, max_attempts=3) == "gemini-4-neon-medium"
    assert forge_mod.resolve_worker_model_for_attempt("gemini-4-argon-low", attempt=2, max_attempts=3) == "gemini-4-neon-medium"
    assert forge_mod.resolve_worker_model_for_attempt("gemini-4-argon-medium", attempt=2, max_attempts=3) == "gemini-4-neon-medium"
    assert forge_mod.resolve_worker_model_for_attempt("gemini-4-neon-high", attempt=2, max_attempts=3) == "gemini-4-neon-high"
    assert forge_mod.resolve_worker_model_for_attempt("gemini-4-carbon-high", attempt=2, max_attempts=3) == "gemini-4-carbon-high"

    assert (
        forge_mod.resolve_worker_model_fallback("gemini-4-hydrogen-high", attempt=1, max_attempts=3)
        == "gemini-4-neon-medium"
    )
    assert (
        forge_mod.resolve_worker_model_fallback("gemini-4-hydrogen-xhigh", attempt=1, max_attempts=3)
        == "gemini-4-neon-medium"
    )
    assert (
        forge_mod.resolve_worker_model_fallback("gemini-4-neon-medium", attempt=1, max_attempts=3)
        == "gemini-4-neon-high"
    )
    assert (
        forge_mod.resolve_worker_model_fallback("gemini-4-neon-high", attempt=1, max_attempts=3)
        == "gemini-4-argon-high"
    )
    assert (
        forge_mod.resolve_worker_model_fallback("gemini-4-hydrogen-high", attempt=2, max_attempts=3)
        == "gemini-4-neon-medium"
    )
    assert (
        forge_mod.resolve_worker_model_fallback(
            "gemini-4-neon-medium",
            attempt=2,
            max_attempts=3,
            rejected_models={"gemini-4-hydrogen-high", "gemini-4-neon-medium"},
        )
        == "gemini-4-neon-high"
    )
    assert (
        forge_mod.resolve_worker_model_fallback("gemini-4-neon-medium", attempt=3, max_attempts=3)
        == "gemini-4-neon-high"
    )
    assert (
        forge_mod.resolve_worker_model_fallback(
            "gemini-4-neon-high",
            attempt=3,
            max_attempts=3,
            rejected_models={"gemini-4-hydrogen-high", "gemini-4-neon-medium", "gemini-4-neon-high"},
        )
        == "gemini-4-argon-high"
    )


def test_should_run_carbon_preflight_gate():
    """Verify Carbon (High) pre-flight eligibility rules and explicit review_spec overrides."""
    non_core_chunk = [{"target_files_json": json.dumps(["docs/README.md"])}]
    non_core_build = {"touches_core_bridge": 0, "plan_json": json.dumps({})}

    # 1-chunk non-core build returns False
    assert forge_mod.should_run_carbon_preflight(non_core_build, non_core_chunk) is False
    assert (
        forge_mod.should_run_carbon_preflight(
            {"touches_core_bridge": 0, "plan_json": None},
            [{"target_files": ["scripts/helper.py"]}],
        )
        is False
    )

    # 2-chunk non-core build returns False; 3-chunk non-core build returns True
    two_chunks = [
        {"target_files_json": json.dumps(["docs/a.md"])},
        {"target_files_json": json.dumps(["docs/b.md"])},
    ]
    assert forge_mod.should_run_carbon_preflight(non_core_build, two_chunks) is False

    three_chunks = [
        {"target_files_json": json.dumps(["docs/a.md"])},
        {"target_files_json": json.dumps(["docs/b.md"])},
        {"target_files_json": json.dumps(["docs/c.md"])},
    ]
    assert forge_mod.should_run_carbon_preflight(non_core_build, three_chunks) is True

    # 1-chunk build with touches_core_bridge=1 or core target_files returns True
    assert (
        forge_mod.should_run_carbon_preflight(
            {"touches_core_bridge": 1, "plan_json": "{}"},
            non_core_chunk,
        )
        is True
    )
    assert (
        forge_mod.should_run_carbon_preflight(
            non_core_build,
            [{"target_files_json": json.dumps(["tools/forge.py"])}],
        )
        is True
    )
    assert (
        forge_mod.should_run_carbon_preflight(
            non_core_build,
            [{"target_files": ["/workspace/tools/forge.py"]}],
        )
        is True
    )
    assert (
        forge_mod.should_run_carbon_preflight(
            non_core_build,
            [{"target_files_json": json.dumps(["tools/bridge_models.py"])}],
        )
        is True
    )

    # Explicit plan_json review_spec overrides eligibility in both directions
    override_false_build = {"touches_core_bridge": 1, "plan_json": json.dumps({"review_spec": False})}
    assert forge_mod.should_run_carbon_preflight(override_false_build, three_chunks) is False
    assert (
        forge_mod.should_run_carbon_preflight(
            override_false_build,
            [{"target_files_json": json.dumps(["tools/forge.py"])}],
        )
        is False
    )

    override_true_build = {"touches_core_bridge": 0, "plan_json": json.dumps({"review_spec": True})}
    assert forge_mod.should_run_carbon_preflight(override_true_build, non_core_chunk) is True
    assert forge_mod.should_run_carbon_preflight(override_true_build, two_chunks) is True


def test_apply_carbon_preflight_refinements(tmp_path):
    """Verify Carbon (High) pre-flight notes append idempotently and target_symbols_json merges cleanly."""
    repo = _init_test_git_repo(tmp_path / "repo")
    (repo / "sample.py").write_text(
        "def initial_fn():\n    return 1\n\n\ndef refined_fn():\n    return 2\n\n\ndef extra_fn():\n    return 3\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "sample.py"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Add sample.py"], cwd=str(repo), capture_output=True, check=True)

    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)

    build_id = "fg-test-carbon-preflight"
    plan = {
        "id": build_id,
        "title": "Carbon Pre-Flight Refinement Test",
        "repo": str(repo),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Preserve existing function contracts."],
        "chunks": [
            {
                "title": "Update sample module",
                "target_files": ["sample.py"],
                "target_symbols": ["sample.py:initial_fn"],
                "instructions": "Modify initial_fn in sample.py with deterministic validation.",
                "verification_cmd": "python3 -m py_compile sample.py",
            },
            {
                "title": "Update documentation",
                "target_files": ["README.md"],
                "instructions": "Document the updated validation rules in README.md.",
                "verification_cmd": "true",
            },
        ],
    }
    enq = forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)
    assert enq["ok"] is True

    c0_id = f"{build_id}-c0"
    c1_id = f"{build_id}-c1"
    review_payload = {
        "summary": "Clarify input validation and hydrate refined_fn.",
        "chunk_refinements": {
            c0_id: {
                "review_notes": "- Guard against empty inputs.\n- Keep return type deterministic.",
                "target_symbols": {
                    "sample.py": ["initial_fn", "refined_fn"],
                },
            },
        },
    }

    refined_count = forge_mod.apply_carbon_preflight_refinements(build_id, review_payload, db_path=db_path)
    assert refined_count == 1

    with forge_mod.get_db_connection(db_path) as conn:
        c0_row = conn.execute(
            "SELECT instructions, target_symbols_json FROM forge_chunks WHERE id = ?",
            (c0_id,),
        ).fetchone()
        c1_row = conn.execute(
            "SELECT instructions, target_symbols_json FROM forge_chunks WHERE id = ?",
            (c1_id,),
        ).fetchone()

    expected_notes_block = (
        "Modify initial_fn in sample.py with deterministic validation.\n\n"
        "### Carbon (High) Pre-Flight Review Notes:\n"
        "- Guard against empty inputs.\n"
        "- Keep return type deterministic."
    )
    assert c0_row["instructions"] == expected_notes_block
    assert json.loads(c0_row["target_symbols_json"]) == ["sample.py:initial_fn", "sample.py:refined_fn"]
    assert c1_row["instructions"] == "Document the updated validation rules in README.md."
    assert json.loads(c1_row["target_symbols_json"]) == []

    # Idempotency: re-applying identical payload performs zero updates and never duplicates notes header
    refined_repeat = forge_mod.apply_carbon_preflight_refinements(build_id, review_payload, db_path=db_path)
    assert refined_repeat == 0

    with forge_mod.get_db_connection(db_path) as conn:
        c0_after_repeat = conn.execute(
            "SELECT instructions, target_symbols_json FROM forge_chunks WHERE id = ?",
            (c0_id,),
        ).fetchone()
    assert c0_after_repeat["instructions"] == expected_notes_block
    assert c0_after_repeat["instructions"].count("### Carbon (High) Pre-Flight Review Notes:") == 1
    assert json.loads(c0_after_repeat["target_symbols_json"]) == ["sample.py:initial_fn", "sample.py:refined_fn"]

    # Subsequent refinement with a new symbol still merges symbols without duplicating review notes header
    second_payload = {
        "chunk_refinements": {
            c0_id: {
                "review_notes": "- Should not duplicate header.",
                "target_symbols": ["sample.py:extra_fn"],
            },
        },
    }
    refined_sym_only = forge_mod.apply_carbon_preflight_refinements(build_id, second_payload, db_path=db_path)
    assert refined_sym_only == 1

    with forge_mod.get_db_connection(db_path) as conn:
        c0_after_second = conn.execute(
            "SELECT instructions, target_symbols_json FROM forge_chunks WHERE id = ?",
            (c0_id,),
        ).fetchone()
    assert c0_after_second["instructions"] == expected_notes_block
    assert c0_after_second["instructions"].count("### Carbon (High) Pre-Flight Review Notes:") == 1
    assert json.loads(c0_after_second["target_symbols_json"]) == [
        "sample.py:initial_fn",
        "sample.py:refined_fn",
        "sample.py:extra_fn",
    ]


def _make_fake_path_mapper(path_map: dict[str, Path]):
    """Return a Path wrapper mapping specific absolute container paths to tmp_path files."""
    real_path_cls = Path

    def _mapped_path(raw, *args, **kwargs):
        p = real_path_cls(raw, *args, **kwargs)
        key = str(p)
        if key in path_map:
            return path_map[key]
        if key.startswith(("/secrets/", "/opt/zero/", "/docker/", "/app/", "/workspace/.env")):
            return path_map.get("__missing__", real_path_cls("/nonexistent-forge-test-path") / p.name)
        return p

    return _mapped_path


def test_baseball_token_never_falls_back_to_zero_outside_ivy(tmp_path, monkeypatch):
    """Ensure _get_discord_token(for_channel_id=BASEBALL_CHANNEL_ID) never returns Zero's token outside Ivy."""
    zero_secrets = tmp_path / "zero_secrets.json"
    zero_secrets.write_text(json.dumps({"DISCORD_BOT_TOKEN": "zero-secret-tok"}), encoding="utf-8")
    zero_env = tmp_path / "zero.env"
    zero_env.write_text("DISCORD_BOT_TOKEN=zero-dotenv-tok\n", encoding="utf-8")

    monkeypatch.setattr(forge_mod, "is_ivy_container", lambda: False)
    monkeypatch.setenv("AGENT_ROLE", "vulcan")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "zero-env-tok")
    monkeypatch.delenv("IVY_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("IVY_BOT_TOKEN", raising=False)

    path_map = {
        "/secrets/env.json": zero_secrets,
        "/opt/zero/secrets/env.json": zero_secrets,
        "/opt/zero/.env": zero_env,
        "__missing__": tmp_path / "missing_file",
    }
    monkeypatch.setattr(forge_mod, "Path", _make_fake_path_mapper(path_map))
    monkeypatch.setattr("tools.deliver_image.get_discord_token", lambda: "zero-deliver-tok")

    # No Ivy token configured -> must return None (never Zero's token)
    assert forge_mod._get_discord_token(for_channel_id=forge_mod.BASEBALL_CHANNEL_ID) is None
    # Non-baseball cross-channel call still resolves Zero's token normally
    assert forge_mod._get_discord_token(for_channel_id=forge_mod.ZERO_OPS_CHANNEL_ID) == "zero-secret-tok"

    # Dedicated Ivy env / secrets / .env are honored when present
    ivy_env = tmp_path / "ivy.env"
    ivy_env.write_text("DISCORD_BOT_TOKEN=ivy-dotenv-tok\n", encoding="utf-8")
    path_map["/docker/discord-ivy-agent/.env"] = ivy_env
    assert forge_mod._get_discord_token(for_channel_id=forge_mod.BASEBALL_CHANNEL_ID) == "ivy-dotenv-tok"

    monkeypatch.setenv("IVY_DISCORD_TOKEN", "ivy-explicit-env-tok")
    assert forge_mod._get_discord_token(for_channel_id=forge_mod.BASEBALL_CHANNEL_ID) == "ivy-explicit-env-tok"


def test_baseball_token_prefers_ivy_env_inside_ivy_container(tmp_path, monkeypatch):
    """Ensure _get_discord_token inside Ivy prefers Ivy's own token and ignores /opt/zero/.env."""
    zero_secrets = tmp_path / "zero_secrets.json"
    zero_secrets.write_text(json.dumps({"DISCORD_BOT_TOKEN": "zero-secret-tok"}), encoding="utf-8")
    zero_env = tmp_path / "zero.env"
    zero_env.write_text("DISCORD_BOT_TOKEN=zero-dotenv-tok\n", encoding="utf-8")
    app_env = tmp_path / "app.env"
    app_env.write_text("DISCORD_BOT_TOKEN=ivy-app-dotenv-tok\n", encoding="utf-8")

    monkeypatch.setattr(forge_mod, "is_ivy_container", lambda: True)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("IVY_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("IVY_BOT_TOKEN", raising=False)

    path_map = {
        "/secrets/env.json": zero_secrets,
        "/opt/zero/secrets/env.json": zero_secrets,
        "/opt/zero/.env": zero_env,
        "/app/.env": app_env,
        "__missing__": tmp_path / "missing_file",
    }
    monkeypatch.setattr(forge_mod, "Path", _make_fake_path_mapper(path_map))
    monkeypatch.setattr("tools.deliver_image.get_discord_token", lambda: "zero-deliver-tok")

    # Picks up Ivy's /app/.env even though /opt/zero/.env and /secrets/env.json have Zero's token
    assert forge_mod._get_discord_token(for_channel_id=forge_mod.BASEBALL_CHANNEL_ID) == "ivy-app-dotenv-tok"
    # Even with for_channel_id=None inside Ivy, resolves to Ivy's token (never Zero's)
    assert forge_mod._get_discord_token() == "ivy-app-dotenv-tok"

    # os.getenv("DISCORD_BOT_TOKEN") takes precedence inside Ivy
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "ivy-runtime-env-tok")
    assert forge_mod._get_discord_token(for_channel_id=forge_mod.BASEBALL_CHANNEL_ID) == "ivy-runtime-env-tok"

    # Without any Ivy token at all, returns None rather than falling back to Zero
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    path_map.pop("/app/.env")
    assert forge_mod._get_discord_token(for_channel_id=forge_mod.BASEBALL_CHANNEL_ID) is None


def test_baseball_decision_card_rest_skips_when_no_ivy_token(tmp_path, monkeypatch):
    """Ensure _post_decision_card_rest passes for_channel_id=BASEBALL_CHANNEL_ID and skips posting without Ivy token."""
    db_path = tmp_path / "forge_baseball_rest.db"
    forge_mod.init_forge_db(db_path)

    dec = forge_mod.stage_decision(
        category="bridge_reload",
        title="Reload Bridge (fg-bb-rest)",
        summary="Reload bridge in-place.",
        handler_type="bridge_reload",
        payload={"build_id": "fg-bb-rest"},
        channel_id=forge_mod.BASEBALL_CHANNEL_ID,
        db_path=db_path,
        post_to_discord=False,
    )
    did = dec["decision_id"]

    seen_calls = []
    post_calls = []

    def _fake_get_token(for_forge_channel=False, for_channel_id=None):
        seen_calls.append({"for_forge_channel": for_forge_channel, "for_channel_id": for_channel_id})
        return None

    def _fake_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = {"id": "12345"}
        return m

    monkeypatch.setattr(forge_mod, "_get_discord_token", _fake_get_token)
    monkeypatch.setattr(forge_mod.requests, "post", _fake_post)

    res_msg_id = forge_mod._post_decision_card_rest(did, db_path=db_path)
    assert res_msg_id is None
    assert seen_calls == [{"for_forge_channel": False, "for_channel_id": forge_mod.BASEBALL_CHANNEL_ID}]
    assert post_calls == []

    with forge_mod.get_db_connection(db_path) as conn:
        row = conn.execute("SELECT message_id FROM forge_decisions WHERE id = ?", (did,)).fetchone()
    assert row["message_id"] is None

    # expire_stale_decisions also passes for_channel_id=BASEBALL_CHANNEL_ID and skips PATCH without Ivy token
    patch_calls = []
    monkeypatch.setattr(forge_mod.requests, "patch", lambda *a, **kw: patch_calls.append((a, kw)))
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute(
            "UPDATE forge_decisions SET message_id = 777888, expires_at = ? WHERE id = ?",
            (time.time() - 60.0, did),
        )
    seen_calls.clear()
    expired = forge_mod.expire_stale_decisions(db_path=db_path)
    assert did in expired
    assert seen_calls == [{"for_forge_channel": False, "for_channel_id": forge_mod.BASEBALL_CHANNEL_ID}]
    assert patch_calls == []


def test_baseball_decision_card_bridge_reload_deferred_to_ivy_outbox(tmp_path, monkeypatch):
    """Ensure #baseball bridge_reload decisions defer REST posting to Ivy's outbox poller when Vulcan lacks Ivy's token."""
    db_path = tmp_path / "forge_baseball_defer.db"
    forge_mod.init_forge_db(db_path)

    build_id = "fg-bb-defer-1"
    now = time.time()
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO forge_builds (
                id, title, repo, origin_channel_id, origin_thread_id,
                touches_core_bridge, status, plan_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 1, 'completed', '{}', ?, ?)
            """,
            (build_id, "Baseball Bridge Update", str(tmp_path), forge_mod.BASEBALL_CHANNEL_ID, now, now),
        )
        conn.execute(
            """
            INSERT INTO forge_chunks (
                id, build_id, chunk_index, title, target_files_json, target_symbols_json,
                prohibited_files_json, instructions, verification_cmd, status
            ) VALUES (?, ?, 0, 'Chunk 0', '[]', '[]', '[]', 'Do work', 'true', 'completed')
            """,
            (f"{build_id}-c0", build_id),
        )

    monkeypatch.setattr(forge_mod, "_get_discord_token", lambda for_forge_channel=False, for_channel_id=None: None)

    rest_post_calls = []
    pending_calls = []
    outbox_calls = []

    monkeypatch.setattr(forge_mod, "_post_decision_card_rest", lambda *a, **kw: rest_post_calls.append((a, kw)))
    monkeypatch.setattr(forge_mod, "post_pending_decision_cards", lambda *a, **kw: pending_calls.append((a, kw)) or [])
    monkeypatch.setattr("tools.outbox.queue_outbox_message", lambda **kw: outbox_calls.append(kw) or {"ok": True})

    ok = forge_mod.dispatch_build_completion_to_origin(build_id, db_path=db_path)
    assert ok is True
    assert rest_post_calls == []
    assert pending_calls == []
    assert len(outbox_calls) == 1
    assert outbox_calls[0]["channel"] == forge_mod.BASEBALL_CHANNEL_ID
    assert outbox_calls[0]["msg_type"] == "agent_turn"

    with forge_mod.get_db_connection(db_path) as conn:
        dec_row = conn.execute(
            "SELECT * FROM forge_decisions WHERE category = 'bridge_reload' AND channel_id = ?",
            (forge_mod.BASEBALL_CHANNEL_ID,),
        ).fetchone()
    assert dec_row is not None
    assert dec_row["status"] == "pending"
    assert dec_row["message_id"] is None


def test_heartbeat_tracker_incremental_transcript_and_scandir_throttle(tmp_path, monkeypatch):
    """Verify HeartbeatTracker scans transcript incrementally and strictly tracks only spawned child subagent IDs without os.scandir(brain_dir)."""
    brain_root = tmp_path / "brain"
    conv_id = "conv-main"
    logs_dir = brain_root / conv_id / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    tfile = logs_dir / "transcript.jsonl"
    tfile.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "hello"}) + "\n",
        encoding="utf-8",
    )

    # Create spawned child subagent transcripts + an unrelated session transcript
    sub1_logs = brain_root / "conv-sub-1" / ".system_generated" / "logs"
    sub1_logs.mkdir(parents=True, exist_ok=True)
    sub1_tfile = sub1_logs / "transcript.jsonl"
    sub1_tfile.write_text('{"type": "PLANNER_RESPONSE", "content": "sub1"}\n', encoding="utf-8")

    unrelated_logs = brain_root / "conv-unrelated-other" / ".system_generated" / "logs"
    unrelated_logs.mkdir(parents=True, exist_ok=True)
    (unrelated_logs / "transcript.jsonl").write_text('{"type": "PLANNER_RESPONSE", "content": "other"}\n', encoding="utf-8")

    fake_now = [1000.0]
    monkeypatch.setattr(forge_mod.time, "time", lambda: fake_now[0])
    os.utime(sub1_tfile, (1000.0, 1000.0))
    os.utime(unrelated_logs / "transcript.jsonl", (1000.0, 1000.0))

    scandir_calls = []
    real_scandir = os.scandir

    def _spy_scandir(path):
        if Path(path) == brain_root:
            scandir_calls.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(forge_mod.os, "scandir", _spy_scandir)

    tracker = HeartbeatTracker(conv_id=conv_id, brain_dir=brain_root)
    sig1 = tracker.compute_disk_signature()
    assert tracker._has_subagents is False
    assert tracker._last_transcript_offset == tfile.stat().st_size
    assert len(scandir_calls) == 0
    assert sig1[0] == 1  # only main transcript

    # Append non-subagent bytes; verify incremental seek skips already-scanned prefix
    prev_offset = tracker._last_transcript_offset
    seek_positions = []
    real_open = open

    def _spy_open(file, mode="r", *args, **kwargs):
        fh = real_open(file, mode, *args, **kwargs)
        if str(file) == str(tfile) and "b" in mode:
            orig_seek = fh.seek

            def _tracked_seek(pos, whence=0):
                seek_positions.append(pos)
                return orig_seek(pos, whence)

            fh.seek = _tracked_seek
        return fh

    with tfile.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "working"}) + "\n")

    with patch("builtins.open", side_effect=_spy_open):
        sig2 = tracker.compute_disk_signature()

    assert seek_positions == [prev_offset]
    assert tracker._has_subagents is False
    assert tracker._last_transcript_offset == tfile.stat().st_size
    assert len(scandir_calls) == 0
    assert sig2[0] == 1

    # Append invoke_subagent + child conv-sub-1 result; scans ONLY conv-sub-1 (never unrelated sessions or os.scandir)
    fake_now[0] = 1005.0
    with tfile.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "invoke_subagent"}]}) + "\n")
        f.write(json.dumps({
            "step_index": 3,
            "type": "GENERIC",
            "content": 'Created the following subagent:\n{\n  "conversationId": "conv-sub-1"\n}\n',
        }) + "\n")

    sig3 = tracker.compute_disk_signature()
    assert tracker._has_subagents is True
    assert tracker._subagent_conv_ids == {"conv-sub-1"}
    assert len(scandir_calls) == 0
    assert sig3[0] == 2  # main transcript + conv-sub-1 only (unrelated session ignored!)

    # Second child conv-sub-2 is ignored until explicitly spawned in transcript
    fake_now[0] = 1020.0
    sub2_logs = brain_root / "conv-sub-2" / ".system_generated" / "logs"
    sub2_logs.mkdir(parents=True, exist_ok=True)
    sub2_tfile = sub2_logs / "transcript.jsonl"
    sub2_tfile.write_text('{"type": "PLANNER_RESPONSE", "content": "sub2"}\n', encoding="utf-8")
    os.utime(sub2_tfile, (1020.0, 1020.0))

    sig4 = tracker.compute_disk_signature()
    assert len(scandir_calls) == 0
    assert sig4[0] == 2  # conv-sub-2 not spawned by this turn yet

    # Once conv-sub-2 is spawned by invoke_subagent, it IS included immediately
    fake_now[0] = 1036.0
    with tfile.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 4, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "invoke_subagent"}]}) + "\n")
        f.write(json.dumps({
            "step_index": 5,
            "type": "GENERIC",
            "content": 'Created the following subagent:\n{\n  "conversationId": "conv-sub-2"\n}\n',
        }) + "\n")
    sig5 = tracker.compute_disk_signature()
    assert len(scandir_calls) == 0
    assert tracker._subagent_conv_ids == {"conv-sub-1", "conv-sub-2"}
    assert sig5[0] == 3

    # File shrink resets _last_transcript_offset and subagent IDs cleanly
    tfile.write_text('{"step_index": 0, "type": "USER_INPUT", "content": "reset"}\n', encoding="utf-8")
    tracker.compute_disk_signature()
    assert tracker._last_transcript_offset == tfile.stat().st_size
    assert tracker._has_subagents is False
    assert tracker._subagent_conv_ids == set()


def test_check_runaway_tool_loop_tail_read_large_transcript(tmp_path):
    """Verify check_runaway_tool_loop detects loops via bounded tail read on >200KB transcripts."""
    brain_root = tmp_path / "brain"
    conv_id = "conv-large-tail"
    logs_dir = brain_root / conv_id / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    tfile = logs_dir / "transcript.jsonl"

    # Write >220KB of historical padding lines
    filler_line = json.dumps({"type": "PLANNER_RESPONSE", "content": "x" * 2048}) + "\n"
    padding = filler_line * 115  # ~235KB
    assert len(padding.encode("utf-8")) > 200_000

    tail_entries = [
        {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "run tests"},
    ]
    for _ in range(3):
        tail_entries.append({
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest fail.py"}}],
        })
        tail_entries.append({
            "type": "GENERIC",
            "content": "Command exited with code 1.\nError: assertion failed",
        })
    tail_text = "\n".join(json.dumps(e) for e in tail_entries) + "\n"
    tfile.write_text(padding + tail_text, encoding="utf-8")

    is_loop, reason = check_runaway_tool_loop(conv_id, brain_dir=brain_root, threshold=3)
    assert is_loop is True
    assert "Runaway tool loop" in reason

    # Appending a fresh USER_INPUT + clean response clears the active turn loop
    with tfile.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "try again"}) + "\n")
        f.write(json.dumps({"type": "PLANNER_RESPONSE", "content": "Fixed cleanly."}) + "\n")

    is_loop_after, _ = check_runaway_tool_loop(conv_id, brain_dir=brain_root, threshold=3)
    assert is_loop_after is False


def test_stage_decision_uses_cryptographic_hex_ids_and_preserves_retry_bypass(tmp_path, monkeypatch):
    import re
    import tools.forge as forge_mod

    db_path = tmp_path / "forge_sec_ids.db"
    forge_mod.init_forge_db(db_path)

    ids = set()
    for i in range(10):
        res = forge_mod.stage_decision(
            category="homelab",
            title=f"Action {i}",
            summary="Verify random token_hex decision ID.",
            handler_type="shell",
            payload={"command": "true", "note": f"n{i}"},
            channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
            db_path=db_path,
        )
        assert res["ok"] is True
        did = res["decision_id"]
        assert re.fullmatch(r"dec-[0-9a-f]{12}-homelab", did), f"Unexpected decision_id format: {did}"
        ids.add(did)
    assert len(ids) == 10

    # Verify is_retry auto-start bypass is still preserved without FORGE_APPROVED_CARD_EXECUTION=1
    monkeypatch.delenv("FORGE_APPROVED_CARD_EXECUTION", raising=False)
    retry_plan = {
        "title": "Retry Multi-Chunk Build",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "is_retry": True,
        "chunks": [
            {"title": "C1", "target_files": ["tools/a.py"], "instructions": "c1"},
            {"title": "C2", "target_files": ["tools/b.py"], "instructions": "c2"},
        ],
    }
    req_retry, reasons_retry = forge_mod.evaluate_plan_approval_gate(
        retry_plan,
        batch_state_path="/tmp/nonexistent.json",
        in_flight_path=tmp_path / "missing.json",
        db_path=db_path,
    )
    assert req_retry is False
    assert reasons_retry == []


def test_docker_compose_handler_rejects_shell_injection_and_uses_shell_false(tmp_path, monkeypatch):
    import tools.forge as forge_mod

    db_path = tmp_path / "forge_dc_sec.db"
    forge_mod.init_forge_db(db_path)

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        m = MagicMock()
        m.returncode = 0
        m.stdout = "ok"
        m.stderr = ""
        return m

    monkeypatch.setattr(forge_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(forge_mod.shutil, "which", lambda cmd: "/usr/bin/docker" if cmd == "docker" else None)

    # 1. Legitimate allowlisted actions execute via shell=False with explicit argv list
    for action, expected_tokens in (
        ("up", ["up"]),
        ("up -d", ["up", "-d"]),
        ("start", ["start"]),
        ("stop", ["stop"]),
        ("pull", ["pull"]),
        ("restart", ["restart"]),
    ):
        d = forge_mod.stage_decision(
            category="homelab",
            title=f"Compose {action}",
            summary="Safe compose action.",
            handler_type="docker_compose",
            payload={"service": "mealie_app-1.0", "action": action, "compose_dir": str(tmp_path)},
            channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
            db_path=db_path,
        )
        res = forge_mod.execute_decision(d["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
        assert res["ok"] is True, f"Expected action '{action}' to succeed, got: {res}"
        last_cmd, last_kwargs = calls[-1]
        assert last_cmd == ["docker", "compose", *expected_tokens, "mealie_app-1.0"]
        assert last_kwargs.get("shell") is False

    # 2. Shell injection and disallowed actions / invalid service names are rejected without invoking subprocess
    calls_before = len(calls)
    malicious_payloads = [
        {"service": "mealie; rm -rf /", "action": "restart", "compose_dir": str(tmp_path)},
        {"service": "$(whoami)", "action": "up -d", "compose_dir": str(tmp_path)},
        {"service": "mealie && cat /secrets/env.json", "action": "start", "compose_dir": str(tmp_path)},
        {"service": "-f", "action": "restart", "compose_dir": str(tmp_path)},
        {"service": "mealie", "action": "down", "compose_dir": str(tmp_path)},
        {"service": "mealie", "action": "up -d; id", "compose_dir": str(tmp_path)},
        {"service": "mealie", "action": "exec", "compose_dir": str(tmp_path)},
    ]
    for bad_payload in malicious_payloads:
        d_bad = forge_mod.stage_decision(
            category="homelab",
            title="Malicious Compose",
            summary="Must be rejected.",
            handler_type="docker_compose",
            payload=bad_payload,
            channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
            db_path=db_path,
        )
        res_bad = forge_mod.execute_decision(d_bad["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
        assert res_bad["ok"] is False, f"Expected payload {bad_payload} to be rejected"
        assert res_bad["status"] == "failed"
    assert len(calls) == calls_before, "subprocess.run must never be invoked for rejected docker_compose payloads"


def test_docker_compose_handler_falls_back_to_host2_ssh_when_docker_absent(tmp_path, monkeypatch):
    import tools.forge as forge_mod
    import tools.nas_config as nas_config_mod

    db_path = tmp_path / "forge_dc_ssh.db"
    forge_mod.init_forge_db(db_path)

    ssh_calls = []

    def _fake_run_ssh(host, remote_cmd, timeout=120, **kwargs):
        ssh_calls.append((host, remote_cmd, timeout, os.environ.get("FORGE_APPROVED_CARD_EXECUTION")))
        return 0, "started", ""

    monkeypatch.setattr(forge_mod.shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(nas_config_mod, "HOST_2_IP", "198.51.100.84")
    monkeypatch.setattr(nas_config_mod, "HOST_1_IP", "198.51.100.82")
    monkeypatch.setattr(nas_config_mod, "run_ssh", _fake_run_ssh)

    d_ok = forge_mod.stage_decision(
        category="homelab",
        title="Restart Baseball Stack",
        summary="Approving restarts baseball worker on Host 2 via SSH fallback.",
        handler_type="docker_compose",
        payload={
            "service": "baseball_worker",
            "action": "up -d",
            "compose_dir": "/opt/stacks/baseball",
            "compose_file": "docker-compose.yml",
        },
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    res_ok = forge_mod.execute_decision(d_ok["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
    assert res_ok["ok"] is True
    assert "via SSH" in res_ok["receipt"]
    assert os.environ.get("FORGE_APPROVED_CARD_EXECUTION") is None
    assert len(ssh_calls) == 1
    host_called, cmd_called, timeout_called, approved_flag = ssh_calls[0]
    assert host_called == "198.51.100.84"
    assert timeout_called == 60
    assert approved_flag == "1"
    assert "cd /opt/stacks/baseball" in cmd_called
    assert "docker compose -f docker-compose.yml up -d baseball_worker" in cmd_called

    # Disallowed actions and invalid service names must still be rejected before SSH
    d_bad = forge_mod.stage_decision(
        category="homelab",
        title="Bad Compose Action",
        summary="Approving must reject disallowed action.",
        handler_type="docker_compose",
        payload={"service": "baseball; id", "action": "restart", "compose_dir": "/opt/stacks/baseball"},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    res_bad = forge_mod.execute_decision(d_bad["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
    assert res_bad["ok"] is False
    assert len(ssh_calls) == 1


def test_dismiss_decision_cas_guard_and_stale_executing_recovery(tmp_path):
    import tools.forge as forge_mod

    db_path = tmp_path / "forge_cas_sec.db"
    forge_mod.init_forge_db(db_path)

    # 1. Pending card can be dismissed once; second dismiss fails cleanly
    d_pend = forge_mod.stage_decision(
        category="ops",
        title="Pending Card",
        summary="Dismiss me.",
        handler_type="shell",
        payload={"command": "true", "cwd": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    r_dis1 = forge_mod.dismiss_decision(d_pend["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
    assert r_dis1["ok"] is True
    assert r_dis1["status"] == "dismissed"

    r_dis2 = forge_mod.dismiss_decision(d_pend["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
    assert r_dis2["ok"] is False
    assert r_dis2["status"] == "dismissed"

    # 2. Approved card CANNOT be overwritten by dismiss_decision
    d_app = forge_mod.stage_decision(
        category="ops",
        title="Approved Card",
        summary="Already approved.",
        handler_type="shell",
        payload={"command": "true", "cwd": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    r_exec = forge_mod.execute_decision(d_app["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
    assert r_exec["ok"] is True
    assert r_exec["status"] == "approved"

    r_dis_app = forge_mod.dismiss_decision(d_app["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
    assert r_dis_app["ok"] is False
    assert r_dis_app["status"] == "approved"
    with forge_mod.get_db_connection(db_path) as conn:
        assert conn.execute("SELECT status FROM forge_decisions WHERE id = ?", (d_app["decision_id"],)).fetchone()["status"] == "approved"

    # 3. Executing card CANNOT be overwritten by dismiss_decision
    d_exec = forge_mod.stage_decision(
        category="ops",
        title="Executing Card",
        summary="Currently executing.",
        handler_type="shell",
        payload={"command": "true", "cwd": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    now = time.time()
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute(
            "UPDATE forge_decisions SET status = 'executing', executing_started_at = ? WHERE id = ?",
            (now, d_exec["decision_id"]),
        )
    r_dis_exec = forge_mod.dismiss_decision(d_exec["decision_id"], user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
    assert r_dis_exec["ok"] is False
    assert r_dis_exec["status"] == "executing"

    # 4. Fresh executing card (< 15 min) is NOT reaped, while stale executing card (> 15 min) IS recovered
    d_stale = forge_mod.stage_decision(
        category="ops",
        title="Stale Executing Card",
        summary="Stuck in executing.",
        handler_type="shell",
        payload={"command": "true", "cwd": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute(
            "UPDATE forge_decisions SET status = 'executing', executing_started_at = ? WHERE id = ?",
            (now - 1000.0, d_stale["decision_id"]),
        )

    expired_or_recovered = forge_mod.expire_stale_decisions(db_path=db_path)
    assert d_stale["decision_id"] in expired_or_recovered
    assert d_exec["decision_id"] not in expired_or_recovered

    with forge_mod.get_db_connection(db_path) as conn:
        stale_row = conn.execute("SELECT status, receipt_text FROM forge_decisions WHERE id = ?", (d_stale["decision_id"],)).fetchone()
        fresh_row = conn.execute("SELECT status FROM forge_decisions WHERE id = ?", (d_exec["decision_id"],)).fetchone()
    assert stale_row["status"] == "failed"
    assert "stale executing" in stale_row["receipt_text"].lower()
    assert fresh_row["status"] == "executing"

    # 5. Boot reconciliation also recovers stale executing decisions
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute(
            "UPDATE forge_decisions SET status = 'executing', executing_started_at = ? WHERE id = ?",
            (now - 1200.0, d_exec["decision_id"]),
        )
    reconcile_res = forge_mod.reconcile_forge_on_boot(db_path=db_path)
    assert d_exec["decision_id"] in reconcile_res.get("recovered_decisions", [])


def test_rebase_conflict_never_overwrites_base_ref(tmp_path, monkeypatch):
    """Verify rebase conflict onto an advanced base_ref aborts cleanly without overwriting sibling commits."""
    repo = _init_test_git_repo(tmp_path / "repo")
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(forge_mod, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_completion_to_origin", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_failure_to_origin", lambda *a, **kw: None)

    plan = {
        "id": "fg-rebase-conflict",
        "title": "Rebase Conflict Safety",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Preserve sibling commit on base_ref"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["README.md"],
                "instructions": "Update README.md with verified changes and ensure git history remains intact.",
                "verification_cmd": "true",
            },
            {
                "title": "Chunk 1",
                "target_files": ["other.py"],
                "instructions": "Implement unrelated pending chunk changes in other.py cleanly.",
                "verification_cmd": "true",
            },
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)
    staging_ref = "forge/release-fg-rebase-conflict"
    sibling_shas = []

    class ConflictPopen:
        def __init__(self, cmd, cwd=None, **kwargs):
            self.cwd = Path(cwd)
            # Advance staging_ref (simulating a parallel sibling chunk landing first) with a conflicting edit
            sibling_wt = tmp_path / f"sib-{len(sibling_shas)}"
            subprocess.run(["git", "worktree", "add", "--detach", str(sibling_wt), staging_ref], cwd=str(repo), check=True, capture_output=True)
            (sibling_wt / "README.md").write_text(f"# Sibling Commit {len(sibling_shas)}\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", f"sibling {len(sibling_shas)}"], cwd=str(sibling_wt), check=True, capture_output=True)
            sib_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(sibling_wt), check=True, capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "branch", "-f", staging_ref, sib_sha], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", str(sibling_wt)], cwd=str(repo), check=True, capture_output=True)
            sibling_shas.append(sib_sha)
            # Worker writes conflicting edit on the stale worktree base
            (self.cwd / "README.md").write_text("# Conflicting Worker Edit\n", encoding="utf-8")
            self.stdout = iter([])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    real_popen = subprocess.Popen

    def _side_effect(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd):
            return ConflictPopen(cmd, *args, **kwargs)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _side_effect)
    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    with forge_mod.get_db_connection(db_path) as conn:
        c0 = conn.execute("SELECT * FROM forge_chunks WHERE id = 'fg-rebase-conflict-c0'").fetchone()

    ok = sup.execute_chunk_in_worktree("fg-rebase-conflict", c0)
    assert ok is False

    # base_ref MUST still point to the latest sibling commit and MUST NOT have been overwritten
    final_base_sha = subprocess.run(["git", "rev-parse", staging_ref], cwd=str(repo), check=True, capture_output=True, text=True).stdout.strip()
    assert final_base_sha == sibling_shas[-1]
    base_readme = subprocess.run(["git", "show", f"{staging_ref}:README.md"], cwd=str(repo), check=True, capture_output=True, text=True).stdout
    assert "Sibling Commit" in base_readme

    with forge_mod.get_db_connection(db_path) as conn:
        c0_after = conn.execute("SELECT status, last_error FROM forge_chunks WHERE id = 'fg-rebase-conflict-c0'").fetchone()
        b_after = conn.execute("SELECT status FROM forge_builds WHERE id = 'fg-rebase-conflict'").fetchone()
    assert c0_after["status"] == "failed"
    assert "rebase" in c0_after["last_error"].lower()
    assert b_after["status"] == "failed"


def test_execute_chunk_fails_cleanly_on_push_rejection(tmp_path, monkeypatch):
    """Verify non-fast-forward remote push rejection fails the chunk/build and records stderr instead of marking merged/completed."""
    bare_remote = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)

    local_repo = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare_remote), str(local_repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(local_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(local_repo), check=True, capture_output=True)
    (local_repo / "README.md").write_text("# Initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(local_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=str(local_repo), check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(local_repo), check=True, capture_output=True)

    # Prepare a peer clone to advance remote main concurrently during worker execution
    peer_repo = tmp_path / "peer"
    subprocess.run(["git", "clone", str(bare_remote), str(peer_repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Peer"], cwd=str(peer_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "peer@example.com"], cwd=str(peer_repo), check=True, capture_output=True)

    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(forge_mod, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_completion_to_origin", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_failure_to_origin", lambda *a, **kw: None)

    plan = {
        "id": "fg-push-reject",
        "title": "Push Rejection Guard",
        "repo": str(local_repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Test push return code handling"],
        "chunks": [
            {
                "title": "Edit app.py",
                "target_files": ["app.py"],
                "instructions": "Create app.py with constant X = 1 and verify clean git push behavior.",
                "verification_cmd": "true",
            }
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)

    # Also simulate push failing by pushing a non-conflicting commit to bare_remote AFTER local_repo fetched before merge
    orig_run = subprocess.run
    pushed_peer = [False]

    def _spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "git" and cmd[1] == "push" and not pushed_peer[0]:
            pushed_peer[0] = True
            (peer_repo / "peer_only.txt").write_text("remote ahead\n", encoding="utf-8")
            orig_run(["git", "add", "."], cwd=str(peer_repo), check=True, capture_output=True)
            orig_run(["git", "commit", "-m", "remote advance"], cwd=str(peer_repo), check=True, capture_output=True)
            orig_run(["git", "push", "origin", "main"], cwd=str(peer_repo), check=True, capture_output=True)
        return orig_run(cmd, *args, **kwargs)

    class WorkerPopen:
        def __init__(self, cmd, cwd=None, **kwargs):
            (Path(cwd) / "app.py").write_text("X = 1\n", encoding="utf-8")
            self.stdout = iter([])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "run", _spy_run)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda cmd, *a, **kw: WorkerPopen(cmd, *a, **kw)
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd)
        else real_popen(cmd, *a, **kw),
    )

    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    with forge_mod.get_db_connection(db_path) as conn:
        c0 = conn.execute("SELECT * FROM forge_chunks WHERE id = 'fg-push-reject-c0'").fetchone()

    ok = sup.execute_chunk_in_worktree("fg-push-reject", c0)
    assert ok is False

    with forge_mod.get_db_connection(db_path) as conn:
        c0_after = conn.execute("SELECT status, last_error FROM forge_chunks WHERE id = 'fg-push-reject-c0'").fetchone()
        b_after = conn.execute("SELECT status FROM forge_builds WHERE id = 'fg-push-reject'").fetchone()
    assert c0_after["status"] == "failed"
    assert "git push origin main failed" in c0_after["last_error"]
    assert "rejected" in c0_after["last_error"].lower() or "non-fast-forward" in c0_after["last_error"].lower()
    assert b_after["status"] == "failed"


def test_prepare_clean_repo_never_resets_dirty_workspace(tmp_path, monkeypatch):
    """Verify _prepare_clean_repo_and_sync_base preserves dirty WORKSPACE_DIR edits when before_merge=False even if origin/main is ahead."""
    bare_remote = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare_remote)], check=True, capture_output=True)

    ws_repo = tmp_path / "workspace"
    subprocess.run(["git", "clone", str(bare_remote), str(ws_repo)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(ws_repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(ws_repo), check=True, capture_output=True)
    (ws_repo / "README.md").write_text("# Base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(ws_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=str(ws_repo), check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(ws_repo), check=True, capture_output=True)

    # Advance origin/main from a peer clone
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", str(bare_remote), str(peer)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Peer"], cwd=str(peer), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "peer@example.com"], cwd=str(peer), check=True, capture_output=True)
    (peer / "README.md").write_text("# Remote Newer\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "Peer advance"], cwd=str(peer), check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(peer), check=True, capture_output=True)

    # Leave uncommitted local edits in ws_repo (acting as WORKSPACE_DIR)
    (ws_repo / "README.md").write_text("# Uncommitted Local Edit\n", encoding="utf-8")
    monkeypatch.setattr(forge_mod, "WORKSPACE_DIR", ws_repo)

    latest_ref = forge_mod._prepare_clean_repo_and_sync_base(ws_repo, "main", before_merge=False)
    assert latest_ref == "origin/main"
    # Local uncommitted edits MUST survive intact (no git reset --hard!)
    assert (ws_repo / "README.md").read_text(encoding="utf-8") == "# Uncommitted Local Edit\n"


def test_cross_container_worktree_lock_and_reconcile_preservation(tmp_path, monkeypatch):
    """Verify _ensure_chunk_worktree locks the worktree with forge-active-<build_id> and reconcile_forge_on_boot preserves active locks under repo lock."""
    import fcntl

    repo = _init_test_git_repo(tmp_path / "repo")
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "WORKSPACE_DIR", repo)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(forge_mod, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_completion_to_origin", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_failure_to_origin", lambda *a, **kw: None)

    plan = {
        "id": "fg-wt-lock-test",
        "title": "Worktree Lock Test",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Verify worktree lock"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["README.md"],
                "instructions": "Update README.md inside a locked FORGE worktree and verify lock cleanup.",
                "verification_cmd": "true",
            }
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)

    observed_lock_reason = []

    class InspectLockPopen:
        def __init__(self, cmd, cwd=None, **kwargs):
            wt_p = Path(cwd)
            lock_f = repo / ".git" / "worktrees" / wt_p.name / "locked"
            if lock_f.exists():
                observed_lock_reason.append(lock_f.read_text(encoding="utf-8").strip())
            (wt_p / "README.md").write_text("# Locked Worktree Edit\n", encoding="utf-8")
            self.stdout = iter([])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda cmd, *a, **kw: InspectLockPopen(cmd, *a, **kw)
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd)
        else real_popen(cmd, *a, **kw),
    )

    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    with forge_mod.get_db_connection(db_path) as conn:
        c0 = conn.execute("SELECT * FROM forge_chunks WHERE id = 'fg-wt-lock-test-c0'").fetchone()

    assert sup.execute_chunk_in_worktree("fg-wt-lock-test", c0) is True
    assert observed_lock_reason == ["forge-active-fg-wt-lock-test"]
    assert not (repo / ".git" / "worktrees" / "forge-wt-fg-wt-lock-test-c0").exists()

    # Simulate Vulcan holding a locked worktree whose /tmp path does not exist in Zero's container namespace
    active_bid = "fg-peer-active"
    peer_wt = tmp_path / f"forge-wt-{active_bid}-c0"
    subprocess.run(["git", "worktree", "add", "-b", f"forge/{active_bid}-c0", str(peer_wt), "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "worktree", "lock", "--reason", f"forge-active-{active_bid}", str(peer_wt)], cwd=str(repo), check=True, capture_output=True)
    # Remove peer_wt directory to simulate a different container's /tmp mount namespace
    import shutil
    shutil.rmtree(peer_wt)

    lock_calls = []
    orig_acquire = forge_mod._acquire_repo_git_lock

    def _spy_acquire(rdir):
        lock_calls.append(Path(rdir).resolve())
        return orig_acquire(rdir)

    monkeypatch.setattr(forge_mod, "_acquire_repo_git_lock", _spy_acquire)

    worker_lock_path = db_path.parent / f".forge_build_worker_{active_bid}.lock"
    with open(worker_lock_path, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            forge_mod.reconcile_forge_on_boot(db_path=db_path)
            assert repo.resolve() in lock_calls
            # Locked metadata MUST survive boot reconciliation while active_bid holds its worker flock
            assert (repo / ".git" / "worktrees" / f"forge-wt-{active_bid}-c0" / "locked").exists()
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    # Clean up simulated worktree lock after releasing worker flock
    forge_mod.reconcile_forge_on_boot(db_path=db_path)
    assert not (repo / ".git" / "worktrees" / f"forge-wt-{active_bid}-c0").exists()


def test_heartbeat_tracker_missing_transcript_and_baseline_step_scoping(tmp_path):
    """Verify HeartbeatTracker returns False when transcript is missing and ignores pre-baseline or unrelated subagents."""
    brain_root = tmp_path / "brain"
    conv_id = "conv-scoped"
    conv_path = brain_root / conv_id / ".system_generated"
    (conv_path / "logs").mkdir(parents=True, exist_ok=True)

    # Pre-existing unrelated session in brain_dir
    unrelated = brain_root / "conv-unrelated" / ".system_generated" / "logs"
    unrelated.mkdir(parents=True, exist_ok=True)
    (unrelated / "transcript.jsonl").write_text('{"type": "PLANNER_RESPONSE"}\n', encoding="utf-8")

    # 1. Missing transcript.jsonl MUST return False (not brain_dir.exists())
    tracker_missing = HeartbeatTracker(conv_id=conv_id, brain_dir=brain_root, baseline_step_index=5)
    assert tracker_missing._check_transcript_has_subagents(conv_path, conv_id) is False
    assert tracker_missing.compute_disk_signature() == (0, 0.0, 0)

    # 2. Subagent spawned at step <= baseline_step_index MUST be ignored; only step > baseline_step_index is tracked
    old_child = brain_root / "child-old" / ".system_generated" / "logs"
    old_child.mkdir(parents=True, exist_ok=True)
    (old_child / "transcript.jsonl").write_text('{"type": "PLANNER_RESPONSE"}\n', encoding="utf-8")

    new_child = brain_root / "child-new" / ".system_generated" / "logs"
    new_child.mkdir(parents=True, exist_ok=True)
    (new_child / "transcript.jsonl").write_text('{"type": "PLANNER_RESPONSE"}\n', encoding="utf-8")

    tfile = conv_path / "logs" / "transcript.jsonl"
    lines = [
        {"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "invoke_subagent"}]},
        {"step_index": 3, "type": "GENERIC", "content": 'Created the following subagent:\n{"conversationId": "child-old"}\n'},
        {"step_index": 6, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "invoke_subagent"}]},
        {"step_index": 7, "type": "GENERIC", "content": 'Created the following subagent:\n{"conversationId": "child-new"}\n'},
    ]
    tfile.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

    tracker = HeartbeatTracker(conv_id=conv_id, brain_dir=brain_root, baseline_step_index=5)
    sig = tracker.compute_disk_signature()
    assert tracker._subagent_conv_ids == {"child-new"}
    assert sig[0] == 2  # main transcript + child-new only (child-old and conv-unrelated excluded)


def test_run_build_pipeline_fails_on_unmet_or_cyclic_depends_on(tmp_path, monkeypatch):
    """Verify run_build_pipeline never bypasses unmet/cyclic depends_on and marks build failed cleanly."""
    repo = _init_test_git_repo(tmp_path / "repo")
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(forge_mod, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_failure_to_origin", lambda *a, **kw: None)

    plan = {
        "id": "fg-cyclic-deps",
        "title": "Cyclic Depends On Build",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Cyclic dep test"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["a.py"],
                "instructions": "Implement module a.py after dependency resolution succeeds cleanly.",
                "verification_cmd": "true",
            },
            {
                "title": "Chunk 1",
                "target_files": ["b.py"],
                "instructions": "Implement module b.py after dependency resolution succeeds cleanly.",
                "verification_cmd": "true",
            },
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)
    # Inject cyclic depends_on directly in SQLite: chunk 0 depends on chunk 1, chunk 1 depends on chunk 0
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute("UPDATE forge_chunks SET depends_on_json = '[1]' WHERE id = 'fg-cyclic-deps-c0'")
        conn.execute("UPDATE forge_chunks SET depends_on_json = '[0]' WHERE id = 'fg-cyclic-deps-c1'")

    executed_chunks = []
    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    monkeypatch.setattr(sup, "execute_chunk_in_worktree", lambda bid, ch: executed_chunks.append(ch["id"]) or True)

    ok = sup.run_build_pipeline("fg-cyclic-deps")
    assert ok is False
    assert executed_chunks == []  # Blocked chunks MUST NOT be executed out of order!

    with forge_mod.get_db_connection(db_path) as conn:
        b_row = conn.execute("SELECT status FROM forge_builds WHERE id = 'fg-cyclic-deps'").fetchone()
        c_rows = conn.execute("SELECT status, last_error FROM forge_chunks WHERE build_id = 'fg-cyclic-deps'").fetchall()
    assert b_row["status"] == "failed"
    assert all(r["status"] == "failed" for r in c_rows)
    assert all("depends_on" in (r["last_error"] or "") for r in c_rows)


def test_maybe_spawn_next_build_worker_closes_parent_log_fd(tmp_path, monkeypatch):
    """Verify maybe_spawn_next_build_worker closes the parent log file handle immediately after Popen."""
    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(forge_mod, "is_ivy_container", lambda: False)

    plan = {
        "id": "fg-fd-leak-check",
        "title": "FD Closure Check",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["FD test"],
        "chunks": [
            {
                "title": "C0",
                "target_files": ["a.py"],
                "instructions": "Verify build worker spawning closes parent log file descriptor cleanly.",
                "verification_cmd": "true",
            }
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)

    captured_stdout = []

    class DummyProc:
        pid = 999999

        def poll(self):
            return None

    def _fake_popen(cmd, **kwargs):
        captured_stdout.append(kwargs.get("stdout"))
        return DummyProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    monkeypatch.setattr(sup, "should_execute_builds", lambda: True)

    spawned = sup.maybe_spawn_next_build_worker()
    assert spawned == "fg-fd-leak-check"
    assert len(captured_stdout) == 1
    assert captured_stdout[0].closed is True


def test_run_job_worker_cancel_escalates_sigterm_to_sigkill(tmp_path, monkeypatch):
    """Verify _run_job_worker_locked escalates SIGTERM -> wait(1.0) -> SIGKILL when a cancelled child ignores SIGTERM."""
    import signal

    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "_is_prod_forge_env", lambda *a, **kw: False)
    monkeypatch.setattr(forge_mod, "sync_forge_channel_header", lambda *a, **kw: None)

    task_id = "dt-cancel-escalate"
    tdir = forge_mod._get_job_dir(task_id)
    tdir.mkdir(parents=True, exist_ok=True)
    log_p = tdir / "run.log"
    meta = {
        "task_id": task_id,
        "name": "Stubborn Task",
        "command": "sleep 60",
        "channel": "zero-chat",
        "timeout": 120,
        "cwd": str(tmp_path),
        "status": "running",
        "start_time": time.time(),
        "notify_on_success": False,
        "synthesize": False,
        "log_file": str(log_p),
    }
    forge_mod._write_job_meta(task_id, meta, db_path=db_path)

    signals_sent = []
    wait_calls = [0]

    class StubbornProc:
        pid = 424242

        def wait(self, timeout=None):
            wait_calls[0] += 1
            if wait_calls[0] == 1:
                # First wait slice expires -> triggers cancellation check
                meta["status"] = "cancelled"
                forge_mod._write_job_meta(task_id, meta, db_path=db_path)
                raise subprocess.TimeoutExpired(cmd="sleep 60", timeout=timeout)
            if wait_calls[0] == 2:
                # Ignores SIGTERM during 1.0s grace wait -> must trigger SIGKILL escalation
                assert timeout == 1.0
                raise subprocess.TimeoutExpired(cmd="sleep 60", timeout=timeout)
            return -9

        def terminate(self):
            signals_sent.append(("terminate", self.pid))

        def kill(self):
            signals_sent.append(("kill", self.pid))

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: StubbornProc())
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals_sent.append((sig, pgid)))

    forge_mod._run_job_worker_locked(task_id, meta, db_path=db_path)
    assert (signal.SIGTERM, 424242) in signals_sent
    assert (signal.SIGKILL, 424242) in signals_sent
    assert signals_sent.index((signal.SIGTERM, 424242)) < signals_sent.index((signal.SIGKILL, 424242))


def test_decision_edit_modal_rejects_non_owner_before_ui_mutation(tmp_path, monkeypatch):
    """Verify ForgeDecisionEditModal.on_submit blocks unauthorized users before disabling buttons or executing."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    db_path = tmp_path / "forge.db"
    forge_mod.init_forge_db(db_path)

    exec_called = []
    monkeypatch.setattr(forge_mod, "execute_decision", lambda *a, **kw: exec_called.append(a) or {"ok": True})

    async def _run():
        modal = forge_mod.ForgeDecisionEditModal("dec-owner-guard", {"cmd": "echo hi"}, db_path=db_path)
        interaction = MagicMock()
        interaction.user.id = forge_mod.OWNER_USER_ID + 12345
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        await modal.on_submit(interaction)

        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()
        interaction.message.edit.assert_not_awaited()
        assert exec_called == []

    asyncio.run(_run())


def test_execute_approval_card_cas_fences_out_late_worker_after_stale_recovery(tmp_path, monkeypatch):
    """Verify late execute_approval_card completion cannot overwrite a decision recovered as 'failed'."""
    db_path = tmp_path / "forge_cas_stale.db"
    forge_mod.init_forge_db(db_path)

    staged = forge_mod.stage_decision(
        category="homelab",
        title="Slow Approved Command",
        summary="Simulate slow execution recovered before completion.",
        handler_type="shell_cmd",
        payload={"command": "echo done", "cwd": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    decision_id = staged["decision_id"]

    recovered_ids_seen = []

    def _slow_subprocess_run(*args, **kwargs):
        # While worker is running subprocess, simulate stale timeout recovery in DB
        with forge_mod.get_db_connection(db_path) as conn:
            row = conn.execute("SELECT executing_started_at FROM forge_decisions WHERE id = ?", (decision_id,)).fetchone()
            assert row is not None and row["executing_started_at"] is not None
            conn.execute(
                "UPDATE forge_decisions SET executing_started_at = ? WHERE id = ?",
                (float(row["executing_started_at"]) - 1200.0, decision_id),
            )
        recovered_ids_seen.extend(
            forge_mod.recover_stale_executing_decisions(db_path=db_path, stale_seconds=900.0)
        )
        m = MagicMock()
        m.returncode = 0
        m.stdout = "late success"
        m.stderr = ""
        return m

    monkeypatch.setattr(forge_mod.subprocess, "run", _slow_subprocess_run)

    res = forge_mod.execute_approval_card(
        decision_id,
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )

    assert recovered_ids_seen == [decision_id]
    assert res["ok"] is False
    assert res["fenced"] is True
    assert res["status"] == "failed"
    assert "fenced out" in res["error"].lower()

    with forge_mod.get_db_connection(db_path) as conn:
        final_row = conn.execute("SELECT * FROM forge_decisions WHERE id = ?", (decision_id,)).fetchone()
    assert final_row["status"] == "failed"
    assert "recovered from stale executing state" in final_row["receipt_text"]


def test_execute_approval_card_cas_fences_out_stale_worker_when_token_superseded(tmp_path, monkeypatch):
    """Verify completion UPDATE checks exact executing_started_at token even if status is still 'executing'."""
    db_path = tmp_path / "forge_cas_token.db"
    forge_mod.init_forge_db(db_path)

    staged = forge_mod.stage_decision(
        category="homelab",
        title="Superseded Execution",
        summary="Verify executing_started_at mismatch fences completion.",
        handler_type="shell_cmd",
        payload={"command": "echo ok", "cwd": str(tmp_path)},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=db_path,
    )
    decision_id = staged["decision_id"]
    newer_exec_ts = time.time() + 50.0

    def _superseded_subprocess_run(*args, **kwargs):
        with forge_mod.get_db_connection(db_path) as conn:
            conn.execute(
                "UPDATE forge_decisions SET executing_started_at = ? WHERE id = ?",
                (newer_exec_ts, decision_id),
            )
        m = MagicMock()
        m.returncode = 0
        m.stdout = "stale worker done"
        m.stderr = ""
        return m

    monkeypatch.setattr(forge_mod.subprocess, "run", _superseded_subprocess_run)

    res = forge_mod.execute_approval_card(
        decision_id,
        user_id=forge_mod.OWNER_USER_ID,
        db_path=db_path,
    )

    assert res["ok"] is False
    assert res["fenced"] is True
    assert res["status"] == "executing"

    with forge_mod.get_db_connection(db_path) as conn:
        row = conn.execute("SELECT status, executing_started_at, receipt_text FROM forge_decisions WHERE id = ?", (decision_id,)).fetchone()
    assert row["status"] == "executing"
    assert row["executing_started_at"] == newer_exec_ts
    assert row["receipt_text"] == ""


def test_validate_forge_plan_heavy_verify_gate_and_exemptions(tmp_path):
    """Verify validate_forge_plan rejects unscoped heavy local verify commands unless scoped, Modal-offloaded, or exempted."""
    repo = _init_test_git_repo(tmp_path / "heavy_verify_repo")

    def _make_plan(v_cmd, plan_allow=False, chunk_allow=False):
        p = {
            "title": "Heavy Verify Gate Test",
            "repo": str(repo),
            "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
            "context_fact_sheet": ["Verify plan-time heavy verification gate."],
            "chunks": [
                {
                    "title": "Chunk 0",
                    "target_files": ["README.md"],
                    "instructions": "Update README.md with verified documentation details and run scoped checks.",
                    "verification_cmd": v_cmd,
                }
            ],
        }
        if plan_allow:
            p["allow_heavy_verify"] = True
        if chunk_allow:
            p["chunks"][0]["allow_heavy_verify"] = True
        return p

    for bad_cmd in (
        "npm run lint",
        "cd web/ui && npm run lint",
        "npx eslint .",
        "npx --no-install eslint --max-warnings=0 .",
        "npm test",
        "cd web/ui && npm run test",
        "sh scripts/verify.sh",
        "./scripts/verify.sh --lint-only",
    ):
        ok, msg = forge_mod.validate_forge_plan(_make_plan(bad_cmd))
        assert ok is False, f"Expected heavy verify command to be rejected: {bad_cmd}"
        assert "Heavy-Verify Gate" in msg

    for good_cmd in (
        "npx eslint web/ui/src/App.tsx",
        "npm test -- web/ui/src/App.test.tsx",
        "sh scripts/verify.sh --staged",
        "python3 tools/modal_verify.py --cmd 'sh scripts/verify.sh'",
        "python3 tools/modal_ui_webgl.py --smoke",
    ):
        ok, msg = forge_mod.validate_forge_plan(_make_plan(good_cmd))
        assert ok is True, f"Expected safe/offloaded command to pass ({good_cmd}): {msg}"

    # Explicit exemptions (plan-level and chunk-level)
    ok_plan, _ = forge_mod.validate_forge_plan(_make_plan("npm run lint", plan_allow=True))
    assert ok_plan is True
    ok_chunk, _ = forge_mod.validate_forge_plan(_make_plan("sh scripts/verify.sh", chunk_allow=True))
    assert ok_chunk is True


def test_enqueue_build_respects_active_repo_builds_and_serialize_repo(tmp_path, monkeypatch):
    """Verify enqueue_build reports active repo builds and serialize_repo holds follow-up builds cleanly in FIFO order."""
    repo = _init_test_git_repo(tmp_path / "repo_ser")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py", "b.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add a and b"], cwd=str(repo), check=True, capture_output=True)

    db_path = tmp_path / "forge_ser.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")

    plan_a = {
        "title": "Repo Build A",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Modify a.py"],
        "chunks": [
            {
                "title": "Chunk A",
                "target_files": ["a.py"],
                "instructions": "Update a.py with new constant and verify syntax.",
                "verification_cmd": "python3 -m py_compile a.py",
            }
        ],
    }
    plan_b = {
        "title": "Repo Build B",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Modify b.py"],
        "chunks": [
            {
                "title": "Chunk B",
                "target_files": ["b.py"],
                "instructions": "Update b.py with new constant and verify syntax.",
                "verification_cmd": "python3 -m py_compile b.py",
            }
        ],
    }

    res_a = forge_mod.enqueue_build(plan_a, db_path=db_path, post_widget=False)
    assert res_a["active_repo_builds"] == []

    res_b = forge_mod.enqueue_build(plan_b, db_path=db_path, post_widget=False, serialize_repo=True)
    assert res_a["build_id"] in res_b["active_repo_builds"]

    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    monkeypatch.setattr(sup, "should_execute_builds", lambda: True)

    def _fake_popen(*a, **kw):
        mp = MagicMock()
        mp.poll.return_value = None
        return mp

    with patch("subprocess.Popen", side_effect=_fake_popen), patch("builtins.open", unittest.mock.mock_open()):
        first_spawn = sup.maybe_spawn_next_build_worker()
        second_spawn = sup.maybe_spawn_next_build_worker()

    assert first_spawn == res_a["build_id"]
    assert second_spawn is None

    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute("UPDATE forge_builds SET status = 'completed' WHERE id = ?", (res_a["build_id"],))
    sup._active_build_procs.pop(res_a["build_id"], None)

    with patch("subprocess.Popen", side_effect=_fake_popen), patch("builtins.open", unittest.mock.mock_open()):
        after_spawn = sup.maybe_spawn_next_build_worker()
    assert after_spawn == res_b["build_id"]


def test_verify_chunk_worktree_git_status_nonzero_and_bash_executable(tmp_path):
    """Verify verify_chunk_worktree reports explicit git metadata error on non-zero git status and uses /bin/bash."""
    broken_wt = tmp_path / "broken_wt"
    broken_wt.mkdir()
    (broken_wt / ".git").write_text("gitdir: /nonexistent/path/to/gitdir\n", encoding="utf-8")

    ok, diff_stat, err = forge_mod.verify_chunk_worktree(
        broken_wt,
        verification_cmd="true",
        require_changes=True,
    )
    assert ok is False
    assert diff_stat == ""
    assert "Worktree git metadata error (exit" in err
    assert "zero file changes" not in err

    # Verify bash-specific syntax (pipefail + [[ ... ]]) works in verify_chunk_worktree
    good_repo = _init_test_git_repo(tmp_path / "bash_wt")
    (good_repo / "README.md").write_text("# Bash Verify Test\n", encoding="utf-8")
    ok_bash, _, err_bash = forge_mod.verify_chunk_worktree(
        good_repo,
        verification_cmd="set -o pipefail && [[ -f README.md ]] && echo ok | grep -q ok",
        target_files=["README.md"],
        require_changes=True,
    )
    assert ok_bash is True, f"Expected bash verification_cmd to pass, got: {err_bash}"


def test_reconcile_forge_on_boot_preserves_db_active_worktrees_across_containers(tmp_path, monkeypatch):
    """Verify reconcile_forge_on_boot protects .git/worktrees metadata for DB running/queued builds even without local flock."""
    import shutil

    repo = _init_test_git_repo(tmp_path / "cross_repo")
    db_path = tmp_path / "forge_cross.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "WORKSPACE_DIR", repo)

    plan = {
        "id": "fg-vulcan-live",
        "title": "Vulcan Live Build",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Cross-container boot protection test"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["README.md"],
                "instructions": "Update README.md inside Vulcan container.",
                "verification_cmd": "true",
            }
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute("UPDATE forge_builds SET status = 'running' WHERE id = 'fg-vulcan-live'")

    # Create a locked worktree and remove its checkout dir to simulate Vulcan's separate /tmp namespace
    peer_wt = tmp_path / "forge-wt-fg-vulcan-live-c0"
    subprocess.run(
        ["git", "worktree", "add", "-b", "forge/fg-vulcan-live-c0", str(peer_wt), "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "lock", "--reason", "forge-active-fg-vulcan-live", str(peer_wt)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    shutil.rmtree(peer_wt)

    # Zero boots in another container with NO worker flock held: metadata MUST survive because DB status is 'running'
    forge_mod.reconcile_forge_on_boot(db_path=db_path)
    assert (repo / ".git" / "worktrees" / "forge-wt-fg-vulcan-live-c0" / "locked").exists()

    # Once build completes, boot reconciliation cleans up the stale locked metadata
    with forge_mod.get_db_connection(db_path) as conn:
        conn.execute("UPDATE forge_builds SET status = 'completed' WHERE id = 'fg-vulcan-live'")
    forge_mod.reconcile_forge_on_boot(db_path=db_path)
    assert not (repo / ".git" / "worktrees" / "forge-wt-fg-vulcan-live-c0").exists()


def test_execute_chunk_injects_prohibited_env_and_repairs_corrupted_worktree(tmp_path, monkeypatch):
    """Verify execute_chunk_in_worktree injects FORGE_PROHIBITED_FILES and repairs corrupted worktree gitdir preserving target files."""
    repo = _init_test_git_repo(tmp_path / "repair_repo")
    (repo / "target.py").write_text("VAL = 1\n", encoding="utf-8")
    (repo / "secret.py").write_text("KEEP = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "target.py", "secret.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init files"], cwd=str(repo), check=True, capture_output=True)

    db_path = tmp_path / "forge_repair.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(forge_mod, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_completion_to_origin", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_failure_to_origin", lambda *a, **kw: None)

    plan = {
        "id": "fg-repair-test",
        "title": "Repair & Env Test",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Verify env injection and worktree repair"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["target.py"],
                "prohibited_files": ["secret.py"],
                "instructions": "Update target.py without touching secret.py.",
                "verification_cmd": "python3 -m py_compile target.py",
            }
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)

    observed_prohibited_envs = []
    attempt_seen = [0]
    preserved_seen_on_attempt2 = []

    class RepairMockPopen:
        def __init__(self, cmd, cwd=None, env=None, **kwargs):
            attempt_seen[0] += 1
            wt_p = Path(cwd)
            observed_prohibited_envs.append((env or {}).get("FORGE_PROHIBITED_FILES"))
            if attempt_seen[0] == 1:
                # Write modified target file AND corrupt .git gitdir to trigger repair on attempt 2
                (wt_p / "target.py").write_text("VAL = 42\n", encoding="utf-8")
                (wt_p / ".git").write_text("gitdir: /nonexistent/corrupted/gitdir\n", encoding="utf-8")
            else:
                # Attempt 2 must see repaired git worktree AND preserved target.py content
                rev = subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    cwd=str(wt_p),
                    capture_output=True,
                    text=True,
                )
                assert rev.returncode == 0 and rev.stdout.strip() == "true"
                preserved_seen_on_attempt2.append((wt_p / "target.py").read_text(encoding="utf-8"))
            self.stdout = iter([])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda cmd, *a, **kw: RepairMockPopen(cmd, *a, **kw)
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd)
        else real_popen(cmd, *a, **kw),
    )

    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    with forge_mod.get_db_connection(db_path) as conn:
        c0 = conn.execute("SELECT * FROM forge_chunks WHERE id = 'fg-repair-test-c0'").fetchone()

    assert sup.execute_chunk_in_worktree("fg-repair-test", c0) is True
    assert attempt_seen[0] == 2
    assert json.loads(observed_prohibited_envs[0]) == ["secret.py"]
    assert preserved_seen_on_attempt2 == ["VAL = 42\n"]


def test_execute_chunk_cas_fencing_never_flips_merged_chunk_or_completed_build(tmp_path, monkeypatch):
    """Verify late/duplicate chunk failure cannot overwrite an already-merged chunk or completed build."""
    repo = _init_test_git_repo(tmp_path / "cas_repo")
    db_path = tmp_path / "forge_cas_chunk.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(forge_mod, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_completion_to_origin", lambda *a, **kw: None)

    fail_notifs = []
    monkeypatch.setattr(forge_mod, "dispatch_build_failure_to_origin", lambda bid, **kw: fail_notifs.append(bid))

    plan = {
        "id": "fg-cas-chunk",
        "title": "CAS Chunk Fencing Test",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Verify CAS fencing on chunk/build failure"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["README.md"],
                "instructions": "Update README.md with verified content.",
                "verification_cmd": "false",
            }
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)

    class LateWorkerPopen:
        def __init__(self, cmd, cwd=None, **kwargs):
            # Simulate primary worker already finishing and merging chunk + completing build
            with forge_mod.get_db_connection(db_path) as conn:
                conn.execute("UPDATE forge_chunks SET status = 'merged' WHERE id = 'fg-cas-chunk-c0'")
                conn.execute("UPDATE forge_builds SET status = 'completed' WHERE id = 'fg-cas-chunk'")
            (Path(cwd) / "README.md").write_text("# Late Edit\n", encoding="utf-8")
            self.stdout = iter([])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda cmd, *a, **kw: LateWorkerPopen(cmd, *a, **kw)
        if isinstance(cmd, list) and any("/usr/local/bin/agy" in str(x) for x in cmd)
        else real_popen(cmd, *a, **kw),
    )

    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    with forge_mod.get_db_connection(db_path) as conn:
        c0 = conn.execute("SELECT * FROM forge_chunks WHERE id = 'fg-cas-chunk-c0'").fetchone()

    assert sup.execute_chunk_in_worktree("fg-cas-chunk", c0) is False
    with forge_mod.get_db_connection(db_path) as conn:
        c_final = conn.execute("SELECT status FROM forge_chunks WHERE id = 'fg-cas-chunk-c0'").fetchone()
        b_final = conn.execute("SELECT status FROM forge_builds WHERE id = 'fg-cas-chunk'").fetchone()
    assert c_final["status"] == "merged"
    assert b_final["status"] == "completed"
    assert fail_notifs == []


def test_build_git_push_args_and_env_and_preflight_auth(tmp_path, monkeypatch):
    """Verify _build_git_push_args_and_env fallback paths, _preflight_check_remote_push_auth, and prod fast-fail."""
    repo = _init_test_git_repo(tmp_path / "push_auth_repo")
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/octo-org/demo-repo.git"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # 1. When no git credential helper, gh not available, and SSH key exists -> SSH URL fallback + GIT_TERMINAL_PROMPT=0
    fake_home = tmp_path / "fake_home"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".ssh" / "id_ed25519").write_text("dummy-test-key-placeholder\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(forge_mod, "_has_git_https_credential_helper", lambda rp: False)
    monkeypatch.setattr(forge_mod.shutil, "which", lambda name: None)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)

    cmd_ssh, env_ssh = forge_mod._build_git_push_args_and_env(repo, remote="origin", branch="main")
    assert env_ssh["GIT_TERMINAL_PROMPT"] == "0"
    assert cmd_ssh == ["git", "push", "git@github.com:octo-org/demo-repo.git", "main"]

    # 2. When gh is installed and authenticated -> injects gh auth git-credential
    monkeypatch.setattr(forge_mod.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    real_run = subprocess.run

    def _mock_gh_auth_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:3] == ["/usr/bin/gh", "auth", "status"]:
            m = MagicMock()
            m.returncode = 0
            m.stdout = "Logged in"
            m.stderr = ""
            return m
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(forge_mod.subprocess, "run", _mock_gh_auth_run)
    cmd_gh, env_gh = forge_mod._build_git_push_args_and_env(repo, remote="origin", branch="main")
    assert env_gh["GIT_TERMINAL_PROMPT"] == "0"
    assert cmd_gh == ["git", "-c", "credential.helper=!/usr/bin/gh auth git-credential", "push", "origin", "main"]

    # 3. When neither credential helper, gh, nor SSH key exists -> pre-flight fails fast
    monkeypatch.setattr(forge_mod.subprocess, "run", real_run)
    monkeypatch.setattr(forge_mod.shutil, "which", lambda name: None)
    (fake_home / ".ssh" / "id_ed25519").unlink()

    pf_ok, pf_err = forge_mod._preflight_check_remote_push_auth(repo)
    assert pf_ok is False
    assert "Remote push auth pre-flight failed" in pf_err

    # 3b. Split pushurl (SSH fetch, HTTPS push) is inspected via --push
    subprocess.run(
        ["git", "remote", "set-url", "origin", "git@github.com:octo-org/demo-repo.git"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "--push", "origin", "https://github.com/octo-org/demo-repo.git"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    split_ok, split_err = forge_mod._preflight_check_remote_push_auth(repo)
    assert split_ok is False
    assert "https://github.com/octo-org/demo-repo.git" in split_err
    (fake_home / ".ssh" / "id_ed25519").write_text("dummy-test-key-placeholder\n", encoding="utf-8")
    cmd_split, _ = forge_mod._build_git_push_args_and_env(repo, remote="origin", branch="main")
    assert cmd_split == ["git", "push", "git@github.com:octo-org/demo-repo.git", "main"]
    (fake_home / ".ssh" / "id_ed25519").unlink()

    # 4. In prod mode, run_build_pipeline fails fast before spawning chunk 0 worker
    db_path = tmp_path / "forge_preflight.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "FORGE_LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(forge_mod, "_is_prod_forge_env", lambda p: True)
    monkeypatch.setattr(forge_mod, "post_or_update_build_widget", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "sync_forge_channel_header", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "dispatch_build_failure_to_origin", lambda *a, **kw: None)
    monkeypatch.setattr(forge_mod, "_prepare_clean_repo_and_sync_base", lambda r, b, **kw: "HEAD")

    plan = {
        "id": "fg-preflight-fail",
        "title": "Preflight Auth Fail Fast",
        "repo": str(repo),
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "context_fact_sheet": ["Verify fast-fail before chunk 0"],
        "chunks": [
            {
                "title": "Chunk 0",
                "target_files": ["README.md"],
                "instructions": "Update README.md with release notes.",
                "verification_cmd": "true",
            }
        ],
    }
    forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)

    worker_called = []
    sup = forge_mod.ForgeSupervisor(db_path=db_path)
    monkeypatch.setattr(sup, "execute_chunk_in_worktree", lambda *a, **kw: worker_called.append(a) or True)

    assert sup.run_build_pipeline("fg-preflight-fail") is False
    assert worker_called == []
    with forge_mod.get_db_connection(db_path) as conn:
        b_row = conn.execute("SELECT status FROM forge_builds WHERE id = 'fg-preflight-fail'").fetchone()
        c_row = conn.execute("SELECT status, last_error FROM forge_chunks WHERE id = 'fg-preflight-fail-c0'").fetchone()
    assert b_row["status"] == "failed"
    assert c_row["status"] == "failed"
    assert "Remote push auth pre-flight failed" in c_row["last_error"]


def test_format_chunk_prompt_appends_repo_verify_script(tmp_path):
    """format_chunk_prompt appends sh scripts/verify.sh --staged and adds staged-file guidance."""
    repo = _init_test_git_repo(tmp_path / "repo_verify")
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    verify_sh = scripts_dir / "verify.sh"
    verify_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    verify_sh.chmod(0o755)
    (repo / "sample.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Add verify script and sample"], cwd=str(repo), capture_output=True, check=True)

    db_path = tmp_path / "forge_prompt.db"
    forge_mod.init_forge_db(db_path)

    build_id = "fg-test-prompt-repo-verify"
    plan = {
        "id": build_id,
        "title": "Prompt Repo Verify Test",
        "repo": str(repo),
        "origin_channel_id": forge_mod.FORGE_CHANNEL_ID,
        "context_fact_sheet": ["Keep sample.py clean."],
        "chunks": [
            {
                "title": "Update sample.py",
                "target_files": ["sample.py"],
                "instructions": "Add multiply helper to sample.py.",
                "verification_cmd": "python3 -m py_compile sample.py",
            },
            {
                "title": "Already includes verify.sh",
                "target_files": ["sample.py"],
                "instructions": "Check verify.sh is not duplicated.",
                "verification_cmd": "python3 -m py_compile sample.py && sh scripts/verify.sh --staged",
            },
        ],
    }
    enq = forge_mod.enqueue_build(plan, db_path=db_path, post_widget=False)
    assert enq["ok"] is True

    prompt_c0 = forge_mod.format_chunk_prompt(build_id, 0, db_path=db_path, worktree_dir=str(repo))
    assert (
        "- **Mandatory Verification Gate:** `python3 -m py_compile sample.py && sh scripts/verify.sh --staged`"
        in prompt_c0
    )
    assert "git add sample.py" in prompt_c0
    assert "--staged" in prompt_c0

    prompt_c1 = forge_mod.format_chunk_prompt(build_id, 1, db_path=db_path, worktree_dir=str(repo))
    assert (
        "- **Mandatory Verification Gate:** `python3 -m py_compile sample.py && sh scripts/verify.sh --staged`"
        in prompt_c1
    )
    assert "sh scripts/verify.sh --staged && sh scripts/verify.sh --staged" not in prompt_c1
    assert "git add sample.py" in prompt_c1


def test_verify_chunk_worktree_autofix_ruff_and_guards(tmp_path):
    """verify_chunk_worktree auto-fixes safe ruff lint/format failures once, while skipping prohibited files and pin errors."""
    if not forge_mod.shutil.which("ruff"):
        pytest.skip("ruff not installed in test environment")

    repo = _init_test_git_repo(tmp_path / "repo_autofix")
    sample_py = repo / "sample.py"
    sample_py.write_text("def greet(name: str) -> str:\n    return f'hello {name}'\n", encoding="utf-8")
    secret_py = repo / "secret.py"
    secret_py.write_text("x=1+2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial python files"], cwd=str(repo), capture_output=True, check=True)

    # 1. Introduce a safe-fixable ruff lint error (unused import) + format issue
    sample_py.write_text(
        "import os\n\ndef greet(name: str) -> str:\n    x=1+2\n    return f'hello {name} {x}'\n",
        encoding="utf-8",
    )

    ok, diff_stat, err = forge_mod.verify_chunk_worktree(
        repo,
        verification_cmd="ruff check sample.py && ruff format --check sample.py",
        prohibited_files=["secret.py"],
        target_files=["sample.py"],
        require_changes=True,
    )
    assert ok is True, f"Expected autofix retry to pass, got error: {err}"
    assert err == ""
    assert diff_stat
    fixed_src = sample_py.read_text(encoding="utf-8")
    assert "import os" not in fixed_src
    assert "x = 1 + 2" in fixed_src

    # 2. Prohibited files must never be modified by _try_autofix_python_lint_format
    secret_before = secret_py.read_bytes()
    autofixed_prohibited = forge_mod._try_autofix_python_lint_format(
        repo,
        changed_files=["secret.py"],
        prohibited_files=["secret.py"],
        verify_output="1 file would be reformatted",
        effective_cmd="ruff format --check secret.py",
    )
    assert autofixed_prohibited is False
    assert secret_py.read_bytes() == secret_before

    # 3. Ruff version-pin mismatch must never trigger auto-fix
    pin_py = repo / "pin_test.py"
    pin_py.write_text("import sys\nx=1+2\n", encoding="utf-8")
    pin_before = pin_py.read_bytes()
    autofixed_pin = forge_mod._try_autofix_python_lint_format(
        repo,
        changed_files=["pin_test.py"],
        prohibited_files=None,
        verify_output="[ruff] ruff version 0.1.0 does not match the pin 0.9.0; 1 file would be reformatted",
        effective_cmd="sh scripts/verify.sh --staged",
    )
    assert autofixed_pin is False
    assert pin_py.read_bytes() == pin_before

    # 4. When --staged is in effective_cmd, auto-fixed files are re-staged automatically
    subprocess.run(["git", "add", "pin_test.py"], cwd=str(repo), capture_output=True, check=True)
    autofixed_staged = forge_mod._try_autofix_python_lint_format(
        repo,
        changed_files=["pin_test.py"],
        prohibited_files=None,
        verify_output="1 file would be reformatted",
        effective_cmd="sh scripts/verify.sh --staged",
    )
    assert autofixed_staged is True
    # After re-staging, unstaged diff for pin_test.py should be empty
    unstaged_diff = subprocess.run(
        ["git", "diff", "--", "pin_test.py"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    assert unstaged_diff.stdout.strip() == ""


def test_unknown_exit_render_job_widget_embed(tmp_path, monkeypatch):
    import tools.forge as forge

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    monkeypatch.setattr(forge, "TASKS_DIR", tmp_path / "detached_tasks")

    start_ts = time.time() - 3600.0
    end_ts = start_ts + 42.0
    with forge.get_db_connection(db_p) as conn:
        conn.execute(
            """
            INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, duration_seconds)
            VALUES (?, 'Interrupted Job', 'python3 worker.py', 'zero-chat', '/workspace', 60, 'unknown_exit', ?, ?, NULL)
            """,
            ("dt-unknown-render-1", start_ts, end_ts),
        )

    embed = forge.render_job_widget_embed("dt-unknown-render-1", db_path=db_p)
    assert embed["title"].startswith("⚠️")
    assert "**INTERRUPTED**" in embed["description"]
    assert "⏱️ 42s" in embed["description"]
    assert "Auto-clears in 1h" in embed["footer"]["text"]


def test_unknown_exit_prune_completed_job_widgets(tmp_path):
    import tools.forge as forge

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    now = time.time()

    with forge.get_db_connection(db_p) as conn:
        # Expired after FAILED_JOB_WIDGET_TTL_SECONDS (600s)
        conn.execute(
            """
            INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, forge_message_id)
            VALUES (?, 'Old Interrupted', 'echo 1', 'zero-chat', '/workspace', 60, 'unknown_exit', ?, ?, 1111111111)
            """,
            ("dt-unknown-old", now - 800.0, now - 700.0),
        )
        # Recent unknown_exit in same channel, superseded by newer completed job
        conn.execute(
            """
            INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, forge_message_id)
            VALUES (?, 'Superseded Interrupted', 'echo 2', 'lounge', '/workspace', 60, 'unknown_exit', ?, ?, 2222222222)
            """,
            ("dt-unknown-superseded", now - 100.0, now - 50.0),
        )
        conn.execute(
            """
            INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, forge_message_id)
            VALUES (?, 'Newer Completed', 'echo 3', 'lounge', '/workspace', 60, 'completed', ?, ?, NULL)
            """,
            ("dt-completed-newer", now - 40.0, now - 10.0),
        )
        # Recent unknown_exit in another channel with no newer completed job -> should NOT be pruned
        conn.execute(
            """
            INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, forge_message_id)
            VALUES (?, 'Fresh Interrupted', 'echo 4', 'forge', '/workspace', 60, 'unknown_exit', ?, ?, 3333333333)
            """,
            ("dt-unknown-fresh", now - 100.0, now - 50.0),
        )

    pruned = forge.prune_completed_job_widgets(ttl_seconds=3600.0, db_path=db_p, delete_remote=False)
    assert "dt-unknown-old" in pruned
    assert "dt-unknown-superseded" in pruned
    assert "dt-unknown-fresh" not in pruned


def test_unknown_exit_sweep_stale_forge_channel_embeds(tmp_path, monkeypatch):
    import tools.forge as forge

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    old_ts = time.time() - 700.0

    with forge.get_db_connection(db_p) as conn:
        conn.execute(
            """
            INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, start_time, end_time, forge_message_id)
            VALUES (?, 'Stale Interrupted Job', 'python3 job.py', 'zero-chat', '/workspace', 60, 'unknown_exit', ?, ?, 1564468993128009881)
            """,
            ("dt-unknown-sweep-1", old_ts - 60.0, old_ts),
        )

    fake_messages = [
        {
            "id": "1564468993128009881",
            "timestamp": "2026-10-27T01:00:00+00:00",
            "embeds": [
                {
                    "title": "⚙️ Stale Interrupted Job",
                    "description": "**RUNNING** · `Zero-Agent` · <#1542081375287640084> · ⏱️ 1m 0s",
                    "footer": {"text": "dt-unknown-sweep-1 · Oct 26"},
                }
            ],
        },
        {
            # Non-job warning embed must NOT be swept solely because of ⚠️ prefix
            "id": "1564468993128009882",
            "timestamp": "2026-10-27T01:00:00+00:00",
            "embeds": [
                {
                    "title": "⚠️ System Warning",
                    "description": "**NOTICE** · Non-job warning card",
                    "footer": {"text": "Notice"},
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
    assert "dt-unknown-sweep-1" in swept
    assert len(deleted_urls) == 1
    assert "1564468993128009881" in deleted_urls[0]


def test_unknown_exit_reconcile_forge_on_boot(tmp_path, monkeypatch):
    import tools.forge as forge

    db_p = tmp_path / "forge.db"
    forge.init_forge_db(db_p)
    tasks_dir = tmp_path / "detached_tasks"
    monkeypatch.setattr(forge, "TASKS_DIR", tasks_dir)
    monkeypatch.setattr(forge, "is_ivy_container", lambda: False)
    monkeypatch.setattr(forge, "_is_pid_running", lambda pid: False)
    monkeypatch.setattr(forge, "_get_active_build_worker_ids", lambda *a, **kw: set())

    widget_calls = []
    monkeypatch.setattr(
        forge,
        "post_or_update_job_widget",
        lambda j_id, db_path=None, **kw: widget_calls.append((j_id, Path(db_path))) or 999888777,
    )

    job_id = "dt-reap-unknown-1"
    job_dir = tasks_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    start_ts = time.time() - 120.0
    meta_file = job_dir / "meta.json"
    meta_file.write_text(
        json.dumps(
            {
                "task_id": job_id,
                "name": "Dead Running Job",
                "command": "python3 run.py",
                "channel": "zero-chat",
                "status": "running",
                "start_time": start_ts,
                "end_time": None,
                "duration_seconds": None,
                "forge_message_id": 1564468993128009888,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with forge.get_db_connection(db_p) as conn:
        conn.execute(
            """
            INSERT INTO forge_jobs (id, name, command, channel, cwd, timeout, status, worker_pid, child_pid, log_file, start_time, forge_message_id)
            VALUES (?, 'Dead Running Job', 'python3 run.py', 'zero-chat', '/workspace', 60, 'running', 999991, 999992, '', ?, 1564468993128009888)
            """,
            (job_id, start_ts),
        )

    res = forge.reconcile_forge_on_boot(db_path=db_p)
    assert res["pruned_jobs"] >= 1
    assert widget_calls == [(job_id, db_p)]

    with forge.get_db_connection(db_p) as conn:
        row = conn.execute(
            "SELECT status, end_time, duration_seconds FROM forge_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "unknown_exit"
    assert row["end_time"] is not None
    assert row["duration_seconds"] is not None and row["duration_seconds"] >= 100.0

    updated_meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert updated_meta["status"] == "unknown_exit"
    assert updated_meta["end_time"] == row["end_time"]
    assert updated_meta["duration_seconds"] is not None and updated_meta["duration_seconds"] >= 100.0


def test_stage_decision_rejects_noop_and_approval_handlers(tmp_path):
    """Verify stage_decision and stage_decision_card reject 'noop' and 'approval' handlers."""
    import pytest

    db_path = tmp_path / "forge_reject_noop.db"
    forge_mod.init_forge_db(db_path)

    for bad_handler in ("noop", "approval", "NOOP", " approval ", "NoOp"):
        with pytest.raises(ValueError, match="prohibited"):
            forge_mod.stage_decision(
                category="ops",
                title="No-op Card",
                summary="Should be rejected.",
                handler_type=bad_handler,
                payload={"note": "nothing"},
                channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
                db_path=db_path,
            )

    for bad_handler in ("noop", "approval"):
        with pytest.raises(ValueError, match="prohibited"):
            forge_mod.stage_decision_card(
                category="ops",
                title="No-op Card Alias",
                summary="Should be rejected.",
                handler_type=bad_handler,
                payload={},
                channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
                db_path=db_path,
            )

    with forge_mod.get_db_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM forge_decisions").fetchone()[0]
    assert count == 0


def test_execute_decision_fails_legacy_noop_and_approval_cards(tmp_path):
    """Verify legacy 'noop' and 'approval' rows in forge_decisions fail cleanly when executed."""
    db_path = tmp_path / "forge_legacy_noop.db"
    forge_mod.init_forge_db(db_path)

    now = time.time()
    legacy_handlers = ("noop", "approval", "NOOP", " approval ")
    with forge_mod.get_db_connection(db_path) as conn:
        for idx, h_type in enumerate(legacy_handlers):
            conn.execute(
                """
                INSERT INTO forge_decisions (
                    id, category, title, summary, handler_type, payload_json,
                    channel_id, is_private, status, expires_at, created_at
                ) VALUES (?, 'ops', ?, 'Legacy card', ?, ?, ?, 1, 'pending', ?, ?)
                """,
                (
                    f"dec-legacy-{idx}",
                    f"Legacy {h_type}",
                    h_type,
                    json.dumps({"note": "legacy"}),
                    forge_mod.ZERO_CHAT_CHANNEL_ID,
                    now + 3600.0,
                    now,
                ),
            )

    for idx, h_type in enumerate(legacy_handlers):
        did = f"dec-legacy-{idx}"
        res = forge_mod.execute_decision(did, user_id=forge_mod.OWNER_USER_ID, db_path=db_path)
        assert res["ok"] is False
        assert res["status"] == "failed"
        assert "Unsupported no-op handler_type" in res["error"]
        assert res["receipt"] == res["error"]
        with forge_mod.get_db_connection(db_path) as conn:
            row = conn.execute(
                "SELECT status, receipt_text FROM forge_decisions WHERE id = ?",
                (did,),
            ).fetchone()
        assert row["status"] == "failed"
        assert row["receipt_text"] == res["receipt"]


def test_promote_and_push_other_build_repos_promotes_and_pushes_all_earlier_repos(tmp_path):
    """Verify multi-repo build promotion advances default_branch and pushes to origin in earlier repos."""
    def _init_repo_with_origin(name: str) -> tuple[Path, Path]:
        origin = tmp_path / f"{name}-origin.git"
        repo = tmp_path / name
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=str(repo), check=True)
        (repo / "README.md").write_text("init\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(repo), check=True, capture_output=True)
        return repo, origin

    repo_a, origin_a = _init_repo_with_origin("repo-a")
    release_ref = "forge/release-fg-multirepo-1"

    # Simulate Chunk 0 merging into forge/release-<build_id> in Repo A while main stays behind
    subprocess.run(["git", "checkout", "-b", release_ref], cwd=str(repo_a), check=True, capture_output=True)
    (repo_a / "chunk0.txt").write_text("from chunk 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "chunk0.txt"], cwd=str(repo_a), check=True)
    subprocess.run(["git", "commit", "-m", "feat: chunk 0 in repo a"], cwd=str(repo_a), check=True, capture_output=True)
    chunk0_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_a), check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "main"], cwd=str(repo_a), check=True, capture_output=True)

    # Confirm main and origin/main do NOT yet have chunk0_sha
    assert subprocess.run(["git", "rev-parse", "main"], cwd=str(repo_a), check=True, capture_output=True, text=True).stdout.strip() != chunk0_sha

    logs: list[str] = []
    err = forge_mod._promote_and_push_other_build_repos(
        [repo_a],
        release_ref,
        {"propagate_branches_json": json.dumps(["release-preview"])},
        log_fn=logs.append,
    )
    assert err is None
    assert any("promoted" in line for line in logs)

    # Local main, origin main, and propagated branch in Repo A must all now be at chunk0_sha
    assert subprocess.run(["git", "rev-parse", "main"], cwd=str(repo_a), check=True, capture_output=True, text=True).stdout.strip() == chunk0_sha
    assert subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=str(origin_a), check=True, capture_output=True, text=True).stdout.strip() == chunk0_sha
    assert subprocess.run(["git", "rev-parse", "release-preview"], cwd=str(repo_a), check=True, capture_output=True, text=True).stdout.strip() == chunk0_sha
    assert (repo_a / "chunk0.txt").read_text(encoding="utf-8") == "from chunk 0\n"


def test_get_forge_worktree_root_and_chunk_dir(tmp_path, monkeypatch):
    """Verify get_forge_worktree_root/get_chunk_worktree_dir respect FORGE_WORKTREE_ROOT, writable DATA_DIR, and /tmp fallback."""
    monkeypatch.delenv("FORGE_WORKTREE_ROOT", raising=False)

    # 1. Writable DATA_DIR -> <DATA_DIR>/forge-worktrees without eager creation
    data_dir = tmp_path / "nvme_data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(forge_mod, "DATA_DIR", data_dir)
    assert forge_mod.get_forge_worktree_root() == data_dir / "forge-worktrees"
    assert not (data_dir / "forge-worktrees").exists()
    assert forge_mod.get_chunk_worktree_dir("fg-123-c0") == data_dir / "forge-worktrees" / "forge-wt-fg-123-c0"

    # 2. Missing DATA_DIR -> /tmp fallback
    monkeypatch.setattr(forge_mod, "DATA_DIR", tmp_path / "missing_data")
    assert forge_mod.get_forge_worktree_root() == Path("/tmp")
    assert forge_mod.get_chunk_worktree_dir("fg-123-c0") == Path("/tmp/forge-wt-fg-123-c0")

    # 3. FORGE_WORKTREE_ROOT env var overrides everything (with whitespace stripped)
    custom_root = tmp_path / "custom_wt_root"
    monkeypatch.setenv("FORGE_WORKTREE_ROOT", f"  {custom_root}  ")
    assert forge_mod.get_forge_worktree_root() == custom_root
    assert forge_mod.get_chunk_worktree_dir("fg-456-c1") == custom_root / "forge-wt-fg-456-c1"


def test_reconcile_forge_on_boot_cleans_nvme_and_legacy_tmp_worktrees(tmp_path, monkeypatch):
    """Verify reconcile_forge_on_boot cleans stale worktrees in both NVMe worktree root and legacy /tmp while preserving active builds."""
    import shutil

    db_path = tmp_path / "forge_reconcile_wt.db"
    forge_mod.init_forge_db(db_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(forge_mod, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(forge_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(forge_mod, "TASKS_DIR", tmp_path / "detached_tasks")
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", db_path)
    monkeypatch.delenv("FORGE_WORKTREE_ROOT", raising=False)

    nvme_root = data_dir / "forge-worktrees"
    nvme_stale = nvme_root / "forge-wt-fg-stale-nvme-c0"
    nvme_active = nvme_root / "forge-wt-fg-active-nvme-c0"
    nvme_stale.mkdir(parents=True)
    nvme_active.mkdir(parents=True)

    pid = os.getpid()
    tmp_stale = Path(f"/tmp/forge-wt-fg-test-stale-{pid}-c0")
    tmp_active = Path(f"/tmp/forge-wt-fg-test-active-{pid}-c0")
    tmp_stale.mkdir(parents=True, exist_ok=True)
    tmp_active.mkdir(parents=True, exist_ok=True)

    try:
        with forge_mod.get_db_connection(db_path) as conn:
            for active_bid in ("fg-active-nvme", f"fg-test-active-{pid}"):
                conn.execute(
                    """
                    INSERT INTO forge_builds (id, title, objective, origin_channel_id, repo, status, created_at, updated_at)
                    VALUES (?, 'Active Test', 'Keep active wt', 1, '/workspace', 'running', ?, ?)
                    """,
                    (active_bid, time.time(), time.time()),
                )

        res = forge_mod.reconcile_forge_on_boot(db_path=db_path)
        cleaned = set(res["cleaned_worktrees"])
        assert str(nvme_stale) in cleaned
        assert str(tmp_stale) in cleaned
        assert not nvme_stale.exists()
        assert not tmp_stale.exists()
        assert nvme_active.exists()
        assert tmp_active.exists()
    finally:
        shutil.rmtree(tmp_stale, ignore_errors=True)
        shutil.rmtree(tmp_active, ignore_errors=True)


def test_agora_ticker_heal_checks_nvme_and_tmp_worktrees(tmp_path, monkeypatch):
    """Verify agora_forge_ticker.heal_and_check_active_forge_build recognizes worktrees under NVMe root, FORGE_WORKTREE_ROOT, and /tmp."""
    import shutil
    import tools.agora_forge_ticker as ticker_mod

    db_path = tmp_path / "forge_ticker.db"
    forge_mod.init_forge_db(db_path)
    monkeypatch.setattr(ticker_mod, "FORGE_DB", db_path)

    custom_root = tmp_path / "forge-worktrees"
    custom_root.mkdir(parents=True)
    monkeypatch.setenv("FORGE_WORKTREE_ROOT", str(custom_root))

    stale_ts = time.time() - 300.0  # >180s without worktree -> auto-healed; with worktree -> kept active
    bid_with_wt = "fg-agora-live-wt"
    bid_orphan = "fg-agora-orphan"
    (custom_root / f"forge-wt-{bid_with_wt}-c0").mkdir(parents=True)

    with forge_mod.get_db_connection(db_path) as conn:
        for bid, title in ((bid_with_wt, "AGORA-F1 Live"), (bid_orphan, "AGORA-F2 Orphan")):
            conn.execute(
                """
                INSERT INTO forge_builds (id, title, objective, origin_channel_id, repo, status, created_at, updated_at)
                VALUES (?, ?, 'Ticker test', 1, '/workspace/agora-roguelike', 'running', ?, ?)
                """,
                (bid, title, stale_ts, stale_ts),
            )
            conn.execute(
                """
                INSERT INTO forge_chunks (id, build_id, chunk_index, title, instructions, status)
                VALUES (?, ?, 0, 'Chunk 0', 'Verify ticker heal', 'running')
                """,
                (f"{bid}-c0", bid),
            )

    active_label = ticker_mod.heal_and_check_active_forge_build()
    assert active_label is not None and bid_with_wt in active_label

    with forge_mod.get_db_connection(db_path) as conn:
        live_row = conn.execute("SELECT status FROM forge_builds WHERE id = ?", (bid_with_wt,)).fetchone()
        orphan_row = conn.execute("SELECT status FROM forge_builds WHERE id = ?", (bid_orphan,)).fetchone()
    assert live_row["status"] == "running"
    assert orphan_row["status"] == "failed"

    # Also verify legacy /tmp fallback when FORGE_WORKTREE_ROOT is unset
    monkeypatch.delenv("FORGE_WORKTREE_ROOT", raising=False)
    pid = os.getpid()
    tmp_wt = Path(f"/tmp/forge-wt-fg-agora-tmp-{pid}-c0")
    tmp_wt.mkdir(parents=True, exist_ok=True)
    try:
        with forge_mod.get_db_connection(db_path) as conn:
            conn.execute("UPDATE forge_builds SET status = 'failed' WHERE id = ?", (bid_with_wt,))
            conn.execute(
                """
                INSERT INTO forge_builds (id, title, objective, origin_channel_id, repo, status, created_at, updated_at)
                VALUES (?, 'AGORA-F3 Legacy Tmp', 'Ticker tmp test', 1, '/workspace/agora-roguelike', 'running', ?, ?)
                """,
                (f"fg-agora-tmp-{pid}", stale_ts, stale_ts),
            )
        label_tmp = ticker_mod.heal_and_check_active_forge_build()
        assert label_tmp is not None and f"fg-agora-tmp-{pid}" in label_tmp
    finally:
        shutil.rmtree(tmp_wt, ignore_errors=True)


def test_isolation_is_prod_forge_env_and_temp_path_guards(tmp_path, monkeypatch):
    """Verify _is_temp_path and _is_prod_forge_env prevent test runs or /tmp DBs from being treated as production."""
    tmp_db = tmp_path / "forge.db"
    forge_mod.init_forge_db(tmp_db)

    assert forge_mod._is_temp_path(tmp_db) is True
    assert forge_mod._is_temp_path("/tmp/ephemeral_forge.db") is True
    assert forge_mod._is_temp_path("/workspace/data/forge.db") is False
    assert forge_mod._is_temp_path("") is False

    # Monkeypatch FORGE_DB_PATH to tmp_db WITHOUT monkeypatching TASKS_DIR (Root Cause 1 reproduction)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", tmp_db)
    assert forge_mod._is_prod_forge_env(tmp_db) is False
    assert forge_mod._is_prod_forge_env() is False

    # Even if FORGE_ALLOW_PROD_IN_TESTS is set, a /tmp DB path must still return False
    monkeypatch.setenv("FORGE_ALLOW_PROD_IN_TESTS", "1")
    assert forge_mod._is_prod_forge_env(tmp_db) is False
    assert forge_mod._is_prod_forge_env() is False

    # With FORGE_ALLOW_PROD_IN_TESTS unset, even a non-temp path returns False under PYTEST_CURRENT_TEST
    monkeypatch.delenv("FORGE_ALLOW_PROD_IN_TESTS", raising=False)
    non_temp_db = Path("/workspace/data/forge.db")
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", non_temp_db)
    assert forge_mod._is_prod_forge_env(non_temp_db) is False


def test_isolation_consolidate_legacy_forge_db_refuses_non_temp_legacy(tmp_path, monkeypatch):
    """Verify consolidate_legacy_forge_db resolves defaults dynamically and refuses to unlink/symlink non-temp legacy DBs."""
    pri = tmp_path / "primary_forge.db"
    leg_dir = tmp_path / "simulated_non_temp_data"
    leg_dir.mkdir(parents=True)
    leg = leg_dir / "forge.db"
    forge_mod.init_forge_db(pri)
    forge_mod.init_forge_db(leg)

    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", pri)
    monkeypatch.setattr(forge_mod, "DATA_DIR", leg_dir)

    # 1. Primary is temp, legacy is treated as non-temp -> refused_temp_primary (and legacy_db_path=None resolves from DATA_DIR)
    orig_is_temp = forge_mod._is_temp_path
    monkeypatch.setattr(
        forge_mod,
        "_is_temp_path",
        lambda p: False if Path(p) == leg else orig_is_temp(p),
    )
    res = forge_mod.consolidate_legacy_forge_db()
    assert res["symlinked"] is False
    assert res["status"] == "refused_temp_primary"
    assert leg.exists() and not leg.is_symlink()

    # 2. Synthetic non-existent non-temp legacy path is refused before .exists() check
    synthetic_legacy = Path("/workspace/nonexistent_dir/forge.db")
    res_synth = forge_mod.consolidate_legacy_forge_db(primary_db_path=pri, legacy_db_path=synthetic_legacy)
    assert res_synth["symlinked"] is False
    assert res_synth["status"] == "refused_temp_primary"

    # 3. Both primary and legacy treated as non-temp under PYTEST_CURRENT_TEST -> refused_pytest_prod_legacy
    monkeypatch.setattr(forge_mod, "_is_temp_path", lambda p: False)
    res_pytest = forge_mod.consolidate_legacy_forge_db(primary_db_path=pri, legacy_db_path=leg)
    assert res_pytest["symlinked"] is False
    assert res_pytest["status"] == "refused_pytest_prod_legacy"
    assert leg.exists() and not leg.is_symlink()


def test_isolation_reconcile_forge_on_boot_skips_consolidate_on_tmp_db(tmp_path, monkeypatch):
    """Verify reconcile_forge_on_boot(db_path=tmp_db) with FORGE_DB_PATH=tmp_db never invokes consolidate_legacy_forge_db."""
    tmp_db = tmp_path / "forge.db"
    forge_mod.init_forge_db(tmp_db)
    monkeypatch.setattr(forge_mod, "FORGE_DB_PATH", tmp_db)

    called = []
    monkeypatch.setattr(
        forge_mod,
        "consolidate_legacy_forge_db",
        lambda *a, **kw: called.append((a, kw)) or {"symlinked": False},
    )

    res = forge_mod.reconcile_forge_on_boot(db_path=tmp_db)
    assert isinstance(res, dict)
    assert called == []


def test_live_discord_blocked_guards_unmocked_requests_under_pytest(tmp_path, monkeypatch):
    """Verify _is_live_discord_blocked blocks real requests.* calls under pytest while allowing MagicMock/custom mocks."""
    tmp_db = tmp_path / "forge.db"
    forge_mod.init_forge_db(tmp_db)
    monkeypatch.delenv("FORGE_ALLOW_LIVE_DISCORD_IN_TESTS", raising=False)

    assert forge_mod._is_live_discord_blocked(forge_mod.requests.get) is True
    assert forge_mod._is_live_discord_blocked(forge_mod.requests.post) is True
    assert forge_mod._is_live_discord_blocked(forge_mod.requests.patch) is True
    assert forge_mod._is_live_discord_blocked(forge_mod.requests.put) is True
    assert forge_mod._is_live_discord_blocked(forge_mod.requests.delete) is True

    assert forge_mod._is_live_discord_blocked(MagicMock()) is False
    assert forge_mod._is_live_discord_blocked(lambda *a, **kw: None) is False

    # Fail test immediately if any underlying HTTP request via Session.request is ever attempted
    monkeypatch.setattr(
        forge_mod.requests.sessions.Session,
        "request",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Unmocked live Discord HTTP request fired!")),
    )

    # 1. _get_vulcan_webhook returns None without hitting network even if _is_prod_forge_env is forced True
    monkeypatch.setattr(forge_mod, "_is_prod_forge_env", lambda *a, **kw: True)
    monkeypatch.setattr(forge_mod, "VULCAN_WEBHOOK_CACHE_PATHS", (tmp_path / "no_webhook_cache.json",))
    assert forge_mod._get_vulcan_webhook(db_path=tmp_db) is None

    # 2. _post_forge_channel_message and _patch_forge_channel_message return safe no-ops with bot token
    assert (
        forge_mod._post_forge_channel_message(
            {"content": "test"},
            forge_mod.FORGE_CHANNEL_ID,
            token="fake-bot-token",
            db_path=tmp_db,
        )
        is None
    )
    assert (
        forge_mod._patch_forge_channel_message(
            123456789,
            {"content": "test"},
            forge_mod.FORGE_CHANNEL_ID,
            token="fake-bot-token",
            db_path=tmp_db,
        )
        == (False, 0)
    )

    # Also verify webhook branch in _post_forge_channel_message and _patch_forge_channel_message is blocked
    monkeypatch.setattr(
        forge_mod,
        "_get_vulcan_webhook",
        lambda *a, **kw: {"id": "111", "token": "tok", "name": "Vulcan"},
    )
    assert (
        forge_mod._post_forge_channel_message(
            {"content": "test"},
            forge_mod.FORGE_CHANNEL_ID,
            token="fake-bot-token",
            db_path=tmp_db,
        )
        is None
    )
    assert (
        forge_mod._patch_forge_channel_message(
            123456789,
            {"content": "test"},
            forge_mod.FORGE_CHANNEL_ID,
            token="fake-bot-token",
            db_path=tmp_db,
        )
        == (False, 0)
    )

    # 3. _post_decision_card_rest returns None without calling requests.post
    staged = forge_mod.stage_decision(
        category="ops",
        title="Test Isolation Decision",
        summary="Verify live Discord guard.",
        handler_type="command",
        payload={"command": "echo ok"},
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        db_path=tmp_db,
        post_to_discord=False,
    )
    did = staged["decision_id"] if isinstance(staged, dict) else str(staged)
    assert forge_mod._post_decision_card_rest(did, db_path=tmp_db, token="fake-bot-token") is None

    # 4. _merge_remote_active_embeds_into_header and _sweep_stale_forge_channel_embeds return safe no-ops
    assert (
        forge_mod._merge_remote_active_embeds_into_header(
            "Header Content",
            forge_mod.FORGE_CHANNEL_ID,
            "fake-bot-token",
        )
        == "Header Content"
    )
    assert (
        forge_mod._sweep_stale_forge_channel_embeds(
            cutoff=time.time(),
            forge_channel_id=forge_mod.FORGE_CHANNEL_ID,
            token="fake-bot-token",
            db_paths=[tmp_db],
        )
        == []
    )


def test_choice_button_in_flight_bypasses_approval_gate_for_home_channels(tmp_path, monkeypatch):
    """Verify from_choice_button=True in in_flight_turn.json bypasses evaluate_plan_approval_gate for home channels (even multi-chunk / core-bridge) unless requires_approval=True."""
    import tools.forge as forge_mod
    import tools.bridge_state as bstate

    db_path = tmp_path / "forge_choice_btn.db"
    forge_mod.init_forge_db(db_path)
    ifp = tmp_path / "in_flight_turn.json"
    monkeypatch.setattr(bstate, "IN_FLIGHT_FILE", ifp)

    # Record in-flight with from_choice_button=True via bridge_state.record_in_flight
    seq1 = bstate.record_in_flight(
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        prompt="Fix Test Isolation First",
        conv_id="conv-choice-1",
        from_choice_button=True,
    )
    assert seq1 is not None
    # Verify retry attempt of same prompt preserves from_choice_button=True even if omitted on second call
    seq2 = bstate.record_in_flight(
        channel_id=forge_mod.ZERO_CHAT_CHANNEL_ID,
        prompt="Fix Test Isolation First",
        conv_id="conv-choice-1",
        pid=12345,
    )
    assert seq2 is not None
    if_data = json.loads(ifp.read_text(encoding="utf-8"))
    assert if_data[str(forge_mod.ZERO_CHAT_CHANNEL_ID)]["from_choice_button"] is True
    assert if_data["from_choice_button"] is True

    # 1. Home channel multi-chunk + core-bridge plan bypasses approval gate when from_choice_button=True
    core_multi_plan = {
        "title": "Fix Test Isolation & Core Bridge",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.ZERO_CHAT_CHANNEL_ID,
        "touches_core_bridge": True,
        "chunks": [
            {"title": "Chunk 1", "target_files": ["tools/bridge.py"], "instructions": "Update bridge.py."},
            {"title": "Chunk 2", "target_files": ["tools/forge.py"], "instructions": "Update forge.py."},
        ],
    }
    req_home, reasons_home = forge_mod.evaluate_plan_approval_gate(
        core_multi_plan,
        batch_state_path="/tmp/nonexistent.json",
        in_flight_path=ifp,
        db_path=db_path,
    )
    assert req_home is False
    assert reasons_home == []

    # 2. Explicit requires_approval=True is NEVER bypassed even with from_choice_button=True
    explicit_req_plan = dict(core_multi_plan, requires_approval=True)
    req_explicit, reasons_explicit = forge_mod.evaluate_plan_approval_gate(
        explicit_req_plan,
        batch_state_path="/tmp/nonexistent.json",
        in_flight_path=ifp,
        db_path=db_path,
    )
    assert req_explicit is True
    assert any("Explicit requires_approval" in r for r in reasons_explicit)

    # 3. Non-home / Crab Cavern channel is NEVER bypassed by from_choice_button=True
    crab_channel_id = 1534452820995080192  # #lounge
    bstate.record_in_flight(
        channel_id=crab_channel_id,
        prompt="Fix Test Isolation First",
        conv_id="conv-crab-1",
        from_choice_button=True,
    )
    crab_plan = dict(core_multi_plan, origin_channel_id=crab_channel_id)
    req_crab, reasons_crab = forge_mod.evaluate_plan_approval_gate(
        crab_plan,
        batch_state_path="/tmp/nonexistent.json",
        in_flight_path=ifp,
        db_path=db_path,
    )
    assert req_crab is True
    assert len(reasons_crab) > 0


def test_explicit_approval_prompt_natural_directives_and_questions(tmp_path):
    """Verify natural directives ('build it', 'fix both', 'let's do that') bypass, while questions ('should we build it?') do NOT bypass."""
    import tools.forge as forge_mod

    db_path = tmp_path / "forge_directives.db"
    forge_mod.init_forge_db(db_path)
    ifp = tmp_path / "in_flight_turn.json"

    multi_plan = {
        "title": "Wave 2 Pipeline",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.ZERO_CHAT_CHANNEL_ID,
        "chunks": [
            {"title": "Chunk 1", "target_files": ["tools/a.py"], "instructions": "Update a.py."},
            {"title": "Chunk 2", "target_files": ["tools/b.py"], "instructions": "Update b.py."},
        ],
    }

    natural_directives = [
        "build it",
        "build this now",
        "run it",
        "fix both",
        "fix this",
        "patch it",
        "patch both",
        "let's do that",
        "lets do wave 2",
        "let us do both",
        "go for it",
        "make it happen",
        "wire it up",
        "kick it off",
        "queue wave 2",
        "queue forge fix now",
        "yes please",
        "Yes",
        "persist the state fix",
        '[USER MID-TURN STEERING UPDATE]\n\n[ORIGINAL IN-FLIGHT TASK BEING EXECUTED]:\n"Queue FORGE Fix Now"\n\n[NEW DIRECTIVE RECEIVED MID-TURN]:\n"Can you also make sure audio keeps playing when I swipe away?"',
    ]
    for directive in natural_directives:
        assert forge_mod._is_explicit_approval_prompt(directive) is True, f"Expected '{directive}' to match"
        ifp.write_text(
            json.dumps({str(forge_mod.ZERO_CHAT_CHANNEL_ID): {"prompt": directive}}),
            encoding="utf-8",
        )
        req, reasons = forge_mod.evaluate_plan_approval_gate(
            multi_plan,
            batch_state_path="/tmp/nonexistent.json",
            in_flight_path=ifp,
            db_path=db_path,
        )
        assert req is False, f"Directive '{directive}' should bypass approval gate"
        assert reasons == []

    questions_and_non_directives = [
        "should we build it?",
        "should we fix both",
        "what if we patch it",
        "tell me more about how we wire it up",
        "why did we build this?",
        "Fix Test Isolation First",
        "Quiet Wave 2 (No Prep Pings)",
    ]
    for q in questions_and_non_directives:
        assert forge_mod._is_explicit_approval_prompt(q) is False, f"Expected '{q}' NOT to match"
        ifp.write_text(
            json.dumps({str(forge_mod.ZERO_CHAT_CHANNEL_ID): {"prompt": q}}),
            encoding="utf-8",
        )
        req_q, reasons_q = forge_mod.evaluate_plan_approval_gate(
            multi_plan,
            batch_state_path="/tmp/nonexistent.json",
            in_flight_path=ifp,
            db_path=db_path,
        )
        assert req_q is True, f"Question/non-directive '{q}' should NOT bypass approval gate"
        assert len(reasons_q) > 0


def test_approval_gate_excludes_test_doc_prompt_files_from_blast_radius(tmp_path):
    """Verify test/doc/prompt/example files do not trigger the wide-blast-radius file count gate and routine 2-step scratch probes do not trigger post-calibration gate."""
    import tools.forge as forge_mod

    db_path = tmp_path / "forge_blast.db"
    forge_mod.init_forge_db(db_path)
    nonexistent_ifp = tmp_path / "nonexistent_if.json"

    # 5 production files + 4 test/doc/prompt/example files = 9 total files, but <6 production files -> no wide blast radius gate
    plan_with_tests_and_docs = {
        "title": "Feature With Tests and Docs",
        "repo": "/workspace",
        "origin_channel_id": forge_mod.ZERO_OPS_CHANNEL_ID,
        "chunks": [
            {
                "title": "Single Chunk",
                "target_files": [
                    "tools/prod_a.py",
                    "tools/prod_b.py",
                    "tools/prod_c.py",
                    "tools/prod_d.py",
                    "tools/prod_e.py",
                    "tools/test_prod_a.py",
                    "tools/prod_b_test.py",
                    "docs/architecture.md",
                    "config/settings.example",
                ],
                "instructions": "Implement prod_a..e and update tests/docs.",
            }
        ],
    }

    # Routine 2-step read-only diagnostic scratch probe over 120s should also NOT trigger post-calibration gate
    bs_routine = tmp_path / "batch_routine.json"
    now_ts = time.time()
    bs_routine.write_text(
        json.dumps({"last_time": now_ts, "turn_start_time": now_ts - 120.0, "total_scratch_runs": 2}),
        encoding="utf-8",
    )

    req, reasons = forge_mod.evaluate_plan_approval_gate(
        plan_with_tests_and_docs,
        batch_state_path=bs_routine,
        in_flight_path=nonexistent_ifp,
        db_path=db_path,
    )
    assert req is False, f"Expected no approval required, got reasons: {reasons}"
    assert reasons == []


if __name__ == "__main__":
    unittest.main()
