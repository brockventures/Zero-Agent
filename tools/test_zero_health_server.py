"""
Unit test suite for tools/zero_health_server.py.
Validates dynamic gateway heartbeat evaluation, degraded HTTP 503 response, and healthy HTTP 200 response.
"""

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

import tools.zero_health_server as zhealth


class TestZeroHealthServer(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self._orig_data_dir = zhealth.DATA_DIR
        self._orig_fallback_token_file = zhealth.FALLBACK_GOTCHI_TOKEN_FILE
        self._orig_secrets_env_file = zhealth.SECRETS_ENV_FILE
        zhealth.DATA_DIR = self.temp_path
        zhealth.FALLBACK_GOTCHI_TOKEN_FILE = self.temp_path / "fallback_gotchi_bearer_token.json"
        zhealth.SECRETS_ENV_FILE = self.temp_path / "secrets_env.json"

    def tearDown(self):
        zhealth.DATA_DIR = self._orig_data_dir
        zhealth.FALLBACK_GOTCHI_TOKEN_FILE = self._orig_fallback_token_file
        zhealth.SECRETS_ENV_FILE = self._orig_secrets_env_file
        self.temp_dir.cleanup()

    @staticmethod
    def _make_handler(
        path: str,
        headers: dict,
        body: bytes = b"",
        command: str = "GET",
        client_address: tuple[str, int] = ("127.0.0.1", 12345),
    ):
        h = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        h.path = path
        h.headers = headers
        h.command = command
        h.client_address = client_address
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.send_response = MagicMock()
        h.send_header = MagicMock()
        h.end_headers = MagicMock()
        return h

    @staticmethod
    def _sent_headers(handler) -> dict[str, str]:
        return {call.args[0].lower(): call.args[1] for call in handler.send_header.call_args_list}

    def test_health_evaluation_healthy(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 10,
            "gateway_status": "connected",
            "gateway_latency_ms": 42.5,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()

        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["components"]["discord_gateway"], "connected")
        self.assertLessEqual(payload["components"]["gateway_heartbeat_age_seconds"], 15)

    def test_health_evaluation_stale_gateway_heartbeat(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 240,  # 4 minutes old (> 180s)
            "gateway_status": "connected",
            "gateway_latency_ms": 42.5,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        # Default mode returns 200 with turn_state OFFLINE for clean dashboard rendering
        code, payload = handler._evaluate_health()
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["turn_state"], "OFFLINE")
        self.assertEqual(payload["components"]["discord_gateway"], "stalled_or_disconnected")
        self.assertGreater(payload["components"]["gateway_heartbeat_age_seconds"], 180)

        # Strict mode returns 503 for monitoring checks
        code_strict, _ = handler._evaluate_health(strict=True)
        self.assertEqual(code_strict, 503)

    def test_health_evaluation_disconnected_gateway(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 10,
            "gateway_status": "disconnected",
            "gateway_latency_ms": None,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["turn_state"], "OFFLINE")
        self.assertEqual(payload["components"]["discord_gateway"], "stalled_or_disconnected")

        code_strict, _ = handler._evaluate_health(strict=True)
        self.assertEqual(code_strict, 503)

    def test_health_evaluation_enriched_status_fields(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 5,
            "gateway_status": "connected",
            "gateway_latency_ms": 35.0,
            "state": "PROCESSING"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))

        # Write dummy in-flight turn with self PID
        in_flight_data = {
            "123": {
                "channel_id": 123,
                "prompt": "Test prompt",
                "ts": now - 20,
                "pid": os.getpid()
            }
        }
        (self.temp_path / "in_flight_turn.json").write_text(json.dumps(in_flight_data))

        # Write dummy detached tasks (1 running)
        detached_dir = self.temp_path / "detached_tasks" / "task_abc"
        detached_dir.mkdir(parents=True, exist_ok=True)
        (detached_dir / "meta.json").write_text(json.dumps({
            "task_id": "task_abc",
            "status": "running",
            "child_pid": os.getpid(),
            "start_time": now - 50
        }))

        # Write dummy daemon pids
        (self.temp_path / "daemon_pids.json").write_text(json.dumps({"pids": [os.getpid()]}))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()

        self.assertEqual(code, 200)
        self.assertEqual(payload["turn_state"], "PROCESSING")
        # in_flight_turns should be sum of 1 active turn + 1 detached task = 2
        self.assertEqual(payload["in_flight_turns"], 2)
        self.assertEqual(payload["active_turns"], 1)
        self.assertEqual(payload["active_detached_tasks"], 1)
        self.assertEqual(payload["daemon_workers"], 1)
        self.assertIn("active_model", payload)
        self.assertIn("model_short", payload)
        self.assertIn("messages_sent", payload)
        self.assertIn("uptime_human", payload)
        self.assertEqual(len(payload["in_flight"]), 1)
        self.assertEqual(payload["in_flight"][0]["prompt"], "Test prompt")

    def test_health_evaluation_custom_bot_stats(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 5,
            "gateway_status": "connected",
            "gateway_latency_ms": 12.0,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))
        (self.temp_path / "bot_stats.json").write_text(json.dumps({"all_time_messages_sent": 2450}))

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        code, payload = handler._evaluate_health()

        self.assertEqual(code, 200)
        self.assertEqual(payload["messages_sent"], 2450)
        self.assertEqual(payload["components"]["messages_sent"], 2450)

    @patch("tools.zero_health_server.run_ssh")
    def test_ivy_evaluation_success(self, mock_ssh):
        zhealth.IVY_CACHE = {"ts": 0.0, "status_code": 200, "payload": None}
        mock_output = json.dumps({
            "container_status": "running",
            "session_meta": {"turns": 14},
            "runtime_config": {"model": "gemini-4-hydrogen-high"},
            "bot_stats": {"all_time_messages_sent": 820},
            "in_flight": {"ch_1": {"prompt": "Analyze Ohtani", "ts": time.time() - 10}},
            "active_detached_tasks": 1,
            "daemon_running": True
        })
        mock_ssh.return_value = (0, mock_output, "")

        code, payload = zhealth._evaluate_ivy(force=True)

        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "healthy")
        self.assertEqual(payload["agent"], "Ivy")
        self.assertEqual(payload["turn_state"], "PROCESSING")
        self.assertEqual(payload["container_status"], "running")
        self.assertEqual(payload["active_model"], "Gemini 4 Hydrogen (High)")
        self.assertEqual(payload["model_short"], "4 Hydrogen")
        self.assertEqual(payload["session_turns"], 14)
        # Sum of 1 active turn + 1 detached task = 2
        self.assertEqual(payload["in_flight_turns"], 2)
        self.assertEqual(payload["active_turns"], 1)
        self.assertEqual(payload["active_detached_tasks"], 1)
        self.assertEqual(payload["messages_sent"], 820)
        self.assertTrue(payload["daemon_running"])

    @patch("tools.zero_health_server.run_ssh")
    def test_ivy_evaluation_failure_offline_200_and_strict_503(self, mock_ssh):
        zhealth.IVY_CACHE = {"ts": 0.0, "status_code": 200, "payload": None}
        mock_ssh.return_value = (1, "", "Connection refused")

        # Default mode returns 200 with turn_state OFFLINE
        code, payload = zhealth._evaluate_ivy(force=True)
        self.assertEqual(code, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["turn_state"], "OFFLINE")
        self.assertEqual(payload["container_status"], "unreachable")
        self.assertIn("SSH execution failed", payload["error"])

        # Strict mode returns 503
        code_strict, _ = zhealth._evaluate_ivy(force=True, strict=True)
        self.assertEqual(code_strict, 503)

    @patch("tools.zero_health_server.run_ssh")
    def test_ivy_evaluation_caching(self, mock_ssh):
        zhealth.IVY_CACHE = {"ts": 0.0, "status_code": 200, "payload": None}
        mock_output = json.dumps({
            "container_status": "running",
            "session_meta": {"turns": 14},
            "runtime_config": {"model": "gemini-3.7-flash-high"},
            "daemon_running": True
        })
        mock_ssh.return_value = (0, mock_output, "")

        code1, payload1 = zhealth._evaluate_ivy(force=True)
        self.assertEqual(mock_ssh.call_count, 1)

        # Immediate second call should hit cache without calling run_ssh again
        code2, payload2 = zhealth._evaluate_ivy(force=False)
        self.assertEqual(mock_ssh.call_count, 1)
        self.assertEqual(code1, code2)
        self.assertEqual(payload1, payload2)

    def test_public_landing_host_and_html(self):
        self.assertTrue(zhealth.ZeroHealthHandler._is_public_landing_host("brock.ventures"))
        self.assertTrue(zhealth.ZeroHealthHandler._is_public_landing_host("www.brock.ventures:443"))
        self.assertFalse(zhealth.ZeroHealthHandler._is_public_landing_host("status.brock.ventures"))
        html = zhealth.ZeroHealthHandler._render_public_landing_html()
        self.assertIn("upgrade-insecure-requests", html)
        self.assertIn("rel=\"icon\"", html)
        self.assertLess(html.index("Seerr"), html.index("Plex"))
        self.assertLess(html.index("Plex"), html.index("Shelfmark"))
        self.assertLess(html.index("Shelfmark"), html.index("Books Library"))

    def test_check_gotchi_external_auth_and_forward_headers(self):
        (self.temp_path / "gotchi_bearer_token.json").write_text(json.dumps({"token": "secret-gotchi-tok-123"}))
        clean_env = {
            "GOTCHI_BEARER_TOKEN": "",
            "GOTCHI_DEVICE_TOKENS": "",
            "MVG_DEVICE_TOKENS": "",
        }

        with patch.dict(os.environ, clean_env, clear=False):
            # 1. Local/LAN request without CF headers passes
            h1 = self._make_handler("/api/voice/logs", {})
            self.assertFalse(h1._is_external_cf_request())
            self.assertTrue(h1._check_gotchi_external_auth())

            # 2. External request with CF-Connecting-IP and case-insensitive / whitespace-padded Bearer token passes
            h2 = self._make_handler(
                "/api/voice/logs",
                {"Cf-Connecting-Ip": "198.51.100.42", "authorization": "  bEaReR   secret-gotchi-tok-123  "},
            )
            self.assertTrue(h2._is_external_cf_request())
            self.assertTrue(h2._check_gotchi_external_auth())

            # 3. External request with CF-Ray only (no CF-Connecting-IP) is treated as external and requires token
            h_ray = self._make_handler("/api/gotchi/brain", {"cf-ray": "8a1b2c3d4e5f-SJC"})
            self.assertTrue(h_ray._is_external_cf_request())
            self.assertFalse(h_ray._check_gotchi_external_auth())
            h_ray.send_response.assert_called_once_with(401)

            # 4. Previously exempt paths and node_id=gotchi-* MUST fail closed with 401 without valid token
            for protected_path in (
                "/api/gotchi/brain",
                "/api/voice/interact",
                "/api/gotchi/notify?node_id=gotchi-v1",
                "/api/config/llm?node_id=gotchi-1",
                "/gotchi.apk",
                "/api/app/forwarder/apk",
                "/api/app/forwarder/apk.apk",
            ):
                for cf_headers in (
                    {"CF-Connecting-IP": "198.51.100.42"},
                    {"CF-Ray": "8a1b2c3d4e5f-LAX"},
                    {"CF-Connecting-IP": "198.51.100.42", "Authorization": "Bearer wrong-token"},
                ):
                    hx = self._make_handler(protected_path, cf_headers)
                    self.assertFalse(hx._check_gotchi_external_auth(), f"Expected {protected_path} to require auth")
                    hx.send_response.assert_called_once_with(401)

            # 5. _proxy_to_gotchi_gateway blocks unauthorized external requests before connecting to 127.0.0.1:8771
            h_unauth = self._make_handler(
                "/api/gotchi/brain",
                {"CF-Connecting-IP": "198.51.100.42", "Content-Length": "2"},
                body=b"{}",
                command="POST",
            )
            with patch.object(h_unauth, "_ensure_gotchi_gateway_up") as mock_up, \
                 patch("http.client.HTTPConnection") as mock_conn_cls:
                h_unauth.do_POST()
                h_unauth.send_response.assert_called_once_with(401)
                mock_up.assert_not_called()
                mock_conn_cls.assert_not_called()

            # 6. Valid Bearer token passes _proxy_to_gotchi_gateway AND forwards Authorization header to 127.0.0.1:8771
            hp = self._make_handler(
                "/api/gotchi/brain",
                {
                    "Cf-Connecting-Ip": "198.51.100.42",
                    "Authorization": "Bearer secret-gotchi-tok-123",
                    "Content-Type": "application/json",
                    "Content-Length": "4",
                },
                body=b"test",
                command="POST",
            )
            with patch.object(hp, "_ensure_gotchi_gateway_up"), patch("http.client.HTTPConnection") as mock_conn_cls:
                mock_conn = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.getheaders.return_value = [("Content-Type", "application/json")]
                mock_resp.read.return_value = b'{"ok":true}'
                mock_conn.getresponse.return_value = mock_resp
                mock_conn_cls.return_value = mock_conn

                hp.do_POST()

                self.assertTrue(mock_conn.request.called)
                _, kwargs = mock_conn.request.call_args
                forwarded_headers = {k.lower(): v for k, v in kwargs["headers"].items()}
                self.assertEqual(forwarded_headers.get("authorization"), "Bearer secret-gotchi-tok-123")
                hp.send_response.assert_called_once_with(200)

    def test_load_expected_bearer_tokens_sources_and_fail_closed(self):
        clean_env = {
            "GOTCHI_BEARER_TOKEN": "",
            "GOTCHI_DEVICE_TOKENS": "",
            "MVG_DEVICE_TOKENS": "",
        }
        # Fail closed when no tokens exist anywhere
        with patch.dict(os.environ, clean_env, clear=False):
            h_empty = self._make_handler(
                "/api/gotchi/brain",
                {"CF-Connecting-IP": "198.51.100.55", "Authorization": "Bearer any-token"},
            )
            self.assertEqual(h_empty._load_expected_bearer_tokens(), [])
            self.assertFalse(h_empty._check_gotchi_external_auth())
            h_empty.send_response.assert_called_once_with(401)

        # Load tokens from DATA_DIR, fallback file, secrets/env.json, and env vars (JSON dict + node=tok CSV)
        (self.temp_path / "gotchi_bearer_token.json").write_text(json.dumps({"token": "tok-data-dir"}))
        zhealth.FALLBACK_GOTCHI_TOKEN_FILE.write_text(json.dumps({"token": "tok-fallback"}))
        zhealth.SECRETS_ENV_FILE.write_text(json.dumps({
            "GOTCHI_BEARER_TOKEN": "tok-sec-bearer",
            "GOTCHI_DEVICE_TOKENS": {"gotchi-a": "tok-sec-dict-a"},
            "MVG_DEVICE_TOKENS": "gotchi-b=tok-sec-csv-b==,gotchi-c=tok-sec-csv-c",
        }))
        env_tokens = {
            "GOTCHI_BEARER_TOKEN": "tok-env-bearer",
            "GOTCHI_DEVICE_TOKENS": '{"gotchi-d": "tok-env-json-d"}',
            "MVG_DEVICE_TOKENS": "gotchi-e=tok-env-csv-e",
        }
        with patch.dict(os.environ, env_tokens, clear=False):
            h = self._make_handler("/api/search", {"CF-Ray": "8f123456-SJC"})
            loaded = h._load_expected_bearer_tokens()
            for expected in (
                "tok-data-dir",
                "tok-fallback",
                "tok-sec-bearer",
                "tok-sec-dict-a",
                "tok-sec-csv-b==",
                "tok-sec-csv-c",
                "tok-env-bearer",
                "tok-env-json-d",
                "tok-env-csv-e",
            ):
                self.assertIn(expected, loaded)

            h_ok = self._make_handler(
                "/api/search",
                {"CF-Ray": "8f123456-SJC", "Authorization": "Bearer tok-sec-csv-b=="},
            )
            self.assertTrue(h_ok._check_gotchi_external_auth())

    def test_external_cf_auth_enforced_across_get_and_head_routes(self):
        (self.temp_path / "gotchi_bearer_token.json").write_text(json.dumps({"token": "valid-tok-999"}))
        clean_env = {
            "GOTCHI_BEARER_TOKEN": "",
            "GOTCHI_DEVICE_TOKENS": "",
            "MVG_DEVICE_TOKENS": "",
        }
        protected_get_paths = (
            "/api/gotchi/brain",
            "/api/voice/interact",
            "/api/gotchi/notify?node_id=gotchi-v1",
            "/api/config/llm?node_id=gotchi-1",
            "/api/search?q=test",
            "/api/embed?q=test",
            "/api/gif_rank?q=test",
            "/api/telemetry",
            "/api/context",
            "/api/sessions",
            "/api/vulcan",
            "/api/forge",
            "/dashboard",
            "/gotchi",
            "/gotchi.apk",
        )
        with patch.dict(os.environ, clean_env, clear=False):
            for cf_header in (
                {"Host": "zero.brock.ventures", "CF-Connecting-IP": "198.51.100.88"},
                {"Host": "zero.brock.ventures", "CF-Ray": "89abcdef0123-SJC"},
            ):
                for path in protected_get_paths:
                    h = self._make_handler(path, cf_header, command="GET")
                    h.do_GET()
                    h.send_response.assert_called_once_with(401)

                # HEAD /api/search must also return 401 without body
                h_head = self._make_handler("/api/search?q=test", cf_header, command="HEAD")
                h_head.do_HEAD()
                h_head.send_response.assert_called_once_with(401)
                self.assertEqual(h_head.wfile.getvalue(), b"")

            # Valid Bearer token passes GET /api/sessions and HEAD /api/search
            fake_zd = MagicMock()
            fake_zd.get_sessions_list.return_value = [{"id": "s1"}]
            h_sess = self._make_handler(
                "/api/sessions",
                {
                    "Host": "zero.brock.ventures",
                    "CF-Connecting-IP": "198.51.100.88",
                    "Authorization": "Bearer valid-tok-999",
                },
                command="GET",
            )
            with patch("tools.zero_health_server._get_zero_dashboard", return_value=fake_zd):
                h_sess.do_GET()
            h_sess.send_response.assert_called_once_with(200)
            self.assertNotIn("access-control-allow-origin", self._sent_headers(h_sess))

            h_head_ok = self._make_handler(
                "/api/search?q=test",
                {
                    "Host": "zero.brock.ventures",
                    "CF-Ray": "89abcdef0123-SJC",
                    "Authorization": "Bearer valid-tok-999",
                },
                command="HEAD",
            )
            h_head_ok.do_HEAD()
            h_head_ok.send_response.assert_called_once_with(200)

            # Public landing portal stays public on brock.ventures / www.brock.ventures
            h_landing = self._make_handler(
                "/",
                {"Host": "www.brock.ventures", "CF-Connecting-IP": "198.51.100.88"},
                command="GET",
            )
            h_landing.do_GET()
            h_landing.send_response.assert_called_once_with(200)

            h_landing_404 = self._make_handler(
                "/api/search",
                {"Host": "www.brock.ventures", "CF-Connecting-IP": "198.51.100.88"},
                command="GET",
            )
            h_landing_404.do_GET()
            h_landing_404.send_response.assert_called_once_with(404)

    def test_cors_restricted_to_public_liveness_and_avatar_paths(self):
        fake_zd = MagicMock()
        fake_zd.get_telemetry_payload.return_value = {"ok": True}
        fake_zd.get_sessions_list.return_value = []
        fake_zd.get_vulcan_forge_payload.return_value = {"jobs": []}
        fake_zd.render_dashboard_html.return_value = "<html>dashboard</html>"

        # Sensitive endpoints must NOT emit Access-Control-Allow-Origin: * on GET or OPTIONS
        sensitive_paths = (
            "/api/telemetry",
            "/api/context",
            "/api/sessions",
            "/api/vulcan",
            "/api/forge",
            "/dashboard",
        )
        for path in sensitive_paths:
            h_get = self._make_handler(path, {"Host": "localhost:8769"}, command="GET")
            with patch("tools.zero_health_server._get_zero_dashboard", return_value=fake_zd):
                h_get.do_GET()
            h_get.send_response.assert_called_once_with(200)
            self.assertNotIn("access-control-allow-origin", self._sent_headers(h_get), f"Unexpected CORS on GET {path}")

            h_opt = self._make_handler(path, {"Host": "localhost:8769"}, command="OPTIONS")
            h_opt.do_OPTIONS()
            h_opt.send_response.assert_called_once_with(204)
            self.assertNotIn("access-control-allow-origin", self._sent_headers(h_opt), f"Unexpected CORS on OPTIONS {path}")

        for opt_only_path in ("/api/search", "/api/embed", "/api/gif_rank", "/gotchi", "/gotchi.apk"):
            h_opt = self._make_handler(opt_only_path, {"Host": "localhost:8769"}, command="OPTIONS")
            h_opt.do_OPTIONS()
            h_opt.send_response.assert_called_once_with(204)
            self.assertNotIn(
                "access-control-allow-origin",
                self._sent_headers(h_opt),
                f"Unexpected CORS on OPTIONS {opt_only_path}",
            )

        # Public liveness paths DO emit Access-Control-Allow-Origin: *
        for pub_path, host in (
            ("/health", "zero.brock.ventures"),
            ("/api/health", "zero.brock.ventures"),
            ("/status", "zero.brock.ventures"),
            ("/api/status", "zero.brock.ventures"),
            ("/", "probe.brock.ventures"),
            ("/", "status.brock.ventures"),
        ):
            h_get = self._make_handler(pub_path, {"Host": host}, command="GET")
            h_get.do_GET()
            h_get.send_response.assert_called_once_with(200)
            self.assertEqual(self._sent_headers(h_get).get("access-control-allow-origin"), "*")

            h_opt = self._make_handler(pub_path, {"Host": host}, command="OPTIONS")
            h_opt.do_OPTIONS()
            h_opt.send_response.assert_called_once_with(204)
            self.assertEqual(self._sent_headers(h_opt).get("access-control-allow-origin"), "*")

    def test_external_health_redacts_in_flight_prompts(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 5,
            "gateway_status": "connected",
            "gateway_latency_ms": 25.0,
            "state": "PROCESSING",
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))
        in_flight_data = {
            "456": {
                "channel_id": 456,
                "prompt": "Private confidential user prompt",
                "ts": now - 15,
                "pid": os.getpid(),
            }
        }
        (self.temp_path / "in_flight_turn.json").write_text(json.dumps(in_flight_data))

        # Local/LAN request keeps prompt unredacted
        h_local = self._make_handler("/api/health", {"Host": "localhost:8769"}, command="GET")
        h_local.do_GET()
        h_local.send_response.assert_called_once_with(200)
        local_payload = json.loads(h_local.wfile.getvalue().decode("utf-8"))
        self.assertEqual(local_payload["in_flight"][0]["prompt"], "Private confidential user prompt")

        # External CF requests (CF-Connecting-IP and CF-Ray) redact prompt to "[redacted]"
        for cf_headers in (
            {"Host": "zero.brock.ventures", "CF-Connecting-IP": "198.51.100.99"},
            {"Host": "zero.brock.ventures", "CF-Ray": "8a9b0c1d2e3f-SJC"},
            {"Host": "probe.brock.ventures", "CF-Connecting-IP": "198.51.100.99"},
        ):
            req_path = "/" if cf_headers["Host"].startswith("probe.") else "/api/health"
            h_ext = self._make_handler(req_path, cf_headers, command="GET")
            h_ext.do_GET()
            h_ext.send_response.assert_called_once_with(200)
            ext_payload = json.loads(h_ext.wfile.getvalue().decode("utf-8"))
            self.assertEqual(len(ext_payload["in_flight"]), 1)
            self.assertEqual(ext_payload["in_flight"][0]["prompt"], "[redacted]")

    def test_serve_gotchi_forwarder_apk(self):
        dummy_apk = self.temp_path / "dummy.apk"
        dummy_apk.write_bytes(b"PK\x03\x04dummy-apk-payload")

        h = self._make_handler("/gotchi.apk", {"Host": "gotchi.brock.ventures"}, command="GET")

        with patch.object(zhealth.ZeroHealthHandler, "_resolve_gotchi_forwarder_apk", return_value=dummy_apk):
            h.do_GET()

        h.send_response.assert_called_once_with(200)
        sent_headers = {call.args[0]: call.args[1] for call in h.send_header.call_args_list}
        self.assertEqual(sent_headers.get("Content-Type"), "application/vnd.android.package-archive")
        self.assertEqual(sent_headers.get("Content-Disposition"), 'attachment; filename="gotchi-v2.apk"')
        self.assertNotIn("Access-Control-Allow-Origin", sent_headers)
        self.assertEqual(h.wfile.getvalue(), b"PK\x03\x04dummy-apk-payload")

    def test_health_components_proc_fallback_and_pid_self_healing(self):
        now = time.time()
        beacon_data = {
            "gateway_heartbeat": now - 5,
            "gateway_status": "connected",
            "gateway_latency_ms": 20.0,
            "state": "IDLE"
        }
        (self.temp_path / "liveness_beacon.json").write_text(json.dumps(beacon_data))
        # Write stale PIDs that do not exist
        (self.temp_path / "mcp_daemon.pid").write_text("9999991")
        (self.temp_path / "zero_mail_listener.pid").write_text("9999992")

        def fake_discover(patterns, required_substrings=()):
            if patterns == "mcp_daemon.py":
                return 42421
            if "mail_listener" in patterns:
                return 42422
            return None

        handler = zhealth.ZeroHealthHandler.__new__(zhealth.ZeroHealthHandler)
        with patch("tools.zero_health_server._discover_process_pid", side_effect=fake_discover):
            code, payload = handler._evaluate_health()

        self.assertEqual(code, 200)
        self.assertEqual(payload["components"]["mcp_daemon"], "running")
        self.assertEqual(payload["components"]["mail_listener"], "running")
        self.assertEqual((self.temp_path / "mcp_daemon.pid").read_text().strip(), "42421")
        self.assertEqual((self.temp_path / "zero_mail_listener.pid").read_text().strip(), "42422")

    def test_health_server_is_running_and_port_fallback(self):
        orig_pid_file = zhealth.PID_FILE
        try:
            zhealth.PID_FILE = self.temp_path / "zero_health_server.pid"
            zhealth.PID_FILE.write_text("9999993")

            with patch("tools.zero_health_server._discover_process_pid", return_value=51510):
                pid = zhealth.is_running()
                self.assertEqual(pid, 51510)
                self.assertEqual(zhealth.PID_FILE.read_text().strip(), "51510")

            # When PID is not found but port 8769 is alive, ensure_health_server_running does not spawn duplicate
            zhealth.PID_FILE.unlink(missing_ok=True)
            with patch("tools.zero_health_server._discover_process_pid", return_value=None), \
                 patch("tools.zero_health_server._check_health_port_alive", return_value=True), \
                 patch("tools.zero_health_server.start_daemon") as mock_start:
                zhealth.ensure_health_server_running()
                mock_start.assert_not_called()
                status = zhealth.get_status()
                self.assertTrue(status["running"])
        finally:
            zhealth.PID_FILE = orig_pid_file

    def test_mcp_daemon_pid_self_healing_and_healthy_port_fallback(self):
        from tools import mcp_daemon

        orig_pid_file = mcp_daemon.PID_FILE
        orig_lock_file = mcp_daemon.LOCK_FILE
        try:
            mcp_daemon.PID_FILE = self.temp_path / "mcp_daemon.pid"
            mcp_daemon.LOCK_FILE = self.temp_path / "mcp_daemon.lock"
            mcp_daemon.PID_FILE.write_text("9999994")

            # 1. Reconciles stale PID_FILE via /proc discovery
            with patch("tools.daemon_singleton.find_live_daemon_pids", return_value=[61610]):
                pid = mcp_daemon.get_daemon_pid()
                self.assertEqual(pid, 61610)
                self.assertEqual(mcp_daemon.PID_FILE.read_text().strip(), "61610")

            # 2. If PID_FILE is missing and /proc returns empty, but all ports return 200 OK, get_status treats as healthy
            mcp_daemon.PID_FILE.unlink(missing_ok=True)
            with patch("tools.daemon_singleton.find_live_daemon_pids", return_value=[]), \
                 patch("tools.mcp_daemon.check_port_health", return_value=(True, "HTTP 200 OK")):
                st = mcp_daemon.get_status()
                self.assertTrue(st["running"])
                self.assertTrue(st["healthy"])
        finally:
            mcp_daemon.PID_FILE = orig_pid_file
            mcp_daemon.LOCK_FILE = orig_lock_file

    def test_non_loopback_non_cf_requests_require_auth(self):
        (self.temp_path / "gotchi_bearer_token.json").write_text(json.dumps({"token": "admin-tok-001"}))
        env_tokens = {
            "ZERO_HEALTH_ADMIN_TOKEN": "zero-admin-tok-002",
            "ZERO_ADMIN_TOKEN": "",
            "GOTCHI_BEARER_TOKEN": "",
            "GOTCHI_DEVICE_TOKENS": '{"gotchi-mike-01": "collab-tok-mike"}',
            "MVG_DEVICE_TOKENS": "gotchi-coley-01=collab-tok-coley",
        }
        remote_addr = ("198.51.100.25", 54321)

        with patch.dict(os.environ, env_tokens, clear=False):
            # 1. Non-loopback request WITHOUT CF headers fails closed with 401 on Gotchi and admin routes
            for path in ("/api/gotchi/brain", "/api/voice/logs", "/api/search?q=test", "/api/forge", "/dashboard"):
                h_unauth = self._make_handler(
                    path,
                    {"Host": "198.51.100.10:8769"},
                    command="GET",
                    client_address=remote_addr,
                )
                self.assertFalse(h_unauth._is_external_cf_request())
                self.assertFalse(h_unauth._is_loopback_client())
                h_unauth.do_GET()
                h_unauth.send_response.assert_called_once_with(401)

            # 2. Non-loopback POST to Gotchi proxy without CF headers and without token fails closed with 401
            h_post = self._make_handler(
                "/api/gotchi/brain",
                {"Host": "198.51.100.10:8769", "Content-Length": "2"},
                body=b"{}",
                command="POST",
                client_address=remote_addr,
            )
            with patch.object(h_post, "_ensure_gotchi_gateway_up") as mock_up, \
                 patch("http.client.HTTPConnection") as mock_conn_cls:
                h_post.do_POST()
                h_post.send_response.assert_called_once_with(401)
                mock_up.assert_not_called()
                mock_conn_cls.assert_not_called()

            # 3. Collaborator device token passes Gotchi auth on non-loopback non-CF request
            h_collab_gotchi = self._make_handler(
                "/api/voice/logs",
                {"Host": "198.51.100.10:8769", "Authorization": "Bearer collab-tok-mike"},
                client_address=remote_addr,
            )
            self.assertTrue(h_collab_gotchi._check_gotchi_external_auth())

            # 4. Loopback (127.0.0.1 and ::1) without CF headers passes admin & Gotchi auth
            for loop_addr in (("127.0.0.1", 40001), ("::1", 40002)):
                h_loop = self._make_handler("/api/forge", {"Host": "localhost:8769"}, client_address=loop_addr)
                self.assertTrue(h_loop._check_admin_auth())
                self.assertTrue(h_loop._check_gotchi_external_auth())

            # 5. Public brock.ventures landing page remains public even from non-loopback
            h_pub = self._make_handler(
                "/",
                {"Host": "www.brock.ventures"},
                command="GET",
                client_address=remote_addr,
            )
            h_pub.do_GET()
            h_pub.send_response.assert_called_once_with(200)

    def test_admin_token_scope_rejects_collaborator_tokens_with_403(self):
        env_tokens = {
            "ZERO_HEALTH_ADMIN_TOKEN": "super-admin-tok",
            "ZERO_ADMIN_TOKEN": "alt-admin-tok",
            "GOTCHI_BEARER_TOKEN": "owner-gotchi-tok",
            "GOTCHI_DEVICE_TOKENS": '{"gotchi-mike-01": "collab-mike-tok"}',
            "MVG_DEVICE_TOKENS": "gotchi-alex-01=collab-alex-tok",
        }
        admin_routes = (
            "/api/search?q=ryan",
            "/api/embed?q=ryan",
            "/api/gif_rank?q=laugh",
            "/api/telemetry",
            "/api/context",
            "/api/sessions",
            "/api/forge",
            "/api/vulcan",
            "/dashboard",
        )
        remote_addr = ("203.0.113.42", 44444)

        with patch.dict(os.environ, env_tokens, clear=False):
            h_probe = self._make_handler("/api/search", {}, client_address=remote_addr)
            admin_tokens = h_probe._load_admin_bearer_tokens()
            device_tokens = h_probe._load_gotchi_device_tokens()
            expected_tokens = h_probe._load_expected_bearer_tokens()

            self.assertIn("super-admin-tok", admin_tokens)
            self.assertIn("alt-admin-tok", admin_tokens)
            self.assertIn("owner-gotchi-tok", admin_tokens)
            self.assertNotIn("collab-mike-tok", admin_tokens)
            self.assertNotIn("collab-alex-tok", admin_tokens)
            self.assertIn("collab-mike-tok", device_tokens)
            self.assertIn("collab-alex-tok", device_tokens)
            self.assertIn("collab-mike-tok", expected_tokens)
            self.assertIn("super-admin-tok", expected_tokens)

            # Collaborator token -> 403 Forbidden on ALL admin/private routes (both CF and non-loopback)
            for headers_base in (
                {"Host": "zero.brock.ventures", "CF-Connecting-IP": "198.51.100.77"},
                {"Host": "198.51.100.10:8769"},
            ):
                for collab_tok in ("collab-mike-tok", "collab-alex-tok"):
                    hdrs = dict(headers_base)
                    hdrs["Authorization"] = f"Bearer {collab_tok}"
                    for route in admin_routes:
                        h_get = self._make_handler(route, hdrs, command="GET", client_address=remote_addr)
                        h_get.do_GET()
                        h_get.send_response.assert_called_once_with(403)
                        body = json.loads(h_get.wfile.getvalue().decode("utf-8"))
                        self.assertEqual(body.get("error"), "forbidden")

                    h_head = self._make_handler("/api/forge", hdrs, command="HEAD", client_address=remote_addr)
                    h_head.do_HEAD()
                    h_head.send_response.assert_called_once_with(403)
                    self.assertEqual(h_head.wfile.getvalue(), b"")

                # Missing or bogus token -> 401 Unauthorized on admin routes
                for bad_hdrs in (
                    dict(headers_base),
                    {**headers_base, "Authorization": "Bearer totally-wrong"},
                ):
                    h_401 = self._make_handler("/api/search?q=test", bad_hdrs, command="GET", client_address=remote_addr)
                    h_401.do_GET()
                    h_401.send_response.assert_called_once_with(401)

            # Admin token passes GET /api/forge and passes include_private=True
            fake_zd = MagicMock()
            fake_zd.get_vulcan_forge_payload.return_value = {"decisions": [], "builds": []}
            h_ok = self._make_handler(
                "/api/forge",
                {
                    "Host": "zero.brock.ventures",
                    "CF-Connecting-IP": "198.51.100.77",
                    "Authorization": "Bearer super-admin-tok",
                },
                command="GET",
                client_address=remote_addr,
            )
            with patch("tools.zero_health_server._get_zero_dashboard", return_value=fake_zd):
                h_ok.do_GET()
            h_ok.send_response.assert_called_once_with(200)
            fake_zd.get_vulcan_forge_payload.assert_called_once_with(limit=30, include_private=True)

    def test_vulcan_forge_payload_filters_private_decisions_by_default(self):
        import sqlite3

        from tools import zero_dashboard as zdash

        forge_db = self.temp_path / "forge.db"
        now = time.time()
        conn = sqlite3.connect(str(forge_db))
        conn.execute("""
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
        conn.execute(
            "INSERT INTO forge_decisions VALUES ('dec-pub', 'forge_enqueue', 'Public Card', 'Public summary', 'forge', 123, NULL, 0, 'pending', ?, ?, NULL, '')",
            (now + 3600, now - 10),
        )
        conn.execute(
            "INSERT INTO forge_decisions VALUES ('dec-priv', 'email_dispatch', 'Private Family Card', 'Confidential details', 'email', 456, NULL, 1, 'pending', ?, ?, NULL, '')",
            (now + 3600, now),
        )
        conn.commit()
        conn.close()

        # Default (include_private=False): hides private Decision Cards and excludes them from pending count
        pub_payload = zdash.get_vulcan_forge_payload(
            limit=10,
            forge_db_path=forge_db,
            telemetry_db_path=self.temp_path / "missing_telemetry.db",
        )
        self.assertEqual(pub_payload["pending_decisions_count"], 1)
        self.assertEqual([d["id"] for d in pub_payload["decisions"]], ["dec-pub"])

        # Admin-verified (include_private=True): returns both public and private Decision Cards
        admin_payload = zdash.get_vulcan_forge_payload(
            limit=10,
            forge_db_path=forge_db,
            telemetry_db_path=self.temp_path / "missing_telemetry.db",
            include_private=True,
        )
        self.assertEqual(admin_payload["pending_decisions_count"], 2)
        self.assertEqual({d["id"] for d in admin_payload["decisions"]}, {"dec-pub", "dec-priv"})


if __name__ == "__main__":
    unittest.main()
