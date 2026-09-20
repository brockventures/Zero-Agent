#!/usr/bin/env python3
"""Nintendo Switch Library & Profile Tool for Zero via nxapi.

Interacts with nxapi to query Nintendo Switch Online (NSO) and Parental Controls (Moon) APIs.
Saves raw normalized database to /workspace/data/switch_data.json.
Generates structured gaming profile for /workspace/memory/private/media_gaming_switch_profile.md.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
NXAPI_DATA = DATA_DIR / "nxapi"
SWITCH_JSON = DATA_DIR / "switch_data.json"
PROFILE_MD = Path("/workspace/memory/private/media_gaming_switch_profile.md")
PROFILE_SYMLINK = Path("/workspace/memory/media_gaming_switch_profile.md")
MOON_TOKEN_FILE = DATA_DIR / "nxapi_moon_session_token.json"


def run_cmd(args: list[str]) -> tuple[int, str, str]:
    env = dict(os.environ)
    res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    return res.returncode, res.stdout, res.stderr


def is_authenticated() -> bool:
    return MOON_TOKEN_FILE.exists()


def fetch_switch_data() -> dict:
    data = {
        "fetched_at": datetime.now(PT).isoformat(),
        "account_id": None,
        "devices": [],
        "summaries": {},
    }

    if MOON_TOKEN_FILE.exists():
        try:
            with open(MOON_TOKEN_FILE) as f:
                tdata = json.load(f)
                data["account_id"] = tdata.get("sub") or "f6d99ef26fc11a99"
        except Exception:
            pass

    # Query Moon (Parental Controls) Devices
    code, stdout, stderr = run_cmd([
        "nxapi", "--data-path", str(NXAPI_DATA), "pctl", "devices", "--json"
    ])
    if code == 0 and stdout.strip():
        try:
            # nxapi outputs "Listing devices\n{...}"
            json_start = stdout.find("{")
            if json_start >= 0:
                pctl_devs = json.loads(stdout[json_start:])
                data["devices"] = pctl_devs.get("items", [])
        except Exception as e:
            print(f"[Switch] Warning parsing devices json: {e}", file=sys.stderr)

    # For each device, fetch summaries
    for dev in data["devices"]:
        dev_id = dev.get("deviceId")
        if not dev_id:
            continue
        dev_data = {}
        
        # Daily summaries
        c, out, _ = run_cmd([
            "nxapi", "--data-path", str(NXAPI_DATA), "pctl", "daily-summaries", dev_id, "--json"
        ])
        if c == 0 and out.strip():
            try:
                j_idx = out.find("{")
                if j_idx >= 0:
                    dev_data["daily"] = json.loads(out[j_idx:])
            except Exception:
                dev_data["daily"] = None

        # Monthly summaries
        c, out, _ = run_cmd([
            "nxapi", "--data-path", str(NXAPI_DATA), "pctl", "monthly-summaries", dev_id, "--json"
        ])
        if c == 0 and out.strip():
            try:
                j_idx = out.find("{")
                if j_idx >= 0:
                    dev_data["monthly"] = json.loads(out[j_idx:])
            except Exception:
                dev_data["monthly"] = None

        data["summaries"][dev_id] = dev_data

    return data


def generate_profile_markdown(data: dict) -> str:
    now_str = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    lines = [
        "# Ryan Brock: Nintendo Switch Profile & Telemetry",
        f"*Ingested on {now_str} via nxapi (Nintendo Switch Parental Controls & NSO).* \n",
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
                f"### Console {idx}: **{dev.get('label', 'Nintendo Switch')}**",
                f"- **Device ID:** `{dev.get('deviceId')}`",
                f"- **Hardware Generation:** `{info.get('platformGeneration', 'Unknown')}`",
                f"- **Serial Number:** `{info.get('serialNumber', 'Unknown')}`",
                f"- **Firmware Version:** `{fw_str}`",
                f"- **Region / Language:** `{info.get('region')} / {info.get('language')}`",
                f"- **Master Unlock PIN:** `{info.get('synchronizedUnlockCode', 'N/A')}`",
                f"- **Time Zone:** `{info.get('timeZone')}`",
                f"- **Sync Status:** `{dev.get('parentalControlSettingState', {}).get('synchronizationStatus', 'UNKNOWN')}`",
                f"- **Last Cloud Sync:** `{last_sync_str}`",
                f"- **Primary Console:** `{'Yes' if dev.get('primary') else 'No'}`",
                "",
            ])

    lines.extend([
        "---",
        "",
        "## 2. Playtime & Telemetry Summary",
        "- **Active Monitoring Mode:** Nintendo Switch Parental Controls (Moon) API linked.",
        "- **Telemetry Resolution:** Daily playtime rounded per 5-minute increments per user/title, launched session counts, monthly aggregate summaries.",
        "- **Historical Logs:** Newly paired console — daily session summaries will accumulate as titles are launched and played.",
    ])

    return "\n".join(lines) + "\n"


def main():
    if not is_authenticated():
        print("[Switch] Error: No authenticated Nintendo Account found in nxapi.", file=sys.stderr)
        print("[Switch] Run auth flow first.", file=sys.stderr)
        sys.exit(1)

    print("[Switch] Authenticated Nintendo Account found. Fetching telemetry...")
    data = fetch_switch_data()
    
    SWITCH_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(SWITCH_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"[Switch] Saved raw data to {SWITCH_JSON}")

    md = generate_profile_markdown(data)
    PROFILE_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(PROFILE_MD, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[Switch] Generated profile document at {PROFILE_MD}")

    if not PROFILE_SYMLINK.exists():
        try:
            PROFILE_SYMLINK.symlink_to("private/media_gaming_switch_profile.md")
        except Exception:
            pass


if __name__ == "__main__":
    main()
