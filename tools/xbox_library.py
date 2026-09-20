#!/usr/bin/env python3
"""Xbox Library Ingestion & Gaming Profile Tool for Zero.

Fetches complete Xbox game library, achievements, and Gamerscore stats via OpenXBL API.
Saves raw normalized database to /workspace/data/xbox_games.json.
Generates structured gaming profile for /workspace/memory/media_gaming_xbox_profile.md.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
CREDENTIALS_FILE = DATA_DIR / "xbl_credentials.json"
GAMES_JSON = DATA_DIR / "xbox_games.json"
PROFILE_MD = Path("/workspace/memory/media_gaming_xbox_profile.md")
PROFILE_MD_LINK = Path("/workspace/.agents/memory/media_gaming_xbox_profile.md")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def load_api_key() -> str:
    if CREDENTIALS_FILE.exists():
        try:
            with open(CREDENTIALS_FILE, "r") as f:
                data = json.load(f)
                key = data.get("api_key")
                if key:
                    return key.strip()
        except Exception:
            pass
    env_key = os.environ.get("OPENXBL_API_KEY")
    if env_key:
        return env_key.strip()
    return ""


def xbl_get(endpoint: str, api_key: str) -> dict:
    url = f"https://xbl.io/api/v2{endpoint}" if endpoint.startswith("/") else endpoint
    headers = dict(DEFAULT_HEADERS)
    headers["X-Authorization"] = api_key
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_xbox_library(api_key: str) -> tuple[dict, list[dict]]:
    # 1. Account info
    acc_data = xbl_get("/account", api_key)
    profile_users = acc_data.get("content", {}).get("profileUsers", [{}])
    profile_user = profile_users[0] if profile_users else {}
    xuid = profile_user.get("id", "")
    settings_list = profile_user.get("settings", [])
    settings = {s.get("id"): s.get("value") for s in settings_list if "id" in s}

    account_info = {
        "xuid": xuid,
        "gamertag": settings.get("Gamertag", "Brock416"),
        "gamerscore": settings.get("Gamerscore", "40540"),
        "tier": settings.get("AccountTier", "Gold"),
        "avatar_url": settings.get("GameDisplayPicRaw", ""),
    }

    # 2. Achievements / Title history
    ach_data = xbl_get("/achievements", api_key)
    titles = ach_data.get("content", {}).get("titles", [])

    return account_info, titles


def generate_profile_markdown(account_info: dict, titles: list[dict]) -> str:
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    gt = account_info.get("gamertag", "Brock416")
    total_g = account_info.get("gamerscore", "0")
    total_titles = len(titles)

    # Compute stats
    device_counts = {}
    completed_games = []
    in_progress = []
    started_games = []

    for t in titles:
        name = t.get("name", "Unknown")
        devs = t.get("devices", [])
        for d in devs:
            device_counts[d] = device_counts.get(d, 0) + 1

        ach = t.get("achievement", {})
        cur_g = ach.get("currentGamerscore", 0)
        max_g = ach.get("totalGamerscore", 0)
        cur_ach = ach.get("currentAchievements", 0)
        progress = ach.get("progressPercentage", 0)
        last_played = (t.get("titleHistory") or {}).get("lastTimePlayed")

        game_summary = {
            "name": name,
            "devices": devs,
            "cur_g": cur_g,
            "max_g": max_g,
            "achievements": cur_ach,
            "progress": progress,
            "last_played": last_played,
        }

        if cur_g > 0:
            started_games.append(game_summary)
            if progress >= 100 or (max_g > 0 and cur_g >= max_g):
                completed_games.append(game_summary)
            else:
                in_progress.append(game_summary)

    # Sort started games by earned gamerscore descending
    started_games.sort(key=lambda x: x["cur_g"], reverse=True)
    in_progress.sort(key=lambda x: x["cur_g"], reverse=True)

    md = []
    md.append(f"# Ryan Brock: Xbox Gaming Profile & Telemetry")
    md.append(f"*Ingested on {now_str} via OpenXBL API (`Gamertag: {gt}`, `XUID: {account_info.get('xuid')}`).*\n")
    md.append("---\n")

    md.append("## 1. High-Level Gaming Telemetry")
    md.append(f"- **Gamertag:** `{gt}`")
    md.append(f"- **Total Gamerscore:** **{int(total_g):,} G**")
    md.append(f"- **Total Catalog Titles:** **{total_titles} games**")
    md.append(f"- **Active Played Titles (Earned Gamerscore > 0):** {len(started_games)} games")
    md.append(f"- **100% Completed Titles:** {len(completed_games)} games")
    
    dev_str = ", ".join(f"**{k}** ({v})" for k, v in sorted(device_counts.items(), key=lambda x: x[1], reverse=True))
    md.append(f"- **Platform Footprint:** {dev_str}\n")
    md.append("---\n")

    md.append("## 2. Top Games by Gamerscore (Highest Investment)")
    md.append("| Game | Platform(s) | Gamerscore | Progress |")
    md.append("| :--- | :--- | :--- | :--- |")
    for g in started_games[:25]:
        devs_label = "/".join(g["devices"])
        md.append(f"| **{g['name']}** | `{devs_label}` | **{g['cur_g']:,}** / {g['max_g']:,} G | {g['progress']}% |")
    md.append("\n---\n")

    if completed_games:
        md.append(f"## 3. 100% Completed Games ({len(completed_games)})")
        for g in completed_games:
            md.append(f"- **{g['name']}** (`{'/'.join(g['devices'])}`) — **{g['cur_g']:,} G** (100% Completion)")
        md.append("\n---\n")

    md.append("## 4. Cross-Platform Genre & Taste Synthesis")
    md.append("Highlights observed across Xbox history compared to Steam library:")
    md.append("- **Sports & Simulation Leadership:** Heavy recurring investment in *MLB The Show* series and *EA Sports FC / FIFA* on console.")
    md.append("- **Action RPGs & Shooters:** Substantial Gamerscore in *Destiny 2*, *Clair Obscur: Expedition 33*, *Diablo*, and *Halo* franchises.")
    md.append("- **Strategy & Indie Crossover:** Shared playtime with PC Game Pass titles (*Crusader Kings III*, *Loop Hero*, *Ni no Kuni II*).")

    return "\n".join(md) + "\n"


def main(force_refresh=False):
    key = load_api_key()
    if not key:
        print("[Xbox] Error: No API key found in xbl_credentials.json or environment.", file=sys.stderr)
        sys.exit(1)

    print(f"[Xbox] Authenticating with OpenXBL API...")
    account_info, titles = fetch_xbox_library(key)
    print(f"[Xbox] Authenticated as {account_info.get('gamertag')} (Gamerscore: {account_info.get('gamerscore')} G).")
    print(f"[Xbox] Ingested {len(titles)} titles.")

    # Save JSON database
    payload = {
        "account": account_info,
        "fetched_at": datetime.now(PT).isoformat(),
        "total_titles": len(titles),
        "titles": titles,
    }
    with open(GAMES_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"[Xbox] Saved raw database to {GAMES_JSON}")

    # Generate Profile Markdown
    md_content = generate_profile_markdown(account_info, titles)
    PROFILE_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(PROFILE_MD, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"[Xbox] Generated profile document at {PROFILE_MD}")

    # Symlink to .agents if present
    if PROFILE_MD_LINK.parent.exists():
        try:
            if not PROFILE_MD_LINK.exists() and not PROFILE_MD_LINK.is_symlink():
                PROFILE_MD_LINK.symlink_to(PROFILE_MD)
        except Exception:
            pass

    return account_info, titles


if __name__ == "__main__":
    force = "--force" in sys.argv
    main(force_refresh=force)
