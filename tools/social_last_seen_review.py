#!/usr/bin/env python3
"""Weekly Social & Last Seen Review Sidecar.

Runs weekly (Sunday evening @ 8:30 PM PT).
Reviews the past 7 days of calendar events, text messages (SMS/RCS via OpenMessage),
and communications to detect social outings, gatherings, and personal interactions.
If candidates are identified:
  - Formats a message presenting the proposed updates.
  - Adds interactive Discord buttons to confirm or ignore.
  - Stashes pending updates in /workspace/data/pending_last_seen_updates.json.
Silent if no new qualifying social interactions were identified.
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/workspace/data"))
CSV_PATH = Path(os.environ.get("FRIENDS_CSV_PATH", "/workspace/data/friends_and_family_master.csv"))
MD_PATH = Path(os.environ.get("FRIENDS_MD_PATH", "/workspace/memory/private/private_friends_and_family.md"))
PENDING_FILE = DATA_DIR / "pending_last_seen_updates.json"

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR.parent))

try:
    from workspace_mcp import _auth_headers, CALENDAR_BASE, GMAIL_BASE, FAMILY_CALENDARS
except Exception:
    _auth_headers = None

try:
    from sidecars import _ssh_cmd, _resolve_nas_config
except Exception:
    def _ssh_cmd(host, cmd, timeout=30):
        import subprocess
        ssh_key = os.environ.get("NAS_SSH_KEY", "/secrets/id_ed25519" if os.path.exists("/secrets/id_ed25519") else "/root/.ssh/id_ed25519")
        ssh_user = os.environ.get("NAS_SSH_USER", "Brock")
        ssh_port = os.environ.get("NAS_SSH_PORT") or str(49000 + 876)
        return subprocess.run([
            "ssh", "-i", ssh_key, "-p", ssh_port,
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{ssh_user}@{host}", cmd
        ], capture_output=True, text=True, timeout=timeout)

    def _resolve_nas_config():
        return os.environ.get("NAS_HOST_1_IP", "127.0.0.1"), os.environ.get("NAS_HOST_2_IP", "127.0.0.1"), os.environ.get("NAS_SSH_PORT") or str(49000 + 876)

HOUSEHOLD_MEMBERS = {
    "Ryan Brock",
    "Emily Brock (née Allen)",
    "Emily Brock",
    "Rosalind ('Rosie') Brock",
    "Rosie Brock",
    "Isaac Brock",
    "Baby Girl #3 Brock"
}

ROUTINE_SKIP_KEYWORDS = [
    "orthodontist", "dentist", "doctor", "ob-gyn", "transfer of care",
    "consultation", "pickup", "dropoff", "swimming lesson", "solo", "workout",
    "pt session", "meeting", "1:1", "sync", "standup", "interview", "review",
    "plumber", "contractor", "invoice", "oil change", "repair", "service",
    "checkup", "exam", "vet", "physician", "pediatrician"
]

def normalize_phone(phone_str: str) -> str:
    """Normalize phone number to standard 10-digit format for matching."""
    if not phone_str:
        return ""
    digits = re.sub(r"\D", "", phone_str)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    if len(digits) > 10:
        return digits[-10:]
    return digits

def load_contacts() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def build_contact_indexes(contacts: list[dict]):
    """Build normalized indexes for phone, email, and name lookups."""
    phone_map = {}
    email_map = {}
    name_map = {}

    for person in contacts:
        name = person.get("Name", "").strip()
        if not name or name in HOUSEHOLD_MEMBERS:
            continue

        # Phone indexing
        p = normalize_phone(person.get("Phone Number", ""))
        if p and len(p) == 10:
            phone_map[p] = person

        # Extract secondary phones from Notes column
        notes = person.get("Notes & Connections", "")
        for alt_p in re.findall(r"\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", notes):
            norm_alt = normalize_phone(alt_p)
            if norm_alt and len(norm_alt) == 10:
                phone_map[norm_alt] = person

        # Email indexing
        em = person.get("Email Address", "").strip().lower()
        if em and "@" in em:
            email_map[em] = person

        # Name indexing (clean tokens)
        clean_name = re.sub(r"\(.*?\)", "", name).strip().lower()
        clean_name = clean_name.replace("née allen", "").replace("'", "").strip()
        name_map[clean_name] = person

    return phone_map, email_map, name_map

def fetch_recent_calendar_events(days: int = 7) -> list[dict]:
    if not _auth_headers:
        return []
    now_utc = datetime.now(timezone.utc)
    time_min = (now_utc - timedelta(days=days)).isoformat()
    time_max = now_utc.isoformat()
    try:
        headers = _auth_headers()
    except Exception:
        return []

    events = []
    for cid, cname in [("primary", "Ryan"), ("family05249951047154432652@group.calendar.google.com", "Family")]:
        try:
            params = urllib.parse.urlencode({
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": 100
            })
            req = urllib.request.Request(f"{CALENDAR_BASE}/calendars/{urllib.parse.quote(cid)}/events?{params}", headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                events.extend(data.get("items", []))
        except Exception:
            pass
    return events

def fetch_recent_text_messages(days: int = 7) -> tuple[list[dict], dict]:
    """Query Google Messages via OpenMessage container on Host2 (.84)."""
    _, host_2, _ = _resolve_nas_config()
    since_date = (datetime.now(PT) - timedelta(days=days)).strftime("%Y-%m-%d")

    msgs = []
    conv_participants = {}

    try:
        # 1. Fetch recent messages
        cmd_msgs = f'docker exec openmessage openmessage read "" --since {since_date} --limit 500 --json'
        res_msgs = _ssh_cmd(host_2, cmd_msgs, timeout=25)
        if res_msgs.returncode == 0 and res_msgs.stdout.strip():
            msgs = json.loads(res_msgs.stdout)

        # 2. Fetch conversation threads for participant mapping
        cmd_threads = 'docker exec openmessage openmessage threads --json'
        res_threads = _ssh_cmd(host_2, cmd_threads, timeout=20)
        if res_threads.returncode == 0 and res_threads.stdout.strip():
            threads = json.loads(res_threads.stdout)
            for t in threads:
                cid = str(t.get("ConversationID"))
                parts_raw = t.get("Participants", "[]")
                try:
                    parts = json.loads(parts_raw) if isinstance(parts_raw, str) else parts_raw
                except Exception:
                    parts = []
                other_parts = []
                for p in parts:
                    if not p.get("is_me"):
                        other_parts.append({
                            "name": p.get("name", ""),
                            "number": normalize_phone(p.get("number", ""))
                        })
                conv_participants[cid] = {
                    "name": t.get("Name", ""),
                    "is_group": t.get("IsGroup", False),
                    "participants": other_parts
                }
    except Exception as e:
        print(f"[SocialReview] Warning fetching text messages: {e}")

    return msgs, conv_participants

def is_date_newer(candidate_date: str, current_last_seen: str) -> bool:
    """Determine if candidate_date (YYYY-MM-DD) is strictly newer than current_last_seen."""
    if not candidate_date:
        return False
    clean_curr = (current_last_seen or "").strip()
    if not clean_curr or clean_curr == "-":
        return True
    if re.match(r"^\d{4}-\d{2}-\d{2}$", clean_curr):
        return candidate_date > clean_curr
    # If in text format like "Jul, '26" or "2026-07-01", standard string comparison or True
    return candidate_date > clean_curr

def identify_social_updates(days: int = 7) -> list[dict]:
    """Scan calendar events and text messages for social gatherings & interactions."""
    contacts = load_contacts()
    if not contacts:
        return []

    phone_map, email_map, name_map = build_contact_indexes(contacts)

    # Dictionary: contact_name -> { "contact": dict, "interactions": list[dict], "latest_date": str }
    detected_by_person = {}

    def record_interaction(person_dict: dict, date_str: str, source: str, summary: str, location: str = ""):
        name = person_dict.get("Name", "")
        if not name or name in HOUSEHOLD_MEMBERS:
            return

        curr_last_seen = person_dict.get("Last Seen", "").strip() or "-"
        if name not in detected_by_person:
            detected_by_person[name] = {
                "name": name,
                "contact": person_dict,
                "current_last_seen": curr_last_seen,
                "latest_date": date_str,
                "interactions": []
            }

        entry = detected_by_person[name]
        if date_str > entry["latest_date"]:
            entry["latest_date"] = date_str

        # Add interaction record
        entry["interactions"].append({
            "date": date_str,
            "source": source,
            "summary": summary,
            "location": location
        })

    # ---------------------------------------------------------
    # 1. Scan Calendar Events
    # ---------------------------------------------------------
    events = fetch_recent_calendar_events(days)
    for ev in events:
        summary = ev.get("summary", "").strip()
        desc = ev.get("description", "").strip()
        loc = ev.get("location", "").strip()
        attendees = [a.get("displayName") or a.get("email", "") for a in ev.get("attendees", [])]
        start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
        if not start:
            continue
        event_date = start[:10]
        full_text = f"{summary} {desc} {loc} {' '.join(attendees)}".lower()

        if any(kw in full_text for kw in ROUTINE_SKIP_KEYWORDS):
            continue

        for person in contacts:
            name = person.get("Name", "")
            if name in HOUSEHOLD_MEMBERS:
                continue

            name_clean = re.sub(r"\(.*?\)", "", name).strip().lower()
            name_clean = name_clean.replace("née allen", "").replace("'", "").strip()
            tokens = [t for t in name_clean.split() if len(t) > 2]
            matched = False

            if name_clean and name_clean in full_text:
                matched = True
            elif len(tokens) >= 2 and all(t in full_text for t in tokens):
                matched = True
            elif person.get("Email Address") and person.get("Email Address").lower() in full_text:
                matched = True

            if matched:
                record_interaction(
                    person_dict=person,
                    date_str=event_date,
                    source="Calendar",
                    summary=summary or "Calendar Event",
                    location=loc
                )

    # ---------------------------------------------------------
    # 2. Scan Text Messages (OpenMessage RCS/SMS)
    # ---------------------------------------------------------
    msgs, conv_participants = fetch_recent_text_messages(days)
    msg_aggregates = {}  # (person_name, msg_date) -> list of message bodies

    for m in msgs:
        sname = (m.get("SenderName") or "").strip()
        snum = normalize_phone(m.get("SenderNumber") or "")
        cid = str(m.get("ConversationID") or "")
        is_from_me = m.get("IsFromMe", False)
        ts_ms = m.get("TimestampMS", 0)
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=PT) if ts_ms else datetime.now(PT)
        msg_date = dt.strftime("%Y-%m-%d")
        body = (m.get("Body") or "").strip()

        # Sanitize 2FA / OTP
        if re.search(r"\b\d{4,8}\b", body) and any(w in body.lower() for w in ("code", "verification", "pin", "sudo")):
            body = "[REDACTED 2FA]"

        matched_contacts = []
        if not is_from_me:
            # Incoming: match sender
            if snum in phone_map:
                matched_contacts.append(phone_map[snum])
            elif sname:
                sname_clean = re.sub(r"\(.*?\)", "", sname).strip().lower()
                for cname_clean, c in name_map.items():
                    if cname_clean in sname_clean or (len(cname_clean.split()) >= 2 and all(tok in sname_clean for tok in cname_clean.split() if len(tok) > 2)):
                        matched_contacts.append(c)
                        break
        else:
            # Outgoing: match thread participants
            thread_info = conv_participants.get(cid)
            if thread_info:
                for p in thread_info.get("participants", []):
                    pnum = p.get("number")
                    pname = p.get("name")
                    if pnum in phone_map:
                        matched_contacts.append(phone_map[pnum])
                    elif pname:
                        pname_clean = re.sub(r"\(.*?\)", "", pname).strip().lower()
                        for cname_clean, c in name_map.items():
                            if cname_clean in pname_clean or (len(cname_clean.split()) >= 2 and all(tok in pname_clean for tok in cname_clean.split() if len(tok) > 2)):
                                matched_contacts.append(c)
                                break

        for person in matched_contacts:
            pname = person.get("Name")
            if pname in HOUSEHOLD_MEMBERS:
                continue
            key = (pname, msg_date)
            if key not in msg_aggregates:
                msg_aggregates[key] = {
                    "person": person,
                    "date": msg_date,
                    "count": 0,
                    "sample": ""
                }
            msg_aggregates[key]["count"] += 1
            if body and not msg_aggregates[key]["sample"] and not body.startswith("http"):
                msg_aggregates[key]["sample"] = body[:80]

    for (pname, msg_date), agg in msg_aggregates.items():
        sample_txt = f': "{agg["sample"]}"' if agg["sample"] else ""
        cnt_txt = f" ({agg['count']} msgs)" if agg["count"] > 1 else ""
        summary_str = f"Text message exchange{cnt_txt}{sample_txt}"
        record_interaction(
            person_dict=agg["person"],
            date_str=msg_date,
            source="Text Message",
            summary=summary_str
        )

    # ---------------------------------------------------------
    # 3. Filter for strictly newer Last Seen proposals
    # ---------------------------------------------------------
    proposed_updates = []
    for pname, item in detected_by_person.items():
        curr_last_seen = item["current_last_seen"]
        latest_date = item["latest_date"]

        if is_date_newer(latest_date, curr_last_seen):
            # Pick the most prominent / representative interaction
            primary_interaction = item["interactions"][-1]
            for inter in item["interactions"]:
                if inter["date"] == latest_date:
                    primary_interaction = inter
                    if inter["source"] == "Calendar":
                        break

            proposed_updates.append({
                "name": pname,
                "current_last_seen": curr_last_seen,
                "proposed_last_seen": latest_date,
                "event_summary": primary_interaction.get("summary", ""),
                "event_date": latest_date,
                "location": primary_interaction.get("location", ""),
                "source": primary_interaction.get("source", "Interaction"),
                "all_interactions": item["interactions"]
            })

    # Sort proposals by proposed_last_seen descending
    proposed_updates.sort(key=lambda x: x["proposed_last_seen"], reverse=True)
    return proposed_updates

def format_review_message(updates: list[dict]) -> tuple[bool, str]:
    if not updates:
        return False, "No qualifying social events or new interactions identified in the past week."

    lines = [
        "🗓️ **Weekly Social & Last Seen Review**",
        f"Identified **{len(updates)}** candidate social interaction(s) from the past week:\n"
    ]

    for i, u in enumerate(updates, 1):
        loc_str = f" at {u['location']}" if u.get('location') else ""
        src_str = f" `[{u.get('source', 'Interaction')}]`" if u.get('source') else ""
        qualifier = " *(Digital Contact)*" if u.get("source") == "Text Message" else " *(In-Person)*"
        lines.append(f"{i}. **{u['name']}**")
        lines.append(f"   • **Interaction:** _{u['event_summary']}_{loc_str} on `{u['event_date']}`{src_str}")
        lines.append(f"   • **Proposed Update:** `{u['current_last_seen']}` ➔ `{u['proposed_last_seen']}`{qualifier}\n")

    lines.append("_Please confirm accuracy before I write these updates to the master dataset and Google Sheet._")
    lines.append("\n[CHOICES: Confirm & Apply Last Seen Updates | Ignore Last Seen Updates]")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(updates, f, indent=2)

    return True, "\n".join(lines)

def apply_pending_updates() -> tuple[bool, str]:
    if not PENDING_FILE.exists():
        return False, "No pending Last Seen updates found."
    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        pending = json.load(f)
    if not pending:
        return False, "Pending updates file is empty."

    # 1. Update CSV
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    update_map = {item["name"]: item["proposed_last_seen"] for item in pending}
    applied_count = 0

    for r in rows:
        if r["Name"] in update_map:
            r["Last Seen"] = update_map[r["Name"]]
            applied_count += 1

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # 2. Update private_friends_and_family.md if exists
    if MD_PATH.exists():
        try:
            with open(MD_PATH, "r", encoding="utf-8") as f:
                md_content = f.read()

            for name, new_date in update_map.items():
                # Convert YYYY-MM-DD to Month, 'YY for memory doc consistency or ISO
                try:
                    dt = datetime.strptime(new_date, "%Y-%m-%d")
                    date_repr = dt.strftime("%b, '%y")
                except Exception:
                    date_repr = new_date

                # Match patterns like: **Name:** ... Last Seen: Jul, '26
                # Or insert if not present
                escaped_name = re.escape(name)
                pattern = rf"(\*\*{escaped_name}[\*\:][^\n]*?Last Seen:\s*)([^\)\n]+)"
                if re.search(pattern, md_content):
                    md_content = re.sub(pattern, rf"\g<1>{date_repr}", md_content)

            with open(MD_PATH, "w", encoding="utf-8") as f:
                f.write(md_content)
        except Exception as e:
            print(f"[SocialReview] Warning updating markdown memory: {e}")

    # 3. Sync to live Google Sheets
    sheets_synced = False
    try:
        from push_friends_to_sheets import push_to_google_sheets
        push_to_google_sheets()
        sheets_synced = True
    except Exception as e:
        print(f"[SocialReview] Warning pushing to Google Sheets: {e}")

    PENDING_FILE.unlink(missing_ok=True)
    sheet_note = " and synced to live Google Sheet." if sheets_synced else " (Google Sheet sync failed)."
    return True, f"✅ Successfully updated Last Seen for {applied_count} contact(s){sheet_note}"

def main():
    parser = argparse.ArgumentParser(description="Zero Weekly Social & Last Seen Review")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    parser.add_argument("--apply", action="store_true", help="Apply currently pending updates")
    parser.add_argument("--quiet", action="store_true", help="Suppress output if no events")
    parser.add_argument("--json", action="store_true", help="Output results in JSON")
    args = parser.parse_args()

    if args.apply:
        ok, msg = apply_pending_updates()
        print(msg)
        return

    updates = identify_social_updates(days=args.days)
    has_events, msg = format_review_message(updates)

    if args.json:
        print(json.dumps({
            "has_events": has_events,
            "count": len(updates),
            "updates": updates,
            "message": msg
        }, indent=2))
        return

    if has_events:
        print(msg)
    elif not args.quiet:
        print(msg)

if __name__ == "__main__":
    main()
