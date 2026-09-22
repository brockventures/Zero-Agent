"""
bridge_ambient.py — Crab Cavern Ingestion, Multi-Agent Sliding Window & Mention Gating.

Core Responsibilities:
1. Channel History Warmup: Prefetch recent Discord messages on startup/join to populate
   rolling multi-agent sliding window and recover backlog turns after reconnects.
2. Brock Guild Public Gating: Strict owner-only/Ivy tagging discipline in public channels
   (#seerr-requests-and-chat, #server-updates, #seerr-notifications, #baseball).
3. Crab Cavern Protocol: Two-tier ambient ingestion (direct mention / Tier 2 classifier),
   channel-specific tag gating, role mention translation, and peer loop prevention.
4. Banana Watcher Directives: Handshake synthesis for executive summaries, topic stalls,
   and loop clamps.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal
import time
from typing import Callable, Optional
import uuid

import discord

from tools.bridge_commands import (
    execute_bridge_reload,
    execute_container_restart,
)
from tools.bridge_runner import (
    channel_active_procs,
    steering_channels,
)
from tools.bridge_state import (
    ATTACHMENTS_DIR,
    IVY_USER_ID,
    OWNER_USER_ID,
    READONLY_NOTIFICATION_CHANNELS,
    clear_channel_session_id,
    get_runtime_rules,
    is_brock_guild,
    is_container_restart_intent,
    is_home_channel,
    is_excluded_channel,
    is_reply_to_zero as resolve_is_reply_to_zero,
    is_reload_intent,
)

BANANA_WATCHER_BOT_ID = 1545924520236290198
PROCESSED_BACKLOG_MSG_IDS: set[int] = set()
channel_last_bot_reply: dict[int, float] = {}

NON_NAME_NOUNS = r"(?:day|shot|downtime|errors?|detections?|latency|tolerance|cost|percent|sum|crossing|emission|point|index|out|wrapping|layout|config|leakage|padding|margin|trust|knowledge|defect|defects|risk|budget|drift|progress)\b"


def contains_zero_mention(text: str) -> bool:
    """Check if text addresses Zero via @Zero, snowflake, vocative, or bare 'Zero' text."""
    if not text:
        return False
    # 1. Explicit @zero mention anywhere
    if re.search(r"@zero\b", text, re.IGNORECASE):
        return True
    # 2. Greetings: 'hey/hi/hello zero'
    if re.search(r"\b(?:hey|hi|hello)\s+zero\b", text, re.IGNORECASE):
        return True
    # 3. Punctuation vocative: 'Zero:', 'Zero,', 'Zero -', 'Zero?'
    # Note: For dashes, require whitespace or end of string to prevent false-positives on hyphenated words (zero-leakage, zero-shot, zero-sum)
    if re.search(r"(?:^|[\n.!?\s,;])zero\s*(?:[:,]|--?(?:\s+|$))", text, re.IGNORECASE) or re.search(r"\bzero\s*[?!]", text, re.IGNORECASE):
        return True
    # 4. Directive verbs targeting Zero: 'ask Zero', 'tag Zero', 'tell Zero', 'cc Zero'
    if re.search(r"\b(?:ask|tag|tell|ping|cc)\s+zero\b", text, re.IGNORECASE):
        return True
    # 5. Sentence starter: 'Zero <verb/query>' at start of message, line, or sentence
    if re.search(r"(?:^|[\n.!?]\s*)zero\b(?:\s+(?!" + NON_NAME_NOUNS + r")\S+|$)", text, re.IGNORECASE):
        return True
    return False


async def warm_channel_history(
    channel: discord.abc.Messageable,
    limit: int = 25,
    bot: Optional[discord.Client] = None,
    turn_queue: Optional[asyncio.Queue] = None,
    ext_turn_queue: Optional[asyncio.Queue] = None,
    reload_fn: Optional[Callable] = None,
    handle_message_fn: Optional[Callable] = None,
) -> None:
    """Prefetch recent messages from Discord channel to initialize history buffer and recover unhandled turns."""
    global PROCESSED_BACKLOG_MSG_IDS
    if not channel or not hasattr(channel, "history"):
        return
    try:
        from tools.channel_history import record_message
        msgs = []
        async for m in channel.history(limit=limit):
            msgs.append(m)
        msgs.reverse()
        for m in msgs:
            author_name = m.author.display_name or m.author.name
            reply_id = m.reference.message_id if m.reference else None
            record_message(
                channel_id=channel.id,
                channel_name=getattr(channel, "name", str(channel.id)),
                author_name=author_name,
                is_bot=m.author.bot,
                content=m.content,
                msg_id=m.id,
                reply_to_id=reply_id,
                timestamp=m.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if hasattr(m, "created_at") else None,
            )
        print(f"[Bridge] Warmed channel history for #{getattr(channel, 'name', channel.id)}: {len(msgs)} messages loaded.")

        # Backlog Recovery Guard: Check for unhandled user messages in home channels sent during reconnect or downtime
        if is_home_channel(channel) and bot and turn_queue and msgs:
            now_ts = time.time()
            last_msg = msgs[-1]
            if not last_msg.author.bot and last_msg.content.strip():
                msg_age = (now_ts - last_msg.created_at.timestamp()) if hasattr(last_msg, "created_at") else 0
                if 0 <= msg_age <= 900:  # sent within the last 15 minutes
                    if last_msg.id not in PROCESSED_BACKLOG_MSG_IDS:
                        PROCESSED_BACKLOG_MSG_IDS.add(last_msg.id)
                        author_name = last_msg.author.display_name or last_msg.author.name
                        print(f"[Bridge] 🔄 Recovered unhandled home message from {author_name} sent during downtime/reconnect ({last_msg.id}): \"{last_msg.content[:80]}\"")
                        target_fn = handle_message_fn
                        if not target_fn:
                            from tools.bridge_handlers import handle_message
                            target_fn = handle_message
                        asyncio.create_task(
                            target_fn(
                                msg=last_msg,
                                bot=bot,
                                home_turn_queue=turn_queue,
                                ext_turn_queue=ext_turn_queue,
                                reload_fn=reload_fn,
                            )
                        )
    except Exception as e:
        print(f"[Bridge] Error warming channel history for {getattr(channel, 'id', channel)}: {e}")


async def route_external_message(
    msg: discord.Message,
    bot: discord.Client,
    content: str,
    author_name: str,
    home_turn_queue: asyncio.Queue,
    ext_turn_queue: asyncio.Queue,
    reload_fn: Optional[Callable] = None,
    rules: Optional[dict] = None,
) -> bool:
    """Evaluate and route an inbound message arriving outside of home operations channels.
    
    Returns True if the message was completely handled, enqueued, or intentionally ignored/buffered.
    Returns False if it should fall through to default handling.
    """
    rules = rules or get_runtime_rules()

    # 1. Public channels in Brock Discord (e.g. #seerr-requests-and-chat, #server-updates, #seerr-notifications, #baseball)
    # Strict Rule: In Brock Discord, Zero strictly ignores all bots (including Ivy) and ONLY responds to Ryan Brock explicitly tagging Zero.
    if is_brock_guild(msg):
        # Dedicated Excluded Channel Quarantine (#baseball):
        # Owned exclusively by Ivy. Zero responds ONLY if:
        # - Ryan Brock (OWNER_USER_ID) explicitly tags Zero or replies directly to Zero.
        # - Ivy (IVY_USER_ID) explicitly tags Zero's snowflake or replies directly to Zero.
        # Governed by Last Word Protocol and 4s cascade cooldown.
        is_excluded = is_excluded_channel(msg.channel)
        if is_excluded:
            if msg.author.id not in (OWNER_USER_ID, IVY_USER_ID):
                return True
        else:
            # Standard Brock Public Channels (#server-updates, #seerr-*): responds ONLY to Ryan Brock.
            if msg.author.bot or msg.author.id != OWNER_USER_ID:
                return True

        bot_id = str(bot.user.id) if bot.user else "1542285964213358633"
        reply_to_zero = resolve_is_reply_to_zero(msg, bot)

        if is_excluded:
            has_tag = (
                f"<@{bot_id}>" in content or
                f"<@!{bot_id}>" in content or
                bool(re.search(r"@zero\b", content, re.IGNORECASE))
            )
            is_tagged = has_tag or reply_to_zero
        else:
            is_tagged = (
                reply_to_zero or
                (bot.user and bot.user in msg.mentions) or
                f"<@{bot_id}>" in content or
                f"<@!{bot_id}>" in content or
                contains_zero_mention(content) or
                re.search(r"(?:@robot\b|\b(?:hey|hi|hello)\s+@?robot\b|^\s*@?robot\s*[:,-])", content, re.IGNORECASE) is not None
            )

        if not is_tagged:
            return True

        # Check if author is Ivy
        is_ivy = (msg.author.id == IVY_USER_ID)
        is_last_word = False
        last_word_streak = 0
        if is_ivy:
            from tools.last_word_protocol import is_bot_paused, check_last_word_condition, build_last_word_prompt_injection
            paused, rem, _ = is_bot_paused(msg.channel.id, msg.author.id, "Ivy")
            if paused:
                print(f"[BridgeAmbient] Excluded channel #{getattr(msg.channel, 'name', msg.channel.id)}: Ivy is paused ({rem:.1f}s). Dropping.")
                return True

            threshold = int(rules.get("excluded_channel_last_word_threshold", 4))
            is_last_word, last_word_streak = check_last_word_condition(
                channel_id=msg.channel.id,
                bot_id=msg.author.id,
                bot_name="Ivy",
                threshold=threshold,
            )

        # Clean invocation prefix
        cleaned = content
        cleaned = re.sub(rf"<@!?{bot_id}>", "", cleaned)
        cleaned = re.sub(r"^(hey\s+)?zero[:,\s]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"@zero\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^(hey\s+)?robot[:,\s]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"@robot\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip()

        saved_attachments = []
        if msg.attachments:
            for att in msg.attachments:
                safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", att.filename)
                dest = ATTACHMENTS_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
                try:
                    await att.save(dest)
                    saved_attachments.append(str(dest))
                except Exception as e:
                    print(f"[Bridge] Failed saving attachment: {e}")

        if not is_ivy:
            if cleaned.lower() in ("!reset", "/reset", "!new", "/new"):
                clear_channel_session_id(msg.channel.id, "home")
                await msg.reply("🔄 Conversation session reset for this channel.")
                return True

            if is_container_restart_intent(cleaned):
                ch_name = getattr(msg.channel, "name", str(msg.channel.id))
                await execute_container_restart(msg.channel, initiator=author_name, reason=f"Manual Docker container restart requested via #{ch_name}")
                return True

            if is_reload_intent(cleaned):
                ch_name = getattr(msg.channel, "name", str(msg.channel.id))
                if reload_fn:
                    await reload_fn(msg.channel, initiator=author_name, force=True, reason=f"Manual in-place bridge reload requested via #{ch_name}")
                else:
                    await execute_bridge_reload(bot, msg.channel, initiator=author_name, force=True, reason=f"Manual in-place bridge reload requested via #{ch_name}")
                return True

        prompt_content = cleaned
        is_lazy = bool(
            not prompt_content or
            re.fullmatch(r"[\^\s\.\?!]+", prompt_content) or
            prompt_content.lower() in ("^", "^^", "^^^", "this", "look", "see", "what?", "check this")
        )
        if is_lazy and not saved_attachments:
            prompt_content = (
                "[OPERATIONAL DIRECTIVE - LAZY TYPER ADDRESSING]:\n"
                f"The user sent a minimal prompt ('{content.strip()}').\n"
                "Humans are lazy typers: review recent conversation/channel history to identify the topic, question, or task at hand and address it directly."
            )

        if saved_attachments:
            image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
            has_images = any(Path(p).suffix.lower() in image_exts for p in saved_attachments)
            image_directive = (
                "\n\n[CRITICAL IMAGE INPUT INVARIANT]:\n"
                "One or more images are attached to this message. You MUST parse and inspect every image (using view_file) and understand its visual contents, error traces, screenshots, or diagrams as part of the primary input, EVEN IF the accompanying text message made no mention of the image."
                if has_images else ""
            )
            hint = "\n\n[Attached file(s) available via view_file tool]:\n" + "\n".join(f"- {p}" for p in saved_attachments)
            if is_lazy:
                prompt_content = (
                    f"[OPERATIONAL DIRECTIVE - LAZY TYPER & ATTACHMENT ADDRESSING]:\n"
                    f"The user sent an attachment with a minimal prompt ('{content.strip()}').\n"
                    "Humans are lazy typers: review the attached image/file(s) and recent conversation history to address the active topic directly."
                )
            prompt_content = (prompt_content or "Please inspect the attached file(s) and assist.") + hint + image_directive

        if not prompt_content:
            return True

        if is_ivy and is_last_word:
            pause_mins = int(rules.get("last_word_pause_minutes", 3))
            prompt_content += build_last_word_prompt_injection("Ivy", last_word_streak, pause_minutes=pause_mins)

        ch_name = getattr(msg.channel, "name", str(msg.channel.id))
        ch_ctx_block = ""
        try:
            from tools.channel_history import format_channel_context
            ch_ctx = format_channel_context(msg.channel.id, limit=10, exclude_msg_id=msg.id)
            if ch_ctx:
                ch_ctx_block = f"\n{ch_ctx}\n\n"
        except Exception:
            pass

        if is_ivy:
            public_prompt = (
                f"[OPERATIONAL DIRECTIVE - BROCK DISCORD #{ch_name} (CROSS-BOT PEER COMMUNICATION)]:\n"
                f"You are responding directly to Ivy (<@{IVY_USER_ID}>), the Assistant GM AI partner in #{ch_name}.\n"
                f"• Be direct, sharp, and concise (single Discord message, max 2000 characters).\n"
                f"• Honor the baseball domain boundaries and peer operating discipline.\n"
                f"• Public-Safe Etiquette: Do NOT reveal sensitive credentials, tokens, private IPs, or personal family information.\n\n"
                f"{ch_ctx_block}"
                f"{prompt_content}"
            )
        else:
            public_prompt = (
                f"[OPERATIONAL DIRECTIVE - BROCK DISCORD PUBLIC CHANNEL #{ch_name}]:\n"
                f"You are responding directly to Ryan in a public channel on Brock Discord.\n"
                f"• Be direct, concise, and helpful (single Discord message, max 2000 characters).\n"
                f"• Public-Safe Etiquette: Do NOT reveal sensitive credentials, tokens, private IPs, or personal family information.\n\n"
                f"{ch_ctx_block}"
                f"{prompt_content}"
            )

        await home_turn_queue.put({
            "prompt": public_prompt,
            "status_msg": None,
            "reply_target": msg,
            "attachments": saved_attachments,
            "is_steer": False,
            "mode": "home",
            "channel_id": msg.channel.id,
            "author_name": author_name,
            "is_last_word": is_last_word,
            "last_word_bot_id": str(msg.author.id) if is_ivy else None,
            "last_word_bot_name": "Ivy" if is_ivy else None,
            "last_word_streak": last_word_streak,
            "queued_at": time.perf_counter(),
        })
        return True

    # 2. Ignore automated notification / webhook channels unless directly tagged
    ch_name = getattr(msg.channel, "name", "").lower()
    if (msg.channel.id in READONLY_NOTIFICATION_CHANNELS or ch_name in ("server-updates", "downloads")) and not (bot.user and bot.user in msg.mentions):
        return True

    # 3. Crab Cavern & External / Shared Space Mode (Crab Cavern Protocol)
    from tools.channel_history import is_handoff_addressed_to_zero
    handoff_for_zero = is_handoff_addressed_to_zero(content)

    is_banana_watcher = (
        getattr(msg.author, "id", None) == BANANA_WATCHER_BOT_ID or
        str(getattr(msg.author, "id", "")) == str(BANANA_WATCHER_BOT_ID) or
        "banana watcher" in author_name.lower()
    )
    is_banana_summary_prompt = bool(
        is_banana_watcher and
        re.search(r"🍌 \*\*Discussion Concluded\*\*: Topic `(?P<subject>[^`]+)` has reached resolution", content)
    )
    is_banana_stall_prompt = bool(
        is_banana_watcher and
        re.search(r"🍌 \*\*Topic Stalled\*\*: Topic `(?P<subject>[^`]+)`", content)
    )
    is_banana_loop_prompt = bool(
        is_banana_watcher and
        re.search(r"🍌 \*\*Loop Warning\*\*: Topic `(?P<subject>[^`]+)`", content)
    )
    if is_banana_summary_prompt or is_banana_stall_prompt or is_banana_loop_prompt:
        handoff_for_zero = True

    # Envelope evaluation & topic resolution
    try:
        from tools.handoff import parse_envelope
        envelope = parse_envelope(content)
        if envelope:
            import importlib
            import tools.topic_tracker
            importlib.reload(tools.topic_tracker)
            tools.topic_tracker.check_and_resolve_topic(envelope, content, author_name, msg.id, msg.channel.id)
            floor_state = str(envelope.get("floor") or "").lower()
            if floor_state == "closed" and not handoff_for_zero:
                print(f"[Bridge] Suppressed turn: floor is closed per envelope from {author_name}")
                return True
    except Exception as te:
        print(f"[Bridge] Error checking topic resolution: {te}")

    # Loop prevention for peer bots (Amos, Marvin, etc.)
    if msg.author.bot and not is_banana_watcher:
        try:
            from tools.last_word_protocol import is_bot_paused, is_last_word_in_flight
            paused, rem, rec = is_bot_paused(msg.channel.id, msg.author.id, author_name)
            if paused or is_last_word_in_flight(msg.channel.id, str(msg.author.id)) or is_last_word_in_flight(msg.channel.id, author_name):
                print(f"[Bridge] Suppressed message from bot {author_name} ({msg.author.id}) in channel {msg.channel.id}: paused under Last Word Protocol ({rem:.0f}s remaining)")
                return True
        except Exception as pe:
            print(f"[Bridge] Error checking bot cooldown: {pe}")

        if re.search(r"\b(staying silent|remaining silent|stay silent|no ask|nothing outstanding|standing by|room quiet|silence boundaries|no-op)\b", content, re.IGNORECASE):
            print(f"[Bridge] Dropped bot status/silence narration message from {author_name} in channel {msg.channel.id}")
            return True

        words = [w for w in content.split() if any(c.isalnum() for c in w)]
        if len(words) < 4 and not handoff_for_zero:
            return True

        now = time.time()
        last_bot_reply = channel_last_bot_reply.get(msg.channel.id, 0)
        if (now - last_bot_reply < 4.0) and not handoff_for_zero:
            print(f"[Bridge] Suppressed bot reply due to 4s cascade cooldown in channel {msg.channel.id}")
            return True
        channel_last_bot_reply[msg.channel.id] = now

    # Addressing Gate: check if this message is a direct reply to Zero
    bot_id = str(bot.user.id) if bot.user else "1542285964213358633"
    bot_mention_1 = f"<@{bot_id}>"
    bot_mention_2 = f"<@!{bot_id}>"

    is_reply_to_zero = resolve_is_reply_to_zero(msg, bot)

    # Channel-specific tag enforcement
    channel_tag_requirements = rules.get("channel_tag_requirements", {})
    req_tag = channel_tag_requirements.get(str(msg.channel.id))
    is_tagged_role = False
    role_ids = [str(r.id) for r in getattr(msg, "role_mentions", [])]
    is_robot_tagged = (
        "<@&1543285916506783799>" in content or
        "1543285916506783799" in role_ids or
        "<@&1542294519914037341>" in content or
        "1542294519914037341" in role_ids or
        re.search(r"(?:^|[\s,;])@robot\b", content, re.IGNORECASE) is not None or
        re.search(r"^(?:hey\s+)?robot[:,\s]", content, re.IGNORECASE) is not None
    )
    is_team_tagged = (
        "<@&1543462881624858624>" in content or
        "1543462881624858624" in role_ids or
        re.search(r"(?:^|[\s,;])@team\b", content, re.IGNORECASE) is not None or
        re.search(r"^(?:hey\s+)?team[:,\s]", content, re.IGNORECASE) is not None
    )

    if req_tag:
        allowed_tags = req_tag if isinstance(req_tag, list) else [req_tag]
        allowed_tag_strs = [f"<@&{t}>" for t in allowed_tags]
        has_required_tag = (
            any(t_str in content for t_str in allowed_tag_strs) or
            any(str(t) in role_ids for t in allowed_tags)
        )
        is_direct_bot_ping = (
            (bot.user and bot.user in msg.mentions) or
            f"<@{bot_id}>" in content or
            f"<@!{bot_id}>" in content or
            contains_zero_mention(content) or
            is_robot_tagged or
            is_team_tagged or
            is_reply_to_zero
        )
        if not has_required_tag and not is_direct_bot_ping:
            print(f"[Bridge] Message in channel {msg.channel.id} ignored: missing required tag(s) {allowed_tag_strs}")
            return True
        is_tagged_role = True

    # Conversational follow-up detection:
    is_conversational_follow_up = False
    if not msg.author.bot and not is_reply_to_zero:
        try:
            from tools.classifier import is_explicitly_addressed_to_other
            if not is_explicitly_addressed_to_other(content):
                from tools.channel_history import get_recent_messages
                prev_msgs = get_recent_messages(msg.channel.id, limit=5, exclude_msg_id=msg.id)
                if prev_msgs:
                    last_msg = prev_msgs[-1]
                    last_author = str(last_msg.get("author", "")).lower()
                    if last_author == "zero" or (last_msg.get("is_bot") and "zero" in last_author):
                        now_ts = time.time()
                        if hasattr(msg, "created_at"):
                            try:
                                val = msg.created_at.timestamp()
                                if isinstance(val, (int, float)):
                                    now_ts = float(val)
                            except Exception:
                                pass
                        last_ts_str = last_msg.get("timestamp")
                        last_msg_ts = 0.0
                        if last_ts_str:
                            try:
                                last_dt = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                                last_msg_ts = last_dt.timestamp()
                            except Exception:
                                pass

                        elapsed = (now_ts - last_msg_ts) if last_msg_ts > 0 else 0
                        if isinstance(elapsed, (int, float)) and 0 <= elapsed <= 300:
                            last_content = last_msg.get("content", "")
                            asked_question = ("?" in last_content[-400:]) or ('"reply": "optional"' in last_content) or ('"floor": "open"' in last_content)
                            is_action_or_affirmative = bool(re.search(
                                r"^(?:yep|yeah|yes|sure|go ahead|sounds good|do it|proceed|approved|lgtm|update|check|push|fix|please|pls|thanks|thank you|can you|could you|what about|how about|also|no|nope|wait|try|use|make|let|revert|rollback|deploy|show|tell|why|explain|see|look|run|stop|start|restart|reload|clean|add|remove|delete|set|get|test|verify|option|choice|step)\b",
                                content.strip(),
                                re.IGNORECASE,
                            ))
                            is_direct_query = bool(re.search(
                                r"^(?:done|status|ready|finished|how'?s|is\s+it|did\s+it|any\s+update|which|where|what)\b",
                                content.strip(),
                                re.IGNORECASE,
                            )) or ("?" in content and len(content.split()) <= 15)
                            if asked_question or is_action_or_affirmative or is_direct_query:
                                is_conversational_follow_up = True
                                print(f"[Bridge] Conversational follow-up to Zero detected from {author_name} ({elapsed:.1f}s after Zero's turn) in channel {msg.channel.id}")
        except Exception as fe:
            print(f"[Bridge] Warning evaluating conversational follow-up: {fe}")

    is_mentioned = (
        is_tagged_role or
        (bot.user and bot.user in msg.mentions) or
        bot_mention_1 in content or
        bot_mention_2 in content or
        is_robot_tagged or
        is_team_tagged or
        contains_zero_mention(content) or
        re.search(r"(?:@robot\b|\b(?:hey|hi|hello)\s+@?robot\b|^\s*@?robot\s*[:,-])", content, re.IGNORECASE) is not None or
        is_reply_to_zero or
        handoff_for_zero or
        is_conversational_follow_up
    )
    if not is_mentioned:
        if rules.get("ambient_classifier_enabled", False):
            try:
                from tools.classifier import score_relevance
                score = await asyncio.to_thread(score_relevance, content, author_name)
                threshold = float(rules.get("ambient_relevance_threshold", 0.80))
                if score >= threshold:
                    print(f"[Bridge] Ambient classifier scored {score:.2f} >= {threshold:.2f} for {author_name}. Triggering chime-in.")
                else:
                    print(f"[Bridge] Ambient classifier scored {score:.2f} < {threshold:.2f} for {author_name} (buffered, remaining silent)")
                    return True
            except Exception as ce:
                print(f"[Bridge] Error running ambient classifier: {ce}")
                return True
        else:
            print(f"[Bridge] Buffered message from {author_name} in channel {msg.channel.id} (passive observer mode)")
            return True

    # Clean mentions from prompt
    target_role_ids = {"1543462881624858624", "1543285916506783799", "1542294519914037341"}
    if req_tag:
        if isinstance(req_tag, list):
            target_role_ids.update(str(t) for t in req_tag)
        else:
            target_role_ids.add(str(req_tag))

    other_mentions = [
        m for m in re.findall(r"<@!?([0-9]+)>", content)
        if m != bot_id
    ]

    is_banana_directive = is_banana_summary_prompt or is_banana_stall_prompt or is_banana_loop_prompt

    if other_mentions or is_banana_directive:
        cleaned = re.sub(rf"<@!?{bot_id}>", "@Zero", content)
    else:
        cleaned = re.sub(rf"<@!?{bot_id}>", "", content)

    for rid in target_role_ids:
        cleaned = re.sub(rf"^\s*<@&{rid}>\s*", "", cleaned)
    cleaned = re.sub(r"<@&1543462881624858624>", "@team", cleaned)
    cleaned = re.sub(r"<@&1543285916506783799>", "@robot", cleaned)
    cleaned = re.sub(r"<@&1542294519914037341>", "@robot", cleaned)
    for rid in target_role_ids:
        cleaned = re.sub(rf"<@&{rid}>", "", cleaned)

    if not other_mentions and not is_banana_directive:
        cleaned = re.sub(r"^(?:hey\s+)?zero\b[:,\s]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"@zero\b", "", cleaned, flags=re.IGNORECASE)
    elif is_banana_directive:
        cleaned = re.sub(r"@Zero\s*\(@Zero\):?", "@Zero:", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"^(hey\s+)?robot[:,\s]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"@robot\b", "", cleaned, flags=re.IGNORECASE)

    try:
        from tools.handoff import TARGET_MENTIONS
        for name, mention_str in TARGET_MENTIONS.items():
            if name not in ("zero", "robot", "team"):
                m_id = mention_str.strip("<@!>")
                cleaned = re.sub(rf"<@!?{m_id}>", f"@{name.title()}", cleaned)
    except Exception:
        pass

    cleaned = cleaned.strip()

    if is_banana_summary_prompt:
        m_subj = re.search(r"🍌 \*\*Discussion Concluded\*\*: Topic `(?P<subject>[^`]+)` has reached resolution", content)
        concluded_subj = m_subj.group("subject") if m_subj else "concluded-topic"
        cleaned += (
            f"\n\n[CRITICAL OPERATIONAL DIRECTIVE - RULE 7 CONCLUDED DISCUSSION EXECUTIVE SUMMARY]:\n"
            f"Banana Watcher has officially declared topic `{concluded_subj}` concluded in #the-banana-stand.\n"
            f"In accordance with Crab Cavern Ratified Peer Operating Rule 7, you MUST execute the following:\n"
            f"1. Review recent channel context to understand the problem, debate, and final consensus reached on `{concluded_subj}`.\n"
            f"2. Synthesize a concise executive summary (strictly under 250 words) structured as:\n"
            f"   • Problem / Motivation\n"
            f"   • Resolution / Ratified Consensus\n"
            f"   • Shipped Artifacts & PR/Code references\n"
            f"   CRITICAL STYLE REQUIREMENT: Write strictly in plain language, without overly complex industry lingo, dense academic jargon, or acronym walls.\n"
            f"3. Dispatch the executive summary directly to #lounge (<#1534452820995080192>) via outbox:\n"
            f"   python3 /workspace/tools/outbox.py --channel lounge --message \"<summary_text>\"\n"
            f"4. If durable decisions or architecture were agreed upon, log the resolution to /workspace/memory/crab_cavern/decisions.md.\n"
            f"5. Acknowledge in #the-banana-stand with STRICTLY and ONLY '🍌' (a single banana emoji).\n"
            f"   CRITICAL: Do NOT output the summary text body into #the-banana-stand!"
        )

    if is_banana_stall_prompt:
        m_subj = re.search(r"🍌 \*\*Topic Stalled\*\*: Topic `(?P<subject>[^`]+)`", content)
        stalled_subj = m_subj.group("subject") if m_subj else "stalled-topic"
        cleaned += (
            f"\n\n[CRITICAL OPERATIONAL DIRECTIVE - BANANA WATCHER TOPIC STALLED NUDGE]:\n"
            f"Banana Watcher has flagged that topic `{stalled_subj}` has stalled without resolution in #the-banana-stand.\n"
            f"In accordance with Crab Cavern Ratified Peer Operating Rule 6 ('Ship it or track it: never let agreed proposals drop on the floor'):\n"
            f"1. Review recent channel context on `{stalled_subj}`.\n"
            f"2. If the discussion concluded, reached consensus, or should be parked:\n"
            f"   Reply directly in #the-banana-stand with: '🍌 Parking `{stalled_subj}`, Banana Watcher: <brief reason>' with handoff envelope (kind: 'resolution', floor: 'closed', reply: 'none').\n"
            f"3. If there is an actionable proposal or pending task agreed upon, record a task ticket in /workspace/data/tasks.json, log to /workspace/memory/crab_cavern/decisions.md, and post the ticket details.\n"
            f"4. You MUST respond to Banana Watcher. NEVER emit [NO_REPLY] to a Banana Watcher nudge."
        )

    if is_banana_loop_prompt:
        m_subj = re.search(r"🍌 \*\*Loop Warning\*\*: Topic `(?P<subject>[^`]+)`", content)
        loop_subj = m_subj.group("subject") if m_subj else "loop-topic"
        cleaned += (
            f"\n\n[CRITICAL OPERATIONAL DIRECTIVE - BANANA WATCHER LOOP WARNING]:\n"
            f"Banana Watcher has flagged topic `{loop_subj}` for excessive turns without consensus.\n"
            f"Clamp the loop immediately: synthesize a terminal conclusion, close the floor (`floor: 'closed'`), or park the topic.\n"
            f"You MUST respond to Banana Watcher. NEVER emit [NO_REPLY] to a Banana Watcher prompt."
        )

    saved_attachments = []
    if msg.attachments:
        for att in msg.attachments:
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", att.filename)
            dest = ATTACHMENTS_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
            try:
                await att.save(dest)
                saved_attachments.append(str(dest))
            except Exception as e:
                print(f"[Bridge] Failed saving attachment: {e}")

    # Administrative commands in external channels
    if cleaned.lower() in ("!reset", "/reset", "!new", "/new"):
        clear_channel_session_id(msg.channel.id, "external")
        await msg.reply("🔄 Session reset for this channel.")
        return True

    pause_match = re.search(
        r"^(?:!pause|/pause|pause\s+responses?\s+to|pause\s+responding\s+to|pause)\s+([a-zA-Z0-9_-]+)(?:\s+(?:for\s+)?(\d+)\s*(?:m|min|mins|minutes)?)?$",
        cleaned,
        re.IGNORECASE,
    )
    if pause_match:
        target_bot = pause_match.group(1).strip()
        mins = int(pause_match.group(2)) if pause_match.group(2) else int(rules.get("last_word_pause_minutes", 3))
        from tools.last_word_protocol import pause_bot
        rec = pause_bot(
            channel_id=msg.channel.id,
            bot_id=target_bot if target_bot.isdigit() else None,
            bot_name=target_bot if not target_bot.isdigit() else None,
            duration_seconds=mins * 60.0,
            reason=f"Operator command from {author_name}",
        )
        await msg.reply(f"⏸️ Responses to **{rec.get('bot_name', target_bot)}** in <#{msg.channel.id}> paused for {mins} minutes (until {rec.get('pause_until_pt')}).")
        return True

    unpause_match = re.search(
        r"^(?:!unpause|/unpause|resume\s+responses?\s+to|resume\s+responding\s+to|unpause)\s+([a-zA-Z0-9_-]+)$",
        cleaned,
        re.IGNORECASE,
    )
    if unpause_match:
        target_bot = unpause_match.group(1).strip()
        from tools.last_word_protocol import unpause_bot
        removed = unpause_bot(msg.channel.id, target_bot)
        if removed:
            await msg.reply(f"▶️ Responses to **{target_bot}** in <#{msg.channel.id}> resumed.")
        else:
            await msg.reply(f"ℹ️ **{target_bot}** was not currently paused in <#{msg.channel.id}>.")
        return True

    agora_halt_match = re.search(r"^(?:!halt|/halt|!stop|/stop)(?:\s+(.*))?$", cleaned.strip(), re.IGNORECASE)
    if agora_halt_match:
        from tools.agora_kill_switch import trigger_kill_switch
        res = trigger_kill_switch(initiator=author_name, action="halt", channel_id=msg.channel.id)
        await msg.reply(res["message"])
        return True

    agora_resume_match = re.search(r"^(?:!resume|/resume|!start|/start)(?:\s+(.*))?$", cleaned.strip(), re.IGNORECASE)
    if agora_resume_match:
        from tools.agora_kill_switch import trigger_kill_switch
        res = trigger_kill_switch(initiator=author_name, action="resume", channel_id=msg.channel.id)
        await msg.reply(res["message"])
        return True

    if is_container_restart_intent(cleaned):
        if msg.author.id != OWNER_USER_ID:
            await msg.reply("⚠️ Administrative container restart commands are restricted to the bot owner.")
            return True
        await execute_container_restart(msg.channel, initiator=author_name, reason="Manual Docker container restart requested via Discord (external channel)")
        return True

    if is_reload_intent(cleaned):
        if msg.author.id != OWNER_USER_ID:
            await msg.reply("⚠️ Administrative bridge commands are restricted to the bot owner.")
            return True
        if reload_fn:
            await reload_fn(msg.channel, initiator=author_name, force=True, reason="Manual in-place bridge reload requested via Discord (external channel)")
        else:
            await execute_bridge_reload(bot, msg.channel, initiator=author_name, force=True, reason="Manual in-place bridge reload requested via Discord (external channel)")
        return True

    # Check for minimal / lazy pings
    is_lazy_pointer = bool(
        not cleaned or
        re.fullmatch(r"[\^\s\.\?!]+", cleaned) or
        cleaned.lower() in ("^", "^^", "^^^", "this", "look", "see", "what?", "check this")
    )
    if is_lazy_pointer and not saved_attachments:
        cleaned = (
            f"[OPERATIONAL DIRECTIVE - LAZY TYPER ADDRESSING]:\n"
            f"The user sent a minimal ping ('{content.strip()}').\n"
            f"Humans are lazy typers: you MUST read back up the recent messages in Discord channel context to identify and address the active topic, question, problem, link, or proposal at hand immediately."
        )

    if saved_attachments:
        image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
        has_images = any(Path(p).suffix.lower() in image_exts for p in saved_attachments)
        image_directive = (
            "\n\n[CRITICAL IMAGE INPUT INVARIANT]:\n"
            "One or more images are attached to this message. You MUST parse and inspect every image (using view_file) and understand its visual contents, error traces, screenshots, or diagrams as part of the primary input, EVEN IF the accompanying text message made no mention of the image."
            if has_images else ""
        )
        hint = "\n\n[Attached file(s) available via view_file tool]:\n" + "\n".join(f"- {p}" for p in saved_attachments)
        if is_lazy_pointer:
            cleaned = (
                f"[OPERATIONAL DIRECTIVE - LAZY TYPER & ATTACHMENT ADDRESSING]:\n"
                f"The user sent an attachment with a minimal prompt ('{content.strip()}').\n"
                "Humans are lazy typers: review the attached image/file(s) and recent channel context to address the active topic directly."
            )
        cleaned = (cleaned or "Please inspect the attached file(s) and assist.") + hint + image_directive

    if not cleaned:
        await msg.reply("What's up? Give me something interesting to work on.")
        return True

    try:
        await msg.channel.typing()
    except Exception:
        pass

    # Group Chat Mid-Turn Steering Check (Channel-scoped)
    target_proc = channel_active_procs.get(msg.channel.id)
    if target_proc is not None and target_proc.returncode is None:
        steering_channels.add(msg.channel.id)
        try:
            target_proc.send_signal(signal.SIGINT)
        except Exception as se:
            print(f"[Bridge] Warning sending SIGINT for external group steering in channel {msg.channel.id}: {se}")

        steer_prompt = (
            f"[MID-TURN GROUP CONVERSATION UPDATE]\n"
            f"While you were drafting your reply, a new message arrived in the channel from {author_name}:\n"
            f"\"{cleaned}\"\n\n"
            f"CRITICAL INSTRUCTIONS FOR REVISED TURN:\n"
            f"1. Absorb this new context immediately. If your in-progress thought is now obsolete, answered, or contradicted, pivot cleanly or yield.\n"
            f"2. If you were emitting a ```handoff block, ensure it is completely valid, closed JSON or omitted entirely.\n"
            f"3. Keep your output concise (strictly under 2,000 chars) and respond naturally to the CURRENT state of the room."
        )
        print(f"[Bridge] Silently steering in-flight external turn for new message from {author_name}")
        await ext_turn_queue.put({
            "prompt": steer_prompt,
            "status_msg": None,
            "reply_target": msg,
            "attachments": saved_attachments,
            "is_steer": True,
            "mode": "external",
            "channel_id": msg.channel.id,
            "author_name": author_name,
            "queued_at": time.perf_counter(),
        })
        return True

    is_last_word = False
    last_word_streak = 0
    if msg.author.bot and not is_banana_watcher and rules.get("last_word_protocol_enabled", True):
        lw_channels = rules.get("last_word_channels", [1534452820995080192])
        if not lw_channels or msg.channel.id in lw_channels:
            try:
                from tools.last_word_protocol import build_last_word_prompt_injection, check_last_word_condition, mark_last_word_in_flight
                lw_thresh = int(rules.get("last_word_threshold", 4))
                is_last_word, last_word_streak = check_last_word_condition(
                    msg.channel.id, msg.author.id, author_name, threshold=lw_thresh
                )
                if is_last_word:
                    lw_mins = int(rules.get("last_word_pause_minutes", 30))
                    cleaned += build_last_word_prompt_injection(author_name, last_word_streak, lw_mins)
                    mark_last_word_in_flight(msg.channel.id, str(msg.author.id))
                    mark_last_word_in_flight(msg.channel.id, author_name)
                    print(f"[Bridge] Last Word Protocol engaged for {author_name} in channel {msg.channel.id} (streak: {last_word_streak}, pause: {lw_mins}m)")
            except Exception as lwe:
                print(f"[Bridge] Error checking Last Word Protocol condition: {lwe}")

    await ext_turn_queue.put({
        "prompt": cleaned,
        "status_msg": None,
        "reply_target": msg,
        "attachments": saved_attachments,
        "is_steer": False,
        "mode": "external",
        "channel_id": msg.channel.id,
        "author_name": author_name,
        "is_last_word": is_last_word,
        "last_word_bot_id": str(msg.author.id) if msg.author.bot else None,
        "last_word_bot_name": author_name if msg.author.bot else None,
        "last_word_streak": last_word_streak,
        "queued_at": time.perf_counter(),
    })
    return True
