"""Unit tests for tools/seerr_card_sync.py."""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if sys.path[0] != REPO_ROOT:
    sys.path.insert(0, REPO_ROOT)

import tools.seerr_card_sync as scs

TEST_SEERR_URL = "https://198.51.100.10:5055"
TEST_SEERR_KEY = "test-seerr-api-key"
TEST_DISCORD_TOKEN = "test-discord-bot-token"


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpClient:
    """In-memory HTTP client simulating Discord v10 + Seerr v1 endpoints."""

    def __init__(
        self,
        messages: list[dict[str, Any]],
        webhooks: list[dict[str, Any]],
        seerr_pages: list[dict[str, Any]],
        details: dict[tuple[str, str], dict[str, Any]],
        patch_status: int = 200,
    ):
        self.messages = messages
        self.webhooks = webhooks
        self.seerr_pages = seerr_pages
        self.details = details
        self.patch_status = patch_status
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.patch_calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int = 15,
    ) -> FakeResponse:
        self.get_calls.append((url, params))
        if url.endswith("/messages"):
            return FakeResponse(self.messages)
        if url.endswith("/webhooks"):
            return FakeResponse(self.webhooks)
        if url.endswith("/api/v1/request"):
            skip = int((params or {}).get("skip", 0))
            page_idx = skip // 100
            if page_idx < len(self.seerr_pages):
                return FakeResponse(self.seerr_pages[page_idx])
            return FakeResponse({"results": [], "pageInfo": {"pages": len(self.seerr_pages)}})
        if "/api/v1/" in url:
            tail = url.split("/api/v1/", 1)[1]
            parts = tail.split("/", 1)
            if len(parts) == 2 and (parts[0], parts[1]) in self.details:
                return FakeResponse(self.details[(parts[0], parts[1])])
            return FakeResponse({}, status_code=404)
        return FakeResponse({}, status_code=404)

    def patch(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: int = 15,
    ) -> FakeResponse:
        self.patch_calls.append((url, json or {}))
        return FakeResponse({}, status_code=self.patch_status)


def _make_embed(
    title: str,
    status: str = "Processing",
    requested_by: str = "ryan",
    seasons: str | None = None,
    poster_url: str = "https://image.tmdb.org/t/p/w600_and_h900_bestv2/abc123.jpg",
    include_readonly: bool = True,
) -> dict[str, Any]:
    fields = [
        {"name": "Requested By", "value": requested_by, "inline": True},
        {"name": "Request Status", "value": status, "inline": True},
    ]
    if seasons is not None:
        fields.append({"name": "Requested Seasons", "value": seasons, "inline": True})

    embed: dict[str, Any] = {
        "title": title,
        "color": 10181046,
        "fields": fields,
        "thumbnail": {"url": poster_url},
    }
    if include_readonly:
        embed["type"] = "rich"
        embed["flags"] = 0
        embed["content_type"] = "application/json"
        embed["thumbnail"].update({
            "proxy_url": "https://media.discordapp.net/external/xyz/abc123.jpg",
            "width": 600,
            "height": 900,
            "placeholder": "aaaa",
            "placeholder_version": 1,
        })
    return embed


def test_get_seerr_config_env_and_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SEERR_BASE_URL", raising=False)
    monkeypatch.delenv("SEERR_API_KEY", raising=False)

    env_file = tmp_path / "env.json"
    env_file.write_text(
        json.dumps({
            "SEERR_BASE_URL": f"{TEST_SEERR_URL}/",
            "SEERR_API_KEY": TEST_SEERR_KEY,
        }),
        encoding="utf-8",
    )

    base_url, api_key = scs.get_seerr_config(env_file)
    assert base_url == TEST_SEERR_URL
    assert api_key == TEST_SEERR_KEY

    monkeypatch.setenv("SEERR_BASE_URL", "https://198.51.100.20:5055/")
    monkeypatch.setenv("SEERR_API_KEY", "override-key")
    base_url2, api_key2 = scs.get_seerr_config(env_file)
    assert base_url2 == "https://198.51.100.20:5055"
    assert api_key2 == "override-key"


def test_get_seerr_config_missing_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SEERR_BASE_URL", raising=False)
    monkeypatch.delenv("SEERR_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        scs.get_seerr_config(tmp_path / "nonexistent.json")


def test_parse_seerr_embed_movie_and_tv():
    movie_embed = _make_embed(
        "Movie Request Automatically Approved: Dune: Part Two (2024)",
        status="Processing",
        requested_by="Alice",
        poster_url="https://image.tmdb.org/t/p/w600_and_h900_bestv2/dune2.jpg",
    )
    parsed_movie = scs.parse_seerr_embed(movie_embed, message_ts="2026-11-19T08:00:00Z")
    assert parsed_movie == {
        "media_type": "movie",
        "title": "Dune: Part Two",
        "year": 2024,
        "requested_by": "Alice",
        "current_status": "Processing",
        "requested_seasons": set(),
        "poster_path": "/dune2.jpg",
        "message_ts": "2026-11-19T08:00:00Z",
    }

    tv_embed = _make_embed(
        "Series Request Automatically Approved: The Office (US) (2005)",
        status="Partially Available",
        requested_by="Bob",
        seasons="1, 2",
        poster_url="https://image.tmdb.org/t/p/original/office_us.jpg?v=2",
    )
    parsed_tv = scs.parse_seerr_embed(tv_embed, message_ts="2026-11-19T08:05:00Z")
    assert parsed_tv is not None
    assert parsed_tv["media_type"] == "tv"
    assert parsed_tv["title"] == "The Office (US)"
    assert parsed_tv["year"] == 2005
    assert parsed_tv["requested_by"] == "Bob"
    assert parsed_tv["current_status"] == "Partially Available"
    assert parsed_tv["requested_seasons"] == {1, 2}
    assert parsed_tv["poster_path"] == "/office_us.jpg"


def test_parse_seerr_embed_rejects_non_seerr():
    assert scs.parse_seerr_embed({}) is None
    assert scs.parse_seerr_embed({"title": "Radarr Grabbed: Inception (2010)"}) is None
    assert scs.parse_seerr_embed(None) is None  # type: ignore[arg-type]


def test_match_card_movie_vs_tv_disambiguation():
    card_movie = {
        "media_type": "movie",
        "title": "Fargo",
        "year": 1996,
        "requested_by": "ryan",
        "requested_seasons": set(),
        "poster_path": "",
        "message_ts": "2026-11-19T08:00:00Z",
    }
    tv_cand = {
        "request": {
            "id": 10,
            "createdAt": "2026-11-19T08:00:01Z",
            "requestedBy": {"plexUsername": "ryan"},
            "media": {"mediaType": "tv", "tmdbId": 60622},
        },
        "detail": {"name": "Fargo", "firstAirDate": "2014-04-15", "posterPath": "/fargo_tv.jpg"},
    }
    movie_cand = {
        "request": {
            "id": 11,
            "createdAt": "2026-11-19T08:00:05Z",
            "requestedBy": {"plexUsername": "ryan"},
            "media": {"mediaType": "movie", "tmdbId": 275},
        },
        "detail": {"title": "Fargo", "releaseDate": "1996-03-08", "posterPath": "/fargo_movie.jpg"},
    }

    matched = scs.match_card_to_request(card_movie, [tv_cand, movie_cand])
    assert matched is movie_cand


def test_match_card_poster_path_and_timestamp_proximity():
    # Two TV requests for same poster & user (different timestamps / seasons)
    card = {
        "media_type": "tv",
        "title": "Severance",
        "year": 2022,
        "requested_by": "ryan",
        "requested_seasons": {2},
        "poster_path": "/sev.jpg",
        "message_ts": "2026-11-19T10:00:00Z",
    }
    old_req = {
        "request": {
            "id": 101,
            "createdAt": "2026-10-01T10:00:00Z",
            "requestedBy": {"displayName": "ryan"},
            "seasons": [{"seasonNumber": 1, "status": 5}],
            "media": {"mediaType": "tv", "tmdbId": 95396},
        },
        "detail": {"name": "Severance", "firstAirDate": "2022-02-18", "posterPath": "/sev.jpg"},
    }
    new_req = {
        "request": {
            "id": 102,
            "createdAt": "2026-11-19T09:59:58Z",
            "requestedBy": {"displayName": "ryan"},
            "seasons": [{"seasonNumber": 2, "status": 2}],
            "media": {"mediaType": "tv", "tmdbId": 95396},
        },
        "detail": {"name": "Severance", "firstAirDate": "2022-02-18", "posterPath": "/sev.jpg"},
    }
    other_user_req = {
        "request": {
            "id": 103,
            "createdAt": "2026-11-19T10:00:00Z",
            "requestedBy": {"displayName": "guest"},
            "seasons": [{"seasonNumber": 2, "status": 3}],
            "media": {"mediaType": "tv", "tmdbId": 95396},
        },
        "detail": {"name": "Severance", "firstAirDate": "2022-02-18", "posterPath": "/sev.jpg"},
    }

    matched = scs.match_card_to_request(card, [other_user_req, old_req, new_req])
    assert matched is new_req

    # Title fallback with timestamp proximity when poster_path is absent
    card_no_poster = dict(card, poster_path="", requested_seasons=set())
    matched_prox = scs.match_card_to_request(card_no_poster, [old_req, new_req])
    assert matched_prox is new_req


def test_resolve_seerr_request_status_variants():
    assert scs.resolve_seerr_request_status({"status": 3}) == ("Declined", 15158332, "Declined")
    assert scs.resolve_seerr_request_status({"status": 4}) == ("Failed", 15158332, "Failed")
    assert scs.resolve_seerr_request_status({"status": 1, "media": {"status": 2}}) == (
        "Pending Approval",
        10181046,
        "Pending Approval",
    )
    assert scs.resolve_seerr_request_status({"status": 2, "media": {"status": 3}}) == (
        "Processing",
        10181046,
        "Processing",
    )
    assert scs.resolve_seerr_request_status({"status": 5, "media": {"status": 3}}) == (
        "Available",
        3066993,
        "Available on Plex",
    )
    assert scs.resolve_seerr_request_status({"status": 2, "media": {"status": 5}}) == (
        "Available",
        3066993,
        "Available on Plex",
    )
    assert scs.resolve_seerr_request_status({"status": 2, "media": {"status": 4}}) == (
        "Partially Available",
        15965202,
        "Partially Available on Plex",
    )


def test_resolve_seerr_request_status_tv_per_season():
    # Some seasons 4/5 -> Partially Available
    req_partial = {
        "status": 2,
        "media": {"status": 4},
        "seasons": [
            {"seasonNumber": 1, "status": 2},
            {"seasonNumber": 2, "status": 2},
        ],
    }
    detail_partial = {
        "mediaInfo": {
            "seasons": [
                {"seasonNumber": 1, "status": 5},
                {"seasonNumber": 2, "status": 3},
            ]
        }
    }
    assert scs.resolve_seerr_request_status(
        req_partial, detail=detail_partial, requested_seasons={1, 2}
    ) == ("Partially Available", 15965202, "Partially Available on Plex")

    # All requested seasons == 5 -> Available (even if req.status is still 2)
    detail_full = {
        "mediaInfo": {
            "seasons": [
                {"seasonNumber": 1, "status": 5},
                {"seasonNumber": 2, "status": 5},
            ]
        }
    }
    assert scs.resolve_seerr_request_status(
        req_partial, detail=detail_full, requested_seasons={1, 2}
    ) == ("Available", 3066993, "Available on Plex")

    # Unrequested season 1 already available (media.status == 4), requested season 2 still processing
    req_s2_only = {
        "status": 2,
        "media": {"status": 4},
        "seasons": [{"seasonNumber": 2, "status": 2}],
    }
    detail_s1_avail = {
        "mediaInfo": {
            "seasons": [
                {"seasonNumber": 1, "status": 5},
                {"seasonNumber": 2, "status": 3},
            ]
        }
    }
    assert scs.resolve_seerr_request_status(
        req_s2_only, detail=detail_s1_avail, requested_seasons={2}
    ) == ("Processing", 10181046, "Processing")


def test_build_updated_embed_strips_readonly_and_formats_pt():
    orig = _make_embed(
        "Movie Request Automatically Approved: Arrival (2016)",
        status="Processing",
        include_readonly=True,
    )
    orig_snapshot = copy.deepcopy(orig)
    fixed_pt = datetime(2026, 11, 19, 14, 35, tzinfo=ZoneInfo("America/Los_Angeles"))

    updated = scs.build_updated_embed(
        orig,
        new_status="Available",
        new_color=3066993,
        footer_suffix="Available on Plex",
        now_pt=fixed_pt,
    )

    assert orig == orig_snapshot  # deep copy did not mutate input
    assert updated["color"] == 3066993
    status_field = next(f for f in updated["fields"] if f["name"] == "Request Status")
    assert status_field["value"] == "Available"
    assert updated["footer"]["text"] == (
        "Status synced via Seerr • Available on Plex (2026-11-19 02:35 PM PT)"
    )

    for forbidden in scs.READONLY_EMBED_KEYS:
        assert forbidden not in updated
        assert forbidden not in updated["thumbnail"]
    assert updated["thumbnail"]["url"] == orig["thumbnail"]["url"]


def test_sync_seerr_cards_dry_run_and_live_patch(monkeypatch):
    monkeypatch.setenv("SEERR_BASE_URL", TEST_SEERR_URL)
    monkeypatch.setenv("SEERR_API_KEY", TEST_SEERR_KEY)
    monkeypatch.setattr(scs, "get_discord_token", lambda: TEST_DISCORD_TOKEN)

    sleep_calls: list[float] = []
    monkeypatch.setattr(scs.time, "sleep", lambda sec: sleep_calls.append(sec))

    messages = [
        # Candidate 1: Movie Processing -> Available
        {
            "id": "msg-1",
            "webhook_id": scs.DEFAULT_WEBHOOK_ID,
            "timestamp": "2026-11-19T08:00:00Z",
            "embeds": [
                _make_embed(
                    "Movie Request Automatically Approved: Dune (2021)",
                    status="Processing",
                    requested_by="ryan",
                    poster_url="https://image.tmdb.org/t/p/w600_and_h900_bestv2/dune.jpg",
                )
            ],
        },
        # Candidate 2: TV Processing -> Partially Available
        {
            "id": "msg-2",
            "webhook_id": scs.DEFAULT_WEBHOOK_ID,
            "timestamp": "2026-11-19T08:10:00Z",
            "embeds": [
                _make_embed(
                    "Series Request Automatically Approved: Andor (2022)",
                    status="Processing",
                    requested_by="ryan",
                    seasons="1, 2",
                    poster_url="https://image.tmdb.org/t/p/w600_and_h900_bestv2/andor.jpg",
                )
            ],
        },
        # Candidate 3: Unchanged (still Processing)
        {
            "id": "msg-3",
            "webhook_id": scs.DEFAULT_WEBHOOK_ID,
            "timestamp": "2026-11-19T08:20:00Z",
            "embeds": [
                _make_embed(
                    "Movie Request Automatically Approved: Solaris (1972)",
                    status="Processing",
                    requested_by="ryan",
                    poster_url="https://image.tmdb.org/t/p/w600_and_h900_bestv2/solaris.jpg",
                )
            ],
        },
        # Already Available -> not a candidate
        {
            "id": "msg-4",
            "webhook_id": scs.DEFAULT_WEBHOOK_ID,
            "timestamp": "2026-11-19T07:00:00Z",
            "embeds": [
                _make_embed(
                    "Movie Request Automatically Approved: Heat (1995)",
                    status="Available",
                    requested_by="ryan",
                    poster_url="https://image.tmdb.org/t/p/w600_and_h900_bestv2/heat.jpg",
                )
            ],
        },
        # Non-webhook bot message -> ignored
        {
            "id": "msg-5",
            "timestamp": "2026-11-19T07:05:00Z",
            "embeds": [{"title": "Chat message"}],
        },
    ]

    webhooks = [{"id": scs.DEFAULT_WEBHOOK_ID, "name": "Request", "token": "wh-secret-token"}]

    seerr_pages = [
        {
            "pageInfo": {"pages": 1},
            "results": [
                {
                    "id": 201,
                    "status": 5,
                    "createdAt": "2026-11-19T08:00:00Z",
                    "requestedBy": {"username": "ryan"},
                    "media": {"mediaType": "movie", "tmdbId": 438631, "status": 5},
                },
                # Duplicate tmdbId to verify memoization of /api/v1/movie/438631
                {
                    "id": 202,
                    "status": 2,
                    "createdAt": "2026-11-18T08:00:00Z",
                    "requestedBy": {"username": "other_user"},
                    "media": {"mediaType": "movie", "tmdbId": 438631, "status": 5},
                },
                {
                    "id": 203,
                    "status": 2,
                    "createdAt": "2026-11-19T08:10:00Z",
                    "requestedBy": {"username": "ryan"},
                    "seasons": [{"seasonNumber": 1, "status": 2}, {"seasonNumber": 2, "status": 2}],
                    "media": {"mediaType": "tv", "tmdbId": 83867, "status": 4},
                },
                {
                    "id": 204,
                    "status": 2,
                    "createdAt": "2026-11-19T08:20:00Z",
                    "requestedBy": {"username": "ryan"},
                    "media": {"mediaType": "movie", "tmdbId": 593, "status": 3},
                },
            ],
        }
    ]

    details = {
        ("movie", "438631"): {"title": "Dune", "releaseDate": "2021-09-15", "posterPath": "/dune.jpg"},
        ("tv", "83867"): {
            "name": "Andor",
            "firstAirDate": "2022-09-21",
            "posterPath": "/andor.jpg",
            "mediaInfo": {
                "seasons": [
                    {"seasonNumber": 1, "status": 5},
                    {"seasonNumber": 2, "status": 3},
                ]
            },
        },
        ("movie", "593"): {"title": "Solaris", "releaseDate": "1972-03-20", "posterPath": "/solaris.jpg"},
    }

    # 1. Dry run
    dry_client = FakeHttpClient(messages, webhooks, seerr_pages, details)
    dry_res = scs.sync_seerr_cards(dry_run=True, http_client=dry_client)
    assert dry_res["scanned"] == 5
    assert dry_res["candidates"] == 3
    assert dry_res["updated"] == 2
    assert dry_res["skipped"] == 1
    assert dry_res["errors"] == 0
    assert len(dry_res["changes"]) == 2
    assert all(c["dry_run"] is True for c in dry_res["changes"])
    assert len(dry_client.patch_calls) == 0
    assert sleep_calls == []
    # Memoized detail fetch: movie/438631 fetched once despite 2 requests
    detail_urls = [u for u, _ in dry_client.get_calls if "/api/v1/movie/438631" in u]
    assert len(detail_urls) == 1

    # 2. Live run
    live_client = FakeHttpClient(messages, webhooks, seerr_pages, details)
    live_res = scs.sync_seerr_cards(dry_run=False, http_client=live_client)
    assert live_res["scanned"] == 5
    assert live_res["candidates"] == 3
    assert live_res["updated"] == 2
    assert live_res["skipped"] == 1
    assert live_res["errors"] == 0
    assert len(live_client.patch_calls) == 2
    assert sleep_calls == [0.4, 0.4]

    patch_url_1, patch_body_1 = live_client.patch_calls[0]
    assert patch_url_1 == (
        f"https://discord.com/api/v10/webhooks/{scs.DEFAULT_WEBHOOK_ID}/wh-secret-token/messages/msg-1"
    )
    patched_embed_1 = patch_body_1["embeds"][0]
    assert patched_embed_1["color"] == 3066993
    assert "type" not in patched_embed_1
    assert "proxy_url" not in patched_embed_1["thumbnail"]
    assert "Status synced via Seerr • Available on Plex" in patched_embed_1["footer"]["text"]

    patch_url_2, patch_body_2 = live_client.patch_calls[1]
    assert patch_url_2.endswith("/messages/msg-2")
    patched_embed_2 = patch_body_2["embeds"][0]
    assert patched_embed_2["color"] == 15965202
    assert "Status synced via Seerr • Partially Available on Plex" in patched_embed_2["footer"]["text"]


def test_seerr_card_sync_transient_timeout_suppression(monkeypatch):
    """Single transient Discord read timeouts are suppressed until 3 consecutive failures."""
    import requests

    import tools.sidecars as sc

    monkeypatch.setattr(sc, "_SEERR_SYNC_TRANSIENT_STREAK", 0)
    err = requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='discord.com', port=443): Read timed out. (read timeout=30)")
    monkeypatch.setattr("tools.seerr_card_sync.sync_seerr_cards", lambda **_kw: (_ for _ in ()).throw(err))

    ok1, msg1 = sc.run_seerr_card_sync()
    ok2, msg2 = sc.run_seerr_card_sync()
    ok3, msg3 = sc.run_seerr_card_sync()

    assert ok1 is True and "suppressed" in msg1
    assert ok2 is True and "suppressed" in msg2
    assert ok3 is False and "Seerr card sync failed" in msg3

    # Clean run resets the streak
    monkeypatch.setattr("tools.seerr_card_sync.sync_seerr_cards", lambda **_kw: {"updated": 0, "errors": 0, "scanned": 5})
    ok_reset, _ = sc.run_seerr_card_sync()
    assert ok_reset is True
    assert sc._SEERR_SYNC_TRANSIENT_STREAK == 0
