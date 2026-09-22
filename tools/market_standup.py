#!/usr/bin/env python3
"""
market_standup.py - Crab Cavern Autonomous Market Sandbox Standup Dispatcher

Runs daily at 07:00 PM PT (19:00 PT) via BridgeScheduler in schedule.json.
Autonomously syncs progress, open PRs, and blockers across Zero, Amos, and Aerial
for the brockventures/market-sandbox project in #the-banana-stand.

Workflow:
1. Queries GitHub API for brockventures/market-sandbox (open PRs, latest commits).
2. Assembles structured status and next-actions brief.
3. Claims Banana mutex lock via tools/banana.py.
4. Dispatches handoff envelope (kind: status, floor: open) to #the-banana-stand (1534436119888793750) via tools/outbox.py.
5. Releases Banana mutex.
6. Records execution state.
"""

import sys
import os
import json
import time
import argparse
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from banana import claim, release, BananaError, BananaBlockedError
from outbox import queue_outbox_message

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
HISTORY_FILE = DATA_DIR / "market_standup_history.json"
TARGET_CHANNEL = "1534436119888793750"  # #the-banana-stand
REPO = "brockventures/market-sandbox"
ROBOTS_ROLE = "<@&1543462881624858624>"  # @robot role snowflake
DISCORD_EPOCH = 1420070400000


def get_discord_bot_token() -> str:
    """Retrieve Discord bot token from environment or secrets."""
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    env_json_path = Path("/secrets/env.json")
    if env_json_path.exists():
        try:
            with open(env_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("DISCORD_BOT_TOKEN", "").strip()
        except Exception:
            pass
    return ""


def datetime_to_snowflake(dt: datetime) -> int:
    """Convert timezone-aware datetime to Discord snowflake integer."""
    dt_utc = dt.astimezone(timezone.utc)
    timestamp_ms = int(dt_utc.timestamp() * 1000)
    return (timestamp_ms - DISCORD_EPOCH) << 22


def get_previous_evening_window(now_pt: datetime) -> tuple[datetime, datetime]:
    """Return the (start_pt, end_pt) window for the previous day's 7:00 PM to 11:59:59 PM PT."""
    yesterday = (now_pt - timedelta(days=1)).date()
    start_pt = datetime(yesterday.year, yesterday.month, yesterday.day, 19, 0, 0, tzinfo=PT)
    end_pt = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59, tzinfo=PT)
    return start_pt, end_pt


def fetch_evening_discord_messages(
    channel_id: str | int = TARGET_CHANNEL,
    start_pt: datetime | None = None,
    end_pt: datetime | None = None,
    token: str | None = None
) -> list[dict]:
    """
    Directly query Discord REST API for messages in channel_id between start_pt and end_pt.
    Uses Discord snowflake pagination, handles chronological ordering, and retrieves thread messages.
    """
    if not token:
        token = get_discord_bot_token()
    if not token:
        print("[MarketStandup] Warning: DISCORD_BOT_TOKEN not found, skipping Discord chat fetch")
        return []

    if start_pt is None or end_pt is None:
        now_pt = datetime.now(PT)
        start_pt, end_pt = get_previous_evening_window(now_pt)

    after_snowflake = datetime_to_snowflake(start_pt)
    before_snowflake = datetime_to_snowflake(end_pt)

    messages = []
    curr_after = after_snowflake

    while True:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages?after={curr_after}&limit=100"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "ZeroDiscordBridge/1.0"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
                if not batch:
                    break

                batch.sort(key=lambda m: int(m["id"]))

                reached_end = False
                for m in batch:
                    mid = int(m["id"])
                    if mid > before_snowflake:
                        reached_end = True
                        break
                    messages.append(m)

                if reached_end or len(batch) < 100:
                    break

                curr_after = batch[-1]["id"]
        except Exception as e:
            print(f"[MarketStandup] Discord API fetch error: {e}", file=sys.stderr)
            break

    # Also check if any message has an active thread attached and fetch its messages
    thread_messages = []
    for m in messages:
        thread_id = m.get("thread", {}).get("id")
        if thread_id:
            try:
                t_url = f"https://discord.com/api/v10/channels/{thread_id}/messages?after={after_snowflake}&limit=100"
                t_req = urllib.request.Request(
                    t_url,
                    headers={
                        "Authorization": f"Bot {token}",
                        "User-Agent": "ZeroDiscordBridge/1.0"
                    }
                )
                with urllib.request.urlopen(t_req, timeout=10) as t_resp:
                    t_batch = json.loads(t_resp.read().decode("utf-8"))
                    for tm in t_batch:
                        if int(tm["id"]) <= before_snowflake:
                            tm["is_thread"] = True
                            tm["parent_id"] = m["id"]
                            thread_messages.append(tm)
            except Exception as te:
                print(f"[MarketStandup] Thread fetch error for {thread_id}: {te}", file=sys.stderr)

    all_msgs = messages + thread_messages
    all_msgs.sort(key=lambda m: int(m["id"]))
    return all_msgs


def format_chat_transcript(messages: list[dict], max_chars: int = 15000) -> str:
    """Format fetched Discord messages into a compact transcript for LLM synthesis."""
    if not messages:
        return ""

    lines = []
    for m in messages:
        author = m.get("author", {}).get("username", "Unknown")
        content = m.get("content", "").strip()
        if not content:
            continue
        if len(content) > 400:
            content = content[:400] + "..."
        prefix = "[Thread] " if m.get("is_thread") else ""
        lines.append(f"{prefix}{author}: {content}")

    transcript = "\n".join(lines)
    if len(transcript) > max_chars:
        transcript = transcript[-max_chars:]
    return transcript


def get_github_headers() -> dict:
    """Construct HTTP headers for GitHub API queries, including auth token if available."""
    headers = {
        "User-Agent": "ZeroMarketStandup/1.0",
        "Accept": "application/vnd.github.v3+json"
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        env_json_path = Path("/secrets/env.json")
        if env_json_path.exists():
            try:
                with open(env_json_path, "r", encoding="utf-8") as f:
                    token = json.load(f).get("GITHUB_TOKEN", "").strip()
            except Exception:
                pass
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def get_repo_state() -> dict:
    """Fetch recent open PRs, open tasks/issues, and commit activity from GitHub API and local git."""
    state = {"open_prs": [], "open_issues": [], "recent_commits": [], "error": None}
    headers = get_github_headers()

    def fetch_api(endpoint: str):
        url = f"https://api.github.com/repos/{REPO}/{endpoint}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # 1. Fetch open PRs
    try:
        raw_prs = fetch_api("pulls?state=open&per_page=10")
        for pr in raw_prs:
            state["open_prs"].append({
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "author": {"login": pr.get("user", {}).get("login", "unknown")},
                "headRefName": pr.get("head", {}).get("ref", "")
            })
    except Exception as e:
        print(f"[MarketStandup] Warning: GitHub PRs fetch error: {e}", file=sys.stderr)

    # 2. Fetch open task board issues (filtering out PRs)
    try:
        raw_issues = fetch_api("issues?state=open&per_page=15")
        for iss in raw_issues:
            if "pull_request" not in iss:
                state["open_issues"].append({
                    "number": iss.get("number"),
                    "title": iss.get("title", ""),
                    "labels": iss.get("labels", []),
                    "assignees": iss.get("assignees", []),
                    "state": iss.get("state")
                })
    except Exception as e:
        print(f"[MarketStandup] Warning: GitHub issues fetch error: {e}", file=sys.stderr)

    # 3. Fetch recent commits on main (GitHub API first, local git fallback)
    try:
        raw_commits = fetch_api("commits?per_page=4")
        for c in raw_commits:
            sha = c.get("sha", "")[:7]
            msg = c.get("commit", {}).get("message", "").split("\n")[0]
            author = c.get("commit", {}).get("author", {}).get("name", "unknown")
            state["recent_commits"].append(f"`{sha}` {msg} ({author})")
    except Exception as e:
        print(f"[MarketStandup] Warning: GitHub commits API error: {e}", file=sys.stderr)

    # Local git fallback for recent commits if API call failed
    if not state["recent_commits"]:
        try:
            res_git = subprocess.run(
                ["git", "-C", "/workspace/market-sandbox", "log", "-n", "3", "--format=`%h` %s (%an)"],
                capture_output=True, text=True, timeout=5
            )
            if res_git.returncode == 0 and res_git.stdout.strip():
                state["recent_commits"] = [l.strip() for l in res_git.stdout.strip().splitlines() if l.strip()]
        except Exception:
            pass

    return state


def synthesize_standing_agenda(state: dict, chat_transcript: str = "", date_label: str = "", staged_items: list = None) -> str:
    """Generate dynamic standing agenda and next steps based on repository activity, staged RFCs, open task board issues, and discussions."""
    open_prs = state.get("open_prs", [])
    open_issues = state.get("open_issues", [])
    recent_commits = state.get("recent_commits", [])

    chat_context_block = ""
    if chat_transcript.strip():
        chat_context_block = (
            f"\nRecent Collaboration & Design Discussions"
            f"{f' on {date_label}' if date_label else ''}:\n"
            f"{chat_transcript}\n"
        )
    
    staged_context_block = ""
    if staged_items:
        staged_context_block = (
            f"\nStaged Feature Requests & RFC Proposals for Tonight's Agenda:\n"
            f"{json.dumps(staged_items, indent=2)}\n"
        )

    issues_context_block = ""
    if open_issues:
        issues_context_block = (
            f"\nOpen GitHub Task Board Issues:\n"
            f"{json.dumps(open_issues[:6], indent=2)}\n"
        )
    
    prompt = (
        f"You are Zero posting the daily multi-agent standup for repo brockventures/market-sandbox with Amos and Aerial in #the-banana-stand.\n\n"
        f"Open PRs:\n{json.dumps(open_prs, indent=2)}\n\n"
        f"{issues_context_block}\n"
        f"Recent Commits:\n{json.dumps(recent_commits, indent=2)}\n\n"
        f"{staged_context_block}\n"
        f"{chat_context_block}\n"
        f"Synthesize numbered bullet points for 'Standing Agenda & Peer Check-in' assigning or checking in on Amos (<@1468012353206354197>), Aerial (<@1542035925603713086>), and Zero based on the repository state, open PRs, open GitHub tasks, and staged feature requests (Marvin has been removed from the active project docket). Keep each line crisp, specific, and actionable (<90 characters per bullet). Output ONLY the numbered lines."
    )
    try:
        res = subprocess.run(
            ["agy", "--model=gemini-3.8-flash-low", "--disable-slash-commands", f"-p={prompt}"],
            capture_output=True, text=True, timeout=25
        )
        if res.returncode == 0 and res.stdout.strip():
            lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip() and (l[0].isdigit() or l.startswith("-") or l.startswith("*"))]
            if len(lines) >= 2:
                return "\n".join(lines[:3])
    except Exception as e:
        print(f"[MarketStandup] LLM agenda synthesis fallback: {e}")

    # Dynamic fallback based on live repository state & open items
    items = []
    if staged_items:
        for idx, it in enumerate(staged_items[:3], 1):
            topic = it.get("topic", "")
            assignee = it.get("assignee", "Team")
            task_id = f" (#{it.get('task_id')})" if it.get("task_id") else ""
            details = it.get("details", "")
            items.append(f"{idx}. {assignee}{task_id}: {topic} — {details[:65]}...")
    elif open_prs:
        for idx, pr in enumerate(open_prs[:3], 1):
            items.append(f"{idx}. Review & land PR #{pr['number']}: {pr['title'][:65]}...")
    else:
        items.append("1. Zero (<@1542285964213358633>): Corporate Warfare & Order Flow — Market variance, short-selling, & circuit breakers.")
        items.append("2. Amos (<@1468012353206354197>): Spatial Routing & Engine Physics — Station price deltas and burn economics.")
        items.append("3. Aerial (<@1542035925603713086>): Client & Frontend Telemetry — Streaming feeds and visualizer integration.")
    return "\n".join(items)


def build_standup_message(state: dict, now_pt: datetime, chat_transcript: str = "", date_label: str = "") -> str:
    """Construct the handoff envelope and standup text."""
    date_str = now_pt.strftime("%Y-%m-%d %I:%M %p PT")
    
    # Check for staged feature requests / agenda items
    staged_file = DATA_DIR / "market_standup_staged.json"
    staged_items = []
    staged_section = ""
    if staged_file.exists():
        try:
            staged_raw = json.loads(staged_file.read_text())
            staged_items = staged_raw.get("items", []) if isinstance(staged_raw, dict) else staged_raw
            if staged_items:
                lines = []
                for it in staged_items:
                    topic = it.get("topic", "")
                    task_id = it.get("task_id")
                    task_tag = f" (Task #{task_id})" if task_id else ""
                    assignee = it.get("assignee")
                    assignee_tag = f" [{assignee}]" if assignee else ""
                    details = it.get("details", "")
                    if len(details) > 120:
                        details = details[:117].rstrip() + "..."
                    lines.append(f"- **{topic}{task_tag}{assignee_tag}:** {details}")
                staged_section = "\n\n**Feature Requests & RFC Proposals on Deck:**\n" + "\n".join(lines)
        except Exception as e:
            print(f"[MarketStandup] Error loading staged items: {e}", file=sys.stderr)

    # Format open PRs
    prs_summary = []
    if state.get("open_prs"):
        for pr in state["open_prs"]:
            prs_summary.append(f"- PR #{pr['number']}: {pr['title']} ({pr.get('author', {}).get('login', 'unknown')})")
    else:
        prs_summary.append("- No open PRs currently outstanding.")

    # Format open issues / GitHub task board
    issues_summary = []
    if state.get("open_issues"):
        for issue in state["open_issues"][:4]:
            num = issue.get("number")
            title = issue.get("title", "")
            if len(title) > 65:
                title = title[:62].rstrip() + "..."
            labels = [l.get("name") for l in issue.get("labels", []) if isinstance(l, dict) and l.get("name")]
            tag_str = f" *({', '.join(labels[:2])})*" if labels else ""
            issues_summary.append(f"- Issue #{num}: {title}{tag_str}")
    else:
        issues_summary.append("- No open issues on board.")

    commits_summary = []
    if state.get("recent_commits"):
        for c in state["recent_commits"]:
            commits_summary.append(f"- {c}")
    else:
        commits_summary.append("- No recent commit activity fetched.")

    prs_text = "\n".join(prs_summary)
    issues_text = "\n".join(issues_summary)
    commits_text = "\n".join(commits_summary)
    agenda_text = synthesize_standing_agenda(state, chat_transcript=chat_transcript, date_label=date_label, staged_items=staged_items)

    envelope = f"""🍌 ```handoff
{{
  "v": 1,
  "kind": "status",
  "reply": "optional",
  "floor": "open",
  "scope": "channel",
  "subject": "agent-collaborative-project",
  "round": 1
}}
```"""
    header = f"**Autonomous Daily Standup — Market Sandbox** ({date_str})\n\nCurrent repository health on [`{REPO}`](https://github.com/{REPO}):"
    footer = f"Any blockers on deck? {ROBOTS_ROLE} (@robot) Floor is open: brainstorm new mechanics, event shocks, and feature ideas for the Agora game/app."

    body_parts = [
        header,
        f"**Open Tasks & Issues (GitHub Task Board):**\n{issues_text}",
        f"**Open PRs:**\n{prs_text}",
        f"**Recent Activity:**\n{commits_text}",
    ]
    if staged_section.strip():
        body_parts.append(staged_section.strip())
    body_parts.append(f"**Standing Agenda & Peer Check-in:**\n{agenda_text}")
    body_parts.append(footer)

    msg = f"{envelope}\n\n" + "\n\n".join(body_parts)

    # Enforce strict Discord 2,000 char ceiling guard (<1900 chars)
    if len(msg) > 1900:
        # Phase 1: condense staged section if present
        if staged_items:
            condensed_lines = [f"- **{it.get('topic')}:** {it.get('details', '')[:60]}..." for it in staged_items[:3]]
            staged_condensed = "**Feature Requests on Deck:**\n" + "\n".join(condensed_lines)
            body_parts = [
                header,
                f"**Open Tasks & Issues (GitHub Task Board):**\n{issues_text}",
                f"**Open PRs:**\n{prs_text}",
                f"**Recent Activity:**\n{commits_text}",
                staged_condensed,
                f"**Standing Agenda & Peer Check-in:**\n{agenda_text}",
                footer,
            ]
            msg = f"{envelope}\n\n" + "\n\n".join(body_parts)

        # Phase 2: if still over 1900, condense open issues and strip staged section
        if len(msg) > 1900:
            if state.get("open_issues"):
                condensed_issues = [f"- Issue #{i.get('number')}: {i.get('title', '')[:45]}..." for i in state["open_issues"][:3]]
                issues_condensed = "\n".join(condensed_issues)
            else:
                issues_condensed = "- No open issues on board."
            body_parts = [
                header,
                f"**Open Tasks & Issues (GitHub Task Board):**\n{issues_condensed}",
                f"**Open PRs:**\n{prs_text}",
                f"**Recent Activity:**\n{commits_text}",
                f"**Standing Agenda & Peer Check-in:**\n{agenda_text}",
                footer,
            ]
            msg = f"{envelope}\n\n" + "\n\n".join(body_parts)

        # Phase 3: hard clamp guaranteeing <=1900 chars under all circumstances
        if len(msg) > 1900:
            msg = msg[:1830].rstrip() + "\n\n... *(content condensed for Discord 2,000 ceiling)*"

    return msg


def dispatch_market_standup(test_mode: bool = False, quiet: bool = False, window_mode: str = "previous_day", dry_run: bool = False, **kwargs) -> dict:
    """Execute the standup check and dispatch."""
    if dry_run:
        test_mode = True
    now_pt = datetime.now(PT)
    state = get_repo_state()

    if window_mode == "today":
        start_pt = datetime(now_pt.year, now_pt.month, now_pt.day, 19, 0, 0, tzinfo=PT)
        end_pt = datetime(now_pt.year, now_pt.month, now_pt.day, 23, 59, 59, tzinfo=PT)
    else:
        start_pt, end_pt = get_previous_evening_window(now_pt)

    date_label = start_pt.strftime("%Y-%m-%d")
    raw_msgs = fetch_evening_discord_messages(TARGET_CHANNEL, start_pt=start_pt, end_pt=end_pt)
    chat_transcript = format_chat_transcript(raw_msgs)
    state["evening_messages_count"] = len(raw_msgs)

    message = build_standup_message(state, now_pt, chat_transcript=chat_transcript, date_label=date_label)

    if test_mode:
        if not quiet:
            print("[TEST MODE] Constructed message:\n" + message)
        return {
            "status": "ok",
            "test": True,
            "prs": len(state.get("open_prs", [])),
            "evening_messages": len(raw_msgs),
            "window": f"{start_pt.strftime('%Y-%m-%d %I:%M %p PT')} -> {end_pt.strftime('%I:%M %p PT')}"
        }

    # Step 1: Claim Banana Mutex (best-effort; do not abort on external API errors)
    claimed = False
    try:
        claim("zero-market-standup")
        claimed = True
    except BananaBlockedError as e:
        print(f"[MarketStandup] Banana blocked by {e.holder}, deferring/aborting: {e}", file=sys.stderr)
        return {"status": "error", "error": f"Banana blocked by {e.holder}"}
    except Exception as e:
        print(f"[MarketStandup] Warning: Error claiming Banana ({e}). Proceeding with outbox dispatch...", file=sys.stderr)

    try:
        # Step 2: Queue to #the-banana-stand
        res = queue_outbox_message(TARGET_CHANNEL, message)
        
        # Step 3: Record history
        record = {
            "timestamp": now_pt.isoformat(),
            "time_pt": now_pt.strftime("%Y-%m-%d %I:%M %p PT"),
            "prs_count": len(state.get("open_prs", [])),
            "evening_messages_count": len(raw_msgs),
            "outbox_id": res.get("id"),
            "status": "dispatched"
        }
        history = []
        if HISTORY_FILE.exists():
            try:
                history = json.loads(HISTORY_FILE.read_text())
            except Exception:
                history = []
        history.append(record)
        HISTORY_FILE.write_text(json.dumps(history[-30:], indent=2))

        return {
            "status": "ok",
            "outbox_id": res.get("id"),
            "prs": len(state.get("open_prs", [])),
            "evening_messages": len(raw_msgs)
        }
    finally:
        # Step 4: Always release Banana Mutex
        if claimed:
            try:
                release()
            except Exception as e:
                print(f"[WARN] Failed to release Banana token: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Market Sandbox Autonomous Daily Standup Dispatcher")
    parser.add_argument("--test", action="store_true", help="Run test mode without posting or claiming mutex")
    parser.add_argument("--dispatch", action="store_true", help="Force immediate dispatch to #the-banana-stand")
    parser.add_argument("--window", choices=["previous_day", "today"], default="previous_day", help="Evening window to query from Discord (default: previous_day)")
    parser.add_argument("--quiet", action="store_true", help="Suppress stdout output")
    args = parser.parse_args()

    if args.test:
        res = dispatch_market_standup(test_mode=True, quiet=args.quiet, window_mode=args.window)
        print(json.dumps(res, indent=2))
        sys.exit(0)
    elif args.dispatch:
        res = dispatch_market_standup(test_mode=False, quiet=args.quiet, window_mode=args.window)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("status") == "ok" else 1)
    else:
        # Default sidecar wrapper invocation
        res = dispatch_market_standup(test_mode=False, quiet=args.quiet, window_mode=args.window)
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
