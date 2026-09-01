#!/usr/bin/env python3
"""
outbox.py - Decoupled Cross-Channel Asynchronous Message Dispatch
Implements Marvin's atomic JSONL queue pattern (data/outbox/pending.jsonl)
so Zero can dispatch messages to other channels (#lounge, #agent-chat, #zero-chat)
without blocking the active turn or coupling delivery to the current execution thread.
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path("/workspace/data")
OUTBOX_DIR = DATA_DIR / "outbox"
PENDING_FILE = OUTBOX_DIR / "pending.jsonl"

KNOWN_CHANNELS = {
    "agent-chat": 1534436119888793750,
    "lounge": 1534452820995080192,
    "zero-chat": 1542081375287640084,
    "general": 1534452820995080192,  # alias to lounge or main
    "signals": 1534436119888793750,
    "staff-comms": 1534436119888793750,
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
    if not content:
        raise ValueError("Message content cannot be empty.")
    if len(content) > 4000:
        raise ValueError(f"Message content exceeds 4,000 character ceiling (len={len(content)}).")

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
    Format and queue an informational proposal/consensus notice to #zero-chat.
    Ensures post-consensus decisions from Crab Cavern are visible to Ryan immediately.
    """
    is_shipped = action_type.lower() in ("shipped", "deployed", "completed", "live")
    header = "📦 **Crab Cavern Deliverable Shipped**" if is_shipped else "📋 **Crab Cavern Engineering Commitment**"
    task_line = f"\n• **Durable Task:** Task #{task_id}" if task_id else ""
    msg = (
        f"{header}\n"
        f"• **Subject:** `{subject}`\n"
        f"• **Collaborators:** {agreed_with}\n"
        f"• **Status:** {action_type}{task_line}\n"
        f"• **Summary:** {details}\n\n"
        f"_Flagged for visibility. Let me know in `#zero-chat` if you want to steer or adjust._"
    )
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

def main():
    parser = argparse.ArgumentParser(
        description="Zero Cross-Channel Outbox Queue Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 tools/outbox.py --channel lounge --message "Executive summary from #agent-chat debate"
  python3 tools/outbox.py --list
  python3 tools/outbox.py --flush
"""
    )
    parser.add_argument("--channel", "-c", help="Target channel name or ID (e.g. lounge, agent-chat, zero-chat)")
    parser.add_argument("--message", "-m", help="Message content to queue for cross-channel delivery")
    parser.add_argument("--list", "-l", action="store_true", help="List all currently queued pending messages")
    parser.add_argument("--flush", "-f", action="store_true", help="Drain and print all pending messages")
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

    if args.flush:
        drained = flush_pending_messages()
        if args.json:
            print(json.dumps(drained, indent=2))
        else:
            print(f"🚀 Flushed {len(drained)} messages from outbox.")
            for msg in drained:
                print(f"  -> #{msg['channel']}: {msg['content'][:80]}...")
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
