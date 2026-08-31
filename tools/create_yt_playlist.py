#!/usr/bin/env python3
"""Create YouTube / YouTube Music playlist and populate tracks."""

import json
import os
import sys
import time
import urllib.request
import urllib.parse

SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", os.environ.get("GOOGLE_OAUTH_SECRETS", ""))
if not SECRETS_PATH:
    if os.path.exists("/secrets/youtube_oauth.json"):
        SECRETS_PATH = "/secrets/youtube_oauth.json"
    elif os.path.exists("/secrets/google_oauth.json"):
        SECRETS_PATH = "/secrets/google_oauth.json"
    elif os.path.exists("/workspace/config/youtube_oauth.json"):
        SECRETS_PATH = "/workspace/config/youtube_oauth.json"
    else:
        SECRETS_PATH = "/workspace/config/google_oauth.json"

TIMEOUT = 20

def load_credentials(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        creds = {}
        for line in content.splitlines():
            line = line.strip().rstrip(",")
            if ":" in line:
                k, v = line.split(":", 1)
                creds[k.strip().strip("\"").strip("'")] = v.strip().strip("\"").strip("'")
        return creds

def get_access_token() -> str:
    creds = load_credentials(SECRETS_PATH)

    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        tokens = json.loads(resp.read().decode())
        return tokens["access_token"]

TRACKS = [
    ("The Interrupters", "Gave You Everything"),
    ("Suburban Legends", "Bright Spring Morning"),
    ("Streetlight Manifesto", "We Will Fall Together"),
    ("Save Ferris", "Come On Eileen"),
    ("The Aquabats!", "Super Rad!"),
    ("Big D and the Kids Table", "Shining On"),
    ("fun.", "Walking the Dog"),
    ("Jukebox the Ghost", "Hold It In"),
    ("Saint Motel", "My Type"),
    ("The Hoosiers", "Goodbye Mr. A"),
    ("Bleachers", "Rollercoaster"),
    ("The Wombats", "Greek Tragedy"),
    ("Fitz and the Tantrums", "MoneyGrabber"),
    ("The Academy Is...", "About a Girl"),
    ("Motion City Soundtrack", "LG FUAD"),
    ("We the Kings", "Check Yes, Juliet"),
    ("Cobra Starship", "Guilty Pleasure"),
    ("The Starting Line", "The Best of Me"),
    ("zebrahead", "All My Friends Are Nobodies"),
    ("MUNA", "Silk Chiffon"),
    ("COIN", "Crash My Car"),
    ("The Beaches", "Blame Brett"),
    ("Remi Wolf", "Disco Man"),
    ("Lights", "Prodigal Daughter"),
    ("The Sounds", "Living in America")
]

def main():
    tok = get_access_token()
    print("1. Creating Playlist on YouTube...")
    
    # 1. Create Playlist
    pl_url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet,status"
    pl_body = {
        "snippet": {
            "title": "Brass, Power Chords & Clever Hooks",
            "description": "Curated discovery playlist based on 444 Liked Songs: Reel Big Fish, OK Go, Fall Out Boy, Paramore, and 90s/00s ska-punk/geek-rock."
        },
        "status": {
            "privacyStatus": "unlisted"
        }
    }
    req = urllib.request.Request(
        pl_url,
        data=json.dumps(pl_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        pl_data = json.loads(resp.read().decode())
        playlist_id = pl_data["id"]
        print(f"✅ Playlist created successfully! ID: {playlist_id}")

    yt_music_url = f"https://music.youtube.com/playlist?list={playlist_id}"
    yt_web_url = f"https://www.youtube.com/playlist?list={playlist_id}"
    print(f"🎵 YouTube Music URL: {yt_music_url}")
    print(f"📺 YouTube Web URL: {yt_web_url}")

    # 2. Search & Add Tracks
    print(f"\n2. Searching and adding {len(TRACKS)} tracks...")
    added_count = 0

    for idx, (artist, title) in enumerate(TRACKS, 1):
        query = urllib.parse.quote(f"{artist} {title} official audio")
        search_url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={query}&type=video&maxResults=1"
        s_req = urllib.request.Request(search_url, headers={"Authorization": f"Bearer {tok}"})
        
        try:
            with urllib.request.urlopen(s_req, timeout=TIMEOUT) as s_resp:
                s_data = json.loads(s_resp.read().decode())
                items = s_data.get("items", [])
                if not items:
                    print(f"  ❌ [{idx}/{len(TRACKS)}] Could not find video for: {artist} - {title}")
                    continue
                video_id = items[0]["id"]["videoId"]
                video_title = items[0]["snippet"]["title"]

            # Add to playlist
            add_url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
            add_body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id
                    }
                }
            }
            a_req = urllib.request.Request(
                add_url,
                data=json.dumps(add_body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Content-Type": "application/json"
                }
            )
            with urllib.request.urlopen(a_req, timeout=TIMEOUT) as a_resp:
                if a_resp.status in (200, 201):
                    added_count += 1
                    print(f"  ✅ [{idx}/{len(TRACKS)}] Added: {artist} - {title} ({video_id})")
            
            time.sleep(0.4) # polite rate pacing

        except Exception as e:
            print(f"  ⚠️ Error adding {artist} - {title}: {e}")

    print(f"\n🎉 DONE! Added {added_count}/{len(TRACKS)} tracks to YouTube Music playlist.")
    print(f"Direct Link: {yt_music_url}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("Usage: create_yt_playlist.py [--dry-run]")
        sys.exit(0)
    main()
