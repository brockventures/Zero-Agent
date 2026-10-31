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

## 1. Complexity Routing Tiers (Execution Loop & Verification Cost Heuristic — Home & Crab Cavern)

Gated strictly on **Execution Loop & Verification Cost** (never raw file count) across both Home operations channels and Crab Cavern channels (`#gotchi`, `#gotchi-chat`, `#side-project`, `#debug-huddle`, `#the-banana-stand`):

- **Tier 1 (Inline Fast-Path — `<= 3 Tool Steps` & `< 45s` Total):** Stay inline ONLY when exact target lines are already known, verification is a fast local lint/unit check (`< 15s`), and the entire edit + test + commit/PR finishes in **2–3 tool calls** (`< 45s`).
- **Tier 2 (Single-Chunk FORGE Build — `1 Atomic PR`, Even 1 File):** Any single-PR feature, bugfix, or firmware change that requires a compiled toolchain (ESP32 PlatformIO C++ builds), hardware/OTA flash, Docker build, CAD render, `> 15s` test/benchmark sweep, writing a new test suite, or iterating across edit-test-fix loops (`4+ tool steps` or `> 45s`).
- **Tier 3 (Multi-Chunk FORGE Pipeline — `N Ordered PRs`):** Cross-module architectures, multi-subsystem features, or contract changes decomposed into ordered TDD chunks (`1 chunk = 1 PR`).
- **Ad-Hoc Background Shell Jobs (`> 45s`):** Long-running non-PR scripts, wireless OTA flash sweeps, migrations, or benchmarks run via `python3 /workspace/tools/forge.py job start --command "..."` (with `tools/detached_runner.py` retained as a backwards-compatible delegation shim).

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

## 4. Staging Inline Decision Cards (`Approve | Edit | Dismiss`) & Two-Stage FORGE Gate

### Two-Stage Analysis -> Approve -> FORGE Build Pattern (`forge_enqueue`)
When a task requires data analysis, calibration, or investigation before modifying multi-file production code (or >2 files / >90s build work):
1. **Stage 1 (Interactive Turn = Analysis & Proposal Only):** Complete the investigation/math inline, write the validated `ForgePlan` JSON to `/workspace/data/forge_plans/<slug>.json`, and stage an inline Decision Card with `--handler "forge_enqueue"` and `--post` so Ryan gets real `Approve | Edit | Dismiss` buttons.
2. **Stage 2 (1-Click Enqueue to `#forge`):** Clicking **Approve** deterministically invokes `enqueue_build()` on that saved `ForgePlan` in <1s with zero LLM tokens, launching isolated `#forge` worktree workers cleanly.

```bash
# Stage a Two-Stage FORGE Build Proposal Card
python3 /workspace/tools/forge.py stage-decision \
  --category "forge" \
  --title "Calibrate Pitcher Stuff+ Weights & Ship Tests" \
  --summary "Completed 5-fold CV calibration (RMSE -4.2%). Tap Approve to enqueue the 2-chunk FORGE build." \
  --handler "forge_enqueue" \
  --payload '{"plan_file": "/workspace/data/forge_plans/pitcher_stuff_plus.json"}' \
  --channel-id 1542081375287640084 \
  --post
```

### Other Proactive Action Cards
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
Supported `--handler` types: `forge_enqueue`, `shell`, `send_mail`, `docker_compose`, `gh_merge`, `bridge_reload`, `file_delete`.
