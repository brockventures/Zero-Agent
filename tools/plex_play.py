#!/usr/bin/env python3
"""
Plex Living Room TV Direct Launcher & Playback Controller (`/workspace/tools/plex_play.py`).

Resolves any Movie or TV Show / Episode on Host 1 Plex Media Server and launches it
directly inside the native Plex for Android TV app (`com.plexapp.android`) on the
Living Room Google TV Streamer via Home Assistant (`remote.living_room_tv`,
`media_player.living_room_tv`, and `media_player.plex_plex_for_android_tv_google_tv_streamer`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import requests
from pathlib import Path
from typing import Any, Optional

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools import nas_config
except ImportError:
    import nas_config  # type: ignore

try:
    from tools.plex_audiobook_enricher import _load_state, _resolve_plex_token, _save_state
except ImportError:
    from plex_audiobook_enricher import _load_state, _resolve_plex_token, _save_state  # type: ignore


DEFAULT_REMOTE_ENTITY = "remote.living_room_tv"
DEFAULT_ANDROID_TV_ENTITY = "media_player.living_room_tv"
DEFAULT_CAST_ENTITY = "media_player.living_room_living_room_tv"
DEFAULT_PLEX_ENTITY = "media_player.plex_plex_for_android_tv_google_tv_streamer"


def load_ha_config(ha_cfg_path: str | Path = "/secrets/ha.json") -> tuple[str, str]:
    """Return (base_url, token) for Home Assistant."""
    p = Path(ha_cfg_path)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        return data["url"].rstrip("/"), data["token"]
    env_p = Path("/secrets/env.json")
    if env_p.exists():
        data = json.loads(env_p.read_text(encoding="utf-8"))
        return data.get("HA_BASE_URL", f"http://{nas_config.HOST_1_IP}:8123").rstrip("/"), data["HA_ACCESS_TOKEN"]
    raise RuntimeError("Missing Home Assistant credentials in /secrets/ha.json or /secrets/env.json")


def get_plex_server_info(plex_url: str, plex_token: str) -> str:
    """Return the Plex server's machineIdentifier."""
    r = requests.get(
        f"{plex_url}/",
        params={"X-Plex-Token": plex_token},
        headers={"Accept": "application/json"},
        timeout=8,
    )
    r.raise_for_status()
    return str(r.json()["MediaContainer"]["machineIdentifier"])


def resolve_plex_media(
    query: str,
    plex_url: str,
    plex_token: str,
    media_type: str = "auto",
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> dict[str, Any]:
    """Search Plex for a movie or TV show/episode matching `query`."""
    r = requests.get(
        f"{plex_url}/hubs/search",
        params={"query": query, "X-Plex-Token": plex_token, "limit": 10},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    hubs = r.json().get("MediaContainer", {}).get("Hub", [])

    candidates: list[dict[str, Any]] = []
    for hub in hubs:
        for item in hub.get("Metadata", []):
            itype = item.get("type")
            if itype not in ("movie", "show", "episode"):
                continue
            if media_type != "auto" and itype != media_type:
                continue
            candidates.append(item)

    if not candidates:
        raise ValueError(f"No Plex movie or show found matching '{query}'.")

    # Prioritize exact or close title matches on movies/shows before episodes
    q_norm = re.sub(r"[^a-z0-9]+", "", query.lower())

    def _score(item: dict[str, Any]) -> tuple[int, int]:
        title = item.get("title") or ""
        t_norm = re.sub(r"[^a-z0-9]+", "", title.lower())
        exact = 2 if t_norm == q_norm else (1 if q_norm in t_norm or t_norm in q_norm else 0)
        type_prio = 2 if item.get("type") in ("movie", "show") else 1
        return (exact, type_prio)

    candidates.sort(key=_score, reverse=True)
    chosen = candidates[0]

    # If a show was matched and season/episode or OnDeck is needed, resolve the target episode
    if chosen.get("type") == "show":
        show_rk = chosen["ratingKey"]
        show_title = chosen.get("title", query)
        if season is not None and episode is not None:
            r_eps = requests.get(
                f"{plex_url}/library/metadata/{show_rk}/allLeaves",
                params={"X-Plex-Token": plex_token},
                headers={"Accept": "application/json"},
                timeout=10,
            )
            for ep in r_eps.json().get("MediaContainer", {}).get("Metadata", []):
                if int(ep.get("parentIndex", -1)) == int(season) and int(ep.get("index", -1)) == int(episode):
                    chosen = ep
                    break
        else:
            # Check if there is an OnDeck / next episode to resume, otherwise S01E01
            r_eps = requests.get(
                f"{plex_url}/library/metadata/{show_rk}/allLeaves",
                params={"X-Plex-Token": plex_token},
                headers={"Accept": "application/json"},
                timeout=10,
            )
            leaves = r_eps.json().get("MediaContainer", {}).get("Metadata", [])
            if leaves:
                unwatched = [ep for ep in leaves if not ep.get("viewCount")]
                chosen = unwatched[0] if unwatched else leaves[0]
        chosen["show_title"] = show_title

    return {
        "ratingKey": str(chosen["ratingKey"]),
        "key": str(chosen["key"]),
        "type": str(chosen.get("type", "movie")),
        "title": str(chosen.get("title", query)),
        "show_title": chosen.get("show_title") or chosen.get("grandparentTitle"),
        "season": chosen.get("parentIndex"),
        "episode": chosen.get("index"),
        "year": chosen.get("year"),
        "librarySectionTitle": chosen.get("librarySectionTitle") or ("Movies" if chosen.get("type") == "movie" else "TV Shows"),
        "viewOffset": int(chosen.get("viewOffset") or 0),
    }


def build_plex_android_deep_link(rating_key: str, machine_id: str) -> str:
    """Build the native Android TV `plex://play/` deep-link URI for `com.plexapp.android`."""
    meta_key = urllib.parse.quote(f"/library/metadata/{rating_key}", safe="")
    return f"plex://play/?metadataKey={meta_key}&server={machine_id}"


def play_on_living_room_tv(
    query: str,
    media_type: str = "auto",
    season: Optional[int] = None,
    episode: Optional[int] = None,
    cold_start_delay: float = 10.0,
) -> dict[str, Any]:
    """Wake the Living Room Google TV Streamer, open the native Plex app, and start playback."""
    state = _load_state()
    plex_token = _resolve_plex_token(state)
    _save_state(state)
    plex_url = f"http://{nas_config.HOST_1_IP}:32400"

    machine_id = get_plex_server_info(plex_url, plex_token)
    item = resolve_plex_media(
        query=query,
        plex_url=plex_url,
        plex_token=plex_token,
        media_type=media_type,
        season=season,
        episode=episode,
    )
    deep_link = build_plex_android_deep_link(item["ratingKey"], machine_id)

    ha_url, ha_token = load_ha_config()
    ha_headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}

    def _get_ha_state(eid: str) -> dict[str, Any]:
        try:
            r = requests.get(f"{ha_url}/api/states/{eid}", headers=ha_headers, timeout=5)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    def _get_active_plex_session() -> Optional[dict[str, Any]]:
        try:
            r = requests.get(
                f"{plex_url}/status/sessions",
                params={"X-Plex-Token": plex_token},
                headers={"Accept": "application/json"},
                timeout=6,
            )
            for s in r.json().get("MediaContainer", {}).get("Metadata", []):
                player_title = (s.get("Player") or {}).get("title", "")
                if str(s.get("ratingKey")) == item["ratingKey"] or "living room" in player_title.lower():
                    return s
        except Exception:
            pass
        return None

    # 1. Check if the TV is cold (off/standby) and wake remote.living_room_tv
    rem_st = _get_ha_state(DEFAULT_REMOTE_ENTITY)
    mp_st = _get_ha_state(DEFAULT_ANDROID_TV_ENTITY)
    is_cold = (
        rem_st.get("state") != "on"
        or mp_st.get("state") in ("off", "standby", "unavailable", None)
    )

    if is_cold:
        requests.post(
            f"{ha_url}/api/services/remote/turn_on",
            headers=ha_headers,
            json={"entity_id": DEFAULT_REMOTE_ENTITY},
            timeout=8,
        )
        time.sleep(cold_start_delay)

    # 2. Clear any stale Google Cast receiver (Default Media Receiver / YouTube) that would block native Plex
    cast_st = _get_ha_state(DEFAULT_CAST_ENTITY)
    if (
        cast_st.get("state") in ("playing", "paused", "idle")
        and (cast_st.get("attributes") or {}).get("app_id") not in (None, "AndroidNativeApp")
    ):
        requests.post(
            f"{ha_url}/api/services/media_player/turn_off",
            headers=ha_headers,
            json={"entity_id": DEFAULT_CAST_ENTITY},
            timeout=5,
        )
        time.sleep(1.2)

    # 3. Dispatch native Android TV deep link (`plex://play/?metadataKey=...&server=...`)
    # This opens the native Plex for Android TV app directly on the item's playback/detail screen.
    requests.post(
        f"{ha_url}/api/services/media_player/play_media",
        headers=ha_headers,
        json={
            "entity_id": DEFAULT_ANDROID_TV_ENTITY,
            "media_content_type": "url",
            "media_content_id": deep_link,
        },
        timeout=10,
    )
    time.sleep(3.5)

    # Verify Plex is foregrounded; if cold boot launcher stole focus, re-fire once
    cur_app = (_get_ha_state(DEFAULT_REMOTE_ENTITY).get("attributes") or {}).get("current_activity", "")
    if "plex" not in str(cur_app).lower():
        requests.post(
            f"{ha_url}/api/services/media_player/play_media",
            headers=ha_headers,
            json={
                "entity_id": DEFAULT_ANDROID_TV_ENTITY,
                "media_content_type": "url",
                "media_content_id": deep_link,
            },
            timeout=10,
        )
        time.sleep(3.5)

    # 4. Check if playback already started; if Plex landed on the profile picker or detail Play button,
    # send DPAD_CENTER (and if HA Plex entity came online, also trigger direct play_media if needed).
    sess = _get_active_plex_session()
    if not sess:
        plex_ent_st = _get_ha_state(DEFAULT_PLEX_ENTITY).get("state")
        if plex_ent_st not in ("unavailable", "unknown", None):
            payload_dict: dict[str, Any] = {"library_name": item["librarySectionTitle"]}
            if item["type"] == "movie":
                payload_dict["title"] = item["title"]
            else:
                payload_dict["show.title"] = item.get("show_title") or item["title"]
                if item.get("season") is not None:
                    payload_dict["season.index"] = int(item["season"])
                if item.get("episode") is not None:
                    payload_dict["episode.index"] = int(item["episode"])
            requests.post(
                f"{ha_url}/api/services/media_player/play_media",
                headers=ha_headers,
                json={
                    "entity_id": DEFAULT_PLEX_ENTITY,
                    "media_content_type": "movie" if item["type"] == "movie" else "episode",
                    "media_content_id": json.dumps(payload_dict),
                },
                timeout=8,
            )
            time.sleep(2.5)
            sess = _get_active_plex_session()

    if not sess:
        # Press DPAD_CENTER to confirm profile or click the highlighted Play/Resume button on the movie page
        requests.post(
            f"{ha_url}/api/services/remote/send_command",
            headers=ha_headers,
            json={"entity_id": DEFAULT_REMOTE_ENTITY, "command": "DPAD_CENTER"},
            timeout=6,
        )
        time.sleep(3.0)
        sess = _get_active_plex_session()

    if not sess:
        # If first DPAD_CENTER logged past the profile prompt to the home screen, re-send deep link + DPAD_CENTER once
        requests.post(
            f"{ha_url}/api/services/media_player/play_media",
            headers=ha_headers,
            json={
                "entity_id": DEFAULT_ANDROID_TV_ENTITY,
                "media_content_type": "url",
                "media_content_id": deep_link,
            },
            timeout=8,
        )
        time.sleep(2.5)
        requests.post(
            f"{ha_url}/api/services/remote/send_command",
            headers=ha_headers,
            json={"entity_id": DEFAULT_REMOTE_ENTITY, "command": "DPAD_CENTER"},
            timeout=6,
        )
        time.sleep(2.5)
        sess = _get_active_plex_session()

    cur_app_final = (_get_ha_state(DEFAULT_REMOTE_ENTITY).get("attributes") or {}).get("current_activity", "")
    return {
        "ok": True,
        "playing_verified": sess is not None,
        "plex_app_foreground": "plex" in str(cur_app_final).lower(),
        "cold_start": is_cold,
        "item": item,
        "deep_link": deep_link,
        "session_state": (sess.get("Player") or {}).get("state") if sess else None,
        "player_title": (sess.get("Player") or {}).get("title") if sess else "Living Room TV",
    }


def main():
    parser = argparse.ArgumentParser(description="Play a movie or TV show directly in the native Plex app on the Living Room TV")
    parser.add_argument("query", help="Movie or TV show title to search and play")
    parser.add_argument("--type", choices=["auto", "movie", "show", "episode"], default="auto", help="Media type filter")
    parser.add_argument("--season", "-s", type=int, default=None, help="Season number (for TV shows)")
    parser.add_argument("--episode", "-e", type=int, default=None, help="Episode number (for TV shows)")
    parser.add_argument("--resolve-only", action="store_true", help="Only resolve metadata and deep link without touching the TV")
    args = parser.parse_args()

    if args.resolve_only:
        state = _load_state()
        plex_token = _resolve_plex_token(state)
        _save_state(state)
        plex_url = f"http://{nas_config.HOST_1_IP}:32400"
        machine_id = get_plex_server_info(plex_url, plex_token)
        item = resolve_plex_media(
            query=args.query,
            plex_url=plex_url,
            plex_token=plex_token,
            media_type=args.type,
            season=args.season,
            episode=args.episode,
        )
        print(json.dumps({
            "ok": True,
            "item": item,
            "deep_link": build_plex_android_deep_link(item["ratingKey"], machine_id),
        }, indent=2))
        return

    res = play_on_living_room_tv(
        query=args.query,
        media_type=args.type,
        season=args.season,
        episode=args.episode,
    )
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
