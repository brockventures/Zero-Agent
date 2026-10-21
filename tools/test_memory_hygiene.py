"""Unit tests for memory hygiene (#1 ephemeral noise gate, #3 explicit subject superseding, #5 decisions compaction)."""

import sys
from pathlib import Path

if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

from tools.topic_tracker import is_ephemeral_memory_noise, upsert_decision_resolution
from tools.memory_manager import compact_decisions_log


def test_is_ephemeral_memory_noise():
    # Agora burst trading orders & round headers are vetoed
    assert is_ephemeral_memory_noise("BUY 40 ORE @ 120 AT NEXUS and MOVE TO VESTA", "agora-round-12")
    assert is_ephemeral_memory_noise("🔔 STATION AGORA // COMBINE ROUND 45 quotes updated", "agora")
    # Floor yields and short status bumps are vetoed
    assert is_ephemeral_memory_noise("Yielding the floor to Amos to resolve the envelope check.", "general")
    assert is_ephemeral_memory_noise("Shipped in PR #929 with auto-merge armed and all checks passing.", "pr-status")
    # Substantive engineering decisions pass cleanly
    assert not is_ephemeral_memory_noise(
        "Consensus reached to enforce greedy MMR diversity re-ranking (lambda=0.7) in _fuse_memory_rrf to prevent top-K chunk cannibalization.",
        "mmr-memory-reranking",
    )


def test_upsert_decision_resolution_supersedes_same_subject(tmp_path):
    dec_file = tmp_path / "decisions.md"
    dec_file.write_text("# Decisions\n")

    wrote1 = upsert_decision_resolution(
        subject="subreddit-rollout-strategy",
        author_name="Zero",
        kind="proposal",
        summary_text="Initial proposal: post 1 anchor thread daily across 8-10 seed posts before cross-posting.",
        timestamp_str="2026-10-21 10:00 AM PT",
        decisions_path=dec_file,
    )
    assert wrote1

    wrote2 = upsert_decision_resolution(
        subject="subreddit-rollout-strategy",
        author_name="Zero / Amos / Aerial / Marvin",
        kind="consensus",
        summary_text="Updated consensus: pin one Playbook Megathread for daily recipe comments and use curated external links for top-level posts.",
        timestamp_str="2026-10-21 10:48 AM PT",
        decisions_path=dec_file,
    )
    assert wrote2

    content = dec_file.read_text()
    assert content.count("### Resolution: `subreddit-rollout-strategy`") == 1
    assert "Updated consensus: pin one Playbook Megathread" in content
    assert "Initial proposal:" not in content
    assert "supersedes 1 prior" in content


def test_compact_decisions_log_dedupes_and_supersedes(tmp_path):
    dec_file = tmp_path / "decisions.md"
    raw = """# Decisions

### Resolution: `agora-live-burst` (2026-10-20 01:00 PM PT)
* **Author**: Zero
* **Kind**: `status`
* **Summary**: BUY 50 FUEL @ 80 AT HELIOS and MOVE TO NEXUS

### Resolution: `topic-alpha` (2026-10-20 02:00 PM PT)
* **Author**: Amos
* **Kind**: `finding`
* **Summary**: First draft of the alpha architecture using polling every 5 seconds on the main thread.

### Resolution: `topic-alpha` (2026-10-20 02:15 PM PT)
* **Author**: Amos
* **Kind**: `consensus`
* **Summary**: Final alpha architecture replaces polling with event-driven sqlite-vec triggers and WAL checkpoints.

### Resolution: `general` (2026-10-20 03:00 PM PT)
* **Author**: Aerial
* **Kind**: `answer`
* **Summary**: Duplicate general note about masking CommonMark code spans before running regex replacements in outbound messages.

### Resolution: `general` (2026-10-20 03:01 PM PT)
* **Author**: Aerial
* **Kind**: `answer`
* **Summary**: Duplicate general note about masking CommonMark code spans before running regex replacements in outbound messages.
"""
    dec_file.write_text(raw)
    stats = compact_decisions_log(decisions_path=dec_file, dry_run=True, semantic_threshold=0.0)
    assert stats["before"] == 5
    assert stats["kept"] == 2
    assert stats["removed"] == 3


def test_compact_decisions_log_semantic_clustering(tmp_path):
    dec_file = tmp_path / "decisions.md"
    raw = """# Decisions

### Resolution: `banana-protocol-pr-11` (2026-09-05 08:29 PM PT)
* **Author**: Zero / Amos
* **Kind**: `resolution`
* **Summary**: Ratified decoupled floor governance timeout at 180s in PR #11 across all agents.

### Resolution: `banana-protocol-pr-11-v0.6.0` (2026-09-05 08:30 PM PT)
* **Author**: Zero / Amos / Marvin
* **Kind**: `resolution`
* **Summary**: Ratified decoupled floor governance timeout at 180s in PR #11 v0.6.0 across all agents.

### Resolution: `sqlite-vec-mmr-rerank` (2026-10-21 10:49 AM PT)
* **Author**: Zero
* **Kind**: `architecture`
* **Summary**: Added greedy MMR diversity re-ranking (lambda=0.7) to hybrid search.
"""
    dec_file.write_text(raw)
    stats = compact_decisions_log(decisions_path=dec_file, dry_run=True, semantic_threshold=0.50)
    assert stats["before"] == 3
    assert stats["semantic_merged"] == 1
    assert stats["kept"] == 2
