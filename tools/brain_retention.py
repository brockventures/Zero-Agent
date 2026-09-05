#!/usr/bin/env python3
"""
Brain Retention & Pruning Utility for Zero / Antigravity.

Enforces rolling retention (default 30 days) on historical conversation directories
under ~/.gemini/antigravity-cli/brain/.
Strictly preserves all active channel sessions and active threads tracked in data/sessions.json.
Synchronizes transcript SQLite FTS5 index upon pruning.
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

WORKSPACE = Path("/workspace")
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from tools.bridge_state import DATA_DIR, SESSIONS_FILE, SESSION_METADATA_FILE
from tools.transcript_index import TranscriptIndexer

BRAIN_DIR = Path(os.environ.get("GEMINI_BRAIN_DIR", "/root/.gemini/antigravity-cli/brain"))


def get_protected_conversation_ids() -> set[str]:
    """Collect all currently active conversation IDs across all channels and threads."""
    protected = set()

    # 1. Active mappings in sessions.json
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE) as f:
                d = json.load(f)
                for val in d.values():
                    if val and isinstance(val, str):
                        protected.add(val.strip())
        except Exception as e:
            print(f"[BrainRetention] Warning reading sessions.json: {e}")

    # 2. Metadata file active conv_ids
    if SESSION_METADATA_FILE.exists():
        try:
            with open(SESSION_METADATA_FILE) as f:
                md = json.load(f)
                for entry in md.values():
                    if isinstance(entry, dict) and entry.get("conv_id"):
                        protected.add(entry["conv_id"].strip())
        except Exception as e:
            print(f"[BrainRetention] Warning reading session_metadata.json: {e}")

    return protected


def get_dir_mtime(conv_dir: Path) -> float:
    """Determine the most recent modification time for a conversation directory."""
    mtimes = [conv_dir.stat().st_mtime]
    t_file = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
    if t_file.exists():
        try:
            mtimes.append(t_file.stat().st_mtime)
        except Exception:
            pass
    tf_file = conv_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
    if tf_file.exists():
        try:
            mtimes.append(tf_file.stat().st_mtime)
        except Exception:
            pass
    return max(mtimes)


def get_dir_size_bytes(conv_dir: Path) -> int:
    """Calculate total size of directory in bytes."""
    total = 0
    try:
        for entry in conv_dir.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except Exception:
                    pass
    except Exception:
        pass
    return total


def prune_brain_sessions(
    max_age_days: int = 30,
    brain_dir: Path = BRAIN_DIR,
    dry_run: bool = False
) -> dict:
    """Prune conversation directories older than max_age_days, protecting active sessions."""
    stats = {
        "max_age_days": max_age_days,
        "scanned_count": 0,
        "protected_count": 0,
        "pruned_count": 0,
        "freed_bytes": 0,
        "freed_mb": 0.0,
        "pruned_conv_ids": [],
        "dry_run": dry_run,
        "index_pruned": {}
    }

    if not brain_dir.exists():
        return stats

    protected_ids = get_protected_conversation_ids()
    now = time.time()
    cutoff_seconds = max_age_days * 86400

    try:
        conv_dirs = [d for d in brain_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except Exception as e:
        print(f"[BrainRetention] Error listing brain directories: {e}")
        return stats

    stats["scanned_count"] = len(conv_dirs)

    for cdir in conv_dirs:
        conv_id = cdir.name
        if conv_id in protected_ids:
            stats["protected_count"] += 1
            continue

        try:
            mtime = get_dir_mtime(cdir)
        except Exception:
            continue

        age_seconds = now - mtime
        if age_seconds > cutoff_seconds:
            size_bytes = get_dir_size_bytes(cdir)
            stats["pruned_conv_ids"].append(conv_id)
            stats["freed_bytes"] += size_bytes

            if not dry_run:
                try:
                    shutil.rmtree(cdir, ignore_errors=True)
                except Exception as e:
                    print(f"[BrainRetention] Error removing {cdir}: {e}")

    stats["pruned_count"] = len(stats["pruned_conv_ids"])
    stats["freed_mb"] = round(stats["freed_bytes"] / (1024 * 1024), 2)

    # Sync SQLite search index if any folders were physically deleted
    if not dry_run and stats["pruned_count"] > 0:
        try:
            indexer = TranscriptIndexer()
            stats["index_pruned"] = indexer.prune_missing()
        except Exception as ie:
            print(f"[BrainRetention] Warning updating transcript index: {ie}")

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prune historical Antigravity brain sessions.")
    parser.add_argument("--max-age-days", type=int, default=30, help="Maximum age in days before pruning (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="Report pruning targets without deleting files")
    parser.add_argument("--quiet", action="store_true", help="Output minimal machine-readable JSON")
    args = parser.parse_args()

    result = prune_brain_sessions(max_age_days=args.max_age_days, dry_run=args.dry_run)
    if args.quiet:
        print(json.dumps(result))
    else:
        prefix = "[DRY RUN] " if result["dry_run"] else ""
        print(f"{prefix}Brain Retention Sweep (Retention: {result['max_age_days']} days):")
        print(f"• Scanned: {result['scanned_count']} sessions")
        print(f"• Protected (Active): {result['protected_count']} sessions")
        print(f"• Pruned: {result['pruned_count']} sessions ({result['freed_mb']} MB freed)")
        if result["pruned_conv_ids"]:
            sample = result["pruned_conv_ids"][:5]
            print(f"• Sample Pruned IDs: {', '.join(sample)}{'...' if len(result['pruned_conv_ids']) > 5 else ''}")
