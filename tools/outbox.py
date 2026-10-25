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
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

DATA_DIR = Path("/workspace/data")
OUTBOX_DIR = DATA_DIR / "outbox"
PENDING_FILE = OUTBOX_DIR / "pending.jsonl"
HISTORY_FILE = OUTBOX_DIR / "history.jsonl"
DEFAULT_DEDUPE_WINDOW_SECONDS = 600

KNOWN_CHANNELS = {
    "the-banana-stand": 1534436119888793750,
    "agent-chat": 1534436119888793750,  # alias for backward compatibility
    "lounge": 1534452820995080192,
    "zero-chat": 1542081375287640084,
    "forge": 1562899941595160657,
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
    "3d-print": 1555973316752449576,
    "debug-huddle": 1549555629222404096,
    "side-project": 1551465050072416286,
    "mirrormere": 1552732003076669460,
    "agora": 1558202642663211169,
    "brock-house": 1550577908811178095,
    "vault": 1550577910757458015,
    "gotchi": 1561509863018856468,
    "gotchi-chat": 1562246590943461429,
    "gotchi-log": 1562246590943461429,
    "r-residentagents": 1562544598423965856,
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

def extract_summary_topic(content: str) -> str | None:
    """Extract topic title if content is an executive summary."""
    import re
    m = re.search(r"(?:📋|🍌)?\s*\*{0,2}Executive Summary:\s*([^\*\n\r]+)\*{0,2}", content, re.IGNORECASE)
    if m:
        return m.group(1).strip().lower()
    return None

def normalize_text_for_dedupe(text: str) -> str:
    import re
    cleaned = re.sub(r"[\*\_`#🍌📋]", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned

def topic_match(t1: str | None, t2: str | None) -> bool:
    """Fuzzy match two executive summary topic titles."""
    if not t1 or not t2:
        return False
    t1, t2 = t1.lower().strip(), t2.lower().strip()
    if t1 == t2 or t1 in t2 or t2 in t1:
        return True
    import re
    words1 = set(re.findall(r"\w+", t1))
    words2 = set(re.findall(r"\w+", t2))
    stop = {"vs", "and", "or", "the", "in", "on", "for", "to", "of", "a", "an"}
    w1 = words1 - stop
    w2 = words2 - stop
    if not w1 or not w2:
        return False
    overlap = len(w1 & w2) / max(len(w1), len(w2))
    return overlap >= 0.5

def is_duplicate_outbox_message(
    channel: str,
    content: str,
    window_seconds: int = DEFAULT_DEDUPE_WINDOW_SECONDS
) -> tuple[bool, str]:
    """
    Check if an identical message or duplicate executive summary was recently
    queued or dispatched to the given channel within window_seconds.
    """
    clean_name, _ = resolve_channel(channel)
    now = time.time()
    new_norm = normalize_text_for_dedupe(content)
    new_topic = extract_summary_topic(content)

    # 1. Check currently pending queue
    pending = get_pending_messages()
    for p in pending:
        if p.get("channel") != clean_name:
            continue
        p_ts = p.get("created_at", 0)
        if now - p_ts > window_seconds:
            continue
        p_norm = normalize_text_for_dedupe(p.get("content", ""))
        if p_norm == new_norm:
            return True, f"identical message already pending in queue ({p.get('id')})"
        p_topic = extract_summary_topic(p.get("content", ""))
        if new_topic and p_topic and topic_match(new_topic, p_topic):
            return True, f"executive summary for '{new_topic}' already pending in queue ({p.get('id')})"

    # 2. Check recently dispatched history
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        h = json.loads(line)
                    except Exception:
                        continue
                    if h.get("channel") != clean_name:
                        continue
                    h_ts = h.get("dispatched_at") or h.get("created_at", 0)
                    if now - h_ts > window_seconds:
                        continue
                    h_norm = normalize_text_for_dedupe(h.get("content", ""))
                    if h_norm == new_norm:
                        return True, f"identical message dispatched to #{clean_name} {int(now - h_ts)}s ago ({h.get('id')})"
                    h_topic = extract_summary_topic(h.get("content", ""))
                    if new_topic and h_topic and topic_match(new_topic, h_topic):
                        return True, f"executive summary for topic '{new_topic}' dispatched to #{clean_name} {int(now - h_ts)}s ago ({h.get('id')})"
        except Exception as e:
            print(f"[Outbox] Warning reading history for dedupe check: {e}", file=sys.stderr)

    return False, ""

# Alias for backward compatibility and intuitive naming
is_duplicate_recent = is_duplicate_outbox_message

def record_dispatched_history(omsg: dict):
    """Record successfully dispatched message to history with 48h retention pruning."""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {
        "id": omsg.get("id"),
        "channel": omsg.get("channel"),
        "channel_id": omsg.get("channel_id"),
        "content": omsg.get("content", ""),
        "source": omsg.get("source"),
        "dispatched_at": now,
        "dispatched_at_iso": datetime.now(timezone.utc).isoformat()
    }
    retained = []
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            h = json.loads(line)
                            if now - (h.get("dispatched_at") or 0) < 172800:
                                retained.append(h)
                        except Exception:
                            pass
        except Exception:
            pass
    retained.append(record)
    try:
        tmp_file = OUTBOX_DIR / f"history.{int(now*1000)}.{os.getpid()}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            for r in retained:
                f.write(json.dumps(r) + "\n")
        tmp_file.replace(HISTORY_FILE)
        try:
            from tools.bridge_state import increment_bot_messages
            increment_bot_messages(1)
        except Exception:
            pass
    except Exception as e:
        print(f"[Outbox] Warning saving history: {e}", file=sys.stderr)

def queue_outbox_message(
    channel: str,
    content: str,
    source_turn: str = "zero",
    dedupe_window: int = DEFAULT_DEDUPE_WINDOW_SECONDS,
    force: bool = False,
    msg_type: str = "text",
) -> dict:
    """
    Queue a message for asynchronous delivery to another Discord channel.
    Uses atomic append to durable pending.jsonl queue.
    Guarded by deduplication window (default 600s). Pass force=True to bypass.
    """
    clean_name, ch_id = resolve_channel(channel)
    content = content.strip()

    # Format message for Discord: collapses link previews (< >), cleans broken file links, converts tables
    if msg_type == "text":
        try:
            from tools.bridge_formatting import format_for_discord
            content = format_for_discord(content)
        except Exception:
            pass

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

    # Deduplication Guard
    if not force:
        try:
            from tools.bridge_state import is_crab_cavern_silenced, CRAB_CAVERN_CHANNEL_IDS
            if ch_id in CRAB_CAVERN_CHANNEL_IDS or clean_name in ("the-banana-stand", "lounge", "side-project"):
                silenced, reason = is_crab_cavern_silenced()
                if silenced:
                    print(f"[Outbox] Crab Cavern silence guard: Suppressed message to #{clean_name} ({reason})", file=sys.stderr)
                    return {
                        "id": f"suppressed-silence-{int(time.time()*1000)}-{os.getpid()}",
                        "channel": clean_name,
                        "channel_id": ch_id,
                        "content": content,
                        "source": source_turn,
                        "created_at": time.time(),
                        "created_at_iso": datetime.now(timezone.utc).isoformat(),
                        "status": "silenced",
                        "suppressed": True,
                        "reason": reason,
                    }
        except Exception as e:
            print(f"[Outbox] Warning checking silence status: {e}", file=sys.stderr)

        is_dup, reason = is_duplicate_outbox_message(clean_name, content, window_seconds=dedupe_window)
        if is_dup:
            print(f"[Outbox] Deduplication guard: Suppressed duplicate message to #{clean_name} ({reason})", file=sys.stderr)
            return {
                "id": f"suppressed-dedupe-{int(time.time()*1000)}-{os.getpid()}",
                "channel": clean_name,
                "channel_id": ch_id,
                "content": content,
                "source": source_turn,
                "created_at": time.time(),
                "created_at_iso": datetime.now(timezone.utc).isoformat(),
                "status": "deduplicated",
                "suppressed": True,
                "reason": reason
            }
    else:
        # Hard identical-content guard: even with force=True, identical message to the same channel
        # within 120 seconds is blocked to prevent accidental loop/retry double-posts.
        is_dup, reason = is_duplicate_outbox_message(clean_name, content, window_seconds=120)
        if is_dup and "identical message" in reason:
            print(f"[Outbox] Hard duplicate guard: Blocked forced identical message to #{clean_name} ({reason})", file=sys.stderr)
            return {
                "id": f"suppressed-hard-dedupe-{int(time.time()*1000)}-{os.getpid()}",
                "channel": clean_name,
                "channel_id": ch_id,
                "content": content,
                "source": source_turn,
                "created_at": time.time(),
                "created_at_iso": datetime.now(timezone.utc).isoformat(),
                "status": "deduplicated",
                "suppressed": True,
                "reason": f"Hard duplicate guard: {reason}"
            }

    msg_record = {
        "id": f"outbox-{int(time.time()*1000)}-{os.getpid()}",
        "channel": clean_name,
        "channel_id": ch_id,
        "content": content,
        "source": source_turn,
        "type": msg_type,
        "created_at": time.time(),
        "created_at_iso": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
        "suppressed": False
    }

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    
    # Atomic write to line
    with open(PENDING_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(msg_record) + "\n")

    return msg_record

def queue_agent_turn(
    channel: str,
    prompt: str,
    job_name: str = "Detached Task Synthesis",
    force: bool = False,
) -> dict:
    """
    Queue an autonomous agent turn to be triggered by the bridge on the target channel.
    The bridge queue worker executes a full agy turn to synthesize findings.
    """
    return queue_outbox_message(
        channel=channel,
        content=prompt,
        source_turn=job_name,
        force=force,
        msg_type="agent_turn",
    )

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

    raw_content = omsg.get("content", "")
    if not raw_content:
        if omsg.get("prompt"):
            # This is an agent synthesis turn, not a static text message
            print(f"[Outbox] Notice: Message {omsg.get('id')} is an agent_turn; routing to turn_queue instead of REST.", file=sys.stderr)
        return False

    try:
        url = f"https://discord.com/api/v10/channels/{target_cid}/messages"
        # Banana Protocol Channel Quarantine: Strip handoff envelopes from non-banana channels
        if not is_banana_stand:
            raw_content = re.sub(
                r"(\n*(?:```(?:handoff)?|<!--\s*(?:banana-)?envelope)\s*\n?\{.*?\}\s*(?:```|-->)\s*)$",
                "",
                raw_content,
                flags=re.DOTALL,
            ).rstrip()
        choice_components = None
        try:
            from tools.bridge_formatting import format_for_discord, chunk_text, _CODE_OR_CHOICES_RE, mask_code_spans, restore_code_spans, strip_unquoted_choices
            formatted_content = format_for_discord(raw_content)
            if not is_banana_stand:
                masked_c, c_spans = mask_code_spans(formatted_content, prefix="\x00OUTBOX_CHOICES_")
                matches = [m for m in _CODE_OR_CHOICES_RE.finditer(masked_c) if m.group(2) is not None]
                parsed_choices = []
                for m in matches:
                    raw_c = m.group(3).strip()
                    delim = "|" if "|" in raw_c else ","
                    opts = [
                        restore_code_spans(c, c_spans, prefix="\x00OUTBOX_CHOICES_").strip().strip("'\"`“”‘’").strip()
                        for c in raw_c.split(delim)
                        if c.strip()
                    ]
                    opts = [c for c in opts if c]
                    if opts and not all(c in ("...", "…", "Option 1", "Option 2", "Option 3") for c in opts):
                        parsed_choices = opts
                if parsed_choices:
                    formatted_content = strip_unquoted_choices(formatted_content)
                    btns = []
                    for opt in parsed_choices[:5]:
                        btn_label = (opt[:77] + "...") if len(opt) > 80 else opt
                        btns.append({"type": 2, "style": 1, "label": btn_label, "custom_id": f"choice:{btn_label}"})
                    choice_components = [{"type": 1, "components": btns}]
            chunks = chunk_text(formatted_content, max_len=1900)
        except Exception:
            chunks = [raw_content] if len(raw_content) <= 1900 else [raw_content[i:i+1900] for i in range(0, len(raw_content), 1900)]

        try:
            from tools.bridge_formatting import strip_markdown_for_push_preview
        except Exception:
            strip_markdown_for_push_preview = lambda x: x

        all_ok = True
        for chunk_idx, chunk in enumerate(chunks):
            if chunk_idx > 0:
                time.sleep(0.5)
            is_last = (chunk_idx == len(chunks) - 1)
            initial_chunk = strip_markdown_for_push_preview(chunk) if not is_banana_stand else chunk
            body_dict = {"content": initial_chunk}
            if is_last and choice_components:
                body_dict["components"] = choice_components
            payload = json.dumps(body_dict).encode("utf-8")
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
            sent_msg_id = None
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status not in (200, 201):
                    all_ok = False
                else:
                    try:
                        resp_json = json.loads(resp.read().decode("utf-8"))
                        sent_msg_id = resp_json.get("id")
                    except Exception:
                        pass

            if sent_msg_id and initial_chunk != chunk:
                try:
                    edit_url = f"https://discord.com/api/v10/channels/{target_cid}/messages/{sent_msg_id}"
                    edit_dict = {"content": chunk}
                    if is_last and choice_components:
                        edit_dict["components"] = choice_components
                    edit_req = urllib.request.Request(
                        edit_url,
                        data=json.dumps(edit_dict).encode("utf-8"),
                        headers={
                            "Authorization": f"Bot {token}",
                            "Content-Type": "application/json",
                            "User-Agent": "DiscordBot (https://github.com/brockventures/zero-agent, 1.0)"
                        },
                        method="PATCH"
                    )
                    with urllib.request.urlopen(edit_req, timeout=15):
                        pass
                except Exception as pe:
                    print(f"[Outbox] Warning restoring markdown after plain push preview: {pe}", file=sys.stderr)

        if all_ok:
            record_dispatched_history(omsg)
        return all_ok
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
    parser.add_argument("--force", action="store_true", help="Bypass deduplication guard and force message delivery")
    parser.add_argument("--dedupe-window", type=int, default=DEFAULT_DEDUPE_WINDOW_SECONDS, help=f"Deduplication window in seconds (default: {DEFAULT_DEDUPE_WINDOW_SECONDS})")
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

    record = queue_outbox_message(
        args.channel,
        args.message,
        dedupe_window=args.dedupe_window,
        force=args.force
    )
    if record.get("suppressed"):
        if args.json:
            print(json.dumps(record, indent=2))
        else:
            print(f"⚠️ Message to #{record['channel']} suppressed by deduplication guard ({record.get('reason')}). Use --force to override.")
        return

    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(f"✅ Queued cross-channel outbox message to #{record['channel']} (ID: {record['id']})")

if __name__ == "__main__":
    main()
