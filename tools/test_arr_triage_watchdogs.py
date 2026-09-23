#!/usr/bin/env python3
"""Comprehensive test suite for Autonomous Arr & Prowlarr Health Triage Watchdogs."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

import pytest
import tools.arr_queue_watchdog as aqw
import tools.prowlarr_watchdog as pw


class MockResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def decode(self):
        return self._data.decode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    """Fixture to provide isolated state files and audit logs."""
    state_arr = tmp_path / "arr_queue_state.json"
    state_prowl = tmp_path / "prowlarr_state.json"
    audit_file = tmp_path / "arr_triage_audit.jsonl"

    monkeypatch.setattr(aqw, "STATE_FILE", state_arr)
    monkeypatch.setattr(aqw, "AUDIT_LOG_FILE", audit_file)
    monkeypatch.setattr(pw, "STATE_FILE", state_prowl)
    monkeypatch.setattr(pw, "AUDIT_LOG_FILE", audit_file)

    return tmp_path, audit_file


# --------------------------------------------------------------------------
# Arr Queue Watchdog Tests
# --------------------------------------------------------------------------

def test_arr_allowed_hosts_auto_remediation(temp_env, monkeypatch):
    """Tier 1: AllowedHostsCheck is silently auto-remediated and not alerted to user."""
    _, audit_file = temp_env

    monkeypatch.setattr(aqw, "_get_api_keys", lambda: ("sonarr_mock_key", "radarr_mock_key"))
    monkeypatch.setattr(aqw, "fetch_queue", lambda app, port, key: [])

    # Health returns AllowedHostsCheck
    health_mock = [
        {
            "source": "AllowedHostsCheck",
            "type": "warning",
            "message": "Allowed Hosts is not configured"
        }
    ]
    monkeypatch.setattr(aqw, "fetch_health", lambda app, port, key: health_mock)

    # Mock auto-remediation call
    auto_remed_mock = MagicMock(return_value=True)
    monkeypatch.setattr(aqw, "_auto_remediate_allowed_hosts", auto_remed_mock)

    has_activity, summary, items = aqw.run_watchdog(auto_fix=True, force_dispatch=True)

    assert auto_remed_mock.call_count == 2  # Once for Radarr, once for Sonarr
    assert "AllowedHostsCheck" in summary
    assert "Auto-applied LAN host whitelist" in summary
    assert "🚨 **Arr Health Outage Alert" not in summary  # No false outage alert!


def test_arr_ignored_sources_suppression(temp_env, monkeypatch):
    """Tier 2: Benign and cosmetic health checks are completely suppressed."""
    monkeypatch.setattr(aqw, "_get_api_keys", lambda: ("sonarr_mock_key", "radarr_mock_key"))
    monkeypatch.setattr(aqw, "fetch_queue", lambda app, port, key: [])

    health_mock = [
        {"source": "UpdateCheck", "type": "warning", "message": "New update is available"},
        {"source": "BranchCheck", "type": "warning", "message": "Branch is outdated"},
        {"source": "PackageMaintainerMessage", "type": "warning", "message": "Docker maintainer note"},
        {"source": "TaskCanceledException", "type": "warning", "message": "System.Threading.Tasks.TaskCanceledException"}
    ]
    monkeypatch.setattr(aqw, "fetch_health", lambda app, port, key: health_mock)

    has_activity, summary, items = aqw.run_watchdog(auto_fix=True, force_dispatch=True)

    assert not has_activity
    assert summary == ""


def test_arr_critical_vs_non_critical_debounce(temp_env, monkeypatch):
    """Tier 3: Critical health checks alert after 15m debounce, non-critical wait 120m."""
    monkeypatch.setattr(aqw, "_get_api_keys", lambda: ("sonarr_mock_key", "radarr_mock_key"))
    monkeypatch.setattr(aqw, "fetch_queue", lambda app, port, key: [])

    # First run at T=0: Download client down (critical) + Unclassified warning (non-critical)
    health_mock = [
        {"source": "DownloadClientCheck", "type": "error", "message": "Unable to communicate with SABnzbd"},
        {"source": "CustomPluginCheck", "type": "warning", "message": "Minor plugin notice"}
    ]
    monkeypatch.setattr(aqw, "fetch_health", lambda app, port, key: health_mock)

    t0 = 1000000.0
    monkeypatch.setattr(aqw.time, "time", lambda: t0)
    has_act1, sum1, _ = aqw.run_watchdog(auto_fix=True, force_dispatch=False)
    assert not has_act1  # Debouncing, 0 elapsed

    # Run at T=0 + 20 minutes (1200s): Critical check (15m limit) should alert, non-critical (120m limit) should NOT
    monkeypatch.setattr(aqw.time, "time", lambda: t0 + 1200)
    has_act2, sum2, _ = aqw.run_watchdog(auto_fix=True, force_dispatch=False)
    assert has_act2
    assert "DownloadClientCheck" in sum2
    assert "CRITICAL" in sum2
    assert "CustomPluginCheck" not in sum2

    # Run at T=0 + 130 minutes (7800s): Non-critical check should now alert
    monkeypatch.setattr(aqw.time, "time", lambda: t0 + 7800)
    has_act3, sum3, _ = aqw.run_watchdog(auto_fix=True, force_dispatch=False)
    assert has_act3
    assert "CustomPluginCheck" in sum3


# --------------------------------------------------------------------------
# Prowlarr Watchdog Tests
# --------------------------------------------------------------------------

def test_prowlarr_single_indexer_backoff_suppression(temp_env, monkeypatch):
    """Tier 2: Single indexer throttle with 4 healthy active indexers is suppressed as nominal jitter."""
    _, audit_file = temp_env

    monkeypatch.setattr(pw, "_get_api_key", lambda: "mock_prowlarr_key")

    # 4 enabled indexers
    indexers_mock = [
        {"id": 1, "name": "NZBGeek", "enable": True},
        {"id": 2, "name": "NinjaCentral", "enable": True},
        {"id": 3, "name": "DrunkenSlug", "enable": True},
        {"id": 4, "name": "AltHUB", "enable": True},
    ]

    # Only 1 indexer is in temporary backoff
    status_mock = [
        {"indexerId": 1, "disabledTill": "2026-09-18T02:30:00Z", "failureCount": 6}
    ]

    def mock_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/indexerstatus" in url:
            return MockResponse(json.dumps(status_mock).encode())
        elif "/indexer" in url:
            return MockResponse(json.dumps(indexers_mock).encode())
        elif "/health" in url:
            return MockResponse(json.dumps([]).encode())
        return MockResponse(b"[]")

    monkeypatch.setattr(pw.urllib.request, "urlopen", mock_urlopen)

    has_act, summary, issues = pw.check_prowlarr(force=True)

    # Should be nominal because 3 out of 4 indexers are healthy
    assert not has_act
    assert "nominal" in summary
    assert len(issues) == 0

    # Verify audit receipt was written
    assert audit_file.exists()
    with open(audit_file) as f:
        log_content = f.read()
        assert "suppressed_transient_single_backoff" in log_content
        assert "NZBGeek" in log_content


def test_prowlarr_systemic_indexer_outage_escalates(temp_env, monkeypatch):
    """Tier 3: Multiple indexers throttled or permanently disabled escalates immediately."""
    monkeypatch.setattr(pw, "_get_api_key", lambda: "mock_prowlarr_key")

    indexers_mock = [
        {"id": 1, "name": "NZBGeek", "enable": True},
        {"id": 2, "name": "NinjaCentral", "enable": True},
        {"id": 3, "name": "DrunkenSlug", "enable": False},  # Permanently disabled!
    ]

    status_mock = [
        {"indexerId": 1, "disabledTill": "2026-09-18T02:30:00Z", "failureCount": 8},
        {"indexerId": 2, "disabledTill": "2026-09-18T02:30:00Z", "failureCount": 10},
    ]

    def mock_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/indexerstatus" in url:
            return MockResponse(json.dumps(status_mock).encode())
        elif "/indexer" in url:
            return MockResponse(json.dumps(indexers_mock).encode())
        elif "/health" in url:
            return MockResponse(json.dumps([]).encode())
        return MockResponse(b"[]")

    monkeypatch.setattr(pw.urllib.request, "urlopen", mock_urlopen)

    has_act, summary, issues = pw.check_prowlarr(force=True)

    assert has_act
    assert "🚨 **Prowlarr Critical Indexer Outage Alert**" in summary
    assert "DrunkenSlug" in summary
    assert "NZBGeek" in summary
    assert "NinjaCentral" in summary


def test_prowlarr_allowed_hosts_auto_remediation(temp_env, monkeypatch):
    """Tier 1: Prowlarr AllowedHostsCheck triggers silent auto-remediation."""
    monkeypatch.setattr(pw, "_get_api_key", lambda: "mock_prowlarr_key")

    indexers_mock = [{"id": 1, "name": "NZBGeek", "enable": True}]
    status_mock = []
    health_mock = [{"source": "AllowedHostsCheck", "type": "warning", "message": "Allowed Hosts not configured"}]

    def mock_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/indexerstatus" in url:
            return MockResponse(json.dumps(status_mock).encode())
        elif "/indexer" in url:
            return MockResponse(json.dumps(indexers_mock).encode())
        elif "/health" in url:
            return MockResponse(json.dumps(health_mock).encode())
        return MockResponse(b"[]")

    monkeypatch.setattr(pw.urllib.request, "urlopen", mock_urlopen)
    auto_remed_mock = MagicMock(return_value=True)
    monkeypatch.setattr(pw, "_auto_remediate_allowed_hosts", auto_remed_mock)

    has_act, summary, issues = pw.check_prowlarr(force=True)

    assert auto_remed_mock.called
    assert not has_act
    assert "nominal" in summary


def test_arr_non_upgrade_auto_purged_and_blocklisted(temp_env, monkeypatch):
    """Tier 1: Non-upgrade releases rejected by Custom Formats are auto-purged from download client and blocklisted."""
    _, audit_file = temp_env

    monkeypatch.setattr(aqw, "_get_api_keys", lambda: ("sonarr_mock_key", "radarr_mock_key"))
    monkeypatch.setattr(aqw, "fetch_health", lambda app, port, key: [])
    monkeypatch.setattr(aqw, "_auto_remediate_media_management", lambda app, port, key: (False, ""))

    queue_mock = [
        {
            "id": 1495902780,
            "downloadId": "mock-download-123",
            "title": "Ted.Lasso.S04E08.PROPER.1080p.WEB.h264-ETHEL",
            "status": "completed",
            "trackedDownloadStatus": "warning",
            "statusMessages": [
                {
                    "title": "Ted.Lasso.S04E08.PROPER.1080p.WEB.h264-ETHEL",
                    "messages": [
                        "Not a Custom Format upgrade for existing episode file(s). New: [Language ENG, Repack/Proper] (5) do not improve on Existing: [ATVP, Language ENG, WEB Tier 01] (1800)"
                    ]
                }
            ],
            "series": {"path": "/data/media/tv/Ted Lasso"}
        }
    ]
    monkeypatch.setattr(aqw, "fetch_queue", lambda app, port, key: queue_mock if app == "Sonarr" else [])

    remove_mock = MagicMock(return_value=True)
    monkeypatch.setattr(aqw, "remove_queue_item", remove_mock)

    has_activity, summary, items = aqw.run_watchdog(auto_fix=True, force_dispatch=True)

    assert remove_mock.called
    assert remove_mock.call_args[0][2] == 1495902780
    assert remove_mock.call_args[1]["remove_from_client"] is True
    assert remove_mock.call_args[1]["blocklist"] is True
    assert "⚠️ **Arr Queue Warnings Detected**" not in summary
    assert "🛠️ **Arr Import Auto-Remediation Live**" in summary
    assert "inferior non-upgrade" in summary

    # Verify audit receipt was written
    assert audit_file.exists()
    with open(audit_file) as f:
        log_content = f.read()
        assert "queue_non_upgrade" in log_content
        assert "auto_purged_and_blocklisted" in log_content


def test_arr_proper_repack_config_auto_remediated(temp_env, monkeypatch):
    """Tier 1: Media management downloadPropersAndRepacks is auto-remediated to doNotPrefer."""
    _, audit_file = temp_env

    mock_cfg = {"id": 1, "downloadPropersAndRepacks": "preferAndUpgrade"}

    def mock_urlopen(req, timeout=8):
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        if method == "GET":
            return MockResponse(json.dumps(mock_cfg).encode())
        elif method == "PUT":
            data = json.loads(req.data.decode("utf-8"))
            mock_cfg.update(data)
            return MockResponse(json.dumps(mock_cfg).encode())
        return MockResponse(b"{}")

    monkeypatch.setattr(aqw.urllib.request, "urlopen", mock_urlopen)

    changed, old_val = aqw._auto_remediate_media_management("Sonarr", 8989, "mock_key")
    assert changed is True
    assert old_val == "preferAndUpgrade"
    assert mock_cfg["downloadPropersAndRepacks"] == "doNotPrefer"

    # Running a second time should detect it is already doNotPrefer
    changed_again, _ = aqw._auto_remediate_media_management("Sonarr", 8989, "mock_key")
    assert changed_again is False

