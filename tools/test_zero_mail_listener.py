#!/usr/bin/env python3
"""
Unit test suite for tools/zero_mail_listener.py.
Validates robust secret fallback resolution chain and rate-limited error logging.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REMO_ROOT = Path(__file__).resolve().parent.parent
if str(REMO_ROOT) not in sys.path:
    sys.path.insert(0, str(REMO_ROOT))
WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.append(str(WORKSPACE))

import tools.work_cal_sync as wcs
import tools.zero_mail_listener as zmail


class TestZeroMailListener(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        zmail._last_throttled_errors.clear()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_credentials_valid_primary(self):
        secret_file = self.temp_path / "google_oauth.json"
        valid_data = {
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "refresh_token": "test-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token"
        }
        secret_file.write_text(json.dumps(valid_data))

        with patch.dict(os.environ, {"GOOGLE_OAUTH_PATH": str(secret_file)}):
            creds, path = zmail.resolve_credentials()
            self.assertIsNotNone(creds)
            self.assertEqual(path, str(secret_file))
            self.assertEqual(creds["client_id"], "test-client-id")

    def test_resolve_credentials_corrupt_fallback_chain(self):
        # Primary is corrupt JSON
        primary_file = self.temp_path / "corrupt_oauth.json"
        primary_file.write_text("{broken json syntax: true,")

        # Fallback is valid
        fallback_file = self.temp_path / "valid_fallback_oauth.json"
        valid_data = {
            "client_id": "fallback-id",
            "client_secret": "fallback-secret",
            "refresh_token": "fallback-token"
        }
        fallback_file.write_text(json.dumps(valid_data))

        with patch("tools.zero_mail_listener.os.path.exists") as mock_exists, \
             patch("tools.zero_mail_listener.os.path.getsize") as mock_size, \
             patch("builtins.open", unittest.mock.mock_open()):

            def custom_exists(p):
                return p in ("/secrets/google_oauth.json", "/workspace/config/google_oauth.json")

            def custom_size(p):
                return 100

            mock_exists.side_effect = custom_exists
            mock_size.side_effect = custom_size

            # When opening /secrets/google_oauth.json return corrupt, when opening config return valid
            def custom_open_impl(file_path, *args, **kwargs):
                if file_path == "/secrets/google_oauth.json":
                    return unittest.mock.mock_open(read_data="{corrupt: json").return_value
                elif file_path == "/workspace/config/google_oauth.json":
                    return unittest.mock.mock_open(read_data=json.dumps(valid_data)).return_value
                return unittest.mock.mock_open().return_value

            with patch("builtins.open", side_effect=custom_open_impl):
                creds, path = zmail.resolve_credentials()
                self.assertIsNotNone(creds)
                self.assertEqual(path, "/workspace/config/google_oauth.json")
                self.assertEqual(creds["client_id"], "fallback-id")

    def test_resolve_credentials_missing_required_fields(self):
        incomplete_file = self.temp_path / "incomplete.json"
        incomplete_file.write_text(json.dumps({"client_id": "only-id"}))

        with patch.dict(os.environ, {"GOOGLE_OAUTH_PATH": str(incomplete_file)}), \
             patch("tools.zero_mail_listener.os.path.exists", side_effect=lambda p: p == str(incomplete_file)):
            creds, path = zmail.resolve_credentials()
            self.assertIsNone(creds)
            self.assertIsNone(path)

    def test_log_error_throttled_deduplication(self):
        logged = []
        with patch("tools.zero_mail_listener.log", side_effect=logged.append):
            # First log passes through
            zmail.log_error_throttled("test_key", "Fatal read error", interval=60)
            self.assertEqual(len(logged), 1)
            self.assertIn("Fatal read error", logged[0])

            # Immediate repeat is suppressed
            zmail.log_error_throttled("test_key", "Fatal read error", interval=60)
            self.assertEqual(len(logged), 1)

            # Different key passes through
            zmail.log_error_throttled("other_key", "Different error", interval=60)
            self.assertEqual(len(logged), 2)

    def test_is_silent_system_email_subject_and_snippet(self):
        self.assertTrue(
            zmail.is_silent_system_email(
                {"subject": "[WORK-CAL-SYNC] Rolling 14-Day Window"}, ""
            )
        )
        self.assertTrue(
            zmail.is_silent_system_email(
                {"subject": "Re: [work-cal-sync] Rolling 14-Day Window"}, ""
            )
        )
        self.assertTrue(
            zmail.is_silent_system_email(
                {"subject": "Calendar Update"},
                '{"schema": "zero.work_cal_sync.v1", "events": []}',
            )
        )
        self.assertFalse(
            zmail.is_silent_system_email(
                {"subject": "Hello Zero"}, "Normal human email body"
            )
        )

    def test_process_message_payload_ignores_work_cal_sync_subject(self):
        m_data = {
            "payload": {
                "headers": [
                    {"name": "From", "value": "user@example.com"},
                    {"name": "To", "value": zmail.TARGET_EMAIL},
                    {"name": "Subject", "value": "[WORK-CAL-SYNC] Rolling 14-Day Window"},
                ]
            },
            "snippet": '{"generated_at": "2026-04-12T19:00:08Z"}',
        }
        with patch("tools.zero_mail_listener.post_discord") as mock_post:
            res = zmail.process_message_payload(
                m_data, bot_token="fake-token", channel_id="123456"
            )
            self.assertEqual(res, "ignored_work_cal_sync")
            mock_post.assert_not_called()

    def test_process_message_payload_ignores_work_cal_sync_snippet(self):
        m_data = {
            "payload": {
                "headers": [
                    {"name": "From", "value": "user@example.com"},
                    {"name": "To", "value": zmail.TARGET_EMAIL},
                    {"name": "Subject", "value": "Daily Digest"},
                ]
            },
            "snippet": '{"schema": "zero.work_cal_sync.v1", "window_days": 14}',
        }
        with patch("tools.zero_mail_listener.post_discord") as mock_post, \
             patch("tools.zero_mail_listener.ingest_work_cal_message") as mock_ingest:
            res = zmail.process_message_payload(
                m_data, bot_token="fake-token", channel_id="123456"
            )
            self.assertEqual(res, "ignored_work_cal_sync")
            mock_post.assert_not_called()
            mock_ingest.assert_called_once_with(m_data)

    def test_extract_and_ingest_work_cal_message_base64_plain(self):
        import base64

        raw_payload = {
            "schema": "zero.work_cal_sync.v1",
            "generated_at": "2026-11-03T10:51:33-08:00",
            "timezone": "America/Los_Angeles",
            "window_start": "2026-11-03T00:00:00-08:00",
            "window_end": "2026-11-09T23:59:59-08:00",
            "events": [
                {
                    "uid_hash": "aa01cfd50d77c851",
                    "summary": "Work Meeting",
                    "start": "2026-11-03T20:30:00-08:00",
                    "end": "2026-11-03T21:30:00-08:00",
                },
                {
                    "uid_hash": "5a376ff081497f74",
                    "summary": "Work Meeting",
                    "start": "2026-11-04T17:05:00-08:00",
                    "end": "2026-11-04T17:45:00-08:00",
                },
            ],
        }
        encoded = base64.urlsafe_b64encode(json.dumps(raw_payload).encode("utf-8")).decode("ascii")
        m_data = {
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "[WORK-CAL-SYNC] Rolling 14-Day Window"}
                ],
                "body": {"data": encoded},
            },
            "snippet": "",
        }
        state_file = self.temp_path / "work_calendar_sync.json"
        extracted = wcs.extract_work_cal_json_from_message(m_data)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["schema"], "zero.work_cal_sync.v1")

        ok = wcs.ingest_work_cal_message(m_data, state_path=state_file)
        self.assertTrue(ok)
        self.assertTrue(state_file.exists())

        events = wcs.get_cached_work_events(
            time_min_iso="2026-11-03T00:00:00-08:00",
            time_max_iso="2026-11-04T00:00:00-08:00",
            state_path=state_file,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "work_aa01cfd50d77c851")
        self.assertEqual(events[0]["calendar"], "Work (Ryan)")
        self.assertEqual(events[0]["summary"], "Work Meeting")
        self.assertEqual(events[0]["description"], "Synced via [WORK-CAL-SYNC]")

    def test_extract_work_cal_json_from_html_and_snippet(self):
        import base64

        html_body = '<div><p>Header</p><pre>{&quot;schema&quot;: &quot;zero.work_cal_sync.v1&quot;, &quot;events&quot;: [{&quot;uid_hash&quot;: &quot;h1&quot;, &quot;summary&quot;: &quot;Sync&quot;, &quot;start&quot;: &quot;2026-11-03T09:00:00-08:00&quot;, &quot;end&quot;: &quot;2026-11-03T10:00:00-08:00&quot;}]}</pre></div>'
        encoded = base64.urlsafe_b64encode(html_body.encode("utf-8")).decode("ascii")
        m_data = {
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/html", "body": {"data": encoded}},
                ],
            }
        }
        extracted = wcs.extract_work_cal_json_from_message(m_data)
        self.assertIsNotNone(extracted)
        self.assertEqual(len(extracted["events"]), 1)
        self.assertEqual(extracted["events"][0]["uid_hash"], "h1")

    def test_polling_loop_triggers_reconcile_when_work_cal_ingest_succeeds(self):
        msg_metadata = {
            "id": "msg_sync_101",
            "threadId": "thread_sync_101",
            "snippet": "Sync snippet",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "[WORK-CAL-SYNC] Latest schedule"},
                    {"name": "From", "value": "calendar-bot@example.com"},
                ]
            },
        }

        list_response = {"messages": [{"id": "msg_sync_101"}]}

        def mock_urlopen_impl(req, timeout=15):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            if "messages?q=" in url:
                resp.read.return_value = json.dumps(list_response).encode("utf-8")
            elif "messages/msg_sync_101" in url:
                resp.read.return_value = json.dumps(msg_metadata).encode("utf-8")
            else:
                resp.read.return_value = b"{}"
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        mock_ingest = MagicMock(return_value=True)
        mock_reconcile = MagicMock()

        def mock_archive(mid, tok=None, thread_id=None):
            zmail._running = False

        mock_arch = MagicMock(side_effect=mock_archive)

        zmail._running = True
        with patch("tools.zero_mail_listener.get_access_token", return_value="tok-xyz"), \
             patch("tools.zero_mail_listener.load_seen", return_value={"seed_id"}), \
             patch("tools.zero_mail_listener.save_seen"), \
             patch("tools.zero_mail_listener.load_env", return_value={"DISCORD_BOT_TOKEN": "bot-tok"}), \
             patch("tools.zero_mail_listener.ingest_work_cal_message", mock_ingest), \
             patch("tools.zero_mail_listener.reconcile_work_cal_to_google_calendar", mock_reconcile), \
             patch("tools.zero_mail_listener.archive_work_cal_message", mock_arch), \
             patch("tools.zero_mail_listener.urllib.request.urlopen", side_effect=mock_urlopen_impl):
            zmail.run_loop()

        mock_ingest.assert_called_once()
        mock_reconcile.assert_called_once_with(tok="tok-xyz")
        mock_arch.assert_called_once_with("msg_sync_101", tok="tok-xyz", thread_id="thread_sync_101")

    def test_polling_loop_handles_reconcile_exception_gracefully(self):
        msg_metadata = {
            "id": "msg_sync_102",
            "threadId": "thread_sync_102",
            "snippet": "Sync snippet",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "[WORK-CAL-SYNC] Latest schedule"},
                    {"name": "From", "value": "calendar-bot@example.com"},
                ]
            },
        }

        list_response = {"messages": [{"id": "msg_sync_102"}]}

        def mock_urlopen_impl(req, timeout=15):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            if "messages?q=" in url:
                resp.read.return_value = json.dumps(list_response).encode("utf-8")
            elif "messages/msg_sync_102" in url:
                resp.read.return_value = json.dumps(msg_metadata).encode("utf-8")
            else:
                resp.read.return_value = b"{}"
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        mock_ingest = MagicMock(return_value=True)
        mock_reconcile = MagicMock(side_effect=RuntimeError("Google Calendar API 503"))

        def mock_archive(mid, tok=None, thread_id=None):
            zmail._running = False

        mock_arch = MagicMock(side_effect=mock_archive)

        zmail._running = True
        with patch("tools.zero_mail_listener.get_access_token", return_value="tok-xyz"), \
             patch("tools.zero_mail_listener.load_seen", return_value={"seed_id"}), \
             patch("tools.zero_mail_listener.save_seen"), \
             patch("tools.zero_mail_listener.load_env", return_value={"DISCORD_BOT_TOKEN": "bot-tok"}), \
             patch("tools.zero_mail_listener.ingest_work_cal_message", mock_ingest), \
             patch("tools.zero_mail_listener.reconcile_work_cal_to_google_calendar", mock_reconcile), \
             patch("tools.zero_mail_listener.archive_work_cal_message", mock_arch), \
             patch("tools.zero_mail_listener.urllib.request.urlopen", side_effect=mock_urlopen_impl):
            zmail.run_loop()

        mock_reconcile.assert_called_once_with(tok="tok-xyz")
        mock_arch.assert_called_once_with("msg_sync_102", tok="tok-xyz", thread_id="thread_sync_102")

    def test_polling_loop_skips_reconcile_when_work_cal_ingest_fails(self):
        msg_metadata = {
            "id": "msg_sync_103",
            "threadId": "thread_sync_103",
            "snippet": "Sync snippet",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "[WORK-CAL-SYNC] Bad payload"},
                    {"name": "From", "value": "calendar-bot@example.com"},
                ]
            },
        }

        list_response = {"messages": [{"id": "msg_sync_103"}]}

        def mock_urlopen_impl(req, timeout=15):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = MagicMock()
            if "messages?q=" in url:
                resp.read.return_value = json.dumps(list_response).encode("utf-8")
            elif "messages/msg_sync_103" in url:
                resp.read.return_value = json.dumps(msg_metadata).encode("utf-8")
            else:
                resp.read.return_value = b"{}"
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        mock_ingest = MagicMock(return_value=False)
        mock_reconcile = MagicMock()

        def mock_archive(mid, tok=None, thread_id=None):
            zmail._running = False

        mock_arch = MagicMock(side_effect=mock_archive)

        zmail._running = True
        with patch("tools.zero_mail_listener.get_access_token", return_value="tok-xyz"), \
             patch("tools.zero_mail_listener.load_seen", return_value={"seed_id"}), \
             patch("tools.zero_mail_listener.save_seen"), \
             patch("tools.zero_mail_listener.load_env", return_value={"DISCORD_BOT_TOKEN": "bot-tok"}), \
             patch("tools.zero_mail_listener.ingest_work_cal_message", mock_ingest), \
             patch("tools.zero_mail_listener.reconcile_work_cal_to_google_calendar", mock_reconcile), \
             patch("tools.zero_mail_listener.archive_work_cal_message", mock_arch), \
             patch("tools.zero_mail_listener.urllib.request.urlopen", side_effect=mock_urlopen_impl):
            zmail.run_loop()

        mock_ingest.assert_called()
        mock_reconcile.assert_not_called()
        mock_arch.assert_called_once_with("msg_sync_103", tok="tok-xyz", thread_id="thread_sync_103")


if __name__ == "__main__":
    unittest.main()
