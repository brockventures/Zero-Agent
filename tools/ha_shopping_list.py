#!/usr/bin/env python3
"""
Home Assistant Todo / Shopping List Connector
Author: Zero
Description: Interfaces with Home Assistant's native `todo.shopping_list` entity via
REST API to synchronize collaborative household grocery items across Ryan and Emily's
Pixel phones, ingest pending items for Whole Foods AFX cart compilation, and mark items completed.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

WORKSPACE = "/workspace"
SECRETS_FILE = "/secrets/env.json"
DEFAULT_TODO_ENTITY = "todo.shopping_list"


def _load_ha_creds() -> tuple[str, str]:
    if not os.path.exists(SECRETS_FILE):
        raise RuntimeError(f"Secrets file not found: {SECRETS_FILE}")
    with open(SECRETS_FILE, "r") as f:
        env = json.load(f)
    base = env.get("HA_BASE_URL", "http://127.0.0.1:8123/").rstrip("/")
    token = env.get("HA_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("HA_ACCESS_TOKEN missing in secrets")
    return base, token


def _ha_call_service(domain: str, service: str, payload: dict, return_response: bool = False) -> dict:
    base, token = _load_ha_creds()
    url = f"{base}/api/services/{domain}/{service}"
    if return_response:
        url += "?return_response=true"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        res = json.loads(resp.read().decode())
        return res


def get_pending_groceries(entity_id: str = DEFAULT_TODO_ENTITY) -> list[dict]:
    """Fetches all uncompleted items from Home Assistant's todo entity.
    Returns list of dicts: [{'id': uid, 'title': summary, 'status': 'needs_action'}, ...]
    """
    try:
        res = _ha_call_service("todo", "get_items", {"entity_id": entity_id}, return_response=True)
        service_res = res.get("service_response", {})
        entity_data = service_res.get(entity_id, {})
        items = entity_data.get("items", [])
        
        pending = []
        for it in items:
            status = it.get("status", "")
            if status == "needs_action" and it.get("summary"):
                summary = it["summary"].strip()
                cleaned_title = summary
                if cleaned_title.startswith("[Demo WF]"):
                    cleaned_title = cleaned_title.replace("[Demo WF]", "").strip()
                
                pending.append({
                    "id": it.get("uid", ""),
                    "title": cleaned_title,
                    "raw_title": summary,
                    "status": status,
                })
        return pending
    except Exception as e:
        print(f"[HA_ShoppingList] Error fetching pending groceries: {e}", file=sys.stderr)
        return []


def add_grocery_item(item_summary: str, entity_id: str = DEFAULT_TODO_ENTITY) -> bool:
    """Adds a new item to Home Assistant's shopping list."""
    try:
        _ha_call_service("todo", "add_item", {"entity_id": entity_id, "item": item_summary})
        return True
    except Exception as e:
        print(f"[HA_ShoppingList] Error adding item '{item_summary}': {e}", file=sys.stderr)
        return False


def complete_grocery_item(item_id_or_summary: str, entity_id: str = DEFAULT_TODO_ENTITY) -> bool:
    """Marks an item completed in Home Assistant's shopping list."""
    try:
        _ha_call_service("todo", "update_item", {
            "entity_id": entity_id,
            "item": item_id_or_summary,
            "status": "completed",
        })
        return True
    except Exception as e:
        print(f"[HA_ShoppingList] Error completing item '{item_id_or_summary}': {e}", file=sys.stderr)
        return False


def remove_completed_items(entity_id: str = DEFAULT_TODO_ENTITY) -> bool:
    """Clears all completed items from Home Assistant's shopping list."""
    try:
        _ha_call_service("todo", "remove_completed_items", {"entity_id": entity_id})
        return True
    except Exception as e:
        print(f"[HA_ShoppingList] Error removing completed items: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Home Assistant Shopping List Connector")
    parser.add_argument("--list", action="store_true", help="List pending grocery items")
    parser.add_argument("--add", type=str, help="Add an item to the shopping list")
    parser.add_argument("--complete", type=str, help="Mark an item completed by UID or summary")
    parser.add_argument("--clear-completed", action="store_true", help="Clear all completed items")
    parser.add_argument("--json-out", action="store_true", help="Output JSON format")
    args = parser.parse_args()

    if args.add:
        ok = add_grocery_item(args.add)
        print(f"Added '{args.add}': {ok}")
    elif args.complete:
        ok = complete_grocery_item(args.complete)
        print(f"Completed '{args.complete}': {ok}")
    elif args.clear_completed:
        ok = remove_completed_items()
        print(f"Cleared completed items: {ok}")
    elif args.list or args.json_out:
        items = get_pending_groceries()
        if args.json_out:
            print(json.dumps(items, indent=2))
        else:
            print(f"Pending Items in {DEFAULT_TODO_ENTITY} ({len(items)}):")
            for it in items:
                print(f"  • {it['title']} (uid: {it['id']})")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
