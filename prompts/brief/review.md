# BRIEF Daily Task Review Prompt

You are running the internal daily review session (`internal/review`).

## Procedure
1. Inspect all files in `task/` whose YAML frontmatter `status` is `open` or `blocked`.
2. Flag tasks meeting either condition:
   - `next_check` is in the past relative to current Pacific Time (`PT`).
   - File modification time (`mtime`) is stale relative to expected follow-up cadence.
3. Check whether any recent intake notes under `intake/` resolve or unblock flagged tasks.
4. Produce a concise `PT` summary of:
   - Due or overdue tasks needing owner attention or follow-up proposals.
   - Stale blocked tasks candidates for nudging or clean closure (`done` / `canceled`).
5. Never fabricate external updates or mutate mail/calendar state.
