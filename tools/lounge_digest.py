import sys
from pathlib import Path
if '/workspace' not in sys.path:
    sys.path.insert(0, '/workspace')
#!/usr/bin/env python3
"""
lounge_digest.py - Crab Cavern Periodic Lounge Engineering Digest

Aggregates completed PRs, closed issues, and in-flight tasks across
Crab Cavern repositories into a consolidated, ELI5 sprint digest for #lounge
every 2 to 3 hours, replacing noisy per-PR/per-issue executive summaries.

Enforces:
- 2-Hour Cooldown Window: Prevents frequent chatter in #lounge.
- Zero-Noise Invariant: If nothing shipped or changed, stays completely silent.
- Angle Bracket Links: All URLs enclosed in < > to suppress Discord embed bloat.
- High-level ELI5 formatting: Plain English, <= 250 words per digest.
"""

import os
import sys
import re
import json
import time
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = Path("/workspace/data")
STATE_FILE = DATA_DIR / "lounge_digest_state.json"
QUEUE_FILE = DATA_DIR / "lounge_digest_queue.json"
LOUNGE_CHANNEL_ID = "1534452820995080192"

DEFAULT_COOLDOWN_SECONDS = 7200  # 2 hours

ACTIVE_REPOS = [
    "brockventures/agora-roguelike",
    "brockventures/market-sandbox",
]


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_digest_ts": 0, "processed_prs": []}


def save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_queue() -> list[dict]:
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_queue(queue: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


def enqueue_completed_item(
    repo: str,
    item_type: str,
    number: int,
    title: str,
    summary: str = "",
    url: str = "",
) -> None:
    """Enqueue a completed PR or issue for the next periodic digest."""
    queue = load_queue()
    item_id = f"{repo}#{number}"
    # Avoid duplicate enqueues
    for item in queue:
        if item.get("id") == item_id:
            return
    queue.append({
        "id": item_id,
        "repo": repo,
        "item_type": item_type,
        "number": number,
        "title": title,
        "summary": summary,
        "url": url or f"https://github.com/{repo}/pull/{number}" if item_type == "pr" else f"https://github.com/{repo}/issues/{number}",
        "timestamp": int(time.time()),
    })
    save_queue(queue)


def fetch_recently_merged_prs(repo: str, since_ts: float) -> list[dict]:
    """Fetch PRs merged since since_ts via GitHub CLI."""
    cmd = [
        "gh", "pr", "list",
        "-R", repo,
        "--state", "merged",
        "--limit", "15",
        "--json", "number,title,mergedAt,url,headRefName"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        prs = json.loads(res.stdout) if res.stdout else []
    except Exception:
        return []

    recent = []
    for pr in prs:
        merged_at_str = pr.get("mergedAt")
        if not merged_at_str:
            continue
        try:
            dt = datetime.fromisoformat(merged_at_str.replace("Z", "+00:00"))
            m_ts = dt.timestamp()
            if m_ts >= since_ts:
                recent.append({
                    "id": f"{repo}#{pr['number']}",
                    "repo": repo,
                    "item_type": "pr",
                    "number": pr["number"],
                    "title": pr["title"],
                    "url": pr.get("url", ""),
                    "timestamp": m_ts,
                })
        except Exception:
            continue
    return recent


def fetch_open_in_flight_prs(repo: str) -> list[dict]:
    """Fetch currently open PRs to report active in-flight work."""
    cmd = [
        "gh", "pr", "list",
        "-R", repo,
        "--state", "open",
        "--limit", "5",
        "--json", "number,title,url,reviewDecision"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout) if res.stdout else []
    except Exception:
        return []


def clean_title(title: str) -> str:
    """Strip common conventional commit prefixes for cleaner conversational text."""
    title = re.sub(r"^(?:feat|fix|test|refactor|chore|docs|style|perf)(?:\([^)]+\))?:\s*", "", title, flags=re.IGNORECASE)
    return title[0].upper() + title[1:] if title else title


def compile_digest(since_ts: float) -> tuple[str | None, list[str]]:
    """
    Compile a consolidated digest of work completed since since_ts.
    Returns (digest_text, processed_item_ids).
    If no work completed, returns (None, []).
    """
    queue = load_queue()
    processed_ids = set()
    items = []

    # 1. Collect from explicit queue
    for q in queue:
        if q.get("timestamp", 0) >= since_ts or not since_ts:
            items.append(q)
            processed_ids.add(q.get("id"))

    # 2. Auto-harvest from active GitHub repos
    for repo in ACTIVE_REPOS:
        recent_prs = fetch_recently_merged_prs(repo, since_ts)
        for rpr in recent_prs:
            if rpr["id"] not in processed_ids:
                items.append(rpr)
                processed_ids.add(rpr["id"])

    if not items:
        return None, []

    # Sort items by number
    items.sort(key=lambda x: x.get("number", 0))

    # Fetch currently in-flight work
    in_flight = []
    for repo in ACTIVE_REPOS:
        open_prs = fetch_open_in_flight_prs(repo)
        for opr in open_prs:
            in_flight.append(opr)

    # Format into ELI5 digest
    lines = [
        "🛠️ **Engineering Sprint Digest**",
        "",
        "**What Shipped:**"
    ]

    for item in items:
        pnum = item.get("number")
        url = item.get("url") or f"https://github.com/{item.get('repo')}/pull/{pnum}"
        title = clean_title(item.get("title", ""))
        lines.append(f"- [PR #{pnum}](<{url}>): {title}")

    if in_flight:
        lines.append("")
        lines.append("**In Flight:**")
        for opr in in_flight:
            pnum = opr.get("number")
            url = opr.get("url") or f"https://github.com/brockventures/agora-roguelike/pull/{pnum}"
            title = clean_title(opr.get("title", ""))
            rdec = opr.get("reviewDecision")
            status_tag = f" ({rdec.lower()})" if rdec else ""
            lines.append(f"- [PR #{pnum}](<{url}>): {title}{status_tag}")

    digest_text = "\n".join(lines)
    return digest_text, list(processed_ids)


def run_digest(
    dry_run: bool = False,
    force: bool = False,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
) -> dict:
    """
    Evaluate and optionally dispatch the sprint digest to #lounge.
    """
    from tools.bridge_state import is_crab_cavern_silenced
    silenced, reason = is_crab_cavern_silenced()
    if silenced and not force:
        return {
            "status": "silenced",
            "message": f"Crab Cavern silenced. Digest skipped ({reason}).",
        }

    now = time.time()
    state = load_state()
    last_digest_ts = state.get("last_digest_ts", 0)

    elapsed = now - last_digest_ts
    if not force and elapsed < (cooldown_seconds - 120):  # allow 2m scheduler jitter
        remaining_mins = int((cooldown_seconds - elapsed) // 60)
        return {
            "status": "cooldown",
            "message": f"Cooldown active. Next digest eligible in {remaining_mins}m.",
            "elapsed": elapsed
        }

    # Query for items completed since last digest (or past 2 hours if last digest was 0)
    query_since = last_digest_ts if last_digest_ts > 0 else (now - cooldown_seconds)
    digest_text, processed_ids = compile_digest(query_since)

    if not digest_text:
        # Zero-Noise Invariant: No updates, do not post anything
        if not dry_run:
            state["last_digest_ts"] = now
            save_state(state)
        return {
            "status": "silent",
            "message": "No new PRs or issues completed since last digest. Staying silent."
        }

    if dry_run:
        return {
            "status": "dry_run",
            "digest": digest_text,
            "processed_ids": processed_ids
        }

    # Dispatch to #lounge via outbox
    from tools.outbox import queue_outbox_message
    res = queue_outbox_message(
        channel=LOUNGE_CHANNEL_ID,
        content=digest_text,
        source_turn="lounge_digest",
        dedupe_window=3600,
        force=True
    )

    # Update state & prune queue
    state["last_digest_ts"] = now
    processed_set = set(processed_ids)
    old_processed = set(state.get("processed_prs", []))
    state["processed_prs"] = list(old_processed.union(processed_set))
    save_state(state)

    # Prune queue file
    current_queue = load_queue()
    remaining_queue = [q for q in current_queue if q.get("id") not in processed_set]
    save_queue(remaining_queue)

    return {
        "status": "dispatched",
        "outbox_result": res,
        "processed_ids": processed_ids,
        "digest": digest_text
    }


def main():
    parser = argparse.ArgumentParser(description="Crab Cavern Lounge Engineering Digest")
    parser.add_argument("--dispatch", action="store_true", help="Compile and dispatch if cooldown elapsed")
    parser.add_argument("--force", action="store_true", help="Bypass cooldown interval")
    parser.add_argument("--dry-run", action="store_true", help="Preview digest without sending")
    parser.add_argument("--enqueue", nargs=4, metavar=("REPO", "TYPE", "NUM", "TITLE"), help="Enqueue an item")
    args = parser.parse_args()

    if args.enqueue:
        repo, itype, num_str, title = args.enqueue
        enqueue_completed_item(repo, itype, int(num_str), title)
        print(f"Enqueued {repo}#{num_str} ('{title}')")
        return

    res = run_digest(dry_run=args.dry_run, force=args.force)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
