#!/usr/bin/env python3
"""Weekly Household Radar (Sundays @ 20:00 PT).

Covers:
1. 7-Day Week Ahead Schedule Radar (Google Calendar).
2. 30-Day Contract & Subscription Renewal Lookahead (reminders.json).
"""

import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from workspace_mcp import calendar_list_events, gmail_search

PT = ZoneInfo("America/Los_Angeles")

def get_upcoming_birthdays_and_anniversaries(now_pt: datetime, days: int = 30) -> list[tuple[date, str]]:
    """Dynamically pull birthdays and anniversaries from contacts CSVs and Google Calendar."""
    if now_pt.weekday() == 6:
        start_date = (now_pt + timedelta(days=1)).date()
    else:
        start_date = now_pt.date()
    end_date = start_date + timedelta(days=days)

    items: list[tuple[date, str]] = []
    seen_keys: set[tuple[date, str]] = set()

    # 1. Google Contacts Birthdays
    contacts_file = Path("/workspace/data/google_contacts_birthdays.csv")
    if contacts_file.exists():
        try:
            with open(contacts_file, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = row.get("Name", "").strip()
                    bday = row.get("Birthday", "").strip()
                    if not bday or not name:
                        continue
                    if bday.startswith("--"):
                        md = bday[2:]
                    elif len(bday) == 10:
                        md = bday[5:]
                    elif len(bday) == 5:
                        md = bday
                    else:
                        continue
                    try:
                        m, d = map(int, md.split("-"))
                        target_date = date(now_pt.year, m, d)
                        if not (start_date <= target_date <= end_date):
                            target_date = date(now_pt.year + 1, m, d)
                        if start_date <= target_date <= end_date:
                            key = (target_date, name.lower())
                            if key not in seen_keys:
                                seen_keys.add(key)
                                fmt_date = target_date.strftime("%b %d")
                                items.append((target_date, f"• **{fmt_date}** — {name}'s Birthday"))
                    except Exception:
                        pass
        except Exception as e:
            print(f"[WeeklyDigest] Error reading google_contacts_birthdays.csv: {e}", file=sys.stderr)

    # 2. Friends and Family Master CSV (including child birthdays in notes)
    master_file = Path("/workspace/data/friends_and_family_master.csv")
    if master_file.exists():
        try:
            with open(master_file, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = row.get("Name", "").strip()
                    bday = row.get("Birthday", "").strip()
                    notes = row.get("Notes & Connections", "").strip()
                    if bday and len(bday) == 5 and "-" in bday:
                        try:
                            m, d = map(int, bday.split("-"))
                            target_date = date(now_pt.year, m, d)
                            if not (start_date <= target_date <= end_date):
                                target_date = date(now_pt.year + 1, m, d)
                            if start_date <= target_date <= end_date:
                                key = (target_date, name.lower())
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    fmt_date = target_date.strftime("%b %d")
                                    items.append((target_date, f"• **{fmt_date}** — {name}'s Birthday"))
                        except Exception:
                            pass
                    for child_name, child_bday in re.findall(r"(\b[A-Za-z\s]+?)\s*\((\d{2}-\d{2})\)", notes):
                        try:
                            m, d = map(int, child_bday.split("-"))
                            target_date = date(now_pt.year, m, d)
                            if not (start_date <= target_date <= end_date):
                                target_date = date(now_pt.year + 1, m, d)
                            if start_date <= target_date <= end_date:
                                c_clean = re.sub(r"^(child|son|daughter|kid)\s+", "", child_name.strip(), flags=re.I)
                                key = (target_date, c_clean.lower())
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    fmt_date = target_date.strftime("%b %d")
                                    items.append((target_date, f"• **{fmt_date}** — {c_clean}'s Birthday ({name}'s family)"))
                        except Exception:
                            pass
        except Exception as e:
            print(f"[WeeklyDigest] Error reading friends_and_family_master.csv: {e}", file=sys.stderr)

    # 3. Google Calendar sweep for anniversaries and gotcha days across all calendars
    try:
        t_min = now_pt.isoformat()
        t_max = (now_pt + timedelta(days=days)).isoformat()
        res = json.loads(calendar_list_events("all", t_min, t_max, max_results=50))
        for ev in res.get("events", []):
            summ = ev.get("summary", "").strip()
            if not summ:
                continue
            s_lower = summ.lower()
            if any(k in s_lower for k in ["anniversary", "gotcha day", "wedding"]):
                start_str = ev.get("start", "")
                if "T" in start_str:
                    ev_date = datetime.fromisoformat(start_str).astimezone(PT).date()
                else:
                    ev_date = datetime.strptime(start_str, "%Y-%m-%d").date()
                if start_date <= ev_date <= end_date:
                    key = (ev_date, summ.lower())
                    if key not in seen_keys:
                        seen_keys.add(key)
                        fmt_date = ev_date.strftime("%b %d")
                        items.append((ev_date, f"• **{fmt_date}** — {summ}"))
    except Exception as e:
        print(f"[WeeklyDigest] Error scanning calendar anniversaries: {e}", file=sys.stderr)

    return items

def get_upcoming_reminders_and_renewals(now_pt: datetime, days: int = 30) -> list[str]:
    """Dynamically pull reminders, birthdays, and anniversaries due in the next 30 days."""
    combined_items: list[tuple[date, str]] = []
    reminders_file = Path("/workspace/data/reminders.json")
    if not reminders_file.exists():
        reminders_file = Path(__file__).resolve().parent.parent / "data" / "reminders.json"
    if now_pt.weekday() == 6:
        start_date = (now_pt + timedelta(days=1)).date()
    else:
        start_date = now_pt.date()
    end_date = start_date + timedelta(days=days)
    today_str = start_date.strftime("%Y-%m-%d")
    limit_str = end_date.strftime("%Y-%m-%d")

    # 1. Operational deadlines and homelab reminders
    if reminders_file.exists():
        try:
            with open(reminders_file, "r", encoding="utf-8") as f:
                rems = json.load(f)
            due_rems = [r for r in rems if today_str <= r.get("due", "") <= limit_str]
            due_rems.sort(key=lambda r: r.get("due", ""))
            seen_msgs: set[str] = set()
            for r in due_rems:
                msg = r.get("message", "")
                due_date = r.get("due", "")
                try:
                    dt = datetime.strptime(due_date, "%Y-%m-%d").date()
                    fmt_date = dt.strftime("%b %d")
                except Exception:
                    dt = start_date
                    fmt_date = due_date

                # Suppress financial bills and payment reminders
                if re.search(r"\b(bill|bills|payment|payments|autopay|auto-pay|lease|invoice|statement)\b", msg, re.I):
                    continue

                # Clean dynamic text extraction
                clean_msg = re.sub(r"[#*_`~]", "", msg).split("\n")[0].strip()
                clean_msg = re.sub(r"^[^\w\s]+", "", clean_msg).strip()
                if not clean_msg:
                    continue

                # Deduplicate identical recurring reminders
                dedup_key = re.sub(r"\s+", " ", clean_msg.lower())
                if dedup_key in seen_msgs:
                    continue
                seen_msgs.add(dedup_key)

                combined_items.append((dt, f"• **{fmt_date}** — {clean_msg[:55].strip()}"))
        except Exception as e:
            print(f"[WeeklyDigest] Error reading reminders.json: {e}", file=sys.stderr)

    # 2. Birthdays and Anniversaries
    bday_items = get_upcoming_birthdays_and_anniversaries(now_pt, days=days)
    combined_items.extend(bday_items)

    # Sort all items chronologically
    combined_items.sort(key=lambda x: x[0])

    return [item[1] for item in combined_items[:10]]



def get_recent_cashflow_rows() -> list[str]:
    """Query recent statements from Actual Budget recurring transactions or Gmail fallback."""
    rows = [
        "Item           Amount   Trend ",
        "------------------------------",
    ]
    detected_items = {}

    # 1. Query Actual Budget recurring transactions dynamically before Gmail fallback
    try:
        from actual import Actual
        from actual.queries import get_schedules, get_payees
        try:
            import nas_config
        except ImportError:
            from tools import nas_config

        actual_url = os.environ.get("ACTUAL_SERVER_URL", f"http://{nas_config.HOST_2_IP}:5006")
        actual_password = os.environ.get("ACTUAL_PASSWORD", "zerobudget")
        budget_file = os.environ.get("ACTUAL_BUDGET_FILE", "My Finances")

        with Actual(base_url=actual_url, password=actual_password, file=budget_file, timeout=10.0) as actual:
            session = actual.session
            payees = {}
            try:
                payees = {p.id: p.name for p in get_payees(session)}
            except Exception:
                pass

            schedules = get_schedules(session)
            for sched in schedules:
                name = getattr(sched, "name", None)
                amt = getattr(sched, "amount", None)
                rule = getattr(sched, "rule", None)
                if rule and hasattr(rule, "conditions"):
                    for cond in rule.conditions:
                        f = getattr(cond, "field", None)
                        val = getattr(cond, "value", None)
                        if f == "description" and not name and val in payees:
                            name = payees[val]
                        elif f == "amount" and amt is None:
                            if hasattr(val, "num1"):
                                amt = abs(val.num1) / 100.0
                            elif isinstance(val, (int, float)):
                                amt = abs(val) / 100.0 if abs(val) >= 100 else abs(val)

                if name and amt is not None:
                    try:
                        amt_f = float(amt)
                        if isinstance(amt, int) and abs(amt) >= 100:
                            amt_f = abs(amt) / 100.0
                        else:
                            amt_f = abs(amt_f)
                        detected_items[name] = (f"{amt_f:.2f}", "flat ")
                    except Exception:
                        pass
    except Exception as e:
        print(f"[WeeklyDigest] Warning querying Actual Budget recurring transactions: {e}", file=sys.stderr)

    # 2. Gmail search fallback if Actual Budget did not yield items
    if not detected_items:
        try:
            res = json.loads(gmail_search('subject:(bill OR statement OR payment OR invoice) newer_than:35d', 10))
            for m in res.get("messages", []):
                subj = m.get("subject", "").lower()
                snip = m.get("snippet", "")
                amt_match = re.search(r"\$(\d{1,4}(?:\.\d{2})?)", snip)
                if "famly" in m.get("from", "").lower() or "preschool" in snip.lower() or "sacc" in snip.lower():
                    amt = amt_match.group(1) if amt_match else "779.00"
                    detected_items["Preschool"] = (amt, "flat ")
                elif "flagstar" in m.get("from", "").lower() or "mortgage" in subj:
                    detected_items["Mortgage P&I"] = ("8360.00", "flat ")
                elif "pge" in m.get("from", "").lower() or "gas and electric" in snip.lower():
                    amt = amt_match.group(1) if amt_match else "280.00"
                    detected_items["PG&E Net"] = (amt, "solar")
                elif "att" in m.get("from", "").lower() or "fiber" in snip.lower():
                    amt = amt_match.group(1) if amt_match else "90.36"
                    detected_items["AT&T Fiber"] = (amt, "flat ")
        except Exception as e:
            print(f"[WeeklyDigest] Warning querying gmail statements: {e}", file=sys.stderr)

    baseline = [
        ("Kia EV9 Lease", "749.27", "flat "),
        ("AT&T Fiber", "90.36", "flat "),
        ("Preschool (PCC)", "779.00", "flat "),
        ("Water/Trash", "145.20", "flat "),
    ]
    for name, amt, trend in baseline:
        if name not in detected_items and not any(k in name for k in detected_items):
            detected_items[name] = (amt, trend)

    for name, (amt, trend) in list(detected_items.items())[:5]:
        amt_float = float(amt.replace(",", ""))
        rows.append(f"{name[:14]:<14} {amt_float:>7.2f}    {trend:<6}")
    return rows


FILTER_PATTERNS = [
    re.compile(r"\b(rosie|isaac)\s+(dropoff|pickup)\b", re.I),
    re.compile(r"\biready\s+begins\b", re.I),
    re.compile(r"\b(rafaela('?s)?\s+class(es)?|isaac\s*[-–/]\s*rafaela)\b", re.I),
]


def get_week_ahead_events(now_pt: datetime | None = None) -> list[str]:
    """Query Google Calendar for the upcoming week, filtering out routine school dropoff/pickup noise."""
    if now_pt is None:
        now_pt = datetime.now(PT)

    # If running on Sunday, look ahead to the upcoming week (Monday through Sunday).
    # Otherwise, look ahead 7 days starting from today.
    if now_pt.weekday() == 6:  # Sunday
        start_dt = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_dt = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)

    end_dt = (start_dt + timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    min_iso = start_dt.astimezone(timezone.utc).isoformat()
    max_iso = end_dt.astimezone(timezone.utc).isoformat()

    lines = []
    try:
        res_raw = calendar_list_events("all", min_iso, max_iso, max_results=50)
        data = json.loads(res_raw)
        events = data.get("events", [])

        for ev in events:
            cal_name = ev.get("calendar", "")
            if cal_name and any(ex in cal_name.lower() for ex in ("roy cloud", "emily")):
                continue

            summary = ev.get("summary", "").strip()
            if not summary or any(p.search(summary) for p in FILTER_PATTERNS):
                continue
            start_str = ev.get("start", "")
            end_str = ev.get("end", "")
            if not start_str:
                continue

            if "T" in start_str:
                try:
                    s_dt = datetime.fromisoformat(start_str).astimezone(PT)
                    date_badge = s_dt.strftime("%a %b %d")
                    s_time = s_dt.strftime("%-I:%M %p")
                    if end_str and "T" in end_str:
                        e_dt = datetime.fromisoformat(end_str).astimezone(PT)
                        e_time = e_dt.strftime("%-I:%M %p")
                        time_part = f" @ {s_time} – {e_time}"
                    else:
                        time_part = f" @ {s_time}"
                except Exception:
                    date_badge = start_str[:10]
                    time_part = ""
            else:
                try:
                    d = datetime.strptime(start_str, "%Y-%m-%d")
                    date_badge = d.strftime("%a %b %d")
                except Exception:
                    date_badge = start_str
                time_part = ""

            lines.append(f"• **{date_badge}**{time_part} — {summary}")
    except Exception as e:
        print(f"[WeeklyDigest] Error querying calendar: {e}", file=sys.stderr)
        lines.append("• *Calendar events temporarily unavailable.*")

    return lines[:15]


def generate_weekly_digest(now_pt: datetime | None = None) -> str:
    if now_pt is None:
        now_pt = datetime.now(PT)
    date_header = now_pt.strftime("%A, %B %d, %Y")

    # 1. 7-Day Week Ahead Schedule Radar
    week_events = get_week_ahead_events(now_pt)
    week_block = "\n".join(week_events) if week_events else "• *No upcoming events scheduled for this week.*"

    # 2. Upcoming Deadlines & 30-Day Lookahead
    renewals_lines = get_upcoming_reminders_and_renewals(now_pt, days=30)
    renewals_block = "\n".join(renewals_lines) if renewals_lines else "• *No upcoming deadlines or renewals.*"

    # 3. Personal Data & Gaming Profile Sync Receipt
    sync_block = ""
    status_file = Path("/workspace/data/sidecar_status.json")
    if not status_file.exists():
        status_file = Path(__file__).resolve().parent.parent / "data" / "sidecar_status.json"
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                statuses = json.load(f)
            p_sync = statuses.get("personal_data_sync", {})
            p_summary = p_sync.get("summary") or p_sync.get("extra", {}).get("summary")
            if p_summary:
                compact_summary = p_summary.strip().split("\n")[0]
                sync_block = f"\n\n🎮 **Weekly Profile Sync:** {compact_summary}"
        except Exception as e:
            print(f"[WeeklyDigest] Error reading sidecar_status.json: {e}", file=sys.stderr)

    report = f"""📊 **Weekly Household Radar**
_{date_header}_

🗓️ **7-Day Week Ahead:**
{week_block}

📅 **30-Day Lookahead & Deadlines:**
{renewals_block}{sync_block}"""
    return report


if __name__ == "__main__":
    print(generate_weekly_digest())
