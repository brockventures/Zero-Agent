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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WORK_CAL_SCHEMA = "zero.work_cal_sync.v1"
WORK_CAL_STATE_PATH = Path(os.environ.get("DATA_DIR", "/workspace/data")) / "work_calendar_sync.json"
DEFAULT_TZ = ZoneInfo("America/Los_Angeles")
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = 15

HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^<>]*)?>")
STYLE_SCRIPT_RE = re.compile(r"<style[\s\S]*?</style>|<script[\s\S]*?</script>", flags=re.IGNORECASE)


def _decode_b64url(data: str) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
    except Exception:
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
        except Exception:
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
            except Exception:
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
            except Exception:
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


def ingest_work_cal_payload(payload: dict, state_path: Path | None = None) -> dict:
    """Validate schema/events and atomically write normalized state to work_calendar_sync.json."""
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a dictionary.")
    if payload.get("schema") != WORK_CAL_SCHEMA:
        raise ValueError(f"Unsupported work calendar schema: {payload.get('schema')!r}")

    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("Payload 'events' must be a list.")

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
            uid_hash = hashlib.sha256(f"{summary}|{start}|{end}".encode("utf-8")).hexdigest()[:16]
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
        "ingested_at": datetime.now(timezone.utc).isoformat(),
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

    return state


def ingest_work_cal_message(m_data: dict, state_path: Path | None = None) -> bool:
    """Extract and ingest a [WORK-CAL-SYNC] payload from a Gmail message dictionary."""
    payload = extract_work_cal_json_from_message(m_data)
    if not payload:
        return False
    try:
        ingest_work_cal_payload(payload, state_path=state_path)
        return True
    except Exception:
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
    return dt.astimezone(timezone.utc)


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
    except Exception:
        return []

    if not isinstance(data, dict) or data.get("schema") != WORK_CAL_SCHEMA:
        return []

    tz_name = data.get("timezone") or "America/Los_Angeles"
    try:
        fallback_tz = ZoneInfo(tz_name)
    except Exception:
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
    if tok is None:
        repo_root = Path(__file__).resolve().parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        try:
            from tools.zero_mail_listener import get_access_token
        except ImportError:
            from zero_mail_listener import get_access_token
        tok = get_access_token()

    body = json.dumps({"removeLabelIds": ["INBOX", "UNREAD", "IMPORTANT"]}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }
    ok = False
    try:
        mod_url = f"{GMAIL_BASE}/messages/{mid}/modify"
        mod_req = urllib.request.Request(mod_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(mod_req, timeout=TIMEOUT) as resp:
            ok = resp.status in (200, 204) or True
    except Exception:
        return False

    if thread_id:
        try:
            t_url = f"{GMAIL_BASE}/threads/{thread_id}/modify"
            t_req = urllib.request.Request(t_url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(t_req, timeout=TIMEOUT):
                pass
        except Exception:
            pass

    return ok


def sync_latest_from_gmail(state_path: Path | None = None) -> dict | None:
    """Fetch recent subject:\"[WORK-CAL-SYNC]\" messages from Gmail, ingest the newest valid payload,
    and archive/mark read all matching [WORK-CAL-SYNC] messages.
    """
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from tools.zero_mail_listener import get_access_token
    except ImportError:
        from zero_mail_listener import get_access_token

    tok = get_access_token()
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
                ingested_state = ingest_work_cal_payload(payload, state_path=state_path)
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
        "--state-path",
        type=Path,
        default=None,
        help="Optional override path for work_calendar_sync.json",
    )
    args = parser.parse_args(argv)

    if args.sync:
        state = sync_latest_from_gmail(state_path=args.state_path)
        if state is None:
            print("[work_cal_sync] No valid [WORK-CAL-SYNC] message found in Gmail.", file=sys.stderr)
            return 1
        target = args.state_path or WORK_CAL_STATE_PATH
        print(
            f"[work_cal_sync] Synced {len(state.get('events', []))} work event(s) "
            f"(generated_at={state.get('generated_at')}) -> {target}"
        )
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
