#!/usr/bin/env python3
"""SQLite FTS5 Transcript, Memory, and Channel History Indexer for Zero / Antigravity.

Provides sub-millisecond full-text search across:
1. Agent session transcripts (~/.gemini/antigravity-cli/brain/*/transcript.jsonl)
2. Long-term memory markdown files (/workspace/memory/**/*.md)
3. Discord channel & thread message history (/workspace/data/channel_history.json)
"""

import os
import sys
import glob
import json
import time
import sqlite3
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional

DB_PATH = os.environ.get("TRANSCRIPT_INDEX_DB", "/workspace/data/transcript_index.db")
BRAIN_DIR = os.environ.get("GEMINI_BRAIN_DIR", "/root/.gemini/antigravity-cli/brain")
MEMORY_DIR = os.environ.get("WORKSPACE_MEMORY_DIR", "/workspace/memory")
CHANNEL_HISTORY_PATH = os.environ.get("CHANNEL_HISTORY_PATH", "/workspace/data/channel_history.json")


def get_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA foreign_keys=OFF;")
    return con


def init_schema(con: sqlite3.Connection):
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


def extract_frontmatter(content: str) -> tuple[dict, str]:
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
        self.con = get_db(db_path)
        init_schema(self.con)

    def close(self):
        self.con.close()

    def sync_all(self, force: bool = False) -> Dict[str, Any]:
        t0 = time.time()
        stats = {
            "transcripts_indexed": 0,
            "transcripts_skipped": 0,
            "transcript_steps": 0,
            "memory_files_indexed": 0,
            "memory_files_skipped": 0,
            "channel_messages_indexed": 0,
            "duration_s": 0.0,
        }

        # 1. Sync Transcripts
        self._sync_transcripts(force, stats)

        # 2. Sync Memory Files
        self._sync_memory(force, stats)

        # 3. Sync Channel History
        self._sync_channel_history(force, stats)

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

    def _sync_transcripts(self, force: bool, stats: dict):
        if not os.path.exists(BRAIN_DIR):
            return

        cursor = self.con.cursor()
        indexed_map = {}
        for row in cursor.execute("SELECT path, mtime, size FROM indexed_files WHERE path LIKE '%transcript.jsonl'"):
            indexed_map[row[0]] = (row[1], row[2])

        conv_dirs = [d for d in os.listdir(BRAIN_DIR) if os.path.isdir(os.path.join(BRAIN_DIR, d))]
        
        for conv_id in conv_dirs:
            t_path = os.path.join(BRAIN_DIR, conv_id, ".system_generated", "logs", "transcript.jsonl")
            if not os.path.isfile(t_path):
                continue

            try:
                st = os.stat(t_path)
                mtime, size = st.st_mtime, st.st_size
            except Exception:
                continue

            prev = indexed_map.get(t_path)
            if not force and prev and prev[0] == mtime and prev[1] == size:
                stats["transcripts_skipped"] += 1
                continue

            # Read and parse JSONL
            steps_data = []
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
                    elif s_type == "PLANNER_RESPONSE" or s_source == "MODEL":
                        role = "assistant"
                    elif s_type in ("TOOL_OUTPUT", "GENERIC") or s_source == "SYSTEM":
                        role = "tool" if s_type == "TOOL_OUTPUT" else "system"
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

            # Database write
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

                self.con.execute("""
                    INSERT OR REPLACE INTO indexed_files (path, mtime, size, item_count, indexed_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                """, (t_path, mtime, size, len(steps_data)))

            stats["transcripts_indexed"] += 1
            stats["transcript_steps"] += len(steps_data)

    def _sync_memory(self, force: bool, stats: dict):
        if not os.path.exists(MEMORY_DIR):
            return

        cursor = self.con.cursor()
        indexed_map = {}
        for row in cursor.execute("SELECT path, mtime, size FROM indexed_files WHERE path LIKE '%.md'"):
            indexed_map[row[0]] = (row[1], row[2])

        md_files = glob.glob(os.path.join(MEMORY_DIR, "**/*.md"), recursive=True)

        for m_path in md_files:
            try:
                st = os.stat(m_path)
                mtime, size = st.st_mtime, st.st_size
            except Exception:
                continue

            prev = indexed_map.get(m_path)
            if not force and prev and prev[0] == mtime and prev[1] == size:
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

                self.con.execute("""
                    INSERT OR REPLACE INTO indexed_files (path, mtime, size, item_count, indexed_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                """, (m_path, mtime, size, 1))

            stats["memory_files_indexed"] += 1

    def _sync_channel_history(self, force: bool, stats: dict):
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
            return

        try:
            with open(CHANNEL_HISTORY_PATH, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
        except Exception:
            return

        messages = []
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

        with self.con:
            self.con.execute("DELETE FROM channel_fts;")
            self.con.executemany("""
                INSERT INTO channel_fts (message_id, channel_id, channel_name, author, timestamp, content)
                VALUES (?, ?, ?, ?, ?, ?)
            """, messages)

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
                stats["transcripts_pruned"] += 1

        # 2. Check memory files
        cursor.execute("SELECT path FROM indexed_files WHERE path LIKE '%.md'")
        for (p,) in cursor.fetchall():
            if not os.path.exists(p):
                with self.con:
                    self.con.execute("DELETE FROM indexed_files WHERE path = ?", (p,))
                    self.con.execute("DELETE FROM memory_fts WHERE file_path = ?", (p,))
                stats["memory_pruned"] += 1

        return stats

    def search(self, query: str, target: str = "all", limit: int = 10) -> Dict[str, Any]:
        results = {
            "query": query,
            "target": target,
            "transcripts": [],
            "memory": [],
            "channel": []
        }

        # Format FTS5 query to handle simple keywords vs complex boolean
        fts_query = self._format_query(query)
        if not fts_query:
            return results

        cursor = self.con.cursor()

        if target in ("all", "transcripts"):
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
                for row in cursor.execute(q, (fts_query, limit)):
                    results["transcripts"].append({
                        "conversation_id": row[0],
                        "step_index": row[1],
                        "role": row[2],
                        "created_at": row[3],
                        "content_snippet": (row[4] or "").replace("«b»", "**").replace("«/b»", "**"),
                        "tools_snippet": (row[5] or "").replace("«b»", "**").replace("«/b»", "**"),
                        "score": round(row[6], 4)
                    })
            except sqlite3.OperationalError as e:
                results["transcript_error"] = str(e)

        if target in ("all", "memory"):
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
                for row in cursor.execute(q, (fts_query, limit)):
                    results["memory"].append({
                        "file_path": row[0],
                        "file_name": row[1],
                        "category": row[2],
                        "title": row[3],
                        "description": row[4],
                        "snippet": (row[5] or "").replace("«b»", "**").replace("«/b»", "**"),
                        "score": round(row[6], 4)
                    })
            except sqlite3.OperationalError as e:
                results["memory_error"] = str(e)

        if target in ("all", "channel"):
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
                for row in cursor.execute(q, (fts_query, limit)):
                    results["channel"].append({
                        "message_id": row[0],
                        "channel_id": row[1],
                        "channel_name": row[2],
                        "author": row[3],
                        "timestamp": row[4],
                        "snippet": (row[5] or "").replace("«b»", "**").replace("«/b»", "**"),
                        "score": round(row[6], 4)
                    })
            except sqlite3.OperationalError as e:
                results["channel_error"] = str(e)

        return results

    def _format_query(self, raw: str) -> str:
        clean = raw.strip()
        if not clean:
            return ""
        # If user used raw FTS syntax (AND, OR, NOT, quotes), pass as is
        if any(op in clean for op in (" AND ", " OR ", " NOT ", '"', "*")):
            return clean
        
        # Otherwise tokenize words and join with implicit AND
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

        return {
            "database_path": self.db_path,
            "database_size_mb": db_size_mb,
            "last_sync": last_sync[0] if last_sync else "Never",
            "indexed_conversations": total_convs,
            "indexed_transcript_steps": total_steps,
            "indexed_memory_files": total_memory,
            "indexed_channel_messages": total_msgs
        }


def format_search_results(results: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"🔍 Search Results for: `{results.get('query')}`\n")

    mem_list = results.get("memory", [])
    if mem_list:
        lines.append(f"### 📄 Memory Files ({len(mem_list)} matches)")
        for m in mem_list:
            lines.append(f"- **[{m['title']}](file://{m['file_path']})** (`{m['category']}`)")
            if m.get("description"):
                lines.append(f"  *Description:* {m['description']}")
            lines.append(f"  *Snippet:* {m['snippet']}\n")

    tr_list = results.get("transcripts", [])
    if tr_list:
        lines.append(f"### 💬 Session Transcripts ({len(tr_list)} matches)")
        for t in tr_list:
            role_badge = f"**[{t['role'].upper()}]**"
            ts = t.get("created_at", "")[:19].replace("T", " ")
            lines.append(f"- {role_badge} Conversation `[{t['conversation_id'][:8]}...](conversation://{t['conversation_id']})` (Step #{t['step_index']}, {ts}):")
            if t.get("content_snippet"):
                lines.append(f"  *Content:* {t['content_snippet']}")
            if t.get("tools_snippet"):
                lines.append(f"  *Tools:* `{t['tools_snippet']}`")
            lines.append("")

    ch_list = results.get("channel", [])
    if ch_list:
        lines.append(f"### 🌐 Discord Channel Messages ({len(ch_list)} matches)")
        for c in ch_list:
            lines.append(f"- **{c['author']}** in `#{c['channel_name']}` ({c['timestamp']}):")
            lines.append(f"  *Message:* {c['snippet']}\n")

    if not mem_list and not tr_list and not ch_list:
        lines.append("*(No matching records found)*")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Zero / Antigravity Transcript & Memory FTS5 Indexer")
    subparsers = parser.add_subparsers(dest="command")

    # Sync / Index command
    sync_p = subparsers.add_parser("index", help="Sync and build FTS5 index")
    sync_p.add_argument("--force", action="store_true", help="Force reindex all files")

    # Search command
    search_p = subparsers.add_parser("search", help="Search the index")
    search_p.add_argument("query", type=str, help="Search query string")
    search_p.add_argument("--target", choices=["all", "transcripts", "memory", "channel"], default="all", help="Target index scope")
    search_p.add_argument("--limit", type=int, default=10, help="Maximum results per category")
    search_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # Stats command
    subparsers.add_parser("stats", help="Show index statistics")

    args = parser.parse_args()
    indexer = TranscriptIndexer()

    try:
        if args.command == "index" or args.command is None:
            force = getattr(args, "force", False)
            res = indexer.sync_all(force=force)
            print(json.dumps(res, indent=2))
        elif args.command == "search":
            res = indexer.search(args.query, target=args.target, limit=args.limit)
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
