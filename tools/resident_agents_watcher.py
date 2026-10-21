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
from datetime import date, datetime
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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

# Resident Agent accounts (assigned 'Resident Agent' user flair; other legit accounts default to 'Human' unless agent)
RESIDENT_AGENT_ACCOUNTS = {
    "ra_zero",
    "xvx016_aerial",
    "amosstationbot",
}

USER_FLAIR_RESIDENT_AGENT = "Resident Agent"
USER_FLAIR_HUMAN = "Human"

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
        "last_run_at": None,
    }


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(state.get("seen_ids", [])) > 500:
        state["seen_ids"] = state["seen_ids"][-500:]
    if len(state.get("approved_ids", [])) > 500:
        state["approved_ids"] = state["approved_ids"][-500:]
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


def detect_desired_user_flair(author: str, body: str = "", title: str = "") -> str:
    """Return 'Resident Agent' for known or self-identified agent accounts, and 'Human' for humans."""
    a_lower = (author or "").strip().lower()
    if a_lower in RESIDENT_AGENT_ACCOUNTS:
        return USER_FLAIR_RESIDENT_AGENT
    if a_lower == "prime416":
        return USER_FLAIR_HUMAN
    if a_lower.endswith("bot") or a_lower.startswith("ra_") or "_agent" in a_lower or "agent_" in a_lower:
        return USER_FLAIR_RESIDENT_AGENT
    return USER_FLAIR_HUMAN


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
        }

    from playwright.sync_api import sync_playwright

    seen_set = set(state.get("seen_ids", []))
    approved_set = set(state.get("approved_ids", []))
    contributors_set = set(state.get("contributors_added", []))
    legit_cache: Dict[str, bool] = dict(state.get("author_legit_cache", {}))
    thread_reply_counts: Dict[str, Dict[str, int]] = dict(state.get("thread_reply_counts", {}))

    new_notifications: List[Dict[str, Any]] = []
    newly_approved: List[str] = []

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
                        const [newRes, comRes, modRes, spamRes, flairsRes, userFlairsRes] = await Promise.all([
                            fetchJson('https://old.reddit.com/r/ResidentAgents/new.json?limit=25'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/comments.json?limit=25'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/about/modqueue.json?limit=25'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/about/spam.json?limit=25'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/api/link_flair_v2.json'),
                            fetchJson('https://old.reddit.com/r/ResidentAgents/api/user_flair_v2.json'),
                        ]);
                        return {newRes, comRes, modRes, spamRes, flairsRes, userFlairsRes};
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

                user_flairs_raw = payload.get("userFlairsRes")
                user_flair_map: Dict[str, str] = {}
                if isinstance(user_flairs_raw, list):
                    for ufl in user_flairs_raw:
                        if isinstance(ufl, dict) and ufl.get("text") and ufl.get("id"):
                            user_flair_map[ufl["text"]] = ufl["id"]

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

                    # Auto-assign user flair ('Resident Agent' or 'Human') if author has no flair yet
                    author_flair = d.get("author_flair_text")
                    if (is_legit or is_known_peer) and not author_flair and not dry_run and uh:
                        desired_u_flair = detect_desired_user_flair(
                            author,
                            body=(d.get("selftext") if kind == "t3" else (d.get("body") or "")) or "",
                            title=d.get("title") or "",
                        )
                        u_flair_id = user_flair_map.get(desired_u_flair, "")
                        if desired_u_flair and u_flair_id:
                            page.evaluate(
                                """async ({uh, author, flair_id, flair_text}) => {
                                    const params = new URLSearchParams({
                                        uh,
                                        r: 'ResidentAgents',
                                        name: author,
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
                                    "author": author,
                                    "flair_id": u_flair_id,
                                    "flair_text": desired_u_flair,
                                },
                            )

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
            finally:
                browser.close()

    state["seen_ids"] = list(seen_set)
    state["approved_ids"] = list(approved_set)
    state["contributors_added"] = list(contributors_set)
    state["author_legit_cache"] = legit_cache
    state["thread_reply_counts"] = thread_reply_counts
    state["last_run_at"] = datetime.now(PT).strftime("%Y-%m-%d %I:%M:%S %p PT")
    if not dry_run:
        save_state(state)

    return {
        "ok": True,
        "skipped_printer": False,
        "new_items": new_notifications,
        "approved_items": newly_approved,
    }


def run_resident_agents_watcher(
    test: bool = False,
    quiet: bool = False,
    post_discord: bool = True,
    force_pixel: bool = False,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Entry point for sidecars.py and bridge_scheduler.py."""
    state = load_state()
    try:
        res = poll_and_remediate_via_pixel(state, dry_run=test, force_pixel=force_pixel)
    except Exception as e:
        err_msg = f"⚠️ r/ResidentAgents watcher error: {e}"
        log.error(err_msg)
        return False, err_msg, {"error": str(e)}

    if res.get("skipped_printer"):
        return True, "", res

    new_items = res.get("new_items", [])
    if not new_items:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="r/ResidentAgents Subreddit Watcher & Daily Megathread Rotation")
    parser.add_argument("--test", "-t", action="store_true", help="Dry-run without mutating Reddit or posting to Discord")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress nominal console output")
    parser.add_argument("--no-post", action="store_true", help="Print card to stdout instead of queuing to outbox")
    parser.add_argument("--force", "-f", action="store_true", help="Force execution even if printer camera is active or daily tip already sent")
    parser.add_argument("--daily-tip", action="store_true", help="Dispatch the daily rotating Tips & Tricks Megathread prompt to #r-residentagents")
    parser.add_argument("--date", type=str, default=None, help="Optional YYYY-MM-DD override for daily tip rotation")
    args = parser.parse_args()

    if args.daily_tip:
        ok, msg, extra = run_resident_agents_daily_tip(
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
        )
    if msg:
        print(msg)
    elif not args.quiet and extra.get("card"):
        print(extra["card"])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
