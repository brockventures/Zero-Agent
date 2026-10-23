#!/usr/bin/env python3
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import requests

API_KEY = os.environ.get("TAUTULLI_API_KEY", "")
WEBHOOK_URL = os.environ.get("DISCORD_PLEX_WEBHOOK_URL", "")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.nas_config import resolve_nas_config
except ImportError:
    from nas_config import resolve_nas_config

HOST_1_IP, _, _, _, _ = resolve_nas_config()

if os.path.exists("/secrets/env.json"):
    try:
        with open("/secrets/env.json") as f:
            _env_data = json.load(f)
            if not API_KEY:
                API_KEY = _env_data.get("TAUTULLI_API_KEY", "")
            if not WEBHOOK_URL:
                WEBHOOK_URL = _env_data.get("DISCORD_PLEX_WEBHOOK_URL", "")
            if not SONARR_API_KEY:
                SONARR_API_KEY = _env_data.get("SONARR_API_KEY", "")
    except Exception:
        pass

TAUTULLI_API_URL = os.environ.get("TAUTULLI_URL", f"http://{HOST_1_IP}:8181/api/v2")
SONARR_PORT = 8989


def _norm_title(title: str) -> str:
    """Normalize show title for cross-matching between Plex/Tautulli and Sonarr."""
    t = (title or "").strip().lower()
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", t)
    return re.sub(r"[^a-z0-9]+", "", t)


def _fetch_sonarr_season_completeness() -> dict[tuple[str, int], bool]:
    """Return {(normalized_title, season_num): is_complete_full_season} from Sonarr."""
    key = SONARR_API_KEY
    if not key:
        try:
            from tools.arr_queue_watchdog import _get_api_keys
            key, _ = _get_api_keys()
        except Exception:
            key = ""
    if not key:
        return {}

    url = f"http://{HOST_1_IP}:{SONARR_PORT}/api/v3/series"
    req = urllib.request.Request(url, headers={"X-Api-Key": key})
    completeness: dict[tuple[str, int], bool] = {}
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            series_list = json.loads(resp.read().decode())
        for s in series_list:
            raw_title = s.get("title") or ""
            keys = {_norm_title(raw_title), re.sub(r"[^a-z0-9]+", "", raw_title.lower())}
            for alt in s.get("alternateTitles", []):
                if alt.get("title"):
                    keys.add(_norm_title(alt["title"]))
            for seas in s.get("seasons", []):
                s_num = seas.get("seasonNumber")
                if not isinstance(s_num, int) or s_num <= 0:
                    continue
                st = seas.get("statistics") or {}
                total_eps = int(st.get("totalEpisodeCount") or 0)
                file_eps = int(st.get("episodeFileCount") or 0)
                next_airing = st.get("nextAiring")
                # A season is only a complete full season if all episodes exist on disk and none are still unaired
                is_full = bool(total_eps > 0 and file_eps >= total_eps and not next_airing)
                for k in keys:
                    if k:
                        completeness[(k, s_num)] = is_full
    except Exception:
        pass
    return completeness


def _is_full_season(show_name: str, season_num: int, sonarr_map: dict[tuple[str, int], bool]) -> bool:
    """Check whether (show_name, season_num) is a verified complete season."""
    if not sonarr_map:
        return True
    for candidate in (_norm_title(show_name), re.sub(r"[^a-z0-9]+", "", (show_name or "").lower())):
        if (candidate, season_num) in sonarr_map:
            return sonarr_map[(candidate, season_num)]
    return False


def _fetch_tautulli_children(rating_key: str) -> list[dict]:
    if not rating_key:
        return []
    url = f"{TAUTULLI_API_URL}?apikey={API_KEY}&cmd=get_children_metadata&rating_key={rating_key}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("response", {}).get("data", {}).get("children_list", []) or []
    except Exception:
        return []


def _format_episode_str(s_int: int, e_int: int, e_title: str) -> str:
    ep_str = f"S{s_int:02d}E{e_int:02d}"
    if e_title and not e_title.startswith("Episode "):
        ep_str += f' ("{e_title}")'
    return ep_str


def generate_digest(days: int = 7, tag_all: bool = False) -> str:
    url = f"{TAUTULLI_API_URL}?apikey={API_KEY}&cmd=get_recently_added&count=50"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode())
            items = data.get("response", {}).get("data", {}).get("recently_added", [])
    except Exception:
        return ""

    cutoff = time.time() - (days * 86400)
    sonarr_map = _fetch_sonarr_season_completeness()

    movies = []
    seen_movies = set()
    seasons = set()
    full_season_keys: set[tuple[str, int]] = set()
    episodes_by_show: dict[tuple[str, int], dict[int, str]] = {}

    def _process_season_item(show_name: str, s_int: int, s_name: str, season_rk: str):
        if _is_full_season(show_name, s_int, sonarr_map):
            seasons.add(f"{show_name} — {s_name}")
            full_season_keys.add((show_name, s_int))
        else:
            children = _fetch_tautulli_children(season_rk)
            for ch in children:
                if ch.get("media_type") != "episode":
                    continue
                ch_added = float(ch.get("added_at") or 0)
                if ch_added >= cutoff:
                    try:
                        e_int = int(ch.get("media_index"))
                        e_title = ch.get("title") or ""
                        episodes_by_show.setdefault((show_name, s_int), {})[e_int] = _format_episode_str(s_int, e_int, e_title)
                    except (TypeError, ValueError):
                        pass

    for item in items:
        added = float(item.get("added_at", 0))
        if added < cutoff:
            continue
        mtype = item.get("media_type")
        if mtype == "movie":
            title = f"{item.get('title')} ({item.get('year')})"
            if title not in seen_movies:
                seen_movies.add(title)
                movies.append(title)
        elif mtype == "show":
            show_name = item.get("title") or ""
            show_rk = str(item.get("rating_key") or "")
            if show_name and show_rk:
                for ch_season in _fetch_tautulli_children(show_rk):
                    if ch_season.get("media_type") != "season":
                        continue
                    try:
                        s_int = int(ch_season.get("media_index"))
                    except (TypeError, ValueError):
                        continue
                    if s_int <= 0:
                        continue
                    s_name = ch_season.get("title") or f"Season {s_int}"
                    s_rk = str(ch_season.get("rating_key") or "")
                    s_added = float(ch_season.get("added_at") or 0)
                    if s_added >= cutoff or not s_added:
                        _process_season_item(show_name, s_int, s_name, s_rk)
        elif mtype == "season":
            show_name = item.get("parent_title") or ""
            s_name = item.get("title") or ""
            season_rk = str(item.get("rating_key") or "")
            try:
                s_int = int(item.get("media_index") or re.sub(r"\D+", "", s_name) or 0)
            except ValueError:
                s_int = 0
            if show_name and s_int > 0:
                _process_season_item(show_name, s_int, s_name or f"Season {s_int}", season_rk)
        elif mtype == "episode":
            show_name = item.get("grandparent_title")
            s_idx = item.get("parent_media_index")
            e_idx = item.get("media_index")
            e_title = item.get("title") or ""
            if show_name and s_idx and e_idx:
                try:
                    s_int = int(s_idx)
                    e_int = int(e_idx)
                    episodes_by_show.setdefault((show_name, s_int), {})[e_int] = _format_episode_str(s_int, e_int, e_title)
                except ValueError:
                    pass

    # Suppress individual episode lines if the entire season was imported in full
    standalone_episodes = []
    for (show_name, s_int), ep_map in episodes_by_show.items():
        if (show_name, s_int) not in full_season_keys and f"{show_name} — Season {s_int}" not in seasons:
            for e_int in sorted(ep_map.keys()):
                standalone_episodes.append(f"{show_name} — {ep_map[e_int]}")

    if not movies and not seasons and not standalone_episodes:
        return ""

    header = "@everyone 🍿 **New on Plex This Week**\n" if tag_all else "🍿 **New on Plex This Week**\n"
    lines = [header]
    if movies:
        lines.append("**🎬 Movies Added:**")
        for m in sorted(movies):
            lines.append(f"- {m}")
        lines.append("")
    if seasons:
        lines.append("**📺 Full Seasons Added:**")
        for s in sorted(seasons):
            lines.append(f"- {s}")
        lines.append("")
    if standalone_episodes:
        lines.append("**✨ New Episodes Added:**")
        for e in sorted(standalone_episodes):
            lines.append(f"- {e}")
        lines.append("")

    lines.append("*Enjoy your weekend watching!*")
    return "\n".join(lines)


def post_digest(tag_all: bool = False):
    digest = generate_digest(days=7, tag_all=tag_all)
    if not digest:
        print("No new media added this week.")
        return

    if WEBHOOK_URL:
        payload = {
            "username": "Plex Weekly Roundup",
            "avatar_url": "https://raw.githubusercontent.com/Tautulli/Tautulli/master/data/interfaces/default/images/logo-plex.png",
            "content": digest,
            "allowed_mentions": {"parse": ["everyone"]} if tag_all else {"parse": []}
        }
        try:
            res = requests.post(WEBHOOK_URL, json=payload)
            if res.status_code in (200, 204):
                print("Weekly digest posted successfully via webhook!")
                return
            else:
                print(f"Failed to post digest via webhook: {res.status_code} {res.text}")
        except Exception as we:
            print(f"Webhook request failed: {we}")

    # Fallback to outbox queue for #server-updates
    try:
        from outbox import queue_outbox_message
        queue_outbox_message("server-updates", digest, source_turn="plex_weekly_digest")
        print("Weekly digest queued via outbox to #server-updates!")
    except Exception as e:
        print(f"Failed to queue outbox message: {e}")


if __name__ == "__main__":
    tag = "--tag-all" in sys.argv
    if len(sys.argv) > 1 and "post" in sys.argv:
        post_digest(tag_all=tag)
    else:
        print(generate_digest(days=7, tag_all=tag) or "No new media added this week.")
