#!/usr/bin/env python3
"""Unit tests for tools/zero_dashboard.py and zero_health_server routing enhancements."""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

CURRENT_DIR = Path(__file__).resolve().parent.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.append(str(WORKSPACE))

import tools.zero_dashboard as zdash
import tools.zero_health_server as zhealth


class TestZeroDashboard(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.orig_data_dir = zdash.DATA_DIR
        self.orig_db_path = zdash.DB_PATH
        self.orig_brain_dir = zdash.BRAIN_DIR
        self.orig_sessions_file = zdash.SESSIONS_FILE

        zdash.DATA_DIR = self.temp_path
        zdash.DB_PATH = self.temp_path / "telemetry.db"
        self.orig_forge_db_path = getattr(zdash, "FORGE_DB_PATH", None)
        zdash.FORGE_DB_PATH = self.temp_path / "forge.db"
        zdash.BRAIN_DIR = self.temp_path / "brain"
        zdash.SESSIONS_FILE = self.temp_path / "sessions.json"

    def tearDown(self):
        zdash.DATA_DIR = self.orig_data_dir
        zdash.DB_PATH = self.orig_db_path
        zdash.FORGE_DB_PATH = self.orig_forge_db_path
        zdash.BRAIN_DIR = self.orig_brain_dir
        zdash.SESSIONS_FILE = self.orig_sessions_file
        self.temp_dir.cleanup()

    def test_get_active_model_config(self):
        cfg = zdash.get_active_model_config()
        self.assertIn("limit", cfg)
        self.assertIn("tripwire", cfg)
        self.assertIn("name", cfg)
        self.assertEqual(cfg["limit"], 1000000)
        self.assertEqual(cfg["tripwire"], 800000)

    def test_get_sessions_list_empty(self):
        sessions = zdash.get_sessions_list()
        self.assertIsInstance(sessions, list)

    def test_parse_session_transcript_sample(self):
        conv_id = "test-session-123"
        log_dir = zdash.BRAIN_DIR / conv_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        tfile = log_dir / "transcript.jsonl"

        lines = [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-09-28T15:00:00Z", "content": "Check context"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-09-28T15:00:05Z",
                "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": "/workspace/tools/bridge.py"}}],
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-09-28T15:00:10Z",
                "thinking": "Inspecting code structure",
                "content": "All clear",
            },
        ]
        with open(tfile, "w") as f:
            for l in lines:
                f.write(json.dumps(l) + "\n")

        timeline, tool_usage, running_tokens, compactions = zdash.parse_session_transcript(conv_id)
        self.assertEqual(len(timeline), 3)
        self.assertEqual(timeline[0]["category"], "user")
        self.assertEqual(timeline[1]["category"], "tool")
        self.assertEqual(timeline[1]["tool"], "view_file")
        self.assertEqual(timeline[2]["category"], "reasoning")
        self.assertEqual(tool_usage.get("view_file"), 1)
        self.assertGreater(running_tokens, 10000)

    def test_render_dashboard_html(self):
        html = zdash.render_dashboard_html()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("channel-select", html)
        self.assertIn("timeline-svg", html)
        self.assertIn("bg-slate-950", html)
        self.assertIn("Tripwire", html)
        self.assertIn("% Weekly", html)
        self.assertIn("Ground-Truth Account Quota Telemetry", html)
        self.assertIn("data:image/", html)
        self.assertIn("Gemini Weekly", html)
        self.assertIn("Gemini 5-Hour", html)
        self.assertNotIn("Claude & GPT Weekly", html)
        self.assertNotIn("Claude & GPT 5-Hour", html)

    def test_get_telemetry_payload_structure(self):
        payload = zdash.get_telemetry_payload()
        self.assertIn("status", payload)
        self.assertIn("model", payload)
        self.assertIn("sessions", payload)
        self.assertIn("selected_session", payload)
        self.assertIn("channel_rollups", payload)
        self.assertIn("weekly_token_ceiling", payload)
        self.assertIn("quota", payload)
        self.assertIn("quota_attribution", payload)
        self.assertEqual(payload["status"], "healthy")


    def test_get_vulcan_forge_payload_empty(self):
        payload = zdash.get_vulcan_forge_payload()
        self.assertIn("summary", payload)
        self.assertIn("kpis", payload)
        self.assertIn("builds", payload)
        self.assertIn("jobs", payload)
        self.assertIn("decisions", payload)
        self.assertEqual(payload["active_builds_count"], 0)
        self.assertEqual(payload["queued_builds_count"], 0)
        self.assertEqual(payload["completed_builds_count"], 0)
        self.assertEqual(payload["failed_builds_count"], 0)
        self.assertEqual(payload["active_jobs_count"], 0)
        self.assertEqual(payload["pending_decisions_count"], 0)
        self.assertEqual(payload["total_tokens"], 0)
        self.assertEqual(payload["pct_weekly"], 0.0)

    def test_get_vulcan_forge_payload_with_mock_databases(self):
        import sqlite3
        # 1. Telemetry DB with forge parent_channel_name
        tconn = sqlite3.connect(str(self.temp_path / "telemetry.db"))
        tconn.execute("""
            CREATE TABLE token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_id INTEGER,
                channel_name TEXT,
                parent_channel_id INTEGER,
                parent_channel_name TEXT,
                is_thread INTEGER DEFAULT 0,
                thread_id INTEGER,
                conv_id TEXT,
                mode TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                thinking_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                is_backfill INTEGER DEFAULT 0
            );
        """)
        now = time.time()
        tconn.execute(
            """
            INSERT INTO token_usage (
                timestamp, channel_name, parent_channel_name, mode,
                input_tokens, output_tokens, cache_read_tokens, thinking_tokens, total_tokens
            ) VALUES (?, 'forge-fg-100', 'forge', 'forge', 1000, 500, 4000, 200, 5500);
            """,
            (now,),
        )
        tconn.commit()
        tconn.close()

        # 2. Forge DB with builds, chunks, jobs, decisions
        fconn = sqlite3.connect(str(self.temp_path / "forge.db"))
        fconn.execute("""
            CREATE TABLE forge_builds (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                repo TEXT NOT NULL DEFAULT '/workspace',
                origin_channel_id INTEGER NOT NULL,
                origin_thread_id INTEGER,
                origin_conv_id TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                total_tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        fconn.execute("""
            CREATE TABLE forge_chunks (
                id TEXT PRIMARY KEY,
                build_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                worker_model TEXT NOT NULL DEFAULT 'gemini-3.8-flash-high',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                branch_name TEXT,
                diff_stat TEXT,
                last_error TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            );
        """)
        fconn.execute("""
            CREATE TABLE forge_jobs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                command TEXT NOT NULL,
                cwd TEXT NOT NULL DEFAULT '/workspace',
                channel TEXT NOT NULL DEFAULT 'zero-chat',
                status TEXT NOT NULL DEFAULT 'starting',
                worker_pid INTEGER,
                child_pid INTEGER,
                exit_code INTEGER,
                log_file TEXT,
                start_time REAL NOT NULL,
                end_time REAL,
                duration_seconds REAL
            );
        """)
        fconn.execute("""
            CREATE TABLE forge_decisions (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                handler_type TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                is_private INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                resolved_at REAL,
                receipt_text TEXT NOT NULL DEFAULT ''
            );
        """)
        fconn.execute(
            "INSERT INTO forge_builds VALUES ('fg-1', 'Build 1', '/workspace', 1562899941595160657, NULL, NULL, 'running', 5500, ?, ?)",
            (now - 60, now),
        )
        fconn.execute(
            "INSERT INTO forge_builds VALUES ('fg-2', 'Build 2', '/workspace', 1562899941595160657, NULL, NULL, 'completed', 12000, ?, ?)",
            (now - 120, now - 60),
        )
        fconn.execute(
            "INSERT INTO forge_chunks VALUES ('fg-1-c0', 'fg-1', 0, 'Chunk Zero', 'gemini-3.8-flash-high', 1, 5500, 'forge/fg-1', '1 file changed', '', 'running')"
        )
        fconn.execute(
            "INSERT INTO forge_jobs VALUES ('job-1', 'Detached Task', 'pytest', '/workspace', 'zero-ops', 'running', 100, 101, NULL, '/tmp/log', ?, NULL, NULL)",
            (now,),
        )
        fconn.execute(
            "INSERT INTO forge_decisions VALUES ('dec-1', 'forge_enqueue', 'Decision Alpha', 'Summary card', 'forge', 1562899941595160657, NULL, 0, 'pending', ?, ?, NULL, '')",
            (now + 3600, now),
        )
        fconn.commit()
        fconn.close()

        payload = zdash.get_vulcan_forge_payload(
            limit=10,
            forge_db_path=self.temp_path / "forge.db",
            telemetry_db_path=self.temp_path / "telemetry.db",
        )
        self.assertEqual(payload["active_builds_count"], 1)
        self.assertEqual(payload["completed_builds_count"], 1)
        self.assertEqual(payload["active_jobs_count"], 1)
        self.assertEqual(payload["pending_decisions_count"], 1)
        self.assertEqual(payload["total_tokens"], 5500)
        self.assertEqual(payload["input_tokens"], 1000)
        self.assertEqual(payload["output_tokens"], 500)
        self.assertEqual(payload["cache_read_tokens"], 4000)
        self.assertEqual(payload["cache_hit_rate"], 80.0)
        self.assertGreater(payload["pct_weekly"], 0.0)

        self.assertEqual(len(payload["builds"]), 2)
        b1 = payload["builds"][0]
        self.assertEqual(b1["build_id"], "fg-1")
        self.assertEqual(b1["status"], "running")
        self.assertEqual(len(b1["chunks"]), 1)
        self.assertEqual(b1["chunks"][0]["attempts"], 1)
        self.assertEqual(b1["chunks"][0]["tokens"], 5500)
        self.assertEqual(b1["chunks"][0]["model"], "gemini-3.8-flash-high")

        self.assertEqual(len(payload["jobs"]), 1)
        self.assertEqual(payload["jobs"][0]["id"], "job-1")
        self.assertEqual(payload["jobs"][0]["status"], "running")

        self.assertEqual(len(payload["decisions"]), 1)
        self.assertEqual(payload["decisions"][0]["id"], "dec-1")
        self.assertEqual(payload["decisions"][0]["status"], "pending")

    def test_get_telemetry_payload_includes_vulcan(self):
        payload = zdash.get_telemetry_payload()
        self.assertIn("vulcan", payload)
        vulcan = payload["vulcan"]
        self.assertIn("summary", vulcan)
        self.assertIn("builds", vulcan)
        self.assertIn("jobs", vulcan)
        self.assertIn("decisions", vulcan)
        self.assertIn("active_builds_count", vulcan)

    def test_render_dashboard_html_vulcan_subtab(self):
        html = zdash.render_dashboard_html()
        self.assertIn("Context &amp; Channel Telemetry", html)
        self.assertIn("Vulcan &amp; Build Queue", html)
        self.assertIn("tab-pane-vulcan", html)
        self.assertIn("tab-pane-telemetry", html)
        self.assertIn("switchMainTab", html)
        self.assertIn("renderVulcanTab", html)
        self.assertIn("vulcan-kpi-tokens", html)
        self.assertIn("vulcan-builds-table-body", html)
        self.assertIn("vulcan-jobs-table-body", html)
        self.assertIn("vulcan-decisions-table-body", html)

        # Ivy dashboard must NOT include Vulcan tab
        html_ivy = zdash.render_dashboard_html(agent="ivy")
        self.assertNotIn("Vulcan &amp; Build Queue", html_ivy)
        self.assertNotIn('<div id="tab-pane-vulcan"', html_ivy)

class TestZeroHealthServerRouting(unittest.TestCase):

    def _mock_handler(self, path: str, headers: dict = None):
        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        handler.path = path
        handler.headers = headers or {}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_do_GET_html_dashboard_for_browser(self):
        handler = self._mock_handler("/", headers={"Accept": "text/html,application/xhtml+xml"})
        handler.do_GET()

        handler.send_response.assert_called_with(200)
        # Check Content-Type header was set to html
        call_args = [c[0] for c in handler.send_header.call_args_list]
        self.assertIn(("Content-Type", "text/html; charset=utf-8"), call_args)

        output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("<!DOCTYPE html>", output)
        self.assertIn("channel-select", output)

    def test_do_GET_json_health_for_api_probe(self):
        handler = self._mock_handler("/api/health", headers={"Accept": "application/json"})
        handler.do_GET()

        handler.send_response.assert_called_with(200)
        call_args = [c[0] for c in handler.send_header.call_args_list]
        self.assertIn(("Content-Type", "application/json"), call_args)

        output = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertIn("status", output)
        self.assertIn("service", output)
        self.assertEqual(output["service"], "zero-health")

    def test_do_GET_telemetry_api(self):
        handler = self._mock_handler("/api/telemetry", headers={"Accept": "application/json"})
        handler.do_GET()

        handler.send_response.assert_called_with(200)
        output = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(output["status"], "healthy")
        self.assertIn("selected_session", output)
        self.assertIn("model", output)

    def test_do_GET_vulcan_api(self):
        handler = self._mock_handler("/api/vulcan", headers={"Accept": "application/json"})
        handler.do_GET()

        handler.send_response.assert_called_with(200)
        call_args = [c[0] for c in handler.send_header.call_args_list]
        self.assertIn(("Content-Type", "application/json"), call_args)

        output = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertIn("summary", output)
        self.assertIn("builds", output)
        self.assertIn("jobs", output)
        self.assertIn("decisions", output)
        self.assertIn("active_builds_count", output)

    def test_do_HEAD_vulcan_api(self):
        handler = self._mock_handler("/api/vulcan")
        handler.do_HEAD()

        handler.send_response.assert_called_with(200)
        call_args = [c[0] for c in handler.send_header.call_args_list]
        self.assertIn(("Content-Type", "application/json"), call_args)

    def test_do_GET_sessions_api(self):
        handler = self._mock_handler("/api/sessions", headers={"Accept": "application/json"})
        handler.do_GET()

        handler.send_response.assert_called_with(200)
        output = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertIn("sessions", output)


    def test_do_GET_avatar(self):
        handler = self._mock_handler("/api/avatar")
        handler.do_GET()

        handler.send_response.assert_called_with(200)
        call_args = [c[0] for c in handler.send_header.call_args_list]
        self.assertIn(("Content-Type", "image/png"), call_args)

    def test_render_dashboard_html_ivy(self):
        html = zdash.render_dashboard_html(agent="ivy")
        self.assertIn("IVY", html)
        self.assertIn("SABERMETRIC GM", html)
        self.assertIn("Gemini 3.8 Flash (High)", html)
        self.assertIn("#baseball", html)
        self.assertIn("7-Day Token Summary", html)
        self.assertNotIn("Daemon & Background Compute", html)
        self.assertNotIn('<select id="channel-select"', html)
        self.assertNotIn("{{BRAND_NAME}}", html)
        self.assertNotIn("{{PAGE_TITLE}}", html)

    def test_do_GET_ivy_host(self):
        handler = self._mock_handler("/", headers={"Host": "ivy.brock.ventures", "Accept": "text/html"})
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("IVY", output)
        self.assertIn("SABERMETRIC GM", output)

    def test_do_GET_ivy_health(self):
        handler = self._mock_handler("/api/health", headers={"Host": "ivy.brock.ventures", "Accept": "application/json"})
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        output = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(output.get("agent"), "Ivy")
        self.assertEqual(output.get("service"), "ivy-health")

    def test_backfill_classify_conversation_baseball_and_sidecar_isolation(self):
        from tools.backfill_telemetry import classify_conversation

        # 1. Memory Consolidation Evaluator mentioning #baseball must stay sidecar
        prompt_mc = (
            "<USER_REQUEST>\nYou are the Memory Consolidation Evaluator for Zero.\n"
            "Transcript: [Session: 1548196929308065893] discussing #baseball and Ivy."
        )
        chid, chname, mode = classify_conversation("conv-mc", prompt_mc, prompt_mc, 4, None)
        self.assertIsNone(chid)
        self.assertEqual(chname, "memory-consolidation")
        self.assertEqual(mode, "sidecar")

        # 2. Multi-step #zero-chat session whose GEMINI.md / memory mentions #baseball must not mislabel as baseball
        prompt_zc = "<USER_REQUEST>\n[System Time]: Wed Oct 14\nCheck token burn."
        sample_zc = prompt_zc + "\nGEMINI.md: Dedicated Excluded Channel Quarantine (#baseball 1548196929308065893)"
        chid2, chname2, mode2 = classify_conversation("conv-zc", prompt_zc, sample_zc, 40, None)
        self.assertEqual(chid2, 1542081375287640084)
        self.assertEqual(chname2, "zero-chat")
        self.assertEqual(mode2, "home")

        # 3. Genuine #baseball session with explicit channel header
        prompt_bb = "<USER_REQUEST>\n--- RECENT CHANNEL HISTORY (#baseball, last 5 messages) ---"
        chid3, chname3, mode3 = classify_conversation("conv-bb", prompt_bb, prompt_bb, 25, None)
        self.assertEqual(chid3, 1548196929308065893)
        self.assertEqual(chname3, "baseball")
        self.assertEqual(mode3, "home")


if __name__ == "__main__":
    unittest.main()

