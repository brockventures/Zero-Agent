#!/usr/bin/env python3
"""
outbox.py - Decoupled Cross-Channel Asynchronous Message Dispatch
Implements Marvin's atomic JSONL queue pattern (data/outbox/pending.jsonl)
so Zero can dispatch messages to other channels (#lounge, #the-banana-stand, #zero-chat)
without blocking the active turn or coupling delivery to the current execution thread.
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

DATA_DIR = Path("/workspace/data")
OUTBOX_DIR = DATA_DIR / "outbox"
PENDING_FILE = OUTBOX_DIR / "pending.jsonl"

KNOWN_CHANNELS = {
    "the-banana-stand": 1534436119888793750,
    "agent-chat": 1534436119888793750,  # alias for backward compatibility
    "lounge": 1534452820995080192,
    "zero-chat": 1542081375287640084,
    "steam-deck": 1544953277592899615,
    "home-assistant": 1544953275877556334,
    "finances": 1544955532765560924,
    "homelab": 1544955535722545253,
    "shopping": 1544955538033348618,
    "zero-ops": 1544953279664889888,
    "harness-management": 1544953279664889888,
    "general": 1534452820995080192,  # alias to lounge or main
    "signals": 1534436119888793750,
    "staff-comms": 1534436119888793750,
    "server-updates": 1330447543477338202,
    "baseball": 1548196929308065893,
    "projects": 1548196930788524094,
    "brock-house": 1550577908811178095,
    "vault": 1550577910757458015,
}

def resolve_channel(channel_input: str | int) -> tuple[str, int | None]:
    """Resolve channel name or ID to (clean_name, channel_id)."""
    ch_str = str(channel_input).strip().lstrip("#")
    if ch_str.isdigit():
        ch_id = int(ch_str)
        # Find reverse name
        for name, cid in KNOWN_CHANNELS.items():
            if cid == ch_id:
                return name, ch_id
        return f"channel-{ch_id}", ch_id
    
    clean_name = ch_str.lower()
    ch_id = KNOWN_CHANNELS.get(clean_name)
    return clean_name, ch_id

def queue_outbox_message(channel: str, content: str, source_turn: str = "zero") -> dict:
    """
    Queue a message for asynchronous delivery to another Discord channel.
    Uses atomic append to durable pending.jsonl queue.
    """
    clean_name, ch_id = resolve_channel(channel)
    content = content.strip()

    # Strip reaction GIFs if target channel has them disabled
    try:
        from tools.bridge_state import is_gif_disabled_for_channel
        from tools.bridge_formatting import strip_reaction_gifs
        if is_gif_disabled_for_channel(clean_name, channel_id=ch_id):
            content = strip_reaction_gifs(content)
    except Exception:
        pass

    if not content:
        raise ValueError("Message content cannot be empty.")
    if len(content) > 10000:
        raise ValueError(f"Message content exceeds 10,000 character ceiling (len={len(content)}).")

    msg_record = {
        "id": f"outbox-{int(time.time()*1000)}-{os.getpid()}",
        "channel": clean_name,
        "channel_id": ch_id,
        "content": content,
        "source": source_turn,
        "created_at": time.time(),
        "created_at_iso": datetime.now(timezone.utc).isoformat()
    }

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    
    # Atomic write to line
    with open(PENDING_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(msg_record) + "\n")

    return msg_record

def dispatch_proposal_notice(
    subject: str,
    agreed_with: str,
    action_type: str,
    details: str,
    task_id: int | None = None
) -> dict:
    """
    Format and queue a synthesized proposal/consensus executive notice to #zero-chat.
    Ensures post-consensus decisions from Crab Cavern are readable and context-rich.
    """
    is_shipped = action_type.lower() in ("shipped", "deployed", "completed", "live")
    clean_title = subject.replace("-", " ").replace("_", " ").title()
    header = f"📦 **Crab Cavern: {clean_title} Shipped**" if is_shipped else f"📋 **Crab Cavern Decision: {clean_title}**"
    
    parts = [header, details.strip()]
    metadata_bits = []
    if task_id:
        metadata_bits.append(f"Task #{task_id}")
    if agreed_with:
        metadata_bits.append(f"Aligned with {agreed_with}")
    if action_type and action_type.lower() not in ("consensus_reached", "shipped", "approved"):
        metadata_bits.append(f"Status: {action_type}")
        
    if metadata_bits:
        parts.append(f"*(Tracking: {', '.join(metadata_bits)})*")
        
    msg = "\n\n".join(parts)
    return queue_outbox_message("zero-chat", msg, source_turn="crab-cavern-consensus")

def get_pending_messages() -> list[dict]:
    """Read all pending messages in outbox queue."""
    if not PENDING_FILE.exists():
        return []
    messages = []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except Exception:
                        pass
    except Exception as e:
        print(f"[Outbox] Error reading pending messages: {e}", file=sys.stderr)
    return messages

def flush_pending_messages() -> list[dict]:
    """
    Atomically acquire and drain pending messages for delivery.
    Renames pending.jsonl to .flushing.<ts> so concurrent writes aren't lost.
    """
    if not PENDING_FILE.exists():
        return []
    
    proc_file = OUTBOX_DIR / f"flushing.{int(time.time()*1000)}.{os.getpid()}.jsonl"
    try:
        PENDING_FILE.replace(proc_file)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[Outbox] Error rotating pending file: {e}", file=sys.stderr)
        return []

    messages = []
    try:
        with open(proc_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except Exception:
                        pass
        proc_file.unlink()
    except Exception as e:
        print(f"[Outbox] Error processing flush file {proc_file}: {e}", file=sys.stderr)

    return messages

def dispatch_via_rest(omsg: dict) -> bool:
    """Send an outbox message directly to Discord via REST API."""
    import urllib.request
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("[Outbox] Warning: DISCORD_BOT_TOKEN not found, cannot dispatch via REST.", file=sys.stderr)
        return False

    target_cid = omsg.get("channel_id")
    ch_name = omsg.get("channel")
    if not target_cid:
        print(f"[Outbox] Warning: Missing channel_id for message {omsg.get('id')}", file=sys.stderr)
        return False

    is_banana_stand = (target_cid == 1534436119888793750 or str(ch_name) in ("the-banana-stand", "agent-chat"))
    if is_banana_stand:
        try:
            from tools.banana import claim, release
            claim(subject=omsg.get("id", "outbox-cli-flush"))
        except Exception as be:
            print(f"[Outbox] Warning claiming Banana: {be}", file=sys.stderr)

    try:
        url = f"https://discord.com/api/v10/channels/{target_cid}/messages"
        payload = json.dumps({"content": omsg.get("content", "")}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/brockventures/zero-agent, 1.0)"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        print(f"[Outbox] Error delivering message {omsg.get('id')} to {target_cid}: {e}", file=sys.stderr)
        dlq_file = DATA_DIR / "outbox" / "failed.jsonl"
        try:
            dlq_file.parent.mkdir(parents=True, exist_ok=True)
            failed_entry = dict(omsg)
            failed_entry["failed_at"] = time.time()
            failed_entry["error"] = str(e)
            with open(dlq_file, "a", encoding="utf-8") as df:
                df.write(json.dumps(failed_entry) + "\n")
        except Exception:
            pass
        return False
    finally:
        if is_banana_stand:
            try:
                from tools.banana import release
                release()
            except Exception:
                pass

def main():
    parser = argparse.ArgumentParser(
        description="Zero Cross-Channel Outbox Queue Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 tools/outbox.py --channel lounge --message "Executive summary from #the-banana-stand debate"
  python3 tools/outbox.py --list
  python3 tools/outbox.py --flush
"""
    )
    parser.add_argument("--channel", "-c", help="Target channel name or ID (e.g. lounge, the-banana-stand, zero-chat)")
    parser.add_argument("--message", "-m", help="Message content to queue for cross-channel delivery")
    parser.add_argument("--list", "-l", action="store_true", help="List all currently queued pending messages")
    parser.add_argument("--flush", "-f", action="store_true", help="Drain and immediately dispatch all pending messages via Discord REST API")
    parser.add_argument("--discard", action="store_true", help="Purge pending queue without sending (discard messages)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    if args.list:
        pending = get_pending_messages()
        if args.json:
            print(json.dumps(pending, indent=2))
        else:
            print(f"📦 Outbox Queue: {len(pending)} pending messages")
            for idx, msg in enumerate(pending, 1):
                print(f"  {idx}. [{msg['created_at_iso']}] -> #{msg['channel']} ({len(msg['content'])} chars): {msg['content'][:60]}...")
        return

    if args.discard:
        drained = flush_pending_messages()
        if args.json:
            print(json.dumps({"discarded": len(drained)}, indent=2))
        else:
            print(f"🗑️ Discarded {len(drained)} messages from outbox without sending.")
        return

    if args.flush:
        drained = flush_pending_messages()
        dispatched_count = 0
        for msg in drained:
            success = dispatch_via_rest(msg)
            if success:
                dispatched_count += 1
                if not args.json:
                    print(f"  ✓ Dispatched -> #{msg['channel']} ({msg.get('id')})")
            else:
                if not args.json:
                    print(f"  ✗ Failed to dispatch -> #{msg['channel']} ({msg.get('id')})")
        if args.json:
            print(json.dumps({"flushed": len(drained), "dispatched": dispatched_count}, indent=2))
        else:
            print(f"🚀 Flushed and dispatched {dispatched_count}/{len(drained)} messages from outbox.")
        return

    if not args.channel or not args.message:
        parser.print_help()
        sys.exit(1)

    record = queue_outbox_message(args.channel, args.message)
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(f"✅ Queued cross-channel outbox message to #{record['channel']} (ID: {record['id']})")

if __name__ == "__main__":
    main()
