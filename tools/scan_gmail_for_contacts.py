#!/usr/bin/env python3
"""High-precision Gmail and Address Book scanner to enrich and modernize contact info."""

import csv
import json
import os
import re
import urllib.request
import urllib.parse
from email.utils import parseaddr
from datetime import datetime

SECRETS_PATH = os.environ.get("GOOGLE_OAUTH_PATH", "/secrets/google_oauth.json")
if not os.path.exists(SECRETS_PATH) and os.path.exists("/workspace/config/google_oauth.json"):
    SECRETS_PATH = "/workspace/config/google_oauth.json"

CSV_PATH = os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv")
CARDS_CSV = "/workspace/data/christmas_card_address_book.csv"

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

def search_gmail(tok, query, max_results=6):
    q = urllib.parse.quote(query)
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={q}&maxResults={max_results}"
    r = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(r, timeout=12) as resp:
            data = json.loads(resp.read().decode())
            return data.get("messages", [])
    except Exception:
        return []

def get_message_detail(tok, msg_id):
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}?format=full"
    r = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(r, timeout=12) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None

def is_valid_personal_email(email):
    email = email.lower().strip()
    if not email or "@" not in email:
        return False
    disallowed = [
        "noreply", "no-reply", "donotreply", "notification", "support",
        "updates", "mailer-daemon", "google.com", "linkedin.com", "facebookmail.com",
        "amazonses.com", "stamps.com", "walmart.com", "evite.com", "minted.com",
        "target.com", "zillow.com", "paypal.com", "docusign.net", "invitations@linkedin.com"
    ]
    for d in disallowed:
        if d in email:
            return False
    return True

def run_scanner():
    tok = get_access_token()

    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        master = list(reader)

    # 1. First pass: Curated Christmas & Holiday Address Book
    # Maps explicit names / households to their modern active emails
    curated_emails = {
        "geoff lloyd": "geofflloyd421@gmail.com",
        "sarah lloyd": "sjleeman310@gmail.com",
        "sam carton": "samuel.carton@gmail.com",
        "valerie ong": "valerieonglg@gmail.com",
        "paul derby": "paul.derby@gmail.com",
        "kyle": "kylerawls@kw.com",
        "josine verhagen": "josineverhagen@gmail.com",
        "daniel van doorn": "vandoorndaniel@gmail.com",
        "katherine scott": "katherineyeescott@gmail.com",
        "katherine yee scott": "katherineyeescott@gmail.com",
        "alex albanese bennett": "alex.albanese7@gmail.com",
        "vuk brajušković": "vbrajuskovic@gmail.com",
        "vuk brajuskovic": "vbrajuskovic@gmail.com",
        "warren": "XWarrenCai@gmail.com",
        "daisy": "XWarrenCai@gmail.com",
        "jim allen": "jim.allen@arris.com",
        "james ('jim') allen": "jim.allen@arris.com",
        "anthony papa": "anthony.papa@gmail.com",
        "ian coley": "ian.coley@gmail.com",
        "ashley duncan": "aduncan8@me.com",
        "jessica chau": "jessicabchau@gmail.com",
        "jerome miller": "waterjerome@gmail.com",
        "andy schultz": "andy.schultz1@gmail.com",
        "natalie markovich": "nataliemarkovich@gmail.com",
        "melina butuci": "melina.butuci@gmail.com",
        "julia farache": "juliafarache@gmail.com"
    }

    updated_count = 0
    print("--- Phase 1: Applying Curated Modern Email Mapping ---")
    for row in master:
        name_lower = row["Name"].lower()
        clean = re.sub(r"\(.*?\)", "", name_lower).replace("née allen", "").replace("'", "").strip()
        
        for k, v in curated_emails.items():
            if k in name_lower or k in clean or clean in k:
                cur = row["Email Address"].strip()
                if cur != v and ("edu" in cur.lower() or "aol" in cur.lower() or "yahoo" in cur.lower() or "arrisi" in cur.lower() or not cur):
                    print(f"🔄 Updating '{row['Name']}': {cur or '[None]'} ➔ {v}")
                    row["Email Address"] = v
                    updated_count += 1
                break

    # 2. Phase 2: Live Gmail Search for specific key friends / family
    # Let's target friends where we have old college/outdated emails or missing emails
    scan_targets = [
        ("Anthony Papa", "Anthony"),
        ("Ian Coley", "Ian"),
        ("Sam Carton", "Sam"),
        ("Ashley Beck", "Ashley"),
        ("Jimmy Beck", "Jimmy"),
        ("Gianna Beck", "Gianna"),
        ("Erick Bennett", "Erick"),
        ("Raj Kumar", "Raj"),
        ("Derek Morris", "Derek"),
        ("Carolyn Morris", "Carolyn"),
        ("Stephanie Carrera", "Stephanie"),
        ("Iliia Krastev", "Iliia"),
        ("Pralav Shetty", "Pralav"),
        ("Drew Shetty", "Drew"),
        ("Chelsea Estacio", "Chelsea"),
        ("Zenturion Estacio", "Zenturion"),
        ("Chris Berkey", "Chris"),
        ("Reza", "Reza"),
        ("Mehrnoush", "Mehrnoush"),
        ("Eric", "Eric"),
        ("Prabal", "Prabal"),
        ("Mokshya", "Mokshya"),
        ("James Bryant", "James Bryant"),
        ("Charlotte Rajasingh", "Charlotte")
    ]

    print("\n--- Phase 2: Targeted Gmail Thread Search ---")
    for full_name, search_term in scan_targets:
        # Find matching row in master
        target_row = None
        for r in master:
            if full_name.lower() in r["Name"].lower() or r["Name"].lower() in full_name.lower():
                target_row = r
                break
        if not target_row:
            continue

        query = f'"{search_term}"'
        if " " in full_name:
            query = f'"{full_name}"'

        msgs = search_gmail(tok, query, max_results=5)
        discovered_emails = {}

        for m in msgs:
            md = get_message_detail(tok, m["id"])
            if not md:
                continue

            internal_date = int(md.get("internalDate", 0)) / 1000.0
            payload = md.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

            for hfield in ["from", "to", "cc"]:
                hval = headers.get(hfield, "")
                if hval:
                    for part in hval.split(","):
                        real_name, email_addr = parseaddr(part)
                        if is_valid_personal_email(email_addr):
                            first_match = search_term.lower() in real_name.lower() or search_term.lower() in email_addr.lower()
                            if first_match:
                                if email_addr not in discovered_emails or internal_date > discovered_emails[email_addr]:
                                    discovered_emails[email_addr] = internal_date

        if discovered_emails:
            sorted_emails = sorted(discovered_emails.items(), key=lambda x: x[1], reverse=True)
            newest_email, newest_ts = sorted_emails[0]
            newest_dt = datetime.fromtimestamp(newest_ts).strftime("%Y-%m")
            cur_email = target_row["Email Address"].strip()

            if not cur_email:
                print(f"✨ Found new email for '{target_row['Name']}': {newest_email} (Active {newest_dt})")
                target_row["Email Address"] = newest_email
                updated_count += 1
            elif cur_email.lower() != newest_email.lower():
                if "u.northwestern.edu" in cur_email.lower() or "illinois.edu" in cur_email.lower() or "stanford.edu" in cur_email.lower() or "aol.com" in cur_email.lower() or "yahoo.com" in cur_email.lower():
                    print(f"🔄 Modernizing email for '{target_row['Name']}': {cur_email} ➔ {newest_email} (Active {newest_dt})")
                    target_row["Email Address"] = newest_email
                    updated_count += 1

    # Write back CSV
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(master)

    print(f"\n✅ Total contact records refreshed: {updated_count}")

if __name__ == "__main__":
    run_scanner()
