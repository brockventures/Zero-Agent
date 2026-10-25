#!/usr/bin/env python3
"""MLB Stream Watcher & Auto-Dispatcher for Cubs game.

Monitors mlb24all.ir until the Chicago Cubs broadcast relay goes live (~7:00 PM PT),
immediately casts to the Living Room TV, and notifies #zero-chat via outbox.
"""

import sys
sys.path.insert(0, "/workspace")
import time
import subprocess
import json

from tools.mlb_stream_extractor import (
    fetch_stateshot,
    resolve_team_game,
    resolve_media_event,
    resolve_stream_url,
    cast_to_home_assistant
)

MAX_WAIT_SECONDS = 900  # 15 minutes
POLL_INTERVAL = 15

def run():
    start_time = time.time()
    print("Starting MLB Stream Watcher for Chicago Cubs...")

    while time.time() - start_time < MAX_WAIT_SECONDS:
        try:
            data = fetch_stateshot()
            game, teams, target_team_id = resolve_team_game(data, "cubs")
            media_event = resolve_media_event(data, game, target_team_id)

            flavors = data.get("flavors", [])
            chosen_flavor = None
            for fl in flavors:
                if "free" in fl.get("id", "").lower() and media_event["id"] in fl.get("media_event_ids", []):
                    chosen_flavor = fl["id"]
                    break

            if not chosen_flavor:
                for fl in flavors:
                    if "free.live" in fl.get("id", "").lower():
                        chosen_flavor = fl["id"]
                        break
                if not chosen_flavor:
                    chosen_flavor = "free.live.sportsnet"

            # Attempt stream resolution
            try:
                stream_url = resolve_stream_url(media_event["id"], chosen_flavor)
                if stream_url:
                    print(f"Stream URL live: {stream_url}")
                    # Dispatch to Living Room TV
                    success = cast_to_home_assistant(stream_url, "media_player.living_room_tv")
                    status_text = "successfully dispatched to Living Room TV" if success else "failed to cast to TV"
                    msg = (
                        f"⚾ **Cubs @ Padres is live!**\n"
                        f"Broadcast: `{media_event.get('title')}` ({chosen_flavor})\n"
                        f"Playback: {status_text} in native VLC."
                    )
                    subprocess.run([
                        sys.executable, "/workspace/tools/outbox.py",
                        "--channel", "zero-chat",
                        "--message", msg,
                        "--flush"
                    ], check=False)
                    return 0
            except Exception as e:
                print(f"Waiting for live feed (event {media_event.get('id')}, status {media_event.get('status')}): {e}")

        except Exception as e:
            print(f"Polling loop notice: {e}")

        time.sleep(POLL_INTERVAL)

    print("Timed out waiting for Cubs stream.")
    return 1

if __name__ == "__main__":
    sys.exit(run())
