#!/usr/bin/env python3
import sys, json, time, urllib.request, requests

import os

import urllib.parse

API_KEY = os.environ.get("TAUTULLI_API_KEY", "")
WEBHOOK_URL = os.environ.get("DISCORD_PLEX_WEBHOOK_URL", "")
HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "")

if os.path.exists("/secrets/env.json"):
    try:
        with open("/secrets/env.json") as f:
            _env_data = json.load(f)
            if not API_KEY:
                API_KEY = _env_data.get("TAUTULLI_API_KEY", "")
            if not WEBHOOK_URL:
                WEBHOOK_URL = _env_data.get("DISCORD_PLEX_WEBHOOK_URL", "")
            if not HOST_1_IP:
                if _env_data.get("NAS_HOST_1_IP"):
                    HOST_1_IP = _env_data["NAS_HOST_1_IP"]
                elif _env_data.get("HA_BASE_URL"):
                    HOST_1_IP = urllib.parse.urlparse(_env_data["HA_BASE_URL"]).hostname
    except Exception:
        pass

if not HOST_1_IP and os.path.exists("/secrets/ha.json"):
    try:
        with open("/secrets/ha.json") as f:
            _ha_data = json.load(f)
            if _ha_data.get("url"):
                HOST_1_IP = urllib.parse.urlparse(_ha_data["url"]).hostname
    except Exception:
        pass

HOST_1_IP = HOST_1_IP or "127.0.0.1"
TAUTULLI_API_URL = os.environ.get("TAUTULLI_URL", f"http://{HOST_1_IP}:8181/api/v2")

def generate_digest(days: int = 7, tag_all: bool = False) -> str:
    url = f"{TAUTULLI_API_URL}?apikey={API_KEY}&cmd=get_recently_added&count=100"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            items = data.get("response", {}).get("data", {}).get("recently_added", [])
    except Exception as e:
        return ""

    cutoff = time.time() - (days * 86400)
    movies = []
    seen_movies = set()
    seasons = set()
    episodes_by_show = {}

    for item in items:
        added = float(item.get("added_at", 0))
        if added >= cutoff:
            mtype = item.get("media_type")
            if mtype == "movie":
                title = f"{item.get('title')} ({item.get('year')})"
                if title not in seen_movies:
                    seen_movies.add(title)
                    movies.append(title)
            elif mtype == "season":
                show_name = item.get("parent_title")
                s_name = item.get("title")
                if show_name and s_name:
                    seasons.add(f"{show_name} — {s_name}")
            elif mtype == "episode":
                show_name = item.get("grandparent_title")
                s_idx = item.get("parent_media_index")
                e_idx = item.get("media_index")
                e_title = item.get("title")
                if show_name and s_idx and e_idx:
                    try:
                        s_int = int(s_idx)
                        e_int = int(e_idx)
                        ep_str = f"S{s_int:02d}E{e_int:02d}"
                        if e_title and not e_title.startswith("Episode "):
                            ep_str += f' ("{e_title}")'
                        episodes_by_show.setdefault((show_name, s_int), []).append(ep_str)
                    except ValueError:
                        pass

    # Suppress individual episode lines if the entire season was imported in full
    standalone_episodes = []
    for (show_name, s_int), eps in episodes_by_show.items():
        if f"{show_name} — Season {s_int}" not in seasons:
            for ep in eps:
                standalone_episodes.append(f"{show_name} — {ep}")

    if not movies and not seasons and not standalone_episodes:
        return ""

    header = "@everyone 🍿 **New on Plex This Week**\n" if tag_all else "🍿 **New on Plex This Week**\n"
    lines = [header]
    if movies:
        lines.append("**🎬 Movies Added:**")
        for m in sorted(movies):
            lines.append(f"• {m}")
        lines.append("")
    if seasons:
        lines.append("**📺 Full Seasons Added:**")
        for s in sorted(seasons):
            lines.append(f"• {s}")
        lines.append("")
    if standalone_episodes:
        lines.append("**✨ New Episodes Added:**")
        for e in sorted(standalone_episodes):
            lines.append(f"• {e}")
        lines.append("")

    lines.append("*Enjoy your weekend watching!*")
    return "\n".join(lines)

def post_digest(tag_all: bool = False):
    digest = generate_digest(days=7, tag_all=tag_all)
    if not digest:
        print("No new media added this week.")
        return

    payload = {
        "username": "Plex Weekly Roundup",
        "avatar_url": "https://raw.githubusercontent.com/Tautulli/Tautulli/master/data/interfaces/default/images/logo-plex.png",
        "content": digest,
        "allowed_mentions": {"parse": ["everyone"]} if tag_all else {"parse": []}
    }
    res = requests.post(WEBHOOK_URL, json=payload)
    if res.status_code in (200, 204):
        print("Weekly digest posted successfully!")
    else:
        print(f"Failed to post digest: {res.status_code} {res.text}")

if __name__ == "__main__":
    tag = "--tag-all" in sys.argv
    if len(sys.argv) > 1 and "post" in sys.argv:
        post_digest(tag_all=tag)
    else:
        print(generate_digest(days=7, tag_all=tag) or "No new media added this week.")
