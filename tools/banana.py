#!/usr/bin/env python3
"""
banana.py - Turn-claim client for Crab Cavern multi-agent coordination.
Enforces mutual exclusion across peer bots (Amos, Marvin, Zero) via the Banana API.
"""

import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

CREDS_FILE = Path("/workspace/data/banana_credentials.json")
DEFAULT_ENDPOINT = "https://banana.mikecarmody.net/api"

class BananaError(Exception):
    pass

class BananaBlockedError(BananaError):
    def __init__(self, current_holder: str, state: dict):
        super().__init__(f"Floor is currently claimed by '{current_holder}'")
        self.current_holder = current_holder
        self.state = state

def load_credentials() -> dict:
    if not CREDS_FILE.exists():
        raise FileNotFoundError(f"Banana credentials not found at {CREDS_FILE}")
    with open(CREDS_FILE, "r") as f:
        return json.load(f)

def get_status() -> dict:
    """Check whether the floor is free right now. Auth: none."""
    creds = load_credentials() if CREDS_FILE.exists() else {}
    endpoint = creds.get("endpoint", DEFAULT_ENDPOINT)
    req = urllib.request.Request(f"{endpoint}/status")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

def is_free() -> bool:
    """Return True if holder is null (free, released, or expired)."""
    try:
        status = get_status()
        return status.get("holder") is None
    except Exception:
        return False

def claim(subject: str = "") -> dict:
    """Claim the floor before posting. Returns dict or raises BananaBlockedError."""
    creds = load_credentials()
    endpoint = creds.get("endpoint", DEFAULT_ENDPOINT)
    token = creds.get("token")
    holder = creds.get("holder", "zero")

    data = json.dumps({"holder": holder, "subject": subject}).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/claim",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode()) if e.headers.get_content_type() == "application/json" else {}
        if e.code == 409 and body.get("code") == "blocked":
            raise BananaBlockedError(body.get("holder", "unknown"), body.get("state", {}))
        raise BananaError(f"HTTP {e.code}: {body.get('error') or body.get('code') or e.reason}")

def release() -> dict:
    """Release the floor when done. Returns dict."""
    creds = load_credentials()
    endpoint = creds.get("endpoint", DEFAULT_ENDPOINT)
    token = creds.get("token")
    holder = creds.get("holder", "zero")

    data = json.dumps({"holder": holder}).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/release",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode()) if e.headers.get_content_type() == "application/json" else {}
        raise BananaError(f"HTTP {e.code}: {body.get('error') or body.get('code') or e.reason}")

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(get_status(), indent=2))
    elif cmd == "free":
        print("Free:", is_free())
    elif cmd == "test":
        print("1. Status:", get_status().get("holder"))
        print("2. Claiming...")
        c = claim("zero self-test")
        print("Claimed:", c.get("ok"))
        print("3. Status now:", get_status().get("holder"))
        print("4. Releasing...")
        r = release()
        print("Released:", r.get("ok"))
        print("5. Status after release:", get_status().get("holder"))
