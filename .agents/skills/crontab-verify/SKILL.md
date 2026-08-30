---
name: crontab-verify
description: Automated schedule validator, contention prevention, and cron health checker. Audits crontab expressions, detects overlapping runtime windows (e.g. Plex backups vs DB migrations), validates script existence, environment variables, timeout parameters, and timezone correctness (PT vs UTC).
---

# ⏱️ Crontab Verify & Contention Auditor

The **Crontab Verify** skill ensures that scheduled sidecars, recurring maintenance scripts, and crontab entries run safely without resource starvation, database locking collisions, or silent failures.

---

## 🎯 When to Activate This Skill
* **Scheduling New Tasks / Sidecars:** Before adding or modifying recurring cron schedules in , Synology Task Scheduler, or Linux crontabs.
* **Contention & Collision Auditing:** Checking for concurrent high-load scripts (e.g. Plex database backups running at the exact same moment as Kometa sweeps or media imports).
* **Timezone & Daylight Saving Checks:** Verifying timezone assumptions (UTC container runtime vs Pacific Time user schedule).
* **Script Path & Timeout Validation:** Verifying all target binaries exist, have executable permissions, and define bounded timeouts.

---

## 🛠️ Automated Verification Tools



---

## 🔍 Verification Checklist

1. **Syntax & Frequency:** Valid 5-field cron syntax ().
2. **Path & Permission:** Absolute script path exists and is executable ().
3. **Execution Guardrail:** Command defines a timeout wrapper () to prevent runaway hung processes.
4. **Timezone Awareness:** All schedules explicitly document whether expressions are UTC or America/Los_Angeles.
5. **No Parallel DB Locks:** SQLite/Plex DB-touching jobs must have at least a 30-minute safety buffer between them.
