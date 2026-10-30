#!/usr/bin/env python3
"""Workday Proactive Decision Cards Engine.

Wires event-driven workday Decision Card workflows:
1. Morning/Commute Inbox & RCS Action Triage Card
2. 48h Family Calendar Radar Card
3. Afternoon Dinner Pivot Card

Adheres strictly to FORGE maker-checker discipline and event-driven silence:
When zero actionable items or anomalies are detected, returns (True, '', {'cards_staged': 0})
to avoid posting nominal noise to Discord channels.
"""

from __future__ import annotations

import argparse
from datetime import datetime, time as dt_time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Optional
from zoneinfo import ZoneInfo

# Ensure tools and workspace are on sys.path
_CURRENT_DIR = Path(__file__).resolve().parent
_WORKSPACE_DIR = _CURRENT_DIR.parent
for _p in [str(_WORKSPACE_DIR), str(_CURRENT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from tools.forge import FORGE_DB_PATH, stage_decision
except ImportError:
    try:
        from forge import FORGE_DB_PATH, stage_decision
    except ImportError:
        FORGE_DB_PATH = Path(os.environ.get("FORGE_DB_PATH", "/workspace/data/forge.db"))

        def stage_decision(*args: Any, **kwargs: Any) -> dict:  # type: ignore
            raise RuntimeError("tools.forge.stage_decision is unavailable.")

try:
    from tools.mealie_tool import api_request as mealie_api_request
except ImportError:
    try:
        from mealie_tool import api_request as mealie_api_request
    except ImportError:
        mealie_api_request = None  # type: ignore

try:
    from tools.workspace_mcp import calendar_list_events, gmail_search
except ImportError:
    try:
        from workspace_mcp import calendar_list_events, gmail_search
    except ImportError:
        calendar_list_events = None  # type: ignore
        gmail_search = None  # type: ignore

# Target Discord Channels
CHANNEL_ZERO_CHAT = 1542081375287640084
CHANNEL_ZERO_OPS = 1544953279664889888
CHANNEL_BROCK_HOUSE = 1550577908811178095
CHANNEL_SHOPPING = 1544955538033348618
CHANNEL_FINANCES = 1544955532765560924
CHANNEL_HOME_ASSISTANT = 1544953275877556334
CHANNEL_PROJECTS = 1548196930788524094

PT = ZoneInfo("America/Los_Angeles")

# Actionable Comms Keyword Patterns
ACTIONABLE_COMMS_PATTERNS: dict[str, list[str]] = {
    "school_daycare": [
        r"\bschool\b",
        r"\bdaycare\b",
        r"\bpreschool\b",
        r"\bkindergarten\b",
        r"\bteacher\b",
        r"\bdismissal\b",
        r"\bfield trip\b",
        r"\btuition\b",
        r"\bclassroom\b",
        r"\bpta\b",
        r"\bearly release\b",
        r"\bdrop-?off\b",
        r"\bpick-?up\b",
    ],
    "medical": [
        r"\bdoctor\b",
        r"\bpediatrician\b",
        r"\bdentist\b",
        r"\bappointment\b",
        r"\bprescription\b",
        r"\blab results?\b",
        r"\bimmunization\b",
        r"\bvaccin",
        r"\bmychart\b",
        r"\bclinic\b",
        r"\bpatient\b",
        r"\bphysician\b",
        r"\burgent care\b",
        r"\bpharmacy\b",
        r"\brx\b",
    ],
    "contractor": [
        r"\bcontractor\b",
        r"\bhvac\b",
        r"\bplumber\b",
        r"\belectrician\b",
        r"\bestimate\b",
        r"\bquote\b",
        r"\brepair\b",
        r"\bservice call\b",
        r"\btechnician\b",
        r"\broofing\b",
        r"\bhandyman\b",
        r"\bappliance repair\b",
    ],
    "package_exception": [
        r"\bdelivery exception\b",
        r"\bpackage delay\b",
        r"\bshipment delay\b",
        r"\brescheduled\b",
        r"\bdelivery attempt\b",
        r"\baction required\b",
        r"\bheld at\b",
        r"\bdelivery issue\b",
        r"\bfailed delivery\b",
        r"\bincorrect address\b",
        r"\bdelayed delivery\b",
        r"\bpackage\b.*?\bdelay",
        r"\bshipment\b.*?\bdelay",
    ],
    "rsvp": [
        r"\brsvp\b",
        r"\binvitation\b",
        r"\bbirthday party\b",
        r"\bpotluck\b",
        r"\bconfirm attendance\b",
        r"\bplease respond\b",
        r"\bare you coming\b",
        r"\bwill you attend\b",
    ],
}

CATEGORY_DISPLAY_NAMES = {
    "school_daycare": "School/Daycare",
    "medical": "Medical",
    "contractor": "Contractor",
    "package_exception": "Package Exception",
    "rsvp": "RSVP",
}

CALENDAR_PREP_KEYWORDS = [
    "birthday",
    "party",
    "trip",
    "doctor",
    "camp",
    "flight",
    "dentist",
    "pediatrician",
    "celebration",
]


def _get_triage_seen_path() -> Path:
    """Resolve the persistence path for triage_card_seen.json."""
    custom_path = os.environ.get("TRIAGE_SEEN_FILE_PATH")
    if custom_path:
        return Path(custom_path)
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return Path(data_dir) / "triage_card_seen.json"
    candidate = Path("/workspace/data/triage_card_seen.json")
    if candidate.parent.exists() and os.access(str(candidate.parent), os.W_OK):
        return candidate
    local_data = _WORKSPACE_DIR / "data"
    local_data.mkdir(parents=True, exist_ok=True)
    return local_data / "triage_card_seen.json"


def _load_seen_triage_ids(seen_path: Optional[Path] = None) -> set[str]:
    """Load seen message IDs from triage_card_seen.json."""
    target_path = seen_path or _get_triage_seen_path()
    if not target_path.exists():
        return set()
    try:
        content = target_path.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, list):
            return {str(x) for x in data}
        if isinstance(data, dict):
            return {str(x) for x in data.get("seen_ids", [])}
    except Exception:
        pass
    return set()


def _save_seen_triage_ids(seen_ids: set[str], seen_path: Optional[Path] = None) -> None:
    """Persist seen message IDs to triage_card_seen.json."""
    target_path = seen_path or _get_triage_seen_path()
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(PT).isoformat(),
            "seen_ids": sorted(list(seen_ids)),
        }
        target_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


def _classify_comms_item(item: dict[str, Any]) -> Optional[str]:
    """Classify a communication item into one of the 5 actionable categories."""
    raw_cat = str(item.get("category", "")).strip().lower().replace(" ", "_").replace("/", "_")
    if raw_cat:
        for standard_cat in ACTIONABLE_COMMS_PATTERNS:
            if raw_cat == standard_cat or raw_cat in standard_cat:
                return standard_cat

    # Regex search across subject, snippet, body, and sender
    searchable_text = (
        f"{item.get('subject', '')} {item.get('snippet', '')} "
        f"{item.get('body', '')} {item.get('text', '')} {item.get('sender', '')}"
    ).lower()

    for cat, patterns in ACTIONABLE_COMMS_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, searchable_text, re.IGNORECASE):
                return cat
    return None


def _parse_recipe_minutes(raw_time: Any) -> int:
    """Parse recipe duration into integer minutes from ISO 8601, string, or numeric."""
    if raw_time is None:
        return 0
    if isinstance(raw_time, (int, float)):
        return int(raw_time)

    time_str = str(raw_time).strip()
    if not time_str:
        return 0

    if time_str.isdigit():
        return int(time_str)

    # Check ISO 8601 duration (e.g. PT1H30M, PT45M, PT2H)
    iso_match = re.search(r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?", time_str, re.IGNORECASE)
    if iso_match:
        hrs = int(iso_match.group("hours") or 0)
        mins = int(iso_match.group("minutes") or 0)
        if hrs or mins:
            return hrs * 60 + mins

    # Check plain text duration (e.g. '1 hr 15 mins', '45 min', '2 hours')
    total = 0
    h_match = re.search(r"(\d+)\s*(?:h|hr|hour)s?", time_str, re.IGNORECASE)
    m_match = re.search(r"(\d+)\s*(?:m|min|minute)s?", time_str, re.IGNORECASE)
    if h_match:
        total += int(h_match.group(1)) * 60
    if m_match:
        total += int(m_match.group(1))
    if total > 0:
        return total

    # Direct number extraction fallback
    num_match = re.search(r"^(\d+)", time_str)
    if num_match:
        return int(num_match.group(1))

    return 0


def _parse_event_datetime(raw_dt: Any, default_time: dt_time = dt_time(0, 0)) -> tuple[datetime, bool]:
    """Parse calendar start/end date into timezone-aware datetime in PT and all-day flag."""
    if isinstance(raw_dt, datetime):
        if raw_dt.tzinfo is None:
            return raw_dt.replace(tzinfo=PT), False
        return raw_dt.astimezone(PT), False

    dt_str = str(raw_dt).strip()
    # Check YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", dt_str):
        d = datetime.strptime(dt_str, "%Y-%m-%d").date()
        return datetime.combine(d, default_time, tzinfo=PT), True

    # Check ISO 8601
    try:
        clean_str = dt_str.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(clean_str)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=PT), False
        return parsed.astimezone(PT), False
    except Exception:
        # Fallback to now
        return datetime.now(PT), False


# ==============================================================================
# Workflow 1: Morning/Commute Inbox & RCS Action Triage Card
# ==============================================================================
def run_inbox_rcs_action_triage_card(
    slot: str = "morning",
    dry_run: bool = False,
    test: bool = False,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    items_override: Optional[list[dict[str, Any]]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Scan unread actionable Gmail/RCS items and stage a Decision Card in #zero-chat.

    Actionable categories: school/daycare, medical, contractor texts, package exceptions, RSVPs.
    Deduplicates via /workspace/data/triage_card_seen.json.
    Strictly event-driven: returns (True, '', {'cards_staged': 0}) when 0 items detected.
    """
    seen_ids = _load_seen_triage_ids()

    # 1. Gather raw candidates
    candidates: list[dict[str, Any]] = []
    if items_override is not None:
        candidates = list(items_override)
    elif not test:
        # In live run, query Gmail if workspace_mcp is available
        if gmail_search is not None:
            try:
                raw_gmail = gmail_search("is:unread", max_results=25)
                data = json.loads(raw_gmail) if isinstance(raw_gmail, str) else raw_gmail
                for msg in data.get("messages", []):
                    candidates.append({
                        "id": msg.get("id"),
                        "source": "gmail",
                        "sender": msg.get("from", ""),
                        "subject": msg.get("subject", ""),
                        "snippet": msg.get("snippet", ""),
                    })
            except Exception:
                pass

    # 2. Filter & classify actionable items
    actionable_items: list[dict[str, Any]] = []
    new_seen_ids = set(seen_ids)

    for item in candidates:
        raw_id = item.get("id") or item.get("message_id")
        if not raw_id:
            h = hashlib.sha256(
                f"{item.get('source')}:{item.get('sender')}:{item.get('subject')}".encode("utf-8")
            ).hexdigest()[:16]
            raw_id = f"gen-{h}"
        item_id = str(raw_id)

        if item_id in seen_ids:
            continue

        category = _classify_comms_item(item)
        if category:
            item_copy = dict(item)
            item_copy["id"] = item_id
            item_copy["category"] = category
            item_copy["category_display"] = CATEGORY_DISPLAY_NAMES.get(category, category.title())
            actionable_items.append(item_copy)
            new_seen_ids.add(item_id)

    # 3. Silent early exit if 0 actionable items
    if not actionable_items:
        return True, "", {"cards_staged": 0, "detected_items": 0, "slot": slot}

    # 4. Format Decision Card
    count = len(actionable_items)
    slot_title = slot.capitalize()
    title = f"📬 {slot_title} Comms Action Triage ({count} item{'s' if count != 1 else ''})"

    summary_lines = [
        f"### 📬 {slot_title} Actionable Comms Triage ({count})",
        "Actionable school, medical, contractor, package, or RSVP messages requiring review:",
        "",
    ]
    for it in actionable_items[:10]:
        cat_disp = it.get("category_display", "Action")
        sender = it.get("sender") or it.get("from") or "Unknown"
        subj = it.get("subject") or it.get("snippet") or it.get("text") or "(no subject)"
        summary_lines.append(f"• **[{cat_disp}]** `{sender}`: {subj[:80]}")
    if count > 10:
        summary_lines.append(f"• *... and {count - 10} additional items*")

    summary_text = "\n".join(summary_lines)

    # 5. Dry-run early exit
    if dry_run:
        return True, summary_text, {
            "cards_staged": 0,
            "dry_run": True,
            "detected_items": count,
            "slot": slot,
        }

    # 6. Stage Decision Card in #zero-chat
    # Single-line shell payload passing _validate_decision_shell_payload
    payload = {
        "command": f"echo 'Triage acknowledged for {count} {slot} items'",
        "detected_ids": [it["id"] for it in actionable_items],
        "slot": slot,
    }

    should_post = post_to_discord and not test
    stage_res = stage_decision(
        category="comms",
        title=title,
        summary=summary_text,
        handler_type="shell",
        payload=payload,
        channel_id=CHANNEL_ZERO_CHAT,
        db_path=db_path,
        post_to_discord=should_post,
    )

    # Persist updated seen IDs
    _save_seen_triage_ids(new_seen_ids)

    return True, summary_text, {
        "cards_staged": 1,
        "decision_id": stage_res.get("decision_id"),
        "detected_items": count,
        "slot": slot,
    }


# ==============================================================================
# Workflow 2: 48h Family Calendar Radar Card
# ==============================================================================
def run_family_calendar_radar_card(
    dry_run: bool = False,
    test: bool = False,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    events_override: Optional[list[dict[str, Any]]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Scan next 48h of family calendar events for overlaps, tight buffers (<15m), and prep items.

    Stages a Decision Card in #brock-house (1550577908811178095).
    Strictly event-driven: returns (True, '', {'cards_staged': 0}) when 0 conflicts/prep alerts.
    """
    now_pt = datetime.now(PT)
    window_end_pt = now_pt + timedelta(hours=48)

    # 1. Fetch events
    raw_events: list[dict[str, Any]] = []
    if events_override is not None:
        raw_events = list(events_override)
    elif not test:
        if calendar_list_events is not None:
            try:
                res = calendar_list_events(
                    calendar_id="all",
                    time_min_iso=now_pt.isoformat(),
                    time_max_iso=window_end_pt.isoformat(),
                    max_results=50,
                )
                data = json.loads(res) if isinstance(res, str) else res
                raw_events = data.get("events", [])
            except Exception:
                pass

    # 2. Parse & normalize events within 48h window
    normalized_events: list[dict[str, Any]] = []
    for ev in raw_events:
        summary = str(ev.get("summary", "(Untitled)")).strip()
        start_val = ev.get("start")
        end_val = ev.get("end") or start_val
        if not start_val:
            continue

        start_dt, is_all_day = _parse_event_datetime(start_val, default_time=dt_time(0, 0))
        end_dt, _ = _parse_event_datetime(end_val, default_time=dt_time(23, 59, 59) if is_all_day else dt_time(0, 0))

        # Check if event touches [now_pt, window_end_pt]
        if end_dt < now_pt or start_dt > window_end_pt:
            continue

        normalized_events.append({
            "id": ev.get("id", ""),
            "summary": summary,
            "description": ev.get("description", ""),
            "location": ev.get("location", ""),
            "start_dt": start_dt,
            "end_dt": end_dt,
            "is_all_day": is_all_day,
        })

    # 3. Detect Overlaps, Tight Buffers (<15m), and Prep items
    issues: list[dict[str, Any]] = []

    # Sort timed events chronologically
    timed_events = [e for e in normalized_events if not e["is_all_day"]]
    timed_events.sort(key=lambda e: e["start_dt"])

    # A. Overlaps between timed events
    for i in range(len(timed_events)):
        ev1 = timed_events[i]
        for j in range(i + 1, len(timed_events)):
            ev2 = timed_events[j]
            if ev2["start_dt"] >= ev1["end_dt"]:
                break
            # ev2 starts before ev1 ends -> overlap
            if ev1["start_dt"] < ev2["end_dt"] and ev2["start_dt"] < ev1["end_dt"]:
                issues.append({
                    "type": "overlap",
                    "title": f"Overlap: '{ev1['summary']}' & '{ev2['summary']}'",
                    "details": (
                        f"'{ev1['summary']}' ({ev1['start_dt'].strftime('%a %I:%M%p')}-{ev1['end_dt'].strftime('%I:%M%p')}) "
                        f"overlaps with '{ev2['summary']}' ({ev2['start_dt'].strftime('%a %I:%M%p')}-{ev2['end_dt'].strftime('%I:%M%p')})"
                    ),
                    "events": [ev1["summary"], ev2["summary"]],
                })

    # B. Tight Buffers (<15m) between consecutive timed events
    for i in range(len(timed_events) - 1):
        ev1 = timed_events[i]
        ev2 = timed_events[i + 1]
        if ev1["end_dt"] <= ev2["start_dt"]:
            gap_seconds = (ev2["start_dt"] - ev1["end_dt"]).total_seconds()
            if 0 <= gap_seconds < 15 * 60:
                gap_mins = int(gap_seconds // 60)
                issues.append({
                    "type": "tight_buffer",
                    "title": f"Tight buffer: {gap_mins}m between '{ev1['summary']}' & '{ev2['summary']}'",
                    "details": (
                        f"Only {gap_mins}m buffer between '{ev1['summary']}' (ends {ev1['end_dt'].strftime('%I:%M%p')}) "
                        f"and '{ev2['summary']}' (starts {ev2['start_dt'].strftime('%I:%M%p')})"
                    ),
                    "buffer_minutes": gap_mins,
                    "events": [ev1["summary"], ev2["summary"]],
                })

    # C. Events needing prep (birthday, party, trip, doctor, camp, etc.)
    for ev in normalized_events:
        haystack = f"{ev['summary']} {ev['description']}".lower()
        matched_prep = [kw for kw in CALENDAR_PREP_KEYWORDS if kw in haystack]
        if matched_prep:
            kw = matched_prep[0]
            time_disp = (
                ev["start_dt"].strftime("%a %b %d at %I:%M %p")
                if not ev["is_all_day"]
                else ev["start_dt"].strftime("%a %b %d (All Day)")
            )
            issues.append({
                "type": "prep",
                "title": f"Prep needed ({kw}): '{ev['summary']}'",
                "details": f"'{ev['summary']}' on {time_disp} requires prep or logistics readiness.",
                "prep_keyword": kw,
                "events": [ev["summary"]],
            })

    # 4. Silent exit if 0 issues
    if not issues:
        return True, "", {"cards_staged": 0, "issues": []}

    # 5. Format Decision Card
    count = len(issues)
    title = f"🗓️ 48h Family Calendar Radar ({count} alert{'s' if count != 1 else ''})"

    summary_lines = [
        f"### 🗓️ 48h Family Calendar Radar ({count})",
        "Upcoming schedule collisions, tight buffers, and prep items requiring attention:",
        "",
    ]
    for issue in issues[:8]:
        itype = issue["type"]
        if itype == "overlap":
            summary_lines.append(f"• ⚠️ **Overlap:** {issue['details']}")
        elif itype == "tight_buffer":
            summary_lines.append(f"• ⏳ **Tight Buffer:** {issue['details']}")
        elif itype == "prep":
            summary_lines.append(f"• 🎒 **Prep Needed:** {issue['details']}")
    if count > 8:
        summary_lines.append(f"• *... and {count - 8} additional alerts*")

    summary_text = "\n".join(summary_lines)

    # 6. Dry run early exit
    if dry_run:
        return True, summary_text, {
            "cards_staged": 0,
            "dry_run": True,
            "issues": issues,
        }

    # 7. Stage Decision Card in #brock-house
    payload = {
        "command": f"echo 'Calendar radar acknowledged: {count} alerts'",
        "issue_count": count,
    }

    should_post = post_to_discord and not test
    stage_res = stage_decision(
        category="calendar",
        title=title,
        summary=summary_text,
        handler_type="shell",
        payload=payload,
        channel_id=CHANNEL_BROCK_HOUSE,
        db_path=db_path,
        post_to_discord=should_post,
    )

    return True, summary_text, {
        "cards_staged": 1,
        "decision_id": stage_res.get("decision_id"),
        "issues": issues,
    }


# ==============================================================================
# Workflow 3: Afternoon Dinner Pivot Card
# ==============================================================================
def run_dinner_pivot_card(
    dry_run: bool = False,
    test: bool = False,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    plan_override: Optional[dict[str, Any]] = None,
    events_override: Optional[list[dict[str, Any]]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Check tonight's Mealie dinner against evening calendar density (16:30-20:00 PT).

    Stages a swap/push Decision Card in #shopping (1544955538033348618) if:
      - Tonight's dinner is unscheduled in Mealie, OR
      - >=2 evening events collide with a >30m recipe.

    Strictly event-driven: returns (True, '', {'cards_staged': 0}) when nominal.
    """
    now_pt = datetime.now(PT)
    today = now_pt.date()
    window_start = datetime.combine(today, dt_time(16, 30), tzinfo=PT)
    window_end = datetime.combine(today, dt_time(20, 0), tzinfo=PT)

    # 1. Resolve Evening Calendar Density (16:30 - 20:00 PT)
    evening_events: list[dict[str, Any]] = []
    if events_override is not None:
        raw_events = list(events_override)
    elif not test and calendar_list_events is not None:
        try:
            res = calendar_list_events(
                calendar_id="all",
                time_min_iso=window_start.isoformat(),
                time_max_iso=window_end.isoformat(),
                max_results=20,
            )
            data = json.loads(res) if isinstance(res, str) else res
            raw_events = data.get("events", [])
        except Exception:
            raw_events = []
    else:
        raw_events = []

    for ev in raw_events:
        start_val = ev.get("start")
        end_val = ev.get("end") or start_val
        if not start_val:
            continue
        start_dt, is_all_day = _parse_event_datetime(start_val, default_time=dt_time(16, 30))
        end_dt, _ = _parse_event_datetime(end_val, default_time=dt_time(20, 0))

        # Check collision with 16:30 - 20:00 PT window
        if start_dt < window_end and end_dt > window_start and not is_all_day:
            evening_events.append({
                "summary": ev.get("summary", "(Event)"),
                "start_dt": start_dt,
                "end_dt": end_dt,
            })

    # 2. Resolve Tonight's Dinner Plan
    is_scheduled = False
    recipe_name = ""
    recipe_minutes = 0

    if plan_override is not None:
        if (
            plan_override.get("unscheduled") is True
            or plan_override.get("scheduled") is False
            or (not plan_override.get("recipe") and not plan_override.get("name"))
        ):
            is_scheduled = False
        else:
            rec = plan_override.get("recipe") if isinstance(plan_override.get("recipe"), dict) else plan_override
            recipe_name = rec.get("name", "Tonight's Dinner")
            recipe_minutes = _parse_recipe_minutes(
                rec.get("total_time") or rec.get("cook_time") or rec.get("prep_time") or 0
            )
            is_scheduled = True
    elif not test:
        # Check local active meal plan file
        plan_file = _WORKSPACE_DIR / "data" / "active_meal_plan.json"
        if plan_file.exists():
            try:
                data = json.loads(plan_file.read_text(encoding="utf-8"))
                meals = data.get("meals", {})
                # Look for matching day
                today_str = today.isoformat()
                weekday_name = today.strftime("%A").lower()
                for slot_key, slot_val in meals.items():
                    if slot_key.lower() == weekday_name or slot_val.get("date") == today_str:
                        rec = slot_val.get("recipe")
                        if rec and rec.get("name"):
                            recipe_name = rec.get("name")
                            recipe_minutes = _parse_recipe_minutes(
                                rec.get("total_time") or rec.get("cook_time") or 0
                            )
                            is_scheduled = True
                            break
            except Exception:
                pass

        # If still unscheduled, query Mealie API if available
        if not is_scheduled and mealie_api_request is not None:
            try:
                today_iso = today.isoformat()
                mp = mealie_api_request(f"/api/households/mealplans?start_date={today_iso}&end_date={today_iso}")
                if isinstance(mp, dict) and mp.get("items"):
                    item = mp["items"][0]
                    rec = item.get("recipe") or {}
                    if rec.get("name"):
                        recipe_name = rec.get("name")
                        recipe_minutes = _parse_recipe_minutes(rec.get("total_time") or 0)
                        is_scheduled = True
            except Exception:
                pass

    # 3. Evaluate Decision Card Trigger
    trigger = False
    trigger_reason = ""

    if not is_scheduled:
        trigger = True
        trigger_reason = "Tonight's dinner is currently unscheduled in Mealie."
    elif len(evening_events) >= 2 and recipe_minutes > 30:
        trigger = True
        ev_names = ", ".join(f"'{e['summary']}'" for e in evening_events)
        trigger_reason = (
            f"High evening calendar density: {len(evening_events)} events collide with the dinner window "
            f"(16:30-20:00 PT: {ev_names}), while tonight's planned meal ('{recipe_name}') takes {recipe_minutes} mins (>30m)."
        )

    # 4. Silent exit if nominal
    if not trigger:
        return True, "", {
            "cards_staged": 0,
            "scheduled": is_scheduled,
            "recipe_minutes": recipe_minutes,
            "evening_events": len(evening_events),
        }

    # 5. Format Decision Card
    if not is_scheduled:
        title = "🍽️ Dinner Pivot: No dinner scheduled tonight"
        summary_lines = [
            "### 🍽️ Dinner Pivot Card",
            "Tonight's dinner is currently **unscheduled** in Mealie.",
            "",
            "• **Evening Events:** "
            + (", ".join(f"'{e['summary']}'" for e in evening_events) if evening_events else "None"),
            "• **Recommendation:** Choose a quick pantry staple or stage Whole Foods / takeout delivery.",
        ]
    else:
        title = f"🍽️ Dinner Pivot: High evening density ({len(evening_events)} events) vs '{recipe_name}' ({recipe_minutes}m)"
        summary_lines = [
            "### 🍽️ Dinner Pivot Card",
            f"**Collision Detected:** {trigger_reason}",
            "",
            "• **Planned Recipe:** " + f"{recipe_name} ({recipe_minutes} minutes)",
            "• **Colliding Events:** " + ", ".join(f"'{e['summary']}'" for e in evening_events),
            "• **Recommendation:** Swap tonight's recipe with a 15-minute quick meal or push to tomorrow.",
        ]

    summary_text = "\n".join(summary_lines)

    # 6. Dry run early exit
    if dry_run:
        return True, summary_text, {
            "cards_staged": 0,
            "dry_run": True,
            "trigger_reason": trigger_reason,
            "evening_events": len(evening_events),
        }

    # 7. Stage Decision Card in #shopping
    clean_reason = re.sub(r"[^a-zA-Z0-9 _-]", "", trigger_reason)[:50].strip()
    payload = {
        "command": f"echo 'Dinner pivot acknowledged: {clean_reason}'",
        "recipe_name": recipe_name,
        "recipe_minutes": recipe_minutes,
        "evening_events_count": len(evening_events),
    }

    should_post = post_to_discord and not test
    stage_res = stage_decision(
        category="mealie",
        title=title,
        summary=summary_text,
        handler_type="shell",
        payload=payload,
        channel_id=CHANNEL_SHOPPING,
        db_path=db_path,
        post_to_discord=should_post,
    )

    return True, summary_text, {
        "cards_staged": 1,
        "decision_id": stage_res.get("decision_id"),
        "trigger_reason": trigger_reason,
        "recipe_name": recipe_name,
        "evening_events": len(evening_events),
    }


# ==============================================================================
# CLI Entrypoint
# ==============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Workday Proactive Decision Cards")
    parser.add_argument(
        "--workflow",
        choices=["triage", "calendar", "dinner", "all"],
        default="all",
        help="Which workflow to execute",
    )
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without staging cards")
    parser.add_argument("--slot", default="morning", help="Time slot for comms triage (morning/commute)")
    args = parser.parse_args()

    results: list[tuple[str, bool, str, dict[str, Any]]] = []

    if args.workflow in ("triage", "all"):
        ok, summary, meta = run_inbox_rcs_action_triage_card(slot=args.slot, dry_run=args.dry_run)
        results.append(("triage", ok, summary, meta))

    if args.workflow in ("calendar", "all"):
        ok, summary, meta = run_family_calendar_radar_card(dry_run=args.dry_run)
        results.append(("calendar", ok, summary, meta))

    if args.workflow in ("dinner", "all"):
        ok, summary, meta = run_dinner_pivot_card(dry_run=args.dry_run)
        results.append(("dinner", ok, summary, meta))

    for name, ok, summary, meta in results:
        status = "OK" if ok else "FAILED"
        staged = meta.get("cards_staged", 0)
        print(f"[{name.upper()}] {status} — Cards staged: {staged}")
        if summary:
            print(f"Summary:\n{summary}\n")


if __name__ == "__main__":
    main()
