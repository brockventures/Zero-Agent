#!/usr/bin/env python3
"""Personal Data Sources & Memory Profile Sync Sidecar for Zero.

Weekly automated synchronization across all live personal data sources:
1. Steam Web API -> /workspace/data/steam_games.json & media_gaming_steam_profile.md
2. Xbox OpenXBL API -> /workspace/data/xbox_games.json & media_gaming_xbox_profile.md
3. Nintendo Switch 2 Moon API -> /workspace/data/switch_data.json & media_gaming_switch_profile.md
4. YouTube / YouTube Music (Prime416 OAuth) -> /workspace/data/youtube_prime416.json, music_taste.md & media_and_podcasts.md
5. Amazon Order Listener Rollup -> amazon_orders.json synthesized & rolled into amazon_purchasing_patterns.md
6. Plex / Tautulli Watch History -> rqbrock (330261023) watch log semantically partitioned into media_plex_taste_profile.md
7. Hybrid Memory Indexer -> Incremental FTS5 + dense vector re-index of /workspace/memory/
"""

from __future__ import annotations

import email.utils
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
WORKSPACE = Path("/workspace")
DATA_DIR = WORKSPACE / "data"
CONFIG_DIR = WORKSPACE / "config"
PRIVATE_MEM_DIR = WORKSPACE / "memory" / "private"

for _p in (str(WORKSPACE), str(WORKSPACE / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def update_frontmatter_field(md_text: str, key: str, value: str) -> str:
    """Deterministically update a YAML frontmatter key without unanchored regexes."""
    lines = md_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return md_text
    prefix = f"{key}:"
    for idx in range(1, min(len(lines), 25)):
        stripped = lines[idx].strip()
        if stripped == "---":
            break
        if stripped.startswith(prefix):
            indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip())]
            lines[idx] = f"{indent}{key}: {value}"
            return "\n".join(lines) + ("\n" if md_text.endswith("\n") else "")
    return md_text


def upsert_markdown_section(md_text: str, section_heading_prefix: str, new_section_block: str) -> str:
    """Deterministically replace or append a trailing Markdown section starting with `section_heading_prefix`."""
    lines = md_text.splitlines()
    cut_idx = None
    for idx, line in enumerate(lines):
        if line.strip().startswith(section_heading_prefix):
            cut_idx = idx
            # Also trim preceding '---' separator if present
            while cut_idx > 0 and lines[cut_idx - 1].strip() in ("", "---"):
                cut_idx -= 1
            break
    base_lines = lines[:cut_idx] if cut_idx is not None else lines
    while base_lines and not base_lines[-1].strip():
        base_lines.pop()
    base_text = "\n".join(base_lines)
    return f"{base_text}\n\n---\n\n{new_section_block.strip()}\n"


def sync_amazon_purchasing_profile() -> tuple[bool, str]:
    """Roll live orders from /workspace/data/amazon_orders.json into amazon_purchasing_patterns.md with LLM thematic synthesis."""
    orders_file = DATA_DIR / "amazon_orders.json"
    profile_file = PRIVATE_MEM_DIR / "amazon_purchasing_patterns.md"
    if not orders_file.exists() or not profile_file.exists():
        return False, "Missing amazon_orders.json or amazon_purchasing_patterns.md"

    orders = json.load(open(orders_file, "r", encoding="utf-8"))
    if not isinstance(orders, list):
        return False, "Malformed amazon_orders.json"

    def parse_dt(o: dict) -> float:
        raw = o.get("date", "")
        try:
            return email.utils.parsedate_to_datetime(raw).timestamp()
        except Exception:
            return 0.0

    by_id: dict[str, dict] = {}
    for o in orders:
        if isinstance(o, dict) and o.get("order_id"):
            by_id[o["order_id"]] = o
    sorted_orders = sorted(by_id.values(), key=parse_dt, reverse=True)

    today_str = datetime.now(PT).strftime("%Y-%m-%d")
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")

    # Optional lightweight LLM synthesis of recent purchasing themes
    theme_bullets: list[str] = []
    try:
        from tools.llm_sidecar import _is_test_fast_mode, call_sidecar_llm_json
        if not _is_test_fast_mode() and sorted_orders:
            compact_items = [
                {"date": o.get("date", "")[:16], "total": o.get("total"), "items": o.get("items", [])}
                for o in sorted_orders[:30]
            ]
            prompt = (
                "Synthesize Ryan's recent Amazon orders into 3-4 concise thematic bullets "
                "(e.g., Homelab & Networking, 3D Printing & Hardware Lab, Kids & Family, Household Staples).\n"
                f"Orders:\n{json.dumps(compact_items, indent=2)}\n\n"
                "Return ONLY valid JSON: {\"themes\": [\"- **<Theme>:** <1-sentence summary of specific items>\", ...]}"
            )
            parsed = call_sidecar_llm_json("personal_data_sync", prompt, model="gemini-3.8-flash-low", timeout=35)
            if isinstance(parsed, dict) and isinstance(parsed.get("themes"), list):
                theme_bullets = [str(b).strip() for b in parsed["themes"] if str(b).strip()]
    except Exception:
        pass

    sec8_lines = [
        "## 8. 📦 Live Incremental Order Log (Gmail Listener Rollup)",
        "",
        f"*Auto-synced on {now_str} from `/workspace/data/amazon_orders.json` ({len(sorted_orders)} recent orders captured via `amazon_order_listener.py`):*",
        "",
    ]
    if theme_bullets:
        sec8_lines.append("### Recent Purchasing Themes (LLM Synthesis)")
        sec8_lines.extend(theme_bullets)
        sec8_lines.append("")
        sec8_lines.append("### Itemized Order Ledger")

    for o in sorted_orders:
        oid = o.get("order_id", "")
        tot = o.get("total", "Unknown")
        raw_dt = o.get("date", "")
        try:
            dt_str = email.utils.parsedate_to_datetime(raw_dt).astimezone(PT).strftime("%Y-%m-%d")
        except Exception:
            dt_str = raw_dt[:16]
        items = o.get("items") or []
        if items:
            items_str = "; ".join(str(i).strip() for i in items)
        else:
            subj = str(o.get("subject", "")).replace('Ordered: "', "").rstrip('"')
            items_str = subj or "Amazon Order"
        tot_label = f" ({tot})" if tot and tot != "Unknown" else ""
        sec8_lines.append(f"- **{dt_str}** (`{oid}`{tot_label}): {items_str}")

    raw_md = profile_file.read_text(encoding="utf-8")
    raw_md = update_frontmatter_field(raw_md, "updated", today_str)
    new_md = upsert_markdown_section(raw_md, "## 8. 📦 Live Incremental Order Log", "\n".join(sec8_lines))
    profile_file.write_text(new_md, encoding="utf-8")
    return True, f"{len(sorted_orders)} live orders rolled into amazon_purchasing_patterns.md"


def sync_plex_taste_profile() -> tuple[bool, str]:
    """Roll recent Tautulli watch history for Ryan (rqbrock, user_id=330261023) into media_plex_taste_profile.md using LLM classification."""
    profile_file = PRIVATE_MEM_DIR / "media_plex_taste_profile.md"
    if not profile_file.exists():
        return False, "Missing media_plex_taste_profile.md"

    from tools.plex_weekly_digest import API_KEY, TAUTULLI_API_URL
    if not API_KEY:
        return False, "Missing TAUTULLI_API_KEY"

    # Strictly filter to Ryan's personal Plex account (rqbrock = 330261023; brock0li is Kara = 355211689)
    RYAN_PLEX_USER_ID = "330261023"
    url = f"{TAUTULLI_API_URL}?apikey={API_KEY}&cmd=get_history&user_id={RYAN_PLEX_USER_ID}&length=200"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("response", {}).get("data", {}).get("data", [])

    movies_map: dict[str, dict] = {}
    shows_map: dict[str, dict] = {}

    for r in rows:
        if str(r.get("user_id")) != RYAN_PLEX_USER_ID:
            continue
        watched = float(r.get("watched_status") or 0)
        if watched < 0.5:
            continue
        mtype = r.get("media_type")
        player = str(r.get("player") or "")
        dt_ts = int(r.get("date") or 0)
        dt_str = datetime.fromtimestamp(dt_ts, PT).strftime("%Y-%m-%d") if dt_ts else ""

        if mtype == "movie":
            title = r.get("title") or r.get("full_title") or "Unknown"
            if title not in movies_map:
                movies_map[title] = {
                    "title": title,
                    "year": r.get("year"),
                    "date": dt_str,
                    "player": player,
                }
        elif mtype == "episode":
            show = r.get("grandparent_title") or "Unknown Series"
            s_num = r.get("parent_media_index")
            e_num = r.get("media_index")
            ep_title = r.get("title") or ""
            ep_code = f"S{int(s_num):02d}E{int(e_num):02d}" if str(s_num).isdigit() and str(e_num).isdigit() else ep_title
            info = shows_map.setdefault(show, {"title": show, "count": 0, "latest_dt": dt_str, "latest_ep": ep_code, "players": set()})
            info["count"] += 1
            if player:
                info["players"].add(player)
            if dt_str > info["latest_dt"]:
                info["latest_dt"] = dt_str
                info["latest_ep"] = ep_code

    # Use Gemini 3.8 Flash (Low) to semantically partition Ryan's personal viewing vs Rosie/Isaac kids' media
    kids_titles_set: set[str] = set()
    all_titles = [
        {"title": s["title"], "type": "tv", "players": sorted(list(s["players"]))}
        for s in shows_map.values()
    ] + [
        {"title": m["title"], "type": "movie", "year": m["year"], "player": m["player"]}
        for m in movies_map.values()
    ]

    try:
        from tools.llm_sidecar import _is_test_fast_mode, call_sidecar_llm_json
        if not _is_test_fast_mode() and all_titles:
            prompt = (
                "Classify these Plex titles watched on Ryan's account (`rqbrock`).\n"
                "Ryan has two young kids (Rosie, age 6; Isaac, age 2) who watch kids' cartoons and family animation "
                "(e.g. Bluey, Sesame Street, Vampirina, Trolls, Minions, Toy Story, Moana, Paddington, Monster House), "
                "often on the 'Pixel Tablet'. Note that adult/teen anime or superhero titles like JoJo's Bizarre Adventure, "
                "X-Men '97, The Legend of Vox Machina, Spider-Man: Brand New Day, Supergirl, and Lanterns are Ryan's personal viewing.\n\n"
                f"Titles:\n{json.dumps(all_titles, indent=2)}\n\n"
                "Return ONLY valid JSON listing the exact title strings that belong to the kids/toddler cluster:\n"
                "{\"kids_titles\": [\"Bluey (2018)\", \"Sesame Street\", ...]}"
            )
            parsed = call_sidecar_llm_json("personal_data_sync", prompt, model="gemini-3.8-flash-low", timeout=35)
            if isinstance(parsed, dict) and isinstance(parsed.get("kids_titles"), list):
                kids_titles_set = {str(t).strip().lower() for t in parsed["kids_titles"]}
    except Exception:
        pass

    # Deterministic fallback if LLM is skipped in fast-test mode
    if not kids_titles_set:
        fallback_prefixes = (
            "sesame street", "bluey", "vampirina", "daniel tiger", "paw patrol", "peppa pig",
            "trolls", "minions", "toy story", "moana", "paddington", "monster house",
        )
        for item in all_titles:
            t_low = item["title"].lower()
            if any(t_low.startswith(p) for p in fallback_prefixes):
                kids_titles_set.add(t_low)

    personal_shows = [s for s in shows_map.values() if s["title"].lower() not in kids_titles_set]
    personal_movies = [m for m in movies_map.values() if m["title"].lower() not in kids_titles_set]
    kids_shows = [s for s in shows_map.values() if s["title"].lower() in kids_titles_set]
    kids_movies = [m for m in movies_map.values() if m["title"].lower() in kids_titles_set]

    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    sec3_lines = [
        "## 4. 🍿 Live Tautulli Watch Activity (`rqbrock` Personal History)",
        "",
        f"*Auto-synced on {now_str} via Tautulli API (`user_id=330261023`, `rqbrock`, semantically partitioned via `gemini-3.8-flash-low`):*",
        "",
    ]
    if personal_shows:
        sec3_lines.append("### Recently Watched TV Series (`rqbrock`)")
        for st in sorted(personal_shows, key=lambda x: (x["latest_dt"], x["count"]), reverse=True)[:15]:
            sec3_lines.append(
                f"- **{st['title']}**: {st['count']} recent episode(s) logged (Latest: `{st['latest_ep']}` on {st['latest_dt']})"
            )
        sec3_lines.append("")
    if personal_movies:
        sec3_lines.append("### Recently Watched Movies (`rqbrock`)")
        for m in personal_movies[:15]:
            yr = f" ({m['year']})" if m.get("year") else ""
            dev = f" [{m['player']}]" if m.get("player") else ""
            sec3_lines.append(f"- **{m['title']}**{yr} — watched {m['date']}{dev}")
        sec3_lines.append("")
    if kids_shows or kids_movies:
        kids_summary = ", ".join(
            [f"*{s['title']}* ({s['count']} eps)" for s in kids_shows[:6]]
            + [f"*{m['title']}*" for m in kids_movies[:6]]
        )
        sec3_lines.append(f"- **Filtered Family / Kids Activity (`rqbrock` / `Pixel Tablet`):** {kids_summary}")

    raw_md = profile_file.read_text(encoding="utf-8")
    # Clean up previous ## 3. 🍿 Live Tautulli heading if present
    raw_md = upsert_markdown_section(raw_md, "## 3. 🍿 Live Tautulli Watch Activity", "\n".join(sec3_lines))
    raw_md = upsert_markdown_section(raw_md, "## 4. 🍿 Live Tautulli Watch Activity", "\n".join(sec3_lines))
    profile_file.write_text(raw_md, encoding="utf-8")
    return True, f"{len(personal_shows)} personal series & {len(personal_movies)} personal movies synced for rqbrock ({len(kids_shows) + len(kids_movies)} kids titles separated)"


def sync_youtube_prime416_profile() -> tuple[bool, str]:
    """Sync Ryan's Prime416 YouTube & YouTube Music playlists, liked videos, and subscriptions."""
    yt_oauth_file = Path("/secrets/youtube_oauth.json")
    if not yt_oauth_file.exists():
        yt_oauth_file = CONFIG_DIR / "youtube_oauth.json"
    if not yt_oauth_file.exists():
        return False, "Missing /secrets/youtube_oauth.json"

    cfg = json.loads(yt_oauth_file.read_text(encoding="utf-8"))
    token_data = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=token_data), timeout=15) as r:
        access_token = json.loads(r.read().decode("utf-8"))["access_token"]

    def yt_get(endpoint: str) -> dict:
        req = urllib.request.Request(
            f"https://www.googleapis.com/youtube/v3/{endpoint}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    playlists_resp = yt_get("playlists?part=snippet,contentDetails&mine=true&maxResults=50")
    playlists = []
    for pl in playlists_resp.get("items", []):
        pid = pl.get("id")
        title = pl.get("snippet", {}).get("title", "Untitled")
        count = pl.get("contentDetails", {}).get("itemCount", 0)
        tracks = []
        if pid:
            try:
                p_items = yt_get(f"playlistItems?part=snippet&playlistId={pid}&maxResults=50").get("items", [])
                for pit in p_items:
                    sn = pit.get("snippet", {})
                    t_title = sn.get("title", "")
                    artist = (sn.get("videoOwnerChannelTitle") or "").replace(" - Topic", "").strip()
                    if t_title and t_title not in ("Deleted video", "Private video"):
                        tracks.append(f"{t_title} ({artist})" if artist else t_title)
            except Exception:
                pass
        playlists.append({"id": pid, "title": title, "item_count": count, "tracks": tracks})

    likes_resp = yt_get("playlistItems?part=snippet&playlistId=LL&maxResults=40")
    total_likes = likes_resp.get("pageInfo", {}).get("totalResults", 0)
    recent_likes = []
    for it in likes_resp.get("items", []):
        sn = it.get("snippet", {})
        title = sn.get("title", "")
        ch = (sn.get("videoOwnerChannelTitle") or "").replace(" - Topic", "").strip()
        dt = (sn.get("publishedAt") or "")[:10]
        if title and title not in ("Deleted video", "Private video"):
            recent_likes.append({"date": dt, "title": title, "channel": ch})

    subs_resp = yt_get("subscriptions?part=snippet&mine=true&maxResults=50")
    subscriptions = [
        it.get("snippet", {}).get("title", "")
        for it in subs_resp.get("items", [])
        if it.get("snippet", {}).get("title")
    ]

    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    payload = {
        "channel_handle": "@prime416",
        "channel_id": "UCcdIheQ7Mj-e6vvGBw_A-Tg",
        "fetched_at": datetime.now(PT).isoformat(),
        "total_liked_videos": total_likes,
        "playlists": playlists,
        "subscriptions": subscriptions,
        "recent_likes": recent_likes,
    }
    (DATA_DIR / "youtube_prime416.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # 1. Update music_taste.md with live Prime416 playlists
    music_file = PRIVATE_MEM_DIR / "music_taste.md"
    if music_file.exists():
        m_lines = [
            "## 6. 🎧 Live YouTube Music & `Prime416` Playlists (API Sync)",
            "",
            f"*Auto-synced on {now_str} via YouTube Data API v3 (`@prime416`, `UCcdIheQ7Mj-e6vvGBw_A-Tg`):*",
            "",
        ]
        for pl in playlists:
            sample = ", ".join(pl["tracks"][:8])
            sample_str = f" — Highlights: {sample}" if sample else ""
            m_lines.append(f"- **{pl['title']}** ({pl['item_count']} tracks){sample_str}")
        raw_music = music_file.read_text(encoding="utf-8")
        music_file.write_text(
            upsert_markdown_section(raw_music, "## 6. 🎧 Live YouTube Music", "\n".join(m_lines)),
            encoding="utf-8",
        )

    # 2. Update media_and_podcasts.md with live Prime416 subscriptions & recent likes
    media_file = PRIVATE_MEM_DIR / "media_and_podcasts.md"
    if media_file.exists():
        yt_lines = [
            "## 6. ▶️ Live YouTube (`Prime416`) Subscriptions & Recent Likes",
            "",
            f"*Auto-synced on {now_str} via YouTube Data API v3 (`@prime416`, {len(subscriptions)} subscriptions, {total_likes:,} liked videos):*",
            "",
            f"- **Active Channel Subscriptions ({len(subscriptions)}):** " + ", ".join(f"**{s}**" for s in subscriptions),
            "- **Recent Liked Videos (`LL`):**",
        ]
        for lk in recent_likes[:12]:
            ch_str = f" ({lk['channel']})" if lk["channel"] else ""
            yt_lines.append(f"  - **{lk['date']}**: {lk['title']}{ch_str}")
        raw_media = media_file.read_text(encoding="utf-8")
        media_file.write_text(
            upsert_markdown_section(raw_media, "## 6. ▶️ Live YouTube", "\n".join(yt_lines)),
            encoding="utf-8",
        )

    return True, f"{len(playlists)} playlists, {len(subscriptions)} subscriptions, and {total_likes:,} likes synced from Prime416"


def sync_pocketcasts_profile() -> tuple[bool, str]:
    """Sync Pocket Casts subscriptions, listening stats, and recent episode history."""
    import urllib.parse
    import urllib.request

    pc_path = Path("/secrets/pocketcasts.json")
    if not pc_path.exists():
        pc_path = Path("/workspace/config/pocketcasts.json")
    if not pc_path.exists():
        return False, "Missing pocketcasts.json credentials"

    creds = json.loads(pc_path.read_text(encoding="utf-8"))
    token = creds.get("token")

    # Authenticate / refresh token via login_pocket_casts
    if creds.get("email") and creds.get("password"):
        login_req = urllib.request.Request(
            "https://api.pocketcasts.com/user/login_pocket_casts",
            data=json.dumps({
                "email": creds["email"],
                "password": creds["password"],
                "scope": "webplayer",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(login_req, timeout=15) as resp:
            login_data = json.loads(resp.read().decode("utf-8"))
            token = login_data.get("token") or login_data.get("accessToken") or token
            if login_data.get("refreshToken"):
                creds["refreshToken"] = login_data["refreshToken"]
            creds["token"] = token
            if pc_path.exists():
                pc_path.write_text(json.dumps(creds, indent=2), encoding="utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    def pc_post(ep: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"https://api.pocketcasts.com{ep}",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    pod_resp = pc_post("/user/podcast/list", {"v": 1})
    stats_resp = pc_post("/user/stats/summary", {})
    hist_resp = pc_post("/user/history", {})
    upnext_resp = pc_post("/up_next/list", {"version": 2})

    podcasts = [
        {
            "title": p.get("title", ""),
            "author": p.get("author", ""),
            "uuid": p.get("uuid", ""),
            "description": p.get("description", ""),
        }
        for p in pod_resp.get("podcasts", [])
        if p.get("title")
    ]

    time_listened_hrs = round(int(stats_resp.get("timeListened") or 0) / 3600.0, 1)
    time_speed_hrs = round(int(stats_resp.get("timeVariableSpeed") or 0) / 3600.0, 1)
    time_skip_hrs = round(int(stats_resp.get("timeSkipping") or 0) / 3600.0, 1)
    time_intro_hrs = round(int(stats_resp.get("timeIntroSkipping") or 0) / 3600.0, 1)
    time_silence_hrs = round(int(stats_resp.get("timeSilenceRemoval") or 0) / 3600.0, 1)
    total_saved_hrs = round(time_speed_hrs + time_skip_hrs + time_intro_hrs + time_silence_hrs, 1)

    history_eps = []
    for e in hist_resp.get("episodes", [])[:100]:
        history_eps.append({
            "podcast": e.get("podcastTitle", ""),
            "title": e.get("title", ""),
            "published": (e.get("published") or "")[:10],
            "duration_min": round(int(e.get("duration") or 0) / 60.0, 1),
        })

    up_next_eps = [
        {
            "title": e.get("title", ""),
            "podcast_uuid": e.get("podcast", ""),
            "published": (e.get("published") or "")[:10],
        }
        for e in upnext_resp.get("episodes", [])[:15]
        if e.get("title")
    ]

    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    payload = {
        "fetched_at": datetime.now(PT).isoformat(),
        "stats": {
            "time_listened_hours": time_listened_hrs,
            "time_listened_days": round(time_listened_hrs / 24.0, 1),
            "total_saved_hours": total_saved_hrs,
            "variable_speed_saved_hours": time_speed_hrs,
            "skipping_saved_hours": time_skip_hrs,
            "intro_skipping_saved_hours": time_intro_hrs,
            "silence_removal_saved_hours": time_silence_hrs,
        },
        "subscribed_podcasts": podcasts,
        "up_next": up_next_eps,
        "recent_history": history_eps,
    }
    (DATA_DIR / "pocketcasts_data.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    media_file = PRIVATE_MEM_DIR / "media_and_podcasts.md"
    if media_file.exists():
        sub_titles = [p["title"] for p in podcasts if not p["title"].startswith("Maximum Fun Bonus Content")]
        pc_lines = [
            "## 7. 🎙️ Live Pocket Casts Subscriptions & Listening Telemetry",
            "",
            f"*Auto-synced on {now_str} via Pocket Casts API (`api.pocketcasts.com`):*",
            "",
            f"- **Lifetime Listening Time:** **{time_listened_hrs:,.1f} hours** (**{time_listened_hrs / 24.0:,.1f} days**)",
            f"- **Total Time Saved:** **{total_saved_hrs:,.1f} hours** (Variable Speed: **{time_speed_hrs:,.1f}h**, Skipping: **{time_skip_hrs:,.1f}h**, Intro Skip: **{time_intro_hrs:,.1f}h**, Trim Silence: **{time_silence_hrs:,.1f}h**)",
            f"- **Active Subscribed Shows ({len(podcasts)} feeds):** " + ", ".join(f"**{t}**" for t in sub_titles),
        ]
        if up_next_eps:
            pc_lines.append("- **Up Next Queue:** " + "; ".join(f"*{u['title']}*" for u in up_next_eps[:6]))
        pc_lines.append("- **Recent Listening History (Latest 15 Played Episodes):**")
        for h in history_eps[:15]:
            pc_lines.append(f"  - **{h['published']}** — **{h['podcast']}**: *{h['title']}* ({h['duration_min']} min)")

        raw_media = media_file.read_text(encoding="utf-8")
        media_file.write_text(
            upsert_markdown_section(raw_media, "## 7. 🎙️ Live Pocket Casts", "\n".join(pc_lines)),
            encoding="utf-8",
        )

    return True, f"{len(podcasts)} feeds, {time_listened_hrs:,.1f}h lifetime listening, {len(history_eps)} recent episodes synced"


def run_personal_data_sync(test: bool = False, quiet: bool = True) -> tuple[bool, str, dict]:
    """Execute full personal data & memory profile synchronization pass."""
    results: dict[str, str] = {}
    errors: list[str] = []

    # 1. Steam Web API
    try:
        from tools.steam_library import main as sync_steam
        steam_data = sync_steam(force_refresh=True)
        results["steam"] = f"{steam_data.get('total_games', 0)} games synced"
    except Exception as e:
        errors.append(f"Steam: {e}")

    # 2. Xbox OpenXBL API
    try:
        from tools.xbox_library import main as sync_xbox
        acc, titles = sync_xbox(force_refresh=True)
        results["xbox"] = f"{len(titles)} titles, {acc.get('gamerscore')} G synced"
    except Exception as e:
        errors.append(f"Xbox: {e}")

    # 3. Nintendo Switch 2 Moon API
    try:
        from tools.switch_library import main as sync_switch
        sw_data = sync_switch()
        devs = sw_data.get("devices", [])
        dev_id = devs[0].get("deviceId") if devs else ""
        daily_cnt = len(((sw_data.get("summaries", {}).get(dev_id) or {}).get("daily") or {}).get("items", []))
        results["switch"] = f"{len(devs)} console(s), {daily_cnt} daily summaries synced"
    except Exception as e:
        errors.append(f"Switch: {e}")

    # 4. YouTube / YouTube Music (Prime416)
    try:
        ok_yt, msg_yt = sync_youtube_prime416_profile()
        if ok_yt:
            results["youtube"] = msg_yt
        else:
            errors.append(f"YouTube: {msg_yt}")
    except Exception as e:
        errors.append(f"YouTube: {e}")

    # 5. Pocket Casts API
    try:
        ok_pc, msg_pc = sync_pocketcasts_profile()
        if ok_pc:
            results["pocketcasts"] = msg_pc
        else:
            errors.append(f"Pocket Casts: {msg_pc}")
    except Exception as e:
        errors.append(f"Pocket Casts: {e}")

    # 6. Amazon Order Rollup
    try:
        ok_amz, msg_amz = sync_amazon_purchasing_profile()
        if ok_amz:
            results["amazon"] = msg_amz
        else:
            errors.append(f"Amazon: {msg_amz}")
    except Exception as e:
        errors.append(f"Amazon: {e}")

    # 7. Plex / Tautulli Watch History
    try:
        ok_plex, msg_plex = sync_plex_taste_profile()
        if ok_plex:
            results["plex"] = msg_plex
        else:
            errors.append(f"Plex: {msg_plex}")
    except Exception as e:
        errors.append(f"Plex: {e}")

    # 7. Incremental Hybrid Memory Indexer (transcript_index.db — memory-only fast path)
    try:
        from collections import defaultdict
        from tools.transcript_index import TranscriptIndexer
        idx_engine = TranscriptIndexer()
        mem_stats: dict[str, int] = defaultdict(int)
        idx_engine._sync_memory(force=False, stats=mem_stats, embed=False)
        idx_engine.close()
        results["memory_index"] = f"transcript_index.db updated ({mem_stats['memory_files_indexed']} modified memory file(s) indexed)"
    except Exception as e:
        errors.append(f"Indexer: {e}")

    ok = len(errors) == 0
    summary_parts = [f"**{k.capitalize()}:** {v}" for k, v in results.items()]
    if errors:
        summary_parts.append(f"**Warnings:** {'; '.join(errors)}")
    summary_str = " | ".join(summary_parts)

    if not ok:
        report = f"⚠️ **Personal Data Sync Degraded:** {summary_str}"
    elif not quiet:
        report = "✅ **Personal Data Sync Complete:**\n- " + "\n- ".join(summary_parts)
    else:
        report = ""

    return ok, report, {"results": results, "errors": errors, "summary": summary_str}


if __name__ == "__main__":
    is_test = "--test" in sys.argv or "-t" in sys.argv
    is_quiet = "--quiet" in sys.argv or "-q" in sys.argv
    ok, rep, meta = run_personal_data_sync(test=is_test, quiet=is_quiet)
    if rep:
        print(rep)
    else:
        print(f"(nominal - {meta.get('summary', 'all personal profiles synced')})")
    sys.exit(0 if ok else 1)
