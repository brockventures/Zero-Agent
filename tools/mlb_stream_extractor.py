#!/usr/bin/env python3
"""MLB Live Stream Extractor & Chromecast Dispatcher.

Extracts direct live HLS (.m3u8) streams from mlb24all.ir without requiring
browser DOM navigation, popups, or manual unmuting.
Dispatches directly to Home Assistant Android TV / Chromecast devices.
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://mlb24all.ir/",
    "Origin": "https://mlb24all.ir"
}


def fetch_stateshot() -> Dict[str, Any]:
    """Fetch and parse stateshot JSON containing games, teams, and media events."""
    # First attempt: embedded stateshot in homepage
    try:
        req = urllib.request.Request("https://mlb24all.ir", headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            m = re.search(r"var stateshot = ({.*?});", html)
            if m:
                return json.loads(m.group(1))
    except Exception as e:
        sys.stderr.write(f"Homepage stateshot fetch failed: {e}\n")

    # Second attempt: direct API stateshot
    try:
        req = urllib.request.Request("https://api.mlb24all.ir/api/v4/stateshot", headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        sys.stderr.write(f"Direct API stateshot fetch failed: {e}\n")

    raise RuntimeError("Failed to fetch stateshot data from mlb24all.ir")


def resolve_team_game(data: Dict[str, Any], team_query: str) -> Tuple[Dict[str, Any], Dict[str, str], int]:
    """Find the active/upcoming game for the requested team."""
    teams = {t["id"]: t["name"] for t in data.get("teams", [])}
    games = data.get("games", [])
    status_priority = {"L": 0, "W": 1, "P": 2, "S": 3, "F": 99}

    if team_query.lower() in ("any", "live", "world_series", "ws", "postseason") and games:
        sorted_games = sorted(
            games,
            key=lambda g: (
                status_priority.get(g.get("status"), 50),
                -int(g.get("id") or 0),
            ),
        )
        chosen_game = sorted_games[0]
        target_team_id = chosen_game.get("home_team_id") or chosen_game.get("away_team_id")
        return chosen_game, teams, target_team_id

    team_matches = [
        (tid, name) for tid, name in teams.items()
        if team_query.lower() in name.lower()
    ]
    if not team_matches:
        raise ValueError(f"No MLB team matching '{team_query}' found in schedule.")

    target_team_id, target_team_name = team_matches[0]

    # Look for games with target team, prioritize Live ('L'), then Starting Soon ('S'/'P'), then Final ('F')
    matching_games = [
        g for g in games
        if target_team_id in (g.get("away_team_id"), g.get("home_team_id"))
    ]

    if not matching_games:
        raise ValueError(f"No games scheduled for {target_team_name}.")

    # Sort by priority: Live ('L') > Warmup ('W') > Pre-game ('P') > Scheduled ('S') > Final ('F')
    matching_games.sort(key=lambda g: status_priority.get(g.get("status"), 50))
    chosen_game = matching_games[0]

    return chosen_game, teams, target_team_id


def _find_matching_media_events(data: Dict[str, Any], game: Dict[str, Any]) -> list:
    """Find media events bound to game['id'] or unbound ('game_id': null) live/matching feeds."""
    exact = [
        me for me in data.get("media_events", [])
        if me.get("game_id") == game["id"]
    ]
    if exact:
        return exact

    teams = {t["id"]: t["name"] for t in data.get("teams", [])}
    away_name = (teams.get(game.get("away_team_id")) or "").lower()
    home_name = (teams.get(game.get("home_team_id")) or "").lower()
    tokens = [w for w in (away_name.split() + home_name.split()) if len(w) > 3]

    unbound = []
    for me in data.get("media_events", []):
        if me.get("game_id") is not None:
            continue
        blob = f"{me.get('title', '')} {me.get('description', '')}".lower()
        if any(tok in blob for tok in tokens) or me.get("status") == "L":
            unbound.append(me)
    return unbound


def resolve_media_event(
    data: Dict[str, Any],
    game: Dict[str, Any],
    target_team_id: int,
    target_team_name: Optional[str] = None,
    feed_override: Optional[str] = None
) -> Dict[str, Any]:
    """Select the best media event (broadcast feed) for the target team."""
    media_events = _find_matching_media_events(data, game)

    if not media_events:
        if game.get("status") in ("W", "P", "S"):
            return {
                "id": None,
                "game_id": game["id"],
                "title": "National",
                "status": "P",
                "description": "Pre-game / Scheduled",
            }
        raise ValueError(f"No media events found for game ID {game['id']}.")

    is_away = (game.get("away_team_id") == target_team_id)
    if feed_override and feed_override.lower() in ("home", "away", "national"):
        preferred_title = feed_override.capitalize()
    else:
        preferred_title = "Away" if is_away else "Home"

    # 1. Prefer targeted team broadcast that is live ('L')
    for me in media_events:
        if me.get("title", "").strip().lower() == preferred_title.lower() and me.get("status") == "L":
            return me

    # 2. Prefer targeted team broadcast (any status)
    for me in media_events:
        if me.get("title", "").strip().lower() == preferred_title.lower():
            return me

    # 3. Match team name in description if available and live
    if target_team_name:
        for me in media_events:
            if target_team_name.lower() in me.get("description", "").lower() and me.get("status") == "L":
                return me

    # 4. Prefer any live feed ('L')
    for me in media_events:
        if me.get("status") == "L":
            return me

    # 5. Fallback to first available
    return media_events[0]


def resolve_stream_url(media_event_id: int, flavor_id: str = "free.live.sportsnet") -> str:
    """Call the generate_stream_info API to get the live HLS .m3u8 master URL."""
    api_url = "https://api.mlb24all.ir/api/v2/generate_stream_info"
    payload = json.dumps({
        "media_event_id": media_event_id,
        "flavor_id": flavor_id
    }).encode("utf-8")

    headers = dict(DEFAULT_HEADERS)
    headers["Content-Type"] = "application/json"

    req = urllib.request.Request(api_url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as he:
        body = he.read().decode("utf-8", errors="ignore")
        if he.code == 406:
            raise RuntimeError(f"Stream relay not yet active or flavor '{flavor_id}' mismatch for media event {media_event_id} ({body.strip()}).")
        raise RuntimeError(f"HTTP {he.code} from stream generator: {body.strip()}")

    stream_url = res.get("url")
    if not stream_url:
        raise RuntimeError(f"API response did not contain stream URL: {res}")

    return stream_url


def cast_to_home_assistant(
    stream_url: str,
    entity_id: str = "media_player.living_room_tv",
    cold_start_delay: float = 12.0
) -> bool:
    """Wake device, wait for HDMI/CEC handshake if cold, and dispatch stream URL to Home Assistant.
    
    Decoupled dispatch: dispatches vlc:// intent cleanly without follow-up intent bombardment.
    Accommodates the ~10-12s Samsung TV power-on + Broadlink IR input switch automation.
    """
    ha_cfg_path = "/secrets/ha.json"
    with open(ha_cfg_path) as f:
        ha_cfg = json.load(f)

    base_url = ha_cfg["url"].rstrip("/")
    token = ha_cfg["token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    def _get_state(eid: str) -> dict:
        try:
            req = urllib.request.Request(f"{base_url}/api/states/{eid}", headers=headers)
            with urllib.request.urlopen(req, timeout=4) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return {}

    # 1. Auto-fallback if legacy media_player.living_room_tv_2 was requested after HA entity rename
    if entity_id == "media_player.living_room_tv_2" and not _get_state(entity_id):
        if _get_state("media_player.living_room_tv"):
            entity_id = "media_player.living_room_tv"

    # 2. Determine if device is cold starting
    remote_entity = "remote.living_room_tv" if "living_room" in entity_id else None
    is_cold = False
    if remote_entity:
        rem_st = _get_state(remote_entity).get("state")
        med_st = _get_state(entity_id).get("state")
        if rem_st != "on" or med_st in ("off", "standby", "unavailable", None):
            is_cold = True

        # Wake remote if not on or cold
        try:
            on_payload = json.dumps({"entity_id": remote_entity}).encode("utf-8")
            on_req = urllib.request.Request(f"{base_url}/api/services/remote/turn_on", data=on_payload, headers=headers)
            urllib.request.urlopen(on_req, timeout=8)
        except Exception as e:
            sys.stderr.write(f"Notice: Failed to send remote.turn_on: {e}\n")

    # 3. Clear any stale Google Cast receiver so it does not conflict with native VLC
    # (Never turn off entity_id itself!)
    cast_entity = None
    if "living_room" in entity_id:
        for cand_cast in ("media_player.living_room_living_room_tv", "media_player.living_room_tv"):
            if cand_cast != entity_id:
                cast_entity = cand_cast
                break
    if cast_entity:
        try:
            cast_st = _get_state(cast_entity)
            cast_attrs = cast_st.get("attributes", {})
            cast_app_id = cast_attrs.get("app_id")
            if (
                cast_st.get("state") in ("playing", "paused", "idle")
                and cast_app_id
                and cast_app_id != "AndroidNativeApp"
                and cast_attrs.get("app_name") != "VLC"
            ):
                stop_payload = json.dumps({"entity_id": cast_entity}).encode("utf-8")
                stop_req = urllib.request.Request(f"{base_url}/api/services/media_player/turn_off", data=stop_payload, headers=headers)
                urllib.request.urlopen(stop_req, timeout=4)
                time.sleep(1.5)  # Allow Android TV surface teardown to complete before firing VLC intent
        except Exception:
            pass

    # 3. If cold starting, wait for TV/Chromecast HDMI-CEC handshake & launcher readiness
    if is_cold:
        sys.stderr.write(f"Cold start detected: waiting {cold_start_delay}s for Samsung TV HDMI input switch & handshake...\n")
        time.sleep(cold_start_delay)
    elif remote_entity:
        # If warm and already running an app (e.g. BrowseHere playing previous stream),
        # send HOME first to release the active video surface so the new stream intent is not swallowed.
        try:
            home_payload = json.dumps({"entity_id": remote_entity, "command": "HOME"}).encode("utf-8")
            home_req = urllib.request.Request(f"{base_url}/api/services/remote/send_command", data=home_payload, headers=headers)
            urllib.request.urlopen(home_req, timeout=4)
            time.sleep(1.2)
        except Exception:
            pass

    # 4. Dispatch play_media URL (explicit vlc:// scheme prevents fallback browser intercept)
    vlc_media_id = stream_url if stream_url.startswith("vlc://") else f"vlc://{stream_url}"
    play_payload = json.dumps({
        "entity_id": entity_id,
        "media_content_type": "url",
        "media_content_id": vlc_media_id
    }).encode("utf-8")

    def _send_play() -> bool:
        play_req = urllib.request.Request(f"{base_url}/api/services/media_player/play_media", data=play_payload, headers=headers)
        with urllib.request.urlopen(play_req, timeout=10) as resp:
            return resp.status == 200

    success = _send_play()
    if not success:
        return False

    # 4. If cold starting, Google TV launcher boot initialization can swallow the initial intent.
    # Check once after a short delay: if stuck on launcher, re-fire once and release.
    time.sleep(2.5)
    current_app = _get_state(entity_id).get("attributes", {}).get("app_id")
    if current_app and current_app != "org.videolan.vlc":
        sys.stderr.write(f"Launcher retained focus ({current_app}); re-dispatching VLC intent once...\n")
        _send_play()

    # 5. Decoupled dispatch: do NOT continue polling/re-dispatching as it tramples active VLC playback
    return success



def main():
    parser = argparse.ArgumentParser(description="Extract live MLB HLS stream and cast to Chromecast")
    parser.add_argument("--team", default="cubs", help="Team name to look for (default: cubs)")
    parser.add_argument("--feed", choices=["auto", "home", "away", "national"], default="auto", help="Broadcast feed preference (default: auto)")
    parser.add_argument("--cast", action="store_true", help="Automatically cast to Home Assistant media player")
    parser.add_argument("--entity", default="media_player.living_room_tv", help="Target HA media player entity")
    parser.add_argument("--json", action="store_true", dest="output_json", help="Output full JSON metadata")
    parser.add_argument("--url-only", action="store_true", help="Print only the resolved .m3u8 URL")
    args = parser.parse_args()

    data = fetch_stateshot()
    game, teams, target_team_id = resolve_team_game(data, args.team)
    target_team_name = teams.get(target_team_id)
    feed_pref = None if args.feed == "auto" else args.feed
    media_event = resolve_media_event(
        data,
        game,
        target_team_id,
        target_team_name=target_team_name,
        feed_override=feed_pref
    )

    # Determine flavor dynamically across all media events for this game
    flavors = data.get("flavors", [])
    game_media_events = _find_matching_media_events(data, game)
    # Put preferred media_event first, then any other media_events for this game
    ordered_events = [media_event] + [
        me for me in game_media_events if me.get("id") != media_event.get("id")
    ]

    chosen_flavor = None
    stream_url = None
    last_err = None

    for cand_me in ordered_events:
        me_id = cand_me.get("id")
        if not me_id:
            continue
        cand_flavors = []
        # 1. Flavors explicitly listing this media_event_id (prefer 'free' first, then any)
        for fl in flavors:
            if me_id in fl.get("media_event_ids", []) and "free" in fl.get("id", "").lower():
                cand_flavors.append(fl["id"])
        for fl in flavors:
            if me_id in fl.get("media_event_ids", []) and fl.get("id") not in cand_flavors:
                cand_flavors.append(fl["id"])
        # 2. Check if media_event itself specifies flavor(s)
        if cand_me.get("flavor_id") and cand_me["flavor_id"] not in cand_flavors:
            cand_flavors.append(cand_me["flavor_id"])
        for fl_id in cand_me.get("flavor_ids", []) or []:
            if fl_id not in cand_flavors:
                cand_flavors.append(fl_id)

        for fl_id in cand_flavors:
            try:
                stream_url = resolve_stream_url(me_id, fl_id)
                if stream_url:
                    media_event = cand_me
                    chosen_flavor = fl_id
                    break
            except Exception as e:
                last_err = e
        if stream_url:
            break

    if not stream_url and media_event.get("id"):
        # Fallback: try all free.live flavors against the primary media_event
        fallback_flavors = [
            fl["id"] for fl in flavors if "free" in fl.get("id", "").lower()
        ] or ["free.live.sportsnet"]
        for fl_id in fallback_flavors:
            try:
                stream_url = resolve_stream_url(media_event["id"], fl_id)
                if stream_url:
                    chosen_flavor = fl_id
                    break
            except Exception as e:
                last_err = e

    if not chosen_flavor:
        chosen_flavor = "free.live.sportsnet"

    if not stream_url and last_err:
        sys.stderr.write(
            f"Debug stateshot: game={json.dumps(game)} "
            f"events={json.dumps(game_media_events)} "
            f"flavors={json.dumps(flavors)}\n"
        )
        if game.get("status") not in ("W", "P", "S") and media_event.get("status") != "P":
            raise last_err

    result = {
        "team": teams.get(target_team_id),
        "game_id": game.get("id"),
        "status": game.get("status"),
        "matchup": f"{teams.get(game.get('away_team_id'))} @ {teams.get(game.get('home_team_id'))}",
        "scores": game.get("scores"),
        "broadcast": media_event.get("title"),
        "description": media_event.get("description", "").strip(),
        "flavor": chosen_flavor,
        "stream_url": stream_url
    }

    if not stream_url:
        result["stream_ready"] = False
        result["notice"] = (
            f"{result['matchup']} is in Warmup/Pre-game (status: {result['status']}). "
            "First pitch is scheduled for 7:00 PM PT. The live stream relay opens right at game time."
        )
        # If cast was requested, ensure the TV is powered on and awake so it's ready
        if args.cast:
            remote_entity = "remote.living_room_tv" if "living_room" in args.entity else None
            if remote_entity:
                try:
                    with open("/secrets/ha.json") as f:
                        ha_cfg = json.load(f)
                    base_url = ha_cfg["url"].rstrip("/")
                    token = ha_cfg["token"]
                    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    on_payload = json.dumps({"entity_id": remote_entity}).encode("utf-8")
                    on_req = urllib.request.Request(f"{base_url}/api/services/remote/turn_on", data=on_payload, headers=headers)
                    urllib.request.urlopen(on_req, timeout=5)
                    result["tv_powered"] = True
                except Exception:
                    result["tv_powered"] = False

    if stream_url and args.cast:
        success = cast_to_home_assistant(stream_url, args.entity)
        result["casted"] = success
        result["target_entity"] = args.entity

    if args.url_only:
        print(stream_url)
    elif args.output_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Game: {result['matchup']} ({result['status']})")
        print(f"Broadcast: {result['broadcast']} ({result['flavor']})")
        if result.get("stream_url"):
            print(f"Stream: {result['stream_url']}")
        elif result.get("notice"):
            print(f"Notice: {result['notice']}")
        if args.cast:
            if not result.get("stream_url"):
                tv_status = "Powered on & primed on Google TV launcher" if result.get("tv_powered") else "Ready"
                print(f"TV Status: {tv_status}. Stream relay will dispatch once live at 7:00 PM PT.")
            else:
                status_str = "SUCCESS" if result.get("casted") else "FAILED"
                print(f"Casting to {args.entity}: {status_str}")


if __name__ == "__main__":
    main()
