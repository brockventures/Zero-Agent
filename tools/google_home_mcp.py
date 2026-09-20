#!/usr/bin/env python3
"""Google Home MCP Server & CLI Tool for Zero.

Connects to Google's official Home MCP endpoint (https://home.googleapis.com/mcp)
using OAuth 2.0 with the https://www.googleapis.com/auth/home.platform.v2 scope.

Exposes smart home resources, states, camera/history event logs, and parameterized
device actions over Model Context Protocol (MCP) and CLI.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
from mcp.server.mcpserver import MCPServer

SECRETS_PATH = (
    "/workspace/config/google_home_oauth.json"
    if os.path.exists("/workspace/config/google_home_oauth.json")
    else (
        "/secrets/google_home_oauth.json"
        if os.path.exists("/secrets/google_home_oauth.json")
        else (
            "/workspace/config/google_oauth.json"
            if os.path.exists("/workspace/config/google_oauth.json")
            else os.environ.get("GOOGLE_OAUTH_PATH", os.environ.get("GOOGLE_OAUTH_CREDENTIALS", "/secrets/google_oauth.json"))
        )
    )
)
PT = ZoneInfo("America/Los_Angeles")
TIMEOUT = 30
HOME_MCP_ENDPOINT = "https://home.googleapis.com/mcp"

_cached_token: Optional[str] = None
_token_expiry: float = 0.0

server = MCPServer("google-home")


def load_credentials(path: str) -> dict:
    """Load OAuth credentials JSON safely."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        creds = {}
        for line in content.splitlines():
            line = line.strip().rstrip(",")
            if ":" in line:
                k, v = line.split(":", 1)
                creds[k.strip().strip("\"").strip("'")] = v.strip().strip("\"").strip("'")
        return creds


def get_access_token() -> str:
    """Retrieve or refresh the OAuth access token."""
    global _cached_token, _token_expiry
    now = time.time()
    if _cached_token and now < _token_expiry - 60:
        return _cached_token

    if not os.path.exists(SECRETS_PATH):
        raise FileNotFoundError(f"OAuth credentials not found at {SECRETS_PATH}")

    creds = load_credentials(SECRETS_PATH)
    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request(creds.get("token_uri", "https://oauth2.googleapis.com/token"), data=data)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        tokens = json.loads(resp.read().decode("utf-8"))
        _cached_token = tokens["access_token"]
        expires_in = tokens.get("expires_in", 3600)
        _token_expiry = now + expires_in
        return _cached_token


def _call_home_mcp(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a tool call to the official Google Home MCP endpoint."""
    token = get_access_token()
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments
        }
    }
    req = urllib.request.Request(
        HOME_MCP_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "error" in data:
                return {"ok": False, "error": data["error"]}
            return {"ok": True, "result": data.get("result", {})}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        return {"ok": False, "http_status": e.code, "error": body}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@server.tool()
def list_homes() -> str:
    """Retrieves a list of homes/structures the user has access to in Google Home."""
    res = _call_home_mcp("list_homes", {})
    return json.dumps(res, indent=2)


@server.tool()
def list_home_resources(structure_id: Optional[str] = None) -> str:
    """Retrieves a list of home resources including devices, rooms, and scenes."""
    args: Dict[str, Any] = {}
    if structure_id:
        args["structureId"] = structure_id
    res = _call_home_mcp("list_home_resources", args)
    return json.dumps(res, indent=2)


@server.tool()
def list_home_states(structure_id: Optional[str] = None, freshness: str = "DEFAULT") -> str:
    """Retrieves current states of home resources including online status and trait states."""
    args: Dict[str, Any] = {"freshness": freshness}
    if structure_id:
        args["structureId"] = structure_id
    res = _call_home_mcp("list_home_states", args)
    return json.dumps(res, indent=2)


@server.tool()
def list_home_history(
    structure_id: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    include_media_urls: bool = True
) -> str:
    """Retrieves event history and camera event clips for the home structure."""
    args: Dict[str, Any] = {
        "structureId": structure_id,
        "includeMediaUrls": include_media_urls
    }
    if start_time:
        args["startTime"] = start_time
    if end_time:
        args["endTime"] = end_time
    res = _call_home_mcp("list_home_history", args)
    return json.dumps(res, indent=2)


@server.tool()
def run_home_actions(structure_id: str, actions: List[Dict[str, Any]]) -> str:
    """Executes a list of actions on one or more Google Home resources (e.g. OnOff, Volume, Level)."""
    args = {
        "structureId": structure_id,
        "homeActionRequests": actions
    }
    res = _call_home_mcp("run_home_actions", args)
    return json.dumps(res, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Google Home MCP Client & CLI")
    parser.add_argument("--homes", action="store_true", help="List all homes/structures")
    parser.add_argument("--resources", action="store_true", help="List all resources/devices")
    parser.add_argument("--states", action="store_true", help="List all resource states")
    parser.add_argument("--history", action="store_true", help="List home history/events")
    parser.add_argument("--structure-id", type=str, help="Specific structure ID")
    parser.add_argument("--auth-url", action="store_true", help="Print Google Home OAuth authorization URL")

    args = parser.parse_args()

    if args.auth_url:
        creds = load_credentials(SECRETS_PATH)
        client_id = creds.get("client_id", "")
        scopes = [
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/home.platform.v2"
        ]
        params = {
            "client_id": client_id,
            "redirect_uri": "http://localhost:8080",
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent"
        }
        url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
        print("GOOGLE HOME OAUTH CONSENT URL:")
        print(url)
        return

    if args.homes:
        print(list_homes())
    elif args.resources:
        print(list_home_resources(args.structure_id))
    elif args.states:
        print(list_home_states(args.structure_id))
    elif args.history:
        if not args.structure_id:
            print("Error: --structure-id is required for history queries")
            sys.exit(1)
        print(list_home_history(args.structure_id))
    else:
        # Run standard MCP server mode
        server.run()


if __name__ == "__main__":
    main()
