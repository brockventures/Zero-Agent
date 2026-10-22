---
name: forge-orchestrator
description: >-
  Use this skill whenever orchestrating Tier 2/3 multi-file or multi-PR engineering builds in #forge,
  launching detached background shell jobs (`forge.py job start`), staging inline Decision Cards
  (Approve / Edit / Dismiss) for proactive actions, or inspecting the FORGE build and approval queue via tools/forge.py.
---

# 🛠️ FORGE Orchestrator & Decisions Framework Skill (v1.1)

**FORGE** (**F**ederated **O**rchestration, **R**untime & **G**ated **E**xecution) is Zero's unified SQLite state machine (`/workspace/data/forge.db`) for multi-chunk spec-driven builds in `#forge` (`1562899941595160657`), native background shell jobs (`forge_jobs`), activity-renewed turn leases, and zero-token inline Decision Cards.

---

## 1. Complexity Routing Tiers

- **Tier 1 (Inline Fast-Path — `<90s`):** Single-file edits (`<50` lines), config tweaks, and targeted 1-file test runs stay inline in the current turn.
- **Tier 2 (Single-Chunk FORGE Build — `1 Atomic PR`):** Multi-file features or refactors (`2–4` files) taking `2–15` minutes.
- **Tier 3 (Multi-Chunk FORGE Pipeline — `N Ordered PRs`):** Cross-module architectures or new subsystems decomposed into ordered TDD chunks (`1 chunk = 1 PR`).
- **Ad-Hoc Background Shell Jobs (`>90s`):** Long-running scripts, migrations, or sweeps run via `python3 /workspace/tools/forge.py job start --command "..."` (with `tools/detached_runner.py` retained as a backwards-compatible delegation shim).

---

## 2. Enqueueing a FORGE v1.1 Build (`ForgePlan` JSON)

1. Write a validated `ForgePlan` JSON file to `scratch/forge_plan.json` (incorporating Aerial's `Vanadis` symbol pre-hydration, Amos's target-file overlap guard, and Alex's optional `verification_cmd` fallback):
```json
{
  "title": "Short descriptive build title",
  "objective": "High-level why/goal so workers understand intent beyond mechanical steps",
  "origin_channel_id": 1544953279664889888,
  "origin_thread_id": null,
  "repo": "/workspace",
  "tier": 2,
  "touches_core_bridge": false,
  "context_fact_sheet": [
    "Exact function signature or architectural invariant 1",
    "Exact schema or module constraint 2"
  ],
  "shared_context": [
    "Shared exemplar or cross-chunk protocol note"
  ],
  "chunks": [
    {
      "chunk_index": 0,
      "title": "Atomic Chunk Title",
      "repo": "/workspace",
      "worker_model": "gemini-3.8-flash-high",
      "target_files": ["tools/example.py", "tools/test_example.py"],
      "target_symbols": ["tools/example.py:ExampleRunner", "helper_fn"],
      "prohibited_files": ["tools/bridge.py"],
      "shared_context": ["Chunk-specific exemplar"],
      "instructions": "Step-by-step TDD implementation instructions...",
      "verification_cmd": "ruff check tools/example.py && pytest tools/test_example.py"
    }
  ]
}
```
- **Turn-0 Symbol Pre-Hydration (`target_symbols`):** `forge.py` extracts exact AST/line-slice code blocks for listed classes/functions and embeds them directly into the worker prompt so Flash skips file-discovery roundtrips.
- **Optional `verification_cmd`:** If omitted, `forge.py` automatically synthesizes a baseline `py_compile` + matching `pytest` verification command from `target_files`.
- **Target-File Overlap Guard:** `enqueue_build()` automatically blocks concurrent builds whose `target_files` overlap an active `queued` or `running` build (override with `--allow-overlap`).

2. Enqueue the plan:
```bash
python3 /workspace/tools/forge.py enqueue scratch/forge_plan.json
```

---

## 3. Native Detached Job Runner (`forge.py job`)

```bash
# Start a detached background job (>90s)
python3 /workspace/tools/forge.py job start \
  --command "python3 /workspace/tools/heavy_script.py" \
  --channel "zero-chat" \
  --name "Heavy Data Sweep" \
  --timeout 3600

# Inspect status, list jobs, tail logs, or cancel
python3 /workspace/tools/forge.py job status <task_id>
python3 /workspace/tools/forge.py job list --limit 10
python3 /workspace/tools/forge.py job logs <task_id> --lines 50
python3 /workspace/tools/forge.py job cancel <task_id>
```

---

## 4. Staging Inline Decision Cards (`Approve | Edit | Dismiss`)

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
