#!/usr/bin/env python3
"""Cubs Game Day Notifier & Stream Dispatcher.

Monitors official MLB Stats API for Chicago Cubs games.
When a game is ~10 minutes from first pitch (window: 0-15m), sends a Discord notification
with one-click [CHOICES: Cast Cubs on TV | Dismiss] buttons.
State is tracked in /workspace/data/cubs_notifier_state.json to prevent duplicate alerts.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT_TZ = ZoneInfo("America/Los_Angeles")
CUBS_TEAM_ID = 112
STATE_FILE = Path("/workspace/data/cubs_notifier_state.json")


def load_state() -> dict:
    """Load notification state tracking notified game PKs."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"notified_games": {}, "last_check": None}


def save_state(state: dict):
    """Save notification state atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = STATE_FILE.with_suffix(".tmp")
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    temp_file.replace(STATE_FILE)


def fetch_cubs_game(date_str: str = None, retries: int = 2) -> list[dict]:
    """Fetch schedule for the Cubs on a specific date (YYYY-MM-DD in PT) with retry backoff."""
    if not date_str:
        now_pt = datetime.now(PT_TZ)
        date_str = now_pt.strftime("%Y-%m-%d")

    url = (
        f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId={CUBS_TEAM_ID}"
        f"&date={date_str}&hydrate=team,broadcasts,venue"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Zero-MLB-Monitor/1.0"})
    
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            games = []
            for d in data.get("dates", []):
                for g in d.get("games", []):
                    games.append(g)
            return games
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5)
    raise last_err or RuntimeError("Failed to fetch Cubs schedule")


def format_game_notification(game: dict, minutes_left: int = 10) -> str:
    """Format rich Discord message with interactive [CHOICES: ...] block."""
    away_team = game.get("teams", {}).get("away", {})
    home_team = game.get("teams", {}).get("home", {})
    away_name = away_team.get("team", {}).get("name", "Away Team")
    home_name = home_team.get("team", {}).get("name", "Home Team")
    away_rec = away_team.get("leagueRecord", {})
    home_rec = home_team.get("leagueRecord", {})
    away_rec_str = f" ({away_rec.get('wins', 0)}-{away_rec.get('losses', 0)})" if away_rec else ""
    home_rec_str = f" ({home_rec.get('wins', 0)}-{home_rec.get('losses', 0)})" if home_rec else ""

    # Parse first pitch time in PT
    game_date_utc = game.get("gameDate")
    if game_date_utc:
        dt_utc = datetime.fromisoformat(game_date_utc.replace("Z", "+00:00"))
        dt_pt = dt_utc.astimezone(PT_TZ)
        pitch_time_str = dt_pt.strftime("%-I:%M %p PT")
    else:
        pitch_time_str = "Soon"

    venue_name = game.get("venue", {}).get("name", "Ballpark")

    # Find broadcast
    is_cubs_home = (home_team.get("team", {}).get("id") == CUBS_TEAM_ID)
    tv_broadcasts = []
    for b in game.get("broadcasts", []):
        if b.get("type") == "TV":
            b_name = b.get("name")
            ha = b.get("homeAway")
            if (is_cubs_home and ha == "home") or (not is_cubs_home and ha == "away"):
                tv_broadcasts.insert(0, f"{b_name} ({ha.capitalize()})")
            else:
                tv_broadcasts.append(f"{b_name} ({ha.capitalize()})")

    broadcast_str = tv_broadcasts[0] if tv_broadcasts else "Marquee Sports Network"

    time_desc = f"in ~{int(round(minutes_left))} minutes" if minutes_left > 1 else "starting now"

    msg = (
        f"⚾ **Chicago Cubs Game Alert** — First pitch {time_desc}!\n\n"
        f"**{away_name}**{away_rec_str} @ **{home_name}**{home_rec_str}\n"
        f"🕒 **First Pitch:** {pitch_time_str} ({venue_name})\n"
        f"📺 **Broadcast:** {broadcast_str}\n\n"
        f"[CHOICES: Cast Cubs on TV | Dismiss]"
    )
    return msg


def check_cubs_game(force: bool = False, test: bool = False) -> tuple[bool, str, dict]:
    """Check if a Cubs game is starting within ~10-15 minutes and format notification."""
    now_pt = datetime.now(PT_TZ)
    date_str = now_pt.strftime("%Y-%m-%d")

    if test:
        # In test mode, check today, then tomorrow if today is complete or off
        try:
            games = fetch_cubs_game(date_str)
        except Exception as e:
            return False, f"⚠️ Error querying MLB Stats API in test mode: {e}", {"error": str(e)}
        # If today has no games or all are final, check tomorrow
        active_games = [g for g in games if g.get("status", {}).get("abstractGameState") != "Final"]
        if not active_games:
            tomorrow_pt = now_pt.date() + timedelta(days=1)
            try:
                games = fetch_cubs_game(tomorrow_pt.strftime("%Y-%m-%d"))
            except Exception as e:
                return False, f"⚠️ Error querying MLB Stats API for tomorrow: {e}", {"error": str(e)}
        else:
            games = active_games

        if not games:
            return True, "No upcoming Cubs games found for testing.", {"test": True}
        g = games[0]
        rep = format_game_notification(g, minutes_left=10)
        return True, rep, {"test": True, "game_pk": g.get("gamePk")}

    try:
        games = fetch_cubs_game(date_str)
    except Exception as e:
        # On background polling (non-forced), treat transient network timeouts as silent warnings to prevent chat spam
        if not force:
            return True, "", {"status": "transient_fetch_warning", "warning": str(e)}
        return False, f"⚠️ Error querying MLB Stats API: {e}", {"error": str(e)}

    if not games:
        return True, "", {"status": "no_game_today", "date": date_str}

    state = load_state()
    notified_pks = state.get("notified_games", {})
    now_ts = time.time()

    for game in games:
        game_pk = str(game.get("gamePk"))
        status_info = game.get("status", {})
        abstract_state = status_info.get("abstractGameState")

        # Skip concluded or postponed games
        if abstract_state in ("Final", "Postponed"):
            continue

        game_date_utc = game.get("gameDate")
        if not game_date_utc:
            continue

        dt_utc = datetime.fromisoformat(game_date_utc.replace("Z", "+00:00"))
        game_ts = dt_utc.timestamp()
        diff_minutes = (game_ts - now_ts) / 60.0

        # Window: between 15 minutes before first pitch and 5 minutes after scheduled start
        in_window = (0 <= diff_minutes <= 15) or (-5 <= diff_minutes < 0 and abstract_state != "Live")

        if force or (in_window and game_pk not in notified_pks):
            rep = format_game_notification(game, minutes_left=max(0, diff_minutes))
            notified_pks[game_pk] = {
                "notified_at": datetime.now(PT_TZ).strftime("%Y-%m-%d %I:%M %p PT"),
                "matchup": f"{game.get('teams', {}).get('away', {}).get('team', {}).get('name')} @ {game.get('teams', {}).get('home', {}).get('team', {}).get('name')}",
                "game_date": game_date_utc
            }
            state["notified_games"] = notified_pks
            save_state(state)
            return True, rep, {"notified": True, "game_pk": game_pk, "minutes_left": diff_minutes}

    return True, "", {"status": "nominal_outside_window"}


def main():
    parser = argparse.ArgumentParser(description="Check for upcoming Cubs game and alert ~10 minutes before first pitch")
    parser.add_argument("--test", action="store_true", help="Print sample notification for next game without saving state")
    parser.add_argument("--force", action="store_true", help="Force notification for today's game regardless of time window")
    parser.add_argument("--cast", action="store_true", help="Directly trigger TV cast for the Cubs game")
    args = parser.parse_args()

    if args.cast:
        import subprocess
        cmd = [sys.executable, "/workspace/tools/mlb_stream_extractor.py", "--team", "cubs", "--cast"]
        res = subprocess.run(cmd)
        sys.exit(res.returncode)

    ok, rep, meta = check_cubs_game(force=args.force, test=args.test)
    if rep:
        print(rep)
    elif not ok:
        sys.stderr.write(f"Error: {meta}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
