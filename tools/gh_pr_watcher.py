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
import json
import logging
import os
from pathlib import Path
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
    if not schedule_file.exists():
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


def run_gh(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Execute gh command safely with standard timeouts and capture output."""
    cmd = ["gh"] + args
    return subprocess.run(
        cmd,
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
