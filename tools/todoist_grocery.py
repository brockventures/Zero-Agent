#!/usr/bin/env python3
"""
Todoist Grocery Connector
Author: Zero
Description: Interfaces with Todoist API v1 to maintain Whole Foods and Costco shopping lists,
fetch pending items for cart compilation, add items, and mark tasks completed.
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Ensure workspace is in sys.path
for _path in [str(Path(__file__).resolve().parent.parent), "/workspace"]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

TODOIST_BASE_URL = "https://api.todoist.com/api/v1"
DEFAULT_LIST_TITLE = "Whole Foods"
SECRETS_FILE = "/secrets/env.json"


def _normalize_project_name(title: str | None) -> str:
    """Normalize project name by stripping leading shopping cart emojis and surrounding whitespace."""
    if not title:
        return ""
    # Strip leading cart emoji 🛒 (U+1F6D2), 🛍 (U+1F6CD), variation selector \ufe0f, and whitespace
    return re.sub(r"^[\s🛒🛍\ufe0f]+", "", title).strip()


def _get_api_token() -> str:
    """Resolves Todoist API token from environment variable or /secrets/env.json."""
    token = os.environ.get("TODOIST_API_TOKEN")
    if token:
        return token

    secrets_path = Path(SECRETS_FILE)
    if secrets_path.exists():
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                token = data.get("TODOIST_API_TOKEN")
                if token:
                    return token
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    raise RuntimeError("TODOIST_API_TOKEN not found in environment or /secrets/env.json")


def _headers() -> dict:
    """Constructs HTTP request headers for Todoist API requests."""
    token = _get_api_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _api_request(
    endpoint: str,
    method: str = "GET",
    data: dict | None = None,
    params: dict | None = None,
) -> dict | list:
    """Executes an HTTP request to the Todoist API v1."""
    url = f"{TODOIST_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    headers = _headers()
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)


def _fetch_all(endpoint: str, params: dict | None = None) -> list[dict]:
    """Fetches all items from a potentially paginated Todoist endpoint (supporting envelope and list responses)."""
    query_params = dict(params or {})
    all_items: list[dict] = []
    cursor = None

    while True:
        if cursor:
            query_params["cursor"] = cursor
        res = _api_request(endpoint, method="GET", params=query_params)
        if isinstance(res, list):
            all_items.extend(res)
            break
        elif isinstance(res, dict):
            items = res.get("results")
            if items is None:
                items = res.get("items", [])
            if isinstance(items, list):
                all_items.extend(items)
            cursor = res.get("next_cursor")
            if not cursor:
                break
        else:
            break

    return all_items


def list_projects() -> list[dict]:
    """Fetches all projects from Todoist."""
    return _fetch_all("/projects")


def get_or_create_project(title: str = DEFAULT_LIST_TITLE) -> str:
    """Finds or creates a Todoist project matching title (case-insensitive, emoji-stripped) and returns its ID."""
    norm_title = _normalize_project_name(title).lower()
    projects = list_projects()
    for p in projects:
        p_name = _normalize_project_name(p.get("name", "")).lower()
        if p_name == norm_title:
            return str(p["id"])

    # Not found, create it
    target_name = _normalize_project_name(title) or title
    body = {"name": target_name}
    res = _api_request("/projects", method="POST", data=body)
    return str(res["id"])


def get_pending_groceries(list_id: str | None = None, title: str = DEFAULT_LIST_TITLE) -> list[dict]:
    """Fetches all uncompleted items in the grocery project."""
    if not list_id:
        list_id = get_or_create_project(title)

    items = _fetch_all("/tasks", params={"project_id": list_id})
    pending = []
    for item in items:
        is_completed = bool(item.get("is_completed") or item.get("checked"))
        content = (item.get("content") or item.get("title") or "").strip()
        if not is_completed and content:
            pending.append({
                "id": str(item["id"]),
                "title": content,
                "notes": item.get("description") or item.get("notes", ""),
                "status": "needsAction",
                "content": content,
                "project_id": str(item.get("project_id", list_id)),
            })
    return pending


def add_grocery_item(
    title_text: str,
    list_id: str | None = None,
    notes: str = "",
    list_title: str = DEFAULT_LIST_TITLE,
) -> dict:
    """Adds a grocery task item to the specified Todoist project."""
    if not list_id:
        list_id = get_or_create_project(list_title)

    payload = {"content": title_text, "project_id": list_id}
    if notes:
        payload["description"] = notes

    res = _api_request("/tasks", method="POST", data=payload)
    if isinstance(res, dict):
        if "content" in res and "title" not in res:
            res["title"] = res["content"]
        if "id" in res:
            res["id"] = str(res["id"])
    return res


def complete_task(
    task_id: str,
    list_id: str | None = None,
    list_title: str = DEFAULT_LIST_TITLE,
) -> dict:
    """Closes/completes a task in Todoist by task ID."""
    res = _api_request(f"/tasks/{task_id}/close", method="POST")
    if not isinstance(res, dict) or not res:
        res = {"id": str(task_id), "status": "completed"}
    else:
        if "id" not in res:
            res["id"] = str(task_id)
        if "status" not in res:
            res["status"] = "completed"
    return res


def main():
    parser = argparse.ArgumentParser(description="Todoist Grocery Connector")
    parser.add_argument("--list", action="store_true", help="List pending grocery items")
    parser.add_argument("--add", type=str, help="Add item to grocery list")
    parser.add_argument("--notes", type=str, default="", help="Optional notes for added item")
    parser.add_argument("--complete", type=str, help="Complete a task by ID")
    parser.add_argument("--list-id", type=str, help="Target project ID (optional)")
    parser.add_argument("--list-title", type=str, default=DEFAULT_LIST_TITLE, help="Target project title")
    parser.add_argument("--projects", action="store_true", help="List all Todoist projects")
    args = parser.parse_args()

    try:
        if args.list:
            items = get_pending_groceries(list_id=args.list_id, title=args.list_title)
            print(json.dumps(items, indent=2))
        elif args.add:
            res = add_grocery_item(args.add, list_id=args.list_id, notes=args.notes, list_title=args.list_title)
            print(f"Added task {res.get('id')}: {res.get('title') or res.get('content')}")
        elif args.complete:
            res = complete_task(args.complete, list_id=args.list_id, list_title=args.list_title)
            print(f"Completed task {res.get('id')}")
        elif args.projects:
            projects = list_projects()
            print(json.dumps(projects, indent=2))
        else:
            parser.print_help()
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
