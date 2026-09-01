#!/usr/bin/env python3
"""Monthly Core Friends & Family Reconnect Reminder Sidecar.

Runs monthly on the 1st of the month (@ 9:00 AM PT).
Scans friends_and_family_master.csv for friends marked:
  - Core == TRUE
  - Out of Town == FALSE
  - Last Seen >= 8 weeks ago (or never recorded)
Excludes immediate household members.
Formats a clean reminder listing elapsed time and contact context to prompt social planning.
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

    for person in contacts:
        name = person.get("Name", "").strip()
        if not name or name in HOUSEHOLD_MEMBERS:
            continue

        core = str(person.get("Core", "")).strip().upper() == "TRUE"
        out_of_town = str(person.get("Out of Town", "")).strip().upper() == "TRUE"

        # Criteria: Core group and NOT out of town (local)
        if not core or out_of_town:
            continue

        raw_last_seen = person.get("Last Seen", "").strip()
        ls_date = parse_last_seen_date(raw_last_seen)

        if ls_date is None:
            # Never seen or no date recorded
            days_ago = None
            weeks_ago = None
            qualifying.append({
                "name": name,
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
        loc_str = ""
        if p["address"]:
            # Extract city/state if present
            m = re.search(r",\s*([^,]+,\s*[A-Z]{2}(?:\s*\d{5})?)", p["address"])
            if m:
                loc_str = f" ({m.group(1).strip()})"
            else:
                loc_str = f" ({p['address'][:35]})"

        lines.append(f"{i}. **{p['name']}**{loc_str}")
        lines.append(f"   • **Last Seen:** {p['time_str']}")
        if p["notes"]:
            notes_snip = p['notes']
            if len(notes_snip) > 100:
                notes_snip = notes_snip[:97] + "..."
            lines.append(f"   • **Notes:** _{notes_snip}_")
        
        contact_bits = []
        if p["phone"]:
            contact_bits.append(p["phone"])
        if p["email"]:
            contact_bits.append(f"`{p['email']}`")
        if contact_bits:
            lines.append(f"   • **Contact:** {' · '.join(contact_bits)}")
        lines.append("")

    lines.append("_Consider reaching out to organize dinner, drinks, playdates, or weekend plans!_")
    lines.append("\n[CHOICES: View Full Friends Sheet | Remind Me in 2 Weeks | Dismiss]")

    return True, "\n".join(lines), qualifying

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
