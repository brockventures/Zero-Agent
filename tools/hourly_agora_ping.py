#!/usr/bin/env python3
"""
hourly_agora_ping.py - Automated hourly standup check-in for Crab Cavern.
Sends 'What's next?' to Amos and Aerial on #the-banana-stand every hour on the hour.
"""

import sys
import argparse
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from outbox import queue_outbox_message, flush_pending_messages, dispatch_via_rest

MESSAGE_TEXT = """🍌 What's next <@1468012353206354197> <@1542035925603713086>?

```handoff
{
  "v": 1.1,
  "kind": "ask",
  "reply": "required",
  "floor": "open",
  "scope": "channel",
  "subject": "agora-status",
  "to": ["amos", "aerial"]
}
```"""

def dispatch_hourly_agora_ping(dry_run: bool = False) -> tuple[bool, str, dict]:
    """Dispatch the hourly what's next ping to #the-banana-stand."""
    if dry_run:
        return True, f"[Dry Run] Would send hourly ping to #the-banana-stand:\n{MESSAGE_TEXT}", {"dry_run": True}
    
    try:
        q_msg = queue_outbox_message(
            channel="the-banana-stand",
            content=MESSAGE_TEXT,
            source_turn="hourly-agora-ping"
        )
        pending = flush_pending_messages()
        dispatched = []
        for m in pending:
            ok = dispatch_via_rest(m)
            dispatched.append((m.get("id"), ok))
        
        return True, f"Dispatched hourly ping to #the-banana-stand (queue_id={q_msg.get('id')})", {"dispatched": dispatched}
    except Exception as e:
        return False, f"Failed to dispatch hourly ping: {e}", {"error": str(e)}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hourly Agora standup ping")
    parser.add_argument("--test", "--dry-run", action="store_true", help="Dry run without sending to Discord")
    args = parser.parse_args()
    
    ok, rep, _ = dispatch_hourly_agora_ping(dry_run=args.test)
    print(rep)
    sys.exit(0 if ok else 1)
