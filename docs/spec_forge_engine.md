# FORGE Technical Specification (`tools/forge.py`)
**F**ederated **O**rchestration, **R**untime & **G**ated **E**xecution

**Status:** Approved for Implementation (2026-10-22)  
**Target Channel:** `#forge` (under Operations category `1544953274363412533`)  
**Merge & Reload Policy:** Auto-merge verified chunks via squash PR; stage `bridge_*.py` core edits atomically and gate only the final `Reload Bridge In-Place` Decision Card.  
**Target Modules:** `tools/forge.py`, `tools/repo_ops.py`, `tools/vault_sync.py`, `tools/bridge_engine.py`, `tools/bridge_runner.py`, `tools/bridge_commands.py`, `tools/bridge_scheduler.py`  
**Database:** `/workspace/data/forge.db` (SQLite WAL mode)

---

## 1. Executive Architecture

FORGE unifies three previously fragmented subsystems into a single SQLite state machine (`/workspace/data/forge.db`):
1. **Lease-Based Turn & Background Runtime:** Replaces the blind 30-minute wall-clock timeout (`turn_max_ceiling_seconds = 1800`, `AGY_PRINT_TIMEOUT = "30m"`), `task_settle.py`, and `detached_runner.py` with an **Activity-Renewed Lease** driven by four ground-truth heartbeat signals.
2. **Multi-Chunk Spec-Driven Build Queue (`#forge`):** Inspired by Aerial's `Vanadis` and 2026 Maker-Checker architectures. Decomposes Tier 2/3 engineering tasks into ordered, isolated TDD chunks (`1 chunk = 1 atomic PR`) executed by **`gemini-3.8-flash-high`** inside isolated `git worktree` sandboxes, tracked via a single live-updating Discord widget in `#forge`.
3. **Deterministic Decisions Framework (Inline Approval Cards):** Backs Discord `Approve | Edit | Dismiss` buttons with persistent SQLite action payloads (`forge_decisions`) so proactive sidecars and human-gated actions execute deterministically in <1s at zero token cost.

---

## 2. Pillar 1: Activity-Renewed Lease & Heartbeat Engine

### 2.1 Retirement of the Wall-Clock Guillotine
- **Deprecated:** `turn_max_ceiling_seconds` (`1800.0`) in `config/runtime_rules.json` and `PRINT_TIMEOUT = "30m"` in `bridge_runner.py` / `bridge_daemons.py`.
- **CLI Safety Backstop:** `AGY_PRINT_TIMEOUT` is raised to `"4h"` strictly as an OS-level orphan-process failsafe.
- **Rolling Progress Lease (`lease_ttl_seconds = 300.0`):** Every active turn holds a 300-second sliding lease. The lease renews to `now + 300s` whenever **any** of the four ground-truth signals advances:
  1. **Stream Delta:** New `stream-json` step or substantive token event on `agy` stdout.
  2. **Process Tree Activity:** Child PIDs under `proc.pid` (`process_probe.py`) showing CPU ticks, active non-stdin wait states, or changing child PIDs.
  3. **SDK Task Log Growth:** File size or `mtime` advancement in `/root/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/tasks/*.log`.
  4. **Subagent Transcript Growth:** File size or `mtime` advancement in active child subagent `transcript.jsonl` files.
- **Runaway Loop Circuit Breaker:** If `AgyStreamParser` observes 3 consecutive identical tool calls returning an identical error signature, the lease is revoked immediately (`LOOP_DETECTED`) instead of burning tokens.

---

## 3. Pillar 2: The FORGE Build Queue & Orchestrator-Worker Contract

### 3.1 Complexity Routing Tiers (When to Use FORGE)
- **Tier 1 (Inline Fast-Path — Current Channel Turn):**
  - Single-file edits, config tweaks, small bug fixes (<50 lines changed), or quick diagnostics expected to finish in **<90 seconds**.
  - *Auto-Promotion Guard:* If an inline shell command or SDK task exceeds 90s, the bridge automatically registers it as a tracked background job in `forge.db` and posts a status card in `#forge`.
- **Tier 2 (Single-Chunk FORGE Build — 1 Atomic PR):**
  - Feature additions, refactors touching 2–4 files, or test suites taking **2 to 15 minutes**.
- **Tier 3 (Multi-Chunk FORGE Pipeline — N Ordered PRs):**
  - New subsystems, cross-module refactors, or multi-repo changes decomposed into $N$ ordered TDD chunks (`1 chunk = 1 PR`).

### 3.2 The Orchestrator Spec Contract (`ForgePlan` JSON)
The Orchestrator (running on **Gemini 4 Argon** in the originating channel) never mutates production files on Tier 2/3 tasks. Instead, it inspects the target interfaces, constructs a validated `ForgePlan` JSON, and enqueues it via `python3 /workspace/tools/forge.py enqueue plan.json`:
- **Pre-Enqueue Validator:** Rejects plans with missing `target_files`, empty `context_fact_sheet`, or missing `verification_cmd` assertions to prevent Spec-to-Code drift.

```json
{
  "title": "Implement Unified FORGE Engine",
  "origin_channel_id": 1544953279664889888,
  "origin_thread_id": null,
  "repo": "/workspace",
  "tier": 3,
  "touches_core_bridge": true,
  "context_fact_sheet": [
    "bridge_engine.py TurnCoordinator.check_watchdog_timeout() currently enforces max_turn_ceiling=1800.0.",
    "All SQLite connections must enable PRAGMA journal_mode=WAL and busy_timeout=5000."
  ],
  "chunks": [
    {
      "chunk_index": 0,
      "title": "Lease Heartbeat Monitor & Timeout Retirement",
      "worker_model": "gemini-3.8-flash-high",
      "target_files": ["tools/bridge_engine.py", "tools/bridge_runner.py", "tools/test_bridge_engine.py"],
      "prohibited_files": ["tools/bridge.py", "tools/bridge_commands.py"],
      "instructions": "1. Replace self.is_hard_ceiling with check_activity_lease()...\n2. Add SDK task log and subagent transcript mtime probes...",
      "verification_cmd": "ruff check tools/bridge_engine.py tools/bridge_runner.py && pytest tools/test_bridge_engine.py"
    }
  ]
}
```

### 3.3 Isolated Git Worktree Execution & Model Escalation
1. **Worktree Sandbox:** When `ForgeWorker` dequeues Chunk $k$, it creates a lightweight isolated worktree:
   `git worktree add -b forge/<build_id>-chunk-<k> /tmp/forge-wt-<chunk_id> <base_branch>`
   This guarantees background builds **never** dirty `/workspace` while interactive turns are running in other channels.
2. **Structured Worker Prompt Hydration:**
   - **Build Header & Fact Sheet:** High-density architectural invariants from the Argon orchestrator.
   - **Completed Chunks Ledger (`0 .. k-1`):** Merged `PR #` and `git diff --stat` from prior chunks.
   - **Active Chunk Contract (`k`):** Target files, step-by-step TDD instructions, and mandatory `verification_cmd`.
   - **Boundary Guard (`k+1 .. N-1`):** Titles of upcoming chunks with hard prohibition against implementing them early.
3. **Model & Self-Correction Ladder:**
   - Default Worker Model: **`gemini-3.8-flash-high`** (configurable per chunk to `gemini-3.8-flash-medium` for simple test/doc chunks).
   - **Maker-Checker Gate:** After the worker exits, `forge.py` deterministically runs `verification_cmd` + `validate_commit_safety.py` in the worktree.
   - If verification fails, FORGE feeds the exact `pytest`/`ruff` traceback back into the worker (up to 2 retries). If it still fails, FORGE escalates once to **`gemini-4-argon-medium`**.

### 3.4 Post-Build Orchestrator Evaluation & Originating Channel Notification
Evaluation operates in two distinct tiers so intermediate chunks run fast without burning Argon quota, while final delivery wakes the originating channel with a verified completion summary:
1. **Intermediate Chunks (`0 .. N-2` — Deterministic Gate Only):**
   - Evaluated strictly by `forge.py` running `verification_cmd` (`ruff` + scoped `pytest` + `validate_commit_safety.py`) and verifying git diff scope (`prohibited_files` untouched).
   - When green, `forge.py` merges the PR, updates the `#forge` widget in place, and immediately starts Chunk $k+1$ without spamming the originating channel.
2. **Final Chunk Completion (`N-1` — Originating Channel Wake & Notification):**
   - Once Chunk $N-1$ merges, `forge.py` **always wakes the Argon Orchestrator in the exact channel or thread where you started the build (`origin_channel_id` / `origin_thread_id` / `origin_conv_id`)** with a structured **FORGE Completion Envelope**:
     - Merged `PR #` list, commit SHAs, and cumulative `git diff --stat`.
     - Test execution summary across all chunks.
     - Original `ForgePlan` objective and `context_fact_sheet`.
   - **Orchestrator Final Review & Delivery to You:** Argon inspects the cumulative diff against the original spec to verify cross-chunk integration, confirms no edge cases were stubbed out by Flash, updates `README.md` / `AGENTS.md` if needed, and **posts a completion notification directly back into the original channel/thread where you requested the work** (attaching the `Reload Bridge In-Place` Decision Card if `touches_core_bridge=true`).

### 3.5 Universal GitHub Discipline & Release-Branch Staging
1. **Standardized PR Lifecycle Across All 5 Repos:**
   - `tools/repo_ops.py` is upgraded as the universal git driver across `/workspace` (`Zero-Agent`), `/workspace/kalshi-quant`, `/workspace/agora-roguelike`, `/workspace/sharkfin`, and `/workspace/market-sandbox`.
   - All Tier 2/3 changes on `Zero-Agent` go through worktree branches, PRs, and squash merges (`repo_ops.py merge`) with `git pull --rebase --autostash` on `/workspace` after merge.
   - `tools/bridge_git_sync.py` remains strictly as a Tier 1 safety net for quick inline config/rule edits on reload.
2. **Preventing `/workspace/` vs. `/app/` Mid-Build Desync:**
   - When `touches_core_bridge=true` (modifying `tools/bridge_*.py`), chunks target a staging branch (`forge/release-<build_id>`) so live `/workspace/tools/` is not half-updated while `/app/bridge.py` runs in memory.
   - When the final chunk passes verification, FORGE squash-merges the release PR into `main`, syncs `/workspace`, copies `tools/bridge_*.py` to `/app/`, and presents the `Reload Bridge In-Place` Decision Card.
3. **Crash & Boot Recovery (`on_ready`):**
   - On bridge startup, `forge_supervisor.reconcile_on_boot()` runs `git worktree prune`, cleans stale `/tmp/forge-wt-*` directories, and resets any interrupted `running` chunks for clean retry.

---

## 4. Pillar 3: Decisions Framework (Inline Action Approval Cards)

### 4.1 Surface Separation
- **`#forge` Channel:** Exclusively displays Build Queue progress widgets and detached background task cards.
- **Domain Channels (`#zero-chat`, `#homelab`, `#home-assistant`, `#finances`, `#zero-ops`):** Receive **Decision Cards** whenever a proactive sidecar or reload gate requires human authorization.

### 4.2 Decision Lifecycle (`forge_decisions`)
1. **Staging (`forge.py stage-decision`):** Sidecars or turns register an action payload (`handler`: `shell` | `send_mail` | `calendar` | `docker_compose` | `ha_service` | `gh_merge` | `bridge_reload`, `args_json`, `channel_id`, `ttl_seconds`).
2. **Discord Component Rendering:** Posts an inline card with persistent buttons:
   - `forge_approve:<decision_id>` (`ButtonStyle.success`)
   - `forge_edit:<decision_id>` (`ButtonStyle.secondary` -> opens `discord.ui.Modal` pre-filled with editable parameters)
   - `forge_dismiss:<decision_id>` (`ButtonStyle.danger`)
3. **Zero-Token Execution:** Clicking **Approve** validates Ryan's Discord ID (`179407724335988736`), executes the handler directly in Python (<1s latency, 0 LLM tokens), edits the card with the execution timestamp (`PT`) + output receipt, and disables the buttons.
4. **Global `/queue` Slash Command:** Summarizes active `#forge` builds alongside any pending Decision Cards across channels (respecting the `#zero-chat` privacy filter when run outside `#zero-chat`).

---

## 5. SQLite Schema (`/workspace/data/forge.db`)

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS forge_builds (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '/workspace',
    tier INTEGER NOT NULL DEFAULT 2,
    touches_core_bridge INTEGER NOT NULL DEFAULT 0,
    origin_channel_id INTEGER NOT NULL,
    origin_thread_id INTEGER,
    origin_conv_id TEXT,
    forge_message_id INTEGER,
    fact_sheet_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'queued', -- queued | running | awaiting_approval | completed | failed | cancelled
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS forge_chunks (
    id TEXT PRIMARY KEY,
    build_id TEXT NOT NULL REFERENCES forge_builds(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    worker_model TEXT NOT NULL DEFAULT 'gemini-3.8-flash-high',
    target_files_json TEXT NOT NULL DEFAULT '[]',
    prohibited_files_json TEXT NOT NULL DEFAULT '[]',
    instructions TEXT NOT NULL,
    verification_cmd TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | running | verifying | awaiting_approval | merged | failed
    attempt_count INTEGER NOT NULL DEFAULT 0,
    pr_number INTEGER,
    branch_name TEXT,
    diff_stat TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    started_at REAL,
    completed_at REAL,
    UNIQUE(build_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS forge_decisions (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL, -- email | calendar | homelab | homeassistant | github | build_gate
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    handler_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    is_private INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | edited_approved | dismissed | expired | failed
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    resolved_at REAL,
    receipt_text TEXT NOT NULL DEFAULT ''
);
```

---

## 6. Deprecation & Upgrade Impact Matrix

- **`tools/task_settle.py` & `data/pending_sdk_tasks.json`:** **Deprecated & Removed.** Replaced by the native SDK task log/message heartbeat probe inside `bridge_engine.py` and `forge.py`.
- **`tools/detached_runner.py` & `data/detached_tasks/`:** **Consolidated.** `detached_runner.py` becomes a lightweight CLI shim that registers single-chunk shell jobs in `forge.db` and renders their status in `#forge`.
- **`tools/bridge_scheduler.py`:** Replaces `check_and_dispatch_completed_tasks()` with `forge_supervisor.tick()` (runs every 10s to advance queued chunks, monitor detached PIDs, and expire stale `forge_decisions` cards).
- **`tools/bridge_git_sync.py`:** Updated so pre-reload git sync ignores `/tmp/forge-wt-*` worktrees and `is_bridge_busy()` checks active `forge_builds`.

---

## 7. Private Companion Repository (`brockventures/Zero-Vault`)

1. **Purpose & Scope (`tools/vault_sync.py`):**
   - Provides full Git version history and off-NAS disaster recovery for high-value state gitignored in public `brockventures/Zero-Agent`:
     - `/workspace/memory/` (`private/`, `vault/`, `public/`, `crab_cavern/`)
     - `/workspace/config/schedule.json` and non-secret local configs
     - `/workspace/tools/promote_memory.py` and `/workspace/tools/pack_shard.py`
2. **Cryptographic Boundary for `/secrets/`:**
   - Raw plaintext credential files (`/secrets/env.json`, `*.pem`, `google_oauth.json`) are **never** committed in plaintext.
   - `vault_sync.py` packs and encrypts `/secrets/` using `age` (or `gpg --symmetric` with key at `/secrets/.vault_master.key`) into an encrypted `secrets/secrets.tar.age` archive before committing to `Zero-Vault`.

---

## 8. `agents.md` (`GEMINI.md` / `AGENTS.md`), Rules & Context Budget Governance

### 8.1 True Platform Limits & Current Headroom
- **Per-File Platform Limit (`24,000` bytes / `24 KB`):** `/workspace/GEMINI.md` and `/workspace/AGENTS.md` both resolve to `/workspace/agents.md` (deduplicated by path at runtime), which currently sits at **`11,778` bytes**—leaving **`12,222` bytes (~51%)** of per-file headroom. *(Note: The ~120-line guideline in `autonomous_github_management.md` applies strictly to bootstrapping lightweight `AGENTS.md` files in sub-repos like `market-sandbox`, not `/workspace/agents.md`).*
- **Aggregate Always-On Rules Budget (`20,000` tokens / `~80 KB`):** Across `/workspace/agents.md` (`11.8 KB`) plus the 10 `always_on` `.agents/rules/*.md` files (`32.4 KB`), our actual always-on footprint is **`44,201` bytes (~`11,050` tokens)**—leaving nearly **`9,000` tokens (~`35 KB`)** of aggregate headroom.

### 8.2 Clean Codification Plan
1. **Direct Updates in `/workspace/agents.md`:**
   - Replace the legacy `detached_runner.py` entries and add a dedicated **FORGE Build Queue, Leases & Decisions Framework** section directly in `agents.md` (well within the `24,000`-byte per-file ceiling).
2. **Updates in `.agents/rules/`:**
   - **`silent_background_execution.md`:** Replace legacy `task_settle` references with the FORGE rolling lease and `#forge` build queue handoff rules.
   - **`autonomous_github_management.md`:** Codify universal `repo_ops.py` PR discipline across all 5 repos and `Zero-Vault` sync rules, and clarify that the ~120-line `AGENTS.md` target applies only to sub-repositories.
3. **On-Demand Skill (`.agents/skills/forge-orchestrator/SKILL.md`):**
   - Full `ForgePlan` JSON templates, CLI invocations (`forge.py enqueue`, `forge.py stage-decision`), and Decision Card handler specs live in `.agents/skills/forge-orchestrator/SKILL.md` (with `detached-task` redirecting to it).
4. **Platform Limit Guard (`tools/test_commit_safety.py`):**
   - Enforces the real Antigravity platform limits (`<= 24,000` bytes per rule file and `<= 20,000` tokens aggregate across `always_on` rules).
