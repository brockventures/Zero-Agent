---
name: forge-orchestrator
description: >-
  Use this skill whenever orchestrating Tier 2/3 multi-file or multi-PR engineering builds in #forge,
  staging inline Decision Cards (Approve / Edit / Dismiss) for proactive actions, or inspecting the
  FORGE build and approval queue via tools/forge.py.
---

# 🛠️ FORGE Orchestrator & Decisions Framework Skill

**FORGE** (**F**ederated **O**rchestration, **R**untime & **G**ated **E**xecution) is Zero's unified SQLite state machine (`/workspace/data/forge.db`) for multi-chunk spec-driven builds in `#forge` (`1562899941595160657`), activity-renewed turn leases, and zero-token inline Decision Cards.

---

## 1. Complexity Routing Tiers

- **Tier 1 (Inline Fast-Path — `<90s`):** Single-file edits (`<50` lines), config tweaks, and targeted 1-file test runs stay inline in the current turn.
- **Tier 2 (Single-Chunk FORGE Build — `1 Atomic PR`):** Multi-file features or refactors (`2–4` files) taking `2–15` minutes.
- **Tier 3 (Multi-Chunk FORGE Pipeline — `N Ordered PRs`):** Cross-module architectures or new subsystems decomposed into ordered TDD chunks (`1 chunk = 1 PR`).

---

## 2. Enqueueing a FORGE Build (`ForgePlan` JSON)

1. Write a validated `ForgePlan` JSON file to `scratch/forge_plan.json`:
```json
{
  "title": "Short descriptive build title",
  "origin_channel_id": 1544953279664889888,
  "origin_thread_id": null,
  "repo": "/workspace",
  "tier": 2,
  "touches_core_bridge": false,
  "context_fact_sheet": [
    "Exact function signature or architectural invariant 1",
    "Exact schema or module constraint 2"
  ],
  "chunks": [
    {
      "chunk_index": 0,
      "title": "Atomic Chunk Title",
      "worker_model": "gemini-3.8-flash-high",
      "target_files": ["tools/example.py", "tools/test_example.py"],
      "prohibited_files": ["tools/bridge.py"],
      "instructions": "Step-by-step TDD implementation instructions...",
      "verification_cmd": "ruff check tools/example.py && pytest tools/test_example.py"
    }
  ]
}
```
2. Enqueue the plan:
```bash
python3 /workspace/tools/forge.py enqueue scratch/forge_plan.json
```

---

## 3. Staging Inline Decision Cards (`Approve | Edit | Dismiss`)

Stage deterministic approval cards for proactive sidecars or human gates:
```bash
python3 /workspace/tools/forge.py stage-decision \
  --category "homelab" \
  --title "Restart wedged container" \
  --summary "Container health check failed 3 times. Tap Approve to run scoped compose up." \
  --handler "docker_compose" \
  --payload '{"service": "homeassistant", "action": "up -d", "compose_dir": "/docker"}' \
  --channel-id 1544955535722545253 \
  --post
```
Supported `--handler` types: `shell`, `send_mail`, `docker_compose`, `gh_merge`, `bridge_reload`.
