"""
test_forge.py — Comprehensive Unit Tests for FORGE Engine & Zero-Vault Encryption.

Covers:
1. Activity-Renewed Lease & 4-Signal Heartbeat Tracker (SDK tasks, subagent transcripts, runaway loops).
2. ForgePlan Validation, Multi-Chunk Enqueueing, Prompt Hydration, and Originating Channel Notification.
3. Deterministic Maker-Checker Worktree Gate & Prohibited File Boundary Enforcement.
4. Decisions Framework (Approve / Edit / Dismiss, TTL Expiry, Privacy Filtering in /queue).
5. Zero-Vault AES-256-GCM Secret Archive Encryption & Round-Trip Decryption + Platform Rule Size Guard.
"""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

from tools.forge import (
    FORGE_CHANNEL_ID,
    OWNER_USER_ID,
    ZERO_CHAT_CHANNEL_ID,
    ZERO_OPS_CHANNEL_ID,
    HeartbeatTracker,
    check_runaway_tool_loop,
    dismiss_decision,
    dispatch_build_completion_to_origin,
    enqueue_build,
    execute_decision,
    expire_stale_decisions,
    format_chunk_prompt,
    format_origin_completion_envelope,
    format_queue_digest,
    init_forge_db,
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

    def test_01_validate_and_enqueue_forge_plan(self):
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

        # Valid 2-chunk plan
        good_plan = {
            "title": "Upgrade Telemetry & Queue Engine",
            "origin_channel_id": ZERO_OPS_CHANNEL_ID,
            "origin_thread_id": 1599999999999999999,
            "repo": "/workspace",
            "tier": 3,
            "context_fact_sheet": [
                "Use WAL mode on SQLite connections.",
                "Never block interactive channel turns over 90 seconds.",
            ],
            "chunks": [
                {
                    "title": "Build SQLite Schema",
                    "worker_model": "gemini-3.8-flash-high",
                    "target_files": ["tools/forge.py"],
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
                    "verification_cmd": "pytest tools/test_forge.py",
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

        # Verify widget rendering and chunk prompt hydration
        widget = render_build_widget_text(build_id, db_path=self.db_path)
        self.assertIn("Upgrade Telemetry & Queue Engine", widget)
        self.assertIn("0/2 chunk(s) merged", widget)
        self.assertIn("gemini-3.8-flash-high", widget)

        prompt_c0 = format_chunk_prompt(build_id, 0, db_path=self.db_path, worktree_dir="/tmp/forge-wt-test")
        self.assertIn("Use WAL mode on SQLite connections.", prompt_c0)
        self.assertIn("**Prohibited Files (DO NOT MODIFY):** `tools/bridge.py`", prompt_c0)
        self.assertIn("DO NOT implement the following upcoming chunks", prompt_c0)
        self.assertIn("Chunk 2: Wire Bridge Slash Command", prompt_c0)

        # Verify originating-channel completion envelope
        env_text = format_origin_completion_envelope(build_id, db_path=self.db_path)
        self.assertIn(build_id, env_text)
        self.assertIn("touches_core_bridge=true", env_text)

        with patch("tools.outbox.queue_outbox_message") as mock_outbox:
            ok = dispatch_build_completion_to_origin(build_id, db_path=self.db_path)
            self.assertTrue(ok)
            mock_outbox.assert_called_once()
            self.assertEqual(mock_outbox.call_args.kwargs["channel"], 1599999999999999999)
            self.assertEqual(mock_outbox.call_args.kwargs["msg_type"], "agent_turn")

    def test_02_decisions_framework_and_privacy_filter(self):
        # 1. Stage a public ops decision in #zero-ops
        d_ops = stage_decision(
            category="homelab",
            title="Restart Z-Wave JS UI",
            summary="Node 14 ping timed out; run scoped compose restart.",
            handler_type="shell",
            payload={"command": "echo 'restarted zwave'", "cwd": "/tmp"},
            channel_id=ZERO_OPS_CHANNEL_ID,
            db_path=self.db_path,
        )
        # 2. Stage a private email decision in #zero-chat
        d_priv = stage_decision(
            category="email",
            title="Send Contractor Reply",
            summary="Confirm Tuesday 9:00 AM PT window.",
            handler_type="shell",
            payload={"command": "echo 'sent mail'", "cwd": "/tmp"},
            channel_id=ZERO_CHAT_CHANNEL_ID,
            db_path=self.db_path,
        )

        # Running /queue in #zero-ops must hide the private #zero-chat decision
        digest_ops = format_queue_digest(request_channel_id=ZERO_OPS_CHANNEL_ID, db_path=self.db_path)
        self.assertIn("Restart Z-Wave JS UI", digest_ops)
        self.assertNotIn("Send Contractor Reply", digest_ops)

        # Running /queue in #zero-chat shows both
        digest_priv = format_queue_digest(request_channel_id=ZERO_CHAT_CHANNEL_ID, db_path=self.db_path)
        self.assertIn("Restart Z-Wave JS UI", digest_priv)
        self.assertIn("Send Contractor Reply", digest_priv)

        # Unauthorized user cannot approve
        unauth = execute_decision(d_ops["decision_id"], user_id=999999, db_path=self.db_path)
        self.assertFalse(unauth["ok"])

        # Authorized user approves -> executes in <1s and produces receipt
        exec_res = execute_decision(d_ops["decision_id"], user_id=OWNER_USER_ID, db_path=self.db_path)
        self.assertTrue(exec_res["ok"])
        self.assertEqual(exec_res["status"], "approved")
        self.assertIn("restarted zwave", exec_res["receipt"])
        self.assertIn("Approved & Executed", exec_res["formatted_card"])

        # Dismiss the second decision
        dis_res = dismiss_decision(d_priv["decision_id"], user_id=OWNER_USER_ID, db_path=self.db_path)
        self.assertTrue(dis_res["ok"])
        self.assertEqual(dis_res["status"], "dismissed")

        # Stage an already-expired decision and verify expire_stale_decisions
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

        # Create an active SDK task log and verify heartbeat detection
        task_log = tasks_dir / "task-1.log"
        task_log.write_text("Step 1 output...\n", encoding="utf-8")
        self.assertTrue(tracker.poll_disk_progress())
        # Second poll without new bytes returns False
        self.assertFalse(tracker.poll_disk_progress())
        # Append bytes -> returns True
        task_log.write_text("Step 1 output...\nStep 2 output...\n", encoding="utf-8")
        self.assertTrue(tracker.poll_disk_progress())

        # Test 3-strike runaway tool loop detector
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

    def test_04_verify_chunk_worktree_and_boundary_guard(self):
        wt_dir = Path(self.tmp_dir.name) / "wt_repo"
        wt_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "noreply@github.com"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Zero"], cwd=str(wt_dir), check=True, capture_output=True)

        (wt_dir / "allowed.py").write_text("x = 1\n", encoding="utf-8")
        (wt_dir / "prohibited.py").write_text("y = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(wt_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(wt_dir), check=True, capture_output=True)

        # Modify prohibited file -> must fail boundary guard
        (wt_dir / "prohibited.py").write_text("y = 2\n", encoding="utf-8")
        passed, _, err = verify_chunk_worktree(wt_dir, "true", prohibited_files=["prohibited.py"])
        self.assertFalse(passed)
        self.assertIn("Boundary violation", err)

        # Restore prohibited file and modify allowed file -> passes
        subprocess.run(["git", "checkout", "prohibited.py"], cwd=str(wt_dir), check=True, capture_output=True)
        (wt_dir / "allowed.py").write_text("x = 2\n", encoding="utf-8")
        passed, diff_stat, err = verify_chunk_worktree(wt_dir, "python3 -c 'import allowed; assert allowed.x == 2'", prohibited_files=["prohibited.py"])
        self.assertTrue(passed, err)
        self.assertIn("1 file changed", diff_stat)

    def test_05_vault_aes256_gcm_roundtrip_and_rule_platform_limits(self):
        sec_dir = Path(self.tmp_dir.name) / "mock_secrets"
        sec_dir.mkdir(parents=True, exist_ok=True)
        (sec_dir / "env.json").write_text('{"MOCK_KEY": "super-secret"}', encoding="utf-8")
        (sec_dir / "pii_rules.json").write_text('{"patterns": []}', encoding="utf-8")
        key_file = sec_dir / ".vault_master.key"
        out_enc = Path(self.tmp_dir.name) / "secrets.tar.gz.enc"

        enc_res = encrypt_secrets_archive(secrets_dir=sec_dir, output_file=out_enc, key_path=key_file)
        self.assertEqual(enc_res["included_count"], 2)
        self.assertTrue(out_enc.exists())
        # Verify encrypted file does not contain plaintext secret
        raw_cipher = out_enc.read_bytes()
        self.assertNotIn(b"super-secret", raw_cipher)

        members = decrypt_secrets_archive(encrypted_file=out_enc, key_path=key_file)
        self.assertEqual(sorted(members), ["env.json", "pii_rules.json"])

        # Verify Antigravity platform rule file size limits (<= 24,000 bytes per file)
        agents_md = Path("/workspace/agents.md")
        self.assertLessEqual(agents_md.stat().st_size, 24000)
        for rule_file in Path("/workspace/.agents/rules").glob("*.md"):
            self.assertLessEqual(rule_file.stat().st_size, 24000, f"{rule_file.name} exceeds 24KB limit")


if __name__ == "__main__":
    unittest.main()
