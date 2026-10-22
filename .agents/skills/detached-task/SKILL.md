---
name: detached-task
description: >-
  Use this skill whenever executing, monitoring, or managing ad-hoc commands, scripts, builds, or migrations expected to take longer than 90 seconds.
  Spawns jobs completely detached from the active Discord bridge turn via FORGE's native job runner (`forge.py job`), tails logs to disk, and delivers completion notifications asynchronously via tools/outbox.py.
---

# ⚡ Detached Task Skill (FORGE Native Background Job Runner)

The **Detached Task** skill prevents Discord turn lockups, typing indicator spam, and bridge timeouts by decoupling long-running ad-hoc commands (`>90s`) into autonomous background processes managed natively by **FORGE** (`tools/forge.py job`, with `tools/detached_runner.py` maintained as a 100% backwards-compatible delegation shim).

---

## 🎯 The 90-Second Invariant & Trigger Gate

Whenever an ad-hoc command, script, test suite, or migration is expected or estimated to take **longer than 90 seconds**:

1. **NEVER execute synchronously** within the conversational Discord turn.
2. **NEVER hold the turn open** with standard `run_command` polling.
3. **MANDATORY DECOUPLING:** Launch the job via `python3 /workspace/tools/forge.py job start` (or `python3 /workspace/tools/detached_runner.py start`).
4. **SILENT EXECUTION (ZERO INTERSTITIAL CHATTER):** Do NOT output intermediate task IDs, process PIDs, or placeholder messages to chat ('Starting task...', 'Running in background...'). Conclude or proceed silently. The runner delivers strictly the final result upon completion.
5. **ASYNCHRONOUS OUTBOX DELIVERY:** Every job is tracked in `forge.db` (`forge_jobs` table, visible in `/queue`) and wakes the originating channel via `tools/outbox.py` upon completion or failure.

---

## 🛠️ CLI Quick Reference (`tools/forge.py job`)

### 1. Launch a Detached Job
```bash
python3 /workspace/tools/forge.py job start \
  --command "python3 /workspace/tools/heavy_script.py --all" \
  --channel "zero-chat" \
  --name "Heavy Data Ingestion" \
  --timeout 7200
```
*(Legacy alias `python3 /workspace/tools/detached_runner.py start ...` accepts identical flags and delegates directly to `forge.py`).*

### 2. Check Job Status
```bash
python3 /workspace/tools/forge.py job status <task_id>
```

### 3. List Recent Background Jobs
```bash
python3 /workspace/tools/forge.py job list --limit 10
```

### 4. Tail Execution Logs
```bash
python3 /workspace/tools/forge.py job logs <task_id> --lines 50
```

### 5. Cancel / Abort a Running Job
```bash
python3 /workspace/tools/forge.py job cancel <task_id>
```
*Sends `SIGTERM` (and `SIGKILL` if needed) to the detached process group.*

---

## 🐍 Python API Reference

```python
from tools.forge import start_job, get_job_status, cancel_job, list_jobs

# Launch detached job natively via FORGE
res = start_job(
    command="npm run build:prod",
    channel="zero-chat",
    name="Production Webpack Build",
    timeout=1800,
    cwd="/workspace/web",
)
task_id = res["task_id"]
```
*(Importing `start_task`, `get_task_status`, `cancel_task`, `list_tasks` from `tools.detached_runner` remains fully supported via the delegation shim.)*

---

## ⚠️ Distinction from Related Systems

* **`forge-orchestrator` (`forge.py enqueue`):** Used for Tier 2/3 multi-file or multi-PR code builds executed by isolated `git worktree` agent workers (`gemini-3.8-flash-high`).
* **`crontab-verify` (`schedule.json`):** Used for **recurring** cron/interval maintenance (e.g. daily backups, hourly watchdogs).
* **`remote-transfer`:** Specialized for multi-megabyte/gigabyte SSH/rsync file migrations with hardware bandwidth governors (`--bwlimit`) and `systemd-inhibit`.
* **`detached-task` (`forge.py job`):** Used for **one-off, ad-hoc shell commands** that take `>90s` (e.g. database migrations, media transcoding, large data exports).
