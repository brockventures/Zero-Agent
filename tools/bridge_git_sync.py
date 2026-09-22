#!/usr/bin/env python3
"""
Zero Discord Bridge - Automated Pre-Reload Git Synchronization Module
Ensures all modified architectural code, tools, rules, and documentation in /workspace
are safely committed and pushed to origin/main whenever an in-place bridge reload
or container restart occurs.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path("/workspace/data")
PT_TZ = ZoneInfo("America/Los_Angeles")
LAST_SYNC_FILE = DATA_DIR / "last_reload_git_sync.json"

# Whitelisted directories for auto-staging untracked architectural assets
SAFE_UNTRACKED_PREFIXES = (
    "tools/",
    ".agents/",
    "specs/",
    "config/runtime_rules.json",
)


def get_current_git_sha(repo_dir: Path | str = "/workspace") -> str:
    """Retrieve current short HEAD commit SHA."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def sync_git_on_reload(
    repo_dir: Path | str = "/workspace",
    reason: str = "Manual in-place bridge reload",
    initiator: str = "user",
    timeout: float = 20.0,
) -> dict:
    """Safely stages, commits, and pushes modified architecture assets before reload.
    
    Returns a dict summarizing actions taken:
    {
        "synced": bool,
        "clean": bool,
        "sha": str,
        "files": list[str],
        "message": str,
        "error": str | None
    }
    """
    repo = Path(repo_dir)
    result = {
        "synced": False,
        "clean": False,
        "sha": get_current_git_sha(repo),
        "files": [],
        "message": "",
        "error": None,
    }

    if not (repo / ".git").exists():
        result["message"] = "Not a git repository"
        return result

    try:
        # 1. Inspect status
        st_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        status_lines = [l.strip() for l in st_proc.stdout.splitlines() if l.strip()]

        if not status_lines:
            result["clean"] = True
            result["message"] = f"Working tree clean at commit {result['sha']}."
            return result

        # 2. Stage modified tracked files
        subprocess.run(
            ["git", "add", "-u"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )

        # 3. Stage untracked files matching safe architecture prefixes
        for line in status_lines:
            if line.startswith("??"):
                rel_path = line[3:].strip().strip('"')
                if any(rel_path.startswith(prefix) or rel_path.endswith(".md") for prefix in SAFE_UNTRACKED_PREFIXES):
                    # Don't add scratch or private files
                    if "scratch/" not in rel_path and "private" not in rel_path and not rel_path.endswith((".log", ".tmp", ".bak")):
                        subprocess.run(
                            ["git", "add", rel_path],
                            cwd=str(repo),
                            capture_output=True,
                            timeout=5,
                        )

        # 4. Check if anything is actually staged
        diff_proc = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        staged_files = [f.strip() for f in diff_proc.stdout.splitlines() if f.strip()]

        if not staged_files:
            result["clean"] = True
            result["message"] = f"No architectural changes to stage. Working tree nominal at {result['sha']}."
            return result

        result["files"] = staged_files

        # 5. Formulate commit message
        now_pt = datetime.now(timezone.utc).astimezone(PT_TZ)
        title = f"feat(bridge): auto-sync architecture updates before reload"
        body = (
            f"Automated pre-reload architecture sync.\n\n"
            f"• Reason: {reason}\n"
            f"• Initiator: {initiator}\n"
            f"• Timestamp: {now_pt.strftime('%Y-%m-%d %I:%M:%S %p PT')}\n"
            f"• Files ({len(staged_files)}):\n" + "\n".join(f"  - {f}" for f in staged_files[:20])
        )
        if len(staged_files) > 20:
            body += f"\n  - ... and {len(staged_files) - 20} more"

        subprocess.run(
            ["git", "commit", "-m", title, "-m", body],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )

        new_sha = get_current_git_sha(repo)
        result["sha"] = new_sha

        # 6. Rebase & Push
        try:
            subprocess.run(
                ["git", "pull", "--rebase", "origin", "main"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception:
            pass

        push_proc = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )

        result["synced"] = True
        result["message"] = f"Successfully committed and pushed {len(staged_files)} file(s) to origin/main ({new_sha})."

        # Record to last sync file for startup briefing consumption
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "sha": new_sha,
                "synced_at": time.time(),
                "synced_at_pt": now_pt.strftime("%Y-%m-%d %I:%M:%S %p PT"),
                "reason": reason,
                "initiator": initiator,
                "files_count": len(staged_files),
                "files": staged_files[:15],
            }
            tmp_file = LAST_SYNC_FILE.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp_file.replace(LAST_SYNC_FILE)
        except Exception as se:
            print(f"[BridgeGitSync] Warning saving last sync file: {se}")

    except Exception as e:
        result["error"] = str(e)
        result["message"] = f"Git sync encountered an issue: {e}"
        print(f"[BridgeGitSync] ⚠️ {result['message']}")

    return result
