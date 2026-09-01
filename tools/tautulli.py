"""Tautulli (Plex activity/history) queries for Ivy-Gemini.

Read-only queries against Tautulli's API v2 on Host1, using
$TAUTULLI_API_KEY.
"""

import logging
import os

import requests

log = logging.getLogger("tautulli")

import json
import urllib.parse

API_KEY = os.environ.get("TAUTULLI_API_KEY", "")
HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "")

if os.path.exists("/secrets/env.json"):
    try:
        with open("/secrets/env.json") as f:
            d = json.load(f)
            if not API_KEY:
                API_KEY = d.get("TAUTULLI_API_KEY", "")
            if not HOST_1_IP:
                if d.get("NAS_HOST_1_IP"):
                    HOST_1_IP = d["NAS_HOST_1_IP"]
                elif d.get("HA_BASE_URL"):
                    HOST_1_IP = urllib.parse.urlparse(d["HA_BASE_URL"]).hostname
    except Exception:
        pass

if not HOST_1_IP and os.path.exists("/secrets/ha.json"):
    try:
        with open("/secrets/ha.json") as f:
            d = json.load(f)
            if d.get("url"):
                HOST_1_IP = urllib.parse.urlparse(d["url"]).hostname
    except Exception:
        pass

HOST_1_IP = HOST_1_IP or "127.0.0.1"
URL = os.environ.get("TAUTULLI_URL", f"http://{HOST_1_IP}:8181/api/v2")
TIMEOUT = 25

CMDS = {"get_activity", "get_history", "get_users", "get_user_player_stats",
        "server_status"}


def tautulli(cmd: str, user: str = "", length: int = 10) -> dict:
    """Query Plex streaming activity, watch history, users, or server status."""
    if not API_KEY:
        return {"ok": False, "error": "TAUTULLI_API_KEY is not set."}
    if cmd not in CMDS:
        return {"ok": False, "error": f"unknown cmd '{cmd}'. Allowed: {sorted(CMDS)}"}

    try:
        length = max(1, min(int(length or 10), 50))
    except (TypeError, ValueError):
        length = 10

    params = {"apikey": API_KEY, "cmd": cmd, "length": length}
    if user:
        params["user"] = user

    last_err = ""
    for attempt in range(2):
        try:
            r = requests.get(URL, params=params, timeout=TIMEOUT)
            if r.status_code != 200:
                return {"ok": False, "status": r.status_code, "error": r.text[:300]}
            data = r.json()
            return {"ok": True, "data": data.get("response", {}).get("data", {})}
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)
            if attempt == 0:
                import time
                time.sleep(1)

    return {"ok": False, "error": last_err}
