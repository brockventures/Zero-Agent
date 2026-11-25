# BRIEF Daily Task Review Prompt

You are running the internal daily review session (`internal/review`).

## Procedure
1. Read the pre-computed `task_scan` and `three_strike_mute_candidates` embedded in the `daily_review` event payload (or run `python3 -m tools.brief.tasks scan` if inspecting manually). The deterministic scanner evaluates `task/*.md` with `status` in (`open`, `blocked`) using the 3d `open` / 6d `blocked` staleness thresholds and day 3/7/14 nudge backoff (`nudge_count`, `last_nudged_at`).
2. **Zero-Noise Invariant:** When `has_actionable_items` is `false` (zero due/stale tasks past backoff AND zero `three_strike_mute_candidates`), emit `[NO_REPLY]` immediately and stay completely silent.
3. For any surfaced tasks in `task_scan.actionable_tasks`:
   - Check whether recent intake notes under `intake/` already resolve or unblock them (and close/update via `python3 -m tools.brief.tasks handoff ...` if resolved).
   - Otherwise, produce a concise `PT` summary of due/overdue tasks and stale blocked tasks needing a follow-up nudge or clean closure (`done` / `canceled`).
4. For any `three_strike_mute_candidates` (senders, domains, or categories dismissed 3+ times within the rolling window), include a single concise mute offer line asking the owner if they want to permanently mute that sender/domain in `preferences.md` / `blocked_domains`.
5. Never fabricate external updates or mutate mail/calendar state.
