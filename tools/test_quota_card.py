#!/usr/bin/env python3
"""Unit tests for quota_card.py, workspace_mcp.py retry/IPv4 logic, and google_tasks_sync.py network resilience."""

from __future__ import annotations

import http.client
import inspect
import io
import json
import socket
import ssl
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_WORKSPACE = Path(__file__).resolve().parent.parent
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))
if str(_WORKSPACE / "tools") not in sys.path:
    sys.path.insert(0, str(_WORKSPACE / "tools"))

from tools.google_tasks_sync import _api_request
from tools.quota_card import (
    build_quota_card,
    compute_cycle_metrics,
    fetch_usage_data,
    format_progress_bar,
    parse_gemini_weekly_bucket,
)
from tools.quota_poller import fetch_quota_buckets
from tools.workspace_mcp import _ipv4_preferred_getaddrinfo, urlopen_with_retry


class TestQuotaCardFetchUsageData(unittest.TestCase):
    """Verify fetch_usage_data timeout defaults, JSON parsing, and retry on TimeoutExpired."""

    def test_fetch_usage_data_default_timeout_is_30(self):
        sig = inspect.signature(fetch_usage_data)
        self.assertEqual(sig.parameters["timeout"].default, 30)
        self.assertEqual(sig.parameters["retries"].default, 1)

    @patch("tools.quota_card.subprocess.run")
    def test_fetch_usage_data_success_first_attempt(self, mock_run):
        mock_output = json.dumps({"command": {"data": {"groups": []}}, "response": "ok"})
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        data = fetch_usage_data()
        self.assertEqual(data["response"], "ok")
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.kwargs.get("timeout"), 30)

    @patch("tools.quota_card.subprocess.run")
    def test_fetch_usage_data_retries_on_timeout_expired(self, mock_run):
        mock_output = json.dumps({"status": "success", "groups": []})
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd=["agy", "-p", "/usage"], timeout=30),
            MagicMock(returncode=0, stdout=mock_output, stderr=""),
        ]

        data = fetch_usage_data(timeout=30, retries=1)
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(mock_run.call_count, 2)

    @patch("tools.quota_card.subprocess.run")
    def test_fetch_usage_data_raises_when_all_attempts_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["agy", "-p", "/usage"], timeout=30)
        with self.assertRaises(subprocess.TimeoutExpired):
            fetch_usage_data(timeout=30, retries=1)
        self.assertEqual(mock_run.call_count, 2)

    @patch("tools.quota_card.subprocess.run")
    def test_fetch_usage_data_empty_output_raises_runtime_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        with self.assertRaises(RuntimeError):
            fetch_usage_data(timeout=30, retries=0)


class TestQuotaCardBuildQuotaCard(unittest.TestCase):
    """Verify build_quota_card with live CLI output and clean fallback to telemetry.db snapshots."""

    def test_progress_bar_and_cycle_metrics_helpers(self):
        bar, pct = format_progress_bar(0.5, width=10)
        self.assertEqual(pct, 50)
        self.assertEqual(bar, "[█████░░░░░]")

        now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        metrics = compute_cycle_metrics(0.5, "2026-10-25T12:00:00Z", now_utc=now)
        self.assertEqual(metrics["countdown"], "2d 0h")

        raw_sample = {
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-weekly",
                                    "remaining_fraction": 0.9,
                                    "reset_time": "2026-10-25T00:00:00Z",
                                }
                            ],
                        }
                    ]
                }
            }
        }
        b = parse_gemini_weekly_bucket(raw_sample)
        self.assertEqual(b["id"], "gemini-weekly")

    def test_build_quota_card_with_provided_data(self):
        sample_data = {
            "command": {
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-weekly",
                                    "remaining_fraction": 0.82,
                                    "reset_time": "2026-10-25T20:00:00Z",
                                }
                            ],
                        }
                    ]
                }
            }
        }
        fixed_now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        ok, card, meta = build_quota_card(now_utc=fixed_now, data=sample_data)
        self.assertTrue(ok)
        self.assertIn("Gemini Weekly Quota", card)
        self.assertIn("82% remaining", card)
        self.assertEqual(meta["remaining_pct"], 82)

    @patch("tools.quota_card.fetch_usage_data")
    @patch("tools.quota_poller.get_latest_quota")
    def test_build_quota_card_falls_back_to_get_latest_quota_on_cli_timeout(
        self, mock_get_latest, mock_fetch
    ):
        mock_fetch.side_effect = subprocess.TimeoutExpired(cmd=["agy", "-p", "/usage"], timeout=30)
        now_ts = time.time()
        mock_get_latest.return_value = {
            "gemini-weekly": {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.65,
                "reset_time": "2026-10-25T18:00:00Z",
                "timestamp": now_ts - 300,  # 5 minutes ago (<6h)
            }
        }

        fixed_now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        ok, card, meta = build_quota_card(now_utc=fixed_now)
        self.assertTrue(ok)
        self.assertIn("Gemini Weekly Quota", card)
        self.assertIn("65% remaining", card)
        self.assertEqual(meta["remaining_pct"], 65)
        mock_get_latest.assert_called_once()

    @patch("tools.quota_card.fetch_usage_data")
    @patch("tools.quota_poller.get_latest_quota")
    def test_build_quota_card_rejects_stale_fallback_older_than_6h(
        self, mock_get_latest, mock_fetch
    ):
        mock_fetch.side_effect = subprocess.TimeoutExpired(cmd=["agy", "-p", "/usage"], timeout=30)
        now_ts = time.time()
        mock_get_latest.return_value = {
            "gemini-weekly": {
                "bucket_id": "gemini-weekly",
                "remaining_fraction": 0.65,
                "reset_time": "2026-10-25T18:00:00Z",
                "timestamp": now_ts - (7 * 3600),  # 7 hours ago (>6h)
            }
        }

        fixed_now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        ok, card, meta = build_quota_card(now_utc=fixed_now)
        self.assertFalse(ok)
        self.assertIn("Failed to retrieve Gemini weekly quota", card)
        self.assertIn("error", meta)

    @patch("tools.quota_card.fetch_usage_data")
    @patch("tools.quota_poller.get_latest_quota")
    def test_build_quota_card_fails_when_both_cli_and_fallback_fail(
        self, mock_get_latest, mock_fetch
    ):
        mock_fetch.side_effect = RuntimeError("agy process exited with error")
        mock_get_latest.return_value = {}

        ok, card, meta = build_quota_card()
        self.assertFalse(ok)
        self.assertIn("Failed to retrieve Gemini weekly quota", card)
        self.assertIn("error", meta)


class TestQuotaPollerDefaults(unittest.TestCase):
    """Verify quota_poller.fetch_quota_buckets default timeout."""

    def test_fetch_quota_buckets_default_timeout_is_30(self):
        sig = inspect.signature(fetch_quota_buckets)
        self.assertEqual(sig.parameters["timeout"].default, 30)


class TestWorkspaceMcpUrlopenWithRetry(unittest.TestCase):
    """Verify urlopen_with_retry recovers from RemoteDisconnected, SSLError, and transient HTTP codes."""

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_urlopen_with_retry_retries_on_remote_disconnected(self, mock_urlopen, _mock_sleep):
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"access_token": "mock-token-xyz"}'
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None

        mock_urlopen.side_effect = [
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            fake_resp,
        ]

        resp = urlopen_with_retry("https://oauth2.googleapis.com/token", retries=2, backoff=0.01)
        self.assertEqual(resp, fake_resp)
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_urlopen_with_retry_retries_on_ssl_error(self, mock_urlopen, _mock_sleep):
        fake_resp = MagicMock()
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None

        mock_urlopen.side_effect = [
            ssl.SSLError("decryption failed or bad record mac"),
            fake_resp,
        ]

        resp = urlopen_with_retry("https://gmail.googleapis.com/gmail/v1/users/me/labels", retries=2, backoff=0.01)
        self.assertEqual(resp, fake_resp)
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_urlopen_with_retry_retries_on_http_503(self, mock_urlopen, _mock_sleep):
        fake_resp = MagicMock()
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None

        http_503 = urllib.error.HTTPError(
            url="https://gmail.googleapis.com",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=io.BytesIO(b"Unavailable"),
        )
        mock_urlopen.side_effect = [http_503, fake_resp]

        resp = urlopen_with_retry("https://gmail.googleapis.com", retries=2, backoff=0.01)
        self.assertEqual(resp, fake_resp)
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("urllib.request.urlopen")
    def test_urlopen_with_retry_does_not_retry_on_http_404(self, mock_urlopen):
        http_404 = urllib.error.HTTPError(
            url="https://gmail.googleapis.com",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(b"Not Found"),
        )
        mock_urlopen.side_effect = http_404

        with self.assertRaises(urllib.error.HTTPError):
            urlopen_with_retry("https://gmail.googleapis.com", retries=2)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_urlopen_with_retry_raises_when_retries_exhausted(self, mock_urlopen, _mock_sleep):
        mock_urlopen.side_effect = http.client.RemoteDisconnected("Remote end closed connection")

        with self.assertRaises(http.client.RemoteDisconnected):
            urlopen_with_retry("https://oauth2.googleapis.com/token", retries=2, backoff=0.01)
        self.assertEqual(mock_urlopen.call_count, 3)


class TestGoogleTasksSyncApiRequest(unittest.TestCase):
    """Verify _api_request retries on RemoteDisconnected, SSLError, and defaults to retries=3."""

    def test_api_request_default_retries_is_3(self):
        sig = inspect.signature(_api_request)
        self.assertEqual(sig.parameters["retries"].default, 3)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_api_request_retries_on_remote_disconnected(self, mock_urlopen, _mock_sleep):
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"items": []}'
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None

        mock_urlopen.side_effect = [
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            fake_resp,
        ]

        req = urllib.request.Request("https://tasks.googleapis.com/tasks/v1/users/@me/lists")
        data = _api_request(req, timeout=10.0, retries=2, backoff=0.01)
        self.assertEqual(data, b'{"items": []}')
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("time.sleep", return_value=None)
    @patch("urllib.request.urlopen")
    def test_api_request_retries_on_ssl_error(self, mock_urlopen, _mock_sleep):
        fake_resp = MagicMock()
        fake_resp.read.return_value = b'{"id": "gt-123"}'
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None

        mock_urlopen.side_effect = [
            ssl.SSLError("TLS connection unexpectedly terminated"),
            fake_resp,
        ]

        req = urllib.request.Request("https://tasks.googleapis.com/tasks/v1/lists/1/tasks")
        data = _api_request(req, timeout=10.0, retries=2, backoff=0.01)
        self.assertEqual(data, b'{"id": "gt-123"}')
        self.assertEqual(mock_urlopen.call_count, 2)


class TestIPv4PreferenceGetAddrInfo(unittest.TestCase):
    """Verify socket.getaddrinfo wrapper attempts AF_INET when family == 0 / AF_UNSPEC."""

    def test_ipv4_preference_on_unspec_family(self):
        with patch("tools.workspace_mcp._orig_getaddrinfo") as mock_orig:
            mock_orig.side_effect = [
                [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.1", 443))],
            ]

            res = _ipv4_preferred_getaddrinfo("oauth2.googleapis.com", 443, family=0)
            self.assertEqual(res[0][0], socket.AF_INET)
            # Verify AF_INET was attempted
            mock_orig.assert_called_once_with("oauth2.googleapis.com", 443, socket.AF_INET, 0, 0, 0)

    def test_ipv4_preference_fallback_when_af_inet_fails(self):
        with patch("tools.workspace_mcp._orig_getaddrinfo") as mock_orig:
            mock_orig.side_effect = [
                socket.gaierror(-2, "Name or service not known"),
                [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))],
            ]

            res = _ipv4_preferred_getaddrinfo("example.internal", 443, family=0)
            self.assertEqual(res[0][0], socket.AF_INET6)
            self.assertEqual(mock_orig.call_count, 2)


if __name__ == "__main__":
    unittest.main()
