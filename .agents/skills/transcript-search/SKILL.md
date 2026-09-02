---
name: transcript-search
description: >-
  Use this skill whenever searching historical agent session transcripts, durable memory documents (/workspace/memory/), or Discord channel history.
  Executes fast SQLite FTS5 BM25-ranked full-text search to recall past debugging steps, decisions, tool parameters, or past user discussions.
---

# 🔍 Transcript & Memory FTS5 Search Skill

The **transcript-search** skill provides sub-millisecond full-text search (BM25 ranked) across:
1. **Agent Session Transcripts:** Step-by-step turns, tools executed, and model reasoning across all historical conversations (`~/.gemini/antigravity-cli/brain/*/`).
2. **Durable Memory Files:** All 200+ curated operational and personal markdown files in `/workspace/memory/`.
3. **Discord Message History:** Messages across home and multi-agent channels from `/workspace/data/channel_history.json`.

---

## 🛠️ Usage & Commands

```bash
# 1. Search across all sources (transcripts, memory, Discord)
python3 /workspace/tools/transcript_index.py search "<query>"

# 2. Scope search to a specific target
python3 /workspace/tools/transcript_index.py search "<query>" --target memory
python3 /workspace/tools/transcript_index.py search "<query>" --target transcripts
python3 /workspace/tools/transcript_index.py search "<query>" --target channel

# 3. Synchronize / incrementally update the index
python3 /workspace/tools/transcript_index.py index

# 4. View index statistics
python3 /workspace/tools/transcript_index.py stats
```

---

## 💡 Query Syntax Tips

* **Simple Keyword Search:** `python3 /workspace/tools/transcript_index.py search "docker restart"`
* **Exact Phrase Search:** `python3 /workspace/tools/transcript_index.py search '"Container Manager"'`
* **Boolean Operators:** `python3 /workspace/tools/transcript_index.py search "plex AND (transcode OR sqlite)"`
* **Prefix Search:** `python3 /workspace/tools/transcript_index.py search "pretool*"`
