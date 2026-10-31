#!/usr/bin/env python3
"""
Google Tasks Two-Way Sync Engine for Zero
Author: Zero
Description:
    Provides robust, bidirectional synchronization between Zero's task tracker
    (/workspace/data/tasks.json) and Google Tasks app ('Zero's List').

Features:
- Bidirectional status sync: checks off tasks completed on phone or in Zero.
- Automatic import: tasks added in Google Tasks app are imported to Zero.
- Automatic export: tasks added in Zero are pushed to Google Tasks.
- Title and metadata synchronization with Zero Task ID tags in notes.
- Conflict-safe state tracking in /workspace/data/google_tasks_sync_state.json.
- Fully compatible with the 5-layer sidecar framework.
"""

import argparse
import http.client
import json
import logging
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(TOOLS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR.parent))

from tools.workspace_mcp import _get_access_token

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
TASKS_FILE = DATA_DIR / "tasks.json"
SYNC_STATE_FILE = DATA_DIR / "google_tasks_sync_state.json"

TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
TARGET_LIST_TITLE = "Zero's List"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("google_tasks_sync")


def _headers() -> dict:
    tok = _get_access_token()
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }


def _api_request(req: urllib.request.Request, timeout: float = 25.0, retries: int = 3, backoff: float = 1.5) -> bytes:
    """Execute HTTP request against Google Tasks API with exponential backoff and retries."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # Retry on transient server errors or rate limits
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                log.warning("Google Tasks API HTTP %d on attempt %d/%d, retrying in %.1fs...", e.code, attempt + 1, retries + 1, backoff * (attempt + 1))
                time.sleep(backoff * (attempt + 1))
                continue
            raise
        except (http.client.RemoteDisconnected, urllib.error.URLError, TimeoutError, socket.timeout, ConnectionResetError, ssl.SSLError) as e:
            last_err = e
            if attempt < retries:
                log.warning("Google Tasks API network error (%s) on attempt %d/%d, retrying in %.1fs...", e, attempt + 1, retries + 1, backoff * (attempt + 1))
                time.sleep(backoff * (attempt + 1))
                continue
            raise last_err


def list_tasklists() -> list[dict]:
    """Retrieve all task lists for the user."""
    req = urllib.request.Request(f"{TASKS_BASE}/users/@me/lists", headers=_headers())
    data = json.loads(_api_request(req).decode("utf-8"))
    return data.get("items", [])


def get_or_create_target_list(title: str = TARGET_LIST_TITLE) -> str:
    """Find or create 'Zero's List' and return its Google TaskList ID."""
    state = load_sync_state()
    cached_id = state.get("list_id")
    if cached_id:
        return cached_id

    tasklists = list_tasklists()
    for tl in tasklists:
        if tl.get("title", "").strip().lower() == title.strip().lower():
            state["list_id"] = tl["id"]
            save_sync_state(state)
            return tl["id"]

    # Create new tasklist
    body = json.dumps({"title": title}).encode("utf-8")
    req = urllib.request.Request(f"{TASKS_BASE}/users/@me/lists", data=body, headers=_headers(), method="POST")
    data = json.loads(_api_request(req).decode("utf-8"))
    new_id = data["id"]
    state["list_id"] = new_id
    save_sync_state(state)
    return new_id


def fetch_all_google_tasks(list_id: str) -> list[dict]:
    """Fetch all tasks in the list including completed and hidden items."""
    tasks = []
    page_token = None
    while True:
        params = {
            "showCompleted": "true",
            "showHidden": "true",
            "maxResults": "100",
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{TASKS_BASE}/lists/{list_id}/tasks?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=_headers())
        data = json.loads(_api_request(req).decode("utf-8"))
        tasks.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return tasks


def infer_appropriate_due_datetime(
    title: str,
    priority: str = "p2",
    explicit_due: str | None = None,
    now_pt: datetime | None = None,
) -> tuple[str, str]:
    """Infer an appropriate due date and target PT time for a task.

    Returns:
        (due_rfc3339_utc, target_time_pt_display)
        - Google Tasks REST API v1 stores the date portion of `due` in RFC 3339 UTC (`YYYY-MM-DDT00:00:00.000Z`).
        - Also returns a human-readable PT date/time string (e.g. `Sat Oct 31 @ 10:00 AM PT`) for task notes/metadata.
    """
    now = now_pt or datetime.now(PT)
    t_lower = (title or "").lower()

    target_dt: datetime | None = None

    # 1. Explicit ISO date or datetime passed via --due
    if explicit_due:
        exp = explicit_due.strip()
        try:
            if "T" in exp or " " in exp:
                cleaned = exp.replace(" ", "T")
                dt = datetime.fromisoformat(cleaned)
                target_dt = dt.astimezone(PT) if dt.tzinfo else dt.replace(tzinfo=PT)
            else:
                d = datetime.strptime(exp[:10], "%Y-%m-%d")
                target_dt = datetime(d.year, d.month, d.day, 9, 0, tzinfo=PT)
        except Exception:
            target_dt = None

    # 2. Parse embedded date/time in title, e.g. "(Sun Nov 01 at 01:30 PM)" or "Nov 01" or "2026-11-01"
    if target_dt is None:
        m_iso = re.search(r"\b(202\d-\d{2}-\d{2})\b", title)
        if m_iso:
            try:
                d = datetime.strptime(m_iso.group(1), "%Y-%m-%d")
                target_dt = datetime(d.year, d.month, d.day, 9, 0, tzinfo=PT)
            except Exception:
                pass

    if target_dt is None:
        months = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        m_mon = re.search(
            r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})\b",
            t_lower,
        )
        if m_mon:
            mon_num = months[m_mon.group(1)[:3]]
            day_num = int(m_mon.group(2))
            year_num = now.year
            try:
                cand = datetime(year_num, mon_num, day_num, 9, 0, tzinfo=PT)
                if cand.date() < (now.date() - timedelta(days=30)):
                    cand = datetime(year_num + 1, mon_num, day_num, 9, 0, tzinfo=PT)
                # Parse optional time like "at 01:30 PM" or "at 9:00 AM"
                m_time = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t_lower)
                if m_time:
                    hr = int(m_time.group(1))
                    mn = int(m_time.group(2) or 0)
                    ampm = m_time.group(3)
                    if ampm == "pm" and hr < 12:
                        hr += 12
                    elif ampm == "am" and hr == 12:
                        hr = 0
                    cand = cand.replace(hour=hr, minute=mn)
                # If this is a "prep for" task for a future event, schedule prep the day before at 10:00 AM PT (or morning of if tomorrow/today)
                if t_lower.startswith("prep ") or "prep for" in t_lower or "gift" in t_lower:
                    if cand.date() > now.date():
                        prep_day = cand.date() - timedelta(days=1)
                        if prep_day < now.date():
                            prep_day = now.date()
                        cand = datetime(prep_day.year, prep_day.month, prep_day.day, 10, 0, tzinfo=PT)
                    else:
                        cand = cand.replace(hour=9, minute=0)
                target_dt = cand
            except Exception:
                pass

    # 3. Relative keywords ("today", "tonight", "tomorrow")
    if target_dt is None:
        if "tonight" in t_lower:
            target_dt = now.replace(hour=18, minute=0, second=0, microsecond=0)
        elif "today" in t_lower:
            hr = 17 if now.hour < 17 else min(21, now.hour + 1)
            target_dt = now.replace(hour=hr, minute=0, second=0, microsecond=0)
        elif "tomorrow" in t_lower:
            tmr = now + timedelta(days=1)
            target_dt = tmr.replace(hour=9, minute=0, second=0, microsecond=0)

    # 4. Priority-based smart default when no explicit date is mentioned
    if target_dt is None:
        prio = (priority or "p2").lower()
        if prio == "p1":
            # P1: today at 5:00 PM PT if before 3:00 PM, otherwise tomorrow at 9:00 AM PT
            if now.hour < 15:
                target_dt = now.replace(hour=17, minute=0, second=0, microsecond=0)
            else:
                tmr = now + timedelta(days=1)
                target_dt = tmr.replace(hour=9, minute=0, second=0, microsecond=0)
        elif prio == "p2":
            # P2: tomorrow at 10:00 AM PT
            tmr = now + timedelta(days=1)
            target_dt = tmr.replace(hour=10, minute=0, second=0, microsecond=0)
        else:
            # P3: upcoming Saturday at 10:00 AM PT
            days_until_sat = (5 - now.weekday()) % 7
            if days_until_sat == 0:
                days_until_sat = 7
            sat = now + timedelta(days=days_until_sat)
            target_dt = sat.replace(hour=10, minute=0, second=0, microsecond=0)

    due_rfc3339 = f"{target_dt.strftime('%Y-%m-%d')}T00:00:00.000Z"
    disp_pt = target_dt.strftime("%a %b %d @ %-I:%M %p PT")
    return due_rfc3339, disp_pt


def create_google_task(
    list_id: str,
    title: str,
    notes: str = "",
    status: str = "needsAction",
    due: str | None = None,
) -> dict:
    """Create a task in Google Tasks."""
    payload: dict = {
        "title": title.strip(),
        "notes": notes.strip(),
        "status": status,
    }
    if due:
        payload["due"] = due
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{TASKS_BASE}/lists/{list_id}/tasks", data=data, headers=_headers(), method="POST")
    return json.loads(_api_request(req).decode("utf-8"))


def patch_google_task(list_id: str, task_id: str, updates: dict) -> dict:
    """Update a task in Google Tasks."""
    data = json.dumps(updates).encode("utf-8")
    req = urllib.request.Request(
        f"{TASKS_BASE}/lists/{list_id}/tasks/{task_id}",
        data=data,
        headers=_headers(),
        method="PATCH",
    )
    return json.loads(_api_request(req).decode("utf-8"))


def delete_google_task(list_id: str, task_id: str) -> bool:
    """Delete a task from Google Tasks."""
    req = urllib.request.Request(
        f"{TASKS_BASE}/lists/{list_id}/tasks/{task_id}",
        headers=_headers(),
        method="DELETE",
    )
    try:
        _api_request(req)
        return True
    except Exception:
        return False


def load_sync_state() -> dict:
    """Load persistent sync state mapping."""
    if SYNC_STATE_FILE.exists():
        try:
            with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"list_id": None, "list_title": TARGET_LIST_TITLE, "last_sync": "", "mappings": {}}


def save_sync_state(state: dict):
    """Persist sync state to disk atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SYNC_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(SYNC_STATE_FILE)


def load_local_tasks() -> list[dict]:
    """Load tasks from /workspace/data/tasks.json."""
    if TASKS_FILE.exists():
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_local_tasks(tasks: list[dict]):
    """Persist tasks to /workspace/data/tasks.json atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)
    tmp.replace(TASKS_FILE)


def extract_task_id_from_notes(notes: str | None) -> int | None:
    """Extract local Zero task ID from notes tag, e.g. [Zero Task #4 | Priority: P2]."""
    if not notes:
        return None
    m = re.search(r"\[Zero Task #(\d+)", notes)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


DEV_PATTERNS = [
    r"^(feat|fix|refactor|chore|test|perf|ci|docs)[\(:]",
    r"https?://github\.com/",
    r"github issue",
    r"\(issue #\d+\)",
    r"#\d+:",
    r"\bpr #\d+\b",
    r"\bagora[- ]roguelike\b",
    r"\bagent[- ]playbook\b",
    r"\bagora\b",
    r"\bbanana[- ]protocol\b",
    r"\begress telemetry\b",
    r"\bucp client\b",
    r"\bagentic storefronts\b",
    r"\bharden agent identity\b",
    r"\breview aerial/alex\b",
    r"\bmarvin persona\b",
    r"\bterminal web hud\b",
    r"\bmarket variance rfc\b",
    r"\brivals & corporate warfare\b",
    r"\bmemsearch\b",
]


def is_dev_task(task: dict) -> bool:
    """Return True if task is an internal software engineering, repository, or agent ticket."""
    if task.get("dev") is True or task.get("type") in ("dev", "engineering", "code"):
        return True
    text = f"{task.get('title', '')} {task.get('notes') or ''}".lower().strip()
    return any(re.search(pat, text) for pat in DEV_PATTERNS)


def sync_tasks(quiet: bool = False) -> dict:
    """
    Execute full two-way synchronization between tasks.json and Google Tasks.
    Returns:
        dict with sync statistics and summary message.
    """
    start_time = datetime.now(PT)
    list_id = get_or_create_target_list(TARGET_LIST_TITLE)
    state = load_sync_state()
    mappings = state.get("mappings", {})

    local_tasks = load_local_tasks()
    google_tasks = fetch_all_google_tasks(list_id)

    # Fast indexes
    local_by_id = {t["id"]: t for t in local_tasks}
    google_by_id = {gt["id"]: gt for gt in google_tasks}

    changes = {
        "pushed_to_google": [],
        "pulled_to_local": [],
        "created_in_google": [],
        "created_in_local": [],
        "deleted_in_google": [],
    }

    # Step 1: Discover unmapped Google tasks that have local ID in notes
    for gt in google_tasks:
        gid = gt["id"]
        already_mapped = any(m.get("google_id") == gid for m in mappings.values())
        if already_mapped:
            continue

        embedded_id = extract_task_id_from_notes(gt.get("notes"))
        if embedded_id and embedded_id in local_by_id:
            loc = local_by_id[embedded_id]
            if is_dev_task(loc):
                continue
            lid_str = str(embedded_id)
            mappings[lid_str] = {
                "google_id": gid,
                "last_title": gt.get("title", ""),
                "last_status": loc.get("status", "pending"),
                "last_google_status": gt.get("status", "needsAction"),
            }
            loc["google_task_id"] = gid

    # Step 2: Remote -> Local (Incorporate changes Ryan made on phone/web)
    for gt in google_tasks:
        gid = gt["id"]
        gt_title = gt.get("title", "").strip()
        gt_status = gt.get("status", "needsAction")

        matching_lid = None
        for lid_str, m in mappings.items():
            if m.get("google_id") == gid:
                matching_lid = lid_str
                break

        if matching_lid and int(matching_lid) in local_by_id:
            loc = local_by_id[int(matching_lid)]
            if is_dev_task(loc):
                continue
            map_entry = mappings[matching_lid]

            # 2a. Status change on Google Tasks
            last_g_status = map_entry.get("last_google_status")
            if gt_status != last_g_status:
                if gt_status == "completed" and loc.get("status") != "completed":
                    loc["status"] = "completed"
                    loc["updated"] = datetime.now(PT).strftime("%Y-%m-%d %H:%M")
                    changes["pulled_to_local"].append(f"Completed #{loc['id']}: {loc['title']}")
                elif gt_status == "needsAction" and loc.get("status") == "completed":
                    loc["status"] = "pending"
                    loc["updated"] = datetime.now(PT).strftime("%Y-%m-%d %H:%M")
                    changes["pulled_to_local"].append(f"Reopened #{loc['id']}: {loc['title']}")
                map_entry["last_google_status"] = gt_status

            # 2b. Title edit on Google Tasks
            last_title = map_entry.get("last_title")
            if gt_title and gt_title != last_title and gt_title != loc.get("title"):
                old_title = loc.get("title")
                loc["title"] = gt_title
                loc["updated"] = datetime.now(PT).strftime("%Y-%m-%d %H:%M")
                changes["pulled_to_local"].append(f"Renamed #{loc['id']} ('{old_title}' -> '{gt_title}')")
                map_entry["last_title"] = gt_title

        elif not matching_lid and gt_title:
            # New task created on Google Tasks app by Ryan!
            new_id = max([t.get("id", 0) for t in local_tasks], default=0) + 1
            new_status = "completed" if gt_status == "completed" else "pending"
            new_task = {
                "id": new_id,
                "title": gt_title,
                "priority": "p2",
                "status": new_status,
                "updated": datetime.now(PT).strftime("%Y-%m-%d %H:%M"),
                "google_task_id": gid,
            }
            local_tasks.append(new_task)
            local_by_id[new_id] = new_task

            # Stamp Zero Task ID tag in Google notes
            tag = f"[Zero Task #{new_id} | Priority: P2]"
            current_notes = gt.get("notes", "")
            updated_notes = f"{tag}\n{current_notes}".strip()
            gt["notes"] = updated_notes
            try:
                patch_google_task(list_id, gid, {"notes": updated_notes})
            except Exception:
                pass

            mappings[str(new_id)] = {
                "google_id": gid,
                "last_title": gt_title,
                "last_status": new_status,
                "last_google_status": gt_status,
            }
            changes["created_in_local"].append(f"Imported #{new_id}: {gt_title}")

    # Step 3: Local -> Remote (Push changes created or updated in Zero)
    for loc in local_tasks:
        lid_str = str(loc["id"])
        map_entry = mappings.get(lid_str)

        if is_dev_task(loc):
            # Internal dev tasks are never synced to Google Tasks. If previously mapped, purge from Google Tasks.
            if map_entry:
                gid = map_entry.get("google_id")
                if gid:
                    try:
                        delete_google_task(list_id, gid)
                        changes["deleted_in_google"].append(f"Purged dev task #{loc['id']}: {loc['title']}")
                    except Exception as e:
                        log.warning(f"Error purging dev task #{loc['id']} from Google Tasks: {e}")
                mappings.pop(lid_str, None)
                if "google_task_id" in loc:
                    del loc["google_task_id"]
            continue

        is_completed = loc.get("status") in ("completed", "retired")
        target_g_status = "completed" if is_completed else "needsAction"
        prio_label = loc.get("priority", "p2").upper()
        due_rfc3339, disp_pt = infer_appropriate_due_datetime(
            title=loc.get("title", ""),
            priority=loc.get("priority", "p2"),
            explicit_due=loc.get("due"),
            now_pt=start_time,
        )
        time_label = loc.get("target_time_pt") or disp_pt
        tag = f"[Zero Task #{loc['id']} | Priority: {prio_label} | Target: {time_label}]"

        if map_entry:
            gid = map_entry.get("google_id")
            gt = google_by_id.get(gid)
            if gt:
                patch_data = {}
                # Title change in Zero
                if loc["title"] != map_entry.get("last_title") and loc["title"] != gt.get("title"):
                    patch_data["title"] = loc["title"]
                # Status change in Zero
                if target_g_status != map_entry.get("last_google_status") and target_g_status != gt.get("status"):
                    patch_data["status"] = target_g_status
                # Assign due date if explicitly set/updated in Zero
                if not is_completed and loc.get("due") and (
                    not gt.get("due")
                    or (map_entry.get("last_due") and loc.get("due") != map_entry.get("last_due"))
                ):
                    patch_data["due"] = due_rfc3339
                # Notes priority update (only update if Zero Task tag is completely missing)
                gt_notes = gt.get("notes") or ""
                if f"[Zero Task #{loc['id']}" not in gt_notes:
                    base_notes = re.sub(r"\[Zero Task #\d+[^\]]*\]\n?", "", gt_notes).strip()
                    patch_data["notes"] = f"{tag}\n{base_notes}".strip()

                if patch_data:
                    try:
                        patch_google_task(list_id, gid, patch_data)
                        changes["pushed_to_google"].append(f"Updated #{loc['id']}: {list(patch_data.keys())}")
                        if "title" in patch_data:
                            map_entry["last_title"] = patch_data["title"]
                        if "status" in patch_data:
                            map_entry["last_google_status"] = patch_data["status"]
                        if "due" in patch_data:
                            map_entry["last_due"] = loc.get("due")
                    except Exception as e:
                        log.warning(f"Error patching Google task #{loc['id']}: {e}")
            else:
                # Task was deleted from Google Tasks; retire locally if currently active
                if loc.get("status") not in ("completed", "retired"):
                    loc["status"] = "retired"
                    loc["updated"] = datetime.now(PT).strftime("%Y-%m-%d %H:%M")
                    changes["pulled_to_local"].append(f"Retired #{loc['id']} (deleted from Google Tasks)")
                    map_entry["last_status"] = "retired"
                    map_entry["last_google_status"] = "completed"
        else:
            # New task in Zero: create in Google Tasks with appropriate due date & target time
            if not loc.get("due"):
                loc["due"] = due_rfc3339[:10]
            if not loc.get("target_time_pt"):
                loc["target_time_pt"] = disp_pt
            try:
                res = create_google_task(
                    list_id=list_id,
                    title=loc["title"],
                    notes=tag,
                    status=target_g_status,
                    due=due_rfc3339,
                )
                gid = res["id"]
                loc["google_task_id"] = gid
                mappings[lid_str] = {
                    "google_id": gid,
                    "last_title": loc["title"],
                    "last_status": loc.get("status", "pending"),
                    "last_google_status": target_g_status,
                    "last_due": loc.get("due"),
                }
                changes["created_in_google"].append(f"Exported #{loc['id']}: {loc['title']} (due {time_label})")
            except Exception as e:
                log.error(f"Error exporting task #{loc['id']} to Google Tasks: {e}")

    # Step 4: Propagate local deletions (tasks removed from tasks.json -> delete from Google Tasks)
    for lid_str in list(mappings.keys()):
        try:
            lid_int = int(lid_str)
        except ValueError:
            continue
        if lid_int not in local_by_id:
            map_entry = mappings.pop(lid_str, {})
            gid = map_entry.get("google_id")
            if gid and gid in google_by_id:
                try:
                    delete_google_task(list_id, gid)
                    changes["deleted_in_google"].append(f"Deleted task #{lid_int}: {map_entry.get('last_title', '')}")
                except Exception as e:
                    log.warning(f"Error deleting locally removed task #{lid_int} from Google Tasks: {e}")

    # Persist updated state and local tasks
    save_local_tasks(local_tasks)
    state["last_sync"] = datetime.now(PT).strftime("%Y-%m-%d %H:%M PT")
    state["mappings"] = mappings
    save_sync_state(state)

    total_ops = sum(len(v) for v in changes.values())
    summary_parts = []
    if changes["created_in_google"]:
        summary_parts.append(f"Exported {len(changes['created_in_google'])} task(s) to Google Tasks")
    if changes["created_in_local"]:
        summary_parts.append(f"Imported {len(changes['created_in_local'])} task(s) from Google Tasks")
    if changes["pulled_to_local"]:
        summary_parts.append(f"Updated {len(changes['pulled_to_local'])} local task(s)")
    if changes["pushed_to_google"]:
        summary_parts.append(f"Updated {len(changes['pushed_to_google'])} Google task(s)")
    if changes["deleted_in_google"]:
        summary_parts.append(f"Purged {len(changes['deleted_in_google'])} dev task(s) from Google Tasks")

    msg = "; ".join(summary_parts) if summary_parts else "All tasks in sync (nominal)."

    if not quiet or total_ops > 0:
        log.info(f"Sync complete in {(datetime.now(PT) - start_time).total_seconds():.2f}s: {msg}")

    return {
        "ok": True,
        "total_operations": total_ops,
        "changes": changes,
        "summary": msg,
        "active_count": len([t for t in local_tasks if t.get("status") not in ("completed", "retired")]),
        "completed_count": len([t for t in local_tasks if t.get("status") == "completed"]),
    }


def main():
    parser = argparse.ArgumentParser(description="Google Tasks Two-Way Sync for Zero")
    parser.add_argument("--sync", action="store_true", help="Perform two-way synchronization")
    parser.add_argument("--status", action="store_true", help="Display sync state summary")
    parser.add_argument("--quiet", action="store_true", help="Suppress output if no changes made")
    args = parser.parse_args()

    if args.status:
        state = load_sync_state()
        local_tasks = load_local_tasks()
        print(f"List Title: {state.get('list_title')}")
        print(f"List ID: {state.get('list_id')}")
        print(f"Last Sync: {state.get('last_sync', 'Never')}")
        print(f"Mapped Tasks: {len(state.get('mappings', {}))}/{len(local_tasks)}")
        return

    # Default to sync
    res = sync_tasks(quiet=args.quiet)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
