#!/usr/bin/env python3
"""Unified NFL & MLB Live Stream Extractor with Local HLS Sanitizer Relay.

Extracts direct live HLS (.m3u8) streams from nfl24all.ir or mlb24all.ir,
optionally registers them with the local HLS sanitizer relay to strip SSAI
tracking beacons (preventing VLC for Android TV crashes), and casts to
Home Assistant Android TV devices via ``tools.mlb_stream_extractor.cast_to_home_assistant``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
for _p in (str(REPO_ROOT), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.mlb_stream_extractor import cast_to_home_assistant
except ImportError:  # pragma: no cover - direct execution fallback
    from mlb_stream_extractor import cast_to_home_assistant  # type: ignore[no-redef]


SPORT_CONFIGS: dict[str, dict[str, str]] = {
    "nfl": {
        "base_url": "https://nfl24all.ir",
        "api_base_url": "https://api.nfl24all.ir",
        "default_flavor": "free.live.nfl",
        "default_team": "bears",
    },
    "mlb": {
        "base_url": "https://mlb24all.ir",
        "api_base_url": "https://api.mlb24all.ir",
        "default_flavor": "free.live.sportsnet",
        "default_team": "cubs",
    },
}

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_STATUS_PRIORITY = {"L": 0, "W": 1, "P": 2, "S": 3, "F": 99}
_SPANISH_KEYWORDS = (
    "español",
    "espanol",
    "spanish",
    "telemundo",
    "universo",
    "espn deportes",
    "fox deportes",
    "(es)",
    " sap",
)
SECRETS_ENV_PATH = Path("/secrets/env.json")


def get_sport_config(sport: str) -> dict[str, str]:
    key = (sport or "nfl").strip().lower()
    if key not in SPORT_CONFIGS:
        raise ValueError(f"Unsupported sport '{sport}'. Expected one of: {', '.join(SPORT_CONFIGS)}")
    return SPORT_CONFIGS[key]


def build_headers(sport: str) -> dict[str, str]:
    cfg = get_sport_config(sport)
    return {
        "User-Agent": _USER_AGENT,
        "Referer": f"{cfg['base_url']}/",
        "Origin": cfg["base_url"],
    }


def fetch_stateshot(sport: str = "nfl") -> dict[str, Any]:
    """Fetch and parse stateshot JSON for the requested sport."""
    cfg = get_sport_config(sport)
    headers = build_headers(sport)

    try:
        req = urllib.request.Request(cfg["base_url"], headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            match = re.search(r"var stateshot = (\{.*?\});", html, re.DOTALL)
            if match:
                return json.loads(match.group(1))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"Homepage stateshot fetch failed ({cfg['base_url']}): {exc}\n")

    try:
        req = urllib.request.Request(f"{cfg['api_base_url']}/api/v4/stateshot", headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"Direct API stateshot fetch failed ({cfg['api_base_url']}): {exc}\n")

    raise RuntimeError(f"Failed to fetch stateshot data from {cfg['base_url']}")


def resolve_team_game(data: dict[str, Any], team_query: str) -> tuple[dict[str, Any], dict[Any, str], Any]:
    """Find the highest-priority active/upcoming game for the requested team."""
    teams = {t["id"]: t["name"] for t in data.get("teams", [])}
    games = data.get("games", [])

    if team_query.lower() in ("any", "live", "postseason", "playoffs", "super_bowl", "world_series", "ws") and games:
        sorted_games = sorted(
            games,
            key=lambda g: (
                _STATUS_PRIORITY.get(g.get("status"), 50),
                -int(g.get("id") or 0),
            ),
        )
        chosen_game = sorted_games[0]
        target_team_id = chosen_game.get("home_team_id") or chosen_game.get("away_team_id")
        return chosen_game, teams, target_team_id

    team_matches = [
        (tid, name)
        for tid, name in teams.items()
        if team_query.lower() in name.lower()
    ]
    if not team_matches:
        raise ValueError(f"No team matching '{team_query}' found in schedule.")

    target_team_id, target_team_name = team_matches[0]
    matching_games = [
        g for g in games
        if target_team_id in (g.get("away_team_id"), g.get("home_team_id"))
    ]
    if not matching_games:
        raise ValueError(f"No games scheduled for {target_team_name}.")

    matching_games.sort(key=lambda g: _STATUS_PRIORITY.get(g.get("status"), 50))
    return matching_games[0], teams, target_team_id


def _find_matching_media_events(data: dict[str, Any], game: dict[str, Any]) -> list[dict[str, Any]]:
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

    unbound: list[dict[str, Any]] = []
    for me in data.get("media_events", []):
        if me.get("game_id") is not None:
            continue
        blob = f"{me.get('title', '')} {me.get('description', '')}".lower()
        if any(tok in blob for tok in tokens) or me.get("status") == "L":
            unbound.append(me)
    return unbound


def _is_spanish_event(me: dict[str, Any]) -> bool:
    blob = " ".join(
        str(me.get(k) or "")
        for k in ("title", "description", "language", "lang")
    ).lower()
    return any(kw in blob for kw in _SPANISH_KEYWORDS)


def resolve_media_event(
    data: dict[str, Any],
    game: dict[str, Any],
    target_team_id: Any,
    target_team_name: str | None = None,
    feed_override: str | None = None,
    sport: str = "nfl",
) -> dict[str, Any]:
    """Select the best broadcast media event, preferring live English/National feeds for NFL."""
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

    override_norm = feed_override.strip().lower() if feed_override else None
    wants_spanish = override_norm in ("espanol", "español", "spanish")

    if wants_spanish:
        spanish_pool = [me for me in media_events if _is_spanish_event(me)]
        pool = spanish_pool or media_events
    else:
        english_pool = [me for me in media_events if not _is_spanish_event(me)]
        pool = english_pool or media_events

    is_away = game.get("away_team_id") == target_team_id
    team_side = "away" if is_away else "home"

    if override_norm in ("home", "away", "national"):
        for me in pool:
            if me.get("title", "").strip().lower() == override_norm and me.get("status") == "L":
                return me
        for me in pool:
            if me.get("title", "").strip().lower() == override_norm:
                return me

    if sport.lower() == "nfl" and override_norm in (None, "auto"):
        # NFL priority: live National > live team side / description > any live English > National > first
        for me in pool:
            if me.get("title", "").strip().lower() == "national" and me.get("status") == "L":
                return me
        for me in pool:
            if me.get("title", "").strip().lower() == team_side and me.get("status") == "L":
                return me
        if target_team_name:
            for me in pool:
                if target_team_name.lower() in me.get("description", "").lower() and me.get("status") == "L":
                    return me
        for me in pool:
            if me.get("status") == "L":
                return me
        for me in pool:
            if me.get("title", "").strip().lower() == "national":
                return me
        return pool[0]

    # MLB / general priority: live preferred side > preferred side > team description > any live
    for me in pool:
        if me.get("title", "").strip().lower() == team_side and me.get("status") == "L":
            return me
    for me in pool:
        if me.get("title", "").strip().lower() == team_side:
            return me
    if target_team_name:
        for me in pool:
            if target_team_name.lower() in me.get("description", "").lower() and me.get("status") == "L":
                return me
    for me in pool:
        if me.get("status") == "L":
            return me
    return pool[0]


def resolve_stream_url(media_event_id: int, flavor_id: str, sport: str = "nfl") -> str:
    """Call generate_stream_info API to resolve the upstream master HLS .m3u8 URL."""
    cfg = get_sport_config(sport)
    api_url = f"{cfg['api_base_url']}/api/v2/generate_stream_info"
    payload = json.dumps({
        "media_event_id": media_event_id,
        "flavor_id": flavor_id,
    }).encode("utf-8")

    headers = build_headers(sport)
    headers["Content-Type"] = "application/json"

    req = urllib.request.Request(api_url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        if exc.code == 406:
            raise RuntimeError(
                f"Stream relay not yet active or flavor '{flavor_id}' mismatch "
                f"for media event {media_event_id} ({body.strip()})."
            ) from exc
        raise RuntimeError(f"HTTP {exc.code} from stream generator: {body.strip()}") from exc

    stream_url = res.get("url")
    if not stream_url:
        raise RuntimeError(f"API response did not contain stream URL: {res}")
    return stream_url


def _resolve_relay_token() -> str:
    tok = os.environ.get("HLS_RELAY_TOKEN", "").strip()
    if tok:
        return tok
    try:
        if SECRETS_ENV_PATH.exists():
            data = json.loads(SECRETS_ENV_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return str(data.get("HLS_RELAY_TOKEN") or "").strip()
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def register_with_relay(
    upstream_url: str,
    referer: str,
    relay_host: str | None = None,
    relay_port: int = 8844,
    mode: str = "hls",
) -> dict[str, Any] | None:
    """Register upstream stream with the local HLS relay; returns None with stderr notice on failure."""
    control_host = (relay_host or "").strip() or "127.0.0.1"
    endpoint = f"http://{control_host}:{relay_port}/register"
    payload = json.dumps({
        "upstream_url": upstream_url,
        "referer": referer,
        "mode": mode,
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    token = _resolve_relay_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(
            f"Notice: Local HLS relay at {endpoint} is unreachable ({exc}); "
            "falling back to direct upstream URL.\n"
        )
        return None


def _select_candidate_flavors(
    data: dict[str, Any],
    media_event: dict[str, Any],
    default_flavor: str,
) -> list[str]:
    me_id = media_event.get("id")
    flavors = data.get("flavors", [])
    cand_flavors: list[str] = []

    for fl in flavors:
        if me_id in fl.get("media_event_ids", []) and "free" in fl.get("id", "").lower():
            cand_flavors.append(fl["id"])
    for fl in flavors:
        if me_id in fl.get("media_event_ids", []) and fl.get("id") not in cand_flavors:
            cand_flavors.append(fl["id"])
    if media_event.get("flavor_id") and media_event["flavor_id"] not in cand_flavors:
        cand_flavors.append(media_event["flavor_id"])
    for fl_id in media_event.get("flavor_ids", []) or []:
        if fl_id not in cand_flavors:
            cand_flavors.append(fl_id)
    if default_flavor not in cand_flavors:
        cand_flavors.append(default_flavor)
    return cand_flavors


def extract_stream(
    sport: str = "nfl",
    team: str | None = None,
    feed: str = "auto",
    use_relay: bool | None = None,
    relay_mode: str = "hls",
    relay_host: str | None = None,
    relay_port: int = 8844,
    cast: bool = False,
    entity: str = "media_player.living_room_tv",
) -> dict[str, Any]:
    """Resolve live game stream, optionally wrap with local HLS sanitizer relay, and cast."""
    cfg = get_sport_config(sport)
    team_query = team or cfg["default_team"]
    relay_enabled = (sport.lower() == "nfl") if use_relay is None else use_relay

    data = fetch_stateshot(sport)
    game, teams, target_team_id = resolve_team_game(data, team_query)
    target_team_name = teams.get(target_team_id)
    feed_pref = None if feed == "auto" else feed

    media_event = resolve_media_event(
        data,
        game,
        target_team_id,
        target_team_name=target_team_name,
        feed_override=feed_pref,
        sport=sport,
    )

    game_media_events = _find_matching_media_events(data, game)
    ordered_events = [media_event] + [
        me for me in game_media_events if me.get("id") != media_event.get("id")
    ]
    # Keep non-Spanish preference when falling back across events unless Spanish was requested
    if feed_pref not in ("espanol", "español", "spanish"):
        non_es = [me for me in ordered_events if not _is_spanish_event(me)]
        es = [me for me in ordered_events if _is_spanish_event(me)]
        ordered_events = non_es + es

    chosen_flavor: str | None = None
    upstream_url: str | None = None
    last_err: Exception | None = None

    for cand_me in ordered_events:
        me_id = cand_me.get("id")
        if not me_id:
            continue
        for fl_id in _select_candidate_flavors(data, cand_me, cfg["default_flavor"]):
            try:
                upstream_url = resolve_stream_url(me_id, fl_id, sport=sport)
                if upstream_url:
                    media_event = cand_me
                    chosen_flavor = fl_id
                    break
            except (RuntimeError, OSError, urllib.error.URLError, ValueError) as exc:
                last_err = exc
        if upstream_url:
            break

    if not upstream_url and media_event.get("id"):
        fallback_flavors = [
            fl["id"] for fl in data.get("flavors", []) if "free" in fl.get("id", "").lower()
        ] or [cfg["default_flavor"]]
        for fl_id in fallback_flavors:
            try:
                upstream_url = resolve_stream_url(media_event["id"], fl_id, sport=sport)
                if upstream_url:
                    chosen_flavor = fl_id
                    break
            except (RuntimeError, OSError, urllib.error.URLError, ValueError) as exc:
                last_err = exc

    if not chosen_flavor:
        chosen_flavor = cfg["default_flavor"]

    if not upstream_url and last_err and game.get("status") not in ("W", "P", "S") and media_event.get("status") != "P":
        raise last_err

    final_url = upstream_url
    relay_info: dict[str, Any] | None = None
    if upstream_url and relay_enabled:
        relay_info = register_with_relay(
            upstream_url=upstream_url,
            referer=f"{cfg['base_url']}/",
            relay_host=relay_host,
            relay_port=relay_port,
            mode=relay_mode,
        )
        if relay_info:
            final_url = relay_info.get("ts_url") if relay_mode == "ts" else relay_info.get("hls_url")

    result: dict[str, Any] = {
        "sport": sport.lower(),
        "team": target_team_name,
        "game_id": game.get("id"),
        "status": game.get("status"),
        "matchup": f"{teams.get(game.get('away_team_id'))} @ {teams.get(game.get('home_team_id'))}",
        "scores": game.get("scores"),
        "broadcast": media_event.get("title"),
        "description": (media_event.get("description") or "").strip(),
        "flavor": chosen_flavor,
        "stream_url": final_url,
        "upstream_url": upstream_url,
        "relayed": bool(relay_info),
    }
    if relay_info:
        result["relay_mode"] = relay_mode
        result["stream_id"] = relay_info.get("stream_id")

    if not final_url:
        result["stream_ready"] = False
        result["notice"] = (
            f"{result['matchup']} is in Warmup/Pre-game (status: {result['status']}). "
            "The live stream relay opens right at game time."
        )
    elif cast:
        result["casted"] = cast_to_home_assistant(final_url, entity)
        result["target_entity"] = entity

    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified NFL/MLB live stream extractor with local HLS sanitizer relay"
    )
    parser.add_argument("--sport", choices=["nfl", "mlb"], default="nfl", help="Sport to query (default: nfl)")
    parser.add_argument("--team", default=None, help="Team name to look for (default: bears for nfl, cubs for mlb)")
    parser.add_argument(
        "--feed",
        choices=["auto", "home", "away", "national"],
        default="auto",
        help="Broadcast feed preference (default: auto)",
    )
    parser.add_argument("--cast", action="store_true", help="Automatically cast resolved stream to Home Assistant")
    parser.add_argument(
        "--relay",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Route stream through local HLS sanitizer relay (default: enabled for nfl, disabled for mlb)",
    )
    parser.add_argument(
        "--relay-mode",
        choices=["hls", "ts"],
        default="hls",
        help="Relay playback mode: sanitized HLS playlist or MPEG-TS remux (default: hls)",
    )
    parser.add_argument("--relay-host", default=None, help="Relay control host (default: 127.0.0.1)")
    parser.add_argument("--relay-port", type=int, default=8844, help="Relay port (default: 8844)")
    parser.add_argument("--entity", default="media_player.living_room_tv", help="Target HA media player entity")
    parser.add_argument("--url-only", action="store_true", help="Print only the resolved stream URL")
    parser.add_argument("--json", action="store_true", dest="output_json", help="Output full JSON metadata")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    result = extract_stream(
        sport=args.sport,
        team=args.team,
        feed=args.feed,
        use_relay=args.relay,
        relay_mode=args.relay_mode,
        relay_host=args.relay_host,
        relay_port=args.relay_port,
        cast=args.cast,
        entity=args.entity,
    )

    if args.url_only:
        print(result.get("stream_url") or "")
    elif args.output_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Sport: {result['sport'].upper()}")
        print(f"Game: {result['matchup']} ({result['status']})")
        print(f"Broadcast: {result['broadcast']} ({result['flavor']})")
        if result.get("stream_url"):
            label = "Relayed Stream" if result.get("relayed") else "Stream"
            print(f"{label}: {result['stream_url']}")
        elif result.get("notice"):
            print(f"Notice: {result['notice']}")
        if args.cast and result.get("stream_url"):
            status_str = "SUCCESS" if result.get("casted") else "FAILED"
            print(f"Casting to {args.entity}: {status_str}")


if __name__ == "__main__":
    main()
