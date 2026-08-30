#!/usr/bin/env python3
"""Steam Library Ingestion & Gaming Profile Tool for Zero.

Fetches complete game library and playtime stats via Steam Web API.
Parses modern Steam Cloud user collections from /workspace/data/cloud-storage-namespace-1.json:
- Tier A (26 games: Play Next)
- Tier B (43 games: On Deck)
- Tier C (70 games: Tertiary Backlog)
- Tier D (171 games: Deep Backlog)
- Tier F (65 games: Cold Storage)
- Played (214 games: Finished / Experienced)
- A-coop (6 games: Priority Multiplayer)
- Multi (55 games: Multiplayer Catalog)
- Rosie (44 games: Kids / Family)
- Hidden (57 games)
Generates structured gaming taste profile for /workspace/memory/media_gaming_steam_profile.md.
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
PROFILE_MD = Path("/workspace/memory/media_gaming_steam_profile.md")
PROFILE_MD_LINK = Path("/workspace/.agents/memory/media_gaming_steam_profile.md")

STEAM_API_KEY = "20CBA3E1E0D5DF1A683B968F2739B43B"
STEAM_ID = "76561197975996046"

KNOWN_EXTRA_APPS = {
    238960: "Path of Exile",
    553850: "HELLDIVERS™ 2",
    1118310: "RetroArch",
    205790: "Dota 2 Beta",
    1083500: "Half-Life: Alyx Non-VR Mod",
    950670: "Black Mesa: Xen Beta"
}

def fetch_owned_games():
    url = f"http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?key={STEAM_API_KEY}&steamid={STEAM_ID}&format=json&include_appinfo=1&include_played_free_games=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
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

def main(force_refresh=False):
    games = []
    if not force_refresh and GAMES_JSON.exists():
        try:
            with open(GAMES_JSON, "r", encoding="utf-8") as f:
                cached = json.load(f)
                games = cached.get("games", [])
                print(f"[Steam] Loaded {len(games)} games from local cache.")
        except Exception:
            pass

    if not games:
        print("[Steam] Fetching owned games via Web API...")
        games = fetch_owned_games()
        print(f"[Steam] Retrieved {len(games)} owned games.")

    print("[Steam] Parsing modern cloud-storage collections...")
    collections = parse_cloud_collections()
    print(f"[Steam] Parsed collections: { {k: len(v) for k, v in collections.items()} }")

    # Map appid to game
    app_map = {g["appid"]: g for g in games}

    # Ensure extra apps exist
    for cname, appids in collections.items():
        for aid in appids:
            if aid not in app_map:
                name = KNOWN_EXTRA_APPS.get(aid, f"AppID {aid}")
                extra_game = {
                    "appid": aid,
                    "name": name,
                    "playtime_forever": 0,
                    "tags": []
                }
                games.append(extra_game)
                app_map[aid] = extra_game

    # Tag games
    for g in games:
        g["collections"] = []
    for cname, appids in collections.items():
        for aid in appids:
            if aid in app_map and cname not in app_map[aid]["collections"]:
                app_map[aid]["collections"].append(cname)

    # Save raw database
    payload = {
        "steam_id": STEAM_ID,
        "persona_name": "Firebird / skypiea",
        "fetched_at": datetime.now(PT).isoformat(),
        "total_games": len(games),
        "collections_summary": {k: len(v) for k, v in collections.items()},
        "games": games
    }
    with open(GAMES_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[Steam] Saved raw database to {GAMES_JSON}")

    # Compute playtime analytics
    played_games = [g for g in games if g.get("playtime_forever", 0) > 0]
    unplayed_games = [g for g in games if g.get("playtime_forever", 0) == 0]
    total_mins = sum(g.get("playtime_forever", 0) for g in games)
    total_hours = total_mins / 60.0

    played_games.sort(key=lambda g: g.get("playtime_forever", 0), reverse=True)

    # Breakdown by collection
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

    # Format Markdown Synthesis
    md = []
    md.append("# Ryan Brock: Steam Gaming Profile & Taste Synthesis")
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    md.append(f"*Ingested on {now_str} via Steam Web API & live `cloud-storage-namespace-1.json` (`steamid: {STEAM_ID}`, Handle: `Firebird` / `skypiea`, Account created 2005).*\n")
    md.append("---")
    md.append("## 1. High-Level Gaming Telemetry & Tier Breakdown")
    md.append(f"- **Total Library Size:** {len(games)} games")
    md.append(f"- **Total Logged Playtime:** {total_hours:,.1f} hours ({total_mins:,} minutes / ~{total_hours/24:.0f} cumulative days)")
    md.append(f"- **Active Tiers Overview:**")
    md.append(f"  • **Tier A (Priority Play Next):** {len(tier_a)} games")
    md.append(f"  • **Tier B (On Deck):** {len(tier_b)} games")
    md.append(f"  • **Tier C (Tertiary Backlog):** {len(tier_c)} games")
    md.append(f"  • **Tier D (Quaternary / Deep Backlog):** {len(tier_d)} games")
    md.append(f"  • **Tier F (Cold Storage):** {len(tier_f)} games")
    md.append(f"  • **Played (Completed / Experienced):** {len(col_played)} games")
    md.append(f"  • **A-coop (Priority Multiplayer):** {len(col_coop)} games")
    md.append(f"  • **Multi (Multiplayer Catalog):** {len(col_multi)} games")
    md.append(f"  • **Rosie (Family / Kids):** {len(col_rosie)} games")
    md.append(f"  • **Hidden:** {len(col_hidden)} games")
    md.append("")
    md.append("---")
    md.append("## 2. Tier A — Priority Target Queue (26 Games)")
    md.append("*The games Ryan has actively flagged as his top priority to play next:*")
    md.append("")
    for g in tier_a:
        hrs = g.get('playtime_forever', 0) / 60.0
        hrs_str = f"{hrs:.1f} hrs logged" if hrs > 0 else "Unplayed"
        name = g.get('name', 'Unknown')
        aid = g.get('appid', '')
        md.append(f"- **{name}** (`AppID: {aid}`): **{hrs_str}**")
    md.append("")
    md.append("---")
    md.append("## 3. Tier B — On Deck (43 Games)")
    md.append("*The secondary queue queued up right behind Tier A:*")
    md.append("")
    for g in tier_b:
        hrs = g.get('playtime_forever', 0) / 60.0
        hrs_str = f"{hrs:.1f} hrs logged" if hrs > 0 else "Unplayed"
        name = g.get('name', 'Unknown')
        aid = g.get('appid', '')
        md.append(f"- **{name}** (`AppID: {aid}`): **{hrs_str}**")
    md.append("")
    md.append("---")
    md.append("## 4. Priority Co-op & Multiplayer (A-coop)")
    for g in col_coop:
        hrs = g.get('playtime_forever', 0) / 60.0
        name = g.get('name', 'Unknown')
        md.append(f"- **{name}** ({hrs:.1f} hrs)")
    md.append("")
    md.append("---")
    md.append("## 5. Category 'Played' (214 Completed / Retired Games)")
    md.append(f"*Top 30 most-played among your {len(col_played)} retired titles:*")
    for g in col_played[:30]:
        hrs = g.get('playtime_forever', 0) / 60.0
        name = g.get('name', 'Unknown')
        md.append(f"- **{name}**: {hrs:.1f} hrs")
    md.append("")
    md.append("---")
    md.append("## 6. The All-Time Pantheon (Top 20 Most Played Across Entire Account)")
    for idx, g in enumerate(played_games[:20], 1):
        hrs = g.get('playtime_forever', 0) / 60.0
        last_played = g.get('rtime_last_played', 0)
        lp_str = datetime.fromtimestamp(last_played, tz=timezone.utc).astimezone(PT).strftime("%Y-%m-%d") if last_played else "N/A"
        name = g.get('name', 'Unknown')
        md.append(f"{idx:2d}. **{name}** — **{hrs:,.1f} hrs** (Last played: {lp_str})")
    md.append("")
    md.append("---")
    md.append("## 7. Zero's Tactical Analysis of Tier A")
    md.append("1. **The In-Progress Blockbusters:**")
    md.append("   • **Hollow Knight: Silksong (63.7 hrs):** High mastery, deep mechanical progression.")
    md.append("   • **Mass Effect™ Legendary Edition (22.0 hrs):** Premier space opera narrative.")
    md.append("   • **Deep Rock Galactic: Survivor (22.0 hrs):** Fast-paced tactical mining auto-shooter.")
    md.append("   • **Yooka-Laylee (17.8 hrs):** 3D platformer comfort collection.")
    md.append("   • **STAR WARS™ KOTOR II (14.2 hrs):** Obsidian narrative masterpiece.")
    md.append("2. **The Unstarted Heavyweights (0.0 hrs logged in Tier A):**")
    md.append("   • **Red Dead Redemption 2:** Masterclass open-world storytelling & pacing.")
    md.append("   • **Dwarf Fortress:** The ultimate complex systems, emergent narrative, and logistics simulation.")
    md.append("   • **Return of the Obra Dinn:** Lucas Pope logical deduction masterpiece (pure failure-analysis forensics!).")
    md.append("   • **Warhammer 40k: Rogue Trader:** Deep Owlcat CRPG matching your D&D DM narrative appetite.")
    md.append("   • **Kingdom Come: Deliverance:** Unforgiving historical simulation.")
    md.append("   • **Hogwarts Legacy:** Immersive world-building and exploration.")
    md.append("   • **Total War: WARHAMMER III:** Macro-strategic tactical command.")

    content = "\n".join(md) + "\n"
    with open(PROFILE_MD, "w", encoding="utf-8") as f:
        f.write(content)
    with open(PROFILE_MD_LINK, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[Steam] Successfully generated {PROFILE_MD} with all modern cloud collections!")

if __name__ == "__main__":
    main()
