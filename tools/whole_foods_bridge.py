#!/usr/bin/env python3
"""
Whole Foods Staging Bridge & Amazon Endpoint Diagnostic Utility
Author: Zero
Description: Manages Home Assistant todo.shopping_list syncing for grocery orders
and runs live bot-defense / session telemetry against Amazon retail endpoints.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS_PATH = Path("/secrets/ha.json")


def get_ha_client():
    if not SECRETS_PATH.exists():
        raise FileNotFoundError(f"HA secrets file not found at {SECRETS_PATH}")
    with open(SECRETS_PATH, "r") as f:
        data = json.load(f)
    return data.get("url"), data.get("token")


def ha_request(endpoint: str, payload: dict = None, query: str = ""):
    base_url, token = get_ha_client()
    url = f"{base_url}{endpoint}"
    if query:
        url += f"?{query}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_items():
    res = ha_request(
        "/api/services/todo/get_items",
        payload={"entity_id": "todo.shopping_list"},
        query="return_response",
    )
    items = res.get("service_response", {}).get("todo.shopping_list", {}).get("items", [])
    return items


def add_items(items: list[str]):
    added = []
    for item in items:
        res = ha_request(
            "/api/services/todo/add_item",
            payload={"entity_id": "todo.shopping_list", "item": item},
        )
        added.append(item)
    return added


def remove_items(items: list[str]):
    res = ha_request(
        "/api/services/todo/remove_item",
        payload={"entity_id": "todo.shopping_list", "item": items},
    )
    return res


def clear_demo():
    current = list_items()
    to_remove = [it["summary"] for it in current if it.get("summary", "").startswith("[Demo WF]")]
    if to_remove:
        remove_items(to_remove)
    return to_remove


def probe_amazon():
    search_term = "365 whole foods organic whole milk"
    url = f"https://www.amazon.com/s?k={urllib.parse.quote(search_term)}&i=wholefoods"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {
                "status": resp.status,
                "bot_wall_triggered": False,
                "message": "Direct HTML received",
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        is_bot_wall = (
            "api-services-support@amazon.com" in body
            or "automated access" in body.lower()
            or e.code == 503
        )
        return {
            "status": e.code,
            "bot_wall_triggered": is_bot_wall,
            "signature": "Amazon PerimeterX / WAF 503 Bot Mitigation" if is_bot_wall else f"HTTP {e.code}",
            "response_body_snippet": body[:300].strip(),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Whole Foods Cart & HA Bridge Diagnostic")
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("list", help="List items in HA todo.shopping_list")
    subparsers.add_parser("clear_demo", help="Clear [Demo WF] items")
    subparsers.add_parser("probe_amazon", help="Probe Amazon Whole Foods endpoint bot defense")

    add_parser = subparsers.add_parser("add", help="Add items to HA shopping list")
    add_parser.add_argument("items", nargs="+", help="Items to add")

    args = parser.parse_args()

    if args.action == "list":
        items = list_items()
        print(json.dumps({"count": len(items), "items": items}, indent=2))
    elif args.action == "add":
        added = add_items(args.items)
        print(json.dumps({"added": added}, indent=2))
    elif args.action == "clear_demo":
        removed = clear_demo()
        print(json.dumps({"removed": removed}, indent=2))
    elif args.action == "probe_amazon":
        result = probe_amazon()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
