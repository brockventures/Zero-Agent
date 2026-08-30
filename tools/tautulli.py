"""Tautulli (Plex activity/history) queries for Ivy-Gemini.

Read-only queries against Tautulli's API v2 on Host1, using
$TAUTULLI_API_KEY.
"""

import logging
import os

import requests

log = logging.getLogger("tautulli")

API_KEY = os.environ.get("TAUTULLI_API_KEY", "")
if not API_KEY and os.path.exists("/secrets/env.json"):
    try:
        import json
        with open("/secrets/env.json") as f:
            API_KEY = json.load(f).get("TAUTULLI_API_KEY", "")
    except Exception:
        pass

HOST_1_IP = os.environ.get("NAS_HOST_1_IP", "127.0.0.1")
URL = os.environ.get("TAUTULLI_URL", f"http://{HOST_1_IP}:8181/api/v2")
TIMEOUT = 10

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

    try:
        r = requests.get(URL, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "error": r.text[:300]}
        data = r.json()
        return {"ok": True, "data": data.get("response", {}).get("data", {})}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}
