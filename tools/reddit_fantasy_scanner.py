#!/usr/bin/env python3
"""Reddit Fantasy Baseball Scanner & Draft Proposal Sidecar.

Scans r/fantasybaseball for active keeper, trade, and roster dilemma comments,
generates grounded 3-sentence draft replies following Ryan's dugout persona,
and stages candidate cards in #zero-chat for approval.

Follows the 5-layer sidecar lifecycle architecture.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

for _p in ["/workspace", "/workspace/tools"]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reddit_fantasy_scanner")

ZERO_CHAT_CHANNEL_ID = 1542081375287640084
STATE_FILE = Path("/workspace/data/reddit_fantasy_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not read state file: %s", e)
    return {"seen_comments": [], "last_run_at": None, "last_thread_url": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Bound seen comments history
    if len(state.get("seen_comments", [])) > 200:
        state["seen_comments"] = state["seen_comments"][-200:]
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning("Could not save state file: %s", e)


def scan_reddit_via_pixel(timeout_sec: int = 40) -> List[Dict[str, Any]]:
    """Scan r/fantasybaseball using the Pixel 8 Pro browser automation."""
    try:
        from playwright.sync_api import sync_playwright
        from tools.pixel_browser import pixel_session
    except ImportError:
        log.error("Pixel browser or Playwright dependencies unavailable.")
        return []

    candidates: List[Dict[str, Any]] = []

    try:
        with pixel_session(force=True) as cdp_url:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(cdp_url)
                page = browser.contexts[0].pages[0]

                log.info("Navigating to r/fantasybaseball...")
                page.goto("https://www.reddit.com/r/fantasybaseball/", timeout=25000)
                page.wait_for_timeout(3000)

                # Locate Daily / Nightly Anything Goes threads + active front-page discussion threads
                links = page.locator('a[href*="/comments/"]').all()
                target_threads = []
                seen_hrefs = set()
                for l in links:
                    href = l.get_attribute("href") or ""
                    link_text = l.inner_text().strip().replace("\n", " ")
                    if "/comments/" in href and "anything_goes" in href.lower():
                        full_url = (href if href.startswith("http") else f"https://www.reddit.com{href}").split("?")[0].rstrip("/") + "/"
                        if full_url not in seen_hrefs:
                            seen_hrefs.add(full_url)
                            target_threads.append((link_text or "Anything Goes Thread", full_url))

                # Also include top non-recruitment discussion threads so empty offseason Anything Goes threads do not starve discovery
                for l in links:
                    href = l.get_attribute("href") or ""
                    link_text = l.inner_text().strip().replace("\n", " ")
                    if "/r/fantasybaseball/comments/" in href and "recruitment" not in href.lower():
                        full_url = (href if href.startswith("http") else f"https://www.reddit.com{href}").split("?")[0].rstrip("/") + "/"
                        if full_url not in seen_hrefs and link_text and not re.match(r"^\d+\s+comments?$", link_text.lower()):
                            seen_hrefs.add(full_url)
                            target_threads.append((link_text or "Discussion Thread", full_url))
                            if len(target_threads) >= 4:
                                break

                log.info("Found %d target discussion threads", len(target_threads))

                for thread_title, thread_url in target_threads[:4]:
                    log.info("Scanning thread: %s", thread_url)
                    page.goto(thread_url, timeout=25000)
                    page.wait_for_timeout(3000)

                    # Extract comments
                    entries = page.locator('div.comment, shreddit-comment, div[data-type="comment"]').all()
                    log.info("Thread has %d comment elements", len(entries))

                    for entry in entries:
                        # Extract author and stable thingid if present
                        author = entry.get_attribute("author") or "community_member"
                        thingid = entry.get_attribute("thingid") or entry.get_attribute("data-fullname") or entry.get_attribute("id")

                        # Target comment body element specifically to avoid capturing nested child replies
                        body_elem = entry.locator('div[slot="comment"], div.md, div.usertext-body').first
                        if body_elem.count() > 0:
                            raw_text = body_elem.inner_text().strip()
                        else:
                            raw_text = entry.inner_text().strip()

                        # Basic filtering
                        if not raw_text or len(raw_text) < 25:
                            continue
                        if any(b in raw_text.lower() for b in ["automoderator", "deleted", "[removed]", "writers and website owners"]):
                            continue

                        # Extract author if not on element
                        author = entry.get_attribute("author") or ""
                        if not author or author == "community_member":
                            author_elem = entry.locator('a[href*="/user/"], a[slot="author"], span[slot="authorName"]').first
                            if author_elem.count() > 0:
                                author = author_elem.inner_text().strip().replace("u/", "")
                        if not author or author == "community_member":
                            lines = raw_text.splitlines()
                            for line in lines[:3]:
                                if line.startswith("[–]") or "points" in line or "ago" in line:
                                    parts = line.replace("[–]", "").strip().split()
                                    if parts:
                                        author = parts[0]
                                    break
                        if not author:
                            author = "community_member"

                        # Identify dilemma / question while preserving all legitimate user lines (never strip lines that merely contain the word 'points' or 'save' in prose)
                        lines = raw_text.splitlines()
                        action_bar_tokens = {"permalink", "embed", "save", "parent", "report", "give award", "reply", "share"}
                        cleaned_lines = []
                        for l in lines:
                            ls = l.strip()
                            l_low = ls.lower()
                            if ls.startswith("[–]") or ls.startswith("[-]"):
                                continue
                            # Skip Reddit metadata header lines (e.g. 'username 2 points 3 hours ago')
                            if re.search(r"\b\d+\s+points?\b.*\bago\b", l_low):
                                continue
                            # Skip standalone Reddit footer action bar lines
                            words_in_line = set(re.findall(r"[a-z]+", l_low))
                            if words_in_line and words_in_line.issubset(action_bar_tokens | {"award", "give", "child", "children", "comments"}):
                                continue
                            cleaned_lines.append(l)
                        clean_body = "\n".join(cleaned_lines).strip()

                        if not clean_body or len(clean_body) < 15:
                            continue

                        # A genuine dilemma MUST be an inquiry asking for advice/decision, not advice statements
                        has_question = ("?" in clean_body) or any(
                            q in clean_body.lower() for q in [
                                "who should", "should i", "who would you", "thoughts on",
                                "advice on", "help me choose", "which one", "who do i",
                                "do i trade", "do i drop", "do i add", "would you trade"
                            ]
                        )
                        if not has_question:
                            continue

                        # Extract hierarchy depth accurately across Shreddit, Old Reddit (.sitetable.nestedlisting / .child), and New Reddit
                        depth_val = 0
                        depth_attr = entry.get_attribute("depth")
                        parent_id_attr = (
                            entry.get_attribute("parentid")
                            or entry.get_attribute("data-parent-id")
                            or entry.get_attribute("parent-id")
                            or ""
                        )
                        if depth_attr is not None and str(depth_attr).lstrip("-").isdigit():
                            depth_val = int(depth_attr)
                        elif parent_id_attr.startswith("t1_"):
                            depth_val = 1
                        else:
                            try:
                                dom_depth = entry.evaluate(
                                    """el => {
                                        if (el.getAttribute('depth')) return parseInt(el.getAttribute('depth'), 10) || 0;
                                        if ((el.getAttribute('parentid') || '').startsWith('t1_')) return 1;
                                        let p = el.parentElement;
                                        let d = 0;
                                        while (p) {
                                            if (p.matches && (p.matches('shreddit-comment, div.comment, div[data-type="comment"]') || p.classList.contains('child'))) {
                                                d += 1;
                                            }
                                            p = p.parentElement;
                                        }
                                        return d;
                                    }"""
                                )
                                if isinstance(dom_depth, int) and dom_depth > 0:
                                    depth_val = dom_depth
                            except Exception:
                                pass

                        # Also check if raw_text includes a 'parent' action link (Old Reddit only renders 'parent' on replies to other comments, never on top-level comments)
                        if depth_val == 0 and re.search(r"\bparent\b", raw_text.lower()):
                            depth_val = 1

                        # Strictly skip non-top-level comments (replies to other users' dilemmas)
                        if depth_val > 0:
                            continue

                        replies_attr = entry.get_attribute("replies")
                        if replies_attr and replies_attr.isdigit():
                            reply_count = int(replies_attr)
                        else:
                            reply_count = entry.locator('shreddit-comment, div.comment').count()

                        time_ago = ""
                        time_elem = entry.locator('time, span[data-testid="comment_timestamp"]').first
                        if time_elem.count() > 0:
                            time_ago = time_elem.inner_text().strip()

                        # Stable raw_id: uses thingid if available, or first 80 chars so subsequent replies never change the hash
                        stable_key = thingid if thingid else f"{thread_url}::{author}::{hash(clean_body[:80])}"

                        candidates.append({
                            "thread_title": thread_title,
                            "thread_url": thread_url,
                            "author": author,
                            "text": clean_body,
                            "raw_id": stable_key,
                            "depth": depth_val,
                            "reply_count": reply_count,
                            "time_ago": time_ago,
                        })

    except Exception as e:
        log.warning("Pixel browser scan hit error: %s", e)

    return candidates


def _cap_sentences(text: str, max_sentences: int = 2) -> str:
    """Enforce a hard maximum sentence count on generated draft text."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    if len(parts) <= max_sentences:
        return text.strip()
    return " ".join(parts[:max_sentences])


def generate_draft_comment(
    question_text: str,
    author: str,
    thread_title: str = "r/fantasybaseball Anything Goes",
    candidate_ref: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate a friendly, warm 2-sentence draft reply via Ivy (discord-ivy-agent · Gemini 4 Argon Low)."""
    try:
        from tools.llm_sidecar import call_ivy_fantasy_brain
        res = call_ivy_fantasy_brain(
            {
                "text": question_text,
                "author": author,
                "thread_title": thread_title,
            },
            return_context=True,
        )
        if isinstance(res, tuple):
            ivy_draft, ctx = res
            if candidate_ref is not None and isinstance(ctx, dict):
                candidate_ref["big_board_context"] = ctx
        else:
            ivy_draft = str(res or "")
        if ivy_draft and len(ivy_draft.split()) >= 8:
            return _cap_sentences(ivy_draft, max_sentences=2)
    except Exception as e:
        log.warning("Ivy fantasy brain delegation fallback: %s", e)

    text_lower = question_text.lower()

    if any(w in text_lower for w in ["dynasty", "rebuild", "tear down", "absolute zero"]):
        return (
            "A true ground-up dynasty rebuild is realistically a 2 to 3-year window, so flip aging vets early and build your foundation around young bats. "
            "On the pitching side, save your trade capital and use open roster spots to churn the wire for emerging breakout arms and new pitch shapes."
        )
    elif "closer" in text_lower or "save" in text_lower:
        return (
            "Keep an eye on bullpen turnover early in camp, as velocity spikes often tip the job before managers admit it. "
            "Grab the high-whiff setup arms with clean command splits late and let the ninth-inning roles sort themselves out in April."
        )
    elif "keeper" in text_lower or "keep" in text_lower:
        return (
            "With no round penalty in play, lock in the proven five-category anchors at the top of your board before chasing prospect variance. "
            "Elite everyday bats with established plate discipline give you the safest multi-year floor while the rest of your roster takes shape."
        )
    elif "trade" in text_lower:
        return (
            "I lean toward holding the proven production here since the underlying barrel rate and plate discipline back up the floor you are already getting. "
            "From the other owner's side, they probably won't bite unless they're rebuilding or desperate for a 2-for-1 depth piece."
        )
    else:
        return (
            "Banking on underlying plate discipline and walk-to-strikeout ratios over recent streakiness usually pays off in that spot. "
            "I'd hold the steadier floor and let the rest of the league overpay for variance."
        )


def score_candidate(candidate: Dict[str, Any]) -> Tuple[float, List[str]]:
    """Score a dilemma candidate based on engagement potential, depth, and actionable substance."""
    score = 50.0
    reasons = []

    text = candidate.get("text", "").strip()
    text_lower = text.lower()
    words = text.split()
    word_count = len(words)
    reply_count = candidate.get("reply_count", 0)
    depth = candidate.get("depth", 0)
    time_text = candidate.get("time_ago", "").lower()

    # 1. Reply Count / Unanswered Preference
    if reply_count == 0:
        score += 35.0
        reasons.append("Unanswered (+35)")
    elif reply_count == 1:
        score += 10.0
        reasons.append("1 Reply (+10)")
    elif reply_count >= 3:
        score -= 40.0
        reasons.append(f"Crowded ({reply_count} replies, -40)")
    elif reply_count == 2:
        score -= 20.0
        reasons.append("2 Replies (-20)")

    # 2. Hierarchy / Top-Level Placement (Strictly require top-level dilemma; disqualify sub-comments)
    if depth == 0:
        score += 20.0
        reasons.append("Top-Level Dilemma (+20)")
    else:
        score = -100.0
        reasons.append("Sub-comment Disqualified (-100)")
        return round(score, 1), reasons

    # 2b. Reply-Phrasing Guard (Catch replies masquerading as depth=0 when DOM flattens comments)
    reply_phrasing = [
        "i would definitely do this",
        "i'd definitely do this",
        "i would do this",
        "ended the year injured, right?",
        "right? i would",
        "agree with the above",
        "this is the way",
        "take that deal and run",
    ]
    if any(rp in text_lower for rp in reply_phrasing):
        score = -100.0
        reasons.append("Reply-to-Dilemma Phrasing Disqualified (-100)")
        return round(score, 1), reasons

    # 3. Context & League Structure Mention
    league_keywords = [
        "dynasty", "keeper", "12-team", "10-team", "14-team", "16-team",
        "12t", "10t", "roto", "categories", "cats", "h2h", "points league",
        "pts", "fab", "waiver", "rebuild", "contender"
    ]
    matched_league = [kw for kw in league_keywords if kw in text_lower]
    if matched_league:
        score += 20.0
        reasons.append(f"League Context ({matched_league[0]}, +20)")

    # 4. Actionable Trade-Off Words
    tradeoff_keywords = [
        "trade", "keep", "drop", "add", "who would you", "sleeper", "closer",
        "target", "start or sit", "vs", "or should i", "give up", "buy low", "sell high"
    ]
    matched_tradeoffs = [kw for kw in tradeoff_keywords if kw in text_lower]
    if matched_tradeoffs:
        score += 15.0
        reasons.append("Trade-Off Focus (+15)")

    # 5. Length & Substance Sweet Spot
    if word_count < 12:
        score -= 25.0
        reasons.append("Too Brief (-25)")
    elif 15 <= word_count <= 80:
        score += 15.0
        reasons.append("Ideal Length (+15)")
    elif word_count > 150:
        score -= 10.0
        reasons.append("Rambling (-10)")

    # 6. Recency Window
    if any(m in time_text for m in ["m ago", "min", "just now", "1h ago"]):
        score += 15.0
        reasons.append("Fresh Dilemma (+15)")
    elif any(s in time_text for s in ["d ago", "day", "8h ago", "12h ago"]):
        score -= 20.0
        reasons.append("Stale Thread (-20)")

    # 7. Disqualifications / Heavy Penalties
    off_topic = ["touchdown", "quarterback", "fantasy football", "rb1", "wr1", "nfl", "nba", "lebron", "mahomes"]
    if any(ot in text_lower for ot in off_topic):
        score = -50.0
        reasons.append("Off-Topic Disqualified (-50)")

    has_question = ("?" in text) or any(
        q in text_lower for q in [
            "who should", "should i", "who would you", "thoughts on",
            "advice on", "help me choose", "which one", "who do i",
            "do i trade", "do i drop", "do i add", "would you trade"
        ]
    )
    if not has_question:
        score -= 75.0
        reasons.append("No Question (-75)")

    return round(score, 1), reasons


def format_draft_card(candidate: Dict[str, Any], draft: str) -> str:
    """Format candidate comment and draft proposal into an actionable Discord card."""
    author = candidate.get("author", "fellow manager")
    thread_title = candidate.get("thread_title", "r/fantasybaseball Anything Goes")
    thread_url = candidate.get("thread_url", "https://www.reddit.com/r/fantasybaseball/")

    text = candidate.get("text", "").strip()

    # Ensure multiline text has '>' on every non-empty line for clean Discord blockquote
    formatted_quote = "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())

    bb_block = ""
    bb_ctx = candidate.get("big_board_context")
    if isinstance(bb_ctx, dict) and bb_ctx.get("valuations"):
        lc = bb_ctx.get("league_context") or {}
        vals = [v for v in bb_ctx["valuations"] if isinstance(v, dict) and "player_name" in v]
        if vals:
            fmt_label = f"{lc.get('teams', 12)}-Team {lc.get('scoring', 'roto').upper()} · {lc.get('horizon', 'keeper')}"
            top_chips = [
                f"{v['player_name']} (`${round(v.get('league_specific_dollar', 0))}`)"
                for v in vals[:8]
            ]
            bb_block = f"- **Big Board ({fmt_label}):** {', '.join(top_chips)}\n\n"

    return (
        f"⚾ **r/fantasybaseball Discovery & Draft Card**\n\n"
        f"- **Thread:** [**{thread_title}**](<{thread_url}>)\n"
        f"- **Dilemma from u/{author}:**\n"
        f"{formatted_quote}\n\n"
        f"{bb_block}"
        f"- **Proposed Draft (Ivy Brain · Gemini 4 Argon Low · 2-Sentence Cap):**\n"
        f"\"{draft}\"\n\n"
        f"- **Action:** Tap the thread link above to post manually, or reply to approve dispatch once mobile session is authenticated."
    )


def run_reddit_fantasy_scanner(
    test: bool = False,
    quiet: bool = False,
    post_discord: bool = False,
    channel_id: Optional[int] = None,
    *args, **kwargs
) -> Tuple[bool, str, Dict[str, Any]]:
    """Execution entry point conforming to the 5-layer sidecar contract."""
    state = load_state()
    seen = set(state.get("seen_comments", []))

    log.info("Executing Reddit Fantasy Baseball Scanner (test=%s)...", test)
    candidates = scan_reddit_via_pixel()

    if not candidates:
        msg = "No new actionable fantasy baseball dilemmas found in active threads."
        log.info(msg)
        return True, msg, {"count": 0, "status": "nominal"}

    # Score and rank all unseen candidates
    scored_candidates = []
    for cand in candidates:
        cid = cand.get("raw_id")
        if test or (cid and cid not in seen):
            score, reasons = score_candidate(cand)
            cand["quality_score"] = score
            cand["score_reasons"] = reasons
            # Minimum threshold for consideration (relaxed in test mode)
            if score >= (0 if test else 40):
                scored_candidates.append(cand)

    if not scored_candidates:
        msg = "Scanned active threads; no high-quality, actionable dilemma comments found."
        log.info(msg)
        return True, msg, {"count": 0, "status": "no_qualifying_candidates"}

    # Rank descending by quality score
    scored_candidates.sort(key=lambda c: c["quality_score"], reverse=True)
    selected = scored_candidates[0]

    draft = generate_draft_comment(
        selected.get("text", ""),
        selected.get("author", ""),
        selected.get("thread_title", "r/fantasybaseball Anything Goes"),
        candidate_ref=selected,
    )
    card = format_draft_card(selected, draft)

    # Update state
    if not test and selected.get("raw_id"):
        seen.add(selected.get("raw_id"))
        state["seen_comments"] = list(seen)
        state["last_run_at"] = time.strftime("%Y-%m-%d %I:%M %p PT")
        state["last_thread_url"] = selected.get("thread_url")
        save_state(state)

    # Post to Discord if requested
    if post_discord:
        dest_cid = channel_id or ZERO_CHAT_CHANNEL_ID
        try:
            from tools.outbox import send_message
            send_message(dest_cid, card)
            log.info("Dispatched draft card to Discord channel %s", dest_cid)
        except Exception as e:
            log.warning("Could not dispatch to Discord: %s", e)

    return True, card, {"selected": selected, "draft": draft}


def main():
    parser = argparse.ArgumentParser(description="Reddit Fantasy Baseball Scanner")
    parser.add_argument("--test", action="store_true", help="Run in test mode without modifying state")
    parser.add_argument("--quiet", action="store_true", help="Suppress logging")
    parser.add_argument("--post-discord", action="store_true", help="Post candidate draft card to Discord")
    parser.add_argument("--channel-id", type=int, default=ZERO_CHAT_CHANNEL_ID, help="Destination Discord channel ID")
    args = parser.parse_args()

    ok, report, _ = run_reddit_fantasy_scanner(
        test=args.test,
        quiet=args.quiet,
        post_discord=args.post_discord,
        channel_id=args.channel_id
    )
    print(report)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
