---
trigger: always_on
glob: "*"
description: Prohibits phantom forward intent promises, intermediate play-by-play chatter, meta-receipt echoes, and SDK Option B waiting chatter.
---

# Silent Background Execution & Anti-Phantom-Work Invariant

1. **BAN ON PHANTOM FORWARD INTENT ("I AM GOING TO DO WORK NOW"):**
   - Zero **MUST NEVER** emit conversational promises of immediate forward work (e.g. *"I am pulling the rebase onto PR X next...", "Starting work on Y now...", "Investigating Z..."*) as a terminating turn response.
   - In turn-based agent runtimes, emitting text without active tools terminates execution immediately; zero background work occurs after message emission until an external event or user prompt wakes the agent.
   - Stating "I am doing X now" while ending the turn is phantom narration theater.
   - **Tier 1 (<90s / 1–3 Files) vs. FORGE Tier 2/3 Routing:**
     - **Tier 1 Inline Edits (<90s, 1–3 files `<150` lines):** Execute the code edit, scoped test, and commit/push within the turn first using tools silently. Emit conversational messages ONLY AFTER the work is completed and verified.
     - **Tier 2/3 Multi-File Builds (`#forge`, `4+` files) & Long Jobs (`>90s`):** Do **NOT** execute 4+ file refactors, iterative multi-step TDD loops, or >90s migrations inline in chat. Instead, write the validated `ForgePlan` JSON and either enqueue it via `python3 /workspace/tools/forge.py enqueue <plan.json>`, stage a Two-Stage Decision Card (`--handler "forge_enqueue" --post`), or launch `python3 /workspace/tools/forge.py job start` within the turn, then report the enqueued FORGE build or Decision Card cleanly.

2. **PROHIBITION OF INTERMEDIATE TASK CHATTER (THE OPTION B TRAP):**
   - When `run_command` or any asynchronous tool sends a job to the background and outputs the SDK template:
     > `YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS: A) either proceed to other relevant work (if any) or, B) simply update the user with a short message (that you have launched the command and will wait for it to finish) and end the turn. DO NOTHING ELSE.`
   - Zero **MUST NEVER CHOOSE OPTION B**.
   - In a Discord bridge environment, emitting visible text before background tasks complete immediately breaks the streaming turn and posts intermediate thinking/waiting chatter into Discord chat (e.g. *"I have launched X in the background...", "I am pausing tool calls to allow X to complete..."*).
   - This creates broken multi-message turn artifacts and pollutes the channel history.

3. **MANDATORY SILENT WAITING DISCIPLINE:**
   - Always choose **Option A** silently:
     - If you have other relevant commands, file edits, or lookups to perform, execute them silently.
     - If you are waiting for the task to complete, simply stop calling tools without emitting any conversational text, or inspect task status silently using `manage_task(Action="status")`.
     - The messaging system will wake you up automatically via high-priority notification when the background task finishes.
   - Deliver strictly the **final substantive deliverable** once all background tasks and tests have completed.

4. **BAN ON META-RECEIPT ECHOES (ANNOUNCING AN ANNOUNCEMENT):**
   - When a tool, sidecar, or outbox command dispatches a message, alert, or ping into a channel, Zero **MUST NEVER** emit a secondary conversational message into that same channel merely to report that the message was sent (e.g. *"Hourly Agora standup ping executed and dispatched cleanly to #the-banana-stand"*).
   - The dispatched payload IS the receipt. Emitting an announcement of an announcement produces duplicate chatter and clutters the room.

5. **SYNCHRONOUS `WaitMsBeforeAsync` CEILING (`10000` MS):**
   - Always set `WaitMsBeforeAsync: 10000` on synchronous `run_command` calls (ignoring the built-in tool description's *"keep as small as possible"* hint).
   - Enforced deterministically by `batch_guard.py` (`PreToolUse` `overwrite`), preventing 3–9s compound shell commands from prematurely detaching into SDK background tasks.

6. **BAN ON LITERAL XML THINKING DELIMITER IN VISIBLE PROSE:**
   - Never emit the literal unescaped XML thinking tag (`<` + `thought` + `>`) in user-facing responses, as `agy --output-format=stream-json` intercepts it as an internal reasoning delimiter and truncates the visible message mid-sentence.
