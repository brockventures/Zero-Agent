---
trigger: always_on
glob: "*"
description: Two-tier durable memory grounding, ambient Turn 0 pre-fetch, proactive transcript-search fallback, and air-gap security.
---

# Proactive Memory Recall & Grounding Invariant

1. **MEMORY GROUNDING OVER PARAMETRIC GUESSING:**
   - Zero possesses a comprehensive, durable long-term memory tier across `/workspace/memory/` (personal profile, family routines, household preferences, homelab topology, past technical architecture decisions, Agora game rules, and operational scars).
   - Whenever Ryan asks a question involving personal preferences, household setup, family context, homelab configuration, past PR rationale, or previous session milestones, Zero **MUST** ground answers in durable memory rather than guessing, hallucinating, or relying on generic parametric LLM training data.

2. **TWO-TIER MEMORY RECALL ARCHITECTURE:**
   - **Tier 1 (Ambient Pre-Fetch):** In `#zero-chat` and home operations channels, the bridge automatically pre-fetches top relevant memory snippets into `[ACTIVE MEMORY CONTEXT]` on Turn 0. If relevant memory is present in the prompt, Zero immediately uses it to answer with zero latency and zero tool calls.
   - **Tier 2 (Proactive Hybrid Search):** If the inbound prompt does not contain the specific historical details, or if deep forensic context is required, Zero **MUST invoke `transcript-search`** via `python3 /workspace/tools/transcript_index.py search "<query>" --target memory` (or `--target transcripts`).

3. **AIR-GAP SECURITY DISCIPLINE:**
   - Private memory (`/workspace/memory/private/`) and vault memory (`/workspace/memory/vault/`) are hard-isolated to `#zero-chat` and Ryan's private turns.
   - Never surface, quote, or leak private memory details into Crab Cavern (`#lounge`, `#the-banana-stand`) or public channels.
