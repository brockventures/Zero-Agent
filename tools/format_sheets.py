#!/usr/bin/env python3
"""Format and style Google Spreadsheet while preserving custom user columns (Core, Out of Town)."""

import csv
import json
import os
import urllib.request
import urllib.parse

SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", "/secrets/google_oauth.json")
if not os.path.exists(SECRETS_PATH) and os.path.exists("/workspace/config/google_oauth.json"):
    SECRETS_PATH = "/workspace/config/google_oauth.json"

CSV_PATH = os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv")
DEFAULT_SHEET_ID = os.environ.get("FRIENDS_SHEET_ID", "1ZAhET8stLzHTR3tsRWZVB9UAgIIEWH1Gs_9Nw3KQBv8")

def load_credentials(path):
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

def get_access_token():
    creds = load_credentials(SECRETS_PATH)
    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]

def format_spreadsheet(sheet_id=DEFAULT_SHEET_ID):
    from push_friends_to_sheets import push_to_google_sheets
    return push_to_google_sheets(sheet_id)

if __name__ == "__main__":
    try:
        url = format_spreadsheet()
        print(f"SUCCESS_URL={url}")
    except Exception as e:
        print("Error formatting spreadsheet:", e)
