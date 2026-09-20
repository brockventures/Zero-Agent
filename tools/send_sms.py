#!/usr/bin/env python3
"""send_sms.py - Outbound SMS / RCS Messenger via OpenMessage on Host 2.

Allows Zero to dispatch SMS/RCS messages to friends & family contacts
via the paired OpenMessage bridge running on Host 2 (.84).
"""

import sys
import os
import json
import re
import argparse
from pathlib import Path

# Add workspace to path
WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

try:
    from tools.sidecars import _ssh_cmd, _resolve_nas_config
except ImportError:
    from sidecars import _ssh_cmd, _resolve_nas_config

CSV_PATH = Path("/workspace/data/friends_and_family_master.csv")


def find_conversation_for_contact(target: str) -> tuple[str | None, str | None]:
    """Resolve a contact name, phone number, or conversation ID to (conv_id, label)."""
    _, host_2, _ = _resolve_nas_config()
    
    # 1. If numeric conversation ID, check directly
    if target.isdigit() and len(target) < 6:
        res = _ssh_cmd(host_2, f'docker exec openmessage openmessage thread {target} --limit 1 --json')
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout)
                return str(data.get("conversation_id", target)), data.get("label", target)
            except Exception:
                pass

    # 2. Look up phone number from contacts CSV if name was given
    phone_from_csv = None
    clean_target = target.strip().lower()
    if CSV_PATH.exists():
        import csv
        with open(CSV_PATH, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = row.get("Name", "").strip().lower()
                first = name.split()[0] if name else ""
                if clean_target in name or clean_target == first:
                    phone_from_csv = row.get("Phone Number", "").strip()
                    break

    # 3. Query OpenMessage threads for match
    queries = [target]
    if phone_from_csv:
        digits = re.sub(r"[^\d]", "", phone_from_csv)
        queries.insert(0, digits)
        queries.insert(1, phone_from_csv)

    for q in queries:
        res = _ssh_cmd(host_2, f'docker exec openmessage openmessage thread "{q}" --limit 1 --json')
        if res.returncode == 0 and res.stdout.strip():
            try:
                data = json.loads(res.stdout)
                matches = data.get("matches", [])
                if matches:
                    # Prefer 1-on-1 conversations over group chats if multiple matches
                    direct = [m for m in matches if " " not in m.get("name", "") or "," not in m.get("name", "")]
                    chosen = direct[0] if direct else matches[0]
                    return str(chosen.get("conversation_id")), chosen.get("name")
                elif data.get("conversation_id"):
                    return str(data.get("conversation_id")), data.get("label")
            except Exception:
                pass

    return None, None


def send_sms(target: str, message: str) -> tuple[bool, str]:
    """Send an SMS/RCS message to target (name, phone, or conversation ID)."""
    if not message.strip():
        return False, "Message cannot be empty."

    conv_id, label = find_conversation_for_contact(target)
    if not conv_id:
        return False, f"Could not find an existing conversation thread for '{target}'."

    _, host_2, _ = _resolve_nas_config()
    # Escape single quotes in message for remote shell execution
    escaped_msg = message.replace("'", "'\"'\"'")
    cmd = f"docker exec openmessage openmessage send {conv_id} '{escaped_msg}'"
    
    res = _ssh_cmd(host_2, cmd, timeout=25)
    if res.returncode == 0:
        return True, f"Sent to {label or target} (thread {conv_id}): \"{message}\""
    else:
        err = (res.stderr or res.stdout).strip()
        return False, f"OpenMessage send failed: {err}"


def main():
    parser = argparse.ArgumentParser(description="Send SMS/RCS via OpenMessage")
    parser.add_argument("target", help="Contact name, phone number, or conversation ID")
    parser.add_argument("message", help="Message body")
    args = parser.parse_args()

    ok, msg = send_sms(args.target, args.message)
    if ok:
        print(f"✅ {msg}")
        sys.exit(0)
    else:
        print(f"❌ {msg}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
