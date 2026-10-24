import sys
import json
import pytest

# Ensure workspace is on sys.path
sys.path.insert(0, "/workspace")

from tools.transcript_index import (
    check_has_vec,
    TranscriptIndexer,
    chunk_text,
    extract_frontmatter,
    format_search_results,
    init_schema,
    get_db,
)


@pytest.fixture
def temp_db(tmp_path):
    db_file = str(tmp_path / "test_index.db")
    con = get_db(db_file, load_vec=True)
    init_schema(con)
    con.close()
    return db_file


def test_chunk_text():
    # Empty
    assert chunk_text("") == []
    # Short
    assert chunk_text("Hello world", max_chars=100) == ["Hello world"]
    # Long text with paragraphs
    paras = ["Para " + str(i) + " " + ("content " * 20) for i in range(5)]
    full_text = "\n\n".join(paras)
    chunks = chunk_text(full_text, max_chars=150, overlap=30)
    assert len(chunks) >= 5
    for c in chunks:
        assert len(c) <= 250


def test_extract_frontmatter():
    doc = """---
name: Test Rule
category: rules
description: A test description
---
# Body
This is the markdown body.
"""
    fm, body = extract_frontmatter(doc)
    assert fm.get("name") == "Test Rule"
    assert fm.get("category") == "rules"
    assert "This is the markdown body." in body

    # No frontmatter
    doc2 = "Plain markdown text"
    fm2, body2 = extract_frontmatter(doc2)
    assert fm2 == {}
    assert body2 == "Plain markdown text"


def test_schema_init(temp_db):
    con = get_db(temp_db, load_vec=True)
    cur = con.cursor()
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "meta" in tables
    assert "indexed_files" in tables
    assert "conversations" in tables
    assert "memory_fts" in tables
    assert "transcript_fts" in tables
    assert "channel_fts" in tables
    assert "memory_chunks" in tables
    assert "transcript_chunks" in tables
    assert "channel_chunks" in tables
    if check_has_vec(con):
        assert "vec_memory" in tables
        assert "vec_transcripts" in tables
        assert "vec_channel" in tables
    con.close()


def test_indexing_and_hybrid_search(tmp_path):
    db_file = str(tmp_path / "hybrid_test.db")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    
    # Create test memory files
    doc1 = mem_dir / "nas_hang.md"
    doc1.write_text("""---
title: Synology Log Driver Hang
category: scars
description: Docker logs hanging indefinitely
---
DSM SQLite log driver hangs indefinitely on Host 2 when querying docker logs without tail.
Always use ha_get_state or check disk logs instead.
""")

    doc2 = mem_dir / "shopping.md"
    doc2.write_text("""---
title: Shopping Advisor Rule
category: rules
description: Amazon product recommendations
---
Product sourcing and gear recommendations must invoke shopping-advisor and amazon_serpapi.py.
Retrieve verified 1P Prime ASINs and defect review audits.
""")

    indexer = TranscriptIndexer(db_file)
    # Monkeypatch MEMORY_DIR for test
    import tools.transcript_index as ti
    orig_mem = ti.MEMORY_DIR
    ti.MEMORY_DIR = str(mem_dir)

    try:
        stats = indexer.sync_all(force=True, embed=True, scope="memory")
        assert stats["memory_files_indexed"] == 2
        assert stats["memory_chunks"] >= 2

        # 1. Exact match query
        res_exact = indexer.search("Synology Log Driver Hang", target="memory", mode="hybrid")
        assert len(res_exact["memory"]) >= 1
        top_match = res_exact["memory"][0]
        assert "Synology Log Driver Hang" in top_match["title"]
        assert top_match["match_type"] in ("hybrid", "exact_lexical")
        assert "rrf_score" in top_match

        # 2. Semantic query with different wording
        res_semantic = indexer.search("buying items on amazon prime", target="memory", mode="hybrid")
        assert len(res_semantic["memory"]) >= 1
        top_sem = res_semantic["memory"][0]
        assert "Shopping" in top_sem["title"]
        assert top_sem["match_type"] in ("hybrid", "semantic_vector")

        # 3. Test pure FTS mode
        res_fts = indexer.search("ASINs", target="memory", mode="fts")
        assert len(res_fts["memory"]) >= 1
        assert res_fts["memory"][0]["match_type"] == "exact_lexical"

        # 4. Format search results helper
        fmt = format_search_results(res_exact)
        assert "Search Results for" in fmt
        assert "Synology" in fmt

        # 5. Stats endpoint
        st = indexer.stats()
        assert st["indexed_memory_files"] == 2
        assert st["vector_chunks"]["memory"] >= 2
    finally:
        ti.MEMORY_DIR = orig_mem
        indexer.close()


def test_channel_indexing_and_search(tmp_path):
    db_file = str(tmp_path / "channel_test.db")
    ch_file = tmp_path / "channel_history.json"
    
    mock_channel_data = {
        "1542081375287640084": [
            {
                "id": "1001",
                "channel_name": "zero-chat",
                "author": "Ryan",
                "timestamp": "2026-09-30 20:00:00",
                "content": "Let's investigate the PostgreSQL RRF claims versus SQLite embedded vectors"
            },
            {
                "id": "1002",
                "channel_name": "zero-chat",
                "author": "Zero",
                "timestamp": "2026-09-30 20:01:00",
                "content": "Handled. FastEmbed ONNX runs in 11ms on CPU with sqlite-vec and zero daemons"
            }
        ]
    }
    ch_file.write_text(json.dumps(mock_channel_data))

    import tools.transcript_index as ti
    orig_ch = ti.CHANNEL_HISTORY_PATH
    ti.CHANNEL_HISTORY_PATH = str(ch_file)
    indexer = TranscriptIndexer(db_file)

    try:
        stats = indexer.sync_all(force=True, embed=True, scope="channel")
        assert stats["channel_messages_indexed"] == 2
        assert stats["channel_chunks"] == 2

        # Hybrid search on channel
        res = indexer.search("PostgreSQL versus SQLite", target="channel", mode="hybrid")
        assert len(res["channel"]) >= 1
        top = res["channel"][0]
        assert top["author"] == "Ryan"
        assert "rrf_score" in top
    finally:
        ti.CHANNEL_HISTORY_PATH = orig_ch
        indexer.close()


def test_transcript_indexing_and_search(tmp_path):
    db_file = str(tmp_path / "transcript_test.db")
    brain_dir = tmp_path / "brain"
    conv_dir = brain_dir / "conv_abc123" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True)
    t_file = conv_dir / "transcript.jsonl"

    lines = [
        {"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "created_at": "2026-09-30T21:00:00Z", "content": "How do we configure Dockhand and Tautulli backups?"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "source": "MODEL", "created_at": "2026-09-30T21:00:05Z", "content": "Dockhand and Tautulli run in SQLite WAL mode. Always checkpoint before copying DB files."}
    ]
    t_file.write_text("\n".join(json.dumps(item) for item in lines) + "\n")

    import tools.transcript_index as ti
    orig_brain = ti.BRAIN_DIR
    ti.BRAIN_DIR = str(brain_dir)
    indexer = TranscriptIndexer(db_file)

    try:
        stats = indexer.sync_all(force=True, embed=True, scope="transcripts", depth="full")
        assert stats["transcripts_indexed"] == 1
        assert stats["transcript_steps"] == 2
        assert stats["transcript_chunks"] == 2

        # Search transcripts
        res = indexer.search("SQLite WAL mode checkpoint", target="transcripts", mode="hybrid")
        assert len(res["transcripts"]) >= 1
        assert res["transcripts"][0]["conversation_id"] == "conv_abc123"
    finally:
        ti.BRAIN_DIR = orig_brain
        indexer.close()


def test_prune_missing(tmp_path):
    db_file = str(tmp_path / "prune_test.db")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    
    doc1 = mem_dir / "temp_doc.md"
    doc1.write_text("---\ntitle: Temporary\n---\nTemporary content to be pruned.")

    import tools.transcript_index as ti
    orig_mem = ti.MEMORY_DIR
    ti.MEMORY_DIR = str(mem_dir)
    indexer = TranscriptIndexer(db_file)

    try:
        indexer.sync_all(force=True, embed=True, scope="memory")
        assert indexer.stats()["indexed_memory_files"] == 1
        assert indexer.stats()["vector_chunks"]["memory"] == 1

        # Delete the file and prune
        doc1.unlink()
        p_stats = indexer.prune_missing()
        assert p_stats["memory_pruned"] == 1
        assert indexer.stats()["indexed_memory_files"] == 0
        assert indexer.stats()["vector_chunks"]["memory"] == 0
    finally:
        ti.MEMORY_DIR = orig_mem
        indexer.close()


def test_mmr_diversity_reranking(tmp_path):
    import numpy as np

    db_file = str(tmp_path / "mmr_test.db")
    indexer = TranscriptIndexer(db_file)
    try:
        v_cluster_1 = (np.array([1.0, 0.0] + [0.0] * 382, dtype=np.float32)).tobytes()
        v_cluster_2 = (np.array([0.99, 0.14] + [0.0] * 382, dtype=np.float32)).tobytes()
        v_novel = (np.array([0.0, 1.0] + [0.0] * 382, dtype=np.float32)).tobytes()

        bm25_list = [
            {"file_path": "/mem/doc_a.md", "file_name": "doc_a.md", "category": "scars", "title": "Cluster Doc A", "description": "", "snippet": "A", "bm25_score": -5.0},
            {"file_path": "/mem/doc_b.md", "file_name": "doc_b.md", "category": "scars", "title": "Cluster Doc B", "description": "", "snippet": "B", "bm25_score": -4.8},
            {"file_path": "/mem/doc_c.md", "file_name": "doc_c.md", "category": "rules", "title": "Novel Doc C", "description": "", "snippet": "C", "bm25_score": -4.5},
        ]
        vec_list = [
            {"chunk_id": 1, "distance": 0.1, "_embedding": v_cluster_1, "file_path": "/mem/doc_a.md", "file_name": "doc_a.md", "category": "scars", "title": "Cluster Doc A", "description": "", "content": "A"},
            {"chunk_id": 2, "distance": 0.12, "_embedding": v_cluster_2, "file_path": "/mem/doc_b.md", "file_name": "doc_b.md", "category": "scars", "title": "Cluster Doc B", "description": "", "content": "B"},
            {"chunk_id": 3, "distance": 0.15, "_embedding": v_novel, "file_path": "/mem/doc_c.md", "file_name": "doc_c.md", "category": "rules", "title": "Novel Doc C", "description": "", "content": "C"},
        ]

        fused = indexer._fuse_memory_rrf(bm25_list, vec_list, limit=2, k=60, mmr_lambda=0.7)
        assert len(fused) == 2
        assert fused[0]["file_path"] == "/mem/doc_a.md"
        # MMR promotes orthogonal Novel Doc C over near-duplicate Cluster Doc B for slot 2
        assert fused[1]["file_path"] == "/mem/doc_c.md"
    finally:
        indexer.close()


def test_asymptotic_recency_decay_floor(tmp_path):
    import time
    from tools.transcript_index import compute_recency_multiplier

    # Brand new memory = 1.0
    assert compute_recency_multiplier(0.0) == 1.0
    # 30 days (1 half-life) = 0.95
    assert abs(compute_recency_multiplier(30.0) - 0.95) < 1e-4
    # 10 years old never drops below 0.90 floor
    assert compute_recency_multiplier(3650.0) >= 0.90
    assert abs(compute_recency_multiplier(3650.0) - 0.90) < 1e-4

    # Verify tie-breaking across two equally ranked candidates where one is fresh and one is 180 days old
    db_file = str(tmp_path / "recency_test.db")
    indexer = TranscriptIndexer(db_file)
    try:
        now = time.time()
        with indexer.con:
            indexer.con.execute(
                "INSERT INTO indexed_files (path, mtime, size, item_count, indexed_at) VALUES (?, ?, 100, 1, 'now')",
                ("/mem/old_doc.md", now - 180 * 86400),
            )
            indexer.con.execute(
                "INSERT INTO indexed_files (path, mtime, size, item_count, indexed_at) VALUES (?, ?, 100, 1, 'now')",
                ("/mem/new_doc.md", now),
            )

        # Give old_doc rank 1 in BM25 and rank 2 in vector; give new_doc rank 2 in BM25 and rank 1 in vector
        # Without recency tie-breaker, both have identical raw RRF score (1/61 + 1/62)
        bm25_list = [
            {"file_path": "/mem/old_doc.md", "file_name": "old_doc.md", "category": "scars", "title": "Old Doc", "description": "", "snippet": "old", "bm25_score": -5.0},
            {"file_path": "/mem/new_doc.md", "file_name": "new_doc.md", "category": "scars", "title": "New Doc", "description": "", "snippet": "new", "bm25_score": -5.0},
        ]
        vec_list = [
            {"chunk_id": 1, "distance": 0.1, "_embedding": None, "file_path": "/mem/new_doc.md", "file_name": "new_doc.md", "category": "scars", "title": "New Doc", "description": "", "content": "new"},
            {"chunk_id": 2, "distance": 0.1, "_embedding": None, "file_path": "/mem/old_doc.md", "file_name": "old_doc.md", "category": "scars", "title": "Old Doc", "description": "", "content": "old"},
        ]

        fused = indexer._fuse_memory_rrf(bm25_list, vec_list, limit=2, k=60)
        assert fused[0]["file_path"] == "/mem/new_doc.md"
        assert fused[0]["recency_multiplier"] == 1.0
        assert fused[1]["file_path"] == "/mem/old_doc.md"
        assert 0.90 <= fused[1]["recency_multiplier"] < 0.91
    finally:
        indexer.close()


def test_embedding_proxy_fast_path(tmp_path, monkeypatch):
    import base64
    import numpy as np
    import urllib.request
    import tools.transcript_index as ti

    db_file = str(tmp_path / "proxy_test.db")
    indexer = TranscriptIndexer(db_file, use_proxy=True)
    try:
        monkeypatch.setattr(ti, "_EMBEDDING_MODEL", None)
        fake_vec = np.ones(384, dtype=np.float32) / np.sqrt(384)
        fake_b64 = base64.b64encode(fake_vec.tobytes()).decode("ascii")

        called_urls = []

        class DummyResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def getcode(self):
                return 200

            def read(self):
                return json.dumps({"embedding_b64": fake_b64}).encode("utf-8")

        def fake_urlopen(req, timeout=8.0):
            called_urls.append(req.full_url)
            return DummyResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(
            ti,
            "get_embedding_model",
            lambda: (_ for _ in ()).throw(AssertionError("Local model should not load when proxy succeeds")),
        )

        res = indexer.search("test proxy embedding query", target="memory", mode="hybrid", limit=3)
        assert len(called_urls) == 1
        assert "/api/embed?q=" in called_urls[0]
        assert res["mode"] == "hybrid"
    finally:
        indexer.close()

