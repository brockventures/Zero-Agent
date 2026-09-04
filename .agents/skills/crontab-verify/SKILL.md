---
name: crontab-verify
description: >-
  Use this skill whenever creating, modifying, or auditing recurring cron schedules, sidecars, or scheduled maintenance tasks.
  Enforces the 5-layer sidecar lifecycle, validates JSON schedule configurations, checks script existence, detects minute-level resource contention, and audits Discord command hooks.
---

# ⏱️ Karakos Sidecar Lifecycle & Crontab Auditor

The **Crontab Verify** skill ensures that scheduled sidecars, recurring maintenance scripts, and crontab entries run safely without resource starvation, database locking collisions, or silent execution drops.

---

## 🎯 When to Activate This Skill
* **Creating a New Sidecar or Scheduled Job:** Follow the mandatory 5-layer lifecycle protocol before claiming work is complete.
* **Modifying an Existing Schedule:** Any edits to [`data/schedule.json`](file:///workspace/data/schedule.json), [`tools/sidecars.py`](file:///workspace/tools/sidecars.py), or crontabs.
* **Investigating Missed or Dropped Executions:** Forensic debugging when a scheduled task fails to fire or post to Discord.
* **Auditing Resource Contention:** Checking for concurrent high-load scripts (e.g. SQLite memory consolidation, Plex sweeps, or NAS log reviews colliding on the same minute).

---

## 🏗️ The 5-Layer Sidecar Lifecycle Protocol

In this architecture, **writing a standalone `.py` script is only Layer 1**. A production sidecar is incomplete and will fail to run unless all 5 layers are satisfied:

| Layer | File / Location | Responsibility & Failure Mode If Omitted |
| :--- | :--- | :--- |
| **1. Standalone Script** | `/workspace/tools/<script>.py` | Fast, deterministic script with CLI flags (`--test`, `--quiet`), explicit timeout bounds, and clean exit codes. |
| **2. Unified Wrapper** | `/workspace/tools/sidecars.py` | Registered in `run_sidecar_job()` and CLI `action` dispatcher. Ensures execution duration, exit status (`ok`/`warning`/`error`), and outputs are durably logged to `sidecar_status.json`. |
| **3. Scheduler Registration** | `/workspace/data/schedule.json` | Registered in persistent Karakos schedule with valid schema (`id`, `name`, `schedule_type`, `hour_pt`, `minute_pt`, `prompt`). **Missing this means the scheduler will never fire the job.** |
| **4. On-Demand Discord Hook** | `/workspace/tools/bridge_handlers.py` | Mapped in `triggers` (`!<action>` and `/<action>`). Enables Ryan or peers to test or trigger the job on-demand in `#zero-chat`. |
| **5. Contention & Alignment** | `schedule.json` & `sidecar_audit.py` | Pacific Time (`hour_pt`/`minute_pt`) alignment and collision check. Stagger jobs by at least 15 minutes to prevent queue starvation and SQLite lock contention. |

> [!CAUTION]
> **Never invoke the runtime `schedule` tool during Discord bridge turns.** The builtin `schedule` tool spawns a local background cron in the CLI runtime (`agy`), keeping stdout open and causing `bridge_runner.py` to hang on `proc.wait()` until hitting the 30-minute timeout (`PRINT_TIMEOUT=30m`). Always register recurring sidecars in `schedule.json` (Layer 3).

---

## 🛠️ Automated Verification Tools

Always execute these automated verification commands:

```bash
# 1. Audit all registered sidecars against disk, wrappers, and triggers
python3 /workspace/tools/sidecar_audit.py audit

# 2. View 24-hour visual schedule matrix to identify timing collisions
python3 /workspace/tools/sidecar_audit.py matrix

# 3. Test-run a specific sidecar with a bounded timeout
python3 /workspace/tools/sidecar_audit.py test <job_id>

# 4. View formatted upcoming schedule
python3 /workspace/tools/scheduler_tool.py summary
```

---

## 🔍 Pre-Completion Verification Checklist

Before reporting that a sidecar is created or scheduled, verify each step:

1. **Schema Correctness:** Valid `id` slug, explicit `schedule_type` (`daily`, `interval`, `weekly`, `monthly`), and valid timing fields.
2. **Path & Permissions:** Target script path exists in `/workspace/tools/` and is executable.
3. **Catchup Policy:** If `catchup_if_missed` is `true`, a reasonable `catchup_window_seconds` (e.g. 7200s or 14400s) must be specified.
4. **Timezone Awareness:** All schedules use Pacific Time (`America/Los_Angeles`).
5. **No DB / I/O Collisions:** Do not schedule SQLite-modifying tasks (`dreaming`, `memory_doctor`, `session_rollover`) or NAS sweeps at the same Pacific minute.
6. **Deterministic Verification:** `python3 /workspace/tools/sidecar_audit.py audit` reports **0 Critical Failures**.
