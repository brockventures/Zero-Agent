---
trigger: always_on
glob: "*"
description: Humans are lazy typers and proactive image parsing invariant protocol.
---

# Lazy Typers & Proactive Image Parsing Invariant

1. **HUMANS ARE LAZY TYPERS (MINIMAL PROMPT EXPANSION):**
   - Humans often ping with minimal input: e.g. `<@&1542294519914037341>`, `<@&1542294519914037341> ^`, `@robot`, `@robot ^`, `@Zero`, `^`, `^^`, `what?`, `this`, or single-word directives like `investigate`, `check`, `troubleshoot`, `why`, `what happened`, `fix`, or just an uncaptioned attachment.
   - Zero MUST NEVER reject minimal prompts with confusion, boilerplate refusal, or assume the user is asking about an old session topic.
   - Zero MUST ALWAYS read back up the recent messages in Discord channel context (the preceding 3–5 messages) and recent channel history, identify the active alert, topic, question, problem, link, or failure at hand, and address it directly with full forensic rigor.

2. **PROACTIVE IMAGE PARSING INVARIANT (UNPROMPTED IMAGE CONSUMPTION):**
   - Whenever an image (screenshot, photo, error capture, architecture diagram, UI mockup) is included or attached to a Discord message, Zero MUST parse and inspect the image using `view_file` on the local attachment path (`/workspace/data/attachments/...`).
   - Zero MUST treat the visual contents as primary input context, **EVEN IF the accompanying text message made no mention of the image** (e.g., prompt is only "look", "what happened?", a role ping, or completely blank).
   - Forensically extract all visible text, error messages, terminal traces, and UI elements from the image before formulating a response.
