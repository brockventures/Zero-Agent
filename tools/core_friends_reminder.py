#!/usr/bin/env python3
"""Monthly Core Friends & Family Reconnect Reminder Sidecar.

Runs monthly on the 1st of the month (@ 9:00 AM PT).
Scans friends_and_family_master.csv for friends marked:
  - Core == TRUE
  - Out of Town == FALSE
  - Last Seen >= 8 weeks ago (or never recorded)
Excludes immediate household members.
Pairs partners/couples into a single consolidated line.
Formats a clean reminder listing elapsed time to prompt social planning.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
CSV_PATH = Path(os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv"))

HOUSEHOLD_MEMBERS = {
    "Ryan Brock",
    "Emily Brock (née Allen)",
    "Emily Brock",
    "Rosalind ('Rosie') Brock",
    "Rosie Brock",
    "Isaac Brock",
    "Baby Girl #3 Brock"
}

def load_contacts() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def parse_last_seen_date(last_seen_str: str) -> date | None:
    """Parse last seen string into a date object if possible."""
    if not last_seen_str:
        return None
    s = last_seen_str.strip()
    if not s or s == "-":
        return None
    # ISO format YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            pass
    # Month, 'YY format e.g. "Jul, '26" or "Aug, 2026"
    m_match = re.match(r"^([A-Za-z]{3,9}),?\s*'?(\d{2,4})$", s)
    if m_match:
        m_str, y_str = m_match.groups()
        if len(y_str) == 2:
            y_str = f"20{y_str}"
        try:
            dt = datetime.strptime(f"{m_str[:3]} 1 {y_str}", "%b %d %Y")
            return dt.date()
        except ValueError:
            pass
    return None

def find_partner(person: dict, all_contacts: list[dict]) -> dict | None:
    """Identify if the person has a partner or spouse in the contacts database."""
    notes = person.get("Notes & Connections", "")
    pname = person.get("Name", "").strip()
    paddr = person.get("Physical Address", "").strip()

    patterns = [
        r"(?:married to|married/partner to|spouse(?:\s+to|\s+is)?|husband(?:\s+to|\s+is)?|wife(?:\s+to|\s+is)?|life partner(?:\s+to|\s+is)?)\s+([A-Za-z\s\(\)\'\-]+?)(?:;|,|\(|\.|\n|$)",
        r"(?:partner to)\s+([A-Za-z\s\(\)\'\-]+?)(?:;|,|\(|\.|\n|$)",
        r"(?<!business\s)(?<!managing\s)(?<!equity\s)(?<!law\s)(?<!senior\s)(?<!junior\s)(?<!founding\s)(?<!practice\s)(?<!trading\s)\bpartner[:\s]+([A-Za-z\s\(\)\'\-]+?)(?:;|,|\(|\.|\n|$)",
    ]

    # 1. Partner in person's notes
    for pat in patterns:
        m = re.search(pat, notes, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            candidate = re.sub(r"\(.*?\)", "", candidate).strip()
            for c in all_contacts:
                cname = c.get("Name", "").strip()
                if cname == pname:
                    continue
                cname_clean = re.sub(r"\(.*?\)", "", cname).strip()
                if candidate.lower() == cname_clean.lower():
                    return c
                if len(candidate) > 2 and (candidate.lower() in cname_clean.lower() or cname_clean.lower() in candidate.lower()):
                    return c
                if candidate.split() and candidate.split()[0].lower() == cname.split()[0].lower():
                    if paddr and paddr == c.get("Physical Address", "").strip():
                        return c

    # 2. Check if another contact lists this person as spouse / partner
    for c in all_contacts:
        cname = c.get("Name", "").strip()
        if cname == pname:
            continue
        cnotes = c.get("Notes & Connections", "")
        for pat in patterns:
            m = re.search(pat, cnotes, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()
                candidate = re.sub(r"\(.*?\)", "", candidate).strip()
                pname_clean = re.sub(r"\(.*?\)", "", pname).strip()
                if candidate.lower() == pname_clean.lower() or candidate.lower() in pname_clean.lower():
                    return c
                if candidate.split() and candidate.split()[0].lower() == pname.split()[0].lower():
                    if paddr and paddr == c.get("Physical Address", "").strip():
                        return c

    return None

def format_couple_display_name(person: dict, partner: dict | None) -> str:
    """Format single name or combined couple name."""
    name1 = person.get("Name", "").strip()
    if not partner:
        return re.sub(r"\(.*?\)", "", name1).strip()

    name2 = partner.get("Name", "").strip()
    c1 = re.sub(r"\(.*?\)", "", name1).strip()
    c2 = re.sub(r"\(.*?\)", "", name2).strip()
    c1 = re.sub(r"\s+", " ", c1)
    c2 = re.sub(r"\s+", " ", c2)

    parts1 = c1.split()
    parts2 = c2.split()

    first1 = parts1[0] if parts1 else ""
    first2 = parts2[0] if parts2 else ""
    last1 = parts1[-1] if len(parts1) > 1 else ""
    last2 = parts2[-1] if len(parts2) > 1 else ""

    if last1 and last2 and last1.lower() == last2.lower():
        return f"{first1} & {first2} {last1}"
    else:
        return f"{c1} & {c2}"

def check_core_friends_unseen(weeks: int = 8, as_of_date: str | None = None) -> tuple[bool, str, list[dict]]:
    """Identify Core local friends who have not been seen in >= weeks.
    
    Returns:
        (has_friends: bool, formatted_message: str, qualifying_friends: list[dict])
    """
    contacts = load_contacts()
    if not contacts:
        return False, "No contacts found in master database.", []

    ref_date = datetime.strptime(as_of_date, "%Y-%m-%d").date() if as_of_date else datetime.now(PT).date()
    cutoff_days = weeks * 7
    cutoff_date = ref_date - timedelta(days=cutoff_days)

    qualifying = []
    seen_names = set()

    for person in contacts:
        name = person.get("Name", "").strip()
        if not name or name in HOUSEHOLD_MEMBERS or name in seen_names:
            continue

        core = str(person.get("Core", "")).strip().upper() == "TRUE"
        out_of_town = str(person.get("Out of Town", "")).strip().upper() == "TRUE"

        # Criteria: Core group and NOT out of town (local)
        if not core or out_of_town:
            continue

        partner = find_partner(person, contacts)
        partner_name = partner.get("Name", "").strip() if partner else ""

        # Mark both as seen so we don't duplicate
        seen_names.add(name)
        if partner_name:
            seen_names.add(partner_name)

        raw_last_seen = person.get("Last Seen", "").strip()
        ls_date = parse_last_seen_date(raw_last_seen)

        # If partner has a newer or recorded date, use that if person has None
        if partner:
            p_last_seen = partner.get("Last Seen", "").strip()
            p_ls_date = parse_last_seen_date(p_last_seen)
            if ls_date is None and p_ls_date is not None:
                ls_date = p_ls_date
                raw_last_seen = p_last_seen

        display_name = format_couple_display_name(person, partner)

        if ls_date is None:
            # Never seen or no date recorded
            qualifying.append({
                "name": name,
                "partner_name": partner_name,
                "display_name": display_name,
                "last_seen_raw": raw_last_seen or "-",
                "last_seen_date": None,
                "days_ago": 9999,
                "weeks_ago": None,
                "time_str": "Never recorded / No recent date",
                "phone": person.get("Phone Number", "").strip(),
                "email": person.get("Email Address", "").strip(),
                "address": person.get("Physical Address", "").strip(),
                "notes": person.get("Notes & Connections", "").strip()
            })
        elif ls_date <= cutoff_date:
            # Qualified as unseen for >= cutoff_days
            days_ago = (ref_date - ls_date).days
            weeks_ago = round(days_ago / 7.0, 1)
            time_str = f"`{ls_date.isoformat()}` (~{int(weeks_ago)} weeks / {days_ago} days ago)"
            qualifying.append({
                "name": name,
                "partner_name": partner_name,
                "display_name": display_name,
                "last_seen_raw": raw_last_seen,
                "last_seen_date": ls_date.isoformat(),
                "days_ago": days_ago,
                "weeks_ago": weeks_ago,
                "time_str": time_str,
                "phone": person.get("Phone Number", "").strip(),
                "email": person.get("Email Address", "").strip(),
                "address": person.get("Physical Address", "").strip(),
                "notes": person.get("Notes & Connections", "").strip()
            })

    if not qualifying:
        return False, f"All local Core friends have been seen within the past {weeks} weeks. ✅", []

    # Sort: longest unseen first (highest days_ago)
    qualifying.sort(key=lambda x: -x["days_ago"])

    # Format message
    date_header = ref_date.strftime("%B %-d, %Y")
    lines = [
        f"👋 **Monthly Core Friend Social Planning Check** — {date_header}",
        f"Identified **{len(qualifying)}** local Core friend(s) who haven't been seen in at least **{weeks} weeks** ({cutoff_days} days):\n"
    ]

    for i, p in enumerate(qualifying, 1):
        disp = p.get("display_name") or p["name"]
        lines.append(f"{i}. **{disp}**")
        lines.append(f"   • **Last Seen:** {p['time_str']}\n")

    lines.append("_Consider reaching out to organize dinner, drinks, playdates, or weekend plans!_")
    lines.append("\n[CHOICES: View Full Friends Sheet | Remind Me in 2 Weeks | Dismiss]")

    return True, "\n".join(lines).strip(), qualifying

def main():
    parser = argparse.ArgumentParser(description="Zero Monthly Core Friends Reconnect Reminder")
    parser.add_argument("--weeks", type=int, default=8, help="Threshold in weeks unseen (default: 8)")
    parser.add_argument("--as-of-date", type=str, default=None, help="Reference date YYYY-MM-DD (default: today)")
    parser.add_argument("--quiet", action="store_true", help="Suppress output if 0 friends meet criteria")
    parser.add_argument("--json", action="store_true", help="Output results in JSON")
    args = parser.parse_args()

    has_friends, msg, qualifying = check_core_friends_unseen(weeks=args.weeks, as_of_date=args.as_of_date)

    if args.json:
        print(json.dumps({
            "has_friends": has_friends,
            "count": len(qualifying),
            "weeks_threshold": args.weeks,
            "friends": qualifying,
            "message": msg
        }, indent=2))
        return

    if has_friends:
        print(msg)
    elif not args.quiet:
        print(msg)

if __name__ == "__main__":
    main()
