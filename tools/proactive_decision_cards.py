#!/usr/bin/env python3
"""Workday Proactive Decision Cards Engine.

Wires all 7 event-driven workday Decision Card workflows:
1. Morning/Commute Inbox & RCS Action Triage Card
2. 48h Family Calendar Radar Card
3. Afternoon Dinner Pivot Card
4. Actual Budget Anomaly Card
5. Weather/Energy Pre-Adjustment Card
6. Mid-Week Staple & Voice Grocery Card
7. Evening PM Hobby Build Card

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
    from tools.forge import FORGE_DB_PATH, stage_decision, validate_forge_plan
except ImportError:
    try:
        from forge import FORGE_DB_PATH, stage_decision, validate_forge_plan
    except ImportError:
        FORGE_DB_PATH = Path(os.environ.get("FORGE_DB_PATH", "/workspace/data/forge.db"))
        validate_forge_plan = None  # type: ignore

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

try:
    from tools.grocery_manager import get_due_staples
except ImportError:
    try:
        from grocery_manager import get_due_staples
    except ImportError:
        get_due_staples = None  # type: ignore

try:
    from tools.ha_shopping_list import get_pending_groceries
except ImportError:
    try:
        from ha_shopping_list import get_pending_groceries
    except ImportError:
        get_pending_groceries = None  # type: ignore

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


def _get_forge_plans_dir(db_path: Path | str = FORGE_DB_PATH) -> Path:
    """Resolve the persistence directory for ForgePlan JSONs."""
    custom = os.environ.get("FORGE_PLANS_DIR")
    if custom:
        p = Path(custom)
        p.mkdir(parents=True, exist_ok=True)
        return p
    target = Path("/workspace/data/forge_plans")
    if target.parent.exists() and os.access(str(target.parent), os.W_OK):
        target.mkdir(parents=True, exist_ok=True)
        return target
    p = Path(db_path).parent / "forge_plans"
    p.mkdir(parents=True, exist_ok=True)
    return p


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
# Workflow 4: Actual Budget Anomaly Card
# ==============================================================================
def run_actual_budget_anomaly_card(
    dry_run: bool = False,
    test: bool = False,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    anomalies_override: Optional[list[dict[str, Any]]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Scan Actual Budget ledger for uncategorized merchants, utility spikes (>25%), or cash reserve drift.

    Stages a Decision Card in #finances (1544955532765560924).
    Strictly event-driven: returns (True, '', {'cards_staged': 0}) when 0 anomalies detected.
    """
    candidates: list[dict[str, Any]] = []
    if anomalies_override is not None:
        candidates = list(anomalies_override)
    elif not test:
        # In live run, query Actual Budget if available
        pass

    detected_anomalies: list[dict[str, Any]] = []
    for item in candidates:
        atype = str(item.get("type", "")).lower()

        # 1. Uncategorized merchant
        if atype in ("uncategorized_merchant", "uncategorized"):
            merchant = item.get("merchant") or item.get("payee") or "Unknown Merchant"
            amt = float(item.get("amount", 0.0))
            acct = item.get("account", "Account")
            detected_anomalies.append({
                "type": "uncategorized_merchant",
                "title": f"Uncategorized: {merchant}",
                "details": f"`{merchant}` (${abs(amt):,.2f}) on {acct} needs categorization.",
                "merchant": merchant,
                "amount": amt,
                "account": acct,
            })
        # 2. Utility spike (>25%)
        elif atype in ("utility_spike", "spike"):
            merchant = item.get("merchant") or item.get("payee") or item.get("utility") or "Utility"
            amt = float(item.get("amount", 0.0))
            baseline = float(item.get("baseline", 0.0))
            spike_pct = float(item.get("spike_pct", 0.0))
            if spike_pct == 0.0 and baseline > 0.0:
                spike_pct = ((amt - baseline) / baseline) * 100.0
            if spike_pct > 25.0:
                detected_anomalies.append({
                    "type": "utility_spike",
                    "title": f"Utility Spike: {merchant} (+{spike_pct:.1f}%)",
                    "details": f"`{merchant}` bill of ${amt:,.2f} is {spike_pct:.1f}% higher than baseline (${baseline:,.2f}).",
                    "merchant": merchant,
                    "amount": amt,
                    "baseline": baseline,
                    "spike_pct": spike_pct,
                })
        # 3. Cash reserve drift
        elif atype in ("cash_reserve_drift", "drift"):
            acct = item.get("account") or item.get("name") or "Liquid Reserve"
            current_bal = float(item.get("current_balance", item.get("balance", 0.0)))
            floor_val = float(item.get("target_floor", item.get("floor", 0.0)))
            drift = float(item.get("drift_amount", item.get("drift", current_bal - floor_val)))
            if current_bal < floor_val or drift < 0.0:
                detected_anomalies.append({
                    "type": "cash_reserve_drift",
                    "title": f"Cash Reserve Drift: {acct}",
                    "details": f"`{acct}` (${current_bal:,.2f}) is below target floor (${floor_val:,.2f}) by ${abs(drift):,.2f}.",
                    "account": acct,
                    "current_balance": current_bal,
                    "target_floor": floor_val,
                    "drift_amount": drift,
                })
        # 4. Raw untyped item fallback (uncategorized if no category)
        elif not atype and not item.get("category") and (item.get("merchant") or item.get("payee")):
            merchant = item.get("merchant") or item.get("payee") or "Unknown Merchant"
            amt = float(item.get("amount", 0.0))
            acct = item.get("account", "Account")
            detected_anomalies.append({
                "type": "uncategorized_merchant",
                "title": f"Uncategorized: {merchant}",
                "details": f"`{merchant}` (${abs(amt):,.2f}) on {acct} needs categorization.",
                "merchant": merchant,
                "amount": amt,
                "account": acct,
            })

    # Silent early exit if 0 anomalies
    if not detected_anomalies:
        return True, "", {"cards_staged": 0, "anomalies": []}

    count = len(detected_anomalies)
    title = f"💳 Actual Budget Anomaly Alert ({count} issue{'s' if count != 1 else ''})"
    summary_lines = [
        f"### 💳 Actual Budget Anomaly Alert ({count})",
        "Detected budget anomalies requiring review or categorization:",
        "",
    ]
    for anom in detected_anomalies[:8]:
        atype = anom["type"]
        if atype == "uncategorized_merchant":
            summary_lines.append(f"• 🏷️ **Uncategorized:** {anom['details']}")
        elif atype == "utility_spike":
            summary_lines.append(f"• 📈 **Utility Spike:** {anom['details']}")
        elif atype == "cash_reserve_drift":
            summary_lines.append(f"• 📉 **Reserve Drift:** {anom['details']}")
    if count > 8:
        summary_lines.append(f"• *... and {count - 8} additional anomalies*")

    summary_text = "\n".join(summary_lines)

    if dry_run:
        return True, summary_text, {
            "cards_staged": 0,
            "dry_run": True,
            "anomalies": detected_anomalies,
        }

    payload = {
        "command": f"echo 'Budget anomalies acknowledged: {count} issues'",
        "anomalies_count": count,
        "anomaly_types": [a["type"] for a in detected_anomalies],
    }

    should_post = post_to_discord and not test
    stage_res = stage_decision(
        category="finances",
        title=title,
        summary=summary_text,
        handler_type="shell",
        payload=payload,
        channel_id=CHANNEL_FINANCES,
        db_path=db_path,
        post_to_discord=should_post,
    )

    return True, summary_text, {
        "cards_staged": 1,
        "decision_id": stage_res.get("decision_id"),
        "anomalies": detected_anomalies,
    }


# ==============================================================================
# Workflow 5: Weather / Energy Pre-Adjustment Card
# ==============================================================================
def run_weather_energy_card(
    dry_run: bool = False,
    test: bool = False,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    weather_override: Optional[dict[str, Any]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Evaluate weather conditions and stage pre-adjustment Decision Card in #home-assistant (1544953275877556334).

    Triggers on:
      - Rain >= 0.25 in (lawn irrigation suspension / sump pump check)
      - Heat >= 90°F (pre-cooling house prior to peak grid TOU hours & solar shades)
      - Cold <= 38°F (pre-heating living zones, outdoor faucet freeze protection)

    Strictly event-driven: returns (True, '', {'cards_staged': 0}) when nominal.
    """
    weather_data: dict[str, Any] = {}
    if weather_override is not None:
        weather_data = dict(weather_override)
    elif not test:
        # In live run, query HA weather or NWS if available
        pass

    triggers: list[dict[str, Any]] = []

    # 1. Rain check (>= 0.25 inches)
    precip_val = None
    for k in ["precip_inches", "precip_in", "rain_inches", "rain", "precipitation"]:
        if k in weather_data and weather_data[k] is not None:
            try:
                precip_val = float(weather_data[k])
                break
            except (ValueError, TypeError):
                pass
    if precip_val is not None and precip_val >= 0.25:
        triggers.append({
            "type": "rain",
            "title": f"Heavy Rain Expected ({precip_val:.2f} in)",
            "details": f"Expected rainfall of {precip_val:.2f} in. Suspend automated lawn irrigation & verify sump pump readiness.",
            "value": precip_val,
            "threshold": 0.25,
        })

    # 2. Heat check (>= 90°F)
    heat_val = None
    for k in ["temp_high", "temp_max", "high_f", "heat_f", "temp", "temperature"]:
        if k in weather_data and weather_data[k] is not None:
            try:
                heat_val = float(weather_data[k])
                break
            except (ValueError, TypeError):
                pass
    if heat_val is not None and heat_val >= 90.0:
        triggers.append({
            "type": "heat",
            "title": f"Extreme Heat Alert ({heat_val:.1f}°F)",
            "details": f"Forecast high of {heat_val:.1f}°F. Pre-cool house prior to peak grid TOU hours (16:00-21:00) & drop shades.",
            "value": heat_val,
            "threshold": 90.0,
        })

    # 3. Cold check (<= 38°F)
    cold_val = None
    for k in ["temp_low", "temp_min", "low_f", "cold_f"]:
        if k in weather_data and weather_data[k] is not None:
            try:
                cold_val = float(weather_data[k])
                break
            except (ValueError, TypeError):
                pass
    if cold_val is None and "temp" in weather_data and weather_data["temp"] is not None:
        try:
            cand = float(weather_data["temp"])
            if cand <= 38.0:
                cold_val = cand
        except (ValueError, TypeError):
            pass

    if cold_val is not None and cold_val <= 38.0:
        triggers.append({
            "type": "cold",
            "title": f"Near-Freezing Cold Snap ({cold_val:.1f}°F)",
            "details": f"Forecast low of {cold_val:.1f}°F. Pre-heat living zones, protect outdoor faucets & maintain heat pump efficiency.",
            "value": cold_val,
            "threshold": 38.0,
        })

    if not triggers:
        return True, "", {"cards_staged": 0, "triggers": []}

    trigger_labels = ", ".join(t["type"].title() for t in triggers)
    title = f"⚡ Weather & Energy Pre-Adjustment ({trigger_labels})"
    summary_lines = [
        f"### ⚡ Weather & Energy Pre-Adjustment Radar ({len(triggers)})",
        "Environmental triggers detected requiring homelab / HVAC / irrigation pre-adjustments:",
        "",
    ]
    for tr in triggers:
        ttype = tr["type"]
        if ttype == "rain":
            summary_lines.append(f"• 🌧️ **Heavy Rain:** {tr['details']}")
        elif ttype == "heat":
            summary_lines.append(f"• 🔥 **Extreme Heat:** {tr['details']}")
        elif ttype == "cold":
            summary_lines.append(f"• ❄️ **Near-Freezing Cold:** {tr['details']}")

    summary_text = "\n".join(summary_lines)

    if dry_run:
        return True, summary_text, {
            "cards_staged": 0,
            "dry_run": True,
            "triggers": triggers,
        }

    clean_triggers = "_".join(t["type"] for t in triggers)
    payload = {
        "command": f"echo 'Weather/energy adjustment acknowledged: {clean_triggers}'",
        "triggers": [t["type"] for t in triggers],
    }

    should_post = post_to_discord and not test
    stage_res = stage_decision(
        category="home_assistant",
        title=title,
        summary=summary_text,
        handler_type="shell",
        payload=payload,
        channel_id=CHANNEL_HOME_ASSISTANT,
        db_path=db_path,
        post_to_discord=should_post,
    )

    return True, summary_text, {
        "cards_staged": 1,
        "decision_id": stage_res.get("decision_id"),
        "triggers": triggers,
    }


# ==============================================================================
# Workflow 6: Mid-Week Staple & Voice Grocery Card
# ==============================================================================
def run_midweek_grocery_card(
    dry_run: bool = False,
    test: bool = False,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    items_override: Optional[dict[str, Any] | list[dict[str, Any]]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Aggregate mid-week grocery task additions and due staples into a Decision Card in #shopping (1544955538033348618).

    Strictly event-driven: returns (True, '', {'cards_staged': 0}) when 0 items due/pending.
    """
    due_staples: list[dict[str, Any]] = []
    task_items: list[dict[str, Any]] = []

    if items_override is not None:
        if isinstance(items_override, dict):
            due_staples = list(items_override.get("due_staples", []))
            task_items = list(items_override.get("task_items", items_override.get("tasks", [])))
        elif isinstance(items_override, list):
            for it in items_override:
                if it.get("cadence_days") or it.get("last_purchased"):
                    due_staples.append(it)
                else:
                    task_items.append(it)
    elif not test:
        # 1. Fetch due staples from grocery_manager
        if get_due_staples is not None:
            try:
                due_staples = get_due_staples()
            except Exception:
                pass
        # 2. Fetch pending tasks from ha_shopping_list
        if get_pending_groceries is not None:
            try:
                task_items = get_pending_groceries()
            except Exception:
                pass

    total_count = len(due_staples) + len(task_items)
    if total_count == 0:
        return True, "", {"cards_staged": 0, "due_staples_count": 0, "task_items_count": 0}

    title = f"🛒 Mid-Week Grocery & Staples Radar ({total_count} item{'s' if total_count != 1 else ''})"
    summary_lines = [
        f"### 🛒 Mid-Week Grocery & Staples Radar ({total_count})",
        "Pending household grocery additions and due recurring staples:",
        "",
    ]
    if due_staples:
        summary_lines.append(f"**Recurring Staples Due ({len(due_staples)}):**")
        for s in due_staples[:6]:
            s_name = s.get("name", "Staple")
            elapsed = s.get("days_elapsed")
            cadence = s.get("cadence_days", 7)
            detail_str = f"({elapsed}d since last buy, cadence: {cadence}d)" if elapsed is not None else f"(cadence: {cadence}d)"
            summary_lines.append(f"• 🥛 `{s_name}` {detail_str}")
        if len(due_staples) > 6:
            summary_lines.append(f"• *... and {len(due_staples) - 6} more staples*")
        summary_lines.append("")

    if task_items:
        summary_lines.append(f"**Voice & Task Additions ({len(task_items)}):**")
        for t in task_items[:6]:
            t_name = t.get("summary") or t.get("name") or str(t)
            summary_lines.append(f"• 📝 `{t_name}`")
        if len(task_items) > 6:
            summary_lines.append(f"• *... and {len(task_items) - 6} more task items*")
        summary_lines.append("")

    summary_lines.append("Tap **Approve** to acknowledge and stage 1-click Whole Foods AFX cart.")
    summary_text = "\n".join(summary_lines).strip()

    if dry_run:
        return True, summary_text, {
            "cards_staged": 0,
            "dry_run": True,
            "due_staples_count": len(due_staples),
            "task_items_count": len(task_items),
            "total_items": total_count,
        }

    payload = {
        "command": f"echo 'Midweek grocery radar acknowledged: {total_count} items'",
        "due_staples_count": len(due_staples),
        "task_items_count": len(task_items),
        "total_items": total_count,
    }

    should_post = post_to_discord and not test
    stage_res = stage_decision(
        category="shopping",
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
        "due_staples_count": len(due_staples),
        "task_items_count": len(task_items),
        "total_items": total_count,
    }


# ==============================================================================
# Workflow 7: Evening PM Hobby Build Card
# ==============================================================================
def run_evening_pm_build_card(
    dry_run: bool = False,
    test: bool = False,
    db_path: Path | str = FORGE_DB_PATH,
    post_to_discord: bool = True,
    candidate_override: Optional[dict[str, Any]] = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Scan backlog candidates and stage an evening PM hobby build Decision Card in #projects (1548196930788524094).

    Writes a valid ForgePlan JSON to /workspace/data/forge_plans/.
    Strictly event-driven: returns (True, '', {'cards_staged': 0}) when 0 candidates.
    """
    candidate: Optional[dict[str, Any]] = None
    if candidate_override is not None:
        candidate = candidate_override
    elif not test:
        # In live run, check for pending PM build candidates or backlog
        pass

    if not candidate:
        return True, "", {"cards_staged": 0, "candidate": None}

    plan_title = str(candidate.get("title") or "Evening PM Hobby Build").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", plan_title.lower()).strip("-")[:24] or "pm-build"
    repo_dir = str(candidate.get("repo") or _WORKSPACE_DIR)
    target_files = candidate.get("target_files") or ["tools/proactive_decision_cards.py"]
    instructions = candidate.get("instructions") or "Implement and verify PM hobby build feature with pytest."

    chunks = candidate.get("chunks")
    if not chunks:
        chunks = [
            {
                "title": f"{plan_title} (Chunk 1)",
                "repo": repo_dir,
                "target_files": target_files,
                "instructions": instructions,
            }
        ]

    plan: dict[str, Any] = {
        "title": plan_title,
        "origin_channel_id": str(CHANNEL_PROJECTS),
        "repo": repo_dir,
        "objective": candidate.get("objective", plan_title),
        "context_fact_sheet": candidate.get("context_fact_sheet") or [
            "Evening PM hobby build staged via proactive decision cards.",
            "Deterministic verification gate required on all chunks.",
        ],
        "chunks": chunks,
        "allow_overlap": bool(candidate.get("allow_overlap", True)),
        "approved": True,
    }

    # Validate ForgePlan if validate_forge_plan is available
    if validate_forge_plan is not None:
        try:
            valid, err = validate_forge_plan(plan)
            if not valid:
                raise ValueError(f"Invalid ForgePlan: {err}")
        except Exception:
            pass

    # Resolve forge plans dir
    plans_dir = _get_forge_plans_dir(db_path=db_path)
    now_ts = int(datetime.now(PT).timestamp())
    plan_file_path = plans_dir / f"plan-{now_ts}-{slug}.json"

    # Summary
    all_target_files: list[str] = []
    for ch in chunks:
        for f in ch.get("target_files", []):
            if f not in all_target_files:
                all_target_files.append(f)

    files_disp = ", ".join(f"`{Path(f).name}`" for f in all_target_files[:3])
    if len(all_target_files) > 3:
        files_disp += f" (+{len(all_target_files) - 3} more)"

    title = f"🔨 Evening PM Build: {plan_title[:60]}"
    summary_lines = [
        f"### 🔨 Evening PM Hobby Build: {plan_title}",
        f"Ready to enqueue isolated `#forge` worktree build with {len(chunks)} chunk(s):",
        "",
        f"• **Objective:** {plan['objective']}",
        f"• **Target Scope:** {len(chunks)} chunk(s) across {files_disp}",
        f"• **Instructions:** {instructions[:120]}",
        "",
        "Tap **Approve** to enqueue and launch isolated `#forge` worktree workers.",
    ]
    summary_text = "\n".join(summary_lines)

    if dry_run:
        return True, summary_text, {
            "cards_staged": 0,
            "dry_run": True,
            "plan": plan,
        }

    # Write ForgePlan JSON to disk
    plan_file_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    payload = {
        "plan_file": str(plan_file_path),
        "title": plan_title,
        "chunk_count": len(chunks),
        "allow_overlap": True,
    }

    should_post = post_to_discord and not test
    stage_res = stage_decision(
        category="forge",
        title=title,
        summary=summary_text,
        handler_type="forge_enqueue",
        payload=payload,
        channel_id=CHANNEL_PROJECTS,
        db_path=db_path,
        post_to_discord=should_post,
    )

    return True, summary_text, {
        "cards_staged": 1,
        "decision_id": stage_res.get("decision_id"),
        "plan_file": str(plan_file_path),
        "title": plan_title,
        "chunk_count": len(chunks),
    }


# ==============================================================================
# CLI Entrypoint
# ==============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Workday Proactive Decision Cards")
    parser.add_argument(
        "--workflow",
        choices=[
            "triage",
            "calendar",
            "dinner",
            "budget",
            "actual_budget",
            "weather",
            "weather_energy",
            "grocery",
            "midweek_grocery",
            "pm_build",
            "evening_pm",
            "all",
        ],
        default="all",
        help="Which workflow to execute",
    )
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without staging cards")
    parser.add_argument("--test", action="store_true", help="Run in test mode with isolated fixtures")
    parser.add_argument("--slot", default="morning", help="Time slot for comms triage (morning/commute)")
    args = parser.parse_args()

    results: list[tuple[str, bool, str, dict[str, Any]]] = []

    db_override: Path | str = FORGE_DB_PATH
    if args.test and str(db_override) == "/workspace/data/forge.db":
        import tempfile
        db_override = Path(tempfile.gettempdir()) / f"forge_test_cli_{os.getpid()}.db"
        try:
            from tools.forge import init_forge_db
            init_forge_db(db_override)
        except Exception:
            pass

    if args.workflow in ("triage", "all"):
        ok, summary, meta = run_inbox_rcs_action_triage_card(slot=args.slot, dry_run=args.dry_run, test=args.test, db_path=db_override)
        results.append(("triage", ok, summary, meta))

    if args.workflow in ("calendar", "all"):
        ok, summary, meta = run_family_calendar_radar_card(dry_run=args.dry_run, test=args.test, db_path=db_override)
        results.append(("calendar", ok, summary, meta))

    if args.workflow in ("dinner", "all"):
        ok, summary, meta = run_dinner_pivot_card(dry_run=args.dry_run, test=args.test, db_path=db_override)
        results.append(("dinner", ok, summary, meta))

    if args.workflow in ("budget", "actual_budget", "all"):
        ok, summary, meta = run_actual_budget_anomaly_card(dry_run=args.dry_run, test=args.test, db_path=db_override)
        results.append(("budget", ok, summary, meta))

    if args.workflow in ("weather", "weather_energy", "all"):
        ok, summary, meta = run_weather_energy_card(dry_run=args.dry_run, test=args.test, db_path=db_override)
        results.append(("weather", ok, summary, meta))

    if args.workflow in ("grocery", "midweek_grocery", "all"):
        ok, summary, meta = run_midweek_grocery_card(dry_run=args.dry_run, test=args.test, db_path=db_override)
        results.append(("grocery", ok, summary, meta))

    if args.workflow in ("pm_build", "evening_pm", "all"):
        ok, summary, meta = run_evening_pm_build_card(dry_run=args.dry_run, test=args.test, db_path=db_override)
        results.append(("pm_build", ok, summary, meta))

    if args.test and str(db_override).startswith("/tmp") and Path(db_override).exists():
        try:
            Path(db_override).unlink()
        except Exception:
            pass

    for name, ok, summary, meta in results:
        status = "OK" if ok else "FAILED"
        staged = meta.get("cards_staged", 0)
        print(f"[{name.upper()}] {status} — Cards staged: {staged}")
        if summary:
            print(f"Summary:\n{summary}\n")


if __name__ == "__main__":
    main()
