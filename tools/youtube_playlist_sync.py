#!/usr/bin/env python3
"""YouTube & YouTube Music Playlist Synchronizer.

Creates playlists and adds tracks directly to YouTube / YouTube Music library
using the Google YouTube Data API v3 with Zero's OAuth credentials.
"""

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

def create_playlist(title: str, description: str, privacy: str = "private") -> dict:
    tok = get_access_token()
    url = "https://www.googleapis.com/youtube/v3/playlists?part=snippet,status"
    body = {
        "snippet": {
            "title": title,
            "description": description
        },
        "status": {
            "privacyStatus": privacy
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())

def search_track_video_id(artist: str, track: str) -> str:
    tok = get_access_token()
    query = urllib.parse.quote(f"{artist} {track} official audio")
    url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={query}&type=video&maxResults=1"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
        items = data.get("items", [])
        if items:
            return items[0]["id"]["videoId"]
    return None

def add_track_to_playlist(playlist_id: str, video_id: str):
    tok = get_access_token()
    url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id
            }
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        tok = get_access_token()
        print("Access token retrieved successfully!")
