#!/usr/bin/env python3
"""
Unit tests for Host 1 go2rtc resource and zombie stream watchdog.
Uses RFC 5737 documentation IP addresses (198.51.100.82) and generic hostnames.
"""

import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_TOOLS_DIR = str(Path(__file__).resolve().parent)
for _p in ["/workspace", "/workspace/tools", _TOOLS_DIR, _REPO_ROOT]:
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

import tools.go2rtc_watchdog as gwd
from tools import sidecars

MOCK_HOST_1_IP = "198.51.100.82"


def test_parse_mem_mib_units():
    """Verify parse_mem_mib accurately handles various memory units and formats."""
    assert gwd.parse_mem_mib("12.59MiB") == 12.59
    assert gwd.parse_mem_mib("12.59MiB / 768MiB") == 12.59
    assert gwd.parse_mem_mib("100MB") == 100.0
    assert gwd.parse_mem_mib("1.5 GiB") == 1536.0
    assert gwd.parse_mem_mib("512 KiB") == 0.5
    assert gwd.parse_mem_mib("1048576 B") == 1.0
    assert gwd.parse_mem_mib("0B") == 0.0
    assert gwd.parse_mem_mib("") == 0.0
    assert gwd.parse_mem_mib("invalid") == 0.0


def test_parse_mem_stat():
    """Verify parse_mem_stat extracts both used and limit MiB."""
    used, lim = gwd.parse_mem_stat("180.5MiB / 768MiB")
    assert used == 180.5
    assert lim == 768.0

    used_single, lim_single = gwd.parse_mem_stat("100MiB")
    assert used_single == 100.0
    assert lim_single == 0.0


def test_parse_cpu_perc():
    """Verify parse_cpu_perc handles percentages cleanly."""
    assert gwd.parse_cpu_perc("2.59%") == 2.59
    assert gwd.parse_cpu_perc(" 14.0% ") == 14.0
    assert gwd.parse_cpu_perc("0%") == 0.0
    assert gwd.parse_cpu_perc("invalid") == 0.0


def test_sanitize_stream_url_redacts_credentials():
    """Verify sanitize_stream_url masks tokens, client IDs, and secrets."""
    raw_url = (
        "nest:?client_id=secret_client_12345&client_secret=secret_gocspx_99999"
        "&refresh_token=secret_refresh_token_abcde&project_id=test-project&device_id=AVPHwE123"
    )
    sanitized = gwd.sanitize_stream_url(raw_url)
    assert "secret_client_12345" not in sanitized
    assert "secret_gocspx_99999" not in sanitized
    assert "secret_refresh_token_abcde" not in sanitized
    assert "test-project" in sanitized
    assert "[REDACTED]" in sanitized


def test_nominal_low_resources_and_no_streams():
    """Verify nominal low CPU/RAM and 0 streams returns has_issues=False and empty report when unforced."""
    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 2.5,
        "mem_used_mib": 15.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.0195,
        "streams_summary": {
            "total_streams": 9,
            "active_streams": 0,
            "active_nest_streams": 0,
            "zombie_streams": [],
            "streams": {},
        },
    }

    has_issues, summary, data = gwd.evaluate_go2rtc_health(
        telemetry,
        auto_reap=True,
        cpu_threshold=25.0,
        mem_threshold_mib=350.0,
        host_ip=MOCK_HOST_1_IP,
        force=False,
    )

    assert has_issues is False
    assert "nominal" in summary
    assert data["has_issues"] is False

    # Sidecars wrapper should return silent empty message when nominal and unforced
    with patch.object(gwd, "collect_go2rtc_telemetry", return_value=telemetry):
        ok, msg, sidecar_data = sidecars.run_go2rtc_watchdog(force=False, test=False)
        assert ok is True
        assert msg == ""
        assert sidecar_data["has_issues"] is False

    # When forced, sidecars wrapper returns the formatted summary
    with patch.object(gwd, "collect_go2rtc_telemetry", return_value=telemetry):
        ok, msg, sidecar_data = sidecars.run_go2rtc_watchdog(force=True, test=False)
        assert ok is True
        assert "Host 1 go2rtc Health Status (Forced Run)" in msg


def test_high_sustained_cpu_triggers_alert():
    """Verify CPU >= 25% sustained triggers formatted alert without backticks around numbers."""
    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 28.5,
        "mem_used_mib": 50.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.065,
        "streams_summary": {
            "total_streams": 2,
            "active_streams": 1,
            "active_nest_streams": 1,
            "zombie_streams": [],
            "streams": {},
        },
    }

    has_issues, summary, data = gwd.evaluate_go2rtc_health(
        telemetry,
        auto_reap=True,
        cpu_threshold=25.0,
        mem_threshold_mib=350.0,
        host_ip=MOCK_HOST_1_IP,
    )

    assert has_issues is True
    assert "Host 1 go2rtc Health Alert" in summary
    assert "- CPU Usage: 28.5% (threshold: 25.0%)" in summary
    # Plain markdown bullets, no backticks around percentages/numbers
    assert "`28.5%`" not in summary
    assert any("Sustained CPU usage 28.5%" in r for r in data["reasons"])


def test_high_memory_triggers_alert():
    """Verify memory >= 350 MiB (or >= 50% container limit) triggers alert."""
    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 5.0,
        "mem_used_mib": 410.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.5338,
        "streams_summary": {
            "total_streams": 1,
            "active_streams": 0,
            "active_nest_streams": 0,
            "zombie_streams": [],
            "streams": {},
        },
    }

    has_issues, summary, data = gwd.evaluate_go2rtc_health(
        telemetry,
        auto_reap=True,
        cpu_threshold=25.0,
        mem_threshold_mib=350.0,
        host_ip=MOCK_HOST_1_IP,
    )

    assert has_issues is True
    assert "- Memory Usage: 410.0 MiB / 768.0 MiB" in summary
    assert "`410.0 MiB`" not in summary
    assert any("Memory usage 410.0 MiB exceeds ceiling 350.0 MiB" in r for r in data["reasons"])


def test_zombie_keyframe_stream_auto_reap_and_token_redaction():
    """Verify leaked keyframe zombie consumers are safely reaped and tokens are never leaked."""
    secret_token = "ultra_confidential_oauth_refresh_token_98765"
    raw_producer_url = f"nest:?client_id=client_xyz&refresh_token={secret_token}&device_id=AVPHwE999"

    streams_payload = {
        "deck_camera": {
            "producers": [{"url": raw_producer_url}],
            "consumers": [
                {
                    "format_name": "keyframe",
                    "user_agent": "Python/3.11 aiohttp/3.8.5",
                    "bytes_send": 512,
                }
            ],
        }
    }

    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 4.0,
        "mem_used_mib": 50.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.065,
        "streams_summary": gwd.parse_go2rtc_streams(streams_payload),
    }

    with patch.object(gwd, "run_ssh", return_value=(0, "ok", "")) as mock_ssh:
        has_issues, summary, data = gwd.evaluate_go2rtc_health(
            telemetry,
            auto_reap=True,
            host_ip=MOCK_HOST_1_IP,
        )

    assert has_issues is True
    assert "deck_camera" in data["reaped_streams"]
    assert "- Reaped Zombie Streams: deck_camera" in summary
    # Mandatory zero-knowledge token safety check
    assert secret_token not in summary
    assert "client_xyz" not in summary

    # Verify SSH curl reap command ran DELETE then PUT with URL encoding
    mock_ssh.assert_called_once()
    reap_cmd = mock_ssh.call_args[0][1]
    assert "DELETE 'http://127.0.0.1:1984/api/streams?src=deck_camera'" in reap_cmd
    assert "PUT 'http://127.0.0.1:1984/api/streams?name=deck_camera&src=" in reap_cmd


def test_active_webrtc_live_viewer_is_never_reaped():
    """Verify streams with an active WebRTC/MSE live viewer are never reaped."""
    streams_payload = {
        "front_door": {
            "producers": [{"url": "nest:?device_id=AVPHwE111"}],
            "consumers": [
                {
                    "format_name": "webrtc",
                    "user_agent": "Mozilla/5.0 Chrome/120.0.0.0",
                    "bytes_send": 54000,
                },
                {
                    "format_name": "keyframe",
                    "user_agent": "Python/3.11",
                    "bytes_send": 256,
                },
            ],
        }
    }

    summary = gwd.parse_go2rtc_streams(streams_payload)
    assert len(summary["zombie_streams"]) == 0
    assert summary["active_streams"] == 1

    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 5.0,
        "mem_used_mib": 60.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.078,
        "streams_summary": summary,
    }

    with patch.object(gwd, "run_ssh") as mock_ssh:
        has_issues, rep, data = gwd.evaluate_go2rtc_health(telemetry, auto_reap=True, host_ip=MOCK_HOST_1_IP)

    assert has_issues is False
    assert len(data["reaped_streams"]) == 0
    assert "nominal" in rep
    mock_ssh.assert_not_called()


def test_stalled_zero_byte_snapshot_consumer_detected_as_zombie():
    """Verify stalled bytes_send==0 Python snapshot clients are detected as zombies and reaped."""
    streams_payload = {
        "backyard_cam": {
            "producers": [{"url": "nest:?device_id=AVPHwE222"}],
            "consumers": [
                {
                    "format_name": "",
                    "user_agent": "Python-urllib/3.11",
                    "bytes_send": 0,
                }
            ],
        }
    }

    summary = gwd.parse_go2rtc_streams(streams_payload)
    assert len(summary["zombie_streams"]) == 1
    assert summary["zombie_streams"][0]["name"] == "backyard_cam"
    assert summary["zombie_streams"][0]["can_reap"] is True

    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 3.0,
        "mem_used_mib": 40.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.052,
        "streams_summary": summary,
    }

    with patch.object(gwd, "run_ssh", return_value=(0, "ok", "")) as mock_ssh:
        has_issues, rep, data = gwd.evaluate_go2rtc_health(telemetry, auto_reap=True, host_ip=MOCK_HOST_1_IP)

    assert has_issues is True
    assert "backyard_cam" in data["reaped_streams"]
    assert "backyard_cam" in rep
    mock_ssh.assert_called_once()


def test_missing_producer_url_warns_unreapable():
    """Verify zombie streams missing a producer URL cannot be reaped and emit an unreapable warning."""
    streams_payload = {
        "mystery_stream": {
            "producers": [],
            "consumers": [{"format_name": "keyframe", "bytes_send": 100}],
        }
    }

    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 2.0,
        "mem_used_mib": 30.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.039,
        "streams_summary": gwd.parse_go2rtc_streams(streams_payload),
    }

    with patch.object(gwd, "run_ssh") as mock_ssh:
        has_issues, rep, data = gwd.evaluate_go2rtc_health(telemetry, auto_reap=True, host_ip=MOCK_HOST_1_IP)

    assert has_issues is True
    assert "mystery_stream" in data["unreapable_streams"]
    assert "- Unreapable Zombie Streams: mystery_stream (missing producer URL)" in rep
    mock_ssh.assert_not_called()


def test_hung_or_empty_go2rtc_api_response_triggers_alert():
    """Verify hung or empty go2rtc API JSON triggers health alert."""
    streams_summary = gwd.parse_go2rtc_streams("INVALID_NON_JSON")
    assert streams_summary.get("error") is not None

    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 1.0,
        "mem_used_mib": 20.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.026,
        "streams_summary": streams_summary,
    }

    has_issues, rep, data = gwd.evaluate_go2rtc_health(telemetry, auto_reap=True, host_ip=MOCK_HOST_1_IP)
    assert has_issues is True
    assert "go2rtc API unreachable/hung" in rep
    assert data["has_issues"] is True


def test_container_not_running_triggers_alert():
    """Verify stopped/exited container triggers alert."""
    telemetry = {
        "status": "container_issue",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "exited",
        "restart_count": 5,
        "avg_cpu": 0.0,
        "mem_used_mib": 0.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.0,
        "streams_summary": gwd.parse_go2rtc_streams({}),
    }

    has_issues, rep, data = gwd.evaluate_go2rtc_health(telemetry, host_ip=MOCK_HOST_1_IP)
    assert has_issues is True
    assert "🚨 Host 1 go2rtc Health Alert" in rep
    assert "- Container Status: exited (5 restarts)" in rep
    assert data["has_issues"] is True


def test_ssh_transport_failure_triggers_alert():
    """Verify SSH connection failure triggers transport alert."""
    telemetry = {
        "status": "error",
        "host_ip": MOCK_HOST_1_IP,
        "error": "SSH connection failed (code 255): Connection timed out",
    }

    has_issues, rep, data = gwd.evaluate_go2rtc_health(telemetry, host_ip=MOCK_HOST_1_IP)
    assert has_issues is True
    assert "Host 1 go2rtc Transport Alert" in rep
    assert "Connection timed out" in rep
    assert data["has_issues"] is True


def test_max_active_nest_streams_triggers_alert():
    """Verify >= 3 simultaneous active Nest camera streams triggers alert."""
    streams_payload = {
        "nest_camera_1": {
            "producers": [{"url": "nest:?device_id=1"}],
            "consumers": [{"format_name": "webrtc", "bytes_send": 1000}],
        },
        "nest_camera_2": {
            "producers": [{"url": "nest:?device_id=2"}],
            "consumers": [{"format_name": "webrtc", "bytes_send": 1000}],
        },
        "nest_camera_3": {
            "producers": [{"url": "nest:?device_id=3"}],
            "consumers": [{"format_name": "webrtc", "bytes_send": 1000}],
        },
    }

    summary = gwd.parse_go2rtc_streams(streams_payload)
    assert summary["active_nest_streams"] == 3

    telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 10.0,
        "mem_used_mib": 100.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.13,
        "streams_summary": summary,
    }

    has_issues, rep, data = gwd.evaluate_go2rtc_health(telemetry, max_nest_streams=3, host_ip=MOCK_HOST_1_IP)
    assert has_issues is True
    assert "- Active Nest Streams: 3 (threshold: 3)" in rep
    assert data["has_issues"] is True


def test_collect_go2rtc_telemetry_parses_bundled_output():
    """Verify collect_go2rtc_telemetry correctly unpacks the bundled SSH command output."""
    raw_ssh_stdout = (
        "running 2\n"
        "12.4% 150.0MiB / 768MiB\n"
        "14.6% 152.0MiB / 768MiB\n"
        "---JSON---\n"
        '{"test_stream": {"producers": [{"url": "nest:?device_id=abc"}], "consumers": null}}'
    )

    with patch.object(gwd, "run_ssh", return_value=(0, raw_ssh_stdout, "")):
        telemetry = gwd.collect_go2rtc_telemetry(MOCK_HOST_1_IP)

    assert telemetry["status"] == "nominal"
    assert telemetry["container_status"] == "running"
    assert telemetry["restart_count"] == 2
    assert telemetry["avg_cpu"] == 13.5
    assert telemetry["mem_used_mib"] == 152.0
    assert telemetry["mem_limit_mib"] == 768.0
    assert telemetry["streams_summary"]["total_streams"] == 1


def test_sidecars_run_go2rtc_watchdog_failure_case():
    """Verify sidecars.run_go2rtc_watchdog returns (False, report, data) when an issue is detected."""
    failing_telemetry = {
        "status": "nominal",
        "host_ip": MOCK_HOST_1_IP,
        "error": None,
        "container_status": "running",
        "restart_count": 0,
        "avg_cpu": 35.0,  # Exceeds 25.0%
        "mem_used_mib": 50.0,
        "mem_limit_mib": 768.0,
        "mem_ratio": 0.065,
        "streams_summary": {"total_streams": 0, "active_streams": 0, "active_nest_streams": 0, "zombie_streams": []},
    }

    with patch.object(gwd, "collect_go2rtc_telemetry", return_value=failing_telemetry):
        ok, msg, data = sidecars.run_go2rtc_watchdog(force=False, test=False)
        assert ok is False
        assert "Host 1 go2rtc Health Alert" in msg
        assert "- CPU Usage: 35.0%" in msg
        assert data["has_issues"] is True
