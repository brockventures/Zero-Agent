#!/usr/bin/env python3
"""Steam Library Ingestion & Gaming Profile Tool for Zero.

Fetches complete game library and playtime stats via Steam Web API.
Parses modern Steam Cloud user collections from /workspace/data/cloud-storage-namespace-1.json:
- Tier A (Play Next)
- Tier B (On Deck)
- Tier C (Tertiary Backlog)
- Tier D (Deep Backlog)
- Tier F (Cold Storage)
- Played (Finished / Experienced)
- A-coop (Priority Multiplayer)
- Multi (Multiplayer Catalog)
- Rosie (Kids / Family)
- Hidden
Generates structured gaming taste profile for /workspace/memory/private/media_gaming_steam_profile.md.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
GAMES_JSON = DATA_DIR / "steam_games.json"
CLOUD_JSON = DATA_DIR / "cloud-storage-namespace-1.json"
PROFILE_MD = Path("/workspace/memory/private/media_gaming_steam_profile.md")
LEGACY_PROFILE_MD = Path("/workspace/memory/media_gaming_steam_profile.md")
PROFILE_MD_LINK = Path("/workspace/.agents/memory/media_gaming_steam_profile.md")

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "20CBA3E1E0D5DF1A683B968F2739B43B")
STEAM_ID = os.environ.get("STEAM_ID", "76561197975996046")

KNOWN_EXTRA_APPS = {
    238960: "Path of Exile",
    553850: "HELLDIVERS™ 2",
    1118310: "RetroArch",
    205790: "Dota 2 Beta",
    1083500: "Half-Life: Alyx Non-VR Mod",
    950670: "Black Mesa: Xen Beta",
}


def fetch_owned_games():
    url = (
        f"http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
        f"?key={STEAM_API_KEY}&steamid={STEAM_ID}&format=json&include_appinfo=1&include_played_free_games=1"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("response", {}).get("games", [])


def parse_cloud_collections():
    if not CLOUD_JSON.exists():
        return {}
    with open(CLOUD_JSON, "r", encoding="utf-8") as f:
        items = json.load(f)

    collections = {}
    for entry in items:
        if isinstance(entry, list) and len(entry) == 2:
            k, v = entry
            if isinstance(v, dict) and "user-collections" in k:
                val_str = v.get("value", "{}")
                try:
                    cdata = json.loads(val_str)
                    name = cdata.get("name") or cdata.get("id")
                    added = set(cdata.get("added", []))
                    removed = set(cdata.get("removed", []))
                    actual = list(added - removed)
                    if name and name != "None":
                        collections[name] = actual
                except Exception:
                    pass
    return collections


def main(force_refresh=True):
    games = []
    if force_refresh or not GAMES_JSON.exists():
        try:
            print("[Steam] Fetching owned games via Web API...")
            games = fetch_owned_games()
            print(f"[Steam] Retrieved {len(games)} owned games from Steam Web API.")
        except Exception as e:
            print(f"[Steam] Warning: Web API fetch failed ({e}); falling back to local cache.", file=sys.stderr)

    if not games and GAMES_JSON.exists():
        try:
            with open(GAMES_JSON, "r", encoding="utf-8") as f:
                cached = json.load(f)
                games = cached.get("games", [])
                print(f"[Steam] Loaded {len(games)} games from local cache.")
        except Exception:
            pass

    collections = parse_cloud_collections()
    app_map = {g["appid"]: g for g in games}

    for cname, appids in collections.items():
        for aid in appids:
            if aid not in app_map:
                name = KNOWN_EXTRA_APPS.get(aid, f"AppID {aid}")
                extra_game = {
                    "appid": aid,
                    "name": name,
                    "playtime_forever": 0,
                    "tags": [],
                }
                games.append(extra_game)
                app_map[aid] = extra_game

    for g in games:
        g["collections"] = []
    for cname, appids in collections.items():
        for aid in appids:
            if aid in app_map and cname not in app_map[aid]["collections"]:
                app_map[aid]["collections"].append(cname)

    payload = {
        "steam_id": STEAM_ID,
        "persona_name": "Firebird / skypiea",
        "fetched_at": datetime.now(PT).isoformat(),
        "total_games": len(games),
        "collections_summary": {k: len(v) for k, v in collections.items()},
        "games": games,
    }
    GAMES_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(GAMES_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[Steam] Saved raw database to {GAMES_JSON}")

    played_games = [g for g in games if g.get("playtime_forever", 0) > 0]
    total_mins = sum(g.get("playtime_forever", 0) for g in games)
    total_hours = total_mins / 60.0

    played_games.sort(key=lambda g: g.get("playtime_forever", 0), reverse=True)
    recent_played = sorted(
        [g for g in games if g.get("rtime_last_played", 0) > 0],
        key=lambda g: g.get("rtime_last_played", 0),
        reverse=True,
    )

    tier_a = [app_map[aid] for aid in collections.get("A", []) if aid in app_map]
    tier_b = [app_map[aid] for aid in collections.get("B", []) if aid in app_map]
    tier_c = [app_map[aid] for aid in collections.get("C", []) if aid in app_map]
    tier_d = [app_map[aid] for aid in collections.get("D", []) if aid in app_map]
    tier_f = [app_map[aid] for aid in collections.get("F", []) if aid in app_map]
    col_played = [app_map[aid] for aid in collections.get("Played", []) if aid in app_map]
    col_coop = [app_map[aid] for aid in collections.get("A-coop", []) if aid in app_map]
    col_multi = [app_map[aid] for aid in collections.get("Multi", []) if aid in app_map]
    col_rosie = [app_map[aid] for aid in collections.get("Rosie", []) if aid in app_map]
    col_hidden = [app_map[aid] for aid in collections.get("Hidden", []) if aid in app_map]

    tier_a.sort(key=lambda g: g.get("playtime_forever", 0), reverse=True)
    tier_b.sort(key=lambda g: g.get("playtime_forever", 0), reverse=True)
    col_played.sort(key=lambda g: g.get("playtime_forever", 0), reverse=True)

    md = []
    md.append("# Ryan Brock: Steam Gaming Profile & Taste Synthesis")
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    md.append(
        f"*Synced on {now_str} via Steam Web API & `cloud-storage-namespace-1.json` "
        f"(`steamid: {STEAM_ID}`, Handle: `Firebird` / `skypiea`, Account created 2005).*\n"
    )
    md.append("---")
    md.append("## 1. High-Level Gaming Telemetry & Tier Breakdown")
    md.append(f"- **Total Library Size:** {len(games)} games")
    md.append(f"- **Total Logged Playtime:** {total_hours:,.1f} hours ({total_mins:,} minutes / ~{total_hours/24:.0f} cumulative days)")
    md.append("- **Active Tiers Overview:**")
    md.append(f"  - **Tier A (Priority Play Next):** {len(tier_a)} games")
    md.append(f"  - **Tier B (On Deck):** {len(tier_b)} games")
    md.append(f"  - **Tier C (Tertiary Backlog):** {len(tier_c)} games")
    md.append(f"  - **Tier D (Quaternary / Deep Backlog):** {len(tier_d)} games")
    md.append(f"  - **Tier F (Cold Storage):** {len(tier_f)} games")
    md.append(f"  - **Played (Completed / Experienced):** {len(col_played)} games")
    md.append(f"  - **A-coop (Priority Multiplayer):** {len(col_coop)} games")
    md.append(f"  - **Multi (Multiplayer Catalog):** {len(col_multi)} games")
    md.append(f"  - **Rosie (Family / Kids):** {len(col_rosie)} games")
    md.append(f"  - **Hidden:** {len(col_hidden)} games")
    md.append("")
    if recent_played:
        md.append("---")
        md.append("## 1B. Most Recently Played on Steam (Live Telemetry)")
        for g in recent_played[:12]:
            hrs = g.get("playtime_forever", 0) / 60.0
            lp = g.get("rtime_last_played", 0)
            lp_str = datetime.fromtimestamp(lp, tz=timezone.utc).astimezone(PT).strftime("%Y-%m-%d")
            md.append(f"- **{g.get('name', 'Unknown')}** — **{hrs:,.1f} hrs total** (Last played: {lp_str})")
        md.append("")
    md.append("---")
    md.append(f"## 2. Tier A — Priority Target Queue ({len(tier_a)} Games)")
    md.append("*The games Ryan has actively flagged as his top priority to play next:*")
    md.append("")
    for g in tier_a:
        hrs = g.get("playtime_forever", 0) / 60.0
        hrs_str = f"{hrs:.1f} hrs logged" if hrs > 0 else "Unplayed"
        name = g.get("name", "Unknown")
        aid = g.get("appid", "")
        md.append(f"- **{name}** (`AppID: {aid}`): **{hrs_str}**")
    md.append("")
    md.append("---")
    md.append(f"## 3. Tier B — On Deck ({len(tier_b)} Games)")
    md.append("*The secondary queue queued up right behind Tier A:*")
    md.append("")
    for g in tier_b:
        hrs = g.get("playtime_forever", 0) / 60.0
        hrs_str = f"{hrs:.1f} hrs logged" if hrs > 0 else "Unplayed"
        name = g.get("name", "Unknown")
        aid = g.get("appid", "")
        md.append(f"- **{name}** (`AppID: {aid}`): **{hrs_str}**")
    md.append("")
    md.append("---")
    md.append("## 4. Priority Co-op & Multiplayer (A-coop)")
    for g in col_coop:
        hrs = g.get("playtime_forever", 0) / 60.0
        name = g.get("name", "Unknown")
        md.append(f"- **{name}** ({hrs:.1f} hrs)")
    md.append("")
    md.append("---")
    md.append(f"## 5. Category 'Played' ({len(col_played)} Completed / Retired Games)")
    md.append(f"*Top 30 most-played among your {len(col_played)} retired titles:*")
    for g in col_played[:30]:
        hrs = g.get("playtime_forever", 0) / 60.0
        name = g.get("name", "Unknown")
        md.append(f"- **{name}**: {hrs:.1f} hrs")
    md.append("")
    md.append("---")
    md.append("## 6. The All-Time Pantheon (Top 20 Most Played Across Entire Account)")
    for idx, g in enumerate(played_games[:20], 1):
        hrs = g.get("playtime_forever", 0) / 60.0
        last_played = g.get("rtime_last_played", 0)
        lp_str = datetime.fromtimestamp(last_played, tz=timezone.utc).astimezone(PT).strftime("%Y-%m-%d") if last_played else "N/A"
        name = g.get("name", "Unknown")
        md.append(f"{idx:2d}. **{name}** — **{hrs:,.1f} hrs** (Last played: {lp_str})")
    md.append("")

    content = "\n".join(md) + "\n"
    PROFILE_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(PROFILE_MD, "w", encoding="utf-8") as f:
        f.write(content)
    for extra_path in (LEGACY_PROFILE_MD, PROFILE_MD_LINK):
        try:
            if extra_path.parent.exists():
                with open(extra_path, "w", encoding="utf-8") as f:
                    f.write(content)
        except Exception:
            pass
    print(f"[Steam] Successfully generated {PROFILE_MD} ({len(games)} games, {total_hours:,.1f} hrs)!")
    return payload


if __name__ == "__main__":
    no_refresh = "--cached" in sys.argv
    main(force_refresh=not no_refresh)
