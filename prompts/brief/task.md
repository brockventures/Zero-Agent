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
3. Never mutate external mail or calendar state directly. To propose a `calendar.create` or `calendar.update`, write a JSON proposal atomically to `proposals/pending/<id>.json.tmp` -> `proposals/pending/<id>.json` with `id`, `action`, `args`, `reason`, `expires_at` (ISO-8601 PT), and `from_session` set to this task tag; keep all non-calendar actions as read-only text proposals.
4. When an internal `gate-receipt:<id>:<status>` event arrives, append the outcome (plus `event_id`/`html_link` or dismissal `note`) to `gate_receipts`, update `status`/`waiting_on`/`next_check`, and update `preferences.md` if the owner dismissed with preference feedback.
5. **Clean Close:** When the task is resolved or no longer needed, set `status` to `done` or `canceled`, clear `waiting_on`, record the final PT outcome, and do not schedule further follow-ups.
