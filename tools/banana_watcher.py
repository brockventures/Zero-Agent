#!/usr/bin/env python3
"""
banana_watcher.py - Lightweight Channel Watchdog & Conversation Referee
Operates as @BananaWatcher in #the-banana-stand (1534436119888793750).

Monitors conversation flow, prevents topics from being dropped in silence,
enforces Fast-ACK handoffs, and ensures healthy closure without infinite loops.
"""

import os
import sys
import json
import time
import signal
import argparse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
STATE_FILE = DATA_DIR / "banana_watcher_state.json"
PID_FILE = DATA_DIR / "banana_watcher.pid"
LOG_FILE = DATA_DIR / "banana_watcher.log"

AGORA_CHANNEL_ID = 1558202642663211169
BANANA_STAND_CHANNEL_ID = 1534436119888793750
LOUNGE_CHANNEL_ID = 1534452820995080192
WATCH_CHANNEL_IDS = [AGORA_CHANNEL_ID, BANANA_STAND_CHANNEL_ID]
DEFAULT_ENDPOINT = "https://banana.mikecarmody.net/api"

# Role & Bot IDs for deterministic tagging
BOT_IDS = {
    "amos": "1468012353206354197",
    "marvin": "1492043459618537492",
    "aerial": "1542035925603713086",
    "zero": "1542285964213358633",
}
BANANA_WATCHER_BOT_ID = "1545924520236290198"
ZERO_BOT_ID = BOT_IDS["zero"]
ROLE_ROBOT_ID = "1543462881624858624"   # @Robot
ROLE_ZERO_ID = "1543285916506783799"    # @Zero

# SLA thresholds
STALL_THRESHOLD_SECONDS = 600       # 10 minutes of idle after open topic -> nudge
AUTO_CLOSE_TIMEOUT_SECONDS = 1800   # 30 minutes of idle after open topic -> auto-close/reap
FAST_ACK_TIMEOUT_SECONDS = 120      # 2 minutes of unacknowledged direct handoff -> nudge
LOOP_WARNING_ROUNDS = 30            # 30 turns without terminal state -> nudge to summarize

# Game & Simulation keywords for topics that must remain open during active multi-round events
GAME_TOPIC_KEYWORDS = (
    "agora",
    "trading-floor",
    "trading_floor",
    "combine",
    "tournament",
    "agon",
    "game",
    "match",
    "simulation",
)

# Topics that represent design, meta, specs, or brainstorms rather than active simulation floors
GAME_TOPIC_EXCLUSIONS = (
    "design",
    "pitch",
    "rfc",
    "spec",
    "meta",
    "eval",
    "retrospective",
    "brainstorm",
    "doc",
    "docs",
    "documentation",
    "guide",
    "initiative",
    "portal",
    "review",
    "pr",
    "settled",
)

def is_game_topic(subject: str) -> bool:
    """
    Returns True if the subject represents an active game, simulation, tournament,
    combine, or trading floor (e.g. Station Agora / Operation AGON).
    During games, the floor must remain open across multi-round events without loop warnings,
    stalled-topic nudges, or auto-clamping.
    """
    if not subject:
        return False
    sub_lower = subject.lower()
    if any(ex in sub_lower for ex in GAME_TOPIC_EXCLUSIONS):
        return False
    return any(kw in sub_lower for kw in GAME_TOPIC_KEYWORDS)

def log(msg: str):
    now_str = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S PT")
    line = f"[{now_str}] {msg}"
    print(line, flush=True)

def get_discord_token() -> str:
    token = os.getenv("BANANA_WATCHER_TOKEN")
    if not token and os.path.exists("/secrets/env.json"):
        try:
            with open("/secrets/env.json") as f:
                d = json.load(f)
                token = d.get("BANANA_WATCHER_TOKEN") or d.get("DISCORD_BOT_TOKEN")
        except Exception:
            pass
    if not token:
        token = os.getenv("DISCORD_BOT_TOKEN")
    return token or ""

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "nudged_stalls": {},        # msg_id -> timestamp
        "nudged_handoffs": {},      # msg_id -> timestamp
        "warned_contradictions": {},# msg_id -> timestamp
        "summarized_subjects": {},  # subject_key -> timestamp
        "autoclosed_topics": {},    # msg_id -> timestamp
        "last_seen_msg_id": None,
        "last_mutex_state": {},
        "active_subject": None,
        "active_subject_turns": 0,
        "last_check_ts": None,
    }

def save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        now = time.time()
        for key in ("nudged_stalls", "nudged_handoffs", "warned_contradictions", "summarized_subjects", "autoclosed_topics"):
            if key in state and isinstance(state[key], dict):
                state[key] = {k: v for k, v in state[key].items() if now - v < 172800}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"Error saving state: {e}")

def get_recent_messages(channel_id: int = BANANA_STAND_CHANNEL_ID, limit: int = 15) -> list:
    # Prefer DISCORD_BOT_TOKEN for channel ingestion because Zero's bot holds the privileged
    # Message Content Intent, ensuring peer messages and handoff envelopes are not stripped of text.
    zero_tok = os.getenv("DISCORD_BOT_TOKEN")
    bw_tok = get_discord_token()
    candidates = [c for c in [zero_tok, bw_tok] if c]
    seen = set()
    tokens = [c for c in candidates if not (c in seen or seen.add(c))]

    for tok in tokens:
        for attempt in range(2):
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bot {tok}",
                    "User-Agent": "BananaWatcher/1.0 (CrabCavern; https://github.com/brockventures)"
                }
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        # If this token lacks privileged message content intent, peer messages will have empty content.
                        peer_msgs = [m for m in data if str(m.get("author", {}).get("id")) != BANANA_WATCHER_BOT_ID]
                        # If recent peer messages exist and all of the latest 3 have empty content, this token is stripped.
                        if peer_msgs and all(len(m.get("content", "")) == 0 for m in peer_msgs[:min(3, len(peer_msgs))]):
                            log("Warning: Bot token returned stripped peer messages (missing Message Content Intent). Rejecting data.")
                            break
                        return data
            except urllib.error.HTTPError as he:
                if he.code == 403 and tok != tokens[-1]:
                    break
                log(f"Error fetching channel messages: {he}")
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                log(f"Error fetching channel messages: {e}")
    return []

def post_discord(content: str, channel_id: int = BANANA_STAND_CHANNEL_ID, dry_run: bool = False) -> bool:
    if dry_run:
        log(f"[DRY RUN] Would post to #the-banana-stand:\n{content}")
        return True

    tok = get_discord_token()
    if not tok:
        log("Error: No BANANA_WATCHER_TOKEN available to post.")
        return False

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bot {tok}",
            "Content-Type": "application/json",
            "User-Agent": "BananaWatcher/1.0 (CrabCavern)"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                log(f"Successfully posted to channel #{channel_id} as Banana Watcher ({resp.status})")
                return True
    except Exception as e:
        log(f"Error posting to Discord as Banana Watcher: {e}")
    return False

def parse_envelope_from_content(content: str):
    try:
        from tools.handoff import parse_envelope
        env = parse_envelope(content)
        if env:
            return env
    except Exception:
        pass
    import re
    m = re.search(r"```(?:handoff)?\s*\n?(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None

def parse_iso_timestamp(iso_str: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return time.time()

# Known agents, developers, and broadcast wildcards in Crab Cavern
KNOWN_RECIPIENTS = {
    # Peer bots
    "amos", "marvin", "aerial", "zero", "bananawatcher", "banana_watcher",
    # Developers & humans
    "mike", "arbiter", "alex", "arcane", "ian", "themoonproblem", "moon problem", "dr. coley", "dr_coley", "coley", "ryan", "harper", "harperwallbanger",
    # Broadcast wildcards & team roles (ratified v1.1)
    "team", "all", "*", "robot", "robots",
}

RATIFIED_KINDS = {
    "finding", "question", "answer", "handoff", "correction", "status", "proposal", "consensus", "summary"
}

KIND_ALIASES = {
    "resolution": "status",
    "review": "finding",
    "update": "status",
}

def normalize_subject(subj: str) -> str:
    """Normalize subject to its base slug before any subtitle colon."""
    if not subj:
        return ""
    base = str(subj).split(":")[0].strip()
    return base.lower()

def subjects_match(s1: str, s2: str) -> bool:
    """Check if two subject strings refer to the same base discussion."""
    if not s1 or not s2:
        return False
    str1 = str(s1).strip()
    str2 = str(s2).strip()
    if str1.lower() == str2.lower():
        return True
    return normalize_subject(str1) == normalize_subject(str2)

def analyze_envelope_contradictions(env: dict, subject_turns: int = 1) -> list:
    """
    Scans a handoff envelope for protocol contradictions.
    Returns a list of dicts: [{"tag": str, "title": str, "detail": str, "fix": str}]
    """
    contradictions = []

    raw_kind = str(env.get("kind") or "").lower().strip()
    kind = KIND_ALIASES.get(raw_kind, raw_kind)
    floor = str(env.get("floor") or "open").lower().strip()
    reply = str(env.get("reply") or "optional").lower().strip()
    subject = str(env.get("subject") or "").strip()

    # Extract all recipient targets (string or list, supporting v1.1)
    to_raw = env.get("to") if env.get("to") is not None else env.get("target")
    targets = []
    if isinstance(to_raw, list):
        for item in to_raw:
            if isinstance(item, str):
                targets.extend(p.strip().lower().lstrip("<@").rstrip(">").lstrip("@") for p in item.split(",") if p.strip())
    elif isinstance(to_raw, str):
        targets.extend(p.strip().lower().lstrip("<@").rstrip(">").lstrip("@") for p in to_raw.split(",") if p.strip())

    # 1. Premature / Contradictory Floor Closure (e.g. kind: status with floor: closed on multi-turn thread)
    # Setting floor: closed halts conversation across all bots (Tier.SILENT) without reaching consensus/resolution,
    # freezing the topic in limbo.
    interim_kinds = ("status", "finding", "question", "proposal", "correction", "answer", "handoff")
    is_parked_subject = subject.lower().startswith("parked:") or subject.lower().startswith("parked ")
    if floor == "closed" and kind in interim_kinds and subject_turns >= 2 and not is_parked_subject and raw_kind != "resolution":
        contradictions.append({
            "tag": "premature_closure",
            "title": f"`floor: closed` with interim `kind: {kind}`",
            "detail": f"Floor was closed on `{subject or 'unnamed topic'}`, but tagged with interim `{kind}`.",
            "fix": f"If this discussion is resolved, clamp with `kind: consensus` or `kind: summary`. If parked without resolution, prefix subject with `parked:` and set `reply: none`."
        })

    # 2. Deadlock Handoff (reply: required/baton with floor: closed)
    # Direct deadlock: the target is commanded to reply under Fast-ACK SLA, but forbidden to speak because the floor is closed.
    if floor == "closed" and reply in ("required", "baton"):
        contradictions.append({
            "tag": "deadlock_handoff",
            "title": f"`reply: {reply}` with `floor: closed`",
            "detail": f"A reply was required on `{subject or 'unnamed topic'}`, but the floor was closed.",
            "fix": f"Peers cannot reply to a closed floor. Either set `reply: none` or reopen the floor (`floor: open`)."
        })

    # 3. Open Terminal State (terminal kind with floor: open or reply: required/baton)
    # Declaring a terminal kind (consensus/resolution/summary) while leaving the floor open or requiring replies
    # invites post-resolution debate loops.
    terminal_kinds = ("consensus", "summary")
    if kind in terminal_kinds and (floor == "open" or reply in ("required", "baton")):
        contradictions.append({
            "tag": "open_terminal",
            "title": f"Terminal `kind: {kind}` with open floor / required reply",
            "detail": f"`{subject or 'unnamed topic'}` was declared `{kind}`, but `floor` is open or `reply: {reply}`.",
            "fix": f"Terminal topics must be clamped with `floor: closed` and `reply: none` to prevent debate loops."
        })

    # 4. Unaddressed Required Reply (Orphaned Baton)
    # Requiring a reply without targeting an agent creates bystander apathy or simultaneous claim collisions.
    is_unaddressed = not targets or all(t in ("null", "none", "") for t in targets)
    if reply in ("required", "baton") and is_unaddressed and floor != "closed":
        contradictions.append({
            "tag": "unaddressed_baton",
            "title": f"`reply: {reply}` without specific target",
            "detail": f"`reply: {reply}` was requested on `{subject or 'unnamed topic'}` without designating a specific agent owner (`to: <agent>`).",
            "fix": f"Fast-ACK SLA requires a designated owner (`to: <agent>`). If broadcasting ambiently, use `reply: optional`."
        })

    # 5. Unanswerable Question (kind: question with reply: none)
    # Posing a question while commanding everyone not to reply.
    if kind == "question" and reply == "none":
        contradictions.append({
            "tag": "unanswerable_question",
            "title": f"`kind: question` with `reply: none`",
            "detail": f"A question was asked on `{subject or 'unnamed topic'}`, but `reply: none` forbids peers from answering.",
            "fix": f"If soliciting answers, use `reply: optional` or `reply: required`."
        })

    # 6. Yielded Baton Pass (kind: handoff with reply: none)
    # Passing the baton (kind: handoff) while forbidding a reply.
    if kind == "handoff" and reply == "none":
        contradictions.append({
            "tag": "yielded_baton_pass",
            "title": f"`kind: handoff` with `reply: none`",
            "detail": f"Execution was handed off on `{subject or 'unnamed topic'}`, but `reply: none` tells the target not to answer.",
            "fix": f"Handoffs require a target (`to: <agent>`) and `reply: required` or `reply: baton`."
        })

    # 7. Governor Limit Breach Without Clamp (round >= max_rounds without terminal clamp)
    # Exclude active game / trading floor topics where rounds represent simulation checkpoints rather than conversation loop governors.
    try:
        round_val = int(env.get("round", 0))
        max_rounds_val = int(env.get("max_rounds", 0))
        if not is_game_topic(subject) and round_val > 0 and max_rounds_val > 0 and round_val >= max_rounds_val:
            if floor != "closed" or reply != "none" or kind not in terminal_kinds:
                contradictions.append({
                    "tag": "governor_breach",
                    "title": f"Round budget reached ({round_val}/{max_rounds_val}) without terminal clamp",
                    "detail": f"Topic `{subject or 'unnamed topic'}` reached max rounds ({round_val}/{max_rounds_val}), but was not clamped.",
                    "fix": f"Circuit breaker requires terminal clamp: `floor: closed`, `reply: none`, `kind: summary/consensus`."
                })
    except (ValueError, TypeError):
        pass

    # 8. Game / Trading Floor Floor Closure
    # Live game, tournament, combine, or trading floor topics must keep floor: open while simulation rounds are active.
    if is_game_topic(subject) and floor == "closed" and kind not in ("final", "game_over", "final_bell"):
        contradictions.append({
            "tag": "game_floor_closed",
            "title": f"`floor: closed` during active game/trading floor `{subject}`",
            "detail": f"The floor was closed on active game/trading floor topic `{subject}`. Game floors must remain open (`floor: open`) while simulation rounds are active.",
            "fix": "Set `floor: open` to ensure trading and strategy rounds continue without interruption."
        })

    # 9. Non-Existent or Unregistered Recipient Check
    # Addressing messages to non-existent bots or unregistered handles drops messages on the floor
    # and leaves turns in limbo because no runtime is listening to wake up.
    if targets:
        unknown_targets = [t for t in targets if t not in KNOWN_RECIPIENTS and not t.isdigit() and t not in ("null", "none")]
        if unknown_targets:
            bad_names = ", ".join(f"`{t}`" for t in unknown_targets)
            contradictions.append({
                "tag": "unknown_recipient",
                "title": f"Unknown recipient: {bad_names}",
                "detail": f"Envelope targeted {bad_names}, which is not a registered Crab Cavern agent, human dev, or valid broadcast wildcard.",
                "fix": "Target registered bots (`amos`, `marvin`, `aerial`, `zero`), human devs (`ryan`, `alex`, `mike`, `dr. coley`), or valid wildcards (`team`, `all`, `*`)."
            })

    # 10. Unratified Enum Verb (Invalid kind)
    # Using non-standard kinds (e.g. kind: "task", "bug") triggers schema validation exceptions in peer parsers (Pydantic/marshmallow).
    if raw_kind and raw_kind not in RATIFIED_KINDS and raw_kind not in KIND_ALIASES and not is_game_topic(subject):
        contradictions.append({
            "tag": "unratified_kind",
            "title": f"Unratified envelope `kind: {raw_kind}`",
            "detail": f"Envelope uses non-standard `kind: {raw_kind}`, which causes schema validation failures in peer runtimes.",
            "fix": "Use a ratified v1.1 kind verb: `finding`, `question`, `answer`, `handoff`, `correction`, `status`, `proposal`, `consensus`, `summary` (or ratified aliases: `resolution`->`status`, `review`->`finding`, `update`->`status`)."
        })

    return contradictions

def check_channel_and_evaluate(channel_id: int = BANANA_STAND_CHANNEL_ID, dry_run: bool = False) -> list:
    state = load_state()
    messages = get_recent_messages(channel_id=channel_id, limit=15)
    if not messages:
        return []

    actions = []
    now = time.time()

    chrono_msgs = list(reversed(messages))
    latest_channel_msg = chrono_msgs[-1] if chrono_msgs else {}
    latest_channel_ts = parse_iso_timestamp(latest_channel_msg.get("timestamp", ""))
    channel_idle = now - latest_channel_ts

    envelopes_with_msg = []
    for m in chrono_msgs:
        env = parse_envelope_from_content(m.get("content", ""))
        if env:
            envelopes_with_msg.append((m, env))

    if not envelopes_with_msg:
        return actions

    latest_env_msg, latest_env = envelopes_with_msg[-1]
    subject = str(latest_env.get("subject") or "").strip()
    raw_latest_kind = str(latest_env.get("kind") or "").lower().strip()
    kind = KIND_ALIASES.get(raw_latest_kind, raw_latest_kind)
    floor = str(latest_env.get("floor") or "open").lower().strip()
    reply = str(latest_env.get("reply") or "optional").lower().strip()
    to_agent = str(latest_env.get("to") or latest_env.get("target") or "").lower().strip()
    env_msg_id = str(latest_env_msg.get("id"))
    env_ts = parse_iso_timestamp(latest_env_msg.get("timestamp", ""))
    elapsed = now - env_ts

    # Check live Banana API mutex holder if available
    active_mutex_holder = None
    try:
        from tools.banana import get_status as get_banana_status
        b_status = get_banana_status()
        active_mutex_holder = b_status.get("holder")
    except Exception:
        pass

    # A topic is only terminal if floor is explicitly closed OR explicit consensus/resolution/summary without open floor.
    # Interim research findings (kind == "finding") are never terminal.
    is_terminal = (floor == "closed") or (kind in ("consensus", "resolution", "summary") and floor != "open")

    # Count turns on this subject
    subject_turns = sum(1 for m, e in envelopes_with_msg if subjects_match(e.get("subject"), subject))

    # 0. Handoff Envelope Contradiction Warnings
    # If the latest envelope contains contradictory directives, warn the emitting author immediately.
    # Exclude Banana Watcher itself.
    author_info = latest_env_msg.get("author", {})
    author_id = str(author_info.get("id") or "")
    author_name = author_info.get("username", "peer")

    if author_id != BANANA_WATCHER_BOT_ID and env_msg_id not in state.get("warned_contradictions", {}):
        contradictions = analyze_envelope_contradictions(latest_env, subject_turns=subject_turns)
        if contradictions:
            author_tag = f"<@{author_id}> (@{author_name.title()})" if author_id in BOT_IDS.values() else (f"<@{author_id}>" if author_id else f"@{author_name}")
            warn_lines = [f"🍌 **Handoff Contradiction Detected** on `{subject or 'unnamed'}`:"]
            warn_lines.append(f"{author_tag}, your handoff envelope contains contradictory directives:")
            for c in contradictions:
                warn_lines.append(f"• **{c['title']}**: {c['detail']}\n  ↳ *Guidance:* {c['fix']}")
            warn_msg = "\n".join(warn_lines)

            tags_str = ", ".join(c["tag"] for c in contradictions)
            actions.append(f"Contradiction warning to {author_name} on {subject}: [{tags_str}]")
            if post_discord(warn_msg, dry_run=dry_run):
                if not dry_run:
                    state.setdefault("warned_contradictions", {})[env_msg_id] = now
                    save_state(state)

    # 1. Stalled & Inactive Topic Management: Floor open, non-terminal kind, channel idle, and no active mutex holder
    # Exclude headless heartbeats, pings, pure handshakes, or live game/simulation topics
    norm_subj = normalize_subject(subject)
    concluded_key = f"concluded-{norm_subj}"
    is_already_concluded = (
        concluded_key in state.get("summarized_subjects", {})
        or f"concluded-{subject}" in state.get("summarized_subjects", {})
        or subject in state.get("concluded_subjects", {})
        or norm_subj in state.get("concluded_subjects", {})
    )

    closing_keywords = ("park it", "parking it", "already parked", "floor is closed", "wrap when ready")
    latest_content_lower = (latest_env_msg.get("content") or "").lower() if latest_env_msg else ""
    ended_on_park = any(kw in latest_content_lower for kw in closing_keywords)

    subject_already_resolved = any(
        subjects_match(e.get("subject"), subject) and (
            str(e.get("floor") or "").lower() == "closed" or
            str(e.get("kind") or "").lower() in ("resolution", "consensus")
        )
        for m, e in envelopes_with_msg
    )

    is_stallable = (
        bool(subject)
        and not is_terminal
        and not is_already_concluded
        and not ended_on_park
        and not (subject_already_resolved and kind not in ("proposal", "question"))
        and not is_game_topic(subject)
        and kind not in ("heartbeat", "handshake", "ping")
        and not (kind == "status" and reply == "none")
    )
    if is_stallable and not active_mutex_holder:
        # 1b. Hard Reap / Auto-Close: Entire channel idle >= 30 mins -> auto-clamp topic
        if channel_idle >= AUTO_CLOSE_TIMEOUT_SECONDS:
            if env_msg_id not in state.get("autoclosed_topics", {}):
                mins = int(channel_idle // 60)
                concluded_key = f"concluded-{norm_subj}"
                # If topic had substantive turns and ended on a deploy status, progress report, or verified evidence
                ended_on_delivery = (
                    subject_turns >= 2
                    and (
                        kind in ("status", "finding")
                        or bool(latest_env.get("evidence"))
                    )
                )

                if ended_on_delivery and concluded_key not in state.get("summarized_subjects", {}):
                    # Check dedupe guard before prompting
                    closing_content = (latest_env_msg.get("content") or "").lower() if latest_env_msg else ""
                    already_dispatched = (
                        "executive summary dispatched" in closing_content
                        or "summary dispatched to #lounge" in closing_content
                        or "summary dispatched to lounge" in closing_content
                    )
                    if not already_dispatched:
                        try:
                            from tools.outbox import is_duplicate_outbox_message
                            is_dup, _ = is_duplicate_outbox_message("lounge", f"Executive Summary: {subject}")
                            if is_dup:
                                already_dispatched = True
                        except Exception:
                            pass

                    if already_dispatched:
                        actions.append(f"Auto-closed topic with summary already dispatched: {subject}")
                        if not dry_run:
                            state.setdefault("autoclosed_topics", {})[env_msg_id] = now
                            state.setdefault("nudged_stalls", {})[env_msg_id] = now
                            state.setdefault("summarized_subjects", {})[concluded_key] = now
                            save_state(state)
                    else:
                        close_msg = (
                            f"🍌 **Topic Concluded (30m Inactivity)**: Topic `{subject}` ended on `{kind}` with verified state.\n"
                            f"Auto-clamping floor to closed."
                        )
                        actions.append(f"Auto-closed topic: {subject} ({mins}m idle)")
                        if post_discord(close_msg, channel_id=channel_id, dry_run=dry_run):
                            if not dry_run:
                                state.setdefault("autoclosed_topics", {})[env_msg_id] = now
                                state.setdefault("nudged_stalls", {})[env_msg_id] = now
                                state.setdefault("summarized_subjects", {})[concluded_key] = now
                                save_state(state)
                elif concluded_key not in state.get("summarized_subjects", {}):
                    # Open proposal or debate without resolution: auto-park it to clean floor without #lounge spam
                    close_msg = (
                        f"🍌 **Topic Timed Out (30m Inactivity)**: Topic `{subject}` has been idle for {mins}m without consensus or working code.\n"
                        f"Auto-parking topic and clamping floor.\n\n"
                        f"```handoff\n"
                        f"{{\n"
                        f'  "v": 0,\n'
                        f'  "kind": "status",\n'
                        f'  "floor": "closed",\n'
                        f'  "reply": "none",\n'
                        f'  "subject": "{subject}"\n'
                        f"}}\n"
                        f"```"
                    )
                    actions.append(f"Auto-parked timed out topic: {subject} ({mins}m idle)")
                    if post_discord(close_msg, channel_id=channel_id, dry_run=dry_run):
                        if not dry_run:
                            state.setdefault("autoclosed_topics", {})[env_msg_id] = now
                            state.setdefault("nudged_stalls", {})[env_msg_id] = now
                            state.setdefault("summarized_subjects", {})[concluded_key] = now
                            save_state(state)

        # 1a. Soft Nudge: Floor open, idle >= 10 mins (and < 30 mins)
        elif channel_idle >= STALL_THRESHOLD_SECONDS:
            stall_subject_key = f"stall-{subject}"
            last_stall_nudge = state.get("nudged_stalls", {}).get(stall_subject_key, 0)
            if (now - last_stall_nudge) > 1800 and env_msg_id not in state.get("nudged_stalls", {}):
                mins = int(channel_idle // 60)

                # Determine single designated closer instead of broadcasting to @robot
                topic_initiator_id = None
                for m, e in envelopes_with_msg:
                    if subjects_match(e.get("subject"), subject):
                        auth_id = str(m.get("author", {}).get("id") or "")
                        if auth_id in BOT_IDS.values():
                            topic_initiator_id = auth_id
                            break

                target_bot_id = topic_initiator_id or (author_id if author_id in BOT_IDS.values() else ZERO_BOT_ID)
                target_bot_name = next((k for k, v in BOT_IDS.items() if v == target_bot_id), "team")

                nudge_msg = (
                    f"🍌 **Topic Stalled**: Topic `{subject}` has been quiet for {mins}m without a resolution or ticket.\n"
                    f"<@{target_bot_id}> (@{target_bot_name.title()}): Is this ready to land in a PR/task, or are we parking it?"
                )
                actions.append(f"Stalled topic nudge: {subject} ({mins}m idle) [target: {target_bot_name}]")
                if post_discord(nudge_msg, channel_id=channel_id, dry_run=dry_run):
                    if not dry_run:
                        state.setdefault("nudged_stalls", {})[env_msg_id] = now
                        state.setdefault("nudged_stalls", {})[stall_subject_key] = now
                        save_state(state)

    # 1c. Structured discussion completion: if round >= max_rounds, auto-prompt summary after 2m idle
    is_round_complete = False
    try:
        env_round = int(latest_env.get("round") or 0)
        env_max_rounds = int(latest_env.get("max_rounds") or 0)
        if env_round > 0 and env_max_rounds > 0 and env_round >= env_max_rounds:
            is_round_complete = True
    except (ValueError, TypeError):
        pass

    if is_round_complete and not is_game_topic(subject) and not is_terminal and not is_already_concluded:
        # Discussion reached its max rounds. If idle for 2 minutes (120s), automatically conclude without 10m stall
        if channel_idle >= 120 and concluded_key not in state.get("summarized_subjects", {}):
            closing_content = (latest_env_msg.get("content") or "").lower() if latest_env_msg else ""
            already_dispatched = (
                "executive summary dispatched" in closing_content
                or "summary dispatched to #lounge" in closing_content
                or "summary dispatched to lounge" in closing_content
            )
            if not already_dispatched:
                try:
                    from tools.outbox import is_duplicate_outbox_message
                    is_dup, _ = is_duplicate_outbox_message("lounge", f"Executive Summary: {subject}")
                    if is_dup:
                        already_dispatched = True
                except Exception:
                    pass

            stall_subject_key = f"stall-{subject}"
            if already_dispatched:
                actions.append(f"Auto-closed completed rounds topic with summary already dispatched: {subject}")
                if not dry_run:
                    state.setdefault("autoclosed_topics", {})[env_msg_id] = now
                    state.setdefault("nudged_stalls", {})[env_msg_id] = now
                    state.setdefault("nudged_stalls", {})[stall_subject_key] = now
                    state.setdefault("summarized_subjects", {})[concluded_key] = now
                    save_state(state)
            else:
                summary_msg = (
                    f"🍌 **Discussion Concluded (Max Rounds Reached)**: Topic `{subject}` reached final round {env_round}/{env_max_rounds}.\n"
                    f"Floor is clamped to closed."
                )
                actions.append(f"Discussion concluded on max rounds reached: {subject}")
                if post_discord(summary_msg, channel_id=channel_id, dry_run=dry_run):
                    if not dry_run:
                        state.setdefault("autoclosed_topics", {})[env_msg_id] = now
                        state.setdefault("nudged_stalls", {})[env_msg_id] = now
                        state.setdefault("nudged_stalls", {})[stall_subject_key] = now
                        state.setdefault("summarized_subjects", {})[concluded_key] = now
                        save_state(state)

    # 2. Fast-ACK SLA Nudge: Direct handoff to specific bot with reply: required/baton
    if to_agent and to_agent in BOT_IDS and reply in ("required", "baton") and not is_terminal:
        target_bot_id = BOT_IDS[to_agent]
        target_has_replied = False
        for m in messages:
            m_ts = parse_iso_timestamp(m.get("timestamp", ""))
            m_author = m.get("author", {}).get("username", "").lower()
            m_author_id = str(m.get("author", {}).get("id", ""))
            if m_ts > env_ts and (to_agent in m_author or m_author_id == target_bot_id):
                target_has_replied = True
                break

        if not target_has_replied and elapsed >= FAST_ACK_TIMEOUT_SECONDS and active_mutex_holder != to_agent:
            if env_msg_id not in state.get("nudged_handoffs", {}):
                sender = latest_env_msg.get("author", {}).get("username", "peer")
                nudge_msg = (
                    f"🍌 **Fast-ACK SLA**: <@{target_bot_id}> (@{to_agent.title()}), you have an open handoff from {sender} on `{subject}`.\n"
                    f"Please emit Fast-ACK status or yield the floor."
                )
                actions.append(f"Fast-ACK nudge to {to_agent} on {subject}")
                if post_discord(nudge_msg, channel_id=channel_id, dry_run=dry_run):
                    if not dry_run:
                        state.setdefault("nudged_handoffs", {})[env_msg_id] = now
                        save_state(state)

    # 3. Echo-Loop / Turn Count Rule: Count turns on this subject
    # Game / simulation / trading-floor topics (e.g. Agora, combine, tournament) are live multi-round events
    # and MUST NOT trigger loop warnings or prompts to close the floor.
    if subject and not is_game_topic(subject) and subject_turns >= LOOP_WARNING_ROUNDS and not is_terminal:
        loop_key = f"loop-{subject}"
        last_loop_nudge = state.get("nudged_stalls", {}).get(loop_key, 0)
        # Nudge once, then latch for at least 1 hour (3600s) to avoid cascading nags on every turn
        if (now - last_loop_nudge) > 3600:
            nudge_msg = (
                f"🍌 **Loop Warning**: Topic `{subject}` has reached {subject_turns} turns without a terminal consensus.\n"
                f"<@&{ROLE_ROBOT_ID}> (@robot): Please clamp with a terminal summary, task ticket, or close the floor to avoid loop contention."
            )
            actions.append(f"Loop warning on {subject} ({subject_turns} turns)")
            if post_discord(nudge_msg, channel_id=channel_id, dry_run=dry_run):
                if not dry_run:
                    state.setdefault("nudged_stalls", {})[loop_key] = now
                    save_state(state)

    # 4. Concluded Discussion Executive Summary Prompt (DEPRECATED)
    # Executive summaries to #lounge are now handled on a periodic batched digest cadence
    # via tools/lounge_digest.py every 2 hours to avoid noisy per-PR/per-issue spam in #lounge.

    if actions or (now - (state.get("last_check_ts") or 0) >= 300):
        state["last_check_ts"] = now
        if not dry_run:
            save_state(state)

    return actions

INTRO_MESSAGE = (
    "🍌 **Banana Watcher is online.**\n\n"
    "I am monitoring `#the-banana-stand` for conversation flow health:\n"
    "• **Fast-ACK SLA (120s):** Direct handoffs (`reply: required/baton`) require an explicit status ACK or floor yield.\n"
    "• **Stalled Topics (10m):** Open proposals or active topics will be nudged for PR/task landing.\n"
    "• **Auto-Close TTL (30m):** Topics idle for 30m are auto-clamped (reaped or concluded) to prevent zombie floors.\n"
    "• **Loop Breaker (30 turns):** Extended unclosed debates will be prompted to summarize and close.\n\n"
    "Floor is open. Carry on."
)

def run_gateway_daemon(dry_run: bool = False, interval: int = 30, send_intro: bool = False):
    log(f"Starting BananaWatcher Gateway daemon (interval={interval}s, dry_run={dry_run}, intro={send_intro})...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    token = get_discord_token()
    if not token:
        log("Error: No BANANA_WATCHER_TOKEN available.")
        return

    import discord
    import asyncio

    intents = discord.Intents.default()
    intents.message_content = False

    client = discord.Client(intents=intents)

    async def watchdog_task():
        await client.wait_until_ready()
        ch = client.get_channel(BANANA_STAND_CHANNEL_ID)
        if not ch:
            log(f"Warning: Channel #{BANANA_STAND_CHANNEL_ID} not accessible by Banana Watcher.")
            return

        if send_intro:
            try:
                await ch.send(INTRO_MESSAGE)
                log("Successfully posted intro beacon to #the-banana-stand.")
            except Exception as e:
                log(f"Error posting intro beacon: {e}")

        last_heartbeat_log = time.time()
        while not client.is_closed():
            now_loop = time.time()
            if now_loop - last_heartbeat_log >= 3600:
                log("Banana Watcher heartbeat: gateway connected and monitoring #the-banana-stand.")
                last_heartbeat_log = now_loop
            for ch_id in WATCH_CHANNEL_IDS:
                try:
                    actions = check_channel_and_evaluate(channel_id=ch_id, dry_run=dry_run)
                    if actions:
                        log(f"Evaluation actions for #{ch_id}: {actions}")
                except Exception as e:
                    log(f"Error in evaluation cycle for #{ch_id}: {e}")
            await asyncio.sleep(interval)

    @client.event
    async def on_ready():
        log(f"Banana Watcher connected and 🟢 ONLINE as {client.user} (ID: {client.user.id})")
        try:
            await client.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name="#agora & #the-banana-stand 🍌"),
                status=discord.Status.online
            )
        except Exception as pe:
            log(f"Warning setting presence: {pe}")
        asyncio.create_task(watchdog_task())

    def _handle_exit(signum, frame):
        log(f"Received signal {signum}, stopping Gateway client cleanly...")
        try:
            asyncio.create_task(client.close())
        except Exception:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

    try:
        client.run(token, log_handler=None)
    except Exception as e:
        log(f"Gateway client exited: {e}")
    finally:
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except Exception:
                pass
        log("BananaWatcher stopped.")

def get_daemon_status() -> dict:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            status_path = Path(f"/proc/{pid}/status")
            if cmdline_path.exists() and status_path.exists():
                cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
                status_text = status_path.read_text(encoding="utf-8", errors="ignore")
                tgid_lines = [line.split()[1] for line in status_text.splitlines() if line.startswith("Tgid:")]
                if (tgid_lines and int(tgid_lines[0]) != pid) or "banana_watcher" not in cmdline:
                    PID_FILE.unlink(missing_ok=True)
                    return {"running": False, "pid": None}
            return {"running": True, "pid": pid}
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
    return {"running": False, "pid": None}

def start_daemon(interval: int = 30, intro: bool = False) -> dict:
    st = get_daemon_status()
    if st["running"]:
        return {"ok": True, "status": "already_running", "pid": st["pid"]}

    import subprocess
    log_f = open(LOG_FILE, "a")
    cmd = [sys.executable, str(Path(__file__).resolve()), "_daemon", f"--interval={interval}"]
    if intro:
        cmd.append("--intro")
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid))
    time.sleep(0.5)
    st = get_daemon_status()
    return {"ok": True, "status": "started", "pid": st["pid"] or proc.pid}

def stop_daemon() -> dict:
    st = get_daemon_status()
    if not st["running"]:
        return {"ok": True, "status": "not_running"}
    try:
        os.kill(st["pid"], signal.SIGTERM)
        for _ in range(15):
            time.sleep(0.2)
            try:
                os.kill(st["pid"], 0)
            except OSError:
                break
        else:
            os.kill(st["pid"], signal.SIGKILL)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:
            pass
    return {"ok": True, "status": "stopped", "pid": st["pid"]}

def ensure_banana_watcher_running():
    st = get_daemon_status()
    if not st["running"]:
        start_daemon()

def main():
    parser = argparse.ArgumentParser(description="BananaWatcher Daemon")
    parser.add_argument("command", choices=["start", "stop", "status", "run-once", "intro", "_daemon"], default="status", nargs="?")
    parser.add_argument("--intro", action="store_true", help="Send intro message on start")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without posting to Discord")
    parser.add_argument("--interval", type=int, default=30, help="Polling interval in seconds (default: 30)")
    args = parser.parse_args()

    if args.command == "status":
        st = get_daemon_status()
        state = load_state()
        last_check_str = "Never"
        if state.get("last_check_ts"):
            ago = int(time.time() - state["last_check_ts"])
            last_check_str = f"{ago}s ago"
        print("🍌 BananaWatcher Status:")
        print(f"  Daemon Running: {st['running']} (PID: {st['pid']})")
        print(f"  Channels: #{AGORA_CHANNEL_ID} (#agora) & #{BANANA_STAND_CHANNEL_ID} (#the-banana-stand)")
        print(f"  Last Evaluation: {last_check_str}")
        print(f"  Tracked Stalls: {len(state.get('nudged_stalls', {}))}")
        print(f"  Tracked Auto-Closes: {len(state.get('autoclosed_topics', {}))}")
        print(f"  Tracked Handoffs: {len(state.get('nudged_handoffs', {}))}")
        print(f"  Tracked Contradictions: {len(state.get('warned_contradictions', {}))}")
        print(f"  Tracked Summaries: {len(state.get('summarized_subjects', {}))}")
    elif args.command == "run-once":
        actions = check_channel_and_evaluate(dry_run=args.dry_run)
        print(f"Run-once complete. Actions triggered: {actions}")
    elif args.command == "intro":
        res = post_discord(INTRO_MESSAGE)
        print(f"Intro beacon posted: {res}")
    elif args.command == "start":
        res = start_daemon(interval=args.interval, intro=args.intro)
        if res.get("status") == "already_running":
            print(f"BananaWatcher already running with PID {res['pid']}.")
        else:
            print(f"Started BananaWatcher Gateway daemon (PID: {res['pid']}).")
    elif args.command == "_daemon":
        run_gateway_daemon(dry_run=args.dry_run, interval=args.interval, send_intro=args.intro)
    elif args.command == "stop":
        res = stop_daemon()
        if res.get("status") == "not_running":
            print("BananaWatcher is not running.")
            sys.exit(0)
        elif not res.get("ok"):
            print(f"Error stopping process: {res.get('error')}")
        else:
            print(f"Sent SIGTERM to BananaWatcher (PID {res['pid']}).")

if __name__ == "__main__":
    main()
