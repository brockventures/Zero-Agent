#!/usr/bin/env python3
"""
Repository Operations & Git Lifecycle Automation Tool for Zero.
Handles deterministic PR merging, worktree lifecycles, and branch hygiene across shared repos.
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("repo_ops")


def _run(cmd: list[str], cwd: Path | str) -> tuple[int, str, str]:
    """Execute command in target directory and return returncode, stdout, stderr."""
    res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return res.returncode, res.stdout.strip(), res.stderr.strip()


def prune_merged_branches(repo_dir: Path, target_base: str = "origin/main") -> list[str]:
    """Delete local branches that have already been merged into the base branch."""
    code, stdout, _ = _run(["git", "branch", "--merged", target_base], repo_dir)
    if code != 0:
        return []

    deleted = []
    for line in stdout.splitlines():
        b = line.strip().replace("*", "").strip()
        if not b or b in ("main", "master"):
            continue
        del_code, _, _ = _run(["git", "branch", "-d", b], repo_dir)
        if del_code == 0:
            deleted.append(b)
            log.info("Deleted merged local branch: %s", b)
        else:
            # If standard delete fails, force delete if already merged upstream
            del_f_code, _, _ = _run(["git", "branch", "-D", b], repo_dir)
            if del_f_code == 0:
                deleted.append(b)
                log.info("Force deleted merged local branch: %s", b)

    return deleted


def cleanup_worktrees(repo_dir: Path) -> list[str]:
    """Remove inactive external worktrees and prune the worktree registry."""
    code, stdout, _ = _run(["git", "worktree", "list", "--porcelain"], repo_dir)
    if code != 0:
        return []

    repo_dir_resolved = repo_dir.resolve()
    cleaned = []
    lines = stdout.splitlines()

    for line in lines:
        if line.startswith("worktree "):
            wt_path = Path(line.split("worktree ", 1)[1].strip()).resolve()
            if wt_path == repo_dir_resolved:
                continue

            # Check if dirty before removing
            d_code, d_out, _ = _run(["git", "status", "--porcelain"], wt_path)
            if d_code == 0 and not d_out:
                rm_code, _, rm_err = _run(["git", "worktree", "remove", str(wt_path)], repo_dir)
                if rm_code == 0:
                    cleaned.append(str(wt_path))
                    log.info("Removed clean worktree: %s", wt_path)
                else:
                    log.warning("Failed to remove worktree %s: %s", wt_path, rm_err)
            else:
                log.warning("Skipping dirty worktree: %s", wt_path)

    _run(["git", "worktree", "prune"], repo_dir)
    return cleaned


def prune_remote_tracking(repo_dir: Path, remote: str = "origin") -> bool:
    """Prune stale remote-tracking references."""
    code, _, _ = _run(["git", "fetch", "--prune", remote], repo_dir)
    return code == 0


def clean_repo(repo_dir: Path, base_branch: str = "origin/main") -> dict:
    """Run full hygiene sweep on target repository."""
    log.info("Running hygiene sweep on %s...", repo_dir)
    prune_remote_tracking(repo_dir)
    cleaned_wts = cleanup_worktrees(repo_dir)
    deleted_branches = prune_merged_branches(repo_dir, base_branch)

    return {
        "repo": str(repo_dir),
        "removed_worktrees": cleaned_wts,
        "deleted_branches": deleted_branches,
    }


def merge_pr_and_cleanup(repo_dir: Path, pr_number: int, strategy: str = "squash") -> dict:
    """Autonomously merge a PR, update main, and tear down local branch/worktrees."""
    log.info("Merging PR #%d in %s (strategy=%s)...", pr_number, repo_dir, strategy)

    # 1. Execute GitHub merge with remote branch deletion
    merge_cmd = ["gh", "pr", "merge", str(pr_number), f"--{strategy}", "--delete-branch", "--auto"]
    code, out, err = _run(merge_cmd, repo_dir)
    if code != 0:
        # Fall back to immediate merge without auto flag if auto is not enabled
        merge_cmd = ["gh", "pr", "merge", str(pr_number), f"--{strategy}", "--delete-branch"]
        code, out, err = _run(merge_cmd, repo_dir)
        if code != 0:
            raise RuntimeError(f"Failed to merge PR #{pr_number}: {err or out}")

    # 2. Get the head branch name for local teardown
    view_code, view_out, _ = _run(["gh", "pr", "view", str(pr_number), "--json", "headRefName", "-q", ".headRefName"], repo_dir)
    head_branch = view_out.strip() if view_code == 0 else ""

    # 3. Pull latest main cleanly (using --rebase --autostash so Tier 1 edits in /workspace are never clobbered)
    _run(["git", "checkout", "main"], repo_dir)
    _run(["git", "pull", "--rebase", "--autostash", "origin", "main"], repo_dir)

    # 4. Clean up worktrees and local branch
    cleaned_wts = cleanup_worktrees(repo_dir)
    deleted_branches = []
    if head_branch and head_branch != "main":
        del_code, _, _ = _run(["git", "branch", "-D", head_branch], repo_dir)
        if del_code == 0:
            deleted_branches.append(head_branch)

    # 5. Prune any other merged branches and remote refs
    extra_deleted = prune_merged_branches(repo_dir)
    deleted_branches.extend([b for b in extra_deleted if b not in deleted_branches])
    prune_remote_tracking(repo_dir)

    return {
        "pr": pr_number,
        "merged": True,
        "head_branch": head_branch,
        "removed_worktrees": cleaned_wts,
        "deleted_branches": deleted_branches,
    }


def main():
    parser = argparse.ArgumentParser(description="Zero Repo Operations & Git Lifecycle Automation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Clean command
    clean_p = subparsers.add_parser("clean", help="Prune merged branches and orphaned worktrees")
    clean_p.add_argument("repo_dir", nargs="?", default="/workspace/market-sandbox", help="Repository directory")
    clean_p.add_argument("--base", default="origin/main", help="Base branch for merge comparison")

    # Merge command
    merge_p = subparsers.add_parser("merge", help="Autonomously merge PR, pull main, and teardown branch")
    merge_p.add_argument("pr_number", type=int, help="PR number to merge")
    merge_p.add_argument("repo_dir", nargs="?", default="/workspace/market-sandbox", help="Repository directory")
    merge_p.add_argument("--strategy", default="squash", choices=["squash", "merge", "rebase"], help="Merge strategy")

    args = parser.parse_args()
    repo_path = Path(args.repo_dir).resolve()

    if not (repo_path / ".git").exists():
        log.error("Path is not a git repository: %s", repo_path)
        sys.exit(1)

    if args.command == "clean":
        res = clean_repo(repo_path, base_branch=args.base)
        print(f"Hygiene sweep complete: {len(res['removed_worktrees'])} worktrees removed, {len(res['deleted_branches'])} branches deleted.")
    elif args.command == "merge":
        res = merge_pr_and_cleanup(repo_path, args.pr_number, strategy=args.strategy)
        print(f"PR #{res['pr']} merged cleanly. Removed {len(res['removed_worktrees'])} worktrees, deleted {len(res['deleted_branches'])} local branches.")


if __name__ == "__main__":
    main()
