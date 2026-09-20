---
name: detached-task
description: >-
  Use this skill whenever executing, monitoring, or managing ad-hoc commands, scripts, builds, or migrations expected to take longer than 3 minutes (180 seconds).
  Spawns jobs completely detached from the active Discord bridge turn, tails logs to disk, and delivers completion notifications asynchronously via tools/outbox.py.
---

# ⚡ Detached Task Skill (Asynchronous Non-Blocking Execution)

The **Detached Task** skill prevents Discord turn lockups, typing indicator spam, and bridge watchdog timeouts by decoupling long-running ad-hoc commands into autonomous background processes.

---

## 🎯 The 3-Minute Invariant & Trigger Gate

Whenever an ad-hoc command, script, test suite, or migration is expected or estimated to take **longer than 3 minutes (180 seconds)**:

1. **NEVER execute synchronously** within the conversational Discord turn.
2. **NEVER hold the turn open** with standard `run_command` polling.
3. **MANDATORY DECOUPLING:** Launch the job via `/workspace/tools/detached_runner.py start`.
4. **IMMEDIATE DISCORD REPLY:** Output the task ID, process PID, and log path to the user, then conclude the turn immediately so the channel remains unblocked.
5. **ASYNCHRONOUS OUTBOX DELIVERY:** The detached runner monitors the child process to completion, logs stdout/stderr to disk, and automatically queues an outbox notification with execution status, elapsed duration, exit code, and a tail log snippet upon finish.

---

## 🛠️ CLI Quick Reference (`tools/detached_runner.py`)

### 1. Launch a Detached Task
```bash
python3 /workspace/tools/detached_runner.py start \
  --command "python3 /workspace/tools/heavy_script.py --all" \
  --channel "zero-chat" \
  --name "Heavy Data Ingestion" \
  --timeout 7200
```
*Returns immediately with JSON containing `task_id`, `worker_pid`, `child_pid`, and `log_file`.*

### 2. Check Task Status
```bash
python3 /workspace/tools/detached_runner.py status <task_id>
```

### 3. List Recent Background Tasks
```bash
python3 /workspace/tools/detached_runner.py list --limit 10
```

### 4. Tail Execution Logs
```bash
python3 /workspace/tools/detached_runner.py logs <task_id> --lines 50
```

### 5. Cancel / Abort a Running Task
```bash
python3 /workspace/tools/detached_runner.py cancel <task_id>
```
*Sends `SIGTERM` (and `SIGKILL` if needed) to the detached process group.*

---

## 🐍 Python API Reference

```python
from tools.detached_runner import start_task, get_task_status, cancel_task

# Launch detached task
res = start_task(
    command="npm run build:prod",
    channel="zero-chat",
    name="Production Webpack Build",
    timeout=1800,
    cwd="/workspace/web"
)
task_id = res["task_id"]
```

---

## 🏗️ Architecture & Lifecycle Topology

```text
[Discord Channel] (User Request)
       │
       ▼
[Zero Interactive Turn] ─── (Spawns detached_runner.py start)
       │
       ├──► Replies immediately with Task ID & Log path
       └──► Finishes turn; Discord channel unlocked
               │
               ▼
   [Detached Worker Process] (Independent Process Group)
         ├── Redirects stdout/stderr to /workspace/data/detached_tasks/<id>/run.log
         ├── Enforces execution timeout
         └── Upon Exit / Error / Timeout:
               │
               ▼
     [tools/outbox.py] ─── (Atomic append to /workspace/data/outbox/pending.jsonl)
               │
               ▼
     [bridge_scheduler.py] ─── (Periodic flush delivers notification to Discord)
```

---

## ⚠️ Distinction from Related Systems

* **`crontab-verify` / Karakos (`schedule.json`):** Used for **recurring** cron/interval maintenance (e.g. daily backups, hourly watchdogs). Do NOT use `detached-task` for recurring cron jobs.
* **`remote-transfer`:** Specialized for multi-megabyte/gigabyte SSH/rsync file migrations with hardware bandwidth governors (`--bwlimit`) and `systemd-inhibit`.
* **`detached-task`:** Used for **one-off, ad-hoc commands** that take >3 minutes (e.g. database migrations, deep repo test suites, media transcoding, large git clones).
