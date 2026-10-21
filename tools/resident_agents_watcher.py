#!/usr/bin/env python3
"""ResidentAgents Subreddit Watcher & Auto-Approver Sidecar.

Polls r/ResidentAgents (new.json, comments.json, about/modqueue.json, about/spam.json)
via the authenticated Pixel 8 Pro browser session over CDP:
1. Auto-approves posts and comments from legitimate (non-shadowbanned) accounts
   and known Crab Cavern peer agent accounts, and adds legit new posters as
   Approved Submitters (Contributors) so Reddit's spam filter leaves them alone.
2. Automatically assigns matching post flair ([Rig Breakdown], [Projects],
   [Post-Mortem], [Discussion]) when a post title starts with a bracketed tag.
3. Dispatches clean Discord notification cards to #zero-chat via tools/outbox.py
   with direct links so Ryan can read or organically upvote from mobile/desktop.
4.Never executes automated voting (POST /api/vote).
"""

from __future__ import annotations

import argparse
from datetime import datetime
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
STATE_FILE = Path("/workspace/data/resident_agents_watcher_state.json")

# Known legitimate peer/collaborator accounts that should always be auto-approved
KNOWN_PEER_ACCOUNTS = {
    "prime416",
    "xvx016_aerial",
    "amosstationbot",
}

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
                    return data
        except Exception as e:
            log.warning("Could not read state file %s: %s", STATE_FILE, e)
    return {
        "seen_ids": [],
        "approved_ids": [],
        "contributors_added": [],
        "author_legit_cache": {},
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

    new_notifications: List[Dict[str, Any]] = []
    newly_approved: List[str] = []

    with pixel_session(force=force_pixel) as cdp_url:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()

                if not page.url.startswith("https://old.reddit.com"):
                    page.goto("https://old.reddit.com/r/ResidentAgents/new.json?limit=5", timeout=20000)
                    page.wait_for_timeout(800)

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

                    # Determine whether to notify Ryan on Discord
                    # Notify for any unseen item by someone other than prime416 (or if an item was just rescued from spam!)
                    is_unseen = fullname not in seen_set
                    if author_lower != "prime416" and (is_unseen or auto_approved_now):
                        if is_legit or is_known_peer:
                            permalink = d.get("permalink") or f"/r/ResidentAgents/comments/{d.get('id', '')}/"
                            full_url = f"https://www.reddit.com{permalink}"
                            new_notifications.append(
                                {
                                    "fullname": fullname,
                                    "kind": kind,
                                    "author": author,
                                    "title": d.get("title") or "",
                                    "link_title": d.get("link_title") or "",
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
    if post_discord and not test:
        queue_outbox_message(
            channel=str(ZERO_CHAT_CHANNEL_ID),
            content=card,
            source_turn="resident_agents_watcher",
        )
        # Return empty string to bridge_scheduler when enqueued to outbox so it doesn't double-post
        return True, "", {"card": card, **res}

    return True, card, {"card": card, **res}


def main() -> None:
    parser = argparse.ArgumentParser(description="r/ResidentAgents Subreddit Watcher & Auto-Approver")
    parser.add_argument("--test", "-t", action="store_true", help="Dry-run without mutating Reddit or posting to Discord")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress nominal console output")
    parser.add_argument("--no-post", action="store_true", help="Print card to stdout instead of queuing to outbox")
    parser.add_argument("--force", "-f", action="store_true", help="Force Pixel session even if printer camera is active")
    args = parser.parse_args()

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
