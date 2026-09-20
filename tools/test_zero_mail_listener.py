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
from unittest.mock import patch

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

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
             patch("builtins.open", unittest.mock.mock_open()) as mock_open:

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

        with patch.dict(os.environ, {"GOOGLE_OAUTH_PATH": str(incomplete_file)}):
            with patch("tools.zero_mail_listener.os.path.exists", side_effect=lambda p: p == str(incomplete_file)):
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


if __name__ == "__main__":
    unittest.main()
