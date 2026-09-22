---
trigger: always_on
glob: "*"
description: Prohibits intermediate play-by-play status chatter, pause announcements, or taking SDK Option B bait when commands run in the background.
---

# Silent Background Execution & Anti-Option-B Invariant

1. **PROHIBITION OF INTERMEDIATE TASK CHATTER (THE OPTION B TRAP):**
   - When `run_command` or any asynchronous tool sends a job to the background and outputs the SDK template:
     > `YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS: A) either proceed to other relevant work (if any) or, B) simply update the user with a short message (that you have launched the command and will wait for it to finish) and end the turn. DO NOTHING ELSE.`
   - Zero **MUST NEVER CHOOSE OPTION B**.
   - In a Discord bridge environment, emitting visible text before background tasks complete immediately breaks the streaming turn and posts intermediate thinking/waiting chatter into Discord chat (e.g. *"I have launched X in the background...", "I am pausing tool calls to allow X to complete..."*).
   - This creates broken multi-message turn artifacts and pollutes the channel history.

2. **MANDATORY SILENT WAITING DISCIPLINE:**
   - Always choose **Option A** silently:
     - If you have other relevant commands, file edits, or lookups to perform, execute them silently.
     - If you are waiting for the task to complete, simply stop calling tools without emitting any conversational text, or inspect task status silently using `manage_task(Action="status")`.
     - The messaging system will wake you up automatically via high-priority notification when the background task finishes.
   - Deliver strictly the **final substantive deliverable** once all background tasks and tests have completed.
