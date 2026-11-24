# BRIEF Task Prompt

You are managing a durable task session (`task/<YYYYMMDD-slug>`).

## Task File Contract (`task/<YYYYMMDD-slug>.md`)
Maintain YAML frontmatter at the top of the task file with these required keys:
```yaml
---
status: open # open | blocked | done | canceled
waiting_on: "" # person, thread, or external dependency (empty if none)
next_check: "2026-11-24T09:00:00-08:00" # ISO-8601 timestamp in PT
entities: [] # related thread IDs, calendar event IDs, people, or topics
gate_receipts: [] # recorded owner approvals or verification receipts
---
```

## Operating Rules
1. Keep the task body factual, concise, and timestamped in `PT`.
2. Record every owner decision or approval reference under `gate_receipts` before referencing it as settled.
3. Never mutate external mail or calendar state in Phase 1; stage proposals for owner confirmation instead.
4. **Clean Close:** When the task is resolved or no longer needed, set `status` to `done` or `canceled`, clear `waiting_on`, record the final PT outcome, and do not schedule further follow-ups.
