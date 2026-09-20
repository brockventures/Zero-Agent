#!/usr/bin/env python3
"""
Google Tasks Grocery Connector
Author: Zero
Description: Integrates with Google Tasks API to maintain a shared family grocery list
(e.g., '🛒 Whole Foods'), fetch pending items for cart compilation, and mark items completed.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/workspace")
from tools.workspace_mcp import _get_access_token, load_credentials, SECRETS_PATH

TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
DEFAULT_LIST_TITLE = "🛒 Whole Foods"
SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/tasks",
]


def get_auth_url() -> str:
    creds = load_credentials(SECRETS_PATH)
    params = {
        "client_id": creds["client_id"],
        "redirect_uri": "http://localhost:8080",
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"https://accounts.google.com/o/oauth2/auth?{urllib.parse.urlencode(params)}"


def _task_headers() -> dict:
    tok = _get_access_token()
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }


def list_tasklists() -> list[dict]:
    req = urllib.request.Request(f"{TASKS_BASE}/users/@me/lists", headers=_task_headers())
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
        return data.get("items", [])


def get_or_create_grocery_list(title: str = DEFAULT_LIST_TITLE) -> str:
    """Finds or creates a Google Task list with the specified title and returns its ID."""
    tasklists = list_tasklists()
    for tl in tasklists:
        if tl.get("title", "").strip().lower() == title.strip().lower():
            return tl["id"]

    # Not found, create it
    body = json.dumps({"title": title}).encode("utf-8")
    req = urllib.request.Request(f"{TASKS_BASE}/users/@me/lists", data=body, headers=_task_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
        return data["id"]


def get_pending_groceries(list_id: str | None = None, title: str = DEFAULT_LIST_TITLE) -> list[dict]:
    """Fetches all uncompleted items in the grocery task list."""
    if not list_id:
        list_id = get_or_create_grocery_list(title)

    params = urllib.parse.urlencode({"showCompleted": "false", "maxResults": 100})
    req = urllib.request.Request(f"{TASKS_BASE}/lists/{list_id}/tasks?{params}", headers=_task_headers())
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
        items = data.get("items", [])
        return [
            {
                "id": item["id"],
                "title": item.get("title", ""),
                "notes": item.get("notes", ""),
                "status": item.get("status", ""),
            }
            for item in items
            if item.get("status") == "needsAction" and item.get("title")
        ]


def add_grocery_item(title_text: str, list_id: str | None = None, notes: str = "", list_title: str = DEFAULT_LIST_TITLE) -> dict:
    if not list_id:
        list_id = get_or_create_grocery_list(list_title)

    body_dict = {"title": title_text}
    if notes:
        body_dict["notes"] = notes

    data = json.dumps(body_dict).encode("utf-8")
    req = urllib.request.Request(f"{TASKS_BASE}/lists/{list_id}/tasks", data=data, headers=_task_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def complete_task(task_id: str, list_id: str | None = None, list_title: str = DEFAULT_LIST_TITLE) -> dict:
    if not list_id:
        list_id = get_or_create_grocery_list(list_title)

    # In Google Tasks API, to complete a task, send PATCH with status='completed'
    body = json.dumps({"status": "completed", "id": task_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{TASKS_BASE}/lists/{list_id}/tasks/{task_id}",
        data=body,
        headers=_task_headers(),
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def main():
    parser = argparse.ArgumentParser(description="Google Tasks Grocery Connector")
    parser.add_argument("--auth-url", action="store_true", help="Print OAuth authorization URL for Google Tasks scope")
    parser.add_argument("--list", action="store_true", help="List pending grocery items")
    parser.add_argument("--add", type=str, help="Add item to grocery list")
    parser.add_argument("--complete", type=str, help="Complete a task by ID")
    parser.add_argument("--list-id", type=str, help="Target task list ID (optional)")
    parser.add_argument("--list-title", type=str, default=DEFAULT_LIST_TITLE, help="Target task list title")
    args = parser.parse_args()

    if args.auth_url:
        print("OAuth URL:")
        print(get_auth_url())
        return

    try:
        if args.list:
            items = get_pending_groceries(list_id=args.list_id, title=args.list_title)
            print(json.dumps(items, indent=2))
        elif args.add:
            res = add_grocery_item(args.add, list_id=args.list_id, list_title=args.list_title)
            print(f"Added task {res.get('id')}: {res.get('title')}")
        elif args.complete:
            res = complete_task(args.complete, list_id=args.list_id, list_title=args.list_title)
            print(f"Completed task {res.get('id')}")
        else:
            parser.print_help()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
