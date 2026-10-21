#!/usr/bin/env python3
"""SQLite FTS5 + Dense Vector Hybrid Search Engine for Zero / Antigravity.

Provides sub-15ms hybrid search combining:
1. Sparse Lexical Search: SQLite FTS5 (BM25 porter unicode61)
2. Dense Vector Search: Pure ONNX FastEmbed (BAAI/bge-small-en-v1.5) via sqlite-vec
3. Reciprocal Rank Fusion (RRF, k=60) mathematically fusing ranked lists

Corpus scopes:
- Long-term memory markdown documents (/workspace/memory/**/*.md)
- Agent session transcripts (~/.gemini/antigravity-cli/brain/*/transcript.jsonl)
- Discord channel & thread message history (/workspace/data/channel_history.json)
"""

import os
import sys
import glob
import json
import time
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

DB_PATH = os.environ.get("TRANSCRIPT_INDEX_DB", "/workspace/data/transcript_index.db")
BRAIN_DIR = os.environ.get("GEMINI_BRAIN_DIR", "/root/.gemini/antigravity-cli/brain")
MEMORY_DIR = os.environ.get("WORKSPACE_MEMORY_DIR", "/workspace/memory")
CHANNEL_HISTORY_PATH = os.environ.get("CHANNEL_HISTORY_PATH", "/workspace/data/channel_history.json")

MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_CACHE_DIR = os.environ.get("FASTEMBED_CACHE_PATH", "/workspace/data/cache/fastembed")
DEFAULT_RRF_K = 60
EMBEDDING_DIM = 384

_EMBEDDING_MODEL = None


def get_embedding_model():
    """Lazy load singleton FastEmbed ONNX model with persistent local disk cache."""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        try:
            from fastembed import TextEmbedding
        except ImportError:
            return None

        os.makedirs(MODEL_CACHE_DIR, exist_ok=True)
        try:
            _EMBEDDING_MODEL = TextEmbedding(MODEL_NAME, cache_dir=MODEL_CACHE_DIR, local_files_only=True)
        except Exception:
            try:
                _EMBEDDING_MODEL = TextEmbedding(MODEL_NAME, cache_dir=MODEL_CACHE_DIR)
            except Exception as e:
                sys.stderr.write(f"Warning: Failed to load FastEmbed model: {e}\n")
                return None
    return _EMBEDDING_MODEL


def check_has_vec(con: sqlite3.Connection) -> bool:
    """Check if sqlite-vec extension is active on this connection."""
    try:
        cur = con.cursor()
        cur.execute("SELECT vec_version();")
        return cur.fetchone() is not None
    except Exception:
        return False


def get_db(db_path: str = DB_PATH, load_vec: bool = True) -> sqlite3.Connection:
    """Connect to SQLite database and dynamically load sqlite-vec extension if available."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path, timeout=60.0)
    con.execute("PRAGMA busy_timeout = 60000;")
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA foreign_keys=OFF;")

    if load_vec:
        try:
            import sqlite_vec
            con.enable_load_extension(True)
            sqlite_vec.load(con)
            con.enable_load_extension(False)
        except Exception:
            pass

    return con


def init_schema(con: sqlite3.Connection):
    """Initialize FTS5, metadata, chunk, and sqlite-vec tables."""
    with con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS indexed_files (
                path TEXT PRIMARY KEY,
                mtime REAL,
                size INTEGER,
                item_count INTEGER,
                indexed_at TEXT
            );
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                started_at TEXT,
                updated_at TEXT,
                step_count INTEGER,
                first_user_prompt TEXT,
                mtime REAL
            );
        """)
        # FTS5 tables
        con.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
                conversation_id UNINDEXED,
                step_index UNINDEXED,
                role,
                created_at UNINDEXED,
                content,
                tool_calls,
                thinking,
                tokenize='porter unicode61'
            );
        """)
        con.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                file_path UNINDEXED,
                file_name,
                category,
                title,
                description,
                content,
                tokenize='porter unicode61'
            );
        """)
        con.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS channel_fts USING fts5(
                message_id UNINDEXED,
                channel_id UNINDEXED,
                channel_name,
                author,
                timestamp UNINDEXED,
                content,
                tokenize='porter unicode61'
            );
        """)

        # Vector chunk companion metadata tables
        con.execute("""
            CREATE TABLE IF NOT EXISTS memory_chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                file_name TEXT,
                category TEXT,
                title TEXT,
                description TEXT,
                chunk_index INTEGER DEFAULT 0,
                content TEXT
            );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_mem_chunks_path ON memory_chunks(file_path);")

        con.execute("""
            CREATE TABLE IF NOT EXISTS transcript_chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                role TEXT,
                created_at TEXT,
                title TEXT,
                content TEXT,
                tool_calls TEXT
            );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_tr_chunks_conv ON transcript_chunks(conversation_id);")

        con.execute("""
            CREATE TABLE IF NOT EXISTS channel_chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                channel_id TEXT,
                channel_name TEXT,
                author TEXT,
                timestamp TEXT,
                content TEXT
            );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_ch_chunks_msg ON channel_chunks(message_id);")

        # sqlite-vec virtual tables
        if check_has_vec(con):
            con.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory USING vec0(
                    chunk_id INTEGER PRIMARY KEY,
                    embedding float[{EMBEDDING_DIM}]
                );
            """)
            con.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_transcripts USING vec0(
                    chunk_id INTEGER PRIMARY KEY,
                    embedding float[{EMBEDDING_DIM}]
                );
            """)
            con.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_channel USING vec0(
                    chunk_id INTEGER PRIMARY KEY,
                    embedding float[{EMBEDDING_DIM}]
                );
            """)


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150) -> List[str]:
    """Split text into coherent semantic paragraph chunks with overlap."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current_chunk = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            start = 0
            while start < len(para):
                end = min(start + max_chars, len(para))
                if end < len(para):
                    space_idx = para.rfind(" ", start + max_chars // 2, end)
                    if space_idx != -1:
                        end = space_idx
                sub = para[start:end].strip()
                if sub:
                    chunks.append(sub)
                if end >= len(para):
                    break
                start = max(start + 1, end - overlap)
            continue

        if current_len + len(para) + 2 > max_chars:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                if len(current_chunk[-1]) <= overlap:
                    current_chunk = [current_chunk[-1], para]
                    current_len = len(current_chunk[0]) + len(para) + 2
                else:
                    current_chunk = [para]
                    current_len = len(para)
            else:
                chunks.append(para)
                current_chunk = []
                current_len = 0
        else:
            current_chunk.append(para)
            current_len += len(para) + 2

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def extract_frontmatter(content: str) -> Tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    raw_fm, body = parts[1], parts[2]
    meta = {}
    for line in raw_fm.splitlines():
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body.strip()


class TranscriptIndexer:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.con = get_db(db_path, load_vec=True)
        cur = self.con.cursor()
        has_tables = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_chunks'"
        ).fetchone()
        if not has_tables:
            init_schema(self.con)
        self.has_vec = check_has_vec(self.con)

    def close(self):
        self.con.close()

    def sync_all(self, force: bool = False, embed: bool = True, scope: str = "all", depth: str = "recent") -> Dict[str, Any]:
        t0 = time.time()
        stats = {
            "transcripts_indexed": 0,
            "transcripts_skipped": 0,
            "transcript_steps": 0,
            "transcript_chunks": 0,
            "memory_files_indexed": 0,
            "memory_files_skipped": 0,
            "memory_chunks": 0,
            "channel_messages_indexed": 0,
            "channel_chunks": 0,
            "duration_s": 0.0,
            "has_vec": self.has_vec,
        }

        embed_enabled = embed and self.has_vec and (get_embedding_model() is not None)

        # 1. Sync Transcripts
        if scope in ("all", "transcripts"):
            self._sync_transcripts(force, stats, embed=embed_enabled, depth=depth)

        # 2. Sync Memory Files
        if scope in ("all", "memory"):
            self._sync_memory(force, stats, embed=embed_enabled)

        # 3. Sync Channel History
        if scope in ("all", "channel"):
            self._sync_channel_history(force, stats, embed=embed_enabled)

        # 4. Prune Missing Files
        prune_stats = self.prune_missing()
        stats["transcripts_pruned"] = prune_stats["transcripts_pruned"]
        stats["memory_pruned"] = prune_stats["memory_pruned"]

        # Update meta timestamp
        with self.con:
            self.con.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("last_sync", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            )

        stats["duration_s"] = round(time.time() - t0, 3)
        return stats

    def _sync_transcripts(self, force: bool, stats: dict, embed: bool = True, depth: str = "recent"):
        if not os.path.exists(BRAIN_DIR):
            return

        cursor = self.con.cursor()
        indexed_map = {}
        for row in cursor.execute("SELECT path, mtime, size FROM indexed_files WHERE path LIKE '%transcript.jsonl'"):
            indexed_map[row[0]] = (row[1], row[2])

        conv_dirs = [d for d in os.listdir(BRAIN_DIR) if os.path.isdir(os.path.join(BRAIN_DIR, d))]
        model = get_embedding_model() if embed else None

        # Sort conv_dirs by mtime descending so 'recent' depth captures newest sessions
        conv_paths = []
        for conv_id in conv_dirs:
            t_path = os.path.join(BRAIN_DIR, conv_id, ".system_generated", "logs", "transcript.jsonl")
            if os.path.isfile(t_path):
                try:
                    conv_paths.append((conv_id, t_path, os.path.getmtime(t_path)))
                except OSError:
                    pass
        conv_paths.sort(key=lambda x: x[2], reverse=True)
        print(f"Syncing transcripts: {len(conv_paths)} sessions found (depth: {depth})...", flush=True)

        for idx, (conv_id, t_path, mtime) in enumerate(conv_paths):
            try:
                st = os.stat(t_path)
                mtime, size = st.st_mtime, st.st_size
            except Exception:
                continue

            prev = indexed_map.get(t_path)
            # If already indexed and not forced, check if vector chunks also exist
            if not force and prev and prev[0] == mtime and prev[1] == size:
                if not embed:
                    stats["transcripts_skipped"] += 1
                    continue
                # If embed requested, verify vector chunk presence
                chunk_exists = cursor.execute("SELECT 1 FROM transcript_chunks WHERE conversation_id = ? LIMIT 1", (conv_id,)).fetchone()
                if chunk_exists:
                    stats["transcripts_skipped"] += 1
                    continue

            # Read and parse JSONL
            steps_data = []
            embed_candidates = []
            started_at = ""
            updated_at = ""
            first_prompt = ""

            with open(t_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    s_idx = obj.get("step_index", 0)
                    s_type = obj.get("type", "")
                    s_source = obj.get("source", "")
                    c_at = obj.get("created_at", "")
                    content = obj.get("content") or ""
                    thinking = obj.get("thinking") or ""
                    tool_calls = obj.get("tool_calls") or []

                    if not started_at and c_at:
                        started_at = c_at
                    if c_at:
                        updated_at = c_at

                    # Determine Role
                    if s_type == "USER_INPUT" or s_source == "USER_EXPLICIT":
                        role = "user"
                        if not first_prompt and content:
                            first_prompt = content[:300].strip()
                    elif s_type in ("TOOL_OUTPUT", "GENERIC") or s_source == "SYSTEM":
                        role = "tool" if s_type in ("TOOL_OUTPUT", "GENERIC") else "system"
                    elif s_type == "PLANNER_RESPONSE" or s_source == "MODEL":
                        role = "assistant"
                    else:
                        role = "other"

                    tool_str = ""
                    if tool_calls:
                        t_items = []
                        for tc in tool_calls:
                            if isinstance(tc, dict):
                                t_name = tc.get("name", "")
                                t_args = tc.get("args", {})
                                t_items.append(f"{t_name}({json.dumps(t_args, ensure_ascii=False)})")
                            elif isinstance(tc, str):
                                t_items.append(tc)
                        tool_str = " ".join(t_items)

                    steps_data.append((conv_id, s_idx, role, c_at, content, tool_str, thinking))

                    # Filter vector embedding candidates:
                    # Always embed user prompts; embed assistant answers based on depth
                    if embed and model:
                        if role == "user" and content.strip():
                            embed_candidates.append({
                                "step_index": s_idx,
                                "role": "user",
                                "created_at": c_at,
                                "title": f"User Turn #{s_idx}",
                                "content": content.strip()[:1500],
                                "tool_calls": tool_str[:300],
                                "embed_text": f"User: {content.strip()[:1000]}"
                            })
                        elif role == "assistant" and len(content.strip()) >= 30:
                            # Depth options: 'full' embeds all sessions; 'recent' embeds top 50; 'user' embeds user-only
                            should_embed_asst = (depth == "full") or (depth == "recent" and idx < 50)
                            if should_embed_asst:
                                embed_candidates.append({
                                    "step_index": s_idx,
                                    "role": "assistant",
                                    "created_at": c_at,
                                    "title": f"Assistant Response #{s_idx}",
                                    "content": content.strip()[:1500],
                                    "tool_calls": tool_str[:300],
                                    "embed_text": f"Assistant: {content.strip()[:1000]}"
                                })

            # FTS5 + Metadata write
            with self.con:
                self.con.execute("DELETE FROM transcript_fts WHERE conversation_id = ?", (conv_id,))
                self.con.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))

                self.con.execute("""
                    INSERT OR REPLACE INTO conversations 
                    (conversation_id, started_at, updated_at, step_count, first_user_prompt, mtime)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (conv_id, started_at, updated_at, len(steps_data), first_prompt, mtime))

                self.con.executemany("""
                    INSERT INTO transcript_fts (conversation_id, step_index, role, created_at, content, tool_calls, thinking)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, steps_data)

                # Vector chunk cleanup and write
                if embed and self.has_vec:
                    prev_cids = [r[0] for r in self.con.execute("SELECT chunk_id FROM transcript_chunks WHERE conversation_id = ?", (conv_id,)).fetchall()]
                    for cid in prev_cids:
                        self.con.execute("DELETE FROM vec_transcripts WHERE chunk_id = ?", (cid,))
                    self.con.execute("DELETE FROM transcript_chunks WHERE conversation_id = ?", (conv_id,))

                    if embed_candidates and model:
                        texts_to_embed = [cand["embed_text"] for cand in embed_candidates]
                        try:
                            embs = list(model.embed(texts_to_embed, batch_size=32))
                            import numpy as np
                            for cand, emb in zip(embed_candidates, embs):
                                cur = self.con.cursor()
                                cur.execute("""
                                    INSERT INTO transcript_chunks 
                                    (conversation_id, step_index, role, created_at, title, content, tool_calls)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                """, (conv_id, cand["step_index"], cand["role"], cand["created_at"], cand["title"], cand["content"], cand["tool_calls"]))
                                c_id = cur.lastrowid
                                self.con.execute(
                                    "INSERT INTO vec_transcripts (chunk_id, embedding) VALUES (?, ?)",
                                    (c_id, np.array(emb, dtype=np.float32).tobytes())
                                )
                            stats["transcript_chunks"] += len(embed_candidates)
                        except Exception as e:
                            sys.stderr.write(f"Warning: embedding failed for {conv_id}: {e}\n")

                self.con.execute("""
                    INSERT OR REPLACE INTO indexed_files (path, mtime, size, item_count, indexed_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                """, (t_path, mtime, size, len(steps_data)))

            stats["transcripts_indexed"] += 1
            stats["transcript_steps"] += len(steps_data)
            if (idx + 1) % 25 == 0 or (idx + 1) == len(conv_paths):
                print(f"Transcripts processed: {idx + 1}/{len(conv_paths)} (chunks: {stats['transcript_chunks']})", flush=True)

    def _sync_memory(self, force: bool, stats: dict, embed: bool = True):
        if not os.path.exists(MEMORY_DIR):
            return

        cursor = self.con.cursor()
        indexed_map = {}
        for row in cursor.execute("SELECT path, mtime, size FROM indexed_files WHERE path LIKE '%.md'"):
            indexed_map[row[0]] = (row[1], row[2])

        raw_md_files = glob.glob(os.path.join(MEMORY_DIR, "**/*.md"), recursive=True)
        skip_names = {"MEMORY.md", "MEMORY_PRIVATE.md", "MEMORY_PUBLIC.md", "VAULT.md"}
        md_files = [
            p for p in raw_md_files
            if not os.path.islink(p) and os.path.basename(p) not in skip_names
        ]
        model = get_embedding_model() if embed else None
        print(f"Syncing memory files: {len(md_files)} files found...", flush=True)

        for m_idx, m_path in enumerate(md_files):
            try:
                st = os.stat(m_path)
                mtime, size = st.st_mtime, st.st_size
            except Exception:
                continue

            prev = indexed_map.get(m_path)
            if not force and prev and prev[0] == mtime and prev[1] == size:
                if not embed:
                    stats["memory_files_skipped"] += 1
                    continue
                # Verify chunk exists
                chunk_exists = cursor.execute("SELECT 1 FROM memory_chunks WHERE file_path = ? LIMIT 1", (m_path,)).fetchone()
                if chunk_exists:
                    stats["memory_files_skipped"] += 1
                    continue

            try:
                with open(m_path, "r", encoding="utf-8", errors="ignore") as f:
                    raw_text = f.read()
            except Exception:
                continue

            fm, body = extract_frontmatter(raw_text)
            fname = os.path.basename(m_path)
            title = fm.get("name") or fm.get("title") or fname.replace(".md", "")
            desc = fm.get("description") or ""

            rel_dir = os.path.dirname(os.path.relpath(m_path, MEMORY_DIR))
            cat = rel_dir if rel_dir and rel_dir != "." else "root"

            with self.con:
                self.con.execute("DELETE FROM memory_fts WHERE file_path = ?", (m_path,))
                self.con.execute("""
                    INSERT INTO memory_fts (file_path, file_name, category, title, description, content)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (m_path, fname, cat, title, desc, body))

                # Vector chunking & embedding
                if embed and self.has_vec:
                    prev_cids = [r[0] for r in self.con.execute("SELECT chunk_id FROM memory_chunks WHERE file_path = ?", (m_path,)).fetchall()]
                    for cid in prev_cids:
                        self.con.execute("DELETE FROM vec_memory WHERE chunk_id = ?", (cid,))
                    self.con.execute("DELETE FROM memory_chunks WHERE file_path = ?", (m_path,))

                    chunks = chunk_text(body, max_chars=1200, overlap=150)
                    if chunks and model:
                        texts_to_embed = [f"[{cat}] {title}\n{chk}" for chk in chunks]
                        try:
                            embs = list(model.embed(texts_to_embed, batch_size=32))
                            import numpy as np
                            for idx, (chk, emb) in enumerate(zip(chunks, embs)):
                                cur = self.con.cursor()
                                chunk_title = title if len(chunks) == 1 else f"{title} (Part {idx + 1})"
                                cur.execute("""
                                    INSERT INTO memory_chunks 
                                    (file_path, file_name, category, title, description, chunk_index, content)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                """, (m_path, fname, cat, chunk_title, desc, idx, chk))
                                c_id = cur.lastrowid
                                self.con.execute(
                                    "INSERT INTO vec_memory (chunk_id, embedding) VALUES (?, ?)",
                                    (c_id, np.array(emb, dtype=np.float32).tobytes())
                                )
                            stats["memory_chunks"] += len(chunks)
                        except Exception as e:
                            sys.stderr.write(f"Warning: embedding failed for {m_path}: {e}\n")

                self.con.execute("""
                    INSERT OR REPLACE INTO indexed_files (path, mtime, size, item_count, indexed_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                """, (m_path, mtime, size, 1))

            stats["memory_files_indexed"] += 1
            if (m_idx + 1) % 100 == 0 or (m_idx + 1) == len(md_files):
                print(f"Memory files processed: {m_idx + 1}/{len(md_files)} (chunks: {stats['memory_chunks']})", flush=True)

    def _sync_channel_history(self, force: bool, stats: dict, embed: bool = True):
        if not os.path.exists(CHANNEL_HISTORY_PATH):
            return

        try:
            st = os.stat(CHANNEL_HISTORY_PATH)
            mtime, size = st.st_mtime, st.st_size
        except Exception:
            return

        cursor = self.con.cursor()
        row = cursor.execute("SELECT mtime, size FROM indexed_files WHERE path = ?", (CHANNEL_HISTORY_PATH,)).fetchone()
        if not force and row and row[0] == mtime and row[1] == size:
            if not embed:
                return
            has_channel_chunks = cursor.execute("SELECT 1 FROM channel_chunks LIMIT 1").fetchone()
            if has_channel_chunks:
                return

        try:
            with open(CHANNEL_HISTORY_PATH, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
        except Exception:
            return

        messages = []
        embed_msgs = []
        if isinstance(data, dict):
            for ch_id, msg_list in data.items():
                if isinstance(msg_list, list):
                    for m in msg_list:
                        if not isinstance(m, dict):
                            continue
                        m_id = str(m.get("id", ""))
                        ch_name = m.get("channel_name", "")
                        author = m.get("author", "")
                        ts = m.get("timestamp", "")
                        content = m.get("content", "")
                        if content:
                            messages.append((m_id, ch_id, ch_name, author, ts, content))
                            if len(content.strip()) >= 8:
                                embed_msgs.append((m_id, ch_id, ch_name, author, ts, content.strip()))

        model = get_embedding_model() if embed else None

        with self.con:
            self.con.execute("DELETE FROM channel_fts;")
            self.con.executemany("""
                INSERT INTO channel_fts (message_id, channel_id, channel_name, author, timestamp, content)
                VALUES (?, ?, ?, ?, ?, ?)
            """, messages)

            if embed and self.has_vec:
                self.con.execute("DELETE FROM vec_channel;")
                self.con.execute("DELETE FROM channel_chunks;")

                if embed_msgs and model:
                    texts_to_embed = [f"#{m[2]} ({m[3]}): {m[5]}" for m in embed_msgs]
                    try:
                        print(f"Embedding {len(texts_to_embed)} channel messages...", flush=True)
                        embs = model.embed(texts_to_embed, batch_size=64)
                        import numpy as np
                        for idx, (m, emb) in enumerate(zip(embed_msgs, embs)):
                            cur = self.con.cursor()
                            cur.execute("""
                                INSERT INTO channel_chunks 
                                (message_id, channel_id, channel_name, author, timestamp, content)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (m[0], m[1], m[2], m[3], m[4], m[5]))
                            c_id = cur.lastrowid
                            self.con.execute(
                                "INSERT INTO vec_channel (chunk_id, embedding) VALUES (?, ?)",
                                (c_id, np.array(emb, dtype=np.float32).tobytes())
                            )
                            if (idx + 1) % 100 == 0 or (idx + 1) == len(embed_msgs):
                                print(f"Channel messages embedded: {idx + 1}/{len(embed_msgs)}", flush=True)
                        stats["channel_chunks"] += len(embed_msgs)
                    except Exception as e:
                        sys.stderr.write(f"Warning: channel history embedding failed: {e}\n")

            self.con.execute("""
                INSERT OR REPLACE INTO indexed_files (path, mtime, size, item_count, indexed_at)
                VALUES (?, ?, ?, ?, datetime('now'))
            """, (CHANNEL_HISTORY_PATH, mtime, size, len(messages)))

        stats["channel_messages_indexed"] = len(messages)

    def prune_missing(self) -> dict:
        """Purge indexed transcripts and memory files that no longer exist on disk."""
        stats = {"transcripts_pruned": 0, "memory_pruned": 0}
        cursor = self.con.cursor()

        # 1. Check transcripts
        cursor.execute("SELECT path FROM indexed_files WHERE path LIKE '%transcript.jsonl'")
        for (p,) in cursor.fetchall():
            if not os.path.exists(p):
                parts = Path(p).parts
                conv_id = None
                for i, part in enumerate(parts):
                    if part == "brain" and i + 1 < len(parts):
                        conv_id = parts[i + 1]
                        break
                with self.con:
                    self.con.execute("DELETE FROM indexed_files WHERE path = ?", (p,))
                    if conv_id:
                        self.con.execute("DELETE FROM conversations WHERE conversation_id = ?", (conv_id,))
                        self.con.execute("DELETE FROM transcript_fts WHERE conversation_id = ?", (conv_id,))
                        if self.has_vec:
                            c_ids = [r[0] for r in self.con.execute("SELECT chunk_id FROM transcript_chunks WHERE conversation_id = ?", (conv_id,)).fetchall()]
                            for cid in c_ids:
                                self.con.execute("DELETE FROM vec_transcripts WHERE chunk_id = ?", (cid,))
                            self.con.execute("DELETE FROM transcript_chunks WHERE conversation_id = ?", (conv_id,))
                stats["transcripts_pruned"] += 1

        # 2. Check memory files (also prune symlinks and index catalogs)
        skip_names = {"MEMORY.md", "MEMORY_PRIVATE.md", "MEMORY_PUBLIC.md", "VAULT.md"}
        cursor.execute("SELECT path FROM indexed_files WHERE path LIKE '%.md'")
        for (p,) in cursor.fetchall():
            if not os.path.exists(p) or os.path.islink(p) or os.path.basename(p) in skip_names:
                with self.con:
                    self.con.execute("DELETE FROM indexed_files WHERE path = ?", (p,))
                    self.con.execute("DELETE FROM memory_fts WHERE file_path = ?", (p,))
                    if self.has_vec:
                        c_ids = [r[0] for r in self.con.execute("SELECT chunk_id FROM memory_chunks WHERE file_path = ?", (p,)).fetchall()]
                        for cid in c_ids:
                            self.con.execute("DELETE FROM vec_memory WHERE chunk_id = ?", (cid,))
                        self.con.execute("DELETE FROM memory_chunks WHERE file_path = ?", (p,))
                stats["memory_pruned"] += 1

        return stats

    def search(
        self,
        query: str,
        target: str = "all",
        limit: int = 10,
        mode: str = "hybrid",
        rrf_k: int = DEFAULT_RRF_K
    ) -> Dict[str, Any]:
        """Execute BM25, dense vector, or Reciprocal Rank Fusion hybrid search."""
        t_start = time.perf_counter()
        results = {
            "query": query,
            "target": target,
            "mode": mode,
            "transcripts": [],
            "memory": [],
            "channel": [],
            "latency_ms": 0.0,
        }

        # Check if vector capability is available
        model = get_embedding_model() if mode in ("hybrid", "vector") else None
        can_vector = self.has_vec and (model is not None)

        # Fallback to fts if vector search is impossible
        if mode == "vector" and not can_vector:
            results["error"] = "sqlite-vec or FastEmbed embedding model unavailable for vector search."
            return results
        if mode == "hybrid" and not can_vector:
            mode = "fts"

        # Embed query once for all targets if vector needed
        query_bytes = None
        if mode in ("hybrid", "vector") and model:
            import numpy as np
            q_emb = list(model.embed([query]))[0]
            query_bytes = np.array(q_emb, dtype=np.float32).tobytes()

        fts_query = self._format_query(query)
        candidate_k = max(limit * 3, 25)

        # 1. Search Memory
        if target in ("all", "memory"):
            results["memory"] = self._search_memory_target(
                fts_query, query_bytes, limit, mode, rrf_k, candidate_k
            )

        # 2. Search Transcripts
        if target in ("all", "transcripts"):
            results["transcripts"] = self._search_transcripts_target(
                fts_query, query_bytes, limit, mode, rrf_k, candidate_k
            )

        # 3. Search Channel History
        if target in ("all", "channel"):
            results["channel"] = self._search_channel_target(
                fts_query, query_bytes, limit, mode, rrf_k, candidate_k
            )

        results["latency_ms"] = round((time.perf_counter() - t_start) * 1000, 2)
        return results

    def _search_memory_target(self, fts_query: str, query_bytes: Optional[bytes], limit: int, mode: str, rrf_k: int, candidate_k: int) -> List[dict]:
        cursor = self.con.cursor()
        bm25_matches: List[dict] = []
        vec_matches: List[dict] = []

        # A. FTS5 BM25
        if mode in ("hybrid", "fts") and fts_query:
            try:
                q = """
                    SELECT file_path, file_name, category, title, description,
                           snippet(memory_fts, 5, '«b»', '«/b»', '...', 32) as match_content,
                           bm25(memory_fts) as rank
                    FROM memory_fts
                    WHERE memory_fts MATCH ?
                    ORDER BY rank ASC
                    LIMIT ?
                """
                for row in cursor.execute(q, (fts_query, candidate_k)):
                    bm25_matches.append({
                        "file_path": row[0],
                        "file_name": row[1],
                        "category": row[2],
                        "title": row[3],
                        "description": row[4],
                        "snippet": (row[5] or "").replace("«b»", "**").replace("«/b»", "**"),
                        "bm25_score": round(row[6], 4)
                    })
            except sqlite3.OperationalError:
                pass

        # B. Vector KNN (2-step lookup so SQLite planner preserves vec0 KNN index instead of full-table JOIN scan)
        if mode in ("hybrid", "vector") and query_bytes and self.has_vec:
            try:
                v_rows = cursor.execute(
                    """
                    SELECT chunk_id, distance, embedding
                    FROM vec_memory
                    WHERE embedding MATCH ? AND k = ?
                    ORDER BY distance ASC
                    """,
                    (query_bytes, candidate_k),
                ).fetchall()
                if v_rows:
                    cids = [r[0] for r in v_rows]
                    placeholders = ",".join("?" * len(cids))
                    chunk_map = {
                        r[0]: r[1:]
                        for r in cursor.execute(
                            f"""
                            SELECT chunk_id, file_path, file_name, category, title, description, content
                            FROM memory_chunks
                            WHERE chunk_id IN ({placeholders})
                            """,
                            cids,
                        )
                    }
                    for cid, dist, emb_bytes in v_rows:
                        c_row = chunk_map.get(cid)
                        if c_row:
                            vec_matches.append({
                                "chunk_id": cid,
                                "distance": round(dist, 4),
                                "_embedding": emb_bytes,
                                "file_path": c_row[0],
                                "file_name": c_row[1],
                                "category": c_row[2],
                                "title": c_row[3],
                                "description": c_row[4],
                                "content": c_row[5],
                            })
            except Exception:
                pass

        if mode == "fts":
            for m in bm25_matches:
                m["match_type"] = "exact_lexical"
            return bm25_matches[:limit]
        if mode == "vector":
            seen = set()
            out = []
            for v in vec_matches:
                if v["file_path"] not in seen:
                    seen.add(v["file_path"])
                    v.pop("_embedding", None)
                    v["snippet"] = v["content"][:220].strip() + "..."
                    v["match_type"] = "semantic_vector"
                    out.append(v)
            return out[:limit]

        # Hybrid RRF Fusion
        return self._fuse_memory_rrf(bm25_matches, vec_matches, limit, rrf_k)

    def _fuse_memory_rrf(self, bm25_list: List[dict], vec_list: List[dict], limit: int, k: int, mmr_lambda: float = 0.7) -> List[dict]:
        scores: Dict[str, float] = {}
        items: Dict[str, dict] = {}
        meta: Dict[str, dict] = {}
        emb_map: Dict[str, Any] = {}

        for rank, item in enumerate(bm25_list, start=1):
            fp = item["file_path"]
            scores[fp] = scores.get(fp, 0.0) + (1.0 / (k + rank))
            items[fp] = item
            meta[fp] = {"bm25_rank": rank, "vector_rank": None, "distance": None}

        # Deduplicate multiple chunks from same file for vector ranks (keep best rank)
        vec_seen = set()
        v_rank = 1
        for item in vec_list:
            fp = item["file_path"]
            if fp in vec_seen:
                continue
            vec_seen.add(fp)
            if item.get("_embedding"):
                emb_map[fp] = item["_embedding"]
            scores[fp] = scores.get(fp, 0.0) + (1.0 / (k + v_rank))
            if fp not in items:
                items[fp] = {
                    "file_path": item["file_path"],
                    "file_name": item["file_name"],
                    "category": item["category"],
                    "title": item["title"],
                    "description": item["description"],
                    "snippet": item["content"][:220].strip() + "...",
                    "bm25_score": None
                }
            if fp in meta:
                meta[fp]["vector_rank"] = v_rank
                meta[fp]["distance"] = item["distance"]
            else:
                meta[fp] = {"bm25_rank": None, "vector_rank": v_rank, "distance": item["distance"]}
            v_rank += 1

        sorted_fps = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        pool_fps = sorted_fps[:max(limit * 3, 25)]

        # Backfill embeddings for any BM25-only pool candidates so MMR can evaluate pairwise cosine similarity
        missing_fps = [fp for fp in pool_fps if fp not in emb_map]
        if missing_fps and self.has_vec:
            try:
                placeholders = ",".join("?" * len(missing_fps))
                rows = self.con.execute(
                    f"""
                    SELECT mc.file_path, vm.embedding
                    FROM memory_chunks mc
                    JOIN vec_memory vm ON mc.chunk_id = vm.chunk_id
                    WHERE mc.file_path IN ({placeholders}) AND mc.chunk_index = 0
                    """,
                    missing_fps,
                ).fetchall()
                for r_fp, r_emb in rows:
                    if r_fp not in emb_map and r_emb:
                        emb_map[r_fp] = r_emb
            except Exception:
                pass

        # Greedy Maximal Marginal Relevance (MMR) selection over normalized RRF scores and cosine similarity
        selected_fps: List[str] = []
        if len(pool_fps) <= 1 or not emb_map:
            selected_fps = pool_fps[:limit]
        else:
            try:
                import numpy as np
                vecs: Dict[str, Any] = {}
                for fp, raw_b in emb_map.items():
                    if isinstance(raw_b, (bytes, bytearray, memoryview)):
                        arr = np.frombuffer(raw_b, dtype=np.float32)
                        norm = float(np.linalg.norm(arr))
                        if norm > 0:
                            vecs[fp] = arr / norm

                max_rrf = scores[pool_fps[0]] if pool_fps and scores[pool_fps[0]] > 0 else 1.0
                remaining = list(pool_fps)
                while remaining and len(selected_fps) < limit:
                    if not selected_fps:
                        selected_fps.append(remaining.pop(0))
                        continue
                    best_fp = None
                    best_mmr = -1e9
                    for cand_fp in remaining:
                        rel = scores[cand_fp] / max_rrf
                        c_vec = vecs.get(cand_fp)
                        if c_vec is not None:
                            sims = [
                                float(np.dot(c_vec, vecs[s_fp]))
                                for s_fp in selected_fps
                                if s_fp in vecs
                            ]
                            max_sim = max(sims) if sims else 0.0
                        else:
                            max_sim = 0.0
                        mmr_val = mmr_lambda * rel - (1.0 - mmr_lambda) * max_sim
                        if mmr_val > best_mmr:
                            best_mmr = mmr_val
                            best_fp = cand_fp
                    if best_fp is not None:
                        selected_fps.append(best_fp)
                        remaining.remove(best_fp)
                    else:
                        break
            except Exception:
                selected_fps = pool_fps[:limit]

        fused = []
        for fp in selected_fps:
            entry = dict(items[fp])
            entry["rrf_score"] = round(scores[fp], 6)
            entry["bm25_rank"] = meta[fp]["bm25_rank"]
            entry["vector_rank"] = meta[fp]["vector_rank"]
            entry["vector_distance"] = meta[fp]["distance"]
            if meta[fp]["bm25_rank"] and meta[fp]["vector_rank"]:
                entry["match_type"] = "hybrid"
            elif meta[fp]["bm25_rank"]:
                entry["match_type"] = "exact_lexical"
            else:
                entry["match_type"] = "semantic_vector"
            fused.append(entry)
        return fused

    def _search_transcripts_target(self, fts_query: str, query_bytes: Optional[bytes], limit: int, mode: str, rrf_k: int, candidate_k: int) -> List[dict]:
        cursor = self.con.cursor()
        bm25_matches: List[dict] = []
        vec_matches: List[dict] = []

        if mode in ("hybrid", "fts") and fts_query:
            try:
                q = """
                    SELECT conversation_id, step_index, role, created_at,
                           snippet(transcript_fts, 4, '«b»', '«/b»', '...', 32) as match_content,
                           snippet(transcript_fts, 5, '«b»', '«/b»', '...', 24) as match_tools,
                           bm25(transcript_fts) as rank
                    FROM transcript_fts
                    WHERE transcript_fts MATCH ?
                    ORDER BY rank ASC
                    LIMIT ?
                """
                for row in cursor.execute(q, (fts_query, candidate_k)):
                    bm25_matches.append({
                        "conversation_id": row[0],
                        "step_index": row[1],
                        "role": row[2],
                        "created_at": row[3],
                        "content_snippet": (row[4] or "").replace("«b»", "**").replace("«/b»", "**"),
                        "tools_snippet": (row[5] or "").replace("«b»", "**").replace("«/b»", "**"),
                        "bm25_score": round(row[6], 4)
                    })
            except sqlite3.OperationalError:
                pass

        if mode in ("hybrid", "vector") and query_bytes and self.has_vec:
            try:
                v_rows = cursor.execute(
                    """
                    SELECT chunk_id, distance
                    FROM vec_transcripts
                    WHERE embedding MATCH ? AND k = ?
                    ORDER BY distance ASC
                    """,
                    (query_bytes, candidate_k),
                ).fetchall()
                if v_rows:
                    cids = [r[0] for r in v_rows]
                    placeholders = ",".join("?" * len(cids))
                    chunk_map = {
                        r[0]: r[1:]
                        for r in cursor.execute(
                            f"""
                            SELECT chunk_id, conversation_id, step_index, role, created_at, title, content, tool_calls
                            FROM transcript_chunks
                            WHERE chunk_id IN ({placeholders})
                            """,
                            cids,
                        )
                    }
                    for cid, dist in v_rows:
                        c_row = chunk_map.get(cid)
                        if c_row:
                            vec_matches.append({
                                "chunk_id": cid,
                                "distance": round(dist, 4),
                                "conversation_id": c_row[0],
                                "step_index": c_row[1],
                                "role": c_row[2],
                                "created_at": c_row[3],
                                "title": c_row[4],
                                "content": c_row[5],
                                "tool_calls": c_row[6],
                            })
            except Exception:
                pass

        if mode == "fts":
            for m in bm25_matches:
                m["match_type"] = "exact_lexical"
            return bm25_matches[:limit]
        if mode == "vector":
            out = []
            for v in vec_matches:
                out.append({
                    "conversation_id": v["conversation_id"],
                    "step_index": v["step_index"],
                    "role": v["role"],
                    "created_at": v["created_at"],
                    "content_snippet": v["content"][:220].strip() + "...",
                    "tools_snippet": v["tool_calls"] or "",
                    "distance": v["distance"],
                    "match_type": "semantic_vector"
                })
            return out[:limit]

        # Hybrid RRF Fusion
        return self._fuse_transcripts_rrf(bm25_matches, vec_matches, limit, rrf_k)

    def _fuse_transcripts_rrf(self, bm25_list: List[dict], vec_list: List[dict], limit: int, k: int) -> List[dict]:
        scores: Dict[Tuple[str, int], float] = {}
        items: Dict[Tuple[str, int], dict] = {}
        meta: Dict[Tuple[str, int], dict] = {}

        for rank, item in enumerate(bm25_list, start=1):
            key = (item["conversation_id"], int(item["step_index"]))
            scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))
            items[key] = item
            meta[key] = {"bm25_rank": rank, "vector_rank": None, "distance": None}

        for rank, item in enumerate(vec_list, start=1):
            key = (item["conversation_id"], int(item["step_index"]))
            scores[key] = scores.get(key, 0.0) + (1.0 / (k + rank))
            if key not in items:
                items[key] = {
                    "conversation_id": item["conversation_id"],
                    "step_index": item["step_index"],
                    "role": item["role"],
                    "created_at": item["created_at"],
                    "content_snippet": item["content"][:220].strip() + "...",
                    "tools_snippet": item["tool_calls"] or "",
                    "bm25_score": None
                }
            if key in meta:
                meta[key]["vector_rank"] = rank
                meta[key]["distance"] = item["distance"]
            else:
                meta[key] = {"bm25_rank": None, "vector_rank": rank, "distance": item["distance"]}

        sorted_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        fused = []
        for key in sorted_keys[:limit]:
            entry = dict(items[key])
            entry["rrf_score"] = round(scores[key], 6)
            entry["bm25_rank"] = meta[key]["bm25_rank"]
            entry["vector_rank"] = meta[key]["vector_rank"]
            entry["vector_distance"] = meta[key]["distance"]
            if meta[key]["bm25_rank"] and meta[key]["vector_rank"]:
                entry["match_type"] = "hybrid"
            elif meta[key]["bm25_rank"]:
                entry["match_type"] = "exact_lexical"
            else:
                entry["match_type"] = "semantic_vector"
            fused.append(entry)
        return fused

    def _search_channel_target(self, fts_query: str, query_bytes: Optional[bytes], limit: int, mode: str, rrf_k: int, candidate_k: int) -> List[dict]:
        cursor = self.con.cursor()
        bm25_matches: List[dict] = []
        vec_matches: List[dict] = []

        if mode in ("hybrid", "fts") and fts_query:
            try:
                q = """
                    SELECT message_id, channel_id, channel_name, author, timestamp,
                           snippet(channel_fts, 5, '«b»', '«/b»', '...', 32) as match_content,
                           bm25(channel_fts) as rank
                    FROM channel_fts
                    WHERE channel_fts MATCH ?
                    ORDER BY rank ASC
                    LIMIT ?
                """
                for row in cursor.execute(q, (fts_query, candidate_k)):
                    bm25_matches.append({
                        "message_id": row[0],
                        "channel_id": row[1],
                        "channel_name": row[2],
                        "author": row[3],
                        "timestamp": row[4],
                        "snippet": (row[5] or "").replace("«b»", "**").replace("«/b»", "**"),
                        "bm25_score": round(row[6], 4)
                    })
            except sqlite3.OperationalError:
                pass

        if mode in ("hybrid", "vector") and query_bytes and self.has_vec:
            try:
                v_rows = cursor.execute(
                    """
                    SELECT chunk_id, distance
                    FROM vec_channel
                    WHERE embedding MATCH ? AND k = ?
                    ORDER BY distance ASC
                    """,
                    (query_bytes, candidate_k),
                ).fetchall()
                if v_rows:
                    cids = [r[0] for r in v_rows]
                    placeholders = ",".join("?" * len(cids))
                    chunk_map = {
                        r[0]: r[1:]
                        for r in cursor.execute(
                            f"""
                            SELECT chunk_id, message_id, channel_id, channel_name, author, timestamp, content
                            FROM channel_chunks
                            WHERE chunk_id IN ({placeholders})
                            """,
                            cids,
                        )
                    }
                    for cid, dist in v_rows:
                        c_row = chunk_map.get(cid)
                        if c_row:
                            vec_matches.append({
                                "chunk_id": cid,
                                "distance": round(dist, 4),
                                "message_id": c_row[0],
                                "channel_id": c_row[1],
                                "channel_name": c_row[2],
                                "author": c_row[3],
                                "timestamp": c_row[4],
                                "content": c_row[5],
                            })
            except Exception:
                pass

        if mode == "fts":
            for m in bm25_matches:
                m["match_type"] = "exact_lexical"
            return bm25_matches[:limit]
        if mode == "vector":
            out = []
            for v in vec_matches:
                out.append({
                    "message_id": v["message_id"],
                    "channel_id": v["channel_id"],
                    "channel_name": v["channel_name"],
                    "author": v["author"],
                    "timestamp": v["timestamp"],
                    "snippet": v["content"][:220].strip() + "...",
                    "distance": v["distance"],
                    "match_type": "semantic_vector"
                })
            return out[:limit]

        # Hybrid RRF Fusion
        return self._fuse_channel_rrf(bm25_matches, vec_matches, limit, rrf_k)

    def _fuse_channel_rrf(self, bm25_list: List[dict], vec_list: List[dict], limit: int, k: int) -> List[dict]:
        scores: Dict[str, float] = {}
        items: Dict[str, dict] = {}
        meta: Dict[str, dict] = {}

        for rank, item in enumerate(bm25_list, start=1):
            mid = item["message_id"]
            scores[mid] = scores.get(mid, 0.0) + (1.0 / (k + rank))
            items[mid] = item
            meta[mid] = {"bm25_rank": rank, "vector_rank": None, "distance": None}

        for rank, item in enumerate(vec_list, start=1):
            mid = item["message_id"]
            scores[mid] = scores.get(mid, 0.0) + (1.0 / (k + rank))
            if mid not in items:
                items[mid] = {
                    "message_id": item["message_id"],
                    "channel_id": item["channel_id"],
                    "channel_name": item["channel_name"],
                    "author": item["author"],
                    "timestamp": item["timestamp"],
                    "snippet": item["content"][:220].strip() + "...",
                    "bm25_score": None
                }
            if mid in meta:
                meta[mid]["vector_rank"] = rank
                meta[mid]["distance"] = item["distance"]
            else:
                meta[mid] = {"bm25_rank": None, "vector_rank": rank, "distance": item["distance"]}

        sorted_mids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        fused = []
        for mid in sorted_mids[:limit]:
            entry = dict(items[mid])
            entry["rrf_score"] = round(scores[mid], 6)
            entry["bm25_rank"] = meta[mid]["bm25_rank"]
            entry["vector_rank"] = meta[mid]["vector_rank"]
            entry["vector_distance"] = meta[mid]["distance"]
            if meta[mid]["bm25_rank"] and meta[mid]["vector_rank"]:
                entry["match_type"] = "hybrid"
            elif meta[mid]["bm25_rank"]:
                entry["match_type"] = "exact_lexical"
            else:
                entry["match_type"] = "semantic_vector"
            fused.append(entry)
        return fused

    def _format_query(self, raw: str) -> str:
        clean = raw.strip()
        if not clean:
            return ""
        if any(op in clean for op in (" AND ", " OR ", " NOT ", '"', "*")):
            return clean
        words = clean.split()
        safe_words = [f'"{w}"' for w in words if w.isalnum() or "-" in w or "_" in w]
        if not safe_words:
            return f'"{clean}"'
        return " AND ".join(safe_words)

    def stats(self) -> Dict[str, Any]:
        cursor = self.con.cursor()
        total_convs = cursor.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        total_steps = cursor.execute("SELECT COUNT(*) FROM transcript_fts").fetchone()[0]
        total_memory = cursor.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        total_msgs = cursor.execute("SELECT COUNT(*) FROM channel_fts").fetchone()[0]
        last_sync = cursor.execute("SELECT value FROM meta WHERE key = 'last_sync'").fetchone()

        db_size_mb = 0.0
        if os.path.exists(self.db_path):
            db_size_mb = round(os.path.getsize(self.db_path) / (1024 * 1024), 2)

        mem_chunks = 0
        tr_chunks = 0
        ch_chunks = 0
        try:
            mem_chunks = cursor.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
            tr_chunks = cursor.execute("SELECT COUNT(*) FROM transcript_chunks").fetchone()[0]
            ch_chunks = cursor.execute("SELECT COUNT(*) FROM channel_chunks").fetchone()[0]
        except sqlite3.OperationalError:
            pass

        return {
            "database_path": self.db_path,
            "database_size_mb": db_size_mb,
            "last_sync": last_sync[0] if last_sync else "Never",
            "indexed_conversations": total_convs,
            "indexed_transcript_steps": total_steps,
            "indexed_memory_files": total_memory,
            "indexed_channel_messages": total_msgs,
            "vector_engine": "sqlite-vec (vec0) + FastEmbed ONNX" if self.has_vec else "FTS5 only",
            "vector_model": MODEL_NAME,
            "vector_dimensions": EMBEDDING_DIM,
            "vector_chunks": {
                "memory": mem_chunks,
                "transcripts": tr_chunks,
                "channel": ch_chunks,
                "total": mem_chunks + tr_chunks + ch_chunks
            }
        }


def format_search_results(results: Dict[str, Any]) -> str:
    lines = []
    mode_label = results.get("mode", "hybrid").upper()
    lat = results.get("latency_ms", 0.0)
    lines.append(f"🔍 Search Results for: `{results.get('query')}` [{mode_label} — {lat}ms]\n")

    def badge(mtype: str) -> str:
        if mtype == "hybrid":
            return "⚡ **[HYBRID]**"
        elif mtype == "semantic_vector":
            return "🧠 **[SEMANTIC]**"
        elif mtype == "exact_lexical":
            return "📝 **[EXACT]**"
        return ""

    mem_list = results.get("memory", [])
    if mem_list:
        lines.append(f"### 📄 Memory Files ({len(mem_list)} matches)")
        for m in mem_list:
            b = badge(m.get("match_type", ""))
            score_info = f"*(RRF: {m['rrf_score']:.4f})*" if "rrf_score" in m else f"*(Dist: {m.get('distance', m.get('vector_distance', 'N/A'))})*"
            lines.append(f"- {b} **[{m['title']}](file://{m['file_path']})** (`{m['category']}`) {score_info}")
            if m.get("description"):
                lines.append(f"  *Description:* {m['description']}")
            lines.append(f"  *Snippet:* {m['snippet']}\n")

    tr_list = results.get("transcripts", [])
    if tr_list:
        lines.append(f"### 💬 Session Transcripts ({len(tr_list)} matches)")
        for t in tr_list:
            b = badge(t.get("match_type", ""))
            role_badge = f"**[{t['role'].upper()}]**"
            ts = t.get("created_at", "")[:19].replace("T", " ")
            score_info = f"*(RRF: {t['rrf_score']:.4f})*" if "rrf_score" in t else f"*(Dist: {t.get('distance', t.get('vector_distance', 'N/A'))})*"
            lines.append(f"- {b} {role_badge} Conversation `[{t['conversation_id'][:8]}...](conversation://{t['conversation_id']})` (Step #{t['step_index']}, {ts}) {score_info}:")
            if t.get("content_snippet"):
                lines.append(f"  *Content:* {t['content_snippet']}")
            if t.get("tools_snippet"):
                lines.append(f"  *Tools:* `{t['tools_snippet']}`")
            lines.append("")

    ch_list = results.get("channel", [])
    if ch_list:
        lines.append(f"### 🌐 Discord Channel Messages ({len(ch_list)} matches)")
        for c in ch_list:
            b = badge(c.get("match_type", ""))
            score_info = f"*(RRF: {c['rrf_score']:.4f})*" if "rrf_score" in c else f"*(Dist: {c.get('distance', c.get('vector_distance', 'N/A'))})*"
            lines.append(f"- {b} **{c['author']}** in `#{c['channel_name']}` ({c['timestamp']}) {score_info}:")
            lines.append(f"  *Message:* {c['snippet']}\n")

    if not mem_list and not tr_list and not ch_list:
        lines.append("*(No matching records found)*")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Zero / Antigravity Hybrid FTS5 + Vector Indexer (RRF)")
    subparsers = parser.add_subparsers(dest="command")

    # Index command
    sync_p = subparsers.add_parser("index", help="Sync and build FTS5 and vector index")
    sync_p.add_argument("--force", action="store_true", help="Force reindex all files")
    sync_p.add_argument("--no-embed", action="store_true", help="Skip vector embedding generation (FTS-only sync)")
    sync_p.add_argument("--scope", choices=["all", "memory", "transcripts", "channel"], default="all", help="Target corpus scope")
    sync_p.add_argument("--depth", choices=["recent", "full", "user"], default="recent", help="Transcript embedding depth")

    # Search command
    search_p = subparsers.add_parser("search", help="Search the index via hybrid RRF, FTS5, or vector")
    search_p.add_argument("query", type=str, help="Search query string")
    search_p.add_argument("--target", choices=["all", "transcripts", "memory", "channel"], default="all", help="Target index scope")
    search_p.add_argument("--mode", choices=["hybrid", "fts", "vector"], default="hybrid", help="Search mode (default: hybrid)")
    search_p.add_argument("--limit", type=int, default=10, help="Maximum results per category")
    search_p.add_argument("--rrf-k", type=int, default=DEFAULT_RRF_K, help="RRF smoothing constant (default: 60)")
    search_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # Stats command
    subparsers.add_parser("stats", help="Show index and vector statistics")

    args = parser.parse_args()
    indexer = TranscriptIndexer()

    try:
        if args.command == "index" or args.command is None:
            force = getattr(args, "force", False)
            embed = not getattr(args, "no_embed", False)
            scope = getattr(args, "scope", "all")
            depth = getattr(args, "depth", "recent")
            res = indexer.sync_all(force=force, embed=embed, scope=scope, depth=depth)
            print(json.dumps(res, indent=2))
        elif args.command == "search":
            res = indexer.search(
                args.query,
                target=args.target,
                limit=args.limit,
                mode=args.mode,
                rrf_k=args.rrf_k
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(format_search_results(res))
        elif args.command == "stats":
            res = indexer.stats()
            print(json.dumps(res, indent=2))
        else:
            parser.print_help()
    finally:
        indexer.close()


if __name__ == "__main__":
    main()
