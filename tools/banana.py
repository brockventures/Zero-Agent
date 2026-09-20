#!/usr/bin/env python3
"""
banana.py - Turn-claim client for Crab Cavern multi-agent coordination.
Enforces mutual exclusion across peer bots (Amos, Marvin, Zero) via the Banana API.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
CREDS_FILE = Path("/workspace/data/banana_credentials.json")
HEALTH_FILE = Path("/workspace/data/banana_health.json")
STATE_FILE = Path("/workspace/data/banana_session_state.json")
DEFAULT_ENDPOINT = "https://banana.mikecarmody.net/api"
DEFAULT_LEASE_TTL_SECONDS = 120

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

def _record_health(status: str, error_msg: str = "", holder: str | None = None) -> None:
    """Record Banana API health state for sidecars and observability."""
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        if HEALTH_FILE.exists():
            try:
                with open(HEALTH_FILE, "r") as f:
                    current = json.load(f)
            except Exception:
                current = {}

        now_pt = datetime.now(PT)
        consec_errors = 0 if status in ("healthy", "blocked") else current.get("consecutive_errors", 0) + 1
        
        health_data = {
            "status": status,  # healthy, blocked, degraded, error
            "last_check_pt": now_pt.strftime("%Y-%m-%d %I:%M:%S %p PT"),
            "last_check_iso": now_pt.isoformat(),
            "consecutive_errors": consec_errors,
            "last_error": error_msg[:300] if error_msg else "",
            "holder": holder
        }
        with open(HEALTH_FILE, "w") as f:
            json.dump(health_data, f, indent=2)
    except Exception:
        pass

def get_status() -> dict:
    """Check whether the floor is free right now. Auth: none."""
    creds = load_credentials() if CREDS_FILE.exists() else {}
    endpoint = creds.get("endpoint", DEFAULT_ENDPOINT)
    req = urllib.request.Request(f"{endpoint}/status", headers={"User-Agent": "ZeroBananaClient/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            _record_health("healthy", holder=data.get("holder"))
            return data
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="ignore")
        err_msg = f"HTTP {e.code}: {err_body[:100].strip() or e.reason}"
        _record_health("degraded", error_msg=err_msg)
        print(f"[Banana] ⚠️ Banana Mutex API degraded on GET /status: {err_msg}", file=sys.stderr)
        raise BananaError(err_msg)
    except Exception as e:
        err_msg = str(e)
        _record_health("degraded", error_msg=err_msg)
        print(f"[Banana] ⚠️ Banana Mutex API connection failure on GET /status: {err_msg}", file=sys.stderr)
        raise BananaError(err_msg)

def is_free() -> bool:
    """Return True if holder is null (free, released, or expired)."""
    try:
        status = get_status()
        return status.get("holder") is None
    except Exception:
        return False

def check_health() -> dict:
    """Retrieve current or cached Banana API health status."""
    try:
        status = get_status()
        return {
            "status": "healthy",
            "free": status.get("holder") is None,
            "holder": status.get("holder"),
            "subject": status.get("subject"),
            "consecutive_errors": 0
        }
    except BananaError as be:
        cached = {}
        if HEALTH_FILE.exists():
            try:
                with open(HEALTH_FILE, "r") as f:
                    cached = json.load(f)
            except Exception:
                pass
        return {
            "status": "degraded",
            "error": str(be),
            "consecutive_errors": cached.get("consecutive_errors", 1),
            "last_check_pt": cached.get("last_check_pt")
        }

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass

def _clear_state() -> None:
    try:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
    except Exception:
        pass

def claim(subject: str = "") -> dict:
    """Claim the floor before posting. Returns dict or raises BananaBlockedError / BananaError."""
    creds = load_credentials()
    endpoint = creds.get("endpoint", DEFAULT_ENDPOINT)
    token = creds.get("token")
    holder = creds.get("holder", "zero")

    saved_state = _load_state()
    effective_subject = subject or saved_state.get("subject", "")

    data = json.dumps({"holder": holder, "subject": effective_subject}).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/claim",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ZeroBananaClient/1.0"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            # Extract generation fencing token
            state = body.get("state") if isinstance(body.get("state"), dict) else {}
            gen = body.get("generation") or state.get("id") or body.get("id")
            ret_subj = body.get("subject") or effective_subject
            _save_state({"generation": gen, "subject": ret_subj})
            _record_health("healthy", holder=holder)
            return body
    except urllib.error.HTTPError as e:
        body = {}
        try:
            raw = e.read().decode(errors="ignore")
            if e.headers.get_content_type() == "application/json":
                body = json.loads(raw)
            else:
                body = {"raw": raw}
        except Exception:
            body = {}

        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = body.get("code") or err.get("code")
        if e.code == 409 and (code == "blocked" or "blocked" in str(body).lower()):
            blocked_holder = body.get("holder") or err.get("holder") or (body.get("state") or {}).get("holder") or "unknown"
            _record_health("blocked", holder=blocked_holder)
            raise BananaBlockedError(blocked_holder, body.get("state", {}))

        err_detail = err.get("message") or body.get("error") or body.get("code") or body.get("raw") or e.reason
        err_msg = f"HTTP {e.code}: {str(err_detail).strip()}"
        _record_health("degraded", error_msg=err_msg)
        print(f"[Banana] ⚠️ Banana Mutex claim failed with server error: {err_msg}", file=sys.stderr)
        raise BananaError(err_msg)
    except Exception as e:
        err_msg = str(e)
        _record_health("degraded", error_msg=err_msg)
        print(f"[Banana] ⚠️ Banana Mutex claim network/connection failure: {err_msg}", file=sys.stderr)
        raise BananaError(err_msg)

def release() -> dict:
    """Release the floor when done. Returns dict."""
    creds = load_credentials()
    endpoint = creds.get("endpoint", DEFAULT_ENDPOINT)
    token = creds.get("token")
    holder = creds.get("holder", "zero")

    saved_state = _load_state()
    payload = {"holder": holder}
    if saved_state.get("generation") is not None:
        payload["generation"] = saved_state["generation"]

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/release",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ZeroBananaClient/1.0"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            _clear_state()
            _record_health("healthy", holder=None)
            return body
    except urllib.error.HTTPError as e:
        body = {}
        try:
            raw = e.read().decode(errors="ignore")
            if e.headers.get_content_type() == "application/json":
                body = json.loads(raw)
            else:
                body = {"raw": raw}
        except Exception:
            body = {}
        err_msg = f"HTTP {e.code}: {body.get('error') or body.get('code') or body.get('raw') or e.reason}"
        _record_health("degraded", error_msg=err_msg)
        print(f"[Banana] ⚠️ Banana Mutex release server error: {err_msg}", file=sys.stderr)
        raise BananaError(err_msg)
    except Exception as e:
        err_msg = str(e)
        _record_health("degraded", error_msg=err_msg)
        print(f"[Banana] ⚠️ Banana Mutex release connection error: {err_msg}", file=sys.stderr)
        raise BananaError(err_msg)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(get_status(), indent=2))
    elif cmd == "free":
        print("Free:", is_free())
    elif cmd == "health":
        print(json.dumps(check_health(), indent=2))
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
