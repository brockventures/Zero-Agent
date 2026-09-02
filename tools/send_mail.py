#!/usr/bin/env python3
"""Zero Outbound Email Dispatch Utility (send_mail.py).

Standardized CLI and Python helper for sending emails as Zero with attachment support.
- Default Sender: Zero configured sender address
- Mandatory CC: Owner notification address (automatically enforced on all external recipients)
- Supports direct sending, draft staging, and file attachments
- 25MB Pre-flight Size Guardrail & Directory Auto-Zipping
- Preserves Gmail thread context (In-Reply-To and References RFC headers)
"""

import argparse
import base64
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional, Union

# Ensure tools dir is in path
sys.path.insert(0, "/workspace/tools")
try:
    import workspace_mcp
    DEFAULT_FROM = getattr(workspace_mcp, "DEFAULT_FROM", "")
    DEFAULT_CC = getattr(workspace_mcp, "DEFAULT_CC", "")
except ImportError:
    DEFAULT_FROM = os.environ.get("ZERO_SENDER_EMAIL", "")
    DEFAULT_CC = os.environ.get("ZERO_NOTIFICATION_EMAIL", "")

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024  # 24MB safe threshold under Gmail 25MB limit
TIMEOUT = 30


def get_auth_headers() -> dict:
    """Retrieve Bearer auth headers from workspace_mcp credentials."""
    import workspace_mcp
    return workspace_mcp._auth_headers()


def send_email(
    to: str,
    subject: str,
    body: str,
    thread_id: str = "",
    from_email: str = DEFAULT_FROM,
    cc: str = "",
    attachments: Optional[List[Union[str, Path]]] = None,
    is_draft: bool = False
) -> dict:
    """Dispatch or draft an email via Gmail API, supporting attachments."""
    if not to or not subject:
        raise ValueError("'to' and 'subject' are required.")
    if body is None:
        body = ""

    # Enforce Ryan CC policy
    effective_cc = cc.strip() if cc else ""
    if DEFAULT_CC not in effective_cc and DEFAULT_CC not in to:
        effective_cc = f"{effective_cc}, {DEFAULT_CC}".strip(", ") if effective_cc else DEFAULT_CC

    msg = EmailMessage()
    msg["To"] = to
    if effective_cc:
        msg["Cc"] = effective_cc
    msg["Subject"] = subject
    msg["From"] = from_email or DEFAULT_FROM
    msg.set_content(body)

    # Process and attach files
    attached_files = []
    total_attachment_bytes = 0

    if attachments:
        for item in attachments:
            path = Path(item)
            if not path.exists():
                raise FileNotFoundError(f"Attachment target not found: {path}")

            # Auto-zip directories
            if path.is_dir():
                temp_dir = tempfile.mkdtemp(prefix="zero_zip_")
                zip_base = Path(temp_dir) / path.name
                archive_path = Path(shutil.make_archive(str(zip_base), "zip", root_dir=str(path)))
                filename = archive_path.name
                file_data = archive_path.read_bytes()
                ctype = "application/zip"
            else:
                filename = path.name
                file_data = path.read_bytes()
                ctype, encoding = mimetypes.guess_type(str(path))
                if ctype is None or encoding is not None:
                    if path.suffix == ".py":
                        ctype = "text/x-python"
                    elif path.suffix == ".md":
                        ctype = "text/markdown"
                    elif path.suffix == ".json":
                        ctype = "application/json"
                    else:
                        ctype = "application/octet-stream"

            total_attachment_bytes += len(file_data)
            if total_attachment_bytes > MAX_ATTACHMENT_BYTES:
                raise ValueError(
                    f"Total attachment size ({total_attachment_bytes / (1024*1024):.2f}MB) exceeds safe 24MB Gmail limit."
                )

            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(
                file_data,
                maintype=maintype,
                subtype=subtype,
                filename=filename
            )
            attached_files.append({"filename": filename, "bytes": len(file_data), "path": str(path)})

    headers = get_auth_headers()

    msg_payload = {}
    if thread_id:
        msg_payload["threadId"] = thread_id
        # Attempt to retrieve parent Message-ID for RFC header compliance
        try:
            t_req = urllib.request.Request(
                f"{GMAIL_BASE}/threads/{thread_id}?format=metadata&metadataHeaders=Message-Id&metadataHeaders=Message-ID",
                headers=headers
            )
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

    endpoint = f"{GMAIL_BASE}/drafts" if is_draft else f"{GMAIL_BASE}/messages/send"
    post_payload = {"message": msg_payload} if is_draft else msg_payload
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(post_payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
        action_type = "Draft created" if is_draft else "Email sent"
        return {
            "ok": True,
            "action": action_type,
            "id": data.get("id"),
            "threadId": data.get("threadId"),
            "to": to,
            "cc": effective_cc,
            "from": msg["From"],
            "subject": subject,
            "attachments": attached_files
        }


def main():
    parser = argparse.ArgumentParser(description="Zero Outbound Email Dispatch CLI (with Attachments & 25MB Guard)")
    parser.add_argument("--to", "-t", required=True, help="Recipient email address")
    parser.add_argument("--subject", "-s", required=True, help="Email subject")
    parser.add_argument("--body", "-b", help="Email body text")
    parser.add_argument("--body-file", "-f", help="Path to file containing email body")
    parser.add_argument("--attach", "-a", action="append", default=[], help="File or directory path to attach (can specify multiple)")
    parser.add_argument("--thread-id", help="Optional Gmail Thread ID to reply into")
    parser.add_argument("--cc", default="", help=f"Additional CC recipients (defaults to {DEFAULT_CC})")
    parser.add_argument("--from", dest="from_email", default=DEFAULT_FROM, help=f"Sender address (defaults to {DEFAULT_FROM})")
    parser.add_argument("--draft", action="store_true", help="Stage as draft instead of sending immediately")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    elif not body:
        if not sys.stdin.isatty():
            body = sys.stdin.read()
        else:
            body = ""

    try:
        res = send_email(
            to=args.to,
            subject=args.subject,
            body=body,
            thread_id=args.thread_id or "",
            from_email=args.from_email,
            cc=args.cc,
            attachments=args.attach,
            is_draft=args.draft
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            status_icon = "📝" if args.draft else "🚀"
            print(f"{status_icon} {res['action']} successfully!")
            print(f"  • ID: {res['id']}")
            print(f"  • Thread: {res['threadId']}")
            print(f"  • To: {res['to']}")
            print(f"  • From: {res['from']}")
            print(f"  • CC: {res['cc']}")
            print(f"  • Subject: {res['subject']}")
            if res.get("attachments"):
                print("  • Attachments:")
                for att in res["attachments"]:
                    print(f"    - {att['filename']} ({att['bytes']} bytes)")
    except Exception as e:
        err = {"ok": False, "error": str(e)}
        if args.json:
            print(json.dumps(err, indent=2))
        else:
            print(f"❌ Failed to dispatch email: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
