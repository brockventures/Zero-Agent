"""Unit tests for zero_dashboard.py."""

from __future__ import annotations

import json
from pathlib import Path
from tools.zero_dashboard import (
    PERSISTENT_CHANNELS,
    format_channel_display_name,
    get_sessions_list,
    get_telemetry_payload,
    render_dashboard_html,
    build_chart_points,
    parse_session_transcript,
    stitch_session_timelines,
    get_active_model_config,
)


def test_persistent_channels_alignment():
    """Verify PERSISTENT_CHANNELS strictly matches the 3 dedicated warm daemons."""
    assert PERSISTENT_CHANNELS == {"zero-chat", "the-banana-stand", "lounge"}
    assert "zero-ops" not in PERSISTENT_CHANNELS
    assert "homelab" not in PERSISTENT_CHANNELS
    assert "vault" not in PERSISTENT_CHANNELS


def test_format_channel_display_name():
    """Test display name formatting for persistent vs dynamic channels."""
    assert format_channel_display_name("zero-chat", True) == "[P] #zero-chat"
    assert format_channel_display_name("zero-ops", False) == "#zero-ops"
    assert format_channel_display_name("#homelab", False) == "#homelab"


def test_build_chart_points_preserves_compactions():
    """Test that build_chart_points preserves compaction inflection points during downsampling."""
    timeline = [
        {
            "step": i,
            "tokens_est": 10000 + i * 500,
            "category": "tool",
            "tool": "run_command",
            "time_short": "12:00 PM",
            "timestamp": 1000.0 + i * 10,
            "is_compaction": False,
        }
        for i in range(100)
    ]
    # Inject a compaction at step 50
    timeline[50] = {
        "step": 50,
        "tokens_est": 11000,
        "category": "compaction",
        "tool": "compaction",
        "time_short": "12:25 PM",
        "timestamp": 1500.0,
        "is_compaction": True,
        "tokens_before": 35000,
        "recovered_tokens": 24000,
    }

    points = build_chart_points(timeline, max_points=25)
    assert len(points) <= 30
    compaction_points = [p for p in points if p.get("is_compaction")]
    assert len(compaction_points) == 1
    assert compaction_points[0]["step"] == 50
    assert compaction_points[0]["recovered"] == 24000


def test_render_dashboard_html_contains_channel_centric_elements():
    """Verify HTML contains channel selector, side-by-side comparison, and stitched compacted elements."""
    html = render_dashboard_html()
    assert "xAxisLine" in html
    assert "chart-compaction-badge" in html
    assert "channel-select" in html
    assert "⚡ Persistent Daemon Channels" in html
    assert "⚙️ Dynamic Active Channels" in html
    assert "📦 Recent Compacted History" not in html
    assert "side-by-side-comparison" in html
    assert "active-session-card" in html
    assert "compacted-session-card" in html
    assert "tab-trajectory-full" in html
    assert "Compacted History (Unified)" in html


def test_telemetry_payload_structure_and_stitched_timeline():
    """Verify get_telemetry_payload structure organizes data by channel with stitched compacted timeline."""
    payload = get_telemetry_payload(target="zero-ops")
    assert payload["status"] == "healthy"
    assert "channel" in payload
    assert payload["channel"]["name"] == "zero-ops"
    assert "channels" in payload
    assert len(payload["channels"]) > 0

    assert "active_session" in payload
    if payload["active_session"]:
        act = payload["active_session"]
        assert "current_tokens" in act
        assert "fill_pct" in act
        assert "cache_hit_rate" in act
        assert "timeline" in act

    # Check stitched compacted session
    assert "compacted_summary" in payload
    assert payload["compacted_summary"]["count"] > 0
    assert "stitched_compacted_session" in payload
    stitched = payload["stitched_compacted_session"]
    assert stitched is not None
    assert "timeline" in stitched
    assert len(stitched["timeline"]) > 0
    assert "chart_points" in stitched
    assert len(stitched["chart_points"]) > 0

    # Rollover/compaction points should be in the stitched timeline
    rollovers = [p for p in stitched["chart_points"] if p.get("is_rollover") or p.get("is_compaction")]
    assert len(rollovers) > 0

    # Full channel lifecycle
    assert "full_channel_session" in payload
    assert payload["full_channel_session"] is not None

    # Backward compatibility
    assert "selected_session" in payload
    assert "channel_rollups" in payload
    assert "daemon_rollups" in payload
