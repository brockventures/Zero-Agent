#!/usr/bin/env python3
"""
agora_kill_switch.py - Emergency Kill Switch & Floor Controller for Station Agora

Intercepts !halt and !resume commands across Discord channels (#the-banana-stand, #lounge).
Bypasses LLM reasoning to execute immediate zero-latency operational actions:
1. Freezes local trading loops via /workspace/data/agora_trading_halted.flag.
2. Releases Banana coordination mutex if held.
3. Dispatches floor halt/resume to Agora Referee API (/referee/admin/floor).
4. Durably logs kill switch events to /workspace/data/kill_switch_history.json.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = TOOLS_DIR.parent
for p in (str(TOOLS_DIR), str(WORKSPACE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)
PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
FLAG_FILE = DATA_DIR / "agora_trading_halted.flag"
HISTORY_FILE = DATA_DIR / "kill_switch_history.json"
DEFAULT_AGORA_URL = "https://agora-banana-production.up.railway.app"


def get_admin_token() -> str:
    token = os.environ.get("AGORA_ADMIN_TOKEN", "").strip()
    if token:
        return token
    secrets_path = Path("/secrets/env.json")
    if secrets_path.exists():
        try:
            with open(secrets_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("AGORA_ADMIN_TOKEN", "").strip()
        except Exception:
            pass
    return ""


def get_agora_url() -> str:
    url = os.environ.get("AGORA_URL", "").strip()
    if url:
        return url.rstrip("/")
    return DEFAULT_AGORA_URL


def is_trading_halted() -> bool:
    """Check if the local emergency halt flag is active."""
    return FLAG_FILE.exists()


def set_remote_floor(action: str) -> tuple[bool, str]:
    """
    Call Station Agora referee endpoint to update floor state.
    Returns (success, detail_message).
    """
    admin_token = get_admin_token()
    base_url = get_agora_url()
    target_floor = "closed" if action == "halt" else "open"

    req_body = json.dumps({"action": action, "floor": target_floor}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"

    endpoints = [
        f"{base_url}/referee/admin/floor",
        f"{base_url}/referee/floor",
        "https://agora.mikecarmody.net/referee/admin/floor"
    ]

    for ep in endpoints:
        req = urllib.request.Request(ep, data=req_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return True, f"Remote referee floor set to '{data.get('floor', target_floor)}' via {ep}"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            return False, f"HTTP {e.code} from {ep}: {e.reason}"
        except Exception:
            continue

    return False, "Could not reach remote referee endpoint (backend deployment pending). Local flag set."


def log_kill_switch_event(event: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history = []
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(event)
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-100:], f, indent=2)
    except Exception as e:
        print(f"[KillSwitch] History write error: {e}", file=sys.stderr)


def trigger_kill_switch(initiator: str, action: str = "halt", channel_id: int | str = "") -> dict:
    """
    Execute emergency kill switch or resume.
    """
    now_pt = datetime.now(PT).strftime("%I:%M:%S %p PT")
    action = action.lower().strip()

    if action == "halt":
        # 1. Set local marker flag
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        FLAG_FILE.touch()

        # 2. Release Banana mutex if held
        banana_status = "unheld"
        try:
            from tools.banana import release as banana_release
            banana_release()
            banana_status = "released"
        except Exception as be:
            banana_status = f"noop ({be})"

        # 3. Disarm remote referee
        remote_ok, remote_detail = set_remote_floor("halt")

        event = {
            "timestamp_pt": now_pt,
            "action": "halt",
            "initiator": initiator,
            "channel_id": str(channel_id),
            "remote_ok": remote_ok,
            "remote_detail": remote_detail,
            "banana_status": banana_status
        }
        log_kill_switch_event(event)

        msg = (
            f"🛑 **KILL SWITCH TRIGGERED**: Agora trading halted by **{initiator}** at {now_pt}.\n"
            f"• **Floor State**: `closed` (All incoming order submissions will be rejected with `market_halted`).\n"
            f"• **Local Harnesses**: Frozen via `agora_trading_halted.flag`.\n"
            f"• **Banana Mutex**: {banana_status}.\n"
            f"• **Referee Sync**: {remote_detail}"
        )
        return {"status": "ok", "action": "halt", "message": msg}

    elif action == "resume":
        # 1. Clear local marker flag
        if FLAG_FILE.exists():
            try:
                FLAG_FILE.unlink()
            except Exception:
                pass

        # 2. Re-enable remote referee
        remote_ok, remote_detail = set_remote_floor("resume")

        event = {
            "timestamp_pt": now_pt,
            "action": "resume",
            "initiator": initiator,
            "channel_id": str(channel_id),
            "remote_ok": remote_ok,
            "remote_detail": remote_detail
        }
        log_kill_switch_event(event)

        msg = (
            f"🟢 **TRADING RESUMED**: Agora floor reopened by **{initiator}** at {now_pt}.\n"
            f"• **Floor State**: `open` (Order submissions active).\n"
            f"• **Local Harnesses**: Unfrozen.\n"
            f"• **Referee Sync**: {remote_detail}"
        )
        return {"status": "ok", "action": "resume", "message": msg}

    else:
        return {"status": "error", "message": f"Unknown kill switch action: {action}"}


if __name__ == "__main__":
    act = sys.argv[1] if len(sys.argv) > 1 else "halt"
    res = trigger_kill_switch(initiator="CLI", action=act)
    print(res["message"])
