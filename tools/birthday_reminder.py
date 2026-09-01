#!/usr/bin/env python3
"""Daily Birthday Reminder Sidecar.

Runs daily at 7:00 AM PT.
Scans /workspace/data/friends_and_family_master.csv for any contacts whose birthday matches today.
If matches are found, formats an alert with interactive Discord buttons to text them.
Silent if no birthdays today.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
CSV_PATH = Path(os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv"))

def check_birthdays(target_date: str | None = None) -> tuple[bool, str, list[dict]]:
    """Check for birthdays matching target_date (default: today in PT).
    
    target_date format: 'MM-DD' or 'YYYY-MM-DD'
    Returns: (has_birthdays: bool, formatted_message: str, matched_contacts: list[dict])
    """
    now_pt = datetime.now(PT)
    if not target_date:
        today_mm_dd = now_pt.strftime("%m-%d")
        display_date = now_pt.strftime("%A, %B %d")
    else:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", target_date):
            today_mm_dd = target_date[5:]
            dt = datetime.strptime(target_date, "%Y-%m-%d")
            display_date = dt.strftime("%A, %B %d")
        elif re.match(r"^\d{2}-\d{2}$", target_date):
            today_mm_dd = target_date
            dt = datetime.strptime(f"{now_pt.year}-{target_date}", "%Y-%m-%d")
            display_date = dt.strftime("%B %d")
        else:
            today_mm_dd = target_date
            display_date = target_date

    if not CSV_PATH.exists():
        return False, f"⚠️ Contacts file not found at {CSV_PATH}", []

    matched = []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bday = row.get("Birthday", "").strip()
            if not bday or bday.startswith("Expected"):
                continue
            
            b_clean = bday.lstrip("-")
            if re.match(r"^\d{4}-\d{2}-\d{2}$", b_clean):
                b_clean = b_clean[5:]
            
            if b_clean == today_mm_dd:
                matched.append(row)

    if not matched:
        return False, f"No birthdays on {display_date} ({today_mm_dd}).", []

    lines = [f"🎂 **Birthday Alert — {display_date}**\n"]
    buttons = []

    for person in matched:
        name = person.get("Name", "Unknown")
        phone = person.get("Phone Number", "")
        notes = person.get("Notes & Connections", "")
        
        first_name = name.split()[0]
        if "(" in first_name:
            first_name = first_name.split("(")[0].strip()
        
        lines.append(f"• **{name}** turns a year older today!")
        if phone:
            lines.append(f"  📞 **Phone:** `{phone}`")
        if notes:
            lines.append(f"  📝 **Notes:** _{notes}_")
        lines.append("")

        if phone:
            buttons.append(f'Text {first_name} "Happy birthday!"')

    if not buttons:
        buttons.append("Dismiss")
    elif len(buttons) == 1:
        buttons.append("Remind Me Later")

    choices_tag = f"[CHOICES: '" + " | ".join(buttons) + "]"
    message = "\n".join(lines).strip() + f"\n\n[CHOICES: " + " | ".join(buttons) + "]"

    return True, message, matched

def main():
    parser = argparse.ArgumentParser(description="Zero Daily Birthday Reminder Sidecar")
    parser.add_argument("--date", help="Test specific MM-DD or YYYY-MM-DD date")
    parser.add_argument("--quiet", action="store_true", help="Suppress output if no birthdays")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")

    args = parser.parse_args()

    has_birthdays, msg, contacts = check_birthdays(args.date)

    if args.json:
        print(json.dumps({
            "has_birthdays": has_birthdays,
            "count": len(contacts),
            "contacts": contacts,
            "message": msg
        }, indent=2))
        return

    if has_birthdays:
        print(msg)
    elif not args.quiet:
        print(msg)

if __name__ == "__main__":
    main()
