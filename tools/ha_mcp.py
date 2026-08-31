#!/usr/bin/env python3
"""Home Assistant MCP Server for Ivy-AG.

Exposes safe Home Assistant operations over Model Context Protocol (MCP).
Operating rules:
- Blocked outright: anything that restarts/stops HA or the Supervisor.
- Timestamps converted to Pacific Time (America/Los_Angeles).
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from mcp.server.mcpserver import MCPServer

# Load credentials from /secrets/ha.json or env
SECRETS_PATH = os.environ.get("HA_SECRETS_PATH", "/secrets/ha.json")
BASE_URL = os.environ.get("HA_BASE_URL", "http://127.0.0.1:8123")
TOKEN = os.environ.get("HA_ACCESS_TOKEN", "")

if os.path.exists(SECRETS_PATH):
    try:
        with open(SECRETS_PATH) as f:
            cfg = json.load(f)
            BASE_URL = cfg.get("url", BASE_URL).rstrip("/")
            TOKEN = cfg.get("token", "")
    except Exception:
        pass

if not TOKEN:
    TOKEN = os.environ.get("HA_ACCESS_TOKEN", "")
if os.environ.get("HA_BASE_URL"):
    BASE_URL = os.environ["HA_BASE_URL"].rstrip("/")

PT = ZoneInfo("America/Los_Angeles")
TIMEOUT = 25

BLOCKED_SERVICES = {
    ("homeassistant", "restart"),
    ("homeassistant", "stop"),
}
BLOCKED_DOMAINS = {"hassio", "supervisor", "update", "shell_command"}

server = MCPServer("home-assistant")

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }

def _to_pt(ts: str) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PT).strftime("%Y-%m-%d %I:%M:%S %p PT")
    except Exception:
        return ts

@server.tool()
def ha_ping() -> str:
    """Check Home Assistant API liveness."""
    try:
        req = urllib.request.Request(f"{BASE_URL}/api/", headers=_headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return json.dumps({"ok": True, "message": data.get("message", "API running.")})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@server.tool()
def ha_get_state(entity_id: str) -> str:
    """Read one entity's state, friendly name, and attributes. Timestamps in Pacific Time."""
    if not entity_id:
        return json.dumps({"ok": False, "error": "entity_id required"})
    try:
        url = f"{BASE_URL}/api/states/{urllib.parse.quote(entity_id)}"
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            d = json.loads(resp.read().decode())
            res = {
                "ok": True,
                "entity_id": d.get("entity_id"),
                "state": d.get("state"),
                "friendly_name": (d.get("attributes") or {}).get("friendly_name"),
                "attributes": d.get("attributes"),
                "last_changed_pt": _to_pt(d.get("last_changed", "")),
            }
            return json.dumps(res)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return json.dumps({"ok": False, "error": f"entity '{entity_id}' not found"})
        return json.dumps({"ok": False, "error": f"HTTP {e.code}: {e.reason}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@server.tool()
def ha_search_entities(query: str, limit: int = 30) -> str:
    """Find entity IDs and states by substring match against id or friendly name."""
    q = (query or "").strip().lower()
    if not q:
        return json.dumps({"ok": False, "error": "query required"})
    try:
        limit = max(1, min(int(limit or 30), 60))
    except (TypeError, ValueError):
        limit = 30
    try:
        req = urllib.request.Request(f"{BASE_URL}/api/states", headers=_headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            entities = json.loads(resp.read().decode())
            hits = []
            for d in entities:
                eid = d.get("entity_id", "")
                name = (d.get("attributes") or {}).get("friendly_name", "") or ""
                if q in eid.lower() or q in name.lower():
                    hits.append({
                        "entity_id": eid,
                        "friendly_name": name,
                        "state": d.get("state"),
                    })
            return json.dumps({
                "ok": True,
                "count": len(hits),
                "matches": hits[:limit],
                "truncated": len(hits) > limit
            })
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@server.tool()
def ha_call_service(domain: str, service: str, entity_id: str = "", data_json: str = "") -> str:
    """Call an HA service. Blocked: restarts, supervisor, or updates."""
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    if not domain or not service:
        return json.dumps({"ok": False, "error": "domain and service required"})
    if domain in BLOCKED_DOMAINS or (domain, service) in BLOCKED_SERVICES:
        return json.dumps({
            "ok": False,
            "error": f"REFUSED: {domain}.{service} restarts or reconfigures core services. Requires Ryan's explicit approval."
        })
    payload = {}
    if data_json:
        try:
            extra = json.loads(data_json)
            if isinstance(extra, dict):
                payload.update(extra)
            else:
                return json.dumps({"ok": False, "error": "data_json must be a JSON object"})
        except Exception as e:
            return json.dumps({"ok": False, "error": f"Invalid data_json: {e}"})
    if entity_id:
        payload["entity_id"] = entity_id

    try:
        url = f"{BASE_URL}/api/services/{urllib.parse.quote(domain)}/{urllib.parse.quote(service)}"
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=_headers(), method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            content = resp.read().decode()
            changed = json.loads(content) if content else []
            return json.dumps({
                "ok": True,
                "called": f"{domain}.{service}",
                "payload": payload,
                "entities_changed": [
                    {"entity_id": c.get("entity_id"), "state": c.get("state")}
                    for c in changed if isinstance(c, dict)
                ][:20]
            })
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

if __name__ == "__main__":
    import sys
    port = 8766
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=")[1])
        elif arg == "--port" and sys.argv.index(arg) + 1 < len(sys.argv):
            port = int(sys.argv[sys.argv.index(arg) + 1])
    if "--sse" in sys.argv:
        server.run(transport="sse", host="127.0.0.1", port=port)
    else:
        server.run(transport="stdio")

