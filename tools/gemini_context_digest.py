#!/usr/bin/env python3
"""Zero & Ivy Daily Context Digest (Auto-Archived Gmail Ecosystem Bridge).

Compiles a comprehensive daily briefing of Zero and Ivy activity, decisions,
homelab state, active tasks, reminders, household context, and fantasy baseball
work across all Discord channels, then sends it from the configured agent sender to
the configured recipient and immediately auto-archives it (removes INBOX and
UNREAD labels, applies `Zero-Context` label) via the Gmail API.

This makes the full Zero + Ivy operational memory searchable and retrievable
across consumer Gemini surfaces (Pixel Assistant, Gmail side-panel, and
gemini.google.com via @Gmail / Google Workspace extension) with zero inbox clutter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))
if str(WORKSPACE / "tools") not in sys.path:
    sys.path.insert(0, str(WORKSPACE / "tools"))

import send_mail
import workspace_mcp

PT = ZoneInfo("America/Los_Angeles")
DATA_DIR = WORKSPACE / "data"
MEM_PRIV_DIR = WORKSPACE / "memory" / "private"
MEM_PUB_DIR = WORKSPACE / "memory" / "public"
CHANNEL_HISTORY_FILE = DATA_DIR / "channel_history.json"
TASKS_FILE = DATA_DIR / "tasks.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CONTEXT_LABEL_NAME = "Zero-Context"

# Core channels to summarize
HOME_OPS_CHANNELS = {
    "1542081375287640084": "#zero-chat (Primary Engineering & Personal Ops)",
    "1544955535722545253": "#homelab (Infrastructure & NAS Ops)",
    "1548196930788524094": "#projects (Active Software Projects)",
    "1544953275877556334": "#home-assistant (Smart Home & IoT)",
    "1544955538033348618": "#shopping (Meal Planning & Whole Foods Cart)",
    "1544955532765560924": "#finances (Budget, Cash Sweep & Variance)",
    "1550577910757458015": "#vault (Quantitative Trading & Kalshi)",
    "1555973316752449576": "#3d-print (3D Printing & Slicing)",
    "1544953277592899615": "#steam-deck (Steam Deck & Gaming)",
}

IVY_CHANNELS = {
    "1548196929308065893": "#baseball (Ivy — Sabermetric Assistant GM & Big Board)",
}

CRAB_CAVERN_CHANNELS = {
    "1534436119888793750": "#the-banana-stand (Crab Cavern Engineering)",
    "1551465050072416286": "#side-project (Highball & Outpost Sprints)",
    "1558202642663211169": "#agora (Station Agora Trading & Roguelike)",
    "1534452820995080192": "#lounge (Crab Cavern Social & Banter)",
}


def _parse_ts(msg: dict) -> datetime | None:
    """Parse message timestamp into UTC datetime."""
    raw = msg.get("timestamp", "")
    if not raw:
        return None
    try:
        clean = raw.replace(" UTC", "").strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _clean_msg_snippet(text: str, max_len: int = 320) -> str:
    """Strip noisy tags, reaction directives, and collapse whitespace."""
    if not text:
        return ""
    s = re.sub(r"\[REACT:[^\]]+\]", "", text)
    s = re.sub(r"\[CHOICES:[^\]]+\]", "", s)
    s = re.sub(r"\[GIF\]\([^\)]+\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        return s[: max_len - 3].rstrip() + "..."
    return s


def gather_channel_activity(hours_back: int = 24) -> dict[str, list[str]]:
    """Extract recent dialogue highlights across Zero, Ivy, and Crab Cavern channels."""
    sections: dict[str, list[str]] = {
        "zero": [],
        "ivy": [],
        "crab_cavern": [],
        "threads": [],
    }
    if not CHANNEL_HISTORY_FILE.exists():
        return sections

    try:
        with open(CHANNEL_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return sections

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    for cid, msgs in history.items():
        if not isinstance(msgs, list) or not msgs:
            continue
        recent = [m for m in msgs if _parse_ts(m) and _parse_ts(m) >= cutoff]
        if not recent:
            continue

        ch_name = str(recent[-1].get("channel_name") or cid)
        lines = []
        # Keep up to the last 10 substantive messages per active channel
        for m in recent[-10:]:
            author = m.get("author", "User")
            content = _clean_msg_snippet(m.get("content", ""), max_len=350)
            if not content or content == "[NO_REPLY]":
                continue
            dt_pt = _parse_ts(m).astimezone(PT).strftime("%I:%M %p PT")
            lines.append(f"  - [{dt_pt}] {author}: {content}")

        if not lines:
            continue

        if cid in HOME_OPS_CHANNELS:
            header = HOME_OPS_CHANNELS[cid]
            sections["zero"].append(f"### {header}\n" + "\n".join(lines))
        elif cid in IVY_CHANNELS:
            header = IVY_CHANNELS[cid]
            sections["ivy"].append(f"### {header}\n" + "\n".join(lines))
        elif cid in CRAB_CAVERN_CHANNELS:
            header = CRAB_CAVERN_CHANNELS[cid]
            sections["crab_cavern"].append(f"### {header}\n" + "\n".join(lines))
        elif ch_name.startswith("🧵"):
            sections["threads"].append(f"### Thread: {ch_name}\n" + "\n".join(lines[-5:]))

    return sections


def gather_git_commits(hours_back: int = 24) -> list[str]:
    """Return recent git commits in /workspace over the past N hours."""
    try:
        res = subprocess.run(
            [
                "git",
                "log",
                f"--since={hours_back} hours ago",
                "--pretty=format:- `%h` (%cr): %s",
                "-n",
                "15",
            ],
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        out = (res.stdout or "").strip()
        return [line for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def gather_recent_memory_updates(hours_back: int = 48) -> list[str]:
    """List recently updated memory files and directives."""
    items = []
    cutoff_ts = time.time() - (hours_back * 3600)
    for directory, label in ((MEM_PRIV_DIR, "private"), (MEM_PUB_DIR, "public")):
        if not directory.exists():
            continue
        files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[:12]:
            if p.name == "MEMORY.md":
                continue
            if p.stat().st_mtime >= cutoff_ts:
                dt_str = datetime.fromtimestamp(p.stat().st_mtime, tz=PT).strftime("%b %d %I:%M %p PT")
                # Extract first heading or description
                snippet = ""
                try:
                    raw = p.read_text(encoding="utf-8", errors="ignore")
                    for line in raw.splitlines():
                        line_s = line.strip()
                        if line_s.startswith("description:"):
                            snippet = line_s.split(":", 1)[1].strip().strip('"')
                            break
                        if line_s.startswith("# "):
                            snippet = line_s.lstrip("# ").strip()
                            break
                except Exception:
                    pass
                desc = f" — {snippet}" if snippet else ""
                items.append(f"- [{label}] `{p.name}` (updated {dt_str}){desc}")
    return items


def gather_active_tasks_and_reminders() -> tuple[list[str], list[str]]:
    """Gather active P1/P2 tasks from tasks.json and upcoming reminders from reminders.json."""
    task_lines = []
    if TASKS_FILE.exists():
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            for t in tasks:
                status = str(t.get("status", "")).lower()
                if status in ("completed", "retired", "done"):
                    continue
                prio = str(t.get("priority", "p2")).upper()
                task_lines.append(f"- [{prio}] #{t.get('id')}: {t.get('title')} ({status})")
        except Exception:
            pass

    reminder_lines = []
    if REMINDERS_FILE.exists():
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                rems = json.load(f)
            now_date = datetime.now(PT).strftime("%Y-%m-%d")
            limit_date = (datetime.now(PT) + timedelta(days=21)).strftime("%Y-%m-%d")
            for r in rems if isinstance(rems, list) else rems.get("reminders", []):
                if r.get("completed") or r.get("fired"):
                    continue
                d_str = r.get("date") or r.get("due_date") or ""
                if now_date <= d_str <= limit_date:
                    reminder_lines.append(f"- {d_str}: {r.get('text') or r.get('title')}")
        except Exception:
            pass

    return task_lines, reminder_lines


def gather_ivy_baseball_context() -> list[str]:
    """Pull Ivy's latest baseball memory and league context from Host 2."""
    lines = []
    try:
        from tools.nas_config import run_ssh, HOST_2_IP
        cmd = (
            f"head -n 60 {nas_config.DOCKER_ROOT}/discord-ivy-agent/workspace/memory/MEMORY.md {nas_config.DOCKER_ROOT}/discord-ivy-agent/workspace/memory/sos_legacy_2026.md {nas_config.DOCKER_ROOT}/discord-ivy-agent/workspace/memory/12gm_3to_2026.md 2>/dev/null"
        )
        code, stdout, _ = run_ssh(HOST_2_IP, cmd, timeout=10)
        if code == 0 and stdout.strip():
            for line in stdout.strip().splitlines()[:40]:
                lines.append(line)
    except Exception:
        pass
    return lines


def build_context_digest_email(hours_back: int = 24) -> tuple[str, str]:
    """Build `(subject, body)` for the daily Zero & Ivy context digest."""
    now_pt = datetime.now(PT)
    date_str = now_pt.strftime("%Y-%m-%d")
    pretty_date = now_pt.strftime("%A, %B %d, %Y (%I:%M %p PT)")

    subject = f"[Zero & Ivy Daily Context Digest] — {date_str}"

    # 1. Quota snapshot
    quota_block = ""
    try:
        from tools.quota_card import build_quota_card
        ok, card, _ = build_quota_card()
        if ok:
            quota_block = card.replace("```text\n", "").replace("```", "").strip()
    except Exception:
        pass

    # 2. Channel activity
    activity = gather_channel_activity(hours_back=hours_back)
    commits = gather_git_commits(hours_back=hours_back)
    mem_updates = gather_recent_memory_updates(hours_back=48)
    tasks, reminders = gather_active_tasks_and_reminders()
    ivy_mem = gather_ivy_baseball_context()

    body_parts = [
        f"# Zero & Ivy — Daily Operational Context Digest ({pretty_date})",
        "",
        "This automated digest bridges Ryan Brock's custom Google Antigravity (`agy`) agents — "
        "**Zero** (autonomous homelab, software engineering, household & quant partner) and "
        "**Ivy** (Sabermetric Fantasy Baseball Assistant GM & Big Board valuation engine) — "
        "into Google Workspace so Gemini across Pixel, Gmail, and web has full continuity.",
        "",
        "---",
        "",
        "## 1. Core Identity & Architecture Baseline",
        f"- **Owner:** Ryan Brock (`{getattr(workspace_mcp, 'DEFAULT_CC', '')}`), Senior L6 PM/TPM at Google (Pixel Product Quality).",
        f"- **Zero (`{getattr(workspace_mcp, 'DEFAULT_FROM', 'Zero')}`):** Runs in `discord-antigravity-agent` on Synology NAS Host 2 (`.84`), managing Host 1 (`.82`, Media/Arr/Home Assistant/Nginx) and Host 2 (`.84`, Baseball/Dockhand/Zero). Dashboard: https://zero.brock.ventures",
        "- **Ivy:** Runs in `discord-ivy-agent` on Host 2 (`.84`) in `#baseball`. Manages the 2026/2027 BIG projection system, Dynasty 3-year valuations, and Ryan's fantasy baseball leagues (`Sons of Samhorn Legacy` keeper league, `12GM 3TO`, `SoS Dynasty`). Dashboard: https://ivy.brock.ventures",
    ]

    if quota_block:
        body_parts.extend([
            "",
            "## 2. Live Gemini Weekly Quota Status",
            quota_block,
        ])

    body_parts.extend([
        "",
        f"## 3. Zero & Ryan Activity (Last {hours_back} Hours)",
    ])
    if activity["zero"]:
        body_parts.extend(activity["zero"])
    else:
        body_parts.append("- No new messages in primary Home Ops channels during this window.")

    if activity["threads"]:
        body_parts.append("\n### Active Threads")
        body_parts.extend(activity["threads"])

    body_parts.extend([
        "",
        f"## 4. Ivy & Fantasy Baseball (`#baseball` — Last {hours_back} Hours)",
    ])
    if activity["ivy"]:
        body_parts.extend(activity["ivy"])
    else:
        body_parts.append("- No new `#baseball` chat messages in the last 24 hours.")

    if ivy_mem:
        body_parts.append("\n### Ivy League & Valuation Reference Snapshot")
        body_parts.extend(ivy_mem[:25])

    body_parts.extend([
        "",
        "## 5. Active Tasks & Upcoming Reminders",
        "### Open Tasks (`tasks.json`)",
    ])
    body_parts.extend(tasks[:20] if tasks else ["- No open tasks."])

    if reminders:
        body_parts.append("\n### Upcoming 21-Day Reminders")
        body_parts.extend(reminders[:15])

    body_parts.extend([
        "",
        "## 6. Recent Memory & Directive Updates (Last 48 Hours)",
    ])
    body_parts.extend(mem_updates if mem_updates else ["- No memory file changes in the last 48 hours."])

    body_parts.extend([
        "",
        f"## 7. Recent Code & Tooling Commits (Last {hours_back} Hours)",
    ])
    body_parts.extend(commits if commits else ["- No new git commits in the last 24 hours."])

    if activity["crab_cavern"]:
        body_parts.extend([
            "",
            f"## 8. Crab Cavern Multi-Agent Collaboration (Last {hours_back} Hours)",
        ])
        body_parts.extend(activity["crab_cavern"])

    body_parts.extend([
        "",
        "---",
        "Keywords: Zero, Ivy, Antigravity, agy, Brock Homelab, Big Board, Sons of Samhorn, Daily Context Digest",
    ])

    return subject, "\n".join(body_parts)


def ensure_gmail_label(label_name: str = CONTEXT_LABEL_NAME) -> str | None:
    """Find or create a custom Gmail label and return its label ID."""
    headers = workspace_mcp._auth_headers()
    req = urllib.request.Request(f"{GMAIL_BASE}/labels", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for lbl in data.get("labels", []):
        if str(lbl.get("name", "")).lower() == label_name.lower():
            return lbl.get("id")

    # Create label if it doesn't exist
    payload = json.dumps({
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }).encode("utf-8")
    create_req = urllib.request.Request(
        f"{GMAIL_BASE}/labels",
        data=payload,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(create_req, timeout=15) as resp:
        created = json.loads(resp.read().decode("utf-8"))
        return created.get("id")


def auto_archive_digest(subject: str, sent_msg_id: str = "", sent_thread_id: str = "") -> dict[str, Any]:
    """Remove INBOX and UNREAD labels from the digest thread/messages and apply Zero-Context."""
    headers = workspace_mcp._auth_headers()
    label_id = None
    try:
        label_id = ensure_gmail_label(CONTEXT_LABEL_NAME)
    except Exception as e:
        print(f"[ContextDigest] Notice creating/fetching label: {e}", file=sys.stderr)

    modified_ids = []

    # Wait briefly for Cloudflare Email Routing delivery to land the inbound copy if separate
    time.sleep(3.0)

    # Search for messages matching this exact subject in the last day
    q = urllib.parse.quote(f'subject:"{subject}" newer_than:2d')
    search_req = urllib.request.Request(f"{GMAIL_BASE}/messages?q={q}&maxResults=10", headers=headers)
    msg_ids = set()
    if sent_msg_id:
        msg_ids.add(sent_msg_id)
    try:
        with urllib.request.urlopen(search_req, timeout=15) as resp:
            sdata = json.loads(resp.read().decode("utf-8"))
            for m in sdata.get("messages", []):
                if m.get("id"):
                    msg_ids.add(m["id"])
    except Exception as e:
        print(f"[ContextDigest] Search warning: {e}", file=sys.stderr)

    mod_body: dict[str, Any] = {
        "removeLabelIds": ["INBOX", "UNREAD"],
    }
    if label_id:
        mod_body["addLabelIds"] = [label_id]
    payload = json.dumps(mod_body).encode("utf-8")

    for mid in msg_ids:
        try:
            mreq = urllib.request.Request(
                f"{GMAIL_BASE}/messages/{mid}/modify",
                data=payload,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(mreq, timeout=15) as resp:
                json.loads(resp.read().decode("utf-8"))
                modified_ids.append(mid)
        except Exception as e:
            print(f"[ContextDigest] Failed to modify message {mid}: {e}", file=sys.stderr)

    return {
        "archived_message_ids": modified_ids,
        "label_id": label_id,
        "label_name": CONTEXT_LABEL_NAME,
    }


def run_daily_context_digest(
    hours_back: int = 24,
    dry_run: bool = False,
    test: bool = False,
    quiet: bool = False,
) -> tuple[bool, str, dict]:
    """Generate, send, and auto-archive the daily Zero & Ivy Context Digest."""
    try:
        subject, body = build_context_digest_email(hours_back=hours_back)
        if dry_run:
            return True, f"Dry-run generated `{subject}` ({len(body)} chars).", {"subject": subject, "chars": len(body)}

        to_addr = getattr(workspace_mcp, "DEFAULT_CC", "") or os.environ.get("ADMIN_CC_EMAIL", "")
        from_addr = getattr(workspace_mcp, "DEFAULT_FROM", "") or os.environ.get("AGENT_FROM_EMAIL", "Zero")

        send_res = send_mail.send_email(
            to=to_addr,
            subject=subject,
            body=body,
            from_email=from_addr,
            cc="",
            is_draft=False,
        )
        msg_id = send_res.get("id", "")
        thread_id = send_res.get("threadId", "")

        archive_res = auto_archive_digest(subject=subject, sent_msg_id=msg_id, sent_thread_id=thread_id)
        summary = (
            f"Dispatched and auto-archived `{subject}` "
            f"(msg_id=`{msg_id}`, label=`{archive_res.get('label_name')}`, "
            f"archived={len(archive_res.get('archived_message_ids', []))} msg(s), {len(body):,} chars)."
        )
        meta = {
            "subject": subject,
            "message_id": msg_id,
            "thread_id": thread_id,
            "chars": len(body),
            **archive_res,
        }
        return True, ("" if quiet else summary), meta
    except Exception as e:
        return False, f"⚠️ **Daily Context Digest Failed:** `{e}`", {"error": str(e)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Send & auto-archive the Zero & Ivy Daily Context Digest.")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window in hours (default: 24)")
    parser.add_argument("--dry-run", action="store_true", help="Print digest without sending email")
    parser.add_argument("--test", "-t", action="store_true", help="Test mode")
    parser.add_argument("--quiet", "-q", action="store_true", help="Silent when nominal")
    args = parser.parse_args()

    if args.dry_run:
        subj, body = build_context_digest_email(hours_back=args.hours)
        print(f"Subject: {subj}\n" + "=" * 60 + f"\n{body}")
        return 0

    ok, msg, meta = run_daily_context_digest(
        hours_back=args.hours,
        dry_run=args.dry_run,
        test=args.test,
        quiet=args.quiet,
    )
    if msg:
        print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
