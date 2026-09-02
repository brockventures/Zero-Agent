#!/usr/bin/env python3
"""Google Workspace (Gmail & Calendar) MCP Server for Ivy-AG.

Authenticates via /secrets/google_oauth.json using stored OAuth refresh token.
Token refreshes are cached in-memory and renewed automatically.
Account: user@example.com
"""

import json
import os
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from mcp.server.mcpserver import MCPServer

SECRETS_PATH = "/workspace/config/google_oauth.json" if os.path.exists("/workspace/config/google_oauth.json") else os.environ.get("GOOGLE_OAUTH_PATH", os.environ.get("GOOGLE_OAUTH_CREDENTIALS", "/secrets/google_oauth.json"))
ACCOUNT = os.environ.get("GOOGLE_ACCOUNT", "user@example.com")
PT = ZoneInfo("America/Los_Angeles")
TIMEOUT = 30

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

_cached_token = None
_token_expiry = 0

server = MCPServer("google-workspace")

def load_credentials(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        creds = {}
        for line in content.splitlines():
            line = line.strip().rstrip(",")
            if ":" in line:
                k, v = line.split(":", 1)
                creds[k.strip().strip("\"").strip("'")] = v.strip().strip("\"").strip("'")
        return creds

def _get_access_token() -> str:
    global _cached_token, _token_expiry
    now = time.time()
    if _cached_token and now < _token_expiry - 60:
        return _cached_token

    if not os.path.exists(SECRETS_PATH):
        raise FileNotFoundError(f"OAuth credentials not found at {SECRETS_PATH}")

    creds = load_credentials(SECRETS_PATH)

    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request(creds.get("token_uri", "https://oauth2.googleapis.com/token"), data=data)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        tokens = json.loads(resp.read().decode())
        _cached_token = tokens["access_token"]
        expires_in = tokens.get("expires_in", 3600)
        _token_expiry = now + expires_in
        return _cached_token

def _auth_headers() -> dict:
    tok = _get_access_token()
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json"
    }

@server.tool()
def gmail_search(query: str = "is:unread", max_results: int = 15) -> str:
    """Search Gmail messages. Returns snippet, id, threadId."""
    try:
        params = urllib.parse.urlencode({
            "q": query,
            "maxResults": min(max(1, max_results), 50)
        })
        req = urllib.request.Request(f"{GMAIL_BASE}/messages?{params}", headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            messages = data.get("messages", [])
            results = []
            for m in messages:
                # Fetch snippet and headers for each message
                m_req = urllib.request.Request(f"{GMAIL_BASE}/messages/{m['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date", headers=_auth_headers())
                with urllib.request.urlopen(m_req, timeout=TIMEOUT) as m_resp:
                    m_data = json.loads(m_resp.read().decode())
                    headers = {h["name"]: h["value"] for h in m_data.get("payload", {}).get("headers", [])}
                    results.append({
                        "id": m["id"],
                        "threadId": m.get("threadId"),
                        "subject": headers.get("Subject", "(no subject)"),
                        "from": headers.get("From", "(unknown)"),
                        "date": headers.get("Date", ""),
                        "snippet": m_data.get("snippet", "")
                    })
            return json.dumps({"ok": True, "count": len(results), "messages": results})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@server.tool()
def gmail_get_thread(thread_id: str) -> str:
    """Fetch full Gmail thread content by thread_id."""
    if not thread_id:
        return json.dumps({"ok": False, "error": "thread_id required"})
    try:
        req = urllib.request.Request(f"{GMAIL_BASE}/threads/{thread_id}", headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            messages = []
            for m in data.get("messages", []):
                headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
                messages.append({
                    "id": m["id"],
                    "from": headers.get("From", ""),
                    "subject": headers.get("Subject", ""),
                    "date": headers.get("Date", ""),
                    "snippet": m.get("snippet", "")
                })
            return json.dumps({"ok": True, "thread_id": thread_id, "messages": messages})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

def _get_email_defaults() -> tuple[str, str]:
    sender = os.environ.get("ZERO_SENDER_EMAIL", os.environ.get("ZERO_EMAIL", ""))
    notify = os.environ.get("ZERO_NOTIFICATION_EMAIL", os.environ.get("OWNER_EMAIL", ""))
    if not sender or not notify:
        env_path = Path("/workspace/.env")
        if env_path.exists():
            try:
                for line in env_path.read_text().splitlines():
                    if line.startswith("ZERO_EMAIL=") and not sender:
                        sender = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("OWNER_EMAIL=") and not notify:
                        notify = line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    if not notify:
        try:
            priv_prof = Path("/workspace/memory/private/user_ryan.md")
            if priv_prof.exists():
                m = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", priv_prof.read_text())
                if m:
                    notify = m.group(0)
        except Exception:
            pass
    from_header = f"Zero <{sender}>" if sender and "<" not in sender else sender
    return from_header, notify

DEFAULT_FROM, DEFAULT_CC = _get_email_defaults()

@server.tool()
def gmail_create_draft(to: str, subject: str, body: str, thread_id: str = "", from_email: str = DEFAULT_FROM, cc: str = "", attachments: list[str] = None) -> str:
    """Create a draft email in Gmail (safe default: does not send without confirmation)."""
    if not to or not subject:
        return json.dumps({"ok": False, "error": "to and subject required"})
    try:
        import base64
        import mimetypes
        from pathlib import Path
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["To"] = to
        effective_cc = cc.strip() if cc else ""
        if DEFAULT_CC not in effective_cc and DEFAULT_CC not in to:
            effective_cc = f"{effective_cc}, {DEFAULT_CC}".strip(", ") if effective_cc else DEFAULT_CC
        if effective_cc:
            msg["Cc"] = effective_cc
        msg["Subject"] = subject
        msg["From"] = from_email or DEFAULT_FROM
        msg.set_content(body or "")

        if attachments:
            for item in attachments:
                path = Path(item)
                if path.is_file():
                    ctype, encoding = mimetypes.guess_type(str(path))
                    if ctype is None or encoding is not None:
                        ctype = "text/x-python" if path.suffix == ".py" else ("text/markdown" if path.suffix == ".md" else "application/octet-stream")
                    maintype, subtype = ctype.split("/", 1)
                    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

        msg_payload = {"raw": ""}
        if thread_id:
            msg_payload["threadId"] = thread_id
            # Try fetching parent message to set proper RFC in-reply-to headers
            try:
                t_req = urllib.request.Request(f"{GMAIL_BASE}/threads/{thread_id}?format=metadata&metadataHeaders=Message-Id&metadataHeaders=Message-ID", headers=_auth_headers())
                with urllib.request.urlopen(t_req, timeout=TIMEOUT) as t_resp:
                    t_data = json.loads(t_resp.read().decode())
                    messages = t_data.get("messages", [])
                    if messages:
                        parent = messages[-1]
                        hdrs = {h["name"].lower(): h["value"] for h in parent.get("payload", {}).get("headers", [])}
                        parent_msg_id = hdrs.get("message-id")
                        if parent_msg_id:
                            msg["In-Reply-To"] = parent_msg_id
                            msg["References"] = parent_msg_id
            except Exception:
                pass

        msg_payload["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = json.dumps({"message": msg_payload}).encode("utf-8")
        req = urllib.request.Request(f"{GMAIL_BASE}/drafts", data=payload, headers=_auth_headers(), method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return json.dumps({"ok": True, "draft_id": data.get("id"), "message": f"Draft created for {to}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@server.tool()
def gmail_send_message(to: str, subject: str, body: str, thread_id: str = "", from_email: str = DEFAULT_FROM, cc: str = "", attachments: list[str] = None) -> str:
    """Send an email directly via Gmail."""
    if not to or not subject:
        return json.dumps({"ok": False, "error": "to and subject required"})
    try:
        import base64
        import mimetypes
        from pathlib import Path
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["To"] = to
        effective_cc = cc.strip() if cc else ""
        if DEFAULT_CC not in effective_cc and DEFAULT_CC not in to:
            effective_cc = f"{effective_cc}, {DEFAULT_CC}".strip(", ") if effective_cc else DEFAULT_CC
        if effective_cc:
            msg["Cc"] = effective_cc
        msg["Subject"] = subject
        msg["From"] = from_email or DEFAULT_FROM
        msg.set_content(body or "")

        if attachments:
            for item in attachments:
                path = Path(item)
                if path.is_file():
                    ctype, encoding = mimetypes.guess_type(str(path))
                    if ctype is None or encoding is not None:
                        ctype = "text/x-python" if path.suffix == ".py" else ("text/markdown" if path.suffix == ".md" else "application/octet-stream")
                    maintype, subtype = ctype.split("/", 1)
                    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

        msg_payload = {}
        if thread_id:
            msg_payload["threadId"] = thread_id
            try:
                t_req = urllib.request.Request(f"{GMAIL_BASE}/threads/{thread_id}?format=metadata&metadataHeaders=Message-Id&metadataHeaders=Message-ID", headers=_auth_headers())
                with urllib.request.urlopen(t_req, timeout=TIMEOUT) as t_resp:
                    t_data = json.loads(t_resp.read().decode())
                    messages = t_data.get("messages", [])
                    if messages:
                        parent = messages[-1]
                        hdrs = {h["name"].lower(): h["value"] for h in parent.get("payload", {}).get("headers", [])}
                        parent_msg_id = hdrs.get("message-id")
                        if parent_msg_id:
                            msg["In-Reply-To"] = parent_msg_id
                            msg["References"] = parent_msg_id
            except Exception:
                pass

        msg_payload["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = json.dumps(msg_payload).encode("utf-8")
        req = urllib.request.Request(f"{GMAIL_BASE}/messages/send", data=payload, headers=_auth_headers(), method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return json.dumps({"ok": True, "message_id": data.get("id"), "thread_id": data.get("threadId"), "message": f"Email sent successfully to {to}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

FAMILY_CALENDARS = {
    "primary": "Ryan",
    "contact@example.com": "Emily",
    "family05249951047154432652@group.calendar.google.com": "Family",
    "8ppu6rut9gsr2r29ljuivh5sk0@group.calendar.google.com": "Home",
    "e0398ec5e0eb506519aa935c582e66c533fe9c77c8ff5111bb41786b6f170190@group.calendar.google.com": "Nanny Share",
    "c_f713b3055b57e1d48ca0962e78773a57b609cad582b1bf714d84b0b8c8af0e2c@group.calendar.google.com": "Roy Cloud PTO",
}

@server.tool()
def calendar_list_calendars() -> str:
    """List all accessible Google Calendars (primary, shared, family)."""
    try:
        req = urllib.request.Request(f"{CALENDAR_BASE}/users/me/calendarList", headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            cals = []
            for item in data.get("items", []):
                cals.append({
                    "id": item.get("id"),
                    "summary": item.get("summary"),
                    "primary": item.get("primary", False),
                    "access_role": item.get("accessRole")
                })
            return json.dumps({"ok": True, "count": len(cals), "calendars": cals})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

@server.tool()
def calendar_list_events(calendar_id: str = "primary", time_min_iso: str = "", time_max_iso: str = "", max_results: int = 30) -> str:
    """List Google Calendar events. calendar_id can be 'primary', 'all' (sweeps Ryan, Emily, family, home, nanny share), or a specific calendar ID."""
    try:
        now = datetime.now(timezone.utc)
        if not time_min_iso:
            time_min_iso = now.isoformat()
        if not time_max_iso:
            from datetime import timedelta
            time_max_iso = (now + timedelta(days=1)).isoformat()

        cals_to_query = []
        if calendar_id.lower() == "all":
            cals_to_query = list(FAMILY_CALENDARS.items())
        else:
            cid = calendar_id if calendar_id else "primary"
            cname = FAMILY_CALENDARS.get(cid, cid)
            cals_to_query = [(cid, cname)]

        all_events = []
        seen_events = set()

        for cid, cname in cals_to_query:
            encoded_id = urllib.parse.quote(cid)
            params = urllib.parse.urlencode({
                "timeMin": time_min_iso,
                "timeMax": time_max_iso,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": min(max(1, max_results), 50)
            })
            req = urllib.request.Request(f"{CALENDAR_BASE}/calendars/{encoded_id}/events?{params}", headers=_auth_headers())
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    data = json.loads(resp.read().decode())
                    for ev in data.get("items", []):
                        start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
                        end = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")
                        summary = ev.get("summary", "(no title)")
                        ev_key = (summary, start)
                        if ev_key in seen_events:
                            continue
                        seen_events.add(ev_key)
                        all_events.append({
                            "id": ev.get("id"),
                            "calendar": cname,
                            "summary": summary,
                            "start": start,
                            "end": end,
                            "location": ev.get("location", ""),
                            "description": ev.get("description", "")
                        })
            except Exception as ce:
                print(f"[Calendar] Warning fetching {cid}: {ce}")

        all_events.sort(key=lambda x: x.get("start", ""))
        return json.dumps({"ok": True, "count": len(all_events), "events": all_events[:max_results]})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})

if __name__ == "__main__":
    import sys
    port = 8765
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=")[1])
        elif arg == "--port" and sys.argv.index(arg) + 1 < len(sys.argv):
            port = int(sys.argv[sys.argv.index(arg) + 1])
    if "--sse" in sys.argv:
        server.run(transport="sse", host="127.0.0.1", port=port)
    else:
        server.run(transport="stdio")

