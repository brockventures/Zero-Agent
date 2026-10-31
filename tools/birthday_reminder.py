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
TARGET_CHANNEL_ID = 1542081375287640084
CSV_PATH = Path(os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv"))
LAST_DECISION_STAGED: bool = False


def _should_stage(stage: bool | None) -> bool:
    """Determine whether a Decision Card should be staged."""
    if stage is True:
        return True
    if stage is False:
        return False
    return "pytest" not in sys.modules and not os.environ.get("PYTEST_CURRENT_TEST")

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR.parent))

def find_partner(person: dict, all_contacts: list[dict] | None = None) -> str | None:
    """Resolve partner name from structured 'Partner' column or notes regex."""
    partner_val = person.get("Partner", "").strip()
    if partner_val:
        return re.sub(r"\(.*?\)", "", partner_val).strip()

    pname = person.get("Name", "").strip()
    if all_contacts:
        for c in all_contacts:
            cname = c.get("Name", "").strip()
            if cname == pname:
                continue
            c_partner = c.get("Partner", "").strip()
            if c_partner:
                c_part = re.sub(r"\(.*?\)", "", c_partner).strip()
                if c_part.lower() == pname.lower():
                    return cname

    notes = person.get("Notes & Connections", "")
    if notes:
        m = re.search(
            r"(?:married to|married/partner to|spouse(?:\s+to|\s+is)?|husband(?:\s+to|\s+is)?|wife(?:\s+to|\s+is)?|life partner(?:\s+to|\s+is)?|partner to)\s+([A-Za-z\s\(\)\'\-]+?)(?:;|,|\(|\.|\n|$)",
            notes,
            re.IGNORECASE,
        )
        if m:
            return re.sub(r"\(.*?\)", "", m.group(1)).strip()
    return None

def check_birthdays(
    target_date: str | None = None,
    stage: bool | None = None,
) -> tuple[bool, str, list[dict]]:
    """Check for birthdays matching target_date (default: today in PT).
    
    target_date format: 'MM-DD' or 'YYYY-MM-DD'
    Returns: (has_birthdays: bool, formatted_message: str, matched_contacts: list[dict])
    """
    global LAST_DECISION_STAGED
    LAST_DECISION_STAGED = False
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
    all_contacts = []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            all_contacts.append(row)
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
        partner = find_partner(person, all_contacts)
        if partner and "Partner" not in person:
            person["Partner"] = partner
        
        first_name = name.split()[0]
        if "(" in first_name:
            first_name = first_name.split("(")[0].strip()
        
        lines.append(f"• **{name}** turns a year older today!")
        if partner:
            lines.append(f"  💍 **Partner:** {partner}")
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

    decision_staged = False
    if _should_stage(stage):
        try:
            from tools.forge import stage_decision

            names = ", ".join(p.get("Name", "Friend") for p in matched)
            summary_text = f"Birthday alert for {names} today ({display_date}). Send birthday wishes!"
            cmd = f"python3 /workspace/tools/birthday_reminder.py --date {today_mm_dd}"

            res = stage_decision(
                category="birthday",
                title=f"Birthday Alert: {names}",
                summary=summary_text,
                handler_type="shell",
                payload={
                    "command": cmd,
                    "cwd": "/workspace",
                },
                channel_id=TARGET_CHANNEL_ID,
                post_to_discord=True,
            )
            decision_staged = bool(res.get("ok", True) if isinstance(res, dict) else True)
        except Exception as e:
            print(f"[BirthdayReminder] Decision Card staging fallback: {e}", file=sys.stderr)

    LAST_DECISION_STAGED = decision_staged
    for person in matched:
        person["_decision_staged"] = decision_staged

    if not decision_staged:
        message = "\n".join(lines).strip() + "\n\n[CHOICES: " + " | ".join(buttons) + "]"
    else:
        message = "\n".join(lines).strip()

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
