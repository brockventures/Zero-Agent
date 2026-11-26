"""Unit and integration tests for tools/hls_stream_relay.py and tools/sports_stream_extractor.py."""

from __future__ import annotations

import gzip
import io
import json
import sys
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import hls_stream_relay as relay
from tools import sports_stream_extractor as extractor

# ---------------------------------------------------------------------------
# Pure HLS Sanitization Unit Tests
# ---------------------------------------------------------------------------


def test_is_bogus_ssai_segment_detects_tracking_and_preserves_media():
    # Exact Akamai / Anvato SSAI tracking pattern
    assert relay.is_bogus_ssai_segment(
        "https://cbs-anvato.akamaized.net/targeting/analytics?hdntl=exp=1700000000~acl=*~hmac=deadbeef"
    )
    assert relay.is_bogus_ssai_segment("/targeting/analytics?hdntl=exp=1700000000~acl=*~hmac=abc")
    assert relay.is_bogus_ssai_segment("https://ads.example.com/beacon?id=42")
    assert relay.is_bogus_ssai_segment("https://ads.example.com/pixel.gif?cb=123")
    assert relay.is_bogus_ssai_segment("https://ads.example.com/tracking/event")
    assert relay.is_bogus_ssai_segment("https://ads.example.com/impression/v1")

    # Valid media segments must always be preserved (including signed query strings)
    assert not relay.is_bogus_ssai_segment("https://cdn.example.com/live/seg_001.ts?hdntl=exp=1700000~hmac=123")
    assert not relay.is_bogus_ssai_segment("chunk_002.m4s?token=xyz")
    assert not relay.is_bogus_ssai_segment("init_video.mp4")
    assert not relay.is_bogus_ssai_segment("audio_003.aac?hdntl=abc")
    assert not relay.is_bogus_ssai_segment("video_004.cmfv?sig=1")
    assert not relay.is_bogus_ssai_segment("audio_005.cmfa?sig=2")


def test_sanitize_media_playlist_strips_ssai_and_discontinuities():
    raw_playlist = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:101
#EXT-X-KEY:METHOD=AES-128,URI="keys/key1.key"
#EXT-X-PROGRAM-DATE-TIME:2026-11-26T20:00:00Z
#EXTINF:6.000,
seg_101.ts?hdntl=exp=123
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-11-26T20:00:06Z
#EXTINF:0.000,
/targeting/analytics?hdntl=exp=123~acl=*~hmac=deadbeef
#EXTINF:2.000,
#EXT-X-BYTERANGE:1024@0
https://ads.example.com/beacon?event=start
#EXT-X-DISCONTINUITY
#EXTINF:6.000,
seg_102.ts?hdntl=exp=123
"""
    upstream_base = "https://cdn.example.com/hls/live/720p/index.m3u8"
    sanitized, stats = relay.sanitize_media_playlist(
        raw_playlist, upstream_base, strip_discontinuities=True
    )

    assert stats["stripped_segments"] == 2
    assert stats["stripped_discontinuities"] == 2
    assert "/targeting/analytics" not in sanitized
    assert "ads.example.com/beacon" not in sanitized
    assert "#EXT-X-DISCONTINUITY" not in sanitized
    assert "#EXT-X-BYTERANGE:1024@0" not in sanitized
    assert "2026-11-26T20:00:06Z" not in sanitized
    # Valid segments and key URI resolved against upstream_base
    assert 'URI="https://cdn.example.com/hls/live/720p/keys/key1.key"' in sanitized
    assert "https://cdn.example.com/hls/live/720p/seg_101.ts?hdntl=exp=123" in sanitized
    assert "https://cdn.example.com/hls/live/720p/seg_102.ts?hdntl=exp=123" in sanitized
    assert "2026-11-26T20:00:00Z" in sanitized


def test_sanitize_media_playlist_can_preserve_discontinuities():
    raw_playlist = """#EXTM3U
#EXTINF:6.0,
seg1.ts
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
seg2.ts
"""
    sanitized, stats = relay.sanitize_media_playlist(
        raw_playlist, "https://cdn.example.com/live/media.m3u8", strip_discontinuities=False
    )
    assert stats["stripped_segments"] == 0
    assert stats["stripped_discontinuities"] == 0
    assert "#EXT-X-DISCONTINUITY" in sanitized


def test_rewrite_master_playlist_rewrites_variants_and_media_tags():
    master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="English",URI="audio/eng.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080
1080p/index.m3u8?token=abc
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
https://alt.example.com/720p/index.m3u8
"""
    rewritten = relay.rewrite_master_playlist(
        master,
        upstream_base_url="https://cdn.example.com/live/master.m3u8",
        stream_id="abc123def456",
        relay_base_url="http://198.51.100.10:8844",
    )

    expected_audio = urllib.parse.quote("https://cdn.example.com/live/audio/eng.m3u8", safe="")
    expected_1080 = urllib.parse.quote("https://cdn.example.com/live/1080p/index.m3u8?token=abc", safe="")
    expected_720 = urllib.parse.quote("https://alt.example.com/720p/index.m3u8", safe="")

    assert f'URI="http://198.51.100.10:8844/stream/abc123def456/variant.m3u8?u={expected_audio}"' in rewritten
    assert f"http://198.51.100.10:8844/stream/abc123def456/variant.m3u8?u={expected_1080}" in rewritten
    assert f"http://198.51.100.10:8844/stream/abc123def456/variant.m3u8?u={expected_720}" in rewritten


# ---------------------------------------------------------------------------
# FastAPI Relay Endpoints & Auth Guard Tests
# ---------------------------------------------------------------------------


def test_relay_endpoints_and_auth_guard(monkeypatch):
    relay.registry.streams.clear()
    monkeypatch.setattr(relay.nas_config, "HOST_2_IP", "198.51.100.25")
    monkeypatch.delenv("HLS_RELAY_TOKEN", raising=False)
    monkeypatch.delenv("ZERO_HEALTH_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(relay, "_get_auth_tokens", lambda: ["secret-test-token"])

    master_body = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1280x720
720p/media.m3u8?hdntl=tok1
"""
    media_body = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXTINF:0.0,
/targeting/analytics?hdntl=exp=1700000~acl=*~hmac=deadbeef
#EXT-X-DISCONTINUITY
#EXTINF:6.0,
seg001.ts?hdntl=tok1
"""

    async def fake_fetch(url: str, headers: dict[str, str]):
        if "master.m3u8" in url:
            return master_body, "https://cdn.example.com/nfl/master.m3u8"
        if "media.m3u8" in url:
            return media_body, "https://cdn.example.com/nfl/720p/media.m3u8"
        raise AssertionError(f"Unexpected URL fetched: {url}")

    relay.set_fetch_upstream(fake_fetch)
    try:
        # 1. Non-loopback client without token -> 401 on /register and /status
        lan_client = TestClient(relay.app, client=("198.51.100.99", 50000))
        r_unauth = lan_client.post(
            "/register",
            json={"upstream_url": "https://cdn.example.com/nfl/master.m3u8", "mode": "hls"},
        )
        assert r_unauth.status_code == 401
        assert lan_client.get("/status").status_code == 401

        # /health remains unauthenticated for LAN monitors
        r_health = lan_client.get("/health")
        assert r_health.status_code == 200
        assert r_health.json()["status"] == "ok"

        # Non-loopback client WITH valid Bearer token -> 200
        r_bearer = lan_client.post(
            "/register",
            json={"upstream_url": "https://cdn.example.com/nfl/master.m3u8", "mode": "hls"},
            headers={"Authorization": "Bearer secret-test-token"},
        )
        assert r_bearer.status_code == 200

        # 2. Loopback client -> 200 without Bearer token
        local_client = TestClient(relay.app, client=("127.0.0.1", 50000))
        r_reg = local_client.post(
            "/register",
            json={
                "upstream_url": "https://cdn.example.com/nfl/master.m3u8",
                "referer": "https://nfl24all.ir/",
                "mode": "hls",
            },
        )
        assert r_reg.status_code == 200
        reg_data = r_reg.json()
        stream_id = reg_data["stream_id"]
        assert reg_data["hls_url"] == f"http://198.51.100.25:8844/stream/{stream_id}/master.m3u8"
        assert reg_data["ts_url"] == f"http://198.51.100.25:8844/stream/{stream_id}/live.ts"

        # 3. Unauthenticated LAN client (e.g. VLC on Android TV) can fetch master and variant playlists
        r_master = lan_client.get(f"/stream/{stream_id}/master.m3u8")
        assert r_master.status_code == 200
        assert r_master.headers["content-type"].startswith("application/vnd.apple.mpegurl")
        assert r_master.headers["cache-control"] == "no-store"

        variant_lines = [ln for ln in r_master.text.splitlines() if ln and not ln.startswith("#")]
        assert len(variant_lines) == 1
        variant_path = urllib.parse.urlsplit(variant_lines[0])
        r_variant = lan_client.get(f"{variant_path.path}?{variant_path.query}")
        assert r_variant.status_code == 200
        assert "/targeting/analytics" not in r_variant.text
        assert "#EXT-X-DISCONTINUITY" not in r_variant.text
        assert "https://cdn.example.com/nfl/720p/seg001.ts?hdntl=tok1" in r_variant.text

        # /media.m3u8 direct convenience endpoint also works without ?u=
        r_media = lan_client.get(f"/stream/{stream_id}/media.m3u8")
        assert r_media.status_code == 200
        assert "https://cdn.example.com/nfl/720p/seg001.ts?hdntl=tok1" in r_media.text

        # /status reports cumulative sanitization stats for loopback client
        r_status = local_client.get("/status")
        assert r_status.status_code == 200
        assert r_status.json()["active_streams"] >= 2
    finally:
        relay.set_fetch_upstream(None)
        relay.registry.streams.clear()


def test_default_fetch_upstream_handles_raw_gzip():
    raw_playlist = "#EXTM3U\n#EXTINF:6.0,\nseg.ts\n"
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(raw_playlist.encode("utf-8"))
    gz_bytes = buf.getvalue()
    assert gz_bytes[:2] == b"\x1f\x8b"
    assert gzip.decompress(gz_bytes).decode("utf-8") == raw_playlist


# ---------------------------------------------------------------------------
# Unified Sports Stream Extractor Tests (NFL & MLB)
# ---------------------------------------------------------------------------


def _sample_nfl_stateshot():
    return {
        "teams": [
            {"id": 10, "name": "Chicago Bears"},
            {"id": 11, "name": "Detroit Lions"},
        ],
        "games": [
            {
                "id": 9001,
                "away_team_id": 10,
                "home_team_id": 11,
                "status": "L",
                "scores": {"away": 17, "home": 14},
            }
        ],
        "media_events": [
            {
                "id": 701,
                "game_id": 9001,
                "title": "Español",
                "description": "CBS Deportes",
                "status": "L",
            },
            {
                "id": 702,
                "game_id": 9001,
                "title": "National",
                "description": "CBS Broadcast",
                "status": "L",
            },
        ],
        "flavors": [
            {
                "id": "free.live.nfl",
                "media_event_ids": [701, 702],
                "name": "NFL Live Free",
            }
        ],
    }


def _sample_mlb_stateshot():
    return {
        "teams": [
            {"id": 53, "name": "Chicago Cubs"},
            {"id": 48, "name": "Milwaukee Brewers"},
        ],
        "games": [
            {
                "id": 6683,
                "away_team_id": 53,
                "home_team_id": 48,
                "status": "L",
                "scores": {"away": 3, "home": 1},
            }
        ],
        "media_events": [
            {
                "id": 56958,
                "game_id": 6683,
                "title": "Away",
                "description": "Cubs Broadcast",
                "status": "L",
            },
            {
                "id": 56976,
                "game_id": 6683,
                "title": "Home",
                "description": "Brewers Broadcast",
                "status": "L",
            },
        ],
        "flavors": [
            {
                "id": "free.live.sportsnet",
                "media_event_ids": [56958, 56976],
                "name": "Sportsnet Free",
            }
        ],
    }


def test_nfl_prefers_live_english_national_over_espanol():
    data = _sample_nfl_stateshot()
    game, teams, tid = extractor.resolve_team_game(data, "bears")
    assert tid == 10
    event = extractor.resolve_media_event(
        data, game, tid, target_team_name=teams[tid], sport="nfl"
    )
    assert event["id"] == 702
    assert event["title"] == "National"


def test_mlb_prefers_team_side_broadcast():
    data = _sample_mlb_stateshot()
    game, teams, tid = extractor.resolve_team_game(data, "cubs")
    assert tid == 53
    event = extractor.resolve_media_event(
        data, game, tid, target_team_name=teams[tid], sport="mlb"
    )
    assert event["id"] == 56958
    assert event["title"] == "Away"


def test_extract_stream_nfl_registers_relay_and_casts():
    nfl_data = _sample_nfl_stateshot()
    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=12):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        if url == "https://nfl24all.ir":
            html = f"<html><script>var stateshot = {json.dumps(nfl_data)};</script></html>"
            resp.read.return_value = html.encode("utf-8")
            return resp
        if url == "https://api.nfl24all.ir/api/v2/generate_stream_info":
            body = json.loads(req.data.decode("utf-8"))
            assert body["media_event_id"] == 702
            assert body["flavor_id"] == "free.live.nfl"
            resp.read.return_value = json.dumps(
                {"url": "https://cdn.example.com/cbs/master.m3u8"}
            ).encode("utf-8")
            return resp
        if url == "http://127.0.0.1:8844/register":
            captured["register_body"] = req.data.decode("utf-8")
            resp.read.return_value = json.dumps(
                {
                    "stream_id": "deadbeef1234",
                    "hls_url": "http://198.51.100.25:8844/stream/deadbeef1234/master.m3u8",
                    "ts_url": "http://198.51.100.25:8844/stream/deadbeef1234/live.ts",
                }
            ).encode("utf-8")
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen), patch.object(
        extractor, "cast_to_home_assistant", return_value=True
    ) as mock_cast:
        res = extractor.extract_stream(sport="nfl", team="bears", cast=True)

    assert res["relayed"] is True
    assert res["stream_url"] == "http://198.51.100.25:8844/stream/deadbeef1234/master.m3u8"
    assert res["upstream_url"] == "https://cdn.example.com/cbs/master.m3u8"
    assert res["casted"] is True
    mock_cast.assert_called_once_with(
        "http://198.51.100.25:8844/stream/deadbeef1234/master.m3u8",
        "media_player.living_room_tv",
    )


def test_extract_stream_nfl_falls_back_gracefully_when_relay_offline(capsys):
    nfl_data = _sample_nfl_stateshot()

    def fake_urlopen(req, timeout=12):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        if url == "https://nfl24all.ir":
            html = f"<html><script>var stateshot = {json.dumps(nfl_data)};</script></html>"
            resp.read.return_value = html.encode("utf-8")
            return resp
        if url == "https://api.nfl24all.ir/api/v2/generate_stream_info":
            resp.read.return_value = json.dumps(
                {"url": "https://cdn.example.com/cbs/master.m3u8"}
            ).encode("utf-8")
            return resp
        if url == "http://127.0.0.1:8844/register":
            raise ConnectionRefusedError("Connection refused")
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = extractor.extract_stream(sport="nfl", team="bears", cast=False)

    err = capsys.readouterr().err
    assert "falling back to direct upstream URL" in err
    assert res["relayed"] is False
    assert res["stream_url"] == "https://cdn.example.com/cbs/master.m3u8"
