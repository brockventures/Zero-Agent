#!/usr/bin/env python3
"""gh_pr_watcher.py - Automated GitHub PR & Review Discord Watcher Sidecar.

Monitors open PRs authored by Zero or requesting Zero's review across active repositories
(mcarmody/lomin, brockventures/*, arcane103/*).
Tracks seen reviews, comments, and reviewDecision states in /workspace/data/gh_pr_watcher_state.json.
Dispatches actionable Discord wake-ups to #forge via tools/outbox.py
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

import requests

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
DEFAULT_CHANNELS: list[str | int] = [FORGE_CHANNEL_ID]

DEFAULT_STATE_FILE = Path("/workspace/data/gh_pr_watcher_state.json")
DEFAULT_SCHEDULE_FILE = Path("/workspace/data/schedule.json")
DEFAULT_TARGET_REPOS = [
    "mcarmody/lomin",
    "brockventures/*",
    "arcane103/*",
    "azylman/mirrormere",
]

# Whitelisted repositories for event-driven webhooks, email doorbells, and auto-merge
WHITELISTED_REPOS: set[str] = {
    "mcarmody/lomin",
    "arcane103/mirrormere",
    "azylman/mirrormere",
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
    if not schedule_file.exists():
        return False
    try:
        raw = schedule_file.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            if "jobs" in parsed:
                if not isinstance(parsed["jobs"], list):
                    return False
                job_list = parsed["jobs"]
            else:
                job_list = []
                parsed["jobs"] = job_list
        elif isinstance(parsed, list):
            job_list = parsed
        else:
            return False

        for item in job_list:
            if isinstance(item, dict) and item.get("id") == "gh_pr_watcher":
                return False

        job_list.append(
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
        tmp_file.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
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
        "seen_ci_failures": [],
        "seen_merge_conflicts": [],
        "review_decisions": {},
        "auto_merged_prs": [],
        "active_notifications": {},
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
        "seen_ci_failures",
        "seen_merge_conflicts",
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


def get_discord_bot_token(token: str | None = None) -> str | None:
    """Resolve Discord bot token from parameter, env, or /secrets/env.json."""
    if token and str(token).strip():
        return str(token).strip()
    env_token = os.environ.get("DISCORD_BOT_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()
    secrets_file = Path("/secrets/env.json")
    if secrets_file.exists():
        try:
            data = json.loads(secrets_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                tok = data.get("DISCORD_BOT_TOKEN")
                if tok and str(tok).strip():
                    return str(tok).strip()
        except Exception as exc:
            log.warning("Failed loading DISCORD_BOT_TOKEN from %s: %s", secrets_file, exc)
    return None


def cleanup_pr_notifications(
    repo: str | None = None,
    pr_number: int | str | None = None,
    state_file: Path = DEFAULT_STATE_FILE,
    token: str | None = None,
    sweep_channels: bool = True,
    state: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> int:
    """Delete tracked Discord notification cards for a PR and/or sweep Discord channels.

    Returns the number of notification messages deleted.
    """
    bot_token = get_discord_bot_token(token)

    if state is None:
        state = load_state(state_file)

    active_notifs: dict[str, list[dict[str, Any]]] = state.setdefault(
        "active_notifications", {}
    )

    num: int | None = None
    if pr_number is not None:
        try:
            num = int(pr_number)
        except (ValueError, TypeError):
            num = None

    clean_repo = repo.strip().lower() if repo else None

    # 1. Identify matching keys in active_notifications
    matching_keys: list[str] = []
    for k in list(active_notifs.keys()):
        k_repo, _, k_num_str = k.partition("#")
        k_repo_clean = k_repo.strip().lower()
        try:
            k_num = int(k_num_str)
        except ValueError:
            k_num = None

        repo_matches = (clean_repo is None) or (k_repo_clean == clean_repo)
        num_matches = (num is None) or (k_num == num)
        if repo_matches and num_matches:
            matching_keys.append(k)

    deleted_count = 0
    deleted_message_ids: set[str] = set()

    # 2. Delete tracked message IDs via REST
    for k in matching_keys:
        records = active_notifs.get(k, [])
        for item in records:
            cid = item.get("channel_id")
            mid = item.get("message_id")
            if mid and cid and bot_token:
                mid_str = str(mid)
                if mid_str not in deleted_message_ids:
                    if not dry_run:
                        url = f"https://discord.com/api/v10/channels/{cid}/messages/{mid_str}"
                        try:
                            res = requests.delete(
                                url,
                                headers={"Authorization": f"Bot {bot_token}"},
                                timeout=10,
                            )
                            if res.status_code in (200, 204, 404):
                                deleted_count += 1
                                deleted_message_ids.add(mid_str)
                        except Exception as exc:
                            log.warning(
                                "Failed deleting notification message %s in channel %s: %s",
                                mid,
                                cid,
                                exc,
                            )
                    else:
                        deleted_count += 1
                        deleted_message_ids.add(mid_str)
        active_notifs.pop(k, None)

    # 3. Sweep Discord channels for PR cards
    if sweep_channels and bot_token:
        for cid in (FORGE_CHANNEL_ID, ZERO_OPS_CHANNEL_ID):
            get_url = f"https://discord.com/api/v10/channels/{cid}/messages?limit=30"
            try:
                res = requests.get(
                    get_url,
                    headers={"Authorization": f"Bot {bot_token}"},
                    timeout=10,
                )
                if res.status_code == 200:
                    messages = res.json()
                    if isinstance(messages, list):
                        for m in messages:
                            mid = m.get("id")
                            if not mid or str(mid) in deleted_message_ids:
                                continue
                            content = str(m.get("content") or "")
                            is_pr_card = any(
                                marker in content
                                for marker in (
                                    "**GitHub PR Comment**",
                                    "**GitHub PR Review:",
                                    "**GitHub Review Requested**",
                                )
                            )
                            if not is_pr_card:
                                continue
                            if clean_repo and clean_repo not in content.lower():
                                continue
                            if num is not None and f"[#{num} " not in content:
                                continue

                            mid_str = str(mid)
                            if not dry_run:
                                del_url = f"https://discord.com/api/v10/channels/{cid}/messages/{mid_str}"
                                del_res = requests.delete(
                                    del_url,
                                    headers={"Authorization": f"Bot {bot_token}"},
                                    timeout=10,
                                )
                                if del_res.status_code in (200, 204, 404):
                                    deleted_count += 1
                                    deleted_message_ids.add(mid_str)
                            else:
                                deleted_count += 1
                                deleted_message_ids.add(mid_str)
            except Exception as exc:
                log.warning(
                    "Failed sweeping channel %s for PR notification cards: %s",
                    cid,
                    exc,
                )

    if not dry_run:
        try:
            save_state(state, state_file)
        except Exception as exc:
            log.warning("Failed saving state after cleanup: %s", exc)

    return deleted_count


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
    is_default_patterns = (target_patterns is None) or (target_patterns == DEFAULT_TARGET_REPOS)
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
        enforce_pattern: bool = True,
    ) -> None:
        repo_clean = str(repo or "").strip()
        if not repo_clean or not number:
            return
        if enforce_pattern and not matches_target_patterns(repo_clean, target_patterns):
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

    enforce = not is_default_patterns

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
                    enforce_pattern=enforce,
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
                    enforce_pattern=enforce,
                )
    except Exception as exc:
        log.warning("Failed querying prs review-requested: %s", exc)

    # 3. Search PRs that involve @me
    try:
        res = run_gh([
            "search",
            "prs",
            "--state=open",
            "--involves=@me",
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
                    enforce_pattern=enforce,
                )
    except Exception as exc:
        log.warning("Failed querying prs involves=@me: %s", exc)

    # 4. Target repo patterns
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
                            enforce_pattern=True,
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
                            enforce_pattern=True,
                        )
            except Exception as exc:
                log.warning("Failed querying pr list for repo %s: %s", pat, exc)

    # 5. Ingest unread/recent PullRequest notifications
    try:
        res = run_gh(["api", "/notifications"])
        if res.returncode == 0:
            for notif in json.loads(res.stdout or "[]"):
                subj = notif.get("subject") or {}
                if subj.get("type") == "PullRequest":
                    notif_reason = notif.get("reason")
                    api_url = subj.get("url") or ""
                    parts = api_url.split("/repos/")
                    if len(parts) == 2:
                        subparts = parts[1].split("/pulls/")
                        if len(subparts) == 2 and subparts[1].isdigit():
                            r_name = subparts[0]
                            num = int(subparts[1])
                            if notif_reason is not None:
                                valid_reason = notif_reason in (
                                    "author",
                                    "review_requested",
                                    "mention",
                                    "assign",
                                    "comment",
                                    "team_mention",
                                    "state_change",
                                )
                                if not (valid_reason or r_name.startswith("brockventures/")):
                                    continue
                            _add_pr(
                                repo=r_name,
                                number=num,
                                title=subj.get("title", ""),
                                url=f"https://github.com/{r_name}/pull/{num}",
                                author="",
                                enforce_pattern=True,
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
            "number,title,url,author,reviewDecision,reviews,latestReviews,reviewRequests,comments,statusCheckRollup,headRefOid,state,merged,mergeable,mergeStateStatus",
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


def format_ci_failure_message(
    repo: str,
    pr_number: int,
    title: str,
    url: str,
    check_name: str,
    details_url: str = "",
) -> str:
    """Build structured Discord notification card for a CI check failure."""
    lines = [
        "❌ **GitHub CI Check Failure**",
        f"**Repo:** `{repo}` | **PR:** [#{pr_number} {title}]({url})",
        f"**Failed Check:** `{check_name}`",
    ]
    if details_url:
        lines.append(f"**Details:** [View Log / Run]({details_url})")
    lines.append("")
    lines.append("> ⚠️ CI workflow failed on head commit. Action required before merge.")
    return "\n".join(lines)


def format_merge_conflict_message(
    repo: str,
    pr_number: int,
    title: str,
    url: str,
) -> str:
    """Build structured Discord notification card for a PR merge conflict."""
    return "\n".join([
        "⚠️ **GitHub PR Merge Conflict**",
        f"**Repo:** `{repo}` | **PR:** [#{pr_number} {title}]({url})",
        "",
        "> 💥 This branch has merge conflicts that must be resolved before merging.",
    ])


def extract_failed_checks(status_rollup: Any) -> list[dict[str, Any]]:
    """Extract failed CI checks from statusCheckRollup."""
    if not status_rollup:
        return []

    failed: list[dict[str, Any]] = []
    checks: list[Any] = []

    if isinstance(status_rollup, list):
        checks = status_rollup
    elif isinstance(status_rollup, dict):
        contexts = status_rollup.get("contexts") or status_rollup.get("checks")
        if isinstance(contexts, list) and contexts:
            checks = contexts
        else:
            state = str(status_rollup.get("state") or status_rollup.get("status") or "").upper()
            conclusion = str(status_rollup.get("conclusion") or "").upper()
            if state in ("FAILURE", "ERROR") or conclusion in (
                "FAILURE",
                "TIMED_OUT",
                "ACTION_REQUIRED",
                "CANCELLED",
                "STARTUP_FAILURE",
            ):
                name = str(
                    status_rollup.get("name")
                    or status_rollup.get("context")
                    or status_rollup.get("workflowName")
                    or "status_check"
                )
                failed.append({
                    "name": name,
                    "url": status_rollup.get("targetUrl")
                    or status_rollup.get("detailsUrl")
                    or status_rollup.get("url")
                    or "",
                    "conclusion": conclusion or state,
                })

    for check in checks:
        if not isinstance(check, dict):
            continue
        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or "").upper()
        if conclusion in (
            "FAILURE",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "CANCELLED",
            "STARTUP_FAILURE",
        ) or state in ("FAILURE", "ERROR"):
            name = str(
                check.get("name")
                or check.get("context")
                or check.get("workflowName")
                or "unnamed_check"
            )
            failed.append({
                "name": name,
                "url": check.get("targetUrl")
                or check.get("detailsUrl")
                or check.get("url")
                or "",
                "conclusion": conclusion or state,
            })

    return failed


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
    state_file: Path = DEFAULT_STATE_FILE,
) -> dict[str, Any]:
    """Check a single PR for new reviews, comments, or review requests and notify."""
    if channels is None:
        channels = DEFAULT_CHANNELS

    repo = pr_details.get("repo", "")
    pr_number = pr_details.get("number", 0)
    title = pr_details.get("title", f"PR #{pr_number}")
    url = pr_details.get("url", f"https://github.com/{repo}/pull/{pr_number}")
    review_decision = pr_details.get("reviewDecision") or "NONE"
    pr_key = f"{repo}#{pr_number}"

    seen_reviews: set[str] = set(state.setdefault("seen_review_ids", []))
    seen_comments: set[str] = set(state.setdefault("seen_comment_ids", []))
    seen_reqs: set[str] = set(state.setdefault("seen_review_requests", []))
    seen_ci_failures: set[str] = set(state.setdefault("seen_ci_failures", []))
    seen_merge_conflicts: set[str] = set(state.setdefault("seen_merge_conflicts", []))
    review_decisions: dict[str, str] = state.setdefault("review_decisions", {})
    active_notifications: dict[str, list[dict[str, Any]]] = state.setdefault(
        "active_notifications", {}
    )
    pr_head_ref_oids: dict[str, str] = state.setdefault("pr_head_ref_oids", {})

    stats = {
        "reviews_notified": 0,
        "comments_notified": 0,
        "review_requests_notified": 0,
        "ci_failures_notified": 0,
        "conflicts_notified": 0,
        "decision_changed": False,
        "auto_merged": False,
        "notifications_cleaned": 0,
    }

    # Detect if Zero responded, headRefOid changed, or PR is MERGED/CLOSED
    zero_users = {current_user.lower(), "brockventures", "zero-brock"}
    latest_external_ts: str | None = None
    latest_zero_ts: str | None = None

    for rev in pr_details.get("reviews", []):
        author_login = (rev.get("author") or {}).get("login", "")
        ts = str(rev.get("submittedAt") or rev.get("createdAt") or "")
        if not ts:
            continue
        if author_login and author_login.lower() in zero_users:
            if latest_zero_ts is None or ts > latest_zero_ts:
                latest_zero_ts = ts
        elif author_login:
            if latest_external_ts is None or ts > latest_external_ts:
                latest_external_ts = ts

    for com in pr_details.get("comments", []):
        author_login = (com.get("author") or {}).get("login", "")
        ts = str(com.get("createdAt") or "")
        if not ts:
            continue
        if author_login and author_login.lower() in zero_users:
            if latest_zero_ts is None or ts > latest_zero_ts:
                latest_zero_ts = ts
        elif author_login:
            if latest_external_ts is None or ts > latest_external_ts:
                latest_external_ts = ts

    zero_responded = bool(
        latest_external_ts and latest_zero_ts and latest_zero_ts >= latest_external_ts
    )

    current_head = str(pr_details.get("headRefOid") or "")
    head_changed = False
    tracked_notifs = active_notifications.get(pr_key, [])
    if current_head and tracked_notifs:
        for notif in tracked_notifs:
            notif_head = notif.get("head_ref_oid")
            if notif_head and notif_head != current_head:
                head_changed = True
                break
    if current_head and pr_key in pr_head_ref_oids:
        if pr_head_ref_oids[pr_key] and pr_head_ref_oids[pr_key] != current_head:
            head_changed = True

    pr_state = str(pr_details.get("state") or "").upper()
    is_closed_or_merged = pr_state in ("MERGED", "CLOSED") or pr_details.get("merged") is True or pr_details.get("closed") is True

    if zero_responded or head_changed or is_closed_or_merged:
        if not seed_only:
            cleaned = cleanup_pr_notifications(
                repo=repo,
                pr_number=pr_number,
                state_file=state_file,
                state=state,
                dry_run=dry_run,
            )
            stats["notifications_cleaned"] = cleaned

    if current_head:
        pr_head_ref_oids[pr_key] = current_head

    if is_closed_or_merged:
        state["seen_review_ids"] = list(seen_reviews)
        state["seen_comment_ids"] = list(seen_comments)
        state["seen_review_requests"] = list(seen_reqs)
        state["seen_ci_failures"] = list(seen_ci_failures)
        state["seen_merge_conflicts"] = list(seen_merge_conflicts)
        state["review_decisions"] = review_decisions
        state["active_notifications"] = active_notifications
        state["pr_head_ref_oids"] = pr_head_ref_oids
        return stats

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
                dispatch_res = dispatch_notification(msg, channels=channels, dry_run=dry_run)
                stats["reviews_notified"] += 1
                if not dry_run:
                    notif_list = active_notifications.setdefault(pr_key, [])
                    for d_res in dispatch_res:
                        notif_list.append({
                            "channel_id": d_res.get("channel_id") or d_res.get("channel") or 0,
                            "message_id": d_res.get("message_id") or 0,
                            "event_id": rev_id,
                            "created_at": submitted_at or datetime.now(timezone.utc).isoformat(),
                            "head_ref_oid": current_head,
                        })

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
            dispatch_res = dispatch_notification(msg, channels=channels, dry_run=dry_run)
            stats["review_requests_notified"] += 1
            if not dry_run:
                notif_list = active_notifications.setdefault(pr_key, [])
                for d_res in dispatch_res:
                    notif_list.append({
                        "channel_id": d_res.get("channel_id") or d_res.get("channel") or 0,
                        "message_id": d_res.get("message_id") or 0,
                        "event_id": req_key,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "head_ref_oid": current_head,
                    })

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
            dispatch_res = dispatch_notification(msg, channels=channels, dry_run=dry_run)
            stats["comments_notified"] += 1
            if not dry_run:
                notif_list = active_notifications.setdefault(pr_key, [])
                for d_res in dispatch_res:
                    notif_list.append({
                        "channel_id": d_res.get("channel_id") or d_res.get("channel") or 0,
                        "message_id": d_res.get("message_id") or 0,
                        "event_id": com_id,
                        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
                        "head_ref_oid": current_head,
                    })

    # 4. Check reviewDecision transition
    old_decision = review_decisions.get(pr_key)
    review_decisions[pr_key] = review_decision
    if old_decision and old_decision != review_decision and review_decision != "NONE":
        stats["decision_changed"] = True

    # 5. Check CI check failures and merge conflicts on authored/contributed PRs
    author_obj = pr_details.get("author")
    if isinstance(author_obj, dict):
        pr_author = str(author_obj.get("login") or "").strip().lower()
    else:
        pr_author = str(author_obj or "").strip().lower()

    is_authored = bool(
        pr_author and (pr_author in zero_users or pr_author == (current_user or "").lower())
    )
    is_contributed = False

    if not is_authored:
        if (
            pr_details.get("viewerDidAuthor")
            or pr_details.get("contributed")
            or pr_details.get("is_contributed")
        ):
            is_contributed = True
        else:
            for rev in pr_details.get("reviews", []):
                rev_a = rev.get("author")
                rev_login = (
                    rev_a.get("login") if isinstance(rev_a, dict) else str(rev_a or "")
                ).strip().lower()
                if rev_login and (
                    rev_login in zero_users or rev_login == (current_user or "").lower()
                ):
                    is_contributed = True
                    break
            if not is_contributed:
                for com in pr_details.get("comments", []):
                    com_a = com.get("author")
                    com_login = (
                        com_a.get("login") if isinstance(com_a, dict) else str(com_a or "")
                    ).strip().lower()
                    if com_login and (
                        com_login in zero_users or com_login == (current_user or "").lower()
                    ):
                        is_contributed = True
                        break
            if not is_contributed:
                for assignee in pr_details.get("assignees", []):
                    a_login = (
                        assignee.get("login")
                        if isinstance(assignee, dict)
                        else str(assignee or "")
                    ).strip().lower()
                    if a_login and (
                        a_login in zero_users or a_login == (current_user or "").lower()
                    ):
                        is_contributed = True
                        break

    if is_authored or is_contributed:
        # A) Check CI check failures
        failed_checks = extract_failed_checks(pr_details.get("statusCheckRollup"))
        for chk in failed_checks:
            check_name = chk.get("name") or "unnamed_check"
            fail_key = f"ci_fail:{repo}#{pr_number}:{current_head}:{check_name}"
            if fail_key not in seen_ci_failures:
                seen_ci_failures.add(fail_key)
                if not seed_only:
                    msg = format_ci_failure_message(
                        repo=repo,
                        pr_number=pr_number,
                        title=title,
                        url=url,
                        check_name=check_name,
                        details_url=chk.get("url") or "",
                    )
                    dispatch_res = dispatch_notification(msg, channels=channels, dry_run=dry_run)
                    stats["ci_failures_notified"] += 1
                    if not dry_run:
                        notif_list = active_notifications.setdefault(pr_key, [])
                        for d_res in dispatch_res:
                            notif_list.append({
                                "channel_id": d_res.get("channel_id") or d_res.get("channel") or 0,
                                "message_id": d_res.get("message_id") or 0,
                                "event_id": fail_key,
                                "created_at": datetime.now(timezone.utc).isoformat(),
                                "head_ref_oid": current_head,
                            })

        # B) Check merge conflicts
        mergeable = str(pr_details.get("mergeable") or "").upper()
        merge_state = str(pr_details.get("mergeStateStatus") or "").upper()
        if mergeable == "CONFLICTING" or merge_state == "DIRTY":
            conflict_key = f"conflict:{repo}#{pr_number}:{current_head}"
            if conflict_key not in seen_merge_conflicts:
                seen_merge_conflicts.add(conflict_key)
                if not seed_only:
                    msg = format_merge_conflict_message(
                        repo=repo,
                        pr_number=pr_number,
                        title=title,
                        url=url,
                    )
                    dispatch_res = dispatch_notification(msg, channels=channels, dry_run=dry_run)
                    stats["conflicts_notified"] += 1
                    if not dry_run:
                        notif_list = active_notifications.setdefault(pr_key, [])
                        for d_res in dispatch_res:
                            notif_list.append({
                                "channel_id": d_res.get("channel_id") or d_res.get("channel") or 0,
                                "message_id": d_res.get("message_id") or 0,
                                "event_id": conflict_key,
                                "created_at": datetime.now(timezone.utc).isoformat(),
                                "head_ref_oid": current_head,
                            })

    # 6. Check and attempt auto-merge for whitelisted authors
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
    state["seen_ci_failures"] = list(seen_ci_failures)
    state["seen_merge_conflicts"] = list(seen_merge_conflicts)
    state["review_decisions"] = review_decisions
    state["active_notifications"] = active_notifications
    state["pr_head_ref_oids"] = pr_head_ref_oids
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
        "ci_failures_notified": 0,
        "conflicts_notified": 0,
        "decisions_changed": 0,
        "auto_merged": 0,
        "notifications_cleaned": 0,
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
            state_file=state_file,
        )
        total_stats["prs_checked"] += 1
        total_stats["reviews_notified"] += st["reviews_notified"]
        total_stats["comments_notified"] += st["comments_notified"]
        total_stats["review_requests_notified"] += st["review_requests_notified"]
        total_stats["ci_failures_notified"] += st.get("ci_failures_notified", 0)
        total_stats["conflicts_notified"] += st.get("conflicts_notified", 0)
        total_stats["notifications_cleaned"] += st.get("notifications_cleaned", 0)
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
        state_file=state_file,
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
        if repo_cand.lower().startswith("brockventures/") or repo_cand.lower() == "azylman/mirrormere":
            matched_repo = repo_cand

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
        if repo_cand.lower().startswith("brockventures/") or repo_cand.lower() == "azylman/mirrormere":
            matched_repo = repo_cand

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
        help="Comma-separated channel names or numeric IDs (default: #forge)",
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
    parser.add_argument(
        "--cleanup-repo",
        type=str,
        default="",
        help="Repository to clean up notifications for",
    )
    parser.add_argument(
        "--cleanup-pr",
        type=str,
        default="",
        help="PR number to clean up notifications for",
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

    if args.cleanup_repo or args.cleanup_pr:
        cleaned = cleanup_pr_notifications(
            repo=args.cleanup_repo or None,
            pr_number=args.cleanup_pr or None,
            state_file=args.state_file,
            dry_run=args.dry_run,
        )
        print(f"Cleaned {cleaned} notifications")
        return 0

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
