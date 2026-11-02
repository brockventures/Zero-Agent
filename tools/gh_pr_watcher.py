#!/usr/bin/env python3
"""gh_pr_watcher.py - Automated GitHub PR & Review Discord Watcher Sidecar.

Monitors open PRs authored by Zero or requesting Zero's review across active repositories
(mcarmody/lomin, brockventures/*, arcane103/*).
Tracks seen reviews, comments, and reviewDecision states in /workspace/data/gh_pr_watcher_state.json.
Dispatches actionable Discord wake-ups to #forge and #zero-ops via tools/outbox.py
when new reviews (CHANGES_REQUESTED, APPROVED, COMMENTED) or review requests arrive.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fnmatch
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

# Ensure local repo path takes precedence over global /workspace
_local_repo = str(Path(__file__).resolve().parent.parent)
if _local_repo in sys.path:
    sys.path.remove(_local_repo)
sys.path.insert(0, _local_repo)
for _p in ("/workspace", "/workspace/tools"):
    if _p not in sys.path:
        sys.path.append(_p)

try:
    from tools.outbox import queue_outbox_message
except ImportError:
    # Graceful fallback for isolated test environments
    def queue_outbox_message(
        channel: str | int,
        content: str,
        source_turn: str = "gh_pr_watcher",
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "channel": channel,
            "content": content,
            "source": source_turn,
            "status": "queued",
        }


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("gh_pr_watcher")

# Channel configuration
FORGE_CHANNEL_ID = 1562899941595160657
ZERO_OPS_CHANNEL_ID = 1544953279664889888
DEFAULT_CHANNELS: list[str | int] = [FORGE_CHANNEL_ID, ZERO_OPS_CHANNEL_ID]

DEFAULT_STATE_FILE = Path("/workspace/data/gh_pr_watcher_state.json")
DEFAULT_SCHEDULE_FILE = Path("/workspace/data/schedule.json")
DEFAULT_TARGET_REPOS = [
    "mcarmody/lomin",
    "brockventures/*",
    "arcane103/*",
]

# Whitelisted repositories for event-driven webhooks, email doorbells, and auto-merge
WHITELISTED_REPOS: set[str] = {
    "mcarmody/lomin",
    "arcane103/mirrormere",
    "brockventures/Zero-Gotchi",
    "brockventures/market-sandbox",
    "brockventures/Zero-Agent",
}

# Whitelisted Crab Cavern authors for automated review and squash auto-merges
WHITELISTED_AUTHORS: set[str] = {
    "mcarmody",
    "arcane103",
    "aerial-bot",
    "amos-bot",
    "marvin-bot",
    "ryanbrock",
    "zero-brock",
    "brockventures",
}


def ensure_schedule_registered(
    schedule_file: Path = DEFAULT_SCHEDULE_FILE,
) -> bool:
    """Register gh_pr_watcher in /workspace/data/schedule.json (every 3m) if not present."""
    if not schedule_file.exists() or schedule_file.resolve() == DEFAULT_SCHEDULE_FILE.resolve():
        return False
    try:
        raw = schedule_file.read_text(encoding="utf-8")
        entries = json.loads(raw)
        if not isinstance(entries, list):
            return False
        for item in entries:
            if isinstance(item, dict) and item.get("id") == "gh_pr_watcher":
                return False
        entries.append(
            {
                "id": "gh_pr_watcher",
                "name": "GitHub PR Watcher & Auto-Merge",
                "enabled": True,
                "schedule_type": "interval",
                "interval_seconds": 180,
                "cron_expression": "*/3 * * * *",
                "prompt": "Run GitHub PR review and auto-merge watcher using python3 /workspace/tools/gh_pr_watcher.py",
                "catchup_if_missed": False,
                "channel_id": FORGE_CHANNEL_ID,
            }
        )
        tmp_file = schedule_file.with_suffix(f".tmp.{os.getpid()}")
        tmp_file.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        tmp_file.replace(schedule_file)
        return True
    except Exception as exc:
        log.warning("Failed ensuring gh_pr_watcher in %s: %s", schedule_file, exc)
        return False


def is_ci_passing_or_empty(status_rollup: Any) -> bool:
    """Return True if CI status checks are all passing or empty/unconfigured."""
    if not status_rollup:
        return True

    # Handle single dictionary summary
    if isinstance(status_rollup, dict):
        state = str(status_rollup.get("state") or status_rollup.get("status") or "").upper()
        if state in ("FAILURE", "ERROR"):
            return False
        if state in ("PENDING", "IN_PROGRESS", "QUEUED"):
            return False
        checks = status_rollup.get("contexts") or status_rollup.get("checks") or []
        if not checks:
            return state in ("SUCCESS", "COMPLETED", "")
        status_rollup = checks

    if isinstance(status_rollup, list):
        for check in status_rollup:
            if not isinstance(check, dict):
                continue
            conclusion = str(check.get("conclusion") or "").upper()
            state = str(check.get("state") or "").upper()
            status = str(check.get("status") or "").upper()

            # Definite failure states
            if conclusion in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED", "STARTUP_FAILURE"):
                return False
            if state in ("FAILURE", "ERROR"):
                return False

            # In-progress / pending states block merge until finished
            if status in ("IN_PROGRESS", "QUEUED", "WAITING", "REQUESTED"):
                return False
            if state in ("PENDING", "EXPECTED"):
                return False

    return True


def try_auto_merge_pr(
    repo: str,
    pr_number: int | str,
    pr_data: dict[str, Any],
    dry_run: bool = False,
) -> bool:
    """Attempt squash auto-merge on an approved PR from a whitelisted author with green CI.

    Checks:
    1. Author is in WHITELISTED_AUTHORS.
    2. reviewDecision == 'APPROVED' or clean peer review from a whitelisted collaborator.
    3. CI status rollup is passing or empty.

    Executes:
    gh pr merge <num> --repo <repo> --squash --auto
    (with fallback to gh pr merge <num> --repo <repo> --squash --delete-branch if
    auto-merge setting is disabled on that repository).
    """
    try:
        num = int(pr_number)
    except (ValueError, TypeError):
        return False

    # 1. Author must be whitelisted
    author = pr_data.get("author")
    if isinstance(author, dict):
        author_login = str(author.get("login") or "").strip().lower()
    else:
        author_login = str(author or "").strip().lower()

    if not author_login or author_login not in WHITELISTED_AUTHORS:
        log.info(
            "PR %s#%s author '%s' is not whitelisted for auto-merge",
            repo,
            num,
            author_login,
        )
        return False

    # 2. Check review decision & clean whitelisted peer approval
    review_decision = str(pr_data.get("reviewDecision") or "").upper()
    if review_decision == "CHANGES_REQUESTED":
        log.info(
            "PR %s#%s has CHANGES_REQUESTED; blocking auto-merge",
            repo,
            num,
        )
        return False

    # Check for any active CHANGES_REQUESTED in reviews
    latest_reviews = pr_data.get("latestReviews") or []
    all_reviews = latest_reviews if latest_reviews else (pr_data.get("reviews") or [])
    combined_reviews = list(latest_reviews) + [
        r for r in (pr_data.get("reviews") or []) if r not in latest_reviews
    ]
    for rev in all_reviews:
        if str(rev.get("state") or "").upper() == "CHANGES_REQUESTED":
            log.info(
                "PR %s#%s has active CHANGES_REQUESTED review; blocking auto-merge",
                repo,
                num,
            )
            return False

    is_approved = (review_decision == "APPROVED")
    if not is_approved:
        # Check if there is a clean peer approval from a whitelisted collaborator / Zero
        # (including when Zero approves a whitelisted collaborator PR on brockventures/* repos)
        for rev in combined_reviews:
            if str(rev.get("state") or "").upper() == "APPROVED":
                rev_author = rev.get("author")
                if isinstance(rev_author, dict):
                    rev_login = str(rev_author.get("login") or "").strip().lower()
                else:
                    rev_login = str(rev_author or "").strip().lower()
                if rev_login in WHITELISTED_AUTHORS and rev_login != author_login:
                    is_approved = True
                    break

    if not is_approved:
        log.info(
            "PR %s#%s is not approved (decision=%s); skipping auto-merge",
            repo,
            num,
            review_decision or "NONE",
        )
        return False

    # 3. Check CI status rollup (must be passing or empty)
    ci_rollup = pr_data.get("statusCheckRollup")
    if not is_ci_passing_or_empty(ci_rollup):
        log.info(
            "PR %s#%s CI checks are not passing/complete; skipping auto-merge",
            repo,
            num,
        )
        return False

    # 4. Execute squash auto-merge
    if dry_run:
        log.info(
            "[DRY-RUN] gh pr merge %s --repo %s --squash --auto",
            num,
            repo,
        )
        return True

    log.info("Executing auto-merge for %s#%s (author: %s)...", repo, num, author_login)
    auto_res = run_gh(["pr", "merge", str(num), "--repo", repo, "--squash", "--auto"])
    if auto_res.returncode == 0:
        log.info("Armed squash auto-merge for %s#%s", repo, num)
        return True

    log.warning(
        "gh pr merge --auto failed for %s#%s (%s); attempting fallback --squash --delete-branch",
        repo,
        num,
        auto_res.stderr.strip(),
    )

    fallback_res = run_gh(["pr", "merge", str(num), "--repo", repo, "--squash", "--delete-branch"])
    if fallback_res.returncode == 0:
        log.info("Successfully merged %s#%s with fallback --squash --delete-branch", repo, num)
        return True

    log.error(
        "gh pr merge fallback failed for %s#%s: %s",
        repo,
        num,
        fallback_res.stderr.strip(),
    )
    return False


def run_gh(
    args: list[str],
    timeout: int = 30,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute gh command safely with standard timeouts and capture output."""
    cmd = ["gh"] + args
    return subprocess.run(
        cmd,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def get_authenticated_user() -> str:
    """Retrieve logged-in GitHub username from gh auth or environment fallback."""
    try:
        res = run_gh(["api", "/user", "--jq", ".login"])
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception as exc:
        log.warning("Failed querying gh api /user: %s", exc)

    return os.environ.get("GH_USER", "brockventures")


def load_state(state_file: Path) -> dict[str, Any]:
    """Load watcher state from disk or return a clean initialized state."""
    default_state: dict[str, Any] = {
        "version": 1,
        "last_run": None,
        "seen_review_ids": [],
        "seen_comment_ids": [],
        "seen_review_requests": [],
        "review_decisions": {},
        "auto_merged_prs": [],
    }
    if not state_file.exists():
        return default_state

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return default_state
            for k, v in default_state.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception as exc:
        log.warning(
            "Failed loading state from %s: %s; using default state",
            state_file,
            exc,
        )
        return default_state


def save_state(state: dict[str, Any], state_file: Path) -> None:
    """Persist watcher state to disk atomically."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    for k in (
        "seen_review_ids",
        "seen_comment_ids",
        "seen_review_requests",
        "auto_merged_prs",
    ):
        if isinstance(state.get(k), (set, list)):
            state[k] = sorted(list(set(state[k])))

    tmp_file = state_file.with_suffix(f".tmp.{os.getpid()}")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        tmp_file.replace(state_file)
    except Exception as exc:
        if tmp_file.exists():
            tmp_file.unlink(missing_ok=True)
        log.error("Failed saving state to %s: %s", state_file, exc)
        raise


def matches_target_patterns(repo: str, target_patterns: list[str] | None) -> bool:
    """Return True if repo matches any pattern in target_patterns.

    If target_patterns is empty or None, all repos match (returns True).
    Patterns can be exact repo names ('mcarmody/lomin') or wildcards ('brockventures/*').
    Matching is case-insensitive.
    """
    if not target_patterns:
        return True
    patterns = [p.strip().lower() for p in target_patterns if p.strip()]
    if not patterns:
        return True
    repo_clean = str(repo or "").strip().lower()
    if not repo_clean:
        return False
    for pat in patterns:
        if pat.endswith("/*"):
            owner = pat[:-2]
            if repo_clean.startswith(owner + "/"):
                return True
        elif repo_clean == pat or fnmatch.fnmatch(repo_clean, pat):
            return True
    return False


def discover_open_prs(
    target_patterns: list[str] | None = None,
    current_user: str | None = None,
) -> list[dict[str, Any]]:
    """Discover all open PRs authored by user, requesting review, or in target repos."""
    if target_patterns is None:
        target_patterns = DEFAULT_TARGET_REPOS
    if current_user is None:
        current_user = get_authenticated_user()

    discovered: dict[tuple[str, int], dict[str, Any]] = {}

    def _add_pr(
        repo: str,
        number: int | str | None,
        title: str = "",
        url: str = "",
        author: str = "",
        review_decision: str | None = None,
    ) -> None:
        repo_clean = str(repo or "").strip()
        if not repo_clean or not number:
            return
        if not matches_target_patterns(repo_clean, target_patterns):
            return
        try:
            num = int(number)
        except (ValueError, TypeError):
            return

        key = (repo_clean.lower(), num)
        if key not in discovered:
            discovered[key] = {
                "repo": repo_clean,
                "number": num,
                "title": title or f"PR #{num}",
                "url": url or f"https://github.com/{repo_clean}/pull/{num}",
                "author": author,
                "reviewDecision": review_decision,
            }

    # 1. Search PRs authored by @me across all repos
    try:
        res = run_gh([
            "search",
            "prs",
            "--state=open",
            "--author=@me",
            "--json",
            "repository,number,title,url,author",
        ])
        if res.returncode == 0:
            for item in json.loads(res.stdout or "[]"):
                repo_name = (item.get("repository") or {}).get("nameWithOwner", "")
                _add_pr(
                    repo=repo_name,
                    number=item.get("number"),
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    author=(item.get("author") or {}).get("login", current_user),
                )
    except Exception as exc:
        log.warning("Failed querying prs authored by @me: %s", exc)

    # 2. Search PRs where review is requested from @me
    try:
        res = run_gh([
            "search",
            "prs",
            "--state=open",
            "--review-requested=@me",
            "--json",
            "repository,number,title,url,author",
        ])
        if res.returncode == 0:
            for item in json.loads(res.stdout or "[]"):
                repo_name = (item.get("repository") or {}).get("nameWithOwner", "")
                _add_pr(
                    repo=repo_name,
                    number=item.get("number"),
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    author=(item.get("author") or {}).get("login", ""),
                )
    except Exception as exc:
        log.warning("Failed querying prs review-requested: %s", exc)

    # 3. Target repo patterns
    for pat in target_patterns:
        pat = pat.strip()
        if not pat:
            continue
        if pat.endswith("/*"):
            owner = pat[:-2]
            try:
                res = run_gh([
                    "search",
                    "prs",
                    "--state=open",
                    f"--owner={owner}",
                    "--json",
                    "repository,number,title,url,author",
                ])
                if res.returncode == 0:
                    for item in json.loads(res.stdout or "[]"):
                        repo_name = (item.get("repository") or {}).get("nameWithOwner", "")
                        _add_pr(
                            repo=repo_name,
                            number=item.get("number"),
                            title=item.get("title", ""),
                            url=item.get("url", ""),
                            author=(item.get("author") or {}).get("login", ""),
                        )
            except Exception as exc:
                log.warning("Failed querying prs for owner %s: %s", owner, exc)
        else:
            try:
                res = run_gh([
                    "pr",
                    "list",
                    "--repo",
                    pat,
                    "--state",
                    "open",
                    "--json",
                    "number,title,url,author,reviewDecision",
                ])
                if res.returncode == 0:
                    for item in json.loads(res.stdout or "[]"):
                        _add_pr(
                            repo=pat,
                            number=item.get("number"),
                            title=item.get("title", ""),
                            url=item.get("url", ""),
                            author=(item.get("author") or {}).get("login", ""),
                            review_decision=item.get("reviewDecision"),
                        )
            except Exception as exc:
                log.warning("Failed querying pr list for repo %s: %s", pat, exc)

    # 4. Ingest unread/recent PullRequest notifications
    try:
        res = run_gh(["api", "/notifications"])
        if res.returncode == 0:
            for notif in json.loads(res.stdout or "[]"):
                subj = notif.get("subject") or {}
                if subj.get("type") == "PullRequest":
                    api_url = subj.get("url") or ""
                    parts = api_url.split("/repos/")
                    if len(parts) == 2:
                        subparts = parts[1].split("/pulls/")
                        if len(subparts) == 2 and subparts[1].isdigit():
                            r_name = subparts[0]
                            num = int(subparts[1])
                            _add_pr(
                                repo=r_name,
                                number=num,
                                title=subj.get("title", ""),
                                url=f"https://github.com/{r_name}/pull/{num}",
                                author="",
                            )
    except Exception as exc:
        log.warning("Failed querying notifications: %s", exc)

    return list(discovered.values())


def get_pr_details(repo: str, number: int) -> dict[str, Any] | None:
    """Fetch complete PR reviews, comments, and reviewDecision via gh pr view."""
    try:
        res = run_gh([
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,url,author,reviewDecision,reviews,latestReviews,reviewRequests,comments,statusCheckRollup",
        ])
        if res.returncode != 0:
            log.warning(
                "gh pr view failed for %s#%s: %s",
                repo,
                number,
                res.stderr.strip(),
            )
            return None

        data = json.loads(res.stdout or "{}")
        data["repo"] = repo
        return data
    except Exception as exc:
        log.warning("Failed fetching PR details for %s#%s: %s", repo, number, exc)
        return None


def truncate_body(text: str, max_length: int = 1200) -> str:
    """Format and truncate body text cleanly for Discord formatting ceilings."""
    clean = (text or "").strip()
    if not clean:
        return "*(No review body provided)*"
    if len(clean) > max_length:
        return clean[:max_length].rstrip() + "\n... *(truncated)*"
    return clean


def format_review_message(
    repo: str,
    pr_number: int,
    title: str,
    url: str,
    review: dict[str, Any],
    current_decision: str,
) -> str:
    """Build structured Discord notification card for a PR review."""
    rev_state = review.get("state", "COMMENTED")
    reviewer = (review.get("author") or {}).get("login", "unknown")
    submitted_at = review.get("submittedAt", "")
    body_text = truncate_body(review.get("body", ""))

    emoji_map = {
        "CHANGES_REQUESTED": "⚠️",
        "APPROVED": "✅",
        "COMMENTED": "💬",
        "DISMISSED": "🔄",
    }
    emoji = emoji_map.get(rev_state, "🔔")

    lines = [
        f"{emoji} **GitHub PR Review: {rev_state}**",
        f"**Repo:** `{repo}` | **PR:** [#{pr_number} {title}]({url})",
        f"**Reviewer:** @{reviewer} | **Overall Decision:** `{current_decision or 'NONE'}`",
    ]
    if submitted_at:
        lines.append(f"**Submitted At:** `{submitted_at}`")
    lines.append("")
    lines.append("**Key Findings & Review Body:**")
    lines.append(f">>> {body_text}")
    return "\n".join(lines)


def format_review_request_message(
    repo: str,
    pr_number: int,
    title: str,
    url: str,
    reviewer: str,
    author: str,
) -> str:
    """Build structured Discord notification card for a new review request."""
    return "\n".join([
        "👀 **GitHub Review Requested**",
        f"**Repo:** `{repo}` | **PR:** [#{pr_number} {title}]({url})",
        f"**Requested Reviewer:** @{reviewer}",
        f"**PR Author:** @{author}",
    ])


def format_comment_message(
    repo: str,
    pr_number: int,
    title: str,
    url: str,
    comment: dict[str, Any],
) -> str:
    """Build structured Discord notification card for a top-level PR comment."""
    author = (comment.get("author") or {}).get("login", "unknown")
    body_text = truncate_body(comment.get("body", ""))
    return "\n".join([
        "💬 **GitHub PR Comment**",
        f"**Repo:** `{repo}` | **PR:** [#{pr_number} {title}]({url})",
        f"**Commenter:** @{author}",
        "",
        f">>> {body_text}",
    ])


def dispatch_notification(
    content: str,
    channels: list[str | int] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Dispatch formatted alert card to configured Discord channels via outbox."""
    if channels is None:
        channels = DEFAULT_CHANNELS

    results: list[dict[str, Any]] = []
    if dry_run:
        for ch in channels:
            log.info("[DRY-RUN] Outbox dispatch to %s:\n%s", ch, content)
            results.append({
                "channel": ch,
                "content": content,
                "status": "dry_run",
            })
        return results

    for ch in channels:
        try:
            res = queue_outbox_message(
                channel=ch,
                content=content,
                source_turn="gh_pr_watcher",
            )
            results.append(res)
        except Exception as exc:
            log.warning("Failed queuing outbox message to %s: %s", ch, exc)

    return results


def check_pr(
    pr_details: dict[str, Any],
    state: dict[str, Any],
    current_user: str,
    channels: list[str | int] | None = None,
    dry_run: bool = False,
    seed_only: bool = False,
) -> dict[str, Any]:
    """Check a single PR for new reviews, comments, or review requests and notify."""
    if channels is None:
        channels = DEFAULT_CHANNELS

    repo = pr_details.get("repo", "")
    pr_number = pr_details.get("number", 0)
    title = pr_details.get("title", f"PR #{pr_number}")
    url = pr_details.get("url", f"https://github.com/{repo}/pull/{pr_number}")
    review_decision = pr_details.get("reviewDecision") or "NONE"

    seen_reviews: set[str] = set(state.setdefault("seen_review_ids", []))
    seen_comments: set[str] = set(state.setdefault("seen_comment_ids", []))
    seen_reqs: set[str] = set(state.setdefault("seen_review_requests", []))
    review_decisions: dict[str, str] = state.setdefault("review_decisions", {})

    stats = {
        "reviews_notified": 0,
        "comments_notified": 0,
        "review_requests_notified": 0,
        "decision_changed": False,
        "auto_merged": False,
    }

    # 1. Check reviews (CHANGES_REQUESTED, APPROVED, COMMENTED, etc.)
    for rev in pr_details.get("reviews", []):
        rev_id = str(rev.get("id") or "").strip()
        author_login = (rev.get("author") or {}).get("login", "")
        submitted_at = rev.get("submittedAt", "")
        if not rev_id:
            rev_id = f"rev:{repo}#{pr_number}:{author_login}:{submitted_at}"

        # Ignore self-authored reviews
        if author_login and author_login.lower() == current_user.lower():
            seen_reviews.add(rev_id)
            continue

        if rev_id in seen_reviews:
            continue

        seen_reviews.add(rev_id)
        rev_state = rev.get("state", "COMMENTED")
        if rev_state in ("CHANGES_REQUESTED", "APPROVED", "COMMENTED", "DISMISSED"):
            if not seed_only:
                msg = format_review_message(
                    repo=repo,
                    pr_number=pr_number,
                    title=title,
                    url=url,
                    review=rev,
                    current_decision=review_decision,
                )
                dispatch_notification(msg, channels=channels, dry_run=dry_run)
                stats["reviews_notified"] += 1

    # 2. Check review requests
    for req in pr_details.get("reviewRequests", []):
        req_login = req.get("login") or req.get("name") or ""
        if not req_login:
            continue
        req_key = f"{repo}#{pr_number}:req:{req_login}"
        if req_key in seen_reqs:
            continue

        seen_reqs.add(req_key)
        if not seed_only:
            pr_author = (pr_details.get("author") or {}).get("login", "unknown")
            msg = format_review_request_message(
                repo=repo,
                pr_number=pr_number,
                title=title,
                url=url,
                reviewer=req_login,
                author=pr_author,
            )
            dispatch_notification(msg, channels=channels, dry_run=dry_run)
            stats["review_requests_notified"] += 1

    # 3. Check comments
    for com in pr_details.get("comments", []):
        com_id = str(com.get("id") or "").strip()
        author_login = (com.get("author") or {}).get("login", "")
        created_at = com.get("createdAt", "")
        if not com_id:
            com_id = f"com:{repo}#{pr_number}:{author_login}:{created_at}"

        if author_login and author_login.lower() == current_user.lower():
            seen_comments.add(com_id)
            continue

        if com_id in seen_comments:
            continue

        seen_comments.add(com_id)
        if not seed_only:
            msg = format_comment_message(
                repo=repo,
                pr_number=pr_number,
                title=title,
                url=url,
                comment=com,
            )
            dispatch_notification(msg, channels=channels, dry_run=dry_run)
            stats["comments_notified"] += 1

    # 4. Check reviewDecision transition
    pr_key = f"{repo}#{pr_number}"
    old_decision = review_decisions.get(pr_key)
    review_decisions[pr_key] = review_decision
    if old_decision and old_decision != review_decision and review_decision != "NONE":
        stats["decision_changed"] = True

    # 5. Check and attempt auto-merge for whitelisted authors
    if not seed_only:
        auto_merged_prs = state.setdefault("auto_merged_prs", [])
        if review_decision == "CHANGES_REQUESTED":
            while pr_key in auto_merged_prs:
                auto_merged_prs.remove(pr_key)
        elif pr_key not in auto_merged_prs:
            did_merge = try_auto_merge_pr(
                repo=repo,
                pr_number=pr_number,
                pr_data=pr_details,
                dry_run=dry_run,
            )
            if did_merge:
                stats["auto_merged"] = True
                if not dry_run:
                    auto_merged_prs.append(pr_key)

    # Sync back to state dict
    state["seen_review_ids"] = list(seen_reviews)
    state["seen_comment_ids"] = list(seen_comments)
    state["seen_review_requests"] = list(seen_reqs)
    state["review_decisions"] = review_decisions
    if not seed_only:
        state["auto_merged_prs"] = list(state.get("auto_merged_prs", []))

    return stats


def poll_and_notify(
    state_file: Path = DEFAULT_STATE_FILE,
    target_patterns: list[str] | None = None,
    channels: list[str | int] | None = None,
    dry_run: bool = False,
    seed_only: bool = False,
    current_user: str | None = None,
) -> dict[str, Any]:
    """Execute complete poll cycle across open PRs and update state."""
    if target_patterns is None:
        target_patterns = DEFAULT_TARGET_REPOS
    if channels is None:
        channels = DEFAULT_CHANNELS
    if current_user is None:
        current_user = get_authenticated_user()

    log.info(
        "Polling GitHub PRs for user %s (patterns: %s)",
        current_user,
        target_patterns,
    )
    state = load_state(state_file)

    open_prs = discover_open_prs(
        target_patterns=target_patterns, current_user=current_user
    )
    log.info("Discovered %d open PRs", len(open_prs))

    total_stats = {
        "prs_checked": 0,
        "reviews_notified": 0,
        "comments_notified": 0,
        "review_requests_notified": 0,
        "decisions_changed": 0,
        "auto_merged": 0,
    }

    for pr_info in open_prs:
        repo = pr_info["repo"]
        num = pr_info["number"]
        details = get_pr_details(repo, num)
        if not details:
            continue

        st = check_pr(
            pr_details=details,
            state=state,
            current_user=current_user,
            channels=channels,
            dry_run=dry_run,
            seed_only=seed_only,
        )
        total_stats["prs_checked"] += 1
        total_stats["reviews_notified"] += st["reviews_notified"]
        total_stats["comments_notified"] += st["comments_notified"]
        total_stats["review_requests_notified"] += st["review_requests_notified"]
        if st["decision_changed"]:
            total_stats["decisions_changed"] += 1
        if st.get("auto_merged"):
            total_stats["auto_merged"] += 1

    if not dry_run:
        save_state(state, state_file)
        log.info("State updated successfully at %s", state_file)

    return total_stats


def handle_event_pr(
    repo: str,
    pr_number: int,
    state_file: Path = DEFAULT_STATE_FILE,
    channels: list[str | int] | None = None,
    dry_run: bool = False,
    current_user: str | None = None,
) -> dict[str, Any] | None:
    """Fetch authenticated PR details via gh pr view and evaluate reviews/auto-merge."""
    if current_user is None:
        current_user = get_authenticated_user()
    if channels is None:
        channels = DEFAULT_CHANNELS

    details = get_pr_details(repo, pr_number)
    if not details:
        log.warning("Could not fetch PR details for %s#%s", repo, pr_number)
        return None

    state = load_state(state_file)
    stats = check_pr(
        pr_details=details,
        state=state,
        current_user=current_user,
        channels=channels,
        dry_run=dry_run,
    )
    if not dry_run:
        save_state(state, state_file)
    return stats


def parse_github_email_doorbell(
    raw_headers: dict[str, Any],
    trigger_eval: bool = False,
    state_file: Path = DEFAULT_STATE_FILE,
    channels: list[str | int] | None = None,
    dry_run: bool = False,
    current_user: str | None = None,
) -> tuple[str, int] | None:
    """Zero-trust dumb doorbell: verify github.com DKIM and extract (repo, pr_number).

    Verifies:
    1. Authentication-Results contains dkim=pass for github.com.
    2. From header contains notifications@github.com.
    3. Extracts (repo, pr_number) strictly from List-ID, Message-ID, In-Reply-To, References.
    4. Validates repo is in WHITELISTED_REPOS.
    5. NEVER reads or returns the email body.
    """
    if not isinstance(raw_headers, dict):
        return None

    def _get_val(key: str) -> str:
        k_lower = key.lower()
        for k, v in raw_headers.items():
            if k.lower() == k_lower:
                if isinstance(v, list):
                    return "\n".join(str(item) for item in v)
                return str(v or "")
        return ""

    # 1. Verify DKIM
    auth_results = (
        _get_val("Authentication-Results")
        or _get_val("ARC-Authentication-Results")
        or _get_val("Authentication-Results-Original")
    ).lower()

    if "dkim=pass" not in auth_results:
        log.warning("Email doorbell rejected: dkim=pass not found in Authentication-Results")
        return None

    # If domain attribution is present in Authentication-Results, ensure it references github.com
    if any(marker in auth_results for marker in ("header.i", "header.d", "domain=")):
        if "github.com" not in auth_results:
            log.warning(
                "Email doorbell rejected: DKIM pass is not for github.com (%s)",
                auth_results,
            )
            return None

    # 2. Verify From header
    from_header = _get_val("From").lower()
    if "notifications@github.com" not in from_header:
        log.warning(
            "Email doorbell rejected: From header is not notifications@github.com (%s)",
            from_header,
        )
        return None

    # 3. Extract (repo, pr_number) strictly from safe headers
    candidate_keys = ["List-ID", "Message-ID", "In-Reply-To", "References"]
    pr_regex = re.compile(
        r"(?:https?://github\.com/|<)?([a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+)/pull/(\d+)",
        re.IGNORECASE,
    )

    repo_cand: str | None = None
    pr_num: int | None = None

    for key in candidate_keys:
        val = _get_val(key)
        if not val:
            continue
        match = pr_regex.search(val)
        if match:
            repo_cand = match.group(1).strip()
            try:
                pr_num = int(match.group(2))
                break
            except (ValueError, TypeError):
                continue

    # Fallback for List-ID: <repo.owner.github.com> + Message-ID /pull/(\d+)
    if not repo_cand:
        list_id = _get_val("List-ID")
        list_match = re.search(
            r"<([a-zA-Z0-9_\-\.]+)\.([a-zA-Z0-9_\-\.]+)\.github\.com>", list_id
        )
        if list_match:
            potential_repo = f"{list_match.group(2)}/{list_match.group(1)}"
            for k in ["Message-ID", "In-Reply-To", "References"]:
                num_match = re.search(r"/pull/(\d+)", _get_val(k))
                if num_match:
                    repo_cand = potential_repo
                    pr_num = int(num_match.group(1))
                    break

    if not repo_cand or pr_num is None:
        log.info("Email doorbell: no repo/pull/number found in headers")
        return None

    # 4. Whitelist validation
    matched_repo: str | None = None
    for allowed_repo in WHITELISTED_REPOS:
        if repo_cand.lower() == allowed_repo.lower():
            matched_repo = allowed_repo
            break

    if not matched_repo:
        log.warning(
            "Email doorbell rejected: repository '%s' is not in WHITELISTED_REPOS",
            repo_cand,
        )
        return None

    if trigger_eval:
        handle_event_pr(
            repo=matched_repo,
            pr_number=pr_num,
            state_file=state_file,
            channels=channels,
            dry_run=dry_run,
            current_user=current_user,
        )

    return (matched_repo, pr_num)


def parse_discord_github_webhook_embed(
    embed: dict[str, Any],
    trigger_eval: bool = False,
    state_file: Path = DEFAULT_STATE_FILE,
    channels: list[str | int] | None = None,
    dry_run: bool = False,
    current_user: str | None = None,
) -> tuple[str, int] | None:
    """Extract (repo, pr_number) from Discord /github webhook embed validated against WHITELISTED_REPOS."""
    if not isinstance(embed, dict):
        return None

    pr_url_pattern = re.compile(
        r"https?://github\.com/([a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+)/pull/(\d+)",
        re.IGNORECASE,
    )

    repo_cand: str | None = None
    pr_num: int | None = None

    # 1. Check direct url field
    url = str(embed.get("url") or "")
    match = pr_url_pattern.search(url)
    if match:
        repo_cand = match.group(1)
        pr_num = int(match.group(2))

    # 2. Check title field
    if not repo_cand:
        title = str(embed.get("title") or "")
        match = pr_url_pattern.search(title)
        if match:
            repo_cand = match.group(1)
            pr_num = int(match.group(2))
        else:
            repo_match = re.search(
                r"\[([a-zA-Z0-9_\-\.]+/[a-zA-Z0-9_\-\.]+)(?::[^\]]+)?\]",
                title,
            )
            pr_match = re.search(r"#(\d+)", title)
            if repo_match and pr_match:
                repo_cand = repo_match.group(1)
                pr_num = int(pr_match.group(1))

    # 3. Check author.url
    if not repo_cand:
        author_url = str((embed.get("author") or {}).get("url") or "")
        match = pr_url_pattern.search(author_url)
        if match:
            repo_cand = match.group(1)
            pr_num = int(match.group(2))

    # 4. Check description or fields
    if not repo_cand:
        desc = str(embed.get("description") or "")
        match = pr_url_pattern.search(desc)
        if match:
            repo_cand = match.group(1)
            pr_num = int(match.group(2))

    if not repo_cand and "fields" in embed and isinstance(embed["fields"], list):
        for f in embed["fields"]:
            if isinstance(f, dict):
                f_val = str(f.get("value") or "")
                match = pr_url_pattern.search(f_val)
                if match:
                    repo_cand = match.group(1)
                    pr_num = int(match.group(2))
                    break

    if not repo_cand or pr_num is None:
        return None

    # Validate against WHITELISTED_REPOS
    matched_repo: str | None = None
    for allowed_repo in WHITELISTED_REPOS:
        if repo_cand.lower() == allowed_repo.lower():
            matched_repo = allowed_repo
            break

    if not matched_repo:
        log.info(
            "Discord webhook embed: repository '%s' is not in WHITELISTED_REPOS",
            repo_cand,
        )
        return None

    if trigger_eval:
        handle_event_pr(
            repo=matched_repo,
            pr_number=pr_num,
            state_file=state_file,
            channels=channels,
            dry_run=dry_run,
            current_user=current_user,
        )

    return (matched_repo, pr_num)


def process_discord_webhook_embed(
    embed: dict[str, Any],
    state_file: Path = DEFAULT_STATE_FILE,
    channels: list[str | int] | None = None,
    dry_run: bool = False,
    current_user: str | None = None,
) -> dict[str, Any] | None:
    """Parse Discord webhook embed and trigger authenticated gh pr view + auto-merge evaluation."""
    parsed = parse_discord_github_webhook_embed(embed)
    if not parsed:
        return None
    repo, pr_num = parsed
    return handle_event_pr(
        repo=repo,
        pr_number=pr_num,
        state_file=state_file,
        channels=channels,
        dry_run=dry_run,
        current_user=current_user,
    )


def process_github_email_doorbell(
    raw_headers: dict[str, Any],
    state_file: Path = DEFAULT_STATE_FILE,
    channels: list[str | int] | None = None,
    dry_run: bool = False,
    current_user: str | None = None,
) -> dict[str, Any] | None:
    """Parse verified DKIM email headers and trigger authenticated gh pr view + auto-merge evaluation."""
    parsed = parse_github_email_doorbell(raw_headers)
    if not parsed:
        return None
    repo, pr_num = parsed
    return handle_event_pr(
        repo=repo,
        pr_number=pr_num,
        state_file=state_file,
        channels=channels,
        dry_run=dry_run,
        current_user=current_user,
    )


def ensure_repo_discord_webhook(
    repo: str,
    discord_webhook_url: str,
) -> bool:
    """Idempotently register /github webhook on a GitHub repo using gh api repos/{repo}/hooks.

    Verifies if a webhook pointing to the target Discord webhook URL already exists.
    If not, creates an active webhook subscribed to pull_request, pull_request_review,
    pull_request_review_comment, and issue_comment events.
    """
    clean_url = (discord_webhook_url or "").strip()
    if not clean_url:
        log.warning("No discord_webhook_url provided for %s", repo)
        return False

    if not clean_url.endswith("/github"):
        clean_url = clean_url.rstrip("/") + "/github"

    # 1. Query existing hooks
    res = run_gh(["api", f"repos/{repo}/hooks"])
    if res.returncode != 0:
        log.warning("Failed listing hooks for %s: %s", repo, res.stderr.strip())
        return False

    try:
        hooks = json.loads(res.stdout or "[]")
        if not isinstance(hooks, list):
            hooks = []
    except Exception as exc:
        log.warning("Failed parsing hooks response for %s: %s", repo, exc)
        return False

    # Check for existing hook matching target URL (or base URL)
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        config = hook.get("config") or {}
        existing_url = str(config.get("url") or "").rstrip("/")
        if existing_url in (clean_url.rstrip("/"), discord_webhook_url.strip().rstrip("/")):
            log.info("Webhook already exists for %s: %s", repo, existing_url)
            return True

    # 2. Register hook via POST
    payload = {
        "name": "web",
        "active": True,
        "events": [
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "issue_comment",
        ],
        "config": {
            "url": clean_url,
            "content_type": "json",
            "insecure_ssl": "0",
        },
    }

    create_res = run_gh(
        ["api", f"repos/{repo}/hooks", "-X", "POST", "--input", "-"],
        input=json.dumps(payload),
    )
    if create_res.returncode == 0:
        log.info("Registered Discord GitHub webhook for %s", repo)
        return True

    log.warning("Failed registering webhook for %s: %s", repo, create_res.stderr.strip())
    return False


def ensure_repo_discord_webhooks(
    discord_webhook_url: str | None = None,
    repos: list[str] | None = None,
) -> dict[str, bool]:
    """Idempotently register Discord GitHub webhooks across multiple repositories.

    Defaults to owned brockventures/* repositories in WHITELISTED_REPOS.
    """
    if discord_webhook_url is None:
        discord_webhook_url = os.environ.get("DISCORD_GITHUB_WEBHOOK_URL", "")

    if repos is None:
        repos = [r for r in sorted(WHITELISTED_REPOS) if r.startswith("brockventures/")]

    results: dict[str, bool] = {}
    for repo in repos:
        results[repo] = ensure_repo_discord_webhook(repo, discord_webhook_url)

    return results


def main() -> int:
    """CLI entry point for gh_pr_watcher."""
    parser = argparse.ArgumentParser(
        description="GitHub PR & Review Discord Watcher Sidecar"
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="Path to state JSON file",
    )
    parser.add_argument(
        "--repos",
        type=str,
        default=",".join(DEFAULT_TARGET_REPOS),
        help="Comma-separated target repo patterns",
    )
    parser.add_argument(
        "--channels",
        type=str,
        default="",
        help="Comma-separated channel names or numeric IDs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and print without queuing outbox messages or saving state",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Record current PR reviews/comments to state without sending notifications",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="Interval in seconds for continuous polling daemon mode (0 = run once)",
    )
    parser.add_argument(
        "--register-webhooks",
        action="store_true",
        help="Register Discord /github webhooks on owned brockventures repos",
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default="",
        help="Discord webhook URL to register",
    )
    parser.add_argument(
        "--process-embed",
        type=str,
        default="",
        help="JSON string or file path containing Discord webhook embed to process",
    )
    parser.add_argument(
        "--process-email-headers",
        type=str,
        default="",
        help="JSON string or file path containing raw email headers to process",
    )
    args = parser.parse_args()

    target_patterns = [p.strip() for p in args.repos.split(",") if p.strip()]
    parsed_channels: list[str | int] = []
    if args.channels:
        for c in args.channels.split(","):
            c_clean = c.strip()
            if not c_clean:
                continue
            parsed_channels.append(int(c_clean) if c_clean.isdigit() else c_clean)

    channels = parsed_channels or DEFAULT_CHANNELS

    if args.register_webhooks:
        res_hooks = ensure_repo_discord_webhooks(discord_webhook_url=args.webhook_url or None)
        print(f"Webhook registration completed: {res_hooks}")
        return 0 if all(res_hooks.values()) else 1

    if args.process_embed:
        raw_embed = args.process_embed
        embed_p = Path(raw_embed)
        if embed_p.is_file():
            raw_embed = embed_p.read_text(encoding="utf-8")
        embed_data = json.loads(raw_embed)
        res_embed = process_discord_webhook_embed(
            embed=embed_data,
            state_file=args.state_file,
            channels=channels,
            dry_run=args.dry_run,
        )
        print(f"Processed embed: {res_embed}")
        return 0

    if args.process_email_headers:
        raw_headers = args.process_email_headers
        headers_p = Path(raw_headers)
        if headers_p.is_file():
            raw_headers = headers_p.read_text(encoding="utf-8")
        headers_data = json.loads(raw_headers)
        res_email = process_github_email_doorbell(
            raw_headers=headers_data,
            state_file=args.state_file,
            channels=channels,
            dry_run=args.dry_run,
        )
        print(f"Processed email headers: {res_email}")
        return 0

    if not args.dry_run:
        ensure_schedule_registered()

    if args.interval <= 0:
        stats = poll_and_notify(
            state_file=args.state_file,
            target_patterns=target_patterns,
            channels=channels,
            dry_run=args.dry_run,
            seed_only=args.seed,
        )
        print(f"PR Watcher completed: {stats}")
        return 0

    log.info("Starting PR watcher daemon loop (interval=%ss)...", args.interval)
    try:
        while True:
            try:
                stats = poll_and_notify(
                    state_file=args.state_file,
                    target_patterns=target_patterns,
                    channels=channels,
                    dry_run=args.dry_run,
                    seed_only=args.seed,
                )
                log.info("Poll cycle completed: %s", stats)
            except Exception as exc:
                log.error("Error during poll cycle: %s", exc)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("PR watcher daemon stopped by KeyboardInterrupt.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
