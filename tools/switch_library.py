#!/usr/bin/env python3
"""Nintendo Switch 2 Library & Profile Tool for Zero (Direct Moon REST Client).

Directly queries Nintendo Account OAuth and Nintendo Switch Parental Controls (Moon) REST APIs
in pure Python (eliminating the external Node/nxapi CLI dependency and handling version gates).
Saves cumulative normalized database to /workspace/data/switch_data.json.
Generates structured gaming profile for /workspace/memory/private/media_gaming_switch_profile.md.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
SWITCH_JSON = DATA_DIR / "switch_data.json"
PROFILE_MD = Path("/workspace/memory/private/media_gaming_switch_profile.md")
PROFILE_SYMLINK = Path("/workspace/memory/media_gaming_switch_profile.md")
MOON_TOKEN_FILE = DATA_DIR / "nxapi_moon_session_token.json"

ZNMA_CLIENT_ID = "54789befb391a838"
ZNMA_DISPLAY_VERSION = "2.3.0"
ZNMA_INTERNAL_BUILD = "9999"
DEFAULT_NA_ID = "f6d99ef26fc11a99"


def is_authenticated() -> bool:
    return MOON_TOKEN_FILE.exists()


def exchange_session_token(session_token: str) -> str:
    url = "https://accounts.nintendo.com/connect/1.0.0/api/token"
    body = json.dumps({
        "client_id": ZNMA_CLIENT_ID,
        "session_token": session_token,
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer-session-token",
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "OnlineLounge/2.10.0 NASDKAPI Android",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["access_token"]


def moon_get(path: str, access_token: str) -> dict:
    url = f"https://api-lp1.pctl.srv.nintendo.net/moon/v1{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Cache-Control": "no-store",
            "Content-Type": "application/json; charset=utf-8",
            "X-Moon-App-Id": "com.nintendo.znma",
            "X-Moon-Os": "ANDROID",
            "X-Moon-Os-Version": "34",
            "X-Moon-Model": "Pixel 8 Pro",
            "X-Moon-TimeZone": "America/Los_Angeles",
            "X-Moon-Os-Language": "en-US",
            "X-Moon-App-Language": "en-US",
            "X-Moon-App-Display-Version": ZNMA_DISPLAY_VERSION,
            "X-Moon-App-Internal-Version": ZNMA_INTERNAL_BUILD,
            "User-Agent": f"moon_ANDROID/{ZNMA_DISPLAY_VERSION} (com.nintendo.znma; build:{ZNMA_INTERNAL_BUILD}; ANDROID 34)",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_switch_data() -> dict:
    with open(MOON_TOKEN_FILE, "r", encoding="utf-8") as f:
        tdata = json.load(f)
    session_token = tdata.get("session_token") or tdata.get("token")
    account_id = tdata.get("sub") or DEFAULT_NA_ID

    access_token = exchange_session_token(session_token)

    # Load previous switch_data.json to preserve historical daily summaries beyond the 31-day rolling window
    existing_daily_by_dev: dict[str, dict[str, dict]] = {}
    if SWITCH_JSON.exists():
        try:
            prev = json.load(open(SWITCH_JSON, "r", encoding="utf-8"))
            for d_id, s_obj in (prev.get("summaries") or {}).items():
                for item in (s_obj.get("daily") or {}).get("items", []):
                    if item.get("date"):
                        existing_daily_by_dev.setdefault(d_id, {})[item["date"]] = item
        except Exception:
            pass

    devs_resp = moon_get(f"/users/{account_id}/devices", access_token)
    devices = devs_resp.get("items", [])

    summaries = {}
    for dev in devices:
        dev_id = dev.get("deviceId")
        if not dev_id:
            continue
        dev_data = {}
        try:
            daily_live = moon_get(f"/devices/{dev_id}/daily_summaries", access_token)
            merged_map = dict(existing_daily_by_dev.get(dev_id, {}))
            for item in daily_live.get("items", []):
                if item.get("date"):
                    merged_map[item["date"]] = item
            merged_items = [merged_map[k] for k in sorted(merged_map.keys(), reverse=True)]
            daily_live["items"] = merged_items
            daily_live["count"] = len(merged_items)
            dev_data["daily"] = daily_live
        except Exception as e:
            print(f"[Switch] Warning fetching daily_summaries: {e}", file=sys.stderr)
            dev_data["daily"] = None

        try:
            monthly_idx = moon_get(f"/devices/{dev_id}/monthly_summaries", access_token)
            monthly_details = {}
            for m_str in (monthly_idx.get("indexes") or [])[:6]:
                try:
                    monthly_details[m_str] = moon_get(f"/devices/{dev_id}/monthly_summaries/{m_str}", access_token)
                except Exception:
                    pass
            monthly_idx["details"] = monthly_details
            dev_data["monthly"] = monthly_idx
        except Exception as e:
            print(f"[Switch] Warning fetching monthly_summaries: {e}", file=sys.stderr)
            dev_data["monthly"] = None

        summaries[dev_id] = dev_data

    return {
        "fetched_at": datetime.now(PT).isoformat(),
        "account_id": account_id,
        "devices": devices,
        "summaries": summaries,
    }


def generate_profile_markdown(data: dict) -> str:
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    lines = [
        "# Ryan Brock: Nintendo Switch 2 Profile & Telemetry",
        f"*Synced on {now_str} via Nintendo Switch Parental Controls (Moon) REST API.*\n",
        "---",
        "",
        "## 1. Hardware & Console Registry",
    ]

    devices = data.get("devices", [])
    if not devices:
        lines.append("*(No active consoles linked to Parental Controls)*")
    else:
        for idx, dev in enumerate(devices, 1):
            info = dev.get("device", {})
            fw = info.get("firmwareVersion", {})
            fw_str = f"{fw.get('displayedVersion')} (internal {fw.get('internalVersion')})" if fw else "Unknown"
            last_sync = info.get("synchronizedParentalControlSetting", {}).get("synchronizedAt")
            last_sync_str = datetime.fromtimestamp(last_sync, PT).strftime("%Y-%m-%d %I:%M:%S %p PT") if last_sync else "Unknown"

            lines.extend([
                f"### Console {idx}: **{dev.get('label', 'Nintendo Switch 2')}**",
                f"- **Device ID:** `{dev.get('deviceId')}`",
                f"- **Hardware Generation:** `{info.get('platformGeneration', 'Unknown')}`",
                f"- **Serial Number:** `{info.get('serialNumber', 'Unknown')}`",
                f"- **Firmware Version:** `{fw_str}`",
                f"- **Region / Language:** `{info.get('region')} / {info.get('language')}`",
                f"- **Master Unlock PIN:** `{info.get('synchronizedUnlockCode', 'N/A')}`",
                f"- **Sync Status:** `{dev.get('parentalControlSettingState', {}).get('synchronizationStatus', 'UNKNOWN')}`",
                f"- **Last Cloud Sync:** `{last_sync_str}`",
                "",
            ])

    lines.extend(["---", "", "## 2. Playtime & Software Telemetry"])

    summaries = data.get("summaries", {})
    for dev in devices:
        dev_id = dev.get("deviceId")
        s_obj = summaries.get(dev_id) or {}
        daily_items = (s_obj.get("daily") or {}).get("items", [])

        total_secs = sum(d.get("playingTime", 0) or 0 for d in daily_items)
        active_days = [d for d in daily_items if (d.get("playingTime") or 0) > 0]

        app_stats: dict[str, dict] = {}
        player_stats: dict[str, dict] = {}

        for d in daily_items:
            d_date = d.get("date", "")
            for app in d.get("playedApps", []) or []:
                aid = app.get("applicationId") or app.get("title", "Unknown")
                title = app.get("title", "Unknown")
                st = app_stats.setdefault(aid, {
                    "title": title,
                    "first_play": app.get("firstPlayDate") or d_date,
                    "last_play": d_date,
                    "days_active": 0,
                    "playing_secs": 0,
                })
                st["days_active"] += 1
                if d_date > st["last_play"]:
                    st["last_play"] = d_date

            for dp in d.get("devicePlayers", []) or []:
                prof = dp.get("playerProfile") or {}
                nick = prof.get("nickname") or "Player"
                p_secs = dp.get("playingTime", 0) or 0
                pst = player_stats.setdefault(nick, {"secs": 0, "days": 0, "apps": {}})
                if p_secs > 0:
                    pst["secs"] += p_secs
                    pst["days"] += 1
                for pa in dp.get("playedApps", []) or []:
                    aid = pa.get("applicationId")
                    a_secs = pa.get("playingTime", 0) or 0
                    if aid and aid in app_stats:
                        app_stats[aid]["playing_secs"] += a_secs
                        tname = app_stats[aid]["title"]
                        pst["apps"][tname] = pst["apps"].get(tname, 0) + a_secs

        lines.append(f"- **Tracked Window:** {len(daily_items)} days ({len(active_days)} active play days)")
        lines.append(f"- **Cumulative Logged Console Time:** **{total_secs / 3600.0:.1f} hours** ({total_secs // 60:,} minutes)")
        lines.append("")

        if app_stats:
            lines.append("### Played Switch 2 Titles")
            sorted_apps = sorted(
                app_stats.values(),
                key=lambda x: (x["playing_secs"], x["days_active"], x["last_play"]),
                reverse=True,
            )
            for a in sorted_apps:
                hrs = a["playing_secs"] / 3600.0
                hrs_note = f"**{hrs:.1f} hrs**" if hrs > 0 else f"**{a['days_active']} active days**"
                lines.append(
                    f"- **{a['title']}**: {hrs_note} across {a['days_active']} day(s) "
                    f"(First played: {a['first_play']}, Last played: {a['last_play']})"
                )
            lines.append("")

        if player_stats:
            lines.append("### Per-Player Breakdown")
            for nick, pst in sorted(player_stats.items(), key=lambda x: x[1]["secs"], reverse=True):
                p_hrs = pst["secs"] / 3600.0
                top_apps = ", ".join(
                    f"*{t}* ({s/3600.0:.1f}h)"
                    for t, s in sorted(pst["apps"].items(), key=lambda x: x[1], reverse=True)[:5]
                )
                app_suffix = f" — Top titles: {top_apps}" if top_apps else ""
                lines.append(f"- **{nick}**: **{p_hrs:.1f} hours** across {pst['days']} active day(s){app_suffix}")
            lines.append("")

    return "\n".join(lines) + "\n"


def main():
    if not is_authenticated():
        print("[Switch] Error: No authenticated Nintendo Account token found at nxapi_moon_session_token.json.", file=sys.stderr)
        sys.exit(1)

    print("[Switch] Fetching live Nintendo Switch 2 telemetry via Moon REST API...")
    data = fetch_switch_data()

    SWITCH_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(SWITCH_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"[Switch] Saved raw data to {SWITCH_JSON}")

    md = generate_profile_markdown(data)
    PROFILE_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(PROFILE_MD, "w", encoding="utf-8") as f:
        f.write(md)
    try:
        with open(PROFILE_SYMLINK, "w", encoding="utf-8") as f:
            f.write(md)
    except Exception:
        pass
    print(f"[Switch] Generated profile document at {PROFILE_MD}")
    return data


if __name__ == "__main__":
    main()
