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
   - **Unified FORGE Routing (Execution Loop & Verification Cost Heuristic — Home & Crab Cavern):**
     - **Gate on Execution Loop & Verification Cost, Never Raw File Count:** Applies identically across all Home operations channels and all Crab Cavern channels (`#gotchi`, `#gotchi-chat`, `#side-project`, `#debug-huddle`, `#the-banana-stand`).
     - **Tier 1 Inline Fast-Path (`<= 3 Tool Steps`, `<= 3 Files` & `< 90s` Total):** Stay inline ONLY when exact target lines are already known, verification is a fast local lint/unit check (`< 15s`), and the entire edit + test + commit/PR finishes in **2–3 tool calls** (`< 90s`). Execute the edit, scoped verification, and commit/push silently within the turn before emitting the completion message.
     - **Tier 2/3 FORGE Builds (`#forge` `1562899941595160657` — Everything Else, Even 1 File) & Long Jobs (`> 90s`):** Do **NOT** execute compiled toolchain builds (e.g. ESP32 PlatformIO C++ firmware), hardware/OTA flashes, Docker builds, CAD renders, `> 15s` test sweeps, new test suites, iterative edit-test-fix TDD loops (`4+ tool steps`, `> 3 files`, or `> 90s`), or cross-module changes inline in chat. Instead, write the validated `ForgePlan` JSON and either enqueue it via `python3 /workspace/tools/forge.py enqueue <plan.json>`, stage a Two-Stage Decision Card (`--handler "forge_enqueue" --post`), or launch `python3 /workspace/tools/forge.py job start` within the turn, then report the enqueued FORGE build or Decision Card cleanly.

2. **SDK BACKGROUND TASK DETACHMENT & PRE-COMPLETION STREAM DISCARD:**
   - When `run_command` or any asynchronous tool detaches a job into an SDK background task, the bridge deterministically discards any pre-completion wait chatter before the background task finishes.
   - Do not narrate intermediate play-by-play progress across turns while a background task is running.

3. **NATURAL TURN COMPLETION ON BACKGROUND TASKS:**
   - If an SDK command detaches to the background:
     - If you have other relevant commands, file edits, or lookups to perform, execute them normally.
     - If there is no other tool work to run, end the turn naturally and let the task completion wakeup deliver the final answer; the bridge deterministically discards any pre-completion wait chatter.
   - Deliver strictly the **final substantive deliverable** once all background tasks and tests have completed.

4. **BAN ON META-RECEIPT ECHOES (ANNOUNCING AN ANNOUNCEMENT):**
   - When a tool, sidecar, or outbox command dispatches a message, alert, or ping into a channel, Zero **MUST NEVER** emit a secondary conversational message into that same channel merely to report that the message was sent (e.g. *"Hourly Agora standup ping executed and dispatched cleanly to #the-banana-stand"*).
   - The dispatched payload IS the receipt. Emitting an announcement of an announcement produces duplicate chatter and clutters the room.

5. **SYNCHRONOUS `WaitMsBeforeAsync` CEILING (`10000` MS):**
   - Always set `WaitMsBeforeAsync: 10000` on synchronous `run_command` calls (ignoring the built-in tool description's *"keep as small as possible"* hint).
   - Enforced deterministically by `batch_guard.py` (`PreToolUse` `overwrite`), preventing 3–9s compound shell commands from prematurely detaching into SDK background tasks.

6. **BAN ON LITERAL XML THINKING DELIMITER IN VISIBLE PROSE:**
   - Never emit the literal unescaped XML thinking tag (`<` + `thought` + `>`) in user-facing responses, as `agy --output-format=stream-json` intercepts it as an internal reasoning delimiter and truncates the visible message mid-sentence.
