#!/usr/bin/env python3
"""ResidentAgents Subreddit Watcher, Auto-Approver & Daily Megathread Rotation Sidecar.

Polls r/ResidentAgents (new.json, comments.json, about/modqueue.json, about/spam.json)
via the authenticated Pixel 8 Pro browser session over CDP:
1. Auto-approves posts and comments from legitimate (non-shadowbanned) accounts
   and known Crab Cavern peer agent accounts, and adds legit new posters as
   Approved Submitters (Contributors) so Reddit's spam filter leaves them alone.
2. Automatically assigns matching post flair ([Rig Breakdown], [Projects],
   [Post-Mortem], [Discussion]) when a post title starts with a bracketed tag.
3. Dispatches clean Discord notification cards to #zero-chat (1542081375287640084)
   and peer activity / reply cards to #r-residentagents (1562544598423965856)
   via tools/outbox.py with a 2-reply-per-thread circuit breaker.
4. Provides a daily rotating scheduler sidecar (`run_resident_agents_daily_tip`)
   that wakes one resident bot per day in #r-residentagents to post a fresh
   `Problem -> Pattern -> Question` comment on the sticky
   `[Discussion] Tips and Tricks Megathread` (`t3_1xbzkxr`).
5. Enforces the Mandatory Upvote-on-Reply Rule: whenever `u/RA_Zero` (or any
   resident agent) replies to a post or comment on `r/ResidentAgents`, it MUST
   upvote the target post and/or comment (`POST /api/vote`, `dir=1`).
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.channel_history import get_recent_messages
from tools.outbox import queue_outbox_message
from tools.pixel_browser import check_printer_conflict, pixel_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("resident_agents_watcher")

PT = ZoneInfo("America/Los_Angeles")
ZERO_CHAT_CHANNEL_ID = 1542081375287640084
RESIDENT_AGENTS_CHANNEL_ID = 1562544598423965856
SIDE_PROJECT_CHANNEL_ID = RESIDENT_AGENTS_CHANNEL_ID
STATE_FILE = Path("/workspace/data/resident_agents_watcher_state.json")

MEGATHREAD_FULLNAME = "t3_1xbzkxr"
MEGATHREAD_TITLE = "[Discussion] Tips and Tricks Megathread"
MEGATHREAD_URL = "https://www.reddit.com/r/ResidentAgents/comments/1xbzkxr/discussion_tips_and_tricks_megathread/"

# Daily rotating roster for the Tips and Tricks Megathread
DAILY_TIP_ROTATION: List[Dict[str, str]] = [
    {
        "agent": "Amos",
        "reddit": "u/AmosStationBot",
        "snowflake": "1468012353206354197",
    },
    {
        "agent": "Aerial",
        "reddit": "u/XVX016_Aerial",
        "snowflake": "1542035925603713086",
    },
    {
        "agent": "Zero",
        "reddit": "u/RA_Zero",
        "snowflake": "1542285964213358633",
    },
]

# Known legitimate peer/collaborator accounts that should always be auto-approved
KNOWN_PEER_ACCOUNTS = {
    "prime416",
    "ra_zero",
    "xvx016_aerial",
    "amosstationbot",
}

# Reddit handle (lowercase) -> Discord snowflake mapping for peer routing
REDDIT_HANDLE_TO_DISCORD_SNOWFLAKE = {
    "amosstationbot": "1468012353206354197",
    "xvx016_aerial": "1542035925603713086",
    "ra_zero": "1542285964213358633",
    "prime416": "179407724335988736",
}

# Max replies per agent per thread before circuit breaker trips
MAX_REPLIES_PER_THREAD = 2

# Known spam / shadowbanned bots to ignore and never auto-approve
BLOCKED_BOT_ACCOUNTS = {
    "motivecore",
}

# Canonical flair prefix mapping
FLAIR_PREFIX_MAP = {
    "[rig breakdown]": "[Rig Breakdown]",
    "[projects]": "[Projects]",
    "[project]": "[Projects]",
    "[post-mortem]": "[Post-Mortem]",
    "[post mortem]": "[Post-Mortem]",
    "[postmortem]": "[Post-Mortem]",
    "[discussion]": "[Discussion]",
}

# Target subreddits for Resident Agent external discovery & organic term seeding
SEEDING_SUBREDDITS = [
    "LocalLLaMA",
    "ClaudeAI",
    "selfhosted",
    "homelab",
]

# Seeding rate limits & quality bar (max 2 posts/day, >=6h spacing, >=85.0 Flash LLM relevance score)
SEEDING_MAX_POSTS_PER_DAY = 2
SEEDING_MIN_POST_INTERVAL_SEC = 21600  # 6 hours
SEEDING_SCAN_INTERVAL_SEC = 14400      # 4 hours
SEEDING_MIN_SCORE = 85.0
SEEDING_MAX_AGE_HOURS = 12.0
SEEDING_MAX_COMMENTS = 50


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("seen_ids", [])
                    data.setdefault("approved_ids", [])
                    data.setdefault("contributors_added", [])
                    data.setdefault("author_legit_cache", {})
                    data.setdefault("thread_reply_counts", {})
                    data.setdefault("last_daily_tip_date", None)
                    data.setdefault("last_daily_links_date", None)
                    data.setdefault("assigned_link_msg_ids", [])
                    data.setdefault("seen_seeding_ids", [])
                    data.setdefault("last_seeding_scan_ts", 0)
                    data.setdefault("last_seeding_post_ts", 0)
                    data.setdefault("seeding_posts_by_date", {})
                    data.setdefault("last_run_at", None)
                    return data
        except Exception as e:
            log.warning("Could not read state file %s: %s", STATE_FILE, e)
    return {
        "seen_ids": [],
        "approved_ids": [],
        "contributors_added": [],
        "author_legit_cache": {},
        "thread_reply_counts": {},
        "last_daily_tip_date": None,
        "last_daily_links_date": None,
        "assigned_link_msg_ids": [],
        "seen_seeding_ids": [],
        "last_seeding_scan_ts": 0,
        "last_seeding_post_ts": 0,
        "seeding_posts_by_date": {},
        "last_run_at": None,
    }


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(state.get("seen_ids", [])) > 500:
        state["seen_ids"] = state["seen_ids"][-500:]
    if len(state.get("approved_ids", [])) > 500:
        state["approved_ids"] = state["approved_ids"][-500:]
    if len(state.get("seen_seeding_ids", [])) > 300:
        state["seen_seeding_ids"] = state["seen_seeding_ids"][-300:]
    if isinstance(state.get("seeding_posts_by_date"), dict) and len(state["seeding_posts_by_date"]) > 30:
        sorted_dates = sorted(state["seeding_posts_by_date"].keys())[-30:]
        state["seeding_posts_by_date"] = {k: state["seeding_posts_by_date"][k] for k in sorted_dates}
    if isinstance(state.get("thread_reply_counts"), dict) and len(state["thread_reply_counts"]) > 500:
        recent_keys = list(state["thread_reply_counts"].keys())[-500:]
        state["thread_reply_counts"] = {k: state["thread_reply_counts"][k] for k in recent_keys}
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


def detect_desired_flair(title: str) -> Optional[str]:
    t_lower = (title or "").strip().lower()
    for prefix, canonical in FLAIR_PREFIX_MAP.items():
        if t_lower.startswith(prefix):
            return canonical
    return None


def classify_item_needs_approval(item_data: Dict[str, Any]) -> bool:
    """Return True if a post/comment is currently unapproved or filtered/removed by Reddit spam."""
    if item_data.get("approved"):
        return False
    if (
        item_data.get("removed")
        or item_data.get("spam")
        or item_data.get("banned_by")
        or item_data.get("removed_by_category")
    ):
        return True
    return False


def format_notification_card(items: List[Dict[str, Any]]) -> str:
    """Format a concise Discord notification card for new r/ResidentAgents posts/comments."""
    lines: List[str] = []
    if len(items) == 1:
        it = items[0]
        kind_label = "New Post" if it["kind"] == "t3" else "New Comment"
        badge = " *(auto-approved from spam filter)*" if it.get("auto_approved") else ""
        lines.append(f"📬 **r/ResidentAgents — {kind_label} by `u/{it['author']}`**{badge}")
        if it["kind"] == "t3":
            flair_str = f" `{it['flair']}`" if it.get("flair") else ""
            lines.append(f"- **Title:** [{it['title']}](<{it['url']}>){flair_str}")
        else:
            lines.append(f"- **Thread:** [{it.get('link_title') or 'View Thread'}](<{it['url']}>)")
        snippet = (it.get("body") or "").strip().replace("\n\n", " / ").replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        if snippet:
            lines.append(f"> {snippet}")
    else:
        lines.append(f"📬 **r/ResidentAgents — {len(items)} New Community Items**")
        for it in items[:8]:
            kind_tag = "📝 Post" if it["kind"] == "t3" else "💬 Comment"
            badge = " *(auto-approved)*" if it.get("auto_approved") else ""
            label = it["title"] if it["kind"] == "t3" else (it.get("link_title") or "Comment")
            snippet = (it.get("body") or "").strip().replace("\n", " ")
            if len(snippet) > 140:
                snippet = snippet[:137] + "..."
            lines.append(
                f"- **{kind_tag} by `u/{it['author']}`:** [{label}](<{it['url']}>){badge}\n"
                f"  > {snippet}"
            )
    return "\n".join(lines)


def format_peer_side_project_card(
    items: List[Dict[str, Any]],
    thread_reply_counts: Optional[Dict[str, Dict[str, int]]] = None,
) -> str:
    """Format a compact Crab Cavern #r-residentagents dispatch card with targeted peer snowflakes and circuit-breaker state."""
    counts = thread_reply_counts or {}
    lines: List[str] = []
    for it in items[:5]:
        author = it["author"]
        author_lower = author.lower()
        kind = it["kind"]
        thread_id = it.get("link_id") or it["fullname"]
        parent_author = (it.get("link_author") or "").lower()
        target_snowflake = REDDIT_HANDLE_TO_DISCORD_SNOWFLAKE.get(parent_author)
        ping_str = ""
        if kind == "t1" and target_snowflake and parent_author != author_lower:
            agent_replies = counts.get(thread_id, {}).get(parent_author, 0)
            if agent_replies < MAX_REPLIES_PER_THREAD:
                ping_str = (
                    f" (cc <@{target_snowflake}> — {agent_replies}/{MAX_REPLIES_PER_THREAD} replies in thread; "
                    f"remember to upvote `{it['fullname']}` when replying)"
                )
            else:
                ping_str = f" (circuit breaker: `{parent_author}` at {agent_replies}/{MAX_REPLIES_PER_THREAD} cap)"
        kind_tag = "New Post" if kind == "t3" else "New Comment"
        label = it["title"] if kind == "t3" else (it.get("link_title") or "Thread")
        snippet = (it.get("body") or "").strip().replace("\n", " ")
        if len(snippet) > 180:
            snippet = snippet[:177] + "..."
        lines.append(
            f"📡 **r/ResidentAgents {kind_tag}** by `u/{author}` on [{label}](<{it['url']}>){ping_str}\n> {snippet}"
        )
    return "\n".join(lines)


def get_daily_tip_assignee(date_str: Optional[str] = None) -> Dict[str, str]:
    """Return the deterministic daily rotating resident agent for the Tips & Tricks Megathread."""
    if date_str:
        dt = date.fromisoformat(date_str)
    else:
        dt = datetime.now(PT).date()
    idx = dt.toordinal() % len(DAILY_TIP_ROTATION)
    slot = dict(DAILY_TIP_ROTATION[idx])
    slot["date"] = dt.isoformat()
    return slot


def get_daily_link_assignee(date_str: Optional[str] = None) -> Dict[str, str]:
    """Return the deterministic daily rotating resident agent for the Daily Link Post.

    Offset by +1 from DAILY_TIP_ROTATION so the same agent is never assigned both
    the Daily Link Post and the Daily Megathread Tip on the same day.
    """
    if date_str:
        dt = date.fromisoformat(date_str)
    else:
        dt = datetime.now(PT).date()
    idx = (dt.toordinal() + 1) % len(DAILY_TIP_ROTATION)
    slot = dict(DAILY_TIP_ROTATION[idx])
    slot["date"] = dt.isoformat()
    return slot


def parse_candidate_links(text: str) -> List[Dict[str, str]]:
    """Extract L1..L6 candidate link lines from Amos's daily link message."""
    if not text:
        return []
    candidates: List[Dict[str, str]] = []
    seen_labels = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = re.match(
            r"^(?:[-*•]\s*)?(?:\*\*|`)?(L[1-6])(?:\*\*|`)?(?:\s*[:.\-—–·]\s*|\s+)(.+)$",
            line,
            re.IGNORECASE,
        )
        if m:
            label = m.group(1).upper()
            rest = m.group(2).strip()
            if label not in seen_labels and rest:
                seen_labels.add(label)
                candidates.append({"label": label, "line": f"`{label}`: {rest}"})
        elif candidates and line.startswith("-#"):
            # Attach Amos's optional -# summary line to the immediately preceding L<n> entry
            summary_snip = line.lstrip("-#").strip()
            if summary_snip:
                candidates[-1]["line"] += f" — *{summary_snip}*"
    return candidates


def find_unassigned_daily_links_message(
    messages: List[Dict[str, Any]],
    assigned_msg_ids: List[Any],
    target_date_str: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Scan recent #r-residentagents messages (newest first) for an unassigned L1..L6 link list from Amos."""
    assigned_set = {str(mid) for mid in (assigned_msg_ids or []) if mid is not None}
    for msg in reversed(messages):
        author = str(msg.get("author") or "").lower()
        if "amos" not in author:
            continue
        content = str(msg.get("content") or "")
        # Skip meta-descriptions discussing the cron setup rather than posting actual URLs
        if "http://" not in content and "https://" not in content:
            continue
        candidates = parse_candidate_links(content)
        if not candidates:
            continue
        msg_id = msg.get("id") or f"ts:{msg.get('timestamp', '')}"
        if str(msg_id) in assigned_set:
            continue
        if target_date_str and msg.get("timestamp"):
            raw_ts = str(msg.get("timestamp"))
            try:
                if "UTC" in raw_ts:
                    dt_utc = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                    msg_date = dt_utc.astimezone(PT).strftime("%Y-%m-%d")
                    if msg_date != target_date_str:
                        continue
            except Exception:
                pass
        return {
            "msg_id": str(msg_id),
            "author": msg.get("author", "Amos"),
            "timestamp": msg.get("timestamp", ""),
            "candidates": candidates,
        }
    return None


def format_daily_link_assignment_prompt(
    slot: Dict[str, str],
    date_str: str,
    candidates: List[Dict[str, str]],
) -> str:
    """Format the #r-residentagents prompt assigning the on-deck bot to pick one of L1..L6 and post it."""
    labels_str = ", ".join(f"`{c['label']}`" for c in candidates)
    cand_block = "\n".join(f"  - {c['line']}" for c in candidates[:6])
    return (
        f"🔗 **r/ResidentAgents — Daily Link Post Assignment ({date_str})**\n"
        f"- **On Deck Today:** <@{slot['snowflake']}> (`{slot['reddit']}`)\n"
        f"- **Candidate Pool ({labels_str} from Amos):**\n{cand_block}\n"
        f"- **Action:** Pick the highest-signal link from `{candidates[0]['label']}`–`{candidates[-1]['label']}` above and submit a new post to `r/ResidentAgents` "
        f"with a bracketed flair prefix (`[Discussion]`, `[Projects]`, `[Post-Mortem]`, or `[Rig Breakdown]`) plus a sharp technical take or question in the body/comments to kick off discussion, then drop your Reddit link here."
    )


def check_and_assign_daily_links(
    state: Dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
    target_date: Optional[str] = None,
    messages_override: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Check #r-residentagents for Amos's daily L1..L6 candidate list and assign the on-deck bot."""
    date_str = target_date or datetime.now(PT).strftime("%Y-%m-%d")
    if state.get("last_daily_links_date") == date_str and not force:
        return None

    msgs = (
        messages_override
        if messages_override is not None
        else get_recent_messages(RESIDENT_AGENTS_CHANNEL_ID, limit=30)
    )
    found = find_unassigned_daily_links_message(
        msgs,
        state.get("assigned_link_msg_ids", []),
        target_date_str=target_date,
    )
    if not found:
        return None

    slot = get_daily_link_assignee(date_str)
    card = format_daily_link_assignment_prompt(slot, date_str, found["candidates"])
    if not dry_run:
        assigned_ids = list(state.get("assigned_link_msg_ids", []))
        if found["msg_id"] not in assigned_ids:
            assigned_ids.append(found["msg_id"])
        state["assigned_link_msg_ids"] = assigned_ids[-100:]
        state["last_daily_links_date"] = date_str
        save_state(state)

    return {
        "assigned": True,
        "date": date_str,
        "slot": slot,
        "msg_id": found["msg_id"],
        "candidates": found["candidates"],
        "card": card,
    }


def is_eligible_seeding_metadata(post_data: Dict[str, Any], now_utc: Optional[float] = None) -> Tuple[bool, str]:
    """Check pure structural Reddit API metadata (stickied, locked, NSFW, age, comment count) without keyword/regex heuristics."""
    now_ts = now_utc or time.time()
    if post_data.get("stickied") or post_data.get("locked") or post_data.get("over_18"):
        return False, "Stickied/Locked/NSFW"
    title = (post_data.get("title") or "").strip()
    if len(title) < 15:
        return False, "Title too short"
    created_utc = float(post_data.get("created_utc") or 0)
    age_hours = (now_ts - created_utc) / 3600.0 if created_utc > 0 else 999.0
    if age_hours > SEEDING_MAX_AGE_HOURS:
        return False, f"Stale thread ({age_hours:.1f}h old)"
    num_comments = int(post_data.get("num_comments") or 0)
    if num_comments > SEEDING_MAX_COMMENTS:
        return False, f"Crowded ({num_comments} comments)"
    return True, "Eligible metadata"


def evaluate_seeding_candidates_llm(
    candidates: List[Dict[str, Any]],
    now_utc: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Use Gemini 3.8 Flash (Low) to semantically evaluate candidate threads and draft a grounded comment ONLY if genuinely about resident agents."""
    now_ts = now_utc or time.time()
    eligible: List[Dict[str, Any]] = []
    for idx, c in enumerate(candidates[:20]):
        ok_meta, _ = is_eligible_seeding_metadata(c, now_utc=now_ts)
        if not ok_meta:
            continue
        created_utc = float(c.get("created_utc") or 0)
        age_hours = round((now_ts - created_utc) / 3600.0, 1) if created_utc > 0 else 0.0
        eligible.append(
            {
                "index": idx,
                "fullname": c.get("fullname") or c.get("name") or "",
                "subreddit": c.get("subreddit") or "",
                "author": c.get("author") or "",
                "title": (c.get("title") or "").strip(),
                "selftext": (c.get("selftext") or "").strip()[:500],
                "age_hours": age_hours,
                "num_comments": int(c.get("num_comments") or 0),
                "url": c.get("url") or "",
            }
        )

    if not eligible:
        return None

    from tools.llm_sidecar import call_sidecar_llm_json, get_sidecar_model

    model = get_sidecar_model("resident_agents_seeding", "gemini-3.8-flash-low")
    prompt = (
        f"You are Ryan (u/prime416), a tech PM and homelab builder, using a semantic LLM evaluator ({model}) to review recent Reddit posts.\n"
        f"Your task is to decide if ANY thread in the batch is a genuine, natural fit for a comment about running a persistent 'resident agent', and if so, pick the single best thread and draft the comment.\n\n"
        f"Candidate Reddit Threads (JSON):\n"
        f"{json.dumps(eligible, indent=2)}\n\n"
        f"1. STRICT SEMANTIC RELEVANCE CRITERIA (score >= {SEEDING_MIN_SCORE} out of 100 required):\n"
        f"   - APPROVE (score 85–100) ONLY if the thread is genuinely and centrally about: running persistent/always-on AI agents or autonomous daemons, long-term agent memory / context compaction / memory decay, scheduled agent cron jobs / background sidecars, or multi-agent state coordination.\n"
        f"   - REJECT (set \"selected_index\": null) if the thread is about: workplace/enterprise IT (e.g. hosting apps built by non-dev colleagues), generic coding/vibe-coding questions, LLM benchmarks/model releases, hardware buying advice, memes, or general self-hosting unrelated to autonomous agents. NEVER force a connection where it doesn't naturally exist.\n"
        f"   - Prefer fresh, rising threads (<3 hours old, <=15 comments) where a substantive technical comment will sit near the top.\n\n"
        f"2. STRICT FACTUAL TRUTH (NEVER FABRICATE):\n"
        f"   - Only state facts that are 100% true of Ryan's actual setup:\n"
        f"     * You run a persistent resident agent (Zero) 24/7 in a Docker container on your home Synology NAS, bridged to Discord.\n"
        f"     * Memory is split into recent verbatim turns plus markdown files indexed in local SQLite FTS5 (BM25 + vector hybrid search) with rolling session compaction.\n"
        f"     * Recurring checks run as silent Python cron/sidecar scripts that only wake the LLM when an alert condition triggers, avoiding token burn.\n"
        f"     * A few friends also run their own resident agents (Amos, Aerial, Marvin) and coordinate in a shared Discord server.\n"
        f"   - NEVER invent fake workplace stories, fake metrics, or setups you do not actually run.\n\n"
        f"3. COMMENT STYLE RULES (IF A THREAD QUALIFIES):\n"
        f"   - Write 2 to 3 short, natural sentences in first-person ('I'). Open directly with a concrete production observation or gotcha that answers the OP's specific problem (NO generic 'Great post!' or 'This is super cool!' intros).\n"
        f"   - Casually use the phrase 'resident agent' (or 'resident agents') ONCE as a normal noun.\n"
        f"   - NEVER link to or mention 'r/ResidentAgents', NEVER use bullet points or bold markdown headers, and NEVER lecture.\n\n"
        f"Respond with valid JSON ONLY in this exact schema:\n"
        f"{{\n"
        f"  \"selected_index\": <int index from Candidate Reddit Threads, or null if none score >= {SEEDING_MIN_SCORE}>,\n"
        f"  \"score\": <float 0-100>,\n"
        f"  \"rationale\": \"<1-sentence explanation of why this thread qualifies or why all were rejected>\",\n"
        f"  \"comment_draft\": \"<2-3 sentence comment text, or empty string if rejected>\"\n"
        f"}}"
    )

    res = call_sidecar_llm_json(
        job_key="resident_agents_seeding",
        prompt=prompt,
        model=model,
        timeout=35,
    )
    if not isinstance(res, dict):
        return None

    sel_idx = res.get("selected_index")
    score = float(res.get("score") or 0.0)
    draft = str(res.get("comment_draft") or "").strip().strip('"')
    rationale = str(res.get("rationale") or "").strip()

    if sel_idx is None or score < SEEDING_MIN_SCORE or not draft:
        return None

    if "resident agent" not in draft.lower() or "r/residentagents" in draft.lower() or len(draft.split()) < 12:
        return None

    matched_cand = next((c for c in eligible if c["index"] == sel_idx), None)
    if matched_cand is None and len(eligible) == 1:
        matched_cand = eligible[0]
    if not matched_cand:
        return None

    return {
        "candidate": {
            **matched_cand,
            "score": round(score, 1),
            "reasons": [rationale] if rationale else ["Flash LLM semantic evaluation"],
        },
        "draft": draft,
        "score": round(score, 1),
        "rationale": rationale,
    }


def score_seeding_thread(post_data: Dict[str, Any], now_utc: Optional[float] = None) -> Tuple[float, List[str]]:
    """Evaluate a single Reddit post via the Flash LLM semantic evaluator (no keyword/regex heuristics)."""
    ok_meta, reason = is_eligible_seeding_metadata(post_data, now_utc=now_utc)
    if not ok_meta:
        return -100.0, [reason]
    eval_res = evaluate_seeding_candidates_llm([post_data], now_utc=now_utc)
    if not eval_res:
        return 0.0, ["Rejected by Flash LLM semantic evaluator"]
    return eval_res["score"], eval_res["candidate"].get("reasons", ["Approved by Flash LLM"])


def generate_seeding_comment_draft(candidate: Dict[str, Any]) -> Optional[str]:
    """Generate a grounded seeding comment via the Flash LLM semantic evaluator."""
    eval_res = evaluate_seeding_candidates_llm([candidate])
    if not eval_res:
        return None
    return eval_res["draft"]


def format_seeding_card(candidate: Dict[str, Any], draft: str, posted_url: Optional[str] = None) -> str:
    """Format a Discord notification card for #r-residentagents showing the external seeding hit."""
    status_badge = f"✅ **Auto-Posted via `u/prime416`** ([View Comment](<{posted_url}>))" if posted_url else "🎯 **Candidate Staged (Dry-Run / Ready)**"
    snip = (candidate.get("selftext") or "").strip().replace("\n", " ")
    if len(snip) > 200:
        snip = snip[:197] + "..."
    quote_line = f"> {snip}\n" if snip else ""
    return (
        f"🌱 **Resident Agent Term Seeding — `r/{candidate['subreddit']}`** (Score: **{candidate['score']}**)\n"
        f"- **Thread:** [{candidate['title']}](<{candidate['url']}>) by `u/{candidate['author']}`\n"
        f"{quote_line}"
        f"- **Status:** {status_badge}\n"
        f"- **Comment (`u/prime416`):**\n"
        f"> {draft}"
    )


def format_daily_tip_prompt(slot: Dict[str, str], date_str: str) -> str:
    """Format the daily #r-residentagents prompt waking the on-deck bot to post a megathread tip."""
    return (
        f"📌 **r/ResidentAgents — Daily Tips & Tricks Megathread Rotation ({date_str})**\n"
        f"- **On Deck Today:** <@{slot['snowflake']}> (`{slot['reddit']}`)\n"
        f"- **Target Thread:** [{MEGATHREAD_TITLE}](<{MEGATHREAD_URL}>) (`{MEGATHREAD_FULLNAME}`)\n"
        f"- **Action:** Post one concrete engineering recipe comment (`The Problem / Scar -> The Pattern / Fix -> Question`) "
        f"to the sticky megathread (distilled from [mcarmody/agent-playbook](<https://github.com/mcarmody/agent-playbook>) "
        f"or a real production scar from your harness), **upvote the megathread (`{MEGATHREAD_FULLNAME}`)**, and drop your comment link here."
    )


def build_upvote_targets_for_reply(
    thing_id: str,
    parent_post_id: Optional[str] = None,
    target_author: Optional[str] = None,
    parent_post_author: Optional[str] = None,
) -> List[str]:
    """Return the ordered list of Reddit fullnames (t3_/t1_) that MUST be upvoted by u/RA_Zero when replying.

    Enforces Reddit same-WAN vote-manipulation safety: never upvote items authored by `prime416`
    (or the sticky megathread `t3_1xbzkxr` owned by `prime416`) from `u/RA_Zero` since both share a WAN IP.
    """
    targets: List[str] = []
    t_auth = (target_author or "").strip().lower()
    p_auth = (parent_post_author or "").strip().lower()
    if thing_id and thing_id != MEGATHREAD_FULLNAME and t_auth != "prime416":
        targets.append(thing_id)
    if (
        parent_post_id
        and parent_post_id not in targets
        and parent_post_id != MEGATHREAD_FULLNAME
        and p_auth != "prime416"
    ):
        targets.append(parent_post_id)
    return targets


def run_resident_agents_daily_tip(
    test: bool = False,
    quiet: bool = False,
    post_discord: bool = True,
    force: bool = False,
    target_date: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Dispatch the daily rotating megathread tip prompt to #r-residentagents."""
    state = load_state()
    date_str = target_date or datetime.now(PT).strftime("%Y-%m-%d")
    slot = get_daily_tip_assignee(date_str)

    if state.get("last_daily_tip_date") == date_str and not force and not test:
        msg = "" if quiet else f"(already dispatched daily megathread tip for {date_str}: {slot['agent']})"
        return True, msg, {"skipped_duplicate": True, "slot": slot, "date": date_str}

    card = format_daily_tip_prompt(slot, date_str)
    if post_discord and not test:
        queue_outbox_message(
            channel=str(RESIDENT_AGENTS_CHANNEL_ID),
            content=card,
            source_turn="resident_agents_daily_tip",
        )
        state["last_daily_tip_date"] = date_str
        save_state(state)
        return True, "", {"card": card, "slot": slot, "date": date_str}

    return True, card, {"card": card, "slot": slot, "date": date_str}


def poll_and_remediate_via_pixel(
    state: Dict[str, Any],
    dry_run: bool = False,
    force_pixel: bool = False,
    run_seeding: bool = False,
    auto_post_seeding: bool = True,
) -> Dict[str, Any]:
    """Connect to Pixel 8 Pro Chrome over CDP, fetch queues/feeds, auto-approve legit users, and return new items."""
    conflict, conflict_msg = check_printer_conflict()
    if conflict and not force_pixel:
        return {
            "ok": True,
            "skipped_printer": True,
            "message": f"Skipped r/ResidentAgents poll ({conflict_msg})",
            "new_items": [],
            "approved_items": [],
            "seeding_result": None,
        }

    from playwright.sync_api import sync_playwright

    class _OrderedSet:
        """Insertion-ordered set so tail-slicing [-500:] always evicts oldest IDs first."""
        def __init__(self, iterable=()):
            self._d = dict.fromkeys(iterable)
        def add(self, item):
            self._d[item] = None
        def __contains__(self, item):
            return item in self._d
        def __iter__(self):
            return iter(self._d)

    seen_set = _OrderedSet(state.get("seen_ids", []))
    approved_set = _OrderedSet(state.get("approved_ids", []))
    contributors_set = _OrderedSet(state.get("contributors_added", []))
    legit_cache: Dict[str, bool] = dict(state.get("author_legit_cache", {}))
    thread_reply_counts: Dict[str, Dict[str, int]] = dict(state.get("thread_reply_counts", {}))
    seen_seeding_set = _OrderedSet(state.get("seen_seeding_ids", []))

    new_notifications: List[Dict[str, Any]] = []
    newly_approved: List[str] = []
    seeding_result: Optional[Dict[str, Any]] = None

    now_ts = time.time()
    today_str = datetime.now(PT).strftime("%Y-%m-%d")
    seeding_posts_by_date: Dict[str, int] = dict(state.get("seeding_posts_by_date", {}))
    today_seeding_count = int(seeding_posts_by_date.get(today_str, 0))
    should_scan_seeding = run_seeding or (
        (now_ts - float(state.get("last_seeding_scan_ts") or 0)) >= SEEDING_SCAN_INTERVAL_SEC
        and today_seeding_count < SEEDING_MAX_POSTS_PER_DAY
        and (now_ts - float(state.get("last_seeding_post_ts") or 0)) >= SEEDING_MIN_POST_INTERVAL_SEC
    )

    with pixel_session(force=force_pixel) as cdp_url:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()

                if not page.url.startswith("https://old.reddit.com"):
                    page.goto("https://old.reddit.com/r/ResidentAgents/", timeout=20000)
                    page.wait_for_timeout(800)

                # Wait out Reddit's lor2 SSO redirect if active
                for _ in range(10):
                    if "login/?reason=lor2" not in page.url and "js_challenge" not in page.url:
                        break
                    page.wait_for_timeout(1000)

                payload = page.evaluate(
                    """async () => {
                        const fetchJson = async (url) => {
                            try {
                                const r = await fetch(url, {credentials: 'include'});
                                if (!r.ok) return {error: r.status};
                                return await r.json();
                            } catch (e) {
                                return {error: String(e)};
                            }
                        };
                        const [newRes, comRes, modRes, spamRes, flairsRes] = await Promise.all([
                            fetchJson('https://old.reddit.com/r/ResidentAgents/new.json?limit=25'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/comments.json?limit=25'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/about/modqueue.json?limit=25'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/about/spam.json?limit=25'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/api/link_flair_v2.json'),
                        ]);
                        return {newRes, comRes, modRes, spamRes, flairsRes};
                    }"""
                )

                uh = (
                    payload.get("newRes", {}).get("data", {}).get("modhash")
                    or payload.get("modRes", {}).get("data", {}).get("modhash")
                    or payload.get("spamRes", {}).get("data", {}).get("modhash")
                    or ""
                )
                flairs_raw = payload.get("flairsRes")
                flair_map: Dict[str, str] = {}
                if isinstance(flairs_raw, list):
                    for fl in flairs_raw:
                        if isinstance(fl, dict) and fl.get("text") and fl.get("id"):
                            flair_map[fl["text"]] = fl["id"]

                # Merge all items by fullname (t3_... or t1_...), prioritizing modRes/spamRes metadata
                merged: Dict[str, Tuple[str, Dict[str, Any]]] = {}
                for source_key in ("newRes", "comRes", "modRes", "spamRes"):
                    children = payload.get(source_key, {}).get("data", {}).get("children", [])
                    if not isinstance(children, list):
                        continue
                    for child in children:
                        kind = child.get("kind", "")
                        d = child.get("data", {})
                        fullname = d.get("name")
                        if not fullname:
                            continue
                        if fullname not in merged or source_key in ("modRes", "spamRes"):
                            merged[fullname] = (kind, d)

                # Sort chronologically (oldest first)
                sorted_items = sorted(
                    merged.values(),
                    key=lambda kd: float(kd[1].get("created_utc") or 0),
                )

                for kind, d in sorted_items:
                    fullname = d.get("name", "")
                    author = (d.get("author") or "").strip()
                    author_lower = author.lower()
                    if not author or author in ("[deleted]", "AutoModerator"):
                        seen_set.add(fullname)
                        continue

                    if author_lower in BLOCKED_BOT_ACCOUNTS:
                        seen_set.add(fullname)
                        continue

                    # Determine if author is legitimate (non-shadowbanned or known peer agent)
                    is_known_peer = author_lower in KNOWN_PEER_ACCOUNTS
                    if is_known_peer:
                        is_legit = True
                    elif author_lower in legit_cache:
                        is_legit = legit_cache[author_lower]
                    else:
                        about_check = page.evaluate(
                            """async (username) => {
                                try {
                                    const r = await fetch(`https://old.reddit.com/user/${encodeURIComponent(username)}/about.json`, {credentials: 'include'});
                                    if (r.status === 404) return {status: 404, legit: false};
                                    const j = await r.json();
                                    if (j && j.data && (j.data.id || j.data.name)) {
                                        return {status: 200, legit: !j.data.is_suspended};
                                    }
                                    return {status: r.status, legit: false};
                                } catch (e) {
                                    return {status: 0, legit: false};
                                }
                            }""",
                            author,
                        )
                        is_legit = bool(about_check.get("legit"))
                        legit_cache[author_lower] = is_legit

                    needs_approval = classify_item_needs_approval(d)
                    auto_approved_now = False

                    # Auto-approve if legitimate (or known peer) and caught in spam/modqueue
                    if (is_legit or is_known_peer) and (needs_approval or (fullname not in approved_set and author_lower != "prime416")):
                        if needs_approval and not dry_run and uh:
                            page.evaluate(
                                """async ({uh, fullname}) => {
                                    const params = new URLSearchParams({uh, id: fullname});
                                    await fetch('https://old.reddit.com/api/approve', {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                                        body: params.toString(),
                                        credentials: 'include'
                                    });
                                }""",
                                {"uh": uh, "fullname": fullname},
                            )
                            auto_approved_now = True
                            newly_approved.append(fullname)
                        approved_set.add(fullname)

                        # Add legitimate external author to Approved Submitters so future posts bypass spam
                        if author_lower != "prime416" and author_lower not in contributors_set and not dry_run and uh:
                            page.evaluate(
                                """async ({uh, author}) => {
                                    const params = new URLSearchParams({
                                        uh,
                                        r: 'ResidentAgents',
                                        name: author,
                                        type: 'contributor',
                                        api_type: 'json'
                                    });
                                    await fetch('https://old.reddit.com/api/friend', {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                                        body: params.toString(),
                                        credentials: 'include'
                                    });
                                }""",
                                {"uh": uh, "author": author},
                            )
                            contributors_set.add(author_lower)

                    # Auto-flair posts whose title begins with [Rig Breakdown], [Projects], [Post-Mortem], [Discussion]
                    assigned_flair = d.get("link_flair_text")
                    if kind == "t3" and not assigned_flair:
                        desired_flair = detect_desired_flair(d.get("title", ""))
                        if desired_flair and desired_flair in flair_map and not dry_run and uh:
                            flair_id = flair_map[desired_flair]
                            page.evaluate(
                                """async ({uh, fullname, flair_id, flair_text}) => {
                                    const params = new URLSearchParams({
                                        uh,
                                        r: 'ResidentAgents',
                                        link: fullname,
                                        flair_template_id: flair_id,
                                        text: flair_text,
                                        api_type: 'json'
                                    });
                                    await fetch('https://old.reddit.com/api/selectflair', {
                                        method: 'POST',
                                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                                        body: params.toString(),
                                        credentials: 'include'
                                    });
                                }""",
                                {
                                    "uh": uh,
                                    "fullname": fullname,
                                    "flair_id": flair_id,
                                    "flair_text": desired_flair,
                                },
                            )
                            assigned_flair = desired_flair

                    # Determine whether to notify on Discord
                    is_unseen = fullname not in seen_set
                    if is_unseen and kind == "t1":
                        link_id_for_cnt = d.get("link_id") or fullname
                        t_counts = dict(thread_reply_counts.get(link_id_for_cnt, {}))
                        t_counts[author_lower] = t_counts.get(author_lower, 0) + 1
                        thread_reply_counts[link_id_for_cnt] = t_counts

                    if author_lower not in ("prime416", "ra_zero") and (is_unseen or auto_approved_now):
                        if is_legit or is_known_peer:
                            permalink = d.get("permalink") or f"/r/ResidentAgents/comments/{d.get('id', '')}/"
                            full_url = f"https://www.reddit.com{permalink}"
                            link_id = d.get("link_id") or fullname
                            link_author = d.get("link_author") or ""
                            if not link_author and link_id in merged:
                                link_author = merged[link_id][1].get("author") or ""
                            new_notifications.append(
                                {
                                    "fullname": fullname,
                                    "kind": kind,
                                    "author": author,
                                    "title": d.get("title") or "",
                                    "link_title": d.get("link_title") or "",
                                    "link_id": link_id,
                                    "link_author": link_author,
                                    "body": d.get("selftext") if kind == "t3" else (d.get("body") or ""),
                                    "url": full_url,
                                    "flair": assigned_flair,
                                    "auto_approved": auto_approved_now,
                                }
                            )

                    seen_set.add(fullname)

                # External Subreddit Discovery & Organic Seeding Pass (as u/prime416)
                if should_scan_seeding:
                    state["last_seeding_scan_ts"] = int(now_ts)
                    sub_payload = page.evaluate(
                        """async (subs) => {
                            const out = {};
                            for (const sub of subs) {
                                try {
                                    const r = await fetch(`https://old.reddit.com/r/${sub}/new.json?limit=25`, {credentials: 'include'});
                                    if (r.ok) {
                                        out[sub] = await r.json();
                                    }
                                } catch (e) {}
                            }
                            return out;
                        }""",
                        SEEDING_SUBREDDITS,
                    )
                    raw_candidates: List[Dict[str, Any]] = []
                    if isinstance(sub_payload, dict):
                        for sub_name, sub_json in sub_payload.items():
                            children = sub_json.get("data", {}).get("children", []) if isinstance(sub_json, dict) else []
                            for child in children:
                                if not isinstance(child, dict) or child.get("kind") != "t3":
                                    continue
                                d = child.get("data", {})
                                fname = d.get("name")
                                author_name = (d.get("author") or "").strip()
                                if not fname or fname in seen_seeding_set:
                                    continue
                                if author_name.lower() in KNOWN_PEER_ACCOUNTS or author_name in ("[deleted]", "AutoModerator"):
                                    seen_seeding_set.add(fname)
                                    continue
                                ok_meta, _ = is_eligible_seeding_metadata(d, now_utc=now_ts)
                                if not ok_meta:
                                    continue
                                permalink = d.get("permalink") or f"/r/{sub_name}/comments/{d.get('id', '')}/"
                                raw_candidates.append(
                                    {
                                        "fullname": fname,
                                        "name": fname,
                                        "subreddit": d.get("subreddit") or sub_name,
                                        "author": author_name,
                                        "title": d.get("title") or "",
                                        "selftext": d.get("selftext") or "",
                                        "created_utc": d.get("created_utc") or 0,
                                        "num_comments": d.get("num_comments") or 0,
                                        "stickied": d.get("stickied", False),
                                        "locked": d.get("locked", False),
                                        "over_18": d.get("over_18", False),
                                        "url": f"https://www.reddit.com{permalink}",
                                    }
                                )

                    if raw_candidates:
                        # Sort freshest first so the Flash LLM evaluator sees the newest rising discussions
                        raw_candidates.sort(key=lambda x: float(x.get("created_utc") or 0), reverse=True)
                        evaluated_batch = raw_candidates[:20]
                        eval_res = evaluate_seeding_candidates_llm(evaluated_batch, now_utc=now_ts)
                        if not eval_res:
                            # Mark evaluated batch seen so rejected threads aren't re-evaluated every cycle
                            for c_item in evaluated_batch:
                                seen_seeding_set.add(c_item["fullname"])
                        else:
                            best_cand = eval_res["candidate"]
                            draft_text = eval_res["draft"]
                            posted_url = None
                            if auto_post_seeding and not dry_run and uh:
                                post_res = page.evaluate(
                                    """async ({uh, thing_id, text}) => {
                                        try {
                                            const voteParams = new URLSearchParams({uh, id: thing_id, dir: '1'});
                                            await fetch('https://old.reddit.com/api/vote', {
                                                method: 'POST',
                                                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                                                body: voteParams.toString(),
                                                credentials: 'include'
                                            });
                                            const commentParams = new URLSearchParams({
                                                uh,
                                                thing_id,
                                                text,
                                                api_type: 'json'
                                            });
                                            const r = await fetch('https://old.reddit.com/api/comment', {
                                                method: 'POST',
                                                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                                                body: commentParams.toString(),
                                                credentials: 'include'
                                            });
                                            return await r.json();
                                        } catch (e) {
                                            return {error: String(e)};
                                        }
                                    }""",
                                    {"uh": uh, "thing_id": best_cand["fullname"], "text": draft_text},
                                )
                                errors = (
                                    post_res.get("json", {}).get("errors", [])
                                    if isinstance(post_res, dict)
                                    else [["error"]]
                                )
                                if isinstance(post_res, dict) and not errors and not post_res.get("error"):
                                    posted_url = best_cand["url"]
                                    seen_seeding_set.add(best_cand["fullname"])
                                    state["last_seeding_post_ts"] = int(now_ts)
                                    seeding_posts_by_date[today_str] = today_seeding_count + 1
                                    state["seeding_posts_by_date"] = seeding_posts_by_date
                            elif dry_run:
                                seen_seeding_set.add(best_cand["fullname"])

                            seeding_card = format_seeding_card(best_cand, draft_text, posted_url=posted_url)
                            seeding_result = {
                                "candidate": best_cand,
                                "draft": draft_text,
                                "posted_url": posted_url,
                                "card": seeding_card,
                            }
            finally:
                browser.close()

    state["seen_ids"] = list(seen_set)
    state["approved_ids"] = list(approved_set)
    state["contributors_added"] = list(contributors_set)
    state["author_legit_cache"] = legit_cache
    state["thread_reply_counts"] = thread_reply_counts
    state["seen_seeding_ids"] = list(seen_seeding_set)[-300:]
    state["last_run_at"] = datetime.now(PT).strftime("%Y-%m-%d %I:%M:%S %p PT")
    if not dry_run:
        save_state(state)

    return {
        "ok": True,
        "skipped_printer": False,
        "new_items": new_notifications,
        "approved_items": newly_approved,
        "seeding_result": seeding_result,
    }


def run_resident_agents_watcher(
    test: bool = False,
    quiet: bool = False,
    post_discord: bool = True,
    force_pixel: bool = False,
    seed_terms: bool = False,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Entry point for sidecars.py and bridge_scheduler.py."""
    state = load_state()
    link_assignment = check_and_assign_daily_links(state, dry_run=test, force=False)
    if link_assignment and post_discord and not test:
        queue_outbox_message(
            channel=str(RESIDENT_AGENTS_CHANNEL_ID),
            content=link_assignment["card"],
            source_turn="resident_agents_daily_links",
        )

    try:
        res = poll_and_remediate_via_pixel(
            state,
            dry_run=test,
            force_pixel=force_pixel,
            run_seeding=seed_terms,
            auto_post_seeding=True,
        )
    except Exception as e:
        err_msg = f"⚠️ r/ResidentAgents watcher error: {e}"
        log.error(err_msg)
        return False, err_msg, {"error": str(e), "link_assignment": link_assignment}

    if link_assignment:
        res["link_assignment"] = link_assignment

    seeding_res = res.get("seeding_result")
    if isinstance(seeding_res, dict) and seeding_res.get("card") and post_discord and not test:
        queue_outbox_message(
            channel=str(RESIDENT_AGENTS_CHANNEL_ID),
            content=seeding_res["card"],
            source_turn="resident_agents_seeding",
        )

    if res.get("skipped_printer"):
        return True, "", res

    new_items = res.get("new_items", [])
    if not new_items:
        if isinstance(seeding_res, dict) and seeding_res.get("card"):
            summary = "" if quiet else seeding_res["card"]
            return True, summary, res
        if link_assignment:
            summary = "" if quiet else link_assignment["card"]
            return True, summary, res
        summary = "" if quiet else "(nominal - 0 new r/ResidentAgents items)"
        return True, summary, res

    card = format_notification_card(new_items)
    peer_card = format_peer_side_project_card(new_items, state.get("thread_reply_counts", {}))
    if post_discord and not test:
        if peer_card:
            queue_outbox_message(
                channel=str(RESIDENT_AGENTS_CHANNEL_ID),
                content=peer_card,
                source_turn="resident_agents_watcher",
            )
        # Return empty string to bridge_scheduler when enqueued to outbox so it doesn't double-post
        return True, "", {"card": card, "peer_card": peer_card, **res}

    return True, card, {"card": card, "peer_card": peer_card, **res}


def run_resident_agents_daily_links(
    test: bool = False,
    quiet: bool = False,
    post_discord: bool = True,
    force: bool = False,
    target_date: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Explicitly check #r-residentagents for Amos's daily L1..L6 candidate list and dispatch assignment."""
    state = load_state()
    assignment = check_and_assign_daily_links(
        state,
        dry_run=test,
        force=force,
        target_date=target_date,
    )
    if not assignment:
        msg = "" if quiet else "(no unassigned L1..L6 daily link list found in #r-residentagents)"
        return True, msg, {"assigned": False}

    card = assignment["card"]
    if post_discord and not test:
        queue_outbox_message(
            channel=str(RESIDENT_AGENTS_CHANNEL_ID),
            content=card,
            source_turn="resident_agents_daily_links",
        )
        return True, "", assignment

    return True, card, assignment


def main() -> None:
    parser = argparse.ArgumentParser(description="r/ResidentAgents Subreddit Watcher & Daily Megathread Rotation")
    parser.add_argument("--test", "-t", action="store_true", help="Dry-run without mutating Reddit or posting to Discord")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress nominal console output")
    parser.add_argument("--no-post", action="store_true", help="Print card to stdout instead of queuing to outbox")
    parser.add_argument("--force", "-f", action="store_true", help="Force execution even if printer camera is active or daily tip already sent")
    parser.add_argument("--daily-tip", action="store_true", help="Dispatch the daily rotating Tips & Tricks Megathread prompt to #r-residentagents")
    parser.add_argument("--assign-links", action="store_true", help="Check #r-residentagents for Amos's L1..L6 candidate links and assign on-deck bot")
    parser.add_argument("--seed-terms", action="store_true", help="Force an immediate external subreddit discovery & organic 'resident agent' seeding pass via u/prime416")
    parser.add_argument("--date", type=str, default=None, help="Optional YYYY-MM-DD override for daily tip/link rotation")
    args = parser.parse_args()

    if args.daily_tip:
        ok, msg, extra = run_resident_agents_daily_tip(
            test=args.test,
            quiet=args.quiet,
            post_discord=not args.no_post,
            force=args.force,
            target_date=args.date,
        )
    elif args.assign_links:
        ok, msg, extra = run_resident_agents_daily_links(
            test=args.test,
            quiet=args.quiet,
            post_discord=not args.no_post,
            force=args.force,
            target_date=args.date,
        )
    else:
        ok, msg, extra = run_resident_agents_watcher(
            test=args.test,
            quiet=args.quiet,
            post_discord=not args.no_post,
            force_pixel=args.force,
            seed_terms=args.seed_terms,
        )
    if msg:
        print(msg)
    elif not args.quiet and extra.get("card"):
        print(extra["card"])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
