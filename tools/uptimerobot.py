#!/usr/bin/env python3
"""UptimeRobot monitor status for Ivy-AG.

Read-only queries against UptimeRobot's API v2, using UPTIMEROBOT_API_KEY
from environment or /secrets/env.json.
"""

import json
import logging
import os
import sys
import requests

log = logging.getLogger("uptimerobot")

BASE = "https://api.uptimerobot.com/v2"
TIMEOUT = 10

def _get_api_key() -> str:
    key = os.environ.get("UPTIMEROBOT_API_KEY", "")
    if not key and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                key = json.load(f).get("UPTIMEROBOT_API_KEY", "")
        except Exception:
            pass
    return key

def uptimerobot(action: str = "get_monitors", monitors: str = "") -> dict:
    """Check monitor status, response times, and uptime ratios."""
    api_key = _get_api_key()
    if not api_key:
        return {"ok": False, "error": "UPTIMEROBOT_API_KEY is not set."}

    payload = {"api_key": api_key, "format": "json"}
    if action == "get_monitors":
        payload["response_times"] = "1"
        payload["custom_uptime_ratios"] = "1-7-30"
        if monitors:
            payload["monitors"] = monitors
        endpoint = f"{BASE}/getMonitors"
    elif action == "get_account_details":
        endpoint = f"{BASE}/getAccountDetails"
    else:
        return {"ok": False, "error": f"Unknown action: {action}"}

    try:
        r = requests.post(endpoint, data=payload, timeout=TIMEOUT)
        data = r.json()
        return {"ok": data.get("stat") == "ok", "result": data}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "get_monitors"
    res = uptimerobot(action=action)
    print(json.dumps(res, indent=2))
