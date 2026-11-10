#!/usr/bin/env python3
"""Work Calendar Sync Consumer (`zero.work_cal_sync.v1`).

Parses and persists `[WORK-CAL-SYNC] Rolling 14-Day Window` email payloads
into `/workspace/data/work_calendar_sync.json` and exposes cached work events
for calendar queries and conflict radar.
"""

import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WORK_CAL_SCHEMA = "zero.work_cal_sync.v1"
WORK_CAL_STATE_PATH = Path(os.environ.get("DATA_DIR", "/workspace/data")) / "work_calendar_sync.json"
DEFAULT_TZ = ZoneInfo("America/Los_Angeles")
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
SYNC_SOURCE_KEY = "zero_sync_source"
SYNC_SOURCE_VAL = "work_cal_sync_v1"
SYNC_UID_KEY = "zero_work_cal_uid"
SYNC_DESC = "Synced via [WORK-CAL-SYNC]"
TIMEOUT = 15

HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^<>]*)?>")
STYLE_SCRIPT_RE = re.compile(r"<style[\s\S]*?</style>|<script[\s\S]*?</script>", flags=re.IGNORECASE)


def _decode_b64url(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _strip_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    cleaned = STYLE_SCRIPT_RE.sub(" ", raw_html)
    cleaned = HTML_TAG_RE.sub(" ", cleaned)
    return html.unescape(cleaned)


def _collect_part_texts(part: dict, target_mime: str, results: list[str]) -> None:
    if not isinstance(part, dict):
        return
    mime = (part.get("mimeType") or "").lower()
    body_data = part.get("body", {}).get("data")
    if mime == target_mime and body_data:
        decoded = _decode_b64url(body_data)
        if decoded:
            results.append(decoded)
    for sub in part.get("parts", []) or []:
        _collect_part_texts(sub, target_mime, results)


def _try_parse_work_cal_json(text: str) -> dict | None:
    if not text:
        return None
    for candidate_str in (text.strip(), html.unescape(text).strip()):
        if not candidate_str:
            continue
        # Direct parse
        try:
            obj = json.loads(candidate_str)
            if isinstance(obj, dict) and obj.get("schema") == WORK_CAL_SCHEMA:
                return obj
        except Exception:  # noqa: BLE001, S110
            pass

        # Substring JSON object scan via JSONDecoder.raw_decode
        decoder = json.JSONDecoder()
        idx = 0
        while True:
            brace_pos = candidate_str.find("{", idx)
            if brace_pos == -1:
                break
            try:
                obj, _ = decoder.raw_decode(candidate_str, brace_pos)
                if isinstance(obj, dict) and obj.get("schema") == WORK_CAL_SCHEMA:
                    return obj
            except Exception:  # noqa: BLE001, S110
                pass
            idx = brace_pos + 1

        # Fallback: first '{' to last '}'
        first_brace = candidate_str.find("{")
        last_brace = candidate_str.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            try:
                obj = json.loads(candidate_str[first_brace : last_brace + 1])
                if isinstance(obj, dict) and obj.get("schema") == WORK_CAL_SCHEMA:
                    return obj
            except Exception:  # noqa: BLE001, S110
                pass
    return None


def extract_work_cal_json_from_message(m_data: dict) -> dict | None:
    """Decode base64url body parts (text/plain, text/html strip tags, or snippet fallback)
    to locate and parse the JSON payload with schema == 'zero.work_cal_sync.v1'.
    """
    if not isinstance(m_data, dict):
        return None

    # Allow passing an already-unwrapped payload dict directly in tests/callers
    if m_data.get("schema") == WORK_CAL_SCHEMA and isinstance(m_data.get("events"), list):
        return m_data

    payload = m_data.get("payload")
    candidates: list[str] = []

    if isinstance(payload, dict):
        plain_parts: list[str] = []
        _collect_part_texts(payload, "text/plain", plain_parts)
        candidates.extend(plain_parts)

        html_parts: list[str] = []
        _collect_part_texts(payload, "text/html", html_parts)
        for h in html_parts:
            candidates.append(_strip_html(h))

        # Direct payload.body.data fallback regardless of mimeType
        top_data = payload.get("body", {}).get("data")
        if top_data:
            decoded_top = _decode_b64url(top_data)
            if decoded_top:
                candidates.append(decoded_top)
                candidates.append(_strip_html(decoded_top))

    snippet = m_data.get("snippet")
    if isinstance(snippet, str) and snippet.strip():
        candidates.append(snippet)
        candidates.append(_strip_html(snippet))

    for text in candidates:
        parsed = _try_parse_work_cal_json(text)
        if parsed is not None:
            return parsed

    return None


def _resolve_access_token(tok: str | None = None) -> str:
    if tok is not None:
        return tok
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from tools.zero_mail_listener import get_access_token
    except ImportError:
        from zero_mail_listener import get_access_token
    return get_access_token()


def ingest_work_cal_payload(
    payload: dict,
    state_path: Path | None = None,
    reconcile_calendar: bool = False,
    calendar_id: str = "primary",
    tok: str | None = None,
) -> dict:
    """Validate schema/events and atomically write normalized state to work_calendar_sync.json."""
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a dictionary.")  # noqa: TRY004
    if payload.get("schema") != WORK_CAL_SCHEMA:
        raise ValueError(f"Unsupported work calendar schema: {payload.get('schema')!r}")

    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("Payload 'events' must be a list.")  # noqa: TRY004

    normalized_events: list[dict] = []
    seen_hashes: set[str] = set()
    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        start = str(ev.get("start") or "").strip()
        end = str(ev.get("end") or "").strip()
        if not start or not end:
            continue
        summary = str(ev.get("summary") or "Work Meeting").strip() or "Work Meeting"
        uid_hash = str(ev.get("uid_hash") or "").strip()
        if not uid_hash:
            uid_hash = hashlib.sha256(f"{summary}|{start}|{end}".encode()).hexdigest()[:16]
        if uid_hash in seen_hashes:
            continue
        seen_hashes.add(uid_hash)
        norm_ev = {
            "uid_hash": uid_hash,
            "summary": summary,
            "start": start,
            "end": end,
        }
        if ev.get("location"):
            norm_ev["location"] = str(ev["location"]).strip()
        normalized_events.append(norm_ev)

    normalized_events.sort(key=lambda x: x.get("start", ""))

    state = {
        "schema": WORK_CAL_SCHEMA,
        "generated_at": payload.get("generated_at", ""),
        "ingested_at": datetime.now(UTC).isoformat(),
        "timezone": payload.get("timezone", "America/Los_Angeles"),
        "window_start": payload.get("window_start", ""),
        "window_end": payload.get("window_end", ""),
        "events": normalized_events,
    }

    target_path = Path(state_path) if state_path is not None else WORK_CAL_STATE_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".work_cal_sync_",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if reconcile_calendar:
        reconcile_work_cal_to_google_calendar(
            state,
            state_path=target_path,
            calendar_id=calendar_id,
            tok=tok,
        )

    return state


def ingest_work_cal_message(
    m_data: dict,
    state_path: Path | None = None,
    reconcile_calendar: bool = False,
    calendar_id: str = "primary",
    tok: str | None = None,
) -> bool:
    """Extract and ingest a [WORK-CAL-SYNC] payload from a Gmail message dictionary."""
    payload = extract_work_cal_json_from_message(m_data)
    if not payload:
        return False
    try:
        ingest_work_cal_payload(
            payload,
            state_path=state_path,
            reconcile_calendar=reconcile_calendar,
            calendar_id=calendar_id,
            tok=tok,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _parse_aware_datetime(iso_str: str, fallback_tz: ZoneInfo = DEFAULT_TZ) -> datetime | None:
    if not iso_str or not isinstance(iso_str, str):
        return None
    s = iso_str.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=fallback_tz)
    return dt.astimezone(UTC)


def get_cached_work_events(
    time_min_iso: str = "",
    time_max_iso: str = "",
    state_path: Path | None = None,
) -> list[dict]:
    """Read work_calendar_sync.json, filter events overlapping [time_min_iso, time_max_iso]
    using aware datetime comparison, and return formatted calendar event dicts.
    """
    target_path = Path(state_path) if state_path is not None else WORK_CAL_STATE_PATH
    if not target_path.exists():
        return []

    try:
        data = json.loads(target_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []

    if not isinstance(data, dict) or data.get("schema") != WORK_CAL_SCHEMA:
        return []

    tz_name = data.get("timezone") or "America/Los_Angeles"
    try:
        fallback_tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        fallback_tz = DEFAULT_TZ

    t_min = _parse_aware_datetime(time_min_iso, fallback_tz=fallback_tz) if time_min_iso else None
    t_max = _parse_aware_datetime(time_max_iso, fallback_tz=fallback_tz) if time_max_iso else None

    results: list[dict] = []
    for ev in data.get("events", []):
        if not isinstance(ev, dict):
            continue
        start_str = ev.get("start")
        end_str = ev.get("end")
        uid_hash = ev.get("uid_hash")
        if not start_str or not end_str or not uid_hash:
            continue

        ev_start = _parse_aware_datetime(start_str, fallback_tz=fallback_tz)
        ev_end = _parse_aware_datetime(end_str, fallback_tz=fallback_tz)
        if ev_start is None or ev_end is None:
            continue

        if t_min is not None and ev_end <= t_min:
            continue
        if t_max is not None and ev_start >= t_max:
            continue

        results.append(
            {
                "id": f"work_{uid_hash}",
                "calendar": "Work (Ryan)",
                "summary": ev.get("summary", "Work Meeting"),
                "start": start_str,
                "end": end_str,
                "location": ev.get("location", ""),
                "description": "Synced via [WORK-CAL-SYNC]",
            }
        )

    results.sort(key=lambda x: x.get("start", ""))
    return results


def archive_work_cal_message(
    mid: str,
    tok: str | None = None,
    thread_id: str | None = None,
) -> bool:
    """Remove INBOX, UNREAD, and IMPORTANT labels from a [WORK-CAL-SYNC] message (and thread)."""
    if not mid:
        return False
    resolved_tok = _resolve_access_token(tok)

    body = json.dumps({"removeLabelIds": ["INBOX", "UNREAD", "IMPORTANT"]}).encode()
    headers = {
        "Authorization": f"Bearer {resolved_tok}",
        "Content-Type": "application/json",
    }
    ok = False
    try:
        mod_url = f"{GMAIL_BASE}/messages/{mid}/modify"
        mod_req = urllib.request.Request(mod_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(mod_req, timeout=TIMEOUT) as resp:
            ok = resp.status in (200, 204) or True
    except Exception:  # noqa: BLE001
        return False

    if thread_id:
        try:
            t_url = f"{GMAIL_BASE}/threads/{thread_id}/modify"
            t_req = urllib.request.Request(t_url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(t_req, timeout=TIMEOUT):
                pass
        except Exception:  # noqa: BLE001, S110
            pass

    return ok


def reconcile_work_cal_to_google_calendar(
    state: dict | None = None,
    *,
    state_path: Path | None = None,
    calendar_id: str = "primary",
    tok: str | None = None,
) -> dict:
    """Upsert new/moved 'Work Meeting' blocks and delete canceled/declined ones
    on Google Calendar, strictly restricted to events with zero_sync_source == 'work_cal_sync_v1'.
    """
    if state is None:
        target_path = Path(state_path) if state_path is not None else WORK_CAL_STATE_PATH
        if not target_path.exists():
            raise FileNotFoundError(f"Work calendar state file not found: {target_path}")
        state = json.loads(target_path.read_text(encoding="utf-8"))

    if not isinstance(state, dict):
        raise ValueError("Payload must be a dictionary.")  # noqa: TRY004
    if state.get("schema") != WORK_CAL_SCHEMA:
        raise ValueError(f"Unsupported work calendar schema: {state.get('schema')!r}")

    resolved_tok = _resolve_access_token(tok)

    tz_name = state.get("timezone") or "America/Los_Angeles"
    try:
        fallback_tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        fallback_tz = DEFAULT_TZ

    now_utc = datetime.now(UTC)
    win_min_dt = _parse_aware_datetime(str(state.get("window_start") or ""), fallback_tz=fallback_tz)
    win_max_dt = _parse_aware_datetime(str(state.get("window_end") or ""), fallback_tz=fallback_tz)
    if win_min_dt is None:
        win_min_dt = now_utc - timedelta(days=1)
    if win_max_dt is None:
        win_max_dt = now_utc + timedelta(days=15)

    encoded_cal_id = urllib.parse.quote(calendar_id, safe="")
    auth_headers = {
        "Authorization": f"Bearer {resolved_tok}",
        "Content-Type": "application/json",
    }

    # Fetch existing synced events from Google Calendar in the sync window
    existing_items: list[dict] = []
    page_token: str | None = None
    while True:
        query_params = {
            "privateExtendedProperty": f"{SYNC_SOURCE_KEY}={SYNC_SOURCE_VAL}",
            "singleEvents": "true",
            "timeMin": win_min_dt.isoformat(),
            "timeMax": win_max_dt.isoformat(),
            "maxResults": "250",
        }
        if page_token:
            query_params["pageToken"] = page_token
        query_str = urllib.parse.urlencode(query_params)
        list_url = f"{CALENDAR_BASE}/calendars/{encoded_cal_id}/events?{query_str}"
        req = urllib.request.Request(list_url, headers=auth_headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in data.get("items", []):
            if isinstance(item, dict):
                existing_items.append(item)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    # Safety lock: Python-side double verification
    # ONLY touch events where extendedProperties.private.zero_sync_source == 'work_cal_sync_v1' and status != 'cancelled'
    existing_by_uid: dict[str, list[dict]] = {}
    stale_or_invalid_events: list[dict] = []
    for item in existing_items:
        if item.get("status") == "cancelled":
            continue
        ext_priv = item.get("extendedProperties", {}).get("private", {})
        if not isinstance(ext_priv, dict) or ext_priv.get(SYNC_SOURCE_KEY) != SYNC_SOURCE_VAL:
            continue
        uid = ext_priv.get(SYNC_UID_KEY)
        if uid:
            existing_by_uid.setdefault(uid, []).append(item)
        else:
            stale_or_invalid_events.append(item)

    inserted = 0
    updated = 0
    deleted = 0
    unchanged = 0

    # Delete any duplicate existing synced events for the same uid
    for uid, items in list(existing_by_uid.items()):
        if len(items) > 1:
            for dup in items[1:]:
                dup_id = dup.get("id")
                if dup_id:
                    del_url = f"{CALENDAR_BASE}/calendars/{encoded_cal_id}/events/{urllib.parse.quote(dup_id, safe='')}"
                    del_req = urllib.request.Request(
                        del_url,
                        headers={"Authorization": f"Bearer {resolved_tok}"},
                        method="DELETE",
                    )
                    try:
                        with urllib.request.urlopen(del_req, timeout=TIMEOUT):
                            pass
                    except urllib.error.HTTPError as exc:
                        if exc.code not in (404, 410):
                            raise
                    deleted += 1
            existing_by_uid[uid] = [items[0]]

    # Delete any stale work_cal_sync_v1 events lacking a UID
    for stale_item in stale_or_invalid_events:
        stale_id = stale_item.get("id")
        if stale_id:
            del_url = f"{CALENDAR_BASE}/calendars/{encoded_cal_id}/events/{urllib.parse.quote(stale_id, safe='')}"
            del_req = urllib.request.Request(
                del_url,
                headers={"Authorization": f"Bearer {resolved_tok}"},
                method="DELETE",
            )
            try:
                with urllib.request.urlopen(del_req, timeout=TIMEOUT):
                    pass
            except urllib.error.HTTPError as exc:
                if exc.code not in (404, 410):
                    raise
            deleted += 1

    # Filter desired events overlapping [win_min_dt, win_max_dt]
    raw_events = state.get("events", [])
    desired_events: list[dict] = []
    desired_uids: set[str] = set()

    for ev in raw_events:
        if not isinstance(ev, dict):
            continue
        start_str = str(ev.get("start") or "").strip()
        end_str = str(ev.get("end") or "").strip()
        uid_hash = str(ev.get("uid_hash") or "").strip()
        if not start_str or not end_str or not uid_hash:
            continue

        ev_start = _parse_aware_datetime(start_str, fallback_tz=fallback_tz)
        ev_end = _parse_aware_datetime(end_str, fallback_tz=fallback_tz)
        if ev_start is None or ev_end is None:
            continue

        if ev_end <= win_min_dt or ev_start >= win_max_dt:
            continue

        desired_events.append(ev)
        desired_uids.add(uid_hash)

    # Reconcile each desired event: POST if missing, PATCH if changed, else unchanged
    for ev in desired_events:
        uid_hash = ev["uid_hash"]
        start_str = ev["start"]
        end_str = ev["end"]

        body = {
            "summary": "Work Meeting",
            "description": SYNC_DESC,
            "start": {"dateTime": start_str, "timeZone": tz_name},
            "end": {"dateTime": end_str, "timeZone": tz_name},
            "transparency": "opaque",
            "reminders": {"useDefault": False, "overrides": []},
            "extendedProperties": {
                "private": {
                    SYNC_SOURCE_KEY: SYNC_SOURCE_VAL,
                    SYNC_UID_KEY: uid_hash,
                }
            },
        }

        existing_match = existing_by_uid.get(uid_hash, [None])[0]
        if existing_match is None:
            # POST new event
            post_url = f"{CALENDAR_BASE}/calendars/{encoded_cal_id}/events"
            post_data = json.dumps(body).encode()
            post_req = urllib.request.Request(post_url, data=post_data, headers=auth_headers, method="POST")
            with urllib.request.urlopen(post_req, timeout=TIMEOUT) as resp:
                resp.read()
            inserted += 1
        else:
            # Check if start/end instant or summary changed
            ex_summary = existing_match.get("summary")
            ex_start_str = (
                existing_match.get("start", {}).get("dateTime")
                or existing_match.get("start", {}).get("date")
                or ""
            )
            ex_end_str = (
                existing_match.get("end", {}).get("dateTime")
                or existing_match.get("end", {}).get("date")
                or ""
            )
            ex_start_dt = _parse_aware_datetime(ex_start_str, fallback_tz=fallback_tz)
            ex_end_dt = _parse_aware_datetime(ex_end_str, fallback_tz=fallback_tz)
            des_start_dt = _parse_aware_datetime(start_str, fallback_tz=fallback_tz)
            des_end_dt = _parse_aware_datetime(end_str, fallback_tz=fallback_tz)

            time_or_summary_changed = (
                ex_summary != "Work Meeting"
                or ex_start_dt != des_start_dt
                or ex_end_dt != des_end_dt
            )
            ex_desc = existing_match.get("description", "")
            ex_transparency = existing_match.get("transparency", "")
            other_fields_changed = (
                ex_desc != SYNC_DESC
                or ex_transparency != "opaque"
            )

            if time_or_summary_changed or other_fields_changed:
                ev_id = existing_match.get("id")
                patch_url = f"{CALENDAR_BASE}/calendars/{encoded_cal_id}/events/{urllib.parse.quote(ev_id, safe='')}"
                patch_data = json.dumps(body).encode()
                patch_req = urllib.request.Request(patch_url, data=patch_data, headers=auth_headers, method="PATCH")
                with urllib.request.urlopen(patch_req, timeout=TIMEOUT) as resp:
                    resp.read()
                updated += 1
            else:
                unchanged += 1

    # Delete any existing work_cal_sync_v1 event whose uid_hash is no longer present in desired events
    for uid, items in existing_by_uid.items():
        if uid not in desired_uids:
            for item in items:
                ev_id = item.get("id")
                if ev_id:
                    del_url = f"{CALENDAR_BASE}/calendars/{encoded_cal_id}/events/{urllib.parse.quote(ev_id, safe='')}"
                    del_req = urllib.request.Request(
                        del_url,
                        headers={"Authorization": f"Bearer {resolved_tok}"},
                        method="DELETE",
                    )
                    try:
                        with urllib.request.urlopen(del_req, timeout=TIMEOUT):
                            pass
                    except urllib.error.HTTPError as exc:
                        if exc.code not in (404, 410):
                            raise
                    deleted += 1

    return {
        "ok": True,
        "inserted": inserted,
        "updated": updated,
        "deleted": deleted,
        "unchanged": unchanged,
    }


def sync_latest_from_gmail(
    state_path: Path | None = None,
    reconcile_calendar: bool = False,
    calendar_id: str = "primary",
) -> dict | None:
    """Fetch recent subject:\"[WORK-CAL-SYNC]\" messages from Gmail, ingest the newest valid payload,
    and archive/mark read all matching [WORK-CAL-SYNC] messages.
    """
    tok = _resolve_access_token()
    query = urllib.parse.quote('subject:"[WORK-CAL-SYNC]"')
    list_url = f"{GMAIL_BASE}/messages?q={query}&maxResults=20"
    req = urllib.request.Request(list_url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        listing = json.loads(resp.read().decode("utf-8"))

    messages = listing.get("messages", [])
    ingested_state: dict | None = None
    for m in messages:
        mid = m.get("id")
        if not mid:
            continue
        thread_id = m.get("threadId")
        if ingested_state is None:
            msg_url = f"{GMAIL_BASE}/messages/{mid}?format=full"
            msg_req = urllib.request.Request(msg_url, headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(msg_req, timeout=TIMEOUT) as msg_resp:
                m_data = json.loads(msg_resp.read().decode("utf-8"))
            if not thread_id:
                thread_id = m_data.get("threadId")
            payload = extract_work_cal_json_from_message(m_data)
            if payload:
                ingested_state = ingest_work_cal_payload(
                    payload,
                    state_path=state_path,
                    reconcile_calendar=reconcile_calendar,
                    calendar_id=calendar_id,
                    tok=tok,
                )
        archive_work_cal_message(mid, tok=tok, thread_id=thread_id)
    return ingested_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync [WORK-CAL-SYNC] payload from Gmail.")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Fetch the latest [WORK-CAL-SYNC] email from Gmail and persist to work_calendar_sync.json",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Reconcile work calendar events onto Google Calendar",
    )
    parser.add_argument(
        "--calendar-id",
        type=str,
        default="primary",
        help="Target Google Calendar ID (default: primary)",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=None,
        help="Optional override path for work_calendar_sync.json",
    )
    args = parser.parse_args(argv)

    if args.sync:
        state = sync_latest_from_gmail(
            state_path=args.state_path,
            reconcile_calendar=args.reconcile,
            calendar_id=args.calendar_id,
        )
        if state is None:
            print("[work_cal_sync] No valid [WORK-CAL-SYNC] message found in Gmail.", file=sys.stderr)
            return 1
        target = args.state_path or WORK_CAL_STATE_PATH
        reconcile_note = " (and reconciled to Google Calendar)" if args.reconcile else ""
        print(
            f"[work_cal_sync] Synced {len(state.get('events', []))} work event(s) "
            f"(generated_at={state.get('generated_at')}) -> {target}{reconcile_note}"
        )
        return 0

    if args.reconcile:
        res = reconcile_work_cal_to_google_calendar(
            state_path=args.state_path,
            calendar_id=args.calendar_id,
        )
        print(
            f"[work_cal_sync] Reconciled to {args.calendar_id}: "
            f"inserted={res.get('inserted', 0)} updated={res.get('updated', 0)} "
            f"deleted={res.get('deleted', 0)} unchanged={res.get('unchanged', 0)}"
        )
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
