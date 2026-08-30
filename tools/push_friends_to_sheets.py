#!/usr/bin/env python3
"""Create and populate Google Spreadsheet with Master Friends & Family Dataset."""

import csv
import json
import os
import urllib.request
import urllib.parse

SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", "/secrets/google_oauth.json")
if not os.path.exists(SECRETS_PATH) and os.path.exists("/workspace/config/google_oauth.json"):
    SECRETS_PATH = "/workspace/config/google_oauth.json"

CSV_PATH = os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv")

def push_to_google_sheets():
    with open(SECRETS_PATH) as f:
        creds = json.load(f)

    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        tok = json.loads(resp.read().decode())["access_token"]

    # 1. Create Spreadsheet
    sheet_body = {
        "properties": {
            "title": "Private Network: Friends, Family & Neighbors Master"
        }
    }
    s_req = urllib.request.Request(
        "https://sheets.googleapis.com/v4/spreadsheets",
        data=json.dumps(sheet_body).encode("utf-8"),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(s_req) as s_resp:
        sheet_info = json.loads(s_resp.read().decode())
        sheet_id = sheet_info["spreadsheetId"]
        sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        print(f"✅ Created Google Spreadsheet! ID: {sheet_id}")
        print(f"🔗 URL: {sheet_url}")

    # 2. Ingest CSV data
    rows = []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            rows.append(row)

    # 3. Populate rows via valueRange
    append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/A1:append?valueInputOption=USER_ENTERED"
    val_body = {"values": rows}
    a_req = urllib.request.Request(
        append_url,
        data=json.dumps(val_body).encode("utf-8"),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(a_req) as a_resp:
        res = json.loads(a_resp.read().decode())
        print(f"✅ Populated {len(rows)} rows into Google Sheet!")

    return sheet_url

if __name__ == "__main__":
    try:
        url = push_to_google_sheets()
        print(f"SUCCESS_URL={url}")
    except Exception as e:
        print("Error:", e)
