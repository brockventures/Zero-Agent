#!/usr/bin/env python3
"""Cloudflare DNS management for Ivy-AG.

List/read/create/update/delete DNS records across Ryan's zones:
- brock.ventures
- getbigboard.com
- ryanbrock.org

Uses CLOUDFLARE_API_TOKEN from environment or /secrets/env.json.
"""

import json
import logging
import os
import sys
import requests

log = logging.getLogger("cloudflare")

BASE = "https://api.cloudflare.com/client/v4"
TIMEOUT = 20

def _get_api_token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                token = json.load(f).get("CLOUDFLARE_API_TOKEN", "")
        except Exception:
            pass
    return token

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_api_token()}",
        "Content-Type": "application/json"
    }

def _zone_id(zone_name: str) -> tuple[str | None, dict | None]:
    token = _get_api_token()
    if not token:
        return None, {"ok": False, "error": "CLOUDFLARE_API_TOKEN not configured."}

    r = requests.get(f"{BASE}/zones", headers=_headers(), timeout=TIMEOUT)
    data = r.json()
    if not data.get("success"):
        return None, {"ok": False, "error": data.get("errors")}
    zones = {z["name"]: z["id"] for z in data.get("result", [])}
    if zone_name not in zones:
        return None, {"ok": False, "error": f"Zone '{zone_name}' not found. Available: {sorted(zones)}"}
    return zones[zone_name], None

def cloudflare_dns(action: str, zone_name: str = "", record_id: str = "",
                   record_type: str = "", record_name: str = "", content: str = "",
                   proxied: bool = False, ttl: int = 1) -> dict:
    """Manage Cloudflare DNS records across zones."""
    token = _get_api_token()
    if not token:
        return {"ok": False, "error": "CLOUDFLARE_API_TOKEN is not set."}

    try:
        if action == "list_zones":
            r = requests.get(f"{BASE}/zones", headers=_headers(), timeout=TIMEOUT)
            data = r.json()
            if not data.get("success"):
                return {"ok": False, "error": data.get("errors")}
            return {"ok": True, "zones": {z["name"]: z["id"] for z in data.get("result", [])}}

        if not zone_name:
            return {"ok": False, "error": "zone_name is required for this action."}
        zid, err = _zone_id(zone_name)
        if err:
            return err

        if action == "list_records":
            r = requests.get(f"{BASE}/zones/{zid}/dns_records", headers=_headers(), timeout=TIMEOUT)
            data = r.json()
            return {"ok": data.get("success", False), "records": data.get("result", []), "errors": data.get("errors")}

        if action == "get_record":
            if not record_id:
                return {"ok": False, "error": "record_id is required."}
            r = requests.get(f"{BASE}/zones/{zid}/dns_records/{record_id}", headers=_headers(), timeout=TIMEOUT)
            data = r.json()
            return {"ok": data.get("success", False), "record": data.get("result"), "errors": data.get("errors")}

        if action == "create_record":
            payload = {"type": record_type, "name": record_name, "content": content, "ttl": ttl, "proxied": proxied}
            r = requests.post(f"{BASE}/zones/{zid}/dns_records", headers=_headers(), json=payload, timeout=TIMEOUT)
            data = r.json()
            return {"ok": data.get("success", False), "result": data.get("result"), "errors": data.get("errors")}

        if action == "update_record":
            if not record_id:
                return {"ok": False, "error": "record_id is required."}
            payload = {"type": record_type, "name": record_name, "content": content, "ttl": ttl, "proxied": proxied}
            r = requests.put(f"{BASE}/zones/{zid}/dns_records/{record_id}", headers=_headers(), json=payload, timeout=TIMEOUT)
            data = r.json()
            return {"ok": data.get("success", False), "result": data.get("result"), "errors": data.get("errors")}

        if action == "delete_record":
            if not record_id:
                return {"ok": False, "error": "record_id is required."}
            r = requests.delete(f"{BASE}/zones/{zid}/dns_records/{record_id}", headers=_headers(), timeout=TIMEOUT)
            data = r.json()
            return {"ok": data.get("success", False), "result": data.get("result"), "errors": data.get("errors")}

        return {"ok": False, "error": f"Unknown action: {action}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "list_zones"
    zone = sys.argv[2] if len(sys.argv) > 2 else ""
    res = cloudflare_dns(action=action, zone_name=zone)
    print(json.dumps(res, indent=2))
