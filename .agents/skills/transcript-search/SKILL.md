---
name: transcript-search
description: >-
  Use this skill whenever answering questions or investigating issues that touch on Ryan's personal context, family, preferences, historical homelab decisions, past debugging sessions, or Crab Cavern agreements. Executes sub-15ms SQLite FTS5 BM25 + FastEmbed dense vector hybrid search (RRF) to pull exact receipts before answering or assuming.
---

# 🔍 Transcript & Memory Hybrid Search Skill (FTS5 + Vector RRF)

The **transcript-search** skill is Zero's primary operational memory engine. It provides sub-15ms hybrid search combining:
1. **Sparse Lexical Search:** SQLite FTS5 (BM25 ranked with porter unicode61 tokenization).
2. **Dense Semantic Vector Search:** Pure ONNX FastEmbed (`BAAI/bge-small-en-v1.5`, 384-dim) via `sqlite-vec`.
3. **Reciprocal Rank Fusion (RRF, $k=60$):** Mathematical rank fusion combining exact keyword hits and semantic meaning without arbitrary score scaling.

Corpus scopes:
- **Durable Memory Files:** Operational and personal markdown documents in `/workspace/memory/` (preferences, architecture, family, scars, rules).
- **Agent Session Transcripts:** Step-by-step turns, tools executed, and model reasoning across historical conversations (`~/.gemini/antigravity-cli/brain/*/`).
- **Discord Message History:** Messages across home and multi-agent channels from `/workspace/data/channel_history.json`.

---

## ⚡ Memory-First Operating Directive

**Zero grounds answers in historical memory by default.**
Whenever Ryan asks a question involving:
- Personal preferences, family context, dietary habits, or household routines
- Homelab hardware, network topology, container configs, or server IP addresses
- Past PR decisions, architecture debates, trade-offs, or Agora game rules
- Past debugging sessions, root causes, or error resolutions

If the immediate prompt context or ambient memory block does not contain the specific facts, **invoke this skill immediately** before answering or guessing.

---

## 🛠️ Usage & Commands

```bash
# 1. Hybrid Search across all sources (Default: BM25 + Vector RRF)
python3 /workspace/tools/transcript_index.py search "<query>"

# 2. Scope search to a specific target
python3 /workspace/tools/transcript_index.py search "<query>" --target memory
python3 /workspace/tools/transcript_index.py search "<query>" --target transcripts
python3 /workspace/tools/transcript_index.py search "<query>" --target channel

# 3. Explicit search modes (hybrid, vector, fts)
python3 /workspace/tools/transcript_index.py search "<query>" --mode hybrid
python3 /workspace/tools/transcript_index.py search "<query>" --mode vector
python3 /workspace/tools/transcript_index.py search "<query>" --mode fts

# 4. Synchronize / incrementally update the index
python3 /workspace/tools/transcript_index.py index

# 5. Long-running full corpus embedding (via Detached Runner)
python3 /workspace/tools/detached_runner.py start \
  --command "python3 /workspace/tools/transcript_index.py index --scope all" \
  --name "Hybrid RRF Corpus Embedding" \
  --channel "zero-chat" \
  --notify-on-success

# 6. View index and vector statistics
python3 /workspace/tools/transcript_index.py stats
```

---

## 💡 Query Syntax & Search Badges

Results return with clear match badges:
* ⚡ **`[HYBRID]`**: Item scored highly in both exact lexical (BM25) and dense semantic vector search.
* 🧠 **`[SEMANTIC]`**: High vector cosine similarity (conceptual match even if keywords differ).
* 📝 **`[EXACT]`**: Exact lexical BM25 match (exact token, commit hash, or error string).

FTS syntax supported in hybrid / FTS modes:
* **Simple Keyword Search:** `python3 /workspace/tools/transcript_index.py search "docker restart"`
* **Exact Phrase Search:** `python3 /workspace/tools/transcript_index.py search '"Container Manager"'`
* **Boolean Operators:** `python3 /workspace/tools/transcript_index.py search "plex AND (transcode OR sqlite)"`
* **Prefix Search:** `python3 /workspace/tools/transcript_index.py search "pretool*"`
